"""Hippo public task-memory payload contract 的 Cortex host adapter。

本模組只依賴已發布的 JSON contract，不 import Hippo、不探查 memory root，
也不寫入 Hippo ledger。Manager 呼叫端會提供 payload provider，以及目前
Work Item／WorkflowRun／card／Job 對應的 executor capability。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


TASK_MEMORY_ADAPTER_SCHEMA = "cortex/task-memory-adapter/v1"
TASK_MEMORY_EVENT_SCHEMA = "cortex/task-memory-receipt/v1"
TASK_MEMORY_READ_MODEL_SCHEMA = "cortex/task-memory-read-model/v1"
SUPPORTED_SCHEMA_MAJOR = 1
MAX_INTENT_CHARS = 280
MAX_SUMMARY_CHARS = 240
MAX_PUBLIC_FIELD_CHARS = 800
MAX_CANDIDATES = 3
MAX_RECEIPT_BYTES = 32 * 1024
MAX_SIDECAR_BYTES = 8 * 1024 * 1024
MAX_APPLIED_ARTIFACT_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NOTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SCHEMA_MAJOR_RE = re.compile(r"(?:^|/)v?(\d+)(?:\.\d+)?$")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|password|secret)(\s*[=:]\s*)[^\s,;]+"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.])(?:/[^\s,;]+|[A-Za-z]:\\[^\s,;]+)")
_EVENT_NAMES = frozenset(
    {
        "candidate-selected",
        "offer-emitted",
        "read-attempted",
        "content-returned",
        "context-delivered",
        "read-failed",
        "ineligible",
        "snapshot-ready",
        "applied-with-evidence",
    }
)
_FAILURE_REASONS = frozenset(
    {
        "provider-unavailable",
        "provider-error",
        "provider-timeout",
        "permission-denied",
        "unsupported-schema-major",
        "malformed-payload",
        "task-id-mismatch",
        "mode-mismatch",
        "scope-mismatch",
        "manifest-mismatch",
        "candidate-authorization-invalid",
        "candidate-unavailable",
        "no-authorized-candidates",
        "no-supported-delivery-capability",
        "note-not-in-manifest",
        "content-hash-mismatch",
        "snapshot-unavailable",
        "action-evidence-missing",
    }
)
_PROVIDER_DIAGNOSTIC_CODES = frozenset(
    {
        "permission-denied",
        "timeout",
        "scope-mismatch",
        "hash-mismatch",
        "unsupported-schema",
        "manifest-mismatch",
        "invalid-request",
        "size-limit",
        "provider-error",
    }
)


class TaskMemoryError(ValueError):
    """可機器判讀且有界的 task-memory contract failure。"""

    def __init__(self, reason: str, message: str | None = None) -> None:
        if reason not in _FAILURE_REASONS:
            reason = "malformed-payload"
        self.reason = reason
        super().__init__(message or reason)


class TaskMemoryProviderError(RuntimeError):
    """Provider protocol error carrying only a bounded diagnostic code."""

    def __init__(self, code: str, *, reason: str = "provider-error") -> None:
        self.code = code if code in _PROVIDER_DIAGNOSTIC_CODES else "provider-error"
        self.reason = reason if reason in _FAILURE_REASONS else "provider-error"
        super().__init__(self.code)


@dataclass(frozen=True)
class TaskMemoryCapabilities:
    """目前 task attempt 選定 executor 所宣告的 capability。"""

    inline: bool = False
    snapshot: bool = False
    note_fetch: bool = False

    def __post_init__(self) -> None:
        for name in ("inline", "snapshot", "note_fetch"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"capability {name} must be bool")

    def to_dict(self) -> dict[str, bool]:
        return {
            "inline": self.inline,
            "snapshot": self.snapshot,
            "note_fetch": self.note_fetch,
        }


@dataclass(frozen=True)
class TaskMemoryContext:
    """由 Cortex state 映射而來的精確 task identity 與受限 scope。"""

    task_id: str
    attempt_id: str
    session_id: str | None
    session_proxy: str | None
    model_id: str | None
    repo: str
    work_id: str
    workflow_run_id: str
    job_id: str
    card: str
    executor: str
    tool: str
    project: str
    task_kind: str
    goal: str
    related_files: tuple[str, ...]
    related_errors: tuple[str, ...]
    capabilities: TaskMemoryCapabilities
    read_scope: Mapping[str, str]
    allowed_evidence_sources: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "attempt_id",
            "repo",
            "work_id",
            "workflow_run_id",
            "job_id",
            "card",
            "executor",
            "tool",
            "project",
            "task_kind",
            "goal",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"task memory context {name} must be non-empty")
        if self.session_id is not None and (not isinstance(self.session_id, str) or not self.session_id):
            raise ValueError("session_id must be null or non-empty")
        if self.session_proxy is not None and (
            not isinstance(self.session_proxy, str) or not self.session_proxy
        ):
            raise ValueError("session_proxy must be null or non-empty")
        if self.model_id is not None and (not isinstance(self.model_id, str) or not self.model_id):
            raise ValueError("model_id must be null or non-empty")
        if (self.session_id is None) == (self.session_proxy is None):
            raise ValueError("exactly one of session_id or session_proxy is required")
        safe_goal = _safe_public_text(self.goal, limit=MAX_INTENT_CHARS)
        if not safe_goal:
            raise ValueError("task memory intent must be non-empty after redaction")
        object.__setattr__(self, "goal", safe_goal)
        if self.repo != self.project:
            raise ValueError("task memory project must equal canonical repository")
        if not isinstance(self.capabilities, TaskMemoryCapabilities):
            raise ValueError("task memory capabilities invalid")
        expected_scope = {
            "repo": self.repo,
            "work_id": self.work_id,
            "workflow_run_id": self.workflow_run_id,
            "card": self.card,
        }
        if not isinstance(self.read_scope, Mapping) or dict(self.read_scope) != expected_scope:
            raise ValueError("task memory read scope invalid")
        if not isinstance(self.related_files, tuple) or any(
            not isinstance(item, str) or not _is_repo_relative(item)
            for item in self.related_files
        ):
            raise ValueError("task memory related file must be repo-relative")
        if not isinstance(self.related_errors, tuple):
            raise ValueError("task memory related errors must be a tuple")
        safe_errors = tuple(
            item
            for item in (_safe_public_text(value, limit=MAX_PUBLIC_FIELD_CHARS) for value in self.related_errors)
            if item
        )
        object.__setattr__(self, "related_errors", safe_errors)
        if not isinstance(self.allowed_evidence_sources, tuple):
            raise ValueError("task memory allowed evidence sources must be a tuple")
        safe_sources = tuple(
            dict.fromkeys(
                _safe_public_text(value, limit=MAX_PUBLIC_FIELD_CHARS)
                for value in self.allowed_evidence_sources
            )
        )
        if any(not value for value in safe_sources):
            raise ValueError("task memory allowed evidence source is empty")
        object.__setattr__(self, "allowed_evidence_sources", safe_sources)

    def to_envelope(self, *, mode: str) -> dict[str, Any]:
        """建立 public-contract request，並將 host scope 留作 optional data。"""

        if mode not in {"inline", "snapshot", "note_fetch", "ineligible", "failure"}:
            raise ValueError("unsupported task memory delivery mode")
        delivery: dict[str, Any] = {
            "mode": mode,
            "capabilities": self.capabilities.to_dict(),
            "host_scope": {
                "repo": self.repo,
                "work_id": self.work_id,
                "workflow_run_id": self.workflow_run_id,
                "card": self.card,
                "attempt_id": self.attempt_id,
                "session_id": self.session_id,
                "session_proxy": self.session_proxy,
                "executor": self.executor,
                "model_id": self.model_id,
                "tool": self.tool,
                "project": self.project,
                "task_kind": self.task_kind,
                "read_scope": dict(self.read_scope),
                "allowed_evidence_sources": list(self.allowed_evidence_sources),
                "related_files": list(self.related_files),
                "related_errors": list(self.related_errors),
            },
        }
        return {
            "schema_version": "1",
            "task_id": self.task_id,
            "intent": self.goal,
            "candidates": [],
            "delivery": delivery,
            "evidence": [],
            "producer": {"id": "cortex-host"},
            "adapter": {"id": "cortex-task-memory", "version": "1"},
            "project": self.project,
        }


def task_memory_context_from_cortex(
    *,
    work_item: Any,
    run: Any,
    step: Any,
    job: Mapping[str, Any],
    capabilities: TaskMemoryCapabilities,
    goal: str,
    related_files: Sequence[str] = (),
    related_errors: Sequence[str] = (),
    allowed_evidence_sources: Sequence[str] = (),
) -> TaskMemoryContext:
    """精確映射 Work Item／WorkflowRun／card／Job 邊界。

    不以 title 或 card name 推導 task identity。穩定 identity 來自正式
    repo/work/run tuple；Job ID 是該次 attempt 的精確 identity。
    """

    repo = _required_string(getattr(run, "repo", None), "run.repo")
    work_id = _required_string(getattr(run, "work_id", None), "run.work_id")
    run_id = _required_string(getattr(run, "run_id", None), "run.run_id")
    card = _required_string(getattr(step, "card", None), "step.card")
    phase = _required_string(getattr(step, "phase", None), "step.phase")
    job_id = _required_string(job.get("job_id"), "job.job_id")
    if (
        getattr(work_item, "repo", None) != repo
        or getattr(work_item, "work_id", None) != work_id
    ):
        raise ValueError("work item identity mismatch")
    item_run = getattr(work_item, "workflow_run_id", None)
    if item_run is not None and item_run != run_id:
        raise ValueError("work item identity mismatch")
    steps = getattr(run, "steps", ())
    if sum(1 for candidate in steps if getattr(candidate, "card", None) == card) != 1:
        raise ValueError("workflow card identity mismatch")
    if not any(candidate is step or candidate == step for candidate in steps):
        raise ValueError("workflow card identity mismatch")
    if (
        job.get("workflow_run_id") != run_id
        or
        job.get("workflow_card") != card
        or job.get("workflow_phase") != phase
        or job.get("job_id") != job_id
    ):
        raise ValueError("job identity mismatch")
    executor = _required_string(job.get("executor"), "job.executor")
    session_id = job.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or not session_id):
        raise ValueError("job.session_id must be null or non-empty")
    session_proxy = None if session_id else f"job:{job_id}"
    task_id = _stable_task_id(repo, work_id, run_id)
    safe_goal = _safe_public_text(goal, limit=MAX_INTENT_CHARS)
    if not safe_goal:
        raise ValueError("task memory intent must be non-empty after redaction")
    normalized_files = tuple(dict.fromkeys(_repo_ref(value) for value in related_files))
    normalized_errors = tuple(
        item
        for item in (_safe_public_text(value, limit=MAX_PUBLIC_FIELD_CHARS) for value in related_errors)
        if item
    )
    evidence_sources = tuple(
        dict.fromkeys(_safe_public_text(value, limit=MAX_PUBLIC_FIELD_CHARS) for value in allowed_evidence_sources)
    )
    if any(not value for value in evidence_sources):
        raise ValueError("allowed evidence sources contain an empty value")
    return TaskMemoryContext(
        task_id=task_id,
        attempt_id=job_id,
        session_id=session_id,
        session_proxy=session_proxy,
        model_id=job.get("model_id"),
        repo=repo,
        work_id=work_id,
        workflow_run_id=run_id,
        job_id=job_id,
        card=card,
        executor=executor,
        tool=_required_string(job.get("tool") or executor, "job.tool"),
        project=repo,
        task_kind=phase,
        goal=safe_goal,
        related_files=normalized_files,
        related_errors=normalized_errors,
        capabilities=capabilities,
        read_scope={
            "repo": repo,
            "work_id": work_id,
            "workflow_run_id": run_id,
            "card": card,
        },
        allowed_evidence_sources=evidence_sources,
    )


@dataclass
class PreparedTaskMemory:
    context: TaskMemoryContext
    status: str
    mode: str
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    inline_context: tuple[dict[str, str], ...] = ()
    snapshot_id: str | None = None
    snapshot_path: Path | None = None
    events: tuple[dict[str, Any], ...] = ()
    returned_note_ids: set[str] = field(default_factory=set)
    context_delivered_note_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class TaskMemoryFetch:
    content: str | None
    events: tuple[dict[str, Any], ...]
    reason: str | None = None


class TaskMemoryAdapter:
    """依 capability 消費 Hippo public payload provider 的 adapter。"""

    def __init__(
        self,
        *,
        provider: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
        note_fetch: Callable[[str, str], str] | None = None,
    ) -> None:
        self.provider = provider
        self.note_fetch = note_fetch

    def prepare(
        self,
        context: TaskMemoryContext,
        *,
        snapshot_root: str | Path | None = None,
    ) -> PreparedTaskMemory:
        mode = self._select_mode(context.capabilities, snapshot_root=snapshot_root)
        if mode is None:
            return self._failure(
                context,
                status="ineligible",
                reason="no-supported-delivery-capability",
                requested_mode="ineligible",
            )
        if self.provider is None:
            return self._failure(
                context,
                status="read-failed",
                reason="provider-unavailable",
                requested_mode=mode,
            )
        request = context.to_envelope(mode=mode)
        try:
            response = self.provider(request)
        except PermissionError as exc:
            return self._failure(
                context,
                status="read-failed",
                reason="permission-denied",
                requested_mode=mode,
                permission_layer=(
                    "provider" if getattr(exc, "provider_code", None) == "permission-denied" else "unknown"
                ),
                provider_code=getattr(exc, "provider_code", None),
            )
        except TimeoutError as exc:
            return self._failure(
                context,
                status="read-failed",
                reason="provider-timeout",
                requested_mode=mode,
                provider_code=getattr(exc, "provider_code", None),
            )
        except TaskMemoryProviderError as exc:
            return self._failure(
                context,
                status="parked" if exc.reason == "unsupported-schema-major" else "read-failed",
                reason=exc.reason,
                requested_mode=mode,
                provider_code=exc.code,
            )
        except Exception:
            return self._failure(
                context,
                status="read-failed",
                reason="provider-error",
                requested_mode=mode,
            )
        try:
            payload = _validate_payload(response, context=context, requested_mode=mode)
        except _UnsupportedSchema:
            return self._failure(
                context,
                status="parked",
                reason="unsupported-schema-major",
                requested_mode=mode,
            )
        except TaskMemoryError as exc:
            return self._failure(
                context,
                status="read-failed",
                reason=exc.reason,
                requested_mode=mode,
            )
        if not payload["candidates"]:
            return self._failure(
                context,
                status="ineligible",
                reason="no-authorized-candidates",
                requested_mode=mode,
            )
        candidate_map = {candidate["note_id"]: candidate for candidate in payload["candidates"]}
        events = tuple(
            event
            for candidate in payload["candidates"]
            for event in (
                _event(context, "candidate-selected", mode=mode, candidate=candidate),
                _event(context, "offer-emitted", mode=mode, candidate=candidate),
            )
        )
        prepared = PreparedTaskMemory(
            context=context,
            status="offered",
            mode=mode,
            payload=payload,
            manifest=payload["delivery"]["manifest"],
            candidates=candidate_map,
            events=events,
        )
        if mode == "inline":
            prepared.inline_context = tuple(
                {
                    "note_id": candidate["note_id"],
                    "content_hash": candidate["content_hash"],
                    "text": candidate.get("excerpt") or candidate["summary"],
                }
                for candidate in payload["candidates"]
            )
        elif mode == "snapshot":
            try:
                prepared.snapshot_id, prepared.snapshot_path = _materialize_snapshot(
                    context=context,
                    manifest=prepared.manifest,
                    candidates=candidate_map,
                    snapshot_root=Path(snapshot_root) if snapshot_root is not None else None,
                )
            except PermissionError:
                return self._failure(
                    context,
                    status="read-failed",
                    reason="permission-denied",
                    requested_mode=mode,
                    permission_layer="unknown",
                )
            except (OSError, ValueError):
                return self._failure(
                    context,
                    status="read-failed",
                    reason="snapshot-unavailable",
                    requested_mode=mode,
                )
            prepared.events += tuple(
                _event(
                    context,
                    "snapshot-ready",
                    mode=mode,
                    candidate=candidate,
                    snapshot_id=prepared.snapshot_id,
                )
                for candidate in payload["candidates"]
            )
        return prepared

    def _select_mode(
        self,
        capabilities: TaskMemoryCapabilities,
        *,
        snapshot_root: str | Path | None,
    ) -> str | None:
        if capabilities.note_fetch and self.note_fetch is not None:
            return "note_fetch"
        if capabilities.snapshot and snapshot_root is not None:
            return "snapshot"
        if capabilities.inline:
            return "inline"
        return None

    def confirm_context_delivered(
        self,
        prepared: PreparedTaskMemory,
        *,
        delivered: bool = True,
        reason: str = "permission-denied",
    ) -> tuple[dict[str, Any], ...]:
        if prepared.mode != "inline" or prepared.status != "offered":
            raise ValueError("context delivery requires an offered inline payload")
        if delivered:
            events = tuple(
                _event(prepared.context, "context-delivered", mode="inline", candidate=candidate)
                for candidate in prepared.candidates.values()
            )
            prepared.context_delivered_note_ids.update(prepared.candidates)
            return events
        return tuple(
            _event(
                prepared.context,
                "read-failed",
                mode="inline",
                candidate=candidate,
                reason=_bounded_reason(reason),
                permission_layer="unknown" if reason == "permission-denied" else None,
            )
            for candidate in prepared.candidates.values()
        )

    def fetch_note(self, prepared: PreparedTaskMemory, note_id: str) -> TaskMemoryFetch:
        candidate = _require_manifest_candidate(prepared, note_id)
        attempted = _event(
            prepared.context,
            "read-attempted",
            mode="note_fetch",
            candidate=candidate,
        )
        if prepared.mode != "note_fetch" or self.note_fetch is None:
            failed = _event(
                prepared.context,
                "read-failed",
                mode=prepared.mode,
                candidate=candidate,
                reason="provider-unavailable",
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason="provider-unavailable")
        try:
            content = self.note_fetch(prepared.context.task_id, note_id)
        except PermissionError as exc:
            failed = _event(
                prepared.context,
                "read-failed",
                mode="note_fetch",
                candidate=candidate,
                reason="permission-denied",
                permission_layer=(
                    "provider" if getattr(exc, "provider_code", None) == "permission-denied" else "unknown"
                ),
                provider_code=getattr(exc, "provider_code", None),
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason="permission-denied")
        except TimeoutError as exc:
            failed = _event(
                prepared.context,
                "read-failed",
                mode="note_fetch",
                candidate=candidate,
                reason="provider-timeout",
                provider_code=getattr(exc, "provider_code", None),
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason="provider-timeout")
        except TaskMemoryProviderError as exc:
            failed = _event(
                prepared.context,
                "read-failed",
                mode="note_fetch",
                candidate=candidate,
                reason=exc.reason,
                provider_code=exc.code,
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason=exc.reason)
        except Exception:
            failed = _event(
                prepared.context,
                "read-failed",
                mode="note_fetch",
                candidate=candidate,
                reason="provider-error",
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason="provider-error")
        if not isinstance(content, str) or _sha256(content.encode("utf-8")) != candidate["content_hash"]:
            failed = _event(
                prepared.context,
                "read-failed",
                mode="note_fetch",
                candidate=candidate,
                reason="content-hash-mismatch",
                provider_code="hash-mismatch",
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason="content-hash-mismatch")
        prepared.returned_note_ids.add(note_id)
        returned = _event(prepared.context, "content-returned", mode="note_fetch", candidate=candidate)
        return TaskMemoryFetch(content=content, events=(attempted, returned))

    def open_snapshot(
        self,
        prepared: PreparedTaskMemory,
        snapshot_id: str,
        note_id: str,
    ) -> TaskMemoryFetch:
        candidate = _require_manifest_candidate(prepared, note_id)
        attempted = _event(
            prepared.context,
            "read-attempted",
            mode="snapshot",
            candidate=candidate,
        )
        path = prepared.snapshot_path
        try:
            if (
                prepared.mode != "snapshot"
                or not isinstance(snapshot_id, str)
                or _SHA256_RE.fullmatch(snapshot_id) is None
                or snapshot_id != prepared.snapshot_id
                or path is None
                or path.is_symlink()
                or not path.is_file()
                or stat.S_IMODE(path.stat().st_mode) & 0o222
            ):
                raise ValueError("snapshot binding invalid")
            snapshot_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                snapshot_stat = os.fstat(snapshot_fd)
                if not stat.S_ISREG(snapshot_stat.st_mode) or snapshot_stat.st_mode & 0o222:
                    raise ValueError("snapshot is not sealed read-only content")
                snapshot_stream = os.fdopen(snapshot_fd, "rb")
                snapshot_fd = -1
                with snapshot_stream:
                    encoded = snapshot_stream.read(MAX_SIDECAR_BYTES + 1)
            finally:
                if snapshot_fd >= 0:
                    os.close(snapshot_fd)
            if len(encoded) > MAX_SIDECAR_BYTES:
                raise ValueError("snapshot exceeds size bound")
            if _sha256(encoded) != snapshot_id:
                raise ValueError("snapshot hash mismatch")
            document = json.loads(encoded.decode("utf-8"))
            if (
                document.get("schema") != "cortex/task-memory-snapshot/v1"
                or document.get("task_id") != prepared.context.task_id
                or document.get("project") != prepared.context.project
                or document.get("manifest_sha256") != prepared.manifest.get("sha256")
                or note_id not in document.get("entries", {})
            ):
                raise ValueError("snapshot binding invalid")
            content = document["entries"][note_id]["content"]
            if _sha256(content.encode("utf-8")) != candidate["content_hash"]:
                raise ValueError("snapshot note hash mismatch")
        except PermissionError:
            failed = _event(
                prepared.context,
                "read-failed",
                mode="snapshot",
                candidate=candidate,
                reason="permission-denied",
                permission_layer="unknown",
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason="permission-denied")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
            failed = _event(
                prepared.context,
                "read-failed",
                mode="snapshot",
                candidate=candidate,
                reason="snapshot-unavailable",
            )
            return TaskMemoryFetch(content=None, events=(attempted, failed), reason="snapshot-unavailable")
        prepared.returned_note_ids.add(note_id)
        returned = _event(prepared.context, "content-returned", mode="snapshot", candidate=candidate)
        return TaskMemoryFetch(content=content, events=(attempted, returned))

    def record_applied(
        self,
        prepared: PreparedTaskMemory,
        note_id: str,
        *,
        evidence_ref: str | None = None,
        evidence_sha256: str | None = None,
    ) -> dict[str, Any]:
        candidate = _require_manifest_candidate(prepared, note_id)
        if (
            note_id not in prepared.returned_note_ids
            and note_id not in prepared.context_delivered_note_ids
        ):
            raise ValueError("applied evidence requires delivered content for this note")
        if (
            evidence_ref is None
            or not _is_repo_relative(evidence_ref)
            or not isinstance(evidence_sha256, str)
            or _SHA256_RE.fullmatch(evidence_sha256) is None
        ):
            raise ValueError("action evidence is required and must be hash-bound")
        return _event(
            prepared.context,
            "applied-with-evidence",
            mode=prepared.mode,
            candidate=candidate,
            evidence={"ref": evidence_ref, "sha256": evidence_sha256},
        )

    @staticmethod
    def _failure(
        context: TaskMemoryContext,
        *,
        status: str,
        reason: str,
        requested_mode: str,
        permission_layer: str | None = None,
        provider_code: str | None = None,
    ) -> PreparedTaskMemory:
        event_name = "ineligible" if status == "ineligible" else "read-failed"
        event = _event(
            context,
            event_name,
            mode=requested_mode,
            reason=reason,
            permission_layer=permission_layer,
            provider_code=provider_code,
        )
        return PreparedTaskMemory(
            context=context,
            status=status,
            mode=requested_mode,
            reason=reason,
            events=(event,),
        )


class _UnsupportedSchema(Exception):
    pass


def _validate_payload(
    value: object,
    *,
    context: TaskMemoryContext,
    requested_mode: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TaskMemoryError("malformed-payload")
    try:
        payload = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise TaskMemoryError("malformed-payload") from exc
    major = _schema_major(payload.get("schema_version"))
    if major is None:
        raise TaskMemoryError("malformed-payload")
    if major != SUPPORTED_SCHEMA_MAJOR:
        raise _UnsupportedSchema
    if payload.get("task_id") != context.task_id:
        raise TaskMemoryError("task-id-mismatch")
    if payload.get("project", context.project) != context.project:
        raise TaskMemoryError("scope-mismatch")
    intent = payload.get("intent")
    if not isinstance(intent, str) or len(intent) > MAX_INTENT_CHARS:
        raise TaskMemoryError("malformed-payload")
    delivery = payload.get("delivery")
    candidates = payload.get("candidates")
    if not isinstance(delivery, Mapping) or not isinstance(candidates, list):
        raise TaskMemoryError("malformed-payload")
    if delivery.get("mode") != requested_mode:
        raise TaskMemoryError("mode-mismatch")
    capabilities = delivery.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise TaskMemoryError("malformed-payload")
    if any(
        key in capabilities and not isinstance(capabilities[key], bool)
        for key in ("inline", "snapshot", "note_fetch")
    ):
        raise TaskMemoryError("malformed-payload")
    if requested_mode in {"inline", "snapshot", "note_fetch"} and not capabilities.get(
        requested_mode, False
    ):
        raise TaskMemoryError("mode-mismatch")
    if len(candidates) > MAX_CANDIDATES:
        raise TaskMemoryError("malformed-payload")
    normalized_candidates: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for raw in candidates:
        if not isinstance(raw, Mapping):
            raise TaskMemoryError("malformed-payload")
        authorization = raw.get("authorization")
        if not isinstance(authorization, Mapping) or authorization.get("status") != "authorized":
            raise TaskMemoryError("candidate-authorization-invalid")
        availability = raw.get("availability")
        if not isinstance(availability, Mapping) or availability.get("status") != "available":
            raise TaskMemoryError("candidate-unavailable")
        note_id = raw.get("note_id", raw.get("ref"))
        if not isinstance(note_id, str) or _NOTE_ID_RE.fullmatch(note_id) is None:
            raise TaskMemoryError("malformed-payload")
        content_hash = raw.get("content_hash")
        content_version = raw.get("content_version")
        if (
            not isinstance(content_hash, str)
            or _SHA256_RE.fullmatch(content_hash) is None
            or not isinstance(content_version, str)
            or not content_version
        ):
            raise TaskMemoryError("manifest-mismatch")
        project = raw.get("project", context.project)
        if project != context.project:
            raise TaskMemoryError("scope-mismatch")
        row = dict(raw)
        ref = raw.get("ref")
        if not isinstance(ref, str) or not ref or len(ref) > MAX_PUBLIC_FIELD_CHARS:
            raise TaskMemoryError("malformed-payload")
        row["ref"] = _safe_public_text(ref, limit=MAX_PUBLIC_FIELD_CHARS)
        row["note_id"] = note_id
        row["content_hash"] = content_hash
        row["content_version"] = content_version
        row["summary"] = _safe_public_text(raw.get("summary"), limit=MAX_SUMMARY_CHARS)
        if not row["summary"]:
            raise TaskMemoryError("malformed-payload")
        if "excerpt" in raw:
            row["excerpt"] = _safe_public_text(raw.get("excerpt"), limit=MAX_PUBLIC_FIELD_CHARS)
        applicability = raw.get("applicability", ())
        if not isinstance(applicability, Sequence) or isinstance(applicability, (str, bytes)):
            raise TaskMemoryError("malformed-payload")
        row["applicability"] = [
            _safe_public_text(item, limit=MAX_PUBLIC_FIELD_CHARS)
            for item in applicability
            if isinstance(item, str) and item
        ]
        row["relevance_reason"] = _safe_public_text(
            raw.get("relevance_reason", ""), limit=MAX_PUBLIC_FIELD_CHARS
        )
        source_time = raw.get("source_time")
        if not isinstance(source_time, str) or not source_time:
            raise TaskMemoryError("manifest-mismatch")
        row["source_time"] = source_time
        previous = seen.get(note_id)
        if previous is not None:
            if previous != content_hash:
                raise TaskMemoryError("manifest-mismatch")
            continue
        seen[note_id] = content_hash
        normalized_candidates.append(row)
    manifest = delivery.get("manifest")
    if not isinstance(manifest, Mapping):
        raise TaskMemoryError("manifest-mismatch")
    _validate_manifest(manifest, context=context, candidates=normalized_candidates)
    delivery_copy = dict(delivery)
    delivery_copy["manifest"] = dict(manifest)
    payload["delivery"] = delivery_copy
    payload["candidates"] = normalized_candidates
    return payload


def _validate_manifest(
    manifest: Mapping[str, Any],
    *,
    context: TaskMemoryContext,
    candidates: Sequence[Mapping[str, Any]],
) -> None:
    if (
        manifest.get("schema") != "hippo/task-memory-manifest/v1"
        or manifest.get("task_id") != context.task_id
        or manifest.get("project") != context.project
    ):
        raise TaskMemoryError("manifest-mismatch")
    entries = manifest.get("entries")
    digest = manifest.get("sha256")
    if not isinstance(entries, list) or not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise TaskMemoryError("manifest-mismatch")
    unsigned = dict(manifest)
    unsigned.pop("sha256", None)
    expected = _sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if digest != expected:
        raise TaskMemoryError("manifest-mismatch")
    entry_map: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TaskMemoryError("manifest-mismatch")
        note_id = entry.get("note_id")
        if not isinstance(note_id, str) or note_id in entry_map:
            raise TaskMemoryError("manifest-mismatch")
        if entry.get("project", context.project) != context.project:
            raise TaskMemoryError("scope-mismatch")
        entry_map[note_id] = entry
    if set(entry_map) != {str(candidate["note_id"]) for candidate in candidates}:
        raise TaskMemoryError("manifest-mismatch")
    for candidate in candidates:
        entry = entry_map[str(candidate["note_id"])]
        if (
            entry.get("content_hash") != candidate["content_hash"]
            or entry.get("content_version") != candidate["content_version"]
        ):
            raise TaskMemoryError("manifest-mismatch")


def _materialize_snapshot(
    *,
    context: TaskMemoryContext,
    manifest: Mapping[str, Any],
    candidates: Mapping[str, Mapping[str, Any]],
    snapshot_root: Path | None,
) -> tuple[str, Path]:
    if snapshot_root is None:
        raise ValueError("snapshot root unavailable")
    entries: dict[str, dict[str, str]] = {}
    manifest_entries = {
        str(item["note_id"]): item for item in manifest.get("entries", ()) if isinstance(item, Mapping)
    }
    for note_id, candidate in candidates.items():
        entry = manifest_entries.get(note_id)
        content = entry.get("content") if entry is not None else None
        if (
            not isinstance(content, str)
            or _sha256(content.encode("utf-8")) != candidate["content_hash"]
        ):
            raise ValueError("snapshot entry missing or hash mismatch")
        entries[note_id] = {
            "content_hash": str(candidate["content_hash"]),
            "content_version": str(candidate["content_version"]),
            "content": content,
        }
    document = {
        "schema": "cortex/task-memory-snapshot/v1",
        "task_id": context.task_id,
        "project": context.project,
        "manifest_sha256": manifest["sha256"],
        "entries": entries,
    }
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    snapshot_id = _sha256(encoded)
    root = snapshot_root.expanduser().resolve()
    _mkdir_private(root)
    directory = root / "task-memory" / "snapshots" / context.task_id
    _mkdir_private(directory)
    path = directory / f"{snapshot_id}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != encoded:
            raise ValueError("snapshot content-address conflict")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o444)
        _fsync_dir(directory)
    return snapshot_id, path


def _event(
    context: TaskMemoryContext,
    event_name: str,
    *,
    mode: str,
    candidate: Mapping[str, Any] | None = None,
    reason: str | None = None,
    permission_layer: str | None = None,
    provider_code: str | None = None,
    snapshot_id: str | None = None,
    evidence: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if event_name not in _EVENT_NAMES:
        raise ValueError("unsupported task memory event")
    candidate = candidate or {}
    row: dict[str, Any] = {
        "schema": TASK_MEMORY_EVENT_SCHEMA,
        "event": event_name,
        "task_id": context.task_id,
        "attempt_id": context.attempt_id,
        "session_id": context.session_id,
        "session_proxy": context.session_proxy,
        "repo": context.repo,
        "work_id": context.work_id,
        "workflow_run_id": context.workflow_run_id,
        "job_id": context.job_id,
        "card": context.card,
        "executor": context.executor,
        "model_id": context.model_id,
        "tool": context.tool,
        "project": context.project,
        "task_kind": context.task_kind,
        "mode": mode,
        "eligible_authorized": bool(candidate.get("note_id"))
        and event_name != "ineligible",
        "note_id": candidate.get("note_id"),
        "content_hash": candidate.get("content_hash"),
        "content_version": candidate.get("content_version"),
        "timestamp": _now_iso(),
    }
    if reason is not None:
        row["reason"] = _bounded_reason(reason)
    if permission_layer is not None:
        row["permission_layer"] = permission_layer if permission_layer in {"unknown", "host", "provider", "executor"} else "unknown"
    if provider_code in _PROVIDER_DIAGNOSTIC_CODES:
        row["provider_code"] = provider_code
    if snapshot_id is not None:
        row["snapshot_id"] = snapshot_id
    if evidence is not None:
        row["evidence"] = dict(evidence)
    if candidate:
        row["applicability"] = list(candidate.get("applicability", ()))
        row["relevance_reason"] = candidate.get("relevance_reason", "")
        row["source_time"] = candidate.get("source_time")
    if event_name == "content-returned":
        # Hippo's strict funnel treats inline and snapshot as not-read.
        row["counts_as_read"] = mode == "note_fetch"
    else:
        row["counts_as_read"] = False
    logical_key = {
        "task_id": row["task_id"],
        "attempt_id": row["attempt_id"],
        "event": row["event"],
        "note_id": row["note_id"],
        "content_hash": row["content_hash"],
    }
    row["event_id"] = "tmr-" + _sha256(
        json.dumps(logical_key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )[:32]
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ValueError("task memory receipt exceeds bounded size")
    return row


class TaskMemoryReceiptStore:
    """由 Manager 擁有的 append-only receipt sidecar，與 registry、Hippo 分離。"""

    def __init__(self, coordinator_root: str | Path) -> None:
        self.root = Path(coordinator_root).expanduser().resolve()

    @staticmethod
    def _key(repo: str, work_id: str, run_id: str) -> str:
        material = json.dumps([repo, work_id, run_id], ensure_ascii=False, separators=(",", ":"))
        return _sha256(material.encode("utf-8"))

    def sidecar_path(self, repo: str, work_id: str, run_id: str) -> Path:
        return self.root / "task-memory" / "receipts" / f"{self._key(repo, work_id, run_id)}.jsonl"

    def sidecar_ref(self, repo: str, work_id: str, run_id: str) -> str:
        return f"cortex-task-memory://receipts/{self._key(repo, work_id, run_id)}"

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        row = _validate_event(event)
        path = self.sidecar_path(row["repo"], row["work_id"], row["workflow_run_id"])
        directory = path.parent
        _mkdir_private(directory)
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            rows = self._read_path(path)
            existing = next((item for item in rows if item["event_id"] == row["event_id"]), None)
            if existing is not None:
                if _event_equivalence(existing) != _event_equivalence(row):
                    raise ValueError("task memory receipt conflict: duplicate event id changed payload")
                return existing
            encoded = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            current_size = path.stat().st_size if path.exists() else 0
            if current_size + len(encoded) > MAX_SIDECAR_BYTES:
                raise ValueError("task memory receipt sidecar exceeds size bound")
            fd = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                offset = 0
                while offset < len(encoded):
                    written = os.write(fd, encoded[offset:])
                    if written <= 0:
                        raise OSError("short write while appending task memory receipt")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(directory)
            return row
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def events_for_run(self, repo: str, work_id: str, run_id: str) -> list[dict[str, Any]]:
        return self._read_path(self.sidecar_path(repo, work_id, run_id))

    def _read_path(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise ValueError("task memory receipt sidecar path invalid")
        if path.stat().st_size > MAX_SIDECAR_BYTES:
            raise ValueError("task memory receipt sidecar exceeds size bound")
        rows: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("task memory receipt sidecar is not a regular file")
            stream = os.fdopen(fd, "rb")
            fd = -1
        finally:
            if fd >= 0:
                os.close(fd)
        with stream:
            for raw in stream:
                if not raw.endswith(b"\n"):
                    raise ValueError("task memory receipt sidecar has incomplete row")
                try:
                    row = _validate_event(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("task memory receipt sidecar row invalid") from exc
                previous = seen.get(row["event_id"])
                if previous is not None:
                    if _event_equivalence(previous) != _event_equivalence(row):
                        raise ValueError("task memory receipt sidecar contains conflicting duplicate")
                    continue
                seen[row["event_id"]] = row
                rows.append(row)
        return rows


def summarize_canary(
    events: Sequence[Mapping[str, Any]],
    *,
    minimum_successes: int = 5,
    minimum_rate: float = 0.95,
    legacy_schema_compatible: bool = True,
    observed_blockers: Sequence[str] = (),
) -> dict[str, Any]:
    """計算版本化取得指標；不讀取或修改 strict KPI state。"""

    if minimum_successes < 1 or not 0.0 <= minimum_rate <= 1.0:
        raise ValueError("canary gate bounds invalid")
    known_blockers = {
        "scope-leak",
        "cross-project-misattribution",
        "relay-overwrite",
        "legacy-output-schema-break",
    }
    if any(not isinstance(item, str) or item not in known_blockers for item in observed_blockers):
        raise ValueError("unknown task memory canary blocker")
    by_path: dict[str, dict[str, Any]] = {}
    selected: dict[str, dict[tuple[str, str, str], None]] = {}
    succeeded: dict[str, set[tuple[str, str, str]]] = {}
    denials: dict[str, set[str]] = {}
    blockers: set[str] = set()
    blockers.update(observed_blockers)
    selected_all: set[tuple[str, str, str]] = set()
    read_attempts: set[tuple[str, str, str]] = set()
    returned_all: set[tuple[str, str, str]] = set()
    for raw in events:
        event = _validate_event(raw)
        mode = event.get("mode")
        if mode not in {"inline", "snapshot", "note_fetch"}:
            mode = event.get("requested_mode")
        if mode in {"inline", "snapshot", "note_fetch"}:
            selected.setdefault(mode, {})
            succeeded.setdefault(mode, set())
            denials.setdefault(mode, set())
            row = by_path.setdefault(
                mode,
                {
                    "path": mode,
                    "authorized_attempts": 0,
                    "successes": 0,
                    "authorized_failures": 0,
                    "permission_denials": 0,
                },
            )
            if event.get("event") == "candidate-selected" and event.get("eligible_authorized") is True:
                identity = (
                    str(event["task_id"]),
                    str(event.get("note_id") or ""),
                    str(event.get("content_hash") or ""),
                )
                selected[mode][identity] = None
                selected_all.add(identity)
            if event.get("event") == "read-attempted":
                read_attempts.add(
                    (
                        str(event["task_id"]),
                        str(event.get("note_id") or ""),
                        str(event.get("content_hash") or ""),
                    )
                )
            if event.get("event") in {"content-returned", "context-delivered"}:
                identity = (
                    str(event["task_id"]),
                    str(event.get("note_id") or ""),
                    str(event.get("content_hash") or ""),
                )
                succeeded[mode].add(identity)
                returned_all.add(identity)
                if identity not in selected_all:
                    blockers.add("receipt-without-candidate")
                if event.get("event") == "content-returned" and mode in {"snapshot", "note_fetch"} and identity not in read_attempts:
                    blockers.add("return-without-read-attempt")
            if event.get("event") == "applied-with-evidence":
                identity = (
                    str(event["task_id"]),
                    str(event.get("note_id") or ""),
                    str(event.get("content_hash") or ""),
                )
                if identity not in returned_all:
                    blockers.add("applied-without-return")
            if event.get("event") == "read-failed" and event.get("reason") == "permission-denied":
                denials[mode].add(str(event["attempt_id"]))
    for mode, row in by_path.items():
        eligible = set(selected.get(mode, {}))
        successes = eligible & succeeded.get(mode, set())
        row["authorized_attempts"] = len(eligible)
        row["successes"] = len(successes)
        row["authorized_failures"] = len(eligible - successes)
        row["permission_denials"] = len(denials.get(mode, set()))
        row["success_rate"] = len(successes) / len(eligible) if eligible else None
        row["passed"] = (
            len(successes) >= minimum_successes
            and row["success_rate"] is not None
            and row["success_rate"] >= minimum_rate
        )
    if not legacy_schema_compatible:
        blockers.add("legacy-output-schema-break")
    return {
        "schema": "cortex/task-memory-canary/v1",
        "paths": by_path,
        "ineligible_count": sum(
            1 for event in events if event.get("event") == "ineligible"
        ),
        "read_failure_count": sum(
            1 for event in events if event.get("event") == "read-failed"
        ),
        "unknown_or_ineligible": sum(
            1 for event in events if event.get("event") in {"ineligible", "read-failed"}
        ),
        "blockers": sorted(blockers),
        "legacy_strict_kpi_mutated": False,
        "passed": bool(by_path) and all(row["passed"] for row in by_path.values()) and not blockers,
    }


def project_task_memory_read_model(
    *,
    work_item: Any,
    runs: Sequence[Any],
    jobs: Sequence[Mapping[str, Any]],
    receipts: TaskMemoryReceiptStore,
    blocking_reason: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """唯讀串接 Monitor Work Item、Manager WorkflowRun/Jobs 與 receipt sidecar。"""

    repo = _required_string(getattr(work_item, "repo", None), "work_item.repo")
    work_id = _required_string(getattr(work_item, "work_id", None), "work_item.work_id")
    matching_runs = [run for run in runs if run.repo == repo and run.work_id == work_id]
    run_ids = {run.run_id for run in matching_runs}
    matching_jobs = [
        dict(job)
        for job in jobs
        if job.get("workflow_run_id") in run_ids
    ]
    run_rows: list[dict[str, Any]] = []
    for run in matching_runs:
        run_jobs = [job for job in matching_jobs if job.get("workflow_run_id") == run.run_id]
        events = receipts.events_for_run(repo, work_id, run.run_id)
        plan_rows = [
            {
                "ref": item.ref,
                "kind": item.kind,
                "sha256": item.baseline_sha256,
            }
            for item in getattr(run, "planning_authority", ())
        ]
        gates = [item.to_dict() for item in getattr(run, "gate_refs", ())]
        run_rows.append(
            {
                "run_id": run.run_id,
                "status": run.status,
                "state": run.current_phase,
                "facets": list(getattr(run, "facets", ()) or ()),
                "needs_human_reason": getattr(run, "needs_human_reason", None),
                "source_revision": run.source_revision,
                "planning_source_revision": run.planning_source_revision,
                "planning_artifacts": plan_rows,
                "routing_identity": dict(run.resolved_model_chain or {}),
                "jobs": [
                    {
                        "job_id": job.get("job_id"),
                        "card": job.get("workflow_card"),
                        "phase": job.get("workflow_phase"),
                        "state": job.get("status"),
                        "executor": job.get("executor"),
                        "model_id": job.get("model_id"),
                        "session_id": job.get("session_id"),
                        "session_proxy": (
                            None
                            if job.get("session_id")
                            else f"job:{job.get('job_id')}"
                            if isinstance(job.get("job_id"), str)
                            else None
                        ),
                    }
                    for job in run_jobs
                ],
                "receipts": {
                    "sidecar_ref": receipts.sidecar_ref(repo, work_id, run.run_id) if events else None,
                    "event_count": len(events),
                    "events": events,
                },
                "gate_evidence": {
                    "workflow_refs": list(run.evidence_refs),
                    "gate_refs": gates,
                    "test_steps": [
                        {"card": step.card, "policy": step.test_policy, "result": step.gate_result}
                        for step in run.steps
                        if step.test_policy not in {None, "none"}
                    ],
                    "review_jobs": [
                        job.get("job_id")
                        for job in run_jobs
                        if job.get("workflow_phase") == "review"
                    ],
                },
            }
        )
    run_rows.sort(key=lambda row: row["run_id"])
    return {
        "schema": TASK_MEMORY_READ_MODEL_SCHEMA,
        "work_item": {
            "repo": repo,
            "work_id": work_id,
            "state": getattr(work_item, "state", None),
            "phase": getattr(work_item, "phase", None),
            "facets": list(getattr(work_item, "facets", ()) or ()),
            "next_actions": list(getattr(work_item, "next_actions", ()) or ()),
            "blocking_reason": dict(blocking_reason) if isinstance(blocking_reason, Mapping) else None,
        },
        "runs": run_rows,
    }


def _validate_event(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("task memory receipt must be an object")
    row = dict(value)
    allowed = {
        "schema", "event", "event_id", "task_id", "attempt_id", "session_id",
        "session_proxy", "repo", "work_id", "workflow_run_id", "job_id", "card",
        "executor", "model_id", "tool", "project", "task_kind", "mode",
        "eligible_authorized", "note_id", "content_hash", "content_version", "timestamp",
        "reason", "permission_layer", "snapshot_id", "applicability", "relevance_reason",
        "source_time", "evidence", "counts_as_read", "provider_code",
    }
    if set(row) - allowed:
        raise ValueError("task memory receipt contains unsupported fields")
    required = {
        "schema",
        "event",
        "event_id",
        "task_id",
        "attempt_id",
        "repo",
        "work_id",
        "workflow_run_id",
        "job_id",
        "card",
        "executor",
        "tool",
        "project",
        "task_kind",
        "mode",
        "eligible_authorized",
        "timestamp",
        "counts_as_read",
    }
    if (
        not required <= set(row)
        or row.get("schema") != TASK_MEMORY_EVENT_SCHEMA
        or row.get("event") not in _EVENT_NAMES
        or row.get("project") != row.get("repo")
        or not isinstance(row.get("eligible_authorized"), bool)
        or not isinstance(row.get("timestamp"), str)
        or row.get("mode") not in {"inline", "snapshot", "note_fetch", "ineligible", "failure"}
        or not isinstance(row.get("counts_as_read"), bool)
    ):
        raise ValueError("task memory receipt schema invalid")
    for field_name in required - {
        "schema",
        "event",
        "eligible_authorized",
        "timestamp",
        "counts_as_read",
    }:
        if not isinstance(row.get(field_name), str) or not row[field_name]:
            raise ValueError(f"task memory receipt {field_name} invalid")
    if row.get("reason") is not None and row["reason"] not in _FAILURE_REASONS:
        raise ValueError("task memory receipt reason invalid")
    if row.get("provider_code") is not None and row["provider_code"] not in _PROVIDER_DIAGNOSTIC_CODES:
        raise ValueError("task memory provider diagnostic code invalid")
    if row.get("permission_layer") is not None and row["permission_layer"] not in {
        "unknown",
        "host",
        "provider",
        "executor",
    }:
        raise ValueError("task memory permission layer invalid")
    if row.get("model_id") is not None and (
        not isinstance(row["model_id"], str) or not row["model_id"]
    ):
        raise ValueError("task memory receipt model_id invalid")
    if (row.get("session_id") is None) == (row.get("session_proxy") is None):
        raise ValueError("task memory receipt needs exactly one session identity")
    for field_name in ("session_id", "session_proxy"):
        if row.get(field_name) is not None and (
            not isinstance(row[field_name], str) or not row[field_name]
        ):
            raise ValueError(f"task memory receipt {field_name} invalid")
    if row.get("note_id") is not None and (
        not isinstance(row["note_id"], str) or _NOTE_ID_RE.fullmatch(row["note_id"]) is None
    ):
        raise ValueError("task memory receipt note_id invalid")
    if row.get("content_hash") is not None and (
        not isinstance(row["content_hash"], str) or _SHA256_RE.fullmatch(row["content_hash"]) is None
    ):
        raise ValueError("task memory receipt content_hash invalid")
    if row["counts_as_read"] != (
        row["event"] == "content-returned" and row["mode"] == "note_fetch"
    ):
        raise ValueError("task memory receipt violates strict read semantics")
    expected_modes = {
        "content-returned": {"snapshot", "note_fetch"},
        "context-delivered": {"inline"},
        "read-attempted": {"snapshot", "note_fetch"},
        "snapshot-ready": {"snapshot"},
        "ineligible": {"ineligible"},
    }
    if row["event"] in expected_modes and row["mode"] not in expected_modes[row["event"]]:
        raise ValueError("task memory receipt event/mode mismatch")
    if row["event"] in {
        "candidate-selected", "offer-emitted", "read-attempted", "content-returned",
        "context-delivered", "snapshot-ready", "applied-with-evidence",
    } and (
        row.get("note_id") is None
        or row.get("content_hash") is None
        or row.get("content_version") is None
    ):
        raise ValueError("task memory receipt candidate binding missing")
    if row.get("content_version") is not None and (
        not isinstance(row["content_version"], str) or not row["content_version"]
    ):
        raise ValueError("task memory receipt content_version invalid")
    if row["event"] in {"read-failed", "ineligible"} and row.get("reason") is None:
        raise ValueError("task memory failure receipt reason missing")
    if row.get("reason") == "permission-denied" and row.get("permission_layer") is None:
        raise ValueError("task memory permission layer must remain explicit")
    if row.get("snapshot_id") is not None and (
        not isinstance(row["snapshot_id"], str)
        or _SHA256_RE.fullmatch(row["snapshot_id"]) is None
    ):
        raise ValueError("task memory receipt snapshot id invalid")
    if row.get("applicability") is not None and (
        not isinstance(row["applicability"], list)
        or any(
            not isinstance(item, str) or len(item) > MAX_PUBLIC_FIELD_CHARS
            for item in row["applicability"]
        )
    ):
        raise ValueError("task memory receipt applicability invalid")
    if row.get("relevance_reason") is not None and (
        not isinstance(row["relevance_reason"], str)
        or len(row["relevance_reason"]) > MAX_PUBLIC_FIELD_CHARS
    ):
        raise ValueError("task memory receipt relevance reason invalid")
    if row.get("source_time") is not None and (
        not isinstance(row["source_time"], str) or len(row["source_time"]) > 100
    ):
        raise ValueError("task memory receipt source time invalid")
    evidence = row.get("evidence")
    if row["event"] == "applied-with-evidence":
        if (
            not isinstance(evidence, Mapping)
            or set(evidence) != {"ref", "sha256"}
            or not _is_repo_relative(evidence.get("ref"))
            or not isinstance(evidence.get("sha256"), str)
            or _SHA256_RE.fullmatch(evidence["sha256"]) is None
        ):
            raise ValueError("task memory applied receipt evidence invalid")
    elif evidence is not None:
        raise ValueError("task memory evidence is only valid on applied receipt")
    logical_key = {
        "task_id": row["task_id"],
        "attempt_id": row["attempt_id"],
        "event": row["event"],
        "note_id": row.get("note_id"),
        "content_hash": row.get("content_hash"),
    }
    expected_id = "tmr-" + _sha256(
        json.dumps(logical_key, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )[:32]
    if row.get("event_id") != expected_id:
        raise ValueError("task memory receipt event id invalid")
    if row.get("task_id") != _stable_task_id(
        row["repo"], row["work_id"], row["workflow_run_id"]
    ):
        raise ValueError("task memory receipt task identity invalid")
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        raise ValueError("task memory receipt exceeds size bound")
    return row


def _event_equivalence(event: Mapping[str, Any]) -> bytes:
    normalized = {key: value for key, value in event.items() if key != "timestamp"}
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _require_manifest_candidate(prepared: PreparedTaskMemory, note_id: str) -> dict[str, Any]:
    if not isinstance(note_id, str) or _NOTE_ID_RE.fullmatch(note_id) is None:
        raise ValueError("note is outside task manifest")
    candidate = prepared.candidates.get(note_id)
    if candidate is None:
        raise ValueError("note is outside task manifest")
    manifest_entries = prepared.manifest.get("entries", ())
    if not any(
        isinstance(entry, Mapping)
        and entry.get("note_id") == note_id
        and entry.get("content_hash") == candidate.get("content_hash")
        and entry.get("content_version") == candidate.get("content_version")
        for entry in manifest_entries
    ):
        raise ValueError("note is outside task manifest")
    return candidate


def _verify_applied_artifact(worktree: object, evidence: Mapping[str, Any]) -> None:
    """以 Job worktree 內的實際唯讀檔案驗證 applied evidence SHA。"""

    reference = evidence.get("ref")
    expected_hash = evidence.get("sha256")
    if (
        not isinstance(worktree, (str, Path))
        or not isinstance(reference, str)
        or not _is_repo_relative(reference)
        or not isinstance(expected_hash, str)
        or _SHA256_RE.fullmatch(expected_hash) is None
    ):
        raise ValueError("task memory applied evidence artifact cannot be verified")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    opened: list[int] = []
    try:
        parent_fd = os.open(worktree, flags)
        opened.append(parent_fd)
        parts = PurePosixPath(reference).parts
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            child_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if not final:
                child_flags |= getattr(os, "O_DIRECTORY", 0)
            child_fd = os.open(part, child_flags, dir_fd=parent_fd)
            opened.append(child_fd)
            parent_fd = child_fd
        file_stat = os.fstat(parent_fd)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_APPLIED_ARTIFACT_BYTES:
            raise ValueError("task memory applied evidence artifact cannot be verified")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(parent_fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_APPLIED_ARTIFACT_BYTES:
                raise ValueError("task memory applied evidence artifact cannot be verified")
            digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise ValueError("task memory applied evidence artifact hash mismatch")
    except (OSError, TypeError) as exc:
        raise ValueError("task memory applied evidence artifact cannot be verified") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _schema_major(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    match = _SCHEMA_MAJOR_RE.search(value)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _stable_task_id(repo: str, work_id: str, run_id: str) -> str:
    material = json.dumps([repo, work_id, run_id], ensure_ascii=False, separators=(",", ":"))
    return "task-" + _sha256(material.encode("utf-8"))[:40]


def _bounded_reason(reason: str) -> str:
    return reason if reason in _FAILURE_REASONS else "provider-error"


def _safe_public_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", value)
    text = _ABSOLUTE_PATH_RE.sub("[path]", text)
    return text.strip()[:limit]


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _repo_ref(value: object) -> str:
    if not isinstance(value, str) or not _is_repo_relative(value):
        raise ValueError("task memory related file must be repo-relative")
    return value


def _is_repo_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mkdir_private(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise ValueError("task memory state directory invalid")
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        if item.is_symlink() or not item.is_dir():
            raise ValueError("task memory state directory invalid")
        os.chmod(item, 0o700)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
