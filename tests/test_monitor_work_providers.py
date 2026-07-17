from __future__ import annotations

import json
import subprocess
from pathlib import Path

from paulsha_cortex.monitor.providers import GitHubWorkProvider, RepoWorkProvider


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


def _completed(payload, *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def test_github_provider_uses_typed_argv_and_json():
    runner = FakeRunner(
        _completed(
            [[
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
            ]]
        )
    )

    result = GitHubWorkProvider("example/acme", runner=runner, timeout_seconds=12).scan()

    assert result.status == "ok"
    assert [(source.kind, source.ref, source.status) for source in result.sources] == [
        ("github_issue", "example/acme#14", "open"),
        ("github_pr", "example/acme#15", "closed"),
    ]
    argv, timeout = runner.calls[0]
    assert argv == (
        "gh",
        "api",
        "--method",
        "GET",
        "--paginate",
        "--slurp",
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
