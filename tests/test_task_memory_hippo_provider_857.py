from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import live_receipt_validators, manager, requirement_delivery
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.task_memory import (
    TaskMemoryAdapter,
    TaskMemoryCapabilities,
    TaskMemoryContext,
    TaskMemoryProviderError,
    TaskMemoryReceiptStore,
    _validate_payload,
    task_memory_context_from_cortex,
)
from paulsha_cortex.coordinator.task_memory_hippo import (
    HIPPO_COMMAND_ENV,
    HIPPO_TIMEOUT_ENV,
    HippoTaskMemoryClient,
    resolve_hippo_command,
)
from paulsha_cortex.coordinator.workflow import WorkflowStep
from paulsha_cortex.porcelain import task_memory_canary
from paulsha_cortex import cli


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "task_memory"
_BODY = "Use the scoped parser boundary and keep retrieval task-local.\n"
_EXCERPT = "Use the scoped parser boundary."


def _context(mode: str = "note_fetch", *, allowed_sources=("hippo",), repo="acme/demo"):
    identity = hashlib.sha256(repo.encode()).hexdigest()[:8]
    work_id = f"provider-test-{mode}-{identity}"
    run_id = f"provider-run-{mode}-{identity}"
    card = f"provider-card-{mode}"
    step = SimpleNamespace(phase="build", card=card)
    run = SimpleNamespace(repo=repo, work_id=work_id, run_id=run_id, steps=(step,))
    work_item = SimpleNamespace(repo=run.repo, work_id=work_id, workflow_run_id=run_id)
    job = {
        "job_id": f"provider-job-{mode}",
        "workflow_run_id": run_id,
        "workflow_card": card,
        "workflow_phase": "build",
        "executor": "fixture-executor",
        "tool": "fixture-executor",
        "model_id": "fixture-model",
    }
    return task_memory_context_from_cortex(
        work_item=work_item,
        run=run,
        step=step,
        job=job,
        capabilities=TaskMemoryCapabilities(
            inline=mode == "inline",
            snapshot=mode == "snapshot",
            note_fetch=mode == "note_fetch",
        ),
        goal="Verify the bounded provider contract for this work item.",
        allowed_evidence_sources=allowed_sources,
    )


def _fixture_context() -> TaskMemoryContext:
    request = json.loads((_FIXTURE_DIR / "hippo-note-fetch-request.json").read_text())
    scope = request["delivery"]["host_scope"]
    return TaskMemoryContext(
        task_id=request["task_id"],
        attempt_id="job-fixture",
        session_id=None,
        session_proxy="job:job-fixture",
        model_id=None,
        repo=request["project"],
        work_id=scope["work_id"],
        workflow_run_id=scope["workflow_run_id"],
        job_id="job-fixture",
        card=scope["card"],
        executor="fixture-cli",
        tool="fixture-cli",
        project=request["project"],
        task_kind="build",
        goal=request["intent"],
        related_files=("src/parser.py",),
        related_errors=(),
        capabilities=TaskMemoryCapabilities(note_fetch=True),
        read_scope=scope["read_scope"],
        allowed_evidence_sources=tuple(scope["allowed_evidence_sources"]),
    )


