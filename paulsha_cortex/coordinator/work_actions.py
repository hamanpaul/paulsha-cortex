"""Manager-owned work lifecycle mutations reached only through the control queue."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import stat as statmod
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from paulsha_cortex.config import paths
from paulsha_cortex._yaml import safe_load
from paulsha_cortex.github_rate_limit import is_rate_limit_signal

from .diagnostics import diagnostic_reason
from .claim import (
    AUTO_LABEL,
    ClaimCandidate,
    authority_digest_without_planning_outputs,
    build_claim_key,
    build_label_argv,
    claim_identity_digest,
    openspec_refs_compatible as claim_openspec_refs_compatible,
    planning_declared_openspec_changes as claim_planning_declared_openspec_changes,
    decide_auto_claim,
    decide_manual_start,
    load_work_authorities,
    load_work_authorities_with_snapshot_items,
    load_work_authority,
    work_authority_digest,
)
from .delivery import (
    ArchiveGateFacts,
    ForeignReviewEvidence,
    MaintainerReviewEvidence,
    PullRequestMetadata,
    ReviewLoop,
    ShipOrchestrator,
    build_openspec_archive_argv,
    build_openspec_validate_change_argv,
    repair_budget_for_band,
    validate_archive_gate,
    validate_pr_metadata,
    _maintainer_review_authority_digest,
    _maintainer_review_pr_matches,
    _normalize_maintainer_review_evidence_refs,
    _validate_foreign_review,
)
from .github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    DeliveryPolicy,
    GitHubDeliveryClient,
    evaluate_delivery_gate,
    evaluate_remote_closure,
)
from . import candidate_base
from . import engineering_outcome
from . import not_claimable
from . import verification
from . import worktree_reclaim
from .preflight import PreflightRequest, load_preflight_command, run_preflight
from .work_bridge import (
    current_sizing_snapshot,
    extract_model_chain_override,
    resolve_trusted_repo_root,
    workflow_status,
)
from .workflow import GateEvidenceRef, brainstorm_authority_bound


logger = logging.getLogger(__name__)

Runner = Callable[..., object]
ShipExecutor = Callable[[dict[str, Any], object], dict[str, Any]]

# Retirement-family actions tear down a local stuck run and never depend on an
# issue's live open/closed state, so they tolerate a rate-limited-but-
# last-known-good canonical GitHub authority (see the ``load_work_authority``
# call in ``execute_work_action``). Every other action keeps fail-closed.
_RETIREMENT_ACTIONS = frozenset({"abandon", "retire-delivered"})

# #519：`reset-reclaim-budget` 與 retirement family 同屬「只動本機 coordinator
# 狀態、不依賴 issue 當下 open/closed 的解卡動作」，適用同一條 rate-limit 容忍
# 理由——系統被限流的當下，正是卡死的 work item 最需要被解開的時候。
# #731：`refreeze-base` 同理——它只讀 run 自身狀態與來源樹的 git ref，一個位元組
# 都不取自 issue 的當下 open/closed。
_LOCAL_UNBLOCK_ACTIONS = _RETIREMENT_ACTIONS | {"reset-reclaim-budget", "refreeze-base"}


def _positive_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _absolute_file(value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{field} must be a regular non-symlink file")
    return path


def _json_file(value: object, *, field: str) -> object:
    path = _absolute_file(value, field=field)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field} is unreadable") from exc


def _pr_metadata(args: dict[str, Any], *, required_issues: tuple[int, ...]) -> PullRequestMetadata:
    if not required_issues:
        raise RuntimeError("ship requires at least one mapped issue")
    payload = _json_file(args.get("pr_metadata_path"), field="pr_metadata_path")
    if not isinstance(payload, dict) or set(payload) != {"title", "body", "labels"}:
        raise ValueError("PR metadata malformed")
    labels = payload.get("labels")
    if (
        not isinstance(payload.get("title"), str)
        or not isinstance(payload.get("body"), str)
        or not isinstance(labels, list)
    ):
        raise ValueError("PR metadata labels malformed")
    metadata = PullRequestMetadata(
        title=payload.get("title"),
        body=payload.get("body"),
        labels=tuple(labels),
    )
    gate = validate_pr_metadata(metadata=metadata, required_issues=required_issues)
    if not gate.allowed:
        raise RuntimeError(f"PR metadata blocked: {', '.join(gate.reasons)}")
    return metadata


def _ensure_openspec_change_scaffold(*, repo_root: Path, change: str) -> None:
    """#776：archive 前補齊舊 manifest 世代缺失的 openspec change 結構。

    舊版 combo manifest 的 planning 只把 ``tasks.md`` 寫進 candidate（實機
    workflow-85114100），archive gate 的 ``openspec validate <change> --type
    change --strict`` 因缺
    ``proposal.md``／specs delta 而結構性失敗（``Unknown item``）。Manager 在
    自己的 ship workspace 於 gate 之前補齊 scaffold——內容為指標式：canonical
    規格仍是 ``docs/superpowers/specs/`` 的 spec/design（與 repo 既有 proposal
    的 Capabilities 段慣例一致），delta 以單一 Requirement 錨定「以 canonical
    規格驗收」。scaffold 隨 archive commit 一起收進、candidate 前進並經
    re-verification 檢視（#653 設計本有的重驗步驟）。已有 proposal／delta 的
    change 一字不動；產出 deterministic（無時間戳）。
    """

    change_dir = repo_root / "openspec" / "changes" / change
    if not change_dir.is_dir():
        return
    spec_ref = f"docs/superpowers/specs/{change}-spec.md"
    design_ref = f"docs/superpowers/specs/{change}-design.md"
    proposal = change_dir / "proposal.md"
    if not proposal.is_file():
        proposal.write_text(
            "---\n"
            "status: accepted\n"
            f"work_item: {change}\n"
            "---\n"
            "\n"
            "## Goals\n"
            "\n"
            f"本 change 的 canonical 目標與需求載於 `{spec_ref}`。\n"
            "\n"
            "## Why\n"
            "\n"
            f"設計取捨與動機載於 `{design_ref}`。\n"
            "\n"
            "## What Changes\n"
            "\n"
            f"- 依 `{spec_ref}` 的 Requirements 實作並驗收（本 proposal 由 Manager 於 archive 前補齊結構，canonical 規格不在此重複）。\n"
            "\n"
            "## Capabilities\n"
            "\n"
            "### Modified Capabilities\n"
            f"- 詳見 `{spec_ref}` 與 `{design_ref}`。\n",
            encoding="utf-8",
        )
    specs_dir = change_dir / "specs"
    has_delta = specs_dir.is_dir() and any(specs_dir.rglob("*.md"))
    if not has_delta:
        delta_dir = specs_dir / change
        delta_dir.mkdir(parents=True, exist_ok=True)
        (delta_dir / "spec.md").write_text(
            "## ADDED Requirements\n"
            "\n"
            f"### Requirement: 依 canonical superpowers 規格驗收\n"
            "\n"
            f"本 change 的 canonical Requirements 載於 `{spec_ref}`；candidate MUST 滿足該規格的全部驗收條件，且 verify／review 以該規格為唯一需求來源。\n"
            "\n"
            "#### Scenario: canonical 規格驗收\n"
            "\n"
            f"- **WHEN** 依 `{spec_ref}` 的 Requirements 對 candidate 驗收\n"
            "- **THEN** 全部驗收條件成立\n",
            encoding="utf-8",
        )


def _validate_local_archive_inputs(
    *,
    repo_root: Path,
    change: str,
    runner: Runner,
) -> None:
    tasks_path = repo_root / "openspec" / "changes" / change / "tasks.md"
    try:
        tasks_text = tasks_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("OpenSpec tasks unavailable") from exc
    task_states = re.findall(r"(?m)^\s*[-*]\s+\[([ xX])\]\s+", tasks_text)
    canonical = runner(
        build_openspec_validate_change_argv(change),
        cwd=str(repo_root),
        shell=False,
        capture_output=True,
        text=True,
    )
    policy = runner(
        ["python3", "-m", "policy_check", "--repo", "."],
        cwd=str(repo_root),
        shell=False,
        capture_output=True,
        text=True,
    )
    try:
        changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        changelog = ""
    unreleased_match = re.search(
        r"(?ms)^## \[Unreleased\]\s*(.*?)(?=^## |\Z)", changelog
    )
    unreleased = unreleased_match.group(1) if unreleased_match else ""
    fragments = {
        repo_root / directory / f"{change}.md"
        for directory in ("changelog.d", "changes")
    }
    fragment_dir = repo_root / "changelog.d"
    if not fragment_dir.is_symlink() and fragment_dir.is_dir():
        fragments.update(fragment_dir.glob("*.md"))
    change_token = re.compile(
        rf"(?i)(?<![a-z0-9-]){re.escape(change)}(?![a-z0-9-])"
    )
    fragment_present = False
    for path in sorted(fragments):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            fragment_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if fragment_text.strip() and (
            path.name == f"{change}.md"
            or path.name.endswith(f"-{change}.md")
            or change_token.search(fragment_text)
        ):
            fragment_present = True
            break
    facts = ArchiveGateFacts(
        tasks_complete=bool(task_states) and all(state.lower() == "x" for state in task_states),
        canonical_specs_valid=getattr(canonical, "returncode", None) == 0,
        doc_references_valid=getattr(policy, "returncode", None) == 0,
        changelog_present=(
            re.search(
                rf"(?im)^\s*[-*]\s+.*(?<![a-z0-9-]){re.escape(change)}(?![a-z0-9-]).*$",
                unreleased,
            )
            is not None
            or fragment_present
        ),
    )
    gate = validate_archive_gate(facts)
    if not gate.allowed:
        raise RuntimeError(f"archive gate blocked: {', '.join(gate.reasons)}")


def _repo_identity(repo: object) -> str:
    if not isinstance(repo, str) or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError("repo must be canonical owner/name")
    return repo


def _remote_repo(value: str) -> str | None:
    patterns = (
        r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
        r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
        r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value.strip())
        if match is not None:
            return match.group(1)
    return None


def _canonical_repo_root(value: object, *, repo: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("repo_root must be absolute")
    raw = Path(value)
    try:
        root = raw.resolve(strict=True)
    except OSError as exc:
        raise ValueError("repo_root unavailable") from exc
    if raw.is_symlink() or raw.absolute() != root or not root.is_dir():
        raise ValueError("repo_root must be a real non-symlink directory")
    top_level = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if top_level.returncode != 0 or not isinstance(top_level.stdout, str):
        raise ValueError("repo_root canonical git top-level unavailable")
    try:
        canonical_top_level = Path(top_level.stdout.strip()).resolve(strict=True)
    except OSError as exc:
        raise ValueError("repo_root canonical git top-level unavailable") from exc
    if canonical_top_level != root:
        raise ValueError("repo_root must equal canonical git top-level")
    completed = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        raise ValueError("repo_root canonical origin remote unavailable")
    remote_repo = _remote_repo(completed.stdout)
    if remote_repo != repo:
        raise ValueError("repo_root origin remote must match requested repo")
    return root


# #246：例外摘要必須可安全外流——OSError 子類（FileNotFoundError／PermissionError
# 等）的預設字串會內嵌絕對路徑，而 blocked 結果與 daemon summary 都會被寫進 durable
# done record 與 log。這裡只保留型別名與清洗過的訊息：POSIX 絕對路徑與 ~ 展開路徑
# 一律以 <path> 取代（R-21 tier: shareable，AI-SEC-001 禁止輸出個人絕對路徑）。
_ABSOLUTE_PATH_RE = re.compile(r"(?:~|/)[^\s'\"]*/[^\s'\"]*")


def safe_exception_summary(exc: BaseException) -> str:
    """把例外轉成不含絕對路徑的簡短摘要，供 durable 記錄與日誌使用。"""
    return f"{type(exc).__name__}: {_ABSOLUTE_PATH_RE.sub('<path>', str(exc))}"


def _path_has_symlink(root: Path, relative: str) -> bool:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def _override_path(args: dict[str, Any], *, repo: str) -> Path:
    root = resolve_trusted_repo_root(repo, explicit=args.get("repo_root"))
    cortex_dir = root / ".cortex"
    if cortex_dir.is_symlink():
        raise ValueError("repo_root .cortex must not be a symlink")
    target = cortex_dir / "work-items.yaml"
    if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
        raise ValueError("work override path escapes repo_root")
    return target


def _canonical_source(*, args: dict[str, Any], repo: str) -> dict[str, str]:
    issue = args.get("issue")
    kind = args.get("kind")
    ref = args.get("ref")
    legacy = isinstance(issue, int) and not isinstance(issue, bool) and issue > 0
    typed = isinstance(kind, str) or ref is not None
    if legacy and typed:
        raise ValueError("link/unlink issue conflicts with kind/ref")
    if legacy:
        return {"kind": "github_issue", "ref": f"{repo}#{issue}"}
    if kind not in {"github_issue", "github_pr", "openspec", "path"}:
        raise ValueError("link/unlink kind invalid")
    if not isinstance(ref, str) or not ref:
        raise ValueError("link/unlink ref required")
    if kind in {"github_issue", "github_pr"}:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)", ref)
        if match is None or match.group(1) != repo:
            raise ValueError(f"{kind} ref must be canonical and match repo")
    elif kind == "openspec":
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", ref) is None:
            raise ValueError("openspec ref must be a safe slug")
    else:
        pure = PurePosixPath(ref)
        if pure.is_absolute() or ".." in pure.parts or ref != pure.as_posix() or ref in {"", "."}:
            raise ValueError("path ref must be canonical repo-relative path")
    return {"kind": kind, "ref": ref}


def _write_override(path: Path, payload: dict[str, Any]) -> None:
    lines = ["version: 1", "work_items:"]
    for work_id, row in payload["work_items"].items():
        lines.extend([f"  {work_id}:", f"    title: {row['title']!r}"])
        for field in ("links", "excludes"):
            lines.append(f"    {field}:")
            values = row.get(field, [])
            if not values:
                lines[-1] += " []"
            else:
                for value in values:
                    lines.extend(
                        [
                            f"      - kind: {value['kind']}",
                            f"        ref: {value['ref']!r}",
                        ]
                    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("work override path must not use symlinks")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _validate_override_payload(payload: object, *, repo: str) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or isinstance(payload.get("version"), bool)
        or set(payload) != {"version", "work_items"}
        or not isinstance(payload.get("work_items"), dict)
    ):
        raise ValueError("work override malformed")
    for work_id, row in payload["work_items"].items():
        if (
            not isinstance(work_id, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None
            or not isinstance(row, dict)
            or set(row) != {"title", "links", "excludes"}
            or not isinstance(row.get("title"), str)
            or not row["title"].strip()
        ):
            raise ValueError("work override row malformed")
        for field in ("links", "excludes"):
            values = row.get(field)
            if not isinstance(values, list):
                raise ValueError("work override source list malformed")
            canonical: list[dict[str, str]] = []
            for value in values:
                if not isinstance(value, dict) or set(value) != {"kind", "ref"}:
                    raise ValueError("work override source malformed")
                canonical.append(
                    _canonical_source(
                        args={"kind": value["kind"], "ref": value["ref"]},
                        repo=repo,
                    )
                )
            if len({(value["kind"], value["ref"]) for value in canonical}) != len(canonical):
                raise ValueError("work override source duplicated")
        linked = {(value["kind"], value["ref"]) for value in row["links"]}
        excluded = {(value["kind"], value["ref"]) for value in row["excludes"]}
        if linked & excluded:
            raise ValueError("work override source cannot be linked and excluded")
    return payload


def _mutate_override(*, args: dict[str, Any], repo: str, work_id: str) -> dict[str, Any]:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise ValueError("work_id invalid")
    source = _canonical_source(args=args, repo=repo)
    path = _override_path(args, repo=repo)
    if source["kind"] == "path" and _path_has_symlink(path.parent.parent, source["ref"]):
        raise ValueError("path ref must not traverse a symlink")
    if path.exists():
        payload = safe_load(path.read_text(encoding="utf-8"))
    else:
        payload = {"version": 1, "work_items": {}}
    payload = _validate_override_payload(payload, repo=repo)
    row = payload["work_items"].setdefault(
        work_id,
        {"title": args.get("title") or work_id, "links": [], "excludes": []},
    )
    if not isinstance(row, dict):
        raise ValueError("work override row malformed")
    row.setdefault("title", work_id)
    row.setdefault("links", [])
    row.setdefault("excludes", [])
    ref = source
    if args["action"] == "link":
        if ref not in row["links"]:
            row["links"].append(ref)
        row["excludes"] = [value for value in row["excludes"] if value != ref]
    else:
        row["links"] = [value for value in row["links"] if value != ref]
        if ref not in row["excludes"]:
            row["excludes"].append(ref)
    _validate_override_payload(payload, repo=repo)
    _write_override(path, payload)
    from paulsha_cortex.monitor.correlation import load_work_item_overrides
    try:
        loaded = load_work_item_overrides(path.parent.parent)
        if work_id not in loaded.work_items:
            raise ValueError(f"work override readback failed: {work_id} missing after write")
    except Exception as exc:
        raise ValueError(f"work override readback failed: {exc}") from exc
    return {"action": args["action"], "override_path": str(path), "source": ref}


def _source_already_authorized(authority, source: dict[str, str]) -> bool:
    """issue #203：intake 判斷『這個 source 是否已經在受監控快照中被授權』，
    用來決定要不要再多寫一筆 override link。只認 WorkAuthority 已載入的
    mapped_* 欄位——這些欄位是 monitor correlation 的產出，不是 override
    檔本身（override 檔寫入後仍要等下一輪 monitor 收斂才會反映到快照，見
    ``_intake_action`` docstring）。
    """
    kind = source["kind"]
    ref = source["ref"]
    if kind in {"github_issue", "github_pr"}:
        match = re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#([1-9][0-9]*)", ref)
        if match is None:
            return False
        number = int(match.group(1))
        pool = authority.mapped_issues if kind == "github_issue" else authority.mapped_prs
        return number in pool
    if kind == "openspec":
        return ref in authority.mapped_openspec
    if kind == "path":
        return ref in authority.mapped_todo_paths
    return False


def _intake_action(
    *,
    args: dict[str, Any],
    authority,
    requested_by: str,
    now_epoch: float,
    state_path: Path,
    snapshot_path: str | Path | None,
    workflow_registry=None,
    workflow_starter=None,
    readiness_checker=None,
) -> dict[str, Any]:
    """issue #203：把「（必要時）link + start」合成單一 work-action，取代已
    停用的低階 dispatch，作為『拿到一個 issue/task 就進件』的單一入口。

    Intake 絕不能無中生有出新的 authority——呼叫端傳進來的 ``authority`` 已由
    ``execute_work_action`` 透過既有 ``load_work_authority`` 載入（要求
    ``(repo, work_id)`` 這一列本來就存在於受監控的權威快照裡），這裡只做兩
    件事：(a) 若帶了 issue/kind+ref 且尚未反映在目前快照的 mapped_* 欄位，
    寫一筆 override link 供下一輪 monitor correlation 採信；(b) 驗證『重新
    載入後』的 authority 確實有至少一項明文授權的來源（confirmed_todo 或
    mapped_issues/mapped_todo_paths），再原樣轉交 ``_claim_action``（start
    語意）。

    寫 override 這一步是非同步的：``.cortex/work-items.yaml`` 與受監控的
    ``work-items.snapshot.json`` 是兩份分開維護的狀態，前者要等 monitor 下一
    輪 correlation 才會併入後者（見 ``paulsha_cortex/monitor/correlation.py``
    與 ``coordinator/claim.py`` 的 ``_load_snapshot``）。因此「首次連結就地
    開工」在同一次呼叫內不保證成立——若重新載入後 authority 仍未授權該來
    源，這裡 fail-closed 拒絕（不假裝已生效），而不是靜默略過驗證。這是刻意
    的簡化決策：intake 不引入額外的可續傳（resumable）分段設計，只做「link
    （若需要）→ reload → 驗證 → claim」這一條直路。
    """
    repo = authority.repo
    work_id = authority.work_id
    issue = args.get("issue")
    kind = args.get("kind")
    ref = args.get("ref")
    provided_link_args = issue is not None or kind is not None or ref is not None
    linked = False
    link_result: dict[str, Any] | None = None
    if provided_link_args:
        source = _canonical_source(args=args, repo=repo)
        if not _source_already_authorized(authority, source):
            link_args = dict(args)
            link_args["action"] = "link"
            link_result = _mutate_override(args=link_args, repo=repo, work_id=work_id)
            linked = True
            authority = load_work_authority(
                repo=repo, work_id=work_id, snapshot_path=snapshot_path
            )
    if not (authority.mapped_issues or authority.mapped_todo_paths or authority.confirmed_todo):
        raise ValueError(
            "work-action intake 需要 confirmed Todo 或已 link 的 issue/kind+ref，"
            "或在同一次呼叫帶 issue/kind+ref 建立 link；純文字任務不得憑空建立 authority"
        )
    claim_result = _claim_action(
        args=args,
        authority=authority,
        requested_by=requested_by,
        now_epoch=now_epoch,
        state_path=state_path,
        workflow_registry=workflow_registry,
        workflow_starter=workflow_starter,
        readiness_checker=readiness_checker,
    )
    # run 保留在頂層（而非巢狀於某個 "claim_result" 鍵下）：manager_daemon 的
    # job 派工觸發（`result["result"]["run"]`）與 start/resume 共用同一個讀取
    # 路徑，intake 必須維持相同形狀才能被同一段 dispatch 邏輯接住。
    result = dict(claim_result)
    result["linked"] = linked
    result["link_result"] = link_result
    return result


def _run_state_path() -> Path:
    return paths.coordinator_root() / "delivery-journal.json"


_DELIVERY_JOURNAL_SCHEMA = "cortex-delivery-journal/v1"
_DELIVERY_JOURNAL_BASELINE_FIELD = "_delivery_journal_baseline"
_DELIVERY_JOURNAL_REVISION_FIELD = "revision"
_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD = "publication_events"
_DELIVERY_JOURNAL_LOCK_SUFFIX = ".lock"
_DELIVERY_JOURNAL_LOCK_TIMEOUT_SECONDS = 5.0
_DELIVERY_JOURNAL_LOCK_RETRY_SECONDS = 0.01
_DELIVERY_JOURNAL_TEST_HOOK: Callable[..., None] | None = None


class _DeliveryJournalConflict(RuntimeError):
    """The caller must reload and recompute against the latest durable journal."""


class _DeliveryJournalUnknown(RuntimeError):
    """The write may have become visible, but this call could not prove it."""


def _delivery_journal_test_hook(stage: str, **context: object) -> None:
    hook = _DELIVERY_JOURNAL_TEST_HOOK
    if hook is not None:
        hook(stage=stage, **context)


def _delivery_journal_text(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > 200
        or "\n" in value
        or not value.isprintable()
    ):
        raise ValueError(f"{field} malformed")
    return value


def _delivery_journal_revision(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} malformed")
    return value


def _delivery_journal_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _delivery_journal_canonical_payload(
    value: object, *, field: str
) -> tuple[object, str]:
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} malformed") from exc
    return json.loads(canonical), _delivery_journal_digest(canonical.encode("utf-8"))


def _delivery_journal_entry(
    value: object, *, field: str
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"payload", "payload_sha256"}
        or not isinstance(value.get("payload_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["payload_sha256"]) is None
    ):
        raise ValueError(f"{field} malformed")
    payload, digest = _delivery_journal_canonical_payload(
        value.get("payload"), field=field
    )
    if digest != value["payload_sha256"]:
        raise ValueError(f"{field} malformed")
    return {"payload": payload, "payload_sha256": digest}


def _delivery_journal_publication_events(
    value: object, *, field: str
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} malformed")
    normalized: dict[str, dict[str, Any]] = {}
    for event_id, row in value.items():
        normalized_id = _delivery_journal_text(
            event_id, field=f"{field}[event_id]"
        )
        if (
            not isinstance(row, dict)
            or set(row) not in (
                {"event_id", "kind", "intent"},
                {"event_id", "kind", "intent", "result"},
            )
            or row.get("event_id") != normalized_id
        ):
            raise ValueError(f"{field} malformed")
        normalized_row = {
            "event_id": normalized_id,
            "kind": _delivery_journal_text(
                row.get("kind"), field=f"{field}[{normalized_id}].kind"
            ),
            "intent": _delivery_journal_entry(
                row.get("intent"), field=f"{field}[{normalized_id}].intent"
            ),
        }
        if "result" in row:
            normalized_row["result"] = _delivery_journal_entry(
                row.get("result"), field=f"{field}[{normalized_id}].result"
            )
        normalized[normalized_id] = normalized_row
    return normalized


def _delivery_journal_publication_index(
    runs: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        run_id: row[_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD]
        for run_id, row in runs.items()
        if row.get(_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD)
    }


def _delivery_journal_payload(
    payload: object, *, revision: int
) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != _DELIVERY_JOURNAL_SCHEMA
        or not isinstance(payload.get("runs"), dict)
    ):
        raise ValueError("work run state malformed")
    if _DELIVERY_JOURNAL_REVISION_FIELD in payload:
        _delivery_journal_revision(
            payload.get(_DELIVERY_JOURNAL_REVISION_FIELD),
            field="work run state revision",
        )
    normalized = copy.deepcopy(payload)
    normalized.pop(_DELIVERY_JOURNAL_BASELINE_FIELD, None)
    normalized[_DELIVERY_JOURNAL_REVISION_FIELD] = revision
    for key, row in normalized["runs"].items():
        if (
            not isinstance(key, str)
            or not isinstance(row, dict)
            or row.get("run_id") != key
            or not isinstance(row.get("run_id"), str)
            or not isinstance(row.get("claim_key"), str)
            or not isinstance(row.get("snapshot_hash"), str)
            or not isinstance(row.get("source_revisions"), list)
            or not isinstance(row.get("provider_revision"), str)
            or not isinstance(row.get("authority_digest"), str)
            or not isinstance(row.get("workflow_step_ids"), list)
        ):
            raise ValueError("work run record malformed")
        if _DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD in row:
            events = _delivery_journal_publication_events(
                row[_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD],
                field=f"work run record {key} publication events",
            )
            if events:
                row[_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD] = events
            else:
                row.pop(_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD, None)
        else:
            row.pop(_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD, None)
    return normalized


def _delivery_journal_logical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    logical = copy.deepcopy(payload)
    logical.pop(_DELIVERY_JOURNAL_REVISION_FIELD, None)
    return logical


def _delivery_journal_baseline(
    *, exists: bool, revision: int, content_sha256: str | None
) -> dict[str, Any]:
    return {
        "exists": exists,
        "revision": revision,
        "content_sha256": content_sha256,
    }


def _validated_delivery_journal_baseline(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "exists",
        "revision",
        "content_sha256",
    }:
        raise ValueError("work run state baseline missing")
    exists = value.get("exists")
    revision = _delivery_journal_revision(
        value.get("revision"), field="work run state baseline"
    )
    digest = value.get("content_sha256")
    if not isinstance(exists, bool):
        raise ValueError("work run state baseline missing")
    if exists:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("work run state baseline missing")
    elif digest is not None:
        raise ValueError("work run state baseline missing")
    return _delivery_journal_baseline(
        exists=exists,
        revision=revision,
        content_sha256=digest,
    )


def _refresh_delivery_journal_baseline(
    payload: dict[str, Any], *, exists: bool, revision: int, content_sha256: str | None
) -> None:
    payload[_DELIVERY_JOURNAL_REVISION_FIELD] = revision
    payload[_DELIVERY_JOURNAL_BASELINE_FIELD] = _delivery_journal_baseline(
        exists=exists,
        revision=revision,
        content_sha256=content_sha256,
    )


def _delivery_journal_snapshot(path: Path) -> SimpleNamespace:
    if path.is_symlink():
        raise ValueError("work run state unreadable")
    if not path.exists():
        payload = _delivery_journal_payload(
            {
                "schema": _DELIVERY_JOURNAL_SCHEMA,
                "runs": {},
            },
            revision=0,
        )
        return SimpleNamespace(
            payload=payload,
            exists=False,
            revision=0,
            content_sha256=None,
        )
    if not path.is_file():
        raise ValueError("work run state unreadable")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("work run state unreadable") from exc
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("work run state unreadable") from exc
    revision = _delivery_journal_revision(
        payload.get(_DELIVERY_JOURNAL_REVISION_FIELD, 0),
        field="work run state revision",
    )
    normalized = _delivery_journal_payload(payload, revision=revision)
    return SimpleNamespace(
        payload=normalized,
        exists=True,
        revision=revision,
        content_sha256=_delivery_journal_digest(raw),
    )


def _delivery_journal_lock_path(path: Path) -> Path:
    return path.parent / f"{path.name}{_DELIVERY_JOURNAL_LOCK_SUFFIX}"


def _delivery_journal_lock_fd(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _delivery_journal_lock_path(path)
    if lock_path.is_symlink():
        raise RuntimeError("work run state lock path invalid")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("work run state lock unavailable") from exc
    try:
        if not statmod.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("work run state lock path invalid")
        return fd
    except BaseException:
        os.close(fd)
        raise


class _DeliveryJournalLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> None:
        fd = _delivery_journal_lock_fd(self._path)
        deadline = time.monotonic() + _DELIVERY_JOURNAL_LOCK_TIMEOUT_SECONDS
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("work run state lock timeout")
                    time.sleep(_DELIVERY_JOURNAL_LOCK_RETRY_SECONDS)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._fd is not None
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
        return None


def _write_delivery_journal_candidate(
    path: Path, candidate_payload: dict[str, Any]
) -> SimpleNamespace:
    try:
        content = (
            json.dumps(
                candidate_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("work run state malformed") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            _delivery_journal_test_hook(
                "before-file-fsync", path=path, candidate=candidate_payload
            )
            os.fsync(handle.fileno())
        _delivery_journal_test_hook(
            "after-file-fsync", path=path, candidate=candidate_payload
        )
        os.replace(temporary, path)
        _delivery_journal_test_hook(
            "after-replace", path=path, candidate=candidate_payload
        )
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _delivery_journal_test_hook(
            "after-directory-fsync", path=path, candidate=candidate_payload
        )
        confirmed = _delivery_journal_snapshot(path)
        if (
            confirmed.revision != candidate_payload[_DELIVERY_JOURNAL_REVISION_FIELD]
            or _delivery_journal_logical_payload(confirmed.payload)
            != _delivery_journal_logical_payload(candidate_payload)
        ):
            raise RuntimeError("work run state confirmation drift")
        _delivery_journal_test_hook(
            "after-readback",
            path=path,
            candidate=candidate_payload,
            confirmed=confirmed.payload,
        )
        return confirmed
    except Exception as exc:
        # KeyboardInterrupt／SystemExit 等控制流程照原樣傳遞，不改寫成 unknown outcome。
        raise _DeliveryJournalUnknown("work run state outcome unknown") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _save_runs_result(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    baseline = _validated_delivery_journal_baseline(
        payload.get(_DELIVERY_JOURNAL_BASELINE_FIELD)
    )
    with _DeliveryJournalLock(path):
        current = _delivery_journal_snapshot(path)
        if baseline != _delivery_journal_baseline(
            exists=current.exists,
            revision=current.revision,
            content_sha256=current.content_sha256,
        ):
            return {"outcome": "conflict", "reason": "stale-baseline"}
        candidate_logical = _delivery_journal_payload(
            payload, revision=current.revision
        )
        if (
            _delivery_journal_publication_index(candidate_logical["runs"])
            != _delivery_journal_publication_index(current.payload["runs"])
        ):
            return {
                "outcome": "conflict",
                "reason": "publication-entry-conflict",
            }
        if _delivery_journal_logical_payload(candidate_logical) == _delivery_journal_logical_payload(
            current.payload
        ):
            _refresh_delivery_journal_baseline(
                payload,
                exists=current.exists,
                revision=current.revision,
                content_sha256=current.content_sha256,
            )
            return {"outcome": "committed", "revision": current.revision}
        candidate_to_write = copy.deepcopy(candidate_logical)
        candidate_to_write[_DELIVERY_JOURNAL_REVISION_FIELD] = current.revision + 1
        confirmed = _write_delivery_journal_candidate(path, candidate_to_write)
        _refresh_delivery_journal_baseline(
            payload,
            exists=True,
            revision=confirmed.revision,
            content_sha256=confirmed.content_sha256,
        )
        return {"outcome": "committed", "revision": confirmed.revision}


def _load_runs(path: Path) -> dict[str, Any]:
    snapshot = _delivery_journal_snapshot(path)
    payload = snapshot.payload
    _refresh_delivery_journal_baseline(
        payload,
        exists=snapshot.exists,
        revision=snapshot.revision,
        content_sha256=snapshot.content_sha256,
    )
    return payload


def _save_runs(path: Path, payload: dict[str, Any]) -> None:
    result = _save_runs_result(path, payload)
    outcome = result["outcome"]
    if outcome == "committed":
        return None
    if outcome == "conflict":
        raise _DeliveryJournalConflict(
            f"work run state conflict: {result['reason']}"
        )
    raise _DeliveryJournalUnknown(
        f"work run state outcome unknown: {result.get('reason', 'unknown')}"
    )


def _append_delivery_publication_event(
    path: Path,
    *,
    run_id: str,
    event_id: str,
    event_kind: str,
    entry_kind: str,
    payload: object,
) -> dict[str, Any]:
    normalized_run_id = _delivery_journal_text(run_id, field="publication run_id")
    normalized_event_id = _delivery_journal_text(
        event_id, field="publication event_id"
    )
    normalized_event_kind = _delivery_journal_text(
        event_kind, field="publication event kind"
    )
    if entry_kind not in {"intent", "result"}:
        raise ValueError("publication entry kind malformed")
    normalized_payload, payload_sha256 = _delivery_journal_canonical_payload(
        payload, field="publication payload"
    )
    with _DeliveryJournalLock(path):
        current = _delivery_journal_snapshot(path)
        row = current.payload["runs"].get(normalized_run_id)
        if not isinstance(row, dict):
            return {"outcome": "conflict", "reason": "run-missing"}
        existing_events = copy.deepcopy(
            row.get(_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD, {})
        )
        current_event = existing_events.get(normalized_event_id)
        if current_event is not None:
            if current_event["kind"] != normalized_event_kind:
                return {"outcome": "conflict", "reason": "event-kind-conflict"}
            existing_entry = current_event.get(entry_kind)
            if existing_entry is not None:
                if existing_entry == {
                    "payload": normalized_payload,
                    "payload_sha256": payload_sha256,
                }:
                    return {
                        "outcome": "committed",
                        "revision": current.revision,
                        "event_id": normalized_event_id,
                        "entry_kind": entry_kind,
                        "payload_sha256": payload_sha256,
                    }
                return {"outcome": "conflict", "reason": "payload-conflict"}
            if entry_kind == "intent":
                return {"outcome": "conflict", "reason": "intent-conflict"}
            current_event["result"] = {
                "payload": normalized_payload,
                "payload_sha256": payload_sha256,
            }
        else:
            if entry_kind != "intent":
                return {"outcome": "conflict", "reason": "result-before-intent"}
            existing_events[normalized_event_id] = {
                "event_id": normalized_event_id,
                "kind": normalized_event_kind,
                "intent": {
                    "payload": normalized_payload,
                    "payload_sha256": payload_sha256,
                },
            }
        candidate = copy.deepcopy(current.payload)
        candidate_row = candidate["runs"][normalized_run_id]
        candidate_row[_DELIVERY_JOURNAL_PUBLICATION_EVENTS_FIELD] = existing_events
        candidate[_DELIVERY_JOURNAL_REVISION_FIELD] = current.revision + 1
        try:
            confirmed = _write_delivery_journal_candidate(path, candidate)
        except _DeliveryJournalUnknown:
            return {
                "outcome": "unknown",
                "reason": "write-unconfirmed",
                "event_id": normalized_event_id,
                "entry_kind": entry_kind,
                "payload_sha256": payload_sha256,
            }
        return {
            "outcome": "committed",
            "revision": confirmed.revision,
            "event_id": normalized_event_id,
            "entry_kind": entry_kind,
            "payload_sha256": payload_sha256,
        }


def _canonical_workflow_run(*, workflow_registry, authority):
    digest = work_authority_digest(authority)
    matches = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id == authority.work_id
        and run.source_revision == digest
        and run.issue_refs
        == tuple(f"{authority.repo}#{number}" for number in authority.mapped_issues)
        # A workflow's planning cards may create the active OpenSpec change
        # after the claim was persisted.  In that case the run legitimately
        # carries no OpenSpec ref yet, while the current authority does.  Use
        # the same compatibility rule as resume/claim so delivery and recovery
        # bind that run instead of treating its own planning output as a new
        # identity.
        and _openspec_refs_compatible(run, authority)
        and run.pr_refs
        == tuple(f"{authority.repo}#{number}" for number in authority.mapped_prs)
    ]
    # A re-claimed work item legitimately leaves superseded historical runs
    # with the same authority refs.  Delivery must bind to the one live run;
    # counting those audit records as competing canonical runs deadlocks ship
    # as soon as a retry/reclaim has occurred.  Multiple live runs remain a
    # hard failure rather than an arbitrary choice.
    active = [run for run in matches if run.status == "ongoing"]
    if len(active) != 1:
        raise RuntimeError(
            "delivery WorkflowRun does not match current WorkAuthority"
        )
    return active[0]


def _delivery_journal_row(run, authority) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "claim_key": run.claim_key,
        "repo": authority.repo,
        "work_id": authority.work_id,
        "source_revisions": list(authority.source_revisions),
        "snapshot_hash": authority.snapshot_hash,
        "provider_revision": authority.github_provider_revision,
        "authority_digest": work_authority_digest(authority),
        "mapped_issues": list(authority.mapped_issues),
        "mapped_prs": list(authority.mapped_prs),
        "mapped_openspec": list(authority.mapped_openspec),
        "mapped_todo_paths": list(authority.mapped_todo_paths),
        "workflow_step_ids": [
            f"{run.run_id}:{step.phase}:{step.card}" for step in run.steps
        ],
    }


def _load_work_run(
    *, state_path: Path, workflow_registry, authority
) -> tuple[dict[str, Any], dict[str, Any], object]:
    run = _canonical_workflow_run(
        workflow_registry=workflow_registry,
        authority=authority,
    )
    state = _load_runs(state_path)
    active = state["runs"].get(run.run_id)
    if active is None:
        active = _delivery_journal_row(run, authority)
        state["runs"][run.run_id] = active
        _save_runs(state_path, state)
    else:
        provenance = {
            "source_revisions": list(authority.source_revisions),
            "snapshot_hash": authority.snapshot_hash,
            "provider_revision": authority.github_provider_revision,
        }
        if any(active.get(field) != value for field, value in provenance.items()):
            active.update(provenance)
            _save_runs(state_path, state)
    return state, active, run


# #216 AC4：work_bridge.start_canonical_workflow（#217）在 source-owner transfer
# 尚未完成時 raise 的訊息前綴；_claim_action 靠它辨識並轉成結構化 blocked 結果。
_SOURCE_OWNER_CONFLICT_PREFIX = "source-owner transfer incomplete"


def _expected_claim_key(authority) -> str:
    return build_claim_key(
        ClaimCandidate(
            authority=authority,
            repo=authority.repo,
            work_id=authority.work_id,
            source_revisions=authority.source_revisions,
            confirmed_todo=authority.confirmed_todo,
            confirmed_issue=authority.mapped_issues[0] if authority.mapped_issues else None,
            auto_label=False,
            active_run_id=None,
            active_claim_key=None,
        )
    )


def _validate_current_run_authority(active: dict[str, Any], authority, canonical_run) -> None:
    expected = {
        "claim_key": canonical_run.claim_key,
        "source_revisions": list(authority.source_revisions),
        "authority_digest": work_authority_digest(authority),
        "mapped_issues": list(authority.mapped_issues),
        "mapped_prs": list(authority.mapped_prs),
        "mapped_openspec": list(authority.mapped_openspec),
        "mapped_todo_paths": list(authority.mapped_todo_paths),
    }
    if any(active.get(field) != value for field, value in expected.items()):
        raise RuntimeError("persisted workflow does not match current WorkAuthority")
    step_ids = active.get("workflow_step_ids")
    if (
        not isinstance(active.get("run_id"), str)
        or not isinstance(step_ids, list)
        or not step_ids
        or any(not isinstance(step_id, str) or not step_id for step_id in step_ids)
        or len(set(step_ids)) != len(step_ids)
    ):
        raise ValueError("persisted workflow step identity malformed")


def _ship_binding(args: dict[str, Any], authority) -> dict[str, Any]:
    pr_number = _positive_int(args.get("pr_number"), field="pr_number")
    change = args.get("change")
    todo_paths = args.get("todo_paths")
    if pr_number not in authority.mapped_prs:
        raise RuntimeError("ship PR is not authorized by WorkAuthority")
    if authority.mapped_openspec:
        if not isinstance(change, str) or change not in authority.mapped_openspec:
            raise RuntimeError("ship OpenSpec change is not authorized by WorkAuthority")
    elif change is not None:
        raise RuntimeError("ship OpenSpec change is not authorized by WorkAuthority")
    if (
        not isinstance(todo_paths, list)
        or any(not isinstance(path, str) or not path for path in todo_paths)
        or tuple(sorted(todo_paths)) != authority.mapped_todo_paths
        or len(set(todo_paths)) != len(todo_paths)
    ):
        raise RuntimeError("ship Todo refs are not exactly authorized by WorkAuthority")
    return {
        "pr_number": pr_number,
        "change": change,
        "todo_paths": list(authority.mapped_todo_paths),
    }


_COPILOT_REARM_PERMIT_SCHEMA = "cortex-copilot-review-rearm-permit/v1"
_DELIVERY_REVIEW_EPOCH_SCHEMA = "cortex-delivery-review-epoch/v1"


def _exact_verified_candidate_head(run) -> str | None:
    candidate_head = getattr(run, "candidate_head", None)
    verified_head = getattr(run, "verified_head", None)
    if (
        not isinstance(candidate_head, str)
        or verification.SAFE_SHA_RE.fullmatch(candidate_head) is None
        or not isinstance(verified_head, str)
        or verification.SAFE_SHA_RE.fullmatch(verified_head) is None
        or candidate_head != verified_head
    ):
        return None
    return candidate_head


def _ship_state_hash(ship: dict[str, Any]) -> str:
    return verification.canonical_json_hash(ship)


def _delivery_binding_hash(binding: dict[str, Any]) -> str:
    return verification.canonical_json_hash(binding)


def _validated_copilot_rearm_permit(
    value: object, *, field: str = "copilot review rearm permit"
) -> dict[str, Any]:
    required = {
        "schema",
        "run_id",
        "ship_hash",
        "old_head",
        "candidate_head",
        "authority_digest",
        "delivery_binding_hash",
        "history_prefix_hash",
        "requested_by",
        "transition_id",
        "created_at_epoch",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != _COPILOT_REARM_PERMIT_SCHEMA
        or not isinstance(value.get("run_id"), str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", value["run_id"]) is None
        or any(
            not isinstance(value.get(name), str)
            or verification.SAFE_SHA_RE.fullmatch(value[name]) is None
            for name in ("old_head", "candidate_head")
        )
        or any(
            not isinstance(value.get(name), str)
            or re.fullmatch(r"[0-9a-f]{64}", value[name]) is None
            for name in (
                "ship_hash",
                "authority_digest",
                "delivery_binding_hash",
                "history_prefix_hash",
            )
        )
        or not isinstance(value.get("requested_by"), str)
        or not value["requested_by"].strip()
        or "\n" in value["requested_by"]
        or not isinstance(value.get("transition_id"), str)
        or not value["transition_id"].strip()
        or "\n" in value["transition_id"]
        or not isinstance(value.get("created_at_epoch"), (int, float))
        or isinstance(value.get("created_at_epoch"), bool)
        or not math.isfinite(float(value["created_at_epoch"]))
    ):
        raise ValueError(f"{field} malformed")
    return value


def _copilot_rearm_permit(active: dict[str, Any]) -> dict[str, Any] | None:
    value = active.get("copilot_review_rearm_permit")
    if value is None:
        return None
    return _validated_copilot_rearm_permit(value)


def _delivery_review_epochs(active: dict[str, Any]) -> list[dict[str, Any]]:
    value = active.get("delivery_review_epochs")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("delivery review history malformed")
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"schema", "ship", "ship_hash", "archived_at_epoch", "rearm"}
            or item.get("schema") != _DELIVERY_REVIEW_EPOCH_SCHEMA
            or not isinstance(item.get("ship"), dict)
            or not isinstance(item.get("ship_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["ship_hash"]) is None
            or _ship_state_hash(item["ship"]) != item["ship_hash"]
            or not isinstance(item.get("archived_at_epoch"), (int, float))
            or isinstance(item.get("archived_at_epoch"), bool)
            or not math.isfinite(float(item["archived_at_epoch"]))
        ):
            raise ValueError("delivery review history malformed")
        _validated_copilot_rearm_permit(item.get("rearm"), field="delivery review history")
    return value


def _delivery_review_history_hash(active: dict[str, Any]) -> str:
    return verification.canonical_json_hash(_delivery_review_epochs(active))


def _append_delivery_review_epoch(
    *,
    active: dict[str, Any],
    ship: dict[str, Any],
    permit: dict[str, Any],
    now_epoch: float,
) -> None:
    history = list(_delivery_review_epochs(active))
    archived_ship = json.loads(json.dumps(ship, ensure_ascii=False, sort_keys=True))
    history.append(
        {
            "schema": _DELIVERY_REVIEW_EPOCH_SCHEMA,
            "ship": archived_ship,
            "ship_hash": _ship_state_hash(archived_ship),
            "archived_at_epoch": float(now_epoch),
            "rearm": dict(permit),
        }
    )
    active["delivery_review_epochs"] = history


def _copilot_epoch_rearm_record(permit: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": permit["run_id"],
        "from_head": permit["old_head"],
        "authority_digest": permit["authority_digest"],
        "delivery_binding_hash": permit["delivery_binding_hash"],
        "requested_by": permit["requested_by"],
        "transition_id": permit["transition_id"],
    }


def _invalidate_copilot_rearm_permit(
    *,
    active: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
) -> None:
    if active.pop("copilot_review_rearm_permit", None) is not None:
        _save_runs(state_path, state)


def _pick_adoptable_copilot_review(
    reviews: tuple[object, ...], *, head: str
) -> object | None:
    adoptable = [
        review
        for review in reviews
        if review.commit_id == head
        and review.author == COPILOT_REVIEWER_LOGIN
        and review.state in {"COMMENTED", "APPROVED"}
        and not review.is_error
    ]
    if not adoptable:
        return None
    return max(adoptable, key=lambda value: (value.submitted_at_epoch, value.review_id))


def _set_copilot_request_outcome_unknown(
    *,
    active: dict[str, Any],
    ship: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    workflow_registry,
    canonical_run,
    authority,
    detail: str,
) -> dict[str, Any]:
    active["ship"] = {
        **ship,
        "phase": "needs_human",
        "reason": "copilot-review-request-outcome-unknown",
    }
    active.pop("copilot_review_rearm_permit", None)
    _save_runs(state_path, state)
    workflow_registry._manager_update_workflow_run(
        canonical_run.run_id,
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=diagnostic_reason(
            "copilot-review-request-outcome-unknown",
            detail,
            source="work_actions._ship_action:copilot-request-outcome-unknown",
            run_id=canonical_run.run_id,
            work_id=canonical_run.work_id,
            head=str(ship.get("head") or ""),
        ),
    )
    return {
        "action": "needs_human",
        "reason": "copilot-review-request-outcome-unknown",
        "next_actions": ["abandon", "review-attest"],
        "next_step_hint": (
            f"cortex work review-attest {canonical_run.work_id} --repo {authority.repo} --actor <operator> --payload <file>"
        ),
    }


def _review_attest_evidence_refs(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("review-attest evidence_refs malformed")
    normalized: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "ref", "sha256"}:
            raise ValueError("review-attest evidence_refs malformed")
        evidence_path = _absolute_file(
            item.get("ref"), field="review-attest evidence_refs.ref"
        )
        supplied_hash = item.get("sha256")
        if (
            item.get("kind") != "operator-reproduction"
            or not isinstance(supplied_hash, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", supplied_hash) is None
        ):
            raise ValueError("review-attest evidence_refs malformed")
        actual_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        if actual_hash != supplied_hash.lower():
            raise ValueError("review-attest evidence_refs sha256 mismatch")
        normalized.append(
            {
                "kind": "operator-reproduction",
                "ref": str(evidence_path),
                "sha256": actual_hash,
            }
        )
    return [dict(item) for item in _normalize_maintainer_review_evidence_refs(normalized)]


def _merged_awaiting_closure_detail(change: str | None) -> str:
    if change is None:
        return (
            "PR 已在遠端 merge，本地 closeout（Todo／CompletionRecord）"
            "尚未完成，需要人工接手收尾"
        )
    return (
        "PR 已在遠端 merge，本地 closeout（openspec archive／todo 回寫）"
        "尚未完成，需要人工接手收尾"
    )


def _command_result_payload(result: object) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "argv": list(getattr(result, "argv")),
        "returncode": getattr(result, "returncode"),
    }


def _preflight_hash(preflight: object) -> str:
    return verification.canonical_json_hash(
        {
            "passed": getattr(preflight, "passed"),
            "failed_stage": getattr(preflight, "failed_stage"),
            "head": getattr(preflight, "head"),
            "tree_hash": getattr(preflight, "tree_hash"),
            "policy": _command_result_payload(getattr(preflight, "policy")),
            "ci_parity": _command_result_payload(getattr(preflight, "ci_parity")),
        }
    )


def _checks_hash(remote: object) -> str:
    return verification.canonical_json_hash(
        [
            {
                "name": check.name,
                "status": check.status,
                "conclusion": check.conclusion,
            }
            for check in remote.checks
        ]
    )


def _merge_authorization_body(
    *,
    active: dict[str, Any],
    authority,
    binding: dict[str, Any],
    preflight: object,
    remote: object,
    copilot: object | None,
    foreign_review: ForeignReviewEvidence,
    maintainer_review: MaintainerReviewEvidence | None = None,
    superseded_authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_foreign = _validate_foreign_review(
        foreign_review,
        expected_head=preflight.head,
    )
    if verification.canonical_json_hash(normalized_foreign) != foreign_review.expected_hash.lower():
        raise RuntimeError("foreign review evidence hash changed during authorization")
    common = {
        "run_id": active["run_id"],
        "workflow_step_ids": list(active["workflow_step_ids"]),
        "repo": authority.repo,
        "work_id": authority.work_id,
        "authority_digest": work_authority_digest(authority),
        "pr_number": binding["pr_number"],
        "change": binding["change"],
        "todo_paths": list(binding["todo_paths"]),
        "head": preflight.head,
        "tree_hash": preflight.tree_hash,
        "foreign_review_path": foreign_review.path,
        "foreign_review_hash": foreign_review.expected_hash.lower(),
        "preflight_hash": _preflight_hash(preflight),
        "checks_hash": _checks_hash(remote),
    }
    if maintainer_review is not None:
        if copilot is not None:
            raise ValueError("merge authorization review authority is ambiguous")
        body = {
            "schema": "cortex-merge-authorization/v2",
            **common,
            "review_kind": "maintainer-review",
            "review_ref": maintainer_review.path,
            "review_hash": maintainer_review.expected_hash.lower(),
        }
        if superseded_authorization is not None:
            body["superseded_authorization_ref"] = superseded_authorization["path"]
            body["superseded_authorization_hash"] = superseded_authorization["hash"]
        return body
    if superseded_authorization is not None:
        raise ValueError("merge authorization superseded evidence requires maintainer review")
    if copilot is None:
        raise ValueError("merge authorization review authority missing")
    return {
        "schema": "cortex-merge-authorization/v1",
        **common,
        "copilot_requested_at_epoch": copilot.loop.requested_at,
        "copilot_review_id": copilot.review_id,
        "copilot_hash": verification.canonical_json_hash(
            {
                "head": copilot.head,
                "review_id": copilot.review_id,
                "requested_at_epoch": copilot.loop.requested_at,
            }
        ),
    }


def _authorization_record(
    body: dict[str, Any], *, state_path: Path
) -> dict[str, Any]:
    digest = verification.canonical_json_hash(body)
    run_id = body.get("run_id")
    head = body.get("head")
    schema = body.get("schema")
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", run_id) is None
        or not isinstance(head, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", head) is None
    ):
        raise ValueError("merge authorization identity malformed")
    root = state_path.resolve().parent / "evidence" / "merge-authorization"
    root.mkdir(parents=True, exist_ok=True)
    if schema == "cortex-merge-authorization/v1":
        target = root / f"{run_id}-{head.lower()}.json"
    elif schema == "cortex-merge-authorization/v2":
        target = root / f"{run_id}-{head.lower()}-{digest}.json"
    else:
        raise ValueError("merge authorization identity malformed")
    wrapper = {"payload": body, "hash": digest}
    if target.exists():
        if (
            target.is_symlink()
            or target.stat().st_mode & 0o222
            or json.loads(target.read_text(encoding="utf-8")) != wrapper
        ):
            raise RuntimeError("merge authorization evidence conflict")
    else:
        temporary = root / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(wrapper, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or json.loads(target.read_text(encoding="utf-8")) != wrapper:
                    raise RuntimeError("merge authorization evidence conflict")
            os.chmod(target, 0o444)
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"payload": body, "hash": digest, "path": str(target)}


def _authorization_schema(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    payload = value.get("payload")
    schema = payload.get("schema") if isinstance(payload, dict) else None
    return schema if isinstance(schema, str) else None


def _read_immutable_evidence_wrapper(path_value: object) -> tuple[dict[str, Any], str] | None:
    if not isinstance(path_value, str):
        return None
    evidence_path = Path(path_value)
    if (
        not evidence_path.is_absolute()
        or evidence_path.is_symlink()
        or not evidence_path.is_file()
    ):
        return None
    try:
        if evidence_path.stat().st_mode & 0o222:
            return None
        wrapper = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(wrapper, dict) or set(wrapper) != {"payload", "hash"}:
        return None
    payload = wrapper.get("payload")
    digest = wrapper.get("hash")
    if (
        not isinstance(payload, dict)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or verification.canonical_json_hash(payload) != digest
    ):
        return None
    return payload, digest


def _superseded_authorization_valid(body: dict[str, Any]) -> bool:
    superseded_ref = body.get("superseded_authorization_ref")
    superseded_hash = body.get("superseded_authorization_hash")
    if (
        not isinstance(superseded_ref, str)
        or not isinstance(superseded_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", superseded_hash) is None
    ):
        return False
    wrapper = _read_immutable_evidence_wrapper(superseded_ref)
    if wrapper is None:
        return False
    payload, digest = wrapper
    return (
        digest == superseded_hash
        and payload.get("schema") == "cortex-merge-authorization/v1"
        and payload.get("run_id") == body.get("run_id")
        and payload.get("repo") == body.get("repo")
        and payload.get("work_id") == body.get("work_id")
        and payload.get("head") == body.get("head")
        and payload.get("tree_hash") == body.get("tree_hash")
    )


def _authorization_matches(
    value: object,
    *,
    active: dict[str, Any],
    authority,
    binding: dict[str, Any],
    preflight: object,
    remote: object | None = None,
) -> bool:
    if not _authorization_identity_matches(
        value,
        active=active,
        authority=authority,
        binding=binding,
        head=preflight.head,
        tree_hash=preflight.tree_hash,
    ):
        return False
    body = value["payload"]
    return (
        body.get("preflight_hash") == _preflight_hash(preflight)
        and (remote is None or body.get("checks_hash") == _checks_hash(remote))
    )


def _authorization_identity_matches(
    value: object,
    *,
    active: dict[str, Any],
    authority,
    binding: dict[str, Any],
    head: str,
    tree_hash: str,
    terminal_reconciliation: bool = False,
) -> bool:
    if not isinstance(value, dict) or set(value) != {"payload", "hash", "path"}:
        return False
    body = value.get("payload")
    digest = value.get("hash")
    evidence_path = value.get("path")
    common_required = {
        "schema",
        "run_id",
        "workflow_step_ids",
        "repo",
        "work_id",
        "authority_digest",
        "pr_number",
        "change",
        "todo_paths",
        "head",
        "tree_hash",
        "foreign_review_path",
        "foreign_review_hash",
        "preflight_hash",
        "checks_hash",
    }
    if not isinstance(body, dict):
        return False
    body_keys = set(body)
    schema = body.get("schema")
    if schema == "cortex-merge-authorization/v1":
        required_keys = common_required | {
            "copilot_requested_at_epoch",
            "copilot_review_id",
            "copilot_hash",
        }
        if body_keys != required_keys:
            return False
        has_superseded = False
    elif schema == "cortex-merge-authorization/v2":
        required_keys = common_required | {
            "review_kind",
            "review_ref",
            "review_hash",
        }
        superseded_keys = {
            "superseded_authorization_ref",
            "superseded_authorization_hash",
        }
        if body_keys == required_keys:
            has_superseded = False
        elif body_keys == required_keys | superseded_keys:
            has_superseded = True
        else:
            return False
    else:
        return False
    wrapper = _read_immutable_evidence_wrapper(evidence_path)
    if (
        wrapper is None or verification.canonical_json_hash(body) != digest
    ):
        return False
    evidence_payload, evidence_digest = wrapper
    if evidence_payload != body or evidence_digest != digest:
        return False
    common_valid = (
        body.get("run_id") == active.get("run_id")
        and body.get("workflow_step_ids") == active.get("workflow_step_ids")
        and body.get("repo") == authority.repo
        and body.get("work_id") == authority.work_id
        and (
            body.get("authority_digest") == work_authority_digest(authority)
            or (
                terminal_reconciliation
                and isinstance(body.get("authority_digest"), str)
                and re.fullmatch(r"[0-9a-f]{64}", body["authority_digest"])
                is not None
            )
        )
        and body.get("pr_number") == binding["pr_number"]
        and body.get("change") == binding["change"]
        and body.get("todo_paths") == binding["todo_paths"]
        and body.get("head") == head
        and body.get("tree_hash") == tree_hash
        and isinstance(body.get("foreign_review_path"), str)
        and Path(body["foreign_review_path"]).is_absolute()
        and all(
            isinstance(body.get(field), str)
            and re.fullmatch(r"[0-9a-f]{64}", body[field]) is not None
            for field in (
                "foreign_review_hash",
                "preflight_hash",
                "checks_hash",
            )
        )
    )
    if not common_valid:
        return False
    if schema == "cortex-merge-authorization/v1":
        return (
            isinstance(body.get("copilot_review_id"), int)
            and not isinstance(body.get("copilot_review_id"), bool)
            and body["copilot_review_id"] > 0
            and isinstance(body.get("copilot_requested_at_epoch"), (int, float))
            and not isinstance(body.get("copilot_requested_at_epoch"), bool)
            and math.isfinite(float(body["copilot_requested_at_epoch"]))
            and isinstance(body.get("copilot_hash"), str)
            and re.fullmatch(r"[0-9a-f]{64}", body["copilot_hash"]) is not None
        )
    if has_superseded and not _superseded_authorization_valid(body):
        return False
    review_ref = body.get("review_ref")
    if not (
        body.get("review_kind") == "maintainer-review"
        and isinstance(review_ref, str)
        and Path(review_ref).is_absolute()
        and not Path(review_ref).is_symlink()
        and Path(review_ref).is_file()
        and Path(review_ref).stat().st_mode & 0o222 == 0
        and isinstance(body.get("review_hash"), str)
        and re.fullmatch(r"[0-9a-f]{64}", body["review_hash"]) is not None
    ):
        return False
    try:
        review_payload = json.loads(Path(review_ref).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return verification.canonical_json_hash(review_payload) == body["review_hash"]


def _trusted_evidence_refs(authorization: dict[str, Any]) -> tuple[dict[str, str], ...]:
    body = authorization["payload"]
    current_review = (
        {
            "kind": "maintainer-review",
            "ref": body["review_ref"],
            "hash": body["review_hash"],
        }
        if body.get("schema") == "cortex-merge-authorization/v2"
        else {
            "kind": "copilot",
            "ref": f"github-review:{body['copilot_review_id']}",
            "hash": body["copilot_hash"],
        }
    )
    return (
        {
            "kind": "preflight",
            "ref": f"head:{body['head']}:tree:{body['tree_hash']}",
            "hash": body["preflight_hash"],
        },
        {
            "kind": "foreign_review",
            "ref": body["foreign_review_path"],
            "hash": body["foreign_review_hash"],
        },
        current_review,
        {
            "kind": "merge_authorization",
            "ref": authorization["path"],
            "hash": authorization["hash"],
        },
    )


def _maintainer_review_record(body: dict[str, Any], *, state_path: Path) -> dict[str, str]:
    digest = verification.canonical_json_hash(body)
    root = state_path.resolve().parent / "evidence" / "maintainer-review"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{body['run_id']}-{body['candidate']}.json"
    content = (json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != content or target.stat().st_mode & 0o222:
            raise RuntimeError("maintainer review evidence conflict")
    else:
        temporary = root / f".{target.name}.{uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, target)
            os.chmod(target, 0o444)
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except FileExistsError:
            if (
                target.is_symlink()
                or target.read_bytes() != content
                or target.stat().st_mode & 0o222
            ):
                raise RuntimeError("maintainer review evidence conflict")
        finally:
            temporary.unlink(missing_ok=True)
    return {"ref": str(target), "hash": digest}


def _validate_maintainer_review(
    *,
    path: object,
    expected_hash: object,
    run,
    authority,
    pr_number: int | None,
    candidate: str,
) -> dict[str, Any]:
    evidence_path = _absolute_file(path, field="maintainer_review_path")
    if evidence_path.stat().st_mode & 0o222:
        raise ValueError("maintainer review evidence must be immutable")
    try:
        body = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("maintainer review evidence unreadable") from exc
    bound_refs = [ref for ref in run.gate_refs if ref.kind == "maintainer-review"]
    required_fields = {
        "schema",
        "repo",
        "work_id",
        "run_id",
        "authority_digest",
        "pr_number",
        "candidate",
        "actor",
        "requested_by",
        "verdict",
        "summary",
        "findings",
        "reviewed_at_epoch",
    }
    optional_fields = {"evidence_refs"}
    try:
        _normalize_maintainer_review_evidence_refs(
            body.get("evidence_refs") if isinstance(body, dict) else None
        )
    except ValueError as exc:
        raise RuntimeError("maintainer review does not authorize exact HEAD") from exc
    if (
        run.current_phase != "review"
        or run.candidate_head != candidate
        or run.verified_head != candidate
        or len(bound_refs) != 1
        or bound_refs[0].ref != str(evidence_path)
        or bound_refs[0].sha256 != expected_hash
        or not isinstance(body, dict)
        or not required_fields.issubset(body)
        or bool(set(body) - (required_fields | optional_fields))
        or body.get("schema") != "cortex-maintainer-review/v1"
        or body.get("repo") != authority.repo
        or body.get("work_id") != authority.work_id
        or body.get("run_id") != run.run_id
        or body.get("authority_digest")
        != _maintainer_review_authority_digest(
            authority, pr_number=body.get("pr_number")
        )
        or not _maintainer_review_pr_matches(
            body.get("pr_number"),
            expected_pr_number=pr_number,
            authority=authority,
        )
        or body.get("candidate") != candidate
        or body.get("verdict") != "approved"
        or body.get("findings") != []
        or not isinstance(expected_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        or verification.canonical_json_hash(body) != expected_hash
    ):
        raise RuntimeError("maintainer review does not authorize exact HEAD")
    return body


def _review_attest_action(
    *,
    args: dict[str, Any],
    requested_by: str,
    authority,
    runner: Runner,
    now_epoch: float,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    allowed = {
        "action",
        "repo",
        "work_id",
        "actor",
        "verdict",
        "summary",
        "findings",
        "evidence_refs",
    }
    extras = set(args) - allowed
    if extras:
        raise ValueError(f"review-attest rejects caller evidence/input: {sorted(extras)[0]}")
    actor = args.get("actor")
    summary = args.get("summary")
    findings = args.get("findings")
    evidence_refs = _review_attest_evidence_refs(args.get("evidence_refs"))
    if (
        not isinstance(actor, str) or not actor.strip() or len(actor) > 128 or "\n" in actor
        or args.get("verdict") != "approved"
        or not isinstance(summary, str) or not summary.strip() or len(summary) > 4000
        or findings != []
        or not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
    ):
        raise ValueError("review-attest payload invalid")
    _state, active, run = _load_work_run(
        state_path=state_path,
        workflow_registry=workflow_registry,
        authority=authority,
    )
    _validate_current_run_authority(active, authority, run)
    if any(
        step.phase == "review" and step.gate_result == "needs_human"
        for step in run.steps
    ):
        raise RuntimeError(
            "review-attest does not adjudicate review-gate blocking findings; "
            "use retry-review --reason to accept or retry-build --reason to reject"
        )
    foreign = [ref for ref in run.gate_refs if ref.kind == "foreign-review"]
    change = authority.mapped_openspec[0] if len(authority.mapped_openspec) == 1 else None
    if (
        run.current_phase != "review"
        or run.status != "ongoing"
        or not isinstance(run.candidate_head, str)
        or run.verified_head != run.candidate_head
        or len(foreign) != 1
        or len(authority.mapped_prs) > 1
        or len(authority.mapped_openspec) > 1
        or (
            len(authority.mapped_prs) == 1
            and run.pr_refs != (f"{run.repo}#{authority.mapped_prs[0]}",)
        )
        or (
            len(authority.mapped_prs) == 0
            and (run.pr_refs != () or authority.mapped_openspec != ())
        )
    ):
        raise RuntimeError("review-attest requires current exact-HEAD review run")
    pr_number: int | None = authority.mapped_prs[0] if authority.mapped_prs else None
    if pr_number is not None:
        remote = GitHubDeliveryClient(runner=runner).fetch_delivery_facts(
            repo=authority.repo,
            pr_number=pr_number,
            change=change,
        )
        if remote.head != run.candidate_head:
            raise RuntimeError("review-attest PR HEAD mismatch")
    body = {
        "schema": "cortex-maintainer-review/v1",
        "repo": authority.repo,
        "work_id": authority.work_id,
        "run_id": run.run_id,
        "authority_digest": work_authority_digest(authority),
        "pr_number": pr_number,
        "candidate": run.candidate_head,
        "actor": actor.strip(),
        "requested_by": requested_by,
        "verdict": "approved",
        "summary": summary.strip(),
        "findings": [],
        "reviewed_at_epoch": float(now_epoch),
    }
    if evidence_refs:
        body["evidence_refs"] = evidence_refs
    record = _maintainer_review_record(body, state_path=state_path)
    refs = {ref.kind: ref for ref in run.gate_refs if ref.kind != "copilot"}
    refs["maintainer-review"] = GateEvidenceRef(
        "maintainer-review", record["ref"], record["hash"]
    )
    workflow_registry._manager_update_workflow_run(
        run.run_id,
        gate_refs=tuple(
            refs[kind]
            for kind in ("brainstorm", "foreign-review", "maintainer-review")
            if kind in refs
        ),
        facets=tuple(facet for facet in run.facets if facet != "needs_human"),
    )
    return {"action": "review-attested", "head": run.candidate_head, **record}


def _ship_with_maintainer_review(
    *,
    args: dict[str, Any],
    active: dict[str, Any],
    state: dict[str, Any],
    state_path: Path,
    authority,
    canonical_run,
    binding: dict[str, Any],
    preflight: object,
    remote: object,
    orchestrator: ShipOrchestrator,
    github: GitHubDeliveryClient,
    ship: dict[str, Any] | None,
    fix_rounds: int,
) -> dict[str, Any]:
    path = args.get("maintainer_review_path")
    expected_hash = args.get("maintainer_review_hash")
    _validate_maintainer_review(
        path=path,
        expected_hash=expected_hash,
        run=canonical_run,
        authority=authority,
        pr_number=binding["pr_number"],
        candidate=preflight.head,
    )
    maintainer = MaintainerReviewEvidence(path=str(path), expected_hash=str(expected_hash))
    foreign_review = ForeignReviewEvidence(
        path=str(_absolute_file(args.get("foreign_review_path"), field="foreign_review_path")),
        expected_hash=args.get("foreign_review_hash"),
    )
    remote_gate = evaluate_delivery_gate(
        facts=remote,
        policy=DeliveryPolicy(
            expected_head=preflight.head,
            required_closing_issues=authority.mapped_issues,
            review_kind="maintainer-review",
        ),
    )
    if not remote_gate.allowed:
        raise RuntimeError(f"merge authorization blocked: {', '.join(remote_gate.reasons)}")
    existing_authorization = ship.get("merge_authorization") if ship else None
    superseded_authorization = (
        ship.get("superseded_merge_authorization") if ship else None
    )
    if _authorization_schema(existing_authorization) == "cortex-merge-authorization/v1":
        if (
            superseded_authorization is not None
            and superseded_authorization != existing_authorization
        ):
            raise RuntimeError("persisted merge authorization differs from current gate evidence")
        superseded_authorization = existing_authorization
        existing_authorization = None
    if superseded_authorization is not None and (
        _authorization_schema(superseded_authorization) != "cortex-merge-authorization/v1"
        or not _authorization_identity_matches(
            superseded_authorization,
            active=active,
            authority=authority,
            binding=binding,
            head=preflight.head,
            tree_hash=preflight.tree_hash,
            terminal_reconciliation=True,
        )
    ):
        raise RuntimeError("persisted merge authorization differs from current gate evidence")
    body = _merge_authorization_body(
        active=active,
        authority=authority,
        binding=binding,
        preflight=preflight,
        remote=remote,
        copilot=None,
        foreign_review=foreign_review,
        maintainer_review=maintainer,
        superseded_authorization=superseded_authorization,
    )
    if existing_authorization is not None and (
        not isinstance(existing_authorization, dict)
        or existing_authorization.get("payload") != body
    ):
        raise RuntimeError("persisted merge authorization differs from current gate evidence")
    authorization = _authorization_record(body, state_path=state_path)
    if existing_authorization is not None and existing_authorization != authorization:
        raise RuntimeError("persisted merge authorization differs from current gate evidence")
    next_ship = {
        **(ship or {}),
        "phase": "merge-authorized",
        "head": preflight.head,
        "tree_hash": preflight.tree_hash,
        "review_kind": "maintainer-review",
        "review_ref": maintainer.path,
        "fix_rounds": fix_rounds,
        "pr_number": binding["pr_number"],
        "change": binding["change"],
        "todo_paths": list(binding["todo_paths"]),
        "merge_authorization": authorization,
    }
    if superseded_authorization is not None:
        next_ship["superseded_merge_authorization"] = superseded_authorization
    else:
        next_ship.pop("superseded_merge_authorization", None)
    active["ship"] = next_ship
    _save_runs(state_path, state)
    try:
        merged = orchestrator.merge_if_ready(
            repo=authority.repo,
            pr_number=binding["pr_number"],
            change=binding["change"],
            expected_head=preflight.head,
            expected_tree_hash=preflight.tree_hash,
            authority=authority,
            preflight=preflight,
            copilot=None,
            foreign_review=foreign_review,
            maintainer_review=maintainer,
        )
    except RuntimeError:
        post_merge = github.fetch_merge_status(
            repo=authority.repo, pr_number=binding["pr_number"]
        )
        if (
            not post_merge.merged
            or post_merge.pr_head != preflight.head
            or not _authorization_matches(
                authorization,
                active=active,
                authority=authority,
                binding=binding,
                preflight=preflight,
                remote=remote,
            )
        ):
            raise
        merged = SimpleNamespace(
            expected_head=preflight.head,
            expected_tree_hash=preflight.tree_hash,
        )
    else:
        post_merge = github.fetch_merge_status(
            repo=authority.repo, pr_number=binding["pr_number"]
        )
        if not post_merge.merged or post_merge.pr_head != preflight.head:
            raise RuntimeError("merge side effect is not visible on exact PR HEAD")
    active["ship"] = {
        **active["ship"],
        "phase": "merged",
        "head": merged.expected_head,
        "tree_hash": merged.expected_tree_hash,
        "merge_commit": post_merge.merge_commit,
    }
    _save_runs(state_path, state)
    return {
        "action": "merged-awaiting-closure",
        "head": preflight.head,
        "review_kind": "maintainer-review",
        "review_ref": maintainer.path,
        "review_hash": maintainer.expected_hash,
    }


def _recoverable_maintainer_ship_stop(
    *,
    ship: dict[str, Any] | None,
    args: dict[str, Any],
) -> bool:
    """Detect a complete maintainer locator on a recoverable Copilot stop."""

    has_path = args.get("maintainer_review_path") is not None
    has_hash = args.get("maintainer_review_hash") is not None
    if has_path != has_hash:
        raise ValueError("maintainer review path/hash must be supplied together")
    return bool(
        has_path
        and ship is not None
        and ship.get("phase") == "needs_human"
        and isinstance(ship.get("reason"), str)
        and str(ship["reason"]).startswith("copilot-")
    )


def _effective_superseded_generations(workflow_registry, *, repo: str, work_id: str) -> list:
    """#519：計入 semantic-reclaim 熔斷的 superseded 世代（扣掉已赦免者）。

    `_claim_action` 的熔斷判定與 `reset-reclaim-budget` 動作共用同一支計數，
    兩邊對「還剩幾代額度」的認知不可能分歧。registry 沒有
    `reclaim_reset_cleared_run_ids`（舊版本或測試 double）時視為沒有任何赦免，
    也就是維持熔斷最嚴格的既有行為——fail-closed 方向。
    """

    cleared_lookup = getattr(workflow_registry, "reclaim_reset_cleared_run_ids", None)
    cleared = (
        cleared_lookup(repo=repo, work_id=work_id)
        if callable(cleared_lookup)
        else frozenset()
    )
    return [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == repo
        and run.work_id == work_id
        and run.status == "superseded"
        and run.run_id not in cleared
    ]


def _fallback_workflow_starter(workflow_registry, state_path: Path):
    """Test/embedding fallback; installed daemon supplies the production starter."""

    from .work_bridge import default_workflow_manifest
    from .workflow import WorkflowStep

    def start(bound_authority, claim_key, reason):
        manifest = default_workflow_manifest(
            bound_authority.work_id,
            change=(
                bound_authority.mapped_openspec[0]
                if bound_authority.mapped_openspec
                else bound_authority.work_id
            ),
        )
        steps = tuple(
            WorkflowStep(
                phase=step.phase,
                persona=step.persona,
                card=step.card,
                executor="cortex-manager" if step.phase == "claim" else step.executor,
                model="deterministic" if step.phase == "claim" else step.model,
                domain="cortex" if step.phase == "claim" else step.domain,
                inputs=step.inputs,
                outputs=step.outputs,
                gate_result="passed" if step.phase == "claim" else step.gate_result,
            )
            for step in manifest.steps
        )
        return workflow_registry._manager_create_workflow_run(
            work_id=bound_authority.work_id,
            repo=bound_authority.repo,
            claim_key=claim_key,
            source_revision=work_authority_digest(bound_authority),
            workspace_root=str(state_path.parent.resolve()),
            combo=manifest.combo,
            current_phase="define",
            steps=steps,
            issue_refs=tuple(
                f"{bound_authority.repo}#{number}" for number in bound_authority.mapped_issues
            ),
            openspec_refs=bound_authority.mapped_openspec,
            pr_refs=tuple(
                f"{bound_authority.repo}#{number}" for number in bound_authority.mapped_prs
            ),
            attempts={"claim": 1, "define": 1},
            facets=("needs_human",) if reason is not None else (),
            gate_status="running",
            needs_human_reason=(
                diagnostic_reason(
                    "claim-blocked",
                    f"claim 判定需要人工介入即建立 run：{reason}",
                    source="work_actions._fallback_workflow_starter",
                    work_id=bound_authority.work_id,
                    repo=bound_authority.repo,
                    claim_key=claim_key,
                )
                if reason is not None
                else None
            ),
        )

    return start


# #669：`work_bridge.start_canonical_workflow` 在 claim 判定需要人工介入時建立的
# run，其結構化理由固定帶這組 (reason, source)。這是「修正前留下的殭屍 run」唯一
# 可機械辨識的簽名，用它把清理指引精準綁到那批 run 上，不會誤傷任何真的在跑的
# needs_human run（build／verify／review 卡住的 run 既不在 claim phase，
# `source` 也不是這一支）。
_CLAIM_BLOCKED_REASON = "claim-blocked"
_CLAIM_BLOCKED_SOURCE = "work_bridge.start_workflow_for_authority"

_NOT_CLAIMABLE_SOURCE = "work_actions._claim_action:not-claimable"


def _claim_blocked_stale_run(run) -> Any | None:
    """#669 修正前留下的 claim-blocked 殭屍 run；不是的話回 ``None``。

    判準刻意收到最窄：**ongoing ＋ 停在 `claim` phase ＋ 掛 `needs_human` ＋ 結構化
    理由正是 `claim-blocked`／`work_bridge.start_workflow_for_authority` ＋ 沒有任何
    evidence／PR**。少任何一項都不算——宣告一個「可以直接清掉」的 run 卻其實握有
    工作成果，比不宣告更糟。
    """

    if run is None or getattr(run, "status", None) != "ongoing":
        return None
    if getattr(run, "current_phase", None) != "claim":
        return None
    if "needs_human" not in tuple(getattr(run, "facets", ()) or ()):
        return None
    if tuple(getattr(run, "evidence_refs", ()) or ()) or tuple(getattr(run, "pr_refs", ()) or ()):
        return None
    payload = getattr(run, "needs_human_reason", None)
    if not isinstance(payload, dict):
        return None
    if payload.get("reason") != _CLAIM_BLOCKED_REASON:
        return None
    if payload.get("source") != _CLAIM_BLOCKED_SOURCE:
        return None
    return run


def _not_claimable_response(
    *,
    authority,
    state_path: Path,
    stale_run=None,
) -> dict[str, Any]:
    """#669：記一筆耐久的 `not-claimable` 並回報「沒有建立 run」。

    回傳的 ``run`` 恆為 ``None``——這正是本次修正的重點：判定「不可 claim」不再
    留下任何 durable run。既有殭屍 run（修正前建立的）以 ``stale_run_id`` 如實
    揭露並附上清理指令，不隱藏、也不由系統自行清除（清除是 operator 的明示動作，
    見 `#373` 的守衛：auto-claim 不得自動清除或重試 needs_human run）。
    """

    # 兩種 row 分開命名，operator 一眼就知道要不要動手：
    # - `missing_issue`：沒建 run，work item 現在就是不可 claim（workstream 多半
    #   永遠停在這個狀態，屬預期）。
    # - `claim-blocked-stale-run`：#669 修正前留下的殭屍 run 還在，需要一次清理。
    reason = "missing_issue" if stale_run is None else "claim-blocked-stale-run"
    detail = "claim 判定 work item 目前不可 claim，且刻意不建立 run：missing_issue"
    if stale_run is not None:
        detail += f"；另有 #669 修正前留下的 claim-blocked run {stale_run.run_id} 待清理"
    if stale_run is not None:
        hint = (
            f"殭屍 run {stale_run.run_id} 是 #669 修正前「判定 missing_issue 仍建立 run」"
            "留下的，永遠不會推進。確認無誤後以 `cortex work abandon "
            f"{authority.work_id} --repo {authority.repo} --expected-run-id "
            f"{stale_run.run_id} --actor <operator> --reason '#669 claim-blocked zombie'` "
            "清除；清掉之後本項目只會留在 not_claimable 清單，不再佔用 attention。"
        )
    else:
        hint = (
            "本 work item 沒有對應的 GitHub issue，claim 因此不建立 run。"
            "workstream 類（`docs/superpowers/workstreams/*`，設計上不對應單一 issue）"
            "維持現狀即可；若這是漏開 issue，開好之後以 `cortex work link "
            f"{authority.work_id} --repo {authority.repo} --issue <N>` 綁定，"
            "下一輪掃描即自動 claim，本筆記錄同時自動消失。"
        )
    entry = not_claimable.record(
        not_claimable.ledger_path(Path(state_path).parent),
        repo=authority.repo,
        work_id=authority.work_id,
        reason=reason,
        detail=detail,
        source=_NOT_CLAIMABLE_SOURCE,
        next_step_hint=hint,
        authority_digest=work_authority_digest(authority),
        mapped_openspec=authority.mapped_openspec,
        mapped_todo_paths=authority.mapped_todo_paths,
        stale_run_id=None if stale_run is None else stale_run.run_id,
    )
    response: dict[str, Any] = {
        "action": "not_claimable",
        "reason": reason,
        "run": None,
        "not_claimable": entry,
        "next_step_hint": hint,
    }
    if stale_run is not None:
        response["stale_run_id"] = stale_run.run_id
        response["legal_next_steps"] = ("abandon",)
    return response


def _maybe_record_copilot_timeout_rearm_permit(
    *,
    args: dict[str, Any],
    authority,
    requested_by: str,
    now_epoch: float,
    state_path: Path,
    workflow_registry,
    canonical_run,
) -> dict[str, Any] | None:
    if args.get("action") != "resume":
        return None
    extras = set(args) - {"action", "repo", "work_id", "issue"}
    if extras:
        raise ValueError(f"resume rejects caller evidence/input: {sorted(extras)[0]}")
    if canonical_run is None or canonical_run.status != "ongoing":
        return None
    state, active, run = _load_work_run(
        state_path=state_path,
        workflow_registry=workflow_registry,
        authority=authority,
    )
    _validate_current_run_authority(active, authority, run)
    ship = active.get("ship")
    if ship is None:
        return None
    if not isinstance(ship, dict):
        raise ValueError("ship state malformed")
    if ship.get("phase") != "needs_human" or ship.get("reason") != "copilot-review-timeout":
        return None
    old_head = ship.get("head")
    if (
        not isinstance(old_head, str)
        or verification.SAFE_SHA_RE.fullmatch(old_head) is None
    ):
        raise ValueError("copilot timeout ship state malformed")
    candidate_head = _exact_verified_candidate_head(run)
    if candidate_head is None:
        return None
    if old_head == candidate_head:
        if active.get("copilot_review_rearm_permit") is not None:
            _invalidate_copilot_rearm_permit(
                active=active,
                state=state,
                state_path=state_path,
            )
            raise RuntimeError("copilot timeout rearm permit is stale")
        return None
    binding = active.get("delivery_binding")
    if not isinstance(binding, dict):
        raise RuntimeError("copilot timeout rearm requires persisted delivery binding")
    desired = {
        "schema": _COPILOT_REARM_PERMIT_SCHEMA,
        "run_id": run.run_id,
        "ship_hash": _ship_state_hash(ship),
        "old_head": old_head,
        "candidate_head": candidate_head,
        "authority_digest": work_authority_digest(authority),
        "delivery_binding_hash": _delivery_binding_hash(binding),
        "history_prefix_hash": _delivery_review_history_hash(active),
        "requested_by": requested_by,
        "transition_id": f"manager-transition-{uuid4().hex}",
        "created_at_epoch": float(now_epoch),
    }
    existing = _copilot_rearm_permit(active)
    if existing is None:
        active["copilot_review_rearm_permit"] = desired
        _save_runs(state_path, state)
        return desired
    same_tuple = (
        existing["run_id"] == desired["run_id"]
        and existing["ship_hash"] == desired["ship_hash"]
        and existing["old_head"] == desired["old_head"]
        and existing["candidate_head"] == desired["candidate_head"]
        and existing["authority_digest"] == desired["authority_digest"]
        and existing["delivery_binding_hash"] == desired["delivery_binding_hash"]
        and existing["history_prefix_hash"] == desired["history_prefix_hash"]
        and existing["requested_by"] == desired["requested_by"]
    )
    if not same_tuple:
        _invalidate_copilot_rearm_permit(
            active=active,
            state=state,
            state_path=state_path,
        )
        raise RuntimeError("copilot timeout rearm permit conflicts with current state")
    return existing


def _claim_action(
    *,
    args: dict[str, Any],
    authority,
    requested_by: str = "operator",
    now_epoch: float,
    state_path: Path,
    automatic: bool = False,
    auto_label: bool | None = None,
    workflow_registry=None,
    workflow_starter=None,
    readiness_checker=None,
) -> dict[str, Any]:
    """Claim decision for one authority.

    ``readiness_checker``, when supplied, is the #211 pre-claim readiness
    transaction (see ``claim_readiness.py``): a callable of
    ``(authority, issue_ref) -> ReadinessOutcome`` run before a
    ``ClaimCandidate`` is even assembled. Any readiness failure returns here
    without ever calling ``workflow_starter`` — no workflow job, worktree, or
    model session may be created until all six checks pass. ``None`` (the
    default) preserves prior behaviour exactly, so existing callers that do
    not yet wire a checker are unaffected.
    """

    if args.get("action") == "resume":
        extras = set(args) - {"action", "repo", "work_id", "issue"}
        if extras:
            raise ValueError(f"resume rejects caller evidence/input: {sorted(extras)[0]}")

    canonical_run = None
    # #524：canonical_run 是靠「in-flight 保護傘」而非 claim_key／identity 比對
    # 找回來的旗標，供下方 #216 AC5 的 resume 分支判定用（見該處註解）。
    inflight_resume = False
    if workflow_registry is not None:
        all_runs = workflow_registry.list_workflow_runs()
        expected_key = _expected_claim_key(authority)
        matching = [
            run
            for run in all_runs
            if run.repo == authority.repo
            and run.work_id == authority.work_id
            and run.claim_key == expected_key
            and run.status == "ongoing"
        ]
        if len(matching) > 1:
            raise RuntimeError("canonical workflow claim is ambiguous")
        canonical_run = matching[0] if matching else None
        if canonical_run is None and (automatic or args.get("action") == "resume"):
            active = [
                run
                for run in all_runs
                if run.repo == authority.repo
                and run.work_id == authority.work_id
                and run.status == "ongoing"
                and run.issue_refs
                == tuple(
                    f"{authority.repo}#{number}"
                    for number in authority.mapped_issues
                )
                and _openspec_refs_compatible(run, authority)
            ]
            if len(active) > 1:
                raise RuntimeError("active workflow identity is ambiguous")
            canonical_run = active[0] if active else None
        if canonical_run is None:
            # #524：in-flight 保護傘——「已在 flight 且未失敗的 run 不得被新的
            # claim 作廢」。
            #
            # 根因：`claim_key` 由 `work_authority_digest` 導出，而該 digest 折入
            # `source_revisions`。run 自己的 planning 卡把 spec/design/plan 寫進
            # governed roots 之後，monitor 會把這些檔案當成**新的 confirmed
            # source** 併入同一個 work item，digest 因此改變、`claim_key` 隨之
            # 漂移——run 是被自己的成功產出擠掉識別的。上面第一段用
            # `_expected_claim_key(authority)` 比對 persisted `run.claim_key`
            # 於是必然落空。
            #
            # 第二段 fallback 雖然改用不受 planning 產出影響的穩定識別
            # （issue_refs/openspec_refs），卻只在 `automatic`（auto-scan）或
            # `args["action"] == "resume"` 時才跑；`start`／`intake` 這兩個
            # control-request 入口整段跳過，canonical_run 保持 None，claim 路徑
            # 把它當成全新 claim，`registry._manager_create_workflow_run` 再無
            # 條件把同 (repo, work_id) 的 ongoing run 全部標成 superseded
            # （見 registry.py 的 supersede 迴圈）——這正是 #524 生產現場
            # `workflow-009fe9ab303df196209d` 四張卡全 passed、phase 已達 build
            # 卻在 90 秒後被自行作廢的路徑。
            #
            # 這裡補的是最後一道、不分呼叫端的防線，判準有二，缺一不可：
            #
            # (1)「未失敗」：`run.status == "ongoing"` 且
            #     `workflow_status(run) == "ongoing"`。兩個都要——`workflow_status`
            #     對 abandon 釋放過的 run（`superseded` + `planning_released`）
            #     刻意回傳 `"ongoing"`（見 work_bridge.py），只看它會把
            #     #256／#416 的 abandon→reclaim 出口一併鎖死；只看
            #     `run.status` 又會把 needs_human／needs_decomposition／blocked
            #     的 run 誤納入保護。
            #
            # (2)「漂移完全來自 run 自己的產出」：把 planning phase 自產的
            #     `superpowers_spec:`／`superpowers_plan:` source 剝掉之後重算的
            #     authority digest，必須與 run 持久化的 `source_revision`（claim
            #     當下的 `work_authority_digest`）逐字相符。相符即代表「除了這個
            #     run 自己寫出來的 spec/design/plan 以外，authority 一個字都沒
            #     變」，此時換代純屬自我作廢。
            #
            # 判準 (2) 同時守住既有的 operator 逃生口：issue 開關、openspec
            # revision、todo 成員變動等**真正的** authority 變更不會被剝除，
            # digest 依然不同，`start` 照舊開新世代（見
            # tests/test_work_actions.py::test_source_change_starts_new_canonical_run）。
            inflight_digest = authority_digest_without_planning_outputs(authority)
            inflight = [
                run
                for run in all_runs
                if run.repo == authority.repo
                and run.work_id == authority.work_id
                and run.status == "ongoing"
                and workflow_status(run) == "ongoing"
                and run.source_revision == inflight_digest
            ]
            if len(inflight) > 1:
                raise RuntimeError("active workflow identity is ambiguous")
            if inflight:
                canonical_run = inflight[0]
                inflight_resume = True
        if canonical_run is None and args.get("action") == "resume":
            completed = [
                run
                for run in all_runs
                if run.repo == authority.repo
                and run.work_id == authority.work_id
                and run.status == "done"
                and run.current_phase == "ship"
            ]
            if len(completed) > 1:
                raise RuntimeError("completed workflow identity is ambiguous")
            canonical_run = completed[0] if completed else None
    issue = args.get("issue") if args.get("issue") is not None else (
        authority.mapped_issues[0] if authority.mapped_issues else None
    )
    if readiness_checker is not None:
        issue_ref = f"{authority.repo}#{issue}" if issue is not None else None
        readiness = readiness_checker(authority, issue_ref)
        if not readiness.ready:
            return {
                "action": "needs_human" if readiness.terminal else "blocked",
                "reason": readiness.reason,
                "run": None,
                "readiness_failed_check": readiness.failed_check,
            }
    planning_failure_hint = (
        _planning_failure_hint(canonical_run) if canonical_run is not None else None
    )
    candidate = ClaimCandidate(
        authority=authority,
        repo=authority.repo,
        work_id=authority.work_id,
        source_revisions=authority.source_revisions,
        confirmed_todo=authority.confirmed_todo,
        confirmed_issue=issue,
        auto_label=(authority.auto_label if auto_label is None else auto_label) if automatic else False,
        active_run_id=canonical_run.run_id if canonical_run is not None else None,
        active_claim_key=canonical_run.claim_key if canonical_run is not None else None,
        active_status=workflow_status(canonical_run) if canonical_run is not None else None,
        active_snapshot_hash=authority.snapshot_hash if canonical_run is not None else None,
        active_source_revisions=(
            authority.source_revisions if canonical_run is not None else None
        ),
        active_provider_revision=(
            authority.github_provider_revision if canonical_run is not None else None
        ),
        active_authority_digest=(
            work_authority_digest(authority) if canonical_run is not None else None
        ),
        # #213（design #208 A.1）freeze 接線：Yellow band 的 freeze point 移到
        # plan_review_gate ready=True 之後——run.plan_review_passed 是 dispatch
        # 掛載點寫入的持久化基準。Green/Red/None band 從沒呼叫過 gate，比照
        # pre-#213 立即凍結行為（視為已通過，#223 已定案的 fail-soft 慣例）。
        active_plan_review_passed=(
            True
            if canonical_run is None or getattr(canonical_run, "sizing_band", None) != "yellow"
            else getattr(canonical_run, "plan_review_passed", False)
        ),
        active_claim_identity_digest=(
            claim_identity_digest(authority) if canonical_run is not None else None
        ),
        # #256 R2：needs_human 的可執行下一步由 run 自身狀態決定——phase 與
        # 系統寫入的 planning 失敗 evidence，呼叫端自述不參與。
        active_phase=(
            getattr(canonical_run, "current_phase", None) if canonical_run is not None else None
        ),
        active_planning_failure_classification=(
            planning_failure_hint["classification"] if planning_failure_hint else None
        ),
        active_planning_failure_reason=(
            planning_failure_hint["reason"] if planning_failure_hint else None
        ),
    )
    if (
        canonical_run is not None
        and canonical_run.claim_key != _expected_claim_key(authority)
        and (automatic or args.get("action") == "resume" or inflight_resume)
    ):
        # #524：`inflight_resume` 讓 `start`／`intake` 也走進本分支。上面的
        # 保護傘既然把 in-flight run 找了回來，就必須在這裡以 resume 收尾——
        # 否則會落到下方 `decide_manual_start` -> `_existing()`，那裡對
        # 「persisted claim_key 與目前 authority 不符」是直接
        # `raise ValueError("persisted claim key does not match authority")`，
        # 等於把原本的自行 supersede 換成一個例外，run 一樣救不回來。
        # #216 AC5：authority 宣告已變更（claim_key 不比對即代表 authority_digest
        # 不同）——只 invalidate 依賴該 authority 內容的 verify/review gate，
        # build phase 已產出的 Candidate 保持不變。僅在 verify/review phase 且
        # 沒有 in-flight job 時才符合精準 invalidation 的前置條件；不符合時
        # （build/claim/define/plan phase、或有 active job）維持既有『原樣
        # resume』行為，不強行 invalidate。
        new_digest = work_authority_digest(authority)
        authority_restart_classification = None
        if canonical_run.current_phase in {"verify", "review"}:
            try:
                canonical_run = workflow_registry._manager_reset_workflow_for_authority_restart(
                    canonical_run.run_id,
                    authority_digest=new_digest,
                )
                authority_restart_classification = _classify_retry(
                    canonical_run, workflow_registry, trigger="authority-restart"
                )
            except ValueError:
                pass
        active = canonical_run.to_dict()
        active.update(
            {
                "snapshot_hash": authority.snapshot_hash,
                "source_revisions": list(authority.source_revisions),
                "provider_revision": authority.github_provider_revision,
                "authority_digest": new_digest,
                "status": workflow_status(canonical_run),
            }
        )
        if authority_restart_classification is not None:
            active["retry_classification"] = authority_restart_classification
        return {
            "action": "resume",
            "reason": "active-workflow",
            "run": active,
        }
    decision = (
        decide_auto_claim(candidate, now_epoch=now_epoch)
        if automatic
        else decide_manual_start(candidate, now_epoch=now_epoch)
    )
    # #669：這一輪判定「不可 claim」與否，決定 `not-claimable` ledger 該記還是該收。
    # 兩者共用同一個布林，兩邊對「這件事還在不在」的認知不可能分歧——ledger 因此
    # 不會留下永久假警報（issue 補上、或既有殭屍 run 被 abandon 之後自動消失）。
    stale_claim_blocked_run = _claim_blocked_stale_run(canonical_run)
    work_item_not_claimable = decision.action == "needs_human" and (
        decision.reason == "missing_issue" or stale_claim_blocked_run is not None
    )
    if not work_item_not_claimable:
        not_claimable.clear(
            not_claimable.ledger_path(Path(state_path).parent),
            repo=authority.repo,
            work_id=authority.work_id,
        )
    if decision.action == "claim":
        if workflow_starter is None:
            raise RuntimeError("canonical workflow starter unavailable")
        # #218 AC2（design #208 E）：語意 re-claim 的世代熔斷——同一 (repo, work_id)
        # 已累積 SEMANTIC_RECLAIM_LIMIT 個 superseded 世代（v1..v3）時，不得自動
        # 建立下一版 run（v4），強制 needs_human。計數以 registry 的 run 歷史為準
        # （跨 run_id，不受 active dict 換代歸零影響）。
        # #519：計數改扣掉已被 `reset-reclaim-budget` 明示赦免的世代——熔斷原本
        # 對全部歷史無條件累加且沒有任何重置路徑，根因（例如 #507／#511／#516
        # 這些 cortex 自身缺陷）修好之後 work item 仍永久鎖死。
        if workflow_registry is not None:
            superseded_generations = _effective_superseded_generations(
                workflow_registry, repo=authority.repo, work_id=authority.work_id
            )
            if len(superseded_generations) >= SEMANTIC_RECLAIM_LIMIT:
                return {
                    "action": "needs_human",
                    "reason": "semantic-reclaim-budget-exhausted",
                    "run": None,
                    "superseded_generations": len(superseded_generations),
                    # #519：熔斷結果必須自帶下一步，否則 operator 只看到「額度
                    # 用盡」而不知道有解（比照 #218 AC3 的 legal_next_steps 慣例）。
                    "reclaim_budget_limit": SEMANTIC_RECLAIM_LIMIT,
                    "superseded_run_ids": sorted(
                        run.run_id for run in superseded_generations
                    ),
                    "legal_next_steps": ("reset-reclaim-budget",),
                    "next_step_hint": (
                        "世代熔斷已觸發；確認根因已排除後，以 "
                        f"`cortex work reset-reclaim-budget {authority.work_id} "
                        f"--repo {authority.repo} --actor <operator> --reason <單行理由>` "
                        "明示重置額度（會留下 cortex-work-reclaim-reset/v1 稽核紀錄），"
                        "再重新 start。"
                    ),
                }
        try:
            run = workflow_starter(authority, str(decision.claim_key), None)
        except RuntimeError as error:
            # #216 AC4：source-owner transfer 尚未完成（`start_canonical_workflow`
            # 的 `_other_owner_ongoing_runs` 防線，#217）——fail-closed 轉成結構化
            # blocked 結果，明確標記 source_owner_repair 分類；builder 從未被
            # 呼叫（workflow_starter 在建立任何 job 前就先 raise）。
            if not str(error).startswith(_SOURCE_OWNER_CONFLICT_PREFIX):
                raise
            return {
                "action": "blocked",
                "reason": "source-owner-repair-pending",
                "run": None,
                "retry_classification": _classify_retry(
                    None, None, trigger="source-owner-repair"
                ),
            }
        active = run.to_dict()
    elif work_item_not_claimable:
        # #669：`missing_issue` 不得再物化成 run。
        #
        # 舊行為是「先建 run 再宣告 blocked」（`work_bridge.start_canonical_workflow`
        # 的 `needs_human_reason` 分支，detail 逐字寫著「claim 判定需要人工介入即
        # 建立 run」）。實機首輪掃描後 24 個 workstream work item 因此各自變成一個
        # `current_phase: claim`／`gate_state: running`／`evidence_refs: []`／
        # `next_actions: []` 的 `needs_human` run，永遠不會推進，`attention` 信噪比
        # 1:24——真正該人看的 blocker 被埋掉。
        #
        # 而 `missing_issue` 對 workstream 而言**是預期狀態，不是異常**：
        # `docs/superpowers/workstreams/cost-governance-cluster/todo.md` 開頭逐字
        # 寫著「本 workstream 不對應單一 issue」。把預期狀態物化成 durable state
        # 是根本的類別錯誤。
        #
        # 但「只是不建 run」會把 fail-loud 換成 fail-silent：真的該有 issue 卻沒有
        # 的 work item 會被靜默略過。因此每一次跳過都必須在 `not-claimable` ledger
        # 留一筆 operator 查得到的紀錄（`cortex status` 的 `not_claimable` 區塊）。
        #
        # 第二個判準（`_claim_blocked_stale_run`）只涵蓋**修正前既有的殭屍 run**：
        # 它們存在時 `_resume_decision` 會先回 `human-intervention-required`，
        # `missing_issue` 這條判準看不到它們，operator 因此也拿不到清理指引。
        return _not_claimable_response(
            authority=authority,
            state_path=state_path,
            stale_run=stale_claim_blocked_run,
        )
    elif decision.action == "needs_human":
        claim_key = build_claim_key(
            ClaimCandidate(
                authority=authority,
                repo=authority.repo,
                work_id=authority.work_id,
                source_revisions=authority.source_revisions,
                confirmed_todo=authority.confirmed_todo,
                confirmed_issue=None,
                auto_label=authority.auto_label,
                active_run_id=None,
                active_claim_key=None,
            )
        )
        if canonical_run is None:
            if workflow_starter is None:
                raise RuntimeError("canonical workflow starter unavailable")
            canonical_run = workflow_starter(authority, claim_key, decision.reason)
        elif args.get("action") == "resume":
            if workflow_starter is None:
                raise RuntimeError("canonical workflow starter unavailable")
            canonical_run = workflow_starter(
                authority, canonical_run.claim_key, None
            )
        active = canonical_run.to_dict()
        active["reason"] = decision.reason
    elif canonical_run is not None:
        # #420：`decision.action == "resume"` 只代表 `_resume_decision` 判定
        # `active_status == "ongoing"`（needs_human/blocked/done/
        # needs_decomposition 都有自己的專屬分支，不會落到這裡）——即 facets
        # 乾淨、單純還在跑。explicit `cortex work resume`（`args["action"] ==
        # "resume"`）本就會在這裡重呼叫 `workflow_starter`，讓仍卡在 define
        # phase（`apply_workflow_action(action="start")` 的 claim→define→plan
        # 同步續推段被中途打斷，例如 `_load_planning_artifacts` 之類未被
        # needs_human 分支接住的例外）的 run 有機會重跑一次。periodic
        # auto-claim scan（`automatic=True`，`args["action"] == "auto-scan"`）
        # 過去完全不滿足 `args.get("action") == "resume"` 這個字面比對，導致
        # 同一個 claim_key 每輪都只落到下面 `active = canonical_run.to_dict()`
        # 的原樣反映——`workflow_starter` 永遠不會再被呼叫。這正是 auto-claim
        # 建立的 run 完成 define 前若被中斷，就永久卡住、explicit intake 卻能
        # 在同一 request 內同步續推的根因（#420）。這裡加上
        # `automatic and decision.action == "resume"` 這個等價觸發，且刻意
        # **不**擴及 needs_human/blocked：那兩個分支仍只在明確
        # `args["action"] == "resume"`（即人工介入）時才重試，維持 #373 的
        # 守衛——auto-claim 不得自動清除或重試 needs_human run。
        # `start_canonical_workflow` 對非 define phase 的既有 run 本就是原樣
        # early return（見 work_bridge.py `_claimable_existing_runs`／
        # `existing_run.current_phase != "define"` 短路），故對 plan/build/
        # verify/review 等已推進的 run 重呼叫 `workflow_starter` 是安全的
        # no-op，不會產生非預期的重派工。
        if (
            args.get("action") == "resume"
            and decision.action in {"resume", "needs_human", "blocked"}
        ) or (automatic and decision.action == "resume"):
            if workflow_starter is None:
                raise RuntimeError("canonical workflow starter unavailable")
            canonical_run = workflow_starter(
                authority, canonical_run.claim_key, None
            )
        active = canonical_run.to_dict()
    else:
        active = None
    if active is not None:
        active.update(
            {
                "snapshot_hash": authority.snapshot_hash,
                "source_revisions": list(authority.source_revisions),
                "provider_revision": authority.github_provider_revision,
                "authority_digest": work_authority_digest(authority),
                "status": (
                    workflow_status(canonical_run)
                    if canonical_run is not None
                    else active.get("status", "ongoing")
                ),
            }
        )
        # #208 收口 wiring 1：sizing 是否算得出來的可觀測標記，比照
        # claim_readiness.capability_probe 的 envelope_unavailable bypass 模式
        # ——輸入不可得（無 plan artifact／combo／宣告欄位）時 sizing_score 為
        # None，這裡如實反映，不掩蓋。
        active["sizing_unavailable"] = active.get("sizing_score") is None
        if readiness_checker is not None:
            frozen_dict = readiness.frozen.to_dict()
            active["frozen_readiness"] = frozen_dict
            # #208 收口 wiring 5：把凍結集持久化到 run 本身（#211 掛在 run dict
            # 只是 API 回應層的浮動增補，dispatch 建 builder worktree 時讀的是
            # registry 裡的 WorkflowRun，必須實際寫回才能被消費）。builder
            # worktree 一旦建立就不再讀這個欄位，重複凍結不會造成陳舊 base。
            run_id_to_persist = active.get("run_id")
            if workflow_registry is not None and isinstance(run_id_to_persist, str):
                persisted = workflow_registry._manager_update_workflow_run(
                    run_id_to_persist, frozen_readiness=frozen_dict
                )
                active["frozen_readiness"] = persisted.frozen_readiness
    if canonical_run is not None:
        rearm_permit = _maybe_record_copilot_timeout_rearm_permit(
            args=args,
            authority=authority,
            requested_by=requested_by,
            now_epoch=now_epoch,
            state_path=state_path,
            workflow_registry=workflow_registry,
            canonical_run=canonical_run,
        )
        if active is not None and rearm_permit is not None:
            active["copilot_review_rearm_permit"] = rearm_permit
    response: dict[str, Any] = {
        "action": decision.action,
        "reason": decision.reason,
        "run": active,
    }
    # #256 R2：operator／agent 不必翻 registry 就知道下一步——狀態帶得出合法
    # 動作集合與具體 blocking reason 時一併回報。
    if decision.next_actions:
        response["next_actions"] = list(decision.next_actions)
    if decision.next_step_hint is not None:
        response["next_step_hint"] = decision.next_step_hint
    # #546（部分）：卡片卡在 needs_human 時，`_resume_decision` 看不到 job 層
    # 事實，宣告的唯一出口是 `abandon`（＝燒掉一個世代與合格的 commit）。
    # 這裡把同樣以 run/job 事實判定為「真的會被受理」的復原動作補進去，順序維持
    # 「既有決策優先、補充在後」，不重排既有值。#569：verify／review 的 reviewer
    # 卡一併涵蓋——那個現場的 operator 正是因為 `next_actions` 只寫著 `abandon`
    # 才轉而使用只重置不重派的 `retry-verify`。
    if decision.action == "needs_human" and canonical_run is not None:
        extra = [
            item
            for item in _phase_recovery_actions(canonical_run, workflow_registry)
            if item not in response.get("next_actions", [])
        ]
        if extra:
            response["next_actions"] = [*response.get("next_actions", []), *extra]
            reason_payload = canonical_run.needs_human_reason
            reason_code = (
                reason_payload.get("reason")
                if isinstance(reason_payload, dict)
                else None
            )
            if "retry-review" in extra and reason_code == "blocking-findings":
                response["next_step_hint"] = blocking_findings_next_step_hint(
                    work_id=canonical_run.work_id,
                    repo=canonical_run.repo,
                    candidate=canonical_run.candidate_head,
                )
            elif "review-attest" in extra and authority is not None:
                response["next_step_hint"] = (
                    f"cortex work review-attest {canonical_run.work_id} --repo {authority.repo} --actor <operator> --payload <file>"
                )
    if decision.blocking_reason is not None:
        response["blocking_reason"] = decision.blocking_reason
    return response


class RetryClassification(str, Enum):
    """#208 根因3 定案的 retry 分類（enum 定案，後波不得改名）。

    #215 落地 MODEL_REPAIR／ORCHESTRATOR_RETRY 兩類判準——純看 run/job 狀態即可
    反推。#216 補齊後三類：AUTHORITY_RESTART／REVIEW_HANDOFF_FAILURE／
    SOURCE_OWNER_REPAIR 的觸發情境（WorkAuthority 宣告變更、review 交接本身
    失敗、claim 所有權轉移中）不是 run/job 狀態能反推的資訊，而是呼叫端在
    觸發當下就已經知道的情境，故由 `_classify_retry` 的 `trigger` 參數直接
    指定，見其docstring。
    """

    MODEL_REPAIR = "model_repair"
    ORCHESTRATOR_RETRY = "orchestrator_retry"
    AUTHORITY_RESTART = "authority_restart"
    REVIEW_HANDOFF_FAILURE = "review_handoff_failure"
    SOURCE_OWNER_REPAIR = "source_owner_repair"


# #216：trigger 對映的三類是呼叫端已知、run/job 狀態無法反推的觸發情境，見
# `_classify_retry` docstring。
_RETRY_TRIGGER_CLASSIFICATIONS: dict[str, RetryClassification] = {
    "authority-restart": RetryClassification.AUTHORITY_RESTART,
    "review-handoff-failure": RetryClassification.REVIEW_HANDOFF_FAILURE,
    "source-owner-repair": RetryClassification.SOURCE_OWNER_REPAIR,
    # retry-verify：candidate 完全不變的 verification 重跑不是模型修復，
    # 依 #208 根因3 不得計入 model failure 指標（也不得吃 #218 repair budget）。
    "verification-rerun": RetryClassification.ORCHESTRATOR_RETRY,
}

# #569：`retry-card` 重派 reviewer 卡時的分類——candidate 一個位元組都沒變，因此
# 與 `retry-verify`／`retry-review` 同類，不是 model repair。build phase 不在表
# 內（取到 `None`），維持 #545 既有的狀態推論。
_RETRY_CARD_PHASE_TRIGGERS: dict[str, str] = {
    "verify": "verification-rerun",
    "review": "review-handoff-failure",
}


def _classify_retry(
    run, workflow_registry, *, trigger: str | None = None
) -> RetryClassification:
    """判斷一次 retry 的性質（#208 根因3；#216 補齊後三類）。

    ``trigger``（#216）：呼叫端已知、run/job 狀態無法反推的觸發情境——
    ``'authority-restart'``／``'review-handoff-failure'``／
    ``'source-owner-repair'``。指定時直接採信呼叫端判斷、略過以下的狀態推論；
    ``None``（預設）維持 #215 既有的 MODEL_REPAIR／ORCHESTRATOR_RETRY 狀態推論
    行為不變，呼叫端不需改動。

    model_repair：candidate 已產出可被下游評估的內容——run 已離開 build phase
    進入 verify/review，或 build phase 已有一顆乾淨終止（status=='exited'、
    exit_code==0）且已綁定 workflow_evidence 的 builder job。此後任何
    needs_human 都是下游對 candidate 內容本身的判斷，屬需要模型修的內容缺陷。

    orchestrator_retry：build phase 尚未有前述「乾淨終止且已綁定 evidence」的
    builder job（job 缺席、未乾淨終止，或乾淨終止卻沒有 evidence）——中斷發生
    在 provider／stale base／claim sequencing 等 orchestrator 層，不是模型內容
    問題，不得計入模型 failure 指標。

    判準只看 run.current_phase 與 JobRegistry 的 job status/exit_code/
    workflow_evidence，刻意不看 run.attempts 世代數（不再只以 vN 判斷重試性質）。
    """
    if trigger is not None:
        try:
            return _RETRY_TRIGGER_CLASSIFICATIONS[trigger]
        except KeyError:
            raise ValueError(f"retry classify 不支援的 trigger: {trigger!r}") from None
    if run.current_phase != "build":
        return RetryClassification.MODEL_REPAIR
    build_steps = [step for step in run.steps if step.phase == "build"]
    repair_card = build_steps[-1].card if build_steps else None
    terminal_with_evidence = [
        job
        for job in workflow_registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_phase") == "build"
        and job.get("workflow_card") == repair_card
        and job.get("status") == "exited"
        and job.get("exit_code") == 0
        and job.get("workflow_evidence") is not None
    ]
    if terminal_with_evidence:
        return RetryClassification.MODEL_REPAIR
    return RetryClassification.ORCHESTRATOR_RETRY


def _recompute_and_persist_sizing(workflow_registry, run):
    """#208 收口 wiring 3：repair／re-claim 成功路徑重算 sizing band。

    輸入條件與 wiring 1（claim 時計算）共用同一份 ``current_sizing_snapshot``
    fail-soft helper——重算得出來就寫回 run（``_manager_update_workflow_run``），
    算不出來（plan 缺宣告欄位、combo 無法解析等）維持現值不動，不得讓既有測試
    變紅。band 若因此跨帶升到 red，由 #223 既有的 dispatch 攔截在下次 dispatch
    時自然生效，這裡不重複路由。
    """

    artifact_rows = [
        {"kind": item.kind, "ref": item.ref} for item in run.planning_authority
    ]
    score, band = current_sizing_snapshot(
        workspace_root=run.workspace_root,
        combo_name=run.combo,
        artifact_rows=artifact_rows,
    )
    if score is None:
        return run
    return workflow_registry._manager_update_workflow_run(
        run.run_id, sizing_score=score, sizing_band=band
    )


def _record_operator_adjudication(
    *,
    run,
    card: str,
    args: dict[str, Any],
    state_path: Path | None,
    now_epoch: float | None,
) -> dict[str, str] | None:
    """#755：`reason` → operator-adjudication evidence 的共用落地（retry-card／retry-build）。

    驗證在呼叫端已完成（非空、有界、state_path 存在）；這裡只負責一致的 body 形狀
    與 content-addressed 寫入。
    """

    adjudication = args.get("reason")
    if adjudication is None:
        return None
    actor_value = args.get("actor")
    return _write_supersede_evidence(
        {
            "schema": OPERATOR_ADJUDICATION_SCHEMA,
            "repo": run.repo,
            "work_id": run.work_id,
            "run_id": run.run_id,
            "card": card,
            "actor": (
                actor_value if isinstance(actor_value, str) and actor_value.strip() else "operator"
            ),
            "reason": adjudication.strip(),
            "phase": run.current_phase,
            "created_at": (
                datetime.fromtimestamp(float(now_epoch), tz=timezone.utc).isoformat()
                if isinstance(now_epoch, (int, float)) and not isinstance(now_epoch, bool)
                else ""
            ),
        },
        state_path=state_path,
        subdir="operator-adjudication",
        label="operator-adjudication",
        max_size=16384,
    )


def operator_adjudication_receipt(
    evidence: dict[str, str] | None, *, card: str
) -> dict[str, object] | None:
    """#814：`--reason` 落地後回給 operator 的收據——明示裁決已記錄、會於下一次該卡 dispatch
    隨 contract 的 `operator_adjudications` 注入 prompt。舊行為只回 `adjudication_evidence`，
    operator 無從得知它是否真的會被 builder 讀到。沒有 evidence（未帶 reason）回 None。
    """

    if evidence is None:
        return None
    return {
        "evidence": dict(evidence),
        "next_step_hint": (
            f"裁決已記錄為 immutable evidence，將於下一次 `{card}` dispatch 隨 contract 的 "
            "`operator_adjudications` 區塊逐字注入 builder／reviewer prompt（兩側讀同一份 evidence）；"
            "不需要再繞 reviewer findings 轉述。"
        ),
    }


def _validate_operator_adjudication_args(
    args: dict[str, Any], *, state_path: Path | None, action: str
) -> None:
    adjudication = args.get("reason")
    if adjudication is None:
        return
    if (
        not isinstance(adjudication, str)
        or not adjudication.strip()
        or len(adjudication) > OPERATOR_ADJUDICATION_REASON_LIMIT
    ):
        raise ValueError(
            f"{action} reason must be a non-empty string within "
            f"{OPERATOR_ADJUDICATION_REASON_LIMIT} characters"
        )
    if state_path is None:
        raise ValueError(f"{action} reason requires a durable state path")


def _retry_build_model_chain_override(
    args: dict[str, Any],
) -> dict[str, dict[str, str]] | None:
    """#205：明示的 retry-build 只允許切換 builder lane。

    planner／reviewer 是既有 run 的凍結身分；讓 recovery request 任意改寫它們
    會繞過原本的 claim-time independence contract。builder 需要換 provider 時，
    由這條窄入口把 override 帶到 registry，後續 dispatch 仍走同一套 identity
    capability／domain fail-closed 驗證。
    """

    override = extract_model_chain_override(args)
    if override is None:
        return None
    if set(override) != {"builder"}:
        raise ValueError("retry-build only permits a builder model-chain override")
    return override


def _retry_card_model_chain_override(
    args: dict[str, Any],
    *,
    phase: str,
) -> dict[str, dict[str, str]] | None:
    """Allow an explicit lane replacement for the card being retried.

    A reviewer recovery must not silently rewrite the frozen builder or planner
    identities (and vice versa for the mid-build card).  The normal dispatch
    identity registry still performs capability and independence-domain checks;
    this helper only narrows which persona the operator may name.
    """

    override = extract_model_chain_override(args)
    if override is None:
        return None
    expected_persona = "builder" if phase == "build" else "reviewer"
    if set(override) != {expected_persona}:
        raise ValueError(
            f"retry-card only permits a {expected_persona} model-chain override"
        )
    return override


def _candidate_tree_paths(
    *, workspace_root: Path, candidate: str, pathspec: str
) -> tuple[str, ...]:
    listed = subprocess.run(
        [
            "git", "-C", str(workspace_root), "ls-tree", "-r", "--name-only", "-z",
            candidate, "--", pathspec,
        ],
        shell=False,
        capture_output=True,
    )
    if listed.returncode != 0:
        raise RuntimeError("retry-build exact candidate tree inspection failed")
    try:
        return tuple(
            value.decode("utf-8")
            for value in listed.stdout.split(b"\0")
            if value
        )
    except UnicodeDecodeError as exc:
        raise RuntimeError("retry-build exact candidate tree inspection failed") from exc


def _candidate_tree_matching_archive_entries(
    *,
    workspace_root: Path,
    candidate: str,
    change: str,
) -> tuple[str, ...]:
    active_paths = _candidate_tree_paths(
        workspace_root=workspace_root,
        candidate=candidate,
        pathspec=f"openspec/changes/{change}/",
    )
    archive_paths = _candidate_tree_paths(
        workspace_root=workspace_root,
        candidate=candidate,
        pathspec="openspec/changes/archive/",
    )
    if not active_paths:
        return ()
    suffix = f"-{change}"
    entries = {
        parts[3]
        for parts in (PurePosixPath(path).parts for path in archive_paths)
        if len(parts) > 4
        and parts[:3] == ("openspec", "changes", "archive")
        and (parts[3] == change or parts[3].endswith(suffix))
    }
    return tuple(sorted(entries))


def _retry_build_action(*, args: dict[str, Any], authority, workflow_registry, state_path: Path | None = None, now_epoch: float | None = None) -> dict[str, Any]:
    """Reopen the final builder card with exact-Candidate CAS after a human stop."""

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_candidate", "reason",
        "planner_executor", "planner_model", "builder_executor", "builder_model",
        "reviewer_executor", "reviewer_model",
    }
    if extras:
        raise ValueError(f"retry-build rejects caller evidence/input: {sorted(extras)[0]}")
    # #755：選填 operator 指示——repair 回合過去只有 stale 的跨卡回饋可看。
    _validate_operator_adjudication_args(args, state_path=state_path, action="retry-build")
    model_chain_override = _retry_build_model_chain_override(args)
    expected_candidate = args.get("expected_candidate")
    if (
        not isinstance(expected_candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(expected_candidate) is None
    ):
        raise ValueError("retry-build requires exact expected_candidate")
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("retry-build issue is not authorized by WorkAuthority")
    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    active = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id == authority.work_id
        and run.status == "ongoing"
        and run.issue_refs == expected_issues
        and _openspec_refs_compatible(run, authority)
    ]
    if len(active) != 1:
        raise RuntimeError("retry-build requires one active canonical WorkflowRun")
    run = active[0]
    if "needs_human" not in run.facets:
        raise RuntimeError("retry-build requires needs_human workflow")
    if run.current_phase not in {"build", "verify", "review"}:
        raise RuntimeError("retry-build requires build/verify/review workflow")
    if run.candidate_head != expected_candidate.lower():
        raise RuntimeError("retry-build expected Candidate CAS mismatch")
    from . import manager

    archive_applied = manager._manager_archive_applied(
        run,
        registry=workflow_registry,
    )
    warnings: list[dict[str, object]] = []
    if len(authority.mapped_openspec) == 1:
        change = authority.mapped_openspec[0]
        archive_entries = _candidate_tree_matching_archive_entries(
            workspace_root=Path(str(run.workspace_root)),
            candidate=run.candidate_head,
            change=change,
        )
        if archive_entries:
            warnings.append(
                {
                    "change": change,
                    "archive_entries": list(archive_entries),
                    "message": (
                        "exact Candidate tree contains both the active OpenSpec change and its "
                        "official archive entry; fix the coexistence before the next review"
                    ),
                }
            )
    retry_classification = _classify_retry(run, workflow_registry)
    if run.current_phase == "build":
        repair_action = (
            "Recover the exact Candidate after a builder terminalization failure. Preserve all "
            "declared input snapshots and inspect any existing unbound worktree commits before "
            "changing files. Fix only real Candidate failures, preserve any Manager-owned official "
            "OpenSpec archive, and do not recreate the active change or claim merge, issue closure, "
            "or done. Commit or adopt a tested descendant Candidate."
        )
    elif archive_applied:
        repair_action = (
            "Repair the exact Candidate after a post-archive verification or review failure. "
            "Inspect any existing worktree repair commits. Preserve the Manager-owned official "
            "OpenSpec archive and fix only real Candidate failures identified by the current "
            "verification/review evidence. Do not recreate the active change or claim merge, issue "
            "closure, or done. If the Candidate already contains a re-created active change "
            "directory for the archived change under openspec/changes/ (outside "
            "openspec/changes/archive/), delete it and keep only the official archive. "
            "Commit or adopt a tested descendant Candidate."
        )
    else:
        repair_action = (
            "Repair the exact Candidate after a delivery preflight failure. Inspect any existing "
            "worktree repair commits, run the authoritative preflight, fix only real Candidate "
            "failures, and make active OpenSpec tasks describe and complete only pre-archive work. "
            "Do not claim archive, merge, issue closure, or done before Manager performs those "
            "actions. Commit or adopt a tested descendant Candidate."
        )
    # 先落 content-addressed 裁決 evidence 再重置 run：evidence 寫入失敗時
    # run 維持 needs_human，不會在缺裁決的情況下重派。
    adjudication_evidence = _record_operator_adjudication(
        run=run,
        card="subagent-build",
        args=args,
        state_path=state_path,
        now_epoch=now_epoch,
    )
    updated = workflow_registry._manager_reset_workflow_for_retry_build(
        run.run_id,
        expected_candidate=expected_candidate.lower(),
        repair_action=repair_action,
        retry_classification=retry_classification.value,
        model_chain_override=model_chain_override,
    )
    updated = _recompute_and_persist_sizing(workflow_registry, updated)
    return {
        "action": "retry-build",
        "adjudication_evidence": adjudication_evidence,
        "adjudication": operator_adjudication_receipt(adjudication_evidence, card="subagent-build"),
        "reason": "candidate-repair-dispatched",
        "expected_candidate": expected_candidate.lower(),
        "warnings": warnings,
        "run": updated.to_dict(),
        "retry_classification": retry_classification,
    }


def _blocking_findings_recovery_actions(run) -> tuple[str, ...]:
    """Return only blocking-findings exits whose reset preconditions are met."""

    reason_payload = getattr(run, "needs_human_reason", None)
    reason_code = (
        reason_payload.get("reason")
        if isinstance(reason_payload, dict)
        else str(reason_payload or "")
    )
    if (
        getattr(run, "current_phase", None) != "review"
        or getattr(run, "status", None) != "ongoing"
        or "needs_human" not in getattr(run, "facets", ())
        or reason_code != "blocking-findings"
    ):
        return ()

    actions: list[str] = []
    candidate = getattr(run, "candidate_head", None)
    verify_steps = [step for step in run.steps if step.phase == "verify"]
    if (
        isinstance(candidate, str)
        and candidate == getattr(run, "verified_head", None)
        and any(item.kind == "plan" for item in run.planning_authority)
        and verify_steps
        and all(step.gate_result == "passed" for step in verify_steps)
    ):
        actions.append("retry-review")

    build_steps = [step for step in run.steps if step.phase == "build"]
    passed_ship_steps = [
        step
        for step in run.steps
        if step.phase == "ship" and step.gate_result == "passed"
    ]
    if (
        isinstance(candidate, str)
        and build_steps
        and all(step.gate_result == "passed" for step in build_steps)
        and len(passed_ship_steps) <= 1
        and all(
            step.card == "openspec-archive"
            and step.executor == "cortex-manager"
            and step.model == "deterministic"
            and step.domain == "cortex"
            for step in passed_ship_steps
        )
    ):
        actions.append("retry-build")
    return tuple(actions)


def blocking_findings_next_step_hint(*, work_id, repo, candidate) -> str:
    """Format the two operator exits for a blocking review finding."""

    work_value = (
        work_id
        if isinstance(work_id, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id)
        else "<work-id>"
    )
    repo_value = (
        repo
        if isinstance(repo, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        else "<owner/repo>"
    )
    candidate_value = (
        candidate
        if isinstance(candidate, str)
        and re.fullmatch(r"[0-9a-fA-F]{40}", candidate)
        else "<candidate-sha>"
    )
    prefix = f"cortex run work {{action}} {work_value} --repo {repo_value} --expected-candidate {candidate_value} --actor <operator> --reason '<裁決>'"
    return (
        "接受 finding：`"
        + prefix.format(action="retry-review")
        + "`\n駁回 finding：`"
        + prefix.format(action="retry-build")
        + "`\n兩者都不適用時才執行 `abandon`。"
    )


def _phase_recovery_actions(run, workflow_registry) -> tuple[str, ...]:
    """#546（部分）：run 停在 needs_human 時真的可用的 recovery 動作。

    `claim._resume_decision` 只看得到 run 的 phase 與 planning failure 記錄，因此
    卡片卡住時它宣告的唯一出口是 `abandon`——實測（run
    ``workflow-084f75e2178cf7547476``）operator 因此以為只能燒掉一個世代，而
    `regenerate-gates`／`retry-card` 其實都可用。這個 helper 在 work action 層
    （拿得到 JobRegistry）補上那段曝光面。

    #569 一般化到 verify／review：同一個現場的 verification 卡（reviewer job 輸出
    損壞、evidence 綁不上）過去在 `next_actions` 裡同樣只看得到 `abandon`，
    operator 因此改用 `retry-verify`——那條路只重置不重派，四小時後 needs_human
    原地回鍋。函式名從 `_build_phase_recovery_actions` 一併改名，因為它已不再只
    覆蓋 build phase。

    刻意**只宣告會被受理的動作**：每一項都用與該動作自身完全相同的前置驗
    （同一份 job/step 判準）判定，拿不準就不宣告。宣告一個保證失敗的動作比不
    宣告更糟——這是 #382 已經付過學費的教訓。
    """

    reason_code = (
        run.needs_human_reason.get("reason")
        if isinstance(getattr(run, "needs_human_reason", None), dict)
        else str(getattr(run, "needs_human_reason", None) or "")
    )

    from .manager import _current_workflow_step
    from .manager import GATE_LEDGER_REQUIRED_PHASES
    from .registry import (
        ACTIVE_JOB_STATUSES,
        MAX_RETRY_CARD_REDISPATCHES,
        RETRY_CARD_PHASE_PERSONA,
        TERMINAL_JOB_STATUSES,
        _retry_card_redispatch_count,
    )

    if (
        run is None
        or workflow_registry is None
        or getattr(run, "status", None) not in ("ongoing", "needs_human")
        or "needs_human" not in getattr(run, "facets", ())
    ):
        return ()

    actions: list[str] = []
    if run.current_phase in RETRY_CARD_PHASE_PERSONA:
        jobs_readable = False
        try:
            jobs = list(workflow_registry.list_jobs())
            jobs_readable = True
        except Exception:  # pragma: no cover - 曝光面不得因讀取失敗而讓 resume 死掉
            jobs = []
        run_jobs = [job for job in jobs if job.get("workflow_run_id") == run.run_id]
        if not any(job.get("status") in ACTIVE_JOB_STATUSES for job in run_jobs):
            # regenerate-gates：與 `_regenerate_gates_action` 同一組 candidate 條件。
            ledger_jobs = [
                job
                for job in run_jobs
                if job.get("workflow_phase") in GATE_LEDGER_REQUIRED_PHASES
                and job.get("status") in TERMINAL_JOB_STATUSES
                and isinstance(job.get("log_path"), str)
                and job.get("log_path")
                and Path(job["log_path"]).is_file()
            ]
            if ledger_jobs:
                worktree = ledger_jobs[-1].get("worktree")
                if isinstance(worktree, str) and Path(worktree).is_dir():
                    actions.append("regenerate-gates")

            # retry-card：與 `_retry_card_action` 同一組前置驗（含 #569 的 reviewer 卡）。
            target = _current_workflow_step(run)
            if target is not None and target.persona == RETRY_CARD_PHASE_PERSONA[run.current_phase]:
                card_jobs = _retry_card_target_jobs(run, run_jobs, card=target.card)
                if (
                    card_jobs
                    and card_jobs[-1].get("status") in TERMINAL_JOB_STATUSES
                    and all(job.get("workflow_evidence") is None for job in card_jobs)
                    and _retry_card_redispatch_count(
                        run.attempts,
                        matching_job_count=len(card_jobs),
                        card_id=target.card,
                    )
                    < MAX_RETRY_CARD_REDISPATCHES
                ):
                    actions.append("retry-card")

            if jobs_readable:
                actions.extend(
                    action
                    for action in _blocking_findings_recovery_actions(run)
                    if action not in actions
                )

    if reason_code.startswith("copilot-") and "review-attest" not in actions:
        actions.append("review-attest")
    return tuple(actions)


def _retry_card_target_jobs(run, jobs, *, card: str) -> list[dict[str, Any]]:
    """`retry-card` 眼中「這張卡的 job」——與 dispatch 的 matching 同一組判準。

    verify／review 的 job 以 candidate 定錨（見
    `manager._dispatch_workflow_card` 的 ``matching``）：要重派／要拒絕的都是
    「這張卡對**現在這個 candidate**」的那幾顆，上一代 candidate 留下的歷史紀錄
    不參與判斷——把它們算進來會讓「retry-build 換過 candidate 之後 reviewer 卡再
    次卡住」變成無解，也就是再造一次本 issue 的 catch-22。
    """

    return [
        job
        for job in jobs
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_phase") == run.current_phase
        and job.get("workflow_card") == card
        # #765：與 advance 選擇同一條 era 判準——authority restart（#373）後，
        # 前代 era 的 job（含其綁定 evidence）是歷史稽核列：拿它們當「已採信不可
        # 重派」的拒絕理由，會讓新 era 的重派無解（實機：verification-38 的舊 era
        # evidence 擋死 retry-card，run 卡在 verify 永不推進）。
        # 缺欄位視為未 pin（legacy／測試 fixture，#379 同型容忍）；帶欄位者必須同 era。
        and job.get("workflow_claim_key") in (None, run.claim_key)
        and (
            run.current_phase == "build"
            or job.get("subject_head") == run.candidate_head
        )
    ]


def _retry_card_action(*, args: dict[str, Any], authority, workflow_registry, state_path: Path | None = None, now_epoch: float | None = None) -> dict[str, Any]:
    """#545／#569：以現行 prompt 重派「當前 phase 內最早一張尚未採信的卡」。

    現場一（#545，run ``workflow-084f75e2178cf7547476`` build phase）：builder
    交付的 RED commit 合格、ledger 已由 ``regenerate-gates`` 重生成正確，但**舊
    job 的 terminal envelope 是模型輸出**（自報 gate 名 ``'focused pytest RED
    expectation'``），契約內不可竄改，``resume`` 重新採信仍必敗於
    ``gate-evidence-unknown-gate``。唯一乾淨出路是以修好的 prompt（#541 已把
    canonical gate 名機械注入 ``allowed_names``）重派那張卡產生**新** envelope，
    但契約內沒有這條路：

    - ``retry-build`` 只受理最後一張 builder 卡（tdd-red 是中段卡），且它是
      candidate 修復語意——會把該卡的 ``action`` 覆寫成 repair 文案，中段卡走那
      條路等於把卡片本身的指示抹掉。
    - ``recover-pre-candidate`` 要求 null candidate（worktree-isolation 早已錨定
      candidate）。
    - ``abandon`` 會燒掉合格的 RED commit 與一個世代。

    現場二（#569，**同一個 run** 的 verify phase）：verification job
    ``wf-865ecb7f70-verification-484``（agy，#568 的權限剖面缺陷）exit 0 但 log
    完全沒有 JSON envelope，harvest 每次都撞
    ``workflow terminal log has no JSON evidence``。形狀與 #545 完全相同——卡片
    最新的終止 job 輸出損壞、evidence 綁不上、harvest 永遠贏過 dispatch——但
    reviewer 卡當時沒有等價出口：``retry-verify`` 是 slice-lane 時代的 **phase
    級**重置，它清掉 needs_human 與 verify step 卻**不在同一個 action 內派新
    job**（實測回應 ``job: None``），run 因此四小時對 tick 隱形後 needs_human
    原地回鍋。本動作因此一併受理 verify／review 的 reviewer 卡。

    本動作**只做重派**：不改任何既有 job 或 envelope（舊紀錄原樣保留供稽核）、
    不動 builder 的 commit 與 worktree、不改判——新 job 的採信仍走既有 harvest
    流程。新 job 的身分由 identity registry 在 dispatch 當下重新解析（reset 會清
    掉該卡的 ``executor``／``model``／``domain``），**不複製舊 job 的身分**——
    #568 的 reviewer fail-over 正依賴這一點。

    fail closed 條件：exact WorkflowRun CAS、run 必須 ongoing 且帶
    ``needs_human``、必須在 build／verify／review phase、指名的卡必須**正是**下
    一次 dispatch 會派的那一張（該 phase 內最早一張非 passed 的卡，且 persona 與
    phase 相符）、該卡不得已有綁定的 ``workflow_evidence``（已採信不可重派、
    evidence immutable）、該卡必須已有一顆終止的 job（沒派過的卡屬 ``resume``
    的職責）、且 run 不得有 active job。任一條不成立即拒絕，不做任何 side
    effect。
    """

    # 與 dispatch 端共用同一個「下一張要派哪張卡」判準——宣告可行的重派必須
    # 真的落在同一張卡上（#382：宣告與實作不得各自為政）。
    from .manager import _current_workflow_step
    from .registry import (
        MAX_RETRY_CARD_REDISPATCHES,
        RETRY_CARD_PHASE_PERSONA,
        TERMINAL_JOB_STATUSES,
        _retry_card_budget_message,
        _retry_card_budget_reason,
        _retry_card_redispatch_count,
    )

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_run_id", "card",
        "reason",
        "planner_executor", "planner_model", "builder_executor", "builder_model",
        "reviewer_executor", "reviewer_model",
    }
    if extras:
        raise ValueError(f"retry-card rejects caller evidence/input: {sorted(extras)[0]}")
    # #752：選填的 operator 裁決文字。驗在最前面——裁決寫不進去就不該動 run。
    _validate_operator_adjudication_args(args, state_path=state_path, action="retry-card")
    expected_run_id = args.get("expected_run_id")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("retry-card requires exact expected_run_id")
    card = args.get("card")
    if (
        not isinstance(card, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", card) is None
    ):
        raise ValueError("retry-card requires exact card id")
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("retry-card issue is not authorized by WorkAuthority")
    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    active = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id == authority.work_id
        and run.status == "ongoing"
        and run.issue_refs == expected_issues
        and _openspec_refs_compatible(run, authority)
    ]
    if len(active) != 1:
        raise RuntimeError("retry-card requires one active canonical WorkflowRun")
    run = active[0]
    if run.run_id != expected_run_id:
        raise RuntimeError("retry-card expected WorkflowRun CAS mismatch")
    if "needs_human" not in run.facets:
        raise RuntimeError("retry-card requires needs_human workflow")
    if run.current_phase not in RETRY_CARD_PHASE_PERSONA:
        raise RuntimeError("retry-card requires build/verify/review-phase workflow")
    model_chain_override = _retry_card_model_chain_override(
        args, phase=run.current_phase
    )
    expected_persona = RETRY_CARD_PHASE_PERSONA[run.current_phase]
    target = _current_workflow_step(run)
    if target is None or target.persona != expected_persona:
        raise RuntimeError(f"retry-card requires a pending {expected_persona} card")
    if target.card != card:
        raise RuntimeError("retry-card expected card mismatch")
    card_jobs = _retry_card_target_jobs(
        run, workflow_registry.list_jobs(), card=card
    )
    latest_card_job = card_jobs[-1] if card_jobs else None
    accepted_evidence = any(
        job.get("workflow_evidence") is not None for job in card_jobs
    )
    # A builder repair can be dispatched after a previously accepted attempt
    # when an operator adjudication identifies a real defect.  If that newer
    # attempt then dies before producing an envelope (provider failure, for
    # example), the old evidence is immutable history, not evidence for the
    # current pending attempt.  Permit exactly this narrow recovery shape:
    # build phase, latest job terminal and non-zero/failed, no evidence on the
    # latest job.  Reviewer cards and accepted latest attempts remain blocked.
    superseded_by_failed_builder = bool(
        run.current_phase == "build"
        and latest_card_job is not None
        and latest_card_job.get("workflow_evidence") is None
        and latest_card_job.get("status") in TERMINAL_JOB_STATUSES
        and (
            latest_card_job.get("status") == "failed"
            or latest_card_job.get("exit_code") not in (None, 0)
        )
    )
    if accepted_evidence and not superseded_by_failed_builder:
        # 已採信的 evidence 不可重寫，也不得以「重派」名義繞過——那張卡已經有
        # 被系統採信的結論了。只有上面明確限定的 newer failed builder attempt
        # 能把舊 evidence 留在稽核歷史、重新開放當前 pending attempt。
        raise RuntimeError("retry-card refuses a card with accepted evidence")
    if not card_jobs or latest_card_job.get("status") not in TERMINAL_JOB_STATUSES:
        raise RuntimeError("retry-card requires a terminal job for the card")
    retry_card_count = _retry_card_redispatch_count(
        run.attempts,
        matching_job_count=len(card_jobs),
        card_id=card,
    )
    if retry_card_count >= MAX_RETRY_CARD_REDISPATCHES:
        workflow_registry._manager_update_workflow_run(
            run.run_id,
            needs_human_reason=_retry_card_budget_reason(
                card,
                retry_card_count,
                source="coordinator.work_actions._retry_card_action",
            ),
        )
        raise RuntimeError(_retry_card_budget_message(card, retry_card_count))
    # candidate 完全沒變的 reviewer 重派不是模型修復，比照 `retry-verify`／
    # `retry-review` 的既有分類（#208 根因3：不得計入 model failure 指標，也不得
    # 吃 #218 的 repair budget）。build phase 維持 #545 的狀態推論不變。
    retry_classification = _classify_retry(
        run,
        workflow_registry,
        trigger=_RETRY_CARD_PHASE_TRIGGERS.get(run.current_phase),
    )
    # #752／#755：operator 裁決經 Manager 落地為 immutable evidence——dispatch 端由
    # `manager._operator_adjudications()` 讀回、進 retry_context 的
    # `operator_adjudications` 鍵（bounded CLI、Manager-owned，非 candidate 內容）。
    # 先落 content-addressed 裁決 evidence 再重置 run：evidence 寫入失敗時
    # run 維持 needs_human，不會在缺裁決的情況下重派。
    adjudication_evidence = _record_operator_adjudication(
        run=run,
        card=card,
        args=args,
        state_path=state_path,
        now_epoch=now_epoch,
    )
    updated = workflow_registry._manager_reset_workflow_for_retry_card(
        run.run_id,
        expected_run_id=expected_run_id,
        card=card,
        retry_classification=retry_classification.value,
        model_chain_override=model_chain_override,
    )
    updated = _recompute_and_persist_sizing(workflow_registry, updated)
    return {
        "action": "retry-card",
        "adjudication_evidence": adjudication_evidence,
        "adjudication": operator_adjudication_receipt(adjudication_evidence, card=card),
        "reason": f"{expected_persona}-card-redispatched",
        "expected_run_id": expected_run_id,
        "run_id": run.run_id,
        "card_id": card,
        # 舊 job 一個都不動；列出來只是讓 operator 知道稽核時該對照哪幾筆。
        "superseded_job_ids": [str(job.get("job_id")) for job in card_jobs],
        "run": updated.to_dict(),
        "retry_classification": retry_classification,
    }


def _retry_verify_action(*, args: dict[str, Any], authority, workflow_registry) -> dict[str, Any]:
    """Rerun verification only for the exact unchanged Candidate after a human stop（#216 AC2）。

    build phase 完全不動：不重派 builder、不重建 candidate，只把 verify step
    打回 pending 讓 verification runner 針對既有 Candidate 重跑。
    """

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_candidate",
    }
    if extras:
        raise ValueError(f"retry-verify rejects caller evidence/input: {sorted(extras)[0]}")
    expected_candidate = args.get("expected_candidate")
    if (
        not isinstance(expected_candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(expected_candidate) is None
    ):
        raise ValueError("retry-verify requires exact expected_candidate")
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("retry-verify issue is not authorized by WorkAuthority")
    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    active = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id == authority.work_id
        and run.status == "ongoing"
        and run.issue_refs == expected_issues
        and _openspec_refs_compatible(run, authority)
    ]
    if len(active) != 1:
        raise RuntimeError("retry-verify requires one active canonical WorkflowRun")
    run = active[0]
    if "needs_human" not in run.facets:
        raise RuntimeError("retry-verify requires needs_human workflow")
    if run.current_phase != "verify":
        raise RuntimeError("retry-verify requires verify-phase workflow")
    if run.candidate_head != expected_candidate.lower():
        raise RuntimeError("retry-verify expected Candidate CAS mismatch")
    # candidate 不變的 verification 重跑屬 orchestration 層原因，非模型修復。
    retry_classification = _classify_retry(
        run, workflow_registry, trigger="verification-rerun"
    )
    updated = workflow_registry._manager_reset_workflow_for_retry_verify(
        run.run_id,
        expected_candidate=expected_candidate.lower(),
        retry_classification=retry_classification.value,
    )
    updated = _recompute_and_persist_sizing(workflow_registry, updated)
    return {
        "action": "retry-verify",
        "reason": "verification-rerun-dispatched",
        "expected_candidate": expected_candidate.lower(),
        "run": updated.to_dict(),
        "retry_classification": retry_classification,
    }


def _retry_review_action(
    *,
    args: dict[str, Any],
    authority,
    workflow_registry,
    state_path: Path | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Relaunch foreign review only for the exact verified Candidate（#216 AC3）。

    build／verify phase 完全不動：不重跑 builder、不重建 candidate。若 run 缺少
    冷凍 plan authority（尚未經 plan review freeze），在任何狀態變更前直接
    fail-closed（pre-dispatch fail），不派任何 review job。
    """

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_candidate", "reason",
    }
    if extras:
        raise ValueError(f"retry-review rejects caller evidence/input: {sorted(extras)[0]}")
    _validate_operator_adjudication_args(
        args, state_path=state_path, action="retry-review"
    )
    expected_candidate = args.get("expected_candidate")
    if (
        not isinstance(expected_candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(expected_candidate) is None
    ):
        raise ValueError("retry-review requires exact expected_candidate")
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("retry-review issue is not authorized by WorkAuthority")
    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    active = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id == authority.work_id
        and run.status == "ongoing"
        and run.issue_refs == expected_issues
        and _openspec_refs_compatible(run, authority)
    ]
    if len(active) != 1:
        raise RuntimeError("retry-review requires one active canonical WorkflowRun")
    run = active[0]
    if "needs_human" not in run.facets:
        raise RuntimeError("retry-review requires needs_human workflow")
    if run.current_phase != "review":
        raise RuntimeError("retry-review requires review-phase workflow")
    if (
        run.candidate_head != expected_candidate.lower()
        or run.verified_head != expected_candidate.lower()
    ):
        raise RuntimeError("retry-review expected Candidate CAS mismatch")
    if not any(item.kind == "plan" for item in run.planning_authority):
        raise RuntimeError("retry-review requires frozen plan authority pre-dispatch")
    from .manager import _current_workflow_step

    target = _current_workflow_step(run)
    card = (
        target.card
        if target is not None
        else next(
            (step.card for step in reversed(run.steps) if step.phase == "review"),
            "review",
        )
    )
    # 重跑 review 本身即是 review 交接修復：candidate 未變，不是 model repair。
    retry_classification = _classify_retry(
        run, workflow_registry, trigger="review-handoff-failure"
    )
    # 先落 content-addressed 裁決 evidence 再重置 run：evidence 寫入失敗時
    # run 維持 needs_human，不會在缺裁決的情況下重派。
    adjudication_evidence = _record_operator_adjudication(
        run=run,
        card=card,
        args=args,
        state_path=state_path,
        now_epoch=now_epoch,
    )
    updated = workflow_registry._manager_reset_workflow_for_retry_review(
        run.run_id,
        expected_candidate=expected_candidate.lower(),
        retry_classification=retry_classification.value,
    )
    updated = _recompute_and_persist_sizing(workflow_registry, updated)
    return {
        "action": "retry-review",
        "adjudication_evidence": adjudication_evidence,
        "adjudication": operator_adjudication_receipt(adjudication_evidence, card=card),
        "reason": "foreign-review-rerun-dispatched",
        "expected_candidate": expected_candidate.lower(),
        "run": updated.to_dict(),
        "retry_classification": retry_classification,
    }


