from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from diagnostic_fixtures import fixture_needs_human_reason
from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.github_delivery import (
    DeliveryFacts,
    GitHubCheck,
    MergeStatus,
)
from paulsha_cortex.coordinator.preflight import CommandResult, PreflightResult
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef


HEAD = "a" * 40
ALT_HEAD = "c" * 40
OTHER_HEAD = "d" * 40
TREE = "b" * 40


@dataclass
class ReviewRunEnv:
    snapshot: Path
    state: Path
    registry: JobRegistry
    authority: Any
    run_id: str
    workflow_step_ids: list[str]
    maintainer: dict[str, str]
    foreign_path: Path
    foreign_hash: str
    foreign_payload: dict[str, Any]
    binding: dict[str, Any]
    pr_metadata_path: Path
    repo_root: Path


def _only_journal_row(state: Path) -> dict[str, Any]:
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema"] == "cortex-delivery-journal/v1"
    assert len(payload["runs"]) == 1
    return next(iter(payload["runs"].values()))


def _journal_row(state: Path, run_id: str) -> dict[str, Any]:
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["schema"] == "cortex-delivery-journal/v1"
    return payload["runs"][run_id]


def _initialize_delivery_journal(*, snapshot: Path, state: Path) -> dict[str, Any]:
    authority = work_actions.load_work_authority(
        repo="acme/demo",
        work_id="demo",
        snapshot_path=snapshot,
    )
    registry = JobRegistry(state_path=state.parent / "jobs.json")
    work_actions._load_work_run(
        state_path=state,
        workflow_registry=registry,
        authority=authority,
    )
    return _only_journal_row(state)


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "remote",
                "add",
                "origin",
                f"git@github.com:{repo}.git",
            ],
            check=True,
        )
    return root


def _pr_metadata(
    path: Path,
    *,
    title: str = "fix(work): 修正工作流程",
    body: str = "Closes #12",
) -> Path:
    path.write_text(
        json.dumps({"title": title, "body": body, "labels": ["enhancement"]}),
        encoding="utf-8",
    )
    return path


def _snapshot(
    path: Path,
    *,
    issues: tuple[int, ...] = (12,),
    source_revisions: tuple[str, ...] = ("issue:12@open", "openspec:demo@1"),
    provider_revision: str = "gh-1",
    auto_label: bool = True,
    prs: tuple[int, ...] = (8,),
    changes: tuple[str, ...] = ("demo",),
    todo_paths: tuple[str, ...] = ("docs/todo.md",),
) -> Path:
    _init_repo(path.parent)
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": provider_revision,
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "demo",
                        "mapped_issues": list(issues),
                        "mapped_prs": list(prs),
                        "mapped_openspec": list(changes),
                        "mapped_todo_paths": list(todo_paths),
                        "confirmed_todo": True,
                        "auto_label": auto_label,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _default_checks() -> tuple[GitHubCheck, ...]:
    return (GitHubCheck("pytest", "completed", "success"),)


def _default_preflight() -> PreflightResult:
    return PreflightResult(
        passed=True,
        failed_stage=None,
        policy=CommandResult(("policy",), 0, "", ""),
        ci_parity=CommandResult(("preflight",), 0, "", ""),
        head=HEAD,
        tree_hash=TREE,
    )


def _default_delivery_facts(
    *,
    checks: tuple[GitHubCheck, ...] | None = None,
) -> DeliveryFacts:
    return DeliveryFacts(
        head=HEAD,
        mergeable=True,
        mergeable_state="clean",
        checks=checks or _default_checks(),
        copilot_reviews=(),
        review_threads=(),
        closing_issues=(12,),
        active_openspec_absent=True,
        archive_present=True,
    )


def _authorization_file_state(path: str | Path) -> tuple[bytes, int]:
    file_path = Path(path)
    return file_path.read_bytes(), file_path.stat().st_mode & 0o777


def _merge_dir(state: Path) -> Path:
    return state.resolve().parent / "evidence" / "merge-authorization"


