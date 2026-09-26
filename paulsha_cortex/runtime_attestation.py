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
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    """挑選安全的 Cortex 環境宣告，不保留未列入允許清單的值。"""

    return {
        key: value
        for key, value in sorted(environment.items())
        if key.startswith(("PSC_", "PAULSHACLAW_"))
        and not _SECRET_KEY_RE.search(key)
        and isinstance(value, str)
    }


def manager_configuration_snapshot(
    arguments: Mapping[str, object], environment: Mapping[str, str]
) -> tuple[dict[str, object], dict[str, str]]:
    """建立 Manager 有效配置摘要，以及可獨立比對的元件摘要。"""

    safe_args = _safe_config(arguments)
    safe_env = safe_environment_projection(environment)
    env_revision = configuration_revision(safe_env)
    invocation_revision = configuration_revision(safe_args)
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


def _read_small_unit_file(unit_path: str) -> bytes | None:
    descriptor = -1
    try:
        descriptor = os.open(
            unit_path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
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
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unit_exec_start(unit_bytes: bytes | None) -> list[str] | None:
    if unit_bytes is None:
        return None
    try:
        lines = unit_bytes.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line.partition("=")[2].lstrip("-@:+!")
        try:
            arguments = shlex.split(value)
        except ValueError:
            return None
        return arguments or None
    return None


def _declared_service_artifact(
    service: str, *, exec_path: str | None, unit_bytes: bytes | None
) -> dict[str, object]:
    argv = _unit_exec_start(unit_bytes)
    if not argv:
        return _safe_artifact({})
    if service == "monitor":
        if len(argv) < 3 or argv[0] != exec_path or argv[1:3] != ["-m", "paulsha_cortex.monitor"]:
            return _safe_artifact({})
        return artifact_identity_from_python(exec_path)
    if service == "manager":
        if len(argv) < 3 or argv[0] != "/usr/bin/env" or argv[1] != "bash":
            return _safe_artifact({})
        script = Path(argv[2])
        if script.name != "service-manager.sh" or script.parent.name != "scripts":
            return _safe_artifact({})
        package_root = script.parent.parent
        if package_root.name != "paulsha_cortex":
            return _safe_artifact({})
        return artifact_identity_from_package_root(package_root)
    return _safe_artifact({})


def service_declaration_projection(
    units: object, *, instance: str
) -> dict[str, dict[str, object]]:
    """投影 service 宣告欄位，不回傳環境值或檔案內容。"""

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
            }
            continue
        exec_path = row.get("exec_path")
        path_value = exec_path if isinstance(exec_path, str) else None
        unit_path = row.get("path")
        unit_bytes = _read_small_unit_file(unit_path) if isinstance(unit_path, str) else None
        unit_digest = hashlib.sha256(unit_bytes).hexdigest() if unit_bytes is not None else None
        result[service] = {
            "unit": unit_name,
            "status": row.get("status") if isinstance(row.get("status"), str) else "unknown",
            "pid": row.get("pid") if type(row.get("pid")) is int else None,
            "exec_path_sha256": (
                hashlib.sha256(os.fsencode(path_value)).hexdigest()
                if path_value is not None
                else None
            ),
            "disk_unit_sha256": unit_digest,
            "artifact": _declared_service_artifact(
                service,
                exec_path=path_value,
                unit_bytes=unit_bytes,
            ),
            "stale": row.get("stale") if type(row.get("stale")) is bool else None,
        }
    return result


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


def _inspect_runtime_state_fd(
    directory_fd: int, *, service: str, instance: str, state_root: Path
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
    process_start = max(starts, key=lambda row: str(row.get("process_started_at")))
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
    state_root: Path, *, service: str, instance: str
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
        )
    finally:
        os.close(directory_fd)


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
            and declared_invocation_revision is None
            and config_status == "match"
        ):
            config_status = "unknown"
            reason = "invocation-declaration-unknown"
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
            "invocation_revision": (
                "match"
                if declared_invocation_revision is not None
                and isinstance(latest, Mapping)
                and isinstance(latest.get("config"), Mapping)
                and isinstance(latest["config"].get("components"), Mapping)
                and latest["config"]["components"].get("invocation_revision")
                == declared_invocation_revision
                else (
                    "unknown"
                    if isinstance(latest, Mapping)
                    and isinstance(latest.get("config"), Mapping)
                    and isinstance(latest["config"].get("components"), Mapping)
                    and "invocation_revision" in latest["config"]["components"]
                    else "not-applicable"
                )
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
    require_process_match: bool = False,
    in_flight_jobs: int | None = None,
    current_artifact: Mapping[str, object] | None = None,
) -> dict[str, object]:
    state = inspect_runtime_state(state_root, service=service, instance=instance)
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
        status = "rolled-back" if isinstance(rollback, Mapping) else ("verified" if verified else "unknown")
        summary: dict[str, object] = {
            "status": status,
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
