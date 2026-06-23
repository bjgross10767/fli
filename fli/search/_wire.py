r"""Parsing helpers for Google Flights' FlightsFrontendService wire format.

The Service returns JSONP-flavoured responses of the form::

    )]}'\n\n
    <chunk1_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    <chunk2_byte_len>\n
    [["wrb.fr", null, "<inner JSON string>"]]
    ...

`GetShoppingResults` and `GetCalendarGraph` happen to emit a single chunk so
the legacy parsers in this package could get away with `lstrip(")]}'")`.
`GetBookingResults` emits two chunks, so we need a proper multi-chunk reader.

Important quirk: the length headers count UTF-8 **bytes**, not Python string
characters. When the response contains any non-ASCII characters (which it
sometimes does — airport names, airline names) the offsets diverge, so the
reader must operate over the byte representation of the body.

This module centralises that reader and exposes :func:`iter_wrb_chunks` which
yields the decoded inner JSON of each ``wrb.fr`` chunk.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = b")]}'"


def iter_wrb_chunks(body: str | bytes) -> Iterator[Any]:
    """Yield the inner JSON object of every ``wrb.fr`` chunk in ``body``.

    Robust to single-chunk responses with no length headers (the older
    ``GetShoppingResults`` / ``GetCalendarGraph`` shape) — those are parsed
    by falling back to a single JSON load over the trimmed body.
    """
    if isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = body

    raw = raw.lstrip()
    if raw.startswith(_PREFIX):
        raw = raw[len(_PREFIX) :]
    raw = raw.lstrip()

    if not raw:
        return

    # Fast path: no length headers (legacy single-chunk responses).
    if not (b"0" <= raw[:1] <= b"9"):
        try:
            outer = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Failed to decode single-chunk wrb.fr body as JSON", exc_info=True)
            return
        yield from _chunks_from_outer(outer)
        return

    cursor = 0
    while cursor < len(raw):
        # Read the decimal length prefix terminated by \n.
        end = raw.find(b"\n", cursor)
        if end == -1:
            break
        try:
            length = int(raw[cursor:end])
        except ValueError:
            logger.warning(
                "Malformed length header at offset %d; truncating chunk stream",
                cursor,
            )
            break
        # Google's length header counts the leading newline after the header
        # AND the trailing newline that separates this chunk from the next.
        # We've already consumed the leading newline (it terminated the header),
        # so we read `length - 1` bytes which gives JSON + trailing \n.
        cursor = end + 1
        chunk_bytes = max(length - 1, 0)
        payload = raw[cursor : cursor + chunk_bytes]
        cursor += chunk_bytes
        try:
            outer = json.loads(payload.strip().decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Discarding malformed wrb.fr chunk", exc_info=True)
            continue
        yield from _chunks_from_outer(outer)


def _chunks_from_outer(outer: Any) -> Iterator[Any]:
    """Walk a top-level chunk list and yield decoded inner-JSON payloads."""
    if not isinstance(outer, list):
        return
    for row in outer:
        if not isinstance(row, list) or len(row) < 3:
            continue
        if row[0] != "wrb.fr":
            continue
        inner = row[2]
        if not isinstance(inner, str) or not inner:
            continue
        try:
            yield json.loads(inner)
        except (ValueError, json.JSONDecodeError):
            logger.warning("Failed to decode wrb.fr inner JSON payload", exc_info=True)
            continue


def parse_first_wrb_payload(body: str | bytes) -> Any:
    """Return the inner JSON of the first ``wrb.fr`` chunk, or None."""
    for chunk in iter_wrb_chunks(body):
        return chunk
    return None


# Protobuf type URL Google embeds in the wrb.fr envelope when the request was
# rejected mid-pipeline (rate-limit / fingerprint / quota). The "rejected"
# envelope keeps the wrb.fr row but sets its inner-JSON slot (``row[2]``) to
# ``null`` and stashes the error detail in ``row[5]``. ``parse_first_wrb_payload``
# correctly returns None for that shape, but callers had no way to distinguish
# "no flights match these filters" (success with empty result set) from
# "Google rejected the request" (transient failure to retry / back off).
RATE_LIMIT_ERROR_MARKER = "travel.frontend.flights.ErrorResponse"


def is_rate_limit_response(body: str | bytes) -> bool:
    """Return True if ``body`` looks like Google's ErrorResponse envelope.

    Uses a substring match on the protobuf type URL Google embeds in the
    envelope. The marker is part of a stable protobuf descriptor
    (``type.googleapis.com/travel.frontend.flights.ErrorResponse``) so the
    check is robust to envelope-position drift — any future field
    reshuffle inside the envelope still keeps the type URL.
    """
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            return False
    return RATE_LIMIT_ERROR_MARKER in body


def extract_error_session_id(body: str | bytes) -> str | None:
    """Best-effort: pull the session id from an ErrorResponse envelope.

    Returns ``None`` when the body has no recognisable session id, or
    when the envelope shape differs from the captured sample. The session
    id is only used to enrich the exception message — never required for
    correctness.
    """
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            return None
    if RATE_LIMIT_ERROR_MARKER not in body:
        return None
    # Strip the JSONP prefix and parse the outer chunk list.
    raw = body.lstrip()
    if raw.startswith(")]}'"):
        raw = raw[len(")]}'") :]
    raw = raw.lstrip()
    if not raw:
        return None
    try:
        outer = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(outer, list):
        return None
    for row in outer:
        if (
            isinstance(row, list)
            and len(row) >= 6
            and row[0] == "wrb.fr"
            and isinstance(row[5], list)
        ):
            try:
                details = row[5][2][0]
                # details = [type_url, [[_, [session_meta], 0, session_id, ...], 0]]
                if isinstance(details, list) and len(details) >= 2 and isinstance(details[1], list):
                    candidate = details[1][0][3]
                    if isinstance(candidate, str) and candidate:
                        return candidate
            except (IndexError, TypeError):
                continue
    return None
