from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.control import constants, contract
from paulsha_cortex.coordinator import autonomy as coordinator_autonomy, completion, manager_daemon, verification
from paulsha_cortex.coordinator.registry import JobRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeRegistry:
    def __init__(self, jobs: list[dict] | None = None) -> None:
        self._jobs = list(jobs or [])
        self._seq = len(self._jobs)
        self._slices: list[dict] = []

    def list_jobs(self) -> list[dict]:
        return [dict(job) for job in self._jobs]

    def create_job(
        self,
        *,
        task: str,
        persona: str,
        branch: str,
        pane: str,
        worktree: str,
        dispatch_head: str | None = None,
        executor: str | None = None,
        session_name: str | None = None,
        pid: int | None = None,
        log_path: str | None = None,
        exit_code: int | None = None,
        kind: str = "build",
        model_id: str | None = None,
        independence_domain: str | None = None,
        subject_head: str | None = None,
        spec_hash: str | None = None,
        plan_hash: str | None = None,
        verification_hash: str | None = None,
    ) -> dict:
        self._seq += 1
        job = {
            "job_id": f"{task}-{self._seq}",
            "task": task,
            "persona": persona,
            "kind": kind,
            "branch": branch,
            "pane": pane,
            "worktree": worktree,
            "status": "dispatched",
            "dispatch_head": dispatch_head,
            "executor": executor,
            "model_id": model_id,
            "independence_domain": independence_domain,
            "session_name": session_name,
            "pid": pid,
            "log_path": log_path,
            "exit_code": exit_code,
            "subject_head": subject_head,
            "spec_hash": spec_hash,
            "plan_hash": plan_hash,
            "verification_hash": verification_hash,
        }
        self._jobs.append(job)
        return dict(job)

    def attach_launch_handle(
        self,
        job_id: str,
        *,
        executor: str | None = None,
        model_id: str | None = None,
        session_name: str | None = None,
        pid: int | None = None,
        log_path: str | None = None,
    ) -> dict:
        for job in self._jobs:
            if job["job_id"] == job_id:
                job["executor"] = executor
                if model_id is not None:
                    job["model_id"] = model_id
                job["session_name"] = session_name
                job["pid"] = pid
                job["log_path"] = log_path
                return dict(job)
        raise KeyError(job_id)

    def update_status(self, job_id: str, status: str) -> dict:
        for job in self._jobs:
            if job["job_id"] == job_id:
                job["status"] = status
                return dict(job)
        raise KeyError(job_id)

    def create_slice(
        self,
        *,
        slice_id: str,
        spec_path: str,
        spec_hash: str,
        plan_path: str,
        plan_hash: str,
        target_branch: str,
        target_remote: str = "origin",
        verification_hash: str | None = None,
        verification: dict | None = None,
        dispatch_base: str | None = None,
        builder_job_id: str | None = None,
        reviewer_job_id: str | None = None,
        candidate: str | None = None,
    ) -> dict:
        row = {
            "slice_id": slice_id,
            "spec": {"path": spec_path, "hash": spec_hash},
            "plan": {"path": plan_path, "hash": plan_hash},
            "target_branch": target_branch,
            "target_remote": target_remote,
            "verification": {"hash": verification_hash or ("0" * 64), "contract": verification},
            "dispatch_base": dispatch_base,
            "builder_job_id": builder_job_id,
            "reviewer_job_id": reviewer_job_id,
            "candidate": candidate,
            "state": "pending",
            "gate_state": "pending",
            "current_evidence_refs": [],
            "current_evaluation_refs": [],
            "evidence_history": [],
            "evaluation_history": [],
            "actions": [],
            "created_at": "T0",
            "updated_at": "T0",
        }
        self._slices.append(row)
        return dict(row)

    def update_slice(self, slice_id: str, **updates) -> dict:
        for row in self._slices:
            if row["slice_id"] == slice_id:
                for key, value in updates.items():
                    if value is None:
                        continue
                    if key == "verification_hash":
                        row["verification"]["hash"] = value
                    else:
                        row[key] = value
                return dict(row)
        raise KeyError(slice_id)

    def record_action(self, slice_id: str, **kwargs) -> dict:
        for row in self._slices:
            if row["slice_id"] == slice_id:
                row["actions"].append(dict(kwargs))
                return dict(row)
        raise KeyError(slice_id)

    def get_slice(self, slice_id: str) -> dict:
        for row in self._slices:
            if row["slice_id"] == slice_id:
                return dict(row)
        raise KeyError(slice_id)


class FakeDispatcher:
    def __init__(self, registry: FakeRegistry, worktree_creator=None, git_runner=None) -> None:
        self._registry = registry
        self._worktree_creator = worktree_creator
        self._git_runner = git_runner or _default_git_runner


def _git_ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _default_git_runner(args: list[str]):
    if not args:
        return ""
    if args[0] == "rev-parse":
        return "f" * 40
    if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
        return ""
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        return "f" * 40
    if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base" and args[3] == "--is-ancestor":
        return ""
    return ""


class FakeWorktreeCreator:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self.calls: list[str] = []

    def create(self, branch: str) -> str:
        self.calls.append(branch)
        return str(self._base_dir / branch.replace("/", "__"))


class RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str):
        from paulsha_cortex.coordinator.launcher import LaunchHandle

        self.calls.append(
            {
                "slice_id": slice_id,
                "prompt": prompt,
                "worktree": worktree,
                "log_dir": log_dir,
            }
        )
        return LaunchHandle(
            executor="copilot",
            model_id=None,
            session_name=slice_id,
            pid=1000 + len(self.calls),
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _write_request(req_id: str, **overrides) -> dict:
    request = {
        "schema_version": constants.SCHEMA_VERSION,
        "req_id": req_id,
        "type": "tick",
        "args": {"executor": "copilot"},
        "requested_by": "cockpit",
        "created_at": "2026-07-03T09:00:00+00:00",
    }
    request.update(overrides)
    contract.atomic_write_json(constants.requests_dir() / f"{req_id}.json", request)
    return request


def _seed_dependency_completion(
    *,
    root: Path,
    handoff_dir: Path,
    slice_id: str,
) -> None:
    candidate = "b" * 40
    target_sha = "c" * 40
    verification_ref = verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "candidate": candidate,
            "status": "verified",
            "summary": "verification-succeeded",
            "details": {"ok": True},
        },
        coordinator_root=root,
    )
    record = completion.write_completion_record(
        {
            "schema_version": completion.COMPLETION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "spec_hash": "0" * 64,
            "plan_hash": "1" * 64,
            "verification_hash": "2" * 64,
            "builder_job_id": f"{slice_id}-builder-1",
            "reviewer_job_id": None,
            "dispatch_base": "a" * 40,
            "candidate": candidate,
            "target_branch": "main",
            "target_remote": "origin",
            "target_ref": "refs/remotes/origin/main",
            "target_ref_sha": target_sha,
            "verification_evidence_path": verification_ref["path"],
            "verification_evidence_hash": verification_ref["hash"],
            "review_policy": "not-required",
            "docs_class": "informational",
            "review_evaluation_path": None,
            "review_evaluation_hash": None,
            "completed_at": "2026-07-12T00:00:00+00:00",
        },
        coordinator_root=root,
    )
    contract.atomic_write_json(
        handoff_dir / f"{slice_id}.json",
        {
            "slice_id": slice_id,
            "job_id": f"{slice_id}-builder-1",
            "gate_status": "passed",
            "completion": "exited",
            "exit_code": 0,
            "branch": f"feature/{slice_id}",
            "gate_reason": "candidate-merged",
            "gate_verdict": None,
            "verification_evidence_path": verification_ref["path"],
            "verification_evidence_hash": verification_ref["hash"],
            "review_evaluation_path": None,
            "review_evaluation_hash": None,
            "completion_record_path": record["path"],
            "completion_record_hash": record["hash"],
            "slice_state": "completed",
            "spec_hash": "0" * 64,
            "plan_hash": "1" * 64,
            "verification_hash": "2" * 64,
            "completed_at": "2026-07-12T00:00:00+00:00",
        },
    )


