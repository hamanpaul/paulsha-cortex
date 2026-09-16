"""cortex install service——render→copy→daemon-reload→enable，冪等。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Sequence

import yaml

from paulsha_cortex.config import paths
from paulsha_cortex.deploy import hooks as hook_reconcile

_INSTANCE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_SUPPORTED_EXECUTORS = frozenset({"copilot", "claude", "codex"})
_PRESERVE_EXISTING_PATHS = frozenset(
    {"PSC_INSTANCE", "PSC_RUN_ROOT", "PSC_MONITOR_STATE_ROOT"}
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstallServiceResult:
    exit_code: int
    mode: str
    message: str


@dataclass(frozen=True)
class _MigrationInputs:
    existing_project: bool
    project_payload: dict[str, object]
    project_needs_update: bool
    identities_need_update: bool


def _template(name: str) -> str:
    return (resources.files("paulsha_cortex.deploy") / "templates" / name).read_text()


def _service_script_path() -> Path:
    return Path(str(resources.files("paulsha_cortex") / "scripts" / "service-manager.sh"))


def render_units(instance: str, interval: int, repo_root: Path | str | None = None) -> dict[str, str]:
    service = _template("manager.service.tmpl").replace("__INSTANCE__", instance)
    service = service.replace("__SERVICE_SCRIPT__", str(_service_script_path()))
    timer = _template("manager.timer.tmpl").replace("__INSTANCE__", instance)
    timer = re.sub(r"^OnUnitActiveSec=.*$", f"OnUnitActiveSec={interval}", timer, flags=re.M)
    monitor = _template("monitor.service.tmpl").replace("__INSTANCE__", instance)
    monitor = monitor.replace("__PY__", sys.executable)
    service_root = Path(repo_root).resolve() if repo_root is not None else paths.repo_root().resolve()
    service = service.replace("__REPO_ROOT__", str(service_root))
    monitor = monitor.replace("__REPO_ROOT__", str(service_root))
    return {
        f"{instance}-manager.service": service,
        f"{instance}-manager.timer": timer,
        f"{instance}-monitor.service": monitor,
    }


def _systemctl_available() -> bool:
    if shutil.which("systemctl") is None:
        return False
    probe = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True)
    return probe.returncode == 0


def _run_systemctl_install_step(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _systemctl_install_failure(
    *,
    stage: str,
    result: subprocess.CompletedProcess[str],
    unit_dir: Path,
    retry_argv: Sequence[str],
    hook_note: str | None = None,
) -> InstallServiceResult:
    stderr_message = (result.stderr or "systemctl 指令失敗").strip()
    if not stderr_message:
        stderr_message = "未回報錯誤訊息"
    retry_command = " ".join(("systemctl", "--user", *retry_argv))
    exit_code = result.returncode if result.returncode > 0 else 1
    message = (
        f"systemctl {stage} 失敗：{stderr_message}\n"
        f"unit 已落檔於 {unit_dir}，僅 systemd reload/enable 未完成。\n"
        f"unit dir: {unit_dir}\n"
        f"retry: {retry_command}"
    )
    if hook_note:
        # reconcile 發生在本 for loop 之前；即使 systemctl 這一步失敗，
        # hook 檔案／備份的副作用已經產生，必須一併回報給 operator。
        message = f"{message}\n{hook_note}"
    return InstallServiceResult(
        exit_code=exit_code,
        mode="systemd",
        message=message,
    )


def _resolve_git_repo_root(repo_root: Path) -> Path:
    candidate = repo_root.expanduser().resolve()
    probe = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise ValueError(f"{candidate} 不是 git repo")
    return Path(probe.stdout.strip()).resolve()


def _validate_instance(instance: str) -> str:
    if not _INSTANCE_RE.fullmatch(instance):
        raise ValueError(
            f"instance 名稱不合法（僅允許 [a-z0-9-]，不可含路徑分隔）: {instance!r}"
        )
    return instance


def _validate_interval(interval: int) -> int:
    if interval <= 0:
        raise ValueError(f"interval 必須為正整數，實際 {interval!r}")
    return interval


def _write_managed_env(
    env_file: Path,
    managed: dict[str, str],
    *,
    preserve_existing: frozenset[str] = frozenset(),
) -> None:
    """更新 managed keys（PY / PSC_REPO_ROOT），就地保留其餘 operator 手動行與註解。

    每次 install 只覆寫本函式管理的鍵；既有的 PSC_MANAGER_SPECS_DIR、
    PSC_WORKTREE_ROOT、PSC_COORDINATOR_ROOT、PSC_SPECS_ROOT 等未進 managed
    dict 的 operator 設定不受影響（本函式只走 `key in remaining` 分支才會動它）。

    `preserve_existing` 是給「managed dict 裡的鍵，但仍想尊重既有值」用的例外；
    **不要**把任何依 PSC_AGENTS_ROOT／instance 推導出來的路徑放進來（PY /
    PSC_REPO_ROOT / PSC_REPO_IDENTITY / PSC_AGENTS_ROOT / PSC_PROJECT_CONFIG_ROOT /
    PSC_CONTROL_ROOT 皆屬此類）——那種值一旦被舊值鎖住，`cortex install service`
    重裝就永遠修不好一個早期殘留的錯誤路徑（issue #371 的根因；#375 引入
    PSC_CONTROL_ROOT 時複驗明確警告過不得重蹈覆轍）。
    """
    if env_file.is_symlink():
        raise ValueError("runtime bootstrap env 不可為 symlink")
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    remaining = dict(managed)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in remaining:
            if key in preserve_existing:
                out.append(line)
                remaining.pop(key)
            else:
                out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def _read_plain_env(env_file: Path) -> dict[str, str]:
    if env_file.is_symlink():
        raise ValueError("runtime bootstrap env 不可為 symlink")
    if not env_file.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key.strip() != key or key in values:
            raise ValueError(f"runtime bootstrap env 格式錯誤: {env_file}: {raw!r}")
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"runtime bootstrap env quote invalid: {env_file}: {raw!r}")
            value = value[1:-1]
        values[key] = value
    return values


_GIT_ORIGIN_SSH_RE = re.compile(r"^[\w.-]+@(?P<host>[^:/]+):(?P<path>.+?)/?$")
_GIT_ORIGIN_URL_RE = re.compile(
    r"^(?:https?|git|ssh)://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+?)/?$"
)


def _normalize_git_origin(url: str) -> str | None:
    """把 SSH（`git@host:owner/name.git`）與 HTTPS（`https://host/owner/name`）
    正規化成同一個身分字串 `host/path`（小寫、去掉尾綴 `.git`）。

    解不出 host/path 回 None；此函式不 raise。
    """
    candidate = url.strip()
    if not candidate:
        return None
    match = _GIT_ORIGIN_SSH_RE.match(candidate) or _GIT_ORIGIN_URL_RE.match(candidate)
    if not match:
        return None
    host = match.group("host").strip().strip("/")
    path = match.group("path").strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not host or not path:
        return None
    return f"{host}/{path}".lower()


def _resolve_repo_identity(repo_root: Path) -> str:
    """身分真值：git remote origin 正規化成功則回 `git:<host/path>`；
    非 git repo／無 origin／git 指令失敗一律靜默退回 `path:<resolved repo_root>`。

    此函式保證不 raise，讓呼叫端（installer 主流程、doctor probe）
    不必額外包一層 try/except 就能安全呼叫。
    """
    try:
        resolved = repo_root.expanduser().resolve()
    except OSError:
        return f"path:{repo_root}"
    try:
        probe = subprocess.run(
            ["git", "-C", str(resolved), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return f"path:{resolved}"
    if probe.returncode == 0:
        normalized = _normalize_git_origin(probe.stdout.strip())
        if normalized:
            return f"git:{normalized}"
    return f"path:{resolved}"


def _resolve_agents_root(raw_agents_root: str | None) -> Path | None:
    if not raw_agents_root:
        return None
    candidate = Path(raw_agents_root).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate


def _reject_foreign_default_agents_root(
    agents_root: Path | None, bootstrap_root: Path, *, source: str
) -> None:
    """Reject an implicit agents root resolved outside the current HOME."""
    if agents_root is None:
        return
    home_root = bootstrap_root.parent.resolve()
    try:
        agents_root.expanduser().resolve().relative_to(home_root)
    except ValueError as exc:
        raise ValueError(
            f"{source} 的 PSC_AGENTS_ROOT={agents_root} 不在目前 "
            f"HOME={home_root} 底下；如為合法自訂路徑請使用 --agents-root 明確指定"
        ) from exc


def _load_project_config_payload(config_path: Path) -> dict[str, object] | None:
    """Return a validated project config, or ``None`` for a missing/syntax-invalid file.

    A loader failure is not the same as an invalid project document.  Preserve
    that distinction so operators can see the underlying environment/config
    error instead of being told to move a potentially valid file.
    """
    if not config_path.is_file():
        return None
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"project config 無法讀取：{config_path}") from exc
    except (UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        from paulsha_cortex.monitor.config import load_config

        load_config(config_path=config_path)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"project config 驗證失敗：{config_path}；原因：{exc}"
        ) from exc
    return dict(payload)


def _workspace_index_for_repo(
    payload: dict[str, object], repo_root: Path
) -> int | None:
    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, list):
        return None
    resolved_repo = repo_root.expanduser().resolve()
    for index, row in enumerate(workspaces):
        if not isinstance(row, dict) or not row.get("path"):
            continue
        try:
            if Path(str(row["path"])).expanduser().resolve() == resolved_repo:
                return index
        except OSError:
            continue
    return None


def _backup_file(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    mode = path.stat().st_mode & 0o7777
    backup = path.with_name(f"{path.name}.bak-{timestamp}")
    suffix = 1
    fd = -1
    try:
        while True:
            try:
                fd = os.open(
                    backup,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    mode,
                )
                os.fchmod(fd, mode)
                break
            except FileExistsError:
                backup = path.with_name(f"{path.name}.bak-{timestamp}-{suffix}")
                suffix += 1
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(path.read_bytes())
    except Exception:
        if fd >= 0:
            os.close(fd)
        backup.unlink(missing_ok=True)
        raise
    return backup


def _instance_env_file(runtime_dir: Path, instance: str) -> Path:
    return runtime_dir / f"{instance}.env"


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o600
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.restore-",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)


def _prepare_migration_inputs(*, config_root: Path, repo_root: Path) -> _MigrationInputs:
    project_config = config_root / "project-cortex.yaml"
    identities = config_root / "model-identities.yaml"

    identity_exists = identities.is_file() or identities.is_symlink()
    identity_error: Exception | None = None
    if identity_exists:
        try:
            from paulsha_cortex.coordinator.model_identities import load_model_identities

            load_model_identities(config_root, use_packaged_default=False)
        except (OSError, ValueError) as exc:
            identity_error = exc
    elif identities.exists():
        raise ValueError(f"model-identities.yaml 不是一般檔案，拒絕覆寫：{identities}")
    if identity_error is not None:
        raise ValueError(
            f"既有 model-identities.yaml 無法載入，拒絕覆寫：{identities}"
        ) from identity_error

    existing_project = project_config.is_file() or project_config.is_symlink()
    project_payload = _load_project_config_payload(project_config)
    if project_payload is None:
        if os.path.lexists(project_config):
            if project_config.is_file():
                raise ValueError(
                    f"既有 project config 無法載入，拒絕覆寫：{project_config}；"
                    "請修復或移走該檔案後再重試"
                )
            raise ValueError(
                f"project config 不是一般檔案，拒絕覆寫：{project_config}；"
                "請修復或移走該檔案後再重試"
            )
        project_payload = {
            "workspaces": [
                {
                    "name": repo_root.name,
                    "path": str(repo_root),
                    "exact_project": True,
                }
            ]
        }
        project_needs_update = True
    else:
        workspaces = project_payload["workspaces"]
        if not isinstance(workspaces, list):
            raise ValueError(f"project config workspaces 格式錯誤：{project_config}")
        target_index = _workspace_index_for_repo(project_payload, repo_root)
        if target_index is None:
            workspaces = [dict(row) for row in workspaces]
            workspaces.append(
                {
                    "name": repo_root.name,
                    "path": str(repo_root),
                    "exact_project": True,
                }
            )
            project_payload["workspaces"] = workspaces
            project_needs_update = True
        else:
            # A path match is an operator-owned entry.  Keep the complete
            # validated document byte-for-byte; in particular, do not promote
            # an existing ``exact_project: false`` entry during install.
            project_needs_update = False

    if project_needs_update and project_config.is_symlink():
        raise ValueError(
            f"既有 project config 為 symlink，拒絕 append/replace：{project_config}；"
            "請改為一般檔案後再重試"
        )

    return _MigrationInputs(
        existing_project=existing_project,
        project_payload=project_payload,
        project_needs_update=project_needs_update,
        identities_need_update=not identity_exists,
    )


def _migrate_instance_config(
    *,
    env_file: Path,
    existing: dict[str, str],
    config_root: Path,
    repo_root: Path,
    managed_env: dict[str, str],
) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    # Validate fail-loud inputs before creating a persistent lock path.  The
    # locked path repeats this check after acquiring the lock to close the
    # race between this preflight and the mutation.
    _prepare_migration_inputs(config_root=config_root, repo_root=repo_root)
    lock_path = config_root / ".cortex-migration.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    lock_held = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_held = True
        _migrate_instance_config_locked(
            env_file=env_file,
            existing=existing,
            config_root=config_root,
            repo_root=repo_root,
            managed_env=managed_env,
        )
    finally:
        try:
            if lock_held:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _migrate_instance_config_locked(
    *,
    env_file: Path,
    existing: dict[str, str],
    config_root: Path,
    repo_root: Path,
    managed_env: dict[str, str],
) -> None:
    """Adopt a legacy instance with a validated, rollback-safe config bundle."""
    project_config = config_root / "project-cortex.yaml"
    identities = config_root / "model-identities.yaml"
    migration_inputs = _prepare_migration_inputs(
        config_root=config_root,
        repo_root=repo_root,
    )
    existing_project = migration_inputs.existing_project
    project_payload = migration_inputs.project_payload
    project_needs_update = migration_inputs.project_needs_update
    identities_need_update = migration_inputs.identities_need_update

    current_root = existing.get("PSC_PROJECT_CONFIG_ROOT", "").strip()
    root_needs_update = (
        not current_root
        or Path(current_root).expanduser().resolve() != config_root.resolve()
    )
    if not (project_needs_update or identities_need_update or root_needs_update):
        _write_managed_env(
            env_file,
            managed_env,
            preserve_existing=_PRESERVE_EXISTING_PATHS,
        )
        return

    project_text = yaml.safe_dump(project_payload, sort_keys=False)
    identities_text = "schema_version: 3\nidentities: []\n"
    previous = {
        env_file: env_file.read_bytes() if env_file.is_file() else None,
        project_config: project_config.read_bytes() if project_config.is_file() else None,
        identities: identities.read_bytes() if identities.is_file() else None,
    }
    backup_paths: dict[Path, Path] = {}
    staging = Path(tempfile.mkdtemp(prefix=".cortex-migration-", dir=config_root))
    try:
        if project_needs_update:
            staged_project = staging / "project-cortex.yaml"
            staged_project.write_text(project_text, encoding="utf-8")
            from paulsha_cortex.monitor.config import load_config

            load_config(config_path=staged_project)
        if identities_need_update:
            staged_identities = staging / "model-identities.yaml"
            staged_identities.write_text(identities_text, encoding="utf-8")
            from paulsha_cortex.coordinator.model_identities import load_model_identities

            load_model_identities(staging, use_packaged_default=False)
        if project_needs_update:
            if existing_project:
                backup_paths[project_config] = _backup_file(project_config)
                shutil.copymode(project_config, staged_project)
            os.replace(staged_project, project_config)
        if identities_need_update:
            os.replace(staged_identities, identities)
        _write_managed_env(
            env_file,
            managed_env,
            preserve_existing=_PRESERVE_EXISTING_PATHS,
        )
    except Exception:
        restore_errors: dict[Path, Exception] = {}
        restore_results: dict[Path, str] = {}
        for path, previous_content in previous.items():
            try:
                _restore_file(path, previous_content)
            except Exception as exc:
                restore_errors[path] = exc
                restore_results[path] = f"失敗:{exc}"
            else:
                restore_results[path] = "成功"
        if restore_errors:
            risky_paths = []
            for path, previous_content in previous.items():
                backup = backup_paths.get(path)
                if backup is not None:
                    provenance = f"有備份:{backup.resolve()}"
                elif previous_content is None:
                    provenance = "遷移前不存在，rollback 僅移除"
                else:
                    provenance = (
                        "無備份；pre-migration 內容只存在於記憶體中的 previous"
                    )
                risky_paths.append(
                    f"{path.resolve()}={provenance}；restore={restore_results[path]}"
                )
            has_unbacked_paths = any(path not in backup_paths for path in previous)
            if has_unbacked_paths:
                prefix = "migration rollback 失敗，未取得備份的檔案與既有備份逐一列出："
            else:
                prefix = "migration rollback 失敗，各檔案備份逐一列出："
            message = f"{prefix}{', '.join(risky_paths)}"
            first_restore_error = next(iter(restore_errors.values()))
            raise ValueError(message) from first_restore_error
        for backup in backup_paths.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "migration backup cleanup failed path=%s: %s",
                    backup,
                    exc,
                )
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def install_service_result(
    instance: str,
    interval: int,
    repo_root: Path,
    *,
    rebind: bool = False,
    agents_root: Path | str | None = None,
) -> InstallServiceResult:
    home = Path.home()
    bootstrap_root = home / ".agents"
    runtime_dir = bootstrap_root / "core" / "runtime"
    env_file = runtime_dir / f"{instance}-manager.env"
    existing = _read_plain_env(env_file)
    instance_env = _read_plain_env(_instance_env_file(runtime_dir, instance))
    explicit_agents_root = (
        _resolve_agents_root(str(agents_root)) if agents_root is not None else None
    )
    if agents_root is not None and explicit_agents_root is None:
        raise ValueError("--agents-root 必須為絕對路徑")
    new_identity = _resolve_repo_identity(repo_root)
    existing_identity = existing.get("PSC_REPO_IDENTITY", "").strip()
    if existing_identity and existing_identity != new_identity and not rebind:
        # 比對基準是身分真值（git remote origin 正規化，或非 git repo 時的路徑指紋），
        # 不是呼叫者——這是 #366 對 #198 的修正：舊守衛比對「既有值 vs 呼叫者」，
        # 一旦既有值腐化過一次，同一呼叫者之後即可永久繞過。
        # 缺戳記（existing_identity 為空字串，涵蓋 env 不存在／#366 前的舊安裝）
        # 一律短路放行並補寫戳記，屬遷移路徑，不得 fail-closed。
        raise ValueError(
            f"既有 runtime env（{env_file}）記錄的 repo 身分為 {existing_identity}，"
            f"與目前 --repo-root 解析出的身分 {new_identity} 不一致，"
            "如為合法搬遷請加上 --rebind 明確放行；否則請確認 --repo-root 是否誤指向其他 repo。"
        )
    persisted_executor = existing.get("PSC_MANAGER_EXECUTOR", "").strip()
    if persisted_executor and persisted_executor not in _SUPPORTED_EXECUTORS:
        raise ValueError("既有 PSC_MANAGER_EXECUTOR 必須為 copilot、claude 或 codex")
    instance_executor = instance_env.get("PSC_MANAGER_EXECUTOR", "").strip()
    if instance_executor and instance_executor not in _SUPPORTED_EXECUTORS:
        raise ValueError("既有 instance PSC_MANAGER_EXECUTOR 必須為 copilot、claude 或 codex")
    persisted_agents_root = _resolve_agents_root(existing.get("PSC_AGENTS_ROOT"))
    process_agents_root = _resolve_agents_root(os.environ.get("PSC_AGENTS_ROOT", ""))
    if explicit_agents_root is None:
        _reject_foreign_default_agents_root(
            persisted_agents_root,
            bootstrap_root,
            source=f"既有 runtime env（{env_file}）的 PSC_AGENTS_ROOT",
        )
        _reject_foreign_default_agents_root(
            process_agents_root,
            bootstrap_root,
            source="process 環境 PSC_AGENTS_ROOT",
        )
    selected_agents_root = explicit_agents_root or persisted_agents_root
    if selected_agents_root is None:
        selected_agents_root = process_agents_root
    if selected_agents_root is None:
        selected_agents_root = bootstrap_root
    unit_dir = home / ".config" / "systemd" / "user"
    # Units always bootstrap their EnvironmentFile from %h/.agents. The env
    # file then redirects all mutable/runtime data through PSC_AGENTS_ROOT and
    # the more-specific PSC_* roots.
    for directory in (
        unit_dir,
        runtime_dir,
        selected_agents_root / "specs",
        selected_agents_root / "monitor",
        selected_agents_root / "config" / "paulsha",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for name, content in render_units(instance, interval, repo_root=repo_root).items():
        (unit_dir / name).write_text(content)
    managed_env = {
        "PY": sys.executable,
        "PSC_INSTANCE": instance,
        "PSC_REPO_ROOT": str(repo_root),
        "PSC_REPO_IDENTITY": new_identity,
        "PSC_AGENTS_ROOT": str(selected_agents_root),
        "PSC_RUN_ROOT": str(selected_agents_root / "run" / instance),
        "PSC_MONITOR_STATE_ROOT": str(selected_agents_root / "monitor"),
        "PSC_PROJECT_CONFIG_ROOT": str(selected_agents_root / "config" / "paulsha"),
        # #375：control plane（manager.lock／requests／done／status.json）沒有
        # instance 成分時，兩個 instance 若共用同一個 PSC_AGENTS_ROOT（installer
        # 目前預設就是如此：agents_root 預設為 $HOME/.agents，與 instance 名稱
        # 無關）會搶同一把 manager.lock——第二個 instance 的 daemon 啟動即退出，
        # 靜默 adopt 對方 pid，work item 永遠不會被處理。比照 PSC_RUN_ROOT 的
        # `run/<instance>` 模式讓它天生 per-instance。
        "PSC_CONTROL_ROOT": str(selected_agents_root / "control" / instance),
    }
    executor_override = os.environ.get("PSC_MANAGER_EXECUTOR", "").strip()
    if executor_override:
        if executor_override not in _SUPPORTED_EXECUTORS:
            raise ValueError("PSC_MANAGER_EXECUTOR 必須為 copilot、claude 或 codex")
        managed_env["PSC_MANAGER_EXECUTOR"] = executor_override
    _migrate_instance_config(
        env_file=env_file,
        existing=existing,
        config_root=Path(managed_env["PSC_PROJECT_CONFIG_ROOT"]),
        repo_root=repo_root,
        managed_env=managed_env,
    )
    hook_reconcile_result = hook_reconcile.reconcile_codex_hooks(
        hook_reconcile.default_codex_hooks_path(home)
    )
    hook_note = f"codex hooks reconcile: {hook_reconcile_result.detail}"
    if not _systemctl_available():
        return InstallServiceResult(
            exit_code=0,
            mode="fallback",
            message=(
                f"systemd 不可用：單元已落檔 {unit_dir}，請改用 service-manager.sh 前景模式；"
                "fallback 缺少 `cortex service logs --follow` 即時串流。\n"
                f"{hook_note}"
            ),
        )
    for stage, args in (
        ("daemon-reload", ("daemon-reload",)),
        ("enable monitor service", ("enable", f"{instance}-monitor.service")),
        ("enable manager timer", ("enable", f"{instance}-manager.timer")),
    ):
        result = _run_systemctl_install_step(*args)
        if result.returncode != 0:
            return _systemctl_install_failure(
                stage=stage,
                result=result,
                unit_dir=unit_dir,
                retry_argv=list(args),
                hook_note=hook_note,
            )
    return InstallServiceResult(
        exit_code=0,
        mode="systemd",
        message=(
            f"installed: {instance}-manager.{{service,timer}} + {instance}-monitor.service\n"
            f"{hook_note}"
        ),
    )


def install_service(
    instance: str,
    interval: int,
    repo_root: Path,
    *,
    rebind: bool = False,
    agents_root: Path | str | None = None,
) -> int:
    result = install_service_result(
        instance,
        interval,
        repo_root,
        rebind=rebind,
        agents_root=agents_root,
    )
    print(result.message)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else list(sys.argv[1:])
    if raw_args and raw_args[0] == "trust-root":
        from paulsha_cortex.trust_root.install.cli import main as trust_root_main

        return int(trust_root_main(raw_args[1:]) or 0)
    parser = argparse.ArgumentParser(
        prog="cortex install",
        description=(
            "安裝 Phase 1 systemd --user units，或使用可回滾的 Phase 2 trust-root installer。"
        ),
    )
    sub = parser.add_subparsers(dest="target", required=True)
    svc = sub.add_parser("service", help="安裝 manager service/timer 與 monitor service")
    svc.add_argument("--instance", default="cortex", help="systemd unit 前綴（預設：cortex）")
    svc.add_argument(
        "--interval", type=int, default=300,
        help="deprecated manager timer 的 OnUnitActiveSec 秒數；daemon tick 請用 PSC_MANAGER_INTERVAL_SECONDS",
    )
    svc.add_argument(
        "--repo-root", default=str(Path.cwd()),
        help="被治理的目標 git repo（預設：目前目錄）",
    )
    svc.add_argument(
        "--agents-root",
        default=None,
        help="明確指定 mutable/runtime agents root；可豁免既有 default root 的 HOME 檢查",
    )
    svc.add_argument(
        "--rebind", action="store_true",
        help="既有 instance 記錄的 repo 身分與 --repo-root 不符時，明確放行本次搬遷",
    )
    sub.add_parser(
        "trust-root",
        help="Phase 2: plan/apply/credentials/activate/verify/rollback",
        add_help=False,
    )
    args = parser.parse_args(raw_args)
    try:
        instance = _validate_instance(args.instance)
        interval = _validate_interval(args.interval)
        repo_root = _resolve_git_repo_root(Path(args.repo_root))
        return install_service(
            instance,
            interval,
            repo_root,
            rebind=args.rebind,
            agents_root=args.agents_root,
        )
    except ValueError as exc:
        parser.error(str(exc))
    raise AssertionError("argparse.error must exit")
