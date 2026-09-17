"""Tests for the keyed power/gas sources. No network — all the pure aggregation
halves are exercised with synthetic API payloads in the documented response shapes.

These modules cannot yet be run against their live APIs (they need credentials), so
the tests deliberately cover the *transformation*, which is where a wrong answer is
invisible: a peak/off-peak window off by an hour, ERCOT's positional-array rows
zipped in the wrong order, or a mean quoted for a market whose daily mean is
dominated by a few scarcity hours.
"""
from __future__ import annotations

import pandas as pd
import pytest

from gutter_macro.sources import eia, ercot, pjm


# =========================================================================
# PJM
# =========================================================================


def pjm_rows(day: str, prices: dict[int, float]) -> list[dict]:
    return [
        {
            "datetime_beginning_ept": f"{day}T{h:02d}:00:00",
            "pnode_id": 51217,
            "pnode_name": "WESTERN HUB",
            "total_lmp_da": p,
        }
        for h, p in prices.items()
    ]


def test_pjm_daily_mean_over_all_hours():
    rows = pjm_rows("2026-09-16", {h: float(h) for h in range(24)})
    out = pjm.to_daily(rows)

    assert len(out) == 1
    assert out.value.iloc[0] == pytest.approx(11.5)   # mean of 0..23


def test_pjm_peak_window_is_hours_ending_8_to_23_on_weekdays():
    """2026-09-16 is a Wednesday. Peak is hour-beginning 7..22 — 16 hours."""
    rows = pjm_rows("2026-09-16", {h: (100.0 if 7 <= h <= 22 else 20.0) for h in range(24)})
    out = pjm.to_daily(rows)

    assert out.on_peak.iloc[0] == pytest.approx(100.0)
    assert out.off_peak.iloc[0] == pytest.approx(20.0)


def test_pjm_weekend_has_no_peak_hours():
    """2026-09-19 is a Saturday: every hour is off-peak under PJM's convention."""
    rows = pjm_rows("2026-09-19", {h: 50.0 for h in range(24)})
    out = pjm.to_daily(rows)

    assert pd.isna(out.on_peak.iloc[0])
    assert out.off_peak.iloc[0] == pytest.approx(50.0)


def test_pjm_reposted_hour_keeps_the_later_record():
    """PJM reposts corrected hours; the stale value must not skew the mean."""
    rows = pjm_rows("2026-09-16", {0: 10.0})
    rows.append({**rows[0], "total_lmp_da": 99.0})
    out = pjm.to_daily(rows)

    assert out.value.iloc[0] == pytest.approx(99.0)


def test_pjm_negative_prices_survive():
    """Negative LMPs are real and routinely occur overnight in high-wind hours."""
    rows = pjm_rows("2026-09-16", {0: -12.5, 1: 12.5})
    out = pjm.to_daily(rows)

    assert out.value.iloc[0] == pytest.approx(0.0)


def test_pjm_missing_fields_raise_rather_than_silently_empty():
    with pytest.raises(pjm.PJMError, match="missing expected fields"):
        pjm.to_daily([{"datetime_beginning_ept": "2026-09-16T00:00:00"}])


def test_pjm_empty_rows_give_an_empty_typed_frame():
    out = pjm.to_daily([])
    assert out.empty
    assert list(out.columns) == ["date", "value", "on_peak", "off_peak"]


def test_pjm_unparseable_prices_are_dropped():
    rows = pjm_rows("2026-09-16", {0: 10.0})
    rows.append({**rows[0], "datetime_beginning_ept": "2026-09-16T01:00:00",
                 "total_lmp_da": "n/a"})
    out = pjm.to_daily(rows)

    assert out.value.iloc[0] == pytest.approx(10.0)


def test_pjm_year_windows_tile_the_range_without_overlap():
    """Windows must cover [start, end) exactly: no gap that silently drops a day,
    no overlap that double-counts one."""
    start, end = pd.Timestamp("2023-09-17"), pd.Timestamp("2026-09-18")
    windows = pjm._year_windows(start, end)

    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, prev_hi), (next_lo, _) in zip(windows, windows[1:]):
        assert next_lo - prev_hi == pd.Timedelta(minutes=1)


def test_pjm_window_includes_today():
    """Day-ahead prices for today were published yesterday. An upper bound at
    today's midnight would drop them."""
    start, end = pd.Timestamp("2026-09-16"), pd.Timestamp("2026-09-18")
    windows = pjm._year_windows(start, end)

    assert windows[-1][1] >= pd.Timestamp("2026-09-17 23:59")


# =========================================================================
# ERCOT
# =========================================================================


def ercot_body(rows: list[list]) -> dict:
    return {
        "fields": [
            {"name": "deliveryDate"},
            {"name": "hourEnding"},
            {"name": "settlementPoint"},
            {"name": "settlementPointPrice"},
        ],
        "data": rows,
        "_meta": {"totalPages": 1},
    }


def test_ercot_rows_are_zipped_by_declared_field_order():
    """Rows are positional arrays. Assuming an order rather than reading `fields`
    would silently swap price and hour."""
    body = ercot_body([["2026-09-16", "01:00", "HB_NORTH", 32.5]])
    rows = ercot._rows_from(body)

    assert rows[0]["settlementPointPrice"] == 32.5
    assert rows[0]["hourEnding"] == "01:00"


