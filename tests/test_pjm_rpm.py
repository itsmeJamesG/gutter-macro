"""Tests for the PJM RPM capacity-auction parser. No network — workbooks are built
in memory, and one fixture is the real file if it has been cached locally.

These pin the layout quirks observed in PJM's actual spreadsheet, every one of which
produces a plausible-looking but wrong chart if mishandled: the two spellings of the
delivery-year header, "**" for an unmodeled zone, cancelled auctions carrying text in
the price-adjacent column, float dust in the stored values, and the two product-
definition changes that make a straight line across them a lie.
"""
from __future__ import annotations

import io
from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from gutter_macro.sources import pjm_rpm

# PJM's actual published workbook, downloaded 2026-09-17 and committed so this
# suite keeps testing the real layout rather than only our idea of it.
REAL_FILE = Path(__file__).parent / "fixtures" / "pjm_rpm_clearing_prices_2026-09-17.xlsx"


def workbook(rows: list[list]) -> bytes:
    """Build an .xlsx with PJM's shape: title row, zone header, then blocks."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "RCP"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEADER = [
    ["Resource Clearing Prices for RPM Auctions"],
    [None, "Capacity Product Type *", "RTO", "MAAC", "EMAAC"],
]


def simple(*blocks: list) -> bytes:
    return workbook([*HEADER, *blocks])


# ------------------------------------------------------------------ headers


@pytest.mark.parametrize("label,expected_year,expected_text", [
    ("DY 07/08", 2007, "2007/2008"),
    ("DY27/28", 2027, "2027/2028"),      # no space — both spellings are in the file
    ("DY 28/29", 2028, "2028/2029"),
    ("DY99/00", 2099, "2099/2000"),      # century logic is naive by design; documents it
])
def test_delivery_year_header_spellings(label, expected_year, expected_text):
    df = pjm_rpm.parse(simple([label], ["BRA", "CP", 100.0]))

    assert df.delivery_year.iloc[0] == expected_year
    assert df.delivery_year_label.iloc[0] == expected_text


def test_delivery_year_is_the_starting_year():
    """Auction dates are irregular post-2022, so the x-axis must be delivery year."""
    df = pjm_rpm.parse(simple(["DY 25/26"], ["BRA", "CP", 269.92]))

    assert df.delivery_year.iloc[0] == 2025


def test_rows_before_any_delivery_year_are_ignored():
    df = pjm_rpm.parse(simple(["BRA", "CP", 999.0], ["DY 25/26"], ["BRA", "CP", 269.92]))

    assert list(df.price) == [269.92]


# ------------------------------------------------------------------- values


def test_double_asterisk_zone_is_null_not_zero():
    """'**' means the LDA was not modeled. As 0.0 it would draw a zone crashing."""
    df = pjm_rpm.parse(simple(["DY 25/26"], ["BRA", "CP", 269.92, " ** ", 300.0]))

    assert set(df.zone) == {"RTO", "EMAAC"}
    assert "MAAC" not in set(df.zone)


def test_cancelled_auction_is_skipped_entirely():
    """1IA/2IA were cancelled for years; the notice sits in the product column."""
    df = pjm_rpm.parse(simple(
        ["DY 25/26"],
        ["BRA", "CP", 269.92],
        ["1IA", "***   AUCTION CANCELLED"],
        ["2IA", "***   AUCTION CANCELLED"],
    ))

    assert set(df.auction) == {"BRA"}


def test_float_dust_is_rounded_to_cents():
    """PJM stores 95.78999999999999; the published price is $95.79."""
    df = pjm_rpm.parse(simple(["DY 22/23"], ["BRA", "CP", 95.78999999999999]))

    assert df.price.iloc[0] == 95.79


@pytest.mark.parametrize("cell,expected", [
    (269.92, 269.92),
    ("269.92", 269.92),
    ("$269.92", 269.92),
    ("1,269.92", 1269.92),
    ("**", None),
    ("*", None),
    ("  ", None),
    (None, None),
    ("AUCTION CANCELLED", None),
    (True, None),          # bool is an int subclass; must not become 1.0
])
def test_price_cell_parsing(cell, expected):
    assert pjm_rpm._price(cell) == expected


def test_zero_price_survives():
    """A genuine $0 clearing price must not be confused with a null cell."""
    df = pjm_rpm.parse(simple(["DY 25/26"], ["BRA", "CP", 0.0]))

    assert df.price.iloc[0] == 0.0


# ---------------------------------------------------------------- selection


def test_incremental_auctions_are_kept_but_separable():
    df = pjm_rpm.parse(simple(
        ["DY 25/26"], ["BRA", "CP", 269.92], ["3IA", "CP", 323.9],
    ))

    assert set(df.auction) == {"BRA", "3IA"}
    assert list(pjm_rpm.rto_base_residual(df).price) == [269.92]


def test_headline_picks_cp_over_transition_subproducts():
    """2018/19 quotes CP alongside BASE GEN and BASE DR/EE. CP is the headline."""
    df = pjm_rpm.parse(simple(
        ["DY 18/19"],
        ["BRA", "CP", 164.77],
        ["BRA", "BASE GEN", 149.98],
        ["BRA", "BASE DR/EE", 149.98],
    ))
    head = pjm_rpm.headline_rto_bra(df)

    assert len(head) == 1
    assert head.price.iloc[0] == 164.77
    assert head.product_type.iloc[0] == "CP"


def test_headline_picks_annual_in_the_three_product_regime():
    df = pjm_rpm.parse(simple(
        ["DY 15/16"],
        ["BRA", "Annual", 136.00],
        ["BRA", "Ext Summer", 136.00],
        ["BRA", "Limited", 118.54],
    ))
    head = pjm_rpm.headline_rto_bra(df)

    assert len(head) == 1
    assert head.price.iloc[0] == 136.00


def test_headline_is_one_row_per_delivery_year():
    df = pjm_rpm.parse(simple(
        ["DY 15/16"], ["BRA", "Annual", 136.0], ["BRA", "Limited", 118.54],
        ["DY 25/26"], ["BRA", "CP", 269.92],
    ))
    head = pjm_rpm.headline_rto_bra(df)

    assert list(head.delivery_year) == [2015, 2025]


def test_product_change_is_flagged_as_not_comparable():
    """Annual -> CP is a definitional break. Drawing one continuous line across it
    implies a price move that did not happen."""
    df = pjm_rpm.parse(simple(
        ["DY 17/18"], ["BRA", "Annual", 120.0],
        ["DY 18/19"], ["BRA", "CP", 164.77],
        ["DY 19/20"], ["BRA", "CP", 100.0],
    ))
    head = pjm_rpm.headline_rto_bra(df)

    assert list(head.comparable_to_prior) == [True, False, True]


# ----------------------------------------------------------------- failures


def test_empty_workbook_raises():
    with pytest.raises(pjm_rpm.PJMRPMError):
        pjm_rpm.parse(workbook([]))


def test_missing_zone_header_raises():
    """If PJM restructures the sheet we want a loud failure, not empty charts."""
    body = workbook([["something else"], [None, "Product", "Region"], ["DY 25/26"]])
    with pytest.raises(pjm_rpm.PJMRPMError, match="zone header"):
        pjm_rpm.parse(body)


def test_header_present_but_no_prices_raises():
    with pytest.raises(pjm_rpm.PJMRPMError, match="no prices"):
        pjm_rpm.parse(simple(["DY 25/26"], ["1IA", "***   AUCTION CANCELLED"]))


def test_garbage_bytes_raise_a_typed_error():
    with pytest.raises(pjm_rpm.PJMRPMError, match="Could not open"):
        pjm_rpm.parse(b"not a workbook")


def test_http_error_raises():
    class Resp:
        status_code = 503
        content = b""

    class Sess:
        def get(self, url, timeout=None):
            return Resp()

    with pytest.raises(pjm_rpm.PJMRPMError, match="HTTP 503"):
        pjm_rpm.fetch(session=Sess())


# ------------------------------------------------- against the real file

@pytest.mark.skipif(not REAL_FILE.exists(), reason="real PJM workbook not cached")
def test_real_workbook_known_values():
    """Values read off PJM's published spreadsheet on 2026-09-17. If PJM restates a
    historical auction or changes the layout, this fails loudly."""
    head = pjm_rpm.headline_rto_bra(pjm_rpm.parse(REAL_FILE.read_bytes()))
    prices = dict(zip(head.delivery_year, head.price))

    assert prices[2024] == 28.92
    assert prices[2025] == 269.92      # the +833% datacenter repricing
    assert prices[2026] == 329.17
    assert prices[2027] == 333.44
    assert prices[2028] == 325.00      # first decline after the spike
    assert head.delivery_year.is_monotonic_increasing
    assert head.delivery_year.is_unique
