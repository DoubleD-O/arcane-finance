# Scope: what Arcane Finance App does

Draft, 2026-11-21. Source: the original brief plus the owner's decisions:
- **Purpose:** a real tool a regional finance team would run, sequenced so a shareable synthetic demo exists early.
- **Vision:** all four jobs below.

Open questions are marked **Q**. Defaults in *italics* apply unless the owner changes them.

## Users

| Role | What they do in the app |
|---|---|
| Country controller / Country Finance | Submits monthly files, corrects them, explains changes |
| Regional FP&A lead (primary user) | Consolidates, reviews, approves and publishes the regional release; builds the Comex pack |
| AP and Accounting | Link invoices to accruals; confirm reversals in the ledger (job 3) |
| Comex / leadership | Read the pack. They don't log in to the app |

## One foundation under all four jobs

Every job reads and writes the same canonical model. Building the foundation once is what makes the jobs connect: a resubmission flows into the variance, the deck and the plan's actuals.

- **Dimensions:** entity/market, period, account (regional chart of accounts), scenario (actual/budget/forecast/plan) and version, currency, and later client, supplier, cost center and product.
- **Facts:** monthly GL/financials first. AP invoice lines, accruals, payroll/workforce and plan drivers come later, each with its own grain and keys.
- **Release:** an immutable, approved snapshot with an ID and a manifest (source file hashes, mapping version, FX rates). All outputs come from a release, never from raw data.
- **Lineage:** every figure traces back to its file, sheet, row and mapping rule.
- **Money:** decimal, with documented rounding. Never floats.

## Job 1: Regional close

**Does:** takes country Excel/CSV submissions, maps them to the regional chart of accounts, validates and reconciles them, tracks each market's status (received → validated → needs review → approved), and publishes a versioned release. When a market resubmits after sign-off, it shows the exact change to every KPI and preserves the old release.
**Done when:** two markets, one late resubmission with a new account. The app shows the EBITDA delta before approval, keeps the October release reproducible, and lists the needed actions (missing, stale or failed submissions; material changes).
- **Q1. FX (decided 2026-11-22):** each market submits in local currency and the release holds its rate table. The reporting currency must be **the user's choice** (SGD in the demo today; it already comes only from `reporting_currency` in `config/close.toml`, with no SGD in the code). Still to build: rate files quoted against one base currency with cross rates, so a user can pick SGD, USD or another currency per report or deck without a new rate file. Releases keep storing local amounts and rates, so a past release can be shown in another currency without changing what was approved.
- **Q2. Submission shape (decided 2026-11-22):** the tool ships sensible default templates and lets each user customise. Country submissions already work this way (a saved mapping per market translates any layout). Still to do: the same mapping approach for the close-controls files (accruals, invoices, GL), which use fixed CSV templates today, plus a downloadable default template per file type.
- **Q3. Materiality:** *a change is "material" above an absolute and a percentage threshold per KPI, set in config.* Do you have typical thresholds?

## Job 2: Reporting and decks

**Does:** actual vs budget, forecast and prior year, with amounts, %, percentage points and favorable/unfavorable labels that understand expense signs. Drill-down from APAC → market → account, and later to client, supplier and expense driver. Exports: flat XLSX/CSV, and a Comex PowerPoint (KPI scorecard, P&L vs plan, market bridge, outlook and actions) stamped with the release ID.
**Done when:** every deck figure matches the app for the same release ID, and regenerating after a resubmission produces the revised deck while the old one is preserved.
- **Q4. Comex pack (decided 2026-11-22):** ship a sensible default pack and let the user customise which slides appear, their order and titles, with no code. Today the 7-slide order is fixed in code; only the look (template, colours) is customisable. Still to do: a pack definition file (for example `config/deck/pack.toml`) listing the slides, with the current 7 as the default.
- *Board and Sales packs come after the Comex pack.*

## Job 3: Close controls

**Does:** Finance raises an accrual, AP links the invoice (including partial or ambiguous matches), and Accounting confirms the reversal or clearing in the ledger. All three see the same evidence, amount at risk, status and next owner. An item closes only with ledger evidence or an approved resolution.
**Done when:** the reference fixture works end to end: a S$100k accrual with a S$110k invoice, a missing reversal, a partial match and an absent owner. April nets S$10k under the fixture's reversal policy.
- **Q5. Inputs:** *accrual register and AP invoice lines arrive as files, the same way as job 1.* No live ERP connection until approved.

## Job 4: Planning and modeling

**Does:** a driver-based plan from assumption pages (volume/price/mix, costs, hiring, working capital, capex, debt), rolled through the P&L, balance sheet and cash flow. Year 1 is shown by month and Years 1–3 by year, with base/upside/downside scenarios. Also price-volume-mix bridges and a workforce schedule. Plans become the "budget/forecast" scenario that jobs 1–2 compare against.
**Done when:** a small fixture ties out every month (balance sheet balances, cash ties), and changing one driver flows correctly through all three statements.
- *Hand-checkable fixture first: one entity, two products, 12 months. The 36-month horizon comes after that ties out.*
- **Q6. AI-led model setup:** in scope for job 4, or later? *Later. The manual setup produces the same configuration.*

## Sequence (demo first, but each step is real)

| Step | Builds | Why here | Demo after this step |
|---|---|---|---|
| 1 | Foundation + job 1 | Everything else reads releases | Resubmission diff in the CLI |
| 2 | Job 2: variance + Comex deck | Completes the 90-second demo story | **Shareable demo**: country file → approved pack → correction → revised slide |
| 3 | Thin UI (Streamlit) over steps 1–2 | Lets a fresh user try it uncoached | Interactive demo |
| 4 | Job 3: close controls | Reuses intake, lineage and owners | Finance/AP/Accounting handoff |
| 5 | Job 4: planning | Largest and most self-contained; feeds plan scenarios back into job 2 | Driver change → three statements |
| Later | PostgreSQL/Sheets publishers, Board/Sales packs, read-only AI assistant | Wait until a real deployment needs them | — |

## Out of scope for now

Real company data, employer system connections, a public repo or hosting, and live AI calls.
