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
    "executor",
    "model_id",
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
        "execution",
        "runtime_capabilities",
    }
)
# #262 R1：card 以資料宣告執行所需 runtime capability，形式為 `<kind>:<name>`
# （例：`module:pytest`、`executable:socat`）。新增 card 只需寫這份宣告，
# 不必修改 preflight 實作。kind 白名單與
# coordinator.runtime_preflight.CAPABILITY_KINDS 為同一組值。
RUNTIME_CAPABILITY_KINDS = ("module", "executable", "bridge", "provider")
_EXECUTION_KEYS = frozenset({"action", "commit_policy", "test_policy"})
_COMBO_FILE_KEYS = frozenset({"combo"})
_COMBO_KEYS = frozenset({"id", "task_type", "cards", "gate_spine", "band_triggered"})
_COMBO_ENTRY_KEYS = frozenset({"ref", "depends_on"})
_GATE_CHECK_KEYS = frozenset({"after", "exists"})
_BAND_TRIGGERED_KEYS = frozenset({"trigger", "cards", "gate_spine"})
# gate_spine 兩層制（#208 H.1 / #221）：band 依嚴重度單調遞增，band_triggered.trigger 必須是其中之一。
BAND_LEVELS = ("green", "yellow", "red")
_PLACEHOLDER_RE = re.compile(r"<([^<>]+)>")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

DEFAULT_CARDS_PATH = Path(__file__).with_name("data") / "cards.yaml"
DEFAULT_COMBOS_DIR = Path(__file__).with_name("data") / "combos"


class DeckSchemaError(ValueError):
    """deck 資料載入／驗證錯誤（fail-closed：任一錯即整批拒載）。"""


def instance_combos_dir() -> Path:
    """`$PSC_AGENTS_ROOT/config/combos/`——instance-local combo override 目錄。

    函式內 import 避免頂層循環依賴風險（比照 `porcelain/init_sample.py` 既有慣例）。
    """
    from paulsha_cortex.config import paths as _paths

    return _paths.agents_root() / "config" / "combos"


def combo_search_dirs(*, package_dir: Path = DEFAULT_COMBOS_DIR) -> tuple[Path, ...]:
    """依優先序回傳實際存在的 combo 搜尋目錄：instance-local 先於套件內建。

    `package_dir` 可由呼叫端注入，讓 `deck/cli.py` 這類模組保留自己的
    `DEFAULT_COMBOS_DIR` module-level 繫結供既有測試 monkeypatch。
    """
    candidates = (instance_combos_dir(), package_dir)
    return tuple(d for d in candidates if d.is_dir())


def resolve_combo_path(combo_id: str, *, package_dir: Path = DEFAULT_COMBOS_DIR) -> Path:
    """依 `combo_search_dirs()` 順序尋找 `<combo_id>.yaml`；找不到則 fail-closed。"""
    dirs = combo_search_dirs(package_dir=package_dir)
    for directory in dirs:
        candidate = directory / f"{combo_id}.yaml"
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(d) for d in dirs) if dirs else "(無搜尋目錄存在)"
    raise DeckSchemaError(f"combo 未找到: {combo_id}（搜尋過: {searched}）")


def iter_combo_files(*, package_dir: Path = DEFAULT_COMBOS_DIR) -> list[tuple[str, Path]]:
    """列舉可用 combo（依 id 排序、去重）：instance-local 同 id 覆蓋套件內建。"""
    found: dict[str, Path] = {}
    for directory in combo_search_dirs(package_dir=package_dir):
        for path in sorted(directory.glob("*.yaml")):
            if path.stem not in found:
                found[path.stem] = path
    return sorted(found.items())


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
    action: str | None = None
    commit_policy: str | None = None
    test_policy: str | None = None
    # #262 R1：dispatch 前 preflight 依這份宣告逐項檢查。
    runtime_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComboEntry:
    ref: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateCheck:
    after: str
    exists: tuple[str, ...]


@dataclass(frozen=True)
class BandTriggeredSpine:
    """gate_spine 加掛層（band 觸發後才併入的 cards/gate_spine）。

    只有 Combo.gate_spine（必要核心）計入 acceptance_surfaces；本層資料
    刻意放在獨立欄位，讓下游（H.1 sizing）不會誤把加掛層算進去。
    """

    trigger: str
    cards: tuple[ComboEntry, ...] = ()
    gate_spine: tuple[GateCheck, ...] = ()


