"""Regression coverage for issue #296.

2026-08-04 hippo incident: six parallel workflow lanes cleared claim→define
→plan→build, then every lane deadlocked at verify dispatch with
``ValueError: workflow planning input drift`` — the tdd-red/subagent-build
card contract requires the builder to tick ``tasks.md`` checkboxes, while the
#219 reviewer authority-proving mechanism froze that same file's baseline at
claim time and fail-closed on any hash drift, including the mandated tick.

Investigation for #296 found the underlying mechanism had already been fixed
the same day by issue #310 (PRs #311 and #312 — commits ``5dc5899`` and
``3f234a8``): ``_workflow_input_snapshot`` and
``_authority_map_with_checkbox_tolerance`` both tolerate a checkbox-only
``- [ ]`` ↔ ``- [x]`` change on ``kind=plan`` ``tasks.md``/``todo.md`` refs,
while any other byte difference — on those files or on any ``kind=spec``
authority ref such as ``proposal.md`` — still fails closed. #296 was filed
independently a few hours *before* #310's fix landed, describing the same
incident with a deeper three-mechanism root-cause writeup; it was never
closed as a duplicate.

``tests/test_checkbox_drift_tolerance.py`` already pins the #310 fix at the
level of the individual helper functions with synthetic (non-git) fixtures.
These tests instead exercise the exact call sequence
``_dispatch_workflow_card`` performs for a ``persona == "reviewer"`` step
(covering both the ``verify`` and ``review`` phases, per
``WORKFLOW_PHASES`` / ``_PHASE_PERSONA`` in workflow.py) — authority-map
build, input-snapshot build, cross-authority proof, and reviewer sandbox
materialization from a *real* git repository — to pin the fix at production
fidelity and close out #296 with confirming, not merely inherited, coverage.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import review as foreign_review
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority

TASKS_REF = "openspec/changes/2026-08-04-demo-296/tasks.md"
PROPOSAL_REF = "openspec/changes/2026-08-04-demo-296/proposal.md"

TASKS_BASELINE = """---
status: accepted
work_item: demo-296
---

# Tasks

