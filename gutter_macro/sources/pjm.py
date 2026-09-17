"""PJM Data Miner 2 — hourly LMP at a hub, aggregated to daily.

Request shape (verified against PJM's public API contract as implemented by the
gridstatus library, which is the best public reference for it):

    GET https://api.pjm.com/api/v1/<feed>
    header  Ocp-Apim-Subscription-Key: <key>
    params  startRow, rowCount (<=50000), fields, sort,
            datetime_beginning_ept = "MM/DD/YYYY HH:MM" + "to" + "MM/DD/YYYY HH:MM"
    body    {"items": [...], "totalRows": N, "links": [{"rel": "next", "href": ...}]}
            or {"errors": [...]} on failure

The hub's pnode_id is *resolved by name at runtime* rather than hardcoded. PJM's hub
node ids are not self-describing (51217 and 51288 are both hubs) and silently pulling
the wrong one yields a plausible price series for the wrong location — the worst kind
of failure. One extra request per refresh buys certainty.

NOTE: this module is written against the documented contract but has not yet been
exercised against the live API — it needs a PJM_API_KEY. `probe()` prints what the
API actually returns so the first real run can confirm or correct the field names.
"""
from __future__ import annotations

import pandas as pd
import requests

BASE = "https://api.pjm.com/api/v1"
DA_FEED = "da_hrl_lmps"
PNODE_FEED = "pnode"
MAX_ROWS = 50_000

# PJM on-peak: hours ending 8 through 23, Monday-Friday. PJM also excludes NERC
# holidays; we do not, because the holiday calendar would need its own maintenance
# and the effect on a daily mean is small. Documented rather than silently applied.
ONPEAK_HOURS = range(7, 23)  # hour-beginning 7..22 == hour-ending 8..23


class PJMError(RuntimeError):
    pass


def _session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Ocp-Apim-Subscription-Key": api_key,
            "User-Agent": "gutter-macro/0.1.0 (james@gutter.cc)",
        }
    )
    return s


