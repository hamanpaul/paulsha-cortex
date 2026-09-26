from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.control import contract
from paulsha_cortex.coordinator import cli, work_actions
from paulsha_cortex.coordinator.claim import WorkAuthority
from paulsha_cortex.coordinator.github_delivery import RemoteClosureFacts
from paulsha_cortex import cli as umbrella_cli
from paulsha_cortex.monitor.correlation import CorrelatedWork
from paulsha_cortex.monitor.providers import WorkflowRegistryProvider
from paulsha_cortex.monitor.work_api import _parse_closure_evidence
from paulsha_cortex.monitor.work_models import WorkSource


REPO = "acme/demo"
WORK_ID = "demo"
HEAD = "a" * 40
MERGE = "c" * 40
DEFAULT = "d" * 40
TODO_REVISION = "e" * 40
ARCHIVED_TASKS = "openspec/changes/archive/2026-09-01-demo/tasks.md"
SOURCE_REVISIONS = {
    "github_issue:acme/demo#12": "identity:acme/demo#12;state:closed",
    "github_pr:acme/demo#8": "identity:acme/demo#8;state:closed",
    "openspec:acme/demo:demo": "identity:demo;state:archived",
    "todo:acme/demo:docs/todo.md": "identity:docs/todo.md",
}


def _authority() -> WorkAuthority:
    revisions = tuple(
        f"{source_id}@{revision}"
        for source_id, revision in sorted(SOURCE_REVISIONS.items())
    )
    return WorkAuthority._verified(
        repo=REPO,
        work_id=WORK_ID,
        mapped_issues=(12,),
        mapped_prs=(8,),
        mapped_openspec=("demo",),
        mapped_todo_paths=("docs/todo.md",),
        confirmed_todo=True,
        auto_label=False,
        source_revisions=revisions,
        provider_revision="monitor-revision",
        last_success_epoch=100,
        snapshot_hash="snapshot-hash",
    )


def _closure_facts(**overrides) -> RemoteClosureFacts:
    values = {
        "merge_commit": MERGE,
        "pr_head": HEAD,
        "merge_parents": (HEAD, "b" * 40),
        "default_head": DEFAULT,
        "merge_is_ancestor": True,
        "merge_is_merge_commit": True,
        "issue_states": {12: "closed"},
        "active_openspec_absent": True,
        "archive_present": True,
        "todo_complete": True,
        "todo_revisions": {
            "docs/todo.md": TODO_REVISION,
            ARCHIVED_TASKS: "f" * 40,
        },
        "completion_record_valid": False,
        "openspec_required": True,
    }
    values.update(overrides)
    return RemoteClosureFacts(**values)


def _record_body() -> dict:
    return {
        "schema": "cortex-work-close-delivered/v1",
        "repo": REPO,
        "work_id": WORK_ID,
        "actor": "operator",
        "reason": "Delivered before WorkflowRun tracking was enabled.",
        "authority_digest": "f" * 64,
        "source_revisions": SOURCE_REVISIONS,
        "issue_states": [{"ref": f"{REPO}#12", "state": "closed"}],
        "pull_request": {
            "ref": f"{REPO}#8",
            "candidate": HEAD,
            "merge_commit": MERGE,
            "merge_parents": [HEAD, "b" * 40],
            "default_head": DEFAULT,
            "merge_is_ancestor": True,
            "merge_is_merge_commit": True,
        },
        "openspec": {
            "refs": ["demo"],
            "active_openspec_absent": True,
            "archive_present": True,
        },
        "todo_revisions": {"docs/todo.md": TODO_REVISION},
    }


def _write_close_delivered_record(root: Path, body: dict | None = None) -> Path:
    from paulsha_cortex.coordinator.verification import canonical_json_hash

    payload = _record_body() if body is None else body
    digest = canonical_json_hash(payload)
    directory = root / "evidence" / "work-close-delivered"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{WORK_ID}-{digest}.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    target.chmod(0o444)
    return target


