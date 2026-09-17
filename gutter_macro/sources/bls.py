"""BLS Public Data API — CPI, PPI and CES series.

Two API versions, and we use whichever the environment supports:
    v1  no key, 25 requests/day, 25 series/request, 10 years/request
    v2  free key (BLS_API_KEY), 500 requests/day, 50 series/request, 20 years/request

Both return the same payload shape, so the only differences are the URL, the batch
size and the year span. One dashboard refresh costs a single request per year-chunk,
so even the keyless tier is comfortable for a daily rebuild.

Traps this module handles, all observed live against the API:
  - A value can be the literal string "-" (no datum for that period) even when the
    observation object exists. CUUR0000SEGD01 does this. These become NaN, not 0.0.
  - Period M13 is the annual average, not a 13th month. Dropped.
  - Semiannual series use S01/S02/S03. Dropped for the same reason.
  - "No Data Available for Series X Year: Y" arrives in `message` with an overall
    status of REQUEST_SUCCEEDED, so a missing series is silent unless we check.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pandas as pd
import requests

V1_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
V2_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# Month codes only. M13 is the annual average; S0x are semiannual aggregates.
_MONTH_PERIODS = {f"M{m:02d}" for m in range(1, 13)}

_session = requests.Session()
_session.headers.update({"User-Agent": "gutter-macro/0.1.0 (james@gutter.cc)"})


class BLSError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Tier:
    url: str
    batch: int
    span: int          # max years per request
    key: str | None


def _tier(api_key: str | None = None) -> _Tier:
    key = api_key if api_key is not None else os.environ.get("BLS_API_KEY") or None
    if key:
        return _Tier(V2_URL, batch=50, span=20, key=key)
    return _Tier(V1_URL, batch=25, span=10, key=None)


def fetch(
    series_ids: list[str],
    start_year: int,
    end_year: int,
    api_key: str | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Fetch BLS series as a tidy long frame.

    Returns columns [date, series_id, value]; `date` is the first of the month, so a
    monthly index lines up with FRED's convention without further massaging. Series
    the API reports as unavailable are simply absent from the result — callers check
    for missing keys rather than getting a frame of silent NaNs.
    """
    if not series_ids:
        return _empty()
    if start_year > end_year:
        raise ValueError(f"start_year {start_year} is after end_year {end_year}")

    tier = _tier(api_key)
    sess = session or _session
    frames: list[pd.DataFrame] = []

    for id_batch in _chunks(series_ids, tier.batch):
        for lo, hi in _year_windows(start_year, end_year, tier.span):
            frames.append(_request(sess, tier, id_batch, lo, hi))

    if not frames:
        return _empty()
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["date", "series_id"], keep="last")
    return out.sort_values(["series_id", "date"]).reset_index(drop=True)


def _request(
    sess: requests.Session, tier: _Tier, ids: list[str], lo: int, hi: int
) -> pd.DataFrame:
    payload: dict[str, object] = {
        "seriesid": ids,
        "startyear": str(lo),
        "endyear": str(hi),
    }
    if tier.key:
        payload["registrationkey"] = tier.key

    r = sess.post(
        tier.url,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    if r.status_code != 200:
        raise BLSError(f"BLS returned HTTP {r.status_code} for {ids}: {r.text[:200]}")

    body = r.json()
    status = body.get("status")
    if status != "REQUEST_SUCCEEDED":
        raise BLSError(f"BLS status {status!r}: {body.get('message')}")

    rows: list[tuple[pd.Timestamp, str, float]] = []
    for series in body.get("Results", {}).get("series", []):
        sid = series["seriesID"]
        for obs in series.get("data", []):
            period = obs.get("period", "")
            if period not in _MONTH_PERIODS:
                continue          # annual average (M13) or semiannual (S0x)
            value = _parse_value(obs.get("value"))
            month = int(period[1:])
            rows.append((pd.Timestamp(year=int(obs["year"]), month=month, day=1), sid, value))

    if not rows:
        return _empty()
    return pd.DataFrame(rows, columns=["date", "series_id", "value"])


def _parse_value(raw: object) -> float:
    """BLS uses '-' for a period with no datum. Anything unparseable becomes NaN."""
    if raw is None:
        return float("nan")
    text = str(raw).strip().replace(",", "")
    if not text or text == "-":
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _year_windows(start: int, end: int, span: int) -> list[tuple[int, int]]:
    """Inclusive year windows no wider than `span` years."""
    windows = []
    lo = start
    while lo <= end:
        hi = min(lo + span - 1, end)
        windows.append((lo, hi))
        lo = hi + 1
    return windows


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "series_id": pd.Series(dtype="object"),
            "value": pd.Series(dtype="float64"),
        }
    )
