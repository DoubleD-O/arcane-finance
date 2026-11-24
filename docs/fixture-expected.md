# Fixture: expected results (October 2026)

Hand-derived from the files that `examples/make_fixtures.py` writes, the mappings in `config/mappings/`, and `fx/2026-10.toml`. This is reference evidence. Change a number here only with a documented business-rule change.

All reporting figures are SGD in profit sign (revenue +, costs −). KPIs: Revenue = REV. Gross margin (GM) = REV + COGS. EBITDA = REV + COGS + PAY + RENT + MKT + OTH (DEP is below EBITDA).

## Per-file KPI totals

| File | Local ccy | Revenue | GM | EBITDA | Rate | Revenue SGD | GM SGD | EBITDA SGD |
|---|---|---|---|---|---|---|---|---|
| sg_actual_v1.csv | SGD | 1,000,000 | 550,000 | 170,000 | 1 | 1,000,000 | 550,000 | 170,000 |
| sg_actual_v2.csv (MKT 40,000 → 41,000) | SGD | 1,000,000 | 550,000 | 169,000 | 1 | 1,000,000 | 550,000 | 169,000 |
| sg_budget.csv | SGD | 950,000 | 520,000 | 125,000 | 1 | 950,000 | 520,000 | 125,000 |
| au_actual_v1.xlsx | AUD | 500,000 | 240,000 | 60,000 | 0.86 | 430,000 | 206,400 | 51,600 |
| au_actual_v2.xlsx (+64000 Consulting 15,000) | AUD | 500,000 | 240,000 | 45,000 | 0.86 | 430,000 | 206,400 | 38,700 |
| au_budget.xlsx | AUD | 520,000 | 255,000 | 75,000 | 0.88 | 457,600 | 224,400 | 66,000 |

Depreciation (not in any KPI): SG 20,000 SGD, AU 8,000 AUD in both actual and budget.

## Scenario: late market, sign-off, resubmission with a new account

Actuals for 2026-10 are due 2026-11-05 (`due_day = 5`).

| # | Action | Expected |
|---|---|---|
| E1 | Ingest `sg_actual_v1.csv` (received 2026-11-04T10:00), `sg_budget.csv` and `au_budget.xlsx` (both 2026-11-01T09:00) | All three validated, no exceptions. Tie-out: unmapped total 0 for each |
| E2 | `status --as-of 2026-11-06` | AU actual = missing. One signal: MISSING_SUBMISSION, AU actual, owner "AU country controller", due 2026-11-05, overdue |
| E3 | `publish` (dry run) | Refused: AU actual missing. No release exists |
| E4 | Ingest `au_actual_v1.xlsx` (received 2026-11-07T15:00) | Validated |
| E5 | Ingest `au_actual_v1.xlsx` again | No-op, reported as a repeat. Still one submission and one validation for that file. Status unchanged, no duplicate signals |
| E6 | `publish --approve --by "Regional FP&A lead"` | Release **2026-10-R1**. Actual APAC: Revenue 1,430,000; GM 756,400; EBITDA 221,600. Budget APAC: Revenue 1,407,600; GM 744,400; EBITDA 191,000. Status: all four slots approved, no signals |
| E7 | Ingest `au_actual_v2.xlsx` (received 2026-11-12T11:00) | needs_review. One UNMAPPED_ACCOUNT exception: source account 64000, row 9, 15,000 AUD. Tie-out on raw file amounts (AUD, as shown in the file): source 963,000 = mapped 948,000 + unmapped 15,000 |
| E8 | `status` | AU actual = needs_review. Signal VALIDATION_FAILED, owner "AU country controller", impact unknown. Current release still 2026-10-R1 |
| E9 | `publish` (dry run) | Refused: AU actual not validated. 2026-10-R1 still current |
| E10 | Add `"64000" = "OTH"` to the AU mapping, then `revalidate` | AU v2 validated under the new mapping version |
| E11 | `status` | AU actual = changed. Signal MATERIAL_CHANGE, owner "Regional FP&A lead". AU EBITDA 51,600 → 38,700 (−12,900; −25.00%). APAC EBITDA 221,600 → 208,700 (−12,900; −5.82%). Revenue and GM unchanged |
| E12 | `publish` (dry run) | Shows the same delta. Account level: AU OTH actual −8,600 → −21,500 (−12,900). Writes nothing; current release still R1 |
| E13 | `publish --approve --by "Regional FP&A lead"` | Release **2026-10-R2**, previous = R1. Actual APAC EBITDA 208,700. Manifest lists AU actual with the v2 submission and the new AU mapping version |
| E14 | `verify-release 2026-10-R1` and `export 2026-10-R1` | R1 still verifies. Its export still totals actual APAC EBITDA 221,600 and lists au_actual_v1.xlsx |
| E15 | `diff --from 2026-10-R1 --to 2026-10-R2` | Same numbers as E11 and E12 |
| E16 | `trace --release 2026-10-R1 --market AU --account REV` (actual) | One fact: au_actual_v1.xlsx, row 4, 500,000 AUD × 0.86 = 430,000 SGD |

