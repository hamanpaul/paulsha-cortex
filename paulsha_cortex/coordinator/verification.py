from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from paulsha_cortex.config import paths


VERIFICATION_SCHEMA_VERSION = 1
VALID_DOCS_CLASSES = frozenset({"normative", "informational", "trivial", "code"})
SAFE_SLICE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
SAFE_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
SAFE_REMOTE_RE = re.compile(r"[A-Za-z0-9._-]+")


class ContractValidationError(ValueError):
    def __init__(self, field: str, message: str, *, code: str = "invalid-frontmatter") -> None:
        self.field = field
        self.code = code
        self.detail = message
        super().__init__(message)

    def as_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "field": self.field,
            "message": self.detail,
        }


class AtomicWriteConflictError(FileExistsError):
    """Raised when a no-clobber atomic write loses a create race."""


def sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def canonical_json_hash(payload: Any) -> str:
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def review_policy_for_docs_class(docs_class: str) -> str:
    if docs_class in {"normative", "code"}:
        return "required"
    if docs_class in {"informational", "trivial"}:
        return "not-required"
    raise ContractValidationError("verification.docs_class", f"unsupported docs_class: {docs_class!r}")


def normalize_repo_root(candidate: str | Path | None) -> Path:
    if candidate is None:
        return paths.repo_root().resolve()
    return Path(candidate).resolve()


