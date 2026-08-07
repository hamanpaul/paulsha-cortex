from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from .schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    DeckSchemaError,
    load_cards,
    load_combo,
)
from .task_types import TaskTypeTaxonomy, TitleClassification, classify_title

_COMBO_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_SELECTION_SOURCES = frozenset({"task-type-auto", "explicit-override", "bypass-default"})


@dataclass(frozen=True)
class ComboSelection:
    combo_id: str
    source: str
    task_type: str | None
    reason: str

    def __post_init__(self) -> None:
        if _COMBO_ID_RE.fullmatch(self.combo_id) is None:
            raise ValueError(f"combo selection combo_id 非法: {self.combo_id!r}")
        if self.source not in _SELECTION_SOURCES:
            raise ValueError(f"combo selection source 非法: {self.source!r}")
        if self.task_type is not None and (
            not isinstance(self.task_type, str) or not self.task_type
        ):
            raise ValueError("combo selection task_type 必須為 null 或非空字串")
        if (
            not isinstance(self.reason, str)
            or not self.reason
            or len(self.reason) > 500
        ):
            raise ValueError("combo selection reason 必須為 1–500 字字串")


class ComboSelectionError(DeckSchemaError):
    """Fail-closed combo selection error with actionable per-issue diagnostics."""


def _reverse_combo_task_type(
    combo_id: str,
    taxonomy: TaskTypeTaxonomy,
) -> str | None:
    for task_type, spec in taxonomy.task_types.items():
        if spec.combo == combo_id:
            return task_type
    return None


def _bounded_reason(summary: str, *, details: list[str] | None = None) -> str:
    reason = summary if not details else f"{summary}; " + "; ".join(details)
    return reason[:500]


def _format_issue_line(issue_number: int, title: str | None, classification: TitleClassification) -> str:
    display_title = title if title is not None else "<missing-title>"
    return f"#{issue_number}: {display_title} → {classification.kind}/{classification.reason}"


def _raise_selection_error(summary: str, diagnostics: list[str]) -> None:
    if diagnostics:
        summary = f"{summary}: " + "; ".join(diagnostics)
    raise ComboSelectionError(summary)


def select_combo(
    titles: Mapping[int, str | None] | None,
    *,
    taxonomy: TaskTypeTaxonomy,
    override: str | None = None,
    default_combo: str = "feature-oneshot",
) -> ComboSelection:
    if _COMBO_ID_RE.fullmatch(default_combo) is None:
        raise ComboSelectionError(f"default combo id invalid: {default_combo!r}")
    if override is not None:
        if _COMBO_ID_RE.fullmatch(override) is None:
            raise ComboSelectionError(f"combo override invalid: {override!r}")
        # R3：override 只要 combo 檔存在且可經 load_combo 驗證即可用——taxonomy
        # 反查（下方 _reverse_combo_task_type）只用來標記 provenance 的
        # task_type，找不到對應 type（例如 legacy combo mcu-feature 未列在
        # task-types.yaml 映射）時保留 None，不得因此判定 override 未知。
        try:
            load_combo(DEFAULT_COMBOS_DIR / f"{override}.yaml", load_cards(DEFAULT_CARDS_PATH))
        except DeckSchemaError as exc:
            raise ComboSelectionError(f"combo override unknown: {override}: {exc}") from exc
        task_type = _reverse_combo_task_type(override, taxonomy)
        return ComboSelection(
            combo_id=override,
            source="explicit-override",
            task_type=task_type,
            reason=_bounded_reason(f"explicit override selected {override}"),
        )

    if titles is None:
        return ComboSelection(
            combo_id=default_combo,
            source="bypass-default",
            task_type=None,
            reason="snapshot-drift: authoritative issue titles unavailable",
        )
    if not titles:
        return ComboSelection(
            combo_id=default_combo,
            source="bypass-default",
            task_type=None,
            reason="absent: no mapped issue titles",
        )

    diagnostics: list[str] = []
    matched_types: set[str] = set()
    bypass_markers: list[str] = []
    for issue_number, title in sorted(titles.items()):
        classification = (
            classify_title(title, taxonomy)
            if title is not None
            else TitleClassification(
                kind="absent",
                task_type=None,
                scope=None,
                disposition="bypass",
                reason="absent: github_issue source has no title",
            )
        )
        diagnostics.append(_format_issue_line(issue_number, title, classification))
        if classification.kind in {"unknown_type", "ambiguous"}:
            _raise_selection_error("combo selection fail-closed", diagnostics)
        if classification.kind == "matched" and classification.task_type is not None:
            matched_types.add(classification.task_type)
        elif classification.kind in {"absent", "unparseable"}:
            bypass_markers.append(f"#{issue_number} {classification.kind}")

    if len(matched_types) >= 2:
        _raise_selection_error("combo selection matched multiple task types", diagnostics)
    if len(matched_types) == 1:
        task_type = next(iter(matched_types))
        combo_id = taxonomy.task_types[task_type].combo
        if combo_id is None:
            return ComboSelection(
                combo_id=default_combo,
                source="bypass-default",
                task_type=task_type,
                reason=_bounded_reason(
                    f"combo-gap: task_type {task_type} has no mapped combo",
                    details=bypass_markers[:3],
                ),
            )
        return ComboSelection(
            combo_id=combo_id,
            source="task-type-auto",
            task_type=task_type,
            reason=_bounded_reason(f"matched task_type {task_type} → {combo_id}"),
        )
    marker = "unparseable" if any("unparseable" in row for row in bypass_markers) else "absent"
    return ComboSelection(
        combo_id=default_combo,
        source="bypass-default",
        task_type=None,
        reason=_bounded_reason(
            f"{marker}: no mapped task_type signal",
            details=bypass_markers[:3],
        ),
    )
