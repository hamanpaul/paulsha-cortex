from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from paulsha_cortex.config import paths

from . import job_workspace
from .._yaml import YAMLError, safe_load
from ..persona import gate
from ..persona.contract import PersonaContract, validate_persona_schema


VERIFICATION_SCHEMA_VERSION = 1
VALID_DOCS_CLASSES = frozenset({"normative", "informational", "trivial", "code"})
SAFE_SLICE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
SAFE_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
SAFE_REMOTE_RE = re.compile(r"[A-Za-z0-9._-]+")
SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV")
PERSONA_CATALOG_PATH = "paulsha_cortex/persona/personas.yaml"
TEMP_WORKTREE_DIRNAME = ".psc-verification-worktrees"

GitRunner = Callable[[list[str]], object]
SubprocessRunner = Callable[..., object]


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
    allowed = {"docs_class", "review_policy", "required_artifacts", "checks", "tests", "full_suite"}
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
    review_policy = value.get("review_policy")
    derived_review_policy = review_policy_for_docs_class(docs_class)
    if review_policy is not None and review_policy != derived_review_policy:
        raise ContractValidationError(
            "verification.review_policy",
            f"review_policy must match docs_class derived value: {derived_review_policy}",
        )
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
        "review_policy": derived_review_policy,
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
    extras = sorted(set(payload) - required)
    if extras:
        raise ValueError(f"verification evidence unexpected key: {extras[0]}")
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
        "details": copy.deepcopy(details),
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


