# Logins: real people instead of typed-in roles

Today every action records a role the user types or picks ("Acting as", `--by`, `--approved-by`). Approval rules (approver differs from actor, approver roles, segregation of duties in close controls) therefore stop honest mistakes but not misuse. This step makes each action belong to a signed-in person.

## Modes

- **Open mode** (no users exist in the store): today's behaviour, unchanged, so the demo, the tests and a first look still work. The UI and CLI say so plainly: "No users yet: anyone can act as any role. Add the first user with `fct users add … --role admin`."
- **Signed-in mode** starts as soon as the first user exists in the store and cannot be switched off except by removing every user (admin only, with a confirmation). Every state-changing action then needs a signed-in user.

## Users and roles

- A **user**: username (lower case, unique), display name, one or more roles, active flag, password hash, created by/at, password changed at.
- **Roles** are the role names already in config (market owners, the regional owner, `[controls]` roles and approver roles) plus `admin`. A user acts in one of their roles at a time; the role is chosen in the UI sidebar or with `--as ROLE` on the CLI (defaulted when the user has exactly one role). Every rule that today checks a role string now checks "the signed-in user holds this role and is acting in it".
- **Admin** manages users: `fct users add|list|disable|enable|set-roles|reset-password`. The first user must include `admin`. A user can change their own password (`fct users passwd`). There must always be at least one active admin.
- **Admin and approver are kept apart** (the owner's default): a user holding `admin` cannot also hold an approver role, and an approver role cannot be given to an admin; `users add` and `users set-roles` refuse it and say why. An admin can reset any password, so approvals stay with people who do not manage users.
- Passwords: at least 12 characters; stored as `hashlib.scrypt` (n=2**14, r=8, p=1, 16-byte random salt), never logged or echoed. After 5 failed attempts the account locks for 15 minutes; a wrong current password in `users passwd` counts as a failed sign-in. No recovery: an admin resets it.

## Signing in

- **UI**: a sign-in form when signed-in mode is on; the sidebar shows the person, their acting role and Sign out. The "Acting as" free choice is replaced by the user's own roles. The UI still listens on this computer only.
- **CLI**: `fct login` (password from a prompt, or the `FCT_PASSWORD` environment variable for scripts), `fct logout`, `fct whoami`. A session token (32 random bytes; only its hash stored) lives in `~/.arcane-finance/sessions/` per store and expires after 12 hours. A token file that does not hold a token (corrupt, not ASCII) reads as signed out; login and logout still work.
- **Ending sessions**: a password change or reset, disabling a user and removing one of their roles end every session of that user; enabling them again does not revive an old token. Each sign-in purges expired sessions and any left for a user who is gone or disabled.
- **UI fails closed**: open mode is shown only when the store has no users or does not exist yet. Any other failure to read the account shows an error and no page. Signed out, the sidebar shows neither the page list nor the store path.

## Two-person rules

Where a second person must approve (a close-controls resolution, an override link, a large variance explanation), the approval must come from that person's own sign-in:

- The first person **requests** the action; it goes into an **Approvals** queue with everything the approver needs to see (the item, amount, evidence, reason, requester).
- An eligible approver (holds an approver role, is not the requester, and is not acting in the item owner's role) **approves or rejects** it from their own session: UI Approvals tab, or `fct approvals list|approve ID|reject ID --reason`.
- `--approved-by` is refused in signed-in mode ("approval must come from the approver's own sign-in"). In open mode it works as today.
- **Facts bound to a request**: each request stores the facts its approver is shown. Resolve: issue, amount at risk, accrued, invoiced, reversal. Explain: accrued, invoiced, difference. Override link: the invoice (supplier, amount), the accrual (supplier, amount), the mismatched fields and the current link. Approving recomputes them; if any differs the approval is refused ("The facts changed since the request: invoiced 110,000.00 → 190,000.00; … Reject it and request again.") and the request stays pending so it can be rejected.
- **Nothing to record**: if an approved action is already in place (a no-op), the approval is still recorded as a decision and closes the request, and says so.
- Publishing a release needs a user acting in the regional owner role; one person is enough, as today.

## Audit

Every recorded action stores the username, display name and acting role; releases record "approved by Name (role)". History views show the person, not just the role. Events recorded in open mode keep their role-only form and are shown as such.

**Recorded and effective time.** In signed-in mode the store stamps every receipt, controls import, controls event, request and decision with the real clock (`logged_at`; `identity.clock`, replaceable as `Store.clock` in tests). `--as-of` and `--received-at` may still be given: they are kept in the existing time columns as the effective (claimed) time, and everything derived from time (states as of a moment, which file is latest) keeps using it, so the stories' figures do not change. History, the approvals list and the received column show "recorded 2026-11-27 09:00, effective 2026-05-04 09:00" when the two differ by a minute or more. Open mode records no separate clock and shows what it showed before. `fct publish` and the UI take no claimed time, so a release published there is approved at the clock time.

**Console output** on a console that cannot show a character (Windows cp1252) prints a plain stand-in (`→` as `->`) instead of failing part-way through a command.

## Tests

- All existing tests stay green unchanged (open mode).
- Signed-in mode: action without sign-in refused; wrong password; lockout and unlock after 15 minutes (clock injectable); role not held refused; disabled user refused; session expiry; last admin cannot be disabled; first user must be admin.
- Two-person: requester cannot approve their own request; approver in the owner's role refused; approve and reject paths recorded with both people; `--approved-by` refused in signed-in mode.
- The C1–C10 close-controls story and the E-story replayed in signed-in mode with real users produce the same states and figures as in open mode.
- AppTest: sign-in, wrong password, role picker limited to own roles, Approvals tab flow.