def _write_authorization_wrapper(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    digest = work_actions.verification.canonical_json_hash(payload)
    wrapper = {"payload": payload, "hash": digest}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(wrapper, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o444)
    return {"payload": payload, "hash": digest, "path": str(path)}


def _bootstrap_review_env(
    tmp_path: Path,
    *,
    maintainer_candidate: str = HEAD,
) -> ReviewRunEnv:
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    started = work_actions.execute_work_action(
        args={"action": "start", "repo": "acme/demo", "work_id": "demo"},
        requested_by="operator",
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
    )
    run_id = started["result"]["run"]["run_id"]
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    authority = work_actions.load_work_authority(
        repo="acme/demo",
        work_id="demo",
        snapshot_path=snapshot,
    )
    maintainer_body = {
        "schema": "cortex-maintainer-review/v1",
        "repo": authority.repo,
        "work_id": authority.work_id,
        "run_id": run_id,
        "authority_digest": work_actions.work_authority_digest(authority),
        "pr_number": 8,
        "candidate": maintainer_candidate,
        "actor": "maintainer",
        "requested_by": "operator",
        "verdict": "approved",
        "summary": "Exact-HEAD review passed.",
        "findings": [],
        "reviewed_at_epoch": 205.0,
    }
    maintainer = work_actions._maintainer_review_record(maintainer_body, state_path=state)
    foreign_payload = {"state": "passed", "candidate": HEAD}
    foreign_path = tmp_path / "foreign.json"
    foreign_path.write_text(json.dumps(foreign_payload), encoding="utf-8")
    foreign_hash = work_actions.verification.canonical_json_hash(foreign_payload)
    for phase in ("plan", "build", "verify", "review"):
        registry._manager_update_workflow_run(run_id, current_phase=phase)
    registry._manager_update_workflow_run(
        run_id,
        candidate_head=HEAD,
        verified_head=HEAD,
        pr_refs=("acme/demo#8",),
        gate_refs=(
            GateEvidenceRef("foreign-review", str(foreign_path), foreign_hash),
            GateEvidenceRef("maintainer-review", maintainer["ref"], maintainer["hash"]),
        ),
        gate_status="passed",
        facets=("needs_human",),
        needs_human_reason=fixture_needs_human_reason(),
    )
    row = _initialize_delivery_journal(snapshot=snapshot, state=state)
    return ReviewRunEnv(
        snapshot=snapshot,
        state=state,
        registry=registry,
        authority=authority,
        run_id=run_id,
        workflow_step_ids=list(row["workflow_step_ids"]),
        maintainer=maintainer,
        foreign_path=foreign_path,
        foreign_hash=foreign_hash,
        foreign_payload=foreign_payload,
        binding={"pr_number": 8, "change": "demo", "todo_paths": ["docs/todo.md"]},
        pr_metadata_path=_pr_metadata(tmp_path / "pr.json"),
        repo_root=tmp_path,
    )


def _legacy_v1_body(
    env: ReviewRunEnv,
    *,
    authority_digest: str | None = None,
    head: str = HEAD,
) -> dict[str, Any]:
    return {
        "schema": "cortex-merge-authorization/v1",
        "run_id": env.run_id,
        "workflow_step_ids": env.workflow_step_ids,
        "repo": env.authority.repo,
        "work_id": env.authority.work_id,
        "authority_digest": authority_digest or work_actions.work_authority_digest(env.authority),
        **env.binding,
        "head": head,
        "tree_hash": TREE,
        "copilot_requested_at_epoch": 200.0,
        "copilot_review_id": 9,
        "copilot_hash": "1" * 64,
        "foreign_review_path": str(env.foreign_path),
        "foreign_review_hash": env.foreign_hash,
        "preflight_hash": "3" * 64,
        "checks_hash": "4" * 64,
    }


def _persist_legacy_v1(
    env: ReviewRunEnv,
    *,
    authority_digest: str | None = None,
    head: str = HEAD,
) -> dict[str, Any]:
    return work_actions._authorization_record(
        _legacy_v1_body(env, authority_digest=authority_digest, head=head),
        state_path=env.state,
    )


def _v2_body(
    env: ReviewRunEnv,
    *,
    checks: tuple[GitHubCheck, ...] | None = None,
    superseded: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = {
        "schema": "cortex-merge-authorization/v2",
        "run_id": env.run_id,
        "workflow_step_ids": env.workflow_step_ids,
        "repo": env.authority.repo,
        "work_id": env.authority.work_id,
        "authority_digest": work_actions.work_authority_digest(env.authority),
        **env.binding,
        "head": HEAD,
        "tree_hash": TREE,
        "foreign_review_path": str(env.foreign_path),
        "foreign_review_hash": env.foreign_hash,
        "preflight_hash": work_actions._preflight_hash(_default_preflight()),
        "checks_hash": work_actions._checks_hash(
            _default_delivery_facts(checks=checks)
        ),
        "review_kind": "maintainer-review",
        "review_ref": env.maintainer["ref"],
        "review_hash": env.maintainer["hash"],
    }
    if superseded is not None:
        body["superseded_authorization_ref"] = superseded["path"]
        body["superseded_authorization_hash"] = superseded["hash"]
    return body


def _set_ship(
    env: ReviewRunEnv,
    *,
    phase: str,
    reason: str | None = None,
    authorization: dict[str, Any] | None = None,
) -> None:
    payload = json.loads(env.state.read_text(encoding="utf-8"))
    row = payload["runs"][env.run_id]
    ship = {
        "phase": phase,
        "head": HEAD,
        "tree_hash": TREE,
        "fix_rounds": 0,
        "pr_number": env.binding["pr_number"],
        "change": env.binding["change"],
        "todo_paths": list(env.binding["todo_paths"]),
    }
    if reason is not None:
        ship["reason"] = reason
    if authorization is not None:
        ship["merge_authorization"] = authorization
    row["ship"] = ship
    env.state.write_text(json.dumps(payload), encoding="utf-8")


def _ship_args(env: ReviewRunEnv, **overrides: Any) -> dict[str, Any]:
    args = {
        "action": "ship",
        "repo": env.authority.repo,
        "work_id": env.authority.work_id,
        "repo_root": str(env.repo_root),
        "pr_number": env.binding["pr_number"],
        "change": env.binding["change"],
        "todo_paths": list(env.binding["todo_paths"]),
        "foreign_review_path": str(env.foreign_path),
        "foreign_review_hash": env.foreign_hash,
        "maintainer_review_path": env.maintainer["ref"],
        "maintainer_review_hash": env.maintainer["hash"],
        "pr_metadata_path": str(env.pr_metadata_path),
    }
    args.update(overrides)
    return args


def _ship(env: ReviewRunEnv, **overrides: Any) -> dict[str, Any]:
    return work_actions.execute_work_action(
        args=_ship_args(env, **overrides),
        requested_by="operator",
        snapshot_path=env.snapshot,
        state_path=env.state,
        now=lambda: 210,
        workflow_registry=env.registry,
    )


def _patch_ship_runtime(
    monkeypatch: pytest.MonkeyPatch,
    env: ReviewRunEnv,
    *,
    merge_callback: Callable[[dict[str, Any], dict[str, bool]], Any],
    checks: tuple[GitHubCheck, ...] | None = None,
) -> SimpleNamespace:
    merged = {"value": False}
    merge_calls: list[dict[str, Any]] = []

    class GitHub:
        def __init__(self, *, runner):
            pass

        def ensure_pr_metadata(self, **kwargs):
            pass

        def fetch_delivery_facts(self, **kwargs):
            return DeliveryFacts(
                head=HEAD,
                mergeable=True,
                mergeable_state="clean",
                checks=checks or _default_checks(),
                copilot_reviews=(),
                review_threads=(),
                closing_issues=tuple(env.authority.mapped_issues),
                active_openspec_absent=True,
                archive_present=True,
            )

        def fetch_merge_status(self, **kwargs):
            return MergeStatus(
                merged=merged["value"],
                pr_head=HEAD,
                merge_commit="e" * 40 if merged["value"] else None,
            )

    class Orchestrator:
        def __init__(self, *, github, now):
            pass

        def merge_if_ready(self, **kwargs):
            merge_calls.append(kwargs)
            return merge_callback(kwargs, merged)

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)
    monkeypatch.setattr(work_actions, "ShipOrchestrator", Orchestrator)
    monkeypatch.setattr(
        work_actions,
        "_validate_foreign_review",
        lambda *args, **kwargs: env.foreign_payload,
    )
    monkeypatch.setattr(work_actions, "load_preflight_command", lambda: ("preflight",))
    monkeypatch.setattr(
        work_actions,
        "run_preflight",
        lambda **kwargs: _default_preflight(),
    )
    return SimpleNamespace(merge_calls=merge_calls, merged=merged)


def _merge_success(kwargs: dict[str, Any], merged: dict[str, bool]) -> SimpleNamespace:
    assert kwargs["copilot"] is None
    assert kwargs["maintainer_review"].path
    merged["value"] = True
    return SimpleNamespace(expected_head=HEAD, expected_tree_hash=TREE)


def _merge_forbidden(kwargs: dict[str, Any], merged: dict[str, bool]) -> SimpleNamespace:
    raise AssertionError("merge_if_ready must not be called")


def _tamper_authorization(auth: dict[str, Any], *, writable: bool) -> None:
    path = Path(auth["path"])
    if writable:
        path.chmod(0o644)
        return
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    wrapper["payload"]["copilot_review_id"] = wrapper["payload"]["copilot_review_id"] + 1
    path.chmod(0o644)
    path.write_text(
        json.dumps(wrapper, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o444)


def _assert_superseded_success(
    env: ReviewRunEnv,
    legacy: dict[str, Any],
    *,
    expect_superseded: bool,
) -> dict[str, Any]:
    row = _journal_row(env.state, env.run_id)
    ship = row["ship"]
    assert ship["phase"] == "merged"
    authorization = ship["merge_authorization"]
    assert authorization["payload"]["schema"] == "cortex-merge-authorization/v2"
    assert authorization["payload"]["review_kind"] == "maintainer-review"
    assert Path(authorization["path"]).name == (
        f"{env.run_id}-{HEAD.lower()}-{authorization['hash']}.json"
    )
    refs = work_actions._trusted_evidence_refs(authorization)
    assert refs == (
        {
            "kind": "preflight",
            "ref": f"head:{HEAD}:tree:{TREE}",
            "hash": authorization["payload"]["preflight_hash"],
        },
        {
            "kind": "foreign_review",
            "ref": str(env.foreign_path),
            "hash": env.foreign_hash,
        },
        {
            "kind": "maintainer-review",
            "ref": env.maintainer["ref"],
            "hash": env.maintainer["hash"],
        },
        {
            "kind": "merge_authorization",
            "ref": authorization["path"],
            "hash": authorization["hash"],
        },
    )
    if expect_superseded:
        assert authorization["payload"]["superseded_authorization_ref"] == legacy["path"]
        assert authorization["payload"]["superseded_authorization_hash"] == legacy["hash"]
        assert ship["superseded_merge_authorization"] == legacy
    else:
        assert "superseded_authorization_ref" not in authorization["payload"]
        assert "superseded_authorization_hash" not in authorization["payload"]
        assert "superseded_merge_authorization" not in ship
    return authorization


@pytest.mark.parametrize(
    "legacy_authority_digest",
    [
        None,
        "f" * 64,
    ],
    ids=["current-authority", "stale-authority"],
)
def test_needs_human_copilot_v1_is_superseded_by_maintainer_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    legacy_authority_digest: str | None,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env, authority_digest=legacy_authority_digest)
    before_state = _authorization_file_state(legacy["path"])
    _set_ship(
        env,
        phase="needs_human",
        reason="copilot-review-timeout",
        authorization=legacy,
    )
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_success,
    )

    result = _ship(env)

    assert result["result"]["action"] == "merged-awaiting-closure"
    assert result["result"]["review_kind"] == "maintainer-review"
    _assert_superseded_success(env, legacy, expect_superseded=True)
    assert _authorization_file_state(legacy["path"]) == before_state
    assert len(runtime.merge_calls) == 1


