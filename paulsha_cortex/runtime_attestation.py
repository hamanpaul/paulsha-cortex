"""記錄程序實際載入的程式與配置身分，並避免保存機敏值。"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shlex
import stat
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

RUNTIME_ATTESTATION_SCHEMA = "cortex/loaded-runtime-attestation/v1"
_SERVICES = frozenset({"manager", "monitor"})
_INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PACKAGE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}\Z")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_REVISION_RE = re.compile(r"[0-9a-f]{7,64}\Z")
_SECRET_KEY_RE = re.compile(
    r"(?:password|secret|token|credential|api[_-]?key|private[_-]?key|access[_-]?key|authorization)",
    re.IGNORECASE,
)
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_ENV_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z")
_FLAG_TOKEN_RE = re.compile(r"--([A-Za-z][A-Za-z0-9-]*)(=(.*))?\Z")


class RuntimeAttestationError(ValueError):
    """表示載入身分證據無效或不安全。"""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_config(value: object, *, depth: int = 0) -> object:
    if depth > 12:
        raise RuntimeAttestationError("configuration nesting exceeds limit")
    if value is None or type(value) in {bool, int, str}:
        if isinstance(value, str) and len(value) > 8192:
            raise RuntimeAttestationError("configuration string exceeds limit")
        return value
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise RuntimeAttestationError("configuration contains a non-finite number")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str) or len(raw_key) > 256:
                raise RuntimeAttestationError("configuration key is invalid")
            if _SECRET_KEY_RE.search(raw_key):
                continue
            result[raw_key] = _safe_config(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 10000:
            raise RuntimeAttestationError("configuration list exceeds limit")
        return [_safe_config(child, depth=depth + 1) for child in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return _safe_config(asdict(value), depth=depth + 1)
    if hasattr(value, "value") and type(getattr(value, "value")) in {str, int}:
        return _safe_config(getattr(value, "value"), depth=depth + 1)
    raise RuntimeAttestationError("configuration contains an unsupported value")


def configuration_revision(configuration: Mapping[str, object] | object) -> str:
    """計算 JSON 相容且移除機敏欄位後的有效配置摘要。"""

    encoded = _canonical_bytes(_safe_config(configuration))
    if len(encoded) > _MAX_CONFIG_BYTES:
        raise RuntimeAttestationError("configuration exceeds size limit")
    return hashlib.sha256(encoded).hexdigest()


def safe_environment_projection(environment: Mapping[str, str]) -> dict[str, str]:
    """挑選安全的 Cortex 環境宣告，不保留未列入允許清單的值。

    #1098：白名單額外收 ``PY`` 這一個單獨鍵——``scripts/service-manager.sh``
    wrapper 用它決定實際執行哪個直譯器／venv，drop-in 覆寫它時，`cortex
    service status` 的 executor 顯示欄位與（透過
    ``_declared_service_artifact``）manager artifact 判定都需要能看到這個
    有效值；沒有它就只看得到 wrapper 腳本自己所在的套件根，看不出實際載入的
    是哪個 venv。``PY`` 不是機敏值（只是一個直譯器路徑），比對規則沿用既有
    的 ``_SECRET_KEY_RE`` 檢查。"""

    return {
        key: value
        for key, value in sorted(environment.items())
        if (key.startswith(("PSC_", "PAULSHACLAW_")) or key == "PY")
        and not _SECRET_KEY_RE.search(key)
        and isinstance(value, str)
    }


def _safe_invocation_argv(argv: Sequence[object]) -> list[str]:
    """機敏過濾後的原始 argv token 清單。

    ``invocation_revision`` 改成直接對 daemon 實際收到的 argv token 取雜湊
    （而不是先經 argparse 解析成 dict 再雜湊），仍必須擋掉可能夾帶機敏值的
    旗標——沿用 ``_safe_config`` 對 Mapping key 已有的 ``_SECRET_KEY_RE``
    判斷規則：把 ``--flag`` token 當作 key 比對，命中時整個丟棄；
    ``--flag=value`` 一個 token 內就把整個 token 丟掉，``--flag value`` 兩個
    token 則額外丟掉緊接在後面、看起來不是旗標的下一個 token（也就是它的
    值）。目前 manager daemon 的旗標（見 ``coordinator/manager_daemon.py`` 的
    ``argparse`` 定義：poll-interval／executor／model 等）都不是機敏值，這裡
    純粹是防禦未來新增旗標，不依賴、也不假設目前的旗標清單。"""

    safe: list[str] = []
    drop_next_value = False
    for token in argv:
        if not isinstance(token, str):
            raise RuntimeAttestationError("invocation argv token 型別不合法")
        if drop_next_value:
            drop_next_value = False
            if not token.startswith("-"):
                continue
        match = _FLAG_TOKEN_RE.fullmatch(token)
        if match and _SECRET_KEY_RE.search(match.group(1)):
            if match.group(2) is None:
                drop_next_value = True
            continue
        safe.append(token)
    return safe


def manager_configuration_snapshot(
    arguments: Mapping[str, object],
    environment: Mapping[str, str],
    *,
    argv: Sequence[str] | None = None,
) -> tuple[dict[str, object], dict[str, str]]:
    """建立 Manager 有效配置摘要，以及可獨立比對的元件摘要。

    ``invocation_revision`` 改成以 daemon 實際收到的原始 argv token
    （``argv``）計算，不再用 argparse 解析後的 namespace（``arguments``）：
    namespace 對每個未指定的旗標一律套用預設值，其中一部分預設值本身是讀
    環境變數算出來的（例如 ``--tick-interval``／``--max-load`` 未指定時，
    預設值來自 ``PSC_TICK_INTERVAL_SECONDS``／``PSC_MANAGER_MAX_LOAD`` 等環境
    變數）。這些「被環境影響出來的預設值」已經由 ``environment_revision``
    涵蓋一次；再用解析後 namespace 算 ``invocation_revision`` 等於把同一份
    環境資訊摻進第二個 component，而宣告端（``cortex service status``）只能
    從 systemd 有效 ``ExecStart`` 反推 argv token，重放不出 argparse 的
    default 邏輯，導致任何部署都無法達成 config match（#841 loaded runtime
    attestation 的既知缺口）。改成只雜湊 argv token 後，宣告端只要能從
    ExecStart 反推出同一份 token 清單（見 ``manager_declared_invocation_revision``）
    就能重建、不必知道任何 argparse 預設值或環境變數。

    ``arguments``（即回傳的 ``config["arguments"]``）維持吃解析後的
    namespace 不變——那是給人看的診斷欄位，不是比對用的 revision 來源。

    ``argv`` 省略時（既有呼叫端／測試）退回舊行為，以 ``arguments`` 算
    ``invocation_revision``，維持相容。"""

    safe_args = _safe_config(arguments)
    safe_env = safe_environment_projection(environment)
    env_revision = configuration_revision(safe_env)
    invocation_source: object = (
        _safe_invocation_argv(argv) if argv is not None else safe_args
    )
    invocation_revision = configuration_revision(invocation_source)
    config = {"arguments": safe_args, "environment": safe_env}
    return config, {
        "environment_revision": env_revision,
        "invocation_revision": invocation_revision,
    }


def manager_environment_revision(environment: Mapping[str, str]) -> str:
    return configuration_revision(safe_environment_projection(environment))


def cli_runtime_observation(
    *,
    instance: str,
    environment: Mapping[str, str],
    artifact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """描述這次短命 CLI 程序，不將它誤認為長駐服務。"""

    return {
        "status": "observed",
        "instance": instance,
        "pid": os.getpid(),
        "observed_at": _utc(),
        "artifact": _safe_artifact(artifact or artifact_identity()),
        "environment_revision": manager_environment_revision(environment),
    }


def _read_small_unit_descriptor(descriptor: int) -> bytes | None:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_RECEIPT_BYTES:
            return None
        content = bytearray()
        while len(content) <= _MAX_RECEIPT_BYTES:
            chunk = os.read(
                descriptor, min(65536, _MAX_RECEIPT_BYTES + 1 - len(content))
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(content) > _MAX_RECEIPT_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            return None
        return bytes(content)
    except OSError:
        return None


def _read_small_unit_file(unit_path: str) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(
            unit_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        return _read_small_unit_descriptor(descriptor)
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_unit_files(unit_path: str | None) -> list[tuple[str, bytes]] | None:
    if not isinstance(unit_path, str) or not unit_path:
        return None
    main_path = Path(unit_path)
    if not main_path.is_absolute():
        return None
    try:
        main_path.name.encode("utf-8")
    except UnicodeError:
        return None
    main_bytes = _read_small_unit_file(unit_path)
    if main_bytes is None:
        return None

    files = [(main_path.name, main_bytes)]
    dropin_dir = main_path.with_name(f"{main_path.name}.d")
    try:
        dropin_info = os.lstat(dropin_dir)
    except FileNotFoundError:
        return files
    except (OSError, UnicodeError, ValueError):
        return None
    if stat.S_ISLNK(dropin_info.st_mode) or not stat.S_ISDIR(dropin_info.st_mode):
        return None
    directory_fd = -1
    try:
        directory_fd = os.open(
            dropin_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError):
        return None

    try:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode):
            return None
        try:
            names = sorted(name for name in os.listdir(directory_fd) if name.endswith(".conf"))
            for name in names:
                name.encode("utf-8")
        except (OSError, UnicodeError):
            return None
        for name in names:
            descriptor = -1
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                content = _read_small_unit_descriptor(descriptor)
            except (OSError, UnicodeError, ValueError):
                return None
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            if content is None:
                return None
            files.append((f"{dropin_dir.name}/{name}", content))
        after = os.fstat(directory_fd)
        if (before.st_dev, before.st_ino, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_mtime_ns,
        ):
            return None
        return files
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        os.close(directory_fd)


def _unit_files_digest(files: list[tuple[str, bytes]] | None) -> str | None:
    if files is None:
        return None
    entries = [
        (name, hashlib.sha256(content).hexdigest()) for name, content in files
    ]
    return hashlib.sha256(_canonical_bytes(entries)).hexdigest()


def _systemd_unit_files(
    fragment_path: object, drop_in_paths: object
) -> list[tuple[str, bytes]] | None:
    """安全讀取 systemd 回報的完整 fragment/drop-in 集合。"""

    if not isinstance(fragment_path, str) or not fragment_path:
        return None
    if not Path(fragment_path).is_absolute() or not isinstance(drop_in_paths, str):
        return None
    try:
        dropins = shlex.split(drop_in_paths)
    except ValueError:
        return None
    if any(not Path(path).is_absolute() or "\\" in path for path in dropins):
        return None
    paths = [fragment_path, *dropins]
    files: list[tuple[str, bytes]] = []
    for path in paths:
        content = _read_small_unit_file(path)
        if content is None:
            return None
        files.append((path, content))
    return files


def _unit_exec_start(files: list[tuple[str, bytes]] | None) -> list[str] | None:
    if files is None:
        return None
    commands: list[list[str]] = []
    for _name, unit_bytes in files:
        try:
            lines = unit_bytes.decode("utf-8").splitlines()
        except UnicodeError:
            return None
        section = ""
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                if not line.endswith("]") or "]" in line[1:-1]:
                    return None
                section = line[1:-1].strip()
                continue
            if section != "Service":
                continue
            match = re.match(r"^\s*ExecStart\s*=(.*)$", raw)
            if match is None:
                if re.match(r"^ExecStart(?:\s|:|$)", line):
                    return None
                continue
            value = match.group(1)
            if not value.strip():
                commands.clear()
                continue
            try:
                arguments = shlex.split(value)
            except ValueError:
                return None
            if not arguments:
                return None
            arguments[0] = arguments[0].lstrip("-@:+!")
            if not arguments[0]:
                return None
            commands.append(arguments)
    return commands[0] if len(commands) == 1 else None


def _systemd_exec_start(value: object) -> list[str] | None:
    """解析 systemctl show 的 ExecStart 結構化輸出，不重讀 unit 語法。"""

    if not isinstance(value, str) or not value:
        return None
    matches = list(re.finditer(r"\bargv\[\]=", value))
    if len(matches) != 1:
        return None
    start = matches[0].end()
    end_match = re.search(r"\s*;\s*ignore_errors=|\s*}", value[start:])
    if end_match is None:
        return None
    raw_argv = value[start : start + end_match.start()]
    try:
        argv = shlex.split(raw_argv)
    except ValueError:
        return None
    return argv or None


def _systemd_environment(value: object) -> dict[str, str] | None:
    if not isinstance(value, str):
        return None
    if not value.strip():
        return {}
    try:
        entries = shlex.split(value)
    except ValueError:
        return None
    environment: dict[str, str] = {}
    for entry in entries:
        match = _ENV_ASSIGNMENT_RE.fullmatch(entry)
        if match is None:
            return None
        environment[match.group(1)] = match.group(2)
    return environment


def _environment_file_paths(value: object) -> list[tuple[str, bool]] | None:
    if not isinstance(value, str):
        return None
    if not value.strip():
        return []
    pattern = re.compile(r"([^\s()]+) \(ignore_errors=(yes|no)\)")
    paths: list[tuple[str, bool]] = []
    offset = 0
    for match in pattern.finditer(value):
        if value[offset : match.start()].strip():
            return None
        path = match.group(1)
        if not Path(path).is_absolute() or "\\" in path:
            return None
        paths.append((path, match.group(2) == "yes"))
        offset = match.end()
    if value[offset:].strip():
        return None
    return paths


def _parse_environment_file(content: bytes) -> dict[str, str] | None:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        return None
    environment: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.lstrip()
        if not line or line.startswith("#"):
            continue
        if "\\" in line or "\x00" in line:
            return None
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if match is None:
            return None
        value = match.group(2)
        if value.startswith(("'", '"')):
            quote = value[0]
            # systemd 的引號值內，另一種引號是字面值（例如 #857 文件允許的
            # JSON argv：`'["/abs", "..."]'`）；只禁止同種引號本身出現在值內。
            # 反斜線已在上方整行拒絕，因此不必處理跳脫；多段串接仍不支援。
            quoted = re.fullmatch(
                re.escape(quote) + r"([^" + re.escape(quote) + r"]*)" + re.escape(quote) + r"\s*",
                value,
            )
            if quoted is None:
                return None
            value = quoted.group(1)
        elif any(character.isspace() or character in "'\"" for character in value):
            return None
        environment[match.group(1)] = value
    return environment


def _systemd_environment_sources(
    properties: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str]] | None:
    environment = _systemd_environment(properties.get("Environment"))
    paths = _environment_file_paths(properties.get("EnvironmentFiles"))
    if environment is None or paths is None:
        return None
    from_files: dict[str, str] = {}
    for path, ignore_errors in paths:
        content = _read_small_unit_file(path)
        if content is None:
            if ignore_errors and not os.path.lexists(path):
                continue
            return None
        parsed = _parse_environment_file(content)
        if parsed is None:
            return None
        from_files.update(parsed)
    if (
        "PYTHONPATH" in environment
        and "PYTHONPATH" in from_files
        and environment["PYTHONPATH"] != from_files["PYTHONPATH"]
    ):
        return None
    return environment, from_files


def _working_directory_artifact(path: object) -> dict[str, object] | None:
    """若工作目錄會遮蔽安裝套件，回傳該來源；無法證明時回傳 unknown。"""

    if not isinstance(path, str) or not Path(path).is_absolute():
        return _safe_artifact({})
    candidate = Path(path) / "paulsha_cortex"
    try:
        info = os.lstat(candidate)
    except FileNotFoundError:
        return None
    except OSError:
        return _safe_artifact({})
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return _safe_artifact({})
    return artifact_identity_from_package_root(candidate)


def _env_command_assignments(argv: list[str]) -> tuple[dict[str, str], int]:
    if not argv or argv[0] != "/usr/bin/env":
        return {}, 0
    index = 1
    assignments: dict[str, str] = {}
    while index < len(argv):
        assignment = _ENV_ASSIGNMENT_RE.fullmatch(argv[index])
        if assignment is None:
            break
        assignments[assignment.group(1)] = assignment.group(2)
        index += 1
    return assignments, index


def _effective_env_value(
    key: str,
    *,
    env_assignments: Mapping[str, str],
    environment: Mapping[str, str],
    from_files: Mapping[str, str],
) -> tuple[bool, str | None]:
    """單一環境變數（例如 ``PYTHONPATH``／``PY``）目前有效值的判定。

    優先序：ExecStart 內 ``/usr/bin/env KEY=VAL`` 命令層前綴 > systemd
    ``Environment=`` 指令層 > ``EnvironmentFile``。``EnvironmentFile`` 與另外
    兩層的值互相衝突時視為無法安全判定──這種變數（PYTHONPATH／PY）任何一段
    衝突都可能改變實際載入的套件／直譯器，衝突時不臆測任何一邊，回傳
    ``(False, None)``。"""

    env_value = environment.get(key)
    file_value = from_files.get(key)
    command_value = env_assignments.get(key)
    if file_value is not None and any(
        value is not None and value != file_value
        for value in (env_value, command_value)
    ):
        return False, None
    if command_value is not None:
        return True, command_value
    if file_value is not None:
        return True, file_value
    return True, env_value


def _effective_pythonpath(
    *,
    env_assignments: Mapping[str, str],
    environment: Mapping[str, str],
    from_files: Mapping[str, str],
) -> tuple[bool, str | None]:
    return _effective_env_value(
        "PYTHONPATH",
        env_assignments=env_assignments,
        environment=environment,
        from_files=from_files,
    )


def _pythonpath_artifact(pythonpath: str) -> dict[str, object]:
    """依 Python 實際匯入順序（依序走訪 PYTHONPATH 各路徑段）找出第一個提供
    ``paulsha_cortex`` 套件的位置；只看第一段會在套件其實裝在後面段落時誤判
    unknown。第一個命中的段落若是 symlink 或非目錄，視為不安全，直接回傳
    unknown，不再往後找（避免攻擊者用假的第一段掩蓋真正被載入的位置）。"""
    for segment in pythonpath.split(os.pathsep):
        if not segment or not Path(segment).is_absolute():
            continue
        candidate = Path(segment) / "paulsha_cortex"
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            return _safe_artifact({})
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return _safe_artifact({})
        return artifact_identity_from_package_root(candidate)
    return _safe_artifact({})


def _is_python_executable(value: str) -> bool:
    return re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(value).name) is not None


def _env_python_module_artifact(
    argv: list[str], *, module: str
) -> dict[str, object] | None:
    return _python_module_artifact(argv, module=module, environment={}, from_files={})


def _python_module_artifact(
    argv: list[str],
    *,
    module: str,
    environment: Mapping[str, str],
    from_files: Mapping[str, str],
    working_directory: str | None = None,
) -> dict[str, object] | None:
    if not argv:
        return None
    env_assignments, command_index = _env_command_assignments(argv)
    if (
        command_index + 2 >= len(argv)
        or not _is_python_executable(argv[command_index])
        or argv[command_index + 1 : command_index + 3] != ["-m", module]
    ):
        return None
    path_known, pythonpath = _effective_pythonpath(
        env_assignments=env_assignments,
        environment=environment,
        from_files=from_files,
    )
    if not path_known:
        return _safe_artifact({})
    if pythonpath is None:
        workdir_artifact = (
            _working_directory_artifact(working_directory)
            if working_directory is not None
            else None
        )
        if workdir_artifact is not None:
            return workdir_artifact
        return artifact_identity_from_python(argv[command_index])
    return _pythonpath_artifact(pythonpath)


def _declared_service_artifact(
    service: str,
    *,
    exec_path: str | None,
    argv: list[str] | None,
    environment: Mapping[str, str] | None = None,
    from_files: Mapping[str, str] | None = None,
    working_directory: str | None = None,
) -> dict[str, object]:
    if not argv:
        return _safe_artifact({})
    module = {
        "manager": "paulsha_cortex.coordinator.manager_daemon",
        "monitor": "paulsha_cortex.monitor",
    }.get(service)
    if module is not None:
        module_artifact = _python_module_artifact(
            argv,
            module=module,
            environment=environment or {},
            from_files=from_files or {},
            working_directory=working_directory,
        )
        if module_artifact is not None:
            return module_artifact
    if service == "monitor":
        if len(argv) < 3 or argv[0] != exec_path or argv[1:3] != ["-m", "paulsha_cortex.monitor"]:
            return _safe_artifact({})
        return artifact_identity_from_python(exec_path)
    if service == "manager":
        env_assignments, command_index = _env_command_assignments(argv)
        if len(argv) <= command_index + 1 or argv[command_index] != "bash":
            return _safe_artifact({})
        script = Path(argv[command_index + 1])
        if script.name != "service-manager.sh" or script.parent.name != "scripts":
            return _safe_artifact({})
        package_root = script.parent.parent
        if package_root.name != "paulsha_cortex":
            return _safe_artifact({})
        path_known, pythonpath = _effective_pythonpath(
            env_assignments=env_assignments,
            environment=environment or {},
            from_files=from_files or {},
        )
        if not path_known:
            return _safe_artifact({})
        if pythonpath is not None:
            return _pythonpath_artifact(pythonpath)
        if working_directory is not None:
            workdir_artifact = _working_directory_artifact(working_directory)
            if workdir_artifact is not None:
                return workdir_artifact
        # #1098：wrapper（``scripts/service-manager.sh``）實際執行哪個直譯器
        # 由 ``PY`` 環境變數決定（腳本內 ``PY=${PY:-$(command -v python3)}``）；
        # drop-in 以 ``Environment=PY=/other/venv/bin/python`` 覆寫時，之前這裡
        # 完全沒看 ``PY``，永遠回報 wrapper 腳本自己所在的套件根，與實際載入
        # 的 venv 不同步。有效 ``PY`` 可判定時，改用該直譯器的匯入結果；
        # ``PY`` 未被任何一層宣告（多數既有部署的實際狀態）時，沿用「wrapper
        # 與套件同根」既有假設；判定衝突（EnvironmentFile 與其他層不一致）時
        # 一律 unknown，不臆測。
        py_known, py_interpreter = _effective_env_value(
            "PY",
            env_assignments=env_assignments,
            environment=environment or {},
            from_files=from_files or {},
        )
        if not py_known:
            return _safe_artifact({})
        if py_interpreter is not None:
            return artifact_identity_from_python(py_interpreter)
        return artifact_identity_from_package_root(package_root)
    return _safe_artifact({})


def _environment_source_and_overlay(
    row: object,
) -> tuple[str, dict[str, str]]:
    """單個 service 目前的 ``environment_source`` 與其有效環境（已套用
    ``safe_environment_projection`` 的安全鍵值白名單投影，值仍是原始字串）。

    判定只依賴這個 row 的 ``systemd``／``_systemd_unavailable`` 欄位，與
    artifact 判定（``unit_files``／``effective_argv``）完全獨立，因此可以安全地
    在 ``service_declaration_projection``（對外只留 digest）與
    ``service_environment_overlay``（內部重建用，含真實值）兩處共用同一份
    結果——這是 doctor 與 ``cortex service status`` 對同一 service 的結論保證
    一致的關鍵：兩邊都只能透過這個函式判定，不得各自另外解析。"""

    if not isinstance(row, Mapping):
        return "unavailable", {}
    properties = row.get("systemd")
    if isinstance(properties, Mapping):
        required_properties = {
            "ExecStart",
            "Environment",
            "EnvironmentFiles",
            "DropInPaths",
            "FragmentPath",
            "WorkingDirectory",
        }
        if not required_properties.issubset(properties):
            return "unknown", {}
        environment_sources = _systemd_environment_sources(properties)
        if environment_sources is None:
            return "unknown", {}
        environment, from_files = environment_sources
        effective_argv = _systemd_exec_start(properties.get("ExecStart"))
        env_assignments: dict[str, str] = {}
        if effective_argv:
            env_assignments, _index = _env_command_assignments(effective_argv)
        merged_environment = {**from_files, **environment, **env_assignments}
        return "systemd-effective", safe_environment_projection(merged_environment)
    if row.get("_systemd_unavailable") is True:
        # 探測不到 systemd 有效屬性集合，不能確認是否有 drop-in 覆寫；呼叫端應
        # 改用既有 direct-mode fallback，這裡維持 "unavailable" 不臆測。
        return "unavailable", {}
    return "unavailable", {}


def manager_declared_invocation_revision(
    row: object, environment: Mapping[str, str]
) -> str | None:
    """從 manager unit 目前的 systemd 有效 ``ExecStart`` 反推 daemon 實際收到
    的 argv，換算成跟 ``manager_configuration_snapshot`` 同一份
    ``invocation_revision``，供 ``cortex service status`` 的
    ``declared_invocation_revision`` 用。只在能證明形狀、且拿得到必要值時
    回傳字串，其餘情況回傳 ``None``（讓 ``compare_runtime_state`` 維持
    unknown，不臆測）。

    目前已知會出現的兩種 ExecStart 形狀：

    1. 直接呼叫 ``[env KEY=VAL ...] python3 -m
       paulsha_cortex.coordinator.manager_daemon <args...>``——``<args...>``
       就是 daemon 收到的 argv，直接取用（沿用 ``_env_command_assignments``
       跳過 ``/usr/bin/env`` 前綴、``_is_python_executable`` 判斷 python
       執行檔，判斷邏輯與 ``_python_module_artifact`` 對齊，避免兩邊各自
       另開一套 shape 判定）。

    2. installer（``paulsha_cortex/deploy/installer.py`` 的
       ``render_units``／``manager.service.tmpl``）目前實際產生的形狀：
       ``ExecStart=/usr/bin/env bash <pkg>/scripts/service-manager.sh``。
       daemon 的 module 呼叫發生在這個腳本內部的背景子行程（見
       ``service-manager.sh`` 的 ``start_manager_loop``），ExecStart 本身看
       不到；但腳本原始碼是套件內固定內容，永遠只傳一個旗標：
       ``--specs-dir "${PSC_MANAGER_SPECS_DIR:-$HOME/.agents/specs}"``。
       若 ``PSC_MANAGER_SPECS_DIR`` 有在有效環境（``environment``，即
       ``_environment_source_and_overlay`` 算出的 ``PSC_*``／
       ``PAULSHACLAW_*`` 投影）中宣告，可以精確重建這個 argv；若沒有，
       預設值取決於執行 systemd ``--user`` 服務的 ``$HOME``，這個值不在
       ``safe_environment_projection`` 的允許清單內，沒有安全管道可以驗證，
       因此回傳 ``None`` 而不是猜測家目錄。"""

    if not isinstance(row, Mapping):
        return None
    properties = row.get("systemd")
    if not isinstance(properties, Mapping):
        return None
    effective_argv = _systemd_exec_start(properties.get("ExecStart"))
    if not effective_argv:
        return None
    _env_assignments, command_index = _env_command_assignments(effective_argv)
    remaining = effective_argv[command_index:]
    if (
        len(remaining) >= 3
        and _is_python_executable(remaining[0])
        and remaining[1:3] == ["-m", "paulsha_cortex.coordinator.manager_daemon"]
    ):
        declared_argv = remaining[3:]
    elif len(remaining) >= 2 and remaining[0] == "bash":
        script = Path(remaining[1])
        if not (
            script.name == "service-manager.sh"
            and script.parent.name == "scripts"
            and script.parent.parent.name == "paulsha_cortex"
        ):
            return None
        specs_dir = environment.get("PSC_MANAGER_SPECS_DIR")
        if specs_dir is None:
            return None
        declared_argv = ["--specs-dir", specs_dir]
    else:
        return None
    try:
        return configuration_revision(_safe_invocation_argv(declared_argv))
    except RuntimeAttestationError:
        return None


def service_environment_overlay(
    units: object, *, instance: str
) -> dict[str, dict[str, object]]:
    """回傳每個 service 目前的有效環境來源與內容，供 doctor／``cortex service
    status`` 內部重建 root／config 用；判定規則與 ``service_declaration_projection``
    共用同一個 ``_environment_source_and_overlay``，確保兩者結論一致。

    回傳值的 ``environment`` 含真實環境值（僅 ``PSC_*``／``PAULSHACLAW_*``），
    只能在程序內部使用，絕對不能序列化進 JSON 輸出或 CLI 顯示——對外一律使用
    ``service_declaration_projection`` 回傳的 ``environment_digest``。"""

    rows = units if isinstance(units, Mapping) else {}
    result: dict[str, dict[str, object]] = {}
    for service in ("manager", "monitor"):
        unit_name = f"{instance}-{service}.service"
        source, overlay = _environment_source_and_overlay(rows.get(unit_name))
        result[service] = {"environment_source": source, "environment": overlay}
    return result


def service_declaration_projection(
    units: object, *, instance: str
) -> dict[str, dict[str, object]]:
    """投影 service 宣告欄位，不回傳環境值或檔案內容。

    附帶回傳每個 service 目前有效環境的摘要（``environment_digest``：對
    ``safe_environment_projection`` 後的內容取 canonical SHA-256）與其來源標示
    （``environment_source``），讓 service status／doctor 可以共用同一份
    「systemctl show 有效值優先於 fallback 讀檔」判定，不必各自重新解析 unit 檔
    或 EnvironmentFile。這個函式只回傳摘要，不回傳環境原值——真正的值只能透過
    ``service_environment_overlay`` 在程序內部取得，且該函式的回傳值不得序列化
    進 JSON 輸出。"""

    rows = units if isinstance(units, Mapping) else {}
    result: dict[str, dict[str, object]] = {}
    for service in ("manager", "monitor"):
        unit_name = f"{instance}-{service}.service"
        row = rows.get(unit_name)
        if not isinstance(row, Mapping):
            result[service] = {
                "unit": unit_name,
                "status": "unknown",
                "pid": None,
                "exec_path_sha256": None,
                "disk_unit_sha256": None,
                "artifact": _safe_artifact({}),
                "stale": None,
                "environment_source": "unavailable",
                "environment_digest": configuration_revision({}),
            }
            continue
        exec_path = row.get("exec_path")
        path_value = exec_path if isinstance(exec_path, str) else None
        properties = row.get("systemd")
        environment: dict[str, str] | None = None
        from_files: dict[str, str] | None = None
        declaration_known = True
        # 環境來源判定與 artifact 判定分開計算：exec artifact 需要能雜湊 unit/
        # drop-in 檔案內容才算「known」，但有效環境只要 Environment=／
        # EnvironmentFiles= 能安全解析即可信任，不需要連帶依賴 unit 檔雜湊成功。
        # 環境來源／有效環境本身改由 ``_environment_source_and_overlay`` 統一判定
        # （與 ``service_environment_overlay`` 共用），這裡只留 artifact 判定要用
        # 的 unit_files／effective_argv／environment／from_files。
        if isinstance(properties, Mapping):
            required_properties = {
                "ExecStart",
                "Environment",
                "EnvironmentFiles",
                "DropInPaths",
                "FragmentPath",
                "WorkingDirectory",
            }
            if not required_properties.issubset(properties):
                declaration_known = False
                unit_files = None
                effective_argv = None
            else:
                unit_files = _systemd_unit_files(
                    properties.get("FragmentPath"),
                    properties.get("DropInPaths"),
                )
                effective_argv = _systemd_exec_start(properties.get("ExecStart"))
                environment_sources = _systemd_environment_sources(properties)
                if environment_sources is None:
                    declaration_known = False
                else:
                    environment, from_files = environment_sources
                declaration_known = declaration_known and unit_files is not None
                declaration_known = declaration_known and effective_argv is not None
        elif row.get("_systemd_unavailable") is True:
            # Without systemd's effective property set, the probe cannot prove that its
            # file-only view covers every configured unit/drop-in search directory.
            declaration_known = False
            unit_files = None
            effective_argv = None
        else:
            unit_path = row.get("path")
            unit_files = _read_unit_files(
                unit_path if isinstance(unit_path, str) else None
            )
            effective_argv = _unit_exec_start(unit_files)
            declaration_known = unit_files is not None and effective_argv is not None
        unit_digest = _unit_files_digest(unit_files)
        effective_exec_path = effective_argv[0] if effective_argv else None
        environment_source, effective_environment = _environment_source_and_overlay(row)
        result[service] = {
            "unit": unit_name,
            "status": row.get("status") if isinstance(row.get("status"), str) else "unknown",
            "pid": row.get("pid") if type(row.get("pid")) is int else None,
            "exec_path_sha256": (
                hashlib.sha256(os.fsencode(effective_exec_path)).hexdigest()
                if effective_exec_path is not None
                else None
            ),
            "disk_unit_sha256": unit_digest,
            "artifact": (
                _declared_service_artifact(
                    service,
                    exec_path=path_value,
                    argv=effective_argv,
                    environment=environment,
                    from_files=from_files,
                    working_directory=(
                        properties.get("WorkingDirectory")
                        if isinstance(properties, Mapping)
                        else None
                    ),
                )
                if declaration_known
                else _safe_artifact({})
            ),
            "stale": row.get("stale") if type(row.get("stale")) is bool else None,
            "environment_source": environment_source,
            "environment_digest": configuration_revision(effective_environment),
        }
        if service == "manager":
            # live 驗收發現：`probe_service_runtime` 對外回傳的 units 已移除
            # `systemd` 原始屬性，`cortex service status` 事後拿不到 ExecStart，
            # 宣告端 invocation 永遠是 None。必須在這裡（原始 row 仍在手上）先算
            # 出宣告值；投影只放 sha256 摘要，不含 argv 原文。
            result[service]["invocation_revision"] = (
                manager_declared_invocation_revision(row, effective_environment)
                if environment_source == "systemd-effective"
                else None
            )
    return result


def unknown_runtime_report(
    reason: str, current_artifact: Mapping[str, object]
) -> dict[str, object]:
    """單一 service 目前狀態判定不出來時的標準投影。

    ``cortex service status`` 與 doctor 的 loaded-runtime 判定共用同一個
    shape，確保兩邊在同一種「宣告存在但無法安全信任」情境下（例如
    ``environment_source`` 判定為 ``unknown``，或有效環境算出來卻連不上
    receipt）回報的結構完全一致——這是 #841 對抗審查第五輪修掉「doctor 另起
    一份精簡版 unknown 結構，與 service status 對不起來」那組回歸的關鍵。"""

    is_installed = current_artifact.get("kind") == "installed-wheel"
    return {
        "status": "unknown",
        "reason": reason,
        "loaded": None,
        "installed_artifact": current_artifact if is_installed else {
            "kind": "unknown",
            "package": current_artifact.get("package"),
            "package_version": "unknown",
            "source_revision": "unknown",
            "sha256": None,
        },
        "current_artifact": current_artifact,
        "comparison": {
            "status": "unknown",
            "reason": reason,
            "artifact_status": "unknown",
            "config_status": "unknown",
            "transition_disposition": "unknown-in-flight-state",
            "transition_safe": False,
        },
        "initial_config_revision": None,
        "effective_config_revision": None,
        "previous_process_start": None,
        "trust_root": {"status": "unknown"},
    }


def declared_service_environment(
    declaration: object,
    *,
    direct_fallback: Callable[[], Mapping[str, str]],
) -> tuple[dict[str, str], str]:
    """單一 service 依目前 ``environment_source`` 判定會拿到的有效環境。

    判定規則只有這裡一份：``systemd-effective`` 直接信任已套用 drop-in 的
    有效值；``unavailable``（探測不到 systemd 有效屬性，需要退回既有讀法）
    交給呼叫端提供的 ``direct_fallback``；``unknown``（屬性存在但無法安全
    解析，例如 ``Environment=``／``EnvironmentFiles=`` 的 ``PYTHONPATH`` 互相
    衝突）一律視為無法信任，回傳空環境，不得沿用另一個 service 的值或呼叫端
    自己的殼層環境頂替。

    ``cortex service status``（讀真實 ``os.environ`` 下的既有 EnvironmentFile）
    與 doctor（可注入 ``home``／``base_env`` 的 hermetic 讀法）行為差異只在
    各自的 ``direct_fallback`` 實作，兩邊都必須經過這個函式判斷分支，不得各自
    重新判斷 source——這是 #841 對抗審查第五輪 MAJOR 指出「doctor 用 manager
    的環境去比對 monitor，與 service status 不一致」那組回歸的修法核心。"""

    source = (
        declaration.get("environment_source")
        if isinstance(declaration, Mapping)
        else None
    )
    overlay = declaration.get("environment") if isinstance(declaration, Mapping) else None
    if source == "systemd-effective" and isinstance(overlay, Mapping):
        return dict(overlay), "systemd-effective"
    if source == "unavailable":
        try:
            fallback = direct_fallback()
        except Exception:
            return {}, "unknown"
        return dict(fallback), "direct-fallback"
    return {}, "unknown"


def monitor_configuration_revision_from_environment(
    environment: Mapping[str, str],
) -> str | None:
    """解析 Monitor 目前有效配置，只回傳摘要，不暴露配置值。"""

    try:
        from unittest.mock import patch

        from .monitor.config import load_config

        with patch.dict(os.environ, dict(environment), clear=True):
            return configuration_revision(load_config())
    except Exception:
        return None


def _tree_digest(package_root: Path) -> str | None:
    if package_root.is_symlink() or not package_root.is_dir():
        return None
    files: list[tuple[str, str]] = []
    try:
        for path in package_root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            relative = path.relative_to(package_root).as_posix()
            content_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append((relative, content_digest))
    except OSError:
        return None
    if not files:
        return None
    files.sort()
    return hashlib.sha256(_canonical_bytes(files)).hexdigest()


def artifact_identity_from_package_root(
    package_root: Path, *, site_packages: Path | None = None
) -> dict[str, object]:
    """從 service 宣告指向的套件樹辨識安裝來源與 artifact。"""

    root = package_root.expanduser()
    site = site_packages or root.parent
    distribution = None
    try:
        for candidate in importlib.metadata.distributions(path=[str(site)]):
            name = candidate.metadata.get("Name", "").lower().replace("_", "-")
            if name == "paulsha-cortex":
                distribution = candidate
                break
    except (OSError, ValueError, TypeError):
        distribution = None
    version = "unknown"
    source_revision = "unknown"
    editable = False
    same_root = False
    if distribution is not None:
        try:
            version = str(distribution.version)
            installed_root = Path(distribution.locate_file("paulsha_cortex"))
            same_root = installed_root.resolve() == root.resolve()
            direct_url = distribution.read_text("direct_url.json")
            direct_payload = json.loads(direct_url) if direct_url else {}
            dir_info = (
                direct_payload.get("dir_info")
                if isinstance(direct_payload, dict)
                else None
            )
            editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            version = "unknown"
        source_revision = _distribution_source_revision(distribution)
    digest = _tree_digest(root)
    kind = (
        "unknown"
        if digest is None
        else "installed-wheel"
        if distribution is not None and same_root and not editable
        else "source-override"
    )
    return _safe_artifact(
        {
            "kind": kind,
            "package": "paulsha-cortex",
            "package_version": version,
            "source_revision": source_revision,
            "sha256": digest,
        }
    )


def artifact_identity_from_python(executable: str | Path | None) -> dict[str, object]:
    """依照 service 宣告的 Python prefix 找出該環境安裝的 Cortex 套件。"""

    if not isinstance(executable, (str, Path)) or not str(executable):
        return _safe_artifact({})
    interpreter = Path(executable).expanduser()
    if not interpreter.is_absolute():
        return _safe_artifact({})
    prefix = interpreter.parent.parent
    site_directories = sorted(
        (*prefix.glob("lib/python*/site-packages"), *prefix.glob("lib64/python*/site-packages"))
    )
    identities = [
        artifact_identity_from_package_root(site / "paulsha_cortex", site_packages=site)
        for site in site_directories
        if (site / "paulsha_cortex").is_dir()
    ]
    for identity in identities:
        if identity.get("kind") != "unknown":
            return identity
    return _safe_artifact({})


def _distribution_source_revision(distribution: Any) -> str:
    try:
        raw = distribution.read_text("direct_url.json")
        payload = json.loads(raw) if raw else {}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return "unknown"
    vcs_info = payload.get("vcs_info") if isinstance(payload, dict) else None
    revision = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
    if isinstance(revision, str) and _REVISION_RE.fullmatch(revision.lower()):
        return revision.lower()
    return "unknown"


def artifact_identity(
    *, package_root: Path | None = None, source_override: bool | None = None
) -> dict[str, object]:
    """辨識匯入中的套件樹，區分 checkout source override 與已安裝 wheel。"""

    distribution = None
    imported_root = package_root
    if imported_root is None:
        try:
            module = importlib.import_module("paulsha_cortex")
            module_file = getattr(module, "__file__", None)
            imported_root = Path(module_file).resolve().parent if module_file else None
        except (ImportError, OSError, RuntimeError):
            imported_root = None
        try:
            distribution = importlib.metadata.distribution("paulsha-cortex")
        except importlib.metadata.PackageNotFoundError:
            distribution = None

    installed_root: Path | None = None
    editable = False
    if distribution is not None:
        try:
            installed_root = Path(
                distribution.locate_file("paulsha_cortex")
            ).resolve()
            raw_direct_url = distribution.read_text("direct_url.json")
            direct_url = json.loads(raw_direct_url) if raw_direct_url else {}
            dir_info = direct_url.get("dir_info") if isinstance(direct_url, dict) else None
            editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            installed_root = None

    same_import = False
    if imported_root is not None and installed_root is not None:
        try:
            same_import = imported_root.resolve() == installed_root.resolve()
        except OSError:
            same_import = False
    is_source = (
        source_override is True
        or imported_root is None
        or (distribution is None)
        or (not same_import)
        or editable
    )
    selected_root = imported_root
    package_version = "unknown"
    source_revision = "unknown"
    if distribution is not None:
        try:
            package_version = str(distribution.version)
        except (OSError, ValueError, TypeError):
            package_version = "unknown"
        source_revision = _distribution_source_revision(distribution)
    if is_source:
        revision = os.environ.get("PSC_SOURCE_REVISION", "").lower()
        source_revision = revision if _REVISION_RE.fullmatch(revision) else "unknown"
        kind = "source-override"
    else:
        kind = "installed-wheel"
        selected_root = installed_root

    digest = _tree_digest(selected_root) if selected_root is not None else None
    return {
        "kind": kind if digest is not None else "unknown",
        "package": "paulsha-cortex",
        "package_version": package_version,
        "source_revision": source_revision,
        "sha256": digest,
    }


def _utc(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise RuntimeAttestationError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeAttestationError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _root_digest(state_root: Path) -> str:
    normalized = Path(os.path.abspath(state_root.expanduser()))
    return hashlib.sha256(os.fsencode(str(normalized))).hexdigest()


def _safe_artifact(identity: Mapping[str, object]) -> dict[str, object]:
    kind = identity.get("kind")
    digest = identity.get("sha256")
    package = identity.get("package")
    version = identity.get("package_version")
    revision = identity.get("source_revision")
    if kind not in {"installed-wheel", "source-override", "unknown"}:
        kind = "unknown"
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        digest = None
        kind = "unknown"
    return {
        "kind": kind,
        "package": "paulsha-cortex" if package == "paulsha-cortex" else "unknown",
        "package_version": (
            version
            if isinstance(version, str) and _PACKAGE_VERSION_RE.fullmatch(version)
            else "unknown"
        ),
        "source_revision": (
            revision.lower()
            if isinstance(revision, str)
            and (_REVISION_RE.fullmatch(revision.lower()) or revision == "unknown")
            else "unknown"
        ),
        "sha256": digest,
    }


def _safe_trust_root(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {"status": "unknown", "reason": "install-receipt-unavailable"}
    status = value.get("status")
    if status not in {"verified", "unknown", "drift", "rolled-back"}:
        status = "unknown"
    result: dict[str, object] = {"status": status}
    for key in (
        "receipt_id",
        "plan_sha256",
        "receipt_sha256",
        "activation_revision",
        "inventory_sha256",
        "verification_sha256",
        "rollback_revision",
    ):
        item = value.get(key)
        if isinstance(item, str) and _SHA256_RE.fullmatch(item):
            result[key] = item
    receipt_id = value.get("receipt_id")
    if isinstance(receipt_id, str) and _UUID_RE.fullmatch(receipt_id):
        result["receipt_id"] = receipt_id
    reason = value.get("reason")
    if reason in {
        "install-receipt-unavailable",
        "install-receipt-unconfigured",
        "install-receipt-invalid",
        "install-rollback-blocked",
        "install-rollback-incomplete",
    }:
        result["reason"] = reason
    in_flight = value.get("in_flight_jobs")
    if type(in_flight) is int and in_flight >= 0:
        result["in_flight_jobs"] = in_flight
    return result


def _open_directory(path: Path, *, create: bool) -> int:
    expanded = path.expanduser()
    if not expanded.is_absolute() or ".." in expanded.parts:
        raise RuntimeAttestationError("runtime state path is unsafe")
    normalized = Path(os.path.abspath(expanded))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open("/", flags)
    try:
        for component in normalized.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=descriptor)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                os.close(child)
                raise RuntimeAttestationError("runtime state path contains a non-directory")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _attestation_directory(state_root: Path) -> Path:
    root = state_root.expanduser()
    if not root.is_absolute() or ".." in root.parts:
        raise RuntimeAttestationError("runtime state root is unsafe")
    return Path(os.path.abspath(root / "runtime-attestations"))


def _open_attestation_directory(state_root: Path, *, create: bool) -> tuple[Path, int]:
    directory = _attestation_directory(state_root)
    descriptor = _open_directory(directory, create=create)
    info = os.fstat(descriptor)
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.close(descriptor)
        raise RuntimeAttestationError("runtime attestation directory permissions are unsafe")
    return directory, descriptor


def _write_immutable_receipt(state_root: Path, document: Mapping[str, object]) -> Path:
    encoded = _canonical_bytes(document) + b"\n"
    if len(encoded) > _MAX_RECEIPT_BYTES:
        raise RuntimeAttestationError("runtime receipt exceeds size limit")
    service = str(document["service"])
    started_at = str(document["process_started_at"])
    epoch = int(datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp())
    name = (
        f"{service}-{epoch:010d}-{int(document['event_seq']):06d}-"
        f"{document['receipt_id']}.json"
    )
    directory, directory_fd = _open_attestation_directory(state_root, create=True)
    temporary_name = f".runtime-attestation-{uuid.uuid4()}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)
    return directory / name


def _normalize_document(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RuntimeAttestationError("receipt payload is invalid")
    if payload.get("schema") != RUNTIME_ATTESTATION_SCHEMA:
        raise RuntimeAttestationError("receipt schema is unknown")
    required = {
        "receipt_id",
        "process_id",
        "event_seq",
        "event_type",
        "previous_receipt_id",
        "service",
        "instance",
        "instance_root_sha256",
        "pid",
        "process_started_at",
        "recorded_at",
        "artifact",
        "config",
        "trust_root",
    }
    if not required.issubset(payload):
        raise RuntimeAttestationError("receipt fields are incomplete")
    receipt_id = payload.get("receipt_id")
    process_id = payload.get("process_id")
    config = payload.get("config")
    artifact = payload.get("artifact")
    components = config.get("components") if isinstance(config, dict) else None
    if (
        payload.get("service") not in _SERVICES
        or not isinstance(payload.get("instance"), str)
        or _INSTANCE_RE.fullmatch(payload["instance"]) is None
        or not isinstance(receipt_id, str)
        or _UUID_RE.fullmatch(receipt_id) is None
        or not isinstance(process_id, str)
        or _UUID_RE.fullmatch(process_id) is None
        or not isinstance(payload.get("instance_root_sha256"), str)
        or _SHA256_RE.fullmatch(payload["instance_root_sha256"]) is None
        or type(payload.get("pid")) is not int
        or payload["pid"] <= 0
        or type(payload.get("event_seq")) is not int
        or payload["event_seq"] < 0
        or payload.get("event_type") not in {"startup", "config-reload"}
        or not isinstance(artifact, dict)
        or not isinstance(config, dict)
        or not isinstance(components, dict)
        or not isinstance(payload.get("trust_root"), dict)
    ):
        raise RuntimeAttestationError("receipt identity is invalid")
    for timestamp_key in ("process_started_at", "recorded_at"):
        _utc(payload.get(timestamp_key))
    initial_revision = config.get("initial_revision")
    effective_revision = config.get("effective_revision")
    if (
        not isinstance(initial_revision, str)
        or _SHA256_RE.fullmatch(initial_revision) is None
        or not isinstance(effective_revision, str)
        or _SHA256_RE.fullmatch(effective_revision) is None
    ):
        raise RuntimeAttestationError("receipt config revision is invalid")
    safe_components: dict[str, str] = {}
    for key, value in components.items():
        if (
            isinstance(key, str)
            and not _SECRET_KEY_RE.search(key)
            and len(key) <= 128
            and isinstance(value, str)
            and _SHA256_RE.fullmatch(value)
        ):
            safe_components[key] = value
    previous_id = payload.get("previous_receipt_id")
    if previous_id is not None and (
        not isinstance(previous_id, str) or _UUID_RE.fullmatch(previous_id) is None
    ):
        raise RuntimeAttestationError("receipt previous id is invalid")
    return {
        "schema": RUNTIME_ATTESTATION_SCHEMA,
        "receipt_id": receipt_id,
        "process_id": process_id,
        "event_seq": payload["event_seq"],
        "event_type": payload["event_type"],
        "previous_receipt_id": previous_id,
        "service": payload["service"],
        "instance": payload["instance"],
        "instance_root_sha256": payload["instance_root_sha256"],
        "pid": payload["pid"],
        "process_started_at": _utc(payload["process_started_at"]),
        "recorded_at": _utc(payload["recorded_at"]),
        "artifact": _safe_artifact(artifact),
        "config": {
            "initial_revision": initial_revision,
            "effective_revision": effective_revision,
            "components": safe_components,
        },
        "trust_root": _safe_trust_root(payload["trust_root"]),
    }


def _read_document_at(directory_fd: int, name: str) -> dict[str, object]:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _MAX_RECEIPT_BYTES
        ):
            raise RuntimeAttestationError("receipt file is unsafe")
        content = bytearray()
        while len(content) <= _MAX_RECEIPT_BYTES:
            chunk = os.read(descriptor, min(65536, _MAX_RECEIPT_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > _MAX_RECEIPT_BYTES:
            raise RuntimeAttestationError("receipt file exceeds size limit")
        payload = json.loads(content.decode("utf-8"))
        after = os.fstat(descriptor)
        if (
            (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise RuntimeAttestationError("receipt changed while being read")
        return _normalize_document(payload)
    except RuntimeAttestationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeAttestationError("receipt file is corrupt") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unknown(reason: str) -> dict[str, object]:
    return {
        "status": "unknown",
        "reason": reason,
        "latest": None,
        "process_start": None,
        "initial_config_revision": None,
        "effective_config_revision": None,
        "previous_process_start": None,
        "receipt_count": 0,
    }


def record_runtime_startup(
    *,
    service: str,
    instance: str,
    state_root: Path,
    configuration: Mapping[str, object],
    artifact: Mapping[str, object] | None = None,
    started_at: str | None = None,
    pid: int | None = None,
    process_id: str | None = None,
    config_components: Mapping[str, str] | None = None,
    trust_root: Mapping[str, object] | None = None,
) -> Path:
    if service not in _SERVICES or _INSTANCE_RE.fullmatch(instance) is None:
        raise RuntimeAttestationError("service or instance identity is invalid")
    actual_pid = os.getpid() if pid is None else pid
    actual_process_id = process_id or str(uuid.uuid4())
    if (
        type(actual_pid) is not int
        or actual_pid <= 0
        or _UUID_RE.fullmatch(actual_process_id) is None
    ):
        raise RuntimeAttestationError("process identity is invalid")
    runtime_artifact = _safe_artifact(artifact or artifact_identity())
    effective_revision = configuration_revision(configuration)
    components = {
        key: value
        for key, value in (config_components or {}).items()
        if isinstance(key, str)
        and not _SECRET_KEY_RE.search(key)
        and len(key) <= 128
        and isinstance(value, str)
        and _SHA256_RE.fullmatch(value)
    }
    components.setdefault("effective_revision", effective_revision)
    timestamp = _utc(started_at)
    receipt_id = str(uuid.uuid4())
    document = {
        "schema": RUNTIME_ATTESTATION_SCHEMA,
        "receipt_id": receipt_id,
        "process_id": actual_process_id,
        "event_seq": 0,
        "event_type": "startup",
        "previous_receipt_id": None,
        "service": service,
        "instance": instance,
        "instance_root_sha256": _root_digest(state_root),
        "pid": actual_pid,
        "process_started_at": timestamp,
        "recorded_at": timestamp,
        "artifact": runtime_artifact,
        "config": {
            "initial_revision": effective_revision,
            "effective_revision": effective_revision,
            "components": components,
        },
        "trust_root": _safe_trust_root(trust_root),
    }
    return _write_immutable_receipt(state_root, document)


def record_config_reload(
    *,
    service: str,
    instance: str,
    state_root: Path,
    previous_receipt: Path,
    configuration: Mapping[str, object],
    recorded_at: str | None = None,
    config_components: Mapping[str, str] | None = None,
) -> Path:
    """在同一程序的 receipt chain 追加 ``config-reload`` 事件。

    **production 沒有呼叫端（#841 AC4 的 reload 條目 owner 裁決 N/A）**：Manager／
    Monitor 都沒有 hot config reload 入口，程序設定在啟動時固定，變更只能靠重啟
    生效（重啟寫新的 startup receipt）。此函式只保留 v1 receipt chain 的
    ``config-reload`` 事件格式，讓 reader 的 chain 驗證（stale parent 拒絕、initial／
    effective revision 分開）維持可測；沒有真正切換 in-memory config 的 reload owner
    之前不得從 production 呼叫，接線時須同步更新
    ``docs/loaded-runtime-attestation.md`` 與對應的 guard 測試。"""

    root_dir, directory_fd = _open_attestation_directory(state_root, create=False)
    try:
        if Path(os.path.abspath(previous_receipt.parent)) != root_dir:
            raise RuntimeAttestationError(
                "config reload receipt belongs to a different state root"
            )
        previous = _read_document_at(directory_fd, previous_receipt.name)
        if (
            previous.get("service") != service
            or previous.get("instance") != instance
            or previous.get("event_type") not in {"startup", "config-reload"}
        ):
            raise RuntimeAttestationError(
                "config reload is not bound to the active process"
            )
        state = _inspect_runtime_state_fd(
            directory_fd,
            service=service,
            instance=instance,
            state_root=state_root,
        )
        latest = state.get("latest")
        if (
            state.get("status") != "attested"
            or not isinstance(latest, Mapping)
            or latest.get("receipt_id") != previous.get("receipt_id")
        ):
            raise RuntimeAttestationError("config reload receipt is stale")
        revision = configuration_revision(configuration)
        components = dict(previous["config"].get("components", {}))
        for key, value in (config_components or {}).items():
            if (
                isinstance(key, str)
                and not _SECRET_KEY_RE.search(key)
                and len(key) <= 128
                and isinstance(value, str)
                and _SHA256_RE.fullmatch(value)
            ):
                components[key] = value
        components["effective_revision"] = revision
        document = {
            **previous,
            "receipt_id": str(uuid.uuid4()),
            "event_seq": int(previous["event_seq"]) + 1,
            "event_type": "config-reload",
            "previous_receipt_id": previous["receipt_id"],
            "recorded_at": _utc(recorded_at),
            "config": {
                "initial_revision": previous["config"]["initial_revision"],
                "effective_revision": revision,
                "components": components,
            },
        }
    finally:
        os.close(directory_fd)
    return _write_immutable_receipt(state_root, document)


#: receipt 的 ``process_started_at`` 與 /proc 推回的啟動時間之間容許的牆鐘位移
#: （NTP step、VM 時間同步）。超過即視為同 PID 的先前程序（PID 重用）。
_PROCESS_START_TOLERANCE_SECONDS = 120.0


def live_process_started_epoch(
    pid: object, *, proc_root: Path = Path("/proc")
) -> float | None:
    """Start time (epoch seconds) of a live process, from ``/proc/<pid>/stat``.

    ``starttime`` is clock ticks since boot and ``btime`` the boot time in the
    current wall-clock frame, so the result shifts with a clock step exactly as
    much as the receipt's own ``process_started_at`` may; a reused PID differs
    by the lifetime of the earlier process.  ``None`` when it cannot be read.
    """

    if type(pid) is not int or pid <= 0:
        return None
    try:
        stat_text = (proc_root / str(pid) / "stat").read_text(encoding="ascii")
        boot_text = (proc_root / "stat").read_text(encoding="ascii")
        fields = stat_text.rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        btime = next(
            int(line.split()[1])
            for line in boot_text.splitlines()
            if line.startswith("btime ")
        )
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, UnicodeError, IndexError, ValueError, StopIteration):
        return None
    if hz <= 0:
        return None
    return btime + ticks / hz


def _receipt_started_epoch(row: Mapping[str, object]) -> float | None:
    try:
        return datetime.fromisoformat(
            str(row.get("process_started_at")).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def _inspect_runtime_state_fd(
    directory_fd: int,
    *,
    service: str,
    instance: str,
    state_root: Path,
    expected_pid: int | None = None,
    expected_process_started_epoch: float | None = None,
) -> dict[str, object]:
    try:
        names = sorted(
            name
            for name in os.listdir(directory_fd)
            if name.startswith(f"{service}-") and name.endswith(".json")
        )
    except OSError:
        return _unknown("receipt-unavailable")
    if not names:
        return _unknown("receipt-missing")
    documents: list[dict[str, object]] = []
    try:
        for name in names:
            documents.append(_read_document_at(directory_fd, name))
    except RuntimeAttestationError as exc:
        reason = "schema-unknown" if "schema is unknown" in str(exc) else "receipt-corrupt"
        return _unknown(reason)
    matches = [
        row for row in documents
        if row.get("service") == service and row.get("instance") == instance
    ]
    if not matches:
        return _unknown("receipt-missing")
    expected_root = _root_digest(state_root)
    if any(row.get("instance_root_sha256") != expected_root for row in matches):
        return _unknown("instance-root-mismatch")
    starts = [row for row in matches if row.get("event_type") == "startup" and row.get("event_seq") == 0]
    if not starts:
        return _unknown("receipt-chain-invalid")
    # #841 AC4：牆鐘可能在兩次啟動之間往回跳（NTP step、VM 時間同步），單憑
    # ``process_started_at`` 會把已結束程序的舊 receipt 當成最新。呼叫端已知 unit
    # 目前的 MainPID 時，先在屬於該 PID 的 startup receipt 中挑最新者；沒有任何
    # receipt 屬於它才退回牆鐘最新，交給 compare_runtime_state 標 process 不符。
    eligible = starts
    if type(expected_pid) is int and expected_process_started_epoch is not None:
        # #841 審查：PID 被重用、而新程序尚未寫 receipt 時，同 PID 的舊 receipt
        # 會被挑中並判成 match。呼叫端給了 live 程序由 /proc 推得的啟動時間時，
        # 啟動時間差距超過容忍值的同 PID receipt 屬於先前的程序。
        def _reused(row: Mapping[str, object]) -> bool:
            if row.get("pid") != expected_pid:
                return False
            started = _receipt_started_epoch(row)
            return (
                started is None
                or abs(started - expected_process_started_epoch)
                > _PROCESS_START_TOLERANCE_SECONDS
            )

        eligible = [row for row in starts if not _reused(row)]
        if not eligible:
            return _unknown("receipt-missing")
    live_starts = (
        [row for row in eligible if row.get("pid") == expected_pid]
        if type(expected_pid) is int
        else []
    )
    process_start = max(
        live_starts or eligible, key=lambda row: str(row.get("process_started_at"))
    )
    previous_starts = [
        row for row in starts if row.get("process_id") != process_start.get("process_id")
    ]
    previous_process_start = (
        max(previous_starts, key=lambda row: str(row.get("process_started_at")))
        if previous_starts
        else None
    )
    process_id = process_start.get("process_id")
    history = sorted(
        [row for row in matches if row.get("process_id") == process_id],
        key=lambda row: int(row.get("event_seq", -1)),
    )
    previous_id: object = None
    for sequence, row in enumerate(history):
        if row.get("event_seq") != sequence or row.get("previous_receipt_id") != previous_id:
            return _unknown("receipt-chain-invalid")
        if sequence == 0 and row.get("event_type") != "startup":
            return _unknown("receipt-chain-invalid")
        if sequence > 0 and row.get("event_type") != "config-reload":
            return _unknown("receipt-chain-invalid")
        previous_id = row.get("receipt_id")
    latest = history[-1]
    config = latest["config"]
    if not isinstance(config.get("initial_revision"), str) or not isinstance(
        config.get("effective_revision"), str
    ):
        return _unknown("receipt-corrupt")
    return {
        "status": "attested",
        "reason": None,
        "latest": latest,
        "process_start": process_start,
        "initial_config_revision": config["initial_revision"],
        "effective_config_revision": config["effective_revision"],
        "previous_process_start": previous_process_start,
        "receipt_count": len(history),
    }


def inspect_runtime_state(
    state_root: Path,
    *,
    service: str,
    instance: str,
    expected_pid: int | None = None,
    expected_process_started_epoch: float | None = None,
) -> dict[str, object]:
    if service not in _SERVICES or _INSTANCE_RE.fullmatch(instance) is None:
        return _unknown("identity-invalid")
    try:
        _directory, directory_fd = _open_attestation_directory(
            state_root, create=False
        )
    except FileNotFoundError:
        return _unknown("receipt-missing")
    except (OSError, RuntimeAttestationError):
        return _unknown("receipt-corrupt")
    try:
        return _inspect_runtime_state_fd(
            directory_fd,
            service=service,
            instance=instance,
            state_root=state_root,
            expected_pid=expected_pid,
            expected_process_started_epoch=expected_process_started_epoch,
        )
    finally:
        os.close(directory_fd)


def _invocation_revision_component_status(
    latest: object, declared_invocation_revision: str | None
) -> str:
    """回報 ``config_components["invocation_revision"]`` 的獨立狀態。

    與整體 ``config_status`` 分開計算（那個會被 environment_revision 那個
    component 的比對結果影響），這裡只單純反映「宣告值 vs receipt 裡的
    invocation_revision」這一組比較，四種結果：
    - ``match``：兩邊都有值且相等。
    - ``drift``：兩邊都有值但不相等。
    - ``unknown``：receipt 有這個 component，但宣告端算不出來
      （``declared_invocation_revision is None``）。
    - ``not-applicable``：receipt 根本沒有這個 component（例如舊版 receipt
      或本來就不記錄 invocation_revision 的 service）。"""

    observed = (
        latest["config"]["components"].get("invocation_revision")
        if isinstance(latest, Mapping)
        and isinstance(latest.get("config"), Mapping)
        and isinstance(latest["config"].get("components"), Mapping)
        else None
    )
    if not isinstance(observed, str):
        return "not-applicable"
    if declared_invocation_revision is None:
        return "unknown"
    return "match" if declared_invocation_revision == observed else "drift"


def compare_runtime_state(
    state: Mapping[str, object],
    *,
    current_artifact: Mapping[str, object] | None,
    declared_config_revision: str | None,
    declared_config_component: str = "effective_revision",
    declared_invocation_revision: str | None = None,
    expected_pid: int | None = None,
    require_process_match: bool = False,
    in_flight_jobs: int | None = None,
) -> dict[str, object]:
    latest = state.get("latest") if isinstance(state, Mapping) else None
    reason = state.get("reason") if isinstance(state, Mapping) else "receipt-unknown"
    artifact_status = "unknown"
    config_status = "unknown"
    process_status = (
        "unknown"
        if expected_pid is not None or require_process_match
        else "not-checked"
    )
    if not isinstance(latest, Mapping):
        reason = reason or "receipt-unknown"
    else:
        loaded_artifact = latest.get("artifact")
        installed = _safe_artifact(current_artifact or {})
        if not isinstance(loaded_artifact, Mapping):
            reason = "artifact-unknown"
        elif loaded_artifact.get("kind") == "source-override" or installed.get("kind") == "source-override":
            reason = "source-override"
        elif (
            loaded_artifact.get("kind") == "installed-wheel"
            and installed.get("kind") == "installed-wheel"
            and isinstance(loaded_artifact.get("sha256"), str)
            and isinstance(installed.get("sha256"), str)
        ):
            artifact_status = (
                "match" if loaded_artifact["sha256"] == installed["sha256"] else "drift"
            )
            if artifact_status == "drift":
                reason = "artifact-drift"
        if expected_pid is not None:
            process_status = "match" if latest.get("pid") == expected_pid else "unknown"
            if process_status == "unknown":
                reason = "process-identity-mismatch"
        elif require_process_match:
            process_status = "unknown"
            reason = "process-identity-unverified"
        config = latest.get("config")
        components = config.get("components") if isinstance(config, Mapping) else None
        observed_config = (
            components.get(declared_config_component)
            if isinstance(components, Mapping)
            else None
        )
        if observed_config is None and declared_config_component == "effective_revision":
            observed_config = config.get("effective_revision") if isinstance(config, Mapping) else None
        if isinstance(declared_config_revision, str) and isinstance(observed_config, str):
            config_status = "match" if declared_config_revision == observed_config else "drift"
            if config_status == "drift":
                reason = "config-drift"
        elif declared_config_revision is None:
            reason = reason or "declared-config-unknown"
        if (
            declared_config_component == "environment_revision"
            and isinstance(components, Mapping)
            and isinstance(components.get("invocation_revision"), str)
        ):
            observed_invocation_revision = components.get("invocation_revision")
            if declared_invocation_revision is None:
                # 宣告端（cortex service status）算不出 declared_invocation_revision
                # （例如 ExecStart 形狀無法解析、或形狀已知但缺必要環境變數，見
                # manager_declared_invocation_revision）——receipt 有這個
                # component 卻沒有東西可比對，即使 environment_revision 剛好
                # match 也不能宣稱 config match，維持 unknown、不臆測。
                if config_status == "match":
                    config_status = "unknown"
                    reason = "invocation-declaration-unknown"
            elif declared_invocation_revision != observed_invocation_revision:
                # 宣告端與 receipt 都有值但不相等：daemon 實際收到的 argv 與
                # 目前 ExecStart／環境變數反推出的宣告值不一致，這本身就是一種
                # config drift，即使 environment_revision 那個 component 剛好
                # match 也要整體回報 drift，不能被 environment 這一半蓋過去。
                config_status = "drift"
                reason = "invocation-drift"
    if artifact_status == "drift" or config_status == "drift":
        status = "drift"
    elif (
        artifact_status == "match"
        and config_status == "match"
        and process_status in {"match", "not-checked"}
    ):
        status = "match"
    else:
        status = "unknown"

    if in_flight_jobs is None and isinstance(latest, Mapping):
        trust_root = latest.get("trust_root")
        candidate = trust_root.get("in_flight_jobs") if isinstance(trust_root, Mapping) else None
        if type(candidate) is int:
            in_flight_jobs = candidate
    if type(in_flight_jobs) is not int or in_flight_jobs < 0:
        transition_disposition = "unknown-in-flight-state"
    elif in_flight_jobs > 0:
        transition_disposition = "blocked-in-flight"
    else:
        transition_disposition = "clear-not-authorized"
    return {
        "status": status,
        "reason": reason,
        "artifact_status": artifact_status,
        "config_status": config_status,
        "process_status": process_status,
        "config_components": {
            declared_config_component: config_status,
            "invocation_revision": _invocation_revision_component_status(
                latest, declared_invocation_revision
            ),
        },
        "loaded_artifact_sha256": (
            latest.get("artifact", {}).get("sha256")
            if isinstance(latest, Mapping) and isinstance(latest.get("artifact"), Mapping)
            else None
        ),
        "installed_artifact_sha256": (
            _safe_artifact(current_artifact or {}).get("sha256")
            if current_artifact is not None
            else None
        ),
        "loaded_config_revision": (
            state.get("effective_config_revision") if isinstance(state, Mapping) else None
        ),
        "declared_config_revision": declared_config_revision,
        "transition_disposition": transition_disposition,
        "transition_safe": False,
    }


def runtime_status_report(
    state_root: Path,
    *,
    service: str,
    instance: str,
    declared_config_revision: str | None = None,
    declared_config_component: str = "effective_revision",
    declared_invocation_revision: str | None = None,
    expected_pid: int | None = None,
    expected_process_started_epoch: float | None = None,
    require_process_match: bool = False,
    in_flight_jobs: int | None = None,
    current_artifact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    state = inspect_runtime_state(
        state_root,
        service=service,
        instance=instance,
        expected_pid=expected_pid,
        expected_process_started_epoch=expected_process_started_epoch,
    )
    current = _safe_artifact(current_artifact or artifact_identity())
    comparison = compare_runtime_state(
        state,
        current_artifact=current,
        declared_config_revision=declared_config_revision,
        declared_config_component=declared_config_component,
        declared_invocation_revision=declared_invocation_revision,
        expected_pid=expected_pid,
        require_process_match=require_process_match,
        in_flight_jobs=in_flight_jobs,
    )
    latest = state.get("latest")
    return {
        "status": comparison["status"],
        "reason": comparison["reason"] or state.get("reason"),
        "loaded": latest,
        "installed_artifact": current,
        "comparison": comparison,
        "initial_config_revision": state.get("initial_config_revision"),
        "effective_config_revision": state.get("effective_config_revision"),
        "previous_process_start": state.get("previous_process_start"),
        "trust_root": latest.get("trust_root") if isinstance(latest, Mapping) else {"status": "unknown"},
        "current_artifact": current,
        "installed_artifact": current if current.get("kind") == "installed-wheel" else {
            "kind": "unknown",
            "package": current.get("package"),
            "package_version": "unknown",
            "source_revision": "unknown",
            "sha256": None,
        },
    }


def trust_root_receipt_summary(path: Path | str | None) -> dict[str, object]:
    """讀取既有 install、activation、verification 與 rollback receipt 鏈。"""

    if path is None or not str(path):
        return {"status": "unknown", "reason": "install-receipt-unconfigured"}
    receipt_path = Path(path).expanduser()
    try:
        from .trust_root.install.core import InstallReceipt

        receipt = InstallReceipt.load(receipt_path)
        document = receipt.to_dict()
        verification = document.get("verification_evidence")
        activations = document.get("activation_journal", [])
        rollback = document.get("rollback")
        activation_revision = hashlib.sha256(_canonical_bytes(activations)).hexdigest()
        receipt_digest = getattr(receipt, "_checkpoint_sha256", None)
        if not isinstance(receipt_digest, str) or _SHA256_RE.fullmatch(receipt_digest) is None:
            receipt_digest = None
        rollback_revision = (
            hashlib.sha256(_canonical_bytes(rollback)).hexdigest()
            if isinstance(rollback, Mapping)
            else None
        )
        verified = (
            document.get("qualified") is True
            and isinstance(verification, Mapping)
            and verification.get("result") == "pass"
            and isinstance(verification.get("sha256"), str)
            and isinstance(activations, list)
            and all(
                isinstance(row, Mapping) and row.get("status") == "completed"
                for row in activations
            )
        )
        # 狀態以 installer 寫入的 receipt ``state`` 為準（#841 AC5）：rollback 遇到
        # retained drift／unknown durable state（例如 Manager 事後寫入的 jobs.json）
        # 會停在 ``rollback-blocked``，不得因為已有 ``rollback`` 區塊就寫成
        # ``rolled-back``；rollback 中斷時 receipt 停在 ``rolling-back``，
        # ``qualified`` 尚未清掉、activation journal 已逐筆移除，也不得回頭算成
        # ``verified``。
        state = document.get("state")
        reason: str | None = None
        if state == "rolled-back" and isinstance(rollback, Mapping):
            status = "rolled-back"
        elif state == "rollback-blocked":
            status, reason = "unknown", "install-rollback-blocked"
        elif state == "rolling-back":
            status, reason = "unknown", "install-rollback-incomplete"
        elif state == "applied" and verified:
            status = "verified"
        else:
            status = "unknown"
        summary: dict[str, object] = {
            "status": status,
            "reason": reason,
            "receipt_id": document.get("receipt_id"),
            "plan_sha256": document.get("plan_sha256"),
            "receipt_sha256": receipt_digest,
            "activation_revision": activation_revision,
            "verification_sha256": verification.get("sha256") if isinstance(verification, Mapping) else None,
            "rollback_revision": rollback_revision,
        }
        return _safe_trust_root(summary)
    except Exception:
        return {"status": "unknown", "reason": "install-receipt-invalid"}
