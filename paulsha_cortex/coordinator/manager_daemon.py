from __future__ import annotations

import argparse
import fcntl
import inspect
import json
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from paulsha_cortex.config import paths
from ..control import constants, contract
from . import autonomy, manager, planning_runtime
from .cli import _refuse_unsafe_fanout, _resolve_launcher
from .dispatcher import Dispatcher
from .model_identities import load_model_identities
from .registry import JobRegistry
from .seams import ScriptWorktreeCreator, TmuxPaneSender
from .work_actions import safe_exception_summary

DEFAULT_TICK_INTERVAL = 300.0
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_PERSONA = "builder"
DEFAULT_EXECUTOR = "copilot"
DEFAULT_MAX_LOAD = 1.0
RECENT_DONE_LIMIT = 10
# issue #265：`recent_done` 需要 recency window 才不會被過期 manifest 永久佔滿名額。
# 24 小時是「一次 tick 週期 ~5 分鐘」與「operator 至少每天巡一次面板」之間的折衷值：
# 夠短，不會讓數天前的死屍霸占僅 10 個名額；夠長，跨夜、跨個位數 tick 失敗重試仍在窗內。
# 可用 PSC_MANAGER_RECENT_DONE_WINDOW_SECONDS 覆寫，見 README「觀察任務狀態」章節。
RECENT_DONE_WINDOW_SECONDS = 86400.0
MANAGER_CMD_MARKER = "paulsha_cortex.coordinator.manager_daemon"

# Periodic-tick failure resilience (issue #249): a persistently failing tick
# must never retry every poll cycle -- that turns a 5-minute schedule into a
# hot loop. Consecutive failures back off the next attempt exponentially
# (``tick_interval * BASE**min(failures, MAX_EXPONENT)``); once
# TICK_CIRCUIT_BREAKER_THRESHOLD consecutive failures accrue, periodic ticks
# pause entirely for TICK_CIRCUIT_BREAKER_COOLDOWN_SECONDS before one more
# attempt is made. Request-queue processing (including a manual "tick"
# request, the operator's rescue channel) is never gated by any of this.
TICK_BACKOFF_MULTIPLIER_BASE = 2.0
TICK_BACKOFF_MAX_EXPONENT = 4  # caps backoff at tick_interval * 2**4 = 16x
TICK_CIRCUIT_BREAKER_THRESHOLD = 6  # consecutive failures before ticks pause
TICK_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 3600.0  # cool-down before one retry

LOG_ERROR_SUMMARY_INTERVAL = 50  # suppressed-repeat summary cadence for _log_error
_MULTI_SEGMENT_PATH_PATTERN = re.compile(r"\S*/\S*/\S+")
TICK_ERROR_REASON_MAX_LENGTH = 200  # bounds status.json against a runaway message


@dataclass
class HeldLock:
    path: Path
    fd: int = -1

    def release(self) -> None:
        if self.fd >= 0:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.path.unlink(missing_ok=True)


