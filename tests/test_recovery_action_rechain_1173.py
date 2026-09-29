from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from paulsha_cortex.coordinator import manager, work_actions
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.model_identities import load_model_identities
from paulsha_cortex.coordinator.workflow import WorkflowStep


REPO = "acme/demo"
WORK_ID = "demo"
RUN_ID = "workflow-" + "a" * 20
ERA = "claim:v1:" + "c" * 64
CANDIDATE = "b" * 40
CHAIN_ARGS = {
    "planner_executor": "claude",
    "planner_model": "sonnet",
    "builder_executor": "claude",
    "builder_model": "sonnet",
    "reviewer_executor": "agy",
    "reviewer_model": "gemini-3.1-pro-high",
}


def _authority():
    return SimpleNamespace(
        repo=REPO,
        work_id=WORK_ID,
        mapped_issues=(12,),
        mapped_openspec=("demo",),
    )


def _registry(tmp_path: Path, **overrides):
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    values = {
        "work_id": WORK_ID,
        "repo": REPO,
        "claim_key": ERA,
        "source_revision": "d" * 64,
        "workspace_root": str(tmp_path / "workspace"),
        "combo": "feature-oneshot",
        "current_phase": "build",
        "steps": (
            WorkflowStep(
                phase="build", persona="builder", card="implement",
                executor=None, model=None, domain=None, inputs=(), outputs=(),
                commit_policy="required", test_policy="focused", gate_result="pending",
            ),
        ),
        "issue_refs": (f"{REPO}#12",),
        "openspec_refs": ("demo",),
        "pr_refs": (),
        "attempts": {"build": 1},
        "gate_status": "running",
        "facets": ("needs_human",),
        "candidate_head": CANDIDATE,
        "needs_human_reason": diagnostic_reason(
            "builder-timeout", "builder stopped before reviewer dispatch",
            source="tests.test_recovery_action_rechain_1173",
        ),
    }
    values.update(overrides)
    if "needs_human" not in values["facets"]:
        values.pop("needs_human_reason", None)
    resolved = values.pop("resolved_model_chain", None)
    status = values.pop("status", "ongoing")
    run = registry._manager_create_workflow_run(**values)
    if resolved is not None:
        run = registry._manager_update_workflow_run(
            run.run_id, resolved_model_chain=resolved
        )
    if status != run.status:
        index = registry._find_workflow_run_index(run.run_id)
        registry._workflows[index] = replace(registry._workflows[index], status=status)
        run = registry.get_workflow_run(run.run_id)
    return registry, run


def _action_args(run, **overrides):
    args = {
        "action": "rechain",
        "repo": REPO,
        "work_id": WORK_ID,
        "actor": "operator",
        "reason": "#831 same-domain reviewer pin; select independent identities",
        "expected_run_id": run.run_id,
        "expected_candidate": CANDIDATE,
        "expected_era": ERA,
        **CHAIN_ARGS,
    }
    args.update(overrides)
    return args


def _invoke(tmp_path, registry, run, *, authority=None, **overrides):
    return work_actions._rechain_action(
        args=_action_args(run, **overrides),
        authority=authority or _authority(),
        now_epoch=1_790_000_000,
        state_path=tmp_path / "state.json",
        workflow_registry=registry,
    )


def _audit_files(tmp_path):
    root = tmp_path / "evidence" / "work-model-chain-readjudication"
    return sorted(path.name for path in root.glob("*.json")) if root.exists() else []