def _run_dispatch_request(
    monkeypatch,
    tmp_path,
    *,
    args: dict,
    metas: list[dict],
    jobs: list[dict] | None = None,
    requested_by: str = "cockpit",
):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    req_id = "20260703T090007Z-44444444444444444444444444444444"
    _write_request(req_id, type="dispatch", args=args, requested_by=requested_by)
    specs_dir = tmp_path / "specs"
    _materialize_dispatch_metas(tmp_path, metas)
    registry = FakeRegistry(jobs)
    worktree_creator = FakeWorktreeCreator(tmp_path / "worktrees")
    dispatcher = FakeDispatcher(registry, worktree_creator=worktree_creator, git_runner=_default_git_runner)
    launcher = RecordingLauncher()
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(specs_dir),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
        scan_specs_fn=lambda specs_dir: coordinator_autonomy.scan_specs(specs_dir),
    )
    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )
    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    return done, launcher, registry, worktree_creator


def _run_complete_request(
    monkeypatch,
    tmp_path,
    *,
    args: dict,
    metas: list[dict] | None = None,
    jobs: list[dict] | None = None,
):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090009Z-66666666666666666666666666666666"
    _write_request(req_id, type="complete", args=args)
    registry = FakeRegistry(jobs)
    dispatcher = FakeDispatcher(
        registry,
        worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"),
        git_runner=_default_git_runner,
    )
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=RecordingLauncher(),
        scan_specs_fn=lambda specs_dir: metas or [],
    )
    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )
    return contract.read_json(constants.done_dir() / f"{req_id}.json")


def _write_v1_spec(specs_dir: Path, slice_id: str, *, dispatch: str = "auto") -> Path:
    spec_path = specs_dir / f"{slice_id}.md"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path = specs_dir.parent / "docs" / "superpowers" / "plans" / f"{slice_id}.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(f"# {slice_id}\n", encoding="utf-8")
    spec_path.write_text(
        "---\n"
        f"dispatch: {dispatch}\n"
        f"slice_id: {slice_id}\n"
        f"plan: {plan_path.relative_to(specs_dir.parent).as_posix()}\n"
        "target_branch: main\n"
        "verification:\n"
        "  docs_class: code\n"
        "  required_artifacts: []\n"
        "  checks:\n"
        "    - kind: persona-scope\n"
        "    - kind: command\n"
        "      name: policy\n"
        "      argv: [python3, -m, pytest, -q]\n"
        "      cwd: .\n"
        "      timeout_seconds: 30\n"
        "  tests: []\n"
        "  full_suite:\n"
        "    argv: [python3, -m, pytest, -q]\n"
        "    cwd: .\n"
        "    timeout_seconds: 60\n"
        "    baseline: no-regression\n"
        "---\n",
        encoding="utf-8",
    )
    return spec_path


def _materialize_dispatch_metas(repo_root: Path, metas: list[dict]) -> None:
    specs_dir = repo_root / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    for meta in metas:
        slice_id = meta["slice_id"]
        dispatch = meta.get("dispatch", "hold")
        plan_rel = meta.get("plan")
        lines = [f"dispatch: {dispatch}", f"slice_id: {slice_id}"]
        if isinstance(plan_rel, str):
            lines.append(f"plan: {plan_rel}")
            plan_path = repo_root / plan_rel
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            if not plan_path.exists():
                plan_path.write_text(f"# {slice_id}\n", encoding="utf-8")
        depends_on = meta.get("depends_on", [])
        if depends_on:
            deps = ", ".join(depends_on)
            lines.append(f"depends_on: [{deps}]")
        target_branch = meta.get("target_branch")
        verification = meta.get("verification")
        if dispatch == "auto" and not isinstance(target_branch, str):
            target_branch = "main"
        if dispatch == "auto" and not isinstance(verification, dict):
            verification = {
                "docs_class": "code",
                "required_artifacts": [],
                "checks": [
                    {"kind": "persona-scope"},
                    {
                        "kind": "command",
                        "name": "policy",
                        "argv": ["python3", "-m", "pytest", "-q"],
                        "cwd": ".",
                        "timeout_seconds": 30,
                    },
                ],
                "tests": [],
                "full_suite": {
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 60,
                    "baseline": "no-regression",
                },
            }
        if isinstance(target_branch, str):
            lines.append(f"target_branch: {target_branch}")
        if isinstance(verification, dict):
            lines.extend(
                [
                    "verification:",
                    f"  docs_class: {verification.get('docs_class', 'code')}",
                    "  required_artifacts: []",
                    "  checks:",
                ]
            )
            for check in verification.get("checks", []):
                if check["kind"] == "persona-scope":
                    lines.append("    - kind: persona-scope")
                else:
                    lines.extend(
                        [
                            "    - kind: command",
                            f"      name: {check['name']}",
                            f"      argv: [{', '.join(check['argv'])}]",
                            f"      cwd: {check.get('cwd', '.')}",
                            f"      timeout_seconds: {check.get('timeout_seconds', 30)}",
                        ]
                    )
            lines.append("  tests: []")
            full_suite = verification.get("full_suite", {})
            lines.extend(
                [
                    "  full_suite:",
                    f"    argv: [{', '.join(full_suite.get('argv', ['python3', '-m', 'pytest', '-q']))}]",
                    f"    cwd: {full_suite.get('cwd', '.')}",
                    f"    timeout_seconds: {full_suite.get('timeout_seconds', 60)}",
                    f"    baseline: {full_suite.get('baseline', 'no-regression')}",
                ]
            )
        spec_path = specs_dir / f"{slice_id}.md"
        spec_path.write_text("---\n" + "\n".join(lines) + "\n---\n", encoding="utf-8")