def test_close_delivered_creates_completion_record_without_workflow_run(
    monkeypatch, tmp_path: Path
) -> None:
    authority = _authority()
    remote_calls = []

    def fetch_remote_closure(self, **kwargs):
        remote_calls.append(kwargs)
        return _closure_facts(
            todo_revisions={path: TODO_REVISION for path in kwargs["todo_paths"]}
        )

    monkeypatch.setattr(work_actions, "load_work_authority", lambda **_kwargs: authority)
    monkeypatch.setattr(
        work_actions.GitHubDeliveryClient, "fetch_remote_closure", fetch_remote_closure
    )
    monkeypatch.setattr(
        work_actions.GitHubDeliveryClient,
        "_commit_tree_paths",
        lambda self, **_kwargs: (ARCHIVED_TASKS,),
    )
    registry = SimpleNamespace(list_workflow_runs=lambda: [])
    state_path = tmp_path / "jobs.json"

    result = work_actions.execute_work_action(
        args={
            "action": "close-delivered",
            "repo": REPO,
            "work_id": WORK_ID,
            "actor": "operator",
            "reason": "Delivered before WorkflowRun tracking was enabled.",
        },
        requested_by="operator",
        state_path=state_path,
        workflow_registry=registry,
    )

    assert result["result"]["action"] == "closed-delivered"
    record = Path(result["result"]["completion_record"]["ref"])
    assert record.is_file()
    assert record.stat().st_mode & 0o222 == 0
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["schema"] == "cortex-work-close-delivered/v1"
    assert payload["actor"] == "operator"
    assert payload["reason"] == "Delivered before WorkflowRun tracking was enabled."
    assert payload["source_revisions"] == SOURCE_REVISIONS
    assert remote_calls == [
        {
            "repo": REPO,
            "pr_number": 8,
            "change": "demo",
            "required_issues": (12,),
            "todo_paths": ("docs/todo.md",),
        },
        {
            "repo": REPO,
            "pr_number": 8,
            "change": "demo",
            "required_issues": (12,),
            "todo_paths": ("docs/todo.md", ARCHIVED_TASKS),
        },
    ]
    assert registry.list_workflow_runs() == []


def test_close_delivered_fails_closed_when_remote_closure_is_incomplete(
    monkeypatch, tmp_path: Path
) -> None:
    authority = _authority()
    monkeypatch.setattr(work_actions, "load_work_authority", lambda **_kwargs: authority)
    monkeypatch.setattr(
        work_actions.GitHubDeliveryClient,
        "fetch_remote_closure",
        lambda self, **kwargs: _closure_facts(
            issue_states={12: "open"},
            todo_revisions={path: TODO_REVISION for path in kwargs["todo_paths"]},
        ),
    )
    monkeypatch.setattr(
        work_actions.GitHubDeliveryClient,
        "_commit_tree_paths",
        lambda self, **_kwargs: (ARCHIVED_TASKS,),
    )
    state_path = tmp_path / "jobs.json"

    with pytest.raises(RuntimeError, match="remote closure blocked: issue-not-closed"):
        work_actions.execute_work_action(
            args={
                "action": "close-delivered",
                "repo": REPO,
                "work_id": WORK_ID,
                "actor": "operator",
                "reason": "Remote issue is still open.",
            },
            requested_by="operator",
            state_path=state_path,
            workflow_registry=SimpleNamespace(list_workflow_runs=lambda: []),
        )

    assert not (tmp_path / "evidence" / "work-close-delivered").exists()


