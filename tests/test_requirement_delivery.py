from __future__ import annotations

import copy
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from paulsha_cortex.coordinator import completion, claim, review, verification
from paulsha_cortex.coordinator.github_delivery import RemoteClosureFacts
from paulsha_cortex import runtime_attestation
from paulsha_cortex.coordinator.requirement_delivery import (
    INDEX_SCHEMA,
    IndexConflict,
    inspect_delivery,
    read_index,
    reconcile_delivery,
    validate_manifest,
)


HEAD = "1" * 40
MERGE = "2" * 40
REPO = "hamanpaul/paulsha-cortex"
WORK = "requirement-delivery-accounting"
PROFILE_KEY = "epk:v1:observed:" + "3" * 64
CONFIG_REVISION = runtime_attestation.configuration_revision({"config": "fixture"})
ARTIFACT_SHA = "5" * 64
NOW = 1_790_380_800.0


class _GitHub:
    def __init__(self, *, merge: str = MERGE, head: str = HEAD, issue_state: str = "closed") -> None:
        self.merge = merge
        self.head = head
        self.issue_state = issue_state
        self.calls: list[dict] = []

    def fetch_remote_closure(self, **kwargs):
        self.calls.append(kwargs)
        return RemoteClosureFacts(
            merge_commit=self.merge,
            pr_head=self.head,
            merge_parents=(MERGE, self.head),
            default_head=self.merge,
            merge_is_ancestor=True,
            merge_is_merge_commit=True,
            issue_states={845: self.issue_state},
            active_openspec_absent=True,
            archive_present=True,
            todo_complete=True,
            todo_revisions={"docs/todo.md": "6" * 40},
            completion_record_valid=True,
        )


def _authority(*, work_id: str = WORK, last_success: float = NOW - 1, snapshot_hash: str = "b" * 64, source_revision: str = "a" * 40):
    return claim.WorkAuthority._verified(
        repo=REPO,
        work_id=work_id,
        mapped_issues=(845,),
        mapped_prs=(7,),
        mapped_openspec=("delivery-work",),
        mapped_todo_paths=("docs/todo.md",),
        confirmed_todo=True,
        auto_label=False,
        source_revisions=(source_revision,),
        provider_revision="monitor-revision-1",
        last_success_epoch=last_success,
        snapshot_hash=snapshot_hash,
    )


def _authority_record(authority, *, run_id: str = "run-1", merge: str = MERGE) -> dict:
    return {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "snapshot_hash": authority.snapshot_hash,
        "provider_id": authority.github_provider_id,
        "provider_revision": authority.github_provider_revision,
        "source_revisions": list(authority.source_revisions),
        "mapped_issues": list(authority.mapped_issues),
        "mapped_prs": list(authority.mapped_prs),
        "mapped_openspec": list(authority.mapped_openspec),
        "mapped_todo_paths": list(authority.mapped_todo_paths),
        "pr_number": 7,
        "change": "delivery-work",
        "todo_paths": ["docs/todo.md"],
        "merge_commit": merge,
        "run_id": run_id,
        "workflow_step_ids": ["build-1", "verify-1", "review-1", "ship-1"],
        "trusted_evidence_refs": [
            {"kind": "foreign_review", "ref": "review:review-1", "hash": "c" * 64},
            {"kind": "preflight", "ref": "tree:" + HEAD, "hash": "d" * 64},
            {"kind": "merge_authorization", "ref": "run:" + run_id, "hash": "7" * 64},
            {"kind": "maintainer-review", "ref": "approval:9", "hash": "8" * 64},
        ],
    }


