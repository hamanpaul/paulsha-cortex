from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import cli, manager_daemon
from paulsha_cortex.coordinator.autonomy import (
    DispatchReadyError,
    dispatch_ready,
    parse_spec_frontmatter,
)
from paulsha_cortex.coordinator.model_identities import IdentityRegistry


def _write_spec(dirpath: Path, name: str, frontmatter: str | None, body: str = "x") -> Path:
    path = dirpath / name
    if frontmatter is None:
        path.write_text(body + "\n", encoding="utf-8")
    else:
        path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path


def _verification_block(*, docs_class: str = "code") -> str:
    return (
        "target_branch: main\n"
        "verification:\n"
        f"  docs_class: {docs_class}\n"
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
        "    baseline: no-regression"
    )


def _meta(
    slice_id: str,
    *,
    dispatch: str = "auto",
    plan: str = "docs/superpowers/plans/example.md",
    depends_on: list[str] | None = None,
    executor: str | None = None,
    model_id: str | None = None,
) -> dict:
    spec_path = f"/specs/{slice_id}.md"
    return {
        "path": spec_path,
        "dispatch": dispatch,
        "slice_id": slice_id,
        "plan": plan,
        "depends_on": list(depends_on or []),
        "target_branch": "main",
        "verification": {
            "docs_class": "code",
            "review_policy": "required",
            "required_artifacts": [],
            "checks": [
                {"kind": "persona-scope"},
                {
                    "kind": "command",
                    "name": "policy",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 300,
                },
            ],
            "tests": [],
            "full_suite": {
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 300,
                "baseline": "no-regression",
            },
        },
        "parse_error": None,
        "executor": executor,
        "model_id": model_id,
        "_pinned_inputs": {
            "spec_path": spec_path,
            "spec_hash": "0" * 64,
            "plan_path": plan,
            "plan_hash": "1" * 64,
            "target_branch": "main",
            "target_remote": "origin",
            "verification_hash": "2" * 64,
        },
    }


def _identity_registry() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-5.4-codex",
                "independence_domain": "openai",
            },
            {
                "executor": "copilot",
                "model_id": "claude-haiku-4.5",
                "independence_domain": "anthropic",
            },
        ]
    )


def _default_git_runner(args: list[str]):
    if not args:
        return ""
    if args[0] == "rev-parse":
        return "f" * 40
    if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
        return ""
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        return "f" * 40
    if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
        return ""
    return ""


class FakeRegistry:
    def __init__(self) -> None:
        self._jobs: list[dict] = []
        self._seq = 0
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
            "actions": [],
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


class FakeWorktreeCreator:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self.calls: list[tuple[str, str | None]] = []

    def create(self, branch: str, base_sha: str | None = None) -> str:
        self.calls.append((branch, base_sha))
        return str(self._base_dir / branch.replace("/", "__"))


class FakeDispatcher:
    def __init__(self, registry: FakeRegistry, worktree_creator: FakeWorktreeCreator | None = None) -> None:
        self._registry = registry
        self._worktree_creator = worktree_creator
        self._git_runner = _default_git_runner


class RecordingLauncher:
    def __init__(self, *, executor: str, model_id: str | None, label: str) -> None:
        self.executor = executor
        self.model_id = model_id
        self.label = label
        self.calls: list[dict[str, str]] = []
        self.commit_capability_requests = 0

    def as_commit_required(self):
        self.commit_capability_requests += 1
        return self

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
            executor=self.executor,
            model_id=self.model_id,
            session_name=f"{self.label}:{slice_id}",
            pid=1000 + len(self.calls),
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def test_frontmatter_paired_executor_model_id_parsed(tmp_path: Path) -> None:
    spec = _write_spec(
        tmp_path,
        "paired.md",
        "dispatch: auto\n"
        "slice_id: paired-slice\n"
        "plan: docs/superpowers/plans/paired.md\n"
        "executor: codex\n"
        "model_id: gpt-5.4-codex\n"
        f"{_verification_block()}",
    )

    meta = parse_spec_frontmatter(spec)

    assert meta["dispatch"] == "auto"
    assert meta["slice_id"] == "paired-slice"
    assert meta["plan"] == "docs/superpowers/plans/paired.md"
    assert meta.get("executor") == "codex"
    assert meta.get("model_id") == "gpt-5.4-codex"
    assert meta["parse_error"] is None


