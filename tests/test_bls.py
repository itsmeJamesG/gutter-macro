"""Tests for the BLS fetcher. No network — the session is injected.

These pin down the four payload quirks that were observed live against api.bls.gov
while building the registry, each of which fails silently rather than loudly:
a "-" value inside an otherwise valid observation, the M13 annual average posing as
a month, semiannual S0x periods, and a missing series reported only in `message`
while the overall status is still REQUEST_SUCCEEDED.
"""
from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from gutter_macro.sources import bls


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class FakeSession:
    """Records every POST and replays a queue of canned bodies."""

    def __init__(self, *bodies: dict):
        self.bodies = list(bodies)
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "payload": json.loads(data)})
        body = self.bodies.pop(0) if self.bodies else _body()
        return FakeResponse(body) if not isinstance(body, FakeResponse) else body


def _series(sid: str, data: list[dict]) -> dict:
    return {"seriesID": sid, "data": data}


def _obs(year: str, period: str, value: str) -> dict:
    return {"year": year, "period": period, "periodName": period, "value": value}


def _body(*series: dict, message: list | None = None, status: str = "REQUEST_SUCCEEDED") -> dict:
    return {
        "status": status,
        "message": message or [],
        "Results": {"series": list(series)},
    }


# --------------------------------------------------------------------- values


def test_dash_value_becomes_nan_not_zero():
    """CUUR0000SEGD01 returns a literal '-' for periods with no datum. Treating that
    as 0.0 would draw a price index crashing to zero."""
    sess = FakeSession(_body(_series("X", [_obs("2025", "M01", "-")])))
    df = bls.fetch(["X"], 2025, 2025, api_key="", session=sess)

    assert len(df) == 1
    assert math.isnan(df.iloc[0].value)


@pytest.mark.parametrize("raw,expected", [
    ("305.444", 305.444),
    ("1,234.5", 1234.5),
    (" 40.27 ", 40.27),
    ("-", None),
    ("", None),
    (None, None),
    ("n/a", None),
])
def test_value_parsing(raw, expected):
    got = bls._parse_value(raw)
    if expected is None:
        assert math.isnan(got)
    else:
        assert got == pytest.approx(expected)


# -------------------------------------------------------------------- periods


def test_annual_average_m13_is_dropped():
    """M13 is the annual average. Kept, it would appear as a 13th month and both
    inflate the series and break any month-over-month calculation."""
    sess = FakeSession(_body(_series("X", [
        _obs("2025", "M11", "100.0"),
        _obs("2025", "M12", "101.0"),
        _obs("2025", "M13", "95.0"),
    ])))
    df = bls.fetch(["X"], 2025, 2025, api_key="", session=sess)

    assert len(df) == 2
    assert list(df.date.dt.month) == [11, 12]


def test_semiannual_periods_are_dropped():
    """CUUS series publish S01/S02/S03. They are aggregates, not months."""
    sess = FakeSession(_body(_series("X", [
        _obs("2025", "S01", "417.374"),
        _obs("2025", "M03", "100.0"),
    ])))
    df = bls.fetch(["X"], 2025, 2025, api_key="", session=sess)

    assert len(df) == 1
    assert df.iloc[0].date == pd.Timestamp("2025-03-01")


def test_dates_are_first_of_month():
    """Aligns the monthly index with FRED's convention so the two sources can be
    concatenated without resampling."""
    sess = FakeSession(_body(_series("X", [_obs("2026", "M08", "1.0")])))
    df = bls.fetch(["X"], 2026, 2026, api_key="", session=sess)

    assert df.iloc[0].date == pd.Timestamp("2026-08-01")


# -------------------------------------------------------------------- failures


def test_missing_series_is_absent_not_nan_filled():
    """BLS reports an unavailable series in `message` while status stays
    REQUEST_SUCCEEDED. The caller must be able to tell 'no such series' from
    'series exists but is empty', so it simply does not appear."""
    sess = FakeSession(_body(
        _series("GOOD", [_obs("2025", "M01", "1.0")]),
        message=["No Data Available for Series BAD Year: 2025"],
    ))
    df = bls.fetch(["GOOD", "BAD"], 2025, 2025, api_key="", session=sess)

    assert set(df.series_id) == {"GOOD"}


