"""Users, passwords, sessions and the Actor every state-changing call receives (docs/design-logins.md).

Open mode: the store has no users, so an Actor is a role someone typed, as before. Signed-in mode starts with the
first user; every action then needs an Actor with a person, built by `act` from a signed-in user's own roles.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .config import Workspace
    from .store import Store

ADMIN = "admin"
MIN_PASSWORD = 12
LOCK_AFTER = 5
LOCK_FOR = timedelta(minutes=15)
SESSION_FOR = timedelta(hours=12)
OPEN_MODE = ("No users yet: anyone can act as any role. Add the first user with "
             "`fct users add USERNAME --name NAME --role admin`.")
SIGN_IN_FIRST = "Sign in first: this store has users, so every action needs a signed-in user (fct login)."
OWN_SIGN_IN = "Approval must come from the approver's own sign-in; it cannot be given with --approved-by."
_USERNAME = re.compile(r"[a-z][a-z0-9._-]{1,31}")
_TOKEN = re.compile(r"[0-9a-f]{64}")
_SCRYPT = dict(n=2**14, r=8, p=1)


class IdentityError(ValueError):
    """A readable refusal: nothing was changed."""


@dataclass(frozen=True)
class Person:
    username: str
    display_name: str


@dataclass(frozen=True)
class Actor:
    """Who is acting: a role, and in signed-in mode the person acting in it."""

    role: str
    person: Person | None = None

    @property
    def label(self) -> str:
        return f"{self.person.display_name} ({self.role})" if self.person else self.role


def actor(by: Actor | str | None) -> Actor:
    """A bare role string is the open-mode form: a role with no person."""
    return by if isinstance(by, Actor) else Actor(by or "")


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    roles: tuple[str, ...]
    active: bool
    created_by: str
    created_at: datetime
    password_changed_at: datetime

    @property
    def person(self) -> Person:
        return Person(self.username, self.display_name)


@dataclass(frozen=True)
class Credentials:
    password_hash: str
    failed_sign_ins: int
    locked_until: datetime | None


@dataclass(frozen=True)
class UserEvent:
    """An admin action on users, append-only."""

    kind: str
    username: str
    by: Actor
    at: datetime
    detail: str = ""


def clock() -> datetime:
    """The real clock. In signed-in mode the store stamps every action and receipt with it, beside the time the
    action claims (--as-of, --received-at), so a claimed time is shown but never hides when it was recorded."""
    return datetime.now().replace(microsecond=0)


def when_text(at: datetime, logged_at: datetime | None = None) -> str:
    """The effective time, and the recorded one beside it when they differ by a minute or more."""
    if logged_at is None or abs(logged_at - at) < timedelta(minutes=1):
        return f"{at:%Y-%m-%d %H:%M}"
    return f"recorded {logged_at:%Y-%m-%d %H:%M}, effective {at:%Y-%m-%d %H:%M}"


# Passwords


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    _, n, r, p, salt, digest = encoded.split("$")
    computed = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(computed, bytes.fromhex(digest))


# Compared against when the username is unknown, so a wrong username costs the same time as a wrong password.
_DUMMY_HASH = hash_password("not a password", b"\0" * 16)


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise IdentityError(f"The password must be at least {MIN_PASSWORD} characters.")


# Modes and roles


def signed_in_mode(store: Store) -> bool:
    return store.user_count() > 0


def sign_in_refusal(store: Store, by: Actor) -> str | None:
    """Why `by` may not act on this store, or None."""
    return SIGN_IN_FIRST if by.person is None and signed_in_mode(store) else None


def require_signed_in(store: Store, by: Actor | str | None) -> Actor:
    a = actor(by)
    if refusal := sign_in_refusal(store, a):
        raise IdentityError(refusal)
    return a


def known_roles(ws: Workspace) -> tuple[str, ...]:
    """The role names in config: market owners, the regional owner, [controls] roles; then admin."""
    cfg = ws.config
    names = [m.owner for m in cfg.markets] + [cfg.regional_owner]
    if cfg.controls is not None:
        names += [*cfg.controls.owners, *cfg.controls.approver_roles]
    return tuple(dict.fromkeys([*names, ADMIN]))


def act(user: User, role: str | None = None) -> Actor:
    """The user acting in one of their own roles; `role` may be left out when they hold exactly one."""
    if not user.active:
        raise IdentityError(f"{user.username} is disabled.")
    if not role:
        if len(user.roles) != 1:
            raise IdentityError(f"{user.username} holds several roles ({', '.join(user.roles)}); choose one "
                                "with --as ROLE.")
        role = user.roles[0]
    if role not in user.roles:
        raise IdentityError(f"{user.username} does not hold the role {role!r}; their roles: "
                            f"{', '.join(user.roles)}.")
    return Actor(role, user.person)


# Users


def _roles(ws: Workspace, roles: Iterable[str], held: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Known roles, and never admin with an approver role: an admin can reset any password, so approvals stay
    with other people. `held` are the user's roles before this change, which pick the refusal's wording."""
    roles = tuple(dict.fromkeys(r.strip() for r in roles if r.strip()))
    if not roles:
        raise IdentityError("Give at least one role (--role).")
    known = known_roles(ws)
    unknown = [r for r in roles if r not in known]
    if unknown:
        raise IdentityError(f"Unknown role {unknown[0]!r}; roles: {', '.join(known)}.")
    cfg = ws.config.controls
    approver = [r for r in roles if cfg is not None and r in cfg.approver_roles]
    if ADMIN in roles and approver:
        why = "an admin can reset any password, so approvals stay with people who do not manage users."
        if ADMIN in held:
            raise IdentityError(f"An approver role ({approver[0]}) cannot be given to an admin: {why}")
        if any(r in held for r in approver):
            raise IdentityError(f"A user holding an approver role ({approver[0]}) cannot also be made admin: "
                                f"{why}")
        raise IdentityError(f"A user holding admin cannot also hold an approver role ({approver[0]}): {why}")
    return roles


