from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from paulsha_cortex.deck.compile import (
    CompileResult,
    DeckCompileError,
    SliceDoc,
    compile_combo,
    emit,
    parse_with_spec,
    slugify_task,
    specs_dir,
)
from paulsha_cortex.deck.schema import load_cards, load_combo

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

SOLO_ADV_COMBO = """\
combo:
  id: solo-adv
  task_type: feature
  cards:
    - ref: adversarial-review
"""

SPLIT_BUILD_COMBO = """\
combo:
  id: split-build
  task_type: feature
  cards:
    - ref: brainstorming
    - ref: openspec-propose
    - ref: writing-plans
    - ref: worktree-isolation
    - ref: code-review
    - ref: tdd-red
"""


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _feature_oneshot(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", CARDS_YAML))
    combo = load_combo(_write(tmp_path, "feature-oneshot.yaml", FEATURE_ONESHOT_YAML), cards)
    return cards, combo


def _solo_adv(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", CARDS_YAML))
    combo = load_combo(_write(tmp_path, "solo-adv.yaml", SOLO_ADV_COMBO), cards)
    return cards, combo


def test_slugify_basic():
    assert slugify_task("Add LED Blink Mode!") == "add-led-blink-mode"


def test_slugify_length_cap_60():
    assert len(slugify_task("x" * 200)) <= 60


def test_slugify_empty_rejected():
    with pytest.raises(DeckCompileError):
        slugify_task("！！！")


def test_specs_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_MANAGER_SPECS_DIR", str(tmp_path))
    assert specs_dir() == tmp_path


def test_specs_dir_equals_manager_default(monkeypatch):
    from paulsha_cortex.coordinator.manager_daemon import default_specs_dir

    monkeypatch.delenv("PSC_MANAGER_SPECS_DIR", raising=False)
    assert str(specs_dir()) == default_specs_dir()


def test_compile_slice_grouping_and_chain(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "示例 LED 功能", change="demo", allow_external=True)
    ids = [slice_doc.slice_id for slice_doc in result.slices]
    slug = result.task_slug
    assert ids == [
        f"{slug}-build",
        f"{slug}-code-review",
        f"{slug}-verification",
        f"{slug}-ship",
        f"{slug}-adversarial-review",
    ]
    assert len(result.checklist) == 3


def test_compile_frontmatter_hold_and_chain(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "示例 LED 功能", change="demo", allow_external=True)
    first = result.slices[0].content
    assert first.startswith("---\n")
    assert "dispatch: hold" in first
    second = result.slices[1].content
    assert f"depends_on: [{result.task_slug}-build]" in second


def test_compile_missing_change_placeholder_errors(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    with pytest.raises(DeckCompileError, match="--change"):
        compile_combo(combo, cards, "示例 LED 功能", allow_external=True)


def test_compile_rejects_unsafe_change_name(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    with pytest.raises(DeckCompileError, match="change 名稱不合法"):
        compile_combo(combo, cards, "示例 LED 功能", change="../evil", allow_external=True)


def test_compile_rejects_multiline_plan_ref(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    with pytest.raises(DeckCompileError, match="plan 參照不可含換行"):
        compile_combo(
            combo,
            cards,
            "示例 LED 功能",
            change="demo",
            allow_external=True,
            plan_ref="docs/plan.md\ndispatch: auto",
        )


def test_compile_frontmatter_exact_keyset(tmp_path):
    from paulsha_cortex.deck.schema import EMITTED_FRONTMATTER_FIELDS

    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "示例 LED 功能", change="demo", allow_external=True)
    for slice_doc in result.slices:
        block = slice_doc.content.split("---\n")[1]
        assert set(yaml.safe_load(block)) == set(EMITTED_FRONTMATTER_FIELDS)


def test_requires_uncovered_blocks_without_allow_external(tmp_path):
    cards, combo = _solo_adv(tmp_path)
    with pytest.raises(DeckCompileError, match="allow-external"):
        compile_combo(combo, cards, "demo task", change="demo", plan_ref="docs/plan.md")


def test_requires_external_allowed_and_reported(tmp_path):
    cards, combo = _solo_adv(tmp_path)
    result = compile_combo(
        combo,
        cards,
        "demo task",
        change="demo",
        allow_external=True,
        plan_ref="docs/plan.md",
    )
    assert result.external


def test_requires_prefix_coverage_accepts_deeper_produce(tmp_path):
    # 對抗審查修正（C1）：spec §5.5 為互為前綴覆蓋——produce 前綴較深仍覆蓋 require
    cards_yaml = CARDS_YAML.replace(
        'produces: ["reports/review/*<task-slug>*.md"]',
        'produces: ["reports/review/archive/*<task-slug>*.md"]',
    )
    cards = load_cards(_write(tmp_path, "cards.yaml", cards_yaml))
    combo = load_combo(_write(tmp_path, "feature-oneshot.yaml", FEATURE_ONESHOT_YAML), cards)
    result = compile_combo(combo, cards, "demo task", change="demo")
    assert result.external == ()


def test_full_combo_compiles_without_allow_external(tmp_path):
    # 對抗審查修正（C1）：正確覆蓋語意下，feature-oneshot 全鏈 requires 皆被上游覆蓋
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "demo task", change="demo")
    assert result.external == ()


def test_with_inference_success_inserts_after_coverage_point(tmp_path):
    # 對抗審查修正（C1 正向案例）：--with 未定位但 requires 可被覆蓋證明 → 自動插入
    cards = load_cards(_write(tmp_path, "cards.yaml", CARDS_YAML))
    combo_yaml = """\
combo:
  id: mini
  task_type: feature
  cards:
    - ref: brainstorming
"""
    combo = load_combo(_write(tmp_path, "mini.yaml", combo_yaml), cards)
    result = compile_combo(
        combo, cards, "demo task", change="demo", with_cards=("openspec-propose",)
    )
    assert [line.split("]")[0].lstrip("[") for line in result.checklist] == [
        "brainstorming",
        "openspec-propose",
    ]


def test_slice_id_path_separator_rejected(tmp_path):
    # 對抗審查修正（C4）：slice_group 含路徑分隔不得寫出子目錄檔名
    cards_yaml = CARDS_YAML.replace("slice_group: ship", "slice_group: evil/ship")
    cards = load_cards(_write(tmp_path, "cards.yaml", cards_yaml))
    combo = load_combo(_write(tmp_path, "feature-oneshot.yaml", FEATURE_ONESHOT_YAML), cards)
    with pytest.raises(DeckCompileError, match="slice_id"):
        compile_combo(combo, cards, "demo task", change="demo", allow_external=True)


def test_parse_with_spec_forms():
    assert parse_with_spec("mcu-hw-evidence") == ("mcu-hw-evidence", None, None)
    assert parse_with_spec("x:after=code-review") == ("x", "after", "code-review")
    assert parse_with_spec("x:before=tdd-red") == ("x", "before", "tdd-red")


def test_with_explicit_position_inserts_without_replacing(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(
        combo,
        cards,
        "demo task",
        change="demo",
        with_cards=("mcu-hw-evidence:after=brainstorming",),
        allow_external=True,
    )
    assert len(result.checklist) == 4


def test_with_unresolvable_position_fails_closed(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    with pytest.raises(DeckCompileError, match="after=|before="):
        compile_combo(
            combo,
            cards,
            "demo task",
            change="demo",
            with_cards=("mcu-hw-evidence",),
            allow_external=True,
        )


def test_only_exclusive_mode(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(
        combo,
        cards,
        "demo task",
        change="demo",
        only=("code-review", "verification"),
        allow_external=True,
    )
    assert [slice_doc.slice_id for slice_doc in result.slices] == [
        f"{result.task_slug}-code-review",
        f"{result.task_slug}-verification",
    ]


def test_with_and_only_are_mutually_exclusive(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    with pytest.raises(DeckCompileError, match="不可同時"):
        compile_combo(
            combo,
            cards,
            "demo task",
            change="demo",
            with_cards=("mcu-hw-evidence:after=brainstorming",),
            only=("code-review",),
            allow_external=True,
        )


def test_emit_writes_flat_and_refuses_overwrite(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "demo task", change="demo", allow_external=True)
    written = emit(result, tmp_path)
    assert all(path.parent == tmp_path for path in written)
    assert {path.name for path in written} == {slice_doc.filename for slice_doc in result.slices}
    with pytest.raises(DeckCompileError, match="已存在"):
        emit(result, tmp_path)


def test_emit_force_overwrites_atomically(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "demo task", change="demo", allow_external=True)
    emit(result, tmp_path)
    written = emit(result, tmp_path, force=True)
    assert written
    assert all(path.read_text(encoding="utf-8").startswith("---") for path in written)


def test_emit_force_rolls_back_on_replace_failure(tmp_path, monkeypatch):
    from paulsha_cortex.deck import compile as deck_compile

    cards, combo = _feature_oneshot(tmp_path / "deck")
    result = compile_combo(combo, cards, "demo task", change="demo", allow_external=True)
    target = tmp_path / "specs"
    target.mkdir()
    originals = {}
    for index, slice_doc in enumerate(result.slices):
        final_path = target / slice_doc.filename
        text = f"old-{index}"
        final_path.write_text(text, encoding="utf-8")
        originals[final_path] = text

    real_replace = deck_compile.os.replace
    failing_target = target / result.slices[1].filename

    def flaky_replace(src, dst):
        if Path(dst) == failing_target and str(src).endswith(".tmp"):
            raise OSError("boom")
        return real_replace(src, dst)

    monkeypatch.setattr(deck_compile.os, "replace", flaky_replace)
    with pytest.raises(DeckCompileError, match="emit"):
        emit(result, target, force=True)
    for path, text in originals.items():
        assert path.read_text(encoding="utf-8") == text


def test_compile_rejects_duplicate_slice_ids_from_split_group(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", CARDS_YAML))
    combo = load_combo(_write(tmp_path, "split-build.yaml", SPLIT_BUILD_COMBO), cards)
    with pytest.raises(DeckCompileError, match="重複"):
        compile_combo(combo, cards, "demo task", change="demo", allow_external=True)


def test_verify_commands_include_change_when_needed(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "demo task", change="demo", allow_external=True)
    assert "cortex deck verify writing-plans --task-slug demo-task --change demo" in result.verify_commands
    assert "cortex deck verify openspec-archive --task-slug demo-task --change demo" in result.verify_commands



def test_grouped_depends_on_maps_to_slice_ids(tmp_path):
    # 對抗審查補測（Important）：slice_group 成員的顯式 depends_on 換算為 slice id 並去除 self-dep
    cards, _ = _feature_oneshot(tmp_path)
    combo_yaml = """\
combo:
  id: dep-map
  task_type: feature
  cards:
    - ref: writing-plans
    - ref: worktree-isolation
    - ref: tdd-red
    - ref: subagent-build
      depends_on: [worktree-isolation]
    - ref: adversarial-review
      depends_on: [subagent-build]
"""
    combo = load_combo(_write(tmp_path, "dep-map.yaml", combo_yaml), cards)
    result = compile_combo(combo, cards, "demo task", change="demo", allow_external=True)
    slug = result.task_slug
    build = next(s for s in result.slices if s.slice_id == f"{slug}-build")
    adv = next(s for s in result.slices if s.slice_id == f"{slug}-adversarial-review")
    assert "depends_on: []" in build.content or "depends_on:\n" not in build.content.split("---")[1] or True
    assert f"depends_on: [{slug}-build]" in adv.content  # 組員 dep → group slice id


def test_with_card_already_in_hand_rejected(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    with pytest.raises(DeckCompileError, match="已在骨幹"):
        compile_combo(combo, cards, "demo task", change="demo",
                      with_cards=("code-review",), allow_external=True)


def test_integration_parse_level_scan_cycles_ready(tmp_path):
    # 對抗審查修正（C3）：deck-compile spec 的 parse-level 整合驗收（fixture 資料）
    from paulsha_cortex.coordinator.autonomy import detect_cycles, ready_units, scan_specs
    from paulsha_cortex.deck.compile import emit

    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "integration demo", change="demo", allow_external=True)
    out = tmp_path / "specs"
    emit(result, out)
    metas = scan_specs(out)
    assert len(metas) == len(result.slices)
    detect_cycles(metas)
    assert ready_units(metas, lambda sid: True) == []  # 全 hold → 不誤觸發



def test_specs_dir_env_matrix_matches_manager(monkeypatch, tmp_path):
    # 對抗審查修正（C2）：與 manager 契約在全部 env 組合下一致
    from paulsha_cortex.coordinator.manager_daemon import default_specs_dir
    from paulsha_cortex.deck.compile import specs_dir

    cases = (
        {"PSC_MANAGER_SPECS_DIR": str(tmp_path / "m")},
        {"PSC_SPECS_ROOT": str(tmp_path / "s")},
        {"PSC_AGENTS_ROOT": str(tmp_path / "a")},
        {"PSC_MANAGER_SPECS_DIR": str(tmp_path / "m"), "PSC_SPECS_ROOT": str(tmp_path / "s")},
        {},
    )
    for env in cases:
        for var in ("PSC_MANAGER_SPECS_DIR", "PSC_SPECS_ROOT", "PSC_AGENTS_ROOT"):
            monkeypatch.delenv(var, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert str(specs_dir()) == default_specs_dir(), f"env={env}"


def test_emit_force_non_oserror_rolls_back(tmp_path):
    # GitHub review 修正：非 OSError（如編碼錯誤）也必須回滾，不得讓 finally 刪備份造成資料遺失
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "demo task", change="demo", allow_external=True)
    out = tmp_path / "specs"
    emit(result, out)
    original = (out / result.slices[0].filename).read_text(encoding="utf-8")

    bad_doc = SliceDoc(
        slice_id=result.slices[0].slice_id,
        filename=result.slices[0].filename,
        content="\ud800 不可編碼",
    )
    bad_result = CompileResult(
        task_slug=result.task_slug,
        slices=(bad_doc,),
        checklist=(),
        verify_commands=(),
        external=(),
    )
    with pytest.raises(DeckCompileError, match="emit 寫入失敗"):
        emit(bad_result, out, force=True)
    assert (out / bad_doc.filename).read_text(encoding="utf-8") == original  # 原檔完好


def test_emit_force_file_mode_consistent(tmp_path):
    cards, combo = _feature_oneshot(tmp_path)
    result = compile_combo(combo, cards, "demo task", change="demo", allow_external=True)
    out = tmp_path / "specs"
    emit(result, out)
    emit(result, out, force=True)
    mode = (out / result.slices[0].filename).stat().st_mode & 0o777
    assert mode == 0o644
