"""Pure lifecycle reducer for the four public Work Item states."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .correlation import CorrelationResult
from .work_models import WorkItem


_STATES = frozenset({"topic", "todo", "ongoing", "done"})


@dataclass(frozen=True)
class ClosureEvidence:
    pr_merged_with_merge_commit: bool = False
    issues_all_closed: bool = False
    remote_active_openspec_absent: bool = False
    remote_archive_present: bool = False
    todo_tasks_complete: bool = False
    completion_record_valid: bool = False

    @property
    def complete(self) -> bool:
        return all(
            (
                self.pr_merged_with_merge_commit,
                self.issues_all_closed,
                self.remote_active_openspec_absent,
                self.remote_archive_present,
                self.todo_tasks_complete,
                self.completion_record_valid,
            )
        )


@dataclass(frozen=True)
class LifecycleFacts:
    previous_state: str | None = None
    provider_degraded: bool = False
    active_workflow: bool = False
    active_todo: bool = False
    open_issue: bool = False
    closure: ClosureEvidence | None = None
    facets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.previous_state == "on-going":
            object.__setattr__(self, "previous_state", "ongoing")
        if self.previous_state is not None and self.previous_state not in _STATES:
            raise ValueError(f"invalid previous lifecycle state: {self.previous_state!r}")


@dataclass(frozen=True)
class LifecycleDecision:
    state: str
    facets: tuple[str, ...]
    trace: tuple[Mapping[str, object], ...]

    @property
    def public_state(self) -> str:
        return "on-going" if self.state == "ongoing" else self.state


@dataclass(frozen=True)
class LifecycleProjection:
    items: tuple[WorkItem, ...]
    explanations: Mapping[str, Mapping]


def reduce_lifecycle(facts: LifecycleFacts) -> LifecycleDecision:
    facets = set(facts.facets)
    trace: list[dict[str, object]] = []
    if facts.provider_degraded:
        facets.add("degraded")
        frozen = facts.previous_state or "topic"
        trace.append(
            {"rule": "provider_degraded_freeze", "accepted": True, "state": frozen}
        )
        return LifecycleDecision(frozen, tuple(sorted(facets)), tuple(trace))
    trace.append({"rule": "provider_degraded_freeze", "accepted": False})
    if facts.active_workflow:
        trace.append({"rule": "active_workflow", "accepted": True, "state": "ongoing"})
        return LifecycleDecision("ongoing", tuple(sorted(facets)), tuple(trace))
    trace.append({"rule": "active_workflow", "accepted": False})
    if facts.closure is not None and facts.closure.complete:
        trace.append({"rule": "strict_closure", "accepted": True, "state": "done"})
        return LifecycleDecision("done", tuple(sorted(facets)), tuple(trace))
    trace.append({"rule": "strict_closure", "accepted": False})
    if facts.active_todo:
        trace.append({"rule": "active_todo", "accepted": True, "state": "todo"})
        return LifecycleDecision("todo", tuple(sorted(facets)), tuple(trace))
    trace.append({"rule": "active_todo", "accepted": False})
    trace.append({"rule": "open_issue", "accepted": bool(facts.open_issue), "state": "topic"})
    return LifecycleDecision("topic", tuple(sorted(facets)), tuple(trace))


def project_work_items(
    correlation: CorrelationResult,
    *,
    repo: str,
    updated_at: str,
    previous_items: Sequence[WorkItem] = (),
    closure_by_work: Mapping[str, ClosureEvidence] | None = None,
) -> LifecycleProjection:
    """Apply the reducer to correlated groups and attach its trace to explain."""
    previous = {
        item.work_id: item for item in previous_items if item.repo == repo
    }
    closure_by_work = closure_by_work or {}
    explanations = {
        work_id: dict(explanation)
        for work_id, explanation in correlation.explanations.items()
    }
    if correlation.degraded:
        frozen_items: list[WorkItem] = []
        for item in previous.values():
            decision = reduce_lifecycle(
                LifecycleFacts(
                    previous_state=item.state,
                    provider_degraded=True,
                    facets=tuple(facet for facet in item.facets if facet != "degraded"),
                )
            )
            frozen_items.append(
                WorkItem(
                    work_id=item.work_id,
                    repo=item.repo,
                    title=item.title,
                    state=decision.state,
                    phase=item.phase,
                    facets=decision.facets,
                    sources=item.sources,
                    next_actions=item.next_actions,
                    workflow_run_id=item.workflow_run_id,
                    updated_at=updated_at,
                )
            )
            explanation = dict(explanations.get(item.work_id, _empty_explanation(item.work_id)))
            explanation["reducer_trace"] = list(decision.trace)
            explanations[item.work_id] = explanation
        return LifecycleProjection(
            items=tuple(sorted(frozen_items, key=lambda item: (item.repo, item.work_id))),
            explanations=explanations,
        )

    projected: list[WorkItem] = []
    todo_kinds = {"todo", "superpowers_spec", "superpowers_plan", "openspec"}
    for group in correlation.groups:
        prior = previous.get(group.work_id)
        workflows = [
            source
            for source in group.sources
            if source.kind == "workflow_run" and source.status not in {"done", "completed", "failed"}
        ]
        facts = LifecycleFacts(
            previous_state=prior.state if prior is not None else None,
            active_workflow=bool(workflows),
            active_todo=any(
                source.kind in todo_kinds and source.status == "active"
                for source in group.sources
            ),
            open_issue=any(
                source.kind == "github_issue" and source.status == "open"
                for source in group.sources
            ),
            closure=closure_by_work.get(group.work_id),
            facets=tuple(
                facet for facet in (prior.facets if prior is not None else ())
                if facet != "degraded"
            ),
        )
        decision = reduce_lifecycle(facts)
        workflow = workflows[0] if workflows else None
        projected.append(
            WorkItem(
                work_id=group.work_id,
                repo=repo,
                title=group.title,
                state=decision.state,
                phase=prior.phase if prior is not None and workflows else None,
                facets=decision.facets,
                sources=group.sources,
                next_actions=("start",)
                if decision.state == "todo" and group.confidence == "confirmed"
                else (),
                workflow_run_id=workflow.ref if workflow is not None else None,
                updated_at=updated_at,
            )
        )
        explanation = dict(
            explanations.get(group.work_id, _empty_explanation(group.work_id))
        )
        explanation["reducer_trace"] = list(decision.trace)
        explanations[group.work_id] = explanation
    return LifecycleProjection(
        items=tuple(sorted(projected, key=lambda item: (item.repo, item.work_id))),
        explanations=explanations,
    )


def _empty_explanation(work_id: str) -> dict:
    return {
        "work_id": work_id,
        "authoritative_links": [],
        "inferred_signals": [],
        "competing_candidates": [],
        "exclusions": [],
        "reducer_trace": [],
    }
