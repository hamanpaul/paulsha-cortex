from __future__ import annotations

from datetime import datetime, timezone

import pytest

from paulsha_cortex.monitor.lifecycle import project_work_items
from paulsha_cortex.monitor.correlation import correlate_work_sources
from paulsha_cortex.monitor.work_api import (
    AmbiguousWorkItemError,
    WorkModelRefresher,
    WorkReadModelStore,
)
from paulsha_cortex.monitor.models import ProjectState
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider
from paulsha_cortex.monitor.work_models import ProviderSnapshot, WorkItem, WorkSource
from paulsha_cortex.monitor.work_snapshot import WorkSnapshot, WorkSnapshotStore


NOW = "2026-07-17T10:00:00Z"


def _item(repo: str, work_id: str, *, state: str = "todo") -> WorkItem:
    return WorkItem(
        work_id=work_id,
        repo=repo,
        title=work_id,
        state=state,
        phase=None,
        facets=(),
        sources=(),
        next_actions=(),
        workflow_run_id=None,
        updated_at=NOW,
    )


def _snapshot(*items: WorkItem, sequence: int = 0) -> WorkSnapshot:
    return WorkSnapshot(
        sequence=sequence,
        written_at=NOW,
        providers={},
        work_items=items,
        source_owners={},
        exclusions=(),
    )


def test_same_explicit_work_id_can_coexist_across_repos_and_lookup_is_unambiguous():
    store = WorkReadModelStore(
        _snapshot(_item("example/one", "shared"), _item("example/two", "shared"))
    )

    assert len(store.list_work_items(include_done=True)["items"]) == 2
    assert store.get_work_item("shared", repo="example/one")["item"]["repo"] == "example/one"
    with pytest.raises(AmbiguousWorkItemError):
        store.get_work_item("shared")


def test_inferred_todo_is_visible_but_has_no_start_authority(tmp_path):
    artifact = tmp_path / "docs/superpowers/specs/work.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("# work\n", encoding="utf-8")
    source = WorkSource(
        source_id="todo:example/acme:docs/superpowers/specs/work.md",
        kind="superpowers_spec",
        ref="docs/superpowers/specs/work.md",
        revision="local:1",
        status="active",
        confidence="confirmed",
        provider="repo:example/acme",
    )
    correlation = correlate_work_sources(tmp_path, "example/acme", (source,))

    projection = project_work_items(
        correlation, repo="example/acme", updated_at=NOW
    )

    assert projection.items[0].state == "todo"
    assert projection.items[0].next_actions == ()


def test_closing_reference_inherits_override_owner_without_competing_collision(tmp_path):
    override = tmp_path / ".cortex/work-items.yaml"
    override.parent.mkdir(parents=True)
    override.write_text(
        """version: 1
work_items:
  work:
    title: Work
    links:
      - kind: github_issue
        ref: example/acme#7
    excludes: []
""",
        encoding="utf-8",
    )
    issue = WorkSource(
        source_id="github_issue:example/acme#7",
        kind="github_issue",
        ref="example/acme#7",
        revision="github:i7",
        status="closed",
        confidence="confirmed",
        provider="github:example/acme",
    )
    pull = WorkSource(
        source_id="github_pr:example/acme#9",
        kind="github_pr",
        ref="example/acme#9",
        revision="github:p9",
        status="closed",
        confidence="confirmed",
        provider="github:example/acme",
    )

    result = correlate_work_sources(
        tmp_path,
        "example/acme",
        (issue, pull),
        closing_links={pull.source_id: issue.source_id},
    )

    assert not result.degraded
    assert result.source_owners == {issue.source_id: "work", pull.source_id: "work"}


class _StaticProvider:
    def __init__(self, snapshot: ProviderSnapshot):
        self.snapshot = snapshot

    def scan(self) -> ProviderSnapshot:
        return self.snapshot


def _provider(provider_id: str, sources=(), *, observations=None, at=NOW):
    return ProviderSnapshot(
        provider_id=provider_id,
        status="ok",
        last_attempt_at=at,
        last_success_at=at,
        revision="revision:1",
        diagnostics=(),
        sources=tuple(sources),
        observations=observations or {},
    )


