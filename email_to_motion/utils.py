"""
utils.py — Shared non-Slack utilities used across multiple modules.
"""

import json
import logging
import re
import time
from typing import Any

import anthropic
import requests as _requests

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


# ── Retry logic for transient HTTP errors ────────────────────────────────────

# These exception types indicate a transient network condition — safe to retry.
_HTTP_RETRYABLE = (
    _requests.exceptions.Timeout,          # read or connect timed out
    _requests.exceptions.ConnectionError,  # DNS failure, refused, reset, etc.
    _requests.exceptions.ChunkedEncodingError,  # connection dropped mid-stream
)


def _http_request_with_retries(
    method: str,
    url: str,
    max_retries: int = 3,
    base_delay: float = 2.0,
    **kwargs,
) -> _requests.Response:
    """
    Internal helper: make an HTTP request with exponential back-off on transient
    network errors and HTTP 5xx / 429 responses.

    Args:
        method:      HTTP method string, e.g. "GET", "POST".
        url:         URL to request.
        max_retries: Maximum number of retries (default 3; total attempts = max_retries + 1).
        base_delay:  Initial back-off in seconds (doubles each retry; default 2.0).
        **kwargs:    Forwarded verbatim to requests.request (e.g. timeout=30, json=...).

    Returns the Response on success (2xx or non-5xx/429).
    Raises the last exception when all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = _requests.request(method, url, **kwargs)
            # Retry on server errors (5xx) and rate limits (429); raise immediately on other 4xx
            if resp.status_code in (429,) or resp.status_code >= 500:
                raise _requests.exceptions.HTTPError(
                    f"HTTP {resp.status_code}", response=resp
                )
            return resp
        except (*_HTTP_RETRYABLE, _requests.exceptions.HTTPError) as exc:
            last_exc = exc
            if attempt < max_retries:
                # Respect Retry-After header if present (e.g. Motion 429)
                retry_after: float = base_delay * (2 ** attempt)
                if hasattr(exc, "response") and exc.response is not None:
                    ra = exc.response.headers.get("Retry-After")
                    if ra:
                        try:
                            retry_after = max(retry_after, float(ra))
                        except ValueError:
                            pass
                log.warning(
                    "HTTP %s transient error (attempt %d/%d): %s — retrying in %.0fs",
                    method, attempt + 1, max_retries + 1, exc, retry_after,
                )
                import time as _time_mod
                _time_mod.sleep(retry_after)
            else:
                log.error(
                    "HTTP %s failed after %d attempt(s): %s",
                    method, max_retries + 1, exc,
                )
    raise last_exc  # type: ignore[misc]


def get_with_retries(
    url: str,
    max_retries: int = 3,
    base_delay: float = 2.0,
    **kwargs,
) -> _requests.Response:
    """
    GET ``url`` with exponential back-off on transient network errors and
    HTTP 5xx responses.

    Args:
        url:         URL to fetch.
        max_retries: Maximum number of retries (default 3; total attempts = max_retries + 1).
        base_delay:  Initial back-off in seconds (doubles each retry; default 2.0).
        **kwargs:    Forwarded verbatim to requests.get (e.g. timeout=30, headers=...).

    Returns the Response on success (2xx or non-5xx/429).
    Raises the last exception when all retries are exhausted.
    """
    return _http_request_with_retries("GET", url, max_retries=max_retries, base_delay=base_delay, **kwargs)


def post_with_retries(
    url: str,
    max_retries: int = 3,
    base_delay: float = 2.0,
    **kwargs,
) -> _requests.Response:
    """
    POST ``url`` with exponential back-off on transient network errors and
    HTTP 5xx / 429 responses (respects Retry-After header).

    Args:
        url:         URL to POST to.
        max_retries: Maximum number of retries (default 3; total attempts = max_retries + 1).
        base_delay:  Initial back-off in seconds (doubles each retry; default 2.0).
        **kwargs:    Forwarded verbatim to requests.post (e.g. timeout=30, json=...).

    Returns the Response on success.
    Raises the last exception when all retries are exhausted.
    """
    return _http_request_with_retries("POST", url, max_retries=max_retries, base_delay=base_delay, **kwargs)


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
