"""RED→GREEN tests for `cortex work gc`（#178 feat-work-gc-v2）。

全程在 tmp git repo fixture 內建構，不碰真 repo、不需網路。任何跟真實
`~/.agents` 或 `paulsha-cortex-worktrees` 有關的路徑一律不得出現於這支測試。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import gc


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")


def _artifact(artifacts, kind: str, identifier: str) -> gc.Artifact:
    for artifact in artifacts:
        if artifact.kind == kind and artifact.identifier == identifier:
            return artifact
    raise AssertionError(f"no {kind} artifact for {identifier!r} in {artifacts!r}")


def _branch_exists(root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# R2: merged 判定必須內容層驗證
# ---------------------------------------------------------------------------


def test_ancestor_merged_branch_classified_reclaim(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/ancestor")
    (repo / "ancestor.txt").write_text("ancestor\n", encoding="utf-8")
    _git(repo, "add", "ancestor.txt")
    _git(repo, "commit", "-qm", "ancestor commit")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/ancestor")

    report = gc.run_gc(repo, worktree_root=tmp_path / "no-pool")

    artifact = _artifact(report.artifacts, "branch", "feature/ancestor")
    assert artifact.action == gc.ACTION_RECLAIM
    assert artifact.reason == gc.REASON_MERGED_ANCESTOR
    assert _branch_exists(repo, "feature/ancestor")


def test_squash_merged_branch_classified_reclaim(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/squash")
    (repo / "squash.txt").write_text("squash payload\n", encoding="utf-8")
    _git(repo, "add", "squash.txt")
    _git(repo, "commit", "-qm", "squash source commit")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "feature/squash")
    _git(repo, "commit", "-qm", "squash merge feature/squash")

    # squash-merge 後原分支 commit 不在 default branch 歷史（ref-ancestry 失真）。
    ancestor_probe = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "feature/squash", "main"],
        check=False,
    )
    assert ancestor_probe.returncode != 0

    report = gc.run_gc(repo, worktree_root=tmp_path / "no-pool")

    artifact = _artifact(report.artifacts, "branch", "feature/squash")
    assert artifact.action == gc.ACTION_RECLAIM
    assert artifact.reason == gc.REASON_MERGED_CONTENT


def test_content_mismatch_classified_unmerged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/unmerged")
    (repo / "unmerged.txt").write_text("never landed\n", encoding="utf-8")
    _git(repo, "add", "unmerged.txt")
    _git(repo, "commit", "-qm", "unmerged commit")
    _git(repo, "checkout", "-q", "main")

    report = gc.run_gc(repo, worktree_root=tmp_path / "no-pool")

    artifact = _artifact(report.artifacts, "branch", "feature/unmerged")
    assert artifact.action == gc.ACTION_KEEP
    assert artifact.reason == gc.REASON_UNMERGED_CONTENT


# ---------------------------------------------------------------------------
# R3: fail-safe：疑義一律保留
# ---------------------------------------------------------------------------


def test_unmerged_branch_never_in_apply_list(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/unmerged")
    (repo / "unmerged.txt").write_text("never landed\n", encoding="utf-8")
    _git(repo, "add", "unmerged.txt")
    _git(repo, "commit", "-qm", "unmerged commit")
    _git(repo, "checkout", "-q", "main")

    report = gc.run_gc(repo, apply=True, worktree_root=tmp_path / "no-pool")

    artifact = _artifact(report.artifacts, "branch", "feature/unmerged")
    assert artifact.action == gc.ACTION_KEEP
    assert artifact.reason == gc.REASON_UNMERGED_CONTENT
    assert _branch_exists(repo, "feature/unmerged")


def test_dirty_worktree_kept(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    pool = tmp_path / "repo-worktrees"
    pool.mkdir()
    worktree_path = pool / "feature-dirty"
    _git(repo, "worktree", "add", str(worktree_path), "-b", "feature/dirty")
    (worktree_path / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    report = gc.run_gc(repo, apply=True, worktree_root=pool)

    wt_artifact = _artifact(report.artifacts, "worktree", str(worktree_path))
    assert wt_artifact.action == gc.ACTION_KEEP
    assert wt_artifact.reason == gc.REASON_DIRTY_WORKTREE
    assert worktree_path.exists()
    assert (worktree_path / "scratch.txt").exists()

    # 分支掛在被保留的 worktree 上：即便內容本身可判 merged，仍必須 protected，
    # 不得被 `git branch -D`（git 本身也會拒絕，但 GC 必須先分類正確並附理由）。
    branch_artifact = _artifact(report.artifacts, "branch", "feature/dirty")
    assert branch_artifact.action == gc.ACTION_KEEP
    assert branch_artifact.reason == gc.REASON_PROTECTED
    assert _branch_exists(repo, "feature/dirty")


def test_closed_unmerged_pr_branch_kept_with_annotation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/pr-closed")
    (repo / "pr.txt").write_text("closed pr content\n", encoding="utf-8")
    _git(repo, "add", "pr.txt")
    _git(repo, "commit", "-qm", "pr commit")
    _git(repo, "checkout", "-q", "main")

    def closed_provider(_repo_root: Path, branch: str) -> str | None:
        return "closed_unmerged" if branch == "feature/pr-closed" else None

    report = gc.run_gc(
        repo,
        worktree_root=tmp_path / "no-pool",
        pr_status_provider=closed_provider,
    )
    artifact = _artifact(report.artifacts, "branch", "feature/pr-closed")
    assert artifact.action == gc.ACTION_KEEP
    assert artifact.reason == gc.REASON_PR_CLOSED_UNMERGED

    def raising_provider(_repo_root: Path, _branch: str) -> str | None:
        raise RuntimeError("gh unavailable")

    degraded_report = gc.run_gc(
        repo,
        worktree_root=tmp_path / "no-pool",
        pr_status_provider=raising_provider,
    )
    degraded_artifact = _artifact(degraded_report.artifacts, "branch", "feature/pr-closed")
    assert degraded_artifact.action == gc.ACTION_KEEP
    assert degraded_artifact.reason == gc.REASON_UNMERGED_CONTENT

    absent_report = gc.run_gc(
        repo,
        worktree_root=tmp_path / "no-pool",
        pr_status_provider=None,
    )
    absent_artifact = _artifact(absent_report.artifacts, "branch", "feature/pr-closed")
    assert absent_artifact.action == gc.ACTION_KEEP
    assert absent_artifact.reason == gc.REASON_UNMERGED_CONTENT


def test_git_error_fails_safe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/ok")
    (repo / "ok.txt").write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "ok.txt")
    _git(repo, "commit", "-qm", "ok commit")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/ok")

    _git(repo, "checkout", "-qb", "feature/broken")
    (repo / "broken.txt").write_text("broken\n", encoding="utf-8")
    _git(repo, "add", "broken.txt")
    _git(repo, "commit", "-qm", "broken commit")
    _git(repo, "checkout", "-q", "main")

    def flaky_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        if "cherry" in args:
            raise RuntimeError("simulated git failure")
        return subprocess.run(["git", *args], capture_output=True, text=True)

    report = gc.run_gc(
        repo,
        worktree_root=tmp_path / "no-pool",
        git_runner=flaky_runner,
    )

    broken_artifact = _artifact(report.artifacts, "branch", "feature/broken")
    assert broken_artifact.action == gc.ACTION_KEEP
    assert broken_artifact.reason == gc.REASON_VERIFICATION_ERROR

    # 單一 git 命令失敗不得中斷整批判定：其餘項目仍要照常分類。
    ok_artifact = _artifact(report.artifacts, "branch", "feature/ok")
    assert ok_artifact.action == gc.ACTION_RECLAIM
    assert ok_artifact.reason == gc.REASON_MERGED_ANCESTOR


# ---------------------------------------------------------------------------
# R1: proposal-first 回收命令
# ---------------------------------------------------------------------------


def test_default_dry_run_mutates_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    pool = tmp_path / "repo-worktrees"
    pool.mkdir()

    # 已 merge 分支：也建一個乾淨 worktree 掛在上面，驗證即使分類為 reclaim，
    # dry-run 仍完全不動它。
    _git(repo, "checkout", "-qb", "feature/clean-merged")
    (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "clean.txt")
    _git(repo, "commit", "-qm", "clean commit")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/clean-merged")
    clean_worktree = pool / "feature-clean-merged"
    _git(repo, "worktree", "add", str(clean_worktree), "feature/clean-merged")

    # 未 merge 分支。
    _git(repo, "checkout", "-qb", "feature/still-open")
    (repo / "open.txt").write_text("open\n", encoding="utf-8")
    _git(repo, "add", "open.txt")
    _git(repo, "commit", "-qm", "open commit")
    _git(repo, "checkout", "-q", "main")

    # dirty worktree。
    dirty_worktree = pool / "feature-dirty"
    _git(repo, "worktree", "add", str(dirty_worktree), "-b", "feature/dirty")
    (dirty_worktree / "scratch.txt").write_text("untracked\n", encoding="utf-8")

    jobs_json = tmp_path / "jobs.json"
    jobs_json.write_text('{"jobs": []}\n', encoding="utf-8")

    def _snapshot() -> tuple[str, str, bytes, bool, bool]:
        worktree_list = _git(repo, "worktree", "list", "--porcelain").stdout
        branch_list = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/").stdout
        return (
            worktree_list,
            branch_list,
            jobs_json.read_bytes(),
            (dirty_worktree / "scratch.txt").exists(),
            clean_worktree.exists(),
        )

    before = _snapshot()
    report = gc.run_gc(repo, apply=False, worktree_root=pool)
    after = _snapshot()

    assert before == after

    # 分類仍要如實回報（即使不執行）。
    clean_artifact = _artifact(report.artifacts, "worktree", str(clean_worktree))
    assert clean_artifact.action == gc.ACTION_RECLAIM
    dirty_artifact = _artifact(report.artifacts, "worktree", str(dirty_worktree))
    assert dirty_artifact.action == gc.ACTION_KEEP
    assert dirty_artifact.reason == gc.REASON_DIRTY_WORKTREE
    open_artifact = _artifact(report.artifacts, "branch", "feature/still-open")
    assert open_artifact.action == gc.ACTION_KEEP
    assert open_artifact.reason == gc.REASON_UNMERGED_CONTENT
    assert report.applied is False


# ---------------------------------------------------------------------------
# R4/R5: apply 執行、報告 schema、registry 唯讀
# ---------------------------------------------------------------------------


def test_apply_reclaims_clean_merged_worktree_and_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    pool = tmp_path / "repo-worktrees"
    pool.mkdir()

    _git(repo, "checkout", "-qb", "feature/clean-merged")
    (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "clean.txt")
    _git(repo, "commit", "-qm", "clean commit")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/clean-merged")
    clean_worktree = pool / "feature-clean-merged"
    _git(repo, "worktree", "add", str(clean_worktree), "feature/clean-merged")

    report = gc.run_gc(repo, apply=True, worktree_root=pool)

    assert report.applied is True
    wt_artifact = _artifact(report.artifacts, "worktree", str(clean_worktree))
    assert wt_artifact.action == gc.ACTION_RECLAIM
    assert not clean_worktree.exists()

    branch_artifact = _artifact(report.artifacts, "branch", "feature/clean-merged")
    assert branch_artifact.action == gc.ACTION_RECLAIM
    assert not _branch_exists(repo, "feature/clean-merged")


def test_apply_reverifies_before_mutating_toctou(tmp_path: Path) -> None:
    """R1／D2：`--apply` 對每個 reclaim 項目在執行前重新驗證，狀態已變時轉 keep+apply-error。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    pool = tmp_path / "repo-worktrees"
    pool.mkdir()

    _git(repo, "checkout", "-qb", "feature/clean-merged")
    (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "clean.txt")
    _git(repo, "commit", "-qm", "clean commit")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/clean-merged")
    clean_worktree = pool / "feature-clean-merged"
    _git(repo, "worktree", "add", str(clean_worktree), "feature/clean-merged")

    artifacts = gc.scan(repo, worktree_root=pool)
    wt_artifact = _artifact(artifacts, "worktree", str(clean_worktree))
    assert wt_artifact.action == gc.ACTION_RECLAIM

    # 在 scan 與 apply 之間注入一筆未 commit 變更，模擬 TOCTOU race。
    (clean_worktree / "late.txt").write_text("late change\n", encoding="utf-8")

    applied = gc.apply_gc(repo, artifacts, default_branch="main")
    result = _artifact(applied, "worktree", str(clean_worktree))

    assert result.action == gc.ACTION_KEEP
    assert result.reason == gc.REASON_APPLY_ERROR
    assert clean_worktree.exists()
    assert (clean_worktree / "late.txt").exists()

    # 分支候選也要重驗：worktree 未真的被移除，branch 仍掛在上面，
    # `git branch -D` 若被誤觸會失敗；apply 必須把它安全地記為 apply-error。
    branch_artifact = _artifact(applied, "branch", "feature/clean-merged")
    assert branch_artifact.action == gc.ACTION_KEEP
    assert branch_artifact.reason == gc.REASON_APPLY_ERROR
    assert _branch_exists(repo, "feature/clean-merged")


