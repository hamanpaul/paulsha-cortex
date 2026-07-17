from __future__ import annotations

import pytest

from paulsha_cortex.monitor.lifecycle import (
    ClosureEvidence,
    LifecycleFacts,
    project_work_items,
    reduce_lifecycle,
)
from paulsha_cortex.monitor.correlation import (
    CorrelatedWork,
    CorrelationResult,
)
from paulsha_cortex.monitor.work_models import WorkItem, WorkSource


def _closure(**overrides):
    values = {
        "pr_merged_with_merge_commit": True,
        "issues_all_closed": True,
        "remote_active_openspec_absent": True,
        "remote_archive_present": True,
        "todo_tasks_complete": True,
        "completion_record_valid": True,
    }
    values.update(overrides)
    return ClosureEvidence(**values)


def test_lifecycle_priority_workflow_then_done_then_todo_then_topic():
    assert reduce_lifecycle(LifecycleFacts(active_workflow=True, closure=_closure())).state == "ongoing"
    assert reduce_lifecycle(LifecycleFacts(closure=_closure())).state == "done"
    assert reduce_lifecycle(LifecycleFacts(active_todo=True, open_issue=True)).state == "todo"
    assert reduce_lifecycle(LifecycleFacts(open_issue=True)).state == "topic"


@pytest.mark.parametrize(
    "missing",
    [
        "pr_merged_with_merge_commit",
        "issues_all_closed",
        "remote_active_openspec_absent",
        "remote_archive_present",
        "todo_tasks_complete",
        "completion_record_valid",
    ],
)
def test_partial_closure_never_projects_done(missing):
    decision = reduce_lifecycle(
        LifecycleFacts(active_todo=True, closure=_closure(**{missing: False}))
    )
    assert decision.state == "todo"


def test_provider_degraded_freezes_previous_state_and_adds_facet():
    decision = reduce_lifecycle(
        LifecycleFacts(
            previous_state="todo",
            provider_degraded=True,
            active_workflow=True,
            closure=_closure(),
        )
    )
    assert decision.state == "todo"
    assert decision.facets == ("degraded",)
    assert decision.trace[0]["rule"] == "provider_degraded_freeze"


def test_done_item_reopens_to_todo_or_topic():
    reopened_with_artifact = reduce_lifecycle(
        LifecycleFacts(previous_state="done", active_todo=True, open_issue=True)
    )
    reopened_issue_only = reduce_lifecycle(
        LifecycleFacts(previous_state="done", open_issue=True)
    )
    assert reopened_with_artifact.state == "todo"
    assert reopened_issue_only.state == "topic"


def test_public_state_spells_on_going_but_internal_state_is_ongoing():
    decision = reduce_lifecycle(LifecycleFacts(active_workflow=True))
    assert decision.state == "ongoing"
    assert decision.public_state == "on-going"


def _source(kind, ref, status):
    return WorkSource(
        source_id=f"{kind}:{ref}",
        kind=kind,
        ref=ref,
        revision="rev",
        status=status,
        confidence="confirmed",
        provider="repo:example/acme" if kind != "github_issue" else "github:example/acme",
    )


def _correlation(*sources, degraded=False):
    return CorrelationResult(
        groups=(
            CorrelatedWork(
                work_id="work",
                title="Work",
                sources=tuple(sources),
                confidence="confirmed",
            ),
        ) if sources else (),
        source_owners={source.source_id: "work" for source in sources},
        exclusions=(),
        explanations={
            "work": {
                "work_id": "work",
                "authoritative_links": [],
                "inferred_signals": [],
                "competing_candidates": [],
                "exclusions": [],
                "reducer_trace": [],
            }
        },
        degraded=degraded,
        diagnostics=("collision",) if degraded else (),
    )


def _previous(state="done"):
    return WorkItem(
        work_id="work",
        repo="example/acme",
        title="Work",
        state=state,
        phase=None,
        facets=(),
        sources=(),
        next_actions=(),
        workflow_run_id=None,
        updated_at="2026-07-17T09:00:00Z",
    )


def test_projection_builds_todo_and_populates_reducer_trace():
    result = _correlation(
        _source("github_issue", "example/acme#14", "open"),
        _source("superpowers_spec", "docs/superpowers/specs/work.md", "active"),
    )
    projection = project_work_items(result, repo="example/acme", updated_at="2026-07-17T10:00:00Z")
    assert projection.items[0].state == "todo"
    assert projection.items[0].next_actions == ("start",)
    assert projection.explanations["work"]["reducer_trace"][-1]["rule"] == "active_todo"


def test_projection_strict_done_then_reopen():
    closed = _correlation(_source("github_issue", "example/acme#14", "closed"))
    done = project_work_items(
        closed,
        repo="example/acme",
        updated_at="2026-07-17T10:00:00Z",
        closure_by_work={"work": _closure()},
    )
    assert done.items[0].state == "done"

    reopened = _correlation(_source("github_issue", "example/acme#14", "open"))
    projected = project_work_items(
        reopened,
        repo="example/acme",
        updated_at="2026-07-17T11:00:00Z",
        previous_items=done.items,
    )
    assert projected.items[0].state == "topic"


def test_projection_degraded_freezes_all_previous_items():
    projection = project_work_items(
        _correlation(degraded=True),
        repo="example/acme",
        updated_at="2026-07-17T10:00:00Z",
        previous_items=(_previous("done"),),
    )
    assert projection.items[0].state == "done"
    assert projection.items[0].facets == ("degraded",)
    assert projection.explanations["work"]["reducer_trace"][0]["rule"] == "provider_degraded_freeze"
