"""Integrity tests for the macro series registry.

The registry is the single source of truth for the whole dashboard, so these guard
the invariants the rest of the stack assumes rather than any computation: unique
keys, resolvable lookups, and — the one that actually bites — that no granular BLS
item is ever routed to FRED, which 404s on all of them.
"""
from __future__ import annotations

import pytest

from gutter_macro.series import (
    ALL_SERIES,
    GAS_CURVE_CONTRACTS,
    REGISTRY,
    Freq,
    Panel,
    Source,
    by_key,
    by_panel,
    by_source,
)


def test_keys_are_unique():
    keys = [s.key for s in ALL_SERIES]
    assert len(keys) == len(set(keys)), "duplicate key in registry"


def test_source_ids_are_unique_within_a_source():
    seen: set[tuple[Source, str]] = set()
    for s in ALL_SERIES:
        pair = (s.source, s.source_id)
        assert pair not in seen, f"duplicate {pair}"
        seen.add(pair)


def test_every_key_resolves():
    for s in ALL_SERIES:
        assert by_key(s.key) is s


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        by_key("no_such_series")


def test_every_series_lands_in_exactly_one_panel():
    from_panels = [s for p in Panel for s in by_panel(p)]
    assert sorted(x.key for x in from_panels) == sorted(x.key for x in ALL_SERIES)


def test_every_series_lands_in_exactly_one_source():
    from_sources = [s for src in Source for s in by_source(src)]
    assert sorted(x.key for x in from_sources) == sorted(x.key for x in ALL_SERIES)


def test_no_panel_is_empty():
    """An empty panel renders as a blank card with a title and no explanation."""
    for panel in Panel:
        assert by_panel(panel), f"panel {panel.value} has no series"


@pytest.mark.parametrize("series_id", [
    "CUUR0000SEEE03",   # internet services
    "CUUR0000SEEE02",   # computer software
    "CUUR0000SS68023",  # tax prep
    "PCU541110541110",  # PPI offices of lawyers
    "CES2023800003",    # specialty trade contractor wages
])
def test_granular_bls_items_are_not_routed_to_fred(series_id):
    """All of these return HTTP 404 from fredgraph. Routing one to FRED would fail
    the whole nightly build, so the registry must keep them on Source.BLS."""
    spec = next(s for s in ALL_SERIES if s.source_id == series_id)
    assert spec.source is Source.BLS


def test_nsa_granular_items_are_not_marked_seasonal():
    """BLS publishes no seasonally adjusted version of these. Claiming SA would put a
    misleading label on the chart."""
    for series_id in ("CUUR0000SEEE03", "CUUR0000SEEE02", "CUUR0000SS68023"):
        spec = next(s for s in ALL_SERIES if s.source_id == series_id)
        assert not spec.seasonal


def test_sa_cpi_aggregates_use_the_cusr_prefix():
    """CUSR = seasonally adjusted, CUUR = not. A spec claiming SA while pointing at a
    CUUR id is a silent mislabel."""
    for spec in by_source(Source.BLS):
        if spec.source_id.startswith("CU"):
            assert spec.seasonal == spec.source_id.startswith("CUSR")


def test_every_series_documents_units():
    for s in ALL_SERIES:
        assert s.units, f"{s.key} has no units"


def test_key_caveats_are_documented():
    """These notes are rendered as dashboard footnotes. Losing them means shipping a
    chart that invites the wrong read."""
    assert "hedonic" in by_key("cpi_software").note
    assert "CUUR0000SEGD01" in by_key("ppi_legal").note        # why the substitution
    assert "delivery year" in by_key("pjm_bra_clearing_price").note
    assert "cap" in by_key("ercot_north_hub_da").note.lower()  # $5,000/MWh cap


def test_gas_curve_has_four_ordered_contracts():
    assert [s.key for s in GAS_CURVE_CONTRACTS] == [f"henry_hub_c{n}" for n in (1, 2, 3, 4)]
    assert all(s.source is Source.EIA for s in GAS_CURVE_CONTRACTS)


def test_all_series_is_registry_plus_curve():
    assert len(ALL_SERIES) == len(REGISTRY) + len(GAS_CURVE_CONTRACTS)


def test_keyed_sources_are_flagged_in_notes():
    """Anything needing a credential says so, so a missing key is diagnosable from
    the registry alone."""
    for spec in ALL_SERIES:
        if spec.source in (Source.EIA, Source.PJM, Source.ERCOT):
            assert "KEY" in spec.note.upper(), f"{spec.key} does not name its API key"


def test_frequencies_are_plausible_for_their_source():
    for spec in by_source(Source.BLS):
        assert spec.freq is Freq.MONTHLY, f"{spec.key}: BLS publishes monthly"
    assert by_key("pjm_bra_clearing_price").freq is Freq.ANNUAL
