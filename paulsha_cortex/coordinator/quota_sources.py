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
_CODEX_UNIT = "provider:openai-codex-app-server/rate-limit-percent/v2"
_COPILOT_UNIT = "provider:github-copilot-sdk/requests/v1"
_AGY_UNIT = "provider:google-antigravity-cli/quota-fraction/v1"
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
            "unit_semantics_ref": _CODEX_UNIT,
        },
        "copilot": {
            "state": "supported",
            "method": "account.getQuota",
            "source_schema": "github-copilot-sdk-quota-v1",
            "authority_ref": "https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/usage-and-billing",
            "unit_semantics_ref": _COPILOT_UNIT,
        },
        "agy": {
            "state": "supported",
            "argv": ("agy", "-p", "/usage", "--output-format", "json"),
            "source_schema": "google-antigravity-cli-usage-v1",
            "authority_ref": "https://antigravity.google/docs/cli/commands/usage",
            "unit_semantics_ref": _AGY_UNIT,
        },
        "claude": {
            "state": "unsupported",
            "reason": "no-documented-machine-readable-quota-remaining-interface",
        },
        "cg": {"state": "unknown", "reason": "no-verified-quota-read-contract"},
    }
    return dict(contracts.get(executor, {"state": "unknown", "reason": "unknown-executor"}))


def capture_provider_quota(
    executor: str,
    payload: object,
    *,
    profile_key: str,
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
            executor, targets, profile_key, observed_at_ms, ttl_ms, reason, descriptors, unit_catalog
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


def _validate_targets(targets, *, profile_key, descriptors, unit_catalog):
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
        if status.get("state") != "complete" or not _binding_has_profile(binding, profile_key):
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


def _binding_has_profile(binding: dict[str, Any], profile_key: str) -> bool:
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
    return {}


def _parse_codex(payload: dict[str, Any], targets):
    body = payload.get("result", payload)
    if not isinstance(body, dict):
        return {}
    limits = body.get("rateLimits", body.get("rate_limits", body))
    if not isinstance(limits, dict):
        return {}
    limit_id = limits.get("limitId", limits.get("limit_id"))
    if not isinstance(limit_id, str) or not limit_id:
        return {}
    result = {}
    for target in targets:
        prefix = f"codex:{limit_id}:"
        if not target.resource_key.startswith(prefix):
            continue
        window_name = target.resource_key[len(prefix):]
        window = limits.get(window_name)
        if not isinstance(window, dict):
            continue
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
            result[target.resource_key] = (None, None, "invalid-provider-value")
            continue
        if _target_semantics(target) != _CODEX_UNIT:
            result[target.resource_key] = (None, None, "provider-unit-mapping-mismatch")
            continue
        reset_at_ms = None if reset_seconds is None else reset_seconds * 1000
        result[target.resource_key] = (str(100 - used), reset_at_ms, None)
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
        if _target_semantics(target) != _COPILOT_UNIT:
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
        if _target_semantics(target) != _AGY_UNIT:
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


def _unknown_capture(executor, targets, profile_key, observed_at_ms, ttl_ms, reason, descriptors, unit_catalog):
    observations: list[schema.QuotaObservation] = []
    gaps: list[CoverageGap] = []
    try:
        indexed = _validate_targets(targets, profile_key=profile_key,
                                    descriptors=descriptors, unit_catalog=unit_catalog)
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
            "method": "provider_status",
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
