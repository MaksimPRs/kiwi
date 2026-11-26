#!/usr/bin/env python3
"""File GitHub Projects V2 tasks from TODO/FIXME comments and [WIP] headings.

Unlike deepseek_triage.py, this needs no model: the marker text already is the
task, so extraction is deterministic and free. DeepSeek is not called at all.

Sources:
  comment  `// TODO ...`, `// FIXME [RAFTY-8] ...` in Rust sources
  wip      markdown headings marked `[WIP]`

Every task carries an HTML comment fingerprint in its body, so re-running never
duplicates a task, even after line numbers shift.

Usage:
    python3 scripts/scan_markers.py --list
    python3 scripts/scan_markers.py --list --kinds fixme,wip

Comments that already carry a tracking key (`// FIXME [RAFTY-8] ...`) are skipped:
the key means the issue exists, so a generated task would duplicate it.
    python3 scripts/scan_markers.py --project MaksimPRs/4 --dry-run
    python3 scripts/scan_markers.py --project MaksimPRs/4 --as-issues --dry-run

Exit status is 0 on success and 1 on a configuration or API failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
USER_AGENT = "deepseek-marker-triage/1.0"
FP_PREFIX = "marker-fp:"

TEXT_SUFFIXES = {".rs", ".toml", ".md", ".yml", ".yaml", ".json", ".sh", ".py", ".txt"}
SKIP_DIRS = {".git", "target", ".cargo-home", "node_modules", ".idea", "cv"}

COMMENT_RE = re.compile(
    r"^(?P<indent>[ \t]*)//[ \t]*(?P<kind>TODO|FIXME|XXX|HACK)\b[:\s\-]*(?P<text>.*)$",
    re.IGNORECASE,
)
# Trailing form: `Some(code), // TODO ...`, but never `https://TODO ...`.
TRAILING_COMMENT_RE = re.compile(
    r"(?<!:)//[ \t]*(?P<kind>TODO|FIXME|XXX|HACK)\b[:\s\-]*(?P<text>.*)$", re.IGNORECASE
)
WIP_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>.*?)\s*\[WIP\]\s*$")
WIP_ANY_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>.*\bWIP\b.*)$")
KEY_RE = re.compile(r"\[(?P<key>[A-Z][A-Z0-9]+-\d+)\]")


class TriageError(RuntimeError):
    """Fatal, user-facing failure."""


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@dataclass
class Marker:
    kind: str
    path: str
    line: int
    text: str
    title: str
    key: str | None = None
    context: list[str] = field(default_factory=list)
    fingerprint: str = ""


# --------------------------------------------------------------------------- #
# Extraction (no network)
# --------------------------------------------------------------------------- #


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise TriageError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def tracked_files(root: str) -> list[str]:
    out = git("-C", root, "ls-files", "-z")
    return [path for path in out.split("\0") if path]


def read_lines(root: str, rel_path: str) -> list[str] | None:
    if os.path.splitext(rel_path)[1].lower() not in TEXT_SUFFIXES:
        return None
    if any(part in SKIP_DIRS for part in rel_path.split(os.sep)):
        return None
    absolute = os.path.join(root, rel_path)
    try:
        if os.path.getsize(absolute) > 1_000_000:
            return None
        with open(absolute, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return None


def scan_rust(root: str, rel_path: str, kinds: set[str]) -> list[Marker]:
    lines = read_lines(root, rel_path)
    if lines is None:
        return []
    markers: list[Marker] = []
    for index, line in enumerate(lines):
        comment = COMMENT_RE.match(line) or TRAILING_COMMENT_RE.search(line)
        if comment and comment.group("kind").lower() in kinds:
            text = comment.group("text").strip()
            if not text:
                continue
            key_match = KEY_RE.search(text)
            markers.append(
                Marker(
                    kind=comment.group("kind").lower(),
                    path=rel_path,
                    line=index + 1,
                    text=text,
                    title=text,
                    key=key_match.group("key") if key_match else None,
                    context=[line.strip()],
                )
            )
            continue
    return markers


def scan_markdown(root: str, rel_path: str, kinds: set[str]) -> list[Marker]:
    if "wip" not in kinds:
        return []
    lines = read_lines(root, rel_path)
    if lines is None:
        return []
    markers: list[Marker] = []
    for index, line in enumerate(lines):
        match = WIP_HEADING_RE.match(line) or WIP_ANY_HEADING_RE.match(line)
        if not match:
            continue
        title = re.sub(r"\[WIP\]|\bWIP\b", "", match.group("title"))
        title = re.sub(r"\(\s*\)|\[\s*\]", "", title)  # "Current limitations (WIP)" -> "Current limitations"
        title = " ".join(title.split()).strip(" :-")
        if not title:
            continue
        key_match = KEY_RE.search(title)
        markers.append(
            Marker(
                kind="wip",
                path=rel_path,
                line=index + 1,
                text=title,
                title=f"Finish: {title}",
                key=key_match.group("key") if key_match else None,
                context=[line.strip()],
            )
        )
    return markers


def fingerprint(marker: Marker) -> str:
    """Stable across line moves: kind + path + normalised text."""
    normalised = " ".join(marker.text.split()).lower()
    digest = hashlib.sha256(f"{marker.kind}\0{marker.path}\0{normalised}".encode()).hexdigest()
    return digest[:16]


def collect_markers(
    root: str, kinds: set[str], paths: list[str] | None
) -> tuple[list[Marker], int]:
    """Return the markers worth filing, plus how many were skipped as already tracked."""
    files = paths or tracked_files(root)
    found: list[Marker] = []
    for rel_path in files:
        if rel_path.endswith(".rs"):
            found.extend(scan_rust(root, rel_path, kinds))
        elif rel_path.endswith(".md"):
            found.extend(scan_markdown(root, rel_path, kinds))

    markers: list[Marker] = []
    skipped = 0
    for marker in found:
        if marker.key:
            # `[RAFTY-8]` means a tracking issue already exists; a new task would duplicate it.
            skipped += 1
            log(f"{marker.kind}:{marker.path}:{marker.line} references {marker.key}; skipping")
            continue
        marker.fingerprint = fingerprint(marker)
        markers.append(marker)
    markers.sort(key=lambda m: (m.path, m.line))
    return markers, skipped


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def request_json(
    method: str, url: str, token: str, payload: Any | None = None, attempts: int = 4
) -> Any:
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
            with urllib.request.urlopen(request, timeout=60) as response:
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


def graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = request_json("POST", f"{GITHUB_API}/graphql", token, {"query": query, "variables": variables})
    if payload.get("errors"):
        messages = "; ".join(str(e.get("message", "unknown")) for e in payload["errors"])
        raise TriageError(f"GraphQL error: {messages}")
    return payload.get("data") or {}


PROJECT_FIELDS = """
fragment P on ProjectV2 {
  id title url
  fields(first: 50) {
    nodes {
      ... on ProjectV2FieldCommon { id name dataType }
      ... on ProjectV2SingleSelectField { options { id name } }
    }
  }
}
"""

# A board is owned by either a user or an organization, and the two need
# different root fields. Asking for the wrong one is a hard GraphQL error rather
# than a null, so each shape is queried on its own.
USER_PROJECT_QUERY = (
    "query($login: String!, $number: Int!) {"
    " user(login: $login) { projectV2(number: $number) { ...P } }"
    "}" + PROJECT_FIELDS
)

ORG_PROJECT_QUERY = (
    "query($login: String!, $number: Int!) {"
    " organization(login: $login) { projectV2(number: $number) { ...P } }"
    "}" + PROJECT_FIELDS
)

DRAFT_BODIES_QUERY = """
query($project: ID!, $cursor: String) {
  node(id: $project) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { content { __typename ... on DraftIssue { title body } } }
      }
    }
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

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}
"""

UPDATE_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field, value: $value}
  ) { projectV2Item { id } }
}
"""


