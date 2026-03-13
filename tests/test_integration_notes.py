"""
Integration tests for the notes pipeline.

These tests exercise the full path from a Slack event dict through
process_message() to a written .md file, with all external I/O replaced
by mocks or stubs:

  - config.claude  → stubbed responses (no real API calls)
  - config.slack   → MagicMock (captures postMessage / reactions_add calls)
  - requests.get   → stubbed (for Slack file downloads)
  - filesystem     → tmp_path pytest fixture

The goal is to catch wiring bugs — wrong field names, wrong call order,
missing data flowing between stages — that unit tests can't see.
"""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import anthropic
import pytest

import email_to_motion.slack_notes_handler as snh
import email_to_motion.note_generator as ng
from email_to_motion import config


# ── Constants ─────────────────────────────────────────────────────────────────

_CHANNEL = "C_NOTES_TEST"
_TS      = "1741111111.000001"

_BARE_NOTE = """\
---
date: 2026-03-12 10:00
from: Alice <alice@example.com>
subject: Test Subject
tags: [testing]
attachments: []
---

## Summary
A test email about testing.

## Key Points
- Point one
- Point two
"""

_FENCED_NOTE = f"```markdown\n{_BARE_NOTE}\n```"

_NOTE_WITH_ATTACHMENT = """\
---
date: 2026-03-12 10:00
from: Alice <alice@example.com>
subject: Test Subject
tags: [testing]
attachments: [report.pdf]
---

## Summary
Email with a PDF.

## report.pdf
- Section one detail
- Section two detail
"""


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_dedup():
    """Reset the in-flight dedup set before and after every test."""
    snh._processing_ts.clear()
    yield
    snh._processing_ts.clear()