def _get(sess: requests.Session, url: str, params: dict | None = None) -> dict:
    r = sess.get(url, params=params, timeout=120)
    if r.status_code == 401:
        raise PJMError("PJM rejected the subscription key (401). Check PJM_API_KEY.")
    if r.status_code == 429:
        raise PJMError("PJM rate limit hit (429). Back off and retry.")
    if r.status_code != 200:
        raise PJMError(f"PJM returned HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    if isinstance(body, dict) and body.get("errors"):
        raise PJMError(f"PJM API error: {body['errors']}")
    return body


def _paged_items(sess: requests.Session, url: str, params: dict) -> list[dict]:
    """Follow PJM's `links[rel=next]` pagination to exhaustion."""
    body = _get(sess, url, params)
    items = list(body.get("items", []))
    seen_pages = 1
    while True:
        nxt = next(
            (l["href"] for l in body.get("links", []) if l.get("rel") == "next"), None
        )
        if not nxt or seen_pages > 200:  # guard against a self-referential next link
            break
        body = _get(sess, nxt)
        new = body.get("items", [])
        if not new:
            break
        items.extend(new)
        seen_pages += 1
    return items


def resolve_pnode_id(api_key: str, pnode_name: str = "WESTERN HUB") -> int:
    """Look up a pricing node's id by its exact name."""
    sess = _session(api_key)
    items = _paged_items(
        sess,
        f"{BASE}/{PNODE_FEED}",
        {
            "rowCount": 50000,
            "startRow": 1,
            "fields": "pnode_id,pnode_name,pnode_type,effective_date,termination_date",
        },
    )
    target = pnode_name.strip().upper()
    matches = {
        int(i["pnode_id"])
        for i in items
        if str(i.get("pnode_name", "")).strip().upper() == target
    }
    if not matches:
        near = sorted(
            {
                str(i.get("pnode_name"))
                for i in items
                if target.split()[0] in str(i.get("pnode_name", "")).upper()
            }
        )[:10]
        raise PJMError(
            f"No PJM pnode named {pnode_name!r}. Similar names: {near or 'none'}"
        )
    # A pnode keeps its id across effective-date rows; several rows, one id.
    if len(matches) > 1:
        raise PJMError(f"{pnode_name!r} resolved to multiple ids: {sorted(matches)}")
    return matches.pop()


def fetch_hub_daily(
    api_key: str,
    history_years: int = 3,
    pnode_name: str = "WESTERN HUB",
    today: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Daily mean day-ahead LMP at a hub, with on-peak and off-peak splits.

    Returns [date, value, on_peak, off_peak] where `value` is the 24-hour mean.
    """
    pnode_id = resolve_pnode_id(api_key, pnode_name)
    anchor = pd.Timestamp(today) if today is not None else pd.Timestamp.utcnow()
    # Exclusive upper bound at tomorrow midnight: day-ahead prices for *today* were
    # published yesterday, so stopping at today's midnight drops a real day.
    end = anchor.normalize() + pd.Timedelta(days=1)
    start = end - pd.DateOffset(years=history_years)

    sess = _session(api_key)
    rows: list[dict] = []
    # One request per calendar year: a single node is 8,760 hourly rows a year, well
    # inside the 50,000 row cap, and a per-year window keeps any one failure small.
    for lo, hi in _year_windows(start, end):
        rows.extend(
            _paged_items(
                sess,
                f"{BASE}/{DA_FEED}",
                {
                    "startRow": 1,
                    "rowCount": MAX_ROWS,
                    "pnode_id": str(pnode_id),
                    "fields": "datetime_beginning_ept,pnode_id,pnode_name,total_lmp_da",
                    "sort": "datetime_beginning_ept",
                    "datetime_beginning_ept": (
                        f"{lo.strftime('%m/%d/%Y %H:%M')}to{hi.strftime('%m/%d/%Y %H:%M')}"
                    ),
                },
            )
        )

    return to_daily(rows)


def to_daily(rows: list[dict]) -> pd.DataFrame:
    """Aggregate hourly LMP records to a daily frame. Pure — the tested half."""
    if not rows:
        return _empty()

    df = pd.DataFrame(rows)
    missing = {"datetime_beginning_ept", "total_lmp_da"} - set(df.columns)
    if missing:
        raise PJMError(f"PJM response is missing expected fields: {sorted(missing)}")

    df["ts"] = pd.to_datetime(df["datetime_beginning_ept"], format="ISO8601", errors="coerce")
    df["lmp"] = pd.to_numeric(df["total_lmp_da"], errors="coerce")
    df = df.dropna(subset=["ts", "lmp"])
    if df.empty:
        return _empty()

    # Duplicate hours appear when PJM reposts a day; the later record wins.
    df = df.drop_duplicates(subset=["ts"], keep="last")

    df["date"] = df.ts.dt.normalize()
    df["hour"] = df.ts.dt.hour
    df["is_peak"] = df.ts.dt.weekday.lt(5) & df.hour.isin(list(ONPEAK_HOURS))

    daily = df.groupby("date").agg(value=("lmp", "mean")).reset_index()
    peak = (
        df[df.is_peak].groupby("date").agg(on_peak=("lmp", "mean")).reset_index()
    )
    off = (
        df[~df.is_peak].groupby("date").agg(off_peak=("lmp", "mean")).reset_index()
    )
    out = daily.merge(peak, on="date", how="left").merge(off, on="date", how="left")
    return out.sort_values("date").reset_index(drop=True)


def probe(api_key: str, pnode_name: str = "WESTERN HUB") -> dict:
    """One small request, printed raw. Run this the first time a key exists to
    confirm the field names above before trusting a full pull."""
    sess = _session(api_key)
    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=2)
    body = _get(
        sess,
        f"{BASE}/{DA_FEED}",
        {
            "startRow": 1,
            "rowCount": 5,
            "datetime_beginning_ept": (
                f"{start.strftime('%m/%d/%Y %H:%M')}to{end.strftime('%m/%d/%Y %H:%M')}"
            ),
        },
    )
    return {
        "totalRows": body.get("totalRows"),
        "keys": sorted((body.get("items") or [{}])[0].keys()),
        "first_item": (body.get("items") or [None])[0],
    }


def _year_windows(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple]:
    windows = []
    lo = start
    while lo < end:
        hi = min(lo + pd.DateOffset(years=1) - pd.Timedelta(minutes=1), end)
        windows.append((lo, hi))
        lo = hi + pd.Timedelta(minutes=1)
    return windows


def _empty() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.Series(dtype="datetime64[ns]"),
            "value": pd.Series(dtype="float64"),
            "on_peak": pd.Series(dtype="float64"),
            "off_peak": pd.Series(dtype="float64"),
        }
    )
