# Gutter Macro Dashboard

Tracking where AI shows up in the real economy: the power it needs, what grid
capacity costs, and which prices are actually moving.

Live app: one Fly machine serving a static page plus a JSON payload that a
background timer rebuilds.

```
gutter_macro/
  series.py            the registry — every series, its source, units and caveats
  build.py             pull -> cache parquet -> emit the dashboard payload
  sources/
    bls.py             CPI, PPI, CES          (keyless v1, or BLS_API_KEY for v2)
    fred.py            BEA NIPA, Henry Hub spot   (no key)
    pjm_rpm.py         capacity auction results    (no key)
    eia.py             Henry Hub futures curve     (EIA_API_KEY)
    pjm.py             Western Hub day-ahead LMP   (PJM_API_KEY)
    ercot.py           North Hub day-ahead SPP     (three ERCOT credentials)
app/
  main.py              FastAPI: /, /api/data.json, /healthz, /refresh
  static/              index.html, chart.js, app.js — no CDN, no build step
scripts/refresh.py
```

## Running it

```bash
uv sync
uv run python scripts/refresh.py            # -> site/data.json
MACRO_DATA_PATH=$PWD/site/data.json uv run uvicorn app.main:app --port 8099
```

Open http://127.0.0.1:8099. The build works with no credentials at all — it
ships the thirteen free series and reports the rest as skipped.

## Credentials

All free. Missing ones are reported and skipped, never fatal — the dashboard
renders the panels it has and lists the rest.

| Variable | For | Register |
|---|---|---|
| `BLS_API_KEY` | *optional* — raises the rate limit only | https://data.bls.gov/registrationEngine/ |
| `EIA_API_KEY` | Henry Hub futures curve | https://www.eia.gov/opendata/register.php |
| `PJM_API_KEY` | PJM Western Hub LMP | https://apiportal.pjm.com/ |
| `ERCOT_API_KEY` | ERCOT subscription key | https://developer.ercot.com/ |
| `ERCOT_USERNAME` | ERCOT B2C token flow | same registration |
| `ERCOT_PASSWORD` | ERCOT B2C token flow | same registration |

Put them in `.env` at the repo root; `refresh.py` reads it and never
overrides a real environment variable.

**Confirm a keyed API before trusting a full pull.** The three keyed modules were
written against published contracts but not yet exercised live:

```bash
uv run uv run python scripts/refresh.py --probe eia     # prints the route's declared facets
uv run uv run python scripts/refresh.py --probe pjm     # prints field names + WESTERN HUB pnode_id
uv run uv run python scripts/refresh.py --probe ercot   # prints the positional field order
```

## Deploying

```bash
fly apps create gutter-macro                                   # first time only
fly volumes create gutter_macro_data --size 1 --region iad
fly secrets set EIA_API_KEY=... PJM_API_KEY=... ERCOT_API_KEY=... \
                ERCOT_USERNAME=... ERCOT_PASSWORD=...
fly deploy --remote-only
```

`primary_region` is pinned to `iad` deliberately: **ERCOT blocks requests from
outside the United States**, and does so with an unhelpful 403.

The machine is `auto_stop_machines = false` on purpose. The refresh timer runs
in-process, and a suspended machine never wakes to rebuild — it would serve a
stale payload indefinitely while looking healthy.

## What's in it, and what each series will and won't tell you

### Wholesale power
PJM Western Hub and ERCOT North day-ahead prices, daily mean with on-peak and
off-peak splits. **Spot, not forwards.** The Cal-27/Cal-28 strips that actually
price the AI buildout trade on CME and ICE and need a paid feed; CME blocks
scripted access (its product-slate endpoint returns 403). ERCOT also carries a
median alongside the mean, because it is energy-only with a $5,000/MWh cap and a
daily mean is dominated by a handful of scarcity hours.

The two ISOs use **different on-peak conventions** — PJM is HE 8–23 Mon–Fri,
ERCOT is HE 7–22 Mon–Sat. They are defined separately in each module rather than
shared, precisely so neither drifts onto the other's definition.

### PJM capacity auctions
Parsed from PJM's official clearing-price spreadsheet, so a new auction appears
on its own. 2007/08 through 2028/29.

The product definition changed twice (single product → Annual/Ext Summer/Limited
in 2014 → Capacity Performance in 2018). Prices are **not like-for-like across
those breaks**, so the payload carries a `comparable_to_prior` flag and the chart
shades those bars rather than drawing one continuous line.

Plot against **delivery year, never auction date** — auction timing has been
irregular since 2022.

