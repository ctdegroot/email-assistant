"""
pdf_sign_handler.py — Automatic PDF sign-and-date pipeline.

Two modes, selected automatically based on the uploaded PDF:

AcroForm PDFs (fillable forms):
  Reads embedded field names and fills:
    • Signature fields  → Signature.pdf image stamped at the field's bounding box
    • Date fields       → today's date
    • Name fields       → SIGNER_NAME
    • Title fields      → SIGNER_TITLE
    • Department fields → SIGNER_DEPARTMENT
    • Institution fields→ SIGNER_INSTITUTION
    • Email fields      → SIGNER_EMAIL

Flat PDFs (no embedded form fields):
  Uses a two-step hybrid approach:
    1. Geometry scan — PyMuPDF's get_drawings() locates all candidate signing
       areas (horizontal underlines and hollow rectangular boxes) in the lower
       half of the page.  Table/form borders are excluded structurally.
    2. Claude classifier — a rendered crop of the page plus a numbered list of
       candidates is sent to Claude, which identifies (by index) which candidate
       is the primary signer's signature area and which is the date field.
       Claude reasons about semantics; all coordinates come from geometry.
    3. Pure-vision fallback — if geometry finds no candidates, Claude estimates
       pixel coordinates directly from a rendered crop (less reliable).

  Only signature and date are applied — no attempt is made to locate other text
  fields, as their layouts vary too much to detect reliably.

Dependencies:
  PyMuPDF (fitz)  — pip install pymupdf
  pypdf           — pip install pypdf

Configuration (all optional — leave blank to skip that fill):
  SIGNER_NAME        — e.g. "Dr. Jane Smith"
  SIGNER_TITLE       — e.g. "Associate Professor"
  SIGNER_DEPARTMENT  — e.g. "Department of Mechanical and Materials Engineering"
  SIGNER_INSTITUTION — e.g. "Western University"
  SIGNER_EMAIL       — e.g. "jsmith@university.edu"
  REF_LETTER_TEMPLATES_DIR — directory containing Signature.pdf
  SLACK_SIGN_CHANNEL — Slack channel name (without #)
"""

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import requests

from . import activity_log
from . import config
from .slack_helpers import get_channel_id, mark_processed
from .utils import call_with_retries

log = logging.getLogger(__name__)

# ── Channel ID resolution ─────────────────────────────────────────────────────

_channel_id: str = ""


def init(channel_name: str):
    """
    Resolve SLACK_SIGN_CHANNEL to its Slack channel ID and cache it.
    Called once from socket_listener.start().  Does nothing if channel_name is empty.
    """
    global _channel_id
    if not channel_name:
        return
    try:
        _channel_id = get_channel_id(channel_name.lstrip("#"))
    except ValueError as exc:
        log.warning("pdf_sign: %s — channel disabled.", exc)
    except Exception as exc:
        log.warning("pdf_sign: could not resolve channel '%s': %s", channel_name, exc)


# ── Field-name heuristics (AcroForm mode) ─────────────────────────────────────

_DATE_PATTERNS    = frozenset({
    "date", "dated", "datesigned", "signaturedate", "sigdate", "dateofissue",
    "issuedate", "dateofexecution", "executiondate", "datecompleted",
})
_SIG_PATTERNS     = frozenset({
    "signature", "sign", "sig", "esignature", "esig", "autograph",
    "signhere", "authorisedsignature", "authorizedsignature",
})
_NAME_PATTERNS    = frozenset({
    "name", "fullname", "printname", "printedname", "signername",
    "authorizedby", "authorisedby", "authorizedname", "signatoryname",
})
_TITLE_PATTERNS   = frozenset({
    "title", "jobtitle", "position", "rank", "designation",
})
_DEPT_PATTERNS    = frozenset({
    "department", "dept", "division", "faculty", "school", "unit",
})
_INST_PATTERNS    = frozenset({
    "institution", "organization", "organisation", "employer",
    "affiliation", "university", "college", "company", "firm",
})
_EMAIL_PATTERNS   = frozenset({
    "email", "emailaddress", "emailaddr", "contactemail",
})