def test_run_loop_drains_tick_request_writes_done_and_updates_status(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    registry = FakeRegistry([{"job_id": "job-1", "task": "slice-b", "status": "running"}])
    request = _write_request("20260703T090000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    seen: list[str] = []

    def request_executor(req: dict) -> dict:
        seen.append(req["req_id"])
        return {"dispatched": ["slice-a"], "completed": [], "errors": []}

    status_provider = manager_daemon.build_status_provider(
        registry=registry,
        ready_provider=lambda: ["slice-a"],
        recent_done_provider=lambda: [
            {"slice_id": "slice-z", "gate_status": "passed", "at": "2026-07-03T08:59:00+00:00"}
        ],
    )

    started = manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=status_provider,
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=4321,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{request['req_id']}.json")
    status = contract.read_json(constants.status_path())

    assert started is True
    assert seen == [request["req_id"]]
    assert list(constants.requests_dir().glob("*.json")) == []
    assert done["status"] == "ok"
    assert done["result"]["dispatched"] == ["slice-a"]
    assert status["ready"] == ["slice-a"]
    assert status["in_flight"] == [{"job_id": "job-1", "slice_id": "slice-b", "state": "running"}]
    assert status["recent_done"][0]["slice_id"] == "slice-z"
    assert status["daemon"]["pid"] == 4321
    assert status["daemon"]["last_tick_at"] == "2026-07-03T09:05:00+00:00"


def test_built_executor_and_status_provider_use_injected_dispatcher_and_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    registry = FakeRegistry([{"job_id": "job-1", "task": "slice-b", "status": "running"}])
    dispatcher = FakeDispatcher(registry)
    launcher = object()
    request = _write_request("20260703T090000Z-ffffffffffffffffffffffffffffffff")
    calls: list[dict] = []

    def fake_run_tick(
        dispatcher_arg,
        *,
        metas,
        launcher,
        persona,
        is_satisfied,
        handoff_dir,
        require_idle,
        max_load,
        reaper,
    ) -> dict:
        calls.append(
            {
                "dispatcher": dispatcher_arg,
                "metas": metas,
                "launcher": launcher,
                "persona": persona,
                "handoff_dir": handoff_dir,
                "require_idle": require_idle,
                "max_load": max_load,
                "reaper": reaper,
                "predicate": is_satisfied,
            }
        )
        return {
            "dispatch_skipped": False,
            "dispatched": ["slice-a"],
            "completed": [],
            "errors": [],
            "reaped": None,
        }

    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir="docs/superpowers/specs",
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
        scan_specs_fn=lambda specs_dir: [{"slice_id": "slice-a", "dispatch": "auto", "plan": "p.md", "depends_on": []}],
        run_tick_fn=fake_run_tick,
    )
    status_provider = manager_daemon.build_status_provider(
        registry=registry,
        ready_provider=lambda: ["slice-a"],
        recent_done_provider=lambda: [{"slice_id": "slice-z", "gate_status": "passed", "at": "2026-07-03T08:59:00+00:00"}],
    )

    started = manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=status_provider,
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=4321,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{request['req_id']}.json")
    status = contract.read_json(constants.status_path())

    assert started is True
    assert len(calls) == 1
    assert calls[0]["dispatcher"] is dispatcher
    assert calls[0]["launcher"] is launcher
    assert calls[0]["persona"] == manager_daemon.DEFAULT_PERSONA
    assert calls[0]["handoff_dir"] == str(tmp_path / "handoff")
    assert calls[0]["require_idle"] is False
    assert calls[0]["max_load"] == manager_daemon.DEFAULT_MAX_LOAD
    assert calls[0]["reaper"] is None
    assert callable(calls[0]["predicate"])
    assert done["status"] == "ok"
    assert done["result"]["dispatched"] == ["slice-a"]
    assert status["in_flight"] == [{"job_id": "job-1", "slice_id": "slice-b", "state": "running"}]


def test_periodic_tick_runner_does_not_wire_reaper_and_uses_default_executor(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    dispatcher = FakeDispatcher(FakeRegistry())
    launcher = object()
    calls: list[dict[str, object]] = []

    def fake_run_tick(
        dispatcher_arg,
        *,
        metas,
        launcher,
        persona,
        is_satisfied,
        handoff_dir,
        require_idle,
        max_load,
        reaper,
    ) -> dict:
        calls.append(
            {
                "dispatcher": dispatcher_arg,
                "launcher": launcher,
                "persona": persona,
                "handoff_dir": handoff_dir,
                "require_idle": require_idle,
                "max_load": max_load,
                "reaper": reaper,
            }
        )
        return {"dispatch_skipped": False, "dispatched": [], "completed": [], "errors": [], "reaped": None}

    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
        run_tick_fn=fake_run_tick,
        scan_specs_fn=lambda specs_dir: [],
    )

    runner()

    assert len(calls) == 1
    assert calls[0]["dispatcher"] is dispatcher
    assert calls[0]["launcher"] is launcher
    assert calls[0]["persona"] == manager_daemon.DEFAULT_PERSONA
    assert calls[0]["handoff_dir"] == str(tmp_path / "handoff")
    assert calls[0]["require_idle"] is True
    assert calls[0]["max_load"] == manager_daemon.DEFAULT_MAX_LOAD
    assert calls[0]["reaper"] is None


def test_duplicate_req_id_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090001Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    existing_done = contract.build_done(req_id=req_id, status="ok", result={"dispatched": ["existing"]})
    contract.atomic_write_json(constants.done_dir() / f"{req_id}.json", existing_done)
    _write_request(req_id)
    calls: list[str] = []

    started = manager_daemon.run_loop(
        request_executor=lambda req: calls.append(req["req_id"]) or {"dispatched": ["new"]},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    assert started is True
    assert calls == []
    assert contract.read_json(constants.done_dir() / f"{req_id}.json") == existing_done
    assert list(constants.requests_dir().glob("*.json")) == []


def test_invalid_schema_request_writes_error_done(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090002Z-cccccccccccccccccccccccccccccccc"
    _write_request(req_id, schema_version=999)

    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    assert done["status"] == "error"
    assert "schema_version" in done["error"]


def test_missing_req_id_request_writes_error_done(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090002Z-req-id-from-filename"
    contract.atomic_write_json(
        constants.requests_dir() / f"{req_id}.json",
        {
            "schema_version": constants.SCHEMA_VERSION,
            "type": "tick",
            "args": {"executor": "copilot"},
            "requested_by": "cockpit",
            "created_at": "2026-07-03T09:00:00+00:00",
        },
    )

    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    assert done["status"] == "error"
    assert "req_id" in done["error"]


def test_failing_request_is_isolated_and_requests_stay_time_ordered(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    older = _write_request("20260703T090003Z-dddddddddddddddddddddddddddddddd")
    newer = _write_request("20260703T090004Z-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
    seen: list[str] = []

    def request_executor(req: dict) -> dict:
        seen.append(req["req_id"])
        if req["req_id"] == older["req_id"]:
            raise RuntimeError("boom")
        return {"dispatched": ["slice-b"], "completed": [], "errors": []}

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    older_done = contract.read_json(constants.done_dir() / f"{older['req_id']}.json")
    newer_done = contract.read_json(constants.done_dir() / f"{newer['req_id']}.json")

    assert seen == [older["req_id"], newer["req_id"]]
    assert older_done["status"] == "error"
    assert "boom" in older_done["error"]
    assert newer_done["status"] == "ok"


def test_same_second_requests_follow_file_time_order_not_uuid_order(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    first = _write_request("20260703T090004Z-ffffffffffffffffffffffffffffffff")
    second = _write_request("20260703T090004Z-00000000000000000000000000000000")
    first_path = constants.requests_dir() / f"{first['req_id']}.json"
    second_path = constants.requests_dir() / f"{second['req_id']}.json"
    os.utime(first_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second_path, ns=(2_000_000_000, 2_000_000_000))
    seen: list[str] = []

    manager_daemon.run_loop(
        request_executor=lambda req: seen.append(req["req_id"]) or {"dispatched": [], "completed": [], "errors": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    assert seen == [first["req_id"], second["req_id"]]


def test_missing_request_file_during_sort_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    vanished = _write_request("20260703T090004Z-11111111111111111111111111111111")
    survivor = _write_request("20260703T090004Z-22222222222222222222222222222222")
    vanished_path = constants.requests_dir() / f"{vanished['req_id']}.json"
    survivor_path = constants.requests_dir() / f"{survivor['req_id']}.json"
    seen: list[str] = []
    real_path_stat = Path.stat
    removed = False

    def flaky_path_stat(self: Path, *args, **kwargs):
        nonlocal removed
        if not removed and self == vanished_path:
            removed = True
            self.unlink(missing_ok=True)
        return real_path_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_path_stat)

    started = manager_daemon.run_loop(
        request_executor=lambda req: seen.append(req["req_id"]) or {"dispatched": ["slice-a"], "completed": [], "errors": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    assert started is True
    assert seen == [survivor["req_id"]]
    assert not vanished_path.exists()
    assert not survivor_path.exists()


def test_executor_filenotfound_writes_error_done(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    request = _write_request("20260703T090004Z-33333333333333333333333333333333")

    started = manager_daemon.run_loop(
        request_executor=lambda req: (_ for _ in ()).throw(FileNotFoundError("missing spec")),
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{request['req_id']}.json")

    assert started is True
    assert done["status"] == "error"
    assert "FileNotFoundError" in done["error"]


def test_periodic_tick_is_idle_gated(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    periodic_calls: list[str] = []

    def periodic_tick_runner() -> dict:
        periodic_calls.append("called")
        return {"dispatch_skipped": "not-idle", "dispatched": [], "completed": [], "errors": []}

    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=periodic_tick_runner,
        poll_interval=0.0,
        tick_interval=0.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    status = contract.read_json(constants.status_path())
    assert periodic_calls == ["called"]
    assert status["daemon"]["idle"] is False
    assert status["daemon"]["last_tick_at"] is None


def test_request_tick_resets_periodic_deadline(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    _write_request("20260703T090004Z-11111111111111111111111111111111")
    periodic_calls: list[str] = []
    monotonic_points = iter((0.0, 5.0, 5.0))

    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatch_skipped": False, "dispatched": ["slice-a"], "completed": [], "errors": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: periodic_calls.append("called") or {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=10.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: next(monotonic_points),
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    assert periodic_calls == []


def test_idle_skipped_periodic_tick_does_not_reset_deadline(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    periodic_calls: list[str] = []
    monotonic_points = iter((0.0, 5.0, 6.0))

    manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: periodic_calls.append("called") or {"dispatch_skipped": "not-idle"},
        poll_interval=0.0,
        tick_interval=5.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: next(monotonic_points),
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=2,
    )

    assert periodic_calls == ["called", "called"]


def test_status_provider_error_is_logged_and_loop_continues(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    errors: list[str] = []
    provider_calls = 0

    def status_provider() -> dict:
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            raise RuntimeError("status snapshot failed")
        return {"ready": ["slice-a"], "in_flight": [], "recent_done": []}

    monkeypatch.setattr(manager_daemon, "_log_error", lambda exc: errors.append(str(exc)))

    started = manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=status_provider,
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=2,
    )

    status = contract.read_json(constants.status_path())

    assert started is True
    assert provider_calls == 2
    assert errors == ["status snapshot failed"]
    assert status["ready"] == ["slice-a"]


def test_status_write_error_is_logged_and_loop_continues(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    errors: list[str] = []
    real_atomic_write_json = contract.atomic_write_json
    status_write_failures = 0

    def flaky_atomic_write_json(path, payload):
        nonlocal status_write_failures
        if path == constants.status_path() and status_write_failures == 0:
            status_write_failures += 1
            raise OSError("status write failed")
        return real_atomic_write_json(path, payload)

    monkeypatch.setattr(contract, "atomic_write_json", flaky_atomic_write_json)
    monkeypatch.setattr(manager_daemon, "_log_error", lambda exc: errors.append(str(exc)))

    started = manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {"ready": ["slice-a"], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=2,
    )

    status = contract.read_json(constants.status_path())

    assert started is True
    assert status_write_failures == 1
    assert errors == ["status write failed"]
    assert status["ready"] == ["slice-a"]


def test_done_write_error_is_logged_and_preserves_request_order(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    older = _write_request("20260703T090005Z-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    newer = _write_request("20260703T090005Z-cccccccccccccccccccccccccccccccc")
    older_path = constants.requests_dir() / f"{older['req_id']}.json"
    newer_path = constants.requests_dir() / f"{newer['req_id']}.json"
    errors: list[str] = []
    seen: list[str] = []
    real_persist_done = manager_daemon._persist_done
    persist_failures = 0

    def flaky_persist_done(payload: dict) -> dict:
        nonlocal persist_failures
        if persist_failures == 0:
            persist_failures += 1
            raise OSError("done write failed")
        return real_persist_done(payload)

    monkeypatch.setattr(manager_daemon, "_persist_done", flaky_persist_done)
    monkeypatch.setattr(manager_daemon, "_log_error", lambda exc: errors.append(str(exc)))

    started = manager_daemon.run_loop(
        request_executor=lambda req: seen.append(req["req_id"]) or {"dispatched": ["slice-a"], "completed": [], "errors": []},
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    status = contract.read_json(constants.status_path())

    assert started is True
    assert persist_failures == 1
    assert seen == [older["req_id"]]
    assert errors == ["done write failed"]
    assert older_path.exists()
    assert newer_path.exists()
    assert contract.read_json(constants.done_dir() / f"{older['req_id']}.json") is None
    assert status["ready"] == []


def test_runtime_status_provider_lists_recent_done_by_completion_time(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "slice-a.json").write_text(
        '{"slice_id":"slice-a","gate_status":"passed","completed_at":"2026-07-03T09:01:00+00:00"}',
        encoding="utf-8",
    )
    (handoff_dir / "slice-b.json").write_text(
        '{"slice_id":"slice-b","gate_status":"passed","completed_at":"2026-07-03T09:03:00+00:00"}',
        encoding="utf-8",
    )
    registry = FakeRegistry([{"job_id": "job-1", "task": "slice-x", "status": "running"}])
    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(handoff_dir),
        scan_specs_fn=lambda specs_dir: [{"slice_id": "slice-ready", "dispatch": "auto", "plan": "p.md", "depends_on": []}],
        ready_units_fn=lambda metas, predicate: metas,
    )

    status = provider()

    assert status["ready"] == ["slice-ready"]
    assert status["in_flight"] == [{"job_id": "job-1", "slice_id": "slice-x", "state": "running"}]
    assert [entry["slice_id"] for entry in status["recent_done"]] == ["slice-b", "slice-a"]


def test_runtime_status_provider_classifies_held_units(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    provider = manager_daemon.build_runtime_status_provider(
        registry=FakeRegistry(),
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(handoff_dir),
        scan_specs_fn=lambda specs_dir: [
            {"slice_id": "slice-ready", "dispatch": "auto", "plan": "ready.md", "depends_on": []},
            {"slice_id": "slice-no-plan", "dispatch": "auto", "plan": None, "depends_on": []},
            {"slice_id": "slice-held", "dispatch": "hold", "plan": "held.md", "depends_on": []},
            {"slice_id": "slice-blocked", "dispatch": "auto", "plan": "blocked.md", "depends_on": ["slice-dep"]},
        ],
    )

    status = provider()

    assert status["ready"] == ["slice-ready"]
    assert status["held"] == [
        {"slice_id": "slice-no-plan", "reasons": ["no-plan"]},
        {"slice_id": "slice-held", "reasons": ["dispatch-hold"]},
        {"slice_id": "slice-blocked", "reasons": ["deps-unsatisfied:slice-dep"]},
    ]
    assert {item["slice_id"] for item in status["held"]} == {"slice-no-plan", "slice-held", "slice-blocked"}


def test_run_loop_persists_held_status_from_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))

    started = manager_daemon.run_loop(
        request_executor=lambda req: {"dispatched": []},
        status_provider=lambda: {
            "ready": [],
            "held": [{"slice_id": "slice-held", "reasons": ["dispatch-hold"]}],
            "in_flight": [],
            "recent_done": [],
        },
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    status = contract.read_json(constants.status_path())

    assert started is True
    assert status["held"] == [{"slice_id": "slice-held", "reasons": ["dispatch-hold"]}]


def test_allow_unsafe_fanout_over_one_ready_slice_writes_error_done(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090006Z-22222222222222222222222222222222"
    _write_request(req_id, type="fanout", args={"allow_unsafe": True})
    dispatcher = FakeDispatcher(FakeRegistry())
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda specs_dir: [
            {"slice_id": "slice-a", "dispatch": "auto", "plan": "a.md", "depends_on": []},
            {"slice_id": "slice-b", "dispatch": "auto", "plan": "b.md", "depends_on": []},
        ],
        dispatch_ready_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch_ready should not run")),
    )

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    assert done["status"] == "error"
    assert "--allow-unsafe" in done["error"]


def test_dispatch_unknown_slice(monkeypatch, tmp_path):
    done, launcher, _, _ = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-missing"},
        metas=[{"slice_id": "slice-a", "dispatch": "auto", "plan": "a.md", "depends_on": []}],
    )

    assert done["status"] == "error"
    assert done["error"].endswith("unknown-slice")
    assert launcher.calls == []


def test_complete_request_runs_complete_tick(monkeypatch, tmp_path):
    jobs = [
        {
            "job_id": "slice-a-1",
            "task": "slice-a",
            "persona": "builder",
            "branch": "feature/slice-a",
            "pane": "",
            "worktree": "/wt/slice-a",
            "status": "exited",
            "dispatch_head": None,
            "executor": "copilot",
            "session_name": "slice-a",
            "pid": 1001,
            "log_path": str(tmp_path / "logs" / "slice-a.jsonl"),
            "exit_code": 0,
        }
    ]
    done = _run_complete_request(
        monkeypatch,
        tmp_path,
        args={"handoff_dir": str(tmp_path / "handoff"), "specs_dir": str(tmp_path / "specs")},
        metas=[{"slice_id": "slice-b", "dispatch": "auto", "plan": "b.md", "depends_on": ["slice-a"]}],
        jobs=jobs,
    )

    assert done["status"] == "ok"
    assert done["result"]["completed"] == [{"slice_id": "slice-a", "gate_status": "needs_human"}]
    assert done["result"]["released"] == []


def test_complete_request_without_specs_dir_skips_spec_scan(monkeypatch, tmp_path):
    jobs = [
        {
            "job_id": "slice-a-1",
            "task": "slice-a",
            "persona": "builder",
            "branch": "feature/slice-a",
            "pane": "",
            "worktree": "/wt/slice-a",
            "status": "exited",
            "dispatch_head": None,
            "executor": "copilot",
            "session_name": "slice-a",
            "pid": 1001,
            "log_path": str(tmp_path / "logs" / "slice-a.jsonl"),
            "exit_code": 0,
        }
    ]
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090010Z-77777777777777777777777777777777"
    _write_request(req_id, type="complete", args={"handoff_dir": str(tmp_path / "handoff")})
    registry = FakeRegistry(jobs)
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=RecordingLauncher(),
        scan_specs_fn=lambda specs_dir: (_ for _ in ()).throw(AssertionError("scan_specs should not run")),
    )

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    assert done["status"] == "ok"
    assert done["result"]["completed"] == [{"slice_id": "slice-a", "gate_status": "needs_human"}]


def test_complete_request_forwards_review_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090011Z-88888888888888888888888888888888"
    _write_request(
        req_id,
        type="complete",
        args={
            "handoff_dir": str(tmp_path / "handoff"),
            "review_executor": "codex",
            "review_model": "gpt-5.4",
        },
    )
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    captured: dict[str, object] = {}

    def fake_complete_tick(dispatcher_arg, **kwargs):
        captured["dispatcher"] = dispatcher_arg
        captured.update(kwargs)
        return {"completed": [], "errors": [], "warnings": []}

    monkeypatch.setattr(manager_daemon.manager, "complete_tick", fake_complete_tick)
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=RecordingLauncher(),
    )
    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    assert captured["dispatcher"] is dispatcher
    assert captured["review_executor"] == "codex"
    assert captured["review_model"] == "gpt-5.4"


def test_slice_action_request_runs_manager_action(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    req_id = "20260703T090012Z-99999999999999999999999999999999"
    _write_request(
        req_id,
        type="slice-action",
        args={"slice_id": "slice-a", "action": "retry-build", "actor": "operator"},
    )
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    captured: dict[str, object] = {}

    def fake_apply(dispatcher_arg, **kwargs):
        captured["dispatcher"] = dispatcher_arg
        captured.update(kwargs)
        return {"slice_id": kwargs["slice_id"], "action": kwargs["action"], "result": "ok"}

    monkeypatch.setattr(manager_daemon.manager, "apply_slice_action", fake_apply)
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=RecordingLauncher(),
    )

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    assert done["status"] == "ok"
    assert done["result"] == {"slice_id": "slice-a", "action": "retry-build", "result": "ok"}
    assert captured["dispatcher"] is dispatcher
    assert captured["slice_id"] == "slice-a"
    assert captured["action"] == "retry-build"
    assert captured["actor"] == "operator"


def test_periodic_tick_runner_passes_default_builder_model(monkeypatch, tmp_path):
    dispatcher = FakeDispatcher(FakeRegistry())
    launcher = object()
    calls: list[dict[str, object]] = []

    def fake_run_tick(
        dispatcher_arg,
        *,
        metas,
        launcher,
        persona,
        is_satisfied,
        handoff_dir,
        require_idle,
        max_load,
        reaper,
    ) -> dict:
        calls.append({"dispatcher": dispatcher_arg, "launcher": launcher})
        return {"dispatch_skipped": False, "dispatched": [], "completed": [], "errors": [], "reaped": None}

    captured: list[dict[str, object]] = []

    def fake_resolve(executor, injected, *, allow_unsafe, model):
        captured.append({"executor": executor, "model": model})
        return injected

    monkeypatch.setattr(manager_daemon, "_resolve_launcher", fake_resolve)
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
        default_executor="copilot",
        default_model="claude-haiku-4.5",
        run_tick_fn=fake_run_tick,
        scan_specs_fn=lambda specs_dir: [],
    )

    runner()

    assert calls and calls[0]["dispatcher"] is dispatcher
    assert captured[0] == {"executor": "copilot", "model": "claude-haiku-4.5"}


def test_dispatch_no_plan(monkeypatch, tmp_path):
    done, launcher, _, _ = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a"},
        metas=[{"slice_id": "slice-a", "dispatch": "auto", "plan": None, "depends_on": []}],
    )

    assert done["status"] == "error"
    assert done["error"].endswith("invalid-spec:plan")
    assert launcher.calls == []


def test_dispatch_deps_unsatisfied(monkeypatch, tmp_path):
    done, launcher, _, _ = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a"},
        metas=[{"slice_id": "slice-a", "dispatch": "auto", "plan": "a.md", "depends_on": ["slice-dep"]}],
    )

    assert done["status"] == "error"
    assert "deps-unsatisfied" in done["error"]
    assert "slice-dep" in done["error"]
    assert launcher.calls == []


def test_dispatch_uses_request_specific_handoff_dir_for_deps(monkeypatch, tmp_path):
    override_handoff = tmp_path / "override-handoff"
    override_handoff.mkdir()
    _seed_dependency_completion(root=tmp_path, handoff_dir=override_handoff, slice_id="slice-dep")

    done, launcher, registry, worktree_creator = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a", "handoff_dir": str(override_handoff)},
        metas=[{"slice_id": "slice-a", "dispatch": "auto", "plan": "a.md", "depends_on": ["slice-dep"]}],
    )

    assert done["status"] == "ok"
    assert done["result"]["job_id"] == "slice-a-1"
    assert done["result"]["slice_id"] == "slice-a"
    assert done["result"]["branch"] == "feature/slice-a"
    assert done["result"]["worktree"] == str(tmp_path / "worktrees" / "feature__slice-a")
    assert done["result"]["target_branch"] == "main"
    assert done["result"]["target_remote"] == "origin"
    assert len(done["result"]["spec_hash"]) == 64
    assert len(done["result"]["plan_hash"]) == 64
    assert len(done["result"]["verification_hash"]) == 64
    assert worktree_creator.calls == ["feature/slice-a"]
    assert [job["job_id"] for job in registry.list_jobs()] == ["slice-a-1"]
    assert [call["slice_id"] for call in launcher.calls] == ["slice-a"]


def test_dispatch_hold_blocked(monkeypatch, tmp_path):
    done, launcher, _, _ = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a"},
        metas=[
            {
                "slice_id": "slice-a",
                "dispatch": "hold",
                "plan": "a.md",
                "depends_on": [],
                "target_branch": "main",
                "verification": {
                    "docs_class": "code",
                    "checks": [
                        {"kind": "persona-scope"},
                        {
                            "kind": "command",
                            "name": "policy",
                            "argv": ["python3", "-m", "pytest", "-q"],
                            "cwd": ".",
                            "timeout_seconds": 30,
                        },
                    ],
                    "tests": [],
                    "full_suite": {
                        "argv": ["python3", "-m", "pytest", "-q"],
                        "cwd": ".",
                        "timeout_seconds": 60,
                        "baseline": "no-regression",
                    },
                },
            }
        ],
    )

    assert done["status"] == "error"
    assert done["error"].endswith("dispatch-hold")
    assert launcher.calls == []


def test_dispatch_force_hold_audited(monkeypatch, tmp_path):
    done, launcher, registry, worktree_creator = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a", "force_hold": True},
        metas=[
            {
                "slice_id": "slice-a",
                "dispatch": "hold",
                "plan": "a.md",
                "depends_on": [],
                "target_branch": "main",
                "verification": {
                    "docs_class": "code",
                    "checks": [
                        {"kind": "persona-scope"},
                        {
                            "kind": "command",
                            "name": "policy",
                            "argv": ["python3", "-m", "pytest", "-q"],
                            "cwd": ".",
                            "timeout_seconds": 30,
                        },
                    ],
                    "tests": [],
                    "full_suite": {
                        "argv": ["python3", "-m", "pytest", "-q"],
                        "cwd": ".",
                        "timeout_seconds": 60,
                        "baseline": "no-regression",
                    },
                },
            }
        ],
        requested_by="telegram:42",
    )

    assert done["status"] == "ok"
    assert done["result"]["job_id"] == "slice-a-1"
    assert done["result"]["slice_id"] == "slice-a"
    assert done["result"]["branch"] == "feature/slice-a"
    assert done["result"]["worktree"] == str(tmp_path / "worktrees" / "feature__slice-a")
    assert done["result"]["override"] == "hold"
    assert done["result"]["requested_by"] == "telegram:42"
    assert done["result"]["target_branch"] == "main"
    assert done["result"]["target_remote"] == "origin"
    assert len(done["result"]["spec_hash"]) == 64
    assert len(done["result"]["plan_hash"]) == 64
    assert len(done["result"]["verification_hash"]) == 64
    assert worktree_creator.calls == ["feature/slice-a"]
    assert [job["job_id"] for job in registry.list_jobs()] == ["slice-a-1"]
    assert [call["slice_id"] for call in launcher.calls] == ["slice-a"]


def test_dispatch_force_hold_requires_v1_verification_contract(monkeypatch, tmp_path):
    done, launcher, _, _ = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a", "force_hold": True},
        metas=[{"slice_id": "slice-a", "dispatch": "hold", "plan": "a.md", "depends_on": []}],
        requested_by="telegram:42",
    )

    assert done["status"] == "error"
    assert done["error"].endswith("missing-verification-contract")
    assert launcher.calls == []


def test_dispatch_already_active(monkeypatch, tmp_path):
    done, launcher, _, _ = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a"},
        metas=[{"slice_id": "slice-a", "dispatch": "auto", "plan": "a.md", "depends_on": []}],
        jobs=[{"job_id": "slice-a-9", "task": "slice-a", "status": "running"}],
    )

    assert done["status"] == "error"
    assert done["error"].endswith("already-active")
    assert launcher.calls == []


def test_dispatch_success(monkeypatch, tmp_path):
    done, launcher, registry, worktree_creator = _run_dispatch_request(
        monkeypatch,
        tmp_path,
        args={"slice_id": "slice-a"},
        metas=[{"slice_id": "slice-a", "dispatch": "auto", "plan": "a.md", "depends_on": []}],
    )

    assert done["status"] == "ok"
    assert done["result"]["job_id"] == "slice-a-1"
    assert done["result"]["slice_id"] == "slice-a"
    assert done["result"]["branch"] == "feature/slice-a"
    assert done["result"]["worktree"] == str(tmp_path / "worktrees" / "feature__slice-a")
    assert done["result"]["target_branch"] == "main"
    assert done["result"]["target_remote"] == "origin"
    assert len(done["result"]["spec_hash"]) == 64
    assert len(done["result"]["plan_hash"]) == 64
    assert len(done["result"]["verification_hash"]) == 64
    assert worktree_creator.calls == ["feature/slice-a"]
    assert [job["job_id"] for job in registry.list_jobs()] == ["slice-a-1"]
    assert [call["slice_id"] for call in launcher.calls] == ["slice-a"]


def test_dispatch_success_pins_hashes_into_building_slice(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    specs_dir = tmp_path / "specs"
    _write_v1_spec(specs_dir, "slice-a")
    req_id = "20260703T090011Z-88888888888888888888888888888888"
    _write_request(req_id, type="dispatch", args={"slice_id": "slice-a"})
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree_creator = FakeWorktreeCreator(tmp_path / "worktrees")
    dispatcher = FakeDispatcher(registry, worktree_creator=worktree_creator)
    launcher = RecordingLauncher()
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(specs_dir),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
    )

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "held": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    slice_row = registry.get_slice("slice-a")

    assert done["status"] == "ok"
    assert done["result"]["target_branch"] == "main"
    assert done["result"]["target_remote"] == "origin"
    assert len(done["result"]["spec_hash"]) == 64
    assert len(done["result"]["plan_hash"]) == 64
    assert len(done["result"]["verification_hash"]) == 64
    assert slice_row["state"] == "building"
    assert slice_row["builder_job_id"] == "slice-a-1"
    assert slice_row["target_branch"] == "main"
    assert slice_row["target_remote"] == "origin"
    assert slice_row["verification"]["hash"] == done["result"]["verification_hash"]


def test_redispatch_repins_current_spec_and_plan_hashes(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    specs_dir = tmp_path / "specs"
    spec_path = _write_v1_spec(specs_dir, "slice-a")
    req_id1 = "20260703T090012Z-99999999999999999999999999999999"
    req_id2 = "20260703T090013Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _write_request(req_id1, type="dispatch", args={"slice_id": "slice-a"})
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    worktree_creator = FakeWorktreeCreator(tmp_path / "worktrees")
    dispatcher = FakeDispatcher(registry, worktree_creator=worktree_creator)
    launcher = RecordingLauncher()
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(specs_dir),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
    )

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "held": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )
    first_plan_hash = registry.get_slice("slice-a")["plan"]["hash"]
    registry.update_status("slice-a-1", "failed")
    registry.update_slice("slice-a", state="needs_human", gate_state="needs_human")
    plan_path = tmp_path / "docs" / "superpowers" / "plans" / "slice-a.md"
    plan_path.write_text("# slice-a updated\n", encoding="utf-8")
    spec_path.write_text(spec_path.read_text(encoding="utf-8"), encoding="utf-8")
    _write_request(req_id2, type="dispatch", args={"slice_id": "slice-a"})

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "held": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:06:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    second_done = contract.read_json(constants.done_dir() / f"{req_id2}.json")
    slice_row = registry.get_slice("slice-a")

    assert second_done["status"] == "ok"
    assert slice_row["builder_job_id"] == "slice-a-2"
    assert slice_row["plan"]["hash"] != first_plan_hash


def test_dispatch_without_registry_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    req_id = "20260703T090008Z-55555555555555555555555555555555"
    _write_request(req_id, type="dispatch", args={"slice_id": "slice-a"})
    _write_v1_spec(tmp_path / "specs", "slice-a")
    dispatcher = type(
        "NoRegistryDispatcher",
        (),
        {
            "_worktree_creator": FakeWorktreeCreator(tmp_path / "worktrees"),
            "_git_runner": _default_git_runner,
        },
    )()
    launcher = RecordingLauncher()
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
        scan_specs_fn=lambda specs_dir: coordinator_autonomy.scan_specs(specs_dir),
    )

    manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    done = contract.read_json(constants.done_dir() / f"{req_id}.json")
    assert done["status"] == "error"
    assert "registry" in done["error"]
    assert launcher.calls == []


def test_control_plane_dispatch_e2e_and_same_slice_second_request_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    monkeypatch.setenv("PSC_REPO_ROOT", str(tmp_path))
    from paulsha_cortex.control import client as control_client

    _write_v1_spec(tmp_path / "specs", "slice-a")
    launcher = RecordingLauncher()
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=launcher,
        scan_specs_fn=lambda specs_dir: coordinator_autonomy.scan_specs(specs_dir),
    )

    first_req_id = control_client.submit_request("dispatch", {"slice_id": "slice-a"}, "telegram")
    second_req_id = control_client.submit_request("dispatch", {"slice_id": "slice-a"}, "telegram")
    first_request_path = constants.requests_dir() / f"{first_req_id}.json"
    second_request_path = constants.requests_dir() / f"{second_req_id}.json"
    os.utime(first_request_path, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second_request_path, ns=(2_000_000_000, 2_000_000_000))

    started = manager_daemon.run_loop(
        request_executor=request_executor,
        status_provider=lambda: {"ready": [], "held": [], "in_flight": [], "recent_done": []},
        periodic_tick_runner=lambda: {"dispatch_skipped": False},
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=1,
        max_rounds=1,
    )

    first_done = contract.read_json(constants.done_dir() / f"{first_req_id}.json")
    second_done = contract.read_json(constants.done_dir() / f"{second_req_id}.json")

    assert started is True
    assert first_done["status"] == "ok"
    assert first_done["result"]["job_id"] == "slice-a-1"
    assert first_done["result"]["slice_id"] == "slice-a"
    assert first_done["result"]["branch"] == "feature/slice-a"
    assert first_done["result"]["worktree"] == str(tmp_path / "worktrees" / "feature__slice-a")
    assert first_done["result"]["target_branch"] == "main"
    assert first_done["result"]["target_remote"] == "origin"
    assert len(first_done["result"]["spec_hash"]) == 64
    assert len(first_done["result"]["plan_hash"]) == 64
    assert len(first_done["result"]["verification_hash"]) == 64
    assert second_done["status"] == "error"
    assert second_done["error"].endswith("already-active")
    assert [call["slice_id"] for call in launcher.calls] == ["slice-a"]


def test_second_instance_is_refused_by_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    first = manager_daemon.acquire_lock(pid=111, pid_alive=lambda pid: True)
    lock_payload = contract.read_json(constants.lock_path())

    try:
        started = manager_daemon.run_loop(
            request_executor=lambda req: {"dispatched": []},
            status_provider=lambda: {"ready": [], "in_flight": [], "recent_done": []},
            periodic_tick_runner=lambda: {"dispatch_skipped": False},
            poll_interval=0.0,
            tick_interval=300.0,
            now_fn=lambda: "2026-07-03T09:05:00+00:00",
            monotonic_fn=lambda: 0.0,
            sleep_fn=lambda _: None,
            pid=222,
            pid_alive=lambda pid: pid == 111,
            max_rounds=1,
        )
    finally:
        first.release()

    assert lock_payload["schema_version"] == constants.SCHEMA_VERSION
    assert lock_payload["pid"] == 111
    assert started is False


def test_pid_alive_requires_manager_cmdline(tmp_path) -> None:
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    manager_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            manager_daemon.MANAGER_CMD_MARKER,
            "--poll-interval",
            "60",
            "--tick-interval",
            "300",
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT),
            "PSC_CONTROL_ROOT": str(tmp_path / "live-manager-control"),
        },
    )
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and manager_process.poll() is None:
            if manager_daemon._pid_alive(manager_process.pid):
                break
            time.sleep(0.05)

        assert manager_daemon._pid_alive(manager_process.pid) is True
        assert manager_daemon._pid_alive(foreign.pid) is False
    finally:
        for proc in (foreign, manager_process):
            proc.terminate()
            proc.wait(timeout=10)


