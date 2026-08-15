"""診斷 invariant 家族：#527／#514／#515／#511／#482 一次收編。

0813–0814 五次獨立命中同一條 invariant 的缺口，逐案補洞已證明無效。本檔把
invariant 本身變成測試對象：

> 任何把 run 轉入 ``needs_human``、把 evidence 標為 absent 的狀態變更，必須
> 同時落一份結構化理由（機器可讀 reason ＋ 人可讀 detail ＋ 來源位置）到 run
> 或 evidence，並可由 ``cortex status``／``work show`` 曝光。

三層測試：

1. **掃描式 invariant**（``test_every_needs_human_setter_supplies_a_reason``）——
   AST 枚舉全庫所有把 ``needs_human`` 寫進 facets 的設置點，斷言每一個都同時
   帶 ``needs_human_reason``。新增一個忘了帶理由的設置點會在這裡炸，不必等到
   dogfooding 現場。
2. **執行期強制**——registry 的狀態轉移 API 對「沒帶理由」fail-closed，對
   「清了 facet」自動清理由。
3. **五張 issue 的原始現場**各自成為 fixture。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, manager_daemon, review
from paulsha_cortex.coordinator.diagnostics import (
    DiagnosticInvariantError,
    DiagnosticReason,
    diagnostic_reason,
)
from paulsha_cortex.coordinator.planning import (
    ArtifactEvidenceFailure,
    PlanningArtifact,
    _post_integration_artifact_evidence,
    assess_planning_completeness,
)
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowRun, WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "paulsha_cortex"

_MUTATION_FUNCTIONS = {
    "_manager_update_workflow_run",
    "_manager_create_workflow_run",
}


def _step(
    phase: str = "define",
    card: str = "brainstorming",
    gate_result: str = "pending",
) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona="planner" if phase in {"define", "plan"} else "builder",
        card=card,
        executor="codex",
        model="gpt-primary",
        domain="openai",
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


class _FakeDispatcher:
    """periodic tick runner 只從 dispatcher 取 registry 與 git runner。"""

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry
        self._git_runner = None


def _seed_run(registry: JobRegistry, **overrides) -> WorkflowRun:
    fields = dict(
        work_id="diagnostic-invariant",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase="define",
        steps=(_step(),),
        issue_refs=("hamanpaul/paulsha-cortex#527",),
        openspec_refs=("diagnostic-invariant",),
        attempts={"define": 1},
        gate_status="running",
    )
    fields.update(overrides)
    return registry._manager_create_workflow_run(**fields)


# ---------------------------------------------------------------------------
# 第 1 層：掃描式 invariant
# ---------------------------------------------------------------------------


def _introduces_needs_human(call: ast.Call) -> bool:
    """這個 registry 呼叫的 `facets=` 引數會不會把 `needs_human` 寫進去？

    保守判定：只要引數的原始碼裡出現 `"needs_human"` 字面值，且不是
    「把它濾掉」的形態（`facet != "needs_human"`），就算會設置。掃描式
    invariant 寧可誤報也不可漏報——漏報正是這五張 issue 的形態。
    """

    for keyword in call.keywords:
        if keyword.arg != "facets":
            continue
        source = ast.unparse(keyword.value)
        if "needs_human" not in source:
            return False
        if "!=" in source and "needs_human" in source.split("!=", 1)[1]:
            return False
        return True
    return False


def _supplies_reason(call: ast.Call) -> bool:
    return any(keyword.arg == "needs_human_reason" for keyword in call.keywords)


def _needs_human_setter_sites(source_root: Path = PACKAGE_ROOT) -> list[tuple[Path, ast.Call]]:
    sites: list[tuple[Path, ast.Call]] = []
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _MUTATION_FUNCTIONS:
                continue
            if not _introduces_needs_human(node):
                continue
            sites.append((path, node))
    return sites


def test_scan_finds_the_known_needs_human_setters() -> None:
    """掃描器本身必須真的掃得到東西——否則上面那條 invariant 是空的通過。"""

    sites = _needs_human_setter_sites()
    assert len(sites) >= 20, sites
    files = {path.name for path, _ in sites}
    # #527 的根因現場（daemon resume 迴圈）與 planning lane 都必須在掃描範圍內。
    assert "manager_daemon.py" in files
    assert "manager.py" in files
    assert "work_actions.py" in files


def test_every_needs_human_setter_supplies_a_reason() -> None:
    """invariant 主體：沒有任何一個設置點可以不帶結構化理由。

    這是本 PR 的核心斷言。新增一條 `facets=(..., "needs_human")` 而忘了帶
    `needs_human_reason` 會在這裡失敗——不必等到 operator 在 dogfooding 現場
    看到一個沒有理由的 run。
    """

    offenders = [
        f"{path.relative_to(PACKAGE_ROOT.parent)}:{node.lineno}\n{ast.unparse(node)[:300]}"
        for path, node in _needs_human_setter_sites()
        if not _supplies_reason(node)
    ]
    assert offenders == [], "以下設置點把 run 轉入 needs_human 卻沒帶理由：\n" + "\n\n".join(
        offenders
    )


def test_the_scan_actually_catches_a_missing_reason(tmp_path: Path) -> None:
    """掃描器的反證：一個忘了帶理由的設置點必須被抓出來。

    沒有這條，上面的 invariant 可能只是因為掃描器壞掉而「通過」。
    """

    offender = tmp_path / "offender.py"
    offender.write_text(
        'registry._manager_update_workflow_run(run.run_id, facets=("needs_human",))\n',
        encoding="utf-8",
    )
    compliant = tmp_path / "compliant.py"
    compliant.write_text(
        "registry._manager_update_workflow_run(\n"
        '    run.run_id, facets=("needs_human",), needs_human_reason=reason\n'
        ")\n",
        encoding="utf-8",
    )
    filtering = tmp_path / "filtering.py"
    filtering.write_text(
        "registry._manager_update_workflow_run(\n"
        '    run.run_id, facets=tuple(f for f in run.facets if f != "needs_human")\n'
        ")\n",
        encoding="utf-8",
    )

    sites = _needs_human_setter_sites(tmp_path)
    flagged = {path.name for path, node in sites if not _supplies_reason(node)}
    assert flagged == {"offender.py"}
    # 「把 needs_human 濾掉」的形態不得被誤判為設置點。
    assert "filtering.py" not in {path.name for path, _ in sites}


# ---------------------------------------------------------------------------
# 第 2 層：registry 狀態轉移 API 的執行期強制
# ---------------------------------------------------------------------------


def test_transition_into_needs_human_without_reason_is_rejected(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry)
    with pytest.raises(DiagnosticInvariantError) as excinfo:
        registry._manager_update_workflow_run(run.run_id, facets=("needs_human",))
    assert "結構化理由" in str(excinfo.value)
    # fail-closed：狀態一個位元組都沒動。
    assert registry.get_workflow_run(run.run_id).facets == ()


def test_creating_a_run_already_blocked_without_reason_is_rejected(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    with pytest.raises(DiagnosticInvariantError):
        _seed_run(registry, facets=("needs_human",))


def test_reason_without_the_facet_is_rejected(tmp_path: Path) -> None:
    """反向也 fail-closed：帶了理由卻不設 facet 是不一致的狀態。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry)
    with pytest.raises(DiagnosticInvariantError):
        registry._manager_update_workflow_run(
            run.run_id, facets=(), needs_human_reason=fixture_needs_human_reason()
        )