def resolve_project(token: str, owner: str, number: int) -> dict[str, Any]:
    """Find the board whether the owner is a user or an organization."""
    for root, query in (("user", USER_PROJECT_QUERY), ("organization", ORG_PROJECT_QUERY)):
        try:
            data = graphql(token, query, {"login": owner, "number": number})
        except TriageError as exc:
            if "Could not resolve to" in str(exc):
                continue  # the owner is the other kind; try that shape
            raise
        node = (data.get(root) or {}).get("projectV2")
        if node:
            return node
    raise TriageError(
        f"project #{number} not found for owner {owner!r}, as a user or an organization; "
        "check PROJECT_OWNER/PROJECT_NUMBER and that the token has Projects read access"
    )


def board_fingerprints(token: str, project_id: str) -> set[str]:
    found: set[str] = set()
    cursor: str | None = None
    while True:
        data = graphql(token, DRAFT_BODIES_QUERY, {"project": project_id, "cursor": cursor})
        items = (data.get("node") or {}).get("items") or {}
        for item in items.get("nodes") or []:
            body = ((item.get("content") or {}).get("body") or "")
            found.update(re.findall(rf"{FP_PREFIX}\s*([0-9a-f]{{16}})", body))
        page = items.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return found
        cursor = page.get("endCursor")


