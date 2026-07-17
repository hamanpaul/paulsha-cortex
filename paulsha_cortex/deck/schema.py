from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import yaml

SCHEMA_VERSION = 0
# runtime 契約真相源：coordinator/autonomy.py::parse_spec_frontmatter（勿發明多餘欄位）
EMITTED_FRONTMATTER_FIELDS = (
    "dispatch",
    "slice_id",
    "plan",
    "depends_on",
    "target_branch",
    "verification",
    "parse_error",
)
CARD_KINDS = ("skill",)
CARD_TYPES = ("interactive", "headless")
CARD_CLASSES = ("core", "niche", "emergency")
ALLOWED_PLACEHOLDERS = frozenset({"task-slug", "change"})
_CARDS_FILE_KEYS = frozenset({"version", "cards"})
_CARD_KEYS = frozenset(
    {
        "id",
        "kind",
        "type",
        "class",
        "skill_ref",
        "phase",
        "requires",
        "produces",
        "persona_binding",
        "provider_binding",
        "slice_group",
    }
)
_COMBO_FILE_KEYS = frozenset({"combo"})
_COMBO_KEYS = frozenset({"id", "task_type", "cards", "gate_spine"})
_COMBO_ENTRY_KEYS = frozenset({"ref", "depends_on"})
_GATE_CHECK_KEYS = frozenset({"after", "exists"})
_PLACEHOLDER_RE = re.compile(r"<([^<>]+)>")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

DEFAULT_CARDS_PATH = Path(__file__).with_name("data") / "cards.yaml"
DEFAULT_COMBOS_DIR = Path(__file__).with_name("data") / "combos"


class DeckSchemaError(ValueError):
    """deck 資料載入／驗證錯誤（fail-closed：任一錯即整批拒載）。"""


@dataclass(frozen=True)
class Card:
    id: str
    kind: str
    type: str
    card_class: str  # YAML key: class
    skill_ref: str
    phase: str | None = None
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    persona_binding: str | None = None
    provider_binding: str | None = None
    slice_group: str | None = None


@dataclass(frozen=True)
class ComboEntry:
    ref: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateCheck:
    after: str
    exists: tuple[str, ...]


@dataclass(frozen=True)
class Combo:
    id: str
    task_type: str
    cards: tuple[ComboEntry, ...]
    gate_spine: tuple[GateCheck, ...] = ()


def _check_placeholders(card_id: str, globs: tuple[str, ...], errors: list[str]) -> None:
    for g in globs:
        for name in _PLACEHOLDER_RE.findall(g):
            if name not in ALLOWED_PLACEHOLDERS:
                errors.append(f"{card_id}: 非法佔位符 <{name}>（白名單: {sorted(ALLOWED_PLACEHOLDERS)}）")
        # fail-closed：移除可解析的 token 後，任何殘餘角括號（<>、<<x>>、未閉合 <）一律拒絕
        residue = _PLACEHOLDER_RE.sub("", g)
        if "<" in residue or ">" in residue:
            errors.append(f"{card_id}: glob 含空白/巢狀/未閉合角括號: {g!r}")
        # fail-closed：glob 一律相對於 verify --root，拒絕絕對/rooted 路徑與 .. 逃逸
        if g.startswith(("/", "~")) or _DRIVE_RE.match(g):
            errors.append(f"{card_id}: glob 不得為絕對/rooted 路徑: {g!r}")
        elif ".." in g.split("/"):
            errors.append(f"{card_id}: glob 不得含 .. 路徑段: {g!r}")