## Below-threshold change

| # | Action | Expected |
|---|---|---|
| B1 | After E13, ingest `sg_actual_v2.csv` (received 2026-11-14T09:00) | Validated |
| B2 | `status` | SG actual = changed. SG EBITDA 170,000 → 169,000 (−1,000; −0.59%). APAC EBITDA 208,700 → 207,700 (−1,000; −0.48%). **No** MATERIAL_CHANGE signal. Account level: SG MKT −40,000 → −41,000 |

## Failure cases (each against a fresh store)

| # | Input | Expected |
|---|---|---|
| F1 | `sg_actual_wrong_period.csv` declared as 2026-10 | rejected, WRONG_PERIOD (value 2026-09) |
| F2 | `sg_actual_duplicate.csv` | needs_review, DUPLICATE_ROW on rows 7 and 10 (account 6200, Marketing, 40,000.00) |
| F3 | `au_actual_drift.xlsx` | rejected, MISSING_COLUMN "Amount AUD"; closest header "Amount (AUD)" |
| F4 | FX file without AUD in `[actual]` | publish refused for missing rate. No release written |
| F5 | Publish where the store fails after facts are written but before the pointer moves (test-injected) | Transaction rolls back. No partial release; previous current release unchanged and still verifies |
| F6 | Publish twice with no new submissions | Second call reports the existing release ID; no R(n+1) |

## Step 2: actual vs budget (release 2026-10-R2)

R2 holds SG actual v1, AU actual v2 (with 64000 mapped to OTH), and both budgets. SGD, profit sign. Variance = actual − budget; positive = Favourable. Var % = variance ÷ |budget|. Ratio rows: percentage points from unrounded ratios. Rounded half-up to 2 dp.

### APAC

| Line | Actual | Budget | Variance | Var % | Label |
|---|---|---|---|---|---|
| Revenue | 1,430,000 | 1,407,600 | 22,400 | 1.59% | Favourable |
| Direct costs | (673,600) | (663,200) | (10,400) | −1.57% | Unfavourable |
| Gross margin | 756,400 | 744,400 | 12,000 | 1.61% | Favourable |
| GM % | 52.90% | 52.88% | +0.01 pp | | Favourable |
| Staff costs | (353,200) | (361,200) | 8,000 | 2.21% | Favourable |
| Rent and facilities | (85,800) | (86,400) | 600 | 0.69% | Favourable |
| Marketing | (57,200) | (72,000) | 14,800 | 20.56% | Favourable |
| Other operating costs | (51,500) | (33,800) | (17,700) | −52.37% | Unfavourable |
| EBITDA | 208,700 | 191,000 | 17,700 | 9.27% | Favourable |
| EBITDA % | 14.59% | 13.57% | +1.03 pp | | Favourable |
| Depreciation | (26,880) | (27,040) | 160 | 0.59% | Favourable |
| Operating profit (EBIT) | 181,820 | 163,960 | 17,860 | 10.89% | Favourable |

### SG

| Line | Actual | Budget | Variance | Var % | Label |
|---|---|---|---|---|---|
| Revenue | 1,000,000 | 950,000 | 50,000 | 5.26% | Favourable |
| Direct costs | (450,000) | (430,000) | (20,000) | −4.65% | Unfavourable |
| Gross margin | 550,000 | 520,000 | 30,000 | 5.77% | Favourable |
| GM % | 55.00% | 54.74% | +0.26 pp | | Favourable |
| Staff costs | (250,000) | (260,000) | 10,000 | 3.85% | Favourable |
| Rent and facilities | (60,000) | (60,000) | 0 | 0.00% | On plan |
| Marketing | (40,000) | (50,000) | 10,000 | 20.00% | Favourable |
| Other operating costs | (30,000) | (25,000) | (5,000) | −20.00% | Unfavourable |
| EBITDA | 170,000 | 125,000 | 45,000 | 36.00% | Favourable |
| EBITDA % | 17.00% | 13.16% | +3.84 pp | | Favourable |
| Depreciation | (20,000) | (20,000) | 0 | 0.00% | On plan |
| Operating profit (EBIT) | 150,000 | 105,000 | 45,000 | 42.86% | Favourable |

