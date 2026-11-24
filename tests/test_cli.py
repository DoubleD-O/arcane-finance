"""The E1-E16 story once through `fct` argument parsing and printing."""

import json

from conftest import PERIOD, REPO, run_story

from finance_close_toolkit.cli import main
from finance_close_toolkit.store import Store


def test_story_through_cli(close, capsys, tmp_path):
    common = ["--store", str(close.root / "cli.duckdb"), "--config", str(close.config_path)]

    def fct(*args: str, code: int = 0) -> str:
        assert main([*args, *common]) == code, args
        return capsys.readouterr().out

    def ingest(name: str, market: str, scenario: str, received: str) -> str:
        return fct("ingest", str(close.file(name)), "--market", market, "--period", PERIOD,
                   "--scenario", scenario, "--received-at", received)

    ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-04T10:00")
    ingest("sg_budget.csv", "SG", "budget", "2026-11-01T09:00")
    ingest("au_budget.xlsx", "AU", "budget", "2026-11-01T09:00")

    out = fct("status", "--period", PERIOD, "--as-of", "2026-11-06")
    assert "1. MISSING_SUBMISSION  AU actual  (overdue)" in out
    assert "Owner:    AU country controller" in out

    assert "AU actual: no submission received" in fct("publish", "--period", PERIOD, code=1)

    assert "UNMAPPED" not in fct("inspect", str(close.file("au_actual_v1.xlsx")), "--market", "AU")
    ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00")
    assert ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00").startswith("Repeat:")

    out = fct("publish", "--period", PERIOD, "--approve", "--by", "Regional FP&A lead")
    assert "Published 2026-10-R1" in out
    assert "actual    APAC   1,430,000.00    756,400.00  221,600.00" in out
    assert "budget    APAC   1,407,600.00    744,400.00  191,000.00" in out

    out = ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00")
    assert "UNMAPPED_ACCOUNT row 9: source account 64000" in out
    assert "source 963,000.00 = mapped 948,000.00 + unmapped 15,000.00" in out
    assert "VALIDATION_FAILED" in fct("status", "--period", PERIOD)
    fct("publish", "--period", PERIOD, code=1)

    close.map_consulting_to_oth()
    assert "revalidated" in fct("revalidate", "--period", PERIOD)

    out = fct("status", "--period", PERIOD)
    assert "MATERIAL_CHANGE  AU actual" in out
    assert "actual    AU     EBITDA           51,600.00     38,700.00  -12,900.00   -25.00%  material" in out
    assert "actual    APAC   EBITDA          221,600.00    208,700.00  -12,900.00    -5.82%  material" in out

    out = fct("publish", "--period", PERIOD)
    assert "Dry run: would publish 2026-10-R2" in out
    assert "actual    AU      OTH      Other operating costs  -8,600.00  -21,500.00  -12,900.00" in out

    assert "Published 2026-10-R2 (previous 2026-10-R1)" in fct(
        "publish", "--period", PERIOD, "--approve", "--by", "Regional FP&A lead"
    )
    assert "2026-10-R1 verifies" in fct("verify-release", "2026-10-R1")
    fct("export", "2026-10-R1", "--out", str(tmp_path / "r1"))
    manifest = json.loads((tmp_path / "r1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kpi_totals"]["actual"]["APAC"]["ebitda"] == "221600"

    out = fct("diff", "--from", "2026-10-R1", "--to", "2026-10-R2")
    assert "-12,900.00    -5.82%  material" in out

    out = fct("trace", "--release", "2026-10-R1", "--market", "AU", "--account", "REV", "--scenario", "actual")
    assert "au_actual_v1.xlsx    4    500,000.00  AUD  0.86  430,000.00" in out


def test_unknown_release_is_a_clean_error(close, capsys):
    code = main(["verify-release", "2026-10-R9", "--store", str(close.root / "x.duckdb"),
                 "--config", str(close.config_path)])
    assert code == 2
    assert "no release '2026-10-R9'" in capsys.readouterr().err


def test_variance_deck_and_export_through_cli(close, capsys, tmp_path):
    run_story(close, "E13")
    common = ["--store", str(close.root / "store.duckdb"), "--config", str(close.config_path)]

    def fct(*args: str, code: int = 0) -> str:
        assert main([*args, *common]) == code, args
        return capsys.readouterr().out

    out = fct("variance", "--release", "2026-10-R2")
    assert "EBITDA                     208,700.00    191,000.00    17,700.00    9.27%  Favourable" in out
    assert "EBITDA %                       14.59%        13.57%     +1.03 pp           Favourable" in out
    assert "AU             (27,300.00)  Unfavourable" in out
    assert "Client, supplier and cost-center drill-downs: not available in this data." in out

    out = fct("variance", "--release", "2026-10-R2", "--market", "AU")
    assert "Other operating costs     (21,500.00)    (8,800.00)  (12,700.00)  -144.32%  Unfavourable" in out
    assert "EBITDA bridge" not in out

    decks = tmp_path / "decks"
    commentary = str(REPO / "examples" / "commentary" / "2026-10.toml")
    out = fct("deck", "--release", "2026-10-R2", "--out", str(decks), "--commentary", commentary)
    assert "Comex_2026-10_2026-10-R2.pptx (FINAL)" in out
    assert "Refused:" in fct("deck", "--release", "2026-10-R2", "--out", str(decks), code=1)
    assert "(DRAFT: no commentary)" in fct("deck", "--release", "2026-10-R2", "--out", str(decks), "--force")

    assert "release.xlsx" in fct("export", "2026-10-R2", "--out", str(tmp_path / "x"))
    assert (tmp_path / "x" / "release.xlsx").exists()


def test_proposed_variance_through_cli(close, capsys):
    run_story(close, "B1")
    common = ["--store", str(close.root / "store.duckdb"), "--config", str(close.config_path)]
    assert main(["variance", "--period", PERIOD, "--proposed", *common]) == 0
    out = capsys.readouterr().out
    assert out.startswith("DRAFT: proposed release for 2026-10, not approved (current release 2026-10-R2).")
    assert "EBITDA                     207,700.00" in out
    assert main(["variance", "--period", PERIOD, *common]) == 2


def test_publish_with_expect_refuses_a_proposal_that_changed(close, capsys):
    run_story(close, "E11")
    common = ["--store", str(close.root / "store.duckdb"), "--config", str(close.config_path)]
    close.store.close()
    assert main(["status", "--period", PERIOD, *common]) == 0
    assert ("Next:     Review the change, then approve it or ask the market to resubmit "
            "(review: fct publish --period 2026-10)") in capsys.readouterr().out
    assert main(["publish", "--period", PERIOD, *common]) == 0
    out = capsys.readouterr().out
    [checksum] = [line.split()[-1] for line in out.splitlines() if line.startswith("Facts checksum: ")]
    assert f'fct publish --period 2026-10 --approve --by "<role>" --expect {checksum}' in out

    examples = close.root / "examples"
    assert main(["ingest", str(examples / "sg_actual_v2.csv"), "--market", "SG", "--period", PERIOD,
                 "--scenario", "actual", "--received-at", "2026-11-14T09:00", *common]) == 0
    capsys.readouterr()
    approve = ["publish", "--period", PERIOD, "--approve", "--by", "Regional FP&A lead", *common]
    assert main([*approve, "--expect", checksum]) == 1
    out = capsys.readouterr().out
    assert "The proposal changed since you reviewed it; review it again." in out
    assert "Nothing written." in out
    with Store.open(close.root / "store.duckdb") as store:
        assert [r.release_id for r in store.releases(PERIOD)] == ["2026-10-R1"]


class FakeProcess:
    def __init__(self, cmd, env):
        self.cmd, self.env = cmd, env

    def poll(self):
        return None

    def wait(self):
        return 0


def run_ui(monkeypatch, *args: str) -> tuple[FakeProcess, list[str]]:
    started, opened = [], []
    monkeypatch.setattr("finance_close_toolkit.cli.subprocess.Popen",
                        lambda cmd, env: started.append(FakeProcess(cmd, env)) or started[-1])
    monkeypatch.setattr("finance_close_toolkit.cli.wait_for_port", lambda host, port, process: True)
    monkeypatch.setattr("finance_close_toolkit.cli.webbrowser.open", opened.append)
    assert main(["ui", *args]) == 0
    [process] = started
    return process, opened


def option(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


def test_ui_runs_streamlit_without_holding_the_store_open(monkeypatch, tmp_path, capsys):
    store, config = tmp_path / "ui.duckdb", tmp_path / "close.toml"
    process, opened = run_ui(monkeypatch, "--port", "8599", "--store", str(store), "--config", str(config))
    cmd = process.cmd
    assert cmd[1:4] == ["-m", "streamlit", "run"] and cmd[4].endswith("app.py")
    assert option(cmd, "--server.port") == "8599"
    assert option(cmd, "--server.maxUploadSize") == "20"
    assert (process.env["FCT_STORE"], process.env["FCT_CONFIG"]) == (str(store), str(config))
    assert not store.exists()
    assert opened == ["http://127.0.0.1:8599"]
    assert "no login" not in capsys.readouterr().err


def test_ui_listens_on_this_computer_only_and_never_waits_for_a_prompt(monkeypatch):
    process, _ = run_ui(monkeypatch)
    assert option(process.cmd, "--server.address") == "127.0.0.1"
    assert option(process.cmd, "--server.headless") == "true"
    assert option(process.cmd, "--browser.gatherUsageStats") == "false"


def test_ui_on_another_address_warns_that_there_is_no_login(monkeypatch, capsys):
    process, opened = run_ui(monkeypatch, "--host", "0.0.0.0", "--port", "8600", "--no-browser")
    assert option(process.cmd, "--server.address") == "0.0.0.0"
    assert "the UI has no login" in capsys.readouterr().err
    assert opened == []
