from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from paulsha_cortex.config import paths
from paulsha_cortex.deck.schema import BAND_LEVELS

from .._yaml import YAMLError, safe_load
from . import model_resolution
from .launcher import build_agy_argv

logger = logging.getLogger(__name__)

# #452 B：schema v3 新增封套四欄位＋profile_provenance（全選填）。v1/v2 檔案
# 照載（缺省欄位由查表投影套 DEFAULT_ENVELOPE，見 project_envelope）。
MODEL_IDENTITY_SCHEMA_VERSION = 3
SUPPORTED_MODEL_IDENTITY_SCHEMAS = frozenset({1, 2, 3})
# `agy models` 現在輸出 kebab id（例如 `gemini-3.1-pro-high`），不是顯示名。
# 這裡的常數就是 registry 用來比對／查找的 canonical model_id，必須跟 CLI
# 實際輸出一致，否則 probe_agy_capability 會字面比對失敗（issue #255）。
AGY_MODEL_ID = "gemini-3.1-pro-high"
# 舊版顯示名，僅為相容 v1 schema 的既有設定檔（見 `_is_canonical_agy_model_id`）。
_AGY_MODEL_ID_LEGACY_DISPLAY_NAME = "Gemini 3.1 Pro (High)"
AGY_DOMAIN = "google"
AGY_LIVE_PROBE = "agy-plan-sandbox"
#: #534 起**不再是解析順序**：`select_secondary_planner` 改走三層解析鏈
#: （model_resolution）。保留本常數僅為既有 executor↔domain 對應的歷史記錄
#: （docs/superpowers/specs/model-persona-roster-matrix.md 引用），不參與任何選擇。
PLANNER_PRIORITY = (
    ("agy", "google"),
    ("claude", "anthropic"),
    ("codex", "openai"),
)

# ---------------------------------------------------------------------------
# #452 B／#453 R4：能力封套（capability envelope）常數與查表投影。
# DEFAULT_ENVELOPE 自 envelope_mapping.py 整體搬移至此（#454 spec 非目標第三條
# 明文：schema v3 落地時單一真值搬家、envelope_mapping 改 import，不得複製）。
# ---------------------------------------------------------------------------

#: `#209` R2 的兩個封閉值域全集（宣告順序即 canonical 順序）。
CONSISTENCY_SCOPE_DOMAIN = (
    "code",
    "test",
    "spec",
    "openspec",
    "changelog",
    "docs",
    "pr",
    "issue",
)
ACCEPTANCE_MODES_DOMAIN = (
    "focused_tests",
    "repo_gate",
    "live_evidence",
    "github_closure",
)

#: 封套四欄位名（`#209` R2）。
ENVELOPE_FIELDS = (
    "accepts_bands",
    "invariant_ceiling",
    "consistency_scope",
    "acceptance_modes",
)

ENVELOPE_SOURCE_MEASURED = "measured"
ENVELOPE_SOURCE_DEFAULT = "default"


def _persona_default(bands: tuple[str, ...]) -> Mapping[str, object]:
    return {
        "accepts_bands": bands,
        "invariant_ceiling": None,
        "consistency_scope": CONSISTENCY_SCOPE_DOMAIN,
        "acceptance_modes": ACCEPTANCE_MODES_DOMAIN,
    }


#: `#453` R1–R4 定案的 per-persona 保守預設封套（單一真值；值為 tuple 防止
#: 呼叫端誤改常數）。builder／reviewer 不含 red（#223 攔截鏈下 red 不可達
#: build／review）；planner 全值域含 red（needs_decomposition 收斂路徑必需）；
#: `invariant_ceiling` 為 bypass sentinel ``None``，MUST NOT 讀成 0。
#: key 集合與 workflow.MODEL_CHAIN_PERSONAS 對齊（測試鎖定，不在此 import
#: workflow 以避免模組循環）。
DEFAULT_ENVELOPE: Mapping[str, Mapping[str, object]] = {
    "planner": _persona_default(tuple(BAND_LEVELS)),
    "builder": _persona_default(tuple(BAND_LEVELS[:2])),
    "reviewer": _persona_default(tuple(BAND_LEVELS[:2])),
}

#: 封套語境下合法的 persona 集合（＝DEFAULT_ENVELOPE 的 key 集合）。
ENVELOPE_PERSONAS = frozenset(DEFAULT_ENVELOPE)

#: profile_provenance 允許的 key（fingerprint／source 必填，其餘選填）。
_PROVENANCE_ALLOWED_KEYS = frozenset(
    {"fingerprint", "source", "reasons", "observation", "profiled_at"}
)
#: 評測指紋六元組（#455 §4.1 定案，不含 pricing）。
PROFILE_FINGERPRINT_KEYS = (
    "executor",
    "model_id",
    "persona",
    "deck_id",
    "deck_content_sha256",
    "patchmud_version",
)


def _assert_no_duplicate_yaml_keys(text: str) -> None:
    contexts: list[tuple[int, set[str]]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if ":" not in item_text:
                while contexts and contexts[-1][0] > indent:
                    contexts.pop()
                continue
            key = item_text.split(":", 1)[0].strip()
            context_indent = indent + 2
            while contexts and contexts[-1][0] >= context_indent:
                contexts.pop()
            contexts.append((context_indent, set()))
            if key in contexts[-1][1]:
                raise ValueError(f"duplicate key '{key}' at line {lineno}")
            contexts[-1][1].add(key)
            continue
        if ":" not in stripped:
            continue
        key = stripped.split(":", 1)[0].strip()
        while contexts and contexts[-1][0] > indent:
            contexts.pop()
        if not contexts or contexts[-1][0] < indent:
            contexts.append((indent, set()))
        if key in contexts[-1][1]:
            raise ValueError(f"duplicate key '{key}' at line {lineno}")
        contexts[-1][1].add(key)


def _is_canonical_agy_model_id(model_id: str) -> bool:
    """v1 設定檔可能仍寫著舊顯示名；兩種拼法都視為 canonical agy 身分。"""
    return model_id in (AGY_MODEL_ID, _AGY_MODEL_ID_LEGACY_DISPLAY_NAME)


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _validate_string_subset(
    value: object, *, field: str, domain: tuple[str, ...]
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string list")
    items = tuple(item.strip() for item in value)
    if len(set(items)) != len(items):
        raise ValueError(f"{field} contains duplicates")
    invalid = [item for item in items if item not in domain]
    if invalid:
        raise ValueError(f"{field} contains value outside domain: {invalid[0]!r}")
    return items


def _validate_profile_provenance(
    value: object,
    *,
    index: int,
    executor: str,
    model_id: str,
    measured_fields: tuple[str, ...],
) -> Mapping[str, object]:
    prefix = f"model-identities[{index}].profile_provenance"
    if not isinstance(value, Mapping):
        raise ValueError(f"{prefix} must be an object")
    extras = set(value) - _PROVENANCE_ALLOWED_KEYS
    if extras:
        raise ValueError(f"{prefix}.{sorted(extras)[0]} unexpected")
    fingerprint = value.get("fingerprint")
    if not isinstance(fingerprint, Mapping) or set(fingerprint) != set(
        PROFILE_FINGERPRINT_KEYS
    ):
        raise ValueError(
            f"{prefix}.fingerprint must declare exactly {list(PROFILE_FINGERPRINT_KEYS)}"
        )
    for key in PROFILE_FINGERPRINT_KEYS:
        _nonempty(fingerprint.get(key), f"{prefix}.fingerprint.{key}")
    if fingerprint["executor"].strip() != executor or fingerprint["model_id"].strip() != model_id:
        raise ValueError(f"{prefix}.fingerprint identity mismatch: {executor}/{model_id}")
    persona = fingerprint["persona"].strip()
    if persona not in ENVELOPE_PERSONAS:
        raise ValueError(f"{prefix}.fingerprint.persona invalid: {persona!r}")
    source = value.get("source")
    if not isinstance(source, Mapping) or set(source) - set(ENVELOPE_FIELDS):
        raise ValueError(f"{prefix}.source keys must be a subset of {list(ENVELOPE_FIELDS)}")
    for field_name, field_source in source.items():
        if field_source not in (ENVELOPE_SOURCE_MEASURED, ENVELOPE_SOURCE_DEFAULT):
            raise ValueError(f"{prefix}.source.{field_name} invalid: {field_source!r}")
    # #453 R4：registry 檔案永不寫入預設值——「有寫」必為 measured、measured 必「有寫」。
    declared_measured = tuple(
        field_name
        for field_name in ENVELOPE_FIELDS
        if source.get(field_name) == ENVELOPE_SOURCE_MEASURED
    )
    if set(declared_measured) != set(measured_fields):
        raise ValueError(
            f"{prefix}.source measured fields {sorted(declared_measured)} do not match "
            f"row envelope fields {sorted(measured_fields)}"
        )
    for key in ("reasons", "observation"):
        if key in value and not isinstance(value[key], Mapping):
            raise ValueError(f"{prefix}.{key} must be an object")
    if "profiled_at" in value:
        _nonempty(value["profiled_at"], f"{prefix}.profiled_at")
    return value


def _validate_envelope_columns(
    row: Mapping[str, object], *, index: int, executor: str, model_id: str
) -> dict[str, object]:
    """#452 B：schema v3 封套欄位的 fail-closed 驗證（#209 R2 值域契約）。"""

    envelope: dict[str, object] = {}
    if "accepts_bands" in row:
        envelope["accepts_bands"] = _validate_string_subset(
            row["accepts_bands"],
            field=f"model-identities[{index}].accepts_bands",
            domain=tuple(BAND_LEVELS),
        )
    if "invariant_ceiling" in row:
        ceiling = row["invariant_ceiling"]
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
            raise ValueError(
                f"model-identities[{index}].invariant_ceiling must be an integer >= 0"
            )
        envelope["invariant_ceiling"] = ceiling
    if "consistency_scope" in row:
        envelope["consistency_scope"] = _validate_string_subset(
            row["consistency_scope"],
            field=f"model-identities[{index}].consistency_scope",
            domain=CONSISTENCY_SCOPE_DOMAIN,
        )
    if "acceptance_modes" in row:
        envelope["acceptance_modes"] = _validate_string_subset(
            row["acceptance_modes"],
            field=f"model-identities[{index}].acceptance_modes",
            domain=ACCEPTANCE_MODES_DOMAIN,
        )
    measured_fields = tuple(sorted(envelope))
    provenance = row.get("profile_provenance")
    if measured_fields and provenance is None:
        raise ValueError(
            f"model-identities[{index}] envelope fields require profile_provenance"
        )
    if provenance is not None:
        if not measured_fields:
            raise ValueError(
                f"model-identities[{index}].profile_provenance requires at least one "
                "measured envelope field"
            )
        envelope["profile_provenance"] = _validate_profile_provenance(
            provenance,
            index=index,
            executor=executor,
            model_id=model_id,
            measured_fields=measured_fields,
        )
    return envelope


@dataclass(frozen=True)
class ModelIdentity:
    executor: str
    model_id: str
    independence_domain: str
    capabilities: tuple[str, ...] = ()
    live_probe: str | None = None
    # #452 B（schema v3）：封套四欄位＋profile_provenance，全選填。registry 檔案
    # 永不寫入預設值（#453 R4）——欄位缺省＝查表投影時套 DEFAULT_ENVELOPE 並標
    # source=default；欄位有值＝patchmud 實測（provenance.source 必為 measured）。
    accepts_bands: tuple[str, ...] | None = None
    invariant_ceiling: int | None = None
    consistency_scope: tuple[str, ...] | None = None
    acceptance_modes: tuple[str, ...] | None = None
    # dict 不可 hash：排除在 __hash__ 之外（等值比較仍包含本欄位，shadow 檢查
    # 語意不變——內容不同的同鍵身分在 load_model_identities 會被判為 overlay
    # 覆寫 packaged，而不是相同列）。
    profile_provenance: Mapping[str, object] | None = field(default=None, hash=False)
    # #534：解析層 provenance。三者皆為 loader 蓋章／overlay 指令，**不是** YAML
    # 身分欄位，因此一律排除在等值比較與 hash 之外（shadow 判定只看身分內容），
    # 也不進 `to_dict()`（registry 檔案格式不變，`write_registry_file` 寫出的
    # packaged roster 逐欄與 #534 之前相同）。
    #: 身分來自 host overlay 或 packaged roster（見 model_resolution）。
    origin: str = field(
        default=model_resolution.IDENTITY_ORIGIN_OVERLAY, compare=False, hash=False
    )
    #: overlay 對 packaged 身分的處置：None／"park"／"demote"（#509 殘項）。
    operator_action: str | None = field(default=None, compare=False, hash=False)
    #: overlay 是否明示宣告本列覆寫同鍵 packaged 身分（#509「合法覆寫語意」）。
    override_packaged: bool = field(default=False, compare=False, hash=False)

    def legacy_dict(self) -> dict[str, str]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "independence_domain": self.independence_domain,
        }

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = self.legacy_dict()
        payload["capabilities"] = list(self.capabilities)
        if self.live_probe is not None:
            payload["live_probe"] = self.live_probe
        if self.accepts_bands is not None:
            payload["accepts_bands"] = list(self.accepts_bands)
        if self.invariant_ceiling is not None:
            payload["invariant_ceiling"] = self.invariant_ceiling
        if self.consistency_scope is not None:
            payload["consistency_scope"] = list(self.consistency_scope)
        if self.acceptance_modes is not None:
            payload["acceptance_modes"] = list(self.acceptance_modes)
        if self.profile_provenance is not None:
            payload["profile_provenance"] = json.loads(
                json.dumps(self.profile_provenance, ensure_ascii=False, sort_keys=True)
            )
        return payload

    def measured_envelope_fields(self) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in ENVELOPE_FIELDS
            if getattr(self, field_name) is not None
        )