def _require_admin(store: Store, by: Actor) -> None:
    if by.person is None:
        raise IdentityError(SIGN_IN_FIRST)
    if by.role != ADMIN:
        raise IdentityError(f"Only an admin manages users; {by.label} is not acting as {ADMIN}.")


def _user(store: Store, username: str) -> User:
    user = store.user(username)
    if user is None:
        raise IdentityError(f"No user {username!r}.")
    return user


def add_user(store: Store, ws: Workspace, by: Actor | str | None, username: str, display_name: str,
             roles: Iterable[str], password: str, now: datetime | None = None) -> User:
    """The first user needs no sign-in and must hold admin; after that only an admin adds users."""
    now = now or datetime.now().replace(microsecond=0)
    by = actor(by)
    first = not signed_in_mode(store)
    if not first:
        _require_admin(store, by)
    if not _USERNAME.fullmatch(username):
        raise IdentityError("A username is 2 to 32 characters: lower-case letters, digits, '.', '_' or '-', "
                            "starting with a letter.")
    if store.user(username) is not None:
        raise IdentityError(f"User {username!r} already exists.")
    display_name = display_name.strip()
    if not display_name:
        raise IdentityError("Give the person's name (--name).")
    roles = _roles(ws, roles)
    if first and ADMIN not in roles:
        raise IdentityError(f"The first user must include the {ADMIN} role, so someone can manage users.")
    _check_password(password)
    user = User(username, display_name, roles, True, by.person.username if by.person else "(first user)", now, now)
    store.add_user(user, hash_password(password), UserEvent("add", username, by, now, ", ".join(roles)))
    return user


def _active_admins(store: Store) -> list[str]:
    return [u.username for u in store.users() if u.active and ADMIN in u.roles]


def _keep_an_admin(store: Store, username: str) -> None:
    if _active_admins(store) == [username]:
        raise IdentityError(f"{username} is the last active admin; there must always be one.")


def set_active(store: Store, by: Actor, username: str, active: bool, now: datetime | None = None) -> User:
    now = now or datetime.now().replace(microsecond=0)
    _require_admin(store, by)
    user = _user(store, username)
    if user.active == active:
        return user
    if not active:
        _keep_an_admin(store, username)
    changed = replace(user, active=active)
    store.update_user(changed, UserEvent("enable" if active else "disable", username, by, now),
                      end_sessions=not active)
    return changed


def set_roles(store: Store, ws: Workspace, by: Actor, username: str, roles: Iterable[str],
              now: datetime | None = None) -> User:
    now = now or datetime.now().replace(microsecond=0)
    _require_admin(store, by)
    user = _user(store, username)
    roles = _roles(ws, roles, user.roles)
    if ADMIN in user.roles and ADMIN not in roles and user.active:
        _keep_an_admin(store, username)
    changed = replace(user, roles=roles)
    store.update_user(changed, UserEvent("set_roles", username, by, now, ", ".join(roles)),
                      end_sessions=not set(user.roles) <= set(roles))
    return changed


