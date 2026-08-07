"""`cortex work gc`：交付後產物回收（#178 feat-work-gc-v2）。

回收範圍僅限 worktree pool 內殘留的 build worktree，與 repo 的 local branch；
唯讀 registry（不 import 任何 manager 狀態檔寫入 API），不刪 remote branch、不動
PR、不清 delivery journal／correlation（見
`docs/superpowers/specs/feat-work-gc-v2-spec.md` 非目標）。

proposal-first：預設 dry-run 只分類不執行；`--apply` 才對 `reclaim` 項目執行，
且逐項於執行前重新驗證（防 TOCTOU）。merged 判定一律走內容層驗證鏈
（`git merge-base --is-ancestor` → `git cherry` 內容等價），不得以
`git branch -d`／`--merged`／ref-ancestry 為準——squash-merge 後 ref 層訊號會
騙人，這是本模組存在的核心理由。任何疑義（unmerged content、dirty worktree、
git 命令失敗、default／current／掛在 keep worktree 上的 branch）一律 `keep`
並附機器可讀 reason code，絕不誤刪。
"""
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from paulsha_cortex.config.paths import repo_root as default_repo_root
from paulsha_cortex.config.paths import worktree_root_for

from . import verification

GC_SCHEMA = "cortex-work-gc/v1"

GitRunner = Callable[[list[str]], object]
PRStatusProvider = Callable[[Path, str], "str | None"]

ACTION_RECLAIM = "reclaim"
ACTION_KEEP = "keep"

REASON_MERGED_ANCESTOR = "merged-ancestor"
REASON_MERGED_CONTENT = "merged-content"
REASON_UNMERGED_CONTENT = "unmerged-content"
REASON_DIRTY_WORKTREE = "dirty-worktree"
REASON_PROTECTED = "protected"
REASON_PR_CLOSED_UNMERGED = "pr-closed-unmerged"
REASON_VERIFICATION_ERROR = "verification-error"
REASON_APPLY_ERROR = "apply-error"


@dataclass
class Artifact:
    kind: str  # "worktree" | "branch"
    identifier: str  # worktree 絕對路徑 or 分支名
    action: str
    reason: str
    branch: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "identifier": self.identifier,
            "action": self.action,
            "reason": self.reason,
        }
        if self.branch is not None:
            payload["branch"] = self.branch
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass
class GCReport:
    repo_root: str
    applied: bool
    artifacts: list[Artifact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": GC_SCHEMA,
            "repo_root": self.repo_root,
            "applied": self.applied,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    def render_text(self) -> str:
        lines = []
        for artifact in self.artifacts:
            branch_note = ""
            if artifact.kind == "worktree" and artifact.branch:
                branch_note = f" [{artifact.branch}]"
            lines.append(
                f"{artifact.kind}\t{artifact.identifier}{branch_note}\t"
                f"{artifact.action}\t{artifact.reason}"
            )
        return "\n".join(lines)


@dataclass
class _WorktreeEntry:
    path: Path
    branch: str | None


def _default_git_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _git(repo: Path, args: list[str], git_runner: GitRunner | None) -> dict[str, Any]:
    runner = git_runner or _default_git_runner
    return verification._run_git(["-C", str(repo), *args], runner)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_default_branch(repo_root: Path, git_runner: GitRunner | None = None) -> str:
    """依 `origin/HEAD` 解析 default branch，無法解析時退回 `main`；MUST NOT fetch。"""
    result = _git(repo_root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], git_runner)
    if result["status"] == "ok":
        value = result["stdout"].strip()
        if value.startswith("origin/"):
            value = value[len("origin/") :]
        if value:
            return value
    return "main"


def resolve_current_branch(repo_root: Path, git_runner: GitRunner | None = None) -> str | None:
    """repo_root 本身（非 worktree pool）目前 checked-out 的分支；detached 時回 None。"""
    result = _git(repo_root, ["symbolic-ref", "--short", "HEAD"], git_runner)
    if result["status"] == "ok":
        value = result["stdout"].strip()
        return value or None
    return None


