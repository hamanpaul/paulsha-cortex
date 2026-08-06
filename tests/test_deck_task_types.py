from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, DeckSchemaError, load_cards, load_combo
from paulsha_cortex.deck.task_types import (
    DEFAULT_TASK_TYPES_PATH,
    TASK_TYPE_VALUES,
    classify_title,
    load_task_types,
)


def _combo_catalog() -> dict[str, object]:
    cards = load_cards(DEFAULT_CARDS_PATH)
    catalog = {}
    for combo_file in sorted(DEFAULT_COMBOS_DIR.glob("*.yaml")):
        combo = load_combo(combo_file, cards)
        catalog[combo.id] = combo
    return catalog


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_task_types_yaml_loads_frozen_six_values() -> None:
    taxonomy = load_task_types(combos=_combo_catalog())

    assert tuple(taxonomy.task_types) == TASK_TYPE_VALUES
    assert taxonomy.task_types["feat"].combo == "feature-oneshot"
    assert taxonomy.task_types["fix"].combo == "fix-standard"
    assert taxonomy.task_types["docs"].combo is None
    assert taxonomy.task_types["test"].combo is None
    assert taxonomy.task_types["ci"].combo is None
    assert taxonomy.task_types["refactor"].combo is None


def test_loader_rejects_unknown_top_level_key(tmp_path: Path) -> None:
    bad = DEFAULT_TASK_TYPES_PATH.read_text(encoding="utf-8") + "unexpected: true\n"
    with pytest.raises(DeckSchemaError, match="unexpected"):
        load_task_types(_write(tmp_path, "task-types.yaml", bad), combos=_combo_catalog())


@pytest.mark.parametrize(
    "replace_with",
    [
        "scopes: []\n",
        "scopes:\n  - cli\n  - cli\n",
        "scopes:\n  - cli\n  - bad scope\n",
    ],
)
def test_loader_rejects_invalid_scopes(tmp_path: Path, replace_with: str) -> None:
    original = DEFAULT_TASK_TYPES_PATH.read_text(encoding="utf-8")
    _, _, tail = original.partition("scopes:\n")
    bad = original.replace("scopes:\n" + tail, replace_with)
    with pytest.raises(DeckSchemaError, match="scope|scopes"):
        load_task_types(_write(tmp_path, "task-types.yaml", bad), combos=_combo_catalog())


def test_classify_matched_with_scope() -> None:
    taxonomy = load_task_types(combos=_combo_catalog())

    classification = classify_title("fix(cli): 修正 exit code", taxonomy)

    assert classification.kind == "matched"
    assert classification.task_type == "fix"
    assert classification.scope == "cli"
    assert classification.disposition == "proceed"


def test_classify_matched_without_scope() -> None:
    taxonomy = load_task_types(combos=_combo_catalog())

    classification = classify_title("feat: 新增選牌", taxonomy)

    assert classification.kind == "matched"
    assert classification.task_type == "feat"
    assert classification.scope is None
    assert classification.disposition == "proceed"


def test_classify_unknown_type() -> None:
    taxonomy = load_task_types(combos=_combo_catalog())

    classification = classify_title("perf(cli): accelerate combo lookup", taxonomy)

    assert classification.kind == "unknown_type"
    assert classification.task_type is None
    assert classification.scope == "cli"
    assert classification.disposition == "fail_closed"
    assert "feat" in classification.reason


def test_classify_out_of_vocab_scope_ambiguous() -> None:
    taxonomy = load_task_types(combos=_combo_catalog())

    classification = classify_title("fix(claimx): 修正", taxonomy)

    assert classification.kind == "ambiguous"
    assert classification.task_type == "fix"
    assert classification.scope == "claimx"
    assert classification.disposition == "fail_closed"


def test_classify_absent_and_unparseable() -> None:
    taxonomy = load_task_types(combos=_combo_catalog())

    absent = classify_title("repair selector fallback", taxonomy)
    unparseable = classify_title("fix(: broken", taxonomy)

    assert absent.kind == "absent"
    assert absent.disposition == "bypass"
    assert unparseable.kind == "unparseable"
    assert unparseable.disposition == "bypass"
