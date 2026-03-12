"""
Tests for utils.py — parse_claude_json.
"""

import json
import pytest

from email_to_motion.utils import parse_claude_json


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
