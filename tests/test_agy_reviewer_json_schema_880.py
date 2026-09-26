"""RED regression coverage for issue #880's AGY reviewer terminal contract."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.launcher import SubprocessLauncher, build_agy_argv
from paulsha_cortex.coordinator.registry import JobRegistry


REVIEW_KINDS = ("workflow-verification-result", "workflow-review-result")


@pytest.fixture(autouse=True)
def _clear_agy_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(launcher_module.AGY_PRINT_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(launcher_module.gate_ledger.GATE_TIMEOUT_ENV, raising=False)


def _verification_terminal(
    *, details: object, report_ref: str, status: str = "verified"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": status,
        "summary": "verification passed",
        "details": details,
        "reports": [{"path": report_ref, "body": "# Verification\n\nPassed.\n"}],
    }


def _verification_fixture(
    tmp_path: Path, *, details: object, status: str = "verified"
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    coordinator_root = tmp_path / "coordinator"
    report_ref = "reports/verify/fix-agy-reviewer-json-schema.md"
    log_path = tmp_path / "verification.jsonl"
    log_path.write_text(
        json.dumps(
            _verification_terminal(details=details, report_ref=report_ref, status=status)
        )
        + "\n",
        encoding="utf-8",
    )

    registry = JobRegistry(state_path=coordinator_root / "jobs.json")
    job = registry.create_job(
        task="verification",
        persona="reviewer",
        kind="review",
        branch="feature/880-fix-agy-reviewer-json-schema",
        pane="",
        worktree=str(repo_root),
        executor="agy",
        model_id="gemini-3.1-pro-high",
        independence_domain="google",
        subject_head="a" * 40,
        workflow_run_id="workflow-880",
        workflow_claim_key="claim-880",
        workflow_repo="owner/repo",
        workflow_card="verification",
        workflow_phase="verify",
        workflow_repo_root=str(repo_root),
        workflow_outputs=(report_ref,),
        source_revision="source-880",
    )
    registry.attach_launch_handle(
        job["job_id"],
        executor="agy",
        model_id="gemini-3.1-pro-high",
        log_path=str(log_path),
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    return registry, job, coordinator_root, report_ref


def _evidence_payload(bound_job: dict[str, object], coordinator_root: Path) -> dict[str, object]:
    locator = bound_job["workflow_evidence"]
    assert isinstance(locator, dict)
    evidence_path = coordinator_root / str(locator["path"])
    return json.loads(evidence_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("kind", REVIEW_KINDS)
def test_agy_reviewer_uses_the_shared_claude_schema(kind: str, tmp_path: Path) -> None:
    argv = build_agy_argv(
        prompt="inspect",
        slice_id="verify-880",
        log_dir=str(tmp_path / "logs"),
        worktree=str(tmp_path / "reviewer"),
        review_only=True,
        review_terminal_kind=kind,
    )

    schema = json.loads(argv[argv.index("--json-schema") + 1])
    # #888：agy 走 Gemini structured output，schema 是同一份 Claude 契約經
    # `_gemini_compatible_schema` 改寫（整數 enum→min/max、null type→nullable）。
    assert schema == json.loads(launcher_module._gemini_review_json_schema(kind))
    assert schema == launcher_module._gemini_compatible_schema(
        json.loads(launcher_module._claude_review_json_schema(kind))
    )
    _assert_gemini_schema_subset(schema)


def _assert_gemini_schema_subset(node: object) -> None:
    if isinstance(node, list):
        for item in node:
            _assert_gemini_schema_subset(item)
        return
    if not isinstance(node, dict):
        return
    enum = node.get("enum")
    if enum is not None:
        assert all(isinstance(item, str) for item in enum), enum
    assert not isinstance(node.get("type"), list), node.get("type")
    for value in node.values():
        _assert_gemini_schema_subset(value)


def test_gemini_compatible_schema_rewrites_only_unsupported_shapes() -> None:
    rewritten = launcher_module._gemini_compatible_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "line": {"type": ["integer", "null"], "minimum": 1},
                "status": {"type": "string", "enum": ["passed", "failed"]},
            },
        }
    )
    assert rewritten == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"type": "integer", "minimum": 1, "maximum": 1},
            "line": {"type": "integer", "nullable": True, "minimum": 1},
            "status": {"type": "string", "enum": ["passed", "failed"]},
        },
    }
    # 字串鍵 map（authority_hashes）→ [{key, value}] 陣列；Gemini 沒有 map 型別，
    # 模型會把路徑鍵改寫成識別字，實機 job 503／505 的 ref set 因此對不上。
    assert launcher_module._gemini_compatible_schema(
        {"type": "object", "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}
    ) == {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["key", "value"],
            "properties": {
                "key": {"type": "string", "minLength": 1},
                "value": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
    }
    with pytest.raises(ValueError):
        launcher_module._gemini_compatible_schema({"enum": [1, 2]})
    with pytest.raises(ValueError):
        launcher_module._gemini_compatible_schema({"type": ["integer", "string"]})


@pytest.mark.parametrize(
    "review_only, review_terminal_kind",
    ((True, None), (False, "workflow-verification-result")),
)
def test_agy_reviewer_terminal_kind_validation(
    review_only: bool,
    review_terminal_kind: str | None,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        build_agy_argv(
            prompt="inspect",
            slice_id="verify-880",
            log_dir=str(tmp_path / "logs"),
            worktree=str(tmp_path / "reviewer"),
            review_only=review_only,
            review_terminal_kind=review_terminal_kind,
        )


def test_agy_reviewer_requires_terminal_kind_when_omitted(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match="^agy reviewer terminal contract kind missing$",
    ):
        build_agy_argv(
            prompt="inspect",
            slice_id="verify-880",
            log_dir=str(tmp_path / "logs"),
            worktree=str(tmp_path / "reviewer"),
            review_only=True,
        )


def test_agy_reviewer_rejects_json_envelope_opt_out(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match="^agy reviewer requires json envelope for terminal schema$",
    ):
        build_agy_argv(
            prompt="inspect",
            slice_id="verify-880",
            log_dir=str(tmp_path / "logs"),
            worktree=str(tmp_path / "reviewer"),
            review_only=True,
            review_terminal_kind="workflow-verification-result",
            json_envelope=False,
        )


def test_agy_probe_and_builder_shapes_do_not_gain_reviewer_schema(tmp_path: Path) -> None:
    probe = build_agy_argv(
        prompt="p",
        slice_id="cortex-capability-probe",
        log_dir=".",
        model="gemini-3.1-pro-high",
        read_only=True,
        json_envelope=False,
    )
    assert probe == [
        "agy",
        "--print",
        "p",
        "--mode",
        "plan",
        "--sandbox",
        "--print-timeout",
        "2400s",
        "--model",
        "gemini-3.1-pro-high",
    ]

    builder = build_agy_argv(
        prompt="implement",
        slice_id="build-880",
        log_dir=str(tmp_path / "logs"),
        worktree=str(tmp_path / "builder"),
    )
    assert builder[:2] == ["agy", "--print"]
    assert builder[2].startswith("implement\n\n")
    assert builder[3:] == [
        "--mode",
        "accept-edits",
        "--add-dir",
        str((tmp_path / "builder").resolve()),
        "--output-format",
        "json",
        "--print-timeout",
        "2400s",
    ]
    assert "--json-schema" not in builder


def test_agy_reviewer_launcher_forwards_terminal_kind_to_argv_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_builder(**kwargs: object) -> list[str]:
        calls.append(kwargs)
        kind = kwargs["review_terminal_kind"]
        return ["agy", "--json-schema", str(kind)]

    class FakeProcess:
        pid = 880

    monkeypatch.setitem(launcher_module._ARGV_BUILDERS, "agy", fake_builder)
    monkeypatch.setattr(
        launcher_module.subprocess,
        "Popen",
        lambda argv, **kwargs: FakeProcess(),
    )
    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")

    SubprocessLauncher(
        "agy",
        review_only=True,
        review_terminal_kind="workflow-verification-result",
    ).launch(
        slice_id="verification-880",
        prompt="inspect",
        worktree=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
    )

    assert calls and calls[0]["review_terminal_kind"] == "workflow-verification-result"


def test_verification_string_details_are_normalized_into_evidence(tmp_path: Path) -> None:
    registry, job, coordinator_root, _report_ref = _verification_fixture(
        tmp_path,
        details="AGY returned the verification details as text.",
    )

    bound = manager.terminalize_workflow_job(
        registry,
        job_id=job["job_id"],
        coordinator_root=coordinator_root,
    )

    evidence = _evidence_payload(bound, coordinator_root)
    assert evidence["payload"]["details"] == {
        "text": "AGY returned the verification details as text."
    }


def test_verification_empty_string_details_remain_invalid(tmp_path: Path) -> None:
    registry, job, coordinator_root, _report_ref = _verification_fixture(
        tmp_path,
        details="",
    )

    with pytest.raises(ValueError, match="workflow verification terminal schema invalid"):
        manager.terminalize_workflow_job(
            registry,
            job_id=job["job_id"],
            coordinator_root=coordinator_root,
        )


@pytest.mark.parametrize("status", ("failed", "needs_human"))
def test_non_passing_verification_does_not_normalize_details_warning(
    status: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry, job, coordinator_root, _report_ref = _verification_fixture(
        tmp_path,
        details="failure details",
        status=status,
    )

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        with pytest.raises(
            ValueError,
            match=rf"workflow verification terminal reported non-passing status: {status}",
        ):
            manager.terminalize_workflow_job(
                registry,
                job_id=job["job_id"],
                coordinator_root=coordinator_root,
            )

    assert not any(
        "workflow verification terminal details normalized from string" in record.getMessage()
        for record in caplog.records
    )


def test_verification_object_details_remain_unchanged(tmp_path: Path) -> None:
    registry, job, coordinator_root, _report_ref = _verification_fixture(
        tmp_path,
        details={"checks": {"pytest": "passed"}},
    )

    bound = manager.terminalize_workflow_job(
        registry,
        job_id=job["job_id"],
        coordinator_root=coordinator_root,
    )

    evidence = _evidence_payload(bound, coordinator_root)
    assert evidence["payload"]["details"] == {"checks": {"pytest": "passed"}}


def test_agy_structured_output_meta_keys_are_stripped_from_terminal_payload() -> None:
    # #888：agy --json-schema 的實機 response = 模型的 fenced 文字 + 最後一行
    # structured output（多掛 toolAction／toolSummary）。Manager 必須拿最後那行，
    # 並只剝掉這兩個固定的 CLI 中繼鍵，details 物件形狀原樣保留。
    from paulsha_cortex.coordinator import manager as manager_module

    response = (
        "```json\n"
        "{\n  \"schema_version\": 1,\n  \"kind\": \"workflow-verification-result\"\n}\n"
        "```\n"
        "{\"details\":{\"text\":\"ok\"},\"kind\":\"workflow-verification-result\","
        "\"reports\":[{\"body\":\"ok\",\"path\":\"reports/verify/x.md\"}],"
        "\"schema_version\":1,\"status\":\"verified\",\"summary\":\"ok\","
        "\"toolAction\":\"Finishing the interaction\",\"toolSummary\":\"Workflow verification result\"}\n"
    )
    parsed = manager_module._parse_terminal_json_text(response)
    assert parsed == {
        "details": {"text": "ok"},
        "kind": "workflow-verification-result",
        "reports": [{"body": "ok", "path": "reports/verify/x.md"}],
        "schema_version": 1,
        "status": "verified",
        "summary": "ok",
    }
    untouched = {"schema_version": 1, "kind": "workflow-review-result", "reports": [], "extra": 1}
    assert manager_module._strip_agy_structured_output_meta(untouched) is untouched


def test_manager_folds_agy_key_value_authority_hashes() -> None:
    from paulsha_cortex.coordinator import manager as manager_module

    digest = "0" * 64
    folded = manager_module._fold_agy_key_value_map(
        [{"key": "docs/superpowers/plans/x.md", "value": digest}]
    )
    assert folded == {"docs/superpowers/plans/x.md": digest}
    # 非 {key, value} 形狀、重複鍵、空鍵：原樣回傳，交由既有驗證 fail closed。
    for bad in (
        [{"key": "a", "value": digest, "extra": 1}],
        [{"key": "a", "value": digest}, {"key": "a", "value": digest}],
        [{"key": "", "value": digest}],
        {"a": digest},
        [],
    ):
        assert manager_module._fold_agy_key_value_map(bad) == bad
