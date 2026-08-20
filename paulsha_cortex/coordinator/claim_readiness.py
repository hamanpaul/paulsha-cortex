"""Pre-claim readiness transaction (issue #211, design #208 A.2).

This module is the six-gate readiness *transaction* that must pass, in full,
before the Manager is allowed to create a workflow job, a builder worktree,
or a model session for a claim. It is deliberately separate from
``coordinator/preflight.py`` (a build-time CI-parity tool that runs the repo's
own policy/test gates) and from ``claim.py`` (pure, I/O-free claim policy):
several of the six checks require real I/O (git fetch, GitHub API, a live
model probe), which ``claim.py``'s docstring explicitly disallows.

Design (issue #208 A.2) — checks run in *cost order*, cheapest first, so an
expensive check is never paid for when a cheap one would already have failed:

1. ``local_scope``     — heading / OpenSpec strict / changelog-scope satisfiability (pure local string)
2. ``base_sha``        — local base SHA vs. fetched remote main (one fetch)
3. ``monitor_snapshot`` — monitor snapshot revision + single-owner invariant (local state)
4. ``github_owner``    — GitHub issue owner/link consistency (network, rate-limited)
5. ``capability``      — ``capable()`` predicate table lookup (config; #209 not yet landed,
                          so an absent lookup is an observability *bypass*, not a failure)
6. ``live_probe``      — an actual live probe session (e.g. ``agy-plan-sandbox``); always last,
                          and TTL-cached so a burst of claim attempts does not repeatedly pay for it.

The transaction's output is a **frozen set** of SHA/hash values (never a bare
boolean): once ``evaluate_pre_claim_readiness`` returns ready, the returned
``FrozenReadinessSet`` is what dispatch must carry forward — in particular the
frozen ``base_sha``, which a builder worktree must be created from instead of
whatever ``main`` happens to be at the moment the worktree is actually built.
This directly closes the stale-base class of defect described in hippo #18
(#2) and #41 (v2): readiness freezes the base once, and nothing downstream
may silently re-derive a fresher (or staler) one.

Failure classification is not uniform. Most failures are retryable (heading
gap, stale base, an owner transfer still in flight); exactly one is terminal
and must never be retried: a *policy scope contract conflict*, where the
work item's own frozen scope forbids something the repo's policy requires
(the literal cause of death in hippo #18 point 9 — PR-aware R-09 requires a
changelog fragment while the frozen scope explicitly forbade one). Terminal
failures resolve to ``needs_human``; retryable failures resolve to Manager's
existing ``blocked`` vocabulary so no new action name has to be learned by
downstream consumers (CLI, cockpit).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from . import verification
from .claim import WorkAuthority, work_authority_digest
from ..project_policy import ProjectPolicyError, read_repo_tier

FROZEN_READINESS_SET_SCHEMA = "pre-claim-readiness-frozen-set/v1"
LIVE_PROBE_DEFAULT_TTL_SECONDS = 300.0
CHECK_ORDER = (
    "local_scope",
    "base_sha",
    "monitor_snapshot",
    "github_owner",
    "capability",
    "live_probe",
)

GitRunner = verification.GitRunner


@dataclass(frozen=True)
class ReadinessCheckResult:
    """One check's verdict. ``terminal`` is only meaningful when ``passed`` is False."""

    name: str
    passed: bool
    reason: str | None = None
    terminal: bool = False
    observation: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in CHECK_ORDER:
            raise ValueError(f"unknown pre-claim readiness check: {self.name!r}")
        if self.passed and (self.reason is not None or self.terminal):
            raise ValueError("a passed readiness check must not carry reason/terminal")
        if not self.passed and not self.reason:
            raise ValueError("a failed readiness check requires a reason")


def _passed(name: str, **observation: object) -> ReadinessCheckResult:
    return ReadinessCheckResult(name=name, passed=True, observation=observation)


def _failed(name: str, reason: str, *, terminal: bool = False) -> ReadinessCheckResult:
    return ReadinessCheckResult(name=name, passed=False, reason=reason, terminal=terminal)