@dataclass(frozen=True)
class IdentityRegistry:
    schema_version: int
    identities: tuple[ModelIdentity, ...]
    # #534：本次載入的解析上下文（政策／評估合格清單／診斷）。手工建構的
    # registry 不帶上下文，一律走 DEFAULT_CONTEXT（全視為 operator 指定，
    # 維持既有順序語意）。不參與等值比較與 hash。
    resolution: "model_resolution.ResolutionContext | None" = field(
        default=None, compare=False, hash=False
    )

    @property
    def resolution_context(self) -> "model_resolution.ResolutionContext":
        return self.resolution or model_resolution.DEFAULT_CONTEXT

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, object]],
        *,
        schema_version: int = MODEL_IDENTITY_SCHEMA_VERSION,
        origin: str = model_resolution.IDENTITY_ORIGIN_OVERLAY,
    ) -> "IdentityRegistry":
        identities: list[ModelIdentity] = []
        seen: set[tuple[str, str]] = set()
        allowed = {
            "executor",
            "model_id",
            "independence_domain",
            "capabilities",
            "live_probe",
            # #534／#509：overlay 明示覆寫同鍵 packaged 身分的旗標（選配）。
            "override_packaged",
        }
        if int(schema_version) >= 3:
            # schema v3（#452 B）才認得封套欄位；v1/v2 帶了一律 fail-closed。
            # #452 對抗審查修正：閘門由 schema_version 直接推導（原本獨立的
            # allow_envelope 參數預設 True，讓「封套欄位是 v3 才有的契約」只在
            # 檔案載入層成立、建構層可繞過）。
            allowed |= set(ENVELOPE_FIELDS) | {"profile_provenance"}
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError(f"model-identities[{index}] must be an object")
            extras = set(row) - allowed
            if extras:
                raise ValueError(f"model-identities[{index}].{sorted(extras)[0]} unexpected")
            executor = _nonempty(row.get("executor"), f"model-identities[{index}].executor")
            model_id = _nonempty(row.get("model_id"), f"model-identities[{index}].model_id")
            domain = _nonempty(
                row.get("independence_domain"),
                f"model-identities[{index}].independence_domain",
            )
            capabilities_raw = row.get("capabilities", [])
            if not isinstance(capabilities_raw, list) or any(
                not isinstance(item, str) or not item.strip() for item in capabilities_raw
            ):
                raise ValueError(f"model-identities[{index}].capabilities must be a string list")
            capabilities = tuple(item.strip() for item in capabilities_raw)
            if len(set(capabilities)) != len(capabilities):
                raise ValueError(f"model-identities[{index}].capabilities contains duplicates")
            live_probe_raw = row.get("live_probe")
            live_probe = (
                None
                if live_probe_raw is None
                else _nonempty(live_probe_raw, f"model-identities[{index}].live_probe")
            )
            override_packaged = row.get("override_packaged", False)
            if not isinstance(override_packaged, bool):
                raise ValueError(
                    f"model-identities[{index}].override_packaged must be a boolean"
                )
            if executor == "agy" and "planning" in capabilities:
                if domain != AGY_DOMAIN or live_probe != AGY_LIVE_PROBE:
                    raise ValueError(
                        f"model-identities[{index}] agy planning requires google and {AGY_LIVE_PROBE}"
                    )
            envelope = _validate_envelope_columns(
                row, index=index, executor=executor, model_id=model_id
            )
            key = (executor, model_id)
            if key in seen:
                raise ValueError(f"model-identities duplicate identity: {executor}/{model_id}")
            seen.add(key)
            identities.append(
                ModelIdentity(
                    executor=executor,
                    model_id=model_id,
                    independence_domain=domain,
                    capabilities=capabilities,
                    live_probe=live_probe,
                    origin=origin,
                    override_packaged=override_packaged,
                    **envelope,
                )
            )
        return cls(schema_version=schema_version, identities=tuple(identities))

    def get(self, executor: str, model_id: str) -> ModelIdentity | None:
        for identity in self.identities:
            if (identity.executor, identity.model_id) == (executor, model_id):
                return identity
        return None

    def require(self, executor: str, model_id: str) -> ModelIdentity:
        identity = self.get(executor, model_id)
        if identity is None:
            raise ValueError(f"model identity unknown: {executor}/{model_id}")
        return identity

    def legacy_mapping(self) -> dict[tuple[str, str], dict[str, str]]:
        return {
            (identity.executor, identity.model_id): identity.legacy_dict()
            for identity in self.identities
        }


def _packaged_registry_path() -> Path:
    return Path(__file__).with_name("data") / "model-identities.yaml"