def _write_completion(root: Path, *, authority=None, run_id: str = "run-1", candidate: str = HEAD, slice_id: str = "delivery-slice", same_review_domain: bool = False) -> dict:
    authority = authority or _authority()
    verify_ref = verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "candidate": candidate,
            "status": "reviewing",
            "summary": "verification-succeeded",
            "details": {"ok": True},
        },
        coordinator_root=root,
    )
    review_payload = review.build_gate_evaluation(
        slice_id=slice_id,
        state="passed",
        reason="accepted",
        builder_job_id="builder-1",
        reviewer_job_id="reviewer-1",
        candidate=candidate,
        launch_identity={
            "builder": {"executor": "codex", "model_id": "builder", "independence_domain": "builder-domain"},
            "reviewer": {"executor": "claude", "model_id": "reviewer", "independence_domain": "builder-domain" if same_review_domain else "reviewer-domain"},
        },
    )
    review_ref = review.write_gate_evaluation(review_payload, coordinator_root=root)
    payload = {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": slice_id,
        "spec_hash": "e" * 64,
        "plan_hash": "f" * 64,
        "verification_hash": "1" * 64,
        "builder_job_id": "builder-1",
        "reviewer_job_id": "reviewer-1",
        "dispatch_base": "a" * 40,
        "candidate": candidate,
        "target_branch": "main",
        "target_remote": "origin",
        "target_ref": "refs/remotes/origin/main",
        "target_ref_sha": MERGE,
        "verification_evidence_path": verify_ref["path"],
        "verification_evidence_hash": verify_ref["hash"],
        "review_policy": "required",
        "docs_class": "code",
        "review_evaluation_path": review_ref["path"],
        "review_evaluation_hash": review_ref["hash"],
        "completed_at": "2026-09-25T00:00:00+00:00",
        "work_authority": _authority_record(authority, run_id=run_id),
    }
    ref = completion.write_completion_record(payload, coordinator_root=root)
    return {"locator": str(Path(ref["path"]).relative_to(root)), "sha256": ref["hash"]}


def _target(*, profile_key: str = PROFILE_KEY, config_revision: str = CONFIG_REVISION) -> dict:
    return {
        "repo": REPO,
        "candidate_sha": HEAD,
        "artifact_sha256": ARTIFACT_SHA,
        "source_revision": MERGE,
        "service": "manager",
        "instance": "default",
        "profile_key": profile_key,
        "config_revision": config_revision,
    }


def _write_runtime(root: Path, *, target=None, pid: int = 555, state_name: str = "runtime-0") -> None:
    target = target or _target()
    runtime_attestation.record_runtime_startup(
        service="manager",
        instance="default",
        state_root=root / state_name,
        configuration={"config": "fixture"},
        artifact={
            "kind": "installed-wheel",
            "package": "paulsha-cortex",
            "package_version": "1.0.0",
            "source_revision": target["source_revision"],
            "sha256": target["artifact_sha256"],
        },
        started_at="2026-09-25T00:00:00+00:00",
        pid=pid,
        config_components={"profile_key": target["profile_key"].rsplit(":", 1)[-1]},
        trust_root={"status": "verified", "receipt_id": "00000000-0000-4000-8000-000000000001"},
    )


def _write_live(root: Path, *, requirement_id: str, revision: str, criterion_id: str, target=None) -> dict:
    target = target or _target()
    payload = {
        "schema": "cortex/live-canary-receipt/v1",
        "result": "passed",
        "requirement_id": requirement_id,
        "requirement_revision": revision,
        "acceptance_id": criterion_id,
        "observed_at": "2026-09-25T00:00:00+00:00",
        "target": target,
        "authority": {"id": "release-operator", "version": "1", "receipt": "approval:123"},
        "independence": {"canary_domain": "loaded-runtime", "review_domain": "reviewer-domain"},
    }
    path = root / f"live-{requirement_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {"locator": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _manifest(root: Path, specs: list[tuple[str, str]], *, required=None, waiver_policy=None) -> dict:
    required = required or ["source", "test", "review", "merge", "installed", "live"]
    requirements = []
    for requirement_id, revision in specs:
        source = root / "specs" / f"{requirement_id}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f"{requirement_id} {revision}\n", encoding="utf-8")
        requirements.append(
            {
                "id": requirement_id,
                "revision": revision,
                "title": f"需求 {requirement_id}",
                "source_ref": {
                    "locator": str(source.relative_to(root)),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                },
                "acceptance_criteria": [
                    {"id": f"{requirement_id}-AC1", "description": "逐項驗收"}
                ],
                "evidence_policy": {
                    "required_stages": list(required),
                    "waivable_stages": [],
                    "max_age_seconds": {stage: 604800 for stage in ("installed", "live") if stage in required},
                    "policy_version": "delivery-validator/v1",
                    "owner": {"id": "delivery-owner", "work_ids": ["#845"], "recovery": "cortex work show requirement-delivery-accounting"},
                },
            }
        )
    result = {
        "schema": "cortex/requirement-manifest/v1",
        "manifest_id": "test-requirements",
        "authority_ref": {"kind": "accepted-plan", "revision": "plan-v1"},
        "requirements": requirements,
        "waiver_policy": waiver_policy or {"authorities": []},
    }
    return result


