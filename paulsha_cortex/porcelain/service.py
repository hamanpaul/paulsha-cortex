from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Sequence

from paulsha_cortex.config.runtime import resolve_runtime_root
from paulsha_cortex.control import constants
from paulsha_cortex.deploy import installer
from paulsha_cortex.runtime_attestation import (
    artifact_identity,
    cli_runtime_observation,
    declared_service_environment,
    live_process_started_epoch,
    manager_declared_invocation_revision,
    manager_environment_revision,
    monitor_configuration_revision_from_environment,
    runtime_status_report,
    service_declaration_projection,
    service_environment_overlay,
    trust_root_receipt_summary,
    unknown_runtime_report,
)

from . import COMMANDS, PorcelainCommand, register
from ._runtime_probe import probe_service_runtime

SERVICE_SCHEMA = "cortex-porcelain/service/v1"
_ENSURE_START_TIMEOUT_SECONDS = 10.0
_ENSURE_POLL_INTERVAL_SECONDS = 0.1
_AGENTS_ROOT_INSTALL_HINT = (
    "porcelain 請改用 cortex install service --agents-root PATH"
)


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
    install_cmd.add_argument(
        "--rebind", action="store_true",
        help="既有 instance 記錄的 repo 身分與 --repo-root 不符時，明確放行本次搬遷",
    )
    install_cmd.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")

    for command_name, help_text in (
        ("start", "啟動 manager service/timer"),
        ("stop", "停止 manager service/timer"),
        ("restart", "重啟 manager service/timer"),
        ("status", "顯示 service runtime 與已載入 artifact/config 身分"),
    ):
        cmd = sub.add_parser(command_name, help=help_text)
        cmd.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
        cmd.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")
        if command_name == "status":
            cmd.add_argument(
                "--system", action="store_true",
                help="讀取 system scope units 與 Trust Root install receipt",
            )

    ensure = sub.add_parser("ensure-running", help="確保 manager／monitor 正在執行（輸出 JSON）")
    ensure.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))

    logs = sub.add_parser("logs", help="讀取 service logs")
    logs.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    logs.add_argument("-n", type=int, default=20, help="顯示最近 N 行")
    logs.add_argument("--follow", action="store_true", help="持續追蹤")
    logs.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")

    uninstall = sub.add_parser("uninstall", help="移除 manager/monitor units")
    uninstall.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    uninstall.add_argument("--purge", action="store_true", help="一併移除 bootstrap env")
    uninstall.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/service/v1 JSON")

    # issue #618：trust-root Phase 2b 的 system-level unit 以此為 ExecStart
    # （permgen.ManagerUnit 產生 `<venv>/bin/cortex service run`）。與 start/stop
    # 不同——它不是 systemctl 包裝，而是 daemon 迴圈本身。
    # 參數不在此宣告：`main` 會在 parse 前攔截 run，把其餘 argv（含 --help）
    # 原樣交給 daemon，避免在這裡複製一份會與 daemon parser 漂移的宣告。
    sub.add_parser("run", help="前景執行 manager daemon（system unit 的 ExecStart）")
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


def _append_agents_root_install_hint(message: str) -> str:
    if (
        "PSC_AGENTS_ROOT" not in message
        or "--agents-root" not in message
        or _AGENTS_ROOT_INSTALL_HINT in message
    ):
        return message
    return f"{message.rstrip()}\n{_AGENTS_ROOT_INSTALL_HINT}"


def install(*, instance: str, interval: int, repo_root: str, rebind: bool = False) -> dict[str, Any]:
    validated_instance = installer._validate_instance(instance)
    validated_interval = installer._validate_interval(interval)
    validated_repo_root = installer._resolve_git_repo_root(Path(repo_root))
    result = installer.install_service_result(
        validated_instance, validated_interval, validated_repo_root, rebind=rebind
    )
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


