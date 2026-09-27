from __future__ import annotations

import copy
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from paulsha_cortex.coordinator import completion, claim, live_receipt_validators, review, verification
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
from qualification.validate import (
    CANARY_BUILDER_EFFORT,
    CANARY_BUILDER_EXECUTOR,
    CANARY_BUILDER_MODEL,
    REQUIRED_PROVIDERS,
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
    """可控 remote closure 事實的假 GitHub client。

    ``closing_issues`` 與 ``todo_complete`` 必須能各自獨立變動，才能表達
    「issue 目前 closed 但不是被這個 PR 關閉」與「mapped todo 仍有未勾選項」
    兩種與 issue_state／merge 無關的負例（見 requirement_delivery 對抗審查
    第三輪 BLOCKER：舊版 stub 把兩者都固定成通過值，測試永遠驗不到這兩條）。
    """

    def __init__(
        self,
        *,
        merge: str = MERGE,
        head: str = HEAD,
        issue_state: str = "closed",
        closing_issues: tuple[int, ...] = (845,),
        todo_complete: bool = True,
        default_head: str | None = None,
        merge_is_ancestor: bool = True,
    ) -> None:
        self.merge = merge
        self.head = head
        self.issue_state = issue_state
        self.closing_issues = closing_issues
        self.todo_complete = todo_complete
        # 對抗審查第四輪 BLOCKER：其他 PR 之後推進 default branch 時，
        # `default_head` 會跟這條 PR 的 merge_commit 不同（但 merge_commit
        # 仍是它的祖先）；預設沿用舊行為（等於 merge），只在測試明確要模擬
        # 「default branch 之後又推進」時才覆寫。
        self.default_head = default_head if default_head is not None else merge
        self.merge_is_ancestor = merge_is_ancestor
        self.calls: list[dict] = []

    def fetch_remote_closure(self, **kwargs):
        self.calls.append(kwargs)
        return RemoteClosureFacts(
            merge_commit=self.merge,
            pr_head=self.head,
            merge_parents=(MERGE, self.head),
            default_head=self.default_head,
            merge_is_ancestor=self.merge_is_ancestor,
            merge_is_merge_commit=True,
            issue_states={845: self.issue_state},
            active_openspec_absent=True,
            archive_present=True,
            todo_complete=self.todo_complete,
            todo_revisions={"docs/todo.md": "6" * 40},
            completion_record_valid=True,
            closing_issues=self.closing_issues,
        )


class _FlakyGitHub:
    """對抗審查第四輪 MAJOR（同 generation 重讀弱化）用：模擬短暫網路抖動，
    讓 merge 階段暫時失去已驗證欄位（回退成 unknown），而不是真的失效。"""

    def fetch_remote_closure(self, **kwargs):
        raise RuntimeError("transient network hiccup")


def _authority(
    *,
    work_id: str = WORK,
    last_success: float = NOW - 1,
    snapshot_hash: str = "b" * 64,
    source_revision: str = "a" * 40,
    mapped_todo_paths: tuple[str, ...] = ("docs/todo.md",),
):
    return claim.WorkAuthority._verified(
        repo=REPO,
        work_id=work_id,
        mapped_issues=(845,),
        mapped_prs=(7,),
        mapped_openspec=("delivery-work",),
        mapped_todo_paths=mapped_todo_paths,
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
        "todo_paths": sorted(authority.mapped_todo_paths),
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


def _write_completion(
    root: Path,
    *,
    authority=None,
    run_id: str = "run-1",
    candidate: str = HEAD,
    slice_id: str = "delivery-slice",
    same_review_domain: bool = False,
    verification_status: str = "reviewing",
    test_results: list[dict] | None = None,
    acceptance_ids: list[str] | None = None,
) -> dict:
    authority = authority or _authority()
    if test_results is None:
        test_results = [{
            "name": "targeted-test",
            "status": "passed",
            "acceptance_ids": acceptance_ids or ["R01-AC1"],
        }]
    verify_ref = verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "candidate": candidate,
            "status": verification_status,
            "summary": "verification-succeeded",
            "details": {"ok": True, "tests": test_results},
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


# #845 對抗審查第九輪 MAJOR：這個 suite 的所有 governed deployment-canary
# fixture 都沿用同一個 `_target()`，因此規範 evidence 目錄 digest 是常數；
# 集中定義一次，取代先前寫死的 `"canary-evidence"` 字面路徑。
_CANARY_EVIDENCE_SCOPE = live_receipt_validators.deployment_canary_evidence_scope(_target())


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
    authority_locator = "authority/accepted-plan.md"
    authority_path = root / authority_locator
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    plan_rows = [
        "| ID／問題 | 本版明確納入的修正 | 既有落點／需要補的範圍 | 必須取得的驗收證據 |",
        "| --- | --- | --- | --- |",
    ]
    for requirement_id, _revision in specs:
        plan_rows.append(
            f"| {requirement_id} 需求 {requirement_id} | 範圍 {requirement_id} | "
            f"交付脈絡 {requirement_id} | 逐項驗收 {requirement_id} |"
        )
    authority_path.write_text(
        "---\nstatus: accepted\n---\n\n# Accepted requirement plan\n\n"
        + "\n".join(plan_rows)
        + "\n",
        encoding="utf-8",
    )
    authority_sha256 = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    requirements = []
    for requirement_id, revision in specs:
        requirements.append(
            {
                "id": requirement_id,
                "revision": revision,
                "title": f"需求 {requirement_id}",
                "source_ref": {
                    "locator": authority_locator,
                    "sha256": authority_sha256,
                },
                "acceptance_criteria": [
                    {
                        "id": f"{requirement_id}-AC1",
                        "description": f"範圍 {requirement_id}; 交付脈絡 {requirement_id}; 逐項驗收 {requirement_id}",
                    }
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
        "authority_ref": {
            "kind": "accepted-plan",
            "locator": authority_locator,
            "revision": authority_sha256,
        },
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
        completion_ref = _write_completion(
            root,
            authority=authority,
            run_id=run_id,
            slice_id=f"delivery-{index + 1}",
            acceptance_ids=[f"{requirement_id}-AC1"],
        )
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


def _context(root: Path, *, authorities=None, github=None, current_artifact=None, live_validator=None, domain_deriver=None, waiver_validator=None):
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
        # 未特別指定 kind-aware 導出（大多數測試不觸及 #845 對抗審查第九輪
        # BLOCKER 的封閉登記表路徑）時，這個 test double 直接沿用 receipt 自報
        # 的 `independence.canary_domain`，等同修法前的行為；`_governed_case`
        # 會改用真正的 `live_receipt_validators.derive_canary_domain`。
        "live_receipt_domain_deriver": domain_deriver or (lambda receipt: (receipt.get("independence") or {}).get("canary_domain")),
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


def test_a01_snapshot_with_mixed_generation_rows_uses_highest_generation_for_coverage(tmp_path: Path) -> None:
    """對抗審查第五輪 BLOCKER：inspect_delivery 對同一 requirement／criterion
    只要看到任一 covered row 就算通過；source snapshot 若同時含同一 repo／
    work／run 邏輯範圍內較舊 covered 與較新 blocked 的兩個 source_generation
    row（例如 producer 尚未清理掉舊紀錄），判定前必須先依 logical 範圍只留
    最高 generation 的 row 決定 covered 與否，不能讓已被取代的較舊 covered
    row 蓋過較新的 blocked 事實。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    covered_row = snapshot["mappings"][0]
    assert covered_row["source_generation"] == 1
    blocked_row = copy.deepcopy(covered_row)
    blocked_row["source_generation"] = 2
    blocked_row["completion_record"] = _write_completion(
        tmp_path,
        run_id=covered_row["run_id"],
        slice_id="newer-generation-review-conflict",
        same_review_domain=True,
    )
    snapshot["mappings"].append(blocked_row)

    report = inspect_delivery(manifest, snapshot, **context)

    assert report["closure_readiness"] == "not-ready"
    requirement = next(r for r in report["requirements"] if r["requirement_id"] == "R01")
    assert requirement["status"] == "blocked"
    criterion = requirement["acceptance_criteria"][0]
    assert criterion["status"] == "blocked"
    assert any(
        g["requirement_id"] == "R01" and g["stage"] == "review" and g["status"] == "failed"
        for g in report["gaps"]
    )


def test_a01_same_generation_covered_and_blocked_rows_are_not_covered(tmp_path: Path) -> None:
    """對抗審查（第五輪 review）MAJOR：同一 repo／work／run、同一 source_generation
    在單次 snapshot 內同時殘留 covered 與 blocked row（例如同代弱化重讀與舊結果
    並存）時，該邏輯範圍的事實互相矛盾，不得以 any(covered) 判為 covered。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    covered_row = snapshot["mappings"][0]
    conflicting_row = copy.deepcopy(covered_row)
    conflicting_row["completion_record"] = _write_completion(
        tmp_path,
        run_id=covered_row["run_id"],
        slice_id="same-generation-review-conflict",
        same_review_domain=True,
    )
    snapshot["mappings"].append(conflicting_row)

    report = inspect_delivery(manifest, snapshot, **context)

    assert report["closure_readiness"] == "not-ready"
    requirement = next(r for r in report["requirements"] if r["requirement_id"] == "R01")
    criterion = requirement["acceptance_criteria"][0]
    assert criterion["status"] != "covered"


def test_a01_missing_acceptance_or_evidence_policy_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, [("R01", "r1")])
    del manifest["requirements"][0]["acceptance_criteria"]
    with pytest.raises(ValueError, match="acceptance"):
        validate_manifest(manifest, authority_root=tmp_path)
    manifest = _manifest(tmp_path, [("R01", "r1")])
    del manifest["requirements"][0]["evidence_policy"]
    with pytest.raises(ValueError, match="evidence policy"):
        validate_manifest(manifest, authority_root=tmp_path)


def test_a13_manifest_authority_digest_must_match_accepted_source(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, [("R01", "r1")])
    validate_manifest(manifest, authority_root=tmp_path)

    authority_path = tmp_path / manifest["authority_ref"]["locator"]
    authority_path.write_text(authority_path.read_text(encoding="utf-8") + "\nupdated\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authority.*digest"):
        validate_manifest(manifest, authority_root=tmp_path)


@pytest.mark.parametrize(
    "mutate",
    [
        # 缺漏：保留同一 accepted revision 卻整條需求被拿掉。
        lambda manifest: manifest["requirements"].pop(),
        # 多出：manifest 宣稱了 accepted authority 沒有的需求。
        lambda manifest: manifest["requirements"].append(copy.deepcopy(manifest["requirements"][0]) | {"id": "R99"}),
        # 縮減：需求仍在，但驗收條件被清空，不能用「非空檢查」矇混過關。
        lambda manifest: manifest["requirements"][0]["acceptance_criteria"].pop(),
        # 竄改：需求 id 保留，但標題與 accepted authority 記載的不一致。
        lambda manifest: manifest["requirements"][0].update({"title": "被竄改的標題"}),
    ],
)
def test_a13_manifest_must_match_accepted_requirement_and_criterion_inventory(tmp_path: Path, mutate) -> None:
    manifest = _manifest(tmp_path, [("R01", "r1"), ("R02", "r2")])
    mutate(manifest)

    with pytest.raises(ValueError, match="accepted authority"):
        validate_manifest(manifest, authority_root=tmp_path)


def test_a02_reviewing_verification_without_criterion_test_binding_is_a_test_gap(tmp_path: Path) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    row = snapshot["mappings"][0]
    row["completion_record"] = _write_completion(
        tmp_path,
        run_id=row["run_id"],
        slice_id="reviewing-without-tests",
        verification_status="reviewing",
        test_results=[],
    )

    report = inspect_delivery(manifest, snapshot, **context)

    test_gap = next(gap for gap in report["gaps"] if gap["stage"] == "test")
    assert test_gap["acceptance_id"] == "R01-AC1"
    assert test_gap["status"] == "missing"
    assert report["closure_readiness"] == "not-ready"


@pytest.mark.parametrize(
    "test_result",
    [
        {"name": "unbound", "status": "passed"},
        {"name": "failed", "status": "failed", "acceptance_ids": ["R01-AC1"]},
        {"name": "wrong-criterion", "status": "passed", "acceptance_ids": ["R01-AC2"]},
    ],
)
def test_a02_only_passing_test_explicitly_bound_to_criterion_covers_it(tmp_path: Path, test_result: dict) -> None:
    manifest, snapshot, context = _ready_case(tmp_path)
    row = snapshot["mappings"][0]
    row["completion_record"] = _write_completion(
        tmp_path,
        run_id=row["run_id"],
        slice_id="criterion-test-binding",
        verification_status="reviewing",
        test_results=[test_result],
    )

    report = inspect_delivery(manifest, snapshot, **context)

    test_gap = next(gap for gap in report["gaps"] if gap["stage"] == "test")
    assert test_gap["acceptance_id"] == "R01-AC1"
    assert report["closure_readiness"] == "not-ready"


def test_production_manifest_pins_all_refine_requirements_and_sources() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = repo_root / "docs/superpowers/specs/refine-requirements-v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    normalized = validate_manifest(manifest, authority_root=repo_root)
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
    [
        "bad-hash", "wrong-repo", "wrong-work", "wrong-run", "wrong-candidate",
        "wrong-pr", "old-revision", "wrong-merge",
        "issue-not-referenced-by-pr", "todo-incomplete",
    ],
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
    elif defect == "issue-not-referenced-by-pr":
        # issue 目前 closed，但不是被這個 PR 的 closingIssuesReferences 關閉
        # （例如人工關閉或被別的 PR 關閉）；merge 不能就此記 verified。
        context["github_client"] = _GitHub(closing_issues=())
    elif defect == "todo-incomplete":
        # mapped todo.md 遠端仍有未勾選項；merge 不能記 verified。
        context["github_client"] = _GitHub(todo_complete=False)
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "not-ready"


def test_a03_merge_requires_pr_to_actually_close_the_mapped_issue(tmp_path: Path) -> None:
    """issue closed 是觀測事實，但唯有這個 PR 的 closingIssuesReferences 真正
    綁定該 issue，merge 階段才可記 verified；否則須留下具體 gap。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    context["github_client"] = _GitHub(closing_issues=())

    report = inspect_delivery(manifest, snapshot, **context)

    merge_gap = next(gap for gap in report["gaps"] if gap["stage"] == "merge")
    assert merge_gap["status"] == "failed"
    assert merge_gap["reason"] == "remote-closing-issue-reference-missing"
    assert report["closure_readiness"] == "not-ready"


def test_a03_merge_requires_mapped_todo_to_be_complete(tmp_path: Path) -> None:
    """mapped todo.md 遠端仍有未勾選項時，merge 階段不可記 verified。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    context["github_client"] = _GitHub(todo_complete=False)

    report = inspect_delivery(manifest, snapshot, **context)

    merge_gap = next(gap for gap in report["gaps"] if gap["stage"] == "merge")
    assert merge_gap["status"] == "failed"
    assert merge_gap["reason"] == "remote-todo-incomplete"
    assert report["closure_readiness"] == "not-ready"


def test_a03_merge_requests_todo_completion_bound_to_merge_commit(tmp_path: Path) -> None:
    """對抗審查第六輪 BLOCKER：merge 階段的 todo 完成度不能只看「目前」
    default head——PR 合併當下 mapped todo 尚未全勾、之後別的無關 commit 才
    補勾，重跑 reconcile 不能把同一筆已 blocked 的交付誤判成 covered。
    consumer 必須明確要求 `fetch_remote_closure` 以這個 PR／merge 當下（merge
    commit 的 tree）內容判定 todo_complete，不是「現在」。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    github = _GitHub()
    context["github_client"] = github

    inspect_delivery(manifest, snapshot, **context)

    assert github.calls
    assert github.calls[0]["todo_at_merge_commit"] is True


def test_a03_authority_matches_todo_paths_ignores_row_order(monkeypatch) -> None:
    """對抗審查第六輪 MAJOR：`_authority_matches` 曾以 tuple 逐序比對
    `todo_paths`；同一組已授權的 mapped todo 路徑，只因 row 記錄的順序跟
    `WorkAuthority.mapped_todo_paths` 不同，就會被誤判為 unauthorized。必須
    改用集合比對，不能因順序不同就 fail closed。

    直接測 `_authority_matches` 本身，並把它一定會先呼叫的共用 gate
    `delivery._validate_work_authority`（強制單一 todo path，見
    `delivery.py:458`）暫時中和：那條 gate 目前讓多 todo path 的
    `WorkAuthority` 連進到這行比對前就先被拒絕，多 todo 授權情境要等它放寬
    才會在真實呼叫路徑上出現；這裡先確保「一旦放寬」，這行比對本身是
    順序無關且 fail-closed 的正確實作，不是等共用 gate 放寬後才發現另一個
    bug。CompletionRecord schema（另一個 domain validator，非本票 scope）
    同樣目前只允許單一 todo path 落盤，不能拿來當端到端 fixture。"""
    import paulsha_cortex.coordinator.requirement_delivery as rd

    monkeypatch.setattr(rd.delivery, "_validate_work_authority", lambda authority, *, now_epoch: None)
    paths = ("docs/todo.md", "docs/todo2.md")
    authority = _authority(mapped_todo_paths=paths)
    row = {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "pr_number": 7,
        "change": "delivery-work",
        "todo_paths": list(reversed(paths)),
        "run_id": "run-1",
        "workflow_step_ids": ["build-1", "verify-1", "review-1", "ship-1"],
    }
    record = {"work_authority": _authority_record(authority, run_id="run-1")}

    rd._authority_matches(row, authority, record, now_epoch=NOW)


def test_a03_authority_matches_todo_paths_still_fails_closed_on_missing_path(monkeypatch) -> None:
    """集合比對仍須 fail closed：row 缺一條授權路徑就不能通過（見上一測試對
    `_validate_work_authority` 中和的說明）。"""
    import paulsha_cortex.coordinator.requirement_delivery as rd

    monkeypatch.setattr(rd.delivery, "_validate_work_authority", lambda authority, *, now_epoch: None)
    paths = ("docs/todo.md", "docs/todo2.md")
    authority = _authority(mapped_todo_paths=paths)
    row = {
        "repo": authority.repo,
        "work_id": authority.work_id,
        "pr_number": 7,
        "change": "delivery-work",
        "todo_paths": ["docs/todo.md"],
        "run_id": "run-1",
        "workflow_step_ids": ["build-1", "verify-1", "review-1", "ship-1"],
    }
    record = {"work_authority": _authority_record(authority, run_id="run-1")}

    with pytest.raises(ValueError, match="Todo paths"):
        rd._authority_matches(row, authority, record, now_epoch=NOW)


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


def test_a06_default_head_advancing_after_merge_keeps_completed_delivery_covered(tmp_path: Path) -> None:
    """對抗審查第四輪 BLOCKER：其他 PR 推進 default branch 後重跑
    delivery gaps／reconcile，已完成交付的 CompletionRecord.target_ref_sha
    （合併當下捕捉、不可變）不會再等於「目前」default head——只要這條 PR
    的 merge commit 仍是目前 default head 的祖先（merge ancestry），已
    covered 的需求不能因為之後的正常推進而退化成 remote-default-head-mismatch。
    """
    manifest, snapshot, context = _ready_case(tmp_path)
    advanced_default_head = "9" * 40
    context["github_client"] = _GitHub(default_head=advanced_default_head)

    report = inspect_delivery(manifest, snapshot, **context)

    assert report["closure_readiness"] == "ready"
    merge_evidence = report["mappings"][0]["evidence"]["merge"]
    assert merge_evidence["status"] == "verified"
    assert merge_evidence["target_sha"] == advanced_default_head


def test_a07_late_lower_generation_cannot_flip_blocked_scope_back_to_covered(tmp_path: Path) -> None:
    """對抗審查第四輪 MAJOR：跨 mapping 的 generation 守門原本只看既有
    covered rows；若同一 requirement／criterion／repo／work／run 的
    generation 2 已是 blocked，遲到的 generation 1（不同 CompletionRecord，
    mapping_id 因而不同）不能被當新 coverage 插入、把 index 翻回 ready。
    守門必須以該 logical 範圍內已見過的最高 generation（不論狀態）為準。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    row = snapshot["mappings"][0]

    blocked_gen2 = copy.deepcopy(snapshot)
    blocked_gen2["snapshot_revision"] = 2
    blocked_gen2["mappings"][0]["source_generation"] = 2
    blocked_context = {**context, "github_client": _GitHub(merge="0" * 40)}
    first = reconcile_delivery(manifest, blocked_gen2, index_path=index_path, **blocked_context)
    assert first["report"]["closure_readiness"] == "not-ready"
    assert first["index"]["mappings"][0]["status"] == "blocked"

    late_gen1 = copy.deepcopy(snapshot)
    late_gen1["snapshot_revision"] = 3
    late_row = late_gen1["mappings"][0]
    late_row["source_generation"] = 1
    late_row["completion_record"] = _write_completion(
        tmp_path,
        run_id=row["run_id"],
        slice_id="late-lower-generation-under-blocked-scope",
        acceptance_ids=["R01-AC1"],
    )

    result = reconcile_delivery(manifest, late_gen1, index_path=index_path, **context)

    assert result["changed"] is False
    assert result["pending_reason"] == "late-source-generation-ignored"
    stored = read_index(index_path)
    assert len(stored["mappings"]) == 1
    assert stored["mappings"][0]["status"] == "blocked"
    assert stored["mappings"][0]["source_generation"] == 2


def test_a07_same_generation_reread_with_weaker_evidence_does_not_replace_covered(tmp_path: Path) -> None:
    """對抗審查第四輪 MAJOR：same-source-generation-content-drift 只在
    incoming row 維持相同 mapping_id 時才檢查；同一 generation 重讀若暫時
    失去已驗證欄位（例如 merge evidence 因網路抖動短暫拿不到），merge_sha
    從實際值變成缺欄位、mapping_id 因而改變，不能被靜默當成新 mapping 插入、
    讓已 covered 的 requirement 被 blocked/unknown 取代。應保留既有 covered，
    記 pending／drift。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"

    first = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert first["report"]["closure_readiness"] == "ready"
    covered_mapping_id = first["index"]["mappings"][0]["mapping_id"]

    flaky = copy.deepcopy(snapshot)
    flaky_context = {**context, "github_client": _FlakyGitHub()}

    result = reconcile_delivery(manifest, flaky, index_path=index_path, **flaky_context)

    assert result["changed"] is False
    assert result.get("pending_reason") is not None
    stored = read_index(index_path)
    assert len(stored["mappings"]) == 1
    assert stored["mappings"][0]["mapping_id"] == covered_mapping_id
    assert stored["mappings"][0]["status"] == "covered"


def test_a06_same_generation_reread_accepts_strengthened_live_evidence(tmp_path: Path) -> None:
    """對抗審查第六輪 MAJOR：同一 source_generation（同一 mapping_id）先因缺
    live receipt 記為 missing，之後補上合法 live receipt 重跑時，其餘階段皆
    未弱化——不能被 same-source-generation-content-drift 擋下而拒絕更新，
    否則這條 gap 永遠清不掉。同代重讀的弱化保護只能擋證據變弱，證據變強
    必須接受。"""
    manifest, snapshot, context = _ready_case(tmp_path, with_live=False)
    index_path = tmp_path / "index.json"

    first = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert first["report"]["closure_readiness"] == "not-ready"
    assert first["index"]["mappings"][0]["status"] == "missing"
    blocked_mapping_id = first["index"]["mappings"][0]["mapping_id"]

    strengthened = copy.deepcopy(snapshot)
    strengthened["mappings"][0]["live_receipt"] = _write_live(
        tmp_path, requirement_id="R01", revision="r1", criterion_id="R01-AC1"
    )

    result = reconcile_delivery(manifest, strengthened, index_path=index_path, **context)

    assert result.get("pending_reason") is None
    assert result["report"].get("source_generation_drift") is None
    assert result["changed"] is True
    assert result["report"]["closure_readiness"] == "ready"
    stored = read_index(index_path)
    assert stored["mappings"][0]["mapping_id"] == blocked_mapping_id
    assert stored["mappings"][0]["status"] == "covered"


def test_a07_default_head_further_advancing_between_reconciles_does_not_drift(tmp_path: Path) -> None:
    """對抗審查第五輪 MAJOR：merge 改看 ancestry 後，evidence.merge 的
    ``target_sha``／digest 仍隨「目前」default head 改變；同一 generation
    重跑 reconcile 時，main 在兩次呼叫之間正常再往前推進，不能被誤判成
    same-source-generation-content-drift 而卡住既有 covered mapping。merge
    evidence 的 digest／mapping 身分只能綁不可變事實（merge commit、PR
    head、mapped issues、todo 狀態），「目前」default head 只是驗證輸入。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"

    context["github_client"] = _GitHub(default_head="9" * 40)
    first = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)
    assert first["report"]["closure_readiness"] == "ready"
    covered_mapping_id = first["index"]["mappings"][0]["mapping_id"]

    context["github_client"] = _GitHub(default_head="8" * 40)
    second = reconcile_delivery(manifest, snapshot, index_path=index_path, **context)

    assert second["report"]["closure_readiness"] == "ready"
    assert second.get("pending_reason") is None
    assert second["report"].get("source_generation_drift") is None
    assert second["changed"] is False
    stored = read_index(index_path)
    assert stored["mappings"][0]["mapping_id"] == covered_mapping_id
    assert stored["mappings"][0]["status"] == "covered"


def test_a06_never_indexed_older_generation_cannot_roll_back_newer_covered_mapping(tmp_path: Path) -> None:
    """對抗審查第三輪 MAJOR：先落地新 candidate／generation 2 為 covered，之後
    收到一條「從未入索引過」的舊 candidate／generation 1（同一 requirement、
    criterion、work、run，僅 completion_record 內容不同故 mapping_id 不同）。
    generation 比較必須跨 mapping_id、以 requirement/criterion 範圍為準；
    較舊 generation 不得把較新有效 covered mapping 標成 stale 而取而代之。"""
    manifest, snapshot, context = _ready_case(tmp_path)
    index_path = tmp_path / "index.json"
    row = snapshot["mappings"][0]

    newer = copy.deepcopy(snapshot)
    newer["snapshot_revision"] = 2
    newer["mappings"][0]["source_generation"] = 2
    first = reconcile_delivery(manifest, newer, index_path=index_path, **context)
    assert first["report"]["closure_readiness"] == "ready"
    assert first["index"]["mappings"][0]["status"] == "covered"
    newer_mapping_id = first["index"]["mappings"][0]["mapping_id"]

    late_arrival = copy.deepcopy(snapshot)
    late_arrival["snapshot_revision"] = 3
    late_row = late_arrival["mappings"][0]
    late_row["source_generation"] = 1
    late_row["completion_record"] = _write_completion(
        tmp_path,
        run_id=row["run_id"],
        slice_id="never-indexed-old-generation",
        acceptance_ids=["R01-AC1"],
    )

    result = reconcile_delivery(manifest, late_arrival, index_path=index_path, **context)

    assert result["changed"] is False
    assert result["pending_reason"] == "late-source-generation-ignored"
    stored = read_index(index_path)
    assert len(stored["mappings"]) == 1
    assert stored["mappings"][0]["mapping_id"] == newer_mapping_id
    assert stored["mappings"][0]["status"] == "covered"


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


# --- #845 對抗審查 BLOCKER：`live_receipt_validator` 從未接上 production caller ------
#
# 下列測試改用 `live_receipt_validators.governed_live_receipt_validator`（實際
# 掛進 `porcelain/delivery.py` 的同一顆 callable），不是 `_context()` 預設的
# `lambda receipt: True` stub，證明封閉登記表本身可機械驗證 deployment-canary
# qualification 與 #857 task-memory canary 兩種 receipt kind，並對未知 kind、
# 內容綁定不符、canary 未通過與門檻不足各自 fail closed。


def _qualification_payload(*, candidate_sha: str, wheel_sha256: str, status: str = "passed") -> dict:
    providers = [
        {
            "provider": name,
            "requested_model": model,
            "runtime_model": model,
            "requested_effort": effort,
            "runtime_effort": effort,
            "status": "passed",
            "quota": "available",
            "fallback": False,
        }
        for name, (model, effort) in REQUIRED_PROVIDERS.items()
    ]
    return {
        "schema_version": 2,
        "profile": "deployment-canary",
        "status": status,
        "candidate_sha": candidate_sha,
        "wheel": {"filename": "paulsha_cortex-1.0.0-py3-none-any.whl", "sha256": wheel_sha256},
        "bundle": {"sha256": "b" * 64},
        "image": {"digest": "sha256:" + "c" * 64},
        "services": [
            {"name": "cortex-manager.service", "uid": 991, "gid": 991, "active": True},
            {"name": "cortex-monitor.service", "uid": 991, "gid": 991, "active": True},
            {"name": "cortex-egress-proxy.service", "uid": 950, "gid": 950, "active": True},
        ],
        "providers": providers,
        "tests": [
            {"name": "fresh-install", "status": "passed"},
            {"name": "full-dispatch-closeout", "status": "passed"},
        ],
        "artifacts": [{"path": "evidence/summary.json", "sha256": "d" * 64}],
    }


def _copy_qualification_module(source_root: Path) -> None:
    """把真正的 `qualification/validate.py` 複製進測試用 `--source-root`（模擬
    Manager 實際 checkout 內含的 `qualification/`）；#845 對抗審查 MAJOR 修正後，
    production validator 改以 `importlib.util.spec_from_file_location` 從
    `--source-root` 動態載入，不再依賴一般 import 撞運氣命中 repo 根目錄。"""
    real_module = Path(__file__).resolve().parents[1] / "qualification" / "validate.py"
    target_dir = source_root / "qualification"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real_module, target_dir / "validate.py")
    # canary 刷新後 validate.py 由同目錄 contract.py 導出 provider／builder 契約。
    shutil.copy2(real_module.with_name("contract.py"), target_dir / "contract.py")


def _full_canary_qualification(
    evidence_base: Path,
    *,
    candidate_sha: str,
    wheel_sha256: str,
    repository: str,
    work_id: str = "canary-dispatch-1",
    issue: int = 845,
    dispatch_repository: str | None = None,
    status: str = "passed",
) -> dict:
    """在 `evidence_base` 下建出一整份**真正能通過** `qualification/validate.py`
    `require_canary_profile=True` 的 evidence tree（`evidence_base/evidence/*.json`），
    回傳對應的 qualification.json payload（含逐檔 sha256 的 artifacts 清單）。#845
    對抗審查 BLOCKER：production validator 現在真的會用 `evidence_root` 核對這裡
    每個檔案的實際內容與 digest，不能只靠一個假 artifact 矇混。"""
    providers = [
        {
            "provider": name,
            "requested_model": model,
            "runtime_model": model,
            "requested_effort": effort,
            "runtime_effort": effort,
            "status": "passed",
            "quota": "available",
            "fallback": False,
        }
        for name, (model, effort) in REQUIRED_PROVIDERS.items()
    ]
    payload = {
        "schema_version": 2,
        "profile": "deployment-canary",
        "status": status,
        "candidate_sha": candidate_sha,
        "wheel": {"filename": "paulsha_cortex-1.0.0-py3-none-any.whl", "sha256": wheel_sha256},
        "bundle": {"sha256": "c" * 64},
        "image": {"digest": "sha256:" + "d" * 64},
        "services": [
            {"name": "cortex-egress-proxy.service", "uid": 950, "gid": 950, "active": True},
            {"name": "cortex-manager.service", "uid": 991, "gid": 991, "active": True},
            {"name": "cortex-monitor.service", "uid": 991, "gid": 991, "active": True},
        ],
        "providers": providers,
        "tests": [
            {"name": name, "status": "passed"}
            for name in (
                "fresh-install", "idempotent-apply", "drift-detection", "rollback",
                "reinstall", "selfcheck", "registry-equation",
                "generated-installed-attestation", "service-identity-hardening",
                "capability-attack-matrix", "durable-state-attack-matrix",
                "enforcement-plane-attack-matrix", "process-attack-matrix",
                "gate-attack-matrix", "negative-controls",
                "provider-capability-smoke", "full-dispatch-closeout",
                "manager-github-dry-run-push",
            )
        ],
        "artifacts": [],
    }
    attestation = {"ok": True, "failures": [], "warnings": []}
    installed = {
        "schema_version": 1,
        "result": "pass",
        "candidate": {"wheel_sha256": wheel_sha256, "bundle_sha256": "c" * 64},
        "attestation": attestation,
        "artifact_hashes": {"units/cortex-manager.service": "1" * 64},
        "service_identities": {"cortex-manager.service": {"user": "cortex-manager"}},
    }
    generated = {
        "schema_version": 1,
        "ok": True,
        "attestation": attestation,
        "artifact_hashes": installed["artifact_hashes"],
        "service_identities": installed["service_identities"],
    }
    cases = []
    required_cases = {
        "capability": ("T1.1", "T1.2", "T1.3", "T1.4"),
        "enforcement-plane": tuple(f"T3.{index}" for index in range(1, 11)),
        "process": ("T4.1", "T4.2", "T4.3", "T4.4"),
        "gate": tuple(f"T5.{index}" for index in range(1, 11)),
    }
    for family, case_ids in required_cases.items():
        for case_id in case_ids:
            cases.append({"family": family, "case": f"{case_id}-probe", "principal": "probe", "status": "passed", "returncode": 1})
    for operation in ("modify", "truncate", "delete", "replace", "symlink-swap", "rollback"):
        for principal in ("cortex-builder", "cortex-reviewer-planner"):
            cases.append({
                "family": "durable-state",
                "case": f"jobs-registry:{operation}",
                "principal": principal,
                "status": "passed",
                "returncode": 13,
            })
    attack = {
        "schema_version": 1,
        "status": "passed",
        "families": ["capability", "durable-state", "enforcement-plane", "process", "gate"],
        "cases": cases,
        "negative_controls": [
            {"family": family, "case": f"{family}-control", "principal": "trusted", "status": "passed", "returncode": 0}
            for family in ("capability", "durable-state", "enforcement-plane", "process", "gate")
        ],
        "authorized_mutations": [],
        "deny_only_assets": [],
        "covered_assets": 1,
        "registry_asset_ids": ["jobs-registry"],
    }
    provider_evidence = {
        "schema_version": 1,
        "providers": {
            row["provider"]: {
                "preflight": {
                    "returncode": 0,
                    "status": "ready",
                    "authenticated": True,
                    "quota": row["quota"],
                    "fallback": row["fallback"],
                    "skipped": False,
                },
                "returncode": 0,
                "models": [row["runtime_model"]],
                "efforts": [row["runtime_effort"]],
                "native_metadata": True,
                "response_token": True,
            }
            for row in providers
        },
    }
    dispatch = {
        "schema_version": 1,
        "status": "passed",
        "repository": dispatch_repository or repository,
        "work_id": work_id,
        "issue": issue,
        "release_candidate_sha": candidate_sha,
        "workflow_candidate_sha": "f" * 40,
        "terminal": {"state": "done", "work_id": work_id, "run_id": "workflow-qualification"},
        "required_markers": sorted({
            "agent-loop-command", "candidate", "bundle", "verdict", "ledger", "evidence", "completion",
        }),
        "agent_loop_probe": {
            "schema_version": 1,
            "executor": CANARY_BUILDER_EXECUTOR,
            "model_id": CANARY_BUILDER_MODEL,
            "card_id": "worktree-isolation",
            "builder_job_ids": ["build-job"],
            "successful_command_count": 1,
            "all_outputs_nonempty": True,
            "command_sha256": "5" * 64,
            "output_sha256": "6" * 64,
            "log_sha256": "7" * 64,
            "thread_sha256": "8" * 64,
            "runtime_model": CANARY_BUILDER_MODEL,
            "runtime_effort": CANARY_BUILDER_EFFORT,
            "model_provider": "openai",
            "probe_candidate_sha": "9" * 40,
        },
        "artifacts": [
            {"path": "coordinator/jobs.json", "sha256": "2" * 64},
            {"path": "coordinator/commit-spool/build-logs/build-job/job.jsonl", "sha256": "7" * 64},
        ],
    }
    dispatch["agent_loop_probe"]["artifact_set_sha256"] = hashlib.sha256(
        (json.dumps(dispatch["artifacts"], sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()
    github = {
        "schema_version": 1,
        "status": "passed",
        "repository": dispatch_repository or repository,
        "authenticated": True,
        "dry_run": True,
        "remote_refs_unchanged": True,
        "before_sha256": "3" * 64,
        "after_sha256": "3" * 64,
    }
    evidence_dir = evidence_base / "evidence"
    documents = {
        "install-verification.json": installed,
        "generated-installed-attestation.json": generated,
        "attack-matrix.json": attack,
        "provider-capabilities.json": provider_evidence,
        "dispatch-closeout.json": dispatch,
        "manager-github-auth.json": github,
    }
    for name, value in documents.items():
        path = evidence_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    inventory_rows = [
        {"path": f"evidence/{name}", "sha256": hashlib.sha256((evidence_dir / name).read_bytes()).hexdigest()}
        for name in sorted(documents)
    ]
    inventory_path = evidence_dir / "artifact-inventory.json"
    inventory_path.write_text(
        json.dumps({"schema_version": 1, "status": "passed", "artifacts": inventory_rows}, sort_keys=True),
        encoding="utf-8",
    )
    payload["artifacts"] = inventory_rows + [
        {"path": "evidence/artifact-inventory.json", "sha256": hashlib.sha256(inventory_path.read_bytes()).hexdigest()}
    ]
    return payload


def _rate_row(attempts: int, successes: int) -> dict:
    return {"attempts": attempts, "successes": successes, "success_rate": successes / attempts}


def _task_memory_payload(*, target: dict) -> dict:
    return {
        "schema": live_receipt_validators.KIND_TASK_MEMORY_LIVE_CANARY,
        "passed": True,
        # #845 對抗審查第九輪 BLOCKER：`derive_canary_domain` 需要一個獨立於
        # review 執行環境的執行者身分；這個欄位現在是必要欄位。
        "executor": {"id": "hippo-task-memory-canary-runner-1"},
        "target": target,
        "content_retrieval": _rate_row(40, 38),
        "paths": {
            "context-delivered": _rate_row(6, 6),
            "snapshot-ready": _rate_row(20, 19),
            "note-fetch": _rate_row(25, 24),
        },
        "negative_controls": [
            {"case": "permission-denied", "status": "passed"},
            {"case": "cross-scope-rejection", "status": "passed"},
        ],
        "cross_project": [
            {"repo": "hamanpaul/paulsha-cortex", "status": "passed"},
            {"repo": "hamanpaul/paulsha-hippo", "status": "passed"},
        ],
    }


def _write_governed_live(
    root: Path,
    *,
    requirement_id: str,
    revision: str,
    criterion_id: str,
    kind: str,
    evidence: dict,
    target: dict,
    canary_target: dict | None = None,
) -> dict:
    payload = {
        "schema": "cortex/live-canary-receipt/v1",
        "result": "passed",
        "requirement_id": requirement_id,
        "requirement_revision": revision,
        "acceptance_id": criterion_id,
        "observed_at": "2026-09-25T00:00:00+00:00",
        "target": target,
        "authority": {"id": "release-operator", "version": "1", "receipt": "approval:123"},
        # #845 對抗審查第九輪 BLOCKER GREEN：canary_domain 不再自報，改由
        # `derive_canary_domain` 依 kind／evidence 導出（見 `_governed_case`
        # 用真正的 production 導出函式）；receipt 只留 review_domain。
        "independence": {"review_domain": "reviewer-domain"},
        "kind": kind,
        "evidence": evidence,
    }
    if canary_target is not None:
        # #845 對抗審查 BLOCKER：deployment-canary receipt 額外帶入外部 canary
        # 身分（repository／work_id／issue）與 evidence 目錄 locator，供
        # `make_governed_live_receipt_validator` 以 `require_canary_profile=True`
        # 交 `qualification/validate.py` 逐檔核對；不是 evidence 自身的一部分，
        # 因為 evidence 就是原封不動的 qualification.json，其 ROOT_KEYS 是封閉集合。
        payload["canary_target"] = canary_target
    path = root / f"live-governed-{requirement_id}-{abs(hash((kind, requirement_id, revision, id(evidence))))}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {"locator": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _governed_case(
    tmp_path: Path,
    *,
    kind: str,
    evidence_factory,
    canary_target_factory=None,
    install_qualification_module: bool = True,
) -> tuple[dict, dict, dict]:
    specs = [("R01", "r1")]
    manifest = _manifest(tmp_path, specs)
    snapshot = _snapshot(tmp_path, manifest, specs, with_live=False)
    target = snapshot["mappings"][0]["target"]
    canary_target = canary_target_factory(target) if canary_target_factory is not None else None
    live_ref = _write_governed_live(
        tmp_path,
        requirement_id="R01",
        revision="r1",
        criterion_id="R01-AC1",
        kind=kind,
        evidence=evidence_factory(target),
        target=target,
        canary_target=canary_target,
    )
    snapshot["mappings"][0]["live_receipt"] = live_ref
    if install_qualification_module:
        _copy_qualification_module(tmp_path)
    # #845 對抗審查 MAJOR：production validator 改由工廠帶入 --source-root／
    # evidence_root 建立，不再是單一全域 callable；測試沿用同一顆工廠證明
    # `inspect_delivery` 真正接到這個受治理的 production validator。
    validator = live_receipt_validators.make_governed_live_receipt_validator(
        source_root=tmp_path, evidence_root=tmp_path
    )
    # #845 對抗審查第九輪 BLOCKER：這裡改用真正的 production 導出函式，證明
    # `inspect_delivery` 真的走 kind-aware 導出，不是只信 receipt 自報字串。
    context = _context(tmp_path, live_validator=validator, domain_deriver=live_receipt_validators.derive_canary_domain)
    return manifest, snapshot, context


def _canary_target(target: dict, *, evidence_directory: str | None = None) -> dict:
    return {
        "repository": target["repo"],
        "work_id": "canary-dispatch-1",
        "issue": 845,
        # #845 對抗審查第九輪 MAJOR：預設值改為這五個 target 欄位的規範
        # digest（見 `deployment_canary_evidence_scope`），不再是 receipt 自由
        # 選擇的字串；呼叫端仍可傳 `evidence_directory` 覆寫以測試路徑安全性
        # 之類的負例。
        "evidence_directory": evidence_directory if evidence_directory is not None else live_receipt_validators.deployment_canary_evidence_scope(target),
    }


def test_a04_delivery_governed_qualification_live_receipt_reaches_ready(tmp_path: Path) -> None:
    """#845 對抗審查 BLOCKER GREEN：deployment-canary receipt 現在必須帶真正的
    evidence 目錄與外部 canary 身分，以 `require_canary_profile=True` 通過
    `qualification/validate.py` 才算 ready；不是只憑 payload 結構就放行。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        ),
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "verified"


# --- #845 對抗審查第九輪：canary_domain 自報與 target 欄位部分綁定 ----------------


def test_a05_delivery_live_canary_self_reported_domain_must_match_derived_evidence_identity(tmp_path: Path) -> None:
    """#845 對抗審查第九輪 BLOCKER RED→GREEN：修法前 `independence.canary_domain`
    完全是 receipt 自報字串，只要跟 reviewer domain 不同就能通過 independence
    檢查——同一個 reviewer 就能自己捏造一個「看起來獨立」的字串放行。修法後
    canary_domain 改由 `derive_canary_domain` 依 evidence 的 `image.digest`
    導出；receipt 自報一個任意但確實不同於 reviewer domain 的字串（不等於導出
    值）仍必須被拒絕。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        ),
        canary_target_factory=_canary_target,
    )
    locator = snapshot["mappings"][0]["live_receipt"]["locator"]
    live_path = tmp_path / locator
    doc = json.loads(live_path.read_text(encoding="utf-8"))
    assert doc["independence"].get("canary_domain") is None
    # 攻擊者自報一個「看起來合法」、與 reviewer domain 不同的 canary_domain，
    # 但它跟證據實際內容（qualification evidence 的 image.digest）無關。
    doc["independence"]["canary_domain"] = "attacker-chosen-domain"
    live_path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    snapshot["mappings"][0]["live_receipt"]["sha256"] = hashlib.sha256(live_path.read_bytes()).hexdigest()

    report = inspect_delivery(manifest, snapshot, **context)

    assert report["closure_readiness"] != "ready"
    live = report["mappings"][0]["evidence"]["live"]
    assert live["status"] == "failed"
    assert live["reason"] == "live-canary-independence-mismatch"


def test_a04_delivery_deployment_canary_evidence_cannot_be_replayed_across_target_scope(tmp_path: Path) -> None:
    """#845 對抗審查第九輪 MAJOR RED→GREEN：修法前 validator 只把
    candidate_sha／artifact_sha256 綁到 target；qualification evidence 完全
    沒有欄位能證明 source_revision／service／instance／profile_key／
    config_revision，因此同一份真正、完整通過的 canary evidence（針對
    instance="default" 產生）只要改外層 target.instance 就能核銷不同
    instance 的需求。修法後 evidence 目錄必須精確等於這五個欄位的規範
    digest；換了 instance 後，同一份實體 evidence 不再位於新 target 唯一
    合法的路徑下，必須 fail closed。"""
    specs = [("R01", "r1")]
    manifest = _manifest(tmp_path, specs)
    snapshot = _snapshot(tmp_path, manifest, specs, with_live=False)
    row = snapshot["mappings"][0]
    original_target = row["target"]
    replayed_target = {**original_target, "instance": "other"}
    row["target"] = replayed_target
    row["installed_runtime"] = {**row["installed_runtime"], "target": replayed_target}

    # 真正的 evidence 只針對 original_target（instance="default"）產生，物理
    # 落在它的規範 digest 目錄之下。
    original_scope = live_receipt_validators.deployment_canary_evidence_scope(original_target)
    evidence = _full_canary_qualification(
        tmp_path / original_scope,
        candidate_sha=original_target["candidate_sha"],
        wheel_sha256=original_target["artifact_sha256"],
        repository=original_target["repo"],
    )
    # receipt 對外聲稱的 target 已經是被替換過的 replayed_target（跟 row 一致，
    # 通過 `_verify_live` 最外層的 exact-target 相等檢查），但
    # `canary_target.evidence_directory` 仍指向原本那份 evidence 的實體目錄。
    canary_target = {
        "repository": replayed_target["repo"],
        "work_id": "canary-dispatch-1",
        "issue": 845,
        "evidence_directory": original_scope,
    }
    live_ref = _write_governed_live(
        tmp_path,
        requirement_id="R01",
        revision="r1",
        criterion_id="R01-AC1",
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence=evidence,
        target=replayed_target,
        canary_target=canary_target,
    )
    row["live_receipt"] = live_ref
    _copy_qualification_module(tmp_path)
    validator = live_receipt_validators.make_governed_live_receipt_validator(
        source_root=tmp_path, evidence_root=tmp_path
    )
    context = _context(tmp_path, live_validator=validator, domain_deriver=live_receipt_validators.derive_canary_domain)

    report = inspect_delivery(manifest, snapshot, **context)

    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_deployment_canary_without_canary_target_is_rejected(tmp_path: Path) -> None:
    """#845 對抗審查 BLOCKER：即使 evidence 目錄真實存在且完整通過，receipt 缺
    外部 canary 身分（`canary_target`）仍必須拒絕——不得只憑 evidence 自己聲稱
    的 repository／work_id／issue 放行。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        ),
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_deployment_canary_missing_evidence_file_is_rejected(tmp_path: Path) -> None:
    """#845 對抗審查 BLOCKER：evidence 目錄少了一個宣告過的 canary-only 檔案，
    `evidence_root` 逐檔核對必須抓到，不能因為 payload 結構本身合法就放行。"""

    def evidence_factory(target: dict) -> dict:
        payload = _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        )
        (tmp_path / _CANARY_EVIDENCE_SCOPE / "evidence" / "manager-github-auth.json").unlink()
        return payload

    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=evidence_factory,
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_deployment_canary_unlisted_evidence_file_is_rejected(tmp_path: Path) -> None:
    """#845 對抗審查 BLOCKER：evidence 目錄多出一個未列入 artifact-inventory 的
    檔案，`_validate_evidence_file_set` 必須擋下，不能因為宣告的 artifacts 全部
    存在就放行整棵樹。"""

    def evidence_factory(target: dict) -> dict:
        payload = _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        )
        stray = tmp_path / _CANARY_EVIDENCE_SCOPE / "evidence" / "stray.json"
        stray.write_text("{}", encoding="utf-8")
        return payload

    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=evidence_factory,
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_deployment_canary_evidence_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    """#845 對抗審查 BLOCKER：evidence 檔案內容被竄改後（sha256 不再與宣告值
    相符），必須拒絕——證明 validator 真的比對了檔案內容，不是只信宣告的路徑
    存在。"""

    def evidence_factory(target: dict) -> dict:
        payload = _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        )
        tampered = tmp_path / _CANARY_EVIDENCE_SCOPE / "evidence" / "install-verification.json"
        tampered.write_text(tampered.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return payload

    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=evidence_factory,
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_deployment_canary_dispatch_repository_mismatch_is_rejected(tmp_path: Path) -> None:
    """#845 對抗審查 BLOCKER：evidence 自己在 `dispatch-closeout.json` 宣稱的
    repository 與 receipt 帶入的外部 canary 身分不符時必須拒絕——這正是
    `require_canary_profile=True` 外部身分交叉核對存在的理由，不能讓 candidate
    自報的 dispatch 身分單方面說了算。"""

    def evidence_factory(target: dict) -> dict:
        return _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
            dispatch_repository="hamanpaul/some-other-repo",
        )

    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=evidence_factory,
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_deployment_canary_evidence_directory_traversal_is_rejected(tmp_path: Path) -> None:
    """#845 對抗審查 BLOCKER：`canary_target.evidence_directory` 企圖用 `..`
    逃出 delivery evidence_root 時必須拒絕，不得解析到 evidence_root 之外。"""

    def evidence_factory(target: dict) -> dict:
        return _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        )

    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=evidence_factory,
        canary_target_factory=lambda target: _canary_target(target, evidence_directory="../canary-evidence"),
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_deployment_canary_validator_missing_source_root_module_is_rejected(tmp_path: Path) -> None:
    """#845 對抗審查 MAJOR：`--source-root` 底下沒有 `qualification/validate.py`
    （模擬已安裝 wheel 在 checkout 外執行）時，即使 evidence 完全合法也必須
    fail closed，不能因為缺套件就放行。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        ),
        canary_target_factory=_canary_target,
        install_qualification_module=False,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a04_delivery_governed_task_memory_live_receipt_reaches_ready(tmp_path: Path) -> None:
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_TASK_MEMORY_LIVE_CANARY,
        evidence_factory=lambda target: _task_memory_payload(target=target),
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "verified"


def test_a03_delivery_unknown_live_receipt_kind_is_rejected_fail_closed(tmp_path: Path) -> None:
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind="cortex/some-unregistered-kind/v1",
        evidence_factory=lambda target: _qualification_payload(
            candidate_sha=target["candidate_sha"], wheel_sha256=target["artifact_sha256"]
        ),
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    live = report["mappings"][0]["evidence"]["live"]
    assert live["status"] == "failed"
    # #845 對抗審查第九輪 BLOCKER：kind 未登記時，`derive_canary_domain` 就
    # 已經無法導出 canary_domain，因此在到達封閉登記表 validator 之前就已在
    # independence 檢查 fail closed；不再是先前的
    # `governed-live-receipt-validator-rejected`。
    assert live["reason"] == "live-canary-independence-mismatch"


def test_a03_delivery_qualification_wheel_digest_not_bound_to_target_is_rejected(tmp_path: Path) -> None:
    """即使 evidence 目錄與外部 canary 身分皆合法齊備，wheel sha256 與 target
    不符仍必須拒絕——證明是這個具體綁定失敗，不是被別的缺項短路。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256="f" * 64,
            repository=target["repo"],
        ),
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_qualification_not_passed_is_rejected(tmp_path: Path) -> None:
    """即使 evidence 目錄與外部 canary 身分皆合法齊備，qualification.json 頂層
    `status` 不是 `passed` 仍必須拒絕。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
            status="failed",
        ),
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_task_memory_below_success_threshold_is_rejected(tmp_path: Path) -> None:
    def weak_evidence(target: dict) -> dict:
        payload = _task_memory_payload(target=target)
        payload["paths"]["note-fetch"] = _rate_row(20, 18)  # 0.90 < 0.95 門檻
        return payload

    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_TASK_MEMORY_LIVE_CANARY,
        evidence_factory=weak_evidence,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a04_delivery_task_memory_target_binding_mismatch_is_rejected(tmp_path: Path) -> None:
    def unbound_evidence(target: dict) -> dict:
        payload = _task_memory_payload(target=target)
        payload["target"] = {**target, "candidate_sha": "9" * 40}
        return payload

    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_TASK_MEMORY_LIVE_CANARY,
        evidence_factory=unbound_evidence,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_a03_delivery_governed_live_receipt_hash_mismatch_still_fails_closed(tmp_path: Path) -> None:
    """封閉登記表接上後，receipt 檔案本身被竄改仍必須在 `_verify_live` 就地擋下，
    不會因為換了 production validator 而略過既有 sha256 綁定規則。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _qualification_payload(
            candidate_sha=target["candidate_sha"], wheel_sha256=target["artifact_sha256"]
        ),
    )
    locator = snapshot["mappings"][0]["live_receipt"]["locator"]
    live_path = tmp_path / locator
    doc = json.loads(live_path.read_text(encoding="utf-8"))
    doc["evidence"]["status"] = "failed"
    live_path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] != "ready"
    live = report["mappings"][0]["evidence"]["live"]
    assert live["status"] == "failed"
    assert live["reason"] == "live-canary-hash-mismatch"


def test_a04_governed_loader_uses_source_root_contract_and_restores_modules(
    tmp_path: Path, monkeypatch
) -> None:
    """wave-2 整合：validate.py 改由同目錄 contract.py 取得 canary 契約後，受治理
    loader 必須用 `--source-root` 內的 contract.py，不得採用環境中其他同名模組，
    且載入後還原 `sys.modules`。"""
    import sys
    import types

    impostor = types.ModuleType("contract")
    impostor.PROVIDERS = {}
    monkeypatch.setitem(sys.modules, "contract", impostor)
    monkeypatch.delitem(sys.modules, "qualification.contract", raising=False)
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        ),
        canary_target_factory=_canary_target,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "ready"
    assert sys.modules["contract"] is impostor
    assert "qualification.contract" not in sys.modules


def test_a04_governed_loader_fails_closed_without_contract(tmp_path: Path) -> None:
    """source_root 的 validate.py 需要 contract.py 卻缺檔時 fail closed。"""
    manifest, snapshot, context = _governed_case(
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence_factory=lambda target: _full_canary_qualification(
            tmp_path / _CANARY_EVIDENCE_SCOPE,
            candidate_sha=target["candidate_sha"],
            wheel_sha256=target["artifact_sha256"],
            repository=target["repo"],
        ),
        canary_target_factory=_canary_target,
    )
    (tmp_path / "qualification" / "contract.py").unlink()
    context = _context(
        tmp_path,
        live_validator=live_receipt_validators.make_governed_live_receipt_validator(
            source_root=tmp_path, evidence_root=tmp_path
        ),
        domain_deriver=live_receipt_validators.derive_canary_domain,
    )
    report = inspect_delivery(manifest, snapshot, **context)
    assert report["closure_readiness"] == "not-ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] != "verified"
