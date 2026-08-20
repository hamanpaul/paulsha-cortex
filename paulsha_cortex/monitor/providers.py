"""Authoritative source providers for the unified Monitor read model."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import yaml

from paulsha_cortex.config import paths
from paulsha_cortex.coordinator import candidate_base
from paulsha_cortex.github_rate_limit import is_auth_signal, is_rate_limit_signal

from .git_mirror import (
    GitMirrorError,
    GitRunner,
    LocalGitMirror,
    unavailable_provenance,
)
from .event_spool import (
    EventSpool,
    TargetedRefresh,
    coalesce_hints,
    parse_event_timestamp,
)
from .github_issue_sync import (
    DEFAULT_FULL_SYNC_INTERVAL_SECONDS,
    HTTP_NOT_FOUND,
    GitHubResponse,
    IssueEntry,
    IssueSyncState,
    IssueSyncStateError,
    IssueSyncStore,
    cursor_from,
    dedupe_entries,
    drift_between,
    issue_request_path,
    issues_request_path,
    parse_include_response,
)
from .github_pressure import (
    RATE_LIMIT_KIND_PRIMARY,
    RATE_LIMIT_KIND_SECONDARY,
    RATE_LIMIT_KIND_UNKNOWN,
    GitHubPressureGate,
)
from .work_models import ProviderSnapshot, WorkSource


logger = logging.getLogger(__name__)

_ARCHIVE_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-(?P<name>.+)$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(parts: Sequence[bytes]) -> str:
    value = hashlib.sha256()
    for part in parts:
        value.update(len(part).to_bytes(8, "big"))
        value.update(part)
    return value.hexdigest()


def _read_revision(path: Path) -> str:
    return f"local-sha256:{_digest((path.read_bytes(),))}"


def _safe_file(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise OSError(f"artifact escapes repository root: {path}") from error
    if not resolved.is_file():
        raise OSError(f"artifact is not a regular file: {path}")
    return resolved


class RepoWorkProvider:
    """Scan only the fixed artifact paths defined by the accepted contract."""

    def __init__(self, repo_root: str | Path, *, repo: str) -> None:
        self.repo_root = Path(repo_root)
        self.repo = repo
        self.provider_id = f"repo:{repo}"

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        try:
            sources = self._scan_sources()
            observations = self._scan_observations(sources)
            collisions = self._active_archive_collisions()
            revision = "repo-overlay:" + _digest(
                tuple(
                    f"{source.source_id}\0{source.revision}".encode("utf-8")
                    for source in sources
                )
            )
        except (OSError, UnicodeError, ValueError) as error:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="degraded",
                last_attempt_at=attempted_at,
                last_success_at=None,
                revision=None,
                diagnostics=(f"repo scan unavailable: {error}",),
                sources=(),
                observations={},
            )
        if collisions:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="degraded",
                last_attempt_at=attempted_at,
                last_success_at=None,
                revision=None,
                diagnostics=tuple(
                    f"active/archive collision: {name}" for name in collisions
                ),
                sources=sources,
                observations=observations,
            )
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision=revision,
            diagnostics=(),
            sources=sources,
            observations=observations,
        )

    def _scan_observations(self, sources: Sequence[WorkSource]) -> dict:
        signals: list[dict[str, object]] = []
        for source in sources:
            if source.kind not in {
                "todo",
                "superpowers_spec",
                "superpowers_plan",
                "openspec",
            }:
                continue
            paths_to_check: list[Path]
            if source.kind == "openspec":
                change = self.repo_root / "openspec" / "changes" / source.ref
                paths_to_check = [change / "proposal.md", change / "design.md", change / "tasks.md"]
            else:
                paths_to_check = [self.repo_root / source.ref]
            work_id = next(
                (
                    value
                    for path in paths_to_check
                    if path.is_file()
                    if (value := _frontmatter_work_item(path)) is not None
                ),
                None,
            )
            if work_id is None:
                continue
            signals.append(
                {
                    "work_id": work_id,
                    "kind": "artifact_slug",
                    "value": source.ref,
                    "source_ids": [source.source_id],
                    "weight": 1.0,
                }
            )
        return {"inferred_signals": signals}

    def _scan_sources(self) -> tuple[WorkSource, ...]:
        definitions = (
            ("todo", "docs/superpowers/workstreams/**/todo.md"),
            ("superpowers_spec", "docs/superpowers/specs/**/*.md"),
            ("superpowers_plan", "docs/superpowers/plans/**/*.md"),
        )
        sources: list[WorkSource] = []
        for kind, pattern in definitions:
            for discovered in sorted(self.repo_root.glob(pattern)):
                path = _safe_file(self.repo_root, discovered)
                relative = discovered.relative_to(self.repo_root).as_posix()
                sources.append(
                    WorkSource(
                        source_id=f"{kind}:{self.repo}:{relative}",
                        kind=kind,
                        ref=relative,
                        revision=_read_revision(path),
                        status="active",
                        confidence="confirmed",
                        provider=self.provider_id,
                    )
                )

        changes = self.repo_root / "openspec" / "changes"
        if changes.is_dir():
            for change_dir in sorted(changes.iterdir()):
                if change_dir.name == "archive" or not change_dir.is_dir():
                    continue
                files = self._openspec_files(change_dir)
                if not files:
                    continue
                revision_parts: list[bytes] = []
                for path in files:
                    safe = _safe_file(self.repo_root, path)
                    relative = path.relative_to(self.repo_root).as_posix()
                    revision_parts.extend((relative.encode("utf-8"), safe.read_bytes()))
                sources.append(
                    WorkSource(
                        source_id=f"openspec:{self.repo}:{change_dir.name}",
                        kind="openspec",
                        ref=change_dir.name,
                        revision=f"local-sha256:{_digest(tuple(revision_parts))}",
                        status="active",
                        confidence="confirmed",
                        provider=self.provider_id,
                    )
                )
        return tuple(sorted(sources, key=lambda source: (source.kind, source.ref)))

    @staticmethod
    def _openspec_files(change_dir: Path) -> tuple[Path, ...]:
        files = [
            path
            for name in ("proposal.md", "design.md", "tasks.md")
            if (path := change_dir / name).is_file()
        ]
        specs = change_dir / "specs"
        if specs.is_dir():
            files.extend(path for path in specs.rglob("*.md") if path.is_file())
        return tuple(sorted(files))

    def _active_archive_collisions(self) -> tuple[str, ...]:
        changes = self.repo_root / "openspec" / "changes"
        archive = changes / "archive"
        if not changes.is_dir() or not archive.is_dir():
            return ()
        active = {
            path.name
            for path in changes.iterdir()
            if path.is_dir() and path.name != "archive"
        }
        archived: set[str] = set()
        for path in archive.iterdir():
            if not path.is_dir():
                continue
            match = _ARCHIVE_DATE_PREFIX.match(path.name)
            archived.add(match.group("name") if match else path.name)
        return tuple(sorted(active & archived))


class WorkflowRegistryProvider:
    """Read repo-scoped WorkflowRun v2 records without adopting legacy slices."""

    def __init__(
        self,
        repo: str,
        *,
        state_path: str | Path | None = None,
        candidate_base_probe: "candidate_base.MirrorDistanceProbe | None" = None,
    ) -> None:
        self.repo = repo
        self.provider_id = f"workflow:{repo}"
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else paths.coordinator_root() / "jobs.json"
        )
        # #731 (C)：候選 git base 的距離量測。**唯讀**（`rev-parse` ／
        # `rev-list --count`，絕不 fetch）——Monitor 是讀模型，跟著 fetch 會讓
        # 「看一眼 work item」變成會改變 mirror 的動作。注入點留給測試；
        # production 每次 scan 重建一個，才不會把上一輪的 main 位置快取成永久值。
        self._candidate_base_probe = candidate_base_probe

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        if not self.state_path.exists():
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="ok",
                last_attempt_at=attempted_at,
                last_success_at=attempted_at,
                revision="registry:absent",
                diagnostics=(),
                sources=(),
                observations={},
            )
        try:
            raw = self.state_path.read_bytes()
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                raise ValueError("registry root must be an object")
            version = payload.get("schema_version")
            if version == 1:
                _validate_workflow_v1_root(payload)
                return ProviderSnapshot(
                    provider_id=self.provider_id,
                    status="ok",
                    last_attempt_at=attempted_at,
                    last_success_at=attempted_at,
                    revision=f"registry-sha256:{_digest((raw,))}",
                    diagnostics=(),
                    sources=(),
                    observations={},
                )
            if "workflows" in payload:
                rows = _validate_canonical_coordinator_v2_root(payload)
            else:
                _validate_workflow_v2_root(payload)
                rows = payload["workflow_runs"]
            # #731 (C)：候選基底的第二來源是 job 的 `dispatch_head`（實機 0820
            # 逐字：29 個 run 的 `frozen_readiness` 全為 null，唯一記著基底的是
            # 這裡）。同一份 payload 已經含 `jobs`，不必另開檔案讀取。
            job_rows = payload.get("jobs")
            if not isinstance(job_rows, list):
                job_rows = []
            candidate_base_probe = (
                self._candidate_base_probe
                if self._candidate_base_probe is not None
                else candidate_base.MirrorDistanceProbe(
                    mirror_root=candidate_base.default_mirror_root()
                )
            )
            sources: list[WorkSource] = []
            links: dict[str, str] = {}
            schema_retry: dict[str, dict[str, int]] = {}
            needs_human_reasons: dict[str, dict[str, object]] = {}
            candidate_git_bases: dict[str, dict[str, object]] = {}
            diagnostics: list[str] = []
            validated_completions: dict[str, list[dict[str, object]]] = {}
            for row in rows:
                _validate_workflow_v2_row(row)
                if row.get("repo") != self.repo:
                    continue
                run_id = _nonempty(row.get("run_id"), "run_id")
                work_id = _nonempty(row.get("work_id"), "work_id")
                try:
                    completion = _validated_workflow_completion(
                        row, state_path=self.state_path
                    )
                except _WorkflowCompletionValidationError as error:
                    diagnostics.append(
                        f"workflow completion skipped: {run_id}: {error}"
                    )
                    continue
                status = _nonempty(row.get("status", row.get("current_phase")), "status")
                source_id = f"workflow_run:{self.repo}:{run_id}"
                sources.append(
                    WorkSource(
                        source_id=source_id,
                        kind="workflow_run",
                        ref=run_id,
                        revision=f"registry:{payload.get('sequence', payload.get('seq', 0))}",
                        status=status,
                        confidence="confirmed",
                        provider=self.provider_id,
                    )
                )
                _add_workflow_link(links, source_id, work_id)
                # #261 D5：schema mismatch retry 計數存在既有的 attempts 欄位裡
                # （刻意不新增 WorkflowRun 欄位——新欄位會讓每一個 row 落入
                # _WORKFLOW_V2_OPTIONAL_ROW_KEYS 之外而使整份 projection degraded）。
                retry_rows = _schema_retry_rows(row.get("attempts"))
                if retry_rows:
                    schema_retry.setdefault(work_id, {}).update(retry_rows)
                # 診斷 invariant（#527）：run 掛著 needs_human 時，把它的結構化
                # 理由帶進 observations，`cortex work show` 才有得印。手法比照
                # 上面的 `schema_retry`——資料走既有的 observations 通道隨
                # snapshot round-trip，不必在 WorkItem／WorkSnapshot 上新增欄位
                # （新增欄位會讓每個 row 落在 `_WORKFLOW_V2_OPTIONAL_ROW_KEYS`
                # 之外而讓整份 projection degraded，#261 已付過這個學費）。
                blocking = _needs_human_reason_row(row)
                if blocking is not None:
                    needs_human_reasons[work_id] = {"run_id": run_id, **blocking}
                # #731 (C)：候選 git base（真的那個 40-hex commit SHA）與落後
                # mirror 上 origin/main 的距離。同樣走 observations 通道，理由
                # 與上面兩段一致（新增 row 欄位會讓整份 projection degraded）。
                # 只投影仍 ongoing 的 run：已 done／superseded 的 run 的舊基底
                # 出現在 `work show` 上只會誤導（比照 `_needs_human_reason_row`）。
                if row.get("status", "ongoing") == "ongoing":
                    git_base = candidate_base.resolve_candidate_git_base(
                        frozen_readiness=row.get("frozen_readiness"),
                        build_dispatch_heads=(
                            candidate_base.build_dispatch_heads_from_jobs(
                                job_rows, run_id=run_id
                            )
                        ),
                        probe=candidate_base_probe,
                    )
                    candidate_git_bases[work_id] = {
                        "run_id": run_id,
                        **git_base.to_dict(),
                    }
                if status != "superseded":
                    for ref in row.get("issue_refs", []):
                        _add_workflow_link(links, f"github_issue:{ref}", work_id)
                    for ref in row.get("pr_refs", []):
                        _add_workflow_link(links, f"github_pr:{ref}", work_id)
                    for ref in row.get("openspec_refs", []):
                        for canonical_id in (
                            f"openspec:{self.repo}:{ref}",
                            f"github_openspec:{self.repo}:{ref}:active",
                            f"github_openspec:{self.repo}:{ref}:archived",
                        ):
                            _add_workflow_link(links, canonical_id, work_id)
                if completion is not None:
                    validated_completions.setdefault(work_id, []).append(completion)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            return ProviderSnapshot(
                provider_id=self.provider_id,
                status="degraded",
                last_attempt_at=attempted_at,
                last_success_at=None,
                revision=None,
                diagnostics=(f"workflow registry unavailable: {error}",),
                sources=(),
                observations={},
            )
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision=f"registry-sha256:{_digest((raw,))}",
            diagnostics=tuple(diagnostics),
            sources=tuple(sources),
            observations={
                "workflow_links": links,
                "validated_completions": validated_completions,
                "schema_retry": schema_retry,
                "needs_human_reasons": needs_human_reasons,
                "candidate_git_bases": candidate_git_bases,
            },
        )



SCHEMA_RETRY_ATTEMPT_PREFIX = "schema-mismatch:"


def _schema_retry_rows(attempts: object) -> dict[str, int]:
    """#261：從 run.attempts 取出 `schema-mismatch:<card>` 的計數。"""

    if not isinstance(attempts, Mapping):
        return {}
    rows: dict[str, int] = {}
    for key, value in attempts.items():
        if not isinstance(key, str) or not key.startswith(SCHEMA_RETRY_ATTEMPT_PREFIX):
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        rows[key[len(SCHEMA_RETRY_ATTEMPT_PREFIX):]] = value
    return rows


