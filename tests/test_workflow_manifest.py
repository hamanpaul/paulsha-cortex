from __future__ import annotations

from dataclasses import asdict

import pytest

from paulsha_cortex.coordinator.workflow import WorkflowManifest, WorkflowStep
from paulsha_cortex.deck.compile import DeckCompileError, compile_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    EMITTED_FRONTMATTER_FIELDS,
    load_cards,
    load_combo,
)


def _feature_oneshot():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    return cards, combo


def test_workflow_step_serializes_all_persisted_fields() -> None:
    step = WorkflowStep(
        phase="plan",
        persona="planner",
        card="writing-plans",
        executor="agy",
        model="gemini-3.1-pro-high",
        domain="google",
        inputs=("openspec/changes/demo/proposal.md",),
        outputs=("docs/superpowers/plans/demo.md",),
        gate_result="pending",
    )

    assert asdict(step) == {
        "phase": "plan",
        "persona": "planner",
        "card": "writing-plans",
        "executor": "agy",
        "model": "gemini-3.1-pro-high",
        "domain": "google",
        "inputs": ("openspec/changes/demo/proposal.md",),
        "outputs": ("docs/superpowers/plans/demo.md",),
        "gate_result": "pending",
    }


def test_feature_oneshot_emits_card_bound_personas_not_global_builder() -> None:
    cards, combo = _feature_oneshot()
    result = compile_combo(combo, cards, "manifest demo", change="demo")

    assert isinstance(result.workflow_manifest, WorkflowManifest)
    steps = result.workflow_manifest.steps
    assert [step.card for step in steps] == [entry.ref for entry in combo.cards]
    assert [(step.card, step.phase, step.persona) for step in steps] == [
        ("brainstorming", "define", "planner"),
        ("openspec-propose", "define", "planner"),
        ("writing-plans", "plan", "planner"),
        ("worktree-isolation", "build", "builder"),
        ("tdd-red", "build", "builder"),
        ("subagent-build", "build", "builder"),
        ("code-review", "review", "reviewer"),
        ("verification", "verify", "reviewer"),
        ("openspec-archive", "ship", "manager"),
        ("policy-commit", "ship", "manager"),
        ("adversarial-review", "review", "reviewer"),
    ]
    assert {step.persona for step in steps} == {"planner", "builder", "reviewer", "manager"}
    assert all(step.gate_result == "pending" for step in steps)


def test_manifest_resolves_inputs_outputs_and_preserves_slice_frontmatter() -> None:
    cards, combo = _feature_oneshot()
    result = compile_combo(combo, cards, "manifest demo", change="demo")

    plan = next(step for step in result.workflow_manifest.steps if step.card == "writing-plans")
    assert plan.inputs == ("openspec/changes/demo/proposal.md",)
    assert plan.outputs == ("docs/superpowers/plans/*manifest-demo*.md",)

    for slice_doc in result.slices:
        frontmatter = slice_doc.content.split("---\n", 2)[1]
        import yaml

        assert set(yaml.safe_load(frontmatter)) == set(EMITTED_FRONTMATTER_FIELDS)


def test_manifest_rejects_unknown_persona_binding(tmp_path) -> None:
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(
        """\
version: 0
cards:
  - id: ghost-plan
    kind: skill
    type: interactive
    class: core
    skill_ref: ghost
    phase: plan
    requires: []
    produces: [docs/plan.md]
    persona_binding: ghost
""",
        encoding="utf-8",
    )
    combo_path = tmp_path / "combo.yaml"
    combo_path.write_text(
        """\
combo:
  id: ghost
  task_type: feature
  cards:
    - ref: ghost-plan
""",
        encoding="utf-8",
    )
    cards = load_cards(cards_path)
    combo = load_combo(combo_path, cards)

    with pytest.raises(DeckCompileError, match="ghost-plan.*ghost"):
        compile_combo(combo, cards, "ghost", plan_ref="docs/plan.md")