def _env_summary_from_environment(environment: Mapping[str, str]) -> dict[str, Any]:
    """由一份環境變數（不論來源是檔案讀值還是 systemd 有效宣告）投影出
    ``cortex service status`` 的顯示欄位。#1098：drop-in 覆寫
    ``PY``／``PSC_MANAGER_INTERVAL_SECONDS``／``PSC_MANAGER_SPECS_DIR`` 時，
    呼叫端應改傳有效宣告（見 ``_status_payload``），讓顯示值與有效宣告一致，
    不再固定讀 ``~/.agents/core/runtime/<instance>-manager.env`` 這份可能已
    過時的檔案。"""

    interval: int | None = None
    raw_interval = environment.get("PSC_MANAGER_INTERVAL_SECONDS")
    if raw_interval is not None:
        try:
            interval = int(raw_interval)
        except ValueError:
            interval = None
    return {
        "executor": environment.get("PY"),
        "interval_seconds": interval,
        "specs_dir": environment.get("PSC_MANAGER_SPECS_DIR"),
    }


def _env_summary(instance: str) -> dict[str, Any]:
    return _env_summary_from_environment(_read_env_file(_runtime_env_path(instance)))


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


def _read_lock_payload(instance: str | None = None) -> dict[str, Any]:
    if instance is None:
        path = constants.lock_path()
    else:
        path = (
            resolve_runtime_root(
                "PSC_CONTROL_ROOT",
                environment=_fallback_environment(instance),
            )
            / "manager.lock"
        )
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _unit_pid(units: Any, service_name: str) -> int | None:
    if not isinstance(units, dict):
        return None
    row = units.get(service_name)
    if not isinstance(row, dict):
        return None
    pid = row.get("pid")
    return pid if type(pid) is int else None


def _unknown_runtime_report(reason: str, current_artifact: dict[str, object]) -> dict[str, object]:
    # #841 對抗審查第五輪：shape 改由 runtime_attestation.unknown_runtime_report
    # 唯一定義，doctor 的 loaded-runtime 判定共用同一份，避免兩邊結構各自漂移。
    return unknown_runtime_report(reason, current_artifact)


def _service_declared_environment(
    instance: str, declaration: Any
) -> tuple[dict[str, str], str]:
    """依 service 宣告目前的 ``environment_source`` 決定該 service 實際會拿到
    的環境變數。

    分支判定（systemd-effective／unavailable／unknown）改由
    ``runtime_attestation.declared_service_environment`` 唯一決定，doctor 的
    loaded-runtime 判定共用同一條規則（#841 對抗審查第五輪修掉「doctor 用
    manager 的環境去比對 monitor」那組回歸）；這裡只負責提供 ``cortex
    service status`` 自己的 direct-fallback 讀法（讀真實 ``os.environ`` 下既有
    的 ``~/.agents/core/runtime/*.env``），doctor 端則注入可 hermetic 測試的
    讀法，兩邊分支邏輯不會各自漂移。

    #841 對抗審查第四輪 MAJOR：systemd-effective 分支只能用 unit 宣告本身，
    不得以呼叫者（操作 CLI）的殼層環境為底再覆蓋——否則殼層裡未被 unit 宣告
    的 ``PSC_*``（例如操作者自己執行 ``PSC_MONITOR_CONFIG=... cortex service
    status`` 時帶進來的值）會污染這個 service 的判定。呼叫者自己的環境只用於
    描述 operator CLI 本身（見 ``cli_runtime_observation``），不進這裡。缺的
    root 由 ``resolve_runtime_root`` 自行退回已安裝 instance／home 預設，不需
    要在這裡預先補值。"""
    return declared_service_environment(
        declaration, direct_fallback=lambda: _fallback_environment(instance)
    )


