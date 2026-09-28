"""#836 quota source/usage 的唯讀 shadow reconciliation。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any

from . import quota_observation as schema
from .quota_ledger import (
    LedgerAppendResult, LedgerCorrupt, QuotaEventLedger,
    _cross_identity_conflict_digest, _is_terminal_usage_observation,
    _snapshot_has_known_window_epoch,
)
from .quota_sources import (
    CoverageGap, ProviderCapture, ProviderQuotaTarget, capture_provider_quota, _parse_iso_epoch_ms,
)


_PROFILE_KEY_RE = re.compile(r"epk:v1:resolved:[0-9a-f]{64}\Z")
_USAGE_METRICS = frozenset((
    "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens",
))
_MAX_SHADOW_CLOCK_SKEW_MS = 0


@dataclass(frozen=True)
class ShadowRecordResult:
    status: str
    accepted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    gaps: tuple[CoverageGap, ...] = ()


class _MemoryLedger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append_observation(self, observation, *, idempotency_key=None, associations=(),
                            terminal_job_started_at_ms=None, terminal_job_finished_at_ms=None):
        from .quota_ledger import (
            _canonical_bytes, _idempotency_key as make_key,
            _observation_digest_payload, _same_snapshot_value,
            _snapshot_has_known_window_epoch, _terminal_usage_metadata,
        )

        wire = observation.to_dict()
        key = "caller:" + idempotency_key if idempotency_key else make_key(observation, wire, None)
        normalized = list(associations)
        # 與持久化 ledger 對齊：終局 usage 的起訖原始事實一併納入 digest，
        # 避免記憶體版 ledger 在測試中與持久化版本的行為分歧。
        terminal_metadata = _terminal_usage_metadata(
            wire, terminal_job_started_at_ms, terminal_job_finished_at_ms
        )
        digest_payload = _observation_digest_payload(wire, normalized, terminal_metadata)
        digest = hashlib.sha256(_canonical_bytes(digest_payload)).hexdigest()
        previous = [row for row in self.events if row.get("idempotency_key") == key
                    and row.get("kind") == "observation"]
        prior = previous[-1] if previous else None
        conflict = next((row for row in self.events if row.get("idempotency_key") == key
                         and row.get("kind") == "conflict"), None)
        if conflict:
            return LedgerAppendResult("conflict", conflicts=1, idempotency_key=key)
        if prior:
            if any(row.get("payload_sha256") == digest for row in previous):
                return LedgerAppendResult("duplicate", duplicates=1, idempotency_key=key)
            same_value = [row for row in self.events
                          if row.get("idempotency_key") == key
                          and row.get("kind") == "observation"
                          and _same_snapshot_value(row.get("observation"), wire)]
            if same_value:
                if (_snapshot_has_known_window_epoch(wire)
                        and not any(_snapshot_has_known_window_epoch(
                            row.get("observation")
                        ) for row in same_value)):
                    row = {"schema_version": 1, "kind": "observation", "idempotency_key": key,
                           "payload_sha256": digest, "observation": wire}
                    row.update(terminal_metadata)
                    if normalized:
                        row["associations"] = normalized
                    self.events.append(row)
                    return LedgerAppendResult("accepted", accepted=1, idempotency_key=key)
                return LedgerAppendResult("duplicate", duplicates=1, idempotency_key=key)
            self.events.append({
                "schema_version": 1, "kind": "conflict", "idempotency_key": key,
                "existing_sha256": prior["payload_sha256"], "incoming_sha256": digest,
                "scope": _scope_summary(wire), "observed_at_ms": _known_value(wire.get("observed_at_ms")),
                "window_id": _scope_summary(wire).get("window_id"), "associations": normalized,
            })
            return LedgerAppendResult("conflict", conflicts=1, idempotency_key=key)
        # 與持久化 ledger 對齊：即使這筆的 idempotency key 與既有事件都不同，
        # 仍以語意範圍（pool／window／observed_at／metric）尋找衝突（見 #836
        # 對抗審查第七輪 MAJOR quota_ledger.py:274）。
        cross_digest = _cross_identity_conflict_digest(self.events, wire, digest)
        if cross_digest is not None:
            self.events.append({
                "schema_version": 1, "kind": "conflict", "idempotency_key": key,
                "existing_sha256": cross_digest, "incoming_sha256": digest,
                "scope": _scope_summary(wire), "observed_at_ms": _known_value(wire.get("observed_at_ms")),
                "window_id": _scope_summary(wire).get("window_id"), "associations": normalized,
            })
            return LedgerAppendResult("conflict", conflicts=1, idempotency_key=key)
        row = {"schema_version": 1, "kind": "observation", "idempotency_key": key,
               "payload_sha256": digest, "observation": wire}
        row.update(terminal_metadata)
        if normalized:
            row["associations"] = normalized
        self.events.append(row)
        return LedgerAppendResult("accepted", accepted=1, idempotency_key=key)

    def read(self):
        from .quota_ledger import LedgerSnapshot

        return LedgerSnapshot(tuple(dict(row) for row in self.events))


class QuotaShadowService:
    """觀測 provider 與 terminal usage，不含 reservation 或 dispatch API。"""

    def __init__(self, ledger: QuotaEventLedger | _MemoryLedger | None = None) -> None:
        self.ledger = ledger if ledger is not None else QuotaEventLedger()

    @classmethod
    def in_memory(cls) -> "QuotaShadowService":
        return cls(_MemoryLedger())

    def record_observation(
        self,
        payload: object,
        *,
        descriptors: tuple[schema.PoolDescriptor, ...],
        unit_catalog: tuple[schema.UnitDefinition, ...],
        idempotency_key: str | None = None,
    ) -> ShadowRecordResult:
        try:
            observation = payload if isinstance(payload, schema.QuotaObservation) else schema.parse_observation(
                payload, descriptors=descriptors, unit_catalog=unit_catalog
            )
            result = self.ledger.append_observation(observation, idempotency_key=idempotency_key)
        except LedgerCorrupt:
            return ShadowRecordResult("unknown", gaps=(CoverageGap("quota-observation", "ledger-corrupt"),))
        except (schema.QuotaContractError, TypeError, ValueError, OSError):
            return ShadowRecordResult("invalid", gaps=(CoverageGap("quota-observation", "invalid-or-unwritable-observation"),))
        return ShadowRecordResult(
            result.status, result.accepted, result.duplicates, result.conflicts,
        )

    def record_provider_read(
        self,
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
        capture = capture_provider_quota(
            executor, payload, profile_key=profile_key, targets=targets,
            descriptors=descriptors, unit_catalog=unit_catalog,
            observed_at_ms=observed_at_ms, ttl_ms=ttl_ms,
        )
        try:
            for observation in capture.observations:
                self.ledger.append_observation(observation)
        except (LedgerCorrupt, OSError, ValueError):
            return ProviderCapture(
                capture.executor, capture.observations,
                capture.gaps + tuple(CoverageGap(target.resource_key, "ledger-corrupt")
                                     for target in targets),
            )
        return capture

    def record_external_observation(
        self,
        payload: object,
        *,
        descriptors: tuple[schema.PoolDescriptor, ...],
        unit_catalog: tuple[schema.UnitDefinition, ...],
        idempotency_key: str | None = None,
    ) -> ShadowRecordResult:
        """匯入呼叫端已唯讀取得的外部 session/host observation。

        此 consumer 不連線、不讀 credential，也不接受會造成 provider 狀態變更的命令；
        尚未被唯讀觀察覆蓋的外部 session 仍保留 projection gap。
        """
        try:
            observation = schema.parse_observation(
                payload, descriptors=descriptors, unit_catalog=unit_catalog,
            )
        except (schema.QuotaContractError, TypeError, ValueError):
            return ShadowRecordResult(
                "invalid", gaps=(CoverageGap("external-observation", "invalid-external-observation"),)
            )
        method = observation.to_dict().get("source", {}).get("method")
        if method not in {"provider_status", "structured_event"}:
            return ShadowRecordResult(
                "invalid", gaps=(CoverageGap("external-observation", "external-source-not-read-only"),)
            )
        try:
            result = self.ledger.append_observation(
                observation, idempotency_key=idempotency_key,
            )
        except (LedgerCorrupt, OSError, ValueError):
            return ShadowRecordResult(
                "unknown", gaps=(CoverageGap("external-observation", "ledger-corrupt"),)
            )
        return ShadowRecordResult(result.status, result.accepted, result.duplicates, result.conflicts)

    def record_terminal_usage(
        self,
        job: dict[str, Any],
        *,
        profile_key: str,
        binding: schema.ProfilePoolBinding,
        descriptors: tuple[schema.PoolDescriptor, ...],
        unit_catalog: tuple[schema.UnitDefinition, ...],
        unit_ref_by_metric: dict[str, tuple[str, str]],
        observed_at_ms: int,
    ) -> ShadowRecordResult:
        gaps: list[CoverageGap] = []
        if (not isinstance(job, dict) or not isinstance(profile_key, str)
                or not _PROFILE_KEY_RE.fullmatch(profile_key)):
            return ShadowRecordResult("invalid", gaps=(CoverageGap("terminal-usage", "profile-binding-unresolved"),))
        # #1116：executor＋model_id 穩定 identity subject binding 也要能在
        # 這裡涵蓋這個 job 的終局 usage，否則 admission 用 identity binding
        # 判定可行、真正扣消耗時卻只認得 profile／group 兩種 kind，會讓
        # identity 綁定的 pool 永遠扣不到終局 usage（見呼叫端
        # `manager._quota_admission_record_terminal_usage` 文件字串）。job
        # 缺 executor／model_id（非 str）時 identity 傳 `None`，效果等同
        # #1116 之前——只比對 profile／group 兩種 kind。
        job_executor = job.get("executor")
        job_model_id = job.get("model_id")
        identity = (
            (job_executor, job_model_id)
            if isinstance(job_executor, str) and isinstance(job_model_id, str)
            else None
        )
        if (schema.binding_status(binding).get("state") != "complete"
                or not _binding_has_profile(binding.to_dict(), profile_key, identity=identity)):
            return ShadowRecordResult("invalid", gaps=(CoverageGap("terminal-usage", "profile-binding-unresolved"),))
        job_id = job.get("id")
        executor = job.get("executor")
        usage = job.get("usage")
        if (not isinstance(job_id, str) or not job_id or len(job_id) > 128
                or not isinstance(executor, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", executor)
                or not isinstance(usage, dict) or type(observed_at_ms) is not int
                or observed_at_ms < 0 or observed_at_ms > 253402300799999):
            return ShadowRecordResult("invalid", gaps=(CoverageGap("terminal-usage", "terminal-usage-shape-invalid"),))
        if not any(metric in usage for metric in _USAGE_METRICS):
            return ShadowRecordResult("unknown", gaps=(CoverageGap("terminal-usage", "usage-fields-unavailable"),))
        constraints = _binding_constraints(binding.to_dict(), descriptors)
        if not constraints:
            return ShadowRecordResult("unknown", gaps=(CoverageGap("terminal-usage", "binding-scope-unresolved"),))

        # job 開始時間隨終局 usage 一併存進 ledger：投影階段要靠它判定這筆 usage
        # 是否整段落在 snapshot 之後，才可安全整筆扣減；解析不出時視為未知，
        # 不得因此擋下這筆終局 usage 的記錄（仍先接受，投影時保守處理）。
        terminal_job_started_at_ms = _parse_iso_epoch_ms(job.get("started_at"))
        # job 結束時間（若有）用來與 started_at 一起圈出這筆 usage 實際落在
        # 哪一段 window instance；job 未帶 finished_at 時，以呼叫端傳入的
        # observed_at_ms（記錄當下，必然不早於實際結束時間）作保守替代上界。
        terminal_job_finished_at_ms = _parse_iso_epoch_ms(job.get("finished_at"))

        accepted = duplicates = conflicts = 0
        unit_map: dict[tuple[str, str], schema.UnitDefinition] = {}
        for descriptor in descriptors:
            for unit in descriptor.units:
                unit_map[unit.ref] = unit
        for unit in unit_catalog:
            unit_map.setdefault(unit.ref, unit)
        for metric in sorted(_USAGE_METRICS):
            if metric not in unit_ref_by_metric:
                if usage.get(metric) is not None:
                    gaps.append(CoverageGap(metric, "usage-unit-mapping-missing"))
                continue
            unit_ref = tuple(unit_ref_by_metric[metric])
            unit = unit_map.get(unit_ref)
            if unit is None:
                gaps.append(CoverageGap(metric, "usage-unit-unknown"))
                continue
            raw_amount = usage.get(metric)
            quantity: dict[str, Any]
            value: int | None = (
                raw_amount if type(raw_amount) is int and raw_amount >= 0 and len(str(raw_amount)) <= 36
                else None
            )
            if value is None:
                reason = "missing-usage-value" if raw_amount is None else "invalid-usage-value"
                quantity = {"state": "unknown", "reason": reason}
                gaps.append(CoverageGap(metric, reason))
            else:
                quantity = {"state": "observed", "amount": {"kind": "exact", "value": str(value)}}

            # 逐個 constraint（每個 pool/window）各自判定 unit 是否可比對，不能
            # 整批依「是否存在任何可比對 constraint」決定。同一 binding 同時
            # 含可比對 unit 的 pool（例如 tokens）與不可換算 unit 的 pool
            # （premium credit／request）時，先前只要有任一 constraint 命中
            # same_unit 就整批視為「已比對」，導致不可換算的那個 pool 完全
            # 拿不到任何證據、既有 snapshot 的餘額被當確定值（見 #836 對抗
            # 審查第六輪 MAJOR quota_shadow.py:251）。
            not_comparable_gap_emitted = False
            for constraint in constraints:
                comparable = constraint["unit_ref"] == unit_ref
                if comparable:
                    scope = {"state": "known", "value": constraint["scope"]}
                    observation_associations: tuple[dict[str, Any], ...] = ()
                else:
                    # 不同 unit 的 usage 仍保留原生 provenance，以已驗證 binding
                    # 關聯標示「有耗用但不可換算」，不改寫成 subscription
                    # credit；每個不可換算的 pool 都各自留下 association 證據，
                    # 一個 metric 的 gap 只回報一次即可。
                    scope = {"state": "unknown", "reason": "usage-pool-unit-not-comparable"}
                    observation_associations = (constraint["association"],)
                    if not not_comparable_gap_emitted:
                        gaps.append(CoverageGap(metric, "usage-unit-not-comparable"))
                        not_comparable_gap_emitted = True
                # 終局 usage 只記錄原始事實（job 起訖時間，見上方
                # terminal_job_started_at_ms／terminal_job_finished_at_ms），
                # 不在記錄當下依 ledger 現況推導並持久化 window instance：
                # window 歸屬改到 project() 時依當下 ledger 內的 snapshot 動態
                # 判定。記錄當下推導會讓 observation identity 隨 ledger 狀態
                # 變動——同一 job 重播時若 ledger 內容已不同（例如中途補進了
                # snapshot），payload 就會改變並被誤判為 conflict；而且「先記
                # usage、涵蓋同一 window 的較舊 snapshot 之後才補進」的情境會
                # 永遠卡在 unknown，shadow 永不收斂（見 #836 對抗審查第五輪
                # MAJOR quota_shadow.py:273）。
                window_instance = dict(_TERMINAL_USAGE_WINDOW_INSTANCE_UNKNOWN)
                try:
                    observation = _usage_observation(
                        job_id=job_id, executor=executor, metric=metric, profile_key=profile_key,
                        unit=unit, unit_ref=unit_ref, scope=scope, quantity=quantity,
                        observed_at_ms=observed_at_ms, descriptors=descriptors,
                        unit_catalog=unit_catalog, window_instance=window_instance,
                    )
                except (schema.QuotaContractError, TypeError, ValueError):
                    gaps.append(CoverageGap(metric, "usage-observation-invalid"))
                    continue
                # replay key 納入 pool identity（含 revision）：同一 binding 把
                # 同 metric/unit 映到兩個 pool、且 window_id 恰好同名時（例如
                # shared-account 多角色都叫 month），不得因 window_id 相同而互相
                # 撞成 conflict（見 #836 對抗審查第四輪 MAJOR）。每個 constraint
                # 本身的 pool_ref/window_id 已足夠穩定區分，可比對與不可換算
                # 兩種情形共用同一套 key 規則。
                #
                # key 不得納入 profile_key：終局 usage 的去重 identity 只認
                # (job_id, pool identity, metric, window 範圍)。同一個
                # terminal job 若因 group binding／profile alias replay，
                # 先後以兩個不同的 profile_key 記到同一個 shared pool/window
                # （constraint 的 pool_ref/window_id 本身就已相同），只要
                # profile_key 還留在 key 裡，兩筆 key 就永遠不同，導致兩筆
                # usage 都被接受、對同一 pool 雙重扣減。改成不含 profile_key
                # 後，同一 job/pool/metric/window 的重播一律撞上同一個 key，
                # 交給既有的 duplicate／conflict 判定（配合下方 digest 已排除
                # profile_ref／observation_id 的差異）（見 #836 對抗審查第七輪
                # MAJOR quota_shadow.py:307）。
                pool_component = hashlib.sha256(
                    json.dumps(
                        constraint["scope"]["pool_ref"], sort_keys=True, separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:16]
                key = (
                    f"terminal-usage:v1:{job_id}:{metric}:{pool_component}:"
                    + constraint["window_id"]
                )
                result = self.ledger.append_observation(
                    observation,
                    idempotency_key=key,
                    associations=observation_associations,
                    terminal_job_started_at_ms=terminal_job_started_at_ms,
                    terminal_job_finished_at_ms=terminal_job_finished_at_ms,
                )
                accepted += result.accepted
                duplicates += result.duplicates
                conflicts += result.conflicts
                if result.status == "conflict":
                    gaps.append(CoverageGap(metric, "terminal-usage-source-conflict"))
        status = "conflict" if conflicts else "accepted" if accepted else "duplicate" if duplicates else "unknown"
        return ShadowRecordResult(status, accepted, duplicates, conflicts, tuple(gaps))

    def project(
        self,
        *,
        descriptors: tuple[schema.PoolDescriptor, ...],
        unit_catalog: tuple[schema.UnitDefinition, ...],
        now_utc_ms: int,
        demand_by_window: dict[tuple[tuple[str, str, str, str], str], str] | None = None,
    ) -> dict[str, Any]:
        rows = _empty_pool_rows(descriptors)
        if type(now_utc_ms) is not int or now_utc_ms < 0 or now_utc_ms > 253402300799999:
            for row in rows:
                row["remaining"] = _unknown("invalid-projection-time")
                row["coverage_gaps"] = ["invalid-projection-time"]
            return {
                "state": "unknown", "mode": "shadow", "dispatch_effect": "none",
                "pools": rows, "events": [], "coverage_gaps": ["invalid-projection-time"],
            }
        try:
            snapshot = self.ledger.read()
            records = snapshot.events
        except (LedgerCorrupt, OSError):
            # ledger 檔案本身結構損毀（讀取/驗證失敗）：此時無法信任任何一筆
            # 事件，整份投影標 unknown 是唯一安全的做法。
            for row in rows:
                row["coverage_gaps"].append("ledger-corrupt")
            return {
                "state": "unknown", "mode": "shadow", "dispatch_effect": "none",
                "pools": rows, "events": [], "coverage_gaps": ["ledger-corrupt"],
            }

        # ledger 檔案結構完好，但個別事件仍可能因為呼叫端目前傳入的
        # descriptors/unit_catalog 而解析失敗——最常見的情況是 pool 已升版，
        # 呼叫端只帶新 revision，ledger 內舊 revision 的事件因此對不上任何
        # 描述集。這只是「目前解不開這一筆」，不代表整份 ledger 損毀，因此
        # 逐筆 parse、逐筆處理失敗，只讓對應的 pool 標記，不毒化其他 pool
        # （見 #836 對抗審查第四輪 MAJOR：舊 revision 事件不得讓整份 shadow
        # projection 都變成 ledger-corrupt）。
        parsed_records: list[tuple[dict[str, Any], schema.QuotaObservation]] = []
        # usage_delta 事件描述的是實際發生過的耗用；即使目前的
        # unit／pool／revision 描述集認不出某一筆 usage 事件（例如透過
        # association 指向 credit pool 的不可換算 usage，之後又以缺少該
        # usage unit 的舊版／rollback descriptor 投影），也不得整筆丟棄——
        # 這裡先收集受影響的 pool／window key，稍後強制轉為 unknown（見 #836
        # 對抗審查第七輪 MAJOR quota_shadow.py:791）。
        unresolved_usage_keys: set[tuple[tuple[str, str, str, str], str]] = set()
        for record in records:
            if record.get("kind") != "observation":
                continue
            try:
                observation = schema.parse_observation(
                    record.get("observation"), descriptors=descriptors, unit_catalog=unit_catalog,
                )
            except (TypeError, ValueError, schema.QuotaContractError):
                _mark_unresolved_record(record, rows, unresolved_usage_keys)
                continue
            parsed_records.append((record, observation))

        conflicts: dict[tuple[tuple[str, str, str, str], str], int] = {}
        for record in records:
            if record.get("kind") != "conflict":
                continue
            scope = record.get("scope")
            time_value = record.get("observed_at_ms")
            conflict_time = time_value if type(time_value) is int else 2**63 - 1
            conflict_targets: list[tuple[tuple[str, str, str, str], str]] = []
            if isinstance(scope, dict) and scope.get("state") == "known":
                pool = scope.get("pool_ref")
                if isinstance(pool, dict):
                    conflict_targets.append((_pool_key(pool), str(scope.get("window_id"))))
            for association in record.get("associations", []):
                pool = association.get("pool_ref") if isinstance(association, dict) else None
                if isinstance(pool, dict):
                    conflict_targets.append((_pool_key(pool), str(association.get("window_id"))))
            for key in conflict_targets:
                conflicts[key] = max(conflicts.get(key, -1), conflict_time)

        events = [_public_event(record, observation) for record, observation in parsed_records]
        gaps: set[str] = set()
        parsed_by_scope: dict[tuple[tuple[str, str, str, str], str], list[tuple[dict[str, Any], schema.QuotaObservation]]] = {}
        usage_by_scope: dict[tuple[tuple[str, str, str, str], str], list[tuple[dict[str, Any], schema.QuotaObservation]]] = {}
        for record, observation in parsed_records:
            wire = observation.to_dict()
            scope = wire.get("scope", {})
            value = scope.get("value") if isinstance(scope, dict) and scope.get("state") == "known" else None
            measurement = wire.get("measurement", {})
            kind = measurement.get("kind") if isinstance(measurement, dict) else None
            targets: list[tuple[tuple[str, str, str, str], str]] = []
            if isinstance(value, dict) and isinstance(value.get("pool_ref"), dict):
                targets.append((_pool_key(value["pool_ref"]), str(value.get("window_id"))))
            else:
                for association in record.get("associations", []):
                    pool = association.get("pool_ref") if isinstance(association, dict) else None
                    if isinstance(pool, dict):
                        targets.append((_pool_key(pool), str(association.get("window_id"))))
            for key in targets:
                if key not in rows_by_key(rows):
                    continue
                target = usage_by_scope if kind == "usage_delta" else parsed_by_scope
                target.setdefault(key, []).append((record, observation))

        for row in rows:
            key = (_pool_key(row["pool_ref"]), row["window_id"])
            samples = parsed_by_scope.get(key, [])
            if row.get("quantity_kind") == "gauge":
                gauge_candidates = []
                for record, observation in samples:
                    wire = observation.to_dict()
                    measurement = wire.get("measurement", {})
                    if not isinstance(measurement, dict) or measurement.get("kind") != "gauge_snapshot":
                        continue
                    observed_at = _known_value(wire.get("observed_at_ms"))
                    if type(observed_at) is not int or observed_at > now_utc_ms:
                        continue
                    freshness = schema.freshness(
                        observation, now_utc_ms=now_utc_ms,
                        allowed_clock_skew_ms=_MAX_SHADOW_CLOCK_SKEW_MS,
                    )
                    if freshness.get("state") == "fresh":
                        gauge_candidates.append((observed_at, observation))
                latest_gauge = max(gauge_candidates, key=lambda item: (item[0], item[1].observation_id), default=None)
                row["gauge"] = (
                    _copy_quantity(latest_gauge[1].to_dict()["measurement"]["quantity"])
                    if latest_gauge is not None else _unknown("missing-gauge-snapshot")
                )
                row["remaining"] = _unknown("non-remaining-unit")
                row["coverage_gaps"].append("non-remaining-unit")
                row["coverage_gaps"].append("external-session-unobserved")
                row["assessment"] = "unknown"
                row["coverage_gaps"] = sorted(set(row["coverage_gaps"]))
                continue
            snapshot_candidates = []
            stale_seen = False
            clock_rollback = False
            for record, observation in samples:
                wire = observation.to_dict()
                measurement = wire.get("measurement", {})
                if not isinstance(measurement, dict) or measurement.get("kind") != "remaining_snapshot":
                    continue
                observed_at = _known_value(wire.get("observed_at_ms"))
                if type(observed_at) is int and observed_at > now_utc_ms:
                    clock_rollback = True
                    continue
                freshness = schema.freshness(
                    observation, now_utc_ms=now_utc_ms,
                    allowed_clock_skew_ms=_MAX_SHADOW_CLOCK_SKEW_MS,
                )
                if freshness.get("state") == "stale":
                    stale_seen = True
                    continue
                if freshness.get("state") != "fresh":
                    continue
                snapshot_candidates.append((
                    observed_at if type(observed_at) is int else -1,
                    _snapshot_has_known_window_epoch(wire),
                    observation.observation_id,
                    record,
                    observation,
                ))
            selected = max(snapshot_candidates, key=lambda item: item[:3], default=None)
            conflict_at = conflicts.get(key)
            if clock_rollback:
                row["remaining"] = _unknown("clock-rollback")
                row["coverage_gaps"].append("clock-rollback")
            elif selected is None:
                row["remaining"] = _unknown("stale-snapshot" if stale_seen else "missing-snapshot")
                row["coverage_gaps"].append("stale-snapshot" if stale_seen else "missing-snapshot")
                if usage_by_scope.get(key):
                    window_unit = tuple(row["unit_ref"].get(field) for field in ("unit_id", "version"))
                    if any(_known_value(item[1].to_dict().get("unit_ref")) != {
                        "unit_id": window_unit[0], "version": window_unit[1]
                    } for item in usage_by_scope[key]):
                        row["remaining"] = _unknown("usage-unit-not-comparable")
                        row["coverage_gaps"].append("usage-unit-not-comparable")
            elif conflict_at is not None and conflict_at >= selected[0]:
                row["remaining"] = _unknown("source-conflict")
                row["coverage_gaps"].append("source-conflict")
            else:
                _, _, _, selected_record, selected_observation = selected
                selected_wire = selected_observation.to_dict()
                quantity = selected_wire["measurement"]["quantity"]
                row["remaining"] = _copy_quantity(quantity)
                if quantity.get("state") == "unknown":
                    row["coverage_gaps"].append(str(quantity.get("reason", "remaining-unknown")))
                self._apply_usage(row, key, selected_record, selected_observation,
                                  usage_by_scope.get(key, []), now_utc_ms)
            row["coverage_gaps"].append("external-session-unobserved")
            row["coverage_gaps"] = sorted(set(row["coverage_gaps"]))
            demand = None if demand_by_window is None else demand_by_window.get(key)
            row["assessment"] = _assess(row["remaining"], demand)
            if row["assessment"] == "unknown" and demand is not None:
                row["coverage_gaps"].append("demand-or-remaining-unknown")
                row["coverage_gaps"] = sorted(set(row["coverage_gaps"]))
        # 有任何一筆 usage 事件因目前描述集無法解讀而被 _mark_unresolved_record
        # 標記為受影響的 pool，一律強制轉為 unknown：這筆看不懂的耗用可能已經
        # 發生但無法確認金額，不能讓上面依其他有效 snapshot 算出的「確定」餘額
        # 蓋過這個缺口（見 #836 對抗審查第七輪 MAJOR quota_shadow.py:791）。
        for row in rows:
            row_key = (_pool_key(row["pool_ref"]), row["window_id"])
            if row_key in unresolved_usage_keys:
                row["remaining"] = _unknown("usage-unresolvable")
                row["assessment"] = "unknown"
                if "usage-unresolvable" not in row["coverage_gaps"]:
                    row["coverage_gaps"].append("usage-unresolvable")
                row["coverage_gaps"] = sorted(set(row["coverage_gaps"]))
        overall_gaps = sorted({gap for row in rows for gap in row["coverage_gaps"]})
        return {
            "state": "known" if rows and all(row["remaining"].get("state") != "unknown" for row in rows) else "unknown",
            "mode": "shadow",
            "dispatch_effect": "none",
            "pools": rows,
            "events": events,
            "coverage_gaps": overall_gaps,
        }

    @staticmethod
    def _apply_usage(row, key, selected_record, selected_observation, usage_records, now_utc_ms):
        quantity = row["remaining"]
        if quantity.get("state") != "observed":
            return
        baseline = _known_value(selected_observation.to_dict().get("observed_at_ms"))
        baseline_unit = _known_value(selected_observation.to_dict().get("unit_ref"))
        baseline_window = selected_observation.to_dict().get("window_instance")
        baseline_epoch = (
            baseline_window.get("epoch")
            if isinstance(baseline_window, dict) and baseline_window.get("kind") == "interval"
            else None
        )
        baseline_epoch_known = (
            isinstance(baseline_epoch, dict) and baseline_epoch.get("state") == "known"
        )
        base_amount = quantity.get("amount", {})
        if not isinstance(base_amount, dict):
            return
        if base_amount.get("kind") == "exact":
            try:
                base_lower = base_upper = Decimal(base_amount["value"])
            except (KeyError, InvalidOperation, TypeError):
                row["remaining"] = _unknown("remaining-amount-invalid")
                row["coverage_gaps"].append("remaining-amount-invalid")
                return
        elif base_amount.get("kind") == "bounds":
            try:
                base_lower = Decimal(base_amount["lower"]) if base_amount.get("lower") is not None else None
                base_upper = Decimal(base_amount["upper"]) if base_amount.get("upper") is not None else None
            except (InvalidOperation, TypeError):
                row["remaining"] = _unknown("remaining-amount-invalid")
                row["coverage_gaps"].append("remaining-amount-invalid")
                return
        else:
            return
        deducted = Decimal(0)
        invalidated: str | None = None
        for record, observation in usage_records:
            wire = observation.to_dict()
            measured_at = _known_value(wire.get("observed_at_ms"))
            if type(measured_at) is not int or (type(baseline) is int and measured_at <= baseline):
                continue
            if measured_at > now_utc_ms:
                invalidated = "clock-rollback"
                continue
            is_terminal_usage = _is_terminal_usage_observation(wire)
            started_at = finished_at = None
            if is_terminal_usage:
                # 終局 usage 只保存原始事實（job 起訖時間），window 歸屬與
                # 是否已被 snapshot 反映一律在這裡依當下已知的 snapshot 動態
                # 推導，不讀取 wire 內的 window_instance（該欄位對終局 usage
                # 一律是 unknown；見 #836 對抗審查第五輪 MAJOR
                # quota_shadow.py:273）。
                started_at = record.get("terminal_job_started_at_ms") if isinstance(record, dict) else None
                finished_at = record.get("terminal_job_finished_at_ms") if isinstance(record, dict) else None
                if (type(finished_at) is int and type(baseline) is int
                        and finished_at <= baseline):
                    # 整個 job 早在這張 snapshot 之前就已結束，snapshot 的
                    # 餘額已經反映過這筆消耗；即使因重啟才在 snapshot 之後
                    # 重播寫入 ledger（measured_at 較晚），也不得再次扣減，
                    # 也不得判為 straddling 而毒化餘額（見 #836 對抗審查第
                    # 五輪 MAJOR quota_shadow.py:567）。
                    continue
            measurement = wire.get("measurement", {})
            if not isinstance(measurement, dict) or measurement.get("kind") != "usage_delta":
                continue
            usage_unit = _known_value(wire.get("unit_ref"))
            if usage_unit != baseline_unit:
                invalidated = "usage-unit-not-comparable"
                continue
            if not baseline_epoch_known:
                invalidated = "window-epoch-unknown"
                continue
            if is_terminal_usage:
                # job 起訖時間（缺 finished_at 時以這筆 usage 實際寫入 ledger
                # 的時間 measured_at 當保守上界——終局 usage 必然在被記錄之前
                # 就已結束）整段落在同一已知 window 內，才有證據可以安全整筆
                # 扣減；已知開始時間早於 snapshot（跨越 snapshot）沒有可切分
                # 的增量證據，一律視為無法切分，不整筆扣除也不忽略；其餘
                # （起訖時間不足以判定、或跨窗口）一律 unknown。
                effective_finished_at = finished_at if type(finished_at) is int else measured_at
                fits_known_window = (
                    type(started_at) is int and type(baseline) is int and started_at >= baseline
                    and isinstance(baseline_window, dict) and baseline_window.get("kind") == "interval"
                    and type(baseline_window.get("start_ms")) is int
                    and type(baseline_window.get("end_ms")) is int
                    and baseline_window["start_ms"] <= started_at
                    and type(effective_finished_at) is int
                    and effective_finished_at <= baseline_window["end_ms"]
                )
                if not fits_known_window:
                    if type(started_at) is int and type(baseline) is int and started_at < baseline:
                        invalidated = "straddling-usage"
                    else:
                        invalidated = "usage-window-unresolved"
                    continue
            else:
                usage_window = wire.get("window_instance")
                if isinstance(baseline_window, dict) and baseline_window.get("kind") == "interval":
                    if not isinstance(usage_window, dict) or usage_window.get("kind") != "interval" or usage_window != baseline_window:
                        invalidated = "usage-window-unresolved"
                        continue
            usage_quantity = measurement.get("quantity", {})
            if not isinstance(usage_quantity, dict) or usage_quantity.get("state") != "observed":
                invalidated = "usage-quantity-unknown"
                continue
            amount = usage_quantity.get("amount", {})
            if not isinstance(amount, dict) or amount.get("kind") != "exact":
                invalidated = "usage-quantity-not-exact"
                continue
            try:
                value = Decimal(amount.get("value"))
            except (InvalidOperation, TypeError):
                invalidated = "usage-quantity-invalid"
                continue
            if value < 0 or not value.is_finite():
                invalidated = "usage-quantity-invalid"
                continue
            deducted += value
        if invalidated is not None:
            row["remaining"] = _unknown(invalidated)
            row["coverage_gaps"].append(invalidated)
            return
        if base_upper is not None and base_upper < deducted:
            row["remaining"] = _unknown("usage-exceeds-observed-remaining")
            row["coverage_gaps"].append("usage-exceeds-observed-remaining")
            return
        if base_lower is not None and base_lower == base_upper:
            remaining = base_lower - deducted
            if remaining < 0:
                row["remaining"] = _unknown("usage-exceeds-observed-remaining")
                row["coverage_gaps"].append("usage-exceeds-observed-remaining")
                return
            result_amount = {"kind": "exact", "value": _decimal_wire(remaining)}
        else:
            lower = max(Decimal(0), base_lower - deducted) if base_lower is not None else None
            upper = base_upper - deducted if base_upper is not None else None
            if lower is not None and upper is not None and lower == upper:
                result_amount = {"kind": "exact", "value": _decimal_wire(lower)}
            else:
                result_amount = {
                    "kind": "bounds",
                    "lower": None if lower is None else _decimal_wire(lower),
                    "upper": None if upper is None else _decimal_wire(upper),
                }
        row["remaining"] = {"state": "observed", "amount": result_amount}


def _usage_observation(*, job_id, executor, metric, profile_key, unit, unit_ref, scope,
                       quantity, observed_at_ms, descriptors, unit_catalog, window_instance):
    profile_digest = hashlib.sha256(profile_key.encode()).hexdigest()[:12]
    scope_digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    payload = {
        "schema_version": 1,
        # observation_id 不烘進 observed_at_ms（重播當下的記錄時間）：同一個
        # job/metric/scope 的終局 usage 重播時必須產生同一個 observation_id，
        # 否則即使 digest 已排除重播時間，wire 裡的 observation_id 仍會不同，
        # 使 payload_sha256 跟著不同而被誤判成 conflict（見 #836 對抗審查
        # 第四輪 MAJOR：quota_shadow.py:590）。
        "observation_id": "usage-" + hashlib.sha256(
            f"{job_id}\0{metric}\0{profile_digest}\0{scope_digest}".encode()
        ).hexdigest()[:32],
        "scope": scope,
        "profile_ref": {"state": "known", "value": {"schema_version": 1, "key": profile_key}},
        "unit_ref": {"state": "known", "value": {"unit_id": unit_ref[0], "version": unit_ref[1]}},
        "window_instance": window_instance,
        "measurement": {"kind": "usage_delta", "metric_id": metric, "quantity": quantity},
        "observed_at_ms": {"state": "known", "value": observed_at_ms},
        "received_at_ms": observed_at_ms,
        "ttl_ms": {"state": "unknown", "reason": "terminal-usage-is-durable-event"},
        "reset_at_ms": {"state": "unknown", "reason": "usage-reset-unavailable"},
        "source": {
            "source_id": "cortex-executor-usage",
            "source_schema": "cortex-terminal-usage-v1",
            "adapter_version": "usage-extractors-325",
            "authority_ref": "cortex:registry-terminal-usage/v1",
            "method": "executor_usage",
            "provenance_refs": ["cortex:issue-325-usage-extractors/v1", "cortex:registry-terminal-usage/v1"],
            "event_identity": {"state": "unknown", "reason": "registry-job-id-is-caller-idempotency-key"},
        },
        "coverage": ({"state": "complete", "gaps": []}
                     if quantity.get("state") != "unknown" else {
                         "state": "partial", "gaps": [{"scope": metric, "reason": quantity["reason"]}],
                     }),
    }
    combined_units = tuple(item for item in unit_catalog if item.ref != unit.ref) + (unit,)
    return schema.parse_observation(payload, descriptors=descriptors, unit_catalog=combined_units)


_TERMINAL_USAGE_WINDOW_INSTANCE_UNKNOWN = {
    "kind": "unknown", "reason": "usage-window-instance-unavailable",
}


def _binding_constraints(binding_wire, descriptors):
    descriptor_by_ref = {descriptor.pool_ref: descriptor for descriptor in descriptors}
    result = []
    for item in binding_wire.get("constraints", []):
        if not isinstance(item, dict) or item.get("state") != "known":
            continue
        value = item.get("value")
        if not isinstance(value, dict) or not isinstance(value.get("pool_ref"), dict):
            continue
        ref = value["pool_ref"]
        pool_key = tuple(ref.get(name) for name in ("authority_id", "account_id", "pool_id", "revision"))
        descriptor = descriptor_by_ref.get(pool_key)
        if descriptor is None:
            continue
        window_id = value.get("window_id")
        window = next((row for row in descriptor.to_dict()["windows"] if row["window_id"] == window_id), None)
        if window is None:
            continue
        scope = {"pool_ref": dict(ref), "window_id": window_id}
        result.append({
            "scope": scope,
            "window_id": window_id,
            "unit_ref": (window["unit_ref"]["unit_id"], window["unit_ref"]["version"]),
            "association": {"pool_ref": dict(ref), "window_id": window_id,
                            "binding_id": binding_wire["binding_id"]},
        })
    return result


def _binding_has_profile(binding, profile_key, *, identity=None):
    """``identity`` 為 #1116 新增的選填 ``(executor, model_id)``——缺席
    （沿用既有呼叫端不帶這個參數）時行為與 #1116 之前逐字相同，只比對
    resolved profile key 精確綁定（``profile``／``group`` 兩種 kind）；帶入
    時額外接受 executor＋model_id 穩定 subject 綁定（``identity`` kind），
    比照 `quota_admission._matches_exact`／`_matches_identity` 的同一套
    優先序語意（此處只需要『有沒有涵蓋』的布林結果，不需要選出 pool/window
    集合，因此不重用那兩個函式，各自獨立成檔的既有慣例——見模組文件字串）。
    """
    subject = binding.get("subject")
    if not isinstance(subject, dict):
        return False
    if subject.get("kind") == "profile":
        value = subject.get("profile_ref", {}).get("value", {})
        return subject.get("profile_ref", {}).get("state") == "known" and value.get("key") == profile_key
    if subject.get("kind") == "group":
        members = subject.get("members", {})
        return members.get("state") == "known" and any(
            isinstance(item, dict) and item.get("key") == profile_key
            for item in members.get("value", [])
        )
    if subject.get("kind") == "identity" and identity is not None:
        executor, model_id = identity
        return subject.get("executor") == executor and subject.get("model_id") == model_id
    return False


def _empty_pool_rows(descriptors):
    rows = []
    for descriptor in descriptors:
        wire = descriptor.to_dict()
        pool_ref = {key: getattr(descriptor, key) for key in
                    ("authority_id", "account_id", "pool_id", "revision")}
        for window in wire["windows"]:
            rows.append({
                "pool_ref": pool_ref,
                "window_id": window["window_id"],
                "unit_ref": dict(window["unit_ref"]),
                "quantity_kind": next((unit.quantity_kind for unit in descriptor.units
                                        if unit.ref == (window["unit_ref"]["unit_id"],
                                                        window["unit_ref"]["version"])),
                                       "unknown"),
                "remaining": _unknown("missing-snapshot"),
                "assessment": "unknown",
                "coverage_gaps": [],
            })
    return rows


def _scope_summary(wire):
    scope = wire.get("scope", {})
    value = scope.get("value") if isinstance(scope, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("pool_ref"), dict):
        return {"state": "unknown"}
    return {"state": "known", "pool_ref": value["pool_ref"], "window_id": value.get("window_id")}


def _mark_unresolved_record(record, rows, unresolved_usage_keys):
    """單筆事件因目前描述集解析失敗時，依 measurement kind 決定處理方式。

    非 usage 事件（例如舊 revision 的 remaining snapshot）維持既有的軟性
    標記：只在對應 pool 附註 stale-pool-revision，不強制整個 pool 轉
    unknown（見 #836 對抗審查第四輪 MAJOR：不得毒化其他 pool）。

    usage_delta 事件描述的是實際已發生的耗用，即使目前的 unit／pool／
    revision 描述集認不出它，也不得整筆丟棄：受影響的 pool（scope 已知時
    依 scope；scope 因「不可換算」而記為 unknown 時依 association）一律
    記入 unresolved_usage_keys，讓呼叫端強制轉為 unknown 並附上明確 gap。
    scope 未知又完全沒有 association 線索時，保守地視為所有已知 pool 都
    可能受影響（見 #836 對抗審查第七輪 MAJOR quota_shadow.py:791）。"""
    raw = record.get("observation") if isinstance(record, dict) else None
    if not isinstance(raw, dict):
        return
    measurement = raw.get("measurement")
    is_usage = isinstance(measurement, dict) and measurement.get("kind") == "usage_delta"
    scope = _scope_summary(raw)
    targets: list[tuple[tuple[str, ...], str]] = []
    if scope.get("state") == "known":
        pool_ref = scope.get("pool_ref")
        window_id = scope.get("window_id")
        if isinstance(pool_ref, dict):
            targets.append((
                tuple(pool_ref.get(name) for name in ("authority_id", "account_id", "pool_id")),
                window_id,
            ))
    elif is_usage:
        associations = record.get("associations") if isinstance(record, dict) else None
        for association in associations or []:
            if not isinstance(association, dict):
                continue
            pool_ref = association.get("pool_ref")
            window_id = association.get("window_id")
            if isinstance(pool_ref, dict):
                targets.append((
                    tuple(pool_ref.get(name) for name in ("authority_id", "account_id", "pool_id")),
                    window_id,
                ))
    if is_usage and scope.get("state") != "known" and not targets:
        # 既無已知 scope，也沒有任何 association 線索：完全無法判定這筆
        # usage 屬於哪個 pool，保守地視為所有目前已知的 pool 都可能受影響，
        # 不得放行一筆看不懂的耗用事件靜默消失。
        for row in rows:
            unresolved_usage_keys.add((_pool_key(row["pool_ref"]), row["window_id"]))
            row["coverage_gaps"].append("usage-unresolvable")
        return
    for ref_key, window_id in targets:
        for row in rows:
            row_key = tuple(row["pool_ref"].get(name) for name in ("authority_id", "account_id", "pool_id"))
            if row_key != ref_key or row["window_id"] != window_id:
                continue
            if is_usage:
                unresolved_usage_keys.add((_pool_key(row["pool_ref"]), row["window_id"]))
                row["coverage_gaps"].append("usage-unresolvable")
            else:
                row["coverage_gaps"].append("stale-pool-revision")


