from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.monitor.providers import (
    GitHubTerminalProvider,
    GitHubWorkProvider,
    RepoWorkProvider,
    WorkflowRegistryProvider,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_provider_scans_fixed_artifacts_and_excludes_archive(tmp_path):
    _write(tmp_path / "docs/superpowers/workstreams/alpha/todo.md", "# todo\n")
    _write(tmp_path / "docs/superpowers/specs/nested/spec.md", "# spec\n")
    _write(tmp_path / "docs/superpowers/plans/nested/plan.md", "# plan\n")
    _write(tmp_path / "openspec/changes/active-change/proposal.md", "# proposal\n")
    _write(tmp_path / "openspec/changes/active-change/specs/demo/spec.md", "# delta\n")
    _write(
        tmp_path / "openspec/changes/archive/2026-07-17-old-change/proposal.md",
        "# archived\n",
    )
    _write(tmp_path / "random/todo.md", "# ignored\n")

    result = RepoWorkProvider(tmp_path, repo="example/acme").scan()

    assert result.status == "ok"
    assert {(source.kind, source.ref) for source in result.sources} == {
        ("todo", "docs/superpowers/workstreams/alpha/todo.md"),
        ("superpowers_spec", "docs/superpowers/specs/nested/spec.md"),
        ("superpowers_plan", "docs/superpowers/plans/nested/plan.md"),
        ("openspec", "active-change"),
    }
    assert all("archive" not in source.ref for source in result.sources)
    assert result.revision.startswith("repo-overlay:")


def test_repo_provider_revision_includes_uncommitted_overlay(tmp_path):
    artifact = _write(
        tmp_path / "docs/superpowers/specs/work.md",
        "---\nwork_item: work\n---\n# v1\n",
    )
    provider = RepoWorkProvider(tmp_path, repo="example/acme")
    first = provider.scan()

    artifact.write_text("---\nwork_item: work\n---\n# v2\n", encoding="utf-8")
    second = provider.scan()

    assert first.status == second.status == "ok"
    assert first.revision != second.revision
    assert first.sources[0].revision != second.sources[0].revision


def test_repo_provider_active_archive_collision_fails_closed(tmp_path):
    _write(tmp_path / "openspec/changes/duplicate/proposal.md", "# active\n")
    _write(
        tmp_path / "openspec/changes/archive/2026-07-17-duplicate/proposal.md",
        "# archive\n",
    )

    result = RepoWorkProvider(tmp_path, repo="example/acme").scan()

    assert result.status == "degraded"
    assert any("active/archive collision" in item for item in result.diagnostics)


def test_repo_provider_scan_race_is_degraded(monkeypatch, tmp_path):
    artifact = _write(tmp_path / "docs/superpowers/specs/work.md", "# work\n").resolve()
    original = Path.read_bytes

    def disappear(self):
        if self == artifact:
            raise FileNotFoundError("artifact disappeared during scan")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", disappear)
    result = RepoWorkProvider(tmp_path, repo="example/acme").scan()

    assert result.status == "degraded"
    assert result.sources == ()
    assert any("scan unavailable" in item for item in result.diagnostics)


class FakeRunner:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv, *, timeout):
        self.calls.append((tuple(argv), timeout))
        if self.error is not None:
            raise self.error
        return self.result


class SequenceRunner:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def run(self, argv, *, timeout):
        self.calls.append(tuple(argv))
        return next(self.results)


