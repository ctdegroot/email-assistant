"""
Integration tests for the Motion task creation pipeline (tasks.py).

Covers the full path from an unprocessed Slack message through Claude analysis
to a Motion API call and Slack confirmation, with all external I/O replaced
by mocks:

  - config.claude                   → stubbed Claude responses
  - config.slack                    → MagicMock (channel lookup, history, reactions, postMessage)
  - requests.post                   → stubbed Motion REST API
  - tasks._channel_id               → reset between tests
"""

import json
from unittest.mock import MagicMock, patch, call

import pytest
import requests

import email_to_motion.tasks as tasks
from email_to_motion import config


# ── Constants ─────────────────────────────────────────────────────────────────

_CHANNEL_ID   = "C_TASKS_TEST"
_TS           = "1741000000.000001"
_TS2          = "1741000000.000002"

_SINGLE_TASK_JSON = json.dumps([{
    "name":         "Review budget proposal",
    "description":  "Please review the attached Q3 budget proposal.\n\n**Source**: alice@example.com",
    "priority":     "MEDIUM",
    "duration":     60,
    "dueDate":      "2026-03-20",
    "deadlineType": "SOFT",
}])

_TWO_TASK_JSON = json.dumps([
    {
        "name":         "Create grading rubric",
        "description":  "Create a rubric for the midterm exam.",
        "priority":     "HIGH",
        "duration":     45,
        "dueDate":      "2026-03-18",
        "deadlineType": "HARD",
    },
    {
        "name":         "Grade midterm exams",
        "description":  "Grade all midterm submissions.",
        "priority":     "HIGH",
        "duration":     120,
        "dueDate":      "2026-03-22",
        "deadlineType": "SOFT",
    },
])