def _default_git_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _sanitize_stream(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _coerce_process_result(result: object) -> dict[str, Any] | None:
    if isinstance(result, str):
        return {"returncode": 0, "stdout": result, "stderr": ""}
    returncode = getattr(result, "returncode", None)
    if not isinstance(returncode, int):
        return None
    return {
        "returncode": returncode,
        "stdout": _sanitize_stream(getattr(result, "stdout", "")),
        "stderr": _sanitize_stream(getattr(result, "stderr", "")),
    }


def _run_git(args: list[str], git_runner: GitRunner | None) -> dict[str, Any]:
    runner = git_runner or _default_git_runner
    try:
        raw = runner(args)
    except Exception as exc:
        return {
            "status": "runner-error",
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "args": list(args),
        }
    result = _coerce_process_result(raw)
    if result is None:
        return {
            "status": "partial-evidence",
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "args": list(args),
        }
    return {
        "status": "ok" if result["returncode"] == 0 else "non-zero",
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "args": list(args),
    }


# ---------------------------------------------------------------------------
# #641：Manager 對 job 工作樹沒有讀取權時的 fail-closed 面
# ---------------------------------------------------------------------------
#
# 三分（trust-root Phase 2b）部署下，job 的工作樹是 `0700 <job 帳號>`，而登記表
# 資產 `repo-worktree` 在 #641 之後**不再**授 Manager 唯讀 ACL——成果交付整條走
# bundle spool（#637），Manager 沒有理由走進 builder 的樹。
#
# 但本模組底下這一組檢查（worktree HEAD == candidate、工作樹乾淨、以及以
# `cwd=<job 樹>` 執行宣告出來的 check／test／full-suite）**確實**在讀那棵樹。
# 它們在三分下會全部拿到 `Permission denied`。正確的落點是 #629 的第三執行身分
# （既非 builder 也非 Manager，在受控 checkout 判讀）——理由不只是權限：以
# `cwd=<builder 完全掌控內容的樹>` 跑 `pytest` 會載入該樹的 `conftest.py`／plugin，
# 等於 builder 在 `cortex-manager` 身分下取得任意程式碼執行（同一段論證見
# `trust_root/registry.py` 的 `gate-ledger` note 與 `launcher._should_run_gates`）。
#
# 在 #629 落地之前，這裡的行為只有一種是對的：**明確 fail-closed，且理由碼指得出
# 下一步**。三條不得走的路，逐條寫在這裡以免日後被「順手修好」：
#
#   1. **不得靜默略過**——略過等於一張 build 卡沒有任何人檢查過工作樹狀態就往下走。
#   2. **不得改讀 bundle 取代這組檢查**——bundle 的內容同樣由 builder 產生，兩邊
#      同源，那道檢查會退化成「builder 說什麼就是什麼」（#637 PR body 已列此結論）。
#   3. **不得讓 builder 自報「我的工作樹是乾淨的」**——違反 #540 的 acceptance chain
#      （model 既不能自證成功、也不能自證失敗）與 #628 收斂好的證據作者歸屬。

#: 讀不到 job 工作樹時的專屬 summary。與泛用的 `candidate-worktree-unreadable`
#: 分開：後者是「那棵樹壞了」，前者是「Manager 本來就不該讀得到，等 #629」。
WORKTREE_READ_BLOCKED_SUMMARY = "candidate-worktree-unreadable-pending-gate-identity"

#: 阻擋原因所指向的票號——evidence 內逐字帶出，operator 不必回頭讀原始碼。
WORKTREE_READ_BLOCKED_ISSUE = "#629"

WORKTREE_READ_BLOCKED_DETAIL = (
    "Manager 讀不到這個 job 的工作樹。trust-root 三分部署下工作樹是 "
    "`0700 <job 帳號>`，而登記表資產 `repo-worktree` 自 #641 起不再授 Manager "
    "唯讀 ACL（成果交付走 commit-spool 的 bundle，見 #637）。本組檢查"
    "（worktree HEAD == candidate、工作樹乾淨、以及以 cwd=<job 樹> 執行宣告的 "
    "check／test／full-suite）必須改由 #629 的第三執行身分在受控 checkout 執行；"
    "在那之前一律 fail-closed。處置：等 #629，或在 `PSC_JOB_RUNNER=direct` "
    "（Manager 與 job 同 UID）下重跑這張卡。**不得**放寬 `repo-worktree` 的權限、"
    "**不得**改以 bundle 內容替代（同源，檢查會退化）、**不得**採信 builder "
    "自報的工作樹狀態（#540／#628）。"
)

#: git／OS 在「走不進那棵樹」時吐出來的字串。逐條比對而不是只看 returncode——
#: 非零 returncode 也可能是「這不是一個 repo」，那是完全不同的處置。
#:
#: 第一條是**實測**來的，不是猜的：以 git 2.43.0 對一個 `0700` 且屬於**別的 uid**
#: 的真實 repo 跑本模組會用到的三種形狀，三者逐字相同、rc 皆為 128——
#:
#:     $ setpriv --reuid=<manager> git -C <job 樹> rev-parse HEAD
#:     $ setpriv --reuid=<manager> git -C <job 樹> status --porcelain --untracked-files=all
#:     $ setpriv --reuid=<manager> git -C <job 樹> merge-base --is-ancestor HEAD HEAD
#:     fatal: cannot change to '<job 樹>': Permission denied
#:
#: 其餘幾條涵蓋非 git 的讀取面（`Path.exists()` 一類的 `PermissionError`、
#: coreutils 的 `cannot access`）與 EPERM 形態。
_WORKTREE_READ_BLOCKED_MARKERS = (
    "permission denied",
    "cannot change to",
    "operation not permitted",
    "cannot access",
)


def worktree_read_blocked(result: Mapping[str, Any]) -> bool:
    """這次對 job 工作樹的讀取是不是「Manager 沒有權限」造成的？

    `_run_git` 的回傳形狀；`status == "ok"` 一律不算（讀到了就是讀到了）。
    """

    if result.get("status") == "ok":
        return False
    stderr = str(result.get("stderr") or "").lower()
    return any(marker in stderr for marker in _WORKTREE_READ_BLOCKED_MARKERS)


def worktree_read_blocked_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    """寫進 verification evidence 的結構化阻擋原因（理由碼指向 #629）。"""

    return {
        "blocked_on": WORKTREE_READ_BLOCKED_ISSUE,
        "reason": WORKTREE_READ_BLOCKED_SUMMARY,
        "detail": WORKTREE_READ_BLOCKED_DETAIL,
        "git": dict(result),
    }


def _load_catalog_from_text(text: str) -> dict[str, PersonaContract]:
    try:
        raw = safe_load(text)
    except YAMLError as exc:
        raise ValueError(f"persona catalog 解析失敗: {exc}") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("roles"), Mapping):
        raise ValueError("persona catalog 格式錯誤（缺 roles）")
    records = raw["roles"]
    validation = validate_persona_schema(records)
    if not validation.ok:
        raise ValueError(f"persona catalog schema 不合法: {validation.errors}")
    catalog: dict[str, PersonaContract] = {}
    for role, rec in records.items():
        if not isinstance(rec, Mapping):
            raise ValueError(f"persona catalog schema 不合法: {role}")
        raw_skills = rec.get("skills", [])
        if raw_skills is None:
            raw_skills = []
        if not isinstance(raw_skills, list) or any(not isinstance(item, str) for item in raw_skills):
            raise ValueError(f"persona catalog schema 不合法: {role}: skills 必須是字串清單")
        catalog[str(role)] = PersonaContract(
            role=str(rec["role"]),
            version=str(rec["version"]),
            summary=str(rec["summary"]),
            allowed_phases=tuple(rec["allowed_phases"]),
            write_paths=tuple(rec["write_paths"]),
            allowed_tools=tuple(rec["allowed_tools"]),
            skills=tuple(raw_skills),
        )
    return catalog


