# Step 1 design: foundation + regional close

Scope: `docs/SCOPE.md` step 1. Expected numbers: `docs/fixture-expected.md` (reference evidence; do not edit to fit code).

## Decisions (defaults accepted on 2026-11-21; flag if code needs a change)

| Topic | Rule |
|---|---|
| Sign | Canonical amount is **profit sign**: revenue +, costs −. KPIs are plain sums. Each mapping declares its source convention (see Mapping). |
| Money | `decimal.Decimal` end to end; parse TOML with `parse_float=Decimal`. DuckDB `DECIMAL(38,10)`. Round only for display: 2 dp, ROUND_HALF_UP. Never `float`. |
| FX | Markets submit in local currency. One rate file per period with `actual` and `budget` rate sets, quoted as reporting currency per 1 unit of local. Each scenario is translated at its own rate type (actual at actual, budget at budget). Reporting currency SGD. A missing rate blocks publish. |
| Scenarios | `actual`, `budget`. Each (market, period, scenario) is one submission slot. |
| Materiality | Per KPI: material if \|Δ\| ≥ `abs` (reporting currency) **or** \|Δ%\| ≥ `pct`, tested at market level and APAC level. Δ% vs a zero base is "n/m" and only the `abs` test applies. |
| Signals | Derived from current state on every read, so they are deduplicated by construction and disappear when the condition clears. Acknowledge/resolve workflow is deferred. |

## Data shapes

Typed frozen dataclasses (or similar) at the boundary; parse files once, trust internal types.

- **CloseConfig** (`config/close.toml`): reporting currency, markets (`code`, `name`, `currency`, `owner`, `mapping` path), expected scenarios, `due_day` (calendar day of the month after the period by which actuals are due), the regional owner, materiality table, chart-of-accounts path.
- **ChartOfAccounts** (`config/chart_of_accounts.toml`): regional `code` → `name`, `type` ∈ {revenue, cogs, opex, below_ebitda}. KPIs are a fixed table over types: Revenue = revenue; Gross margin = revenue + cogs; EBITDA = revenue + cogs + opex.
- **Mapping** (`config/mappings/<market>.toml`): layout (`format` csv|xlsx, `sheet`, `header_row` 1-based, `account_column`, `description_column`, `amount_column`, optional `period_column`), `sign` ∈ {`tb` (credit negative: canonical = −source), `pl_natural` (all amounts shown positive: canonical = +source for revenue, −source otherwise, by regional account type)}, and `[accounts]` source code → regional code. **Mapping version** = first 12 hex of sha256 of the file bytes.
- **FxRates** (`fx/<period>.toml`): `[actual]` and `[budget]` tables, currency → rate (as strings or TOML floats parsed to Decimal).
- **Submission**: identity = (slot, sha256 of file bytes), so the same file can serve another period or scenario. Fields: market, period, scenario, file name, sheet. Each time it is received it gets a **receipt** (received_at); its received_at is its newest receipt. Raw rows stored (row number in the source file, source account, description, raw amount text, parsed amount) so remapping needs no file.
- **Validation**: one per (submission, mapping version), idempotent on that key. `state` ∈ {rejected, needs_review, validated}, a list of **ValidationException** (`code`, row, detail), mapped facts (regional account, local amount, source row), and a tie-out (source total, mapped total, unmapped total).
- **Release**: immutable. `release_id` = `<period>-R<n>`. Holds release facts (market, scenario, regional account, local amount, currency, rate, reporting amount, submission id, file name, source row), the FX rates used, and a **manifest** (release id, period, previous release id, approved_by, approved_at, reporting currency, per slot: submission id/file/received_at/mapping version, rates, CoA version, KPI totals per market and APAC, and a facts checksum = sha256 over the sorted canonical fact rows). A per-period **current release pointer**.

## Validation rules (registry: code → check → resulting state)