def test_close_delivered_refuses_a_work_item_with_any_workflow_run(
    monkeypatch, tmp_path: Path
) -> None:
    authority = _authority()
    monkeypatch.setattr(work_actions, "load_work_authority", lambda **_kwargs: authority)
    run = SimpleNamespace(repo=REPO, work_id=WORK_ID, status="superseded")

    with pytest.raises(RuntimeError, match="requires a work item with no WorkflowRun"):
        work_actions.execute_work_action(
            args={
                "action": "close-delivered",
                "repo": REPO,
                "work_id": WORK_ID,
                "actor": "operator",
                "reason": "Only work without a run can use this action.",
            },
            requested_by="operator",
            state_path=tmp_path / "jobs.json",
            workflow_registry=SimpleNamespace(list_workflow_runs=lambda: [run]),
        )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"merge_is_merge_commit": False}, "merge-commit-required"),
        ({"archive_present": False}, "openspec-archive-missing"),
        ({"todo_complete": False}, "todo-incomplete"),
    ],
)
def test_close_delivered_refuses_incomplete_delivery_proof(
    monkeypatch, tmp_path: Path, overrides: dict, reason: str
) -> None:
    authority = _authority()
    monkeypatch.setattr(work_actions, "load_work_authority", lambda **_kwargs: authority)
    monkeypatch.setattr(
        work_actions.GitHubDeliveryClient,
        "fetch_remote_closure",
        lambda self, **kwargs: _closure_facts(
            todo_revisions={path: TODO_REVISION for path in kwargs["todo_paths"]},
            **overrides,
        ),
    )
    monkeypatch.setattr(
        work_actions.GitHubDeliveryClient,
        "_commit_tree_paths",
        lambda self, **_kwargs: (ARCHIVED_TASKS,),
    )
    state_path = tmp_path / "jobs.json"

    with pytest.raises(RuntimeError, match=reason):
        work_actions.execute_work_action(
            args={
                "action": "close-delivered",
                "repo": REPO,
                "work_id": WORK_ID,
                "actor": "operator",
                "reason": "Delivery proof must be complete.",
            },
            requested_by="operator",
            state_path=state_path,
            workflow_registry=SimpleNamespace(list_workflow_runs=lambda: []),
        )

    assert not (tmp_path / "evidence" / "work-close-delivered").exists()


