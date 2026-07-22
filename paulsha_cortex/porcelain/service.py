from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from paulsha_cortex.control import constants
from paulsha_cortex.deploy import installer

from . import COMMANDS, PorcelainCommand, register
from ._runtime_probe import probe_service_runtime

SERVICE_SCHEMA = "cortex-porcelain/service/v1"


def register_commands() -> None:
    if "service" in COMMANDS:
        return
    register(PorcelainCommand(name="service", help="管理 service/runtime、logs 與 uninstall", run=main))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex service")
    sub = parser.add_subparsers(dest="command", required=True)

    install_cmd = sub.add_parser("install", help="包裝既有 installer")
    install_cmd.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    install_cmd.add_argument("--interval", type=int, default=300)
    install_cmd.add_argument("--repo-root", default=str(Path.cwd()))
    install_cmd.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")

    for command_name, help_text in (
        ("start", "啟動 manager service/timer"),
        ("stop", "停止 manager service/timer"),
        ("restart", "重啟 manager service/timer"),
        ("status", "顯示 service runtime 狀態"),
    ):
        cmd = sub.add_parser(command_name, help=help_text)
        cmd.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
        cmd.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")

    logs = sub.add_parser("logs", help="讀取 service logs")
    logs.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    logs.add_argument("-n", type=int, default=20, help="顯示最近 N 行")
    logs.add_argument("--follow", action="store_true", help="持續追蹤")
    logs.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")

    uninstall = sub.add_parser("uninstall", help="移除 manager/monitor units")
    uninstall.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    uninstall.add_argument("--purge", action="store_true", help="一併移除 bootstrap env")
    uninstall.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")
    return parser


def _normalize_argv(argv: Sequence[str]) -> list[str]:
    items = list(argv)
    if len(items) >= 4 and items[0] == "uninstall":
        try:
            instance_index = items.index("--instance")
        except ValueError:
            return items
        value_index = instance_index + 1
        if value_index >= len(items):
            return items
        candidate = items[value_index]
        if candidate.startswith("-") and value_index + 1 < len(items) and not items[value_index + 1].startswith("-"):
            items[value_index], items[value_index + 1] = items[value_index + 1], items[value_index]
    return items


