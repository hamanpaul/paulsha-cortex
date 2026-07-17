from __future__ import annotations

import json
from pathlib import Path

from paulsha_cortex.coordinator import planning_runtime
from paulsha_cortex.coordinator.model_identities import AGY_MODEL_ID, IdentityRegistry


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

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
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
    agy_calls = [
        argv for argv in calls if argv and argv[0] == "agy" and argv != ["agy", "models"]
    ]
    assert agy_calls and all("--sandbox" in argv and "--mode" in argv for argv in agy_calls)