def _str_tuple(value, card_id: str, field_name: str, errors: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        errors.append(f"{card_id}: {field_name} 必須是字串清單")
        return ()
    return tuple(value)


def _check_unknown_keys(label: str, record: Mapping, allowed: frozenset[str], errors: list[str]) -> None:
    unknown = sorted(key for key in record if key not in allowed)
    if unknown:
        errors.append(f"{label}: 未知欄位 {unknown}")


def _optional_str(value, card_id: str, field_name: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        errors.append(f"{card_id}: {field_name} 必須為非空字串")
        return None
    return value


def load_cards(path: str | Path) -> dict[str, Card]:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DeckSchemaError(f"cards 載入失敗: {source}: {exc}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("cards"), list):
        raise DeckSchemaError(f"cards 格式錯誤（缺 cards 清單）: {source}")

    errors: list[str] = []
    _check_unknown_keys("cards", raw, _CARDS_FILE_KEYS, errors)
    if raw.get("version") != SCHEMA_VERSION:
        errors.append(f"cards: version 不符，預期 {SCHEMA_VERSION}，實際 {raw.get('version')!r}")
    cards: dict[str, Card] = {}
    seen_ids: set[str] = set()
    for rec in raw["cards"]:
        if not isinstance(rec, Mapping) or not isinstance(rec.get("id"), str) or not rec["id"]:
            errors.append("卡片缺 id 或格式錯誤")
            continue
        cid = rec["id"]
        # 重複偵測獨立於卡片建構成敗（用 seen_ids 而非 cards），避免前一張壞卡遮蔽後續 duplicate
        if cid in seen_ids:
            errors.append(f"{cid}: 重複的 card id")
            continue
        seen_ids.add(cid)
        # 逐卡獨立錯誤域：前一張壞卡不得污染本卡的建構判斷
        rec_errors: list[str] = []
        _check_unknown_keys(cid, rec, _CARD_KEYS, rec_errors)
        kind = rec.get("kind")
        ctype = rec.get("type")
        cclass = rec.get("class")
        skill_ref = rec.get("skill_ref")
        phase = _optional_str(rec.get("phase"), cid, "phase", rec_errors)
        if kind not in CARD_KINDS:
            rec_errors.append(f"{cid}: kind 非法值 {kind!r}")
        if ctype not in CARD_TYPES:
            rec_errors.append(f"{cid}: type 非法值 {ctype!r}")
        if cclass not in CARD_CLASSES:
            rec_errors.append(f"{cid}: class 非法值 {cclass!r}")
        if not isinstance(skill_ref, str) or not skill_ref:
            rec_errors.append(f"{cid}: skill_ref 必須為非空字串")
        if phase is not None and phase not in (
            "research",
            "define",
            "plan",
            "build",
            "verify",
            "review",
            "ship",
        ):
            rec_errors.append(f"{cid}: phase 非法值 {phase!r}")
        requires = _str_tuple(rec.get("requires"), cid, "requires", rec_errors)
        produces = _str_tuple(rec.get("produces"), cid, "produces", rec_errors)
        _check_placeholders(cid, requires + produces, rec_errors)
        persona_binding = _optional_str(rec.get("persona_binding"), cid, "persona_binding", rec_errors)
        provider_binding = _optional_str(rec.get("provider_binding"), cid, "provider_binding", rec_errors)
        slice_group = rec.get("slice_group")
        if slice_group is not None and (not isinstance(slice_group, str) or not slice_group):
            rec_errors.append(f"{cid}: slice_group 必須為非空字串")
        if rec_errors:
            errors.extend(rec_errors)
            continue
        cards[cid] = Card(
            id=cid,
            kind=kind,
            type=ctype,
            card_class=cclass,
            skill_ref=skill_ref,
            phase=phase,
            requires=requires,
            produces=produces,
            persona_binding=persona_binding,
            provider_binding=provider_binding,
            slice_group=slice_group,
        )
    if errors:
        raise DeckSchemaError(f"cards 驗證失敗: {source}: " + "; ".join(errors))
    return cards


def _detect_combo_cycles(entries: tuple[ComboEntry, ...]) -> None:
    graph = {e.ref: list(e.depends_on) for e in entries}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {ref: WHITE for ref in graph}
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if color[dep] == GRAY:
                cycle = stack[stack.index(dep):] + [dep]
                raise DeckSchemaError(f"combo depends_on 循環相依: {' -> '.join(cycle)}")
            if color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[node] = BLACK

    for ref in graph:
        if color[ref] == WHITE:
            visit(ref)


def load_combo(path: str | Path, cards: Mapping[str, Card]) -> Combo:
    source = Path(path)
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DeckSchemaError(f"combo 載入失敗: {source}: {exc}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("combo"), Mapping):
        raise DeckSchemaError(f"combo 格式錯誤（缺 combo 區塊）: {source}")
    errors: list[str] = []
    _check_unknown_keys("combo", raw, _COMBO_FILE_KEYS, errors)
    rec = raw["combo"]
    _check_unknown_keys("combo", rec, _COMBO_KEYS, errors)

    combo_id = rec.get("id")
    task_type = rec.get("task_type")
    if not isinstance(combo_id, str) or not combo_id:
        errors.append("combo 缺 id")
    if not isinstance(task_type, str) or not task_type:
        errors.append("combo 缺 task_type")

    entries: list[ComboEntry] = []
    raw_cards = rec.get("cards")
    if not isinstance(raw_cards, list) or not raw_cards:
        errors.append("combo.cards 必須為非空清單")
        raw_cards = []
    seen: set[str] = set()
    for item in raw_cards:
        if not isinstance(item, Mapping) or not isinstance(item.get("ref"), str):
            errors.append("combo.cards 項目缺 ref")
            continue
        ref = item["ref"]
        _check_unknown_keys(ref, item, _COMBO_ENTRY_KEYS, errors)
        if ref not in cards:
            errors.append(f"未知卡片引用: {ref}")
        if ref in seen:
            errors.append(f"combo 內重複卡片: {ref}")
        seen.add(ref)
        deps = _str_tuple(item.get("depends_on"), ref, "depends_on", errors)
        for d in deps:
            if d not in {c.get("ref") for c in raw_cards if isinstance(c, Mapping)}:
                errors.append(f"{ref}: depends_on 指向 combo 外卡片 {d}")
        entries.append(ComboEntry(ref=ref, depends_on=deps))

    spine: list[GateCheck] = []
    raw_spine = rec.get("gate_spine")
    if raw_spine is not None and not isinstance(raw_spine, list):
        errors.append(f"combo: gate_spine 必須是清單，實際 {type(raw_spine).__name__}")
        raw_spine = []
    for g in raw_spine or []:
        if not isinstance(g, Mapping) or not isinstance(g.get("after"), str):
            errors.append("gate_spine 項目缺 after")
            continue
        _check_unknown_keys(g["after"], g, _GATE_CHECK_KEYS, errors)
        if g["after"] not in seen:
            errors.append(f"gate_spine.after 指向不存在卡片: {g['after']}")
        exists = _str_tuple(g.get("exists"), g["after"], "gate_spine.exists", errors)
        _check_placeholders(g["after"], exists, errors)
        if not exists:
            errors.append(f"{g['after']}: gate_spine.exists 不得為空")
        spine.append(GateCheck(after=g["after"], exists=exists))

    if errors:
        raise DeckSchemaError(f"combo 驗證失敗: {source}: " + "; ".join(errors))

    combo = Combo(id=combo_id, task_type=task_type, cards=tuple(entries), gate_spine=tuple(spine))
    _detect_combo_cycles(combo.cards)
    return combo
