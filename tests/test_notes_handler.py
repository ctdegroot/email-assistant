"""
Tests for slack_notes_handler.py — pure logic only.
No Slack API calls, no file downloads.
"""

import pytest

from email_to_motion.slack_notes_handler import (
    _extract_email_content,
    _mime_to_filetype,
    _unwrap_forward,
)


# ── _unwrap_forward ────────────────────────────────────────────────────────────

class TestUnwrapForward:

    # Subject stripping ────────────────────────────────────────────────────────

    def test_fwd_prefix_stripped(self):
        subject, _ = _unwrap_forward("Fwd: Meeting tomorrow", "")
        assert subject == "Meeting tomorrow"

    def test_fw_prefix_stripped(self):
        subject, _ = _unwrap_forward("FW: Budget review", "")
        assert subject == "Budget review"

    def test_fwd_case_insensitive(self):
        subject, _ = _unwrap_forward("fwd: lower case", "")
        assert subject == "lower case"

    def test_multiple_fwd_all_stripped(self):
        subject, _ = _unwrap_forward("Fwd: Fwd: Original", "")
        assert subject == "Original"
        assert not subject.lower().startswith("fwd")

    def test_non_fwd_subject_unchanged(self):
        subject, _ = _unwrap_forward("Regular email subject", "some body")
        assert subject == "Regular email subject"

    # Sender extraction ────────────────────────────────────────────────────────

    def test_gmail_style_divider(self):
        body = (
            "Hey, passing this along.\n\n"
            "---------- Forwarded message ---------\n"
            "From: Jane Smith <jane@example.com>\n"
            "Date: Mon, 10 Mar 2026\n"
            "Subject: Meeting tomorrow\n"
        )
        _, sender = _unwrap_forward("Fwd: Meeting tomorrow", body)
        assert sender is not None
        assert "jane@example.com" in sender or "Jane Smith" in sender

    def test_outlook_style_divider(self):
        body = (
            "See below.\n\n"
            "-----Original Message-----\n"
            "From: Bob Jones <bob@corp.com>\n"
            "Sent: Tuesday, March 10 2026\n"
        )
        _, sender = _unwrap_forward("FW: Update", body)
        assert sender is not None
        assert "bob@corp.com" in sender or "Bob Jones" in sender

    def test_begin_forwarded_message_style(self):
        body = (
            "FYI\n\n"
            "Begin forwarded message:\n"
            "From: Alice <alice@uni.edu>\n"
            "Subject: Lab report\n"
        )
        _, sender = _unwrap_forward("Fwd: Lab report", body)
        assert sender is not None
        assert "alice@uni.edu" in sender or "Alice" in sender

    def test_no_forward_markers_returns_none_sender(self):
        _, sender = _unwrap_forward("Regular email", "Plain body with no forwarded section")
        assert sender is None

    def test_body_with_from_but_no_marker_uses_first_from(self):
        # No divider — falls back to the first From: line in the whole body
        body = "From: Someone <x@example.com>\nBody text here."
        _, sender = _unwrap_forward("Fwd: Something", body)
        assert sender is not None
        assert "x@example.com" in sender or "Someone" in sender

    def test_returns_tuple_of_two(self):
        result = _unwrap_forward("Fwd: Test", "")
        assert isinstance(result, tuple) and len(result) == 2


# ── _extract_email_content ─────────────────────────────────────────────────────

def _email_file(subject: str, frm_original: str, plain_text: str) -> dict:
    """Build a mock Slack filetype='email' file dict."""
    return {
        "filetype":   "email",
        "subject":    subject,
        "from":       [{"original": frm_original, "address": "fallback@example.com"}],
        "plain_text": plain_text,
    }


