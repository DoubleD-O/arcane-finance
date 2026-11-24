# Planning part two: 36 months, plan as budget, reporting-currency choice

Three slices. Expected numbers: `docs/fixture-expected.md`, sections "Planning part two" and "Reporting currency". Builds on `docs/design-step5.md`.

## A. 36-month horizon

- `months` may be up to 36 (validated 1–36). Output: Year 1 by month, plus an annual column per fiscal year (FY2027, FY2028, FY2029). Monthly detail for every month stays available (CLI `--months all`, xlsx, UI drill-down).
- **Fiscal year** starts at `setup.fiscal_year_start` (a month number, default = the month of `first_month`). Tax year-to-date runs on the fiscal year (this replaces "calendar year" in the step 5 rule; for a January start nothing changes).
- **Annual inputs for later years.** `[volume] annual_units = { "2028" = "30000", "2029" = "36000" }` (key = the calendar year in which that fiscal year starts). A year with an annual figure spreads it to months by the seasonality indices (units × index ÷ sum of indices, rounded per the step 5 remainder rule); a year without one keeps the monthly growth rule. Show the spread in the volume schedule.
- **Year-end rollover.** The balance sheet shows equity as share capital, retained earnings brought forward (opening + previous fiscal years' profit − dividends) and profit for the year to date. At each fiscal year start, the previous year's profit moves into retained earnings brought forward. Total equity is unchanged by the rollover; the check must still tie every month.
- Annual ratios are recomputed from annual amounts; annual balance-sheet figures are the closing month; the lowest monthly cash is shown for each year and overall.

## B. Plan as budget

- A plan can produce the **budget submission** for one market, so the close compares actuals against the plan. Mapping in the plan file:
  ```toml
  [budget]
  market = "SG"
  [budget.accounts]          # plan line -> regional account code
  revenue = "REV"
  cogs = "COGS"
  payroll = "PAY"
  "opex.RENT" = "RENT"
  depreciation = "DEP"
  ```
  Every P&L line above PBT must be mapped or the command refuses and lists the unmapped lines. Interest and tax are below EBIT and are not part of the regional P&L; say so in the output. The plan currency must equal the market currency.
- `fct plan to-budget PLAN --period YYYY-MM [--scenario S] --out DIR` writes the month as a filled **regional submission template** (`templates.py` format, the file name names plan, version, scenario and period). The user then ingests it like any budget file (`fct ingest … --scenario budget` with the market's mapping pointing at `_regional_template.toml`, or the regional template mapping passed explicitly), so lineage, validation and releases work unchanged.
- UI: on the Planning page, "Export as budget" (period, scenario) gives the file as a download.

## C. Reporting-currency choice

- Rate files keep quoting every currency against one **base currency** (`reporting_currency` in `close.toml`, SGD in the demo). A user can view any report or deck in another currency listed in the period's rate file, as long as the release stores its rate.
- **Publishing stores every rate in the period's rate file** with the release (rates table and manifest), not only the markets' currencies. The facts and their checksum are unchanged, so existing release IDs, checksums and deck digests stay the same.
- **Conversion:** an amount in base currency is converted to presentation currency P by dividing by P's rate for the same scenario (actual amounts at the actual rate, budget at the budget rate), at full Decimal precision, rounding only on display. Ratios (GM %, EBITDA %) are recomputed from converted amounts. Bridges, scorecards and P&L tables are built from the converted report so they still sum exactly. Built: each (scenario, market, account) balance is divided and kept at the store's 10 dp scale, then totalled; dividing at 28 significant digits without that scale leaves the gross-margin bridge 4e-22 short. KPI-change materiality stays judged in the base currency, where its thresholds are set.
- Commands: `fct variance … --currency USD`, `fct deck … --currency USD`, `fct export … --currency USD` (adds converted columns to `release.xlsx`). Default = base currency. A currency not in the release's stored rates is refused with the list of available ones. UI: a currency selector on Results and on the deck downloads.
- Every converted output says so: CLI heading, deck footer and title slide ("USD, converted from SGD at actual 1.30 / budget 1.32 SGD per USD"), xlsx Manifest.
- Planning and close controls stay in their own currency for now.

## Decisions made while building A and B

- **Fiscal-year labels** name the calendar year the fiscal year starts in (FY2027 for Jul-27 to Jun-28), matching the `annual_units` key. A plan that starts or ends mid-year has part years; the CLI names them ("Part years: ...").
- **Year 1** is the first twelve plan months. Every plan, including a 12-month one, labels its annual column by fiscal year (FY2027), not "Year".
- **Seasonality** in a plan over 12 months may give 12 indices, repeated each plan year; one index per month still works. Annual units are spread to two decimals and need a whole fiscal year inside the plan, with indices that sum above zero.
- **Rollover**: retained earnings brought forward also takes dividends in the month they are paid. A plan that starts mid fiscal year puts all of the opening retained earnings into brought forward.
- **Lowest cash** per fiscal year is a warning line in the CLI and xlsx, and a metric per year on the Planning page.
- **Annual ratios**: the plan statements carry no ratio lines yet, so there is nothing to recompute.
- **Budget mapping** covers the lines above EBIT: revenue, cogs, payroll, each `opex.<id>`, depreciation. Mapping `interest` or `tax`, or a line the plan does not have, is a load error; an unmapped line is a `to-budget` refusal. `to-budget` reads `close.toml` (`--config`, default `config/close.toml`) for the market's currency and the chart of accounts, and also refuses an account outside the chart and a market that is not configured.
- **Sign**: the file holds amounts as the regional template states, revenue and costs both positive (COGS 10,000 for the "(10,000)" in `docs/fixture-expected.md`).
- **File name**: `budget_<market>_<period>_<plan>_<version>_<scenario>.xlsx`.
- **Planning page budget export** uses the page's scenario and the sidebar's close.toml.

## Disclosure rules

What each output says so the reader does not misread converted figures, part years or a budget file.

- **Translation effect.** Wherever converted (non-base) figures appear, one line says the variance mixes performance with the rate difference: "Variance in USD includes the translation effect of actual 1.30 vs budget 1.32 SGD per USD; a line on plan in SGD can show a USD variance." (rates and currencies from the release). It sits under the CLI `variance` heading, as a caption under the UI Results heading, on the deck's title slide and under the table on every P&L slide, and as `presentation_translation` in the release.xlsx Manifest. Base-currency outputs carry no such line and are unchanged.
- **Market FX notes in a converted view** say the market goes through the base currency: "AU: local AUD translated to SGD at actual 0.86 / budget 0.88 SGD per AUD, then to USD as above." The base view keeps "AU translated at actual 0.86 / budget 0.88 SGD per AUD; variance includes FX."
- **Converted changes tables** (UI Results, deck changes slide) carry "Materiality is judged in SGD, where the thresholds are set." and "2026-10-R1 restated at 2026-10-R2's rates." (for a proposed release, "at the proposed release's rates"). When the previous release stored no rate for the presentation currency it adds "2026-10-R1 did not store a USD rate."; when it stored a different one, "2026-10-R1 stored actual 1.31 / budget 1.31 SGD per USD; its amounts here use 2026-10-R2's actual 1.30 / budget 1.32." The CLI prints no changes table for a release in another currency (`variance` shows none; `status`, `publish` and `diff` stay in the base currency), so it has nothing to caption.
- **Release xlsx headers.** With converted columns present, the base columns name the base currency too ("Actual (SGD)" beside "Actual (USD)"). Without them the headers stay "Actual", "Budget", ... so existing base exports are byte-for-byte the same shape.
- **Part fiscal years.** A part year's column heading shows its months, first year included ("FY2026 (3 mo)", "FY2029 (9 mo)") in the plan xlsx and on the Planning page; the label itself (FY2029, the `annual_units` key) is unchanged, and the CLI keeps plain labels with its "Part years: ..." line. That same line is a "Fiscal years" row in the xlsx Checks sheet and a caption on the Planning page.
- **Currency codes** on input are case-insensitive: `--currency usd` is USD.
- **Budget file provenance.** The budget .xlsx's Instructions sheet starts with a Source block (plan, plan version, scenario, market, period, currency), so where the file came from survives a rename.
- **Seasonality.** Index i applies to plan month i (index 1 to the first plan month, whatever its calendar month); 12 indices repeat each plan year. The Seasonality index row shows no annual total.
