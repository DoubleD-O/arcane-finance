import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import ConfigError

# What fits on the deck's outlook and actions slide without running into the table or the footer.
MAX_OUTLOOK_CHARS = 600
MAX_ACTIONS = 6
MAX_ACTION_CHARS = 120


@dataclass(frozen=True)
class Action:
    action: str
    owner: str
    due: date


@dataclass(frozen=True)
class Commentary:
    """Outlook and actions for a deck (`commentary/<period>.toml`). Plain text, never executed."""

    outlook: str
    actions: tuple[Action, ...]


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected text, got {value!r}")
    return value.strip()


def parse_commentary(text: str, where: str) -> Commentary:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{where}: {e}") from None
    outlook = _text(raw.get("outlook"), f"{where} outlook")
    if len(outlook) > MAX_OUTLOOK_CHARS:
        raise ConfigError(
            f"{where}: outlook is {len(outlook)} characters; at most {MAX_OUTLOOK_CHARS} fit on the slide"
        )
    raw_actions = raw.get("actions", [])
    if len(raw_actions) > MAX_ACTIONS:
        raise ConfigError(f"{where}: {len(raw_actions)} actions; at most {MAX_ACTIONS} fit on the slide")
    actions = []
    for i, a in enumerate(raw_actions, 1):
        due = a.get("due")
        try:
            due = due if isinstance(due, date) else date.fromisoformat(_text(due, f"{where} action {i} due"))
        except ValueError:
            raise ConfigError(f"{where} action {i}: due must be a date (YYYY-MM-DD), got {due!r}") from None
        action = _text(a.get("action"), f"{where} action {i}")
        if len(action) > MAX_ACTION_CHARS:
            raise ConfigError(
                f"{where}: action {i} is {len(action)} characters; at most {MAX_ACTION_CHARS} fit on the slide"
            )
        actions.append(Action(action, _text(a.get("owner"), f"{where} action {i} owner"), due))
    return Commentary(outlook, tuple(actions))


def load_commentary(path: Path) -> Commentary:
    return parse_commentary(Path(path).read_text(encoding="utf-8"), str(path))
