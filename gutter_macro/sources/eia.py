"""EIA Open Data API v2 — NYMEX Henry Hub futures contracts 1 through 4.

Four contracts is what EIA publishes free, and it is enough to show the shape of the
curve (contango vs backwardation) even though a full 24-month strip would need a paid
CME or ICE feed.

Request shape per EIA's documentation:

    GET https://api.eia.gov/v2/natural-gas/pri/fut/data/
        ?api_key=<key>
        &frequency=daily
        &data[0]=value
        &facets[series][]=RNGC1&facets[series][]=RNGC2...
        &start=YYYY-MM-DD
        &sort[0][column]=period&sort[0][direction]=asc
        &length=5000&offset=0

    {"response": {"total": N, "data": [{"period", "series", "value", "units"}, ...]}}

JSON responses cap at 5,000 rows, so pagination is mandatory: four contracts of daily
data over three years is roughly 3,000 rows, but the margin is thin enough that a
longer window would silently truncate without the offset loop below.

NOTE: written against the documented contract, not yet run against the live API —
it needs an EIA_API_KEY. `probe_route()` prints the route's declared facets and data
columns, which is the fastest way to confirm the facet name is `series` before
trusting a full pull.
"""
from __future__ import annotations

import pandas as pd
import requests

BASE = "https://api.eia.gov/v2"
ROUTE = "natural-gas/pri/fut"
PAGE = 5000
CONTRACTS = ("RNGC1", "RNGC2", "RNGC3", "RNGC4")

_session = requests.Session()
_session.headers.update({"User-Agent": "gutter-macro/0.1.0 (james@gutter.cc)"})


class EIAError(RuntimeError):
    pass


def _get(url: str, params: list[tuple[str, str]], session: requests.Session | None = None) -> dict:
    sess = session or _session
    r = sess.get(url, params=params, timeout=90)
    if r.status_code == 403:
        raise EIAError("EIA rejected the API key (403). Check EIA_API_KEY.")
    if r.status_code != 200:
        raise EIAError(f"EIA returned HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    if "error" in body:
        raise EIAError(f"EIA API error: {body['error']}")
    return body


def fetch_gas_curve(
    api_key: str,
    history_years: int = 3,
    contracts: tuple[str, ...] = CONTRACTS,
    today: pd.Timestamp | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Daily settlements for the first N Henry Hub contracts.

    Returns [date, series_id, value] — the same tidy shape as sources.bls and
    sources.fred, so build.py treats all three identically.
    """
    end = pd.Timestamp(today) if today is not None else pd.Timestamp.utcnow().normalize()
    start = end - pd.DateOffset(years=history_years)

    rows: list[dict] = []
    offset = 0
    while True:
        params: list[tuple[str, str]] = [
            ("api_key", api_key),
            ("frequency", "daily"),
            ("data[0]", "value"),
            ("start", start.date().isoformat()),
            ("end", end.date().isoformat()),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("length", str(PAGE)),
            ("offset", str(offset)),
        ]
        params += [("facets[series][]", c) for c in contracts]

        body = _get(f"{BASE}/{ROUTE}/data/", params, session)
        response = body.get("response", {})
        page = response.get("data", [])
        rows.extend(page)

        total = int(response.get("total", len(rows)) or 0)
        offset += PAGE
        if len(page) < PAGE or offset >= total:
            break

    return to_frame(rows)


def to_frame(rows: list[dict]) -> pd.DataFrame:
    """Normalise EIA rows to [date, series_id, value]. Pure — the tested half."""
    if not rows:
        return _empty()

    df = pd.DataFrame(rows)
    if "period" not in df.columns:
        raise EIAError(f"EIA response has no `period` column: {sorted(df.columns)}")
    # EIA labels the series column `series` on this route; accept the documented
    # alternatives rather than failing on a rename.
    series_col = next(
        (c for c in ("series", "seriesId", "series-id", "product") if c in df.columns), None
    )
    if series_col is None:
        raise EIAError(
            f"EIA response has no recognisable series column: {sorted(df.columns)}. "
            f"Run probe_route() to see the route's declared facets."
        )

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df["period"], errors="coerce"),
            "series_id": df[series_col].astype(str),
            "value": pd.to_numeric(df["value"], errors="coerce"),
        }
    )
    out = out.dropna(subset=["date", "value"])
    out = out.drop_duplicates(subset=["date", "series_id"], keep="last")
    return out.sort_values(["series_id", "date"]).reset_index(drop=True)


def latest_curve(frame: pd.DataFrame) -> pd.DataFrame:
    """The most recent complete snapshot across contracts — the curve's shape today.

    Uses the latest date on which *every* contract settled, so the curve is never
    drawn from a mix of dates (which would show a shape that never existed).
    """
    if frame.empty:
        return frame
    counts = frame.groupby("date").series_id.nunique()
    wanted = frame.series_id.nunique()
    complete = counts[counts == wanted]
    if complete.empty:
        return frame.iloc[0:0]
    day = complete.index.max()
    return frame[frame.date == day].sort_values("series_id").reset_index(drop=True)


def probe_route(api_key: str, session: requests.Session | None = None) -> dict:
    """The route's own metadata: declared facets, data columns, frequencies.

    Run once when a key first exists — it answers whether the facet is `series`
    without guessing.
    """
    body = _get(f"{BASE}/{ROUTE}/", [("api_key", api_key)], session)
    response = body.get("response", {})
    return {
        "id": response.get("id"),
        "name": response.get("name"),
        "facets": [f.get("id") for f in response.get("facets", [])],
        "data_columns": sorted((response.get("data") or {}).keys()),
        "frequencies": [f.get("id") for f in response.get("frequency", [])],
    }


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "series_id": pd.Series(dtype="object"),
            "value": pd.Series(dtype="float64"),
        }
    )
