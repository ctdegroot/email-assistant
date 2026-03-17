"""
ref_letter_handler.py — AI-assisted reference letter generator.

Workflow:
  1. User uploads a YAML context file to #ref-letters (optionally with a CV attached).
  2. Bot parses the YAML, extracts CV text if present.
  3. Claude writes the letter body as plain prose paragraphs.
  4. Python renders the body into the WesternLetter LaTeX template.
  5. pdflatex compiles the .tex to a PDF.
  6. Bot posts a .zip containing the .tex and .pdf back to the channel.

YAML schema expected (all fields except candidate.name are optional):

  candidate:
    name: Jane Smith
    role: MASc student
    degree_completed: "MASc, Mechanical Engineering, Western University, 2024"
    target_program: PhD in Mechanical Engineering
    target_institution: MIT
    relationship: Supervised Jane's MASc thesis at Western (2022–2024)
    duration_known: 2 years
    context: >
      Jane completed her MASc under my supervision ...

  letter:
    type: phd-application    # phd-application | postdoc | faculty | fellowship | employment | award
    deadline: 2025-03-01     # optional — registers a watch_date reminder
    addressee: Graduate Admissions Committee

  strengths:
    - label: Research Independence
      detail: >  ...

  specific_examples:
    - > ...

  areas_to_emphasize:
    - Readiness for PhD-level independent research

  tone: null   # optional — e.g. "particularly warm; strongest student I have supervised"

  weaknesses: null   # optional — e.g. "Took time to build confidence presenting, but noticeably improved"

Template support files (place in REF_LETTER_TEMPLATES_DIR):
  - WesternLetter.cls
  - Signature.pdf
  - Engineer_Stacked_PurpleGrey.png   ← must be added manually; not bundled here
"""

import io
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import requests
import yaml

from . import config
from .slack_notes_handler import _download_slack_file, _extract_pdf, _extract_docx
from .slack_helpers import get_channel_id, mark_processed
from .utils import call_with_retries

log = logging.getLogger(__name__)

# ── Channel ID resolution ─────────────────────────────────────────────────────

_channel_id: str = ""


def init(channel_name: str):
    """
    Resolve SLACK_REFLETTER_CHANNEL to its Slack channel ID and cache it.
    Called once from socket_listener.start().  Does nothing if channel_name is empty.
    """
    global _channel_id
    if not channel_name:
        return
    try:
        _channel_id = get_channel_id(channel_name.lstrip("#"))
    except ValueError as e:
        print(f"⚠️  {e} — ref-letter channel disabled.")
    except Exception as e:
        print(f"⚠️  Could not resolve ref-letter channel '{channel_name}': {e}")


# ── LaTeX helpers ─────────────────────────────────────────────────────────────

# Single-pass regex replacement prevents double-escaping (e.g. \textbackslash{} having
# its own braces re-escaped if we did sequential str.replace calls).
_LATEX_ESCAPE_MAP: dict[str, str] = {
    "\\":  r"\textbackslash{}",
    "&":   r"\&",
    "%":   r"\%",
    "$":   r"\$",
    "#":   r"\#",
    "_":   r"\_",
    "{":   r"\{",
    "}":   r"\}",
    "~":   r"\textasciitilde{}",
    "^":   r"\textasciicircum{}",
}
_LATEX_ESCAPE_RE = re.compile(
    "(" + "|".join(re.escape(c) for c in _LATEX_ESCAPE_MAP) + ")"
)


def _latex_escape(text: str) -> str:
    """Escape plain-text content for safe inclusion in a LaTeX document."""
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_ESCAPE_MAP[m.group()], text)


# ── Letter RE line and salutation ─────────────────────────────────────────────

_TYPE_LABELS = {
    "phd-application":  "PhD Application",
    "postdoc":          "Postdoctoral Application",
    "faculty":          "Faculty Application",
    "fellowship":       "Fellowship Application",
    "employment":       "Employment Application",
    "award":            "Award Nomination",
}


def _re_line(data: dict) -> str:
    name        = data.get("candidate", {}).get("name", "Applicant")
    letter_type = data.get("letter", {}).get("type", "")
    type_label  = _TYPE_LABELS.get(letter_type, "Application")
    return _latex_escape(f"Letter of Support for {name} — {type_label}")