def _json_dump(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _service_envelope(command: str, instance: str, *, mode: str, **payload: Any) -> dict[str, Any]:
    return {
        "schema": SERVICE_SCHEMA,
        "command": command,
        "instance": instance,
        "mode": mode,
        **payload,
    }


def install(*, instance: str, interval: int, repo_root: str) -> dict[str, Any]:
    validated_instance = installer._validate_instance(instance)
    validated_interval = installer._validate_interval(interval)
    validated_repo_root = installer._resolve_git_repo_root(Path(repo_root))
    result = installer.install_service_result(validated_instance, validated_interval, validated_repo_root)
    return _service_envelope(
        "install",
        instance,
        mode=result.mode,
        message=result.message,
        result={"exit_code": result.exit_code},
    )


def start(*, instance: str) -> dict[str, Any]:
    if not _systemd_control_available():
        service = _control_service_state(instance)
        return _service_envelope(
            "start",
            instance,
            mode=str(service.get("mode")),
            error=(
                "systemd 不可用；start/stop/restart 僅支援 systemd mode，"
                "請改用前景 service-manager.sh 管理 fallback runtime。"
            ),
            result={"exit_code": 1},
            service=service,
        )
    service_unit, timer_unit = _manager_pair(instance)
    result = _run_systemctl("start", service_unit, timer_unit)
    if result.returncode != 0:
        return _service_envelope(
            "start",
            instance,
            mode="systemd",
            error=_completed_process_error(
                result,
                fallback=f"systemctl start failed for {service_unit} {timer_unit}",
            ),
            result={"exit_code": result.returncode},
        )
    return _service_envelope(
        "start",
        instance,
        mode="systemd",
        result={"exit_code": 0},
        service=_status_payload(instance),
    )


def _emit_command_error(
    command: str,
    instance: str,
    *,
    json_output: bool,
    mode: str,
    exit_code: int,
    message: str,
    **payload: Any,
) -> int:
    if json_output:
        _json_dump(
            _service_envelope(
                command,
                instance,
                mode=mode,
                error=message,
                result={"exit_code": exit_code},
                **payload,
            )
        )
        return exit_code
    sys.stderr.write(message if message.endswith("\n") else message + "\n")
    return exit_code


def _unit_names(instance: str) -> tuple[str, str, str]:
    return (
        f"{instance}-manager.service",
        f"{instance}-manager.timer",
        f"{instance}-monitor.service",
    )


def _manager_pair(instance: str) -> tuple[str, str]:
    manager_service, manager_timer, _monitor_service = _unit_names(instance)
    return manager_service, manager_timer


def _runtime_env_path(instance: str) -> Path:
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    return home / ".agents" / "core" / "runtime" / f"{instance}-manager.env"


def _fallback_log_path() -> Path:
    home = Path(os.environ.get("HOME", str(Path.home()))).expanduser()
    return home / ".agents" / "log" / "manager.log"


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator and key:
            values[key] = value
    return values


def _env_summary(instance: str) -> dict[str, Any]:
    values = _read_env_file(_runtime_env_path(instance))
    interval: int | None = None
    raw_interval = values.get("PSC_MANAGER_INTERVAL_SECONDS")
    if raw_interval is not None:
        try:
            interval = int(raw_interval)
        except ValueError:
            interval = None
    return {
        "executor": values.get("PY"),
        "interval_seconds": interval,
        "specs_dir": values.get("PSC_MANAGER_SPECS_DIR"),
    }


def _pid_is_live(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _fallback_runtime(instance: str, version: str, units: dict[str, Any]) -> dict[str, Any] | None:
    lock_payload = _read_lock_payload()
    pid = lock_payload.get("pid")
    if not isinstance(pid, int) or not _pid_is_live(pid):
        return None
    log_path = _fallback_log_path()
    return {
        "instance": instance,
        "mode": "fallback",
        "version": version,
        "pid": pid,
        "log_path": str(log_path),
        "units": units,
    }


def _read_lock_payload() -> dict[str, Any]:
    path = constants.lock_path()
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _status_payload(instance: str) -> dict[str, Any]:
    probe = probe_service_runtime(instance)
    if probe["mode"] == "systemd":
        units = probe.get("units", {})
        manager_service = f"{instance}-manager.service"
        payload = dict(probe)
        payload["pid"] = units.get(manager_service, {}).get("pid")
        payload["env"] = _env_summary(instance)
        return payload
    fallback = _fallback_runtime(instance, str(probe.get("version", "0.0.0+unknown")), probe.get("units", {}))
    if fallback is not None:
        return fallback
    return {
        "instance": instance,
        "mode": "none",
        "version": probe.get("version", "0.0.0+unknown"),
        "units": probe.get("units", {}),
        "suggested_commands": [f"cortex service install --instance {instance}"],
    }


def _systemd_control_available() -> bool:
    return installer._systemctl_available()


def _control_service_state(instance: str) -> dict[str, Any]:
    service = _status_payload(instance)
    if _systemd_control_available() or service.get("mode") == "fallback":
        return service
    normalized = dict(service)
    normalized["mode"] = "none"
    return normalized


def _mode_error(command: str, instance: str, *, json_output: bool, message: str) -> int:
    service = _control_service_state(instance)
    if not json_output:
        _print_status(service)
    return _emit_command_error(
        command,
        instance,
        json_output=json_output,
        mode=str(service.get("mode")),
        exit_code=1,
        message=message,
        service=service,
    )


def _print_status(service: dict[str, Any]) -> None:
    sys.stdout.write(f"instance: {service.get('instance')}\n")
    sys.stdout.write(f"mode: {service.get('mode')}\n")
    sys.stdout.write(f"version: {service.get('version')}\n")
    pid = service.get("pid")
    if pid is not None:
        sys.stdout.write(f"pid: {pid}\n")
    env = service.get("env")
    if isinstance(env, dict):
        sys.stdout.write("env: " + json.dumps(env, ensure_ascii=False, sort_keys=True) + "\n")
    log_path = service.get("log_path")
    if isinstance(log_path, str):
        sys.stdout.write(f"log_path: {log_path}\n")
    for unit_name, row in sorted(service.get("units", {}).items()):
        if not isinstance(row, dict):
            continue
        line = (
            f"{unit_name}\tstatus={row.get('status')}\tpid={row.get('pid') or '-'}"
            f"\texec_path={row.get('exec_path') or '-'}\tstale={row.get('stale')}"
        )
        suggestion = row.get("suggestion")
        if suggestion:
            line += f"\tsuggestion={suggestion}"
        sys.stdout.write(line + "\n")
    for command in service.get("suggested_commands", []):
        sys.stdout.write(f"suggested: {command}\n")


def _run_install(*, instance: str, interval: int, repo_root: str, json_output: bool) -> int:
    argv = ["service", "--instance", instance, "--repo-root", repo_root, "--interval", str(interval)]
    if json_output:
        payload = install(instance=instance, interval=interval, repo_root=repo_root)
        _json_dump(payload)
        return int(payload.get("result", {}).get("exit_code", 1))
    return int(installer.main(argv) or 0)


def _run_systemctl(verb: str, *units: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", verb, *units],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _completed_process_error(result: subprocess.CompletedProcess[str], *, fallback: str) -> str:
    return (result.stderr or result.stdout or fallback).strip() or fallback


def _run_lifecycle(command: str, *, instance: str, json_output: bool) -> int:
    if command == "start":
        payload = start(instance=instance)
        exit_code = int(payload.get("result", {}).get("exit_code", 1))
        if json_output:
            _json_dump(payload)
            return exit_code
        if exit_code != 0:
            service_payload = payload.get("service")
            if isinstance(service_payload, dict):
                _print_status(service_payload)
            error = payload.get("error")
            if isinstance(error, str):
                sys.stderr.write(error if error.endswith("\n") else error + "\n")
            return exit_code
        _print_status(_status_payload(instance))
        return 0
    if not _systemd_control_available():
        return _mode_error(
            command,
            instance,
            json_output=json_output,
            message="systemd 不可用；start/stop/restart 僅支援 systemd mode，請改用前景 service-manager.sh 管理 fallback runtime。",
        )
    service, timer = _manager_pair(instance)
    result = _run_systemctl(command, service, timer)
    if result.returncode != 0:
        return _emit_command_error(
            command,
            instance,
            json_output=json_output,
            mode="systemd",
            exit_code=result.returncode,
            message=_completed_process_error(
                result,
                fallback=f"systemctl {command} failed for {service} {timer}",
            ),
        )
    if json_output:
        _json_dump(_service_envelope(command, instance, mode="systemd", result={"exit_code": result.returncode}))
        return 0
    _print_status(_status_payload(instance))
    return 0


def _run_status(*, instance: str, json_output: bool) -> int:
    service = _status_payload(instance)
    if json_output:
        _json_dump(_service_envelope("status", instance, mode=str(service.get("mode")), service=service))
        return 0
    _print_status(service)
    return 0


def _journalctl_args(instance: str, *, lines: int, follow: bool) -> list[str]:
    args = ["journalctl", "--user", "-u", f"{instance}-manager.service", "-n", str(max(lines, 0))]
    if follow:
        args.append("-f")
    return args


def _tail_lines(path: Path, lines: int) -> str:
    data = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(data[-max(lines, 0) :]) + ("\n" if data and lines != 0 else "")


def _stream_process_output(argv: list[str]) -> int:
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        sys.stdout.write(line)
    stderr_output = ""
    if process.stderr is not None:
        stderr_output = process.stderr.read()
    return_code = process.wait()
    if return_code != 0 and stderr_output:
        sys.stderr.write(stderr_output)
    return return_code


def _run_logs(*, instance: str, lines: int, follow: bool, json_output: bool) -> int:
    service = _status_payload(instance)
    output: str
    source: str
    mode = str(service.get("mode"))
    if follow and json_output:
        return _emit_command_error(
            "logs",
            instance,
            json_output=True,
            mode=mode,
            exit_code=1,
            message="`cortex service logs --json` 不支援 `--follow` 串流輸出。",
            service=service,
            source="journalctl" if mode == "systemd" else "file",
            lines=max(lines, 0),
        )
    if service.get("mode") == "systemd":
        argv = _journalctl_args(instance, lines=lines, follow=follow)
        if follow:
            return _stream_process_output(argv)
        raw = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=10)
        if raw.returncode != 0:
            return _emit_command_error(
                "logs",
                instance,
                json_output=json_output,
                mode=mode,
                exit_code=raw.returncode,
                message=_completed_process_error(raw, fallback=f"journalctl failed for {instance}"),
                service=service,
                source="journalctl",
                lines=max(lines, 0),
            )
        output = raw.stdout
        source = "journalctl"
    else:
        if follow:
            return _emit_command_error(
                "logs",
                instance,
                json_output=json_output,
                mode=mode,
                exit_code=1,
                message="fallback mode 不支援 `cortex service logs --follow`；請直接 tail log 檔案。",
                service=service,
                source="file",
                lines=max(lines, 0),
            )
        log_path = Path(str(service.get("log_path") or _fallback_log_path()))
        if not log_path.is_file():
            raise ValueError(f"log not found: {log_path}")
        output = _tail_lines(log_path, lines)
        source = "file"
    if json_output:
        _json_dump(
            _service_envelope(
                "logs",
                instance,
                mode=str(service.get("mode")),
                source=source,
                lines=max(lines, 0),
                output=output,
            )
        )
        return 0
    sys.stdout.write(output)
    return 0


def _remove_if_exists(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def _run_uninstall(*, instance: str, purge: bool, json_output: bool) -> int:
    if not _systemd_control_available():
        return _mode_error(
            "uninstall",
            instance,
            json_output=json_output,
            message="systemd 不可用；uninstall 無法停用 user units，請先移除 fallback runtime 再清理 unit 檔案。",
        )
    unit_root = Path(os.environ.get("HOME", str(Path.home()))).expanduser() / ".config" / "systemd" / "user"
    units = _unit_names(instance)
    stop_result = _run_systemctl("stop", *units)
    disable_result = _run_systemctl("disable", *units)
    if stop_result.returncode != 0:
        return _emit_command_error(
            "uninstall",
            instance,
            json_output=json_output,
            mode="systemd",
            exit_code=stop_result.returncode,
            message=_completed_process_error(stop_result, fallback=f"systemctl stop failed for {instance}"),
            purge=purge,
        )
    if disable_result.returncode != 0:
        return _emit_command_error(
            "uninstall",
            instance,
            json_output=json_output,
            mode="systemd",
            exit_code=disable_result.returncode,
            message=_completed_process_error(disable_result, fallback=f"systemctl disable failed for {instance}"),
            purge=purge,
        )
    for unit_name in units:
        _remove_if_exists(unit_root / unit_name)
    env_path = _runtime_env_path(instance)
    if purge:
        _remove_if_exists(env_path)
    daemon_reload = _run_systemctl("daemon-reload")
    if daemon_reload.returncode != 0:
        return _emit_command_error(
            "uninstall",
            instance,
            json_output=json_output,
            mode="systemd",
            exit_code=daemon_reload.returncode,
            message=_completed_process_error(daemon_reload, fallback="systemctl daemon-reload failed"),
            purge=purge,
        )
    if json_output:
        _json_dump(
            _service_envelope(
                "uninstall",
                instance,
                mode="systemd",
                purge=purge,
                result={"exit_code": 0},
            )
        )
        return 0
    sys.stdout.write(f"uninstalled: {instance}\n")
    return 0


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(_normalize_argv(argv))
    try:
        instance = installer._validate_instance(getattr(args, "instance", "cortex"))
        if args.command == "install":
            return _run_install(
                instance=instance,
                interval=args.interval,
                repo_root=args.repo_root,
                json_output=args.json,
            )
        if args.command in {"start", "stop", "restart"}:
            return _run_lifecycle(args.command, instance=instance, json_output=args.json)
        if args.command == "status":
            return _run_status(instance=instance, json_output=args.json)
        if args.command == "logs":
            return _run_logs(
                instance=instance,
                lines=args.n,
                follow=args.follow,
                json_output=args.json,
            )
        if args.command == "uninstall":
            return _run_uninstall(instance=instance, purge=args.purge, json_output=args.json)
    except ValueError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported service command: {args.command}")
    return 2