def _fake_hippo(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "fake-hippo"
    log = tmp_path / "fake-hippo-input.jsonl"
    script.write_text(
        r'''#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import time

argv = sys.argv[1:]
operation = argv[-1] if argv else ""
codes = {
    10: "permission-denied", 11: "timeout", 12: "scope-mismatch",
    13: "hash-mismatch", 14: "unsupported-schema", 15: "manifest-mismatch",
    16: "invalid-request", 17: "size-limit", 18: "provider-error",
}
request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
log_path = os.environ.get("FAKE_HIPPO_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"operation": operation, "request": request}) + "\n")

def fail(code):
    line = os.environ.get("FAKE_HIPPO_STDERR")
    if line is None:
        line = "hippo-task-memory: " + codes.get(code, "provider-error")
    sys.stderr.write(line + "\n")
    raise SystemExit(code)

scope = request.get("delivery", {}).get("host_scope", {})
if operation == "provide":
    if request.get("project") != scope.get("repo"):
        fail(12)
    if "hippo" not in scope.get("allowed_evidence_sources", []):
        fail(10)
    sleep = float(os.environ.get("FAKE_HIPPO_SLEEP_PROVIDE", "0"))
    if sleep:
        time.sleep(sleep)
    code = int(os.environ.get("FAKE_HIPPO_EXIT_PROVIDE", "0"))
    if code:
        fail(code)
    if os.environ.get("FAKE_HIPPO_OVERFLOW_PROVIDE") == "1":
        sys.stdout.write("x" * (512 * 1024 + 128))
        raise SystemExit(0)
    mode = request["delivery"]["mode"]
    project = request["project"]
    full_content = "Use the scoped parser boundary and keep retrieval task-local.\n"
    excerpt = "Use the scoped parser boundary."
    delivered = excerpt if mode == "inline" else full_content
    digest = hashlib.sha256(delivered.encode("utf-8")).hexdigest()
    version = "sha256:" + digest
    candidate = {
        "ref": "fixture-note-1", "note_id": "fixture-note-1", "rank": 1,
        "summary": "Keep retrieval task-local.",
        "authorization": {"status": "authorized"},
        "availability": {"status": "available"},
        "content_hash": digest, "content_version": version,
        "applicability": ["same repository"],
        "relevance_reason": "The task uses the same scoped boundary.",
        "source_time": "2026-09-25T12:00:00Z", "project": project,
    }
    if mode == "inline":
        candidate["excerpt"] = excerpt
    manifest_entry = {
        "note_id": candidate["note_id"], "content_hash": digest,
        "content_version": version, "project": project,
    }
    if mode == "snapshot":
        manifest_entry["content"] = full_content
    entries = [manifest_entry]
    if os.environ.get("FAKE_HIPPO_PAYLOAD_MODE") == "manifest-outside":
        entries.append({
            "note_id": "outside-note", "content_hash": "0" * 64,
            "content_version": "outside", "project": project,
        })
    manifest = {
        "schema": "hippo/task-memory-manifest/v1", "task_id": request["task_id"],
        "project": project, "entries": entries,
    }
    unsigned = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    manifest["sha256"] = hashlib.sha256(unsigned).hexdigest()
    payload = {
        "schema_version": "1", "task_id": request["task_id"],
        "intent": request["intent"], "project": project, "candidates": [candidate],
        "delivery": {"mode": mode, "capabilities": request["delivery"]["capabilities"], "manifest": manifest},
        "evidence": [], "producer": {"id": "hippo-task-memory-provider", "version": "1"},
        "adapter": {"id": "hippo-host-adapter", "version": "1"},
    }
    fault = os.environ.get("FAKE_HIPPO_PAYLOAD_MODE")
    # #857 G857-3 負例：provider 覆寫 Cortex 轉交的 task identity／delivery mode
    # （relay overwrite），或輸出不再符合既有 v1 schema（schema break）。
    if fault == "mode-overwrite":
        payload["delivery"]["mode"] = "inline" if mode != "inline" else "note_fetch"
    elif fault == "task-id-overwrite":
        payload["task_id"] = "task-overwritten-by-provider"
    elif fault == "schema-major-2":
        payload["schema_version"] = "2"
    elif fault == "not-json":
        sys.stdout.write("hippo emitted a non-JSON line\n")
        raise SystemExit(0)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
elif operation == "fetch":
    sleep = float(os.environ.get("FAKE_HIPPO_SLEEP_FETCH", "0"))
    if sleep:
        time.sleep(sleep)
    code = int(os.environ.get("FAKE_HIPPO_EXIT_FETCH", "0"))
    if code:
        fail(code)
    if os.environ.get("FAKE_HIPPO_OVERFLOW_FETCH") == "1":
        sys.stdout.write("x" * (512 * 1024 + 128))
        raise SystemExit(0)
    content = "Changed content to trigger Cortex hash verification.\n" if os.environ.get("FAKE_HIPPO_WRONG_FETCH") == "1" else "Use the scoped parser boundary and keep retrieval task-local.\n"
    sys.stdout.write(json.dumps({"content": content, "content_hash": "ignored", "content_version": "ignored"}) + "\n")
else:
    fail(16)
''',
        encoding="utf-8",
    )
    script.chmod(0o700)
    return script, log


def _client(monkeypatch, script: Path, log: Path, **env):
    monkeypatch.setenv(HIPPO_COMMAND_ENV, json.dumps([str(script)]))
    monkeypatch.setenv("FAKE_HIPPO_LOG", str(log))
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    client = HippoTaskMemoryClient.from_environment()
    assert client is not None
    return client


@pytest.mark.parametrize(
    ("exit_code", "expected_reason", "expected_status", "expected_code"),
    [
        (10, "permission-denied", "read-failed", "permission-denied"),
        (11, "provider-timeout", "read-failed", "timeout"),
        (12, "scope-mismatch", "read-failed", "scope-mismatch"),
        (13, "content-hash-mismatch", "read-failed", "hash-mismatch"),
        (14, "unsupported-schema-major", "parked", "unsupported-schema"),
        (15, "manifest-mismatch", "read-failed", "manifest-mismatch"),
        (16, "provider-error", "read-failed", "invalid-request"),
        (17, "provider-error", "read-failed", "size-limit"),
        (18, "provider-error", "read-failed", "provider-error"),
    ],
)
def test_provide_exit_codes_map_to_bounded_adapter_diagnostics(
    tmp_path, monkeypatch, exit_code, expected_reason, expected_status, expected_code
):
    script, log = _fake_hippo(tmp_path)
    client = _client(monkeypatch, script, log, FAKE_HIPPO_EXIT_PROVIDE=exit_code)
    context = _context()
    provider, fetch = client.callbacks(context)

    prepared = TaskMemoryAdapter(provider=provider, note_fetch=fetch).prepare(context)

    assert prepared.reason == expected_reason
    assert prepared.status == expected_status
    assert prepared.events[0]["provider_code"] == expected_code
    if exit_code == 10:
        assert prepared.events[0]["permission_layer"] == "provider"


@pytest.mark.parametrize(
    ("exit_code", "expected_reason", "expected_code"),
    [
        (10, "permission-denied", "permission-denied"),
        (11, "provider-timeout", "timeout"),
        (12, "scope-mismatch", "scope-mismatch"),
        (13, "content-hash-mismatch", "hash-mismatch"),
        (14, "unsupported-schema-major", "unsupported-schema"),
        (15, "manifest-mismatch", "manifest-mismatch"),
        (16, "provider-error", "invalid-request"),
        (17, "provider-error", "size-limit"),
        (18, "provider-error", "provider-error"),
    ],
)
def test_fetch_exit_codes_map_to_bounded_adapter_diagnostics(
    tmp_path, monkeypatch, exit_code, expected_reason, expected_code
):
    script, log = _fake_hippo(tmp_path)
    client = _client(monkeypatch, script, log, FAKE_HIPPO_EXIT_FETCH=exit_code)
    context = _context()
    provider, fetch = client.callbacks(context)
    adapter = TaskMemoryAdapter(provider=provider, note_fetch=fetch)
    prepared = adapter.prepare(context)
    assert prepared.status == "offered"

    result = adapter.fetch_note(prepared, "fixture-note-1")

    assert result.reason == expected_reason
    assert result.content is None
    assert result.events[-1]["provider_code"] == expected_code
    if exit_code == 10:
        assert result.events[-1]["permission_layer"] == "provider"


@pytest.mark.parametrize("operation", ["provide", "fetch"])
def test_subprocess_timeout_is_bounded_for_both_operations(tmp_path, monkeypatch, operation):
    script, log = _fake_hippo(tmp_path)
    sleep_env = f"FAKE_HIPPO_SLEEP_{operation.upper()}"
    client = _client(
        monkeypatch,
        script,
        log,
        **{sleep_env: "0.25", HIPPO_TIMEOUT_ENV: "0.05"},
    )
    context = _context()
    provider, fetch = client.callbacks(context)
    adapter = TaskMemoryAdapter(provider=provider, note_fetch=fetch)
    prepared = adapter.prepare(context)
    if operation == "provide":
        assert prepared.reason == "provider-timeout"
        assert prepared.events[0]["provider_code"] == "timeout"
    else:
        assert prepared.status == "offered"
        result = adapter.fetch_note(prepared, "fixture-note-1")
        assert result.reason == "provider-timeout"
        assert result.events[-1]["provider_code"] == "timeout"


@pytest.mark.parametrize("operation", ["provide", "fetch"])
def test_subprocess_stdout_limit_is_enforced_for_both_operations(tmp_path, monkeypatch, operation):
    script, log = _fake_hippo(tmp_path)
    client = _client(
        monkeypatch,
        script,
        log,
        **{f"FAKE_HIPPO_OVERFLOW_{operation.upper()}": "1"},
    )
    context = _context()
    provider, fetch = client.callbacks(context)
    adapter = TaskMemoryAdapter(provider=provider, note_fetch=fetch)
    prepared = adapter.prepare(context)
    if operation == "provide":
        assert prepared.reason == "provider-error"
        assert prepared.events[0]["provider_code"] == "size-limit"
    else:
        assert prepared.status == "offered"
        result = adapter.fetch_note(prepared, "fixture-note-1")
        assert result.reason == "provider-error"
        assert result.events[-1]["provider_code"] == "size-limit"


@pytest.mark.parametrize(
    "stderr_text",
    [
        "token=fixture-secret private provider details",
        "hippo-task-memory: provider-error\ntoken=fixture-secret private provider details",
    ],
)
def test_stderr_injection_is_discarded_and_provider_code_is_bounded(
    tmp_path, monkeypatch, stderr_text
):
    script, log = _fake_hippo(tmp_path)
    client = _client(
        monkeypatch,
        script,
        log,
        FAKE_HIPPO_EXIT_PROVIDE="18",
        FAKE_HIPPO_STDERR=stderr_text,
    )
    context = _context()
    provider, fetch = client.callbacks(context)
    prepared = TaskMemoryAdapter(provider=provider, note_fetch=fetch).prepare(context)
    encoded = json.dumps(prepared.events)
    assert prepared.events[0]["provider_code"] == "provider-error"
    assert "fixture-secret" not in encoded
    assert "provider details" not in encoded


def test_fetch_closure_binds_the_validated_manifest_and_rejects_outside_note(
    tmp_path, monkeypatch
):
    script, log = _fake_hippo(tmp_path)
    client = _client(monkeypatch, script, log)
    context = _context()
    provider, fetch = client.callbacks(context)
    adapter = TaskMemoryAdapter(provider=provider, note_fetch=fetch)
    prepared = adapter.prepare(context)
    assert prepared.status == "offered"
    with pytest.raises(TaskMemoryProviderError) as wrong_task:
        fetch("other-task", "fixture-note-1")
    assert wrong_task.value.reason == "task-id-mismatch"
    with pytest.raises(TaskMemoryProviderError) as exc:
        fetch(context.task_id, "outside-note")
    assert exc.value.code == "manifest-mismatch"
    assert exc.value.reason == "note-not-in-manifest"
    assert len(log.read_text().splitlines()) == 1

    assert fetch(context.task_id, "fixture-note-1") == _BODY
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    wrapper = calls[-1]["request"]
    assert calls[-1]["operation"] == "fetch"
    assert wrapper["envelope"] == context.to_envelope(mode="note_fetch")
    assert wrapper["manifest"] == prepared.manifest
    assert wrapper["note_id"] == "fixture-note-1"


def test_fetch_content_hash_is_still_verified_by_cortex(tmp_path, monkeypatch):
    script, log = _fake_hippo(tmp_path)
    client = _client(monkeypatch, script, log, FAKE_HIPPO_WRONG_FETCH="1")
    context = _context()
    provider, fetch = client.callbacks(context)
    adapter = TaskMemoryAdapter(provider=provider, note_fetch=fetch)
    prepared = adapter.prepare(context)

    result = adapter.fetch_note(prepared, "fixture-note-1")

    assert result.reason == "content-hash-mismatch"
    assert result.content is None
    assert result.events[-1]["provider_code"] == "hash-mismatch"


def test_manifest_outside_candidate_is_rejected_by_cortex_validator(tmp_path, monkeypatch):
    script, log = _fake_hippo(tmp_path)
    client = _client(monkeypatch, script, log, FAKE_HIPPO_PAYLOAD_MODE="manifest-outside")
    context = _context()
    provider, fetch = client.callbacks(context)

    prepared = TaskMemoryAdapter(provider=provider, note_fetch=fetch).prepare(context)

    assert prepared.status == "read-failed"
    assert prepared.reason == "manifest-mismatch"


def test_copied_hippo_request_and_payload_fixture_pass_cortex_validator():
    request = json.loads((_FIXTURE_DIR / "hippo-note-fetch-request.json").read_text())
    payload = json.loads((_FIXTURE_DIR / "hippo-note-fetch-payload.json").read_text())
    context = _fixture_context()

    normalized = _validate_payload(payload, context=context, requested_mode="note_fetch")

    assert normalized["task_id"] == request["task_id"]
    assert normalized["project"] == request["project"]
    assert normalized["delivery"]["manifest"]["schema"] == "hippo/task-memory-manifest/v1"
    assert not any(name.startswith("paulsha_hippo") for name in sys.modules)


def test_command_prefix_accepts_json_argv_and_shlex_and_missing_is_unavailable(
    tmp_path, monkeypatch
):
    script, _log = _fake_hippo(tmp_path)
    env = {"PATH": os.environ.get("PATH", ""), HIPPO_COMMAND_ENV: json.dumps([str(script), "--fixture"])}
    resolved = resolve_hippo_command(env)
    assert resolved is not None
    assert resolved[0] == str(script)
    assert resolved[1:] == ("--fixture",)
    env[HIPPO_COMMAND_ENV] = f"{script} --fixture"
    assert resolve_hippo_command(env) == resolved
    env[HIPPO_COMMAND_ENV] = json.dumps(["does-not-exist-task-memory-cli"])
    assert resolve_hippo_command(env) is None


def test_unset_blank_or_relative_hippo_cmd_never_searches_path(tmp_path):
    """issue #857 對抗審查 MAJOR：啟用 task-memory 時必須明示
    PSC_TASK_MEMORY_HIPPO_CMD 且第一個元素為絕對路徑；未設、空白或相對路徑
    一律視為 provider 缺席，即使 PATH 上真的有一個可執行的 `hippo` 也不能被
    意外執行到（避免 PSC_TASK_MEMORY_ENABLED=1 意外跑到 PATH 優先 binary）。
    """

    path_dir = tmp_path / "on-path"
    path_dir.mkdir()
    path_hippo = path_dir / "hippo"
    path_hippo.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8")
    path_hippo.chmod(0o700)
    base_env = {"PATH": f"{path_dir}{os.pathsep}{os.environ.get('PATH', '')}"}

    # 未設環境變數
    assert resolve_hippo_command(dict(base_env)) is None
    # 空白字串
    assert resolve_hippo_command({**base_env, HIPPO_COMMAND_ENV: "   "}) is None
    # 相對 bare name，即使 PATH 上有同名可執行檔也不得被搜到
    assert resolve_hippo_command({**base_env, HIPPO_COMMAND_ENV: "hippo"}) is None
    assert resolve_hippo_command({**base_env, HIPPO_COMMAND_ENV: json.dumps(["hippo"])}) is None
    # 相對路徑（有子路徑但非絕對）
    assert resolve_hippo_command({**base_env, HIPPO_COMMAND_ENV: "./hippo"}) is None

    # 明示絕對路徑才會被接受
    absolute = str(path_hippo)
    resolved = resolve_hippo_command({**base_env, HIPPO_COMMAND_ENV: absolute})
    assert resolved == (absolute,)


def test_optional_real_hippo_cli_integration():
    if not os.environ.get(HIPPO_COMMAND_ENV):
        pytest.skip(
            "PSC_TASK_MEMORY_HIPPO_CMD is not set; live Hippo CLI integration was not requested"
        )
    client = HippoTaskMemoryClient.from_environment()
    assert client is not None
    repo = os.environ.get("PSC_TASK_MEMORY_INTEGRATION_REPO", "hamanpaul/paulsha-cortex")
    context = _context("note_fetch", repo=repo)
    provider, fetch = client.callbacks(context)
    adapter = TaskMemoryAdapter(provider=provider, note_fetch=fetch)
    prepared = adapter.prepare(context)
    assert prepared.status in {"offered", "ineligible"}
    if prepared.status == "offered":
        result = adapter.fetch_note(prepared, next(iter(prepared.candidates)))
        assert result.content is not None


class _CommitLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append({"slice_id": slice_id, "prompt": prompt, "worktree": worktree, "log_dir": log_dir})
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


class _RecordingCreator:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, branch: str, *, job_id: str | None = None, base_sha: str | None = None) -> str:
        return str(self.root)


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test User"], check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)


