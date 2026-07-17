"""Manager-owned work lifecycle mutations reached only through the control queue."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
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
    decide_auto_claim,
    decide_manual_start,
    load_work_authorities,
    load_work_authority,
)
from .delivery import (
    ArchiveGateFacts,
    ForeignReviewEvidence,
    PullRequestMetadata,
    ReviewLoop,
    ShipOrchestrator,
    build_openspec_archive_argv,
    validate_archive_gate,
    validate_pr_metadata,
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
    facts = ArchiveGateFacts(
        tasks_complete=bool(task_states) and all(state.lower() == "x" for state in task_states),
        canonical_specs_valid=getattr(canonical, "returncode", None) == 0,
        doc_references_valid=getattr(policy, "returncode", None) == 0,
        changelog_present="## [Unreleased]" in changelog and "- **" in changelog,
    )
    gate = validate_archive_gate(facts)
    if not gate.allowed:
        raise RuntimeError(f"archive gate blocked: {', '.join(gate.reasons)}")


def _override_path(args: dict[str, Any]) -> Path:
    repo_root = args.get("repo_root")
    if not isinstance(repo_root, str) or not Path(repo_root).is_absolute():
        raise ValueError("link/unlink require absolute repo_root")
    root = Path(repo_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("repo_root must be a directory")
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
    path = _override_path(args)
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
    for key, row in payload["runs"].items():
        if (
            not isinstance(key, str)
            or not isinstance(row, dict)
            or row.get("status") not in {"ongoing", "needs_human", "blocked", "done"}
            or not isinstance(row.get("run_id"), str)
            or not isinstance(row.get("claim_key"), str)
            or not isinstance(row.get("snapshot_hash"), str)
            or not isinstance(row.get("source_revisions"), list)
            or not isinstance(row.get("provider_revision"), str)
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
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_work_run(*, state_path: Path, repo: str, work_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = _load_runs(state_path)
    active = state["runs"].get(f"{repo}/{work_id}")
    if not isinstance(active, dict):
        raise RuntimeError("ship requires a persisted workflow")
    return state, active


def _claim_action(
    *,
    args: dict[str, Any],
    authority,
    now_epoch: float,
    state_path: Path,
    automatic: bool = False,
    auto_label: bool | None = None,
) -> dict[str, Any]:
    state = _load_runs(state_path)
    key = f"{authority.repo}/{authority.work_id}"
    active = state["runs"].get(key)
    if active is not None and not isinstance(active, dict):
        raise ValueError("active work run malformed")
    issue = args.get("issue") if args.get("issue") is not None else (
        authority.mapped_issues[0] if authority.mapped_issues else None
    )
    candidate = ClaimCandidate(
        authority=authority,
        repo=authority.repo,
        work_id=authority.work_id,
        source_revisions=authority.source_revisions,
        confirmed_todo=authority.confirmed_todo,
        confirmed_issue=issue,
        auto_label=(authority.auto_label if auto_label is None else auto_label) if automatic else False,
        active_run_id=active.get("run_id") if active else None,
        active_claim_key=active.get("claim_key") if active else None,
        active_status=active.get("status") if active else None,
        active_snapshot_hash=active.get("snapshot_hash") if active else None,
        active_source_revisions=(
            tuple(active.get("source_revisions", ())) if active else None
        ),
        active_provider_revision=active.get("provider_revision") if active else None,
    )
    decision = (
        decide_auto_claim(candidate, now_epoch=now_epoch)
        if automatic
        else decide_manual_start(candidate, now_epoch=now_epoch)
    )
    if args["action"] == "resume" and active is None:
        raise ValueError("resume requires an active workflow")
    if decision.action == "claim":
        previous_run_id = active.get("run_id") if active else None
        active = {
            "run_id": f"run-{uuid4().hex}",
            "claim_key": decision.claim_key,
            "repo": authority.repo,
            "work_id": authority.work_id,
            "status": "ongoing",
            "source_revisions": list(authority.source_revisions),
            "snapshot_hash": authority.snapshot_hash,
            "provider_revision": authority.github_provider_revision,
        }
        if previous_run_id is not None:
            active["previous_run_id"] = previous_run_id
        state["runs"][key] = active
        _save_runs(state_path, state)
    elif decision.action == "needs_human":
        active = {
            "run_id": active.get("run_id") if active else f"run-{uuid4().hex}",
            "claim_key": build_claim_key(
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
            ),
            "repo": authority.repo,
            "work_id": authority.work_id,
            "status": "needs_human",
            "reason": decision.reason,
            "source_revisions": list(authority.source_revisions),
            "snapshot_hash": authority.snapshot_hash,
            "provider_revision": authority.github_provider_revision,
        }
        state["runs"][key] = active
        _save_runs(state_path, state)
    return {"action": decision.action, "reason": decision.reason, "run": active}


def run_auto_claim_scan(
    *,
    snapshot_path: str | Path | None = None,
    state_path: str | Path | None = None,
    now: Callable[[], float] = time.time,
    runner: Runner = subprocess.run,
) -> list[dict[str, Any]]:
    """Project the durable Monitor snapshot into Manager-owned auto claims."""

    try:
        authorities = load_work_authorities(snapshot_path=snapshot_path)
    except ValueError as exc:
        if "snapshot unavailable" in str(exc):
            return []
        raise
    resolved_state = Path(state_path) if state_path is not None else _run_state_path()
    results: list[dict[str, Any]] = []
    for authority in authorities:
        if not authority.confirmed_todo:
            continue
        live_auto_label = False
        if authority.mapped_issues:
            issue = authority.mapped_issues[0]
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
                continue
            try:
                issue_payload = json.loads(getattr(completed, "stdout", ""))
                labels = issue_payload["labels"]
                if not isinstance(labels, list):
                    raise TypeError
                names = {
                    label["name"]
                    for label in labels
                    if isinstance(label, dict) and isinstance(label.get("name"), str)
                }
            except (json.JSONDecodeError, KeyError, TypeError):
                results.append(
                    {
                        "repo": authority.repo,
                        "work_id": authority.work_id,
                        "action": "blocked",
                        "reason": "github-label-payload-malformed",
                    }
                )
                continue
            live_auto_label = "cortex:auto-on-going" in names
        result = _claim_action(
            args={"action": "auto-scan"},
            authority=authority,
            now_epoch=now(),
            state_path=resolved_state,
            automatic=True,
            auto_label=live_auto_label,
        )
        if result["action"] not in {"ignore", "done"}:
            results.append(
                {
                    "repo": authority.repo,
                    "work_id": authority.work_id,
                    **result,
                }
            )
    return results


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
    _pr_metadata(args, required_issues=authority.mapped_issues)

    state, active = _load_work_run(
        state_path=state_path,
        repo=authority.repo,
        work_id=authority.work_id,
    )
    ship = active.get("ship")
    if ship is not None and not isinstance(ship, dict):
        raise ValueError("ship state malformed")
    github = GitHubDeliveryClient(runner=runner)
    orchestrator = ShipOrchestrator(github=github, now=now)

    if ship and ship.get("phase") == "needs_human":
        return {"action": "needs_human", "reason": ship.get("reason")}

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
            "todo_paths": list(todo_paths_value),
            "completion_record": dict(closure.completion_record),
        }
        active["status"] = "done"
        _save_runs(state_path, state)
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
        if (
            not isinstance(expected_head, str)
            or not isinstance(record, dict)
            or not isinstance(record.get("path"), str)
            or not isinstance(record.get("hash"), str)
            or not isinstance(todo_paths, list)
        ):
            raise ValueError("cached done state malformed")
        from . import completion

        completion_payload = completion.read_completion_record(
            record["path"], expected_hash=record["hash"]
        )
        closure = orchestrator.verify_remote_closure(
            repo=authority.repo,
            pr_number=pr_number,
            change=change,
            authority=authority,
            todo_paths=tuple(todo_paths),
            expected_head=expected_head,
            completion_payload=completion_payload,
        )
        return {
            "action": "done",
            "head": expected_head,
            "merge_commit": closure.facts.merge_commit,
            "completion_record": dict(closure.completion_record),
        }

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
        active["ship"] = {
            **(ship or {}),
            "phase": "merged",
            "head": preflight.head,
            "tree_hash": preflight.tree_hash,
            "merge_commit": merge_status.merge_commit,
            "pr_number": pr_number,
            "change": change,
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
    fix_rounds = ship.get("fix_rounds", 0) if ship else 0
    if not isinstance(fix_rounds, int) or isinstance(fix_rounds, bool) or fix_rounds < 0:
        raise ValueError("ship fix round state malformed")
    if ship and ship.get("phase") == "needs-fix" and previous_head == preflight.head:
        return {"action": "fix-required", "head": preflight.head, "fix_rounds": fix_rounds}
    if previous_head is not None and previous_head != preflight.head:
        fix_rounds += 1
        if fix_rounds > 2:
            active["status"] = "needs_human"
            active["ship"] = {
                **ship,
                "phase": "needs_human",
                "reason": "copilot-finding-budget-exhausted",
                "head": preflight.head,
                "fix_rounds": fix_rounds,
            }
            _save_runs(state_path, state)
            return {"action": "needs_human", "reason": "copilot-finding-budget-exhausted"}
    if not ship or previous_head != preflight.head or ship.get("phase") != "review-requested":
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
            active["status"] = "needs_human"
            active["ship"] = {**ship, "phase": "needs_human", "reason": "copilot-review-timeout"}
            _save_runs(state_path, state)
            return {"action": "needs_human", "reason": "copilot-review-timeout"}
        return {"action": "awaiting-copilot", "head": preflight.head}
    review = max(current_reviews, key=lambda value: (value.submitted_at_epoch, value.review_id))
    loop = ReviewLoop(
        head=preflight.head,
        fix_rounds=fix_rounds,
        epoch_started_at=float(ship.get("epoch_started_at", requested_at)),
        requested_at=float(requested_at),
    )
    finding_count = sum(1 for thread in remote.review_threads if thread.blocks_merge)
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
            "fix_rounds": fix_rounds,
        }
        _save_runs(state_path, state)
        return {"action": "fix-required", "reason": copilot.reason, "findings": finding_count}
    if copilot.action != "passed":
        active["status"] = "needs_human"
        active["ship"] = {**ship, "phase": "needs_human", "reason": copilot.reason}
        _save_runs(state_path, state)
        return {"action": "needs_human", "reason": copilot.reason}

    foreign_review = ForeignReviewEvidence(
        path=str(_absolute_file(args.get("foreign_review_path"), field="foreign_review_path")),
        expected_hash=args.get("foreign_review_hash"),
    )
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
        if not post_merge.merged or post_merge.pr_head != preflight.head:
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