| Code | Trigger | State |
|---|---|---|
| MISSING_COLUMN | A configured column isn't in the header row. Detail names the closest header (difflib). | rejected |
| WRONG_PERIOD | `period_column` present and any value ≠ the declared period | rejected |
| BAD_AMOUNT | Amount cell isn't a number. Accepts `1,234.50`, `(1,234.50)`, `-1234`, blank or `-` as zero. Formula with no cached value counts as bad. | needs_review |
| UNMAPPED_ACCOUNT | Source account not in the mapping. Amount stays in the unmapped tie-out. | needs_review |
| DUPLICATE_ROW | Same source account, description and amount appear more than once | needs_review |

The worst state wins (rejected > needs_review > validated). Rows with an empty account cell and no amount are skipped.

## Market status (derived per period, per expected market × scenario)

`missing` → `rejected` / `needs_review` / `validated` (not in the current release) → `approved` (the latest submission's validation equals what the current release holds). The latest submission = the one with the newest receipt for the slot. A validated latest submission that differs from the release is `changed`, carrying its KPI delta and materiality.

## Needs-attention signals

Each has: type, market, scenario, period, owner, due date, trigger detail, release id it relates to, impact (KPI deltas, or "unknown" when the data can't support one), next action.

1. **MISSING_SUBMISSION**: expected slot has no submission. Overdue once `as_of` passes the due date.
2. **VALIDATION_FAILED**: latest submission is rejected or needs_review. Lists the exceptions. Impact unknown. The approved release stays current.
3. **MATERIAL_CHANGE**: latest validated submissions would change a KPI materially vs the current release. Owner = regional owner; next action = review the delta and approve or reject. A below-threshold change shows as `changed` in status but raises no signal.

## Publish (atomic)

`publish --period P` builds the proposed release from the latest validated submission per expected slot, using the current mapping versions and the period's FX file. It refuses (exit code non-zero, nothing written) when any expected slot is missing or not validated, a latest validation uses a stale mapping version, or an FX rate is missing. Without `--approve` it is a dry run: it prints the KPI diff against the current release and writes nothing. With `--approve --by "<role>"` it inserts the release, its facts and manifest, and moves the pointer in **one DuckDB transaction**. If any step fails, the transaction rolls back and the previous release stays current. Publishing a release whose facts checksum equals the current release's is a no-op that reports the existing ID.

## CLI (`fct`, all commands take `--store` defaulting to `~/.arcane-finance/store.duckdb` and `--config` defaulting to `config/close.toml`)

| Command | Does |
|---|---|
| `inspect FILE --market M` | Routing preview: sheets, header row found, configured columns found or missing (with closest match), each source account with amount and its mapping or "UNMAPPED" |
| `ingest FILE --market M --period P --scenario S [--received-at ISO]` | Stores the submission and validates it. A repeat of the slot's latest file under the same mapping is a no-op that says so. An earlier file sent again gets a new receipt and becomes the latest. An ingest that is not the slot's latest says which file is |
| `revalidate --period P` | Re-validates each latest submission under the current mappings from stored rows |
| `status --period P [--as-of ISO]` | Market × scenario status board, then needs-attention signals |
| `publish --period P [--approve --by ROLE]` | As above |
| `diff --from R1 --to R2` | KPI and account-level deltas between two releases, by market and APAC, with materiality |
| `trace --release R --market M --account A` | Release facts behind one figure, down to file and source row |
| `verify-release R` | Recomputes the release from stored rows, mapping content stored at publish time, and stored rates; checks the facts checksum |
| `export R --out DIR` | Writes `facts.csv` (one row per release fact) and `manifest.json` |

## Modules (`src/finance_close_toolkit/`)

Organized by domain, not by pipeline phase. `money` (parsing and display), `config` (typed loading and versions), `intake` (reading CSV/XLSX into raw rows per layout, `inspect`), `validation` (rule registry), `kpi` (KPI table, diffs, materiality), `store` (DuckDB schema and queries, the only module that touches SQL), `release` (propose, publish, verify, export, trace), `attention` (status and signals), `cli` (argument parsing and printing only). Business logic is callable without the CLI.

## Safety

openpyxl `read_only=True, data_only=True` (never evaluate formulas or macros). Refuse files over 20 MB or 100k rows. CSV export cells that start with `=`, `+`, `-`, `@` and aren't numbers get a leading `'`.
