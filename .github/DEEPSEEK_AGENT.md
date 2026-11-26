# DeepSeek issue triage agent

Two manual workflows that file tasks on a GitHub Projects (V2) board. Both are
`workflow_dispatch` only — nothing runs on push or on new issues — and both
default to a dry run.

| Workflow | Input it reads | Calls DeepSeek? |
| --- | --- | --- |
| DeepSeek issue triage | open GitHub issues | yes, one call per unseen issue |
| Marker tasks | `TODO` / `FIXME` comments and `[WIP]` headings | no, deterministic |

Files:

- `.github/workflows/deepseek-triage.yml` + `scripts/deepseek_triage.py`
- `.github/workflows/deepseek-markers.yml` + `scripts/scan_markers.py`
- both scripts are Python 3 standard library only, no dependencies

## One-time setup

### 1. Create a token for Projects V2

The built-in `GITHUB_TOKEN` is scoped to this repository and **cannot** read or
write Projects V2, regardless of the workflow's `permissions:` block. A separate
token is mandatory.

1. Go to **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token**.
2. Select the `project` scope (and `repo` if this repository is private).
3. If the project belongs to an organization with SAML SSO enabled, authorize the token for that organization.
4. Copy the token.

Fine-grained tokens can also work for organization projects, but the organization
must permit them and grant the token Projects read/write; the classic `project`
scope is the path of least resistance.

### 2. Store the secrets

Repository **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `DEEPSEEK_API_KEY` | key from <https://platform.deepseek.com> |
| Secret | `PROJECT_TOKEN` | the PAT from step 1 |
| Variable | `PROJECT_OWNER` | user or org that owns the board, e.g. `MaksimPRs` |
| Variable | `PROJECT_NUMBER` | number from the board URL `.../projects/4` → `4` |
| Variable | `DEEPSEEK_MODEL` | optional, defaults to `deepseek-chat` (`deepseek-reasoner` also works) |

### 3. Give the board the fields the agent writes to

- **Priority** — a single-select field. The agent maps DeepSeek's priority onto
  whatever options this field has (`High`/`Medium`/`Low`, `P0`-`P3`, …). If the
  field is absent, triage still works and priorities are only reported.
- **Status** — optional single-select. Set with the `--status` flag; the workflow
  does not pass it, so board automation keeps owning the initial status.

## Running it

**Actions → DeepSeek issue triage → Run workflow.**

| Input | Default | Meaning |
| --- | --- | --- |
| `dry_run` | `true` | Report what would happen, write nothing. Turn off to mutate the board. |
| `mode` | `board-items` | `board-items` adds the issue itself as a board item; `draft-tasks` files generated draft issues instead. |
| `max_issues` | `25` | Upper bound on issues triaged per run, keeping DeepSeek spend predictable. |
| `project` | blank | Optional `OWNER/NUMBER` override for the run. |

Run it with `dry_run: true` first and read the job summary — it lists every
issue with the decision, priority, and a one-line reason.

## What the agent does

1. Verifies the project and its fields are reachable (fails fast, before any API spend).
2. Reads open issues over REST; pull requests are ignored.
3. Loads existing board items once, so re-runs never duplicate work.
4. Sends each unseen issue to DeepSeek for a JSON verdict: actionable or not, priority, reason.
5. In `board-items` mode, adds the issue as a project item and sets Priority.
   In `draft-tasks` mode, asks DeepSeek to split the issue into at most three
   one-pull-request tasks, each filed as a draft issue titled `[#12] …` linking back.
6. Writes a markdown table to the workflow summary.

Idempotency: board mode skips issues already on the board, draft mode skips
draft titles it has already created. Re-running is safe.

## Cost and noise controls

- `max_issues` caps the number of DeepSeek calls per run (one call per issue).
- Non-actionable issues are skipped and reported, not converted into tasks; the
  prompt is deliberately conservative (questions, duplicates, and vague wishes
  are rejected).
- `deepseek-chat` is the default; `deepseek-reasoner` costs more but triages
  ambiguous issues better.

## Filing tasks from code markers

For work that is already written down in the code, use the marker scanner
instead of the issue triager. It calls no model: extraction is deterministic, so
it is free and gives the same result on every run.

**Actions → Marker tasks → Run workflow.**

`scripts/scan_markers.py` reads only git-tracked files (`target/` and vendored
crates are ignored) and recognises:

| Kind | Source | Task title |
| --- | --- | --- |
| `fixme`, `todo`, `xxx`, `hack` | `// FIXME …`, including trailing comments | the comment text |
| `wip` | markdown headings containing `[WIP]` | `Finish: <heading>` |

A comment tagged with a tracking key (`// FIXME [RAFTY-8] …`) is **skipped**: the
key means the issue already exists, so a generated task would duplicate it. The
job log lists every skip, e.g. `fixme:rafty/src/node_state.rs:167 references RAFTY-8; skipping`.

Each task body carries a permalink (`blob/<sha>/<path>#L<line>`) and a hidden
fingerprint comment. The fingerprint comes from kind + path + normalised text,
**not** the line number, so re-running after edits never duplicates tasks — and
one `FIXME` repeated on two adjacent lines becomes a single task.

Two destinations:

- default — project **draft issues** (no issue noise, nothing to close)
- `as_issues: true` — real repository issues, which are then added to the board

Caveats worth knowing:

- `todo!()` / `unimplemented!()` stubs are deliberately **not** scanned: several
  stubs are normally covered by one tracking issue (for example "implement
  `InMemoryCommandLog`"), so per-stub tasks would only duplicate it.
- Deleting a marker does **not** close or remove the task it created; the scan
  is append-only. Prune the board when you resolve markers.
- The markdown scan only looks at headings, so inline `(WIP)` mentions do not
  become tasks. Put `[WIP]` in a heading to opt in.
- Run with `dry_run: true` (the default) first: the job log lists every marker
  with its location before anything is written.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `project #N was not found for owner 'X'` | Wrong `PROJECT_OWNER`/`PROJECT_NUMBER`, or `PROJECT_TOKEN` lacks `project` scope. |
| `Could not resolve to a ProjectV2` in the log | `PROJECT_TOKEN` is unset or has no Projects access (the automatic `GITHUB_TOKEN` will never work here). |
| `environment variable PROJECT_TOKEN is required` | Secret not created in step 2. |
| Nothing created, everything "skipped" | The issues genuinely look non-actionable to the model; check the reason column, or use `draft-tasks` mode for finer output. |
| `Could not resolve to an Organization with the login of 'X'` | Older revision of the scripts, which asked for `organization` and `user` in one query; GitHub errors instead of returning null. Current scripts query the two shapes separately. |
| `HTTP 401` from DeepSeek | `DEEPSEEK_API_KEY` is wrong or has no credit. |