def _completed(payload, *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def test_github_provider_uses_typed_argv_and_json():
    rows = [
        {
            "number": 14,
            "title": "umbrella",
            "state": "open",
            "node_id": "ISSUE_node",
            "updated_at": "2026-07-17T10:00:00Z",
            "labels": [{"name": "cortex:auto-on-going"}],
        },
        {
            "number": 15,
            "title": "delivery",
            "state": "closed",
            "node_id": "PR_node",
            "updated_at": "2026-07-17T10:01:00Z",
            "pull_request": {"url": "https://api.github.test/pr/15"},
        },
    ]
    runner = FakeRunner(
        subprocess.CompletedProcess(
            args=("gh",),
            returncode=0,
            stdout="\n".join(json.dumps(row) for row in rows) + "\n",
            stderr="",
        )
    )

    result = GitHubWorkProvider("example/acme", runner=runner, timeout_seconds=12).scan()

    assert result.status == "ok"
    assert [(source.kind, source.ref, source.status) for source in result.sources] == [
        ("github_issue", "example/acme#14", "open"),
        ("github_pr", "example/acme#15", "closed"),
    ]
    assert result.sources[0].title == "umbrella"
    argv, timeout = runner.calls[0]
    assert argv == (
        "gh",
        "api",
        "--method",
        "GET",
        "--paginate",
        "--jq",
        ".[]",
        "repos/example/acme/issues?state=all&per_page=100",
    )
    assert timeout == 12


def test_github_provider_auth_failure_is_degraded():
    runner = FakeRunner(_completed({}, returncode=1, stderr="HTTP 401: Bad credentials"))

    result = GitHubWorkProvider("example/acme", runner=runner).scan()

    assert result.status == "degraded"
    assert result.sources == ()
    assert any("authentication" in item for item in result.diagnostics)


def test_github_provider_rate_limit_is_degraded():
    runner = FakeRunner(
        _completed({}, returncode=1, stderr="HTTP 403: API rate limit exceeded")
    )

    result = GitHubWorkProvider("example/acme", runner=runner).scan()

    assert result.status == "degraded"
    assert any("rate limit" in item for item in result.diagnostics)


def test_github_provider_secondary_rate_limit_mentioning_oauth_is_not_misclassified_as_auth():
    """#370: gh's real secondary/abuse-detection rate limit message mentions
    "OAuth" and invites re-authenticating -- the classifier must recognize
    the rate-limit wording first, not fall into the auth-failure branch."""
    runner = FakeRunner(
        _completed(
            {},
            returncode=1,
            stderr=(
                "HTTP 403: You have exceeded a secondary rate limit for the "
                "OAuth App associated with this personal access token. Please "
                "wait a few minutes before you try again by re-authenticating."
            ),
        )
    )

    result = GitHubWorkProvider("example/acme", runner=runner).scan()

    assert result.status == "degraded"
    assert any("rate limit" in item for item in result.diagnostics)
    assert not any("authentication" in item for item in result.diagnostics)


def test_github_provider_timeout_is_degraded():
    runner = FakeRunner(error=subprocess.TimeoutExpired(cmd=("gh", "api"), timeout=30))

    result = GitHubWorkProvider("example/acme", runner=runner).scan()

    assert result.status == "degraded"
    assert any("timeout" in item for item in result.diagnostics)


def test_github_provider_malformed_json_is_degraded():
    runner = FakeRunner(
        subprocess.CompletedProcess(
            args=("gh",), returncode=0, stdout="not-json", stderr=""
        )
    )

    result = GitHubWorkProvider("example/acme", runner=runner).scan()

    assert result.status == "degraded"
    assert any("JSON" in item for item in result.diagnostics)


def test_workflow_registry_existing_completion_schema_remains_not_valid(monkeypatch, tmp_path):
    state = tmp_path / "workflows.json"
    record = tmp_path / "evidence/completion/record.json"
    record.parent.mkdir(parents=True)
    record.write_text("{}", encoding="utf-8")
    calls = []

    def read_record(path, *, expected_hash=None):
        calls.append((str(path), expected_hash))
        return {"candidate": "a" * 40}

    monkeypatch.setattr(
        "paulsha_cortex.coordinator.completion.read_completion_record", read_record
    )
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 8,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-7",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "review",
                        "completion_record_path": str(record),
                        "completion_record_hash": "b" * 64,
                        "completion_record_revision": "a" * 40,
                        "source_revisions": {"github_pr:example/acme#9": "p" * 40},
                        "pr_candidate": "a" * 40,
                        "merge_revision": "d" * 40,
                    },
                    {
                        "run_id": "foreign",
                        "repo": "example/other",
                        "work_id": "work",
                        "status": "build",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert [source.ref for source in result.sources] == ["run-7"]
    source_id = result.sources[0].source_id
    assert result.observations["workflow_links"] == {source_id: "work"}
    assert result.observations["validated_completions"] == {}
    assert calls == [(str(record), "b" * 64)]


def test_workflow_registry_preserves_validated_completion_identity(monkeypatch, tmp_path):
    state = tmp_path / "workflows.json"
    record = tmp_path / "evidence/completion/record.json"
    record.parent.mkdir(parents=True)
    record.write_text("{}", encoding="utf-8")
    source_revisions = {
        "github_issue:example/acme#7": "github:i7",
        "github_pr:example/acme#9": "github:p9",
    }
    monkeypatch.setattr(
        "paulsha_cortex.coordinator.completion.read_completion_record",
        lambda *_args, **_kwargs: {
            "candidate": "a" * 40,
            "work_id": "work",
            "run_id": "run-7",
            "source_revisions": source_revisions,
            "merge_revision": "d" * 40,
        },
    )
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 8,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-7",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "completed",
                        "completion_record_path": str(record),
                        "completion_record_hash": "b" * 64,
                        "completion_record_revision": "a" * 40,
                        "source_revisions": source_revisions,
                        "pr_candidate": "a" * 40,
                        "merge_revision": "d" * 40,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert result.observations["validated_completions"] == {
        "work": [
            {
                "run_id": "run-7",
                "pr_candidate": "a" * 40,
                "merge_revision": "d" * 40,
                "source_revisions": source_revisions,
            }
        ]
    }


def test_workflow_registry_rejects_cross_work_completion_replay(monkeypatch, tmp_path):
    state = tmp_path / "workflows.json"
    record = tmp_path / "evidence/completion/record.json"
    record.parent.mkdir(parents=True)
    record.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "paulsha_cortex.coordinator.completion.read_completion_record",
        lambda *_args, **_kwargs: {
            "candidate": "a" * 40,
            "work_id": "other-work",
            "run_id": "run-7",
            "source_revisions": {"github_pr:example/acme#9": "p" * 40},
            "merge_revision": "d" * 40,
        },
    )
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 8,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-7",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "review",
                        "completion_record_path": str(record),
                        "completion_record_hash": "b" * 64,
                        "completion_record_revision": "a" * 40,
                        "source_revisions": {"github_pr:example/acme#9": "p" * 40},
                        "pr_candidate": "a" * 40,
                        "merge_revision": "d" * 40,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert result.observations["validated_completions"] == {}