def _sanitized_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _resolve_worktree_cwd(worktree: Path, relative_cwd: str) -> Path:
    resolved = (worktree / relative_cwd).resolve()
    try:
        resolved.relative_to(worktree.resolve())
    except ValueError as exc:
        raise ValueError(f"cwd escapes worktree: {relative_cwd!r}") from exc
    return resolved


def _run_command(
    *,
    argv: list[str],
    cwd: Path,
    timeout_seconds: int | float,
    subprocess_runner: SubprocessRunner | None,
    env: dict[str, str],
) -> dict[str, Any]:
    runner = subprocess_runner or subprocess.run
    try:
        raw = runner(
            argv,
            shell=False,
            cwd=str(cwd),
            timeout=timeout_seconds,
            env=env,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return {
            "status": "missing",
            "argv": list(argv),
            "cwd": str(cwd),
            "timeout_seconds": timeout_seconds,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "argv": list(argv),
            "cwd": str(cwd),
            "timeout_seconds": timeout_seconds,
            "returncode": None,
            "stdout": _sanitize_stream(getattr(exc, "stdout", "")),
            "stderr": _sanitize_stream(getattr(exc, "stderr", "")),
        }
    except Exception as exc:
        return {
            "status": "runner-error",
            "argv": list(argv),
            "cwd": str(cwd),
            "timeout_seconds": timeout_seconds,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
        }
    if isinstance(raw, str):
        return {
            "status": "partial-evidence",
            "argv": list(argv),
            "cwd": str(cwd),
            "timeout_seconds": timeout_seconds,
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    result = _coerce_process_result(raw)
    if result is None:
        return {
            "status": "partial-evidence",
            "argv": list(argv),
            "cwd": str(cwd),
            "timeout_seconds": timeout_seconds,
            "returncode": None,
            "stdout": "",
            "stderr": "",
        }
    status = "passed"
    if result["returncode"] < 0:
        status = "signal"
    elif result["returncode"] != 0:
        status = "non-zero"
    return {
        "status": status,
        "argv": list(argv),
        "cwd": str(cwd),
        "timeout_seconds": timeout_seconds,
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


def _temp_base_worktree_path(*, repo_root: Path, slice_id: str, dispatch_base: str) -> Path:
    return repo_root / TEMP_WORKTREE_DIRNAME / f"{slice_id}-{dispatch_base[:12]}"


def _failure_summary(prefix: str, status: str) -> str:
    if prefix == "full-suite":
        if status == "non-zero":
            return "full-suite-candidate-non-zero"
        if status == "partial-evidence":
            return "full-suite-partial-evidence"
        return f"full-suite-{status}"
    if prefix == "task-test":
        if status == "partial-evidence":
            return "task-test-partial-evidence"
        return f"task-test-{status}"
    if status == "partial-evidence":
        return "command-partial-evidence"
    return f"command-{status}"


def _success_status(docs_class: str) -> str:
    return "verified" if docs_class in {"informational", "trivial"} else "reviewing"


def run_result_verification(
    *,
    slice_row: dict[str, Any],
    job: dict[str, Any],
    repo_root: str | Path | None = None,
    coordinator_root: Path | None = None,
    git_runner: GitRunner | None = None,
    subprocess_runner: SubprocessRunner | None = None,
) -> dict[str, Any]:
    resolved_repo_root = normalize_repo_root(repo_root)
    slice_id = str(slice_row.get("slice_id") or job.get("task") or "unknown")
    dispatch_base = str(slice_row.get("dispatch_base") or "")
    branch = str(job.get("branch") or "")
    worktree = Path(str(job.get("worktree") or resolved_repo_root)).resolve()
    candidate = dispatch_base if SAFE_SHA_RE.fullmatch(dispatch_base) else "0" * 40
    details: dict[str, Any] = {
        "branch": branch,
        "dispatch_base": dispatch_base,
        "candidate": None,
        "worktree": str(worktree),
        "repo_root": str(resolved_repo_root),
        "required_artifacts": [],
        "persona_catalog": None,
        "scope": {"status": "skipped", "changed_paths": [], "violations": []},
        "checks": [],
        "tests": [],
        "full_suite": {
            "status": "skipped",
            "comparison": None,
            "base": None,
            "candidate": None,
            "cleanup": None,
        },
    }

    def _finish(status: str, summary: str) -> dict[str, Any]:
        payload = {
            "schema_version": VERIFICATION_SCHEMA_VERSION,
            "slice_id": slice_id,
            "candidate": candidate,
            "status": status,
            "summary": summary,
            "details": details,
        }
        return write_verification_evidence(payload, coordinator_root=coordinator_root)

    contract_value = None
    verification_meta = slice_row.get("verification")
    if isinstance(verification_meta, dict):
        contract_value = verification_meta.get("contract")
    try:
        contract = validate_verification_contract(
            contract_value,
            repo_root=resolved_repo_root,
            auto_dispatch=False,
        )
    except ContractValidationError as exc:
        details["contract_error"] = exc.as_payload()
        return _finish("needs_human", "verification-contract-invalid")

    # #623：工作區是 per-job clone 時，builder 的 commit 只存在於 clone 自己的
    # object store 裡——底下所有以 `resolved_repo_root` 為根的判讀（candidate、
    # ancestry、required-artifact diff、persona scope diff）都會看不到它。先由
    # Manager 從該 job 的成果 **bundle**（Manager-owned spool，不是 builder 的樹）
    # 單向 fetch 回自己的樹，才有東西可讀。這個 job 沒有 spool 那一格（升級前既存
    # 的工作區、測試裡的假 job 記錄）時是 no-op，既有行為逐字不變。
    if branch:
        try:
            job_workspace.harvest_if_spooled(
                source_repo=resolved_repo_root,
                job=job,
                branch=branch,
                coordinator_root=coordinator_root,
            )
        except job_workspace.WorkspaceError as exc:
            details["candidate_harvest_error"] = str(exc)
            return _finish("needs_human", "candidate-harvest-failed")

    branch_result = _run_git(["-C", str(resolved_repo_root), "rev-parse", branch], git_runner)
    branch_stdout = branch_result["stdout"].strip()
    if branch_result["status"] != "ok" or SAFE_SHA_RE.fullmatch(branch_stdout) is None:
        details["candidate_error"] = branch_result
        return _finish("needs_human", "candidate-unreadable")
    candidate = branch_stdout.lower()
    details["candidate"] = candidate

    # #641：以下每一次對 `worktree` 的存取都是 Manager 伸手進 builder 的樹。三分下
    # 這裡是整個 verification **第一個**碰到那棵樹的點——排在 harvest 之後、任何
    # `cwd=<job 樹>` 的命令執行之前，因此讀不到時不會有任何 builder 掌控的程式碼被
    # 以 Manager 身分執行。完整推導見本檔 `WORKTREE_READ_BLOCKED_DETAIL`。
    worktree_head = _run_git(["-C", str(worktree), "rev-parse", "HEAD"], git_runner)
    worktree_head_stdout = worktree_head["stdout"].strip()
    if worktree_head["status"] != "ok" or SAFE_SHA_RE.fullmatch(worktree_head_stdout) is None:
        details["candidate_worktree_error"] = worktree_head
        if worktree_read_blocked(worktree_head):
            details["candidate_worktree_blocked"] = worktree_read_blocked_payload(worktree_head)
            return _finish("needs_human", WORKTREE_READ_BLOCKED_SUMMARY)
        return _finish("needs_human", "candidate-worktree-unreadable")
    details["candidate_worktree_head"] = worktree_head_stdout.lower()
    if worktree_head_stdout.lower() != candidate:
        return _finish("needs_human", "candidate-worktree-mismatch")
    worktree_status = _run_git(
        ["-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
        git_runner,
    )
    details["candidate_worktree_status"] = {
        "status": worktree_status["status"],
        "stdout": worktree_status["stdout"],
        "stderr": worktree_status["stderr"],
    }
    if worktree_status["status"] != "ok":
        if worktree_read_blocked(worktree_status):
            details["candidate_worktree_blocked"] = worktree_read_blocked_payload(worktree_status)
            return _finish("needs_human", WORKTREE_READ_BLOCKED_SUMMARY)
        return _finish("needs_human", "candidate-worktree-status-error")
    if worktree_status["stdout"].strip():
        return _finish("needs_human", "candidate-worktree-dirty")
    if candidate == dispatch_base.lower():
        return _finish("needs_human", "candidate-not-advanced")

    ancestry = _run_git(
        ["-C", str(resolved_repo_root), "merge-base", "--is-ancestor", dispatch_base, candidate],
        git_runner,
    )
    if ancestry["status"] == "non-zero" and ancestry["returncode"] == 1:
        details["ancestry"] = ancestry
        return _finish("needs_human", "candidate-not-descendant")
    if ancestry["status"] != "ok":
        details["ancestry"] = ancestry
        return _finish("needs_human", "candidate-ancestry-error")
    details["ancestry"] = ancestry

    artifact_rows: list[dict[str, Any]] = []
    for artifact in contract["required_artifacts"]:
        artifact_path = worktree / artifact["path"]
        artifact_result = {
            "path": artifact["path"],
            "must_change": artifact["must_change"],
            "exists": artifact_path.exists(),
            "changed": None,
        }
        details["required_artifacts"].append(artifact_result)
        artifact_rows.append(artifact_result)
        if not artifact_result["exists"]:
            artifact_result["status"] = "missing"
            return _finish("needs_human", "required-artifact-missing")

    artifact_diff = _run_git(
        [
            "-C",
            str(resolved_repo_root),
            "-c",
            "core.quotepath=false",
            "diff",
            "--name-only",
            f"{dispatch_base}..{candidate}",
        ],
        git_runner,
    )
    details["required_artifact_diff"] = artifact_diff
    if artifact_diff["status"] != "ok":
        return _finish("needs_human", "required-artifact-diff-error")
    changed_two_dot = set(line for line in artifact_diff["stdout"].splitlines() if line.strip())
    for artifact_result in artifact_rows:
        artifact_result["changed"] = artifact_result["path"] in changed_two_dot
        if artifact_result["must_change"] and not artifact_result["changed"]:
            artifact_result["status"] = "unchanged"
            return _finish("needs_human", "required-artifact-unchanged")
        artifact_result["status"] = "passed"

    catalog_probe = _run_git(
        ["-C", str(resolved_repo_root), "cat-file", "-e", f"{dispatch_base}:{PERSONA_CATALOG_PATH}"],
        git_runner,
    )
    if catalog_probe["status"] == "ok":
        # repo-local override 宣告存在（#295/#291 R2）：pin 在 dispatch_base commit 讀取，
        # 壞損時 fail-closed，不靜默回退 packaged catalog（R3/D3）。
        catalog_blob = _run_git(
            ["-C", str(resolved_repo_root), "show", f"{dispatch_base}:{PERSONA_CATALOG_PATH}"],
            git_runner,
        )
        if catalog_blob["status"] != "ok":
            details["persona_catalog"] = {
                "path": PERSONA_CATALOG_PATH,
                "commit": dispatch_base,
                "error": catalog_blob,
            }
            return _finish("needs_human", "persona-catalog-unreadable")
        try:
            catalog = _load_catalog_from_text(catalog_blob["stdout"])
        except ValueError as exc:
            details["persona_catalog"] = {
                "path": PERSONA_CATALOG_PATH,
                "commit": dispatch_base,
                "error": str(exc),
            }
            return _finish("needs_human", "persona-catalog-invalid")
        details["persona_catalog"] = {
            "path": PERSONA_CATALOG_PATH,
            "commit": dispatch_base,
            "hash": sha256_bytes(catalog_blob["stdout"].encode("utf-8")),
            "source": "repo-local",
        }
    else:
        # 未宣告 override：canonical 來源改為 cortex 套件內建 catalog（#295 R1/D1），
        # persona 定義是 cortex 的產品資產，不要求被治理 repo 的 git tree 長出該檔。
        from ..persona.loader import DEFAULT_PERSONAS_PATH

        packaged_path = DEFAULT_PERSONAS_PATH
        try:
            packaged_text = packaged_path.read_text(encoding="utf-8")
        except OSError as exc:
            details["persona_catalog"] = {
                "path": str(packaged_path),
                "commit": None,
                "error": str(exc),
            }
            return _finish("needs_human", "persona-catalog-unreadable")
        try:
            catalog = _load_catalog_from_text(packaged_text)
        except ValueError as exc:
            details["persona_catalog"] = {
                "path": str(packaged_path),
                "commit": None,
                "error": str(exc),
            }
            return _finish("needs_human", "persona-catalog-invalid")
        details["persona_catalog"] = {
            "path": str(packaged_path),
            "commit": None,
            "hash": sha256_bytes(packaged_text.encode("utf-8")),
            "source": "packaged",
        }
    scope_diff = _run_git(
        [
            "-C",
            str(resolved_repo_root),
            "-c",
            "core.quotepath=false",
            "diff",
            "--name-only",
            f"{dispatch_base}...{candidate}",
        ],
        git_runner,
    )
    if scope_diff["status"] != "ok":
        details["scope"] = {"status": scope_diff["status"], "changed_paths": [], "violations": [], "error": scope_diff}
        return _finish("needs_human", "persona-scope-error")
    changed_paths = [line for line in scope_diff["stdout"].splitlines() if line.strip()]
    verdict = gate.build_verdict(role="builder", changed_paths=changed_paths, manifest_ok=True, catalog=catalog)
    details["scope"] = {
        "status": "passed" if verdict["ok"] else "violated",
        "changed_paths": changed_paths,
        "violations": verdict["violations"],
    }
    if not verdict["ok"]:
        return _finish("needs_human", "persona-scope-violation")

    env = _sanitized_env()
    for check in contract["checks"]:
        if check["kind"] == "persona-scope":
            continue
        try:
            check_cwd = _resolve_worktree_cwd(worktree, check["cwd"])
        except ValueError as exc:
            details["checks"].append({"name": check["name"], "status": "invalid-cwd", "error": str(exc)})
            return _finish("needs_human", "verification-contract-invalid")
        command_result = _run_command(
            argv=check["argv"],
            cwd=check_cwd,
            timeout_seconds=check["timeout_seconds"],
            subprocess_runner=subprocess_runner,
            env=env,
        )
        command_result["name"] = check["name"]
        details["checks"].append(command_result)
        if command_result["status"] != "passed":
            return _finish("needs_human", _failure_summary("check", command_result["status"]))

    for test_spec in contract["tests"]:
        try:
            test_cwd = _resolve_worktree_cwd(worktree, test_spec["cwd"])
        except ValueError as exc:
            details["tests"].append({"status": "invalid-cwd", "error": str(exc)})
            return _finish("needs_human", "verification-contract-invalid")
        test_result = _run_command(
            argv=test_spec["argv"],
            cwd=test_cwd,
            timeout_seconds=test_spec["timeout_seconds"],
            subprocess_runner=subprocess_runner,
            env=env,
        )
        details["tests"].append(test_result)
        if test_result["status"] != "passed":
            return _finish("needs_human", _failure_summary("task-test", test_result["status"]))

    base_worktree = _temp_base_worktree_path(
        repo_root=resolved_repo_root,
        slice_id=slice_id,
        dispatch_base=dispatch_base,
    )
    add_result = _run_git(
        ["-C", str(resolved_repo_root), "worktree", "add", "--detach", str(base_worktree), dispatch_base],
        git_runner,
    )
    if add_result["status"] != "ok":
        details["full_suite"] = {
            "status": add_result["status"],
            "comparison": None,
            "base": add_result,
            "candidate": None,
            "cleanup": None,
        }
        return _finish("needs_human", "base-worktree-add-failed")

    pending_full_suite_failure: tuple[str, str] | None = None
    cleanup_failure: tuple[str, str] | None = None
    try:
        try:
            base_cwd = _resolve_worktree_cwd(base_worktree, contract["full_suite"]["cwd"])
            candidate_cwd = _resolve_worktree_cwd(worktree, contract["full_suite"]["cwd"])
        except ValueError as exc:
            details["full_suite"] = {
                "status": "invalid-cwd",
                "comparison": None,
                "base": None,
                "candidate": None,
                "cleanup": None,
                "error": str(exc),
            }
            pending_full_suite_failure = ("needs_human", "verification-contract-invalid")
        else:
            base_result = _run_command(
                argv=contract["full_suite"]["argv"],
                cwd=base_cwd,
                timeout_seconds=contract["full_suite"]["timeout_seconds"],
                subprocess_runner=subprocess_runner,
                env=env,
            )
            candidate_result = _run_command(
                argv=contract["full_suite"]["argv"],
                cwd=candidate_cwd,
                timeout_seconds=contract["full_suite"]["timeout_seconds"],
                subprocess_runner=subprocess_runner,
                env=env,
            )
            comparison = "matched"
            status = "passed"
            summary = None
            if base_result["status"] == "non-zero" and candidate_result["status"] == "passed":
                comparison = "improved"
            elif base_result["status"] not in {"passed", "non-zero"}:
                status = base_result["status"]
                summary = f"base-full-suite-{base_result['status']}"
                comparison = "unresolved"
            elif candidate_result["status"] != "passed":
                status = candidate_result["status"]
                summary = _failure_summary("full-suite", candidate_result["status"])
                if base_result["status"] == "non-zero" and candidate_result["status"] == "non-zero":
                    summary = "full-suite-both-non-zero"
                    comparison = "unresolved"
            details["full_suite"] = {
                "status": status,
                "comparison": comparison,
                "base": base_result,
                "candidate": candidate_result,
                "cleanup": None,
            }
            if summary is not None:
                pending_full_suite_failure = ("needs_human", summary)
    finally:
        cleanup = _run_git(
            ["-C", str(resolved_repo_root), "worktree", "remove", "--force", str(base_worktree)],
            git_runner,
        )
        details["full_suite"]["cleanup"] = cleanup
        if details["full_suite"].get("status") == "passed" and cleanup["status"] != "ok":
            details["full_suite"]["status"] = cleanup["status"]
            cleanup_failure = ("needs_human", "base-worktree-cleanup-failed")

    if cleanup_failure is not None:
        return _finish(*cleanup_failure)
    if pending_full_suite_failure is not None:
        return _finish(*pending_full_suite_failure)

    worktree_status_after = _run_git(
        ["-C", str(worktree), "status", "--porcelain", "--untracked-files=all"],
        git_runner,
    )
    details["candidate_worktree_status_after"] = {
        "status": worktree_status_after["status"],
        "stdout": worktree_status_after["stdout"],
        "stderr": worktree_status_after["stderr"],
    }
    if worktree_status_after["status"] != "ok":
        # #641：與前一組同一條規則——讀不到就指名 #629，不與「那棵樹壞了」混為一談。
        if worktree_read_blocked(worktree_status_after):
            details["candidate_worktree_blocked"] = worktree_read_blocked_payload(
                worktree_status_after
            )
            return _finish("needs_human", WORKTREE_READ_BLOCKED_SUMMARY)
        return _finish("needs_human", "candidate-worktree-status-error")
    if worktree_status_after["stdout"].strip():
        return _finish("needs_human", "candidate-worktree-dirty-after-verification")
    final_worktree_head = _run_git(["-C", str(worktree), "rev-parse", "HEAD"], git_runner)
    details["candidate_worktree_head_after"] = final_worktree_head["stdout"].strip().lower()
    if final_worktree_head["status"] != "ok" or final_worktree_head["stdout"].strip().lower() != candidate:
        details["candidate_worktree_head_after_error"] = final_worktree_head
        if worktree_read_blocked(final_worktree_head):
            details["candidate_worktree_blocked"] = worktree_read_blocked_payload(
                final_worktree_head
            )
            return _finish("needs_human", WORKTREE_READ_BLOCKED_SUMMARY)
        return _finish("needs_human", "candidate-worktree-moved-after-verification")

    final_branch = _run_git(["-C", str(resolved_repo_root), "rev-parse", branch], git_runner)
    if final_branch["status"] != "ok" or final_branch["stdout"].strip().lower() != candidate:
        details["final_branch"] = final_branch
        return _finish("needs_human", "candidate-ref-diverged")

    return _finish(_success_status(contract["docs_class"]), "verification-succeeded")