def _validate_abandon_evidence_target(
    target: Path, content: bytes, *, label: str = "abandon", max_size: int = 4096
) -> None:
    # ``label`` 只影響錯誤訊息（#519 讓 reclaim-reset 說自己的名字），判準本身
    # 對每一種 supersede-family evidence 完全一致。``max_size`` 是「evidence 是
    # 小文件」的防禦性上界：abandon／retire-delivered 的 body 欄位固定，4096 綽
    # 綽有餘；#519 的 reclaim-reset body 則隨被赦免的世代數線性成長，沿用 4096
    # 會讓「世代夠多時，byte-for-byte 相同的重放被誤判成 conflict」——冪等重入
    # 因此必然 fail。上界逐 caller 指定，判準其餘部分完全共用。
    try:
        metadata = target.stat()
        conflict = (
            target.is_symlink()
            or not target.is_file()
            or metadata.st_size != len(content)
            or metadata.st_size > max_size
            or metadata.st_mode & 0o222
            or target.read_bytes() != content
        )
    except OSError as error:
        raise RuntimeError(f"workflow {label} evidence conflict") from error
    if conflict:
        raise RuntimeError(f"workflow {label} evidence conflict")


# #776：helper 下沉至 claim.py 供 work_bridge 的 ship 守衛共用（work_bridge
# 不能反向 import 本模組）；底線別名保留既有呼叫點與測試的引用名。
_planning_declared_openspec_changes = claim_planning_declared_openspec_changes
_openspec_refs_compatible = claim_openspec_refs_compatible


