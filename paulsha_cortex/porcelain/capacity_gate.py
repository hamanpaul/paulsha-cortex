from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Sequence

from paulsha_cortex.control import contract as control_contract
from paulsha_cortex.control.client import read_status

from . import COMMANDS, PorcelainCommand, register

CAPACITY_GATE_SCHEMA = "cortex-porcelain/capacity-gate/v1"

# Issue #136 MVP：哪些 Bash 命令視為「昂貴 headless spawn」（另一顆 agent 的全新
# session），而不是一般的 shell 操作。可用 PSC_CAPACITY_GATE_BASH_PATTERN 覆寫，
# 方便測試與未來擴充（例如新增其他 CLI 的 headless 旗標）而不必改程式碼。
DEFAULT_BASH_HEADLESS_PATTERN = re.compile(
    r"\bcodex\s+exec\b"
    r"|\bcopilot\b(?:\s+-p\b|\s+--prompt\b|\s+suggest\b)"
    r"|\bclaude\s+(?:-p|--print)\b"
)

# 一律視為昂貴 spawn 的工具（不看 tool_input，Task/Agent 本身就是啟動 subagent）。
_ALWAYS_GATED_TOOLS = frozenset({"Task", "Agent"})


def register_commands() -> None:
    if "capacity-gate" in COMMANDS:
        return
    register(
        PorcelainCommand(
            name="capacity-gate",
            help="PreToolUse 容量閘門：忙碌時對 subagent/headless spawn 回 ask",
            run=main,
        )
    )


def _bash_headless_pattern() -> re.Pattern[str]:
    override = os.environ.get("PSC_CAPACITY_GATE_BASH_PATTERN")
    if override:
        return re.compile(override)
    return DEFAULT_BASH_HEADLESS_PATTERN


def classify_tool(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """判斷這次工具呼叫是否屬於「昂貴 spawn」，須經容量閘門判斷。

    Task/Agent 一律視為昂貴 spawn；Bash 只有在命令字串符合 headless launcher
    樣式（例如 `codex exec`、`claude -p`、`copilot -p`）時才視為昂貴 spawn。
    其餘工具（Read/Write/Edit/一般 Bash 等）一律不受本閘門管轄。
    """
    if tool_name in _ALWAYS_GATED_TOOLS:
        return True
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not isinstance(command, str):
            return False
        return bool(_bash_headless_pattern().search(command))
    return False


def evaluate_gate(*, tool_name: str, tool_input: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    """回傳 Claude Code PreToolUse hook 協定的決策 payload。

    - 非昂貴 spawn 的工具：一律 allow（空 dict，不含 hookSpecificOutput），
      讓 Claude Code 依其餘既有規則照常放行，本閘門不介入。
    - 昂貴 spawn（Task/Agent，或符合 headless 樣式的 Bash）：
      - status 為 degraded（讀不到／讀不動 control status.json）時視為「無法確認
        manager 是否忙碌」，保守回 ask——這不是 issue 原文明講的分支，而是安全
        預設：degraded 代表「不知道」，不變量（忙碌時不得無節制 spawn）必須被
        強制執行，不可因為讀不到狀態就靜默放行。
      - `daemon.idle` 明確為 False（daemon 存在且正忙）時回 ask。
      - 其餘情況（daemon 存在且 idle）回 allow。
    """
    if not classify_tool(tool_name, tool_input):
        return {}

    degraded = bool(status.get("degraded"))
    daemon = status.get("daemon")
    daemon_idle = daemon.get("idle") if isinstance(daemon, dict) else None

    busy = degraded or daemon_idle is False
    if not busy:
        return {}

    if degraded:
        reason = (
            "manager daemon 狀態不明（status.json 讀取異常/degraded），"
            "為避免違反容量不變量，先詢問是否仍要開新 subagent/headless。"
        )
    else:
        reason = "manager daemon 目前忙碌（daemon.idle=false），是否仍要開新 subagent/headless？"

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }


def _read_status(status_path: str | None) -> dict[str, Any]:
    if status_path is None:
        return read_status()
    payload = control_contract.read_json(status_path)
    if not isinstance(payload, dict):
        return {"degraded": True, "degraded_reason": "missing"}
    return payload


def _read_stdin_payload(stream: Any) -> dict[str, Any]:
    raw = stream.read()
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="cortex capacity-gate")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser(
        "check",
        help="讀 stdin 的 PreToolUse payload，輸出 hook 決策 JSON 到 stdout",
    )
    check.add_argument(
        "--status-path",
        default=None,
        help="覆寫 control status.json 路徑（測試/fixture 注入用）",
    )

    args = parser.parse_args(list(argv))
    if args.command != "check":
        parser.error(f"unsupported capacity-gate command: {args.command}")
        return 2

    payload = _read_stdin_payload(sys.stdin)
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}

    status = _read_status(args.status_path)
    decision = evaluate_gate(tool_name=tool_name, tool_input=tool_input, status=status)

    sys.stdout.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")
    # 決策一律靠 stdout JSON 表達，exit code 固定 0，避免誤觸發 Claude Code
    # 的「hook 執行失敗」分支（那會直接擋下工具呼叫，語意與「詢問使用者」不同）。
    return 0
