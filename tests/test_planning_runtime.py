from __future__ import annotations

import json
from pathlib import Path

from paulsha_cortex.coordinator import planning_runtime
import pytest

from paulsha_cortex.coordinator.model_identities import AGY_MODEL_ID, IdentityRegistry, ModelIdentity


def _completed(stdout: str = "", returncode: int = 0):
    return type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": returncode})()


def test_production_runtime_loads_registry_and_probes_only_safe_launchers(
    monkeypatch, tmp_path: Path
) -> None:
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary", "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy", "model_id": AGY_MODEL_ID, "independence_domain": "google",
                "capabilities": ["planning"], "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: registry)
    calls: list[list[str]] = []
    invocation_cwds: list[Path] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
        if "cwd" in kwargs:
            invocation_cwds.append(Path(kwargs["cwd"]))
        prompt = argv[argv.index("--print") + 1] if "--print" in argv else argv[2]
        marker = "Return only this compact JSON object and perform no tool calls: "
        if marker in prompt:
            return _completed(prompt.split(marker, 1)[1] + "\n")
        marker = "Return only this JSON object and do not call tools: "
        if marker in prompt:
            return _completed(prompt.split(marker, 1)[1] + "\n")
        return _completed(json.dumps({"unexpected": True}))

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, runner=runner
    )

    assert runtime.identity_registry is registry
    assert runtime.probes[("agy", AGY_MODEL_ID)].ready is True
    assert runtime.probes[("codex", "primary")].ready is True
    assert all("--dangerously-bypass-approvals-and-sandbox" not in argv for argv in calls)
    codex_calls = [argv for argv in calls if argv and argv[0] == "codex"]
    assert codex_calls and all(
        argv[argv.index("--sandbox") + 1] == "read-only" for argv in codex_calls
    )
    agy_calls = [
        argv for argv in calls if argv and argv[0] == "agy" and argv != ["agy", "models"]
    ]
    assert agy_calls and all("--sandbox" in argv and "--mode" in argv for argv in agy_calls)
    assert invocation_cwds and all(path != tmp_path for path in invocation_cwds)
    assert all("cortex-planning-" in str(path) for path in invocation_cwds)

    claude_argv = planning_runtime._planning_argv(
        ModelIdentity("claude", "claude-plan", "anthropic", ("planning",)),
        "prompt",
        str(tmp_path / "runtime-output"),
        tmp_path,
    )
    assert claude_argv[claude_argv.index("--permission-mode") + 1] == "plan"
    assert claude_argv[claude_argv.index("--tools") + 1] == ""


def test_planning_runtime_rejects_any_worktree_mutation(tmp_path: Path) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    baseline = tmp_path / "tracked.md"
    baseline.write_text("original\n", encoding="utf-8")

    def runner(argv, **kwargs):
        baseline.write_text("mutated\n", encoding="utf-8")
        (tmp_path / "unexpected.md").write_text("leak\n", encoding="utf-8")
        return _completed("failure\n", returncode=9)

    with pytest.raises(ValueError, match="operator worktree.*rolled back"):
        planning_runtime._invoke_json(
            identity,
            "return JSON",
            worktree=tmp_path,
            runner=runner,
            timeout_seconds=30,
        )
    assert baseline.read_text(encoding="utf-8") == "original\n"
    assert not (tmp_path / "unexpected.md").exists()


def test_planning_runtime_checks_disposable_sandbox_even_on_nonzero(tmp_path: Path) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    baseline = tmp_path / "tracked.md"
    baseline.write_text("operator\n", encoding="utf-8")

    def runner(argv, **kwargs):
        (Path(kwargs["cwd"]) / "leak.md").write_text("sandbox mutation\n", encoding="utf-8")
        return _completed("failed\n", returncode=3)

    with pytest.raises(ValueError, match="disposable read-only sandbox"):
        planning_runtime._invoke_json(
            identity,
            "return JSON",
            worktree=tmp_path,
            runner=runner,
            timeout_seconds=30,
        )
    assert baseline.read_text(encoding="utf-8") == "operator\n"
    assert not (tmp_path / "leak.md").exists()