### AU (translated: actual at 0.86, budget at 0.88 SGD per AUD)

| Line | Actual | Budget | Variance | Var % | Label |
|---|---|---|---|---|---|
| Revenue | 430,000 | 457,600 | (27,600) | −6.03% | Unfavourable |
| Direct costs | (223,600) | (233,200) | 9,600 | 4.12% | Favourable |
| Gross margin | 206,400 | 224,400 | (18,000) | −8.02% | Unfavourable |
| GM % | 48.00% | 49.04% | −1.04 pp | | Unfavourable |
| Staff costs | (103,200) | (101,200) | (2,000) | −1.98% | Unfavourable |
| Rent and facilities | (25,800) | (26,400) | 600 | 2.27% | Favourable |
| Marketing | (17,200) | (22,000) | 4,800 | 21.82% | Favourable |
| Other operating costs | (21,500) | (8,800) | (12,700) | −144.32% | Unfavourable |
| EBITDA | 38,700 | 66,000 | (27,300) | −41.36% | Unfavourable |
| EBITDA % | 9.00% | 14.42% | −5.42 pp | | Unfavourable |
| Depreciation | (6,880) | (7,040) | 160 | 2.27% | Favourable |
| Operating profit (EBIT) | 31,820 | 58,960 | (27,140) | −46.03% | Unfavourable |

### EBITDA bridge

| Release | Budget | SG | AU | Actual |
|---|---|---|---|---|
| 2026-10-R2 | 191,000 | +45,000 | (27,300) | 208,700 |
| 2026-10-R1 | 191,000 | +45,000 | (14,400) | 221,600 |

R1 APAC EBITDA vs budget: +30,600 (16.02%), Favourable.

### Deck checks

| # | Check | Expected |
|---|---|---|
| D1 | R2 deck slide 2, EBITDA row | Actual 208,700; Budget 191,000; Variance 17,700; Var % 9.3%; Favourable. EBITDA % row: +1.0 pp |
| D2 | R2 deck slide 6 (changes since R1) | AU EBITDA (12,900), Material; APAC EBITDA (12,900), Material |
| D3 | R1 deck | No "changes since" slide. Bridge AU (14,400), actual 221,600 |
| D4 | R2 deck without commentary | Slide 7 says outlook and actions not provided; footer ends with DRAFT |
| D5 | Proposed deck after B1 (SG v2 validated, not approved) | Marked DRAFT (proposed). APAC EBITDA 207,700 |

## Step 4: close controls (SG, SGD, debit-positive)

Inputs: `examples/controls/accruals_2026-03.csv`, `invoices_2026-04.csv`, `gl_2026-04.csv` (and `gl_2026-04_v2.csv`, which adds the late A-002 reversal). Roster and tolerance from `[controls]` in `config/close.toml`: tolerance = max(1,000, 5% of accrual).

### After importing all three files (C1)

| Accrual | Accrued | Linked invoices | Invoiced | Invoice state | Reversal | Overall | Signal | Owner | At risk |
|---|---|---|---|---|---|---|---|---|---|
| A-001 | 100,000 | INV-5501 (by reference) | 110,000 | invoiced_with_variance (+10,000 > 5,000) | ledger_confirmed (JE-0401) | open | INVOICE_VARIANCE | SG finance analyst | 10,000 |
| A-002 | 40,000 | INV-5502 (by reference) | 40,000 | invoiced | missing | open | MISSING_REVERSAL | Accounting team | 40,000 |
| A-003 | 60,000 | INV-5503 (by reference) | 25,000 | partially_invoiced | ledger_confirmed (JE-0402) | open | PARTIAL_INVOICE | SG finance analyst | 35,000 |
| A-004 | 12,000 | none | 0 | awaiting_invoice | ledger_confirmed (JE-0403) | open | (see INV-5504) | | |
| A-005 | 12,000 | none | 0 | awaiting_invoice | ledger_confirmed (JE-0404) | open | (see INV-5504) | | |
| A-006 | 8,000 | none | 0 | awaiting_invoice | ledger_confirmed (JE-0405) | open | NO_OWNER | Regional close lead | 8,000 |

