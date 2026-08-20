"""#731 (A)：候選 git base 的**重新凍結**入口（`cortex work refreeze-base`）。

實機（0819 深夜）逐字量到的序列：`abandon` → `reset-reclaim-budget` →
`work start` 三次「換代」，一次都沒換掉候選樹的 HEAD——`work start` 對還有
active workflow 的 work item 回 `action=resume / reason=active-workflow`，不走
新 claim ⇒ 基底原封不動；mirror 已經是新 SHA，候選樹仍停在舊 SHA。凍結本身是
對的（hermetic pinning），缺的是**重新凍結的入口**。

本檔釘住的性質：

1. **迴歸主釘**：重新凍結之後，走**正式** dispatch 路徑（真 `ScriptWorktreeCreator`、
   真 git repo）新派工的 worktree `rev-parse HEAD` **逐字等於**新的 `origin/main`。
   沒有這一條，其餘全部只是狀態欄位的自說自話。
2. 入場條件 fail-closed：已有被採信 candidate／in-flight job／已發佈交付物／
   verify 之後的 phase／非 fast-forward 的基底／build branch 帶著新基底以外的
   commit（#613）——一律拒絕，且**不留任何 side effect**。
3. evidence 落檔含舊／新基底與 actor／reason，schema 為
   `cortex-work-candidate-base-refreeze/v1`。
4. CAS（`--expected-run-id`）與 bounded actor／reason 由 `control/contract.py`
   在**所有入口的收斂點**強制。
5. 出口狀態 == 入口狀態（#728 紀律）：phase／facets／candidate 一個位元組都不動。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.control import contract
from paulsha_cortex.coordinator import job_workspace, manager, seams, work_actions
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason

_REPO = "acme/demo"
_WORK_ID = "demo"
_ACTOR = "operator"
_REASON = "#731：main 已前進 13 支 PR，候選樹仍停在 claim 當下的基底"


# ---------------------------------------------------------------------------
# fixtures：一個真的 upstream ＋ 一棵真的來源樹（origin 指向 upstream）
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _identity(root: Path) -> None:
    _git(root, "config", "user.email", "manager@example.invalid")
    _git(root, "config", "user.name", "Cortex Manager")


def _upstream(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    _identity(root)
    (root / "README.md").write_text("upstream\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root


def _advance_upstream(root: Path, *, filename: str) -> str:
    (root / filename).write_text("advanced\n", encoding="utf-8")
    _git(root, "add", filename)
    _git(root, "commit", "-qm", f"advance {filename}")
    return _git(root, "rev-parse", "HEAD").lower()


def _source_tree(upstream: Path, target: Path) -> Path:
    subprocess.run(
        ["git", "clone", "-q", "--origin", "origin", "--", str(upstream), str(target)],
        check=True,
    )
    _identity(target)
    return target


def _snapshot(path: Path) -> Path:
    """Monitor 快照——形狀比照 `tests/test_reclaim_budget_reset_519.py`。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path.parent)], check=True)
    subprocess.run(
        [
            "git", "-C", str(path.parent), "remote", "add", "origin",
            f"git@github.com:{_REPO}.git",
        ],
        check=True,
    )
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": _REPO,
                        "work_id": _WORK_ID,
                        "mapped_issues": [12],
                        "mapped_prs": [],
                        "mapped_openspec": ["demo"],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["issue:12@open", "openspec:demo@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _build_step(card: str = "worktree-isolation") -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        commit_policy="required",
        test_policy="focused",
        gate_result="pending",
    )


def _run(registry: JobRegistry, *, workspace: Path, frozen_readiness=None, **overrides):
    payload = {
        "work_id": _WORK_ID,
        "repo": _REPO,
        "claim_key": "claim:v1:" + "1" * 64,
        "source_revision": "2" * 64,
        "workspace_root": str(workspace),
        "combo": "feature-oneshot",
        "current_phase": "build",
        "steps": (_build_step(),),
        "issue_refs": (f"{_REPO}#12",),
        "openspec_refs": ("demo",),
        "pr_refs": (),
        "attempts": {"build": 1},
        "gate_status": "running",
        "frozen_readiness": frozen_readiness,
    }
    payload.update(overrides)
    return registry._manager_create_workflow_run(**payload)