@pytest.fixture()
def notes_env(tmp_path, monkeypatch):
    """
    Patch all external dependencies for one test.

    Returns a dict with handles to the mocks so individual tests can
    inspect calls or override return values.
    """
    # Claude stub — returns _BARE_NOTE by default
    claude_resp = MagicMock()
    claude_resp.content = [MagicMock(text=_BARE_NOTE)]
    claude_client = MagicMock()
    claude_client.messages.create.return_value = claude_resp
    monkeypatch.setattr(config, "claude", claude_client)

    # Slack stub
    slack_client = MagicMock()
    monkeypatch.setattr(config, "slack", slack_client)

    # Filesystem
    monkeypatch.setattr(config, "NOTES_OUTPUT_PATH", str(tmp_path))

    # Feature flags
    monkeypatch.setattr(config, "OBSIDIAN_DELIVERY", "local")
    monkeypatch.setattr(config, "OWN_BOT_ID",        "BOT_OWN")
    monkeypatch.setattr(config, "SLACK_BOT_TOKEN",    "xoxb-test")
    monkeypatch.setattr(config, "SLACK_NOTES_CHANNEL","notes-inbox")

    return {
        "tmp_path":    tmp_path,
        "claude":      claude_client,
        "claude_resp": claude_resp,
        "slack":       slack_client,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _email_event(
    subject="Test Subject",
    sender="Alice <alice@example.com>",
    body="Email body here.",
    ts=_TS,
    channel=_CHANNEL,
    email_attachments=None,
):
    """Build a minimal Slack event dict containing a filetype='email' file."""
    email_file: dict = {
        "filetype":   "email",
        "subject":    subject,
        "from":       [{"original": sender}],
        "plain_text": body,
    }
    if email_attachments:
        email_file["attachments"] = email_attachments
    return {
        "type":    "message",
        "ts":      ts,
        "channel": channel,
        "files":   [email_file],
    }


def _written_files(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("*.md"))


# ── Happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:

    def test_note_file_is_created(self, notes_env):
        snh.process_message(_email_event())
        files = _written_files(notes_env["tmp_path"])
        assert len(files) == 1, f"expected 1 note, found {files}"

    def test_note_file_contains_claude_response(self, notes_env):
        snh.process_message(_email_event())
        content = _written_files(notes_env["tmp_path"])[0].read_text()
        assert "## Summary" in content
        assert "## Key Points" in content

    def test_note_file_has_valid_frontmatter(self, notes_env):
        snh.process_message(_email_event())
        content = _written_files(notes_env["tmp_path"])[0].read_text()
        assert content.startswith("---\n")
        assert "\n---\n" in content[3:]   # closing delimiter

    def test_slack_confirmation_sent(self, notes_env):
        snh.process_message(_email_event())
        notes_env["slack"].chat_postMessage.assert_called_once()
        call_kwargs = notes_env["slack"].chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == _CHANNEL
        assert "📝" in call_kwargs["text"]

    def test_slack_confirmation_mentions_filename(self, notes_env):
        snh.process_message(_email_event(subject="Budget Review"))
        text = notes_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "Budget Review" in text

    def test_message_marked_processed(self, notes_env):
        snh.process_message(_email_event())
        notes_env["slack"].reactions_add.assert_called_once()
        call_kwargs = notes_env["slack"].reactions_add.call_args.kwargs
        assert call_kwargs["channel"] == _CHANNEL
        assert call_kwargs["timestamp"] == _TS

    def test_claude_called_with_email_body(self, notes_env):
        snh.process_message(_email_event(body="Unique body content XYZ"))
        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Unique body content XYZ" in prompt

    def test_claude_called_with_sender(self, notes_env):
        snh.process_message(_email_event(sender="Bob <bob@example.com>"))
        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Bob" in prompt

    def test_subject_appears_in_filename(self, notes_env):
        snh.process_message(_email_event(subject="Council Minutes"))
        filename = _written_files(notes_env["tmp_path"])[0].name
        assert "Council Minutes" in filename

    def test_filename_ends_with_md(self, notes_env):
        snh.process_message(_email_event())
        assert _written_files(notes_env["tmp_path"])[0].suffix == ".md"


# ── Guard conditions ──────────────────────────────────────────────────────────

class TestGuards:

    def test_own_bot_message_ignored(self, notes_env):
        event = {**_email_event(), "bot_id": "BOT_OWN"}
        snh.process_message(event)
        assert _written_files(notes_env["tmp_path"]) == []
        notes_env["slack"].chat_postMessage.assert_not_called()

    def test_message_changed_subtype_ignored(self, notes_env):
        event = {**_email_event(), "subtype": "message_changed"}
        snh.process_message(event)
        assert _written_files(notes_env["tmp_path"]) == []

    def test_channel_join_subtype_ignored(self, notes_env):
        event = {**_email_event(), "subtype": "channel_join"}
        snh.process_message(event)
        assert _written_files(notes_env["tmp_path"]) == []

    def test_no_files_falls_back_to_event_text(self, notes_env):
        """If no email file is present, the raw event.text is used as body."""
        event = {"type": "message", "ts": _TS, "channel": _CHANNEL,
                 "text": "Fallback text content"}
        snh.process_message(event)
        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Fallback text content" in prompt


# ── Deduplication ─────────────────────────────────────────────────────────────

class TestDeduplication:

    def test_same_ts_processed_only_once(self, notes_env):
        """
        The _processing_ts guard prevents CONCURRENT duplicates (e.g. socket thread
        and startup sweep both picking up the same message at the same time).
        We simulate that by manually pre-populating the set — exactly what the
        socket thread does before it starts working — then verifying the second
        call is skipped.
        """
        snh._processing_ts.add(_TS)        # socket thread is "currently processing" this ts
        snh.process_message(_email_event()) # concurrent attempt → should be blocked
        assert _written_files(notes_env["tmp_path"]) == []
        notes_env["claude"].messages.create.assert_not_called()

    def test_different_ts_both_processed(self, notes_env):
        snh.process_message(_email_event(ts="1111111111.000001"))
        snh.process_message(_email_event(ts="2222222222.000002"))
        assert len(_written_files(notes_env["tmp_path"])) == 2

    def test_ts_removed_from_set_after_success(self, notes_env):
        snh.process_message(_email_event())
        assert _TS not in snh._processing_ts

    def test_ts_removed_from_set_after_failure(self, notes_env):
        """Even if Claude raises, the ts must be cleared so retries are possible."""
        notes_env["claude"].messages.create.side_effect = RuntimeError("API down")
        snh.process_message(_email_event())
        assert _TS not in snh._processing_ts

    def test_in_flight_ts_blocks_sweep(self, notes_env, monkeypatch):
        """process_unprocessed_notes skips messages that are already in _processing_ts."""
        snh._processing_ts.add(_TS)
        sweep_msg = {**_email_event(), "reactions": []}   # no ✅ → would normally be processed

        monkeypatch.setattr(
            "email_to_motion.slack_notes_handler.get_unprocessed_messages",
            lambda _ch: [sweep_msg],
        )
        snh._channel_id = _CHANNEL
        snh.process_unprocessed_notes()

        notes_env["claude"].messages.create.assert_not_called()


# ── Fenced output sanitisation ────────────────────────────────────────────────

class TestFenceSanitisation:

    def test_fenced_markdown_response_produces_valid_frontmatter(self, notes_env):
        """Claude sometimes returns ```markdown fences — they must be stripped."""
        notes_env["claude_resp"].content = [MagicMock(text=_FENCED_NOTE)]
        snh.process_message(_email_event())
        content = _written_files(notes_env["tmp_path"])[0].read_text()
        assert content.startswith("---\n"), \
            "Frontmatter should start with ---; got: " + repr(content[:40])
        assert not content.startswith("```"), \
            "Code fence should have been stripped"

    def test_fenced_response_note_is_complete(self, notes_env):
        notes_env["claude_resp"].content = [MagicMock(text=_FENCED_NOTE)]
        snh.process_message(_email_event())
        content = _written_files(notes_env["tmp_path"])[0].read_text()
        assert "## Summary" in content
        assert "## Key Points" in content


# ── Attachment pipeline ───────────────────────────────────────────────────────

class TestAttachmentPipeline:

    def test_pdf_attachment_name_in_frontmatter(self, notes_env):
        """Attachment filename must reach Claude's prompt."""
        notes_env["claude_resp"].content = [MagicMock(text=_NOTE_WITH_ATTACHMENT)]
        att = {"filename": "report.pdf", "mimetype": "application/pdf",
               "url": "https://files-origin.slack.com/files-email-priv/xxx/report.pdf"}
        event = _email_event(email_attachments=[att])
        with patch("email_to_motion.slack_notes_handler.requests.get") as mock_get, \
             patch.dict("email_to_motion.slack_notes_handler._BY_MIME",
                        {"application/pdf": lambda _b: "PDF extracted text content"}):
            mock_get.return_value = MagicMock(content=b"%PDF fake", status_code=200)
            mock_get.return_value.raise_for_status = lambda: None
            snh.process_message(event)

        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "report.pdf" in prompt

    def test_pdf_text_reaches_claude_prompt(self, notes_env):
        """Extracted PDF text must be included in the Claude prompt.

        Note: _BY_MIME stores a direct function reference captured at import
        time, so patching the module-level name _extract_pdf has no effect on
        the dict lookup.  We patch _BY_MIME itself instead.
        """
        att = {"filename": "report.pdf", "mimetype": "application/pdf",
               "url": "https://files-origin.slack.com/files-email-priv/xxx/report.pdf"}
        event = _email_event(email_attachments=[att])
        fake_extractor = lambda _bytes: "Unique extracted text ABCXYZ"
        with patch("email_to_motion.slack_notes_handler.requests.get") as mock_get, \
             patch.dict("email_to_motion.slack_notes_handler._BY_MIME",
                        {"application/pdf": fake_extractor}):
            mock_get.return_value = MagicMock(content=b"%PDF fake", status_code=200)
            mock_get.return_value.raise_for_status = lambda: None
            snh.process_message(event)

        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Unique extracted text ABCXYZ" in prompt

    def test_download_failure_still_creates_note(self, notes_env):
        """A failed attachment download must not abort the whole pipeline."""
        att = {"filename": "report.pdf", "mimetype": "application/pdf",
               "url": "https://files-origin.slack.com/files-email-priv/xxx/report.pdf"}
        event = _email_event(email_attachments=[att])
        with patch("email_to_motion.slack_notes_handler.requests.get",
                   side_effect=ConnectionError("network error")):
            snh.process_message(event)

        assert len(_written_files(notes_env["tmp_path"])) == 1

    def test_download_failure_note_lists_attachment(self, notes_env):
        """Even when download fails, the attachment filename must appear in the prompt."""
        att = {"filename": "broken.pdf", "mimetype": "application/pdf",
               "url": "https://files-origin.slack.com/broken.pdf"}
        event = _email_event(email_attachments=[att])
        with patch("email_to_motion.slack_notes_handler.requests.get",
                   side_effect=ConnectionError("network error")):
            snh.process_message(event)

        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "broken.pdf" in prompt

    def test_slack_confirmation_lists_attachment(self, notes_env):
        """The Slack confirmation message must mention the attachment."""
        att = {"filename": "report.pdf", "mimetype": "application/pdf", "url": ""}
        event = _email_event(email_attachments=[att])
        snh.process_message(event)
        text = notes_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "report.pdf" in text


# ── Forwarded email handling ──────────────────────────────────────────────────

class TestForwardedEmail:

    def test_fwd_prefix_stripped_from_filename(self, notes_env):
        event = _email_event(subject="Fwd: Budget Review 2026")
        snh.process_message(event)
        filename = _written_files(notes_env["tmp_path"])[0].name
        assert filename.lower().count("fwd") == 0, \
            f"'Fwd:' should be stripped from filename, got: {filename}"

    def test_original_sender_passed_to_claude(self, notes_env):
        body = (
            "---------- Forwarded message ---------\n"
            "From: Original Author <orig@example.com>\n"
            "Subject: Budget\n\n"
            "The email body."
        )
        event = _email_event(subject="Fwd: Budget", sender="Forwarder <fw@example.com>",
                             body=body)
        snh.process_message(event)
        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        # Original sender should have replaced the forwarder's address
        assert "orig@example.com" in prompt or "Original Author" in prompt


# ── Filename collision ────────────────────────────────────────────────────────

class TestFilenameCollision:

    def test_second_note_gets_counter_suffix(self, notes_env):
        """Two notes with the same subject on the same day get (2) appended."""
        snh.process_message(_email_event(ts="1111111111.000001"))
        snh.process_message(_email_event(ts="2222222222.000002"))
        filenames = [f.name for f in _written_files(notes_env["tmp_path"])]
        assert any("(2)" in n for n in filenames), \
            f"Expected a file with '(2)' in name, got: {filenames}"
        assert any("(2)" not in n for n in filenames)

    def test_both_files_have_valid_content(self, notes_env):
        snh.process_message(_email_event(ts="1111111111.000001"))
        snh.process_message(_email_event(ts="2222222222.000002"))
        for f in _written_files(notes_env["tmp_path"]):
            assert "## Summary" in f.read_text()


# ── Claude error handling ─────────────────────────────────────────────────────

class TestClaudeErrors:

    def test_claude_error_posts_slack_warning(self, notes_env):
        notes_env["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event())
        text = notes_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "⚠️" in text

    def test_claude_error_does_not_write_file(self, notes_env):
        notes_env["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event())
        assert _written_files(notes_env["tmp_path"]) == []

    def test_claude_error_still_clears_processing_ts(self, notes_env):
        notes_env["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event())
        assert _TS not in snh._processing_ts


# ── Startup sweep integration ─────────────────────────────────────────────────

class TestStartupSweep:

    def test_sweep_processes_unread_messages(self, notes_env, monkeypatch):
        """process_unprocessed_notes calls process_message for each unread message."""
        msg = _email_event(ts="9999999999.000001")

        monkeypatch.setattr(
            "email_to_motion.slack_notes_handler.get_unprocessed_messages",
            lambda _ch: [msg],
        )
        snh._channel_id = _CHANNEL
        snh.process_unprocessed_notes()

        assert len(_written_files(notes_env["tmp_path"])) == 1

    def test_sweep_skips_channel_id_not_set(self, notes_env, monkeypatch):
        """If _channel_id is empty, sweep does nothing."""
        snh._channel_id = ""
        called = []
        monkeypatch.setattr(
            "email_to_motion.slack_notes_handler.get_unprocessed_messages",
            lambda _ch: called.append(True) or [],
        )
        snh.process_unprocessed_notes()
        assert called == []


# ── DM alerting on failure ────────────────────────────────────────────────────

class TestDmAlertOnFailure:

    _OWNER_ID = "U_OWNER_123"

    @pytest.fixture()
    def env_with_owner(self, notes_env, monkeypatch):
        """notes_env extended with ALLOWED_SLACK_USER_ID set."""
        monkeypatch.setattr(config, "ALLOWED_SLACK_USER_ID", self._OWNER_ID)
        return notes_env

    def test_dm_sent_to_owner_on_generate_failure(self, env_with_owner):
        """When note generation fails, a DM must be sent to the bot owner."""
        env_with_owner["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event(subject="Important Meeting"))

        # chat_postMessage is called twice: channel warning + owner DM
        calls = env_with_owner["slack"].chat_postMessage.call_args_list
        dm_calls = [c for c in calls if c.kwargs.get("channel") == self._OWNER_ID]
        assert len(dm_calls) == 1, f"Expected 1 DM to owner, got: {dm_calls}"

    def test_dm_contains_subject(self, env_with_owner):
        env_with_owner["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event(subject="Budget Review Q3"))

        calls = env_with_owner["slack"].chat_postMessage.call_args_list
        dm_text = next(
            c.kwargs["text"] for c in calls
            if c.kwargs.get("channel") == self._OWNER_ID
        )
        assert "Budget Review Q3" in dm_text

    def test_dm_contains_error_type(self, env_with_owner):
        env_with_owner["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event())

        calls = env_with_owner["slack"].chat_postMessage.call_args_list
        dm_text = next(
            c.kwargs["text"] for c in calls
            if c.kwargs.get("channel") == self._OWNER_ID
        )
        assert "RuntimeError" in dm_text

    def test_channel_warning_also_sent(self, env_with_owner):
        """The channel should still get the ⚠️ warning even when DM is sent."""
        env_with_owner["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event())

        calls = env_with_owner["slack"].chat_postMessage.call_args_list
        channel_calls = [c for c in calls if c.kwargs.get("channel") == _CHANNEL]
        assert len(channel_calls) == 1
        assert "⚠️" in channel_calls[0].kwargs["text"]

    def test_no_dm_when_owner_not_configured(self, notes_env, monkeypatch):
        """If ALLOWED_SLACK_USER_ID is unset, only the channel warning is posted."""
        monkeypatch.setattr(config, "ALLOWED_SLACK_USER_ID", "")
        notes_env["claude"].messages.create.side_effect = RuntimeError("Claude down")
        snh.process_message(_email_event())

        # Only one call: the channel warning; no DM
        assert notes_env["slack"].chat_postMessage.call_count == 1

    def test_dm_sent_on_write_failure(self, env_with_owner, monkeypatch):
        """DM is also sent when file writing fails (not just Claude failures)."""
        monkeypatch.setattr(
            "email_to_motion.note_generator.write_note",
            lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
        )
        snh.process_message(_email_event(subject="Write Fail Subject"))

        calls = env_with_owner["slack"].chat_postMessage.call_args_list
        dm_calls = [c for c in calls if c.kwargs.get("channel") == self._OWNER_ID]
        assert len(dm_calls) == 1
        assert "Write Fail Subject" in dm_calls[0].kwargs["text"]

    def test_dm_failure_does_not_suppress_original_error_handling(self, env_with_owner):
        """A crash inside _dm_owner_on_failure must not prevent the channel warning."""
        env_with_owner["slack"].chat_postMessage.side_effect = [
            None,                        # first call: channel warning succeeds
            RuntimeError("DM failed"),   # second call: DM itself fails
        ]
        env_with_owner["claude"].messages.create.side_effect = RuntimeError("Claude down")
        # Should not raise even if the DM fails
        snh.process_message(_email_event())


# ── Retry integration ─────────────────────────────────────────────────────────

class TestRetryIntegration:

    def test_transient_rate_limit_retried_and_note_created(self, notes_env, monkeypatch):
        """A RateLimitError on the first attempt should be retried; note written on second."""
        rate_err = anthropic.RateLimitError("rate limited", response=MagicMock(), body={})
        claude_resp = MagicMock()
        claude_resp.content = [MagicMock(text=_BARE_NOTE)]
        notes_env["claude"].messages.create.side_effect = [rate_err, claude_resp]

        with patch("email_to_motion.utils.time.sleep"):
            snh.process_message(_email_event())

        assert len(_written_files(notes_env["tmp_path"])) == 1

    def test_transient_timeout_retried_and_note_created(self, notes_env, monkeypatch):
        timeout_err = anthropic.APITimeoutError(request=MagicMock())
        claude_resp = MagicMock()
        claude_resp.content = [MagicMock(text=_BARE_NOTE)]
        notes_env["claude"].messages.create.side_effect = [timeout_err, claude_resp]

        with patch("email_to_motion.utils.time.sleep"):
            snh.process_message(_email_event())

        assert len(_written_files(notes_env["tmp_path"])) == 1

    def test_exhausted_retries_posts_channel_warning(self, notes_env, monkeypatch):
        """After all retries are exhausted the channel must receive a ⚠️ warning."""
        rate_err = anthropic.RateLimitError("still rate limited", response=MagicMock(), body={})
        notes_env["claude"].messages.create.side_effect = rate_err

        with patch("email_to_motion.utils.time.sleep"):
            snh.process_message(_email_event())

        text = notes_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "⚠️" in text

    def test_exhausted_retries_no_note_written(self, notes_env, monkeypatch):
        rate_err = anthropic.RateLimitError("still rate limited", response=MagicMock(), body={})
        notes_env["claude"].messages.create.side_effect = rate_err

        with patch("email_to_motion.utils.time.sleep"):
            snh.process_message(_email_event())

        assert _written_files(notes_env["tmp_path"]) == []

    def test_sleep_called_between_retries(self, notes_env, monkeypatch):
        """Exponential back-off sleep must be triggered between retry attempts."""
        rate_err = anthropic.RateLimitError("rate limited", response=MagicMock(), body={})
        notes_env["claude"].messages.create.side_effect = rate_err

        with patch("email_to_motion.utils.time.sleep") as mock_sleep:
            snh.process_message(_email_event())

        # 3 retries → 3 sleeps (1s, 2s, 4s)
        assert mock_sleep.call_count == 3