def _load_model_identity_file(
    path: Path, *, origin: str = model_resolution.IDENTITY_ORIGIN_OVERLAY
) -> IdentityRegistry:
    if not path.is_file():
        raise ValueError(f"model-identities missing: {path}")
    try:
        text = path.read_text(encoding="utf-8")
        _assert_no_duplicate_yaml_keys(text)
        payload = safe_load(text)
    except (OSError, UnicodeDecodeError, YAMLError, ValueError) as exc:
        raise ValueError(f"model-identities unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"model-identities invalid root: {path}")
    # #534：新增兩個**選配**頂層區塊——`packaged_overrides`（demote／park packaged
    # 身分）與 `resolution_policy`（packaged fallback 政策）。既有 overlay 檔案不
    # 帶這兩個 key 照舊合法，不需為升級改任何一行。
    extras = set(payload) - {
        "schema_version",
        "identities",
        "packaged_overrides",
        "resolution_policy",
    }
    if extras:
        raise ValueError(f"model-identities unexpected top-level key: {sorted(extras)[0]}")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version not in SUPPORTED_MODEL_IDENTITY_SCHEMAS:
        raise ValueError(
            "model-identities schema_version must be one of "
            f"{sorted(SUPPORTED_MODEL_IDENTITY_SCHEMAS)}, got {schema_version!r}"
        )
    rows = payload.get("identities")
    if not isinstance(rows, list):
        raise ValueError("model-identities identities must be a list")
    if schema_version == 1:
        normalized_rows = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"model-identities[{index}] must be an object")
            extras = set(row) - {"executor", "model_id", "independence_domain"}
            if extras:
                raise ValueError(f"model-identities[{index}].{sorted(extras)[0]} unexpected")
            normalized = dict(row)
            executor = normalized.get("executor")
            model_id = normalized.get("model_id")
            # v1 had no capability field. Preserve its identities as planning
            # fallback candidates; selection still requires a matching live
            # probe and a foreign independence domain.
            if executor != "agy" or (
                isinstance(model_id, str) and _is_canonical_agy_model_id(model_id)
            ):
                normalized["capabilities"] = ["planning"]
            if executor == "agy" and isinstance(model_id, str) and _is_canonical_agy_model_id(
                model_id
            ):
                normalized["live_probe"] = AGY_LIVE_PROBE
            normalized_rows.append(normalized)
        rows = normalized_rows
    # 封套欄位是 schema v3（#452 B）才有的契約；v1/v2 檔案帶了照舊 fail-closed
    # （閘門在 from_rows 內由 schema_version 推導，建構層與檔案層同一條規則）。
    registry = IdentityRegistry.from_rows(
        rows, schema_version=int(schema_version), origin=origin
    )
    overrides = model_resolution.parse_packaged_overrides(payload.get("packaged_overrides"))
    # 沒有 overlay 檔案的部署＝operator 未宣告任何東西，packaged roster 就是全世界
    # ——此時 fallback 預設 allow（僅留 provenance）；有 overlay 檔案時預設 warn
    # （fail-loud），operator 可自行改成 deny 走嚴格 fail-closed。
    policy = model_resolution.parse_resolution_policy(
        payload.get("resolution_policy"),
        default=model_resolution.PACKAGED_FALLBACK_WARN,
    )
    context = model_resolution.ResolutionContext(
        policy=policy,
        packaged_overrides=overrides,
        config_root=str(path.parent),
        overlay_present=origin == model_resolution.IDENTITY_ORIGIN_OVERLAY,
    )
    return replace(registry, resolution=context)


def load_model_identities(
    config_root: str | Path | None = None,
    *,
    use_packaged_default: bool = True,
) -> IdentityRegistry:
    """載入 host overlay ＋ packaged 候選池，並蓋上 #534 的解析層 provenance。

    #534：overlay 絕對優先。overlay 列在合併結果中一律排在 packaged 之前，且
    **同鍵時 overlay 覆寫 packaged**——後者過去是 `raise ValueError`，一列過期
    設定就能打掛整條 periodic tick（#509）。現在改為：

    - 逐欄相等 → 視為同一列，沿用 overlay 那份（無診斷）；
    - 內容不同且 overlay 明示 ``override_packaged: true`` → 合法覆寫（info 診斷）；
    - 內容不同但未明示 → 仍以 overlay 為準（裁決：人工指定優先），但留下 warn
      診斷並打 log，請 operator 補旗標或移除該列。**不再中止載入。**
    """

    root = Path(config_root) if config_root is not None else paths.project_config_root()
    custom_path = root / "model-identities.yaml"
    if not use_packaged_default:
        overlay_only = _load_model_identity_file(
            custom_path, origin=model_resolution.IDENTITY_ORIGIN_OVERLAY
        )
        return replace(
            overlay_only,
            resolution=replace(
                overlay_only.resolution_context,
                eval_roster=model_resolution.load_eval_roster_degraded(root),
            ),
        )

    packaged = _load_model_identity_file(
        _packaged_registry_path(), origin=model_resolution.IDENTITY_ORIGIN_PACKAGED
    )
    eval_roster = model_resolution.load_eval_roster_degraded(root)
    notes: list[model_resolution.ResolutionNote] = []
    if eval_roster.load_error is not None:
        notes.append(
            model_resolution.ResolutionNote(
                "eval-roster-unreadable",
                "fail",
                f"{eval_roster.load_error}（第 2 層視為空清單，解析不會因此多授予資格）",
            )
        )
    if not custom_path.is_file():
        # operator 未宣告任何 overlay：packaged 就是全世界，fallback 預設 allow。
        context = model_resolution.ResolutionContext(
            policy=model_resolution.ResolutionPolicy(
                model_resolution.PACKAGED_FALLBACK_ALLOW
            ),
            eval_roster=eval_roster,
            notes=tuple(notes),
            config_root=str(root),
            overlay_present=False,
        )
        return replace(packaged, resolution=context)
    custom = _load_model_identity_file(
        custom_path, origin=model_resolution.IDENTITY_ORIGIN_OVERLAY
    )
    overlay_context = custom.resolution_context
    packaged_by_key = {
        (item.executor, item.model_id): item for item in packaged.identities
    }
    overlay_keys = {(item.executor, item.model_id) for item in custom.identities}
    additions: list[ModelIdentity] = []
    shadowed: set[tuple[str, str]] = set()
    for identity in custom.identities:
        key = (identity.executor, identity.model_id)
        packaged_identity = packaged_by_key.get(key)
        if packaged_identity is not None:
            shadowed.add(key)
            if packaged_identity != identity:
                if identity.override_packaged:
                    notes.append(
                        model_resolution.ResolutionNote(
                            "packaged-override",
                            "info",
                            f"host overlay 明示覆寫 packaged 身分 {key[0]}/{key[1]}",
                        )
                    )
                else:
                    detail = (
                        f"host overlay 的 {key[0]}/{key[1]} 與 packaged roster 同鍵但內容不同，"
                        "已以 overlay 為準（人工指定優先）。請於該列加上 "
                        "`override_packaged: true` 明示覆寫意圖，或移除該列改用 packaged 版本。"
                    )
                    notes.append(
                        model_resolution.ResolutionNote(
                            "unflagged-packaged-override", "warn", detail
                        )
                    )
                    logger.warning("model-identities %s", detail)
        additions.append(identity)
    # #509 殘項：overlay 可 demote／park packaged 身分。
    overrides_by_key = {item.key: item for item in overlay_context.packaged_overrides}
    for key, override in overrides_by_key.items():
        if key not in packaged_by_key:
            raise ValueError(
                "model-identities packaged_overrides 指向不存在的 packaged 身分: "
                f"{key[0]}/{key[1]}（可處置的 packaged 身分: "
                + ", ".join(f"{a}/{b}" for a, b in packaged_by_key)
                + "）"
            )
        if key in overlay_keys:
            raise ValueError(
                "model-identities packaged_overrides 與 identities 同時宣告同一身分: "
                f"{key[0]}/{key[1]}（兩者意圖矛盾：請擇一）"
            )
        notes.append(
            model_resolution.ResolutionNote(
                f"packaged-{override.action}",
                "info",
                f"host overlay {override.action} packaged 身分 {key[0]}/{key[1]}：{override.reason}",
            )
        )
    retained: list[ModelIdentity] = []
    for identity in packaged.identities:
        key = (identity.executor, identity.model_id)
        if key in shadowed:
            # overlay 已宣告同鍵身分：以 overlay 那列為準，packaged 版本不重複登錄。
            continue
        override = overrides_by_key.get(key)
        retained.append(
            identity if override is None else replace(identity, operator_action=override.action)
        )
    context = model_resolution.ResolutionContext(
        policy=overlay_context.policy,
        eval_roster=eval_roster,
        packaged_overrides=overlay_context.packaged_overrides,
        notes=tuple(notes),
        config_root=str(root),
        overlay_present=True,
    )
    return IdentityRegistry(
        schema_version=MODEL_IDENTITY_SCHEMA_VERSION,
        identities=tuple(additions) + tuple(retained),
        resolution=context,
    )


@dataclass(frozen=True)
class CapabilityProbe:
    ready: bool
    executor: str
    model_id: str
    independence_domain: str
    reason: str | None = None
    diagnostic: str | None = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.executor, self.model_id, self.independence_domain)

    @classmethod
    def ready_for(cls, executor: str, model_id: str, domain: str) -> "CapabilityProbe":
        return cls(True, executor, model_id, domain)


ProcessRunner = Callable[..., object]


def _process_fields(raw: object) -> tuple[int, str, str]:
    returncode = getattr(raw, "returncode", None)
    stdout = getattr(raw, "stdout", None)
    stderr = getattr(raw, "stderr", None)
    if not isinstance(returncode, int) or not isinstance(stdout, str) or not isinstance(stderr, str):
        raise ValueError("malformed process result")
    return returncode, stdout, stderr