def _write_supersede_evidence(
    body: dict[str, Any],
    *,
    state_path: Path,
    subdir: str,
    stem: str | None = None,
    label: str = "abandon",
    max_size: int = 4096,
) -> dict[str, str]:
    """Durable, content-addressed supersede-evidence writer shared by the
    ``work-abandon`` and ``work-retire-delivered`` paths (create-with-O_EXCL +
    hardlink + fsync; a colliding target is re-validated byte-for-byte, so a
    replayed write is idempotent rather than a conflict).

    ``stem`` overrides the filename prefix for records that are not scoped to a
    single run — #519's ``work-reclaim-reset`` is keyed by ``work_id`` because
    a reclaim-budget reset is a work-item-level authorization, not a run-level
    one. Everything else about the durability contract is shared verbatim.
    """

    digest = verification.canonical_json_hash(body)
    root = state_path.resolve().parent / "evidence" / subdir
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{stem if stem is not None else body['run_id']}-{digest}.json"
    content = (
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if target.exists():
        _validate_abandon_evidence_target(target, content, label=label, max_size=max_size)
    else:
        temporary = root / f".{target.name}.{uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError(
                f"workflow {label} evidence temporary collision"
            ) from error
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), 0o444)
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                _validate_abandon_evidence_target(
                    target, content, label=label, max_size=max_size
                )
            else:
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ref": str(target), "hash": digest}