def test_workflow_provider_projects_immutable_operator_completion_without_run(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "jobs.json"
    _write_close_delivered_record(tmp_path)

    result = WorkflowRegistryProvider(REPO, state_path=state_path).scan()

    assert result.status == "ok"
    assert [source.kind for source in result.sources] == ["completion_record"]
    source = result.sources[0]
    assert source.ref.startswith("evidence/work-close-delivered/demo-")
    assert result.observations["workflow_links"] == {source.source_id: WORK_ID}
    assert result.observations["validated_completions"] == {
        WORK_ID: [
            {
                "source_revisions": SOURCE_REVISIONS,
                "pr_candidate": HEAD,
                "merge_revision": MERGE,
            }
        ]
    }


def test_operator_completion_record_satisfies_existing_strict_closure_gates(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "jobs.json"
    _write_close_delivered_record(tmp_path)
    workflow = WorkflowRegistryProvider(REPO, state_path=state_path).scan()
    sources = [
        WorkSource(
            source_id="github_issue:acme/demo#12",
            kind="github_issue",
            ref="acme/demo#12",
            revision="issue-revision",
            status="closed",
            confidence="confirmed",
            provider="github-terminal:acme/demo",
        ),
        WorkSource(
            source_id="github_pr:acme/demo#8",
            kind="github_pr",
            ref="acme/demo#8",
            revision="pr-revision",
            status="closed",
            confidence="confirmed",
            provider="github-terminal:acme/demo",
        ),
        WorkSource(
            source_id="openspec:acme/demo:demo",
            kind="openspec",
            ref="demo",
            revision="openspec-revision",
            status="archived",
            confidence="confirmed",
            provider="github-terminal:acme/demo",
        ),
        WorkSource(
            source_id="todo:acme/demo:docs/todo.md",
            kind="todo",
            ref="docs/todo.md",
            revision="todo-revision",
            status="active",
            confidence="confirmed",
            provider="repo:acme/demo",
        ),
        *workflow.sources,
    ]
    group = CorrelatedWork(
        work_id=WORK_ID, title="Demo", sources=tuple(sources), confidence="confirmed"
    )
    observations = {
        **workflow.observations,
        "remote_prs": [
            {
                "source_id": "github_pr:acme/demo#8",
                "merged_with_merge_commit": True,
                "candidate": HEAD,
                "merge_revision": MERGE,
            }
        ],
        "remote_todos": [
            {
                "work_id": WORK_ID,
                "path": "docs/todo.md",
                "complete": True,
                "revision": TODO_REVISION,
            },
            {
                "openspec_ref": "demo",
                "path": ARCHIVED_TASKS,
                "complete": True,
                "revision": "f" * 40,
            },
        ],
        "remote_openspec_observed": True,
        "remote_openspec": {"active": [], "archived": ["demo"]},
    }

    closure = _parse_closure_evidence(
        observations,
        correlation=SimpleNamespace(groups=(group,)),
        repo=REPO,
    )[WORK_ID]

    assert closure.complete is True


def test_coordinator_cli_exposes_close_delivered_actor_and_reason() -> None:
    parsed = cli._build_parser().parse_args(
        [
            "work",
            "close-delivered",
            WORK_ID,
            "--repo",
            REPO,
            "--actor",
            "operator",
            "--reason",
            "Delivered outside the pipeline.",
        ]
    )
    assert parsed.action == "close-delivered"
    assert parsed.actor == "operator"
    assert parsed.reason == "Delivered outside the pipeline."

    request = contract.build_request(
        req_type="work-action",
        args={
            "action": "close-delivered",
            "repo": REPO,
            "work_id": WORK_ID,
            "actor": "operator",
            "reason": "Delivered outside the pipeline.",
        },
        requested_by="operator",
    )
    assert contract.validate_request(request)["args"]["action"] == "close-delivered"


def test_coordinator_cli_requires_actor_and_reason_for_close_delivered() -> None:
    submitted = []
    result = cli.main(
        ["work", "close-delivered", WORK_ID, "--repo", REPO],
        control_read_status=lambda: {"degraded": False},
        control_submit_request=lambda *_args: submitted.append(_args) or "request-1",
        control_poll_done=lambda *_args, **_kwargs: {"status": "ok", "result": {}},
    )
    assert result == 2
    assert submitted == []


def test_coordinator_cli_forwards_close_delivered_operator_fields() -> None:
    submitted = []
    result = cli.main(
        [
            "work",
            "close-delivered",
            WORK_ID,
            "--repo",
            REPO,
            "--actor",
            "operator",
            "--reason",
            "Delivered outside the pipeline.",
        ],
        control_read_status=lambda: {"degraded": False},
        control_submit_request=lambda *args: submitted.append(args) or "request-1",
        control_poll_done=lambda *_args, **_kwargs: {
            "status": "ok",
            "result": {"action": "closed-delivered"},
        },
    )
    assert result == 0
    assert submitted[0][0] == "work-action"
    assert submitted[0][1]["action"] == "close-delivered"
    assert submitted[0][1]["actor"] == "operator"
    assert submitted[0][1]["reason"] == "Delivered outside the pipeline."


def test_work_help_documents_close_delivered_operator_requirements(capsys) -> None:
    assert "close-delivered" in umbrella_cli._WORK_HELP
    assert "--actor" in umbrella_cli._WORK_HELP
    assert "--reason" in umbrella_cli._WORK_HELP
    with pytest.raises(SystemExit) as exc:
        cli._build_parser().parse_args(["work", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "close-delivered" in output
    assert "--actor" in output
    assert "--reason" in output


def test_closure_openspec_gates_are_vacuous_without_mapped_openspec() -> None:
    """沒有 mapped OpenSpec 時 archive 條件不適用（#1037 同一規則），否則永遠投影不到 done。"""
    sources = (
        WorkSource(
            source_id="github_issue:acme/demo#12",
            kind="github_issue",
            ref="acme/demo#12",
            revision="issue-revision",
            status="closed",
            confidence="confirmed",
            provider="github-terminal:acme/demo",
        ),
        WorkSource(
            source_id="github_pr:acme/demo#8",
            kind="github_pr",
            ref="acme/demo#8",
            revision="pr-revision",
            status="closed",
            confidence="confirmed",
            provider="github-terminal:acme/demo",
        ),
    )
    group = CorrelatedWork(work_id=WORK_ID, title="Demo", sources=sources, confidence="confirmed")
    closure = _parse_closure_evidence(
        {
            "remote_prs": [],
            "remote_todos": [],
            "remote_openspec_observed": True,
            "remote_openspec": {"active": [], "archived": []},
            "validated_completions": {WORK_ID: [{}]},
        },
        correlation=SimpleNamespace(groups=(group,)),
        repo=REPO,
    )[WORK_ID]

    assert closure.remote_active_openspec_absent is True
    assert closure.remote_archive_present is True