def _snapshot(root: Path, manifest: dict, specs: list[tuple[str, str]], *, profile_key=PROFILE_KEY, config_revision=CONFIG_REVISION, with_live=True) -> dict:
    mappings = []
    for index, (requirement_id, revision) in enumerate(specs):
        work_id = WORK if index == 0 else f"{WORK}-{index + 1}"
        run_id = f"run-{index + 1}"
        authority = _authority(work_id=work_id)
        target = _target(profile_key=profile_key, config_revision=config_revision)
        completion_ref = _write_completion(root, authority=authority, run_id=run_id, slice_id=f"delivery-{index + 1}")
        _write_runtime(root, target=target, pid=555 + index, state_name=f"runtime-{index}")
        live_ref = _write_live(
            root,
            requirement_id=requirement_id,
            revision=revision,
            criterion_id=f"{requirement_id}-AC1",
            target=target,
        ) if with_live else None
        mappings.append(
            {
                "requirement_id": requirement_id,
                "requirement_revision": revision,
                "acceptance_ids": [f"{requirement_id}-AC1"],
                "repo": REPO,
                "work_id": work_id,
                "run_id": run_id,
                "workflow_step_ids": ["build-1", "verify-1", "review-1", "ship-1"],
                "source_generation": index + 1,
                "candidate_sha": HEAD,
                "pr_number": 7,
                "change": "delivery-work",
                "todo_paths": ["docs/todo.md"],
                "profile_key": profile_key,
                "config_revision": config_revision,
                "policy_version": "delivery-validator/v1",
                "target": target,
                "completion_record": completion_ref,
                "installed_runtime": {
                    "state_root": f"runtime-{index}",
                    "service": "manager",
                    "instance": "default",
                    "expected_pid": 555 + index,
                    "declared_config_revision": config_revision,
                    "target": target,
                },
                "live_receipt": live_ref,
            }
        )
    return {
        "schema": "cortex/requirement-evidence-snapshot/v1",
        "captured_at": "2026-09-25T00:01:00+00:00",
        "snapshot_revision": 1,
        "mappings": mappings,
        "waivers": [],
    }


def _context(root: Path, *, authorities=None, github=None, current_artifact=None, live_validator=None, waiver_validator=None):
    return {
        "source_root": root,
        "evidence_root": root,
        "coordinator_root": root,
        "authority_loader": lambda repo, work_id: (authorities or {}).get((repo, work_id), _authority(work_id=work_id)),
        "github_client": github or _GitHub(),
        "checkout_resolver": lambda repo: root,
        "current_artifact": current_artifact or {
            "kind": "installed-wheel",
            "package": "paulsha-cortex",
            "package_version": "1.0.0",
            "source_revision": MERGE,
            "sha256": ARTIFACT_SHA,
        },
        "now_epoch": NOW,
        "runtime_state_resolver": lambda service, instance, hint: root / str(hint),
        "live_receipt_validator": live_validator or (lambda receipt: True),
        "waiver_validator": waiver_validator or (lambda waiver: True),
    }


def _ready_case(tmp_path: Path, specs=None, *, with_live=True):
    specs = specs or [("R01", "r1")]
    manifest = _manifest(tmp_path, specs)
    snapshot = _snapshot(tmp_path, manifest, specs, with_live=with_live)
    return manifest, snapshot, _context(tmp_path)


def test_a01_versioned_multi_work_mapping_covers_only_exact_criteria(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path, [("R01", "r1"), ("R02", "r2")])
    assert snapshot["mappings"][1]["work_id"] != snapshot["mappings"][0]["work_id"]
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "ready"
    assert [row["requirement_id"] for row in report["requirements"]] == ["R01", "R02"]
    snapshot["mappings"].pop(0)
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["requirements"][0]["status"] == "missing"
    assert report["requirements"][1]["status"] == "covered"