def test_reason_is_persisted_and_survives_reload(tmp_path: Path) -> None:
    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "worktree-target-exists",
            "worktree target already exists",
            source="tests.test_diagnostic_invariant_family_527",
            run_id=run.run_id,
        ),
    )
    reloaded = JobRegistry(state_path=state).get_workflow_run(run.run_id)
    assert reloaded.needs_human_reason["reason"] == "worktree-target-exists"
    assert reloaded.needs_human_reason["detail"] == "worktree target already exists"
    assert reloaded.needs_human_reason["source"].startswith("tests.")
    assert reloaded.needs_human_reason["recorded_at"]


def test_reason_is_carried_forward_on_repeat_updates(tmp_path: Path) -> None:
    """facet 已在、再寫一次同樣的 facet 不得把第一次的理由洗掉。

    大量呼叫端會在同一個 run 上重複寫 `facets=("needs_human",)`（例如
    `dispatch_or_stop` 的 except 分支再被 resume 迴圈碰一次）——第一次的理由
    才是真正的根因。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "root-cause",
            "第一次的真正原因",
            source="tests.first",
        ),
    )
    later = registry._manager_update_workflow_run(
        run.run_id, facets=("needs_human",), gate_status="failed"
    )
    assert later.needs_human_reason["reason"] == "root-cause"


def test_clearing_the_facet_clears_the_reason(tmp_path: Path) -> None:
    """陳舊理由比沒有理由更糟：facet 清掉了理由必須跟著清。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id, facets=("needs_human",), needs_human_reason=fixture_needs_human_reason()
    )
    cleared = registry._manager_update_workflow_run(run.run_id, facets=())
    assert cleared.needs_human_reason is None


