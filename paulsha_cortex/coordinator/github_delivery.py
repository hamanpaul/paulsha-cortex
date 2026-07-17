"""Fail-closed GitHub delivery gates for a single immutable PR HEAD."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping


GREEN_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
COPILOT_ERROR_MARKERS = (
    "encountered an error",
    "failed to review",
    "unable to review",
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

    current_reviews = tuple(
        review for review in facts.copilot_reviews if review.commit_id == policy.expected_head
    )
    if not current_reviews:
        reasons.append("copilot-current-head-review-missing")
    elif any(review.is_error for review in current_reviews):
        reasons.append("copilot-error-review")
    elif any(review.state.upper() not in {"COMMENTED", "APPROVED"} for review in current_reviews):
        reasons.append("copilot-review-state-invalid")

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


def build_merge_argv(*, pr_number: int, expected_head: str) -> list[str]:
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
        "--merge",
        "--match-head-commit",
        expected_head,
    ]


@dataclass(frozen=True)
class RemoteClosureFacts:
    merge_commit: str
    merge_is_ancestor: bool
    merge_is_merge_commit: bool
    issue_states: Mapping[int, str]
    active_openspec_absent: bool
    archive_present: bool
    todo_complete: bool
    completion_record_valid: bool


def evaluate_remote_closure(
    *,
    facts: RemoteClosureFacts,
    required_issues: tuple[int, ...],
) -> GateResult:
    reasons: list[str] = []
    if len(facts.merge_commit) != 40 or not facts.merge_is_ancestor:
        reasons.append("merge-ancestry-unverified")
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
query($owner:String!,$name:String!,$number:Int!) {
  repository(owner:$owner,name:$name) {
    pullRequest(number:$number) {
      closingIssuesReferences(first:100) { nodes { number } }
      reviewThreads(first:100) { nodes { id isResolved isOutdated } }
    }
  }
}
""".strip()


