"""Typed errors raised by the search client.

These exist so the CLI (and library consumers) can react to network
failures with a clear, user-facing message instead of a raw curl-cffi
traceback. They are intentionally light wrappers — the original
exception is kept as ``__cause__`` for logging.
"""

from __future__ import annotations


class SearchClientError(Exception):
    """Base class for errors talking to the Google Flights backend."""


class SearchTimeoutError(SearchClientError):
    """The request to Google Flights timed out before any data arrived."""


class SearchConnectionError(SearchClientError):
    """A network/DNS issue prevented us from reaching Google Flights."""


class SearchHTTPError(SearchClientError):
    """Google Flights returned a non-2xx HTTP response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        """Store the HTTP status alongside the message for richer logging."""
        super().__init__(message)
        self.status_code = status_code


class GoogleFlightsRateLimited(SearchClientError):
    """Google Flights returned an ErrorResponse instead of flight rows.

    The response is HTTP 200 with a ``wrb.fr`` chunk whose inner payload is
    ``null`` and an outer ``ErrorResponse`` marker in ``row[5]`` of the
    ``GetShoppingResults`` envelope. Previously this surfaced as a silent
    empty result; callers had no way to distinguish "rate-limited" from
    "no flights match these filters". This exception makes the failure
    explicit so the CLI / MCP server can surface a retryable error and
    library users can implement their own backoff.

    Triggered by per-IP / per-session-fingerprint throttling. A short
    backoff (~30-60s) is usually enough; rotating ``curl_cffi``'s
    ``impersonate`` fingerprint sometimes helps.
    """

    def __init__(self, message: str, *, session_id: str | None = None):
        """Store the session id captured from the error envelope, if any."""
        super().__init__(message)
        self.session_id = session_id