def test_production_wiring_passes_workflow_links_and_strict_closure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    issue = WorkSource(
        source_id="github_issue:example/acme#7",
        kind="github_issue",
        ref="example/acme#7",
        revision="github:i7",
        status="closed",
        confidence="confirmed",
        provider="github:example/acme",
    )
    workflow = WorkSource(
        source_id="workflow_run:run-7",
        kind="workflow_run",
        ref="run-7",
        revision="registry:7",
        status="build",
        confidence="confirmed",
        provider="workflow:example/acme",
    )
    gh = _provider(
        "github:example/acme",
        (issue,),
    )
    terminal = _provider(
        "github-terminal:example/acme",
        observations={
            "closing_links": {issue.source_id: "work"},
            "closure_by_work": {
                "work": {
                    "pr_merged_with_merge_commit": True,
                    "remote_active_openspec_absent": True,
                    "remote_archive_present": True,
                }
            },
            "remote_openspec": {"active": [], "archived": ["work"]},
            "remote_openspec_observed": True,
            "remote_todos": [
                {
                    "work_id": "work",
                    "path": "docs/superpowers/workstreams/work/todo.md",
                    "revision": "c" * 40,
                    "complete": True,
                }
            ],
        },
    )
    registry = _provider(
        "workflow:example/acme",
        (workflow,),
        observations={
            "workflow_links": {workflow.source_id: "work"},
            "closure_by_work": {"work": {"completion_record_valid": True}},
        },
    )
    durable = WorkSnapshotStore(tmp_path / "snapshot.json")
    store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(
        durable_store=durable,
        read_store=store,
        github_provider_factory=lambda _repo: _StaticProvider(gh),
        github_terminal_provider_factory=lambda _repo: _StaticProvider(terminal),
        workflow_provider_factory=lambda _repo: _StaticProvider(registry),
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))

    refresher.refresh((project,), include_github=True)
    assert store.get_work_item("work", repo="example/acme")["item"]["state"] == "on-going"

    completed = _provider(
        "workflow:example/acme",
        (),
        observations={
            "closure_by_work": {"work": {"completion_record_valid": True}},
        },
    )
    refresher.workflow_provider_factory = lambda _repo: _StaticProvider(completed)
    refresher.refresh((project,), include_github=True)
    assert store.get_work_item("work", repo="example/acme")["item"]["state"] == "done"


def test_strict_closure_conjoins_all_confirmed_group_issues(tmp_path):
    repo = tmp_path / "repo"
    override = repo / ".cortex/work-items.yaml"
    override.parent.mkdir(parents=True)
    override.write_text(
        """version: 1
work_items:
  work:
    title: Work
    links:
      - kind: github_issue
        ref: example/acme#7
      - kind: github_issue
        ref: example/acme#8
      - kind: github_pr
        ref: example/acme#9
    excludes: []
""",
        encoding="utf-8",
    )
    sources = tuple(
        WorkSource(
            source_id=f"{kind}:example/acme#{number}",
            kind=kind,
            ref=f"example/acme#{number}",
            revision=f"github:{number}",
            status=status,
            confidence="confirmed",
            provider="github:example/acme",
        )
        for kind, number, status in (
            ("github_issue", 7, "closed"),
            ("github_issue", 8, "open"),
            ("github_pr", 9, "closed"),
        )
    )
    gh = _provider("github:example/acme", sources)
    terminal = _provider(
        "github-terminal:example/acme",
        observations={
            "closure_by_work": {
                "work": {
                    "pr_merged_with_merge_commit": True,
                }
            },
            "remote_openspec": {"active": [], "archived": ["work"]},
            "remote_openspec_observed": True,
            "remote_todos": [
                {
                    "work_id": "work",
                    "path": "docs/superpowers/workstreams/work/todo.md",
                    "revision": "c" * 40,
                    "complete": True,
                }
            ],
        },
    )
    registry = _provider(
        "workflow:example/acme",
        observations={
            "closure_by_work": {"work": {"completion_record_valid": True}}
        },
    )
    store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=store,
        github_provider_factory=lambda _repo: _StaticProvider(gh),
        github_terminal_provider_factory=lambda _repo: _StaticProvider(terminal),
        workflow_provider_factory=lambda _repo: _StaticProvider(registry),
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))

    refresher.refresh((project,), include_github=True)

    assert store.get_work_item("work", repo="example/acme")["item"]["state"] != "done"


