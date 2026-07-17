from __future__ import annotations

import json
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
    pull = _github_entity("github_pr", 9, "closed")
    openspec = _remote_openspec_source("work")
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
        (issue, pull),
    )
    terminal = _provider(
        "github-terminal:example/acme",
        (openspec,),
        observations={
            "closing_links": {pull.source_id: issue.source_id},
            "remote_prs": [
                {
                    "source_id": pull.source_id,
                    "candidate": "a" * 40,
                    "merge_revision": "b" * 40,
                    "merged_with_merge_commit": True,
                }
            ],
            "remote_openspec": {"active": [], "archived": ["work"]},
            "remote_openspec_observed": True,
            "remote_todos": [
                {
                    "work_id": "work",
                    "path": "docs/superpowers/workstreams/work/todo.md",
                    "revision": "c" * 40,
                    "complete": True,
                },
                {
                    "openspec_ref": "work",
                    "path": "openspec/changes/archive/2026-07-17-work/tasks.md",
                    "revision": "d" * 40,
                    "complete": True,
                }
            ],
        },
    )
    registry = _provider(
        "workflow:example/acme",
        (workflow,),
        observations={
            "workflow_links": {
                workflow.source_id: "work",
                issue.source_id: "work",
                pull.source_id: "work",
                openspec.source_id: "work",
            },
            "validated_completions": {
                "work": [
                    {
                        "run_id": "run-7",
                        "pr_candidate": "a" * 40,
                        "merge_revision": "b" * 40,
                        "source_revisions": {
                            issue.source_id: issue.revision,
                            pull.source_id: pull.revision,
                            openspec.source_id: openspec.revision,
                        },
                    }
                ]
            },
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
            "workflow_links": {
                issue.source_id: "work",
                pull.source_id: "work",
                openspec.source_id: "work",
            },
            "validated_completions": {
                "work": [
                    {
                        "run_id": "run-7",
                        "pr_candidate": "a" * 40,
                        "merge_revision": "b" * 40,
                        "source_revisions": {
                            issue.source_id: issue.revision,
                            pull.source_id: pull.revision,
                            openspec.source_id: openspec.revision,
                        },
                    }
                ]
            },
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


def _remote_openspec_source(ref: str, *, status: str = "archived") -> WorkSource:
    return WorkSource(
        source_id=f"github_openspec:example/acme:{ref}:{status}",
        kind="openspec",
        ref=ref,
        revision="github-tree:" + "d" * 40,
        status=status,
        confidence="confirmed",
        provider="github-terminal:example/acme",
    )


def _github_entity(kind: str, number: int, status: str) -> WorkSource:
    return WorkSource(
        source_id=f"{kind}:example/acme#{number}",
        kind=kind,
        ref=f"example/acme#{number}",
        revision=f"github:{number}",
        status=status,
        confidence="confirmed",
        provider="github:example/acme",
    )


def _closure_terminal(*, openspec_refs, prs, active=()):
    sources = tuple(_remote_openspec_source(ref) for ref in openspec_refs)
    return _provider(
        "github-terminal:example/acme",
        sources,
        observations={
            "closing_links": {
                f"github_pr:example/acme#{number}": "github_issue:example/acme#7"
                for number, _ in prs
            },
            "remote_prs": [
                {
                    "source_id": f"github_pr:example/acme#{number}",
                    "candidate": chr(96 + index) * 40,
                    "merge_revision": chr(96 + index) * 40,
                    "merged_with_merge_commit": merged,
                }
                for index, (number, merged) in enumerate(prs, 1)
            ],
            "remote_openspec": {
                "active": list(active),
                "archived": list(openspec_refs),
            },
            "remote_openspec_observed": True,
            "remote_todos": [
                {
                    "openspec_ref": ref,
                    "path": f"openspec/changes/archive/2026-07-17-{ref}/tasks.md",
                    "revision": "c" * 40,
                    "complete": True,
                }
                for ref in openspec_refs
            ],
        },
    )


def _run_closure_projection(tmp_path, *, override_text, github_sources, terminal):
    repo = tmp_path / "repo"
    override = repo / ".cortex/work-items.yaml"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(override_text, encoding="utf-8")
    store = WorkReadModelStore.empty()
    mapped_sources = tuple(github_sources) + tuple(terminal.sources)
    remote_prs = terminal.observations.get("remote_prs", [])
    first_pr = remote_prs[0] if remote_prs else {}
    validated_completions = {
        "umbrella": [
            {
                "run_id": "run-complete",
                "pr_candidate": first_pr.get("candidate"),
                "merge_revision": first_pr.get("merge_revision"),
                "source_revisions": {
                    source.source_id: source.revision for source in mapped_sources
                },
            }
        ]
    }
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=store,
        github_provider_factory=lambda _repo: _StaticProvider(
            _provider("github:example/acme", github_sources)
        ),
        github_terminal_provider_factory=lambda _repo: _StaticProvider(terminal),
        workflow_provider_factory=lambda _repo: _StaticProvider(
            _provider(
                "workflow:example/acme",
                observations={
                    "validated_completions": validated_completions
                },
            )
        ),
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))
    refresher.refresh((project,), include_github=True)
    return store, refresher, project


