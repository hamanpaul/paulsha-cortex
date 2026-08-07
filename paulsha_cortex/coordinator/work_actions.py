"""Manager-owned work lifecycle mutations reached only through the control queue."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from enum import Enum
from pathlib import Path
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

from paulsha_cortex.config import paths
from paulsha_cortex._yaml import safe_load

from .claim import (
    ClaimCandidate,
    build_claim_key,
    build_label_argv,
    claim_identity_digest,
    decide_auto_claim,
    decide_manual_start,
    load_work_authorities,
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
    repair_budget_for_band,
    validate_archive_gate,
    validate_pr_metadata,
    _validate_foreign_review,
)
from .github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    DeliveryPolicy,
    GitHubDeliveryClient,
    evaluate_delivery_gate,
)
from . import engineering_outcome
from . import verification
from .preflight import PreflightRequest, load_preflight_command, run_preflight
from .work_bridge import current_sizing_snapshot, resolve_trusted_repo_root, workflow_status
from .workflow import GateEvidenceRef


Runner = Callable[..., object]
ShipExecutor = Callable[[dict[str, Any], object], dict[str, Any]]


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
        ["openspec", "validate", change, "--strict"],
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
    fragments = tuple(
        repo_root / directory / f"{change}.md"
        for directory in ("changelog.d", "changes")
    )
    fragment_present = any(
        path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8").strip()
        for path in fragments
    )
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


def _load_runs(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": "cortex-delivery-journal/v1", "runs": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("work run state unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "cortex-delivery-journal/v1"
        or not isinstance(payload.get("runs"), dict)
    ):
        raise ValueError("work run state malformed")
    for key, row in payload["runs"].items():
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
    return payload


def _save_runs(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
        and run.openspec_refs == authority.mapped_openspec
        and run.pr_refs
        == tuple(f"{authority.repo}#{number}" for number in authority.mapped_prs)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "delivery WorkflowRun does not match current WorkAuthority"
        )
    return matches[0]


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
    if not isinstance(change, str) or change not in authority.mapped_openspec:
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
        return {
            "schema": "cortex-merge-authorization/v2",
            **common,
            "review_kind": "maintainer-review",
            "review_ref": maintainer_review.path,
            "review_hash": maintainer_review.expected_hash.lower(),
        }
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
    if (
        not isinstance(run_id, str)
        or re.fullmatch(r"workflow-[0-9a-f]{20}", run_id) is None
        or not isinstance(head, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", head) is None
    ):
        raise ValueError("merge authorization identity malformed")
    root = state_path.resolve().parent / "evidence" / "merge-authorization"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{run_id}-{head.lower()}.json"
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
    schema = body.get("schema") if isinstance(body, dict) else None
    review_required = (
        {"copilot_requested_at_epoch", "copilot_review_id", "copilot_hash"}
        if schema == "cortex-merge-authorization/v1"
        else {"review_kind", "review_ref", "review_hash"}
        if schema == "cortex-merge-authorization/v2"
        else set()
    )
    if (
        not isinstance(body, dict)
        or not review_required
        or set(body) != common_required | review_required
        or verification.canonical_json_hash(body) != digest
        or not isinstance(evidence_path, str)
        or not Path(evidence_path).is_absolute()
        or Path(evidence_path).is_symlink()
        or not Path(evidence_path).is_file()
        or Path(evidence_path).stat().st_mode & 0o222
    ):
        return False
    try:
        evidence_wrapper = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if evidence_wrapper != {"payload": body, "hash": digest}:
        return False
    common_valid = (
        schema in {"cortex-merge-authorization/v1", "cortex-merge-authorization/v2"}
        and body.get("run_id") == active.get("run_id")
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
    pr_number: int,
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
    if (
        run.current_phase != "review"
        or run.candidate_head != candidate
        or run.verified_head != candidate
        or len(bound_refs) != 1
        or bound_refs[0].ref != str(evidence_path)
        or bound_refs[0].sha256 != expected_hash
        or not isinstance(body, dict)
        or body.get("schema") != "cortex-maintainer-review/v1"
        or body.get("repo") != authority.repo
        or body.get("work_id") != authority.work_id
        or body.get("run_id") != run.run_id
        or body.get("authority_digest") != work_authority_digest(authority)
        or body.get("pr_number") != pr_number
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
    allowed = {"action", "repo", "work_id", "actor", "verdict", "summary", "findings"}
    extras = set(args) - allowed
    if extras:
        raise ValueError(f"review-attest rejects caller evidence/input: {sorted(extras)[0]}")
    actor = args.get("actor")
    summary = args.get("summary")
    findings = args.get("findings")
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
    foreign = [ref for ref in run.gate_refs if ref.kind == "foreign-review"]
    if (
        run.current_phase != "review"
        or run.status != "ongoing"
        or not isinstance(run.candidate_head, str)
        or run.verified_head != run.candidate_head
        or len(foreign) != 1
        or len(authority.mapped_prs) != 1
        or run.pr_refs != (f"{run.repo}#{authority.mapped_prs[0]}",)
    ):
        raise RuntimeError("review-attest requires current exact-HEAD review run")
    pr_number = authority.mapped_prs[0]
    remote = GitHubDeliveryClient(runner=runner).fetch_delivery_facts(
        repo=authority.repo,
        pr_number=pr_number,
        change=authority.mapped_openspec[0],
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
    authorization = _authorization_record(
        _merge_authorization_body(
            active=active,
            authority=authority,
            binding=binding,
            preflight=preflight,
            remote=remote,
            copilot=None,
            foreign_review=foreign_review,
            maintainer_review=maintainer,
        ),
        state_path=state_path,
    )
    existing_authorization = ship.get("merge_authorization") if ship else None
    if existing_authorization is not None and existing_authorization != authorization:
        raise RuntimeError("persisted merge authorization differs from current gate evidence")
    active["ship"] = {
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
        )

    return start


def _claim_action(
    *,
    args: dict[str, Any],
    authority,
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

    canonical_run = None
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
                and run.openspec_refs == authority.mapped_openspec
            ]
            if len(active) > 1:
                raise RuntimeError("active workflow identity is ambiguous")
            canonical_run = active[0] if active else None
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
        and (automatic or args.get("action") == "resume")
    ):
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
    if decision.action == "claim":
        if workflow_starter is None:
            raise RuntimeError("canonical workflow starter unavailable")
        # #218 AC2（design #208 E）：語意 re-claim 的世代熔斷——同一 (repo, work_id)
        # 已累積 SEMANTIC_RECLAIM_LIMIT 個 superseded 世代（v1..v3）時，不得自動
        # 建立下一版 run（v4），強制 needs_human。計數以 registry 的 run 歷史為準
        # （跨 run_id，不受 active dict 換代歸零影響）。
        if workflow_registry is not None:
            superseded_generations = [
                run
                for run in workflow_registry.list_workflow_runs()
                if run.repo == authority.repo
                and run.work_id == authority.work_id
                and run.status == "superseded"
            ]
            if len(superseded_generations) >= SEMANTIC_RECLAIM_LIMIT:
                return {
                    "action": "needs_human",
                    "reason": "semantic-reclaim-budget-exhausted",
                    "run": None,
                    "superseded_generations": len(superseded_generations),
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
        if args.get("action") == "resume" and decision.action in {
            "resume",
            "needs_human",
            "blocked",
        }:
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
    response: dict[str, Any] = {
        "action": decision.action,
        "reason": decision.reason,
        "run": active,
    }
    # #256 R2：operator／agent 不必翻 registry 就知道下一步——狀態帶得出合法
    # 動作集合與具體 blocking reason 時一併回報。
    if decision.next_actions:
        response["next_actions"] = list(decision.next_actions)
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


def _retry_build_action(*, args: dict[str, Any], authority, workflow_registry) -> dict[str, Any]:
    """Reopen the final builder card with exact-Candidate CAS after a human stop."""

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_candidate",
    }
    if extras:
        raise ValueError(f"retry-build rejects caller evidence/input: {sorted(extras)[0]}")
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
        and run.openspec_refs == authority.mapped_openspec
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
    retry_classification = _classify_retry(run, workflow_registry)
    archive_applied = any(
        step.phase == "ship"
        and step.card == "openspec-archive"
        and step.gate_result == "passed"
        for step in run.steps
    )
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
            "closure, or done. Commit or adopt a tested descendant Candidate."
        )
    else:
        repair_action = (
            "Repair the exact Candidate after a delivery preflight failure. Inspect any existing "
            "worktree repair commits, run the authoritative preflight, fix only real Candidate "
            "failures, and make active OpenSpec tasks describe and complete only pre-archive work. "
            "Do not claim archive, merge, issue closure, or done before Manager performs those "
            "actions. Commit or adopt a tested descendant Candidate."
        )
    updated = workflow_registry._manager_reset_workflow_for_retry_build(
        run.run_id,
        expected_candidate=expected_candidate.lower(),
        repair_action=repair_action,
        retry_classification=retry_classification.value,
    )
    updated = _recompute_and_persist_sizing(workflow_registry, updated)
    return {
        "action": "retry-build",
        "reason": "candidate-repair-dispatched",
        "expected_candidate": expected_candidate.lower(),
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
        and run.openspec_refs == authority.mapped_openspec
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


def _retry_review_action(*, args: dict[str, Any], authority, workflow_registry) -> dict[str, Any]:
    """Relaunch foreign review only for the exact verified Candidate（#216 AC3）。

    build／verify phase 完全不動：不重跑 builder、不重建 candidate。若 run 缺少
    冷凍 plan authority（尚未經 plan review freeze），在任何狀態變更前直接
    fail-closed（pre-dispatch fail），不派任何 review job。
    """

    extras = set(args) - {
        "action", "repo", "work_id", "issue", "actor", "expected_candidate",
    }
    if extras:
        raise ValueError(f"retry-review rejects caller evidence/input: {sorted(extras)[0]}")
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
        and run.openspec_refs == authority.mapped_openspec
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
    # 重跑 review 本身即是 review 交接修復：candidate 未變，不是 model repair。
    retry_classification = _classify_retry(
        run, workflow_registry, trigger="review-handoff-failure"
    )
    updated = workflow_registry._manager_reset_workflow_for_retry_review(
        run.run_id,
        expected_candidate=expected_candidate.lower(),
        retry_classification=retry_classification.value,
    )
    updated = _recompute_and_persist_sizing(workflow_registry, updated)
    return {
        "action": "retry-review",
        "reason": "foreign-review-rerun-dispatched",
        "expected_candidate": expected_candidate.lower(),
        "run": updated.to_dict(),
        "retry_classification": retry_classification,
    }


def _validate_abandon_evidence_target(target: Path, content: bytes) -> None:
    try:
        metadata = target.stat()
        conflict = (
            target.is_symlink()
            or not target.is_file()
            or metadata.st_size != len(content)
            or metadata.st_size > 4096
            or metadata.st_mode & 0o222
            or target.read_bytes() != content
        )
    except OSError as error:
        raise RuntimeError("workflow abandon evidence conflict") from error
    if conflict:
        raise RuntimeError("workflow abandon evidence conflict")


def _abandon_record(body: dict[str, Any], *, state_path: Path) -> dict[str, str]:
    digest = verification.canonical_json_hash(body)
    root = state_path.resolve().parent / "evidence" / "work-abandon"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{body['run_id']}-{digest}.json"
    content = (
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if target.exists():
        _validate_abandon_evidence_target(target, content)
    else:
        temporary = root / f".{target.name}.{uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError(
                "workflow abandon evidence temporary collision"
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
                _validate_abandon_evidence_target(target, content)
            else:
                directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ref": str(target), "hash": digest}


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
    if run.issue_refs != expected_issues or run.openspec_refs != authority.mapped_openspec:
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
    return {
        "action": "abandoned",
        "reason": reason,
        "actor": actor,
        "expected_run_id": expected_run_id,
        "evidence": record,
        "run": updated.to_dict(),
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
    record = _recover_planning_record(
        run,
        state_path=state_path,
        actor=actor_value,
        failure_classification=failure_classification,
        failure_reason=failure_reason,
        evidence_ref=failure["evidence_ref"],
        recovered_phase="plan",
    )
    current_facets = tuple(
        facet for facet in run.facets if facet not in {"needs_human", "blocked"}
    )
    evidence_refs = run.evidence_refs
    if record["ref"] not in evidence_refs:
        evidence_refs = (*evidence_refs, record["ref"])
    updated = workflow_registry._manager_update_workflow_run(
        run.run_id,
        current_phase="plan",
        facets=current_facets,
        gate_status="running",
        evidence_refs=evidence_refs,
    )
    return {
        "action": "recovered",
        "reason": "planning-recovery-dispatched",
        "expected_run_id": expected_run_id,
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
    if not wt_path and isinstance(target_slice.get("branch"), str):
        slug = target_slice["branch"].replace("/", "-")
        wt_path = str(Path(paths.worktree_root()) / slug)

    if wt_path and isinstance(wt_path, (str, Path)):
        target_wt = Path(wt_path)
        if target_wt.exists() or target_wt.is_symlink():
            try:
                subprocess.run(["git", "worktree", "remove", "--force", str(target_wt)], check=False)
            except Exception:
                pass
            if target_wt.exists() or target_wt.is_symlink():
                import shutil
                shutil.rmtree(target_wt, ignore_errors=True)

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
    return {
        "action": "recover-pre-candidate",
        "reason": "pre-candidate-slice-reset",
        "slice_id": slice_id,
        "slice_state": updated.get("state"),
        "gate_state": updated.get("gate_state"),
    }


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
    if run.issue_refs != expected_issues or run.openspec_refs != authority.mapped_openspec:
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
    head_result = verification._run_git(["-C", worktree, "rev-parse", "HEAD"], None)
    head_value = str(head_result.get("stdout", "")).strip().lower()
    if (
        head_result.get("status") != "ok"
        or verification.SAFE_SHA_RE.fullmatch(head_value) is None
    ):
        raise RuntimeError("recover-repair-commit could not read worktree HEAD")
    if head_value != expected_candidate:
        raise RuntimeError(
            "recover-repair-commit expected_candidate CAS mismatch against worktree HEAD"
        )
    status_result = verification._run_git(
        ["-C", worktree, "status", "--porcelain", "--untracked-files=all"], None
    )
    if status_result.get("status") != "ok":
        raise RuntimeError("recover-repair-commit could not read worktree status")
    if str(status_result.get("stdout", "")).strip():
        raise RuntimeError("recover-repair-commit requires a clean worktree")
    ancestry_result = verification._run_git(
        ["-C", worktree, "merge-base", "--is-ancestor", original_candidate, expected_candidate],
        None,
    )
    if ancestry_result.get("status") == "non-zero" and ancestry_result.get("returncode") == 1:
        raise RuntimeError("recover-repair-commit requires a descendant candidate")
    if ancestry_result.get("status") != "ok":
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


def run_auto_claim_scan(
    *,
    snapshot_path: str | Path | None = None,
    state_path: str | Path | None = None,
    now: Callable[[], float] = time.time,
    runner: Runner = subprocess.run,
    workflow_registry=None,
    workflow_starter=None,
    readiness_checker=None,
) -> list[dict[str, Any]]:
    """Project the durable Monitor snapshot into Manager-owned auto claims."""

    try:
        authorities = load_work_authorities(snapshot_path=snapshot_path)
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
    for authority in authorities:
        if not authority.confirmed_todo:
            continue
        live_auto_label = False
        if authority.mapped_issues:
            issue_reads_failed = False
            for issue in authority.mapped_issues:
                completed = runner(
                    ["gh", "api", f"repos/{authority.repo}/issues/{issue}"],
                    shell=False,
                    capture_output=True,
                    text=True,
                )
                if getattr(completed, "returncode", None) != 0:
                    results.append(
                        {
                            "repo": authority.repo,
                            "work_id": authority.work_id,
                            "action": "blocked",
                            "reason": "github-label-read-failed",
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
                live_auto_label = live_auto_label or "cortex:auto-on-going" in names
            if issue_reads_failed:
                continue
        try:
            result = _claim_action(
                args={"action": "auto-scan"},
                authority=authority,
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
        or len(authority.mapped_openspec) != 1
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
        # mutation.  Once operator-owned correlation resolves to the one
        # PR/OpenSpec/Todo tuple required by v1, an explicit resume may safely
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
    protected_refs = [f"openspec/changes/{change}", *todo_paths_value]
    if any(_path_has_symlink(repo_root, ref) for ref in protected_refs):
        raise ValueError("ship authorized repo path must not traverse a symlink")
    metadata = _pr_metadata(args, required_issues=authority.mapped_issues)
    persisted_binding = active.get("delivery_binding")
    if persisted_binding is None:
        active["delivery_binding"] = binding
        _save_runs(state_path, state)
    elif persisted_binding != binding:
        raise RuntimeError("ship delivery binding differs from persisted PR/OpenSpec/Todo refs")
    github = GitHubDeliveryClient(runner=runner)
    orchestrator = ShipOrchestrator(github=github, now=now)

    maintainer_recovery = _recoverable_maintainer_ship_stop(ship=ship, args=args)
    if ship and ship.get("phase") == "needs_human" and not maintainer_recovery:
        return {"action": "needs_human", "reason": ship.get("reason")}

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
                canonical_run.run_id, facets=("needs_human",), gate_status="running"
            )
            return {"action": "merged-awaiting-closure", "head": expected_head}

    active_change = repo_root / "openspec" / "changes" / change
    if active_change.is_dir():
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
        raise RuntimeError("ship HEAD differs from authenticated GitHub PR")
    if not remote.active_openspec_absent or not remote.archive_present:
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
                canonical_run.run_id, facets=("needs_human",), gate_status="running"
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
    if ship and ship.get("phase") == "needs-fix" and previous_head == preflight.head:
        return {"action": "fix-required", "head": preflight.head, "fix_rounds": fix_rounds}
    if previous_head is not None and previous_head != preflight.head:
        fix_rounds += 1
        active["repair_rounds"] = fix_rounds
        if fix_rounds > max_fix_rounds:
            active["ship"] = {
                **ship,
                "phase": "needs_human",
                "reason": "copilot-finding-budget-exhausted",
                "head": preflight.head,
                "fix_rounds": fix_rounds,
            }
            _save_runs(state_path, state)
            workflow_registry._manager_update_workflow_run(
                canonical_run.run_id, facets=("needs_human",), gate_status="running"
            )
            return {
                "action": "needs_human",
                "reason": "copilot-finding-budget-exhausted",
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

    requested_at = ship.get("requested_at_epoch")
    if (
        not isinstance(requested_at, (int, float))
        or isinstance(requested_at, bool)
        or not math.isfinite(float(requested_at))
    ):
        raise ValueError("ship review request state malformed")
    current_reviews = [
        review
        for review in remote.copilot_reviews
        if review.commit_id == preflight.head
        and review.author == COPILOT_REVIEWER_LOGIN
        and review.submitted_at_epoch >= float(requested_at)
    ]
    if not current_reviews:
        if float(now_epoch) - float(requested_at) > 15 * 60:
            active["ship"] = {**ship, "phase": "needs_human", "reason": "copilot-review-timeout"}
            workflow_registry._manager_update_workflow_run(
                canonical_run.run_id, facets=("needs_human",), gate_status="running"
            )
            _save_runs(state_path, state)
            return {"action": "needs_human", "reason": "copilot-review-timeout"}
        return {"action": "awaiting-copilot", "head": preflight.head}
    review = max(current_reviews, key=lambda value: (value.submitted_at_epoch, value.review_id))
    loop = ReviewLoop(
        head=preflight.head,
        fix_rounds=fix_rounds,
        epoch_started_at=float(ship.get("epoch_started_at", requested_at)),
        requested_at=float(requested_at),
        max_fix_rounds=max_fix_rounds,
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
            canonical_run.run_id, facets=("needs_human",), gate_status="running"
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
        return {"action": "needs_human", "reason": copilot.reason, **extra}

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
        "link", "unlink", "start", "resume", "retry-build", "retry-verify",
        "retry-review", "recover-planning", "recover-pre-candidate",
        "recover-repair-commit", "abandon", "auto", "ship", "review-attest",
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