def test_rechain_positive_updates_all_pins_and_keeps_old_resolved_evidence(tmp_path):
    registry, run = _registry(
        tmp_path,
        current_phase="review",
        steps=(
            WorkflowStep(
                phase="build", persona="builder", card="implement",
                executor="codex", model="gpt-5.3-codex-spark", domain="openai",
                inputs=(), outputs=(), commit_policy="required",
                test_policy="focused", gate_result="passed",
            ),
        ),
        model_chain_override={
            "planner": {"executor": "claude", "model_id": "sonnet"},
            "builder": {"executor": "codex", "model_id": "gpt-5.3-codex-spark"},
            "reviewer": {"executor": "codex", "model_id": "gpt-5.3-codex-spark"},
        },
        resolved_model_chain={
            "builder": {
                "executor": "codex", "model_id": "gpt-5.3-codex-spark",
                "independence_domain": "openai", "source": "run-override",
            }
        },
    )
    identities_before = load_model_identities()
    with pytest.raises(ValueError, match="independence_domain"):
        manager._workflow_identity_candidates_for_persona(run, "reviewer", identities_before)
    result = _invoke(tmp_path, registry, run)
    updated = registry.get_workflow_run(run.run_id)

    assert result["action"] == "rechain"
    assert updated.model_chain_override == {
        "planner": {"executor": "claude", "model_id": "sonnet"},
        "builder": {"executor": "claude", "model_id": "sonnet"},
        "reviewer": {"executor": "agy", "model_id": "gemini-3.1-pro-high"},
    }
    assert updated.resolved_model_chain == run.resolved_model_chain
    assert updated.candidate_head == run.candidate_head
    assert updated.current_phase == run.current_phase
    assert "needs_human" not in updated.facets
    assert "blocked" not in updated.facets
    assert updated.gate_status == "running"
    assert updated.evidence_refs[:-1] == run.evidence_refs
    assert len(registry.list_jobs()) == 0
    assert len(_audit_files(tmp_path)) == 1
    identities = load_model_identities()
    builder = manager._workflow_identity_candidates_for_persona(updated, "builder", identities)
    reviewer = manager._workflow_identity_candidates_for_persona(updated, "reviewer", identities)
    assert [(item.executor, item.model_id) for item in builder] == [("claude", "sonnet")]
    assert [(item.executor, item.model_id) for item in reviewer] == [
        ("agy", "gemini-3.1-pro-high")
    ]


def test_rechain_accepts_explicit_none_candidate_cas_when_candidate_is_absent(tmp_path):
    registry, run = _registry(tmp_path, candidate_head=None)
    result = _invoke(tmp_path, registry, run, expected_candidate="none")
    assert result["expected_candidate"] == "none"
    assert registry.get_workflow_run(run.run_id).candidate_head is None
    assert len(_audit_files(tmp_path)) == 1


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("expected_run_id", "workflow-" + "f" * 20, "expected WorkflowRun CAS"),
        ("expected_candidate", "e" * 40, "expected Candidate CAS"),
        ("expected_era", "claim:v1:" + "f" * 64, "claim-era CAS"),
        ("actor", "", "bounded actor"),
        ("reason", "bad\nreason", "bounded reason"),
        ("reviewer_model", "missing", "identity failed qualification/pin"),
    ],
)
def test_rechain_rejects_bad_authority_inputs_without_side_effects(
    tmp_path, field, value, match
):
    registry, run = _registry(tmp_path)
    before = registry.get_workflow_run(run.run_id).to_dict()
    with pytest.raises((ValueError, RuntimeError), match=match):
        _invoke(tmp_path, registry, run, **{field: value})
    assert registry.get_workflow_run(run.run_id).to_dict() == before
    assert _audit_files(tmp_path) == []
    assert registry.list_jobs() == []


@pytest.mark.parametrize(
    "field",
    [
        "planner_executor", "planner_model", "builder_executor", "builder_model",
        "reviewer_executor", "reviewer_model",
    ],
)
def test_rechain_rejects_each_missing_persona_pin_without_side_effects(tmp_path, field):
    registry, run = _registry(tmp_path)
    before = registry.get_workflow_run(run.run_id).to_dict()
    with pytest.raises(ValueError, match="complete .* identity pin"):
        _invoke(tmp_path, registry, run, **{field: None})
    assert registry.get_workflow_run(run.run_id).to_dict() == before
    assert _audit_files(tmp_path) == []


