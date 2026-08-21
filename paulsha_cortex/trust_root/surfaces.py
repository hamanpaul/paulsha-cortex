"""Typed runtime surface contract shared by generation and provisioning.

The per-job rows are trust-root data, not coordinator implementation detail.
Keeping them in this dependency-light module means the permission generator,
slot provisioner, launcher metadata, and probes all consume the same rows
without making the generator import a coordinator-owned second truth.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from pathlib import Path


@dataclass(frozen=True)
class PerJobWritableSurface:
    """One row wired into path lookup, unit generation and runtime consumers."""

    surface_id: str
    path_accessor: str
    coordinator_relative: str
    provisioner: str
    consumer: str
    probe: str
    principals: tuple[str, ...]
    asset_id: str
    # ACL recipes are part of the same typed row as the path and consumer.
    slot_access_perms: str = "rwX"
    slot_read_perms: str = "r-X"

    def acl_perms(self, *, writable: bool) -> str:
        return self.slot_access_perms if writable else self.slot_read_perms

    def acl_argument(self, *, account: str, writable: bool, directory: bool) -> str:
        """Return the complete access/default ACL recipe for this row."""

        perms = self.acl_perms(writable=writable)
        spec = f"u:{account}:{perms}"
        if directory:
            spec += f",d:u:{account}:{perms}"
        return spec

    @property
    def writable_root(self) -> str:
        from ..config import paths

        if self.surface_id.endswith("-codex-home"):
            return str(paths.agents_root() / "runtime" / "codex-home" / self.principals[0])
        if self.surface_id.endswith("-runtime-cache"):
            return str(paths.agents_root() / "runtime" / "job-cache" / self.principals[0])
        accessor = getattr(paths, self.path_accessor)
        if self.surface_id.endswith(("-job-log", "-codex-home", "-runtime-cache")):
            return str(accessor(self.principals[0]))
        return str(accessor())

    @property
    def slot_template(self) -> str:
        return f"{self.writable_root}/%i"

    def root_for(self, layout) -> str:
        """Resolve this row against the generator's deployment layout."""

        if self.surface_id == "monitor-event-spool":
            return f"{layout.agents_root}/monitor/event-spool"
        if self.surface_id.endswith("-job-log"):
            from .registry import Principal

            return layout.job_log_spool_root(Principal(self.principals[0]))
        if self.surface_id.endswith("-codex-home"):
            return f"{layout.agents_root}/runtime/codex-home/{self.principals[0]}"
        if self.surface_id.endswith("-runtime-cache"):
            return f"{layout.agents_root}/runtime/job-cache/{self.principals[0]}"
        accessor = getattr(layout, self.path_accessor, None)
        if accessor is None:
            raise ValueError(f"surface {self.surface_id!r} has no layout accessor")
        return str(accessor() if callable(accessor) else accessor)


