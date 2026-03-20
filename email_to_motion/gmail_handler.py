"""
gmail_handler.py — Gmail CC/alias ingestion pipeline.

Polls Gmail INBOX via IMAP for unread emails where a bot alias
(SMTP_USER+suffix@...) appears in To or CC, then dispatches to the
appropriate action handler and replies via SMTP.

Supported aliases:
  +scheduling  →  availability check (checks Chris's Outlook calendar)

IMAP credentials reuse SMTP_USER / SMTP_PASSWORD (Gmail app password).
No additional environment variables are required.
"""

import email
import email.header
import email.utils
import imaplib
import json
import logging
import pathlib
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText

from . import activity_log
from . import config
from .availability import (
    _ask_claude,
    _ask_claude_match,
    _build_summary,
    compute_free_slots,
    fetch_busy_blocks,
)
from .utils import call_with_retries, parse_claude_json

log = logging.getLogger(__name__)

# ── Alias → action routing ────────────────────────────────────────────────────

# Map Gmail + suffix to an action name.  Add new aliases here as needed.
_ALIAS_ACTIONS: dict[str, str] = {
    "scheduling": "scheduling",
}

# ── Deduplication state ───────────────────────────────────────────────────────

_STATE_FILE = pathlib.Path("~/.email_to_motion/gmail_processed.json").expanduser()
_MAX_SEEN = 1000   # cap to avoid unbounded growth


def _load_processed() -> set[str]:
    if not _STATE_FILE.exists():
        return set()
    try:
        with _STATE_FILE.open() as f:
            data = json.load(f)
        return set(data.get("processed", []))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("gmail: could not load state file: %s", exc)
        return set()


