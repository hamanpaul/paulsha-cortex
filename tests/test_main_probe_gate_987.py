"""#987 RED regressions for the main-probe ship gate.

The accepted plan says review-complete ship ticks must probe ``origin/main``
before any preflight, push, or PR creation. These tests reproduce the current
gap with a local bare origin so the later GREEN card has a concrete failure to
turn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.model_identities import IdentityRegistry

from test_preflight_closeout_order import ShipHarness, _capture, _ship_harness


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _wire_local_origin(harness: ShipHarness, root: Path) -> Path:
    bare = root / "origin.git"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", "main", str(bare)],
        check=True,
    )
    if _git(harness.repo, "branch", "--show-current") != "main":
        _git(harness.repo, "branch", "-M", "main")
    _git(harness.repo, "remote", "remove", "origin")
    _git(harness.repo, "remote", "add", "origin", str(bare))
    _git(harness.repo, "push", "--quiet", "-u", "origin", "main")
    _git(harness.repo, "push", "--quiet", "origin", "feature/14-work")
    return bare


def _advance_origin_main(origin: Path, checkout: Path) -> str:
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "config", "user.name", "Test")
    (checkout / "UPSTREAM.md").write_text("origin moved\n", encoding="utf-8")
    _git(checkout, "add", "UPSTREAM.md")
    _git(checkout, "commit", "-qm", "advance origin main")
    _git(checkout, "push", "--quiet", "origin", "main")
    return _git(checkout, "rev-parse", "HEAD")


def test_ship_validator_blocks_clean_but_behind_candidate_before_preflight_or_pr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin-fixture")
    remote_main = _advance_origin_main(origin, tmp_path / "origin-advance")
    assert remote_main != harness.candidate

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["status"] == "needs_human"
    assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()
    assert harness.registry.get_workflow_run(harness.run_id).pr_refs == ()


def test_resume_stops_review_complete_run_when_origin_main_fetch_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
    )
    if _git(harness.repo, "branch", "--show-current") != "main":
        _git(harness.repo, "branch", "-M", "main")
    _git(harness.repo, "remote", "remove", "origin")
    _git(harness.repo, "remote", "add", "origin", str(tmp_path / "missing-origin.git"))

    result = manager.resume_workflow_run(
        SimpleNamespace(_registry=harness.registry, _git_runner=None),
        run_id=harness.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=harness.state_root,
        ship_validator=harness.validator,
        operator_resume=True,
    )

    assert result["reason"] == "main-sync-unavailable"
    assert harness.registry.get_workflow_run(harness.run_id).facets == ("needs_human",)
    assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()
