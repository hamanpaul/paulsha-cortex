from __future__ import annotations

import json
import os
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
    assert all("--skip-git-repo-check" in argv for argv in codex_calls)
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


def test_secondary_prompt_embeds_bounded_repo_sources_without_tool_access(
    monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "openspec" / "changes" / "demo" / "proposal.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nstatus: draft\n---\n## Why\nEvidence.\n", encoding="utf-8")
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
    prompts: list[str] = []

    def runner(argv, **kwargs):
        if argv == ["agy", "models"]:
            return _completed(f"{AGY_MODEL_ID}\n")
        prompt = argv[argv.index("--print") + 1] if "--print" in argv else argv[2]
        prompts.append(prompt)
        for marker in (
            "Return only this compact JSON object and perform no tool calls: ",
            "Return only this JSON object and do not call tools: ",
        ):
            if marker in prompt:
                return _completed(prompt.split(marker, 1)[1] + "\n")
        return _completed(
            json.dumps(
                {
                    "schema_version": 1,
                    "question_pack_id": "qp-demo",
                    "evidence": [
                        {
                            "question_id": "q-demo",
                            "claims": ["The proposal records the intended evidence."],
                            "source_refs": ["openspec/changes/demo/proposal.md"],
                        }
                    ],
                }
            )
        )

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, runner=runner
    )
    result = runtime.secondary_planner(
        {
            "schema_version": 1,
            "pack_id": "qp-demo",
            "questions": [
                {
                    "question_id": "q-demo",
                    "kind": "missing-spec",
                    "prompt": "What is required?",
                    "source_refs": ["openspec/changes/demo/proposal.md"],
                }
            ],
        },
        registry.require("agy", AGY_MODEL_ID),
    )

    assert result["question_pack_id"] == "qp-demo"
    assert "Do not call tools" in prompts[-1]
    assert "Evidence." in prompts[-1]
    assert planning_runtime._planning_destinations(
        {
            "questions": [
                {"source_refs": ["openspec/changes/demo/proposal.md"]}
            ]
        }
    )["plan"] == "docs/superpowers/plans/demo.md"


def test_planning_json_parser_accepts_only_whole_fenced_object(tmp_path: Path) -> None:
    output = tmp_path / "missing.json"
    assert planning_runtime._extract_json(
        '```json\n{"schema_version": 1}\n```\n', output
    ) == {"schema_version": 1}
    with pytest.raises(ValueError, match="no JSON object"):
        planning_runtime._extract_json(
            'Commentary.\n```json\n{"schema_version": 1}\n```\n', output
        )


def test_planning_source_material_rejects_symlink_traversal(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "linked.md"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        planning_runtime._planning_source_material(
            {"questions": [{"source_refs": ["linked.md"]}]}, root=tmp_path
        )


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


def test_planning_runtime_detects_and_rolls_back_directory_and_metadata_pollution(
    tmp_path: Path,
) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    tracked = tmp_path / "tracked.md"
    tracked.write_text("operator\n", encoding="utf-8")
    tracked.chmod(0o640)
    empty = tmp_path / "empty"
    empty.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    directory_link = tmp_path / "dir-link"
    directory_link.symlink_to("target", target_is_directory=True)

    def runner(argv, **kwargs):
        tracked.chmod(0o600)
        empty.rmdir()
        (tmp_path / "pollution-empty").mkdir()
        directory_link.unlink()
        directory_link.symlink_to("empty", target_is_directory=True)
        return _completed(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="operator worktree.*rolled back"):
        planning_runtime._invoke_json(
            identity,
            "return JSON",
            worktree=tmp_path,
            runner=runner,
            timeout_seconds=30,
        )

    assert tracked.stat().st_mode & 0o777 == 0o640
    assert empty.is_dir()
    assert not (tmp_path / "pollution-empty").exists()
    assert directory_link.is_symlink()
    assert os.readlink(directory_link) == "target"


def test_tree_snapshot_covers_empty_directories_directory_links_and_modes(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    baseline_mode = empty.lstat().st_mode & 0o7777
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "dir-link"
    link.symlink_to("target", target_is_directory=True)
    baseline = planning_runtime._tree_snapshot(tmp_path)

    empty.rmdir()
    assert planning_runtime._tree_snapshot(tmp_path) != baseline
    empty.mkdir()
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    empty.chmod(0o700)
    assert planning_runtime._tree_snapshot(tmp_path) != baseline
    empty.chmod(baseline_mode)
    assert planning_runtime._tree_snapshot(tmp_path) == baseline

    link.unlink()
    link.symlink_to("empty", target_is_directory=True)
    assert planning_runtime._tree_snapshot(tmp_path) != baseline


def test_snapshot_permission_error_still_restores_operator_tree(tmp_path: Path) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    protected = tmp_path / "protected"
    protected.mkdir()
    tracked = protected / "tracked.md"
    tracked.write_text("baseline\n", encoding="utf-8")
    xattr_supported = True
    try:
        os.setxattr(tracked, "user.cortex-test", b"baseline")
    except (AttributeError, OSError):
        xattr_supported = False
    protected.chmod(0o750)

    def runner(argv, **kwargs):
        tracked.write_text("polluted\n", encoding="utf-8")
        if xattr_supported:
            os.setxattr(tracked, "user.cortex-test", b"polluted")
        protected.chmod(0)
        return _completed(json.dumps({"ok": True}))

    with pytest.raises(ValueError, match="operator worktree.*rolled back"):
        planning_runtime._invoke_json(
            identity, "return JSON", worktree=tmp_path, runner=runner,
            timeout_seconds=30,
        )

    assert protected.stat().st_mode & 0o777 == 0o750
    assert tracked.read_text(encoding="utf-8") == "baseline\n"
    if xattr_supported:
        assert os.getxattr(tracked, "user.cortex-test") == b"baseline"


def test_operator_restore_fault_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    tracked = tmp_path / "tracked.md"
    tracked.write_text("baseline\n", encoding="utf-8")
    real_restore = planning_runtime._restore_operator_tree

    def runner(argv, **kwargs):
        tracked.write_text("polluted\n", encoding="utf-8")
        return _completed(json.dumps({"ok": True}))

    def restore_then_fail(worktree, baseline):
        real_restore(worktree, baseline)
        raise OSError("restore fsync fault")

    monkeypatch.setattr(planning_runtime, "_restore_operator_tree", restore_then_fail)
    with pytest.raises(RuntimeError, match="restore failed"):
        planning_runtime._invoke_json(
            identity, "return JSON", worktree=tmp_path, runner=runner,
            timeout_seconds=30,
        )
    assert tracked.read_text(encoding="utf-8") == "baseline\n"