def _salutation(data: dict) -> str:
    addressee = (data.get("letter") or {}).get("addressee") or "the Admissions Committee"
    # If the addressee looks like a named person, use "Dear X,"
    # Otherwise use "To the X,"
    if re.search(r'\b(Prof|Dr|Mr|Ms|Mrs|Professor|Doctor)\.?\b', addressee, re.IGNORECASE):
        return _latex_escape(f"Dear {addressee},")
    return _latex_escape(f"To the {addressee},")


# ── Claude prompt ─────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are writing a reference letter on behalf of Christopher DeGroot, "
    "Assistant Professor, Department of Mechanical and Materials Engineering, "
    "Western University. "
    "Write in first person, using a professional academic tone. "
    "Avoid generic superlatives — every positive claim must be grounded in a "
    "specific observation, example, or outcome. "
    "Return ONLY the body paragraphs of the letter (no salutation, no date, "
    "no 'Sincerely', no LaTeX markup, no markdown). "
    "Separate paragraphs with a single blank line."
)


def _build_prompt(data: dict, cv_text: str) -> str:
    cand   = data.get("candidate") or {}
    letter = data.get("letter")    or {}

    lines = ["Generate the body of a reference letter with the following context.\n"]

    lines.append("CANDIDATE:")
    lines.append(f"  Name:               {cand.get('name', 'N/A')}")
    if cand.get("role"):
        lines.append(f"  Role:               {cand['role']}")
    if cand.get("degree_completed"):
        lines.append(f"  Degree completed:   {cand['degree_completed']}")
    if cand.get("target_program"):
        lines.append(f"  Applying to:        {cand['target_program']}")
    if cand.get("target_institution"):
        lines.append(f"  Target institution: {cand['target_institution']}")
    if cand.get("relationship"):
        lines.append(f"  Relationship:       {cand['relationship']}")
    if cand.get("duration_known"):
        lines.append(f"  Duration known:     {cand['duration_known']}")
    if cand.get("context"):
        lines.append(f"  Context:\n    {cand['context'].strip()}")

    lines.append(f"\nLETTER TYPE: {_TYPE_LABELS.get(letter.get('type', ''), 'Application')}")
    if letter.get("addressee"):
        lines.append(f"ADDRESSEE: {letter['addressee']}")

    strengths = data.get("strengths") or []
    if strengths:
        lines.append("\nSTRENGTHS:")
        for s in strengths:
            lines.append(f"  • {s.get('label', '')}: {s.get('detail', '').strip()}")

    examples = data.get("specific_examples") or []
    if examples:
        lines.append("\nSPECIFIC EXAMPLES TO WEAVE IN:")
        for ex in examples:
            lines.append(f"  • {str(ex).strip()}")

    emphasize = data.get("areas_to_emphasize") or []
    if emphasize:
        lines.append("\nAREAS TO EMPHASIZE:")
        for a in emphasize:
            lines.append(f"  • {a}")

    tone = data.get("tone")
    if tone:
        lines.append(f"\nTONE GUIDANCE: {tone}")

    weakness = data.get("weaknesses")
    if weakness:
        lines.append(f"\nWEAKNESS TO ADDRESS (briefly and positively): {weakness}")

    if cv_text:
        lines.append(f"\nCANDIDATE CV / RÉSUMÉ (use for additional supporting detail):\n{cv_text[:4000]}")

    lines.append(
        "\nWrite 3–5 substantive paragraphs that: "
        "(1) open by establishing your relationship and overall endorsement, "
        "(2) cover each strength with grounded specific evidence, "
        "(3) weave in the specific examples naturally, "
        "(4) close with a concrete endorsement suited to the letter type."
    )

    return "\n".join(lines)


