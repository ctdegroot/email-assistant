"""
utils.py — Shared non-Slack utilities used across multiple modules.
"""

import json
import logging
import re
import time
from typing import Any

import anthropic

log = logging.getLogger(__name__)


# ── Retry logic for transient Claude API errors ───────────────────────────────

# These error types are safe to retry because they reflect temporary conditions
# (rate limits, timeouts, transient infra issues) rather than permanent failures.
_ANTHROPIC_RETRYABLE = (
    anthropic.RateLimitError,        # 429 — too many requests; back off and retry
    anthropic.APITimeoutError,       # request timed out; retry
    anthropic.APIConnectionError,    # network hiccup; retry
    anthropic.InternalServerError,   # 5xx from Anthropic infra; retry
)


def call_with_retries(fn, *args, max_retries: int = 3, base_delay: float = 1.0, **kwargs):
    """
    Call fn(*args, **kwargs) with exponential back-off on transient Anthropic API errors.

    Retries on: RateLimitError, APITimeoutError, APIConnectionError, InternalServerError.
    Raises immediately on all other exceptions (e.g. AuthenticationError, BadRequestError).

    Args:
        fn:          Callable to invoke (typically config.claude.messages.create).
        *args:       Positional arguments forwarded to fn.
        max_retries: Maximum number of retries (default 3; total attempts = max_retries + 1).
        base_delay:  Initial back-off in seconds (doubles each retry; default 1.0).
        **kwargs:    Keyword arguments forwarded to fn.

    Returns the result of fn on success.
    Raises the last transient exception when all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except _ANTHROPIC_RETRYABLE as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                log.warning(
                    "Claude API transient error (attempt %d/%d): %s — retrying in %.0fs",
                    attempt + 1, max_retries + 1, type(exc).__name__, delay,
                )
                time.sleep(delay)
            else:
                log.error(
                    "Claude API failed after %d attempt(s): %s: %s",
                    max_retries + 1, type(exc).__name__, exc,
                )
    raise last_exc  # type: ignore[misc]  # always set when loop exits via the except branch


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