def test_workflow_registry_skips_broken_completion_row_and_keeps_other_sources(
    monkeypatch, tmp_path
):
    state = tmp_path / "workflows.json"
    broken = tmp_path / "evidence/completion/broken.json"
    valid = tmp_path / "evidence/completion/valid.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{}", encoding="utf-8")
    valid.write_text("{}", encoding="utf-8")

    def read_record(path, *, expected_hash=None):
        if Path(path) == broken:
            raise ValueError("completion record unreadable")
        return {
            "candidate": "b" * 40,
            "work_id": "good-work",
            "run_id": "run-good",
            "source_revisions": {"github_pr:example/acme#10": "p" * 40},
            "merge_revision": "d" * 40,
        }

    monkeypatch.setattr(
        "paulsha_cortex.coordinator.completion.read_completion_record",
        read_record,
    )
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 8,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-bad",
                        "repo": "example/acme",
                        "work_id": "bad-work",
                        "status": "completed",
                        "completion_record_path": str(broken),
                        "completion_record_hash": "b" * 64,
                        "completion_record_revision": "a" * 40,
                        "source_revisions": {"github_pr:example/acme#9": "p" * 40},
                        "pr_candidate": "a" * 40,
                        "merge_revision": "d" * 40,
                        "pr_refs": ["example/acme#9"],
                    },
                    {
                        "run_id": "run-good",
                        "repo": "example/acme",
                        "work_id": "good-work",
                        "status": "completed",
                        "completion_record_path": str(valid),
                        "completion_record_hash": "c" * 64,
                        "completion_record_revision": "b" * 40,
                        "source_revisions": {"github_pr:example/acme#10": "p" * 40},
                        "pr_candidate": "b" * 40,
                        "merge_revision": "d" * 40,
                        "pr_refs": ["example/acme#10"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert [source.ref for source in result.sources] == ["run-good"]
    assert result.observations["workflow_links"] == {
        "workflow_run:example/acme:run-good": "good-work",
        "github_pr:example/acme#10": "good-work",
    }
    assert result.observations["validated_completions"] == {
        "good-work": [
            {
                "run_id": "run-good",
                "pr_candidate": "b" * 40,
                "merge_revision": "d" * 40,
                "source_revisions": {"github_pr:example/acme#10": "p" * 40},
            }
        ]
    }


def test_workflow_registry_missing_completion_file_skips_row_without_degrading(
    monkeypatch, tmp_path
):
    state = tmp_path / "workflows.json"
    missing = tmp_path / "evidence/completion/missing.json"
    valid = tmp_path / "evidence/completion/valid.json"
    valid.parent.mkdir(parents=True)
    valid.write_text("{}", encoding="utf-8")

    def read_record(path, *, expected_hash=None):
        assert expected_hash == "c" * 64
        assert Path(path) == valid
        return {
            "candidate": "b" * 40,
            "work_id": "good-work",
            "run_id": "run-good",
            "source_revisions": {"github_pr:example/acme#10": "p" * 40},
            "merge_revision": "d" * 40,
        }

    monkeypatch.setattr(
        "paulsha_cortex.coordinator.completion.read_completion_record",
        read_record,
    )
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 8,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-missing",
                        "repo": "example/acme",
                        "work_id": "missing-work",
                        "status": "completed",
                        "completion_record_path": str(missing),
                        "completion_record_hash": "b" * 64,
                        "completion_record_revision": "a" * 40,
                        "source_revisions": {"github_pr:example/acme#9": "p" * 40},
                        "pr_candidate": "a" * 40,
                        "merge_revision": "d" * 40,
                        "pr_refs": ["example/acme#9"],
                    },
                    {
                        "run_id": "run-good",
                        "repo": "example/acme",
                        "work_id": "good-work",
                        "status": "completed",
                        "completion_record_path": str(valid),
                        "completion_record_hash": "c" * 64,
                        "completion_record_revision": "b" * 40,
                        "source_revisions": {"github_pr:example/acme#10": "p" * 40},
                        "pr_candidate": "b" * 40,
                        "merge_revision": "d" * 40,
                        "pr_refs": ["example/acme#10"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert [source.ref for source in result.sources] == ["run-good"]
    assert any(
        diagnostic.startswith("workflow completion skipped: run-missing:")
        for diagnostic in result.diagnostics
    )
    assert result.observations["workflow_links"] == {
        "workflow_run:example/acme:run-good": "good-work",
        "github_pr:example/acme#10": "good-work",
    }
    assert result.observations["validated_completions"] == {
        "good-work": [
            {
                "run_id": "run-good",
                "pr_candidate": "b" * 40,
                "merge_revision": "d" * 40,
                "source_revisions": {"github_pr:example/acme#10": "p" * 40},
            }
        ]
    }


def test_workflow_registry_validates_refs_and_emits_canonical_authority_edges(tmp_path):
    state = tmp_path / "workflows.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 9,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-9",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "completed",
                        "issue_refs": ["example/acme#7"],
                        "pr_refs": ["example/acme#9"],
                        "openspec_refs": ["canary"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert result.observations["workflow_links"] == {
        "workflow_run:example/acme:run-9": "work",
        "github_issue:example/acme#7": "work",
        "github_pr:example/acme#9": "work",
        "openspec:example/acme:canary": "work",
        "github_openspec:example/acme:canary:active": "work",
        "github_openspec:example/acme:canary:archived": "work",
    }


@pytest.mark.parametrize(
    "field,value",
    (
        ("issue_refs", "example/acme#7"),
        ("issue_refs", ["example/other#7"]),
        ("issue_refs", ["example/acme#0"]),
        ("pr_refs", ["example/acme#x"]),
        ("openspec_refs", ["../escape"]),
        ("openspec_refs", ["canary", "canary"]),
    ),
)
def test_workflow_registry_rejects_invalid_typed_refs(tmp_path, field, value):
    state = tmp_path / "workflows.json"
    row = {
        "run_id": "run-9",
        "repo": "example/acme",
        "work_id": "work",
        "status": "completed",
        "issue_refs": [],
        "pr_refs": [],
        "openspec_refs": [],
    }
    row[field] = value
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 9,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [row],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "degraded"


def test_workflow_registry_rejects_cross_work_authority_collision(tmp_path):
    state = tmp_path / "workflows.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 9,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": f"run-{work_id}",
                        "repo": "example/acme",
                        "work_id": work_id,
                        "status": "completed",
                        "issue_refs": ["example/acme#7"],
                    }
                    for work_id in ("one", "two")
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "degraded"
    assert any("authority collision" in note for note in result.diagnostics)


def test_workflow_registry_superseded_run_does_not_claim_issue_authority(tmp_path):
    # A superseded (abandoned) run must not claim issue/pr/openspec authority,
    # so it cannot collide with another work item's run on a shared issue ref
    # (regression for #182: workflow provider degraded froze projection and
    # hid newly override-bound sources like #100).
    state = tmp_path / "workflows.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 9,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-abandoned",
                        "repo": "example/acme",
                        "work_id": "abandoned-batch",
                        "status": "superseded",
                        "issue_refs": ["example/acme#7"],
                        "pr_refs": ["example/acme#9"],
                        "openspec_refs": ["canary"],
                    },
                    {
                        "run_id": "run-active",
                        "repo": "example/acme",
                        "work_id": "real-work",
                        "status": "completed",
                        "issue_refs": ["example/acme#7"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    links = result.observations["workflow_links"]
    # superseded run still maps its own workflow_run source (unique, no collision)
    assert links["workflow_run:example/acme:run-abandoned"] == "abandoned-batch"
    # but it does NOT claim issue/pr/openspec authority (only the active run does)
    assert links["github_issue:example/acme#7"] == "real-work"
    assert "github_pr:example/acme#9" not in links
    assert "openspec:example/acme:canary" not in links


def test_workflow_registry_unknown_schema_and_root_keys_are_degraded(tmp_path):
    state = tmp_path / "workflows.json"
    for payload in (
        {
            "schema_version": 99,
            "sequence": 1,
            "workflow_runs": [],
            "legacy_records": {"jobs": [], "slices": []},
        },
        {
            "schema_version": 2,
            "sequence": 1,
            "workflow_runs": [],
            "legacy_records": {"jobs": [], "slices": []},
            "unknown": True,
        },
        {"sequence": 1, "workflow_runs": []},
    ):
        state.write_text(json.dumps(payload), encoding="utf-8")
        result = WorkflowRegistryProvider("example/acme", state_path=state).scan()
        assert result.status == "degraded"


def test_workflow_registry_explicit_v1_is_compatible_but_never_associated(tmp_path):
    state = tmp_path / "jobs.json"
    state.write_text(
        json.dumps({"schema_version": 1, "seq": 3, "jobs": [], "slices": []}),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert result.sources == ()
    assert result.observations == {}


def test_workflow_registry_rejects_boolean_completion_without_record(tmp_path):
    state = tmp_path / "workflows.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 1,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-1",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "review",
                        "completion_record_valid": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "degraded"


def test_workflow_registry_rejects_completion_record_outside_safe_root(tmp_path):
    record = tmp_path / "outside.json"
    record.write_text("{}", encoding="utf-8")
    state = tmp_path / "coordinator/workflows.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 1,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-1",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "review",
                        "completion_record_path": str(record),
                        "completion_record_hash": "b" * 64,
                        "completion_record_revision": "a" * 40,
                        "source_revisions": {"github_pr:example/acme#9": "p" * 40},
                        "pr_candidate": "a" * 40,
                        "merge_revision": "d" * 40,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert result.sources == ()
    assert result.observations["validated_completions"] == {}
    assert result.diagnostics == (
        "workflow completion skipped: run-1: completion_record_path escapes coordinator completion root",
    )


def test_github_terminal_provider_reads_closing_refs_and_remote_archive(git_origin):
    repo = git_origin()
    repo.commit(
        {"openspec/changes/archive/2026-07-17-work/proposal.md": "# archived\n"},
        message="archive",
    )
    head = repo.branch_commit("feature/9-work", {"src.py": "x = 1\n"})
    merge = repo.merge("feature/9-work")
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": repo.head()},
                },
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "number": 9,
                            "body": "work_item: work\n",
                            "headRefOid": head,
                            "state": "MERGED",
                            "mergedAt": "2026-07-17T10:00:00Z",
                            "mergeCommit": {
                                "oid": merge,
                                "parents": {"totalCount": 2},
                            },
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 7, "state": "CLOSED"}]
                            },
                        }
                    ]
                },
            }
        }
    }
    tree = {
        "truncated": False,
        "tree": [
            {"path": "openspec/changes/archive/2026-07-17-work/proposal.md"}
        ]
    }
    runner = SequenceRunner([_completed(graph), _completed(tree)])

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "ok"
    assert result.observations["closing_links"] == {
        "github_pr:example/acme#9": "github_issue:example/acme#7",
    }
    assert result.observations["remote_prs"] == [
        {
            "source_id": "github_pr:example/acme#9",
            "candidate": head,
            "merge_revision": merge,
            "merged_with_merge_commit": True,
        }
    ]
    assert [(source.ref, source.status) for source in result.sources] == [
        ("work", "archived")
    ]
    assert result.observations["remote_openspec"] == {
        "active": [],
        "archived": ["work"],
    }
    # D2：ancestry 走本機 merge-base，一輪不再有 compare 請求。
    assert not any("/compare/" in argv[-1] for argv in runner.calls)


