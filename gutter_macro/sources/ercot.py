"""ERCOT Public API — day-ahead settlement point prices at a hub.

ERCOT is the most awkward of the three feeds, and all of it is upstream design:

  - Three credentials, not one. A free Azure APIM subscription key
    (ERCOT_API_KEY) *plus* a username and password (ERCOT_USERNAME /
    ERCOT_PASSWORD) used against an Azure B2C ROPC flow to mint an ID token.
  - The ID token lives one hour and cannot be refreshed; a long backfill has to
    re-authenticate mid-run, which `_TokenCache` handles.
  - 30 requests/minute, and requests from outside the United States are blocked
    outright — a deploy in a non-US Fly region will fail with no useful message,
    which is why fly.toml pins primary_region = "iad".
  - Rows come back as positional arrays with a separate `fields` list rather than
    objects, so column order must be read from the response, never assumed.

Endpoint: https://api.ercot.com/api/public-reports/np4-190-cd/dam_stlmnt_pnt_prices

NOTE: written against ERCOT's documented contract; not yet exercised live — it needs
the three credentials. `probe()` returns one small page raw for confirmation.
"""
from __future__ import annotations

import time

import pandas as pd
import requests

TOKEN_URL = (
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
    "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
)
# ERCOT's published public client id for the ROPC flow.
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
SCOPE = f"openid {CLIENT_ID} offline_access"

BASE = "https://api.ercot.com/api/public-reports"
DAM_SPP = "np4-190-cd/dam_stlmnt_pnt_prices"
PAGE_SIZE = 1000

# ERCOT on-peak convention: hours ending 7-22, Monday-Saturday (not Sunday), which
# differs from PJM's. Encoded here rather than shared, precisely because they differ.
ONPEAK_HOURS = range(6, 22)   # hour-beginning 6..21 == hour-ending 7..22


class ERCOTError(RuntimeError):
    pass


class _TokenCache:
    """An ID token plus its expiry. ERCOT tokens last an hour and cannot refresh,
    so a multi-year backfill re-mints rather than failing halfway through."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self, session: requests.Session) -> str:
        # Re-mint a minute early so a request never races the expiry.
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        r = session.post(
            TOKEN_URL,
            data={
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": SCOPE,
                "client_id": CLIENT_ID,
                "response_type": "id_token",
            },
            timeout=60,
        )
        if r.status_code != 200:
            raise ERCOTError(
                f"ERCOT token request failed (HTTP {r.status_code}). Check "
                f"ERCOT_USERNAME / ERCOT_PASSWORD. {r.text[:200]}"
            )
        body = r.json()
        token = body.get("id_token")
        if not token:
            raise ERCOTError(f"ERCOT token response has no id_token: {sorted(body)}")
        self._token = token
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        return token


def fetch_hub_daily(
    api_key: str,
    history_years: int = 3,
    settlement_point: str = "HB_NORTH",
    username: str | None = None,
    password: str | None = None,
    today: pd.Timestamp | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Daily mean day-ahead settlement point price at a hub, with peak/off-peak.

    Returns [date, value, on_peak, off_peak, median]. `median` is carried because
    ERCOT is energy-only with a $5,000/MWh cap: a daily *mean* is dominated by a
    handful of scarcity hours, and quoting only the mean overstates the typical day.
    """
    if not username or not password:
        raise ERCOTError(
            "ERCOT needs ERCOT_USERNAME and ERCOT_PASSWORD in addition to "
            "ERCOT_API_KEY — its API mints a one-hour ID token via a B2C password flow."
        )

    sess = session or requests.Session()
    sess.headers.update({"User-Agent": "gutter-macro/0.1.0 (james@gutter.cc)"})
    tokens = _TokenCache(username, password)

    end = pd.Timestamp(today) if today is not None else pd.Timestamp.utcnow().normalize()
    start = end - pd.DateOffset(years=history_years)

    rows: list[dict] = []
    for lo, hi in _month_windows(start, end):
        page = 1
        while True:
            body = _get(
                sess,
                tokens,
                api_key,
                f"{BASE}/{DAM_SPP}",
                {
                    "deliveryDateFrom": lo.date().isoformat(),
                    "deliveryDateTo": hi.date().isoformat(),
                    "settlementPoint": settlement_point,
                    "page": page,
                    "size": PAGE_SIZE,
                },
            )
            rows.extend(_rows_from(body))
            meta = body.get("_meta") or {}
            total_pages = int(meta.get("totalPages") or 1)
            if page >= total_pages:
                break
            page += 1
            time.sleep(2.1)   # 30 requests/minute ceiling

    return to_daily(rows)


