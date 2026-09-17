"""PJM capacity auction (RPM) clearing prices.

PJM publishes every Reliability Pricing Model auction result in one small official
spreadsheet, refreshed whenever a new auction closes:

    .../rpm-auction-info/rpm-auctions-resource-clearing-price-summary.xlsx

That beats scraping the per-auction PDF reports — it is machine-readable, it is the
same file PJM's own market pages link to, and a new delivery year simply appears as
a new block. A refresh costs one 26 KB download.

Layout, and the quirks that a naive parse gets wrong:

    DY 07/08                       <- delivery-year header, sometimes "DY27/28" (no space)
    BRA   CP    269.92   269.92    <- auction row: type, product, then one price per zone
    1IA   *** AUCTION CANCELLED    <- cancelled auctions occupy the product column
    3IA   CP    323.9    **        <- "**" means the LDA was not modeled that year

  - The product column flips from "*" to "CP" when Capacity Performance replaced the
    older annual/extended-summer/limited products. Prices across that boundary are
    not like-for-like, so `product_type` is carried into the output rather than
    dropped, and the dashboard annotates the break.
  - Prices arrive as floats with binary-representation dust (95.78999999999999).
    Rounded to cents, which is the precision PJM actually publishes.
  - Incremental auctions (1IA/2IA/3IA) share the block with the BRA. They are real
    data but a different thing; callers filter on `auction`.
"""
from __future__ import annotations

import io
import re

import openpyxl
import pandas as pd
import requests

RCP_URL = (
    "https://www.pjm.com/-/media/DotCom/markets-ops/rpm/rpm-auction-info/"
    "rpm-auctions-resource-clearing-price-summary.xlsx"
)

# PJM's CDN rejects non-browser user agents on /-/media/ paths.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_session = requests.Session()
_session.headers.update({"User-Agent": _BROWSER_UA})

# "DY 07/08" and "DY27/28" both occur in the same file.
_DY_RE = re.compile(r"^DY\s*(\d{2})\s*/\s*(\d{2})$")

_AUCTIONS = {"BRA", "1IA", "2IA", "3IA"}

# Cells meaning "no price here", distinct from a price of zero.
_NULL_CELLS = {"", "*", "**", "***", "-", "n/a", "na"}


class PJMRPMError(RuntimeError):
    pass


def fetch(
    url: str = RCP_URL,
    session: requests.Session | None = None,
    content: bytes | None = None,
) -> pd.DataFrame:
    """Return every published RPM clearing price as a tidy long frame.

    Columns: [delivery_year, delivery_year_label, auction, product_type, zone, price].
    `delivery_year` is the starting calendar year (DY 27/28 -> 2027), because auction
    dates are irregular post-2022 and plotting against them misleads.
    """
    if content is None:
        sess = session or _session
        r = sess.get(url, timeout=90)
        if r.status_code != 200:
            raise PJMRPMError(f"PJM returned HTTP {r.status_code} for {url}")
        content = r.content

    return parse(content)


def parse(content: bytes) -> pd.DataFrame:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:  # openpyxl raises a zoo of types on bad input
        raise PJMRPMError(f"Could not open the RPM workbook: {exc}") from exc

    ws = wb[wb.sheetnames[0]]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if not rows:
        raise PJMRPMError("RPM workbook is empty")

    zones = _zone_columns(rows)
    if not zones:
        raise PJMRPMError("Could not locate the zone header row in the RPM workbook")

    records: list[dict] = []
    current_year: int | None = None
    current_label: str | None = None

    for row in rows:
        first = _text(row[0] if row else None)

        match = _DY_RE.match(first)
        if match:
            start, end = match.groups()
            current_year = 2000 + int(start)
            current_label = f"{current_year}/{2000 + int(end)}"
            continue

        if current_year is None or first not in _AUCTIONS:
            continue

        product = _text(row[1] if len(row) > 1 else None)
        if "AUCTION CANC" in product.upper():
            continue  # cancelled, not zero-priced

        for zone, col in zones.items():
            price = _price(row[col] if len(row) > col else None)
            if price is None:
                continue
            records.append(
                {
                    "delivery_year": current_year,
                    "delivery_year_label": current_label,
                    "auction": first,
                    "product_type": product or None,
                    "zone": zone,
                    "price": price,
                }
            )

    if not records:
        raise PJMRPMError("RPM workbook parsed but yielded no prices — layout changed?")

    df = pd.DataFrame.from_records(records)
    return df.sort_values(["delivery_year", "auction", "zone"]).reset_index(drop=True)


def rto_base_residual(df: pd.DataFrame) -> pd.DataFrame:
    """The headline series: RTO-wide Base Residual Auction price per delivery year."""
    out = df[(df.auction == "BRA") & (df.zone == "RTO")]
    return out[
        ["delivery_year", "delivery_year_label", "product_type", "price"]
    ].sort_values("delivery_year").reset_index(drop=True)


def _zone_columns(rows: list[list]) -> dict[str, int]:
    """Map zone name -> column index, from whichever row carries the header."""
    for row in rows[:10]:
        texts = [_text(c) for c in row]
        if "RTO" not in texts:
            continue
        start = texts.index("RTO")
        return {t: i for i, t in enumerate(texts) if i >= start and t}
    return {}


def _text(cell: object) -> str:
    return "" if cell is None else str(cell).strip()


def _price(cell: object) -> float | None:
    """A clearing price, or None for the file's several flavours of 'no value'."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return round(float(cell), 2)
    text = _text(cell).replace("$", "").replace(",", "")
    if text.lower() in _NULL_CELLS or set(text) == {"*"}:
        return None
    try:
        return round(float(text), 2)
    except ValueError:
        return None


# The product definition changed twice. For a single headline line per delivery year
# we take the primary product of whichever regime was in force, in this priority:
#
#   CP           Capacity Performance, DY 2018/19 onward (and the only product now)
#   Annual       the annual product of the 2014-2017 three-product regime
#   *            the single undifferentiated product before 2014
#
# BASE GEN and BASE DR/EE are transition sub-products quoted alongside CP in 2018 and
# 2019; they are not the headline price and are excluded here.
_PRODUCT_PRIORITY = ("CP", "Annual", "*")


def headline_rto_bra(df: pd.DataFrame) -> pd.DataFrame:
    """One RTO Base Residual clearing price per delivery year, for the main chart.

    Also returns `comparable_to_prior`: False on the two delivery years where the
    product definition changed, so the dashboard can break the line rather than imply
    a continuous price series across a definitional discontinuity.
    """
    bra = rto_base_residual(df)
    if bra.empty:
        return bra.assign(comparable_to_prior=pd.Series(dtype=bool))

    rank = {p: i for i, p in enumerate(_PRODUCT_PRIORITY)}
    bra = bra.copy()
    bra["_rank"] = bra.product_type.map(lambda p: rank.get(p, len(rank)))
    picked = (
        bra.sort_values(["delivery_year", "_rank"])
        .groupby("delivery_year", as_index=False)
        .first()
        .drop(columns="_rank")
    )

    prior = picked.product_type.shift()
    picked["comparable_to_prior"] = (picked.product_type == prior) | prior.isna()
    return picked.reset_index(drop=True)