def test_github_terminal_provider_aggregates_pull_requests_across_pages():
    first_page = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
                "pullRequests": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                    "nodes": [
                        {
                            "number": 9,
                            "body": "",
                            "headRefName": "feature/9-work",
                            "headRefOid": "e" * 40,
                            "state": "OPEN",
                            "mergedAt": None,
                            "mergeCommit": None,
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 7, "state": "OPEN"}],
                            },
                        }
                    ],
                },
            }
        }
    }
    second_page = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "ignored", "target": {"oid": "f" * 40}},
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "number": 10,
                            "body": "",
                            "headRefName": "feature/10-work",
                            "headRefOid": "a" * 40,
                            "state": "CLOSED",
                            "mergedAt": None,
                            "mergeCommit": None,
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 8, "state": "CLOSED"}],
                            },
                        }
                    ],
                },
            }
        }
    }
    runner = SequenceRunner(
        [
            _completed(first_page),
            _completed(second_page),
            _completed({"truncated": False, "tree": []}),
        ]
    )

    result = GitHubTerminalProvider("example/acme", runner=runner).scan()

    assert result.status == "ok"
    assert result.observations["closing_links"] == {
        "github_pr:example/acme#9": "github_issue:example/acme#7",
    }
    assert result.observations["branches"] == [
        {"source_id": "github_pr:example/acme#9", "ref": "feature/9-work"},
        {"source_id": "github_pr:example/acme#10", "ref": "feature/10-work"},
    ]
    assert result.observations["remote_prs"] == [
        {
            "source_id": "github_pr:example/acme#10",
            "candidate": "a" * 40,
            "merge_revision": None,
            "merged_with_merge_commit": False,
        },
        {
            "source_id": "github_pr:example/acme#9",
            "candidate": "e" * 40,
            "merge_revision": None,
            "merged_with_merge_commit": False,
        },
    ]
    assert result.observations["default_branch"] == "main"
    assert result.observations["default_revision"] == "d" * 40
    assert runner.calls[0] == (
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={GitHubTerminalProvider._QUERY}",
        "-F",
        "owner=example",
        "-F",
        "name=acme",
    )
    assert runner.calls[1] == (
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={GitHubTerminalProvider._QUERY}",
        "-F",
        "owner=example",
        "-F",
        "name=acme",
        "-F",
        "cursor=cursor-1",
    )


