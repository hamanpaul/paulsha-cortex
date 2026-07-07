from __future__ import annotations

import pytest

from paulsha_cortex.deck.schema import (
    Card,
    Combo,
    DeckSchemaError,
    load_cards,
    load_combo,
)

VALID_CARDS = """\
version: 0
cards:
  - id: writing-plans
    kind: skill
    type: interactive
    class: core
    skill_ref: "superpowers:writing-plans"
    requires: ["openspec/changes/<change>/proposal.md"]
    produces: ["docs/superpowers/plans/*<task-slug>*.md"]
    persona_binding: planner
  - id: build-a
    kind: skill
    type: headless
    class: core
    skill_ref: "superpowers:subagent-driven-development"
    slice_group: build
    requires: []
    produces: []
"""

VALID_COMBO = """\
combo:
  id: demo
  task_type: feature
  cards:
    - ref: writing-plans
    - ref: build-a
      depends_on: [writing-plans]
  gate_spine:
    - after: writing-plans
      exists: ["docs/superpowers/plans/*<task-slug>*.md"]
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_load_cards_valid(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    assert set(cards) == {"writing-plans", "build-a"}
    assert cards["writing-plans"].type == "interactive"
    assert cards["build-a"].slice_group == "build"


def test_load_cards_bad_enum_rejects_whole_file(tmp_path):
    bad = VALID_CARDS.replace("type: headless", "type: batch")
    with pytest.raises(DeckSchemaError, match="build-a.*type"):
        load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_cards_wrong_version_rejected(tmp_path):
    bad = VALID_CARDS.replace("version: 0", "version: 99")
    with pytest.raises(DeckSchemaError, match="version"):
        load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_cards_unknown_placeholder_rejected(tmp_path):
    bad = VALID_CARDS.replace("<task-slug>", "<feature-name>")
    with pytest.raises(DeckSchemaError, match="feature-name"):
        load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_cards_malformed_placeholder_rejected(tmp_path):
    bad = VALID_CARDS.replace("<task-slug>", "<task_slug>")
    with pytest.raises(DeckSchemaError, match="task_slug"):
        load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_cards_unknown_field_rejected(tmp_path):
    bad = VALID_CARDS.replace("slice_group: build", "slcie_group: build")
    with pytest.raises(DeckSchemaError, match="slcie_group"):
        load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_cards_non_string_persona_binding_rejected(tmp_path):
    bad = VALID_CARDS.replace("persona_binding: planner", "persona_binding: 123")
    with pytest.raises(DeckSchemaError, match="persona_binding"):
        load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_cards_non_string_provider_binding_rejected(tmp_path):
    bad = VALID_CARDS.replace("produces: []", "produces: []\n    provider_binding: 123")
    with pytest.raises(DeckSchemaError, match="provider_binding"):
        load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_cards_duplicate_id_rejected(tmp_path):
    dup = VALID_CARDS + VALID_CARDS.split("cards:\n")[1]
    with pytest.raises(DeckSchemaError, match="重複"):
        load_cards(_write(tmp_path, "cards.yaml", dup))


def test_load_combo_valid(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    combo = load_combo(_write(tmp_path, "demo.yaml", VALID_COMBO), cards)
    assert combo.id == "demo"
    assert [c.ref for c in combo.cards] == ["writing-plans", "build-a"]


def test_load_combo_unknown_ref_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace("ref: build-a", "ref: no-such-card")
    with pytest.raises(DeckSchemaError, match="no-such-card"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_load_combo_unknown_field_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace("gate_spine:", "gate_spien:")
    with pytest.raises(DeckSchemaError, match="gate_spien"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_load_combo_unknown_card_entry_field_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace("depends_on: [writing-plans]", "depends_om: [writing-plans]")
    with pytest.raises(DeckSchemaError, match="depends_om"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_load_combo_cycle_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace(
        "    - ref: writing-plans\n",
        "    - ref: writing-plans\n      depends_on: [build-a]\n",
    )
    with pytest.raises(DeckSchemaError, match="循環"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_load_combo_unknown_placeholder_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace("<task-slug>", "<feature-name>")
    with pytest.raises(DeckSchemaError, match="feature-name"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_load_combo_malformed_placeholder_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace("<task-slug>", "<Task-Slug>")
    with pytest.raises(DeckSchemaError, match="Task-Slug"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_load_combo_unknown_gate_field_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace("exists:", "exsts:")
    with pytest.raises(DeckSchemaError, match="exsts"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_load_combo_empty_gate_exists_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace(
        'exists: ["docs/superpowers/plans/*<task-slug>*.md"]',
        "exists: []",
    )
    with pytest.raises(DeckSchemaError, match="exists"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_bad_first_card_still_reports_later_duplicate(tmp_path):
    # 對抗審查回歸：首卡壞 enum + 後續重複 id——兩類錯誤都要完整回報，
    # 不得因全域錯誤狀態讓後續卡的 duplicate 偵測失效
    bad_first = VALID_CARDS.replace("type: interactive", "type: batch")
    dup = bad_first + VALID_CARDS.split("cards:\n")[1]
    with pytest.raises(DeckSchemaError) as exc:
        load_cards(_write(tmp_path, "cards.yaml", dup))
    msg = str(exc.value)
    assert "type 非法值" in msg
    assert "重複" in msg


def test_load_cards_empty_or_non_mapping_rejected(tmp_path):
    for content in ("", "[]", "42", "cards: null"):
        with pytest.raises(DeckSchemaError):
            load_cards(_write(tmp_path, "cards.yaml", content))


def test_load_cards_malformed_angle_tokens_rejected(tmp_path):
    # fail-closed：空 <>、巢狀 <<>>、未閉合 <、孤立 > 一律拒絕
    for bad_glob in ("docs/<>.md", "docs/<<task-slug>>.md", "docs/<task-slug.md", "docs/x>y.md"):
        bad = VALID_CARDS.replace("docs/superpowers/plans/*<task-slug>*.md", bad_glob)
        with pytest.raises(DeckSchemaError, match="角括號"):
            load_cards(_write(tmp_path, "cards.yaml", bad))


def test_load_combo_root_non_mapping_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    for content in ("", "[]", "combo: []"):
        with pytest.raises(DeckSchemaError):
            load_combo(_write(tmp_path, "c.yaml", content), cards)


def test_gate_spine_unknown_after_rejected(tmp_path):
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    bad = VALID_COMBO.replace("after: writing-plans", "after: no-such-card")
    with pytest.raises(DeckSchemaError, match="no-such-card"):
        load_combo(_write(tmp_path, "demo.yaml", bad), cards)


def test_gate_spine_non_list_rejected(tmp_path):
    # GitHub review 修正：gate_spine 誤填字串/物件要有明確錯誤，不得逐字元迭代
    cards = load_cards(_write(tmp_path, "cards.yaml", VALID_CARDS))
    for bad_spine in ('gate_spine: "oops"', "gate_spine: {after: writing-plans}"):
        bad_yaml = VALID_COMBO.split("  gate_spine:")[0] + "  " + bad_spine + "\n"
        with pytest.raises(DeckSchemaError, match="gate_spine 必須是清單"):
            load_combo(_write(tmp_path, "demo.yaml", bad_yaml), cards)


def test_load_cards_absolute_or_escape_glob_rejected(tmp_path):
    # W4 對抗審查回歸：produces/requires glob 拒絕絕對路徑與 .. 逃逸（載入層 fail-closed）
    for bad_glob, expect in (
        ("/etc/hosts", "絕對"),
        ("~/x.md", "絕對"),
        ("C:/x.md", "絕對"),
        ("../pyproject.toml", "路徑段"),
        ("docs/../secret.md", "路徑段"),
    ):
        bad = VALID_CARDS.replace("docs/superpowers/plans/*<task-slug>*.md", bad_glob)
        with pytest.raises(DeckSchemaError, match=expect):
            load_cards(_write(tmp_path, "cards.yaml", bad))
