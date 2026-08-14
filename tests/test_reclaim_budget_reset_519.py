"""#519：semantic-reclaim 世代熔斷的明示重置動作（建議 4）。

`_claim_action` 的 `semantic-reclaim-budget-exhausted` 熔斷（#218 AC2）對
`(repo, work_id)` 的全部 superseded 歷史無條件累加，沒有任何重置路徑——根因
修好之後 work item 仍永久鎖死（實測 `fix-brainstorm-revalidation-diagnostics`
在 4 分鐘內因三個 cortex 自身缺陷累積滿額度）。本檔驗證新增的
`reset-reclaim-budget` operator 動作：

- 重置後同一 work item 可再次 claim；重置前行為完全不變。
- 重置以 append-only 水位（cleared run_ids）表達，既有 run 歷史一列不改。
- 重置後新產生的 superseded 世代重新計數，熔斷會再次上膛。
- `--reason`／`--actor` 為必填且有界；白名單外參數 fail-closed。
- 落 `cortex-work-reclaim-reset/v1` evidence（canonical json hash 命名、
  唯讀原子寫入、內容衝突 raise）。
- 熔斷回傳訊息本身要指出下一步。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.control import contract
from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            [
                "git", "-C", str(root), "remote", "add", "origin",
                f"git@github.com:{repo}.git",
            ],
            check=True,
        )
    return root


def _snapshot(path: Path) -> Path:
    _init_repo(path.parent)
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
                        "repo": "acme/demo",
                        "work_id": "demo",
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


_CLAIM_STEP = WorkflowStep(
    phase="claim",
    persona="manager",
    card="workflow-claim",
    executor=None,
    model=None,
    domain=None,
    inputs=(),
    outputs=(),
    gate_result="pending",
)


def _burn_generations(registry: JobRegistry, count: int, *, offset: int = 0) -> list[str]:
    """建立 `count` 個世代並全部終結為 superseded，回傳其 run_id 清單。

    `_manager_create_workflow_run` 會自動 supersede 同 (repo, work_id) 的前一
    代，最後一代以 `_manager_abandon_workflow_run` 手動終結（比照
    `tests/test_work_actions_repair_budget.py` 既有作法）。
    """

    created: list[str] = []
    for index in range(count):
        run = registry._manager_create_workflow_run(
            repo="acme/demo",
            work_id="demo",
            claim_key=f"claim:v1:{str(offset + index) * 64}",
            source_revision=f"rev-{offset + index}",
            workspace_root="/tmp/workspace",
            combo="feature-oneshot",
            current_phase="claim",
            steps=(_CLAIM_STEP,),
            issue_refs=("acme/demo#12",),
        )
        created.append(run.run_id)
    for run in registry.list_workflow_runs():
        if run.status == "ongoing" and run.run_id in created:
            registry._manager_abandon_workflow_run(
                run.run_id, evidence_ref=f"abandon:test-{run.run_id}"
            )
    return created


def _start(tmp_path: Path, registry: JobRegistry, snapshot: Path, *, starter=None):
    return work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        now=lambda: 150.0,
        snapshot_path=snapshot,
        state_path=tmp_path / "journal.jsonl",
        workflow_registry=registry,
        workflow_starter=starter,
    )["result"]


def _reset(
    tmp_path: Path,
    registry: JobRegistry,
    snapshot: Path,
    *,
    actor: str = "operator",
    reason: str = "#507/#511/#516 已修復部署，三代 abandon 全肇因於引擎缺陷",
    extra: dict | None = None,
):
    args = {
        "action": "reset-reclaim-budget",
        "repo": "acme/demo",
        "work_id": "demo",
        "actor": actor,
        "reason": reason,
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


# --------------------------------------------------------------------------
# 熔斷仍在／重置後可再 claim
# --------------------------------------------------------------------------


def test_budget_exhausted_result_names_reset_as_next_step(tmp_path: Path) -> None:
    """熔斷回傳必須指出下一步，operator 不該只看到「額度用盡」。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)

    def _never_start(authority, claim_key, reason):
        raise AssertionError("熔斷後不得建立新 run")

    result = _start(tmp_path, registry, snapshot, starter=_never_start)
    assert result["action"] == "needs_human"
    assert result["reason"] == "semantic-reclaim-budget-exhausted"
    assert result["superseded_generations"] == 3
    assert result["reclaim_budget_limit"] == work_actions.SEMANTIC_RECLAIM_LIMIT
    assert result["legal_next_steps"] == ("reset-reclaim-budget",)
    assert "reset-reclaim-budget" in result["next_step_hint"]
    assert "--reason" in result["next_step_hint"]