@pytest.mark.parametrize(
    ("run_overrides", "job", "match"),
    [
        ({"facets": ()}, None, "needs_human"),
        ({"status": "superseded"}, None, "active canonical"),
        ({"current_phase": "define"}, None, "attempt boundary"),
        ({"pr_refs": ("acme/demo#5",)}, None, "delivery artifacts"),
        ({}, {"job_id": "job-active", "status": "running"}, "active workflow job"),
    ],
)
def test_rechain_rejects_unsafe_attempt_boundaries_without_side_effects(
    tmp_path, run_overrides, job, match
):
    registry, run = _registry(tmp_path, **run_overrides)
    if job:
        registry._jobs.append({**job, "workflow_run_id": run.run_id})
    before = registry.get_workflow_run(run.run_id).to_dict()
    with pytest.raises(RuntimeError, match=match):
        _invoke(tmp_path, registry, run)
    assert registry.get_workflow_run(run.run_id).to_dict() == before
    assert _audit_files(tmp_path) == []


def test_rechain_rechecks_reviewer_independence_before_recording_audit(tmp_path):
    registry, run = _registry(
        tmp_path,
        steps=(
            WorkflowStep(
                phase="build", persona="builder", card="implement",
                executor=None, model=None, domain="anthropic", inputs=(), outputs=(),
                commit_policy="required", test_policy="focused", gate_result="passed",
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="reviewer identity failed qualification/pin"):
        _invoke(
            tmp_path,
            registry,
            run,
            reviewer_executor="claude",
            reviewer_model="sonnet",
        )
    assert registry.get_workflow_run(run.run_id).to_dict() == run.to_dict()
    assert _audit_files(tmp_path) == []


def test_rechain_requires_work_authority_for_the_mapped_issue(tmp_path):
    registry, run = _registry(tmp_path)
    before = registry.get_workflow_run(run.run_id).to_dict()
    with pytest.raises(RuntimeError, match="issue is not authorized"):
        _invoke(tmp_path, registry, run, issue=99)
    assert registry.get_workflow_run(run.run_id).to_dict() == before
    assert _audit_files(tmp_path) == []


def test_rechain_replay_is_idempotent_and_does_not_rewrite_resolved_chain(tmp_path):
    registry, run = _registry(tmp_path)
    first = _invoke(tmp_path, registry, run)
    after_first = registry.get_workflow_run(run.run_id)
    second = _invoke(tmp_path, registry, after_first)

    assert first["evidence"]["ref"] == second["evidence"]["ref"]
    assert second["already_applied"] is True
    assert registry.get_workflow_run(run.run_id).evidence_refs == after_first.evidence_refs
    assert len(_audit_files(tmp_path)) == 1


def test_rechain_late_replay_does_not_replace_a_newer_pin_or_candidate(tmp_path):
    registry, run = _registry(tmp_path)
    _invoke(tmp_path, registry, run)
    applied = registry.get_workflow_run(run.run_id)
    newer_pin = {
        "planner": {"executor": "claude", "model_id": "sonnet"},
        "builder": {"executor": "claude", "model_id": "sonnet"},
        "reviewer": {"executor": "codex", "model_id": "gpt-5.3-codex-spark"},
    }
    advanced = replace(
        applied,
        current_phase="review",
        candidate_head="e" * 40,
        model_chain_override=newer_pin,
    )
    registry._workflows[registry._find_workflow_run_index(run.run_id)] = advanced

    result = _invoke(tmp_path, registry, advanced)
    current = registry.get_workflow_run(run.run_id)
    assert result["already_applied"] is True
    assert current.model_chain_override == newer_pin
    assert current.candidate_head == "e" * 40
    assert len(_audit_files(tmp_path)) == 1


def test_rechain_audit_written_before_registry_cas_can_be_replayed_after_crash(
    tmp_path, monkeypatch
):
    registry, run = _registry(tmp_path)
    original_update = registry._manager_rechain_workflow

    def crash_before_state_commit(*args, **kwargs):
        raise RuntimeError("simulated crash before registry transition")

    monkeypatch.setattr(registry, "_manager_rechain_workflow", crash_before_state_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _invoke(tmp_path, registry, run)
    assert registry.get_workflow_run(run.run_id).model_chain_override is None
    assert len(_audit_files(tmp_path)) == 1

    monkeypatch.setattr(registry, "_manager_rechain_workflow", original_update)
    _invoke(tmp_path, registry, run)
    assert registry.get_workflow_run(run.run_id).model_chain_override is not None
    assert len(_audit_files(tmp_path)) == 1


def test_rechain_explicit_pin_remains_single_candidate_without_automatic_fallback(tmp_path):
    from dataclasses import replace
    from paulsha_cortex.coordinator.model_identities import load_model_identities

    registry, run = _registry(tmp_path)
    pinned = replace(
        run,
        model_chain_override={
            "builder": {"executor": "claude", "model_id": "sonnet"},
            "reviewer": {"executor": "agy", "model_id": "gemini-3.1-pro-high"},
        },
    )
    identities = load_model_identities()
    builder = manager._workflow_identity_candidates_for_persona(pinned, "builder", identities)
    reviewer = manager._workflow_identity_candidates_for_persona(pinned, "reviewer", identities)
    assert [(item.executor, item.model_id) for item in builder] == [("claude", "sonnet")]
    assert [(item.executor, item.model_id) for item in reviewer] == [
        ("agy", "gemini-3.1-pro-high")
    ]


def test_rechain_rejects_a_reviewer_sharing_the_new_builder_domain(tmp_path):
    """#1173 審查：dispatcher 只比 reviewer 與歷史 builder 的 domain；新的 builder
    pin 也必須與 reviewer 獨立，否則重裁後 build 與 review 落在同一 domain。"""

    registry, run = _registry(tmp_path)
    before = registry.get_workflow_run(run.run_id)

    with pytest.raises(RuntimeError, match="not independent of the new builder pin"):
        _invoke(
            tmp_path,
            registry,
            run,
            reviewer_executor="claude",
            reviewer_model="sonnet",
        )

    assert registry.get_workflow_run(run.run_id) == before
    assert _audit_files(tmp_path) == []


def test_rechain_stale_replay_after_crash_cannot_overwrite_a_later_readjudication(
    tmp_path, monkeypatch
):
    """#1173 審查：A 寫完 audit 後、registry 更新前 crash；B 之後成功，B 的鏈又回到
    needs_human。重送 A 仍通過 run／candidate／era CAS，但不得覆寫 B 的 pin。"""

    registry, run = _registry(tmp_path)
    original_update = registry._manager_rechain_workflow

    def crash_before_state_commit(*args, **kwargs):
        raise RuntimeError("simulated crash before registry transition")

    monkeypatch.setattr(registry, "_manager_rechain_workflow", crash_before_state_commit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _invoke(tmp_path, registry, run)
    monkeypatch.setattr(registry, "_manager_rechain_workflow", original_update)

    later = _invoke(
        tmp_path,
        registry,
        registry.get_workflow_run(run.run_id),
        reason="operator B chose a codex builder",
        builder_executor="codex",
        builder_model="gpt-5.3-codex-spark",
    )
    assert later["already_applied"] is False
    applied = registry.get_workflow_run(run.run_id)
    b_chain = applied.model_chain_override
    index = registry._find_workflow_run_index(run.run_id)
    registry._workflows[index] = replace(
        applied,
        facets=tuple(dict.fromkeys((*applied.facets, "needs_human"))),
        needs_human_reason=run.needs_human_reason,
    )

    with pytest.raises(RuntimeError, match="superseded by a later readjudication"):
        _invoke(tmp_path, registry, registry.get_workflow_run(run.run_id))

    assert registry.get_workflow_run(run.run_id).model_chain_override == b_chain
