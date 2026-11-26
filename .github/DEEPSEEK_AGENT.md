# DeepSeek issue triage agent

`workflow_dispatch` workflow that reads the repository's open issues, asks
DeepSeek which ones describe concrete work, and records the result on a GitHub
Projects (V2) board. It is manual by design: nothing runs on push or on new
issues, and it defaults to a dry run.

Files:

- `.github/workflows/deepseek-triage.yml` — the workflow
- `scripts/deepseek_triage.py` — the agent (Python 3 standard library only, no dependencies)

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

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `project #N was not found for owner 'X'` | Wrong `PROJECT_OWNER`/`PROJECT_NUMBER`, or `PROJECT_TOKEN` lacks `project` scope. |
| `Could not resolve to a ProjectV2` in the log | `PROJECT_TOKEN` is unset or has no Projects access (the automatic `GITHUB_TOKEN` will never work here). |
| `environment variable PROJECT_TOKEN is required` | Secret not created in step 2. |
| Nothing created, everything "skipped" | The issues genuinely look non-actionable to the model; check the reason column, or use `draft-tasks` mode for finer output. |
| `HTTP 401` from DeepSeek | `DEEPSEEK_API_KEY` is wrong or has no credit. |
