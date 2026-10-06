"""Issue #1309：每則送出的 task-memory note 必填處置與原因（task_memory_disposition）。

本檔為 T1 RED regression tests，固定以下行為：
1. 有送記憶的 attempt，terminal schema 要求 task_memory_disposition；沒送記憶時不要求；
2. Manager 驗證 note_id 集合等於實際送出的集合；
3. 缺欄位、格式錯、集合不符時記為 unreported receipt 並附原因，卡片判定與沒有此欄位時完全相同；
4. verdict 只接受 applied／consulted_no_change／not_relevant／stale_or_wrong／already_known／not_read；
   reason 必填且 ≤140 字；
5. _extract_terminal_payload 在形狀驗證前拆掉 task_memory_disposition；
6. 既有的 task_memory_applied 照舊可解析，新舊並存時 disposition 優先且兩者皆被妥善處理。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from paulsha_cortex.coordinator import launcher as launcher_module
from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import task_memory
from paulsha_cortex.coordinator import task_memory_hippo
from paulsha_cortex.coordinator import terminal_contract as tc
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.task_memory import TaskMemoryReceiptStore
from paulsha_cortex.coordinator.workflow import WorkflowStep


NOTE_CONTENT = "Keep the parser boundary scoped to the caller.\n"
NOTE_HASH = hashlib.sha256(NOTE_CONTENT.encode()).hexdigest()
ARTIFACT_REF = "src/parser.py"
ARTIFACT = b"def parse(value):\n    return value.strip()\n"
ARTIFACT_SHA = hashlib.sha256(ARTIFACT).hexdigest()
REPO = "acme/demo"
WORK_ID = "task-memory-disposition"
CLAIM_KEY = "claim:v1:" + "5" * 64
_ABSENT = object()

EXPECTED_VERDICTS = frozenset({
    "applied",
    "consulted_no_change",
    "not_relevant",
    "stale_or_wrong",
    "already_known",
    "not_read",
})


def _valid_disposition_entry(**overrides: object) -> dict[str, object]:
    return {
        "note_id": "note-1",
        "verdict": "consulted_no_change",
        "reason": "Existing boundary already handles caller-scoped normalization.",
        **overrides,
    }


def _payload(request: dict) -> dict:
    """Hippo public payload fixture（inline 交付，一則 note）。"""
    task_id = request["task_id"]
    project = request["project"]
    candidate = {
        "ref": "candidate-1",
        "note_id": "note-1",
        "rank": 1,
        "summary": "Reuse the task-local parser boundary.",
        "excerpt": "Apply the shared parser at the caller boundary.",
        "authorization": {"status": "authorized"},
        "availability": {"status": "available"},
        "content_hash": NOTE_HASH,
        "content_version": "v7",
        "applicability": ["same repository", "parser task"],
        "relevance_reason": "The current task changes parsing behavior.",
        "source_time": "2026-09-25T12:00:00Z",
        "project": project,
    }
    manifest = {
        "schema": "hippo/task-memory-manifest/v1",
        "task_id": task_id,
        "project": project,
        "entries": [
            {
                "note_id": "note-1",
                "content_hash": NOTE_HASH,
                "content_version": "v7",
                "project": project,
            }
        ],
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return {
        "schema_version": "1",
        "task_id": task_id,
        "intent": request["intent"],
        "project": project,
        "candidates": [candidate],
        "delivery": {
            "mode": request["delivery"]["mode"],
            "capabilities": dict(request["delivery"]["capabilities"]),
            "manifest": manifest,
        },
        "evidence": [],
        "producer": {"id": "hippo-core", "version": "fixture"},
        "adapter": {"id": "host-adapter", "version": "fixture"},
    }


class _FakeHippoClient:
    """取代 subprocess Hippo client：只提供 public payload，不做 note fetch。"""

    @classmethod
    def from_environment(cls):
        return cls()

    def callbacks(self, _context):
        return _payload, None


def _git(path: Path, *args: str) -> str:
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = "2000-01-01T00:00:00Z"
    env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00Z"
    result = subprocess.run(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def _init_candidate_worktree(path: Path) -> str:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    artifact = path / ARTIFACT_REF
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(ARTIFACT)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "candidate")
    return _git(path, "rev-parse", "HEAD")


def _write_ledger(log_path: Path) -> None:
    path = tc.gate_ledger_path(log_path)
    payload = {
        "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
        "kind": "workflow-gate-ledger",
        "slice_id": "slice",
        "gates": [{"name": "pytest", "status": "passed", "exit_code": 0}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _prepare_dispatch_memory(monkeypatch, registry, run, job, coordinator_root, *, deliver=True):
    monkeypatch.setenv("PSC_TASK_MEMORY_ENABLED", "1")
    monkeypatch.setattr(manager, "_task_memory_work_item_title", lambda *_a, **_k: None)
    monkeypatch.setattr(task_memory_hippo, "HippoTaskMemoryClient", _FakeHippoClient)
    result = manager._prepare_task_memory_dispatch(
        run=run,
        step=run.steps[0],
        job=job,
        registry=registry,
        coordinator_root=coordinator_root,
    )
    assert result is not None
    adapter, prepared = result
    assert prepared.status == "offered" and prepared.mode == "inline"
    if deliver:
        manager._record_task_memory_events(
            registry,
            adapter.confirm_context_delivered(prepared),
            coordinator_root=coordinator_root,
        )
    monkeypatch.delenv("PSC_TASK_MEMORY_ENABLED")
    return prepared


def _build_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    disposition: object = _ABSENT,
    applied: object = _ABSENT,
    memory: str = "delivered",
):
    worktree = tmp_path / "worktree"
    candidate = _init_candidate_worktree(worktree)
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    step = WorkflowStep(
        phase="build",
        persona="builder",
        card="implement",
        executor="codex",
        model="gpt-builder",
        domain="openai",
        inputs=(),
        outputs=(),
        test_policy="focused",
    )
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key=CLAIM_KEY,
        source_revision="6" * 64,
        workspace_root=str(worktree),
        combo="feature-oneshot",
        current_phase="build",
        steps=(step,),
        attempts={},
        gate_status="running",
    )
    coordinator_root = tmp_path / "coordinator"

    def create():
        return registry.create_job(
            task="build",
            persona="builder",
            branch="feature/task-memory-disposition",
            pane="",
            worktree=str(worktree),
            executor="codex",
            model_id="gpt-builder",
            independence_domain="openai",
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card=step.card,
            workflow_phase="build",
            workflow_repo_root=str(worktree),
            workflow_outputs=(),
            source_revision=run.source_revision,
        )

    prior = create()
    registry.update_headless_result(prior["job_id"], status="failed", exit_code=1)
    job = create()
    if memory in {"delivered", "offered"}:
        _prepare_dispatch_memory(
            monkeypatch,
            registry,
            run,
            job,
            coordinator_root,
            deliver=memory == "delivered",
        )
    terminal: dict[str, object] = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": run.run_id,
        "card_id": step.card,
        "candidate": candidate,
        "outputs": [],
    }
    if disposition is not _ABSENT:
        terminal["task_memory_disposition"] = disposition
    if applied is not _ABSENT:
        terminal["task_memory_applied"] = applied
    log = tmp_path / "build.jsonl"
    log.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
    _write_ledger(log)
    registry.attach_launch_handle(
        job["job_id"], executor="codex", model_id="gpt-builder", log_path=str(log)
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    return registry, run, registry.get_job(job["job_id"]), coordinator_root, worktree


def _receipts(coordinator_root: Path, run) -> list[dict]:
    return TaskMemoryReceiptStore(coordinator_root).events_for_run(run.repo, run.work_id, run.run_id)


def _disposition_reported(coordinator_root: Path, run) -> list[dict]:
    return [row for row in _receipts(coordinator_root, run) if row["event"] == "disposition-reported"]


def _unreported(coordinator_root: Path, run) -> list[dict]:
    return [row for row in _receipts(coordinator_root, run) if row["event"] == "unreported"]


def _evidence_bytes(bound: dict, coordinator_root: Path) -> bytes:
    locator = bound["workflow_evidence"]
    assert isinstance(locator, dict)
    return (coordinator_root / locator["path"]).read_bytes()


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


@pytest.fixture
def baseline_evidence(tmp_path, monkeypatch) -> bytes:
    registry, _run, job, root, _wt = _build_fixture(
        tmp_path / "baseline", monkeypatch, memory="none"
    )
    bound = manager.terminalize_workflow_job(registry, job_id=job["job_id"], coordinator_root=root)
    return _evidence_bytes(bound, root)


# ---------------------------------------------------------------------------
# 1. 常數與 verdict 範圍
# ---------------------------------------------------------------------------


def test_task_memory_disposition_constants_and_verdicts():
    """T1：驗證 TASK_MEMORY_DISPOSITION_TERMINAL_FIELD 與 6 個合法 verdict。"""
    field_name = getattr(task_memory, "TASK_MEMORY_DISPOSITION_TERMINAL_FIELD", None)
    assert field_name == "task_memory_disposition", (
        "TASK_MEMORY_DISPOSITION_TERMINAL_FIELD must be 'task_memory_disposition'"
    )

    verdicts = getattr(task_memory, "TASK_MEMORY_DISPOSITION_VERDICTS", None)
    assert verdicts is not None, "TASK_MEMORY_DISPOSITION_VERDICTS must be defined"
    assert set(verdicts) == EXPECTED_VERDICTS, (
        f"Verdicts must be exactly {EXPECTED_VERDICTS}, got {verdicts}"
    )


def test_task_memory_disposition_json_schema():
    """T1 & T2：terminal schema 的 task_memory_disposition JSON schema。"""
    schema_fn = getattr(task_memory, "task_memory_disposition_json_schema", None)
    assert callable(schema_fn), "task_memory_disposition_json_schema must be callable"

    schema = schema_fn()
    assert isinstance(schema, dict)
    assert schema.get("type") == "array"
    items = schema.get("items", {})
    assert items.get("type") == "object"
    assert set(items.get("required", [])) == {"note_id", "verdict", "reason"}
    props = items.get("properties", {})
    assert "note_id" in props
    assert "verdict" in props
    assert set(props["verdict"].get("enum", [])) == EXPECTED_VERDICTS
    assert props["reason"].get("type") == "string"
    assert props["reason"].get("maxLength") == 140
    _assert_gemini_schema_subset(schema)


# ---------------------------------------------------------------------------
# 2. 純函式解析與驗證（parse_task_memory_disposition）
# ---------------------------------------------------------------------------


def test_parse_task_memory_disposition_accepts_valid_entries():
    """T1：六個合法 verdict 與 ≤140 字 reason 均可正規化解析。"""
    parse_fn = getattr(task_memory, "parse_task_memory_disposition", None)
    assert callable(parse_fn), "parse_task_memory_disposition must be implemented"

    for verdict in sorted(EXPECTED_VERDICTS):
        entry = _valid_disposition_entry(verdict=verdict, reason=f"Valid reason for {verdict}")
        result = parse_fn([entry])
        assert len(result) == 1
        assert result[0]["note_id"] == "note-1"
        assert result[0]["verdict"] == verdict
        assert result[0]["reason"] == f"Valid reason for {verdict}"

    # 140 字上限剛好符合
    boundary_reason = "x" * 140
    result = parse_fn([_valid_disposition_entry(reason=boundary_reason)])
    assert result[0]["reason"] == boundary_reason


@pytest.mark.parametrize(
    "invalid_value",
    [
        None,
        "not-a-list",
        {"note_id": "note-1"},
        [_valid_disposition_entry(verdict="invalid_verdict")],
        [_valid_disposition_entry(verdict="")],
        [_valid_disposition_entry(verdict="ignored")],
        [_valid_disposition_entry(reason="")],  # reason 必填
        [_valid_disposition_entry(reason="   ")],  # 空白無效
        [_valid_disposition_entry(reason="x" * 141)],  # 超過 140 字
        [_valid_disposition_entry(reason=None)],
        [_valid_disposition_entry(note_id="")],
        [_valid_disposition_entry(), _valid_disposition_entry()],  # 重複 note_id
        [{"note_id": "note-1", "verdict": "applied"}],  # 缺 reason
        [{"note_id": "note-1", "reason": "reason"}],  # 缺 verdict
    ],
)
def test_parse_task_memory_disposition_rejects_malformed_values(invalid_value):
    """T1：任何格式錯誤、非法 verdict 或超出字數均拋出 ValueError。"""
    parse_fn = getattr(task_memory, "parse_task_memory_disposition", None)
    assert callable(parse_fn), "parse_task_memory_disposition must be implemented"

    with pytest.raises(ValueError):
        parse_fn(invalid_value)


def test_parse_task_memory_disposition_note_id_set_validation():
    """T1：Manager / parser 驗證 note_id 集合必須等於實際送出的集合。"""
    validate_fn = getattr(task_memory, "validate_disposition_note_ids", None)
    if validate_fn is None:
        parse_fn = getattr(task_memory, "parse_task_memory_disposition", None)
        assert callable(parse_fn), "parse_task_memory_disposition must be implemented"
        # 若以 expected_note_ids 參數傳入
        with pytest.raises(ValueError):
            parse_fn([_valid_disposition_entry(note_id="note-1")], expected_note_ids={"note-1", "note-2"})
        with pytest.raises(ValueError):
            parse_fn([_valid_disposition_entry(note_id="note-9")], expected_note_ids={"note-1"})
    else:
        assert validate_fn(
            [{"note_id": "note-1"}], expected_note_ids={"note-1"}
        ) is True
        assert validate_fn(
            [{"note_id": "note-1"}], expected_note_ids={"note-1", "note-2"}
        ) is False


# ---------------------------------------------------------------------------
# 3. 抽取與剝離：_extract_terminal_payload 在形狀驗證前拆掉 task_memory_disposition
# ---------------------------------------------------------------------------


def test_extract_terminal_payload_strips_disposition_field(tmp_path):
    """T3：在形狀驗證前拆出 task_memory_disposition，不影響卡片採信。"""
    payload = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    log = tmp_path / "terminal.jsonl"
    disposition_data = [_valid_disposition_entry()]
    log.write_text(
        json.dumps({**payload, "task_memory_disposition": disposition_data}) + "\n",
        encoding="utf-8",
    )

    raw = manager._extract_terminal_json(str(log))
    assert "task_memory_disposition" not in raw, (
        "_extract_terminal_json must strip task_memory_disposition"
    )
    assert raw == payload


# ---------------------------------------------------------------------------
# 4. Terminal Schema 注入：有送記憶的 attempt 才要求該欄位
# ---------------------------------------------------------------------------


def test_terminal_schema_requires_disposition_only_when_memory_delivered(tmp_path, monkeypatch):
    """T1 & T2：有送記憶時 terminal schema 要求 task_memory_disposition；沒送記憶時不要求。"""
    # 情況 A：有送記憶
    registry_del, run_del, job_del, root_del, _wt = _build_fixture(
        tmp_path / "del", monkeypatch, memory="delivered"
    )
    contract_del = manager._workflow_card_prompt_contract(
        registry_del,
        run_del,
        run_del.steps[0],
        worktree=str(_wt),
        job=job_del,
        env={},
    )
    t_schema_del = contract_del.get("terminal_schema", {})
    assert "task_memory_disposition" in t_schema_del or (
        "properties" in t_schema_del and "task_memory_disposition" in t_schema_del["properties"]
    ), "Terminal schema must require/contain task_memory_disposition when memory is delivered"

    # 情況 B：沒送記憶
    registry_none, run_none, job_none, root_none, _wt = _build_fixture(
        tmp_path / "none", monkeypatch, memory="none"
    )
    contract_none = manager._workflow_card_prompt_contract(
        registry_none,
        run_none,
        run_none.steps[0],
        worktree=str(_wt),
        job=job_none,
        env={},
    )
    t_schema_none = contract_none.get("terminal_schema", {})
    assert "task_memory_disposition" not in t_schema_none and (
        "properties" not in t_schema_none
        or "task_memory_disposition" not in t_schema_none.get("properties", {})
    ), "Terminal schema must not contain task_memory_disposition when no memory was delivered"


# ---------------------------------------------------------------------------
# 5. Harvest 成功：合法填寫記為 disposition-reported receipt
# ---------------------------------------------------------------------------


def test_build_terminal_disposition_records_disposition_reported_receipt(
    tmp_path, monkeypatch, baseline_evidence
):
    """T1 & T3：合法回報寫入 disposition-reported receipt，含 verdict 與 reason sha256。"""
    disposition = [_valid_disposition_entry(
        note_id="note-1",
        verdict="consulted_no_change",
        reason="Boundary check shows callers already normalized.",
    )]
    registry, run, job, root, _wt = _build_fixture(
        tmp_path / "case", monkeypatch, disposition=disposition, memory="delivered"
    )

    bound = manager.terminalize_workflow_job(
        registry, job_id=job["job_id"], coordinator_root=root
    )

    reported = _disposition_reported(root, run)
    assert len(reported) == 1, "Must record exactly one disposition-reported event"
    receipt = reported[0]
    assert receipt["event"] == "disposition-reported"
    assert receipt["note_id"] == "note-1"
    assert receipt["verdict"] == "consulted_no_change"
    expected_reason_hash = hashlib.sha256(
        "Boundary check shows callers already normalized.".encode("utf-8")
    ).hexdigest()
    assert receipt["reason_sha256"] == expected_reason_hash
    # 原文不洩漏 note 內容
    assert NOTE_CONTENT not in json.dumps(receipt)
    # 卡片採信結果不受影響，與 baseline 逐位元組相同
    assert _evidence_bytes(bound, root) == baseline_evidence


# ---------------------------------------------------------------------------
# 6. Harvest 負向：缺欄位、格式錯、集合不符記為 unreported 並附原因
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_name, disposition_input, expected_reason_fragment",
    [
        ("missing-field", _ABSENT, "missing"),
        ("malformed-verdict", [_valid_disposition_entry(verdict="unknown_verdict")], "malformed"),
        ("malformed-reason-too-long", [_valid_disposition_entry(reason="x" * 141)], "malformed"),
        ("note-outside-manifest", [_valid_disposition_entry(note_id="note-999")], "note-set-mismatch"),
        ("empty-list-when-delivered", [], "note-set-mismatch"),
    ],
)
def test_invalid_or_missing_disposition_records_unreported_and_preserves_card_result(
    case_name, disposition_input, expected_reason_fragment, tmp_path, monkeypatch, baseline_evidence
):
    """T1 & T3：缺漏、格式錯、集合不符均記為 unreported receipt，卡片採信結果逐位元組不變。"""
    registry, run, job, root, _wt = _build_fixture(
        tmp_path / case_name,
        monkeypatch,
        disposition=disposition_input,
        memory="delivered",
    )

    bound = manager.terminalize_workflow_job(
        registry, job_id=job["job_id"], coordinator_root=root
    )

    # 卡片判定完全不受影響
    assert bound["workflow_evidence"] is not None
    assert _evidence_bytes(bound, root) == baseline_evidence

    # 必須寫入 unreported 事件，並附原因
    unreported_events = _unreported(root, run)
    assert len(unreported_events) >= 1, (
        f"Case {case_name}: must record at least one unreported event"
    )
    event = unreported_events[0]
    assert event["event"] == "unreported"
    assert "reason" in event
    assert expected_reason_fragment in event["reason"].lower()


# ---------------------------------------------------------------------------
# 7. 回歸：沒送記憶的 attempt 行為完全不變
# ---------------------------------------------------------------------------


def test_no_disposition_receipts_when_attempt_has_no_memory(tmp_path, monkeypatch, baseline_evidence):
    """沒送記憶的 attempt 不得寫入 disposition-reported 或 unreported receipt。"""
    registry, run, job, root, _wt = _build_fixture(
        tmp_path / "nomem", monkeypatch, memory="none"
    )

    bound = manager.terminalize_workflow_job(
        registry, job_id=job["job_id"], coordinator_root=root
    )

    assert _disposition_reported(root, run) == []
    assert _unreported(root, run) == []
    assert _evidence_bytes(bound, root) == baseline_evidence


# ---------------------------------------------------------------------------
# 8. 新舊並存：task_memory_disposition 與 task_memory_applied 並存
# ---------------------------------------------------------------------------


def test_coexistence_with_legacy_task_memory_applied(tmp_path, monkeypatch):
    """Boundary：既有的 task_memory_applied 照舊可解析；新舊並存時兩者均被妥善處理。"""
    applied_data = [{
        "note_id": "note-1",
        "evidence_ref": ARTIFACT_REF,
        "evidence_sha256": ARTIFACT_SHA,
    }]
    disposition_data = [_valid_disposition_entry(
        note_id="note-1",
        verdict="applied",
        reason="Applied in parser caller boundary.",
    )]
    registry, run, job, root, _wt = _build_fixture(
        tmp_path / "both",
        monkeypatch,
        disposition=disposition_data,
        applied=applied_data,
        memory="delivered",
    )

    manager.terminalize_workflow_job(
        registry, job_id=job["job_id"], coordinator_root=root
    )

    reported = _disposition_reported(root, run)
    assert len(reported) == 1, "disposition-reported must be recorded"
