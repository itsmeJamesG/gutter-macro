"""FRED via the public fredgraph CSV endpoint. No API key required.

Deliberately returns the same tidy long shape as sources.bls.fetch — [date, series_id,
value] with tz-naive dates — so the build step can concatenate the two without
resampling or timezone reconciliation.

Note for anyone extending this: FRED does *not* carry the granular CPI items this
dashboard needs. CUUR0000SEEE03, CUUR0000SEEE02, CUUR0000SEGD01 and CUUR0000SS68023
all return HTTP 404 here and must go through the BLS API instead. Only the headline
seasonally adjusted aggregates (SEHF01, SAH1, SAM2) and the BEA NIPA series are
available from FRED.
"""
from __future__ import annotations

import io

import pandas as pd
import requests

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"

_session = requests.Session()
_session.headers.update({"User-Agent": "gutter-macro/0.1.0 (james@gutter.cc)"})


class FREDError(RuntimeError):
    pass


def fetch(
    series_ids: list[str],
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Fetch FRED series as [date, series_id, value]. Full history; these are small."""
    sess = session or _session
    frames = [_fetch_one(sess, sid) for sid in series_ids]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return _empty()
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["series_id", "date"]).reset_index(drop=True)


def _fetch_one(sess: requests.Session, series_id: str) -> pd.DataFrame:
    r = sess.get(FRED_CSV, params={"id": series_id}, timeout=45)
    if r.status_code == 404:
        raise FREDError(
            f"FRED has no series {series_id!r}. Granular BLS CPI items are not "
            f"mirrored on FRED — route those through sources.bls instead."
        )
    if r.status_code != 200:
        raise FREDError(f"FRED returned HTTP {r.status_code} for {series_id}")

    df = pd.read_csv(io.StringIO(r.text))
    # FRED has used both "DATE" (older) and "observation_date" (current).
    date_col = next(
        (c for c in df.columns if c.lower() in ("date", "observation_date")), None
    )
    if date_col is None or series_id not in df.columns:
        raise FREDError(
            f"Unexpected FRED columns for {series_id}: {df.columns.tolist()}"
        )

    out = pd.DataFrame(
        {
            "date": pd.to_datetime(df[date_col]),
            "series_id": series_id,
            # FRED writes "." for a missing observation.
            "value": pd.to_numeric(df[series_id], errors="coerce"),
        }
    )
    return out.dropna(subset=["value"]).reset_index(drop=True)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "series_id": pd.Series(dtype="object"),
            "value": pd.Series(dtype="float64"),
        }
    )