def _dispatch_once(tmp_path: Path):
    repo = "hamanpaul/paulsha-cortex"
    workspace = tmp_path / "repo"
    _init_git_repo(workspace)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    step = WorkflowStep(
        phase="build", persona="builder", card="tdd-red", executor="copilot",
        model="gpt", domain="openai", inputs=(), outputs=(),
        commit_policy="required", test_policy="red-required", gate_result="pending",
    )
    run = registry._manager_create_workflow_run(
        work_id="task-memory-manager-test", repo=repo,
        claim_key="claim:v1:" + "1" * 64, source_revision="2" * 64,
        workspace_root=str(workspace), combo="feature-oneshot", current_phase="build",
        steps=(step,), issue_refs=(f"{repo}#857",), openspec_refs=(), pr_refs=(),
        attempts={step.card: 1}, gate_status="running",
    )
    launcher = _CommitLauncher()
    dispatcher = SimpleNamespace(
        _registry=registry,
        _worktree_creator=_RecordingCreator(workspace),
        _git_runner=None,
    )
    job = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=IdentityRegistry.from_rows(
            [{"executor": "copilot", "model_id": "gpt", "independence_domain": "openai", "capabilities": ["build"]}]
        ),
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
    )
    assert job is not None
    return registry, run, launcher, tmp_path / "coordinator"