def test_reset_reclaim_budget_restores_claimability(tmp_path: Path) -> None:
    """重置後同一 work item 可再次 claim，且真的建立新 run。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    burned = _burn_generations(registry, 3)

    blocked = _start(tmp_path, registry, snapshot, starter=lambda *a: None)
    assert blocked["reason"] == "semantic-reclaim-budget-exhausted"

    reset = _reset(tmp_path, registry, snapshot)
    assert reset["action"] == "reclaim-budget-reset"
    assert reset["already_reset"] is False
    assert reset["cleared_generations"] == 3
    assert sorted(reset["cleared_run_ids"]) == sorted(burned)

    claimed = _start(tmp_path, registry, snapshot)
    assert claimed["action"] == "claim"
    assert claimed["run"]["run_id"] not in burned


def test_reset_preserves_every_superseded_run_row(tmp_path: Path) -> None:
    """重置不得刪除或改寫任何既有 run 紀錄——run 歷史是稽核來源。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    before = {run.run_id: run.to_dict() for run in registry.list_workflow_runs()}

    _reset(tmp_path, registry, snapshot)

    after = {run.run_id: run.to_dict() for run in registry.list_workflow_runs()}
    assert after == before


def test_reset_then_new_generations_rearm_the_breaker(tmp_path: Path) -> None:
    """重置只赦免當下已存在的世代；之後新產生的 superseded 重新計數。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    _reset(tmp_path, registry, snapshot)

    # 重置後的前兩個世代：額度未滿，仍可 claim（這次 claim 會真的建 v_n run）。
    _burn_generations(registry, 2, offset=10)
    assert _start(tmp_path, registry, snapshot)["action"] == "claim"

    # 再燒一代——連同上一行 claim 出來、隨即被 supersede 的那個 run，重置後的
    # 未赦免世代累積到 4（2 + claim 出來的那個 + 1），熔斷再次上膛。
    _burn_generations(registry, 1, offset=20)

    def _never_start(authority, claim_key, reason):
        raise AssertionError("重置後的額度用盡仍不得建立新 run")

    again = _start(tmp_path, registry, snapshot, starter=_never_start)
    assert again["reason"] == "semantic-reclaim-budget-exhausted"
    assert again["superseded_generations"] == 4
    # 被赦免的三代不得再出現在熔斷計數裡。
    assert not set(again["superseded_run_ids"]) & set(
        registry.list_reclaim_resets()[0]["cleared_run_ids"]
    )


def test_reset_survives_registry_reload(tmp_path: Path) -> None:
    """水位必須持久化：manager 重啟後重置仍然有效，狀態檔可重新載入。"""

    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    _reset(tmp_path, registry, snapshot)

    reloaded = JobRegistry(state_path=state)
    assert len(reloaded.list_reclaim_resets()) == 1
    assert _start(tmp_path, reloaded, snapshot)["action"] == "claim"


def test_legacy_state_file_without_reclaim_resets_still_loads(tmp_path: Path) -> None:
    """舊狀態檔沒有 `reclaim_resets` 根欄位時必須照常載入（加法相容）。"""

    state = tmp_path / "jobs.json"
    JobRegistry(state_path=state)
    registry = JobRegistry(state_path=state)
    _burn_generations(registry, 1)
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload.pop("reclaim_resets", None)
    state.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = JobRegistry(state_path=state)
    assert reloaded.list_reclaim_resets() == []
    assert len(reloaded.list_workflow_runs()) == 1


# --------------------------------------------------------------------------
# 入力驗證
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        None,
        "",
        "   ",
        " leading",
        "多行\n理由",
        "x" * 501,
    ],
)
def test_reset_requires_bounded_single_line_reason(tmp_path: Path, reason) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    with pytest.raises(ValueError, match="reason"):
        _reset(tmp_path, registry, snapshot, reason=reason)


@pytest.mark.parametrize("actor", [None, "", " op", "y" * 129])
def test_reset_requires_bounded_actor(tmp_path: Path, actor) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    with pytest.raises(ValueError, match="actor"):
        _reset(tmp_path, registry, snapshot, actor=actor)


def test_reset_rejects_caller_supplied_evidence(tmp_path: Path) -> None:
    """白名單外參數一律 fail-closed，caller 不得夾帶 evidence／水位。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    with pytest.raises(ValueError, match="rejects caller evidence/input"):
        _reset(
            tmp_path,
            registry,
            snapshot,
            extra={"cleared_run_ids": ["workflow-" + "0" * 20]},
        )


