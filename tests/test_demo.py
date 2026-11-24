"""demo.replay reaches the same state as the CLI commands scripts/demo.sh runs."""

from pathlib import Path

import pytest

from finance_close_toolkit import cli, demo
from finance_close_toolkit.store import Store

EX = demo.EXAMPLES
P = demo.PERIOD
LEAD = demo.APPROVER


def ingest(name: str, market: str, scenario: str, received: str) -> list[str]:
    return ["ingest", str(EX / name), "--market", market, "--period", P, "--scenario", scenario,
            "--received-at", received]


# The state-changing commands of scripts/demo.sh, in its order.
DEMO_SH = [
    ingest("sg_actual_v1.csv", "SG", "actual", "2026-11-04T10:00"),
    ingest("sg_budget.csv", "SG", "budget", "2026-11-01T09:00"),
    ingest("au_budget.xlsx", "AU", "budget", "2026-11-01T09:00"),
    ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00"),
    ingest("au_actual_v1.xlsx", "AU", "actual", "2026-11-07T15:00"),
    ["publish", "--period", P, "--approve", "--by", LEAD],
    ingest("au_actual_v2.xlsx", "AU", "actual", "2026-11-12T11:00"),
    "map 64000 to OTH",
    ["revalidate", "--period", P],
    ["publish", "--period", P, "--approve", "--by", LEAD],
    ingest("sg_actual_v2.csv", "SG", "actual", "2026-11-14T09:00"),
]


def state(store_path: Path) -> dict:
    with Store.open(store_path) as store:
        return {
            "releases": [(r.release_id, r.previous_release_id, r.facts_checksum, r.slots, r.kpi_totals)
                         for r in store.releases(P)],
            "current": store.current_release(P).release_id,
            "counts": {t: store.count(t) for t in ("submissions", "receipts", "validations", "release_facts")},
            "latest": [store.latest_submission(m, P, s) for m in ("SG", "AU") for s in ("actual", "budget")],
        }


def test_replay_matches_the_cli_story(tmp_path):
    cli_dir, lib_dir = tmp_path / "cli", tmp_path / "lib"
    config = demo.prepare(cli_dir)
    for command in DEMO_SH:
        if command == "map 64000 to OTH":
            with open(cli_dir / "config" / "mappings" / "au.toml", "a", encoding="utf-8", newline="\n") as f:
                f.write('"64000" = "OTH"\n')
            continue
        assert cli.main([*command, "--store", str(cli_dir / "store.duckdb"), "--config", str(config)]) == 0

    demo.prepare(lib_dir)
    with Store.open(lib_dir / "store.duckdb") as store:
        assert demo.replay(store, lib_dir, "B2").id == "B2"

    cli_state, lib_state = state(cli_dir / "store.duckdb"), state(lib_dir / "store.duckdb")
    assert [r[0] for r in lib_state["releases"]] == ["2026-10-R1", "2026-10-R2"]
    assert cli_state == lib_state


def test_replay_stops_at_the_step_asked_for(tmp_path):
    demo.prepare(tmp_path)
    with Store.open(tmp_path / "store.duckdb") as store:
        demo.replay(store, tmp_path, "E7")
        assert [r.release_id for r in store.releases(P)] == ["2026-10-R1"]
        assert store.latest_submission("AU", P, "actual").file_name == "au_actual_v2.xlsx"


def test_unknown_step_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown demo step 'E99'"):
        demo.step("E99")