def test_manager_dispatch_is_byte_identical_when_flag_is_off_and_provider_absent(
    tmp_path, monkeypatch
):
    script, log = _fake_hippo(tmp_path)
    monkeypatch.setenv(HIPPO_COMMAND_ENV, json.dumps([str(script)]))
    monkeypatch.setenv("FAKE_HIPPO_LOG", str(log))
    monkeypatch.delenv("PSC_TASK_MEMORY_ENABLED", raising=False)
    monkeypatch.setattr(manager, "_workflow_job_prompt", lambda *args, **kwargs: "LEGACY-PROMPT")

    _registry, _run, launcher, coordinator_root = _dispatch_once(tmp_path / "disabled")

    assert launcher.calls[0]["prompt"] == "LEGACY-PROMPT"
    assert not log.exists()
    assert not (coordinator_root / "task-memory" / "receipts").exists()

    missing_script = tmp_path / "missing-hippo-command"
    monkeypatch.setenv("PSC_TASK_MEMORY_ENABLED", "1")
    monkeypatch.setenv(HIPPO_COMMAND_ENV, json.dumps([str(missing_script)]))
    _registry, run, launcher, coordinator_root = _dispatch_once(tmp_path / "unavailable")
    assert launcher.calls[0]["prompt"] == "LEGACY-PROMPT"
    receipts = TaskMemoryReceiptStore(coordinator_root).events_for_run(
        "hamanpaul/paulsha-cortex", "task-memory-manager-test", run.run_id
    )
    assert len(receipts) == 1
    assert receipts[0]["reason"] == "provider-unavailable"


