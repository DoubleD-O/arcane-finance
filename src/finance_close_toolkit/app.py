"""Streamlit UI over the close library (`fct ui`). It renders library results and computes no figures."""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import altair as alt
import streamlit as st

from finance_close_toolkit import (attention, controls, deck, deck_pack, demo, plan_budget, planning, release, validation,
                                   variance, xlsx)
from finance_close_toolkit.commentary import parse_commentary
from finance_close_toolkit.config import ConfigError, Workspace, load_workspace
from finance_close_toolkit.intake import IntakeError, InspectReport, inspect
from finance_close_toolkit.kpi import APAC, KPIS, Diff, totals
from finance_close_toolkit.money import (fmt_amount, fmt_money, fmt_pct, fmt_quantity, fmt_percent, fmt_pp, fmt_step,
                                         parse_amount, plain)
from finance_close_toolkit.plan_config import Month, PlanError, load_plan, part_years_text
from finance_close_toolkit.release import ReleaseError
from finance_close_toolkit.store import DEFAULT_STORE, Store
from finance_close_toolkit.templates import template_file

READABLE_ERRORS = (ConfigError, IntakeError, ReleaseError, OSError, ValueError)


@dataclass(frozen=True)
class Page:
    ws: Workspace
    store: Store
    period: str


def words(code: str) -> str:
    """needs_review -> Needs review; MATERIAL_CHANGE -> Material change."""
    return code.replace("_", " ").capitalize()


def when(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M")


def parse_when(text: str, label: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must look like 2026-11-07 15:00, got {text!r}") from None


NUMBER_COLUMNS = {"Before", "After", "Change", "Change %", "Actual", "Budget", "Variance", "Var %", "Amount", "Rate",
                  "Row", "Exceptions", "Accrued", "Invoiced", "Invoices", "Reversals", "Net",
                  *(k.label for k in KPIS)}


def table(rows: list[dict]) -> None:
    """Amounts arrive as formatted text (the CLI's strings); number columns are right-aligned.
    A dataframe, not st.table: st.table renders cells as Markdown, and cells hold text from files."""
    columns = rows[0].keys() if rows else ()
    right = {c: st.column_config.TextColumn(alignment="right") for c in columns
             if c in NUMBER_COLUMNS or c.endswith(" amount")}
    st.dataframe(rows, hide_index=True, column_config=right)


_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$])")


def esc(text: str) -> str:
    """File names, file text and inputs shown literally, never as Markdown (links, images, maths)."""
    return _MARKDOWN.sub(r"\\\1", text)


def session_dir(name: str) -> Path:
    """A temporary folder for this browser session (uploads, downloads)."""
    key = f"dir_{name}"
    if key not in st.session_state:
        st.session_state[key] = tempfile.mkdtemp(prefix=f"fct-{name}-")
    return Path(st.session_state[key])


def diff_tables(d: Diff, ws: Workspace, accounts: bool = True, currency: str | None = None) -> None:
    ccy = currency or ws.config.reporting_currency
    if not d.kpis:
        st.write("No changes.")
        return
    st.markdown(f"**KPI changes ({ccy})**")
    table([{"Scenario": k.scenario, "Scope": k.scope, "KPI": k.kpi.label, "Before": fmt_amount(k.before),
            "After": fmt_amount(k.after), "Change": fmt_amount(k.change), "Change %": fmt_pct(k.pct),
            "Materiality": "Material" if k.material else "Below threshold"} for k in d.kpis])
    if accounts:
        st.markdown(f"**Account changes ({ccy})**")
        table([{"Scenario": a.scenario, "Market": a.market, "Account": a.account,
                "Name": ws.coa.accounts[a.account].name, "Before": fmt_amount(a.before),
                "After": fmt_amount(a.after), "Change": fmt_amount(a.change)} for a in d.accounts])


# Close board


def close_board(p: Page) -> None:
    as_of = parse_when(st.text_input("As of (YYYY-MM-DD HH:MM, blank for now)", key="as_of"), "As of")
    b = attention.board(p.store, p.ws, p.period, as_of)
    st.write(f"As of {when(b.as_of)}. Actuals due {b.due.isoformat()}. "
             f"Current release: {b.current_release_id or 'none'}.")
    st.subheader("Status")
    rows = []
    for v in b.slots:
        s, sub = v.slot, v.slot.submission
        rows.append({"Market": s.market.code, "Scenario": s.scenario, "Status": words(v.status),
                     "File": sub.file_name if sub else "", "Received": when(sub.received_at) if sub else "",
                     "Exceptions": str(len(s.validation.exceptions)) if s.validation else "", "Note": v.note})
    table(rows)
    if b.changes is not None:
        st.subheader(f"Changes vs {b.current_release_id} if the latest validated files were published")
        diff_tables(b.changes, p.ws)
    st.subheader("Needs attention")
    if not b.signals:
        st.write("Nothing needs attention.")
    for i, s in enumerate(b.signals, 1):
        with st.container(border=True):
            st.markdown(f"**{i}. {esc(f'{words(s.type)}: {s.market} {s.scenario}')}**"
                        + (" (overdue)" if s.overdue else ""))
            st.markdown("\n".join("- " + esc(line) for line in [
                f"Owner: {s.owner}",
                f"Due: {s.due.isoformat()}" + (", overdue" if s.overdue else ", not overdue"),
                f"Trigger: {s.trigger}",
                f"Release: {s.release_id or 'none'}",
                f"Next action: {s.next_action}",
            ]))
            if s.exceptions:
                st.markdown("Exceptions")
                table([{"Code": e.code, "Row": "" if e.row is None else str(e.row), "Detail": e.detail}
                       for e in s.exceptions])
            if s.impact is None:
                st.write("Impact: unknown")
            else:
                diff_tables(s.impact, p.ws, accounts=False)


# Submit a file


def save_upload(upload) -> Path:
    """Saved under its original file name (kept for lineage) in a per-content folder. Never executed."""
    data = upload.getvalue()
    folder = session_dir("uploads") / hashlib.sha256(data).hexdigest()[:16]
    folder.mkdir(exist_ok=True)
    path = folder / Path(upload.name).name
    if not path.exists():
        path.write_bytes(data)
    return path


def show_validation(v: validation.Validation, currency: str) -> None:
    n = len(v.exceptions)
    st.write(f"Validation: {words(v.state)} under mapping {v.mapping_version} ({n} exception{'s' * (n != 1)}).")
    if v.exceptions:
        table([{"Code": e.code, "Row": "" if e.row is None else str(e.row), "Detail": e.detail} for e in v.exceptions])
    show_tie_out(v.tie_out, currency)


def show_tie_out(t: validation.TieOut, currency: str) -> None:
    st.write(f"Tie-out ({currency}, as in file): source {fmt_amount(t.source_total)} = "
             f"mapped {fmt_amount(t.mapped_total)} + unmapped {fmt_amount(t.unmapped_total)}")