def test_workflow_run_rejects_a_reason_without_the_facet() -> None:
    """dataclass 這層也鎖住同一條耦合，`replace()` 的直呼叫端一併涵蓋。"""

    registry_free_reason = fixture_needs_human_reason().to_dict()
    with pytest.raises(ValueError, match="同進同退"):
        WorkflowRun(
            run_id="workflow-" + "a" * 20,
            work_id="diagnostic-invariant",
            repo="hamanpaul/paulsha-cortex",
            claim_key="claim:v1:" + "1" * 64,
            source_revision="2" * 64,
            workspace_root="/tmp/workspace",
            combo="feature-oneshot",
            current_phase="define",
            steps=(_step(),),
            issue_refs=(),
            openspec_refs=(),
            pr_refs=(),
            attempts={},
            evidence_refs=(),
            gate_refs=(),
            brainstorm_required=False,
            primary_domain=None,
            candidate_head=None,
            verified_head=None,
            facets=(),
            gate_status="running",
            created_at=manager._utcnow(),
            updated_at=manager._utcnow(),
            needs_human_reason=registry_free_reason,
        )


def test_legacy_state_without_a_reason_still_loads(tmp_path: Path) -> None:
    """既有部署的狀態檔裡就躺著「有 facet、沒理由」的 run。

    載入時 fail-closed 會直接把 manager 打掛——本 invariant 之前寫進去的 run
    必須照常載入，只是沒有理由可呈現。
    """

    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id, facets=("needs_human",), needs_human_reason=fixture_needs_human_reason()
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    for row in payload["workflows"]:
        row.pop("needs_human_reason", None)
    state.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = JobRegistry(state_path=state).get_workflow_run(run.run_id)
    assert reloaded.facets == ("needs_human",)
    assert reloaded.needs_human_reason is None


def test_reason_shape_is_fail_closed() -> None:
    """機器可讀 reason ＋ 人可讀 detail ＋ 來源位置，三者缺一不可。"""

    with pytest.raises(DiagnosticInvariantError):
        DiagnosticReason(reason="Not A Code", detail="x", source="mod.fn")
    with pytest.raises(DiagnosticInvariantError):
        DiagnosticReason(reason="ok-code", detail="   ", source="mod.fn")
    with pytest.raises(DiagnosticInvariantError):
        DiagnosticReason(reason="ok-code", detail="x", source="not a source location")
    # 換行會撞上 recover-planning 的 `failure_reason` 檢查，一律壓成單行。
    assert "\n" not in DiagnosticReason(
        reason="ok-code", detail="a\nb", source="mod.fn"
    ).detail


# ---------------------------------------------------------------------------
# 第 3 層：五張 issue 的原始現場
# ---------------------------------------------------------------------------


