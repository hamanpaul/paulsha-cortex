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
    projected: list[WorkItem] = []
    todo_kinds = {"todo", "superpowers_spec", "superpowers_plan", "openspec"}
    for group in correlation.groups:
        prior = previous.get(group.work_id)
        workflows = [
            source
            for source in group.sources
            if source.kind == "workflow_run"
            and source.status not in {"done", "completed", "failed", "superseded"}
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
            provider_degraded=correlation.degraded,
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

    if correlation.degraded:
        projected_ids = {group.work_id for group in correlation.groups}
        # #523：保留分支原本只比對 **work_id**，不比對 **sources**。當某個 source 的
        # 歸屬從 fallback work item（`correlation.py:_fallback_work_id` 產生的
        # `issue:<ref>`）轉移到新宣告的 work item 時，本輪 correlation 已不再產生那個
        # fallback，於是它「不在 projected_ids 裡」→ 連同**舊的 sources** 被整筆放回
        # → 兩個 work item 同時宣稱擁有同一個 source →
        # `work_snapshot.validate_ownership()` raise → 整個 refresh 失敗。
        #
        # 而該例外發生在 `WorkSnapshot.__post_init__`、早於 `replace_durably()`，
        # 所以那一輪算出的 provider 新狀態（包含「backoff 已結束」）一併被丟棄，
        # `previous` 永遠停在崩潰前那一版、`degraded` 永遠為真 → 每輪重演。
        # 亦即 provider 無法離開 degraded，因為記錄它恢復的那次寫入正是拋例外的那次。
        #
        # 修法：保留時剝除已被本輪認領的 source。source 全被認領（集合變空）代表這筆
        # 舊 work item 的內容已完整轉移到新歸屬，整筆丟棄即可——保留一個沒有任何
        # source 的空殼既無資訊也無意義。
        claimed_source_ids = {
            source.source_id for group in correlation.groups for source in group.sources
        }
        for item in previous.values():
            if item.work_id in projected_ids:
                continue
            retained_sources = tuple(
                source
                for source in item.sources
                if source.source_id not in claimed_source_ids
            )
            # 只有「原本有 source、且全部被本輪認領」才代表內容已完整轉移、該整筆丟棄。
            # 原本就沒有 source 的 previous item（例如僅由 workflow_run 衍生的項目）
            # 維持既有保留語意，不受本修正影響。
            if item.sources and not retained_sources:
                continue
            decision = reduce_lifecycle(
                LifecycleFacts(
                    previous_state=item.state,
                    provider_degraded=True,
                    facets=tuple(facet for facet in item.facets if facet != "degraded"),
                )
            )
            projected.append(
                WorkItem(
                    work_id=item.work_id,
                    repo=item.repo,
                    title=item.title,
                    state=decision.state,
                    phase=item.phase,
                    facets=decision.facets,
                    sources=retained_sources,
                    next_actions=item.next_actions,
                    workflow_run_id=item.workflow_run_id,
                    updated_at=updated_at,
                )
            )
            explanation = dict(explanations.get(item.work_id, _empty_explanation(item.work_id)))
            explanation["reducer_trace"] = list(decision.trace)
            explanations[item.work_id] = explanation

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
