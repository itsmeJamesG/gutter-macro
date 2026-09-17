"""Tests for the FRED fetcher. No network — the session is injected.

Pins the two shape problems that would otherwise surface as a blank chart: FRED's
"." missing-value marker parsed as a string, and the DATE/observation_date column
rename that FRED made partway through the endpoint's life.
"""
from __future__ import annotations

import pandas as pd
import pytest

from gutter_macro.sources import fred


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        r = self.responses.pop(0)
        return r if isinstance(r, FakeResponse) else FakeResponse(r)


def test_parses_current_observation_date_column():
    csv = "observation_date,DHHNGSP\n2026-09-15,2.97\n"
    df = fred.fetch(["DHHNGSP"], session=FakeSession(csv))

    assert len(df) == 1
    assert df.iloc[0].date == pd.Timestamp("2026-09-15")
    assert df.iloc[0].value == pytest.approx(2.97)


def test_parses_legacy_date_column():
    """FRED served 'DATE' for years; old cached URLs and mirrors still do."""
    csv = "DATE,DHHNGSP\n2020-01-02,2.12\n"
    df = fred.fetch(["DHHNGSP"], session=FakeSession(csv))

    assert df.iloc[0].date == pd.Timestamp("2020-01-02")


def test_dot_missing_values_are_dropped_not_zero():
    """FRED writes '.' on non-trading days. Coerced to 0.0 it would draw gas
    collapsing to zero every holiday."""
    csv = "observation_date,DHHNGSP\n2026-09-14,.\n2026-09-15,2.97\n"
    df = fred.fetch(["DHHNGSP"], session=FakeSession(csv))

    assert len(df) == 1
    assert df.iloc[0].value == pytest.approx(2.97)


def test_dates_are_timezone_naive_to_match_bls():
    """The build step concatenates BLS and FRED frames; a tz-aware index on one side
    makes that concat raise."""
    csv = "observation_date,X\n2026-08-01,1.0\n"
    df = fred.fetch(["X"], session=FakeSession(csv))

    assert df.date.dt.tz is None


def test_404_names_the_bls_fallback():
    """The granular CPI items 404 here. The error has to say where to go instead,
    because the symptom (empty chart) does not."""
    sess = FakeSession(FakeResponse("<html>", status_code=404))
    with pytest.raises(fred.FREDError, match="BLS"):
        fred.fetch(["CUUR0000SEEE03"], session=sess)


def test_unexpected_columns_raise():
    sess = FakeSession("observation_date,SOMETHING_ELSE\n2026-01-01,1\n")
    with pytest.raises(fred.FREDError, match="Unexpected FRED columns"):
        fred.fetch(["X"], session=sess)


def test_multiple_series_are_concatenated_and_sorted():
    sess = FakeSession(
        "observation_date,B\n2026-01-01,2\n",
        "observation_date,A\n2026-01-01,1\n",
    )
    df = fred.fetch(["B", "A"], session=sess)

    assert list(df.series_id) == ["A", "B"]


def test_empty_request_makes_no_calls():
    sess = FakeSession()
    df = fred.fetch([], session=sess)

    assert df.empty
    assert sess.calls == []
    assert list(df.columns) == ["date", "series_id", "value"]