def test_reset_refuses_when_no_superseded_generation_exists(tmp_path: Path) -> None:
    """沒有任何可赦免世代時拒絕——不做無意義的狀態變更。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    with pytest.raises(RuntimeError, match="no superseded generation"):
        _reset(tmp_path, registry, snapshot)


def test_reset_resend_is_idempotent(tmp_path: Path) -> None:
    """重送相同請求回報 already-reset，不寫第二筆水位。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    first = _reset(tmp_path, registry, snapshot)
    second = _reset(tmp_path, registry, snapshot)

    assert second["already_reset"] is True
    assert second["cleared_generations"] == 0
    assert second["evidence"] == first["evidence"]
    assert len(registry.list_reclaim_resets()) == 1


# --------------------------------------------------------------------------
# evidence
# --------------------------------------------------------------------------


def test_reset_writes_immutable_content_addressed_evidence(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    burned = _burn_generations(registry, 3)
    result = _reset(tmp_path, registry, snapshot)

    target = Path(result["evidence"]["ref"])
    assert target.parent == (tmp_path / "evidence" / "work-reclaim-reset")
    assert target.name == f"demo-{result['evidence']['hash']}.json"
    assert not target.is_symlink()
    assert target.stat().st_mode & 0o222 == 0

    body = json.loads(target.read_text(encoding="utf-8"))
    assert body["schema"] == "cortex-work-reclaim-reset/v1"
    assert body["repo"] == "acme/demo"
    assert body["work_id"] == "demo"
    assert body["actor"] == "operator"
    assert body["reason"].startswith("#507/#511/#516")
    assert body["superseded_generations"] == 3
    assert body["cleared_run_ids"] == sorted(burned)
    assert body["created_at"] == "2025-08-12T12:00:00+00:00"

    from paulsha_cortex.coordinator import verification

    assert verification.canonical_json_hash(body) == result["evidence"]["hash"]


def test_reset_evidence_conflict_fails_closed(tmp_path: Path) -> None:
    """同名 evidence 檔內容不符時必須 raise，不得覆寫稽核紀錄。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    _burn_generations(registry, 3)
    body = work_actions._reclaim_reset_body(
        repo="acme/demo",
        work_id="demo",
        actor="operator",
        reason="conflict probe",
        cleared_run_ids=["workflow-" + "0" * 20],
        created_at="2025-08-12T12:00:00+00:00",
    )
    from paulsha_cortex.coordinator import verification

    digest = verification.canonical_json_hash(body)
    root = tmp_path / "evidence" / "work-reclaim-reset"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"demo-{digest}.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="evidence conflict"):
        work_actions._reclaim_reset_record(body, state_path=tmp_path / "journal.jsonl")


# --------------------------------------------------------------------------
# control contract
# --------------------------------------------------------------------------


def _request(args: dict) -> dict:
    return contract.build_request(
        req_type="work-action", args=args, requested_by="operator"
    )


def test_contract_accepts_well_formed_reset_request() -> None:
    validated = contract.validate_request(
        _request(
            {
                "action": "reset-reclaim-budget",
                "repo": "acme/demo",
                "work_id": "demo",
                "actor": "operator",
                "reason": "引擎缺陷已修復",
            }
        )
    )
    assert validated["args"]["action"] == "reset-reclaim-budget"


@pytest.mark.parametrize(
    "args",
    [
        {"action": "reset-reclaim-budget", "repo": "acme/demo", "work_id": "demo"},
        {
            "action": "reset-reclaim-budget",
            "repo": "acme/demo",
            "work_id": "demo",
            "actor": "operator",
        },
        {
            "action": "reset-reclaim-budget",
            "repo": "acme/demo",
            "work_id": "demo",
            "actor": "operator",
            "reason": "多行\n理由",
        },
    ],
)
def test_contract_rejects_incomplete_reset_request(args: dict) -> None:
    with pytest.raises(ValueError, match="reset-reclaim-budget"):
        contract.validate_request(_request(args))
