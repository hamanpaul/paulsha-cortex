from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from paulsha_cortex.control import contract
from paulsha_cortex.coordinator import cli as coordinator_cli
from paulsha_cortex.coordinator import manager as coordinator_manager
from paulsha_cortex.coordinator import work_actions as coordinator_work_actions
from paulsha_cortex.coordinator import work_bridge
from paulsha_cortex.coordinator.claim import canonical_work_snapshot_path, load_work_authority
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowRun, WorkflowStep
from paulsha_cortex.monitor import providers


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:acme/demo.git"],
        check=True,
    )
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return root


def _write_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    titles: dict[int, str | None] | None,
) -> Path:
    monitor_root = tmp_path / "monitor"
    monitor_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(monitor_root))
    snapshot = canonical_work_snapshot_path()

    provider = {
        "provider_id": "github:acme/demo",
        "status": "ok",
        "last_attempt_at": "2026-08-04T00:00:00Z",
        "last_success_at": "2026-08-04T00:00:00Z",
        "revision": "gh-1",
        "diagnostics": [],
        "sources": [],
        "observations": {},
    }
    sources = [
        {
            "source_id": "openspec:acme/demo:work",
            "kind": "openspec",
            "ref": "work",
            "revision": "spec-1",
            "status": "active",
            "confidence": "confirmed",
            "provider": "repo:acme/demo",
        }
    ]
    for issue_number, title in sorted((titles or {}).items()):
        source = {
            "source_id": f"github_issue:acme/demo#{issue_number}",
            "kind": "github_issue",
            "ref": f"acme/demo#{issue_number}",
            "revision": f"issue-{issue_number}",
            "status": "open",
            "confidence": "confirmed",
            "provider": "github:acme/demo",
        }
        if title is not None:
            source["title"] = title
        sources.insert(0, source)

    snapshot.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {"github:acme/demo": provider},
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "work",
                        "next_actions": ["start"],
                        "sources": sources,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def _step() -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card="subagent-build",
        executor="codex",
        model="gpt-5.4",
        domain="openai",
        inputs=(),
        outputs=(),
        gate_result="pending",
    )


def _workflow_run(*, combo_selection: dict[str, object] | None) -> WorkflowRun:
    return WorkflowRun(
        run_id="workflow-" + "a" * 20,
        work_id="combo-selector",
        repo="acme/demo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root="/tmp/workspace",
        combo="feature-oneshot",
        current_phase="build",
        steps=(_step(),),
        issue_refs=("acme/demo#202",),
        openspec_refs=("work",),
        pr_refs=(),
        attempts={"build": 1},
        evidence_refs=(),
        gate_refs=(),
        brainstorm_required=False,
        primary_domain=None,
        candidate_head=None,
        verified_head=None,
        facets=(),
        gate_status="pending",
        created_at="2026-08-04T00:00:00+00:00",
        updated_at="2026-08-04T00:00:00+00:00",
        combo_selection=combo_selection,
    )


def test_start_canonical_workflow_records_combo_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _repo(tmp_path / "repo")
    snapshot = _write_snapshot(
        tmp_path,
        monkeypatch,
        titles={202: "fix(deck): tighten selector wiring"},
    )
    authority = load_work_authority(repo="acme/demo", work_id="work", snapshot_path=snapshot)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    run = work_bridge.start_canonical_workflow(
        registry=registry,
        authority=authority,
        claim_key="claim:v1:" + "1" * 64,
        coordinator_root=tmp_path / "coordinator",
        explicit_repo_root=workspace,
        needs_human_reason="missing_issue",
    )

    assert run.combo == "fix-standard"
    assert run.combo_selection == {
        "source": "task-type-auto",
        "task_type": "fix",
        "combo": "fix-standard",
        "reason": run.combo_selection["reason"],
    }