def _refreeze(
    tmp_path: Path,
    registry: JobRegistry,
    snapshot: Path,
    *,
    expected_run_id: str,
    actor: str = _ACTOR,
    reason: str = _REASON,
    extra: dict | None = None,
):
    args = {
        "action": "refreeze-base",
        "repo": _REPO,
        "work_id": _WORK_ID,
        "actor": actor,
        "reason": reason,
        "expected_run_id": expected_run_id,
    }
    if extra:
        args.update(extra)
    return work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        now=lambda: 1_755_000_000.0,
        snapshot_path=snapshot,
        state_path=tmp_path / "journal.jsonl",
        workflow_registry=registry,
    )["result"]


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self) -> "_RecordingLauncher":
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append({"slice_id": slice_id, "worktree": worktree})
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _dispatch(
    registry: JobRegistry,
    *,
    run,
    workspace: Path,
    pool: Path,
    coordinator_root: Path,
    force_new_card: bool = False,
):
    """跑**正式** dispatch 路徑：真的 `ScriptWorktreeCreator`、真的 git repo。

    `force_new_card` 就是 `manager_daemon` 在 `retry-card` 之後傳的那一個
    （`manager_daemon.run_loop` → `dispatch_workflow_card(force_new_card=…)`）。
    """

    creator = seams.ScriptWorktreeCreator(repo=workspace, wt_root=pool, base="main")
    dispatcher = type(
        "D", (), {"_registry": registry, "_worktree_creator": creator, "_git_runner": None},
    )()
    dispatched = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=IdentityRegistry.from_rows(
            [{
                "executor": "copilot",
                "model_id": "gpt",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }]
        ),
        launcher_factory=lambda _identity: _RecordingLauncher(),
        coordinator_root=coordinator_root,
        force_new_card=force_new_card,
    )
    assert dispatched is not None
    return registry.get_job(str(dispatched["job_id"]))


def _fail_job(registry: JobRegistry, job) -> None:
    """把 job 判成失敗——實機現場（gate 紅）的形狀。"""

    registry.update_headless_result(str(job["job_id"]), status="failed", exit_code=1)


def _retry_card(tmp_path: Path, registry: JobRegistry, snapshot: Path, *, run_id: str, card: str):
    """走**正式** `retry-card` work action，不手改 registry。"""

    return work_actions.execute_work_action(
        args={
            "action": "retry-card",
            "repo": _REPO,
            "work_id": _WORK_ID,
            "expected_run_id": run_id,
            "card": card,
        },
        requested_by="operator",
        now=lambda: 1_755_000_100.0,
        snapshot_path=snapshot,
        state_path=tmp_path / "journal.jsonl",
        workflow_registry=registry,
    )["result"]


def _fixture(tmp_path: Path):
    upstream = _upstream(tmp_path / "upstream")
    workspace = _source_tree(upstream, tmp_path / "source")
    snapshot = _snapshot(tmp_path / "monitor" / "snapshot.json")
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    return upstream, workspace, snapshot, registry


# ---------------------------------------------------------------------------
# 1. 迴歸主釘：重新凍結後，新派工的 worktree HEAD 逐字等於新的 origin/main
# ---------------------------------------------------------------------------