def issue_fingerprints(token: str, owner: str, repo: str) -> set[str]:
    found: set[str] = set()
    page = 1
    while page <= 10:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues?state=all&per_page=100&page={page}"
        batch = request_json("GET", url, token) or []
        if not batch:
            break
        for raw in batch:
            if "pull_request" in raw:
                continue
            found.update(re.findall(rf"{FP_PREFIX}\s*([0-9a-f]{{16}})", raw.get("body") or ""))
        if len(batch) < 100:
            break
        page += 1
    return found


def build_body(marker: Marker, repo_slug: str, sha: str) -> str:
    link = f"https://github.com/{repo_slug}/blob/{sha}/{marker.path}#L{marker.line}"
    lines = [
        f"Source: [`{marker.path}:{marker.line}`]({link})",
        "",
        "```rust" if marker.path.endswith(".rs") else "```markdown",
        *marker.context,
        "```",
    ]
    lines += ["", f"<!-- {FP_PREFIX} {marker.fingerprint} -->", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project", default="", help="OWNER/NUMBER of the Projects V2 board")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--kinds",
        default="comment,wip",
        help="comma list of comment, wip, or a specific marker: todo, fixme, xxx, hack",
    )
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--paths", nargs="*", default=None, help="optional path filter")
    parser.add_argument("--list", action="store_true", help="print markers and exit; no network")
    parser.add_argument("--as-issues", action="store_true", help="open repository issues instead of project drafts")
    parser.add_argument("--priorities", default="", help="e.g. fixme=High,todo=Medium")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check", action="store_true", help="verify project access, then exit")
    return parser.parse_args(argv)


