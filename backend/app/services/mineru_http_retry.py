"""Bounded retry policy shared by the two remote MinerU HTTP adapters."""
from __future__ import annotations

import json
from http.client import HTTPException
from typing import Callable, TypeVar
from urllib.error import HTTPError, URLError


_T = TypeVar("_T")
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


def is_retryable_mineru_http_error(exc: BaseException) -> bool:
    """Return whether a remote MinerU request may succeed when repeated."""
    if isinstance(exc, HTTPError):
        return exc.code in _RETRYABLE_HTTP_STATUSES or 500 <= exc.code <= 599
    return isinstance(
        exc,
        (
            URLError,
            TimeoutError,
            ConnectionError,
            HTTPException,
            json.JSONDecodeError,
            UnicodeError,
        ),
    )


def call_with_mineru_http_retries(
    operation: Callable[[], _T],
    *,
    max_retries: int,
    sleep: Callable[[float], None],
) -> _T:
    """Run one HTTP operation with bounded exponential-backoff retries.

    ``max_retries`` counts attempts after the first call. The shipped value is
    two, so a transient request gets at most three total attempts.
    """
    retries = max(0, int(max_retries))
    for attempt in range(retries + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= retries or not is_retryable_mineru_http_error(exc):
                raise
            sleep(float(2**attempt))
    raise AssertionError("unreachable")
