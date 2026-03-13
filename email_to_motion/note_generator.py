"""
note_generator.py — Convert email content to structured Obsidian-flavoured Markdown.

Called by slack_notes_handler when a message lands in the notes-inbox channel.
Uses Claude to extract structure, generate tags, and write a clean .md file.

Tag consistency:
  A canonical tag list is maintained at ~/.email_to_motion/known_tags.json.
  Claude is asked to reuse existing tags and only introduce new ones when
  nothing in the list fits. This makes tags self-consistent over time without
  requiring any upfront taxonomy work.

Stage 1: writes to NOTES_OUTPUT_PATH on the local filesystem for inspection.
Stage 2 (future): also push to Obsidian vault via Git.
"""

import glob as _glob_module
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from . import config
from .utils import call_with_retries


# ── Output sanitisation ───────────────────────────────────────────────────────

# Claude occasionally wraps its entire response in a code fence despite being
# told not to, e.g.:
#   ```markdown
#   ---
#   date: ...
#   ```
# Obsidian can't parse frontmatter inside backticks, so we strip the fence.
_FENCE_RE = re.compile(
    r'^```(?:markdown|yaml|md)?\s*\n(.*?)\n?```\s*$',
    re.DOTALL,
)


def _strip_note_fence(text: str) -> str:
    """Remove an outer ``` code fence from a note response, if present."""
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


# ── Canonical tag store ───────────────────────────────────────────────────────

_TAGS_FILE = Path.home() / ".email_to_motion" / "known_tags.json"