_MOTION_RESPONSE = {
    "task": {
        "id":   "MOTION_TASK_ID_001",
        "name": "Review budget proposal",
    }
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_channel_id():
    """Reset the module-level channel ID cache before every test."""
    original = tasks._channel_id
    tasks._channel_id = ""
    yield
    tasks._channel_id = original


@pytest.fixture()
def tasks_env(monkeypatch):
    """
    Patch all external dependencies for tasks pipeline tests.

    Returns handles to mocks so individual tests can inspect calls
    or override return values.
    """
    # Claude stub — returns a single task JSON by default
    claude_resp = MagicMock()
    claude_resp.content = [MagicMock(text=_SINGLE_TASK_JSON)]
    claude_client = MagicMock()
    claude_client.messages.create.return_value = claude_resp
    monkeypatch.setattr(config, "claude", claude_client)

    # Slack stub — conversations_list returns our test channel
    slack_client = MagicMock()
    slack_client.conversations_list.return_value = {
        "channels":          [{"id": _CHANNEL_ID, "name": "email-to-motion"}],
        "response_metadata": {"next_cursor": ""},
    }
    slack_client.conversations_history.return_value = {
        "messages": [_plain_msg(_TS, "Please review the budget proposal.")]
    }
    monkeypatch.setattr(config, "slack", slack_client)

    # Config stubs
    monkeypatch.setattr(config, "SLACK_MOTION_CHANNEL_NAME", "email-to-motion")
    monkeypatch.setattr(config, "MOTION_WORKSPACE_ID",       "WS_TEST")
    monkeypatch.setattr(config, "MOTION_ASSIGNEE_ID",        "USER_TEST")
    monkeypatch.setattr(config, "MOTION_API_KEY",            "mk_test_key")
    monkeypatch.setattr(config, "OWN_BOT_ID",                "BOT_OWN")
    monkeypatch.setattr(config, "PROCESSED_EMOJI",           "white_check_mark")

    # Motion API stub — returns success
    motion_resp = MagicMock()
    motion_resp.json.return_value = _MOTION_RESPONSE
    motion_resp.raise_for_status = lambda: None
    motion_resp.status_code = 200

    return {
        "claude":       claude_client,
        "claude_resp":  claude_resp,
        "slack":        slack_client,
        "motion_resp":  motion_resp,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _plain_msg(ts: str, text: str) -> dict:
    """Build a minimal unprocessed Slack message (no ✅ reaction)."""
    return {"ts": ts, "text": text, "reactions": []}


def _email_file_msg(ts: str, subject: str, body: str, sender: str = "alice@example.com") -> dict:
    """Build a Slack message containing a filetype='email' file."""
    return {
        "ts":    ts,
        "text":  "",
        "files": [{
            "filetype":   "email",
            "subject":    subject,
            "from":       [{"original": sender}],
            "plain_text": body,
        }],
        "reactions": [],
    }


# ── process_channel ───────────────────────────────────────────────────────────

class TestProcessChannel:

    def test_returns_count_of_created_tasks(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            count = tasks.process_channel()
        assert count == 1

    def test_motion_api_called(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.process_channel()
        mock_post.assert_called_once()

    def test_motion_api_receives_task_name(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.process_channel()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["name"] == "Review budget proposal"

    def test_motion_api_receives_workspace_id(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.process_channel()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["workspaceId"] == "WS_TEST"

    def test_motion_api_receives_assignee_id(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.process_channel()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["assigneeId"] == "USER_TEST"

    def test_slack_confirmation_posted(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        tasks_env["slack"].chat_postMessage.assert_called_once()

    def test_slack_confirmation_in_thread(self, tasks_env):
        """Confirmation must be a thread reply to the original message."""
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        kwargs = tasks_env["slack"].chat_postMessage.call_args.kwargs
        assert kwargs.get("thread_ts") == _TS

    def test_slack_confirmation_mentions_task_name(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        text = tasks_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "Review budget proposal" in text

    def test_message_marked_processed_after_task_created(self, tasks_env):
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        tasks_env["slack"].reactions_add.assert_called_once()
        kwargs = tasks_env["slack"].reactions_add.call_args.kwargs
        assert kwargs["channel"] == _CHANNEL_ID
        assert kwargs["timestamp"] == _TS

    def test_no_unprocessed_messages_returns_zero(self, tasks_env):
        tasks_env["slack"].conversations_history.return_value = {"messages": []}
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            count = tasks.process_channel()
        assert count == 0
        tasks_env["slack"].chat_postMessage.assert_not_called()

    def test_short_message_is_skipped(self, tasks_env):
        """Messages under 20 characters must not be sent to Claude."""
        tasks_env["slack"].conversations_history.return_value = {
            "messages": [_plain_msg(_TS, "Hi")]
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            count = tasks.process_channel()
        assert count == 0
        tasks_env["claude"].messages.create.assert_not_called()

    def test_claude_called_with_email_text(self, tasks_env):
        """The full email text must be included in Claude's prompt."""
        tasks_env["slack"].conversations_history.return_value = {
            "messages": [_plain_msg(_TS, "Please review the unique budget proposal XYZZY.")]
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        prompt = tasks_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "XYZZY" in prompt

    def test_email_file_text_extracted_for_claude(self, tasks_env):
        """Email-type Slack files must be unwrapped before sending to Claude."""
        tasks_env["slack"].conversations_history.return_value = {
            "messages": [_email_file_msg(
                _TS,
                subject="Budget Review",
                body="Unique body text QWERTY for extraction testing.",
            )]
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        prompt = tasks_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "QWERTY" in prompt


class TestProcessChannelMultipleTasks:

    def test_two_tasks_from_one_email_both_created(self, tasks_env):
        """Claude returning two tasks → two Motion API calls."""
        tasks_env["claude_resp"].content = [MagicMock(text=_TWO_TASK_JSON)]
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            count = tasks.process_channel()
        assert count == 2
        assert mock_post.call_count == 2

    def test_two_tasks_confirmation_mentions_count(self, tasks_env):
        """Confirmation text must state how many tasks were created."""
        tasks_env["claude_resp"].content = [MagicMock(text=_TWO_TASK_JSON)]
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        text = tasks_env["slack"].chat_postMessage.call_args.kwargs["text"]
        assert "2" in text

    def test_two_messages_both_processed(self, tasks_env):
        """Two unprocessed messages → two Claude calls, two Motion tasks."""
        tasks_env["slack"].conversations_history.return_value = {
            "messages": [
                _plain_msg(_TS,  "Please review the Q3 budget proposal carefully."),
                _plain_msg(_TS2, "Please confirm your attendance at the faculty meeting next week."),
            ]
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            count = tasks.process_channel()
        assert count == 2
        assert tasks_env["claude"].messages.create.call_count == 2

    def test_already_processed_message_skipped(self, tasks_env):
        """A message with the ✅ reaction must not be processed again."""
        tasks_env["slack"].conversations_history.return_value = {
            "messages": [{
                "ts":        _TS,
                "text":      "Please review the budget.",
                "reactions": [{"name": "white_check_mark", "count": 1, "users": ["U123"]}],
            }]
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            count = tasks.process_channel()
        assert count == 0
        tasks_env["claude"].messages.create.assert_not_called()


class TestProcessChannelErrorHandling:

    def test_invalid_claude_json_does_not_crash(self, tasks_env):
        """If Claude returns bad JSON, process_channel must continue without raising."""
        tasks_env["claude_resp"].content = [MagicMock(text="This is not JSON at all!")]
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            # Should not raise
            count = tasks.process_channel()
        assert count == 0

    def test_motion_api_error_does_not_crash(self, tasks_env):
        """An HTTP error from Motion must be caught; process_channel must not raise."""
        error_resp = MagicMock()
        error_resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=429, text="Too Many Requests")
        )
        with patch("email_to_motion.tasks.post_with_retries", return_value=error_resp):
            count = tasks.process_channel()
        assert count == 0

    def test_message_not_marked_processed_after_motion_error(self, tasks_env):
        """If Motion fails, the message must NOT receive a ✅ (so it can be retried)."""
        error_resp = MagicMock()
        error_resp.raise_for_status.side_effect = requests.HTTPError(
            response=MagicMock(status_code=500, text="Internal Server Error")
        )
        with patch("email_to_motion.tasks.post_with_retries", return_value=error_resp):
            tasks.process_channel()
        tasks_env["slack"].reactions_add.assert_not_called()


# ── analyze_with_claude ───────────────────────────────────────────────────────

class TestAnalyzeWithClaude:

    def test_returns_list_of_tasks(self, tasks_env):
        result = tasks.analyze_with_claude("Please review the attached budget document.")
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_single_dict_response_wrapped_in_list(self, tasks_env):
        """If Claude returns a single object (not array), it must be wrapped in a list."""
        single_obj = json.dumps({
            "name":         "Do something",
            "description":  "Do it.",
            "priority":     "MEDIUM",
            "duration":     30,
            "dueDate":      None,
            "deadlineType": "NONE",
        })
        tasks_env["claude_resp"].content = [MagicMock(text=single_obj)]
        result = tasks.analyze_with_claude("Some email text here to process.")
        assert isinstance(result, list)
        assert result[0]["name"] == "Do something"

    def test_task_fields_present(self, tasks_env):
        result = tasks.analyze_with_claude("Please review the budget document by Friday.")
        task = result[0]
        for field in ("name", "description", "priority", "duration", "deadlineType"):
            assert field in task, f"Missing field: {field}"

    def test_email_text_included_in_prompt(self, tasks_env):
        tasks.analyze_with_claude("Unique text ZYXWVU for prompt verification.")
        prompt = tasks_env["claude"].messages.create.call_args.kwargs["messages"][0]["content"]
        assert "ZYXWVU" in prompt


# ── create_motion_task ────────────────────────────────────────────────────────

class TestCreateMotionTask:

    def test_posts_to_motion_tasks_endpoint(self, tasks_env):
        task = {
            "name": "Write report", "description": "Write it.",
            "priority": "MEDIUM", "duration": 60,
            "dueDate": "2026-03-20", "deadlineType": "SOFT",
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.create_motion_task(task)
        url = mock_post.call_args.args[0]
        assert url.endswith("/tasks")

    def test_uses_default_due_date_when_none(self, tasks_env):
        """If dueDate is null, create_motion_task must set a fallback date."""
        task = {
            "name": "Write report", "description": "Write it.",
            "priority": "MEDIUM", "duration": 60,
            "dueDate": None, "deadlineType": "SOFT",
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.create_motion_task(task)
        payload = mock_post.call_args.kwargs["json"]
        # A fallback date must be present and non-empty
        assert payload.get("dueDate"), "dueDate must not be empty when original is None"

    def test_payload_includes_auto_scheduled(self, tasks_env):
        task = {
            "name": "Write report", "description": "Write it.",
            "priority": "HIGH", "duration": 90,
            "dueDate": "2026-03-20", "deadlineType": "HARD",
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.create_motion_task(task)
        payload = mock_post.call_args.kwargs["json"]
        assert "autoScheduled" in payload
        assert payload["autoScheduled"]["deadlineType"] == "HARD"

    def test_api_key_sent_in_header(self, tasks_env):
        task = {
            "name": "Write report", "description": "desc",
            "priority": "LOW", "duration": 30,
            "dueDate": "2026-03-20", "deadlineType": "NONE",
        }
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]) as mock_post:
            tasks.create_motion_task(task)
        headers = mock_post.call_args.kwargs["headers"]
        assert headers.get("X-API-Key") == "mk_test_key"


# ── Channel ID caching ────────────────────────────────────────────────────────

class TestChannelIdCaching:

    def test_channel_looked_up_on_first_call(self, tasks_env):
        tasks_env["slack"].conversations_history.return_value = {"messages": []}
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
        tasks_env["slack"].conversations_list.assert_called_once()

    def test_channel_not_looked_up_again_on_second_call(self, tasks_env):
        """Channel ID is cached; conversations_list must not be called on repeat runs."""
        tasks_env["slack"].conversations_history.return_value = {"messages": []}
        with patch("email_to_motion.tasks.post_with_retries",
                   return_value=tasks_env["motion_resp"]):
            tasks.process_channel()
            tasks.process_channel()
        # conversations_list called once to resolve name → ID; second call uses cache
        assert tasks_env["slack"].conversations_list.call_count == 1
