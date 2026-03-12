"""
utils.py — Shared non-Slack utilities used across multiple modules.
"""

import json
import re
from typing import Any


def parse_claude_json(text: str) -> Any:
    """
    Parse JSON from a Claude response, tolerating optional markdown code fences.

    Claude occasionally wraps its JSON in a ```json … ``` fence despite being
    asked not to.  This function strips the fence if present, then parses.

    Raises json.JSONDecodeError on invalid JSON (callers should catch this).
    """
    text = text.strip()
    m = re.match(r'^```(?:json)?\s*\n?(.*?)\n?```\s*$', text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    return json.loads(text)