def test_acquire_lock_uses_flock_for_single_instance() -> None:
    source = inspect.getsource(manager_daemon.acquire_lock)

    # Single-instance is enforced by an exclusive flock (kernel-released on
    # process death), so a stale lock is reclaimable with no check-then-unlink
    # race that a second contender could use to steal a live lock.
    assert "fcntl.flock" in source
    assert "LOCK_EX" in source
    assert "LOCK_NB" in source
    assert "os.O_EXCL" not in source


def test_main_installs_term_handlers(monkeypatch) -> None:
    installed: list[tuple[signal.Signals, object]] = []

    monkeypatch.setattr(manager_daemon.signal, "signal", lambda signum, handler: installed.append((signum, handler)))
    monkeypatch.setattr(manager_daemon, "run_loop", lambda **kwargs: True)

    exit_code = manager_daemon.main(["--max-rounds", "1"])

    assert exit_code == 0
    assert installed == [
        (signal.SIGTERM, manager_daemon._handle_termination),
        (signal.SIGINT, manager_daemon._handle_termination),
    ]


def test_run_loop_default_builders_receive_injected_dispatcher_and_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry)
    request = _write_request("20260703T090005Z-99999999999999999999999999999999")
    seen: dict[str, object] = {}

    def fake_build_request_executor(
        *, dispatcher, specs_dir, handoff_dir, launcher, default_executor, reaper,
        workflow_runtime_factory,
    ):
        seen["request_dispatcher"] = dispatcher
        seen["request_specs_dir"] = specs_dir
        seen["request_handoff_dir"] = handoff_dir
        seen["request_launcher"] = launcher
        seen["request_default_executor"] = default_executor
        seen["request_reaper"] = reaper
        seen["workflow_runtime_factory"] = workflow_runtime_factory
        return lambda req: {"dispatched": [req["req_id"]], "completed": [], "errors": [], "reaped": None}

    def fake_build_runtime_status_provider(*, registry, specs_dir, handoff_dir, git_runner=None):
        seen["status_registry"] = registry
        seen["status_specs_dir"] = specs_dir
        seen["status_handoff_dir"] = handoff_dir
        seen["status_git_runner"] = git_runner
        return lambda: {"ready": [], "in_flight": [], "recent_done": []}

    def fake_build_periodic_tick_runner(*, dispatcher, specs_dir, handoff_dir, launcher, require_idle, default_executor, reaper):
        seen["periodic_dispatcher"] = dispatcher
        seen["periodic_specs_dir"] = specs_dir
        seen["periodic_handoff_dir"] = handoff_dir
        seen["periodic_launcher"] = launcher
        seen["periodic_require_idle"] = require_idle
        seen["periodic_default_executor"] = default_executor
        seen["periodic_reaper"] = reaper
        return lambda: {"dispatch_skipped": False}

    monkeypatch.setattr(manager_daemon, "build_request_executor", fake_build_request_executor)
    monkeypatch.setattr(manager_daemon, "build_runtime_status_provider", fake_build_runtime_status_provider)
    monkeypatch.setattr(manager_daemon, "build_periodic_tick_runner", fake_build_periodic_tick_runner)

    started = manager_daemon.run_loop(
        poll_interval=0.0,
        tick_interval=300.0,
        now_fn=lambda: "2026-07-03T09:05:00+00:00",
        monotonic_fn=lambda: 0.0,
        sleep_fn=lambda _: None,
        pid=123,
        max_rounds=1,
        dispatcher=dispatcher,
        registry=registry,
    )

    done = contract.read_json(constants.done_dir() / f"{request['req_id']}.json")

    assert started is True
    assert seen["request_dispatcher"] is dispatcher
    assert seen["request_specs_dir"] == str(home / ".agents" / "specs")
    assert seen["request_default_executor"] == manager_daemon.DEFAULT_EXECUTOR
    assert seen["request_reaper"] is None
    assert seen["workflow_runtime_factory"] is manager_daemon.planning_runtime.build_production_planning_runtime
    assert seen["status_registry"] is registry
    assert seen["status_specs_dir"] == str(home / ".agents" / "specs")
    assert seen["periodic_dispatcher"] is dispatcher
    assert seen["periodic_specs_dir"] == str(home / ".agents" / "specs")
    assert seen["periodic_require_idle"] is True
    assert seen["periodic_default_executor"] == manager_daemon.DEFAULT_EXECUTOR
    assert seen["periodic_reaper"] is None
    assert done["result"]["dispatched"] == [request["req_id"]]


