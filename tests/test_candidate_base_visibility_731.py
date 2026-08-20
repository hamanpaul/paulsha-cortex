"""#731 (C)：候選 git base 攤在 `cortex status`／`cortex work show` 上。

缺陷逐字（0819 深夜現場）：

```
候選 worktree  git rev-parse HEAD        → 59a7a9b（0818）
mirror         refs/remotes/origin/main  → 7eb707b（落後 13 支 PR）
cortex status / work show 的任何欄位     → 看不到上面任何一個
```

run 上唯一顯眼的「版本」欄位 `source_revision` 是 64-hex 的 authority digest
（work item 來源材料的 sha256），與 git base 無關——它把診斷帶偏了兩次。

本檔釘住五件事：
1. 候選基底看得到，且**權威來源不重造**（凍結集 → 第一張 build 卡 dispatch_head）。
2. 落後程度算得出來（0 個 commit／超過門檻兩種都釘）。
3. 過門檻時給的是**具名 reason**（機器可讀碼），不是自由文字。
4. 讀不到 mirror／算不出距離時 fail-soft 且說得出原因（`<unresolved:…>`）。
5. **status 路徑不得引入任何 git 寫入或網路動作**——尤其不得 fetch。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from paulsha_cortex import cli as work_cli
from paulsha_cortex.coordinator import candidate_base, manager, manager_daemon
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

_REPO = "hamanpaul/paulsha-cortex"

# 0819 現場的兩個真實 SHA 形狀（40-hex）。用逐字常數而非隨機字串，讓失敗訊息
# 一眼對得回票上的量測。
_FROZEN_BASE = "59a7a9b472fba8fa1acbf6cf6411107f340777c8"
_MIRROR_MAIN = "7eb707bdae54c46f8f0fa135b93b77e117289e86"

#: `source_revision` 的形狀：64-hex authority digest。刻意與上面兩個並列，
#: 讓「兩者不是同一件事」在測試資料上就看得出來。
_SOURCE_REVISION = "22b88b01e9b25245014b" + "0" * 44


# ---------------------------------------------------------------------------
# 假 git runner：只回答本模組會問的兩種唯讀子命令，其餘一律讓測試炸掉。
# ---------------------------------------------------------------------------


class _RecordingGitRunner:
    """記錄每一次 git argv，讓「status 路徑不得寫入／連網」可被逐字斷言。"""

    #: 任何一個出現在 argv 裡就代表呈現面違反唯讀約束。`fetch`／`pull`／`remote`
    #: 是連網；其餘是寫入。
    FORBIDDEN = (
        "fetch", "pull", "push", "clone", "remote", "commit", "checkout",
        "worktree", "reset", "merge", "rebase", "gc", "prune", "add",
    )

    def __init__(self, *, main_sha: str | None, distances: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._main_sha = main_sha
        self._distances = distances or {}

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        if "rev-parse" in args:
            if self._main_sha is None:
                return subprocess.CompletedProcess(args, 128, "", "unknown revision")
            return subprocess.CompletedProcess(args, 0, self._main_sha + "\n", "")
        if "rev-list" in args:
            spec = args[-1]
            base = spec.split("..", 1)[0]
            if base not in self._distances:
                return subprocess.CompletedProcess(args, 128, "", "bad revision")
            return subprocess.CompletedProcess(args, 0, f"{self._distances[base]}\n", "")
        raise AssertionError(f"unexpected git call from a read-only path: {args}")

    def assert_read_only(self) -> None:
        for call in self.calls:
            for token in call:
                assert token not in self.FORBIDDEN, f"read-only path issued: {call}"


def _probe(
    *, main_sha: str | None = _MIRROR_MAIN, distances: dict[str, int] | None = None,
    mirror_root: str | None = "/mirror",
) -> tuple[candidate_base.MirrorDistanceProbe, _RecordingGitRunner]:
    runner = _RecordingGitRunner(main_sha=main_sha, distances=distances)
    return (
        candidate_base.MirrorDistanceProbe(mirror_root=mirror_root, git_runner=runner),
        runner,
    )


def _frozen_readiness(*, base_sha: str) -> dict[str, Any]:
    return {
        "schema": "pre-claim-readiness-frozen-set/v1",
        "repo": _REPO,
        "work_id": "candidate-base-731",
        "base_sha": base_sha,
        "planning_authority_hashes": ["a" * 64],
        "monitor_snapshot_revision": "snap-1",
        "issue_ref": f"{_REPO}#731",
        "executor_identity": "copilot:gpt",
        "frozen_at_epoch": 1_000.0,
        "live_probe_ttl_cached": False,
    }


# ---------------------------------------------------------------------------
# 1. 權威來源：凍結集優先，退回第一張 build 卡的 dispatch_head，都沒有就說出來
# ---------------------------------------------------------------------------


def test_frozen_readiness_base_sha_is_the_authoritative_candidate_base() -> None:
    probe, _ = _probe(distances={_FROZEN_BASE: 13})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE),
        # 凍結集在場時，job 的 dispatch_head 不得贏過它。
        build_dispatch_heads=("b" * 40,),
        probe=probe,
    )
    assert resolved.sha == _FROZEN_BASE
    assert resolved.sha_source == candidate_base.CANDIDATE_BASE_SOURCE_FROZEN_READINESS


def test_first_build_job_dispatch_head_is_used_when_run_has_no_frozen_set() -> None:
    """實機 0820 逐字：29 個 run 的 `frozen_readiness` **全為 null**，
    唯一記著候選基底的是 job 的 `dispatch_head`。"""

    probe, _ = _probe(distances={_FROZEN_BASE: 13})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=None,
        build_dispatch_heads=(_FROZEN_BASE, "c" * 40),
        probe=probe,
    )
    assert resolved.sha == _FROZEN_BASE
    assert resolved.sha_source == candidate_base.CANDIDATE_BASE_SOURCE_BUILD_JOB


def test_build_dispatch_heads_are_ordered_by_creation_and_scoped_to_the_run() -> None:
    heads = candidate_base.build_dispatch_heads_from_jobs(
        [
            # 別條 run 的卡：不得混進來。
            {
                "workflow_run_id": "other",
                "workflow_phase": "build",
                "dispatch_head": "d" * 40,
                "created_at": "2026-08-01T00:00:00+00:00",
            },
            # 同 run 但非 build 階段：verify/review 卡綁的是 candidate 而非 base。
            {
                "workflow_run_id": "run-1",
                "workflow_phase": "verify",
                "dispatch_head": "e" * 40,
                "created_at": "2026-08-01T00:00:00+00:00",
            },
            {
                "workflow_run_id": "run-1",
                "workflow_phase": "build",
                "dispatch_head": "f" * 40,
                "created_at": "2026-08-19T02:00:00+00:00",
            },
            {
                "workflow_run_id": "run-1",
                "workflow_phase": "build",
                "dispatch_head": _FROZEN_BASE,
                "created_at": "2026-08-19T01:00:00+00:00",
            },
        ],
        run_id="run-1",
    )
    assert heads == [_FROZEN_BASE, "f" * 40]


def test_run_without_any_base_reports_a_named_absent_reason_not_silence() -> None:
    probe, runner = _probe()
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=None, build_dispatch_heads=(), probe=probe
    )
    assert resolved.sha is None
    assert resolved.sha_source is None
    assert resolved.behind_origin_main is None
    assert resolved.reason == candidate_base.CANDIDATE_BASE_ABSENT_REASON
    runner.assert_read_only()


# ---------------------------------------------------------------------------
# 2./3. 距離與具名診斷
# ---------------------------------------------------------------------------


def test_zero_commits_behind_carries_no_reason() -> None:
    probe, _ = _probe(distances={_FROZEN_BASE: 0})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE), probe=probe
    )
    assert resolved.behind_origin_main == 0
    assert resolved.reason is None
    assert resolved.stale is False


def test_base_equal_to_mirror_main_short_circuits_without_a_rev_list_call() -> None:
    probe, runner = _probe(main_sha=_MIRROR_MAIN, distances={})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_MIRROR_MAIN), probe=probe
    )
    assert resolved.behind_origin_main == 0
    assert resolved.reason is None
    assert all("rev-list" not in call for call in runner.calls)


def test_below_threshold_is_not_flagged() -> None:
    threshold = candidate_base.CANDIDATE_BASE_STALE_THRESHOLD_COMMITS
    probe, _ = _probe(distances={_FROZEN_BASE: threshold - 1})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE), probe=probe
    )
    assert resolved.behind_origin_main == threshold - 1
    assert resolved.reason is None


def test_at_or_beyond_threshold_yields_the_named_stale_reason() -> None:
    """0819 現場的量：落後 13 支 PR。reason 必須是**機器可讀碼**，不是一句話。"""

    probe, _ = _probe(distances={_FROZEN_BASE: 13})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE), probe=probe
    )
    assert resolved.behind_origin_main == 13
    assert resolved.reason == "candidate-git-base-stale"
    assert resolved.stale is True
    # 具名：不得只是把敘述塞進自由文字欄位。
    assert " " not in resolved.reason


def test_threshold_lives_in_one_place_and_is_env_overridable() -> None:
    assert candidate_base.stale_threshold_commits({}) == (
        candidate_base.CANDIDATE_BASE_STALE_THRESHOLD_COMMITS
    )
    env = {candidate_base.CANDIDATE_BASE_STALE_THRESHOLD_ENV: "3"}
    assert candidate_base.stale_threshold_commits(env) == 3
    # 垃圾值不得讓呈現面爆掉，一律退回常數。
    for bad in ("", "abc", "0", "-4"):
        assert candidate_base.stale_threshold_commits(
            {candidate_base.CANDIDATE_BASE_STALE_THRESHOLD_ENV: bad}
        ) == candidate_base.CANDIDATE_BASE_STALE_THRESHOLD_COMMITS

    probe, _ = _probe(distances={_FROZEN_BASE: 3})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE),
        probe=probe,
        threshold_commits=3,
    )
    assert resolved.reason == candidate_base.CANDIDATE_BASE_STALE_REASON


# ---------------------------------------------------------------------------
# 4. fail-soft：讀不到也要說得出口
# ---------------------------------------------------------------------------


def test_mirror_root_unset_fails_soft_with_an_unresolved_marker() -> None:
    probe, runner = _probe(mirror_root=None)
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE), probe=probe
    )
    assert resolved.sha == _FROZEN_BASE  # 基底本身照常曝光
    assert resolved.behind_origin_main == "<unresolved:MirrorRootUnset>"
    assert resolved.reason == candidate_base.CANDIDATE_BASE_DISTANCE_UNRESOLVED_REASON
    assert runner.calls == []  # 沒有 mirror 就一次 git 都不該發


def test_unreadable_mirror_main_fails_soft_with_a_distinct_marker() -> None:
    probe, runner = _probe(main_sha=None)
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE), probe=probe
    )
    assert resolved.sha == _FROZEN_BASE
    assert resolved.mirror_origin_main is None
    assert resolved.behind_origin_main == "<unresolved:MirrorMainUnreadable>"
    assert resolved.reason == candidate_base.CANDIDATE_BASE_DISTANCE_UNRESOLVED_REASON
    runner.assert_read_only()


def test_base_missing_from_the_mirror_object_store_is_not_reported_as_zero() -> None:
    """「量不到」與「距離是 0」是兩件事，塌縮成 0 會讓過舊的基底看起來很新鮮。"""

    probe, _ = _probe(distances={})  # rev-list 對這個 base 一律非零退出
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE), probe=probe
    )
    assert resolved.behind_origin_main == "<unresolved:BaseNotInMirror>"
    assert resolved.reason == candidate_base.CANDIDATE_BASE_DISTANCE_UNRESOLVED_REASON


def test_distances_are_measured_once_per_base_within_one_snapshot() -> None:
    probe, runner = _probe(distances={_FROZEN_BASE: 13})
    for _ in range(3):
        candidate_base.resolve_candidate_git_base(
            frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE), probe=probe
        )
    assert sum("rev-list" in call for call in runner.calls) == 1
    assert sum("rev-parse" in call for call in runner.calls) == 1


# ---------------------------------------------------------------------------
# 真 git：距離必須逐字等於 `git rev-list --count`
# ---------------------------------------------------------------------------


def _init_mirror(root: Path, *, commits: int) -> tuple[str, str]:
    """造一棵有 remote-tracking ref 的 mirror，回傳 (base_sha, origin/main sha)。"""

    upstream = root / "upstream"
    upstream.mkdir(parents=True)
    run = lambda *args: subprocess.run(list(args), check=True, capture_output=True, text=True)
    run("git", "-C", str(upstream), "init", "-q", "-b", "main")
    run("git", "-C", str(upstream), "config", "user.email", "test@example.com")
    run("git", "-C", str(upstream), "config", "user.name", "Test User")
    (upstream / "README.md").write_text("base\n", encoding="utf-8")
    run("git", "-C", str(upstream), "add", "README.md")
    run("git", "-C", str(upstream), "commit", "-qm", "base")

    mirror = root / "mirror"
    run("git", "clone", "-q", str(upstream), str(mirror))
    base = run("git", "-C", str(mirror), "rev-parse", "refs/remotes/origin/main").stdout.strip()

    for index in range(commits):
        (upstream / f"f{index}.txt").write_text(f"{index}\n", encoding="utf-8")
        run("git", "-C", str(upstream), "add", f"f{index}.txt")
        run("git", "-C", str(upstream), "commit", "-qm", f"c{index}")
    # mirror 這一側只有這裡 fetch 一次（模擬 claim 的職責），之後的量測全唯讀。
    run("git", "-C", str(mirror), "fetch", "-q", "--no-tags", "origin", "main")
    head = run("git", "-C", str(mirror), "rev-parse", "refs/remotes/origin/main").stdout.strip()
    return base, head


@pytest.mark.parametrize("commits", [0, 13])
def test_real_git_distance_matches_rev_list_count(tmp_path: Path, commits: int) -> None:
    base, head = _init_mirror(tmp_path, commits=commits)
    probe = candidate_base.MirrorDistanceProbe(mirror_root=tmp_path / "mirror")
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness={"base_sha": base}, probe=probe, threshold_commits=10
    )
    assert resolved.sha == base
    assert resolved.mirror_origin_main == head
    assert resolved.behind_origin_main == commits
    assert resolved.reason == (
        candidate_base.CANDIDATE_BASE_STALE_REASON if commits >= 10 else None
    )


# ---------------------------------------------------------------------------
# 5. 曝光面：`cortex status` 的 attention／in_flight，與 `cortex work show`
# ---------------------------------------------------------------------------


def _build_run(registry: JobRegistry, workspace_root: Path, *, frozen_readiness=None):
    step = WorkflowStep(
        phase="build",
        persona="builder",
        card="tdd-red",
        executor="copilot",
        model="gpt",
        domain="openai",
        inputs=(),
        outputs=(),
        commit_policy="required",
        test_policy="red-required",
        gate_result="pending",
    )
    return registry._manager_create_workflow_run(
        work_id="candidate-base-731",
        repo=_REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision=_SOURCE_REVISION,
        workspace_root=str(workspace_root),
        combo="feature-oneshot",
        current_phase="build",
        steps=(step,),
        issue_refs=(f"{_REPO}#731",),
        openspec_refs=("candidate-base-731",),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
        frozen_readiness=frozen_readiness,
    )


def _needs_human(registry: JobRegistry, run):
    return registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason={
            "reason": "gate-contradiction",
            "detail": "terminal 自稱 passed 但 gate 'pytest' 實際為 failed",
            "source": "tests.test_candidate_base_visibility_731",
        },
    )


def test_attention_entry_exposes_the_candidate_git_base(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path / "jobs.json")
    run = _build_run(
        registry, tmp_path / "ws", frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE)
    )
    run = _needs_human(registry, run)
    probe, runner = _probe(distances={_FROZEN_BASE: 13})

    entry = manager.workflow_status_entry(registry, run, candidate_base_probe=probe)

    git_base = entry["candidate_git_base"]
    assert git_base["sha"] == _FROZEN_BASE
    assert git_base["behind_origin_main"] == 13
    assert git_base["reason"] == candidate_base.CANDIDATE_BASE_STALE_REASON
    assert git_base["mirror_origin_main"] == _MIRROR_MAIN
    assert git_base["fetched"] is False
    # 與 `source_revision` 是兩件事——長度就分得出來，這裡逐字釘住。
    assert len(git_base["sha"]) == 40
    assert len(run.source_revision) == 64
    assert git_base["sha"] != run.source_revision
    runner.assert_read_only()


def test_in_flight_entry_exposes_the_card_dispatch_base(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path / "jobs.json")
    registry.create_job(
        task="wf-a546ff54f2-worktree-isolation",
        persona="builder",
        branch="feature/candidate-base-731",
        pane="",
        worktree=str(tmp_path / "wt"),
        dispatch_head=_FROZEN_BASE,
        workflow_phase="build",
    )
    probe, runner = _probe(distances={_FROZEN_BASE: 13})

    rows = manager_daemon._in_flight_status(registry, candidate_base_probe=probe)

    assert len(rows) == 1
    git_base = rows[0]["candidate_git_base"]
    assert git_base["sha"] == _FROZEN_BASE
    assert git_base["behind_origin_main"] == 13
    assert git_base["reason"] == candidate_base.CANDIDATE_BASE_STALE_REASON
    runner.assert_read_only()


def test_status_path_never_fetches_or_writes(tmp_path: Path) -> None:
    """驗收條件：**不得**在 status 路徑引入任何 git 寫入或網路動作。

    fetch 是 claim 的職責（`claim_readiness.base_sha_probe` 逐字「Fetch remote
    main once and freeze it」）；呈現面跟著 fetch 會讓「看一眼狀態」變成會改變
    狀態的動作，而且 0819 現場證實 fetch 也救不了基底（run 走的是 resume）。
    """

    registry = JobRegistry(tmp_path / "jobs.json")
    run = _build_run(
        registry, tmp_path / "ws", frozen_readiness=_frozen_readiness(base_sha=_FROZEN_BASE)
    )
    run = _needs_human(registry, run)
    registry.create_job(
        task="wf-a546ff54f2-worktree-isolation",
        persona="builder",
        branch="feature/candidate-base-731",
        pane="",
        worktree=str(tmp_path / "wt"),
        dispatch_head=_FROZEN_BASE,
        workflow_phase="build",
        workflow_run_id=run.run_id,
    )
    probe, runner = _probe(distances={_FROZEN_BASE: 13})

    manager.workflow_status_entry(registry, run, candidate_base_probe=probe)
    manager_daemon._in_flight_status(registry, candidate_base_probe=probe)

    assert runner.calls, "測試本身要真的走到 git 讀取路徑，否則這條斷言是空的"
    runner.assert_read_only()
    for call in runner.calls:
        assert call[0] == "-C" and call[2] in {"rev-parse", "rev-list"}, call


def test_status_entry_survives_a_broken_registry(tmp_path: Path) -> None:
    """呈現面不得因曝光計算失敗而讓整份 status 死掉。"""

    registry = JobRegistry(tmp_path / "jobs.json")
    run = _needs_human(registry, _build_run(registry, tmp_path / "ws"))

    class _Broken:
        def list_jobs(self):
            raise RuntimeError("registry unavailable")

    entry = manager.workflow_status_entry(_Broken(), run)
    assert entry["candidate_git_base"]["sha"] is None
    assert entry["candidate_git_base"]["reason"] == (
        candidate_base.CANDIDATE_BASE_ABSENT_REASON
    )


# ---------------------------------------------------------------------------
# `cortex work show` 的文字輸出
# ---------------------------------------------------------------------------


def test_work_show_prints_the_git_base_and_disambiguates_source_revision() -> None:
    lines = work_cli._format_candidate_git_base(
        {
            "run_id": "workflow-85114100c37cc99e89b1",
            "sha": _FROZEN_BASE,
            "sha_source": candidate_base.CANDIDATE_BASE_SOURCE_BUILD_JOB,
            "behind_origin_main": 13,
            "mirror_origin_main": _MIRROR_MAIN,
            "threshold_commits": 10,
            "reason": candidate_base.CANDIDATE_BASE_STALE_REASON,
            "measured_against": f"mirror:{candidate_base.MIRROR_MAIN_REF}",
            "fetched": False,
        }
    )
    text = "\n".join(lines)
    assert _FROZEN_BASE in text
    assert "behind" in text and "13" in text
    assert candidate_base.CANDIDATE_BASE_STALE_REASON in text
    assert "fetched=false" in text
    # 誤導本身是缺陷的一部分：印 git base 就一併點明 `source_revision` 是什麼。
    assert "source_revision" in text and "authority digest" in text


def test_work_show_stays_silent_for_a_monitor_without_the_field() -> None:
    assert work_cli._format_candidate_git_base(None) == []
    assert work_cli._format_candidate_git_base({}) == []


def test_work_show_end_to_end_text_mode(capsys) -> None:
    class _FakeClient:
        def request(self, request):
            assert request["kind"] == "get_work_item"
            return {
                "ok": True,
                "data": {
                    "item": {
                        "work_id": "candidate-base-731",
                        "state": "on-going",
                        "title": "候選基底可見度",
                        "repo": _REPO,
                        "phase": "build",
                        "source_revision": _SOURCE_REVISION,
                    },
                    "candidate_git_base": {
                        "run_id": "workflow-85114100c37cc99e89b1",
                        "sha": _FROZEN_BASE,
                        "sha_source": candidate_base.CANDIDATE_BASE_SOURCE_BUILD_JOB,
                        "behind_origin_main": 13,
                        "mirror_origin_main": _MIRROR_MAIN,
                        "threshold_commits": 10,
                        "reason": candidate_base.CANDIDATE_BASE_STALE_REASON,
                        "measured_against": f"mirror:{candidate_base.MIRROR_MAIN_REF}",
                        "fetched": False,
                    },
                },
            }

    assert work_cli._work_read_main(
        ["work", "show", "candidate-base-731"], work_client=_FakeClient()
    ) == 0
    out = capsys.readouterr().out
    assert f"candidate_git_base: {_FROZEN_BASE}" in out
    assert "run_id: workflow-85114100c37cc99e89b1" in out
    assert candidate_base.CANDIDATE_BASE_STALE_REASON in out


def test_status_text_mode_prints_the_git_base_for_attention_and_in_flight(capsys) -> None:
    """`cortex status` 文字模式：不必展開整包 JSON 就看得到基底與落後程度。"""

    from paulsha_cortex.porcelain import inspect as porcelain_inspect

    payload = {
        "sha": _FROZEN_BASE,
        "sha_source": candidate_base.CANDIDATE_BASE_SOURCE_BUILD_JOB,
        "behind_origin_main": 13,
        "mirror_origin_main": _MIRROR_MAIN,
        "threshold_commits": 10,
        "reason": candidate_base.CANDIDATE_BASE_STALE_REASON,
        "measured_against": f"mirror:{candidate_base.MIRROR_MAIN_REF}",
        "fetched": False,
    }
    porcelain_inspect._print_status(
        {
            "updated_at": "2026-08-20T00:00:00+00:00",
            "degraded": False,
            "attention": [
                {
                    "kind": "workflow_run",
                    "run_id": "workflow-85114100c37cc99e89b1",
                    "slice_state": "needs_human",
                    "candidate_git_base": payload,
                }
            ],
            "in_flight": [
                {
                    "job_id": "wf-a546ff54f2-worktree-isolation-11",
                    "state": "exited",
                    "candidate_git_base": payload,
                }
            ],
        }
    )
    out = capsys.readouterr().out
    assert f"candidate_git_base[workflow-85114100c37cc99e89b1]: {_FROZEN_BASE}" in out
    assert f"candidate_git_base[wf-a546ff54f2-worktree-isolation-11]: {_FROZEN_BASE}" in out
    assert "behind mirror:refs/remotes/origin/main=13" in out
    assert f"reason: {candidate_base.CANDIDATE_BASE_STALE_REASON} (threshold=10)" in out
    assert "fetched=false" in out


# ---------------------------------------------------------------------------
# (A) ⟷ (C) 的接合：候選基底的權威來源必須是**同一支函式**
# ---------------------------------------------------------------------------


def test_frozen_base_read_is_a_single_shared_export_point() -> None:
    """`work_actions`（(A) 寫入端）與本曝光面（(C) 讀取端）讀的是同一支函式。

    本 repo 已經被「同一個事實兩份表述」咬過很多次（#727 的第二份 `-o` 落點、
    #728 的兩份 `next_actions` 導出）。候選基底的凍結值只能有一個讀取點。
    """

    from paulsha_cortex.coordinator import work_actions

    assert work_actions.candidate_base.frozen_base_sha is (
        candidate_base.frozen_base_sha
    )
    # 正規化／驗證的行為逐字一致（大小寫、空白、非 40-hex、非 Mapping）。
    assert candidate_base.frozen_base_sha({"base_sha": _FROZEN_BASE.upper()}) == _FROZEN_BASE
    assert candidate_base.frozen_base_sha({"base_sha": f"  {_FROZEN_BASE}  "}) == _FROZEN_BASE
    assert candidate_base.frozen_base_sha({"base_sha": "not-a-sha"}) is None
    assert candidate_base.frozen_base_sha({}) is None
    assert candidate_base.frozen_base_sha(None) is None


def test_after_refreeze_the_exposure_reads_the_new_frozen_base() -> None:
    """(A) 重新凍結成功之後，(C) 自動改讀凍結集、`behind` 歸零。

    (A) 寫進 `frozen_readiness` 的 schema 是 `cortex-candidate-base-freeze/v1`
    （run 先前沒有凍結集時的形狀），刻意**不是** readiness 那個六道關卡的 schema。
    (C) 只讀 `base_sha` 一格，因此兩種 schema 都吃得到——這裡逐字釘住。
    """

    from paulsha_cortex.coordinator.work_actions import CANDIDATE_BASE_FREEZE_SCHEMA

    refrozen = {
        "schema": CANDIDATE_BASE_FREEZE_SCHEMA,
        "base_sha": _MIRROR_MAIN,
        "frozen_at_epoch": 1_755_000_000.0,
    }
    probe, _ = _probe(main_sha=_MIRROR_MAIN, distances={})
    resolved = candidate_base.resolve_candidate_git_base(
        frozen_readiness=refrozen,
        # 重新凍結**不會**回頭改寫已派工卡片的 dispatch_head；曝光面必須以凍結集
        # 為準，否則 operator 會在救回來之後仍看到舊基底。
        build_dispatch_heads=(_FROZEN_BASE,),
        probe=probe,
    )
    assert resolved.sha == _MIRROR_MAIN
    assert resolved.sha_source == candidate_base.CANDIDATE_BASE_SOURCE_FROZEN_READINESS
    assert resolved.behind_origin_main == 0
    assert resolved.reason is None


# ---------------------------------------------------------------------------
# Monitor 的 workflow projection：資料走既有 observations 通道，不得 degraded
# ---------------------------------------------------------------------------


def test_monitor_projection_carries_the_git_base_without_degrading(tmp_path: Path) -> None:
    """#261／#527 已付過的學費：新增 WorkflowRun 欄位會讓整份 projection degraded。

    因此候選基底同樣走 observations 通道（`candidate_git_bases`），不新增 row 欄位。
    """

    from paulsha_cortex.monitor.providers import WorkflowRegistryProvider

    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _build_run(registry, tmp_path / "ws")
    registry.create_job(
        task="wf-a546ff54f2-worktree-isolation",
        persona="builder",
        branch="feature/candidate-base-731",
        pane="",
        worktree=str(tmp_path / "wt"),
        dispatch_head=_FROZEN_BASE,
        workflow_phase="build",
        workflow_run_id=run.run_id,
    )
    probe, runner = _probe(distances={_FROZEN_BASE: 13})

    result = WorkflowRegistryProvider(
        _REPO, state_path=state, candidate_base_probe=probe
    ).scan()

    assert result.status == "ok"
    assert result.diagnostics == ()
    projected = result.observations["candidate_git_bases"]["candidate-base-731"]
    assert projected["run_id"] == run.run_id
    assert projected["sha"] == _FROZEN_BASE
    assert projected["sha_source"] == candidate_base.CANDIDATE_BASE_SOURCE_BUILD_JOB
    assert projected["behind_origin_main"] == 13
    assert projected["reason"] == candidate_base.CANDIDATE_BASE_STALE_REASON
    assert projected["fetched"] is False
    # Monitor 是讀模型：projection 路徑同樣不得 fetch。
    runner.assert_read_only()