def test_refreeze_makes_the_next_dispatched_worktree_head_equal_the_new_origin_main(
    tmp_path: Path,
) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    stale_base = _git(workspace, "rev-parse", "HEAD").lower()
    run = _run(
        registry,
        workspace=workspace,
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )

    # main 前進；來源樹的**本地** main 沒有人推進——這正是實機現場的形狀。
    advanced = _advance_upstream(upstream, filename="later.txt")
    assert advanced != stale_base
    assert _git(workspace, "rev-parse", "refs/heads/main").lower() == stale_base

    # 對照組：不重新凍結時，派工拿到的就是那個陳舊基底。這一顆之後被判失敗——
    # 正是實機現場（gate 紅、重派、基底原封不動）的形狀。
    stale_job = _dispatch(
        registry,
        run=run,
        workspace=workspace,
        pool=tmp_path / "pool-stale",
        coordinator_root=tmp_path / "coordinator",
    )
    assert _git(Path(str(stale_job["worktree"])), "rev-parse", "HEAD").lower() == stale_base
    _fail_job(registry, stale_job)

    result = _refreeze(tmp_path, registry, snapshot, expected_run_id=run.run_id)

    assert result["action"] == "refreeze-base"
    assert result["reason"] == "candidate-base-refrozen"
    assert result["already_current"] is False
    assert result["previous_base_sha"] == stale_base
    assert result["base_sha"] == advanced
    # 權威來源就是 dispatch 讀的那一格，不是新造的第二份真實來源。
    assert registry.get_workflow_run(run.run_id).frozen_readiness["base_sha"] == advanced
    # 重新凍結本身不推進 run；後續由既有出口（retry-card → 下一拍 dispatch）接手。
    assert result["next_actions"] == ["retry-card"]

    _retry_card(
        tmp_path, registry, snapshot, run_id=run.run_id, card="worktree-isolation"
    )
    refrozen_job = _dispatch(
        registry,
        run=registry.get_workflow_run(run.run_id),
        workspace=workspace,
        pool=tmp_path / "pool-refrozen",
        coordinator_root=tmp_path / "coordinator",
        force_new_card=True,
    )
    fresh = Path(str(refrozen_job["worktree"]))
    assert fresh != Path(str(stale_job["worktree"]))
    assert _git(fresh, "rev-parse", "HEAD").lower() == advanced, (
        "重新凍結後新派工的 worktree HEAD 必須逐字等於新的 origin/main——"
        "這條不成立，(A) 就沒有真的送達候選樹"
    )
    # 送到 builder 手上的 bundle 錨點與 marker 也一併跟著新基底走。
    assert _git(fresh, "rev-parse", job_workspace.BASE_REF).lower() == advanced


def test_refreeze_does_not_relax_hermetic_pinning_between_two_advances(
    tmp_path: Path,
) -> None:
    """凍結仍然是凍結：重新凍結之後 main 又前進，候選樹不得跟著漂。"""

    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(
        registry,
        workspace=workspace,
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )
    first = _advance_upstream(upstream, filename="first.txt")
    _refreeze(tmp_path, registry, snapshot, expected_run_id=run.run_id)

    job = _dispatch(
        registry,
        run=registry.get_workflow_run(run.run_id),
        workspace=workspace,
        pool=tmp_path / "pool",
        coordinator_root=tmp_path / "coordinator",
    )
    assert _git(Path(str(job["worktree"])), "rev-parse", "HEAD").lower() == first

    # main 再前進一次，但沒有人重新凍結——候選樹不得跟著漂。
    second = _advance_upstream(upstream, filename="second.txt")
    assert second != first
    _fail_job(registry, job)
    _retry_card(
        tmp_path, registry, snapshot, run_id=run.run_id, card="worktree-isolation"
    )
    again = _dispatch(
        registry,
        run=registry.get_workflow_run(run.run_id),
        workspace=workspace,
        pool=tmp_path / "pool",
        coordinator_root=tmp_path / "coordinator",
        force_new_card=True,
    )
    assert _git(Path(str(again["worktree"])), "rev-parse", "HEAD").lower() == first


# ---------------------------------------------------------------------------
# 2. evidence／冪等／出口狀態
# ---------------------------------------------------------------------------


