"""
tests/test_ref_letter.py — Unit tests for ref_letter_handler.

Coverage:
  - YAML parsing and validation
  - LaTeX escaping
  - RE line and salutation generation
  - .tex rendering
  - process_message routing (no YAML → usage message; valid YAML → generation)
  - PDF/zip pipeline (mocked subprocess)
"""

import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

import email_to_motion.config as _cfg

_cfg.slack  = MagicMock()
_cfg.claude = MagicMock()
_cfg.NOTES_OUTPUT_PATH       = "/tmp/test_notes_rl"
_cfg.SLACK_REFLETTER_CHANNEL = "ref-letters"
_cfg.REF_LETTERS_OUTPUT_PATH = "/tmp/test_ref_letters"
_cfg.REF_LETTER_TEMPLATES_DIR = "/tmp/test_templates_rl"
_cfg.MOTION_API_KEY          = "test-key"
_cfg.MOTION_WORKSPACE_ID     = "ws-1"
_cfg.MOTION_ASSIGNEE_ID      = "user-1"
_cfg.ALLOWED_SLACK_USER_ID   = "U_OWNER"
_cfg.OWN_BOT_ID              = "BOT123"

from email_to_motion.ref_letter_handler import (
    _latex_escape,
    _re_line,
    _salutation,
    _render_tex,
    _body_to_latex,
    _make_stem,
    _check_template_files,
    send_template,
    process_message,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_mocks():
    _cfg.slack.reset_mock()
    _cfg.claude.reset_mock()


@pytest.fixture
def templates_dir(tmp_path):
    """Create a fake templates directory with the required files."""
    d = tmp_path / "templates"
    d.mkdir()
    (d / "WesternLetter.cls").write_text("% fake cls", encoding="utf-8")
    (d / "Signature.pdf").write_bytes(b"%PDF-1.4 fake")
    _cfg.REF_LETTER_TEMPLATES_DIR = str(d)
    yield d


MINIMAL_YAML = yaml.dump({
    "candidate": {
        "name": "Jane Smith",
        "role": "MASc student",
        "relationship": "Supervised Jane's MASc thesis (2022–2024)",
    },
    "letter": {
        "type": "phd-application",
        "addressee": "Graduate Admissions Committee",
    },
    "strengths": [
        {"label": "Research Independence", "detail": "Strong self-direction."}
    ],
    "specific_examples": ["First-authored a conference paper."],
    "tone": None,
    "weaknesses": None,
})

FULL_YAML = yaml.dump({
    "candidate": {
        "name": "Jane Smith",
        "role": "MASc student",
        "degree_completed": "MASc, Mechanical Engineering, Western, 2024",
        "target_program": "PhD in Mechanical Engineering",
        "target_institution": "MIT",
        "relationship": "Supervised Jane's MASc thesis at Western (2022–2024)",
        "duration_known": "2 years",
        "context": "Jane led the CFD modeling component of our UV reactor project.",
    },
    "letter": {
        "type": "phd-application",
        "deadline": "2026-06-01",
        "addressee": "Graduate Admissions Committee",
    },
    "strengths": [
        {"label": "Research Independence", "detail": "Self-directed workflow ownership."},
        {"label": "Technical Skill",       "detail": "OpenFOAM, Python."},
    ],
    "specific_examples": [
        "Identified a flaw in our radiation model.",
        "First-authored a CSCE 2024 paper.",
    ],
    "areas_to_emphasize": ["PhD readiness", "CFD depth"],
    "tone": "Particularly warm — strongest student I have supervised.",
    "weaknesses": None,
})


def _mock_yaml_file(content: str, name: str = "context.yaml") -> dict:
    return {
        "filetype": "yaml",
        "name": name,
        "url_private_download": "https://slack/files/context.yaml",
        "url_private": "https://slack/files/context.yaml",
    }


def _mock_event(files: list, channel: str = "C_RL") -> dict:
    return {"channel": channel, "ts": "1234.5678", "files": files}


# ── _latex_escape ─────────────────────────────────────────────────────────────

class TestLatexEscape:

    def test_escapes_ampersand(self):
        assert r"\&" in _latex_escape("A & B")

    def test_escapes_percent(self):
        assert r"\%" in _latex_escape("100%")

    def test_escapes_dollar(self):
        assert r"\$" in _latex_escape("$100")

    def test_escapes_underscore(self):
        assert r"\_" in _latex_escape("hello_world")

    def test_escapes_hash(self):
        assert r"\#" in _latex_escape("#1")

    def test_escapes_backslash_without_double_escaping_braces(self):
        # The single-pass regex must not re-escape the {} in \textbackslash{}
        result = _latex_escape("\\")
        assert result == r"\textbackslash{}"

    def test_plain_text_unchanged(self):
        text = "Hello World. This is a test."
        assert _latex_escape(text) == text


# ── _re_line ──────────────────────────────────────────────────────────────────

class TestReLine:

    def test_phd_application(self):
        data = {"candidate": {"name": "Jane Smith"}, "letter": {"type": "phd-application"}}
        assert "Jane Smith" in _re_line(data)
        assert "PhD Application" in _re_line(data)

    def test_fellowship(self):
        data = {"candidate": {"name": "Bob Jones"}, "letter": {"type": "fellowship"}}
        assert "Fellowship Application" in _re_line(data)

    def test_unknown_type_falls_back(self):
        data = {"candidate": {"name": "Alice"}, "letter": {"type": "unknown"}}
        assert "Application" in _re_line(data)

    def test_special_chars_escaped(self):
        data = {"candidate": {"name": "O'Brien & Co"}, "letter": {"type": "phd-application"}}
        result = _re_line(data)
        assert r"\&" in result   # ampersand escaped to \&


# ── _salutation ───────────────────────────────────────────────────────────────

class TestSalutation:

    def test_committee_uses_to_the(self):
        data = {"letter": {"addressee": "Graduate Admissions Committee"}}
        assert _salutation(data).startswith("To the")

    def test_named_prof_uses_dear(self):
        data = {"letter": {"addressee": "Prof. John Smith"}}
        assert _salutation(data).startswith("Dear")

    def test_dr_uses_dear(self):
        data = {"letter": {"addressee": "Dr. Jane Doe"}}
        assert _salutation(data).startswith("Dear")

    def test_missing_addressee_uses_fallback(self):
        data = {"letter": {}}
        result = _salutation(data)
        assert "Committee" in result


# ── _body_to_latex ────────────────────────────────────────────────────────────

class TestBodyToLatex:

    def test_paragraphs_separated_by_blank_line(self):
        body = "Para one.\n\nPara two."
        result = _body_to_latex(body)
        assert "Para one." in result
        assert "Para two." in result
        assert "\n\n" in result

    def test_special_chars_escaped_in_body(self):
        body = "Score 100% on all tasks."
        assert r"\%" in _body_to_latex(body)

    def test_single_paragraph(self):
        body = "Just one paragraph."
        assert _body_to_latex(body) == "Just one paragraph."


# ── _render_tex ───────────────────────────────────────────────────────────────

class TestRenderTex:

    def test_contains_documentclass(self):
        data = yaml.safe_load(MINIMAL_YAML)
        tex = _render_tex(data, "Letter body here.")
        assert r"\documentclass{WesternLetter}" in tex

    def test_contains_re_line(self):
        data = yaml.safe_load(MINIMAL_YAML)
        tex = _render_tex(data, "Body.")
        assert "Jane Smith" in tex

    def test_contains_salutation(self):
        data = yaml.safe_load(MINIMAL_YAML)
        tex = _render_tex(data, "Body.")
        assert "Graduate Admissions Committee" in tex

    def test_contains_body(self):
        data = yaml.safe_load(MINIMAL_YAML)
        tex = _render_tex(data, "My unique body text here.")
        assert "My unique body text here." in tex

    def test_contains_closing(self):
        data = yaml.safe_load(MINIMAL_YAML)
        tex = _render_tex(data, "Body.")
        assert r"\closingwithsignature" in tex

    def test_no_unresolved_placeholders(self):
        data = yaml.safe_load(MINIMAL_YAML)
        tex = _render_tex(data, "Body.")
        assert "<<" not in tex
        assert ">>" not in tex


# ── _make_stem ────────────────────────────────────────────────────────────────

class TestMakeStem:

    def test_contains_candidate_name_slug(self):
        data = {"candidate": {"name": "Jane Smith"}}
        stem = _make_stem(data)
        assert "jane_smith" in stem

    def test_ends_with_ref_letter(self):
        data = {"candidate": {"name": "Bob"}}
        assert _make_stem(data).endswith("_ref_letter")

    def test_starts_with_date(self):
        import re
        data = {"candidate": {"name": "Alice"}}
        assert re.match(r'\d{4}-\d{2}-\d{2}_', _make_stem(data))


# ── _check_template_files ─────────────────────────────────────────────────────

class TestCheckTemplateFiles:

    def test_no_missing_when_all_present(self, templates_dir):
        assert _check_template_files() == []

    def test_detects_missing_cls(self, templates_dir):
        (templates_dir / "WesternLetter.cls").unlink()
        missing = _check_template_files()
        assert "WesternLetter.cls" in missing

    def test_detects_missing_signature(self, templates_dir):
        (templates_dir / "Signature.pdf").unlink()
        missing = _check_template_files()
        assert "Signature.pdf" in missing


# ── send_template ─────────────────────────────────────────────────────────────

class TestSendTemplate:

    def test_uploads_content_not_path(self, templates_dir):
        """send_template() must use content= (bytes) not file= (local path)."""
        (templates_dir / "template.yaml").write_text("# blank\n", encoding="utf-8")
        send_template("C_TEST")
        _cfg.slack.files_upload_v2.assert_called_once()
        call_kwargs = _cfg.slack.files_upload_v2.call_args[1]
        assert "content" in call_kwargs, "Should use content= not file= so it works on servers"
        assert "file" not in call_kwargs
        assert call_kwargs["channel"] == "C_TEST"
        assert "yaml" in call_kwargs["filename"].lower()

    def test_posts_error_when_template_file_missing(self, tmp_path):
        """If template.yaml is absent, a plain error message is posted instead."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        _cfg.REF_LETTER_TEMPLATES_DIR = str(empty_dir)
        send_template("C_TEST")
        _cfg.slack.files_upload_v2.assert_not_called()
        _cfg.slack.chat_postMessage.assert_called_once()
        text = _cfg.slack.chat_postMessage.call_args[1]["text"]
        assert "template" in text.lower() or "not found" in text.lower()


# ── process_message ───────────────────────────────────────────────────────────

class TestProcessMessage:

    def test_ignores_own_bot_messages(self):
        event = {"bot_id": "BOT123", "channel": "C_RL", "ts": "1.0", "files": []}
        process_message(event)
        _cfg.slack.chat_postMessage.assert_not_called()

    def test_ignores_message_changed_subtype(self):
        event = {"subtype": "message_changed", "channel": "C_RL", "ts": "1.0", "files": []}
        process_message(event)
        _cfg.slack.chat_postMessage.assert_not_called()

    def test_uploads_template_when_no_yaml_attached(self, templates_dir):
        """When no YAML is attached the bot uploads the blank template file."""
        # Create a fake template.yaml inside the templates dir fixture already
        # set up by the templates_dir fixture.
        (templates_dir / "template.yaml").write_text("# blank template\n", encoding="utf-8")
        event = _mock_event(files=[])
        process_message(event)
        _cfg.slack.files_upload_v2.assert_called_once()
        call_kwargs = _cfg.slack.files_upload_v2.call_args[1]
        assert "yaml" in call_kwargs.get("filename", "").lower() or \
               "yaml" in call_kwargs.get("title", "").lower()

    def test_posts_error_on_invalid_yaml(self):
        yaml_file = _mock_yaml_file("this: is: not: valid: yaml: ::::")
        event = _mock_event(files=[yaml_file])
        with patch("email_to_motion.ref_letter_handler._download_slack_file",
                   return_value=b"this: is: not: valid: yaml: ::::"):
            process_message(event)
        calls = [c[1]["text"] for c in _cfg.slack.chat_postMessage.call_args_list]
        assert any("parse" in t.lower() or "yaml" in t.lower() for t in calls)

    def test_posts_error_when_candidate_name_missing(self):
        bad = yaml.dump({"candidate": {}, "letter": {"type": "phd-application"}}).encode()
        yaml_file = _mock_yaml_file("")
        event = _mock_event(files=[yaml_file])
        with patch("email_to_motion.ref_letter_handler._download_slack_file",
                   return_value=bad):
            process_message(event)
        calls = [c[1]["text"] for c in _cfg.slack.chat_postMessage.call_args_list]
        assert any("parse" in t.lower() or "name" in t.lower() for t in calls)

    def test_posts_error_when_template_files_missing(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        _cfg.REF_LETTER_TEMPLATES_DIR = str(empty_dir)

        yaml_file = _mock_yaml_file("")
        event = _mock_event(files=[yaml_file])
        with patch("email_to_motion.ref_letter_handler._download_slack_file",
                   return_value=MINIMAL_YAML.encode()):
            process_message(event)
        texts = [c[1]["text"] for c in _cfg.slack.chat_postMessage.call_args_list]
        assert any("missing" in t.lower() or "template" in t.lower() for t in texts)

    def test_full_pipeline_calls_claude_and_uploads(self, templates_dir, tmp_path):
        _cfg.REF_LETTERS_OUTPUT_PATH = str(tmp_path / "out")

        # Mock Claude response
        fake_response      = MagicMock()
        fake_response.content = [MagicMock(text="First paragraph.\n\nSecond paragraph.")]
        _cfg.claude.messages.create.return_value = fake_response

        yaml_file = _mock_yaml_file("")
        event = _mock_event(files=[yaml_file])

        with patch("email_to_motion.ref_letter_handler._download_slack_file",
                   return_value=MINIMAL_YAML.encode()), \
             patch("email_to_motion.ref_letter_handler._compile_pdf",
                   return_value=(None, "pdflatex not found")):
            process_message(event)

        # Claude was called
        _cfg.claude.messages.create.assert_called_once()
        # File upload was attempted
        _cfg.slack.files_upload_v2.assert_called_once()

    def test_tex_contains_candidate_name(self, templates_dir, tmp_path):
        """The generated .tex file (written to output dir) contains the candidate name."""
        _cfg.REF_LETTERS_OUTPUT_PATH = str(tmp_path / "out")

        fake_response      = MagicMock()
        fake_response.content = [MagicMock(text="Great candidate.\n\nMore detail.")]
        _cfg.claude.messages.create.return_value = fake_response

        yaml_file = _mock_yaml_file("")
        event = _mock_event(files=[yaml_file])

        written_tex = {}

        def _fake_compile(tex_content, stem):
            written_tex["content"] = tex_content
            return None, "pdflatex not found"

        with patch("email_to_motion.ref_letter_handler._download_slack_file",
                   return_value=MINIMAL_YAML.encode()), \
             patch("email_to_motion.ref_letter_handler._compile_pdf", side_effect=_fake_compile):
            process_message(event)

        assert "Jane Smith" in written_tex.get("content", "")

    def test_zip_contains_tex_file(self, templates_dir, tmp_path):
        """The uploaded zip contains a .tex file."""
        out_dir = tmp_path / "out"
        _cfg.REF_LETTERS_OUTPUT_PATH = str(out_dir)

        fake_response      = MagicMock()
        fake_response.content = [MagicMock(text="Body text.")]
        _cfg.claude.messages.create.return_value = fake_response

        yaml_file = _mock_yaml_file("")
        event = _mock_event(files=[yaml_file])

        with patch("email_to_motion.ref_letter_handler._download_slack_file",
                   return_value=MINIMAL_YAML.encode()), \
             patch("email_to_motion.ref_letter_handler._compile_pdf",
                   return_value=(None, "not found")), \
             patch("email_to_motion.ref_letter_handler._cleanup_output_files"):
            process_message(event)

        upload_call = _cfg.slack.files_upload_v2.call_args
        zip_file_path = Path(upload_call[1]["file"])
        assert zip_file_path.suffix == ".zip"
        assert zip_file_path.exists()
        with zipfile.ZipFile(zip_file_path) as zf:
            names = zf.namelist()
        assert any(n.endswith(".tex") for n in names)
