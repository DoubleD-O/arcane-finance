# Step 3 design: thin UI (Streamlit)

Scope: `docs/SCOPE.md` step 3. A browser UI over the existing library so a fresh user can run the close without the CLI. **No business logic in the UI**: every number, state and refusal comes from the existing modules (`attention`, `release`, `variance`, `intake`, `deck`, `xlsx`). If the UI needs something the library doesn't expose, add a small library function and test it there.

## Who and what

Primary user: the regional FP&A lead. Three uncoached tasks it must support (the acceptance test):
1. "Which market needs attention for October, why, and who owns it?"
2. "Check the EBITDA on the deck against the source file row it came from."
3. "AU resubmitted. Review what it changes and approve it."

## Pages (sidebar navigation, period selector shared)

1. **Close board.** Status table (market × scenario: status, file, received, exceptions) and Needs-attention cards (type, owner, due, overdue, trigger, impact, next action). Status always in words, never colour alone.
2. **Submit a file.** Upload CSV/XLSX → pick market, scenario (period from the sidebar) → **preview** (the `inspect` routing preview: the file's own extension, columns found or missing with closest match, accounts with mapping or UNMAPPED, tie-out) → **Ingest** button. If required columns are missing or the extension disagrees with the mapping's format, the preview warns and Ingest stays disabled until the user ticks an explicit "Ingest anyway" box, because the file becomes the slot's latest and blocks publish. Then show the validation result and exceptions in a box that matches the state: validated as success, needs review as warning, rejected as error. Received-at defaults to now; an "advanced" expander allows setting it (needed for the demo).
3. **Review and publish.** The proposed release vs the current one: refusal reasons if blocked, else KPI changes with Material / Below threshold, account changes, and the file behind each slot that changes. "Approved by" text field (required) and an **Approve and publish** button. The button carries the facts checksum of the proposal on screen to `release.publish(expected_checksum=...)`, which refuses ("The proposal changed since you reviewed it; review it again.") and writes nothing if a file arrived in between. After publishing: the new release ID.
4. **Results.** Release picker (default current). P&L vs budget for APAC with tabs per market; the EBITDA bridge as a waterfall (Altair, built from `VarianceReport.bridge`, no recomputation). **Drill-down**: pick market + account → the trace table (file, row, local amount, rate, SGD) from `release.trace`. Changes since previous release when present.
5. **Releases and downloads.** Release history (ID, approved by/at, previous, checksum, verify result on demand). For a chosen release: download deck (optional commentary upload, validated by the existing loader), `release.xlsx`, `facts.csv`, `manifest.json`. Diff any two releases.

Sidebar: store path and config path (defaults as in the CLI), period, and **Demo**: a "Load demo up to step…" selector (E1 … E16, B1, B2) that replays the scripted story into a fresh temporary store, so the walkthrough can start at any point. Move the replay logic out of `scripts/demo.sh` into a library function (`demo.replay(store, config_dir, until)`) and make the shell script call the CLI the same way it does now; both must produce the same state.

## Rules

- Launch: `fct ui` (runs `streamlit run` on the app module, headless with usage stats off so the first run never waits at a prompt, then opens the browser unless `--no-browser`) and documented in README. It listens on `127.0.0.1` only: the UI has no login. `--host` opts in to another address and prints a warning.
- Text from files, inputs and config (file names, approver, FX notes, button labels) goes through `esc()` or a plain-text widget, never raw Markdown. The period is validated before it appears in a heading.
- While a demo is loaded, the Close board's "As of" stays at the demo's as-of across pages. The "Demo loaded" notice clears once the user ingests or publishes.
- Uploads are untrusted: existing size and row limits apply; saved to a temp file keeping the original file name for lineage; never executed.
- Every action button is idempotent and shows the library's message verbatim; errors appear as a readable message, not a traceback.
- Numbers formatted with the existing `money` formatters (same strings as the CLI).
- Accessible: Streamlit's keyboard focus order follows page order; labels on every input; Favourable/Unfavourable and Material in text.

## Tests and verification

- `streamlit.testing.v1.AppTest` tests: each page renders against the demo store at B2 without exceptions; the close board shows the E2 signal at step E2; publish page shows the E11 delta and approving produces R2; results page shows APAC EBITDA 208,700 for R2 and the trace for AU REV row 4; downloads produce a pptx whose slide 2 EBITDA matches.
- `demo.replay` tested for equality with the CLI-driven story (release checksums equal).
- Run the app and drive it in a browser for the three tasks, with screenshots.