def test_refreeze_writes_immutable_evidence_carrying_both_bases_and_operator_inputs(
    tmp_path: Path,
) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    stale_base = _git(workspace, "rev-parse", "HEAD").lower()
    run = _run(registry, workspace=workspace)
    advanced = _advance_upstream(upstream, filename="later.txt")

    result = _refreeze(tmp_path, registry, snapshot, expected_run_id=run.run_id)

    target = Path(result["evidence"]["ref"])
    assert target.parent == (tmp_path / "evidence" / "work-candidate-base-refreeze")
    assert target.name.startswith(f"{run.run_id}-")
    assert not target.is_symlink()
    assert target.stat().st_mode & 0o222 == 0

    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["schema"] == "cortex-work-candidate-base-refreeze/v1"
    assert body["repo"] == _REPO and body["work_id"] == _WORK_ID
    assert body["run_id"] == run.run_id
    assert body["actor"] == _ACTOR and body["reason"] == _REASON
    assert body["previous_base_sha"] == stale_base
    # 未凍結的 run，實際生效的基底是來源樹的本地 `main`——evidence 必須誠實記下
    # 舊基底**是從哪裡讀來的**，而不是含糊帶過。
    assert body["previous_base_source"] == "local-main"
    assert body["base_sha"] == advanced
    assert body["remote_fetch"] == {
        "probe": "claim_readiness.base_sha_probe",
        "remote": "origin",
        "branch": "main",
        "ref": "refs/remotes/origin/main",
        "status": "ok",
        "sha": advanced,
    }
    assert body["previous_phase"] == "build"
    # evidence ref 掛回 run 上，operator 不必自己翻目錄。
    assert result["evidence"]["ref"] in registry.get_workflow_run(run.run_id).evidence_refs


def test_refreeze_leaves_phase_facets_and_candidate_untouched(tmp_path: Path) -> None:
    """#728 紀律：出口狀態就是入口狀態，本動作不製造任何新的 run 狀態。"""

    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(
        registry,
        workspace=workspace,
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )
    _advance_upstream(upstream, filename="later.txt")

    before = registry.get_workflow_run(run.run_id).to_dict()
    _refreeze(tmp_path, registry, snapshot, expected_run_id=run.run_id)
    after = registry.get_workflow_run(run.run_id).to_dict()

    for field in ("current_phase", "facets", "candidate_head", "verified_head", "steps",
                  "gate_status", "status", "planning_authority", "attempts"):
        assert after[field] == before[field], f"refreeze 不得動到 {field}"
    assert after["frozen_readiness"] != before["frozen_readiness"]


def test_refreeze_is_idempotent_once_the_base_is_already_current(tmp_path: Path) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(registry, workspace=workspace)
    _advance_upstream(upstream, filename="later.txt")

    first = _refreeze(tmp_path, registry, snapshot, expected_run_id=run.run_id)
    second = _refreeze(tmp_path, registry, snapshot, expected_run_id=run.run_id)

    assert first["already_current"] is False
    assert second["already_current"] is True
    assert second["reason"] == "candidate-base-already-current"
    assert second["base_sha"] == first["base_sha"]
    written = sorted((tmp_path / "evidence" / "work-candidate-base-refreeze").glob("*.json"))
    assert len(written) == 1, "冪等重入不得寫第二筆 evidence"


# ---------------------------------------------------------------------------
# 3. 入場條件 fail-closed
# ---------------------------------------------------------------------------


def _expect_refusal(tmp_path, registry, snapshot, *, run_id, match: str) -> None:
    with pytest.raises((RuntimeError, ValueError), match=match):
        _refreeze(tmp_path, registry, snapshot, expected_run_id=run_id)
    assert not (tmp_path / "evidence" / "work-candidate-base-refreeze").exists()


def test_refreeze_refuses_a_run_that_already_has_an_accepted_candidate(
    tmp_path: Path,
) -> None:
    """已有被採信 candidate 時，下一張卡的 base 改由 handoff 決定 ⇒ 重新凍結會是
    靜默 no-op。寧可拒絕也不做半套。"""

    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(registry, workspace=workspace)
    _advance_upstream(upstream, filename="later.txt")
    registry._manager_update_workflow_run(run.run_id, candidate_head="a" * 40)

    _expect_refusal(
        tmp_path, registry, snapshot, run_id=run.run_id,
        match="no accepted build candidate",
    )