- [ ] 1.1 RED：新增測試。
- [ ] 1.2 GREEN：實作。
"""

PROPOSAL_BASELINE = "# Proposal\n\nAccepted scope: fix the drift guard.\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _rev_parse(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_candidate_repo(repo: Path) -> None:
    """Seed a real git repo at the claim-time baseline (mirrors the operator
    workspace at the moment planning authority is frozen)."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "canary@example.invalid")
    _git(repo, "config", "user.name", "Canary")
    (repo / TASKS_REF).parent.mkdir(parents=True, exist_ok=True)
    (repo / TASKS_REF).write_text(TASKS_BASELINE, encoding="utf-8")
    (repo / PROPOSAL_REF).write_text(PROPOSAL_BASELINE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def _commit_build(repo: Path, *, tasks_text: str, proposal_text: str | None = None) -> str:
    """Simulate the builder's build commit landing in the same worktree the
    manager reuses (``builder_jobs[-1]["worktree"]``) for verify/review
    dispatch."""
    (repo / TASKS_REF).write_text(tasks_text, encoding="utf-8")
    if proposal_text is not None:
        (repo / PROPOSAL_REF).write_text(proposal_text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "build: tick task checkboxes")
    return _rev_parse(repo)


def _operator_root(tmp_path: Path) -> Path:
    """The frozen claim-time source (``run.workspace_root``): untouched by
    the build, used as the checkbox-tolerance baseline-bytes source."""
    root = tmp_path / "operator"
    (root / TASKS_REF).parent.mkdir(parents=True, exist_ok=True)
    (root / TASKS_REF).write_text(TASKS_BASELINE, encoding="utf-8")
    (root / PROPOSAL_REF).write_text(PROPOSAL_BASELINE, encoding="utf-8")
    return root


def _planning_authority() -> tuple[PlanningArtifactAuthority, ...]:
    return (
        PlanningArtifactAuthority(
            ref=TASKS_REF, kind="plan", work_id="demo-296",
            baseline_sha256=hashlib.sha256(TASKS_BASELINE.encode()).hexdigest(),
        ),
        PlanningArtifactAuthority(
            ref=PROPOSAL_REF, kind="spec", work_id="demo-296",
            baseline_sha256=hashlib.sha256(PROPOSAL_BASELINE.encode()).hexdigest(),
        ),
    )


def _dispatch_reviewer_like(
    *, operator_root: Path, repo: Path, candidate: str, coordinator_root: Path,
):
    """Reproduce the ``persona == "reviewer"`` branch of
    ``manager._dispatch_workflow_card`` (verify and review phases share this
    branch — see ``_PHASE_PERSONA["verify"] == "reviewer"`` in workflow.py):
    authority map -> input snapshot -> cross-authority proof -> sandbox
    materialization -> post-materialization snapshot revalidation.
    """
    run = SimpleNamespace(
        run_id="workflow-" + "b" * 20,
        work_id="demo-296",
        repo="hamanpaul/paulsha-hippo",
        source_revision="2" * 64,
        candidate_head=candidate,
        workspace_root=str(operator_root),
        planning_authority=_planning_authority(),
    )
    step = SimpleNamespace(card="verification")
    patterns = (TASKS_REF, PROPOSAL_REF)

    authority_map = manager._authority_map_with_checkbox_tolerance(run, candidate_root=repo)
    input_snapshot = manager._workflow_input_snapshot(
        run=run, repo_root=repo, patterns=patterns, coordinator_root=coordinator_root,
    )
    foreign_review.verify_authority_in_input_snapshot(
        authority=authority_map, input_snapshot=input_snapshot,
    )
    sandbox, checkout = manager._create_reviewer_sandbox(
        run=run, step=step, executor="codex", candidate_root=repo,
        coordinator_root=coordinator_root, input_snapshot=input_snapshot,
    )
    manager._validate_workflow_input_snapshot(
        checkout, list(input_snapshot), coordinator_root=coordinator_root,
    )
    return sandbox, checkout, input_snapshot


def test_checkbox_only_tick_clears_verify_dispatch_end_to_end(tmp_path: Path) -> None:
    """(a) builder ticks only the checkbox markers -> verify dispatch must
    succeed, and the reviewer sandbox must actually contain the ticked
    content (proving the reviewer sees real build output, not the stale
    baseline)."""
    operator_root = _operator_root(tmp_path)
    repo = tmp_path / "repo"
    _init_candidate_repo(repo)
    ticked = TASKS_BASELINE.replace("- [ ] 1.1", "- [x] 1.1").replace("- [ ] 1.2", "- [X] 1.2")
    candidate = _commit_build(repo, tasks_text=ticked)

    sandbox, checkout, input_snapshot = _dispatch_reviewer_like(
        operator_root=operator_root, repo=repo, candidate=candidate,
        coordinator_root=tmp_path / "coordinator",
    )
    try:
        assert (checkout / TASKS_REF).read_text(encoding="utf-8") == ticked
        rows = {row["path"]: row for row in input_snapshot}
        assert rows[TASKS_REF]["sha256"] == hashlib.sha256(ticked.encode()).hexdigest()
        assert rows[PROPOSAL_REF]["sha256"] == hashlib.sha256(PROPOSAL_BASELINE.encode()).hexdigest()
    finally:
        import shutil

        shutil.rmtree(sandbox, ignore_errors=True)


def test_tasks_md_textual_edit_still_fails_closed(tmp_path: Path) -> None:
    """(b) any non-checkbox byte change to the pinned tasks.md — even
    alongside a legitimate checkbox tick — must still trip the drift guard.
    This is exactly the #310/#321 boundary: builders may toggle checkboxes,
    nothing else."""
    operator_root = _operator_root(tmp_path)
    repo = tmp_path / "repo"
    _init_candidate_repo(repo)
    mutated = (
        TASKS_BASELINE
        .replace("- [ ] 1.1", "- [x] 1.1")
        .replace("1.2 GREEN：實作。", "1.2 GREEN：偷改任務語意。")
    )
    candidate = _commit_build(repo, tasks_text=mutated)

    with pytest.raises(ValueError, match="planning input drift"):
        _dispatch_reviewer_like(
            operator_root=operator_root, repo=repo, candidate=candidate,
            coordinator_root=tmp_path / "coordinator",
        )


def test_proposal_md_spec_edit_still_fails_closed_alongside_legitimate_tick(tmp_path: Path) -> None:
    """(c) a spec-kind authority ref (proposal.md) drifting must still fail
    closed even when bundled with an otherwise-legitimate tasks.md checkbox
    tick in the same build commit — the checkbox tolerance is scoped to
    kind=plan tasks/todo files only and must not leak into spec files."""
    operator_root = _operator_root(tmp_path)
    repo = tmp_path / "repo"
    _init_candidate_repo(repo)
    ticked = TASKS_BASELINE.replace("- [ ] 1.1", "- [x] 1.1").replace("- [ ] 1.2", "- [x] 1.2")
    drifted_proposal = PROPOSAL_BASELINE.replace("fix the drift guard.", "偷改規格範圍。")
    candidate = _commit_build(repo, tasks_text=ticked, proposal_text=drifted_proposal)

    with pytest.raises(ValueError, match="planning input drift"):
        _dispatch_reviewer_like(
            operator_root=operator_root, repo=repo, candidate=candidate,
            coordinator_root=tmp_path / "coordinator",
        )