def test_start_canonical_workflow_bypass_marker_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _repo(tmp_path / "repo")
    snapshot = _write_snapshot(tmp_path, monkeypatch, titles={})
    authority = load_work_authority(repo="acme/demo", work_id="work", snapshot_path=snapshot)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    run = work_bridge.start_canonical_workflow(
        registry=registry,
        authority=authority,
        claim_key="claim:v1:" + "2" * 64,
        coordinator_root=tmp_path / "coordinator",
        explicit_repo_root=workspace,
        needs_human_reason="missing_issue",
    )

    assert run.combo == "feature-oneshot"
    assert run.combo_selection["source"] == "bypass-default"


def test_workflow_run_combo_selection_roundtrip() -> None:
    payload = _workflow_run(
        combo_selection={
            "source": "task-type-auto",
            "task_type": "fix",
            "combo": "fix-standard",
            "reason": "title matched fix",
        }
    ).to_dict()

    assert payload["combo_selection"] == {
        "source": "task-type-auto",
        "task_type": "fix",
        "combo": "fix-standard",
        "reason": "title matched fix",
    }
    assert WorkflowRun.from_dict(payload).combo_selection == payload["combo_selection"]

    legacy = dict(payload)
    legacy.pop("combo_selection")
    assert WorkflowRun.from_dict(legacy).combo_selection is None


def test_providers_projection_not_degraded_with_combo_selection() -> None:
    providers._validate_workflow_v2_row(
        {
            "run_id": "workflow-" + "a" * 20,
            "repo": "acme/demo",
            "work_id": "work",
            "status": "ongoing",
            "issue_refs": ["acme/demo#202"],
            "openspec_refs": ["work"],
            "pr_refs": [],
            "combo_selection": {
                "source": "task-type-auto",
                "task_type": "fix",
                "combo": "fix-standard",
                "reason": "title matched fix",
            },
        }
    )