def _failed_agy(reason: str, diagnostic: str | None = None) -> CapabilityProbe:
    return CapabilityProbe(False, "agy", AGY_MODEL_ID, AGY_DOMAIN, reason, diagnostic)


# ---------------------------------------------------------------------------
# issue #682（#672 票 A）：錯誤語意三分
#
# 修法前 planning 失敗只有一個字面值 `no-heterogeneous-planner`——它把三類
# 結構上完全不同的失敗壓成同一個「拓撲問題」：
#
#   1. job／executor 起不來（沙箱、PATH、runtime、polkit、模板未安裝）
#   2. executor 起來了但異常退出（含「連錯誤訊息都沒有」的靜默退出）
#   3. executor 正常退出但輸出不合約（parse 不了、內容不符、model 不在列）
#
# #670 就是被這樣誤診的：真因是 `agy models` 兩欄漂移造成 100%
# `model-not-listed` ＋ code fence 造成 25% parse 失敗，blocking reason 說的
# 卻是「沒有異質 planner」，排查方向整個帶偏。
#
# 族名在此定義（design D8／spec R6）；票 C（probe 快取）與票 E
# （`JobPlanningInvoker`）落地時直接消費同一組常數，不再各自發明一套。
# ---------------------------------------------------------------------------

#: job 起不來：polkit 拒絕、模板未安裝、shim 不可執行、spec spool 不可寫、
#: instance 已 active、`confirm_template_instance_started` 逾時。
PLANNING_FAILURE_JOB_START = "planning-job-start-failed"
#: job／行程起來了，但 executor 非零退出或零輸出。
PLANNING_FAILURE_EXECUTOR = "planning-executor-failed"
#: executor 正常退出，輸出不符契約（parse 失敗、身分不符、model 不在列）。
PLANNING_FAILURE_OUTPUT = "planning-output-malformed"
#: `planning-executor-failed` 的子類：rc≠0 且 stdout／stderr 皆空。
#:
#: 這是整個家族裡最難查的一種——**連錯誤訊息都沒有**，歸因於是會落到模型、
#: prompt、逾時或憑證，而不會落到執行環境。它 MUST 被顯式命名，MUST NOT 被
#: 壓成任何拓撲原因（spec R6）。
PLANNING_FAILURE_EXECUTOR_SILENT_EXIT = "executor-silent-exit"
#: 尚未歸類的 probe 失敗。**fail-closed**：不當作環境問題，只當作「這個族還
#: 沒被對應過」的可見標記——新增 probe 失敗原因時會直接在拒因表上現形，而不是
#: 被默默塞進三族之一。
PLANNING_FAILURE_UNCLASSIFIED = "planning-probe-unclassified"

PLANNING_FAILURE_FAMILIES = (
    PLANNING_FAILURE_JOB_START,
    PLANNING_FAILURE_EXECUTOR,
    PLANNING_FAILURE_OUTPUT,
    PLANNING_FAILURE_UNCLASSIFIED,
)

#: 哪些族屬「環境」——命中者讓 `manager._classify_planning_failure` 改判
#: `environment`，`_resume_decision` 因而得以浮現 `recover-planning`。
#: `planning-output-malformed` 刻意**不在**此列：那是模型內容／格式問題，
#: 維持 `content` 的 fail-closed 意圖（反向誤報同樣不可接受）。
ENVIRONMENT_GRADE_PLANNING_FAMILIES = frozenset(
    {PLANNING_FAILURE_JOB_START, PLANNING_FAILURE_EXECUTOR}
)

#: probe 失敗原因 → 三分族的明表。散落的 `if` 是漂移的來源，這裡只留一張表。
_PROBE_REASON_FAMILIES: dict[str, str] = {
    # `agy models` 這一次 CLI 呼叫死掉／非零退出：CLI 根本起不來或環境不對。
    "models-probe-failed": PLANNING_FAILURE_EXECUTOR,
    # smoke 呼叫非零退出。
    "smoke-failed": PLANNING_FAILURE_EXECUTOR,
    # CLI 正常退出但列表裡沒有我們要的 model（#670 的兩欄漂移就落在這裡）：
    # 輸出與 roster 契約不符，不是環境壞掉。
    "model-not-listed": PLANNING_FAILURE_OUTPUT,
    "malformed-output": PLANNING_FAILURE_OUTPUT,
    "identity-mismatch": PLANNING_FAILURE_OUTPUT,
    # 票 C／票 E 之後 probe 自己就會回族名，這裡讓它原樣通過。
    PLANNING_FAILURE_JOB_START: PLANNING_FAILURE_JOB_START,
    PLANNING_FAILURE_EXECUTOR: PLANNING_FAILURE_EXECUTOR,
    PLANNING_FAILURE_OUTPUT: PLANNING_FAILURE_OUTPUT,
}

#: `_probe_identity` 的 `safe-probe-failed` diagnostic 的**第一個 token** 是例外型別名，
#: 而它就是唯一能機械分辨「executor 死了」還是「輸出不合約」的線索。
#:
#: **#727 之前那是 diagnostic 的全部內容**，而代價逐字記在 #727：codex 是唯一有憑證、
#: 剖面也對的 planner 候選，它連續四輪派工留下的全部資訊是 `ValueError` 五個字，
#: 定位得靠 `sudo cat planning-probe-cache.json` → 手動重跑 probe → 把串流餵進
#: `_extract_json`。#727 起型別名後面接一段**有界**上下文（rc、stdout 節錄、`-o`
#: 落點在不在），型別名本身**錨在第 0 個 token**——分級的輸入因此逐字不變
#: （票 A 的 `grade=` 錨在開頭是同一條理由）。
_PROBE_FAILED_REASON = "safe-probe-failed"
_ENVIRONMENT_EXCEPTION_NAMES = frozenset(
    {
        "BrokenPipeError",
        "CalledProcessError",
        "ConnectionError",
        "ConnectionResetError",
        "FileNotFoundError",
        "IsADirectoryError",
        "NotADirectoryError",
        "OSError",
        "PermissionError",
        "SubprocessError",
        "TimeoutError",
        "TimeoutExpired",
    }
)
_OUTPUT_EXCEPTION_NAMES = frozenset(
    {
        "JSONDecodeError",
        "KeyError",
        "TypeError",
        "ValueError",
    }
)


def classify_probe_failure(reason: str | None, diagnostic: str | None = None) -> str:
    """probe 的失敗 reason（＋diagnostic）→ 三分族。

    未知 reason 一律落 `planning-probe-unclassified`（content 級）——**寧可
    標成未分類，也不擅自宣稱是環境問題**。把未知失敗當 environment 會讓
    `recover-planning` 對著一個永遠不會自癒的失敗一直重試；當 content 則最多
    是多要一次人工判斷，而且拒因表上會直接看到「unclassified」這個字。
    """

    if not reason:
        return PLANNING_FAILURE_UNCLASSIFIED
    if reason == _PROBE_FAILED_REASON:
        # diagnostic 由 `probe_exception_diagnostic()` 產生，**第 0 個 token 恆為
        # `type(exc).__name__`**；#727 起後面可能再接一段有界上下文（rc／stdout 節錄／
        # `-o` 落點狀態）。分級只看第 0 個 token，因此那段上下文加不加、加多少，
        # 對三分結果一個位元都不影響——這正是「診斷是唯讀資訊，不得讓任何 probe 從
        # not-ready 變成 ready」那條保守方向在分類器這一側的落點。
        name = (diagnostic or "").strip().split(" ", 1)[0]
        if name in _ENVIRONMENT_EXCEPTION_NAMES:
            return PLANNING_FAILURE_EXECUTOR
        if name in _OUTPUT_EXCEPTION_NAMES:
            return PLANNING_FAILURE_OUTPUT
        return PLANNING_FAILURE_UNCLASSIFIED
    return _PROBE_REASON_FAMILIES.get(reason, PLANNING_FAILURE_UNCLASSIFIED)


def _exit_diagnostic(returncode: int, stdout: str, stderr: str) -> str:
    """非零退出的 diagnostic：`exit-code:N`，全空輸出時加註 silent-exit 子類。

    只讀 rc 與「stdout／stderr 是否為空」兩件事，**不把 stderr 內容帶進
    diagnostic**——stderr 是最容易夾帶路徑、env 與憑證錯誤原文的通道，而
    diagnostic 會一路進 log／evidence／`blocking_reason`。
    """

    if not stdout.strip() and not stderr.strip():
        return f"exit-code:{returncode} {PLANNING_FAILURE_EXECUTOR_SILENT_EXIT}"
    return f"exit-code:{returncode}"


# issue #670：開頭 fence 允許帶 info string（``` 後面的語言標籤，例如 `json`），
# 但字元集刻意收斂到「語言標籤長得出來的樣子」——絕不可含 `{`／反引號，否則
# 單行形態 ```` ```{"a":1}``` ```` 會被整串當成 info string 吃掉。
_CODE_FENCE_OPEN = re.compile(r"\A```[A-Za-z0-9_+.\-]*[ \t]*\r?\n?")
_CODE_FENCE_CLOSE = re.compile(r"\r?\n?[ \t]*```\Z")

#: `_failed_agy` diagnostic 帶的原始 stdout 節錄長度上限（issue #670）。
STDOUT_EXCERPT_LIMIT = 200


