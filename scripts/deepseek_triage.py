#!/usr/bin/env python3
"""Turn open GitHub issues into GitHub Projects V2 tasks with DeepSeek.

The script reads the open issues of one repository, asks DeepSeek which of them
describe concrete, actionable work, and then records the verdict on a Projects
V2 board. Two recording modes exist:

  board-items  add the issue itself as a project item and set its Priority
  draft-tasks  file one or more project draft issues that decompose the issue

Authentication is split on purpose:

  GITHUB_TOKEN   repository-scoped, read-only REST access to issues.
  PROJECT_TOKEN  a PAT with the `project` scope. Required because the default
                 GITHUB_TOKEN cannot read or write Projects V2 at all, no matter
                 what `permissions:` the workflow requests.

Usage:
    python3 scripts/deepseek_triage.py --check
    python3 scripts/deepseek_triage.py --dry-run
    python3 scripts/deepseek_triage.py --mode draft-tasks --limit 10

Exit status is 0 on success and 1 on a configuration or API failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
DEEPSEEK_API = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com").rstrip("/")
USER_AGENT = "deepseek-project-triage/1.0"

# Priorities the model may return when the board has no Priority field to read.
FALLBACK_PRIORITIES = ["High", "Medium", "Low"]

# Loose synonyms the model sometimes emits instead of the board's own labels.
PRIORITY_SYNONYMS = {
    "p0": "High",
    "p1": "High",
    "p2": "Medium",
    "p3": "Low",
    "critical": "High",
    "blocker": "High",
    "urgent": "High",
    "major": "High",
    "minor": "Low",
    "trivial": "Low",
    "low": "Low",
    "medium": "Medium",
    "normal": "Medium",
    "high": "High",
}


class TriageError(RuntimeError):
    """Fatal, user-facing failure."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def request_json(
    method: str,
    url: str,
    token: str,
    payload: Any | None = None,
    attempts: int = 4,
    timeout: int = 60,
) -> Any:
    """Call a JSON API, retrying transient failures with exponential backoff."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
    }
    delay = 2.0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            if exc.code in (429, 500, 502, 503, 504) and attempt < attempts:
                log(f"  HTTP {exc.code} from {url}; retrying in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise TriageError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < attempts:
                log(f"  network error ({exc.reason}); retrying in {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
                continue
            raise TriageError(f"network error for {url}: {exc.reason}") from exc

    raise TriageError(f"exhausted retries for {url}")


def github_graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = request_json(
        "POST", f"{GITHUB_API}/graphql", token, {"query": query, "variables": variables}
    )
    if payload.get("errors"):
        messages = "; ".join(
            str(error.get("message", "unknown error")) for error in payload["errors"]
        )
        raise TriageError(f"GraphQL error: {messages}")
    return payload.get("data") or {}


# --------------------------------------------------------------------------- #
# GitHub reads
# --------------------------------------------------------------------------- #

PROJECT_QUERY = """
query($login: String!, $number: Int!) {
  organization(login: $login) { projectV2(number: $number) { ...ProjectFields } }
  user(login: $login) { projectV2(number: $number) { ...ProjectFields } }
}
fragment ProjectFields on ProjectV2 {
  id
  title
  url
  fields(first: 50) {
    nodes {
      ... on ProjectV2FieldCommon { id name dataType }
      ... on ProjectV2SingleSelectField { options { id name } }
    }
  }
}
"""

ITEMS_QUERY = """
query($project: ID!, $cursor: String) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            __typename
            ... on Issue { number title }
            ... on DraftIssue { title }
          }
        }
      }
    }
  }
}
"""


@dataclass
class Project:
    id: str
    title: str
    url: str
    fields: dict[str, dict[str, Any]]

    def single_select(self, name: str) -> dict[str, Any] | None:
        field = self.fields.get(name.lower())
        if not field or field.get("dataType") != "SINGLE_SELECT":
            return None
        return field

    def option_id(self, field: dict[str, Any], label: str) -> str | None:
        wanted = label.strip().lower()
        for option in field.get("options") or []:
            if (option.get("name") or "").strip().lower() == wanted:
                return option["id"]
        return None


def resolve_project(token: str, owner: str, number: int) -> Project:
    data = github_graphql(token, PROJECT_QUERY, {"login": owner, "number": number})
    node = (data.get("organization") or {}).get("projectV2")
    if not node:
        node = (data.get("user") or {}).get("projectV2")
    if not node:
        raise TriageError(
            f"project #{number} was not found for owner {owner!r}. "
            "Check PROJECT_OWNER/PROJECT_NUMBER and that the token has Projects read access."
        )
    fields: dict[str, dict[str, Any]] = {}
    for field in (node.get("fields") or {}).get("nodes") or []:
        if field and field.get("name"):
            fields[field["name"].lower()] = field
    return Project(id=node["id"], title=node["title"], url=node.get("url", ""), fields=fields)


def existing_items(token: str, project_id: str) -> tuple[set[int], set[str]]:
    """Return (issue numbers already on the board, titles of existing drafts)."""
    issue_numbers: set[int] = set()
    draft_titles: set[str] = set()
    cursor: str | None = None
    while True:
        data = github_graphql(token, ITEMS_QUERY, {"project": project_id, "cursor": cursor})
        items = (data.get("node") or {}).get("items") or {}
        for item in items.get("nodes") or []:
            content = item.get("content") or {}
            kind = content.get("__typename")
            if kind == "Issue" and content.get("number") is not None:
                issue_numbers.add(int(content["number"]))
            elif kind == "DraftIssue" and content.get("title"):
                draft_titles.add(content["title"].strip())
        page = items.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return issue_numbers, draft_titles
        cursor = page.get("endCursor")


def fetch_open_issues(token: str, owner: str, repo: str, limit: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page = 1
    while len(issues) < limit:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues?state=open&per_page=100&page={page}"
        batch = request_json("GET", url, token) or []
        if not batch:
            break
        for raw in batch:
            # The issues endpoint also returns pull requests; the board tracks issues only.
            if "pull_request" in raw:
                continue
            issues.append(
                {
                    "number": int(raw["number"]),
                    "title": (raw.get("title") or "").strip(),
                    "body": raw.get("body") or "",
                    "labels": [label.get("name", "") for label in raw.get("labels") or []],
                    "url": raw.get("html_url") or "",
                    "node_id": raw.get("node_id") or "",
                }
            )
            if len(issues) >= limit:
                break
        if len(batch) < 100:
            break
        page += 1
    return issues


# --------------------------------------------------------------------------- #
# DeepSeek triage
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """You triage GitHub issues for a Rust Raft consensus library.
Decide how each issue should be tracked on the maintainers' GitHub Projects board.
Mark an issue actionable only when it describes concrete work: a bug with a plausible cause,
a missing capability, a refactor, or a test gap. Questions, discussion threads, duplicates,
and vague wishes are not actionable.
Reply with a single JSON object and nothing else."""

BOARD_RUBRIC = """Return JSON shaped exactly like:
{"actionable": true, "priority": "PRIORITY", "reason": "one sentence"}
where "priority" is one of: PRIORITIES."""

DRAFT_RUBRIC = """Return JSON shaped exactly like:
{"actionable": true, "priority": "PRIORITY", "reason": "one sentence",
 "tasks": [{"title": "short imperative title", "body": "what to do, acceptance criteria"}]}
