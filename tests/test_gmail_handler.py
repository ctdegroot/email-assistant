"""
Tests for gmail_handler.py — Gmail CC alias ingestion pipeline.

All network calls (IMAP, SMTP, Claude, ICS feed) are mocked.
"""

import email
import email.utils
import json
import pathlib
from datetime import date, datetime, time, timedelta
from email.mime.text import MIMEText
from unittest.mock import MagicMock, call, patch

import pytest
import pytz

from email_to_motion import config
from email_to_motion.gmail_handler import (
    _ALIAS_ACTIONS,
    _SIGNOFF,
    _STATE_FILE,
    _build_addressing_line,
    _build_greeting,
    _build_reply,
    _decode_header,
    _extract_body,
    _extract_other_recipients,
    _find_bot_alias,
    _is_bot_address,
    _load_processed,
    _parse_scheduling_params,
    _save_processed,
    _send_reply,
    poll_and_process,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

BOT_USER = "ctdegroot.digital.assistant@gmail.com"
BOT_BASE = "ctdegroot.digital.assistant"
BOT_DOMAIN = "gmail.com"
SCHEDULING_ALIAS = f"{BOT_BASE}+scheduling@{BOT_DOMAIN}"


def _make_msg(
    from_: str = "Alice Smith <alice@example.com>",
    to: str = "",
    cc: str = "",
    subject: str = "Meeting request",
    body: str = "Hi Chris,\n\nCan we meet next week?\n\nBest,\nAlice",
    msg_id: str = "<test-123@example.com>",
    references: str = "",
) -> email.message.Message:
    """Build a simple plain-text email.message.Message for testing."""
    msg = email.message.Message()
    msg["From"] = from_
    msg["To"] = to
    msg["CC"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = msg_id
    if references:
        msg["References"] = references
    msg.set_payload(body, charset="utf-8")
    msg["Content-Type"] = "text/plain; charset=utf-8"
    return msg


@pytest.fixture(autouse=True)
def patch_smtp_user(monkeypatch):
    """Ensure SMTP_USER, SMTP_PASSWORD, and claude client are set for all tests."""
    monkeypatch.setattr(config, "SMTP_USER", BOT_USER)
    monkeypatch.setattr(config, "SMTP_PASSWORD", "fake-app-password")
    monkeypatch.setattr(config, "claude", MagicMock())


# ── TestDecodeHeader ──────────────────────────────────────────────────────────

class TestDecodeHeader:

    def test_plain_ascii(self):
        assert _decode_header("Hello World") == "Hello World"

    def test_none_returns_empty(self):
        assert _decode_header(None) == ""

    def test_empty_string(self):
        assert _decode_header("") == ""

    def test_encoded_utf8(self):
        # =?UTF-8?B? base64-encoded "Héllo"
        encoded = "=?UTF-8?B?SMOpbGxv?="
        result = _decode_header(encoded)
        assert "H" in result  # at minimum decodes without crashing


# ── TestIsBotAddress ──────────────────────────────────────────────────────────

class TestIsBotAddress:

    def test_base_address(self):
        assert _is_bot_address(BOT_USER) is True

    def test_scheduling_alias(self):
        assert _is_bot_address(SCHEDULING_ALIAS) is True

    def test_arbitrary_plus_alias(self):
        assert _is_bot_address(f"{BOT_BASE}+anything@{BOT_DOMAIN}") is True

    def test_different_user(self):
        assert _is_bot_address("someone.else@gmail.com") is False

    def test_different_domain(self):
        assert _is_bot_address(f"{BOT_BASE}@outlook.com") is False

    def test_case_insensitive(self):
        assert _is_bot_address(BOT_USER.upper()) is True


# ── TestFindBotAlias ──────────────────────────────────────────────────────────

class TestFindBotAlias:

    def test_scheduling_alias_in_to(self):
        msg = _make_msg(to=SCHEDULING_ALIAS)
        assert _find_bot_alias(msg) == "scheduling"

    def test_scheduling_alias_in_cc(self):
        msg = _make_msg(cc=SCHEDULING_ALIAS)
        assert _find_bot_alias(msg) == "scheduling"

    def test_base_address_no_suffix_ignored(self):
        msg = _make_msg(to=BOT_USER)
        assert _find_bot_alias(msg) is None

    def test_unknown_suffix_ignored(self):
        msg = _make_msg(to=f"{BOT_BASE}+unknown@{BOT_DOMAIN}")
        assert _find_bot_alias(msg) is None

    def test_no_bot_address_returns_none(self):
        msg = _make_msg(to="alice@example.com", cc="bob@example.com")
        assert _find_bot_alias(msg) is None

    def test_case_insensitive_local(self):
        msg = _make_msg(to=SCHEDULING_ALIAS.upper())
        assert _find_bot_alias(msg) == "scheduling"

    def test_scheduling_among_multiple_recipients(self):
        msg = _make_msg(
            to=f"alice@example.com, {SCHEDULING_ALIAS}",
            cc="bob@example.com",
        )
        assert _find_bot_alias(msg) == "scheduling"


# ── TestExtractOtherRecipients ────────────────────────────────────────────────

class TestExtractOtherRecipients:

    def test_single_sender_only(self):
        msg = _make_msg(from_="Alice <alice@example.com>", to=SCHEDULING_ALIAS)
        others = _extract_other_recipients(msg)
        addrs = [email.utils.parseaddr(r)[1].lower() for r in others]
        assert "alice@example.com" in addrs

    def test_filters_scheduling_alias(self):
        msg = _make_msg(to=SCHEDULING_ALIAS)
        others = _extract_other_recipients(msg)
        addrs = [email.utils.parseaddr(r)[1].lower() for r in others]
        assert SCHEDULING_ALIAS not in addrs

    def test_filters_smtp_user(self):
        msg = _make_msg(
            from_="Alice <alice@example.com>",
            to=f"{BOT_USER}, {SCHEDULING_ALIAS}",
        )
        others = _extract_other_recipients(msg)
        addrs = [email.utils.parseaddr(r)[1].lower() for r in others]
        assert BOT_USER not in addrs
        assert SCHEDULING_ALIAS not in addrs

    def test_multiple_cc_returned(self):
        msg = _make_msg(
            from_="Alice <alice@example.com>",
            to=SCHEDULING_ALIAS,
            cc="Bob <bob@example.com>, Carol <carol@example.com>",
        )
        others = _extract_other_recipients(msg)
        addrs = [email.utils.parseaddr(r)[1].lower() for r in others]
        assert "alice@example.com" in addrs
        assert "bob@example.com" in addrs
        assert "carol@example.com" in addrs

    def test_no_duplicates(self):
        msg = _make_msg(
            from_="Alice <alice@example.com>",
            to=f"Alice <alice@example.com>, {SCHEDULING_ALIAS}",
        )
        others = _extract_other_recipients(msg)
        addrs = [email.utils.parseaddr(r)[1].lower() for r in others]
        assert addrs.count("alice@example.com") == 1


# ── TestExtractBody ───────────────────────────────────────────────────────────

class TestExtractBody:

    def test_simple_plain_text(self):
        msg = _make_msg(body="Hello World")
        assert "Hello World" in _extract_body(msg)

    def test_multipart_prefers_plain(self):
        import email as _email
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText as _MIMEText
        msg = MIMEMultipart("alternative")
        msg.attach(_MIMEText("Plain text body", "plain", "utf-8"))
        msg.attach(_MIMEText("<b>HTML body</b>", "html", "utf-8"))
        body = _extract_body(msg)
        assert "Plain text body" in body
        assert "<b>" not in body

    def test_multipart_falls_back_to_html(self):
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText as _MIMEText
        msg = MIMEMultipart("alternative")
        msg.attach(_MIMEText("<b>HTML only</b>", "html", "utf-8"))
        body = _extract_body(msg)
        assert "<b>HTML only</b>" in body


# ── TestBuildGreeting ─────────────────────────────────────────────────────────

class TestBuildGreeting:

    def test_no_recipients_fallback(self):
        greeting = _build_greeting([])
        assert greeting == "Hello,"

    def test_one_person_by_display_name(self):
        greeting = _build_greeting(["Alice Smith <alice@example.com>"])
        assert greeting == "Hello Alice,"

    def test_one_person_by_address_local(self):
        greeting = _build_greeting(["alice@example.com"])
        assert greeting == "Hello alice,"

    def test_two_people(self):
        greeting = _build_greeting([
            "Alice <alice@example.com>",
            "Bob <bob@example.com>",
        ])
        assert greeting == "Hello Alice and Bob,"

    def test_three_or_more_says_all(self):
        greeting = _build_greeting([
            "Alice <alice@example.com>",
            "Bob <bob@example.com>",
            "Carol <carol@example.com>",
        ])
        assert greeting == "Hello All,"

    def test_uses_first_name_only(self):
        greeting = _build_greeting(["Dr. Alice Smith <alice@example.com>"])
        # Should use just the first token of the display name
        assert "Dr." in greeting or "Alice" in greeting


# ── TestBuildAddressingLine ───────────────────────────────────────────────────

class TestBuildAddressingLine:

    def test_chris(self):
        line = _build_addressing_line("Chris")
        assert line == "Chris' availability during the requested period is:"

    def test_christopher(self):
        line = _build_addressing_line("Christopher")
        assert line == "Chris' availability during the requested period is:"

    def test_dr_degroot_dot(self):
        line = _build_addressing_line("Dr. DeGroot")
        assert line == "Dr. DeGroot's availability during the requested period is:"

    def test_prof_degroot_dot(self):
        line = _build_addressing_line("Prof. DeGroot")
        assert line == "Dr. DeGroot's availability during the requested period is:"

    def test_professor_degroot(self):
        line = _build_addressing_line("Professor DeGroot")
        assert line == "Dr. DeGroot's availability during the requested period is:"

    def test_dr_no_dot(self):
        line = _build_addressing_line("Dr DeGroot")
        assert line == "Dr. DeGroot's availability during the requested period is:"

    def test_unknown_defaults_to_chris(self):
        line = _build_addressing_line("Hello")
        assert line == "Chris' availability during the requested period is:"

    def test_empty_defaults_to_chris(self):
        line = _build_addressing_line("")
        assert line == "Chris' availability during the requested period is:"


# ── TestBuildReply ────────────────────────────────────────────────────────────

class TestBuildReply:

    def _reply(self, greeting="Hello Alice,", addressing="Chris", avail="- Mon: free all day"):
        return _build_reply(greeting, _build_addressing_line(addressing), avail)

    def test_contains_greeting(self):
        r = self._reply(greeting="Hello Alice,")
        assert r.startswith("Hello Alice,")

    def test_contains_addressing_line(self):
        r = self._reply(addressing="Chris")
        assert "Chris' availability during the requested period is:" in r

    def test_contains_availability(self):
        avail = "- Monday: 9:00 AM–12:00 PM"
        r = self._reply(avail=avail)
        assert avail in r

    def test_contains_cheers(self):
        r = self._reply()
        assert "Cheers," in r

    def test_contains_digital_assistant(self):
        r = self._reply()
        assert "Chris DeGroot's Digital Assistant" in r

    def test_contains_disclaimer(self):
        r = self._reply()
        assert "(Note: this assistant was built by Chris DeGroot and uses Claude AI)" in r

    def test_sections_in_order(self):
        r = self._reply(
            greeting="Hello Alice,",
            addressing="Chris",
            avail="- Monday: free",
        )
        pos_greeting = r.index("Hello Alice,")
        pos_avail = r.index("Chris' availability")
        pos_free = r.index("- Monday: free")
        pos_cheers = r.index("Cheers,")
        assert pos_greeting < pos_avail < pos_free < pos_cheers


# ── TestSendReply ─────────────────────────────────────────────────────────────

class TestSendReply:

    def test_smtp_called_with_correct_args(self):
        msg = _make_msg(
            from_="Alice <alice@example.com>",
            to=SCHEDULING_ALIAS,
            subject="Meeting",
            msg_id="<orig-id@mail.com>",
        )
        reply_body = "Hello Alice,\n\nChris' availability…\n\nCheers,\n…"

        with patch("email_to_motion.gmail_handler.smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            _send_reply(msg, reply_body, [])

        mock_smtp_cls.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with(BOT_USER, "fake-app-password")
        # sendmail was called
        assert mock_server.sendmail.called
        call_args = mock_server.sendmail.call_args
        assert call_args[0][0] == BOT_USER  # from

    def test_subject_prefixed_with_re(self):
        msg = _make_msg(subject="Meeting request", msg_id="<x@y.com>")
        sent_kwargs = {}

        def capture_sendmail(from_, to_addrs, msg_str):
            sent_kwargs["msg_str"] = msg_str

        with patch("email_to_motion.gmail_handler.smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_server.sendmail.side_effect = capture_sendmail
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            _send_reply(msg, "body", [])

        assert "Re: Meeting request" in sent_kwargs.get("msg_str", "")

    def test_in_reply_to_header_set(self):
        msg = _make_msg(msg_id="<orig@mail.com>")
        sent_kwargs = {}

        def capture_sendmail(from_, to_addrs, msg_str):
            sent_kwargs["msg_str"] = msg_str

        with patch("email_to_motion.gmail_handler.smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_server.sendmail.side_effect = capture_sendmail
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            _send_reply(msg, "body", [])

        assert "In-Reply-To: <orig@mail.com>" in sent_kwargs.get("msg_str", "")

    def test_no_re_prefix_if_already_present(self):
        msg = _make_msg(subject="Re: Old topic", msg_id="<x@y.com>")
        sent_kwargs = {}

        def capture_sendmail(from_, to_addrs, msg_str):
            sent_kwargs["msg_str"] = msg_str

        with patch("email_to_motion.gmail_handler.smtplib.SMTP") as mock_smtp_cls:
            mock_server = MagicMock()
            mock_server.sendmail.side_effect = capture_sendmail
            mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            _send_reply(msg, "body", [])

        # Should not double-prepend "Re:"
        raw = sent_kwargs.get("msg_str", "")
        assert "Re: Re:" not in raw


# ── TestParseSchedulingParams ─────────────────────────────────────────────────

class TestParseSchedulingParams:

    def _mock_claude_response(self, json_str: str):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=json_str)]
        return mock_resp

    def test_explicit_duration_extracted(self):
        payload = json.dumps({
            "duration_minutes": 60,
            "start_date": "2027-04-01",
            "end_date": "2027-04-07",
            "addressing": "Dr. DeGroot",
            "their_availability": None,
        })
        with patch("email_to_motion.gmail_handler.call_with_retries") as mock_call:
            mock_call.return_value = self._mock_claude_response(payload)
            params = _parse_scheduling_params("Please find me a 1-hour slot next week.")

        assert params["duration_minutes"] == 60

    def test_default_duration_30_min(self):
        payload = json.dumps({
            "duration_minutes": 30,
            "start_date": "2027-04-01",
            "end_date": "2027-04-07",
            "addressing": "Chris",
            "their_availability": None,
        })
        with patch("email_to_motion.gmail_handler.call_with_retries") as mock_call:
            mock_call.return_value = self._mock_claude_response(payload)
            params = _parse_scheduling_params("Please find some availability.")

        assert params["duration_minutes"] == 30

    def test_their_availability_extracted(self):
        avail_text = "I am free Monday and Wednesday afternoons."
        payload = json.dumps({
            "duration_minutes": 30,
            "start_date": "2027-04-01",
            "end_date": "2027-04-07",
            "addressing": "Chris",
            "their_availability": avail_text,
        })
        with patch("email_to_motion.gmail_handler.call_with_retries") as mock_call:
            mock_call.return_value = self._mock_claude_response(payload)
            params = _parse_scheduling_params("...")

        assert params["their_availability"] == avail_text

    def test_empty_their_availability_becomes_none(self):
        payload = json.dumps({
            "duration_minutes": 30,
            "start_date": "2027-04-01",
            "end_date": "2027-04-07",
            "addressing": "Chris",
            "their_availability": "",
        })
        with patch("email_to_motion.gmail_handler.call_with_retries") as mock_call:
            mock_call.return_value = self._mock_claude_response(payload)
            params = _parse_scheduling_params("...")

        assert params["their_availability"] is None

    def test_malformed_json_falls_back_to_defaults(self):
        with patch("email_to_motion.gmail_handler.call_with_retries") as mock_call:
            mock_call.return_value = self._mock_claude_response("not valid json {{")
            params = _parse_scheduling_params("...")

        assert params["duration_minutes"] == 30
        assert isinstance(params["start_date"], date)
        assert isinstance(params["end_date"], date)
        assert params["end_date"] >= params["start_date"]

    def test_end_before_start_fixed(self):
        payload = json.dumps({
            "duration_minutes": 30,
            "start_date": "2027-04-10",
            "end_date": "2027-04-05",   # end before start — invalid
            "addressing": "Chris",
            "their_availability": None,
        })
        with patch("email_to_motion.gmail_handler.call_with_retries") as mock_call:
            mock_call.return_value = self._mock_claude_response(payload)
            params = _parse_scheduling_params("...")

        assert params["end_date"] >= params["start_date"]

    def test_addressing_dr_degroot(self):
        payload = json.dumps({
            "duration_minutes": 30,
            "start_date": "2027-04-01",
            "end_date": "2027-04-07",
            "addressing": "Dr. DeGroot",
            "their_availability": None,
        })
        with patch("email_to_motion.gmail_handler.call_with_retries") as mock_call:
            mock_call.return_value = self._mock_claude_response(payload)
            params = _parse_scheduling_params("...")

        assert params["addressing"] == "Dr. DeGroot"


# ── TestHandleScheduling ──────────────────────────────────────────────────────

TORONTO_TZ = pytz.timezone("America/Toronto")
TEST_DATE = date(2027, 6, 14)  # Monday


def _make_busy_blocks():
    """A simple busy-blocks dict with one free day."""
    ws = TORONTO_TZ.localize(datetime.combine(TEST_DATE, time(9, 0)))
    we = TORONTO_TZ.localize(datetime.combine(TEST_DATE, time(16, 30)))
    meeting_start = TORONTO_TZ.localize(datetime.combine(TEST_DATE, time(11, 0)))
    meeting_end = TORONTO_TZ.localize(datetime.combine(TEST_DATE, time(12, 0)))
    return {TEST_DATE: [(meeting_start, meeting_end)]}


class TestHandleScheduling:

    def _make_scheduling_msg(self, body="Hi Chris, can we meet next week?"):
        return _make_msg(
            from_="Alice <alice@example.com>",
            to=SCHEDULING_ALIAS,
            subject="Meeting request",
            body=body,
        )

    def _mock_params(self, **overrides):
        defaults = {
            "duration_minutes": 30,
            "start_date": TEST_DATE,
            "end_date": TEST_DATE + timedelta(days=4),
            "addressing": "Chris",
            "their_availability": None,
        }
        defaults.update(overrides)
        return defaults

    @patch("email_to_motion.gmail_handler.activity_log.record")
    @patch("email_to_motion.gmail_handler.smtplib.SMTP")
    @patch("email_to_motion.gmail_handler._ask_claude")
    @patch("email_to_motion.gmail_handler.fetch_busy_blocks")
    @patch("email_to_motion.gmail_handler._parse_scheduling_params")
    def test_standard_pipeline(
        self, mock_params, mock_busy, mock_claude, mock_smtp_cls, mock_log
    ):
        mock_params.return_value = self._mock_params()
        mock_busy.return_value = _make_busy_blocks()
        mock_claude.return_value = "- Monday: 9:00 AM–11:00 AM, 12:00 PM–4:30 PM"

        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        msg = self._make_scheduling_msg()
        others = ["Alice <alice@example.com>"]

        from email_to_motion.gmail_handler import _handle_scheduling
        _handle_scheduling(msg, "Hi Chris, can we meet?", others)

        mock_busy.assert_called_once()
        mock_claude.assert_called_once()
        mock_server.sendmail.assert_called_once()
        mock_log.assert_called_once()

    @patch("email_to_motion.gmail_handler.activity_log.record")
    @patch("email_to_motion.gmail_handler.smtplib.SMTP")
    @patch("email_to_motion.gmail_handler._ask_claude_match")
    @patch("email_to_motion.gmail_handler._ask_claude")
    @patch("email_to_motion.gmail_handler.fetch_busy_blocks")
    @patch("email_to_motion.gmail_handler._parse_scheduling_params")
    def test_match_mode_used_when_their_availability_present(
        self, mock_params, mock_busy, mock_claude, mock_match, mock_smtp_cls, mock_log
    ):
        mock_params.return_value = self._mock_params(
            their_availability="I'm free Mon and Wed mornings."
        )
        mock_busy.return_value = _make_busy_blocks()
        mock_match.return_value = "- Monday: 9:00 AM–11:00 AM (mutual)"

        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        from email_to_motion.gmail_handler import _handle_scheduling
        _handle_scheduling(self._make_scheduling_msg(), "...", ["Alice <alice@example.com>"])

        mock_match.assert_called_once()
        mock_claude.assert_not_called()

    @patch("email_to_motion.gmail_handler.fetch_busy_blocks")
    @patch("email_to_motion.gmail_handler._parse_scheduling_params")
    def test_calendar_fetch_error_logs_and_returns(self, mock_params, mock_busy):
        mock_params.return_value = self._mock_params()
        mock_busy.side_effect = Exception("ICS fetch failed")

        from email_to_motion.gmail_handler import _handle_scheduling
        # Should not raise
        _handle_scheduling(self._make_scheduling_msg(), "...", [])

    @patch("email_to_motion.gmail_handler.activity_log.record")
    @patch("email_to_motion.gmail_handler.smtplib.SMTP")
    @patch("email_to_motion.gmail_handler._ask_claude")
    @patch("email_to_motion.gmail_handler.fetch_busy_blocks")
    @patch("email_to_motion.gmail_handler._parse_scheduling_params")
    def test_dr_degroot_addressing_in_reply(
        self, mock_params, mock_busy, mock_claude, mock_smtp_cls, mock_log
    ):
        mock_params.return_value = self._mock_params(addressing="Dr. DeGroot")
        mock_busy.return_value = {}
        mock_claude.return_value = "I am available during the following times: none."

        mock_server = MagicMock()
        sent_body = {}

        def capture_sendmail(from_, to_addrs, msg_str):
            sent_body["msg"] = msg_str

        mock_server.sendmail.side_effect = capture_sendmail
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        from email_to_motion.gmail_handler import _handle_scheduling
        _handle_scheduling(self._make_scheduling_msg(), "...", ["Alice <alice@example.com>"])

        # The MIME message body may be base64-encoded; parse it to get the plain text
        raw_msg_str = sent_body.get("msg", "")
        parsed = email.message_from_string(raw_msg_str)
        body_text = parsed.get_payload(decode=True)
        if isinstance(body_text, bytes):
            body_text = body_text.decode("utf-8", errors="replace")
        else:
            body_text = raw_msg_str
        assert "Dr. DeGroot's availability" in body_text


# ── TestStateFile ─────────────────────────────────────────────────────────────

class TestStateFile:

    def test_load_missing_file_returns_empty_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "email_to_motion.gmail_handler._STATE_FILE",
            tmp_path / "nonexistent.json",
        )
        result = _load_processed()
        assert result == set()

    def test_round_trip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "gmail_processed.json"
        monkeypatch.setattr("email_to_motion.gmail_handler._STATE_FILE", state_file)

        ids = {"<a@b.com>", "<c@d.com>"}
        _save_processed(ids)
        loaded = _load_processed()
        assert loaded == ids

    def test_cap_at_max_seen(self, tmp_path, monkeypatch):
        state_file = tmp_path / "gmail_processed.json"
        monkeypatch.setattr("email_to_motion.gmail_handler._STATE_FILE", state_file)
        monkeypatch.setattr("email_to_motion.gmail_handler._MAX_SEEN", 5)

        ids = {f"<msg-{i}@x.com>" for i in range(20)}
        _save_processed(ids)

        with state_file.open() as f:
            data = json.load(f)
        assert len(data["processed"]) == 5

    def test_corrupted_file_returns_empty_set(self, tmp_path, monkeypatch):
        state_file = tmp_path / "gmail_processed.json"
        state_file.write_text("not json {{{{")
        monkeypatch.setattr("email_to_motion.gmail_handler._STATE_FILE", state_file)
        result = _load_processed()
        assert result == set()


# ── TestPollAndProcess ────────────────────────────────────────────────────────

def _make_raw_email_bytes(
    from_: str = "Alice <alice@example.com>",
    to: str = SCHEDULING_ALIAS,
    subject: str = "Meeting",
    body: str = "Hi Chris,\n\nCan we meet?",
    msg_id: str = "<test-001@mail.com>",
) -> bytes:
    """Build a minimal RFC 822 email as bytes for IMAP mock."""
    lines = [
        f"From: {from_}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Message-ID: {msg_id}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    return "\r\n".join(lines).encode("utf-8")


class TestPollAndProcess:

    def _make_imap(self, msg_nums: list[bytes], raw_msgs: dict[bytes, bytes]):
        """Return a mock IMAP4_SSL context that simulates inbox messages."""
        mock_imap = MagicMock()
        mock_imap.search.return_value = ("OK", [b" ".join(msg_nums)] if msg_nums else [b""])

        def fetch_side_effect(num, spec):
            raw = raw_msgs.get(num, b"")
            return ("OK", [(b"1 (RFC822 {%d})" % len(raw), raw)])

        mock_imap.fetch.side_effect = fetch_side_effect
        mock_imap.store.return_value = ("OK", [])
        mock_imap.logout.return_value = ("BYE", [])
        return mock_imap

    @patch("email_to_motion.gmail_handler._save_processed")
    @patch("email_to_motion.gmail_handler._load_processed")
    @patch("email_to_motion.gmail_handler.imaplib.IMAP4_SSL")
    def test_no_unseen_messages(self, mock_imap_cls, mock_load, mock_save):
        mock_imap = self._make_imap([], {})
        mock_imap_cls.return_value = mock_imap
        mock_load.return_value = set()

        count = poll_and_process()

        assert count == 0
        mock_save.assert_not_called()

    @patch("email_to_motion.gmail_handler._save_processed")
    @patch("email_to_motion.gmail_handler._load_processed")
    @patch("email_to_motion.gmail_handler.imaplib.IMAP4_SSL")
    def test_already_processed_skipped(self, mock_imap_cls, mock_load, mock_save):
        raw = _make_raw_email_bytes(msg_id="<already@seen.com>")
        mock_imap = self._make_imap([b"1"], {b"1": raw})
        mock_imap_cls.return_value = mock_imap
        mock_load.return_value = {"<already@seen.com>"}

        with patch("email_to_motion.gmail_handler._process_message") as mock_proc:
            count = poll_and_process()

        assert count == 0
        mock_proc.assert_not_called()

    @patch("email_to_motion.gmail_handler._save_processed")
    @patch("email_to_motion.gmail_handler._load_processed")
    @patch("email_to_motion.gmail_handler.imaplib.IMAP4_SSL")
    def test_no_bot_alias_skipped(self, mock_imap_cls, mock_load, mock_save):
        raw = _make_raw_email_bytes(
            to="someone.else@example.com",
            msg_id="<no-alias@mail.com>",
        )
        mock_imap = self._make_imap([b"1"], {b"1": raw})
        mock_imap_cls.return_value = mock_imap
        mock_load.return_value = set()

        with patch("email_to_motion.gmail_handler._process_message") as mock_proc:
            count = poll_and_process()

        assert count == 0
        mock_proc.assert_not_called()
        mock_save.assert_called_once()   # still saves to state to avoid rechecking

    @patch("email_to_motion.gmail_handler._save_processed")
    @patch("email_to_motion.gmail_handler._load_processed")
    @patch("email_to_motion.gmail_handler.imaplib.IMAP4_SSL")
    def test_new_message_processed_and_state_saved(self, mock_imap_cls, mock_load, mock_save):
        raw = _make_raw_email_bytes(
            to=SCHEDULING_ALIAS,
            msg_id="<new-msg@mail.com>",
        )
        mock_imap = self._make_imap([b"1"], {b"1": raw})
        mock_imap_cls.return_value = mock_imap
        mock_load.return_value = set()

        with patch("email_to_motion.gmail_handler._process_message") as mock_proc:
            count = poll_and_process()

        assert count == 1
        mock_proc.assert_called_once()
        mock_save.assert_called_once()
        saved_set = mock_save.call_args[0][0]
        assert "<new-msg@mail.com>" in saved_set

    @patch("email_to_motion.gmail_handler._load_processed")
    @patch("email_to_motion.gmail_handler.imaplib.IMAP4_SSL")
    def test_imap_connection_error_logged_not_raised(self, mock_imap_cls, mock_load):
        mock_imap_cls.side_effect = OSError("Connection refused")
        mock_load.return_value = set()

        # Should not raise
        count = poll_and_process()
        assert count == 0

    @patch("email_to_motion.gmail_handler._save_processed")
    @patch("email_to_motion.gmail_handler._load_processed")
    @patch("email_to_motion.gmail_handler.imaplib.IMAP4_SSL")
    def test_process_message_exception_continues(self, mock_imap_cls, mock_load, mock_save):
        """An exception in _process_message should not crash the poll loop."""
        raw = _make_raw_email_bytes(
            to=SCHEDULING_ALIAS,
            msg_id="<bad-msg@mail.com>",
        )
        mock_imap = self._make_imap([b"1"], {b"1": raw})
        mock_imap_cls.return_value = mock_imap
        mock_load.return_value = set()

        with patch("email_to_motion.gmail_handler._process_message") as mock_proc:
            mock_proc.side_effect = RuntimeError("unexpected!")
            count = poll_and_process()

        # count is 0 because processing raised, but poll_and_process shouldn't crash
        assert count == 0

    @patch("email_to_motion.gmail_handler._save_processed")
    @patch("email_to_motion.gmail_handler._load_processed")
    @patch("email_to_motion.gmail_handler.imaplib.IMAP4_SSL")
    def test_skips_when_no_smtp_credentials(self, mock_imap_cls, mock_load, mock_save, monkeypatch):
        monkeypatch.setattr(config, "SMTP_USER", "")
        count = poll_and_process()
        assert count == 0
        mock_imap_cls.assert_not_called()