def test_non_success_status_raises():
    sess = FakeSession(_body(status="REQUEST_NOT_PROCESSED", message=["daily threshold"]))
    with pytest.raises(bls.BLSError, match="REQUEST_NOT_PROCESSED"):
        bls.fetch(["X"], 2025, 2025, api_key="", session=sess)


def test_http_error_raises():
    sess = FakeSession(FakeResponse({}, status_code=500))
    with pytest.raises(bls.BLSError, match="HTTP 500"):
        bls.fetch(["X"], 2025, 2025, api_key="", session=sess)


def test_empty_request_short_circuits():
    sess = FakeSession()
    df = bls.fetch([], 2025, 2025, api_key="", session=sess)

    assert df.empty
    assert sess.calls == []
    assert list(df.columns) == ["date", "series_id", "value"]


def test_backwards_year_range_raises():
    with pytest.raises(ValueError, match="after"):
        bls.fetch(["X"], 2026, 2025, api_key="")


# ---------------------------------------------------------------- api tiering


def test_keyless_uses_v1_and_omits_registration_key():
    sess = FakeSession(_body(_series("X", [_obs("2025", "M01", "1.0")])))
    bls.fetch(["X"], 2025, 2025, api_key="", session=sess)

    assert sess.calls[0]["url"] == bls.V1_URL
    assert "registrationkey" not in sess.calls[0]["payload"]


def test_key_uses_v2_and_sends_registration_key():
    sess = FakeSession(_body(_series("X", [_obs("2025", "M01", "1.0")])))
    bls.fetch(["X"], 2025, 2025, api_key="secret", session=sess)

    assert sess.calls[0]["url"] == bls.V2_URL
    assert sess.calls[0]["payload"]["registrationkey"] == "secret"


def test_v1_chunks_years_into_ten_year_windows():
    """v1 caps a request at 10 years. 2000-2026 is 27 years -> 3 requests."""
    sess = FakeSession(*[_body() for _ in range(5)])
    bls.fetch(["X"], 2000, 2026, api_key="", session=sess)

    spans = [(c["payload"]["startyear"], c["payload"]["endyear"]) for c in sess.calls]
    assert spans == [("2000", "2009"), ("2010", "2019"), ("2020", "2026")]


def test_v2_uses_wider_twenty_year_windows():
    sess = FakeSession(*[_body() for _ in range(5)])
    bls.fetch(["X"], 2000, 2026, api_key="k", session=sess)

    spans = [(c["payload"]["startyear"], c["payload"]["endyear"]) for c in sess.calls]
    assert spans == [("2000", "2019"), ("2020", "2026")]


def test_series_are_batched_to_the_tier_limit():
    """25 series/request on v1. 60 series -> 3 requests, no series lost."""
    ids = [f"S{i:03d}" for i in range(60)]
    sess = FakeSession(*[_body() for _ in range(5)])
    bls.fetch(ids, 2025, 2025, api_key="", session=sess)

    batches = [c["payload"]["seriesid"] for c in sess.calls]
    assert [len(b) for b in batches] == [25, 25, 10]
    assert [s for b in batches for s in b] == ids


def test_duplicate_observations_keep_one_row():
    """Overlapping year windows must not double-count a month."""
    dup = _body(_series("X", [_obs("2025", "M01", "1.0")]))
    sess = FakeSession(dup, dup)
    df = bls.fetch(["X"], 2025, 2025, api_key="", session=sess)
    df2 = pd.concat([df, df]).drop_duplicates(subset=["date", "series_id"])

    assert len(df) == 1
    assert len(df2) == 1


def test_output_is_sorted_by_series_then_date():
    sess = FakeSession(_body(
        _series("B", [_obs("2025", "M03", "3"), _obs("2025", "M01", "1")]),
        _series("A", [_obs("2025", "M02", "2")]),
    ))
    df = bls.fetch(["A", "B"], 2025, 2025, api_key="", session=sess)

    assert list(df.series_id) == ["A", "B", "B"]
    assert list(df.date.dt.month) == [2, 1, 3]
