"""
Tests for utils.py — parse_claude_json and call_with_retries.
"""

import json
from unittest.mock import MagicMock, patch, call

import anthropic
import pytest

from email_to_motion.utils import parse_claude_json, call_with_retries


class TestParseClaudeJson:

    # ── Clean JSON (no fences) ─────────────────────────────────────────────────

    def test_plain_json_object(self):
        result = parse_claude_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_plain_json_array(self):
        result = parse_claude_json('[{"a": 1}, {"b": 2}]')
        assert result == [{"a": 1}, {"b": 2}]

    def test_leading_trailing_whitespace_stripped(self):
        result = parse_claude_json('  {"x": 42}  ')
        assert result == {"x": 42}

    # ── Fenced JSON ────────────────────────────────────────────────────────────

    def test_backtick_fence_json_label(self):
        text = '```json\n{"key": "value"}\n```'
        assert parse_claude_json(text) == {"key": "value"}

    def test_backtick_fence_no_label(self):
        text = '```\n{"key": "value"}\n```'
        assert parse_claude_json(text) == {"key": "value"}

    def test_fenced_json_array(self):
        text = '```json\n[{"name": "Task 1"}]\n```'
        result = parse_claude_json(text)
        assert result == [{"name": "Task 1"}]

    def test_fenced_with_surrounding_whitespace(self):
        text = '  ```json\n{"k": "v"}\n```  '
        assert parse_claude_json(text) == {"k": "v"}

    def test_fenced_multiline_object(self):
        text = '```json\n{\n  "title": "Meeting",\n  "start": "2026-03-12"\n}\n```'
        result = parse_claude_json(text)
        assert result["title"] == "Meeting"
        assert result["start"] == "2026-03-12"

    # ── Invalid JSON raises ────────────────────────────────────────────────────

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_claude_json("this is not json")

    def test_fenced_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_claude_json("```json\nnot valid json\n```")

    def test_empty_string_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_claude_json("")

    def test_truncated_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_claude_json('{"incomplete":')


# ── call_with_retries ─────────────────────────────────────────────────────────

class TestCallWithRetries:

    def test_success_on_first_attempt_returns_result(self):
        fn = MagicMock(return_value="ok")
        result = call_with_retries(fn, "arg1", kwarg="v")
        assert result == "ok"
        fn.assert_called_once_with("arg1", kwarg="v")

    def test_no_sleep_on_first_attempt_success(self):
        fn = MagicMock(return_value="ok")
        with patch("email_to_motion.utils.time.sleep") as mock_sleep:
            call_with_retries(fn)
        mock_sleep.assert_not_called()

    def test_retries_on_rate_limit_error(self):
        fn = MagicMock(side_effect=[
            anthropic.RateLimitError("rate limited", response=MagicMock(), body={}),
            "ok",
        ])
        with patch("email_to_motion.utils.time.sleep"):
            result = call_with_retries(fn, max_retries=3)
        assert result == "ok"
        assert fn.call_count == 2

    def test_retries_on_api_timeout(self):
        fn = MagicMock(side_effect=[
            anthropic.APITimeoutError(request=MagicMock()),
            "ok",
        ])
        with patch("email_to_motion.utils.time.sleep"):
            result = call_with_retries(fn, max_retries=3)
        assert result == "ok"

    def test_retries_on_connection_error(self):
        fn = MagicMock(side_effect=[
            anthropic.APIConnectionError(request=MagicMock()),
            "ok",
        ])
        with patch("email_to_motion.utils.time.sleep"):
            result = call_with_retries(fn, max_retries=3)
        assert result == "ok"

    def test_retries_on_internal_server_error(self):
        fn = MagicMock(side_effect=[
            anthropic.InternalServerError("server error", response=MagicMock(), body={}),
            "ok",
        ])
        with patch("email_to_motion.utils.time.sleep"):
            result = call_with_retries(fn, max_retries=3)
        assert result == "ok"

    def test_raises_after_max_retries_exhausted(self):
        err = anthropic.RateLimitError("still rate limited", response=MagicMock(), body={})
        fn = MagicMock(side_effect=err)
        with patch("email_to_motion.utils.time.sleep"):
            with pytest.raises(anthropic.RateLimitError):
                call_with_retries(fn, max_retries=2)
        assert fn.call_count == 3   # 1 initial + 2 retries

    def test_sleep_delay_doubles_each_retry(self):
        err = anthropic.RateLimitError("rate limited", response=MagicMock(), body={})
        fn = MagicMock(side_effect=err)
        with patch("email_to_motion.utils.time.sleep") as mock_sleep:
            with pytest.raises(anthropic.RateLimitError):
                call_with_retries(fn, max_retries=3, base_delay=1.0)
        # Delays: 1s, 2s, 4s  (no sleep after the last attempt)
        assert mock_sleep.call_args_list == [call(1.0), call(2.0), call(4.0)]

    def test_custom_base_delay(self):
        err = anthropic.RateLimitError("rate limited", response=MagicMock(), body={})
        fn = MagicMock(side_effect=err)
        with patch("email_to_motion.utils.time.sleep") as mock_sleep:
            with pytest.raises(anthropic.RateLimitError):
                call_with_retries(fn, max_retries=2, base_delay=0.5)
        assert mock_sleep.call_args_list == [call(0.5), call(1.0)]

    def test_non_retryable_error_raises_immediately(self):
        """AuthenticationError is a permanent failure — must NOT be retried."""
        fn = MagicMock(side_effect=anthropic.AuthenticationError(
            "bad key", response=MagicMock(), body={}
        ))
        with patch("email_to_motion.utils.time.sleep") as mock_sleep:
            with pytest.raises(anthropic.AuthenticationError):
                call_with_retries(fn, max_retries=3)
        fn.assert_called_once()       # no retries
        mock_sleep.assert_not_called()

    def test_non_retryable_plain_exception_raises_immediately(self):
        """Arbitrary exceptions (e.g. ValueError) are not retried."""
        fn = MagicMock(side_effect=ValueError("bad input"))
        with patch("email_to_motion.utils.time.sleep") as mock_sleep:
            with pytest.raises(ValueError):
                call_with_retries(fn, max_retries=3)
        fn.assert_called_once()
        mock_sleep.assert_not_called()

    def test_success_after_multiple_retries(self):
        err = anthropic.APITimeoutError(request=MagicMock())
        fn = MagicMock(side_effect=[err, err, "final ok"])
        with patch("email_to_motion.utils.time.sleep"):
            result = call_with_retries(fn, max_retries=3)
        assert result == "final ok"
        assert fn.call_count == 3

    def test_zero_max_retries_raises_on_first_failure(self):
        err = anthropic.RateLimitError("rate limited", response=MagicMock(), body={})
        fn = MagicMock(side_effect=err)
        with patch("email_to_motion.utils.time.sleep") as mock_sleep:
            with pytest.raises(anthropic.RateLimitError):
                call_with_retries(fn, max_retries=0)
        fn.assert_called_once()
        mock_sleep.assert_not_called()
