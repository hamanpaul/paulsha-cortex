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
from . import verification
from .preflight import PreflightRequest, load_preflight_command, run_preflight
from .work_bridge import resolve_trusted_repo_root, workflow_status
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
    policy_text = "\n".join(
        str(getattr(policy, field, "")) for field in ("stdout", "stderr")
    )
    doc_reference_warning = bool(
        re.search(r"(?i)(?:R-22.*WARN|WARN.*R-22|doc-reference.*WARN|WARN.*doc-reference)", policy_text)
    )
    facts = ArchiveGateFacts(
        tasks_complete=bool(task_states) and all(state.lower() == "x" for state in task_states),
        canonical_specs_valid=getattr(canonical, "returncode", None) == 0,
        doc_references_valid=(
            getattr(policy, "returncode", None) == 0 and not doc_reference_warning
        ),
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
    return {"action": args["action"], "override_path": str(path), "source": ref}


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
        and body.get("authority_digest") == work_authority_digest(authority)
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
) -> dict[str, Any]:
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
    )
    if (
        canonical_run is not None
        and canonical_run.claim_key != _expected_claim_key(authority)
        and (automatic or args.get("action") == "resume")
    ):
        active = canonical_run.to_dict()
        active.update(
            {
                "snapshot_hash": authority.snapshot_hash,
                "source_revisions": list(authority.source_revisions),
                "provider_revision": authority.github_provider_revision,
                "authority_digest": work_authority_digest(authority),
                "status": workflow_status(canonical_run),
            }
        )
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
        run = workflow_starter(authority, str(decision.claim_key), None)
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
    return {"action": decision.action, "reason": decision.reason, "run": active}


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
    )
    return {
        "action": "retry-build",
        "reason": "candidate-repair-dispatched",
        "expected_candidate": expected_candidate.lower(),
        "run": updated.to_dict(),
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


def run_auto_claim_scan(
    *,
    snapshot_path: str | Path | None = None,
    state_path: str | Path | None = None,
    now: Callable[[], float] = time.time,
    runner: Runner = subprocess.run,
    workflow_registry=None,
    workflow_starter=None,
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
        result = _claim_action(
            args={"action": "auto-scan"},
            authority=authority,
            now_epoch=now(),
            state_path=resolved_state,
            automatic=True,
            auto_label=live_auto_label,
            workflow_registry=workflow_registry,
            workflow_starter=workflow_starter,
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
            )
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
            run_id=active["run_id"],
            workflow_step_ids=tuple(active["workflow_step_ids"]),
            trusted_evidence_refs=_trusted_evidence_refs(authorization),
        )
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
    fix_rounds = ship.get("fix_rounds", 0) if ship else 0
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
        if fix_rounds > 2:
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
            return {"action": "needs_human", "reason": "copilot-finding-budget-exhausted"}
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
        active["ship"] = {**ship, "phase": "needs_human", "reason": copilot.reason}
        _save_runs(state_path, state)
        workflow_registry._manager_update_workflow_run(
            canonical_run.run_id, facets=("needs_human",), gate_status="running"
        )
        return {"action": "needs_human", "reason": copilot.reason}

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
) -> dict[str, Any]:
    action = args.get("action")
    repo = args.get("repo")
    work_id = args.get("work_id")
    if action not in {
        "link", "unlink", "start", "resume", "retry-build", "abandon", "auto", "ship",
        "review-attest",
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
        )
    elif action == "retry-build":
        result = _retry_build_action(
            args=args,
            authority=authority,
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
