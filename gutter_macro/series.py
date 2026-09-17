"""The declarative registry of every series on the Gutter Macro Dashboard.

Adding a panel line is one SeriesSpec entry. Nothing else in the codebase hardcodes
a series ID: the fetchers dispatch on `source`, and the web app renders whatever the
build step emits, grouped by `panel`.

Every ID below was verified live against its provider on 2026-09-17 — the `verified`
field records the value we saw, so a silent upstream rename shows up as a test
failure rather than an empty chart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Source(str, Enum):
    BLS = "bls"          # api.bls.gov  — CPI, PPI, CES. v1 needs no key.
    FRED = "fred"        # fredgraph CSV — no key needed on this endpoint.
    EIA = "eia"          # api.eia.gov  — needs a free key. Gas curve, wholesale power.
    PJM = "pjm"          # Data Miner 2 — needs a free pjm.com subscription key.
    ERCOT = "ercot"      # ERCOT public API — needs a free subscription key.
    PJM_RPM = "pjm_rpm"  # Capacity auction (BRA) results. Scraped, ~annual.


class Freq(str, Enum):
    DAILY = "daily"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"     # really "per auction" for RPM


class Panel(str, Enum):
    POWER = "power"           # wholesale electricity — the AI demand shock
    CAPACITY = "capacity"     # PJM capacity auctions — the scarcity signal
    GAS = "gas"               # Henry Hub level + curve shape — the marginal fuel
    CPI_ENERGY = "cpi_energy" # what the shock costs households
    CPI_LABOR = "cpi_labor"   # labor AI is bidding up vs. labor it may displace
    CPI_SERVICES = "cpi_services"
    SOFTWARE = "software"     # BEA — the investment boom and its price collapse


@dataclass(frozen=True)
class SeriesSpec:
    key: str                  # our slug, stable across the stack
    label: str                # chart legend text
    source: Source
    source_id: str
    panel: Panel
    freq: Freq
    units: str
    seasonal: bool = False    # True = seasonally adjusted at the source
    verified: str = ""        # value observed at build time, for the drift test
    note: str = ""            # caveats — surfaced in the dashboard footnotes


REGISTRY: list[SeriesSpec] = [
    # ---------------------------------------------------------------- CPI: energy
    SeriesSpec(
        key="cpi_electricity",
        label="Electricity",
        source=Source.BLS, source_id="CUSR0000SEHF01",
        panel=Panel.CPI_ENERGY, freq=Freq.MONTHLY, units="index 1982-84=100",
        seasonal=True, verified="2026-08=305.444",
        note="Household retail electricity. Lags wholesale by quarters — utility rate "
             "cases, not spot markets, set it.",
    ),
    SeriesSpec(
        key="cpi_shelter",
        label="Shelter",
        source=Source.BLS, source_id="CUSR0000SAH1",
        panel=Panel.CPI_ENERGY, freq=Freq.MONTHLY, units="index 1982-84=100",
        seasonal=True, verified="2026-08=430.227",
        note="Included as the comparison baseline: the big non-tradable services "
             "component AI does not touch.",
    ),

    # ---------------------------------------------------------------- CPI: labor
    SeriesSpec(
        key="wage_trades",
        label="Specialty trade contractors — avg hourly earnings",
        source=Source.BLS, source_id="CES2023800003",
        panel=Panel.CPI_LABOR, freq=Freq.MONTHLY, units="$/hour",
        seasonal=True, verified="2026-07=40.27",
        note="Electricians, plumbers, HVAC. The labor the datacenter buildout competes "
             "for, and the clearest wage read on the physical side of AI.",
    ),
    SeriesSpec(
        key="ppi_legal",
        label="Offices of lawyers (PPI)",
        source=Source.BLS, source_id="PCU541110541110",
        panel=Panel.CPI_LABOR, freq=Freq.MONTHLY, units="index",
        verified="2026-08=339.513",
        note="Substitute for CPI legal services (CUUR0000SEGD01), which is sparse, "
             "semiannual and has no 2026 data — unusable for a chart.",
    ),
    SeriesSpec(
        key="cpi_tax_prep",
        label="Tax prep & accounting fees",
        source=Source.BLS, source_id="CUUR0000SS68023",
        panel=Panel.CPI_LABOR, freq=Freq.MONTHLY, units="index Dec 1986=100",
        verified="2026-08=374.475",
        note="NSA only. White-collar work with a plausible near-term automation path.",
    ),

    # ------------------------------------------------------------ CPI: services
    SeriesSpec(
        key="cpi_internet",
        label="Internet services & electronic info providers",
        source=Source.BLS, source_id="CUUR0000SEEE03",
        panel=Panel.CPI_SERVICES, freq=Freq.MONTHLY, units="index Dec 1988=100",
        verified="2026-08=87.518",
        note="NSA only. The level sits far below its Dec 1988 base after decades of\n             decline, but the series has risen steadily since late 2024 — read the\n             level as long-run deflation and the slope as the current trend, and do\n             not quote one as the other.",
    ),
    SeriesSpec(
        key="cpi_software",
        label="Computer software & accessories",
        source=Source.BLS, source_id="CUUR0000SEEE02",
        panel=Panel.CPI_SERVICES, freq=Freq.MONTHLY, units="index Dec 1988=100",
        verified="2026-08=28.131",
        note="NSA only and quality-adjusted, so the long-run decline is largely\n             hedonic — performance per dollar, not a shelf price. The index has\n             risen sharply since late 2025 (22.4 -> 28.1 over the year to Aug 2026),\n             moving opposite to BEA's software investment deflator. That divergence\n             is a finding, not an error: this measures what households pay for\n             packaged software, the BEA index what businesses pay to build it.",
    ),
    SeriesSpec(
        key="cpi_medical_services",
        label="Medical care services",
        source=Source.BLS, source_id="CUSR0000SAM2",
        panel=Panel.CPI_SERVICES, freq=Freq.MONTHLY, units="index 1982-84=100",
        seasonal=True, verified="2026-08=653.532",
        note="Labor-heavy services with heavy AI pilot exposure but regulated pricing.",
    ),

    # ---------------------------------------------------------------- BEA software
    SeriesSpec(
        key="bea_software_nominal",
        label="Software investment (nominal)",
        source=Source.FRED, source_id="B985RC1Q027SBEA",
        panel=Panel.SOFTWARE, freq=Freq.QUARTERLY, units="$B SAAR",
        seasonal=True, verified="2026-04=816.630",
        note="Private nonresidential fixed investment in software.",
    ),
    SeriesSpec(
        key="bea_software_real",
        label="Software investment (real, 2017$)",
        source=Source.FRED, source_id="B985RX1Q020SBEA",
        panel=Panel.SOFTWARE, freq=Freq.QUARTERLY, units="$B chained 2017",
        seasonal=True, verified="2026-04=956.775",
        note="Real exceeds nominal because the deflator is below 100 — the volume of "
             "software bought is growing faster than the dollars spent on it.",
    ),
    SeriesSpec(
        key="bea_software_price",
        label="Software investment price index",
        source=Source.FRED, source_id="B985RG3Q086SBEA",
        panel=Panel.SOFTWARE, freq=Freq.QUARTERLY, units="index 2017=100",
        seasonal=True, verified="2026-04=85.366",
        note="Below 100: software prices have fallen ~15% since 2017 on BEA's "
             "quality-adjusted basis, while spend has risen. The core AI-deflation chart.",
    ),

    # ------------------------------------------------------------------- Power
    SeriesSpec(
        key="pjm_western_hub_da",
        label="PJM Western Hub — day-ahead",
        source=Source.PJM, source_id="da_hrl_lmps/WESTERN HUB",
        panel=Panel.POWER, freq=Freq.DAILY, units="$/MWh",
        note="Hourly day-ahead LMP averaged to a daily mean, plus on-peak (HE 8-23, "
             "weekdays) and off-peak splits. Spot, not forwards: the Cal-27/Cal-28 "
             "strips that price the AI buildout need a paid CME/ICE feed. "
             "Needs PJM_API_KEY.",
    ),
    SeriesSpec(
        key="ercot_north_hub_da",
        label="ERCOT North Hub — day-ahead",
        source=Source.ERCOT, source_id="HB_NORTH",
        panel=Panel.POWER, freq=Freq.DAILY, units="$/MWh",
        note="Day-ahead settlement point price at HB_NORTH, daily mean plus peak/"
             "off-peak. ERCOT is energy-only with a $5,000/MWh cap, so daily means are "
             "scarcity-spike dominated — the median is the honest center. "
             "Needs ERCOT_API_KEY.",
    ),

    # ---------------------------------------------------------- Capacity auctions
    SeriesSpec(
        key="pjm_bra_clearing_price",
        label="PJM Base Residual Auction — RTO clearing price",
        source=Source.PJM_RPM, source_id="bra_rto",
        panel=Panel.CAPACITY, freq=Freq.ANNUAL, units="$/MW-day",
        note="One point per delivery year, not a time series. 2027/28 cleared at the "
             "FERC cap ($333.44) and still came up 6,623 MW short of the reliability "
             "requirement. Auction dates are irregular post-2022 — plot against "
             "delivery year, never against auction date.",
    ),

    # ---------------------------------------------------------------- Natural gas
    SeriesSpec(
        key="henry_hub_spot",
        label="Henry Hub spot",
        source=Source.FRED, source_id="DHHNGSP",
        panel=Panel.GAS, freq=Freq.DAILY, units="$/MMBtu",
        note="Daily spot. The level the futures curve is quoted against.",
    ),
]

# The Henry Hub forward curve: EIA publishes NYMEX settlements for the first four
# contracts. Four points is enough to show contango/backwardation; a full 24-month
# strip needs a paid CME/ICE feed. Registered separately because they render as one
# curve chart, not four time series.
GAS_CURVE_CONTRACTS: list[SeriesSpec] = [
    SeriesSpec(
        key=f"henry_hub_c{n}",
        label=f"Contract {n}",
        source=Source.EIA, source_id=f"RNGC{n}",
        panel=Panel.GAS, freq=Freq.DAILY, units="$/MMBtu",
        note="NYMEX settlement via EIA. Needs EIA_API_KEY.",
    )
    for n in (1, 2, 3, 4)
]

ALL_SERIES: list[SeriesSpec] = [*REGISTRY, *GAS_CURVE_CONTRACTS]


def by_key(key: str) -> SeriesSpec:
    for spec in ALL_SERIES:
        if spec.key == key:
            return spec
    raise KeyError(f"Unknown series key: {key!r}")


def by_source(source: Source) -> list[SeriesSpec]:
    return [s for s in ALL_SERIES if s.source is source]


def by_panel(panel: Panel) -> list[SeriesSpec]:
    return [s for s in ALL_SERIES if s.panel is panel]
