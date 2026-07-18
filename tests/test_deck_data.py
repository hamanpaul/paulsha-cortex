from __future__ import annotations

from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo

# feature-delivery-pipeline SKILL.md 的 11 個 phase → card id（1:1）
PHASE_CARDS = [
    "workflow-claim",      # 0 Manager durable claim
    "brainstorming",        # 1 scope/brainstorm
    "openspec-propose",     # 2 propose
    "writing-plans",        # 3 plan
    "worktree-isolation",   # 4 worktree（slice_group: build）
    "tdd-red",              # 5 TDD（slice_group: build）
    "subagent-build",       # 6 subagent execution（slice_group: build）
    "verification",         # 7 deterministic verify
    "code-review",          # 8 foreign review
    "adversarial-review",   # 9 adversarial review
    "openspec-archive",     # 9 archive（slice_group: ship）
    "policy-commit",        # 10 policy gate + commit（slice_group: ship）
]

MCU_FEATURE_CARDS = [
    "mcu-hw-evidence",
    "writing-plans",
    "worktree-isolation",
    "tdd-red",
    "subagent-build",
    "code-review",
    "receiving-code-review",
    "verification",
]


def test_cards_yaml_loads_and_covers_11_phases():
    cards = load_cards(DEFAULT_CARDS_PATH)
    for cid in PHASE_CARDS:
        assert cid in cards, f"缺 phase 卡: {cid}"


def test_interactive_headless_typing():
    cards = load_cards(DEFAULT_CARDS_PATH)
    interactive = {c.id for c in cards.values() if c.type == "interactive"}
    assert interactive == {
        "brainstorming",
        "openspec-propose",
        "writing-plans",
        "mcu-hw-evidence",
    }
    assert {c.id for c in cards.values() if c.type == "headless"} == (set(PHASE_CARDS) - interactive) | {"receiving-code-review"}
    assert cards["worktree-isolation"].slice_group == "build"
    assert cards["tdd-red"].slice_group == "build"
    assert cards["subagent-build"].slice_group == "build"
    assert cards["openspec-archive"].slice_group == "ship"
    assert cards["policy-commit"].slice_group == "ship"


def test_feature_oneshot_combo_loads():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    assert combo.id == "feature-oneshot"
    assert combo.task_type == "feature"
    assert [c.ref for c in combo.cards] == PHASE_CARDS
    assert [(gate.after, gate.exists) for gate in combo.gate_spine] == [
        ("writing-plans", ("docs/superpowers/plans/*<task-slug>*.md",)),
        ("verification", ("reports/verify/*<task-slug>*.md",)),
        ("code-review", ("reports/review/*<task-slug>*.md",)),
        ("adversarial-review", ("reports/review/*<task-slug>*-adversarial.md",)),
        ("openspec-archive", ("openspec/changes/archive/*<change>*",)),
    ]


def test_mcu_feature_combo_loads():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "mcu-feature.yaml", cards)
    assert combo.id == "mcu-feature"
    assert combo.task_type == "mcu-feature"
    assert cards["mcu-hw-evidence"].card_class == "niche"
    assert cards["mcu-hw-evidence"].skill_ref == "mcu-coding-skill"
    assert [entry.ref for entry in combo.cards] == MCU_FEATURE_CARDS
    assert [(gate.after, gate.exists) for gate in combo.gate_spine] == [
        ("mcu-hw-evidence", ("docs/superpowers/specs/*<task-slug>*-hw-evidence.md",)),
    ]


def test_mcu_feature_real_data_compiles_to_hold_specs(tmp_path):
    from paulsha_cortex.coordinator.autonomy import detect_cycles, ready_units, scan_specs
    from paulsha_cortex.deck.compile import compile_combo, emit

    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "mcu-feature.yaml", cards)
    result = compile_combo(combo, cards, "cc2674 pwm led bring-up", allow_external=True)
    slug = result.task_slug
    # 對抗審查補強：鎖定 slice 結構（build 三卡合組 + 三個獨立 slice）
    assert [s.slice_id for s in result.slices] == [
        f"{slug}-build",
        f"{slug}-code-review",
        f"{slug}-receiving-code-review",
        f"{slug}-verification",
    ]
    # external 精確內容：mcu 任務接續既有 plan，writing-plans 的 proposal requires
    # 無上游（combo 無 openspec-propose）→ 誠實標記為 external
    assert result.external == (
        "writing-plans: openspec/changes/<change>/proposal.md",
    )
    assert f"cortex deck verify mcu-hw-evidence --task-slug {slug}" in result.verify_commands
    # hw-evidence gate 與卡 produces 一致
    assert combo.gate_spine[0].exists == cards["mcu-hw-evidence"].produces
    out = tmp_path / "specs"
    emit(result, out)
    metas = scan_specs(out)
    assert len(metas) == len(result.slices)
    detect_cycles(metas)
    assert ready_units(metas, lambda sid: True) == []


def test_feature_oneshot_real_data_compiles_to_hold_specs(tmp_path):
    # W7 整合驗證：真實資料 feature-oneshot 全鏈 compile→emit→parse-level
    from paulsha_cortex.coordinator.autonomy import detect_cycles, ready_units, scan_specs
    from paulsha_cortex.deck.compile import compile_combo, emit

    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "w7 closeout demo", change="deck-cards-combo-phase-a")
    slug = result.task_slug
    assert result.external == ()  # 全鏈 requires 覆蓋，無需 --allow-external
    assert [s.slice_id for s in result.slices] == [
        f"{slug}-build",
        f"{slug}-verification",
        f"{slug}-code-review",
        f"{slug}-adversarial-review",
        f"{slug}-ship",
    ]
    assert len(result.checklist) == 3  # 三張 interactive 前置卡
    out = tmp_path / "specs"
    emit(result, out)
    metas = scan_specs(out)
    assert len(metas) == 5
    assert {meta["slice_id"] for meta in metas} == {
        f"{slug}-build",
        f"{slug}-verification",
        f"{slug}-code-review",
        f"{slug}-adversarial-review",
        f"{slug}-ship",
    }
    detect_cycles(metas)
    assert ready_units(metas, lambda sid: True) == []  # 全 hold 不誤觸發
