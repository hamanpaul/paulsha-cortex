"""RED coverage for the blocking-findings operator adjudication exit (#956)."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import cli as coordinator_cli
from paulsha_cortex.coordinator import manager, review as foreign_review, work_actions
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import (
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowStep,
)

from diagnostic_fixtures import fixture_needs_human_reason


HEAD = "b" * 40
REPO = "acme/demo"
WORK_ID = "demo"
PLAN_REF = "docs/superpowers/plans/review-gate-adjudication-exit.md"
PLAN_TEXT = "# accepted review-gate adjudication plan\n"

_PERSONA_BY_PHASE = {
    "claim": "manager",
    "define": "planner",
    "plan": "planner",
    "build": "builder",
    "verify": "reviewer",
    "review": "reviewer",
    "ship": "manager",
}


def _step(phase: str, card: str, *, gate_result: str = "pending") -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=_PERSONA_BY_PHASE[phase],
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result=gate_result,
    )


def _base_steps(*, verify_result: str = "passed", review_result: str = "needs_human"):
    return (
        _step("claim", "manager-claim", gate_result="passed"),
        _step("define", "planner-define", gate_result="passed"),
        _step("plan", "planner-plan", gate_result="passed"),
        _step("build", "subagent-build", gate_result="passed"),
        _step("verify", "verification", gate_result=verify_result),
        _step("review", "code-review", gate_result=review_result),
        _step("ship", "manager-ship"),
    )


def _init_repo(root: Path, repo: str = REPO) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{repo}.git"],
            check=True,
        )


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
                        "repo": REPO,
                        "work_id": WORK_ID,
                        "mapped_issues": [12],
                        "mapped_prs": [8],
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


def _plan_authority() -> tuple[PlanningArtifactAuthority, ...]:
    import hashlib

    return (
        PlanningArtifactAuthority(
            ref=PLAN_REF,
            kind="plan",
            work_id=WORK_ID,
            baseline_sha256=hashlib.sha256(PLAN_TEXT.encode()).hexdigest(),
        ),
    )


def _review_fixture(
    tmp_path: Path,
    *,
    reason: str = "blocking-findings",
    candidate_head: str | None = HEAD,
    verified_head: str | None = HEAD,
    plan_authority: bool = True,
    with_foreign_review: bool = False,
):
    snapshot = _snapshot(tmp_path / "snapshot.json")
    authority = work_actions.load_work_authority(
        repo=REPO, work_id=WORK_ID, snapshot_path=snapshot
    )
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    gate_refs = (
        (GateEvidenceRef("foreign-review", "/evidence/foreign-review.json", "f" * 64),)
        if with_foreign_review
        else ()
    )
    run = registry._manager_create_workflow_run(
        work_id=authority.work_id,
        repo=authority.repo,
        claim_key=work_actions._expected_claim_key(authority),
        source_revision=work_actions.work_authority_digest(authority),
        workspace_root=str(tmp_path / "workspace"),
        combo="feature-oneshot",
        current_phase="review",
        steps=_base_steps(),
        issue_refs=tuple(f"{authority.repo}#{number}" for number in authority.mapped_issues),
        openspec_refs=authority.mapped_openspec,
        pr_refs=tuple(f"{authority.repo}#{number}" for number in authority.mapped_prs),
        candidate_head=candidate_head,
        verified_head=verified_head,
        facets=("needs_human",),
        gate_status="failed",
        gate_refs=gate_refs,
        planning_authority=_plan_authority() if plan_authority else (),
        needs_human_reason=fixture_needs_human_reason(
            reason,
            "review gate rejected a candidate with blocking findings",
        ),
    )
    return snapshot, authority, registry, run


def _retry_review(
    tmp_path: Path,
    *,
    reason: str | None = None,
    expected_candidate: str = HEAD,
):
    snapshot, _authority, registry, run = _review_fixture(tmp_path)
    state_path = tmp_path / "runs.json"
    args = {
        "action": "retry-review",
        "repo": REPO,
        "work_id": WORK_ID,
        "issue": 12,
        "actor": "operator",
        "expected_candidate": expected_candidate,
    }
    if reason is not None:
        args["reason"] = reason
    result = work_actions.execute_work_action(
        args=args,
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state_path,
        workflow_registry=registry,
        now=lambda: 1_758_625_447.25,
    )
    return result["result"], registry, run, state_path


@pytest.mark.parametrize("reason", ["   ", "x" * 4001, 123])
def test_retry_review_rejects_invalid_reason_without_side_effects(
    tmp_path: Path, reason: object
) -> None:
    snapshot, _authority, registry, run = _review_fixture(tmp_path)
    state_path = tmp_path / "runs.json"
    with pytest.raises(ValueError) as caught:
        work_actions.execute_work_action(
            args={
                "action": "retry-review",
                "repo": REPO,
                "work_id": WORK_ID,
                "issue": 12,
                "actor": "operator",
                "expected_candidate": HEAD,
                "reason": reason,
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state_path,
            workflow_registry=registry,
        )

    assert str(caught.value) == (
        "retry-review reason must be a non-empty string within 4000 characters"
    )
    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.facets == ("needs_human",)
    assert next(step for step in unchanged.steps if step.phase == "review").gate_result == "needs_human"
    assert not (tmp_path / "evidence" / "operator-adjudication").exists()


def test_retry_review_reason_does_not_write_evidence_on_candidate_cas_mismatch(
    tmp_path: Path,
) -> None:
    snapshot, _authority, registry, _run = _review_fixture(tmp_path)
    state_path = tmp_path / "runs.json"
    with pytest.raises(RuntimeError, match="expected Candidate CAS mismatch"):
        work_actions.execute_work_action(
            args={
                "action": "retry-review",
                "repo": REPO,
                "work_id": WORK_ID,
                "issue": 12,
                "actor": "operator",
                "expected_candidate": "c" * 40,
                "reason": "accept the documented scope-bypass finding",
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=state_path,
            workflow_registry=registry,
        )
    assert not (tmp_path / "evidence" / "operator-adjudication").exists()


def test_retry_review_reason_requires_durable_state_path(tmp_path: Path) -> None:
    _snapshot_path, authority, registry, _run = _review_fixture(tmp_path)

    with pytest.raises(
        ValueError,
        match="retry-review reason requires a durable state path",
    ):
        work_actions._retry_review_action(
            args={
                "action": "retry-review",
                "repo": REPO,
                "work_id": WORK_ID,
                "issue": 12,
                "actor": "operator",
                "expected_candidate": HEAD,
                "reason": "accept the documented scope-bypass finding",
            },
            authority=authority,
            workflow_registry=registry,
            state_path=None,
        )
    assert not (tmp_path / "evidence" / "operator-adjudication").exists()


def test_retry_review_without_reason_returns_null_adjudication_fields(
    tmp_path: Path,
) -> None:
    result, _registry, _run, _state_path = _retry_review(tmp_path)

    assert "adjudication_evidence" in result
    assert "adjudication" in result
    assert result["adjudication_evidence"] is None
    assert result["adjudication"] is None


def test_retry_review_adjudication_reaches_reviewer_prompt_and_truncates_long_reason(
    tmp_path: Path,
) -> None:
    short_root = tmp_path / "short"
    short_root.mkdir()
    raw_reason = "  accept the scope-bypass finding as documented  "
    result, registry, original_run, state_path = _retry_review(
        short_root, reason=raw_reason
    )
    evidence = result["adjudication_evidence"]
    assert set(evidence) == {"ref", "hash"}
    evidence_path = Path(evidence["ref"])
    assert evidence_path == short_root / "evidence" / "operator-adjudication" / (
        f"{original_run.run_id}-{evidence['hash']}.json"
    )
    assert evidence_path.stat().st_mode & 0o222 == 0
    body = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert body["schema"] == "cortex-operator-adjudication/v1"
    assert body["card"] == "code-review"
    assert body["phase"] == "review"
    assert body["reason"] == raw_reason.strip()
    assert body["actor"] == "operator"
    assert result["adjudication"]["evidence"] == evidence
    assert "code-review" in result["adjudication"]["next_step_hint"]

    updated = registry.get_workflow_run(original_run.run_id)
    assert next(step for step in updated.steps if step.phase == "review").gate_result == "pending"
    assert "needs_human" not in updated.facets
    rows = manager._operator_adjudications(updated, state_path.parent)
    prompt = manager._workflow_job_prompt(
        updated,
        next(step for step in updated.steps if step.phase == "review"),
        builder_job_id="review-job-1",
        coordinator_root=state_path.parent,
        operator_adjudications=rows,
    )
    assert raw_reason.strip() in prompt
    assert manager.OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE.strip() in prompt

    long_root = tmp_path / "long"
    long_root.mkdir()
    long_reason = "x" * 2000 + "TAIL-ONLY-" * 50
    long_result, long_registry, long_run, long_state_path = _retry_review(
        long_root, reason=long_reason
    )
    long_evidence = long_result["adjudication_evidence"]
    long_body = json.loads(Path(long_evidence["ref"]).read_text(encoding="utf-8"))
    assert long_body["reason"] == long_reason
    long_updated = long_registry.get_workflow_run(long_run.run_id)
    long_rows = manager._operator_adjudications(long_updated, long_state_path.parent)
    long_prompt = manager._workflow_job_prompt(
        long_updated,
        next(step for step in long_updated.steps if step.phase == "review"),
        builder_job_id="review-job-2",
        coordinator_root=long_state_path.parent,
        operator_adjudications=long_rows,
    )
    assert long_reason[:2000] in long_prompt
    assert "TAIL-ONLY-" not in long_prompt


def test_reviewer_directive_lists_blocking_and_non_blocking_categories() -> None:
    directive = manager.OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE
    blocking = set(foreign_review.BLOCKING_FINDING_CATEGORIES)
    non_blocking = set(foreign_review.VALID_FINDING_CATEGORIES) - blocking

    assert blocking | non_blocking
    assert all(category in directive for category in blocking | non_blocking)
    assert not any(
        category in manager.OPERATOR_ADJUDICATION_DIRECTIVE
        for category in blocking | non_blocking
    )


class _JobList:
    def __init__(self, jobs=(), *, error: Exception | None = None):
        self.jobs = list(jobs)
        self.error = error

    def list_jobs(self):
        if self.error is not None:
            raise self.error
        return list(self.jobs)


def test_blocking_findings_recovery_actions_follow_review_then_build_matrix(
    tmp_path: Path,
) -> None:
    _snapshot_path, _authority, _registry, run = _review_fixture(tmp_path)
    actions = work_actions._phase_recovery_actions(run, _JobList())
    assert "retry-review" in actions
    assert "retry-build" in actions
    assert actions.index("retry-review") < actions.index("retry-build")

    other_root = tmp_path / "other-reason"
    other_root.mkdir()
    _, _, _, other_reason_run = _review_fixture(other_root, reason="reviewer-timeout")
    other_actions = work_actions._phase_recovery_actions(other_reason_run, _JobList())
    assert "retry-review" not in other_actions
    assert "retry-build" not in other_actions

    active_jobs = _JobList(
        [{"workflow_run_id": run.run_id, "status": "running"}]
    )
    active_actions = work_actions._phase_recovery_actions(run, active_jobs)
    assert "retry-review" not in active_actions
    assert "retry-build" not in active_actions

    unreadable_actions = work_actions._phase_recovery_actions(
        run, _JobList(error=RuntimeError("registry unavailable"))
    )
    assert "retry-review" not in unreadable_actions
    assert "retry-build" not in unreadable_actions

    mismatch_root = tmp_path / "candidate-mismatch"
    mismatch_root.mkdir()
    _, _, _, mismatch_run = _review_fixture(
        mismatch_root, verified_head="c" * 40
    )
    mismatch_actions = work_actions._phase_recovery_actions(mismatch_run, _JobList())
    assert "retry-review" not in mismatch_actions
    assert "retry-build" in mismatch_actions

    no_plan_root = tmp_path / "missing-plan"
    no_plan_root.mkdir()
    _, _, _, no_plan_run = _review_fixture(no_plan_root, plan_authority=False)
    no_plan_actions = work_actions._phase_recovery_actions(no_plan_run, _JobList())
    assert "retry-review" not in no_plan_actions
    assert "retry-build" in no_plan_actions


def test_blocking_findings_next_step_hint_formats_commands_and_placeholders() -> None:
    hint_func = getattr(work_actions, "blocking_findings_next_step_hint", None)
    assert callable(hint_func)

    hint = hint_func(work_id=WORK_ID, repo=REPO, candidate=HEAD)
    assert (
        "cortex run work retry-review demo --repo acme/demo "
        f"--expected-candidate {HEAD} --actor <operator> --reason '<裁決>'"
    ) in hint
    assert (
        "cortex run work retry-build demo --repo acme/demo "
        f"--expected-candidate {HEAD} --actor <operator> --reason '<裁決>'"
    ) in hint
    assert "abandon" in hint

    placeholders = hint_func(work_id="Bad ID", repo="bad/repo/path", candidate="not-a-sha")
    assert "<work-id>" in placeholders
    assert "<owner/repo>" in placeholders
    assert "<candidate-sha>" in placeholders


def test_blocking_findings_hint_reaches_status_and_resume(tmp_path: Path) -> None:
    snapshot, _authority, registry, run = _review_fixture(tmp_path)
    hint_func = getattr(work_actions, "blocking_findings_next_step_hint", None)
    assert callable(hint_func)
    expected_hint = hint_func(work_id=WORK_ID, repo=REPO, candidate=HEAD)

    entry = manager.workflow_status_entry(registry, run)
    assert {"abandon", "retry-review", "retry-build"} <= set(entry["next_actions"])
    assert entry["next_step_hint"] == expected_hint

    result = work_actions.execute_work_action(
        args={"action": "resume", "repo": REPO, "work_id": WORK_ID, "issue": 12},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=tmp_path / "runs.json",
        workflow_registry=registry,
        workflow_starter=lambda _authority, _claim_key, _reason: registry.get_workflow_run(
            run.run_id
        ),
    )["result"]
    assert result["action"] == "needs_human"
    assert {"abandon", "retry-review", "retry-build"} <= set(result["next_actions"])
    assert result["next_step_hint"] == expected_hint


def test_review_attest_refuses_blocking_findings_review_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot, _authority, registry, run = _review_fixture(
        tmp_path, with_foreign_review=True
    )
    original = registry.get_workflow_run(run.run_id)
    calls: list[dict[str, object]] = []

    class GitHub:
        def __init__(self, *, runner):
            pass

        def fetch_delivery_facts(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(head=HEAD)

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    with pytest.raises(
        RuntimeError,
        match="review-attest does not adjudicate review-gate blocking findings",
    ):
        work_actions.execute_work_action(
            args={
                "action": "review-attest",
                "repo": REPO,
                "work_id": WORK_ID,
                "actor": "maintainer@example",
                "verdict": "approved",
                "summary": "Exact-HEAD review approved.",
                "findings": [],
            },
            requested_by="operator",
            snapshot_path=snapshot,
            state_path=tmp_path / "runs.json",
            now=lambda: 1_758_625_447.25,
            workflow_registry=registry,
        )

    assert not calls
    assert not (tmp_path / "evidence" / "maintainer-review").exists()
    unchanged = registry.get_workflow_run(run.run_id)
    assert unchanged.gate_refs == original.gate_refs
    assert unchanged.facets == original.facets


def test_work_reason_help_mentions_retry_review_operator_adjudication_limit() -> None:
    parser = coordinator_cli._build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    work_parser = subparsers.choices["work"]
    reason = next(
        action
        for action in work_parser._actions
        if "--reason" in getattr(action, "option_strings", ())
    )

    assert "retry-review" in reason.help
    assert "4000" in reason.help