def _load_known_tags() -> list[str]:
    """Return the sorted list of tags used in previous notes."""
    try:
        if _TAGS_FILE.exists():
            return json.loads(_TAGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_known_tags(new_tags: list[str]):
    """Merge new_tags into the canonical store and persist it."""
    known = set(_load_known_tags())
    known.update(t.strip().lower() for t in new_tags if t.strip())
    try:
        _TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TAGS_FILE.write_text(
            json.dumps(sorted(known), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass  # non-fatal


def _extract_tags_from_markdown(markdown: str) -> list[str]:
    """
    Parse the YAML frontmatter of a generated note and return the tags list.
    Handles both inline (tags: [a, b]) and block (tags:\\n  - a\\n  - b) forms.
    """
    # Grab the frontmatter block
    fm_match = re.match(r'^---\s*\n(.*?)\n---', markdown, re.DOTALL)
    if not fm_match:
        return []
    frontmatter = fm_match.group(1)

    # Inline list: tags: [lab-safety, policy]
    inline = re.search(r'^tags:\s*\[([^\]]*)\]', frontmatter, re.MULTILINE)
    if inline:
        return [t.strip().strip('"\'') for t in inline.group(1).split(',') if t.strip()]

    # Block list:
    #   tags:
    #     - lab-safety
    #     - policy
    block = re.search(r'^tags:\s*\n((?:\s+-\s+\S+\n?)+)', frontmatter, re.MULTILINE)
    if block:
        return re.findall(r'-\s+(\S+)', block.group(1))

    return []


# ── Claude prompt ─────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a knowledge-management assistant. "
    "Your job is to convert forwarded email content into a clean, structured Markdown note "
    "suitable for a personal knowledge base (Obsidian). "
    "Extract the core information — do not pad, repeat, or editorialize. "
    "Output ONLY the Markdown note — no commentary before or after it."
)

_TEMPLATE = """\
Convert the following email (and any attachments) into an Obsidian Markdown note.

INSTRUCTIONS:
- Start with YAML frontmatter containing exactly these keys:
    date:        (the date provided below, verbatim)
    from:        (the sender provided below, verbatim)
    subject:     (the subject provided below, verbatim)
    tags:        (a YAML list — see tagging rules below)
    attachments: (a YAML list of attachment filenames, or [] if none)
- Then a blank line, then the body in this order:

    ## Summary
    2–4 sentence overview of what this email is about and why it matters.

    ## Key Points
    Bullet list of the most important facts, decisions, or actions from the
    email body itself (not the attachments).
    Omit this section if the email body contains no substantive content beyond
    a forwarding note.

    ## [Attachment Name]  ← one section per attachment, titled with the file name
    Reproduce the meaningful substance of the attachment in full:
      - Specific steps, rules, requirements, numbers, names, deadlines.
      - Use nested bullets to mirror the document's own structure.
      - Do NOT just list topics or write a one-line summary.
      - The goal is for this section to be a complete reference so the reader
        never needs to open the original file.
    Repeat this pattern for each attachment present.

    ## Notes
    Any additional context, caveats, or follow-up items not captured above.
    Omit entirely if nothing remains.

- Do not invent information not present in the source material.

TAGGING RULES:
{tag_instructions}

EMAIL METADATA:
  Date:        {date}
  From:        {sender}
  Subject:     {subject}
  Attachments: {attachment_names}

EMAIL BODY:
{body}

ATTACHMENT CONTENT (if any):
{attachment_content}
"""

_TAG_INSTRUCTIONS_COLD = """\
- Choose 2–4 concise, lowercase, hyphenated tags (e.g. lab-safety, budget, conference).
- Tags should be nouns or noun-phrases, not verbs or adjectives."""

_TAG_INSTRUCTIONS_WARM = """\
- You have an existing tag vocabulary — prefer these tags where they fit:
    {known}
- Use 2–4 tags total. Reuse existing tags whenever they apply.
- Only introduce a new tag if none of the existing ones are a reasonable fit.
- New tags must be lowercase and hyphenated (e.g. lab-safety, budget, conference)."""


# ── Note generation ───────────────────────────────────────────────────────────

def generate_note(
    subject: str,
    sender: str,
    date: str,
    body: str,
    attachment_names: list[str],
    attachment_texts: dict[str, str],   # filename → extracted text
) -> str:
    """
    Ask Claude to produce a structured Markdown note from email content.
    Loads the known-tag vocabulary first so Claude reuses existing tags.
    Returns the raw markdown string (including YAML frontmatter).

    Each attachment's text is capped at 3 000 characters so the prompt
    stays well within token limits even with large documents.
    """
    # Each attachment gets up to 8 000 chars — enough for a detailed multi-page
    # document while staying well within the model's context window.
    att_content_parts = []
    for fname, text in attachment_texts.items():
        cap = 8000
        snippet = text[:cap]
        if len(text) > cap:
            snippet += f"\n… (truncated — {len(text) - cap} chars omitted)"
        att_content_parts.append(f"--- {fname} ---\n{snippet}")

    known_tags = _load_known_tags()
    if known_tags:
        tag_instructions = _TAG_INSTRUCTIONS_WARM.format(known=", ".join(known_tags))
    else:
        tag_instructions = _TAG_INSTRUCTIONS_COLD

    prompt = _TEMPLATE.format(
        tag_instructions=tag_instructions,
        date=date,
        sender=sender,
        subject=subject,
        attachment_names=", ".join(attachment_names) if attachment_names else "none",
        body=body[:4000],
        attachment_content="\n\n".join(att_content_parts) or "(none)",
    )

    # max_tokens is sized to fit a detailed note with fully-expanded attachment
    # sections (a few pages of source material → ~600–800 words of note).
    # call_with_retries wraps the API call so transient errors (rate limits,
    # timeouts, 5xx) are automatically retried with exponential back-off.
    response = call_with_retries(
        config.claude.messages.create,
        model="claude-sonnet-4-5-20250929",
        max_tokens=3000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    markdown = _strip_note_fence(response.content[0].text)

    # Update the canonical tag store with any tags used in this note
    _save_known_tags(_extract_tags_from_markdown(markdown))

    return markdown


# ── File writing ──────────────────────────────────────────────────────────────

def _safe_subject_slug(subject: str) -> str:
    """Return the filesystem-safe version of a subject (without date prefix or extension)."""
    safe = re.sub(r'[\\/:*?"<>|#%{}]', '', subject)
    safe = re.sub(r'\s+', ' ', safe).strip()
    return safe[:60]


def _safe_filename(subject: str, date_str: str) -> str:
    """Build a filesystem-safe .md filename from the subject and date."""
    return f"{date_str} - {_safe_subject_slug(subject)}.md"


# ── Content-based deduplication ───────────────────────────────────────────────

def compute_source_hash(
    subject: str,
    sender: str,
    body: str,
    attachment_names: list[str],
) -> str:
    """
    Return a stable SHA-256 fingerprint of the email's source content.

    The hash is derived from the email's subject, sender, body text, and the
    sorted list of attachment filenames.  Timestamps (e.g. Slack ts) are
    intentionally excluded so that re-forwarding the same email produces the
    same hash regardless of when it was processed.
    """
    canonical = "|".join([subject, sender, body, "|".join(sorted(attachment_names))])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _inject_source_hash(markdown: str, source_hash: str) -> str:
    """Insert a ``source_hash:`` field into the YAML frontmatter."""
    # Match the opening --- ... closing --- block
    m = re.match(r'^(---\s*\n.*?)(\n---)', markdown, re.DOTALL)
    if m:
        return m.group(1) + f"\nsource_hash: {source_hash}" + m.group(2) + markdown[m.end():]
    # No frontmatter detected — return unchanged
    return markdown


def _extract_source_hash_from_file(path: Path) -> str | None:
    """Read an existing note file and return its ``source_hash:`` frontmatter field."""
    try:
        text = path.read_text(encoding="utf-8")
        fm = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
        if fm:
            hit = re.search(r'^source_hash:\s*(\S+)', fm.group(1), re.MULTILINE)
            if hit:
                return hit.group(1).strip()
    except Exception:
        pass
    return None


def write_note(
    markdown: str,
    subject: str,
    output_dir: Path,
    source_hash: str | None = None,
) -> tuple[Path, str]:
    """
    Write the markdown string to a .md file in output_dir.

    Deduplication behaviour (when ``source_hash`` is supplied):
    - Scans output_dir for any existing note whose ``source_hash:`` frontmatter
      field matches the supplied hash (i.e. the same email was processed before).
    - If a matching note is found and its content is identical to the new
      markdown, the file is **not** rewritten and status ``"unchanged"`` is
      returned.
    - If a matching note is found but the content differs (e.g. the note
      generator was updated), the existing file is overwritten in place and
      status ``"updated"`` is returned.
    - If no matching note is found the note is treated as a new email: a fresh
      file is created (with a ``(2)`` counter if the slug already exists) and
      status ``"saved"`` is returned.

    When ``source_hash`` is ``None`` the legacy behaviour is preserved: a new
    file is always created, with a counter suffix if the name already exists.

    Creates output_dir if it doesn't exist.
    Returns ``(path, status)`` where status is ``"saved"``, ``"updated"``, or
    ``"unchanged"``.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Embed the hash into the frontmatter so future runs can read it back.
    markdown_to_write = (
        _inject_source_hash(markdown, source_hash) if source_hash else markdown
    )

    # ── Dedup: look for a previously generated note from the same email ───────
    if source_hash:
        slug        = _safe_subject_slug(subject)
        escaped     = _glob_module.escape(slug)
        pattern     = str(output_dir / f"????-??-?? - {escaped}*.md")
        existing    = sorted(Path(p) for p in _glob_module.glob(pattern))

        for path in existing:
            stored_hash = _extract_source_hash_from_file(path)
            if stored_hash != source_hash:
                continue
            # Same email source: decide whether content changed.
            current_content = path.read_text(encoding="utf-8")
            if current_content == markdown_to_write:
                return path, "unchanged"
            path.write_text(markdown_to_write, encoding="utf-8")
            return path, "updated"

    # ── New note: pick a fresh filename (with counter if the slug collides) ───
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = _safe_filename(subject, date_str)
    path     = output_dir / filename

    counter = 1
    while path.exists():
        counter += 1
        filename = _safe_filename(f"{subject} ({counter})", date_str)
        path     = output_dir / filename

    path.write_text(markdown_to_write, encoding="utf-8")
    return path, "saved"