def acquire_lock(
    *,
    path: Path | None = None,
    pid: int | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    now_fn: Callable[[], str] = contract.utcnow,
) -> HeldLock | None:
    """Acquire the single-instance lock via ``flock``.

    ``flock`` is held for the daemon's lifetime and released by the kernel on
    process death, so a stale lock file left by a crashed daemon is reclaimable
    with no manual liveness check or unlink — eliminating the check-then-unlink
    race a second contender could otherwise use to steal a live lock. ``pid``
    identifies the owner recorded in the file (for start.sh adoption); the
    ``pid_alive`` parameter is retained for API compatibility and is unused.
    """
    lock_path = path or constants.lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    owner_pid = os.getpid() if pid is None else pid
    payload = {
        "schema_version": constants.SCHEMA_VERSION,
        "pid": owner_pid,
        "acquired_at": now_fn(),
    }

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another live daemon holds the exclusive lock.
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(
            fd,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.fsync(fd)
    except OSError:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
        raise
    return HeldLock(lock_path, fd)


def default_specs_dir() -> str:
    override = os.environ.get("PSC_MANAGER_SPECS_DIR")
    if override:
        return override
    return str(paths.specs_root())


def default_executor() -> str:
    override = os.environ.get("PSC_MANAGER_EXECUTOR")
    if override:
        return override
    return DEFAULT_EXECUTOR


def default_tick_interval() -> float:
    override = os.environ.get("PSC_MANAGER_INTERVAL_SECONDS")
    if not override:
        return DEFAULT_TICK_INTERVAL
    try:
        return float(override)
    except ValueError:
        return DEFAULT_TICK_INTERVAL


def default_recent_done_window_seconds() -> float:
    override = os.environ.get("PSC_MANAGER_RECENT_DONE_WINDOW_SECONDS")
    if not override:
        return RECENT_DONE_WINDOW_SECONDS
    try:
        return float(override)
    except ValueError:
        return RECENT_DONE_WINDOW_SECONDS


def _parse_iso8601(value: Any) -> datetime | None:
    """寬容解析 ISO-8601 時間字串；解析失敗或非字串一律回傳 ``None``。

    ``recent_done`` 的新鮮度判斷必須保守：解析不出時間就無法證明「新鮮」，
    因此呼叫端會把 ``None`` 當成「排除在 window 之外」處理，而不是預設放行。

    ``recent_done`` 讀的是磁碟上任意來源的 JSON manifest（歷史檔、其他版本
    寫入的檔、手動放的檔），不能假設一定帶時區。``datetime.fromisoformat``
    對不帶時區的字串會回傳 naive datetime，而 naive datetime 的
    ``.timestamp()`` 是用**本機系統時區**解讀、不是 UTC——同一份 manifest
    在 UTC 與 UTC+8 主機上算出的 timestamp 會差到 8 小時，足以讓一份仍在
    window 內的 manifest 被誤判過期而剔除（#265 修法後續發現的邊界缺陷）。
    因此 naive datetime 一律視為 UTC 補上 tzinfo，避免新鮮度判定被執行主機
    的本地時區污染。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _tick_backoff_seconds(base_interval: float, consecutive_failures: int) -> float:
    """Exponential backoff interval for the next periodic-tick retry.

    Doubles per consecutive failure, capped at ``TICK_BACKOFF_MAX_EXPONENT``
    doublings, so a persistently failing tick backs off instead of retrying
    on every poll cycle. ``consecutive_failures <= 0`` (no prior failure)
    returns the unmodified ``base_interval``, preserving today's cadence.
    """
    if consecutive_failures <= 0:
        return base_interval
    exponent = min(consecutive_failures, TICK_BACKOFF_MAX_EXPONENT)
    return base_interval * (TICK_BACKOFF_MULTIPLIER_BASE**exponent)


def _safe_tick_error_summary(exc: Exception) -> dict[str, str]:
    """Build a redacted, length-capped last-failure summary for ``status.json``.

    Unlike the stderr log (operator-only, ephemeral), ``status.json`` is a
    persisted, always-on-disk artifact, so multi-segment filesystem paths
    (which could leak a machine's or user's directory layout) are replaced
    with a placeholder and the reason is capped to a bounded length.
    """
    reason = " ".join(str(exc).split())
    reason = _MULTI_SEGMENT_PATH_PATTERN.sub("<path>", reason)
    if len(reason) > TICK_ERROR_REASON_MAX_LENGTH:
        reason = reason[:TICK_ERROR_REASON_MAX_LENGTH].rstrip() + "…"
    return {"type": type(exc).__name__, "reason": reason}


def _in_flight_status(registry) -> list[dict[str, Any]]:
    in_flight = []
    for job in registry.list_jobs():
        status = job.get("status")
        if status not in manager.IN_FLIGHT_STATUSES:
            continue
        in_flight.append(
            {
                "job_id": job.get("job_id"),
                "slice_id": job.get("task"),
                "state": status,
            }
        )
    return in_flight


def _available_identity_candidates(identity_registry) -> str:
    identities = getattr(identity_registry, "identities", ())
    return ", ".join(
        f"{identity.executor}/{identity.model_id}"
        for identity in identities
        if isinstance(getattr(identity, "executor", None), str)
        and isinstance(getattr(identity, "model_id", None), str)
    ) or "(none)"


def _require_registered_identity(*, identity_registry, executor: str, model_id: str, context: str):
    identity = identity_registry.get(executor, model_id)
    if identity is None:
        available = _available_identity_candidates(identity_registry)
        raise ValueError(f"{context}: {executor}/{model_id}（可用 candidates: {available}）")
    return identity


def _call_with_supported_kwargs(func, *args, **kwargs):
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return func(*args, **kwargs)
    supported = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    }
    filtered_kwargs = {name: value for name, value in kwargs.items() if name in supported}
    return func(*args, **filtered_kwargs)


def _held_reasons(
    meta: dict[str, Any],
    is_satisfied: Callable[[str], bool],
    *,
    batch_ids: set[str],
    handoff_dir: str,
) -> list[str]:
    reasons: list[str] = []
    if isinstance(meta.get("parse_error"), dict):
        reasons.append(f"parse-error:{meta['parse_error'].get('field', 'frontmatter')}")
    if not (isinstance(meta.get("plan"), str) and meta["plan"]):
        reasons.append("no-plan")
    if meta.get("dispatch") != "auto":
        reasons.append("dispatch-hold")
    for dep in meta.get("depends_on", []):
        if not is_satisfied(dep):
            reason = autonomy.classify_batch_dependency(
                dep,
                batch_ids=batch_ids,
                handoff_dir=handoff_dir,
            )
            reasons.append(reason or f"deps-unsatisfied:{dep}")
    return reasons


def build_status_provider(
    *,
    registry,
    ready_provider: Callable[[], list[str]],
    recent_done_provider: Callable[[], list[dict[str, Any]]],
) -> Callable[[], dict[str, Any]]:
    def provider() -> dict[str, Any]:
        return {
            "ready": list(ready_provider()),
            "in_flight": _in_flight_status(registry),
            "recent_done": list(recent_done_provider()),
        }

    return provider


def build_runtime_status_provider(
    *,
    registry,
    specs_dir: str,
    handoff_dir: str,
    scan_specs_fn: Callable[[str], list[dict[str, Any]]] = autonomy.scan_specs,
    ready_units_fn: Callable[[list[dict[str, Any]], Callable[[str], bool]], list[dict[str, Any]]] = autonomy.ready_units,
    recent_done_limit: int = RECENT_DONE_LIMIT,
    recent_done_window_seconds: float | None = RECENT_DONE_WINDOW_SECONDS,
    now_fn: Callable[[], str] = contract.utcnow,
    git_runner=None,
) -> Callable[[], dict[str, Any]]:
    def recent_done_provider() -> list[dict[str, Any]]:
        manifests: list[tuple[str, dict[str, Any]]] = []
        handoff_path = Path(handoff_dir)
        if not handoff_path.is_dir():
            return []
        cutoff_ts: float | None = None
        if recent_done_window_seconds is not None:
            now_dt = _parse_iso8601(now_fn())
            if now_dt is not None:
                cutoff_ts = now_dt.timestamp() - recent_done_window_seconds
        for path in handoff_path.glob("*.json"):
            payload = contract.read_json(path)
            if not isinstance(payload, dict):
                continue
            completed_at = payload.get("completed_at")
            if cutoff_ts is not None:
                # window 內 0 筆時 recent_done 必須是空陣列，不得回退撈更舊的 manifest
                # （issue #265 驗收條件）；完全解析不出時間的 manifest 一律視為過期排除，
                # 不能因為「看不出新鮮度」就預設放行。
                completed_dt = _parse_iso8601(completed_at)
                if completed_dt is None or completed_dt.timestamp() < cutoff_ts:
                    continue
            manifests.append(
                (
                    str(completed_at or ""),
                    {
                        "slice_id": payload.get("slice_id"),
                        "gate_status": payload.get("gate_status"),
                        "at": completed_at,
                        "gate_reason": payload.get("gate_reason"),
                        "job_id": payload.get("job_id"),
                        "branch": payload.get("branch"),
                    },
                )
            )
        manifests.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in manifests[:recent_done_limit]]

    def provider() -> dict[str, Any]:
        metas = scan_specs_fn(specs_dir)
        predicate = lambda slice_id: autonomy.default_is_satisfied(
            slice_id,
            handoff_dir=handoff_dir,
            git_runner=git_runner,
        )
        batch_ids = {
            slice_id
            for meta in metas
            if isinstance((slice_id := meta.get("slice_id")), str) and slice_id
        }
        ready_units = ready_units_fn(metas, predicate)
        ready = [meta["slice_id"] for meta in ready_units]
        ready_ids = set(ready)
        held = []
        for meta in metas:
            slice_id = meta.get("slice_id")
            if not (isinstance(slice_id, str) and slice_id):
                continue
            if slice_id in ready_ids:
                continue
            reasons = _held_reasons(
                meta,
                predicate,
                batch_ids=batch_ids,
                handoff_dir=handoff_dir,
            )
            if reasons:
                held.append({"slice_id": slice_id, "reasons": reasons})
        slices: list[dict[str, Any]] = []
        attention: list[dict[str, Any]] = []
        list_slices = getattr(registry, "list_slices", None)
        if callable(list_slices):
            for slice_row in list_slices():
                if not isinstance(slice_row, dict):
                    continue
                entry = manager.slice_status_entry(
                    registry,
                    slice_row,
                    handoff_dir=handoff_dir,
                    git_runner=git_runner,
                )
                slices.append(entry)
                if entry.get("slice_state") == "needs_human":
                    attention.append(entry)
        return {
            "ready": ready,
            "held": held,
            "in_flight": _in_flight_status(registry),
            "recent_done": recent_done_provider(),
            "slices": slices,
            "attention": attention,
        }

    return provider


def build_request_executor(
    *,
    dispatcher,
    specs_dir: str,
    handoff_dir: str,
    launcher=None,
    default_persona: str = DEFAULT_PERSONA,
    default_executor: str = DEFAULT_EXECUTOR,
    default_model: str | None = None,
    default_review_executor: str | None = None,
    default_review_model: str | None = None,
    default_max_load: float = DEFAULT_MAX_LOAD,
    reaper=None,
    scan_specs_fn: Callable[[str], list[dict[str, Any]]] = autonomy.scan_specs,
    run_tick_fn: Callable[..., dict[str, Any]] = manager.run_tick,
    dispatch_ready_fn: Callable[..., list[dict[str, Any]]] = autonomy.dispatch_ready,
    workflow_identity_registry=None,
    workflow_probes=None,
    workflow_primary_questioner=None,
    workflow_secondary_planner=None,
    workflow_primary_integrator=None,
    workflow_runtime_factory=None,
    workflow_ship_validator=None,
    work_action_fn: Callable[..., dict[str, Any]] | None = None,
    stage_evidence_validator: Callable[[dict[str, str]], bool] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def execute(request: dict[str, Any]) -> dict[str, Any]:
        args = request.get("args", {})
        request_specs_dir = args.get("specs_dir") or specs_dir
        request_handoff_dir = args.get("handoff_dir") or handoff_dir
        predicate = lambda slice_id: autonomy.default_is_satisfied(
            slice_id,
            handoff_dir=request_handoff_dir,
            git_runner=getattr(dispatcher, "_git_runner", None),
        )
        allow_unsafe = bool(args.get("allow_unsafe", False))
        persona = args.get("persona", default_persona)
        requested_model = args.get("model", default_model)
        requested_review_executor = args.get("review_executor", default_review_executor)
        requested_review_model = args.get("review_model", default_review_model)
        request_type = request["type"]
        active_review_launcher = _resolve_launcher(
            requested_review_executor,
            launcher,
            allow_unsafe=allow_unsafe,
            model=requested_review_model,
        )
        builder_identity_registry = None
        builder_launcher_factory = None
        if request_type in {"dispatch", "fanout", "tick"}:
            builder_identity_registry = workflow_identity_registry or load_model_identities()
            builder_launcher_factory = lambda identity: _resolve_launcher(
                identity.executor,
                launcher,
                allow_unsafe=allow_unsafe,
                model=identity.model_id,
            )
            requested_executor = args.get("executor", default_executor)
            if isinstance(requested_executor, str) and requested_executor and isinstance(requested_model, str) and requested_model:
                _require_registered_identity(
                    identity_registry=builder_identity_registry,
                    executor=requested_executor,
                    model_id=requested_model,
                    context=f"{request_type} request 指定的 identity 不存在於 registry",
                )
        if request_type == "workflow-action":
            registry = getattr(dispatcher, "_registry", None)
            if registry is None:
                raise RuntimeError("workflow-action requires dispatcher._registry")
            identities = workflow_identity_registry or load_model_identities()
            state_path = getattr(registry, "_state_path", None)
            coordinator_root = (
                Path(state_path).resolve().parent
                if state_path is not None
                else paths.coordinator_root().resolve()
            )
            launcher_factory = lambda identity: _resolve_launcher(
                identity.executor,
                launcher,
                allow_unsafe=False,
                model=identity.model_id,
            )
            if args.get("action") == "resume":
                extras = set(args) - {"action", "run_id"}
                if extras:
                    raise ValueError(
                        f"workflow resume rejects caller evidence/input: {sorted(extras)[0]}"
                    )
                resume_run = registry.get_workflow_run(str(args.get("run_id") or ""))
                active_ship_validator = workflow_ship_validator
                if (
                    active_ship_validator is None
                    and resume_run.claim_key.startswith("claim:v1:")
                    and len(resume_run.source_revision) == 64
                ):
                    from .work_bridge import build_production_ship_validator

                    active_ship_validator = build_production_ship_validator(
                        registry=registry,
                        coordinator_root=coordinator_root,
                    )
                return manager.resume_workflow_run(
                    dispatcher,
                    run_id=str(args.get("run_id") or ""),
                    identities=identities,
                    launcher_factory=launcher_factory,
                    coordinator_root=coordinator_root,
                    ship_validator=active_ship_validator,
                    operator_resume=True,
                )
            result = manager.apply_workflow_action(
                registry,
                args=args,
                identity_registry=identities,
                probes=workflow_probes,
                primary_questioner=workflow_primary_questioner,
                secondary_planner=workflow_secondary_planner,
                primary_integrator=workflow_primary_integrator,
                runtime_factory=workflow_runtime_factory,
                ship_validator=workflow_ship_validator,
                git_runner=getattr(dispatcher, "_git_runner", None),
                coordinator_root=coordinator_root,
            )
            if args.get("action") == "start":
                run = registry.get_workflow_run(str(result["run_id"]))
                # #214：呼叫端可預先算好本次 stage 的 content-addressed
                # execution key；若既有 job 用同一把 key 留下完整且仍然有效
                # 的 evidence（由 stage_evidence_validator 判定），就直接
                # reuse，短路掉這個「唯一觸發處」，不重啟 model session。
                # 沒帶 key、或找不到可 reuse 的 evidence 時，行為與過去完全
                # 相同（fail-closed：預設照樣派新 job）。
                stage_execution_key = args.get("stage_execution_key")
                reused_from = None
                if isinstance(stage_execution_key, str) and stage_execution_key:
                    reused_from = registry.find_reusable_stage_evidence(
                        stage_execution_key,
                        is_evidence_still_valid=stage_evidence_validator,
                    )
                if reused_from is not None:
                    result["reused_from"] = reused_from
                else:
                    job = manager.dispatch_workflow_card(
                        dispatcher,
                        run=run,
                        identities=identities,
                        launcher_factory=launcher_factory,
                        coordinator_root=coordinator_root,
                    )
                    if job is not None:
                        result["job_id"] = job["job_id"]
            return result
        if request_type == "work-action":
            registry = getattr(dispatcher, "_registry", None)
            if registry is None:
                raise RuntimeError("work-action requires dispatcher._registry")
            if work_action_fn is None:
                result = manager.apply_work_action(
                    args=dict(args),
                    requested_by=request["requested_by"],
                    registry=registry,
                    runtime_factory=(
                        workflow_runtime_factory
                        or planning_runtime.build_production_planning_runtime
                    ),
                )
            else:
                result = work_action_fn(
                    args=dict(args),
                    requested_by=request["requested_by"],
                )
            if args.get("action") in {"start", "resume", "retry-build"}:
                registry = getattr(dispatcher, "_registry", None)
                run_payload = result.get("result", {}).get("run") if isinstance(result, dict) else None
                run_id = run_payload.get("run_id") if isinstance(run_payload, dict) else None
                if registry is not None and isinstance(run_id, str):
                    run = registry.get_workflow_run(run_id)
                    identities = workflow_identity_registry or load_model_identities()
                    state_path = getattr(registry, "_state_path", None)
                    coordinator_root = (
                        Path(state_path).resolve().parent
                        if state_path is not None
                        else paths.coordinator_root().resolve()
                    )
                    launcher_factory = lambda identity: _resolve_launcher(
                        identity.executor,
                        launcher,
                        allow_unsafe=False,
                        model=identity.model_id,
                    )
                    if args.get("action") == "resume" and run.current_phase in {
                        "plan",
                        "build",
                        "verify",
                        "review",
                        "ship",
                    }:
                        active_ship_validator = workflow_ship_validator
                        if (
                            active_ship_validator is None
                            and run.claim_key.startswith("claim:v1:")
                            and len(run.source_revision) == 64
                        ):
                            from .work_bridge import build_production_ship_validator

                            active_ship_validator = build_production_ship_validator(
                                registry=registry,
                                coordinator_root=coordinator_root,
                            )
                        resumed = manager.resume_workflow_run(
                            dispatcher,
                            run_id=run.run_id,
                            identities=identities,
                            launcher_factory=launcher_factory,
                            coordinator_root=coordinator_root,
                            ship_validator=active_ship_validator,
                            operator_resume=True,
                        )
                        result["result"].update(resumed)
                        result["result"]["run"] = registry.get_workflow_run(
                            run.run_id
                        ).to_dict()
                    else:
                        try:
                            job = manager.dispatch_workflow_card(
                                dispatcher,
                                run=run,
                                identities=identities,
                                launcher_factory=launcher_factory,
                                coordinator_root=coordinator_root,
                                force_new_build=args.get("action") == "retry-build",
                            )
                            if args.get("action") == "retry-build" and job is None:
                                raise RuntimeError("retry-build produced no builder Job")
                        except Exception:
                            if args.get("action") == "retry-build":
                                current = registry.get_workflow_run(run.run_id)
                                registry._manager_update_workflow_run(
                                    run.run_id,
                                    facets=tuple(
                                        dict.fromkeys((*current.facets, "needs_human"))
                                    ),
                                    gate_status="running",
                                )
                            raise
                        if job is not None:
                            result["result"]["job_id"] = job["job_id"]
            return result
        if request_type == "complete":
            complete_metas = scan_specs_fn(request_specs_dir) if args.get("specs_dir") else None
            complete_kwargs = {
                "handoff_dir": request_handoff_dir,
                "metas": complete_metas,
            }
            if requested_review_executor is not None or requested_review_model is not None:
                complete_kwargs.update(
                    {
                        "review_launcher": active_review_launcher,
                        "review_executor": requested_review_executor,
                        "review_model": requested_review_model,
                    }
                )
            return manager.complete_tick(
                dispatcher,
                **complete_kwargs,
            )
        if request_type == "slice-action":
            slice_id = args.get("slice_id")
            action = args.get("action")
            actor = args.get("actor")
            if not isinstance(slice_id, str) or not slice_id:
                raise ValueError("slice-action requires slice_id")
            if not isinstance(action, str) or not action:
                raise ValueError("slice-action requires action")
            if not isinstance(actor, str) or not actor:
                raise ValueError("slice-action requires actor")
            return manager.apply_slice_action(
                dispatcher,
                slice_id=slice_id,
                action=action,
                actor=actor,
                specs_dir=request_specs_dir,
                handoff_dir=request_handoff_dir,
                launcher=_resolve_launcher(
                    args.get("executor", default_executor),
                    launcher,
                    allow_unsafe=allow_unsafe,
                    model=requested_model,
                ),
                review_launcher=active_review_launcher,
                persona=persona,
                review_executor=requested_review_executor,
                review_model=requested_review_model,
                git_runner=getattr(dispatcher, "_git_runner", None),
            )
        metas = scan_specs_fn(request_specs_dir)
        if request_type == "dispatch":
            slice_id = args.get("slice_id")
            target = next((meta for meta in metas if meta.get("slice_id") == slice_id), None)
            if target is None:
                raise ValueError("unknown-slice")
            if isinstance(target.get("parse_error"), dict):
                raise ValueError(f"invalid-spec:{target['parse_error'].get('field')}")
            if not (
                isinstance(target.get("target_branch"), str)
                and target["target_branch"]
                and isinstance(target.get("verification"), dict)
            ):
                raise ValueError("missing-verification-contract")
            if not (isinstance(target.get("plan"), str) and target["plan"]):
                raise ValueError("no-plan")
            missing_deps = [dep for dep in target.get("depends_on", []) if not predicate(dep)]
            if missing_deps:
                raise ValueError(f"deps-unsatisfied: {', '.join(missing_deps)}")
            force_hold = bool(args.get("force_hold", False))
            if target.get("dispatch") != "auto" and not force_hold:
                raise ValueError("dispatch-hold")
            registry = getattr(dispatcher, "_registry", None)
            if registry is None:
                raise RuntimeError("dispatch requires registry for already-active guard")
            if any(
                job.get("task") == slice_id and job.get("status") in manager.IN_FLIGHT_STATUSES
                for job in registry.list_jobs()
            ):
                raise ValueError("already-active")
            active_launcher = _resolve_launcher(
                args.get("executor", default_executor),
                launcher,
                allow_unsafe=allow_unsafe,
                model=requested_model,
            )
            dispatched = _call_with_supported_kwargs(
                dispatch_ready_fn,
                [{**target, "dispatch": "auto"}],
                lambda _slice_id: True,
                dispatcher,
                persona=persona,
                launcher=active_launcher,
                handoff_dir=request_handoff_dir,
                git_runner=getattr(dispatcher, "_git_runner", None),
                identity_registry=builder_identity_registry,
                launcher_factory=builder_launcher_factory,
            )
            job = dispatched[0]
            if registry is None:
                raise RuntimeError("dispatch requires registry for pinned slice metadata")
            slice_row = registry.get_slice(slice_id)
            result = {
                "job_id": job.get("job_id"),
                "worktree": job.get("worktree"),
                "branch": job.get("branch"),
                "slice_id": slice_id,
                "target_branch": slice_row.get("target_branch"),
                "target_remote": slice_row.get("target_remote"),
                "spec_hash": slice_row.get("spec", {}).get("hash"),
                "plan_hash": slice_row.get("plan", {}).get("hash"),
                "verification_hash": slice_row.get("verification", {}).get("hash"),
            }
            if force_hold and target.get("dispatch") != "auto":
                result["override"] = "hold"
                result["requested_by"] = request["requested_by"]
            return result
        _refuse_unsafe_fanout(metas, predicate, allow_unsafe=allow_unsafe)
        active_launcher = _resolve_launcher(
            args.get("executor", default_executor),
            launcher,
            allow_unsafe=allow_unsafe,
            model=requested_model,
        )
        if request_type == "fanout":
            jobs = _call_with_supported_kwargs(
                dispatch_ready_fn,
                metas,
                predicate,
                dispatcher,
                persona=persona,
                launcher=active_launcher,
                handoff_dir=request_handoff_dir,
                git_runner=getattr(dispatcher, "_git_runner", None),
                identity_registry=builder_identity_registry,
                launcher_factory=builder_launcher_factory,
            )
            return {
                "dispatch_skipped": False,
                "dispatched": jobs,
                "completed": [],
                "errors": [],
                "reaped": None,
            }
        run_tick_kwargs = {
            "metas": metas,
            "launcher": active_launcher,
            "persona": persona,
            "is_satisfied": predicate,
            "handoff_dir": request_handoff_dir,
            "require_idle": bool(args.get("require_idle", False)),
            "max_load": float(args.get("max_load", default_max_load)),
            "reaper": reaper,
            "identity_registry": builder_identity_registry,
            "launcher_factory": builder_launcher_factory,
        }
        if requested_review_executor is not None or requested_review_model is not None:
            run_tick_kwargs.update(
                {
                    "review_launcher": active_review_launcher,
                    "review_executor": requested_review_executor,
                    "review_model": requested_review_model,
                }
            )
        return _call_with_supported_kwargs(
            run_tick_fn,
            dispatcher,
            **run_tick_kwargs,
        )

    return execute


def build_periodic_tick_runner(
    *,
    dispatcher,
    specs_dir: str,
    handoff_dir: str,
    launcher=None,
    default_persona: str = DEFAULT_PERSONA,
    default_executor: str = DEFAULT_EXECUTOR,
    default_model: str | None = None,
    default_allow_unsafe: bool = False,
    default_max_load: float = DEFAULT_MAX_LOAD,
    require_idle: bool = True,
    reaper=None,
    default_review_executor: str | None = None,
    default_review_model: str | None = None,
    scan_specs_fn: Callable[[str], list[dict[str, Any]]] = autonomy.scan_specs,
    run_tick_fn: Callable[..., dict[str, Any]] = manager.run_tick,
    auto_claim_fn: Callable[[], list[dict[str, Any]]] | None = None,
    workflow_identity_registry=None,
    workflow_ship_validator=None,
) -> Callable[[], dict[str, Any]]:
    predicate = lambda slice_id: autonomy.default_is_satisfied(
        slice_id,
        handoff_dir=handoff_dir,
        git_runner=getattr(dispatcher, "_git_runner", None),
    )

    def execute() -> dict[str, Any]:
        identities = workflow_identity_registry or load_model_identities()
        workflow_launcher_factory = lambda identity: _resolve_launcher(
            identity.executor,
            launcher,
            allow_unsafe=False,
            model=identity.model_id,
        )
        registry = getattr(dispatcher, "_registry", None)
        auto_claim_error: str | None = None
        try:
            auto_claims = (
                auto_claim_fn()
                if auto_claim_fn is not None
                else manager.run_auto_claim_scan(
                    registry=registry,
                    runtime_factory=planning_runtime.build_production_planning_runtime,
                )
            )
        except (ValueError, RuntimeError, OSError) as exc:
            # #246 daemon tick isolation：auto-claim 這個子系統整批失效（例如
            # 快照載入本身壞掉）不得癱瘓本輪 tick——降級為空 auto_claims，
            # 讓後面的 workflow resume 迴圈與 run_tick 照常執行；
            # KeyboardInterrupt/SystemExit 不在攔截範圍，照常往外傳。
            _log_error(exc, context={"action": "auto-claim-scan"})
            auto_claims = []
            auto_claim_error = safe_exception_summary(exc)
        if registry is not None and hasattr(registry, "list_workflow_runs"):
            state_path = getattr(registry, "_state_path", None)
            coordinator_root = (
                Path(state_path).resolve().parent
                if state_path is not None
                else paths.coordinator_root().resolve()
            )
            for workflow in registry.list_workflow_runs():
                if workflow.status != "ongoing" or "blocked" in workflow.facets:
                    continue
                if workflow.current_phase not in {"plan", "build", "verify", "review"}:
                    continue
                try:
                    active_ship_validator = workflow_ship_validator
                    if (
                        active_ship_validator is None
                        and workflow.claim_key.startswith("claim:v1:")
                        and len(workflow.source_revision) == 64
                    ):
                        from .work_bridge import build_production_ship_validator

                        active_ship_validator = build_production_ship_validator(
                            registry=registry,
                            coordinator_root=coordinator_root,
                        )
                    manager.resume_workflow_run(
                        dispatcher,
                        run_id=workflow.run_id,
                        identities=identities,
                        launcher_factory=workflow_launcher_factory,
                        coordinator_root=coordinator_root,
                        ship_validator=active_ship_validator,
                    )
                except Exception as exc:
                    _log_error(
                        exc,
                        context={
                            "action": "resume-workflow",
                            "work_id": workflow.work_id,
                            "repo": workflow.repo,
                            "source_revision": workflow.source_revision,
                        },
                    )
                    registry._manager_update_workflow_run(
                        workflow.run_id,
                        facets=("needs_human",),
                        gate_status="running",
                    )
        metas = scan_specs_fn(specs_dir)
        _refuse_unsafe_fanout(metas, predicate, allow_unsafe=default_allow_unsafe)
        if (
            isinstance(default_executor, str)
            and default_executor
            and isinstance(default_model, str)
            and default_model
        ):
            _require_registered_identity(
                identity_registry=identities,
                executor=default_executor,
                model_id=default_model,
                context="periodic tick 預設 identity 不存在於 registry",
            )
        active_launcher = _resolve_launcher(
            default_executor,
            launcher,
            allow_unsafe=default_allow_unsafe,
            model=default_model,
        )
        active_review_launcher = _resolve_launcher(
            default_review_executor,
            launcher,
            allow_unsafe=default_allow_unsafe,
            model=default_review_model,
        )
        run_tick_kwargs = {
            "metas": metas,
            "launcher": active_launcher,
            "persona": default_persona,
            "is_satisfied": predicate,
            "handoff_dir": handoff_dir,
            "require_idle": require_idle,
            "max_load": default_max_load,
            "reaper": reaper,
            "identity_registry": identities,
            "launcher_factory": lambda identity: _resolve_launcher(
                identity.executor,
                launcher,
                allow_unsafe=default_allow_unsafe,
                model=identity.model_id,
            ),
        }
        if default_review_executor is not None or default_review_model is not None:
            run_tick_kwargs.update(
                {
                    "review_launcher": active_review_launcher,
                    "review_executor": default_review_executor,
                    "review_model": default_review_model,
                }
            )
        result = _call_with_supported_kwargs(run_tick_fn, dispatcher, **run_tick_kwargs)
        summary = {**result, "auto_claims": auto_claims}
        if auto_claim_error is not None:
            # 讓 operator 能從 status/summary 看出 auto-claim 這輪降級了，
            # 而不是靜默吞掉——不含絕對路徑／token，只有型別＋訊息摘要。
            summary["auto_claim_failed"] = True
            summary["auto_claim_error"] = auto_claim_error
        return summary

    return execute


def run_loop(
    *,
    request_executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    status_provider: Callable[[], dict[str, Any]] | None = None,
    periodic_tick_runner: Callable[[], dict[str, Any]] | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    tick_interval: float = DEFAULT_TICK_INTERVAL,
    now_fn: Callable[[], str] = contract.utcnow,
    monotonic_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    pid: int | None = None,
    pid_alive: Callable[[int], bool] | None = None,
    max_rounds: int | None = None,
    specs_dir: str | None = None,
    handoff_dir: str = autonomy.DEFAULT_HANDOFF_DIR,
    registry: JobRegistry | None = None,
    dispatcher: Dispatcher | None = None,
    launcher=None,
    require_idle: bool = True,
    default_executor: str | None = None,
    default_model: str | None = None,
    default_review_executor: str | None = None,
    default_review_model: str | None = None,
    reaper: Callable[[], dict[str, Any]] | None = None,
    recent_done_window_seconds: float | None = RECENT_DONE_WINDOW_SECONDS,
) -> bool:
    runtime_pid = os.getpid() if pid is None else pid
    held_lock = acquire_lock(pid=runtime_pid, pid_alive=pid_alive, now_fn=now_fn)
    if held_lock is None:
        return False

    resolved_specs_dir = specs_dir or default_specs_dir()
    resolved_default_executor = default_executor or DEFAULT_EXECUTOR
    reg = registry
    disp = dispatcher

    def ensure_registry() -> JobRegistry:
        nonlocal reg
        if reg is None:
            reg = JobRegistry()
        return reg

    def ensure_dispatcher() -> Dispatcher:
        nonlocal disp
        if disp is None:
            disp = Dispatcher(ensure_registry(), TmuxPaneSender(), ScriptWorktreeCreator())
        return disp

    if request_executor is not None:
        executor = request_executor
    else:
        executor_kwargs = {
            "dispatcher": ensure_dispatcher(),
            "specs_dir": resolved_specs_dir,
            "handoff_dir": handoff_dir,
            "launcher": launcher,
            "default_executor": resolved_default_executor,
            "reaper": reaper,
            "workflow_runtime_factory": planning_runtime.build_production_planning_runtime,
        }
        if default_model is not None:
            executor_kwargs["default_model"] = default_model
        if default_review_executor is not None or default_review_model is not None:
            executor_kwargs.update(
                {
                    "default_review_executor": default_review_executor,
                    "default_review_model": default_review_model,
                }
            )
        executor = build_request_executor(**executor_kwargs)
    provider = status_provider or build_runtime_status_provider(
        registry=ensure_registry(),
        specs_dir=resolved_specs_dir,
        handoff_dir=handoff_dir,
        git_runner=getattr(ensure_dispatcher(), "_git_runner", None),
        recent_done_window_seconds=recent_done_window_seconds,
        now_fn=now_fn,
    )
    if periodic_tick_runner is not None:
        periodic_runner = periodic_tick_runner
    else:
        periodic_kwargs = {
            "dispatcher": ensure_dispatcher(),
            "specs_dir": resolved_specs_dir,
            "handoff_dir": handoff_dir,
            "launcher": launcher,
            "require_idle": require_idle,
            "default_executor": resolved_default_executor,
            "reaper": reaper,
        }
        if default_model is not None:
            periodic_kwargs["default_model"] = default_model
        if default_review_executor is not None or default_review_model is not None:
            periodic_kwargs.update(
                {
                    "default_review_executor": default_review_executor,
                    "default_review_model": default_review_model,
                }
            )
        periodic_runner = build_periodic_tick_runner(**periodic_kwargs)

    constants.requests_dir().mkdir(parents=True, exist_ok=True)
    constants.done_dir().mkdir(parents=True, exist_ok=True)

    last_tick_at: str | None = None
    daemon_idle = True
    last_tick_monotonic = monotonic_fn()
    rounds = 0
    consecutive_tick_failures = 0
    tick_circuit_open = False
    last_tick_error: dict[str, str] | None = None
    _reset_log_error_dedup_state()

    try:
        while max_rounds is None or rounds < max_rounds:
            tick_ran_this_round = False
            request_drain_interrupted = False
            request_paths = sorted(constants.requests_dir().glob("*.json"), key=_request_sort_key)
            for request_path in request_paths:
                if not request_path.exists():
                    continue
                request_id = request_path.stem
                done_path = constants.done_dir() / f"{request_id}.json"
                if done_path.exists():
                    try:
                        request_path.unlink(missing_ok=True)
                    except Exception as exc:  # noqa: BLE001
                        _log_error(exc)
                        request_drain_interrupted = True
                        break
                    continue

                started_at = now_fn()
                try:
                    try:
                        request = _load_request(request_path)
                    except FileNotFoundError:
                        continue
                    summary = executor(request)
                    done_payload = contract.build_done(
                        req_id=request["req_id"],
                        status="ok",
                        result=summary,
                        started_at=started_at,
                    )
                    skipped = isinstance(summary, dict) and summary.get("dispatch_skipped") == "not-idle"
                    daemon_idle = not skipped
                    if request["type"] == "tick" and not skipped:
                        last_tick_at = now_fn()
                        last_tick_monotonic = monotonic_fn()
                        tick_ran_this_round = True
                        # A successful manual tick is the operator's rescue
                        # channel for a tripped circuit breaker: clear the
                        # periodic path's failure bookkeeping so it gets a
                        # fresh normal-cadence attempt next round.
                        consecutive_tick_failures = 0
                        tick_circuit_open = False
                        last_tick_error = None
                except Exception as exc:  # noqa: BLE001
                    done_payload = contract.build_done(
                        req_id=request_id,
                        status="error",
                        error=f"{type(exc).__name__}: {exc}",
                        started_at=started_at,
                    )
                    _log_error(exc)
                try:
                    _persist_done(done_payload)
                    request_path.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    _log_error(exc)
                    request_drain_interrupted = True
                    break

            if tick_circuit_open:
                effective_tick_interval = TICK_CIRCUIT_BREAKER_COOLDOWN_SECONDS
            else:
                effective_tick_interval = _tick_backoff_seconds(tick_interval, consecutive_tick_failures)

            if (
                not request_drain_interrupted
                and not tick_ran_this_round
                and monotonic_fn() - last_tick_monotonic >= effective_tick_interval
            ):
                try:
                    summary = periodic_runner()
                    skipped = isinstance(summary, dict) and summary.get("dispatch_skipped") == "not-idle"
                    daemon_idle = not skipped
                    if not skipped:
                        last_tick_at = now_fn()
                        last_tick_monotonic = monotonic_fn()
                        consecutive_tick_failures = 0
                        tick_circuit_open = False
                        last_tick_error = None
                except Exception as exc:  # noqa: BLE001
                    _log_error(exc)
                    # Failure must still advance the clock -- this is the
                    # core fix for #249: the pre-fix code left
                    # last_tick_monotonic untouched on failure, so the next
                    # poll cycle (3s later) immediately re-triggered the
                    # same failing tick forever instead of waiting out
                    # tick_interval (or the backed-off interval below).
                    consecutive_tick_failures += 1
                    last_tick_error = _safe_tick_error_summary(exc)
                    last_tick_monotonic = monotonic_fn()
                    if consecutive_tick_failures >= TICK_CIRCUIT_BREAKER_THRESHOLD:
                        tick_circuit_open = True

            try:
                snapshot = provider()
                status_payload = contract.build_status(
                    ready=list(snapshot.get("ready", [])),
                    in_flight=list(snapshot.get("in_flight", [])),
                    recent_done=list(snapshot.get("recent_done", [])),
                    daemon={
                        "pid": runtime_pid,
                        "last_tick_at": last_tick_at,
                        "idle": daemon_idle,
                        "consecutive_tick_failures": consecutive_tick_failures,
                        "tick_circuit_open": tick_circuit_open,
                        "last_tick_error": last_tick_error,
                    },
                    updated_at=now_fn(),
                )
                status_payload["held"] = list(snapshot.get("held", []))
                status_payload["slices"] = list(snapshot.get("slices", []))
                status_payload["attention"] = list(snapshot.get("attention", []))
                contract.atomic_write_json(constants.status_path(), status_payload)
            except Exception as exc:  # noqa: BLE001
                _log_error(exc)

            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
            if poll_interval > 0:
                sleep_fn(poll_interval)
    finally:
        held_lock.release()

    return True


def _load_request(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request payload must be an object")
    return contract.validate_request(payload)


def _persist_done(payload: dict[str, Any]) -> dict[str, Any]:
    done_path = constants.done_dir() / f"{payload['req_id']}.json"
    existing = contract.read_json(done_path)
    if existing is not None:
        return existing
    contract.atomic_write_json(done_path, payload)
    return payload


def _request_sort_key(path: Path) -> tuple[int, str]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return (sys.maxsize, path.name)
    return (stat.st_mtime_ns, path.name)


def _lock_is_live(path: Path, pid_alive: Callable[[int], bool]) -> bool:
    payload = contract.read_json(path)
    if not isinstance(payload, dict):
        return False
    owner_pid = payload.get("pid")
    if not isinstance(owner_pid, int):
        return False
    return pid_alive(owner_pid)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    try:
        raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    argv = [part.decode("utf-8", errors="ignore") for part in raw_cmdline if part]
    return any(
        argv[index] == "-m" and argv[index + 1] == MANAGER_CMD_MARKER
        for index in range(len(argv) - 1)
    )


def _handle_termination(_signum: int, _frame: object) -> None:
    raise SystemExit(0)


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_termination)
    signal.signal(signal.SIGINT, _handle_termination)


# Module-level state for _log_error's repeat suppression (issue #249: the
# same ValueError printed 17,013 times over 22 hours because _log_error had
# zero de-duplication). Reset once per daemon run via
# _reset_log_error_dedup_state() -- called at the top of run_loop -- so a
# fresh daemon process (or a fresh test invocation of run_loop) never
# inherits suppression counters from a previous run.
_LOG_ERROR_DEDUP_STATE: dict[str, Any] = {"signature": None, "repeat_count": 0}


def _reset_log_error_dedup_state() -> None:
    _LOG_ERROR_DEDUP_STATE["signature"] = None
    _LOG_ERROR_DEDUP_STATE["repeat_count"] = 0


def _log_error(exc: Exception, *, context: dict[str, Any] | None = None) -> None:
    """Print a manager_daemon error to stderr, with safe context and repeat suppression.

    ``context`` (#246) carries only safe diagnostic scope -- action kind,
    work_id, repo, snapshot/source revision; callers must not pass tokens,
    absolute paths, or issue bodies.

    Repeat suppression (#249): the first occurrence of a signature
    (exception type + message + context) is always printed in full.
    Consecutive repeats of the exact same signature are suppressed and
    replaced by a periodic "still repeating" summary every
    LOG_ERROR_SUMMARY_INTERVAL occurrences, so a persistently failing tick
    can no longer flood the log the way it did in #249 -- while still
    leaving evidence that the failure is ongoing.
    """
    timestamp = contract.utcnow()
    signature = f"{type(exc).__name__}: {exc}"
    if context:
        details = " ".join(f"{key}={value}" for key, value in context.items() if value is not None)
        if details:
            signature = f"{signature} ({details})"
    state = _LOG_ERROR_DEDUP_STATE
    if state["signature"] != signature:
        state["signature"] = signature
        state["repeat_count"] = 0
        print(f"{timestamp} manager_daemon error: {signature}", file=sys.stderr)
        return
    state["repeat_count"] += 1
    if state["repeat_count"] % LOG_ERROR_SUMMARY_INTERVAL == 0:
        print(
            f"{timestamp} manager_daemon error (repeated {state['repeat_count']}x since "
            f"first occurrence, most recent at {timestamp}): {signature}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PaulShiaBro manager daemon")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--tick-interval", type=float, default=default_tick_interval())
    parser.add_argument("--executor", default=default_executor())
    parser.add_argument("--model", default=None)
    parser.add_argument("--review-executor", default=None)
    parser.add_argument("--review-model", default=None)
    parser.add_argument("--specs-dir")
    parser.add_argument("--handoff-dir", default=autonomy.DEFAULT_HANDOFF_DIR)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--no-require-idle", action="store_true")
    parser.add_argument(
        "--recent-done-window-seconds",
        type=float,
        default=default_recent_done_window_seconds(),
        help=(
            "status.json 的 recent_done 只回溯這麼多秒內完成的 manifest（issue #265）；"
            "預設 86400（24 小時），亦可用 PSC_MANAGER_RECENT_DONE_WINDOW_SECONDS 覆寫。"
        ),
    )
    args = parser.parse_args(argv)
    _install_signal_handlers()

    started = run_loop(
        poll_interval=args.poll_interval,
        tick_interval=args.tick_interval,
        specs_dir=args.specs_dir,
        handoff_dir=args.handoff_dir,
        max_rounds=args.max_rounds,
        require_idle=not args.no_require_idle,
        default_executor=args.executor,
        default_model=args.model,
        default_review_executor=args.review_executor,
        default_review_model=args.review_model,
        recent_done_window_seconds=args.recent_done_window_seconds,
    )
    return 0 if started else 1


if __name__ == "__main__":
    raise SystemExit(main())