def test_merge_authorized_copilot_v1_can_finish_via_maintainer_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env)
    before_state = _authorization_file_state(legacy["path"])
    _set_ship(env, phase="merge-authorized", authorization=legacy)
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_success,
    )

    result = _ship(env)

    assert result["result"]["action"] == "merged-awaiting-closure"
    _assert_superseded_success(env, legacy, expect_superseded=True)
    assert _authorization_file_state(legacy["path"]) == before_state
    assert len(runtime.merge_calls) == 1


def test_orphan_legacy_v1_file_does_not_bind_into_new_maintainer_v2(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env)
    before_state = _authorization_file_state(legacy["path"])
    _set_ship(env, phase="needs_human", reason="copilot-review-timeout")
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_success,
    )

    result = _ship(env)

    assert result["result"]["action"] == "merged-awaiting-closure"
    _assert_superseded_success(env, legacy, expect_superseded=False)
    assert _authorization_file_state(legacy["path"]) == before_state
    assert len(runtime.merge_calls) == 1


def test_retry_after_failed_merge_reuses_same_v2_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env)
    _set_ship(
        env,
        phase="needs_human",
        reason="copilot-review-timeout",
        authorization=legacy,
    )
    attempts = {"count": 0}

    def merge_retry(kwargs: dict[str, Any], merged: dict[str, bool]) -> SimpleNamespace:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("merge still pending")
        return _merge_success(kwargs, merged)

    _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=merge_retry,
    )

    with pytest.raises(RuntimeError, match="merge still pending"):
        _ship(env)

    first_row = _journal_row(env.state, env.run_id)
    first_auth = first_row["ship"]["merge_authorization"]
    assert first_row["ship"]["phase"] == "merge-authorized"
    assert first_row["ship"]["superseded_merge_authorization"] == legacy

    result = _ship(env)

    assert result["result"]["action"] == "merged-awaiting-closure"
    second_auth = _assert_superseded_success(env, legacy, expect_superseded=True)
    assert second_auth["path"] == first_auth["path"]
    assert second_auth["hash"] == first_auth["hash"]
    assert list(_merge_dir(env.state).glob(f"{env.run_id}-{HEAD.lower()}-*.json")) == [
        Path(second_auth["path"])
    ]
    assert attempts["count"] == 2