def _normalise(field_name: str) -> str:
    """Lower-case and strip all non-alphanumeric characters for fuzzy matching."""
    return re.sub(r'[^a-z0-9]', '', field_name.lower())


def _field_category(raw_name: str) -> str | None:
    """
    Map a raw AcroForm field name to one of:
    'date', 'signature', 'name', 'title', 'department', 'institution', 'email'.
    Returns None if the field doesn't match any known category.
    """
    norm = _normalise(raw_name)
    for patterns, category in [
        (_SIG_PATTERNS,   "signature"),
        (_DATE_PATTERNS,  "date"),
        (_NAME_PATTERNS,  "name"),
        (_TITLE_PATTERNS, "title"),
        (_DEPT_PATTERNS,  "department"),
        (_INST_PATTERNS,  "institution"),
        (_EMAIL_PATTERNS, "email"),
    ]:
        if norm in patterns or any(p in norm for p in patterns):
            return category
    return None


# ── Signature pixmap ───────────────────────────────────────────────────────────

def _load_signature_pixmap():  # -> fitz.Pixmap | None
    """
    Render the first page of Signature.pdf to a high-res fitz.Pixmap (3×).
    Returns None if the file is missing or fitz is unavailable.
    """
    try:
        import fitz
    except ImportError:
        log.error("pdf_sign: PyMuPDF not installed — run: pip install pymupdf")
        return None

    sig_path = Path(config.REF_LETTER_TEMPLATES_DIR) / "Signature.pdf"
    if not sig_path.exists():
        log.warning("pdf_sign: Signature.pdf not found at %s", sig_path)
        return None

    try:
        sig_doc  = fitz.open(str(sig_path))
        pixmap   = sig_doc[0].get_pixmap(matrix=fitz.Matrix(3, 3), alpha=True)
        sig_doc.close()
        return pixmap
    except Exception as exc:
        log.error("pdf_sign: could not render Signature.pdf: %s", exc)
        return None


# ── AcroForm path ─────────────────────────────────────────────────────────────

def _has_acroform_fields(pdf_bytes: bytes) -> bool:
    """Return True if the PDF contains at least one AcroForm widget."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        has = any(list(page.widgets()) for page in doc)
        doc.close()
        return has
    except Exception:
        return False


def _sign_acroform(pdf_bytes: bytes) -> tuple[bytes | None, dict]:
    """
    Fill recognisable AcroForm fields and return (signed_bytes, summary).

    summary keys: filled, skipped, signed, date_filled, mode, error
    """
    try:
        import fitz
    except ImportError:
        return None, {"error": "PyMuPDF not installed", "mode": "acroform",
                      "filled": [], "skipped": [], "signed": False, "date_filled": False}

    today      = datetime.now().strftime("%B %-d, %Y")
    sig_pixmap = _load_signature_pixmap()

    _FILL_VALUES: dict[str, str] = {
        "date":        today,
        "name":        config.SIGNER_NAME,
        "title":       config.SIGNER_TITLE,
        "department":  config.SIGNER_DEPARTMENT,
        "institution": config.SIGNER_INSTITUTION,
        "email":       config.SIGNER_EMAIL,
    }

    filled:      list[tuple[str, str, str]] = []
    skipped:     list[str]                  = []
    signed       = False
    date_filled  = False

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page in doc:
            for widget in page.widgets():
                raw_name = widget.field_name or ""
                category = _field_category(raw_name)

                if category is None:
                    skipped.append(raw_name)
                    continue

                if category == "signature":
                    if sig_pixmap is not None:
                        page.insert_image(widget.rect, pixmap=sig_pixmap,
                                          keep_proportion=True, overlay=True)
                        signed = True
                        filled.append((raw_name, "signature", "<image>"))
                    else:
                        skipped.append(raw_name)
                    continue

                value = _FILL_VALUES.get(category, "")
                if not value:
                    skipped.append(raw_name)
                    continue

                if widget.field_type in (fitz.PDF_WIDGET_TYPE_TEXT,
                                         fitz.PDF_WIDGET_TYPE_COMBOBOX):
                    widget.field_value = value
                    widget.update()
                    filled.append((raw_name, category, value))
                    if category == "date":
                        date_filled = True
                else:
                    skipped.append(raw_name)

        signed_bytes = doc.tobytes(deflate=True)
        doc.close()

    except Exception as exc:
        log.error("pdf_sign: AcroForm error: %s", exc, exc_info=True)
        return None, {"error": str(exc), "mode": "acroform",
                      "filled": filled, "skipped": skipped,
                      "signed": signed, "date_filled": date_filled}

    return signed_bytes, {
        "mode": "acroform", "filled": filled, "skipped": skipped,
        "signed": signed, "date_filled": date_filled, "error": None,
    }


# ── Flat PDF path ─────────────────────────────────────────────────────────────
# Detection pipeline:
#   1. Geometry scan  — find ALL candidate signing areas (underlines + boxes).
#   2. Claude classify — pass candidates + rendered crop to Claude; it returns
#      indices indicating which candidate is the primary signer's sig field and
#      which is the date.  Coordinates come entirely from geometry.
#   3. Vision fallback — if geometry finds nothing, ask Claude to estimate pixel
#      coordinates directly from a rendered crop (less reliable).

# ── JSON parsing helper ────────────────────────────────────────────────────────

def _parse_json_response(raw: str) -> dict | None:
    """Extract and parse the first JSON object found in Claude's response."""
    candidates = [raw]
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if m:
        candidates.append(m.group())
    for text in candidates:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