def _save_processed(seen: set[str]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = list(seen)[-_MAX_SEEN:]
    try:
        with _STATE_FILE.open("w") as f:
            json.dump({"processed": trimmed}, f)
    except OSError as exc:
        log.error("gmail: could not save state file: %s", exc)


# ── Header / address utilities ────────────────────────────────────────────────

def _decode_header(value: str | None) -> str:
    """Decode an RFC 2047-encoded email header value to a plain string."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded_parts = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            decoded_parts.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_parts.append(fragment)
    return "".join(decoded_parts)


def _parse_addresses(header_value: str) -> list[str]:
    """Return a list of lowercase email addresses from a header value."""
    if not header_value:
        return []
    pairs = email.utils.getaddresses([header_value])
    return [addr.lower() for _, addr in pairs if addr]


def _get_display_name(header_value: str) -> str:
    """Return the display name (or local part of address) from a header value."""
    pairs = email.utils.getaddresses([header_value])
    if not pairs:
        return ""
    name, addr = pairs[0]
    if name:
        return name
    # Fall back to local part of email address
    return addr.split("@")[0] if addr else ""


def _smtp_base_and_domain() -> tuple[str, str]:
    """Split SMTP_USER into (local_part, domain), both lower-cased."""
    user = config.SMTP_USER.lower()
    if "@" in user:
        local, domain = user.split("@", 1)
        return local, domain
    return user, ""


# ── Alias detection ───────────────────────────────────────────────────────────

def _find_bot_alias(msg: email.message.Message) -> str | None:
    """
    Return the action name if a known bot alias appears in To or CC.
    Returns None if no matching alias is found.
    """
    smtp_base, smtp_domain = _smtp_base_and_domain()
    all_to_cc = (
        _parse_addresses(_decode_header(msg.get("To", "")))
        + _parse_addresses(_decode_header(msg.get("CC", "")))
    )
    for addr in all_to_cc:
        if "@" not in addr:
            continue
        local, domain = addr.split("@", 1)
        if domain != smtp_domain:
            continue
        if "+" not in local:
            continue
        base, suffix = local.split("+", 1)
        if base == smtp_base and suffix in _ALIAS_ACTIONS:
            return _ALIAS_ACTIONS[suffix]
    return None


def _is_bot_address(addr: str) -> bool:
    """Return True if addr is any variant of the bot's own address."""
    smtp_base, smtp_domain = _smtp_base_and_domain()
    addr = addr.lower()
    if "@" not in addr:
        return False
    local, domain = addr.split("@", 1)
    if domain != smtp_domain:
        return False
    # Strip any +suffix before comparing
    bare_local = local.split("+")[0]
    return bare_local == smtp_base


def _extract_other_recipients(msg: email.message.Message) -> list[str]:
    """
    Return a list of raw address strings for all participants *except*
    the bot's own addresses (base + any alias) and SMTP_USER itself.
    Includes From, To, and CC.
    """
    all_fields = (
        _decode_header(msg.get("From", ""))
        + ", " + _decode_header(msg.get("To", ""))
        + ", " + _decode_header(msg.get("CC", ""))
    )
    pairs = email.utils.getaddresses([all_fields])
    seen_addrs: set[str] = set()
    result = []
    for name, addr in pairs:
        if not addr:
            continue
        lower = addr.lower()
        if _is_bot_address(lower):
            continue
        if lower in seen_addrs:
            continue
        seen_addrs.add(lower)
        formatted = email.utils.formataddr((name, addr)) if name else addr
        result.append(formatted)
    return result


# ── Email body extraction ─────────────────────────────────────────────────────

def _extract_body(msg: email.message.Message) -> str:
    """
    Extract the plain-text body from a (possibly multipart) email.
    Falls back to decoding the first text/html part if no text/plain exists.
    """
    if msg.is_multipart():
        plain = None
        html = None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and plain is None:
                charset = part.get_content_charset() or "utf-8"
                try:
                    plain = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    plain = ""
            elif ct == "text/html" and html is None:
                charset = part.get_content_charset() or "utf-8"
                try:
                    html = part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    html = ""
        return plain if plain is not None else (html or "")
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            return msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            return ""


# ── Reply assembly ────────────────────────────────────────────────────────────

def _build_greeting(other_recipients: list[str]) -> str:
    """
    Build the greeting line based on the number of other participants.
    """
    if not other_recipients:
        return "Hello,"

    names = []
    for raw in other_recipients:
        name, addr = email.utils.parseaddr(raw)
        # Use display name if available; otherwise the local part of the address
        if name:
            # Use just the first name if a full name is provided
            first = name.strip().split()[0]
            names.append(first)
        elif addr:
            names.append(addr.split("@")[0])

    if len(names) == 1:
        return f"Hello {names[0]},"
    elif len(names) == 2:
        return f"Hello {names[0]} and {names[1]},"
    else:
        return "Hello All,"


def _build_addressing_line(addressing: str) -> str:
    """
    Build the 'X's availability during the requested period is:' line.

    Addressing ∈ {"Chris", "Christopher"}  → Chris' availability…
    Addressing ∈ {"Dr. DeGroot", "Prof. DeGroot", …}  → Dr. DeGroot's availability…
    Default (anything else / unclear)  → Chris' availability…
    """
    addressing_lower = addressing.lower().strip()
    title_prefixes = ("dr.", "dr ", "prof.", "prof ", "professor")
    if any(addressing_lower.startswith(p) for p in title_prefixes):
        return "Dr. DeGroot's availability during the requested period is:"
    # Default: treat as Chris
    return "Chris' availability during the requested period is:"


_SIGNOFF = (
    "Cheers,\n"
    "Chris DeGroot's Digital Assistant\n"
    "(Note: this assistant was built by Chris DeGroot and uses Claude AI)"
)


def _build_reply(
    greeting: str,
    addressing_line: str,
    availability_block: str,
) -> str:
    """Assemble the full email reply body."""
    return (
        f"{greeting}\n\n"
        f"{addressing_line}\n\n"
        f"{availability_block}\n\n"
        f"{_SIGNOFF}"
    )


# ── SMTP reply ────────────────────────────────────────────────────────────────

def _send_reply(
    original_msg: email.message.Message,
    reply_body: str,
    other_recipients: list[str],
) -> None:
    """
    Send reply_body as a plain-text email that threads with the original message.

    From is always SMTP_USER (never the alias).
    To is Reply-To or From of the original.
    Cc is the other non-bot participants.
    """
    reply_to_raw = original_msg.get("Reply-To") or original_msg.get("From", "")
    reply_to_addr = _parse_addresses(_decode_header(reply_to_raw))
    to_addr = reply_to_addr[0] if reply_to_addr else config.SMTP_USER

    subject = _decode_header(original_msg.get("Subject", ""))
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg_id = original_msg.get("Message-ID", "")
    original_refs = original_msg.get("References", "")
    new_refs = f"{original_refs} {msg_id}".strip()

    mime_msg = MIMEText(reply_body, "plain", "utf-8")
    mime_msg["From"] = config.SMTP_USER
    mime_msg["To"] = to_addr
    if other_recipients:
        mime_msg["Cc"] = ", ".join(other_recipients)
    mime_msg["Subject"] = subject
    if msg_id:
        mime_msg["In-Reply-To"] = msg_id
        mime_msg["References"] = new_refs

    # Collect all recipient addresses for the SMTP envelope
    cc_addrs = [a for raw in other_recipients for a in _parse_addresses(raw)]
    all_rcpt = [to_addr] + cc_addrs

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.sendmail(config.SMTP_USER, all_rcpt, mime_msg.as_string())

    log.info("gmail: reply sent — subject=%r to=%r cc_count=%d", subject, to_addr, len(cc_addrs))


# ── Claude prompts for scheduling ─────────────────────────────────────────────

_SCHEDULING_SYSTEM_PROMPT = (
    "You are a scheduling assistant analyzing email requests on behalf of "
    "Chris DeGroot (also known as Dr. DeGroot or Prof. DeGroot). "
    "Extract scheduling parameters from the email thread. "
    "Return ONLY a JSON object — no markdown, no explanation."
)

_SCHEDULING_PROMPT_TEMPLATE = """\
Today's date is {today}. Tomorrow is {tomorrow}.

Analyze this email thread and extract scheduling parameters for Chris DeGroot's \
availability.

EMAIL THREAD:
{email_text}

Return a JSON object with exactly these keys (no extras):
{{
  "duration_minutes": <integer — meeting length in minutes; default 30 if not mentioned>,
  "start_date": "<YYYY-MM-DD — first day of requested period; default {tomorrow} if not mentioned>",
  "end_date": "<YYYY-MM-DD — last day of requested period; default 7 days after start_date if not mentioned>",
  "addressing": "<how the email addresses Chris DeGroot — look at the greeting in the body; e.g. 'Chris', 'Christopher', 'Dr. DeGroot', 'Prof. DeGroot'; default 'Chris' if unclear>",
  "their_availability": "<verbatim text of any availability stated by other participants in the thread, or null if none>"
}}

Rules:
- duration_minutes: look for phrases like '30-minute meeting', '1 hour call', '45 min'. Default 30.
- start_date / end_date: look for 'this week', 'next week', 'March 15–20', etc. \
Default: start_date = {tomorrow}, end_date = {default_end}.
- addressing: the greeting/salutation in the email body that addresses Chris. \
Use 'Chris' as default if the email body is ambiguous or not addressed to Chris personally.
- their_availability: any dates/times other participants say they are free. \
Null if not mentioned.
"""


def _parse_scheduling_params(email_text: str) -> dict:
    """
    Ask Claude to extract scheduling parameters from the email thread.

    Returns a dict with keys: duration_minutes, start_date, end_date,
    addressing, their_availability.
    Falls back to safe defaults on parse failure.
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    default_end = tomorrow + timedelta(days=6)

    prompt = _SCHEDULING_PROMPT_TEMPLATE.format(
        today=today.isoformat(),
        tomorrow=tomorrow.isoformat(),
        default_end=default_end.isoformat(),
        email_text=email_text,
    )

    response = call_with_retries(
        config.claude.messages.create,
        model="claude-sonnet-4-5-20250929",
        max_tokens=512,
        system=_SCHEDULING_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    try:
        params = parse_claude_json(raw)
    except Exception as exc:
        log.warning("gmail: could not parse scheduling params JSON: %s — using defaults", exc)
        params = {}

    # Validate / apply defaults
    tomorrow_str = tomorrow.isoformat()
    default_end_str = default_end.isoformat()

    duration = params.get("duration_minutes", 30)
    if not isinstance(duration, int) or duration < 15:
        duration = 30

    start_str = params.get("start_date") or tomorrow_str
    end_str = params.get("end_date") or default_end_str

    try:
        start_date = date.fromisoformat(start_str)
    except (ValueError, TypeError):
        start_date = tomorrow

    try:
        end_date = date.fromisoformat(end_str)
    except (ValueError, TypeError):
        end_date = tomorrow + timedelta(days=6)

    if end_date < start_date:
        end_date = start_date + timedelta(days=6)

    addressing = params.get("addressing") or "Chris"
    their_availability = params.get("their_availability") or None
    if isinstance(their_availability, str) and not their_availability.strip():
        their_availability = None

    return {
        "duration_minutes": duration,
        "start_date": start_date,
        "end_date": end_date,
        "addressing": addressing,
        "their_availability": their_availability,
    }


# ── Scheduling action handler ─────────────────────────────────────────────────

def _handle_scheduling(
    msg: email.message.Message,
    email_text: str,
    other_recipients: list[str],
) -> None:
    """
    Full pipeline for the +scheduling alias:
      1. Parse requested duration, timeframe, addressing, others' availability.
      2. Fetch calendar and compute free slots.
      3. Format availability (with match mode if others stated their availability).
      4. Build and send the reply.
    """
    log.info("gmail: handling scheduling request")

    params = _parse_scheduling_params(email_text)
    duration_minutes: int = params["duration_minutes"]
    start_date: date = params["start_date"]
    end_date: date = params["end_date"]
    addressing: str = params["addressing"]
    their_availability: str | None = params["their_availability"]

    log.info(
        "gmail: scheduling params — duration=%dmin start=%s end=%s addressing=%r match=%s",
        duration_minutes, start_date, end_date, addressing, their_availability is not None,
    )

    try:
        busy = fetch_busy_blocks(start_date, end_date)
    except Exception as exc:
        log.error("gmail: could not fetch calendar: %s", exc, exc_info=True)
        return

    from datetime import timedelta as _td
    min_duration = _td(minutes=duration_minutes)
    summary = _build_summary(busy, min_duration=min_duration)

    try:
        if their_availability:
            availability_block = _ask_claude_match(summary, their_availability, duration_minutes)
        else:
            availability_block = _ask_claude(summary, duration_minutes)
    except Exception as exc:
        log.error("gmail: Claude error while formatting availability: %s", exc, exc_info=True)
        return

    greeting = _build_greeting(other_recipients)
    addressing_line = _build_addressing_line(addressing)
    reply_body = _build_reply(greeting, addressing_line, availability_block)

    try:
        _send_reply(msg, reply_body, other_recipients)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("gmail: SMTP error sending reply: %s", exc, exc_info=True)
        return

    activity_log.record(
        "gmail_scheduling",
        duration_minutes=duration_minutes,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        match_mode=their_availability is not None,
    )


# ── Per-message dispatcher ────────────────────────────────────────────────────

def _process_message(msg: email.message.Message, action: str) -> None:
    """Dispatch a single message to the appropriate action handler."""
    email_text = _extract_body(msg)
    other_recipients = _extract_other_recipients(msg)

    if action == "scheduling":
        _handle_scheduling(msg, email_text, other_recipients)
    else:
        log.warning("gmail: unknown action %r — skipping", action)


# ── IMAP polling entry point ──────────────────────────────────────────────────

def poll_and_process() -> int:
    """
    Poll Gmail INBOX for unread messages addressed to a bot alias.

    For each new, unprocessed message:
      - Determine the action from the alias suffix.
      - Dispatch to the appropriate handler.
      - Record the Message-ID to avoid reprocessing.

    Returns the number of messages processed.
    """
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        log.debug("gmail: SMTP_USER or SMTP_PASSWORD not configured — skipping poll")
        return 0

    processed = _load_processed()
    newly_processed: list[str] = []
    count = 0

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    except (imaplib.IMAP4.error, OSError) as exc:
        log.error("gmail: IMAP connection failed: %s", exc)
        return 0

    try:
        imap.login(config.SMTP_USER, config.SMTP_PASSWORD)
        imap.select("INBOX")

        _, data = imap.search(None, "UNSEEN")
        msg_nums = data[0].split() if data and data[0] else []

        if not msg_nums:
            log.debug("gmail: no unread messages")
            return 0

        log.info("gmail: found %d unread message(s)", len(msg_nums))

        for num in msg_nums:
            try:
                _, fetch_data = imap.fetch(num, "(RFC822)")
            except imaplib.IMAP4.error as exc:
                log.error("gmail: IMAP fetch error for msg %s: %s", num, exc)
                continue

            if not fetch_data or not fetch_data[0]:
                continue

            raw_bytes = fetch_data[0][1] if isinstance(fetch_data[0], tuple) else None
            if not raw_bytes:
                continue

            msg = email.message_from_bytes(raw_bytes)
            msg_id = msg.get("Message-ID", "").strip()

            if not msg_id:
                log.debug("gmail: message %s has no Message-ID — skipping", num)
                continue

            if msg_id in processed:
                log.debug("gmail: already processed Message-ID %s — skipping", msg_id)
                continue

            action = _find_bot_alias(msg)
            if action is None:
                log.debug("gmail: no bot alias in To/CC for msg %s — skipping", msg_id)
                # Still mark as "seen" in state so we don't re-check it next poll.
                # We do NOT mark it as IMAP-seen to leave unrelated mail unread.
                newly_processed.append(msg_id)
                continue

            subject = _decode_header(msg.get("Subject", ""))
            log.info("gmail: processing msg — id=%s action=%r subject=%r", msg_id, action, subject)

            try:
                _process_message(msg, action)
                count += 1
            except Exception as exc:
                log.error("gmail: unhandled error processing msg %s: %s", msg_id, exc, exc_info=True)

            # Mark IMAP message as Seen after processing
            try:
                imap.store(num, "+FLAGS", "\\Seen")
            except imaplib.IMAP4.error as exc:
                log.warning("gmail: could not mark msg %s as Seen: %s", num, exc)

            newly_processed.append(msg_id)

    except imaplib.IMAP4.error as exc:
        log.error("gmail: IMAP error: %s", exc)
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    if newly_processed:
        processed.update(newly_processed)
        _save_processed(processed)

    log.info("gmail: poll complete — processed %d message(s)", count)
    return count