def test_manager_dispatch_opt_in_uses_provider_and_records_cortex_sidecar(
    tmp_path, monkeypatch
):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)
    monkeypatch.setenv("PSC_TASK_MEMORY_ENABLED", "1")
    monkeypatch.setattr(manager, "_workflow_job_prompt", lambda *args, **kwargs: "LEGACY-PROMPT")

    registry, run, launcher, coordinator_root = _dispatch_once(tmp_path / "enabled")

    assert "Optional task-scoped Hippo memory" in launcher.calls[0]["prompt"]
    assert _EXCERPT in launcher.calls[0]["prompt"]
    events = TaskMemoryReceiptStore(coordinator_root).events_for_run(
        "hamanpaul/paulsha-cortex", "task-memory-manager-test", run.run_id
    )
    assert [event["event"] for event in events] == [
        "candidate-selected", "offer-emitted", "context-delivered"
    ]
    assert all(event["repo"] == run.repo and event["project"] == run.repo for event in events)
    assert _BODY not in json.dumps(events)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 1 and calls[0]["operation"] == "provide"


def test_live_canary_cli_emits_bounded_machine_json_and_private_evidence(
    tmp_path, monkeypatch, capsys
):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)
    evidence = tmp_path / "evidence" / "canary.json"

    result = cli.main(
        [
            "task-memory", "canary", "--repo", "acme/demo", "--repo", "other/demo",
            "--runs", "5", "--evidence-path", str(evidence),
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert result == 0
    assert report["passed"] is True
    assert report["schema"] == "cortex/task-memory-live-canary/v1"
    assert report["repositories"] == ["acme/demo", "other/demo"]
    # #845 B5：`paths` 以 #857 spec R3 的 delivery 結果命名，恰為三條 path，
    # 每條另以 `mode` 標明對應的 executor capability。
    assert set(report["paths"]) == {"context-delivered", "snapshot-ready", "note-fetch"}
    assert {name: row["mode"] for name, row in report["paths"].items()} == {
        "context-delivered": "inline",
        "snapshot-ready": "snapshot",
        "note-fetch": "note_fetch",
    }
    for row in report["paths"].values():
        assert row["attempts"] == row["eligible_authorized_attempts"] == 10
        assert row["successes"] == 10
        assert row["success_rate"] == row["successes"] / row["attempts"] == 1.0
        assert row["successful_provides"] == 10
        assert all(repo_row["successful_provides"] == 5 for repo_row in row["per_repo"].values())
    assert report["content_retrieval"]["paths"] == ["note-fetch", "snapshot-ready"]
    assert report["content_retrieval"]["attempts"] == 20
    assert report["content_retrieval"]["eligible_authorized_attempts"] == 20
    assert report["content_retrieval"]["successes"] == 20
    assert report["content_retrieval"]["success_rate"] == 1.0
    assert report["content_retrieval"]["passed"] is True
    assert report["paths"]["note-fetch"]["success_semantics"] == (
        "content-returned after note content hash match"
    )
    assert report["paths"]["snapshot-ready"]["success_event"] == "content-returned"
    assert report["paths"]["snapshot-ready"]["success_semantics"] == (
        "content-returned after snapshot read-back and hash match; snapshot-ready is not a read"
    )
    assert report["paths"]["context-delivered"]["metric_kind"] == "delivery"
    assert report["paths"]["context-delivered"]["eligible_authorized_delivery_rate"] == 1.0
    assert report["paths"]["context-delivered"]["counts_as_read"] is False
    assert report["inline_delivery"]["delivery_rate"] == 1.0
    assert report["inline_delivery"]["counts_as_read"] is False
    assert report["negative_controls"] == [
        {"case": "permission-denied", "attempted": 6, "observed": 6, "status": "passed"},
        {
            "case": "cross-scope-rejection",
            "attempted": 6,
            "observed": 6,
            "misattribution_leaks": 0,
            "status": "passed",
        },
    ]
    assert [(row["repo"], row["status"]) for row in report["cross_project"]] == [
        ("acme/demo", "passed"),
        ("other/demo", "passed"),
    ]
    assert report["scope_checks"]["scope_leaks"] == 0
    assert report["relay_checks"] == {
        "executor_prompts_checked": 10,
        "executor_prompt_overwrites": 0,
        "provider_identity_overwrites": 0,
        "passed": True,
    }
    assert report["schema_checks"] == {
        "provider_payloads_checked": 30,
        "schema_breaks": 0,
        "passed": True,
    }
    assert report["blockers"] == []
    assert report["legacy_strict_kpi_mutated"] is False
    assert report["strict_read_violations"] == 0
    assert isinstance(report["executor"]["id"], str) and report["executor"]["id"]
    assert report["target"] is None
    assert _BODY not in captured.out
    assert _BODY not in evidence.read_text()
    assert str(tmp_path) not in captured.out
    assert evidence.stat().st_mode & 0o777 == 0o600


def test_live_canary_rejects_fewer_than_five_successful_provides_per_repo_path(
    tmp_path, monkeypatch, capsys
):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)

    result = cli.main(
        [
            "task-memory", "canary", "--repo", "acme/demo", "--repo", "other/demo",
            "--runs", "1",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert report["passed"] is False
    assert all(
        not row["passed"] and row["successful_provides"] == 1
        for mode in report["paths"].values()
        for row in mode["per_repo"].values()
    )
    # 聚合指標也必須套用每 repo／path 至少 5 次的門檻，不能只看成功率。
    assert report["content_retrieval"]["passed"] is False
    assert report["inline_delivery"]["passed"] is False


# ---------------------------------------------------------------------------
# #845 B5：canary 產物與 `cortex/task-memory-live-canary/v1` validator 相容
# ---------------------------------------------------------------------------

_CANARY_TARGET = {
    "repo": "acme/cortex-deploy",
    "candidate_sha": "a" * 40,
    "artifact_sha256": "b" * 64,
    "source_revision": "c" * 40,
    "service": "cortex-manager.service",
    "instance": "cortex",
    "profile_key": "epk:v1:resolved:" + "d" * 64,
    "config_revision": "e" * 64,
}


def _run_canary_cli(capsys, *extra: str):
    result = cli.main(
        ["task-memory", "canary", "--repo", "acme/demo", "--repo", "other/demo", *extra]
    )
    return result, json.loads(capsys.readouterr().out)


def _live_receipt(evidence: dict) -> dict:
    return {
        "schema": "cortex/live-canary-receipt/v1",
        "result": "passed",
        "requirement_id": "R99",
        "requirement_revision": "r1",
        "acceptance_id": "R99-AC1",
        "observed_at": evidence["finished_at"],
        "target": dict(_CANARY_TARGET),
        "authority": {"id": "release-operator", "version": "1", "receipt": "approval:fixture"},
        "independence": {"review_domain": "reviewer-domain"},
        "kind": live_receipt_validators.KIND_TASK_MEMORY_LIVE_CANARY,
        "evidence": evidence,
    }


def test_live_canary_evidence_is_accepted_by_governed_live_receipt_validator(
    tmp_path, monkeypatch, capsys
):
    """#845 B5：`--evidence-path` 落檔的真 canary 產物原封不動包進
    `cortex/live-canary-receipt/v1`，必須走完 `_verify_live`（hash／target／
    期限／independence 導出）並通過 production validator。"""
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)
    target_file = tmp_path / "target.json"
    target_file.write_text(json.dumps(_CANARY_TARGET), encoding="utf-8")
    evidence_root = tmp_path / "coordinator"
    evidence_path = evidence_root / "evidence" / "task-memory-canary.json"

    result, report = _run_canary_cli(
        capsys, "--runs", "5", "--evidence-path", str(evidence_path),
        "--target-file", str(target_file),
    )

    assert result == 0 and report["passed"] is True
    produced = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert produced["target"] == _CANARY_TARGET
    receipt = _live_receipt(produced)
    receipt_path = evidence_root / "live" / "task-memory-canary-receipt.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    row = {
        "repo": _CANARY_TARGET["repo"],
        "candidate_sha": _CANARY_TARGET["candidate_sha"],
        "target": dict(_CANARY_TARGET),
        "live_receipt": {
            "locator": "live/task-memory-canary-receipt.json",
            "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        },
    }
    requirement = {
        "id": "R99",
        "revision": "r1",
        "evidence_policy": {"max_age_seconds": {"live": 3600}},
    }
    validator = live_receipt_validators.make_governed_live_receipt_validator(
        source_root=tmp_path, evidence_root=evidence_root
    )

    stage = requirement_delivery._verify_live(
        row,
        requirement,
        "R99-AC1",
        evidence_root=evidence_root,
        now=datetime.now(timezone.utc),
        validator=validator,
        review_document=None,
        domain_deriver=live_receipt_validators.derive_canary_domain,
    )

    assert stage["status"] == "verified", stage
    assert validator(receipt) is True
    assert live_receipt_validators.derive_canary_domain(receipt) == (
        "task-memory-live-canary-executor:" + produced["executor"]["id"]
    )
    # 未帶 --target-file 的產物沒有綁定 target，不能拿來核銷任何需求。
    assert validator(_live_receipt({**produced, "target": None})) is False


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda t: {k: v for k, v in t.items() if k != "service"}, id="missing-field"),
        pytest.param(lambda t: {**t, "extra": "value"}, id="extra-field"),
        pytest.param(lambda t: {**t, "candidate_sha": "not-a-sha"}, id="invalid-sha"),
        pytest.param(lambda t: [t], id="not-an-object"),
    ],
)
def test_live_canary_rejects_invalid_target_file(tmp_path, monkeypatch, capsys, mutate):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)
    target_file = tmp_path / "target.json"
    target_file.write_text(json.dumps(mutate(dict(_CANARY_TARGET))), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "task-memory", "canary", "--repo", "acme/demo", "--repo", "other/demo",
                "--target-file", str(target_file),
            ]
        )

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "unrecognized arguments" not in error
    assert "--target-file" in error
    assert not log.exists()