def test_frontmatter_executor_without_model_id_invalid(tmp_path: Path) -> None:
    executor_only = parse_spec_frontmatter(
        _write_spec(
            tmp_path,
            "executor-only.md",
            "dispatch: auto\n"
            "slice_id: executor-only\n"
            "plan: docs/superpowers/plans/executor-only.md\n"
            "executor: codex\n"
            f"{_verification_block()}",
        )
    )
    model_only = parse_spec_frontmatter(
        _write_spec(
            tmp_path,
            "model-only.md",
            "dispatch: auto\n"
            "slice_id: model-only\n"
            "plan: docs/superpowers/plans/model-only.md\n"
            "model_id: gpt-5.4-codex\n"
            f"{_verification_block()}",
        )
    )

    assert executor_only["parse_error"]["code"] == "invalid-frontmatter"
    assert executor_only["parse_error"]["field"] == "model_id"
    assert model_only["parse_error"]["code"] == "invalid-frontmatter"
    assert model_only["parse_error"]["field"] == "executor"


def test_emitted_frontmatter_fields_include_identity() -> None:
    from paulsha_cortex.deck.schema import EMITTED_FRONTMATTER_FIELDS

    assert "executor" in EMITTED_FRONTMATTER_FIELDS
    assert "model_id" in EMITTED_FRONTMATTER_FIELDS


def test_dispatch_ready_per_slice_override_reaches_job_row(tmp_path: Path) -> None:
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    default_launcher = RecordingLauncher(executor="copilot", model_id=None, label="default")
    identities = _identity_registry()
    factory_calls: list[tuple[str, str]] = []
    override_launchers: list[RecordingLauncher] = []

    def launcher_factory(identity):
        factory_calls.append((identity.executor, identity.model_id))
        launcher = RecordingLauncher(
            executor=identity.executor,
            model_id=identity.model_id,
            label="override",
        )
        override_launchers.append(launcher)
        return launcher

    jobs = dispatch_ready(
        [
            _meta("slice-override", executor="codex", model_id="gpt-5.4-codex"),
            _meta("slice-default"),
        ],
        is_satisfied=lambda _slice_id: True,
        dispatcher=dispatcher,
        persona="builder",
        launcher=default_launcher,
        identity_registry=identities,
        launcher_factory=launcher_factory,
        git_runner=_default_git_runner,
    )

    override_job = next(job for job in jobs if job["task"] == "slice-override")
    default_job = next(job for job in jobs if job["task"] == "slice-default")

    assert factory_calls == [("codex", "gpt-5.4-codex")]
    assert len(override_launchers) == 1
    assert override_launchers[0].commit_capability_requests == 1
    assert override_launchers[0].calls[0]["slice_id"] == "slice-override"
    assert default_launcher.commit_capability_requests == 1
    assert default_launcher.calls[0]["slice_id"] == "slice-default"
    assert (override_job["executor"], override_job["model_id"]) == ("codex", "gpt-5.4-codex")
    assert (default_job["executor"], default_job["model_id"]) == ("copilot", None)
    assert registry.get_slice("slice-override")["builder_job_id"] == override_job["job_id"]


def test_dispatch_ready_unknown_identity_fail_closed_lists_available(tmp_path: Path) -> None:
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    default_launcher = RecordingLauncher(executor="copilot", model_id=None, label="default")
    identities = _identity_registry()

    with pytest.raises(DispatchReadyError) as excinfo:
        dispatch_ready(
            [
                _meta("slice-bad", executor="codex", model_id="missing-model"),
                _meta("slice-good"),
            ],
            is_satisfied=lambda _slice_id: True,
            dispatcher=dispatcher,
            persona="builder",
            launcher=default_launcher,
            identity_registry=identities,
            launcher_factory=lambda identity: RecordingLauncher(
                executor=identity.executor,
                model_id=identity.model_id,
                label="override",
            ),
            git_runner=_default_git_runner,
        )

    assert [job["task"] for job in excinfo.value.jobs] == ["slice-good"]
    assert "codex/missing-model" in str(excinfo.value)
    assert "codex/gpt-5.4-codex" in str(excinfo.value)
    assert default_launcher.calls[0]["slice_id"] == "slice-good"
    assert registry.get_slice("slice-bad")["state"] == "needs_human"
    # reviewer #333-1：identity 檢查失敗於 base_sha 解析前，needs_human 之後
    # dispatch_base 不會再被更新，故 pending slice 建立時須先嘗試填入既有
    # branch head（非硬編碼 None），保留可診斷基準。
    assert registry.get_slice("slice-bad")["dispatch_base"] == "f" * 40


