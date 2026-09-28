"""Issue #1136：卡片 terminal 的 ``task_memory_applied`` 回報與 applied receipt 接線。

#857 的 adapter 已能產生 ``applied-with-evidence`` receipt，但正式卡片流程沒有任何
呼叫者：terminal 沒有欄位、harvest 不解析，「使用」這一層恆為 0。本檔釘住：

- terminal 帶合法 ``task_memory_applied`` → harvest 採信 terminal 之後，以該 attempt
  dispatch 時交付的 note（由 Manager receipt sidecar 重建）寫出 applied receipt；
- note 未交付／不在 manifest／屬於別的 attempt、evidence 不在 candidate、hash 不符、
  非 repo 相對路徑、畸形欄位 → 不產生 receipt、卡片採信結果逐位元組不變、只有
  class-only 診斷；
- 未帶欄位 → 與現況相同（回歸）；
- reviewer 的 Claude／AGY terminal schema 開放這個選填欄位，且仍在 Gemini 相容子集內；
- Hippo legacy strict KPI 分母不受影響（#857 R5）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import launcher as launcher_module
from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import task_memory
from paulsha_cortex.coordinator import task_memory_hippo
from paulsha_cortex.coordinator import terminal_contract as tc
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.task_memory import (
    TaskMemoryReceiptStore,
    parse_task_memory_applied,
    summarize_canary,
)
from paulsha_cortex.coordinator.workflow import WorkflowStep


NOTE_CONTENT = "Keep the parser boundary scoped to the caller.\n"
NOTE_HASH = hashlib.sha256(NOTE_CONTENT.encode()).hexdigest()
ARTIFACT_REF = "src/parser.py"
ARTIFACT = b"def parse(value):\n    return value.strip()\n"
ARTIFACT_SHA = hashlib.sha256(ARTIFACT).hexdigest()
REPO = "acme/demo"
WORK_ID = "task-memory-applied"
CLAIM_KEY = "claim:v1:" + "5" * 64
REPORT_REF = "reports/verify/task-memory-applied.md"
_ABSENT = object()
_WARNING = "task-memory applied evidence rejected"


def _valid_entry(**overrides: object) -> dict[str, object]:
    return {
        "note_id": "note-1",
        "evidence_ref": ARTIFACT_REF,
        "evidence_sha256": ARTIFACT_SHA,
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
    # 固定 author/committer 時間：兩個獨立 fixture 的 candidate SHA 逐字相同，
    # canonical evidence 因此可以逐位元組比較。
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
    """走正式 dispatch 路徑：`_prepare_task_memory_dispatch` 產生並寫入 offer receipt，
    launch 之後 `confirm_context_delivered` 寫入 context-delivered。"""

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
    applied: object = _ABSENT,
    memory: str = "delivered",
):
    """真實 registry／WorkflowRun／builder job＋真 git candidate 的 build 卡 harvest fixture。

    ``memory``：``"delivered"``（本 attempt offer＋context-delivered）、
    ``"offered"``（只有 offer，未確認交付）、``"none"``（本 attempt 沒有 task memory）、
    ``"other-attempt"``（同卡前一個 attempt 交付過，本 attempt 沒有）。
    """

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
            branch="feature/task-memory-applied",
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

    # 每個 fixture 都先有一個失敗的前次 attempt：registry 依序配發 job id，這讓
    # 所有 fixture 的本 attempt job id（因此 canonical evidence）逐字相同。
    prior = create()
    if memory == "other-attempt":
        _prepare_dispatch_memory(monkeypatch, registry, run, prior, coordinator_root)
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


def _applied(coordinator_root: Path, run) -> list[dict]:
    return [row for row in _receipts(coordinator_root, run) if row["event"] == "applied-with-evidence"]


def _evidence_bytes(bound: dict, coordinator_root: Path) -> bytes:
    locator = bound["workflow_evidence"]
    assert isinstance(locator, dict)
    return (coordinator_root / locator["path"]).read_bytes()


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "task-memory" in record.getMessage()
    ]


@pytest.fixture
def baseline_evidence(tmp_path, monkeypatch) -> bytes:
    """未帶欄位、沒有 task memory 的同一張卡：採信結果的逐位元組基準。"""

    registry, _run, job, root, _wt = _build_fixture(
        tmp_path / "baseline", monkeypatch, memory="none"
    )
    bound = manager.terminalize_workflow_job(registry, job_id=job["job_id"], coordinator_root=root)
    return _evidence_bytes(bound, root)


# ---------------------------------------------------------------------------
# 正向：build 卡 terminal 帶合法欄位 → applied receipt
# ---------------------------------------------------------------------------


def test_build_terminal_applied_entry_records_applied_with_evidence_receipt(
    tmp_path, monkeypatch, caplog, baseline_evidence
):
    registry, run, job, root, _wt = _build_fixture(
        tmp_path / "case", monkeypatch, applied=[_valid_entry()]
    )
    before = _receipts(root, run)
    assert [row["event"] for row in before] == [
        "candidate-selected", "offer-emitted", "context-delivered"
    ]

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        bound = manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=root
        )

    assert _warnings(caplog) == []
    applied = _applied(root, run)
    assert len(applied) == 1
    receipt = applied[0]
    assert receipt["note_id"] == "note-1"
    assert receipt["content_hash"] == NOTE_HASH
    assert receipt["attempt_id"] == job["job_id"] == receipt["job_id"]
    assert receipt["card"] == "implement" and receipt["task_kind"] == "build"
    assert receipt["mode"] == "inline"
    assert receipt["evidence"] == {"ref": ARTIFACT_REF, "sha256": ARTIFACT_SHA}
    # R5：applied 永遠不是 strict read，也不改寫 legacy 分母。
    assert receipt["counts_as_read"] is False
    # applied receipt 與 offer 綁同一份 candidate 摘要（不含 note 內容）。
    offer = next(row for row in before if row["event"] == "offer-emitted")
    for field in ("applicability_sha256", "relevance_reason_sha256", "source_time", "content_version"):
        assert receipt[field] == offer[field]
    assert NOTE_CONTENT not in json.dumps(_receipts(root, run))
    # 卡片採信結果與未帶欄位時逐位元組相同：欄位不進 canonical evidence。
    assert _evidence_bytes(bound, root) == baseline_evidence
    assert "task_memory_applied" not in _evidence_bytes(bound, root).decode()

    # 重跑 harvest 冪等：同一筆 receipt 不重複。
    manager._harvest_task_memory_applied(
        registry, job_id=job["job_id"], value=[_valid_entry()], coordinator_root=root
    )
    assert len(_applied(root, run)) == 1


def test_canonical_v2_build_terminal_also_carries_applied_field(tmp_path, monkeypatch):
    registry, run, job, root, _wt = _build_fixture(
        tmp_path, monkeypatch, applied=[_valid_entry()]
    )
    log = Path(job["log_path"])
    terminal = json.loads(log.read_text(encoding="utf-8"))
    terminal.update({"schema_version": 2, "diagnostics": {}, "gate_evidence": [
        {"name": "pytest", "status": "passed"}
    ]})
    log.write_text(json.dumps(terminal) + "\n", encoding="utf-8")

    manager.terminalize_workflow_job(registry, job_id=job["job_id"], coordinator_root=root)

    assert len(_applied(root, run)) == 1


# ---------------------------------------------------------------------------
# 負向：不產生 receipt、卡片結果不變、只有 class-only 診斷
# ---------------------------------------------------------------------------


_REJECTED_CASES = {
    "note-not-delivered": {"memory": "offered", "applied": [_valid_entry()]},
    "no-task-memory-for-attempt": {"memory": "none", "applied": [_valid_entry()]},
    "note-delivered-to-other-attempt": {"memory": "other-attempt", "applied": [_valid_entry()]},
    "note-outside-manifest": {"applied": [_valid_entry(note_id="note-9")]},
    "hash-mismatch": {"applied": [_valid_entry(evidence_sha256="0" * 64)]},
    "evidence-missing": {"applied": [_valid_entry(evidence_ref="src/missing.py")]},
    "parent-escape": {"applied": [_valid_entry(evidence_ref="../outside.py")]},
    "absolute-path": {"applied": [_valid_entry(evidence_ref="/etc/hosts")]},
    "git-metadata": {"applied": [_valid_entry(evidence_ref=".git/HEAD")]},
    "uppercase-sha": {"applied": [_valid_entry(evidence_sha256=ARTIFACT_SHA.upper())]},
    "short-sha": {"applied": [_valid_entry(evidence_sha256=ARTIFACT_SHA[:63])]},
    "extra-key": {"applied": [{**_valid_entry(), "summary": "used it"}]},
    "missing-key": {"applied": [{"note_id": "note-1", "evidence_ref": ARTIFACT_REF}]},
    "not-a-list": {"applied": _valid_entry()},
    "too-many": {"applied": [_valid_entry(note_id=f"note-{i}") for i in range(4)]},
    "duplicate-note": {"applied": [_valid_entry(), _valid_entry()]},
    "non-string-note": {"applied": [_valid_entry(note_id=1)]},
}


@pytest.mark.parametrize("case", sorted(_REJECTED_CASES))
def test_rejected_applied_entries_leave_card_result_unchanged(
    case, tmp_path, monkeypatch, caplog, baseline_evidence
):
    spec = _REJECTED_CASES[case]
    registry, run, job, root, _wt = _build_fixture(
        tmp_path / "case",
        monkeypatch,
        applied=spec["applied"],
        memory=spec.get("memory", "delivered"),
    )
    before = _receipts(root, run)

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        bound = manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=root
        )

    assert _applied(root, run) == []
    assert _receipts(root, run) == before
    assert _evidence_bytes(bound, root) == baseline_evidence
    assert registry.get_job(job["job_id"])["workflow_evidence"] == bound["workflow_evidence"]
    messages = _warnings(caplog)
    assert messages and all(_WARNING in message for message in messages)
    # class-only：不得把 note id、路徑或 hash 寫進診斷。
    for message in messages:
        assert "note-" not in message and "src/" not in message and ARTIFACT_SHA not in message


def test_evidence_outside_attempt_candidate_commit_is_rejected(
    tmp_path, monkeypatch, caplog, baseline_evidence
):
    """worktree 裡存在、hash 也對，但不在本 attempt candidate commit 內 → 拒絕。"""

    untracked = b"scratch file never committed\n"
    registry, run, job, root, worktree = _build_fixture(
        tmp_path / "case",
        monkeypatch,
        applied=[_valid_entry(
            evidence_ref="src/scratch.py",
            evidence_sha256=hashlib.sha256(untracked).hexdigest(),
        )],
    )
    (worktree / "src" / "scratch.py").write_bytes(untracked)

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        bound = manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=root
        )

    assert _applied(root, run) == []
    assert _evidence_bytes(bound, root) == baseline_evidence
    assert any(_WARNING in message for message in _warnings(caplog))


def test_dirty_worktree_content_differing_from_candidate_is_rejected(
    tmp_path, monkeypatch, caplog
):
    dirty = b"uncommitted edit\n"
    registry, run, job, root, worktree = _build_fixture(
        tmp_path,
        monkeypatch,
        applied=[_valid_entry(evidence_sha256=hashlib.sha256(dirty).hexdigest())],
    )
    (worktree / ARTIFACT_REF).write_bytes(dirty)

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        manager.terminalize_workflow_job(registry, job_id=job["job_id"], coordinator_root=root)

    assert _applied(root, run) == []
    assert any(_WARNING in message for message in _warnings(caplog))


def test_one_invalid_entry_does_not_block_another_valid_entry(tmp_path, monkeypatch, caplog):
    registry, run, job, root, _wt = _build_fixture(
        tmp_path,
        monkeypatch,
        applied=[_valid_entry(note_id="note-9"), _valid_entry()],
    )

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        manager.terminalize_workflow_job(registry, job_id=job["job_id"], coordinator_root=root)

    assert [row["note_id"] for row in _applied(root, run)] == ["note-1"]
    assert len(_warnings(caplog)) == 1


def test_empty_list_is_an_explicit_no_op(tmp_path, monkeypatch, caplog, baseline_evidence):
    registry, run, job, root, _wt = _build_fixture(tmp_path / "case", monkeypatch, applied=[])
    before = _receipts(root, run)

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        bound = manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=root
        )

    assert _receipts(root, run) == before
    assert _warnings(caplog) == []
    assert _evidence_bytes(bound, root) == baseline_evidence


# ---------------------------------------------------------------------------
# 回歸：未帶欄位 → 與現況相同
# ---------------------------------------------------------------------------


def test_terminal_without_field_is_unchanged(tmp_path, monkeypatch, caplog, baseline_evidence):
    registry, run, job, root, _wt = _build_fixture(tmp_path / "case", monkeypatch)
    before = _receipts(root, run)

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        bound = manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=root
        )

    assert _receipts(root, run) == before
    assert _warnings(caplog) == []
    assert _evidence_bytes(bound, root) == baseline_evidence


def test_extract_terminal_json_is_identity_without_field_and_strips_it_otherwise(tmp_path):
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
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    assert manager._extract_terminal_json(str(log)) == payload
    raw, applied = manager._extract_terminal_payload(str(log))
    assert raw == payload and applied is manager._TERMINAL_FIELD_ABSENT

    log.write_text(
        json.dumps({**payload, "task_memory_applied": [_valid_entry()]}) + "\n", encoding="utf-8"
    )
    assert manager._extract_terminal_json(str(log)) == payload
    raw, applied = manager._extract_terminal_payload(str(log))
    assert raw == payload and applied == [_valid_entry()]


@pytest.mark.parametrize("applied", ([_valid_entry()], "malformed", None))
def test_pre_terminalize_classifiers_ignore_the_optional_field(tmp_path, applied):
    """malformed／明示停止判定看不到這個欄位：帶或不帶、合法或畸形，分類都一樣。"""

    card = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    stop = {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "failed",
        "summary": "pytest failed",
        "details": {"pytest": "failed"},
        "reports": [{"path": REPORT_REF, "body": "# Verification\n\nFailed.\n"}],
    }

    def job_for(payload: dict, phase: str) -> dict:
        log = tmp_path / f"{phase}-{len(list(tmp_path.iterdir()))}.jsonl"
        log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return {
            "status": "exited",
            "exit_code": 0,
            "workflow_evidence": None,
            "workflow_phase": phase,
            "workflow_run_id": "run",
            "workflow_card": "card",
            "log_path": str(log),
        }

    for payload, phase in ((card, "build"), (stop, "verify")):
        plain = job_for(payload, phase)
        carried = job_for({**payload, "task_memory_applied": applied}, phase)
        assert manager._malformed_workflow_card_terminal(carried) is (
            manager._malformed_workflow_card_terminal(plain)
        )
        assert manager._explicit_stop_gate_terminal(carried) == (
            manager._explicit_stop_gate_terminal(plain)
        )
    assert manager._malformed_workflow_card_terminal(job_for(card, "build")) is False
    assert manager._explicit_stop_gate_terminal(job_for(stop, "verify")) == stop


# ---------------------------------------------------------------------------
# reviewer：evidence 以審查對象（workflow_repo_root）驗證
# ---------------------------------------------------------------------------


def _verify_fixture(tmp_path: Path, monkeypatch, *, applied: object):
    review_target = tmp_path / "review-target"
    (review_target / "src").mkdir(parents=True)
    (review_target / ARTIFACT_REF).write_bytes(ARTIFACT)
    sandbox = tmp_path / "reviewer-sandbox"
    (sandbox / "src").mkdir(parents=True)
    (sandbox / "src" / "sandbox_only.py").write_bytes(ARTIFACT)
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    step = WorkflowStep(
        phase="verify",
        persona="reviewer",
        card="verification",
        executor="agy",
        model="gemini-3.1-pro-high",
        domain="google",
        inputs=(),
        outputs=(REPORT_REF,),
    )
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key=CLAIM_KEY,
        source_revision="6" * 64,
        workspace_root=str(review_target),
        combo="feature-oneshot",
        current_phase="verify",
        steps=(step,),
        attempts={},
        candidate_head="a" * 40,
        gate_status="running",
    )
    job = registry.create_job(
        task="verification",
        persona="reviewer",
        kind="review",
        branch="feature/task-memory-applied",
        pane="",
        worktree=str(sandbox),
        executor="agy",
        model_id="gemini-3.1-pro-high",
        independence_domain="google",
        subject_head="a" * 40,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=step.card,
        workflow_phase="verify",
        workflow_repo_root=str(review_target),
        workflow_outputs=(REPORT_REF,),
        source_revision=run.source_revision,
    )
    coordinator_root = tmp_path / "coordinator"
    _prepare_dispatch_memory(monkeypatch, registry, run, job, coordinator_root)
    terminal = {
        "schema_version": 1,
        "kind": "workflow-verification-result",
        "status": "verified",
        "summary": "verification passed",
        "details": {"pytest": "passed"},
        "reports": [{"path": REPORT_REF, "body": "# Verification\n\nPassed.\n"}],
        "task_memory_applied": applied,
    }
    log = tmp_path / "verification.jsonl"
    log.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
    registry.attach_launch_handle(
        job["job_id"], executor="agy", model_id="gemini-3.1-pro-high", log_path=str(log)
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    return registry, run, registry.get_job(job["job_id"]), coordinator_root


def test_verification_terminal_applied_entry_is_verified_in_review_target(
    tmp_path, monkeypatch, caplog
):
    registry, run, job, root = _verify_fixture(tmp_path, monkeypatch, applied=[_valid_entry()])

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        bound = manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=root
        )

    assert bound["workflow_evidence"] is not None
    assert _warnings(caplog) == []
    applied = _applied(root, run)
    assert len(applied) == 1
    assert applied[0]["card"] == "verification" and applied[0]["task_kind"] == "verify"
    assert applied[0]["evidence"] == {"ref": ARTIFACT_REF, "sha256": ARTIFACT_SHA}


def test_reviewer_evidence_only_in_disposable_sandbox_is_rejected(
    tmp_path, monkeypatch, caplog
):
    registry, run, job, root = _verify_fixture(
        tmp_path,
        monkeypatch,
        applied=[_valid_entry(evidence_ref="src/sandbox_only.py")],
    )

    with caplog.at_level(logging.WARNING, logger=manager.__name__):
        bound = manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=root
        )

    assert bound["workflow_evidence"] is not None
    assert _applied(root, run) == []
    assert any(_WARNING in message for message in _warnings(caplog))


# ---------------------------------------------------------------------------
# schema：reviewer 工具 schema 開放選填欄位，仍在 Gemini 相容子集內
# ---------------------------------------------------------------------------


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


@pytest.mark.parametrize("kind", ("workflow-verification-result", "workflow-review-result"))
def test_reviewer_schemas_expose_optional_task_memory_applied(kind):
    claude = json.loads(launcher_module._claude_review_json_schema(kind))
    assert claude["properties"]["task_memory_applied"] == task_memory.task_memory_applied_json_schema()
    assert "task_memory_applied" not in claude["required"]
    field = claude["properties"]["task_memory_applied"]
    assert field["maxItems"] == 3
    assert field["items"]["additionalProperties"] is False
    assert set(field["items"]["required"]) == {"note_id", "evidence_ref", "evidence_sha256"}

    gemini = json.loads(launcher_module._gemini_review_json_schema(kind))
    _assert_gemini_schema_subset(gemini)
    assert gemini["properties"]["task_memory_applied"]["type"] == "array"
    assert gemini["properties"]["task_memory_applied"]["items"]["properties"]["evidence_sha256"] == {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
    }


# ---------------------------------------------------------------------------
# 嚴格欄位驗證（純函式）
# ---------------------------------------------------------------------------


def test_parse_task_memory_applied_normalizes_valid_entries():
    assert parse_task_memory_applied([]) == ()
    assert parse_task_memory_applied([_valid_entry()]) == (
        {"note_id": "note-1", "evidence_ref": ARTIFACT_REF, "evidence_sha256": ARTIFACT_SHA},
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "note-1",
        {"note_id": "note-1"},
        [_valid_entry(note_id=f"note-{i}") for i in range(4)],
        [_valid_entry(), _valid_entry()],
        [_valid_entry(evidence_ref="")],
        [_valid_entry(evidence_ref=".")],
        [_valid_entry(evidence_ref="src/./parser.py")],
        [_valid_entry(evidence_ref="src\\parser.py")],
        [_valid_entry(evidence_ref="src/parser.py\n")],
        [_valid_entry(evidence_ref="a" * 1025)],
        [_valid_entry(evidence_ref="vendor/.git/config")],
        [_valid_entry(note_id="")],
        [_valid_entry(note_id="bad note")],
        [_valid_entry(evidence_sha256=None)],
        [["note-1", ARTIFACT_REF, ARTIFACT_SHA]],
    ],
)
def test_parse_task_memory_applied_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        parse_task_memory_applied(value)


# ---------------------------------------------------------------------------
# prompt：只有 build／verify／review 卡、且真的交付了 note 才告知回報方式
# ---------------------------------------------------------------------------


def test_task_memory_prompt_block_explains_applied_reporting(tmp_path, monkeypatch):
    registry, run, job, root, _wt = _build_fixture(tmp_path, monkeypatch, memory="none")
    prepared = _prepare_dispatch_memory(monkeypatch, registry, run, job, root, deliver=False)

    prompt = manager._append_task_memory_inline("PROMPT", prepared)

    assert prompt.startswith("PROMPT\n\nOptional task-scoped Hippo memory")
    assert "[note-1] Apply the shared parser at the caller boundary." in prompt
    assert "task_memory_applied" in prompt
    assert "evidence_ref" in prompt and "evidence_sha256" in prompt
    assert "Omit the field" in prompt


def test_task_memory_prompt_block_is_unchanged_without_offer_or_for_plan_cards(
    tmp_path, monkeypatch
):
    registry, run, job, root, _wt = _build_fixture(tmp_path, monkeypatch, memory="none")
    prepared = _prepare_dispatch_memory(monkeypatch, registry, run, job, root, deliver=False)

    not_offered = task_memory.PreparedTaskMemory(
        context=prepared.context, status="read-failed", mode="inline"
    )
    assert manager._append_task_memory_inline("PROMPT", not_offered) == "PROMPT"

    plan_context = task_memory.TaskMemoryContext(
        **{
            **{name: getattr(prepared.context, name) for name in prepared.context.__dataclass_fields__},
            "task_kind": "plan",
        }
    )
    plan_prepared = task_memory.PreparedTaskMemory(
        context=plan_context,
        status="offered",
        mode="inline",
        inline_context=prepared.inline_context,
    )
    plan_prompt = manager._append_task_memory_inline("PROMPT", plan_prepared)
    assert "[note-1]" in plan_prompt
    assert "task_memory_applied" not in plan_prompt


# ---------------------------------------------------------------------------
# #857 R5：legacy strict KPI 分母不受影響
# ---------------------------------------------------------------------------


def test_applied_receipt_does_not_change_legacy_strict_kpi_or_retrieval_denominators(
    tmp_path, monkeypatch
):
    registry, run, job, root, _wt = _build_fixture(
        tmp_path, monkeypatch, applied=[_valid_entry()]
    )
    before = summarize_canary(_receipts(root, run), minimum_successes=1)

    manager.terminalize_workflow_job(registry, job_id=job["job_id"], coordinator_root=root)

    events = _receipts(root, run)
    assert [row["event"] for row in events][-1] == "applied-with-evidence"
    after = summarize_canary(events, minimum_successes=1)
    assert after["paths"] == before["paths"]
    assert after["content_retrieval"] == before["content_retrieval"]
    assert after["inline_delivery"] == before["inline_delivery"]
    assert after["legacy_strict_kpi_mutated"] is False
    assert after["blockers"] == []
    assert all(row["counts_as_read"] is False for row in events)
