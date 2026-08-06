"""#313：verify phase 不得要求 gate ledger。

verification 卡以 review-only 沙箱啟動，`launcher._should_run_gates` 對
review-only／read-only 明確回 False——wrapper 不含 ledger 階段。若
`GATE_LEDGER_REQUIRED_PHASES` 仍含 verify，verification 卡的 passed terminal
一律「沒有可重驗的 gate ledger」fail-closed（結構性永不可過）。verify 的獨立
證據層是 deterministic verification report 管線，不是 wrapper gate ledger；
build phase 的 ledger 要求維持不變。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, terminal_contract


def _passed_raw() -> dict[str, object]:
    return {
        "schema_version": terminal_contract.TERMINAL_SCHEMA_VERSION,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
        "diagnostics": {},
        "gate_evidence": [],
    }


def test_gate_ledger_required_phases_is_build_only() -> None:
    assert manager.GATE_LEDGER_REQUIRED_PHASES == frozenset({"build"})


def test_verify_phase_passed_without_ledger_is_accepted(tmp_path: Path) -> None:
    job = {
        "workflow_phase": "verify",
        "log_path": str(tmp_path / "job.jsonl"),
    }
    manager._assert_terminal_gate_consistency(_passed_raw(), job=job)


def test_build_phase_passed_without_ledger_still_fail_closed(tmp_path: Path) -> None:
    job = {
        "workflow_phase": "build",
        "log_path": str(tmp_path / "job.jsonl"),
    }
    with pytest.raises(terminal_contract.TerminalContractError):
        manager._assert_terminal_gate_consistency(_passed_raw(), job=job)
