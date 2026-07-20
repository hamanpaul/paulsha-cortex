"""Fail-closed GitHub delivery gates for a single immutable PR HEAD."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable, Mapping
from urllib.parse import quote


GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
COPILOT_REVIEWER_LOGIN = "copilot-pull-request-reviewer[bot]"
_SHIP_CAPABILITY = object()
COPILOT_ERROR_MARKERS = (
    "encountered an error",
    "failed to review",
    "unable to review",
    "wasn't able to review",
    "was not able to review",
    "couldn't review",
    "could not review",
)


@dataclass(frozen=True)
class GitHubCheck:
    name: str
    status: str
    conclusion: str | None

    @property
    def terminal_green(self) -> bool:
        return self.status == "completed" and self.conclusion in GREEN_CONCLUSIONS


@dataclass(frozen=True)
class CopilotReview:
    review_id: int
    commit_id: str
    state: str
    body: str
    author: str
    submitted_at_epoch: float

    @property
    def is_error(self) -> bool:
        body = self.body.casefold()
        return any(marker in body for marker in COPILOT_ERROR_MARKERS)


@dataclass(frozen=True)
class ReviewThread:
    thread_id: str
    resolved: bool
    outdated: bool

    @property
    def blocks_merge(self) -> bool:
        return not self.resolved and not self.outdated


@dataclass(frozen=True)
class DeliveryFacts:
    head: str
    mergeable: bool
    mergeable_state: str
    checks: tuple[GitHubCheck, ...]
    copilot_reviews: tuple[CopilotReview, ...]
    review_threads: tuple[ReviewThread, ...]
    closing_issues: tuple[int, ...]
    active_openspec_absent: bool
    archive_present: bool


@dataclass(frozen=True)
class DeliveryPolicy:
    expected_head: str
    required_closing_issues: tuple[int, ...]
    copilot_review_id: int | None = None
    copilot_requested_at_epoch: float | None = None
    review_kind: str = "copilot"


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reasons: tuple[str, ...]


def _unique_reasons(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def evaluate_delivery_gate(*, facts: DeliveryFacts, policy: DeliveryPolicy) -> GateResult:
    """Evaluate only remotely re-read facts for the expected immutable HEAD."""

    reasons: list[str] = []
    if facts.head != policy.expected_head:
        reasons.append("head-race")
    if not facts.mergeable or facts.mergeable_state not in {"clean", "has_hooks"}:
        reasons.append("not-mergeable")
    if not facts.checks or any(not check.terminal_green for check in facts.checks):
        reasons.append("checks-not-terminal-green")

    if policy.review_kind == "copilot":
        if policy.copilot_review_id is None or policy.copilot_requested_at_epoch is None:
            reasons.append("copilot-review-policy-invalid")
            current_reviews = ()
        else:
            current_reviews = tuple(
                review
                for review in facts.copilot_reviews
                if review.commit_id == policy.expected_head
                and review.review_id == policy.copilot_review_id
                and review.author == COPILOT_REVIEWER_LOGIN
                and review.submitted_at_epoch >= policy.copilot_requested_at_epoch
            )
        if not current_reviews:
            reasons.append("copilot-current-head-review-missing")
        elif any(review.is_error for review in current_reviews):
            reasons.append("copilot-error-review")
        elif any(review.state.upper() not in {"COMMENTED", "APPROVED"} for review in current_reviews):
            reasons.append("copilot-review-state-invalid")
    elif policy.review_kind != "maintainer-review":
        reasons.append("delivery-review-policy-invalid")

    if any(thread.blocks_merge for thread in facts.review_threads):
        reasons.append("review-thread-open")
    missing_issues = set(policy.required_closing_issues) - set(facts.closing_issues)
    if missing_issues:
        reasons.append("closing-issue-missing")
    if not facts.active_openspec_absent:
        reasons.append("active-openspec-present")
    if not facts.archive_present:
        reasons.append("openspec-archive-missing")
    normalized = _unique_reasons(reasons)
    return GateResult(allowed=not normalized, reasons=normalized)

def build_copilot_request_argv(*, repo: str, pr_number: int) -> list[str]:
    if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise ValueError("repo must be owner/name")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    return [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{repo}/pulls/{pr_number}/requested_reviewers",
        "-f",
        "reviewers[]=copilot-pull-request-reviewer[bot]",
    ]


def build_merge_argv(*, repo: str, pr_number: int, expected_head: str) -> list[str]:
    GitHubDeliveryClient._repo_parts(repo)
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    if len(expected_head) != 40 or any(
        character not in "0123456789abcdefABCDEF" for character in expected_head
    ):
        raise ValueError("expected_head must be a 40-character hexadecimal SHA")
    return [
        "gh",
        "pr",
        "merge",
        str(pr_number),
        "--repo",
        repo,
        "--merge",
        "--match-head-commit",
        expected_head,
    ]


@dataclass(frozen=True)
class RemoteClosureFacts:
    merge_commit: str
    pr_head: str
    merge_parents: tuple[str, ...]
    default_head: str
    merge_is_ancestor: bool
    merge_is_merge_commit: bool
    issue_states: Mapping[int, str]
    active_openspec_absent: bool
    archive_present: bool
    todo_complete: bool
    todo_revisions: Mapping[str, str]
    completion_record_valid: bool


@dataclass(frozen=True)
class MergeStatus:
    merged: bool
    pr_head: str
    merge_commit: str | None


def evaluate_remote_closure(
    *,
    facts: RemoteClosureFacts,
    required_issues: tuple[int, ...],
    expected_head: str,
) -> GateResult:
    reasons: list[str] = []
    if re.fullmatch(r"[0-9a-fA-F]{40}", facts.default_head) is None:
        reasons.append("remote-default-unverified")
    if len(facts.merge_commit) != 40 or not facts.merge_is_ancestor:
        reasons.append("merge-ancestry-unverified")
    if facts.pr_head != expected_head or expected_head not in facts.merge_parents:
        reasons.append("merged-pr-head-unverified")
    if not facts.merge_is_merge_commit:
        reasons.append("merge-commit-required")
    if any(facts.issue_states.get(issue) != "closed" for issue in required_issues):
        reasons.append("issue-not-closed")
    if not facts.active_openspec_absent:
        reasons.append("active-openspec-present")
    if not facts.archive_present:
        reasons.append("openspec-archive-missing")
    if not facts.todo_complete:
        reasons.append("todo-incomplete")
    if not facts.completion_record_valid:
        reasons.append("completion-record-invalid")
    normalized = _unique_reasons(reasons)
    return GateResult(allowed=not normalized, reasons=normalized)


Runner = Callable[..., object]
_WORK_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$issueCursor:String,$threadCursor:String) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      closingIssuesReferences(first:100,after:$issueCursor) {
        nodes { number repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
      reviewThreads(first:100,after:$threadCursor) {
        nodes { id isResolved isOutdated }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".strip()


class GitHubDeliveryClient:
    """Authenticated ``gh api`` seam; any malformed remote fact fails closed."""

    def __init__(
        self,
        *,
        runner: Runner = subprocess.run,
        metadata_retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runner = runner
        if any(
            not isinstance(delay, (int, float))
            or isinstance(delay, bool)
            or not math.isfinite(float(delay))
            or delay < 0
            for delay in metadata_retry_delays
        ):
            raise ValueError(
                "GitHub metadata retry delays must be finite non-negative numbers"
            )
        self._metadata_retry_delays = tuple(
            float(delay) for delay in metadata_retry_delays
        )
        self._sleeper = sleeper

    def _run(self, argv: list[str], *, expect_json: bool) -> object:
        raw = self._runner(
            argv,
            shell=False,
            capture_output=True,
            text=True,
        )
        returncode = getattr(raw, "returncode", None)
        if returncode != 0:
            stderr = getattr(raw, "stderr", "")
            raise RuntimeError(f"gh command failed: {stderr}".rstrip())
        if not expect_json:
            return getattr(raw, "stdout", "")
        stdout = getattr(raw, "stdout", "")
        if not isinstance(stdout, str):
            raise RuntimeError("gh command returned non-text output")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh command returned malformed JSON") from exc

    def _api(self, endpoint: str) -> object:
        return self._run(["gh", "api", endpoint], expect_json=True)

    def _metadata_json(self, argv: list[str]) -> object:
        """Retry only the idempotent PR-metadata transaction surface."""

        for attempt in range(len(self._metadata_retry_delays) + 1):
            try:
                return self._run(argv, expect_json=True)
            except RuntimeError as error:
                if (
                    attempt >= len(self._metadata_retry_delays)
                    or re.search(r"\bHTTP (?:502|503|504)\b", str(error)) is None
                ):
                    raise
                self._sleeper(self._metadata_retry_delays[attempt])
        raise AssertionError("unreachable metadata retry state")

    def _api_pages(self, endpoint: str) -> list[object]:
        stdout = self._run(
            ["gh", "api", "--paginate", "--jq", ".", endpoint],
            expect_json=False,
        )
        if not isinstance(stdout, str):
            raise RuntimeError("GitHub paginated payload malformed")
        try:
            pages = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise RuntimeError("GitHub paginated payload malformed") from exc
        if not pages:
            raise RuntimeError("GitHub paginated payload malformed")
        return pages

    @staticmethod
    def _repo_parts(repo: str) -> tuple[str, str]:
        parts = repo.split("/")
        if (
            len(parts) != 2
            or not all(parts)
            or any(re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None for part in parts)
        ):
            raise ValueError("repo must be owner/name")
        return parts[0], parts[1]

    def _work_graph(self, *, repo: str, pr_number: int) -> dict[str, object]:
        owner, name = self._repo_parts(repo)
        issue_nodes: list[object] = []
        thread_nodes: list[object] = []
        issue_cursor: str | None = None
        thread_cursor: str | None = None
        issues_done = False
        threads_done = False
        while True:
            argv = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={_WORK_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={pr_number}",
            ]
            if issue_cursor is not None:
                argv.extend(["-F", f"issueCursor={issue_cursor}"])
            if thread_cursor is not None:
                argv.extend(["-F", f"threadCursor={thread_cursor}"])
            payload = self._run(argv, expect_json=True)
            if not isinstance(payload, dict) or payload.get("errors"):
                raise RuntimeError("GitHub GraphQL work facts unavailable")
            try:
                work = payload["data"]["repository"]["pullRequest"]  # type: ignore[index]
                issue_connection = work["closingIssuesReferences"]
                thread_connection = work["reviewThreads"]
            except (KeyError, TypeError) as exc:
                raise RuntimeError("GitHub GraphQL work facts malformed") from exc
            if not isinstance(issue_connection, dict) or not isinstance(thread_connection, dict):
                raise RuntimeError("GitHub GraphQL work facts malformed")
            issue_page = issue_connection.get("nodes")
            thread_page = thread_connection.get("nodes")
            issue_info = issue_connection.get("pageInfo")
            thread_info = thread_connection.get("pageInfo")
            if not isinstance(issue_page, list) or not isinstance(thread_page, list):
                raise RuntimeError("GitHub GraphQL nodes malformed")
            if not isinstance(issue_info, dict) or not isinstance(thread_info, dict):
                raise RuntimeError("GitHub GraphQL pageInfo malformed")
            issue_next = issue_info.get("hasNextPage")
            thread_next = thread_info.get("hasNextPage")
            if not isinstance(issue_next, bool) or not isinstance(thread_next, bool):
                raise RuntimeError("GitHub GraphQL pageInfo malformed")
            if not issues_done:
                issue_nodes.extend(issue_page)
            if not threads_done:
                thread_nodes.extend(thread_page)
            issues_done = issues_done or not issue_next
            threads_done = threads_done or not thread_next
            if issues_done and threads_done:
                return {
                    "closingIssuesReferences": {"nodes": issue_nodes},
                    "reviewThreads": {"nodes": thread_nodes},
                }
            if not issues_done:
                next_issue_cursor = issue_info.get("endCursor")
                if (
                    not isinstance(next_issue_cursor, str)
                    or not next_issue_cursor
                    or next_issue_cursor == issue_cursor
                ):
                    raise RuntimeError("GitHub GraphQL issue cursor malformed")
                issue_cursor = next_issue_cursor
            if not threads_done:
                next_thread_cursor = thread_info.get("endCursor")
                if (
                    not isinstance(next_thread_cursor, str)
                    or not next_thread_cursor
                    or next_thread_cursor == thread_cursor
                ):
                    raise RuntimeError("GitHub GraphQL thread cursor malformed")
                thread_cursor = next_thread_cursor

    def _tree_paths(self, *, repo: str, ref: str) -> tuple[str, ...]:
        payload = self._api(f"repos/{repo}/git/trees/{ref}?recursive=1")
        if not isinstance(payload, dict) or payload.get("truncated") is not False:
            raise RuntimeError("GitHub tree unavailable or truncated")
        rows = payload.get("tree")
        if not isinstance(rows, list):
            raise RuntimeError("GitHub tree payload malformed")
        paths: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise RuntimeError("GitHub tree entry malformed")
            paths.append(row["path"])
        return tuple(paths)

    def _commit_tree_paths(self, *, repo: str, commit: str) -> tuple[str, ...]:
        payload = self._api(f"repos/{repo}/git/commits/{commit}")
        try:
            tree_sha = payload["tree"]["sha"]  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("GitHub commit tree malformed") from exc
        if not isinstance(tree_sha, str):
            raise RuntimeError("GitHub commit tree malformed")
        return self._tree_paths(repo=repo, ref=tree_sha)

    @staticmethod
    def _openspec_facts(paths: tuple[str, ...], change: str) -> tuple[bool, bool]:
        if not change or "/" in change or change in {".", ".."}:
            raise ValueError("change must be a safe OpenSpec slug")
        active_prefix = f"openspec/changes/{change}/"
        archive_pattern = re.compile(
            rf"^openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(change)}/"
        )
        active_absent = not any(path.startswith(active_prefix) for path in paths)
        archive_present = any(archive_pattern.match(path) for path in paths)
        return active_absent, archive_present

    def fetch_delivery_facts(
        self,
        *,
        repo: str,
        pr_number: int,
        change: str,
    ) -> DeliveryFacts:
        self._repo_parts(repo)
        pull = self._api(f"repos/{repo}/pulls/{pr_number}")
        if not isinstance(pull, dict):
            raise RuntimeError("GitHub pull request payload malformed")
        try:
            head = pull["head"]["sha"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("GitHub pull request refs malformed") from exc
        if not isinstance(head, str):
            raise RuntimeError("GitHub pull request refs malformed")

        check_pages = self._api_pages(
            f"repos/{repo}/commits/{head}/check-runs?per_page=100"
        )
        status_pages = self._api_pages(
            f"repos/{repo}/commits/{head}/statuses?per_page=100"
        )
        review_pages = self._api_pages(
            f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100"
        )

        checks: list[GitHubCheck] = []
        check_rows: list[object] = []
        expected_total: int | None = None
        for page in check_pages:
            if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
                raise RuntimeError("GitHub check runs malformed")
            total = page.get("total_count")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise RuntimeError("GitHub check runs total_count malformed")
            expected_total = total if expected_total is None else expected_total
            if total != expected_total:
                raise RuntimeError("GitHub check runs total_count changed during read")
            check_rows.extend(page["check_runs"])
        if expected_total != len(check_rows):
            raise RuntimeError("GitHub check runs pagination incomplete")
        for row in check_rows:
            if not isinstance(row, dict):
                raise RuntimeError("GitHub check run malformed")
            checks.append(
                GitHubCheck(
                    name=str(row.get("name", "")),
                    status=str(row.get("status", "")),
                    conclusion=row.get("conclusion")
                    if isinstance(row.get("conclusion"), str)
                    else None,
                )
            )
        status_rows: list[object] = []
        for page in status_pages:
            if not isinstance(page, list):
                raise RuntimeError("GitHub commit statuses malformed")
            status_rows.extend(page)
        seen_status_contexts: set[str] = set()
        for row in status_rows:
            if not isinstance(row, dict):
                raise RuntimeError("GitHub commit status malformed")
            state = row.get("state")
            context = row.get("context")
            if not isinstance(state, str) or not isinstance(context, str):
                raise RuntimeError("GitHub commit status malformed")
            if context in seen_status_contexts:
                continue
            seen_status_contexts.add(context)
            checks.append(
                GitHubCheck(
                    name=context,
                    status="in_progress" if state == "pending" else "completed",
                    conclusion="success" if state == "success" else state,
                )
            )
        reviews: list[CopilotReview] = []
        review_rows: list[object] = []
        for page in review_pages:
            if not isinstance(page, list):
                raise RuntimeError("GitHub reviews malformed")
            review_rows.extend(page)
        for row in review_rows:
            if not isinstance(row, dict):
                raise RuntimeError("GitHub review malformed")
            login = row.get("user", {}).get("login") if isinstance(row.get("user"), dict) else None
            if login != COPILOT_REVIEWER_LOGIN:
                continue
            try:
                review_id = row["id"]
                commit_id = row["commit_id"]
                state = row["state"]
                body_value = row.get("body")
                submitted_at = row["submitted_at"]
                if (
                    not isinstance(review_id, int)
                    or isinstance(review_id, bool)
                    or review_id <= 0
                    or not isinstance(commit_id, str)
                    or re.fullmatch(r"[0-9a-fA-F]{40}", commit_id) is None
                    or not isinstance(state, str)
                    or (body_value is not None and not isinstance(body_value, str))
                    or not isinstance(submitted_at, str)
                ):
                    raise ValueError("review fields malformed")
                submitted_datetime = datetime.fromisoformat(
                    submitted_at.replace("Z", "+00:00")
                )
                if submitted_datetime.tzinfo is None:
                    raise ValueError("submitted_at timezone missing")
                submitted_epoch = submitted_datetime.timestamp()
                reviews.append(
                    CopilotReview(
                        review_id=review_id,
                        commit_id=commit_id.lower(),
                        state=state,
                        body=body_value or "",
                        author=login,
                        submitted_at_epoch=submitted_epoch,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("GitHub Copilot review malformed") from exc

        graph = self._work_graph(repo=repo, pr_number=pr_number)
        try:
            issue_numbers: list[int] = []
            for node in graph["closingIssuesReferences"]["nodes"]:  # type: ignore[index]
                if (
                    not isinstance(node, dict)
                    or not isinstance(node.get("number"), int)
                    or isinstance(node.get("number"), bool)
                    or not isinstance(node.get("repository"), dict)
                    or not isinstance(node["repository"].get("nameWithOwner"), str)
                ):
                    raise TypeError("closing issue node malformed")
                if node["repository"]["nameWithOwner"] == repo:
                    issue_numbers.append(node["number"])
            issues = tuple(sorted(issue_numbers))
            parsed_threads: list[ReviewThread] = []
            for node in graph["reviewThreads"]["nodes"]:  # type: ignore[index]
                if (
                    not isinstance(node, dict)
                    or not isinstance(node.get("id"), str)
                    or not node["id"]
                    or not isinstance(node.get("isResolved"), bool)
                    or not isinstance(node.get("isOutdated"), bool)
                ):
                    raise TypeError("review thread node malformed")
                parsed_threads.append(
                    ReviewThread(
                        thread_id=node["id"],
                        resolved=node["isResolved"],
                        outdated=node["isOutdated"],
                    )
                )
            threads = tuple(parsed_threads)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("GitHub PR association facts malformed") from exc
        # Ship validates the exact candidate tree.  The default branch still
        # contains the active change until this PR is merged.
        paths = self._commit_tree_paths(repo=repo, commit=head)
        active_absent, archive_present = self._openspec_facts(paths, change)
        return DeliveryFacts(
            head=head,
            mergeable=pull.get("mergeable") is True,
            mergeable_state=str(pull.get("mergeable_state", "")),
            checks=tuple(checks),
            copilot_reviews=tuple(reviews),
            review_threads=threads,
            closing_issues=issues,
            active_openspec_absent=active_absent,
            archive_present=archive_present,
        )

    def fetch_remote_closure(
        self,
        *,
        repo: str,
        pr_number: int,
        change: str,
        required_issues: tuple[int, ...],
        todo_paths: tuple[str, ...],
    ) -> RemoteClosureFacts:
        self._repo_parts(repo)
        pull = self._api(f"repos/{repo}/pulls/{pr_number}")
        repository = self._api(f"repos/{repo}")
        if not isinstance(pull, dict) or not isinstance(repository, dict):
            raise RuntimeError("GitHub closure payload malformed")
        merge_commit = pull.get("merge_commit_sha")
        try:
            pr_head = pull["head"]["sha"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("GitHub merged PR head malformed") from exc
        default_branch = repository.get("default_branch")
        if (
            pull.get("merged_at") is None
            or not isinstance(merge_commit, str)
            or not isinstance(default_branch, str)
            or not isinstance(pr_head, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", pr_head) is None
        ):
            raise RuntimeError("GitHub merge evidence incomplete")
        encoded_branch = quote(default_branch, safe="")
        default_ref = self._api(f"repos/{repo}/git/ref/heads/{encoded_branch}")
        try:
            default_head = default_ref["object"]["sha"]  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("GitHub default branch ref malformed") from exc
        if (
            not isinstance(default_head, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", default_head) is None
        ):
            raise RuntimeError("GitHub default branch ref malformed")
        default_head = default_head.lower()
        comparison = self._api(
            f"repos/{repo}/compare/{merge_commit}...{default_head}"
        )
        if not isinstance(comparison, dict):
            raise RuntimeError("GitHub ancestry comparison malformed")
        merge_payload = self._api(f"repos/{repo}/git/commits/{merge_commit}")
        if not isinstance(merge_payload, dict) or not isinstance(
            merge_payload.get("parents"), list
        ):
            raise RuntimeError("GitHub merge commit payload malformed")
        parent_rows = merge_payload["parents"]
        merge_parents: list[str] = []
        for parent in parent_rows:
            if (
                not isinstance(parent, dict)
                or not isinstance(parent.get("sha"), str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", parent["sha"]) is None
            ):
                raise RuntimeError("GitHub merge commit parent malformed")
            merge_parents.append(parent["sha"].lower())
        issue_states: dict[int, str] = {}
        for issue in required_issues:
            payload = self._api(f"repos/{repo}/issues/{issue}")
            if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
                raise RuntimeError("GitHub issue state malformed")
            issue_states[issue] = payload["state"]
        paths = self._commit_tree_paths(repo=repo, commit=default_head)
        active_absent, archive_present = self._openspec_facts(paths, change)
        if not todo_paths:
            raise ValueError("remote Todo paths are required for closure")
        todo_revisions: dict[str, str] = {}
        todo_complete = True
        task_pattern = re.compile(r"(?m)^\s*[-*]\s+\[([ xX])\]\s+")
        for todo_path in todo_paths:
            pure = PurePosixPath(todo_path)
            if (
                not todo_path
                or pure.is_absolute()
                or ".." in pure.parts
                or pure.suffix.lower() != ".md"
            ):
                raise ValueError("Todo path must be a safe repo-relative markdown path")
            encoded_path = quote(todo_path, safe="/")
            encoded_ref = quote(default_head, safe="")
            payload = self._api(
                f"repos/{repo}/contents/{encoded_path}?ref={encoded_ref}"
            )
            if (
                not isinstance(payload, dict)
                or payload.get("type") != "file"
                or payload.get("encoding") != "base64"
                or not isinstance(payload.get("content"), str)
                or not isinstance(payload.get("sha"), str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", payload["sha"]) is None
            ):
                raise RuntimeError("GitHub remote Todo payload malformed")
            try:
                encoded_content = "".join(payload["content"].split())
                content = base64.b64decode(
                    encoded_content, validate=True
                ).decode("utf-8")
            except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
                raise RuntimeError("GitHub remote Todo content malformed") from exc
            task_states = task_pattern.findall(content)
            if not task_states or any(state == " " for state in task_states):
                todo_complete = False
            todo_revisions[todo_path] = payload["sha"].lower()
        return RemoteClosureFacts(
            merge_commit=merge_commit,
            pr_head=pr_head.lower(),
            merge_parents=tuple(merge_parents),
            default_head=default_head,
            merge_is_ancestor=comparison.get("status") in {"ahead", "identical"},
            merge_is_merge_commit=len(merge_parents) >= 2,
            issue_states=issue_states,
            active_openspec_absent=active_absent,
            archive_present=archive_present,
            todo_complete=todo_complete,
            todo_revisions=todo_revisions,
            completion_record_valid=False,
        )

    def request_copilot(self, *, repo: str, pr_number: int) -> None:
        self._run(
            build_copilot_request_argv(repo=repo, pr_number=pr_number),
            expect_json=True,
        )

    def fetch_default_branch(self, *, repo: str) -> str:
        self._repo_parts(repo)
        payload = self._api(f"repos/{repo}")
        branch = payload.get("default_branch") if isinstance(payload, dict) else None
        if not isinstance(branch, str) or not branch:
            raise RuntimeError("GitHub default branch unavailable")
        return branch

    def create_or_get_pull_request(
        self,
        *,
        repo: str,
        branch: str,
        expected_head: str,
        title: str,
        body: str,
        labels: tuple[str, ...],
    ) -> int:
        """Idempotently create the only open PR for an authenticated exact head."""

        owner, _name = self._repo_parts(repo)
        if (
            not isinstance(branch, str)
            or not branch
            or branch.startswith("-")
            or any(character.isspace() for character in branch)
        ):
            raise ValueError("PR branch must be a safe non-empty name")
        if re.fullmatch(r"[0-9a-fA-F]{40}", expected_head or "") is None:
            raise ValueError("expected_head must be a 40-character hexadecimal SHA")
        repository = self._api(f"repos/{repo}")
        if not isinstance(repository, dict) or not isinstance(
            repository.get("default_branch"), str
        ):
            raise RuntimeError("GitHub default branch unavailable")
        default_branch = repository["default_branch"]
        encoded_head = quote(f"{owner}:{branch}", safe="")
        existing = self._api(
            f"repos/{repo}/pulls?state=open&head={encoded_head}&per_page=100"
        )
        if not isinstance(existing, list):
            raise RuntimeError("GitHub open PR lookup malformed")
        exact: list[dict] = []
        for row in existing:
            if not isinstance(row, dict):
                raise RuntimeError("GitHub open PR lookup malformed")
            head = row.get("head")
            base = row.get("base")
            if not isinstance(head, dict) or not isinstance(base, dict):
                raise RuntimeError("GitHub open PR refs malformed")
            if head.get("ref") == branch:
                if (
                    head.get("sha") != expected_head.lower()
                    or base.get("ref") != default_branch
                ):
                    raise RuntimeError("existing PR branch is bound to a different head/base")
                exact.append(row)
        if len(exact) > 1:
            raise RuntimeError("multiple open PRs match the workflow branch")
        if exact:
            pull = exact[0]
        else:
            pull = self._run(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repo}/pulls",
                    "-f",
                    f"title={title}",
                    "-f",
                    f"body={body}",
                    "-f",
                    f"head={branch}",
                    "-f",
                    f"base={default_branch}",
                ],
                expect_json=True,
            )
        if not isinstance(pull, dict):
            raise RuntimeError("GitHub PR create payload malformed")
        number = pull.get("number")
        head = pull.get("head")
        base = pull.get("base")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number <= 0
            or not isinstance(head, dict)
            or head.get("sha") != expected_head.lower()
            or head.get("ref") != branch
            or not isinstance(base, dict)
            or base.get("ref") != default_branch
        ):
            raise RuntimeError("GitHub PR create identity mismatch")
        self.ensure_pr_metadata(
            repo=repo,
            pr_number=number,
            title=title,
            body=body,
            labels=labels,
        )
        return number

    def ensure_pr_metadata(
        self,
        *,
        repo: str,
        pr_number: int,
        title: str,
        body: str,
        labels: tuple[str, ...],
    ) -> None:
        """Update an existing authorized PR and prove every field by reread."""

        self._repo_parts(repo)
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
        if not isinstance(title, str) or not title or not isinstance(body, str) or not body:
            raise ValueError("PR title/body must be non-empty strings")
        if (
            not labels
            or any(not isinstance(label, str) or not label.strip() for label in labels)
            or len(set(labels)) != len(labels)
        ):
            raise ValueError("PR labels must be unique non-empty strings")
        self._metadata_json(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"repos/{repo}/pulls/{pr_number}",
                "-f",
                f"title={title}",
                "-f",
                f"body={body}",
            ]
        )
        label_argv = [
            "gh",
            "api",
            "--method",
            "PUT",
            f"repos/{repo}/issues/{pr_number}/labels",
        ]
        for label in labels:
            label_argv.extend(["-f", f"labels[]={label}"])
        self._metadata_json(label_argv)
        pull = self._metadata_json(
            ["gh", "api", f"repos/{repo}/pulls/{pr_number}"]
        )
        issue = self._metadata_json(
            ["gh", "api", f"repos/{repo}/issues/{pr_number}"]
        )
        if not isinstance(pull, dict) or not isinstance(issue, dict):
            raise RuntimeError("GitHub PR metadata reread malformed")
        rows = issue.get("labels")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("name"), str)
            for row in rows
        ):
            raise RuntimeError("GitHub PR metadata reread malformed")
        remote_labels = tuple(sorted(row["name"] for row in rows))
        if (
            pull.get("title") != title
            or pull.get("body") != body
            or remote_labels != tuple(sorted(labels))
        ):
            raise RuntimeError("GitHub PR metadata reread mismatch")

    def fetch_merge_status(self, *, repo: str, pr_number: int) -> MergeStatus:
        self._repo_parts(repo)
        pull = self._api(f"repos/{repo}/pulls/{pr_number}")
        if not isinstance(pull, dict):
            raise RuntimeError("GitHub PR merge status malformed")
        try:
            head = pull["head"]["sha"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("GitHub PR merge status malformed") from exc
        merge_commit = pull.get("merge_commit_sha")
        merged = pull.get("merged_at") is not None
        if (
            not isinstance(head, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", head) is None
            or (merged and (
                not isinstance(merge_commit, str)
                or re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit) is None
            ))
        ):
            raise RuntimeError("GitHub PR merge status malformed")
        return MergeStatus(
            merged=merged,
            pr_head=head.lower(),
            merge_commit=merge_commit.lower() if merged else None,
        )

    def merge(
        self,
        *,
        repo: str,
        pr_number: int,
        expected_head: str,
        _capability: object | None = None,
    ) -> None:
        if _capability is not _SHIP_CAPABILITY:
            raise PermissionError("merge is restricted to ShipOrchestrator")
        self._run(
            build_merge_argv(repo=repo, pr_number=pr_number, expected_head=expected_head),
            expect_json=False,
        )

    def merge_if_ready(
        self,
        *,
        repo: str,
        pr_number: int,
        change: str,
        policy: DeliveryPolicy,
        _capability: object | None = None,
    ) -> DeliveryFacts:
        if _capability is not _SHIP_CAPABILITY:
            raise PermissionError("merge admission is restricted to ShipOrchestrator")
        facts = self.fetch_delivery_facts(
            repo=repo,
            pr_number=pr_number,
            change=change,
        )
        result = evaluate_delivery_gate(facts=facts, policy=policy)
        if not result.allowed:
            raise RuntimeError(f"delivery gate blocked: {', '.join(result.reasons)}")
        self.merge(
            repo=repo,
            pr_number=pr_number,
            expected_head=policy.expected_head,
            _capability=_capability,
        )
        return facts