def list_worktrees(repo_root: Path, git_runner: GitRunner | None = None) -> list[_WorktreeEntry]:
    result = _git(repo_root, ["worktree", "list", "--porcelain"], git_runner)
    if result["status"] != "ok":
        return []
    entries: list[_WorktreeEntry] = []
    current: dict[str, str] = {}

    def _flush() -> None:
        if not current:
            return
        worktree = current.get("worktree")
        if not worktree:
            return
        branch_ref = current.get("branch")
        branch = None
        if branch_ref:
            branch = branch_ref[len("refs/heads/") :] if branch_ref.startswith("refs/heads/") else branch_ref
        entries.append(_WorktreeEntry(path=Path(worktree), branch=branch))

    for line in result["stdout"].splitlines():
        line = line.rstrip("\n")
        if not line:
            _flush()
            current = {}
            continue
        if line.startswith("worktree "):
            _flush()
            current = {"worktree": line[len("worktree ") :]}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :]
    _flush()
    return entries


def list_local_branches(repo_root: Path, git_runner: GitRunner | None = None) -> list[str]:
    result = _git(
        repo_root, ["for-each-ref", "--format=%(refname:short)", "refs/heads/"], git_runner
    )
    if result["status"] != "ok":
        return []
    return [line.strip() for line in result["stdout"].splitlines() if line.strip()]


def _classify_merge(
    repo_root: Path,
    default_branch: str,
    ref: str,
    git_runner: GitRunner | None,
) -> tuple[bool, str]:
    """R2 兩段驗證鏈：ancestor → cherry 內容等價。回傳 (merged, reason)。"""
    ancestor = _git(repo_root, ["merge-base", "--is-ancestor", ref, default_branch], git_runner)
    if ancestor["status"] == "ok":
        return True, REASON_MERGED_ANCESTOR
    if not (ancestor["status"] == "non-zero" and ancestor["returncode"] == 1):
        # runner-error / partial-evidence，或非預期 returncode（如 bad revision）
        return False, REASON_VERIFICATION_ERROR

    cherry = _git(repo_root, ["cherry", default_branch, ref], git_runner)
    if cherry["status"] != "ok":
        return False, REASON_VERIFICATION_ERROR
    lines = [line for line in cherry["stdout"].splitlines() if line.strip()]
    if any(line.startswith("+") for line in lines):
        return False, REASON_UNMERGED_CONTENT
    return True, REASON_MERGED_CONTENT


def _annotate_pr_status(
    repo_root: Path,
    branch: str | None,
    reason: str,
    pr_status_provider: PRStatusProvider | None,
) -> tuple[str, str | None]:
    """closed-unmerged PR best-effort 註記；provider 缺席／拋錯一律安全退化。"""
    if reason != REASON_UNMERGED_CONTENT or branch is None or pr_status_provider is None:
        return reason, None
    try:
        status = pr_status_provider(repo_root, branch)
    except Exception:
        return reason, None
    if status == "closed_unmerged":
        return REASON_PR_CLOSED_UNMERGED, f"PR for {branch} is closed and unmerged"
    return reason, None


def _classify_worktree(
    entry: _WorktreeEntry,
    *,
    repo_root: Path,
    default_branch: str,
    current_branch: str | None,
    git_runner: GitRunner | None,
    pr_status_provider: PRStatusProvider | None,
) -> Artifact:
    identifier = str(entry.path)
    if entry.branch is not None and entry.branch in (default_branch, current_branch):
        return Artifact("worktree", identifier, ACTION_KEEP, REASON_PROTECTED, branch=entry.branch)

    status = _git(entry.path, ["status", "--porcelain"], git_runner)
    if status["status"] != "ok":
        return Artifact(
            "worktree", identifier, ACTION_KEEP, REASON_VERIFICATION_ERROR, branch=entry.branch
        )
    if status["stdout"].strip():
        return Artifact("worktree", identifier, ACTION_KEEP, REASON_DIRTY_WORKTREE, branch=entry.branch)

    head_result = _git(entry.path, ["rev-parse", "HEAD"], git_runner)
    if head_result["status"] != "ok":
        return Artifact(
            "worktree", identifier, ACTION_KEEP, REASON_VERIFICATION_ERROR, branch=entry.branch
        )
    ref = entry.branch or head_result["stdout"].strip()

    merged, reason = _classify_merge(repo_root, default_branch, ref, git_runner)
    if merged:
        return Artifact("worktree", identifier, ACTION_RECLAIM, reason, branch=entry.branch)
    reason, detail = _annotate_pr_status(repo_root, entry.branch, reason, pr_status_provider)
    return Artifact("worktree", identifier, ACTION_KEEP, reason, branch=entry.branch, detail=detail)


