"""#308：operator 顯式零 gate（ledger gates 為空）時，模型自述的 gate_evidence
不得觸發 gate-evidence-unknown-gate fail-closed。

#261 文件明示「未宣告 gate＝operator 顯式選擇、此設定下沒有 R2 保護」；空 ledger
下沒有可對照的獨立證據層，模型自述本就不構成授權，對照它只會把授權結果變成
模型隨機行為（gpt-5.4 會把 shell 指令如 `pwd` 填進 gate_evidence，W1 批次
job wf-efce4a166b-worktree-isolation-419 實測卡死）。ledger 非空時维持 fail-closed。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator import gate_ledger, terminal_contract


def _passed_envelope(gate_evidence: list[dict[str, object]]) -> object:
    return terminal_contract.validate_envelope(
        {
            "schema_version": terminal_contract.TERMINAL_SCHEMA_VERSION,
            "kind": "workflow-card",
            "status": "passed",
            "run_id": "run",
            "card_id": "card",
            "candidate": "a" * 40,
            "outputs": [],
            "diagnostics": {},
            "gate_evidence": gate_evidence,
        }
    )


def _empty_ledger(tmp_path: Path) -> Path:
    ledger_path = tmp_path / "job.gates.json"
    payload = gate_ledger.write_gate_ledger(
        ledger_path=ledger_path, worktree=tmp_path, env={}
    )
    assert payload["gates"] == []
    return ledger_path


def test_empty_ledger_tolerates_model_claimed_gates(tmp_path: Path) -> None:
    ledger_path = _empty_ledger(tmp_path)
    envelope = _passed_envelope([{"name": "pwd", "status": "passed"}])
    authorization = terminal_contract.authorize_terminal(
        envelope, ledger_path=ledger_path, require_ledger=True
    )
    assert authorization.authorized is True
    assert authorization.verified_gates == ()


def test_empty_ledger_without_claims_still_authorized(tmp_path: Path) -> None:
    ledger_path = _empty_ledger(tmp_path)
    envelope = _passed_envelope([])
    authorization = terminal_contract.authorize_terminal(
        envelope, ledger_path=ledger_path, require_ledger=True
    )
    assert authorization.authorized is True


def test_nonempty_ledger_unknown_claim_still_fail_closed(tmp_path: Path) -> None:
    ledger_path = tmp_path / "job.gates.json"
    gate_ledger.write_gate_ledger(
        ledger_path=ledger_path,
        worktree=tmp_path,
        env={"PSC_GATE_CMD_SMOKE": "python3 -c pass"},
    )
    envelope = _passed_envelope([{"name": "pwd", "status": "passed"}])
    with pytest.raises(terminal_contract.TerminalContractError) as excinfo:
        terminal_contract.authorize_terminal(
            envelope, ledger_path=ledger_path, require_ledger=True
        )
    assert excinfo.value.reason == "gate-evidence-unknown-gate"
