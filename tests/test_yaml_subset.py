"""`paulsha_cortex._yaml` subset parser 的回歸測試。

觸發事件（#466 實跑驗證）：`patchmud report` 以 PyYAML `safe_dump` 落盤的
report.yaml 使用 **indentless block sequence**（`key:` 之後、同縮排的 `- ` 序列
——PyYAML 預設輸出形狀），subset parser 原本在 `parse_mapping` 把空值 key 當
空 dict 吞掉、隨即在 dash 行炸 `unexpected indentation`，`cortex model profile`
的 report 讀取整條斷掉。修正只在「空值 key 緊接同縮排 dash」時轉進序列解析，
純擴大接受集。
"""

from __future__ import annotations

import pytest

from paulsha_cortex._yaml import YAMLError, safe_load


def test_indentless_block_sequence_under_mapping_key() -> None:
    text = (
        "leaderboards:\n"
        "  clear_rate:\n"
        "    rows:\n"
        "    - clears: 8\n"
        "      loadout: P0T0R0\n"
        "      model: claude:claude-sonnet-5\n"
        "      runs: 8\n"
        "      value: 1.0\n"
        "    status: ok\n"
    )
    parsed = safe_load(text)
    board = parsed["leaderboards"]["clear_rate"]
    assert board["status"] == "ok"
    # 注意 value 是字串：subset parser 的 scalar 只認 int（isdigit），float
    # 維持原文——envelope mapping 規格本就只吃整數 runs/clears、不用浮點 value。
    assert board["rows"] == [
        {
            "clears": 8,
            "loadout": "P0T0R0",
            "model": "claude:claude-sonnet-5",
            "runs": 8,
            "value": "1.0",
        }
    ]


def test_indentless_sequence_at_top_level_key() -> None:
    text = (
        "runs:\n"
        "- clear: 1\n"
        "  encounter: input-validation-v1\n"
        "  end_reason: commit\n"
        "- clear: 0\n"
        "  encounter: parser-edge-v1\n"
        "  end_reason: 'failed:protocol'\n"
        "schema_version: 1\n"
    )
    parsed = safe_load(text)
    assert parsed["schema_version"] == 1
    assert [row["encounter"] for row in parsed["runs"]] == [
        "input-validation-v1",
        "parser-edge-v1",
    ]
    assert parsed["runs"][1]["end_reason"] == "failed:protocol"


def test_indented_sequence_still_parses() -> None:
    """既有縮排式序列（fixture／手寫 YAML 常見）行為不變。"""
    text = "rows:\n  - model: a\n    runs: 8\n  - model: b\n    runs: 8\n"
    parsed = safe_load(text)
    assert [row["model"] for row in parsed["rows"]] == ["a", "b"]


def test_dash_without_preceding_empty_key_is_still_rejected() -> None:
    """dash 不緊接空值 key（真 malformed）仍要炸，不得被新分支吞掉。"""
    with pytest.raises(YAMLError):
        safe_load("a: 1\n- x\n")
