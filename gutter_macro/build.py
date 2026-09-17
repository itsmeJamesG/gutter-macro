"""Pull every registered series, cache it, and emit the dashboard payload.

Split deliberately into a pure part and an IO part:

    assemble(frames, capacity, ...)   pure — frames in, JSON-ready dict out. Tested
                                      exhaustively, no network, no clock, no disk.
    build(...)                        IO — fetches, caches parquet, writes the JSON.

A source whose credential is missing is *skipped*, not fatal: the dashboard renders
what it has and reports the rest in `status`, so a half-configured deploy shows eight
working panels and an honest note rather than a stack trace.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import series as reg
from .series import Freq, Panel, SeriesSpec, Source

# The parquet cache is written at runtime, so where it lives depends on the
# deployment: beside the package for local work, on the mounted volume in
# production. The image itself is neither writable by the app user nor persistent.
CACHE_DIR = Path(
    os.environ.get("MACRO_CACHE_DIR", Path(__file__).resolve().parent.parent / "data_cache")
)

# Ranges the dashboard offers. The payload always carries the widest of them, and
# the page windows client-side — switching range is then instant and costs no
# request. At 10 years the compact payload is still well under 150 KB.
RANGES: tuple[int, ...] = (1, 3, 5, 10)
MAX_HISTORY_YEARS = max(RANGES)
DEFAULT_RANGE = 3

PANEL_TITLES: dict[Panel, str] = {
    Panel.POWER: "Wholesale power",
    Panel.CAPACITY: "PJM capacity auctions",
    Panel.GAS: "Natural gas",
    Panel.CPI_ENERGY: "Energy & shelter prices",
    Panel.CPI_LABOR: "Labor-intensive services",
    Panel.CPI_SERVICES: "Digital & delivered services",
    Panel.SOFTWARE: "Software investment",
}

PANEL_BLURBS: dict[Panel, str] = {
    Panel.POWER: "What AI's electricity demand costs at the hub.",
    Panel.CAPACITY: "What it costs to promise the grid will be there.",
    Panel.GAS: "The marginal fuel setting the power price.",
    Panel.CPI_ENERGY: "What the demand shock reaches households as.",
    Panel.CPI_LABOR: "Work AI competes for, and work it may replace.",
    Panel.CPI_SERVICES: "Where software prices are already falling.",
    Panel.SOFTWARE: "Spend rising while the price index falls.",
}

# Credentials required per source; every name must be present to attempt a fetch.
# ERCOT needs three: a subscription key plus a username/password for its one-hour
# B2C ID token. An empty tuple means the source needs no credential at all.
REQUIRED_KEYS: dict[Source, tuple[str, ...]] = {
    Source.BLS: (),          # v1 works keyless; BLS_API_KEY only raises the rate limit
    Source.FRED: (),
    Source.PJM_RPM: (),
    Source.EIA: ("EIA_API_KEY",),
    Source.PJM: ("PJM_API_KEY",),
    Source.ERCOT: ("ERCOT_API_KEY", "ERCOT_USERNAME", "ERCOT_PASSWORD"),
}


@dataclass
class BuildResult:
    payload: dict
    status: dict[str, str] = field(default_factory=dict)

    @property
    def ok_count(self) -> int:
        return sum(1 for v in self.status.values() if v == "ok")

    @property
    def problems(self) -> dict[str, str]:
        return {k: v for k, v in self.status.items() if v != "ok"}


# =========================================================================
# Pure assembly
# =========================================================================


def assemble(
    frames: dict[str, pd.DataFrame],
    capacity: pd.DataFrame | None = None,
    status: dict[str, str] | None = None,
    generated_at: datetime | None = None,
    history_years: int = MAX_HISTORY_YEARS,
    today: pd.Timestamp | None = None,
    default_range: int = DEFAULT_RANGE,
) -> dict:
    """Build the dashboard payload from per-series long frames.

    `frames` maps a registry key to a frame with [date, value]. Pure: pass `today`
    and `generated_at` to make output deterministic in tests.

    The payload carries `history_years` of data — the widest range on offer — and
    the page narrows it. Windowing server-side would mean a rebuild per range and
    would break the year-over-year view at the 1-year range, which needs a year of
    data *before* the window opens to compute its first point.
    """
    status = dict(status or {})
    now = generated_at or datetime.now(timezone.utc)
    cutoff = _cutoff(frames, history_years, today)
    anchor = _anchor(frames, today)

    panels = []
    for panel in Panel:
        specs = reg.by_panel(panel)
        entries = [
            _series_entry(spec, frames[spec.key], cutoff)
            for spec in specs
            if spec.key in frames and not frames[spec.key].empty
        ]
        panels.append(
            {
                "key": panel.value,
                "title": PANEL_TITLES[panel],
                "blurb": PANEL_BLURBS[panel],
                "series": entries,
                # The capacity spec renders from its own block, not from `frames`, so it
            # is never "missing" just because it has no time-series entry.
            "missing": [
                s.key for s in specs
                if s.key not in frames and s.source is not Source.PJM_RPM
            ],
            }
        )

    return {
        "generated_at": now.replace(microsecond=0).isoformat(),
        "history_years": history_years,
        "ranges": [r for r in RANGES if r <= history_years],
        "default_range": min(default_range, history_years),
        # The client windows relative to this, not to its own clock — a viewer in
        # another timezone, or looking at a stale payload, must see the same window
        # the data actually supports.
        "latest_date": anchor.date().isoformat() if anchor is not None else None,
        "start_date": cutoff.date().isoformat() if cutoff is not None else None,
        "panels": panels,
        "capacity": _capacity_block(capacity),
        "status": status,
    }


def _anchor(
    frames: dict[str, pd.DataFrame], today: pd.Timestamp | None
) -> pd.Timestamp | None:
    """The newest observation anywhere in the payload — the client's window origin."""
    if today is not None:
        return pd.Timestamp(today)
    latest = [f.date.max() for f in frames.values() if not f.empty]
    return max(latest) if latest else None


