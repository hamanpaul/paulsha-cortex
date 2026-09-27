"""#836 額度觀測 collector：依 `quota_sources.provider_read_contract()` 對真實
provider 執行唯讀讀取，並把結果交給既有 `capture_provider_quota()` 解析。

本模組是唯一真的會啟動 provider CLI／子程序的地方；`quota_sources` 本身仍維持
「只解析已取得的 payload、不呼叫任何介面」的邊界不變。子程序的存活時間全程受
逾時控管，逾時或讀完就緒回應後一律 terminate→kill 回收，不留殭屍；不讀任何
credential 檔，也不把 provider 原始 payload／帳號識別外洩到 ledger、stdout 或
錯誤訊息——只有 `capture_provider_quota()` 產出的、已去識別的 observation 才會
往下游走。

另外提供 `load_collector_config()`：讀取版本化的 `cortex/quota-pools/v1` 設定檔
（descriptors／unit_catalog／bindings 與選填 lease_ms），並解析本模組自訂的
`collector_targets` 區塊為 `ProviderQuotaTarget`；驗證完全交給既有的
`quota_observation`／`quota_sources` parser，不另寫第二套 schema 檢查。
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from . import quota_observation as schema
from .quota_ledger import LedgerCorrupt
from .quota_shadow import QuotaShadowService
from .quota_sources import (
    ProviderCapture,
    ProviderQuotaTarget,
    _PROFILE_KEY_RE as _RESOLVED_PROFILE_KEY_RE,
    _RESOURCE_KEY_RE,
    _binding_has_profile,
    _unknown_capture,
    capture_provider_quota,
    provider_read_contract,
)

__all__ = [
    "CollectorConfigError",
    "CollectorConfig",
    "CollectorTargetGroup",
    "RawRead",
    "OUTPUT_SCHEMA",
    "load_collector_config",
    "collect_provider_quota",
    "read_codex_quota",
    "read_agy_quota",
    "read_copilot_quota",
    "append_captures_to_ledger",
    "observation_export_payload",
]

_DEFAULT_TIMEOUT_S = 20.0
_CONFIG_SCHEMA = "cortex/quota-pools/v1"
OUTPUT_SCHEMA = "cortex-quota-observations/v1"
_EXECUTOR_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_GAP_REASON_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


def _cortex_version() -> str:
    try:
        return importlib.metadata.version("paulsha-cortex")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


# ---------------------------------------------------------------------------
# provider 讀取結果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawRead:
    """單次 provider 讀取的結果：成功給 payload，失敗給精確的分類 gap 原因。

    ``error`` 一律是 ``[a-z][a-z0-9-]*`` 形狀的機器可讀 reason code，絕不含
    子程序 stderr 原文或任何未經審視的自由文字，避免不慎外洩帳號識別。
    """

    payload: dict[str, Any] | None
    error: str | None = None


def _coded_reason(reason: str, *, fallback: str) -> str:
    return reason if _GAP_REASON_RE.fullmatch(reason) else fallback


# ---------------------------------------------------------------------------
# codex：app-server stdio JSON-RPC
# ---------------------------------------------------------------------------


def _default_spawn_codex() -> subprocess.Popen:
    return subprocess.Popen(
        ("codex", "app-server"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _write_json_line(stream: Any, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def _read_line_before(stream: Any, deadline: float) -> str | None:
    """在 deadline 前讀一行；逾時回傳 None。

    以背景執行緒包住可能無限期阻塞的 ``readline()``，讓整體逾時判定不必依賴
    真實 OS pipe fd（測試可用任意具 ``readline()`` 的假物件），逾時後呼叫端
    仍會終止子程序，執行緒因而解除阻塞、自然結束（daemon thread 不留殭屍）。
    """

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    box: list[str | None] = [None]
    done = threading.Event()

    def _read() -> None:
        try:
            box[0] = stream.readline()
        except (OSError, ValueError):
            box[0] = ""
        finally:
            done.set()

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    done.wait(remaining)
    if not done.is_set():
        return None
    return box[0]


def _read_response(stream: Any, deadline: float, *, match_id: int) -> tuple[dict[str, Any] | None, str | None]:
    """讀到 ``id`` 對得上的 JSON-RPC 回應為止；忽略中途通知／其他 id。"""

    while True:
        if time.monotonic() >= deadline:
            return None, "collector-timeout"
        line = _read_line_before(stream, deadline)
        if line is None:
            return None, "collector-timeout"
        if line == "":
            return None, "collector-process-exited"
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return None, "collector-invalid-json"
        if not isinstance(parsed, dict):
            return None, "collector-invalid-json"
        if parsed.get("id") != match_id:
            continue
        if "error" in parsed:
            return None, "collector-protocol-error"
        return parsed, None


def _terminate_process(proc: Any) -> None:
    """結束子程序（terminate→kill），全程 best-effort 回收，不留殭屍。"""

    if proc is None:
        return
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
    except (OSError, ValueError):
        pass


def read_codex_quota(
    *,
    cortex_version: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    spawn: Callable[[], Any] = _default_spawn_codex,
) -> RawRead:
    """啟動 ``codex app-server``，走 JSON-RPC 讀 ``account/rateLimits/read``。

    協定：送 ``initialize``（id=1）→ 收到回應後送 ``initialized`` 通知 → 送
    ``account/rateLimits/read``（id=2）→ 取 id=2 的 ``result``。整體逾時（預設
    20 秒）或協定失敗（逾時／子程序提前結束／壞 JSON／協定錯誤／缺 id=2 回應）
    皆回精確 gap，且無論成功或失敗都會終止子程序。
    """

    deadline = time.monotonic() + timeout_s
    try:
        proc = spawn()
    except OSError:
        return RawRead(None, "collector-spawn-failed")

    try:
        try:
            _write_json_line(proc.stdin, {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "cortex-quota-collector",
                        "version": cortex_version or _cortex_version(),
                    }
                },
            })
        except (OSError, ValueError):
            return RawRead(None, "collector-write-failed")

        _, error = _read_response(proc.stdout, deadline, match_id=1)
        if error is not None:
            return RawRead(None, error)

        try:
            _write_json_line(proc.stdin, {"jsonrpc": "2.0", "method": "initialized"})
            _write_json_line(proc.stdin, {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"})
        except (OSError, ValueError):
            return RawRead(None, "collector-write-failed")

        response, error = _read_response(proc.stdout, deadline, match_id=2)
        if error is not None:
            return RawRead(None, error)
        result = response.get("result") if response is not None else None
        if not isinstance(result, dict):
            return RawRead(None, "collector-invalid-response")
        return RawRead({"result": result}, None)
    finally:
        _terminate_process(proc)


# ---------------------------------------------------------------------------
# agy：單次 argv 呼叫
# ---------------------------------------------------------------------------


def _default_run_agy(argv: tuple[str, ...], timeout_s: float) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - argv 來自 provider_read_contract()，非使用者輸入
        argv, capture_output=True, text=True, timeout=timeout_s
    )


def read_agy_quota(
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    run: Callable[[tuple[str, ...], float], subprocess.CompletedProcess] = _default_run_agy,
) -> RawRead:
    """依 ``provider_read_contract("agy")`` 的 argv 執行一次唯讀讀取。

    逾時交給 ``subprocess.run(timeout=...)`` 的標準終止/回收語意；非零 exit
    與壞 JSON 各自回精確 gap，不猜測 provider 的輸出形狀。
    """

    contract = provider_read_contract("agy")
    argv = contract.get("argv")
    if not isinstance(argv, tuple) or not argv:
        return RawRead(None, "collector-contract-missing-argv")
    try:
        completed = run(argv, timeout_s)
    except subprocess.TimeoutExpired:
        return RawRead(None, "collector-timeout")
    except OSError:
        return RawRead(None, "collector-spawn-failed")
    if completed.returncode != 0:
        return RawRead(None, "collector-nonzero-exit")
    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return RawRead(None, "collector-invalid-json")
    if not isinstance(payload, dict):
        return RawRead(None, "collector-invalid-json")
    return RawRead(payload, None)


# ---------------------------------------------------------------------------
# copilot／claude／cg：目前沒有可唯讀讀取的非互動介面
# ---------------------------------------------------------------------------


def read_copilot_quota() -> RawRead:
    """Copilot SDK 的 ``account.getQuota`` 是嵌入 Node/TS runtime 的正式介面，
    但 ``copilot`` CLI（2026-09-27 以 ``copilot --help`` 逐條核對其子命令：
    app/login/help/init/update/version/sessions/memories/plugin/mcp/skill/
    instruction/lsp/completion）沒有對應的非互動唯讀路徑；不猜測、不改用
    互動或會變更狀態的指令，直接回報 coverage gap。
    """

    return RawRead(None, "copilot-quota-read-path-unavailable")


# ---------------------------------------------------------------------------
# 單一 executor 的完整讀取＋解析流程
# ---------------------------------------------------------------------------


def collect_provider_quota(
    executor: str,
    *,
    profile_key: str,
    targets: tuple[ProviderQuotaTarget, ...],
    descriptors: tuple[schema.PoolDescriptor, ...],
    unit_catalog: tuple[schema.UnitDefinition, ...],
    observed_at_ms: int,
    ttl_ms: int = 300_000,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    cortex_version: str | None = None,
    spawn_codex: Callable[[], Any] | None = None,
    run_agy: Callable[[tuple[str, ...], float], subprocess.CompletedProcess] | None = None,
) -> ProviderCapture:
    """對單一 executor 執行唯讀讀取，交給 ``capture_provider_quota()`` 解析。

    claude／cg／未知 executor 沒有讀取動作，直接依 ``provider_read_contract()``
    既有的 unsupported／unknown 狀態回報（沿用既有 parser，不另寫第二套）。
    """

    if executor == "codex":
        raw: RawRead | None = read_codex_quota(
            cortex_version=cortex_version,
            timeout_s=timeout_s,
            spawn=spawn_codex or _default_spawn_codex,
        )
    elif executor == "agy":
        raw = read_agy_quota(timeout_s=timeout_s, run=run_agy or _default_run_agy)
    elif executor == "copilot":
        raw = read_copilot_quota()
    else:
        raw = None

    if raw is not None and raw.error is not None:
        reason = _coded_reason(raw.error, fallback="collector-read-failed")
        return _unknown_capture(
            executor, targets, profile_key, observed_at_ms, ttl_ms, reason, descriptors, unit_catalog
        )
    payload = raw.payload if raw is not None else None
    return capture_provider_quota(
        executor,
        payload,
        profile_key=profile_key,
        targets=targets,
        descriptors=descriptors,
        unit_catalog=unit_catalog,
        observed_at_ms=observed_at_ms,
        ttl_ms=ttl_ms,
    )


def append_captures_to_ledger(
    service: QuotaShadowService, captures: Sequence[ProviderCapture]
) -> str:
    """把多個 executor 的 capture 觀測寫入 ledger。

    路徑不可寫（例如多 UID 部署下執行者不是 Manager 帳號）時不崩潰，回報
    ``ledger-unwritable``，由呼叫端建議改用 ``--output``。
    """

    try:
        for capture in captures:
            for observation in capture.observations:
                service.ledger.append_observation(observation)
    except (LedgerCorrupt, OSError, ValueError):
        return "ledger-unwritable"
    return "written"


def observation_export_payload(
    observations: Sequence[schema.QuotaObservation], *, config_revision: str, generated_at_ms: int
) -> dict[str, Any]:
    """``--output`` 落地格式：只含 parser 產出的 observation wire dict，
    不含任何原始 provider payload 或帳號識別。"""

    return {
        "schema": OUTPUT_SCHEMA,
        "config_revision": config_revision,
        "generated_at_ms": generated_at_ms,
        "observations": [observation.to_dict() for observation in observations],
    }


# ---------------------------------------------------------------------------
# operator 設定檔：cortex/quota-pools/v1 + collector_targets
# ---------------------------------------------------------------------------


class CollectorConfigError(ValueError):
    """collector 設定檔格式、schema 版本或 target 對照錯誤。"""


@dataclass(frozen=True)
class CollectorTargetGroup:
    executor: str
    profile_key: str
    targets: tuple[ProviderQuotaTarget, ...]


@dataclass(frozen=True)
class CollectorConfig:
    config_revision: str
    descriptors: tuple[schema.PoolDescriptor, ...]
    unit_catalog: tuple[schema.UnitDefinition, ...]
    bindings: tuple[schema.ProfilePoolBinding, ...]
    lease_ms: int | None
    target_groups: tuple[CollectorTargetGroup, ...]

    def executors(self) -> tuple[str, ...]:
        seen: list[str] = []
        for group in self.target_groups:
            if group.executor not in seen:
                seen.append(group.executor)
        return tuple(seen)

    def groups_for(self, executor: str) -> tuple[CollectorTargetGroup, ...]:
        return tuple(group for group in self.target_groups if group.executor == executor)


def load_collector_config(path: str | Path) -> CollectorConfig:
    """讀取 ``cortex/quota-pools/v1`` 設定檔並解析選填的 ``collector_targets``。

    descriptors／unit_catalog／bindings 完全交給既有的
    ``quota_observation.parse_*`` 驗證；本函式只多解析 ``collector_targets``
    （每個 executor 的 ``ProviderQuotaTarget`` 規格），不另寫第二套 schema 檢查。
    路徑由呼叫端明確給定，本模組不讀任何預設路徑。
    """

    try:
        raw_text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CollectorConfigError(f"config-unreadable: {exc}") from exc
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CollectorConfigError(f"config-invalid-json: {exc}") from exc
    if not isinstance(payload, dict):
        raise CollectorConfigError("config-not-object")
    if payload.get("schema") != _CONFIG_SCHEMA:
        raise CollectorConfigError(f"config-schema-mismatch: expected {_CONFIG_SCHEMA}")
    config_revision = payload.get("config_revision")
    if not isinstance(config_revision, str) or not config_revision:
        raise CollectorConfigError("config-revision-missing")

    unit_catalog_raw = payload.get("unit_catalog", [])
    descriptors_raw = payload.get("descriptors", [])
    bindings_raw = payload.get("bindings", [])
    if not isinstance(unit_catalog_raw, list) or not isinstance(descriptors_raw, list) or not isinstance(
        bindings_raw, list
    ):
        raise CollectorConfigError("config-shape-invalid")

    try:
        unit_catalog = tuple(schema.parse_unit_definition(item) for item in unit_catalog_raw)
        descriptors = tuple(schema.parse_pool_descriptor(item) for item in descriptors_raw)
        bindings = tuple(schema.parse_binding(item, descriptors=descriptors) for item in bindings_raw)
    except (schema.QuotaContractError, TypeError, ValueError, KeyError) as exc:
        raise CollectorConfigError(f"config-schema-invalid: {exc}") from exc

    lease_ms = payload.get("lease_ms")
    if lease_ms is not None and (type(lease_ms) is not int or lease_ms <= 0):
        raise CollectorConfigError("config-lease-ms-invalid")

    target_groups = _parse_collector_targets(
        payload.get("collector_targets"), descriptors=descriptors, bindings=bindings
    )
    return CollectorConfig(
        config_revision=config_revision,
        descriptors=descriptors,
        unit_catalog=unit_catalog,
        bindings=bindings,
        lease_ms=lease_ms,
        target_groups=target_groups,
    )


def _parse_collector_targets(
    raw: object,
    *,
    descriptors: tuple[schema.PoolDescriptor, ...],
    bindings: tuple[schema.ProfilePoolBinding, ...],
) -> tuple[CollectorTargetGroup, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise CollectorConfigError("collector-targets-invalid")
    descriptor_by_ref = {descriptor.pool_ref: descriptor for descriptor in descriptors}
    grouped: dict[tuple[str, str], list[ProviderQuotaTarget]] = {}
    order: list[tuple[str, str]] = []
    for executor, items in raw.items():
        if not isinstance(executor, str) or not _EXECUTOR_RE.fullmatch(executor):
            raise CollectorConfigError(f"collector-targets-invalid-executor: {executor!r}")
        if not isinstance(items, list):
            raise CollectorConfigError(f"collector-targets-invalid: {executor}")
        for item in items:
            target, profile_key = _parse_collector_target_item(
                item, descriptor_by_ref=descriptor_by_ref, bindings=bindings
            )
            key = (executor, profile_key)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(target)
    return tuple(
        CollectorTargetGroup(executor=executor, profile_key=profile_key, targets=tuple(grouped[(executor, profile_key)]))
        for executor, profile_key in order
    )


def _parse_collector_target_item(
    item: object,
    *,
    descriptor_by_ref: Mapping[tuple[str, str, str, str], schema.PoolDescriptor],
    bindings: tuple[schema.ProfilePoolBinding, ...],
) -> tuple[ProviderQuotaTarget, str]:
    if not isinstance(item, dict):
        raise CollectorConfigError("collector-target-invalid")
    resource_key = item.get("resource_key")
    window_id = item.get("window_id")
    profile_key = item.get("profile_key")
    pool_ref_raw = item.get("pool_ref")
    if (
        not isinstance(resource_key, str)
        or not _RESOURCE_KEY_RE.fullmatch(resource_key)
        or not isinstance(window_id, str)
        or not isinstance(profile_key, str)
        or not _RESOLVED_PROFILE_KEY_RE.fullmatch(profile_key)
        or not isinstance(pool_ref_raw, dict)
    ):
        raise CollectorConfigError(f"collector-target-invalid: {resource_key!r}")
    pool_ref = tuple(pool_ref_raw.get(key) for key in ("authority_id", "account_id", "pool_id", "revision"))
    descriptor = descriptor_by_ref.get(pool_ref)  # type: ignore[arg-type]
    if descriptor is None:
        raise CollectorConfigError(f"collector-target-unknown-pool: {resource_key}")
    binding = _find_binding(bindings, pool_ref=pool_ref, window_id=window_id, profile_key=profile_key)
    if binding is None:
        raise CollectorConfigError(f"collector-target-no-binding: {resource_key}")
    return (
        ProviderQuotaTarget(resource_key=resource_key, binding=binding, descriptor=descriptor, window_id=window_id),
        profile_key,
    )


def _find_binding(
    bindings: tuple[schema.ProfilePoolBinding, ...],
    *,
    pool_ref: tuple[object, ...],
    window_id: str,
    profile_key: str,
) -> schema.ProfilePoolBinding | None:
    for binding in bindings:
        if schema.binding_status(binding).get("state") != "complete":
            continue
        wire = binding.to_dict()
        if not _binding_has_profile(wire, profile_key):
            continue
        for row in wire.get("constraints", []):
            if not isinstance(row, dict) or row.get("state") != "known":
                continue
            value = row.get("value")
            if not isinstance(value, dict):
                continue
            row_pool_ref = value.get("pool_ref", {})
            if not isinstance(row_pool_ref, dict):
                continue
            candidate = tuple(
                row_pool_ref.get(key) for key in ("authority_id", "account_id", "pool_id", "revision")
            )
            if candidate == pool_ref and value.get("window_id") == window_id:
                return binding
    return None
