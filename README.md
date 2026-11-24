# Arcane Finance: an APAC regional close toolkit

**From country submissions to an approved Comex pack. See what changed, reconcile the figures, and reproduce the version leadership saw.**

A working Python prototype of the monthly regional close, built on synthetic data. Country controllers send their numbers in their own Excel or CSV layouts. Regional FP&A turns them into one approved, versioned release, sees exactly what a late correction would change before approving it, and produces the Comex deck from that release. Every figure on every slide traces back to a file, a sheet and a row.

## The problem it addresses

Most regional closes run on spreadsheets and email. That works until a country resubmits after sign-off. Then three questions get hard to answer quickly:

- What exactly changes in the numbers leadership already saw?
- Is the change material, and who has to act on it?
- Can we still reproduce the pack that was approved last week?

This toolkit makes those three answers routine.

## What it does

| Area | What a finance user gets |
|---|---|
| **Regional close** | Ingests each market's file through a saved column mapping, validates it (missing columns, wrong period, unmapped accounts, duplicates, bad amounts), tracks every market's status, and publishes an approved release in one transaction. A resubmission shows its KPI and account-level impact, with materiality, before anyone approves it. Earlier releases stay reproducible and verifiable. |
| **Reporting** | Actual vs budget for APAC and each market, with favourable/unfavourable labels that understand cost signs, an EBITDA bridge, and drill-down to source rows. Exports a Comex PowerPoint deck and an Excel workbook from the same calculation, so the deck cannot disagree with the app. |
| **Deck packs** | Which slides a deck has, in what order and with what titles, comes from a short pack file. Comex, Board and Market review packs ship as defaults. The look comes from an ordinary PowerPoint template edited in Slide Master. |
| **Reporting currency** | Any report or deck can be shown in another currency stored with the release. Conversions are labelled, including the translation effect inside the variance. |
| **Close controls** | Follows each accrual from Finance to the AP invoice to the ledger reversal. Problems land in the right queue (Finance, AP, Accounting, close lead) with the amount at risk and the evidence. An item clears only with ledger evidence or a resolution approved by a second person. |
| **Planning** | A driver-based plan of up to 36 months (volume, price and mix, hiring, opex, working capital, capex, debt, tax) rolled through P&L, balance sheet and cash flow, with scenarios. It ties to the cent every month, with no plug. Any month can become a market's budget submission. |
| **Templates** | Ready-to-fill Excel templates for every input, with instructions generated from the configuration, and a preview of how any file will be read before it is imported. |

Everything runs locally: a command-line tool (`fct`) and a browser UI (`fct ui`) over the same library.

## See it in five minutes

Python 3.12 or later.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
bash scripts/demo.sh               # the scripted October close, printed step by step
fct ui                             # the browser UI; use Demo in the sidebar to load any step
```

The demo story, all synthetic:

1. Singapore and Australia submit October actuals and budgets. Australia is late and is flagged as overdue against its owner.
2. Release 2026-10-R1 is approved: APAC EBITDA S$221,600.
3. Australia resubmits with a new consulting-fees account. The file is held for review and R1 stays current.
4. Once the account is mapped, the tool shows the change before approval: AU EBITDA down S$12,900 (−25%), APAC down 5.8%, flagged material.
5. Release R2 is approved. R1 still verifies and exports at S$221,600.
6. The Comex deck for R2 includes a "Changes since R1" slide. A later S$1,000 correction is tracked but stays below the materiality threshold.

A 90-second walkthrough script is in [docs/demo-script.md](docs/demo-script.md).

## How the numbers are checked

- **Expected results come first.** Every scenario's figures were derived by hand before the code was written. They live in [docs/fixture-expected.md](docs/fixture-expected.md), and the tests assert them exactly (Decimal arithmetic, no floats).
- **Independent review.** Each step was reviewed by rebuilding the numbers separately and probing edge cases: loss years, cash shortfalls, wrong sign conventions, resubmissions of old files, stale approvals.
- **Self-checking outputs.** Releases carry a checksum and can be rebuilt from stored source rows. Plans check that the balance sheet balances and that cash ties in every month.
- `python -m pytest` runs the full suite.

## How it was built

Built with Claude Code, Anthropic's AI coding agent. The author set the direction and made the product and finance decisions: what the tool should do, how users customise it, which currency rules and approval rules apply, and what to accept. For each step, a lead agent wrote the design and derived the expected numbers by hand before any code existed. A separate agent wrote the code, and another independently rebuilt the numbers and tried to break the result. Every finding was fixed and re-checked before it was merged.

## Design notes

The design documents in [docs/](docs/) record each decision: sign conventions, FX translation, materiality, release atomicity, segregation of duties in close controls, and the planning rules. Start with [docs/SCOPE.md](docs/SCOPE.md).

## Limits

- Synthetic data only. It has never been connected to a real ledger or ERP.
- Roles are typed in, not logged in. The approval rules prevent honest mistakes, not deliberate misuse.
- Single user, local DuckDB store. Not a hosted, multi-user service.

## License

See [LICENSE](LICENSE).
