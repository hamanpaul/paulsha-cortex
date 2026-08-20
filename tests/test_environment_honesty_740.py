"""#740：誠實紀律的環境維度。

builder sandbox 刻意比 gate 環境嚴（IPAddressDeny、加固剖面），與變更無關的測試
可能只在 sandbox 紅；#606 的文字少了這一維，誠實的模型跑全套→看到環境紅→依指示
自報 failed，形成 explicit-stop 確定性迴圈（實機 jobs subagent-build-15／-16）。
本檔釘：有宣告 gate 的分支帶 environment-honesty 段（省略而非自貶、宣稱綠仍禁止），
且 `test_policy=none` 的分支（#721 的隔離）一個位元組都不受影響。
"""

from __future__ import annotations

from paulsha_cortex.coordinator import gate_ledger

_ENV = {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"}


def test_declared_gate_hint_carries_environment_honesty() -> None:
    hint = gate_ledger.gate_scope_honesty_hint(_ENV, test_policy="focused")
    assert "Environment honesty" in hint
    assert "in its own gate environment" in hint
    assert "leave that gate out of gate_evidence" in hint
    # #606 的紀律原樣保留：省略 ≠ 可以宣稱綠。
    assert "claiming the gate green stays forbidden" in hint
    assert "NOT evidence that the declared gate is green" in hint


def test_no_gate_card_hint_is_untouched_by_740() -> None:
    """#721 的隔離不變：不要求 gate 的卡拿到的文字不含任何 #740 新句。"""

    hint = gate_ledger.gate_scope_honesty_hint(_ENV, test_policy="none")
    assert "Environment honesty" not in hint
    assert "leave that gate out of gate_evidence" not in hint


def test_no_declared_specs_branch_is_untouched_by_740() -> None:
    hint = gate_ledger.gate_scope_honesty_hint({}, test_policy="focused")
    assert "Environment honesty" not in hint