def _public_event(record, observation):
    wire = observation.to_dict()
    source = wire.get("source", {})
    measurement = wire.get("measurement", {})
    scope = _scope_summary(wire)
    return {
        "observation_id": observation.observation_id,
        "measurement_kind": measurement.get("kind"),
        "metric_id": measurement.get("metric_id"),
        "quantity": measurement.get("quantity"),
        "scope": scope,
        "associations": record.get("associations", []),
        "profile_key": _known_value(wire.get("profile_ref"), nested=True),
        "unit_ref": _known_value(wire.get("unit_ref")),
        "observed_at_ms": _known_value(wire.get("observed_at_ms")),
        "source_method": source.get("method"),
        "source_schema": source.get("source_schema"),
        "source_id": source.get("source_id"),
        "adapter_version": source.get("adapter_version"),
        "provenance_refs": source.get("provenance_refs", []),
    }


def _known_value(value, *, nested=False):
    if not isinstance(value, dict) or value.get("state") != "known":
        return None
    result = value.get("value")
    return result.get("key") if nested and isinstance(result, dict) else result


def _pool_key(pool):
    return tuple(pool.get(key) for key in ("authority_id", "account_id", "pool_id", "revision"))


def rows_by_key(rows):
    return {(_pool_key(row["pool_ref"]), row["window_id"]): row for row in rows}


def _unknown(reason):
    return {"state": "unknown", "reason": reason}


def _copy_quantity(quantity):
    return json.loads(json.dumps(quantity, ensure_ascii=False))


def _assess(remaining, demand):
    if demand is None or remaining.get("state") != "observed":
        return "unknown"
    try:
        required = Decimal(demand)
    except (InvalidOperation, TypeError):
        return "unknown"
    if not required.is_finite() or required < 0:
        return "unknown"
    amount = remaining.get("amount", {})
    if not isinstance(amount, dict):
        return "unknown"
    if amount.get("kind") == "exact":
        try:
            value = Decimal(amount.get("value"))
        except (InvalidOperation, TypeError):
            return "unknown"
        if not value.is_finite() or value < 0:
            return "unknown"
        return "sufficient" if value >= required else "insufficient"
    if amount.get("kind") == "bounds":
        try:
            lower = Decimal(amount["lower"]) if amount.get("lower") is not None else None
            upper = Decimal(amount["upper"]) if amount.get("upper") is not None else None
        except (InvalidOperation, TypeError):
            return "unknown"
        if lower is not None and lower >= required:
            return "sufficient"
        if upper is not None and upper < required:
            return "insufficient"
    return "unknown"


def _decimal_wire(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