def _needs_human_reason_row(row: Mapping[str, Any]) -> dict[str, object] | None:
    """診斷 invariant（#527）：從 run row 取出 needs_human 的結構化理由。

    只在 run 仍 ongoing 且確實掛著 `needs_human` 時回傳——已 done／superseded 的
    run 的舊理由不該出現在 `cortex work show` 上。缺欄位（本 invariant 之前寫的
    legacy run）回傳 ``None``，讓呈現面自然退回舊行為，不 fail-closed：
    provider 一旦 degraded，整個 work item 的讀模型都會被凍住（#523）。
    """

    if row.get("status", "ongoing") != "ongoing":
        return None
    facets = row.get("facets")
    if not isinstance(facets, (list, tuple)) or "needs_human" not in facets:
        return None
    payload = row.get("needs_human_reason")
    if not isinstance(payload, Mapping):
        return None
    reason = payload.get("reason")
    detail = payload.get("detail")
    source = payload.get("source")
    if not all(isinstance(item, str) and item for item in (reason, detail, source)):
        return None
    evidence_refs = payload.get("evidence_refs")
    return {
        "reason": reason,
        "detail": detail,
        "source": source,
        "recorded_at": payload.get("recorded_at"),
        "evidence_refs": [
            item for item in (evidence_refs or []) if isinstance(item, str) and item
        ],
    }


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


_WORKFLOW_V1_KEYS = frozenset({"schema_version", "seq", "jobs", "slices"})
_WORKFLOW_V2_KEYS = frozenset(
    {"schema_version", "sequence", "workflow_runs", "legacy_records"}
)
_WORKFLOW_V2_REQUIRED_ROW_KEYS = frozenset({"run_id", "repo", "work_id"})
_WORKFLOW_V2_OPTIONAL_ROW_KEYS = frozenset(
    {
        "status", "current_phase", "claim_key", "combo", "steps", "issue_refs",
        "openspec_refs", "pr_refs", "attempts", "evidence", "facets",
        "created_at", "updated_at", "completion_record_path",
        "completion_record_hash", "completion_record_revision", "source_revisions",
        "pr_candidate", "merge_revision",
        # Exact coordinator.workflow.WorkflowRun fields.
        "source_revision", "workspace_root", "evidence_refs", "gate_refs",
        "brainstorm_required", "primary_domain", "candidate_head",
        "verified_head", "gate_status", "completion_source_revisions",
        "planning_authority", "planning_source_revision",
        # #216：retry_classification（provenance-only，比照 pr_candidate／
        # merge_revision 的可選欄位模式）。
        "retry_classification",
        # #222（design #208 H.2）：五維 sizing 總分／band 的 work item 快照。
        "sizing_score", "sizing_band",
        # #223（design #208 H.3）：Red band 拆分次數快照。
        "decomposition_depth",
        # #208 收口 wiring：#213 freeze 接線持久化欄位／#211 pre-claim readiness
        # 凍結集，供 builder worktree 建立消費。
        "plan_review_passed", "frozen_readiness",
        # #205：run-scoped planner/builder/reviewer 模型鏈覆寫（claim 時凍結）
        # 與其解析結果稽核紀錄（executor/model/domain/來源），provenance-only，
        # 比照 retry_classification／sizing_score 的可選欄位模式。
        "model_chain_override", "resolved_model_chain",
        # #202：combo 選牌來源／task_type／bypass reason 的 provenance-only 欄位。
        "combo_selection",
        # 診斷 invariant（#527／#514／#515／#511／#482）：run 被轉入 needs_human
        # 時同時落地的結構化理由（機器可讀 reason ＋ 人可讀 detail ＋ 來源位置）。
        # **必須列在這裡**：這個 whitelist 是封閉的，漏掉會讓每一個 run row 都被
        # 判成「含不支援的欄位」，整份 workflow projection 因此 degraded——那正是
        # #261 D5 選擇把 schema retry 計數塞進既有 `attempts` 而不新增欄位的原因。
        "needs_human_reason",
    }
)


class _WorkflowCompletionValidationError(ValueError):
    """Bad completion contents for one row; provider may skip that row only."""