def test_github_terminal_provider_pull_request_page_limit_is_explicit_failure():
    runner = SequenceRunner(
        [
            _completed(
                {
                    "data": {
                        "repository": {
                            "defaultBranchRef": {
                                "name": "main",
                                "target": {"oid": "d" * 40},
                            },
                            "pullRequests": {
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": f"cursor-{index}",
                                },
                                "nodes": [],
                            },
                        }
                    }
                }
            )
            for index in range(20)
        ]
    )

    result = GitHubTerminalProvider("example/acme", runner=runner).scan()

    assert result.status == "degraded"
    assert result.sources == ()
    assert result.observations == {}
    assert result.diagnostics == ("github terminal evidence unavailable",)
    assert len(runner.calls) == 20
    assert all(call[:3] == ("gh", "api", "graphql") for call in runner.calls)


def test_github_terminal_squash_merge_is_not_a_merge_commit():
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "number": 9,
                            "body": "work_item: work\n",
                            "state": "MERGED",
                            "mergedAt": "2026-07-17T10:00:00Z",
                            "mergeCommit": {
                                "oid": "a" * 40,
                                "parents": {"totalCount": 1},
                            },
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 7, "state": "CLOSED"}],
                            },
                        }
                    ],
                },
            }
        }
    }
    runner = SequenceRunner([_completed(graph), _completed({"truncated": False, "tree": []})])

    result = GitHubTerminalProvider("example/acme", runner=runner).scan()

    assert result.status == "ok"
    assert not result.observations["remote_prs"][0]["merged_with_merge_commit"]