# ---------------------------------------------------------------------------
# #857 G857-3：relay overwrite／schema 破壞偵測、task kind 輪替、KPI 由觀測計算
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fault", ["mode-overwrite", "task-id-overwrite"])
def test_live_canary_flags_provider_relay_overwrite_as_blocker(
    tmp_path, monkeypatch, capsys, fault
):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log, FAKE_HIPPO_PAYLOAD_MODE=fault)

    result, report = _run_canary_cli(capsys, "--runs", "5")

    assert result == 1
    assert report["passed"] is False
    assert "relay-overwrite" in report["blockers"]
    assert report["relay_checks"]["provider_identity_overwrites"] == 30
    assert report["relay_checks"]["passed"] is False
    assert report["schema_checks"]["schema_breaks"] == 0


def test_live_canary_detects_executor_prompt_relay_overwrite(tmp_path, monkeypatch, capsys):
    """inline 交付必須以附加方式進入 Cortex 轉交給 executor 的 prompt；canary
    以 Manager 正式 composer 組 prompt 並核對原 prompt 逐字保留。"""
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)

    def overwriting(prompt, prepared):
        # 回歸模擬：task-memory 區塊取代原 prompt，而不是附加在其後。
        rows = [f"[{item['note_id']}] {item['text']}" for item in prepared.inline_context]
        return "\n".join(rows) if rows else prompt

    monkeypatch.setattr(manager, "_append_task_memory_inline", overwriting)

    result, report = _run_canary_cli(capsys, "--runs", "5")

    assert result == 1
    assert report["passed"] is False
    assert "relay-overwrite" in report["blockers"]
    assert report["relay_checks"]["executor_prompts_checked"] == 10
    assert report["relay_checks"]["executor_prompt_overwrites"] == 10
    # 被覆寫的 prompt 不算完成 inline 交付；其他 path 不受影響。
    assert report["paths"]["context-delivered"]["successes"] == 0
    assert report["paths"]["note-fetch"]["passed"] is True
    assert report["paths"]["snapshot-ready"]["passed"] is True


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        ("FAKE_HIPPO_EXIT_PROVIDE", "14"),
        ("FAKE_HIPPO_PAYLOAD_MODE", "schema-major-2"),
        ("FAKE_HIPPO_PAYLOAD_MODE", "not-json"),
    ],
)
def test_live_canary_flags_legacy_output_schema_break_as_blocker(
    tmp_path, monkeypatch, capsys, env_name, env_value
):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log, **{env_name: env_value})

    result, report = _run_canary_cli(capsys, "--runs", "5")

    assert result == 1
    assert report["passed"] is False
    assert "legacy-output-schema-break" in report["blockers"]
    assert report["schema_checks"] == {
        "provider_payloads_checked": 30,
        "schema_breaks": 30,
        "passed": False,
    }
    assert report["relay_checks"]["provider_identity_overwrites"] == 0