def normalize_repo_relative_path(
    value: object,
    *,
    repo_root: Path,
    field: str,
    allow_dot: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(field, f"{field} must be a non-empty string")
    raw = value.strip()
    joined = (repo_root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        relative = joined.relative_to(repo_root)
    except ValueError as exc:
        raise ContractValidationError(field, f"{field} escapes repo root: {value!r}") from exc
    normalized = relative.as_posix() or "."
    if normalized == "." and not allow_dot:
        raise ContractValidationError(field, f"{field} must not resolve to repo root")
    return normalized


def normalize_argv(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContractValidationError(field, f"{field} must be a non-empty argv list")
    argv: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ContractValidationError(f"{field}[{index}]", "argv items must be non-empty strings")
        argv.append(item)
    if len(argv) >= 2 and argv[0] in {"bash", "sh", "/bin/bash", "/bin/sh"} and argv[1] == "-c":
        raise ContractValidationError(field, "bash -c is not allowed in typed argv commands")
    return argv


def normalize_timeout(value: object, *, field: str) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ContractValidationError(field, f"{field} must be a positive number")
    return value


def normalize_remote_name(value: object) -> str:
    remote = "origin" if value is None else str(value).strip()
    if not remote:
        remote = "origin"
    if SAFE_REMOTE_RE.fullmatch(remote) is None:
        raise ValueError(f"invalid PSC_TARGET_REMOTE: {remote!r}")
    return remote


def normalize_non_empty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(field, f"{field} must be a non-empty string")
    return value.strip()


def normalize_required_artifacts(value: object, *, repo_root: Path) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ContractValidationError("verification.required_artifacts", "required_artifacts must be a list")
    artifacts: list[dict[str, Any]] = []
    for index, entry in enumerate(value):
        field = f"verification.required_artifacts[{index}]"
        if not isinstance(entry, dict):
            raise ContractValidationError(field, "artifact entry must be an object")
        if set(entry) - {"path", "must_change"}:
            extra = sorted(set(entry) - {"path", "must_change"})[0]
            raise ContractValidationError(f"{field}.{extra}", f"unknown artifact key: {extra}")
        must_change = entry.get("must_change", False)
        if not isinstance(must_change, bool):
            raise ContractValidationError(
                f"{field}.must_change",
                "must_change must be a boolean",
            )
        artifacts.append(
            {
                "path": normalize_repo_relative_path(
                    entry.get("path"),
                    repo_root=repo_root,
                    field=f"{field}.path",
                ),
                "must_change": must_change,
            }
        )
    return artifacts


def normalize_command_like(
    value: object,
    *,
    field: str,
    repo_root: Path,
    allow_name: bool,
    allow_baseline: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractValidationError(field, f"{field} must be an object")
    allowed = {"argv", "cwd", "timeout_seconds"}
    if allow_name:
        allowed.add("name")
    if allow_baseline:
        allowed.add("baseline")
    extras = set(value) - allowed
    if extras:
        extra = sorted(extras)[0]
        raise ContractValidationError(f"{field}.{extra}", f"unknown key: {extra}")
    normalized = {
        "argv": normalize_argv(value.get("argv"), field=f"{field}.argv"),
        "cwd": normalize_repo_relative_path(
            value.get("cwd"),
            repo_root=repo_root,
            field=f"{field}.cwd",
            allow_dot=True,
        ),
        "timeout_seconds": normalize_timeout(
            value.get("timeout_seconds"),
            field=f"{field}.timeout_seconds",
        ),
    }
    if allow_name:
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ContractValidationError(f"{field}.name", "command check name must be a non-empty string")
        normalized["name"] = name.strip()
    if allow_baseline:
        baseline = value.get("baseline")
        if baseline != "no-regression":
            raise ContractValidationError(f"{field}.baseline", "full_suite baseline must be 'no-regression'")
        normalized["baseline"] = baseline
    return normalized


def validate_verification_contract(
    value: object,
    *,
    repo_root: Path,
    auto_dispatch: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractValidationError("verification", "verification must be an object")
    allowed = {"docs_class", "required_artifacts", "checks", "tests", "full_suite"}
    extras = set(value) - allowed
    if extras:
        extra = sorted(extras)[0]
        raise ContractValidationError(
            f"verification.{extra}",
            f"unknown verification key: {extra}",
        )
    docs_class = value.get("docs_class")
    if not isinstance(docs_class, str) or docs_class not in VALID_DOCS_CLASSES:
        raise ContractValidationError("verification.docs_class", f"unsupported docs_class: {docs_class!r}")
    checks_value = value.get("checks")
    if not isinstance(checks_value, list):
        raise ContractValidationError("verification.checks", "checks must be a list")
    checks: list[dict[str, Any]] = []
    persona_scope_count = 0
    policy_command_count = 0
    for index, entry in enumerate(checks_value):
        field = f"verification.checks[{index}]"
        if not isinstance(entry, dict):
            raise ContractValidationError(field, "check entry must be an object")
        kind = entry.get("kind")
        if kind == "persona-scope":
            if set(entry) != {"kind"}:
                extra = sorted(set(entry) - {"kind"})[0]
                raise ContractValidationError(f"{field}.{extra}", f"unknown persona-scope key: {extra}")
            persona_scope_count += 1
            checks.append({"kind": "persona-scope"})
            continue
        if kind == "command":
            normalized = normalize_command_like(
                {key: entry[key] for key in entry if key != "kind"},
                field=field,
                repo_root=repo_root,
                allow_name=True,
                allow_baseline=False,
            )
            if normalized["name"] == "policy":
                policy_command_count += 1
            checks.append({"kind": "command", **normalized})
            continue
        raise ContractValidationError(f"{field}.kind", f"unsupported check kind: {kind!r}")
    tests_value = value.get("tests")
    if not isinstance(tests_value, list):
        raise ContractValidationError("verification.tests", "tests must be a list")
    tests = [
        normalize_command_like(
            entry,
            field=f"verification.tests[{index}]",
            repo_root=repo_root,
            allow_name=False,
            allow_baseline=False,
        )
        for index, entry in enumerate(tests_value)
    ]
    full_suite = normalize_command_like(
        value.get("full_suite"),
        field="verification.full_suite",
        repo_root=repo_root,
        allow_name=False,
        allow_baseline=True,
    )
    if auto_dispatch:
        if persona_scope_count != 1:
            raise ContractValidationError(
                "verification.checks",
                "auto dispatch requires exactly one persona-scope check",
            )
        if policy_command_count < 1:
            raise ContractValidationError(
                "verification.checks",
                "auto dispatch requires at least one named policy command check",
            )
    return {
        "docs_class": docs_class,
        "review_policy": review_policy_for_docs_class(docs_class),
        "required_artifacts": normalize_required_artifacts(value.get("required_artifacts"), repo_root=repo_root),
        "checks": checks,
        "tests": tests,
        "full_suite": full_suite,
    }


def evidence_path(
    *,
    slice_id: str,
    candidate: str,
    coordinator_root: Path | None = None,
) -> Path:
    if SAFE_SLICE_ID_RE.fullmatch(slice_id) is None:
        raise ValueError(f"unsafe slice_id for evidence path: {slice_id!r}")
    if SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError(f"unsafe candidate sha for evidence path: {candidate!r}")
    root = (coordinator_root or paths.coordinator_root()).resolve()
    return root / "evidence" / "verification" / f"{slice_id}-{candidate.lower()}.json"


def validate_verification_evidence(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("verification evidence must be an object")
    required = {"schema_version", "slice_id", "candidate", "status", "summary", "details"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"verification evidence missing keys: {', '.join(missing)}")
    if payload.get("schema_version") != VERIFICATION_SCHEMA_VERSION:
        raise ValueError(
            f"verification evidence schema_version must be {VERIFICATION_SCHEMA_VERSION}, "
            f"got {payload.get('schema_version')!r}"
        )
    slice_id = payload.get("slice_id")
    if not isinstance(slice_id, str) or SAFE_SLICE_ID_RE.fullmatch(slice_id) is None:
        raise ValueError(f"invalid verification evidence slice_id: {slice_id!r}")
    candidate = payload.get("candidate")
    if not isinstance(candidate, str) or SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError(f"invalid verification evidence candidate: {candidate!r}")
    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("verification evidence status must be a non-empty string")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary:
        raise ValueError("verification evidence summary must be a non-empty string")
    details = payload.get("details")
    if not isinstance(details, dict):
        raise ValueError("verification evidence details must be an object")
    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "slice_id": slice_id,
        "candidate": candidate.lower(),
        "status": status,
        "summary": summary,
        "details": dict(details),
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temp_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.link(temp_path, target)
        except FileExistsError as exc:
            raise AtomicWriteConflictError(str(target)) from exc
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    temp_path.unlink(missing_ok=True)


def _existing_evidence_result_or_raise(path: Path, content_hash: str) -> dict[str, Any]:
    conflict_reason = "existing evidence unreadable"
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, dict):
        try:
            existing_normalized = validate_verification_evidence(existing)
        except ValueError as exc:
            existing_normalized = None
            conflict_reason = f"invalid schema: {exc}"
        else:
            if canonical_json_hash(existing_normalized) == content_hash:
                return {"path": str(path), "hash": content_hash, "payload": existing_normalized}
            conflict_reason = "content mismatch"
    quarantine_dir = path.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    quarantine_path = quarantine_dir / f"{path.stem}-{uuid4().hex}.json"
    os.replace(path, quarantine_path)
    raise RuntimeError(f"conflicting verification evidence: {path} ({conflict_reason})")


def write_verification_evidence(
    payload: object,
    *,
    coordinator_root: Path | None = None,
) -> dict[str, Any]:
    normalized = validate_verification_evidence(payload)
    path = evidence_path(
        slice_id=normalized["slice_id"],
        candidate=normalized["candidate"],
        coordinator_root=coordinator_root,
    )
    content_hash = canonical_json_hash(normalized)
    if path.exists():
        return _existing_evidence_result_or_raise(path, content_hash)
    try:
        atomic_write_json(path, normalized)
    except AtomicWriteConflictError:
        return _existing_evidence_result_or_raise(path, content_hash)
    return {"path": str(path), "hash": content_hash, "payload": normalized}
