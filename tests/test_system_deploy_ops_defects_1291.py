"""Regression coverage for system deployment operation defects in #1291."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import legacy_spec_parking, manager_daemon, source_sync
from paulsha_cortex.coordinator.model_identities import load_model_identities
from paulsha_cortex.monitor.config import load_config
from paulsha_cortex.monitor.git_mirror import GitMirrorError, LocalGitMirror, SubprocessGitRunner
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    THREE_WAY_SCHEME,
    build_manager_unit,
    build_monitor_unit,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_read_only_monitor_mirror_never_fetches(git_origin) -> None:
    repo = git_origin()
    repo.commit({"docs/superpowers/workstreams/example/todo.md": "# todo\n"})
    commands: list[tuple[str, ...]] = []

    class ReadOnlyGitRunner:
        def run(self, argv, *, timeout, stdin=None):
            command = tuple(argv)
            commands.append(command)
            if "fetch" in command:
                raise AssertionError("read-only Monitor attempted to fetch")
            return SubprocessGitRunner().run(command, timeout=timeout, stdin=stdin)

    mirror = LocalGitMirror(
        repo.checkout,
        repo=repo.repo,
        runner=ReadOnlyGitRunner(),
        allow_fetch=False,
    )
    mirror.require(required=(repo.head(),), default_branch="main")
    assert not any("fetch" in command for command in commands)

    missing = LocalGitMirror(
        repo.checkout,
        repo=repo.repo,
        runner=ReadOnlyGitRunner(),
        allow_fetch=False,
    )
    with pytest.raises(GitMirrorError, match="Manager source sync"):
        missing.require(required=("f" * 40,), default_branch="main")
    assert not any("fetch" in command for command in commands)


@pytest.mark.parametrize(
    ("monitor_values", "message"),
    [
        ({"github_refresh_interval_seconds": 1800}, "refresh_interval_seconds"),
        ({"github_refresh_interval_seconds": 900}, "refresh_interval_seconds"),
        ({"provider_stale_after_seconds": 901}, "provider_stale_after_seconds"),
    ],
)
def test_monitor_refresh_and_claim_freshness_limits_are_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    monitor_values: dict[str, int],
    message: str,
) -> None:
    monkeypatch.delenv("PSC_MONITOR_REPO_ROOT_ONLY", raising=False)
    monkeypatch.delenv("PSC_MONITOR_REPO_READONLY", raising=False)
    fields = "\n".join(f"  {key}: {value}" for key, value in monitor_values.items())
    config = _write(
        tmp_path / "project-cortex.yaml",
        "workspaces:\n  - {name: cortex, path: /tmp/cortex}\nmonitor:\n" + fields + "\n",
    )
    with pytest.raises(ValueError, match=message):
        load_config(config_path=config)


def test_system_monitor_uses_instance_repo_and_read_only_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "state" / "repos" / "paulsha-cortex"
    repo_root.mkdir(parents=True)
    config_path = _write(
        tmp_path / "config" / "project-cortex.yaml",
        "workspaces:\n  - {name: cortex, path: /home/operator/prj/paulsha-cortex}\n",
    )
    monkeypatch.setenv("PSC_MONITOR_REPO_ROOT_ONLY", "1")
    monkeypatch.setenv("PSC_MONITOR_REPO_READONLY", "1")
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo_root))

    config = load_config(config_path=config_path)

    assert config.workspaces[0].path == repo_root.resolve()
    assert config.repo_checkout_read_only is True
    assert config.github_refresh_interval_seconds < config.provider_stale_after_seconds
    monitor_unit = build_monitor_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert "Environment=PSC_MONITOR_REPO_READONLY=1" in monitor_unit.content
    assert "Environment=PSC_MONITOR_REPO_ROOT_ONLY=1" in monitor_unit.content
    source_root = DEFAULT_LAYOUT.repo_source_root
    assert not any(
        path == source_root or path.startswith(source_root.rstrip("/") + "/")
        for path in monitor_unit.read_write_paths
    )


def test_system_deploy_installs_default_quota_shadow_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    monkeypatch.setenv("PSC_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("PSC_PROJECT_CONFIG_ROOT", str(config_root / "paulsha"))
    monkeypatch.delenv("PSC_QUOTA_POOLS_CONFIG", raising=False)
    monkeypatch.setenv("PSC_SYSTEM_QUOTA_SHADOW_DEFAULT", "1")
    manager_daemon._QUOTA_POOLS_CONFIG_CACHE.clear()

    context = manager_daemon._quota_admission_context_for(environment={})

    assert context is not None
    assert context.config_revision == "system-shadow-default-v1"
    assert len(context.descriptors) == 1
    identities = load_model_identities(config_root / "paulsha").identities
    assert {binding.to_dict()["subject"]["model_id"] for binding in context.bindings} == {
        identity.model_id for identity in identities
    }
    assert manager_daemon._quota_admission_context_for(
        environment={"PSC_QUOTA_ADMISSION_ENFORCE": "on"}
    ) is None
    assert "Environment=PSC_SYSTEM_QUOTA_SHADOW_DEFAULT=1" in build_manager_unit(
        THREE_WAY_SCHEME, DEFAULT_LAYOUT
    ).content


def test_manager_syncs_default_branch_for_read_only_monitor(
    git_origin,
) -> None:
    remote = git_origin()
    initial = remote.commit({"README.md": "initial\n"})
    remote.publish()
    source = remote.root / "manager-source"
    subprocess.run(
        ("git", "clone", "--quiet", str(remote.origin), str(source)),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ("git", "-C", str(source), "remote", "set-url", "origin", remote.url),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        (
            "git", "-C", str(source), "config",
            f"url.{remote.origin.as_uri()}.insteadOf", remote.url,
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ("git", "-C", str(source), "checkout", "--quiet", "--detach", initial),
        check=True,
        capture_output=True,
        text=True,
    )
    (source / ".git" / "FETCH_HEAD").unlink(missing_ok=True)

    latest = remote.commit(
        {".cortex/work-items.yaml": "work_items: []\n"},
        message="add work item registry",
    )
    remote.publish()

    result = source_sync.sync_source_checkout(source)

    assert result == "advanced"
    assert subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == latest
    assert (source / ".cortex/work-items.yaml").read_text(encoding="utf-8") == "work_items: []\n"
    assert not (source / ".git" / "FETCH_HEAD").exists()
    manager_unit = build_manager_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert "Environment=PSC_MANAGER_REPO_SOURCE_SYNC=1" in manager_unit.content
    assert "Environment=PSC_PARK_LEGACY_AUTO_SPECS=1" in manager_unit.content


def test_legacy_auto_specs_are_parked_once_and_reversible(tmp_path: Path) -> None:
    specs = tmp_path / "specs"
    auto = _write(specs / "legacy.md", "---\ndispatch: auto\n---\nlegacy\n")
    hold = _write(specs / "manual.md", "---\ndispatch: hold\n---\nmanual\n")
    marker = tmp_path / "coordinator" / "legacy-auto-spec-parking.json"

    parked = legacy_spec_parking.park_legacy_auto_specs(
        specs,
        marker,
        parked_date=date(2026, 10, 6),
        parse_spec=lambda path: {"dispatch": "auto" if path.name == "legacy.md" else "hold"},
    )

    assert parked == (".parked-2026-10-06/legacy.md",)
    parked_path = specs / parked[0]
    assert parked_path.read_text(encoding="utf-8") == "---\ndispatch: auto\n---\nlegacy\n"
    assert not auto.exists()
    assert hold.exists()
    _write(specs / "new.md", "new auto spec\n")
    assert legacy_spec_parking.park_legacy_auto_specs(
        specs,
        marker,
        parked_date=date(2026, 10, 6),
        parse_spec=lambda path: {"dispatch": "auto"},
    ) == ()
    assert (specs / "new.md").exists()
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["schema"] == "cortex/legacy-auto-spec-parking/v1"
