#!/usr/bin/env bash
# October 2026 close story (docs/fixture-expected.md E1-E16, B1-B2) against a throwaway store,
# then the step 2 reporting on release R2, then the step 4 close-controls story (C1-C9, and C10 on a
# second store), then the step 5 plan (docs/fixture-expected.md "Step 5") and the 36-month plan with its
# March 2027 budget submission ("Planning part two"). Decks and exports are kept in a printed output folder.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
OUT="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -r "$ROOT/config" "$ROOT/fx" "$WORK/"
EX="$ROOT/examples/2026-10"
P=2026-10

if command -v fct >/dev/null 2>&1; then
    FCT=(command fct)
else
    FCT=("${PYTHON:-python}" -m finance_close_toolkit.cli)
fi

run() { "${FCT[@]}" "$@" --store "$WORK/store.duckdb" --config "$WORK/config/close.toml"; }

step() { printf '\n==== %s\n$ fct %s\n\n' "$1" "$2"; }

# Runs a command that is expected to be refused (exit 1) and says so.
refused() {
    set +e
    run "$@"
    local code=$?
    set -e
    if [ "$code" -ne 1 ]; then echo "expected exit code 1, got $code" >&2; exit 1; fi
    echo "(exit code 1, as expected)"
}

step "E1  Ingest SG actual v1, SG budget, AU budget" "ingest ..."
run ingest "$EX/sg_actual_v1.csv" --market SG --period $P --scenario actual --received-at 2026-11-04T10:00
echo
run ingest "$EX/sg_budget.csv" --market SG --period $P --scenario budget --received-at 2026-11-01T09:00
echo
run ingest "$EX/au_budget.xlsx" --market AU --period $P --scenario budget --received-at 2026-11-01T09:00

step "E2  Status the day after actuals were due" "status --period $P --as-of 2026-11-06"
run status --period $P --as-of 2026-11-06

step "E3  Publish dry run with AU actual missing" "publish --period $P"
refused publish --period $P

step "E4  Ingest AU actual v1 (late)" "ingest au_actual_v1.xlsx ..."
run inspect "$EX/au_actual_v1.xlsx" --market AU
echo
run ingest "$EX/au_actual_v1.xlsx" --market AU --period $P --scenario actual --received-at 2026-11-07T15:00

step "E5  Ingest the same AU file again" "ingest au_actual_v1.xlsx ..."
run ingest "$EX/au_actual_v1.xlsx" --market AU --period $P --scenario actual --received-at 2026-11-07T15:00
echo
run status --period $P --as-of 2026-11-07T16:00

step "E6  Approve and publish" "publish --period $P --approve --by \"Regional FP&A lead\""
run publish --period $P --approve --by "Regional FP&A lead"
echo
run status --period $P --as-of 2026-11-08

step "E7  AU resubmits with a new account" "ingest au_actual_v2.xlsx ..."
run ingest "$EX/au_actual_v2.xlsx" --market AU --period $P --scenario actual --received-at 2026-11-12T11:00

step "E8  Status" "status --period $P"
run status --period $P --as-of 2026-11-12T12:00

step "E9  Publish dry run while AU actual needs review" "publish --period $P"
refused publish --period $P

step "E10 Map 64000 Consulting fees to OTH, then revalidate" "revalidate --period $P"
printf '"64000" = "OTH"\n' >> "$WORK/config/mappings/au.toml"
run revalidate --period $P

step "E11 Status" "status --period $P"
run status --period $P --as-of 2026-11-12T13:00

step "E12 Publish dry run" "publish --period $P"
run publish --period $P

step "E13 Approve and publish R2" "publish --period $P --approve --by \"Regional FP&A lead\""
run publish --period $P --approve --by "Regional FP&A lead"

step "E14 R1 is still reproducible" "verify-release $P-R1; export $P-R1"
run verify-release $P-R1
run export $P-R1 --out "$WORK/export-R1"
echo
cat "$WORK/export-R1/facts.csv"