def _validate_workflow_v1_root(payload: Mapping) -> None:
    if set(payload) != _WORKFLOW_V1_KEYS:
        raise ValueError("workflow registry v1 root keys are invalid")
    if (
        isinstance(payload.get("seq"), bool)
        or not isinstance(payload.get("seq"), int)
        or not isinstance(payload.get("jobs"), list)
        or not isinstance(payload.get("slices"), list)
    ):
        raise ValueError("workflow registry v1 root values are invalid")


def _validate_workflow_v2_root(payload: Mapping) -> None:
    if payload.get("schema_version") != 2:
        raise ValueError("unsupported workflow registry schema_version")
    if set(payload) != _WORKFLOW_V2_KEYS:
        raise ValueError("workflow registry v2 root keys are invalid")
    sequence = payload.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("workflow registry sequence must be a non-negative integer")
    if not isinstance(payload.get("workflow_runs"), list):
        raise ValueError("workflow_runs must be an array")
    legacy = payload.get("legacy_records")
    if (
        not isinstance(legacy, Mapping)
        or set(legacy) != {"jobs", "slices"}
        or not isinstance(legacy.get("jobs"), list)
        or not isinstance(legacy.get("slices"), list)
    ):
        raise ValueError("workflow registry legacy_records are invalid")


def _validate_canonical_coordinator_v2_root(payload: Mapping) -> list[dict[str, object]]:
    """Read the exact JobRegistry v2 schema without instantiating its writer.

    Monitor is strictly read-only here: importing ``JobRegistry`` would run its
    migration path and violate the single-writer boundary.
    """

    required = {"schema_version", "seq", "jobs", "slices", "workflows", "legacy_records"}
    # #519：`reclaim_resets`（semantic-reclaim 熔斷的重置水位）是加法相容的可選
    # 根欄位——寫入端不 bump schema_version，好讓本欄位出現前寫下的既有狀態檔照
    # 常載入。Monitor 因此必須同時接受「有」與「沒有」兩種形狀；仍不接受任何其
    # 他未知根欄位，維持既有的 fail-closed 嚴格度。
    optional = {"reclaim_resets"}
    keys = set(payload)
    if (
        not required <= keys
        or not keys <= (required | optional)
        or payload.get("schema_version") != 2
    ):
        raise ValueError("canonical coordinator registry root keys are invalid")
    if (
        isinstance(payload.get("seq"), bool)
        or not isinstance(payload.get("seq"), int)
        or payload["seq"] < 0
        or not isinstance(payload.get("jobs"), list)
        or not isinstance(payload.get("slices"), list)
        or not isinstance(payload.get("workflows"), list)
        or not isinstance(payload.get("legacy_records"), Mapping)
        or not isinstance(payload.get("reclaim_resets", []), list)
    ):
        raise ValueError("canonical coordinator registry root values are invalid")
    from paulsha_cortex.coordinator.workflow import WorkflowRun

    return [WorkflowRun.from_dict(row).to_dict() for row in payload["workflows"]]


def _validate_workflow_v2_row(row: object) -> None:
    if not isinstance(row, Mapping):
        raise ValueError("workflow run must be an object")
    keys = set(row)
    if not _WORKFLOW_V2_REQUIRED_ROW_KEYS.issubset(keys):
        raise ValueError("workflow run misses required keys")
    if keys - _WORKFLOW_V2_REQUIRED_ROW_KEYS - _WORKFLOW_V2_OPTIONAL_ROW_KEYS:
        raise ValueError("workflow run contains unsupported keys")
    if "status" not in row and "current_phase" not in row:
        raise ValueError("workflow run requires status or current_phase")
    repo = _nonempty(row.get("repo"), "repo")
    if re.fullmatch(r"[^/#\s]+/[^/#\s]+", repo) is None:
        raise ValueError("workflow repo must be owner/name")
    work_id = _nonempty(row.get("work_id"), "work_id")
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise ValueError("workflow work_id must be a slug")
    _nonempty(row.get("run_id"), "run_id")
    _nonempty(row.get("status", row.get("current_phase")), "status")
    for field in ("issue_refs", "pr_refs"):
        refs = _typed_workflow_refs(row.get(field, []), field=field)
        if any(
            re.fullmatch(rf"{re.escape(repo)}#[1-9][0-9]*", ref) is None
            for ref in refs
        ):
            raise ValueError(f"workflow {field} must contain repo-scoped refs")
    openspec_refs = _typed_workflow_refs(
        row.get("openspec_refs", []), field="openspec_refs"
    )
    if any(re.fullmatch(r"[a-z0-9][a-z0-9-]*", ref) is None for ref in openspec_refs):
        raise ValueError("workflow openspec_refs must contain slugs")


def _typed_workflow_refs(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(ref, str) or not ref for ref in value
    ):
        raise ValueError(f"workflow {field} must be an array of strings")
    if len(value) != len(set(value)):
        raise ValueError(f"workflow {field} contains duplicate refs")
    return tuple(value)


def _add_workflow_link(links: dict[str, str], source_id: str, work_id: str) -> None:
    previous = links.setdefault(source_id, work_id)
    if previous != work_id:
        raise ValueError(
            f"workflow authority collision: {source_id} -> {previous}, {work_id}"
        )


