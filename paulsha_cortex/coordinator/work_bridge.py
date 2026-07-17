"""Narrow integration seam joining WorkAuthority, WorkflowRun, and delivery.

The JobRegistry aggregate is the only workflow truth.  Delivery keeps a
run-keyed journal, but never invents a second run identity or lifecycle state.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

from paulsha_cortex.config import paths
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    load_cards,
    load_combo,
)

from . import verification
from .claim import WorkAuthority, load_work_authority, work_authority_digest
from .github_delivery import GitHubDeliveryClient
from .model_identities import IdentityRegistry, load_model_identities
from .preflight import PreflightRequest, load_preflight_command, run_preflight


def _remote_repo(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return None
    value = result.stdout.strip()
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if value.startswith(prefix):
            repo = value[len(prefix) :].removesuffix(".git").rstrip("/")
            return repo if repo.count("/") == 1 else None
    return None


def resolve_trusted_repo_root(repo: str, *, explicit: object = None) -> Path:
    """Resolve owner/name only through installed repo/Monitor configuration."""

    candidates: list[Path] = []
    if isinstance(explicit, str) and explicit:
        raw = Path(explicit).expanduser()
        try:
            root = raw.resolve(strict=True)
        except OSError as exc:
            raise ValueError("explicit repo root unavailable") from exc
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            shell=False,
            capture_output=True,
            text=True,
        )
        try:
            top_root = Path(top.stdout.strip()).resolve(strict=True)
        except (AttributeError, OSError):
            top_root = Path()
        if (
            raw.is_symlink()
            or not root.is_dir()
            or top.returncode != 0
            or top_root != root
            or _remote_repo(root) != repo
        ):
            raise ValueError(
                "explicit repo root must be the canonical git top-level and its remote must match owner/name"
            )
        return root
    candidates.append(paths.repo_root())
    try:
        from paulsha_cortex.monitor.config import load_config

        config = load_config()
        candidates.extend(item.path for item in config.workspaces)
        candidates.extend(Path(item.path) for item in config.hippo_projects)
    except (AttributeError, FileNotFoundError, OSError, TypeError, ValueError):
        pass
    matches: list[Path] = []
    for raw in candidates:
        try:
            root = raw.resolve(strict=True)
        except OSError:
            continue
        if raw.is_symlink() or not root.is_dir() or _remote_repo(root) != repo:
            continue
        if root not in matches:
            matches.append(root)
    if len(matches) != 1:
        raise ValueError("trusted repo registry did not resolve exactly one owner/name root")
    return matches[0]


def default_workflow_manifest(work_id: str, *, change: str | None):
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(
        combo,
        cards,
        work_id,
        change=change or work_id,
        allow_external=True,
    )
    if result.workflow_manifest is None:  # pragma: no cover - compile contract
        raise RuntimeError("feature-oneshot did not produce a workflow manifest")
    result.workflow_manifest.validate_manager_spine()
    return result.workflow_manifest


def _artifact_rows(root: Path, authority: WorkAuthority) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, ref: str) -> None:
        key = (kind, ref)
        if key not in seen and (root / ref).is_file():
            seen.add(key)
            rows.append({"kind": kind, "ref": ref})

    for revision in authority.source_revisions:
        source_id = revision.rsplit("@", 1)[0]
        prefix = f"superpowers_spec:{authority.repo}:"
        if source_id.startswith(prefix):
            add("spec", source_id[len(prefix) :])
        prefix = f"superpowers_plan:{authority.repo}:"
        if source_id.startswith(prefix):
            add("plan", source_id[len(prefix) :])
    for change in authority.mapped_openspec:
        base = f"openspec/changes/{change}"
        add("spec", f"{base}/proposal.md")
        add("design", f"{base}/design.md")
        add("plan", f"{base}/tasks.md")
    for ref in authority.mapped_todo_paths:
        add("plan", ref)
    return rows


def _write_manifest(root: Path, claim_key: str, manifest) -> Path:
    directory = root / "workflow-manifests"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{claim_key.removeprefix('claim:v1:')}.json"
    body = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    if target.exists():
        if target.read_text(encoding="utf-8") != body:
            raise RuntimeError("canonical workflow manifest conflicts with persisted claim")
        return target
    temporary = directory / f".{target.name}.{os.getpid()}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def start_canonical_workflow(
    *,
    registry,
    authority: WorkAuthority,
    claim_key: str,
    coordinator_root: str | Path,
    explicit_repo_root: object = None,
    identity_registry: IdentityRegistry | None = None,
    runtime_factory=None,
    needs_human_reason: str | None = None,
):
    """Create/resume the real WorkflowRun for a WorkAuthority claim."""

    existing = [run for run in registry.list_workflow_runs() if run.claim_key == claim_key]
    if existing:
        if len(existing) != 1 or existing[0].repo != authority.repo or existing[0].work_id != authority.work_id:
            raise RuntimeError("canonical workflow claim collision")
        return existing[0]
    root = resolve_trusted_repo_root(authority.repo, explicit=explicit_repo_root)
    change = authority.mapped_openspec[0] if authority.mapped_openspec else authority.work_id
    manifest = default_workflow_manifest(authority.work_id, change=change)
    if needs_human_reason is not None:
        run = registry._manager_create_workflow_run(
            work_id=authority.work_id,
            repo=authority.repo,
            claim_key=claim_key,
            source_revision=work_authority_digest(authority),
            workspace_root=str(root),
            combo=manifest.combo,
            current_phase="claim",
            steps=manifest.steps,
            issue_refs=tuple(f"{authority.repo}#{number}" for number in authority.mapped_issues),
            openspec_refs=authority.mapped_openspec,
            pr_refs=tuple(f"{authority.repo}#{number}" for number in authority.mapped_prs),
            attempts={"claim": 1},
            facets=("needs_human",),
            gate_status="running",
        )
        return run
    manifest_path = _write_manifest(Path(coordinator_root), claim_key, manifest)
    identities = identity_registry or load_model_identities()
    planning = [identity for identity in identities.identities if "planning" in identity.capabilities]
    if not planning:
        raise RuntimeError("no primary planning identity configured")
    primary = next(
        (identity for executor in ("codex", "claude", "agy") for identity in planning if identity.executor == executor),
        planning[0],
    )
    from . import manager

    result = manager.apply_workflow_action(
        registry,
        args={
            "action": "start",
            "work_id": authority.work_id,
            "repo": authority.repo,
            "claim_key": claim_key,
            "source_revision": work_authority_digest(authority),
            "artifact_root": str(root),
            "evidence_dir": str(Path(coordinator_root) / "evidence" / "planning"),
            "manifest_path": str(manifest_path),
            "planning_artifacts": _artifact_rows(root, authority),
            "primary_executor": primary.executor,
            "primary_model": primary.model_id,
            "primary_domain": primary.independence_domain,
            "issue_refs": [f"{authority.repo}#{number}" for number in authority.mapped_issues],
            "openspec_refs": list(authority.mapped_openspec),
            "pr_refs": [f"{authority.repo}#{number}" for number in authority.mapped_prs],
        },
        identity_registry=identities,
        runtime_factory=runtime_factory,
        coordinator_root=coordinator_root,
    )
    return registry.get_workflow_run(str(result["run_id"]))


def workflow_status(run) -> str:
    if getattr(run, "status", "ongoing") == "done":
        return "done"
    if "needs_human" in run.facets:
        return "needs_human"
    if "blocked" in run.facets:
        return "blocked"
    return "ongoing"


def _write_json_evidence(root: Path, category: str, payload: dict) -> dict[str, str]:
    digest = verification.canonical_json_hash(payload)
    directory = root.resolve() / "evidence" / category
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    envelope = {"payload": payload, "hash": digest}
    if target.exists():
        if target.is_symlink() or json.loads(target.read_text(encoding="utf-8")) != envelope:
            raise RuntimeError(f"{category} evidence conflict")
    else:
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            target.chmod(0o400)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ref": str(target), "hash": digest}


def _pr_metadata(run) -> dict[str, object]:
    issues = []
    for ref in run.issue_refs:
        prefix = f"{run.repo}#"
        if not ref.startswith(prefix) or not ref[len(prefix) :].isdigit():
            raise ValueError("workflow issue ref malformed")
        issues.append(int(ref[len(prefix) :]))
    if not issues:
        raise ValueError("workflow delivery requires a confirmed issue")
    body = "\n".join(
        [
            f"## 摘要\n\n完成 `{run.work_id}` 統一工作生命週期。",
            "## 驗證\n\n- [x] Manager exact-HEAD delivery gates",
            *(f"Closes #{number}" for number in sorted(issues)),
        ]
    )
    return {
        "title": f"feat(workflow): 完成 {run.work_id}",
        "body": body,
        "labels": ["enhancement"],
    }


def _metadata_file(root: Path, run, metadata: dict[str, object]) -> Path:
    directory = root.resolve() / "evidence" / "pr-metadata"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run.run_id}.json"
    body = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if target.exists():
        if target.is_symlink() or target.read_text(encoding="utf-8") != body:
            raise RuntimeError("workflow PR metadata conflict")
        return target
    temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _builder_binding(
    registry,
    run,
    candidate: str,
    *,
    state_root: Path,
    foreign_ref,
) -> tuple[Path, str]:
    """Use the foreign-review edge to select the exact builder job.

    A feature workflow legitimately has several build cards, so counting all
    successful build jobs is ambiguous. The terminal review evidence names the
    one builder job whose candidate it actually reviewed.
    """

    from . import review

    review_payload, review_job = _workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=run,
        phase="review",
        expected_ref=foreign_ref.ref,
        expected_hash=foreign_ref.sha256,
    )
    evaluation = review.validate_gate_evaluation(review_payload)
    builder_job_id = evaluation.get("builder_job_id")
    if (
        evaluation.get("state") != "passed"
        or evaluation.get("candidate") != candidate
        or evaluation.get("reviewer_job_id") != review_job.get("job_id")
        or not isinstance(builder_job_id, str)
    ):
        raise RuntimeError("delivery foreign-review builder binding malformed")
    row = registry.get_job(builder_job_id)
    if (
        row.get("workflow_run_id") != run.run_id
        or row.get("workflow_phase") != "build"
        or row.get("status") != "exited"
        or row.get("exit_code") != 0
        or row.get("subject_head") != candidate
    ):
        raise RuntimeError("delivery requires the reviewed exact-candidate builder job")
    worktree = row.get("worktree")
    branch = row.get("branch")
    if not isinstance(worktree, str) or not isinstance(branch, str) or not branch:
        raise RuntimeError("builder delivery binding malformed")
    root = Path(worktree).resolve(strict=True)
    return root, branch


def _authority_with_manager_pr(authority: WorkAuthority, pr_number: int) -> WorkAuthority:
    return WorkAuthority._verified(
        repo=authority.repo,
        work_id=authority.work_id,
        mapped_issues=authority.mapped_issues,
        mapped_prs=(pr_number,),
        mapped_openspec=authority.mapped_openspec,
        mapped_todo_paths=authority.mapped_todo_paths,
        confirmed_todo=authority.confirmed_todo,
        auto_label=authority.auto_label,
        source_revisions=authority.source_revisions,
        provider_revision=authority.github_provider_revision,
        provider_id=authority.github_provider_id,
        last_success_epoch=authority.github_last_success_epoch,
        snapshot_hash=authority.snapshot_hash,
    )


def _workflow_evidence_payload(
    *,
    registry,
    state_root: Path,
    run,
    phase: str,
    expected_ref: str | None = None,
    expected_hash: str | None = None,
) -> tuple[dict, dict]:
    rows = [
        row
        for row in registry.list_jobs()
        if row.get("workflow_run_id") == run.run_id
        and row.get("workflow_phase") == phase
        and row.get("status") == "exited"
        and row.get("exit_code") == 0
        and isinstance(row.get("workflow_evidence"), dict)
    ]
    if expected_ref is not None:
        rows = [
            row
            for row in rows
            if str(
                (
                    state_root / str(row["workflow_evidence"]["path"])
                ).resolve()
            )
            == str(Path(expected_ref).resolve())
        ]
    if len(rows) != 1:
        raise RuntimeError(f"delivery requires one canonical {phase} evidence job")
    job = rows[0]
    locator = job["workflow_evidence"]
    relative = Path(str(locator.get("path")))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("workflow evidence locator escapes coordinator root")
    path = (state_root / relative).resolve(strict=True)
    path.relative_to(state_root)
    content = path.read_bytes()
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != locator.get("hash") or (
        expected_hash is not None and actual_hash != expected_hash
    ):
        raise RuntimeError("workflow evidence hash drift")
    envelope = json.loads(content.decode("utf-8"))
    expected_job = {
        "job_id": job["job_id"],
        "run_id": job["workflow_run_id"],
        "claim_key": job["workflow_claim_key"],
        "repo": job["workflow_repo"],
        "source_revision": job["source_revision"],
        "card_id": job["workflow_card"],
        "phase": job["workflow_phase"],
        "inputs": job.get("workflow_inputs", []),
        "outputs": job.get("workflow_outputs", []),
        "output_baseline": job.get("workflow_output_baseline", []),
    }
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != 1
        or envelope.get("kind") != phase
        or envelope.get("job") != expected_job
        or not isinstance(envelope.get("artifacts"), list)
        or not isinstance(payload, dict)
    ):
        raise RuntimeError("workflow evidence envelope malformed")
    payload = dict(payload)
    payload.pop("outputs", None)
    return payload, job


def _completion_draft(
    *,
    registry,
    state_root: Path,
    run,
    authority: WorkAuthority,
    candidate: str,
    pr_number: int,
    foreign_ref,
    runner,
    now,
) -> Path | None:
    journal_path = state_root / "delivery-journal.json"
    if not journal_path.is_file() or journal_path.is_symlink():
        return None
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    row = journal.get("runs", {}).get(run.run_id) if isinstance(journal, dict) else None
    ship = row.get("ship") if isinstance(row, dict) else None
    if not isinstance(ship, dict) or ship.get("phase") != "merged":
        return None
    authorization = ship.get("merge_authorization")
    if not isinstance(authorization, dict):
        raise RuntimeError("merged delivery journal lacks authorization")
    from . import completion, review
    from . import work_actions

    verification_payload, _verify_job = _workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=run,
        phase="verify",
    )
    verification_payload["status"] = "reviewing"
    verification_record = verification.write_verification_evidence(
        verification_payload,
        coordinator_root=state_root,
    )
    review_payload, review_job = _workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=run,
        phase="review",
        expected_ref=foreign_ref.ref,
    )
    review_record = review.write_gate_evaluation(
        review_payload,
        coordinator_root=state_root,
    )
    builder_job_id = review_record["payload"]["builder_job_id"]
    builder_job = registry.get_job(builder_job_id)
    dispatch_base = builder_job.get("dispatch_head")
    branch = builder_job.get("branch")
    if (
        not isinstance(dispatch_base, str)
        or verification.SAFE_SHA_RE.fullmatch(dispatch_base) is None
        or not isinstance(branch, str)
        or not branch
        or review_job.get("job_id") != review_record["payload"]["reviewer_job_id"]
    ):
        raise RuntimeError("workflow completion job binding malformed")
    github = GitHubDeliveryClient(runner=runner)
    closure = github.fetch_remote_closure(
        repo=authority.repo,
        pr_number=pr_number,
        change=authority.mapped_openspec[0],
        required_issues=authority.mapped_issues,
        todo_paths=authority.mapped_todo_paths,
    )
    default_branch = github.fetch_default_branch(repo=authority.repo)
    by_kind: dict[str, list[str]] = {"spec": [], "plan": []}
    for item in run.planning_authority:
        if item.kind in by_kind:
            by_kind[item.kind].append(item.baseline_sha256)
    if not by_kind["spec"] or not by_kind["plan"]:
        raise RuntimeError("completion requires canonical spec and plan authority")
    trusted_refs = work_actions._trusted_evidence_refs(authorization)
    payload = {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": run.run_id,
        "spec_hash": verification.canonical_json_hash(sorted(by_kind["spec"])),
        "plan_hash": verification.canonical_json_hash(sorted(by_kind["plan"])),
        "verification_hash": verification_record["hash"],
        "builder_job_id": builder_job_id,
        "reviewer_job_id": review_record["payload"]["reviewer_job_id"],
        "dispatch_base": dispatch_base,
        "candidate": candidate,
        "target_branch": default_branch,
        "target_remote": "origin",
        "target_ref": f"refs/remotes/origin/{default_branch}",
        "target_ref_sha": closure.default_head,
        "verification_evidence_path": verification_record["path"],
        "verification_evidence_hash": verification_record["hash"],
        "review_policy": "required",
        "docs_class": "code",
        "review_evaluation_path": review_record["path"],
        "review_evaluation_hash": review_record["hash"],
        "completed_at": datetime.fromtimestamp(float(now()), timezone.utc).isoformat(),
        "work_authority": {
            "repo": authority.repo,
            "work_id": authority.work_id,
            "snapshot_hash": authority.snapshot_hash,
            "provider_id": authority.github_provider_id,
            "provider_revision": authority.github_provider_revision,
            "source_revisions": sorted(authority.source_revisions),
            "mapped_issues": sorted(authority.mapped_issues),
            "mapped_prs": sorted(authority.mapped_prs),
            "mapped_openspec": sorted(authority.mapped_openspec),
            "mapped_todo_paths": sorted(authority.mapped_todo_paths),
            "pr_number": pr_number,
            "change": authority.mapped_openspec[0],
            "todo_paths": sorted(authority.mapped_todo_paths),
            "merge_commit": closure.merge_commit,
            "run_id": run.run_id,
            "workflow_step_ids": sorted(row["workflow_step_ids"]),
            "trusted_evidence_refs": [dict(item) for item in trusted_refs],
        },
    }
    normalized = completion.validate_completion_record(payload)
    directory = state_root / "evidence" / "completion-drafts"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run.run_id}-{candidate}.json"
    if target.exists():
        if json.loads(target.read_text(encoding="utf-8")) != normalized:
            raise RuntimeError("completion draft conflict")
    else:
        verification.atomic_write_json(target, normalized)
    return target


def build_production_ship_validator(
    *,
    registry,
    coordinator_root: str | Path,
    runner: Callable[..., object] = subprocess.run,
    now: Callable[[], float] = time.time,
    snapshot_path: str | Path | None = None,
):
    """Bind review completion to the authenticated, resumable delivery state machine."""

    state_root = Path(coordinator_root).resolve()

    def validate(*, run, candidate: str | None) -> dict[str, object]:
        if (
            not isinstance(candidate, str)
            or run.candidate_head != candidate
            or run.verified_head != candidate
            or run.current_phase != "review"
        ):
            raise ValueError("ship adapter requires review-complete exact candidate")
        foreign = [ref for ref in run.gate_refs if ref.kind == "foreign-review"]
        if len(foreign) != 1 or foreign[0].sha256 is None:
            raise ValueError("ship adapter requires canonical foreign-review evidence")
        authority = load_work_authority(
            repo=run.repo,
            work_id=run.work_id,
            snapshot_path=snapshot_path,
        )
        expected_issues = tuple(f"{run.repo}#{number}" for number in authority.mapped_issues)
        if run.issue_refs != expected_issues or run.openspec_refs != authority.mapped_openspec:
            raise RuntimeError("WorkflowRun refs differ from current WorkAuthority")
        worktree, branch = _builder_binding(
            registry,
            run,
            candidate,
            state_root=state_root,
            foreign_ref=foreign[0],
        )
        metadata = _pr_metadata(run)
        metadata_path = _metadata_file(state_root, run, metadata)
        pr_numbers = []
        for ref in run.pr_refs:
            prefix = f"{run.repo}#"
            if not ref.startswith(prefix) or not ref[len(prefix) :].isdigit():
                raise ValueError("workflow PR ref malformed")
            pr_numbers.append(int(ref[len(prefix) :]))
        if len(pr_numbers) > 1:
            raise RuntimeError("workflow delivery supports one PR")
        if not pr_numbers:
            initial = run_preflight(
                repo_root=worktree,
                command=load_preflight_command(),
                request=PreflightRequest(metadata_path=str(metadata_path)),
                runner=runner,
                now=now,
            )
            if not initial.passed or initial.head != candidate:
                raise RuntimeError("initial PR-metadata preflight failed")
            github = GitHubDeliveryClient(runner=runner)
            number = github.create_or_get_pull_request(
                repo=run.repo,
                branch=branch,
                expected_head=candidate,
                title=str(metadata["title"]),
                body=str(metadata["body"]),
                labels=tuple(metadata["labels"]),
            )
            authority = _authority_with_manager_pr(authority, number)
            updated = registry._manager_update_workflow_run(
                run.run_id,
                source_revision=work_authority_digest(authority),
                pr_refs=(f"{run.repo}#{number}",),
            )
            evidence = _write_json_evidence(
                state_root,
                "delivery-adapter",
                {
                    "schema": "cortex-delivery-adapter/v1",
                    "run_id": updated.run_id,
                    "candidate": candidate,
                    "action": "pr-created",
                    "pr_number": number,
                    "authority_digest": updated.source_revision,
                },
            )
            return {
                "trusted": True,
                "status": "pending",
                "head": candidate,
                "commit_id": candidate,
                **evidence,
            }
        number = pr_numbers[0]
        if authority.mapped_prs not in {(), (number,)}:
            raise RuntimeError("workflow PR differs from current WorkAuthority")
        authority = _authority_with_manager_pr(authority, number)
        if run.source_revision != work_authority_digest(authority):
            run = registry._manager_update_workflow_run(
                run.run_id,
                source_revision=work_authority_digest(authority),
            )
        from . import work_actions

        completion_draft = _completion_draft(
            registry=registry,
            state_root=state_root,
            run=run,
            authority=authority,
            candidate=candidate,
            pr_number=number,
            foreign_ref=foreign[0],
            runner=runner,
            now=now,
        )
        ship_args = {
            "repo_root": str(worktree),
            "pr_number": number,
            "change": authority.mapped_openspec[0] if len(authority.mapped_openspec) == 1 else None,
            "todo_paths": list(authority.mapped_todo_paths),
            "foreign_review_path": foreign[0].ref,
            "foreign_review_hash": foreign[0].sha256,
            "pr_metadata_path": str(metadata_path),
            "skip_tests": False,
        }
        if completion_draft is not None:
            ship_args["completion_record_path"] = str(completion_draft)
        action = work_actions._ship_action(
            args=ship_args,
            authority=authority,
            runner=runner,
            now=now,
            state_path=state_root / "delivery-journal.json",
            workflow_registry=registry,
        )
        status = "passed" if action.get("action") == "done" else "pending"
        if action.get("action") == "needs_human":
            status = "needs_human"
        evidence = _write_json_evidence(
            state_root,
            "delivery-adapter",
            {
                "schema": "cortex-delivery-adapter/v1",
                "run_id": run.run_id,
                "candidate": candidate,
                "action": action.get("action"),
                "pr_number": number,
            },
        )
        result: dict[str, object] = {
            "trusted": True,
            "status": status,
            "head": candidate,
            "commit_id": candidate,
            "reason": action.get("reason"),
            **evidence,
        }
        if status == "passed":
            record = action.get("completion_record")
            merge_revision = action.get("merge_commit")
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("path"), str)
                or not isinstance(record.get("hash"), str)
                or not isinstance(merge_revision, str)
            ):
                raise RuntimeError("delivery completion result malformed")
            result["completion"] = {
                "record_path": record["path"],
                "record_hash": record["hash"],
                "record_revision": candidate,
                "source_revisions": {
                    value.rsplit("@", 1)[0]: value.rsplit("@", 1)[1]
                    for value in authority.source_revisions
                    if "@" in value
                },
                "pr_candidate": candidate,
                "merge_revision": merge_revision,
            }
        return result

    return validate