def strip_code_fence(text: str) -> str:
    """剝掉「整串剛好被一個 markdown code fence 包住」時的 fence，回傳本體。

    issue #670：`probe_agy_capability` 問的是語言模型，實測約 1/6 次會把
    正確的 JSON 包進 ```` ```json ```` fence，於是 `json.loads` 拋錯 ⇒
    `malformed-output` ⇒ probe not ready ⇒ `no-heterogeneous-planner` ⇒ run
    進 `needs_human`。內容明明正確，卻被誤報成拓撲問題。

    支援的形態：``` 與 ```json 兩種開頭、有無尾隨 fence、fence 前後的空白／
    換行、以及單行的 ``` ```json {...}``` ```。

    刻意**只**處理「整串就是單一 fenced block」：帶前言散文的輸出
    （``"Here you go:\\n```json\\n{...}\\n```"``）原樣回傳，交給呼叫端的
    `json.loads` 失敗成 `malformed-output`。這與 `planning_runtime`
    `_find_json_object` 的頂層嚴格語意一致——剝 fence 是**純結構**動作，
    不負責從散文裡撈 JSON，更不負責救「內容真的不對」的輸出：本函式回傳後
    仍要走原本的 `json.loads` 與 `payload != expected` 比對，內容錯誤照樣
    落在 `identity-mismatch`。
    """
    stripped = text.strip()
    opened = _CODE_FENCE_OPEN.match(stripped)
    if opened is None:
        return stripped
    body = stripped[opened.end() :]
    closed = _CODE_FENCE_CLOSE.search(body)
    if closed is not None:
        body = body[: closed.start()]
    return body.strip()


def stdout_excerpt(text: str, *, limit: int = STDOUT_EXCERPT_LIMIT) -> str:
    """把 probe 的原始 stdout 壓成單行節錄，供失敗 diagnostic 使用（issue #670）。

    修復前 `malformed-output` 的 diagnostic 是 `None`，現場零線索——#670 是靠
    人工重跑六遍才看見成因是 code fence。這裡把實際 stdout 前 `limit` 個字元
    帶進 diagnostic，讓下一次同類失敗一眼看得出是格式問題還是別的。

    節錄內容是**模型對一段固定 probe prompt 的回應**：prompt 由本模組寫死
    （只含 `capability`／`model` 兩個常數），argv 不帶任何憑證，env 也不會被
    回顯，因此沒有把 token／憑證帶進 log 或 evidence 的路徑。換行與連續空白
    一律壓成單一空格，避免多行內容污染單行 log。
    """
    collapsed = " ".join(text.split())
    if not collapsed:
        return "<empty>"
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


#: probe 失敗 diagnostic 的**全體**上限（#727）。沿用 repo 既有的 evidence 預算慣例
#: ——`manager.RETRY_CONTEXT_EVIDENCE_LIMIT` 就是 2000，兩者相等由
#: `tests/test_planner_probe_diagnostics_727.py` 釘住（本模組不 import `manager`：
#: `manager` import 本模組，反向會成環）。
#:
#: 為什麼是「有界」而不是「完整」：這份字串會一路進 log／evidence／`blocking_reason`，
#: 而 `CandidateRejection` 的欄位面契約是「沒有任何一個接得到 env、argv、檔案內容或
#: stderr」。#727 沒有放寬那條——只取 **stdout**（job 模式下它是 shim 合併後的那一份，
#: R-5 已記）與我們自己算出來的路徑，stderr 一個位元組都不取。
PLANNING_DIAGNOSTIC_LIMIT = 2000

#: `planning_runtime` 把「這次呼叫的 rc／stdout 節錄／`-o` 落點狀態」掛到即將往上拋的
#: 例外身上時用的屬性名。
#:
#: **為什麼是掛屬性而不是換一個例外型別**：`classify_probe_failure()` 的分級輸入是
#: `type(exc).__name__`（`ValueError` ⇒ 輸出不合約、`TimeoutExpired` ⇒ 環境）。包一層
#: 自訂子類會讓型別名變成一個不在兩張表上的新名字 ⇒ 全部落 `unclassified` ⇒ 分級從
#: `content`／`environment` 掉成 fail-closed 的未分類。診斷面的改善不得改動分級面，
#: 因此原例外**原封不動**往上拋，只多帶一格唯讀的上下文。
PROBE_DIAGNOSTIC_ATTR = "cortex_planning_diagnostic"


def attach_probe_diagnostic(exc: BaseException, detail: str) -> BaseException:
    """把有界上下文掛到 `exc` 上並原樣回傳（見 :data:`PROBE_DIAGNOSTIC_ATTR`）。"""

    try:
        setattr(exc, PROBE_DIAGNOSTIC_ATTR, detail)
    except (AttributeError, TypeError):  # pragma: no cover - 內建例外都掛得上
        pass
    return exc


def probe_exception_diagnostic(exc: BaseException) -> str:
    """一次 probe 失敗的例外 → **有界**診斷字串（#727）。

    形狀恆為 ``<ExcType>[ <bounded context>]``，型別名錨在第 0 個 token
    （:func:`classify_probe_failure` 只看那一格）。上下文有兩個來源，皆為既有物：

    - `PlanningJobError.detail`（job 模式）——`rc=…`／`unit=…`／`profile=…`／
      `binary=…`／`version=…`／`seccomp_filter_fatal=…`／`log=<節錄>`。#727 之前
      `probe_agy_capability` 對它只取 `type(exc).__name__`，於是實機 agy 那一格
      逐字只剩 `PlanningJobError`——**族名對了、病因全丟**。
    - :data:`PROBE_DIAGNOSTIC_ATTR`（`planning_runtime._invoke_json` 掛的）——
      `rc=…`／`stdout=<節錄>`／`last_message=<路徑>|<狀態>`。最後那一格正是 #727
      的關鍵：「落檔寫不進去／讀不回來」與「模型沒輸出」在症狀上本來完全無法區分。
    """

    name = type(exc).__name__
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, str) or not detail.strip():
        detail = getattr(exc, PROBE_DIAGNOSTIC_ATTR, None)
    if not isinstance(detail, str) or not detail.strip():
        return name
    return stdout_excerpt(f"{name} {detail}", limit=PLANNING_DIAGNOSTIC_LIMIT)


def _normalize_model_token(value: str) -> str:
    """正規化 model id／顯示名，容忍 `agy models` 輸出格式（顯示名 vs kebab id、
    大小寫、空白/括號等標點差異）未來再次改版。"""
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _agy_model_line_tokens(line: str) -> tuple[str, tuple[str, ...]]:
    """把 `agy models` 的一行拆成 ``(要傳給 --model 的字面 token, 可比對候選)``。

    issue #670 實測附帶發現：2026-08-18 實機的 `agy models` 已改成**兩欄
    tab 分隔**——``gemini-3.1-pro-high\\tGemini 3.1 Pro (High)``。整行拿去比對
    時字面與正規化雙雙落空（整行正規化後是
    `gemini-3-1-pro-high-gemini-3-1-pro-high`），於是 probe **100%** 死在
    `model-not-listed`——比 #670 的 fence 偽失敗更早、更絕對。

    多欄時 `--model` 只吃得下第一欄的 kebab id，顯示名（`Gemini 3.1 Pro (High)`）
    不是合法 CLI 值，因此**比對**可以用整行或任一欄，**回傳**一律是第一欄。
    單欄（舊格式）時 token 就是整行，行為與修復前逐字相同。
    """
    fields = [field.strip() for field in line.split("\t") if field.strip()]
    if len(fields) <= 1:
        return line, (line,)
    return fields[0], (line, *fields)


def _resolve_agy_cli_token(expected: str, listed: Iterable[str]) -> str | None:
    """在 `agy models` 的輸出行中找出與 expected 語意相符的實際 CLI token。

    優先字面完全比對；找不到時退而用正規化比對，讓顯示名／kebab id 之間的
    命名落差不必等到常數再次寫死才修（issue #255）。回傳的是 `agy models`
    實際印出的字面值（單欄輸出是整行、多欄輸出是 id 欄），因為 `--model`
    必須用 CLI 認得的字面值呼叫。
    """
    entries = [_agy_model_line_tokens(line) for line in listed if line]
    for token, candidates in entries:
        if expected in candidates:
            return token
    target = _normalize_model_token(expected)
    for token, candidates in entries:
        if any(_normalize_model_token(candidate) == target for candidate in candidates):
            return token
    return None