Plus AMBIGUOUS_MATCH for INV-5504 (12,000; candidates A-004 and A-005), owner AP team, at risk 12,000. Total: 5 signals. Queues: SG finance analyst 2 (A-001, A-003); AP team 1; Accounting team 1; Regional close lead 1.

April P&L expense from these items (invoices minus reversals): A-001 110,000 − 100,000 = 10,000; A-002 40,000 − 0 = 40,000 (overstated by 40,000 until reversed); A-003 25,000 − 60,000 = (35,000); INV-5504 12,000; reversals A-004/A-005/A-006 (32,000). Total April = 187,000 − 192,000 = (5,000).

Journal preview, A-001: March Dr OTH 100,000 / Cr Accrued liabilities 100,000. April Dr Accrued liabilities 100,000 / Cr OTH 100,000. April Dr OTH 110,000 / Cr Accounts payable 110,000. Net April: P&L expense 10,000; accrued liabilities 0; accounts payable 110,000; cash 0.

### Actions

| # | Action | Expected |
|---|---|---|
| C2 | `link INV-5504 A-004 --by "AP team"` | A-004 invoiced (12,000) and ledger_confirmed → **cleared**. AMBIGUOUS_MATCH gone. A-005 stays awaiting_invoice with no signal. 4 signals left |
| C3 | Same link again | No-op, reported as already linked. Still 4 signals; one link event |
| C4 | `link INV-5504 A-005 --by "SG finance analyst"` | Refused: role not allowed to link (AP only). Nothing recorded |
| C5 | `explain A-001 --note "Scope extension approved by SG GM" --by "SG finance analyst"` | A-001 **cleared**. 3 signals left |
| C6 | `assign A-006 --owner "SG finance analyst" --by "Regional close lead"` | NO_OWNER gone; A-006 open, awaiting_invoice, owner SG finance analyst, no signal. 2 signals left |
| C7 | Import `gl_2026-04_v2.csv` | A-002 reversal ledger_confirmed (JE-0406) → **cleared**. MISSING_REVERSAL gone. 1 signal left |
| C8 | `resolve A-003 --kind re_accrue --amount 35000 --reason "Phase 2 invoice due May" --by "SG finance analyst" --approved-by "SG finance analyst"` | Refused: approver must differ from the person resolving |
| C9 | Same with `--approved-by "Regional close lead"` | A-003 **resolved** (re-accrue 35,000). 0 signals. History shows the event with who, approver and reason |

End state: cleared A-001, A-002, A-004; resolved A-003; open without signal A-005, A-006 (awaiting invoice). `show A-002` history: imported, reversal missing, reversal confirmed by JE-0406 in the second GL import.

### Alternative path

| # | Action | Expected |
|---|---|---|
| C10 | From C1 (no v2 GL), `resolve A-002 --kind write_back --amount 40000 --reason "Reversal posted manually as JE-0499, extract pending" --by "Accounting team" --approved-by "Regional close lead"` | A-002 resolved; MISSING_REVERSAL gone. The state still shows reversal = missing (evidence), overall = resolved |

## Step 5: planning (`plans/agency_2027.toml`, SGD, Jan–Dec 2027)

Hand-derived. Units 2,000/month: P1 1,600 × 10 and P2 400 × 20.

### Monthly P&L, base

| Line | Jan | Feb | Mar | Apr | May–Oct (each) | Nov | Dec | Year |
|---|---|---|---|---|---|---|---|---|
| Revenue | 24,000 | 24,000 | 24,000 | 24,000 | 24,000 | 24,000 | 24,000 | 288,000 |
| COGS | (10,000) | (10,000) | (10,000) | (10,000) | (10,000) | (10,000) | (10,000) | (120,000) |
| Gross margin | 14,000 | 14,000 | 14,000 | 14,000 | 14,000 | 14,000 | 14,000 | 168,000 |
| Payroll | (3,300) | (3,300) | (3,300) | (5,500) | (5,500) | (5,500) | (5,500) | (59,400) |
| Rent | (1,000) | (1,000) | (1,000) | (1,000) | (1,000) | (1,000) | (1,000) | (12,000) |
| EBITDA | 9,700 | 9,700 | 9,700 | 7,500 | 7,500 | 7,500 | 7,500 | 96,600 |
| Depreciation | (5,000) | (5,000) | (5,000) | (5,000) | (5,500) | (5,500) | (5,500) | (64,000) |
| EBIT | 4,700 | 4,700 | 4,700 | 2,500 | 2,000 | 2,000 | 2,000 | 32,600 |
| Interest | 0 | 0 | (250) | (250) | (250) | (250) | 0 | (2,250) |
| PBT | 4,700 | 4,700 | 4,450 | 2,250 | 1,750 | 1,750 | 2,000 | 30,350 |
| Tax (17%) | (799.00) | (799.00) | (756.50) | (382.50) | (297.50) | (297.50) | (340.00) | (5,159.50) |
| Net income | 3,901.00 | 3,901.00 | 3,693.50 | 1,867.50 | 1,452.50 | 1,452.50 | 1,660.00 | 25,190.50 |