def test_issue_527_resume_failure_lands_the_exception_on_the_run(tmp_path: Path) -> None:
    """#527 現場：build 階段無聲掛 needs_human。

    真正的原因是 ``ValueError: worktree target already exists``，但它只被
    ``_log_error`` print 到 stderr（由 service-manager 導向
    ``~/.agents/log/manager.log``），run 上只留一個沒有理由的 facet。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(
        registry,
        current_phase="build",
        steps=(_step("build", "worktree-isolation"),),
        claim_key="claim:legacy:diagnostic-invariant",
    )

    def exploding_resume(*args, **kwargs):
        # `seams.ScriptWorktreeCreator.create()` 對已存在的 target fail-closed
        # ——#527 現場的實際例外（前代 run 死亡時未回收 worktree）。
        raise ValueError("worktree target already exists")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(manager_daemon.manager, "resume_workflow_run", exploding_resume)
        runner = manager_daemon.build_periodic_tick_runner(
            dispatcher=_FakeDispatcher(registry),
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=object(),
            run_tick_fn=lambda dispatcher_arg, **kwargs: {
                "dispatch_skipped": False,
                "dispatched": [],
                "completed": [],
                "errors": [],
                "reaped": None,
            },
            scan_specs_fn=lambda specs_dir: [],
            auto_claim_fn=lambda: [],
            workflow_identity_registry=object(),
        )
        runner()
    finally:
        monkeypatch.undo()

    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets
    reason = persisted.needs_human_reason
    assert reason is not None, "#527：needs_human 不得再是唯一訊號"
    assert reason["reason"] == "resume-workflow-failed"
    assert "worktree target already exists" in reason["detail"]
    assert reason["source"] == "manager_daemon.periodic_tick:resume-workflow"
    assert reason["context"]["phase"] == "build"


def test_issue_527_status_attention_surfaces_the_blocked_run(tmp_path: Path) -> None:
    """#527 的呈現面：run 過去在 `cortex status` 的五份清單裡一份都不出現。"""

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry, current_phase="build", steps=(_step("build", "tdd-red"),))
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "resume-workflow-failed",
            "ValueError: worktree target already exists",
            source="manager_daemon.periodic_tick:resume-workflow",
        ),
    )

    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        scan_specs_fn=lambda _: [],
        ready_units_fn=lambda metas, predicate: [],
    )
    payload = provider()

    entries = [row for row in payload["attention"] if row.get("kind") == "workflow_run"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["run_id"] == run.run_id
    assert entry["current_phase"] == "build"
    assert entry["reason"] == "resume-workflow-failed"
    assert entry["blocking_reason"]["detail"].startswith("ValueError")
    assert entry["blocking_reason"]["source"].startswith("manager_daemon.")


def test_issue_527_status_text_mode_prints_the_reason(capsys) -> None:
    """理由存在卻沒有任何 operator 會看的介面印它——正是五個現場的共同症狀。"""

    from paulsha_cortex.porcelain import inspect as porcelain_inspect

    porcelain_inspect._print_status(
        {
            "updated_at": "2026-08-15T00:00:00Z",
            "attention": [
                {
                    "kind": "workflow_run",
                    "run_id": "workflow-abc",
                    "slice_state": "needs_human",
                    "blocking_reason": {
                        "reason": "resume-workflow-failed",
                        "detail": "ValueError: worktree target already exists",
                        "source": "manager_daemon.periodic_tick:resume-workflow",
                        "evidence_refs": ["/tmp/evidence/planning-recovery/x.json"],
                    },
                }
            ],
        }
    )
    out = capsys.readouterr().out
    assert "needs_human[workflow-abc]: resume-workflow-failed" in out
    assert "worktree target already exists" in out
    assert "evidence: /tmp/evidence/planning-recovery/x.json" in out


ACCEPTED_SPEC = """---
status: accepted
work_item: diagnostic-invariant
---

## Requirements

- 一條需求。
"""

BLOCKED_SPEC = """---
status: accepted
work_item: diagnostic-invariant
---

## Requirements

- 一條需求。

## Open Questions

- 要選 A 還是 B？
"""


def _write_run_with_brainstorm_evidence(
    tmp_path: Path, *, spec_text: str
) -> tuple[JobRegistry, WorkflowRun, Path]:
    """造出 #514 的現場：已發佈 artifact ＋ 綁定它的 brainstorm evidence。"""

    import hashlib

    workspace = tmp_path / "workspace"
    coordinator = tmp_path / "coordinator"
    spec_ref = "docs/superpowers/specs/diagnostic-invariant-spec.md"
    spec_path = workspace / spec_ref
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec_text, encoding="utf-8")
    digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()

    evidence_dir = coordinator / "evidence" / "planning"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "brainstorm-fixture.json"
    evidence_payload = {
        "schema_version": 1,
        "kind": "brainstorm-peer",
        "scope": {
            "repo": "hamanpaul/paulsha-cortex",
            "work_id": "diagnostic-invariant",
            "source_revision": "2" * 64,
        },
        "artifacts": [{"kind": "spec", "ref": spec_ref, "sha256": digest}],
    }
    evidence_path.write_text(
        json.dumps(evidence_payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    planner_step = WorkflowStep(
        phase="define",
        persona="planner",
        card="brainstorming",
        executor="codex",
        model="gpt-primary",
        domain="openai",
        inputs=(),
        outputs=("docs/superpowers/specs/*.md",),
        gate_result="pending",
    )
    run = _seed_run(
        registry,
        workspace_root=str(workspace),
        steps=(planner_step,),
        brainstorm_required=True,
    )
    run = registry._manager_update_workflow_run(
        run.run_id,
        gate_refs=(
            manager.GateEvidenceRef(
                "brainstorm",
                str(evidence_path),
                hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            ),
        ),
        planning_source_revision="2" * 64,
    )
    return registry, run, coordinator


def test_issue_514_revalidation_rejection_names_the_ref_and_reasons(tmp_path: Path) -> None:
    """#514 現場：重驗失敗只丟一句 `workflow brainstorm artifact is not accepted`。"""

    _registry, run, coordinator = _write_run_with_brainstorm_evidence(
        tmp_path, spec_text=BLOCKED_SPEC
    )
    with pytest.raises(ValueError) as excinfo:
        manager._validated_brainstorm_planning_authority(run, coordinator_root=coordinator)

    message = str(excinfo.value)
    assert message.startswith("workflow brainstorm artifact is not accepted:")
    # 哪一個 artifact。
    assert "diagnostic-invariant-spec.md" in message
    # 哪一條判準——三種 reason 必須分得開。
    assert "reasons=blocking-decision" in message
    # blocking marker 的行號（#513 已定案的格式）。
    assert "markers=L" in message
    # 被拒內容落 `cortex-planning-artifact-rejection/v1` evidence。
    rejection_dir = coordinator / "evidence" / "planning-artifacts"
    written = sorted(rejection_dir.glob("*.json"))
    assert written, "#514：重驗拒收也要留下可查的完整內容"
    body = json.loads(written[0].read_text(encoding="utf-8"))
    assert body["schema"] == manager.PLANNING_ARTIFACT_REJECTION_SCHEMA
    assert body["reasons"] == ["blocking-decision"]
    assert "要選 A 還是 B？" in body["content"]


def test_issue_514_hash_drift_names_the_ref_and_both_digests(tmp_path: Path) -> None:
    """0814 adversarial review 的修正：「磁碟被改動」其實先在 hash drift 失敗。

    診斷必須做在這條真正會被走到的分支上，而不是只做在走不到的 assessment 上。
    """

    _registry, run, coordinator = _write_run_with_brainstorm_evidence(
        tmp_path, spec_text=ACCEPTED_SPEC
    )
    spec_path = (
        Path(run.workspace_root) / "docs/superpowers/specs/diagnostic-invariant-spec.md"
    )
    spec_path.write_text(ACCEPTED_SPEC + "\n<!-- operator edit -->\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        manager._validated_brainstorm_planning_authority(run, coordinator_root=coordinator)
    message = str(excinfo.value)
    assert message.startswith("workflow brainstorm artifact hash drift:")
    assert "diagnostic-invariant-spec.md" in message
    assert "evidence=" in message and "disk=" in message


def _completeness_report() -> object:
    return assess_planning_completeness(
        [PlanningArtifact(kind="spec", ref="docs/spec.md", text=ACCEPTED_SPEC)]
    )


@pytest.mark.parametrize(
    ("ref", "expected_reason"),
    [
        ("../outside-root.md", "artifact-path-escapes-root"),
        ("/etc/passwd", "artifact-path-escapes-root"),
        ("docs/missing.md", "artifact-not-a-regular-file"),
    ],
)
def test_issue_515_each_branch_keeps_its_own_reason(
    tmp_path: Path, ref: str, expected_reason: str
) -> None:
    """#515 現場：14 個裸 `return None` 塌縮成不透明的 `primary-artifact-invalid`。

    每一條分支現在都必須保有自己的 reason——環境類（路徑／權限／編碼）與內容類
    （assessment 不合格）的處置完全不同，塌縮成同一個值等於把診斷丟掉。
    """

    integration = {
        "resolutions": [
            {
                "question_id": "q-1",
                "decision": "x",
                "artifact_kind": "spec",
                "artifact_refs": [ref],
            }
        ]
    }
    failure = _post_integration_artifact_evidence(
        integration, tmp_path, _completeness_report()
    )
    assert isinstance(failure, ArtifactEvidenceFailure)
    assert failure.reason == expected_reason
    assert ref in failure.rendered()


def test_issue_515_symlink_is_distinguishable_from_content_rejection(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text(ACCEPTED_SPEC, encoding="utf-8")
    link = tmp_path / "docs" / "linked.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)

    integration = {
        "resolutions": [
            {
                "question_id": "q-1",
                "decision": "x",
                "artifact_kind": "spec",
                "artifact_refs": ["docs/linked.md"],
            }
        ]
    }
    failure = _post_integration_artifact_evidence(
        integration, tmp_path, _completeness_report()
    )
    assert isinstance(failure, ArtifactEvidenceFailure)
    assert failure.reason == "artifact-symlink-rejected"


def test_issue_515_assessment_rejection_carries_reasons_markers_and_evidence(
    tmp_path: Path,
) -> None:
    """內容類拒收沿用 #513 的 `(reasons=...; markers=Lnn:...)` 格式與 evidence 落檔。"""

    blocked = tmp_path / "docs" / "blocked.md"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text(BLOCKED_SPEC, encoding="utf-8")

    recorded: list[str] = []

    def recorder(assessment) -> str:
        recorded.append(assessment.artifact.ref)
        return "/tmp/evidence/planning-artifacts/run-abc.json"

    integration = {
        "resolutions": [
            {
                "question_id": "q-1",
                "decision": "x",
                "artifact_kind": "spec",
                "artifact_refs": ["docs/blocked.md"],
            }
        ]
    }
    failure = _post_integration_artifact_evidence(
        integration,
        tmp_path,
        _completeness_report(),
        rejection_recorder=recorder,
    )
    assert isinstance(failure, ArtifactEvidenceFailure)
    assert failure.reason == "artifact-assessment-rejected"
    assert failure.ref == "docs/blocked.md"
    assert "reasons=blocking-decision" in failure.detail
    assert "markers=L" in failure.detail
    assert "evidence=/tmp/evidence/planning-artifacts/run-abc.json" in failure.detail
    assert recorded == ["docs/blocked.md"]


def test_issue_515_no_bare_return_none_remains_in_the_checker() -> None:
    """反模式回歸樁：這個函式裡不得再出現裸 `return None`。

    #397／#408 已為同一類缺陷做過兩輪儀器化；本函式是規模最大的殘留（14 個
    分支）。用 AST 鎖住，避免下一次修改又長回來。
    """

    source = (PACKAGE_ROOT / "coordinator" / "planning.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_post_integration_artifact_evidence"
    )
    bare = [
        node.lineno
        for node in ast.walk(target)
        if isinstance(node, ast.Return)
        and (node.value is None or (isinstance(node.value, ast.Constant) and node.value.value is None))
    ]
    assert bare == [], f"仍有裸 return None（行號 {bare}）——#515 的反模式又長回來了"


def test_issue_511_brainstorm_failure_reason_reaches_the_run(tmp_path: Path) -> None:
    """#511 現場：operator 只拿到路徑、沒有拒收原因，且被拒內容不留存。

    PR #513 已把原因與內容補在 `_publish_planning_artifacts`，但那份結構化理由
    到不了 run——只能靠上游 `str(exc)[:160]` 截斷後的字串殘骸。本測試鎖住
    「理由確實落到 run 上、而且是結構化的」。
    """

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry, brainstorm_required=True)
    evidence_ref = str(tmp_path / "evidence" / "planning-recovery" / "run.json")
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        evidence_refs=(evidence_ref,),
        needs_human_reason=diagnostic_reason(
            "brainstorm-not-ready",
            "brainstorm 未收斂（state=needs_human）：primary-artifact-write-rejected: "
            "ValueError: planning artifact is not accepted: docs/spec.md "
            "(reasons=blocking-decision; markers=L9:要選 A 還是 B？)",
            source="manager.apply_workflow_action:start-brainstorm",
            evidence_refs=(evidence_ref,),
            classification="content",
        ),
    )
    persisted = registry.get_workflow_run(run.run_id)
    reason = persisted.needs_human_reason
    assert reason["reason"] == "brainstorm-not-ready"
    # 三種 reason 必須分得開——`blocking-decision` 代表 planner 刻意標記待裁決
    # 事項，重試只會原地打轉。
    assert "reasons=blocking-decision" in reason["detail"]
    assert reason["evidence_refs"] == [evidence_ref]
    assert reason["context"]["classification"] == "content"


def test_issue_511_reason_is_visible_from_work_show(tmp_path: Path) -> None:
    """理由必須能由 `cortex work show` 曝光，不能只活在 run row 裡。"""

    from paulsha_cortex.monitor.providers import _needs_human_reason_row

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "brainstorm-not-ready",
            "planning artifact is not accepted (reasons=blocking-decision)",
            source="manager.apply_workflow_action:start-brainstorm",
        ),
    )
    row = registry.get_workflow_run(run.run_id).to_dict()
    projected = _needs_human_reason_row(row)
    assert projected is not None
    assert projected["reason"] == "brainstorm-not-ready"
    assert projected["source"].startswith("manager.")

    # 已離開 ongoing 的 run 不得把舊理由帶到面板上。
    row["status"] = "superseded"
    assert _needs_human_reason_row(row) is None


