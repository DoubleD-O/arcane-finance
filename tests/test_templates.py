"""Input templates (fct templates write) and the mappings that read differently laid-out files
(docs/design-templates.md, Part B)."""

import csv
import shutil
import tomllib
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import REPO
from openpyxl import Workbook, load_workbook

from finance_close_toolkit import cli, controls, demo, validation
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.controls import Source
from finance_close_toolkit.money import fmt_money
from finance_close_toolkit.store import Store
from finance_close_toolkit.templates import TEMPLATES, TYPE_LABELS

EX = REPO / "examples" / "controls"
LATER = datetime(2026, 6, 1)


@pytest.fixture
def root(tmp_path) -> Path:
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.copytree(REPO / "fx", tmp_path / "fx")
    return tmp_path


def open_store(path: Path):
    return Store.open(path)


# Templates


def test_templates_write_produces_every_template_with_its_instructions(tmp_path, capsys):
    assert cli.main(["templates", "write", "--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "accruals_template.xlsx", "gl_template.xlsx", "invoices_template.xlsx", "submission_template.xlsx"]
    for t in TEMPLATES.values():
        wb = load_workbook(tmp_path / t.file_name())
        assert wb.sheetnames == [t.sheet, "Instructions"]
        data = list(wb[t.sheet].iter_rows(values_only=True))
        assert data == [t.names], "the data sheet is the header row only"
        notes = [r for r in wb["Instructions"].iter_rows(values_only=True)]
        for c in t.columns:
            assert (c.name, TYPE_LABELS[c.type], "yes" if c.required else "no", c.description, c.sign or None,
                    c.example) in [r[:6] for r in notes]
        n = len(t.names)
        assert [r[:n] for r in notes[-2:]] == [t.names, tuple(c.example for c in t.columns)], "ends with an example row"
        cells = [cell for ws in wb for row in ws.iter_rows() for cell in row]
        assert not [cell.coordinate for cell in cells if cell.data_type == "f"], "no formulas"
    assert "submission_template.xlsx" in capsys.readouterr().out


def test_templates_write_csv(tmp_path):
    assert cli.main(["templates", "write", "--out", str(tmp_path), "--csv"]) == 0
    for t in TEMPLATES.values():
        assert (tmp_path / t.file_name("csv")).read_text(encoding="utf-8") == ",".join(t.names) + "\n"
        with open(tmp_path / f"{t.kind}_instructions.csv", newline="", encoding="utf-8") as f:
            first = [r[0] if r else "" for r in csv.reader(f)]
        start = first.index("Column")
        assert first[start:start + len(t.names) + 1] == ["Column", *t.names], "the column table"


def test_text_that_looks_like_a_formula_is_written_as_text(monkeypatch, tmp_path):
    from finance_close_toolkit import templates
    t = templates.TEMPLATES["gl"]
    evil = templates.Column("description", "text", False, "=HYPERLINK(\"http://x\")", "=1+1")
    monkeypatch.setitem(templates.TEMPLATES, "gl", templates.InputTemplate(t.kind, t.sheet, t.what, (*t.columns[:-1], evil)))
    path = tmp_path / "gl.xlsx"
    coa = load_workspace(REPO / "config" / "close.toml").coa
    path.write_bytes(templates.workbook_bytes(templates.TEMPLATES["gl"], coa))
    notes = load_workbook(path)["Instructions"]
    values = [c.value for row in notes.iter_rows() for c in row if c.value]
    assert "'=1+1" in values and all(not (isinstance(v, str) and v.startswith("=")) for v in values)


def test_the_regional_template_mapping_reads_the_submission_template():
    raw = tomllib.loads((REPO / "config" / "mappings" / "_regional_template.toml").read_text(encoding="utf-8"))
    t = TEMPLATES["submission"]
    lay = raw["layout"]
    assert (lay["format"], lay["sheet"], lay["header_row"]) == ("xlsx", t.sheet, 1)
    assert (lay["period_column"], lay["account_column"], lay["description_column"], lay["amount_column"]) == t.names
    chart = tomllib.loads((REPO / "config" / "chart_of_accounts.toml").read_text(encoding="utf-8"))["accounts"]
    assert raw["accounts"] == {code: code for code in chart}
    assert "market" not in raw


SG_OCTOBER = [  # examples/2026-10/sg_actual_v1.csv in regional accounts, as on the P&L
    ("REV", "Product revenue", 800000), ("REV", "Services revenue", 200000), ("COGS", "Cost of sales", 450000),
    ("PAY", "Salaries and wages", 250000), ("RENT", "Office rent", 60000), ("MKT", "Marketing", 40000),
    ("OTH", "Other expenses", 30000), ("DEP", "Depreciation", 20000),
]


def test_sg_october_on_the_regional_template_gives_the_same_validated_facts(root, tmp_path):
    config = root / "config" / "close.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace('mapping = "mappings/sg.toml"', 'mapping = "mappings/_regional_template.toml"'),
                      encoding="utf-8")
    cli.main(["templates", "write", "--out", str(tmp_path / "t")])
    filled = tmp_path / "sg_october.xlsx"
    wb = load_workbook(tmp_path / "t" / "submission_template.xlsx")
    for account, description, amount in SG_OCTOBER:
        wb["Submission"].append(["2026-10", account, description, amount])
    wb.save(filled)

    received = datetime(2026, 11, 4, 10)
    with open_store(tmp_path / "template.duckdb") as s1, open_store(tmp_path / "csv.duckdb") as s2:
        regional = validation.ingest(s1, load_workspace(config), filled, "SG", "2026-10", "actual", received)
        canonical = validation.ingest(s2, load_workspace(REPO / "config" / "close.toml"),
                                      REPO / "examples" / "2026-10" / "sg_actual_v1.csv", "SG", "2026-10", "actual",
                                      received)
    assert regional.validation.state == canonical.validation.state == "validated"
    assert regional.validation.exceptions == ()
    assert regional.validation.facts == canonical.validation.facts


# Close-controls files laid out by another system

RENAMED = {
    "accruals": {"accrual_id": "Accrual ref", "market": "Entity", "period": "Accrual period", "account": "GL account",
                 "supplier": "Vendor name", "reference": "PO number", "amount": "Accrued amount",
                 "owner": "Preparer", "description": "Memo"},
    "invoices": {"invoice_id": "Invoice no", "market": "Entity", "supplier": "Vendor name",
                 "invoice_date": "Invoice date", "period": "Posting period", "account": "GL account",
                 "reference": "PO number", "amount": "Net amount"},
    "gl": {"journal_id": "Journal", "period": "Fiscal period", "market": "Company", "account": "Account",
           "amount": "Amount", "reference": "Reference", "description": "Line text"},
}
TITLE_ROWS = 3  # report title, run time and a blank row above the header


def export_xlsx(csv_path: Path, out: Path, kind: str, negate: bool = False) -> Path:
    """A canonical CSV as an ERP export: renamed, reversed columns plus a column we do not read, title rows above
    the header, and amounts as text like 1,234.50 and (1,234.50)."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    columns = list(reversed(RENAMED[kind]))
    wb = Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append([f"Harbour ERP: {kind} export"])
    ws.append(["Run 2026-05-04 08:00"])
    ws.append([])
    ws.append(["Batch", *(RENAMED[kind][c] for c in columns)])
    for r in rows:
        amount = Decimal(r["amount"])
        ws.append(["B7", *(fmt_money(-amount if negate else amount) if c == "amount" else r[c] for c in columns)])
    wb.save(out)
    return out


def mapping_toml(kind: str, negate: bool = False, **overrides: str) -> str:
    columns = {**RENAMED[kind], **overrides}
    return "\n".join([
        f'amount_sign = "{"negate" if negate else "as_is"}"', "", "[layout]", 'format = "xlsx"', 'sheet = "Export"',
        f"header_row = {TITLE_ROWS + 1}", "", "[layout.columns]", *(f'{c} = "{h}"' for c, h in columns.items()), ""])


@pytest.fixture
def erp(root, tmp_path):
    """Default mappings for the ERP layout in config/controls/mappings, and the exported files; the second GL
    export shows amounts credit-positive, so it is imported with an override mapping that negates."""
    folder = root / "config" / "controls" / "mappings"
    folder.mkdir(parents=True)
    for kind in controls.KINDS:
        (folder / f"{kind}.toml").write_text(mapping_toml(kind), encoding="utf-8")
    (tmp_path / "gl_negate.toml").write_text(mapping_toml("gl", negate=True), encoding="utf-8")
    return {
        "accruals": export_xlsx(EX / "accruals_2026-03.csv", tmp_path / "accruals_export.xlsx", "accruals"),
        "invoices": export_xlsx(EX / "invoices_2026-04.csv", tmp_path / "invoices_export.xlsx", "invoices"),
        "gl": export_xlsx(EX / "gl_2026-04.csv", tmp_path / "gl_export.xlsx", "gl"),
        "gl_v2": export_xlsx(EX / "gl_2026-04_v2.csv", tmp_path / "gl_export_v2.xlsx", "gl", negate=True),
        "gl_negate": tmp_path / "gl_negate.toml",
    }


def summary(c: controls.Controls) -> tuple:
    states = {s.accrual_id: (s.invoiced, s.invoice_state, s.reversal_state, s.overall, s.owner,
                             tuple(sorted(s.open_issues)), tuple((l.invoice.invoice_id, l.method) for l in s.links))
              for s in c.accruals}
    signals = [(s.type, s.item, s.owner, s.amount_at_risk, s.next_action) for s in c.signals]
    pl = {scope: controls.pl_expense(c, *scope) for scope in controls.pl_scopes(c)}
    pending = {scope: [(e.accrual_id, e.kind, e.pl_effect) for e in controls.pl_pending(c, *scope)]
               for scope in controls.pl_scopes(c)}
    return states, signals, pl, pending


def replay_erp(store: Store, ws, erp: dict, until: str) -> None:
    """The C1-C9 story with the ERP exports in place of the canonical CSVs."""
    for step in demo.CONTROL_STEPS[: demo.CONTROL_STEP_IDS.index(until) + 1]:
        if step.id == "C1":
            files = {kind: (erp[kind].name, erp[kind].read_bytes()) for kind in controls.KINDS}
            controls.import_files(store, ws, files, step.at)
        elif step.id == "C7":
            negate = controls.load_mapping(erp["gl_negate"], "gl")
            controls.import_files(store, ws, {"gl": (erp["gl_v2"].name, erp["gl_v2"].read_bytes())}, step.at,
                                  mappings={"gl": negate})
        else:
            step.action(store, ws, step.at)


@pytest.mark.parametrize("step", ["C1", "C6", "C9"])
def test_the_c1_story_from_erp_exports_matches_the_canonical_csvs(root, tmp_path, erp, step):
    ws_erp = load_workspace(root / "config" / "close.toml")
    ws_csv = load_workspace(REPO / "config" / "close.toml")
    with open_store(tmp_path / "erp.duckdb") as s1, open_store(tmp_path / "csv.duckdb") as s2:
        replay_erp(s1, ws_erp, erp, step)
        demo.replay_controls(s2, ws_csv, step)
        from_erp, from_csv = controls.load(s1, ws_erp, LATER), controls.load(s2, ws_csv, LATER)
    assert summary(from_erp) == summary(from_csv)
    assert from_erp.signals or step == "C9"
    a001 = from_erp.accrual("A-001")
    assert a001.accrual.source == Source("accruals_export.xlsx", TITLE_ROWS + 2), "lineage is the file's own row"
    assert a001.accrual.amount == 100000


def test_importing_the_same_export_again_is_a_repeat(root, tmp_path, erp):
    ws = load_workspace(root / "config" / "close.toml")
    files = {"accruals": (erp["accruals"].name, erp["accruals"].read_bytes())}
    with open_store(tmp_path / "s.duckdb") as store:
        [first] = controls.import_files(store, ws, files, datetime(2026, 5, 4))
        [again] = controls.import_files(store, ws, files, datetime(2026, 5, 5))
        assert (first.outcome, again.outcome) == ("imported", "repeat")
        assert store.count("control_imports") == 1


def test_a_missing_mapped_column_names_the_closest_header(root, tmp_path):
    ws = load_workspace(root / "config" / "close.toml")
    path = export_xlsx(EX / "accruals_2026-03.csv", tmp_path / "accruals_export.xlsx", "accruals")
    mapping_file = tmp_path / "m.toml"
    mapping_file.write_text(mapping_toml("accruals", amount="Accrued amt"), encoding="utf-8")
    mapping = controls.load_mapping(mapping_file, "accruals")
    with open_store(tmp_path / "s.duckdb") as store:
        with pytest.raises(controls.ControlsError) as e:
            controls.import_files(store, ws, {"accruals": (path.name, path.read_bytes())}, mappings={"accruals": mapping})
        assert store.count("control_imports") == 0
    assert str(e.value).startswith('accruals_export.xlsx is missing accruals column amount (from "Accrued amt") '
                                   '(closest header "Accrued amount") (found: Batch, Memo, ')


def test_bad_amounts_follow_the_submission_rules(root, tmp_path):
    ws = load_workspace(root / "config" / "close.toml")
    wb = Workbook()
    ws_ = wb.active
    ws_.append(list(RENAMED["gl"].values()))
    ws_.append(["J1", "2026-04", "SG", "OTH", "12k", "A-001", "text amount"])
    ws_.append(["J2", "2026-04", "SG", "OTH", "=E2*2", "A-001", "formula with no cached value"])
    ws_.append(["J3", "2026-04", "SG", "OTH", "(1,234.50)", "A-001", "accounting negative"])
    wb.save(tmp_path / "gl.xlsx")
    mapping_file = tmp_path / "m.toml"
    mapping_file.write_text(mapping_toml("gl").replace("header_row = 4", "header_row = 1")
                            .replace('sheet = "Export"', 'sheet = "Sheet"'), encoding="utf-8")
    r = controls.read("gl", (tmp_path / "gl.xlsx").read_bytes(), "gl.xlsx", ws,
                      controls.load_mapping(mapping_file, "gl"))
    assert r.problems == ("gl.xlsx row 2 amount (\"Amount\"): not an amount: '12k'",
                          "gl.xlsx row 3 amount (\"Amount\"): not an amount: '=E2*2'")
    assert [(row.journal_id, row.amount) for row in r.rows] == [("J3", Decimal("-1234.50"))]


def test_a_filled_xlsx_template_imports_with_no_mapping(tmp_path):
    ws = load_workspace(REPO / "config" / "close.toml")
    cli.main(["templates", "write", "--out", str(tmp_path / "t")])
    wb = load_workbook(tmp_path / "t" / "accruals_template.xlsx")
    with open(EX / "accruals_2026-03.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            wb["Accruals"].append([float(v) if c == "amount" else v for c, v in r.items()])
    wb.save(tmp_path / "accruals.xlsx")
    from_xlsx = controls.parse_file("accruals", (tmp_path / "accruals.xlsx").read_bytes(), "accruals.xlsx", ws)
    from_csv = controls.parse_file("accruals", (EX / "accruals_2026-03.csv").read_bytes(), "a.csv", ws)
    assert [(r.accrual_id, r.amount, r.owner, r.source.row) for r in from_xlsx.rows] == [
        (r.accrual_id, r.amount, r.owner, r.source.row) for r in from_csv.rows]


def test_a_mapping_naming_an_unknown_column_is_refused(tmp_path):
    path = tmp_path / "m.toml"
    path.write_text(mapping_toml("gl") + 'invoice_id = "Invoice"\n', encoding="utf-8")
    with pytest.raises(controls.ControlsError, match="gl mapping: .* layout.columns has invoice_id, not template"):
        controls.load_mapping(path, "gl")


# Through the CLI


def fct(capsys, root: Path, *args: str, code: int = 0) -> str:
    argv = ["controls", *args, "--store", str(root / "s.duckdb"), "--config", str(root / "config" / "close.toml")]
    assert cli.main(argv) == code, args
    return capsys.readouterr().out


def test_cli_inspect_previews_without_importing(capsys, root, erp):
    out = fct(capsys, root, "inspect", str(erp["gl_v2"]), "--kind", "gl", "--mapping", str(erp["gl_negate"]))
    assert "File: gl_export_v2.xlsx (xlsx, gl, read with mapping " in out
    assert "Sheets: Export; using Export" in out
    assert "Header row 4: Batch | Line text | Reference | Amount | Account | Company | Fiscal period | Journal" in out
    assert "Amounts are negated: the file shows them credit-positive." in out
    assert "  6  JE-0402     2026-04  SG      OTH      (60,000.00)   A-003      Reversal of A-003" in out
    assert out.rstrip().endswith("Would import as gl 2026-04 (6 rows). Nothing imported yet.")
    with open_store(root / "s.duckdb") as store:
        assert store.count("control_imports") == 0


def test_cli_inspect_shows_problems_and_the_closest_header(capsys, root, erp, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(mapping_toml("gl", amount="Amont"), encoding="utf-8")
    out = fct(capsys, root, "inspect", str(erp["gl"]), "--kind", "gl", "--mapping", str(bad), code=1)
    assert "amount       Amont          MISSING  Amount" in out
    assert 'Would be refused:\n  - gl_export.xlsx is missing gl column amount (from "Amont") (closest header ' \
           '"Amount")' in out


def test_cli_import_with_a_mapping_override(capsys, root, erp):
    out = fct(capsys, root, "import", "--accruals", str(erp["accruals"]), "--invoices", str(erp["invoices"]),
              "--gl", str(erp["gl"]), "--as-of", "2026-05-04T09:00")
    assert "Imported accruals_export.xlsx as accruals 2026-03 (6 rows)." in out
    out = fct(capsys, root, "import", "--gl", str(erp["gl_v2"]), "--gl-mapping", str(erp["gl_negate"]),
              "--as-of", "2026-05-05T09:00")
    assert "Imported gl_export_v2.xlsx as gl 2026-04 (6 rows), replacing gl_export.xlsx." in out
    assert "JE-0406" in out
    out = fct(capsys, root, "show", "A-002", "--as-of", "2026-05-05T10:00")
    assert "State: invoice invoiced, reversal ledger_confirmed, overall cleared" in out
    assert "Reversal  JE-0406   gl_export_v2.xlsx      10  (40,000.00)" in out


def test_cli_mapping_flag_without_its_file_is_refused(capsys, root, erp):
    out = fct(capsys, root, "import", "--accruals", str(erp["accruals"]), "--gl-mapping", str(erp["gl_negate"]),
              code=1)
    assert out == "Import refused: --gl-mapping needs --gl. Nothing imported.\n"


# Re-imports and mapping identity


def gl_amounts(store: Store, ws) -> list[Decimal]:
    c = controls.load(store, ws, LATER)
    [gl] = [imp for (kind, _), imp in controls.current_imports(c.imports).items() if kind == "gl"]
    return [line.amount for line in gl.rows]


def test_reimporting_under_an_earlier_mapping_makes_that_import_current_again(root, tmp_path, erp):
    ws = load_workspace(root / "config" / "close.toml")
    negate = controls.load_mapping(erp["gl_negate"], "gl")
    as_is = controls.load_mapping(root / "config" / "controls" / "mappings" / "gl.toml", "gl")
    files = {"gl": (erp["gl"].name, erp["gl"].read_bytes())}
    with open_store(tmp_path / "s.duckdb") as store:
        controls.import_files(store, ws, files, datetime(2026, 5, 4), mappings={"gl": negate})
        negated = gl_amounts(store, ws)
        controls.import_files(store, ws, files, datetime(2026, 5, 5))
        assert gl_amounts(store, ws) == [-a for a in negated]
        [again] = controls.import_files(store, ws, files, datetime(2026, 5, 6), mappings={"gl": negate})
        assert again.outcome == "reimported"
        assert again.message == (f"Imported gl_export.xlsx again as gl 2026-04 (mapping gl_negate {negate.version}); "
                                 f"it is current again, replacing gl_export.xlsx (mapping gl {as_is.version}).")
        assert gl_amounts(store, ws) == negated, "the earlier mapping's numbers are current again"
        [repeat] = controls.import_files(store, ws, files, datetime(2026, 5, 7), mappings={"gl": negate})
        assert repeat.outcome == "repeat"
        assert repeat.message == "Repeat: gl_export.xlsx is already imported as gl 2026-04. Nothing changed."
        assert store.count("control_imports") == 2
        c = controls.load(store, ws, datetime(2026, 5, 5, 12))
        assert [imp.mapping for imp in controls.current_imports(c.imports).values()] == [f"mapping gl {as_is.version}"]


def test_a_comment_only_mapping_edit_reads_the_same_import(root, tmp_path, erp):
    ws = load_workspace(root / "config" / "close.toml")
    edited = tmp_path / "gl_negate_edited.toml"
    lines = erp["gl_negate"].read_text(encoding="utf-8").splitlines()
    edited.write_text("# reviewed 2026-05\n" + "\n".join([lines[0], *lines[1:]]) + "  # trailing note\n",
                      encoding="utf-8")
    first, second = controls.load_mapping(erp["gl_negate"], "gl"), controls.load_mapping(edited, "gl")
    assert first.version == second.version
    files = {"gl": (erp["gl"].name, erp["gl"].read_bytes())}
    with open_store(tmp_path / "s.duckdb") as store:
        controls.import_files(store, ws, files, datetime(2026, 5, 4), mappings={"gl": first})
        [again] = controls.import_files(store, ws, files, datetime(2026, 5, 5), mappings={"gl": second})
        assert again.outcome == "repeat"


def test_status_and_show_name_the_mapping_that_read_each_current_import(capsys, root, erp):
    fct(capsys, root, "import", "--accruals", str(erp["accruals"]), "--gl", str(erp["gl_v2"]),
        "--gl-mapping", str(erp["gl_negate"]), "--as-of", "2026-05-04T09:00")
    accruals = controls.load_mapping(root / "config" / "controls" / "mappings" / "accruals.toml", "accruals")
    negate = controls.load_mapping(erp["gl_negate"], "gl")
    expected = (f"Imports: accruals 2026-03 accruals_export.xlsx (mapping accruals {accruals.version}); "
                f"gl 2026-04 gl_export_v2.xlsx (mapping gl_negate {negate.version})")
    assert expected in fct(capsys, root, "status", "--as-of", "2026-05-04T10:00").splitlines()
    assert expected in fct(capsys, root, "show", "A-001", "--as-of", "2026-05-04T10:00").splitlines()


def test_status_says_template_columns_for_a_file_read_without_a_mapping(capsys, root):
    fct(capsys, root, "import", "--accruals", str(EX / "accruals_2026-03.csv"), "--as-of", "2026-05-04T09:00")
    out = fct(capsys, root, "status", "--as-of", "2026-05-04T10:00")
    assert "Imports: accruals 2026-03 accruals_2026-03.csv (template columns)" in out.splitlines()


# Inspect


def test_inspect_finds_the_header_row_when_every_mapped_column_is_missing(capsys, root, erp, tmp_path):
    wrong_row = tmp_path / "gl_row1.toml"
    wrong_row.write_text(mapping_toml("gl").replace(f"header_row = {TITLE_ROWS + 1}", "header_row = 1"),
                         encoding="utf-8")
    out = fct(capsys, root, "inspect", str(erp["gl"]), "--kind", "gl", "--mapping", str(wrong_row), code=1)
    assert f"headers found on row {TITLE_ROWS + 1}; set header_row = {TITLE_ROWS + 1}" in out
    ws = load_workspace(root / "config" / "close.toml")
    with pytest.raises(controls.ControlsError, match="set header_row = 4"):
        controls.parse_file("gl", erp["gl"].read_bytes(), "gl_export.xlsx", ws, controls.load_mapping(wrong_row, "gl"))


def test_inspect_exits_1_when_the_file_would_be_refused(capsys, root, erp, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(mapping_toml("gl", amount="Amont"), encoding="utf-8")
    out = fct(capsys, root, "inspect", str(erp["gl"]), "--kind", "gl", "--mapping", str(bad), code=1)
    assert "Would be refused:" in out
    assert "headers found on row" not in out, "only some columns are missing: the header row is right"


def test_inspect_warns_when_every_gl_amount_is_positive(capsys, root, erp):
    out = fct(capsys, root, "inspect", str(erp["gl_v2"]), "--kind", "gl")
    assert "Warning: all GL amounts are positive; check amount_sign" in out
    out = fct(capsys, root, "inspect", str(erp["gl_v2"]), "--kind", "gl", "--mapping", str(erp["gl_negate"]))
    assert "Warning" not in out


# Periods Excel turned into dates


def test_a_controls_period_excel_turned_into_a_date_says_how_to_fix_it(root, tmp_path):
    ws = load_workspace(root / "config" / "close.toml")
    wb = Workbook()
    sheet = wb.active
    sheet.append(list(TEMPLATES["gl"].names))
    sheet.append(["JE-1", datetime(2026, 4, 1), "SG", "OTH", -100, "A-001", "Reversal"])
    wb.save(tmp_path / "gl.xlsx")
    r = controls.read("gl", (tmp_path / "gl.xlsx").read_bytes(), "gl.xlsx", ws)
    assert r.problems == ("gl.xlsx row 2 period: Excel turned the period into a date (2026-04-01); format the "
                          "period column as text and type 2026-04",)


def test_a_submission_period_excel_turned_into_a_date_says_how_to_fix_it(root, tmp_path):
    config = root / "config" / "close.toml"
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace('mapping = "mappings/sg.toml"', 'mapping = "mappings/_regional_template.toml"'),
                      encoding="utf-8")
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Submission"
    sheet.append(list(TEMPLATES["submission"].names))
    for account, description, amount in SG_OCTOBER:
        sheet.append([datetime(2026, 10, 1), account, description, amount])
    wb.save(tmp_path / "sg.xlsx")
    with open_store(tmp_path / "s.duckdb") as store:
        r = validation.ingest(store, load_workspace(config), tmp_path / "sg.xlsx", "SG", "2026-10", "actual",
                              datetime(2026, 11, 4, 10))
    [wrong] = [e for e in r.validation.exceptions if e.code == "WRONG_PERIOD"]
    assert wrong.detail.endswith("; Excel turned the period into a date (2026-10-01); format the Period column as "
                                 "text and type 2026-10")


def test_template_instructions_list_the_configured_accounts_the_period_format_and_the_sign(root, tmp_path):
    chart = root / "config" / "chart_of_accounts.toml"
    chart.write_text(chart.read_text(encoding="utf-8") + '\n[accounts.TRV]\nname = "Travel"\ntype = "opex"\n',
                     encoding="utf-8")
    assert cli.main(["templates", "write", "--out", str(tmp_path / "t"), "--config",
                     str(root / "config" / "close.toml")]) == 0
    notes = [tuple(v for v in r if v is not None)
             for r in load_workbook(tmp_path / "t" / "submission_template.xlsx")["Instructions"].iter_rows(values_only=True)]
    accounts = load_workspace(root / "config" / "close.toml").coa.accounts
    for a in accounts.values():
        assert (a.code, a.name) in notes
    assert ("TRV", "Travel") in notes, "generated from the configured chart"
    assert ("Period format: YYYY-MM typed as text, like 2026-10. Format the Period column as text so Excel "
            "keeps it.",) in notes
    assert (f"Sign convention: {TEMPLATES['submission'].columns[3].sign}",) in notes
    cli.main(["templates", "write", "--out", str(tmp_path / "t2"), "--csv", "--config",
              str(root / "config" / "close.toml")])
    with open(tmp_path / "t2" / "submission_instructions.csv", newline="", encoding="utf-8") as f:
        assert ["TRV", "Travel"] in [r[:2] for r in csv.reader(f)]