# ── Segment merging ────────────────────────────────────────────────────────────

def _merge_segments(
    segs: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """
    Merge collinear horizontal segments at the same y (±2 pt) into logical
    lines.  Adjacent segments with an x-gap < 80 pt are joined; wider gaps
    produce separate lines.

    Input:  list of (y, x0, x1)
    Output: sorted list of (y, x0_merged, x1_merged)
    """
    by_y: dict[float, list[tuple[float, float]]] = {}
    for y, x0, x1 in segs:
        key = None
        for k in by_y:
            if abs(k - y) < 2:
                key = k
                break
        if key is None:
            key = y
            by_y[key] = []
        by_y[key].append((x0, x1))

    lines = []
    for y, xs in by_y.items():
        xs_sorted = sorted(xs)
        sub_x0, sub_x1 = xs_sorted[0]
        for x0, x1 in xs_sorted[1:]:
            if x0 - sub_x1 < 80:
                sub_x1 = max(sub_x1, x1)
            else:
                lines.append((y, sub_x0, sub_x1))
                sub_x0, sub_x1 = x0, x1
        lines.append((y, sub_x0, sub_x1))
    return sorted(lines)


# ── Geometry scanner ───────────────────────────────────────────────────────────

def _find_all_sig_candidates(page) -> tuple[list[dict], float]:
    """
    Scan the lower half of the page for drawing elements that could be
    signature or date fields.

    Detected shapes:
      'line' — thin horizontal underline (height < 4 pt, width > 40 pt).
                Represents a blank line to write above/on.
      'box'  — hollow rectangle wider than tall (width > 60 pt, 15–150 pt
                tall, no fill or white fill).  Represents a signature/date box.

    Table and form borders — those whose x0 lies at or near the page's left
    margin — are excluded.  The left margin is defined as the minimum x0
    across all candidate elements; anything within 20 pt of that minimum
    is considered a border and discarded.

    Returns:
      candidates — list sorted by (y1, x0); each dict contains:
                     type  — 'line' or 'box'
                     x0, y0, x1, y1  — bounding box in PDF points
                     line_y  — actual underline y (only for 'line' type)
      crop_y     — full-page y coordinate to start a rendering crop from
                   (≥ 50 % of page height, 30 pt above the topmost candidate)
    """
    page_h   = page.rect.height
    search_y = page_h * 0.50

    segments:  list[tuple[float, float, float]] = []
    box_cands: list[dict]                        = []

    for d in page.get_drawings():
        r = d["rect"]
        h = r.y1 - r.y0
        w = r.x1 - r.x0

        if r.y0 < search_y:
            continue

        # Thin horizontal underline
        if h < 4 and w > 40:
            segments.append((round(r.y0, 1), r.x0, r.x1))

        # Hollow rectangle plausible as a signature/date box:
        # wider than tall, at least 60 pt wide, between 15 and 150 pt tall
        elif w > 60 and 15 < h < 150 and w > h * 1.5:
            fill = d.get("fill")
            # No fill, pure white, or transparent → hollow box
            if fill is None or fill in ((1, 1, 1), (1.0, 1.0, 1.0)):
                box_cands.append({
                    "type": "box",
                    "x0": r.x0, "y0": r.y0,
                    "x1": r.x1, "y1": r.y1,
                    "width": w, "height": h,
                })

    if not segments and not box_cands:
        return [], search_y

    all_x0           = [x0 for _, x0, _ in segments] + [b["x0"] for b in box_cands]
    left_margin      = min(all_x0)
    margin_threshold = left_margin + 20

    # Discard elements starting at/near the form's left margin (table borders)
    inner_segs  = [(y, x0, x1) for y, x0, x1 in segments  if x0 > margin_threshold]
    inner_boxes = [b            for b          in box_cands if b["x0"] > margin_threshold]

    logical_lines = _merge_segments(inner_segs)

    candidates: list[dict] = []
    for y, x0, x1 in logical_lines:
        candidates.append({
            "type":   "line",
            "x0":     x0,
            "y0":     y - 60,    # 60 pt of signing space above the underline
            "x1":     x1,
            "y1":     y,
            "line_y": y,         # exact underline y (for date baseline)
        })
    candidates.extend(inner_boxes)
    candidates.sort(key=lambda c: (c["y1"], c["x0"]))

    top_y  = min(c["y0"] for c in candidates) if candidates else search_y
    crop_y = max(search_y, top_y - 30)

    log.info(
        "pdf_sign: %d candidate(s) — %s",
        len(candidates),
        [(c["type"], round(c["x0"]), round(c["y1"])) for c in candidates],
    )
    return candidates, crop_y


# ── Claude candidate classifier ────────────────────────────────────────────────

_CLASSIFY_SYSTEM = (
    "You are a form analyst. "
    "You are shown a rendered image of part of a PDF form together with a numbered "
    "list of geometric candidate fields (underlines and boxes) detected on that page. "
    "Your job is to identify which candidate is the PRIMARY signer's signature field "
    "and which is the PRIMARY signer's date field. "
    "Return only valid JSON — no prose, no markdown."
)

_CLASSIFY_PROMPT = """\
The image shows the bottom portion of a PDF form page \
({img_w}×{img_h} px, cropped from y={crop_y:.0f} pt down the full page).

Detected candidate fields (coordinates in PDF points on the full page):
{candidates_desc}

Task:
• sig_idx  — index of the field where the PRIMARY signer should write their \
signature.  Choose the one labelled "Applicant", "Grantee", "Principal Investigator", \
"Authorized Signatory", "Signature", or similar.  Do NOT choose a field for \
"Witness", "Financial Officer", "Notary", or any other secondary party.
• date_idx — index of the date field belonging to THAT SAME primary signer. \
It may be directly below the signature field or immediately to its right on the \
same row.  Use null if no date field is visible.

Return exactly:
{{
  "sig_idx":  <integer or null>,
  "date_idx": <integer or null>
}}"""


def _classify_candidates_with_claude(
    page,
    candidates: list[dict],
    crop_y: float,
) -> dict | None:
    """
    Render the relevant portion of the page and ask Claude to classify which
    candidate is the primary signer's signature field and which is the date.

    Claude receives:
      • A rendered PNG of the page from crop_y to the bottom (1 px = 1 PDF pt).
      • A textual numbered list of each candidate with its full-page and
        crop-relative coordinates and horizontal position on the page.

    Claude returns by index — it never estimates pixel coordinates, so there
    is no risk of coordinate estimation errors.

    Returns { 'sig_idx': int|None, 'date_idx': int|None } or None on failure.
    """
    import fitz

    page_h  = page.rect.height
    clip    = fitz.Rect(0, crop_y, page.rect.width, page_h)
    pix     = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0), clip=clip)
    img_b64 = base64.standard_b64encode(pix.tobytes("png")).decode()

    page_w = page.rect.width

    def _desc(idx: int, c: dict) -> str:
        cx0 = c["x0"]
        cy0 = c["y0"] - crop_y
        cx1 = c["x1"]
        cy1 = c["y1"] - crop_y
        h_pos = (
            "left"   if c["x0"] < page_w / 3  else
            "right"  if c["x0"] > 2 * page_w / 3 else
            "center"
        )
        shape = "underline" if c["type"] == "line" else "box"
        return (
            f"  [{idx}] {shape} — page: x={c['x0']:.0f}–{c['x1']:.0f}, "
            f"y={c['y0']:.0f}–{c['y1']:.0f}; "
            f"crop image: x={cx0:.0f}–{cx1:.0f}, y={max(0, cy0):.0f}–{cy1:.0f} "
            f"({h_pos} of page)"
        )

    candidates_desc = "\n".join(_desc(i, c) for i, c in enumerate(candidates))
    prompt = _CLASSIFY_PROMPT.format(
        img_w=pix.width,
        img_h=pix.height,
        crop_y=crop_y,
        candidates_desc=candidates_desc,
    )

    try:
        response = call_with_retries(
            config.claude.messages.create,
            model="claude-sonnet-4-5-20250929",
            max_tokens=128,
            system=_CLASSIFY_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": img_b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception as exc:
        log.error("pdf_sign: Claude classify failed: %s", exc)
        return None

    raw    = response.content[0].text.strip()
    result = _parse_json_response(raw)
    if result and "sig_idx" in result:
        log.info("pdf_sign: Claude classification → %s", result)
        return result
    log.warning("pdf_sign: unparseable classify response: %r", raw[:200])
    return None


def _fields_from_classification(
    candidates: list[dict],
    sig_idx: int | None,
    date_idx: int | None,
) -> dict:
    """
    Convert Claude's classification indices into a signing fields dict.

    Returns a dict with any of:
      'signature_rect' — { x0, y0, x1, y1 } in PDF points
      'date_point'     — { x, y } text baseline in PDF points
    """
    fields: dict = {}

    if sig_idx is not None and 0 <= sig_idx < len(candidates):
        c = candidates[sig_idx]
        fields["signature_rect"] = {
            "x0": c["x0"], "y0": c["y0"],
            "x1": c["x1"], "y1": c["y1"],
        }

    if date_idx is not None and 0 <= date_idx < len(candidates):
        c = candidates[date_idx]
        if c["type"] == "line":
            # Place date text just above the underline
            fields["date_point"] = {"x": c["x0"], "y": c["line_y"] - 2}
        else:
            # Place text inside the upper-left of the box
            fields["date_point"] = {"x": c["x0"] + 4, "y": c["y0"] + 14}

    return fields


# ── Pure-vision fallback (no geometry candidates found) ───────────────────────

_LOCATE_SYSTEM = (
    "You are a precise document analyser. "
    "When given a PDF form image you identify the exact pixel coordinates of "
    "specific form fields. You return only valid JSON — no prose, no markdown."
)

_LOCATE_PROMPT = """\
This image shows the BOTTOM PORTION of a PDF form page ({img_w}×{img_h} px).
It has been cropped to focus on the signature section only.

Identify ONLY these two elements for the PRIMARY signer \
(labelled "Grantee", "Applicant", "Signature", "Authorized Signatory", \
"Principal Investigator", or similar — NOT "Financial Officer", "Witness", \
"Notary", or any secondary party):

1. signature_rect — the blank area ABOVE the primary signer's signature \
line where the signature image should be stamped.
2. date_point — the bottom-left point just above the date line that \
belongs to that same primary signer.

Return a JSON object in exactly this format:
{{
  "signature_rect": {{"x0": <int>, "y0": <int>, "x1": <int>, "y1": <int>}},
  "date_point":     {{"x":  <int>, "y":  <int>}}
}}

Rules:
- All values are pixel coordinates within THIS {img_w}×{img_h} cropped image.
- signature_rect must have y1 > y0 and be at least 25px tall.
- date_point.y is the text baseline — place it so text sits just above \
  the date line, not on top of existing text.
- If you cannot confidently locate either field, return {{}}.
- Return ONLY the JSON object, no other text.
"""


def _locate_fields_with_claude(
    image_bytes: bytes,
    img_w: int,
    img_h: int,
) -> dict | None:
    """
    Last-resort fallback: ask Claude to estimate pixel coordinates for the
    primary signer's signature area and date field from a rendered crop.

    Less reliable than the geometry + classification path because Claude must
    estimate coordinates rather than pick from a known list.  Used only when
    get_drawings() returns no candidate lines or boxes.
    """
    img_b64 = base64.standard_b64encode(image_bytes).decode()
    prompt  = _LOCATE_PROMPT.format(img_w=img_w, img_h=img_h)

    try:
        response = call_with_retries(
            config.claude.messages.create,
            model="claude-sonnet-4-5-20250929",
            max_tokens=256,
            system=_LOCATE_SYSTEM,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": img_b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
    except Exception as exc:
        log.error("pdf_sign: Claude vision fallback failed: %s", exc)
        return None

    raw    = response.content[0].text.strip()
    result = _parse_json_response(raw)
    if result:
        if "signature_rect" in result or "date_point" in result:
            return result
        if result == {}:
            return None   # Claude signalled it couldn't find the fields
    log.warning("pdf_sign: unparseable vision response: %r", raw[:200])
    return None


# ── Main flat-PDF signing function ─────────────────────────────────────────────

def _sign_flat(pdf_bytes: bytes) -> tuple[bytes | None, dict]:
    """
    Sign a flat (non-AcroForm) PDF.

    Pipeline (per page):
      1. _find_all_sig_candidates()       → geometry: lines + boxes
      2. _classify_candidates_with_claude() → Claude picks sig/date by index
      3. _fields_from_classification()    → converts indices to PDF coordinates
      4. [fallback] _locate_fields_with_claude() → direct pixel estimation

    Only signature image and today's date are applied.

    Returns (signed_bytes, summary) where summary mirrors _sign_acroform's
    format with mode='flat' and an extra 'detection_method' key.
    """
    try:
        import fitz
    except ImportError:
        return None, {
            "error": "PyMuPDF not installed", "mode": "flat",
            "filled": [], "skipped": [], "signed": False,
            "date_filled": False, "detection_method": None,
        }

    today      = datetime.now().strftime("%B %-d, %Y")
    sig_pixmap = _load_signature_pixmap()

    signed           = False
    date_filled      = False
    detection_method = None

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        for page in doc:

            # ── Step 1: geometry scan ─────────────────────────────────────
            candidates, crop_y = _find_all_sig_candidates(page)

            if candidates:
                # ── Step 2: Claude classifies by index ────────────────────
                classification = _classify_candidates_with_claude(
                    page, candidates, crop_y
                )
                if classification:
                    sig_idx  = classification.get("sig_idx")
                    date_idx = classification.get("date_idx")
                    fields   = _fields_from_classification(
                        candidates, sig_idx, date_idx
                    )
                    if fields:
                        detection_method = "geometry+claude"
                    else:
                        fields = None
                else:
                    fields = None

            else:
                # ── Step 3: pure-vision fallback ──────────────────────────
                log.info("pdf_sign: no geometry candidates — falling back to Claude vision")
                page_h       = page.rect.height
                crop_y_start = page_h * 0.65
                clip         = fitz.Rect(0, crop_y_start, page.rect.width, page_h)
                pix          = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0), clip=clip)
                img_bytes    = pix.tobytes("png")
                raw = _locate_fields_with_claude(img_bytes, pix.width, pix.height)
                fields = None
                if raw:
                    detection_method = "claude_vision"
                    if "signature_rect" in raw:
                        s = raw["signature_rect"]
                        fields = {
                            "signature_rect": {
                                "x0": s["x0"], "y0": s["y0"] + crop_y_start,
                                "x1": s["x1"], "y1": s["y1"] + crop_y_start,
                            }
                        }
                    if "date_point" in raw:
                        d = raw["date_point"]
                        fields = fields or {}
                        fields["date_point"] = {
                            "x": d["x"], "y": d["y"] + crop_y_start,
                        }

            if not fields:
                log.info("pdf_sign: no fields resolved on page %d", page.number + 1)
                continue

            # ── Apply signature image ──────────────────────────────────────
            if "signature_rect" in fields and sig_pixmap is not None:
                s        = fields["signature_rect"]
                pdf_rect = fitz.Rect(s["x0"], s["y0"], s["x1"], s["y1"])
                page.insert_image(pdf_rect, pixmap=sig_pixmap,
                                  keep_proportion=True, overlay=True)
                signed = True
                log.info("pdf_sign: signature stamped at %s", pdf_rect)

            # ── Apply date text ────────────────────────────────────────────
            if "date_point" in fields:
                d      = fields["date_point"]
                pdf_pt = fitz.Point(d["x"], d["y"])
                page.insert_text(pdf_pt, today, fontsize=11,
                                 color=(0, 0, 0), overlay=True)
                date_filled = True
                log.info("pdf_sign: date written at %s", pdf_pt)

            if signed or date_filled:
                break   # signature section found — don't process further pages

        signed_bytes = doc.tobytes(deflate=True)
        doc.close()

    except Exception as exc:
        log.error("pdf_sign: flat-PDF error: %s", exc, exc_info=True)
        return None, {
            "error": str(exc), "mode": "flat",
            "filled": [], "skipped": [], "signed": signed,
            "date_filled": date_filled, "detection_method": detection_method,
        }

    if not signed and not date_filled:
        return None, {
            "error": (
                "Could not locate the signature or date fields. "
                "The form layout may be unusual — try filling it manually."
            ),
            "mode": "flat", "filled": [], "skipped": [],
            "signed": False, "date_filled": False,
            "detection_method": detection_method,
        }

    return signed_bytes, {
        "mode": "flat", "filled": [], "skipped": [],
        "signed": signed, "date_filled": date_filled,
        "error": None, "detection_method": detection_method,
    }


