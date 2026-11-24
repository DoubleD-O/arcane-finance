# Step 2 design: variance view, Comex deck, Excel export

Scope: `docs/SCOPE.md` job 2 (first slice). Builds on step 1 (`docs/design-step1.md`). Expected numbers: `docs/fixture-expected.md`, section "Step 2". Defaults below apply until the owner answers Q4 (his real Comex pack structure).

## Rule: one report object, many renderings

Everything a person sees comes from one pure **VarianceReport** computed from one release (or one proposed release). The CLI table, the XLSX and every deck slide render that object and compute nothing themselves. That is what makes "deck figures match the app" true by construction, and the parity test proves it.

## Data shapes

- **PnlLine** (fixed table, in display order): `REV` Revenue; `COGS` Direct costs; **Gross margin** (subtotal); **GM %** (ratio); `PAY`, `RENT`, `MKT`, `OTH`; **EBITDA** (subtotal); **EBITDA %** (ratio); `DEP`; **Operating profit (EBIT)** (subtotal). Each line is `kind` ∈ {account, subtotal, ratio}. Subtotals sum account types the same way the step 1 KPI table does (reuse it; don't add a second definition). EBIT = all accounts.
- **VarianceRow**: scope (`APAC` or a market code), line, actual, comparison (budget), variance, variance %, label. For amounts, variance = actual − budget. Because amounts are in profit sign, a positive variance is always **Favourable** and a negative one **Unfavourable**, for revenue and costs alike; zero is "On plan". Variance % = variance ÷ |budget|, "n/m" when budget is 0. For ratio lines, variance is in **percentage points** (actual % − budget %), and the % column is blank. A ratio line's label is judged on the points as shown (rounded to 2 dp), so "0.00 pp" always reads "On plan".
- **Bridge**: EBITDA budget → one step per market (that market's EBITDA variance) → EBITDA actual. Steps must sum exactly to actual − budget. Order: markets in config order.
- **VarianceReport**: release ID (or "proposed"), period, reporting currency, approved_by/at, status (`final` for an approved release, `draft` for a proposed one), scope rows for APAC and every market, the bridge, FX rates used per currency and scenario, the facts checksum, and (when the release has a previous release) the KPI changes vs that previous release, reusing step 1's diff and materiality.
- **Commentary** (`commentary/<period>.toml`, optional): `outlook` (text) and `[[actions]]` with `action`, `owner`, `due`. Plain data, never executed. Checked on load against what fits on slide 7: outlook at most 600 characters, at most 6 actions, each action at most 120 characters. Anything over is a config error naming the limit, not a slide that overflows.

## Display conventions

Amounts in reporting currency, 0 dp in the deck and 2 dp in CLI and XLSX, thousands separators, negatives in parentheses: `(450,000)`. Percentages 1 dp in the deck and 2 dp elsewhere, ROUND_HALF_UP. Percentage points 2 dp with an explicit sign on every surface: `+0.01 pp`, `-1.04 pp`, and `0.00 pp` (labelled "On plan") for zero.

**Rule change (2026-11-21).** Percentage points were 1 dp in the deck, so APAC GM % (+0.011 pp) read "0.0 pp" next to "Favourable". They are now 2 dp everywhere. `fixture-expected.md` check D1 still says "+1.0 pp" under the old rule; under this one the deck shows "+1.03 pp", which matches the "Step 2" table there. Favourable/Unfavourable is always written as text (colour may be added but never alone).

## Commands

| Command | Does |
|---|---|
| `variance --release R [--market M]` | P&L vs budget table for APAC (or one market): actual, budget, variance, variance %, label. Without `--market`, also a market summary (Revenue, GM, EBITDA per market) and the EBITDA bridge. `--period P --proposed` runs it on the proposed release instead (marked DRAFT). Client, supplier and cost-center drill-downs are listed as "not available in this data". |
| `deck --release R --out DIR [--commentary FILE]` | Writes `DIR/Comex_<period>_<release>.pptx`. Refuses to overwrite an existing file (exit 1) unless `--force`. A different release gets a different file name, so earlier decks are never touched. `--period P --proposed` writes `Comex_<period>_PROPOSED.pptx`, always marked DRAFT. |
| `export R --out DIR` | Unchanged step 1 files, plus `release.xlsx` with sheets **P&L vs budget** (APAC then each market, numbers as real numbers with a number format: amounts `#,##0.00;(#,##0.00)`, ratios and variance % as fractions under Excel's `0.00%`, ratio variances as a number of points under `+0.00" pp";-0.00" pp";0.00" pp"`), **Facts** (same columns as `facts.csv`), **Manifest** (key/value). |

## Deck (Comex, 16:9, python-pptx, default template, native editable tables and charts)

Look and feel (template, style file, slide size): `docs/design-deck-style.md`, how-to in `docs/deck-styling.md`. Without `config/deck/` files the deck is the built-in 16:9 look described here.

Every slide carries a footer: `<release ID> · <period> · SGD · checksum <first 12> · DRAFT` (the last part only when draft). Slide 1 also shows status in words.

1. **Title.** "APAC Comex pack, October 2026". Release ID, approved by and when, reporting currency, and status. An approved release with commentary: "Status: FINAL". An approved release without commentary: "Figures approved (release <id>). Pack DRAFT: outlook and actions not provided." A proposed release: "Status: DRAFT (proposed release, not approved)", plus "; no commentary" when there is none.
2. **KPI scorecard.** Table: Revenue, Gross margin, GM %, EBITDA, EBITDA %. Columns: Actual, Budget, Variance, Var %, Favourable/Unfavourable.
3. **P&L vs budget (APAC).** All PnlLines.
4. **Markets.** Table: market × (Revenue, GM, EBITDA actual and variance). One note line per non-SGD currency: "AU translated at actual 0.86 / budget 0.88 SGD per AUD; variance includes FX."
5. **EBITDA bridge.** Native editable chart: a waterfall drawn as a stacked column. PowerPoint stacks positive values up from zero and negative values down from zero, separately and in series order, so each bar [min(before, after), max(before, after)] (totals: [min(0, v), max(0, v)]) is split at zero. An invisible base fills the gap between zero and a bar that does not touch it, then a visible series above zero and one below for each of Total, Favourable and Unfavourable. Nothing inverts when negative. Each label is centred in an invisible box a tenth of the chart's span tall, stacked just outside its bar: above when the bar reaches above zero, below otherwise. Category names sit at the bottom of the plot. Under the chart, the same numbers as a small table.
6. **Changes since <previous release>**, only when the release has a previous one. KPI changes table with Material / Below threshold as text.
7. **Outlook and actions.** From commentary. If there's no commentary, the slide says "Outlook and actions not provided for this release" and the deck is DRAFT.

Set the pptx core properties (title, created and modified = approved_at) so regenerating the same release gives the same content.

## Safety

XLSX: write every text cell as a string value, never a formula. Text starting with `= + - @` gets a leading `'` (openpyxl otherwise turns `=...` into a formula). Commentary text is inserted as plain text runs.

## Tests (behaviour)

- Every number in "Step 2" of `fixture-expected.md`, exact Decimal at the report level.
- **Deck parity**: open the written pptx with python-pptx, read every table cell and the chart's data, and check they equal the report rendered with the deck's own formatting. Check the footer release ID on every slide.
- R1 deck written, then R2 deck written: R1's file is byte-identical before and after.
- No commentary: slide 7 placeholder text and DRAFT in the footer. With commentary: the text and actions appear.
- XLSX: reopen, the P&L sheet's numbers equal the report, and a description starting `=` round-trips as text.
- The bridge sums exactly.
- Bridge chart: from the chart's series values, stack each category as PowerPoint does and check its visible range is exactly [min, max] and its label box touches the bar from outside, for the fixture, a bridge crossing zero, and an all-negative bridge.
- Commentary at the limits loads; one character or one action over is a config error.
