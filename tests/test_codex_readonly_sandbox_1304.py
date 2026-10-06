from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import launcher, manager


def _event(item_type: str, text: str) -> str:
    return json.dumps(
        {"type": "item.completed", "item": {"type": item_type, "text": text}}
    )


def test_codex_probe_tests_real_exec_modes_and_selects_safe_workspace_fallback(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        args = list(argv)
        calls.append((args, kwargs))
        assert args[:2] == ["codex", "exec"]
        mode = args[args.index("--sandbox") + 1]
        if mode == "read-only":
            # Codex reports the failed agent turn in JSONL while the process exits 0.
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=_event(
                    "agent_message",
                    "Command failed before execution: filesystem-restricted execution "
                    "requires bubblewrap to isolate app-server sockets",
                ),
                stderr="",
            )
        assert mode == "workspace-write"
        prompt = next(arg for arg in args if "expected hash `" in arg)
        expected_head = prompt.split("expected hash `", 1)[1].split("`", 1)[0]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=_event("agent_message", f"PSC-CODEX-PROBE-OK:{expected_head}"),
            stderr="",
        )

    result = launcher.probe_codex_sandbox_compatibility(
        model_id="probe-model",
        temp_root=tmp_path,
        runner=runner,
    )

    assert [args[args.index("--sandbox") + 1] for args, _ in calls] == [
        "read-only",
        "workspace-write",
    ]
    assert all("codex" == args[0] and args[1] == "exec" for args, _ in calls)
    assert result.selected_mode == "workspace-write"
    assert result.fallback is True
    assert "bubblewrap" in (result.read_only_reason or "")


def test_codex_workspace_fallback_argv_omits_legacy_landlock() -> None:
    argv = launcher.build_codex_argv(
        prompt="probe",
        slice_id="probe",
        log_dir="/tmp",
        write_forbidden=True,
        sandbox_mode_override="workspace-write",
    )

    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert not any("use_legacy_landlock" in token for token in argv)


def test_codex_workspace_fallback_cannot_be_selected_for_a_writable_card() -> None:
    with pytest.raises(ValueError, match="write-forbidden"):
        launcher.build_codex_argv(
            prompt="probe",
            slice_id="probe",
            log_dir="/tmp",
            sandbox_mode_override="workspace-write",
        )


def test_write_forbidden_workflow_rejects_worktree_or_head_changes(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "--quiet", str(worktree)], check=True)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "user.name=Test", "-c",
            "user.email=test@example.invalid", "commit", "--quiet", "--allow-empty",
            "-m", "baseline",
        ],
        check=True,
    )
    (worktree / "pinned-plan.md").write_text("accepted\n", encoding="utf-8")
    baseline_hash = manager._write_forbidden_worktree_snapshot(worktree)
    baseline_head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    job = {
        "worktree": str(worktree),
        "dispatch_head": baseline_head,
        "workflow_sandbox_hash": baseline_hash,
    }

    manager._verify_write_forbidden_worktree(
        job, candidate=baseline_head, baseline_candidate=baseline_head
    )
    (worktree / "pinned-plan.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="write-forbidden workflow card changed worktree"):
        manager._verify_write_forbidden_worktree(
            job, candidate=baseline_head, baseline_candidate=baseline_head
        )

    with pytest.raises(ValueError, match="write-forbidden workflow card changed HEAD"):
        manager._verify_write_forbidden_worktree(
            job, candidate="b" * 40, baseline_candidate=baseline_head
        )


def test_codex_readonly_probe_is_attached_to_launcher_preflight(monkeypatch) -> None:
    probed: list[str] = []
    monkeypatch.setenv("PSC_JOB_RUNNER", "direct")
    instance = launcher.SubprocessLauncher(
        "codex", write_forbidden=True, codex_compatibility_probe=True
    )

    def probe(self):
        probed.append(self._executor)
        self._codex_sandbox_mode_override = "workspace-write"
        self._codex_compatibility_diagnostic = "read-only unavailable"

    monkeypatch.setattr(launcher.SubprocessLauncher, "_ensure_codex_compatibility", probe)
    environment = instance.executor_environment()

    assert probed == ["codex"]
    assert "workspace-write-fallback" in environment.name