def test_stat_combo_selections_aggregation(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    rows = [
        {
            "source": "task-type-auto",
            "task_type": "fix",
            "combo": "fix-standard",
            "reason": "title matched fix",
        },
        {
            "source": "task-type-auto",
            "task_type": "fix",
            "combo": "fix-standard",
            "reason": "title matched fix",
        },
        {
            "source": "bypass-default",
            "task_type": None,
            "combo": "feature-oneshot",
            "reason": "absent title",
        },
        None,
    ]
    for idx, combo_selection in enumerate(rows):
        registry._manager_create_workflow_run(
            repo="hamanpaul/paulsha-cortex",
            work_id=f"combo-agg-{idx}",
            claim_key=f"claim:v1:{str(idx) * 64}",
            source_revision="rev-agg",
            workspace_root="/tmp/workspace",
            combo="feature-oneshot",
            current_phase="build",
            steps=(_step(),),
            issue_refs=(f"hamanpaul/paulsha-cortex#{900 + idx}",),
            combo_selection=combo_selection,
        )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = coordinator_cli.main(["stat", "--combo-selections"], registry=registry)

    assert exit_code == 0
    payload = json.loads(buffer.getvalue())
    assert payload == {
        "combo_selections": {
            "task-type-auto": {"fix": 2},
            "bypass-default": {"unrecorded": 1},
            "unrecorded": {"unrecorded": 1},
        }
    }


@pytest.mark.parametrize("combo", ["", "Fix-Standard", "bad/slash", "bad space"])
def test_contract_validates_start_combo_arg(combo: str) -> None:
    request = contract.build_request(
        req_type="work-action",
        args={
            "action": "start",
            "repo": "acme/demo",
            "work_id": "work",
            "combo": combo,
        },
        requested_by="operator",
    )

    with pytest.raises(ValueError, match="work-action start combo invalid"):
        contract.validate_request(request)

    valid = contract.build_request(
        req_type="work-action",
        args={
            "action": "start",
            "repo": "acme/demo",
            "work_id": "work",
            "combo": "fix-standard",
        },
        requested_by="operator",
    )
    assert contract.validate_request(valid)["args"]["combo"] == "fix-standard"


@pytest.mark.parametrize(
    "action",
    ["resume", "auto", "ship", "abandon", "recover-planning", "review-attest"],
)
def test_contract_fail_closed_rejects_combo_on_non_start_action(action: str) -> None:
    """R3／code review finding：combo override 只在 start 有意義。

    resume 等 action 若夾帶 combo（不論來自 CLI／porcelain 疏漏或 --payload
    繞過），contract 這個所有入口的收斂點必須 fail-closed 拒絕，不得讓未經
    驗證的 combo 有機會流到 ``apply_work_action``／
    ``start_canonical_workflow``。
    """

    request = contract.build_request(
        req_type="work-action",
        args={
            "action": action,
            "repo": "acme/demo",
            "work_id": "work",
            "combo": "fix-standard",
        },
        requested_by="operator",
    )

    with pytest.raises(ValueError, match=f"work-action {action} must not include combo"):
        contract.validate_request(request)


def test_contract_fail_closed_rejects_combo_on_retry_build_even_with_valid_candidate() -> None:
    request = contract.build_request(
        req_type="work-action",
        args={
            "action": "retry-build",
            "repo": "acme/demo",
            "work_id": "work",
            "expected_candidate": "a" * 40,
            "combo": "fix-standard",
        },
        requested_by="operator",
    )

    with pytest.raises(ValueError, match="work-action retry-build must not include combo"):
        contract.validate_request(request)


def test_apply_work_action_drops_combo_override_for_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manager 層縱深防禦：即使 args 帶了 combo（不論是誰的疏漏），resume 也
    絕不能把它轉交 ``start_canonical_workflow``——只有 ``action == "start"``
    才允許（見 code review finding）。
    """

    captured: dict[str, object] = {}

    def fake_start_canonical_workflow(**kwargs):
        captured["combo_override"] = kwargs.get("combo_override")
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(work_bridge, "start_canonical_workflow", fake_start_canonical_workflow)

    def fake_execute_work_action(*, args, requested_by, workflow_registry, workflow_starter):
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            workflow_starter(None, "claim:v1:" + "1" * 64, None)
        return {"action": "captured"}

    monkeypatch.setattr(coordinator_work_actions, "execute_work_action", fake_execute_work_action)

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    result = coordinator_manager.apply_work_action(
        args={
            "action": "resume",
            "repo": "acme/demo",
            "work_id": "work",
            "combo": "fix-standard",
        },
        requested_by="operator",
        registry=registry,
    )

    assert result == {"action": "captured"}
    assert captured["combo_override"] is None


def test_apply_work_action_forwards_combo_override_for_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_start_canonical_workflow(**kwargs):
        captured["combo_override"] = kwargs.get("combo_override")
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(work_bridge, "start_canonical_workflow", fake_start_canonical_workflow)

    def fake_execute_work_action(*, args, requested_by, workflow_registry, workflow_starter):
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            workflow_starter(None, "claim:v1:" + "1" * 64, None)
        return {"action": "captured"}

    monkeypatch.setattr(coordinator_work_actions, "execute_work_action", fake_execute_work_action)

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    coordinator_manager.apply_work_action(
        args={
            "action": "start",
            "repo": "acme/demo",
            "work_id": "work",
            "combo": "fix-standard",
        },
        requested_by="operator",
        registry=registry,
    )

    assert captured["combo_override"] == "fix-standard"


def test_apply_work_action_forwards_combo_override_for_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """issue #203：intake 內部等價於 start，manager 層縱深防禦也要放行
    combo（見 test_apply_work_action_forwards_combo_override_for_start 的
    對稱案例、test_apply_work_action_drops_combo_override_for_resume 的反例）。
    """

    captured: dict[str, object] = {}

    def fake_start_canonical_workflow(**kwargs):
        captured["combo_override"] = kwargs.get("combo_override")
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(work_bridge, "start_canonical_workflow", fake_start_canonical_workflow)

    def fake_execute_work_action(*, args, requested_by, workflow_registry, workflow_starter):
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            workflow_starter(None, "claim:v1:" + "1" * 64, None)
        return {"action": "captured"}

    monkeypatch.setattr(coordinator_work_actions, "execute_work_action", fake_execute_work_action)

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    coordinator_manager.apply_work_action(
        args={
            "action": "intake",
            "repo": "acme/demo",
            "work_id": "work",
            "combo": "fix-standard",
        },
        requested_by="operator",
        registry=registry,
    )

    assert captured["combo_override"] == "fix-standard"