def test_refreeze_refuses_a_post_build_phase(tmp_path: Path) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(registry, workspace=workspace, current_phase="verify")
    _advance_upstream(upstream, filename="later.txt")

    _expect_refusal(
        tmp_path, registry, snapshot, run_id=run.run_id,
        match="pre-verify workflow phase",
    )


def test_refreeze_refuses_while_a_job_is_still_in_flight(tmp_path: Path) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(registry, workspace=workspace)
    _advance_upstream(upstream, filename="later.txt")
    _dispatch(
        registry,
        run=run,
        workspace=workspace,
        pool=tmp_path / "pool",
        coordinator_root=tmp_path / "coordinator",
    )

    _expect_refusal(
        tmp_path, registry, snapshot, run_id=run.run_id, match="no in-flight job",
    )


def test_refreeze_refuses_a_run_with_a_published_delivery_artifact(tmp_path: Path) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(registry, workspace=workspace, pr_refs=(f"{_REPO}#77",))
    _advance_upstream(upstream, filename="later.txt")

    _expect_refusal(
        tmp_path, registry, snapshot, run_id=run.run_id,
        match="no published delivery artifact",
    )


def test_refreeze_refuses_a_run_id_that_is_not_the_active_one(tmp_path: Path) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    _run(registry, workspace=workspace)
    _advance_upstream(upstream, filename="later.txt")

    _expect_refusal(
        tmp_path, registry, snapshot, run_id="workflow-" + "0" * 20,
        match="expected WorkflowRun CAS mismatch",
    )


def test_refreeze_refuses_a_non_fast_forward_remote(tmp_path: Path) -> None:
    """`origin/main` 若不再是既有基準的後代（例如被改寫），重新凍結等於把 run 的
    基準往回倒——拒絕，不是「以最新的為準」。"""

    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    stale_base = _git(workspace, "rev-parse", "HEAD").lower()
    run = _run(registry, workspace=workspace)
    # upstream 換一條不含 stale_base 的歷史。
    _git(upstream, "checkout", "-q", "--orphan", "rewritten")
    (upstream / "OTHER.md").write_text("rewritten\n", encoding="utf-8")
    _git(upstream, "add", "OTHER.md")
    _git(upstream, "commit", "-qm", "rewritten root")
    _git(upstream, "branch", "-qM", "main")
    assert _git(upstream, "rev-parse", "HEAD").lower() != stale_base

    _expect_refusal(
        tmp_path, registry, snapshot, run_id=run.run_id, match="non-fast-forward base",
    )


def test_refreeze_refuses_when_the_build_branch_carries_commits_outside_the_new_base(
    tmp_path: Path,
) -> None:
    """#613 的形狀：abandon 沒有回收 build branch，branch 上還有前一代的 commit。

    下一拍 provision 必定撞 `existing worktree branch has commits outside
    requested base`，因此在**改任何狀態之前**就拒絕；判準與
    `ScriptWorktreeCreator.create()` 的守衛是同一個 git 述詞。
    """

    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(registry, workspace=workspace)
    advanced = _advance_upstream(upstream, filename="later.txt")

    branch = manager.workflow_build_branch(registry.get_workflow_run(run.run_id))
    assert branch == f"feature/12-{_WORK_ID}"
    _git(workspace, "checkout", "-q", "-b", branch)
    (workspace / "gen1.txt").write_text("previous generation\n", encoding="utf-8")
    _git(workspace, "add", "gen1.txt")
    _git(workspace, "commit", "-qm", "gen1 leftover")
    _git(workspace, "checkout", "-q", "main")

    with pytest.raises(RuntimeError, match="commits outside the new base"):
        _refreeze(tmp_path, registry, snapshot, expected_run_id=run.run_id)
    assert not (tmp_path / "evidence" / "work-candidate-base-refreeze").exists()
    assert registry.get_workflow_run(run.run_id).frozen_readiness is None

    # 與生產守衛逐字同源：同一個狀態下真的去 provision，撞的就是 #613 那條訊息。
    with pytest.raises(ValueError, match="existing worktree branch has commits outside"):
        seams.ScriptWorktreeCreator(
            repo=workspace, wt_root=tmp_path / "pool", base="main"
        ).create(branch, job_id="probe-1", base_sha=advanced)


