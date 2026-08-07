"""#324 缺口 A：instance-local combo override 搜尋路徑。

``$PSC_AGENTS_ROOT/config/combos/`` 若存在同 id combo，須優先於套件內建
``paulsha_cortex/deck/data/combos/``；未設定 override 時行為須與現行
``DEFAULT_COMBOS_DIR`` 一致（回歸）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.deck.schema import (
    DEFAULT_COMBOS_DIR,
    DeckSchemaError,
    combo_search_dirs,
    instance_combos_dir,
    iter_combo_files,
    resolve_combo_path,
)

_CUSTOM_COMBO_YAML = """\
combo:
  id: feature-oneshot
  task_type: instance-override
  cards:
    - ref: workflow-claim
"""

_OVERRIDE_ONLY_COMBO_YAML = """\
combo:
  id: override-only
  task_type: instance-override
  cards:
    - ref: workflow-claim
"""


def _instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    agents_root = tmp_path / "agents"
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(agents_root))
    combos_dir = agents_root / "config" / "combos"
    combos_dir.mkdir(parents=True)
    return combos_dir


def test_instance_combos_dir_matches_agents_root_contract(tmp_path, monkeypatch):
    combos_dir = _instance_dir(tmp_path, monkeypatch)
    assert instance_combos_dir() == combos_dir


def test_resolve_combo_path_prefers_instance_local_override(tmp_path, monkeypatch):
    combos_dir = _instance_dir(tmp_path, monkeypatch)
    (combos_dir / "feature-oneshot.yaml").write_text(_CUSTOM_COMBO_YAML, encoding="utf-8")

    resolved = resolve_combo_path("feature-oneshot")

    assert resolved == combos_dir / "feature-oneshot.yaml"
    assert resolved != DEFAULT_COMBOS_DIR / "feature-oneshot.yaml"


def test_resolve_combo_path_falls_back_to_package_dir_when_no_override(tmp_path, monkeypatch):
    # override 目錄本身不存在（autouse fixture 已指到不存在的 tmp 路徑）——
    # 回歸：行為須與現行硬編碼 DEFAULT_COMBOS_DIR 一致。
    resolved = resolve_combo_path("feature-oneshot")

    assert resolved == DEFAULT_COMBOS_DIR / "feature-oneshot.yaml"


def test_resolve_combo_path_finds_override_only_combo(tmp_path, monkeypatch):
    combos_dir = _instance_dir(tmp_path, monkeypatch)
    (combos_dir / "override-only.yaml").write_text(_OVERRIDE_ONLY_COMBO_YAML, encoding="utf-8")

    resolved = resolve_combo_path("override-only")

    assert resolved == combos_dir / "override-only.yaml"


def test_resolve_combo_path_unknown_id_raises_with_searched_dirs(tmp_path, monkeypatch):
    combos_dir = _instance_dir(tmp_path, monkeypatch)

    with pytest.raises(DeckSchemaError) as exc:
        resolve_combo_path("no-such-combo")

    message = str(exc.value)
    assert "no-such-combo" in message
    assert str(combos_dir) in message
    assert str(DEFAULT_COMBOS_DIR) in message


def test_combo_search_dirs_skips_nonexistent_instance_dir():
    # autouse fixture 指到一個不存在的 PSC_AGENTS_ROOT——instance dir 不存在時
    # 只回傳套件內建目錄。
    dirs = combo_search_dirs()

    assert dirs == (DEFAULT_COMBOS_DIR,)


def test_combo_search_dirs_lists_instance_before_package(tmp_path, monkeypatch):
    combos_dir = _instance_dir(tmp_path, monkeypatch)

    dirs = combo_search_dirs()

    assert dirs == (combos_dir, DEFAULT_COMBOS_DIR)


def test_iter_combo_files_instance_overrides_same_id(tmp_path, monkeypatch):
    combos_dir = _instance_dir(tmp_path, monkeypatch)
    (combos_dir / "feature-oneshot.yaml").write_text(_CUSTOM_COMBO_YAML, encoding="utf-8")

    files = dict(iter_combo_files())

    # 同 id 只列一次，且指向 instance-local（覆蓋，不是聯集後兩者並列）。
    assert files["feature-oneshot"] == combos_dir / "feature-oneshot.yaml"


def test_iter_combo_files_includes_instance_only_combo_alongside_package(tmp_path, monkeypatch):
    combos_dir = _instance_dir(tmp_path, monkeypatch)
    (combos_dir / "override-only.yaml").write_text(_OVERRIDE_ONLY_COMBO_YAML, encoding="utf-8")

    files = dict(iter_combo_files())

    assert "override-only" in files
    assert files["override-only"] == combos_dir / "override-only.yaml"
    # 套件內建 combo 仍在（沒有被 override 目錄整批取代）。
    assert "small-fix" in files
    assert files["small-fix"] == DEFAULT_COMBOS_DIR / "small-fix.yaml"


def test_iter_combo_files_sorted_by_id(tmp_path, monkeypatch):
    files = iter_combo_files()
    ids = [combo_id for combo_id, _ in files]

    assert ids == sorted(ids)
