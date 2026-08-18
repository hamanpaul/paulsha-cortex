"""Issue #372：排程觸發的跨系統 digest 出口。

`build_runtime_status_provider`（見 `manager_daemon.py`）與 `control.client.read_status`
已把 manager 的 `attention`／`ready`／`held`／`degraded`／`recent_done` 彙整成單一快照，
但目前只有拉取式介面（`cortex status` / `cortex inspect status`）；沒有排程觸發、
可推播到其他系統的出口，`coordinator_telegram_notifier.py`（孤兒腳本，僅單元測試
import、production 零呼叫）也不是可信的既有模式。

本模組把既有 status 快照組裝成一份結構化 digest（JSON envelope，另含一段人類可讀
摘要文字），並提供兩種投遞方式：

- 檔案 outbox（預設、無外部依賴 fallback）：寫入
  ``<coordinator_root>/digest/outbox/<timestamp>-<random>.json``。
- 可設定命令（``PSC_DIGEST_DELIVERY_CMD``）：typed argv（比照 `preflight.py` 的
  ``PSC_PREFLIGHT_CMD`` 慣例，``shlex.split`` 解析、``shell=False``、逾時保護），
  把 digest JSON 從 stdin pipe 給該命令。

刻意不 import 任何 custom-skills 套件（含 `reply_bridge.py`）——那不屬 cortex，
維持 cortex 對外零 runtime 依賴的定位；也不把孤兒 `coordinator_telegram_notifier.py`
接上，它只驗證過單元測試，未曾在 production 跑過。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from paulsha_cortex.config import paths

from ..control import contract
from ..control.client import read_status

DIGEST_SCHEMA = "cortex-coordinator/digest/v1"
DELIVERY_CMD_ENV = "PSC_DIGEST_DELIVERY_CMD"
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 10.0

Runner = Callable[..., object]


class DigestDeliveryError(RuntimeError):
    """``PSC_DIGEST_DELIVERY_CMD`` 執行失敗或逾時；fail-closed，不靜默回退檔案 outbox。"""

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def assemble_digest(status: Mapping[str, Any], *, now: str) -> dict[str, Any]:
    """把 `read_status()` 快照彙整成結構化 digest（JSON-ready，含人類可讀摘要）。"""

    attention = _as_list(status.get("attention"))
    # #669：claim 判定不可 claim（`missing_issue`）而刻意不建 run 的 work item。
    # 只帶計數不帶明細——digest 是推播摘要，明細留給 `cortex status`；但計數必須
    # 在，否則「被跳過的項目」在推播面完全不存在，等於再造一次盲區。
    not_claimable = _as_list(status.get("not_claimable"))
    ready = _as_list(status.get("ready"))
    held = _as_list(status.get("held"))
    recent_done = _as_list(status.get("recent_done"))
    degraded = bool(status.get("degraded", False))
    degraded_reason = status.get("degraded_reason")

    digest: dict[str, Any] = {
        "schema": DIGEST_SCHEMA,
        "generated_at": now,
        "status_updated_at": status.get("updated_at"),
        "degraded": degraded,
        "degraded_reason": degraded_reason if isinstance(degraded_reason, str) else None,
        "counts": {
            "attention": len(attention),
            "not_claimable": len(not_claimable),
            "ready": len(ready),
            "held": len(held),
            "recent_done": len(recent_done),
        },
        "attention": attention,
        "ready": ready,
        "held": held,
        "recent_done": recent_done,
    }
    digest["summary_text"] = render_digest_text(digest)
    return digest


def _format_attention_row(row: Any) -> str:
    if not isinstance(row, Mapping):
        return f"- {row}"
    slice_id = row.get("slice_id", "-")
    reason = row.get("reason") or "-"
    return f"- {slice_id}: {reason}"


def _format_held_row(row: Any) -> str:
    if not isinstance(row, Mapping):
        return f"- {row}"
    slice_id = row.get("slice_id", "-")
    reasons = row.get("reasons")
    reasons_text = ", ".join(str(item) for item in reasons) if isinstance(reasons, (list, tuple)) else "-"
    return f"- {slice_id}: {reasons_text}"


def _format_recent_done_row(row: Any) -> str:
    if not isinstance(row, Mapping):
        return f"- {row}"
    slice_id = row.get("slice_id", "-")
    gate_status = row.get("gate_status", "-")
    at = row.get("at", "-")
    return f"- {slice_id}: {gate_status} @ {at}"


def render_digest_text(digest: Mapping[str, Any]) -> str:
    """把 `assemble_digest()` 的結構化輸出算成人類可讀的多行摘要（不含 `summary_text` 自身）。"""

    counts = digest.get("counts", {}) if isinstance(digest.get("counts"), Mapping) else {}
    lines = [
        f"digest @ {digest.get('generated_at')}",
        f"status_updated_at: {digest.get('status_updated_at')}",
        f"degraded: {digest.get('degraded')} ({digest.get('degraded_reason')})",
        (
            f"attention={counts.get('attention', 0)} "
            f"not_claimable={counts.get('not_claimable', 0)} "
            f"ready={counts.get('ready', 0)} "
            f"held={counts.get('held', 0)} recent_done={counts.get('recent_done', 0)}"
        ),
    ]
    attention = _as_list(digest.get("attention"))
    if attention:
        lines.append("attention:")
        lines.extend(_format_attention_row(row) for row in attention)
    ready = _as_list(digest.get("ready"))
    if ready:
        lines.append("ready:")
        lines.extend(f"- {item}" for item in ready)
    held = _as_list(digest.get("held"))
    if held:
        lines.append("held:")
        lines.extend(_format_held_row(row) for row in held)
    recent_done = _as_list(digest.get("recent_done"))
    if recent_done:
        lines.append("recent_done:")
        lines.extend(_format_recent_done_row(row) for row in recent_done)
    return "\n".join(lines)


def outbox_dir() -> Path:
    """``cortex digest emit`` 預設的無外部依賴 fallback：檔案 outbox 目錄。"""

    return paths.coordinator_root() / "digest" / "outbox"


def _outbox_filename(now_iso: str) -> str:
    token: str | None = None
    if isinstance(now_iso, str) and now_iso:
        try:
            parsed = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        except ValueError:
            token = None
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            token = parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if not token:
        token = re.sub(r"[^0-9A-Za-z]+", "-", str(now_iso)).strip("-") or "unknown"
    # uuid4 尾碼避免同一秒（甚至同一注入時間戳，測試常見）內多次 emit 互相覆寫。
    return f"{token}-{uuid4().hex[:8]}.json"


def write_outbox_digest(digest: Mapping[str, Any], *, outbox_root: Path | None = None) -> Path:
    """把 digest 原子寫入檔案 outbox，回傳落檔路徑。"""

    target_dir = outbox_root if outbox_root is not None else outbox_dir()
    target = Path(target_dir) / _outbox_filename(str(digest.get("generated_at") or ""))
    contract.atomic_write_json(target, dict(digest))
    return target


def load_delivery_command(env: Mapping[str, str] | None = None) -> tuple[str, ...] | None:
    """讀取 ``PSC_DIGEST_DELIVERY_CMD``；未設定（或空白）時回傳 ``None``（採檔案 outbox fallback）。"""

    source = os.environ if env is None else env
    raw = source.get(DELIVERY_CMD_ENV, "").strip()
    if not raw:
        return None
    try:
        command = tuple(shlex.split(raw))
    except ValueError as exc:
        raise ValueError(f"{DELIVERY_CMD_ENV} is malformed: {exc}") from exc
    if not command:
        raise ValueError(f"{DELIVERY_CMD_ENV} must not be blank once set")
    return command


def deliver_via_command(
    digest: Mapping[str, Any],
    command: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    timeout: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """把 digest 以 JSON 從 stdin pipe 給 ``command``（typed argv，``shell=False``，逾時保護）。"""

    payload = json.dumps(digest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        result = runner(
            list(command),
            input=payload,
            shell=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DigestDeliveryError(
            f"digest delivery command timed out after {timeout}s: {shlex.join(command)}",
            command=command,
        ) from exc
    returncode = getattr(result, "returncode", None)
    stderr_raw = getattr(result, "stderr", b"")
    stderr = stderr_raw.decode("utf-8", errors="replace") if isinstance(stderr_raw, bytes) else str(stderr_raw or "")
    if returncode != 0:
        raise DigestDeliveryError(
            f"digest delivery command failed (exit {returncode}): {shlex.join(command)}",
            command=command,
            returncode=returncode if isinstance(returncode, int) else None,
            stderr=stderr,
        )
    return {"command": list(command), "returncode": returncode, "stderr": stderr}


def emit_digest(
    *,
    status_provider: Callable[[], Mapping[str, Any]] = read_status,
    now_fn: Callable[[], str] = contract.utcnow,
    env: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    outbox_root: Path | None = None,
    timeout: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """組裝並投遞一份 digest；供 `cortex digest emit` 與外部 timer/cron 呼叫的單一入口。

    投遞方式二擇一，不 fallback：``PSC_DIGEST_DELIVERY_CMD`` 已設定時一律走該命令，
    命令失敗（非零 exit／逾時）直接 raise `DigestDeliveryError`，不會靜默改寫檔案
    outbox（避免掩蓋操作端誤設定）；未設定時才寫檔案 outbox。
    """

    status = status_provider()
    now = now_fn()
    digest = assemble_digest(status, now=now)
    command = load_delivery_command(env=env)
    if command is not None:
        result = deliver_via_command(digest, command, runner=runner, timeout=timeout)
        delivery: dict[str, Any] = {"method": "command", **result}
    else:
        path = write_outbox_digest(digest, outbox_root=outbox_root)
        delivery = {"method": "file", "path": str(path)}
    return {"schema": DIGEST_SCHEMA, "digest": digest, "delivery": delivery}