def ingest_message(r: validation.IngestResult) -> str:
    s = r.submission
    slot = f"{s.market} {s.scenario} {s.period}"
    if r.rereceived and r.is_latest:
        text = f"{s.file_name} re-received; now the latest for {slot}, replacing {r.previous.file_name}."
    elif r.rereceived:
        text = f"{s.file_name} re-received as {slot}."
    elif r.repeat and not r.revalidated:
        text = (f"Repeat: {s.file_name} is already stored as {slot} (received {when(s.received_at)}) "
                "and validated under the current mapping. Nothing changed.")
    elif r.repeat:
        text = f"Repeat: {s.file_name} is already stored as {slot}; validated again under the new mapping."
    else:
        text = f"Ingested {s.file_name} as {slot}, received {when(s.received_at)}."
    if not r.is_latest:
        text += f" This is NOT the latest for {slot}: {r.latest.file_name} (received {when(r.latest.received_at)}) is."
    return text


RESULT_BOX = {"validated": st.success, "needs_review": st.warning, "rejected": st.error}


def preview_confirmation(report: InspectReport, suffix: str, market: str, slot: str) -> str | None:
    """Warns about a file that is unlikely to be the slot's submission; returns the label of the
    confirmation it needs before Ingest, or None when it needs none."""
    label = None
    if missing := [c.name for c in report.columns if not c.found]:
        st.warning(esc(f"This file is missing required columns: {', '.join(missing)}. Ingesting it records it "
                       f"as rejected, and it becomes the latest file for {slot}, which blocks publish until a "
                       "corrected file arrives."))
        label = "Ingest anyway; it will be recorded as rejected and become the latest file for this slot"
    expected = report.mapping.layout.format
    if controls.source_format(f"file{suffix}") != expected:
        st.warning(esc(f"This is a {suffix or 'extensionless'} file but the {market} mapping reads {expected} "
                       f"files; check it is the right file for {market}."))
        label = label or "Ingest anyway; it will become the latest file for this slot"
    return label


def template_download(kind: str, ws: Workspace) -> None:
    name, data = template_file(kind, ws.coa)
    st.download_button(esc(f"Download {name}"), data, file_name=name, key=f"template_{kind}")


def submit(p: Page) -> None:
    template_download("submission", p.ws)
    st.caption("The regional template: a market that uses it points its mapping at "
               "config/mappings/_regional_template.toml.")
    upload = st.file_uploader("Submission file (CSV or Excel)", type=list(controls.UPLOAD_TYPES))
    markets = {m.code: m for m in p.ws.config.markets}
    market = st.selectbox("Market", list(markets), format_func=lambda c: f"{c} {markets[c].name}",
                          key="submit_market")
    scenario = st.selectbox("Scenario", p.ws.config.scenarios)
    st.caption(f"Period {p.period} (change it in the sidebar).")
    with st.expander("Advanced"):
        received_text = st.text_input("Received at (YYYY-MM-DD HH:MM, blank for now)", key="received_at")
    if upload is None:
        st.write("Choose a file to preview how it will be read and mapped.")
        return

    path = save_upload(upload)
    report = inspect(path, p.ws.mapping(market))
    t = report.table
    st.subheader("Preview")
    suffix = path.suffix.lower()
    st.markdown(esc(f"File {report.file_name} ({suffix} file)"
                    + (f", sheets {', '.join(t.sheets)}; using {t.sheet}" if t.sheets else "")
                    + f". Header row {report.header_row}."))
    table([{"Configured column": c.name, "Found": "Yes" if c.found else "Missing", "Closest header": c.closest or ""}
           for c in report.columns])
    if t.rows:
        table([{"Row": str(r.row_number), "Source account": r.source_account, "Description": r.description,
                "Amount": fmt_amount(r.amount) if r.amount is not None else f"Not a number: {r.amount_text}",
                "Maps to": report.regional_account(r) or "UNMAPPED"} for r in t.rows])
        show_tie_out(validation.tie_out(t.rows, report.mapping), markets[market].currency)

    label = preview_confirmation(report, suffix, market, f"{market} {scenario} {p.period}")
    confirmed = label is None or st.checkbox(label, key=f"confirm_{path.parent.name}_{market}_{scenario}")
    if st.button("Ingest", type="primary", disabled=not confirmed):
        received = parse_when(received_text, "Received at")
        r = validation.ingest(p.store, p.ws, path, market, p.period, scenario, received)
        st.session_state.pop("demo_notice", None)
        RESULT_BOX[r.validation.state](esc(ingest_message(r)))
        show_validation(r.validation, markets[market].currency)


# Review and publish


def approve(checksum: str) -> None:
    """The Approve button's callback: remembers the checksum of the proposal on screen when it was clicked."""
    st.session_state["approve_checksum"] = checksum


def publish_reviewed(p: Page, checksum: str) -> release.PublishResult | None:
    """Publishes only the proposal the approver saw; the library refuses if it changed since."""
    approver = st.session_state.get("approved_by", "").strip()
    if not approver:
        st.error("Enter who approves this release before publishing.")
        return None
    done = release.publish(p.store, p.ws, p.period, approved_by=approver, expected_checksum=checksum)
    previous = done.proposal.current.release_id if done.proposal.current else "none"
    if done.outcome == "published":
        st.session_state.pop("demo_notice", None)
        st.success(esc(f"Published {done.release_id} (previous {previous}), approved by {approver}."))
    elif done.outcome == "unchanged":
        st.info(f"No change: the facts match current release {done.release_id}.")
    else:
        st.error(esc("Publish refused: " + "; ".join(done.reasons)))
    return done


def review(p: Page) -> None:
    done = None
    if checksum := st.session_state.pop("approve_checksum", None):
        done = publish_reviewed(p, checksum)
    r = release.publish(p.store, p.ws, p.period)
    current = r.proposal.current
    current_id = current.release_id if current else "none"
    if r.outcome == "refused":
        st.error(f"Publish is blocked for {p.period}. Nothing can be approved until this is resolved:")
        st.markdown("\n".join(f"- {esc(b)}" for b in r.reasons))
        st.write(f"Current release: {current_id}.")
        return
    if r.outcome == "unchanged":
        if done is None or done.outcome != "published":
            st.write(f"No change: the facts match current release {r.release_id}. Nothing to approve.")
        return

    st.subheader(f"Proposed release {r.release_id} (previous {current_id})")
    if current is None:
        st.write(f"First release for {p.period}; nothing to compare against.")
        ccy = p.ws.config.reporting_currency
        st.markdown(f"**KPI totals ({ccy})**")
        table([{"Scenario": scenario, "Scope": scope, **{k.label: fmt_amount(v[k.key]) for k in KPIS}}
               for scenario, by_scope in totals(r.proposal.facts, p.ws.coa).items()
               for scope, v in by_scope.items()])
    else:
        st.write(f"Changes vs {current.release_id}:")
        diff_tables(r.diff, p.ws)

    st.markdown("**Files to publish (slots that change)**")
    st.markdown("\n".join("- " + esc(f"{s.market.code} {s.scenario}: {s.submission.file_name}, "
                                     f"received {when(s.submission.received_at)}")
                          for s in r.proposal.slots if s.changes_release))
    st.text_input("Approved by (required)", key="approved_by")
    st.button("Approve and publish", type="primary", on_click=approve, args=(r.facts_checksum,))


