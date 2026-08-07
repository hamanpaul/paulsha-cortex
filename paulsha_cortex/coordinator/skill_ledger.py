"""Skill usage ledger（issue #204）：append-only、可去重的 skill 執行事件記錄。

責任邊界：本模組只認 terminal WorkflowRun/card execution evidence（已完成的
job snapshot），從中萃取「這次執行動用了哪個 skill、結果如何」寫成固定欄位
的一行 JSON 事件，append 進 `paths.skill_usage_ledger_path()`（預設
`~/.agents/registry/skill_usage.jsonl`，`PSC_AGENTS_ROOT` 可整族覆寫）。

刻意不做的事：
- 不把統計寫回 card metadata（`deck/schema.py` 的 Card 維持唯讀資料）。
- 不記錄 job 的 stdout/stderr/handoff payload 等任何自由格式內容——事件欄位
  白名單固定為 `LEDGER_FIELDS`，天生不含 secret／敏感 payload。
- 不判斷 cold-skill／park——那是 `skill_janitor` 的責任，本模組只管記帳與
  聚合讀出（`load_usage_summary`）。

去重：`event_id = "<job_id>:<card_id>:<outcome>"`。同一 terminal job 重放
`append_usage_event` 兩次，ledger 只會有一行——第二次呼叫讀到既有 event_id
即直接回傳（不重複寫入），冪等而非報錯。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from paulsha_cortex.config import paths
from paulsha_cortex.deck.schema import Card

SCHEMA_VERSION = 1

# 白名單：build_event() 的輸出只會有這些 key（順序即序列化前的邏輯順序；
# 實際落盤用 sort_keys=True，順序不影響去重／解析）。
LEDGER_FIELDS = (
    "schema_version",
    "event_id",
    "job_id",
    "run_id",
    "card_id",
    "skill_ref",
    "outcome",
    "workflow_id",
    "recorded_at",
)

VALID_OUTCOMES = frozenset({"completed", "failed", "cancelled"})


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def derive_outcome(job: Mapping[str, object]) -> str | None:
    """依 job 的 terminal 狀態推導 usage outcome。

    非 terminal（或狀態未知）回 None——呼叫端必須把 None 當「不記錄」處理，
    不得臆測。目前 coordinator registry 的 job status 只有兩種 terminal 值
    （`exited`／`failed`，見 `registry.TERMINAL_JOB_STATUSES`），完全沒有
    独立的「取消」狀態；`cancelled` 分支預先支援未來若補上該狀態時零改動即可
    生效，同時也讓呼叫端能以 `job = {**job, "status": "cancelled"}` 之類的
    顯式覆寫方式標記使用者手動取消的執行。
    """
    status = job.get("status")
    if status == "cancelled":
        return "cancelled"
    if status == "failed":
        return "failed"
    if status == "exited":
        return "completed" if job.get("exit_code") == 0 else "failed"
    return None


def build_event(
    job: Mapping[str, object],
    *,
    card_id: str,
    skill_ref: str,
    outcome: str,
    clock: Callable[[], str] = _utcnow,
) -> dict:
    """組出單一 ledger 事件（純函式，不做 I/O）。欄位固定為 `LEDGER_FIELDS`。"""
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"非法 outcome: {outcome!r}")
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("event 需要合法 job_id")
    if not isinstance(card_id, str) or not card_id:
        raise ValueError("event 需要合法 card_id")
    if not isinstance(skill_ref, str) or not skill_ref:
        raise ValueError("event 需要合法 skill_ref")
    run_id = job.get("workflow_run_id")
    workflow_id = job.get("workflow_claim_key") or run_id
    event_id = f"{job_id}:{card_id}:{outcome}"
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "job_id": job_id,
        "run_id": run_id if isinstance(run_id, str) else None,
        "card_id": card_id,
        "skill_ref": skill_ref,
        "outcome": outcome,
        "workflow_id": workflow_id if isinstance(workflow_id, str) else None,
        "recorded_at": clock(),
    }


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def iter_events(path: Path) -> Iterator[dict]:
    """逐行讀 ledger，靜默跳過空行／壞行（append-only 檔案容錯讀取，非驗證器）。"""
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def _find_event(path: Path, event_id: str) -> dict | None:
    for event in iter_events(path):
        if event.get("event_id") == event_id:
            return event
    return None


def append_usage_event(
    job: Mapping[str, object],
    *,
    cards: Mapping[str, Card],
    path: Path | None = None,
    clock: Callable[[], str] = _utcnow,
) -> dict | None:
    """終態 job → append 一筆 usage event（去重、只收 terminal、白名單欄位）。

    回 None 的情況（皆為「不記錄」而非錯誤）：
    - `derive_outcome(job)` 判定非 terminal。
    - job 沒有合法的 `workflow_card`（不是走 workflow card 派工路徑的 job，
      例如舊式 slice-only lane，沒有可歸屬的 skill）。
    - `workflow_card` 指向的 card 不在 `cards`（deck 已下架該 card、或呼叫端
      傳了過期的 card 表）。

    去重：event_id 已存在於 ledger 時直接回傳**磁碟上那一筆既有事件**（讀出而
    非重寫、也不是重新組出的新物件）——同一 terminal job 重放本函式，ledger
    行數不變，回傳值也忠實反映實際落盤內容（而非帶著呼叫當下 `clock()` 的
    新 `recorded_at`）。
    """
    outcome = derive_outcome(job)
    if outcome is None:
        return None
    card_id = job.get("workflow_card")
    if not isinstance(card_id, str) or not card_id:
        return None
    card = cards.get(card_id)
    if card is None:
        return None
    ledger_path = path if path is not None else paths.skill_usage_ledger_path()
    event = build_event(job, card_id=card_id, skill_ref=card.skill_ref, outcome=outcome, clock=clock)
    existing = _find_event(ledger_path, event["event_id"])
    if existing is not None:
        return existing
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(ledger_path.parent)
    return event


def record_usage_events(
    jobs: Iterable[Mapping[str, object]],
    *,
    cards: Mapping[str, Card],
    path: Path | None = None,
    clock: Callable[[], str] = _utcnow,
) -> list[dict]:
    """批次版 `append_usage_event`——供 `manager.run_tick` 的 `ledger_recorder`
    注入點使用：一次吃 complete_tick 這輪產出的 terminal job 快照清單，回傳
    實際寫入（或已存在、冪等回讀）的事件清單，跳過 None（不可記錄）的 job。
    """
    events: list[dict] = []
    for job in jobs:
        event = append_usage_event(job, cards=cards, path=path, clock=clock)
        if event is not None:
            events.append(event)
    return events


@dataclass(frozen=True)
class SkillUsageStats:
    card_id: str
    sample_count: int
    last_used_at: str | None
    outcome_counts: Mapping[str, int]


def load_usage_summary(
    path: Path,
    *,
    since: str | None = None,
) -> dict[str, SkillUsageStats]:
    """聚合 ledger 為 `{card_id: SkillUsageStats}`。

    `since`（ISO8601 UTC `%Y-%m-%dT%H:%M:%SZ`）可選：只計入 `recorded_at >=
    since` 的事件——時間戳同格式下字典序比較即代表時序比較，不需另外 parse。
    """
    counts: dict[str, int] = defaultdict(int)
    last_used: dict[str, str] = {}
    outcomes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in iter_events(path):
        card_id = event.get("card_id")
        if not isinstance(card_id, str):
            continue
        recorded_at = event.get("recorded_at")
        if since is not None:
            if not isinstance(recorded_at, str) or recorded_at < since:
                continue
        counts[card_id] += 1
        outcome = event.get("outcome")
        if isinstance(outcome, str):
            outcomes[card_id][outcome] += 1
        if isinstance(recorded_at, str):
            if card_id not in last_used or recorded_at > last_used[card_id]:
                last_used[card_id] = recorded_at
    return {
        card_id: SkillUsageStats(
            card_id=card_id,
            sample_count=count,
            last_used_at=last_used.get(card_id),
            outcome_counts=dict(outcomes[card_id]),
        )
        for card_id, count in counts.items()
    }