def _loaded_runtime_payload(
    instance: str,
    *,
    manager_pid: int | None,
    monitor_pid: int | None,
    units: Any = None,
    service_declaration: Any = None,
    environment_overlay: Any = None,
) -> dict[str, object]:
    current_artifact = artifact_identity()
    operator_cli = cli_runtime_observation(
        instance=instance,
        environment=os.environ,
        artifact=current_artifact,
    )
    unit_rows = units if isinstance(units, dict) else {}
    declarations = (
        service_declaration
        if isinstance(service_declaration, dict)
        else service_declaration_projection(unit_rows, instance=instance)
    )
    manager_artifact = declarations["manager"].get("artifact")
    monitor_artifact = declarations["monitor"].get("artifact")
    # #841 對抗審查第四輪：environment_overlay（含真實值）與 declarations（只有
    # digest，safe for JSON）分開傳入——後者可能是探測後已濾除 systemd 原始屬性
    # 的快取結果，前者才是判定 manager／monitor 實際環境用的來源。呼叫端沒有
    # 明確傳入時（例如測試直接餵未濾除過的 ``units``），退回從 ``unit_rows``
    # 重新判定，行為與濾除前一致。
    overlays = (
        environment_overlay
        if isinstance(environment_overlay, dict)
        else service_environment_overlay(unit_rows, instance=instance)
    )
    manager_environment, manager_environment_source = _service_declared_environment(
        instance, overlays.get("manager")
    )
    monitor_environment, monitor_environment_source = _service_declared_environment(
        instance, overlays.get("monitor")
    )

    if manager_environment_source == "unknown":
        manager_report = _unknown_runtime_report(
            "service-environment-unknown", current_artifact
        )
    else:
        try:
            manager_root = resolve_runtime_root(
                "PSC_COORDINATOR_ROOT", environment=manager_environment
            )
            # #841 loaded runtime attestation 後續修法：只有 systemd-effective
            # 這條路徑才有可信的 unit 宣告（ExecStart／有效環境）可以反推 daemon
            # 實際收到的 argv；direct-fallback／unknown 沒有對應的 systemd 屬性
            # 集合可用，維持 None（仍為 unknown，不臆測）。manager_unit_row 用
            # unit_rows 裡對應這個 instance 的 manager service 原始探測結果
            # （含 "systemd" 區塊），與 overlays 共用同一份判定來源。
            # 優先採用 probe 在原始 row 仍可用時算好的投影摘要（production 路徑
            # 傳進來的 units 已移除 `systemd` 原始屬性）；只有呼叫端直接給未過濾
            # 的 units 時才退回現場計算。
            projected_invocation = declarations["manager"].get("invocation_revision")
            declared_invocation_revision = (
                projected_invocation
                if isinstance(projected_invocation, str)
                else manager_declared_invocation_revision(
                    unit_rows.get(_unit_names(instance)[0]), manager_environment
                )
                if manager_environment_source == "systemd-effective"
                else None
            )
            manager_report = runtime_status_report(
                manager_root,
                service="manager",
                instance=instance,
                declared_config_revision=manager_environment_revision(
                    manager_environment
                ),
                declared_config_component="environment_revision",
                declared_invocation_revision=declared_invocation_revision,
                expected_pid=manager_pid,
                expected_process_started_epoch=live_process_started_epoch(manager_pid),
                require_process_match=True,
                current_artifact=(
                    manager_artifact if isinstance(manager_artifact, dict) else None
                ),
            )
        except Exception:  # noqa: BLE001 — 無效宣告時維持 unknown。
            manager_report = _unknown_runtime_report(
                "runtime-declaration-unavailable", current_artifact
            )

    if monitor_environment_source == "unknown":
        monitor_report = _unknown_runtime_report(
            "service-environment-unknown", current_artifact
        )
    else:
        try:
            monitor_root = resolve_runtime_root(
                "PSC_MONITOR_STATE_ROOT", environment=monitor_environment
            )
            monitor_report = runtime_status_report(
                monitor_root,
                service="monitor",
                instance=instance,
                declared_config_revision=monitor_configuration_revision_from_environment(
                    monitor_environment
                ),
                expected_pid=monitor_pid,
                expected_process_started_epoch=live_process_started_epoch(monitor_pid),
                require_process_match=True,
                current_artifact=(
                    monitor_artifact if isinstance(monitor_artifact, dict) else None
                ),
            )
        except Exception:  # noqa: BLE001 — 無效宣告時維持 unknown。
            monitor_report = _unknown_runtime_report(
                "runtime-declaration-unavailable", current_artifact
            )

    return {
        "operator_cli": operator_cli,
        "service_declaration": declarations,
        "manager": manager_report,
        "monitor": monitor_report,
        "environment_source": {
            "manager": manager_environment_source,
            "monitor": monitor_environment_source,
        },
    }


