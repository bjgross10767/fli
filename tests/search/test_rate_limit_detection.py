"""End-to-end tests that rate-limit ErrorResponse envelopes raise instead of swallow.

Prior to this fix, Google's rate-limit / fingerprint-reject envelope (HTTP 200
with ``wrb.fr`` row whose inner JSON is ``null`` and a protobuf-typed
``ErrorResponse`` blob in ``row[5]``) was silently mapped to "no flights match
your filters". Callers had no way to distinguish the two cases. These tests
lock in the new behaviour: the search core raises
:class:`GoogleFlightsRateLimited`, and the MCP layer surfaces a tailored
``code="RATE_LIMITED"`` response.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fli.models import (
    Airport,
    DateSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    PassengerInfo,
    TripType,
)
from fli.search import GoogleFlightsRateLimited, SearchDates, SearchFlights

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "flight_search_rate_limit_error.txt"


class _RateLimitedResponse:
    """Stand-in for ``curl_cffi``'s response object, returning the captured body."""

    __slots__ = ("text", "status_code")

    def __init__(self, text: str):
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class _RateLimitedClient:
    """Always returns the captured rate-limit envelope, regardless of input."""

    def __init__(self, body: str):
        self._body = body

    def post(self, url: str, **kwargs: Any) -> _RateLimitedResponse:
        return _RateLimitedResponse(self._body)

    def get(self, url: str, **kwargs: Any) -> _RateLimitedResponse:
        return self._post(url, **kwargs)


@pytest.fixture
def rate_limit_body() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


@pytest.fixture
def rate_limit_client(rate_limit_body: str) -> _RateLimitedClient:
    return _RateLimitedClient(rate_limit_body)


def _one_way_filters() -> FlightSearchFilters:
    return FlightSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.BZN, 0]],
                arrival_airport=[[Airport.SEA, 0]],
                travel_date="2026-07-15",
            )
        ],
    )


def _round_trip_filters() -> FlightSearchFilters:
    return FlightSearchFilters(
        trip_type=TripType.ROUND_TRIP,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.BZN, 0]],
                arrival_airport=[[Airport.SEA, 0]],
                travel_date="2026-07-15",
            ),
            FlightSegment(
                departure_airport=[[Airport.SEA, 0]],
                arrival_airport=[[Airport.BZN, 0]],
                travel_date="2026-07-22",
            ),
        ],
    )


def _date_filters() -> DateSearchFilters:
    return DateSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            FlightSegment(
                departure_airport=[[Airport.BZN, 0]],
                arrival_airport=[[Airport.SEA, 0]],
                travel_date="2026-07-15",
            )
        ],
        from_date="2026-07-10",
        to_date="2026-07-25",
    )


class TestSearchFlightsRateLimit:
    def test_one_way_raises(self, rate_limit_client: _RateLimitedClient) -> None:
        search = SearchFlights()
        search.client = rate_limit_client
        with pytest.raises(GoogleFlightsRateLimited) as exc_info:
            search.search(_one_way_filters(), currency="USD")
        assert "ErrorResponse" in str(exc_info.value)
        # Real captured fixture exposes this session id.
        assert exc_info.value.session_id == "avU5aqPqKqioj8oP77Xy6Qc"

    def test_round_trip_raises(self, rate_limit_client: _RateLimitedClient) -> None:
        # The first outbound POST already hits rate-limit; the expansion fan-out
        # is never reached because _fetch_flights raises before _expand_multi_leg.
        search = SearchFlights()
        search.client = rate_limit_client
        with pytest.raises(GoogleFlightsRateLimited):
            search.search(_round_trip_filters(), currency="USD")

    def test_no_silent_empty_result(self, rate_limit_client: _RateLimitedClient) -> None:
        # Regression guard against the silent-empty bug.
        search = SearchFlights()
        search.client = rate_limit_client
        with pytest.raises(GoogleFlightsRateLimited):
            result = search.search(_one_way_filters(), currency="USD")
            # If we ever reach this line, the bug came back.
            assert result is None or len(result) == 0, (
                f"silent fallback returned {len(result) if result else 'None'} results"
            )


class TestSearchDatesRateLimit:
    def test_search_dates_raises(self, rate_limit_client: _RateLimitedClient) -> None:
        search = SearchDates()
        search.client = rate_limit_client
        with pytest.raises(GoogleFlightsRateLimited):
            search.search(_date_filters(), currency="USD")


class TestMCPServerRateLimit:
    """MCP layer should convert the exception into a structured error.

    Not a vanilla ``success=False, error="Search failed: ..."`` — callers
    keying off the ``code`` field can implement targeted backoff.
    """

    def test_execute_flight_search_returns_rate_limit_code(
        self, rate_limit_body: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fli.mcp import server

        rate_limited = _RateLimitedClient(rate_limit_body)
        original_init = SearchFlights.__init__

        def _patched_init(self, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            self.client = rate_limited

        # Patch SearchFlights to ship the fake client on construction so the
        # MCP layer's own `SearchFlights()` picks it up without extra wiring.
        monkeypatch.setattr(SearchFlights, "__init__", _patched_init, raising=True)
        params = server.FlightSearchParams(
            origin="BZN",
            destination="SEA",
            departure_date="2026-07-15",
            currency="USD",
        )
        result = server._execute_flight_search(params)
        assert result["success"] is False
        assert result["code"] == "RATE_LIMITED"
        assert result["retry_after_s"] == 30
        assert result["session_id"] == "avU5aqPqKqioj8oP77Xy6Qc"
        assert result["flights"] == []

    def test_execute_date_search_returns_rate_limit_code(
        self, rate_limit_body: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fli.mcp import server

        rate_limited = _RateLimitedClient(rate_limit_body)
        original_init = SearchDates.__init__

        def _patched_init(self, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            self.client = rate_limited

        monkeypatch.setattr(SearchDates, "__init__", _patched_init, raising=True)
        params = server.DateSearchParams(
            origin="BZN",
            destination="SEA",
            start_date="2026-07-10",
            end_date="2026-07-25",
            currency="USD",
        )
        result = server._execute_date_search(params)
        assert result["success"] is False
        assert result["code"] == "RATE_LIMITED"
        assert result["retry_after_s"] == 30
        assert result["dates"] == []
