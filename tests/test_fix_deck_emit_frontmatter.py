from __future__ import annotations

from pathlib import Path

import yaml

from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter
from paulsha_cortex.deck.compile import CompileResult, compile_combo, emit
from paulsha_cortex.deck.schema import load_cards, load_combo

CARDS_YAML = """\
version: 0
cards:
  - id: write-spec
    kind: skill
    type: interactive
    class: core
    skill_ref: "superpowers:writing-plans"
    requires: []
    produces: ["docs/superpowers/plans/*<task-slug>*.md"]
    persona_binding: planner
  - id: worktree-isolation
    kind: skill
    type: headless
    class: core
    skill_ref: "superpowers:using-git-worktrees"
    slice_group: build
    requires: ["docs/superpowers/plans/*<task-slug>*.md"]
    produces: []
    persona_binding: builder
"""

COMBO_YAML = """\
combo:
  id: red-only
  task_type: feature
  cards:
    - ref: write-spec
    - ref: worktree-isolation
"""


def _seed(tmp_path: Path):
    cards_path = tmp_path / "cards.yaml"
    combos_dir = tmp_path / "combos"
    cards_path.write_text(CARDS_YAML, encoding="utf-8")
    combos_dir.mkdir()
    combo_path = combos_dir / "red-only.yaml"
    combo_path.write_text(COMBO_YAML, encoding="utf-8")
    cards = load_cards(cards_path)
    combo = load_combo(combo_path, cards)
    return cards, combo


def test_emit_frontmatter_non_empty_target_branch_and_verification_contract(tmp_path):
    cards, combo = _seed(tmp_path)
    result: CompileResult = compile_combo(
        combo,
        cards,
        "fix deck emit frontmatter",
        change="101",
        allow_external=True,
    )
    output_dir = tmp_path / "specs"
    emit(result, output_dir)

    for emitted in sorted(output_dir.glob("*.md")):
        parsed = parse_spec_frontmatter(emitted)
        assert parsed["target_branch"] and isinstance(parsed["target_branch"], str)
        assert parsed["verification"] is not None and isinstance(parsed["verification"], dict)
        assert parsed["parse_error"] is None
        verification = parsed["verification"]
        checks = verification.get("checks")
        tests = verification.get("tests")
        full_suite = verification.get("full_suite")
        frontmatter = yaml.safe_load(emitted.read_text(encoding="utf-8").split("---", 2)[1])
        assert set(frontmatter).issuperset({"target_branch", "verification"})
        assert set(verification).issuperset({"docs_class", "checks", "tests", "full_suite"})
        assert isinstance(checks, list) and checks
        assert any(check.get("kind") == "persona-scope" for check in checks)
        assert any(
            check.get("kind") == "command" and check.get("name") == "policy" for check in checks
        )
        assert isinstance(tests, list) and tests
        assert full_suite is not None
        assert full_suite.get("baseline") == "no-regression"
