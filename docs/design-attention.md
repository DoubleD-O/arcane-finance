# Needs-attention workflow: acknowledge, assign, due dates, reopen

Today a needs-attention item is derived on every read and simply disappears when its condition clears. Nobody can say "I've seen this, I'm on it", hand it to a colleague, set a deadline, or see that a problem came back. This step adds that workflow on top of the derived items, for both the close board (missing submission, failed validation, material change) and close controls (invoice variance, partial invoice, missing reversal, ambiguous or mismatched match, reversal mismatch, no owner).

## Principle

The **condition** stays derived from the data, exactly as now: the workflow never hides, invents or changes a signal, a figure or a refusal. The workflow only adds **who is handling it, by when, and what they said**, as append-only events.

## When observations are recorded

Reads are pure. `status`, `attention list|show`, `controls status|show` and every UI page fold the stored events up to their as-of time and overlay, in memory only, what the data shows at that time that nobody has recorded yet. A read writes nothing, signed in or out, and a read's `--as-of` never writes, so looking at an earlier day cannot clear, reopen or drop anything.

Observations (appeared, new evidence, cleared) are recorded by the operations that change the data, in the same transaction, stamped with the data's effective time and, in signed-in mode, the person acting and the real time it was logged:

| Operation | Effective time |
|---|---|
| `ingest`, including a re-receipt of older bytes and a revalidation it triggers | the receipt time (`--received-at`) |
| `revalidate` | when it runs (the store's clock) |
| `controls import` | the import's `--as-of` |
| close-controls actions (link, explain, assign, resolve, drop) | the action's `--as-of` |
| approval requests and decisions | the request or decision time |
| `publish --approve` | the release's approval time |
| an attention action (ack, assign, due, comment) on an item only a read had seen | the action's `--as-of` |

An observation compares the data as it now stands with the recorded state of every item, so a backdated operation never clears or reopens an item because of data that arrived "later". A config-only change (mapping, FX rates, materiality, owners, `due_days`) changes no data, so it is recorded at the next data-changing operation; until then reads show its effect as "not recorded yet". `fct attention refresh` records it explicitly at the real time (signed in, it needs a sign-in). Repeated observations of the same change, for example from two writers at once, fold into one: an "appeared" on an open item, a "cleared" on a resolved one and "new evidence" with the same fingerprint change nothing. Event numbers come from a database sequence, so concurrent writers never collide.

Effective times can be out of order in an item's history when a receipt is backdated (the SG budget received "on 1 November" after the SG actual of 4 November); history lists events in the order they were recorded.

## Identity of an item

- **Key** = source + type + scope: close board `(period, type, market, scenario)`; close controls `(type, accrual or invoice id)`. The key is stable while the problem persists, so repeated imports never create duplicates.
- **Occurrence**: each time a key appears after having cleared, it is a new occurrence, numbered. Evidence fingerprint = the data the signal is derived from (for example the latest submission and validation, or the accrual's invoices and reversal lines). When the fingerprint changes while the item is open, the item records "new evidence" and an earlier acknowledgement lapses.

## States (derived from the condition plus events)

| State | Meaning |
|---|---|
| **New** | Condition present, nobody has acknowledged this occurrence |
| **Acknowledged** | Someone acknowledged it (with an optional note) and the evidence has not changed since |
| **Resolved** | Condition no longer present; shows when it cleared and the last person who acted |
| **Reopened** | Condition present again after being resolved (a new occurrence); behaves like New, with the history of the earlier occurrence attached |

Any item can additionally be **overdue**: its due date has passed and it is not resolved.

## Actions

| Action | Who | Effect |
|---|---|---|
| Acknowledge (note optional) | the item's owner role or the close lead | New/Reopened → Acknowledged |
| Assign to a person | the close lead, or the current (eligible) assignee handing over | Sets the assignee (a user who holds a suitable role; in open mode, a role) |
| Set or change the due date | the close lead | Defaults: the close calendar due date for close-board items, or, for one that first appears after that date, the day it appeared + `attention.followup_days` (config, default 2 working days) so it is not overdue at birth; the period close date + `controls.due_days` (config, default 5 working days) for controls items |
| Comment | anyone signed in | Adds a note to the item's history |

Working days skip Saturdays and Sundays; zero working days after a weekend day is the next Monday.

An assignee who is later disabled or no longer holds the item's owner or lead role shows as "assignee no longer eligible": the item counts as unassigned in `--mine` and My items, and only the close lead can reassign it.

Nothing here can mark an item resolved: only the data can (a valid file, a ledger line, an approved resolution in close controls). Every action is recorded with the person, role and real time (logins rules), refused with a readable message when not allowed, and idempotent.

## Surfaces

- **CLI**: `fct attention list [--mine] [--overdue] [--state S]`, `fct attention show KEY`, `ack KEY [--note]`, `assign KEY --to USER`, `due KEY --date YYYY-MM-DD`, `comment KEY --note`, `refresh [--period]` (records what a config-only change did; no `--as-of`, the real time). Close board `status` and `controls status` show each item's state, assignee and due date.
- **UI**: on the Close board and the Close controls page, each card shows state, assignee, due date (overdue in words) and the actions the signed-in person may take; a **My items** view lists what is assigned to me, overdue first.
- **Release manifest** records, for the period, the items open at approval with their state, so leadership can see what was acknowledged but still open when the figures were signed off.

## Tests

- No signal, figure or refusal changes anywhere: all existing tests stay green.
- Repeated imports of the same data create no duplicate items.
- E-story: AU missing at E2 → acknowledged by the AU controller → resolved at E4 when the file arrives; VALIDATION_FAILED at E7 → assigned → resolved at E10; the MATERIAL_CHANGE at E11 resolves at E13 on approval.
- Reopen: a cleared item that returns is a new occurrence (Reopened) with the old history attached; new evidence on an acknowledged item lapses the acknowledgement.
- Overdue with an injectable clock; due-date defaults, including the follow-up rule and zero working days on a weekend; permissions for each action, signed in and open mode; an ineligible assignee counts as unassigned.
- Reads are pure: as-of reads leave the event table byte-identical; signed-out reads write nothing; resolved-at is the data change's time; a problem that appears and clears between reads is recorded; two concurrent writers do not collide; a config-only change shows as not recorded until `fct attention refresh`.
- AppTest: cards show state, assignee and due date; acknowledge and assign from the UI; My items.