def _cutoff(
    frames: dict[str, pd.DataFrame], history_years: int, today: pd.Timestamp | None
) -> pd.Timestamp | None:
    """Anchor the window on the newest observation, not the wall clock.

    Anchoring on today would silently shorten every chart when a source lags — BEA
    software investment is a quarter behind, so a 3-year window from today drops a
    whole quarter that we do have.
    """
    if today is not None:
        return pd.Timestamp(today) - pd.DateOffset(years=history_years)
    latest = [f.date.max() for f in frames.values() if not f.empty]
    if not latest:
        return None
    return max(latest) - pd.DateOffset(years=history_years)


def _series_entry(spec: SeriesSpec, frame: pd.DataFrame, cutoff: pd.Timestamp | None) -> dict:
    full = frame.dropna(subset=["value"]).sort_values("date")
    windowed = full if cutoff is None else full[full.date >= cutoff]
    # Keep at least one point even if the series stopped before the window opened,
    # so a stale series shows as a flat stub rather than vanishing without comment.
    if windowed.empty and not full.empty:
        windowed = full.tail(1)

    points = [[d.date().isoformat(), _round(v)] for d, v in zip(windowed.date, windowed.value)]
    # How far back this series reaches *within the payload*, so the page can say
    # "only 4 years of history" rather than silently drawing a short line inside a
    # 10-year frame. Measured on the shipped points, not on upstream history: the
    # client can only window what it was actually sent.
    span_years = (
        round((windowed.date.max() - windowed.date.min()).days / 365.25, 2)
        if len(windowed) > 1 else 0.0
    )
    return {
        "key": spec.key,
        "label": spec.label,
        "units": spec.units,
        "freq": spec.freq.value,
        "seasonal": spec.seasonal,
        "note": spec.note,
        "source": spec.source.value,
        "source_id": spec.source_id,
        "points": points,
        "earliest": points[0][0] if points else None,
        "span_years": span_years,
        "latest": points[-1] if points else None,
        "change": _changes(full, spec),
        "stale": bool(cutoff is not None and not full.empty and full.date.max() < cutoff),
    }


def _changes(full: pd.DataFrame, spec: SeriesSpec) -> dict:
    """Headline deltas. Year-over-year is the honest one for an NSA series — a
    month-over-month move on unadjusted data is mostly seasonality."""
    if full.empty:
        return {}
    latest_date = full.date.max()
    latest_val = float(full[full.date == latest_date].value.iloc[-1])

    out: dict[str, float | None] = {"latest": _round(latest_val)}
    for label, offset in (("yoy", pd.DateOffset(years=1)), ("three_year", pd.DateOffset(years=3))):
        out[label] = _pct_change_at(full, latest_date - offset, latest_val)
    return out


def _pct_change_at(full: pd.DataFrame, target: pd.Timestamp, latest_val: float) -> float | None:
    """Percent change vs the observation nearest `target`, within a tolerance sized
    to the series' own spacing. Returns None rather than comparing against a point
    that is not really a year ago."""
    prior = full[full.date <= target]
    if prior.empty:
        return None
    ref = prior.iloc[-1]
    # Reject a match more than ~45 days stale for monthly/daily data; quarterly data
    # legitimately lands up to a quarter away.
    tolerance = pd.Timedelta(days=120)
    if target - ref.date > tolerance:
        return None
    if ref.value == 0:
        return None
    return _round((latest_val / float(ref.value) - 1.0) * 100.0)


