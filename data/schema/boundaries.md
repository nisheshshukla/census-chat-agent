# Dataset boundaries

What the agent can and cannot answer, derived from introspecting the share
`US_OPEN_CENSUS_DATA_NEIGHBORHOOD_INSIGHTS_FREE_DATASET` (SafeGraph, Snowflake Marketplace).

## What is in the data

| Source | Tables | Geography | Rows | Notes |
|---|---|---|---|---|
| ACS 5-year 2016–2020 ("2020" vintage) | `2020_CBG_B01` … `2020_CBG_C24` (29 tables, ~8,100 columns) | Census block group (2020 boundaries) | 242,335 CBGs | Default vintage. Estimates (`…e#`) with margins of error (`…m#`). Dollar values in 2020 inflation-adjusted dollars. |
| ACS 5-year 2015–2019 ("2019" vintage) | `2019_CBG_*` (29 tables) | Census block group (2010 boundaries) | 220,333 CBGs | Same column layout. Enables a limited 2019-vs-2020 comparison at state/county level (block groups were redrawn, so CBG-level comparison is not valid). |
| 2020 Decennial Census (PL 94-171 redistricting) | `2020_REDISTRICTING_CBG_DATA` (298 columns) | Census block group (2020) | 242,335 | Exact counts, not estimates: total population, race, Hispanic origin, 18+ population, housing units and occupancy, group quarters. |
| Metadata | `*_METADATA_CBG_FIELD_DESCRIPTIONS`, `*_METADATA_CBG_FIPS_CODES`, `*_METADATA_CBG_GEOGRAPHIC_DATA` | | | Column meanings (hierarchy path, universe, topic), county names per FIPS, land/water area and centroid per CBG. |
| Geometry | `*_CBG_GEOMETRY`, `*_CBG_GEOMETRY_WKT` | CBG polygons | | Heavy; not needed for population questions. |
| SafeGraph patterns | `2019_CBG_PATTERNS` | CBG | 220,735 | Foot-traffic sample from Oct 2018 (visits, visitor home CBGs, popular brands). Not Census data; out of scope for the agent. |
| Derived | `2019_RENT_PERCENTAGE_HOUSEHOLD_INCOME`, `2019_TOTAL_RENTAL_GEO` | CBG | | Convenience extracts of B25070; superseded by the ACS tables. |

ACS table families present (both vintages): B01 sex/age, B02 race, B03 Hispanic origin, B07 mobility, B08 commuting, B09 children/relationship, B11 household type, B12 marital status, B14 school enrollment, B15 educational attainment, B16 language, B17 poverty, B19 income, B20 earnings, B21 veterans, B22 food stamps/SNAP, B23 employment, B24 occupation/industry, B25 housing (1,700+ columns), B27 health insurance, B28 computers/internet, B29 citizen voting-age population, B99 allocation flags, C02/C15/C16/C17/C21/C24 collapsed versions.

## Geography hierarchy

Block group id = state FIPS (2) + county FIPS (3) + tract (6) + block group (1). Any question at
state or county level is a sum (for counts) over CBGs sharing that prefix. `CENSUS_APP_DB.SEMANTIC.GEO_CBG`
maps every CBG to state and county names.

Available levels: nation, state (50 + DC + Puerto Rico), county (3,234), tract, block group.

## What is NOT in the data (fast-fail classes)

- **Cities, towns, places, ZIP codes, metro areas, congressional districts, school districts.** The nearest available geography is the county. Los Angeles → Los Angeles County; Springfield → ask which county/state.
- **Time series beyond two ACS vintages.** No 2010 or earlier, no 2021+, no monthly data, no projections or forecasts.
- **Individuals or households by name or address.** Only aggregates.
- **Non-US geographies.**
- **Topics outside ACS/Decennial**: crime, weather, elections, prices, GDP, business counts.
- **True medians above block-group level.** Median income/age/home value exist per CBG. A county or state median is not derivable exactly; the agent reports a household- or population-weighted average of CBG medians and says so, or declines.

## Data semantics the agent must respect

- `…e#` columns are estimates; `…m#` are 90% margins of error. Aggregate MOE for a sum = sqrt(sum of squared MOEs). Coefficient of variation > 30% means low reliability.
- Counts are summable across CBGs; medians, means, per-capita values, ratios, and percentages are not (`IS_SUMMABLE = FALSE` in `FIELD_DESCRIPTIONS`).
- Percent-of-population questions are computed as a ratio of two summed counts, not by averaging CBG percentages.
- Column names are mixed case and must be double-quoted in SQL: `"B01003e1"`. Table names start with a digit and must be double-quoted: `"2020_CBG_B01"`.
- Decennial 2020 counts and ACS 2016–2020 estimates differ by design (point-in-time count vs. 5-year rolling estimate). The agent states which one it used.
