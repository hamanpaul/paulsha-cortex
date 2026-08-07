"""headless executor 完成後，從 job log 抽取 token usage（Issue #325）。

每個 adapter 只吃 ``log_path``（headless session 的 jsonl log 檔），回傳同一份
結構：``{"usage": dict|None, "usage_raw": dict|None, "usage_reason": str|None}``。

設計原則：
- 純函式、無 registry/launcher 依賴，方便單元測試與獨立驗證。
- fail-soft：任何 I/O／解析失敗一律回傳 ``usage=None`` + 非空 ``usage_reason``，
  絕不對呼叫端拋例外（呼叫端是 ``registry.update_headless_result``，用量抽取
  失敗不得影響 job 的 status/exit_code 判定）。
- 三家 executor 的 log 格式互不相同，且各自藏著容易誤讀的陷阱（見各 adapter
  docstring），對映時務必照著已實測驗證過的欄位語意走，不要照直覺猜。
"""

from __future__ import annotations

import json
from typing import Any, Callable


def _empty_result(reason: str) -> dict[str, Any]:
    return {"usage": None, "usage_raw": None, "usage_reason": reason}


def _read_jsonl_dicts(log_path: str) -> list[dict[str, Any]]:
    """逐行讀 jsonl，跳過空行／非 JSON／非 dict 的行，不拋例外給呼叫端。"""
    entries: list[dict[str, Any]] = []
    with open(log_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                parsed = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
    return entries


def _extract_codex(log_path: str) -> dict[str, Any]:
    """codex：逐行找 ``type == "turn.completed"`` 且含 ``usage`` dict 的**最後一筆**
    （累計值，取最後一筆即最終累計）。"""
    try:
        entries = _read_jsonl_dicts(log_path)
    except OSError as exc:
        return _empty_result(f"codex: log file unreadable: {exc}")
    last_usage: dict[str, Any] | None = None
    for entry in entries:
        if entry.get("type") == "turn.completed":
            usage = entry.get("usage")
            if isinstance(usage, dict):
                last_usage = usage
    if last_usage is None:
        return _empty_result("codex: no turn.completed usage line")
    return {
        "usage": {
            "input_tokens": last_usage.get("input_tokens"),
            "output_tokens": last_usage.get("output_tokens"),
            "cached_input_tokens": last_usage.get("cached_input_tokens"),
            "reasoning_output_tokens": last_usage.get("reasoning_output_tokens"),
            "source": "codex",
        },
        "usage_raw": last_usage,
        "usage_reason": None,
    }


def _extract_claude(log_path: str) -> dict[str, Any]:
    """claude：優先找最後一筆 ``type == "result"`` 且含 ``usage`` dict（累計值）。

    欄位對映陷阱：``cache_read_input_tokens`` 才是「從既有 cache 讀到」，對映到
    job 的 ``cached_input_tokens``；``cache_creation_input_tokens`` 是「這次新
    寫入 cache」，不要誤用。

    缺席時 fallback 逐行累加所有 ``message.usage``（``input_tokens`` /
    ``output_tokens`` / ``cache_read_input_tokens``）。
    """
    try:
        entries = _read_jsonl_dicts(log_path)
    except OSError as exc:
        return _empty_result(f"claude: log file unreadable: {exc}")

    last_result_usage: dict[str, Any] | None = None
    for entry in entries:
        if entry.get("type") == "result":
            usage = entry.get("usage")
            if isinstance(usage, dict):
                last_result_usage = usage

    if last_result_usage is not None:
        return {
            "usage": {
                "input_tokens": last_result_usage.get("input_tokens"),
                "output_tokens": last_result_usage.get("output_tokens"),
                "cached_input_tokens": last_result_usage.get("cache_read_input_tokens"),
                "reasoning_output_tokens": None,
                "source": "claude",
            },
            "usage_raw": last_result_usage,
            "usage_reason": None,
        }

    total_input = 0
    total_output = 0
    total_cached = 0
    found = False
    for entry in entries:
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        found = True
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_read = usage.get("cache_read_input_tokens")
        if isinstance(input_tokens, int):
            total_input += input_tokens
        if isinstance(output_tokens, int):
            total_output += output_tokens
        if isinstance(cache_read, int):
            total_cached += cache_read

    if not found:
        return _empty_result("claude: no result.usage or message.usage found")

    return {
        "usage": {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cached_input_tokens": total_cached,
            "reasoning_output_tokens": None,
            "source": "claude",
        },
        "usage_raw": None,
        "usage_reason": (
            "claude: fallback accumulated from message.usage "
            "(no top-level result.usage)"
        ),
    }


def _extract_copilot(log_path: str) -> dict[str, Any]:
    """copilot：**不要讀最後一行 result.usage**——已實測驗證那不是 token 數
    （是 premiumRequests/duration/codeChanges 統計），若比照 codex/claude 先找
    result.usage 會讀到看似合理但語意完全錯誤的資料，且不會有任何例外。

    改為逐行累加所有 ``type == "assistant.message"`` 的 ``data.outputTokens``；
    ``input_tokens`` 固定 ``None``。
    """
    try:
        entries = _read_jsonl_dicts(log_path)
    except OSError as exc:
        return _empty_result(f"copilot: log file unreadable: {exc}")

    total_output = 0
    found = False
    for entry in entries:
        if entry.get("type") != "assistant.message":
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        output_tokens = data.get("outputTokens")
        if isinstance(output_tokens, int):
            total_output += output_tokens
            found = True

    if not found:
        return _empty_result("copilot: no assistant.message events found")

    return {
        "usage": {
            "input_tokens": None,
            "output_tokens": total_output,
            "cached_input_tokens": None,
            "reasoning_output_tokens": None,
            "source": "copilot",
        },
        "usage_raw": None,
        "usage_reason": "copilot: input tokens unavailable in log format",
    }


def _extract_agy(log_path: str) -> dict[str, Any]:
    """agy：issue 明示無正常樣本、受 headless permission 問題阻塞，先落
    unsupported，不讀檔（那些 log 內容是權限拒絕訊息，沒有 usage 資料可抽）。"""
    del log_path
    return _empty_result(
        "agy: unsupported (headless permission blocks normal session log)"
    )


_EXTRACTORS: dict[str, Callable[[str], dict[str, Any]]] = {
    "codex": _extract_codex,
    "claude": _extract_claude,
    "copilot": _extract_copilot,
    "agy": _extract_agy,
}


def extract_usage(executor: str | None, log_path: str | None) -> dict[str, Any]:
    """dispatch table 入口；整段皆 fail-soft，任何未預期例外一律轉成
    ``usage=None`` + ``usage_reason=str(exc)``，絕不上拋。"""
    try:
        if not executor or not log_path or executor not in _EXTRACTORS:
            return _empty_result("unknown executor or missing log_path")
        # agy 不讀檔（見 _extract_agy），其餘 adapter 各自負責檔案不存在／
        # 不可讀的 fail-soft（open() 拋 OSError 由各自的 try/except 接住）。
        return _EXTRACTORS[executor](log_path)
    except BaseException as exc:  # noqa: BLE001 - fail-soft 邊界，絕不上拋
        return _empty_result(str(exc))