def test_issue_511_workflow_row_still_passes_the_monitor_projection(tmp_path: Path) -> None:
    """新欄位必須列進 monitor 的封閉 whitelist，否則整份 projection 會 degraded。"""

    from paulsha_cortex.monitor.providers import _validate_workflow_v2_row

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id, facets=("needs_human",), needs_human_reason=fixture_needs_human_reason()
    )
    _validate_workflow_v2_row(registry.get_workflow_run(run.run_id).to_dict())


CANDIDATE = "a" * 40


def _absent_payload(reason: str, *, builder=None, reviewer=None) -> dict:
    return review.build_gate_evaluation(
        slice_id="slice-482",
        state="absent",
        reason=reason,
        builder_job_id="builder-1",
        reviewer_job_id=None,
        candidate=CANDIDATE,
        launch_identity={"builder": builder, "reviewer": reviewer},
    )


_BUILDER = {"executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai"}
_REVIEWER = {"executor": "agy", "model_id": "gemini", "independence_domain": "google"}


def test_issue_482_retry_with_a_new_identity_no_longer_collides(tmp_path: Path) -> None:
    """#482 現場：合法的 `missing → unknown → registered` 推進撞 immutable artifact。

    第一次 pre-launch 失敗寫下 `reviewer-identity-missing`；operator 依系統自己
    宣告的 next action 帶新 identity 重試，reviewer 選擇正確地改判
    `reviewer-identity-unknown`——但兩者映到同一個 `...-absent.json`，
    immutable writer 因此 raise，沒有任何 reviewer job 被建立。
    """

    first = review.write_gate_evaluation(
        _absent_payload("reviewer-identity-missing"), coordinator_root=tmp_path
    )
    second = review.write_gate_evaluation(
        _absent_payload("reviewer-identity-unknown", builder=_BUILDER),
        coordinator_root=tmp_path,
    )
    third = review.write_gate_evaluation(
        _absent_payload("same-independence-domain", builder=_BUILDER, reviewer=_REVIEWER),
        coordinator_root=tmp_path,
    )

    paths = {first["path"], second["path"], third["path"]}
    assert len(paths) == 3, "#482：三個不同的 absent 結論不得共用一個落點"
    # 前一份 absent evidence 原位保留——設定推進不該需要刪 evidence 才能前進。
    for row in (first, second, third):
        assert Path(row["path"]).is_file()
    assert json.loads(Path(first["path"]).read_text(encoding="utf-8"))["reason"] == (
        "reviewer-identity-missing"
    )