def test_mismatched_legacy_v1_head_fails_closed_before_merge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env, head=ALT_HEAD)
    _set_ship(
        env,
        phase="needs_human",
        reason="copilot-review-timeout",
        authorization=legacy,
    )
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_forbidden,
    )

    with pytest.raises(
        RuntimeError,
        match="persisted merge authorization differs from current gate evidence",
    ):
        _ship(env)

    assert runtime.merge_calls == []
    assert list(_merge_dir(env.state).glob(f"{env.run_id}-{HEAD.lower()}-*.json")) == []


@pytest.mark.parametrize("writable", [False, True], ids=["tampered", "writable"])
def test_tampered_or_writable_legacy_v1_fails_closed_before_merge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    writable: bool,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env)
    _set_ship(
        env,
        phase="needs_human",
        reason="copilot-review-timeout",
        authorization=legacy,
    )
    _tamper_authorization(legacy, writable=writable)
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_forbidden,
    )

    with pytest.raises(
        RuntimeError,
        match="persisted merge authorization differs from current gate evidence",
    ):
        _ship(env)

    assert runtime.merge_calls == []
    assert list(_merge_dir(env.state).glob(f"{env.run_id}-{HEAD.lower()}-*.json")) == []


@pytest.mark.parametrize("variant", ["hash-mismatch", "candidate-mismatch"])
def test_maintainer_review_must_match_exact_head_before_superseding_v1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    variant: str,
) -> None:
    env = _bootstrap_review_env(
        tmp_path,
        maintainer_candidate=OTHER_HEAD if variant == "candidate-mismatch" else HEAD,
    )
    legacy = _persist_legacy_v1(env)
    before_state = _authorization_file_state(legacy["path"])
    _set_ship(
        env,
        phase="needs_human",
        reason="copilot-review-timeout",
        authorization=legacy,
    )
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_forbidden,
    )

    with pytest.raises(RuntimeError, match="maintainer review does not authorize exact HEAD"):
        _ship(
            env,
            maintainer_review_hash="0" * 64 if variant == "hash-mismatch" else env.maintainer["hash"],
        )

    assert runtime.merge_calls == []
    assert list(_merge_dir(env.state).glob(f"{env.run_id}-{HEAD.lower()}-*.json")) == []
    assert _authorization_file_state(legacy["path"]) == before_state


