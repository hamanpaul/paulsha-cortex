from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .schema import Card


class DeckVerifyError(ValueError):
    """verify 參數/樣式錯誤（非 artifact 缺失）：佔位符未解析、絕對路徑、路徑逃逸。"""


_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _validate_name(label: str, value: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise DeckVerifyError(
            f"{label} 名稱不合法（僅允許 [a-z0-9-]，不可含 glob/path metacharacters）: {value!r}"
        )
    return value


@dataclass(frozen=True)
class VerifyResult:
    card_id: str
    ok: bool
    missing: tuple[str, ...]
    matched: tuple[str, ...]


def _subst(glob: str, task_slug: str, change: str | None) -> str:
    out = glob.replace("<task-slug>", task_slug)
    if change:
        out = out.replace("<change>", change)
    return out


def _guard_pattern(card_id: str, pattern: str) -> None:
    """fail-closed 樣式守門（schema 載入已擋一輪，此處為縱深防禦——
    Card 可被直接建構，不必然經過 load_cards）。"""
    if "<" in pattern or ">" in pattern:
        raise DeckVerifyError(
            f"{card_id}: 佔位符未解析（卡片用到 <change> 需提供 change 參數）: {pattern!r}")
    if pattern.startswith(("/", "~")) or Path(pattern).is_absolute():
        raise DeckVerifyError(f"{card_id}: produces glob 不得為絕對路徑: {pattern!r}")
    if ".." in pattern.split("/"):
        raise DeckVerifyError(f"{card_id}: produces glob 不得含 .. 路徑段（root 逃逸）: {pattern!r}")


def verify_card(
    card: Card,
    task_slug: str,
    *,
    root: str | Path = ".",
    change: str | None = None,
) -> VerifyResult:
    """produces glob 存在性驗收（Phase A：只驗存在，不驗內容）。"""
    task_slug = _validate_name("task_slug", task_slug)
    if change is not None:
        change = _validate_name("change", change)
    base = Path(root)
    missing: list[str] = []
    matched: list[str] = []
    for raw in card.produces:
        pattern = _subst(raw, task_slug, change)
        _guard_pattern(card.id, pattern)
        try:
            hit = next(iter(base.glob(pattern)), None)  # 只需「至少一個命中」，短路避免寬 pattern 全列舉
        except (NotImplementedError, ValueError) as exc:
            raise DeckVerifyError(f"{card.id}: 非法 glob 樣式 {pattern!r}: {exc}") from exc
        if hit is not None:
            matched.append(pattern)
        else:
            missing.append(raw if pattern == raw else pattern)
    return VerifyResult(
        card_id=card.id,
        ok=not missing,
        missing=tuple(missing),
        matched=tuple(matched),
    )
