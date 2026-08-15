"""#536：planning artifacts 發佈與 run 狀態更新的單一事務邊界與恢復迴圈。

現場（run `workflow-7a430d31eff66ef13630`）：define 階段的 run，brainstorm 已把
spec/design 發佈到 operator worktree，但 run 狀態永不推進——`updated_at` 停在建立
時刻、`gate_refs=[]`、facets 空、manager log 無錯誤。根因之一是「發佈 artifacts」
與「更新 run 狀態」是兩次分離的 durable 寫入，中間崩潰就留下「artifacts 已落地、
run 狀態停在原地」的中間態。

journal（`<coordinator_root>/planning-transactions/<run_id>.json`）本來就記了每個
mutation 的 before/after hash，但 `reconcile()` **只能由持有該 run 的呼叫端逐 run
觸發**（define 起始、`resume_workflow_run`）。run 一旦離開 `ongoing`
（superseded／done），journal 與它描述的殘留檔就再也沒有任何迴圈會看——實測
coordinator root 上就躺著兩份這種孤兒 journal。

本檔釘住的修法：`reconcile_planning_transactions` 掃整個 journal 目錄、與 run 狀態
無關，成為唯一的恢復路徑；`prepare_commit()` 把事務邊界寫成 durable 事實。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.model_identities import CapabilityProbe, IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo
from paulsha_cortex.coordinator.workflow import GateEvidenceRef, WorkflowManifest


class _HardCrash(BaseException):
    """模擬行程被 SIGKILL：沒有任何 except/finally handler 會跑。"""


# --- 低階事務／sweep 用的最小樁 -------------------------------------------------


class _FakeRegistry:
    def __init__(self, runs) -> None:
        self._runs = list(runs)
        self.updates: list[tuple[str, dict]] = []

    def list_workflow_runs(self):
        return list(self._runs)

    def _manager_update_workflow_run(self, run_id: str, **kwargs):
        self.updates.append((run_id, kwargs))
        return self._runs[0]


def _fake_run(
    *,
    run_id: str,
    workspace_root: Path,
    gate_refs: tuple[GateEvidenceRef, ...] = (),
    status: str = "ongoing",
    facets: tuple[str, ...] = (),
):
    return SimpleNamespace(
        run_id=run_id,
        workspace_root=str(workspace_root),
        gate_refs=gate_refs,
        status=status,
        facets=facets,
    )


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    coordinator = tmp_path / "coordinator"
    workspace.mkdir()
    coordinator.mkdir()
    return workspace, coordinator


def _publish_generation(
    workspace: Path,
    coordinator: Path,
    *,
    run_id: str = "workflow-1",
    with_evidence: bool = True,
    prepared: bool = False,
):
    """走真正的發佈路徑產生一份未收斂的 journal（＝崩潰現場）。"""

    transaction = manager._PlanningPublicationTransaction(
        root=workspace, run_id=run_id, journal_root=coordinator
    )
    artifacts = []
    for name in ("demo-spec.md", "demo-design.md"):
        target = workspace / "docs/superpowers/specs" / name
        transaction.publish(
            target, f"# {name}\n".encode("utf-8"), baseline_hash=None, kind="artifact"
        )
        artifacts.append(target)
    evidence = None
    if with_evidence:
        evidence = coordinator / "evidence" / f"brainstorm-{run_id}.json"
        transaction.write_evidence(evidence, {"schema_version": 1, "kind": "brainstorm-peer"})
    if prepared:
        transaction.prepare_commit()
    return transaction, tuple(artifacts), evidence


def _journal(coordinator: Path, run_id: str = "workflow-1") -> Path:
    return coordinator / "planning-transactions" / f"{run_id}.json"


def _sweep(registry, coordinator: Path, **kwargs):
    kwargs.setdefault("now", time.time() + 10_000)
    return manager.reconcile_planning_transactions(
        registry=registry, coordinator_root=coordinator, **kwargs
    )


# --- 事務邊界本身 ---------------------------------------------------------------


def test_prepare_commit_seals_the_file_side_of_the_transaction(tmp_path: Path) -> None:
    """`prepare_commit` 把「檔案側已全部落地、下一步是唯一的 commit point」
    寫成 durable 事實，且封住之後不得再發佈。"""

    workspace, coordinator = _roots(tmp_path)
    transaction, _, _ = _publish_generation(workspace, coordinator)

    body = json.loads(_journal(coordinator).read_text(encoding="utf-8"))
    assert body["schema_version"] == 3
    assert body["phase"] == "publishing"

    transaction.prepare_commit()
    sealed = json.loads(_journal(coordinator).read_text(encoding="utf-8"))
    assert sealed["phase"] == "prepared"
    assert sealed["operations"] == body["operations"]

    with pytest.raises(ValueError, match="already prepared for commit"):
        transaction.publish(
            workspace / "docs/superpowers/specs/late.md",
            b"# Late\n",
            baseline_hash=None,
            kind="artifact",
        )


# --- 恢復迴圈：崩在提交邊界 ------------------------------------------------------


@pytest.mark.parametrize("prepared", [False, True])
def test_sweep_rolls_back_uncommitted_publication_regardless_of_phase(
    tmp_path: Path, prepared: bool
) -> None:
    """發佈完成（甚至已封邊界）但 registry 從未提交 → 回退，並且收斂。

    判準只有一條：run row 上有沒有這次的 brainstorm gate ref。沒有就代表這批
    產出從未被綁進任何 run（無 authority、無 source revision），前滾在語意上
    不成立——回退才是唯一定義良好的收斂方向。
    """

    workspace, coordinator = _roots(tmp_path)
    _, artifacts, evidence = _publish_generation(workspace, coordinator, prepared=prepared)
    assert all(path.is_file() for path in artifacts)
    registry = _FakeRegistry([_fake_run(run_id="workflow-1", workspace_root=workspace)])

    report = _sweep(registry, coordinator)

    assert [row["outcome"] for row in report] == ["rolled-back"]
    assert report[0]["phase"] == ("prepared" if prepared else "publishing")
    assert not any(path.exists() for path in artifacts)
    assert evidence is not None and not evidence.exists()
    assert not _journal(coordinator).exists()
    assert registry.updates == []


def test_sweep_rolls_forward_when_registry_row_carries_the_gate_ref(tmp_path: Path) -> None:
    """registry 已提交、只差退役 journal → 前滾（逐位元組驗證後退役）。"""

    workspace, coordinator = _roots(tmp_path)
    _, artifacts, evidence = _publish_generation(workspace, coordinator, prepared=True)
    assert evidence is not None
    committed_ref = GateEvidenceRef(
        "brainstorm", str(evidence), manager._sha256_path(evidence)
    )
    registry = _FakeRegistry(
        [
            _fake_run(
                run_id="workflow-1", workspace_root=workspace, gate_refs=(committed_ref,)
            )
        ]
    )

    report = _sweep(registry, coordinator)

    assert [row["outcome"] for row in report] == ["committed"]
    assert all(path.is_file() for path in artifacts)
    assert evidence.is_file()
    assert not _journal(coordinator).exists()


# --- 恢復迴圈：既有殘留自癒 ------------------------------------------------------


def test_sweep_heals_legacy_v2_journal_of_a_superseded_run(tmp_path: Path) -> None:
    """實際部署上的殘留：v2 journal ＋ run 已 superseded。

    這正是 #536 現場留下的形態（`expected_gate_ref: null`、兩筆
    `before_exists: false` 的 artifact op、run 後來被 abandon 成
    `superseded`）。resume 迴圈只看 `ongoing`，所以修法前沒有任何路徑會碰它；
    sweep 與 run 狀態無關，因此同一條恢復路徑就把它收斂掉。
    """

    workspace, coordinator = _roots(tmp_path)
    _, artifacts, _ = _publish_generation(workspace, coordinator, with_evidence=False)
    journal = _journal(coordinator)
    legacy = json.loads(journal.read_text(encoding="utf-8"))
    legacy.pop("phase")
    legacy["schema_version"] = 2
    journal.write_text(json.dumps(legacy, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    registry = _FakeRegistry(
        [
            _fake_run(
                run_id="workflow-1",
                workspace_root=workspace,
                status="superseded",
                facets=("blocked", "planning_released"),
            )
        ]
    )

    report = _sweep(registry, coordinator)

    assert [row["outcome"] for row in report] == ["rolled-back"]
    assert report[0]["phase"] == "publishing"
    assert report[0]["status"] == "superseded"
    assert not any(path.exists() for path in artifacts)
    assert not journal.exists()
    # superseded run 不得被改動（abandon 已是終態）。
    assert registry.updates == []


def test_sweep_rejects_v2_journal_that_smuggles_a_phase_field(tmp_path: Path) -> None:
    workspace, coordinator = _roots(tmp_path)
    _publish_generation(workspace, coordinator, with_evidence=False)
    journal = _journal(coordinator)
    body = json.loads(journal.read_text(encoding="utf-8"))
    body["schema_version"] = 2
    journal.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    registry = _FakeRegistry([_fake_run(run_id="workflow-1", workspace_root=workspace)])

    report = _sweep(registry, coordinator)

    assert report[0]["outcome"] == "drift"
    assert journal.is_file()


# --- 恢復迴圈：安全護欄 ---------------------------------------------------------


def test_sweep_leaves_in_flight_journals_alone(tmp_path: Path) -> None:
    """剛寫下的 journal 可能還在飛（daemon 之外的前景發佈），不得誤傷。"""

    workspace, coordinator = _roots(tmp_path)
    _, artifacts, _ = _publish_generation(workspace, coordinator)
    registry = _FakeRegistry([_fake_run(run_id="workflow-1", workspace_root=workspace)])

    report = manager.reconcile_planning_transactions(
        registry=registry, coordinator_root=coordinator
    )

    assert [row["outcome"] for row in report] == ["in-flight"]
    assert all(path.is_file() for path in artifacts)
    assert _journal(coordinator).is_file()


def test_sweep_surfaces_drift_instead_of_forcing_a_rollback(tmp_path: Path) -> None:
    """operator 改過殘留檔 → 不刪、不靜默：留檔＋落 needs_human facet。"""

    workspace, coordinator = _roots(tmp_path)
    _, artifacts, _ = _publish_generation(workspace, coordinator, prepared=True)
    artifacts[0].write_text("operator edit\n", encoding="utf-8")
    registry = _FakeRegistry([_fake_run(run_id="workflow-1", workspace_root=workspace)])

    report = _sweep(registry, coordinator)

    assert report[0]["outcome"] == "drift"
    assert report[0]["surfaced"] is True
    assert artifacts[0].read_text(encoding="utf-8") == "operator edit\n"
    assert _journal(coordinator).is_file()
    assert registry.updates and registry.updates[0][0] == "workflow-1"
    assert "needs_human" in registry.updates[0][1]["facets"]


def test_sweep_does_not_touch_a_journal_whose_run_is_unknown(tmp_path: Path) -> None:
    """沒有 run row 就無法驗證 journal 自報的 workspace root——fail closed，
    但必須留下可見紀錄，不得靜默。"""

    workspace, coordinator = _roots(tmp_path)
    _, artifacts, _ = _publish_generation(workspace, coordinator)
    registry = _FakeRegistry([])

    report = _sweep(registry, coordinator)

    assert report[0]["outcome"] == "unknown-run"
    assert all(path.is_file() for path in artifacts)
    assert _journal(coordinator).is_file()


def test_sweep_never_deletes_an_artifact_the_operator_already_committed(
    tmp_path: Path,
) -> None:
    """殘留檔若已被納入 git 追蹤，就不再是「未提交的發佈殘留」——跳過刪除、
    退役 journal（比照 `work_actions._gc_one_abandoned_planning_artifact`）。"""

    workspace, coordinator = _roots(tmp_path)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    _, artifacts, _ = _publish_generation(workspace, coordinator, prepared=True)
    subprocess.run(
        ["git", "-C", str(workspace), "add", "--", str(artifacts[0].relative_to(workspace))],
        check=True,
    )
    registry = _FakeRegistry([_fake_run(run_id="workflow-1", workspace_root=workspace)])

    report = _sweep(registry, coordinator)

    assert report[0]["outcome"] == "adopted"
    assert report[0]["skipped"] == [str(artifacts[0])]
    assert artifacts[0].is_file()
    assert not artifacts[1].exists()
    assert not _journal(coordinator).exists()


# --- daemon 接線 ----------------------------------------------------------------


def test_periodic_tick_runs_the_planning_transaction_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """恢復迴圈必須真的被 tick 驅動，且結果對 operator 可見。"""

    registry = SimpleNamespace(
        _state_path=str(tmp_path / "jobs.json"), list_workflow_runs=lambda: []
    )
    dispatcher = SimpleNamespace(_registry=registry, _git_runner=lambda args: "")
    calls: list[Path] = []

    def fake_sweep(*, registry, coordinator_root):
        calls.append(Path(coordinator_root))
        return [
            {"run_id": "workflow-1", "outcome": "rolled-back", "phase": "prepared"},
            {"run_id": "workflow-2", "outcome": "in-flight"},
        ]

    monkeypatch.setattr(manager_daemon.manager, "reconcile_planning_transactions", fake_sweep)
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        run_tick_fn=lambda dispatcher_arg, **kwargs: {
            "dispatch_skipped": False, "dispatched": [], "completed": [],
            "errors": [], "reaped": None,
        },
        scan_specs_fn=lambda specs_dir: [],
        auto_claim_fn=lambda: [],
        workflow_identity_registry=object(),
    )

    summary = runner()

    assert calls == [tmp_path]
    # in-flight 是「這輪沒事做」，不佔 summary 版面；已收斂的必須現身。
    assert summary["planning_transactions"] == [
        {"run_id": "workflow-1", "outcome": "rolled-back", "phase": "prepared"}
    ]


def test_planning_transaction_sweep_failure_cannot_break_the_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#246 的 tick isolation 紀律：sweep 整批失效只降級並回報。"""

    workflow = SimpleNamespace(
        run_id="run-1", work_id="demo", repo="acme/demo", status="ongoing",
        facets=(), current_phase="build", claim_key="claim:legacy:demo",
        source_revision="",
    )
    registry = SimpleNamespace(
        _state_path=str(tmp_path / "jobs.json"), list_workflow_runs=lambda: [workflow]
    )
    dispatcher = SimpleNamespace(_registry=registry, _git_runner=lambda args: "")
    resumed: list[str] = []

    def boom(**kwargs):
        raise OSError("journal directory unreadable")

    monkeypatch.setattr(manager_daemon.manager, "reconcile_planning_transactions", boom)
    monkeypatch.setattr(
        manager_daemon.manager,
        "resume_workflow_run",
        lambda dispatcher_arg, **kwargs: resumed.append(kwargs["run_id"]),
    )
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        run_tick_fn=lambda dispatcher_arg, **kwargs: {
            "dispatch_skipped": False, "dispatched": [], "completed": [],
            "errors": [], "reaped": None,
        },
        scan_specs_fn=lambda specs_dir: [],
        auto_claim_fn=lambda: [],
        workflow_identity_registry=object(),
    )

    summary = runner()

    assert summary["planning_transaction_failed"] is True
    assert "OSError" in summary["planning_transaction_error"]
    assert resumed == ["run-1"]