def test_github_terminal_merge_not_on_default_branch_is_not_terminal(git_origin):
    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    repo.branch_commit("feature/9-work", {"src.py": "x = 1\n"})
    # merge commit 落在側枝上，default branch 走不到它（等同舊 compare 的 diverged）。
    repo.git("checkout", "--quiet", "-b", "release", "main")
    repo.git("merge", "--quiet", "--no-ff", "-m", "side merge", "feature/9-work")
    merge = repo.head("release")
    repo.git("checkout", "--quiet", "main")
    repo.commit({"README.md": "# moved on\n"}, message="advance")
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": repo.head("main")},
                },
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "number": 9,
                            "body": "work_item: work\n",
                            "state": "MERGED",
                            "mergedAt": "2026-07-17T10:00:00Z",
                            "mergeCommit": {"oid": merge, "parents": {"totalCount": 2}},
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 7, "state": "CLOSED"}],
                            },
                        }
                    ],
                },
            }
        }
    }
    runner = SequenceRunner(
        [_completed(graph), _completed({"truncated": False, "tree": []})]
    )

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert not result.observations["remote_prs"][0]["merged_with_merge_commit"]


def test_github_terminal_resolves_ancestry_only_for_workflow_linked_prs(git_origin):
    repo = git_origin()
    repo.commit({"README.md": "# base\n"}, message="base")
    merges: dict[int, str] = {}
    heads: dict[int, str] = {}
    for number in (8, 9):
        heads[number] = repo.branch_commit(
            f"feature/{number}-work", {f"src{number}.py": "x = 1\n"}
        )
        merges[number] = repo.merge(f"feature/{number}-work")

    def merged(number: int) -> dict:
        return {
            "number": number,
            "body": "",
            "headRefName": f"feature/{number}-work",
            "headRefOid": heads[number],
            "state": "MERGED",
            "mergedAt": "2026-07-17T10:00:00Z",
            "mergeCommit": {
                "oid": merges[number],
                "parents": {"totalCount": 2},
            },
            "closingIssuesReferences": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"number": number, "state": "CLOSED"}],
            },
        }

    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": repo.head()},
                },
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [merged(8), merged(9)],
                },
            }
        }
    }
    runner = SequenceRunner(
        [_completed(graph), _completed({"truncated": False, "tree": []})]
    )

    result = GitHubTerminalProvider(
        repo.repo,
        runner=runner,
        relevant_pr_numbers=(9,),
        repo_root=repo.checkout,
    ).scan()

    assert result.status == "ok"
    # D2：REST 只剩 graphql ＋ tree；ancestry 完全在本機解，且只解 workflow-linked 的那一個。
    assert len(runner.calls) == 2
    assert result.observations["remote_reads"]["ancestry_checks"] == 1
    by_number = {
        int(row["source_id"].rsplit("#", 1)[1]): row
        for row in result.observations["remote_prs"]
    }
    assert by_number[8]["merged_with_merge_commit"] is False
    assert by_number[9]["merged_with_merge_commit"] is True


