# EmailToMotion — Claude Code Guide

## What this project is

A personal productivity bot that runs as a background service. It connects to Slack via Socket Mode and polls configured channels for forwarded emails. Depending on which channel an email lands in, it:

- Creates tasks in [Motion](https://usemotion.com) (AI scheduling app)
- Sends calendar invites to Outlook via SMTP + `.ics`
- Saves structured Markdown notes to an Obsidian vault
- Signs and dates PDF forms
- Generates LaTeX reference letters
- Checks for calendar conflicts and drafts rescheduling emails
- Posts watch-date reminders for time-sensitive notes

Claude (Anthropic API) drives all content extraction and generation. There is no web server — the app connects outbound to Slack's WebSocket and polls on an interval.

## Deployment environments

The app runs on **macOS** during development and testing, and on a **Linux server** in production. Code must work on both:

- Use `pathlib.Path` for all file paths (never string concatenation).
- Use `Path.expanduser()` when resolving paths that may start with `~/`.
- Do not use macOS-only tools or APIs.
- The `systemd/` directory contains the Linux service unit for production deployment.

## Environment variables and `.env`

**Do not modify `.env` or `.env.example`.** The owner manages these files directly. If a new feature requires a new env var:

1. Add it to `config.py` with a sensible default (empty string if optional).
2. Document it in `.env.example` with a comment — but do not set a value.
3. Note it in `README.md` under the relevant section.
4. Add it to `config.validate()` only if it is strictly required at startup.

All configuration is accessed via the `config` module — never read `os.environ` directly outside of `config.py`.

## Email constraints

The owner's university address (`christopher.degroot@uwo.ca`) has no API access and cannot be used programmatically. **All outbound email goes through the dedicated Gmail account** configured as `SMTP_USER` (currently `ctdegroot.digital.assistant@gmail.com`).

This applies to every feature that sends email: calendar invites, rescheduling drafts, reference letters, and any future features. Do not attempt to send from or interact with the university address via code.

## Logging

Every module must use the standard `logging` module, not `print`, for operational messages:

```python
import logging
log = logging.getLogger(__name__)

log.debug("detail only useful when debugging")
log.info("normal operational event — %s", detail)
log.warning("unexpected but recoverable — %s", detail)
log.error("failure requiring attention — %s", detail, exc_info=True)
```

Use `exc_info=True` on `log.error` calls inside except blocks so the traceback is captured. Reserve `print()` for startup/shutdown messages in `cli.py` and `socket_listener.py` only.

## Running the app

```bash
# All commands from the repo root, with the venv active
source .venv/bin/activate

# Run (polls every 60 s + Socket Mode listener)
python -m email_to_motion

# Run tests
pytest

# Run a single test file
pytest tests/test_utils.py -v

# Type-check (if pyright/mypy installed)
pyright email_to_motion/
```

Requires a `.env` file (copy `.env.example`). Required variables: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `MOTION_API_KEY`, `MOTION_WORKSPACE_ID`, `MOTION_ASSIGNEE_ID`, `ANTHROPIC_API_KEY`, `SMTP_USER`, `SMTP_PASSWORD`, `CALENDAR_EMAIL`.

## Architecture

```
email_to_motion/
  __main__.py          entry point
  cli.py               argument parsing, startup orchestration
  config.py            env vars + shared client singletons (config.slack, config.claude)
  socket_listener.py   Slack Socket Mode — routes all events to handlers
  scheduler.py         background cron jobs (conflict check, watch-date scan)

  # ── Pipelines (one per action type) ───────────────────────────────────────
  tasks.py             email → Motion task
  events.py            email → .ics calendar invite
  slack_notes_handler.py  email/URL/file → Obsidian .md note
  note_generator.py    Claude prompt + file I/O for notes
  vault_writer.py      Git push to Obsidian vault
  pdf_sign_handler.py  PDF sign + date (AcroForm and flat)
  ref_letter_handler.py  interactive reference letter generator

  # ── Features ───────────────────────────────────────────────────────────────
  availability.py      /availability slash command
  conflict_checker.py  ICS conflict detection + rescheduling email drafts
  watch_date_handler.py  deadline reminders from note frontmatter
  shortcuts.py         "Create Task" / "Create Calendar Event" message shortcuts
  activity_log.py      JSONL audit trail of all processed items

  # ── Shared utilities ───────────────────────────────────────────────────────
  slack_helpers.py     channel lookup, unprocessed message fetch, mark ✅
  utils.py             call_with_retries, get_with_retries, post_with_retries,
                       parse_claude_json
```

## Key conventions

### Error handling and retries

**Always** use these wrappers for external calls:

```python
# All Claude API calls
response = call_with_retries(config.claude.messages.create, model=..., ...)

# HTTP GET (ICS feeds, Slack file downloads)
resp = get_with_retries(url, timeout=30)

# HTTP POST (Motion API)
resp = post_with_retries(url, json=payload, headers=..., timeout=10)
```

Both HTTP helpers retry on transient errors (5xx, 429, connection errors) with exponential back-off. `post_with_retries` also respects the `Retry-After` header. `call_with_retries` retries on `RateLimitError`, `APITimeoutError`, `APIConnectionError`, `InternalServerError`.

### Claude JSON parsing

Claude responses that should be JSON must be parsed with `parse_claude_json()` (in `utils.py`), which strips accidental markdown fences:

```python
from .utils import parse_claude_json
result = parse_claude_json(response.content[0].text)
```

`parse_claude_json` raises `json.JSONDecodeError` on failure — callers must catch it.

### Asking Claude for lists

When a prompt may return zero items (e.g. task extraction respecting a "nothing to go to motion" note), the system prompt must include `"If there are no items, return an empty JSON array: []"`. The caller must handle the empty-list case explicitly — never assume a non-empty result.

### Slack confirmation pattern

Every pipeline that processes a message must:
1. Do the work
2. Call `mark_processed(channel_id, ts)` — adds ✅ to prevent reprocessing
3. Post a confirmation with `config.slack.chat_postMessage(thread_ts=ts, ...)`

`mark_processed` is idempotent (already-reacted is swallowed). Call it even on partial success so a message isn't retried and causes duplicates.

### SMTP

```python
with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
    server.ehlo(); server.starttls(); server.login(...); server.send_message(msg)
```

Always include `timeout=30`. SMTP calls belong inside per-item try/except blocks, not wrapping an entire loop — a timeout on one item must not abort the rest.

### Threading

Heavy work (Claude API, SMTP, Motion API, file I/O) runs in daemon threads spawned by `socket_listener._dispatch`. Do not do blocking work directly in `_dispatch`. Exception: `views.open` must be called within 3 seconds of a trigger, so modal openers run directly in `_dispatch`.

### Config access

Import `config` at module level and access clients as `config.slack` and `config.claude`. Never hold module-level references to the clients themselves — `init_clients()` runs after import time and the references won't be set yet.

```python
from . import config
# later, inside a function:
config.slack.chat_postMessage(...)
config.claude.messages.create(...)
```

## Slack channel routing

| Channel (env var) | Pipeline |
|---|---|
| `SLACK_MOTION_CHANNEL` | `tasks.process_channel()` |
| `SLACK_CALENDAR_CHANNEL` | `events.process_calendar_channel()` |
| `SLACK_NOTES_CHANNEL` | `slack_notes_handler.process_message()` |
| `SLACK_REFLETTER_CHANNEL` | `ref_letter_handler.process_message()` |
| `SLACK_SIGN_CHANNEL` | `pdf_sign_handler.process_message()` |
| `SLACK_REMINDERS_CHANNEL` | watch-date reminder posts (outbound only) |

## Writing new pipelines

A new pipeline module should follow this shape:

```python
# Module-level channel ID cache
_channel_id: str = ""

def init(channel_name: str):
    global _channel_id
    if not channel_name:
        return
    try:
        _channel_id = get_channel_id(channel_name.lstrip("#"))
    except Exception as e:
        print(f"⚠️  Could not resolve channel '{channel_name}': {e}")

def process_message(event: dict):
    # Guard against bot's own messages
    if event.get("bot_id") == config.OWN_BOT_ID:
        return
    channel_id = event.get("channel", "")
    ts = event.get("ts", "")
    try:
        # ... do work ...
        mark_processed(channel_id, ts)
        config.slack.chat_postMessage(channel=channel_id, thread_ts=ts, text="✅ Done")
    except Exception as e:
        log.error("pipeline: error: %s", e, exc_info=True)
        config.slack.chat_postMessage(channel=channel_id, thread_ts=ts,
                                      text=f"⚠️ Error: {e}")
```

Wire it up in `socket_listener.start()` (call `init`) and in `_dispatch`'s `events_api` block.

## Forwarding note convention

When a user forwards an email to both the calendar and tasks channels with routing instructions ("Place hold in calendar for X and Y — nothing to go to motion for this"), both `events.py` and `tasks.py` prompts treat the text before the first email separator as an authoritative routing whitelist. This pattern must be maintained in any new channel that accepts forwarded emails.

## Testing

Tests live in `tests/`. They use `unittest.mock` extensively — no live API calls in unit tests. Run `pytest` to execute all tests.

Integration-style tests (prefixed `test_integration_`) test fuller pipelines with mocked Slack/Claude/Motion responses.

**Every new module requires a corresponding `tests/test_<module>.py`.** Tests are not optional. At minimum cover:

- The Claude JSON parsing path (happy path and malformed/empty response)
- The empty-result case (Claude returns `[]`)
- Error handling (API failure posts a warning to Slack, does not crash the loop)
- Any non-trivial logic (date parsing, routing decisions, field extraction)

Follow the existing test style: `unittest.mock` for all external calls, no live API or network access, class-per-concern organisation. Run `pytest` before considering a feature complete.

## Current model

All Claude calls use `claude-sonnet-4-5-20250929` with `call_with_retries`. Do not hardcode a different model without updating all call sites.

## Planned / in-progress features

- **Gmail CC ingestion**: Receive emails where the Gmail account (`SMTP_USER`) is CC'd, detect the user's intent from the reply message, and reply directly to the email thread — without going through Slack. This will add a new `gmail_handler.py` module and a polling job in `scheduler.py`. All outbound replies use `SMTP_USER` (Gmail), not the university address.