where "priority" is one of: PRIORITIES.
Split the work into at most TASK_LIMIT tasks, each small enough for one pull request.
If the issue is not actionable, return {"actionable": false, "reason": "one sentence", "tasks": []}."""


def build_prompt(issue: dict[str, Any], rubric: str, priorities: list[str], task_limit: int) -> str:
    body = issue["body"].strip()
    if len(body) > 6000:
        body = body[:6000] + "\n\n[issue body truncated]"
    labels = ", ".join(issue["labels"]) or "none"
    filled = rubric.replace("PRIORITIES", ", ".join(priorities)).replace(
        "TASK_LIMIT", str(task_limit)
    )
    return (
        f"Issue #{issue['number']}: {issue['title']}\n"
        f"Labels: {labels}\n"
        f"URL: {issue['url']}\n\n"
        f"Body:\n{body or '(empty)'}\n\n"
        f"{filled}"
    )


def parse_json_reply(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise TriageError(f"DeepSeek returned invalid JSON: {content[:400]!r}") from exc
    if not isinstance(parsed, dict):
        raise TriageError(f"DeepSeek returned a non-object JSON value: {content[:400]!r}")
    return parsed


def triage_issue(
    issue: dict[str, Any],
    api_key: str,
    model: str,
    mode: str,
    priorities: list[str],
    task_limit: int,
) -> dict[str, Any]:
    rubric = DRAFT_RUBRIC if mode == "draft-tasks" else BOARD_RUBRIC
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_prompt(issue, rubric, priorities, task_limit),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    reply = request_json("POST", f"{DEEPSEEK_API}/chat/completions", api_key, payload)
    try:
        content = reply["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise TriageError(f"unexpected DeepSeek response: {str(reply)[:400]}") from exc
    return parse_json_reply(content or "")


def normalise_priority(value: Any, options: list[str]) -> str | None:
    """Map the model's label onto one of the board's own option names."""
    if not isinstance(value, str) or not value.strip():
        return None
    wanted = value.strip()
    for option in options:
        if option.lower() == wanted.lower():
            return option
    alias = PRIORITY_SYNONYMS.get(wanted.lower())
    if alias:
        for option in options:
            if option.lower() == alias.lower():
                return option
    return None


# --------------------------------------------------------------------------- #
# GitHub writes
# --------------------------------------------------------------------------- #

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""

ADD_DRAFT = """
mutation($project: ID!, $title: String!, $body: String) {
  addProjectV2DraftIssue(input: {projectId: $project, title: $title, body: $body}) {
    projectItem { id }
  }
}
"""

UPDATE_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field, value: $value}
  ) {
    projectV2Item { id }
  }
}
"""


