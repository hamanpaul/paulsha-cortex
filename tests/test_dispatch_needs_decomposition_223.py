"""#223（design #208 H.3）：Red band 在 plan phase 完成後轉 needs_decomposition，
不推進到 build；拆分深度逾限轉 needs_human；band 維持 red 期間不得以原身分繼續
重試。

掛載點：``manager._dispatch_workflow_card`` 的 planner/plan phase 完成後、
推進 next_phase 之前（#223 讀碼地圖 a83d2334cec5b55fb）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo


def _manifest():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "needs decomposition", change="needs-decomposition-223")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _write_planning_artifacts(root: Path) -> tuple[PlanningArtifactAuthority, ...]:
    proposal = root / "openspec/changes/needs-decomposition-223/proposal.md"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    authority: list[PlanningArtifactAuthority] = []
    for kind, body in bodies.items():
        ref = f"docs/{kind}.md"
        path = root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        authority.append(
            PlanningArtifactAuthority(
                ref=ref, kind=kind, work_id="needs-decomposition-223", baseline_sha256=digest
            )
        )
    return tuple(authority)


def _dispatcher(registry: JobRegistry):
    return type("D", (), {"_registry": registry, "_git_runner": None})()


def _must_not_launch(_identity):
    raise AssertionError("red band 必須在 build 前攔下，不得啟動任何 launcher")


def _planner_dispatch_setup(tmp_path: Path, monkeypatch):
    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-5.6-luna",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            }
        ]
    )

    class _Launcher:
        def __init__(self):
            self.calls: list[str] = []

        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            self.calls.append(slice_id)
            return LaunchHandle(
                executor="codex",
                model_id="gpt-5.6-luna",
                session_name=slice_id,
                pid=100,
                log_path=str(tmp_path / "planner.jsonl"),
            )

    launcher = _Launcher()
    monkeypatch.setattr(manager, "_runtime_preflight_gate", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        manager, "resolve_limiter", lambda _value: SimpleNamespace(admit=lambda _provider: None)
    )
    monkeypatch.setattr(manager, "resolve_provider", lambda **_kwargs: "codex")
    return identities, launcher


def _make_run(tmp_path: Path, *, sizing_band: str | None, decomposition_depth: int = 0):
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    authority = _write_planning_artifacts(repo)
    sizing_score = (
        None
        if sizing_band is None
        else {"green": 2, "yellow": 5, "red": 8}[sizing_band]
    )
    run = registry._manager_create_workflow_run(
        work_id="needs-decomposition-223",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#223",),
        openspec_refs=("needs-decomposition-223",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=authority,
        sizing_score=sizing_score,
        sizing_band=sizing_band,
        decomposition_depth=decomposition_depth,
    )
    return registry, run


def test_red_band_diverts_to_needs_decomposition_instead_of_advancing_to_build(
    tmp_path: Path, monkeypatch
) -> None:
    registry, run = _make_run(tmp_path, sizing_band="red")
    identities, launcher = _planner_dispatch_setup(tmp_path, monkeypatch)

    result = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert result["job_id"]
    assert persisted.current_phase == "plan"
    assert persisted.facets == ("needs_decomposition",)
    assert persisted.attempts == {"plan": 1}
    jobs = registry.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["persona"] == "planner"
    assert jobs[0]["workflow_card"] == "red-decomposition"
    assert not any(job["persona"] == "builder" for job in jobs)
    assert len(launcher.calls) == 1


def test_red_band_at_depth_limit_escalates_to_needs_human(tmp_path: Path) -> None:
    registry, run = _make_run(tmp_path, sizing_band="red", decomposition_depth=2)

    result = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert result["reason"] == "decomposition-depth-exceeded"
    assert persisted.current_phase == "plan"
    assert persisted.facets == ("needs_human",)


def test_red_band_diversion_is_stable_across_repeated_dispatch_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    # #223 驗收條件 4：band 維持 red 期間，不得以原身分繼續重試——同一 run 連續
    # dispatch 兩次都必須攔在 plan phase，永遠不會有一次「意外」推進到 build。
    registry, run = _make_run(tmp_path, sizing_band="red")
    identities, launcher = _planner_dispatch_setup(tmp_path, monkeypatch)
    results = []

    for _ in range(2):
        result = manager.dispatch_workflow_card(
            _dispatcher(registry),
            run=registry.get_workflow_run(run.run_id),
            identities=identities,
            launcher_factory=lambda _identity: launcher,
            coordinator_root=tmp_path / "coordinator",
        )
        results.append(result)

    persisted = registry.get_workflow_run(run.run_id)
    assert persisted.current_phase == "plan"
    assert persisted.facets == ("needs_decomposition",)
    jobs = registry.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["workflow_card"] == "red-decomposition"
    assert results[0]["job_id"] == results[1]["job_id"]
    assert len(launcher.calls) == 1
    assert not any(job["persona"] == "builder" for job in jobs)


def test_green_band_advances_to_build_unaffected(tmp_path: Path) -> None:
    registry, run = _make_run(tmp_path, sizing_band="green")

    result = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert result is None
    assert persisted.current_phase == "build"
    assert persisted.facets == ()


def test_unset_band_advances_to_build_fail_soft(tmp_path: Path) -> None:
    # 舊 plan（無宣告欄位、band 從未被寫入）：不算 sizing、不掛 band，路由走
    # 現行為（fail-soft，比照 envelope_unavailable 模式）。
    registry, run = _make_run(tmp_path, sizing_band=None)

    result = manager.dispatch_workflow_card(
        _dispatcher(registry),
        run=run,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=_must_not_launch,
        coordinator_root=tmp_path / "coordinator",
    )

    persisted = registry.get_workflow_run(run.run_id)
    assert result is None
    assert persisted.current_phase == "build"