def test_openspec_only_archived_tasks_can_complete_without_todo_md(tmp_path):
    override = """version: 1
work_items:
  umbrella:
    title: Canary
    links:
      - kind: github_issue
        ref: example/acme#7
      - kind: github_pr
        ref: example/acme#9
      - kind: openspec
        ref: canary
    excludes: []
"""
    github = (_github_entity("github_issue", 7, "closed"), _github_entity("github_pr", 9, "closed"))
    terminal = _closure_terminal(openspec_refs=("canary",), prs=((9, True),))

    store, _, _ = _run_closure_projection(
        tmp_path, override_text=override, github_sources=github, terminal=terminal
    )

    assert store.get_work_item("umbrella", repo="example/acme")["item"]["state"] == "done"


def test_all_confirmed_mapped_prs_must_be_terminal_merge_commits(tmp_path):
    override = """version: 1
work_items:
  umbrella:
    title: Multi PR
    links:
      - kind: github_issue
        ref: example/acme#7
      - kind: github_pr
        ref: example/acme#9
      - kind: github_pr
        ref: example/acme#10
      - kind: openspec
        ref: canary
    excludes: []
"""
    github = (
        _github_entity("github_issue", 7, "closed"),
        _github_entity("github_pr", 9, "closed"),
        _github_entity("github_pr", 10, "open"),
    )
    terminal = _closure_terminal(
        openspec_refs=("canary",), prs=((9, True), (10, False))
    )

    store, _, _ = _run_closure_projection(
        tmp_path, override_text=override, github_sources=github, terminal=terminal
    )

    assert store.get_work_item("umbrella", repo="example/acme")["item"]["state"] != "done"


def test_completion_identity_must_match_every_mapped_merged_pr(tmp_path):
    override = """version: 1
work_items:
  umbrella:
    title: Multi PR Identity
    links:
      - kind: github_issue
        ref: example/acme#7
      - kind: github_pr
        ref: example/acme#9
      - kind: github_pr
        ref: example/acme#10
      - kind: openspec
        ref: canary
    excludes: []
"""
    github = (
        _github_entity("github_issue", 7, "closed"),
        _github_entity("github_pr", 9, "closed"),
        _github_entity("github_pr", 10, "closed"),
    )
    terminal = _closure_terminal(
        openspec_refs=("canary",), prs=((9, True), (10, True))
    )

    store, _, _ = _run_closure_projection(
        tmp_path, override_text=override, github_sources=github, terminal=terminal
    )

    assert store.get_work_item("umbrella", repo="example/acme")["item"]["state"] != "done"


@pytest.mark.parametrize("stale_field", ("candidate", "merge_revision", "source_revision"))
def test_same_work_completion_replay_must_match_current_terminal_identity(
    tmp_path, stale_field
):
    override = """version: 1
work_items:
  umbrella:
    title: Replay Guard
    links:
      - kind: github_issue
        ref: example/acme#7
      - kind: github_pr
        ref: example/acme#9
      - kind: openspec
        ref: canary
    excludes: []
"""
    github = (
        _github_entity("github_issue", 7, "closed"),
        _github_entity("github_pr", 9, "closed"),
    )
    terminal = _closure_terminal(openspec_refs=("canary",), prs=((9, True),))
    store, refresher, project = _run_closure_projection(
        tmp_path, override_text=override, github_sources=github, terminal=terminal
    )
    assert store.get_work_item("umbrella", repo="example/acme")["item"]["state"] == "done"

    stale_github = github
    stale_terminal = terminal
    if stale_field == "source_revision":
        stale_github = (
            WorkSource(
                **{
                    **github[0].__dict__,
                    "revision": "github:issue-updated-after-completion",
                }
            ),
            github[1],
        )
    else:
        remote_prs = [dict(row) for row in terminal.observations["remote_prs"]]
        remote_prs[0][stale_field] = "f" * 40
        stale_terminal = _provider(
            terminal.provider_id,
            terminal.sources,
            observations={**terminal.observations, "remote_prs": remote_prs},
        )
    refresher.github_provider_factory = lambda _repo: _StaticProvider(
        _provider("github:example/acme", stale_github)
    )
    refresher.github_terminal_provider_factory = lambda _repo: _StaticProvider(
        stale_terminal
    )

    refresher.refresh((project,), include_github=True)

    assert store.get_work_item("umbrella", repo="example/acme")["item"]["state"] != "done"


