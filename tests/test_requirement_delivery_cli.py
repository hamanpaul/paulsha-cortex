from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from paulsha_cortex import runtime_attestation
from paulsha_cortex.coordinator import claim, completion, github_delivery, live_receipt_validators, review, verification
from paulsha_cortex.coordinator.github_delivery import RemoteClosureFacts
from paulsha_cortex.porcelain import delivery
from qualification.validate import REQUIRED_PROVIDERS


def test_delivery_help_exposes_read_and_reconcile_commands() -> None:
    parser = delivery._build_parser()
    help_text = parser.format_help()
    assert "status" in help_text
    assert "gaps" in help_text
    assert "reconcile" in help_text


def test_reconcile_help_exposes_exact_index_cas(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        delivery.main(["reconcile", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--expected-index-revision" in output
    assert "--evidence-root" not in output
    assert "--coordinator-root" not in output
    assert "--work-snapshot" not in output
    assert "--index INDEX" not in output


def test_loaded_runtime_root_ignores_snapshot_hint_and_uses_service_contract(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(delivery, "selected_instance", lambda: "cortex")
    manager_root = tmp_path / "manager"
    monitor_root = tmp_path / "monitor"
    monkeypatch.setattr(delivery.paths, "coordinator_root", lambda: manager_root)
    monkeypatch.setattr(delivery.paths, "monitor_state_root", lambda: monitor_root)
    assert delivery._runtime_state_root("manager", "cortex", "../../attacker") == manager_root
    assert delivery._runtime_state_root("monitor", "cortex", "/attacker") == monitor_root


def test_loaded_runtime_reader_consumes_service_status_projection(monkeypatch) -> None:
    from paulsha_cortex.porcelain import service

    expected = {"status": "match", "loaded": {"pid": 321}, "trust_root": {"status": "verified"}}
    monkeypatch.setattr(delivery, "selected_instance", lambda: "cortex")
    monkeypatch.setattr(service, "_status_payload", lambda instance: {
        "loaded_runtime": {"manager": expected, "monitor": {"status": "unknown"}}
    })
    assert delivery._runtime_status_report("manager", "cortex") is expected


def test_status_is_machine_readable_and_does_not_write(tmp_path: Path, capsys) -> None:
    index_path = tmp_path / "missing" / "index.json"
    assert delivery.main(["status", "--index", str(index_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "cortex/requirement-delivery-status/v1"
    assert payload["index_generation"] == 0
    assert payload["gaps"] == []
    assert not index_path.parent.exists()


def test_status_preserves_forward_extension_and_gap_projection(tmp_path: Path, capsys) -> None:
    index_path = tmp_path / "index.json"
    original = {
        "schema": "cortex/requirement-delivery-index",
        "schema_version": 1,
        "generation": 3,
        "manifest_id": "refine",
        "manifest_sha256": "a" * 64,
        "snapshot_sha256": "b" * 64,
        "mappings": [],
        "gaps": [{"requirement_id": "R01", "stage": "live", "status": "missing"}],
        "reconcile_receipt": {"coverage": "not-ready"},
        "extensions": {"future_field": {"retained": True}},
    }
    index_path.write_text(json.dumps(original), encoding="utf-8")
    before = index_path.read_bytes()
    assert delivery.main(["status", "--index", str(index_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gaps"] == original["gaps"]
    assert payload["extensions"] == original["extensions"]
    assert index_path.read_bytes() == before


# --- #845 A04：`cortex delivery gaps` 端到端證明封閉登記表 validator 已接上 -------
#
# 下列測試整段走 CLI 入口（`delivery.main(["gaps", ...])`），只 monkeypatch 真正
# 觸網／觸模型的邊界（GitHub client、WorkAuthority loader、loaded-runtime service
# 投影、wall clock），其餘（manifest/snapshot 解析、CompletionRecord、live receipt
# 封閉登記表 validator）都是 production code path，證明合法 fixture receipt 真的
# 能讓需求 ready，且未知 kind／內容不符會產生具體 gap。

_CLI_REPO = "hamanpaul/paulsha-cortex"
_CLI_WORK = "requirement-delivery-accounting"
_CLI_HEAD = "1" * 40
_CLI_MERGE = "2" * 40
_CLI_PROFILE_KEY = "epk:v1:observed:" + "3" * 64
_CLI_ARTIFACT_SHA = "5" * 64
_CLI_NOW = 1_790_380_800.0


def _cli_config_revision() -> str:
    return runtime_attestation.configuration_revision({"config": "cli-fixture"})


def _cli_target(config_revision: str) -> dict:
    return {
        "repo": _CLI_REPO,
        "candidate_sha": _CLI_HEAD,
        "artifact_sha256": _CLI_ARTIFACT_SHA,
        "source_revision": _CLI_MERGE,
        "service": "manager",
        "instance": "default",
        "profile_key": _CLI_PROFILE_KEY,
        "config_revision": config_revision,
    }


def _cli_authority() -> "claim.WorkAuthority":
    return claim.WorkAuthority._verified(
        repo=_CLI_REPO,
        work_id=_CLI_WORK,
        mapped_issues=(845,),
        mapped_prs=(7,),
        mapped_openspec=("delivery-work",),
        mapped_todo_paths=("docs/todo.md",),
        confirmed_todo=True,
        auto_label=False,
        source_revisions=("a" * 40,),
        provider_revision="monitor-revision-1",
        last_success_epoch=_CLI_NOW - 1,
        snapshot_hash="b" * 64,
    )


def _cli_write_completion(root: Path, *, authority: "claim.WorkAuthority") -> dict:
    verify_ref = verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": "delivery-cli-1",
            "candidate": _CLI_HEAD,
            "status": "reviewing",
            "summary": "verification-succeeded",
            "details": {"ok": True, "tests": [{"name": "targeted-test", "status": "passed", "acceptance_ids": ["R01-AC1"]}]},
        },
        coordinator_root=root,
    )
    review_payload = review.build_gate_evaluation(
        slice_id="delivery-cli-1",
        state="passed",
        reason="accepted",
        builder_job_id="builder-1",
        reviewer_job_id="reviewer-1",
        candidate=_CLI_HEAD,
        launch_identity={
            "builder": {"executor": "codex", "model_id": "builder", "independence_domain": "builder-domain"},
            "reviewer": {"executor": "claude", "model_id": "reviewer", "independence_domain": "reviewer-domain"},
        },
    )
    review_ref = review.write_gate_evaluation(review_payload, coordinator_root=root)
    payload = {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": "delivery-cli-1",
        "spec_hash": "e" * 64,
        "plan_hash": "f" * 64,
        "verification_hash": "1" * 64,
        "builder_job_id": "builder-1",
        "reviewer_job_id": "reviewer-1",
        "dispatch_base": "a" * 40,
        "candidate": _CLI_HEAD,
        "target_branch": "main",
        "target_remote": "origin",
        "target_ref": "refs/remotes/origin/main",
        "target_ref_sha": _CLI_MERGE,
        "verification_evidence_path": verify_ref["path"],
        "verification_evidence_hash": verify_ref["hash"],
        "review_policy": "required",
        "docs_class": "code",
        "review_evaluation_path": review_ref["path"],
        "review_evaluation_hash": review_ref["hash"],
        "completed_at": "2026-09-25T00:00:00+00:00",
        "work_authority": {
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
            "merge_commit": _CLI_MERGE,
            "run_id": "run-1",
            "workflow_step_ids": ["build-1", "verify-1", "review-1", "ship-1"],
            "trusted_evidence_refs": [
                {"kind": "foreign_review", "ref": "review:review-1", "hash": "c" * 64},
                {"kind": "preflight", "ref": "tree:" + _CLI_HEAD, "hash": "d" * 64},
                {"kind": "merge_authorization", "ref": "run:run-1", "hash": "7" * 64},
                {"kind": "maintainer-review", "ref": "approval:9", "hash": "8" * 64},
            ],
        },
    }
    ref = completion.write_completion_record(payload, coordinator_root=root)
    return {"locator": str(Path(ref["path"]).relative_to(root)), "sha256": ref["hash"]}


def _cli_manifest(root: Path) -> dict:
    authority_locator = "authority/accepted-plan.md"
    authority_path = root / authority_locator
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_text(
        "---\nstatus: accepted\n---\n\n# Accepted requirement plan\n\n"
        "| ID／問題 | 本版明確納入的修正 | 既有落點／需要補的範圍 | 必須取得的驗收證據 |\n"
        "| --- | --- | --- | --- |\n"
        "| R01 需求 R01 | 範圍 R01 | 交付脈絡 R01 | 逐項驗收 R01 |\n",
        encoding="utf-8",
    )
    authority_sha256 = hashlib.sha256(authority_path.read_bytes()).hexdigest()
    return {
        "schema": "cortex/requirement-manifest/v1",
        "manifest_id": "cli-fixture-requirements",
        "authority_ref": {"kind": "accepted-plan", "locator": authority_locator, "revision": authority_sha256},
        "requirements": [
            {
                "id": "R01",
                "revision": "r1",
                "title": "需求 R01",
                "source_ref": {"locator": authority_locator, "sha256": authority_sha256},
                "acceptance_criteria": [{"id": "R01-AC1", "description": "範圍 R01; 交付脈絡 R01; 逐項驗收 R01"}],
                "evidence_policy": {
                    "required_stages": ["source", "test", "review", "merge", "installed", "live"],
                    "waivable_stages": [],
                    "max_age_seconds": {"installed": 604800, "live": 604800},
                    "policy_version": "delivery-cli-fixture/v1",
                    "owner": {"id": "delivery-owner", "work_ids": ["#845"], "recovery": "cortex work show requirement-delivery-accounting"},
                },
            }
        ],
        "waiver_policy": {"authorities": []},
    }


def _cli_qualification_payload(*, candidate_sha: str, wheel_sha256: str) -> dict:
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
        "status": "passed",
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


def _cli_write_live_receipt(
    root: Path, *, target: dict, kind: str, evidence: dict, canary_target: dict | None = None
) -> dict:
    payload = {
        "schema": "cortex/live-canary-receipt/v1",
        "result": "passed",
        "requirement_id": "R01",
        "requirement_revision": "r1",
        "acceptance_id": "R01-AC1",
        "observed_at": "2026-09-25T00:00:00+00:00",
        "target": target,
        "authority": {"id": "release-operator", "version": "1", "receipt": "approval:123"},
        # #845 對抗審查第九輪 BLOCKER GREEN：canary_domain 不再自報，改由
        # production `live_receipt_validators.derive_canary_domain` 依 kind／
        # evidence 導出（`porcelain/delivery.py` 已固定接線）。
        "independence": {"review_domain": "reviewer-domain"},
        "kind": kind,
        "evidence": evidence,
    }
    if canary_target is not None:
        # #845 對抗審查 BLOCKER：deployment-canary receipt 額外帶入外部 canary
        # 身分與 evidence 目錄 locator，見 `live_receipt_validators.
        # make_governed_live_receipt_validator`。
        payload["canary_target"] = canary_target
    path = root / "live-cli-R01.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return {"locator": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _cli_copy_qualification_module(source_root: Path) -> None:
    """把真正的 `qualification/validate.py` 複製進 CLI `--source-root`（模擬
    Manager 實際 checkout），證明 production validator 改走動態載入後，`cortex
    delivery gaps` 這條 CLI 入口仍能吃到真檔。"""
    real_module = Path(__file__).resolve().parents[1] / "qualification" / "validate.py"
    target_dir = source_root / "qualification"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(real_module, target_dir / "validate.py")


def _cli_full_canary_qualification(
    evidence_base: Path, *, candidate_sha: str, wheel_sha256: str, repository: str
) -> dict:
    """CLI E2E 版本的完整 deployment-canary evidence tree 建構；邏輯與
    `test_requirement_delivery.py` 的 `_full_canary_qualification` 相同（各自
    獨立維護，兩個測試檔本來就各自複製 qualification payload fixture），確保
    `cortex delivery gaps` 這條 production CLI 入口也真的走過 `evidence_root`
    逐檔核對，不是只驗證 unit-level 呼叫。"""
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
    work_id = "canary-dispatch-1"
    issue = 845
    payload = {
        "schema_version": 2,
        "profile": "deployment-canary",
        "status": "passed",
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
        "repository": repository,
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
            "executor": "codex",
            "model_id": "gpt-5.3-codex-spark",
            "card_id": "worktree-isolation",
            "builder_job_ids": ["build-job"],
            "successful_command_count": 1,
            "all_outputs_nonempty": True,
            "command_sha256": "5" * 64,
            "output_sha256": "6" * 64,
            "log_sha256": "7" * 64,
            "thread_sha256": "8" * 64,
            "runtime_model": "gpt-5.3-codex-spark",
            "runtime_effort": "xhigh",
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
        "repository": repository,
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


def _cli_snapshot(root: Path, *, target: dict, completion_ref: dict, live_ref: dict) -> dict:
    return {
        "schema": "cortex/requirement-evidence-snapshot/v1",
        "captured_at": "2026-09-25T00:01:00+00:00",
        "snapshot_revision": 1,
        "mappings": [
            {
                "requirement_id": "R01",
                "requirement_revision": "r1",
                "acceptance_ids": ["R01-AC1"],
                "repo": _CLI_REPO,
                "work_id": _CLI_WORK,
                "run_id": "run-1",
                "workflow_step_ids": ["build-1", "verify-1", "review-1", "ship-1"],
                "source_generation": 1,
                "candidate_sha": _CLI_HEAD,
                "pr_number": 7,
                "change": "delivery-work",
                "todo_paths": ["docs/todo.md"],
                "profile_key": target["profile_key"],
                "config_revision": target["config_revision"],
                "policy_version": "delivery-cli-fixture/v1",
                "target": target,
                "completion_record": completion_ref,
                "installed_runtime": {
                    "state_root": "runtime-0",
                    "service": "manager",
                    "instance": "default",
                    "expected_pid": 555,
                    "declared_config_revision": target["config_revision"],
                    "target": target,
                },
                "live_receipt": live_ref,
            }
        ],
        "waivers": [],
    }


class _CliGitHub:
    def fetch_remote_closure(self, **kwargs) -> RemoteClosureFacts:
        return RemoteClosureFacts(
            merge_commit=_CLI_MERGE,
            pr_head=_CLI_HEAD,
            merge_parents=(_CLI_MERGE, _CLI_HEAD),
            default_head=_CLI_MERGE,
            merge_is_ancestor=True,
            merge_is_merge_commit=True,
            issue_states={845: "closed"},
            active_openspec_absent=True,
            archive_present=True,
            todo_complete=True,
            todo_revisions={"docs/todo.md": "6" * 40},
            completion_record_valid=True,
            closing_issues=(845,),
        )


def _prepare_cli_delivery_gate(
    monkeypatch, tmp_path: Path, *, kind: str, evidence: dict, canary_target: dict | None = None
) -> dict:
    """把 `cortex delivery gaps` 唯一觸網／觸 loaded-runtime 的邊界換成固定 fixture，
    其餘一律走 production code path（manifest/snapshot 解析、CompletionRecord、
    封閉登記表 live receipt validator）。回傳供 `delivery.main` 使用的 argv 片段。

    `--source-root` 與這裡的 tmp_path 相同，因此一律先放一份真正的
    `qualification/validate.py` 進去（#845 對抗審查 MAJOR：production validator
    改為動態從 `--source-root` 載入，不再依賴一般 import）；kind 不是
    deployment-canary-qualification 時這份檔案就是多餘但無害。"""
    _cli_copy_qualification_module(tmp_path)
    config_revision = _cli_config_revision()
    target = _cli_target(config_revision)
    authority = _cli_authority()
    completion_ref = _cli_write_completion(tmp_path, authority=authority)
    live_ref = _cli_write_live_receipt(
        tmp_path, target=target, kind=kind, evidence=evidence, canary_target=canary_target
    )
    manifest = _cli_manifest(tmp_path)
    snapshot = _cli_snapshot(tmp_path, target=target, completion_ref=completion_ref, live_ref=live_ref)
    manifest_path = tmp_path / "manifest.json"
    snapshot_path = tmp_path / "snapshot.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    monkeypatch.setattr(delivery, "selected_instance", lambda: "default")
    monkeypatch.setattr(delivery.paths, "coordinator_root", lambda: tmp_path)
    monkeypatch.setattr(delivery.time, "time", lambda: _CLI_NOW)
    monkeypatch.setattr(delivery.claim, "load_work_authority", lambda *, repo, work_id: authority)
    monkeypatch.setattr(delivery.github_delivery, "GitHubDeliveryClient", lambda: _CliGitHub())
    monkeypatch.setattr(
        delivery,
        "_runtime_status_report",
        lambda service, instance: {
            "status": "match",
            "trust_root": {"status": "verified"},
            "loaded": {
                "receipt_id": "00000000-0000-4000-8000-000000000001",
                "service": service,
                "instance": instance,
                "pid": 555,
                "artifact": {
                    "kind": "installed-wheel",
                    "sha256": target["artifact_sha256"],
                    "source_revision": target["source_revision"],
                },
                "config": {
                    "effective_revision": target["config_revision"],
                    "components": {"profile_key": target["profile_key"].rsplit(":", 1)[-1]},
                },
            },
        },
    )
    return {
        "manifest_path": manifest_path,
        "snapshot_path": snapshot_path,
    }


def test_delivery_gaps_cli_with_governed_qualification_receipt_reaches_ready(monkeypatch, tmp_path: Path, capsys) -> None:
    """#845 對抗審查 BLOCKER／MAJOR GREEN：`cortex delivery gaps` 這條 production
    CLI 入口，帶真正的 evidence 目錄、外部 canary 身分，且 `qualification/
    validate.py` 由 `--source-root` 動態載入，才能到達 ready。"""
    target = _cli_target(_cli_config_revision())
    # #845 對抗審查第九輪 MAJOR：evidence 目錄 locator 必須精確等於這五個
    # target 欄位的規範 digest，不再是自由選擇的 `"canary-evidence"` 字面路徑。
    evidence_scope = live_receipt_validators.deployment_canary_evidence_scope(target)
    evidence = _cli_full_canary_qualification(
        tmp_path / evidence_scope,
        candidate_sha=target["candidate_sha"],
        wheel_sha256=target["artifact_sha256"],
        repository=target["repo"],
    )
    canary_target = {
        "repository": target["repo"],
        "work_id": "canary-dispatch-1",
        "issue": 845,
        "evidence_directory": evidence_scope,
    }
    paths_ = _prepare_cli_delivery_gate(
        monkeypatch,
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence=evidence,
        canary_target=canary_target,
    )
    exit_code = delivery.main([
        "gaps",
        "--manifest", str(paths_["manifest_path"]),
        "--snapshot", str(paths_["snapshot_path"]),
        "--source-root", str(tmp_path),
        "--checkout", f"{_CLI_REPO}={tmp_path}",
    ])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["closure_readiness"] == "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "verified"


def test_delivery_gaps_cli_with_governed_qualification_receipt_missing_source_root_module_is_rejected(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """#845 對抗審查 MAJOR：`--source-root` 沒有 `qualification/` 時（模擬已安裝
    wheel 在 checkout 外執行），即使 evidence 完全合法，CLI 入口也必須 fail
    closed，不能因為缺套件就放行。"""
    target = _cli_target(_cli_config_revision())
    evidence_scope = live_receipt_validators.deployment_canary_evidence_scope(target)
    evidence = _cli_full_canary_qualification(
        tmp_path / evidence_scope,
        candidate_sha=target["candidate_sha"],
        wheel_sha256=target["artifact_sha256"],
        repository=target["repo"],
    )
    canary_target = {
        "repository": target["repo"],
        "work_id": "canary-dispatch-1",
        "issue": 845,
        "evidence_directory": evidence_scope,
    }
    paths_ = _prepare_cli_delivery_gate(
        monkeypatch,
        tmp_path,
        kind=live_receipt_validators.KIND_DEPLOYMENT_CANARY_QUALIFICATION,
        evidence=evidence,
        canary_target=canary_target,
    )
    # `_prepare_cli_delivery_gate` 已放好 `qualification/validate.py`；這裡故意
    # 移除，模擬已安裝 wheel 在 checkout 外執行的情境。
    shutil.rmtree(tmp_path / "qualification")
    exit_code = delivery.main([
        "gaps",
        "--manifest", str(paths_["manifest_path"]),
        "--snapshot", str(paths_["snapshot_path"]),
        "--source-root", str(tmp_path),
        "--checkout", f"{_CLI_REPO}={tmp_path}",
    ])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["closure_readiness"] != "ready"
    assert report["mappings"][0]["evidence"]["live"]["status"] == "failed"


def test_delivery_gaps_cli_with_unknown_live_receipt_kind_reports_gap(monkeypatch, tmp_path: Path, capsys) -> None:
    target = _cli_target(_cli_config_revision())
    evidence = _cli_qualification_payload(candidate_sha=target["candidate_sha"], wheel_sha256=target["artifact_sha256"])
    paths_ = _prepare_cli_delivery_gate(
        monkeypatch, tmp_path, kind="cortex/some-unregistered-kind/v1", evidence=evidence
    )
    exit_code = delivery.main([
        "gaps",
        "--manifest", str(paths_["manifest_path"]),
        "--snapshot", str(paths_["snapshot_path"]),
        "--source-root", str(tmp_path),
        "--checkout", f"{_CLI_REPO}={tmp_path}",
    ])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0  # `gaps` 唯讀且永遠回 0；readiness 本身才是判斷依據
    assert report["closure_readiness"] != "ready"
    live = report["mappings"][0]["evidence"]["live"]
    assert live["status"] == "failed"
    # #845 對抗審查第九輪 BLOCKER：kind 未登記時，`derive_canary_domain` 就
    # 已經無法導出 canary_domain，因此在到達封閉登記表 validator 之前就已在
    # independence 檢查 fail closed；不再是先前的
    # `governed-live-receipt-validator-rejected`。
    assert live["reason"] == "live-canary-independence-mismatch"
    gap_stages = {gap.get("stage") for gap in report.get("gaps", [])}
    assert "live" in gap_stages
