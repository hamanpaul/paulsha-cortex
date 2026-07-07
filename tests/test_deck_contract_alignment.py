from __future__ import annotations

import ast
import re
from pathlib import Path

DECK_DIR = Path("paulsha_cortex/deck")
# 以組字建構 forbidden 清單，避免本檔被字面掃描（第三個測試）誤中
_FORBIDDEN_PREFIXES = tuple("paulsha_cortex." + mod for mod in ("lifecycle", "memory"))
FORBIDDEN = re.compile(r"paulsha_cortex\.(lifecycle|memory)")


def test_frontmatter_fields_match_runtime_contract(tmp_path):
    from paulsha_cortex.deck.schema import EMITTED_FRONTMATTER_FIELDS

    # 真相源：parse_spec_frontmatter 回傳的 meta keys（扣除自身加註的 path）
    from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

    spec = tmp_path / "sample.md"
    spec.write_text(
        "---\ndispatch: hold\nslice_id: x\nplan: p\ndepends_on: []\n---\n",
        encoding="utf-8",
    )
    meta = parse_spec_frontmatter(spec)
    assert set(EMITTED_FRONTMATTER_FIELDS) == set(meta) - {"path"}


def test_deck_package_zero_import_of_lifecycle_and_memory():
    # AST 掃 import 目標（比字面 grep 精確）；deck 每個模組都掃，
    # 即涵蓋「經 deck 自身模組」的 transitive 路徑
    offenders: list[str] = []
    for py in DECK_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                if any(name == p or name.startswith(p + ".") for p in _FORBIDDEN_PREFIXES):
                    offenders.append(f"{py}:{node.lineno}: {name}")
    assert offenders == [], f"deck 包違反零 import 鐵律: {offenders}"


def test_deck_tests_no_literal_forbidden_imports():
    offenders = []
    for py in Path("tests").glob("test_deck_*.py"):
        text = py.read_text(encoding="utf-8")
        if FORBIDDEN.search(text):
            offenders.append(str(py))
    assert offenders == [], f"deck 測試含禁用字面 import: {offenders}"