class TestExtractEmailContent:

    def test_basic_extraction(self):
        files = [_email_file("Test Subject", "Alice <alice@example.com>", "Hello body")]
        subject, sender, body, attachments = _extract_email_content(files)
        assert subject == "Test Subject"
        assert "alice@example.com" in sender or "Alice" in sender
        assert body == "Hello body"
        assert attachments == []

    def test_attachments_separated_from_email_file(self):
        email = _email_file("Subject", "Alice <a@b.com>", "Body")
        pdf   = {"filetype": "pdf", "name": "report.pdf"}
        _, _, _, attachments = _extract_email_content([email, pdf])
        assert len(attachments) == 1
        assert attachments[0]["name"] == "report.pdf"

    def test_multiple_attachments(self):
        email = _email_file("Subject", "Alice <a@b.com>", "Body")
        pdf   = {"filetype": "pdf",  "name": "report.pdf"}
        docx  = {"filetype": "docx", "name": "notes.docx"}
        _, _, _, attachments = _extract_email_content([email, pdf, docx])
        assert len(attachments) == 2

    def test_forwarded_email_unwrapped(self):
        body = (
            "Forwarding this for your info.\n\n"
            "---------- Forwarded message ---------\n"
            "From: Original Sender <orig@example.com>\n"
            "Subject: The real subject\n"
        )
        files = [_email_file("Fwd: The real subject", "Me <me@example.com>", body)]
        subject, sender, _, _ = _extract_email_content(files)
        assert not subject.lower().startswith("fwd")
        assert "orig@example.com" in sender or "Original Sender" in sender

    def test_no_email_file_returns_defaults(self):
        files = [{"filetype": "pdf", "name": "doc.pdf"}]
        subject, sender, body, attachments = _extract_email_content(files)
        assert subject == "Untitled"
        assert sender  == "Unknown"
        assert body    == ""
        assert len(attachments) == 1

    def test_empty_file_list_returns_defaults(self):
        subject, sender, body, attachments = _extract_email_content([])
        assert subject     == "Untitled"
        assert sender      == "Unknown"
        assert body        == ""
        assert attachments == []

    def test_falls_back_to_address_when_no_original(self):
        files = [{
            "filetype":   "email",
            "subject":    "Hello",
            "from":       [{"address": "noname@example.com"}],
            "plain_text": "body",
        }]
        _, sender, _, _ = _extract_email_content(files)
        assert sender == "noname@example.com"

    def test_missing_from_field_uses_unknown(self):
        files = [{
            "filetype":   "email",
            "subject":    "Hello",
            "from":       None,
            "plain_text": "body",
        }]
        _, sender, _, _ = _extract_email_content(files)
        assert sender == "Unknown"

    def test_missing_subject_uses_untitled(self):
        files = [{
            "filetype":   "email",
            "subject":    None,
            "from":       [{"original": "Alice <a@b.com>"}],
            "plain_text": "body",
        }]
        subject, _, _, _ = _extract_email_content(files)
        assert subject == "Untitled"

    def test_empty_plain_text_is_empty_string(self):
        files = [_email_file("Subject", "Alice <a@b.com>", "")]
        _, _, body, _ = _extract_email_content(files)
        assert body == ""

    def test_pdf_embedded_in_email_file_is_extracted(self):
        """Attachments from the original email live inside the email file's 'files' list."""
        email = {
            **_email_file("Agenda - Mar 19 2026", "wedean@uwo.ca", "See attached agenda."),
            "files": [{"filetype": "pdf", "name": "agenda.pdf", "mimetype": "application/pdf"}],
        }
        _, _, _, attachments = _extract_email_content([email])
        assert len(attachments) == 1
        assert attachments[0]["name"] == "agenda.pdf"

    def test_multiple_pdfs_embedded_in_email_file(self):
        email = {
            **_email_file("Two docs", "sender@example.com", "Body"),
            "files": [
                {"filetype": "pdf",  "name": "doc1.pdf"},
                {"filetype": "docx", "name": "doc2.docx"},
            ],
        }
        _, _, _, attachments = _extract_email_content([email])
        names = {a["name"] for a in attachments}
        assert names == {"doc1.pdf", "doc2.docx"}

    def test_embedded_and_direct_attachments_combined(self):
        """Both embedded (inside email file) and direct top-level attachments are returned."""
        email = {
            **_email_file("Subject", "Alice <a@b.com>", "Body"),
            "files": [{"filetype": "pdf", "name": "embedded.pdf"}],
        }
        direct = {"filetype": "pdf", "name": "direct.pdf"}
        _, _, _, attachments = _extract_email_content([email, direct])
        names = {a["name"] for a in attachments}
        assert names == {"embedded.pdf", "direct.pdf"}

    def test_email_file_with_no_files_key_still_works(self):
        """Email file objects with no 'files' key should not raise."""
        email = _email_file("Subject", "Alice <a@b.com>", "Body")
        # _email_file() doesn't add a 'files' key — this should still work fine
        _, _, _, attachments = _extract_email_content([email])
        assert attachments == []

    def test_email_file_with_empty_files_list(self):
        email = {**_email_file("Subject", "Alice <a@b.com>", "Body"), "files": []}
        _, _, _, attachments = _extract_email_content([email])
        assert attachments == []

    # ── f["attachments"] path (actual Slack email-attachment format) ────────────

    def test_pdf_in_attachments_field_is_extracted(self):
        """PDF in f['attachments'] (the real Slack format) is picked up."""
        email = {
            **_email_file("Agenda", "wedean@uwo.ca", "See attached."),
            "attachments": [{
                "name":                 "FEC-Agenda-Mar19.pdf",
                "mimetype":             "application/pdf",
                "url_private_download": "https://files.slack.com/files-pri/xxx/FEC-Agenda-Mar19.pdf",
                "url_private":          "https://files.slack.com/files-pri/xxx/FEC-Agenda-Mar19.pdf",
            }],
        }
        _, _, _, attachments = _extract_email_content([email])
        assert len(attachments) == 1
        assert attachments[0]["name"] == "FEC-Agenda-Mar19.pdf"
        assert attachments[0]["mimetype"] == "application/pdf"
        assert attachments[0]["filetype"] == "pdf"

    def test_attachment_uses_filename_field(self):
        """Slack email attachment objects use 'filename' as the primary name field."""
        email = {
            **_email_file("S", "x@y.com", "body"),
            "attachments": [{"filename": "agenda.pdf", "mimetype": "application/pdf",
                             "url": "https://files-origin.slack.com/x"}],
        }
        _, _, _, attachments = _extract_email_content([email])
        assert attachments[0]["name"] == "agenda.pdf"

    def test_attachment_falls_back_to_name_then_title(self):
        """Falls back to 'name' then 'title' when 'filename' is absent."""
        email = {
            **_email_file("S", "x@y.com", "body"),
            "attachments": [{"title": "report.pdf", "mimetype": "application/pdf",
                             "url_private": "https://slack.com/x"}],
        }
        _, _, _, attachments = _extract_email_content([email])
        assert attachments[0]["name"] == "report.pdf"

    def test_attachment_filetype_derived_from_mime(self):
        email = {
            **_email_file("S", "x@y.com", "body"),
            "attachments": [{"filename": "file.docx",
                             "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             "url": "https://files-origin.slack.com/x"}],
        }
        _, _, _, attachments = _extract_email_content([email])
        assert attachments[0]["filetype"] == "docx"

    def test_attachment_with_no_url_is_still_listed(self):
        """Attachments without a download URL should still appear in the list (listed only)."""
        email = {
            **_email_file("S", "x@y.com", "body"),
            "attachments": [{"name": "doc.pdf", "mimetype": "application/pdf"}],
        }
        _, _, _, attachments = _extract_email_content([email])
        assert len(attachments) == 1
        assert attachments[0]["name"] == "doc.pdf"

    def test_empty_attachments_list_produces_no_attachments(self):
        email = {**_email_file("S", "x@y.com", "body"), "attachments": []}
        _, _, _, attachments = _extract_email_content([email])
        assert attachments == []


# ── _mime_to_filetype ──────────────────────────────────────────────────────────

class TestMimeToFiletype:
    def test_pdf_mime(self):
        assert _mime_to_filetype("application/pdf") == "pdf"

    def test_docx_mime(self):
        assert _mime_to_filetype(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ) == "docx"

    def test_unknown_mime_uses_subtype(self):
        assert _mime_to_filetype("image/jpeg") == "jpeg"

    def test_vnd_prefix_stripped(self):
        result = _mime_to_filetype("application/vnd.ms-excel")
        assert "vnd" not in result

    def test_empty_mime_returns_empty(self):
        assert _mime_to_filetype("") == ""