def test_cli_main_dry_run_text_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/unmerged")
    (repo / "unmerged.txt").write_text("never landed\n", encoding="utf-8")
    _git(repo, "add", "unmerged.txt")
    _git(repo, "commit", "-qm", "unmerged commit")
    _git(repo, "checkout", "-q", "main")

    rc = gc.main(["--repo-root", str(repo)])
    assert rc == 0
    text_output = capsys.readouterr().out
    assert "feature/unmerged" in text_output
    assert "keep" in text_output
    assert "mode=dry-run" in text_output

    rc = gc.main(["--repo-root", str(repo), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "cortex-work-gc/v1"
    assert payload["applied"] is False
    branch_entry = next(
        entry for entry in payload["artifacts"]
        if entry["kind"] == "branch" and entry["identifier"] == "feature/unmerged"
    )
    assert branch_entry["action"] == "keep"
    assert branch_entry["reason"] == "unmerged-content"

    # dry-run CLI 呼叫過後分支仍在。
    assert _branch_exists(repo, "feature/unmerged")


def test_json_report_matches_versioned_schema(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    report = gc.run_gc(repo, worktree_root=tmp_path / "no-pool")
    payload = report.to_dict()

    assert payload["schema"] == "cortex-work-gc/v1"
    assert payload["applied"] is False
    assert payload["repo_root"] == str(repo.resolve())
    assert isinstance(payload["artifacts"], list)
    for entry in payload["artifacts"]:
        assert {"kind", "identifier", "action", "reason"} <= set(entry)


def test_default_branch_and_current_branch_are_protected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    report = gc.run_gc(repo, worktree_root=tmp_path / "no-pool")

    main_artifact = _artifact(report.artifacts, "branch", "main")
    assert main_artifact.action == gc.ACTION_KEEP
    assert main_artifact.reason == gc.REASON_PROTECTED


def test_current_checked_out_branch_protected_even_when_content_mergeable(tmp_path: Path) -> None:
    """current-checked-out-branch 保護與 default-branch 保護是兩條獨立規則：
    repo_root 目前 checked out 在一個「內容上已可判 merged」的非 default 分支時，
    仍必須因為它是目前 checkout 而 protected，不得被 apply 誤刪。
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    _git(repo, "checkout", "-qb", "feature/checked-out-elsewhere")
    (repo / "here.txt").write_text("here\n", encoding="utf-8")
    _git(repo, "add", "here.txt")
    _git(repo, "commit", "-qm", "commit on checked-out branch")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/checked-out-elsewhere")
    # main 現在與 feature/checked-out-elsewhere 同 tip（merged-ancestor 可判）；
    # 接著把 repo_root 本身切到該分支，驗證它因為是「目前 checkout」而受保護。
    _git(repo, "checkout", "-q", "feature/checked-out-elsewhere")

    report = gc.run_gc(repo, apply=True, worktree_root=tmp_path / "no-pool")

    checked_out_artifact = _artifact(report.artifacts, "branch", "feature/checked-out-elsewhere")
    assert checked_out_artifact.action == gc.ACTION_KEEP
    assert checked_out_artifact.reason == gc.REASON_PROTECTED
    assert _branch_exists(repo, "feature/checked-out-elsewhere")

    # default branch 本身依然獨立受保護（fallback "main"）。
    main_artifact = _artifact(report.artifacts, "branch", "main")
    assert main_artifact.action == gc.ACTION_KEEP
    assert main_artifact.reason == gc.REASON_PROTECTED


def test_scan_does_not_import_registry_writers() -> None:
    import inspect

    source = inspect.getsource(gc)
    assert "JobRegistry" not in source
    assert "jobs.json" not in source