def test_issue_482_same_reason_and_identity_stays_idempotent(tmp_path: Path) -> None:
    """同原因＋同身分仍必須是冪等重寫，既有語意一字不改。"""

    first = review.write_gate_evaluation(
        _absent_payload("reviewer-identity-missing"), coordinator_root=tmp_path
    )
    again = review.write_gate_evaluation(
        _absent_payload("reviewer-identity-missing"), coordinator_root=tmp_path
    )
    assert first["path"] == again["path"]
    assert first["hash"] == again["hash"]


def test_issue_482_absent_key_carries_reason_and_identity() -> None:
    """key 的輸入就是過去被排除在外、因而造成碰撞的兩項東西。"""

    missing = review.absent_evaluation_key(
        reason="reviewer-identity-missing", launch_identity={"builder": None, "reviewer": None}
    )
    unknown = review.absent_evaluation_key(
        reason="reviewer-identity-unknown", launch_identity={"builder": None, "reviewer": None}
    )
    same_reason_other_identity = review.absent_evaluation_key(
        reason="reviewer-identity-missing",
        launch_identity={"builder": _BUILDER, "reviewer": None},
    )
    assert len({missing, unknown, same_reason_other_identity}) == 3
    assert len(missing) == review.ABSENT_EVALUATION_KEY_LENGTH


