import argparse
import codecs
import getpass
import os
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from . import (attention, controls, deck, deck_pack, identity, plan_budget, plan_xlsx, planning, release, validation,
               variance, workflow, xlsx)
from .commentary import load_commentary
from .config import ConfigError, Workspace, load_workspace
from .identity import IdentityError
from .intake import IntakeError, inspect
from .kpi import APAC, KPIS, Diff, totals
from .plan_config import PlanError, load_plan, part_years_text
from .money import fmt_amount, fmt_money, fmt_pct, fmt_percent, fmt_pp, fmt_step, parse_amount, plain
from .release import ReleaseError
from .store import DEFAULT_STORE, Store
from .templates import write_templates


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int] = (), indent: str = "") -> str:
    widths = [max(len(str(c)) for c in col) for col in zip(headers, *rows)]

    def line(cells: Sequence[str]) -> str:
        parts = [str(c).rjust(w) if i in right else str(c).ljust(w) for i, (c, w) in enumerate(zip(cells, widths))]
        return (indent + "  ".join(parts)).rstrip()

    return "\n".join([line(headers), *(line(r) for r in rows)])


def _when(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M")


def diff_text(d: Diff, ws: Workspace, indent: str = "", accounts: bool = True) -> str:
    ccy = ws.config.reporting_currency
    if not d.kpis:
        return f"{indent}No changes."
    kpi_rows = [
        [k.scenario, k.scope, k.kpi.label, fmt_amount(k.before), fmt_amount(k.after), fmt_amount(k.change),
         fmt_pct(k.pct), "material" if k.material else "below threshold"]
        for k in d.kpis
    ]
    out = [
        f"{indent}KPI changes ({ccy})",
        table(["Scenario", "Scope", "KPI", "Before", "After", "Change", "Change %", "Materiality"],
              kpi_rows, right=(3, 4, 5, 6), indent=indent),
    ]
    if not accounts:
        return "\n".join(out)
    out += [
        "",
        f"{indent}Account changes ({ccy})",
        table(["Scenario", "Market", "Account", "Name", "Before", "After", "Change"],
              [[a.scenario, a.market, a.account, ws.coa.accounts[a.account].name, fmt_amount(a.before),
                fmt_amount(a.after), fmt_amount(a.change)] for a in d.accounts],
              right=(4, 5, 6), indent=indent),
    ]
    return "\n".join(out)


def totals_text(facts: Sequence[release.ReleaseFact], ws: Workspace) -> str:
    rows = []
    for scenario, by_scope in totals(facts, ws.coa).items():
        order = [m.code for m in ws.config.markets if m.code in by_scope] + [APAC]
        for scope in order:
            rows.append([scenario, scope, *(fmt_amount(by_scope[scope][k.key]) for k in KPIS)])
    return table(["Scenario", "Scope", *(k.label for k in KPIS)], rows, right=(2, 3, 4))


def cmd_inspect(args, ws: Workspace, store: Store) -> int:
    report = inspect(Path(args.file), ws.mapping(args.market))
    t = report.table
    print(f"File: {report.file_name} ({report.mapping.layout.format})")
    if t.sheets:
        print(f"Sheets: {', '.join(t.sheets)}; using {t.sheet}")
    print(f"Header row {report.header_row}: {' | '.join(h for h in t.headers if h) or '(empty)'}")
    print()
    print(table(["Configured column", "Found", "Closest header"],
                [[c.name, "yes" if c.found else "MISSING", c.closest or ""] for c in report.columns]))
    if t.rows:
        print()
        print(table(["Row", "Source account", "Description", "Amount", "Maps to"],
                    [[str(r.row_number), r.source_account, r.description,
                      fmt_amount(r.amount) if r.amount is not None else f"BAD: {r.amount_text}",
                      report.regional_account(r) or "UNMAPPED"] for r in t.rows],
                    right=(0, 3)))
    return 0


def _print_validation(v: validation.Validation, currency: str) -> None:
    n = len(v.exceptions)
    print(f"Validation: {v.state} under mapping {v.mapping_version} ({n} exception{'s' * (n != 1)})")
    for e in v.exceptions:
        print(f"  {e.code} row {e.row}: {e.detail}")
    t = v.tie_out
    print(f"Tie-out ({currency}, as in file): source {fmt_amount(t.source_total)}"
          f" = mapped {fmt_amount(t.mapped_total)} + unmapped {fmt_amount(t.unmapped_total)}")


# Needs attention: the workflow on both sources (docs/design-attention.md)


def attention_list_text(items: tuple[workflow.Item, ...], as_of: datetime) -> str:
    if not items:
        return f"Attention items, as of {_when(as_of)}: none."
    rows = [[i.key, i.state, i.assignee_label or "-", i.due.isoformat() + (" overdue" if i.overdue else ""),
             i.condition.owner] for i in items]
    return (f"Attention items, as of {_when(as_of)}: {len(items)} item{'s' * (len(items) != 1)}\n"
            + table(["Key", "State", "Assignee", "Due", "Owner"], rows))


def attention_show_text(i: workflow.Item) -> str:
    c = i.condition
    out = [i.key, f"{c.type}  {c.scope}  ({c.source}, {c.period})", f"Trigger:  {c.trigger}",
           f"Owner:    {c.owner}; close lead {c.lead}", f"State:    {i.summary()}",
           f"Occurrence {i.occurrence}", "", "History"]
    out.append(table(["#", "When", "Who", "What"], [[str(h.occurrence), h.when, h.who, h.what] for h in i.history]))
    return "\n".join(out)


def _attention_items(args, ws: Workspace, store: Store) -> workflow.Items:
    return workflow.load(store, ws, _as_of(args), getattr(args, "period", None))


def cmd_attention_list(args, ws: Workspace, store: Store) -> int:
    found = _attention_items(args, ws, store)
    if args.mine:
        chosen = workflow.mine(found, identity.actor(acting(args, store)))
    elif args.state is not None:
        chosen = workflow.with_state(found, args.state)
    else:
        chosen = tuple(i for i in found.items if i.is_open)
    chosen = tuple(i for i in chosen if (args.state is None or i.state == args.state)
                   and (not args.period or i.condition.period == args.period) and (not args.overdue or i.overdue))
    print(attention_list_text(chosen, found.as_of))
    return 0


def cmd_attention_show(args, ws: Workspace, store: Store) -> int:
    try:
        print(attention_show_text(_attention_items(args, ws, store).get(args.key)))
    except workflow.WorkflowError as e:
        print(f"Refused: {e}")
        return 1
    return 0


def _attention_action(args, ws: Workspace, store: Store, kind: workflow.ActionKind, **kw) -> int:
    r = workflow.act(store, ws, args.key, kind, acting(args, store), _as_of(args), **kw)
    print(("Refused: " if r.outcome == "refused" else "") + r.message)
    return 1 if r.outcome == "refused" else 0


def cmd_attention_ack(args, ws: Workspace, store: Store) -> int:
    return _attention_action(args, ws, store, "ack", note=args.note or "")


def cmd_attention_assign(args, ws: Workspace, store: Store) -> int:
    return _attention_action(args, ws, store, "assign", to=args.to)


def cmd_attention_due(args, ws: Workspace, store: Store) -> int:
    return _attention_action(args, ws, store, "due", due=date.fromisoformat(args.date))


def cmd_attention_comment(args, ws: Workspace, store: Store) -> int:
    return _attention_action(args, ws, store, "comment", note=args.note)


def cmd_attention_refresh(args, ws: Workspace, store: Store) -> int:
    r = workflow.refresh(store, ws, acting(args, store), args.period)
    print(("Refused: " if r.outcome == "refused" else "") + r.message)
    return 1 if r.outcome == "refused" else 0


# Who is acting: the role typed in open mode, the signed-in person in signed-in mode


def acting(args, store: Store, default_role: str | None = None) -> identity.Actor | str:
    """Open mode: the role given with --by (or --as), as before. Signed-in mode: the person signed in with
    `fct login` for this store, acting in --as (or --by), defaulted when they hold one role, or `default_role`
    when they hold it."""
    by, as_role = getattr(args, "by", None), getattr(args, "as_role", None)
    if by and as_role and by != as_role:
        raise IdentityError(f"--by {by!r} and --as {as_role!r} name different roles; give one.")
    role = as_role or by
    if not identity.signed_in_mode(store):
        return role or ""
    found = identity.session(store, identity.read_token(args.store))
    if found is None:
        raise IdentityError(identity.SIGN_IN_FIRST)
    if not role and default_role in found.user.roles:
        role = default_role
    return identity.act(found.user, role)


def cmd_ingest(args, ws: Workspace, store: Store) -> int:
    received = datetime.fromisoformat(args.received_at) if args.received_at else None
    r = validation.ingest(store, ws, Path(args.file), args.market, args.period, args.scenario, received,
                          by=acting(args, store))
    s = r.submission
    slot = f"{s.market} {s.scenario} {s.period}"
    if r.rereceived and r.is_latest:
        print(f"{s.file_name} re-received; now the latest for {slot}, replacing {r.previous.file_name}.")
    elif r.rereceived:
        print(f"{s.file_name} re-received as {slot}.")
    elif r.repeat and not r.revalidated:
        print(f"Repeat: {s.file_name} is already stored as {slot} (received {_when(s.received_at)}) "
              f"and validated under the current mapping. Nothing changed.")
    elif r.repeat:
        print(f"Repeat: {s.file_name} is already stored as {slot}; validated again under the new mapping.")
    else:
        print(f"Ingested {s.file_name} as {slot}, received {_when(s.received_at)} (submission {s.submission_id[:12]})")
    if not r.is_latest:
        print(f"This is NOT the latest for {slot}: {r.latest.file_name} (received {_when(r.latest.received_at)}) is.")
    _print_validation(r.validation, ws.config.market(s.market).currency)
    return 0


def cmd_revalidate(args, ws: Workspace, store: Store) -> int:
    results = validation.revalidate(store, ws, args.period, by=acting(args, store))
    if not results:
        print(f"No submissions for {args.period}.")
        return 0
    print(table(["Market", "Scenario", "File", "State", "Mapping", "Exceptions", "Result"],
                [[r.submission.market, r.submission.scenario, r.submission.file_name, r.validation.state,
                  r.validation.mapping_version, str(len(r.validation.exceptions)),
                  "revalidated" if r.new else "unchanged"] for r in results],
                right=(5,)))
    return 0


def workflow_line(item: workflow.Item | None) -> list[str]:
    return [f"   Workflow: {item.summary()} [{item.key}]"] if item else []


def _signal_text(i: int, s: attention.Signal, ws: Workspace, item: workflow.Item | None = None) -> str:
    head = f"{i}. {s.type}  {s.market} {s.scenario}" + ("  (overdue)" if s.overdue else "")
    lines = [
        head,
        f"   Owner:    {s.owner}",
        f"   Due:      {s.due.isoformat()}",
        f"   Trigger:  {s.trigger}",
        f"   Release:  {s.release_id or 'none'}",
    ]
    if s.exceptions:
        lines.append("   Exceptions:")
        lines += [f"     {e.code} row {e.row}: {e.detail}" for e in s.exceptions]
    if s.impact is None:
        lines.append("   Impact:   unknown")
    else:
        lines.append("   Impact:")
        lines.append(diff_text(s.impact, ws, indent="     ", accounts=False))
    hint = f" (review: fct publish --period {s.period})" if s.type == "MATERIAL_CHANGE" else ""
    lines.append(f"   Next:     {s.next_action}{hint}")
    lines += workflow_line(item)
    return "\n".join(lines)


def cmd_status(args, ws: Workspace, store: Store) -> int:
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    b = attention.board(store, ws, args.period, as_of)
    print(f"Close status {b.period}, as of {_when(b.as_of)}")
    print(f"Actuals due {b.due.isoformat()}. Current release: {b.current_release_id or 'none'}.")
    print()
    rows = []
    for v in b.slots:
        s = v.slot
        sub = s.submission
        rows.append([s.market.code, s.scenario, v.status, sub.file_name if sub else "-",
                     identity.when_text(sub.received_at, sub.logged_at) if sub else "-",
                     str(len(s.validation.exceptions)) if s.validation else "-", v.note])
    print(table(["Market", "Scenario", "Status", "File", "Received", "Exceptions", "Note"], rows, right=(5,)))
    if b.changes is not None:
        print()
        print(f"Changes vs {b.current_release_id} if the latest validated files were published")
        print(diff_text(b.changes, ws))
    print()
    if not b.signals:
        print("Needs attention: nothing.")
        return 0
    print(f"Needs attention: {len(b.signals)} signal{'s' * (len(b.signals) != 1)}")
    items = workflow.load(store, ws, b.as_of, args.period).by_key()
    for i, s in enumerate(b.signals, 1):
        print()
        print(_signal_text(i, s, ws, items.get(workflow.board_key(s.period, s.type, s.market, s.scenario))))
    return 0


def cmd_publish(args, ws: Workspace, store: Store) -> int:
    signed_in = identity.signed_in_mode(store)
    if args.approve and not args.by and not signed_in:
        raise ReleaseError("--approve needs --by ROLE")
    approver = acting(args, store, ws.config.regional_owner) if args.approve else None
    r = release.publish(store, ws, args.period, approved_by=approver, expected_checksum=args.expect)
    current = r.proposal.current
    if r.outcome == "refused":
        print(f"Publish refused for {args.period}. Nothing written.")
        for reason in r.reasons:
            print(f"  - {reason}")
        print(f"Current release: {current.release_id if current else 'none'}.")
        return 1
    if r.outcome == "unchanged":
        print(f"No change: the facts match current release {r.release_id}. Nothing written.")
        return 0
    facts = r.proposal.facts
    if r.outcome == "dry_run":
        print(f"Dry run: would publish {r.release_id} (previous {current.release_id if current else 'none'}). "
              "Nothing written.")
        print(f"Facts checksum: {r.facts_checksum}")
    else:
        print(f"Published {r.release_id} (previous {current.release_id if current else 'none'}), "
              f"approved by {identity.actor(approver).label}.")
    print()
    print(f"KPI totals ({ws.config.reporting_currency})")
    print(totals_text(facts, ws))
    print()
    if current is None:
        print(f"First release for {args.period}; nothing to compare against.")
    else:
        print(f"Changes vs {current.release_id}")
        print(diff_text(r.diff, ws))
    if r.outcome == "dry_run":
        print()
        who = "" if signed_in else ' --by "<role>"'
        print(f"To publish what you reviewed: fct publish --period {args.period} --approve{who} "
              f"--expect {r.facts_checksum}")
    return 0


def cmd_diff(args, ws: Workspace, store: Store) -> int:
    print(f"{args.from_id} -> {args.to_id}")
    print(diff_text(release.diff_releases(store, ws, args.from_id, args.to_id), ws))
    return 0


def cmd_trace(args, ws: Workspace, store: Store) -> int:
    facts = release.trace(store, args.release, args.market, args.account, args.scenario)
    name = ws.coa.accounts[args.account].name if args.account in ws.coa.accounts else args.account
    print(f"{args.release}  {args.market}  {args.account} ({name})" + (f"  {args.scenario}" if args.scenario else ""))
    if not facts:
        print("No facts.")
        return 1
    ccy = ws.config.reporting_currency
    print(table(["Scenario", "File", "Row", "Local amount", "Ccy", "Rate", f"{ccy} amount", "Submission"],
                [[f.scenario, f.file_name, str(f.source_row), fmt_amount(f.local_amount), f.currency, plain(f.rate),
                  fmt_amount(f.reporting_amount), f.submission_id[:12]] for f in facts],
                right=(2, 3, 5, 6)))
    for scenario in sorted({f.scenario for f in facts}):
        total = sum(f.reporting_amount for f in facts if f.scenario == scenario)
        print(f"Total {scenario}: {fmt_amount(total)} {ccy}")
    return 0


def cmd_verify(args, ws: Workspace, store: Store) -> int:
    r = release.verify_release(store, args.release)
    if r.ok:
        print(f"{r.release_id} verifies: facts rebuilt from stored rows, mappings and rates match checksum "
              f"{r.manifest_checksum[:12]}.")
        return 0
    print(f"{r.release_id} FAILS verification:")
    for p in r.problems:
        print(f"  - {p}")
    return 1


def cmd_export(args, ws: Workspace, store: Store) -> int:
    converted = variance.release_report(store, ws, args.release, args.currency) if args.currency else None
    facts_path, manifest_path = release.export(store, args.release, Path(args.out))
    xlsx_path = xlsx.export_xlsx(store, ws, args.release, Path(args.out), converted)
    print(f"Wrote {facts_path} ({len(store.release_facts(args.release))} facts), {manifest_path} and {xlsx_path}")
    if converted is not None and converted.conversion is not None:
        print(f"{xlsx_path.name} adds amounts in {converted.currency_label}.")
    return 0


def _report(args, ws: Workspace, store: Store) -> variance.VarianceReport:
    if args.proposed:
        if not args.period or args.release:
            raise ReleaseError("--proposed needs --period P and no --release")
        return variance.proposed_report(store, ws, args.period, args.currency)
    if not args.release or args.period:
        raise ReleaseError("give --release R, or --period P --proposed")
    return variance.release_report(store, ws, args.release, args.currency)


def _report_heading(r: variance.VarianceReport) -> str:
    if r.release_id is None:
        current = f"current release {r.previous_release_id}" if r.previous_release_id else "no current release"
        head = f"DRAFT: proposed release for {r.period}, not approved ({current})."
    else:
        head = f"Release {r.release_id} ({r.period}), final. Approved by {r.approved_by} on {_when(r.approved_at)}."
    return f"{head} Checksum {r.facts_checksum[:12]}. Amounts in {r.currency_label}."


def _variance_cells(row: variance.VarianceRow) -> list[str]:
    if row.line.kind == "ratio":
        return [row.line.label, fmt_percent(row.actual), fmt_percent(row.budget), fmt_pp(row.variance), "", row.label]
    return [row.line.label, fmt_money(row.actual), fmt_money(row.budget), fmt_money(row.variance),
            fmt_percent(row.variance_pct), row.label]


_VARIANCE_HEADER = ["Line", "Actual", "Budget", "Variance", "Var %", "Label"]


def cmd_variance(args, ws: Workspace, store: Store) -> int:
    r = _report(args, ws, store)
    scope = ws.config.market(args.market).code if args.market else APAC
    print(_report_heading(r))
    if r.translation_note:
        print(r.translation_note)
    print()
    print(f"P&L vs budget, {scope}")
    print(table(_VARIANCE_HEADER, [_variance_cells(row) for row in r.scope(scope)], right=(1, 2, 3, 4)))
    if scope == APAC:
        print()
        print("Market summary")
        rows = [[m.code, *_variance_cells(r.row(m.code, key))]
                for m in r.markets for key in variance.MARKET_SUMMARY_LINES]
        print(table(["Market", *_VARIANCE_HEADER], rows, right=(2, 3, 4, 5)))
        print()
        print("EBITDA bridge")
        b = r.bridge
        rows = [["Budget EBITDA", fmt_money(b.budget), ""],
                *([s.market, fmt_step(s.amount), r.row(s.market, "ebitda").label] for s in b.steps),
                ["Actual EBITDA", fmt_money(b.actual), ""]]
        print(table(["Step", "Amount", "Label"], rows, right=(1,)))
    notes = r.fx_notes(scope)
    if notes:
        print()
        print("\n".join(notes))
    print()
    *first, last = variance.UNAVAILABLE_DRILL_DOWNS
    print(f"{', '.join(first).capitalize()} and {last} drill-downs: not available in this data.")
    return 0


def cmd_deck_packs(ws: Workspace, config_dir: Path) -> int:
    """Every pack; a broken pack file is listed with its error, and makes the exit code 1."""
    markets = [m.code for m in ws.config.markets]
    broken = 0
    for key in deck_pack.pack_keys(config_dir):
        try:
            pack = deck_pack.find_pack(config_dir, key, markets)
        except ConfigError as e:
            print(f"{key}  BROKEN: {e}")
            broken += 1
            continue
        print(f"{key}  {pack.name}{' (default)' if key == deck_pack.DEFAULT_KEY else ''}")
        print(f'  title  "{pack.title}"')
        for n, slide in enumerate(pack.slides, 1):
            print(f"  {n}. {deck_pack.describe(slide)}")
    return 1 if broken else 0


def cmd_deck(args, ws: Workspace, store: Store) -> int:
    config_dir = Path(args.config).parent
    if args.what == "packs":
        return cmd_deck_packs(ws, config_dir)
    if not args.out:
        raise ConfigError("--out is required to write a deck")
    r = _report(args, ws, store)
    commentary = load_commentary(Path(args.commentary)) if args.commentary else None
    pack = deck_pack.find_pack(config_dir, args.pack, [m.code for m in ws.config.markets])
    template, style = deck.find_styling(config_dir, args.template and Path(args.template),
                                        args.style and Path(args.style))
    for note in template.notes if template else ():
        print(note)
    try:
        path = deck.write_deck(r, commentary, Path(args.out), force=args.force, template=template, style=style,
                               pack=pack)
    except deck.DeckExists as e:
        print(f"Refused: {e}. Nothing written.")
        return 1
    reasons = deck.draft_reasons(r, commentary, pack)
    print(f"Wrote {path} ({'DRAFT: ' + '; '.join(reasons) if reasons else 'FINAL'}).")
    return 0


# Close controls (docs/design-step4.md)


def _signal_lines(i: int, s: controls.Signal, c: controls.Controls,
                  items: dict[str, workflow.Item] | None = None) -> list[str]:
    return [
        f"{i}. {s.type}  {s.item}  {c.currency(s.market)} {fmt_money(s.amount_at_risk)} at risk",
        f"   Trigger:  {s.trigger}",
        *(f"   {'Evidence:' if j == 0 else '':9} {e}" for j, e in enumerate(s.evidence)),
        f"   Next:     {s.next_action}",
        *workflow_line((items or {}).get(workflow.controls_key(s.type, s.item))),
    ]


def controls_status_text(c: controls.Controls, items: dict[str, workflow.Item] | None = None) -> str:
    out = [f"Close controls, as of {_when(c.as_of)}", controls.imports_line(c)]
    queues = c.queues()
    n = len(c.signals)
    out += ["", f"Needs attention: {n} signal{'s' * (n != 1)}",
            "Queues: " + ", ".join(f"{role} {len(sigs)}" for role, sigs in queues.items())]
    i = 0
    for role, sigs in queues.items():
        out += ["", f"{role} ({len(sigs)})"]
        if not sigs:
            out.append("   nothing")
        for s in sigs:
            i += 1
            out += _signal_lines(i, s, c, items)
    out += ["", "Accruals"]
    out.append(table(
        ["Accrual", "Market", "Period", "Account", "Accrued", "Invoiced", "Invoice state", "Reversal", "Overall",
         "Owner"],
        [[s.accrual_id, s.accrual.market, s.accrual.period, s.accrual.account, fmt_money(s.accrual.amount),
          fmt_money(s.invoiced), s.invoice_state, s.reversal_state, s.overall, s.owner or "(none)"]
         for s in c.accruals],
        right=(4, 5)))
    counts = c.counts()
    out.append("Counts: " + ", ".join(f"{o} {counts[o]}" for o in controls.OVERALL))
    unmatched = [m.invoice.invoice_id for m in c.unmatched if len(m.candidates) < 2 and not m.referenced]
    if unmatched:
        out.append("Unmatched invoices (no candidate accrual): " + ", ".join(unmatched))
    for market, period in controls.pl_scopes(c):
        out += ["", *controls_pl_lines(c, market, period)]
    return "\n".join(out)


def controls_pl_lines(c: controls.Controls, market: str, period: str) -> list[str]:
    """The P&L table from the ledger and imports. Pending entries get their own columns and list, so a pending
    re-accrual or write-back never reads as booked."""
    lines = controls.pl_expense(c, market, period)
    total = controls.PlLine("Total", *(sum((getattr(l, f) for l in lines), controls.ZERO)
                                       for f in ("invoices", "reversals", "pending")))
    pending = controls.pl_pending(c, market, period)
    headers = ["Item", "Invoices", "Reversals", "Net"]
    cells = [[l.item, fmt_money(l.invoices), fmt_money(l.reversals), fmt_money(l.net)] for l in (*lines, total)]
    if pending:
        headers += ["Pending (not in ledger)", "Net after pending"]
        for row, l in zip(cells, (*lines, total)):
            row += [fmt_money(l.pending), fmt_money(l.after_pending)]
    out = [f"P&L expense {market} {period} from these items ({c.currency(market)})",
           table(headers, cells, right=tuple(range(1, len(headers))))]
    if pending:
        out += [f"{controls.PENDING_HEADING}:", *(f"  {controls.pending_text(e)}" for e in pending)]
    return out


def controls_show_text(c: controls.Controls, accrual_id: str, items: dict[str, workflow.Item] | None = None) -> str:
    s = c.accrual(accrual_id)
    a, ccy = s.accrual, c.currency(s.accrual.market)
    out = [
        f"{a.accrual_id}  {a.market} {a.period}  {a.account}  {a.supplier}  {ccy} {fmt_money(a.amount)}",
        f"Description: {a.description}",
        f"Owner: {s.owner or '(none)'}" + (f" (file says {a.owner!r})" if a.owner and a.owner != s.owner else ""),
        f"State: invoice {s.invoice_state}, reversal {s.reversal_state}, overall {s.overall}",
        f"Invoiced {fmt_money(s.invoiced)} vs accrued {fmt_money(a.amount)}: difference {fmt_step(s.difference)}, "
        f"tolerance {fmt_money(s.tolerance)}",
        controls.imports_line(c),
    ]
    if s.open_issues:
        out.append("Open issues: " + ", ".join(sorted(s.open_issues)))
    if s.explained:
        out.append(f"Variance explained by {s.explained.by_label}: {s.explained.data['note']}")
    out += [f"Resolution: {controls.resolution_text(r)}" for r in s.resolutions]
    out += [f"{lapse.what}: {'; '.join(lapse.changes)}" for lapse in s.lapsed]
    sigs = [g for g in c.signals if g.item == a.accrual_id]
    out += ["", "Signals" + ("" if sigs else ": none")]
    for i, g in enumerate(sigs, 1):
        out += _signal_lines(i, g, c, items)
    out += ["", "Evidence"]
    out.append(table(["Kind", "ID", "File", "Row", "Amount", "Note"],
                     [list(e) for e in controls.evidence_rows(s)], right=(3, 4)))
    preview = controls.journal_preview(s)
    out += ["", f"Journal preview ({ccy})"]
    out.append(table(["Period", "Entry", "Dr", "Cr", "Amount", "Evidence"],
                     [[e.period, e.label, e.debit, e.credit, fmt_money(e.amount), e.evidence]
                      for e in preview.entries], right=(4,)))
    out.append(controls.net_text(preview.net))
    pending = controls.pending_entries(s)
    if pending:
        out += ["", f"{controls.PENDING_HEADING} ({ccy})", *(f"  {controls.pending_text(e)}" for e in pending)]
    out += ["", "History"]
    out.append(table(["When", "What", "By", "Detail"],
                     [[h.when, h.what, h.by, h.detail] for h in controls.history(c, accrual_id)]))
    return "\n".join(out)


def _as_of(args) -> datetime | None:
    return datetime.fromisoformat(args.as_of) if args.as_of else None


def cmd_controls_import(args, ws: Workspace, store: Store) -> int:
    paths = {kind: getattr(args, kind) for kind in controls.KINDS if getattr(args, kind)}
    files = {kind: (Path(path).name, Path(path).read_bytes()) for kind, path in paths.items()}
    if not files:
        print("Import refused: give --accruals, --invoices and/or --gl. Nothing imported.")
        return 1
    stray = [kind for kind in controls.KINDS if getattr(args, f"{kind}_mapping") and kind not in files]
    if stray:
        print(f"Import refused: --{stray[0]}-mapping needs --{stray[0]}. Nothing imported.")
        return 1
    try:
        mappings = {kind: controls.load_mapping(Path(getattr(args, f"{kind}_mapping")), kind)
                    for kind in files if getattr(args, f"{kind}_mapping")}
        results = controls.import_files(store, ws, files, _as_of(args), allow_drop=args.allow_drop,
                                        reason=args.reason or "", by=acting(args, store), mappings=mappings)
    except controls.ControlsError as e:
        print(f"Import refused. Nothing imported.\n{e}")
        return 1
    for r in results:
        print(r.message)
        for change in r.changes:
            print(f"  {change}")
    return 0


def controls_inspect_text(p: controls.Preview) -> str:
    out = [*p.lines, "", table(list(p.columns[0]), [list(c.values()) for c in p.columns])]
    if p.rows:
        out += ["", p.rows_heading, table(list(p.rows[0]), [list(row.values()) for row in p.rows], right=(0,))]
    if p.warnings:
        out += ["", *p.warnings]
    return "\n".join([*out, "", p.verdict])


def cmd_controls_inspect(args, ws: Workspace, store: Store) -> int:
    path = Path(args.file)
    try:
        mapping = controls.load_mapping(Path(args.mapping), args.kind) if args.mapping else None
        reading = controls.read(args.kind, path.read_bytes(), path.name, ws, mapping)
    except controls.ControlsError as e:
        print(f"Cannot read {path.name}: {e}")
        return 1
    preview = controls.preview(reading)
    print(controls_inspect_text(preview))
    return 0 if preview.imports else 1


def cmd_templates_write(args) -> int:
    coa = load_workspace(Path(args.config), None).coa
    for path in write_templates(Path(args.out), coa, as_csv=args.csv):
        print(f"Wrote {path}")
    return 0


def cmd_controls_status(args, ws: Workspace, store: Store) -> int:
    c = controls.load(store, ws, _as_of(args))
    print(controls_status_text(c, workflow.load(store, ws, c.as_of).by_key()))
    return 0


def cmd_controls_show(args, ws: Workspace, store: Store) -> int:
    c = controls.load(store, ws, _as_of(args))
    try:
        print(controls_show_text(c, args.accrual, workflow.load(store, ws, c.as_of).by_key()))
    except controls.ControlsError as e:
        print(f"Refused: {e}")
        return 1
    return 0


def _action(result: controls.ActionResult) -> int:
    print(("Refused: " if result.outcome == "refused" else "") + result.message)
    return 1 if result.outcome == "refused" else 0


def cmd_controls_link(args, ws: Workspace, store: Store) -> int:
    return _action(controls.link(store, ws, args.invoice, args.accrual, acting(args, store), args.override,
                                 args.reason or "", args.approved_by or "", _as_of(args)))


def cmd_controls_explain(args, ws: Workspace, store: Store) -> int:
    return _action(controls.explain(store, ws, args.accrual, args.note, acting(args, store), args.approved_by or "",
                                    _as_of(args)))


def cmd_controls_assign(args, ws: Workspace, store: Store) -> int:
    return _action(controls.assign(store, ws, args.accrual, args.owner, acting(args, store), _as_of(args)))


def cmd_controls_resolve(args, ws: Workspace, store: Store) -> int:
    try:
        amount = parse_amount(args.amount)
    except ValueError:
        return _action(controls.ActionResult("refused", f"--amount is not a number: {args.amount!r}. "
                                                        "Nothing recorded."))
    return _action(controls.resolve(store, ws, args.accrual, args.kind, amount, args.reason, acting(args, store),
                                    args.approved_by or "", args.issue or "", _as_of(args)))


# Approvals (signed-in mode)



def approvals_text(store: Store, include_decided: bool) -> str:
    rows = [(r, d) for r, d in controls.approvals(store) if include_decided or d is None]
    if not rows:
        return "No approval requests." if include_decided else "Nothing waits for approval."
    out = []
    for r, d in rows:
        out.append(f"Request {r.request_id}: requested by {r.requester.label} "
                   f"({identity.when_text(r.at, r.logged_at)})"
                   + (f"; {controls.decision_text(d)} ({identity.when_text(d.at, d.logged_at)})" if d
                      else "; waiting"))
        out += [f"  {line}" for line in r.summary]
    return "\n".join(out)


def cmd_approvals_list(args, ws: Workspace, store: Store) -> int:
    print(approvals_text(store, args.all))
    return 0


def cmd_approvals_approve(args, ws: Workspace, store: Store) -> int:
    return _action(controls.approve(store, ws, args.request, acting(args, store), _as_of(args)))


def cmd_approvals_reject(args, ws: Workspace, store: Store) -> int:
    return _action(controls.reject(store, ws, args.request, acting(args, store), args.reason, _as_of(args)))


# Users and sign-in


def _secret(env: str, prompt: str, confirm: bool = False) -> str:
    """From the environment variable (for scripts) or a prompt that does not echo; never printed."""
    if value := os.environ.get(env):
        return value
    value = getpass.getpass(prompt)
    if confirm and getpass.getpass("Again: ") != value:
        raise IdentityError("The passwords differ. Nothing changed.")
    return value


def _admin(args, store: Store) -> identity.Actor:
    return acting(args, store, identity.ADMIN)


def cmd_users_add(args, ws: Workspace, store: Store) -> int:
    by = _admin(args, store) if identity.signed_in_mode(store) else None
    password = _secret("FCT_NEW_PASSWORD", f"Password for {args.username}: ", confirm=True)
    user = identity.add_user(store, ws, by, args.username, args.name, args.role, password)
    print(f"Added {user.display_name} ({user.username}); roles: {', '.join(user.roles)}.")
    if by is None:
        print("Signed-in mode is on: every action now needs a signed-in user. Sign in with "
              f"fct login {user.username}.")
    return 0


def cmd_users_list(args, ws: Workspace, store: Store) -> int:
    users = store.users()
    if not users:
        print(identity.OPEN_MODE)
        return 0
    print(table(["Username", "Name", "Roles", "Active", "Created by", "Password changed"],
                [[u.username, u.display_name, ", ".join(u.roles), "yes" if u.active else "no", u.created_by,
                  _when(u.password_changed_at)] for u in users]))
    return 0


def cmd_users_enable(args, ws: Workspace, store: Store, active: bool = True) -> int:
    user = identity.set_active(store, _admin(args, store), args.username, active)
    print(f"{user.username} is {'active' if user.active else 'disabled'}.")
    return 0


def cmd_users_disable(args, ws: Workspace, store: Store) -> int:
    return cmd_users_enable(args, ws, store, active=False)


def cmd_users_set_roles(args, ws: Workspace, store: Store) -> int:
    user = identity.set_roles(store, ws, _admin(args, store), args.username, args.role)
    print(f"{user.username} now holds: {', '.join(user.roles)}.")
    return 0


def cmd_users_reset_password(args, ws: Workspace, store: Store) -> int:
    admin = _admin(args, store)
    identity.reset_password(store, admin, args.username,
                            _secret("FCT_NEW_PASSWORD", f"New password for {args.username}: ", confirm=True))
    print(f"Password reset for {args.username}; any lockout is lifted.")
    return 0


def cmd_users_passwd(args, ws: Workspace, store: Store) -> int:
    found = identity.session(store, identity.read_token(args.store))
    if found is None:
        raise IdentityError(identity.SIGN_IN_FIRST)
    identity.change_password(store, found.user.username, _secret("FCT_PASSWORD", "Current password: "),
                             _secret("FCT_NEW_PASSWORD", "New password: ", confirm=True))
    identity.forget_token(args.store)
    print(f"Password changed for {found.user.username}; every session of theirs has ended. Sign in again with "
          f"fct login {found.user.username}.")
    return 0


def cmd_users_remove_all(args, ws: Workspace, store: Store) -> int:
    if not args.yes:
        raise IdentityError("This removes every user and returns the store to open mode, where anyone can act "
                            "as any role. Run it again with --yes to confirm.")
    removed = identity.remove_all_users(store, _admin(args, store))
    identity.forget_token(args.store)
    print(f"Removed {removed} user{'s' * (removed != 1)}. {identity.OPEN_MODE}")
    return 0


def cmd_login(args, ws: Workspace, store: Store) -> int:
    if not identity.signed_in_mode(store):
        print(identity.OPEN_MODE)
        return 1
    user = identity.sign_in(store, args.username, _secret("FCT_PASSWORD", f"Password for {args.username}: "))
    identity.end_session(store, identity.read_token(args.store))
    token = identity.start_session(store, user)
    identity.save_token(args.store, token)
    found = identity.session(store, token)
    print(f"Signed in as {user.display_name} ({user.username}); roles: {', '.join(user.roles)}. "
          f"The session lasts until {_when(found.expires_at)}.")
    return 0


def cmd_logout(args, ws: Workspace, store: Store) -> int:
    token = identity.read_token(args.store)
    identity.end_session(store, token)
    identity.forget_token(args.store)
    print("Signed out." if token else "Not signed in.")
    return 0


def cmd_whoami(args, ws: Workspace, store: Store) -> int:
    if not identity.signed_in_mode(store):
        print(identity.OPEN_MODE)
        return 0
    found = identity.session(store, identity.read_token(args.store))
    if found is None:
        print("Not signed in. Sign in with fct login USERNAME.")
        return 1
    u = found.user
    print(f"{u.display_name} ({u.username}); roles: {', '.join(u.roles)}; session until {_when(found.expires_at)}.")
    return 0


# Planning (`fct plan`) reads only the plan file, never the store or close.toml.


def shown_months(m: planning.Model, months: str) -> tuple:
    """The months printed by month: Year 1 (the first twelve) or, for --months all, every month."""
    return m.months if months == "all" else m.year_one


def line_cells(x: planning.Line, months, years) -> list[str]:
    """A line's monthly values for the shown months, then one column per fiscal year."""
    annual = (x.for_year(y) for y in years)
    return [*(fmt_money(v) for v in x.values[:len(months)]), *("" if v is None else fmt_money(v) for v in annual)]


def statement_text(s: planning.Statement, months, years, ccy: str, note: str = "") -> str:
    head = f"{s.title} ({ccy}{', ' + note if note else ''})"
    rows = [[x.label, *line_cells(x, months, years)] for x in s.lines]
    return head + "\n" + table(["", *(m.label for m in months), *(y.label for y in years)], rows,
                                right=range(1, len(months) + len(years) + 1))


def closing_months(years) -> str:
    """What a balance's fiscal-year column shows: FY2027 = Dec-27, ..."""
    return ", ".join(f"{y.label} = {y.months[-1].label}" for y in years)


def checks_text(m: planning.Model) -> str:
    out = ["Checks", *(f"  {c.status:<7} {c.name}: {c.detail}" for c in m.checks)]
    if m.warnings:
        out += ["", "Warnings", *(f"  {w}" for w in m.warnings)]
    return "\n".join(out)


def model_heading(m: planning.Model) -> str:
    s, months = m.plan.setup, m.months
    return (f"{s.entity}: plan {m.plan.name} (version {m.plan.version}), scenario {m.scenario}, {s.currency}, "
            f"{months[0].label} to {months[-1].label}")


def columns_text(m: planning.Model, months) -> list[str]:
    """Which months are printed and which fiscal years are part years; nothing for a single full year."""
    out = []
    if len(months) < len(m.months):
        out.append(f"By month: {months[0].label} to {months[-1].label} (--months all prints every month); "
                   f"then one column per fiscal year.")
    part = part_years_text(m.years)
    if part:
        out.append(part)
    return out


def changes_text(m: planning.Model) -> list[str]:
    """What the scenario changes against base, as one line under the heading; nothing for base."""
    return [f"Changes against base: {'; '.join(map(str, m.changes))}"] if m.changes else []


def model_text(m: planning.Model, months: str = "year1") -> str:
    ccy, shown, years = m.plan.setup.currency, shown_months(m, months), m.years
    out = [model_heading(m), *changes_text(m), *columns_text(m, shown)]
    if m.pnl_only_reason:
        out.append(m.pnl_only_reason)
    out += ["", statement_text(m.pnl, shown, years, ccy)]
    if m.balance_sheet is not None:
        out += ["", statement_text(m.balance_sheet, shown, years, ccy, f"month-end; {closing_months(years)}")]
    if m.cash_flow is not None:
        out += ["", statement_text(m.cash_flow, shown, years, ccy)]
    return "\n".join([*out, "", checks_text(m)])


def compare_text(c: planning.Comparison, months: str = "year1") -> str:
    base, scen = c.base, c.scenario
    shown, years, ccy = shown_months(base, months), base.years, base.plan.setup.currency
    rows = []
    for r in c.rows:
        for name, line in (("base", r.base), (scen.scenario, r.scenario), ("change", r.change)):
            rows.append([r.label if name == "base" else "", name, *line_cells(line, shown, years)])
    failing = [m.scenario for m in (base, scen) if not m.passed]
    if failing:
        checks = f"FAIL in {', '.join(failing)} (fct plan run shows which)"
    elif base.pnl_only_reason or scen.pnl_only_reason:
        checks = "balance sheet checks not run (P&L-only)"
    else:
        checks = "all pass in both"
    return "\n".join([
        f"{model_heading(scen)}, against base",
        *changes_text(scen),
        *columns_text(base, shown),
        f"Key lines ({ccy}; a fiscal-year column is the annual total for P&L lines and the closing month for "
        f"balances: {closing_months(years)})",
        table(["Line", "Scenario", *(m.label for m in shown), *(y.label for y in years)], rows,
              right=range(2, len(shown) + len(years) + 2)),
        "",
        f"Lowest month-end cash: base {planning.lowest_text(base)}; {scen.scenario} {planning.lowest_text(scen)}",
        *([f"By fiscal year: {lowest_by_year_text(base, scen)}"] if len(years) > 1 else []),
        f"Checks: {checks}",
    ])


def lowest_by_year_text(base: planning.Model, scen: planning.Model) -> str:
    def one(m: planning.Model, i: int) -> str:
        if not m.lowest_cash_by_year:
            return "n/a"
        _, month, cash = m.lowest_cash_by_year[i]
        return f"{fmt_money(cash)} ({month.label})"
    return "; ".join(f"{y.label} base {one(base, i)}, {scen.scenario} {one(scen, i)}" for i, y in enumerate(base.years))


def cmd_plan_check(args) -> int:
    try:
        plan = load_plan(Path(args.plan))
    except PlanError as e:
        n = len(e.problems)
        print(f"{args.plan}: {n} problem{'s' * (n != 1)}")
        print("\n".join(f"  {p}" for p in e.problems))
        return 1
    print(f"{args.plan}: valid (plan {plan.name}, version {plan.version}, {plan.setup.entity}, "
          f"{plan.setup.currency}, {plan.months[0]} to {plan.months[-1]})")
    print(f"Scenarios: {', '.join(('base', *plan.scenario_names))}")
    if plan.pnl_only:
        print(f"Missing inputs: {', '.join(plan.missing)}. The plan runs P&L only.")
    else:
        print("Balance sheet inputs complete: opening balance sheet, collections and payments.")
    return 0


def cmd_plan_run(args) -> int:
    m = planning.run(load_plan(Path(args.plan)), args.scenario)
    print(model_text(m, args.months))
    return 0 if m.passed else 1


def cmd_plan_compare(args) -> int:
    plan = load_plan(Path(args.plan))
    c = planning.compare(planning.run(plan), planning.run(plan, args.scenario))
    print(compare_text(c, args.months))
    return 0 if c.base.passed and c.scenario.passed else 1


def cmd_plan_export(args) -> int:
    m = planning.run(load_plan(Path(args.plan)), args.scenario)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = plan_xlsx.write_plan_xlsx(out_dir / f"plan_{m.plan.name}_{m.scenario}.xlsx", m)
    print(f"Wrote {path}")
    if not m.passed:
        print("warning: some checks fail; see the Checks sheet", file=sys.stderr)
    return 0


def cmd_plan_to_budget(args) -> int:
    m = planning.run(load_plan(Path(args.plan)), args.scenario)
    ws = load_workspace(Path(args.config))
    sub = plan_budget.budget_submission(m, args.period, ws)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / sub.file_name
    path.write_bytes(sub.xlsx_bytes(ws.coa))
    print(f"Wrote {path}")
    print(f"{sub.market} budget {sub.period} ({sub.currency}) from plan {sub.plan} (version {sub.version}), "
          f"scenario {sub.scenario}, on the regional submission template")
    print(table(["Account", "Plan line", "Amount"], [[r.account, r.line, fmt_money(r.amount)] for r in sub.rows],
                right=(2,), indent="  "))
    print(plan_budget.SIGN_NOTE)
    print(sub.left_out_text)
    print(f"To use it: point {sub.market}'s mapping in close.toml at mappings/_regional_template.toml, then "
          f"fct ingest {path} --market {sub.market} --period {sub.period} --scenario budget")
    return 0


LOCAL_HOST = "127.0.0.1"


def wait_for_port(host: str, port: int, process: subprocess.Popen, timeout: float = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def cmd_ui(args) -> int:
    """Runs the Streamlit app; it opens the store itself, so the CLI must not hold it open.
    Headless, so Streamlit never stops at its first-run email prompt; we open the browser."""
    src = Path(__file__).resolve().parent.parent
    env = {**os.environ, "FCT_STORE": args.store, "FCT_CONFIG": args.config,
           "PYTHONPATH": os.pathsep.join(filter(None, [str(src), os.environ.get("PYTHONPATH")]))}
    if args.fx_dir:
        env["FCT_FX_DIR"] = args.fx_dir
    if args.host != LOCAL_HOST:
        print(f"warning: listening on {args.host}, so other computers can reach it; the UI has no login until the "
              "store has users (fct users add), and until then anyone who reaches it can ingest files and publish "
              "releases.", file=sys.stderr)
    command = [sys.executable, "-m", "streamlit", "run", str(src / "finance_close_toolkit" / "app.py"),
               "--server.address", args.host, "--server.port", str(args.port), "--server.headless", "true",
               "--browser.gatherUsageStats", "false", "--server.maxUploadSize", "20"]
    process = subprocess.Popen(command, env=env)
    browse = LOCAL_HOST if args.host in ("0.0.0.0", "::") else args.host
    if not args.no_browser and wait_for_port(browse, args.port, process):
        webbrowser.open(f"http://{browse}:{args.port}")
    try:
        return process.wait()
    except KeyboardInterrupt:
        return process.wait()


BY_HELP = "the role acting (open mode); signed in, the same as --as"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", default=str(DEFAULT_STORE), help="DuckDB store file")
    common.add_argument("--config", default="config/close.toml", help="close.toml path")
    common.add_argument("--fx-dir", default=None, help="FX rate folder (default: fx/ next to the config folder)")
    common.add_argument("--as", dest="as_role", metavar="ROLE",
                        help="signed-in mode: which of your roles you act in (default: your only role)")

    p = argparse.ArgumentParser(prog="fct", description="APAC regional close toolkit")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(func=func)
        return sp

    sp = add("inspect", cmd_inspect, "preview how a file will be read and mapped")
    sp.add_argument("file")
    sp.add_argument("--market", required=True)

    sp = add("ingest", cmd_ingest, "store and validate a submission")
    sp.add_argument("file")
    sp.add_argument("--market", required=True)
    sp.add_argument("--period", required=True)
    sp.add_argument("--scenario", required=True)
    sp.add_argument("--received-at")

    sp = add("revalidate", cmd_revalidate, "validate latest submissions under the current mappings")
    sp.add_argument("--period", required=True)

    sp = add("status", cmd_status, "status board and needs-attention signals")
    sp.add_argument("--period", required=True)
    sp.add_argument("--as-of")

    sp = add("publish", cmd_publish, "dry run, or publish with --approve (--by ROLE in open mode)")
    sp.add_argument("--period", required=True)
    sp.add_argument("--approve", action="store_true")
    sp.add_argument("--by")
    sp.add_argument("--expect", metavar="CHECKSUM",
                    help="with --approve: the facts checksum the dry run printed; refuses if the proposal changed")

    sp = add("diff", cmd_diff, "KPI and account deltas between two releases")
    sp.add_argument("--from", dest="from_id", required=True)
    sp.add_argument("--to", dest="to_id", required=True)

    sp = add("trace", cmd_trace, "facts behind one figure, down to file and row")
    sp.add_argument("--release", required=True)
    sp.add_argument("--market", required=True)
    sp.add_argument("--account", required=True)
    sp.add_argument("--scenario")

    sp = add("verify-release", cmd_verify, "rebuild a release from stored rows and check its checksum")
    sp.add_argument("release")

    sp = add("export", cmd_export, "write facts.csv, manifest.json and release.xlsx")
    sp.add_argument("release")
    sp.add_argument("--out", required=True)
    sp.add_argument("--currency", help="also show the P&L in this currency in release.xlsx (a stored rate)")

    def report_source(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--release", help="an approved release ID")
        sp.add_argument("--period", help="with --proposed: the period whose proposed release to use")
        sp.add_argument("--proposed", action="store_true", help="use what publish would release now (DRAFT)")
        sp.add_argument("--currency", help="show amounts in this currency, at the rates stored with the release "
                                           "(default: the reporting currency)")

    sp = add("variance", cmd_variance, "P&L vs budget for a release, with market summary and EBITDA bridge")
    report_source(sp)
    sp.add_argument("--market", help="one market instead of APAC")

    sp = add("deck", cmd_deck, "write the Comex PowerPoint deck for a release ('deck packs' lists the packs)")
    sp.add_argument("what", nargs="?", choices=["packs"], help="packs: list the deck packs and their slides")
    report_source(sp)
    sp.add_argument("--out", help="output folder")
    sp.add_argument("--pack", metavar="NAME|FILE",
                    help="which slides: a pack in deck/packs next to close.toml, or a .toml file (default: comex)")
    sp.add_argument("--commentary", help="outlook and actions TOML file")
    sp.add_argument("--force", action="store_true", help="replace an existing deck file")
    sp.add_argument("--template", help="PowerPoint template (default: deck/template.pptx next to close.toml, if present)")
    sp.add_argument("--style", help="deck colours and sizes (default: deck/style.toml next to close.toml, if present)")

    controls_parser = sub.add_parser("controls", help="close controls: accrual, invoice, reversal")
    csub = controls_parser.add_subparsers(dest="controls_command", required=True)

    def control(name: str, func, help_: str, when: str) -> argparse.ArgumentParser:
        sp = csub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(func=func)
        sp.add_argument("--as-of", help=when)
        return sp

    viewed = "show the state as of this time (default: now)"
    recorded = "when the action is recorded (default: now)"
    sp = control("import", cmd_controls_import, "import accrual, invoice and GL files (CSV or xlsx)",
                 "when the files are imported (default: now)")
    for kind in controls.KINDS:
        sp.add_argument(f"--{kind}", metavar="FILE")
        sp.add_argument(f"--{kind}-mapping", metavar="TOML",
                        help=f"read --{kind} with this mapping instead of config/controls/mappings/{kind}.toml")
    sp.add_argument("--allow-drop", action="store_true",
                    help="import even though it drops accruals or invoices that carry evidence (recorded)")
    sp.add_argument("--reason", help="why, for --allow-drop")
    sp.add_argument("--by", metavar="ROLE", help="who is dropping, for --allow-drop (open mode; signed in: --as)")
    sp = csub.add_parser("inspect", parents=[common], help="preview how a controls file will be read; imports nothing")
    sp.set_defaults(func=cmd_controls_inspect)
    sp.add_argument("file")
    sp.add_argument("--kind", required=True, choices=controls.KINDS)
    sp.add_argument("--mapping", metavar="TOML", help="instead of config/controls/mappings/KIND.toml")
    control("status", cmd_controls_status, "queues by role, states and P&L effect", viewed)
    sp = control("show", cmd_controls_show, "one accrual: evidence, state, journal preview, history", viewed)
    sp.add_argument("accrual")
    sp = control("link", cmd_controls_link, "AP links an invoice to an accrual", recorded)
    sp.add_argument("invoice")
    sp.add_argument("accrual")
    sp.add_argument("--by", metavar="ROLE", help=BY_HELP)
    sp.add_argument("--override", action="store_true", help="link an accrual that is not a candidate")
    sp.add_argument("--reason", help="why, for --override")
    sp.add_argument("--approved-by", metavar="ROLE", help="approver role, for --override (open mode only)")
    sp = control("explain", cmd_controls_explain, "the owner explains an invoice variance", recorded)
    sp.add_argument("accrual")
    sp.add_argument("--note", required=True)
    sp.add_argument("--by", metavar="ROLE", help=BY_HELP)
    sp.add_argument("--approved-by", metavar="ROLE",
                    help="approver role, for a variance over explain_approval_abs (open mode only)")
    sp = control("assign", cmd_controls_assign, "the regional close lead assigns an owner", recorded)
    sp.add_argument("accrual")
    sp.add_argument("--owner", required=True, metavar="ROLE")
    sp.add_argument("--by", metavar="ROLE", help=BY_HELP)
    sp = control("resolve", cmd_controls_resolve, "record an approved resolution", recorded)
    sp.add_argument("accrual")
    sp.add_argument("--kind", required=True, choices=controls.RESOLVE_KINDS)
    sp.add_argument("--issue", choices=tuple(controls.ISSUE_RULES),
                    help="the issue this resolves (default: the only open one)")
    sp.add_argument("--amount", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--by", metavar="ROLE", help=BY_HELP)
    sp.add_argument("--approved-by", metavar="ROLE",
                    help="approver role (open mode only; signed in, the resolution waits in approvals)")

    attention_parser = sub.add_parser("attention", help="needs attention: acknowledge, assign, due dates")
    atsub = attention_parser.add_subparsers(dest="attention_command", required=True)

    def attention_command(name: str, func, help_: str, key: bool = True, as_of: bool = True) -> argparse.ArgumentParser:
        sp = atsub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(func=func)
        if as_of:
            sp.add_argument("--as-of", help="when, for overdue and the record (default: now)")
        if key:
            sp.add_argument("key", help="the item key, as fct attention list prints it")
        sp.add_argument("--by", metavar="ROLE", help="who is acting (open mode; signed in: --as)")
        return sp

    sp = attention_command("list", cmd_attention_list, "open items on the close board and in close controls",
                           key=False)
    sp.add_argument("--period", help="only this period")
    sp.add_argument("--mine", action="store_true", help="assigned to me (open mode: to --by ROLE)")
    sp.add_argument("--overdue", action="store_true")
    sp.add_argument("--state", choices=workflow.STATES)
    attention_command("show", cmd_attention_show, "an item's state and history")
    sp = attention_command("ack", cmd_attention_ack, "acknowledge: the owner or the close lead")
    sp.add_argument("--note")
    sp = attention_command("assign", cmd_attention_assign, "assign to a person (open mode: a role)")
    sp.add_argument("--to", required=True, metavar="USER")
    sp = attention_command("due", cmd_attention_due, "set the due date: the close lead")
    sp.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    sp = attention_command("comment", cmd_attention_comment, "add a note to the item's history")
    sp.add_argument("--note", required=True)
    sp = attention_command("refresh", cmd_attention_refresh, "record what the data shows now, after a config-only "
                           "change (signed in: needs a sign-in; recorded at the real time)", key=False, as_of=False)
    sp.add_argument("--period", help="also observe this period")

    approvals_parser = sub.add_parser("approvals", help="signed-in mode: requests waiting for a second person")
    asub = approvals_parser.add_subparsers(dest="approvals_command", required=True)
    sp = asub.add_parser("list", parents=[common], help="requests waiting for approval (--all: decided ones too)")
    sp.set_defaults(func=cmd_approvals_list)
    sp.add_argument("--all", action="store_true")
    for name, func, help_ in (("approve", cmd_approvals_approve, "approve a request from your own sign-in"),
                              ("reject", cmd_approvals_reject, "reject a request, with a reason")):
        sp = asub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(func=func)
        sp.add_argument("request", type=int, metavar="ID")
        sp.add_argument("--as-of", help=recorded)
        if name == "reject":
            sp.add_argument("--reason", required=True)

    users_parser = sub.add_parser("users", help="users and roles (admin), and your own password")
    usub = users_parser.add_subparsers(dest="users_command", required=True)

    def users_command(name: str, func, help_: str, username: bool = True) -> argparse.ArgumentParser:
        sp = usub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(func=func)
        if username:
            sp.add_argument("username")
        return sp

    password_note = " (password from a prompt, or FCT_NEW_PASSWORD)"
    sp = users_command("add", cmd_users_add, "add a user; the first user must include admin" + password_note)
    sp.add_argument("--name", required=True, help="the person's name, shown in history and approvals")
    sp.add_argument("--role", action="append", required=True, help="a role from config, or admin; repeat for more")
    users_command("list", cmd_users_list, "list users and their roles", username=False)
    users_command("disable", cmd_users_disable, "disable a user (keeps their history)")
    users_command("enable", cmd_users_enable, "enable a disabled user")
    sp = users_command("set-roles", cmd_users_set_roles, "replace a user's roles")
    sp.add_argument("--role", action="append", required=True)
    users_command("reset-password", cmd_users_reset_password, "set a new password and lift a lockout" + password_note)
    users_command("passwd", cmd_users_passwd, "change your own password (FCT_PASSWORD, FCT_NEW_PASSWORD)",
                  username=False)
    sp = users_command("remove-all", cmd_users_remove_all, "remove every user: back to open mode", username=False)
    sp.add_argument("--yes", action="store_true", help="confirm")

    sp = add("login", cmd_login, "sign in for this store (password from a prompt, or FCT_PASSWORD)")
    sp.add_argument("username")
    add("logout", cmd_logout, "sign out of this store")
    add("whoami", cmd_whoami, "who is signed in for this store")

    plan_parser = sub.add_parser("plan", help="driver-based plan: P&L, balance sheet and cash flow by month")
    psub = plan_parser.add_subparsers(dest="plan_command", required=True)

    def plan_command(name: str, func, help_: str) -> argparse.ArgumentParser:
        sp = psub.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        sp.add_argument("plan", help="plan file, e.g. plans/agency_2027.toml")
        return sp

    plan_command("check", cmd_plan_check, "validate the plan file; list errors and missing inputs")
    def months_option(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--months", choices=["year1", "all"], default="year1",
                        help="year1: the first twelve months by month, then the fiscal years (default); all: every month")

    sp = plan_command("run", cmd_plan_run, "monthly P&L, balance sheet and cash flow with annual totals and checks")
    sp.add_argument("--scenario", help="a scenario in the plan (default: base)")
    months_option(sp)
    sp = plan_command("compare", cmd_plan_compare, "scenario vs base: key lines by month and year")
    sp.add_argument("--scenario", required=True)
    months_option(sp)
    sp = plan_command("export", cmd_plan_export, "write plan_<name>_<scenario>.xlsx (values only)")
    sp.add_argument("--out", required=True, help="output folder")
    sp.add_argument("--scenario", help="a scenario in the plan (default: base)")
    sp = plan_command("to-budget", cmd_plan_to_budget,
                      "write one plan month as the [budget] market's budget submission (regional template)")
    sp.add_argument("--period", required=True, help="the plan month, YYYY-MM")
    sp.add_argument("--scenario", help="a scenario in the plan (default: base)")
    sp.add_argument("--out", required=True, help="output folder")
    sp.add_argument("--config", default="config/close.toml",
                    help="close.toml path: the market's currency and the chart of accounts")

    templates_parser = sub.add_parser("templates", help="ready-to-fill input templates")
    tsub = templates_parser.add_subparsers(dest="templates_command", required=True)
    sp = tsub.add_parser("write", help="write the submission, accruals, invoices and GL templates")
    sp.set_defaults(func=cmd_templates_write)
    sp.add_argument("--out", required=True, help="output folder")
    sp.add_argument("--csv", action="store_true", help="CSV header files and instruction files instead of xlsx")
    sp.add_argument("--config", default="config/close.toml",
                    help="close.toml path; the Instructions list its chart of accounts")

    sp = add("ui", cmd_ui, "open the browser UI (Streamlit)")
    sp.add_argument("--port", type=int, default=8501)
    sp.add_argument("--host", default=LOCAL_HOST,
                    help="address to listen on (default: this computer only); the UI has no login until the "
                         "store has users")
    sp.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    return p


# What a console that cannot show a character (Windows cp1252) prints instead, so output never fails mid-command.
_PLAIN = {"\u2192": "->", "\u2212": "-", "\u2014": "-", "\u2013": "-", "\u2026": "..."}


def _plain(e: UnicodeEncodeError) -> tuple[str, int]:
    return "".join(_PLAIN.get(ch, "?") for ch in e.object[e.start:e.end]), e.end


codecs.register_error("fct-plain", _plain)


def _safe_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="fct-plain")


def main(argv: Sequence[str] | None = None) -> int:
    _safe_console()
    args = build_parser().parse_args(argv)
    if args.command == "ui":
        return cmd_ui(args)
    if args.command in ("plan", "templates"):
        try:
            return args.func(args)
        except (PlanError, ConfigError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    try:
        ws = load_workspace(Path(args.config), Path(args.fx_dir) if args.fx_dir else None)
        with Store.open(args.store) as store:
            return args.func(args, ws, store)
    except IdentityError as e:
        print(f"Refused: {e}")
        return 1
    except (ConfigError, IntakeError, ReleaseError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