# This is the sole runtime writable-surface table.  Permission generation and
# provisioning intentionally import it from here; do not add a second table
# to either consumer.
PER_JOB_WRITABLE_SURFACES: tuple[PerJobWritableSurface, ...] = (
    PerJobWritableSurface("commit-spool", "commit_spool_root", "commit-spool", "create_slot", "commit_bundle_path", "render_job_writable_properties", ("builder",), "commit-spool"),
    PerJobWritableSurface("monitor-event-spool", "monitor_event_spool_root", "monitor/event-spool", "create_slot", "EventSpool", "render_job_writable_properties", ("builder",), "monitor-event-spool"),
    PerJobWritableSurface("review-verdict-spool", "review_verdict_spool_root", "review-verdicts", "create_slot", "review_verdict_spool_path", "render_job_writable_properties", ("reviewer",), "review-verdict-spool"),
    PerJobWritableSurface("gate-ledger-spool", "gate_ledger_spool_root", "gate-ledger-spool", "create_slot", "gate_spool_ledger_path", "render_job_writable_properties", ("gate",), "gate-ledger-spool"),
    PerJobWritableSurface("gate-worktree", "gate_worktree_root", "gate-worktree", "create_slot", "gate_worktree_dir", "render_job_writable_properties", ("gate",), "gate-worktree-pool"),
    PerJobWritableSurface("builder-job-log", "job_log_spool_root", "commit-spool/build-logs", "prepare_job_log", "prepare_job_log_spool", "build_job_log_probe", ("builder",), "build-job-log-spool"),
    PerJobWritableSurface("reviewer-job-log", "job_log_spool_root", "review-verdicts/planning-logs", "prepare_job_log", "PlanningJobInvoker", "build_job_log_probe", ("reviewer",), "planning-job-log-spool"),
    PerJobWritableSurface("gate-job-log", "job_log_spool_root", "gate-ledger-spool/gate-logs", "prepare_job_log", "prepare_gate_job_log", "build_job_log_probe", ("gate",), "gate-job-log-spool"),
    PerJobWritableSurface("builder-codex-home", "builder_job_codex_home_root", "runtime/codex-home/builder", "create_slot", "build_job_env", "codex_runtime_probe", ("builder",), "builder-job-codex-home-root"),
    PerJobWritableSurface("reviewer-codex-home", "reviewer_job_codex_home_root", "runtime/codex-home/reviewer", "create_slot", "build_job_env", "codex_runtime_probe", ("reviewer",), "reviewer-job-codex-home-root"),
    PerJobWritableSurface("builder-runtime-cache", "builder_job_cache_root", "runtime/job-cache/builder", "create_slot", "build_job_env", "codex_runtime_probe", ("builder",), "builder-job-cache-root"),
    PerJobWritableSurface("reviewer-runtime-cache", "reviewer_job_cache_root", "runtime/job-cache/reviewer", "create_slot", "build_job_env", "codex_runtime_probe", ("reviewer",), "reviewer-job-cache-root"),
)


def writable_surface(surface_id: str) -> PerJobWritableSurface:
    try:
        return next(row for row in PER_JOB_WRITABLE_SURFACES if row.surface_id == surface_id)
    except StopIteration as exc:
        raise ValueError(f"unknown writable surface: {surface_id!r}") from exc


def codex_runtime_surface(
    *, principal: str, surface_id: str
) -> PerJobWritableSurface:
    """Resolve a Codex harvest row from typed metadata, never from job kind."""

    row = writable_surface(surface_id)
    if not row.surface_id.endswith("-codex-home") or principal not in row.principals:
        raise ValueError(
            f"surface {surface_id!r} is not the Codex home for principal {principal!r}"
        )
    return row


def credential_publisher_command(*, manager_account: str, codex_home: str = "$CODEX_HOME") -> str:
    """Render the shared owner-aware auth publisher from Codex surface rows."""

    if not manager_account or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_.-]*", manager_account
    ):
        raise ValueError(f"unsafe manager account: {manager_account!r}")
    codex_rows = tuple(
        row for row in PER_JOB_WRITABLE_SURFACES if row.surface_id.endswith("-codex-home")
    )
    if not codex_rows or any(
        row.surface_id.split("-", 1)[0] not in row.principals for row in codex_rows
    ):
        raise ValueError("Codex runtime surface table has no valid publisher rows")
    auth_leaf = "auth.json"
    auth = (
        f"{codex_home}/{auth_leaf}"
        if codex_home == "$CODEX_HOME"
        else str(Path(codex_home) / auth_leaf)
    )
    quoted = (
        '"$CODEX_HOME/auth.json"'
        if codex_home == "$CODEX_HOME"
        else shlex.quote(auth)
    )
    return (
        f"if test -f {quoted} && test ! -L {quoted} && test -O {quoted}; then "
        f"chmod 0640 {quoted} && "
        f"setfacl -m u:{manager_account}:r--,m::r-- {quoted}; fi"
    )
