"""#836 的 provider quota 讀取契約與純資料 adapter。

此模組只解析呼叫端已取得的正式、可讀 payload；不啟動 CLI、不讀 credential，
也不把 quota 結果交給 admission 或 dispatcher。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
from typing import Any

from . import quota_observation as schema


_PROFILE_KEY_RE = re.compile(r"epk:v1:resolved:[0-9a-f]{64}\Z")
_RESOURCE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
CODEX_UNIT_SEMANTICS = "provider:openai-codex-app-server/rate-limit-percent/v2"
COPILOT_UNIT_SEMANTICS = "provider:github-copilot-sdk/requests/v1"
AGY_UNIT_SEMANTICS = "provider:google-antigravity-cli/quota-fraction/v1"
CLAUDE_UNIT_SEMANTICS = "provider:anthropic-claude-code/rate-limit-percent/v1"
_TIME_MAX_MS = 253402300799999
_DURATION_MAX_MS = 31622400000


@dataclass(frozen=True)
class ProviderQuotaTarget:
    """由 operator 明確配置的 provider resource → pool/window 對照。"""

    resource_key: str
    binding: schema.ProfilePoolBinding
    descriptor: schema.PoolDescriptor
    window_id: str


@dataclass(frozen=True)
class CoverageGap:
    resource_key: str
    reason: str


@dataclass(frozen=True)
class ProviderCapture:
    executor: str
    observations: tuple[schema.QuotaObservation, ...]
    gaps: tuple[CoverageGap, ...]


def provider_read_contract(executor: str) -> dict[str, object]:
    """回傳呼叫端可選用的正式唯讀介面；本函式絕不呼叫該介面。"""

    contracts: dict[str, dict[str, object]] = {
        "codex": {
            "state": "supported",
            "method": "account/rateLimits/read",
            "source_schema": "openai-codex-app-server-rate-limits-v2",
            "authority_ref": "https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/account.rs",
            "unit_semantics_ref": CODEX_UNIT_SEMANTICS,
        },
        "copilot": {
            "state": "supported",
            "method": "account.getQuota",
            "source_schema": "github-copilot-sdk-quota-v1",
            "authority_ref": "https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing",
            "unit_semantics_ref": COPILOT_UNIT_SEMANTICS,
        },
        "agy": {
            "state": "supported",
            "argv": ("agy", "-p", "/usage", "--output-format", "json"),
            "source_schema": "google-antigravity-cli-usage-v1",
            "authority_ref": "https://antigravity.google/docs/cli/commands/usage",
            "unit_semantics_ref": AGY_UNIT_SEMANTICS,
        },
        "claude": {
            "state": "supported",
            "method": "structured_event",
            "source_schema": "anthropic-claude-code-stream-json-rate-limit-event-v1",
            "authority_ref": "https://docs.anthropic.com/en/docs/claude-code/headless",
            "unit_semantics_ref": CLAUDE_UNIT_SEMANTICS,
        },
        "cg": {"state": "unknown", "reason": "no-verified-quota-read-contract"},
    }
    return dict(contracts.get(executor, {"state": "unknown", "reason": "unknown-executor"}))


def capture_provider_quota(
    executor: str,
    payload: object,
    *,
    profile_key: str,
    model_id: str | None = None,
    targets: tuple[ProviderQuotaTarget, ...],
    descriptors: tuple[schema.PoolDescriptor, ...],
    unit_catalog: tuple[schema.UnitDefinition, ...],
    observed_at_ms: int,
    ttl_ms: int = 300_000,
) -> ProviderCapture:
    """將 provider 回應投影為 observation；未知值只產生 unknown 與 coverage gap。"""

    if type(targets) is not tuple:
        return ProviderCapture(executor, (), (CoverageGap("quota-targets", "invalid-target-mapping"),))
    if not isinstance(profile_key, str) or not _PROFILE_KEY_RE.fullmatch(profile_key):
        return ProviderCapture(executor, (), tuple(
            CoverageGap(target.resource_key, "profile-key-not-resolved") for target in targets
        ))
    if (type(observed_at_ms) is not int or observed_at_ms < 0 or observed_at_ms > _TIME_MAX_MS
            or type(ttl_ms) is not int or ttl_ms < 1 or ttl_ms > _DURATION_MAX_MS
            or observed_at_ms + ttl_ms > _TIME_MAX_MS):
        return ProviderCapture(executor, (), tuple(
            CoverageGap(target.resource_key, "invalid-capture-time") for target in targets
        ))
    try:
        indexed = _validate_targets(
            targets,
            profile_key=profile_key,
            executor=executor,
            model_id=model_id,
            descriptors=descriptors,
            unit_catalog=unit_catalog,
        )
    except (TypeError, ValueError, schema.QuotaContractError):
        return ProviderCapture(executor, (), tuple(
            CoverageGap(target.resource_key, "invalid-target-mapping") for target in targets
        ))

    contract = provider_read_contract(executor)
    if contract.get("state") != "supported":
        reason = str(contract.get("reason", "provider-interface-unsupported"))
        return _unknown_capture(
            executor, targets, profile_key, observed_at_ms, ttl_ms, reason, descriptors, unit_catalog,
            model_id=model_id,
        )

    try:
        values = _parse_provider_payload(executor, payload, targets)
    except (TypeError, ValueError, InvalidOperation, OverflowError, KeyError):
        values = {}
        parse_error = True
    else:
        parse_error = False

    observations: list[schema.QuotaObservation] = []
    gaps: list[CoverageGap] = []
    for target in targets:
        target_info = indexed[target.resource_key]
        parsed = values.get(target.resource_key)
        reason: str | None = None
        amount: str | None = None
        reset_at_ms: int | None = None
        if parsed is None:
            reason = "invalid-provider-value" if parse_error or payload is not None else "provider-value-missing"
        else:
            amount, reset_at_ms, reason = parsed
        if reason is not None:
            gaps.append(CoverageGap(target.resource_key, reason))
        observations.append(_observation(
            executor=executor,
            resource_key=target.resource_key,
            profile_key=profile_key,
            target=target,
            target_info=target_info,
            amount=amount,
            reason=reason,
            observed_at_ms=observed_at_ms,
            ttl_ms=ttl_ms,
            reset_at_ms=reset_at_ms,
            unit_catalog=unit_catalog,
            descriptors=descriptors,
        ))
    return ProviderCapture(executor, tuple(observations), tuple(gaps))


def _validate_targets(targets, *, profile_key, descriptors, unit_catalog, executor=None, model_id=None):
    if type(targets) is not tuple or len(targets) > 64:
        raise ValueError("invalid targets")
    if type(descriptors) is not tuple or type(unit_catalog) is not tuple:
        raise ValueError("invalid schema context")
    descriptor_by_ref = {item.pool_ref: item for item in descriptors}
    if len(descriptor_by_ref) != len(descriptors):
        raise ValueError("duplicate pool descriptors")
    result: dict[str, dict[str, object]] = {}
    for target in targets:
        if (not isinstance(target, ProviderQuotaTarget)
                or not isinstance(target.resource_key, str)
                or not _RESOURCE_KEY_RE.fullmatch(target.resource_key)):
            raise ValueError("invalid target")
        if target.resource_key in result:
            raise ValueError("duplicate resource mapping")
        descriptor = target.descriptor
        registered = descriptor_by_ref.get(descriptor.pool_ref)
        if registered is None or registered.to_dict() != descriptor.to_dict():
            raise ValueError("descriptor is not in the supplied context")
        status = schema.binding_status(target.binding)
        binding = target.binding.to_dict()
        if status.get("state") != "complete" or not _binding_has_profile(
            binding, profile_key, executor=executor, model_id=model_id
        ):
            raise ValueError("binding does not cover this profile")
        pool_ref = descriptor.pool_ref
        constraints = binding.get("constraints", [])
        matches = [
            row.get("value") for row in constraints
            if isinstance(row, dict) and row.get("state") == "known"
            and isinstance(row.get("value"), dict)
            and tuple(row["value"].get("pool_ref", {}).get(key) for key in (
                "authority_id", "account_id", "pool_id", "revision"
            )) == pool_ref
            and row["value"].get("window_id") == target.window_id
        ]
        window = next((item for item in descriptor.to_dict()["windows"]
                       if item["window_id"] == target.window_id), None)
        if not matches or window is None:
            raise ValueError("binding omits target pool/window")
        unit_ref = (window["unit_ref"]["unit_id"], window["unit_ref"]["version"])
        unit = next((item for item in (*descriptor.units, *unit_catalog) if item.ref == unit_ref), None)
        if unit is None or unit.quantity_kind != "amount":
            raise ValueError("target unit is not a known amount")
        result[target.resource_key] = {
            "unit_ref": unit_ref,
            "window": window,
            "unit": unit,
            "pool_ref": pool_ref,
        }
    return result


def _binding_has_profile(
    binding: dict[str, Any], profile_key: str, *, executor: str | None = None,
    model_id: str | None = None,
) -> bool:
    """重放固定 target 的 subject 判定；identity 必須精確比對 executor/model。

    這裡的 ``profile_key`` 一定是某個 collector target（見
    `quota_collectors._find_binding`）在**設定載入當下**已經精確比對過一次
    的 resolved key——`target.binding` 是那次比對留下的固定物件，本函式在
    `_validate_targets()` 只是重放同一份判定（收到的 payload／targets 沒變、
    profile_key 沒變），不是重新搜尋『這個候選現在有沒有 binding』。一般
    collector target 仍只帶 profile/group subject；終局 Claude job harvest 另帶
    model_id，才可在此驗證 quota-pools identity subject。"""
    subject = binding.get("subject")
    if not isinstance(subject, dict):
        return False
    if subject.get("kind") == "profile":
        ref = subject.get("profile_ref")
        return bool(isinstance(ref, dict) and ref.get("state") == "known"
                    and isinstance(ref.get("value"), dict)
                    and ref["value"].get("key") == profile_key)
    if subject.get("kind") == "group":
        members = subject.get("members")
        if not isinstance(members, dict) or members.get("state") != "known":
            return False
        return any(isinstance(item, dict) and item.get("key") == profile_key
                   for item in members.get("value", []))
    if subject.get("kind") == "identity":
        return bool(
            isinstance(executor, str) and isinstance(model_id, str)
            and subject.get("executor") == executor and subject.get("model_id") == model_id
        )
    return False


def _parse_provider_payload(executor: str, payload: object, targets):
    if not isinstance(payload, dict):
        return {}
    if executor == "codex":
        return _parse_codex(payload, targets)
    if executor == "copilot":
        return _parse_copilot(payload, targets)
    if executor == "agy":
        return _parse_agy(payload, targets)
    if executor == "claude":
        return _parse_claude(payload, targets)
    return {}


def _parse_claude(payload: dict[str, Any], targets):
    """解析 Claude Code stream-json 的最後一筆 rate_limit_event。"""
    events = payload.get("rate_limit_events")
    if events is None:
        events = (payload,) if payload.get("type") == "rate_limit_event" else ()
    if not isinstance(events, (list, tuple)):
        events = ()
    event = next((row for row in reversed(events)
                  if isinstance(row, dict) and row.get("type") == "rate_limit_event"), None)
    info = event.get("rate_limit_info") if isinstance(event, dict) else None
    windows = info.get("unifiedWindows") if isinstance(info, dict) else None
    result: dict[str, tuple[str | None, int | None, str | None]] = {}
    for target in targets:
        key = target.resource_key
        if target.window_id not in {"five_hour", "seven_day"}:
            result[key] = (None, None, "unsupported-provider-window")
            continue
        window = windows.get(target.window_id) if isinstance(windows, dict) else None
        if not isinstance(window, dict):
            result[key] = (None, None, "provider-window-absent" if isinstance(windows, dict)
                           else "provider-value-missing")
            continue
        utilization = window.get("utilization")
        reset_seconds = window.get("resetsAt")
        if (isinstance(utilization, bool) or not isinstance(utilization, (int, float, Decimal))
                or isinstance(reset_seconds, bool) or type(reset_seconds) is not int
                or reset_seconds < 0 or reset_seconds * 1000 > _TIME_MAX_MS):
            result[key] = (None, None, "invalid-provider-value")
            continue
        try:
            used = Decimal(str(utilization))
        except InvalidOperation:
            result[key] = (None, None, "invalid-provider-value")
            continue
        if not used.is_finite() or used < 0 or used > 1:
            result[key] = (None, None, "invalid-provider-value")
            continue
        if _target_semantics(target) != CLAUDE_UNIT_SEMANTICS:
            result[key] = (None, None, "provider-unit-mapping-mismatch")
            continue
        remaining_percent = (Decimal(1) - used) * Decimal(100)
        result[key] = (_decimal_wire(remaining_percent), reset_seconds * 1000, None)
    return result


def _codex_merged_field(
    by_id_snapshot: dict[str, Any] | None, top_snapshot: dict[str, Any] | None, field_name: str,
) -> tuple[object, bool]:
    """合併同一 limitId 在 ``rateLimitsByLimitId``／頂層 ``rateLimits`` 兩處對
    單一欄位（window 子物件或 gating 欄位）的回報。

    只有兩邊都給出非 null 的值、且值不同才算矛盾；任一邊缺席（key 不存在或
    值為 null，例如另一票只提供 ``{"limitId": "codex"}`` 這種殘缺 stub）不算
    矛盾，直接採用有值的那一邊——不能因為某處只是回報不完整就誤判整個
    observation 矛盾。
    """

    a = by_id_snapshot.get(field_name) if isinstance(by_id_snapshot, dict) else None
    b = top_snapshot.get(field_name) if isinstance(top_snapshot, dict) else None
    if a is not None and b is not None and a != b:
        return None, True
    return (a if a is not None else b), False


def _codex_window_value(
    *,
    ordinary_allowed: object,
    reached_type: object,
    spend_control: object,
    window: object,
    target: "ProviderQuotaTarget",
) -> tuple[str | None, int | None, str | None]:
    """解析單一 limitId 下、單一 window 的剩餘額度。

    ``ordinaryUsageAllowed`` 是整個回應（帳號）層級的 gating 欄位（回應頂層，
    不巢在任一 limitId snapshot 底下）；``rateLimitReachedType``／
    ``spendControlReached`` 是該 limitId 層級的 gating 欄位（不是某個 window
    專屬）。provider 已明示拒絕一般用量時，不得讓任何 window 的 observation
    呈現為「有剩餘」；改記為 remaining 0（`provider-reports-limit-reached`，
    保留 resetsAt）而不是猜測的百分比。
    """

    if (
        (ordinary_allowed is not None and type(ordinary_allowed) is not bool)
        or (reached_type is not None and not isinstance(reached_type, str))
        or (spend_control is not None and type(spend_control) is not bool)
    ):
        return (None, None, "invalid-provider-value")
    blocked = ordinary_allowed is False or reached_type is not None or spend_control is True

    if window is None:
        # provider 明確回報「沒有這個 window」（缺 key 或值為 null），是精確的
        # coverage gap，不是格式錯誤——不要歸成 invalid-provider-value。
        return (None, None, "provider-window-absent")
    if not isinstance(window, dict):
        return (None, None, "invalid-provider-value")

    used = window.get("usedPercent", window.get("used_percent"))
    duration = window.get("windowDurationMins", window.get("window_duration_mins"))
    reset_seconds = window.get("resetsAt", window.get("resets_at"))
    expected = next((item.get("duration_ms") for item in target.descriptor.to_dict()["windows"]
                     if item.get("window_id") == target.window_id), None)
    if (type(used) is not int or used < 0 or used > 100
            or (duration is not None and (
                type(duration) is not int or duration <= 0
                or expected is None or expected != duration * 60_000
            ))
            or (reset_seconds is not None and (
                type(reset_seconds) is not int or reset_seconds < 0
                or reset_seconds * 1000 > _TIME_MAX_MS
            ))
            or expected is None):
        return (None, None, "invalid-provider-value")
    if _target_semantics(target) != CODEX_UNIT_SEMANTICS:
        return (None, None, "provider-unit-mapping-mismatch")

    reset_at_ms = None if reset_seconds is None else reset_seconds * 1000
    if blocked:
        return ("0", reset_at_ms, "provider-reports-limit-reached")
    return (str(100 - used), reset_at_ms, None)


def _parse_codex(payload: dict[str, Any], targets):
    """解析 codex 回應；``target.resource_key`` 形狀固定為
    ``codex:<limitId>:<providerWindowName>``——``<providerWindowName>``（如
    ``primary``／``secondary``）是 provider 回應裡的欄位名，跟本地
    ``target.window_id``（descriptor 那端的 window 識別，可以是任意 operator
    命名，例如 ``short``）是兩個獨立的識別，不能假設兩者字面相同。

    同一 limitId 可能同時出現在頂層 ``rateLimits`` 與
    ``rateLimitsByLimitId[limitId]``（見 #836 對抗審查第九輪 MAJOR-1、
    qualification/driver.py 的既有容忍寫法）：兩處都缺席才算沒有這個 limitId；
    只要任一處有值就採用；兩處都給出非 null 值但不同才算矛盾，回精確的
    ``provider-limit-snapshot-conflict`` gap，不猜哪一份才對。
    """

    body = payload.get("result", payload)
    if not isinstance(body, dict):
        return {}
    by_limit_id_raw = body.get("rateLimitsByLimitId")
    by_limit_id = by_limit_id_raw if isinstance(by_limit_id_raw, dict) else {}
    # 第三層 fallback（直接把整個 body 當 snapshot）延續既有行為：某些呼叫端
    # 直接把 limits 物件本身當 payload 傳入，沒有 rateLimits／rate_limits 包一層。
    top_level_raw = body.get("rateLimits", body.get("rate_limits", body))
    top_level = top_level_raw if isinstance(top_level_raw, dict) else None
    top_limit_id = None
    if top_level is not None:
        candidate = top_level.get("limitId", top_level.get("limit_id"))
        if isinstance(candidate, str) and candidate:
            top_limit_id = candidate
    # ordinaryUsageAllowed 是整個回應層級的欄位（回應頂層，跟 rateLimits／
    # rateLimitsByLimitId 平行），不巢在任一 limitId snapshot 底下。
    ordinary_allowed = body.get("ordinaryUsageAllowed")

    # 候選 limitId：rateLimitsByLimitId 的 key 加上頂層 limitId；resource_key
    # 對到哪個候選字首取「最長者」，避免恰好互為前綴時誤配到較短的那個。
    candidate_limit_ids = [key for key in by_limit_id if isinstance(key, str) and key]
    if top_limit_id is not None and top_limit_id not in candidate_limit_ids:
        candidate_limit_ids.append(top_limit_id)

    result: dict[str, tuple[str | None, int | None, str | None]] = {}
    for target in targets:
        key = target.resource_key
        if not key.startswith("codex:"):
            continue
        remainder = key[len("codex:"):]
        matched_limit_id: str | None = None
        for limit_id in candidate_limit_ids:
            if remainder.startswith(f"{limit_id}:") and (
                matched_limit_id is None or len(limit_id) > len(matched_limit_id)
            ):
                matched_limit_id = limit_id
        if matched_limit_id is None:
            continue
        window_name = remainder[len(matched_limit_id) + 1:]
        if not window_name:
            continue

        by_id_snapshot = by_limit_id.get(matched_limit_id)
        by_id_snapshot = by_id_snapshot if isinstance(by_id_snapshot, dict) else None
        top_snapshot = top_level if matched_limit_id == top_limit_id else None

        if by_id_snapshot is None and top_snapshot is None:
            continue  # 交回上層依既有規則判定 invalid-provider-value／provider-value-missing

        window, window_conflict = _codex_merged_field(by_id_snapshot, top_snapshot, window_name)
        reached_type, reached_conflict = _codex_merged_field(
            by_id_snapshot, top_snapshot, "rateLimitReachedType"
        )
        spend_control, spend_conflict = _codex_merged_field(
            by_id_snapshot, top_snapshot, "spendControlReached"
        )
        if window_conflict or reached_conflict or spend_conflict:
            result[key] = (None, None, "provider-limit-snapshot-conflict")
            continue

        result[key] = _codex_window_value(
            ordinary_allowed=ordinary_allowed, reached_type=reached_type,
            spend_control=spend_control, window=window, target=target,
        )
    return result


def _parse_copilot(payload: dict[str, Any], targets):
    body = payload.get("result", payload)
    if not isinstance(body, dict):
        return {}
    snapshots = body.get("quotaSnapshots", body.get("quota_snapshots"))
    if not isinstance(snapshots, dict):
        return {}
    result = {}
    for target in targets:
        prefix = "copilot:"
        if not target.resource_key.startswith(prefix):
            continue
        key = target.resource_key[len(prefix):]
        row = snapshots.get(key)
        if not isinstance(row, dict):
            continue
        entitlement, used = row.get("entitlementRequests"), row.get("usedRequests")
        if (type(entitlement) is not int or type(used) is not int
                or entitlement < 0 or used < 0 or used > entitlement):
            result[target.resource_key] = (None, None, "invalid-provider-value")
            continue
        if _target_semantics(target) != COPILOT_UNIT_SEMANTICS:
            result[target.resource_key] = (None, None, "provider-unit-mapping-mismatch")
            continue
        reset_at = _parse_iso_epoch_ms(row.get("resetDate"))
        if row.get("resetDate") is not None and reset_at is None:
            result[target.resource_key] = (None, None, "invalid-provider-value")
            continue
        result[target.resource_key] = (str(entitlement - used), reset_at, None)
    return result


def _parse_agy(payload: dict[str, Any], targets):
    # CLI usage JSON and the documented statusline quota object share only the
    # explicitly named fields below; unknown response shapes are not guessed.
    quota = payload.get("quota")
    if not isinstance(quota, dict):
        command = payload.get("command")
        data = command.get("data") if isinstance(command, dict) else None
        groups = data.get("groups") if isinstance(data, dict) else None
        quota = {}
        if isinstance(groups, list):
            for group in groups:
                buckets = group.get("buckets") if isinstance(group, dict) else None
                if not isinstance(buckets, list):
                    continue
                for bucket in buckets:
                    if not isinstance(bucket, dict):
                        continue
                    key = bucket.get("id", bucket.get("key"))
                    if isinstance(key, str) and key and key not in quota:
                        quota[key] = bucket
    result = {}
    for target in targets:
        if not target.resource_key.startswith("agy:"):
            continue
        key = target.resource_key[len("agy:"):]
        row = quota.get(key) if isinstance(quota, dict) else None
        if not isinstance(row, dict):
            continue
        fraction = row.get("remaining_fraction")
        if type(fraction) not in (int, float) or not math.isfinite(float(fraction)):
            result[target.resource_key] = (None, None, "invalid-provider-value")
            continue
        value = Decimal(str(fraction))
        if value < 0 or value > 1:
            result[target.resource_key] = (None, None, "invalid-provider-value")
            continue
        if _target_semantics(target) != AGY_UNIT_SEMANTICS:
            result[target.resource_key] = (None, None, "provider-unit-mapping-mismatch")
            continue
        reset_at = _parse_iso_epoch_ms(row.get("reset_time"))
        if row.get("reset_time") is not None and reset_at is None:
            result[target.resource_key] = (None, None, "invalid-provider-value")
            continue
        result[target.resource_key] = (_decimal_wire(value), reset_at, None)
    return result


def _target_semantics(target: ProviderQuotaTarget) -> str | None:
    wire = target.descriptor.to_dict()
    window = next((item for item in wire["windows"] if item["window_id"] == target.window_id), None)
    if window is None:
        return None
    ref = window["unit_ref"]
    unit = next((item for item in target.descriptor.units
                 if item.ref == (ref["unit_id"], ref["version"])), None)
    return unit.semantics_ref if unit is not None else None


def _unknown_capture(executor, targets, profile_key, observed_at_ms, ttl_ms, reason, descriptors,
                     unit_catalog, *, model_id=None):
    observations: list[schema.QuotaObservation] = []
    gaps: list[CoverageGap] = []
    try:
        indexed = _validate_targets(targets, profile_key=profile_key, executor=executor,
                                    model_id=model_id, descriptors=descriptors,
                                    unit_catalog=unit_catalog)
    except (TypeError, ValueError, schema.QuotaContractError):
        return ProviderCapture(executor, (), tuple(CoverageGap(t.resource_key, "invalid-target-mapping") for t in targets))
    for target in targets:
        gaps.append(CoverageGap(target.resource_key, reason))
        observations.append(_observation(
            executor=executor, resource_key=target.resource_key, profile_key=profile_key,
            target=target, target_info=indexed[target.resource_key], amount=None,
            reason=reason if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", reason) else "provider-interface-unknown",
            observed_at_ms=observed_at_ms, ttl_ms=ttl_ms, reset_at_ms=None,
            unit_catalog=unit_catalog, descriptors=descriptors,
        ))
    return ProviderCapture(executor, tuple(observations), tuple(gaps))


def _provider_window_instance(target_info: dict[str, object], reset_at_ms: int | None) -> dict[str, object]:
    """由 provider 正式回報的 reset 時間與 pool 契約既有的 window duration
    推導可比對的 window epoch；缺 reset 或無法唯一判定時維持 unknown，不臆測。

    真實 producer 路徑（record_provider_read）先前一律回報
    window_instance=unknown，即使收到含 reset 的 fresh snapshot，project() 仍
    只能報 window-epoch-unknown 而不會扣減終局 usage（見 #836 對抗審查第六輪
    BLOCKER quota_sources.py:431）。target_info["window"] 的 duration_ms 只在
    `_validate_targets()` 確認過 quantity_kind 為 amount 的 fixed/rolling 窗口
    才會有值；schema 進一步要求 window_instance 的 end_ms-start_ms 恰等於該
    duration_ms，因此可用 reset_at_ms 當窗口右界、往回推 duration_ms 當左界。
    """

    if reset_at_ms is None:
        return {"kind": "unknown", "reason": "provider-window-instance-unavailable"}
    duration_ms = target_info["window"].get("duration_ms")
    if type(duration_ms) is not int or duration_ms <= 0:
        return {"kind": "unknown", "reason": "provider-window-instance-unavailable"}
    start_ms = reset_at_ms - duration_ms
    if start_ms < 0 or reset_at_ms > _TIME_MAX_MS:
        return {"kind": "unknown", "reason": "provider-window-instance-unavailable"}
    return {
        "kind": "interval",
        "start_ms": start_ms,
        "end_ms": reset_at_ms,
        "epoch": {"state": "known", "value": f"provider-reset:{reset_at_ms}"},
    }


def _observation(*, executor, resource_key, profile_key, target, target_info, amount, reason,
                 observed_at_ms, ttl_ms, reset_at_ms, unit_catalog, descriptors):
    contract = provider_read_contract(executor)
    source_id = {
        "codex": "openai-codex-app-server",
        "copilot": "github-copilot-sdk",
        "agy": "google-antigravity-cli",
        "claude": "anthropic-claude-code",
    }.get(executor, "cortex-unknown-provider")
    source_schema = str(contract.get("source_schema", "cortex-provider-quota-unknown-v1"))
    authority_ref = str(contract.get("authority_ref", "cortex:provider-read-contract/v1"))
    unit_ref = target_info["unit_ref"]
    descriptor = target.descriptor
    pool_ref = {
        "authority_id": descriptor.authority_id,
        "account_id": descriptor.account_id,
        "pool_id": descriptor.pool_id,
        "revision": descriptor.revision,
    }
    payload = {
        "schema_version": 1,
        "observation_id": "quota-" + hashlib.sha256(
            f"{executor}\0{resource_key}\0{profile_key}\0{observed_at_ms}".encode()
        ).hexdigest()[:32],
        "scope": {"state": "known", "value": {"pool_ref": pool_ref, "window_id": target.window_id}},
        "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": profile_key}},
        "unit_ref": {"state": "known", "value": {"unit_id": unit_ref[0], "version": unit_ref[1]}},
        "window_instance": _provider_window_instance(target_info, reset_at_ms),
        "measurement": {
            "kind": "remaining_snapshot", "metric_id": "remaining",
            "quantity": ({"state": "unknown", "reason": reason}
                         if amount is None else {"state": "observed", "amount": {"kind": "exact", "value": amount}}),
        },
        "observed_at_ms": {"state": "known", "value": observed_at_ms},
        "received_at_ms": observed_at_ms,
        "ttl_ms": {"state": "known", "value": ttl_ms},
        "reset_at_ms": ({"state": "unknown", "reason": "provider-reset-unavailable"}
                         if reset_at_ms is None else {"state": "known", "value": reset_at_ms}),
        "source": {
            "source_id": source_id,
            "source_schema": source_schema,
            "adapter_version": "cortex-quota-adapter-836-v1",
            "authority_ref": authority_ref,
            "method": "structured_event" if executor == "claude" else "provider_status",
            "provenance_refs": [authority_ref],
            "event_identity": {"state": "unknown", "reason": "source-event-id-unavailable"},
        },
        "coverage": {"state": "partial", "gaps": [{"scope": resource_key, "reason": reason}]}
        if reason else {"state": "complete", "gaps": []},
    }
    return schema.parse_observation(payload, descriptors=descriptors, unit_catalog=unit_catalog)


def _parse_iso_epoch_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    epoch_ms = int(parsed.timestamp() * 1000)
    return epoch_ms if 0 <= epoch_ms <= _TIME_MAX_MS else None


def _decimal_wire(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