Depreciation: opening assets 120,000 ÷ 24 = 5,000/month; fit-out 12,000 ÷ 24 = 500/month from May (in service), not from March (spend). Interest: 6% ÷ 12 × 50,000 = 250 in each month that starts with the loan outstanding (Mar–Nov, 9 months); Feb starts at 0.

### Cash and balances, base

Tax paid the month after each quarter: Q1 2,354.50 in April, Q2 977.50 in July, Q3 892.50 in October; Q4 935.00 is still payable at December.

| Month-end | Receipts | Payments (AP + payroll + interest + tax + capex) | Debt | Closing cash |
|---|---|---|---|---|
| Jan | 60,000 | 33,300 (opening AP 30,000 + payroll 3,300) | | 226,700.00 |
| Feb | 24,000 | 14,300 | +50,000 | 286,400.00 |
| Mar | 24,000 | 26,550 (incl. capex 12,000, interest 250) | | 283,850.00 |
| Apr | 24,000 | 19,104.50 (incl. tax 2,354.50) | | 288,745.50 |
| May | 24,000 | 16,750 | | 295,995.50 |
| Jun | 24,000 | 16,750 | | 303,245.50 |
| Jul | 24,000 | 17,727.50 (incl. tax 977.50) | | 309,518.00 |
| Aug | 24,000 | 16,750 | | 316,768.00 |
| Sep | 24,000 | 16,750 | | 324,018.00 |
| Oct | 24,000 | 17,642.50 (incl. tax 892.50) | | 330,375.50 |
| Nov | 24,000 | 16,750 | −50,000 | 287,625.50 |
| Dec | 24,000 | 16,500 | | 295,125.50 |

Lowest month-end cash: 226,700.00 (January). No cash shortfall.

Balance sheet at 2027-12-31: cash 295,125.50; AR 24,000; fixed assets cost 132,000, accumulated depreciation 64,000, net 68,000; **total assets 387,125.50**. AP 11,000; tax payable 935.00; debt 0; share capital 300,000; retained earnings 75,190.50; **total liabilities and equity 387,125.50**. Every month must balance exactly and the cash-flow closing cash must equal balance-sheet cash.

Opening balance sheet (2026-12-31): assets 380,000 = liabilities 30,000 + equity 350,000.

### Scenarios

| Scenario | Revenue (year) | EBITDA (year) | Net income (year) | Dec AR | Dec cash | Lowest cash |
|---|---|---|---|---|---|---|
| base | 288,000 | 96,600 | 25,190.50 | 24,000 | 295,125.50 | 226,700.00 (Jan) |
| slow_collections (50% at +1, 50% at +2) | 288,000 | 96,600 | 25,190.50 | 36,000 | 283,125.50 | 226,700.00 (Jan) |
| delayed_hire (H2 from June) | 288,000 | 101,000 | 28,842.50 | 24,000 | 298,777.50 | 226,700.00 (Jan) |

slow_collections moves only AR and cash (Feb onward cash is 12,000 lower than base). delayed_hire: payroll 4,400 lower (April, May), tax 748 higher (in Q2, paid July), so cash +3,652.

### Mix check (`plans/mix_check.toml`)

No opening balance sheet: the engine reports P&L-only mode and produces no balance sheet or cash flow. Monthly revenue 12,000 (800 × 10 + 200 × 20) in base and 14,000 (600 × 10 + 400 × 20) in `mix_shift`.

## Planning part two (`plans/agency_2027_36m.toml`, SGD, Jan-2027 to Dec-2029)