def test_a01_missing_acceptance_or_evidence_policy_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, [("R01", "r1")])
    del manifest["requirements"][0]["acceptance_criteria"]
    with pytest.raises(ValueError, match="acceptance"):
        validate_manifest(manifest)
    manifest = _manifest(tmp_path, [("R01", "r1")])
    del manifest["requirements"][0]["evidence_policy"]
    with pytest.raises(ValueError, match="evidence policy"):
        validate_manifest(manifest)


def test_production_manifest_pins_all_refine_requirements_and_sources() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = repo_root / "docs/superpowers/specs/refine-requirements-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    normalized = validate_manifest(manifest)
    assert [row["id"] for row in normalized["requirements"]] == [f"R{number:02d}" for number in range(1, 15)]
    assert all(row["evidence_policy"]["required_stages"] == ["source", "test", "review", "merge", "installed", "live"] for row in normalized["requirements"])
    plan = repo_root / "docs/superpowers/plans/2026-09-07-cortex-refine-complete.md"
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    assert all(row["source_ref"]["sha256"] == plan_sha for row in normalized["requirements"])


def test_a02_each_delivery_stage_is_accounted_separately(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path, with_live=False)
    result = inspect_delivery(manifest, snapshot, **context)
    stages = {row["stage"]: row["status"] for row in result["gaps"]}
    assert stages["live"] == "missing"
    assert "merge" not in stages
    no_merge = _GitHub(merge="0" * 40)
    result = inspect_delivery(manifest, snapshot, **{**context, "github_client": no_merge})
    assert next(g for g in result["gaps"] if g["stage"] == "merge")["status"] == "failed"
    shutil.rmtree(tmp_path / "runtime-0")
    result = inspect_delivery(manifest, snapshot, **context)
    assert next(g for g in result["gaps"] if g["stage"] == "installed")["status"] == "unknown"


@pytest.mark.parametrize(
    "defect",
    ["bad-hash", "wrong-repo", "wrong-work", "wrong-run", "wrong-candidate", "wrong-pr", "old-revision", "wrong-merge"],
)
def test_a03_invalid_or_cross_bound_evidence_fails_closed(tmp_path: Path, defect: str) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    row = snapshot["mappings"][0]
    if defect == "bad-hash":
        row["completion_record"]["sha256"] = "0" * 64
    elif defect == "wrong-repo":
        row["repo"] = "other/project"
    elif defect == "wrong-work":
        authority = _authority(work_id="other-work")
        context["authority_loader"] = lambda repo, work_id: authority
    elif defect == "wrong-run":
        row["run_id"] = "other-run"
    elif defect == "wrong-candidate":
        row["candidate_sha"] = "9" * 40
    elif defect == "wrong-pr":
        row["pr_number"] = 8
    elif defect == "old-revision":
        row["requirement_revision"] = "r0"
    elif defect == "wrong-merge":
        context["github_client"] = _GitHub(merge="0" * 40)
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "not-ready"


