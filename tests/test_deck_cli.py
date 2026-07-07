from __future__ import annotations

from pathlib import Path

from paulsha_cortex.deck import cli as deck_cli

CARDS_YAML = """\
version: 0
cards:
  - id: brainstorming
    kind: skill
    type: interactive
    class: core
    skill_ref: "superpowers:brainstorming"
    requires: []
    produces: ["docs/superpowers/specs/*<task-slug>*-design.md"]
    persona_binding: planner
  - id: openspec-propose
    kind: skill
    type: interactive
    class: core
    skill_ref: "openspec-propose"
    requires: ["docs/superpowers/specs/*<task-slug>*-design.md"]
    produces:
      - "openspec/changes/<change>/proposal.md"
      - "openspec/changes/<change>/tasks.md"
    persona_binding: planner
  - id: writing-plans
    kind: skill
    type: interactive
    class: core
    skill_ref: "superpowers:writing-plans"
    requires: ["openspec/changes/<change>/proposal.md"]
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
  - id: tdd-red
    kind: skill
    type: headless
    class: core
    skill_ref: "superpowers:test-driven-development"
    slice_group: build
    requires: []
    produces: []
    persona_binding: builder
  - id: subagent-build
    kind: skill
    type: headless
    class: core
    skill_ref: "superpowers:subagent-driven-development"
    slice_group: build
    requires: []
    produces: []
    persona_binding: builder
  - id: code-review
    kind: skill
    type: headless
    class: core
    skill_ref: "superpowers:requesting-code-review"
    requires: []
    produces: ["reports/review/*<task-slug>*.md"]
    persona_binding: reviewer
  - id: verification
    kind: skill
    type: headless
    class: core
    skill_ref: "superpowers:verification-before-completion"
    requires: []
    produces: ["reports/verify/*<task-slug>*.md"]
    persona_binding: reviewer
  - id: openspec-archive
    kind: skill
    type: headless
    class: core
    skill_ref: "openspec-archive-change"
    slice_group: ship
    requires: ["openspec/changes/<change>/tasks.md"]
    produces: ["openspec/changes/archive/*<change>*"]
    persona_binding: manager
  - id: policy-commit
    kind: skill
    type: headless
    class: core
    skill_ref: "conventional-commit"
    slice_group: ship
    requires: []
    produces: []
    persona_binding: manager
  - id: adversarial-review
    kind: skill
    type: headless
    class: core
    skill_ref: "codex:adversarial-review"
    requires: ["reports/review/*<task-slug>*.md"]
    produces: ["reports/review/*<task-slug>*-adversarial.md"]
    persona_binding: reviewer
  - id: mcu-hw-evidence
    kind: skill
    type: interactive
    class: niche
    skill_ref: "mcu-coding-skill"
    requires: []
    produces: ["docs/superpowers/specs/*<task-slug>*-hw-evidence.md"]
    persona_binding: planner
"""

FEATURE_ONESHOT_YAML = """\
combo:
  id: feature-oneshot
  task_type: feature
  cards:
    - ref: brainstorming
    - ref: openspec-propose
    - ref: writing-plans
    - ref: worktree-isolation
    - ref: tdd-red
    - ref: subagent-build
    - ref: code-review
    - ref: verification
    - ref: openspec-archive
    - ref: policy-commit
    - ref: adversarial-review
  gate_spine:
    - after: writing-plans
      exists: ["docs/superpowers/plans/*<task-slug>*.md"]
    - after: code-review
      exists: ["reports/review/*<task-slug>*.md"]
"""

MCU_FEATURE_YAML = """\
combo:
  id: mcu-feature
  task_type: mcu-feature
  cards:
    - ref: mcu-hw-evidence
    - ref: writing-plans
    - ref: worktree-isolation
    - ref: tdd-red
    - ref: subagent-build
    - ref: code-review
    - ref: verification
  gate_spine:
    - after: mcu-hw-evidence
      exists: ["docs/superpowers/specs/*<task-slug>*-hw-evidence.md"]
"""


def _seed_fixture(tmp_path: Path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cards_path = tmp_path / "cards.yaml"
    combos_dir = tmp_path / "combos"
    cards_path.write_text(CARDS_YAML, encoding="utf-8")
    combos_dir.mkdir()
    (combos_dir / "feature-oneshot.yaml").write_text(FEATURE_ONESHOT_YAML, encoding="utf-8")
    (combos_dir / "mcu-feature.yaml").write_text(MCU_FEATURE_YAML, encoding="utf-8")
    monkeypatch.setattr(deck_cli, "DEFAULT_CARDS_PATH", cards_path)
    monkeypatch.setattr(deck_cli, "DEFAULT_COMBOS_DIR", combos_dir)
    return cards_path, combos_dir


def test_list_shows_combos(tmp_path, capsys, monkeypatch):
    _seed_fixture(tmp_path, monkeypatch)
    assert deck_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "feature-oneshot" in out and "mcu-feature" in out


def test_compile_dry_run_writes_nothing(tmp_path, capsys, monkeypatch):
    _seed_fixture(tmp_path / "deck", monkeypatch)
    specs_root = tmp_path / "specs"
    specs_root.mkdir()
    monkeypatch.setenv("PSC_MANAGER_SPECS_DIR", str(specs_root))
    rc = deck_cli.main(["compile", "feature-oneshot", "--task", "demo task", "--change", "demo", "--allow-external"])
    assert rc == 0
    assert list(specs_root.iterdir()) == []
    assert "dispatch: hold" in capsys.readouterr().out


def test_compile_emit_writes_hold_specs(tmp_path, monkeypatch):
    _seed_fixture(tmp_path / "deck", monkeypatch)
    specs_root = tmp_path / "specs"
    monkeypatch.setenv("PSC_MANAGER_SPECS_DIR", str(specs_root))
    rc = deck_cli.main(
        ["compile", "feature-oneshot", "--task", "demo task", "--change", "demo", "--allow-external", "--emit"]
    )
    assert rc == 0
    files = sorted(specs_root.glob("*.md"))
    assert files
    assert all("dispatch: hold" in path.read_text(encoding="utf-8") for path in files)


def test_compile_out_file_path_reports_error(tmp_path, capsys, monkeypatch):
    _seed_fixture(tmp_path / "deck", monkeypatch)
    out_file = tmp_path / "not-a-dir"
    out_file.write_text("occupied", encoding="utf-8")
    rc = deck_cli.main(
        [
            "compile",
            "feature-oneshot",
            "--task",
            "demo task",
            "--change",
            "demo",
            "--allow-external",
            "--out",
            str(out_file),
        ]
    )
    assert rc == 1
    assert "deck:" in capsys.readouterr().err