def add_issue_item(token: str, project_id: str, issue: dict[str, Any]) -> str:
    data = github_graphql(token, ADD_ITEM, {"project": project_id, "content": issue["node_id"]})
    return data["addProjectV2ItemById"]["item"]["id"]


def add_draft_task(token: str, project_id: str, title: str, body: str) -> str:
    data = github_graphql(
        token, ADD_DRAFT, {"project": project_id, "title": title, "body": body}
    )
    return data["addProjectV2DraftIssue"]["projectItem"]["id"]


def set_single_select(
    token: str, project_id: str, item_id: str, field: dict[str, Any], option_id: str
) -> None:
    github_graphql(
        token,
        UPDATE_FIELD,
        {
            "project": project_id,
            "item": item_id,
            "field": field["id"],
            "value": {"singleSelectOptionId": option_id},
        },
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--project",
        required=True,
        help="Project in OWNER/NUMBER form, e.g. MaksimPRs/4",
    )
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="OWNER/REPO, defaults to GITHUB_REPOSITORY")
    parser.add_argument("--mode", choices=("board-items", "draft-tasks"), default="board-items")
    parser.add_argument("--limit", type=int, default=25, help="maximum issues to triage per run")
    parser.add_argument("--max-tasks", type=int, default=3, help="maximum draft tasks per issue")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    parser.add_argument("--priority-field", default="Priority")
    parser.add_argument("--status", default="", help="optional Status option to apply, e.g. Todo")
    parser.add_argument("--dry-run", action="store_true", help="report planned tasks without writing")
    parser.add_argument("--check", action="store_true", help="verify project access, then exit")
    return parser.parse_args(argv)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise TriageError(f"environment variable {name} is required (and must not be empty)")
    return value


def summarise(rows: list[list[str]]) -> None:
    if not rows:
        return
    header = "| Issue | Title | Action | Priority | Why |"
    divider = "| --- | --- | --- | --- | --- |"
    lines = [header, divider]
    lines += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows]
    table = "\n".join(lines)
    print("\n" + table)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("## DeepSeek issue triage\n\n" + table + "\n")