# Results


def variance_rows(rows) -> list[dict]:
    out = []
    for row in rows:
        if row.line.kind == "ratio":
            cells = [fmt_percent(row.actual), fmt_percent(row.budget), fmt_pp(row.variance), ""]
        else:
            cells = [fmt_money(row.actual), fmt_money(row.budget), fmt_money(row.variance),
                     fmt_percent(row.variance_pct)]
        out.append(dict(zip(["Line", "Actual", "Budget", "Variance", "Var %", "Fav / Unfav"],
                            [row.line.label, *cells, row.label])))
    return out


def bridge_chart(r: variance.VarianceReport) -> alt.Chart:
    b = r.bridge
    bars = [{"Step": "Budget EBITDA", "From": 0.0, "To": float(b.budget), "Amount": fmt_money(b.budget),
             "Kind": "Total"}]
    bars += [{"Step": s.market, "From": float(s.before), "To": float(s.after), "Amount": fmt_step(s.amount),
              "Kind": r.row(s.market, "ebitda").label} for s in b.steps]
    bars.append({"Step": "Actual EBITDA", "From": 0.0, "To": float(b.actual), "Amount": fmt_money(b.actual),
                 "Kind": "Total"})
    order = [bar["Step"] for bar in bars]
    kinds = alt.Scale(domain=["Total", "Favourable", "Unfavourable", "On plan"],
                      range=["#44546A", "#2E7D32", "#C62828", "#8C8C8C"])
    return alt.Chart(alt.Data(values=bars)).mark_bar().encode(
        x=alt.X("Step:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("From:Q", title=f"EBITDA ({r.currency})"), y2="To:Q",
        color=alt.Color("Kind:N", scale=kinds, title=None),
        tooltip=["Step:N", "Amount:N", "Kind:N"],
    )


def report_heading(r: variance.VarianceReport) -> str:
    return (f"Release {r.release_id} ({r.period}), final. Approved by {r.approved_by} on {when(r.approved_at)}. "
            f"Checksum {r.facts_checksum[:12]}. Amounts in {r.currency_label}.")


def release_picker(p: Page, label: str, key: str) -> str | None:
    ids = [r.release_id for r in p.store.releases(p.period)]
    if not ids:
        st.write(f"No release yet for {p.period}. Publish one on Review and publish.")
        return None
    current = p.store.current_release(p.period)
    return st.selectbox(label, ids, index=ids.index(current.release_id), key=key,
                        format_func=lambda i: f"{i} (current)" if i == current.release_id else i)


def currency_picker(p: Page, release_id: str, key: str) -> str:
    """The base currency, or another one whose rate the release stores."""
    stored = p.store.release(release_id)
    return st.selectbox("Currency", variance.available_currencies(stored.reporting_currency, stored.rates), key=key)


def results(p: Page) -> None:
    release_id = release_picker(p, "Release", "results_release")
    if release_id is None:
        return
    r = variance.release_report(p.store, p.ws, release_id, currency_picker(p, release_id, "results_currency"))
    st.markdown(esc(report_heading(r)))
    if r.translation_note:
        st.caption(esc(r.translation_note))
    for scope, tab in zip(r.scopes, st.tabs(list(r.scopes))):
        with tab:
            st.markdown(f"**P&L vs budget, {scope}**")
            table(variance_rows(r.scope(scope)))
            if scope == APAC:
                st.markdown("**Market summary**")
                table([{"Market": m.code, **row} for m in r.markets
                       for row in variance_rows(r.row(m.code, key) for key in variance.MARKET_SUMMARY_LINES)])
                st.markdown("**EBITDA bridge**")
                st.altair_chart(bridge_chart(r), width="stretch")
                table([{"Step": "Budget EBITDA", "Amount": fmt_money(r.bridge.budget), "Fav / Unfav": ""},
                       *({"Step": s.market, "Amount": fmt_step(s.amount),
                          "Fav / Unfav": r.row(s.market, "ebitda").label} for s in r.bridge.steps),
                       {"Step": "Actual EBITDA", "Amount": fmt_money(r.bridge.actual), "Fav / Unfav": ""}])
            for note in r.fx_notes(scope):
                st.caption(esc(note))

    st.subheader("Drill-down to source rows")
    if r.conversion is not None:
        st.caption(esc(f"Source rows are in {r.reporting_currency}, as published."))
    scope = st.selectbox("Market", r.scopes, key="trace_scope")
    lines = {line.key: line.label for line in variance.TRACEABLE_LINES}
    line_key = st.selectbox("Line or account", list(lines), format_func=lines.get, key="trace_line")
    scenario = st.selectbox("Scenario", ["actual", "budget", "both"], key="trace_scenario")
    t = variance.trace_line(p.store, release_id, scope, line_key, None if scenario == "both" else scenario)
    ccy = r.reporting_currency
    if not t.facts:
        st.write("No facts.")
    else:
        table([{"Scenario": f.scenario, "Market": f.market, "Account": f.account, "File": f.file_name,
                "Row": str(f.source_row), "Local amount": fmt_amount(f.local_amount), "Ccy": f.currency,
                "Rate": plain(f.rate), f"{ccy} amount": fmt_amount(f.reporting_amount),
                "Submission": f.submission_id[:12]} for f in t.facts])
        for sc in sorted({f.scenario for f in t.facts}):
            st.write(f"Total {sc}: {fmt_amount(t.total(sc))} {ccy}")
    *first, last = variance.UNAVAILABLE_DRILL_DOWNS
    st.caption(f"{', '.join(first).capitalize()} and {last} drill-downs: not available in this data.")

    if r.changes is not None:
        st.subheader(f"Changes since {r.previous_release_id}")
        diff_tables(r.changes, p.ws, currency=r.currency)
        for note in r.changes_notes:
            st.caption(esc(note))


# Releases and downloads


CUSTOM_PACK = "Upload a pack file"


def upload_text(upload) -> str:
    try:
        return upload.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{upload.name} is not UTF-8 text") from None


def _content(upload) -> str | None:
    return None if upload is None else hashlib.sha256(upload.getvalue()).hexdigest()


def downloads_key(release_id: str, pack_key: str, pack_upload, commentary_upload, currency: str | None = None) -> tuple:
    """What prepared downloads were made from, so replacing an uploaded file hides them."""
    return (release_id, pack_key, _content(pack_upload) if pack_key == CUSTOM_PACK else None,
            _content(commentary_upload), currency)


def pack_label(config_dir: Path, key: str, markets: list[str]) -> str:
    try:
        return f"{deck_pack.find_pack(config_dir, key, markets).name} ({key})"
    except ConfigError:
        return f"{key} (broken pack file)"


def prepare_downloads(p: Page, release_id: str, commentary_upload, pack_key: str = deck_pack.DEFAULT_KEY,
                      pack_upload=None, currency: str | None = None) -> dict:
    """`pack_key`: a pack in config/deck/packs, or CUSTOM_PACK to use `pack_upload`. `currency`: the deck's
    currency, also added to release.xlsx (default the base currency)."""
    commentary = None
    if commentary_upload is not None:
        commentary = parse_commentary(upload_text(commentary_upload), Path(commentary_upload.name).name)
    config_dir = Path(st.session_state["config_path"]).parent
    markets = [m.code for m in p.ws.config.markets]
    if pack_key != CUSTOM_PACK:
        pack = deck_pack.find_pack(config_dir, pack_key, markets)
    elif pack_upload is None:
        raise ConfigError("Upload a pack file (TOML), or choose one of the packs.")
    else:
        name = Path(pack_upload.name).name
        pack = deck_pack.parse_pack(upload_text(pack_upload), name, Path(name).stem, markets)
    out = Path(tempfile.mkdtemp(prefix=f"{release_id}-", dir=session_dir("downloads")))
    report = variance.release_report(p.store, p.ws, release_id, currency)
    template, style = deck.find_styling(config_dir)
    deck_path = deck.write_deck(report, commentary, out, template=template, style=style, pack=pack)
    facts_path, manifest_path = release.export(p.store, release_id, out)
    xlsx_path = xlsx.export_xlsx(p.store, p.ws, release_id, out, report)
    reasons = deck.draft_reasons(report, commentary, pack)
    return {"key": downloads_key(release_id, pack_key, pack_upload, commentary_upload, currency),
            "converted": f" Amounts in {report.currency_label}." if report.conversion else "",
            "deck_status": "DRAFT: " + "; ".join(reasons) if reasons else "FINAL",
            "files": [str(deck_path), str(xlsx_path), str(facts_path), str(manifest_path)]}


def releases(p: Page) -> None:
    history = p.store.releases(p.period)
    if not history:
        st.write(f"No release yet for {p.period}.")
        return
    current = p.store.current_release(p.period)
    table([{"Release": r.release_id, "Current": "Yes" if r.release_id == current.release_id else "No",
            "Approved by": r.approved_by, "Approved at": when(r.approved_at),
            "Previous": r.previous_release_id or "none", "Checksum": r.facts_checksum[:12]} for r in history])

    release_id = release_picker(p, "Release", "downloads_release")
    if st.button("Verify release"):
        v = release.verify_release(p.store, release_id)
        if v.ok:
            st.success(f"{v.release_id} verifies: facts rebuilt from stored rows, mappings and rates match "
                       f"checksum {v.manifest_checksum[:12]}.")
        else:
            st.error(f"{v.release_id} fails verification:")
            st.markdown("\n".join(f"- {esc(problem)}" for problem in v.problems))

    st.subheader("Downloads")
    config_dir = Path(st.session_state["config_path"]).parent
    markets = [m.code for m in p.ws.config.markets]
    labels = {k: pack_label(config_dir, k, markets) for k in deck_pack.pack_keys(config_dir)}
    pack_key = st.selectbox("Deck pack (config/deck/packs)", [*labels, CUSTOM_PACK], key="deck_pack",
                            format_func=lambda k: labels.get(k, k))
    pack_upload = None
    if pack_key == CUSTOM_PACK:
        pack_upload = st.file_uploader("Pack file (TOML)", type=["toml"], key="pack_upload")
    commentary = st.file_uploader("Outlook and actions (optional commentary TOML)", type=["toml"])
    currency = currency_picker(p, release_id, "downloads_currency")
    if st.button("Prepare downloads"):
        st.session_state["downloads"] = None
        try:
            st.session_state["downloads"] = prepare_downloads(p, release_id, commentary, pack_key, pack_upload,
                                                              currency)
        except READABLE_ERRORS as e:
            st.error(esc(str(e)))
    ready = st.session_state.get("downloads")
    if ready and ready["key"] == downloads_key(release_id, pack_key, pack_upload, commentary, currency):
        st.markdown(esc(f"Deck for {release_id}: {ready['deck_status']}.{ready['converted']}"))
        for i, name in enumerate(ready["files"]):
            path = Path(name)
            st.download_button(esc(f"Download {path.name}"), path.read_bytes(), file_name=path.name, key=f"dl_{i}")

    if len(history) > 1:
        st.subheader("Compare two releases")
        ids = [r.release_id for r in history]
        before = st.selectbox("From", ids, index=len(ids) - 2, key="diff_from")
        after = st.selectbox("To", ids, index=len(ids) - 1, key="diff_to")
        st.write(f"{before} to {after}")
        diff_tables(release.diff_releases(p.store, p.ws, before, after), p.ws)


# Close controls


def queue_action(action: str, item: str) -> None:
    """A form button's callback: remembers which action to run, so it runs before the page reads the state."""
    st.session_state["controls_action"] = (action, item)


def _value(key: str, default=""):
    value = st.session_state.get(key, default)
    return default if value is None else value


def _resolve(p: Page, role: str, item: str, now: datetime | None) -> controls.ActionResult:
    text = _value(f"resolve_amount_{item}")
    try:
        amount = parse_amount(text)
    except ValueError:
        return controls.ActionResult("refused", f"Amount is not a number: {text!r}. Nothing recorded.")
    return controls.resolve(p.store, p.ws, item, _value(f"resolve_kind_{item}"), amount,
                            _value(f"resolve_reason_{item}"), role, _value(f"resolve_approver_{item}"),
                            _value(f"resolve_issue_{item}"), now)


# Each action reads its form's widgets by key and calls the library, which checks the role and refuses or no-ops.
CONTROL_ACTIONS: dict[str, Callable[[Page, str, str, datetime | None], controls.ActionResult]] = {
    "link": lambda p, role, item, now: controls.link(
        p.store, p.ws, item, _value(f"link_to_{item}"), role, override=bool(_value(f"link_override_{item}", False)),
        reason=_value(f"link_reason_{item}"), approved_by=_value(f"link_approver_{item}"), now=now),
    "explain": lambda p, role, item, now: controls.explain(
        p.store, p.ws, item, _value(f"explain_note_{item}"), role, _value(f"explain_approver_{item}"), now),
    "assign": lambda p, role, item, now: controls.assign(p.store, p.ws, item, _value(f"assign_owner_{item}"), role,
                                                         now),
    "resolve": _resolve,
}
ACTION_BOX = {"recorded": st.success, "no_op": st.info, "refused": st.error}


def approver_select(c: controls.Controls, role: str, owner: str | None, key: str) -> None:
    """Blank until chosen; offers only approver roles other than the person acting and the accrual's owner."""
    st.selectbox("Approved by", controls.allowed_approvers(c.cfg, role, owner), index=None,
                 placeholder="Choose an approver", key=key)


def link_form(c: controls.Controls, invoice_id: str, role: str) -> None:
    candidates = c.candidates(invoice_id)
    override = st.checkbox("Override: link an accrual that is not a candidate", key=f"link_override_{invoice_id}")
    if override or not candidates:
        choices = {ch.accrual_id: ch for ch in controls.override_choices(c, invoice_id)}
        label = {i: f"{i} (candidate)" if ch.candidate else i if not ch.mismatches else f"{i} (differs)"
                 for i, ch in choices.items()}
        chosen = st.selectbox("Link to accrual", list(choices), index=None, placeholder="Choose an accrual",
                              format_func=label.get, key=f"link_to_{invoice_id}")
        if chosen and choices[chosen].mismatches:
            st.warning(esc(f"{chosen} differs from {invoice_id}: {'; '.join(choices[chosen].mismatches)}."))
    else:
        st.selectbox("Link to accrual", list(candidates), index=None, placeholder="Choose an accrual",
                     key=f"link_to_{invoice_id}")
    if override:
        st.text_input("Reason for the override", key=f"link_reason_{invoice_id}")
        approver_select(c, role, None, f"link_approver_{invoice_id}")
    st.button(f"Link {invoice_id}", key=f"link_{invoice_id}", on_click=queue_action, args=("link", invoice_id))


def explain_form(c: controls.Controls, accrual_id: str, role: str) -> None:
    s = c.accrual(accrual_id)
    with st.form(f"explain_form_{accrual_id}", border=False):
        st.text_input("Explanation", key=f"explain_note_{accrual_id}")
        if controls.explain_needs_approval(c.cfg, s):
            st.caption(esc(f"The variance is over {fmt_money(c.cfg.explain_approval_abs)}, so the explanation "
                           "needs an approver."))
            approver_select(c, role, s.owner, f"explain_approver_{accrual_id}")
        st.form_submit_button(f"Explain {accrual_id}", key=f"explain_{accrual_id}", on_click=queue_action,
                              args=("explain", accrual_id))


def assign_form(c: controls.Controls, accrual_id: str) -> None:
    st.selectbox("New owner", c.cfg.owners, key=f"assign_owner_{accrual_id}")
    st.button(f"Assign {accrual_id}", key=f"assign_{accrual_id}", on_click=queue_action, args=("assign", accrual_id))


def resolve_form(c: controls.Controls, s: controls.Signal, role: str) -> None:
    """A form, so typing in it does not rerun the page and close the expander."""
    item, issue = s.item, controls.SIGNAL_ISSUE[s.type]
    st.session_state[f"resolve_issue_{item}"] = issue
    with st.expander(f"Record a resolution for {item} ({issue})"), st.form(f"resolve_form_{item}", border=False):
        st.selectbox("Kind", controls.ISSUE_RULES[issue].kinds, key=f"resolve_kind_{item}")
        st.session_state.setdefault(f"resolve_amount_{item}", plain(s.amount_at_risk))
        st.text_input("Amount (must equal the amount at risk)", key=f"resolve_amount_{item}")
        st.text_input("Reason", key=f"resolve_reason_{item}")
        approver_select(c, role, c.accrual(item).owner, f"resolve_approver_{item}")
        st.form_submit_button(f"Resolve {item}", key=f"resolve_{item}", on_click=queue_action,
                              args=("resolve", item))


def next_action_note(c: controls.Controls, s: controls.Signal, role: str) -> None:
    st.caption(esc(f"Not closed by a resolution: {controls.ISSUE_RULES[controls.SIGNAL_ISSUE[s.type]].instead}."))


# Which forms each signal card offers.
SIGNAL_FORMS: dict[str, Callable[[controls.Controls, controls.Signal, str], None]] = {
    "INVOICE_VARIANCE": lambda c, s, role: explain_form(c, s.item, role),
    "PARTIAL_INVOICE": resolve_form,
    "MISSING_REVERSAL": resolve_form,
    "REVERSAL_MISMATCH": next_action_note,
    "AMBIGUOUS_MATCH": lambda c, s, role: link_form(c, s.item, role),
    "REFERENCE_MISMATCH": lambda c, s, role: link_form(c, s.item, role),
    "NO_OWNER": lambda c, s, role: assign_form(c, s.item),
}


def signal_card(c: controls.Controls, i: int, s: controls.Signal, role: str) -> None:
    with st.container(border=True):
        st.markdown(f"**{i}. {esc(f'{words(s.type)}: {s.item}')}**")
        st.markdown("\n".join("- " + esc(line) for line in [
            f"Owner: {s.owner}",
            f"Amount at risk: {c.currency(s.market)} {fmt_money(s.amount_at_risk)}",
            f"Trigger: {s.trigger}",
            *(f"Evidence: {e}" for e in s.evidence),
            f"Next action: {s.next_action}",
        ]))
        SIGNAL_FORMS[s.type](c, s, role)


def queue_tabs(c: controls.Controls) -> dict[str, tuple[str, ...]]:
    """Tab label -> the roles whose signals it shows: every Finance owner together, then AP, Accounting, lead."""
    cfg = c.cfg
    fixed = (cfg.ap_role, cfg.accounting_role, cfg.close_lead)
    return {"Finance owners": tuple(r for r in cfg.owners if r not in fixed),
            **{role: (role,) for role in fixed}}


def pending_rows(entries) -> list[dict]:
    return [{"Accrual": e.accrual_id, "Period": e.period, "Kind": e.kind, "Dr": e.debit, "Cr": e.credit,
             "P&L effect": fmt_step(e.pl_effect), "Approved by": e.resolution.data["approved_by"]} for e in entries]


def accrual_detail(c: controls.Controls, accrual_id: str) -> None:
    s = c.accrual(accrual_id)
    a, ccy = s.accrual, c.currency(s.accrual.market)
    lines = [
        f"{a.accrual_id}: {a.market} {a.period}, {a.account}, {a.supplier}, {ccy} {fmt_money(a.amount)}",
        f"Description: {a.description}",
        f"Owner: {s.owner or 'none'}",
        f"State: invoice {words(s.invoice_state)}, reversal {words(s.reversal_state)}, overall {words(s.overall)}",
        f"Invoiced {fmt_money(s.invoiced)} vs accrued {fmt_money(a.amount)}: difference {fmt_step(s.difference)}, "
        f"tolerance {fmt_money(s.tolerance)}",
    ]
    if s.explained:
        lines.append(f"Variance explained by {s.explained.by}: {s.explained.data['note']}")
    lines += [f"Resolution: {controls.resolution_text(r)}" for r in s.resolutions]
    lines += [f"{lapse.what}: {'; '.join(lapse.changes)}" for lapse in s.lapsed]
    st.markdown("\n".join("- " + esc(line) for line in lines))
    st.markdown("**Evidence**")
    table([dict(zip(["Kind", "ID", "File", "Row", "Amount", "Note"], r)) for r in controls.evidence_rows(s)])
    preview = controls.journal_preview(s)
    st.markdown(f"**Journal preview ({ccy})**")
    table([{"Period": e.period, "Entry": e.label, "Dr": e.debit, "Cr": e.credit, "Amount": fmt_money(e.amount),
            "Evidence": e.evidence} for e in preview.entries])
    st.write(controls.net_text(preview.net))
    if pending := controls.pending_entries(s):
        st.markdown(f"**{controls.PENDING_HEADING} ({ccy})**")
        table(pending_rows(pending))
    st.markdown("**History**")
    table([{"When": when(h.at), "What": h.what, "By": h.by, "Detail": h.detail}
           for h in controls.history(c, accrual_id)])


def controls_preview(p: Page, kind: str, upload) -> None:
    """How the file will be read, before anything is imported."""
    try:
        r = controls.read(kind, upload.getvalue(), Path(upload.name).name, p.ws)
    except controls.ControlsError as e:
        st.warning(esc(f"{upload.name} cannot be read: {e}"))
        return
    preview = controls.preview(r)
    st.markdown("\n".join("- " + esc(line) for line in preview.lines))
    table(list(preview.columns))
    if preview.rows:
        st.markdown(esc(preview.rows_heading))
        table(list(preview.rows))
    for warning in preview.warnings:
        st.warning(esc(warning))
    (st.markdown if preview.imports else st.warning)(esc(preview.verdict))


def import_controls(p: Page, role: str, now: datetime | None) -> None:
    with st.expander("Import accruals, invoices and GL extracts (CSV or Excel)"):
        st.caption("Templates with an Instructions sheet. Files laid out another way need a mapping in "
                   "config/controls/mappings.")
        for kind in controls.KINDS:
            template_download(kind, p.ws)
        uploads = {kind: st.file_uploader(label, type=list(controls.UPLOAD_TYPES), key=f"controls_upload_{kind}")
                   for kind, label in (("accruals", "Accruals"), ("invoices", "Invoices"), ("gl", "GL extract"))}
        for kind, upload in uploads.items():
            if upload is not None:
                controls_preview(p, kind, upload)
        allow_drop = st.checkbox("Allow dropping accruals or invoices that carry evidence (recorded as you)",
                                 key="controls_allow_drop")
        reason = st.text_input("Reason for dropping", key="controls_drop_reason") if allow_drop else ""
        if st.button("Import", key="controls_import"):
            files = {kind: (Path(u.name).name, u.getvalue()) for kind, u in uploads.items() if u is not None}
            if not files:
                st.error("Choose at least one file to import.")
                return
            try:
                results = controls.import_files(p.store, p.ws, files, now, allow_drop=allow_drop, reason=reason,
                                                by=role)
            except controls.ControlsError as e:
                st.error(esc(f"Import refused. Nothing imported. {e}"))
                return
            st.session_state.pop("demo_notice", None)
            for r in results:
                (st.info if r.outcome == "repeat" else st.success)(esc(r.message))
                if r.changes:
                    st.markdown("\n".join("- " + esc(change) for change in r.changes))


def controls_heading(c: controls.Controls) -> str:
    """The imported periods, not the sidebar period: controls span the accrual period and the next."""
    current = controls.current_imports(c.imports)
    labels = {"accruals": "accruals", "invoices": "invoices", "gl": "GL"}
    scope = ", ".join(f"{labels[kind]} {period}" for kind, period in current)
    return f"Close controls: {scope}" if scope else "Close controls"


def pl_table(c: controls.Controls, market: str, period: str) -> None:
    lines = controls.pl_expense(c, market, period)
    pending = controls.pl_pending(c, market, period)
    ledger = fmt_money(sum((l.net for l in lines), controls.ZERO))
    text = f"P&L expense {market} {period} from these items ({c.currency(market)}): {ledger}"
    if pending:
        text += (f"; {fmt_money(sum((l.after_pending for l in lines), controls.ZERO))} after pending entries "
                 "not yet in the ledger")
    st.markdown(esc(text))
    table([{"Item": l.item, "Invoices": fmt_money(l.invoices), "Reversals": fmt_money(l.reversals),
            "Net": fmt_money(l.net),
            **({"Pending (not in ledger)": fmt_money(l.pending), "Net after pending": fmt_money(l.after_pending)}
               if pending else {})} for l in lines])
    if pending:
        st.markdown(f"**{controls.PENDING_HEADING}**")
        table(pending_rows(pending))


def close_controls(p: Page) -> None:
    cfg = p.ws.config.require_controls()
    heading = st.empty()
    role = st.selectbox("Acting as", cfg.owners, key="controls_role")
    st.caption("Roles are what you assert here, not a login: nothing checks who you are.")
    as_of = parse_when(st.text_input("As of (YYYY-MM-DD HH:MM, blank for now)", key="as_of"), "As of")
    if pending := st.session_state.pop("controls_action", None):
        action, item = pending
        result = CONTROL_ACTIONS[action](p, role, item, as_of)
        if result.outcome == "recorded":
            st.session_state.pop("demo_notice", None)
        ACTION_BOX[result.outcome](esc(result.message))
    import_controls(p, role, as_of)
    c = controls.load(p.store, p.ws, as_of)
    heading.header(controls_heading(c))
    if not c.accruals and not c.imports:
        st.write("No accruals imported yet. Import the files above, or load the close controls demo.")
        return
    counts = c.counts()
    st.write(f"As of {when(c.as_of)}. {len(c.signals)} signal{'s' * (len(c.signals) != 1)} need attention. "
             "Accruals: " + ", ".join(f"{counts[o]} {o}" for o in controls.OVERALL) + ".")
    tabs = queue_tabs(c)
    by_tab = {label: [s for s in c.signals if s.owner in roles] for label, roles in tabs.items()}
    for (label, sigs), tab in zip(by_tab.items(), st.tabs([f"{label} ({len(v)})" for label, v in by_tab.items()])):
        with tab:
            if not sigs:
                st.write("Nothing in this queue.")
            for i, s in enumerate(sigs, 1):
                signal_card(c, i, s, role)
            if label == cfg.ap_role:
                for m in (m for m in c.unmatched if not m.candidates and not m.referenced):
                    with st.container(border=True):
                        st.markdown(esc(f"Unmatched invoice {m.invoice.invoice_id}: {m.invoice.supplier}, "
                                        f"{c.currency(m.invoice.market)} {fmt_money(m.invoice.amount)}, "
                                        f"{m.invoice.source}. No candidate accrual."))
                        link_form(c, m.invoice.invoice_id, role)

    st.subheader("Accruals")
    table([{"Accrual": s.accrual_id, "Market": s.accrual.market, "Period": s.accrual.period,
            "Account": s.accrual.account, "Accrued": fmt_money(s.accrual.amount), "Invoiced": fmt_money(s.invoiced),
            "Invoice state": words(s.invoice_state), "Reversal": words(s.reversal_state),
            "Overall": words(s.overall), "Owner": s.owner or "none"} for s in c.accruals])
    for market, period in controls.pl_scopes(c):
        pl_table(c, market, period)

    st.subheader("Accrual detail")
    if c.accruals:
        accrual_id = st.selectbox("Accrual", [s.accrual_id for s in c.accruals], key="controls_detail")
        accrual_detail(c, accrual_id)


# Planning: reads a plan file only; close.toml only to export a plan month as a budget submission


DEFAULT_PLAN = "plans/agency_2027.toml"
BASE = "base"


ROW_PX = 35  # Streamlit's dataframe row height, header included


def table_height(n_rows: int) -> int:
    """Tall enough for every row and the header, so a statement never scrolls inside the page."""
    return ROW_PX * (n_rows + 1) + 3


def months_table(lines: tuple[planning.Line, ...], months, years, schedules: dict[str, str] | None = None) -> None:
    """A statement or schedule by month (months: the plan's first months, or all of them). Line and the fiscal
    years come first and are pinned, so they stay in view while the months scroll sideways; every column but
    the line is a number."""
    rows = []
    for x in lines:
        fmt = fmt_quantity if x.quantity else fmt_money
        row = {"Line": x.label, **{y.heading: "" if (v := x.for_year(y)) is None else fmt(v) for y in years},
               **{m.label: fmt(v) for m, v in zip(months, x.values)}}
        if schedules is not None:
            row["Schedule"] = schedules.get(x.schedule, "")
        rows.append(row)
    config = {c: st.column_config.TextColumn(alignment="right") for c in (m.label for m in months)}
    config["Line"] = st.column_config.TextColumn(pinned=True, width="medium")
    config |= {y.heading: st.column_config.TextColumn(alignment="right", pinned=True) for y in years}
    st.dataframe(rows, hide_index=True, column_config=config, height=table_height(len(rows)))


def plan_checks(m: planning.Model) -> None:
    st.subheader("Checks")
    failed = [c for c in m.checks if c.passed is False]
    if failed:
        st.error(f"{len(failed)} check{'s' * (len(failed) != 1)} failed. The figures are shown as calculated, "
                 "not adjusted.")
    elif m.pnl_only_reason is not None:
        st.write("Balance sheet checks not run (P&L only).")
    elif m.cash_shortfall:
        st.warning("Checks pass; cash warning. The balance sheet balances and cash ties in every month, but cash "
                   "goes below zero (see Lowest month-end cash above).")
    else:
        st.success("All checks pass: the balance sheet balances and cash ties in every month.")
    table([{"Check": c.name, "Result": c.status.capitalize(), "Detail": c.detail} for c in m.checks])
    for w in m.warnings:
        if m.cash_shortfall is None or m.cash_shortfall not in w:  # the shortfall sits beside the lowest cash
            st.write(esc(w))


def plan_compare(plan, base: planning.Model, m: planning.Model, months) -> None:
    if m.scenario == BASE:
        st.write(f"Choose a scenario to compare with base. Scenarios in this plan: "
                 f"{esc(', '.join(plan.scenario_names) or 'none')}.")
        return
    c = planning.compare(base, m)
    rows = [planning.Line(f"{r.label}, {name}", line.values, annual=line.annual)
            for r in c.rows for name, line in ((BASE, r.base), (m.scenario, r.scenario), ("change", r.change))]
    st.caption("A fiscal-year column is the annual total for P&L lines and the closing month for balances.")
    months_table(tuple(rows), months, m.years)
    st.write(esc(f"Lowest month-end cash: base {planning.lowest_text(base)}; {m.scenario} {planning.lowest_text(m)}."))


def plan_drill(m: planning.Model) -> None:
    st.subheader("Drill down")
    options = {f"{s.title}: {x.label}": x for s in m.statements for x in s.lines if x.schedule}
    chosen = st.selectbox("Statement line", list(options), key="plan_drill")
    if chosen is None:
        return
    sched = m.schedule(options[chosen].schedule)
    st.markdown(esc(f"{chosen} comes from the {sched.title} schedule."))
    st.caption(esc(f"Driven by: {sched.basis}"))
    months_table(sched.lines, m.months, m.years)  # every month, whatever the statements show


def lowest_cash(m: planning.Model) -> None:
    ccy = m.plan.setup.currency
    metric, note = st.columns([1, 2])
    metric.metric(f"Lowest month-end cash ({ccy}, {m.lowest_cash[0].label})", fmt_money(m.lowest_cash[1]))
    if m.cash_shortfall:
        note.warning(esc(m.cash_shortfall))
    if len(m.years) > 1:
        for col, (fy, month, cash) in zip(st.columns(len(m.years)), m.lowest_cash_by_year):
            col.metric(f"Lowest in {fy.label} ({month.label})", fmt_money(cash))


def budget_export(m: planning.Model) -> None:
    """One plan month as the [budget] market's budget submission, for download."""
    st.subheader("Export as budget")
    plan = m.plan
    if plan.budget is None:
        st.write(esc(f"Plan {plan.name} has no [budget] section. Add one naming the market and the regional "
                     "account for each P&L line above EBIT to export a month as a budget submission."))
        return
    period = st.selectbox("Period", [str(x) for x in m.months], key="plan_budget_period",
                          format_func=lambda p: Month.parse(p).label)
    st.caption(esc(f"Scenario {m.scenario} (the scenario chosen above). The market's currency and the chart of "
                   f"accounts come from {st.session_state['config_path']} (Config file in the sidebar)."))
    try:
        ws = load_workspace(Path(st.session_state["config_path"]))
        sub = plan_budget.budget_submission(m, period, ws)
    except (ConfigError, PlanError) as e:
        st.error(esc(str(e)))
        return
    table([{"Account": r.account, "Plan line": r.line, "Amount": fmt_money(r.amount)} for r in sub.rows])
    st.caption(esc(f"{plan_budget.SIGN_NOTE} {sub.left_out_text}"))
    st.download_button(esc(f"Download {sub.file_name}"), sub.xlsx_bytes(ws.coa), file_name=sub.file_name,
                       key="plan_budget_download")
    st.caption(esc(f"Ingest it as {sub.market} budget {sub.period} on Submit a file, with {sub.market}'s mapping "
                   "pointing at mappings/_regional_template.toml."))


def planning_page() -> None:
    st.header("Planning")
    path = st.text_input("Plan file", key="plan_path")
    try:
        plan = load_plan(Path(path))
    except PlanError as e:
        st.error(esc(f"{path}: {len(e.problems)} problem{'s' * (len(e.problems) != 1)}"))
        for p in e.problems:
            st.error(esc(p))
        return
    scenario = st.selectbox("Scenario", [BASE, *plan.scenario_names], key="plan_scenario")
    base = planning.run(plan)
    m = base if scenario == BASE else planning.run(plan, scenario)
    if m.changes:
        st.caption(esc(f"Changes against base: {'; '.join(map(str, m.changes))}"))
    s = m.plan.setup
    st.markdown(esc(f"{s.entity}: plan {plan.name} (version {plan.version}), scenario {m.scenario}, "
                    f"{s.currency}, {m.months[0].label} to {m.months[-1].label}."))
    part = part_years_text(m.years)
    if part:
        st.caption(esc(part))
    if m.pnl_only_reason:
        st.warning(esc(m.pnl_only_reason))
    if m.lowest_cash is not None:
        lowest_cash(m)
    plan_checks(m)
    months = m.months
    if len(m.months) > len(m.year_one):
        every = st.checkbox("Show every month", key="plan_all_months")
        months = m.months if every else m.year_one
        if not every:
            st.caption(esc(f"By month: {months[0].label} to {months[-1].label}, with a column per fiscal year. "
                           "Tick Show every month, or drill down, for the later months."))
    titles = {x.key: x.title for x in m.schedules}
    statements = [(st_.title, st_) for st_ in m.statements]
    tabs = st.tabs([*(t for t, _ in statements), "Scenario vs base"])
    for (title, statement), tab in zip(statements, tabs):
        with tab:
            closing = ", ".join(f"{y.label} = {y.months[-1].label}" for y in m.years)
            note = f", month-end; {closing}" if statement is m.balance_sheet else ""
            st.caption(f"{s.currency}{note}")
            months_table(statement.lines, months, m.years, titles)
    with tabs[-1]:
        plan_compare(plan, base, m, months)
    plan_drill(m)
    budget_export(m)


PAGES: dict[str, Callable[[Page], None]] = {
    "Close board": close_board,
    "Submit a file": submit,
    "Review and publish": review,
    "Results": results,
    "Releases and downloads": releases,
    "Close controls": close_controls,
}
# Pages that write their own heading and do not use the sidebar period.
OWN_HEADING = {"Close controls"}
# Pages that do not open the store (Planning reads close.toml only for its budget export).
STANDALONE: dict[str, Callable[[], None]] = {"Planning": planning_page}


# Sidebar


def last_month() -> str:
    today = date.today()
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return f"{year}-{month:02d}"


def load_demo() -> None:
    step_id = st.session_state["demo_step"]
    try:
        workdir = Path(tempfile.mkdtemp(prefix="fct-demo-"))
        config = demo.prepare(workdir)
        with Store.open(workdir / "store.duckdb") as store:
            step = demo.replay(store, workdir, step_id)
    except READABLE_ERRORS as e:
        st.session_state["demo_notice"] = ("error", f"Demo not loaded: {e}")
        return
    st.session_state.update(store_path=str(workdir / "store.duckdb"), config_path=str(config),
                            period=demo.PERIOD, as_of=when(step.as_of), downloads=None)
    st.session_state["demo_notice"] = ("ok", f"Demo loaded up to {step.id}: {step.title}. "
                                             f"Store {workdir / 'store.duckdb'}.")


def load_controls_demo() -> None:
    step_id = st.session_state["controls_demo_step"]
    try:
        workdir = Path(tempfile.mkdtemp(prefix="fct-controls-demo-"))
        config = demo.prepare(workdir)
        with Store.open(workdir / "store.duckdb") as store:
            step = demo.replay_controls(store, load_workspace(config), step_id)
    except READABLE_ERRORS as e:
        st.session_state["demo_notice"] = ("error", f"Close controls demo not loaded: {e}")
        return
    st.session_state.update(store_path=str(workdir / "store.duckdb"), config_path=str(config), page="Close controls",
                            as_of="")
    st.session_state["demo_notice"] = ("ok", f"Close controls demo loaded up to {step.id}: {step.title}. "
                                             f"Store {workdir / 'store.duckdb'}.")


def sidebar():
    """Returns the chosen page and the place for the demo notice. main fills it after the page ran,
    so ingesting or publishing, which clears the notice, removes it in the same run."""
    # `fct ui --store/--config/--fx-dir` passes its options through the environment.
    defaults = {"store_path": os.environ.get("FCT_STORE", str(DEFAULT_STORE)),
                "config_path": os.environ.get("FCT_CONFIG", "config/close.toml"), "period": last_month(),
                "as_of": "", "plan_path": DEFAULT_PLAN, "demo_step": demo.STEP_IDS[-1], "controls_demo_step": demo.CONTROL_STEP_IDS[0]}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    # Streamlit drops a widget's value on runs that don't draw it; reassigning keeps As of across pages.
    st.session_state["as_of"] = st.session_state["as_of"]
    st.session_state["plan_path"] = st.session_state["plan_path"]
    with st.sidebar:
        st.title("APAC close")
        page = st.radio("Page", [*PAGES, *STANDALONE], key="page")
        st.text_input("Period (YYYY-MM)", key="period")
        st.text_input("Store file", key="store_path")
        st.text_input("Config file (close.toml)", key="config_path")
        with st.expander("Demo"):
            steps = {s.id: s.title for s in demo.STEPS}
            st.selectbox("Load demo up to step", demo.STEP_IDS, key="demo_step",
                         format_func=lambda i: f"{i}  {steps[i]}")
            st.button("Load demo", on_click=load_demo)
            controls_steps = {s.id: s.title for s in demo.CONTROL_STEPS}
            st.selectbox("Close controls demo up to step", demo.CONTROL_STEP_IDS, key="controls_demo_step",
                         format_func=lambda i: f"{i}  {controls_steps[i]}")
            st.button("Load close controls demo", on_click=load_controls_demo)
            notice_box = st.empty()
    return page, notice_box


def run_page(page: str) -> None:
    if page in STANDALONE:
        try:
            STANDALONE[page]()
        except (*READABLE_ERRORS, PlanError) as e:
            st.error(esc(str(e)))
        except Exception as e:
            st.error(esc(f"Unexpected error ({type(e).__name__}): {e}"))
        return
    period = st.session_state["period"]
    if page not in OWN_HEADING:
        try:
            validation.check_period(period)  # before the period appears in any heading
        except ValueError as e:
            st.header(page)
            st.error(esc(str(e)))
            return
        st.header(f"{page}, {period}")
    try:
        fx_dir = os.environ.get("FCT_FX_DIR")
        ws = load_workspace(Path(st.session_state["config_path"]), Path(fx_dir) if fx_dir else None)
        with Store.open(st.session_state["store_path"]) as store:
            PAGES[page](Page(ws, store, period))
    except READABLE_ERRORS as e:
        st.error(esc(str(e)))
    except Exception as e:  # a readable line instead of a traceback, whatever the library raised
        st.error(esc(f"Unexpected error ({type(e).__name__}): {e}"))


def main() -> None:
    st.set_page_config(page_title="APAC close", layout="wide")
    page, notice_box = sidebar()
    run_page(page)
    if notice := st.session_state.get("demo_notice"):
        (notice_box.success if notice[0] == "ok" else notice_box.error)(esc(notice[1]))


main()