def test_issue_482_reviewer_job_keyed_path_is_unchanged(tmp_path: Path) -> None:
    """範圍紀律：reviewer job 已存在時的落點一字未動。"""

    path = review.gate_evaluation_path(
        slice_id="slice-482",
        builder_job_id="builder-1",
        candidate=CANDIDATE,
        reviewer_job_id="reviewer-9",
        coordinator_root=tmp_path,
    )
    assert path.name == "slice-482-reviewer-9.json"


def test_issue_527_provider_projects_the_reason_without_degrading(tmp_path: Path) -> None:
    """理由必須能過 monitor 的 workflow projection，且不得讓 provider degraded。

    #205 曾因為新增 WorkflowRun 欄位而讓每一個 row 落在 optional-key 白名單之外、
    整份 workflow projection 變 degraded（#261 D5 因此選擇不新增欄位）。本測試
    同時鎖住兩件事：理由確實被投影出來，以及 provider 仍為 ok、run 仍在。
    """

    from paulsha_cortex.monitor.providers import WorkflowRegistryProvider

    state = tmp_path / "jobs.json"
    registry = JobRegistry(state_path=state)
    run = _seed_run(registry)
    registry._manager_update_workflow_run(
        run.run_id,
        facets=("needs_human",),
        needs_human_reason=diagnostic_reason(
            "resume-workflow-failed",
            "ValueError: worktree target already exists",
            source="manager_daemon.periodic_tick:resume-workflow",
        ),
    )

    result = WorkflowRegistryProvider("hamanpaul/paulsha-cortex", state_path=state).scan()

    assert result.status == "ok"
    assert result.diagnostics == ()
    assert [source.ref for source in result.sources] == [run.run_id]
    projected = result.observations["needs_human_reasons"]["diagnostic-invariant"]
    assert projected["run_id"] == run.run_id
    assert projected["reason"] == "resume-workflow-failed"
    assert projected["detail"] == "ValueError: worktree target already exists"
    assert projected["source"] == "manager_daemon.periodic_tick:resume-workflow"