def run(args: argparse.Namespace) -> int:
    github_token = require_env("GITHUB_TOKEN")
    project_token = require_env("PROJECT_TOKEN")

    owner, _, number_text = args.project.partition("/")
    if not owner or not number_text.isdigit():
        raise TriageError(f"--project must look like OWNER/NUMBER, got {args.project!r}")
    project = resolve_project(project_token, owner, int(number_text))
    log(f"project: {project.title} ({project.url})")

    priority_field = project.single_select(args.priority_field)
    options = [option["name"] for option in (priority_field or {}).get("options") or []]
    priorities = options or FALLBACK_PRIORITIES
    status_field = project.single_select("Status") if args.status else None

    if args.check:
        log(f"priority field: {priority_field['name'] if priority_field else 'not found'}")
        log(f"priority options: {', '.join(priorities)}")
        log(f"status field: {status_field['name'] if status_field else 'not found'}")
        issues_on_board, drafts = existing_items(project_token, project.id)
        log(f"existing items: {len(issues_on_board)} issues, {len(drafts)} drafts")
        return 0

    if not priority_field:
        log(f"note: no single-select {args.priority_field!r} field on the board; priorities will only be reported")
    if args.status and not status_field:
        log(f"note: no single-select 'Status' field on the board; {args.status!r} will not be applied")

    repo_owner, _, repo_name = args.repo.partition("/")
    if not repo_owner or not repo_name:
        raise TriageError("--repo (or GITHUB_REPOSITORY) must look like OWNER/REPO")

    issues_on_board, draft_titles = existing_items(project_token, project.id)
    issues = fetch_open_issues(github_token, repo_owner, repo_name, args.limit)
    log(f"open issues considered: {len(issues)} (limit {args.limit})")

    api_key = require_env("DEEPSEEK_API_KEY")
    rows: list[list[str]] = []
    created = skipped = failed = 0

    for issue in issues:
        reference = f"#{issue['number']}"
        if args.mode == "board-items" and issue["number"] in issues_on_board:
            log(f"{reference} already on the board; skipping")
            continue

        try:
            verdict = triage_issue(
                issue, api_key, args.model, args.mode, priorities, args.max_tasks
            )
        except TriageError as exc:
            failed += 1
            log(f"{reference} triage failed: {exc}")
            rows.append([reference, issue["title"], "error", "-", str(exc)[:120]])
            continue

        if not verdict.get("actionable"):
            skipped += 1
            reason = str(verdict.get("reason", "")).strip() or "not actionable"
            log(f"{reference} not actionable: {reason}")
            rows.append([reference, issue["title"], "skipped", "-", reason])
            continue

        priority = normalise_priority(verdict.get("priority"), priorities)
        reason = str(verdict.get("reason", "")).strip()

        if args.mode == "board-items":
            if args.dry_run:
                log(f"{reference} would be added to the board (priority {priority or 'unset'})")
                created += 1
                rows.append([reference, issue["title"], "would add", priority or "-", reason])
                continue
            item_id = add_issue_item(project_token, project.id, issue)
            if priority and priority_field:
                set_single_select(
                    project_token,
                    project.id,
                    item_id,
                    priority_field,
                    project.option_id(priority_field, priority),
                )
            if args.status and status_field:
                set_single_select(
                    project_token,
                    project.id,
                    item_id,
                    status_field,
                    project.option_id(status_field, args.status),
                )
            created += 1
            issues_on_board.add(issue["number"])
            log(f"{reference} added to the board")
            rows.append([reference, issue["title"], "added", priority or "-", reason])
            continue

        tasks = [task for task in verdict.get("tasks") or [] if str(task.get("title", "")).strip()]
        tasks = tasks[: args.max_tasks]
        if not tasks:
            failed += 1
            log(f"{reference} marked actionable but produced no tasks")
            rows.append([reference, issue["title"], "no tasks", "-", reason])
            continue

        for task in tasks:
            title = f"[{reference}] {str(task['title']).strip()}"
            if title in draft_titles:
                log(f"{reference} draft already exists: {title}")
                continue
            body = str(task.get("body", "")).strip()
            body += f"\n\nFrom {issue['url']}"
            if args.dry_run:
                log(f"{reference} would create draft: {title}")
                created += 1
                rows.append([reference, issue["title"], "would draft", priority or "-", title])
                continue
            item_id = add_draft_task(project_token, project.id, title, body.strip())
            if priority and priority_field:
                set_single_select(
                    project_token,
                    project.id,
                    item_id,
                    priority_field,
                    project.option_id(priority_field, priority),
                )
            draft_titles.add(title)
            created += 1
            log(f"{reference} created draft: {title}")
            rows.append([reference, issue["title"], "drafted", priority or "-", title])

    summarise(rows)
    log(f"done: {created} created, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(args)
    except TriageError as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
