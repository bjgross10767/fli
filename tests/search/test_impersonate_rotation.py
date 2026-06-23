"""Tests for the per-request impersonate rotation in :mod:`fli.search.client`.

Per-request rotation is the second half of the rate-limit-resilience pair
(PR #208 surfaces the error; rotation reduces how often callers hit it).
The pool intentionally mixes Chrome variants with Firefox + Safari to
spread the JA3 surface so Google's per-fingerprint quota is harder to
exhaust on a tight burst of flight queries.
"""

from __future__ import annotations

from collections import Counter

from curl_cffi.requests import BrowserType

from fli.search.client import _IMPERSONATE_POOL, pick_impersonate


class TestImpersonatePool:
    def test_pool_is_non_empty(self):
        assert len(_IMPERSONATE_POOL) >= 2, "pool too small to actually rotate"

    def test_pool_has_diverse_browsers(self):
        # Pool must include at least 2 distinct browser families so rotation
        # spreads JA3 fingerprints rather than just chrome-version variants.
        families = {name[:4] for name in _IMPERSONATE_POOL}
        assert len(families) >= 2, (
            f"pool {_IMPERSONATE_POOL} only covers families {families}; "
            "include at least 2 of chrome/firefox/safari to widen JA3 surface"
        )

    def test_every_pool_entry_is_valid_curl_cffi_browser(self):
        # If curl_cffi drops a browser version in a future release, the
        # constant value here would silently still resolve as a string
        # but the request would fail at HTTP time. Catch the mismatch
        # at unit-test time instead.
        valid = {b.value for b in BrowserType}
        for name in _IMPERSONATE_POOL:
            assert name in valid, (
                f"pool entry {name!r} is not in curl_cffi.requests.BrowserType "
                f"(known: {sorted(valid)[:10]}...)"
            )


class TestPickImpersonate:
    def test_returns_pool_member(self):
        for _ in range(20):
            assert pick_impersonate() in _IMPERSONATE_POOL

    def test_actually_rotates(self):
        # 500 picks against a 5-entry pool should sample every entry
        # with overwhelming probability (chance of missing one is
        # (4/5)^500 ~= 10^-49). If we ever return the same value 500
        # times, rotation regressed to a constant.
        seen = Counter(pick_impersonate() for _ in range(500))
        assert len(seen) == len(_IMPERSONATE_POOL), (
            f"expected all {len(_IMPERSONATE_POOL)} pool members; got {seen}"
        )
        # Each member should appear roughly 100 times (500/5); allow a
        # generous +/- 40% band so test isn't flaky on biased RNG seeds.
        expected = 500 / len(_IMPERSONATE_POOL)
        for name, count in seen.items():
            assert 0.6 * expected < count < 1.4 * expected, (
                f"rotation imbalance: {name} appeared {count} times (expected ~{int(expected)})"
            )