def test_a03_work_must_be_authorized_for_the_requirement_owner(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    manifest["requirements"][0]["evidence_policy"]["owner"]["work_ids"] = ["hamanpaul/paulsha-cortex#999"]
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "not-ready"
    assert next(g for g in report["gaps"] if g["stage"] == "test")["reason"] == "work-authority-not-listed-for-requirement-owner"


def test_a04_loaded_runtime_binds_artifact_profile_config_and_canary_target(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    row = snapshot["mappings"][0]
    row["target"]["artifact_sha256"] = "9" * 64
    report = inspect_delivery(manifest, snapshot, **context)
    assert next(g for g in report["gaps"] if g["stage"] == "installed")["status"] == "failed"
    row["target"] = _target()
    row["live_receipt"] = _write_live(
        tmp_path,
        requirement_id="R01",
        revision="r1",
        criterion_id="R01-AC1",
        target={**_target(), "instance": "other"},
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert next(g for g in report["gaps"] if g["stage"] == "live")["status"] == "failed"


def test_a04_without_canonical_runtime_root_resolver_is_unknown(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    context.pop("runtime_state_resolver")
    report = inspect_delivery(manifest, snapshot, **context)
    installed = next(g for g in report["gaps"] if g["stage"] == "installed")
    assert installed["status"] == "unknown"
    assert installed["reason"] == "loaded-runtime-state-root-resolver-unavailable"


def test_a05_stale_authority_and_non_independent_review_do_not_pass(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    stale = _authority(last_success=NOW - 100_000)
    context["authority_loader"] = lambda repo, work_id: stale
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "not-ready"
    assert any(g["stage"] == "merge" and g["status"] == "unknown" for g in report["gaps"])
    row = snapshot["mappings"][0]
    row["completion_record"] = _write_completion(
        tmp_path,
        authority=_authority(),
        run_id=row["run_id"],
        slice_id="same-domain-review",
        same_review_domain=True,
    )
    context["authority_loader"] = lambda repo, work_id: _authority(work_id=work_id)
    report = inspect_delivery(manifest, snapshot, **context)
    assert any(g["stage"] == "review" and g["status"] == "failed" for g in report["gaps"])


def test_a06_profile_or_config_drift_stales_only_the_affected_mapping(tmp_path: Path) -> None:
    specs = [("R01", "r1"), ("R02", "r1")]
    manifest, snapshot, context = _ready_case(tmp_path, specs)
    index_path = tmp_path / "index.json"
    first = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert first["report"]["closure_readiness"] == "ready"
    changed = copy.deepcopy(snapshot)
    changed["snapshot_revision"] = 2
    changed["mappings"][0]["source_generation"] = 3
    changed["mappings"][0]["profile_key"] = "epk:v1:observed:" + "8" * 64
    changed["mappings"][0]["config_revision"] = "9" * 64
    changed["mappings"][0]["target"]["profile_key"] = changed["mappings"][0]["profile_key"]
    changed["mappings"][0]["target"]["config_revision"] = changed["mappings"][0]["config_revision"]
    changed["mappings"][0]["installed_runtime"]["declared_config_revision"] = changed["mappings"][0]["config_revision"]
    changed["mappings"][0]["installed_runtime"]["target"] = changed["mappings"][0]["target"]
    result = inspect_delivery(manifest, changed, **context)
    assert result["requirements"][0]["status"] == "blocked"
    assert result["requirements"][1]["status"] == "covered"


def test_a06_candidate_change_stales_only_its_mapping(tmp_path: Path) -> None:
    specs = [("R01", "r1"), ("R02", "r1")]
    manifest, snapshot, context = _ready_case(tmp_path, specs)
    index_path = tmp_path / "index.json"
    reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    changed = copy.deepcopy(snapshot)
    changed["snapshot_revision"] = 2
    changed["mappings"][0]["source_generation"] = 3
    changed["mappings"][0]["candidate_sha"] = "9" * 40
    result = reconcile_delivery(manifest, changed, index_path=index_path, **context)
    statuses = {(row["requirement_id"], row["candidate_sha"]): row["status"] for row in result["index"]["mappings"]}
    assert statuses[("R01", HEAD)] == "stale"
    assert statuses[("R01", "9" * 40)] == "blocked"
    assert statuses[("R02", HEAD)] == "covered"


def test_a06_evidence_policy_revision_stales_only_its_requirement(tmp_path: Path) -> None:
    specs = [("R01", "r1"), ("R02", "r1")]
    manifest, snapshot, context = _ready_case(tmp_path, specs)
    manifest["requirements"][0]["evidence_policy"]["policy_version"] = "delivery-validator/v2"
    result = inspect_delivery(manifest, snapshot, **context)
    assert result["requirements"][0]["status"] == "blocked"
    assert result["requirements"][1]["status"] == "covered"
    assert any(g["requirement_id"] == "R01" and g["status"] == "stale" for g in result["gaps"])


def test_a06_reconcile_marks_only_changed_requirement_history_stale(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path, [("R01", "r1"), ("R02", "r1")])
    index_path = tmp_path / "index.json"
    reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    changed_manifest = copy.deepcopy(manifest)
    changed_manifest["requirements"][0]["revision"] = "r2"
    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["snapshot_revision"] += 1
    result = reconcile_delivery(changed_manifest, changed_snapshot, index_path=index_path, **context)
    statuses = {row["requirement_id"]: row["status"] for row in result["index"]["mappings"]}
    assert statuses["R01"] == "stale"
    assert statuses["R02"] == "covered"


@pytest.mark.parametrize("checkpoint", ["before-index-write", "after-receipt-write", "before-return"])
def test_a07_crash_replay_is_idempotent_and_keeps_completion_immutable(tmp_path: Path, monkeypatch, checkpoint: str) -> None:
    import paulsha_cortex.coordinator.requirement_delivery as rd

    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    completion_path = tmp_path / snapshot["mappings"][0]["completion_record"]["locator"]
    before = hashlib.sha256(completion_path.read_bytes()).hexdigest()

    def crash(point):
        if point == checkpoint:
            raise RuntimeError("simulated process crash")

    monkeypatch.setattr(rd, "_checkpoint", crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    monkeypatch.setattr(rd, "_checkpoint", lambda point: None)
    second = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    revision = read_index(index_path)["revision"]
    third = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert second["changed"] is (checkpoint == "before-index-write")
    assert third["changed"] is False
    assert read_index(index_path)["revision"] == revision
    assert hashlib.sha256(completion_path.read_bytes()).hexdigest() == before


def test_a07_late_terminal_generation_cannot_replace_newer_mapping(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    newer = copy.deepcopy(snapshot)
    newer["snapshot_revision"] = 2
    newer["mappings"][0]["source_generation"] = 2
    reconcile_delivery(manifest, newer, index_path=index_path, **context)
    replay = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert replay["report"]["closure_readiness"] == "pending"
    assert replay["pending_reason"] == "late-source-generation-ignored"
    stored = read_index(index_path)
    assert stored["mappings"][0]["source_generation"] == 2
    assert stored["mappings"][0]["status"] == "covered"


def test_a08_parallel_reconcile_uses_exact_index_cas(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    initial = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    expected = read_index(index_path)["revision"]
    barrier = Barrier(2)
    variants = [copy.deepcopy(snapshot), copy.deepcopy(snapshot)]
    for index, variant in enumerate(variants):
        variant["snapshot_revision"] = 2 + index
        variant["mappings"][0]["source_generation"] = 2 + index

    def run(variant):
        barrier.wait()
        try:
            return reconcile_delivery(
                manifest,
                variant,
                index_path=index_path,
                expected_index_revision=expected,
                **context,
            )
        except IndexConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, variants))
    assert sum(isinstance(value, IndexConflict) for value in outcomes) == 1
    assert read_index(index_path)["generation"] == initial["index"]["generation"] + 1


def test_a08_authority_drift_during_reconcile_stays_pending_without_write(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    calls = 0

    def authority_loader(repo, work_id):
        nonlocal calls
        calls += 1
        return _authority(work_id=work_id, source_revision=("a" if calls == 1 else "c") * 40)

    context["authority_loader"] = authority_loader
    result = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert result["report"]["closure_readiness"] == "pending"
    assert result["pending_reason"] == "work-authority-changed-during-reconcile"
    assert result["changed"] is False
    assert not index_path.exists()


def test_a09_machine_readable_gaps_name_owner_stage_reason_and_recovery(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path, with_live=False)
    before = (tmp_path / "index.json")
    report = inspect_delivery(manifest, snapshot, **context)
    gap = next(row for row in report["gaps"] if row["stage"] == "live")
    assert gap["requirement_id"] == "R01"
    assert gap["acceptance_id"] == "R01-AC1"
    assert gap["owner"]["id"] == "delivery-owner"
    assert gap["reason"]
    assert gap["recovery"]
    assert not before.exists()
    assert report["schema"] == "cortex/requirement-delivery-report/v1"


def test_a09_one_legal_receipt_removes_only_its_own_gap(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path, [("R01", "r1"), ("R02", "r2")], with_live=False)
    shutil.rmtree(tmp_path / "runtime-1")
    before = inspect_delivery(manifest, snapshot, **context)
    assert any(g["requirement_id"] == "R01" and g["stage"] == "live" for g in before["gaps"])
    assert any(g["requirement_id"] == "R02" and g["stage"] == "installed" for g in before["gaps"])
    snapshot["mappings"][0]["live_receipt"] = _write_live(
        tmp_path, requirement_id="R01", revision="r1", criterion_id="R01-AC1"
    )
    after = inspect_delivery(manifest, snapshot, **context)
    assert not any(g["requirement_id"] == "R01" and g["stage"] == "live" for g in after["gaps"])
    assert any(g["requirement_id"] == "R02" and g["stage"] == "installed" for g in after["gaps"])
    assert any(g["requirement_id"] == "R02" and g["stage"] == "live" for g in after["gaps"])


def test_a10_index_migration_preserves_unknown_fields_and_never_edits_sources(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    completion_path = tmp_path / snapshot["mappings"][0]["completion_record"]["locator"]
    completion_bytes = completion_path.read_bytes()
    reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    doc["future_extension"] = {"provenance": ["preserve-me"]}
    index_path.write_text(json.dumps(doc), encoding="utf-8")
    result = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert result["index"]["extensions"]["future_extension"] == {"provenance": ["preserve-me"]}
    assert completion_path.read_bytes() == completion_bytes
    future = dict(doc, schema_version=INDEX_SCHEMA + 1)
    index_path.write_text(json.dumps(future), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported index schema"):
        read_index(index_path)


def test_a11_waiver_requires_exact_scope_approved_authority_and_expiry(tmp_path: Path) -> None:
    waiver_policy = {"authorities": [{"id": "qa-council", "version": "2"}]}
    manifest = _manifest(tmp_path, [("R01", "r1")], required=["source", "test"], waiver_policy=waiver_policy)
    manifest["requirements"][0]["evidence_policy"]["waivable_stages"] = ["test"]
    snapshot = _snapshot(tmp_path, manifest, [("R01", "r1")])
    snapshot["mappings"][0]["completion_record"] = None
    snapshot["waivers"] = [{
        "requirement_id": "R01", "requirement_revision": "r1", "acceptance_id": "R01-AC1",
        "stage": "test", "reason": "accepted exception", "authority": {"id": "qa-council", "version": "2"},
        "expires_at": "2026-10-01T00:00:00+00:00", "receipt": "approval:123",
    }]
    context = _context(tmp_path)
    result = inspect_delivery(manifest, snapshot, **context)
    assert result["closure_readiness"] == "ready"
    assert result["mappings"][0]["evidence"]["test"]["status"] == "not-applicable"
    snapshot["waivers"][0]["stage"] = "live"
    with pytest.raises(ValueError, match="cannot waive installed/live"):
        inspect_delivery(manifest, snapshot, **context)
    snapshot["waivers"][0]["stage"] = "test"
    snapshot["waivers"][0]["acceptance_id"] = "another-requirement-AC1"
    result = inspect_delivery(manifest, snapshot, **context)
    assert result["closure_readiness"] == "not-ready"
    assert result["mappings"][0]["evidence"]["test"]["status"] == "missing"
    snapshot["waivers"][0]["acceptance_id"] = "R01-AC1"
    snapshot["waivers"][0]["expires_at"] = "2026-09-01T00:00:00+00:00"
    result = inspect_delivery(manifest, snapshot, **context)
    assert result["closure_readiness"] == "not-ready"


def test_a12_true_completion_runtime_fixture_rebuilds_only_missing_index_entry(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    first = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert first["report"]["closure_readiness"] == "ready"
    doc = json.loads(index_path.read_text(encoding="utf-8"))
    doc["mappings"] = []
    index_path.write_text(json.dumps(doc), encoding="utf-8")
    resumed = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert resumed["report"]["closure_readiness"] == "ready"
    assert len(resumed["index"]["mappings"]) == 1
    assert resumed["index"]["mappings"][0]["evidence"]["test"]["validator"] == "completion/v1"
    assert resumed["index"]["mappings"][0]["evidence"]["installed"]["validator"] == "loaded-runtime-attestation/v1"