def _apply_operator_install_evidence(
    loaded_runtime: dict[str, Any],
    *,
    environment_overlay: Any,
    instance: str,
) -> None:
    """Resolve the root-owned Trust Root receipt from the operator side.

    System service accounts cannot read install receipts. `service status --system`
    is therefore intended to run as an operator with receipt access (normally via
    sudo), and binds a validated receipt to the active loaded wheel digest.
    """
    try:
        manager_environment, source = _service_declared_environment(
            instance,
            environment_overlay.get("manager")
            if isinstance(environment_overlay, Mapping)
            else None,
        )
        if source == "unknown":
            raise ValueError("system manager environment is unknown")
        manager_root = resolve_runtime_root(
            "PSC_COORDINATOR_ROOT", environment=manager_environment
        )
        state_root = manager_root.parent
        receipt_dir = state_root.parent / f"{state_root.name}-install-receipts"
        installed = loaded_runtime.get("manager", {}).get("installed_artifact", {})
        # The install receipt attests the wheel currently selected by the
        # on-disk service declaration. The loaded process may intentionally
        # still be on the previous wheel, so its digest is never a stand-in:
        # without the installed wheel the receipt cannot be bound (#1160 review).
        expected_wheel = (
            installed.get("wheel_sha256") if isinstance(installed, Mapping) else None
        )
        candidates: list[tuple[int, dict[str, Any]]] = []
        if receipt_dir.is_dir() and not receipt_dir.is_symlink():
            from paulsha_cortex.trust_root.install.core import InstallReceipt

            receipt_paths = sorted(receipt_dir.glob("*.json"))
            if len(receipt_paths) > 256:
                raise ValueError("install receipt directory exceeds scan limit")
            for path in receipt_paths:
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    receipt = InstallReceipt.load(path)
                    document = receipt.to_dict()
                    plan = document.get("plan")
                    roots = plan.get("roots") if isinstance(plan, Mapping) else None
                    candidate = plan.get("candidate") if isinstance(plan, Mapping) else None
                    if not isinstance(roots, Mapping) or roots.get("state") != str(state_root):
                        continue
                    if not isinstance(candidate, Mapping):
                        continue
                    wheel_sha256 = candidate.get("wheel_sha256")
                    identity = plan.get("repo_identity")
                    commit = identity.get("commit") if isinstance(identity, Mapping) else None
                    summary = trust_root_receipt_summary(path)
                    summary["wheel_sha256"] = wheel_sha256
                    if isinstance(commit, str):
                        summary["candidate_commit"] = commit.lower()
                    candidates.append((path.stat().st_mtime_ns, summary))
                except Exception:  # noqa: BLE001 — skip invalid unrelated receipt
                    continue
        evidence = (
            max(candidates, key=lambda row: row[0])[1]
            if candidates
            else {"status": "unknown", "reason": "install-receipt-unavailable"}
        )
        if evidence.get("status") == "verified" and not isinstance(expected_wheel, str):
            evidence = {
                "status": "unknown",
                "reason": "installed-wheel-unresolved",
            }
        elif (
            evidence.get("status") == "verified"
            and evidence.get("wheel_sha256") != expected_wheel
        ):
            evidence = {
                **evidence,
                "status": "drift",
                "reason": "installed-wheel-receipt-mismatch",
            }
    except Exception:  # noqa: BLE001 — operator receipt lookup is fail closed
        evidence = {"status": "unknown", "reason": "install-receipt-unavailable"}

    loaded_runtime["trust_root"] = evidence
    for service in ("manager", "monitor"):
        report = loaded_runtime.get(service)
        if not isinstance(report, dict):
            continue
        report["trust_root"] = evidence
        artifact = report.get("installed_artifact")
        if isinstance(artifact, dict) and evidence.get("status") == "verified":
            if isinstance(evidence.get("wheel_sha256"), str):
                artifact["wheel_sha256"] = evidence["wheel_sha256"]
            if isinstance(evidence.get("candidate_commit"), str):
                artifact["candidate_commit"] = evidence["candidate_commit"]
            artifact["source_revision"] = "unknown"