# ── Slack file download ───────────────────────────────────────────────────────

def _download_pdf(url: str) -> bytes:
    """Download a Slack-hosted file using the bot token."""
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


# ── Result message builder ────────────────────────────────────────────────────

def _build_summary_message(original_name: str, summary: dict) -> str:
    error            = summary.get("error")
    signed           = summary.get("signed",      False)
    date_filled      = summary.get("date_filled", False)
    filled           = summary.get("filled",      [])
    skipped          = summary.get("skipped",     [])
    mode             = summary.get("mode",        "acroform")
    detection_method = summary.get("detection_method")

    if error:
        return f"⚠️ Failed to process `{original_name}`: {error}"

    if not signed and not date_filled and not filled:
        return (
            f"⚠️ Could not find any signature or date fields in `{original_name}`.\n"
            "Ensure the PDF is a standard fillable form or a flat form with clearly "
            "labelled signature and date lines."
        )

    parts = [f"✅ *Signed PDF ready* — `{original_name}`"]
    if mode == "flat":
        if detection_method == "geometry+claude":
            parts.append("_(flat PDF — fields located using geometric analysis + AI)_")
        elif detection_method == "claude_vision":
            parts.append("_(flat PDF — fields located using AI vision)_")
        else:
            parts.append("_(flat PDF)_")

    applied = []
    if signed:
        applied.append("Signature image")
    if date_filled:
        today = datetime.now().strftime("%B %-d, %Y")
        applied.append(f"Date: _{today}_")
    for _, cat, val in filled:
        if cat not in ("signature", "date"):
            applied.append(f"{cat.capitalize()}: _{val}_")
    if applied:
        parts.append("Applied: " + ", ".join(applied))

    if skipped:
        unrecognised = [n for n in skipped if n]
        if unrecognised:
            parts.append(
                f"Unrecognised fields (left blank): `{'`, `'.join(unrecognised[:8])}`"
                + (" …" if len(unrecognised) > 8 else "")
            )

    return "\n".join(parts)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def process_message(event: dict):
    """
    Process one message from the sign-pdf channel.
    Expects at least one PDF attachment.
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

    pdf_file = next(
        (
            f for f in files
            if (f.get("mimetype") == "application/pdf"
                or (f.get("name") or "").lower().endswith(".pdf"))
        ),
        None,
    )

    if not pdf_file:
        config.slack.chat_postMessage(
            channel=channel_id,
            text=(
                "📋 Upload a PDF here and I'll sign it and fill today's date.\n"
                "Fillable AcroForm PDFs are handled automatically. "
                "For flat/printed forms I'll use AI to locate the signature and date lines."
            ),
        )
        return

    original_name = pdf_file.get("name") or "document.pdf"
    dl_url = pdf_file.get("url_private_download") or pdf_file.get("url_private") or ""

    config.slack.chat_postMessage(
        channel=channel_id,
        text=f"✍️ Signing `{original_name}`…",
    )

    try:
        pdf_bytes = _download_pdf(dl_url)
    except Exception as exc:
        log.error("pdf_sign: download failed for %s: %s", original_name, exc)
        config.slack.chat_postMessage(
            channel=channel_id,
            text=f"⚠️ Could not download `{original_name}`: {exc}",
        )
        return

    # Dispatch to the right signing path
    if _has_acroform_fields(pdf_bytes):
        signed_bytes, summary = _sign_acroform(pdf_bytes)
    else:
        log.info("pdf_sign: no AcroForm fields in %s — using flat-PDF pipeline", original_name)
        signed_bytes, summary = _sign_flat(pdf_bytes)

    summary_msg = _build_summary_message(original_name, summary)

    if signed_bytes is None:
        config.slack.chat_postMessage(channel=channel_id, text=summary_msg)
        activity_log.record("pdf_sign", outcome="error",
                            filename=original_name, error=summary.get("error"))
        return

    stem     = Path(original_name).stem
    out_name = f"{stem}_signed.pdf"

    try:
        config.slack.files_upload_v2(
            channel=channel_id,
            content=signed_bytes,
            filename=out_name,
            title=f"Signed — {original_name}",
            initial_comment=summary_msg,
        )
    except Exception as exc:
        log.error("pdf_sign: upload failed: %s", exc)
        config.slack.chat_postMessage(
            channel=channel_id,
            text=f"⚠️ Could not upload signed PDF: {exc}",
        )
        activity_log.record("pdf_sign", outcome="upload_error",
                            filename=original_name, error=str(exc))
        return

    activity_log.record(
        "pdf_sign",
        filename=original_name,
        mode=summary.get("mode"),
        detection_method=summary.get("detection_method"),
        fields_filled=len(summary.get("filled", [])),
        signature_applied=summary.get("signed", False),
        date_applied=summary.get("date_filled", False),
    )

    if ts:
        mark_processed(channel_id, ts)