step "E15 Diff R1 to R2" "diff --from $P-R1 --to $P-R2"
run diff --from $P-R1 --to $P-R2

step "E16 Trace AU revenue in R1" "trace --release $P-R1 --market AU --account REV --scenario actual"
run trace --release $P-R1 --market AU --account REV --scenario actual

step "B1  SG resubmits with marketing 1,000 higher" "ingest sg_actual_v2.csv ..."
run ingest "$EX/sg_actual_v2.csv" --market SG --period $P --scenario actual --received-at 2026-11-14T09:00

step "B2  Status: a change below threshold raises no signal" "status --period $P"
run status --period $P --as-of 2026-11-14T10:00

step "V1  Variance vs budget for R2" "variance --release $P-R2"
run variance --release $P-R2

step "V2  Variance vs budget for AU only" "variance --release $P-R2 --market AU"
run variance --release $P-R2 --market AU

step "V2b Variance for R2 in USD, at the rates stored with the release" "variance --release $P-R2 --currency USD"
run variance --release $P-R2 --currency USD

step "V3  Comex decks for R1 and R2 (R2 with commentary)" "deck --release ... --out $OUT"
run deck --release $P-R1 --out "$OUT"
run deck --release $P-R2 --out "$OUT" --commentary "$ROOT/examples/commentary/$P.toml"

step "V3b Deck packs, and the board pack for R2" "deck packs; deck --release $P-R2 --pack board --out $OUT"
run deck packs
echo
run deck --release $P-R2 --out "$OUT" --pack board --commentary "$ROOT/examples/commentary/$P.toml"

step "V3c The R2 Comex deck in USD" "deck --release $P-R2 --currency USD --out $OUT"
run deck --release $P-R2 --currency USD --out "$OUT" --commentary "$ROOT/examples/commentary/$P.toml"

step "V4  Export R2 with release.xlsx, adding USD columns" "export $P-R2 --currency USD --out $OUT/export-R2"
run export $P-R2 --currency USD --out "$OUT/export-R2"

CX="$ROOT/examples/controls"
ANALYST="SG finance analyst"
LEAD="Regional close lead"

step "T1  Write the input templates" "templates write --out $OUT/templates"
"${FCT[@]}" templates write --out "$OUT/templates" --config "$WORK/config/close.toml"

step "C0  Preview the March accruals before importing" "controls inspect accruals_2026-03.csv --kind accruals"
run controls inspect "$CX/accruals_2026-03.csv" --kind accruals

step "C1  Import March accruals, April invoices and the April GL" "controls import ..."
run controls import --accruals "$CX/accruals_2026-03.csv" --invoices "$CX/invoices_2026-04.csv" \
    --gl "$CX/gl_2026-04.csv" --as-of 2026-05-04T09:00
cp "$WORK/store.duckdb" "$WORK/c1.duckdb"
echo
run controls status --as-of 2026-05-04T09:30
echo
run controls show A-001 --as-of 2026-05-04T09:30

step "C2  AP links INV-5504 to A-004" "controls link INV-5504 A-004 --by \"AP team\""
run controls link INV-5504 A-004 --by "AP team" --as-of 2026-05-04T10:00

step "C3  The same link again" "controls link INV-5504 A-004 --by \"AP team\""
run controls link INV-5504 A-004 --by "AP team" --as-of 2026-05-04T10:05

step "C4  SG finance analyst tries to link" "controls link INV-5504 A-005 --by \"$ANALYST\""
refused controls link INV-5504 A-005 --by "$ANALYST" --as-of 2026-05-04T10:10

step "C5  Explain the A-001 variance" "controls explain A-001 --note ... --by \"$ANALYST\""
run controls explain A-001 --note "Scope extension approved by SG GM" --by "$ANALYST" --as-of 2026-05-04T11:00

step "C6  Assign A-006" "controls assign A-006 --owner \"$ANALYST\" --by \"$LEAD\""
run controls assign A-006 --owner "$ANALYST" --by "$LEAD" --as-of 2026-05-04T12:00