def test_github_terminal_truncated_tree_is_degraded():
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
                "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        }
    }
    runner = SequenceRunner(
        [_completed(graph), _completed({"truncated": True, "tree": []})]
    )

    result = GitHubTerminalProvider("example/acme", runner=runner).scan()

    assert result.status == "degraded"


def test_github_terminal_retries_only_transient_gateway_failures():
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
                "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        }
    }
    transient = _completed(
        {"message": "temporarily unavailable"},
        returncode=1,
        stderr="gh: temporarily unavailable (HTTP 503)",
    )
    runner = SequenceRunner(
        [transient, _completed(graph), _completed({"truncated": False, "tree": []})]
    )
    sleeps = []

    result = GitHubTerminalProvider(
        "example/acme",
        runner=runner,
        retry_delays=(0.25,),
        sleeper=sleeps.append,
    ).scan()

    assert result.status == "ok"
    assert sleeps == [0.25]
    assert runner.calls[0] == runner.calls[1]


def test_github_terminal_does_not_retry_non_transient_api_failure():
    runner = SequenceRunner(
        [
            _completed(
                {"message": "bad credentials"},
                returncode=1,
                stderr="gh: bad credentials (HTTP 401)",
            )
        ]
    )
    sleeps = []

    result = GitHubTerminalProvider(
        "example/acme",
        runner=runner,
        retry_delays=(0.25, 0.5),
        sleeper=sleeps.append,
    ).scan()

    assert result.status == "degraded"
    assert sleeps == []
    assert len(runner.calls) == 1


def test_remote_default_branch_todo_blob_is_only_completion_authority(
    tmp_path, git_origin
):
    todo = tmp_path / "docs/superpowers/workstreams/work/todo.md"
    todo.parent.mkdir(parents=True)
    todo.write_text("---\nwork_item: work\n---\n- [x] local only\n", encoding="utf-8")
    local = RepoWorkProvider(tmp_path, repo="example/acme").scan()
    assert local.observations.get("closure_by_work", {}) == {}

    # D2：remote 那一份現在從本機 git objects 讀（sha 定址），不再打 contents API。
    repo = git_origin()
    todo_path = "docs/superpowers/workstreams/work/todo.md"
    repo.commit(
        {todo_path: "---\nwork_item: work\n---\n- [x] remote task\n"},
        message="remote todo",
    )
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": repo.head()},
                },
                "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        }
    }
    tree = {
        "truncated": False,
        "tree": [
            {
                "path": todo_path,
                "type": "blob",
                "sha": repo.blob_sha(todo_path),
            }
        ],
    }
    runner = SequenceRunner([_completed(graph), _completed(tree)])

    remote = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert remote.status == "ok"
    assert remote.observations["remote_todos"] == [
        {
            "work_id": "work",
            "path": todo_path,
            "revision": repo.blob_sha(todo_path),
            "complete": True,
        }
    ]
    assert len(runner.calls) == 2
    assert not any("/contents/" in argv[-1] for argv in runner.calls)