def _get(
    sess: requests.Session,
    tokens: _TokenCache,
    api_key: str,
    url: str,
    params: dict,
) -> dict:
    r = sess.get(
        url,
        params=params,
        headers={
            "Ocp-Apim-Subscription-Key": api_key,
            "Authorization": f"Bearer {tokens.token(sess)}",
        },
        timeout=120,
    )
    if r.status_code == 401:
        raise ERCOTError("ERCOT rejected the credentials (401).")
    if r.status_code == 403:
        raise ERCOTError(
            "ERCOT returned 403. Either the subscription key is wrong, or the "
            "request came from outside the United States, which ERCOT blocks."
        )
    if r.status_code == 429:
        raise ERCOTError("ERCOT rate limit hit (429). The cap is 30 requests/minute.")
    if r.status_code != 200:
        raise ERCOTError(f"ERCOT returned HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _rows_from(body: dict) -> list[dict]:
    """ERCOT returns positional arrays plus a `fields` list. Zip them by the order
    the response declares — never by an assumed column order."""
    fields = [f.get("name") for f in (body.get("fields") or [])]
    data = body.get("data") or []
    if not fields:
        # Some routes return objects directly; accept that shape too.
        return [r for r in data if isinstance(r, dict)]
    return [dict(zip(fields, row)) for row in data if isinstance(row, (list, tuple))]


def to_daily(rows: list[dict]) -> pd.DataFrame:
    """Aggregate hourly settlement prices to daily. Pure — the tested half."""
    if not rows:
        return _empty()
    df = pd.DataFrame(rows)

    date_col = _pick(df, ("deliveryDate", "DeliveryDate", "delivery_date"))
    hour_col = _pick(df, ("hourEnding", "HourEnding", "hour_ending"))
    price_col = _pick(df, ("settlementPointPrice", "SettlementPointPrice", "settlement_point_price"))
    if not (date_col and price_col):
        raise ERCOTError(f"ERCOT response missing date/price columns: {sorted(df.columns)}")

    df["date"] = pd.to_datetime(df[date_col], errors="coerce")
    df["price"] = pd.to_numeric(df[price_col], errors="coerce")
    df["hour"] = _hour_ending(df[hour_col]) if hour_col else 0
    df = df.dropna(subset=["date", "price"])
    if df.empty:
        return _empty()

    df["is_peak"] = df.date.dt.weekday.lt(6) & (df.hour - 1).isin(list(ONPEAK_HOURS))

    daily = df.groupby("date").agg(
        value=("price", "mean"), median=("price", "median")
    ).reset_index()
    peak = df[df.is_peak].groupby("date").agg(on_peak=("price", "mean")).reset_index()
    off = df[~df.is_peak].groupby("date").agg(off_peak=("price", "mean")).reset_index()
    out = daily.merge(peak, on="date", how="left").merge(off, on="date", how="left")
    return out[["date", "value", "on_peak", "off_peak", "median"]].sort_values(
        "date"
    ).reset_index(drop=True)


def _hour_ending(col: pd.Series) -> pd.Series:
    """ERCOT hour-ending is sometimes "01:00" / "0100", sometimes an integer, and on
    the fall-back DST day gains a "02:00 DST" variant."""
    text = col.astype(str).str.strip().str.replace("DST", "", regex=False).str.strip()
    numeric = pd.to_numeric(text.str.split(":").str[0], errors="coerce")
    return numeric.fillna(0).astype(int)


def _pick(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lowered = {c.lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


def probe(api_key: str, username: str, password: str, settlement_point: str = "HB_NORTH") -> dict:
    """One small page, raw, for confirming field names on first use."""
    sess = requests.Session()
    tokens = _TokenCache(username, password)
    end = pd.Timestamp.utcnow().normalize()
    body = _get(
        sess, tokens, api_key, f"{BASE}/{DAM_SPP}",
        {
            "deliveryDateFrom": (end - pd.Timedelta(days=3)).date().isoformat(),
            "deliveryDateTo": end.date().isoformat(),
            "settlementPoint": settlement_point,
            "page": 1, "size": 5,
        },
    )
    return {
        "fields": [f.get("name") for f in (body.get("fields") or [])],
        "meta": body.get("_meta"),
        "first_row": (body.get("data") or [None])[0],
    }


def _empty() -> pd.DataFrame:
    cols = ["value", "on_peak", "off_peak", "median"]
    return pd.DataFrame(
        {"date": pd.Series(dtype="datetime64[ns]"),
         **{c: pd.Series(dtype="float64") for c in cols}}
    )