def _status_payload(instance: str, *, system_scope: bool = False) -> dict[str, Any]:
    probe = (
        probe_service_runtime(instance, scope="system")
        if system_scope
        else probe_service_runtime(instance)
    )
    # #841 對抗審查第四輪：真實環境值只透過這個 pop 取出，之後任何分支對
    # ``probe`` 做 ``dict(probe)``／整包回傳都不會再帶著它，避免誤落進 JSON
    # 輸出；再以獨立參數傳給 ``_loaded_runtime_payload`` 供內部重建 root 用。
    environment_overlay = probe.pop("_environment_overlay", None)
    if system_scope and isinstance(environment_overlay, dict):
        # System mode has no trustworthy direct/file fallback. Missing effective
        # properties must remain unknown rather than consulting the operator env.
        for row in environment_overlay.values():
            if (
                isinstance(row, dict)
                and row.get("environment_source") != "systemd-effective"
            ):
                row["environment_source"] = "unknown"
    if str(probe["mode"]).startswith("systemd"):
        units = probe.get("units", {})
        manager_service = f"{instance}-manager.service"
        monitor_service = f"{instance}-monitor.service"
        payload = dict(probe)
        payload["pid"] = units.get(manager_service, {}).get("pid")
        # #1098：只有 manager 的有效環境來源確認是 ``systemd-effective``（已
        # 套用 drop-in）時才改用它投影顯示欄位；其餘來源（unavailable／
        # unknown）沒有可信的有效值可用，維持既有讀 `<instance>-manager.env`
        # 檔案的 fallback，不強行套用可能是空的有效環境覆蓋掉檔案內容。
        manager_overlay = (
            environment_overlay.get("manager")
            if isinstance(environment_overlay, dict)
            else None
        )
        manager_effective_environment = (
            manager_overlay.get("environment")
            if isinstance(manager_overlay, dict)
            else None
        )
        if (
            isinstance(manager_overlay, dict)
            and manager_overlay.get("environment_source") == "systemd-effective"
            and isinstance(manager_effective_environment, dict)
        ):
            payload["env"] = _env_summary_from_environment(manager_effective_environment)
        else:
            payload["env"] = _env_summary(instance)
        payload["loaded_runtime"] = _loaded_runtime_payload(
            instance,
            manager_pid=_unit_pid(units, manager_service),
            monitor_pid=_unit_pid(units, monitor_service),
            units=units,
            service_declaration=probe.get("service_declaration"),
            environment_overlay=environment_overlay,
        )
        if system_scope:
            payload["scope"] = "system"
            _apply_operator_install_evidence(
                payload["loaded_runtime"],
                environment_overlay=environment_overlay,
                instance=instance,
            )
        return payload
    if system_scope:
        current = artifact_identity()
        return {
            "instance": instance,
            "mode": "none",
            "scope": "system",
            "version": probe.get("version", "0.0.0+unknown"),
            "units": probe.get("units", {}),
            "loaded_runtime": {
                "operator_cli": cli_runtime_observation(
                    instance=instance, environment=os.environ, artifact=current
                ),
                "service_declaration": probe.get("service_declaration"),
                "manager": unknown_runtime_report("system-units-unavailable", current),
                "monitor": unknown_runtime_report("system-units-unavailable", current),
                "trust_root": {"status": "unknown", "reason": "system-units-unavailable"},
            },
            "suggested_commands": [],
        }
    fallback = _fallback_runtime(instance, str(probe.get("version", "0.0.0+unknown")), probe.get("units", {}))
    if fallback is not None:
        units = fallback.get("units", {})
        fallback["loaded_runtime"] = _loaded_runtime_payload(
            instance,
            manager_pid=fallback.get("pid") if type(fallback.get("pid")) is int else None,
            monitor_pid=_unit_pid(units, f"{instance}-monitor.service"),
            units=units,
            service_declaration=probe.get("service_declaration"),
            environment_overlay=environment_overlay,
        )
        return fallback
    return {
        "instance": instance,
        "mode": "none",
        "version": probe.get("version", "0.0.0+unknown"),
        "units": probe.get("units", {}),
        "loaded_runtime": _loaded_runtime_payload(
            instance,
            manager_pid=-1,
            monitor_pid=-1,
            units=probe.get("units", {}),
            service_declaration=probe.get("service_declaration"),
            environment_overlay=environment_overlay,
        ),
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


def _live_manager_lock_pid(instance: str) -> int | None:
    pid = _read_lock_payload(instance).get("pid")
    return pid if isinstance(pid, int) and _pid_is_live(pid) else None


def _wait_for_manager_lock(instance: str, process: Any | None = None) -> int | None:
    deadline = time.monotonic() + _ENSURE_START_TIMEOUT_SECONDS
    while True:
        pid = _live_manager_lock_pid(instance)
        if pid is not None:
            return pid
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(_ENSURE_POLL_INTERVAL_SECONDS, remaining))


