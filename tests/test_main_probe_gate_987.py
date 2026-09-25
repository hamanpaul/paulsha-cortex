"""#987 main-probe gate regressions.

The accepted plan says review-complete ship ticks must probe ``origin/main``
before any preflight, push, or PR creation. These tests cover the direct
bounded probe, the Manager needs-human wrapper, durable probe evidence, and the
local bare-origin delivery path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, work_bridge
from paulsha_cortex.coordinator.model_identities import IdentityRegistry

from test_preflight_closeout_order import (
    ShipHarness,
    _capture,
    _seed_foreign_review,
    _ship_harness,
)


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


def _read_probe_evidence(ref: str) -> dict[str, object]:
    envelope = json.loads(Path(ref).read_text(encoding="utf-8"))
    assert envelope["hash"] == work_bridge.verification.canonical_json_hash(
        envelope["payload"]
    )
    return envelope["payload"]


def _main_sync_context(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: payload.get(key)
        for key in ("candidate", "stage", "returncode", "error_kind", "main_head")
    }


def _probe_result(
    returncode: int,
    *,
    stdout: bytes | str = b"",
    stderr: bytes | str = b"",
):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _SequencedProbeRunner:
    def __init__(self, outcomes: list[object]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append([str(value) for value in argv])
        if not self._outcomes:
            raise AssertionError(f"unexpected probe argv: {argv!r}")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _RecordingProbeRunner:
    def __init__(self) -> None:
        self.fetch_returncodes: list[int] = []

    def __call__(self, argv, **kwargs):
        result = subprocess.run(argv, **kwargs)
        command = [str(value) for value in argv]
        if command[3:] == ["fetch", "--quiet", "--no-tags", "origin", "main"]:
            self.fetch_returncodes.append(int(getattr(result, "returncode", 1)))
        return result


def _probe_success_prefix(
    *, candidate: str, main_head: str, merge_base: str
) -> list[object]:
    return [
        _probe_result(0, stdout=f"{candidate}\n"),
        _probe_result(0, stdout="commit\n"),
        _probe_result(0),
        _probe_result(0, stdout=f"{main_head}\n"),
        _probe_result(0, stdout="commit\n"),
        _probe_result(0, stdout=f"{merge_base}\n"),
    ]


def _init_probe_repo(root: Path, *, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def _wire_origin(repo: Path, bare: Path) -> Path:
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "--quiet", "-u", "origin", "main")
    return bare


def _advance_origin_with(
    origin: Path,
    checkout: Path,
    *,
    files: dict[str, str],
    message: str,
) -> str:
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "config", "user.name", "Test")
    for relative, content in files.items():
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", message)
    _git(checkout, "push", "--quiet", "origin", "main")
    return _git(checkout, "rev-parse", "HEAD")


def _resume(harness: ShipHarness, *, validator=None) -> dict[str, object]:
    return manager.resume_workflow_run(
        SimpleNamespace(_registry=harness.registry, _git_runner=None),
        run_id=harness.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: None,
        coordinator_root=harness.state_root,
        ship_validator=validator or harness.validator,
        operator_resume=True,
    )


def test_main_sync_probe_skips_terminal_refresh_and_merged_closure(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    candidate = "a" * 40
    journal = {
        "schema": "cortex-delivery-journal/v1",
        "runs": {
            "run-review": {
                "run_id": "run-review",
                "repo": "acme/demo",
                "work_id": "work",
                "ship": {
                    "phase": "merged",
                    "head": candidate,
                    "merge_commit": "b" * 40,
                },
            }
        },
    }
    (state_root / "delivery-journal.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )

    assert work_bridge._should_probe_main_sync(
        run=SimpleNamespace(
            run_id="run-review",
            current_phase="review",
            status="ongoing",
        ),
        state_root=state_root,
        candidate=candidate,
    ) is False
    assert work_bridge._should_probe_main_sync(
        run=SimpleNamespace(
            run_id="run-ship",
            current_phase="ship",
            status="done",
        ),
        state_root=state_root,
        candidate=candidate,
    ) is False


def test_main_sync_probe_rejects_abbreviated_candidate_sha(tmp_path: Path) -> None:
    resolved = "a" * 40
    probe = work_bridge._probe_main_sync(
        worktree=tmp_path,
        candidate="a" * 12,
        runner=_SequencedProbeRunner(
            [
                _probe_result(0, stdout=f"{resolved}\n"),
                _probe_result(0, stdout="commit\n"),
            ]
        ),
        timeout_seconds=0.1,
    )

    assert isinstance(probe, work_bridge.MainSyncProbeFailure)
    assert probe.stage == "candidate-validate"
    assert probe.error_kind == "bad-sha"
    assert probe.returncode == 0
    assert probe.main_head is None


def test_main_sync_probe_rejects_non_commit_candidate_object(tmp_path: Path) -> None:
    repo = _init_probe_repo(tmp_path / "repo", files={"README.md": "demo\n"})
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input="blob object\n",
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()

    probe = work_bridge._probe_main_sync(
        worktree=repo,
        candidate=blob,
        timeout_seconds=0.1,
    )

    assert isinstance(probe, work_bridge.MainSyncProbeFailure)
    assert probe.stage == "candidate-validate"
    assert probe.error_kind == "non-commit"
    assert probe.main_head is None


def test_main_sync_probe_records_fetch_head_resolve_failure(tmp_path: Path) -> None:
    candidate = "a" * 40
    probe = work_bridge._probe_main_sync(
        worktree=tmp_path,
        candidate=candidate,
        runner=_SequencedProbeRunner(
            [
                _probe_result(0, stdout=f"{candidate}\n"),
                _probe_result(0, stdout="commit\n"),
                _probe_result(0),
                _probe_result(128, stderr="fatal: FETCH_HEAD missing\n"),
            ]
        ),
        timeout_seconds=0.1,
    )

    assert isinstance(probe, work_bridge.MainSyncProbeFailure)
    assert probe.stage == "fetch-head-resolve"
    assert probe.error_kind == "command-failed"
    assert probe.main_head is None


def test_main_sync_probe_rejects_non_commit_fetch_head_object(tmp_path: Path) -> None:
    candidate = "a" * 40
    main_head = "b" * 40
    probe = work_bridge._probe_main_sync(
        worktree=tmp_path,
        candidate=candidate,
        runner=_SequencedProbeRunner(
            [
                _probe_result(0, stdout=f"{candidate}\n"),
                _probe_result(0, stdout="commit\n"),
                _probe_result(0),
                _probe_result(0, stdout=f"{main_head}\n"),
                _probe_result(0, stdout="blob\n"),
            ]
        ),
        timeout_seconds=0.1,
    )

    assert isinstance(probe, work_bridge.MainSyncProbeFailure)
    assert probe.stage == "fetch-head-validate"
    assert probe.error_kind == "non-commit"
    assert probe.main_head is None


@pytest.mark.parametrize(
    ("stage", "outcome", "expected_error_kind", "expected_returncode"),
    [
        (
            "merge-base",
            _probe_result(128, stderr="fatal: merge-base failed\n"),
            "command-failed",
            128,
        ),
        (
            "merge-tree",
            _probe_result(129, stderr="error: unknown option `--name-only`\n"),
            "unsupported-option",
            129,
        ),
        (
            "path-parser",
            _probe_result(1, stdout="not-a-tree\0README.md\0"),
            "output-malformed",
            1,
        ),
        (
            "merge-tree",
            subprocess.TimeoutExpired(["git", "merge-tree"], 0.1),
            "timeout",
            None,
        ),
    ],
)
def test_main_sync_probe_preserves_main_head_for_post_fetch_failures(
    tmp_path: Path,
    stage: str,
    outcome: object,
    expected_error_kind: str,
    expected_returncode: int | None,
) -> None:
    candidate = "a" * 40
    main_head = "b" * 40
    merge_base = "c" * 40
    outcomes = _probe_success_prefix(
        candidate=candidate,
        main_head=main_head,
        merge_base=merge_base,
    )
    if stage == "merge-base":
        outcomes[-1] = outcome
    else:
        outcomes.append(outcome)
    probe = work_bridge._probe_main_sync(
        worktree=tmp_path,
        candidate=candidate,
        runner=_SequencedProbeRunner(outcomes),
        timeout_seconds=0.1,
    )

    assert isinstance(probe, work_bridge.MainSyncProbeFailure)
    assert probe.stage == stage
    assert probe.error_kind == expected_error_kind
    assert probe.returncode == expected_returncode
    assert probe.main_head == main_head


def test_main_sync_probe_detects_clean_behind_with_real_bare_origin(
    tmp_path: Path,
) -> None:
    repo = _init_probe_repo(
        tmp_path / "repo",
        files={"README.md": "base\n", "CHANGELOG.md": "## [Unreleased]\n\n- base\n"},
    )
    origin = _wire_origin(repo, tmp_path / "origin" / "origin.git")
    _git(repo, "checkout", "-qb", "feature/probe")
    (repo / "FEATURE.md").write_text("feature only\n", encoding="utf-8")
    _git(repo, "add", "FEATURE.md")
    _git(repo, "commit", "-qm", "feature commit")
    candidate = _git(repo, "rev-parse", "HEAD")
    remote_main = _advance_origin_with(
        origin,
        tmp_path / "origin-advance",
        files={"UPSTREAM.md": "upstream\n"},
        message="advance main",
    )

    probe = work_bridge._probe_main_sync(
        worktree=repo,
        candidate=candidate,
        timeout_seconds=1.0,
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "clean-behind"
    assert probe.main_head == remote_main
    assert probe.conflict_paths == ()
    assert probe.path_classification is None


def test_main_sync_probe_classifies_changelog_top_insert_conflict(tmp_path: Path) -> None:
    repo = _init_probe_repo(
        tmp_path / "repo",
        files={
            "CHANGELOG.md": "## [Unreleased]\n\n- base\n\n## [0.1.0]\n\n- shipped\n",
        },
    )
    origin = _wire_origin(repo, tmp_path / "origin" / "origin.git")
    _git(repo, "checkout", "-qb", "feature/probe")
    (repo / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n- feature entry\n- base\n\n## [0.1.0]\n\n- shipped\n",
        encoding="utf-8",
    )
    _git(repo, "add", "CHANGELOG.md")
    _git(repo, "commit", "-qm", "feature changelog")
    candidate = _git(repo, "rev-parse", "HEAD")
    _advance_origin_with(
        origin,
        tmp_path / "origin-changelog",
        files={
            "CHANGELOG.md": "## [Unreleased]\n\n- main entry\n- base\n\n## [0.1.0]\n\n- shipped\n"
        },
        message="main changelog",
    )

    probe = work_bridge._probe_main_sync(
        worktree=repo,
        candidate=candidate,
        timeout_seconds=1.0,
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "conflict"
    assert probe.conflict_paths == ("CHANGELOG.md",)
    assert probe.path_classification == "changelog-top-insert"


def test_main_sync_probe_parses_multiple_conflict_paths_with_spaces_and_newlines(
    tmp_path: Path,
) -> None:
    newline_name = "docs/line\nbreak.txt"
    spaced_name = "docs/space name.txt"
    repo = _init_probe_repo(
        tmp_path / "repo",
        files={
            newline_name: "base\n",
            spaced_name: "base\n",
            "README.md": "base\n",
        },
    )
    origin = _wire_origin(repo, tmp_path / "origin" / "origin.git")
    _git(repo, "checkout", "-qb", "feature/probe")
    (repo / newline_name).write_text("feature\n", encoding="utf-8")
    (repo / spaced_name).write_text("feature\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "feature conflict")
    candidate = _git(repo, "rev-parse", "HEAD")
    _advance_origin_with(
        origin,
        tmp_path / "origin-conflicts",
        files={newline_name: "main\n", spaced_name: "main\n"},
        message="main conflict",
    )

    probe = work_bridge._probe_main_sync(
        worktree=repo,
        candidate=candidate,
        timeout_seconds=1.0,
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "conflict"
    assert set(probe.conflict_paths) == {newline_name, spaced_name}
    assert probe.path_classification == "multiple-conflicts"


def test_main_sync_probe_classifies_single_readme_conflict_as_generic_conflict(
    tmp_path: Path,
) -> None:
    repo = _init_probe_repo(
        tmp_path / "repo",
        files={"README.md": "base\n"},
    )
    origin = _wire_origin(repo, tmp_path / "origin" / "origin.git")
    _git(repo, "checkout", "-qb", "feature/probe")
    (repo / "README.md").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "feature readme")
    candidate = _git(repo, "rev-parse", "HEAD")
    _advance_origin_with(
        origin,
        tmp_path / "origin-readme",
        files={"README.md": "main\n"},
        message="main readme",
    )

    probe = work_bridge._probe_main_sync(
        worktree=repo,
        candidate=candidate,
        timeout_seconds=1.0,
    )

    assert isinstance(probe, work_bridge.MainSyncProbe)
    assert probe.relation == "conflict"
    assert probe.conflict_paths == ("README.md",)
    assert probe.path_classification == "conflict"


def test_ship_validator_blocks_clean_but_behind_candidate_before_preflight_or_pr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
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


def test_ship_validator_blocks_conflicting_candidate_before_preflight_or_pr(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    default_branch = _git(harness.repo, "branch", "--show-current")
    _git(harness.repo, "checkout", "--quiet", "feature/14-work")
    (harness.repo / "README.md").write_text("feature branch\n", encoding="utf-8")
    _git(harness.repo, "add", "README.md")
    _git(harness.repo, "commit", "-qm", "feature readme")
    harness.candidate = _git(harness.repo, "rev-parse", "HEAD")
    _git(harness.repo, "checkout", "--quiet", default_branch)
    run = harness.registry._manager_update_workflow_run(
        harness.run_id,
        candidate_head=harness.candidate,
        verified_head=harness.candidate,
    )
    _seed_foreign_review(
        registry=harness.registry,
        run=run,
        repo=harness.repo,
        candidate=harness.candidate,
        state_root=harness.state_root,
        worktree=harness.worktree,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin-fixture")
    _advance_origin_with(
        origin,
        tmp_path / "origin-conflict",
        files={"README.md": "main branch\n"},
        message="main readme",
    )

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["status"] == "needs_human"
    assert outcome.result["reason"] == "candidate-conflicts-with-main"
    assert outcome.result["main_sync"]["outcome"] == "conflict"
    assert outcome.result["main_sync"]["conflict_paths"] == ["README.md"]
    assert outcome.result["main_sync"]["path_classification"] == "conflict"
    assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()
    assert harness.registry.get_workflow_run(harness.run_id).pr_refs == ()


def test_ship_validator_allows_in_sync_candidate_through_two_main_probes_and_local_push(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    _wire_local_origin(harness, tmp_path / "origin-fixture")
    seen: list[str | None] = []
    original = work_bridge._probe_main_sync

    def recording_probe(**kwargs):
        result = original(**kwargs)
        seen.append(getattr(result, "main_head", None))
        return result

    monkeypatch.setattr(work_bridge, "_probe_main_sync", recording_probe)

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["status"] == "pending"
    assert seen == [harness.candidate, harness.candidate]
    assert any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert harness.runner.saw_push()
    assert harness.runner.saw_gh()


def test_ship_validator_blocks_push_when_main_advances_between_preflight_and_push(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin-fixture")
    observed: list[str | None] = []
    original_probe = work_bridge._probe_main_sync

    def recording_probe(**kwargs):
        result = original_probe(**kwargs)
        observed.append(getattr(result, "main_head", None))
        return result

    monkeypatch.setattr(work_bridge, "_probe_main_sync", recording_probe)
    original_preflight = work_bridge._run_exact_candidate_preflight
    advanced = {"head": None}

    def advancing_preflight(**kwargs):
        result = original_preflight(**kwargs)
        if advanced["head"] is None:
            advanced["head"] = _advance_origin_main(origin, tmp_path / "origin-advance")
        return result

    monkeypatch.setattr(work_bridge, "_run_exact_candidate_preflight", advancing_preflight)

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["status"] == "needs_human"
    assert outcome.result["reason"] == "candidate-behind-main"
    assert observed == [harness.candidate, advanced["head"]]
    assert any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()


def test_ship_validator_reprobes_after_preflight_even_when_branch_is_already_pushed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin-fixture")
    harness.runner.remote_head = harness.candidate
    observed: list[str | None] = []
    original_probe = work_bridge._probe_main_sync

    def recording_probe(**kwargs):
        result = original_probe(**kwargs)
        observed.append(getattr(result, "main_head", None))
        return result

    monkeypatch.setattr(work_bridge, "_probe_main_sync", recording_probe)
    original_preflight = work_bridge._run_exact_candidate_preflight
    advanced = {"head": None}

    def advancing_preflight(**kwargs):
        result = original_preflight(**kwargs)
        if advanced["head"] is None:
            advanced["head"] = _advance_origin_main(origin, tmp_path / "origin-advance-no-push")
        return result

    monkeypatch.setattr(work_bridge, "_run_exact_candidate_preflight", advancing_preflight)

    outcome = _capture(harness.validator, run=harness.run, candidate=harness.candidate)

    assert outcome.exception is None
    assert isinstance(outcome.result, dict)
    assert outcome.result["status"] == "needs_human"
    assert outcome.result["reason"] == "candidate-behind-main"
    assert observed == [harness.candidate, advanced["head"]]
    assert any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()


@pytest.mark.parametrize(
    ("stage", "final_outcome", "expected_returncode", "expected_error_kind"),
    [
        (
            "merge-base",
            _probe_result(128, stderr="fatal: merge-base failed\n"),
            128,
            "command-failed",
        ),
        (
            "merge-tree",
            _probe_result(129, stderr="error: unknown option `--name-only`\n"),
            129,
            "unsupported-option",
        ),
        (
            "path-parser",
            _probe_result(1, stdout="not-a-tree\0README.md\0"),
            1,
            "output-malformed",
        ),
        (
            "merge-tree",
            subprocess.TimeoutExpired(["git", "merge-tree"], 0.1),
            None,
            "timeout",
        ),
    ],
)
def test_manager_stop_reads_back_post_fetch_main_sync_probe_failures(
    tmp_path: Path,
    monkeypatch,
    stage: str,
    final_outcome: object,
    expected_returncode: int | None,
    expected_error_kind: str,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    outcomes = _probe_success_prefix(
        candidate=harness.candidate,
        main_head="b" * 40,
        merge_base="c" * 40,
    )
    if stage == "merge-base":
        outcomes[-1] = final_outcome
    else:
        outcomes.append(final_outcome)
    validator = work_bridge.build_production_ship_validator(
        registry=harness.registry,
        coordinator_root=harness.state_root,
        snapshot_path=harness.snapshot,
        runner=harness.runner,
        probe_runner=_SequencedProbeRunner(outcomes),
        probe_timeout_seconds=0.1,
    )

    result = _resume(harness, validator=validator)
    payload = _read_probe_evidence(str(result["evidence_ref"]))

    assert result["reason"] == "main-sync-unavailable"
    assert result["evidence_hash"] == work_bridge.verification.canonical_json_hash(payload)
    assert payload["candidate"] == harness.candidate
    assert payload["stage"] == stage
    assert payload["returncode"] == expected_returncode
    assert payload["error_kind"] == expected_error_kind
    assert payload["main_head"] == "b" * 40
    persisted = harness.registry.get_workflow_run(harness.run_id)
    assert json.loads(persisted.needs_human_reason["context"]["main_sync"]) == _main_sync_context(payload)
    assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()


def test_resume_persists_fetch_failure_evidence_and_reprobes_after_remote_repair(
    tmp_path: Path,
    monkeypatch,
) -> None:
    probe_runner = _RecordingProbeRunner()
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=probe_runner,
    )
    if _git(harness.repo, "branch", "--show-current") != "main":
        _git(harness.repo, "branch", "-M", "main")
    _git(harness.repo, "remote", "remove", "origin")
    _git(harness.repo, "remote", "add", "origin", str(tmp_path / "missing-origin.git"))

    result = _resume(harness)
    payload = _read_probe_evidence(str(result["evidence_ref"]))
    persisted = harness.registry.get_workflow_run(harness.run_id)

    assert result["reason"] == "main-sync-unavailable"
    assert result["evidence_hash"] == work_bridge.verification.canonical_json_hash(payload)
    assert payload["candidate"] == harness.candidate
    assert payload["stage"] == "fetch"
    assert probe_runner.fetch_returncodes == [payload["returncode"]]
    assert payload["error_kind"] == "command-failed"
    assert payload["main_head"] is None
    assert persisted.facets == ("needs_human",)
    assert json.loads(persisted.needs_human_reason["context"]["main_sync"]) == _main_sync_context(payload)
    assert not any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert not harness.runner.saw_push()
    assert not harness.runner.saw_gh()

    _wire_local_origin(harness, tmp_path / "repaired-origin")
    observed: list[str | None] = []
    original = work_bridge._probe_main_sync

    def recording_probe(**kwargs):
        result = original(**kwargs)
        observed.append(getattr(result, "main_head", None))
        return result

    monkeypatch.setattr(work_bridge, "_probe_main_sync", recording_probe)

    repaired = _resume(harness)

    assert repaired["reason"] == "delivery-in-progress"
    assert observed == [harness.candidate, harness.candidate]
    assert any(call and call[0] == "preflight" for call in harness.runner.calls)
    assert harness.runner.saw_push()
    assert harness.runner.saw_gh()


def test_reused_ship_workspace_fail_closes_after_source_origin_removal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    harness = _ship_harness(
        tmp_path,
        monkeypatch,
        active_change=False,
        archived_change=True,
        probe_runner=subprocess.run,
    )
    origin = _wire_local_origin(harness, tmp_path / "origin-fixture")

    first = work_bridge._manager_ship_workspace(
        run=harness.run,
        branch="feature/14-work",
        candidate=harness.candidate,
    )
    assert _git(first, "remote", "get-url", "origin") == str(origin)

    _git(harness.repo, "remote", "remove", "origin")

    reused = work_bridge._manager_ship_workspace(
        run=harness.run,
        branch="feature/14-work",
        candidate=harness.candidate,
    )
    origin_url = subprocess.run(
        ["git", "-C", str(reused), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    probe = work_bridge._probe_main_sync(
        worktree=reused,
        candidate=harness.candidate,
        runner=subprocess.run,
    )

    assert reused == first
    assert origin_url.returncode != 0
    assert isinstance(probe, work_bridge.MainSyncProbeFailure)
    assert probe.stage == "fetch"
    assert probe.error_kind == "command-failed"
    assert probe.main_head is None