def _generate_body(data: dict, cv_text: str) -> str:
    """Call Claude to write the letter body. Returns plain prose paragraphs."""
    prompt   = _build_prompt(data, cv_text)
    response = call_with_retries(
        config.claude.messages.create,
        model="claude-sonnet-4-5-20250929",
        max_tokens=2000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ── LaTeX rendering ───────────────────────────────────────────────────────────

# Blank-line-separated plain-text paragraphs → LaTeX paragraph breaks.
# The WesternLetter cls sets \parskip=1em and \parindent=0pt, so a blank line
# between paragraphs produces the correct visual spacing.
def _body_to_latex(body: str) -> str:
    """Convert plain-text paragraphs to LaTeX, escaping special characters."""
    paragraphs = re.split(r'\n{2,}', body.strip())
    return "\n\n".join(_latex_escape(p.strip()) for p in paragraphs if p.strip())


_TEX_TEMPLATE = r"""% Auto-generated by EmailToMotion ref_letter_handler
% Requires: WesternLetter.cls, Signature.pdf, Engineer_Stacked_PurpleGrey.png
\documentclass{WesternLetter}
\begin{document}

\pagestyle{fancy}
\thispagestyle{firstpage}

\vspace*{2cm}

\today

RE: <<RE_LINE>>

\bigskip

<<SALUTATION>>

\bigskip

<<BODY>>

\closingwithsignature

\end{document}
"""


def _render_tex(data: dict, body: str) -> str:
    """Render the LaTeX template, injecting the generated body and metadata."""
    return (
        _TEX_TEMPLATE
        .replace("<<RE_LINE>>",   _re_line(data))
        .replace("<<SALUTATION>>", _salutation(data))
        .replace("<<BODY>>",       _body_to_latex(body))
    )


# ── PDF compilation ───────────────────────────────────────────────────────────

def _check_template_files() -> list[str]:
    """
    Return a list of missing required template files.
    Engineer_Stacked_PurpleGrey.png is checked but its absence produces only a
    warning rather than a hard error (compilation may still succeed with a
    missing header image).
    """
    templates_dir = Path(config.REF_LETTER_TEMPLATES_DIR)
    required = ["WesternLetter.cls", "Signature.pdf"]
    missing  = [f for f in required if not (templates_dir / f).exists()]
    return missing


def _compile_pdf(tex_content: str, stem: str) -> tuple[Path | None, str]:
    """
    Write tex_content to a temp directory alongside the template support files,
    run pdflatex twice (for correct page references), and return
    (pdf_path, log_excerpt).

    Returns (None, error_message) if compilation fails or pdflatex is unavailable.
    """
    if not shutil.which("pdflatex"):
        return None, "pdflatex not found — install texlive-latex-base."

    templates_dir = Path(config.REF_LETTER_TEMPLATES_DIR)

    with tempfile.TemporaryDirectory() as tmpdir:
        work = Path(tmpdir)

        # Copy all support files into the work directory
        for src in templates_dir.iterdir():
            shutil.copy(src, work / src.name)

        tex_path = work / f"{stem}.tex"
        tex_path.write_text(tex_content, encoding="utf-8")

        # Run pdflatex twice (second pass resolves forward references)
        log_excerpt = ""
        for _ in range(2):
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=60,
            )
            log_excerpt = result.stdout[-2000:] if result.stdout else result.stderr[-2000:]

        pdf_src = work / f"{stem}.pdf"
        if not pdf_src.exists():
            return None, f"pdflatex failed:\n{log_excerpt}"

        # Copy PDF to a persistent output directory
        out_dir = Path(config.REF_LETTERS_OUTPUT_PATH).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf_dst = out_dir / f"{stem}.pdf"
        shutil.copy(pdf_src, pdf_dst)
        return pdf_dst, ""


# ── Zip packaging ─────────────────────────────────────────────────────────────

def _build_zip(
    stem: str,
    tex_content: str,
    pdf_path: Path | None,
    source_yaml: bytes | None = None,
) -> Path:
    """
    Create a zip file containing the .tex, (if available) .pdf, and the
    source YAML context file used to generate the letter.
    Returns the path to the zip file.
    """
    out_dir = Path(config.REF_LETTERS_OUTPUT_PATH).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tex_path = out_dir / f"{stem}.tex"
    tex_path.write_text(tex_content, encoding="utf-8")

    zip_path = out_dir / f"{stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tex_path, arcname=f"{stem}.tex")
        if pdf_path and pdf_path.exists():
            zf.write(pdf_path, arcname=f"{stem}.pdf")
        if source_yaml:
            zf.writestr(f"{stem}_context.yaml", source_yaml)

    return zip_path