step "C7  Second April GL with the late A-002 reversal" "controls import --gl gl_2026-04_v2.csv"
run controls import --gl "$CX/gl_2026-04_v2.csv" --as-of 2026-05-05T09:00

step "C8  Resolve A-003, approved by the preparer" "controls resolve A-003 ... --approved-by \"$ANALYST\""
refused controls resolve A-003 --kind re_accrue --amount 35000 --reason "Phase 2 invoice due May" \
    --by "$ANALYST" --approved-by "$ANALYST" --as-of 2026-05-05T10:00

step "C9  Resolve A-003, approved by the close lead" "controls resolve A-003 ... --approved-by \"$LEAD\""
run controls resolve A-003 --kind re_accrue --amount 35000 --reason "Phase 2 invoice due May" \
    --by "$ANALYST" --approved-by "$LEAD" --as-of 2026-05-05T10:05
echo
run controls status --as-of 2026-05-05T11:00
echo
run controls show A-002 --as-of 2026-05-05T11:00

c1() { "${FCT[@]}" "$@" --store "$WORK/c1.duckdb" --config "$WORK/config/close.toml"; }

step "C10 From C1: Accounting writes back A-002 instead" "controls resolve A-002 --kind write_back ..."
c1 controls resolve A-002 --kind write_back --amount 40000 \
    --reason "Reversal posted manually as JE-0499, extract pending" --by "Accounting team" --approved-by "$LEAD" \
    --as-of 2026-05-04T13:00
echo
c1 controls show A-002 --as-of 2026-05-04T13:00

PL="$ROOT/plans"
plan() { "${FCT[@]}" plan "$@"; }

step "P1  Check both plan files" "plan check ..."
plan check "$PL/agency_2027.toml"
echo
plan check "$PL/mix_check.toml"

step "P2  Run the base plan" "plan run plans/agency_2027.toml"
plan run "$PL/agency_2027.toml"

step "P3  Slow collections and the delayed hire against base" "plan compare ... --scenario ..."
plan compare "$PL/agency_2027.toml" --scenario slow_collections
echo
plan compare "$PL/agency_2027.toml" --scenario delayed_hire

step "P4  Mix check: P&L only, mix shifted to 60/40" "plan run plans/mix_check.toml --scenario mix_shift"
plan run "$PL/mix_check.toml" --scenario mix_shift

step "P5  Export the slow collections plan" "plan export ... --out $OUT"
plan export "$PL/agency_2027.toml" --scenario slow_collections --out "$OUT"

step "P6  The 36-month plan: Year 1 by month, then FY2027 to FY2029" "plan run plans/agency_2027_36m.toml"
plan run "$PL/agency_2027_36m.toml"

step "P7  March 2027 of the plan as the SG budget submission" "plan to-budget ... --period 2027-03 --out $OUT"
plan to-budget "$PL/agency_2027_36m.toml" --period 2027-03 --out "$OUT/budget" --config "$WORK/config/close.toml"

step "P8  Ingest it as SG budget 2027-03 through the regional template mapping" "ingest budget_SG_2027-03_... --scenario budget"
sed -i 's#mapping = "mappings/sg.toml"#mapping = "mappings/_regional_template.toml"#' "$WORK/config/close.toml"
run ingest "$OUT"/budget/budget_SG_2027-03_*.xlsx --market SG --period 2027-03 --scenario budget \
    --received-at 2027-02-20T09:00

step "P9  A plan without [budget] is refused" "plan to-budget plans/agency_2027.toml --period 2027-03"
set +e
plan to-budget "$PL/agency_2027.toml" --period 2027-03 --out "$OUT/budget" --config "$WORK/config/close.toml"
code=$?
set -e
if [ "$code" -ne 2 ]; then echo "expected exit code 2, got $code" >&2; exit 1; fi
echo "(exit code 2, as expected)"

printf '\nDemo finished. Store and config copies were in %s (now removed).\n' "$WORK"
printf 'Decks, the R2 export, the input templates, the plan workbook and the budget submission are in %s\n' "$OUT"