def test_issue_527_work_show_text_mode_prints_the_reason(capsys) -> None:
    """`cortex work show` 過去只印 work_id/state/title/repo/phase。"""

    from paulsha_cortex import cli

    class _FakeClient:
        def request(self, request):
            assert request["kind"] == "get_work_item"
            return {
                "ok": True,
                "data": {
                    "item": {
                        "work_id": "diagnostic-invariant",
                        "state": "on-going",
                        "title": "診斷 invariant",
                        "repo": "hamanpaul/paulsha-cortex",
                        "phase": "build",
                    },
                    "blocking_reason": {
                        "run_id": "workflow-abc",
                        "reason": "resume-workflow-failed",
                        "detail": "ValueError: worktree target already exists",
                        "source": "manager_daemon.periodic_tick:resume-workflow",
                        "evidence_refs": ["/tmp/evidence/planning-recovery/x.json"],
                    },
                },
            }

    assert cli._work_read_main(
        ["work", "show", "diagnostic-invariant"], work_client=_FakeClient()
    ) == 0
    out = capsys.readouterr().out
    assert "needs_human: resume-workflow-failed" in out
    assert "worktree target already exists" in out
    assert "run_id: workflow-abc" in out
    assert "evidence: /tmp/evidence/planning-recovery/x.json" in out