def test_live_canary_rotates_task_kind_across_dispatchable_card_phases(
    tmp_path, monkeypatch, capsys
):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)

    result, report = _run_canary_cli(capsys, "--runs", "5")

    assert result == 0
    assert report["task_kinds"] == ["build", "verify", "review"]
    # 輪替集合與 Manager 以 task memory 派工、有 applied 回報管道的卡片 phase 一致。
    assert set(task_memory_canary._TASK_KINDS) == manager.TASK_MEMORY_APPLIED_PHASES
    provided: dict[tuple[str, str], set[str]] = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        if call["operation"] != "provide":
            continue
        request = call["request"]
        scope = request["delivery"]["host_scope"]
        if "hippo" not in scope["allowed_evidence_sources"] or request["project"] != scope["repo"]:
            continue  # permission／cross-scope 負例
        provided.setdefault((scope["repo"], request["delivery"]["mode"]), set()).add(
            scope["task_kind"]
        )
    assert set(provided) == {
        (repo, mode)
        for repo in ("acme/demo", "other/demo")
        for mode in ("inline", "snapshot", "note_fetch")
    }
    assert all(kinds == {"build", "verify", "review"} for kinds in provided.values())
    for row in report["paths"].values():
        assert set(row["task_kinds"]) == {"build", "verify", "review"}
        assert all(kind_row["successes"] >= 1 for kind_row in row["task_kinds"].values())
    assert report["task_kind_coverage"]["passed"] is True


