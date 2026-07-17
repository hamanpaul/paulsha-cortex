"""Manager-owned work lifecycle mutations reached only through the control queue."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from paulsha_cortex.config import paths
from paulsha_cortex._yaml import safe_load

from .claim import (
    ClaimCandidate,
    build_label_argv,
    decide_manual_start,
    load_work_authority,
)
from .delivery import (
    ForeignReviewEvidence,
    ReviewLoop,
    ShipOrchestrator,
    build_openspec_archive_argv,
)
from .github_delivery import COPILOT_REVIEWER_LOGIN, GitHubDeliveryClient
from .preflight import PreflightRequest, load_preflight_command, run_preflight


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


def _override_path(args: dict[str, Any]) -> Path:
    repo_root = args.get("repo_root")
    if not isinstance(repo_root, str) or not Path(repo_root).is_absolute():
        raise ValueError("link/unlink require absolute repo_root")
    return Path(repo_root).resolve() / ".cortex" / "work-items.yaml"


def _write_override(path: Path, payload: dict[str, Any]) -> None:
    lines = ["version: 1", "work_items:"]
    for work_id, row in sorted(payload["work_items"].items()):
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
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _mutate_override(*, args: dict[str, Any], repo: str, work_id: str) -> dict[str, Any]:
    if re.fullmatch(r"[a-z0-9][a-z0-9-]*", work_id) is None:
        raise ValueError("work_id invalid")
    issue = args.get("issue")
    if not isinstance(issue, int) or isinstance(issue, bool) or issue <= 0:
        raise ValueError("link/unlink require a positive issue")
    path = _override_path(args)
    if path.exists():
        payload = safe_load(path.read_text(encoding="utf-8"))
    else:
        payload = {"version": 1, "work_items": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("work_items"), dict)
    ):
        raise ValueError("work override malformed")
    row = payload["work_items"].setdefault(
        work_id,
        {"title": args.get("title") or work_id, "links": [], "excludes": []},
    )
    if not isinstance(row, dict):
        raise ValueError("work override row malformed")
    row.setdefault("title", work_id)
    row.setdefault("links", [])
    row.setdefault("excludes", [])
    ref = {"kind": "github_issue", "ref": f"{repo}#{issue}"}
    if args["action"] == "link":
        if ref not in row["links"]:
            row["links"].append(ref)
        row["excludes"] = [value for value in row["excludes"] if value != ref]
    else:
        row["links"] = [value for value in row["links"] if value != ref]
        if ref not in row["excludes"]:
            row["excludes"].append(ref)
    _write_override(path, payload)
    return {"action": args["action"], "override_path": str(path), "source": ref}


def _run_state_path() -> Path:
    return paths.coordinator_root() / "work-runs.json"


def _load_runs(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": "cortex-work-runs/v1", "runs": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("work run state unreadable") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "cortex-work-runs/v1"
        or not isinstance(payload.get("runs"), dict)
    ):
        raise ValueError("work run state malformed")
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
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_active_run(*, state_path: Path, repo: str, work_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _load_runs(state_path)
    active = state["runs"].get(f"{repo}/{work_id}")
    if not isinstance(active, dict):
        raise RuntimeError("ship requires an active workflow")
    return state, active


def _claim_action(
    *,
    args: dict[str, Any],
    authority,
    now_epoch: float,
    state_path: Path,
) -> dict[str, Any]:
    state = _load_runs(state_path)
    key = f"{authority.repo}/{authority.work_id}"
    active = state["runs"].get(key)
    if active is not None and not isinstance(active, dict):
        raise ValueError("active work run malformed")
    candidate = ClaimCandidate(
        authority=authority,
        repo=authority.repo,
        work_id=authority.work_id,
        source_revisions=authority.source_revisions,
        confirmed_todo=authority.confirmed_todo,
        confirmed_issue=(
            args.get("issue") if args.get("issue") is not None else authority.mapped_issues[0]
        ),
        auto_label=False,
        active_run_id=active.get("run_id") if active else None,
        active_claim_key=active.get("claim_key") if active else None,
    )
    decision = decide_manual_start(candidate, now_epoch=now_epoch)
    if args["action"] == "resume" and active is None:
        raise ValueError("resume requires an active workflow")
    if decision.action == "claim":
        active = {
            "run_id": f"run-{uuid4().hex}",
            "claim_key": decision.claim_key,
            "repo": authority.repo,
            "work_id": authority.work_id,
            "source_revisions": list(authority.source_revisions),
            "snapshot_hash": authority.snapshot_hash,
        }
        state["runs"][key] = active
        _save_runs(state_path, state)
    return {"action": decision.action, "reason": decision.reason, "run": active}


def _ship_action(
    *,
    args: dict[str, Any],
    authority,
    runner: Runner,
    now: Callable[[], float],
    state_path: Path,
) -> dict[str, Any]:
    """Advance one fail-closed delivery stage for the exact durable work item.

    The operation is intentionally resumable: requesting Copilot review, merging,
    and proving remote closure are distinct durable transitions.  A daemon crash
    therefore cannot silently repeat a merge or manufacture terminal evidence.
    """

    repo_root_value = args.get("repo_root")
    if not isinstance(repo_root_value, str) or not Path(repo_root_value).is_absolute():
        raise ValueError("ship requires absolute repo_root")
    repo_root = Path(repo_root_value).resolve()
    pr_number = _positive_int(args.get("pr_number"), field="pr_number")
    change = args.get("change")
    if not isinstance(change, str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", change) is None:
        raise ValueError("ship change must be a safe slug")
    skip_tests = args.get("skip_tests", False)
    if not isinstance(skip_tests, bool):
        raise ValueError("ship skip_tests must be a strict boolean")

    state, active = _load_active_run(
        state_path=state_path,
        repo=authority.repo,
        work_id=authority.work_id,
    )
    ship = active.get("ship")
    if ship is not None and not isinstance(ship, dict):
        raise ValueError("ship state malformed")
    github = GitHubDeliveryClient(runner=runner)
    orchestrator = ShipOrchestrator(github=github, now=now)

    if ship and ship.get("phase") == "merged":
        expected_head = ship.get("head")
        if not isinstance(expected_head, str):
            raise ValueError("ship merged state malformed")
        todo_paths_value = args.get("todo_paths")
        if not isinstance(todo_paths_value, list) or any(
            not isinstance(path, str) or not path for path in todo_paths_value
        ):
            raise ValueError("ship closure requires todo_paths")
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
        )
        active["ship"] = {
            **ship,
            "phase": "done",
            "completion_record": dict(closure.completion_record),
        }
        _save_runs(state_path, state)
        return {
            "action": "done",
            "head": expected_head,
            "merge_commit": closure.facts.merge_commit,
            "completion_record": dict(closure.completion_record),
        }
    if ship and ship.get("phase") == "done":
        return {"action": "done", **ship}

    active_change = repo_root / "openspec" / "changes" / change
    if active_change.is_dir():
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

    now_epoch = now()
    if (
        not isinstance(now_epoch, (int, float))
        or isinstance(now_epoch, bool)
        or not math.isfinite(float(now_epoch))
    ):
        raise ValueError("ship clock must be finite")
    if not ship or ship.get("head") != preflight.head or ship.get("phase") != "review-requested":
        github.request_copilot(repo=authority.repo, pr_number=pr_number)
        active["ship"] = {
            "phase": "review-requested",
            "head": preflight.head,
            "tree_hash": preflight.tree_hash,
            "requested_at_epoch": float(now_epoch),
            "pr_number": pr_number,
            "change": change,
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
            return {"action": "needs_human", "reason": "copilot-review-timeout"}
        return {"action": "awaiting-copilot", "head": preflight.head}
    review = max(current_reviews, key=lambda value: (value.submitted_at_epoch, value.review_id))
    loop = ReviewLoop(
        head=preflight.head,
        fix_rounds=0,
        epoch_started_at=float(requested_at),
        requested_at=float(requested_at),
    )
    copilot = loop.record_review(
        head=review.commit_id,
        now_epoch=now_epoch,
        finding_count=0,
        review_id=review.review_id,
        submitted_at_epoch=review.submitted_at_epoch,
        error=review.is_error,
    )
    if copilot.action != "passed":
        return {"action": "needs_human", "reason": copilot.reason}

    foreign_review = ForeignReviewEvidence(
        path=str(_absolute_file(args.get("foreign_review_path"), field="foreign_review_path")),
        expected_hash=args.get("foreign_review_hash"),
    )
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
    active["ship"] = {
        **ship,
        "phase": "merged",
        "head": merged.expected_head,
        "tree_hash": merged.expected_tree_hash,
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
) -> dict[str, Any]:
    action = args.get("action")
    repo = args.get("repo")
    work_id = args.get("work_id")
    if action not in {"link", "unlink", "start", "resume", "auto", "ship"}:
        raise ValueError("unsupported work action")
    if not isinstance(repo, str) or not isinstance(work_id, str):
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
    if action in {"start", "resume"}:
        result = _claim_action(
            args=args,
            authority=authority,
            now_epoch=now_epoch,
            state_path=resolved_state_path,
        )
    elif action == "auto":
        enabled = args.get("enabled")
        issue = args.get("issue")
        if not isinstance(enabled, bool):
            raise ValueError("auto requires strict boolean enabled")
        if issue not in authority.mapped_issues:
            raise ValueError("auto issue is not authorized")
        argv = build_label_argv(repo=authority.repo, issue=issue, enabled=enabled)
        completed = runner(argv, shell=False, capture_output=True, text=True)
        if getattr(completed, "returncode", None) != 0:
            raise RuntimeError("GitHub auto-label mutation failed")
        result = {"action": "auto", "enabled": enabled, "issue": issue}
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
            )
        )
    return {
        "work_id": authority.work_id,
        "repo": authority.repo,
        "requested_by": requested_by,
        "provider_revision": authority.github_provider_revision,
        "result": result,
    }