def test_dispatch_ready_no_declaration_behavior_unchanged(tmp_path: Path) -> None:
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    default_launcher = RecordingLauncher(executor="copilot", model_id=None, label="default")

    jobs = dispatch_ready(
        [_meta("slice-plain")],
        is_satisfied=lambda _slice_id: True,
        dispatcher=dispatcher,
        persona="builder",
        launcher=default_launcher,
        identity_registry=_identity_registry(),
        launcher_factory=lambda identity: (_ for _ in ()).throw(
            AssertionError(f"launcher_factory should not run for undeclared slice: {identity}")
        ),
        git_runner=_default_git_runner,
    )

    assert [job["task"] for job in jobs] == ["slice-plain"]
    assert default_launcher.commit_capability_requests == 1
    assert default_launcher.calls[0]["slice_id"] == "slice-plain"


def test_held_reasons_classified(tmp_path: Path) -> None:
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "external-dep.json").write_text("{}", encoding="utf-8")

    reasons = manager_daemon._held_reasons(
        {
            "slice_id": "slice-x",
            "dispatch": "auto",
            "plan": "x.md",
            "depends_on": ["batch-dep", "external-dep", "unknown-dep"],
        },
        lambda _slice_id: False,
        batch_ids={"slice-x", "batch-dep"},
        handoff_dir=str(handoff_dir),
    )

    assert reasons == [
        "deps-unsatisfied:batch-dep",
        "deps-external:external-dep",
        "deps-unknown:unknown-dep",
    ]


def test_ready_cli_unknown_dep_stderr_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    _write_spec(
        specs_dir,
        "slice-a.md",
        "dispatch: auto\n"
        "slice_id: slice-a\n"
        "plan: docs/superpowers/plans/slice-a.md\n"
        "depends_on: [ghost-dep]\n"
        f"{_verification_block()}",
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = cli.main(
            ["ready", "--specs-dir", str(specs_dir)],
            is_satisfied=lambda _slice_id: False,
        )

    assert rc == 0
    assert json.loads(stdout.getvalue()) == []
    assert "deps-unknown:ghost-dep" in stderr.getvalue()


def test_fanout_request_unknown_builder_identity_rejected(tmp_path: Path) -> None:
    registry = FakeRegistry()
    dispatcher = FakeDispatcher(registry, worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"))
    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda _specs_dir: [_meta("slice-a")],
        dispatch_ready_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dispatch_ready should not run")
        ),
        workflow_identity_registry=_identity_registry(),
    )

    with pytest.raises(ValueError) as excinfo:
        request_executor(
            {
                "type": "fanout",
                "args": {"executor": "codex", "model": "missing-model"},
                "requested_by": "operator",
            }
        )

    assert "codex/missing-model" in str(excinfo.value)
    assert "codex/gpt-5.4-codex" in str(excinfo.value)


def test_fanout_request_without_model_unchanged(tmp_path: Path) -> None:
    class NoValidationRegistry:
        identities: tuple[object, ...] = ()

        def get(self, executor: str, model_id: str):
            raise AssertionError(f"registry validation should not run for {executor}/{model_id}")

    registry = NoValidationRegistry()
    dispatcher = FakeDispatcher(
        FakeRegistry(),
        worktree_creator=FakeWorktreeCreator(tmp_path / "worktrees"),
    )
    seen: list[dict[str, object]] = []

    def fake_dispatch_ready(
        metas,
        predicate,
        dispatcher,
        *,
        persona,
        launcher,
        handoff_dir,
        git_runner,
        identity_registry,
        launcher_factory,
    ):
        seen.append(
            {
                "metas": list(metas),
                "persona": persona,
                "handoff_dir": handoff_dir,
                "identity_registry": identity_registry,
                "launcher_factory": launcher_factory,
            }
        )
        return [
            {
                "job_id": "slice-a-1",
                "task": "slice-a",
                "branch": "feature/slice-a",
                "worktree": "/wt/slice-a",
            }
        ]

    request_executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        scan_specs_fn=lambda _specs_dir: [_meta("slice-a")],
        dispatch_ready_fn=fake_dispatch_ready,
        workflow_identity_registry=registry,
    )

    result = request_executor(
        {
            "type": "fanout",
            "args": {"executor": "codex"},
            "requested_by": "operator",
        }
    )

    assert result["dispatch_skipped"] is False
    assert [job["task"] for job in result["dispatched"]] == ["slice-a"]
    assert len(seen) == 1
    assert seen[0]["identity_registry"] is registry
    assert callable(seen[0]["launcher_factory"])