def test_remote_archived_openspec_tasks_are_todo_completion_evidence(git_origin):
    repo = git_origin()
    task_path = "openspec/changes/archive/2026-07-17-canary/tasks.md"
    repo.commit({task_path: "- [x] task one\n- [x] task two\n"}, message="archive")
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": repo.head()},
                },
                "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        }
    }
    tree = {
        "truncated": False,
        "tree": [
            {
                "path": task_path,
                "type": "blob",
                "sha": repo.blob_sha(task_path),
            }
        ],
    }
    runner = SequenceRunner([_completed(graph), _completed(tree)])

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "ok"
    assert result.observations["remote_todos"] == [
        {
            "openspec_ref": "canary",
            "path": task_path,
            "revision": repo.blob_sha(task_path),
            "complete": True,
        }
    ]
    assert [(source.ref, source.status) for source in result.sources] == [
        ("canary", "archived")
    ]


def test_remote_todo_blob_absent_from_git_is_degraded(git_origin):
    """D2：舊的 ``contents`` identity mismatch 由 sha 定址取代。

    blob sha 讀回來的內容不可能對不上（sha 就是內容），剩下的失敗模式只有「物件
    不在」——那一格必須 degraded，**不得**靜默當成「檔案不存在」而讓 remote todo
    憑空消失（fail closed）。
    """

    repo = git_origin()
    path = "docs/superpowers/workstreams/work/todo.md"
    repo.commit({path: "---\nwork_item: work\n---\n- [x] task\n"}, message="todo")
    repo.publish()
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {
                    "name": "main",
                    "target": {"oid": repo.head()},
                },
                "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
            }
        }
    }
    tree = {
        "truncated": False,
        "tree": [{"path": path, "type": "blob", "sha": "e" * 40}],
    }
    runner = SequenceRunner([_completed(graph), _completed(tree)])

    result = GitHubTerminalProvider(
        repo.repo, runner=runner, repo_root=repo.checkout
    ).scan()

    assert result.status == "degraded"
    assert result.observations == {}
    assert "git mirror" in result.diagnostics[0]


def test_pr_body_work_item_is_not_confirmed_authority():
    graph = {
        "data": {
            "repository": {
                "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
                "pullRequests": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "number": 9,
                            "body": "work_item: evil\n",
                            "headRefName": "feature/7-work",
                            "state": "OPEN",
                            "mergedAt": None,
                            "mergeCommit": None,
                            "closingIssuesReferences": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"number": 7, "state": "OPEN"}],
                            },
                        }
                    ],
                },
            }
        }
    }
    runner = SequenceRunner(
        [_completed(graph), _completed({"truncated": False, "tree": []})]
    )

    result = GitHubTerminalProvider("example/acme", runner=runner).scan()

    assert result.observations["closing_links"] == {
        "github_pr:example/acme#9": "github_issue:example/acme#7"
    }
    assert "evil" not in json.dumps(result.observations["closing_links"])


def test_schema_retry_counts_surface_without_degrading_projection(tmp_path):
    """#261 D5：schema mismatch 計數要能被投影出來，且不得讓任何 row 變 unsupported。

    #205 曾經因為新增 WorkflowRun 欄位而讓每一個 row 落入 optional-key 白名單之外、
    整份 workflow projection 變 degraded。本測試同時鎖住兩件事：計數確實出現，
    以及 provider 仍為 ok、run 仍被投影出來。
    """

    state = tmp_path / "registry.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 3,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-1",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "build",
                        "attempts": {
                            "build": 2,
                            "schema-mismatch:tdd-red": 2,
                            "schema-mismatch:impl": 1,
                        },
                    },
                    {
                        "run_id": "run-2",
                        "repo": "example/acme",
                        "work_id": "other",
                        "status": "build",
                        "attempts": {"build": 1},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    # 沒有任何 row 被判為 unsupported → provider 仍 ok，兩個 run 都在。
    assert result.status == "ok"
    assert result.diagnostics == ()
    assert sorted(source.ref for source in result.sources) == ["run-1", "run-2"]

    # 只有 schema-mismatch:* 會被萃取出來，一般的 phase attempts 不混入。
    assert result.observations["schema_retry"] == {
        "work": {"tdd-red": 2, "impl": 1}
    }


def test_schema_retry_extraction_ignores_non_integer_counts(tmp_path):
    """壞資料不得讓 provider 掛掉或產出非數值計數。"""

    state = tmp_path / "registry.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "sequence": 1,
                "legacy_records": {"jobs": [], "slices": []},
                "workflow_runs": [
                    {
                        "run_id": "run-1",
                        "repo": "example/acme",
                        "work_id": "work",
                        "status": "build",
                        "attempts": {
                            "schema-mismatch:ok": 1,
                            "schema-mismatch:bool": True,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = WorkflowRegistryProvider("example/acme", state_path=state).scan()

    assert result.status == "ok"
    assert result.observations["schema_retry"] == {"work": {"ok": 1}}