def _abandon_record(body: dict[str, Any], *, state_path: Path) -> dict[str, str]:
    return _write_supersede_evidence(body, state_path=state_path, subdir="work-abandon")


def _retire_delivered_record(body: dict[str, Any], *, state_path: Path) -> dict[str, str]:
    return _write_supersede_evidence(
        body, state_path=state_path, subdir="work-retire-delivered"
    )


def _reclaim_reset_body(
    *,
    repo: str,
    work_id: str,
    actor: str,
    reason: str,
    cleared_run_ids: list[str],
    created_at: str,
) -> dict[str, Any]:
    """#519：`cortex-work-reclaim-reset/v1` 的 canonical evidence body。

    內容即稽核事實：誰、為什麼、在什麼時間點、赦免了哪幾個 superseded 世代。
    `cleared_run_ids` 排序後入 body，讓同一組世代永遠算出同一個 canonical hash。
    """

    return {
        "schema": "cortex-work-reclaim-reset/v1",
        "repo": repo,
        "work_id": work_id,
        "actor": actor,
        "reason": reason,
        "superseded_generations": len(cleared_run_ids),
        "cleared_run_ids": sorted(cleared_run_ids),
        "created_at": created_at,
    }


def _reclaim_reset_record(body: dict[str, Any], *, state_path: Path) -> dict[str, str]:
    return _write_supersede_evidence(
        body,
        state_path=state_path,
        subdir="work-reclaim-reset",
        stem=str(body["work_id"]),
        label="reclaim-reset",
        # body 隨 cleared_run_ids 線性成長（每筆 run_id 約 40 bytes），共用 4096
        # 會讓世代較多的重置一重放就誤判 conflict；放寬到 64KiB 仍是「小文件」
        # 的防禦性上界，且 st_size != len(content) 才是真正的正確性判準。
        max_size=65536,
    )


