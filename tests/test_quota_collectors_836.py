"""Issue #836 collector：唯讀 provider 讀取器、設定檔載入與 `cortex quota` CLI。

fake runner 只模擬 codex app-server／agy CLI 的 stdio 形狀，測試中不呼叫真正的
provider CLI；子程序生命週期（terminate/kill）以假 Popen-like 物件驗證。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import quota_collectors as collectors
from paulsha_cortex.coordinator import quota_ledger
from paulsha_cortex.coordinator import quota_observation as schema
from paulsha_cortex.coordinator import quota_shadow
from paulsha_cortex.coordinator import quota_sources as sources
from paulsha_cortex.porcelain import quota as quota_cli

_PROFILE_A = "epk:v1:resolved:" + "a" * 64
_PROFILE_B = "epk:v1:resolved:" + "b" * 64
_NOW = 1_800_000_000_000


# ---------------------------------------------------------------------------
# fixtures：descriptor／binding／target
# ---------------------------------------------------------------------------


def _profile_ref(key: str) -> dict[str, object]:
    return {"state": "known", "value": {"schema_version": 1, "key": key}}


def _codex_descriptor(
    *, account: str = "account-shared", windows: tuple[tuple[str, int], ...] = (
        ("primary", 10_080 * 60_000), ("secondary", 180 * 60_000),
    )
) -> schema.PoolDescriptor:
    unit = {
        "unit_id": "codex-percent", "version": "1", "quantity_kind": "amount",
        "semantics_ref": "provider:openai-codex-app-server/rate-limit-percent/v2",
    }
    return schema.parse_pool_descriptor({
        "schema_version": 1,
        "authority_id": "operator-budget-authority",
        "account_id": account,
        "pool_id": "pool-codex",
        "revision": "1",
        "authority_ref": "fixture:operator-pool-map/v1",
        "provenance_refs": ["fixture:pool-map/v1"],
        "units": [unit],
        "windows": [
            {
                "window_id": window_id, "kind": "rolling",
                "unit_ref": {"unit_id": "codex-percent", "version": "1"},
                "duration_ms": duration_ms,
            }
            for window_id, duration_ms in windows
        ],
    })


def _agy_descriptor() -> schema.PoolDescriptor:
    unit = {
        "unit_id": "agy-fraction", "version": "1", "quantity_kind": "amount",
        "semantics_ref": "provider:google-antigravity-cli/quota-fraction/v1",
    }
    return schema.parse_pool_descriptor({
        "schema_version": 1,
        "authority_id": "operator-budget-authority",
        "account_id": "account-agy",
        "pool_id": "pool-agy",
        "revision": "1",
        "authority_ref": "fixture:operator-pool-map/v1",
        "provenance_refs": ["fixture:pool-map/v1"],
        "units": [unit],
        "windows": [
            {
                "window_id": "daily", "kind": "rolling",
                "unit_ref": {"unit_id": "agy-fraction", "version": "1"},
                "duration_ms": 86_400_000,
            },
        ],
    })


def _binding(
    descriptors: tuple[schema.PoolDescriptor, ...], key: str, *, binding_id: str = "binding"
) -> schema.ProfilePoolBinding:
    constraints = [
        {
            "state": "known",
            "value": {
                "pool_ref": {
                    "authority_id": descriptor.authority_id, "account_id": descriptor.account_id,
                    "pool_id": descriptor.pool_id, "revision": descriptor.revision,
                },
                "window_id": window["window_id"],
            },
        }
        for descriptor in descriptors for window in descriptor.to_dict()["windows"]
    ]
    return schema.parse_binding(
        {
            "schema_version": 1, "binding_id": binding_id, "revision": "1",
            "subject": {"kind": "profile", "profile_ref": _profile_ref(key)},
            "constraints": constraints,
            "coverage": {"state": "complete", "gaps": []},
        },
        descriptors=descriptors,
    )


def _codex_targets(descriptor, binding, window_ids=("primary", "secondary")):
    return tuple(
        sources.ProviderQuotaTarget(
            resource_key=f"codex:codex:{window_id}", binding=binding,
            descriptor=descriptor, window_id=window_id,
        )
        for window_id in window_ids
    )


def _agy_target(descriptor, binding):
    return sources.ProviderQuotaTarget(
        resource_key="agy:daily", binding=binding, descriptor=descriptor, window_id="daily",
    )


# ---------------------------------------------------------------------------
# fake codex app-server transport
# ---------------------------------------------------------------------------


class _FakeWriteStream:
    def __init__(self, *, raises: bool = False) -> None:
        self.writes: list[str] = []
        self.closed = False
        self._raises = raises

    def write(self, data: str) -> None:
        if self._raises:
            raise OSError("broken pipe")
        self.writes.append(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeReadStream:
    """依 queue 逐行回傳；``block`` 表示佇列耗盡後永遠阻塞，直到 ``close()``
    （模擬真實逾時：呼叫端終止子程序時才會解除阻塞）。"""

    def __init__(self, lines: tuple[str, ...] = (), *, block: bool = False) -> None:
        self._lines = list(lines)
        self._block = block
        self._closed = threading.Event()

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        if self._block and not self._closed.is_set():
            self._closed.wait()
        return ""

    def close(self) -> None:
        self._closed.set()


class _FakeProcess:
    def __init__(
        self, stdout_lines: tuple[str, ...] = (), *, block_stdout: bool = False,
        stdin_raises: bool = False,
    ) -> None:
        self.stdin = _FakeWriteStream(raises=stdin_raises)
        self.stdout = _FakeReadStream(stdout_lines, block=block_stdout)
        self.stderr = _FakeWriteStream()
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False
        self.stdout.close()

    def kill(self) -> None:
        self.killed = True
        self._alive = False
        self.stdout.close()

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _init_ok_line(request_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {}}) + "\n"


def _rate_limits_line(request_id: int = 2) -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "result": {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182},
                "secondary": None,
                "credits": {"balance": "123.45"},
                "planType": "team",
            },
            "rateLimitsByLimitId": {"codex": {"limitId": "codex"}},
            "accountId": "11111111-1111-1111-1111-111111111111",
        },
    }) + "\n"


# ---------------------------------------------------------------------------
# codex collector
# ---------------------------------------------------------------------------


def test_codex_real_response_shape_produces_observation_and_terminates_process():
    proc = _FakeProcess((_init_ok_line(), _rate_limits_line()))
    descriptor = _codex_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding)

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, ttl_ms=60_000, timeout_s=2.0,
        cortex_version="9.9.9", spawn_codex=lambda: proc,
    )

    assert proc.terminated or proc.killed
    by_key = {obs.to_dict()["scope"]["value"]["window_id"]: obs.to_dict() for obs in capture.observations}
    assert by_key["primary"]["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "99"}
    assert by_key["primary"]["reset_at_ms"] == {"state": "known", "value": 1_791_072_182_000}
    # secondary 為 null：provider 明確回報「沒有這個 window」，是精確 gap，
    # 不是格式錯誤，也不是靜默丟棄（#836 對抗審查第九輪 MAJOR-1）。
    assert by_key["secondary"]["measurement"]["quantity"]["state"] == "unknown"
    gap_reasons = {gap.resource_key: gap.reason for gap in capture.gaps}
    assert gap_reasons == {"codex:codex:secondary": "provider-window-absent"}

    # accountId／原始 payload 一律不出現在 observation wire 裡。
    dumped = json.dumps([obs.to_dict() for obs in capture.observations])
    assert "11111111-1111-1111-1111-111111111111" not in dumped
    assert "ordinaryUsageAllowed" not in dumped
    assert "planType" not in dumped

    # initialize request 帶 cortex 版本；initialized 通知在讀到 id=1 之後才送出。
    sent = [json.loads(line) for line in proc.stdin.writes]
    assert sent[0]["method"] == "initialize"
    assert sent[0]["params"]["clientInfo"]["version"] == "9.9.9"
    assert sent[1]["method"] == "initialized"
    assert sent[2]["method"] == "account/rateLimits/read"


# ---------------------------------------------------------------------------
# #836 對抗審查第九輪 MAJOR-1：rateLimitsByLimitId／頂層 rateLimits 對帳
# ---------------------------------------------------------------------------


def _rate_limits_by_limit_id_only_line(request_id: int = 2) -> str:
    """回應只有 rateLimitsByLimitId、沒有頂層 rateLimits（真實形狀之一；
    ``qualification/driver.py`` 的 preflight 早就接受這種形狀）。"""

    return json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "result": {
            "ordinaryUsageAllowed": True,
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "ordinaryUsageAllowed": True,
                    "rateLimitReachedType": None,
                    "spendControlReached": False,
                    "primary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182},
                    "secondary": None,
                },
            },
            "accountId": "22222222-2222-2222-2222-222222222222",
        },
    }) + "\n"


def test_codex_rate_limits_by_limit_id_only_still_produces_observation():
    proc = _FakeProcess((_init_ok_line(), _rate_limits_by_limit_id_only_line()))
    descriptor = _codex_descriptor(windows=(("primary", 10_080 * 60_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, ttl_ms=60_000, timeout_s=2.0, spawn_codex=lambda: proc,
    )

    assert not capture.gaps
    by_key = {obs.to_dict()["scope"]["value"]["window_id"]: obs.to_dict() for obs in capture.observations}
    assert by_key["primary"]["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "99"}


def _codex_two_snapshot_line(*, top: dict, by_id: dict, request_id: int = 2) -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "result": {"rateLimits": top, "rateLimitsByLimitId": {"codex": by_id}},
    }) + "\n"


def test_codex_matching_top_level_and_by_limit_id_snapshots_are_not_a_conflict():
    snapshot = {"limitId": "codex", "primary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182}}
    proc = _FakeProcess((_init_ok_line(), _codex_two_snapshot_line(top=snapshot, by_id=dict(snapshot))))
    descriptor = _codex_descriptor(windows=(("primary", 10_080 * 60_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, ttl_ms=60_000, timeout_s=2.0, spawn_codex=lambda: proc,
    )

    assert not capture.gaps
    by_key = {obs.to_dict()["scope"]["value"]["window_id"]: obs.to_dict() for obs in capture.observations}
    assert by_key["primary"]["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "99"}


def test_codex_conflicting_top_level_and_by_limit_id_snapshots_is_precise_gap_not_a_guess():
    top = {"limitId": "codex", "primary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182}}
    by_id = {"limitId": "codex", "primary": {"usedPercent": 50, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182}}
    proc = _FakeProcess((_init_ok_line(), _codex_two_snapshot_line(top=top, by_id=by_id)))
    descriptor = _codex_descriptor(windows=(("primary", 10_080 * 60_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, ttl_ms=60_000, timeout_s=2.0, spawn_codex=lambda: proc,
    )

    gap_reasons = {gap.resource_key: gap.reason for gap in capture.gaps}
    assert gap_reasons == {"codex:codex:primary": "provider-limit-snapshot-conflict"}
    assert capture.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"


# ---------------------------------------------------------------------------
# #836 對抗審查第九輪 MAJOR-2：provider 明示拒絕一般用量時不得呈現有剩餘
# ---------------------------------------------------------------------------


def _codex_gating_line(
    *, ordinary_usage_allowed=True, rate_limit_reached_type=None, spend_control_reached=False,
    request_id: int = 2,
) -> str:
    # ordinaryUsageAllowed 是回應頂層欄位（跟 rateLimits 平行、不巢在裡面）；
    # rateLimitReachedType／spendControlReached／credits 才是巢在 limitId
    # snapshot 底下的欄位——真實形狀就是這樣分層（見任務給的去識別回應）。
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "result": {
            "ordinaryUsageAllowed": ordinary_usage_allowed,
            "rateLimits": {
                "limitId": "codex",
                "rateLimitReachedType": rate_limit_reached_type,
                "spendControlReached": spend_control_reached,
                "primary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            },
        },
    }) + "\n"


def _run_codex_gating_case(line: str):
    proc = _FakeProcess((_init_ok_line(), line))
    descriptor = _codex_descriptor(windows=(("primary", 10_080 * 60_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))
    return collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, ttl_ms=60_000, timeout_s=2.0, spawn_codex=lambda: proc,
    )


def test_codex_ordinary_usage_not_allowed_forces_remaining_zero_not_optimistic():
    # usedPercent=1（表面上還有 99% 可用），但 ordinaryUsageAllowed=False：
    # provider 已明示拒絕一般用量，不得仍寫出「有剩餘」的觀測。
    capture = _run_codex_gating_case(_codex_gating_line(ordinary_usage_allowed=False))

    obs = capture.observations[0].to_dict()
    assert obs["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "0"}
    assert obs["reset_at_ms"] == {"state": "known", "value": 1_791_072_182_000}
    gap_reasons = {gap.resource_key: gap.reason for gap in capture.gaps}
    assert gap_reasons == {"codex:codex:primary": "provider-reports-limit-reached"}
    dumped = json.dumps(obs)
    assert "hasCredits" not in dumped and "balance" not in dumped


def test_codex_rate_limit_reached_type_set_forces_remaining_zero():
    capture = _run_codex_gating_case(_codex_gating_line(rate_limit_reached_type="secondary"))

    obs = capture.observations[0].to_dict()
    assert obs["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "0"}
    gap_reasons = {gap.resource_key: gap.reason for gap in capture.gaps}
    assert gap_reasons == {"codex:codex:primary": "provider-reports-limit-reached"}


def test_codex_spend_control_reached_forces_remaining_zero():
    capture = _run_codex_gating_case(_codex_gating_line(spend_control_reached=True))

    obs = capture.observations[0].to_dict()
    assert obs["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "0"}
    gap_reasons = {gap.resource_key: gap.reason for gap in capture.gaps}
    assert gap_reasons == {"codex:codex:primary": "provider-reports-limit-reached"}


def test_codex_ordinary_usage_allowed_wrong_type_is_precise_invalid_gap():
    line = json.dumps({
        "jsonrpc": "2.0", "id": 2,
        "result": {
            "ordinaryUsageAllowed": "yes",
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182},
            },
        },
    }) + "\n"
    capture = _run_codex_gating_case(line)

    gap_reasons = {gap.resource_key: gap.reason for gap in capture.gaps}
    assert gap_reasons == {"codex:codex:primary": "invalid-provider-value"}
    assert capture.observations[0].to_dict()["measurement"]["quantity"]["state"] == "unknown"


def test_codex_rate_limit_reached_type_wrong_type_is_precise_invalid_gap():
    line = json.dumps({
        "jsonrpc": "2.0", "id": 2,
        "result": {
            "rateLimits": {
                "limitId": "codex", "rateLimitReachedType": 1,
                "primary": {"usedPercent": 1, "windowDurationMins": 10_080, "resetsAt": 1_791_072_182},
            },
        },
    }) + "\n"
    capture = _run_codex_gating_case(line)

    gap_reasons = {gap.resource_key: gap.reason for gap in capture.gaps}
    assert gap_reasons == {"codex:codex:primary": "invalid-provider-value"}


def test_codex_timeout_reports_gap_and_terminates_process():
    proc = _FakeProcess(block_stdout=True)
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=0.05, spawn_codex=lambda: proc,
    )

    assert proc.terminated or proc.killed
    assert {gap.reason for gap in capture.gaps} == {"collector-timeout"}


def test_codex_process_exits_before_id2_response_reports_gap():
    # 只回 initialize 回應，之後子程序關閉 stdout（EOF）；不是逾時，是提前結束。
    proc = _FakeProcess((_init_ok_line(),))
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=lambda: proc,
    )

    assert proc.terminated or proc.killed
    assert {gap.reason for gap in capture.gaps} == {"collector-process-exited"}


def test_codex_invalid_json_line_reports_gap():
    proc = _FakeProcess((_init_ok_line(), "not-json\n"))
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=lambda: proc,
    )

    assert proc.terminated or proc.killed
    assert {gap.reason for gap in capture.gaps} == {"collector-invalid-json"}


def test_codex_protocol_error_response_reports_gap():
    error_line = json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "boom"}}) + "\n"
    proc = _FakeProcess((_init_ok_line(), error_line))
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=lambda: proc,
    )

    assert proc.terminated or proc.killed
    assert {gap.reason for gap in capture.gaps} == {"collector-protocol-error"}
    dumped = json.dumps([obs.to_dict() for obs in capture.observations])
    assert "boom" not in dumped


def test_codex_spawn_failure_reports_gap_without_crashing():
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    def _spawn():
        raise OSError("no such file")

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=_spawn,
    )
    assert {gap.reason for gap in capture.gaps} == {"collector-spawn-failed"}


def test_codex_write_failure_reports_gap_and_terminates():
    proc = _FakeProcess(stdin_raises=True)
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))

    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=lambda: proc,
    )
    assert proc.terminated or proc.killed
    assert {gap.reason for gap in capture.gaps} == {"collector-write-failed"}


# ---------------------------------------------------------------------------
# agy collector
# ---------------------------------------------------------------------------


def test_agy_real_shape_produces_observation():
    calls: list[tuple[tuple[str, ...], float]] = []

    def _run(argv, timeout_s):
        calls.append((argv, timeout_s))
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"quota": {"daily": {"remaining_fraction": 0.42}}}), stderr="",
        )

    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    target = _agy_target(descriptor, binding)

    capture = collectors.collect_provider_quota(
        "agy", profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=5.0, run_agy=_run,
    )

    assert not capture.gaps
    wire = capture.observations[0].to_dict()
    assert wire["measurement"]["quantity"]["amount"] == {"kind": "exact", "value": "0.42"}
    assert calls == [(("agy", "-p", "/usage", "--output-format", "json"), 5.0)]


def test_agy_timeout_reports_gap():
    def _run(argv, timeout_s):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_s)

    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    target = _agy_target(descriptor, binding)

    capture = collectors.collect_provider_quota(
        "agy", profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=0.05, run_agy=_run,
    )
    assert {gap.reason for gap in capture.gaps} == {"collector-timeout"}


def test_agy_nonzero_exit_reports_gap():
    def _run(argv, timeout_s):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="permission denied: token expired")

    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    target = _agy_target(descriptor, binding)

    capture = collectors.collect_provider_quota(
        "agy", profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=5.0, run_agy=_run,
    )
    assert {gap.reason for gap in capture.gaps} == {"collector-nonzero-exit"}
    dumped = json.dumps([obs.to_dict() for obs in capture.observations])
    assert "token expired" not in dumped


def test_agy_invalid_json_reports_gap():
    def _run(argv, timeout_s):
        return subprocess.CompletedProcess(argv, 0, stdout="not json", stderr="")

    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    target = _agy_target(descriptor, binding)

    capture = collectors.collect_provider_quota(
        "agy", profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=5.0, run_agy=_run,
    )
    assert {gap.reason for gap in capture.gaps} == {"collector-invalid-json"}


# ---------------------------------------------------------------------------
# 沒有正式唯讀路徑的 executor
# ---------------------------------------------------------------------------


def test_copilot_reports_precise_unknown_gap_without_guessing():
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    target = sources.ProviderQuotaTarget(
        resource_key="copilot:primary", binding=binding, descriptor=descriptor, window_id="primary",
    )
    capture = collectors.collect_provider_quota(
        "copilot", profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=1.0,
    )
    assert {gap.reason for gap in capture.gaps} == {"copilot-quota-read-path-unavailable"}


@pytest.mark.parametrize(
    "executor,expected_reason",
    [
        ("claude", "passive-job-log-source"),
        ("cg", "no-verified-quota-read-contract"),
    ],
)
def test_claude_and_cg_report_contract_reason(executor, expected_reason):
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    target = sources.ProviderQuotaTarget(
        resource_key=f"{executor}:primary", binding=binding, descriptor=descriptor, window_id="primary",
    )
    capture = collectors.collect_provider_quota(
        executor, profile_key=_PROFILE_A, targets=(target,), descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=1.0,
    )
    assert {gap.reason for gap in capture.gaps} == {expected_reason}


# ---------------------------------------------------------------------------
# ledger 寫入與不可寫
# ---------------------------------------------------------------------------


def test_append_captures_to_ledger_writes_observations_without_leaking_raw_payload(tmp_path):
    proc = _FakeProcess((_init_ok_line(), _rate_limits_line()))
    descriptor = _codex_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding)
    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=lambda: proc,
    )

    ledger = quota_ledger.QuotaEventLedger(tmp_path / "events.jsonl")
    service = quota_shadow.QuotaShadowService(ledger)
    status = collectors.append_captures_to_ledger(service, [capture])
    assert status == "written"

    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "11111111-1111-1111-1111-111111111111" not in raw
    assert "ordinaryUsageAllowed" not in raw
    snapshot = ledger.read()
    assert len(snapshot.events) == 2


def test_append_captures_to_ledger_reports_unwritable_without_crashing(tmp_path):
    root = tmp_path / "coord"
    root.mkdir(mode=0o500)
    ledger = quota_ledger.QuotaEventLedger(root / "quota-observations" / "events.jsonl")
    service = quota_shadow.QuotaShadowService(ledger)

    proc = _FakeProcess((_init_ok_line(), _rate_limits_line()))
    descriptor = _codex_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding)
    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=lambda: proc,
    )
    try:
        status = collectors.append_captures_to_ledger(service, [capture])
        assert status == "ledger-unwritable"
    finally:
        root.chmod(0o700)


# ---------------------------------------------------------------------------
# load_collector_config()
# ---------------------------------------------------------------------------


def _config_payload(descriptor, binding, *, config_revision="rev-1", collector_targets=None, lease_ms=None):
    payload = {
        "schema": "cortex/quota-pools/v1",
        "config_revision": config_revision,
        "descriptors": [descriptor.to_dict()],
        "unit_catalog": [],
        "bindings": [binding.to_dict()],
    }
    if collector_targets is not None:
        payload["collector_targets"] = collector_targets
    if lease_ms is not None:
        payload["lease_ms"] = lease_ms
    return payload


def _pool_ref_dict(descriptor):
    return {
        "authority_id": descriptor.authority_id, "account_id": descriptor.account_id,
        "pool_id": descriptor.pool_id, "revision": descriptor.revision,
    }


def test_load_collector_config_parses_descriptors_bindings_and_targets(tmp_path):
    descriptor = _codex_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    payload = _config_payload(
        descriptor, binding,
        collector_targets={
            "codex": [
                {
                    "resource_key": "codex:codex:primary", "window_id": "primary",
                    "profile_key": _PROFILE_A, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
        lease_ms=120_000,
    )
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    config = collectors.load_collector_config(config_path)
    assert config.config_revision == "rev-1"
    assert config.lease_ms == 120_000
    assert config.executors() == ("codex",)
    groups = config.groups_for("codex")
    assert len(groups) == 1
    assert groups[0].profile_key == _PROFILE_A
    assert groups[0].targets[0].resource_key == "codex:codex:primary"


def test_load_collector_config_groups_targets_by_profile_key(tmp_path):
    descriptor = _codex_descriptor(account="account-two-profiles")
    binding_a = _binding((descriptor,), _PROFILE_A, binding_id="binding-a")
    binding_b = _binding((descriptor,), _PROFILE_B, binding_id="binding-b")
    payload = {
        "schema": "cortex/quota-pools/v1",
        "config_revision": "rev-2",
        "descriptors": [descriptor.to_dict()],
        "unit_catalog": [],
        "bindings": [binding_a.to_dict(), binding_b.to_dict()],
        "collector_targets": {
            "codex": [
                {
                    "resource_key": "codex:codex:primary", "window_id": "primary",
                    "profile_key": _PROFILE_A, "pool_ref": _pool_ref_dict(descriptor),
                },
                {
                    "resource_key": "codex:codex:secondary", "window_id": "secondary",
                    "profile_key": _PROFILE_B, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
    }
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    config = collectors.load_collector_config(config_path)
    groups = config.groups_for("codex")
    assert {group.profile_key for group in groups} == {_PROFILE_A, _PROFILE_B}


def test_load_collector_config_rejects_schema_mismatch(tmp_path):
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps({"schema": "not-the-right-schema", "config_revision": "x"}), encoding="utf-8")
    with pytest.raises(collectors.CollectorConfigError):
        collectors.load_collector_config(config_path)


def test_load_collector_config_rejects_unresolved_binding(tmp_path):
    descriptor = _codex_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    payload = _config_payload(
        descriptor, binding,
        collector_targets={
            "codex": [
                {
                    # profile_key 沒有任何 binding 覆蓋。
                    "resource_key": "codex:codex:primary", "window_id": "primary",
                    "profile_key": _PROFILE_B, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
    )
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(collectors.CollectorConfigError):
        collectors.load_collector_config(config_path)


def test_load_collector_config_rejects_invalid_lease_ms(tmp_path):
    descriptor = _codex_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    payload = _config_payload(descriptor, binding, lease_ms=0)
    config_path = tmp_path / "quota-pools.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(collectors.CollectorConfigError):
        collectors.load_collector_config(config_path)


def test_load_collector_config_rejects_missing_config_file(tmp_path):
    with pytest.raises(collectors.CollectorConfigError):
        collectors.load_collector_config(tmp_path / "does-not-exist.json")


# ---------------------------------------------------------------------------
# export（--output）→ import 往返
# ---------------------------------------------------------------------------


def test_export_then_import_round_trip_is_equivalent(tmp_path):
    proc = _FakeProcess((_init_ok_line(), _rate_limits_line()))
    descriptor = _codex_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    targets = _codex_targets(descriptor, binding)
    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, timeout_s=2.0, spawn_codex=lambda: proc,
    )
    export_payload = collectors.observation_export_payload(
        capture.observations, config_revision="rev-1", generated_at_ms=_NOW,
    )
    assert export_payload["schema"] == collectors.OUTPUT_SCHEMA
    dumped = json.dumps(export_payload)
    assert "11111111-1111-1111-1111-111111111111" not in dumped

    ledger = quota_ledger.QuotaEventLedger(tmp_path / "events.jsonl")
    service = quota_shadow.QuotaShadowService(ledger)
    accepted = 0
    for item in export_payload["observations"]:
        result = service.record_external_observation(item, descriptors=(descriptor,), unit_catalog=())
        assert result.status in {"accepted", "duplicate"}
        accepted += result.accepted
    assert accepted == len(capture.observations)


def test_import_rejects_non_read_only_method(tmp_path):
    descriptor = _codex_descriptor(windows=(("primary", 604_800_000),))
    payload = {
        "schema_version": 1,
        "observation_id": "fixture-non-read-only",
        "scope": {
            "state": "known",
            "value": {"pool_ref": _pool_ref_dict(descriptor), "window_id": "primary"},
        },
        "profile_ref": _profile_ref(_PROFILE_A),
        "unit_ref": {"state": "known", "value": {"unit_id": "codex-percent", "version": "1"}},
        "window_instance": {"kind": "unknown", "reason": "missing-window-instance"},
        "measurement": {
            "kind": "remaining_snapshot", "metric_id": "remaining",
            "quantity": {"state": "observed", "amount": {"kind": "exact", "value": "10"}},
        },
        "observed_at_ms": {"state": "known", "value": _NOW},
        "received_at_ms": _NOW,
        "ttl_ms": {"state": "known", "value": 60_000},
        "reset_at_ms": {"state": "unknown", "reason": "missing-reset"},
        "source": {
            "source_id": "fixture-executor", "source_schema": "fixture-executor-usage-v1",
            "adapter_version": "fixture-v1", "authority_ref": "fixture:contract/v1",
            "method": "executor_usage", "provenance_refs": ["fixture:contract/v1"],
            "event_identity": {"state": "unknown", "reason": "no-event-id"},
        },
        "coverage": {"state": "complete", "gaps": []},
    }
    ledger = quota_ledger.QuotaEventLedger(tmp_path / "events.jsonl")
    service = quota_shadow.QuotaShadowService(ledger)
    result = service.record_external_observation(payload, descriptors=(descriptor,), unit_catalog=())
    assert result.status == "invalid"
    assert {gap.reason for gap in result.gaps} == {"external-source-not-read-only"}


# ---------------------------------------------------------------------------
# import 信任邊界：#836 對抗審查第九輪 MAJOR-3
# ---------------------------------------------------------------------------


def _import_fixture(tmp_path):
    """一組通過驗證的 baseline：真的走 collect_provider_quota() 產生
    observation，config 內有對應的 collector_targets。回傳
    ``(config, export_payload)``。"""

    descriptor = _codex_descriptor(windows=(("primary", 10_080 * 60_000),))
    binding = _binding((descriptor,), _PROFILE_A)
    proc = _FakeProcess((_init_ok_line(), _rate_limits_line()))
    targets = _codex_targets(descriptor, binding, window_ids=("primary",))
    capture = collectors.collect_provider_quota(
        "codex", profile_key=_PROFILE_A, targets=targets, descriptors=(descriptor,),
        unit_catalog=(), observed_at_ms=_NOW, ttl_ms=300_000, timeout_s=2.0, spawn_codex=lambda: proc,
    )
    export_payload = collectors.observation_export_payload(
        capture.observations, config_revision="rev-1", generated_at_ms=_NOW,
    )
    config_path = _write_config(
        tmp_path, descriptor, binding,
        collector_targets={
            "codex": [
                {
                    "resource_key": "codex:codex:primary", "window_id": "primary",
                    "profile_key": _PROFILE_A, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
    )
    config = collectors.load_collector_config(config_path)
    return config, export_payload


def test_validate_import_payload_accepts_a_genuine_observe_output_export(tmp_path):
    config, export_payload = _import_fixture(tmp_path)
    validated = collectors.validate_import_payload(export_payload, config=config, now_ms=_NOW + 1_000)
    assert len(validated) == len(export_payload["observations"])


def test_validate_import_payload_rejects_hand_crafted_file_with_fake_source_contract(tmp_path):
    """手工構造的 provider_status 檔：schema id／observation 結構都合法，
    但 source_schema／authority_ref 是隨便填的，不是真的
    provider_read_contract() 正式值——必須被整份拒絕，不能只驗 top-level schema。"""

    config, export_payload = _import_fixture(tmp_path)
    forged = json.loads(json.dumps(export_payload))
    forged["observations"][0]["source"]["source_schema"] = "totally-made-up-schema-v1"
    forged["observations"][0]["source"]["authority_ref"] = "https://example.invalid/made-up"
    with pytest.raises(collectors.ImportValidationError, match="import-source-contract-mismatch"):
        collectors.validate_import_payload(forged, config=config, now_ms=_NOW + 1_000)


def test_validate_import_payload_rejects_config_revision_mismatch(tmp_path):
    config, export_payload = _import_fixture(tmp_path)
    forged = json.loads(json.dumps(export_payload))
    forged["config_revision"] = "rev-does-not-match"
    with pytest.raises(collectors.ImportValidationError, match="import-config-revision-mismatch"):
        collectors.validate_import_payload(forged, config=config, now_ms=_NOW + 1_000)


def test_validate_import_payload_rejects_resource_key_not_in_collector_targets(tmp_path):
    config, export_payload = _import_fixture(tmp_path)
    forged = json.loads(json.dumps(export_payload))
    # profile_key 換成設定檔完全沒有對應 collector_targets 的另一個 profile。
    forged["observations"][0]["profile_ref"]["value"]["key"] = _PROFILE_B
    with pytest.raises(collectors.ImportValidationError, match="import-resource-not-configured"):
        collectors.validate_import_payload(forged, config=config, now_ms=_NOW + 1_000)


def test_validate_import_payload_rejects_observed_at_in_the_future(tmp_path):
    config, export_payload = _import_fixture(tmp_path)
    with pytest.raises(collectors.ImportValidationError, match="import-observed-at-in-future"):
        collectors.validate_import_payload(export_payload, config=config, now_ms=_NOW - 1)


def test_validate_import_payload_rejects_expired_observation(tmp_path):
    config, export_payload = _import_fixture(tmp_path)
    with pytest.raises(collectors.ImportValidationError, match="import-observation-expired"):
        collectors.validate_import_payload(
            export_payload, config=config, now_ms=_NOW + 300_000 + 1,
        )


def test_validate_import_payload_rejects_missing_collector_version(tmp_path):
    config, export_payload = _import_fixture(tmp_path)
    forged = json.loads(json.dumps(export_payload))
    del forged["collector_version"]
    with pytest.raises(collectors.ImportValidationError, match="import-collector-version-missing"):
        collectors.validate_import_payload(forged, config=config, now_ms=_NOW + 1_000)


def test_validate_import_payload_error_message_never_contains_raw_content(tmp_path):
    config, export_payload = _import_fixture(tmp_path)
    forged = json.loads(json.dumps(export_payload))
    forged["observations"][0]["profile_ref"]["value"]["key"] = _PROFILE_B
    try:
        collectors.validate_import_payload(forged, config=config, now_ms=_NOW + 1_000)
        raise AssertionError("expected ImportValidationError")
    except collectors.ImportValidationError as exc:
        assert re.fullmatch(r"[a-z][a-z0-9-]*", str(exc))
        assert _PROFILE_B not in str(exc)


def test_cli_quota_import_rejects_hand_crafted_provider_status_file(tmp_path, capsys):
    config, export_payload = _import_fixture(tmp_path)
    forged = json.loads(json.dumps(export_payload))
    forged["observations"][0]["source"]["authority_ref"] = "https://example.invalid/forged"
    forged_path = tmp_path / "forged.json"
    forged_path.write_text(json.dumps(forged), encoding="utf-8")

    config_path = _write_config(
        tmp_path, _codex_descriptor(windows=(("primary", 10_080 * 60_000),)),
        _binding((_codex_descriptor(windows=(("primary", 10_080 * 60_000),)),), _PROFILE_A),
        collector_targets={
            "codex": [
                {
                    "resource_key": "codex:codex:primary", "window_id": "primary",
                    "profile_key": _PROFILE_A,
                    "pool_ref": _pool_ref_dict(_codex_descriptor(windows=(("primary", 10_080 * 60_000),))),
                },
            ],
        },
    )
    exit_code = quota_cli.main(["import", "--config", str(config_path), "--file", str(forged_path)])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "信任邊界" in err
    assert "example.invalid" not in err


# ---------------------------------------------------------------------------
# CLI：`cortex quota observe`／`cortex quota import`
# ---------------------------------------------------------------------------


def _write_config(tmp_path, descriptor, binding, *, collector_targets):
    payload = _config_payload(descriptor, binding, collector_targets=collector_targets)
    path = tmp_path / "quota-pools.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_cli_quota_observe_dry_run_prints_json_summary_without_writing(tmp_path, capsys, monkeypatch):
    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    config_path = _write_config(
        tmp_path, descriptor, binding,
        collector_targets={
            "agy": [
                {
                    "resource_key": "agy:daily", "window_id": "daily",
                    "profile_key": _PROFILE_A, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
    )

    def _run(argv, timeout_s):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"quota": {"daily": {"remaining_fraction": 0.9}}}), stderr="",
        )

    monkeypatch.setattr(collectors, "_default_run_agy", _run)
    exit_code = quota_cli.main(["observe", "--config", str(config_path), "--dry-run", "--json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "cortex-porcelain/quota-observe-summary/v1"
    assert out["executors"]["agy"]["state"] == "ok"
    assert out["executors"]["agy"]["observations"] == 1
    assert not (tmp_path / "events.jsonl").exists()


def test_cli_quota_observe_output_then_import_round_trip(tmp_path, capsys, monkeypatch):
    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    config_path = _write_config(
        tmp_path, descriptor, binding,
        collector_targets={
            "agy": [
                {
                    "resource_key": "agy:daily", "window_id": "daily",
                    "profile_key": _PROFILE_A, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
    )

    def _run(argv, timeout_s):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"quota": {"daily": {"remaining_fraction": 0.75}}}), stderr="",
        )

    monkeypatch.setattr(collectors, "_default_run_agy", _run)
    output_path = tmp_path / "observation.json"
    exit_code = quota_cli.main([
        "observe", "--config", str(config_path), "--output", str(output_path),
    ])
    assert exit_code == 0
    capsys.readouterr()
    assert output_path.exists()
    exported = json.loads(output_path.read_text(encoding="utf-8"))
    assert exported["schema"] == collectors.OUTPUT_SCHEMA
    assert len(exported["observations"]) == 1

    coordinator_root = tmp_path / "manager-coordinator-root"
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))
    import_exit_code = quota_cli.main([
        "import", "--config", str(config_path), "--file", str(output_path), "--json",
    ])
    assert import_exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["accepted"] == 1
    assert summary["invalid"] == 0
    ledger_path = coordinator_root / "quota-observations" / "events.jsonl"
    assert ledger_path.exists()


def test_cli_quota_observe_writes_ledger_by_default(tmp_path, capsys, monkeypatch):
    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    config_path = _write_config(
        tmp_path, descriptor, binding,
        collector_targets={
            "agy": [
                {
                    "resource_key": "agy:daily", "window_id": "daily",
                    "profile_key": _PROFILE_A, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
    )

    def _run(argv, timeout_s):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"quota": {"daily": {"remaining_fraction": 0.5}}}), stderr="",
        )

    monkeypatch.setattr(collectors, "_default_run_agy", _run)
    coordinator_root = tmp_path / "manager-coordinator-root"
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(coordinator_root))

    exit_code = quota_cli.main(["observe", "--config", str(config_path), "--json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["executors"]["_ledger"]["status"] == "written"
    ledger_path = coordinator_root / "quota-observations" / "events.jsonl"
    assert ledger_path.exists()


def test_cli_quota_observe_reports_ledger_unwritable(tmp_path, capsys, monkeypatch):
    descriptor = _agy_descriptor()
    binding = _binding((descriptor,), _PROFILE_A)
    config_path = _write_config(
        tmp_path, descriptor, binding,
        collector_targets={
            "agy": [
                {
                    "resource_key": "agy:daily", "window_id": "daily",
                    "profile_key": _PROFILE_A, "pool_ref": _pool_ref_dict(descriptor),
                },
            ],
        },
    )

    def _run(argv, timeout_s):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"quota": {"daily": {"remaining_fraction": 0.5}}}), stderr="",
        )

    monkeypatch.setattr(collectors, "_default_run_agy", _run)
    root = tmp_path / "readonly-root"
    root.mkdir(mode=0o500)
    monkeypatch.setenv("PSC_COORDINATOR_ROOT", str(root / "coordinator"))

    try:
        exit_code = quota_cli.main(["observe", "--config", str(config_path), "--json"])
        assert exit_code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["executors"]["_ledger"]["status"] == "ledger-unwritable"
    finally:
        root.chmod(0o700)


def test_cli_quota_help_and_subcommand_registration():
    """全程走真正的子程序（`python -m paulsha_cortex.cli ...`），不碰同一 pytest
    process 內共用、跨測試檔可能已被交換過身分的 `paulsha_cortex.porcelain`
    module singleton（`tests/test_porcelain_registry.py` 會 pop＋reimport 該
    package，讓同 session 內其餘測試對它的 `COMMANDS`／`_LOADED_MODULES` 全域
    狀態變得跟載入順序有關；用子程序驗證 CLI help／子命令註冊即可避開這個
    既有的跨測試檔脆弱點，且不涉及任何 provider CLI，只查詢本地 `cortex`
    入口本身的 argparse help，純本機、瞬時完成）。"""

    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parent.parent

    top_help = subprocess.run(
        [sys.executable, "-m", "paulsha_cortex.cli", "--help"],
        cwd=repo_root, capture_output=True, text=True, timeout=30,
    )
    assert top_help.returncode == 0
    assert "quota" in top_help.stdout

    observe_help = subprocess.run(
        [sys.executable, "-m", "paulsha_cortex.cli", "quota", "observe", "--help"],
        cwd=repo_root, capture_output=True, text=True, timeout=30,
    )
    assert observe_help.returncode == 0
    assert "--config" in observe_help.stdout
    assert "--dry-run" in observe_help.stdout

    import_help = subprocess.run(
        [sys.executable, "-m", "paulsha_cortex.cli", "quota", "import", "--help"],
        cwd=repo_root, capture_output=True, text=True, timeout=30,
    )
    assert import_help.returncode == 0
    assert "--file" in import_help.stdout


def test_quota_register_commands_is_idempotent_on_isolated_registry(monkeypatch):
    fake_commands: dict[str, object] = {}

    def _fake_register(command):
        if command.name in fake_commands:
            raise ValueError(f"porcelain command already registered: {command.name}")
        fake_commands[command.name] = command

    monkeypatch.setattr(quota_cli, "COMMANDS", fake_commands)
    monkeypatch.setattr(quota_cli, "register", _fake_register)

    quota_cli.register_commands()
    quota_cli.register_commands()  # 第二次呼叫不應重複註冊或丟例外。

    assert list(fake_commands) == ["quota"]
    assert fake_commands["quota"].run is quota_cli.main