# --- 端到端：真正的 define 流程 --------------------------------------------------


def _manifest() -> WorkflowManifest:
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "production wiring", change="production-wiring")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary",
                "independence_domain": "openai", "capabilities": ["planning"],
            },
            {
                "executor": "claude", "model_id": "secondary",
                "independence_domain": "anthropic", "capabilities": ["planning"],
            },
        ]
    )


def _questioner(report):
    from paulsha_cortex.coordinator.planning import assess_planning_completeness

    return assess_planning_completeness([]).default_question_pack.to_dict()


def _secondary(pack, identity):
    return {
        "schema_version": 1,
        "question_pack_id": pack["pack_id"],
        "evidence": [
            {"question_id": row["question_id"], "claims": ["missing"], "source_refs": ["scan:1"]}
            for row in pack["questions"]
        ],
    }


def _integrator(pack, evidence):
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nBound.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nBound.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    refs = {
        "spec": "docs/superpowers/specs/production-wiring-spec.md",
        "design": "docs/superpowers/specs/production-wiring-design.md",
        "plan": "docs/superpowers/plans/production-wiring-plan.md",
    }
    resolutions = []
    artifacts = []
    for row in pack["questions"]:
        kind = row["kind"].removeprefix("missing-")
        resolutions.append(
            {
                "question_id": row["question_id"], "decision": "accepted",
                "artifact_kind": kind, "artifact_refs": [refs[kind]],
            }
        )
        artifacts.append({"kind": kind, "path": refs[kind], "content": bodies[kind]})
    return {
        "schema_version": 1, "question_pack_id": pack["pack_id"],
        "secondary_evidence_hash": evidence["evidence_hash"],
        "resolutions": resolutions, "artifacts": artifacts,
    }


