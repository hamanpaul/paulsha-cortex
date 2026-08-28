"""#534：模型引擎三層解析鏈（operator overlay → 評估合格清單 → packaged 候選池）。

使用者裁決（2026-08-14）：**人工指定清單優先 → agent 從 patchmud 評估合格清單挑
→ 未評估模型須先經 patchmud eval、合格後人工複核加入清單**。本模組是那條裁決的
唯一真值：

1. ``operator-overlay``——host overlay ``model-identities.yaml`` 宣告的身分。
   operator 的列序即優先序，壓過 packaged roster 的一切內建順序。
2. ``evaluated-roster``——``model-eval-roster.yaml`` 中 ``verdict: pass`` 且
   ``review_status: approved`` 的身分（該角色）。patchmud eval 管線是這層的資料
   供給端（管線本身屬 v4 R2，不在 #534；本層先把契約與消費端做出來，清單可手工
   維護）。
3. ``packaged-fallback``——packaged roster 只剩「候選池」：供評估管線取材，解析時
   為最後一層，且受 ``resolution_policy.packaged_fallback`` 管制（allow／warn／
   deny），選到時一律留下 provenance，不得靜默使用。

本模組刻意不 import :mod:`model_identities`（避免循環 import）；identity 一律
duck-typed 讀取，缺欄位時退回最寬鬆的預設，讓既有測試替身與手工建構的 registry
維持原行為。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .._yaml import YAMLError, safe_load

# ---------------------------------------------------------------------------
# 解析層與身分來源常數
# ---------------------------------------------------------------------------

#: 第 1 層：operator 於 host overlay 人工指定。
RESOLUTION_LAYER_OVERLAY = "operator-overlay"
#: 第 2 層：patchmud 評估合格＋人工複核通過的清單。
RESOLUTION_LAYER_EVALUATED = "evaluated-roster"
#: 第 3 層：packaged 候選池（未評估／未複核），僅為 fallback。
RESOLUTION_LAYER_PACKAGED = "packaged-fallback"
#: 宣告順序即優先序。
RESOLUTION_LAYERS = (
    RESOLUTION_LAYER_OVERLAY,
    RESOLUTION_LAYER_EVALUATED,
    RESOLUTION_LAYER_PACKAGED,
)
_LAYER_RANK = {name: index for index, name in enumerate(RESOLUTION_LAYERS)}

#: identity 來源（載入時由 loader 蓋章，非 YAML 欄位）。
IDENTITY_ORIGIN_OVERLAY = "operator-overlay"
IDENTITY_ORIGIN_PACKAGED = "packaged"
IDENTITY_ORIGINS = frozenset({IDENTITY_ORIGIN_OVERLAY, IDENTITY_ORIGIN_PACKAGED})

#: overlay 對 packaged 身分的處置（#509 殘項：demote／park）。
PACKAGED_ACTION_PARK = "park"
PACKAGED_ACTION_DEMOTE = "demote"
PACKAGED_ACTIONS = frozenset({PACKAGED_ACTION_PARK, PACKAGED_ACTION_DEMOTE})

#: packaged fallback 政策。
PACKAGED_FALLBACK_ALLOW = "allow"
PACKAGED_FALLBACK_WARN = "warn"
PACKAGED_FALLBACK_DENY = "deny"
PACKAGED_FALLBACK_POLICIES = (
    PACKAGED_FALLBACK_ALLOW,
    PACKAGED_FALLBACK_WARN,
    PACKAGED_FALLBACK_DENY,
)

#: persona → 評估角色／capability（與 manager 的
#: ``_MODEL_CHAIN_CAPABILITY_BY_PERSONA`` 同一組值，避免兩處漂移）。
ROLE_BY_PERSONA = {
    "planner": "planning",
    "builder": "build",
    "reviewer": "review",
}
EVAL_ROSTER_ROLES = ("planning", "build", "review")

EVAL_ROSTER_FILENAME = "model-eval-roster.yaml"
EVAL_ROSTER_SCHEMA_VERSION = 1
SUPPORTED_EVAL_ROSTER_SCHEMAS = frozenset({1})
EVAL_VERDICTS = ("pass", "fail", "pending")
REVIEW_STATUSES = ("approved", "rejected", "pending")

_EVAL_REQUIRED_KEYS = (
    "executor",
    "model_id",
    "roles",
    "verdict",
    "evaluated_at",
    "eval_source",
    "review_status",
)
_EVAL_OPTIONAL_KEYS = ("eval_ref", "reviewer", "reviewed_at", "notes")


def role_for_persona(persona: str) -> str:
    """persona → 評估角色；未知 persona 比照 manager 的 catch-all 視為 build。"""

    return ROLE_BY_PERSONA.get(persona, "build")


# Cross-layer compatibility is deliberately expressed in terms of the
# persona's Trust Root principal, not a fixed executor assignment.  The same
# builder contract therefore works for codex, AGY, or a future qualified
# executor without turning ``builder = codex`` into an invariant.
_CREDENTIAL_PRINCIPAL_BY_PERSONA = {
    "builder": "builder",
    "planner": "reviewer-planner",
    "reviewer": "reviewer-planner",
}
_TRUST_ROOT_PRINCIPAL_BY_PERSONA = {
    "builder": "BUILDER",
    "planner": "PLANNER",
    "reviewer": "REVIEWER",
}
_EXPECTED_CREDENTIAL_SHAPE_BY_EXECUTOR = {
    "codex": "home-sticky-tree",
    "agy": "home-redirect-tree",
    "claude": "home-redirect-tree",
}


def _launcher_builder_supports(executor: str, parameter: str) -> bool:
    """Return whether the registered launcher can express ``parameter``.

    Launcher construction remains owned by :mod:`coordinator.launcher` (and
    the AGY writable implementation is supplied by its dependency).  Reading
    the registered builder signature here gives the compatibility guard a
    mechanical, dependency-aware check without duplicating argv generation.
    """

    try:
        import inspect

        from .launcher import _ARGV_BUILDERS

        builder = _ARGV_BUILDERS.get(executor)
        return builder is not None and parameter in inspect.signature(builder).parameters
    except (ImportError, TypeError, ValueError):
        return False


def launcher_profile_for(persona: str, executor: str) -> Mapping[str, object] | None:
    """Derive the launcher half of the effective persona/executor contract.

    A ``None`` result is intentional: it means the installed launcher cannot
    express the required mode, so callers must fail closed before launch.
    In particular, an AGY builder is only compatible once the writable AGY
    launcher dependency has landed; this module does not reimplement it.
    """

    if not isinstance(executor, str) or not executor:
        return None
    if persona == "builder":
        if executor == "cg" or not _launcher_builder_supports(executor, "commit_required"):
            return None
        return {
            "executor": executor,
            "persona": persona,
            "mode": "accept-edits" if executor == "agy" else "workspace-write",
            "requires_worktree": True,
            "commit_required": True,
        }
    if persona == "planner":
        if not _launcher_builder_supports(executor, "read_only"):
            return None
        return {
            "executor": executor,
            "persona": persona,
            "mode": "plan",
            "requires_worktree": False,
            "read_only": True,
            "commit_required": False,
        }
    if persona == "reviewer":
        if not _launcher_builder_supports(executor, "review_only"):
            return None
        return {
            "executor": executor,
            "persona": persona,
            "mode": "review-only",
            "requires_worktree": True,
            "review_only": True,
            "commit_required": False,
        }
    return None


def _toolchain_grant_for(persona: str, executor: str) -> Mapping[str, object] | None:
    """Resolve a principal-specific executor toolchain grant from the inventory."""

    principal_name = _TRUST_ROOT_PRINCIPAL_BY_PERSONA.get(persona)
    if principal_name is None:
        return None
    try:
        from ..trust_root import permgen

        principal = getattr(permgen.Principal, principal_name)
        toolchain_kind = permgen.DependencyKind.TOOLCHAIN_PROGRAM
        if not any(tool.name == executor for tool in permgen.EXECUTOR_TOOLS):
            return None
        if not any(
            dependency.name == executor
            and dependency.kind is toolchain_kind
            and principal in dependency.principals
            for dependency in permgen.RUN_EXTERNAL_DEPENDENCIES
        ):
            return None
    except (AttributeError, ImportError):
        return None
    return {
        "principal": persona,
        "executor": executor,
        "asset_id": "executor-toolchain",
        "executable": True,
    }


def _credential_grant_for(persona: str, executor: str) -> Mapping[str, object] | None:
    """Resolve only the explicitly registered credential cell for this principal."""

    principal = _CREDENTIAL_PRINCIPAL_BY_PERSONA.get(persona)
    if principal is None:
        return None
    try:
        from ..trust_root import permgen

        credential = permgen.credential_for(principal, executor)
    except (AttributeError, ImportError, KeyError):
        return None
    return {
        "principal": principal,
        "executor": executor,
        "shape": credential.shape,
        "asset_id": permgen.credential_asset_id(principal, credential),
    }


def validate_persona_executor_compatibility(
    *,
    persona: str,
    identity: object,
    launcher_profile: Mapping[str, object] | None,
    toolchain_grant: Mapping[str, object] | None,
    credential_grant: Mapping[str, object] | None,
) -> None:
    """Fail closed unless one identity has a complete effective launch contract.

    The error names the first missing layer so doctor and dispatch can expose
    an actionable diagnosis.  This is intentionally a pure validator: callers
    choose how to obtain each fact (loaded Trust Root, a launcher instance, or
    a test fixture), while every entry point applies the same predicate.
    """

    executor = getattr(identity, "executor", None)
    model_id = getattr(identity, "model_id", "unknown")
    capability = role_for_persona(persona)
    capabilities = getattr(identity, "capabilities", ())
    if not isinstance(executor, str) or not executor:
        raise ValueError(f"missing {persona} launcher profile: identity executor is empty")
    if capability not in capabilities:
        raise ValueError(
            f"missing {persona} capability for {executor}/{model_id}: {capability}"
        )

    if not isinstance(launcher_profile, Mapping):
        raise ValueError(f"missing {persona} launcher profile for {executor}/{model_id}")
    if (
        launcher_profile.get("executor") != executor
        or launcher_profile.get("persona") != persona
    ):
        raise ValueError(
            f"missing {persona} launcher profile for {executor}/{model_id}: "
            "executor/persona mismatch"
        )
    mode = launcher_profile.get("mode")
    if persona == "builder":
        if mode not in {"accept-edits", "workspace-write"}:
            raise ValueError(
                f"missing builder launcher profile for {executor}/{model_id}: "
                f"writable mode={mode!r}"
            )
        if launcher_profile.get("requires_worktree") is not True:
            raise ValueError(
                f"missing builder launcher profile for {executor}/{model_id}: worktree"
            )
        if launcher_profile.get("commit_required") is not True:
            raise ValueError(
                f"missing builder launcher profile for {executor}/{model_id}: commit"
            )
    elif persona == "planner":
        if mode not in {"plan", "read-only", "read-only-plan"}:
            raise ValueError(
                f"missing planner launcher profile for {executor}/{model_id}: mode={mode!r}"
            )
        if launcher_profile.get("read_only") is False:
            raise ValueError(
                f"missing planner launcher profile for {executor}/{model_id}: read-only"
            )
    elif persona == "reviewer":
        if mode not in {"review", "review-only", "read-only", "verdict-only"}:
            raise ValueError(
                f"missing reviewer launcher profile for {executor}/{model_id}: mode={mode!r}"
            )
        if (
            launcher_profile.get("review_only") is False
            and launcher_profile.get("read_only") is False
        ):
            raise ValueError(
                f"missing reviewer launcher profile for {executor}/{model_id}: read-only"
            )

    principal = _CREDENTIAL_PRINCIPAL_BY_PERSONA.get(persona, persona)
    if not isinstance(toolchain_grant, Mapping):
        raise ValueError(f"missing {persona} toolchain grant for {executor}/{model_id}")
    if (
        toolchain_grant.get("principal") != persona
        or toolchain_grant.get("executor") != executor
        or toolchain_grant.get("asset_id") != "executor-toolchain"
        or toolchain_grant.get("executable") is not True
    ):
        raise ValueError(
            f"missing {persona} toolchain grant for {executor}/{model_id}: "
            "principal/executor/asset mismatch"
        )

    if not isinstance(credential_grant, Mapping):
        raise ValueError(f"missing {persona} credential grant for {executor}/{model_id}")
    if (
        credential_grant.get("principal") != principal
        or credential_grant.get("executor") != executor
    ):
        raise ValueError(
            f"missing {persona} credential grant for {executor}/{model_id}: "
            "principal/executor mismatch"
        )
    shape = credential_grant.get("shape")
    shape_value = getattr(shape, "value", shape)
    expected_shape = _EXPECTED_CREDENTIAL_SHAPE_BY_EXECUTOR.get(executor)
    if not isinstance(shape_value, str) or not shape_value:
        raise ValueError(f"missing {persona} credential grant for {executor}/{model_id}: shape")
    if expected_shape is not None and shape_value != expected_shape:
        raise ValueError(
            f"missing {persona} credential grant for {executor}/{model_id}: "
            f"shape={shape_value!r}"
        )


def compatibility_contract_for(
    persona: str, identity: object, *, launcher: object | None = None
) -> Mapping[str, Mapping[str, object] | None]:
    """Return the three facts consumed by the shared compatibility predicate."""

    executor = getattr(identity, "executor", "")
    profile: Mapping[str, object] | None
    if launcher is not None:
        profile_factory = getattr(launcher, "compatibility_profile", None)
        if callable(profile_factory):
            try:
                profile = profile_factory(persona=persona)
            except TypeError:
                profile = profile_factory(persona)
            if not isinstance(profile, Mapping):
                profile = None
        else:
            profile = launcher_profile_for(persona, executor)
    else:
        profile = launcher_profile_for(persona, executor)
    return {
        "launcher_profile": profile,
        "toolchain_grant": _toolchain_grant_for(persona, executor),
        "credential_grant": _credential_grant_for(persona, executor),
    }


def validate_identity_compatibility(
    persona: str, identity: object, *, launcher: object | None = None
) -> None:
    """Validate an identity using facts derived from the current deployment."""

    contract = compatibility_contract_for(persona, identity, launcher=launcher)
    validate_persona_executor_compatibility(
        persona=persona,
        identity=identity,
        launcher_profile=contract["launcher_profile"],
        toolchain_grant=contract["toolchain_grant"],
        credential_grant=contract["credential_grant"],
    )


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


# ---------------------------------------------------------------------------
# 第 2 層契約：model-eval-roster.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalRosterEntry:
    """評估合格清單的一列（扁平欄位，可手工維護）。

    合格判準（第 2 層可用）＝ ``verdict == "pass"`` **且**
    ``review_status == "approved"`` **且**該角色列於 ``roles``。三者缺一即不入
    第 2 層——「評估過」不等於「人工核可」，這正是裁決第 3 條的閘門。
    """

    executor: str
    model_id: str
    roles: tuple[str, ...]
    verdict: str
    evaluated_at: str
    eval_source: str
    review_status: str
    eval_ref: str | None = None
    reviewer: str | None = None
    reviewed_at: str | None = None
    notes: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.executor, self.model_id)

    def qualified(self) -> bool:
        return self.verdict == "pass" and self.review_status == "approved"

    def approves(self, role: str) -> bool:
        return self.qualified() and role in self.roles

    def disqualified_reason(self) -> str | None:
        if self.verdict != "pass":
            return f"evaluation verdict={self.verdict}"
        if self.review_status != "approved":
            return f"human review={self.review_status}"
        return None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "executor": self.executor,
            "model_id": self.model_id,
            "roles": list(self.roles),
            "verdict": self.verdict,
            "evaluated_at": self.evaluated_at,
            "eval_source": self.eval_source,
            "review_status": self.review_status,
        }
        for name in _EVAL_OPTIONAL_KEYS:
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True)
class EvalRoster:
    """``model-eval-roster.yaml`` 的載入結果。

    ``load_error`` 非 None 代表清單存在但解析失敗——此時 entries 一律為空
    （fail-closed：壞掉的清單絕不授予任何身分資格），但不丟例外，避免 #509 的
    「一列設定過期把整條調度迴圈打掛」重演；診斷由 ``cortex doctor`` 呈現。
    """

    schema_version: int = EVAL_ROSTER_SCHEMA_VERSION
    entries: tuple[EvalRosterEntry, ...] = ()
    path: str | None = None
    load_error: str | None = None

    def entry_for(self, executor: str, model_id: str) -> EvalRosterEntry | None:
        for entry in self.entries:
            if entry.key == (executor, model_id):
                return entry
        return None

    def approves(self, executor: str, model_id: str, role: str) -> bool:
        entry = self.entry_for(executor, model_id)
        return entry is not None and entry.approves(role)

    def qualified_entries(self) -> tuple[EvalRosterEntry, ...]:
        return tuple(entry for entry in self.entries if entry.qualified())

    def pending_entries(self) -> tuple[EvalRosterEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.qualified())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "path": self.path,
            "load_error": self.load_error,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def parse_eval_roster(payload: object, *, path: str | None = None) -> EvalRoster:
    """fail-closed 解析評估合格清單（未知欄位、非法值域一律拒絕）。"""

    if not isinstance(payload, Mapping):
        raise ValueError("model-eval-roster invalid root")
    extras = set(payload) - {"schema_version", "entries"}
    if extras:
        raise ValueError(f"model-eval-roster unexpected top-level key: {sorted(extras)[0]}")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version not in SUPPORTED_EVAL_ROSTER_SCHEMAS:
        raise ValueError(
            "model-eval-roster schema_version must be one of "
            f"{sorted(SUPPORTED_EVAL_ROSTER_SCHEMAS)}, got {schema_version!r}"
        )
    rows = payload.get("entries", [])
    if not isinstance(rows, list):
        raise ValueError("model-eval-roster entries must be a list")
    allowed = set(_EVAL_REQUIRED_KEYS) | set(_EVAL_OPTIONAL_KEYS)
    entries: list[EvalRosterEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        prefix = f"model-eval-roster[{index}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"{prefix} must be an object")
        unexpected = set(row) - allowed
        if unexpected:
            raise ValueError(f"{prefix}.{sorted(unexpected)[0]} unexpected")
        missing = [name for name in _EVAL_REQUIRED_KEYS if name not in row]
        if missing:
            raise ValueError(f"{prefix}.{missing[0]} is required")
        executor = _nonempty(row.get("executor"), f"{prefix}.executor")
        model_id = _nonempty(row.get("model_id"), f"{prefix}.model_id")
        roles_raw = row.get("roles")
        if not isinstance(roles_raw, list) or not roles_raw:
            raise ValueError(f"{prefix}.roles must be a non-empty string list")
        roles = tuple(_nonempty(item, f"{prefix}.roles[]") for item in roles_raw)
        if len(set(roles)) != len(roles):
            raise ValueError(f"{prefix}.roles contains duplicates")
        invalid = [role for role in roles if role not in EVAL_ROSTER_ROLES]
        if invalid:
            raise ValueError(f"{prefix}.roles invalid: {invalid[0]!r}")
        verdict = _nonempty(row.get("verdict"), f"{prefix}.verdict")
        if verdict not in EVAL_VERDICTS:
            raise ValueError(f"{prefix}.verdict invalid: {verdict!r}")
        review_status = _nonempty(row.get("review_status"), f"{prefix}.review_status")
        if review_status not in REVIEW_STATUSES:
            raise ValueError(f"{prefix}.review_status invalid: {review_status!r}")
        evaluated_at = _nonempty(row.get("evaluated_at"), f"{prefix}.evaluated_at")
        eval_source = _nonempty(row.get("eval_source"), f"{prefix}.eval_source")
        optional: dict[str, str | None] = {}
        for name in _EVAL_OPTIONAL_KEYS:
            raw = row.get(name)
            optional[name] = None if raw is None else _nonempty(raw, f"{prefix}.{name}")
        if review_status == "approved" and (
            optional["reviewer"] is None or optional["reviewed_at"] is None
        ):
            # 人工複核標記必須可稽核：approved 卻沒有複核人／複核日期，等同無人複核。
            raise ValueError(
                f"{prefix}.review_status=approved requires reviewer and reviewed_at"
            )
        key = (executor, model_id)
        if key in seen:
            raise ValueError(f"model-eval-roster duplicate entry: {executor}/{model_id}")
        seen.add(key)
        entries.append(
            EvalRosterEntry(
                executor=executor,
                model_id=model_id,
                roles=roles,
                verdict=verdict,
                evaluated_at=evaluated_at,
                eval_source=eval_source,
                review_status=review_status,
                **optional,
            )
        )
    return EvalRoster(
        schema_version=int(schema_version), entries=tuple(entries), path=path
    )


def load_eval_roster(config_root: str | Path) -> EvalRoster:
    """讀取 host config root 的評估合格清單；檔案不存在＝空清單（全向後相容）。"""

    path = Path(config_root) / EVAL_ROSTER_FILENAME
    if not path.is_file():
        return EvalRoster(path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
        payload = safe_load(text)
    except (OSError, UnicodeDecodeError, YAMLError, ValueError) as exc:
        raise ValueError(f"model-eval-roster unreadable: {path}: {exc}") from exc
    return parse_eval_roster(payload, path=str(path))


def load_eval_roster_degraded(config_root: str | Path) -> EvalRoster:
    """載入清單但**永不丟例外**——解析失敗回空清單＋``load_error``。

    #509 的教訓：設定資料的相容性問題不該讓調度迴圈整條停止。壞掉的清單只會讓
    第 2 層變空（保守方向，絕不因錯誤而多授予資格），診斷交給 doctor。
    """

    try:
        return load_eval_roster(config_root)
    except ValueError as exc:
        return EvalRoster(
            path=str(Path(config_root) / EVAL_ROSTER_FILENAME), load_error=str(exc)
        )


# ---------------------------------------------------------------------------
# overlay 的解析指令：packaged_overrides／resolution_policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackagedOverride:
    """overlay 對某個 packaged 身分的明示處置（#509 殘項）。

    - ``park``：完全停用，該身分不再進入任何解析候選。
    - ``demote``：保留可用性但降到最低位（排在同層其餘候選之後）。
    """

    executor: str
    model_id: str
    action: str
    reason: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.executor, self.model_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "executor": self.executor,
            "model_id": self.model_id,
            "action": self.action,
            "reason": self.reason,
        }


def parse_packaged_overrides(value: object) -> tuple[PackagedOverride, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("model-identities packaged_overrides must be a list")
    allowed = {"executor", "model_id", "action", "reason"}
    overrides: list[PackagedOverride] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(value):
        prefix = f"model-identities.packaged_overrides[{index}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"{prefix} must be an object")
        extras = set(row) - allowed
        if extras:
            raise ValueError(f"{prefix}.{sorted(extras)[0]} unexpected")
        missing = [name for name in sorted(allowed) if name not in row]
        if missing:
            raise ValueError(f"{prefix}.{missing[0]} is required")
        executor = _nonempty(row.get("executor"), f"{prefix}.executor")
        model_id = _nonempty(row.get("model_id"), f"{prefix}.model_id")
        action = _nonempty(row.get("action"), f"{prefix}.action")
        if action not in PACKAGED_ACTIONS:
            raise ValueError(f"{prefix}.action invalid: {action!r}")
        reason = _nonempty(row.get("reason"), f"{prefix}.reason")
        key = (executor, model_id)
        if key in seen:
            raise ValueError(f"model-identities packaged_overrides duplicate: {executor}/{model_id}")
        seen.add(key)
        overrides.append(
            PackagedOverride(
                executor=executor, model_id=model_id, action=action, reason=reason
            )
        )
    return tuple(overrides)


@dataclass(frozen=True)
class ResolutionPolicy:
    """解析政策（overlay 的 ``resolution_policy`` 區塊，全選填）。"""

    packaged_fallback: str = PACKAGED_FALLBACK_WARN

    def __post_init__(self) -> None:
        if self.packaged_fallback not in PACKAGED_FALLBACK_POLICIES:
            raise ValueError(
                "model-identities resolution_policy.packaged_fallback invalid: "
                f"{self.packaged_fallback!r}"
            )

    def to_dict(self) -> dict[str, object]:
        return {"packaged_fallback": self.packaged_fallback}


def parse_resolution_policy(value: object, *, default: str) -> ResolutionPolicy:
    if value is None:
        return ResolutionPolicy(packaged_fallback=default)
    if not isinstance(value, Mapping):
        raise ValueError("model-identities resolution_policy must be an object")
    extras = set(value) - {"packaged_fallback"}
    if extras:
        raise ValueError(
            f"model-identities resolution_policy.{sorted(extras)[0]} unexpected"
        )
    raw = value.get("packaged_fallback")
    if raw is None:
        return ResolutionPolicy(packaged_fallback=default)
    return ResolutionPolicy(
        packaged_fallback=_nonempty(raw, "model-identities resolution_policy.packaged_fallback")
    )


@dataclass(frozen=True)
class ResolutionNote:
    """載入期留下的解析診斷（doctor／log 消費，不影響推進邏輯）。"""

    code: str
    severity: str  # "info" | "warn" | "fail"
    detail: str

    def __post_init__(self) -> None:
        if self.severity not in {"info", "warn", "fail"}:
            raise ValueError(f"resolution note severity invalid: {self.severity!r}")

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass(frozen=True)
class ResolutionContext:
    """一次 registry 載入所帶的解析上下文（政策＋第 2 層清單＋診斷）。"""

    policy: ResolutionPolicy = field(default_factory=ResolutionPolicy)
    eval_roster: EvalRoster = field(default_factory=EvalRoster)
    packaged_overrides: tuple[PackagedOverride, ...] = ()
    notes: tuple[ResolutionNote, ...] = ()
    config_root: str | None = None
    overlay_present: bool = False

    def with_notes(self, extra: Iterable[ResolutionNote]) -> "ResolutionContext":
        return ResolutionContext(
            policy=self.policy,
            eval_roster=self.eval_roster,
            packaged_overrides=self.packaged_overrides,
            notes=self.notes + tuple(extra),
            config_root=self.config_root,
            overlay_present=self.overlay_present,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy.to_dict(),
            "eval_roster": self.eval_roster.to_dict(),
            "packaged_overrides": [item.to_dict() for item in self.packaged_overrides],
            "notes": [item.to_dict() for item in self.notes],
            "config_root": self.config_root,
            "overlay_present": self.overlay_present,
        }


#: 手工建構的 registry（測試替身、程式內組裝）沒有 loader 蓋章，一律視為
#: 「呼叫端自行宣告」＝第 1 層，維持 #534 之前的既有順序語意。
DEFAULT_CONTEXT = ResolutionContext(policy=ResolutionPolicy(PACKAGED_FALLBACK_ALLOW))


# ---------------------------------------------------------------------------
# 解析：分層、排序、政策
# ---------------------------------------------------------------------------


def identity_origin(identity: object) -> str:
    origin = getattr(identity, "origin", None)
    return origin if origin in IDENTITY_ORIGINS else IDENTITY_ORIGIN_OVERLAY


def identity_layer(
    identity: object, *, role: str, eval_roster: EvalRoster | None = None
) -> str | None:
    """(identity, role) → 解析層；``None`` 代表被 operator park（不可解析）。"""

    if getattr(identity, "operator_action", None) == PACKAGED_ACTION_PARK:
        return None
    if identity_origin(identity) == IDENTITY_ORIGIN_OVERLAY:
        return RESOLUTION_LAYER_OVERLAY
    roster = eval_roster or EvalRoster()
    if roster.approves(
        getattr(identity, "executor", ""), getattr(identity, "model_id", ""), role
    ):
        return RESOLUTION_LAYER_EVALUATED
    return RESOLUTION_LAYER_PACKAGED


@dataclass(frozen=True)
class RankedCandidates:
    """分層排序後的候選清單＋可觀測的排除理由與警示。"""

    ordered: tuple
    layers: Mapping[tuple[str, str], str]
    excluded: tuple[tuple[object, str], ...] = ()
    warnings: tuple[str, ...] = ()

    def layer_of(self, identity: object) -> str | None:
        return self.layers.get(
            (getattr(identity, "executor", ""), getattr(identity, "model_id", ""))
        )

    def exclusion_detail(self) -> str:
        return "; ".join(
            f"{getattr(item, 'executor', '?')}/{getattr(item, 'model_id', '?')}: {reason}"
            for item, reason in self.excluded
        )


def rank_candidates(
    candidates: Sequence,
    *,
    role: str,
    context: ResolutionContext | None = None,
    compatibility_for: Callable[[object], Mapping[str, object] | None] | None = None,
) -> RankedCandidates:
    """把既有候選清單重排成三層解析鏈的順序，並套用 packaged fallback 政策。

    排序為 **stable**：層級是主鍵，同層內完全維持呼叫端傳進來的既有順序——
    #452 的 measured 側寫優先、#262 的 primary_domain 偏好因此降級為同層內的
    次要偏好，不再有機會把 operator 的人工指定擠到後面（#534 主訴）。若提供
    ``compatibility_for``，它會在排序前以同一個 persona/executor contract
    validator 篩掉未具備 launcher、toolchain 或 credential grant 的身分。
    """

    ctx = context or DEFAULT_CONTEXT
    roster = ctx.eval_roster
    excluded: list[tuple[object, str]] = []
    warnings: list[str] = []
    layers: dict[tuple[str, str], str] = {}
    ranked: list[tuple[int, int, int, object]] = []
    overlay_contract_failure = False
    for index, identity in enumerate(candidates):
        layer = identity_layer(identity, role=role, eval_roster=roster)
        if layer is None:
            excluded.append((identity, "operator overlay parked this packaged identity"))
            continue
        if (
            layer == RESOLUTION_LAYER_PACKAGED
            and ctx.policy.packaged_fallback == PACKAGED_FALLBACK_DENY
        ):
            excluded.append(
                (
                    identity,
                    "packaged-fallback denied by resolution_policy: 先經 patchmud eval "
                    "並人工複核加入 model-eval-roster.yaml，或列入 host overlay",
                )
            )
            continue
        if compatibility_for is not None:
            try:
                contract = compatibility_for(identity)
                if not isinstance(contract, Mapping):
                    raise ValueError("compatibility contract is not an object")
                validate_persona_executor_compatibility(
                    persona={
                        "planning": "planner",
                        "build": "builder",
                        "review": "reviewer",
                    }.get(role, role),
                    identity=identity,
                    launcher_profile=contract.get("launcher_profile"),
                    toolchain_grant=contract.get("toolchain_grant"),
                    credential_grant=contract.get("credential_grant"),
                )
            except Exception as exc:  # noqa: BLE001 - rejection is diagnostic data
                excluded.append((identity, str(exc)))
                if identity_origin(identity) == IDENTITY_ORIGIN_OVERLAY:
                    # An explicit operator declaration must not silently fall
                    # through to a packaged identity with a different contract.
                    overlay_contract_failure = True
                continue
        demoted = 1 if getattr(identity, "operator_action", None) == PACKAGED_ACTION_DEMOTE else 0
        layers[
            (getattr(identity, "executor", ""), getattr(identity, "model_id", ""))
        ] = layer
        ranked.append((_LAYER_RANK[layer], demoted, index, identity))
    if overlay_contract_failure:
        ranked = []
    ranked.sort(key=lambda row: (row[0], row[1], row[2]))
    ordered = tuple(row[3] for row in ranked)
    if ordered:
        top = ordered[0]
        top_layer = layers[
            (getattr(top, "executor", ""), getattr(top, "model_id", ""))
        ]
        if (
            top_layer == RESOLUTION_LAYER_PACKAGED
            and ctx.policy.packaged_fallback == PACKAGED_FALLBACK_WARN
        ):
            warnings.append(
                f"role={role} 解析落到 packaged-fallback："
                f"{getattr(top, 'executor', '?')}/{getattr(top, 'model_id', '?')}"
                "——該身分僅為候選宣告（未經 patchmud eval／人工複核）。"
                "請列入 host overlay 或評估合格後加入 model-eval-roster.yaml。"
            )
    return RankedCandidates(
        ordered=ordered,
        layers=layers,
        excluded=tuple(excluded),
        warnings=tuple(warnings),
    )
