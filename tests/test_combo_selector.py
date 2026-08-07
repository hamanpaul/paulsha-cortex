from __future__ import annotations

import importlib

import pytest

from paulsha_cortex.coordinator.work_bridge import default_workflow_manifest
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    load_cards,
    load_combo,
)


def _cards():
    return load_cards(DEFAULT_CARDS_PATH)


def _combo_catalog():
    cards = _cards()
    combos = {}
    for path in sorted(DEFAULT_COMBOS_DIR.glob("*.yaml")):
        combo = load_combo(path, cards)
        combos[combo.id] = combo
    return combos


def _task_types_module():
    try:
        return importlib.import_module("paulsha_cortex.deck.task_types")
    except ModuleNotFoundError as exc:
        if exc.name == "paulsha_cortex.deck.task_types":
            pytest.fail("design-task-type-taxonomy (#139) prerequisite missing", pytrace=False)
        raise


def _selector_module():
    try:
        return importlib.import_module("paulsha_cortex.deck.selector")
    except ModuleNotFoundError as exc:
        if exc.name == "paulsha_cortex.deck.selector":
            pytest.fail("task-type combo selector module is not implemented yet", pytrace=False)
        raise


def _taxonomy():
    return _task_types_module().load_task_types(combos=_combo_catalog())


def _select_combo(titles, *, override=None, default_combo="feature-oneshot"):
    selector = _selector_module()
    return selector.select_combo(
        titles,
        taxonomy=_taxonomy(),
        override=override,
        default_combo=default_combo,
    )


def test_fix_standard_combo_loads_and_passes_schema() -> None:
    combo = load_combo(DEFAULT_COMBOS_DIR / "fix-standard.yaml", _cards())

    assert combo.id == "fix-standard"
    assert combo.task_type == "fix"
    assert [gate.after for gate in combo.gate_spine] == ["verification", "code-review"]


def test_fix_standard_manifest_passes_manager_spine() -> None:
    manifest = default_workflow_manifest("demo-work", change=None, combo_name="fix-standard")

    assert manifest.combo == "fix-standard"
    manifest.validate_manager_spine()


def test_task_types_yaml_maps_fix_to_fix_standard() -> None:
    taxonomy = _taxonomy()

    assert taxonomy.task_types["fix"].combo == "fix-standard"
    assert taxonomy.task_types["feat"].combo == "feature-oneshot"
    assert taxonomy.task_types["docs"].combo is None
    assert taxonomy.task_types["test"].combo is None
    assert taxonomy.task_types["ci"].combo is None
    assert taxonomy.task_types["refactor"].combo is None


def test_select_combo_fix_title_selects_fix_standard() -> None:
    selection = _select_combo({202: "fix(deck): tighten selector wiring"})

    assert selection.combo_id == "fix-standard"
    assert selection.source == "task-type-auto"
    assert selection.task_type == "fix"


def test_select_combo_feat_title_selects_feature_oneshot() -> None:
    selection = _select_combo({202: "feat: add selector orchestration"})

    assert selection.combo_id == "feature-oneshot"
    assert selection.source == "task-type-auto"
    assert selection.task_type == "feat"


def test_select_combo_conflicting_matched_types_fail_closed() -> None:
    selector = _selector_module()

    with pytest.raises(selector.ComboSelectionError) as exc:
        _select_combo(
            {
                101: "feat: add selector orchestration",
                102: "fix(deck): tighten selector wiring",
            }
        )

    message = str(exc.value)
    assert "#101" in message
    assert "#102" in message
    assert "feat" in message
    assert "fix" in message


def test_select_combo_unknown_type_fail_closed() -> None:
    selector = _selector_module()

    with pytest.raises(selector.ComboSelectionError) as exc:
        _select_combo({202: "perf(cli): accelerate combo lookup"})

    message = str(exc.value)
    assert "perf" in message
    assert "feat" in message
    assert "refactor" in message


def test_select_combo_out_of_vocab_scope_fail_closed() -> None:
    selector = _selector_module()

    with pytest.raises(selector.ComboSelectionError) as exc:
        _select_combo({202: "fix(claimx): repair selector"})

    message = str(exc.value)
    assert "claimx" in message
    assert "fix" in message


def test_select_combo_absent_title_bypass_with_marker() -> None:
    selection = _select_combo({202: "repair selector fallback"})

    assert selection.combo_id == "feature-oneshot"
    assert selection.source == "bypass-default"
    assert "absent" in selection.reason


def test_select_combo_unparseable_title_bypass_with_marker() -> None:
    selection = _select_combo({202: "fix(: broken"})

    assert selection.combo_id == "feature-oneshot"
    assert selection.source == "bypass-default"
    assert "unparseable" in selection.reason


def test_select_combo_combo_gap_type_bypass_with_marker() -> None:
    selection = _select_combo({202: "docs: explain selector bypass"})

    assert selection.combo_id == "feature-oneshot"
    assert selection.source == "bypass-default"
    assert selection.task_type == "docs"
    assert "docs" in selection.reason


@pytest.mark.parametrize(
    ("titles", "marker"),
    [
        (None, "snapshot-drift"),
        ({}, "absent"),
    ],
)
def test_select_combo_no_titles_bypass(titles, marker: str) -> None:
    selection = _select_combo(titles)

    assert selection.combo_id == "feature-oneshot"
    assert selection.source == "bypass-default"
    assert marker in selection.reason


def test_select_combo_override_wins_over_auto() -> None:
    selection = _select_combo(
        {
            101: "feat: add selector orchestration",
            102: "fix(deck): tighten selector wiring",
        },
        override="fix-standard",
    )

    assert selection.combo_id == "fix-standard"
    assert selection.source == "explicit-override"
    assert selection.task_type == "fix"


def test_select_combo_override_accepts_combo_absent_from_taxonomy_mapping() -> None:
    """R3：override 只要 combo 存在／可載入就應可用，不受 task-types.yaml 的
    type→combo 映射限制。``mcu-feature`` 是 repo 內實際存在、可 load_combo
    的 legacy combo（``paulsha_cortex/deck/data/combos/mcu-feature.yaml``），
    但沒有任何 task_type 映射到它——純靠 taxonomy 反查會誤判 unknown
    （code review finding A）。
    """

    selection = _select_combo(
        {202: "feat: add selector orchestration"},
        override="mcu-feature",
    )

    assert selection.combo_id == "mcu-feature"
    assert selection.source == "explicit-override"
    assert selection.task_type is None


def test_select_combo_override_unknown_combo_fail_closed() -> None:
    selector = _selector_module()

    with pytest.raises(selector.ComboSelectionError, match="no-such-combo"):
        _select_combo(
            {202: "fix(deck): tighten selector wiring"},
            override="no-such-combo",
        )


def test_select_combo_deterministic() -> None:
    titles = {202: "fix(deck): tighten selector wiring"}

    first = _select_combo(titles)
    second = _select_combo(titles)

    assert first == second