def _classify_branch(
    branch: str,
    *,
    repo_root: Path,
    default_branch: str,
    current_branch: str | None,
    protected_branches: set[str],
    git_runner: GitRunner | None,
    pr_status_provider: PRStatusProvider | None,
) -> Artifact:
    if branch == default_branch or branch == current_branch or branch in protected_branches:
        return Artifact("branch", branch, ACTION_KEEP, REASON_PROTECTED, branch=branch)

    merged, reason = _classify_merge(repo_root, default_branch, branch, git_runner)
    if merged:
        return Artifact("branch", branch, ACTION_RECLAIM, reason, branch=branch)
    reason, detail = _annotate_pr_status(repo_root, branch, reason, pr_status_provider)
    return Artifact("branch", branch, ACTION_KEEP, reason, branch=branch, detail=detail)


def scan(
    repo_root: Path,
    *,
    git_runner: GitRunner | None = None,
    pr_status_provider: PRStatusProvider | None = None,
    worktree_root: Path | None = None,
) -> list[Artifact]:
    """唯讀分類：不改變任何 git 狀態，回傳逐項 Artifact。"""
    repo_root = Path(repo_root).resolve()
    default_branch = resolve_default_branch(repo_root, git_runner)
    current_branch = resolve_current_branch(repo_root, git_runner)
    pool_root = (worktree_root if worktree_root is not None else worktree_root_for(repo_root)).resolve()

    artifacts: list[Artifact] = []
    protected_branches: set[str] = set()

    for entry in list_worktrees(repo_root, git_runner):
        try:
            resolved = entry.path.resolve()
        except OSError:
            continue
        if not _is_under(resolved, pool_root):
            continue
        artifact = _classify_worktree(
            entry,
            repo_root=repo_root,
            default_branch=default_branch,
            current_branch=current_branch,
            git_runner=git_runner,
            pr_status_provider=pr_status_provider,
        )
        artifacts.append(artifact)
        if artifact.action != ACTION_RECLAIM and entry.branch:
            protected_branches.add(entry.branch)

    for branch in list_local_branches(repo_root, git_runner):
        artifact = _classify_branch(
            branch,
            repo_root=repo_root,
            default_branch=default_branch,
            current_branch=current_branch,
            protected_branches=protected_branches,
            git_runner=git_runner,
            pr_status_provider=pr_status_provider,
        )
        artifacts.append(artifact)

    return artifacts


def _keep_apply_error(artifact: Artifact, detail: str) -> Artifact:
    return Artifact(
        artifact.kind, artifact.identifier, ACTION_KEEP, REASON_APPLY_ERROR,
        branch=artifact.branch, detail=detail,
    )


def _apply_worktree(
    repo_root: Path,
    artifact: Artifact,
    default_branch: str,
    git_runner: GitRunner | None,
) -> Artifact:
    path = Path(artifact.identifier)
    if not path.exists():
        return _keep_apply_error(artifact, "worktree path no longer exists")
    status = _git(path, ["status", "--porcelain"], git_runner)
    if status["status"] != "ok" or status["stdout"].strip():
        return _keep_apply_error(artifact, "worktree no longer clean")
    head_result = _git(path, ["rev-parse", "HEAD"], git_runner)
    if head_result["status"] != "ok":
        return _keep_apply_error(artifact, "unable to resolve worktree HEAD")
    ref = artifact.branch or head_result["stdout"].strip()
    merged, reason = _classify_merge(repo_root, default_branch, ref, git_runner)
    if not merged:
        return _keep_apply_error(artifact, "worktree no longer verified merged")
    removal = _git(repo_root, ["worktree", "remove", str(path)], git_runner)
    if removal["status"] != "ok":
        return _keep_apply_error(artifact, f"git worktree remove failed: {removal['stderr']}")
    return Artifact(
        artifact.kind, artifact.identifier, ACTION_RECLAIM, reason,
        branch=artifact.branch, detail="removed",
    )