def probe_agy_capability(
    *,
    runner: ProcessRunner | None = None,
    timeout_seconds: int = 45,
) -> CapabilityProbe:
    """Probe both model identity discovery and the exact safe planning mode."""
    process_runner = runner or subprocess.run
    common = {
        "shell": False,
        "capture_output": True,
        "text": True,
        "timeout": timeout_seconds,
    }
    try:
        listed_raw = process_runner(["agy", "models"], **common)
        listed_rc, listed_stdout, listed_stderr = _process_fields(listed_raw)
    except Exception as exc:
        # #727：`type(exc).__name__` 對 job 模式等於把 `PlanningJobError.detail`
        # （rc／unit／profile／binary／version／seccomp／log 節錄）整段丟掉——實機
        # 逐字只剩 `PlanningJobError` 六個字。改走共用的有界投影。
        return _failed_agy("models-probe-failed", probe_exception_diagnostic(exc))
    if listed_rc != 0:
        return _failed_agy(
            "models-probe-failed", _exit_diagnostic(listed_rc, listed_stdout, listed_stderr)
        )
    listed_lines = {line.strip() for line in listed_stdout.splitlines() if line.strip()}
    cli_model_token = _resolve_agy_cli_token(AGY_MODEL_ID, listed_lines)
    if cli_model_token is None:
        available = ", ".join(sorted(listed_lines)[:20])
        return _failed_agy(
            "model-not-listed",
            f"expected={AGY_MODEL_ID!r} available=[{available}]",
        )

    expected = {"capability": "cortex-plan-sandbox", "model": AGY_MODEL_ID}
    # issue #670：軟性措辭（「Return only ...」）不足以讓模型穩定不加 fence，
    # 補上與 `planning_runtime._JSON_OUTPUT_CONTRACT` 同款的顯式輸出契約，把
    # fence 機率壓在源頭；`strip_code_fence` 則是模型仍不從時的保底。
    # 契約放在 payload **之前**，比照 `planning_runtime` 把 `_JSON_OUTPUT_CONTRACT`
    # 排在 `Input:` 之前的既有寫法，讓「指示 → 目標物」的順序一致，
    # 也讓 prompt 尾端仍然剛好是那個 JSON 物件。
    prompt = (
        "Output contract: reply with exactly one JSON object and nothing else — "
        "no prose, no explanation, no code fences. Your reply MUST start with '{'. "
        "Return only this compact JSON object and perform no tool calls: "
        + json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    )
    argv = build_agy_argv(
        prompt=prompt,
        slice_id="cortex-capability-probe",
        log_dir=".",
        model=cli_model_token,
    )
    try:
        smoke_raw = process_runner(argv, **common)
        smoke_rc, smoke_stdout, smoke_stderr = _process_fields(smoke_raw)
    except Exception as exc:
        return _failed_agy("smoke-failed", probe_exception_diagnostic(exc))
    if smoke_rc != 0:
        return _failed_agy(
            "smoke-failed", _exit_diagnostic(smoke_rc, smoke_stdout, smoke_stderr)
        )
    try:
        payload = json.loads(strip_code_fence(smoke_stdout))
    except (json.JSONDecodeError, TypeError):
        return _failed_agy("malformed-output", stdout_excerpt(smoke_stdout))
    if payload != expected:
        return _failed_agy("identity-mismatch", stdout_excerpt(smoke_stdout))
    return CapabilityProbe.ready_for("agy", AGY_MODEL_ID, AGY_DOMAIN)


# ---------------------------------------------------------------------------
# issue #682（#672 票 A）：逐候選拒因表
#
# `select_secondary_planner()` 的迴圈裡有四個 `continue`，全部靜默——每個候選
# 被跳過的真正理由（同 domain？probe 缺席？probe 沒 ready？ready 但身分不
# 符？）都在原地被吃掉，最後只剩一個沒有任何附加資訊的 `no-heterogeneous-
# planner`。拒因表把那四個 `continue` 各記一筆，讓「格式問題」與「拓撲問題」
# 在同一個字串裡是**兩個不同的欄位**，不必重跑六遍才發現（design D8）。
# ---------------------------------------------------------------------------

#: 候選與 primary 同 independence domain（純拓撲事實）。
REJECTION_SAME_DOMAIN = "same-domain"
#: 這個候選根本沒有被 probe 過（probes 表沒有這一格）。
REJECTION_PROBE_ABSENT = "probe-absent"
#: probe 跑了但沒 ready——真正的失敗原因在 `family`／`diagnostic` 兩欄。
REJECTION_PROBE_NOT_READY = "probe-not-ready"
#: probe ready，但它回報的身分與 roster 這一列不符（probe 問到了別人）。
REJECTION_PROBE_IDENTITY_MISMATCH = "probe-identity-mismatch"

REJECTION_REASONS = (
    REJECTION_SAME_DOMAIN,
    REJECTION_PROBE_ABSENT,
    REJECTION_PROBE_NOT_READY,
    REJECTION_PROBE_IDENTITY_MISMATCH,
)


@dataclass(frozen=True)
class CandidateRejection:
    """一個 planning-capable identity 為什麼沒被選上。

    欄位刻意只有六個，而且**沒有任何一個接得到 env、argv、檔案內容或
    stderr**——這份資料會一路進 log／evidence／`blocking_reason`，能不能夾帶
    憑證是欄位面就要回答的問題，不是渲染時再過濾。

    - `executor`／`model_id`／`domain`：roster 常數（`model-identities.yaml`）。
    - `reason`：四個 `continue` 之一，見 `REJECTION_REASONS`。
    - `diagnostic`：probe 側的自由文字，**全體有界**
      （:data:`PLANNING_DIAGNOSTIC_LIMIT`，＝`manager.RETRY_CONTEXT_EVIDENCE_LIMIT`）。
      最寬的來源是模型對一段**固定 probe prompt** 的 stdout 節錄（PR #674 的
      `stdout_excerpt`）、例外型別名，以及 #727 起接在型別名後面的那段上下文
      （rc、stdout 節錄、`-o` 落點的路徑與存在狀態）。**stderr 仍然一個位元組都不取**
      ——那是最容易夾帶路徑、env 與憑證原文的通道，票 A 畫的那條邊界 #727 沒有動。
    - `family`：三分族（`PLANNING_FAILURE_*`）。`same-domain`／`probe-absent`
      這類拓撲拒因為空字串——它們不是「執行失敗」，不該被硬塞進三族之一。
    """

    executor: str
    model_id: str
    domain: str
    reason: str
    diagnostic: str = ""
    family: str = ""

    @property
    def environment_grade(self) -> bool:
        return self.family in ENVIRONMENT_GRADE_PLANNING_FAMILIES

    @classmethod
    def from_probe(
        cls, identity: "ModelIdentity", probe: CapabilityProbe
    ) -> "CandidateRejection":
        """probe 沒 ready ⇒ 帶著 probe 自己的 reason／diagnostic 一起落表。

        **這裡就是 PR #674 與本票的接縫**：#674 讓 `probe_agy_capability`
        失敗時帶 stdout 節錄，本方法讓那份節錄不在 `select_secondary_planner`
        被 `continue` 吃掉，而是原樣（僅做單行化）活到 blocking reason。
        """

        probe_reason = (probe.reason or "").strip()
        probe_diagnostic = (probe.diagnostic or "").strip()
        detail = " ".join(part for part in (probe_reason, probe_diagnostic) if part)
        return cls(
            executor=identity.executor,
            model_id=identity.model_id,
            domain=identity.independence_domain,
            reason=REJECTION_PROBE_NOT_READY,
            diagnostic=detail,
            family=classify_probe_failure(probe_reason, probe_diagnostic),
        )

    @classmethod
    def topological(
        cls, identity: "ModelIdentity", reason: str, diagnostic: str = ""
    ) -> "CandidateRejection":
        return cls(
            executor=identity.executor,
            model_id=identity.model_id,
            domain=identity.independence_domain,
            reason=reason,
            diagnostic=diagnostic,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "domain": self.domain,
            "reason": self.reason,
            "diagnostic": self.diagnostic,
            "family": self.family,
        }

    def head(self) -> str:
        """身分 ＋ 拒因 ＋ 族名。**這一段永不被截斷**（見 `render_...`）。"""

        head = f"{self.executor}/{self.model_id}[{self.domain}]: {self.reason}"
        return f"{head} {self.family}" if self.family else head


#: 單一候選 diagnostic 的字元上限。超出部分以 `…+Nc` 就地記帳。
#: 160 與 `summarize_exception`／`#397` 既有的例外摘要上限同數量級，
#: 足以完整看見一個 fence 開頭與 JSON 前綴。
REJECTION_TABLE_DETAIL_LIMIT = 160
#: 整張表（含前綴）的字元預算。roster 五個 identity、每格 diagnostic 上限
#: 160 時綽綽有餘；超出時**只犧牲 diagnostic、不犧牲身分列**。
REJECTION_TABLE_TOTAL_LIMIT = 1200

#: C0／C1 控制字元（換行、tab、ANSI escape 的 ESC）。diagnostic 帶的是模型
#: 輸出，直接進單行 log 會被它污染。
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

#: 環境級拒因表的辨識式。**錨在字串開頭**，因此模型 stdout 節錄裡就算逐字
#: 寫著 `planning-executor-failed`，也騙不到 `_classify_planning_failure`：
#: grade 是渲染端依 `CandidateRejection.family` 算出來的一個欄位，不是對整串
#: reason 做 substring-search 的結果。
_ENVIRONMENT_GRADE_REASON_RE = re.compile(
    r"\A[a-z0-9-]+ grade=environment candidates=\d+ \("
)


def _single_line_detail(text: str) -> str:
    return " ".join(_CONTROL_CHARS_RE.sub(" ", text).split())


def _truncate_detail(detail: str, limit: int) -> str:
    if len(detail) <= limit:
        return detail
    return f"{detail[:limit]}…+{len(detail) - limit}c"


def rejection_table_grade(rejections: Sequence[CandidateRejection]) -> str:
    """拒因表 → `environment` / `content`。任一條 environment 級即整體 environment。"""

    return (
        "environment"
        if any(rejection.environment_grade for rejection in rejections)
        else "content"
    )


