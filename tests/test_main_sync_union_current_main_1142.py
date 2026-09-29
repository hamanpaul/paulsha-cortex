"""#1142 regressions：main-sync probe 套用 union、retry-build 綁定重試當下的 main。

owner 裁決（2026-09-29）「union＋重試用當下 main」：

1. repo 追蹤的 `.gitattributes` 宣告 `CHANGELOG.md merge=union`；ship main-sync
   probe 的 `merge-tree --write-tree` 以 `--attr-source=<M>` 讀 main 的
   attributes，CHANGELOG 同位置插入不再判 conflict，非 union 路徑的真衝突照判。
2. main-sync stop 後的 `retry-build` 在下達當下重新取得 origin/main，把「當下的
   M」寫進新的 evidence、修復指令與 run 綁定；harvest 以 quarantine ref 驗證修復
   候選是該 M 的後代，停機 evidence 原樣保留作稽核。
3. clean-behind 仍停在 needs_human（本票不改）。

全部以真 git 驗證（bare origin、bundle、merge-tree），不以假 runner 冒充。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import job_workspace, manager, work_actions, work_bridge
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator

from test_main_probe_gate_987 import (
    _SequencedProbeRunner,
    _advance_origin_with,
    _git,
    _init_probe_repo,
    _probe_success_prefix,
    _wire_origin,
)
from test_main_sync_retry_exit import _main_sync_stop, _retry_build


REPO_ROOT = Path(__file__).resolve().parents[1]
_UNION_ATTRIBUTES = "CHANGELOG.md merge=union\n"
_CHANGELOG_BASE = "## [Unreleased]\n\n- base\n\n## [0.1.0]\n\n- shipped\n"


def _try_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=False, capture_output=True, text=True
    )


def _commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _read_evidence(ref: str) -> dict[str, object]:
    envelope = json.loads(Path(ref).read_text(encoding="utf-8"))
    assert envelope["hash"] == work_bridge.verification.canonical_json_hash(
        envelope["payload"]
    )
    return envelope["payload"]


# ---------------------------------------------------------------------------
# 1. repo 追蹤的 union 宣告與 probe 的 attributes 來源
# ---------------------------------------------------------------------------


def test_repository_tracks_changelog_union_merge_attribute() -> None:
    attributes = REPO_ROOT / ".gitattributes"
    assert attributes.is_file() and not attributes.is_symlink()
    declarations = [
        line.split()
        for line in attributes.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert ["CHANGELOG.md", "merge=union"] in declarations


def _union_probe_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = _init_probe_repo(
        tmp_path / "repo",
        files={
            ".gitattributes": _UNION_ATTRIBUTES,
            "CHANGELOG.md": _CHANGELOG_BASE,
            "README.md": "base\n",
        },
    )
    origin = _wire_origin(repo, tmp_path / "origin" / "origin.git")
    _git(repo, "checkout", "-qb", "feature/probe")
    return repo, origin


def test_main_sync_probe_applies_tracked_union_to_changelog_top_insert(
    tmp_path: Path,
) -> None:
    repo, origin = _union_probe_repo(tmp_path)
    candidate = _commit_files(
        repo,
        {"CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- feature entry\n- base")},
        "feature changelog",
    )
    remote_main = _advance_origin_with(
        origin,
        tmp_path / "origin-changelog",
        files={"CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- main entry\n- base")},
        message="main changelog",
    )
    # attributes 來源不隨 ship clone 的工作樹漂移：工作樹刪掉 `.gitattributes`
    # （未 commit）後，probe 仍以 M 的 tree 套用 union。
    (repo / ".gitattributes").unlink()

    probe = work_bridge._probe_main_sync(
        worktree=repo, candidate=candidate, timeout_seconds=5.0
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "clean-behind"
    assert probe.main_head == remote_main
    assert probe.conflict_paths == ()
    merge_tree = probe.raw["merge-tree"]
    assert f"--attr-source={remote_main}" in merge_tree.argv
    assert merge_tree.argv.index(f"--attr-source={remote_main}") < merge_tree.argv.index(
        "merge-tree"
    )
    # clean-behind 仍是 needs_human（#1142 第 3 點不改）。
    stop = work_bridge._main_sync_stop_result(state_root=tmp_path / "state", probe=probe)
    assert stop["status"] == "needs_human"
    assert stop["reason"] == "candidate-behind-main"


def test_main_sync_probe_keeps_real_conflicts_outside_union_paths(tmp_path: Path) -> None:
    repo, origin = _union_probe_repo(tmp_path)
    candidate = _commit_files(
        repo,
        {
            "CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- feature entry\n- base"),
            "README.md": "feature readme\n",
        },
        "feature edits",
    )
    _advance_origin_with(
        origin,
        tmp_path / "origin-conflict",
        files={
            "CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- main entry\n- base"),
            "README.md": "main readme\n",
        },
        message="main edits",
    )

    probe = work_bridge._probe_main_sync(
        worktree=repo, candidate=candidate, timeout_seconds=5.0
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "conflict"
    # CHANGELOG 由 union 收斂，只剩 README 的真衝突。
    assert probe.conflict_paths == ("README.md",)
    assert probe.path_classification == "conflict"
    stop = work_bridge._main_sync_stop_result(state_root=tmp_path / "state", probe=probe)
    assert stop["reason"] == "candidate-conflicts-with-main"


def test_main_sync_probe_ignores_candidate_declared_union_attributes(
    tmp_path: Path,
) -> None:
    """Candidate 不能以自己新增的 attributes 把真衝突宣告成 union。"""

    repo, origin = _union_probe_repo(tmp_path)
    candidate = _commit_files(
        repo,
        {
            ".gitattributes": _UNION_ATTRIBUTES + "README.md merge=union\n",
            "README.md": "feature readme\n",
        },
        "feature widens union",
    )
    _advance_origin_with(
        origin,
        tmp_path / "origin-readme",
        files={"README.md": "main readme\n"},
        message="main readme",
    )
    # 工作樹再疊一層未 commit 的萬用 union，仍不得影響判定。
    (repo / ".gitattributes").write_text("* merge=union\n", encoding="utf-8")

    probe = work_bridge._probe_main_sync(
        worktree=repo, candidate=candidate, timeout_seconds=5.0
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "conflict"
    assert probe.conflict_paths == ("README.md",)


def _declare_readme_union_outside_m(
    repo: Path, tmp_path: Path, monkeypatch, *, source: str
) -> None:
    """在 M 的 tree 以外的 attributes 來源宣告 `README.md merge=union`。"""

    declaration = "README.md merge=union\n"
    if source == "info-attributes":
        git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info" / "attributes").write_text(declaration, encoding="utf-8")
        return
    home = tmp_path / "attr-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if source == "global-attributes-file":
        attributes = home / "global-attributes"
        attributes.write_text(declaration, encoding="utf-8")
        config = home / "global-gitconfig"
        config.write_text(f"[core]\n\tattributesFile = {attributes}\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        return
    if source == "xdg-attributes":
        xdg = home / "xdg"
        (xdg / "git").mkdir(parents=True)
        (xdg / "git" / "attributes").write_text(declaration, encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
        return
    raise AssertionError(source)


@pytest.mark.parametrize(
    "source", ["info-attributes", "global-attributes-file", "xdg-attributes"]
)
def test_main_sync_probe_only_reads_attributes_from_main_commit(
    tmp_path: Path, monkeypatch, source: str
) -> None:
    """來源 repo 的 `info/attributes`、`core.attributesFile`／XDG attributes 不得影響判定。

    M 只宣告 CHANGELOG union：README 兩邊真衝突時仍判 conflict，CHANGELOG 同位置
    插入仍由 M 的 union 收斂。
    """

    repo, origin = _union_probe_repo(tmp_path)
    candidate = _commit_files(
        repo,
        {
            "CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- feature entry\n- base"),
            "README.md": "feature readme\n",
        },
        "feature edits",
    )
    _advance_origin_with(
        origin,
        tmp_path / "origin-conflict",
        files={
            "CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- main entry\n- base"),
            "README.md": "main readme\n",
        },
        message="main edits",
    )
    _declare_readme_union_outside_m(repo, tmp_path, monkeypatch, source=source)

    probe = work_bridge._probe_main_sync(
        worktree=repo, candidate=candidate, timeout_seconds=5.0
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "conflict"
    assert probe.conflict_paths == ("README.md",)
    stop = work_bridge._main_sync_stop_result(state_root=tmp_path / "state", probe=probe)
    assert stop["reason"] == "candidate-conflicts-with-main"


def test_main_sync_probe_keeps_main_declared_changelog_union_despite_source_info_attributes(
    tmp_path: Path, monkeypatch
) -> None:
    repo, origin = _union_probe_repo(tmp_path)
    candidate = _commit_files(
        repo,
        {"CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- feature entry\n- base")},
        "feature changelog",
    )
    remote_main = _advance_origin_with(
        origin,
        tmp_path / "origin-changelog",
        files={"CHANGELOG.md": _CHANGELOG_BASE.replace("- base", "- main entry\n- base")},
        message="main changelog",
    )
    _declare_readme_union_outside_m(repo, tmp_path, monkeypatch, source="info-attributes")

    probe = work_bridge._probe_main_sync(
        worktree=repo, candidate=candidate, timeout_seconds=5.0
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "clean-behind"
    assert probe.main_head == remote_main
    assert probe.conflict_paths == ()


class _RecordingGitRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(([str(value) for value in argv], dict(kwargs)))
        return subprocess.run(argv, **kwargs)


def test_main_sync_probe_runs_merge_tree_in_isolated_throwaway_git_dir(
    tmp_path: Path,
) -> None:
    repo, origin = _union_probe_repo(tmp_path)
    candidate = _commit_files(repo, {"README.md": "feature readme\n"}, "feature readme")
    remote_main = _advance_origin_with(
        origin,
        tmp_path / "origin-readme",
        files={"README.md": "main readme\n"},
        message="main readme",
    )
    runner = _RecordingGitRunner()

    probe = work_bridge._probe_main_sync(
        worktree=repo, candidate=candidate, runner=runner, timeout_seconds=5.0
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "conflict"
    [(argv, kwargs)] = [call for call in runner.calls if "merge-tree" in call[0]]
    git_dir_arg = argv[1]
    assert git_dir_arg.startswith("--git-dir=")
    isolated = Path(git_dir_arg.removeprefix("--git-dir="))
    assert argv[:2] == ["git", git_dir_arg]
    assert argv[2:6] == [
        "-c",
        f"core.attributesFile={os.devnull}",
        f"--attr-source={remote_main}",
        "merge-tree",
    ]
    env = kwargs["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_ATTR_NOSYSTEM"] == "1"
    assert not any(
        key in env for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_ATTR_SOURCE")
    )
    # 用完即刪；merge 結果 tree 寫在暫存 repo，來源 repo 的 object store 唯讀共用。
    assert not isolated.exists()
    tree_oid = probe.raw["merge-tree"].stdout.split(b"\0", 1)[0].decode("ascii")
    assert _try_git(repo, "cat-file", "-e", tree_oid).returncode != 0


@pytest.mark.parametrize(
    ("objects_outcome", "expected_error_kind", "expected_returncode"),
    [
        (
            SimpleNamespace(returncode=128, stdout=b"", stderr=b"fatal: not a git repository\n"),
            "command-failed",
            128,
        ),
        (
            SimpleNamespace(returncode=0, stdout=b"relative/objects\n", stderr=b""),
            "output-malformed",
            0,
        ),
    ],
)
def test_main_sync_probe_isolation_failure_is_main_sync_unavailable(
    tmp_path: Path,
    objects_outcome: object,
    expected_error_kind: str,
    expected_returncode: int,
) -> None:
    candidate, main_head, merge_base = "a" * 40, "b" * 40, "c" * 40
    runner = _SequencedProbeRunner(
        [
            *_probe_success_prefix(
                candidate=candidate, main_head=main_head, merge_base=merge_base
            ),
            objects_outcome,
        ]
    )

    probe = work_bridge._probe_main_sync(
        worktree=tmp_path, candidate=candidate, runner=runner, timeout_seconds=0.1
    )

    assert isinstance(probe, work_bridge.MainSyncProbeFailure)
    assert probe.stage == "merge-tree-objects"
    assert probe.error_kind == expected_error_kind
    assert probe.returncode == expected_returncode
    assert probe.main_head == main_head
    assert not any("merge-tree" in call for call in runner.calls)
    stop = work_bridge._main_sync_stop_result(state_root=tmp_path / "state", probe=probe)
    assert stop["reason"] == work_bridge.MAIN_SYNC_UNAVAILABLE_REASON


# ---------------------------------------------------------------------------
# 2. retry-build 綁定重試當下的 main
# ---------------------------------------------------------------------------


def _stop_then_advance_main(tmp_path: Path, monkeypatch) -> tuple[object, Path, str, str]:
    harness, origin, stop_main = _main_sync_stop(tmp_path, monkeypatch)
    retry_main = _advance_origin_with(
        origin,
        tmp_path / "origin-after-stop",
        files={"AFTER_STOP.md": "main moved after the stop\n"},
        message="main advances after the main-sync stop",
    )
    assert retry_main != stop_main
    return harness, origin, stop_main, retry_main


def test_retry_build_binds_repair_to_main_fetched_at_retry_time(
    tmp_path: Path, monkeypatch
) -> None:
    harness, _origin, stop_main, retry_main = _stop_then_advance_main(tmp_path, monkeypatch)
    stopped = harness.run
    stop_ref = stopped.needs_human_reason["evidence_refs"][0]
    stop_bytes = Path(stop_ref).read_bytes()
    remote_tracking_before = _try_git(
        harness.repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"
    ).stdout.strip()

    result = _retry_build(harness)

    reset = harness.run
    assert reset.current_phase == "build"
    assert "needs_human" not in reset.facets
    binding = reset.main_sync_repair
    assert binding is not None
    assert binding["main_head"] == retry_main
    assert binding["stop_main_head"] == stop_main
    assert binding["candidate"] == harness.candidate
    assert binding["pin_ref"] == f"refs/cortex/main-sync/{reset.run_id}"
    assert result["result"]["main_sync_repair"] == binding

    # 修復指令綁定「當下的 M」，停機 M 只作稽核脈絡。
    action = str([step for step in reset.steps if step.phase == "build"][-1].action)
    assert f"Manager fetched origin/main as {retry_main}" in action
    assert "Include this exact main commit" in action
    assert binding["evidence_ref"] in action
    assert stop_ref in action
    assert f"recorded origin/main as {stop_main} at the stop" in action

    # 新 evidence 同時記錄兩個 M；停機 evidence 原樣保留。
    payload = _read_evidence(binding["evidence_ref"])
    assert payload["schema"] == work_bridge.MAIN_SYNC_RETRY_EVIDENCE_SCHEMA
    assert payload["run_id"] == reset.run_id
    assert payload["candidate"] == harness.candidate
    assert payload["main_head"] == retry_main
    assert payload["stop_main_head"] == stop_main
    assert payload["stop_evidence_ref"] == stop_ref
    assert payload["pin_ref"] == binding["pin_ref"]
    assert Path(binding["evidence_ref"]).parent.name == "main-sync-retry"
    assert Path(stop_ref).read_bytes() == stop_bytes

    # exact M 釘在來源樹的 pin ref；不順手改 origin/main 的 remote-tracking ref。
    assert _git(harness.repo, "rev-parse", binding["pin_ref"]) == retry_main
    remote_tracking_after = _try_git(
        harness.repo, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"
    ).stdout.strip()
    assert remote_tracking_after == remote_tracking_before

    # 綁定經過 registry 持久化後仍可讀回（新頂層欄位的 round-trip）。
    reloaded = type(harness.registry)(state_path=harness.state_root / "jobs.json")
    assert reloaded.get_workflow_run(reset.run_id).main_sync_repair == binding


def test_retry_build_fails_closed_without_reset_when_current_main_is_unreadable(
    tmp_path: Path, monkeypatch
) -> None:
    harness, origin, _stop_main = _main_sync_stop(tmp_path, monkeypatch)
    before = harness.run.to_dict()
    shutil.rmtree(origin)

    with pytest.raises(RuntimeError, match="retry-build could not fetch current origin/main"):
        _retry_build(harness)

    assert harness.run.to_dict() == before
    assert "needs_human" in harness.run.facets
    assert not (harness.state_root / "evidence" / "main-sync-retry").exists()


def test_build_phase_retry_after_main_sync_repair_keeps_the_exact_main_binding(
    tmp_path: Path, monkeypatch
) -> None:
    harness, _origin, _stop_main, retry_main = _stop_then_advance_main(tmp_path, monkeypatch)
    _retry_build(harness)
    reset = harness.run
    binding = dict(reset.main_sync_repair)

    # 修復卡 terminalization 失敗：exited/0 卻沒有 workflow evidence。
    job = harness.registry.create_job(
        task="wf-main-sync-repair",
        persona="builder",
        kind="build",
        branch="feature/14-work",
        pane="",
        worktree=str(harness.repo),
        executor="codex",
        model_id="gpt",
        independence_domain="openai",
        subject_head=harness.candidate,
        workflow_run_id=reset.run_id,
        workflow_claim_key=reset.claim_key,
        workflow_repo=reset.repo,
        workflow_card="build-card",
        workflow_phase="build",
        workflow_repo_root=str(harness.repo),
        source_revision=reset.source_revision,
    )
    harness.registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    harness.registry._manager_update_workflow_run(
        reset.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "workflow-terminal-unbound",
            "builder exited/0 without workflow evidence",
            source="manager.resume_workflow_run",
            run_id=reset.run_id,
        ),
    )

    _retry_build(harness)

    again = harness.run
    assert again.current_phase == "build"
    assert again.main_sync_repair == binding
    action = str([step for step in again.steps if step.phase == "build"][-1].action)
    assert action.startswith("Recover the exact Candidate after a builder terminalization failure.")
    assert f"Manager fetched origin/main as {retry_main}" in action


# ---------------------------------------------------------------------------
# 3. harvest 以 quarantine ref 驗證修復候選含 retry 當下的 M
# ---------------------------------------------------------------------------


def _repair_job(harness, tmp_path: Path, *, name: str, merge_main: str) -> tuple[dict, str]:
    """以 production provisioning＋bundle 步驟做出一顆 builder 修復候選。"""

    job_id = f"1142-{name}"
    spool_key = f"job-1142-{name}"
    workspace = Path(
        ScriptWorktreeCreator(
            repo=harness.repo, wt_root=tmp_path / f"pool-{name}", base="main"
        ).create("feature/14-work", job_id=job_id, base_sha=harness.candidate)
    )
    _git(workspace, "config", "user.email", "builder@example.invalid")
    _git(workspace, "config", "user.name", "Builder")
    _git(workspace, "fetch", "--quiet", "origin", "main")
    _git(workspace, "merge", "--no-ff", "--no-edit", merge_main)
    candidate = _git(workspace, "rev-parse", "HEAD")
    bundle = job_workspace.prepare_commit_spool(
        spool_key=spool_key, coordinator_root=harness.state_root
    )
    bundled = subprocess.run(
        ["bash", "-c", job_workspace.build_bundle_command(workspace=workspace, bundle=bundle)],
        cwd=str(workspace),
        check=False,
        capture_output=True,
        text=True,
    )
    assert bundled.returncode == 0, bundled.stderr
    job = {
        "job_id": spool_key,
        "branch": "feature/14-work",
        "worktree": str(workspace),
        "log_path": f"/logs/workflow/{spool_key}.jsonl",
    }
    return job, candidate


def test_harvest_accepts_repair_containing_retry_time_main_and_rejects_stop_main_only(
    tmp_path: Path, monkeypatch
) -> None:
    harness, _origin, stop_main, retry_main = _stop_then_advance_main(tmp_path, monkeypatch)
    _retry_build(harness)
    run = harness.run
    quarantine_ref = f"refs/cortex/main-sync-quarantine/{run.run_id}"
    feature_ref = "refs/heads/feature/14-work"
    assert _git(harness.repo, "rev-parse", feature_ref) == harness.candidate

    # 只納入停機時的 M：候選不含 retry 當下的 M ⇒ harvest 拒絕，feature ref 不動。
    stale_job, stale_candidate = _repair_job(
        harness, tmp_path, name="stale", merge_main=stop_main
    )
    with pytest.raises(ValueError, match="does not contain the main-sync retry main"):
        manager._harvest_build_candidate(
            stale_job,
            run=run,
            candidate=stale_candidate,
            coordinator_root=harness.state_root,
        )
    assert _git(harness.repo, "rev-parse", feature_ref) == harness.candidate
    assert _try_git(harness.repo, "rev-parse", "--verify", "--quiet", quarantine_ref).returncode != 0

    # 納入 retry 當下的 M ⇒ harvest 通過，feature ref 推進到修復候選。
    fresh_job, fresh_candidate = _repair_job(
        harness, tmp_path, name="fresh", merge_main=retry_main
    )
    harvested = manager._harvest_build_candidate(
        fresh_job,
        run=run,
        candidate=fresh_candidate,
        coordinator_root=harness.state_root,
    )

    assert harvested == fresh_candidate
    assert _git(harness.repo, "rev-parse", feature_ref) == fresh_candidate
    assert _try_git(
        harness.repo, "merge-base", "--is-ancestor", retry_main, fresh_candidate
    ).returncode == 0
    assert _try_git(harness.repo, "rev-parse", "--verify", "--quiet", quarantine_ref).returncode != 0


def test_harvest_rejects_unchanged_candidate_while_main_sync_repair_is_bound(
    tmp_path: Path, monkeypatch
) -> None:
    """builder 沒有產生新 commit（沒有 bundle）時，舊候選同樣不含 M ⇒ 拒絕。"""

    harness, _origin, _stop_main, _retry_main = _stop_then_advance_main(tmp_path, monkeypatch)
    _retry_build(harness)
    job = {"job_id": "1142-noop", "branch": "feature/14-work", "worktree": str(harness.repo)}

    with pytest.raises(ValueError, match="does not contain the main-sync retry main"):
        manager._harvest_build_candidate(
            job,
            run=harness.run,
            candidate=harness.candidate,
            coordinator_root=harness.state_root,
        )
