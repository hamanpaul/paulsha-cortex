"""#261：gate ledger writer 測試。

重點在「ledger 不是模型自述」這條性質：gate 清單來自 operator 的環境變數宣告，
exit code 來自真實 subprocess，模型無法參與。因此本檔刻意跑真的 `true`／`false`
等命令，而不是注入假的 runner——注入假 runner 只會驗到我自己寫的假資料。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import gate_ledger, terminal_contract
from paulsha_cortex.coordinator.launcher import build_wrapper_script


def test_gate_specs_come_from_operator_env_not_model_output() -> None:
    """gate 清單只能由 operator 以環境變數宣告，且拒絕 shell wrapper。"""

    specs = gate_ledger.load_gate_specs(
        {
            "PSC_GATE_CMD_PYTEST": "python3 -m pytest -q",
            "PSC_GATE_CMD_OPENSPEC": "openspec validate --strict",
            "PSC_GATE_CMD_DISABLED": "   ",
            "UNRELATED": "python3 -m evil",
        }
    )
    # 依 gate 名排序，確保 ledger 內容對同一組宣告是確定性的。
    assert [spec.name for spec in specs] == ["openspec", "pytest"]
    assert specs[1].argv == ("python3", "-m", "pytest", "-q")
    # 空值等同未宣告，不會產生一個名為 disabled 的 gate。
    assert "disabled" not in {spec.name for spec in specs}

    # shell wrapper 會讓任意字串重新變成可注入的 shell 片段，必須拒絕。
    with pytest.raises(gate_ledger.GateSpecError):
        gate_ledger.load_gate_specs({"PSC_GATE_CMD_EVIL": "bash -c 'rm -rf /'"})


def test_ledger_records_real_exit_codes(tmp_path: Path) -> None:
    """ledger 的 status 來自真實 subprocess 的 exit code，不是任何人的宣告。"""

    ledger_path = tmp_path / "job.gates.json"
    payload = gate_ledger.write_gate_ledger(
        ledger_path=ledger_path,
        worktree=tmp_path,
        env={
            "PSC_GATE_CMD_GREEN": "python3 -c pass",
            "PSC_GATE_CMD_RED": (
                'python3 -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"'
            ),
            "PSC_SLICE_ID": "slice-1",
        },
    )
    outcomes = {row["name"]: row for row in payload["gates"]}
    assert outcomes["green"]["exit_code"] == 0
    assert outcomes["green"]["status"] == "passed"
    assert outcomes["red"]["exit_code"] != 0
    assert outcomes["red"]["status"] == "failed"
    # 失敗要帶可操作的 detail（stderr 節錄），operator 才不用翻 log。
    assert outcomes["red"]["detail"]
    assert payload["slice_id"] == "slice-1"

    # 落盤內容與回傳一致，且可被 harvest 端直接讀回。
    read_back = terminal_contract.read_gate_ledger(ledger_path)
    assert read_back is not None
    assert read_back[0] == json.loads(ledger_path.read_text(encoding="utf-8"))


def test_unrunnable_gate_is_recorded_as_failed_not_skipped(tmp_path: Path) -> None:
    """跑不起來的 gate 必須記為 failed；否則 operator 設定壞掉會靜默 fail-open。"""

    ledger_path = tmp_path / "job.gates.json"
    payload = gate_ledger.write_gate_ledger(
        ledger_path=ledger_path,
        worktree=tmp_path,
        env={"PSC_GATE_CMD_MISSING": "definitely-not-a-real-binary-261"},
    )
    row = payload["gates"][0]
    assert row["status"] == "failed"
    assert row["exit_code"] != 0

    # 而且這份 ledger 會讓自稱 passed 的 terminal fail closed。
    envelope = terminal_contract.validate_envelope(
        {
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
    )
    with pytest.raises(terminal_contract.GateContradictionError) as excinfo:
        terminal_contract.authorize_terminal(
            envelope, ledger_path=ledger_path, require_ledger=True
        )
    assert excinfo.value.gate == "missing"


def test_ledger_is_written_even_with_no_declared_gates(tmp_path: Path) -> None:
    """沒有宣告 gate 時仍要寫出 ledger——它的存在證明 wrapper 確實跑完了。"""

    ledger_path = tmp_path / "job.gates.json"
    payload = gate_ledger.write_gate_ledger(
        ledger_path=ledger_path, worktree=tmp_path, env={}
    )
    assert payload["gates"] == []
    assert ledger_path.is_file()


def test_wrapper_runs_gate_writer_before_sentinel_and_survives_model_failure(
    tmp_path: Path,
) -> None:
    """wrapper 必須在模型失敗時也寫出 sentinel 與 ledger（以 `;` 串接而非 `&&`）。"""

    sentinel = tmp_path / "s.exit"
    ledger = tmp_path / "s.gates.json"
    script = build_wrapper_script(
        # 讓「模型」以非 0 結束。
        inner_argv=["python3", "-c", "raise SystemExit(9)"],
        sentinel=str(sentinel),
        ledger=str(ledger),
        worktree=str(tmp_path),
        repo_root=str(Path(__file__).resolve().parents[1]),
        run_gates=True,
    )
    # 模型 exit code 先保存；gate 完成後才寫 sentinel，最後還原模型 exit code。
    assert script.index("__psc_rc=$?") < script.index("gate_ledger")
    assert script.index("gate_ledger") < script.index('printf %s "$__psc_rc"')
    assert script.endswith('exit "$__psc_rc"')

    import subprocess

    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=str(tmp_path),
        env={**os.environ, "PSC_GATE_CMD_GREEN": "python3 -c pass"},
        check=False,
        capture_output=True,
    )
    assert proc.returncode == 9
    assert sentinel.read_text(encoding="utf-8") == "9"
    ledger_payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert ledger_payload["gates"][0]["name"] == "green"
    assert ledger_payload["gates"][0]["status"] == "passed"


def test_wrapper_gate_output_never_pollutes_terminal_log(tmp_path: Path) -> None:
    """gate 階段的輸出不得混進 JSONL log，否則會污染 terminal evidence 解析。"""

    script = build_wrapper_script(
        inner_argv=["true"],
        sentinel=str(tmp_path / "s.exit"),
        ledger=str(tmp_path / "s.gates.json"),
        worktree=str(tmp_path),
        repo_root="/repo",
        run_gates=True,
    )
    gate_command = script[script.index("PYTHONPATH=") :].split("; ", 1)[0]
    assert gate_command.endswith(">/dev/null 2>&1")
    assert script.index(gate_command) < script.index('printf %s "$__psc_rc"')