@dataclass(frozen=True)
class Combo:
    id: str
    task_type: str
    cards: tuple[ComboEntry, ...]
    gate_spine: tuple[GateCheck, ...] = ()
    band_triggered: BandTriggeredSpine | None = None


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
            "claim",
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
        execution = rec.get("execution")
        action = commit_policy = test_policy = None
        if execution is not None:
            if not isinstance(execution, Mapping):
                rec_errors.append(f"{cid}: execution 必須為mapping")
            else:
                _check_unknown_keys(f"{cid}.execution", execution, _EXECUTION_KEYS, rec_errors)
                action = _optional_str(execution.get("action"), cid, "execution.action", rec_errors)
                commit_policy = _optional_str(
                    execution.get("commit_policy"), cid, "execution.commit_policy", rec_errors
                )
                test_policy = _optional_str(
                    execution.get("test_policy"), cid, "execution.test_policy", rec_errors
                )
                if commit_policy not in {None, "forbidden", "optional", "required"}:
                    rec_errors.append(f"{cid}: execution.commit_policy 非法值 {commit_policy!r}")
                if test_policy not in {None, "none", "red-required", "focused", "full"}:
                    rec_errors.append(f"{cid}: execution.test_policy 非法值 {test_policy!r}")
        slice_group = rec.get("slice_group")
        if slice_group is not None and (not isinstance(slice_group, str) or not slice_group):
            rec_errors.append(f"{cid}: slice_group 必須為非空字串")
        runtime_capabilities = _str_tuple(
            rec.get("runtime_capabilities"), cid, "runtime_capabilities", rec_errors
        )
        # fail-closed：宣告格式錯誤（未知 kind／空 name）在載入時就擋下，
        # 避免無聲漏檢——漏掉一項宣告等於退回 #262 的現狀。
        for token in runtime_capabilities:
            kind_part, sep, name_part = token.partition(":")
            if not sep or kind_part not in RUNTIME_CAPABILITY_KINDS or not name_part.strip():
                rec_errors.append(
                    f"{cid}: runtime_capabilities 非法值 {token!r}"
                    f"（須為 <kind>:<name>，kind ∈ {'/'.join(RUNTIME_CAPABILITY_KINDS)}）"
                )
        if len(set(runtime_capabilities)) != len(runtime_capabilities):
            rec_errors.append(f"{cid}: runtime_capabilities 有重複宣告")
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
            action=action,
            commit_policy=commit_policy,
            test_policy=test_policy,
            runtime_capabilities=runtime_capabilities,
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

    # band_triggered（gate_spine 兩層制加掛層，#221）：與上面 gate_spine 對稱解析，
    # 但 ref／after 允許指向核心層（seen）或加掛層自身（band_seen），且加掛層卡片
    # 不得與核心層重複（維持「必要核心 vs 加掛層」互斥，避免下游 acceptance_surfaces
    # 誤重複計算）。
    band_triggered: BandTriggeredSpine | None = None
    raw_band = rec.get("band_triggered")
    if raw_band is not None:
        if not isinstance(raw_band, Mapping):
            errors.append(f"combo: band_triggered 必須是 mapping，實際 {type(raw_band).__name__}")
            raw_band = {}
        _check_unknown_keys("combo.band_triggered", raw_band, _BAND_TRIGGERED_KEYS, errors)

        trigger = raw_band.get("trigger")
        if trigger not in BAND_LEVELS:
            errors.append(f"combo.band_triggered: trigger 非法值 {trigger!r}（允許 {list(BAND_LEVELS)}）")
            trigger = None

        band_entries: list[ComboEntry] = []
        raw_band_cards = raw_band.get("cards")
        if raw_band_cards is not None and not isinstance(raw_band_cards, list):
            errors.append(f"combo.band_triggered: cards 必須是清單，實際 {type(raw_band_cards).__name__}")
            raw_band_cards = []
        band_seen: set[str] = set()
        for item in raw_band_cards or []:
            if not isinstance(item, Mapping) or not isinstance(item.get("ref"), str):
                errors.append("combo.band_triggered.cards 項目缺 ref")
                continue
            ref = item["ref"]
            _check_unknown_keys(f"band_triggered.{ref}", item, _COMBO_ENTRY_KEYS, errors)
            if ref not in cards:
                errors.append(f"未知卡片引用: {ref}")
            if ref in seen or ref in band_seen:
                errors.append(f"band_triggered 卡片與骨幹重複: {ref}")
            band_seen.add(ref)
            band_deps = _str_tuple(item.get("depends_on"), ref, "depends_on", errors)
            for d in band_deps:
                if d not in seen and d not in band_seen:
                    errors.append(f"{ref}: depends_on 指向 combo 外卡片 {d}")
            band_entries.append(ComboEntry(ref=ref, depends_on=band_deps))

        band_spine: list[GateCheck] = []
        raw_band_spine = raw_band.get("gate_spine")
        if raw_band_spine is not None and not isinstance(raw_band_spine, list):
            errors.append(f"combo.band_triggered: gate_spine 必須是清單，實際 {type(raw_band_spine).__name__}")
            raw_band_spine = []
        for g in raw_band_spine or []:
            if not isinstance(g, Mapping) or not isinstance(g.get("after"), str):
                errors.append("band_triggered.gate_spine 項目缺 after")
                continue
            _check_unknown_keys(g["after"], g, _GATE_CHECK_KEYS, errors)
            if g["after"] not in seen and g["after"] not in band_seen:
                errors.append(f"band_triggered.gate_spine.after 指向不存在卡片: {g['after']}")
            exists = _str_tuple(g.get("exists"), g["after"], "gate_spine.exists", errors)
            _check_placeholders(g["after"], exists, errors)
            if not exists:
                errors.append(f"{g['after']}: gate_spine.exists 不得為空")
            band_spine.append(GateCheck(after=g["after"], exists=exists))

        if trigger is not None:
            band_triggered = BandTriggeredSpine(
                trigger=trigger, cards=tuple(band_entries), gate_spine=tuple(band_spine)
            )

    if errors:
        raise DeckSchemaError(f"combo 驗證失敗: {source}: " + "; ".join(errors))

    combo = Combo(
        id=combo_id,
        task_type=task_type,
        cards=tuple(entries),
        gate_spine=tuple(spine),
        band_triggered=band_triggered,
    )
    _detect_combo_cycles(combo.cards)
    return combo