def test_main_runs_loop_with_cli_defaults(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run_loop(**kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(manager_daemon, "run_loop", fake_run_loop)

    exit_code = manager_daemon.main([])

    assert exit_code == 0
    assert seen["poll_interval"] == manager_daemon.DEFAULT_POLL_INTERVAL
    assert seen["tick_interval"] == manager_daemon.DEFAULT_TICK_INTERVAL
    assert seen["handoff_dir"] == manager_daemon.autonomy.DEFAULT_HANDOFF_DIR
    assert seen["specs_dir"] is None
    assert seen["max_rounds"] is None
    assert seen["require_idle"] is True
    assert seen["default_executor"] == manager_daemon.DEFAULT_EXECUTOR
    assert "reaper" not in seen


def test_main_honors_manager_env_defaults(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run_loop(**kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setenv("PSC_MANAGER_EXECUTOR", "claude")
    monkeypatch.setenv("PSC_MANAGER_INTERVAL_SECONDS", "123")
    monkeypatch.setattr(manager_daemon, "run_loop", fake_run_loop)

    exit_code = manager_daemon.main([])

    assert exit_code == 0
    assert seen["tick_interval"] == 123.0
    assert seen["default_executor"] == "claude"


def test_main_rejects_removed_no_reap_flag():
    with pytest.raises(SystemExit) as exc:
        manager_daemon.main(["--no-reap"])
    assert exc.value.code == 2


def test_main_returns_one_when_lock_refuses_second_instance(monkeypatch):
    monkeypatch.setattr(manager_daemon, "run_loop", lambda **kwargs: False)

    exit_code = manager_daemon.main(["--tick-interval", "12", "--poll-interval", "1.5"])

    assert exit_code == 1


# ---- #187 review fix #4: flock lock has no stale-lock steal race ----

def test_acquire_lock_reclaims_stale_lock_file(tmp_path):
    """A stale lock file (content present, no live flock holder) is reclaimable."""
    lock_path = tmp_path / "manager.lock"
    lock_path.write_text('{"schema_version":1,"pid":999999,"acquired_at":"x"}\n', encoding="utf-8")
    held = manager_daemon.acquire_lock(path=lock_path, pid=222)
    assert held is not None
    held.release()


def test_acquire_lock_reacquire_after_release(tmp_path):
    lock_path = tmp_path / "manager.lock"
    first = manager_daemon.acquire_lock(path=lock_path, pid=111)
    assert first is not None
    # a second contender is refused while the flock is held (no unlink race)
    assert manager_daemon.acquire_lock(path=lock_path, pid=222) is None
    first.release()
    second = manager_daemon.acquire_lock(path=lock_path, pid=333)
    assert second is not None
    second.release()