def _apply_branch(
    repo_root: Path,
    artifact: Artifact,
    default_branch: str,
    git_runner: GitRunner | None,
) -> Artifact:
    branch = artifact.identifier
    exists = _git(repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], git_runner)
    if exists["status"] != "ok":
        return _keep_apply_error(artifact, "branch no longer exists")
    merged, reason = _classify_merge(repo_root, default_branch, branch, git_runner)
    if not merged:
        return _keep_apply_error(artifact, "branch no longer verified merged")
    deletion = _git(repo_root, ["branch", "-D", branch], git_runner)
    if deletion["status"] != "ok":
        return _keep_apply_error(artifact, f"git branch -D failed: {deletion['stderr']}")
    return Artifact(
        artifact.kind, artifact.identifier, ACTION_RECLAIM, reason,
        branch=branch, detail="deleted",
    )


def apply_gc(
    repo_root: Path,
    artifacts: list[Artifact],
    *,
    default_branch: str | None = None,
    git_runner: GitRunner | None = None,
) -> list[Artifact]:
    """`--apply`：只處理 reclaim 項目，執行前逐項重新驗證（TOCTOU-safe）。

    順序固定為 worktree 先、branch 後——分支若仍掛在 worktree 上，
    `git branch -D` 必然失敗；先移除 worktree 才能安全刪除底下的分支。
    """
    repo_root = Path(repo_root).resolve()
    if default_branch is None:
        default_branch = resolve_default_branch(repo_root, git_runner)

    results_by_key: dict[tuple[str, str], Artifact] = {}
    for artifact in artifacts:
        key = (artifact.kind, artifact.identifier)
        if artifact.kind == "worktree" and artifact.action == ACTION_RECLAIM:
            results_by_key[key] = _apply_worktree(repo_root, artifact, default_branch, git_runner)
        else:
            results_by_key[key] = artifact

    for artifact in artifacts:
        key = (artifact.kind, artifact.identifier)
        if artifact.kind == "branch" and artifact.action == ACTION_RECLAIM:
            results_by_key[key] = _apply_branch(repo_root, artifact, default_branch, git_runner)

    return [results_by_key[(artifact.kind, artifact.identifier)] for artifact in artifacts]


def run_gc(
    repo_root: Path,
    *,
    apply: bool = False,
    git_runner: GitRunner | None = None,
    pr_status_provider: PRStatusProvider | None = None,
    worktree_root: Path | None = None,
) -> GCReport:
    repo_root = Path(repo_root).resolve()
    artifacts = scan(
        repo_root,
        git_runner=git_runner,
        pr_status_provider=pr_status_provider,
        worktree_root=worktree_root,
    )
    if apply:
        default_branch = resolve_default_branch(repo_root, git_runner)
        artifacts = apply_gc(
            repo_root, artifacts, default_branch=default_branch, git_runner=git_runner
        )
    return GCReport(repo_root=str(repo_root), applied=apply, artifacts=artifacts)


def default_pr_status_provider(
    repo_root: Path,
    branch: str,
    *,
    runner: Callable[..., Any] | None = None,
    timeout: float = 15.0,
) -> str | None:
    """best-effort `gh pr list` 查詢；不可用（無 gh／逾時／解析失敗）一律回 None。"""
    run = runner or subprocess.run
    try:
        proc = run(
            [
                "gh", "pr", "list", "--head", branch, "--state", "all",
                "--json", "state", "--limit", "1",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if not data:
        return None
    state = str(data[0].get("state", "")).upper()
    if state == "MERGED":
        return "merged"
    if state == "CLOSED":
        return "closed_unmerged"
    if state == "OPEN":
        return "open"
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex work gc",
        description="proposal-first 回收殘留 build worktree 與已 merge local branch",
    )
    parser.add_argument(
        "--repo-root", default=None, help="被治理的目標 git repo 根目錄（預設：cortex 路徑契約 repo_root()）"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="執行回收（只處理 reclaim 項目）；未帶此旗標為預設 dry-run，不改變任何 git 狀態",
    )
    parser.add_argument("--json", action="store_true", help="輸出 cortex-work-gc/v1 JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else default_repo_root()

    report = run_gc(
        repo_root, apply=args.apply, pr_status_provider=default_pr_status_provider
    )

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        text = report.render_text()
        if text:
            print(text)
        mode = "apply" if report.applied else "dry-run"
        print(f"repo_root={report.repo_root} mode={mode} artifacts={len(report.artifacts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
