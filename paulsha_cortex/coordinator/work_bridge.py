"""Narrow integration seam joining WorkAuthority, WorkflowRun, and delivery.

The JobRegistry aggregate is the only workflow truth.  Delivery keeps a
run-keyed journal, but never invents a second run identity or lifecycle state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
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
    existing_run = None
    if existing:
        if len(existing) != 1 or existing[0].repo != authority.repo or existing[0].work_id != authority.work_id:
            raise RuntimeError("canonical workflow claim collision")
        existing_run = existing[0]
        if existing_run.status != "ongoing":
            return existing_run
        if existing_run.current_phase != "define":
            return existing_run
    root = resolve_trusted_repo_root(authority.repo, explicit=explicit_repo_root)
    change = authority.mapped_openspec[0] if authority.mapped_openspec else authority.work_id
    manifest = default_workflow_manifest(authority.work_id, change=change)
    if needs_human_reason is not None:
        if existing_run is not None:
            return existing_run
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
    if getattr(run, "status", "ongoing") == "superseded":
        return "blocked"
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
    normal_builder = (
        row.get("workflow_phase") == "build"
        and row.get("persona") == "builder"
    )
    manager_archive = (
        row.get("workflow_phase") == "ship"
        and row.get("workflow_card") == "openspec-archive"
        and row.get("persona") == "manager"
        and row.get("executor") == "cortex-manager"
        and row.get("model_id") == "deterministic"
        and row.get("independence_domain") == "cortex"
        and isinstance(row.get("workflow_evidence"), dict)
    )
    if (
        row.get("workflow_run_id") != run.run_id
        or not (normal_builder or manager_archive)
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


def _push_exact_candidate(
    *,
    registry,
    run,
    authority: WorkAuthority,
    state_root: Path,
    worktree: Path,
    branch: str,
    candidate: str,
    runner,
    pre_push: Callable[[], None] | None = None,
) -> None:
    if re.fullmatch(r"feature/[a-z0-9][a-z0-9._/-]*", branch) is None:
        raise ValueError("workflow delivery branch is not an authorized feature ref")
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0 or head.stdout.strip().lower() != candidate:
        raise RuntimeError("delivery push requires exact local Candidate HEAD")
    from . import work_actions

    state_path = state_root / "delivery-journal.json"
    current_digest = work_authority_digest(authority)
    if run.source_revision != current_digest:
        run = registry._manager_update_workflow_run(
            run.run_id,
            source_revision=current_digest,
        )
    journal = work_actions._load_runs(state_path)
    if run.run_id in journal["runs"]:
        _rebase_delivery_journal_authority(
            state_root=state_root,
            run=run,
            authority=authority,
        )
    state, row, _canonical = work_actions._load_work_run(
        state_path=state_path,
        workflow_registry=registry,
        authority=authority,
    )
    ref = f"refs/heads/{branch}"

    def read_remote() -> str | None:
        completed = runner(
            ["git", "-C", str(worktree), "ls-remote", "--exit-code", "origin", ref],
            shell=False,
            capture_output=True,
            text=True,
        )
        if getattr(completed, "returncode", 1) == 2:
            return None
        if getattr(completed, "returncode", 1) != 0:
            raise RuntimeError("delivery remote branch readback failed")
        fields = str(getattr(completed, "stdout", "")).strip().split()
        if len(fields) != 2 or fields[1] != ref or verification.SAFE_SHA_RE.fullmatch(fields[0]) is None:
            raise RuntimeError("delivery remote branch readback malformed")
        return fields[0].lower()

    remote_head = read_remote()
    pushes = row.setdefault("pushes", {})
    persisted = pushes.get(candidate)
    expected = {"branch": branch, "ref": ref, "head": candidate}
    if persisted is not None and persisted != expected:
        raise RuntimeError("delivery push journal conflicts with Candidate")
    if remote_head != candidate:
        if pre_push is not None:
            pre_push()
        pushed = runner(
            ["git", "-C", str(worktree), "push", "origin", f"HEAD:{ref}"],
            shell=False,
            capture_output=True,
            text=True,
        )
        if getattr(pushed, "returncode", 1) != 0:
            raise RuntimeError("delivery exact Candidate push failed")
        remote_head = read_remote()
    if remote_head != candidate:
        raise RuntimeError("delivery remote branch does not match exact Candidate")
    if persisted is None:
        pushes[candidate] = expected
        work_actions._save_runs(state_path, state)


def _rebase_delivery_journal_authority(
    *, state_root: Path, run, authority: WorkAuthority
) -> None:
    from . import work_actions

    state_path = state_root / "delivery-journal.json"
    state = work_actions._load_runs(state_path)
    row = state["runs"].get(run.run_id)
    if not isinstance(row, dict):
        raise RuntimeError("delivery push journal missing canonical run")
    row.update(
        {
            "source_revisions": list(authority.source_revisions),
            "snapshot_hash": authority.snapshot_hash,
            "provider_revision": authority.github_provider_revision,
            "authority_digest": work_authority_digest(authority),
            "mapped_issues": list(authority.mapped_issues),
            "mapped_prs": list(authority.mapped_prs),
            "mapped_openspec": list(authority.mapped_openspec),
            "mapped_todo_paths": list(authority.mapped_todo_paths),
        }
    )
    work_actions._save_runs(state_path, state)


def _archive_path_allowed(path: str, *, change: str) -> bool:
    return (
        path == "CHANGELOG.md"
        or path == "README.md"
        or path.startswith("changelog.d/")
        or path.startswith("docs/")
        or path.startswith("openspec/specs/")
        or path.startswith("openspec/changes/archive/")
        or path.startswith(f"openspec/changes/{change}/")
    )


def _record_manager_ship_job(
    *,
    registry,
    state_root: Path,
    run,
    worktree: Path,
    branch: str,
    card: str,
    old_head: str,
    new_head: str,
):
    existing = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_phase") == "ship"
        and job.get("workflow_card") == card
        and job.get("subject_head") == new_head
        and job.get("status") == "exited"
        and job.get("exit_code") == 0
        and isinstance(job.get("workflow_evidence"), dict)
    ]
    if len(existing) == 1:
        return existing[0]
    if existing:
        raise RuntimeError("manager ship card audit is ambiguous")
    job = registry.create_job(
        task=f"wf-{hashlib.sha256(run.run_id.encode()).hexdigest()[:10]}-{card}",
        persona="manager",
        kind="build",
        branch=branch,
        pane="",
        worktree=str(worktree),
        dispatch_head=old_head,
        executor="cortex-manager",
        model_id="deterministic",
        independence_domain="cortex",
        subject_head=new_head,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=card,
        workflow_phase="ship",
        workflow_repo_root=str(worktree),
        source_revision=run.source_revision,
    )
    job = registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    envelope = {
        "schema_version": 1,
        "kind": "ship",
        "job": {
            "job_id": job["job_id"],
            "run_id": run.run_id,
            "claim_key": run.claim_key,
            "repo": run.repo,
            "source_revision": run.source_revision,
            "card_id": card,
            "phase": "ship",
            "inputs": [],
            "outputs": [],
            "output_baseline": [],
        },
        "payload": {
            "schema_version": 1,
            "kind": "workflow-card",
            "status": "passed",
            "run_id": run.run_id,
            "card_id": card,
            "candidate": new_head,
            "outputs": [],
        },
        "artifacts": [],
    }
    content = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    relative = Path("evidence") / "workflow" / f"{hashlib.sha256(str(job['job_id']).encode()).hexdigest()}.json"
    target = state_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or target.read_bytes() != content:
            raise RuntimeError("manager archive evidence conflict")
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    return registry.bind_workflow_evidence(
        str(job["job_id"]),
        locator={
            "kind": "ship",
            "path": relative.as_posix(),
            "hash": hashlib.sha256(content).hexdigest(),
        },
        subject_head=new_head,
    )


def _commit_archive_and_require_reverification(
    *, registry, state_root: Path, run, authority: WorkAuthority, worktree: Path, branch: str, candidate: str, runner
):
    if len(authority.mapped_openspec) != 1:
        raise RuntimeError("archive commit requires one OpenSpec change")
    change = authority.mapped_openspec[0]
    tracked = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", "HEAD"],
        shell=False,
        capture_output=True,
    )
    untracked = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
        shell=False,
        capture_output=True,
    )
    if tracked.returncode != 0 or untracked.returncode != 0:
        raise RuntimeError("archive diff inspection failed")
    changed = {
        value.decode("utf-8")
        for value in tracked.stdout.split(b"\0") + untracked.stdout.split(b"\0")
        if value
    }
    if not changed or any(not _archive_path_allowed(path, change=change) for path in changed):
        raise RuntimeError("archive diff escaped strict OpenSpec/docs/changelog allowlist")
    added = subprocess.run(
        ["git", "-C", str(worktree), "add", "-A", "--", *sorted(changed)],
        shell=False,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        raise RuntimeError("archive allowlist staging failed")
    staged = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--cached", "--name-only", "--no-renames", "-z"],
        shell=False,
        capture_output=True,
    )
    staged_paths = {value.decode("utf-8") for value in staged.stdout.split(b"\0") if value}
    if staged.returncode != 0 or staged_paths != changed:
        raise RuntimeError("archive staged diff differs from inspected allowlist")
    committed = subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", f"chore(openspec): archive {change}"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if committed.returncode != 0:
        raise RuntimeError("archive allowlist commit failed")
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        shell=False,
        capture_output=True,
        text=True,
    )
    new_head = head.stdout.strip().lower()
    if head.returncode != 0 or verification.SAFE_SHA_RE.fullmatch(new_head) is None or new_head == candidate:
        raise RuntimeError("archive commit did not produce a new exact Candidate")
    _push_exact_candidate(
        registry=registry,
        run=run,
        authority=authority,
        state_root=state_root,
        worktree=worktree,
        branch=branch,
        candidate=new_head,
        runner=runner,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=state_root,
        run=run,
        worktree=worktree,
        branch=branch,
        card="openspec-archive",
        old_head=candidate,
        new_head=new_head,
    )
    return registry._manager_reset_workflow_after_archive(
        run.run_id,
        candidate_head=new_head,
    )


def _authority_with_manager_pr(authority: WorkAuthority, pr_number: int) -> WorkAuthority:
    pr_ref = f"{authority.repo}#{pr_number}"
    source_revisions = set(authority.source_revisions)
    source_revisions.add(f"github_pr:{pr_ref}@identity:{pr_ref};state:open")
    return WorkAuthority._verified(
        repo=authority.repo,
        work_id=authority.work_id,
        mapped_issues=authority.mapped_issues,
        mapped_prs=(pr_number,),
        mapped_openspec=authority.mapped_openspec,
        mapped_todo_paths=authority.mapped_todo_paths,
        confirmed_todo=authority.confirmed_todo,
        auto_label=authority.auto_label,
        source_revisions=tuple(sorted(source_revisions)),
        provider_revision=authority.github_provider_revision,
        provider_id=authority.github_provider_id,
        last_success_epoch=authority.github_last_success_epoch,
        snapshot_hash=authority.snapshot_hash,
    )


def _workflow_evidence_envelope(
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
        and (
            phase not in {"verify", "review"}
            or row.get("subject_head") == run.candidate_head
        )
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
    envelope_job = envelope.get("job") if isinstance(envelope, dict) else None
    if job.get("workflow_input_snapshot") or (
        isinstance(envelope_job, dict) and "input_snapshot" in envelope_job
    ):
        expected_job["input_snapshot"] = job.get("workflow_input_snapshot", [])
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
    return envelope, job


def _workflow_evidence_payload(
    *,
    registry,
    state_root: Path,
    run,
    phase: str,
    expected_ref: str | None = None,
    expected_hash: str | None = None,
) -> tuple[dict, dict]:
    envelope, job = _workflow_evidence_envelope(
        registry=registry,
        state_root=state_root,
        run=run,
        phase=phase,
        expected_ref=expected_ref,
        expected_hash=expected_hash,
    )
    payload = envelope["payload"]
    payload = dict(payload)
    payload.pop("outputs", None)
    return payload, job


def _remove_canonical_untracked_reports(
    *,
    registry,
    state_root: Path,
    run,
    worktree: Path,
) -> None:
    """Remove only exact, canonical report publications before delivery.

    Reviewer reports are Manager-owned evidence material rather than Candidate
    tree content.  They are needed while subsequent review cards snapshot their
    inputs, but must not make the exact committed Candidate dirty at delivery.
    Unknown, tracked, symlinked, stale, or hash-drifted paths remain a hard stop.
    A hash-addressed cleanup intent makes a Manager-completed deletion replayable.
    """

    trusted: dict[str, str] = {}
    for job in registry.list_jobs():
        phase = job.get("workflow_phase")
        locator = job.get("workflow_evidence")
        if (
            job.get("workflow_run_id") != run.run_id
            or phase not in {"verify", "review"}
            or job.get("subject_head") != run.candidate_head
            or job.get("status") != "exited"
            or job.get("exit_code") != 0
            or not isinstance(locator, dict)
            or not isinstance(locator.get("path"), str)
            or not isinstance(locator.get("hash"), str)
        ):
            continue
        evidence_ref = state_root / str(locator["path"])
        envelope, _validated_job = _workflow_evidence_envelope(
            registry=registry,
            state_root=state_root,
            run=run,
            phase=str(phase),
            expected_ref=str(evidence_ref),
            expected_hash=str(locator["hash"]),
        )
        phase_root = f"reports/{'verify' if phase == 'verify' else 'review'}/"
        for artifact in envelope["artifacts"]:
            if (
                not isinstance(artifact, dict)
                or set(artifact) != {"path", "sha256", "baseline_sha256"}
                or not isinstance(artifact.get("path"), str)
                or not artifact["path"].startswith(phase_root)
                or not isinstance(artifact.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", str(artifact["sha256"])) is None
                or artifact.get("baseline_sha256") is not None
                and (
                    not isinstance(artifact.get("baseline_sha256"), str)
                    or re.fullmatch(
                        r"[0-9a-f]{64}", str(artifact["baseline_sha256"])
                    ) is None
                )
            ):
                raise RuntimeError("canonical workflow report artifact malformed")
            relative = Path(str(artifact["path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("canonical workflow report path escapes repo")
            # JobRegistry is append-ordered; a retry publication supersedes the
            # prior canonical body at the same report path.
            trusted[relative.as_posix()] = str(artifact["sha256"])

    cleanup_payload = {
        "schema": "cortex-workflow-report-cleanup/v1",
        "run_id": run.run_id,
        "candidate": run.candidate_head,
        "reports": [
            {"path": path, "sha256": sha256}
            for path, sha256 in sorted(trusted.items())
        ],
    }
    cleanup_digest = verification.canonical_json_hash(cleanup_payload)
    cleanup_path = (
        state_root.resolve() / "evidence" / "report-cleanup" / f"{cleanup_digest}.json"
    )
    cleanup_started = cleanup_path.exists() or cleanup_path.is_symlink()
    if cleanup_started and (
        cleanup_path.is_symlink()
        or not cleanup_path.is_file()
        or cleanup_path.stat().st_mode & 0o222
    ):
        raise RuntimeError("workflow report cleanup evidence is not immutable")

    removals: list[Path] = []
    for relative, expected_hash in sorted(trusted.items()):
        path = worktree / relative
        if not path.exists() and not path.is_symlink():
            if cleanup_started:
                continue
            raise RuntimeError("canonical workflow report is missing before cleanup")
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("canonical workflow report path is not a regular file")
        resolved = path.resolve(strict=True)
        try:
            resolved.relative_to(worktree)
        except ValueError as exc:
            raise RuntimeError("canonical workflow report path escapes worktree") from exc
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != expected_hash:
            raise RuntimeError("canonical workflow report hash drift")
        tracked = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--error-unmatch", "--", relative],
            shell=False,
            capture_output=True,
            text=True,
        )
        if tracked.returncode == 0:
            raise RuntimeError("canonical workflow report unexpectedly tracked")
        if tracked.returncode != 1:
            raise RuntimeError("canonical workflow report tracking state unavailable")
        removals.append(resolved)

    if trusted:
        _write_json_evidence(state_root, "report-cleanup", cleanup_payload)
    for path in removals:
        path.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _run_exact_candidate_preflight(
    *,
    worktree: Path,
    branch: str,
    candidate: str,
    command: tuple[str, ...],
    request: PreflightRequest,
    runner,
    now,
):
    """Run initial metadata preflight in a clean detached exact-Candidate checkout."""

    if verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError("initial preflight Candidate is invalid")
    branch_match = re.fullmatch(r"feature/([a-z0-9][a-z0-9-]*)", branch)
    if branch_match is None:
        raise ValueError("initial preflight delivery branch violates feature policy")
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0 or head.stdout.strip().lower() != candidate:
        raise RuntimeError("initial preflight requires exact local Candidate HEAD")
    parent = Path(tempfile.mkdtemp(prefix="cortex-preflight-"))
    checkout = parent / "candidate"
    checkout_branch = f"feature/preflight-{candidate[:12]}-{uuid4().hex[:8]}"
    added = False
    try:
        created = subprocess.run(
            [
                "git", "-C", str(worktree), "worktree", "add", "-b",
                checkout_branch, str(checkout), candidate,
            ],
            shell=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            raise RuntimeError("exact Candidate preflight checkout failed")
        added = True
        return run_preflight(
            repo_root=checkout,
            command=command,
            request=request,
            runner=runner,
            now=now,
        )
    finally:
        cleanup_error = False
        if added:
            removed = subprocess.run(
                ["git", "-C", str(worktree), "worktree", "remove", "--force", str(checkout)],
                shell=False,
                capture_output=True,
                text=True,
            )
            cleanup_error = removed.returncode != 0
            if not cleanup_error:
                deleted = subprocess.run(
                    [
                        "git", "-C", str(worktree), "update-ref", "-d",
                        f"refs/heads/{checkout_branch}", candidate,
                    ],
                    shell=False,
                    capture_output=True,
                    text=True,
                )
                cleanup_error = deleted.returncode != 0
        shutil.rmtree(parent, ignore_errors=True)
        if cleanup_error:
            raise RuntimeError("exact Candidate preflight checkout cleanup failed")


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
        maintainer = [ref for ref in run.gate_refs if ref.kind == "maintainer-review"]
        if len(maintainer) > 1 or (maintainer and maintainer[0].sha256 is None):
            raise ValueError("ship adapter maintainer-review evidence is ambiguous")
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
        _remove_canonical_untracked_reports(
            registry=registry,
            state_root=state_root,
            run=run,
            worktree=worktree,
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
            initial = _run_exact_candidate_preflight(
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                command=load_preflight_command(),
                request=PreflightRequest(metadata_path=str(metadata_path)),
                runner=runner,
                now=now,
            )
            if not initial.passed or initial.head != candidate:
                raise RuntimeError("initial PR-metadata preflight failed")
            _push_exact_candidate(
                registry=registry,
                run=run,
                authority=authority,
                state_root=state_root,
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                runner=runner,
            )
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
            _rebase_delivery_journal_authority(
                state_root=state_root,
                run=updated,
                authority=authority,
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
        _rebase_delivery_journal_authority(
            state_root=state_root,
            run=run,
            authority=authority,
        )
        def authorize_existing_pr_push() -> None:
            existing = _run_exact_candidate_preflight(
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                command=load_preflight_command(),
                request=PreflightRequest(pr_number=number),
                runner=runner,
                now=now,
            )
            if not existing.passed or existing.head != candidate:
                raise RuntimeError("existing PR exact-Candidate preflight failed")

        _push_exact_candidate(
            registry=registry,
            run=run,
            authority=authority,
            state_root=state_root,
            worktree=worktree,
            branch=branch,
            candidate=candidate,
            runner=runner,
            pre_push=authorize_existing_pr_push,
        )
        from . import work_actions
        from . import review as review_evidence

        foreign_payload, _foreign_job = _workflow_evidence_payload(
            registry=registry,
            state_root=state_root,
            run=run,
            phase="review",
            expected_ref=foreign[0].ref,
            expected_hash=foreign[0].sha256,
        )
        foreign_record = review_evidence.write_gate_evaluation(
            foreign_payload,
            coordinator_root=state_root,
        )

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
            "foreign_review_path": foreign_record["path"],
            "foreign_review_hash": foreign_record["hash"],
            "pr_metadata_path": str(metadata_path),
            "skip_tests": False,
        }
        if maintainer:
            ship_args["maintainer_review_path"] = maintainer[0].ref
            ship_args["maintainer_review_hash"] = maintainer[0].sha256
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
        if action.get("action") == "archive-applied-needs-commit":
            reset = _commit_archive_and_require_reverification(
                registry=registry,
                state_root=state_root,
                run=run,
                authority=authority,
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                runner=runner,
            )
            action = {
                "action": "candidate-reverification-required",
                "head": reset.candidate_head,
                "reason": "archive-commit-invalidated-candidate-evidence",
            }
        if action.get("action") == "done":
            _record_manager_ship_job(
                registry=registry,
                state_root=state_root,
                run=run,
                worktree=worktree,
                branch=branch,
                card="policy-commit",
                old_head=candidate,
                new_head=candidate,
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
        if maintainer:
            result.update(
                {
                    "review_kind": "maintainer-review",
                    "review_ref": maintainer[0].ref,
                    "review_hash": maintainer[0].sha256,
                }
            )
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