def _ensure_payload(
    instance: str,
    *,
    mode: str,
    exit_code: int,
    pids: dict[str, int],
    units: list[str],
    error: str | None = None,
) -> dict[str, Any]:
    payload = _service_envelope(
        "ensure-running",
        instance,
        mode=mode,
        pids=pids,
        units=units,
        result={"exit_code": exit_code},
    )
    if error:
        payload["error"] = error
    return payload


def _reported_unit_names(service: dict[str, Any]) -> list[str]:
    units = service.get("units", {})
    if not isinstance(units, dict):
        return []
    return [
        name
        for name in _unit_names(str(service.get("instance", "cortex")))
        if isinstance(units.get(name), dict) and units[name].get("present")
    ]


def _reported_monitor_pid(service: dict[str, Any], instance: str) -> int | None:
    units = service.get("units", {})
    row = units.get(f"{instance}-monitor.service") if isinstance(units, dict) else None
    pid = row.get("pid") if isinstance(row, dict) else None
    return pid if isinstance(pid, int) and _pid_is_live(pid) else None


def _ensure_systemd_running(instance: str) -> dict[str, Any]:
    manager_service, manager_timer, monitor_service = _unit_names(instance)
    started = start(instance=instance)
    exit_code = int(started.get("result", {}).get("exit_code", 1))
    if exit_code != 0:
        return _ensure_payload(
            instance,
            mode="systemd",
            exit_code=exit_code,
            pids={},
            units=[manager_service, manager_timer, monitor_service],
            error=str(started.get("error") or "systemd manager 啟動失敗"),
        )

    monitor_result = _run_systemctl("start", monitor_service)
    if monitor_result.returncode != 0:
        return _ensure_payload(
            instance,
            mode="systemd",
            exit_code=monitor_result.returncode or 1,
            pids={},
            units=[manager_service, manager_timer, monitor_service],
            error=_completed_process_error(
                monitor_result,
                fallback=f"systemctl start failed for {monitor_service}",
            ),
        )

    manager_pid = _wait_for_manager_lock(instance)
    if manager_pid is None:
        return _ensure_payload(
            instance,
            mode="systemd",
            exit_code=1,
            pids={},
            units=[manager_service, manager_timer, monitor_service],
            error=(
                f"等待 manager.lock 最多 {_ENSURE_START_TIMEOUT_SECONDS:g} 秒，"
                "仍未確認 live manager 持有鎖。"
            ),
        )
    service = _status_payload(instance)
    pids = {"manager": manager_pid}
    monitor_pid = _reported_monitor_pid(service, instance)
    if monitor_pid is not None:
        pids["monitor"] = monitor_pid
    return _ensure_payload(
        instance,
        mode="systemd",
        exit_code=0,
        pids=pids,
        units=[manager_service, manager_timer, monitor_service],
    )