def render_secondary_rejection_reason(
    base_reason: str | None,
    rejections: Sequence[CandidateRejection],
    *,
    detail_limit: int = REJECTION_TABLE_DETAIL_LIMIT,
    total_limit: int = REJECTION_TABLE_TOTAL_LIMIT,
) -> str | None:
    """把拒因表渲染進 blocking reason（design D8 的格式）。

    ``<base> grade=<environment|content> candidates=<N> (<逐條>; <逐條>)``

    三段都是必要的：``grade`` 讓下游分類有一個**不必去 substring-search 模型
    輸出**的欄位；``candidates=<N>`` 讓「表裡應該有幾條」變成一個可核對的
    數字。沒有任何候選時原樣回傳 ``base_reason``——roster 裡真的沒有別人時，
    `no-heterogeneous-planner` 是真話，不必生一張空表。

    **截斷策略**（issue #682 明列的要求：截斷不得讓「哪一條被截掉」變成不可
    知）：

    1. 每一條的**身分 ＋ 拒因 ＋ 族名**（`head()`）永不被截斷、永不被整列丟
       棄。表再長也答得出「有幾個候選、分別是誰、各自為什麼落選」。
    2. 只有 `diagnostic` 會被截。單條超過 `detail_limit` 時就地記帳
       `…+Nc`（少了幾個字寫在原處）。
    3. 全表仍超過 `total_limit` 時，從**最長的 diagnostic 開始**整格換成
       `<detail-elided:Nc>`，同樣就地記帳。因此「被犧牲的是哪一條、犧牲了
       多少」永遠讀得出來。
    4. 身分列本身就撐爆預算時（roster 大到病態）寧可超出預算也不丟列——
       丟列會讓拒因表失去它存在的唯一理由。
    """

    if not rejections:
        return base_reason
    prefix = (
        f"{base_reason} grade={rejection_table_grade(rejections)} "
        f"candidates={len(rejections)} "
    )
    heads = [rejection.head() for rejection in rejections]
    details = [
        _truncate_detail(_single_line_detail(rejection.diagnostic), detail_limit)
        for rejection in rejections
    ]

    def assemble() -> str:
        entries = [
            f"{head} {detail}" if detail else head for head, detail in zip(heads, details)
        ]
        return f"{prefix}({'; '.join(entries)})"

    rendered = assemble()
    # 由長到短逐格讓位；同長度時取索引小的，讓結果與輸入順序一樣是決定性的。
    order = sorted(range(len(details)), key=lambda index: (-len(details[index]), index))
    for index in order:
        if len(rendered) <= total_limit:
            break
        if not details[index]:
            continue
        details[index] = f"<detail-elided:{len(details[index])}c>"
        rendered = assemble()
    return rendered


def is_environment_grade_rejection_reason(reason: str | None) -> bool:
    """reason 是否為帶 environment 級拒因的拒因表（`manager` 的分類例外用）。"""

    return bool(reason) and _ENVIRONMENT_GRADE_REASON_RE.match(reason) is not None


@dataclass(frozen=True)
class SecondarySelection:
    state: str
    reason: str | None
    identity: ModelIdentity | None
    #: issue #682：逐候選拒因表。`reason` 本身刻意維持原字面值
    #: （`no-heterogeneous-planner` 是下游既有的機器判準），拒因走這個新欄位，
    #: 由 `run_heterogeneous_brainstorm` 渲染進 `BrainstormResult.reason`。
    rejections: tuple[CandidateRejection, ...] = ()


def select_secondary_planner(
    *,
    registry: IdentityRegistry,
    primary: tuple[str, str],
    probes: Mapping[tuple[str, str], CapabilityProbe],
) -> SecondarySelection:
    """挑異質 domain 的次要 planner。

    #534：候選順序改走三層解析鏈（operator overlay → 評估合格清單 → packaged
    fallback），不再是 ``PLANNER_PRIORITY`` 的寫死 executor 順序。舊實作把
    ``agy`` 釘在第一位，且只認 ``PLANNER_PRIORITY`` 列出的三組
    ``(executor, domain)``——operator 在 overlay 宣告的 planner（例如 `cg` 或
    任何新 executor）**永遠不可達**，packaged 的 agy 卻穩坐熱路徑首位，正是
    #534 的主訴現場。合法性條件（planning capability、異質 domain、probe
    ready 且 probe 身分相符）逐項不變。

    #682（#672 票 A）：迴圈裡四個 `continue` 過去全部靜默，於是失敗時只剩一個
    沒有任何附加資訊的 `no-heterogeneous-planner`。現在每次 `continue` 都記一筆
    `CandidateRejection`，經 `SecondarySelection.rejections` 交給
    `run_heterogeneous_brainstorm` 渲染進 blocking reason。

    `reason` 欄位本身**刻意不變**（仍是 `no-heterogeneous-planner`）：它是下游
    既有的機器判準與既有測試的斷言對象，拒因表走新欄位而不是把字串改長。
    """

    primary_identity = registry.get(*primary)
    if primary_identity is None:
        return SecondarySelection("needs_human", "primary-identity-unknown", None)
    planning = [
        identity for identity in registry.identities if "planning" in identity.capabilities
    ]
    ranked = model_resolution.rank_candidates(
        planning,
        role="planning",
        context=registry.resolution_context,
        compatibility_for=(
            (
                lambda identity: model_resolution.compatibility_contract_for(
                    "planner", identity
                )
            )
            if registry.resolution is not None
            else None
        ),
    )
    rejections: list[CandidateRejection] = []
    for identity in ranked.ordered:
        if identity.independence_domain == primary_identity.independence_domain:
            # primary 自己也在 planning 名單裡，而它與自己當然同 domain。
            # 記一條「primary 因為與 primary 同 domain 而落選」是**零資訊的
            # 套套邏輯**，只會讓每張表都以一條廢話開頭，所以不記——它不是候選，
            # 它是被拿來比對的那一方。同 domain 的**其他**身分照記。
            if (identity.executor, identity.model_id) != (
                primary_identity.executor,
                primary_identity.model_id,
            ):
                rejections.append(
                    CandidateRejection.topological(identity, REJECTION_SAME_DOMAIN)
                )
            continue
        probe = probes.get((identity.executor, identity.model_id))
        if probe is None:
            rejections.append(
                CandidateRejection.topological(identity, REJECTION_PROBE_ABSENT)
            )
            continue
        if not probe.ready:
            rejections.append(CandidateRejection.from_probe(identity, probe))
            continue
        if probe.identity != (
            identity.executor,
            identity.model_id,
            identity.independence_domain,
        ):
            rejections.append(
                CandidateRejection.topological(
                    identity,
                    REJECTION_PROBE_IDENTITY_MISMATCH,
                    # probe 回報的身分是 roster／probe 兩側的常數，不含自由文字。
                    diagnostic="probe-reported=" + "/".join(probe.identity),
                )
            )
            continue
        return SecondarySelection("ready", None, identity)
    return SecondarySelection(
        "needs_human", "no-heterogeneous-planner", None, tuple(rejections)
    )


# ---------------------------------------------------------------------------
# #452 B/C：查表投影與 capable() 判準 1–5 消費點。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvelopeProjection:
    """一次 (identity, persona) 查表的完整封套投影。

    「一律有值」在本層成立（#453 R4）：任何查表必回一份完整四欄封套——實測欄位
    取 registry 行內值，其餘套 DEFAULT_ENVELOPE[persona] 並逐欄標 source=default。
    """

    persona: str
    envelope: Mapping[str, object]
    source: Mapping[str, str]
    provenance: Mapping[str, object] | None

    @property
    def all_default(self) -> bool:
        return all(value == ENVELOPE_SOURCE_DEFAULT for value in self.source.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "persona": self.persona,
            "envelope": {
                key: (list(value) if isinstance(value, tuple) else value)
                for key, value in self.envelope.items()
            },
            "source": dict(self.source),
            "provenance": (
                json.loads(json.dumps(self.provenance, ensure_ascii=False, sort_keys=True))
                if self.provenance is not None
                else None
            ),
        }


def project_envelope(identity: ModelIdentity | None, persona: str) -> EnvelopeProjection:
    """(identity, persona) → 完整封套投影（#453 R4 查表投影套用點）。

    identity 無實測封套、或實測 persona 與查詢 persona 不同（指紋含 persona，
    pilot-v1 只量 builder）時，整份回 DEFAULT_ENVELOPE[persona]。
    """

    if persona not in DEFAULT_ENVELOPE:
        raise ValueError(f"envelope persona invalid: {persona!r}")
    defaults = DEFAULT_ENVELOPE[persona]
    envelope: dict[str, object] = {name: defaults[name] for name in ENVELOPE_FIELDS}
    source: dict[str, str] = {name: ENVELOPE_SOURCE_DEFAULT for name in ENVELOPE_FIELDS}
    provenance: Mapping[str, object] | None = None
    # getattr 防禦：呼叫端（manager 測試替身）可能傳入非 ModelIdentity 的
    # 輕量 identity 物件；缺封套屬性一律視為無實測、走預設投影。
    identity_provenance = getattr(identity, "profile_provenance", None)
    if identity is not None and isinstance(identity_provenance, Mapping):
        fingerprint = identity_provenance.get("fingerprint")
        measured_persona = (
            str(fingerprint.get("persona")).strip()
            if isinstance(fingerprint, Mapping)
            else None
        )
        if measured_persona == persona:
            for name in ENVELOPE_FIELDS:
                value = getattr(identity, name, None)
                if value is not None:
                    envelope[name] = value
                    source[name] = ENVELOPE_SOURCE_MEASURED
            provenance = identity_provenance
    return EnvelopeProjection(
        persona=persona, envelope=envelope, source=source, provenance=provenance
    )