@dataclass(frozen=True)
class FrozenReadinessSet:
    """The serializable freeze a dispatch must carry instead of re-deriving state.

    ``base_sha`` is the exact value a builder worktree must be created from;
    ``planning_authority_hashes`` pins the WorkAuthority digest(s) readiness
    was evaluated against so a later authority change is detectable rather
    than silently reused.
    """

    schema: str
    repo: str
    work_id: str
    base_sha: str
    planning_authority_hashes: tuple[str, ...]
    monitor_snapshot_revision: str
    issue_ref: str | None
    executor_identity: str
    frozen_at_epoch: float
    live_probe_ttl_cached: bool

    def __post_init__(self) -> None:
        if self.schema != FROZEN_READINESS_SET_SCHEMA:
            raise ValueError("frozen readiness set schema invalid")
        if not isinstance(self.base_sha, str) or verification.SAFE_SHA_RE.fullmatch(self.base_sha) is None:
            raise ValueError("frozen readiness set base_sha invalid")
        if not self.planning_authority_hashes or any(
            not isinstance(value, str) or not value for value in self.planning_authority_hashes
        ):
            raise ValueError("frozen readiness set planning_authority_hashes invalid")
        if not isinstance(self.monitor_snapshot_revision, str) or not self.monitor_snapshot_revision:
            raise ValueError("frozen readiness set monitor_snapshot_revision invalid")
        if not isinstance(self.executor_identity, str) or not self.executor_identity:
            raise ValueError("frozen readiness set executor_identity invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "repo": self.repo,
            "work_id": self.work_id,
            "base_sha": self.base_sha,
            "planning_authority_hashes": list(self.planning_authority_hashes),
            "monitor_snapshot_revision": self.monitor_snapshot_revision,
            "issue_ref": self.issue_ref,
            "executor_identity": self.executor_identity,
            "frozen_at_epoch": self.frozen_at_epoch,
            "live_probe_ttl_cached": self.live_probe_ttl_cached,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "FrozenReadinessSet":
        if not isinstance(payload, Mapping) or payload.get("schema") != FROZEN_READINESS_SET_SCHEMA:
            raise ValueError("frozen readiness set schema invalid")
        hashes = payload.get("planning_authority_hashes")
        if not isinstance(hashes, list):
            raise ValueError("frozen readiness set planning_authority_hashes must be a list")
        issue_ref = payload.get("issue_ref")
        if issue_ref is not None and not isinstance(issue_ref, str):
            raise ValueError("frozen readiness set issue_ref invalid")
        frozen_at = payload.get("frozen_at_epoch")
        if not isinstance(frozen_at, (int, float)) or isinstance(frozen_at, bool):
            raise ValueError("frozen readiness set frozen_at_epoch invalid")
        cached = payload.get("live_probe_ttl_cached")
        if not isinstance(cached, bool):
            raise ValueError("frozen readiness set live_probe_ttl_cached invalid")
        return cls(
            schema=payload.get("schema"),
            repo=payload.get("repo"),
            work_id=payload.get("work_id"),
            base_sha=payload.get("base_sha"),
            planning_authority_hashes=tuple(hashes),
            monitor_snapshot_revision=payload.get("monitor_snapshot_revision"),
            issue_ref=issue_ref,
            executor_identity=payload.get("executor_identity"),
            frozen_at_epoch=float(frozen_at),
            live_probe_ttl_cached=cached,
        )


@dataclass(frozen=True)
class ReadinessContext:
    """The minimal per-claim identity every probe closure is evaluated against."""

    authority: WorkAuthority
    executor_identity: str
    issue_ref: str | None = None


@dataclass(frozen=True)
class ReadinessOutcome:
    """Result of running the full six-check transaction once."""

    ready: bool
    frozen: FrozenReadinessSet | None
    failed_check: str | None
    reason: str | None
    terminal: bool
    checks_run: tuple[str, ...]


ReadinessProbe = Callable[[ReadinessContext], ReadinessCheckResult]


@dataclass(frozen=True)
class ReadinessProbes:
    """The six #208 A.2 checks, bound to concrete callables, already cost-ordered."""

    local_scope: ReadinessProbe
    base_sha: ReadinessProbe
    monitor_snapshot: ReadinessProbe
    github_owner: ReadinessProbe
    capability: ReadinessProbe
    live_probe: ReadinessProbe

    def ordered(self) -> tuple[tuple[str, ReadinessProbe], ...]:
        return tuple((name, getattr(self, name)) for name in CHECK_ORDER)


@dataclass
class LiveProbeCache:
    """TTL cache in front of the live probe — the one check that really starts a session."""

    ttl_seconds: float = LIVE_PROBE_DEFAULT_TTL_SECONDS
    _entries: dict[str, tuple[ReadinessCheckResult, float]] = field(default_factory=dict)

    def peek(self, key: str, *, now: float) -> ReadinessCheckResult | None:
        cached = self._entries.get(key)
        if cached is None:
            return None
        result, expiry = cached
        if now >= expiry:
            del self._entries[key]
            return None
        return result

    def store(self, key: str, result: ReadinessCheckResult, *, now: float) -> None:
        self._entries[key] = (result, now + self.ttl_seconds)


def evaluate_pre_claim_readiness(
    context: ReadinessContext,
    probes: ReadinessProbes,
    *,
    live_probe_cache: LiveProbeCache | None = None,
    now: Callable[[], float] = time.time,
) -> ReadinessOutcome:
    """Run the six checks in cost order, short-circuiting on the first failure.

    On success the return carries a :class:`FrozenReadinessSet`, never a bare
    boolean. On failure the return carries which check failed, its reason,
    and whether the failure is terminal (``needs_human``, never retried) or
    retryable — the caller must not build a ``ClaimCandidate`` or start a
    workflow either way.
    """

    cache = live_probe_cache if live_probe_cache is not None else LiveProbeCache()
    checks_run: list[str] = []
    observations: dict[str, Mapping[str, object]] = {}
    live_probe_cached = False
    for name, probe in probes.ordered():
        checks_run.append(name)
        if name == "live_probe":
            now_epoch = now()
            cache_key = f"{context.authority.repo}:{context.authority.work_id}:{context.executor_identity}"
            cached_result = cache.peek(cache_key, now=now_epoch)
            if cached_result is not None:
                result = cached_result
                live_probe_cached = True
            else:
                result = probe(context)
                cache.store(cache_key, result, now=now_epoch)
        else:
            result = probe(context)
        if not result.passed:
            return ReadinessOutcome(
                ready=False,
                frozen=None,
                failed_check=name,
                reason=result.reason,
                terminal=result.terminal,
                checks_run=tuple(checks_run),
            )
        observations[name] = result.observation

    frozen = FrozenReadinessSet(
        schema=FROZEN_READINESS_SET_SCHEMA,
        repo=context.authority.repo,
        work_id=context.authority.work_id,
        base_sha=str(observations["base_sha"]["base_sha"]),
        planning_authority_hashes=(work_authority_digest(context.authority),),
        monitor_snapshot_revision=str(observations["monitor_snapshot"]["snapshot_revision"]),
        issue_ref=context.issue_ref,
        executor_identity=context.executor_identity,
        frozen_at_epoch=now(),
        live_probe_ttl_cached=live_probe_cached,
    )
    return ReadinessOutcome(
        ready=True,
        frozen=frozen,
        failed_check=None,
        reason=None,
        terminal=False,
        checks_run=tuple(checks_run),
    )


# --------------------------------------------------------------------------
# Default probe factories. Each returns a closure bound to its own concrete
# dependencies (git runner, GitHub CLI runner, capability table, live
# prober, ...), mirroring the ``workflow_starter``-style injectable-callable
# convention already used throughout this package.
# --------------------------------------------------------------------------


def local_scope_probe(
    *,
    repo_root: str | Path | None = None,
    heading_ok: bool = True,
    openspec_strict_ok: bool = True,
    changelog_required: bool = True,
    changelog_forbidden: bool = False,
) -> ReadinessProbe:
    """Pure local-string check: heading presence, OpenSpec strict validity, and
    whether the work item's own frozen scope contradicts a policy requirement
    (e.g. R-09 changelog vs. an explicit no-changelog scope — hippo #18 #9).
    The contradiction case is the sole *terminal* failure in the whole
    transaction; everything else here is a retryable gap.
    """

    def _probe(context: ReadinessContext) -> ReadinessCheckResult:
        if repo_root is not None:
            try:
                read_repo_tier(repo_root)
            except ProjectPolicyError as exc:
                return _failed("local_scope", f"foreign-review-tier:{exc}")
        if not heading_ok:
            return _failed("local_scope", "heading-gap")
        if not openspec_strict_ok:
            return _failed("local_scope", "openspec-strict-failed")
        if changelog_required and changelog_forbidden:
            return _failed("local_scope", "policy-scope-conflict", terminal=True)
        return _passed("local_scope")

    return _probe


def base_sha_probe(
    *,
    repo_root: str | Path,
    remote: str = "origin",
    branch: str = "main",
    local_known_base_sha: str | None = None,
    git_runner: GitRunner | None = None,
) -> ReadinessProbe:
    """Fetch remote main once and freeze it as ``base_sha``.

    If the caller already believed a different SHA was current (a base it
    would otherwise have built a worktree from), that mismatch is the
    stale-base defect from hippo #18 (#2) / #41 (v2): retryable, never
    silently accepted.
    """

    def _probe(context: ReadinessContext) -> ReadinessCheckResult:
        fetch = verification._run_git(
            ["-C", str(repo_root), "fetch", "--no-tags", remote, branch],
            git_runner,
        )
        if fetch["status"] != "ok":
            return _failed("base_sha", "base-sha-fetch-failed")
        ref = verification._run_git(
            ["-C", str(repo_root), "rev-parse", f"refs/remotes/{remote}/{branch}"],
            git_runner,
        )
        sha = ref["stdout"].strip().lower()
        if ref["status"] != "ok" or verification.SAFE_SHA_RE.fullmatch(sha) is None:
            return _failed("base_sha", "base-sha-unreadable")
        if local_known_base_sha is not None and local_known_base_sha.strip().lower() != sha:
            return _failed("base_sha", "stale-base")
        return _passed("base_sha", base_sha=sha)

    return _probe


def monitor_snapshot_probe(*, owner_count: int = 1) -> ReadinessProbe:
    """Freeze the already-loaded monitor snapshot revision and require a single owner.

    ``authority.snapshot_hash`` was already computed when the durable Monitor
    snapshot was loaded, so this check pays no extra I/O of its own — only
    the owner-count invariant is caller-supplied (the caller already knows
    how many active runs claim the same identity).
    """

    def _probe(context: ReadinessContext) -> ReadinessCheckResult:
        if owner_count != 1:
            return _failed("monitor_snapshot", "owner-transfer-incomplete")
        return _passed("monitor_snapshot", snapshot_revision=context.authority.snapshot_hash)

    return _probe


def github_owner_probe(
    *,
    runner: Callable[..., object] = subprocess.run,
    timeout_seconds: int = 20,
) -> ReadinessProbe:
    """Verify the confirmed GitHub issue is still open before claiming it."""

    def _probe(context: ReadinessContext) -> ReadinessCheckResult:
        if context.issue_ref is None:
            return _passed("github_owner", checked=False)
        completed = runner(
            ["gh", "api", f"repos/{context.authority.repo}/issues/{context.issue_ref}"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if getattr(completed, "returncode", None) != 0:
            return _failed("github_owner", "github-owner-link-unreadable")
        try:
            payload = json.loads(getattr(completed, "stdout", ""))
            state = payload["state"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return _failed("github_owner", "github-owner-link-malformed")
        if not isinstance(state, str) or state.lower() != "open":
            return _failed("github_owner", "github-owner-link-mismatch")
        return _passed("github_owner", issue_state=state)

    return _probe


def capability_probe(
    *, capability_lookup: Callable[[str], bool | None] | None = None,
) -> ReadinessProbe:
    """``capable()`` predicate table lookup (#209).

    #209's routing matrix has not landed, so an absent lookup table is a
    pluggable, observability-flagged *bypass* rather than a hard failure —
    per the #208 A.2 design note for this exact check.
    """

    def _probe(context: ReadinessContext) -> ReadinessCheckResult:
        if capability_lookup is None:
            return _passed("capability", bypass="envelope_unavailable")
        state = capability_lookup(context.executor_identity)
        if state is False:
            return _failed("capability", "capability-insufficient")
        return _passed("capability", bypass=None if state is True else "envelope_unavailable")

    return _probe


def live_probe_check(*, prober: Callable[[], object] | None = None) -> ReadinessProbe:
    """The one check that really starts a session (default: ``agy-plan-sandbox``).

    Must run last (enforced by :data:`CHECK_ORDER`) and is wrapped in a TTL
    cache by :func:`evaluate_pre_claim_readiness`, not by this probe itself,
    so the cache is visible to callers via ``ReadinessOutcome``/frozen-set
    provenance instead of being a private implementation detail.
    """

    def _probe(context: ReadinessContext) -> ReadinessCheckResult:
        run = prober
        if run is None:
            from .model_identities import probe_agy_capability

            run = probe_agy_capability
        outcome = run()
        ready = bool(getattr(outcome, "ready", False))
        if not ready:
            reason = getattr(outcome, "reason", None) or "live-probe-failed"
            return _failed("live_probe", str(reason))
        return _passed("live_probe")

    return _probe