def test_unowned_remote_archive_is_terminal_evidence_not_a_work_item(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    terminal = _provider(
        "github-terminal:example/acme",
        (_remote_openspec_source("historical"),),
        observations={
            "remote_openspec": {"active": [], "archived": ["historical"]},
            "remote_openspec_observed": True,
            "remote_todos": [
                {
                    "openspec_ref": "historical",
                    "path": "openspec/changes/archive/2026-07-17-historical/tasks.md",
                    "revision": "c" * 40,
                    "complete": True,
                }
            ],
        },
    )
    store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=store,
        github_provider_factory=lambda _repo: _StaticProvider(
            _provider("github:example/acme")
        ),
        github_terminal_provider_factory=lambda _repo: _StaticProvider(terminal),
        workflow_provider_factory=lambda _repo: _StaticProvider(
            _provider("workflow:example/acme")
        ),
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))

    refresher.refresh((project,), include_github=True)

    assert store.list_work_items()["items"] == []
    assert store.list_work_items(include_done=True)["items"] == []


def test_unowned_closed_github_history_is_closure_evidence_not_work_items(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    github = _provider(
        "github:example/acme",
        (
            _github_entity("github_issue", 7, "closed"),
            _github_entity("github_pr", 9, "closed"),
        ),
    )
    store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=store,
        github_provider_factory=lambda _repo: _StaticProvider(github),
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

    assert store.list_work_items()["items"] == []
    assert store.list_work_items(include_done=True)["items"] == []


def test_completed_workflow_refs_aggregate_remote_archived_openspec(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "coordinator/workflows.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 3,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-3",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "completed",
                        "issue_refs": [],
                        "pr_refs": [],
                        "openspec_refs": ["canary"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    terminal = _provider(
        "github-terminal:example/acme",
        (_remote_openspec_source("canary"),),
        observations={
            "remote_openspec": {"active": [], "archived": ["canary"]},
            "remote_openspec_observed": True,
        },
    )
    store = WorkReadModelStore.empty()
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=store,
        github_provider_factory=lambda _repo: _StaticProvider(
            _provider("github:example/acme")
        ),
        github_terminal_provider_factory=lambda _repo: _StaticProvider(terminal),
        workflow_provider_factory=lambda repo_name: WorkflowRegistryProvider(
            repo_name, state_path=state
        ),
        now=lambda: datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc),
    )
    project = ProjectState(project_id="example/acme", workspace="ws", path=str(repo))

    refresher.refresh((project,), include_github=True)

    item = store.get_work_item("work", repo="example/acme")["item"]
    assert {source["status"] for source in item["sources"]} == {"completed", "archived"}


def test_openspec_closure_uses_all_mapped_refs_not_work_id_slug(tmp_path):
    override = """version: 1
work_items:
  umbrella:
    title: Different Slugs
    links:
      - kind: github_issue
        ref: example/acme#7
      - kind: github_pr
        ref: example/acme#9
      - kind: openspec
        ref: change-a
      - kind: openspec
        ref: change-b
    excludes: []
"""
    github = (_github_entity("github_issue", 7, "closed"), _github_entity("github_pr", 9, "closed"))
    terminal = _closure_terminal(
        openspec_refs=("change-a", "change-b"), prs=((9, True),)
    )
    store, refresher, project = _run_closure_projection(
        tmp_path, override_text=override, github_sources=github, terminal=terminal
    )
    assert store.get_work_item("umbrella", repo="example/acme")["item"]["state"] == "done"

    refresher.github_terminal_provider_factory = lambda _repo: _StaticProvider(
        _closure_terminal(
            openspec_refs=("change-a", "change-b"),
            prs=((9, True),),
            active=("change-b",),
        )
    )
    refresher.refresh((project,), include_github=True)
    assert store.get_work_item("umbrella", repo="example/acme")["item"]["state"] != "done"


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