### Natural gas
Henry Hub spot from FRED, plus contracts 1–4 from EIA as a curve across tenor.
Four points shows contango vs backwardation; a full 24-month strip needs a paid
feed. The curve is only drawn for dates where **every** contract settled, so it
never shows a shape stitched from different days.

### Prices
| Series | ID | Note |
|---|---|---|
| Electricity | `CUSR0000SEHF01` | Retail; lags wholesale by quarters — rate cases set it |
| Shelter | `CUSR0000SAH1` | The baseline AI doesn't touch |
| Specialty trade wages | `CES2023800003` | Electricians, plumbers, HVAC — what the buildout bids for |
| Offices of lawyers | `PCU541110541110` | PPI |
| Tax prep & accounting | `CUUR0000SS68023` | NSA |
| Internet services | `CUUR0000SEEE03` | NSA |
| Computer software | `CUUR0000SEEE02` | NSA, quality-adjusted |
| Medical care services | `CUSR0000SAM2` | |

**CPI legal services (`CUUR0000SEGD01`) is unusable** — 9 sparse semiannual
observations in four years, the most recent a literal `-`, nothing in 2026. PPI
offices of lawyers replaces it.

The granular NSA items are **not on FRED** (all 404) and must go through the BLS
API. `sources/fred.py` raises an error that says so by name.

### Software investment
BEA via FRED: nominal, real (chained 2017$), and the price index. Real exceeds
nominal because the deflator is below 100 — more software is being bought than
the dollar figure suggests.

## Time ranges

Every chart switches between **1Y / 3Y / 5Y / 10Y** from one control row; 3Y is the
default. The payload always carries the widest range (10 years, ~91 KB compact) and
the page narrows it, so switching is instant and costs no request.

**The transform and the window do not commute**, and getting the order wrong fails
silently. `prepare()` in `app.js` encodes it:

| View | Order | Why |
|---|---|---|
| Year over year | transform, **then** window | Needs a year of data *before* the window opens. Windowing first makes the 1Y chart render completely empty. |
| Indexed | window, **then** transform | The =100 base must be the start of the window the viewer chose, not the start of all history. |
| Level | window only | |

Windowing is measured from `latest_date` — the newest observation in the payload —
not from the viewer's clock, so a stale payload or a different timezone can't shift
the window under the data.

Capacity auctions filter by **delivery year** rather than by date, since they are
yearly events. The gas forward curve's far comparison snapshot follows the selected
range (1Y → "1 year ago", 10Y → "10 years ago").

**A series with less history than the window is called out by its start date, not
its span.** BEA publishes a quarter behind, so its span inside a 10-year window is
always a few months short — that is lag, not missing history, and flagging it would
be a permanent false alarm on three panels. `startsInsideWindow()` compares
`earliest` against the window start with one publication period of slack.

## Reading the charts

**Index bases differ between series** (Dec 1988=100 vs 1982-84=100 vs $/hour), so
panels that mix them are shown **indexed to 100 at the window start** and the
Level view is disabled for them. That rule is `canShowLevel` in `app.js`, and it
compares units strings exactly — two series both labelled "index" on different
bases are still not comparable.

One genuine divergence worth watching: **consumer software CPI rose ~25% over the
year to Aug 2026 (22.4 → 28.1) while BEA's software investment deflator fell
~1%.** Not an error — they measure different things (what households pay for
packaged software vs what businesses pay to build it).

## Colour

The palette is validated, not chosen by eye — three categorical slots and a
single-hue ordinal ramp, checked in both light and dark against the lightness
band, chroma floor, CVD separation and contrast. Re-run before changing it:

```bash
node <dataviz>/scripts/validate_palette.js "#2a78d6,#eb6834,#1baf7a" --mode light
```

Aqua sits below 3:1 on the light surface, so the relief rule applies: every chart
ships direct endpoint labels and a table view.

## Tests

```bash
uv run pytest -q                         # 147 Python tests
node --test tests/js/transforms.test.mjs   # 27 JS tests
```

Charts are also verified visually — the validator checks colour, not layout:

```bash
node tests/js/screenshot.mjs        # light / dark / phone, and every range x view
```

It asserts no console errors, no horizontal overflow at any width, and that no
panel renders empty in any of the twelve range x view combinations. That last
check is what catches the year-over-year ordering bug, which produces a blank
1-year chart and no error.

`tests/fixtures/pjm_rpm_clearing_prices_2026-09-17.xlsx` is PJM's real published
workbook, committed so the parser keeps being tested against the actual layout.
If PJM restates a historical auction, that test fails loudly.
