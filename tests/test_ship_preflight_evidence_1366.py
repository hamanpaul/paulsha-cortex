"""Regression coverage for ship-stage preflight evidence (#1366)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from test_ship_lane_no_openspec_911 import (
    HEAD,
    REPO,
    TODO_PATH,
    TREE,
    _pr_metadata,
    _snapshot,
    _start_run,
)


class FakeGitHubDeliveryClient:
    def __init__(self, *, runner) -> None:
        del runner

    def ensure_pr_metadata(self, **kwargs) -> None:
        assert kwargs["repo"] == REPO
        assert kwargs["pr_number"] == 8

    def fetch_delivery_facts(self, **kwargs):
        assert kwargs == {"repo": REPO, "pr_number": 8, "change": None}
        return SimpleNamespace(
            head=HEAD,
            active_openspec_absent=True,
            archive_present=True,
            openspec_required=False,
            copilot_reviews=(),
            review_threads=(),
        )

    def fetch_merge_status(self, **kwargs):
        assert kwargs == {"repo": REPO, "pr_number": 8}
        return SimpleNamespace(merged=False, pr_head=HEAD, merge_commit=None)

    def request_copilot(self, **kwargs) -> None:
        assert kwargs == {"repo": REPO, "pr_number": 8}


def _preflight_result(stage: str) -> PreflightResult:
    policy = CommandResult(
        ("policy", "--check"),
        1 if stage == "policy" else 0,
        "policy output " * 200,
        "policy stderr",
    )
    ci_parity = (
        None
        if stage == "policy"
        else CommandResult(
            ("preflight", "--pr", "8"),
            1 if stage == "ci-parity" else 0,
            "ci output " * 200,
            "ci stderr",
        )
    )
    return PreflightResult(
        passed=False,
        failed_stage=stage,
        policy=policy,
        ci_parity=ci_parity,
        head=HEAD,
        tree_hash=TREE,
    )


def _run_ship(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preflight: PreflightResult,
) -> dict[str, object]:
    snapshot = _snapshot(tmp_path / "snapshot.json", changes=())
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    _start_run(snapshot=snapshot, state=state, registry=registry)
    authority = work_actions.load_work_authority(
        repo=REPO,
        work_id="ship-lane-no-openspec",
        snapshot_path=snapshot,
    )
    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", FakeGitHubDeliveryClient)
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(work_actions, "run_preflight", lambda **_kwargs: preflight)

    def runner(_argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return work_actions._ship_action(
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
        snapshot_path=snapshot,
    )


@pytest.mark.parametrize("stage", ["ci-parity", "policy", "tree-race"])
def test_failed_ship_preflight_writes_evidence_and_attaches_its_path(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match=rf"^ship preflight failed: {stage} \(evidence: .+\.json\)$") as raised:
        _run_ship(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            preflight=_preflight_result(stage),
        )

    evidence_paths = list((tmp_path / "evidence" / "pr-preflight").glob("*.json"))
    assert len(evidence_paths) == 1
    assert str(evidence_paths[0]) in str(raised.value)
    envelope = json.loads(evidence_paths[0].read_text(encoding="utf-8"))
    payload = envelope["payload"]
    assert payload["schema"] == "cortex-pr-preflight/v1"
    assert payload["stage"] == "ship"
    assert payload["status"] == "needs_human"
    assert payload["reason"] == "ship-preflight-failed"
    assert payload["failed_stage"] == stage
    assert payload["head"] == HEAD
    assert payload["tree_hash"] == TREE
    assert payload["policy"]["argv"] == ["policy", "--check"]
    assert payload["policy"]["returncode"] == (1 if stage == "policy" else 0)
    assert len(payload["policy"]["stdout_tail"]) <= 2000
    if stage == "policy":
        assert payload["ci_parity"] is None
    else:
        assert payload["ci_parity"]["argv"] == ["preflight", "--pr", "8"]
        assert payload["ci_parity"]["returncode"] == (1 if stage == "ci-parity" else 0)
        assert len(payload["ci_parity"]["stdout_tail"]) <= 2000


def test_passed_ship_preflight_does_not_write_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passed = PreflightResult(
        passed=True,
        failed_stage=None,
        policy=CommandResult(("policy", "--check"), 0, "", ""),
        ci_parity=CommandResult(("preflight", "--pr", "8"), 0, "", ""),
        head=HEAD,
        tree_hash=TREE,
    )

    result = _run_ship(tmp_path=tmp_path, monkeypatch=monkeypatch, preflight=passed)

    assert result == {"action": "awaiting-copilot", "head": HEAD}
    assert not list((tmp_path / "evidence" / "pr-preflight").glob("*.json"))


def test_evidence_write_failure_preserves_ship_preflight_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_write(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(work_actions, "_preflight_result_evidence", fail_write)

    with pytest.raises(RuntimeError) as raised:
        _run_ship(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            preflight=_preflight_result("ci-parity"),
        )

    assert str(raised.value).startswith("ship preflight failed: ci-parity")
    assert "evidence write failed: OSError" in str(raised.value)