def _define_args(manifest_path: Path, artifact_root: Path) -> dict[str, object]:
    return {
        "action": "start",
        "manifest_path": str(manifest_path),
        "work_id": "production-wiring",
        "repo": "hamanpaul/paulsha-cortex",
        "claim_key": "hamanpaul/paulsha-cortex/production-wiring/rev-a",
        "source_revision": "rev-a",
        "artifact_root": str(artifact_root),
        "planning_artifacts": [],
        "primary_executor": "codex",
        "primary_model": "primary",
        "evidence_dir": str(artifact_root / "evidence"),
    }


def _apply_define(registry, tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(_manifest().to_dict()), encoding="utf-8")
    return manager.apply_workflow_action(
        registry,
        args=_define_args(manifest_path, tmp_path),
        identity_registry=_identities(),
        probes={
            ("claude", "secondary"): CapabilityProbe.ready_for(
                "claude", "secondary", "anthropic"
            )
        },
        primary_questioner=_questioner,
        secondary_planner=_secondary,
        primary_integrator=_integrator,
        coordinator_root=tmp_path,
    )


def test_hard_crash_between_publication_and_state_update_is_healed_by_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#536 主回歸：發佈後、狀態更新前行程當場死亡。

    以「registry 提交那一刻炸掉 ＋ 事務的清理路徑全部失效」模擬 SIGKILL——
    沒有任何 handler 有機會回滾。修法前這會留下一個對所有恢復迴圈隱形的中間態
    （artifacts 已落地、run 停在 define、facets 空、無 gate_refs、journal 無人
    問津）。修法後同一條恢復路徑（sweep）必須把它收斂掉。
    """

    state_path = tmp_path / "registry.json"
    registry = JobRegistry(state_path=state_path)
    original_rollback = manager._PlanningPublicationTransaction.rollback
    original_commit = manager._PlanningPublicationTransaction.commit
    real_write = registry._write_payload_atomically

    def crash_on_plan_transition(payload):
        if any(
            row.get("current_phase") == "plan" and row.get("gate_refs")
            for row in payload.get("workflows", [])
        ):
            raise _HardCrash("manager process killed at the commit boundary")
        real_write(payload)

    monkeypatch.setattr(
        manager._PlanningPublicationTransaction, "rollback", lambda self, **kwargs: ()
    )
    monkeypatch.setattr(manager._PlanningPublicationTransaction, "commit", lambda self: None)
    monkeypatch.setattr(registry, "_write_payload_atomically", crash_on_plan_transition)

    with pytest.raises(_HardCrash):
        _apply_define(registry, tmp_path)

    monkeypatch.setattr(manager._PlanningPublicationTransaction, "rollback", original_rollback)
    monkeypatch.setattr(manager._PlanningPublicationTransaction, "commit", original_commit)
    monkeypatch.setattr(registry, "_write_payload_atomically", real_write)

    # ---- 中間態確實成立（修法前的永久隱形現場）----
    restarted = JobRegistry(state_path=state_path)
    stalled = restarted.list_workflow_runs()[0]
    assert stalled.current_phase == "define"
    assert stalled.status == "ongoing"
    assert stalled.facets == ()
    assert stalled.gate_refs == ()
    published = sorted((tmp_path / "docs/superpowers").rglob("*.md"))
    assert published
    journal = _journal(tmp_path, stalled.run_id)
    assert journal.is_file()
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "prepared"

    # ---- 同一條恢復路徑收斂 ----
    report = _sweep(restarted, tmp_path)

    assert [row["outcome"] for row in report] == ["rolled-back"]
    assert report[0]["run_id"] == stalled.run_id
    assert not journal.exists()
    assert not any(path.exists() for path in published)
    assert not list((tmp_path / "evidence").glob("brainstorm-*.json"))

    # ---- 收斂後重跑 define 正常成功（殘留不再是下一輪的地雷）----
    result = _apply_define(JobRegistry(state_path=state_path), tmp_path)
    assert result["reason"] == "brainstorm-complete"
    assert result["current_phase"] == "plan"


def test_successful_define_seals_the_boundary_and_retires_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正常路徑不回歸：邊界在 registry 提交前就已封住，成功後 journal 退役，
    sweep 也不會再對它做任何事。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    observed: list[str] = []
    original = manager._validated_brainstorm_planning_authority

    def spy(run, **kwargs):
        journal = _journal(tmp_path, run.run_id)
        observed.append(json.loads(journal.read_text(encoding="utf-8"))["phase"])
        return original(run, **kwargs)

    monkeypatch.setattr(manager, "_validated_brainstorm_planning_authority", spy)

    result = _apply_define(registry, tmp_path)

    assert result["reason"] == "brainstorm-complete"
    assert result["current_phase"] == "plan"
    # registry 提交之前，journal 已經宣告「檔案側封住、下一步是 commit point」。
    assert observed == ["prepared"]
    run = registry.list_workflow_runs()[0]
    assert not _journal(tmp_path, run.run_id).exists()
    assert sorted(path.name for path in (tmp_path / "docs/superpowers").rglob("*.md"))

    assert _sweep(registry, tmp_path) == []