def _fallback_environment(instance: str) -> dict[str, str]:
    runtime_dir = _runtime_env_path(instance).parent
    env = os.environ.copy()
    for path in (runtime_dir / f"{instance}.env", _runtime_env_path(instance)):
        env.update(installer._read_plain_env(path))
    env["PSC_INSTANCE"] = instance
    env["PY"] = sys.executable
    return env


def _terminate_process(process: Any | None) -> None:
    if process is None:
        return
    poll = getattr(process, "poll", None)
    terminate = getattr(process, "terminate", None)
    if not callable(poll) or not callable(terminate) or poll() is not None:
        return
    try:
        terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass


def _ensure_fallback_running(instance: str) -> dict[str, Any]:
    manager_process = None
    try:
        env = _fallback_environment(instance)
        specs_dir = env.get("PSC_MANAGER_SPECS_DIR") or str(
            Path(os.environ.get("HOME", str(Path.home()))).expanduser() / ".agents" / "specs"
        )
        log_path = _fallback_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            manager_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "paulsha_cortex.coordinator.manager_daemon",
                    "--specs-dir",
                    specs_dir,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=env.get("PSC_REPO_ROOT") or None,
                start_new_session=True,
            )
            manager_pid = _wait_for_manager_lock(instance, manager_process)
            if manager_pid is None:
                _terminate_process(manager_process)
                return _ensure_payload(
                    instance,
                    mode="fallback",
                    exit_code=1,
                    pids={},
                    units=[],
                    error=(
                        f"本地 manager 未能在 {_ENSURE_START_TIMEOUT_SECONDS:g} 秒內"
                        "取得 manager.lock；詳見 manager.log。"
                    ),
                )
            monitor_process = subprocess.Popen(
                [sys.executable, "-m", "paulsha_cortex.monitor"],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=env.get("PSC_REPO_ROOT") or None,
                start_new_session=True,
            )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _terminate_process(manager_process)
        return _ensure_payload(
            instance,
            mode="fallback",
            exit_code=1,
            pids={},
            units=[],
            error=f"本地 fallback 啟動失敗：{exc}",
        )
    return _ensure_payload(
        instance,
        mode="fallback",
        exit_code=0,
        pids={"manager": manager_pid, "monitor": monitor_process.pid},
        units=[],
    )


