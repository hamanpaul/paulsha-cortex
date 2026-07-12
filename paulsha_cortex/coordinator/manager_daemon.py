from __future__ import annotations

import json
import argparse
import fcntl
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from paulsha_cortex.config import paths
from ..control import constants, contract
from . import autonomy, manager
from .cli import _refuse_unsafe_fanout, _resolve_launcher
from .dispatcher import Dispatcher
from .registry import JobRegistry
from .seams import ScriptWorktreeCreator, TmuxPaneSender

DEFAULT_TICK_INTERVAL = 300.0
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_PERSONA = "builder"
DEFAULT_EXECUTOR = "copilot"
DEFAULT_MAX_LOAD = 1.0
RECENT_DONE_LIMIT = 10
MANAGER_CMD_MARKER = "paulsha_cortex.coordinator.manager_daemon"


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


def _held_reasons(meta: dict[str, Any], is_satisfied: Callable[[str], bool]) -> list[str]:
    reasons: list[str] = []
    if isinstance(meta.get("parse_error"), dict):
        reasons.append(f"parse-error:{meta['parse_error'].get('field', 'frontmatter')}")
    if not (isinstance(meta.get("plan"), str) and meta["plan"]):
        reasons.append("no-plan")
    if meta.get("dispatch") != "auto":
        reasons.append("dispatch-hold")
    for dep in meta.get("depends_on", []):
        if not is_satisfied(dep):
            reasons.append(f"deps-unsatisfied:{dep}")
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
) -> Callable[[], dict[str, Any]]:
    def recent_done_provider() -> list[dict[str, Any]]:
        manifests: list[tuple[str, dict[str, Any]]] = []
        handoff_path = Path(handoff_dir)
        if not handoff_path.is_dir():
            return []
        for path in handoff_path.glob("*.json"):
            payload = contract.read_json(path)
            if not isinstance(payload, dict):
                continue
            manifests.append(
                (
                    str(payload.get("completed_at") or ""),
                    {
                        "slice_id": payload.get("slice_id"),
                        "gate_status": payload.get("gate_status"),
                        "at": payload.get("completed_at"),
                    },
                )
            )
        manifests.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in manifests[:recent_done_limit]]

    def provider() -> dict[str, Any]:
        metas = scan_specs_fn(specs_dir)
        predicate = lambda slice_id: autonomy.default_is_satisfied(slice_id, handoff_dir=handoff_dir)
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
            reasons = _held_reasons(meta, predicate)
            if reasons:
                held.append({"slice_id": slice_id, "reasons": reasons})
        return {
            "ready": ready,
            "held": held,
            "in_flight": _in_flight_status(registry),
            "recent_done": recent_done_provider(),
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
        active_review_launcher = _resolve_launcher(
            requested_review_executor,
            launcher,
            allow_unsafe=allow_unsafe,
            model=requested_review_model,
        )
        if request["type"] == "complete":
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
        metas = scan_specs_fn(request_specs_dir)
        if request["type"] == "dispatch":
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
            dispatched = dispatch_ready_fn(
                [{**target, "dispatch": "auto"}],
                lambda _slice_id: True,
                dispatcher,
                persona=persona,
                launcher=active_launcher,
                handoff_dir=request_handoff_dir,
                git_runner=getattr(dispatcher, "_git_runner", None),
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
        if request["type"] == "fanout":
            jobs = dispatch_ready_fn(
                metas,
                predicate,
                dispatcher,
                persona=persona,
                launcher=active_launcher,
                handoff_dir=request_handoff_dir,
                git_runner=getattr(dispatcher, "_git_runner", None),
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
        }
        if requested_review_executor is not None or requested_review_model is not None:
            run_tick_kwargs.update(
                {
                    "review_launcher": active_review_launcher,
                    "review_executor": requested_review_executor,
                    "review_model": requested_review_model,
                }
            )
        return run_tick_fn(
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
) -> Callable[[], dict[str, Any]]:
    predicate = lambda slice_id: autonomy.default_is_satisfied(
        slice_id,
        handoff_dir=handoff_dir,
        git_runner=getattr(dispatcher, "_git_runner", None),
    )

    def execute() -> dict[str, Any]:
        metas = scan_specs_fn(specs_dir)
        _refuse_unsafe_fanout(metas, predicate, allow_unsafe=default_allow_unsafe)
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
        }
        if default_review_executor is not None or default_review_model is not None:
            run_tick_kwargs.update(
                {
                    "review_launcher": active_review_launcher,
                    "review_executor": default_review_executor,
                    "review_model": default_review_model,
                }
            )
        return run_tick_fn(dispatcher, **run_tick_kwargs)

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

            if (
                not request_drain_interrupted
                and not tick_ran_this_round
                and monotonic_fn() - last_tick_monotonic >= tick_interval
            ):
                try:
                    summary = periodic_runner()
                    skipped = isinstance(summary, dict) and summary.get("dispatch_skipped") == "not-idle"
                    daemon_idle = not skipped
                    if not skipped:
                        last_tick_at = now_fn()
                        last_tick_monotonic = monotonic_fn()
                except Exception as exc:  # noqa: BLE001
                    _log_error(exc)

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
                    },
                    updated_at=now_fn(),
                )
                status_payload["held"] = list(snapshot.get("held", []))
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


def _log_error(exc: Exception) -> None:
    print(f"manager_daemon error: {type(exc).__name__}: {exc}", file=sys.stderr)


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
    )
    return 0 if started else 1


if __name__ == "__main__":
    raise SystemExit(main())
