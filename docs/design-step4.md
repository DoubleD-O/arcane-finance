# Step 4 design: close controls (accrual → invoice → reversal)

Scope: `docs/SCOPE.md` job 3. Finance raises an accrual, AP receives the invoice, Accounting confirms the reversal in the ledger. All three see the same evidence, amount at risk, status and next owner. An item closes only with ledger evidence or an approved resolution. Expected numbers: `docs/fixture-expected.md`, section "Step 4".

## Accounting rules (defaults; the owner can change them)

| Topic | Rule |
|---|---|
| Reversal policy | An accrual raised in period P auto-reverses in P+1. Ledger evidence of the reversal is the GL lines in P+1 with `reference` = accrual ID and the accrual's account and market. They must total exactly −accrual amount. Any other total (an extra or duplicate line, a wrong amount) is a **reversal mismatch**, and every such line counts in the P&L table, so it agrees with the ledger. |
| Sign | Accrual, invoice and GL amounts are **debit-positive** in local currency (an expense accrual is +; its reversal is −). This step does not mix them with the profit-sign release facts. |
| Link checks | An invoice and an accrual must have the same market, supplier and account, and `invoice.period > accrual.period`. An invoice in the accrual's own period or earlier is a double count or a pre-accrual invoice. The checks are one table (`LINK_CHECKS`) used by reference matching, candidate matching and the override warnings. |
| Matching | An invoice links to an accrual (1) by `reference` equal to the accrual's PO reference or ID, if the link checks pass. If the reference names exactly one accrual but a check fails, the invoice is **not** linked and raises REFERENCE_MISMATCH for AP. Else (2) by candidates: the link checks pass, the accrual is still open and the invoice amount is within the tolerance below. Exactly one candidate → auto-linked. More than one → AMBIGUOUS_MATCH for AP. None → unmatched (not an exception in this step). Several invoices can link to one accrual (partial billing). |
| Tolerance | Invoiced total vs accrual is "in line" when \|difference\| ≤ max(1,000, 5% of the accrual) in local currency (config). |
| Missing reversal | Decided by evidence, not by the calendar: once the GL extract for P+1 is imported and has no reversal line, the reversal is **missing**. Before that it is **scheduled**. |
| Owners | `config/close.toml` gains `[controls]`: an `owners` roster (active roles), the AP and Accounting roles, and the regional close lead. An accrual whose owner is blank or not an active role has **no owner**. |
| Approvers | `approver_roles` (default `["Regional close lead", "Accounting team"]`) are the only roles that may approve a resolution, an override link or a large explanation. The approver must differ from the person acting and, for a resolution or explanation, from the accrual's owner. Roles match exactly. |
| Large explanations | An explanation of a variance whose size is over `explain_approval_abs` (default 25,000, in the accrual's local currency, like every amount in this step) needs an approver. At or below it, the owner's explanation is enough. |
| Replacing imports | A newer import of the same kind and period replaces the older one. The import reports what changed: accruals dropped, added, and changed in amount, account, supplier or owner; invoices dropped, added, and changed in amount, account, supplier or reference; GL lines dropped and added. It is refused (nothing stored) when it drops an accrual or invoice that has open issues, links or events, unless `--allow-drop --reason --by` is given, which records a `drop` event per such row. |
| Roles are assertions | `--by`, `--approved-by` and the UI's "Acting as" are what the user asserts, not authenticated identities. Nothing checks who is typing until a login exists. Segregation of duties here stops honest mistakes and makes every approval visible in the history; it does not stop someone who lies about their role. |

## Data shapes

Inputs are fixed-column CSV templates for now (a saved-mapping layer like step 1 can come later). Imports are idempotent by content hash. A newer import of the same kind and period replaces the older one for status; older imports stay stored.

- **Accrual** (`accrual_id`, `market`, `period`, `account`, `supplier`, `reference`, `amount`, `owner`, `description`).
- **Invoice** (`invoice_id`, `market`, `supplier`, `invoice_date`, `period`, `account`, `reference`, `amount`).
- **GlLine** (`journal_id`, `period`, `market`, `account`, `amount`, `reference`, `description`).
- **Event** (append-only audit history): `link` (invoice → accrual, by AP; an override also stores reason, approver and what differs), `explain` (variance note by owner, with the accrued and invoiced totals it explains, and an approver when required), `assign_owner` (by regional close lead), `resolve` (one issue, kind, amount, reason, by, approved_by, and the accrued, invoiced and reversal evidence it was recorded against), `drop` (an import allowed to drop a row with evidence: the row, the files, the reason, who). Each event has who, when and the evidence it cites. Events are never edited or deleted.
- **AccrualState**, derived on every read from accruals, invoices, GL lines and events (a state machine, not stored flags):
  - Invoice side: `awaiting_invoice` → `partially_invoiced` → `invoiced` (within tolerance) or `invoiced_with_variance`.
  - Reversal side: `scheduled` → `ledger_confirmed`, `missing` or `mismatch`.
  - Overall: `cleared` when invoiced (in line, or variance explained) **and** ledger-confirmed; `resolved` when approved resolutions cover every open issue; otherwise `open`.
  - Binding: an explanation covers only while accrued and invoiced are what they were when it was recorded; a resolution only while accrued, invoiced and the reversal evidence are. If any differ (a replacing import, a new invoice, a late reversal), it no longer covers, its signal returns, and the history says so ("Explanation no longer applies: accrued changed 100,000.00 → 10,000.00").
- **ControlSignal** (same shape as step 1's signals): type, item, owner role, amount at risk, evidence (file + row or event), next action.

| Signal | When | Owner | Amount at risk | Next action |
|---|---|---|---|---|
| INVOICE_VARIANCE | invoiced total outside tolerance, not explained | accrual owner | invoiced − accrued | Explain the difference |
| PARTIAL_INVOICE | invoiced less than accrued and reversal confirmed, not resolved | accrual owner | accrued − invoiced | Re-accrue the remainder or confirm no further invoice |
| MISSING_REVERSAL | reversal missing, not resolved | Accounting | accrual amount | Post the reversal or record a resolution |
| REVERSAL_MISMATCH | reversal lines do not total exactly −accrual | Accounting | \|total reversed − accrued\| | Correct the reversal in the ledger and import the corrected GL extract |
| AMBIGUOUS_MATCH | invoice has more than one candidate accrual | AP | invoice amount | Link the invoice to one accrual |
| REFERENCE_MISMATCH | invoice's reference names an accrual that fails the link checks (the trigger names which) | AP | invoice amount | Confirm the invoice belongs to this accrual or correct the reference |
| NO_OWNER | accrual has no active owner | regional close lead | accrual amount | Assign an owner |

### Resolution rules (`ISSUE_RULES`)

Each resolution resolves **one** issue, named with `--issue`, or inferred when the accrual has exactly one open issue (with more, it is refused and the open issues are listed). The amount must equal that issue's amount at risk exactly (Decimal); otherwise it is refused showing the expected amount. The approver follows the Approvers rule above.

| Issue | Signal | Allowed kinds | Pending entry, not yet in the ledger |
|---|---|---|---|
| missing_reversal | MISSING_REVERSAL | write_back, not_expected | write_back: −amount in P+1 (Dr Accrued liabilities / Cr expense) |
| partial_invoice | PARTIAL_INVOICE | re_accrue, not_expected | re_accrue: +amount in P+1 (Dr expense / Cr Accrued liabilities) |
| invoice_variance | INVOICE_VARIANCE | none: use `explain` | |
| no_owner | NO_OWNER | none: use `assign` | |
| reversal_mismatch | REVERSAL_MISMATCH | none: correct the ledger and re-import | |

`not_expected` implies no entry. A pending entry shows in `show`, in the UI detail and as separate "Pending (not in ledger)" and "Net after pending" columns plus a "Pending entries, not yet in the ledger" list under the P&L table, so a resolution never makes the ledger figures look finished.

## Journal preview (per accrual, shown in `show` and the UI)

Accrual (P): Dr expense / Cr Accrued liabilities. Reversal (P+1): Dr Accrued liabilities / Cr expense, one entry per reversal line. Each invoice: Dr expense / Cr Accounts payable. Then the net effect in P+1 on P&L expense, accrued liabilities, accounts payable, and cash (zero until paid). Numbers only from the derived state. Pending entries from resolutions are listed after it, labelled "not yet in the ledger", and are not in the net effect.

## Commands (`fct controls …`) and UI

| Command | Does |
|---|---|
| `import --accruals F --invoices F --gl F` (any subset) `[--allow-drop --reason TEXT --by ROLE]` | Validates columns, types and periods; stores rows with file and row number; idempotent; reports what a replacing file changed; refuses a drop of evidence unless allowed |
| `status [--as-of]` | Queues by role (Finance owners, AP, Accounting, regional close lead): each signal with amount at risk, evidence, next action; then counts by state |
| `show ACCRUAL_ID` | Evidence (accrual row, linked invoices, reversal line), state, journal preview, event history |
| `link INVOICE_ID ACCRUAL_ID --by ROLE` | AP links an invoice; refused unless the accrual is a candidate or the user passes `--override --reason --approved-by` |
| `explain ACCRUAL_ID --note TEXT --by ROLE [--approved-by ROLE]` | Owner explains a variance; an approver is required over `explain_approval_abs` |
| `assign ACCRUAL_ID --owner ROLE --by ROLE` | Regional close lead assigns an owner |
| `resolve ACCRUAL_ID [--issue I] --kind K --amount A --reason TEXT --by ROLE --approved-by ROLE` | Records an approved resolution of one issue |

Every action checks the role is allowed for that action (from `[controls]`), refuses otherwise with a readable message, and is idempotent (repeating the same action is a no-op that says so).

The UI gains a **Close controls** page: a tab per queue (Finance, AP, Accounting, Regional close lead) with the signal cards and action forms, an accrual detail view (evidence, journal preview, pending entries, history), and the import uploader. Its heading names the imported periods ("Close controls: accruals 2026-03, invoices 2026-04, GL 2026-04"), not the sidebar period. An As-of input works like the CLI's `--as-of`, for viewing and for recording. Link and approver selectors start blank; approver selectors offer only the allowed approvers. An override lists candidates first and warns on what differs. The explain and resolve inputs are forms, so typing does not rerun the page. No business logic in the UI.

## Not in this step

Payments and cash timing, provisions, capex and earned-but-unbilled schedules, FX on accruals (fixture is SGD only), and feeding accrual totals into the regional release.
