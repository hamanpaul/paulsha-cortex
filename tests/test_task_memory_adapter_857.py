"""Issue #857：contract consumer、delivery isolation、receipt 與 read model 測試。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from paulsha_cortex.coordinator.task_memory import (
    TaskMemoryAdapter,
    TaskMemoryCapabilities,
    TaskMemoryReceiptStore,
    project_task_memory_read_model,
    summarize_canary,
    task_memory_context_from_cortex,
)
from paulsha_cortex.coordinator.workflow import (
    GateEvidenceRef,
    PlanningArtifactAuthority,
    WorkflowRun,
    WorkflowStep,
)
from paulsha_cortex.monitor.work_models import WorkItem
from paulsha_cortex import cli as umbrella_cli


NOTE_CONTENT = "Use the shared parser and keep the read boundary scoped.\n"
NOTE_HASH = hashlib.sha256(NOTE_CONTENT.encode()).hexdigest()


def _run(*, repo: str = "acme/demo", work_id: str = "demo", phase: str = "build"):
    card = f"{phase}-card"
    step = WorkflowStep(
        phase=phase,
        persona="builder" if phase == "build" else "reviewer",
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result="pending",
        test_policy="focused" if phase == "build" else None,
    )
    run = WorkflowRun(
        run_id="workflow-" + hashlib.sha256(f"{repo}:{work_id}".encode()).hexdigest()[:20],
        work_id=work_id,
        repo=repo,
        claim_key="claim:v1:" + "c" * 64,
        source_revision="issue:857@open",
        workspace_root="/fixture/worktree",
        combo="feature-oneshot",
        current_phase=phase,
        steps=(step,),
        issue_refs=(f"{repo}#857",),
        openspec_refs=("task-memory-delivery-adapter",),
        pr_refs=(),
        attempts={card: 1},
        evidence_refs=("tests/test_task_memory_adapter_857.py",),
        gate_refs=(GateEvidenceRef(kind="maintainer-review", ref="review:fixture"),),
        brainstorm_required=False,
        primary_domain=None,
        candidate_head="a" * 40,
        verified_head=None,
        facets=(),
        gate_status="pending",
        created_at="2026-09-26T00:00:00+00:00",
        updated_at="2026-09-26T00:01:00+00:00",
        planning_authority=(
            PlanningArtifactAuthority(
                ref="docs/superpowers/specs/task-memory-delivery-adapter-spec.md",
                kind="spec",
                work_id=work_id,
                baseline_sha256="2" * 64,
            ),
            PlanningArtifactAuthority(
                ref="docs/superpowers/plans/task-memory-delivery-adapter.md",
                kind="plan",
                work_id=work_id,
                baseline_sha256="3" * 64,
            ),
        ),
        planning_source_revision="planning:" + "4" * 64,
        resolved_model_chain={
            "builder": {
                "executor": "fixture-cli",
                "model_id": "fixture-model",
                "independence_domain": "fixture-domain",
                "source": "evaluated-roster",
            }
        },
    )
    work_item = SimpleNamespace(
        repo=repo,
        work_id=work_id,
        title="Keep the task memory fixture scoped",
        state="ongoing",
        phase=phase,
        workflow_run_id=run.run_id,
        updated_at="2026-09-26T00:01:00+00:00",
    )
    job = {
        "job_id": "job-" + hashlib.sha256(f"{repo}:{work_id}:{phase}".encode()).hexdigest()[:20],
        "workflow_run_id": run.run_id,
        "workflow_card": card,
        "workflow_phase": phase,
        "executor": "fixture-cli",
        "model_id": "fixture-model",
        "status": "running",
        "session_name": "job-session-proxy",
    }
    return work_item, run, step, job


def _context(*, repo="acme/demo", work_id="demo", phase="build", caps=None):
    work_item, run, step, job = _run(repo=repo, work_id=work_id, phase=phase)
    context = task_memory_context_from_cortex(
        work_item=work_item,
        run=run,
        step=step,
        job=job,
        capabilities=caps
        or TaskMemoryCapabilities(inline=True, snapshot=True, note_fetch=True),
        goal="Apply the accepted task-local context to the current work item.",
        related_files=("src/parser.py",),
        related_errors=("ValueError: malformed envelope",),
        allowed_evidence_sources=("work-item", "workflow-run", "planning-authority"),
    )
    return context, work_item, run, step, job


def _payload(request: dict, *, mode: str | None = None, project: str | None = None):
    selected_mode = mode or request["delivery"]["mode"]
    task_id = request["task_id"]
    project = project or request["project"]
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
        "host_optional": {"retained": True},
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
                **({"content": NOTE_CONTENT} if selected_mode == "snapshot" else {}),
            }
        ],
    }
    canonical_manifest = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    manifest["sha256"] = hashlib.sha256(canonical_manifest).hexdigest()
    return {
        "schema_version": "1",
        "task_id": task_id,
        "intent": request["intent"],
        "project": project,
        "candidates": [candidate],
        "delivery": {
            "mode": selected_mode,
            "capabilities": dict(request["delivery"]["capabilities"]),
            "manifest": manifest,
        },
        "evidence": [],
        "producer": {"id": "hippo-core", "version": "fixture"},
        "adapter": {"id": "host-adapter", "version": "fixture"},
        "future_optional": {"preserved": True},
    }


def _provider_for(mode: str | None = None, project: str | None = None):
    return lambda request: _payload(request, mode=mode, project=project)


def test_accepted_planning_set_is_registered_once_and_pins_hippo_dependency():
    paths = (
        Path("docs/superpowers/specs/task-memory-delivery-adapter-spec.md"),
        Path("docs/superpowers/specs/task-memory-delivery-adapter-design.md"),
        Path("docs/superpowers/plans/task-memory-delivery-adapter.md"),
        Path("docs/superpowers/workstreams/task-memory-delivery-adapter/todo.md"),
    )
    work_ids = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1]
        metadata = yaml.safe_load(frontmatter)
        assert metadata["status"] == "accepted"
        work_ids.add(metadata["work_item"])
        assert "/tmp/" not in text
        assert "Hippo #146" in text
    assert work_ids == {"task-memory-delivery-adapter"}

    registration = yaml.safe_load(Path(".cortex/work-items.yaml").read_text())
    matches = [
        (work_id, item)
        for work_id, item in registration["work_items"].items()
        if any(
            source.get("ref") == "hamanpaul/paulsha-cortex#857"
            for source in item.get("links", [])
        )
    ]
    assert len(matches) == 1
    assert matches[0][0] == "task-memory-delivery-adapter"
    registered_refs = {item.get("ref") for item in matches[0][1]["links"]}
    assert {path.as_posix() for path in paths} <= registered_refs


def test_task_identity_comes_from_work_item_run_card_and_job_not_display_names():
    context, work_item, run, step, job = _context()
    assert context.task_id.startswith("task-")
    assert len(context.task_id) == len("task-") + 40
    assert context.repo == run.repo == work_item.repo
    assert context.work_id == run.work_id == work_item.work_id
    assert context.workflow_run_id == run.run_id
    assert context.card == step.card == job["workflow_card"]
    assert context.attempt_id == job["job_id"]
    assert context.session_id is None
    assert context.session_proxy == f"job:{job['job_id']}"
    assert context.project == "acme/demo"
    assert context.task_kind == "build"
    envelope = context.to_envelope(mode="note_fetch")
    assert "raw_prompt" not in envelope
    assert envelope["task_id"] == context.task_id


def test_context_rejects_mismatched_run_project_and_card_identity():
    context, work_item, run, step, job = _context()
    mismatched_work = SimpleNamespace(**{**work_item.__dict__, "repo": "other/repo"})
    with pytest.raises(ValueError, match="work item identity mismatch"):
        task_memory_context_from_cortex(
            work_item=mismatched_work,
            run=run,
            step=step,
            job=job,
            capabilities=context.capabilities,
            goal="bounded goal",
        )
    mismatched_job = {**job, "workflow_run_id": "workflow-" + "9" * 20}
    with pytest.raises(ValueError, match="job identity mismatch"):
        task_memory_context_from_cortex(
            work_item=work_item,
            run=run,
            step=step,
            job=mismatched_job,
            capabilities=context.capabilities,
            goal="bounded goal",
        )


def test_context_redacts_private_text_and_rejects_wide_read_scope():
    work_item, run, step, job = _run()
    context = task_memory_context_from_cortex(
        work_item=work_item,
        run=run,
        step=step,
        job=job,
        capabilities=TaskMemoryCapabilities(inline=True),
        goal="Use token=fixture-secret with source /outside/private.md",
        related_errors=("password=fixture-password in /outside/error.log",),
        allowed_evidence_sources=("workflow-run",),
    )
    envelope = json.dumps(context.to_envelope(mode="inline"))
    assert "fixture-secret" not in envelope
    assert "fixture-password" not in envelope
    assert "/outside/" not in envelope

    with pytest.raises(ValueError, match="read scope invalid"):
        replace(context, read_scope={**context.read_scope, "path": "/outside"})


def test_missing_provider_and_unsupported_schema_park_without_lifecycle_mutation():
    context, _work, run, _step, _job = _context()
    before = run.to_dict()
    unavailable = TaskMemoryAdapter(provider=None).prepare(context)
    assert unavailable.status == "read-failed"
    assert unavailable.reason == "provider-unavailable"
    assert unavailable.events[0]["event"] == "read-failed"

    def unsupported(request):
        payload = _payload(request)
        payload["schema_version"] = "2"
        return payload

    parked = TaskMemoryAdapter(provider=unsupported).prepare(context)
    assert parked.status == "parked"
    assert parked.reason == "unsupported-schema-major"
    assert parked.events[0]["reason"] == "unsupported-schema-major"
    assert run.to_dict() == before


def test_contract_capability_mismatch_fails_closed():
    context, *_ = _context(caps=TaskMemoryCapabilities(note_fetch=True))

    def wrong_capability(request):
        payload = _payload(request)
        payload["delivery"]["capabilities"]["note_fetch"] = False
        return payload

    result = TaskMemoryAdapter(
        provider=wrong_capability,
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    ).prepare(context)
    assert result.status == "read-failed"
    assert result.reason == "mode-mismatch"
    assert result.events[0]["reason"] == "mode-mismatch"
    assert result.events[0]["eligible_authorized"] is False


def test_core_hippo_payload_without_adapter_manifest_extension_is_parked():
    context, *_ = _context(caps=TaskMemoryCapabilities(inline=True))

    def core_contract_only(request):
        return {
            "schema_version": "1",
            "task_id": request["task_id"],
            "intent": request["intent"],
            "candidates": [
                {
                    "ref": "candidate-1",
                    "rank": 1,
                    "summary": "Core Hippo public candidate fixture.",
                    "authorization": {"status": "authorized"},
                    "availability": {"status": "available"},
                }
            ],
            "delivery": {
                "mode": "inline",
                "capabilities": {"inline": True},
            },
            "evidence": [],
            "producer": {"id": "hippo-core"},
            "adapter": {"id": "hippo-task-memory-payload"},
        }

    result = TaskMemoryAdapter(provider=core_contract_only).prepare(context)
    assert result.status == "read-failed"
    assert result.reason == "manifest-mismatch"
    assert result.events[0]["eligible_authorized"] is False


@pytest.mark.parametrize("mode", ["inline", "snapshot", "note_fetch"])
def test_delivery_paths_emit_bound_receipts_and_keep_inline_out_of_read(tmp_path, mode):
    context, *_ = _context(
        caps=TaskMemoryCapabilities(
            inline=mode == "inline",
            snapshot=mode == "snapshot",
            note_fetch=mode == "note_fetch",
        )
    )
    adapter = TaskMemoryAdapter(
        provider=_provider_for(mode),
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    )
    prepared = adapter.prepare(context, snapshot_root=tmp_path / "snapshots")
    assert prepared.status == "offered"
    assert prepared.mode == mode
    assert [item["event"] for item in prepared.events[:2]] == [
        "candidate-selected",
        "offer-emitted",
    ]
    if mode == "snapshot":
        assert prepared.events[-1]["event"] == "snapshot-ready"
    assert prepared.events[0]["task_id"] == context.task_id
    assert prepared.events[0]["attempt_id"] == context.attempt_id
    assert prepared.events[0]["note_id"] == "note-1"
    assert prepared.events[0]["content_hash"] == NOTE_HASH
    assert prepared.events[0]["content_version"] == "v7"
    # MAJOR 修復（issue #857 對抗審查）：receipt 只保留自由文字的 SHA-256 摘要，
    # 不持久化 applicability／relevance_reason 原文；source_time 可解析為
    # ISO8601 時原樣保留。
    assert "applicability" not in prepared.events[0]
    assert "relevance_reason" not in prepared.events[0]
    assert prepared.events[0]["applicability_sha256"] == hashlib.sha256(
        json.dumps(["same repository", "parser task"], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert prepared.events[0]["relevance_reason_sha256"] == hashlib.sha256(
        "The current task changes parsing behavior.".encode()
    ).hexdigest()
    assert prepared.events[0]["source_time"] == "2026-09-25T12:00:00Z"
    assert prepared.payload["future_optional"] == {"preserved": True}

    if mode == "inline":
        delivered = adapter.confirm_context_delivered(prepared)
        assert delivered[0]["event"] == "context-delivered"
        assert delivered[0]["counts_as_read"] is False
        assert all(item["event"] != "content-returned" for item in prepared.events)
    elif mode == "snapshot":
        assert prepared.snapshot_id
        fetched = adapter.open_snapshot(prepared, prepared.snapshot_id, "note-1")
        assert fetched.content == NOTE_CONTENT
        assert fetched.events[-1]["event"] == "content-returned"
        assert fetched.events[-1]["counts_as_read"] is False
    else:
        fetched = adapter.fetch_note(prepared, "note-1")
        assert fetched.content == NOTE_CONTENT
        assert fetched.events[0]["event"] == "read-attempted"
        assert fetched.events[-1]["event"] == "content-returned"
        assert fetched.events[-1]["counts_as_read"] is True


def test_note_fetch_is_manifest_bound_and_never_accepts_a_host_path():
    context, *_ = _context()
    adapter = TaskMemoryAdapter(
        provider=_provider_for("note_fetch"),
        note_fetch=lambda _task_id, note_id: NOTE_CONTENT if note_id == "note-1" else "secret",
    )
    prepared = adapter.prepare(context)
    with pytest.raises(ValueError, match="note is outside task manifest"):
        adapter.fetch_note(prepared, "../private/note.md")
    with pytest.raises(ValueError, match="note is outside task manifest"):
        adapter.fetch_note(prepared, "/outside/private.md")


def test_scope_manifest_and_hash_mismatches_fail_closed_without_returning_content():
    context, *_ = _context()
    cross_project = TaskMemoryAdapter(
        provider=_provider_for("note_fetch", project="other/repo"),
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    ).prepare(context)
    assert cross_project.status == "read-failed"
    assert cross_project.reason == "scope-mismatch"
    assert all("content" not in item for item in cross_project.events)

    def wrong_hash(request):
        payload = _payload(request)
        payload["candidates"][0]["content_hash"] = "0" * 64
        return payload

    mismatch = TaskMemoryAdapter(
        provider=wrong_hash,
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    ).prepare(context)
    assert mismatch.status == "read-failed"
    assert mismatch.reason == "manifest-mismatch"
    assert not any(item["event"] == "content-returned" for item in mismatch.events)


def test_candidate_bound_is_three_and_unknown_optional_contract_fields_survive():
    context, *_ = _context(
        caps=TaskMemoryCapabilities(inline=False, snapshot=False, note_fetch=True)
    )
    request = context.to_envelope(mode="note_fetch")
    payload = _payload(request)
    first = payload["candidates"][0]
    entries = payload["delivery"]["manifest"]["entries"]
    for index in (2, 3):
        candidate = {**first, "ref": f"candidate-{index}", "note_id": f"note-{index}"}
        candidate["host_optional"] = {"preserved_index": index}
        payload["candidates"].append(candidate)
        entries.append(
            {
                "note_id": f"note-{index}",
                "content_hash": NOTE_HASH,
                "content_version": "v7",
                "project": context.project,
            }
        )
    unsigned = dict(payload["delivery"]["manifest"])
    unsigned.pop("sha256")
    payload["delivery"]["manifest"]["sha256"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    prepared = TaskMemoryAdapter(
        provider=lambda _request: payload,
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    ).prepare(context)
    assert prepared.status == "offered"
    assert len(prepared.candidates) == 3
    assert prepared.payload["candidates"][2]["host_optional"] == {"preserved_index": 3}


def test_hostile_provider_free_text_never_reaches_receipt_sidecar_or_read_model(tmp_path):
    """issue #857 對抗審查 MAJOR：有缺陷／惡意的 hippo provider 若把 note 正文
    或 provider 內部診斷訊息塞進 applicability／relevance_reason／source_time，
    receipt／sidecar／read model 都不得含有那段原文，只能含其 SHA-256 摘要，
    且格式不對的 source_time 必須落 "unknown"。
    """

    hostile_note_body = "SECRET NOTE BODY: do not exfiltrate this stderr dump"
    hostile_reason = "traceback: internal provider stderr leaked here " + ("x" * 700)

    def hostile_provider(request):
        payload = _payload(request)
        payload["candidates"][0]["applicability"] = [hostile_note_body]
        payload["candidates"][0]["relevance_reason"] = hostile_reason
        payload["candidates"][0]["source_time"] = "not-a-real-timestamp"
        return payload

    context, work_item, run, _step, job = _context(caps=TaskMemoryCapabilities(inline=True))
    adapter = TaskMemoryAdapter(provider=hostile_provider)
    prepared = adapter.prepare(context)
    assert prepared.status == "offered"
    event = prepared.events[0]
    assert "applicability" not in event
    assert "relevance_reason" not in event
    assert hostile_note_body not in json.dumps(event)
    assert hostile_reason not in json.dumps(event)
    assert event["source_time"] == "unknown"
    assert len(event["applicability_sha256"]) == 64
    assert len(event["relevance_reason_sha256"]) == 64

    store = TaskMemoryReceiptStore(tmp_path / "coordinator")
    store.append(event)
    sidecar_text = store.sidecar_path(
        context.repo, context.work_id, context.workflow_run_id
    ).read_text(encoding="utf-8")
    assert hostile_note_body not in sidecar_text
    assert hostile_reason not in sidecar_text
    assert "not-a-real-timestamp" not in sidecar_text

    view = project_task_memory_read_model(
        work_item=work_item,
        runs=(run,),
        jobs=(job,),
        receipts=store,
    )
    rendered = json.dumps(view)
    assert hostile_note_body not in rendered
    assert hostile_reason not in rendered
    assert "not-a-real-timestamp" not in rendered


def test_denied_candidate_is_rejected_without_copying_denied_summary_to_receipt():
    context, *_ = _context(
        caps=TaskMemoryCapabilities(inline=True, snapshot=False, note_fetch=False)
    )

    def denied_candidate(request):
        payload = _payload(request)
        payload["candidates"][0]["authorization"] = {
            "status": "denied",
            "reason": "permission_denied",
        }
        payload["candidates"][0]["summary"] = "private denied body fixture"
        return payload

    result = TaskMemoryAdapter(provider=denied_candidate).prepare(context)
    assert result.status == "read-failed"
    assert result.reason == "candidate-authorization-invalid"
    assert "private denied body fixture" not in json.dumps(result.events)


def test_snapshot_is_sealed_manifest_bound_and_symlink_replacement_fails_closed(tmp_path):
    context, *_ = _context(
        caps=TaskMemoryCapabilities(inline=False, snapshot=True, note_fetch=False)
    )
    adapter = TaskMemoryAdapter(provider=_provider_for("snapshot"))
    prepared = adapter.prepare(context, snapshot_root=tmp_path / "snapshots")
    assert prepared.status == "offered"
    assert prepared.snapshot_path.stat().st_mode & 0o222 == 0
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}", encoding="utf-8")
    prepared.snapshot_path.unlink()
    prepared.snapshot_path.symlink_to(foreign)
    result = adapter.open_snapshot(prepared, prepared.snapshot_id, "note-1")
    assert result.content is None
    assert result.reason == "snapshot-unavailable"
    assert result.events[-1]["event"] == "read-failed"


def test_snapshot_permission_denial_stays_unknown_and_bounded(tmp_path, monkeypatch):
    from paulsha_cortex.coordinator import task_memory

    context, *_ = _context(caps=TaskMemoryCapabilities(snapshot=True))
    adapter = TaskMemoryAdapter(provider=_provider_for("snapshot"))
    prepared = adapter.prepare(context, snapshot_root=tmp_path / "snapshots")
    assert prepared.snapshot_path is not None
    real_open = task_memory.os.open

    def denied(path, *args, **kwargs):
        if Path(path) == prepared.snapshot_path:
            raise PermissionError("fixture permission boundary")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(task_memory.os, "open", denied)
    fetched = adapter.open_snapshot(prepared, prepared.snapshot_id, "note-1")
    assert fetched.content is None
    assert fetched.reason == "permission-denied"
    assert fetched.events[-1]["permission_layer"] == "unknown"
    assert "fixture permission boundary" not in json.dumps(fetched.events)


@pytest.mark.parametrize("mode", ["inline", "snapshot", "note_fetch"])
def test_permission_denial_is_bounded_and_does_not_claim_a_permission_layer(tmp_path, mode):
    context, *_ = _context(
        caps=TaskMemoryCapabilities(
            inline=mode == "inline",
            snapshot=mode == "snapshot",
            note_fetch=mode == "note_fetch",
        )
    )

    def denied(_request):
        raise PermissionError("private exception detail must not be persisted")

    result = TaskMemoryAdapter(
        provider=denied,
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    ).prepare(context, snapshot_root=tmp_path / "snapshots")
    assert result.status == "read-failed"
    assert result.reason == "permission-denied"
    assert result.events[0]["reason"] == "permission-denied"
    assert result.events[0]["permission_layer"] == "unknown"
    assert "private exception detail" not in json.dumps(result.events)


@pytest.mark.parametrize("mode", ["inline", "snapshot", "note_fetch"])
def test_each_canary_path_has_five_positive_and_permission_negative_controls(tmp_path, mode):
    caps = TaskMemoryCapabilities(
        inline=mode == "inline",
        snapshot=mode == "snapshot",
        note_fetch=mode == "note_fetch",
    )
    positive_events = []
    for index in range(5):
        repo = "acme/service-a" if index % 2 == 0 else "example/tool-b"
        phase = "build" if index % 2 == 0 else "verify"
        context, *_ = _context(
            repo=repo,
            work_id=f"{mode}-positive-{index}",
            phase=phase,
            caps=caps,
        )
        adapter = TaskMemoryAdapter(
            provider=_provider_for(mode),
            note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
        )
        prepared = adapter.prepare(context, snapshot_root=tmp_path / "snapshots")
        positive_events.extend(prepared.events)
        if mode == "inline":
            positive_events.extend(adapter.confirm_context_delivered(prepared))
        elif mode == "snapshot":
            positive_events.extend(adapter.open_snapshot(prepared, prepared.snapshot_id, "note-1").events)
        else:
            positive_events.extend(adapter.fetch_note(prepared, "note-1").events)

    positive_report = summarize_canary(positive_events, minimum_successes=5, minimum_rate=0.95)
    positive_row = positive_report["paths"][mode]
    assert positive_row["successes"] == 5
    assert positive_row["authorized_attempts"] == 5
    assert positive_row["success_rate"] == 1.0
    assert positive_row["passed"] is True
    assert positive_report["legacy_strict_kpi_mutated"] is False

    denied_events = []
    for index in range(5):
        repo = "acme/service-a" if index % 2 == 0 else "example/tool-b"
        phase = "build" if index % 2 == 0 else "verify"
        context, *_ = _context(
            repo=repo,
            work_id=f"{mode}-denied-{index}",
            phase=phase,
            caps=caps,
        )
        adapter = TaskMemoryAdapter(
            provider=_provider_for(mode),
            note_fetch=(
                (lambda _task_id, _note_id: (_ for _ in ()).throw(PermissionError("denied")))
                if mode == "note_fetch"
                else lambda _task_id, _note_id: NOTE_CONTENT
            ),
        )
        prepared = adapter.prepare(context, snapshot_root=tmp_path / "snapshots")
        denied_events.extend(prepared.events)
        if mode == "inline":
            denied_events.extend(adapter.confirm_context_delivered(prepared, delivered=False))
        elif mode == "snapshot":
            assert prepared.snapshot_path is not None
            prepared.snapshot_path.unlink()
            denied_events.extend(
                adapter.open_snapshot(prepared, prepared.snapshot_id, "note-1").events
            )
        else:
            denied_events.extend(adapter.fetch_note(prepared, "note-1").events)

    denied_report = summarize_canary(denied_events, minimum_successes=5, minimum_rate=0.95)
    denied_row = denied_report["paths"][mode]
    assert denied_row["successes"] == 0
    assert denied_row["authorized_attempts"] == 5
    assert denied_row["success_rate"] == 0.0
    assert denied_row["passed"] is False
    if mode != "snapshot":
        assert denied_row["permission_denials"] == 5
    assert denied_report["legacy_strict_kpi_mutated"] is False


def test_inline_context_delivery_does_not_pass_content_retrieval_gate():
    context, *_ = _context(caps=TaskMemoryCapabilities(inline=True))
    adapter = TaskMemoryAdapter(provider=_provider_for("inline"))
    prepared = adapter.prepare(context)
    events = (*prepared.events, *adapter.confirm_context_delivered(prepared))

    report = summarize_canary(events, minimum_successes=1, minimum_rate=0.95)

    assert report["content_retrieval"]["eligible_authorized_attempts"] == 0
    assert report["content_retrieval"]["successes"] == 0
    assert report["content_retrieval"]["success_rate"] is None
    assert report["content_retrieval"]["passed"] is False
    assert report["passed"] is False
    assert report["paths"]["inline"]["successes"] == 1
    assert report["paths"]["inline"]["passed"] is True
    assert report["paths"]["inline"]["metric_kind"] == "delivery"
    assert report["paths"]["inline"]["success_event"] == "context-delivered"
    assert report["paths"]["inline"]["counts_as_read"] is False


def test_canary_scope_relay_and_legacy_schema_observations_are_blockers():
    context, *_ = _context()
    prepared = TaskMemoryAdapter(provider=_provider_for("inline")).prepare(context)
    delivered = TaskMemoryAdapter(provider=_provider_for("inline")).confirm_context_delivered(prepared)
    report = summarize_canary(
        (*prepared.events, *delivered),
        minimum_successes=1,
        observed_blockers=("scope-leak", "cross-project-misattribution", "relay-overwrite"),
        legacy_schema_compatible=False,
    )
    assert report["passed"] is False
    assert report["blockers"] == [
        "cross-project-misattribution",
        "legacy-output-schema-break",
        "relay-overwrite",
        "scope-leak",
    ]


def test_canary_merges_retries_by_task_note_and_hash():
    context, *_ = _context(caps=TaskMemoryCapabilities(note_fetch=True))
    failed_adapter = TaskMemoryAdapter(
        provider=_provider_for("note_fetch"),
        note_fetch=lambda _task_id, _note_id: (_ for _ in ()).throw(PermissionError()),
    )
    failed_prepared = failed_adapter.prepare(context)
    failed = failed_adapter.fetch_note(failed_prepared, "note-1")

    retry_context = replace(
        context,
        attempt_id="job-retry-two",
        job_id="job-retry-two",
        session_proxy="job:job-retry-two",
    )
    retry_adapter = TaskMemoryAdapter(
        provider=_provider_for("note_fetch"),
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    )
    retry_prepared = retry_adapter.prepare(retry_context)
    retried = retry_adapter.fetch_note(retry_prepared, "note-1")
    report = summarize_canary(
        (*failed_prepared.events, *failed.events, *retry_prepared.events, *retried.events),
        minimum_successes=1,
        minimum_rate=0.95,
    )

    row = report["paths"]["note_fetch"]
    assert row["authorized_attempts"] == 1
    assert row["successes"] == 1
    assert row["permission_denials"] == 1
    assert row["success_rate"] == 1.0


def test_unavailable_capability_is_ineligible_and_does_not_fallback_after_failure():
    context, *_ = _context(
        caps=TaskMemoryCapabilities(inline=False, snapshot=False, note_fetch=False)
    )
    called = []
    result = TaskMemoryAdapter(provider=lambda request: called.append(request)).prepare(context)
    assert result.status == "ineligible"
    assert result.reason == "no-supported-delivery-capability"
    assert result.mode == "ineligible"
    assert called == []

    context, *_ = _context()
    failed = TaskMemoryAdapter(
        provider=_provider_for("note_fetch"),
        note_fetch=lambda _task_id, _note_id: (_ for _ in ()).throw(PermissionError()),
    )
    prepared = failed.prepare(context)
    denied = failed.fetch_note(prepared, "note-1")
    assert denied.reason == "permission-denied"
    assert [event["event"] for event in denied.events] == ["read-attempted", "read-failed"]


def test_receipt_sidecar_is_deduplicated_append_only_and_contains_no_note_body(tmp_path):
    context, *_ = _context()
    prepared = TaskMemoryAdapter(provider=_provider_for("inline")).prepare(context)
    store = TaskMemoryReceiptStore(tmp_path / "coordinator")
    first = store.append(prepared.events[0])
    retried = {**prepared.events[0], "timestamp": "2099-01-01T00:00:00Z"}
    assert store.append(retried) == first
    with pytest.raises(ValueError, match="receipt conflict"):
        store.append({**prepared.events[0], "tool": "changed-tool"})

    receipt_rows = store.events_for_run(context.repo, context.work_id, context.workflow_run_id)
    assert len(receipt_rows) == 1
    assert receipt_rows[0]["event_id"] == first["event_id"]
    sidecar = store.sidecar_path(context.repo, context.work_id, context.workflow_run_id)
    assert sidecar.parent.stat().st_mode & 0o777 == 0o700
    assert sidecar.stat().st_mode & 0o777 == 0o600
    assert NOTE_CONTENT not in sidecar.read_text(encoding="utf-8")
    assert not sidecar.is_symlink()


def test_manager_writer_binds_receipt_to_persisted_run_job_and_actual_routing(tmp_path):
    from paulsha_cortex.coordinator import manager

    context, _work_item, run, _step, job = _context()
    prepared = TaskMemoryAdapter(provider=_provider_for("inline")).prepare(context)

    class Registry:
        def get_workflow_run(self, run_id):
            assert run_id == run.run_id
            return run

        def list_jobs(self):
            return [job]

    root = tmp_path / "coordinator"
    written = manager.record_task_memory_receipt(
        Registry(), prepared.events[0], coordinator_root=root
    )
    store = TaskMemoryReceiptStore(root)
    assert store.events_for_run(context.repo, context.work_id, context.workflow_run_id) == [written]
    with pytest.raises(ValueError, match="routing identity mismatch"):
        manager.record_task_memory_receipt(
            Registry(),
            {**prepared.events[0], "executor": "unregistered-executor"},
            coordinator_root=root,
        )
    assert len(store.events_for_run(context.repo, context.work_id, context.workflow_run_id)) == 1


def test_manager_rejects_out_of_order_or_unreturned_task_memory_receipts(tmp_path):
    from paulsha_cortex.coordinator import manager

    context, _work_item, run, _step, job = _context(caps=TaskMemoryCapabilities(inline=True))
    worktree = tmp_path / "job-worktree"
    artifact = worktree / "src" / "parser.py"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"updated parser")
    job["worktree"] = str(worktree)
    adapter = TaskMemoryAdapter(provider=_provider_for("inline"))
    prepared = adapter.prepare(context)
    delivered = adapter.confirm_context_delivered(prepared)
    applied = adapter.record_applied(
        prepared,
        "note-1",
        evidence_ref="src/parser.py",
        evidence_sha256=hashlib.sha256(b"updated parser").hexdigest(),
    )

    class Registry:
        def get_workflow_run(self, _run_id):
            return run

        def list_jobs(self):
            return [job]

    root = tmp_path / "coordinator"
    with pytest.raises(ValueError, match="no matching prior delivery evidence"):
        manager.record_task_memory_receipt(Registry(), applied, coordinator_root=root)
    with pytest.raises(ValueError, match="no matching prior delivery evidence"):
        manager.record_task_memory_receipt(Registry(), delivered[0], coordinator_root=root)

    manager.record_task_memory_receipt(Registry(), prepared.events[0], coordinator_root=root)
    manager.record_task_memory_receipt(Registry(), prepared.events[1], coordinator_root=root)
    manager.record_task_memory_receipt(Registry(), delivered[0], coordinator_root=root)

    with pytest.raises(ValueError, match="artifact hash mismatch"):
        manager.record_task_memory_receipt(
            Registry(),
            {**applied, "evidence": {"ref": "src/parser.py", "sha256": "0" * 64}},
            coordinator_root=root,
        )
    outside = tmp_path / "outside-artifact"
    outside.write_bytes(b"updated parser")
    artifact.unlink()
    artifact.symlink_to(outside)
    with pytest.raises(ValueError, match="artifact cannot be verified"):
        manager.record_task_memory_receipt(Registry(), applied, coordinator_root=root)
    artifact.unlink()
    artifact.write_bytes(b"updated parser")
    assert manager.record_task_memory_receipt(
        Registry(), applied, coordinator_root=root
    )["event"] == "applied-with-evidence"


def test_work_show_task_memory_is_separate_versioned_read_model(tmp_path, capsys):
    context, _work_item, run, _step, job = _context()
    work_item = WorkItem(
        work_id=run.work_id,
        repo=run.repo,
        title="Task memory read model fixture",
        state="ongoing",
        phase=run.current_phase,
        facets=(),
        sources=(),
        next_actions=(),
        workflow_run_id=run.run_id,
        updated_at="2026-09-26T00:01:00+00:00",
    )
    prepared = TaskMemoryAdapter(provider=_provider_for("inline")).prepare(context)
    receipts = TaskMemoryReceiptStore(tmp_path / "coordinator")
    receipts.append(prepared.events[0])

    class WorkClient:
        def request(self, _request):
            return {
                "ok": True,
                "data": {"schema": "cortex-work/v1", "item": work_item.to_dict()},
            }

    class Registry:
        def list_workflow_runs(self):
            return [run]

        def list_jobs(self):
            return [job]

    assert umbrella_cli.main(
        ["work", "show", run.work_id, "--repo", run.repo, "--json"],
        work_client=WorkClient(),
    ) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert legacy["schema"] == "cortex-work/v1"
    assert "task_memory" not in legacy

    assert umbrella_cli.main(
        ["work", "show", run.work_id, "--repo", run.repo, "--task-memory", "--json"],
        work_client=WorkClient(),
        task_memory_registry=Registry(),
        task_memory_receipts=receipts,
    ) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["schema"] == "cortex/task-memory-read-model/v1"
    assert current["runs"][0]["receipts"]["event_count"] == 1


def test_work_show_help_exposes_task_memory_read_model_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        umbrella_cli.main(["work", "show", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--task-memory" in output
    assert "receipt/test/review evidence" in output


def test_tool_neutral_events_and_applied_receipt_require_bound_action_evidence():
    context, *_ = _context()
    adapter = TaskMemoryAdapter(
        provider=_provider_for("note_fetch"),
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    )
    prepared = adapter.prepare(context)
    adapter.fetch_note(prepared, "note-1")
    with pytest.raises(ValueError, match="action evidence is required"):
        adapter.record_applied(prepared, "note-1")
    applied = adapter.record_applied(
        prepared,
        "note-1",
        evidence_ref="src/parser.py",
        evidence_sha256=hashlib.sha256(b"updated parser").hexdigest(),
    )
    assert applied["event"] == "applied-with-evidence"
    assert applied["evidence"]["ref"] == "src/parser.py"
    assert applied["evidence"]["sha256"] == hashlib.sha256(b"updated parser").hexdigest()


def test_inline_applied_receipt_requires_confirmed_delivery_and_artifact_evidence():
    context, *_ = _context(caps=TaskMemoryCapabilities(inline=True))
    adapter = TaskMemoryAdapter(provider=_provider_for("inline"))
    prepared = adapter.prepare(context)
    with pytest.raises(ValueError, match="delivered content"):
        adapter.record_applied(
            prepared,
            "note-1",
            evidence_ref="src/parser.py",
            evidence_sha256=hashlib.sha256(b"updated parser").hexdigest(),
        )

    delivered = adapter.confirm_context_delivered(prepared)
    assert delivered[0]["event"] == "context-delivered"
    with pytest.raises(ValueError, match="action evidence is required"):
        adapter.record_applied(prepared, "note-1")
    applied = adapter.record_applied(
        prepared,
        "note-1",
        evidence_ref="src/parser.py",
        evidence_sha256=hashlib.sha256(b"updated parser").hexdigest(),
    )
    assert applied["event"] == "applied-with-evidence"
    assert applied["counts_as_read"] is False


def test_formal_read_model_joins_work_run_routing_plan_receipts_and_gate_evidence(tmp_path):
    context, work_item, run, _step, job = _context(phase="build")
    before = run.to_dict()
    prepared = TaskMemoryAdapter(provider=_provider_for("inline")).prepare(context)
    store = TaskMemoryReceiptStore(tmp_path / "coordinator")
    store.append(prepared.events[0])
    view = project_task_memory_read_model(
        work_item=work_item,
        runs=(run,),
        jobs=(job,),
        receipts=store,
    )

    assert view["schema"] == "cortex/task-memory-read-model/v1"
    assert view["work_item"] == {
        "repo": "acme/demo",
        "work_id": "demo",
        "state": "ongoing",
        "phase": "build",
        "facets": [],
        "next_actions": [],
        "blocking_reason": None,
    }
    run_view = view["runs"][0]
    assert run_view["run_id"] == run.run_id
    assert run_view["status"] == "ongoing"
    assert run_view["planning_source_revision"] == run.planning_source_revision
    assert run_view["planning_artifacts"][0]["sha256"] == "2" * 64
    assert run_view["jobs"][0]["job_id"] == job["job_id"]
    assert run_view["jobs"][0]["executor"] == job["executor"]
    assert run_view["jobs"][0]["model_id"] == job["model_id"]
    assert run_view["receipts"]["event_count"] == 1
    assert run_view["receipts"]["events"][0]["event"] == "candidate-selected"
    assert run_view["gate_evidence"]["workflow_refs"] == list(run.evidence_refs)
    assert run_view["gate_evidence"]["gate_refs"][0]["kind"] == "maintainer-review"
    assert run.to_dict() == before
    assert "task_memory" not in run.to_dict()


def test_read_model_keeps_blocked_and_undispatched_state_explicit(tmp_path):
    context, work_item, run, _step, _job = _context(phase="plan")
    work_item.state = "todo"
    work_item.phase = "plan"
    work_item.facets = ("needs_human",)
    blocked_run = replace(
        run,
        current_phase="plan",
        facets=("needs_human",),
    )
    view = project_task_memory_read_model(
        work_item=work_item,
        runs=(blocked_run,),
        jobs=(),
        receipts=TaskMemoryReceiptStore(tmp_path / "coordinator"),
        blocking_reason={"reason": "provider-unavailable", "run_id": run.run_id},
    )

    run_view = view["runs"][0]
    assert view["work_item"]["state"] == "todo"
    assert run_view["status"] == "ongoing"
    assert run_view["state"] == "plan"
    assert run_view["facets"] == ["needs_human"]
    assert run_view["needs_human_reason"] is None
    assert run_view["jobs"] == []
    assert run_view["receipts"]["event_count"] == 0
    assert run_view["receipts"]["sidecar_ref"] is None
    assert view["work_item"]["blocking_reason"]["reason"] == "provider-unavailable"


@pytest.mark.parametrize(
    ("repo", "work_id", "phase"),
    [("acme/service-a", "service-a", "build"), ("example/tool-b", "tool-b", "verify")],
)
def test_adapter_has_no_project_or_task_kind_special_case(repo, work_id, phase):
    context, *_ = _context(repo=repo, work_id=work_id, phase=phase)
    result = TaskMemoryAdapter(
        provider=_provider_for("note_fetch", project=repo),
        note_fetch=lambda _task_id, _note_id: NOTE_CONTENT,
    ).prepare(context)
    assert result.status == "offered"
    assert result.context.repo == repo
    assert result.context.task_kind == phase
    assert result.events[0]["project"] == repo
    assert result.events[0]["project"] == context.project