# ── Filename slug ─────────────────────────────────────────────────────────────

def _make_stem(data: dict) -> str:
    """Build a filesystem-safe filename stem: YYYY-MM-DD_firstname_lastname_ref_letter."""
    name = (data.get("candidate") or {}).get("name", "unknown")
    slug = re.sub(r'[^\w\s-]', '', name).strip().lower()
    slug = re.sub(r'[\s]+', '_', slug)
    date_prefix = datetime.now().strftime("%Y-%m-%d")
    return f"{date_prefix}_{slug}_ref_letter"


# ── CV extraction ─────────────────────────────────────────────────────────────

_BY_MIME     = {
    "application/pdf": _extract_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _extract_docx,
}
_BY_FILETYPE = {"pdf": _extract_pdf, "docx": _extract_docx}


def _extract_cv(files: list[dict], yaml_file: dict) -> str:
    """Download and extract text from the first non-YAML attachment."""
    for f in files:
        if f is yaml_file:
            continue
        mime     = f.get("mimetype", "")
        filetype = (f.get("filetype") or "").lower()
        dl_url   = f.get("url_private_download") or f.get("url_private") or ""
        extractor = _BY_MIME.get(mime) or _BY_FILETYPE.get(filetype)
        if extractor and dl_url:
            try:
                raw = _download_slack_file(dl_url)
                return extractor(raw)
            except Exception as e:
                log.warning("ref_letter: CV extraction failed: %s", e)
    return ""


# ── Deadline watch_date registration ─────────────────────────────────────────

def _register_deadline(data: dict, note_path: Path | None):
    """
    If letter.deadline is set and a note_path is provided, add a watch_date
    entry to the corresponding note so the scheduler surfaces a reminder.

    This is best-effort — failures are logged but do not affect the letter output.
    """
    if not note_path:
        return
    deadline = (data.get("letter") or {}).get("deadline")
    if not deadline:
        return
    name = (data.get("candidate") or {}).get("name", "Applicant")
    try:
        import yaml as _yaml
        from .watch_date_handler import _read_note_frontmatter, _write_note_frontmatter

        fm, full_text = _read_note_frontmatter(note_path)
        watch_dates   = fm.get("watch_dates") or []
        label         = f"Ref letter deadline — {name}"
        # Avoid duplicates
        if not any(e.get("label") == label for e in watch_dates if isinstance(e, dict)):
            watch_dates.append({
                "label":        label,
                "date":         str(deadline),
                "status":       "active",
                "snooze_until": None,
                "last_reminded": None,
            })
            fm["watch_dates"] = watch_dates
            _write_note_frontmatter(note_path, fm, full_text)
    except Exception as e:
        log.warning("ref_letter: could not register deadline watch_date: %s", e)


# ── Template delivery ─────────────────────────────────────────────────────────

