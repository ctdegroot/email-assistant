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

import json
import re
from datetime import datetime
from pathlib import Path

from . import config


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
    response = config.claude.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=3000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    markdown = response.content[0].text.strip()

    # Update the canonical tag store with any tags used in this note
    _save_known_tags(_extract_tags_from_markdown(markdown))

    return markdown


# ── File writing ──────────────────────────────────────────────────────────────

def _safe_filename(subject: str, date_str: str) -> str:
    """Build a filesystem-safe .md filename from the subject and date."""
    safe = re.sub(r'[\\/:*?"<>|#%{}]', '', subject)
    safe = re.sub(r'\s+', ' ', safe).strip()
    safe = safe[:60]
    return f"{date_str} - {safe}.md"


def write_note(markdown: str, subject: str, output_dir: Path) -> Path:
    """
    Write the markdown string to a .md file in output_dir.

    Creates output_dir if it doesn't exist.
    If a file with the same name already exists (e.g. two emails with the same
    subject on the same day), appends a counter: "2026-03-10 - Title (2).md".

    Returns the Path of the file that was written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = _safe_filename(subject, date_str)
    path = output_dir / filename

    counter = 1
    while path.exists():
        counter += 1
        filename = _safe_filename(f"{subject} ({counter})", date_str)
        path = output_dir / filename

    path.write_text(markdown, encoding="utf-8")
    return path
