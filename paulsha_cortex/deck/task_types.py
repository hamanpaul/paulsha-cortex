from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml

from .schema import DeckSchemaError, _check_unknown_keys

TASK_TYPE_VALUES = ("feat", "fix", "docs", "test", "ci", "refactor")
DEFAULT_TASK_TYPES_PATH = Path(__file__).with_name("data") / "task-types.yaml"

_TASK_TYPES_FILE_KEYS = frozenset({"version", "task_types", "scopes"})
_TASK_TYPE_KEYS = frozenset({"description", "combo"})
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_TITLE_WITH_SCOPE_RE = re.compile(r"^(?P<type>[a-z]+)\((?P<scope>[^()\s]+)\):\s*(?P<subject>\S.*)$")
_TITLE_WITHOUT_SCOPE_RE = re.compile(r"^(?P<type>[a-z]+):\s*(?P<subject>\S.*)$")
_TITLE_PREFIX_HINT_RE = re.compile(r"^[a-z]+(?::|\()")


@dataclass(frozen=True)
class TaskTypeSpec:
    name: str
    description: str
    combo: str | None = None


@dataclass(frozen=True)
class TaskTypeTaxonomy:
    version: int
    task_types: Mapping[str, TaskTypeSpec]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class TitleClassification:
    kind: str
    task_type: str | None
    scope: str | None
    disposition: str
    reason: str


def _allowed_types_text() -> str:
    return ", ".join(TASK_TYPE_VALUES)


def _load_yaml(path: Path) -> Mapping[str, object]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DeckSchemaError(f"task_types 載入失敗: {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DeckSchemaError(f"task_types 格式錯誤（缺 mapping root）: {path}")
    return raw


def load_task_types(
    path: str | Path = DEFAULT_TASK_TYPES_PATH,
    *,
    combos: Mapping[str, object] | None = None,
) -> TaskTypeTaxonomy:
    source = Path(path)
    raw = _load_yaml(source)
    errors: list[str] = []
    _check_unknown_keys("task_types", raw, _TASK_TYPES_FILE_KEYS, errors)
    if raw.get("version") != 0:
        errors.append(f"task_types: version 不符，預期 0，實際 {raw.get('version')!r}")

    raw_task_types = raw.get("task_types")
    if not isinstance(raw_task_types, Mapping):
        errors.append("task_types: task_types 必須是 mapping")
        raw_task_types = {}
    actual_values = set(raw_task_types)
    expected_values = set(TASK_TYPE_VALUES)
    if actual_values != expected_values:
        missing = sorted(expected_values - actual_values)
        extra = sorted(actual_values - expected_values)
        if missing:
            errors.append(f"task_types: 缺少值域 {missing}")
        if extra:
            errors.append(f"task_types: 出現未知值域 {extra}")

    specs: dict[str, TaskTypeSpec] = {}
    for name in TASK_TYPE_VALUES:
        record = raw_task_types.get(name)
        if not isinstance(record, Mapping):
            errors.append(f"{name}: task_type 定義必須是 mapping")
            continue
        _check_unknown_keys(name, record, _TASK_TYPE_KEYS, errors)
        description = record.get("description")
        combo = record.get("combo")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{name}: description 必須為非空字串")
            continue
        if combo is not None and (not isinstance(combo, str) or not combo.strip()):
            errors.append(f"{name}: combo 必須為 null 或非空字串")
            continue
        if isinstance(combo, str):
            combo = combo.strip()
            if combos is not None and combo not in combos:
                errors.append(f"{name}: combo 引用不存在: {combo}")
                continue
        specs[name] = TaskTypeSpec(name=name, description=description.strip(), combo=combo)

    raw_scopes = raw.get("scopes")
    scopes: list[str] = []
    if not isinstance(raw_scopes, list) or not raw_scopes:
        errors.append("task_types: scopes 必須為非空清單")
    else:
        seen_scopes: set[str] = set()
        for scope in raw_scopes:
            if not isinstance(scope, str) or _TOKEN_RE.fullmatch(scope) is None:
                errors.append(f"task_types: 非法 scope token {scope!r}")
                continue
            if scope in seen_scopes:
                errors.append(f"task_types: scopes 不得重複: {scope}")
                continue
            seen_scopes.add(scope)
            scopes.append(scope)

    if errors:
        raise DeckSchemaError(f"task_types 驗證失敗: {source}: " + "; ".join(errors))
    return TaskTypeTaxonomy(
        version=0,
        task_types=MappingProxyType(dict(specs)),
        scopes=tuple(scopes),
    )


def classify_title(title: str, taxonomy: TaskTypeTaxonomy) -> TitleClassification:
    text = title.strip()
    match = _TITLE_WITH_SCOPE_RE.fullmatch(text)
    if match is not None:
        task_type = match.group("type")
        scope = match.group("scope")
        if task_type not in taxonomy.task_types:
            return TitleClassification(
                kind="unknown_type",
                task_type=None,
                scope=scope,
                disposition="fail_closed",
                reason=(
                    f"unknown_type: {task_type!r} 不在凍結值域"
                    f"（允許: {_allowed_types_text()}）"
                ),
            )
        if scope not in taxonomy.scopes:
            return TitleClassification(
                kind="ambiguous",
                task_type=task_type,
                scope=scope,
                disposition="fail_closed",
                reason=(
                    f"ambiguous: scope {scope!r} 不在受控詞典"
                    f"（允許: {', '.join(taxonomy.scopes)}）"
                ),
            )
        return TitleClassification(
            kind="matched",
            task_type=task_type,
            scope=scope,
            disposition="proceed",
            reason=f"matched: {task_type}({scope})",
        )

    match = _TITLE_WITHOUT_SCOPE_RE.fullmatch(text)
    if match is not None:
        task_type = match.group("type")
        if task_type not in taxonomy.task_types:
            return TitleClassification(
                kind="unknown_type",
                task_type=None,
                scope=None,
                disposition="fail_closed",
                reason=(
                    f"unknown_type: {task_type!r} 不在凍結值域"
                    f"（允許: {_allowed_types_text()}）"
                ),
            )
        return TitleClassification(
            kind="matched",
            task_type=task_type,
            scope=None,
            disposition="proceed",
            reason=f"matched: {task_type}",
        )

    if _TITLE_PREFIX_HINT_RE.match(text):
        return TitleClassification(
            kind="unparseable",
            task_type=None,
            scope=None,
            disposition="bypass",
            reason="unparseable: conventional-commit prefix malformed",
        )
    return TitleClassification(
        kind="absent",
        task_type=None,
        scope=None,
        disposition="bypass",
        reason="absent: no conventional-commit prefix",
    )