def test_refreeze_rejects_unknown_caller_supplied_fields(tmp_path: Path) -> None:
    upstream, workspace, snapshot, registry = _fixture(tmp_path)
    run = _run(registry, workspace=workspace)
    _advance_upstream(upstream, filename="later.txt")

    with pytest.raises(ValueError, match="rejects caller evidence/input"):
        _refreeze(
            tmp_path, registry, snapshot, expected_run_id=run.run_id,
            extra={"base_sha": "b" * 40},
        )


# ---------------------------------------------------------------------------
# 4. contract：CAS ＋ bounded actor／reason 在所有入口的收斂點強制
# ---------------------------------------------------------------------------


def _request(**overrides) -> dict:
    args = {
        "action": "refreeze-base",
        "repo": _REPO,
        "work_id": _WORK_ID,
        "actor": _ACTOR,
        "reason": _REASON,
        "expected_run_id": "workflow-" + "a" * 20,
    }
    args.update(overrides)
    return contract.build_request(req_type="work-action", args=args, requested_by="operator")


def test_contract_accepts_a_well_formed_refreeze_request() -> None:
    validated = contract.validate_request(_request())
    assert validated["args"]["action"] == "refreeze-base"


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"expected_run_id": None}, "exact expected_run_id"),
        ({"expected_run_id": "workflow-nope"}, "exact expected_run_id"),
        ({"actor": ""}, "bounded actor"),
        ({"actor": " padded"}, "bounded actor"),
        ({"actor": "a" * 129}, "bounded actor"),
        ({"reason": ""}, "bounded reason"),
        ({"reason": "line\nbreak"}, "bounded reason"),
        ({"reason": "r" * 501}, "bounded reason"),
    ],
)
def test_contract_rejects_malformed_refreeze_operator_inputs(overrides, match) -> None:
    with pytest.raises(ValueError, match=match):
        contract.validate_request(_request(**overrides))


def test_refreeze_is_reachable_from_every_operator_entrypoint() -> None:
    """R-16：新增的 CLI 動作必須同步出現在 help／choices／contract 上。"""

    from paulsha_cortex import cli as umbrella_cli
    from paulsha_cortex.coordinator.cli import _build_parser as build_coordinator_parser
    from paulsha_cortex.porcelain import recover

    assert "refreeze-base" in contract.WORK_ACTIONS
    assert "refreeze-base" in umbrella_cli._WORK_HELP
    coordinator_help = build_coordinator_parser().format_help()
    assert "work" in coordinator_help
    parsed = build_coordinator_parser().parse_args(
        ["work", "refreeze-base", _WORK_ID, "--repo", _REPO, "--actor", _ACTOR,
         "--reason", _REASON, "--expected-run-id", "workflow-" + "a" * 20]
    )
    assert parsed.action == "refreeze-base"
    recover_parsed = recover._build_parser().parse_args(
        ["work", _WORK_ID, "refreeze-base", "--repo", _REPO, "--actor", _ACTOR,
         "--reason", _REASON, "--expected-run-id", "workflow-" + "a" * 20]
    )
    assert recover_parsed.action == "refreeze-base"


def test_build_branch_derivation_has_a_single_source() -> None:
    """`workflow_build_branch()` 是 dispatch 與 refreeze 共用的**同一個**函式物件。"""

    import inspect

    source = inspect.getsource(manager._dispatch_workflow_card)
    assert "workflow_build_branch(run)" in source
    assert 'builder_branch = f"feature/{builder_work_id}"' not in source
