from __future__ import annotations

from pathlib import Path
from typing import Mapping
import warnings

import yaml

from .contract import PersonaContract, validate_persona_schema

DEFAULT_PERSONAS_PATH = Path(__file__).with_name("personas.yaml")

_VALID_ENFORCEMENT = ("shadow", "enforce")
DEFAULT_ENFORCEMENT = "shadow"


def load_enforcement(path: str | Path | None = None) -> str:
    """讀 personas.yaml 頂層 `enforcement`（全域護欄模式）。

    fail-safe：缺檔／壞 YAML／讀檔 I/O 錯（權限/IO）／缺 key／非法值一律退
    'shadow'（最保守，永不誤翻 enforce）。僅認字面 'shadow' / 'enforce'。
    """
    source = Path(path) if path is not None else DEFAULT_PERSONAS_PATH
    if not source.is_file():
        return DEFAULT_ENFORCEMENT
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        # OSError：source.read_text 可能因權限/IO 失敗（is_file 與 read 間亦有 TOCTOU）；
        # 護欄旗標讀取絕不可崩潰，一律 fail-safe 退 shadow。
        return DEFAULT_ENFORCEMENT
    if not isinstance(raw, Mapping):
        return DEFAULT_ENFORCEMENT
    value = raw.get("enforcement")
    return value if value in _VALID_ENFORCEMENT else DEFAULT_ENFORCEMENT


def load_catalog(path: str | Path | None = None) -> dict[str, PersonaContract]:
    source = Path(path) if path is not None else DEFAULT_PERSONAS_PATH
    if not source.is_file():
        raise FileNotFoundError(f"persona catalog 不存在: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"persona catalog 解析失敗: {source}: {exc}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("roles"), Mapping):
        raise ValueError(f"persona catalog 格式錯誤（缺 roles）: {source}")

    records = raw["roles"]
    result = validate_persona_schema(records)
    if not result.ok:
        raise ValueError(f"persona catalog schema 不合法: {result.errors}")

    catalog: dict[str, PersonaContract] = {}
    for role, rec in records.items():
        raw_skills = rec.get("skills", [])
        if raw_skills is None:
            raw_skills = []
        if not isinstance(raw_skills, list) or any(not isinstance(s, str) for s in raw_skills):
            raise ValueError(f"persona catalog schema 不合法: {role}: skills 必須是字串清單")
        catalog[role] = PersonaContract(
            role=rec["role"],
            version=rec["version"],
            summary=rec["summary"],
            allowed_phases=tuple(rec["allowed_phases"]),
            write_paths=tuple(rec["write_paths"]),
            allowed_tools=tuple(rec["allowed_tools"]),
            skills=tuple(raw_skills),
        )
    _warn_unknown_skills(catalog)
    return catalog


def _warn_unknown_skills(catalog: dict[str, PersonaContract]) -> None:
    """shadow 驗證：fail-open 僅限 deck 缺席；壞目錄要警示、邏輯 bug 不吞。"""
    try:
        from paulshaclaw.deck.schema import DeckSchemaError, DEFAULT_CARDS_PATH, load_cards
    except ImportError:
        return  # deck 套件缺席（如未來拆包）→ 靜默跳過
    try:
        cards = load_cards(DEFAULT_CARDS_PATH)
    except DeckSchemaError as exc:
        warnings.warn(f"skills shadow 驗證跳過（deck 卡片目錄不可用）: {exc}", stacklevel=2)
        return
    for role, contract in catalog.items():
        for sid in contract.skills:
            if sid not in cards:
                warnings.warn(f"persona {role} 引用未知 deck card: {sid}", stacklevel=2)
