"""Issue #669：`claim` 判定「這個 work item 現在不可 claim」時的耐久記錄。

## 為什麼要有這個檔

`work_bridge.start_canonical_workflow` 過去在 claim 判定 `missing_issue` 時**仍然
建立 run**（`detail` 逐字是「claim 判定需要人工介入即建立 run：missing_issue」）。
自我託管首輪掃描後，24 個 `docs/superpowers/workstreams/*/` work item 因此各自變成
一個永遠不會推進的 `needs_human` run：`current_phase: claim`、`gate_state: running`、
`evidence_refs: []`、`next_actions: []`。

而 `missing_issue` 對 workstream 來說**是預期狀態，不是異常**——
`docs/superpowers/workstreams/cost-governance-cluster/todo.md` 開頭逐字寫著「本
workstream 不對應單一 issue」。系統把一個預期狀態物化成 durable state 的結果是
`attention` 信噪比 1:24，真正該人看的 blocker 被埋掉。

## 這個檔買到什麼

修法是「不建 run」（#669 選項 A）。但**只是不建 run 就是把 fail-loud 換成
fail-silent**：真的該有 issue 卻沒有的 work item 會被靜默略過，噪音變成盲區，方向
是錯的。因此每一次「不建 run 就跳過」都必須在這裡留下一筆 operator 查得到的紀錄：

- 耐久：落在 `<coordinator_root>/not-claimable.json`，daemon 重啟不失憶。
- 可查詢：`build_runtime_status_provider` 把它投影進 `cortex status` 的
  `not_claimable` 區塊（與 `attention` 並列但分開——`attention` 只留可行動的項目）。
- 自我收斂：work item 一旦變成可 claim（例如真的補了 issue），下一次 claim 判定
  就會把該筆記錄清掉（見 :func:`clear`），不會留下永久的假警報。

欄位刻意沿用既有詞彙（`reason`／`detail`／`source` 來自 `diagnostics.DiagnosticReason`
的三要素），不另立一套平行的理由體系。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

__all__ = [
    "LEDGER_SCHEMA",
    "LEDGER_FILENAME",
    "ledger_path",
    "load_ledger",
    "list_entries",
    "record",
    "clear",
]


LEDGER_SCHEMA = "cortex-not-claimable/v1"
LEDGER_FILENAME = "not-claimable.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ledger_path(coordinator_root: str | Path) -> Path:
    """`<coordinator_root>/not-claimable.json`。

    呼叫端多半手上只有 `delivery-journal.json` 的路徑（`work_actions` 的
    ``state_path``），傳 `state_path.parent` 即可——與 `jobs.json` 的推導
    （``resolved_state.parent / "jobs.json"``）同一個慣例。
    """

    return Path(coordinator_root) / LEDGER_FILENAME


def _entry_key(*, repo: str, work_id: str) -> str:
    return f"{repo}::{work_id}"


def _empty() -> dict[str, Any]:
    return {"schema": LEDGER_SCHEMA, "items": {}}


def load_ledger(path: str | Path) -> dict[str, Any]:
    """讀回整份 ledger；檔案不存在＝空 ledger，內容壞掉則 fail-closed。

    壞掉時 raise 而非回空：這份檔案的存在理由就是「不可 claim 的項目必須查得
    到」，靜默當成空的等於把盲區再造一次。
    """

    target = Path(path)
    if not target.exists():
        return _empty()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("not-claimable ledger unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != LEDGER_SCHEMA
        or not isinstance(payload.get("items"), dict)
    ):
        raise ValueError("not-claimable ledger malformed")
    for key, row in payload["items"].items():
        if (
            not isinstance(key, str)
            or not isinstance(row, dict)
            or not isinstance(row.get("repo"), str)
            or not isinstance(row.get("work_id"), str)
            or not isinstance(row.get("reason"), str)
            or key != _entry_key(repo=row["repo"], work_id=row["work_id"])
        ):
            raise ValueError("not-claimable ledger row malformed")
    return payload


def _save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def list_entries(path: str | Path) -> list[dict[str, Any]]:
    """呈現面用的穩定排序清單；ledger 壞掉或不存在時回空清單。

    這是唯一一個對壞檔案容忍的入口——`cortex status` 的呈現不得因為一份輔助
    紀錄壞掉就整份死掉（比照 `manager_daemon` 對 `list_workflow_runs()` 的
    try/except 慣例）。寫入路徑（:func:`record`／:func:`clear`）仍然 fail-closed。
    """

    try:
        payload = load_ledger(path)
    except ValueError:
        return []
    return [payload["items"][key] for key in sorted(payload["items"])]


def record(
    path: str | Path,
    *,
    repo: str,
    work_id: str,
    reason: str,
    detail: str,
    source: str,
    next_step_hint: str,
    authority_digest: str | None = None,
    mapped_openspec: Iterable[str] = (),
    mapped_todo_paths: Iterable[str] = (),
    stale_run_id: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """記一筆「這個 work item 現在不可 claim」，回傳落盤後的 row。

    重複觀測（每個 tick 都會再判一次）不新增 row，只更新 ``last_observed_at``
    與 ``observations``；``first_observed_at`` 永遠是第一次觀測的時間，operator
    因此看得出「這件事卡多久了」。
    """

    if not isinstance(repo, str) or not repo:
        raise ValueError("not-claimable record requires repo")
    if not isinstance(work_id, str) or not work_id:
        raise ValueError("not-claimable record requires work_id")
    if not isinstance(reason, str) or not reason:
        raise ValueError("not-claimable record requires reason")
    target = Path(path)
    payload = load_ledger(target)
    key = _entry_key(repo=repo, work_id=work_id)
    observed_at = now if isinstance(now, str) and now else _utcnow()
    previous = payload["items"].get(key)
    row = {
        "repo": repo,
        "work_id": work_id,
        "reason": reason,
        "detail": detail,
        "source": source,
        "next_step_hint": next_step_hint,
        "authority_digest": authority_digest,
        "mapped_openspec": list(mapped_openspec),
        "mapped_todo_paths": list(mapped_todo_paths),
        "stale_run_id": stale_run_id,
        "first_observed_at": (
            previous.get("first_observed_at")
            if isinstance(previous, dict) and isinstance(previous.get("first_observed_at"), str)
            else observed_at
        ),
        "last_observed_at": observed_at,
        "observations": (
            previous.get("observations", 0) + 1
            if isinstance(previous, dict) and isinstance(previous.get("observations"), int)
            else 1
        ),
    }
    payload["items"][key] = row
    _save(target, payload)
    return dict(row)


def clear(path: str | Path, *, repo: str, work_id: str) -> bool:
    """work item 重新變成可 claim 時移除該筆記錄；沒有該筆時完全不寫檔。"""

    target = Path(path)
    if not target.exists():
        return False
    payload = load_ledger(target)
    key = _entry_key(repo=repo, work_id=work_id)
    if key not in payload["items"]:
        return False
    del payload["items"][key]
    _save(target, payload)
    return True
