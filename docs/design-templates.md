# Default templates and customisation

Decision (2026-11-22): the tool ships sensible defaults for the Comex pack and for every input file, and each user can customise them without code. Two independent parts.

## Part A: customisable decks (pack definitions)

A **pack file** (TOML) says which slides a deck has, in what order, with what titles and options. The look still comes from `config/deck/template.pptx` and `style.toml`; numbers still come only from the VarianceReport.

```toml
name = "Comex pack"
title = "APAC Comex pack, {period_long}"     # placeholders: {period}, {period_long}, {release}, {currency}

[[slides]]
type = "title"

[[slides]]
type = "kpi_scorecard"
title = "KPI scorecard, APAC"
kpis = ["revenue", "gross_margin", "gm_pct", "ebitda", "ebitda_pct"]

[[slides]]
type = "pnl"
scope = "APAC"          # "APAC", a market code, or "each_market" (one slide per market)

[[slides]]
type = "markets"

[[slides]]
type = "bridge"
kpi = "ebitda"          # revenue | gross_margin | ebitda

[[slides]]
type = "changes"        # skipped automatically when the release has no previous release

[[slides]]
type = "outlook"
```

Rules:
- Slide types are a **registry** (type → options schema → renderer). Unknown types, unknown keys, unknown KPIs or market codes are clear errors naming the file and slide number. Every type may set `title`; defaults are today's titles.
- **Shipped packs** in `config/deck/packs/`: `comex.toml` (exactly today's 7 slides, the default), `board.toml` (title, KPI scorecard, EBITDA bridge, outlook), `market_review.toml` (title, markets, P&L for each market, changes). The default pack must reproduce today's deck exactly (existing digest and parity tests stay green).
- The pack name goes in the deck file name (`<Pack>_<period>_<release>.pptx`, keeping `Comex_…` for the default) and on the title slide. The footer stays code-owned.
- An `outlook` slide without commentary makes the deck DRAFT (as today); a pack without an outlook slide does not need commentary.
- A pack whose slides all skip for a release (for example only `changes` on a first release) is refused, "no slides to render for this release", and writes no deck. A slide title longer than 100 characters after its placeholders are filled is an error naming the pack file and slide number. Both are checked before any slide is rendered.
- CLI: `fct deck … --pack NAME|FILE` (name = exactly a file stem in `config/deck/packs/`, case-sensitive on every OS), `fct deck packs` lists available packs with their slides; a broken pack file is listed with its error (the other packs still list) and the exit code is 1. UI downloads: a pack selector showing each pack's name and key (shipped packs plus a custom file upload, validated). Prepared downloads belong to the release, the pack, and the content hash of an uploaded pack file and commentary file: replacing either upload hides them until they are prepared again.
- Parity: for every shipped pack, every table cell and chart value equals the report; a bridge on revenue or gross margin sums exactly like the EBITDA bridge.
- Docs: a "Choosing and editing packs" section in `docs/deck-styling.md`.

## Part B: input templates and mappings for every file

Country submissions already use a saved mapping per market (`config/mappings/<market>.toml`). Close-controls files (accruals, invoices, GL) use fixed CSV columns today. Bring them to the same model and give users ready-to-fill templates.

- **Default templates**: `fct templates write --out DIR` writes a ready-to-fill file per input type: `submission_template.xlsx` (Period, Account, Description, Amount, with an Instructions sheet), and `accruals_template.xlsx`, `invoices_template.xlsx`, `gl_template.xlsx` (the canonical columns, with an Instructions sheet listing each column, type, sign convention and an example row). Same in CSV with `--csv`. The UI offers these as downloads on the Submit page and the Close controls page. Every Instructions sheet (and `<kind>_instructions.csv`) is generated from the workspace config: the period format, the sign convention, the column table and the regional accounts (code and name) from the configured chart of accounts, so it cannot drift from what the import accepts. `fct templates write` takes `--config` for that reason.
- A **default submission mapping** `config/mappings/_regional_template.toml` reads `submission_template.xlsx`, so a market that adopts the regional template only points its `mapping` at it.
- **Controls mappings**: optional `config/controls/mappings/<kind>.toml` (kind ∈ accruals, invoices, gl) with the same layout block as submission mappings (format csv|xlsx, sheet, header_row, one source column per canonical column) and an amount sign option (`as_is` or `negate`). With no mapping file, the canonical template columns are expected (today's behaviour). `--mapping FILE` overrides per import. A file's extension picks csv or xlsx through one rule, `controls.source_format` (.csv; .xlsx and .xlsm), which the UI's uploaders and format warning use too. Reuse step 1's reading and number parsing (accounting formats, closest-header suggestion on a missing column, formula cells without cached values are bad amounts).
- `fct controls inspect FILE --kind K` previews: columns found or missing (with closest match), first rows as they will be read, and any parse problems, without importing. It exits 1 when the file would be refused and 0 when it would import. When every mapped column is missing, it looks for the header row in the first 20 rows and suggests it ("headers found on row 4; set header_row = 4"); the import refusal says the same. A GL file whose amounts are all positive imports with a warning, "all GL amounts are positive; check amount_sign" (reversals of expense accruals are negative). The UI import shows the same preview before import; both render `controls.preview`, so the wording lives in one place.
- A period cell Excel turned into a date (`2026-10` typed into a date-formatted cell reads as 2026-10-01) is still refused, for submissions (WRONG_PERIOD) and controls files alike, and the error says how to fix it: "format the Period column as text and type 2026-10", naming the file's own column header.
- Lineage stays file + row of the source file. Imports remain idempotent by content hash: the file's bytes plus, when a mapping read it, the mapping's parsed meaning (format, sheet, header row, columns, amount sign), never its raw text, so a comment-only or reordering edit is the same import. A mapping's version is the hash of that meaning.
- Re-importing bytes that are stored but not current (for example after a wrong-mapping import) makes that import current again, recorded as a new receipt like a step 1 re-receive; the message names the import it replaces and both mappings. Re-importing the current import is a no-op. `controls status` and `controls show` list each current import with what read it (`mapping <name> <version>` or `template columns`).
- Test: the C1 story imported from differently laid-out xlsx exports (renamed and reordered columns, title rows above the header, amounts like `1,234.50` and `(1,234.50)`) through mappings gives exactly the same states, signals and P&L as the canonical CSVs.
