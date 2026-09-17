"""Tests for the dashboard payload assembly. Pure — no network, no disk, no clock.

The build step is where a wrong answer looks most convincing, because the output is
a chart rather than a number. These pin the decisions that would otherwise silently
mislead: which window the charts cover, what "year over year" is allowed to compare
against, and what happens to a series that stopped updating.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from gutter_macro import build as B
from gutter_macro.series import Panel

FIXED_NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)


def monthly(start: str, values: list[float]) -> pd.DataFrame:
    dates = pd.date_range(start, periods=len(values), freq="MS")
    return pd.DataFrame({"date": dates, "value": values})


def assemble(frames, **kw):
    kw.setdefault("generated_at", FIXED_NOW)
    return B.assemble(frames, **kw)


def find(payload: dict, key: str) -> dict | None:
    for panel in payload["panels"]:
        for s in panel["series"]:
            if s["key"] == key:
                return s
    return None


# ------------------------------------------------------------------- shape


def test_every_panel_appears_even_when_empty():
    """A panel that silently disappears looks like it was never meant to exist."""
    payload = assemble({})
    assert [p["key"] for p in payload["panels"]] == [p.value for p in Panel]


def test_panels_carry_title_and_blurb():
    payload = assemble({})
    for panel in payload["panels"]:
        assert panel["title"]
        assert panel["blurb"]


def test_generated_at_is_deterministic_when_injected():
    payload = assemble({}, generated_at=FIXED_NOW)
    assert payload["generated_at"] == "2026-09-17T12:00:00+00:00"


def test_status_is_passed_through():
    payload = assemble({}, status={"henry_hub_c1": "skipped: EIA_API_KEY not set"})
    assert payload["status"]["henry_hub_c1"].startswith("skipped")


def test_series_entry_carries_registry_metadata():
    payload = assemble({"cpi_electricity": monthly("2026-01-01", [1.0, 2.0])})
    entry = find(payload, "cpi_electricity")

    assert entry["label"] == "Electricity"
    assert entry["units"]
    assert entry["note"]
    assert entry["source"] == "bls"
    assert entry["source_id"] == "CUSR0000SEHF01"
    assert entry["seasonal"] is True


# ------------------------------------------------------------------ window


def test_window_anchors_on_newest_observation_not_today():
    """BEA lags a quarter. Anchoring on the wall clock would silently drop real data
    from the near end of every lagging series."""
    frames = {"cpi_electricity": monthly("2020-01-01", list(range(60)))}  # ends 2024-12
    payload = assemble(frames, history_years=3)

    assert payload["start_date"] == "2021-12-01"


def test_explicit_today_overrides_the_anchor():
    frames = {"cpi_electricity": monthly("2020-01-01", list(range(60)))}
    payload = assemble(frames, history_years=3, today=pd.Timestamp("2026-09-17"))

    assert payload["start_date"] == "2023-09-17"


def test_points_outside_the_window_are_dropped():
    frames = {"cpi_electricity": monthly("2020-01-01", [float(i) for i in range(72)])}
    payload = assemble(frames, history_years=3)
    entry = find(payload, "cpi_electricity")

    assert entry["points"][0][0] >= payload["start_date"]


def test_history_years_controls_the_window_width():
    frames = {"cpi_electricity": monthly("2015-01-01", [float(i) for i in range(140)])}
    one = assemble(frames, history_years=1)
    five = assemble(frames, history_years=5)

    assert len(find(one, "cpi_electricity")["points"]) < len(
        find(five, "cpi_electricity")["points"]
    )


def test_stale_series_keeps_a_stub_and_is_flagged():
    """A series that stopped years ago must not vanish without comment — an empty
    chart reads as 'no data collected', which is a different claim."""
    frames = {
        "cpi_electricity": monthly("2026-01-01", [1.0] * 8),      # current
        "cpi_shelter": monthly("2015-01-01", [1.0, 2.0, 3.0]),    # long dead
    }
    payload = assemble(frames, history_years=3)
    dead = find(payload, "cpi_shelter")

    assert dead["stale"] is True
    assert len(dead["points"]) == 1
    assert find(payload, "cpi_electricity")["stale"] is False


# ----------------------------------------------------------------- changes


def test_yoy_uses_the_observation_twelve_months_back():
    frames = {"cpi_electricity": monthly("2025-01-01", [100.0] * 12 + [110.0])}
    entry = find(assemble(frames), "cpi_electricity")

    assert entry["change"]["yoy"] == pytest.approx(10.0)


def test_three_year_change_is_computed():
    values = [100.0] + [100.0] * 35 + [150.0]
    frames = {"cpi_electricity": monthly("2023-01-01", values)}
    entry = find(assemble(frames), "cpi_electricity")

    assert entry["change"]["three_year"] == pytest.approx(50.0)


def test_yoy_is_none_when_history_is_too_short():
    """Better an absent number than a percentage computed off the series' first
    point, which would read as a real year-over-year move."""
    frames = {"cpi_electricity": monthly("2026-06-01", [100.0, 101.0, 102.0])}
    entry = find(assemble(frames), "cpi_electricity")

    assert entry["change"]["yoy"] is None


def test_yoy_rejects_a_reference_point_far_from_a_year_ago():
    """A series with a multi-year gap must not compare against whatever happens to
    be the nearest prior point."""
    df = pd.DataFrame({
        "date": pd.to_datetime(["2019-01-01", "2026-08-01"]),
        "value": [100.0, 200.0],
    })
    entry = find(assemble({"cpi_electricity": df}), "cpi_electricity")

    assert entry["change"]["yoy"] is None


def test_quarterly_series_still_gets_a_yoy():
    """BEA data lands quarterly; the tolerance must accommodate that spacing."""
    dates = pd.date_range("2023-01-01", periods=15, freq="QS")
    df = pd.DataFrame({"date": dates, "value": [100.0] * 14 + [120.0]})
    entry = find(assemble({"bea_software_nominal": df}), "bea_software_nominal")

    assert entry["change"]["yoy"] == pytest.approx(20.0)


def test_zero_reference_does_not_divide_by_zero():
    frames = {"cpi_electricity": monthly("2025-01-01", [0.0] * 12 + [5.0])}
    entry = find(assemble(frames), "cpi_electricity")

    assert entry["change"]["yoy"] is None


def test_nan_values_are_dropped_before_charting():
    """BLS '-' becomes NaN upstream; JSON cannot carry it and a chart cannot plot it."""
    df = monthly("2026-01-01", [1.0, float("nan"), 3.0])
    entry = find(assemble({"cpi_electricity": df}), "cpi_electricity")

    assert [v for _, v in entry["points"]] == [1.0, 3.0]


def test_latest_matches_the_final_point():
    frames = {"cpi_electricity": monthly("2026-01-01", [1.0, 2.0, 7.5])}
    entry = find(assemble(frames), "cpi_electricity")

    assert entry["latest"] == entry["points"][-1]
    assert entry["change"]["latest"] == pytest.approx(7.5)


# ---------------------------------------------------------------- capacity


def capacity_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "delivery_year": [2024, 2025, 2028],
        "delivery_year_label": ["2024/2025", "2025/2026", "2028/2029"],
        "product_type": ["CP", "CP", "CP"],
        "price": [28.92, 269.92, 325.0],
        "comparable_to_prior": [True, True, True],
    })


def test_capacity_block_is_separate_from_the_time_series_panels():
    payload = assemble({}, capacity=capacity_frame())

    assert payload["capacity"]["auctions"][1]["price"] == 269.92
    assert payload["capacity"]["units"] == "$/MW-day"


def test_capacity_absent_is_none_not_empty_dict():
    assert assemble({})["capacity"] is None


def test_capacity_preserves_the_comparability_flag():
    frame = capacity_frame()
    frame.loc[1, "comparable_to_prior"] = False
    payload = assemble({}, capacity=frame)

    assert payload["capacity"]["auctions"][1]["comparable_to_prior"] is False


def test_capacity_is_not_reported_missing_from_its_panel():
    """It renders from its own block; listing it as missing would be a false alarm."""
    payload = assemble({}, capacity=capacity_frame())
    panel = next(p for p in payload["panels"] if p["key"] == "capacity")

    assert panel["missing"] == []


# ------------------------------------------------------------- serializable


def test_payload_is_json_serializable():
    """numpy scalars from pandas are not, and the failure only shows up at write."""
    import json

    payload = assemble(
        {"cpi_electricity": monthly("2026-01-01", [1.0, 2.0])},
        capacity=capacity_frame(),
        status={"x": "ok"},
    )
    json.loads(json.dumps(payload))


def test_missing_lists_name_unfetched_series():
    payload = assemble({})
    gas = next(p for p in payload["panels"] if p["key"] == "gas")

    assert "henry_hub_c1" in gas["missing"]


# ------------------------------------------------------------------ ranges


def test_payload_advertises_the_available_ranges():
    payload = assemble({}, history_years=10)
    assert payload["ranges"] == [1, 3, 5, 10]


def test_ranges_wider_than_the_build_are_not_offered():
    """A 5-year build must not offer a 10-year button that has no data behind it."""
    payload = assemble({}, history_years=5)
    assert payload["ranges"] == [1, 3, 5]


def test_default_range_is_clamped_to_what_was_built():
    payload = assemble({}, history_years=1, default_range=3)
    assert payload["default_range"] == 1


def test_default_range_is_three_years():
    payload = assemble({}, history_years=10)
    assert payload["default_range"] == 3


def test_latest_date_anchors_the_client_window():
    """The client windows relative to this, not its own clock, so a viewer in
    another timezone sees the same window the data supports."""
    frames = {
        "cpi_electricity": monthly("2026-01-01", [1.0] * 8),     # ends 2026-08
        "henry_hub_spot": monthly("2026-01-01", [1.0] * 9),      # ends 2026-09
    }
    payload = assemble(frames, history_years=10)

    assert payload["latest_date"] == "2026-09-01"


def test_latest_date_is_none_without_data():
    assert assemble({})["latest_date"] is None


def test_payload_carries_the_widest_range_not_the_default():
    """Windowing server-side would need a rebuild per range, and would break the
    year-over-year view at 1 year."""
    frames = {"cpi_electricity": monthly("2014-01-01", [float(i) for i in range(150)])}
    payload = assemble(frames, history_years=10)
    entry = find(payload, "cpi_electricity")

    span = pd.Timestamp(entry["points"][-1][0]) - pd.Timestamp(entry["points"][0][0])
    assert span.days > 9 * 365


# ------------------------------------------------------------ per-series span


def test_series_reports_its_earliest_shipped_point():
    frames = {"cpi_electricity": monthly("2020-03-01", [1.0] * 40)}
    entry = find(assemble(frames, history_years=10), "cpi_electricity")

    assert entry["earliest"] == "2020-03-01"
    assert entry["earliest"] == entry["points"][0][0]


def test_span_measures_shipped_points_not_upstream_history():
    """The client can only window what it was sent. Reporting 30 years of upstream
    history for a series shipped with 10 would make the page claim coverage it
    cannot draw."""
    frames = {"cpi_electricity": monthly("1990-01-01", [float(i) for i in range(440)])}
    entry = find(assemble(frames, history_years=10), "cpi_electricity")

    assert entry["span_years"] <= 10.1
    assert entry["span_years"] >= 9.5


def test_span_of_a_single_point_series_is_zero():
    frames = {"cpi_electricity": monthly("2026-08-01", [1.0])}
    entry = find(assemble(frames, history_years=10), "cpi_electricity")

    assert entry["span_years"] == 0.0


def test_quarterly_series_span_reflects_its_publication_lag():
    """BEA publishes a quarter behind, so its span inside a 10-year window is
    legitimately a few months short. This is lag, not missing history — the page
    must not flag it, which is why the client tests `earliest`, not this."""
    dates = pd.date_range("2016-10-01", periods=39, freq="QS")
    frames = {
        "bea_software_nominal": pd.DataFrame({"date": dates, "value": [1.0] * 39}),
        "henry_hub_spot": monthly("2026-09-01", [1.0]),
    }
    entry = find(assemble(frames, history_years=10), "bea_software_nominal")

    assert 9.0 < entry["span_years"] < 10.0