def plan_review_envelope_projection(
    identity: ModelIdentity | None, *, persona: str = "builder"
) -> dict[str, object] | None:
    """`planning._plan_review_envelope` 的 envelope_lookup 投影（#209 R7 兩鍵）。

    #453 R5／#454 R5：投影所需兩鍵（invariant_ceiling／consistency_scope）任一
    來源為 default 時 MUST 回 ``None``——seam 維持 ``envelope_unavailable``
    bypass 字節。v1 映射（#454 R1）恆不量測這兩欄，故本投影現況恆回 None；
    接線的意義在於實測值一旦落地即自動生效，不需再動 seam。
    """

    projection = project_envelope(identity, persona)
    if (
        projection.source["invariant_ceiling"] != ENVELOPE_SOURCE_MEASURED
        or projection.source["consistency_scope"] != ENVELOPE_SOURCE_MEASURED
    ):
        return None
    return {
        "invariant_count": projection.envelope["invariant_ceiling"],
        "artifact_classes": [str(item) for item in projection.envelope["consistency_scope"]],
    }


@dataclass(frozen=True)
class CapabilityCriterion:
    """capable() 單項判準的可觀測結果（#209 R1：六項全評估、不短路）。"""

    name: str
    state: str  # "pass" | "fail" | "bypass"
    detail: str

    def __post_init__(self) -> None:
        if self.state not in {"pass", "fail", "bypass"}:
            raise ValueError(f"capability criterion state invalid: {self.state!r}")


@dataclass(frozen=True)
class CapabilityEvaluation:
    executor: str
    model_id: str
    persona: str
    verdict: bool | None
    criteria: tuple[CapabilityCriterion, ...]

    def failed_criteria(self) -> tuple[CapabilityCriterion, ...]:
        return tuple(item for item in self.criteria if item.state == "fail")

    def to_dict(self) -> dict[str, object]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "persona": self.persona,
            "verdict": self.verdict,
            "criteria": [
                {"name": item.name, "state": item.state, "detail": item.detail}
                for item in self.criteria
            ],
        }


def evaluate_capability(
    identity: ModelIdentity,
    *,
    persona: str,
    sizing_band: str | None = None,
    invariant_count: int | None = None,
    artifact_classes: Iterable[str] | None = None,
    acceptance_mode: str | None = None,
    required_capabilities: Iterable[str] = (),
) -> CapabilityEvaluation:
    """capable() 判準 1–5 的消費點（#209 R1；判準 6 track_record 未落地=#137）。

    六項全評估、不因單項為否而跳過其餘（可觀測性要求）；逐項規則：

    - 判準欄位 source=default → 該項 bypass（#453 R2/R3、#454 R5：預設期零過濾）；
    - 工作側輸入缺席（band／invariant_count／artifact_classes／acceptance_mode
      不明）→ 該項 bypass；
    - 其餘才以實測值真答 pass/fail。

    verdict：封套全 default → ``None``（#453 R5：對應 capability_probe 的
    ``envelope_unavailable`` bypass，證據字節與 v0.1.6 逐位元相同）；否則任一
    項 fail → ``False``；至少一項 pass 且無 fail → ``True``；全 bypass →
    ``None``。
    """

    projection = project_envelope(identity, persona)
    criteria: list[CapabilityCriterion] = []

    def _measured(field_name: str) -> bool:
        return projection.source[field_name] == ENVELOPE_SOURCE_MEASURED

    # 1 sizing band ∈ accepts_bands
    if not _measured("accepts_bands"):
        criteria.append(CapabilityCriterion("sizing_band", "bypass", "accepts-bands-default"))
    elif sizing_band is None:
        criteria.append(CapabilityCriterion("sizing_band", "bypass", "work-band-unknown"))
    else:
        bands = tuple(projection.envelope["accepts_bands"])
        state = "pass" if sizing_band in bands else "fail"
        criteria.append(
            CapabilityCriterion(
                "sizing_band", state, f"band={sizing_band} accepts_bands={list(bands)}"
            )
        )
    # 2 invariant_count ≤ invariant_ceiling（None sentinel MUST NOT 讀成 0，#453 R2）
    if not _measured("invariant_ceiling"):
        criteria.append(
            CapabilityCriterion("invariant_ceiling", "bypass", "invariant-ceiling-default")
        )
    elif invariant_count is None:
        criteria.append(
            CapabilityCriterion("invariant_ceiling", "bypass", "work-invariant-count-unknown")
        )
    else:
        ceiling = int(projection.envelope["invariant_ceiling"])  # type: ignore[arg-type]
        state = "pass" if invariant_count <= ceiling else "fail"
        criteria.append(
            CapabilityCriterion(
                "invariant_ceiling", state, f"invariant_count={invariant_count} ceiling={ceiling}"
            )
        )
    # 3 artifact_classes ⊆ consistency_scope（#453 R3 注意：default 對域外值一律 bypass）
    if not _measured("consistency_scope"):
        criteria.append(
            CapabilityCriterion("consistency_scope", "bypass", "consistency-scope-default")
        )
    elif artifact_classes is None:
        criteria.append(
            CapabilityCriterion("consistency_scope", "bypass", "work-artifact-classes-unknown")
        )
    else:
        scope = frozenset(projection.envelope["consistency_scope"])  # type: ignore[arg-type]
        requested = frozenset(str(item) for item in artifact_classes)
        over = sorted(requested - scope)
        state = "pass" if not over else "fail"
        criteria.append(
            CapabilityCriterion(
                "consistency_scope",
                state,
                f"over_scope={over}" if over else f"artifact_classes={sorted(requested)}",
            )
        )
    # 4 acceptance_mode ∈ acceptance_modes
    if not _measured("acceptance_modes"):
        criteria.append(
            CapabilityCriterion("acceptance_modes", "bypass", "acceptance-modes-default")
        )
    elif acceptance_mode is None:
        criteria.append(
            CapabilityCriterion("acceptance_modes", "bypass", "work-acceptance-mode-unknown")
        )
    else:
        modes = tuple(projection.envelope["acceptance_modes"])
        state = "pass" if acceptance_mode in modes else "fail"
        criteria.append(
            CapabilityCriterion(
                "acceptance_modes", state, f"mode={acceptance_mode} acceptance_modes={list(modes)}"
            )
        )
    # 5 required_capabilities ⊆ capabilities（registry 事實；空需求記 bypass，
    # 避免 vacuous pass 把「什麼都沒評」偽裝成真答）
    required = tuple(sorted({str(item) for item in required_capabilities}))
    if not required:
        criteria.append(
            CapabilityCriterion("capabilities", "bypass", "no-required-capabilities-declared")
        )
    else:
        missing = sorted(set(required) - set(identity.capabilities))
        state = "pass" if not missing else "fail"
        criteria.append(
            CapabilityCriterion(
                "capabilities",
                state,
                f"missing={missing}" if missing else f"required={list(required)}",
            )
        )
    # 6 track_record（#137 未落地；#209 R1 明文本票不假設其內部實作）
    criteria.append(CapabilityCriterion("track_record", "bypass", "track-record-not-landed-#137"))

    if projection.all_default:
        # #453 R5：封套全部來自預設的身分，provider MUST 回 None（不是 True），
        # 使 capability 格維持 envelope_unavailable bypass 的既有證據字節。
        verdict: bool | None = None
    elif any(item.state == "fail" for item in criteria):
        verdict = False
    elif any(item.state == "pass" for item in criteria):
        verdict = True
    else:
        verdict = None
    return CapabilityEvaluation(
        executor=identity.executor,
        model_id=identity.model_id,
        persona=persona,
        verdict=verdict,
        criteria=tuple(criteria),
    )


def build_capability_lookup(
    registry: IdentityRegistry,
    *,
    persona: str,
    sizing_band: str | None = None,
    invariant_count: int | None = None,
    artifact_classes: Iterable[str] | None = None,
    acceptance_mode: str | None = None,
    required_capabilities: Iterable[str] = (),
    observations: list[CapabilityEvaluation] | None = None,
) -> Callable[[str], bool | None]:
    """組出 `claim_readiness.capability_probe` 的 capability_lookup provider。

    介面形狀沿用 #209 既定 seam（``Callable[[str], bool | None]``，輸入
    ``context.executor_identity``，格式 ``executor/model_id``）。查無身分、或
    身分封套全 default → 回 ``None``（bypass 字節不變）；實測身分以
    :func:`evaluate_capability` 真答，逐項判準結果可經 ``observations``
    收集供事後稽核（被排除原因可觀測）。
    """

    def _lookup(executor_identity: str) -> bool | None:
        # 既有測試語料同時存在 "executor/model_id" 與 "executor:model_id" 兩種
        # 拼法；兩者都認，解析不出來一律回 None（bypass，字節不變）。
        text = str(executor_identity)
        executor, separator, model_id = text.partition("/")
        if not separator:
            executor, separator, model_id = text.partition(":")
        if not separator:
            return None
        identity = registry.get(executor.strip(), model_id.strip())
        if identity is None:
            return None
        evaluation = evaluate_capability(
            identity,
            persona=persona,
            sizing_band=sizing_band,
            invariant_count=invariant_count,
            artifact_classes=artifact_classes,
            acceptance_mode=acceptance_mode,
            required_capabilities=required_capabilities,
        )
        if observations is not None:
            observations.append(evaluation)
        return evaluation.verdict

    return _lookup