def _capacity_block(capacity: pd.DataFrame | None) -> dict | None:
    """PJM auction results are events, not a time series — their own shape."""
    if capacity is None or capacity.empty:
        return None
    spec = reg.by_key("pjm_bra_clearing_price")
    rows = [
        {
            "delivery_year": int(r.delivery_year),
            "label": r.delivery_year_label,
            "product": r.product_type,
            "price": _round(float(r.price)),
            "comparable_to_prior": bool(r.comparable_to_prior),
        }
        for r in capacity.itertuples()
    ]
    return {
        "key": spec.key,
        "label": spec.label,
        "units": spec.units,
        "note": spec.note,
        "auctions": rows,
    }


def _round(value: float) -> float:
    return round(float(value), 4)


# =========================================================================
# IO
# =========================================================================


def build(
    history_years: int = MAX_HISTORY_YEARS,
    cache_dir: Path | None = None,
    out_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> BuildResult:
    """Fetch everything available, cache it, and return the payload."""
    from .sources import bls, fred, pjm_rpm

    env = dict(os.environ if env is None else env)
    cache = cache_dir or CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)

    frames: dict[str, pd.DataFrame] = {}
    status: dict[str, str] = {}
    # One extra year beyond the window: the year-over-year view needs a full year of
    # data before the widest window opens to compute its first point.
    start_year = datetime.now(timezone.utc).year - max(history_years, 3) - 1

    # --- BLS -------------------------------------------------------------
    bls_specs = reg.by_source(Source.BLS)
    try:
        raw = bls.fetch([s.source_id for s in bls_specs], start_year, datetime.now().year)
        for spec in bls_specs:
            sub = raw[raw.series_id == spec.source_id][["date", "value"]]
            if sub.empty:
                status[spec.key] = "error: series returned no data"
            else:
                frames[spec.key] = sub.reset_index(drop=True)
                status[spec.key] = "ok"
    except Exception as exc:
        for spec in bls_specs:
            status[spec.key] = f"error: {exc}"

    # --- FRED ------------------------------------------------------------
    for spec in reg.by_source(Source.FRED):
        try:
            sub = fred.fetch([spec.source_id])[["date", "value"]]
            frames[spec.key] = sub.reset_index(drop=True)
            status[spec.key] = "ok"
        except Exception as exc:
            status[spec.key] = f"error: {exc}"

    # --- PJM capacity auctions -------------------------------------------
    capacity = None
    try:
        capacity = pjm_rpm.headline_rto_bra(pjm_rpm.fetch())
        status["pjm_bra_clearing_price"] = "ok"
    except Exception as exc:
        status["pjm_bra_clearing_price"] = f"error: {exc}"

    # --- keyed sources ---------------------------------------------------
    for source in (Source.EIA, Source.PJM, Source.ERCOT):
        specs = reg.by_source(source)
        absent = [k for k in REQUIRED_KEYS[source] if not env.get(k)]
        if absent:
            for spec in specs:
                status[spec.key] = f"skipped: {' and '.join(absent)} not set"
            continue
        frames_from, notes = _fetch_keyed(source, env, history_years)
        frames.update(frames_from)
        status.update(notes)

    _write_cache(frames, capacity, cache)

    payload = assemble(frames, capacity=capacity, status=status, history_years=history_years)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2))
    return BuildResult(payload=payload, status=status)


def _fetch_keyed(
    source: Source, env: dict[str, str], history_years: int
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Keyed sources. Imported lazily so a missing optional dep never breaks a build
    of the free panels."""
    frames: dict[str, pd.DataFrame] = {}
    status: dict[str, str] = {}
    specs = reg.by_source(source)
    try:
        if source is Source.EIA:
            from .sources import eia

            raw = eia.fetch_gas_curve(env["EIA_API_KEY"], history_years=history_years)
            for spec in specs:
                sub = raw[raw.series_id == spec.source_id][["date", "value"]]
                if sub.empty:
                    status[spec.key] = "error: no data"
                else:
                    frames[spec.key] = sub.reset_index(drop=True)
                    status[spec.key] = "ok"
        elif source is Source.PJM:
            from .sources import pjm

            for spec in specs:
                frames[spec.key] = pjm.fetch_hub_daily(
                    env["PJM_API_KEY"], history_years=history_years
                )
                status[spec.key] = "ok"
        elif source is Source.ERCOT:
            from .sources import ercot

            for spec in specs:
                frames[spec.key] = ercot.fetch_hub_daily(
                    env["ERCOT_API_KEY"],
                    history_years=history_years,
                    username=env.get("ERCOT_USERNAME"),
                    password=env.get("ERCOT_PASSWORD"),
                )
                status[spec.key] = "ok"
    except Exception as exc:
        for spec in specs:
            status.setdefault(spec.key, f"error: {exc}")
    return frames, status


def _write_cache(
    frames: dict[str, pd.DataFrame], capacity: pd.DataFrame | None, cache: Path
) -> None:
    for key, frame in frames.items():
        frame.to_parquet(cache / f"{key}.parquet")
    if capacity is not None and not capacity.empty:
        capacity.to_parquet(cache / "pjm_bra_clearing_price.parquet")