def reset_password(store: Store, by: Actor, username: str, password: str, now: datetime | None = None) -> None:
    """An admin sets a new password; it also lifts a lockout and ends the user's sessions. There is no other
    recovery."""
    now = now or datetime.now().replace(microsecond=0)
    _require_admin(store, by)
    _user(store, username)
    _check_password(password)
    store.set_password(username, hash_password(password), now, UserEvent("reset_password", username, by, now))


def change_password(store: Store, username: str, old: str, new: str, now: datetime | None = None) -> None:
    """A wrong current password counts toward the lockout as a wrong sign-in does. Ends the user's sessions."""
    now = now or datetime.now().replace(microsecond=0)
    user = _user(store, username)
    _verify(store, username, old, now, "The current password is wrong. Nothing changed.")
    _check_password(new)
    store.set_password(username, hash_password(new), now,
                       UserEvent("change_password", username, Actor("", user.person), now))


def remove_all_users(store: Store, by: Actor, now: datetime | None = None) -> int:
    """Back to open mode: removes every user and session. The caller confirms first."""
    now = now or datetime.now().replace(microsecond=0)
    _require_admin(store, by)
    return store.remove_all_users(UserEvent("remove_all", "*", by, now))


# Signing in and sessions


def _verify(store: Store, username: str, password: str, now: datetime, wrong: str) -> None:
    """After LOCK_AFTER wrong passwords in a row the account is locked for LOCK_FOR, even to the right one."""
    creds = store.credentials(username)
    if creds is None:
        password_matches(password, _DUMMY_HASH)
        raise IdentityError(wrong)
    if creds.locked_until is not None and now < creds.locked_until:
        raise IdentityError(f"{username} is locked after {LOCK_AFTER} wrong passwords until "
                            f"{creds.locked_until:%Y-%m-%d %H:%M}; wait, or ask an admin to reset the password.")
    if not password_matches(password, creds.password_hash):
        failed = creds.failed_sign_ins + 1
        locked = now + LOCK_FOR if failed >= LOCK_AFTER else None
        store.record_sign_in_failure(username, 0 if locked else failed, locked)
        raise IdentityError(wrong + (f" {username} is now locked for 15 minutes." if locked else ""))
    if creds.failed_sign_ins or creds.locked_until:
        store.record_sign_in_failure(username, 0, None)


def sign_in(store: Store, username: str, password: str, now: datetime | None = None) -> User:
    now = now or datetime.now().replace(microsecond=0)
    username = username.strip().lower()
    _verify(store, username, password, now, "Wrong username or password.")
    user = _user(store, username)
    if not user.active:
        raise IdentityError(f"{username} is disabled; ask an admin.")
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def start_session(store: Store, user: User, now: datetime | None = None) -> str:
    """A new session token; only its hash is stored. The caller keeps the token and never shows it. Signing in
    also purges every expired session and any left for a user who is gone or disabled."""
    now = now or datetime.now().replace(microsecond=0)
    store.purge_sessions(now)
    token = secrets.token_hex(32)
    store.add_session(_token_hash(token), user.username, now, now + SESSION_FOR)
    return token


@dataclass(frozen=True)
class Session:
    user: User
    expires_at: datetime


def session(store: Store, token: str | None, now: datetime | None = None) -> Session | None:
    """The session's user while it is unexpired and they are active; else None."""
    if not token:
        return None
    now = now or datetime.now().replace(microsecond=0)
    found = store.session(_token_hash(token))
    if found is None:
        return None
    username, expires_at = found
    user = store.user(username)
    if now >= expires_at or user is None or not user.active:
        return None
    return Session(user, expires_at)


def end_session(store: Store, token: str | None) -> None:
    if token:
        store.end_session(_token_hash(token))


# The CLI's session file, one per store, under the user's home


def sessions_dir() -> Path:
    return Path(os.environ.get("FCT_SESSIONS_DIR") or Path.home() / ".arcane-finance" / "sessions")


def session_file(store_path: str | Path) -> Path:
    key = hashlib.sha256(str(Path(store_path).resolve()).lower().encode("utf-8")).hexdigest()[:24]
    return sessions_dir() / f"{key}.session"


def save_token(store_path: str | Path, token: str) -> Path:
    """Written owner-only where the OS allows it (POSIX modes; Windows keeps the home folder's ACL)."""
    path = session_file(store_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(token)
    return path


def read_token(store_path: str | Path) -> str | None:
    """The saved token, or None when there is none or the file does not hold one (signed out, not a crash)."""
    try:
        token = session_file(store_path).read_bytes().strip().decode("ascii")
    except (OSError, UnicodeDecodeError):
        return None
    return token if _TOKEN.fullmatch(token) else None


def forget_token(store_path: str | Path) -> None:
    session_file(store_path).unlink(missing_ok=True)