def _validated_workflow_completion(
    row: Mapping, *, state_path: Path
) -> dict[str, object] | None:
    fields = (
        row.get("completion_record_path"),
        row.get("completion_record_hash"),
        row.get("completion_record_revision"),
    )
    if all(value is None for value in fields):
        return None
    if any(value is None for value in fields):
        raise ValueError("completion record path/hash/revision must be supplied together")
    record_path, expected_hash, expected_revision = fields
    if not isinstance(record_path, str) or not record_path:
        raise ValueError("completion_record_path must be a non-empty string")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None:
        raise ValueError("completion_record_hash must be a 64-char hex digest")
    if not isinstance(expected_revision, str) or re.fullmatch(r"[0-9a-fA-F]{40}", expected_revision) is None:
        raise ValueError("completion_record_revision must be a 40-char commit SHA")
    source_revisions = row.get("source_revisions", row.get("completion_source_revisions"))
    if (
        not isinstance(source_revisions, Mapping)
        or not source_revisions
        or any(
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(revision, str)
            or not revision
            for source_id, revision in source_revisions.items()
        )
    ):
        raise ValueError("completion source_revisions must be a non-empty string map")
    pr_candidate = row.get("pr_candidate")
    merge_revision = row.get("merge_revision")
    for field, value in (
        ("pr_candidate", pr_candidate),
        ("merge_revision", merge_revision),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise ValueError(f"{field} must be a 40-char commit SHA")
    try:
        path = Path(record_path)
        if path.is_symlink():
            raise ValueError("completion_record_path must not be a symlink")
        resolved = path.resolve(strict=True)
        allowed_root = (state_path.parent / "evidence" / "completion").resolve()
        try:
            resolved.relative_to(allowed_root)
        except ValueError as error:
            raise ValueError("completion_record_path escapes coordinator completion root") from error
        from paulsha_cortex.coordinator.completion import read_completion_record

        record = read_completion_record(resolved, expected_hash=expected_hash.lower())
    except (OSError, ValueError) as error:
        raise _WorkflowCompletionValidationError(str(error)) from error
    normalized_sources = dict(source_revisions)
    authority_record = record.get("work_authority")
    if isinstance(authority_record, Mapping):
        raw_sources = authority_record.get("source_revisions")
        if not isinstance(raw_sources, list) or any(
            not isinstance(value, str) or "@" not in value for value in raw_sources
        ):
            return None
        record_sources = {
            value.rsplit("@", 1)[0]: value.rsplit("@", 1)[1]
            for value in raw_sources
        }
        record_work_id = authority_record.get("work_id")
        record_run_id = authority_record.get("run_id")
        record_merge_revision = authority_record.get("merge_commit")
    else:
        record_sources = record.get("source_revisions")
        record_work_id = record.get("work_id")
        record_run_id = record.get("run_id")
        record_merge_revision = record.get("merge_revision")
    valid = all(
        (
            record.get("candidate") == expected_revision.lower() == pr_candidate.lower(),
            record_work_id == row.get("work_id"),
            record_run_id == row.get("run_id"),
            record_sources == normalized_sources,
            record_merge_revision == merge_revision.lower(),
        )
    )
    if not valid:
        return None
    return {
        "run_id": row["run_id"],
        "pr_candidate": pr_candidate.lower(),
        "merge_revision": merge_revision.lower(),
        "source_revisions": normalized_sources,
    }


def _frontmatter_work_item(path: Path) -> str | None:
    return _frontmatter_work_item_text(path.read_text(encoding="utf-8"), source=str(path))


def _frontmatter_work_item_text(text: str, *, source: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise ValueError(f"unterminated frontmatter: {source}")
    payload = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"frontmatter must be an object: {source}")
    value = payload.get("work_item")
    return value if isinstance(value, str) and value else None


def _markdown_tasks_complete(path: Path) -> bool:
    return _markdown_tasks_complete_text(path.read_text(encoding="utf-8"))


def _markdown_tasks_complete_text(text: str) -> bool:
    tasks = re.findall(r"^\s*[-*]\s+\[([ xX])\]", text, flags=re.MULTILINE)
    return bool(tasks) and all(marker.lower() == "x" for marker in tasks)


class CommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessCommandRunner:
    def run(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


# #506：rate-limit 診斷字串。三者**都必須**讓
# ``github_rate_limit.is_rate_limit_signal`` 為真——``coordinator/claim.py`` 靠它
# 產生 ``provider-authority-rate-limited-canonical`` 這個 reason code（#370 的
# 成果），字串一旦不含 "rate limit" 就會靜默回歸成「authority 損毀」語意。
# tests/test_monitor_github_pressure_506.py 鎖住這點。
_RATE_LIMIT_DIAGNOSTICS = {
    RATE_LIMIT_KIND_SECONDARY: "secondary rate limit",
    RATE_LIMIT_KIND_PRIMARY: "primary rate limit exhausted",
    RATE_LIMIT_KIND_UNKNOWN: "rate limit exceeded",
}

_RETRY_AFTER = re.compile(r"retry[-\s]?after[\"'\s:=]+(\d+)", re.IGNORECASE)
_RATE_LIMIT_RESET = re.compile(r"x-ratelimit-reset[\"'\s:=]+(\d+)", re.IGNORECASE)


def _retry_after_seconds(message: str | None) -> float | None:
    """從失敗訊息取出 ``Retry-After``／``x-ratelimit-reset`` 提示（沒有就 None）。

    #506：gh 不保證把 header 透出到 stderr，所以這只是「有就尊重」的加值路徑；
    取不到時退避純靠指數。
    """

    if not message:
        return None
    match = _RETRY_AFTER.search(message)
    if match is not None:
        return float(match.group(1))
    match = _RATE_LIMIT_RESET.search(message)
    if match is not None:
        remaining = float(match.group(1)) - time.time()
        return remaining if remaining > 0 else None
    return None


def _probe_rate_limit(
    runner: CommandRunner, *, timeout_seconds: float
) -> tuple[str, float | None]:
    """#506：以 ``gh api rate_limit`` 分辨 primary 與 secondary rate limit。

    ``rate_limit`` 端點**不計入配額**，所以即使已經被限流也能安全查詢：
    ``remaining > 0`` 代表配額還在，403 只可能來自 secondary（abuse detection，
    burst 觸發，攤平請求即可緩解）；``remaining == 0`` 才是 primary 配額耗盡
    （只能等 reset）。兩者的處置完全不同，因此值得多花這一次不計費的請求。

    回傳 ``(kind, reset_after_seconds)``；探測本身失敗／回應不可解時回
    ``unknown``——寧可退回既有的通用診斷，也不要猜錯方向。
    """

    argv = ("gh", "api", "--method", "GET", "rate_limit")
    try:
        completed = runner.run(argv, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return RATE_LIMIT_KIND_UNKNOWN, None
    if completed.returncode != 0:
        return RATE_LIMIT_KIND_UNKNOWN, None
    stdout = (
        completed.stdout.decode(errors="replace")
        if isinstance(completed.stdout, bytes)
        else completed.stdout
    )
    try:
        payload = json.loads(stdout or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return RATE_LIMIT_KIND_UNKNOWN, None
    if not isinstance(payload, Mapping):
        return RATE_LIMIT_KIND_UNKNOWN, None
    buckets: list[Mapping] = []
    resources = payload.get("resources")
    if isinstance(resources, Mapping):
        # core 與 graphql 是本 monitor 實際會打的兩個 bucket（REST 與 graphql
        # provider 各一），任一耗盡都算 primary。
        buckets = [
            bucket
            for name in ("core", "graphql")
            if isinstance(bucket := resources.get(name), Mapping)
        ]
    if not buckets and isinstance(payload.get("rate"), Mapping):
        buckets = [payload["rate"]]
    remainings = [
        bucket
        for bucket in buckets
        if isinstance(bucket.get("remaining"), int)
        and not isinstance(bucket.get("remaining"), bool)
    ]
    if not remainings:
        return RATE_LIMIT_KIND_UNKNOWN, None
    exhausted = [bucket for bucket in remainings if bucket["remaining"] <= 0]
    if not exhausted:
        return RATE_LIMIT_KIND_SECONDARY, None
    resets = [
        float(bucket["reset"])
        for bucket in exhausted
        if isinstance(bucket.get("reset"), int) and not isinstance(bucket.get("reset"), bool)
    ]
    reset_after = max(0.0, max(resets) - time.time()) if resets else None
    return RATE_LIMIT_KIND_PRIMARY, reset_after


def _rate_limit_diagnostic(
    *,
    runner: CommandRunner,
    timeout_seconds: float,
    message: str | None,
    gate: GitHubPressureGate | None,
    prefix: str,
) -> str:
    """分診 rate-limit 失敗、登記退避，並回傳要寫進 diagnostics 的字串。"""

    kind, reset_after = _probe_rate_limit(runner, timeout_seconds=timeout_seconds)
    hint = _retry_after_seconds(message)
    if reset_after is not None:
        hint = reset_after if hint is None else max(hint, reset_after)
    diagnostic = f"{prefix} {_RATE_LIMIT_DIAGNOSTICS[kind]}"
    if gate is None:
        return diagnostic
    delay = gate.note_rate_limited(kind=kind, retry_after_seconds=hint)
    return f"{diagnostic}; backing off {delay:.0f}s"


def _backoff_diagnostic(prefix: str, blocked_seconds: float) -> str:
    return f"{prefix} rate limit backoff active; retry in {blocked_seconds:.0f}s"


class _GitHubRequestError(Exception):
    """一次 issues 請求失敗，已附好要寫進 diagnostics 的字串。"""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class _FetchResult:
    entries: tuple[IssueEntry, ...]
    not_modified: bool
    requests: int
    pages: int
    etag: str | None


@dataclass(frozen=True)
class _SpoolResult:
    """D4：一輪 spool 消費的結果（鏡像增量 ＋ 新的 per-object ETag ＋ 記帳）。"""

    entries: tuple[IssueEntry, ...]
    targeted_etags: Mapping[int, str]
    consume: tuple[Path, ...]
    changed: bool
    report: Mapping[str, Any]


class GitHubWorkProvider:
    """Read GitHub entities through authenticated ``gh api`` JSON only.

    #506 / D3：讀取走 ``state=all&since=`` ＋ ETag 條件請求的增量協定，全量只作
    每日一次的 anti-entropy 對帳。協定本身（游標紀律／ETag 綁定 path／fail-closed
    條件／drift 對帳）全部定義在 ``monitor/github_issue_sync``；本類別只負責發
    ``gh`` 請求、分診失敗、把鏡像投影成 ``ProviderSnapshot``。

    #506 / D4：增量之後再消費一次本機事件 spool（``monitor/event_spool``）——對被
    點名的物件做 targeted 條件請求，驗證通過才更新鏡像。事件是 **hint 不是
    authority**，驗不到的變更一律不寫鏡像，留給每日 anti-entropy。
    """

    # 100 筆/頁 × 50 頁＝5000 筆。超過視為分頁失控（伺服器沒收斂 Link），
    # fail closed，不讓一輪掃描無上限地打下去。
    _PAGE_LIMIT = 50

    # D4：一輪最多驗幾個被點名的物件。hook 是 per-tool-call 觸發的，一個活躍
    # job 可以在半小時內點名幾十次；沒有上限就等於把 D1–D3 省下的配額交還給
    # 事件量決定。超出的事件留在 spool，下一輪再服務（依 emitted_at FIFO）。
    _TARGETED_LIMIT = 20

    def __init__(
        self,
        repo: str,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 30,
        pressure_gate: GitHubPressureGate | None = None,
        sync_store: IssueSyncStore | None = None,
        full_sync_interval_seconds: float = DEFAULT_FULL_SYNC_INTERVAL_SECONDS,
        event_spool: EventSpool | None = None,
        targeted_refresh_limit: int | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        if repo.count("/") != 1 or any(not part for part in repo.split("/")):
            raise ValueError("GitHub repo must use owner/name")
        self.repo = repo
        self.provider_id = f"github:{repo}"
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds
        # #506：未注入 gate 時完全維持舊行為（不節流、不退避）。
        self.pressure_gate = pressure_gate
        # D3：沒有 durable store 就沒有游標可續——每輪都是全量。這不是降級路徑，
        # 是「無狀態即無增量」的誠實契約（測試與一次性呼叫端走的就是它）。
        self.sync_store = sync_store
        self.full_sync_interval_seconds = float(full_sync_interval_seconds)
        # D4：沒有 spool（或 spool 目錄不存在，例如 D5 hook 尚未部署到這台機器）
        # 就完全維持 D3 行為——事件入口是**加速器**，不是任何東西的必要條件。
        self.event_spool = event_spool
        self.targeted_refresh_limit = (
            self._TARGETED_LIMIT if targeted_refresh_limit is None else int(targeted_refresh_limit)
        )
        self._now = now or _utcnow

    # -- 掃描 -------------------------------------------------------------

    def scan(self) -> ProviderSnapshot:
        attempted_at = self._now()
        if self.pressure_gate is not None:
            blocked = self.pressure_gate.blocked_seconds()
            if blocked > 0:
                # #506：退避期間直接跳過，不再硬撞一次 403 去加深限流。
                return self._failure(attempted_at, _backoff_diagnostic("github", blocked))
        notes: list[str] = []
        previous: IssueSyncState | None = None
        if self.sync_store is not None:
            try:
                previous = self.sync_store.load(self.repo)
            except IssueSyncStateError as error:
                # 驗收 4：游標／ETag 狀態損壞 → fail closed 全量重建，
                # **絕不**拿半壞的游標去做增量。
                notes.append(f"github issue sync state unusable; rebuilt in full: {error}")
                logger.warning(
                    "github issue sync state for %s is unusable; rebuilding in full: %s",
                    self.repo,
                    error,
                )
        if previous is None:
            mode, reason = "full", notes[0] if notes else "no durable cursor"
        elif previous.needs_full_sync(
            now=attempted_at, interval_seconds=self.full_sync_interval_seconds
        ):
            mode, reason = "full", "anti-entropy"
        else:
            mode, reason = "incremental", None

        since = previous.since if (mode == "incremental" and previous is not None) else None
        try:
            base_path = issues_request_path(self.repo, since=since)
        except IssueSyncStateError:
            # 游標通過了 state 驗證卻做不出 path——只可能是驗證漏了，退回全量。
            mode, reason, since = "full", "cursor rejected by request builder", None
            base_path = issues_request_path(self.repo)
        # anti-entropy 一輪刻意**不**帶 If-None-Match：全量的職責就是真的重讀一次，
        # 拿 304 換來的「跟上次一樣」不是對帳，是把待驗證的假設當成結論。
        etag = (
            previous.etag
            if (
                mode == "incremental"
                and previous is not None
                and previous.etag is not None
                and previous.etag_request == base_path
            )
            else None
        )

        try:
            fetched = self._fetch_pages(base_path=base_path, since=since, etag=etag)
        except _GitHubRequestError as error:
            # 分頁中斷／任一頁失敗：游標、ETag、鏡像三者原封不動（上層
            # `_retain_last_good` 續用上一份好的快照）。
            return self._failure(attempted_at, error.diagnostic)

        drift: dict[str, list[int]] | None = None
        if fetched.not_modified:
            # 驗收 2：304 不改 mirror、不動游標、不動 ETag——連寫都不寫。
            # （ETag 也刻意不從 304 回應取回：GitHub 的 304 回的是強形式
            # `"<hash>"`，與 200 給的 `W/"<hash>"` 不同，覆蓋回去會讓往後的條件
            # 請求永遠落空。）
            if previous is None:
                # etag 只可能來自 previous，走到這裡代表狀態機被破壞。
                return self._failure(attempted_at, "github returned 304 without a cursor")
            state = previous
            entries = previous.entries
        else:
            if mode == "full":
                entries = fetched.entries
                if previous is not None:
                    drift = drift_between(previous.entries, entries)
            else:
                entries = (
                    previous.merged(fetched.entries)
                    if previous is not None
                    else tuple(sorted(fetched.entries, key=lambda entry: entry.number))
                )
            try:
                state = IssueSyncState(
                    repo=self.repo,
                    entries=entries,
                    # 游標取自回應中最大的 updated_at（非本機時鐘），且永不倒退。
                    since=cursor_from(
                        fetched.entries,
                        floor=previous.since if previous is not None else None,
                    ),
                    etag=fetched.etag,
                    etag_request=base_path if fetched.etag else None,
                    last_full_sync_at=(
                        attempted_at
                        if mode == "full"
                        else (previous.last_full_sync_at if previous is not None else None)
                    ),
                )
            except IssueSyncStateError as error:
                # 合出來的狀態自己過不了驗證：degraded（上層續用上一份好的快照），
                # 絕不讓例外逸出 provider 把整個 refresh 迴圈打斷。
                return self._failure(
                    attempted_at, f"github issue sync state rejected: {error}"
                )

        if drift is not None:
            # 驗收 5：全量對帳發現 drift → 以全量為準，並同時留 log 與 observation。
            logger.warning(
                "github issue mirror drift for %s resolved in favour of the full sync: %s",
                self.repo,
                drift,
            )
            notes.append(f"github issue mirror drift resolved by full sync: {drift}")

        # D4：清單同步結束後才消費事件 spool——先做便宜的批次讀取，被它涵蓋到的
        # 事件就不必再各花一次 targeted 請求。
        spool: _SpoolResult | None = None
        if self.event_spool is not None:
            spool = self._consume_event_spool(
                state=state,
                attempted_at=attempted_at,
                delta_numbers=frozenset(entry.number for entry in fetched.entries),
                # 全量輪次重讀了**所有**物件，因此涵蓋掉所有比它早的事件；304
                # 不是一次讀取（它什麼都沒讀回來），不得算進涵蓋範圍。
                full_read=(mode == "full" and not fetched.not_modified),
                notes=notes,
            )
            targeted_etags = dict(spool.targeted_etags)
            if spool.changed or targeted_etags != state.targeted_etags_by_number:
                try:
                    state = replace(state, entries=spool.entries).with_targeted_etags(
                        targeted_etags
                    )
                except IssueSyncStateError as error:
                    return self._failure(
                        attempted_at, f"github issue sync state rejected: {error}"
                    )
                entries = state.entries

        persisted = True
        if self.sync_store is not None and state is not previous:
            try:
                self.sync_store.save(state)
            except OSError as error:
                # 快照本身是對的，只是游標沒存下來——下一輪會退回全量重建（合併
                # 本來就冪等），所以這是效能退化而非正確性問題，不該讓整輪 degraded。
                persisted = False
                notes.append(
                    f"github issue sync state not persisted; next cycle rebuilds in full: {error}"
                )
                logger.warning(
                    "github issue sync state for %s was not persisted: %s", self.repo, error
                )

        if spool is not None:
            if persisted:
                # 驗收：**處理成功才消費**。事件檔一路留到鏡像真的落地為止——
                # 中途 crash 的代價只是下一輪重驗一次（條件請求命中 304，免費）。
                spool.report["consumed"] = self.event_spool.consume(spool.consume)
            else:
                spool.report["consumed"] = 0
                spool.report["deferred"] += len(spool.consume)

        sources = tuple(
            sorted(
                (
                    entry.to_source(repo=self.repo, provider_id=self.provider_id)
                    for entry in entries
                ),
                key=lambda source: (source.kind, source.ref),
            )
        )
        revision = "github-snapshot:" + _digest(
            tuple(
                f"{source.source_id}\0{source.revision}\0{source.status}".encode("utf-8")
                for source in sources
            )
        )
        if self.pressure_gate is not None:
            # #506：一次成功即代表限流窗已過，清空退避與連續失敗計數。
            self.pressure_gate.note_success()
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision=revision,
            diagnostics=tuple(notes),
            sources=sources,
            observations={
                # R0.5 D1：issues 回應本來就含 labels——把 auto 派工 label 的持有者
                # 記進 observations，讓 manager 的 auto-claim scan 讀鏡像即可判定，
                # 不必每 tick 對每個 mapped issue 各發一次 live `gh api`（實測
                # 57 次/tick，是 fleet 對 GitHub 最大的持續壓力源）。D3 之後這份
                # 由 durable 鏡像導出，因此關閉事件一進增量就會讓該 issue 立刻
                # 退出 auto 派工名單。
                "auto_label_issues": _auto_label_issue_numbers(entries),
                "issue_sync": {
                    "mode": mode,
                    "reason": reason,
                    "not_modified": fetched.not_modified,
                    "requests": fetched.requests,
                    "conditional_requests": 1 if etag is not None else 0,
                    # 304 不計入 GitHub rate limit 配額（實測 x-ratelimit-used
                    # 在條件請求前後不變），因此配額帳與請求帳要分開記。
                    "billed_requests": fetched.requests - (1 if fetched.not_modified else 0),
                    "pages": fetched.pages,
                    "delta_entries": len(fetched.entries),
                    "mirror_entries": len(entries),
                    "since": state.since,
                    "drift": drift,
                    "persisted": persisted,
                },
                # D4：事件入口的記帳。沒接 spool 時整個鍵不出現（既有觀測消費端
                # 一行都不用改）。
                **({"event_spool": dict(spool.report)} if spool is not None else {}),
            },
        )

    # -- 傳輸 -------------------------------------------------------------

    def _fetch_pages(
        self, *, base_path: str, since: str | None, etag: str | None
    ) -> _FetchResult:
        first = self._request(base_path, etag=etag)
        if first.not_modified:
            if etag is None:
                # 沒送 If-None-Match 卻收到 304：協定被破壞。把它當成「沒有變更」
                # 會讓鏡像被一份空回應定住，寧可 degraded。
                raise _GitHubRequestError("github returned 304 without a conditional request")
            return _FetchResult(
                entries=(), not_modified=True, requests=1, pages=0, etag=etag
            )
        entries = list(self._entries(first))
        requests = 1
        pages = 1
        response = first
        while response.has_next_page:
            if pages >= self._PAGE_LIMIT:
                raise _GitHubRequestError("github issue pagination incomplete")
            pages += 1
            response = self._request(
                issues_request_path(self.repo, since=since, page=pages)
            )
            requests += 1
            if response.not_modified:
                # 續頁沒送 If-None-Match，回 304 代表協定被破壞——寧可整輪
                # degraded，也不能把一個空頁當成「這頁沒東西」而截斷鏡像。
                raise _GitHubRequestError("github issue pagination returned 304")
            entries.extend(self._entries(response))
        return _FetchResult(
            # 分頁跑的是活清單，同一個 issue 可能跨頁重複出現——傳輸層先收斂。
            entries=dedupe_entries(entries),
            not_modified=False,
            requests=requests,
            pages=pages,
            etag=first.etag,
        )

    # -- D4：事件入口 -----------------------------------------------------

    def _consume_event_spool(
        self,
        *,
        state: IssueSyncState,
        attempted_at: str,
        delta_numbers: frozenset[int],
        full_read: bool,
        notes: list[str],
    ) -> _SpoolResult:
        """掃 spool → 對被點名物件做 targeted 條件驗證 → 更新鏡像。

        規則（計畫 R0.5 原則 6）：

        - **事件是 hint 不是 authority**：只有 GitHub 自己回的物件才進鏡像。驗證
          失敗（請求錯誤）不寫鏡像**也不消費事件**；驗證回 404（物件被刪除／
          transfer 走）不從鏡像刪任何東西——那是每日全量 anti-entropy 的職責，
          單一 targeted 404 不足以區分「真的沒了」與「權限／路徑一時讀不到」。
        - **去重**：同物件多事件收斂成一次驗證（見 ``coalesce_hints``）。
        - **過期安全跳過**：事件比本輪清單讀取早、而該物件又已被本輪讀取涵蓋
          （出現在增量 delta 裡，或本輪是全量），鏡像就已經至少和事件一樣新，
          直接消費事件、不花請求。spool 是本機目錄，producer 與 consumer 共用
          同一顆時鐘，這個時間比較才成立。
        - **亂序無害**：事件本來就沒有全域順序，這裡也不推論順序——每個物件只問
          GitHub「你現在長怎樣」，答案與事件先後無關。
        """

        assert self.event_spool is not None  # 呼叫端已檢查
        report: dict[str, Any] = {
            "pending": 0,
            "objects": 0,
            "superseded": 0,
            "verified": 0,
            "confirmed": 0,
            "not_modified": 0,
            "unverified": 0,
            "requests": 0,
            "billed_requests": 0,
            "consumed": 0,
            "deferred": 0,
            "quarantined": 0,
            "ignored": {},
            "foreign_schema": 0,
        }
        scan = self.event_spool.scan(now=attempted_at)
        report["quarantined"] = len(scan.quarantined)
        report["ignored"] = dict(scan.ignored)
        report["foreign_schema"] = scan.foreign_schema
        hints = scan.for_repo(self.repo)
        report["pending"] = len(hints)
        refreshes = coalesce_hints(hints, repo=self.repo)
        report["objects"] = len(refreshes)
        if not refreshes:
            return _SpoolResult(
                entries=state.entries,
                targeted_etags=state.targeted_etags_by_number,
                consume=(),
                changed=False,
                report=report,
            )

        try:
            cycle_started = parse_event_timestamp(attempted_at)
        except ValueError:
            cycle_started = None
        by_number = state.by_number
        etags = state.targeted_etags_by_number
        consume: list[Path] = []
        changed = False
        for index, refresh in enumerate(refreshes):
            if index >= self.targeted_refresh_limit:
                # 超出本輪上限：事件留在 spool，下一輪照 emitted_at 先來先服務。
                report["deferred"] += sum(len(row.paths) for row in refreshes[index:])
                notes.append(
                    "github event spool deferred "
                    f"{len(refreshes) - index} object(s) past the per-cycle targeted limit"
                )
                break
            if self._already_covered(
                refresh,
                cycle_started=cycle_started,
                delta_numbers=delta_numbers,
                full_read=full_read,
            ):
                report["superseded"] += 1
                consume.extend(refresh.paths)
                continue
            try:
                response = self._request(
                    issue_request_path(self.repo, refresh.number),
                    etag=etags.get(refresh.number),
                    not_found_ok=True,
                )
            except _GitHubRequestError as error:
                # 驗證不到就不寫鏡像、不消費事件——留給下一輪或每日 anti-entropy。
                # 一併停掉本輪剩餘的 targeted 請求：第一個失敗多半是限流／認證，
                # 繼續打只是把退避窗撐得更深。
                report["deferred"] += sum(len(row.paths) for row in refreshes[index:])
                notes.append(f"github event spool targeted refresh failed: {error.diagnostic}")
                logger.warning(
                    "github event spool targeted refresh for %s#%s failed: %s",
                    self.repo,
                    refresh.number,
                    error.diagnostic,
                )
                break
            report["requests"] += 1
            if response.not_modified:
                # 條件請求命中：物件自上次 targeted 讀取以來沒變，鏡像已經是對的。
                # 304 不計配額，因此 billed_requests 不動。
                report["not_modified"] += 1
                consume.extend(refresh.paths)
                continue
            report["billed_requests"] += 1
            if response.status == HTTP_NOT_FOUND:
                # 物件讀不到——**不**動鏡像（不刪、不改）。刪除／transfer 這類
                # 事件本來就只有每日全量對帳看得到，一次 404 不足以當證據。
                report["unverified"] += 1
                consume.extend(refresh.paths)
                notes.append(
                    f"github event spool hint for {refresh.repo}#{refresh.number} "
                    "could not be verified (404); left to the daily anti-entropy sweep"
                )
                continue
            try:
                entry = IssueEntry.from_api(json.loads(response.body))
            except (json.JSONDecodeError, TypeError, ValueError, KeyError):
                # 壞回應不寫鏡像，也不消費事件（下一輪重試）。
                report["deferred"] += len(refresh.paths)
                notes.append(
                    f"github event spool targeted refresh for {refresh.repo}#{refresh.number} "
                    "returned malformed JSON"
                )
                continue
            if entry.number != refresh.number:
                # 回的不是我們問的那個物件（transfer 重編號等）——不信。
                report["deferred"] += len(refresh.paths)
                notes.append(
                    f"github event spool targeted refresh for {refresh.repo}#{refresh.number} "
                    f"returned issue #{entry.number}"
                )
                continue
            report["verified"] += 1
            if response.etag is not None:
                etags = {**etags, refresh.number: response.etag}
            known = by_number.get(entry.number)
            if known is None or known.differs_from(entry):
                # inferred（事件）→ confirmed（GitHub 自己回的物件）才進鏡像。
                by_number[entry.number] = entry
                changed = True
                report["confirmed"] += 1
            consume.extend(refresh.paths)

        return _SpoolResult(
            entries=tuple(sorted(by_number.values(), key=lambda item: item.number)),
            targeted_etags=etags,
            consume=tuple(consume),
            changed=changed,
            report=report,
        )

    def _already_covered(
        self,
        refresh: TargetedRefresh,
        *,
        cycle_started: datetime | None,
        delta_numbers: frozenset[int],
        full_read: bool,
    ) -> bool:
        """本輪的清單讀取是否已經涵蓋這個事件（→ 免一次 targeted 請求）。

        成立條件：事件在本輪請求發出**之前**產生，且該物件確實被本輪讀回來過
        （出現在增量 delta，或本輪是全量重讀）。GitHub 端的 replication lag 是這
        個推論唯一的殘餘風險，而那本來就是每日 anti-entropy 的守備範圍。
        """

        if cycle_started is None:
            return False
        try:
            emitted = refresh.emitted_at_time
        except ValueError:
            return False
        if emitted > cycle_started:
            return False
        return full_read or refresh.number in delta_numbers

    def _entries(self, response: GitHubResponse) -> tuple[IssueEntry, ...]:
        try:
            payload = json.loads(response.body)
            if not isinstance(payload, list):
                raise ValueError("GitHub issues response is not an array")
            return tuple(IssueEntry.from_api(entity) for entity in payload)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            raise _GitHubRequestError("github API returned malformed JSON") from error

    def _request(
        self, path: str, *, etag: str | None = None, not_found_ok: bool = False
    ) -> GitHubResponse:
        if self.pressure_gate is not None:
            # 節流改為 per-request：改動前一次 `--paginate` 是 gh 在行程內自己
            # 連發分頁，閘門完全管不到那些請求。
            self.pressure_gate.throttle()
        argv: list[str] = ["gh", "api", "--method", "GET", "--include"]
        if etag is not None:
            argv += ["--header", f"If-None-Match: {etag}"]
        argv.append(path)
        try:
            completed = self.runner.run(tuple(argv), timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            raise _GitHubRequestError("github timeout") from None
        except FileNotFoundError:
            raise _GitHubRequestError("github CLI unavailable") from None
        except OSError:
            raise _GitHubRequestError("github provider I/O failure") from None
        stdout = (
            completed.stdout.decode(errors="replace")
            if isinstance(completed.stdout, bytes)
            else (completed.stdout or "")
        )
        try:
            response = parse_include_response(stdout)
        except ValueError:
            response = None
        # 304 必須先判：gh 對任何非 2xx 都以非零離開（實測 `gh: HTTP 304`），
        # 但條件請求命中是**成功**，不是失敗。
        if response is not None and response.not_modified:
            return response
        # D4：targeted 驗證要能分辨 404（物件讀不到，交給 anti-entropy）與其他
        # 失敗（重試）。與 304 同理，gh 對 404 也以非零離開，所以得在 returncode
        # 分診之前先認狀態行——但只有明確要求的呼叫端才拿得到這個回應。
        if not_found_ok and response is not None and response.status == HTTP_NOT_FOUND:
            return response
        if completed.returncode != 0:
            message = (
                completed.stderr.decode(errors="replace")
                if isinstance(completed.stderr, bytes)
                else (completed.stderr or "")
            )
            # #370: rate limit must be checked *before* auth -- GitHub's own
            # secondary/abuse-detection rate limit messages mention "OAuth"
            # and invite re-authenticating, so an auth-first check
            # misclassifies a retryable rate limit as a dead credential.
            if is_rate_limit_signal(message):
                # #506：403 再分診成 primary／secondary，並開啟退避窗。
                raise _GitHubRequestError(
                    _rate_limit_diagnostic(
                        runner=self.runner,
                        timeout_seconds=self.timeout_seconds,
                        message=message,
                        gate=self.pressure_gate,
                        prefix="github",
                    )
                )
            if is_auth_signal(message):
                raise _GitHubRequestError("github authentication failed")
            raise _GitHubRequestError("github API request failed")
        if response is None:
            raise _GitHubRequestError("github API returned a malformed response")
        if response.status // 100 != 2:
            raise _GitHubRequestError("github API request failed")
        return response

    def _failure(self, attempted_at: str, diagnostic: str) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="degraded",
            last_attempt_at=attempted_at,
            last_success_at=None,
            revision=None,
            diagnostics=(diagnostic,),
            sources=(),
        )


# 與 coordinator/claim.py 的 AUTO_LABEL、doctor.py 的 AUTO_LABEL 同值。monitor 不
# import coordinator.claim（避免把 deck/verification 整串拉進 monitor 行程），以
# 常數複本＋本註解維持對齊；改名時三處同步（測試 test_auto_label_constant_alignment
# 釘住）。
AUTO_CLAIM_LABEL = "cortex:auto-on-going"


def _auto_label_issue_numbers(entries: Sequence[IssueEntry]) -> list[int]:
    """開啟 auto 派工 label 的 open issue 編號（排序去重）。

    D3 之後輸入是 durable 鏡像而非單輪回應：一個 issue 在網頁端被關閉，關閉事件
    會隨 `state=all&since=` 的增量進來覆蓋掉鏡像那一列，該 issue 因此在同一個
    refresh 週期內就退出這份名單——manager 不會再 auto-claim 它。
    欄位形狀的嚴格驗證前移到 `IssueEntry`（回應與 durable 狀態兩邊共用同一套）。
    PR 不參與 auto 派工，跳過。
    """

    return sorted(
        entry.number
        for entry in entries
        if not entry.is_pull_request
        and entry.state == "open"
        and AUTO_CLAIM_LABEL in entry.labels
    )


class _GitHubRateLimitError(OSError):
    """#506：``_json`` 內部命中 rate limit 的專用訊號。

    刻意繼承 ``OSError``——既有 ``scan()`` 的 catch-all（``except (OSError, ...)``）
    仍然接得住，即使日後有人漏接這個新型別也只會退回舊的通用診斷，不會讓
    provider 例外逸出。
    """

    def __init__(self, message: str) -> None:
        super().__init__("gh api rate limited")
        self.detail = message


class GitHubTerminalProvider:
    """Read closing references and remote default-branch archive evidence."""

    _QUERY = """query($owner:String!,$name:String!,$cursor:String){repository(owner:$owner,name:$name){defaultBranchRef{name target{... on Commit{oid}}} pullRequests(first:100,after:$cursor,states:[OPEN,CLOSED,MERGED]){pageInfo{hasNextPage endCursor} nodes{number body headRefName headRefOid state mergedAt mergeCommit{oid parents(first:3){totalCount}} closingIssuesReferences(first:100){pageInfo{hasNextPage} nodes{number state}}}}}}"""
    _PULL_REQUEST_PAGE_LIMIT = 20

    def __init__(
        self,
        repo: str,
        *,
        runner: CommandRunner | None = None,
        timeout_seconds: float = 30,
        retry_delays: tuple[float, ...] = (2.0, 5.0, 10.0),
        sleeper: Callable[[float], None] = time.sleep,
        relevant_pr_numbers: tuple[int, ...] | None = None,
        pressure_gate: GitHubPressureGate | None = None,
        repo_root: str | Path | None = None,
        git_runner: GitRunner | None = None,
    ) -> None:
        self.repo = repo
        self.provider_id = f"github-terminal:{repo}"
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds
        # #506 / D2：remote 檔案內容與 merge ancestry 一律走本機 git（見
        # ``git_mirror``）。``repo_root`` 是該 repo 在 workspace 的 canonical
        # checkout；缺它就沒有鏡像，本輪一旦真的需要讀檔／判 ancestry 就 fail
        # closed（degraded），**不會**退回 REST，也不會當成「檔案不存在」。
        self.repo_root = None if repo_root is None else Path(repo_root)
        self.git_runner = git_runner
        # #506：本 provider 仍是 REST 請求量最大的一支（graphql 分頁 + 1 次
        # git tree），節流／退避沒接上它等於沒做減壓。
        self.pressure_gate = pressure_gate
        if any(
            not isinstance(delay, (int, float))
            or isinstance(delay, bool)
            or not math.isfinite(float(delay))
            or delay < 0
            for delay in retry_delays
        ):
            raise ValueError("GitHub terminal retry delays must be finite non-negative numbers")
        self.retry_delays = tuple(float(delay) for delay in retry_delays)
        self.sleeper = sleeper
        if relevant_pr_numbers is not None and (
            len(relevant_pr_numbers) != len(set(relevant_pr_numbers))
            or any(
                not isinstance(number, int)
                or isinstance(number, bool)
                or number <= 0
                for number in relevant_pr_numbers
            )
        ):
            raise ValueError("relevant PR numbers must be unique positive integers")
        self.relevant_pr_numbers = (
            None if relevant_pr_numbers is None else frozenset(relevant_pr_numbers)
        )

    def scan(self) -> ProviderSnapshot:
        attempted_at = _utcnow()
        if self.pressure_gate is not None:
            blocked = self.pressure_gate.blocked_seconds()
            if blocked > 0:
                # #506：退避期間整支 scan 跳過——這裡省下的是一輪裡最大宗的請求量。
                return self._failure(
                    attempted_at, _backoff_diagnostic("github terminal", blocked)
                )
        owner, name = self.repo.split("/", 1)
        try:
            graph = self._json(self._pull_request_argv(owner=owner, name=name))
            repository = graph["data"]["repository"]
            default_branch_ref = repository["defaultBranchRef"]
            pulls = repository["pullRequests"]
            pull_nodes = list(pulls["nodes"])
            page_count = 1
            while pulls["pageInfo"]["hasNextPage"]:
                if page_count >= self._PULL_REQUEST_PAGE_LIMIT:
                    raise ValueError("pull request pagination incomplete")
                cursor = pulls["pageInfo"]["endCursor"]
                if not isinstance(cursor, str) or not cursor:
                    raise ValueError("pull request pagination incomplete")
                graph = self._json(
                    self._pull_request_argv(owner=owner, name=name, cursor=cursor)
                )
                repository = graph["data"]["repository"]
                pulls = repository["pullRequests"]
                pull_nodes.extend(pulls["nodes"])
                page_count += 1
            default_branch = default_branch_ref["name"]
            default_revision = default_branch_ref["target"]["oid"]
            if re.fullmatch(r"[0-9a-fA-F]{40}", default_revision) is None:
                raise ValueError("default branch revision is invalid")
            tree = self._json(
                (
                    "gh", "api", "--method", "GET",
                    f"repos/{self.repo}/git/trees/{default_revision}?recursive=1",
                )
            )
            if tree.get("truncated") is not False:
                raise ValueError("default branch tree is truncated")
            if not isinstance(tree.get("tree"), list):
                raise ValueError("default branch tree entries are invalid")
            todo_entries = self._remote_todo_entries(tree)
            paths = {
                row["path"]
                for row in tree["tree"]
                if isinstance(row, Mapping) and isinstance(row.get("path"), str)
            }
            active_changes = {
                parts[2]
                for path in paths
                if len(parts := path.split("/")) >= 4
                and parts[:2] == ["openspec", "changes"]
                and parts[2] != "archive"
            }
            archived_changes = {
                match.group("name")
                for path in paths
                if path.startswith("openspec/changes/archive/")
                if len(path.split("/")) >= 5
                if (match := _ARCHIVE_DATE_PREFIX.match(path.split("/")[3]))
            }
            if active_changes & archived_changes:
                raise ValueError("remote active/archive OpenSpec collision")
            sources = tuple(
                WorkSource(
                    source_id=f"github_openspec:{self.repo}:{ref}:{status}",
                    kind="openspec",
                    ref=ref,
                    revision=f"github-tree:{default_revision.lower()}",
                    status=status,
                    confidence="confirmed",
                    provider=self.provider_id,
                    title=ref,
                )
                for status, refs in (
                    ("active", sorted(active_changes)),
                    ("archived", sorted(archived_changes)),
                )
                for ref in refs
            )
            links: dict[str, str] = {}
            remote_prs: list[dict[str, object]] = []
            branches: list[dict[str, str]] = []
            # D2：ancestry 不再逐 PR 打 ``compare``；先把候選收集起來，等本輪唯一
            # 一次 ``mirror.require()`` 把物件備齊後再用本機 ``merge-base`` 判定。
            ancestry_candidates: list[tuple[int, str, dict[str, object]]] = []
            for pull in pull_nodes:
                number = pull["number"]
                pr_source_id = f"github_pr:{self.repo}#{number}"
                head_ref = pull.get("headRefName")
                if isinstance(head_ref, str) and head_ref:
                    branches.append({"source_id": pr_source_id, "ref": head_ref})
                closing = pull["closingIssuesReferences"]
                if closing["pageInfo"]["hasNextPage"]:
                    raise ValueError("closing issue pagination incomplete")
                if pull.get("state") != "CLOSED":
                    issues = closing["nodes"]
                    if issues:
                        primary_issue_source = f"github_issue:{self.repo}#{issues[0]['number']}"
                        links[pr_source_id] = primary_issue_source
                        for issue in issues[1:]:
                            issue_source = f"github_issue:{self.repo}#{issue['number']}"
                            links[issue_source] = primary_issue_source
                merge = pull.get("mergeCommit") or {}
                merge_revision = merge.get("oid")
                parent_count = (merge.get("parents") or {}).get("totalCount")
                merge_commit = bool(
                    pull.get("state") == "MERGED"
                    and pull.get("mergedAt")
                    and isinstance(merge_revision, str)
                    and re.fullmatch(r"[0-9a-fA-F]{40}", merge_revision)
                    and isinstance(parent_count, int)
                    and not isinstance(parent_count, bool)
                    and parent_count >= 2
                    and (
                        self.relevant_pr_numbers is None
                        or number in self.relevant_pr_numbers
                    )
                )
                candidate = pull.get("headRefOid")
                row: dict[str, object] = {
                    "source_id": pr_source_id,
                    "candidate": (
                        candidate.lower()
                        if isinstance(candidate, str)
                        and re.fullmatch(r"[0-9a-fA-F]{40}", candidate)
                        else None
                    ),
                    "merge_revision": (
                        merge_revision.lower()
                        if isinstance(merge_revision, str)
                        and re.fullmatch(r"[0-9a-fA-F]{40}", merge_revision)
                        else None
                    ),
                    "merged_with_merge_commit": False,
                }
                if merge_commit:
                    ancestry_candidates.append(
                        (number, str(row["merge_revision"]), row)
                    )
                remote_prs.append(row)
            mirror: LocalGitMirror | None = None
            if todo_entries or ancestry_candidates:
                mirror = self._mirror()
                mirror.require(
                    required=(
                        default_revision.lower(),
                        *(entry[1] for entry in todo_entries),
                    ),
                    ancestry=tuple(
                        (number, revision)
                        for number, revision, _ in ancestry_candidates
                    ),
                    default_branch=default_branch,
                )
            remote_todos = self._remote_todos(todo_entries, mirror=mirror)
            if mirror is not None:
                for _, merge_revision, row in ancestry_candidates:
                    row["merged_with_merge_commit"] = mirror.is_ancestor(
                        merge_revision, default_revision.lower()
                    )
        except subprocess.TimeoutExpired:
            return self._failure(attempted_at, "github terminal timeout")
        except GitMirrorError as error:
            # D2：本機 git 讀不到就 fail closed——上層 ``_retain_last_good`` 會保留
            # 上一份鏡像，絕不把讀取失敗降級成「檔案不存在／不是 ancestor」。
            return self._failure(
                attempted_at, f"github terminal git mirror unavailable: {error}"
            )
        except _GitHubRateLimitError as error:
            # #506：與 GitHubWorkProvider 同一套分診／退避；本 provider 原本把
            # 限流混進 "evidence unavailable"，下游完全分辨不出來。
            return self._failure(
                attempted_at,
                _rate_limit_diagnostic(
                    runner=self.runner,
                    timeout_seconds=self.timeout_seconds,
                    message=error.detail,
                    gate=self.pressure_gate,
                    prefix="github terminal",
                ),
            )
        except FileNotFoundError:
            return self._failure(attempted_at, "github CLI unavailable")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._failure(attempted_at, "github terminal evidence unavailable")
        observations = {
            "closing_links": links,
            "remote_prs": sorted(remote_prs, key=lambda row: str(row["source_id"])),
            "remote_openspec": {
                "active": sorted(active_changes),
                "archived": sorted(archived_changes),
            },
            "remote_openspec_observed": True,
            "default_branch": default_branch,
            "default_revision": default_revision.lower(),
            "remote_todos": remote_todos,
            "branches": branches,
            # D2 provenance：這一輪的 remote 檔案內容與 ancestry 是從哪裡、用哪些
            # ref 讀來的。degraded 的那一輪不會走到這裡，因此 provenance 永遠對應
            # 一份成功的鏡像讀取。
            "remote_reads": (
                dict(mirror.provenance)
                if mirror is not None
                else dict(
                    unavailable_provenance(
                        "no remote artifact or ancestry required this cycle"
                    )
                )
            ),
        }
        if self.pressure_gate is not None:
            # #506：整支 scan 走完都沒被限流，退避窗可以關掉。
            self.pressure_gate.note_success()
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="ok",
            last_attempt_at=attempted_at,
            last_success_at=attempted_at,
            revision="github-terminal:" + _digest((json.dumps(observations, sort_keys=True).encode(),)),
            diagnostics=(),
            sources=sources,
            observations=observations,
        )

    def _pull_request_argv(
        self,
        *,
        owner: str,
        name: str,
        cursor: str | None = None,
    ) -> tuple[str, ...]:
        argv = [
            "gh", "api", "graphql",
            "-f", f"query={self._QUERY}",
            "-F", f"owner={owner}",
            "-F", f"name={name}",
        ]
        if cursor is not None:
            argv.extend(("-F", f"cursor={cursor}"))
        return tuple(argv)

    def _json(self, argv: Sequence[str]) -> Mapping:
        completed = None
        for attempt in range(len(self.retry_delays) + 1):
            if self.pressure_gate is not None:
                # #506：節流點在**每一次** REST 請求前（graphql 分頁與 git tree）。
                # D2 之後逐檔 contents 與逐 PR compare 已改走本機 git，不經此路徑
                # ——git 協定不受 REST rate limit 管轄，節流它只是白白拖慢掃描。
                self.pressure_gate.throttle()
            completed = self.runner.run(argv, timeout=self.timeout_seconds)
            if completed.returncode == 0:
                break
            error = f"{completed.stderr}\n{completed.stdout}"
            if is_rate_limit_signal(error):
                # #506：限流一律不重試——重試 403 只會把 secondary limit 撞得更深。
                raise _GitHubRateLimitError(error)
            if (
                attempt >= len(self.retry_delays)
                or re.search(r"\bHTTP (?:502|503|504)\b", error) is None
            ):
                raise OSError("gh api failed")
            self.sleeper(self.retry_delays[attempt])
        if completed is None or completed.returncode != 0:
            raise OSError("gh api failed")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, Mapping):
            raise ValueError("GitHub response must be an object")
        return payload

    def _failure(self, attempted_at: str, diagnostic: str) -> ProviderSnapshot:
        return ProviderSnapshot(
            provider_id=self.provider_id,
            status="degraded",
            last_attempt_at=attempted_at,
            last_success_at=None,
            revision=None,
            diagnostics=(diagnostic,),
            sources=(),
            observations={},
        )

    def _mirror(self) -> LocalGitMirror:
        if self.repo_root is None:
            raise GitMirrorError(
                "no local checkout configured for git-native remote reads"
            )
        return LocalGitMirror(
            self.repo_root,
            repo=self.repo,
            runner=self.git_runner,
            timeout_seconds=self.timeout_seconds,
        )

    @staticmethod
    def _remote_todo_entries(
        tree: Mapping,
    ) -> tuple[tuple[str, str, re.Match[str] | None], ...]:
        """從 REST tree 挑出要讀的 blob（path、blob sha、archive match）。

        只解析、不讀內容——內容統一在本輪唯一一次 ``git cat-file --batch`` 取得。
        """

        entries: list[tuple[str, str, re.Match[str] | None]] = []
        for entry in tree.get("tree", []):
            if not isinstance(entry, Mapping):
                continue
            path = entry.get("path")
            revision = entry.get("sha")
            if not isinstance(path, str):
                continue
            is_todo = re.fullmatch(
                r"docs/superpowers/workstreams/.+/todo\.md", path
            ) is not None
            archive_match = re.fullmatch(
                r"openspec/changes/archive/(\d{4}-\d{2}-\d{2}-.+)/tasks\.md",
                path,
            )
            if not is_todo and archive_match is None:
                continue
            if entry.get("type") != "blob" or not isinstance(revision, str) or re.fullmatch(
                r"[0-9a-fA-F]{40}", revision
            ) is None:
                raise ValueError("remote Todo tree entry is invalid")
            entries.append((path, revision.lower(), archive_match))
        return tuple(entries)

    def _remote_todos(
        self,
        entries: Sequence[tuple[str, str, re.Match[str] | None]],
        *,
        mirror: LocalGitMirror | None,
    ) -> list[dict[str, object]]:
        if not entries:
            return []
        if mirror is None:  # pragma: no cover - 呼叫端保證 entries 非空即有鏡像
            raise GitMirrorError("git mirror is required to read remote artifacts")
        # D2：blob 一律以 tree 給的 sha 定址讀取。sha 定址本身就是內容識別，取代
        # 舊 ``contents`` 路徑的 type／path／sha／encoding 四項比對；讀不到會由
        # ``read_blobs`` raise（fail closed），不會退化成「檔案不存在」。
        texts = mirror.read_blobs(tuple(revision for _, revision, _ in entries))
        rows: list[dict[str, object]] = []
        for path, revision, archive_match in entries:
            text = texts[revision]
            row: dict[str, object] = {
                "path": path,
                "revision": revision,
                "complete": _markdown_tasks_complete_text(text),
            }
            if archive_match is not None:
                archived_name = archive_match.group(1)
                match = _ARCHIVE_DATE_PREFIX.match(archived_name)
                if match is None:
                    raise ValueError("remote archived OpenSpec tasks path is invalid")
                row["openspec_ref"] = match.group("name")
            else:
                work_id = _frontmatter_work_item_text(
                    text, source=f"github:{path}@{revision}"
                )
                if work_id is None:
                    continue
                row["work_id"] = work_id
            rows.append(row)
        return sorted(
            rows,
            key=lambda row: (
                str(row.get("work_id", row.get("openspec_ref", ""))),
                str(row["path"]),
            ),
        )
