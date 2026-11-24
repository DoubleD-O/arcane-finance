"""The Streamlit UI through streamlit.testing (AppTest), against the demo story loaded from the sidebar."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from conftest import REPO
from openpyxl import load_workbook
from pptx import Presentation
from streamlit.testing.v1 import AppTest

from finance_close_toolkit import app, controls, validation
from finance_close_toolkit.config import load_workspace
from finance_close_toolkit.store import Store

APP = Path(app.__file__)
EXAMPLES = REPO / "examples" / "2026-10"
PAGES = list(app.PAGES)


def load(step: str, page: str) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.sidebar.selectbox(key="demo_step").set_value(step)
    button(at.sidebar, "Load demo").click().run()
    at.sidebar.radio(key="page").set_value(page).run()
    assert_clean(at)
    return at


def assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, errors(at)


def button(where, label: str):
    return next(b for b in where.button if b.label == label)


def shown(markdown: str) -> str:
    """What a reader sees for Markdown the app escaped."""
    return re.sub(r"\\(.)", r"\1", markdown)


def literal(markdown: str) -> bool:
    """True when the app escaped this Markdown, so a reader sees its text as written."""
    return markdown == app.esc(shown(markdown))


def texts(at: AppTest) -> str:
    return "\n".join(shown(e.value) for e in [*at.markdown, *at.text, *at.success, *at.warning, *at.error])


def errors(at: AppTest) -> list[str]:
    return [shown(e.value) for e in at.error]


def rows(at: AppTest, i: int) -> list[dict]:
    return at.dataframe[i].value.to_dict("records")


def find_rows(at: AppTest, **match) -> list[dict]:
    return [r for i in range(len(at.dataframe)) for r in rows(at, i)
            if all(r.get(k) == v for k, v in match.items())]


def upload(at: AppTest, i: int, path: Path, name: str | None = None) -> None:
    at.file_uploader[i].set_value((name or path.name, path.read_bytes(), "application/octet-stream")).run()


def release_ids(at: AppTest) -> list[str]:
    with Store.open(at.session_state["store_path"]) as store:
        return [r.release_id for r in store.releases("2026-10")]


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders_at_b2(page):
    at = load("B2", page)
    if page in app.OWN_HEADING:
        assert at.header[0].value == page and "2026-10" not in at.header[0].value
    else:
        assert at.header[0].value == f"{page}, 2026-10"


def test_close_board_at_e2_names_the_market_the_reason_and_the_owner():
    at = load("E2", "Close board")
    assert at.text_input(key="as_of").value == "2026-11-06 00:00"
    [au] = find_rows(at, Market="AU", Scenario="actual")
    assert au["Status"] == "Missing"
    body = texts(at)
    assert "**1. Missing submission: AU actual** (overdue)" in body
    assert "- Owner: AU country controller" in body
    assert "- Due: 2026-11-05, overdue" in body
    assert "- Trigger: no actual file received for 2026-10 and it is overdue" in body
    assert "- Next action: Chase AU country controller for the AU actual file" in body
    assert "**2." not in body


def test_close_board_keeps_the_demo_as_of_after_other_pages():
    at = load("E2", "Submit a file")
    at.sidebar.radio(key="page").set_value("Close board").run()
    assert at.text_input(key="as_of").value == "2026-11-06 00:00"

    at.sidebar.radio(key="page").set_value("Review and publish").run()
    at.sidebar.selectbox(key="demo_step").set_value("E11")
    button(at.sidebar, "Load demo").click().run()
    at.sidebar.radio(key="page").set_value("Close board").run()
    assert_clean(at)
    assert at.text_input(key="as_of").value == "2026-11-12 13:00"


def test_review_at_e11_shows_the_au_delta_and_approving_publishes_r2():
    at = load("E11", "Review and publish")
    assert at.subheader[0].value == "Proposed release 2026-10-R2 (previous 2026-10-R1)"
    [au] = find_rows(at, Scenario="actual", Scope="AU", KPI="EBITDA")
    assert (au["Before"], au["After"], au["Change"], au["Change %"], au["Materiality"]) == (
        "51,600.00", "38,700.00", "-12,900.00", "-25.00%", "Material")
    [apac] = find_rows(at, Scenario="actual", Scope="APAC", KPI="EBITDA")
    assert (apac["Before"], apac["After"], apac["Change %"], apac["Materiality"]) == (
        "221,600.00", "208,700.00", "-5.82%", "Material")
    [revenue] = find_rows(at, Scenario="actual", Scope="AU", KPI="Revenue")
    assert (revenue["Change"], revenue["Materiality"]) == ("0.00", "Below threshold")
    body = texts(at)
    assert "- AU actual: au_actual_v2.xlsx, received 2026-11-12 11:00" in body
    assert "SG actual:" not in body

    button(at, "Approve and publish").click().run()
    assert errors(at) == ["Enter who approves this release before publishing."]

    at.text_input(key="approved_by").set_value("Regional FP&A lead")
    button(at, "Approve and publish").click().run()
    assert_clean(at)
    assert shown(at.success[0].value) == "Published 2026-10-R2 (previous 2026-10-R1), approved by Regional FP&A lead."
    assert "No change" not in texts(at)
    assert not [b for b in at.button if b.label == "Approve and publish"]
    assert not [e for e in at.sidebar.success if "Demo loaded" in e.value]
    assert release_ids(at) == ["2026-10-R1", "2026-10-R2"]


def test_approving_after_a_new_file_arrived_is_refused_and_publishes_nothing():
    at = load("E11", "Review and publish")
    assert "SG actual:" not in texts(at)
    with Store.open(at.session_state["store_path"]) as store:
        ws = load_workspace(Path(at.session_state["config_path"]))
        validation.ingest(store, ws, EXAMPLES / "sg_actual_v2.csv", "SG", "2026-10", "actual",
                          datetime(2026, 11, 14, 9, 0))
    at.text_input(key="approved_by").set_value("Regional FP&A lead")
    button(at, "Approve and publish").click().run()
    assert not at.exception
    assert errors(at) == ["Publish refused: The proposal changed since you reviewed it; review it again."]
    assert release_ids(at) == ["2026-10-R1"]
    assert "- SG actual: sg_actual_v2.csv, received 2026-11-14 09:00" in texts(at)

    button(at, "Approve and publish").click().run()
    assert_clean(at)
    assert shown(at.success[0].value) == "Published 2026-10-R2 (previous 2026-10-R1), approved by Regional FP&A lead."
    assert release_ids(at) == ["2026-10-R1", "2026-10-R2"]


def test_approver_and_fx_notes_are_shown_literally():
    at = load("E11", "Review and publish")
    approver = "![x](http://example.invalid/x.png) *lead*"
    at.text_input(key="approved_by").set_value(approver)
    button(at, "Approve and publish").click().run()
    at.sidebar.radio(key="page").set_value("Results").run()
    assert_clean(at)
    [heading] = [m.value for m in at.markdown if "Approved by" in m.value]
    assert f"Approved by {approver} on" in shown(heading)
    assert literal(heading)
    notes = [c.value for c in at.caption if "translated at" in c.value]
    assert notes and all(literal(n) for n in notes)


def test_results_for_r2_show_apac_ebitda_and_trace_it_to_source_rows():
    at = load("B2", "Results")
    assert at.selectbox(key="results_release").value == "2026-10-R2"
    [ebitda] = [r for r in rows(at, 0) if r["Line"] == "EBITDA"]
    assert (ebitda["Actual"], ebitda["Budget"], ebitda["Variance"], ebitda["Var %"], ebitda["Fav / Unfav"]) == (
        "208,700.00", "191,000.00", "17,700.00", "9.27%", "Favourable")

    at.selectbox(key="trace_scope").set_value("APAC")
    at.selectbox(key="trace_line").set_value("ebitda").run()
    assert "Total actual: 208,700.00 SGD" in texts(at)

    at.selectbox(key="results_release").set_value("2026-10-R1")
    at.selectbox(key="trace_scope").set_value("AU")
    at.selectbox(key="trace_line").set_value("REV").run()
    assert_clean(at)
    [fact] = find_rows(at, Market="AU", Account="REV")
    assert (fact["File"], fact["Row"], fact["Local amount"], fact["Ccy"], fact["Rate"], fact["SGD amount"]) == (
        "au_actual_v1.xlsx", "4", "500,000.00", "AUD", "0.86", "430,000.00")


def test_downloads_give_a_deck_whose_slide_2_ebitda_matches_the_report():
    at = load("B2", "Releases and downloads")
    assert at.selectbox(key="downloads_release").value == "2026-10-R2"
    upload(at, 0, REPO / "examples" / "commentary" / "2026-10.toml")
    button(at, "Prepare downloads").click().run()
    assert_clean(at)
    assert "Deck for 2026-10-R2: FINAL." in texts(at)
    assert all(literal(b.label) for b in at.download_button)
    assert [shown(b.label) for b in at.download_button] == [
        "Download Comex_2026-10_2026-10-R2.pptx", "Download release.xlsx", "Download facts.csv",
        "Download manifest.json"]
    deck_path = Path(at.session_state["downloads"]["files"][0])
    slide = Presentation(str(deck_path)).slides[1]
    cells = [[c.text_frame.text for c in row.cells] for s in slide.shapes if s.has_table for row in s.table.rows]
    assert ["EBITDA", "208,700", "191,000", "17,700", "9.3%", "Favourable"] in cells


USD = "USD, converted from SGD at actual 1.30 / budget 1.32 SGD per USD"


def test_results_currency_selector_shows_r2_in_usd():
    at = load("B2", "Results")
    assert at.selectbox(key="results_currency").options == ["SGD", "AUD", "USD"]
    assert at.selectbox(key="results_currency").value == "SGD"
    at.selectbox(key="results_currency").set_value("USD").run()
    assert_clean(at)
    [heading] = [shown(m.value) for m in at.markdown if "Approved by" in m.value]
    assert heading.endswith(f"Amounts in {USD}.")
    by_line = {r["Line"]: r for r in rows(at, 0)}
    assert [by_line["Revenue"][k] for k in ("Actual", "Budget", "Variance", "Var %")] == [
        "1,100,000.00", "1,066,363.64", "33,636.36", "3.15%"]
    assert [by_line["EBITDA"][k] for k in ("Actual", "Budget", "Variance", "Var %")] == [
        "160,538.46", "144,696.97", "15,841.49", "10.95%"]
    assert "KPI changes (USD)" in texts(at)
    assert ("Variance in USD includes the translation effect of actual 1.30 vs budget 1.32 SGD per USD; a line on "
            "plan in SGD can show a USD variance.") in captions(at)
    assert "AU: local AUD translated to SGD at actual 0.86 / budget 0.88 SGD per AUD, then to USD as above." \
        in captions(at)
    assert "Materiality is judged in SGD, where the thresholds are set." in captions(at)
    assert "2026-10-R1 restated at 2026-10-R2's rates." in captions(at)


def test_downloads_in_usd_give_a_converted_deck_and_workbook():
    at = load("B2", "Releases and downloads")
    at.selectbox(key="downloads_currency").set_value("USD").run()
    upload(at, 0, REPO / "examples" / "commentary" / "2026-10.toml")
    button(at, "Prepare downloads").click().run()
    assert_clean(at)
    assert f"Deck for 2026-10-R2: FINAL. Amounts in {USD}." in texts(at)
    assert shown(at.download_button[0].label) == "Download Comex_2026-10_2026-10-R2_USD.pptx"
    slide = Presentation(at.session_state["downloads"]["files"][0]).slides[1]
    cells = [[c.text_frame.text for c in row.cells] for s in slide.shapes if s.has_table for row in s.table.rows]
    assert ["EBITDA", "160,538", "144,697", "15,841", "10.9%", "Favourable"] in cells
    workbook = load_workbook(at.session_state["downloads"]["files"][1])
    assert "Actual (USD)" in next(workbook["P&L vs budget"].values)
    assert dict(workbook["Manifest"].values)["presentation_conversion"] == USD
    # Another currency hides the prepared downloads until they are prepared again.
    at.selectbox(key="downloads_currency").set_value("SGD").run()
    assert not at.download_button


def test_a_bad_commentary_file_is_a_readable_error(tmp_path):
    at = load("B2", "Releases and downloads")
    bad = tmp_path / "commentary.toml"
    bad.write_text('outlook = ""\n', encoding="utf-8")
    upload(at, 0, bad)
    button(at, "Prepare downloads").click().run()
    assert not at.exception
    assert errors(at) == ["commentary.toml outlook: expected text, got ''"]
    assert "Compare two releases" in [h.value for h in at.subheader]


def test_downloads_take_a_chosen_pack():
    at = load("B2", "Releases and downloads")
    assert at.selectbox(key="deck_pack").options == [
        "Comex pack (comex)", "Board pack (board)", "Market review (market_review)", app.CUSTOM_PACK]
    at.selectbox(key="deck_pack").set_value("market_review").run()
    button(at, "Prepare downloads").click().run()
    assert_clean(at)
    assert "Deck for 2026-10-R2: FINAL." in texts(at)
    assert shown(at.download_button[0].label) == "Download Market_review_2026-10_2026-10-R2.pptx"
    slides = Presentation(at.session_state["downloads"]["files"][0]).slides
    titles = [s.shapes.title.text_frame.text for s in slides]
    assert titles == ["APAC market review, October 2026", "Markets", "P&L vs budget, SG", "P&L vs budget, AU",
                      "Changes since 2026-10-R1"]
    # Another pack hides the previous pack's downloads until they are prepared again.
    at.selectbox(key="deck_pack").set_value("board").run()
    assert not at.download_button


def test_an_uploaded_pack_is_used_and_an_invalid_one_is_a_readable_error(tmp_path):
    at = load("B2", "Releases and downloads")
    at.selectbox(key="deck_pack").set_value(app.CUSTOM_PACK).run()
    button(at, "Prepare downloads").click().run()
    assert not at.exception
    assert errors(at) == ["Upload a pack file (TOML), or choose one of the packs."]

    bad = tmp_path / "my pack.toml"
    bad.write_text('name = "Mine"\n[[slides]]\ntype = "pnl"\nscope = "NZ"\n', encoding="utf-8")
    upload(at, 0, bad)
    button(at, "Prepare downloads").click().run()
    assert not at.exception
    assert errors(at) == ["my pack.toml: slide 1 (pnl): unknown scope 'NZ'; expected one of APAC, SG, AU, "
                          "each_market"]
    assert not at.download_button
    assert "Compare two releases" in [h.value for h in at.subheader]

    good = tmp_path / "my pack.toml"
    good.write_text('name = "Mine"\n[[slides]]\ntype = "bridge"\nkpi = "gross_margin"\n', encoding="utf-8")
    upload(at, 0, good)
    button(at, "Prepare downloads").click().run()
    assert_clean(at)
    assert shown(at.download_button[0].label) == "Download My_pack_2026-10_2026-10-R2.pptx"
    [slide] = Presentation(at.session_state["downloads"]["files"][0]).slides
    assert slide.shapes.title.text_frame.text == "Gross margin bridge, budget to actual"


def test_submit_previews_then_ingests_the_au_resubmission():
    at = load("E6", "Submit a file")
    at.selectbox(key="submit_market").set_value("AU")
    upload(at, 0, EXAMPLES / "au_actual_v2.xlsx")
    assert_clean(at)
    [consulting] = find_rows(at, **{"Source account": "64000"})
    assert consulting["Maps to"] == "UNMAPPED"
    assert not at.warning
    at.text_input(key="received_at").set_value("2026-11-12 11:00")
    button(at, "Ingest").click().run()
    assert_clean(at)
    assert not at.success
    assert shown(at.warning[0].value) == "Ingested au_actual_v2.xlsx as AU actual 2026-10, received 2026-11-12 11:00."
    assert "Validation: Needs review under mapping" in texts(at)
    assert not [e for e in at.sidebar.success if "Demo loaded" in e.value]

    button(at, "Ingest").click().run()
    assert shown(at.warning[0].value).startswith("Repeat: au_actual_v2.xlsx is already stored as AU actual 2026-10")


def test_a_validated_file_is_a_success():
    at = load("E1", "Submit a file")
    at.selectbox(key="submit_market").set_value("AU")
    upload(at, 0, EXAMPLES / "au_actual_v1.xlsx")
    assert not at.warning and not at.checkbox
    button(at, "Ingest").click().run()
    assert_clean(at)
    assert shown(at.success[0].value).startswith("Ingested au_actual_v1.xlsx as AU actual 2026-10")


def test_a_file_with_none_of_the_columns_needs_confirmation_and_shows_as_rejected(tmp_path):
    at = load("E6", "Submit a file")
    at.selectbox(key="submit_market").set_value("SG")
    garbage = tmp_path / "notes.csv"
    garbage.write_text("Name,Comment\nAlice,hello\n", encoding="utf-8")
    upload(at, 0, garbage)
    assert_clean(at)
    assert [shown(w.value) for w in at.warning] == [
        "This file is missing required columns: GL Code, Description, Amount (SGD), Period. "
        "Ingesting it records it as rejected, and it becomes the latest file for SG actual 2026-10, "
        "which blocks publish until a corrected file arrives."]
    assert button(at, "Ingest").disabled
    [confirm] = at.checkbox
    assert confirm.label == "Ingest anyway; it will be recorded as rejected and become the latest file for this slot"
    confirm.check().run()
    assert not button(at, "Ingest").disabled
    button(at, "Ingest").click().run()
    assert not at.exception and not at.success
    assert shown(at.error[0].value).startswith("Ingested notes.csv as SG actual 2026-10")
    assert "Validation: Rejected under mapping" in texts(at)


def test_a_file_whose_extension_disagrees_with_the_mapping_needs_confirmation():
    at = load("E6", "Submit a file")
    at.selectbox(key="submit_market").set_value("SG")
    upload(at, 0, EXAMPLES / "sg_actual_v1.csv", name="sg_actual_v1.xlsx")
    assert_clean(at)
    assert "File sg_actual_v1.xlsx (.xlsx file)" in texts(at)
    assert [shown(w.value) for w in at.warning] == [
        "This is a .xlsx file but the SG mapping reads csv files; check it is the right file for SG."]
    assert button(at, "Ingest").disabled
    at.checkbox[0].check().run()
    assert not button(at, "Ingest").disabled


def test_a_file_of_the_wrong_format_is_a_readable_error():
    at = load("E6", "Submit a file")
    at.selectbox(key="submit_market").set_value("AU")
    upload(at, 0, EXAMPLES / "sg_actual_v1.csv")
    assert not at.exception
    assert errors(at) == ["sg_actual_v1.csv is not an Excel workbook; the AU mapping expects xlsx"]


def test_a_bad_period_is_a_readable_error():
    at = load("B2", "Close board")
    at.sidebar.text_input(key="period").set_value("Oct 2026").run()
    assert not at.exception
    assert errors(at) == ["period must look like 2026-10, got 'Oct 2026'"]
    assert at.header[0].value == "Close board"


def test_text_from_files_is_shown_literally_not_as_markdown():
    text = "![x](http://example.invalid/x.png) *bold* $x$ <b>"
    escaped = app.esc(text)
    assert shown(escaped) == text
    assert not re.search(r"(?<!\\)[!\[\]()*$<>]", escaped)


# Close controls (docs/fixture-expected.md, Step 4)

CONTROLS = REPO / "examples" / "controls"


def load_controls(step: str) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.sidebar.selectbox(key="controls_demo_step").set_value(step)
    button(at.sidebar, "Load close controls demo").click().run()
    assert_clean(at)
    return at


def controls_state(at: AppTest):
    with Store.open(at.session_state["store_path"]) as store:
        return controls.load(store, load_workspace(Path(at.session_state["config_path"])))


def test_close_controls_at_c1_shows_every_queue_with_its_signals():
    at = load_controls("C1")
    assert [t.label for t in at.tabs] == ["Finance owners (2)", "AP team (1)", "Accounting team (1)",
                                          "Regional close lead (1)"]
    finance, ap, accounting, lead = (texts_in(t) for t in at.tabs)
    assert "**1. Invoice variance: A-001**" in finance and "**2. Partial invoice: A-003**" in finance
    assert "- Amount at risk: SGD 10,000.00" in finance
    assert "- Evidence: invoice INV-5501: invoices_2026-04.csv row 2 (reference)" in finance
    assert "**1. Ambiguous match: INV-5504**" in ap and "candidate A-005: accruals_2026-03.csv row 6" in ap
    assert "**1. Missing reversal: A-002**" in accounting
    assert "**1. No owner: A-006**" in lead
    assert "P&L expense SG 2026-04 from these items (SGD): (5,000.00)" in texts(at)
    bullets = [line[2:] for t in at.tabs for m in t.markdown for line in m.value.splitlines() if line.startswith("- ")]
    assert bullets and all(literal(b) for b in bullets)

    at.selectbox(key="controls_detail").set_value("A-001").run()
    assert_clean(at)
    assert "Net 2026-04: P&L expense 10,000.00; accrued liabilities 0.00; accounts payable 110,000.00; cash 0.00" \
        in texts(at)
    [reversal] = find_rows(at, Entry="Reversal")
    assert (reversal["Dr"], reversal["Cr"], reversal["Amount"], reversal["Evidence"]) == (
        "Accrued liabilities", "OTH", "100,000.00", "JE-0401, gl_2026-04.csv row 2")


def texts_in(where) -> str:
    return "\n".join(shown(m.value) for m in where.markdown)


def test_ap_links_inv5504_to_a004_through_the_form_and_others_are_refused():
    at = load_controls("C1")
    at.selectbox(key="link_to_INV-5504").set_value("A-004")
    at.button(key="link_INV-5504").click().run()
    assert errors(at) == ["'SG finance analyst' may not link invoices; allowed: AP team. Nothing recorded."]
    assert controls_state(at).events == ()

    at.selectbox(key="controls_role").set_value("AP team").run()
    at.selectbox(key="link_to_INV-5504").set_value("A-004")
    at.button(key="link_INV-5504").click().run()
    assert_clean(at)
    assert shown(at.success[0].value) == "Linked INV-5504 to A-004."
    assert [t.label for t in at.tabs][1] == "AP team (0)"
    c = controls_state(at)
    assert (c.accrual("A-004").overall, len(c.signals)) == ("cleared", 4)
    [a4] = find_rows(at, Accrual="A-004")
    assert (a4["Invoiced"], a4["Overall"]) == ("12,000.00", "Cleared")


def test_resolving_needs_an_approver_other_than_the_preparer():
    at = load_controls("C7")
    assert [t.label for t in at.tabs] == ["Finance owners (1)", "AP team (0)", "Accounting team (0)",
                                          "Regional close lead (0)"]
    assert at.text_input(key="resolve_amount_A-003").value == "35000"
    approver = at.selectbox(key="resolve_approver_A-003")
    assert approver.value is None and approver.options == ["Regional close lead", "Accounting team"]
    assert at.selectbox(key="resolve_kind_A-003").options == ["re_accrue", "not_expected"]
    at.text_input(key="resolve_reason_A-003").set_value("Phase 2 invoice due May")
    at.button(key="resolve_A-003").click().run()
    assert errors(at)[0].startswith("A resolution needs an approver")
    assert controls_state(at).accrual("A-003").overall == "open"

    at.selectbox(key="resolve_approver_A-003").set_value("Regional close lead")
    at.button(key="resolve_A-003").click().run()
    assert_clean(at)
    assert shown(at.success[0].value) == "Resolved A-003: re_accrue 35,000.00, approved by Regional close lead."
    assert controls_state(at).signals == ()
    at.selectbox(key="controls_detail").set_value("A-003").run()
    [resolved] = find_rows(at, What="Resolved: re_accrue 35,000.00")
    assert resolved["By"] == "SG finance analyst" and "approved by Regional close lead" in resolved["Detail"]


def test_close_controls_heading_names_the_imported_periods_not_the_sidebar_period():
    at = load_controls("C1")
    assert at.session_state["period"] != "2026-04"
    assert at.header[0].value == "Close controls: accruals 2026-03, invoices 2026-04, GL 2026-04"


def test_close_controls_honour_as_of_like_the_cli():
    at = load_controls("C9")
    assert "0 signals need attention" in texts(at)
    at.text_input(key="as_of").set_value("2026-05-04 09:30").run()
    assert_clean(at)
    assert "As of 2026-05-04 09:30. 5 signals need attention." in texts(at)


def test_the_ambiguous_match_form_starts_blank():
    at = load_controls("C1")
    at.selectbox(key="controls_role").set_value("AP team").run()
    link_to = at.selectbox(key="link_to_INV-5504")
    assert link_to.value is None and link_to.options == ["A-004", "A-005"]
    at.button(key="link_INV-5504").click().run()
    assert controls_state(at).events == ()


def test_an_override_offers_candidates_first_and_warns_on_differences():
    at = load_controls("C1")
    at.selectbox(key="controls_role").set_value("AP team").run()
    at.checkbox(key="link_override_INV-5504").check().run()
    link_to = at.selectbox(key="link_to_INV-5504")
    assert link_to.options[:2] == ["A-004 (candidate)", "A-005 (candidate)"]
    assert at.selectbox(key="link_approver_INV-5504").options == ["Regional close lead", "Accounting team"]
    link_to.set_value("A-001").run()
    assert any("A-001 differs from INV-5504" in shown(w.value) and "supplier" in shown(w.value)
               for w in at.warning)


def test_the_resolve_inputs_are_a_form_so_typing_does_not_rerun():
    at = load_controls("C7")
    at.text_input(key="resolve_reason_A-003").set_value("Phase 2 invoice due May")
    at.selectbox(key="resolve_approver_A-003").set_value("Regional close lead")
    assert at.text_input(key="resolve_reason_A-003").form_id == "resolve_form_A-003"
    at.button(key="resolve_A-003").click().run()
    assert_clean(at)
    assert shown(at.success[0].value) == "Resolved A-003: re_accrue 35,000.00, approved by Regional close lead."
    at.selectbox(key="controls_detail").set_value("A-003").run()
    assert "Pending entries, not yet in the ledger (SGD)" in texts(at)
    pending = find_rows(at, Accrual="A-003", Kind="re_accrue")  # the P&L list and the detail
    assert len(pending) == 2 and {r["P&L effect"] for r in pending} == {"+35,000.00"}


def test_close_controls_imports_the_uploaded_templates():
    at = load("E1", "Close controls")
    assert "No accruals imported yet." in texts(at)
    for i, name in enumerate(["accruals_2026-03.csv", "invoices_2026-04.csv", "gl_2026-04.csv"]):
        upload(at, i, CONTROLS / name)
    button(at, "Import").click().run()
    assert_clean(at)
    assert [shown(s.value) for s in at.success] == [
        "Imported accruals_2026-03.csv as accruals 2026-03 (6 rows).",
        "Imported invoices_2026-04.csv as invoices 2026-04 (4 rows).",
        "Imported gl_2026-04.csv as gl 2026-04 (5 rows)."]
    assert [t.label for t in at.tabs][0] == "Finance owners (2)"


def test_a_bad_controls_csv_is_a_readable_refusal(tmp_path):
    at = load("E1", "Close controls")
    bad = tmp_path / "accruals.csv"
    bad.write_text("accrual_id,market,period,account,supplier,reference,amount,owner,description\n"
                   "A-1,SG,2026-03,OTH,S,,12k,x,d\n", encoding="utf-8")
    upload(at, 0, bad)
    button(at, "Import").click().run()
    assert not at.exception
    assert errors(at) == ["Import refused. Nothing imported. accruals.csv refused:\n"
                          "  - accruals.csv row 2 amount: not an amount: '12k'"]


def planning_page(scenario: str | None = None, plan: str | None = None) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.sidebar.radio(key="page").set_value("Planning").run()
    if plan is not None:
        at.text_input(key="plan_path").set_value(plan).run()
    if scenario is not None:
        at.selectbox(key="plan_scenario").set_value(scenario).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def test_planning_base_shows_statements_checks_and_lowest_cash():
    at = planning_page()
    assert_clean(at)
    assert at.header[0].value == "Planning"
    assert at.text_input(key="plan_path").value == "plans/agency_2027.toml"
    assert [t.label for t in at.tabs] == ["P&L", "Balance sheet", "Cash flow", "Scenario vs base"]
    checks, pnl, bs, cf, drill = (rows(at, i) for i in range(5))
    [net] = [r for r in pnl if r["Line"] == "Net income"]
    assert (net["Jan-27"], net["FY2027"]) == ("3,901.00", "25,190.50")
    [cash] = [r for r in bs if r["Line"] == "Cash"]
    assert cash["FY2027"] == "295,125.50" and cash["Schedule"] == "Cash receipts and payments"
    assert [r["Result"] for r in checks] == ["Pass"] * 5
    assert cf[-1]["Line"] == "Closing cash" and cf[-1]["Dec-27"] == "295,125.50"
    assert drill[0]["Line"] == "Total units"
    assert (drill[0]["Jan-27"], drill[0]["FY2027"]) == ("2,000", "24,000")  # units, not money
    assert list(pnl[0])[:3] == ["Line", "FY2027", "Jan-27"]  # the fiscal year right after Line, both pinned
    pinned = {c for c, cfg in json.loads(at.dataframe[1].proto.columns).items() if cfg.get("pinned")}
    assert pinned == {"Line", "FY2027"}
    assert not at.checkbox  # a 12-month plan shows every month already
    assert "Plan agency_2027 has no [budget] section." in texts(at) and not at.download_button
    assert "All checks pass: the balance sheet balances and cash ties in every month." in texts(at)
    assert (at.metric[0].label, at.metric[0].value) == ("Lowest month-end cash (SGD, Jan-27)", "226,700.00")
    assert "Choose a scenario to compare with base." in texts(at)


def test_planning_scenario_compares_with_base_and_drills_to_a_schedule():
    at = planning_page("slow_collections")
    assert_clean(at)
    [change] = find_rows(at, Line="Closing cash, change")
    assert (change["Feb-27"], change["FY2027"]) == ("(12,000.00)", "(12,000.00)")
    [ar] = find_rows(at, Line="AR, slow_collections")
    assert ar["FY2027"] == "36,000.00"
    assert "Lowest month-end cash: base 226,700.00 (Jan-27); slow_collections 226,700.00 (Jan-27)." in texts(at)
    at.selectbox(key="plan_drill").set_value("Balance sheet: Accounts receivable").run()
    assert "Balance sheet: Accounts receivable comes from the Accounts receivable schedule." in texts(at)
    [receipts] = find_rows(at, Line="Receipts, in the plan")
    assert receipts["Feb-27"] == "12,000.00" and receipts["Mar-27"] == "24,000.00"


def captions(at: AppTest) -> str:
    return "\n".join(shown(c.value) for c in at.caption)


def test_planning_cash_shortfall_is_a_qualified_pass(tmp_path):
    short = tmp_path / "short.toml"
    short.write_text((REPO / "plans" / "agency_2027.toml").read_text(encoding="utf-8")
                     .replace('cash = "200000"', 'cash = "-90000"').replace('share_capital = "300000"', 'share_capital = "10000"'),
                     encoding="utf-8")
    at = planning_page(plan=str(short))
    assert_clean(at)
    assert (at.metric[0].label, at.metric[0].value) == ("Lowest month-end cash (SGD, Jan-27)", "(63,300.00)")
    warnings = [shown(w.value) for w in at.warning]
    assert warnings[0] == "Cash goes below zero from Jan-27 to Apr-27 and in Nov-27; lowest (63,300.00) in Jan-27."
    assert warnings[1].startswith("Checks pass; cash warning.")
    assert "All checks pass" not in texts(at) and not at.success


def test_planning_pnl_only_and_a_bad_plan(tmp_path):
    at = planning_page("mix_shift", plan=str(REPO / "plans" / "mix_check.toml"))
    assert_clean(at)
    assert at.warning[0].value.startswith(app.esc("P&L only: the opening balance sheet"))
    assert [t.label for t in at.tabs] == ["P&L", "Scenario vs base"]
    [revenue] = [r for r in rows(at, 1) if r["Line"] == "Revenue"]
    assert revenue["Jan-27"] == "14,000.00"
    assert [r["Result"] for r in find_rows(at, Check="Mix shares sum to 100% in every month (validated on load)")] == ["Pass"]
    assert "Changes against base: products.A.mix_pct: 80 -> 60; products.B.mix_pct: 20 -> 40" in captions(at)
    assert "All checks pass" not in texts(at)

    bad = tmp_path / "bad.toml"
    bad.write_text((REPO / "plans" / "mix_check.toml").read_text(encoding="utf-8").replace('mix_pct = "20"', 'mix_pct = "30"'),
                   encoding="utf-8")
    at.text_input(key="plan_path").set_value(str(bad)).run()
    assert "products: mix shares sum to 110% in 2027-01, must be 100% in every month (12 months affected)" in errors(at)


PLAN36 = str(REPO / "plans" / "agency_2027_36m.toml")
MONTHS_27 = [f"{m}-27" for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")]


def test_planning_36_months_shows_year_1_by_month_and_the_fiscal_years():
    at = planning_page(plan=PLAN36)
    assert_clean(at)
    checks, pnl, bs = rows(at, 0), rows(at, 1), rows(at, 2)
    assert list(pnl[0]) == ["Line", "FY2027", "FY2028", "FY2029", *MONTHS_27, "Schedule"]
    pinned = {c for c, cfg in json.loads(at.dataframe[1].proto.columns).items() if cfg.get("pinned")}
    assert pinned == {"Line", "FY2027", "FY2028", "FY2029"}
    [net] = [r for r in pnl if r["Line"] == "Net income"]
    assert (net["FY2027"], net["FY2028"], net["FY2029"]) == ("25,190.50", "54,780.00", "142,760.00")
    [bf] = [r for r in bs if r["Line"] == "Retained earnings brought forward"]
    assert (bf["FY2027"], bf["FY2028"], bf["FY2029"]) == ("50,000.00", "75,190.50", "129,970.50")
    assert [r["Result"] for r in checks] == ["Pass"] * 5
    assert [r["Detail"] for r in checks][1] == "36 of 36 months tie to the cent"
    assert [(x.label, x.value) for x in at.metric] == [
        ("Lowest month-end cash (SGD, Jan-27)", "226,700.00"), ("Lowest in FY2027 (Jan-27)", "226,700.00"),
        ("Lowest in FY2028 (Jan-28)", "301,690.50"), ("Lowest in FY2029 (Jan-29)", "422,470.50")]
    assert "SGD, month-end; FY2027 = Dec-27, FY2028 = Dec-28, FY2029 = Dec-29" in captions(at)
    drill = rows(at, 4)
    assert drill[0]["Line"] == "Total units" and drill[0]["Dec-29"] == "3,000"  # drill-down has every month

    at.checkbox(key="plan_all_months").check().run()
    assert_clean(at)
    pnl = rows(at, 1)
    assert len(pnl[0]) == 1 + 3 + 36 + 1 and pnl[0]["Dec-29"] == "36,000.00"


def test_planning_exports_a_month_as_a_budget_submission():
    at = planning_page(plan=PLAN36)
    at.selectbox(key="plan_budget_period").set_value("2027-03").run()
    assert_clean(at)
    assert find_rows(at, Account="COGS") == [{"Account": "COGS", "Plan line": "COGS", "Amount": "10,000.00"}]
    assert [r["Amount"] for r in find_rows(at, **{"Plan line": "Revenue"})] == ["24,000.00"]
    version = app.load_plan(Path(PLAN36)).version
    [download] = at.download_button
    assert shown(download.label) == f"Download budget_SG_2027-03_agency_2027_36m_{version}_base.xlsx"
    assert "Left out: Interest (250.00), Tax (756.50)." in captions(at)

    at.selectbox(key="plan_scenario").set_value("delayed_hire").run()
    at.selectbox(key="plan_budget_period").set_value("2027-04").run()
    assert_clean(at)
    assert shown(at.download_button[0].label).endswith("_delayed_hire.xlsx")
    assert [r["Amount"] for r in find_rows(at, Account="PAY")] == ["3,300.00"]


def test_planning_budget_export_refusal_is_shown_in_place(tmp_path):
    aud = tmp_path / "aud.toml"
    aud.write_text(Path(PLAN36).read_text(encoding="utf-8").replace('currency = "SGD"', 'currency = "AUD"'),
                   encoding="utf-8")
    at = planning_page(plan=str(aud))
    assert errors(at) == ["the plan is in AUD but SG reports in SGD; a budget submission must be in the market's "
                          "currency"]
    assert not at.download_button and at.dataframe  # the statements still show


def test_submit_offers_the_regional_template():
    at = load("E1", "Submit a file")
    assert [shown(b.label) for b in at.download_button] == ["Download submission_template.xlsx"]


def test_close_controls_offer_the_templates_and_preview_an_upload_before_import(tmp_path):
    at = load("E1", "Close controls")
    assert [shown(b.label) for b in at.download_button] == [
        "Download accruals_template.xlsx", "Download invoices_template.xlsx", "Download gl_template.xlsx"]
    upload(at, 0, CONTROLS / "accruals_2026-03.csv")
    assert_clean(at)
    [a001] = find_rows(at, accrual_id="A-001")
    assert (a001["Row"], a001["amount"], a001["owner"]) == ("2", "100,000.00", "SG finance analyst")
    assert "Would import as accruals 2026-03 (6 rows)." in texts(at)
    assert "No accruals imported yet." in texts(at), "the preview imports nothing"

    bad = tmp_path / "gl.csv"
    bad.write_text("journal_id,period,market,account,amont,reference,description\n", encoding="utf-8")
    upload(at, 2, bad)
    assert_clean(at)
    assert find_rows(at, Column="amount", Found="MISSING") == [
        {"Column": "amount", "Read from": "amount", "Found": "MISSING", "Closest header": "amont"}]
    assert any(shown(w.value).startswith("Would be refused:\n  - gl.csv is missing gl column amount "
                                         '(closest header "amont")') for w in at.warning)


def test_replacing_an_uploaded_pack_hides_the_downloads_prepared_from_the_old_one(tmp_path):
    at = load("B2", "Releases and downloads")
    at.selectbox(key="deck_pack").set_value(app.CUSTOM_PACK).run()
    first = tmp_path / "mine.toml"
    first.write_text('name = "Mine"\n[[slides]]\ntype = "bridge"\nkpi = "revenue"\n', encoding="utf-8")
    upload(at, 0, first)
    button(at, "Prepare downloads").click().run()
    assert_clean(at)
    assert shown(at.download_button[0].label) == "Download Mine_2026-10_2026-10-R2.pptx"
    second = tmp_path / "second.toml"
    second.write_text('name = "Mine"\n[[slides]]\ntype = "bridge"\nkpi = "ebitda"\n', encoding="utf-8")
    upload(at, 0, second, name="mine.toml")
    assert_clean(at)
    assert not at.download_button, "the prepared deck came from the replaced pack file"
    button(at, "Prepare downloads").click().run()
    [slide] = Presentation(at.session_state["downloads"]["files"][0]).slides
    assert slide.shapes.title.text_frame.text == "EBITDA bridge, budget to actual"


def test_the_extension_rule_is_the_controls_one():
    assert controls.source_format("gl.XLSM") == "xlsx" and controls.source_format("a.csv") == "csv"
    assert controls.source_format("a.txt") is None
    assert not hasattr(app, "FORMATS"), "the UI uses controls.source_format, not its own table"
    assert controls.UPLOAD_TYPES == ("csv", "xlsx", "xlsm")


def april_plan(tmp_path) -> Path:
    april = tmp_path / "april.toml"
    april.write_text(Path(PLAN36).read_text(encoding="utf-8")
                     .replace("months = 36", "months = 36\nfiscal_year_start = 4")
                     .replace('annual_units = { "2028" = "30000", "2029" = "36000" }',
                              'annual_units = { "2027" = "30000" }'),
                     encoding="utf-8")
    return april


def test_planning_part_years_are_named_and_their_columns_show_the_months(tmp_path):
    at = planning_page(plan=str(april_plan(tmp_path)))
    assert_clean(at)
    assert list(rows(at, 1)[0])[:5] == ["Line", "FY2026 (3 mo)", "FY2027", "FY2028", "FY2029 (9 mo)"]
    assert ("Part years: FY2026 has 3 months (Jan-27 to Mar-27); FY2029 has 9 months (Apr-29 to Dec-29)."
            in captions(at))


def test_planning_seasonality_index_has_no_annual_total():
    at = planning_page(plan=PLAN36)
    at.selectbox(key="plan_drill").set_value("P&L: Revenue").run()
    assert_clean(at)
    [index] = [r for r in rows(at, 4) if r["Line"] == "Seasonality index"]
    assert (index["FY2027"], index["FY2028"], index["FY2029"]) == ("", "", "")
    assert index["Jan-27"] == "1"
