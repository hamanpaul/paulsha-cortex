"""Issue #911 regression coverage for ship lane runs without OpenSpec changes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef


HEAD = "a" * 40
TREE = "b" * 40
REPO = "acme/demo"
WORK_ID = "ship-lane-no-openspec"
TODO_PATH = "docs/todo.md"


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{REPO}.git"],
            check=True,
        )


def _snapshot(
    path: Path,
    *,
    issues: tuple[int, ...] = (911,),
    prs: tuple[int, ...] = (8,),
    changes: tuple[str, ...] = (),
    todo_paths: tuple[str, ...] = (TODO_PATH,),
    source_revisions: tuple[str, ...] = ("issue:911@open",),
) -> Path:
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
                        "mapped_issues": list(issues),
                        "mapped_prs": list(prs),
                        "mapped_openspec": list(changes),
                        "mapped_todo_paths": list(todo_paths),
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _start_run(*, snapshot: Path, state: Path, registry: JobRegistry) -> str:
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": REPO, "work_id": WORK_ID},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
    )
    return started["result"]["run"]["run_id"]


def _journal_row(path: Path, run_id: str) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))["runs"][run_id]


def _pr_metadata(
    path: Path,
    *,
    title: str = "fix(ship): 放寬無 openspec 的 ship lane",
    body: str = "Closes #911",
) -> Path:
    path.write_text(
        json.dumps({"title": title, "body": body, "labels": ["bug"]}),
        encoding="utf-8",
    )
    return path


def test_ship_without_openspec_binds_none_change_and_reaches_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", changes=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = _start_run(snapshot=snapshot, state=state, registry=registry)
    authority = work_actions.load_work_authority(
        repo=REPO,
        work_id=WORK_ID,
        snapshot_path=snapshot,
    )
    events: list[str] = []
    remote_calls: list[dict[str, object]] = []
    preflight_calls: list[dict[str, object]] = []
    commands: list[list[str]] = []

    def fail_if_archive_checked(**_kwargs):
        pytest.fail("ship without OpenSpec must skip local archive validation")

    class GitHub:
        def __init__(self, *, runner):
            del runner

        def ensure_pr_metadata(self, **kwargs):
            events.append("metadata")
            assert kwargs["repo"] == REPO
            assert kwargs["pr_number"] == 8

        def fetch_delivery_facts(self, **kwargs):
            events.append("delivery")
            remote_calls.append(dict(kwargs))
            return SimpleNamespace(
                head=HEAD,
                active_openspec_absent=True,
                archive_present=True,
                copilot_reviews=(),
                openspec_required=False,
            )

        def fetch_merge_status(self, **kwargs):
            events.append("merge-status")
            assert kwargs == {"repo": REPO, "pr_number": 8}
            return SimpleNamespace(merged=False, pr_head=HEAD, merge_commit=None)

        def request_copilot(self, **kwargs):
            events.append("request-copilot")
            assert kwargs == {"repo": REPO, "pr_number": 8}

    def fake_preflight(**kwargs):
        events.append("preflight")
        preflight_calls.append(dict(kwargs))
        return PreflightResult(
            passed=True,
            failed_stage=None,
            policy=CommandResult(("policy",), 0, "", ""),
            ci_parity=CommandResult(("preflight",), 0, "", ""),
            head=HEAD,
            tree_hash=TREE,
        )

    def runner(argv, **kwargs):
        commands.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(work_actions, "_validate_local_archive_inputs", fail_if_archive_checked)
    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(work_actions, "run_preflight", fake_preflight)

    result = work_actions._ship_action(
        args={
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": None,
            "todo_paths": [TODO_PATH],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        authority=authority,
        runner=runner,
        now=lambda: 200,
        state_path=state,
        workflow_registry=registry,
    )

    assert result == {"action": "awaiting-copilot", "head": HEAD}
    row = _journal_row(state, run_id)
    assert row["delivery_binding"] == {
        "pr_number": 8,
        "change": None,
        "todo_paths": [TODO_PATH],
    }
    assert row["ship"]["phase"] == "review-requested"
    assert row["ship"]["change"] is None
    assert events[:2] == ["metadata", "preflight"]
    assert len(preflight_calls) == 1
    assert remote_calls == [{"repo": REPO, "pr_number": 8, "change": None}]
    assert "request-copilot" in events
    assert commands == []
    assert "needs_human" not in registry.get_workflow_run(run_id).facets


def test_review_attest_without_openspec_or_pr_writes_null_pr_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tmp_path / "snapshot.json", prs=(), changes=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = _start_run(snapshot=snapshot, state=state, registry=registry)
    authority = work_actions.load_work_authority(
        repo=REPO,
        work_id=WORK_ID,
        snapshot_path=snapshot,
    )
    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=HEAD,
        verified_head=HEAD,
        pr_refs=(),
        gate_refs=(GateEvidenceRef("foreign-review", "/evidence/foreign.json", "f" * 64),),
        gate_status="passed",
    )

    class GitHub:
        def __init__(self, *, runner):
            del runner

        def fetch_delivery_facts(self, **kwargs):
            raise AssertionError(
                "review-attest before PR creation must not fetch PR delivery facts"
            )

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)

    try:
        result = work_actions._review_attest_action(
            args={
                "action": "review-attest",
                "repo": REPO,
                "work_id": WORK_ID,
                "actor": "maintainer@example",
                "verdict": "approved",
                "summary": "Exact-HEAD adversarial review passed before PR creation.",
                "findings": [],
            },
            requested_by="operator",
            authority=authority,
            runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
            now_epoch=210,
            state_path=state,
            workflow_registry=registry,
        )
    except Exception as exc:  # pragma: no cover - RED path on current implementation
        pytest.fail(
            "review-attest should allow exact-HEAD attestation before PR creation "
            f"when no OpenSpec is mapped: {exc}"
        )

    persisted = json.loads(Path(result["ref"]).read_text(encoding="utf-8"))
    assert result["action"] == "review-attested"
    assert result["head"] == HEAD
    assert persisted["pr_number"] is None
    assert persisted["candidate"] == HEAD
    assert {ref.kind for ref in registry.get_workflow_run(run_id).gate_refs} == {
        "foreign-review",
        "maintainer-review",
    }


def test_multi_openspec_stop_tells_operator_to_unlink_before_resume(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        changes=(WORK_ID, "other-change"),
        source_revisions=("issue:911@open", "openspec:work@1"),
    )
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run_id = _start_run(snapshot=snapshot, state=state, registry=registry)
    authority = work_actions.load_work_authority(
        repo=REPO,
        work_id=WORK_ID,
        snapshot_path=snapshot,
    )

    result = work_actions._ship_action(
        args={
            "repo_root": str(tmp_path),
            "pr_number": 8,
            "change": WORK_ID,
            "todo_paths": [TODO_PATH],
            "pr_metadata_path": str(_pr_metadata(tmp_path / "pr.json")),
        },
        authority=authority,
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
        now=lambda: 200,
        state_path=state,
        workflow_registry=registry,
    )

    assert result == {
        "action": "needs_human",
        "reason": "multiple-delivery-targets-unsupported",
    }
    reason_payload = registry.get_workflow_run(run_id).needs_human_reason or {}
    rendered = json.dumps(reason_payload, ensure_ascii=False)
    assert "cortex work unlink" in rendered
    assert "resume" in rendered