def _read_planning_failure_record(
    *,
    run,
    run_id: str,
) -> dict[str, Any]:
    """讀取 run 指定的 planning 失敗 evidence（僅供 recover-planning 決策）。"""

    def load_record(path: Path) -> dict[str, Any] | None:
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or not path.parent.name == "planning-recovery"
        ):
            return None
        try:
            payload = path.read_text(encoding="utf-8")
            body = json.loads(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(body, dict):
            return None
        return body

    matches = []
    for path_value in run.evidence_refs:
        if not isinstance(path_value, str):
            continue
        record = load_record(Path(path_value))
        if (
            record is None
            or record.get("schema") != "cortex-planning-failure/v1"
            or record.get("run_id") != run_id
        ):
            continue
        classification = record.get("classification")
        reason = record.get("reason")
        if (
            classification not in {"environment", "content"}
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            continue
        matches.append(
            {
                "classification": classification,
                "reason": reason,
                "evidence_ref": path_value,
            }
        )
    if not matches:
        raise RuntimeError("recover-planning requires planning failure evidence")
    if len(matches) > 1:
        raise RuntimeError("recover-planning planning failure evidence ambiguous")
    return matches[0]


def _planning_failure_hint(run) -> dict[str, Any] | None:
    """#256 R2：resume 用的唯讀 planning 失敗判讀，讀不到／有歧義一律回 None。

    與 `_read_planning_failure_record` 共用同一份 evidence 判準，差別只在這裡
    是提示用途（決定 resume 要浮現哪些 next_actions），不得因此放寬任何
    fail-closed 行為——`recover-planning` 本身仍會重新做完整驗證。
    """

    if run is None:
        return None
    try:
        return _read_planning_failure_record(run=run, run_id=run.run_id)
    except (RuntimeError, OSError, AttributeError):
        return None


def _recover_planning_record(
    run,
    *,
    state_path: Path,
    actor: str,
    failure_classification: str,
    failure_reason: str,
    evidence_ref: str,
    recovered_phase: str,
) -> dict[str, str]:
    body = {
        "schema": "cortex-work-planning-recovery/v1",
        "run_id": run.run_id,
        "source_revision": run.source_revision,
        "actor": actor,
        # #256 R4：稽核必須含恢復前後的 run 狀態，不能只留觸發者與判定依據。
        "previous_phase": run.current_phase,
        "previous_facets": sorted(run.facets),
        "recovered_phase": recovered_phase,
        "failure_classification": failure_classification,
        "failure_reason": failure_reason,
        "failure_evidence_ref": evidence_ref,
        "recovery_basis": "planning-runtime-retry",
    }
    digest = verification.canonical_json_hash(body)
    root = state_path.resolve().parent / "evidence" / "planning-recovery"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{run.run_id}-{digest}.json"
    content = (
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != content:
            raise RuntimeError("workflow planning recovery evidence conflict")
    else:
        temporary = root / f".{target.name}.{uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError("workflow planning recovery evidence collision") from error
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), 0o444)
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != content:
                    raise RuntimeError("workflow planning recovery evidence conflict")
            else:
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ref": str(target), "hash": digest}


def _gc_one_abandoned_planning_artifact(item, *, workspace_root: Path) -> None:
    """單一 planning artifact 的 best-effort GC；只在確定安全時才刪檔。

    #416：`item`（`PlanningArtifactAuthority`）記錄 brainstorm define 成功發佈
    這份 artifact 時的 ref／baseline_sha256。abandon 之後，只有「未被 git
    追蹤」且「現存內容 hash 與發佈時 baseline 完全相符」才視為安全可回滾的
    發佈殘留；任何不確定（已追蹤、hash 不符、symlink、tracking 狀態查不到）
    一律留檔——寧可留下需要人工清的殘留，也不能誤刪 operator 內容。呼叫端
    （`_gc_abandoned_planning_artifacts`）負責吞掉這裡拋出的例外，這裡本身
    保持該拋就拋，讓呼叫端統一記 diagnostics。
    """

    relative = Path(item.ref)
    if relative.is_absolute() or ".." in relative.parts:
        # `PlanningArtifactAuthority.__post_init__` 已擋過一次，這裡是縱深防禦。
        raise ValueError(f"planning authority ref outside workspace: {item.ref}")
    resolved_root = workspace_root.resolve()
    path = (resolved_root / relative).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"planning authority ref escapes workspace: {item.ref}") from exc
    if path.is_symlink() or not path.is_file():
        # 已不存在（可能已被清過或本來就沒發佈成功）、或不是一般檔案——
        # 兩者都不是本 GC 的安全操作範圍，視為 no-op。
        return
    tracked = subprocess.run(
        ["git", "-C", str(resolved_root), "ls-files", "--error-unmatch", "--", str(relative)],
        shell=False,
        capture_output=True,
        text=True,
    )
    if tracked.returncode == 0:
        logger.info(
            "planning-artifact-gc-skip-tracked ref=%s reason=git-tracked", item.ref,
        )
        return  # git 已追蹤：不是「未提交的發佈殘留」，交給 git 正常流程管理。
    if tracked.returncode != 1:
        raise RuntimeError(
            f"planning artifact git tracking state unavailable: {item.ref}: "
            f"{tracked.stderr.strip()[:200]}"
        )
    actual_hash = verification.sha256_bytes(path.read_bytes())
    if actual_hash != item.baseline_sha256:
        logger.warning(
            "planning-artifact-gc-skip-hash-drift ref=%s expected=%s actual=%s",
            item.ref, item.baseline_sha256, actual_hash,
        )
        return  # operator 手動改過：不可信任 baseline 已過期，留檔待人工判斷。
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    logger.info("planning-artifact-gc-removed ref=%s", item.ref)


def _gc_abandoned_planning_artifacts(
    run,
    *,
    workflow_registry=None,
    coordinator_root: Path | None = None,
) -> None:
    """abandon 終態化之後，盡力回收已發佈未提交的 planning artifacts。

    #416 根因：`_PlanningPublicationTransaction` 的 rollback 只在 define 流程
    內部失敗時觸發；一旦成功 commit（`run.planning_authority` 落地、journal
    刪除），run 若隨後被 abandon，沒人知道要回滾這些已發佈檔案。下一世代重新
    claim 後 brainstorm 對同一 destinations 再發佈時，`_publish_planning_
    artifacts` 對「檔案已存在但無對應 authority」一律 fail-closed 拒收，殘留檔
    變成死鎖地雷。

    GC 的安全邊界由 `_gc_one_abandoned_planning_artifact` 逐項把關（未追蹤 +
    hash 相符才刪）；這裡只負責逐項呼叫、吞掉任何例外——GC 是 abandon 的附帶
    效果，不得讓 abandon 本身因為 GC 失敗而失敗，失敗只記 diagnostics。
    """

    if workflow_registry is not None and coordinator_root is not None:
        try:
            from . import manager

            report = manager.reconcile_planning_transactions(
                registry=workflow_registry,
                coordinator_root=coordinator_root,
                run_id=run.run_id,
                grace_seconds=0,
            )
            for item in report:
                logger.info(
                    "planning-transaction-abandon-reconcile run_id=%s outcome=%s",
                    item.get("run_id"),
                    item.get("outcome", "unchanged"),
                )
        except Exception as exc:  # noqa: BLE001 - best-effort：GC 不得讓 abandon 失敗
            logger.warning(
                "planning-transaction-abandon-reconcile-failed run_id=%s error=%s: %s",
                run.run_id, type(exc).__name__, str(exc)[:200],
            )

    workspace_root = Path(run.workspace_root)
    for item in run.planning_authority:
        try:
            _gc_one_abandoned_planning_artifact(item, workspace_root=workspace_root)
        except Exception as exc:  # noqa: BLE001 - best-effort：GC 不得讓 abandon 失敗
            logger.warning(
                "planning-artifact-gc-failed run_id=%s ref=%s error=%s: %s",
                run.run_id, item.ref, type(exc).__name__, str(exc)[:200],
            )


def _reclaim_abandoned_build_worktrees(run, workflow_registry, *, state_path: Path) -> None:
    """#478／#527／#613：abandon 後回收 build worktree，並安全退役 build branch。

    #527 的根因之一是 supersede 只改 run 狀態、不動 build worktree——那份
    worktree 連同它在 git registry 的記錄留著，下一世代重派同名分支時
    `git worktree add` 直接以「branch used by worktree at ...」失敗。這裡與
    `_gc_abandoned_planning_artifacts` 走同一個掛載點與同一套 best-effort 紀律：
    回收只能是 abandon 的附帶效果，任何失敗只落 diagnostics，不得讓已經成立的
    abandon 反悔（abandon 的 evidence 與終態轉換此時皆已 durable）。

    範圍以 job 的 `workflow_run_id` 精準框定——與 `engineering_outcome.
    emit_outcome` 同一條歸屬判準，不用 work_id 前綴猜。
    """

    try:
        jobs = workflow_registry.list_jobs()
    except Exception as exc:  # noqa: BLE001 - best-effort：不得讓 abandon 失敗
        logger.warning(
            "build-worktree-reclaim-listing-failed run_id=%s error=%s: %s",
            run.run_id, type(exc).__name__, str(exc)[:200],
        )
        return
    targets = [
        job.get("worktree")
        for job in jobs
        if job.get("workflow_run_id") == run.run_id
        and isinstance(job.get("worktree"), str)
        and job.get("worktree")
    ]
    results = []
    if targets:
        try:
            reclaim_options = {
                "preserve_root": state_path.resolve().parent / "evidence"
            }
            if isinstance(getattr(run, "workspace_root", None), str):
                reclaim_options["repo_root"] = Path(run.workspace_root)
            results = worktree_reclaim.reclaim_worktrees(targets, **reclaim_options)
        except Exception as exc:  # noqa: BLE001 - best-effort：不得讓 abandon 失敗
            logger.warning(
                "build-worktree-reclaim-failed run_id=%s error=%s: %s",
                run.run_id, type(exc).__name__, str(exc)[:200],
            )
            return
    failed = False
    for result in results:
        if result.ok:
            continue
        failed = True
        logger.warning(
            "build-worktree-reclaim-failed run_id=%s path=%s detail=%s",
            run.run_id, result.path, result.detail,
        )
    if failed:
        return

    try:
        from . import manager

        repo_root = Path(run.workspace_root).resolve()
        branch = manager.workflow_build_branch(run)
    except Exception as exc:  # noqa: BLE001 - best-effort：不得讓 abandon 失敗
        logger.warning(
            "build-branch-reclaim-identity-failed run_id=%s error=%s: %s",
            run.run_id, type(exc).__name__, str(exc)[:200],
        )
        return
    try:
        other_runs = workflow_registry.list_workflow_runs()
    except Exception as exc:  # noqa: BLE001 - 無法排除其他 run 使用時保留 branch
        logger.warning(
            "build-branch-reclaim-run-list-failed run_id=%s error=%s: %s",
            run.run_id, type(exc).__name__, str(exc)[:200],
        )
        return
    for other in other_runs:
        if (
            other.run_id == run.run_id
            or other.status != "ongoing"
            or other.repo != run.repo
        ):
            continue
        try:
            other_branch = manager.workflow_build_branch(other)
        except Exception as exc:  # noqa: BLE001 - 無法判斷時 fail-closed 保留 branch
            logger.warning(
                "build-branch-reclaim-other-run-identity-failed run_id=%s other_run_id=%s error=%s: %s",
                run.run_id, getattr(other, "run_id", "unknown"), type(exc).__name__, str(exc)[:200],
            )
            return
        if other_branch == branch:
            logger.info(
                "build-branch-reclaim-in-use run_id=%s other_run_id=%s branch=%s",
                run.run_id, other.run_id, branch,
            )
            return

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
        )

    top_level = git("rev-parse", "--show-toplevel")
    if (
        top_level.returncode != 0
        or Path(top_level.stdout.strip()).resolve() != repo_root
    ):
        logger.warning("build-branch-reclaim-repo-unavailable run_id=%s", run.run_id)
        return
    if git("check-ref-format", "--branch", branch).returncode != 0:
        logger.warning("build-branch-reclaim-name-invalid run_id=%s", run.run_id)
        return
    branch_ref = f"refs/heads/{branch}"
    branch_exists = git("show-ref", "--verify", "--quiet", branch_ref)
    if branch_exists.returncode == 1:
        logger.info("build-branch-reclaim-absent run_id=%s branch=%s", run.run_id, branch)
        return
    if branch_exists.returncode != 0:
        logger.warning(
            "build-branch-reclaim-read-failed run_id=%s branch=%s detail=%s",
            run.run_id, branch, branch_exists.stderr.strip()[:200],
        )
        return
    branch_proc = git("rev-parse", "--verify", f"{branch_ref}^{{commit}}")
    if branch_proc.returncode != 0:
        logger.warning(
            "build-branch-reclaim-read-failed run_id=%s branch=%s detail=%s",
            run.run_id, branch, branch_proc.stderr.strip()[:200],
        )
        return
    branch_sha = branch_proc.stdout.strip().lower()

    base_sha = None
    frozen = getattr(run, "frozen_readiness", None)
    if isinstance(frozen, dict):
        candidate_base = frozen.get("base_sha")
        if isinstance(candidate_base, str) and verification.SAFE_SHA_RE.fullmatch(candidate_base):
            base_sha = candidate_base.lower()
    if base_sha is None:
        for job in jobs:
            if (
                job.get("workflow_run_id") == run.run_id
                and job.get("persona") == "builder"
                and job.get("workflow_phase") == "build"
            ):
                dispatch_head = job.get("dispatch_head")
                if isinstance(dispatch_head, str) and verification.SAFE_SHA_RE.fullmatch(dispatch_head):
                    base_sha = dispatch_head.lower()
                    break
    if base_sha is None:
        main_proc = git("rev-parse", "--verify", "refs/heads/main^{commit}")
        if main_proc.returncode == 0:
            base_sha = main_proc.stdout.strip().lower()
    if base_sha is None:
        logger.warning("build-branch-reclaim-base-unavailable run_id=%s branch=%s", run.run_id, branch)
        return

    count_proc = git("rev-list", "--count", f"{base_sha}..{branch_sha}")
    if count_proc.returncode != 0:
        logger.warning(
            "build-branch-reclaim-ancestry-unavailable run_id=%s branch=%s detail=%s",
            run.run_id, branch, count_proc.stderr.strip()[:200],
        )
        return
    try:
        has_unbased_commits = int(count_proc.stdout.strip()) > 0
    except ValueError:
        logger.warning("build-branch-reclaim-count-invalid run_id=%s branch=%s", run.run_id, branch)
        return

    archive_tag = None
    if has_unbased_commits:
        safe_work_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(run.work_id)).strip(".-") or "work"
        archive_tag = f"archive/{safe_work_id}-{branch_sha[:8]}"
        tag_ref = f"refs/tags/{archive_tag}"
        if git("check-ref-format", tag_ref).returncode != 0:
            logger.warning(
                "build-branch-reclaim-archive-tag-invalid run_id=%s tag=%s",
                run.run_id, archive_tag,
            )
            return
        existing = git("show-ref", "--verify", "--quiet", tag_ref)
        if existing.returncode == 0:
            existing_sha = git("rev-parse", "--verify", f"{tag_ref}^{{commit}}")
            if existing_sha.returncode != 0 or existing_sha.stdout.strip().lower() != branch_sha:
                logger.warning(
                    "build-branch-reclaim-archive-tag-conflict run_id=%s tag=%s",
                    run.run_id, archive_tag,
                )
                return
        elif existing.returncode == 1:
            created = git("tag", archive_tag, branch_sha)
            if created.returncode != 0:
                logger.warning(
                    "build-branch-reclaim-archive-tag-failed run_id=%s tag=%s detail=%s",
                    run.run_id, archive_tag, created.stderr.strip()[:200],
                )
                return
        else:
            logger.warning(
                "build-branch-reclaim-archive-tag-check-failed run_id=%s tag=%s detail=%s",
                run.run_id, archive_tag, existing.stderr.strip()[:200],
            )
            return

    removed = git("branch", "-D", "--", branch)
    if removed.returncode != 0:
        logger.warning(
            "build-branch-reclaim-failed run_id=%s branch=%s detail=%s",
            run.run_id, branch, removed.stderr.strip()[:200],
        )
        return
    logger.info(
        "build-branch-reclaimed run_id=%s branch=%s sha=%s archive_tag=%s",
        run.run_id, branch, branch_sha, archive_tag or "none",
    )


def _superseded_abandon_body(
    run,
    *,
    state_path: Path,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    root = state_path.resolve().parent / "evidence" / "work-abandon"
    candidates: list[Path] = []
    pattern = re.compile(rf"{re.escape(run.run_id)}-([0-9a-f]{{64}})\.json")
    for value in run.evidence_refs:
        path = Path(value)
        if (
            not path.is_absolute()
            or path.parent != root
            or pattern.fullmatch(path.name) is None
        ):
            continue
        candidates.append(path)
    if len(candidates) != 1:
        raise RuntimeError("WorkflowRun was superseded by different authority")
    target = candidates[0]
    try:
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_mode & 0o222
            or target.stat().st_size > 4096
        ):
            raise RuntimeError("WorkflowRun was superseded by different authority")
        raw = target.read_bytes()
        body = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "WorkflowRun was superseded by different authority"
        ) from error
    required = {
        "schema",
        "repo",
        "work_id",
        "run_id",
        "authority_digest",
        "actor",
        "reason",
    }
    if (
        not isinstance(body, dict)
        or set(body) != required
        or body.get("schema") != "cortex-work-abandon/v1"
        or body.get("repo") != run.repo
        or body.get("work_id") != run.work_id
        or body.get("run_id") != run.run_id
        or re.fullmatch(r"[0-9a-f]{64}", str(body.get("authority_digest"))) is None
        or body.get("actor") != actor
        or body.get("reason") != reason
    ):
        raise RuntimeError("WorkflowRun was superseded by different authority")
    content = (
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    digest = verification.canonical_json_hash(body)
    if raw != content or target.name != f"{run.run_id}-{digest}.json":
        raise RuntimeError("WorkflowRun was superseded by different authority")
    return body


def _recover_superseded_action(
    *,
    args: dict[str, Any],
    authority,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """#776：把被 resume 識別失誤 supersede 的已驗證 run 撿回。

    只受理「有 candidate_head、pr_refs 非空、phase 停在 verify/review、同
    (repo, work_id) 無 ongoing run、無 active job」的 superseded run——即
    build 成果與 PR 都在、只是識別鏈斷裂被錯誤作廢的那種。動作＝status 復歸
    ongoing 後立即走 official ``_manager_reset_workflow_for_authority_restart``
    （#216 AC5 語意：verify/review 打回 pending、claim_key/source_revision 對齊
    現 authority、build/candidate/PR 保留），不發明第三種恢復語意。
    """

    from .registry import ACTIVE_JOB_STATUSES

    extras = set(args) - {
        "action", "repo", "work_id", "actor", "expected_run_id", "reason",
    }
    if extras:
        raise ValueError(
            f"recover-superseded rejects caller evidence/input: {sorted(extras)[0]}"
        )
    expected_run_id = args.get("expected_run_id")
    actor = args.get("actor")
    reason = args.get("reason")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("recover-superseded requires exact expected_run_id")
    if (
        not isinstance(actor, str)
        or actor != actor.strip()
        or not 1 <= len(actor) <= 128
        or not actor.isprintable()
    ):
        raise ValueError("recover-superseded requires bounded actor")
    if (
        not isinstance(reason, str)
        or reason != reason.strip()
        or not 1 <= len(reason) <= 500
        or not reason.isprintable()
    ):
        raise ValueError("recover-superseded requires bounded reason")
    related = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo and run.work_id == authority.work_id
    ]
    exact = [run for run in related if run.run_id == expected_run_id]
    if len(exact) != 1:
        raise RuntimeError("recover-superseded expected WorkflowRun CAS mismatch")
    run = exact[0]
    if run.status != "superseded":
        raise RuntimeError("recover-superseded requires superseded run")
    if not run.candidate_head or not run.pr_refs:
        raise RuntimeError(
            "recover-superseded requires delivered candidate (candidate_head + pr_refs)"
        )
    if run.current_phase not in {"verify", "review"}:
        raise RuntimeError("recover-superseded requires verify/review phase run")
    if any(item.status == "ongoing" for item in related):
        raise RuntimeError("recover-superseded refuses while another run is ongoing")
    if any(
        job.get("workflow_run_id") == run.run_id
        and job.get("status") in ACTIVE_JOB_STATUSES
        for job in workflow_registry.list_jobs()
    ):
        raise RuntimeError("recover-superseded refuses active workflow job")
    body = {
        "schema": "cortex-work-recover-superseded/v1",
        "repo": authority.repo,
        "work_id": authority.work_id,
        "run_id": run.run_id,
        "authority_digest": work_authority_digest(authority),
        "actor": actor,
        "reason": reason,
    }
    record = _write_supersede_evidence(
        body,
        state_path=state_path,
        subdir="work-recover-superseded",
        label="recover-superseded",
    )
    # 兩步走：先復歸 ongoing 並剝掉 supersede 迴圈附加的 blocked facet（保留
    # needs_human facet＋理由，維持 `_resolve_needs_human_reason` 的 facet⟷理由
    # invariant），再交給 official restart 一步清 needs_human 並打回 verify。
    workflow_registry._manager_update_workflow_run(
        run.run_id,
        status="ongoing",
        facets=tuple(facet for facet in run.facets if facet != "blocked"),
    )
    updated = workflow_registry._manager_reset_workflow_for_authority_restart(
        run.run_id,
        authority_digest=work_authority_digest(authority),
    )
    return {
        "action": "recovered-superseded",
        "actor": actor,
        "reason": reason,
        "expected_run_id": expected_run_id,
        "evidence": record,
        "run": updated.to_dict(),
    }


def _abandon_action(
    *,
    args: dict[str, Any],
    authority,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """Supersede one exact pre-delivery WorkflowRun without completion evidence."""

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_run_id", "reason",
    }
    if extras:
        raise ValueError(f"abandon rejects caller evidence/input: {sorted(extras)[0]}")
    expected_run_id = args.get("expected_run_id")
    actor = args.get("actor")
    reason = args.get("reason")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("abandon requires exact expected_run_id")
    if (
        not isinstance(actor, str)
        or actor != actor.strip()
        or not 1 <= len(actor) <= 128
        or not actor.isprintable()
    ):
        raise ValueError("abandon requires bounded actor")
    if (
        not isinstance(reason, str)
        or reason != reason.strip()
        or not 1 <= len(reason) <= 500
        or not reason.isprintable()
    ):
        raise ValueError("abandon requires bounded reason")
    related = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo and run.work_id == authority.work_id
    ]
    exact = [run for run in related if run.run_id == expected_run_id]
    if len(exact) != 1:
        raise RuntimeError("abandon expected WorkflowRun CAS mismatch")
    run = exact[0]
    if run.status == "superseded":
        body = _superseded_abandon_body(
            run,
            state_path=state_path,
            actor=actor,
            reason=reason,
        )
        record = _abandon_record(body, state_path=state_path)
        workflow_registry._manager_validate_workflow_abandon(
            run.run_id,
            evidence_ref=record["ref"],
        )
        # #275：重入分支（run 已是 superseded）也要 emit，才能覆蓋「第一次
        # emit 之後、terminal transition 之前 daemon crash」這個窗口；
        # attempt_digest 用 evidence digest——同一份 evidence 內容重讀回來
        # 算出同一個 digest，OutcomeStore.append 據此去重，不產生第二筆。
        outcome_store = engineering_outcome.OutcomeStore(
            engineering_outcome.outcome_store_path(state_path, repo=authority.repo)
        )
        engineering_outcome.emit_outcome(
            outcome_store,
            run=run,
            authority=authority,
            jobs=workflow_registry.list_jobs(),
            outcome="abandoned",
            attempt_digest=record["hash"],
            reason_code=reason,
        )
        updated = workflow_registry._manager_abandon_workflow_run(
            run.run_id,
            evidence_ref=record["ref"],
        )
        # #416：重入分支同樣要嘗試 GC——覆蓋「第一次 GC 之後、下一次 abandon
        # 呼叫之前殘留仍未清乾淨」的窗口；已清過的項目在這裡自然是 no-op
        # （`_gc_one_abandoned_planning_artifact` 對已不存在的檔案直接略過）。
        _gc_abandoned_planning_artifacts(
            updated,
            workflow_registry=workflow_registry,
            coordinator_root=state_path.resolve().parent,
        )
        _reclaim_abandoned_build_worktrees(
            updated, workflow_registry, state_path=state_path
        )
        return {
            "action": "abandoned",
            "reason": reason,
            "actor": actor,
            "expected_run_id": expected_run_id,
            "evidence": record,
            "run": updated.to_dict(),
        }
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("abandon issue is not authorized by WorkAuthority")
    if any(item.status == "ongoing" and item.run_id != run.run_id for item in related):
        raise RuntimeError("abandon refuses a different active WorkflowRun")
    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    if run.issue_refs != expected_issues or not _openspec_refs_compatible(run, authority):
        # #410（孤兒救援，建議 2 的窄放行）：work item 改名／重識別後，舊識別的
        # authority 會失去 issue/openspec 映射（例如 tombstone row 只剩 path 錨點、
        # 或 collision 使 correlation 不再把 issue 分派給本 work_id），此時 run 的
        # refs 與 authority 恆不相等，孤兒 run 永遠不可 abandon——與 v3 相撞的
        # 認領也永遠無法釋放。僅在「authority 完全失去 issue 與 openspec 映射、
        # 而 run 仍留有 refs」這個孤兒簽名下放行；一般 abandon（authority 映射
        # 完整）維持嚴格相等。expected_run_id／actor／reason 的強制項與上方的
        # 單一 ongoing 檢查不變，abandon evidence 照常落盤留稽核。
        orphan_rescue = (
            not authority.mapped_issues
            and not authority.mapped_openspec
            and bool(run.issue_refs or run.openspec_refs)
        )
        if not orphan_rescue:
            raise RuntimeError("abandon WorkflowRun refs differ from current WorkAuthority")
    body = {
        "schema": "cortex-work-abandon/v1",
        "repo": authority.repo,
        "work_id": authority.work_id,
        "run_id": run.run_id,
        "authority_digest": work_authority_digest(authority),
        "actor": actor,
        "reason": reason,
    }
    digest = verification.canonical_json_hash(body)
    target = (
        state_path.resolve().parent
        / "evidence"
        / "work-abandon"
        / f"{run.run_id}-{digest}.json"
    )
    workflow_registry._manager_validate_workflow_abandon(
        run.run_id,
        evidence_ref=str(target),
    )
    record = _abandon_record(body, state_path=state_path)
    # #275：先 durable 寫 canonical outcome，再改 run status（見
    # docs/superpowers/specs/engineering-outcome-contract-design.md）。
    outcome_store = engineering_outcome.OutcomeStore(
        engineering_outcome.outcome_store_path(state_path, repo=authority.repo)
    )
    engineering_outcome.emit_outcome(
        outcome_store,
        run=run,
        authority=authority,
        jobs=workflow_registry.list_jobs(),
        outcome="abandoned",
        attempt_digest=record["hash"],
        reason_code=reason,
    )
    updated = workflow_registry._manager_abandon_workflow_run(
        run.run_id,
        evidence_ref=record["ref"],
    )
    # #416：run 終態化為 superseded 之後，盡力回收已發佈未提交的 planning
    # artifacts——放在狀態轉換之後，確保只有 abandon 真的成立時才動檔案；
    # GC 本身 best-effort、不 raise，失敗不得讓已經成功的 abandon 跟著失敗。
    _gc_abandoned_planning_artifacts(
        updated,
        workflow_registry=workflow_registry,
        coordinator_root=state_path.resolve().parent,
    )
    # #478／#527：supersede 也要回收 build worktree，紀律同上（best-effort）。
    _reclaim_abandoned_build_worktrees(updated, workflow_registry, state_path=state_path)
    return {
        "action": "abandoned",
        "reason": reason,
        "actor": actor,
        "expected_run_id": expected_run_id,
        "evidence": record,
        "run": updated.to_dict(),
    }


def _validate_retirement_operator_inputs(args: dict[str, Any]) -> tuple[str, str, str]:
    """Shared expected_run_id/actor/reason admission for the retirement family
    (``abandon`` and ``retire-delivered``). Returns the validated triple."""

    expected_run_id = args.get("expected_run_id")
    actor = args.get("actor")
    reason = args.get("reason")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("retire-delivered requires exact expected_run_id")
    if (
        not isinstance(actor, str)
        or actor != actor.strip()
        or not 1 <= len(actor) <= 128
        or not actor.isprintable()
    ):
        raise ValueError("retire-delivered requires bounded actor")
    if (
        not isinstance(reason, str)
        or reason != reason.strip()
        or not 1 <= len(reason) <= 500
        or not reason.isprintable()
    ):
        raise ValueError("retire-delivered requires bounded reason")
    return expected_run_id, actor, reason


def _retire_delivered_pr_terminal_status(
    run, *, authority, runner: Runner
) -> list[dict[str, str]]:
    """Prove every ``pr_ref`` of ``run`` is terminal (merged/closed) via the
    existing GitHub provider seam. Refuses (``RuntimeError``) on the first PR
    that is still open, or on any malformed/cross-repo ref — retirement must
    never supersede a run while a delivery could still be in flight."""

    github = GitHubDeliveryClient(runner=runner)
    statuses: list[dict[str, str]] = []
    for ref in run.pr_refs:
        match = re.fullmatch(rf"{re.escape(authority.repo)}#([1-9][0-9]*)", str(ref))
        if match is None:
            raise RuntimeError("retire-delivered PR ref malformed or cross-repo")
        lifecycle = github.fetch_pr_lifecycle_status(
            repo=authority.repo, pr_number=int(match.group(1))
        )
        if not lifecycle.terminal:
            raise RuntimeError("retire-delivered refuses a non-terminal PR")
        statuses.append({"ref": ref, "state": lifecycle.state})
    statuses.sort(key=lambda entry: entry["ref"])
    return statuses