class GitHubDeliveryClient:
    """Authenticated ``gh api`` seam; any malformed remote fact fails closed."""

    def __init__(self, *, runner: Runner = subprocess.run) -> None:
        self._runner = runner

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

    @staticmethod
    def _repo_parts(repo: str) -> tuple[str, str]:
        parts = repo.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repo must be owner/name")
        return parts[0], parts[1]

    def _work_graph(self, *, repo: str, pr_number: int) -> dict[str, object]:
        owner, name = self._repo_parts(repo)
        payload = self._run(
            [
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
            ],
            expect_json=True,
        )
        try:
            work = payload["data"]["repository"]["pullRequest"]  # type: ignore[index]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("GitHub GraphQL work facts malformed") from exc
        if not isinstance(work, dict):
            raise RuntimeError("GitHub GraphQL pullRequest missing")
        return work

    def _tree_paths(self, *, repo: str, ref: str) -> tuple[str, ...]:
        payload = self._api(f"repos/{repo}/git/trees/{ref}?recursive=1")
        if not isinstance(payload, dict) or payload.get("truncated") is True:
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

        check_payload = self._api(f"repos/{repo}/commits/{head}/check-runs")
        status_payload = self._api(f"repos/{repo}/commits/{head}/status")
        review_payload = self._api(f"repos/{repo}/pulls/{pr_number}/reviews")
        if not isinstance(check_payload, dict) or not isinstance(
            check_payload.get("check_runs"), list
        ):
            raise RuntimeError("GitHub check runs malformed")
        if not isinstance(status_payload, dict) or not isinstance(
            status_payload.get("statuses"), list
        ):
            raise RuntimeError("GitHub commit statuses malformed")
        if not isinstance(review_payload, list):
            raise RuntimeError("GitHub reviews malformed")

        checks: list[GitHubCheck] = []
        for row in check_payload["check_runs"]:
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
        for row in status_payload["statuses"]:
            if not isinstance(row, dict):
                raise RuntimeError("GitHub commit status malformed")
            state = row.get("state")
            context = row.get("context")
            if not isinstance(state, str) or not isinstance(context, str):
                raise RuntimeError("GitHub commit status malformed")
            checks.append(
                GitHubCheck(
                    name=context,
                    status="in_progress" if state == "pending" else "completed",
                    conclusion="success" if state == "success" else state,
                )
            )
        reviews: list[CopilotReview] = []
        for row in review_payload:
            if not isinstance(row, dict):
                raise RuntimeError("GitHub review malformed")
            login = row.get("user", {}).get("login") if isinstance(row.get("user"), dict) else None
            if not isinstance(login, str) or "copilot" not in login.casefold():
                continue
            try:
                reviews.append(
                    CopilotReview(
                        review_id=int(row["id"]),
                        commit_id=str(row["commit_id"]),
                        state=str(row["state"]),
                        body=str(row.get("body") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("GitHub Copilot review malformed") from exc

        graph = self._work_graph(repo=repo, pr_number=pr_number)
        try:
            issues = tuple(
                sorted(
                    int(node["number"])
                    for node in graph["closingIssuesReferences"]["nodes"]  # type: ignore[index]
                )
            )
            threads = tuple(
                ReviewThread(
                    thread_id=str(node["id"]),
                    resolved=bool(node["isResolved"]),
                    outdated=bool(node["isOutdated"]),
                )
                for node in graph["reviewThreads"]["nodes"]  # type: ignore[index]
            )
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
        todo_complete: bool,
        completion_record_valid: bool,
    ) -> RemoteClosureFacts:
        self._repo_parts(repo)
        pull = self._api(f"repos/{repo}/pulls/{pr_number}")
        repository = self._api(f"repos/{repo}")
        if not isinstance(pull, dict) or not isinstance(repository, dict):
            raise RuntimeError("GitHub closure payload malformed")
        merge_commit = pull.get("merge_commit_sha")
        default_branch = repository.get("default_branch")
        if (
            pull.get("merged_at") is None
            or not isinstance(merge_commit, str)
            or not isinstance(default_branch, str)
        ):
            raise RuntimeError("GitHub merge evidence incomplete")
        comparison = self._api(
            f"repos/{repo}/compare/{merge_commit}...{default_branch}"
        )
        if not isinstance(comparison, dict):
            raise RuntimeError("GitHub ancestry comparison malformed")
        merge_payload = self._api(f"repos/{repo}/git/commits/{merge_commit}")
        if not isinstance(merge_payload, dict) or not isinstance(
            merge_payload.get("parents"), list
        ):
            raise RuntimeError("GitHub merge commit payload malformed")
        issue_states: dict[int, str] = {}
        for issue in required_issues:
            payload = self._api(f"repos/{repo}/issues/{issue}")
            if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
                raise RuntimeError("GitHub issue state malformed")
            issue_states[issue] = payload["state"]
        paths = self._commit_tree_paths(repo=repo, commit=default_branch)
        active_absent, archive_present = self._openspec_facts(paths, change)
        return RemoteClosureFacts(
            merge_commit=merge_commit,
            merge_is_ancestor=comparison.get("status") in {"ahead", "identical"},
            merge_is_merge_commit=len(merge_payload["parents"]) >= 2,
            issue_states=issue_states,
            active_openspec_absent=active_absent,
            archive_present=archive_present,
            todo_complete=todo_complete,
            completion_record_valid=completion_record_valid,
        )

    def request_copilot(self, *, repo: str, pr_number: int) -> None:
        self._run(
            build_copilot_request_argv(repo=repo, pr_number=pr_number),
            expect_json=True,
        )

    def merge(self, *, pr_number: int, expected_head: str) -> None:
        self._run(
            build_merge_argv(pr_number=pr_number, expected_head=expected_head),
            expect_json=False,
        )

    def merge_if_ready(
        self,
        *,
        repo: str,
        pr_number: int,
        change: str,
        policy: DeliveryPolicy,
    ) -> DeliveryFacts:
        facts = self.fetch_delivery_facts(
            repo=repo,
            pr_number=pr_number,
            change=change,
        )
        result = evaluate_delivery_gate(facts=facts, policy=policy)
        if not result.allowed:
            raise RuntimeError(f"delivery gate blocked: {', '.join(result.reasons)}")
        self.merge(pr_number=pr_number, expected_head=policy.expected_head)
        return facts
