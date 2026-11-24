import importlib.util
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from finance_close_toolkit import attention, demo, release, validation
from finance_close_toolkit.config import Workspace, load_workspace
from finance_close_toolkit.store import Store

REPO = Path(__file__).resolve().parent.parent
PERIOD = "2026-10"
LEAD = "Regional FP&A lead"
APPROVED_AT = datetime(2026, 11, 20, 9, 0)


@dataclass
class Close:
    root: Path
    store: Store

    @property
    def config_path(self) -> Path:
        return self.root / "config" / "close.toml"

    @property
    def ws(self) -> Workspace:
        return load_workspace(self.config_path)

    def file(self, name: str) -> Path:
        return self.root / "examples" / name

    def ingest(self, name: str, market: str, scenario: str, received: str) -> validation.IngestResult:
        return validation.ingest(
            self.store, self.ws, self.file(name), market, PERIOD, scenario, datetime.fromisoformat(received)
        )

    def board(self, as_of: str = "2026-11-20T09:00") -> attention.Board:
        return attention.board(self.store, self.ws, PERIOD, datetime.fromisoformat(as_of))

    def publish(self, approve: bool = False, **kw) -> release.PublishResult:
        return release.publish(
            self.store, self.ws, PERIOD, approved_by=LEAD if approve else None,
            now=APPROVED_AT, **kw,
        )

    def map_consulting_to_oth(self) -> None:
        with open(self.root / "config" / "mappings" / "au.toml", "a", encoding="utf-8", newline="\n") as f:
            f.write('"64000" = "OTH"\n')


def copy_inputs(root: Path) -> None:
    shutil.copytree(REPO / "config", root / "config")
    shutil.copytree(REPO / "fx", root / "fx")
    shutil.copytree(REPO / "examples" / PERIOD, root / "examples")


@pytest.fixture
def close(tmp_path: Path):
    copy_inputs(tmp_path)
    store = Store.open(tmp_path / "store.duckdb")
    yield Close(tmp_path, store)
    store.close()


def run_story(close: Close, through: str) -> None:
    """Apply every state-changing step of the October story up to and including `through`."""
    demo.replay(close.store, close.root, through, examples=close.root / "examples", now=APPROVED_AT)


def build_deck_template(path: Path, **kw) -> Path:
    """scripts/make_deck_template.py's `build`: the starter template, or a variant of it."""
    spec = importlib.util.spec_from_file_location("make_deck_template", REPO / "scripts" / "make_deck_template.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(path, **kw)


# A template unlike the starter in every way the deck must follow: 4:3, a serif theme font, dark red titles,
# a teal table accent, a footer area above the deck's lowest content, and a sample slide that must stay out.
TEST_TEMPLATE = dict(width_in=10, font="Georgia", title_color="5B0E2D", accent1="2F6B5E", title_slide_size=32,
                     footer_top_in=6.3, sample_slide=True)