def _superseded_retire_delivered_body(
    run,
    *,
    state_path: Path,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Re-derive the retire-delivered evidence body from the durable file when
    the run is *already* superseded (crash between outcome emit and terminal
    transition). Mirrors ``_superseded_abandon_body`` but reads back the
    ``pr_terminal_status`` proof instead of re-querying GitHub — so re-entry
    stays sound even when the provider is currently rate-limited."""

    root = state_path.resolve().parent / "evidence" / "work-retire-delivered"
    candidates: list[Path] = []
    pattern = re.compile(rf"{re.escape(run.run_id)}-([0-9a-f]{{64}})\.json")
    for value in run.evidence_refs:
        path = Path(value)
        if (
            not path.is_absolute()
            or path.parent != root
            or pattern.fullmatch(path.name) is None
        ):
            continue
        candidates.append(path)
    if len(candidates) != 1:
        raise RuntimeError("WorkflowRun was superseded by different authority")
    target = candidates[0]
    try:
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_mode & 0o222
            or target.stat().st_size > 8192
        ):
            raise RuntimeError("WorkflowRun was superseded by different authority")
        raw = target.read_bytes()
        body = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "WorkflowRun was superseded by different authority"
        ) from error
    required = {
        "schema",
        "repo",
        "work_id",
        "run_id",
        "authority_digest",
        "actor",
        "reason",
        "pr_terminal_status",
    }
    pr_terminal_status = body.get("pr_terminal_status") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or set(body) != required
        or body.get("schema") != "cortex-work-retire-delivered/v1"
        or body.get("repo") != run.repo
        or body.get("work_id") != run.work_id
        or body.get("run_id") != run.run_id
        or re.fullmatch(r"[0-9a-f]{64}", str(body.get("authority_digest"))) is None
        or body.get("actor") != actor
        or body.get("reason") != reason
        or not isinstance(pr_terminal_status, list)
        or not pr_terminal_status
        or any(
            not isinstance(entry, dict)
            or set(entry) != {"ref", "state"}
            or entry.get("state") not in {"merged", "closed_unmerged"}
            for entry in pr_terminal_status
        )
    ):
        raise RuntimeError("WorkflowRun was superseded by different authority")
    content = (
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    digest = verification.canonical_json_hash(body)
    if raw != content or target.name != f"{run.run_id}-{digest}.json":
        raise RuntimeError("WorkflowRun was superseded by different authority")
    return body


def _retire_delivered_action(
    *,
    args: dict[str, Any],
    authority,
    runner: Runner,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """Supersede one *delivered* orphan run whose every PR is terminal.

    The gap this fills: a work item whose real delivery happened outside the
    cortex pipeline (a fallback subagent merged the PR directly) leaves its
    WorkflowRun stuck ``ongoing`` with terminal ``pr_refs`` — it can neither
    ship (dirty candidate) nor ``abandon`` (the pre-delivery gate rejects any
    ``pr_refs``). This action is the explicit, evidence-backed retirement path:
    it proves every ``pr_ref`` PR is terminal (merged/closed) through the
    provider seam, records that proof as audit evidence, then supersedes the
    run. ``abandon``'s pre-delivery strictness is deliberately left intact.
    """

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_run_id", "reason",
    }
    if extras:
        raise ValueError(
            f"retire-delivered rejects caller evidence/input: {sorted(extras)[0]}"
        )
    expected_run_id, actor, reason = _validate_retirement_operator_inputs(args)
    related = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo and run.work_id == authority.work_id
    ]
    exact = [run for run in related if run.run_id == expected_run_id]
    if len(exact) != 1:
        raise RuntimeError("retire-delivered expected WorkflowRun CAS mismatch")
    run = exact[0]
    if run.status == "superseded":
        body = _superseded_retire_delivered_body(
            run,
            state_path=state_path,
            actor=actor,
            reason=reason,
        )
        record = _retire_delivered_record(body, state_path=state_path)
        workflow_registry._manager_validate_workflow_retire_delivered(
            run.run_id,
            evidence_ref=record["ref"],
        )
        outcome_store = engineering_outcome.OutcomeStore(
            engineering_outcome.outcome_store_path(state_path, repo=authority.repo)
        )
        engineering_outcome.emit_outcome(
            outcome_store,
            run=run,
            authority=authority,
            jobs=workflow_registry.list_jobs(),
            outcome="abandoned",
            attempt_digest=record["hash"],
            reason_code=reason,
        )
        updated = workflow_registry._manager_retire_delivered_workflow_run(
            run.run_id,
            evidence_ref=record["ref"],
        )
        _gc_abandoned_planning_artifacts(updated)
        return {
            "action": "retired-delivered",
            "reason": reason,
            "actor": actor,
            "expected_run_id": expected_run_id,
            "pr_terminal_status": body["pr_terminal_status"],
            "evidence": record,
            "run": updated.to_dict(),
        }
    if any(item.status == "ongoing" and item.run_id != run.run_id for item in related):
        raise RuntimeError("retire-delivered refuses a different active WorkflowRun")
    if not run.pr_refs:
        raise RuntimeError("retire-delivered requires a delivered run with pr refs")
    pr_terminal_status = _retire_delivered_pr_terminal_status(
        run, authority=authority, runner=runner
    )
    body = {
        "schema": "cortex-work-retire-delivered/v1",
        "repo": authority.repo,
        "work_id": authority.work_id,
        "run_id": run.run_id,
        "authority_digest": work_authority_digest(authority),
        "actor": actor,
        "reason": reason,
        "pr_terminal_status": pr_terminal_status,
    }
    digest = verification.canonical_json_hash(body)
    target = (
        state_path.resolve().parent
        / "evidence"
        / "work-retire-delivered"
        / f"{run.run_id}-{digest}.json"
    )
    workflow_registry._manager_validate_workflow_retire_delivered(
        run.run_id,
        evidence_ref=str(target),
    )
    record = _retire_delivered_record(body, state_path=state_path)
    outcome_store = engineering_outcome.OutcomeStore(
        engineering_outcome.outcome_store_path(state_path, repo=authority.repo)
    )
    engineering_outcome.emit_outcome(
        outcome_store,
        run=run,
        authority=authority,
        jobs=workflow_registry.list_jobs(),
        outcome="abandoned",
        attempt_digest=record["hash"],
        reason_code=reason,
    )
    updated = workflow_registry._manager_retire_delivered_workflow_run(
        run.run_id,
        evidence_ref=record["ref"],
    )
    _gc_abandoned_planning_artifacts(updated)
    return {
        "action": "retired-delivered",
        "reason": reason,
        "actor": actor,
        "expected_run_id": expected_run_id,
        "pr_terminal_status": pr_terminal_status,
        "evidence": record,
        "run": updated.to_dict(),
    }


def _close_delivered_action(
    *,
    args: dict[str, Any],
    authority,
    runner: Runner,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """為已交付但缺少記錄的 work item 建立 operator CompletionRecord。

    僅接受沒有 WorkflowRun 的 work item。既有 remote closure gates 必須全數
    通過，才會保存 actor／reason 與遠端事實。
    """

    extras = set(args) - {"action", "repo", "work_id", "actor", "reason"}
    if extras:
        raise ValueError(
            f"close-delivered rejects caller evidence/input: {sorted(extras)[0]}"
        )
    actor = args.get("actor")
    reason = args.get("reason")
    if (
        not isinstance(actor, str)
        or actor != actor.strip()
        or not 1 <= len(actor) <= 128
        or not actor.isprintable()
    ):
        raise ValueError("close-delivered requires bounded actor")
    if (
        not isinstance(reason, str)
        or reason != reason.strip()
        or not 1 <= len(reason) <= 500
        or not reason.isprintable()
    ):
        raise ValueError("close-delivered requires bounded reason")

    related = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo and run.work_id == authority.work_id
    ]
    if related:
        raise RuntimeError("close-delivered requires a work item with no WorkflowRun")
    if not authority.mapped_issues:
        raise RuntimeError("close-delivered requires mapped GitHub issues")
    if len(authority.mapped_prs) != 1:
        raise RuntimeError("close-delivered requires exactly one mapped PR")
    if len(authority.mapped_openspec) > 1:
        raise RuntimeError("close-delivered supports at most one mapped OpenSpec change")
    if not authority.mapped_todo_paths:
        raise RuntimeError("close-delivered requires mapped Todo paths")

    github = GitHubDeliveryClient(runner=runner)
    change = authority.mapped_openspec[0] if authority.mapped_openspec else None
    todo_paths = tuple(authority.mapped_todo_paths)
    facts = github.fetch_remote_closure(
        repo=authority.repo,
        pr_number=authority.mapped_prs[0],
        change=change,
        required_issues=authority.mapped_issues,
        todo_paths=todo_paths,
    )
    if change is not None:
        archive_task_paths = tuple(
            sorted(
                path
                for path in github._commit_tree_paths(
                    repo=authority.repo, commit=facts.default_head
                )
                if re.fullmatch(
                    rf"openspec/changes/archive/\d{{4}}-\d{{2}}-\d{{2}}-"
                    rf"{re.escape(change)}/tasks\.md",
                    path,
                )
            )
        )
        if not archive_task_paths:
            raise RuntimeError("close-delivered remote closure blocked: openspec-tasks-missing")
        first_facts = facts
        facts = github.fetch_remote_closure(
            repo=authority.repo,
            pr_number=authority.mapped_prs[0],
            change=change,
            required_issues=authority.mapped_issues,
            todo_paths=tuple(sorted(set(todo_paths) | set(archive_task_paths))),
        )
        if (
            facts.default_head != first_facts.default_head
            or facts.merge_commit != first_facts.merge_commit
            or facts.pr_head != first_facts.pr_head
            or dict(facts.issue_states) != dict(first_facts.issue_states)
            or facts.active_openspec_absent != first_facts.active_openspec_absent
            or facts.archive_present != first_facts.archive_present
            or any(
                facts.todo_revisions.get(path) != revision
                for path, revision in first_facts.todo_revisions.items()
            )
        ):
            raise RuntimeError("close-delivered remote facts changed during verification")
    gate = evaluate_remote_closure(
        facts=replace(facts, completion_record_valid=True),
        required_issues=authority.mapped_issues,
        expected_head=facts.pr_head,
    )
    if not gate.allowed:
        raise RuntimeError(f"close-delivered remote closure blocked: {', '.join(gate.reasons)}")

    source_revisions: dict[str, str] = {}
    for value in authority.source_revisions:
        source_id, separator, revision = value.partition("@")
        if not separator or not source_id or not revision:
            raise RuntimeError("close-delivered WorkAuthority source revisions malformed")
        if source_id in source_revisions:
            raise RuntimeError("close-delivered WorkAuthority source revisions duplicated")
        source_revisions[source_id] = revision
    if not source_revisions:
        raise RuntimeError("close-delivered requires confirmed source revisions")

    body = {
        "schema": "cortex-work-close-delivered/v1",
        "repo": authority.repo,
        "work_id": authority.work_id,
        "actor": actor,
        "reason": reason,
        "authority_digest": work_authority_digest(authority),
        "source_revisions": dict(sorted(source_revisions.items())),
        "issue_states": [
            {"ref": f"{authority.repo}#{issue}", "state": facts.issue_states[issue]}
            for issue in sorted(authority.mapped_issues)
        ],
        "pull_request": {
            "ref": f"{authority.repo}#{authority.mapped_prs[0]}",
            "candidate": facts.pr_head.lower(),
            "merge_commit": facts.merge_commit.lower(),
            "merge_parents": [parent.lower() for parent in facts.merge_parents],
            "default_head": facts.default_head.lower(),
            "merge_is_ancestor": facts.merge_is_ancestor,
            "merge_is_merge_commit": facts.merge_is_merge_commit,
        },
        "openspec": {
            "refs": list(authority.mapped_openspec),
            "active_openspec_absent": facts.active_openspec_absent,
            "archive_present": facts.archive_present,
        },
        "todo_revisions": dict(sorted(facts.todo_revisions.items())),
    }
    record = _write_supersede_evidence(
        body,
        state_path=state_path,
        subdir="work-close-delivered",
        stem=authority.work_id,
        label="close-delivered",
        max_size=16384,
    )
    return {
        "action": "closed-delivered",
        "actor": actor,
        "reason": reason,
        "issue_states": body["issue_states"],
        "pull_request": body["pull_request"],
        "completion_record": record,
    }


def _validate_reclaim_reset_operator_inputs(args: dict[str, Any]) -> tuple[str, str]:
    """#519：`reset-reclaim-budget` 的 actor／reason 入場驗證。

    界限刻意與 retirement family（`_validate_retirement_operator_inputs`）逐字
    相同——同樣是「一個人、為了一個寫得出來的理由，明示解除一道安全機制」，
    稽核欄位的規格沒有理由分歧。差別只在這裡不要 `expected_run_id`：熔斷觸發
    的前提就是沒有 active run 可以 CAS（`decide_*_claim` 判成 claim 才會走到
    熔斷），硬要一個 exact run id 等同讓這條解鎖路徑永遠打不開。
    """

    actor = args.get("actor")
    reason = args.get("reason")
    if (
        not isinstance(actor, str)
        or actor != actor.strip()
        or not 1 <= len(actor) <= 128
        or not actor.isprintable()
    ):
        raise ValueError("reset-reclaim-budget requires bounded actor")
    if (
        not isinstance(reason, str)
        or reason != reason.strip()
        or not 1 <= len(reason) <= 500
        or not reason.isprintable()
    ):
        raise ValueError("reset-reclaim-budget requires bounded reason")
    return actor, reason


def _reset_reclaim_budget_action(
    *,
    args: dict[str, Any],
    authority,
    now_epoch: float,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """#519：明示重置 (repo, work_id) 的 semantic-reclaim 世代熔斷計數。

    #218 AC2 的熔斷對全部 superseded 歷史無條件累加：不看時間窗、不看失敗原
    因、不看引擎是否已修好，而且完全沒有重置路徑。實測（2026-08-14）一個
    work item 在四分鐘內因三個 cortex 自身缺陷（成功卻被自行 supersede、codex
    exec 逾時、前代殘留 artifact 使 authority fail-closed）耗盡額度後永久鎖
    死——沒有一次是工作項本身的問題，卻再也無法派工。

    重置的實作刻意是 **append-only 水位**：把「本次赦免的 superseded run_id」
    寫成一筆 registry 授權列（`_manager_record_reclaim_reset`），熔斷計數改成
    「superseded 世代扣掉所有已赦免 run_id」。既有 run row 一列不刪不改——run
    歷史是稽核來源，重置是新增一筆授權事實，不是抹掉失敗紀錄。水位以 run_id
    集合而非時間戳表達，因此不依賴任何時鐘假設，且重置後新產生的世代照常累
    加，熔斷會再次上膛（不是永久關閉安全機制）。
    """

    extras = set(args) - {"action", "repo", "work_id", "actor", "reason"}
    if extras:
        raise ValueError(
            f"reset-reclaim-budget rejects caller evidence/input: {sorted(extras)[0]}"
        )
    actor, reason = _validate_reclaim_reset_operator_inputs(args)
    pending = _effective_superseded_generations(
        workflow_registry, repo=authority.repo, work_id=authority.work_id
    )
    if not pending:
        prior = [
            entry
            for entry in workflow_registry.list_reclaim_resets()
            if entry.get("repo") == authority.repo
            and entry.get("work_id") == authority.work_id
        ]
        if prior:
            # 重送同一請求（含 crash window 重試）：額度已經是滿的，不再寫第二
            # 筆授權，回報最後一筆既有紀錄讓呼叫端自證冪等。
            latest = prior[-1]
            return {
                "action": "reclaim-budget-reset",
                "already_reset": True,
                "actor": actor,
                "reason": reason,
                "cleared_generations": 0,
                "cleared_run_ids": [],
                "reclaim_budget_limit": SEMANTIC_RECLAIM_LIMIT,
                "evidence": {
                    "ref": latest["evidence_ref"],
                    "hash": latest["evidence_hash"],
                },
            }
        # 從未燒過額度——重置是無意義的狀態變更，fail closed（比照本檔其餘
        # recovery 動作「判準不符即拒絕，不做 best-effort」的慣例）。
        raise RuntimeError("reset-reclaim-budget has no superseded generation to clear")
    cleared_run_ids = sorted(run.run_id for run in pending)
    body = _reclaim_reset_body(
        repo=authority.repo,
        work_id=authority.work_id,
        actor=actor,
        reason=reason,
        cleared_run_ids=cleared_run_ids,
        created_at=datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
    )
    # #275 的順序慣例：先把稽核事實寫成 durable evidence，再改狀態。兩步之間
    # crash 時最壞只留下一份未被引用的 evidence 檔（水位是 run_id 集合聯集，
    # 重放同一份授權在語意上冪等），不會出現「狀態已放寬但查無授權依據」。
    record = _reclaim_reset_record(body, state_path=state_path)
    entry = workflow_registry._manager_record_reclaim_reset(
        repo=authority.repo,
        work_id=authority.work_id,
        actor=actor,
        reason=reason,
        evidence_ref=record["ref"],
        evidence_hash=record["hash"],
        cleared_run_ids=cleared_run_ids,
        created_at=body["created_at"],
    )
    return {
        "action": "reclaim-budget-reset",
        "already_reset": False,
        "actor": actor,
        "reason": reason,
        "cleared_generations": len(cleared_run_ids),
        "cleared_run_ids": list(entry["cleared_run_ids"]),
        "reclaim_budget_limit": SEMANTIC_RECLAIM_LIMIT,
        "evidence": record,
    }


# --------------------------------------------------------------------------
# #731 (A)：候選 git base 的**重新凍結**入口（`work refreeze-base`）
# --------------------------------------------------------------------------
#
# 凍結本身是對的（hermetic pinning，#211／#208 A.2）：`claim_readiness.
# base_sha_probe` 逐字「Fetch remote main once and freeze it as base_sha」，且對
# 不一致的 `local_known_base_sha` 給 `stale-base`。缺的是**重新凍結**——`work
# start` 對還有 active workflow 的 work item 回 `action=resume /
# reason=active-workflow`，不走新 claim，因此基底原封不動；連續三次「換代」
# （abandon → reset-reclaim-budget → start）一次都沒換掉候選樹的 HEAD。
#
# 候選基底的**權威來源**（查證結果，非推測）：
#
#   1. `WorkflowRun.frozen_readiness["base_sha"]` —— `manager._dispatch_workflow_card`
#      的**首張 build 卡**分支唯一讀的欄位（該檔 `build_base_sha = ...
#      run.frozen_readiness.get("base_sha")`），一路傳進
#      `seams.ScriptWorktreeCreator.create(..., base_sha=...)`。
#   2. 該欄位為 `None` 時，`create()` 退回 `self._base`，而 dispatch 建 creator 時
#      傳的是字面 `base="main"` ⇒ 實際基底是**來源樹的 `refs/heads/main`**。
#      `readiness_checker` 在 production 從未被接線（`execute_work_action` 的預設
#      值是 `None`，manager daemon 也沒有傳），所以實機 run 的 `frozen_readiness`
#      恆為 `None`，基底就是那條**沒有人推進過**的本地 `main`——這正是「mirror
#      已經是 7eb707b、候選樹仍是 59a7a9b」的機制。
#
# 因此本動作把新基底寫進 (1)：那是 dispatch 真的會讀的那一格，不是新造的第二份
# 真實來源；而且順帶把 run 從「隱式跟著本地 main 漂」升級成「明示 pin 在一個
# SHA」，hermetic pinning 只有更強、沒有放寬。
CANDIDATE_BASE_REFREEZE_SCHEMA = "cortex-work-candidate-base-refreeze/v1"

#: #752：operator 對 needs_human 判定的裁決紀錄——verify 階段唯一的可信人裁通道。
OPERATOR_ADJUDICATION_SCHEMA = "cortex-operator-adjudication/v1"
OPERATOR_ADJUDICATION_REASON_LIMIT = 4000

#: 沒有前次凍結集（production 的常態）時寫進 `frozen_readiness` 的形狀。**刻意
#: 不用** `pre-claim-readiness-frozen-set/v1`：那個 schema 的語意是「六道
#: readiness 關卡都通過了」，而本動作只重新凍結 base 一格，寫成前者等於謊稱做過
#: 六道檢查。消費端（`_dispatch_workflow_card`）只讀 `base_sha`，兩種 schema 都
#: 讀得到；有前次凍結集時則**逐欄保留**、只換 `base_sha`（其餘凍結事實不是這次
#: 重新驗證出來的，不得順手覆寫）。
CANDIDATE_BASE_FREEZE_SCHEMA = "cortex-candidate-base-freeze/v1"

#: 允許重新凍結的 phase。基底只在 build 卡 provisioning 時被消費，而 `verify`
#: 之後必然已有被採信的 candidate（那時基底已經改由 `_workflow_build_handoff_base`
#: 決定，重新凍結會是靜默 no-op），因此上界收在 `build`。
REFREEZE_ALLOWED_PHASES = ("claim", "define", "plan", "build")


def _validate_refreeze_operator_inputs(args: dict[str, Any]) -> tuple[str, str, str]:
    """#731：`refreeze-base` 的 expected_run_id／actor／reason 入場驗證。

    界限與 `_validate_retirement_operator_inputs`／
    `_validate_reclaim_reset_operator_inputs` 逐字相同——同一族「一個人明示解除
    ／推動一道凍結」的稽核欄位規格沒有理由分歧。`control/contract.py` 已對這族
    action 強制同一組界限；這裡是縱深防禦（work action 也可能被直接呼叫）。
    """

    expected_run_id = args.get("expected_run_id")
    actor = args.get("actor")
    reason = args.get("reason")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("refreeze-base requires exact expected_run_id")
    if (
        not isinstance(actor, str)
        or actor != actor.strip()
        or not 1 <= len(actor) <= 128
        or not actor.isprintable()
    ):
        raise ValueError("refreeze-base requires bounded actor")
    if (
        not isinstance(reason, str)
        or reason != reason.strip()
        or not 1 <= len(reason) <= 500
        or not reason.isprintable()
    ):
        raise ValueError("refreeze-base requires bounded reason")
    return expected_run_id, actor, reason


def _refreeze_rev(repo_root: Path, revision: str) -> str | None:
    """解析 `revision` 為 exact commit SHA；不存在／不可讀一律回 `None`。"""

    result = verification._run_git(
        ["-C", str(repo_root), "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        None,
    )
    value = str(result.get("stdout", "")).strip().lower()
    if result.get("status") != "ok" or verification.SAFE_SHA_RE.fullmatch(value) is None:
        return None
    return value


def _refreeze_is_ancestor(repo_root: Path, *, ancestor: str, descendant: str) -> bool:
    """`merge-base --is-ancestor` 的三值收斂：0＝是、1＝否、其餘＝拒絕作答。

    判準刻意與 `seams.ScriptWorktreeCreator.create()` 的既有守衛同一個
    git 述詞——重新凍結的入場檢查若用另一套判準，就會出現「refreeze 說可以、
    下一拍 provision 說不行」的兩份真話。
    """

    result = verification._run_git(
        ["-C", str(repo_root), "merge-base", "--is-ancestor", ancestor, descendant],
        None,
    )
    returncode = result.get("returncode")
    if returncode == 0:
        return True
    if returncode == 1:
        return False
    raise RuntimeError(
        "refreeze-base ancestry check failed: "
        f"{str(result.get('stderr', '')).strip()[:200] or result.get('status')}"
    )


def _refreeze_base_body(
    *,
    run,
    actor: str,
    reason: str,
    previous_base_sha: str | None,
    previous_base_source: str,
    base_sha: str,
    baselines: list[dict[str, str]],
    build_branch: str,
    build_branch_sha: str | None,
    created_at: str,
) -> dict[str, Any]:
    """#731：`cortex-work-candidate-base-refreeze/v1` 的 canonical evidence body。

    形狀比照 `cortex-work-reclaim-reset/v1`／`cortex-work-planning-recovery/v1`：
    內容即稽核事實——誰、為什麼、在哪個 run 上、把候選基底從哪裡推到哪裡、
    mirror 的 fetch 結果是什麼、以及當下每一條「已記錄基準」的位置。
    """

    return {
        "schema": CANDIDATE_BASE_REFREEZE_SCHEMA,
        "repo": run.repo,
        "work_id": run.work_id,
        "run_id": run.run_id,
        "actor": actor,
        "reason": reason,
        "previous_base_sha": previous_base_sha,
        "previous_base_source": previous_base_source,
        "base_sha": base_sha,
        # mirror 的 fetch 結果——走的是 claim 用的**同一支** probe，
        # 不是另外寫一次 fetch。
        "remote_fetch": {
            "probe": "claim_readiness.base_sha_probe",
            "remote": "origin",
            "branch": "main",
            "ref": "refs/remotes/origin/main",
            "status": "ok",
            "sha": base_sha,
        },
        "fast_forward_baselines": baselines,
        "build_branch": build_branch,
        "build_branch_sha": build_branch_sha,
        "previous_phase": run.current_phase,
        "previous_facets": sorted(run.facets),
        "workspace_root": str(run.workspace_root),
        "created_at": created_at,
    }


def _refreeze_base_record(body: dict[str, Any], *, state_path: Path) -> dict[str, str]:
    return _write_supersede_evidence(
        body,
        state_path=state_path,
        subdir="work-candidate-base-refreeze",
        label="candidate-base-refreeze",
        # body 欄位固定，但含 ≤500 字 reason、≤128 字 actor 與兩條絕對路徑／branch
        # 名，4096 對長路徑部署偏緊；放寬到 16KiB 仍是「小文件」的防禦性上界，
        # 真正的正確性判準是 `st_size != len(content)` 與逐位元組比對。
        max_size=16384,
    )


def _refreeze_base_action(
    *,
    args: dict[str, Any],
    authority,
    now_epoch: float,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """#731 (A)：把還活著的 run 的候選 git base 重新凍結到目前的 `origin/main`。

    **入場條件全部 fail-closed，寧可拒絕也不做半套**（逐條理由）：

    1. exact `expected_run_id` CAS ＋ 單一 `ongoing` canonical run——重新凍結的
       對象是一個具體的 run，不是「這個 work item 最近那個」。
    2. `current_phase ∈ REFREEZE_ALLOWED_PHASES`——基底只在 build 卡 provisioning
       時被消費。
    3. `candidate_head`／`verified_head` 皆為 `None`——一旦有被採信的 build 成果，
       下一張卡的 base 改由 `_workflow_build_handoff_base()`（＝`candidate_head`）
       決定，寫進 `frozen_readiness` 的新基底**根本不會被讀**。那會是「回報成功
       但什麼都沒發生」的靜默半套，因此拒絕。
    4. 沒有 in-flight job（`dispatched`／`running`）——在跑著的 job 腳下抽換基底
       等於製造 split-brain。
    5. 沒有已發佈的交付物（`pr_refs`／`pr_candidate`／`merge_revision`）——那代表
       run 已經走出管線，重新凍結沒有語意。
    6. **fast-forward only**：新基底必須是**每一條已記錄基準**的後代（或相等）。
       基準集合＝目前的凍結值（沒有凍結值時取來源樹的 `refs/heads/main`，那正是
       `ScriptWorktreeCreator(base="main")` 實際會解析到的東西）∪ 本 run 每個 job
       的 `dispatch_head` ∪ build branch 現在的位置。任何一條不是祖先就拒絕——
       那不是「重新凍結」而是把 run 的既有基準往回倒。
    7. **#613**：build branch 若存在且**不是**新基底的祖先，下一拍 provision 必定
       撞 `existing worktree branch has commits outside requested base`。判準與
       `create()` 的守衛是同一個 git 述詞（見 `_refreeze_is_ancestor`），在**改
       任何狀態之前**就先問，因此不會出現「refreeze 成功、下一拍才炸」。

    **出口狀態 == 入口狀態（#728 紀律）**：本動作不動 `current_phase`、不動
    `facets`、不動 `candidate_head`、不動任何 step 的 `gate_result`。唯一的狀態
    變更是 `frozen_readiness["base_sha"]` 與 append 一筆 evidence ref。因此
    「重新凍結後的 run 狀態是不是後續每一拍的合法入口狀態」在結構上不可能為否
    ——它就是重新凍結**之前**那個狀態。後續怎麼推進（`retry-card`／`resume`／
    `regenerate-gates`）完全沿用既有出口，本動作不代勞、也不製造新狀態。
    """

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "reason", "expected_run_id",
    }
    if extras:
        raise ValueError(
            f"refreeze-base rejects caller evidence/input: {sorted(extras)[0]}"
        )
    expected_run_id, actor, reason = _validate_refreeze_operator_inputs(args)
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("refreeze-base issue is not authorized by WorkAuthority")

    from . import claim_readiness
    from .manager import workflow_build_branch
    from .registry import ACTIVE_JOB_STATUSES

    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    active = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id == authority.work_id
        and run.status == "ongoing"
        and run.issue_refs == expected_issues
        and _openspec_refs_compatible(run, authority)
    ]
    if len(active) != 1:
        raise RuntimeError("refreeze-base requires one active canonical WorkflowRun")
    run = active[0]
    if run.run_id != expected_run_id:
        raise RuntimeError("refreeze-base expected WorkflowRun CAS mismatch")
    if run.current_phase not in REFREEZE_ALLOWED_PHASES:
        raise RuntimeError(
            "refreeze-base requires a pre-verify workflow phase "
            f"(allowed: {'/'.join(REFREEZE_ALLOWED_PHASES)}; got: {run.current_phase})"
        )
    if run.candidate_head is not None or run.verified_head is not None:
        raise RuntimeError(
            "refreeze-base requires a run with no accepted build candidate"
        )
    if run.pr_refs or run.pr_candidate is not None or run.merge_revision is not None:
        raise RuntimeError(
            "refreeze-base requires a run with no published delivery artifact"
        )
    jobs = [
        job
        for job in workflow_registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
    ]
    if any(job.get("status") in ACTIVE_JOB_STATUSES for job in jobs):
        raise RuntimeError("refreeze-base requires no in-flight job for the run")

    workspace_root = Path(run.workspace_root)
    if not workspace_root.is_absolute() or not workspace_root.is_dir():
        raise RuntimeError("refreeze-base requires the run workspace root to exist")

    # #731：凍結集的讀取走 `candidate_base.frozen_base_sha()`——與 (C) 的曝光面
    # （`cortex status`／`work show` 的 `candidate_git_base`）是**同一支函式**。
    # 「這條 run 現在凍結在哪個 base」是同一個事實，寫入端與讀取端各寫一份正規化
    # ／驗證只會漂移（#727 的第二份 `-o` 落點、#728 的兩份 `next_actions` 導出都
    # 是這個形狀）。下面 `None` 時退回本地 `main` 的處置仍屬本動作特有——那回答
    # 的是「下一張卡**會**用什麼基底」，與 (C) 回答的「候選**已經**坐在哪」是同
    # 一條時間軸的前後兩點，理由見該函式 docstring。
    previous_base_sha = candidate_base.frozen_base_sha(run.frozen_readiness)
    previous_base_source = "frozen-readiness" if previous_base_sha else "unresolved"

    # 已記錄基準：新基底必須是它們全部的後代。順序是稽核順序，不是判定順序
    # （全部都要成立）。
    baselines: list[dict[str, str]] = []
    if previous_base_sha is not None:
        baselines.append({"source": "frozen-readiness", "sha": previous_base_sha})
    else:
        # 沒有凍結值時，實際生效的基底是 `ScriptWorktreeCreator(base="main")` 解析
        # 到的來源樹本地 `main`。解析不出來（例如 detached / 無 main）就沒有這條
        # 基準可比——**不編造**（`previous_base_source` 誠實留在 `unresolved`），
        # 也不因此放行其他基準。
        local_main = _refreeze_rev(workspace_root, "main")
        if local_main is not None:
            baselines.append({"source": "local-main", "sha": local_main})
            previous_base_sha = local_main
            previous_base_source = "local-main"
    for job in jobs:
        head = job.get("dispatch_head")
        if isinstance(head, str) and verification.SAFE_SHA_RE.fullmatch(head) is not None:
            baselines.append(
                {"source": f"dispatch-head:{job.get('job_id')}", "sha": head.lower()}
            )
    build_branch = workflow_build_branch(run)
    build_branch_sha = _refreeze_rev(workspace_root, f"refs/heads/{build_branch}")
    if build_branch_sha is not None:
        baselines.append({"source": "build-branch", "sha": build_branch_sha})

    # mirror fetch：走 claim 用的**同一支** probe（`local_known_base_sha=None`，
    # 因此它只 fetch 一次並回報 `origin/main` 現值，不做 stale-base 判定——
    # stale 正是我們要修的那件事）。
    probe = claim_readiness.base_sha_probe(repo_root=workspace_root)
    probe_result = probe(
        claim_readiness.ReadinessContext(
            authority=authority,
            executor_identity="cortex-manager",
            issue_ref=(
                f"{authority.repo}#{authority.mapped_issues[0]}"
                if authority.mapped_issues
                else None
            ),
        )
    )
    if not probe_result.passed:
        raise RuntimeError(f"refreeze-base remote base probe failed: {probe_result.reason}")
    base_sha = str(probe_result.observation["base_sha"]).lower()

    if isinstance(run.frozen_readiness, dict) and previous_base_sha == base_sha:
        # 已經凍結在同一個值：重送同一請求（含 crash window 重試）不寫第二筆
        # evidence，也不動 registry——真正冪等。刻意要求「已有凍結集」才短路：
        # 未凍結的 run 即使本地 `main` 恰好等於 `origin/main`，把隱式基底轉成
        # 明示 pin 仍是一次真實的狀態變更。
        return {
            "action": "refreeze-base",
            "reason": "candidate-base-already-current",
            "already_current": True,
            "actor": actor,
            "operator_reason": reason,
            "expected_run_id": expected_run_id,
            "previous_base_sha": previous_base_sha,
            "base_sha": base_sha,
            "build_branch": build_branch,
            "build_branch_sha": build_branch_sha,
            "run": run.to_dict(),
        }

    for baseline in baselines:
        if baseline["sha"] == base_sha:
            continue
        if _refreeze_is_ancestor(
            workspace_root, ancestor=baseline["sha"], descendant=base_sha
        ):
            continue
        if baseline["source"] == "build-branch":
            # #613 的形狀：前一世代 abandon 沒有回收 build branch，branch 上還有
            # base 以外的 commit。下一拍 provision 必定撞
            # `existing worktree branch has commits outside requested base`，
            # 因此在這裡就拒絕——refreeze 成功但 run 依然 provision 不了，是最糟
            # 的半套。
            raise RuntimeError(
                "refreeze-base rejects a build branch carrying commits outside the new base "
                f"(branch={build_branch} sha={baseline['sha']}; 見 #613：abandon 不回收 "
                "build branch。請先回收該 branch 再重新凍結)"
            )
        raise RuntimeError(
            "refreeze-base rejects a non-fast-forward base "
            f"(baseline={baseline['source']} sha={baseline['sha']} → {base_sha})"
        )

    if isinstance(run.frozen_readiness, dict):
        # 有前次凍結集：**逐欄保留**，只換 base_sha。其餘欄位（planning authority
        # digest、monitor snapshot revision、live probe 快取旗標…）是當初那次
        # readiness transaction 的產物，這次沒有重跑，不得順手覆寫成現值。
        frozen = dict(run.frozen_readiness)
        frozen["base_sha"] = base_sha
    else:
        frozen = {
            "schema": CANDIDATE_BASE_FREEZE_SCHEMA,
            "repo": run.repo,
            "work_id": run.work_id,
            "base_sha": base_sha,
            "frozen_at_epoch": float(now_epoch),
            "frozen_by": "work-refreeze-base",
        }

    body = _refreeze_base_body(
        run=run,
        actor=actor,
        reason=reason,
        previous_base_sha=previous_base_sha,
        previous_base_source=previous_base_source,
        base_sha=base_sha,
        baselines=baselines,
        build_branch=build_branch,
        build_branch_sha=build_branch_sha,
        created_at=datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat(),
    )
    # #275 的順序慣例：先把稽核事實寫成 durable evidence，再改狀態。兩步之間
    # crash 時最壞只留下一份未被引用的 evidence 檔，不會出現「基底已經換掉但查
    # 無授權依據」。
    record = _refreeze_base_record(body, state_path=state_path)
    evidence_refs = run.evidence_refs
    if record["ref"] not in evidence_refs:
        evidence_refs = (*evidence_refs, record["ref"])
    updated = workflow_registry._manager_update_workflow_run(
        run.run_id,
        frozen_readiness=frozen,
        evidence_refs=evidence_refs,
    )
    payload: dict[str, Any] = {
        "action": "refreeze-base",
        "reason": "candidate-base-refrozen",
        "already_current": False,
        "actor": actor,
        # `reason` 這個 key 在本檔的回傳慣例裡是**機器可讀的結果理由**，operator
        # 打的那句話另掛 `operator_reason`，不互相覆蓋。
        "operator_reason": reason,
        "expected_run_id": expected_run_id,
        "previous_base_sha": previous_base_sha,
        "previous_base_source": previous_base_source,
        "base_sha": base_sha,
        "build_branch": build_branch,
        "build_branch_sha": build_branch_sha,
        "fast_forward_baselines": baselines,
        "evidence": record,
        "run": updated.to_dict(),
    }
    next_actions = _phase_recovery_actions(updated, workflow_registry)
    if next_actions:
        payload["next_actions"] = list(next_actions)
    return payload


def _regenerate_gates_action(
    *,
    args: dict[str, Any],
    authority,
    workflow_registry,
) -> dict[str, Any]:
    """#540：對既有 builder job log 依**當前** gate 宣告重新產生 gate ledger。

    gate ledger 是 job 結束當下依當時 env 寫成的檔案，之後即凍結。實測（run
    ``workflow-084f75e2178cf7547476``）manager env 漏宣告 ``PSC_GATE_CMD_PYTEST``
    時 ledger 是 ``gates: []``，builder 交付的合格 RED commit 撞
    ``gate-ledger-missing-expected-gate``；operator 補上宣告並重啟之後，契約內
    卻沒有任何路徑能讓那份 ledger 重新產生——``resume`` 只是重讀同一份舊 ledger
    再拒一次，``retry-build`` 只受理「最後一張 builder 卡」（tdd-red 是中段卡），
    ``recover-pre-candidate`` 要求 null candidate（worktree-isolation 早已錨定
    candidate）。唯一出路是 operator 手動跑 gate_ledger CLI。

    本動作把那個手動步驟收進契約，但**只重跑 gate、不改判**：它重新執行
    operator 宣告的 gate 命令、原子覆寫 ledger，然後就結束；run 仍停在
    needs_human，採信與否由既有的 ``resume`` → harvest 流程重新評估。builder 的
    commit 完全不動（它本來就是好的），也不重派任何模型。

    fail closed 條件：exact WorkflowRun CAS、run 必須在 needs_human、且必須真的
    找得到一個 gate-ledger 相關 phase、已終止、log 與 worktree 都還在的 job。
    任一條不成立即拒絕，不做任何 side effect。

    **#629：執行面收斂到 gate 執行身分。** 本動作原本在 **Manager 進程內**直接呼叫
    `gate_ledger.write_gate_ledger()`——那等於以 `cortex-manager` 的身分，在 builder
    完全掌控內容的工作樹上跑 `pytest`，而 `pytest` 會載入該樹的 `conftest.py` 與
    plugin。direct 模式下 builder 與 Manager 同 UID，這件事本來就沒有邊界可言；OS
    隔離上線後它是一條**真的**提權路徑，而且是最容易被忽略的那一種——它不在派工的
    熱路徑上，只在 operator 手動救援時才走到。

    改法不是在這裡另寫一套降權，而是改呼叫 `gate_runner.run_declared_gates()`：自動
    路徑（`manager.terminalize_workflow_job`）與本動作因此走**同一支**，不會出現
    「自動的那條降權了、手動的那條還在 Manager 進程裡跑」。direct 模式下該函式逐字
    沿用既有行為（就地 `write_gate_ledger`），本動作的產出與訊息面零變化。
    """

    from . import gate_ledger, gate_runner, terminal_contract
    from .manager import GATE_LEDGER_REQUIRED_PHASES

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_run_id",
    }
    if extras:
        raise ValueError(
            f"regenerate-gates rejects caller evidence/input: {sorted(extras)[0]}"
        )
    expected_run_id = args.get("expected_run_id")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("regenerate-gates requires exact expected_run_id")
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("regenerate-gates issue is not authorized by WorkAuthority")
    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    active = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id == authority.work_id
        and run.status == "ongoing"
        and run.issue_refs == expected_issues
        and _openspec_refs_compatible(run, authority)
    ]
    if len(active) != 1:
        raise RuntimeError("regenerate-gates requires one active canonical WorkflowRun")
    run = active[0]
    if run.run_id != expected_run_id:
        raise RuntimeError("regenerate-gates expected WorkflowRun CAS mismatch")
    if "needs_human" not in run.facets:
        raise RuntimeError("regenerate-gates requires needs_human workflow")

    candidates = [
        job
        for job in workflow_registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_phase") in GATE_LEDGER_REQUIRED_PHASES
        and job.get("status") in {"exited", "failed"}
        and isinstance(job.get("log_path"), str)
        and job.get("log_path")
        and Path(job["log_path"]).is_file()
    ]
    if not candidates:
        raise RuntimeError("regenerate-gates requires a terminal builder job log")
    job = candidates[-1]
    worktree = job.get("worktree")
    if not isinstance(worktree, str) or not Path(worktree).is_dir():
        raise RuntimeError("regenerate-gates requires the builder worktree to still exist")

    control_log_path = job.get("control_log_path")
    ledger_path = terminal_contract.gate_ledger_path(
        control_log_path
        if isinstance(control_log_path, str) and control_log_path
        else job["log_path"]
    )
    spool_key = gate_runner.spool_key_for_job(job)
    if spool_key is None:
        raise RuntimeError("regenerate-gates requires a resolvable gate spool key")
    # #738：ancestry baseline 與採信端 `_verify_build_candidate_transition` 同一條
    # 導出（已採信的 candidate，否則這張卡 provision 時的 dispatch_head）。缺席／
    # 非 sha 時不傳，ledger 的 ancestry_ok 維持 None，採信端自然退回既有路徑。
    baseline = None
    for value in (getattr(run, "candidate_head", None), job.get("dispatch_head")):
        if (
            isinstance(value, str)
            and gate_ledger._SHA_RE.fullmatch(value.lower()) is not None
        ):
            baseline = value.lower()
            break
    try:
        payload = gate_runner.run_declared_gates(
            job_id=str(job.get("job_id") or spool_key),
            spool_key=spool_key,
            ledger_path=ledger_path,
            worktree=worktree,
            ancestry_baseline=baseline,
        )
    except gate_ledger.GateSpecError as exc:
        # operator 宣告仍不合法：不寫出任何東西，把設定錯誤原樣回報。
        raise RuntimeError(f"regenerate-gates gate declaration invalid: {exc}") from exc
    except gate_runner.GateRunnerError as exc:
        # 降權模式下 gate 執行身分起不來／沒交付 ledger。**不退回 Manager 進程內
        # 執行**——那正是本動作要移除的那條提權路徑；診斷碼原樣帶出來讓 operator
        # 修部署，而不是靜默換一個更寬的身分把它跑起來。
        raise RuntimeError(
            f"regenerate-gates gate execution failed ({exc.reason}): {exc}"
        ) from exc
    gates = [
        {"name": row.get("name"), "status": row.get("status"), "exit_code": row.get("exit_code")}
        for row in payload.get("gates", [])
        if isinstance(row, dict)
    ]
    return {
        "action": "regenerate-gates",
        # 刻意不叫 "recovered"：本動作沒有改變任何 run/slice 狀態，只是把獨立
        # 證據重新產生出來，採信仍由既有流程負責。
        "reason": "gate-ledger-regenerated",
        "expected_run_id": expected_run_id,
        "run_id": run.run_id,
        "job_id": job.get("job_id"),
        "card_id": job.get("workflow_card"),
        "ledger_path": str(ledger_path),
        "ledger_digest": terminal_contract.gate_ledger_digest(payload),
        "gates": gates,
        "next_actions": ["resume"],
    }


def _recover_planning_action(
    *,
    args: dict[str, Any],
    authority,
    requested_by: str,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """重跑 define 階段 planning，避免環境類失敗造成永久 blocked。"""

    extras = set(args) - {
        "action",
        "repo",
        "work_id",
        "issue",
        "actor",
        "expected_run_id",
        "failure_classification",
        "failure_reason",
    }
    if extras:
        raise ValueError(
            f"recover-planning rejects caller evidence/input: {sorted(extras)[0]}"
        )
    expected_run_id = args.get("expected_run_id")
    failure_classification = args.get("failure_classification")
    failure_reason = args.get("failure_reason")
    actor = args.get("actor")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("recover-planning requires exact expected_run_id")
    if (
        not isinstance(failure_classification, str)
        or failure_classification not in {"environment", "content"}
    ):
        raise ValueError("recover-planning requires failure_classification")
    if (
        not isinstance(failure_reason, str)
        or not failure_reason.strip()
        or "\n" in failure_reason
    ):
        raise ValueError("recover-planning requires failure_reason")
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("recover-planning issue is not authorized by WorkAuthority")
    related = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo and run.work_id == authority.work_id
    ]
    exact = [run for run in related if run.run_id == expected_run_id]
    if len(exact) != 1:
        raise RuntimeError("recover-planning expected WorkflowRun CAS mismatch")
    run = exact[0]
    run_status = workflow_status(run)
    if run_status == "blocked":
        raise RuntimeError("recover-planning workflow is blocked (persisted-block)")
    if run_status == "done":
        raise RuntimeError("recover-planning requires needs_human workflow")
    if run.current_phase != "define":
        evidence_refs = tuple(
            ref for ref in run.evidence_refs if Path(ref).parent.name == "planning-recovery"
        )
        recovery = None
        for path_value in evidence_refs:
            try:
                body = json.loads(Path(path_value).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (
                isinstance(body, dict)
                and body.get("schema") == "cortex-work-planning-recovery/v1"
                and str(body.get("run_id")) == expected_run_id
            ):
                recovery = {"ref": path_value}
                break
        if recovery is None:
            return {
                "action": "recovered",
                "reason": "already-recovered",
                "expected_run_id": expected_run_id,
                "run": run.to_dict(),
                "failure_classification": failure_classification,
                "failure_reason": failure_reason,
            }
        return {
            "action": "recovered",
            "reason": "already-recovered",
            "expected_run_id": expected_run_id,
            "run": run.to_dict(),
            "evidence": recovery,
            "failure_classification": failure_classification,
                "failure_reason": failure_reason,
            }
    if "needs_human" not in run.facets:
        raise RuntimeError("recover-planning requires needs_human workflow in define")
    failure = _read_planning_failure_record(
        run=run, run_id=expected_run_id
    )
    if failure["classification"] != failure_classification:
        raise RuntimeError("recover-planning classification mismatch")
    if failure["classification"] != "environment":
        raise RuntimeError("recover-planning is not allowed for content failures")
    if failure["reason"] != failure_reason:
        raise RuntimeError("recover-planning requires matching failure_reason")
    actor_value = actor if isinstance(actor, str) and actor.strip() else requested_by
    if not isinstance(actor_value, str) or not actor_value.strip():
        actor_value = "operator"
    # #728 裁決（(B)）：`recovery_basis: "planning-runtime-retry"` 的語意是
    # 「**解除封鎖、讓下一拍重跑**」，不是「recover 內部已經重跑過 planning」。
    # 逐字證據（本函式與 manager 的實作，非推測）：
    #
    # 1. 本函式全程沒有任何 planner／runtime 呼叫——沒有 `runtime_factory`、沒有
    #    `run_heterogeneous_brainstorm`、沒有寫 `gate_refs`／`planning_authority`
    #    ／`planning_source_revision`。它只讀失敗 evidence、落一份稽核紀錄、
    #    清掉 `needs_human`／`blocked` 兩個 facet。
    # 2. 全庫**唯一**產生 brainstorm gate evidence 的路徑是
    #    `manager.apply_workflow_action` 的 define 段（`run_heterogeneous_
    #    brainstorm` → `gate_refs=result.gate_refs.as_tuple()` 與 `current_phase
    #    ="plan"` 在**同一次** registry 原子寫入內完成）。
    # 3. 而那條路徑的入口守衛逐字是 `if run.current_phase not in {"claim",
    #    "define"}: return ... "already-claimed"`；`work_bridge.
    #    start_canonical_workflow` 另有一條 `if existing_run.current_phase !=
    #    "define": return existing_run` 短路。
    #
    # ⇒ 把 phase 推到 `plan` 不是「前進」，而是**永久關掉**產生 brainstorm 背書
    # 的唯一入口。因此 `brainstorm_required` 且尚無自己的 brainstorm ref 時，
    # recover 必須留在 `define`，由正常 define 流程重跑並自然產生 evidence。
    #
    # 判準不在這裡另寫一份：直接拿 reconciliation 用的**同一個**函式，去問
    # 「我打算寫進去的那個 phase，是不是對帳的合法入口狀態」。答案是否就退回
    # `define`——出口狀態因此在結構上不可能不是合法入口狀態。
    recovered_phase = "plan" if brainstorm_authority_bound(run, phase="plan") else "define"
    record = _recover_planning_record(
        run,
        state_path=state_path,
        actor=actor_value,
        failure_classification=failure_classification,
        failure_reason=failure_reason,
        evidence_ref=failure["evidence_ref"],
        recovered_phase=recovered_phase,
    )
    current_facets = tuple(
        facet for facet in run.facets if facet not in {"needs_human", "blocked"}
    )
    evidence_refs = run.evidence_refs
    if record["ref"] not in evidence_refs:
        evidence_refs = (*evidence_refs, record["ref"])
    updated = workflow_registry._manager_update_workflow_run(
        run.run_id,
        current_phase=recovered_phase,
        facets=current_facets,
        gate_status="running",
        evidence_refs=evidence_refs,
    )
    return {
        "action": "recovered",
        "reason": "planning-recovery-dispatched",
        "expected_run_id": expected_run_id,
        # #728：出口 phase 是本動作的裁決結果，不再是寫死的 `plan`——operator
        # 不必翻 evidence 檔就看得到這次 recover 把 run 留在哪裡。
        "recovered_phase": recovered_phase,
        "failure_classification": failure_classification,
        "failure_reason": failure_reason,
        "failure_basis": failure,
        "evidence": record,
        "run": updated.to_dict(),
    }


def _recover_pre_candidate_action(
    *,
    args: dict[str, Any],
    authority,
    requested_by: str,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """恢復 candidate 產生前失敗的 builder slice / workflow。"""
    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_candidate",
    }
    if extras:
        raise ValueError(f"recover-pre-candidate rejects caller evidence/input: {sorted(extras)[0]}")
    expected_candidate = args.get("expected_candidate")
    if expected_candidate is not None and expected_candidate.lower() not in {"null", "none", ""}:
        raise ValueError("recover-pre-candidate requires null expected_candidate")
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("recover-pre-candidate issue is not authorized by WorkAuthority")

    matching_slices = [
        s for s in workflow_registry.list_slices()
        if s.get("slice_id") == authority.work_id or s.get("spec", {}).get("path", "").endswith(f"{authority.work_id}.md")
    ]
    if not matching_slices:
        matching_slices = workflow_registry.list_slices()

    target_slice = None
    for s in matching_slices:
        cand = s.get("candidate")
        if not (isinstance(cand, str) and verification.SAFE_SHA_RE.fullmatch(cand) is not None):
            target_slice = s
            break

    if target_slice is None:
        raise RuntimeError("recover-pre-candidate requires a slice with null candidate")

    slice_id = target_slice["slice_id"]
    if target_slice.get("state") not in {"needs_human", "failed", "pending"}:
        raise RuntimeError("recover-pre-candidate requires needs_human or failed slice")

    if target_slice.get("state") == "pending" and target_slice.get("builder_job_id") is None:
        return {
            "action": "recover-pre-candidate",
            "reason": "already-recovered",
            "slice_id": slice_id,
            "slice_state": "pending",
        }

    builder_job_id = target_slice.get("builder_job_id")
    wt_path = None
    if isinstance(builder_job_id, str):
        try:
            b_job = workflow_registry.get_job(builder_job_id)
            wt_path = b_job.get("worktree")
        except Exception:
            pass
    if not wt_path:
        wt_path = target_slice.get("worktree")

    # #478：舊碼用裸 `subprocess.run(["git", "worktree", ...])`（無 `-C <repo>`，
    # 實際跑在 manager 進程的 cwd 上）、`check=False` 吞錯，且只在目錄還在時
    # 觸發——registry 殘留與「目錄已消失但 registry 還在」都清不掉。統一改走
    # `worktree_reclaim`，後置條件不成立即 fail closed。
    # #645：記錄沒有 worktree 時的反推走共用 helper（與 `manager.apply_slice_action`
    # 同一份），新舊兩種目錄形狀都試；pool root 只在真的要反推時才解析（#612）。
    recorded = wt_path if isinstance(wt_path, (str, Path)) and wt_path else None
    pool_root = None
    if recorded is None:
        try:
            pool_root = paths.worktree_root()
        except Exception:
            pool_root = None
    branch_hint = target_slice.get("branch")
    reclaim = worktree_reclaim.reclaim_recorded_or_derived(
        recorded_path=recorded,
        pool_root=pool_root,
        job_id=slice_id,
        branch=branch_hint if isinstance(branch_hint, str) else None,
        preserve_root=state_path.resolve().parent / "evidence",
    )
    if reclaim is not None and not reclaim.ok:
        raise RuntimeError(
            "recover-pre-candidate worktree reclaim failed: "
            f"{reclaim.detail or reclaim.status} ({reclaim.path})"
        )

    actor = args.get("actor") or requested_by
    workflow_registry.record_action(
        slice_id,
        action="operator-recover-pre-candidate",
        actor=actor,
        state="pending",
        gate_state="pending",
        result="ok",
    )
    workflow_registry.update_slice(
        slice_id,
        state="pending",
        gate_state="pending",
        builder_job_id=None,
        candidate=None,
    )
    updated = workflow_registry.get_slice(slice_id)
    payload: dict[str, Any] = {
        "action": "recover-pre-candidate",
        "reason": "pre-candidate-slice-reset",
        "slice_id": slice_id,
        "slice_state": updated.get("state"),
        "gate_state": updated.get("gate_state"),
    }
    if reclaim is not None:
        payload["worktree_reclaim"] = reclaim.to_dict()
    return payload


def _find_repair_adoption_record(
    run,
    *,
    run_id: str,
    adopted_candidate: str,
) -> str | None:
    """掃描 run 既有 evidence_refs 是否已有本次 adoption 的 durable record（#260 D4）。

    只讀 canonical `evidence/work-repair-adoption/` 目錄下的檔案，用於
    `recover-repair-commit` 的冪等判定；不做任何寫入或狀態變更。
    """

    for path_value in run.evidence_refs:
        if not isinstance(path_value, str):
            continue
        path = Path(path_value)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.parent.name != "work-repair-adoption"
        ):
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        if (
            body.get("schema") == "cortex-work-repair-adoption/v1"
            and body.get("run_id") == run_id
            and body.get("adopted_candidate") == adopted_candidate
        ):
            return path_value
    return None


def _repair_adoption_record(
    *,
    run,
    state_path: Path,
    actor: str,
    failed_job_id: str,
    observed_head: str,
    adopted_candidate: str,
    previous_candidate: str,
) -> dict[str, str]:
    """寫入 `cortex-work-repair-adoption/v1` immutable evidence（#260 R3）。

    比照 `_abandon_record`／`_recover_planning_record` 的 create-with-O_EXCL +
    hardlink-publish 慣例；同內容重寫視為冪等，內容衝突 fail closed。
    """

    body = {
        "schema": "cortex-work-repair-adoption/v1",
        "run_id": run.run_id,
        "repo": run.repo,
        "work_id": run.work_id,
        "actor": actor,
        "failed_job_id": failed_job_id,
        "observed_head": observed_head,
        "adopted_candidate": adopted_candidate,
        "previous_candidate": previous_candidate,
    }
    digest = verification.canonical_json_hash(body)
    root = state_path.resolve().parent / "evidence" / "work-repair-adoption"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{run.run_id}-{digest}.json"
    content = (
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if target.exists():
        if target.is_symlink() or target.read_bytes() != content:
            raise RuntimeError("workflow repair-commit adoption evidence conflict")
    else:
        temporary = root / f".{target.name}.{uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError(
                "workflow repair-commit adoption evidence collision"
            ) from error
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), 0o444)
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != content:
                    raise RuntimeError(
                        "workflow repair-commit adoption evidence conflict"
                    )
            else:
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ref": str(target), "hash": digest}


def _recover_repair_commit_action(
    *,
    args: dict[str, Any],
    authority,
    requested_by: str,
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """對 build phase 失敗終止、但已在 worktree 留下合法 descendant commit 的
    repair job，做具雙 CAS 的窄化 adoption（#260 R1-R5）。

    判準全部取自系統既有紀錄與 git 事實：worktree 路徑取自 failed builder job
    row，HEAD／乾淨度／descendant lineage 全部由這裡的 git 呼叫重新確認；
    operator 帶的 ``expected_run_id``／``expected_candidate`` 只做交叉比對，
    不被當作 evidence 採信。此 action 不啟動任何 model session——adoption 完全
    由 manager 側確定性驗證完成。
    """

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor",
        "expected_run_id", "expected_candidate",
    }
    if extras:
        raise ValueError(
            f"recover-repair-commit rejects caller evidence/input: {sorted(extras)[0]}"
        )
    expected_run_id = args.get("expected_run_id")
    expected_candidate = args.get("expected_candidate")
    if (
        not isinstance(expected_run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", expected_run_id) is None
    ):
        raise ValueError("recover-repair-commit requires exact expected_run_id")
    if (
        not isinstance(expected_candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(expected_candidate) is None
    ):
        raise ValueError("recover-repair-commit requires exact expected_candidate")
    expected_candidate = expected_candidate.lower()
    issue = args.get("issue")
    if issue is not None and issue not in authority.mapped_issues:
        raise RuntimeError("recover-repair-commit issue is not authorized by WorkAuthority")
    expected_issues = tuple(
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    )
    related = [
        run
        for run in workflow_registry.list_workflow_runs()
        if run.repo == authority.repo and run.work_id == authority.work_id
    ]
    exact = [run for run in related if run.run_id == expected_run_id]
    if len(exact) != 1:
        raise RuntimeError("recover-repair-commit expected WorkflowRun CAS mismatch")
    run = exact[0]
    if run.issue_refs != expected_issues or not _openspec_refs_compatible(run, authority):
        raise RuntimeError(
            "recover-repair-commit WorkflowRun refs differ from current WorkAuthority"
        )
    actor = args.get("actor")
    actor_value = actor if isinstance(actor, str) and actor.strip() else requested_by
    if not isinstance(actor_value, str) or not actor_value.strip():
        actor_value = "operator"

    # #260 D4：冪等判定先於其餘 admission——adoption 成功後 run 已離開 build
    # phase，若不先檢查會被後面的 phase 檢查誤判為錯誤而非 replay。
    existing_record = _find_repair_adoption_record(
        run, run_id=expected_run_id, adopted_candidate=expected_candidate,
    )
    if run.candidate_head == expected_candidate and existing_record is not None:
        return {
            "action": "recover-repair-commit",
            "reason": "already-recovered",
            "expected_run_id": expected_run_id,
            "expected_candidate": expected_candidate,
            "run": run.to_dict(),
            "evidence": {"ref": existing_record},
        }

    if run.status != "ongoing":
        raise RuntimeError("recover-repair-commit requires ongoing workflow")
    if "needs_human" not in run.facets:
        raise RuntimeError("recover-repair-commit requires needs_human workflow")
    if run.current_phase != "build":
        raise RuntimeError("recover-repair-commit requires build phase workflow")
    original_candidate = run.candidate_head
    if (
        not isinstance(original_candidate, str)
        or verification.SAFE_SHA_RE.fullmatch(original_candidate) is None
    ):
        raise RuntimeError("recover-repair-commit requires an existing build candidate")
    original_candidate = original_candidate.lower()
    if expected_candidate == original_candidate:
        raise RuntimeError(
            "recover-repair-commit requires a new descendant commit; the Candidate "
            "is unchanged, use retry-verify instead"
        )
    build_steps = [step for step in run.steps if step.phase == "build"]
    if not build_steps:
        raise RuntimeError("recover-repair-commit requires build phase steps")
    if (
        any(step.gate_result != "passed" for step in build_steps[:-1])
        or build_steps[-1].gate_result != "pending"
    ):
        raise RuntimeError(
            "recover-repair-commit requires only the final builder card pending"
        )
    repair_card = build_steps[-1].card
    all_jobs = workflow_registry.list_jobs()
    if any(
        job.get("workflow_run_id") == run.run_id
        and job.get("status") in {"dispatched", "running"}
        for job in all_jobs
    ):
        raise RuntimeError("recover-repair-commit refuses active workflow job")
    same_card_jobs = [
        job
        for job in all_jobs
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_phase") == "build"
        and job.get("workflow_card") == repair_card
    ]
    if not same_card_jobs:
        raise RuntimeError("recover-repair-commit requires an existing failed builder job")
    failed_job = same_card_jobs[-1]
    if failed_job.get("status") not in {"exited", "failed"}:
        raise RuntimeError("recover-repair-commit requires a terminalized builder job")
    if failed_job.get("workflow_evidence") is not None:
        raise RuntimeError(
            "recover-repair-commit refuses a job with bound workflow evidence"
        )

    worktree = failed_job.get("worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        raise RuntimeError("recover-repair-commit requires a known builder worktree")
    from .manager import _job_gate_worktree_state

    state = _job_gate_worktree_state(failed_job)
    if state is None:
        raise RuntimeError("recover-repair-commit gate ledger worktree_state unavailable")
    # #763：recover-repair-commit 的 builder-owned HEAD/dirty/ancestry 證據只允許
    # 走授權 gate ledger handoff；缺席或半套 state 一律 fail-closed，不再回頭讀 builder 樹。
    head_value = state.get("head")
    if (
        not isinstance(head_value, str)
        or verification.SAFE_SHA_RE.fullmatch(head_value) is None
    ):
        raise RuntimeError("recover-repair-commit gate ledger HEAD invalid")
    head_value = head_value.lower()
    if head_value != expected_candidate:
        raise RuntimeError(
            "recover-repair-commit expected_candidate CAS mismatch against gate ledger HEAD"
        )
    dirty_total = state.get("dirty_total")
    if type(dirty_total) is not int or dirty_total < 0:
        raise RuntimeError("recover-repair-commit gate ledger dirty state invalid")
    if dirty_total != 0:
        raise RuntimeError("recover-repair-commit requires a clean worktree")
    if state.get("ancestry_baseline") != original_candidate:
        raise RuntimeError("recover-repair-commit candidate ancestry unavailable")
    ancestry_ok = state.get("ancestry_ok")
    if ancestry_ok is False:
        raise RuntimeError("recover-repair-commit requires a descendant candidate")
    if ancestry_ok is not True:
        raise RuntimeError("recover-repair-commit candidate ancestry unavailable")

    record = _repair_adoption_record(
        run=run,
        state_path=state_path,
        actor=actor_value,
        failed_job_id=str(failed_job.get("job_id")),
        observed_head=expected_candidate,
        adopted_candidate=expected_candidate,
        previous_candidate=original_candidate,
    )
    adoption_job = workflow_registry.create_job(
        task=str(failed_job.get("task")),
        persona="builder",
        kind="build",
        branch=str(failed_job.get("branch")),
        pane="",
        worktree=worktree,
        dispatch_head=failed_job.get("dispatch_head"),
        executor=failed_job.get("executor"),
        model_id=failed_job.get("model_id"),
        independence_domain=failed_job.get("independence_domain"),
        subject_head=expected_candidate,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=repair_card,
        workflow_phase="build",
        workflow_repo_root=failed_job.get("workflow_repo_root"),
        workflow_input_root=failed_job.get("workflow_input_root"),
        workflow_inputs=tuple(failed_job.get("workflow_inputs") or ()),
        workflow_input_snapshot=tuple(
            dict(item) for item in (failed_job.get("workflow_input_snapshot") or ())
        ),
        workflow_outputs=tuple(failed_job.get("workflow_outputs") or ()),
        source_revision=run.source_revision,
        workflow_sandbox_hash=failed_job.get("workflow_sandbox_hash"),
        workflow_output_baseline=tuple(
            dict(item) for item in (failed_job.get("workflow_output_baseline") or ())
        ),
    )
    workflow_registry.update_headless_result(
        adoption_job["job_id"], status="exited", exit_code=0
    )
    locator = {
        "kind": "work-repair-adoption",
        "path": record["ref"],
        "hash": record["hash"],
    }
    workflow_registry.bind_workflow_evidence(
        adoption_job["job_id"], locator=locator, subject_head=expected_candidate,
    )
    updated = workflow_registry._manager_adopt_repair_candidate(
        run.run_id,
        expected_candidate=original_candidate,
        adopted_candidate=expected_candidate,
        adoption_job_id=adoption_job["job_id"],
        evidence_ref=record["ref"],
    )
    return {
        "action": "recover-repair-commit",
        "reason": "repair-commit-adopted",
        "expected_run_id": expected_run_id,
        "expected_candidate": expected_candidate,
        "previous_candidate": original_candidate,
        "evidence": record,
        "adoption_job_id": adoption_job["job_id"],
        "run": updated.to_dict(),
    }


#: `#506`：auto-claim scan 逐 issue 讀 label 之間的最小間隔（毫秒）。
#:
#: 這條路徑是 fleet 對 GitHub 最大的持續壓力來源——實測 cortex instance 有 57 個
#: `confirmed_todo` authority 帶 mapped issue，每個 tick 就是 57 次連發的 `gh api`，
#: 且 PR `#512` 的 `GitHubPressureGate` 只注入到 `monitor/providers.py`，coordinator
#: 這一側完全不受節流也不受退避管（monitor 退避期間 manager 照打）。GitHub 的
#: secondary／abuse-detection limit 抓的正是這種連發形狀，實測會把整個帳號推進
#: 懲罰窗，進而讓 `provider-authority-rate-limited-canonical` 擋下所有 claim。
#:
#: 這裡採用與 monitor 相同的「攤平」策略而非降頻：把一輪的 N 次請求攤在 N × interval
#: 秒內送出。設 0 可完全停用（保留舊行為，供測試與單 issue 部署使用）。
DEFAULT_AUTO_CLAIM_GITHUB_INTERVAL_MS = 1000


def _auto_claim_github_interval_seconds() -> float:
    raw = os.environ.get("PSC_MANAGER_GITHUB_INTERVAL_MS", "").strip()
    if not raw:
        return DEFAULT_AUTO_CLAIM_GITHUB_INTERVAL_MS / 1000.0
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            "PSC_MANAGER_GITHUB_INTERVAL_MS 必須是非負整數（毫秒）"
        ) from error
    if value < 0:
        raise ValueError("PSC_MANAGER_GITHUB_INTERVAL_MS 必須是非負整數（毫秒）")
    return value / 1000.0


def run_auto_claim_scan(
    *,
    snapshot_path: str | Path | None = None,
    state_path: str | Path | None = None,
    now: Callable[[], float] = time.time,
    runner: Runner = subprocess.run,
    workflow_registry=None,
    workflow_starter=None,
    readiness_checker=None,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Project the durable Monitor snapshot into Manager-owned auto claims."""

    try:
        authorities, snapshot_items = load_work_authorities_with_snapshot_items(
            snapshot_path=snapshot_path
        )
    except ValueError as exc:
        if "snapshot unavailable" in str(exc):
            return []
        raise
    resolved_state = Path(state_path) if state_path is not None else _run_state_path()
    if workflow_registry is None:
        from .registry import JobRegistry

        workflow_registry = JobRegistry(state_path=resolved_state.parent / "jobs.json")
    if workflow_starter is None:
        workflow_starter = _fallback_workflow_starter(workflow_registry, resolved_state)
    results: list[dict[str, Any]] = []
    github_interval = _auto_claim_github_interval_seconds()
    issued_github_reads = 0
    # #506：一旦本輪撞到 rate limit 就整批停手。舊行為是每個 authority 各自撞一次
    # 才 break 自己那圈，於是限流期間每個 tick 仍會送出 O(authorities) 次必定失敗
    # 的請求——每一次都在延長帳號層級的懲罰窗，形成「越限流越打、越打越限流」的
    # 正回饋。退避窗綁 token 不綁 repo，因此一次命中就足以判定整輪無效。
    rate_limited = False
    for authority in authorities:
        if not authority.confirmed_todo:
            continue
        # R0.5 D1：auto label 先讀鏡像（monitor 的 GitHubWorkProvider 已把持有
        # auto label 的 issue 編號寫進 provider observations，authority.auto_label
        # 據此導出）。鏡像為 False 的 authority **零 API 呼叫**直接進 claim 決策
        # （decide_auto_claim 以 auto-label-missing ignore）——這一步把先前
        # 每 tick 對每個 mapped issue 各發一次 live gh api 的 O(n) sweep
        # （實測 57 次/tick）降為 O(鏡像為 True 的 authority 數)，通常為 0。
        # 鏡像為 True 時才做**一次 targeted 複驗**：label 可能在兩次 monitor
        # refresh 之間被人類移除，claim 是不可逆動作，行前以單發 live 讀取確認。
        live_auto_label = authority.auto_label
        if live_auto_label and authority.mapped_issues:
            # 只有真的需要打 GitHub 的 authority 才受這道停手影響；鏡像 False
            # 或沒有 mapped issue 的 authority 不讀 label，限流與它無關，照常 claim。
            if rate_limited:
                results.append(
                    {
                        "repo": authority.repo,
                        "work_id": authority.work_id,
                        "action": "blocked",
                        "reason": "github-rate-limited-scan-aborted",
                    }
                )
                continue
            issue_reads_failed = False
            verified = False
            for issue in authority.mapped_issues:
                if github_interval > 0 and issued_github_reads:
                    sleeper(github_interval)
                issued_github_reads += 1
                completed = runner(
                    ["gh", "api", f"repos/{authority.repo}/issues/{issue}"],
                    shell=False,
                    capture_output=True,
                    text=True,
                )
                if getattr(completed, "returncode", None) != 0:
                    stderr = getattr(completed, "stderr", "") or ""
                    stdout = getattr(completed, "stdout", "") or ""
                    if is_rate_limit_signal(f"{stderr}\n{stdout}"):
                        rate_limited = True
                    results.append(
                        {
                            "repo": authority.repo,
                            "work_id": authority.work_id,
                            "action": "blocked",
                            "reason": (
                                "github-rate-limited"
                                if rate_limited
                                else "github-label-read-failed"
                            ),
                        }
                    )
                    issue_reads_failed = True
                    break
                try:
                    issue_payload = json.loads(getattr(completed, "stdout", ""))
                    labels = issue_payload["labels"]
                    if not isinstance(labels, list) or any(
                        not isinstance(label, dict) or not isinstance(label.get("name"), str)
                        for label in labels
                    ):
                        raise TypeError
                    names = {label["name"] for label in labels}
                except (json.JSONDecodeError, KeyError, TypeError):
                    results.append(
                        {
                            "repo": authority.repo,
                            "work_id": authority.work_id,
                            "action": "blocked",
                            "reason": "github-label-payload-malformed",
                        }
                    )
                    issue_reads_failed = True
                    break
                if AUTO_LABEL in names:
                    verified = True
                    break
            if issue_reads_failed:
                continue
            # 複驗結果為準：鏡像說有、live 說沒有 → 以 live 為準（label 已被移除）。
            live_auto_label = verified
        try:
            result = _claim_action(
                args={"action": "auto-scan"},
                authority=authority,
                requested_by="manager-daemon",
                now_epoch=now(),
                state_path=resolved_state,
                automatic=True,
                auto_label=live_auto_label,
                workflow_registry=workflow_registry,
                workflow_starter=workflow_starter,
                readiness_checker=readiness_checker,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            # 單一 authority 的 claim 失敗（例如 #246 daemon tick isolation：
            # start_canonical_workflow -> resolve_trusted_repo_root 對不在信任
            # 清單中的 repo fail-closed raise ValueError）不得讓整批 scan 中止；
            # KeyboardInterrupt/SystemExit 不在攔截範圍，照常往外傳。
            reason = (
                "repo-root-unresolved"
                if isinstance(exc, ValueError) and "trusted repo registry" in str(exc)
                else "claim-failed"
            )
            results.append(
                {
                    "repo": authority.repo,
                    "work_id": authority.work_id,
                    "action": "blocked",
                    "reason": reason,
                    "error": safe_exception_summary(exc),
                }
            )
            continue
        if result["action"] not in {"ignore", "done"}:
            results.append(
                {
                    "repo": authority.repo,
                    "work_id": authority.work_id,
                    **result,
                }
            )
    not_claimable.sweep_missing(
        not_claimable.ledger_path(resolved_state.parent),
        present_items=snapshot_items,
    )
    return results


# #218 AC2：語意 re-claim 世代上限——v1..v3 共三個 superseded 世代後熔斷，
# 不得自動建立 v4（design #208 E 原文）。
SEMANTIC_RECLAIM_LIMIT = 3


# #218 AC3: needs_human 停止時揭露剩餘 repair scope、已重複 stage、合法下一步
# 與預估 invalidation 範圍。legal_next_steps 只列出真正已由
# _recoverable_maintainer_ship_stop 承認的重入路徑（maintainer-review）；
# invalidation_scope 是若人工仍要求再開一輪會需要重新走的 phase 範圍——ship 階
# 段的 repair 迴圈只影響 ship 本身，不會回頭讓 build/verify 失效。
def _repair_budget_status(
    *, fix_rounds: int, max_fix_rounds: int, current_phase: str
) -> dict[str, Any]:
    return {
        "repair_rounds_used": fix_rounds,
        "repair_rounds_budget": max_fix_rounds,
        "repair_rounds_remaining": max(max_fix_rounds - fix_rounds, 0),
        "repeated_stage": current_phase,
        "legal_next_steps": ("maintainer-review",),
        "invalidation_scope": (current_phase,),
    }


def _ship_action(
    *,
    args: dict[str, Any],
    authority,
    runner: Runner,
    now: Callable[[], float],
    state_path: Path,
    workflow_registry,
) -> dict[str, Any]:
    """Advance one fail-closed delivery stage for the exact durable work item.

    The operation is intentionally resumable: requesting Copilot review, merging,
    and proving remote closure are distinct durable transitions.  A daemon crash
    therefore cannot silently repeat a merge or manufacture terminal evidence.
    """

    repo_root = _canonical_repo_root(args.get("repo_root"), repo=authority.repo)
    skip_tests = args.get("skip_tests", False)
    if not isinstance(skip_tests, bool):
        raise ValueError("ship skip_tests must be a strict boolean")
    state, active, canonical_run = _load_work_run(
        state_path=state_path,
        workflow_registry=workflow_registry,
        authority=authority,
    )
    _validate_current_run_authority(active, authority, canonical_run)
    # #218 AC1：work-item repair budget 依 sizing band 參數化；band 尚未掛
    # （None，#222 既有 work item）時 repair_budget_for_band fail-soft 回退到
    # 現行 MAX_FIX_ROUNDS=2。red 由 repair_budget_for_band 防禦性拒絕——
    # #223 的路由本應在更早階段攔截，這裡是最後一道防線。
    max_fix_rounds = repair_budget_for_band(canonical_run.sizing_band)
    if active.get("snapshot_hash") != authority.snapshot_hash:
        active["snapshot_hash"] = authority.snapshot_hash
        _save_runs(state_path, state)
    if (
        len(authority.mapped_prs) != 1
        or len(authority.mapped_openspec) > 1
        or len(authority.mapped_todo_paths) != 1
    ):
        active["ship"] = {
            "phase": "needs_human",
            "reason": "multiple-delivery-targets-unsupported",
        }
        _save_runs(state_path, state)
        workflow_registry._manager_update_workflow_run(
            canonical_run.run_id,
            facets=("needs_human",),
            gate_status="running",
            needs_human_reason=diagnostic_reason(
                "multiple-delivery-targets-unsupported",
                "work item 的交付 correlation 尚未收斂到 ship lane 支援的唯一組合"
                f"（需要 pr=1、todo=1、openspec=0 或 1；觀察到 prs={len(authority.mapped_prs)} "
                f"openspec={len(authority.mapped_openspec)} todo={len(authority.mapped_todo_paths)}）。"
                "請先用 `cortex work unlink` 修正多餘的 delivery correlation，待 snapshot 更新後再 `resume`。",
                source="work_actions._ship_action:delivery-targets",
                run_id=canonical_run.run_id,
                work_id=canonical_run.work_id,
                repo=authority.repo,
            ),
        )
        return {
            "action": "needs_human",
            "reason": "multiple-delivery-targets-unsupported",
        }
    ship = active.get("ship")
    if ship is not None and not isinstance(ship, dict):
        raise ValueError("ship state malformed")
    if (
        isinstance(ship, dict)
        and ship.get("phase") == "needs_human"
        and ship.get("reason") == "multiple-delivery-targets-unsupported"
        and active.get("delivery_binding") is None
    ):
        # This stop is established before any delivery binding or external
        # mutation.  Once operator-owned correlation resolves to the v1 ship
        # shape (PR=1, Todo=1, OpenSpec=0 or 1), an explicit resume may safely
        # re-arm the same WorkflowRun instead of requiring registry surgery.
        active.pop("ship")
        _save_runs(state_path, state)
        canonical_run = workflow_registry._manager_update_workflow_run(
            canonical_run.run_id,
            facets=tuple(
                facet for facet in canonical_run.facets if facet != "needs_human"
            ),
            gate_status="running",
        )
        ship = None
    binding = _ship_binding(args, authority)
    pr_number = binding["pr_number"]
    change = binding["change"]
    todo_paths_value = binding["todo_paths"]
    protected_refs = [
        *([f"openspec/changes/{change}"] if change is not None else []),
        *todo_paths_value,
    ]
    if any(_path_has_symlink(repo_root, ref) for ref in protected_refs):
        raise ValueError("ship authorized repo path must not traverse a symlink")
    metadata = _pr_metadata(args, required_issues=authority.mapped_issues)
    persisted_binding = active.get("delivery_binding")
    if persisted_binding is None:
        active["delivery_binding"] = binding
        _save_runs(state_path, state)
    elif persisted_binding != binding:
        _invalidate_copilot_rearm_permit(
            active=active,
            state=state,
            state_path=state_path,
        )
        raise RuntimeError("ship delivery binding differs from persisted PR/OpenSpec/Todo refs")
    github = GitHubDeliveryClient(runner=runner)
    orchestrator = ShipOrchestrator(github=github, now=now)
    rearm_permit = None
    if (
        isinstance(ship, dict)
        and ship.get("phase") == "needs_human"
        and ship.get("reason") == "copilot-review-timeout"
        and active.get("copilot_review_rearm_permit") is not None
    ):
        permit = _copilot_rearm_permit(active)
        assert permit is not None
        current_head = _exact_verified_candidate_head(canonical_run)
        if current_head is None:
            raise RuntimeError("persisted copilot timeout rearm permit requires exact verified Candidate")
        if (
            permit["run_id"] != canonical_run.run_id
            or permit["ship_hash"] != _ship_state_hash(ship)
            or permit["old_head"] != ship.get("head")
            or permit["candidate_head"] != current_head
            or permit["authority_digest"] != work_authority_digest(authority)
            or permit["delivery_binding_hash"] != _delivery_binding_hash(binding)
            or permit["history_prefix_hash"] != _delivery_review_history_hash(active)
        ):
            _invalidate_copilot_rearm_permit(
                active=active,
                state=state,
                state_path=state_path,
            )
            raise RuntimeError("persisted copilot timeout rearm permit conflicts with current state")
        if permit["old_head"] == current_head:
            _invalidate_copilot_rearm_permit(
                active=active,
                state=state,
                state_path=state_path,
            )
            raise RuntimeError("copilot timeout rearm permit no longer targets a new HEAD")
        rearm_permit = permit

    maintainer_recovery = _recoverable_maintainer_ship_stop(ship=ship, args=args)
    if ship and ship.get("phase") == "needs_human" and not maintainer_recovery and rearm_permit is None:
        res = {"action": "needs_human", "reason": ship.get("reason")}
        if str(ship.get("reason", "")).startswith("copilot-"):
            res["next_actions"] = ["abandon", "review-attest"]
            res["next_step_hint"] = (
                f"cortex work review-attest {canonical_run.work_id} --repo {authority.repo} --actor <operator> --payload <file>"
            )
        return res

    if ship and ship.get("phase") == "merged":
        expected_head = ship.get("head")
        tree_hash = ship.get("tree_hash")
        authorization = ship.get("merge_authorization")
        if (
            not isinstance(expected_head, str)
            or not isinstance(tree_hash, str)
            or not _authorization_identity_matches(
                authorization,
                active=active,
                authority=authority,
                binding=binding,
                head=expected_head,
                tree_hash=tree_hash,
                terminal_reconciliation=True,
            )
        ):
            raise ValueError("ship merged state malformed")
        completion_payload = _json_file(
            args.get("completion_record_path"),
            field="completion_record_path",
        )
        closure = orchestrator.verify_remote_closure(
            repo=authority.repo,
            pr_number=pr_number,
            change=change,
            authority=authority,
            todo_paths=tuple(todo_paths_value),
            expected_head=expected_head,
            completion_payload=completion_payload,
            run_id=active["run_id"],
            workflow_step_ids=tuple(active["workflow_step_ids"]),
            trusted_evidence_refs=_trusted_evidence_refs(authorization),
        )
        active["ship"] = {
            **ship,
            "phase": "done",
            "todo_paths": list(todo_paths_value),
            "completion_record": dict(closure.completion_record),
        }
        _save_runs(state_path, state)
        source_revisions = {
            source.rsplit("@", 1)[0]: source.rsplit("@", 1)[1]
            for source in authority.source_revisions
            if "@" in source
        }
        if canonical_run.current_phase == "ship":
            # #275：canonical engineering outcome 必須在 terminal transition
            # （status="done"）之前 durable 寫入，讓外部 learning systems 有一個
            # 不受 WorkflowRun in-place 覆寫影響的 append-only 記錄可讀。
            # attempt_digest 用 completion record hash——同一次 merge 重跑
            # ship（daemon restart／request retry）會算出同一個 hash，因此
            # OutcomeStore.append 據此去重，不產生第二筆 outcome。
            outcome_store = engineering_outcome.OutcomeStore(
                engineering_outcome.outcome_store_path(state_path, repo=authority.repo)
            )
            engineering_outcome.emit_outcome(
                outcome_store,
                run=canonical_run,
                authority=authority,
                jobs=workflow_registry.list_jobs(),
                outcome="shipped",
                attempt_digest=str(closure.completion_record["hash"]),
                candidate={
                    "pr_number": pr_number,
                    "openspec_change": change,
                    "sha": expected_head,
                    "merge_commit": closure.facts.merge_commit,
                },
                verification={"todo_paths": list(todo_paths_value)},
                review={
                    "merge_authorization_hash": (
                        authorization.get("hash") if isinstance(authorization, dict) else None
                    ),
                },
            )
            workflow_registry._manager_update_workflow_run(
                canonical_run.run_id,
                status="done",
                completion_record_path=str(closure.completion_record["path"]),
                completion_record_hash=str(closure.completion_record["hash"]),
                completion_record_revision=expected_head,
                completion_source_revisions=source_revisions,
                pr_candidate=expected_head,
                merge_revision=closure.facts.merge_commit,
                facets=(),
                gate_status="passed",
            )
        return {
            "action": "done",
            "head": expected_head,
            "merge_commit": closure.facts.merge_commit,
            "completion_record": dict(closure.completion_record),
        }
    if ship and ship.get("phase") == "done":
        # Terminal cache is not authority: replay the authenticated remote
        # closure before returning done.
        expected_head = ship.get("head")
        record = ship.get("completion_record")
        todo_paths = ship.get("todo_paths")
        tree_hash = ship.get("tree_hash")
        authorization = ship.get("merge_authorization")
        if (
            not isinstance(expected_head, str)
            or not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("hash"), str)
            or not isinstance(todo_paths, list)
            or not isinstance(tree_hash, str)
            or not _authorization_identity_matches(
                authorization,
                active=active,
                authority=authority,
                binding=binding,
                head=expected_head,
                tree_hash=tree_hash,
                terminal_reconciliation=True,
            )
        ):
            raise ValueError("cached done state malformed")
        from . import completion

        replacement_path = args.get("completion_record_path")
        if replacement_path is None:
            completion_payload = completion.read_completion_record(
                record["path"], expected_hash=record["hash"]
            )
        else:
            completion_payload = _json_file(
                replacement_path,
                field="completion_record_path",
            )
        closure = orchestrator.verify_remote_closure(
            repo=authority.repo,
            pr_number=pr_number,
            change=change,
            authority=authority,
            todo_paths=tuple(todo_paths),
            expected_head=expected_head,
            completion_payload=completion_payload,
            run_id=active["run_id"],
            workflow_step_ids=tuple(active["workflow_step_ids"]),
            trusted_evidence_refs=_trusted_evidence_refs(authorization),
        )
        active["ship"] = {
            **ship,
            "completion_record": dict(closure.completion_record),
        }
        _save_runs(state_path, state)
        return {
            "action": "done",
            "head": expected_head,
            "merge_commit": closure.facts.merge_commit,
            "completion_record": dict(closure.completion_record),
        }

    if ship and ship.get("phase") == "merge-authorized":
        expected_head = ship.get("head")
        tree_hash = ship.get("tree_hash")
        authorization = ship.get("merge_authorization")
        if (
            not isinstance(expected_head, str)
            or not isinstance(tree_hash, str)
            or not _authorization_identity_matches(
                authorization,
                active=active,
                authority=authority,
                binding=binding,
                head=expected_head,
                tree_hash=tree_hash,
            )
        ):
            raise ValueError("ship merge-authorized state malformed")
        merge_status = github.fetch_merge_status(
            repo=authority.repo,
            pr_number=pr_number,
        )
        if merge_status.merged:
            if merge_status.pr_head != expected_head:
                raise RuntimeError("merged PR HEAD does not match authorized HEAD")
            active["ship"] = {
                **ship,
                "phase": "merged",
                "merge_commit": merge_status.merge_commit,
            }
            _save_runs(state_path, state)
            workflow_registry._manager_update_workflow_run(
                canonical_run.run_id,
                facets=("needs_human",),
                gate_status="running",
                needs_human_reason=diagnostic_reason(
                    "merged-awaiting-closure",
                    _merged_awaiting_closure_detail(change),
                    source="work_actions._ship_action:merged-awaiting-closure",
                    run_id=canonical_run.run_id,
                    work_id=canonical_run.work_id,
                    head=expected_head,
                    merge_commit=merge_status.merge_commit,
                ),
            )
            return {"action": "merged-awaiting-closure", "head": expected_head}

    active_change = repo_root / "openspec" / "changes" / change if change is not None else None
    if active_change is not None and active_change.is_dir():
        _validate_local_archive_inputs(
            repo_root=repo_root,
            change=change,
            runner=runner,
        )
        archived = runner(
            build_openspec_archive_argv(change),
            cwd=str(repo_root),
            shell=False,
            capture_output=True,
            text=True,
        )
        if getattr(archived, "returncode", None) != 0:
            raise RuntimeError("official OpenSpec archive failed")
        return {
            "action": "archive-applied-needs-commit",
            "change": change,
            "next_action": "commit and push the archive diff, then enqueue ship again",
        }

    github.ensure_pr_metadata(
        repo=authority.repo,
        pr_number=pr_number,
        title=metadata.title,
        body=metadata.body,
        labels=metadata.labels,
    )
    command = load_preflight_command()
    preflight = run_preflight(
        repo_root=repo_root,
        command=command,
        request=PreflightRequest(
            pr_number=pr_number,
            skip_tests=skip_tests,
            tree_hash=args.get("tree_hash"),
        ),
        runner=runner,
        now=now,
    )
    if not preflight.passed:
        raise RuntimeError(f"ship preflight failed: {preflight.failed_stage}")

    remote = github.fetch_delivery_facts(
        repo=authority.repo,
        pr_number=pr_number,
        change=change,
    )
    if remote.head != preflight.head:
        _invalidate_copilot_rearm_permit(
            active=active,
            state=state,
            state_path=state_path,
        )
        raise RuntimeError("ship HEAD differs from authenticated GitHub PR")
    if (
        remote.openspec_required
        and (not remote.active_openspec_absent or not remote.archive_present)
    ):
        raise RuntimeError("official OpenSpec archive is not present on the exact PR HEAD")

    merge_status = github.fetch_merge_status(repo=authority.repo, pr_number=pr_number)
    if merge_status.merged:
        if merge_status.pr_head != preflight.head:
            raise RuntimeError("merged PR HEAD does not match exact preflight HEAD")
        authorization = ship.get("merge_authorization") if ship else None
        if not _authorization_matches(
            authorization,
            active=active,
            authority=authority,
            binding=binding,
            preflight=preflight,
            remote=remote,
        ):
            active["ship"] = {
                **(ship or {}),
                "phase": "needs_human",
                "reason": "external-merge-without-authorization",
                "head": preflight.head,
                "tree_hash": preflight.tree_hash,
            }
            _save_runs(state_path, state)
            workflow_registry._manager_update_workflow_run(
                canonical_run.run_id,
                facets=("needs_human",),
                gate_status="running",
                needs_human_reason=diagnostic_reason(
                    "external-merge-without-authorization",
                    "PR 在 cortex 授權之外被 merge（本地沒有對應的 merge "
                    "authorization 紀錄），交付鏈路不得自行採信",
                    source="work_actions._ship_action:external-merge",
                    run_id=canonical_run.run_id,
                    work_id=canonical_run.work_id,
                    head=preflight.head,
                    tree_hash=preflight.tree_hash,
                ),
            )
            return {
                "action": "needs_human",
                "reason": "external-merge-without-authorization",
            }
        active["ship"] = {
            **(ship or {}),
            "phase": "merged",
            "head": preflight.head,
            "tree_hash": preflight.tree_hash,
            "merge_commit": merge_status.merge_commit,
            "pr_number": pr_number,
            "change": change,
            "todo_paths": list(todo_paths_value),
            "merge_authorization": authorization,
        }
        _save_runs(state_path, state)
        return {"action": "merged-awaiting-closure", "head": preflight.head}

    now_epoch = now()
    if (
        not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
    ):
        raise ValueError("ship clock must be finite")
    previous_head = ship.get("head") if ship else None
    # #218：repair round 計數提升到 work-item 層（active 頂層，與
    # delivery_binding／snapshot_hash 平級），不再只存在 active["ship"] 裡
    # ——後者會在 multiple-delivery-targets-unsupported 復原路徑被整包
    # pop 掉（見本函式稍早的 active.pop("ship")），若計數留在那裡會被
    # 一併歸零，形同無上限。
    fix_rounds = active.get("repair_rounds", 0)
    if not isinstance(fix_rounds, int) or isinstance(fix_rounds, bool) or fix_rounds < 0:
        raise ValueError("ship fix round state malformed")
    if maintainer_recovery or args.get("maintainer_review_path") is not None:
        if (
            ship is not None
            and ship.get("phase") == "needs_human"
            and ship.get("reason") == "copilot-review-timeout"
            and ship.get("head") != preflight.head
        ):
            raise RuntimeError("copilot timeout rearm requires exact-HEAD Copilot review")
        return _ship_with_maintainer_review(
            args=args,
            active=active,
            state=state,
            state_path=state_path,
            authority=authority,
            canonical_run=canonical_run,
            binding=binding,
            preflight=preflight,
            remote=remote,
            orchestrator=orchestrator,
            github=github,
            ship=ship,
            fix_rounds=fix_rounds,
        )
    if rearm_permit is not None and preflight.head != rearm_permit["candidate_head"]:
        _invalidate_copilot_rearm_permit(
            active=active,
            state=state,
            state_path=state_path,
        )
        raise RuntimeError("ship preflight HEAD differs from explicit resume permit")
    foreign_review = None
    if rearm_permit is not None:
        foreign_review = ForeignReviewEvidence(
            path=str(_absolute_file(args.get("foreign_review_path"), field="foreign_review_path")),
            expected_hash=args.get("foreign_review_hash"),
        )
        _validate_foreign_review(
            foreign_review,
            expected_head=preflight.head,
        )
    if ship and ship.get("phase") == "review-requesting":
        if previous_head != preflight.head:
            return _set_copilot_request_outcome_unknown(
                active=active,
                ship=ship,
                state=state,
                state_path=state_path,
                workflow_registry=workflow_registry,
                canonical_run=canonical_run,
                authority=authority,
                detail=(
                    "已持久化的 review-requesting epoch 與 current exact PR HEAD 不一致；"
                    "Copilot request outcome 無法確認，為避免重送已 fail-closed。"
                ),
            )
        requested_at = ship.get("requested_at_epoch")
        if (
            not isinstance(requested_at, (int, float))
            or isinstance(requested_at, bool)
            or not math.isfinite(float(requested_at))
            or "adopted_review_id" in ship
            or "adopted_at_epoch" in ship
        ):
            return _set_copilot_request_outcome_unknown(
                active=active,
                ship=ship,
                state=state,
                state_path=state_path,
                workflow_registry=workflow_registry,
                canonical_run=canonical_run,
                authority=authority,
                detail=(
                    "已持久化的 review-requesting epoch 缺少有效 request epoch，或帶有不適用的 "
                    "adoption 欄位；無法證明 review 屬於此 request，已 fail-closed。"
                ),
            )
        adopted_review = _pick_adoptable_copilot_review(
            remote.copilot_reviews,
            head=preflight.head,
        )
        if adopted_review is None or adopted_review.submitted_at_epoch < float(requested_at):
            return _set_copilot_request_outcome_unknown(
                active=active,
                ship=ship,
                state=state,
                state_path=state_path,
                workflow_registry=workflow_registry,
                canonical_run=canonical_run,
                authority=authority,
                detail=(
                    "已持久化的 review-requesting epoch 重播時找不到有效的 exact-HEAD "
                    "request-bound Copilot review；Copilot request outcome 無法確認，為避免重送已 fail-closed。"
                ),
            )
        logger.info(
            "recovered request-bound Copilot review from review-requesting run_id=%s head=%s review_id=%s submitted_at=%s",
            canonical_run.run_id,
            preflight.head,
            adopted_review.review_id,
            adopted_review.submitted_at_epoch,
        )
        active["ship"] = {
            **ship,
            "phase": "review-requested",
        }
        _save_runs(state_path, state)
        ship = active["ship"]
    if ship and ship.get("phase") == "needs-fix" and previous_head == preflight.head:
        return {"action": "fix-required", "head": preflight.head, "fix_rounds": fix_rounds}
    if previous_head is not None and previous_head != preflight.head:
        fix_rounds += 1
        active["repair_rounds"] = fix_rounds
        if fix_rounds > max_fix_rounds:
            active.pop("copilot_review_rearm_permit", None)
            active["ship"] = {
                **ship,
                "phase": "needs_human",
                "reason": "copilot-finding-budget-exhausted",
                "head": preflight.head,
                "fix_rounds": fix_rounds,
            }
            _save_runs(state_path, state)
            workflow_registry._manager_update_workflow_run(
                canonical_run.run_id,
                facets=("needs_human",),
                gate_status="running",
                needs_human_reason=diagnostic_reason(
                    "copilot-finding-budget-exhausted",
                    f"Copilot review 修復輪次已達上限（{fix_rounds}/{max_fix_rounds}），"
                    "不再自動重跑",
                    source="work_actions._ship_action:copilot-budget",
                    run_id=canonical_run.run_id,
                    work_id=canonical_run.work_id,
                    head=preflight.head,
                    fix_rounds=str(fix_rounds),
                ),
            )
            return {
                "action": "needs_human",
                "reason": "copilot-finding-budget-exhausted",
                "next_actions": ["abandon", "review-attest"],
                "next_step_hint": (
                    f"cortex work review-attest {canonical_run.work_id} --repo {authority.repo} --actor <operator> --payload <file>"
                ),
                **_repair_budget_status(
                    fix_rounds=fix_rounds,
                    max_fix_rounds=max_fix_rounds,
                    current_phase=canonical_run.current_phase,
                ),
            }
    if (
        not ship
        or previous_head != preflight.head
        or ship.get("phase") not in {"review-requested", "merge-authorized"}
    ):
        if rearm_permit is not None:
            pre_request_gate = evaluate_delivery_gate(
                facts=remote,
                policy=DeliveryPolicy(
                    expected_head=preflight.head,
                    required_closing_issues=(),
                    review_kind="pre-review",
                ),
            )
            if not pre_request_gate.allowed:
                raise RuntimeError(
                    "copilot timeout rearm pre-request gate blocked: "
                    f"{', '.join(pre_request_gate.reasons)}"
                )
        adopted_review = _pick_adoptable_copilot_review(
            remote.copilot_reviews,
            head=preflight.head,
        )
        rearm_record = (
            _copilot_epoch_rearm_record(rearm_permit)
            if rearm_permit is not None
            else None
        )
        if adopted_review is not None:
            logger.info(
                "adopted existing Copilot review run_id=%s head=%s review_id=%s submitted_at=%s",
                canonical_run.run_id,
                preflight.head,
                adopted_review.review_id,
                adopted_review.submitted_at_epoch,
            )
            if rearm_permit is not None and ship is not None:
                _append_delivery_review_epoch(
                    active=active,
                    ship=ship,
                    permit=rearm_permit,
                    now_epoch=float(now_epoch),
                )
                active.pop("copilot_review_rearm_permit", None)
            new_ship = {
                "phase": "review-requested",
                "head": preflight.head,
                "tree_hash": preflight.tree_hash,
                "requested_at_epoch": float(adopted_review.submitted_at_epoch),
                "adopted_review_id": adopted_review.review_id,
                "adopted_at_epoch": float(now_epoch),
                "epoch_started_at": float(now_epoch),
                "fix_rounds": fix_rounds,
                "pr_number": pr_number,
                "change": change,
                "todo_paths": list(todo_paths_value),
            }
            if rearm_record is not None:
                new_ship["rearm"] = rearm_record
            active["ship"] = new_ship
            _save_runs(state_path, state)
            ship = active["ship"]
        else:
            if rearm_permit is None:
                github.request_copilot(repo=authority.repo, pr_number=pr_number)
                active["ship"] = {
                    "phase": "review-requested",
                    "head": preflight.head,
                    "tree_hash": preflight.tree_hash,
                    "requested_at_epoch": float(now_epoch),
                    "epoch_started_at": float(now_epoch),
                    "fix_rounds": fix_rounds,
                    "pr_number": pr_number,
                    "change": change,
                    "todo_paths": list(todo_paths_value),
                }
                _save_runs(state_path, state)
                return {"action": "awaiting-copilot", "head": preflight.head}
            assert ship is not None
            _append_delivery_review_epoch(
                active=active,
                ship=ship,
                permit=rearm_permit,
                now_epoch=float(now_epoch),
            )
            active["ship"] = {
                "phase": "review-requesting",
                "head": preflight.head,
                "tree_hash": preflight.tree_hash,
                "requested_at_epoch": float(now_epoch),
                "epoch_started_at": float(now_epoch),
                "fix_rounds": fix_rounds,
                "pr_number": pr_number,
                "change": change,
                "todo_paths": list(todo_paths_value),
                "rearm": rearm_record,
            }
            active.pop("copilot_review_rearm_permit", None)
            _save_runs(state_path, state)
            try:
                github.request_copilot(repo=authority.repo, pr_number=pr_number)
                post_request = github.fetch_merge_status(
                    repo=authority.repo,
                    pr_number=pr_number,
                )
            except Exception as exc:
                return _set_copilot_request_outcome_unknown(
                    active=active,
                    ship=active["ship"],
                    state=state,
                    state_path=state_path,
                    workflow_registry=workflow_registry,
                    canonical_run=canonical_run,
                    authority=authority,
                    detail=(
                        "明示 resume 建立的新 review-requesting epoch 已持久化，但 Copilot "
                        f"request outcome 無法確認：{safe_exception_summary(exc)}"
                    ),
                )
            if post_request.merged or post_request.pr_head != preflight.head:
                return _set_copilot_request_outcome_unknown(
                    active=active,
                    ship=active["ship"],
                    state=state,
                    state_path=state_path,
                    workflow_registry=workflow_registry,
                    canonical_run=canonical_run,
                    authority=authority,
                    detail=(
                        "Copilot review request 完成後，PR HEAD 已不再是明示 resume "
                        "核可的 exact candidate；request outcome 無法確認，為避免重送已 fail-closed。"
                    ),
                )
            active["ship"] = {**active["ship"], "phase": "review-requested"}
            _save_runs(state_path, state)
            ship = active["ship"]
            return {"action": "awaiting-copilot", "head": preflight.head}

    requested_at = ship.get("requested_at_epoch")
    if (
        not isinstance(requested_at, (int, float))
        or isinstance(requested_at, bool)
        or not math.isfinite(float(requested_at))
    ):
        raise ValueError("ship review request state malformed")
    adopted_at_epoch: float | None = None
    if "adopted_at_epoch" in ship:
        adopted_at = ship.get("adopted_at_epoch")
        if (
            not isinstance(adopted_at, (int, float))
            or isinstance(adopted_at, bool)
            or not math.isfinite(float(adopted_at))
        ):
            raise ValueError("ship review request state malformed")
        adopted_at_epoch = float(adopted_at)
    current_reviews = [
        review
        for review in remote.copilot_reviews
        if review.commit_id == preflight.head
        and review.author == COPILOT_REVIEWER_LOGIN
        and review.submitted_at_epoch >= float(requested_at)
    ]
    if not current_reviews:
        timeout_base = adopted_at_epoch if adopted_at_epoch is not None else float(requested_at)
        if float(now_epoch) - timeout_base > 15 * 60:
            active["ship"] = {**ship, "phase": "needs_human", "reason": "copilot-review-timeout"}
            workflow_registry._manager_update_workflow_run(
                canonical_run.run_id,
                facets=("needs_human",),
                gate_status="running",
                needs_human_reason=diagnostic_reason(
                    "copilot-review-timeout",
                    "已請求 Copilot review 超過 15 分鐘仍未收到對應 HEAD 的回覆",
                    source="work_actions._ship_action:copilot-timeout",
                    run_id=canonical_run.run_id,
                    work_id=canonical_run.work_id,
                    head=preflight.head,
                    requested_at=str(requested_at),
                ),
            )
            _save_runs(state_path, state)
            return {
                "action": "needs_human",
                "reason": "copilot-review-timeout",
                "next_actions": ["abandon", "review-attest"],
                "next_step_hint": (
                    f"cortex work review-attest {canonical_run.work_id} --repo {authority.repo} --actor <operator> --payload <file>"
                ),
            }
        return {"action": "awaiting-copilot", "head": preflight.head}
    review = max(current_reviews, key=lambda value: (value.submitted_at_epoch, value.review_id))
    loop = ReviewLoop(
        head=preflight.head,
        fix_rounds=fix_rounds,
        epoch_started_at=float(ship.get("epoch_started_at", requested_at)),
        requested_at=float(requested_at),
        max_fix_rounds=max_fix_rounds,
        adopted_at=adopted_at_epoch,
    )
    finding_count = sum(1 for thread in remote.review_threads if thread.blocks_merge)
    findings = [
        {"path": thread.path, "line": thread.line, "body": thread.body_excerpt}
        for thread in remote.review_threads
        if thread.blocks_merge
    ][:10]
    copilot = loop.record_review(
        head=review.commit_id,
        now_epoch=now_epoch,
        finding_count=finding_count,
        review_id=review.review_id,
        submitted_at_epoch=review.submitted_at_epoch,
        error=review.is_error,
    )
    if copilot.action == "fix_required":
        active["ship"] = {
            **ship,
            "phase": "needs-fix",
            "review_id": review.review_id,
            "finding_count": finding_count,
            "findings": findings,
            "fix_rounds": fix_rounds,
        }
        _save_runs(state_path, state)
        return {"action": "fix-required", "reason": copilot.reason, "findings": finding_count}
    if copilot.action != "passed":
        active["ship"] = {**ship, "phase": "needs_human", "reason": copilot.reason}
        _save_runs(state_path, state)
        workflow_registry._manager_update_workflow_run(
            canonical_run.run_id,
            facets=("needs_human",),
            gate_status="running",
            needs_human_reason=diagnostic_reason(
                str(copilot.reason),
                f"Copilot review loop 判定為需要人工介入：{copilot.reason}"
                f"（阻擋性 findings {finding_count} 條）",
                source="work_actions._ship_action:copilot-review-loop",
                run_id=canonical_run.run_id,
                work_id=canonical_run.work_id,
                head=preflight.head,
                fix_rounds=str(fix_rounds),
            ),
        )
        extra = (
            _repair_budget_status(
                fix_rounds=fix_rounds,
                max_fix_rounds=max_fix_rounds,
                current_phase=canonical_run.current_phase,
            )
            if copilot.reason == "copilot-finding-budget-exhausted"
            else {}
        )
        res = {"action": "needs_human", "reason": copilot.reason, **extra}
        if str(copilot.reason or "").startswith("copilot-"):
            res["next_actions"] = ["abandon", "review-attest"]
            res["next_step_hint"] = (
                f"cortex work review-attest {canonical_run.work_id} --repo {authority.repo} --actor <operator> --payload <file>"
            )
        return res

    if foreign_review is None:
        foreign_review = ForeignReviewEvidence(
            path=str(_absolute_file(args.get("foreign_review_path"), field="foreign_review_path")),
            expected_hash=args.get("foreign_review_hash"),
        )
    remote_gate = evaluate_delivery_gate(
        facts=remote,
        policy=DeliveryPolicy(
            expected_head=preflight.head,
            required_closing_issues=authority.mapped_issues,
            copilot_review_id=copilot.review_id,
            copilot_requested_at_epoch=copilot.loop.requested_at,
            copilot_adopted_at_epoch=copilot.loop.adopted_at,
        ),
    )
    if not remote_gate.allowed:
        raise RuntimeError(f"merge authorization blocked: {', '.join(remote_gate.reasons)}")
    authorization = _authorization_record(
        _merge_authorization_body(
            active=active,
            authority=authority,
            binding=binding,
            preflight=preflight,
            remote=remote,
            copilot=copilot,
            foreign_review=foreign_review,
        ),
        state_path=state_path,
    )
    existing_authorization = ship.get("merge_authorization") if ship else None
    if existing_authorization is not None and existing_authorization != authorization:
        raise RuntimeError("persisted merge authorization differs from current gate evidence")
    active["ship"] = {
        **ship,
        "phase": "merge-authorized",
        "head": preflight.head,
        "tree_hash": preflight.tree_hash,
        "review_id": copilot.review_id,
        "requested_at_epoch": copilot.loop.requested_at,
        "fix_rounds": fix_rounds,
        "pr_number": pr_number,
        "change": change,
        "todo_paths": list(todo_paths_value),
        "merge_authorization": authorization,
    }
    _save_runs(state_path, state)
    ship = active["ship"]
    try:
        merged = orchestrator.merge_if_ready(
            repo=authority.repo,
            pr_number=pr_number,
            change=change,
            expected_head=preflight.head,
            expected_tree_hash=preflight.tree_hash,
            authority=authority,
            preflight=preflight,
            copilot=copilot,
            foreign_review=foreign_review,
        )
    except RuntimeError:
        post_merge = github.fetch_merge_status(repo=authority.repo, pr_number=pr_number)
        if (
            not post_merge.merged
            or post_merge.pr_head != preflight.head
            or not _authorization_matches(
                authorization,
                active=active,
                authority=authority,
                binding=binding,
                preflight=preflight,
                remote=remote,
            )
        ):
            raise
        merged = SimpleNamespace(
            expected_head=preflight.head,
            expected_tree_hash=preflight.tree_hash,
        )
    else:
        post_merge = github.fetch_merge_status(repo=authority.repo, pr_number=pr_number)
        if not post_merge.merged or post_merge.pr_head != preflight.head:
            raise RuntimeError("merge side effect is not visible on exact PR HEAD")
    active["ship"] = {
        **ship,
        "phase": "merged",
        "head": merged.expected_head,
        "tree_hash": merged.expected_tree_hash,
        "fix_rounds": fix_rounds,
        "merge_commit": post_merge.merge_commit,
        "pr_number": pr_number,
        "change": change,
        "todo_paths": list(todo_paths_value),
        "merge_authorization": authorization,
    }
    _save_runs(state_path, state)
    return {"action": "merged-awaiting-closure", "head": merged.expected_head}


def execute_work_action(
    *,
    args: dict[str, Any],
    requested_by: str,
    runner: Runner = subprocess.run,
    now: Callable[[], float] = time.time,
    ship_executor: ShipExecutor | None = None,
    snapshot_path: str | Path | None = None,
    state_path: str | Path | None = None,
    workflow_registry=None,
    workflow_starter=None,
    readiness_checker=None,
) -> dict[str, Any]:
    action = args.get("action")
    repo = args.get("repo")
    work_id = args.get("work_id")
    if action not in {
        "link", "unlink", "start", "resume", "retry-build", "retry-card",
        "retry-verify", "retry-review", "recover-planning", "recover-pre-candidate",
        "recover-repair-commit", "regenerate-gates", "abandon", "retire-delivered",
        "close-delivered",
        "recover-superseded",
        "reset-reclaim-budget", "refreeze-base", "auto", "ship", "review-attest",
        "intake",
    }:
        raise ValueError("unsupported work action")
    repo = _repo_identity(repo)
    if not isinstance(work_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise ValueError("work action repo/work_id invalid")
    if action in {"link", "unlink"}:
        return {
            "work_id": work_id,
            "repo": repo,
            "requested_by": requested_by,
            "result": _mutate_override(args=args, repo=repo, work_id=work_id),
        }
    authority = load_work_authority(
        repo=repo,
        work_id=work_id,
        snapshot_path=snapshot_path,
        # #370 follow-up: the retirement family (abandon / retire-delivered)
        # tears down a *local* stuck run and never depends on an issue's live
        # open/closed state, so a canonical GitHub provider that is merely
        # rate-limited (degraded with a rate-limit diagnostic, but still
        # carrying a prior last-known-good revision/last_success_at) must not
        # block cleanup — the very moment the system is throttled is when
        # stuck runs most need clearing. Claim/start and every other action
        # keep the strict fail-closed default: they need fresh authority.
        allow_rate_limited_last_known_good=action in _LOCAL_UNBLOCK_ACTIONS,
    )
    now_epoch = now()
    resolved_state_path = Path(state_path) if state_path is not None else _run_state_path()
    if workflow_registry is None:
        from .registry import JobRegistry

        workflow_registry = JobRegistry(state_path=resolved_state_path.parent / "jobs.json")
    if workflow_starter is None:
        workflow_starter = _fallback_workflow_starter(
            workflow_registry, resolved_state_path
        )
    if action in {"start", "resume"}:
        result = _claim_action(
            args=args,
            authority=authority,
            requested_by=requested_by,
            now_epoch=now_epoch,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
            workflow_starter=workflow_starter,
            readiness_checker=readiness_checker,
        )
    elif action == "intake":
        result = _intake_action(
            args=args,
            authority=authority,
            requested_by=requested_by,
            now_epoch=now_epoch,
            state_path=resolved_state_path,
            snapshot_path=snapshot_path,
            workflow_registry=workflow_registry,
            workflow_starter=workflow_starter,
            readiness_checker=readiness_checker,
        )
    elif action == "retry-build":
        result = _retry_build_action(
            args=args,
            authority=authority,
            workflow_registry=workflow_registry,
            state_path=resolved_state_path,
            now_epoch=now_epoch,
        )
    elif action == "retry-card":
        result = _retry_card_action(
            args=args,
            authority=authority,
            workflow_registry=workflow_registry,
            # #752 補遺：帶 reason 時要寫 evidence，必須用**解析後**的 state path
            # ——原始參數在 production（daemon 請求）恆為 None。
            state_path=resolved_state_path,
            now_epoch=now_epoch,
        )
    elif action == "retry-verify":
        result = _retry_verify_action(
            args=args,
            authority=authority,
            workflow_registry=workflow_registry,
        )
    elif action == "retry-review":
        result = _retry_review_action(
            args=args,
            authority=authority,
            workflow_registry=workflow_registry,
            state_path=resolved_state_path,
            now_epoch=now_epoch,
        )
    elif action == "recover-planning":
        result = _recover_planning_action(
            args=args,
            authority=authority,
            requested_by=requested_by,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "recover-pre-candidate":
        result = _recover_pre_candidate_action(
            args=args,
            authority=authority,
            requested_by=requested_by,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "regenerate-gates":
        result = _regenerate_gates_action(
            args=args,
            authority=authority,
            workflow_registry=workflow_registry,
        )
    elif action == "recover-repair-commit":
        result = _recover_repair_commit_action(
            args=args,
            authority=authority,
            requested_by=requested_by,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "abandon":
        result = _abandon_action(
            args=args,
            authority=authority,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "recover-superseded":
        result = _recover_superseded_action(
            args=args,
            authority=authority,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "retire-delivered":
        result = _retire_delivered_action(
            args=args,
            authority=authority,
            runner=runner,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "close-delivered":
        result = _close_delivered_action(
            args=args,
            authority=authority,
            runner=runner,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "reset-reclaim-budget":
        result = _reset_reclaim_budget_action(
            args=args,
            authority=authority,
            now_epoch=now_epoch,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "refreeze-base":
        result = _refreeze_base_action(
            args=args,
            authority=authority,
            now_epoch=now_epoch,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "review-attest":
        result = _review_attest_action(
            args=args,
            requested_by=requested_by,
            authority=authority,
            runner=runner,
            now_epoch=now_epoch,
            state_path=resolved_state_path,
            workflow_registry=workflow_registry,
        )
    elif action == "auto":
        enabled = args.get("enabled")
        issue = args.get("issue")
        if not isinstance(enabled, bool):
            raise ValueError("auto requires strict boolean enabled")
        if issue is None:
            if not authority.mapped_issues:
                raise ValueError("auto requires at least one authorized issue")
            issues = authority.mapped_issues
        else:
            if issue not in authority.mapped_issues:
                raise ValueError("auto issue is not authorized")
            issues = (issue,)
        for target_issue in issues:
            argv = build_label_argv(
                repo=authority.repo,
                issue=target_issue,
                enabled=enabled,
            )
            completed = runner(argv, shell=False, capture_output=True, text=True)
            if getattr(completed, "returncode", None) != 0:
                raise RuntimeError("GitHub auto-label mutation failed")
        result = (
            {"action": "auto", "enabled": enabled, "issues": list(issues)}
            if issue is None
            else {"action": "auto", "enabled": enabled, "issue": issue}
        )
    else:
        result = (
            ship_executor(dict(args), authority)
            if ship_executor is not None
            else _ship_action(
                args=args,
                authority=authority,
                runner=runner,
                now=now,
                state_path=resolved_state_path,
                workflow_registry=workflow_registry,
            )
        )
    return {
        "work_id": authority.work_id,
        "repo": authority.repo,
        "requested_by": requested_by,
        "provider_revision": authority.github_provider_revision,
        "result": result,
    }