Same as `agency_2027.toml` for 2027 (every 2027 figure in "Step 5" must be identical), then annual units 30,000 in 2028 (2,500/month: P1 2,000, P2 500) and 36,000 in 2029 (3,000/month: P1 2,400, P2 600), flat seasonality. Opening assets are fully depreciated after Dec-2028 (24 months); the fit-out's last depreciation month is Apr-2029. No debt after 2027. Tax 17% of fiscal-year-to-date profit (fiscal year = calendar year here).

| Line | FY2027 | FY2028 | FY2029 |
|---|---|---|---|
| Revenue | 288,000 | 360,000 | 432,000 |
| COGS | (120,000) | (150,000) | (180,000) |
| Gross margin | 168,000 | 210,000 | 252,000 |
| Payroll | (59,400) | (66,000) | (66,000) |
| Rent | (12,000) | (12,000) | (12,000) |
| EBITDA | 96,600 | 132,000 | 174,000 |
| Depreciation | (64,000) | (66,000) | (2,000) |
| EBIT | 32,600 | 66,000 | 172,000 |
| Interest | (2,250) | 0 | 0 |
| PBT | 30,350 | 66,000 | 172,000 |
| Tax | (5,159.50) | (11,220.00) | (29,240.00) |
| Net income | 25,190.50 | 54,780.00 | 142,760.00 |

Monthly checks: 2028 tax is 935.00 every month (PBT 5,500); 2029 tax is 2,380.00 in Jan–Apr (PBT 14,000) and 2,465.00 in May–Dec (PBT 14,500). Tax paid in 2028: 935.00 (Jan, Q4 2027) + 2,805.00 in each of Apr, Jul, Oct = 9,350.00. Tax paid in 2029: 2,805.00 (Jan) + 7,140.00 (Apr) + 7,310.00 (Jul) + 7,395.00 (Oct) = 24,650.00.

| Balance sheet | Dec-2027 | Dec-2028 | Dec-2029 |
|---|---|---|---|
| Cash | 295,125.50 | 414,275.50 | 560,125.50 |
| Accounts receivable | 24,000 | 30,000 | 36,000 |
| Fixed assets, net | 68,000 | 2,000 | 0 |
| **Total assets** | **387,125.50** | **446,275.50** | **596,125.50** |
| Accounts payable | 11,000 | 13,500 | 16,000 |
| Tax payable | 935.00 | 2,805.00 | 7,395.00 |
| Share capital | 300,000 | 300,000 | 300,000 |
| Retained earnings brought forward | 50,000 | 75,190.50 | 129,970.50 |
| Profit for the year | 25,190.50 | 54,780.00 | 142,760.00 |
| **Total liabilities and equity** | **387,125.50** | **446,275.50** | **596,125.50** |

Year-end rollover: at Jan-2028, retained earnings brought forward becomes 75,190.50 and profit for the year restarts; at Jan-2029, 129,970.50. Lowest month-end cash: 226,700.00 (Jan-2027) overall.

### Plan as budget

`fct plan to-budget plans/agency_2027_36m.toml --period 2027-03` writes an SG budget submission (regional template, profit sign as the template states) with: REV 24,000; COGS (10,000); PAY (3,300); RENT (1,000); DEP (5,000). Interest (250) and tax are left out and the output says so. Ingested as SG budget 2027-03 it validates with no exceptions; its KPIs are Revenue 24,000, Gross margin 14,000, EBITDA 9,700. For `--period 2029-01`: REV 36,000; COGS (15,000); PAY (5,500); RENT (1,000); DEP (500).

`plans/agency_2027.toml` has no `[budget]` section: `to-budget` refuses and says what to add.

## Reporting currency (release 2026-10-R2 shown in USD)

`fx/2026-10.toml` quotes USD at actual 1.30 and budget 1.32 SGD per USD. Each scenario converts at its own rate; ratios recomputed; rounding on display only.

| APAC line | Actual USD | Budget USD | Variance | Var % |
|---|---|---|---|---|
| Revenue | 1,100,000.00 | 1,066,363.64 | 33,636.36 | 3.15% |
| EBITDA | 160,538.46 | 144,696.97 | 15,841.49 | 10.95% |

(1,430,000 ÷ 1.30; 1,407,600 ÷ 1.32; 208,700 ÷ 1.30; 191,000 ÷ 1.32.) Release 2026-10-R2's ID and facts checksum are unchanged by storing the USD rate. Asking for EUR is refused, listing SGD, AUD and USD.