def send_template(channel_id: str):
    """Upload the blank YAML template to *channel_id* as a Slack file.

    Uses ``content=`` (file bytes sent inline) rather than ``file=`` (local
    path) so this works correctly on remote servers where the Slack API cannot
    reach the server's filesystem.  Falls back to a plain text message if the
    template file itself is missing.
    """
    template_path = Path(config.REF_LETTER_TEMPLATES_DIR) / "template.yaml"
    try:
        template_content = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("ref_letter: cannot read template file %s: %s", template_path, exc)
        config.slack.chat_postMessage(
            channel=channel_id,
            text=(
                "⚠️ Template file not found on the server. "
                "Please ask the administrator to check "
                f"`{config.REF_LETTER_TEMPLATES_DIR}`."
            ),
        )
        return

    config.slack.files_upload_v2(
        channel=channel_id,
        content=template_content,
        filename="ref_letter_template.yaml",
        title="Reference Letter YAML Template",
        initial_comment=(
            "📄 Fill in this template and upload it back to this channel "
            "(optionally attach a CV PDF or Word doc alongside it)."
        ),
    )


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_message(event: dict):
    """
    Process one message from the #ref-letters channel.
    Expects at least one .yaml / .yml file attachment; optionally a CV (PDF/Word).
    Runs in a background thread — never called directly from _dispatch.
    """
    if event.get("bot_id") == config.OWN_BOT_ID:
        return

    subtype = event.get("subtype", "")
    if subtype in ("message_changed", "message_deleted", "channel_join", "channel_leave"):
        return

    channel_id = event.get("channel", "")
    ts         = event.get("ts", "")
    files      = event.get("files") or []

    # ── Locate the YAML context file ─────────────────────────────────────────
    yaml_file = next(
        (
            f for f in files
            if (f.get("filetype") or "").lower() in ("yaml", "text")
            or (f.get("name") or "").lower().endswith((".yaml", ".yml"))
        ),
        None,
    )

    if not yaml_file:
        send_template(channel_id)
        return

    # ── Download and parse YAML ───────────────────────────────────────────────
    try:
        dl_url   = yaml_file.get("url_private_download") or yaml_file.get("url_private") or ""
        raw_yaml = _download_slack_file(dl_url)
        data     = yaml.safe_load(raw_yaml)
        if not isinstance(data, dict):
            raise ValueError("YAML did not parse to a mapping")
        candidate_name = (data.get("candidate") or {}).get("name")
        if not candidate_name:
            raise ValueError("candidate.name is required")
    except Exception as e:
        config.slack.chat_postMessage(
            channel=channel_id,
            text=f"⚠️ Could not parse YAML context file: {e}",
        )
        return

    # ── Post a "working on it" message ────────────────────────────────────────
    config.slack.chat_postMessage(
        channel=channel_id,
        text=f"✍️ Generating reference letter for *{candidate_name}*…",
    )

    # ── Check template files ──────────────────────────────────────────────────
    missing = _check_template_files()
    if missing:
        config.slack.chat_postMessage(
            channel=channel_id,
            text=(
                f"⚠️ Missing template file(s): {', '.join(missing)}\n"
                f"Add them to `{config.REF_LETTER_TEMPLATES_DIR}` and try again."
            ),
        )
        return

    # ── Extract CV (optional) ─────────────────────────────────────────────────
    cv_text = _extract_cv(files, yaml_file)

    # ── Generate letter body via Claude ───────────────────────────────────────
    try:
        body = _generate_body(data, cv_text)
    except Exception as e:
        log.error("ref_letter: Claude generation failed: %s", e)
        config.slack.chat_postMessage(
            channel=channel_id,
            text=f"⚠️ Letter generation failed: {e}",
        )
        return

    # ── Render .tex ───────────────────────────────────────────────────────────
    tex_content = _render_tex(data, body)
    stem        = _make_stem(data)

    # ── Compile PDF ───────────────────────────────────────────────────────────
    pdf_path, pdf_error = _compile_pdf(tex_content, stem)

    # ── Package zip ───────────────────────────────────────────────────────────
    zip_path = _build_zip(stem, tex_content, pdf_path, source_yaml=raw_yaml)

    # ── Post result to Slack ──────────────────────────────────────────────────
    status_parts = ["✅ *Reference letter ready*"]
    if pdf_path:
        status_parts.append("Includes `.tex` and compiled `.pdf`.")
    else:
        status_parts.append(f"`.tex` only — PDF compilation failed: _{pdf_error}_")
        if "Engineer_Stacked_PurpleGrey.png" in pdf_error or "not found" in pdf_error.lower():
            status_parts.append(
                "Tip: ensure `Engineer_Stacked_PurpleGrey.png` is in "
                f"`{config.REF_LETTER_TEMPLATES_DIR}`."
            )

    try:
        config.slack.files_upload_v2(
            channel=channel_id,
            file=str(zip_path),
            filename=zip_path.name,
            title=f"Reference Letter — {candidate_name}",
            initial_comment="\n".join(status_parts),
        )
    except Exception as e:
        # Fallback: post the tex content inline if file upload fails
        log.error("ref_letter: file upload failed: %s", e)
        config.slack.chat_postMessage(
            channel=channel_id,
            text=(
                f"⚠️ Could not upload zip ({e}). "
                f"Files saved to `{config.REF_LETTERS_OUTPUT_PATH}`."
            ),
        )

    # Mark the original message as processed
    if ts:
        mark_processed(channel_id, ts)