def test_production_fuzzy_title_and_artifact_slug_group_for_display_only(tmp_path):
    repo = tmp_path / "repo"
    proposal = repo / "openspec/changes/display-work/proposal.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    issue = WorkSource(
        source_id="github_issue:example/acme#7",
        kind="github_issue",
        ref="example/acme#7",
        revision="github:i7",
        status="open",
        confidence="confirmed",
        provider="github:example/acme",
        title="Display Work",
    )
    gh = _provider("github:example/acme", (issue,))
    store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=store,
        github_provider_factory=lambda _repo: _StaticProvider(gh),
        github_terminal_provider_factory=lambda _repo: _StaticProvider(
            _provider("github-terminal:example/acme")
        ),
        workflow_provider_factory=lambda _repo: _StaticProvider(
            _provider("workflow:example/acme")
        ),
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))

    refresher.refresh((project,), include_github=True)

    items = store.list_work_items(include_done=True)["items"]
    assert [(item["work_id"], len(item["sources"])) for item in items] == [
        ("display-work", 2)
    ]
    assert items[0]["next_actions"] == []


def test_stale_github_snapshot_freezes_state_and_closes_automation_gates(tmp_path):
    stale = "2026-07-17T09:44:59Z"
    provider = _provider("github:example/acme", at=stale)
    previous = WorkSnapshot(
        sequence=1,
        written_at=stale,
        providers={provider.provider_id: provider},
        work_items=(_item("example/acme", "work", state="todo"),),
        source_owners={},
        exclusions=(),
    )
    store = WorkReadModelStore(previous)
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=store,
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
        stale_after_seconds=900,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))

    refresher.refresh((project,), include_github=False)

    item = store.get_work_item("work", repo="example/acme")["item"]
    assert item["state"] == "todo"
    assert "degraded" in item["facets"]
    assert store.list_work_items()["hard_gates"] == {
        "auto_claim": False,
        "merge": False,
        "reasons": ["github:example/acme stale"],
    }


def test_malformed_workflow_registry_retains_last_good_and_freezes(tmp_path):
    repo = tmp_path / "repo"
    spec = repo / "docs/superpowers/specs/work.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("---\nwork_item: work\n---\n# Work\n", encoding="utf-8")
    state = tmp_path / "coordinator/workflows.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        '{"schema_version":2,"sequence":1,"legacy_records":{"jobs":[],"slices":[]},"workflow_runs":['
        '{"run_id":"run-1","repo":"example/acme","work_id":"work","status":"build"}]}'
        ,
        encoding="utf-8",
    )
    durable = WorkSnapshotStore(tmp_path / "snapshot.json")
    store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(
        durable_store=durable,
        read_store=store,
        workflow_provider_factory=lambda name: WorkflowRegistryProvider(
            name, state_path=state
        ),
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))
    refresher.refresh((project,), include_github=False)
    assert store.get_work_item("work", repo="example/acme")["item"]["state"] == "on-going"

    state.write_text(
        '{"schema_version":99,"sequence":2,"legacy_records":{"jobs":[],"slices":[]},"workflow_runs":[]}', encoding="utf-8"
    )
    refresher.refresh((project,), include_github=False)

    item = store.get_work_item("work", repo="example/acme")["item"]
    assert item["state"] == "on-going"
    assert "degraded" in item["facets"]
    provider = durable.load().providers["workflow:example/acme"]
    assert provider.status == "degraded"
    assert [source.ref for source in provider.sources] == ["run-1"]


def test_multi_item_sequence_is_durable_before_read_model_commit(tmp_path):
    path = tmp_path / "snapshot.json"
    durable = WorkSnapshotStore(path)
    store = WorkReadModelStore(_snapshot(_item("example/acme", "one"), sequence=4))
    candidate = _snapshot(
        _item("example/acme", "one", state="ongoing"),
        _item("example/acme", "two"),
        sequence=5,
    )

    events = store.replace_durably(candidate, durable.write)

    assert [event.sequence for event in events] == [5, 6]
    assert durable.load().sequence == store.sequence == 6
    restarted = WorkReadModelStore(durable.load())
    assert restarted.sequence == 6
