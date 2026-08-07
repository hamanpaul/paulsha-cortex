"""Skill park janitor（issue #204）：cold-skill 偵測與 proposal-first park／restore。

治理模型比照 `paulsha_cortex.coordinator.gc`（worktree／branch 回收）：janitor
本身**只讀** ledger、**只寫** proposal 檔——不動 park state；`apply_park` /
`manual_park` / `restore` 才會改動 park state，且都要求呼叫端帶 `approved_by`
（operator 明確觸發，不是自動化路徑可以到達的函式）。

core/emergency 永久豁免：`EXEMPT_CARD_CLASSES` 在 `find_cold_skills`（第一
道防線：根本不會被判定 cold）與 `propose_park` / `apply_park` / `manual_park`
（第二道防線：即使呼叫端繞過 `find_cold_skills` 直接餵 skill id，這裡仍重新
核對 card_class）都各自強制檢查一次——不是「呼叫端負責排除就好」，每個會
改動治理狀態的入口都自己防呆。

cold-skill 判定所需的兩個閾值（`min_samples` / `observation_window_days`）目前
以模組常數給預設值，供 CLI／未來排程沿用；具體數字是設計決策而非技術決策，
決策記錄與可調整方式見
`docs/superpowers/plans/2026-08-07-skill-cold-threshold-defaults.md`。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from paulsha_cortex.deck.schema import Card

from .skill_ledger import SkillUsageStats, load_usage_summary

PARK_STATE_SCHEMA = "cortex-skill-park/v1"
PROPOSAL_SCHEMA = "cortex-skill-park-proposal/v1"

EXEMPT_CARD_CLASSES = frozenset({"core", "emergency"})

# 初始預設值（未經人類最終核可，見上方模組 docstring 指向的決策記錄）：
# - 5 次終態執行才算「有足夠樣本可信」——避免新卡片或剛上線就被單次失敗/罕用誤判。
# - 30 天觀測窗——與既有 `RECENT_DONE_WINDOW_SECONDS`（24 小時）刻意不同尺度：
#   那是「操作面板可見範圍」，這裡是「治理面判斷是否停用」，需要更長的窗才不會
#   把周期性但低頻使用的 skill 誤判為冷門。
DEFAULT_MIN_SAMPLES = 5
DEFAULT_OBSERVATION_WINDOW_DAYS = 30


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso8601(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(directory)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def find_cold_skills(
    usage: Mapping[str, SkillUsageStats],
    cards: Mapping[str, Card],
    *,
    min_samples: int,
    observation_window_days: int,
    now: Callable[[], str] = _utcnow,
) -> list[str]:
    """回傳判定為 cold 的 card id 清單（已排序，供比較穩定）。

    判定為 cold 需**同時**成立：
    1. `card_class` 不在 `EXEMPT_CARD_CLASSES`（core/emergency 永久豁免，
       連候選資格都沒有）。
    2. 有 ledger 紀錄且 `sample_count >= min_samples`——樣本不足時我們沒有
       足夠證據信任「這是冷門」而非「剛好還沒被觀測到」，一律不判定 cold
       （對應 issue 驗收「未達最低樣本…不得自動 park」，也呼應非目標「不把
       低使用率直接等同可刪除」）。
    3. `last_used_at` 早於 `now - observation_window_days`——即使樣本數
       足夠，只要觀測窗內仍有使用紀錄就不算冷；`last_used_at` 缺失（理論上
       不會發生在 sample_count>=1 的情況，但防禦式處理）視為不足證據。

    `min_samples` 與 `observation_window_days` 是純參數，不讀模組常數——
    是否套用 `DEFAULT_MIN_SAMPLES` / `DEFAULT_OBSERVATION_WINDOW_DAYS` 由
    呼叫端決定，讓測試與未來的門檻調整都不必碰這個函式本身。
    """
    cutoff = _parse_iso8601(now()) - timedelta(days=observation_window_days)
    cold: list[str] = []
    for card_id, card in cards.items():
        if card.card_class in EXEMPT_CARD_CLASSES:
            continue
        stats = usage.get(card_id)
        if stats is None or stats.sample_count < min_samples:
            continue
        if stats.last_used_at is None:
            continue
        if _parse_iso8601(stats.last_used_at) >= cutoff:
            continue
        cold.append(card_id)
    return sorted(cold)


def _read_proposal(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def list_proposals(proposals_dir: Path) -> list[dict]:
    """唯讀列出所有 proposal（含 pending 與 approved），依 `created_at` 排序。"""
    if not proposals_dir.is_dir():
        return []
    proposals = []
    for entry in sorted(proposals_dir.glob("*.json")):
        proposal = _read_proposal(entry)
        if proposal is not None:
            proposals.append(proposal)
    return sorted(proposals, key=lambda item: (item.get("created_at") or "", item.get("proposal_id") or ""))


def propose_park(
    cold_skill_ids: Sequence[str],
    *,
    reason: str,
    evidence_window_days: int,
    cards: Mapping[str, Card],
    out_dir: Path,
    clock: Callable[[], str] = _utcnow,
) -> dict:
    """只寫 proposal 檔（不動 park state；比照 `gc.py` dry-run-by-default 的
    proposal-first 精神——分類與執行分離）。

    一 skill 一個 proposal 檔（`<timestamp>-<skill_id>.json`），已存在 pending
    proposal 的 skill 會被跳過（不重複開票）——這讓 janitor 可以每個 tick 都
    呼叫而不會無限堆積重複 proposal（見 `run_janitor_tick`）。

    core/emergency 二次防呆：即使呼叫端已用 `find_cold_skills` 排除，這裡仍
    重新核對 `cards[skill_id].card_class`；任一命中直接 fail-closed（raise），
    不得靜默略過寫出殘缺 proposal 批次。
    """
    exempt_hits = sorted(
        skill_id
        for skill_id in cold_skill_ids
        if cards.get(skill_id) is not None and cards[skill_id].card_class in EXEMPT_CARD_CLASSES
    )
    if exempt_hits:
        raise ValueError(f"core/emergency skill 不得出現在 park proposal: {exempt_hits}")

    existing_pending = {
        proposal.get("skill_id")
        for proposal in list_proposals(out_dir)
        if proposal.get("status") == "pending"
    }
    timestamp = clock()
    ts_slug = timestamp.translate(str.maketrans("", "", ":-"))
    created: list[dict] = []
    skipped: list[str] = []
    for skill_id in sorted(set(cold_skill_ids)):
        if skill_id in existing_pending:
            skipped.append(skill_id)
            continue
        proposal_id = f"{ts_slug}-{skill_id}"
        proposal = {
            "schema": PROPOSAL_SCHEMA,
            "proposal_id": proposal_id,
            "skill_id": skill_id,
            "reason": reason,
            "evidence_window_days": evidence_window_days,
            "created_at": timestamp,
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
        }
        _atomic_write_json(out_dir / f"{proposal_id}.json", proposal)
        created.append(proposal)
    return {"created": created, "skipped_already_pending": skipped}


def run_janitor_tick(
    *,
    cards: Mapping[str, Card],
    ledger_path: Path,
    proposals_dir: Path,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    observation_window_days: int = DEFAULT_OBSERVATION_WINDOW_DAYS,
    reason: str = "cold-skill-auto-detected",
    now: Callable[[], str] = _utcnow,
) -> dict:
    """收尾 janitor 的單次 tick 入口（供 `manager.run_tick` 的 `skill_janitor`
    注入點使用）：讀 ledger → 判斷 cold → 開 proposal。全程零破壞性變更——
    未核准的 proposal 不會改動 park state，重跑不會重複開同一張 pending 票。
    """
    usage = load_usage_summary(ledger_path)
    cold = find_cold_skills(
        usage, cards, min_samples=min_samples, observation_window_days=observation_window_days, now=now
    )
    if not cold:
        return {"cold_skills": [], "proposals_created": [], "proposals_skipped": []}
    result = propose_park(
        cold,
        reason=reason,
        evidence_window_days=observation_window_days,
        cards=cards,
        out_dir=proposals_dir,
        clock=now,
    )
    return {
        "cold_skills": cold,
        "proposals_created": [proposal["proposal_id"] for proposal in result["created"]],
        "proposals_skipped": result["skipped_already_pending"],
    }


def load_park_state(path: Path) -> dict:
    """讀目前 park 狀態；檔案不存在或壞損時回傳空狀態（不 raise——讀取路徑
    對「尚未有任何 skill 被 park 過」是常態，不是錯誤）。"""
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict) and isinstance(raw.get("parked"), dict):
            raw.setdefault("history", [])
            return raw
    return {"schema": PARK_STATE_SCHEMA, "parked": {}, "history": []}


def _append_history(state: dict, entry: dict) -> None:
    """所有決策留 audit trail（issue #204 驗收「所有決策保留 reason、evidence
    window 與 audit trail」）：append-only 於 park state 本身，因為 park state
    本來就是一個小檔案，不值得為此另開一個 ledger。"""
    state.setdefault("history", []).append(entry)


def _require_approved_by(approved_by: str | None) -> str:
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValueError("park／restore 操作需要非空 approved_by（operator 明確觸發）")
    return approved_by.strip()


def _guard_not_exempt(skill_id: str, cards: Mapping[str, Card]) -> None:
    card = cards.get(skill_id)
    if card is not None and card.card_class in EXEMPT_CARD_CLASSES:
        raise ValueError(f"{skill_id} 屬 class={card.card_class}，永久豁免自動／人工 park")


def apply_park(
    proposal_id: str,
    *,
    approved_by: str,
    cards: Mapping[str, Card],
    proposals_dir: Path,
    state_path: Path,
    clock: Callable[[], str] = _utcnow,
) -> dict:
    """核准既有 pending proposal 並套用 park（唯一由 proposal 通往 park state
    改動的路徑；janitor 本身絕對不會呼叫這個函式）。回傳更新後的 park state。
    """
    approved_by = _require_approved_by(approved_by)
    proposal_path = proposals_dir / f"{proposal_id}.json"
    proposal = _read_proposal(proposal_path)
    if proposal is None:
        raise ValueError(f"proposal 不存在或不可讀: {proposal_id}")
    if proposal.get("status") != "pending":
        raise ValueError(f"proposal 已非 pending 狀態: {proposal_id} ({proposal.get('status')!r})")
    skill_id = proposal.get("skill_id")
    if not isinstance(skill_id, str) or not skill_id:
        raise ValueError(f"proposal 缺合法 skill_id: {proposal_id}")
    _guard_not_exempt(skill_id, cards)

    state = load_park_state(state_path)
    now = clock()
    state["parked"][skill_id] = {
        "parked_at": now,
        "reason": proposal.get("reason"),
        "approved_by": approved_by,
        "proposal_id": proposal_id,
    }
    _append_history(
        state,
        {
            "action": "park",
            "skill_id": skill_id,
            "at": now,
            "approved_by": approved_by,
            "reason": proposal.get("reason"),
            "evidence_window_days": proposal.get("evidence_window_days"),
            "proposal_id": proposal_id,
        },
    )
    _atomic_write_json(state_path, state)

    proposal["status"] = "approved"
    proposal["approved_by"] = approved_by
    proposal["approved_at"] = now
    _atomic_write_json(proposal_path, proposal)
    return state


def manual_park(
    skill_id: str,
    *,
    reason: str,
    approved_by: str,
    cards: Mapping[str, Card],
    state_path: Path,
    clock: Callable[[], str] = _utcnow,
) -> dict:
    """operator 手動 park（不經 proposal 檔，直接的明確觸發）——供
    `cortex skill park` 這種「人已經在看著、要立即處理」的路徑使用；仍強制
    core/emergency 豁免與 `approved_by`。"""
    approved_by = _require_approved_by(approved_by)
    if skill_id not in cards:
        raise ValueError(f"未知 skill id（不在目前 deck cards 內）: {skill_id}")
    _guard_not_exempt(skill_id, cards)

    state = load_park_state(state_path)
    now = clock()
    state["parked"][skill_id] = {
        "parked_at": now,
        "reason": reason,
        "approved_by": approved_by,
        "proposal_id": None,
    }
    _append_history(
        state,
        {
            "action": "park",
            "skill_id": skill_id,
            "at": now,
            "approved_by": approved_by,
            "reason": reason,
            "evidence_window_days": None,
            "proposal_id": None,
        },
    )
    _atomic_write_json(state_path, state)
    return state


def restore(
    skill_id: str,
    *,
    approved_by: str,
    state_path: Path,
    reason: str | None = None,
    clock: Callable[[], str] = _utcnow,
) -> dict:
    """把 skill 移出 park 清單（可逆性核心：只改 park state，不動來源 skill
    檔案或 usage ledger 任何一位元）。skill 本來就沒被 park 時為 no-op（不寫
    history、不改檔案內容——冪等）。"""
    approved_by = _require_approved_by(approved_by)
    state = load_park_state(state_path)
    if skill_id in state["parked"]:
        del state["parked"][skill_id]
        _append_history(
            state,
            {
                "action": "restore",
                "skill_id": skill_id,
                "at": clock(),
                "approved_by": approved_by,
                "reason": reason,
            },
        )
        _atomic_write_json(state_path, state)
    return state
