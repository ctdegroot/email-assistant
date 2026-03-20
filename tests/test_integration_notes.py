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
        # Use distinct bodies so they are treated as different emails
        snh.process_message(_email_event(ts="1111111111.000001", body="First distinct body"))
        snh.process_message(_email_event(ts="2222222222.000002", body="Second distinct body"))
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
        with patch("email_to_motion.slack_notes_handler.get_with_retries") as mock_get, \
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
        with patch("email_to_motion.slack_notes_handler.get_with_retries") as mock_get, \
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
        with patch("email_to_motion.slack_notes_handler.get_with_retries",
                   side_effect=ConnectionError("network error")):
            snh.process_message(event)

        assert len(_written_files(notes_env["tmp_path"])) == 1

    def test_download_failure_note_lists_attachment(self, notes_env):
        """Even when download fails, the attachment filename must appear in the prompt."""
        att = {"filename": "broken.pdf", "mimetype": "application/pdf",
               "url": "https://files-origin.slack.com/broken.pdf"}
        event = _email_event(email_attachments=[att])
        with patch("email_to_motion.slack_notes_handler.get_with_retries",
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
        """Two genuinely different emails with the same subject get (2) appended."""
        snh.process_message(_email_event(ts="1111111111.000001", body="First email — completely different content"))
        snh.process_message(_email_event(ts="2222222222.000002", body="Second email — completely different content"))
        filenames = [f.name for f in _written_files(notes_env["tmp_path"])]
        assert any("(2)" in n for n in filenames), \
            f"Expected a file with '(2)' in name, got: {filenames}"
        assert any("(2)" not in n for n in filenames)

    def test_both_files_have_valid_content(self, notes_env):
        snh.process_message(_email_event(ts="1111111111.000001", body="First email — completely different content"))
        snh.process_message(_email_event(ts="2222222222.000002", body="Second email — completely different content"))
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


# ── Note deduplication ────────────────────────────────────────────────────────

class TestNoteDeduplication:
    """
    Content-based deduplication: re-forwarding the same email should overwrite
    the existing note rather than creating a (2) duplicate.  Genuinely different
    emails with the same subject must still create separate notes.
    """

    def test_same_email_does_not_create_duplicate_file(self, notes_env):
        """Re-sending the same email (same content) must not create a second file."""
        snh.process_message(_email_event(ts="1111111111.000001"))
        snh.process_message(_email_event(ts="2222222222.000002"))   # same content
        files = _written_files(notes_env["tmp_path"])
        assert len(files) == 1, f"Expected 1 note, got: {[f.name for f in files]}"

    def test_same_email_same_output_is_unchanged(self, notes_env):
        """When Claude produces identical markdown for a re-send, status is 'unchanged'."""
        snh.process_message(_email_event(ts="1111111111.000001"))
        snh.process_message(_email_event(ts="2222222222.000002"))   # same content
        # Second call's confirmation must say "unchanged"
        calls = notes_env["slack"].chat_postMessage.call_args_list
        last_text = calls[-1].kwargs["text"]
        assert "unchanged" in last_text.lower(), f"Expected 'unchanged' in: {last_text!r}"

    def test_same_email_different_output_overwrites(self, notes_env):
        """If Claude generates different markdown for a re-send, the file is updated in place."""
        _UPDATED_NOTE = """\
---
date: 2026-03-12 10:00
from: Alice <alice@example.com>
subject: Test Subject
tags: [testing, updated]
attachments: []
---

## Summary
An improved summary after code update.

## Key Points
- Improved point one
"""
        # First send
        snh.process_message(_email_event(ts="1111111111.000001"))

        # Second send: same email content, but Claude now returns different markdown
        updated_resp = MagicMock()
        updated_resp.content = [MagicMock(text=_UPDATED_NOTE)]
        notes_env["claude"].messages.create.return_value = updated_resp
        snh.process_message(_email_event(ts="2222222222.000002"))

        # Still only one file
        files = _written_files(notes_env["tmp_path"])
        assert len(files) == 1, f"Expected 1 note after overwrite, got: {[f.name for f in files]}"
        # File contains the updated content
        assert "improved" in files[0].read_text().lower()

    def test_same_email_different_output_status_is_updated(self, notes_env):
        """Status label in the Slack message must be 'updated' after an overwrite."""
        _UPDATED_NOTE = """\
---
date: 2026-03-12 10:00
from: Alice <alice@example.com>
subject: Test Subject
tags: [testing]
attachments: []
---

## Summary
Updated summary.
"""
        snh.process_message(_email_event(ts="1111111111.000001"))

        updated_resp = MagicMock()
        updated_resp.content = [MagicMock(text=_UPDATED_NOTE)]
        notes_env["claude"].messages.create.return_value = updated_resp
        snh.process_message(_email_event(ts="2222222222.000002"))

        calls = notes_env["slack"].chat_postMessage.call_args_list
        last_text = calls[-1].kwargs["text"]
        assert "updated" in last_text.lower(), f"Expected 'updated' in: {last_text!r}"

    def test_different_email_same_subject_creates_second_file(self, notes_env):
        """Two genuinely different emails with the same subject → separate notes."""
        snh.process_message(_email_event(ts="1111111111.000001", body="Body of first email"))
        snh.process_message(_email_event(ts="2222222222.000002", body="Body of second email — entirely different"))
        files = _written_files(notes_env["tmp_path"])
        assert len(files) == 2, f"Expected 2 notes for different emails, got: {[f.name for f in files]}"

    def test_new_email_status_is_saved(self, notes_env):
        """A brand-new email must produce status 'saved' in the Slack confirmation."""
        snh.process_message(_email_event())
        text = notes_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "saved" in text.lower(), f"Expected 'saved' in: {text!r}"

    def test_source_hash_stored_in_frontmatter(self, notes_env):
        """The generated note file must contain a source_hash: field in its frontmatter."""
        snh.process_message(_email_event())
        content = _written_files(notes_env["tmp_path"])[0].read_text()
        assert "source_hash:" in content, "source_hash: must be present in frontmatter"

    def test_source_hash_survives_round_trip(self, notes_env):
        """The hash stored in the file must match what compute_source_hash returns."""
        import re
        import email_to_motion.note_generator as ng

        snh.process_message(_email_event(
            subject="Test Subject",
            sender="Alice <alice@example.com>",
            body="Email body here.",
        ))
        content = _written_files(notes_env["tmp_path"])[0].read_text()
        fm = re.search(r'^source_hash:\s*(\S+)', content, re.MULTILINE)
        assert fm, "source_hash: field not found"
        stored = fm.group(1)
        expected = ng.compute_source_hash("Test Subject", "Alice <alice@example.com>",
                                          "Email body here.", [])
        assert stored == expected

    def test_forwarded_email_same_hash_despite_different_wrapper(self, notes_env):
        """
        The same email re-forwarded with a different note or signature must
        produce the same source_hash so dedup kicks in.

        The user's forwarding wrapper (text above the divider, including their
        signature) must be excluded from the hash.
        """
        inner_content = (
            "---------- Forwarded message ---------\n"
            "From: Original Author <orig@example.com>\n"
            "Date: Thu, 12 Mar 2026 09:00:00 -0500\n"
            "Subject: Budget Review Q1\n"
            "To: Chris <chris@example.com>\n"
            "\n"
            "Hi Chris,\n\n"
            "Please see the attached budget figures.\n\n"
            "Thanks, Original"
        )
        # First send: plain forward with no extra note
        body_1 = inner_content
        # Second send: user added "FYI!" at the top and has a different signature
        body_2 = "FYI!\n\nChris de Groot\nSr. Manager\n\n" + inner_content

        snh.process_message(_email_event(
            subject="Fwd: Budget Review Q1", body=body_1, ts="1111111111.000001",
        ))
        snh.process_message(_email_event(
            subject="Fwd: Budget Review Q1", body=body_2, ts="2222222222.000002",
        ))

        files = _written_files(notes_env["tmp_path"])
        assert len(files) == 1, (
            f"Same email re-forwarded with different wrapper must NOT create a "
            f"second file; got: {[f.name for f in files]}"
        )

    def test_different_forwarded_email_different_hash(self, notes_env):
        """Two genuinely different emails forwarded with the same wrapper → 2 files."""
        wrapper = (
            "FYI\n\n"
            "---------- Forwarded message ---------\n"
            "From: Someone <someone@example.com>\n"
            "Date: Thu, 12 Mar 2026 09:00:00 -0500\n"
            "Subject: Test Subject\n"
            "To: Chris <chris@example.com>\n"
            "\n"
        )
        body_1 = wrapper + "First email content — completely unique."
        body_2 = wrapper + "Second email content — entirely different."

        snh.process_message(_email_event(body=body_1, ts="1111111111.000001"))
        snh.process_message(_email_event(body=body_2, ts="2222222222.000002"))

        files = _written_files(notes_env["tmp_path"])
        assert len(files) == 2, (
            f"Different emails must create separate notes; got: {[f.name for f in files]}"
        )

    def test_note_from_previous_day_detected_as_duplicate(self, notes_env, monkeypatch):
        """Same email processed on a later day must still be matched by its hash."""
        import email_to_motion.note_generator as ng

        # Simulate a note written yesterday by injecting a pre-dated file
        output_dir = notes_env["tmp_path"]
        hash_val = ng.compute_source_hash(
            "Test Subject", "Alice <alice@example.com>", "Email body here.", []
        )
        old_content = ng._inject_source_hash(_BARE_NOTE, hash_val)
        old_file = output_dir / "2026-03-11 - Test Subject.md"
        old_file.write_text(old_content, encoding="utf-8")

        # Now process the same email "today"
        snh.process_message(_email_event())

        # Should not have created a new file alongside the old one
        all_files = _written_files(output_dir)
        assert len(all_files) == 1, (
            f"Expected 1 note (old file reused), got: {[f.name for f in all_files]}"
        )


# ── URL note pipeline ─────────────────────────────────────────────────────────

def _url_event(url="https://example.com/article", ts=_TS, channel=_CHANNEL):
    """A minimal Slack event where the user pasted a URL into the channel."""
    return {
        "type":    "message",
        "ts":      ts,
        "channel": channel,
        "text":    url,
        "files":   [],
    }


_FAKE_HTML = """\
<html><head><title>Test Article | Example University</title></head>
<body>
<article>
<h1>Test Article</h1>
<p>This is the main body of the article with enough content for trafilatura.</p>
<p>Second paragraph with more detail about the topic at hand.</p>
</article>
</body></html>
"""

_FAKE_EXTRACTED = "Test Article\n\nThis is the main body of the article with enough content for trafilatura.\n\nSecond paragraph with more detail about the topic at hand."


class TestUrlNotePipeline:
    """
    Tests for the URL-note input mode: user pastes a URL into #notes-inbox
    and the bot fetches the page and generates a note from its content.
    """

    @pytest.fixture()
    def url_env(self, notes_env, monkeypatch):
        """notes_env with trafilatura stubbed out."""
        monkeypatch.setattr(
            "email_to_motion.slack_notes_handler._fetch_url_content",
            lambda url, channel_id: ("Test Article", _FAKE_EXTRACTED),
        )
        return notes_env

    def test_url_message_creates_note(self, url_env):
        snh.process_message(_url_event())
        files = _written_files(url_env["tmp_path"])
        assert len(files) == 1, f"Expected 1 note from URL, got: {[f.name for f in files]}"

    def test_url_title_becomes_subject_in_filename(self, url_env):
        snh.process_message(_url_event())
        filename = _written_files(url_env["tmp_path"])[0].name
        assert "Test Article" in filename

    def test_url_content_reaches_claude(self, url_env):
        snh.process_message(_url_event())
        prompt = url_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert _FAKE_EXTRACTED in prompt

    def test_url_used_as_sender_in_prompt(self, url_env):
        url = "https://example.com/article"
        snh.process_message(_url_event(url=url))
        prompt = url_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert url in prompt

    def test_url_slack_confirmation_sent(self, url_env):
        snh.process_message(_url_event())
        url_env["slack"].chat_postMessage.assert_called()
        text = url_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "📝" in text

    def test_url_message_marked_processed(self, url_env):
        snh.process_message(_url_event())
        url_env["slack"].reactions_add.assert_called_once()

    def test_same_url_repasted_is_unchanged(self, url_env):
        """Re-pasting the same URL should detect the existing note via hash and not duplicate."""
        snh.process_message(_url_event(ts="1111111111.000001"))
        snh.process_message(_url_event(ts="2222222222.000002"))
        files = _written_files(url_env["tmp_path"])
        assert len(files) == 1, f"Same URL re-pasted must not create 2 files: {[f.name for f in files]}"

    def test_url_fetch_failure_posts_error(self, notes_env, monkeypatch):
        """When _fetch_url_content returns None (fetch failed), a ⚠️ is posted."""
        monkeypatch.setattr(
            "email_to_motion.slack_notes_handler._fetch_url_content",
            lambda url, channel_id: None,
        )
        # The function posts the error itself; process_message should bail without writing
        snh.process_message(_url_event())
        assert _written_files(notes_env["tmp_path"]) == []

    def test_url_fetch_failure_does_not_mark_processed(self, notes_env, monkeypatch):
        """A failed URL fetch must not add ✅ — the user should be able to retry."""
        monkeypatch.setattr(
            "email_to_motion.slack_notes_handler._fetch_url_content",
            lambda url, channel_id: None,
        )
        snh.process_message(_url_event())
        notes_env["slack"].reactions_add.assert_not_called()

    def test_non_url_short_message_not_treated_as_url(self, notes_env):
        """A plain-text message (no URL) must go through the regular text path."""
        event = {
            "type": "message", "ts": _TS, "channel": _CHANNEL,
            "text": "Please take a look at this issue.", "files": [],
        }
        snh.process_message(event)
        # Claude should be called (regular text path) and a note written
        notes_env["claude"].messages.create.assert_called_once()

    def test_url_with_short_label_detected(self, notes_env, monkeypatch):
        """A message like 'FYI: https://...' is still a URL note."""
        monkeypatch.setattr(
            "email_to_motion.slack_notes_handler._fetch_url_content",
            lambda url, channel_id: ("Page Title", "Page body content."),
        )
        event = {
            "type": "message", "ts": _TS, "channel": _CHANNEL,
            "text": "FYI: https://example.com/page", "files": [],
        }
        snh.process_message(event)
        files = _written_files(notes_env["tmp_path"])
        assert len(files) == 1

    def test_url_with_long_surrounding_text_not_treated_as_url(self, notes_env):
        """A URL buried in a substantive message should not trigger URL-note mode."""
        event = {
            "type": "message", "ts": _TS, "channel": _CHANNEL,
            "text": (
                "I read this interesting article https://example.com and "
                "thought we should discuss the implications for the project."
            ),
            "files": [],
        }
        snh.process_message(event)
        # URL fetch must NOT have been called — regular text path instead
        # (Claude called with the full message text as body)
        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "interesting article" in prompt


# ── Direct file upload (no email wrapper) ─────────────────────────────────────

class TestDirectFileUpload:
    """
    Tests for Mode 3: user drags a file (e.g. a PDF saved from the browser)
    directly into the notes-inbox channel without forwarding an email.
    """

    def _pdf_event(self, filename="Budget-Q1-2026.pdf", ts=_TS, channel=_CHANNEL):
        return {
            "type":    "message",
            "ts":      ts,
            "channel": channel,
            "text":    "",
            "files": [
                {
                    "name":                 filename,
                    "filetype":             "pdf",
                    "mimetype":             "application/pdf",
                    "url_private_download": "https://files.slack.com/files-pri/xxx/budget.pdf",
                    "url_private":          "https://files.slack.com/files-pri/xxx/budget.pdf",
                }
            ],
        }

    def test_pdf_filename_used_as_subject(self, notes_env):
        """The PDF filename (minus extension) becomes the note subject / filename."""
        with patch("email_to_motion.slack_notes_handler.get_with_retries") as mock_get, \
             patch.dict("email_to_motion.slack_notes_handler._BY_MIME",
                        {"application/pdf": lambda _b: "Extracted PDF text"}):
            mock_get.return_value = MagicMock(content=b"%PDF fake", status_code=200)
            mock_get.return_value.raise_for_status = lambda: None
            snh.process_message(self._pdf_event(filename="Budget-Q1-2026.pdf"))

        filename = _written_files(notes_env["tmp_path"])[0].name
        assert "Budget-Q1-2026" in filename, f"Expected stem in filename, got: {filename}"

    def test_pdf_content_reaches_claude(self, notes_env):
        """Extracted PDF text must appear in the Claude prompt."""
        with patch("email_to_motion.slack_notes_handler.get_with_retries") as mock_get, \
             patch.dict("email_to_motion.slack_notes_handler._BY_MIME",
                        {"application/pdf": lambda _b: "Unique PDF content ZZZQ"}):
            mock_get.return_value = MagicMock(content=b"%PDF fake", status_code=200)
            mock_get.return_value.raise_for_status = lambda: None
            snh.process_message(self._pdf_event())

        prompt = notes_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "Unique PDF content ZZZQ" in prompt

    def test_direct_pdf_creates_note_file(self, notes_env):
        with patch("email_to_motion.slack_notes_handler.get_with_retries") as mock_get, \
             patch.dict("email_to_motion.slack_notes_handler._BY_MIME",
                        {"application/pdf": lambda _b: "PDF text"}):
            mock_get.return_value = MagicMock(content=b"%PDF fake", status_code=200)
            mock_get.return_value.raise_for_status = lambda: None
            snh.process_message(self._pdf_event())

        assert len(_written_files(notes_env["tmp_path"])) == 1