def test_existing_v2_payload_must_match_current_checks_before_merge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    authorization = work_actions._authorization_record(
        _v2_body(env),
        state_path=env.state,
    )
    _set_ship(env, phase="merge-authorized", authorization=authorization)
    merge_dir_before = sorted(path.name for path in _merge_dir(env.state).iterdir())
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_forbidden,
        checks=(
            GitHubCheck("pytest", "completed", "success"),
            GitHubCheck("lint", "completed", "success"),
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="persisted merge authorization differs from current gate evidence",
    ):
        _ship(env)

    assert runtime.merge_calls == []
    assert sorted(path.name for path in _merge_dir(env.state).iterdir()) == merge_dir_before


def test_superseded_pair_replay_requires_complete_valid_v1_binding(
    tmp_path: Path,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env)
    authorization = _write_authorization_wrapper(
        _merge_dir(env.state) / "manual-v2.json",
        _v2_body(env, superseded=legacy),
    )

    assert work_actions._authorization_identity_matches(
        authorization,
        active={"run_id": env.run_id, "workflow_step_ids": env.workflow_step_ids},
        authority=env.authority,
        binding=env.binding,
        head=HEAD,
        tree_hash=TREE,
    )

    missing_hash = _write_authorization_wrapper(
        _merge_dir(env.state) / "manual-v2-missing-hash.json",
        {
            key: value
            for key, value in authorization["payload"].items()
            if key != "superseded_authorization_hash"
        },
    )
    assert not work_actions._authorization_identity_matches(
        missing_hash,
        active={"run_id": env.run_id, "workflow_step_ids": env.workflow_step_ids},
        authority=env.authority,
        binding=env.binding,
        head=HEAD,
        tree_hash=TREE,
    )

    _tamper_authorization(legacy, writable=False)
    assert not work_actions._authorization_identity_matches(
        authorization,
        active={"run_id": env.run_id, "workflow_step_ids": env.workflow_step_ids},
        authority=env.authority,
        binding=env.binding,
        head=HEAD,
        tree_hash=TREE,
    )

    unknown_schema_body = {
        **_legacy_v1_body(env, head=ALT_HEAD),
        "schema": "cortex-merge-authorization/v9",
    }
    with pytest.raises(ValueError, match="merge authorization identity malformed"):
        work_actions._authorization_record(unknown_schema_body, state_path=env.state)

    assert Path(legacy["path"]).name == f"{env.run_id}-{HEAD.lower()}.json"


def test_merge_authorized_stale_v1_authority_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env = _bootstrap_review_env(tmp_path)
    legacy = _persist_legacy_v1(env, authority_digest="f" * 64)
    before_state = _authorization_file_state(legacy["path"])
    _set_ship(env, phase="merge-authorized", authorization=legacy)
    runtime = _patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=_merge_forbidden,
    )

    with pytest.raises(ValueError, match="ship merge-authorized state malformed"):
        _ship(env)

    assert runtime.merge_calls == []
    assert list(_merge_dir(env.state).glob(f"{env.run_id}-{HEAD.lower()}-*.json")) == []
    assert _authorization_file_state(legacy["path"]) == before_state