def test_live_canary_requires_more_than_one_task_kind(tmp_path, monkeypatch, capsys):
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)
    monkeypatch.setattr(task_memory_canary, "_TASK_KINDS", ("build",))

    result, report = _run_canary_cli(capsys, "--runs", "5")

    assert result == 1
    assert report["passed"] is False
    assert report["task_kind_coverage"]["passed"] is False
    assert all(row["passed"] is True for row in report["paths"].values())


def test_live_canary_legacy_strict_kpi_flag_reflects_observed_receipts(
    tmp_path, monkeypatch, capsys
):
    """`legacy_strict_kpi_mutated` 由實際 receipt 的 counts_as_read 計算：inline
    交付若被標成 Read，canary 必須判為 blocker。"""
    script, log = _fake_hippo(tmp_path)
    _client(monkeypatch, script, log)
    original = TaskMemoryAdapter.confirm_context_delivered

    def regressed(self, prepared, **kwargs):
        return tuple(
            dict(event, counts_as_read=True) for event in original(self, prepared, **kwargs)
        )

    monkeypatch.setattr(TaskMemoryAdapter, "confirm_context_delivered", regressed)

    result, report = _run_canary_cli(capsys, "--runs", "5")

    assert result == 1
    assert report["passed"] is False
    assert report["legacy_strict_kpi_mutated"] is True
    assert report["strict_read_violations"] == 10
    assert "legacy-strict-kpi-mutation" in report["blockers"]


def test_task_memory_canary_appears_in_umbrella_help(capsys):
    assert cli.main(["--help"]) == 0
    assert "task-memory" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli.main(["task-memory", "canary", "--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "--repo" in output
    assert "--runs" in output
    assert "--evidence-path" in output