def _run_ensure_running(instance: str) -> int:
    service: dict[str, Any] = {}
    mode = "none"
    try:
        service = _status_payload(instance)
        service_units = service.get("units", {})
        unit_names = _unit_names(instance)
        units_available = isinstance(service_units, dict) and all(
            isinstance(service_units.get(name), dict)
            and service_units[name].get("present")
            for name in unit_names
        )
        mode = "systemd" if units_available else "fallback"
        manager_pid = _live_manager_lock_pid(instance)
        if manager_pid is not None:
            pids = {"manager": manager_pid}
            monitor_pid = _reported_monitor_pid(service, instance)
            if monitor_pid is not None:
                pids["monitor"] = monitor_pid
            payload = _ensure_payload(
                instance,
                mode="already-running",
                exit_code=0,
                pids=pids,
                units=_reported_unit_names(service),
            )
        elif units_available and _systemd_control_available():
            mode = "systemd"
            payload = _ensure_systemd_running(instance)
        else:
            mode = "fallback"
            payload = _ensure_fallback_running(instance)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        payload = _ensure_payload(
            instance,
            mode=mode,
            exit_code=1,
            pids={},
            units=_reported_unit_names(service),
            error=f"ensure-running 執行失敗：{exc}",
        )
    _json_dump(payload)
    return int(payload.get("result", {}).get("exit_code", 1))


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
    loaded_runtime = service.get("loaded_runtime")
    if isinstance(loaded_runtime, dict):
        sys.stdout.write(
            "loaded_runtime: "
            + json.dumps(loaded_runtime, ensure_ascii=False, sort_keys=True)
            + "\n"
        )
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


def _run_install(
    *, instance: str, interval: int, repo_root: str, json_output: bool, rebind: bool = False
) -> int:
    argv = ["service", "--instance", instance, "--repo-root", repo_root, "--interval", str(interval)]
    if rebind:
        argv.append("--rebind")
    if json_output:
        payload = install(instance=instance, interval=interval, repo_root=repo_root, rebind=rebind)
        _json_dump(payload)
        return int(payload.get("result", {}).get("exit_code", 1))
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            return int(installer.main(argv) or 0)
    finally:
        message = _append_agents_root_install_hint(stderr.getvalue())
        if message:
            sys.stderr.write(message)


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
        service_payload = payload.get("service")
        _print_status(service_payload if isinstance(service_payload, dict) else _status_payload(instance))
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


def _run_status(*, instance: str, json_output: bool, system_scope: bool = False) -> int:
    service = _status_payload(instance, system_scope=system_scope)
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


def _run_foreground(daemon_argv: Sequence[str]) -> int:
    """前景跑 manager daemon（issue #618）。

    stdout/stderr 一律交給呼叫端（system unit 下即 journald），不自建 log 檔——
    Phase 2b 的 `HOME` 為 root-owned 且 unit 帶 `ProtectHome=yes`，
    `scripts/service-manager.sh` 那套 `$HOME/.agents/log` 導向在該佈局下寫不進去。
    daemon 迴圈本身即阻塞並回傳 0/1，符合 `Type=simple` 的期待。
    """
    # 延後匯入：daemon 會拉進整個 coordinator 相依圖，不該在每次 `cortex` 呼叫
    # 註冊 porcelain 命令時就付這個成本。
    from paulsha_cortex.coordinator import manager_daemon

    return manager_daemon.main(list(daemon_argv))


def main(argv: Sequence[str]) -> int:
    items = _normalize_argv(argv)
    if items and items[0] == "run":
        return _run_foreground(items[1:])
    parser = _build_parser()
    args = parser.parse_args(items)
    try:
        instance = installer._validate_instance(getattr(args, "instance", "cortex"))
        if args.command == "install":
            return _run_install(
                instance=instance,
                interval=args.interval,
                repo_root=args.repo_root,
                json_output=args.json,
                rebind=args.rebind,
            )
        if args.command == "ensure-running":
            return _run_ensure_running(instance)
        if args.command in {"start", "stop", "restart"}:
            return _run_lifecycle(args.command, instance=instance, json_output=args.json)
        if args.command == "status":
            return _run_status(
                instance=instance,
                json_output=args.json,
                system_scope=args.system,
            )
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
        print(
            _append_agents_root_install_hint(f"錯誤: {exc}"),
            file=sys.stderr,
        )
        return 1
    parser.error(f"unsupported service command: {args.command}")
    return 2