def test_ercot_accepts_object_rows_too():
    body = {"data": [{"deliveryDate": "2026-09-16", "settlementPointPrice": 1.0}]}
    assert ercot._rows_from(body)[0]["settlementPointPrice"] == 1.0


def test_ercot_reports_mean_and_median_separately():
    """ERCOT is energy-only with a $5,000 cap. One scarcity hour drags the mean far
    from the typical hour, so the median has to travel alongside it."""
    rows = [
        {"deliveryDate": "2026-09-16", "hourEnding": f"{h:02d}:00",
         "settlementPointPrice": 5000.0 if h == 18 else 30.0}
        for h in range(1, 25)
    ]
    out = ercot.to_daily(rows)

    assert out.value.iloc[0] > 200        # mean dragged up by the spike
    assert out["median"].iloc[0] == pytest.approx(30.0)


@pytest.mark.parametrize("raw,expected", [
    ("01:00", 1),
    ("0100", 100),          # documents the alternate spelling's behaviour
    ("24:00", 24),
    ("02:00 DST", 2),
    (13, 13),
])
def test_ercot_hour_ending_parsing(raw, expected):
    assert ercot._hour_ending(pd.Series([raw])).iloc[0] == expected


def test_ercot_peak_window_includes_saturday_but_not_sunday():
    """ERCOT's on-peak convention differs from PJM's: HE 7-22, Mon-Sat."""
    def day(d):
        return [
            {"deliveryDate": d, "hourEnding": f"{h:02d}:00",
             "settlementPointPrice": 100.0 if 7 <= h <= 22 else 10.0}
            for h in range(1, 25)
        ]

    sat = ercot.to_daily(day("2026-09-19"))   # Saturday
    sun = ercot.to_daily(day("2026-09-20"))   # Sunday

    assert sat.on_peak.iloc[0] == pytest.approx(100.0)
    assert pd.isna(sun.on_peak.iloc[0])


def test_ercot_missing_price_column_raises():
    with pytest.raises(ercot.ERCOTError, match="missing date/price"):
        ercot.to_daily([{"deliveryDate": "2026-09-16"}])


def test_ercot_requires_username_and_password():
    """The failure has to name all three credentials, not just the subscription key."""
    with pytest.raises(ercot.ERCOTError, match="ERCOT_USERNAME"):
        ercot.fetch_hub_daily("subkey", username=None, password=None)


def test_ercot_empty_frame_is_typed():
    out = ercot.to_daily([])
    assert list(out.columns) == ["date", "value", "on_peak", "off_peak", "median"]


# =========================================================================
# EIA
# =========================================================================


def eia_rows(pairs: list[tuple[str, str, float]]) -> list[dict]:
    return [{"period": d, "series": s, "value": v, "units": "$/MMBTU"} for d, s, v in pairs]


def test_eia_normalises_to_the_shared_tidy_shape():
    out = eia.to_frame(eia_rows([("2026-09-15", "RNGC1", 2.97)]))

    assert list(out.columns) == ["date", "series_id", "value"]
    assert out.date.iloc[0] == pd.Timestamp("2026-09-15")


def test_eia_accepts_a_renamed_series_column():
    rows = [{"period": "2026-09-15", "seriesId": "RNGC1", "value": 2.97}]
    assert eia.to_frame(rows).series_id.iloc[0] == "RNGC1"


def test_eia_unknown_series_column_names_the_probe():
    rows = [{"period": "2026-09-15", "mystery": "RNGC1", "value": 2.97}]
    with pytest.raises(eia.EIAError, match="probe_route"):
        eia.to_frame(rows)


def test_eia_latest_curve_needs_every_contract_on_one_day():
    """A curve stitched from different dates shows a shape that never existed."""
    rows = eia_rows([
        ("2026-09-14", "RNGC1", 2.9), ("2026-09-14", "RNGC2", 3.0),
        ("2026-09-15", "RNGC1", 2.97),          # RNGC2 has not settled yet
    ])
    curve = eia.latest_curve(eia.to_frame(rows))

    assert set(curve.date) == {pd.Timestamp("2026-09-14")}
    assert len(curve) == 2


def test_eia_latest_curve_is_ordered_by_contract():
    rows = eia_rows([
        ("2026-09-15", "RNGC2", 3.1), ("2026-09-15", "RNGC1", 2.97),
    ])
    curve = eia.latest_curve(eia.to_frame(rows))

    assert list(curve.series_id) == ["RNGC1", "RNGC2"]


def test_eia_latest_curve_of_empty_is_empty():
    assert eia.latest_curve(eia.to_frame([])).empty


def test_eia_duplicate_observations_collapse():
    rows = eia_rows([("2026-09-15", "RNGC1", 2.9), ("2026-09-15", "RNGC1", 2.97)])
    out = eia.to_frame(rows)

    assert len(out) == 1
    assert out.value.iloc[0] == pytest.approx(2.97)


def test_eia_missing_period_column_raises():
    with pytest.raises(eia.EIAError, match="period"):
        eia.to_frame([{"series": "RNGC1", "value": 2.97}])