def priority_map(spec: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in spec.split(","):
        if "=" in entry:
            kind, _, label = entry.partition("=")
            mapping[kind.strip().lower()] = label.strip()
    return mapping


def summarise(rows: list[list[str]]) -> None:
    if not rows:
        return
    table = "\n".join(
        ["| Marker | Location | Task |", "| --- | --- | --- |"]
        + ["| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in rows]
    )
    print("\n" + table)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("## Marker scan\n\n" + table + "\n")


def run(args: argparse.Namespace) -> int:
    root = git("rev-parse", "--show-toplevel").strip()
    kinds: set[str] = set()
    for entry in (kind.strip().lower() for kind in args.kinds.split(",")):
        if not entry:
            continue
        if entry == "comment":
            kinds.update({"todo", "fixme", "xxx", "hack"})
        else:
            kinds.add(entry)
    unknown = kinds - {"todo", "fixme", "xxx", "hack", "wip"}
    if unknown:
        raise TriageError(f"unknown marker kind(s): {', '.join(sorted(unknown))}")
    markers, skipped_tracked = collect_markers(root, kinds, args.paths)
    summary = f"{len(markers)} marker(s), {skipped_tracked} skipped as already tracked"

    if args.list:
        for marker in markers:
            print(f"{marker.kind:8} {marker.path}:{marker.line}  {marker.title}")
        log(summary)
        return 0

    log(summary)
    if len(markers) > args.limit:
        log(f"note: {len(markers)} markers found, capping at {args.limit} (see --limit)")
        markers = markers[: args.limit]

    repo_slug = args.repo
    repo_owner, _, repo_name = repo_slug.partition("/")
    if not repo_owner or not repo_name:
        raise TriageError("--repo (or GITHUB_REPOSITORY) must look like OWNER/REPO")
    sha = git("rev-parse", "HEAD").strip()

    project_token = os.environ.get("PROJECT_TOKEN", "").strip()
    if not project_token:
        raise TriageError("environment variable PROJECT_TOKEN is required (PAT with the `project` scope)")
    project_owner, _, number_text = args.project.partition("/")
    if not project_owner or not number_text.isdigit():
        raise TriageError(f"--project must look like OWNER/NUMBER, got {args.project!r}")
    project = resolve_project(project_token, project_owner, int(number_text))
    log(f"project: {project['title']} ({project.get('url', '')})")
    log(f"repository: {repo_slug} @ {sha[:8]}")
    log(f"markers: {len(markers)}")

    if args.check:
        log(f"drafts already on board: {len(board_fingerprints(project_token, project['id']))}")
        return 0

    priorities = priority_map(args.priorities)
    priority_field = None
    for node in (project.get("fields") or {}).get("nodes") or []:
        if node and (node.get("name") or "").lower() == "priority" and node.get("dataType") == "SINGLE_SELECT":
            priority_field = node
            break

    known = board_fingerprints(project_token, project["id"]) if not args.as_issues else issue_fingerprints(
        project_token, repo_owner, repo_name
    )
    log(f"existing tasks matched by fingerprint: {len(known)}")

    rows: list[list[str]] = []
    created = skipped = 0
    for marker in markers:
        label = f"{marker.kind}:{marker.path}:{marker.line}"
        if marker.fingerprint in known:
            skipped += 1
            log(f"{label} already tracked; skipping")
            continue
        body = build_body(marker, repo_slug, sha)
        if args.dry_run:
            created += 1
            log(f"{label} would create: {marker.title}")
            rows.append([marker.kind, f"{marker.path}:{marker.line}", f"would create: {marker.title}"])
            continue

        if args.as_issues:
            issue = request_json(
                "POST",
                f"{GITHUB_API}/repos/{repo_owner}/{repo_name}/issues",
                project_token,
                {"title": marker.title, "body": body},
            )
            item_id = None
            if marker.kind in priorities and priority_field:
                added = graphql(project_token, ADD_ITEM, {"project": project["id"], "content": issue["node_id"]})
                item_id = added["addProjectV2ItemById"]["item"]["id"]
            log(f"{label} opened issue #{issue['number']}: {marker.title}")
            rows.append([marker.kind, f"{marker.path}:{marker.line}", f"issue #{issue['number']}"])
        else:
            added = graphql(
                project_token,
                ADD_DRAFT,
                {"project": project["id"], "title": marker.title, "body": body},
            )
            item_id = added["addProjectV2DraftIssue"]["projectItem"]["id"]
            log(f"{label} created draft: {marker.title}")
            rows.append([marker.kind, f"{marker.path}:{marker.line}", f"draft: {marker.title}"])

        if item_id and priority_field and marker.kind in priorities:
            option = None
            for candidate in priority_field.get("options") or []:
                if (candidate.get("name") or "").lower() == priorities[marker.kind].lower():
                    option = candidate["id"]
                    break
            if option:
                graphql(
                    project_token,
                    UPDATE_FIELD,
                    {
                        "project": project["id"],
                        "item": item_id,
                        "field": priority_field["id"],
                        "value": {"singleSelectOptionId": option},
                    },
                )
        known.add(marker.fingerprint)
        created += 1

    summarise(rows)
    log(f"done: {created} created, {skipped} skipped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return run(args)
    except TriageError as exc:
        log(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
