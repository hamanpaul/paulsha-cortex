from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager
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
    assert report["repositories"] == ["acme/demo", "other/demo"]
    assert all(report["paths"][mode]["successes"] >= 5 for mode in ("inline", "snapshot", "note_fetch"))
    assert all(report["paths"][mode]["successful_provides"] == 10 for mode in report["paths"])
    assert all(
        row["successful_provides"] == 5
        for mode in report["paths"].values()
        for row in mode["per_repo"].values()
    )
    assert all(report["paths"][mode]["eligible_authorized_success_rate"] == 1.0 for mode in report["paths"])
    assert report["content_retrieval"]["paths"] == ["note_fetch", "snapshot"]
    assert report["content_retrieval"]["eligible_authorized_attempts"] == 20
    assert report["content_retrieval"]["successes"] == 20
    assert report["content_retrieval"]["success_rate"] == 1.0
    assert report["content_retrieval"]["passed"] is True
    assert report["paths"]["note_fetch"]["success_semantics"] == (
        "content-returned after note content hash match"
    )
    assert report["paths"]["snapshot"]["success_semantics"] == (
        "content-returned after snapshot read-back and hash match; snapshot-ready is not a read"
    )
    assert report["paths"]["inline"]["metric_kind"] == "delivery"
    assert report["paths"]["inline"]["eligible_authorized_delivery_rate"] == 1.0
    assert report["paths"]["inline"]["counts_as_read"] is False
    assert report["inline_delivery"]["delivery_rate"] == 1.0
    assert report["inline_delivery"]["counts_as_read"] is False
    assert report["permission_negative_control"]["passed"] is True
    assert report["scope_checks"]["scope_leaks"] == 0
    assert report["cross_project_checks"]["passed"] is True
    assert report["legacy_strict_kpi_mutated"] is False
    assert _BODY not in captured.out
    assert _BODY not in evidence.read_text()
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
