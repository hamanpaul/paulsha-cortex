"""Real-backend contracts that stay inside a temporary root or mocked argv seam."""

from __future__ import annotations

import grp
import hashlib
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

import pytest

import repository_runtime_fixtures
from paulsha_cortex.trust_root.install import (
    AccountCollisionError,
    InstallDriftError,
    UnsafeInstallPathError,
)
from paulsha_cortex.trust_root.install import backend as backend_module
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install.backend import LocalInstallBackend
from paulsha_cortex.trust_root.install.backend import _mode
from paulsha_cortex.trust_root.install.core import (
    InstallReceipt,
    InstallPlanError,
    _account_digest,
    _desired_digest,
    new_install_receipt,
    rollback_receipt,
    validate_preflight,
)


def _completed(argv) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 0, "", "")


@pytest.mark.parametrize(
    ("returncode", "stdout", "load_returncode", "load_stdout", "expected"),
    [
        (0, "active\n", None, None, "active"),
        (3, "inactive\n", None, None, "inactive"),
        (3, "failed\n", None, None, "failed"),
        (4, "inactive\n", 0, "not-found\n", "not-found"),
        (4, "unknown\n", 0, "not-found\n", "not-found"),
        (4, "inactive\n", 1, "", "error"),
        (4, "inactive\n", 0, "loaded\n", "error"),
        (1, "", None, None, "error"),
        (0, "inactive\n", None, None, "error"),
    ],
)
def test_systemctl_is_active_state_requires_matching_returncode_and_stdout(
    returncode: int,
    stdout: str,
    load_returncode: int | None,
    load_stdout: str | None,
    expected: str,
) -> None:
    result = subprocess.CompletedProcess(
        ["systemctl", "is-active", "cortex-manager.service"],
        returncode,
        stdout,
        "Failed to connect to bus" if returncode == 1 else "",
    )
    load_state = (
        subprocess.CompletedProcess(
            ["systemctl", "show", "--property=LoadState", "--value"],
            load_returncode,
            load_stdout,
            "Failed to connect to bus" if load_returncode else "",
        )
        if load_returncode is not None and load_stdout is not None
        else None
    )

    assert backend_module._classify_systemctl_is_active(result, load_state) == expected


def test_mode_parser_accepts_registry_sticky_mode_and_rejects_invalid_values() -> None:
    assert _mode("1755") == 0o1755
    assert _mode("0700") == 0o700
    for invalid in ("755", "01755", "0788", "-700"):
        with pytest.raises(InstallPlanError, match="invalid mode"):
            _mode(invalid)


def test_repository_attestation_disables_root_owned_optional_index_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir(mode=0o755)
    (repository / "README.md").write_text("exact\n", encoding="utf-8")
    (repository / "CURRENT.md").symlink_to("README.md")
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    commit = "a" * 40
    remote = "https://github.com/hamanpaul/paulsha-cortex.git"
    git_dir = repository / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
        '[remote "origin"]\n'
        f"\turl = {remote}\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
        encoding="utf-8",
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(argv, **kwargs):
        command = tuple(argv)
        calls.append((command, kwargs))
        if command[-2:] == ("rev-parse", "HEAD"):
            return subprocess.CompletedProcess(command, 0, f"{commit}\n", "")
        if command[-3:] == ("remote", "get-url", "origin"):
            return subprocess.CompletedProcess(command, 0, f"{remote}\n", "")
        return _completed(command)

    monkeypatch.setattr(backend_module, "_run", run)
    step = {
        "path": str(repository),
        "owner": owner,
        "group": group,
        "mode": "0755",
        "commit": commit,
        "remote": remote,
        "desired_sha256": "d" * 64,
    }

    state = backend_module._repository_state(step)

    assert state["installed_sha256"] == step["desired_sha256"]
    assert calls
    for command, kwargs in calls:
        assert command[:2] == ("git", "--no-optional-locks")
        assert ("-c", "core.fsmonitor=false") == command[2:4]
        assert ("-c", "core.hooksPath=/dev/null") == command[4:6]
        assert kwargs["uid"] == os.getuid()
        assert kwargs["gid"] == os.getgid()
        assert kwargs["env"] == {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": os.defpath,
        }


def test_repository_attestation_rejects_noncanonical_fsmonitor_without_execution(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    git_dir = repository / ".git"
    git_dir.mkdir(parents=True)
    marker = tmp_path / "fsmonitor-executed"
    probe = tmp_path / "malicious-fsmonitor"
    probe.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    probe.chmod(0o755)
    remote = "https://github.com/hamanpaul/paulsha-cortex.git"
    (git_dir / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
        f"\tfsmonitor = {probe}\n"
        '[remote "origin"]\n'
        f"\turl = {remote}\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n",
        encoding="utf-8",
    )
    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name

    state = backend_module._repository_state(
        {
            "path": str(repository),
            "owner": owner,
            "group": group,
            "mode": "0755",
            "commit": "a" * 40,
            "remote": remote,
            "desired_sha256": "d" * 64,
        }
    )

    assert state["installed_sha256"] is None
    assert state["config_safe"] is False
    assert not marker.exists(), "repository inspection must not execute local fsmonitor"


def test_repository_install_isolates_every_root_git_call_from_host_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fixture_git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            env=backend_module._REPOSITORY_GIT_ENV,
        )

    source = tmp_path / "source"
    source.mkdir()
    fixture_git("git", "init", "--quiet", str(source))
    fixture_git("git", "-C", str(source), "config", "user.name", "Cortex Test")
    fixture_git(
        "git",
        "-C",
        str(source),
        "config",
        "user.email",
        "cortex@example.invalid",
    )
    (source / "README.md").write_text("locked\n", encoding="utf-8")
    fixture_git("git", "-C", str(source), "add", "README.md")
    fixture_git("git", "-C", str(source), "commit", "--quiet", "-m", "fixture")
    commit = fixture_git("git", "-C", str(source), "rev-parse", "HEAD").stdout.strip()
    bundle = tmp_path / "source.bundle"
    fixture_git("git", "-C", str(source), "bundle", "create", str(bundle), "HEAD")

    marker = tmp_path / "host-hook-executed"
    hooks = tmp_path / "host-hooks"
    hooks.mkdir()
    post_checkout = hooks / "post-checkout"
    post_checkout.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    post_checkout.chmod(0o755)
    hostile_global = tmp_path / "hostile-global-config"
    hostile_global.write_text(
        f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))

    owner = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    destination = tmp_path / "installed"
    remote = "https://github.com/hamanpaul/paulsha-cortex.git"
    step = {
        "step_id": "repository:paulsha-cortex",
        "kind": "repository",
        "slug": "paulsha-cortex",
        "source": str(bundle),
        "source_sha256": backend_module._sha256_file(bundle),
        "path": str(destination),
        "owner": owner,
        "group": group,
        "mode": "0755",
        "commit": commit,
        "remote": remote,
        "desired_sha256": "d" * 64,
    }
    original_run = backend_module._run
    git_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def spy_run(argv, **kwargs):
        if argv[0] == "git":
            git_calls.append((tuple(argv), dict(kwargs)))
        return original_run(argv, **kwargs)

    monkeypatch.setattr(backend_module, "_run", spy_run)

    result = LocalInstallBackend(require_root=False).apply_step(step)

    assert result["installed_sha256"] == step["desired_sha256"]
    # clone/checkout/set-url, then inspection: rev-parse, remote, status, fsck,
    # and the runtime ref allowlist listing (#1124).
    assert len(git_calls) == 8
    assert git_calls[-1][0][-2] == "for-each-ref"
    for argv, kwargs in git_calls:
        assert argv[:6] == (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
        )
        assert kwargs["env"] == backend_module._REPOSITORY_GIT_ENV
    assert not marker.exists(), "host global hooksPath must never execute"


def test_account_step_creates_exact_group_and_user_through_typed_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    users: dict[str, SimpleNamespace] = {}
    groups: dict[str, SimpleNamespace] = {}
    calls: list[tuple[str, ...]] = []

    def get_user(name: str):
        if name not in users:
            raise KeyError(name)
        return users[name]

    def get_group(name: str):
        if name not in groups:
            raise KeyError(name)
        return groups[name]

    def run(argv, **_kwargs):
        command = tuple(argv)
        calls.append(command)
        if command[0] == "groupadd":
            name = command[-1]
            groups[name] = SimpleNamespace(gr_gid=int(command[2]))
        elif command[0] == "useradd":
            name = command[-1]
            users[name] = SimpleNamespace(
                pw_uid=int(command[2]),
                pw_gid=int(command[4]),
                pw_dir=command[6],
                pw_shell=command[8],
            )
        return _completed(command)

    monkeypatch.setattr(backend_module.pwd, "getpwnam", get_user)
    monkeypatch.setattr(backend_module.grp, "getgrnam", get_group)
    monkeypatch.setattr(backend_module, "_run", run)
    identity = {
        "name": "cortex-builder",
        "uid": 993,
        "gid": 993,
        "home": str(tmp_path / "var/lib/cortex-builder"),
        "login_program": "/usr/sbin/nologin",
    }
    step = {
        "step_id": "account:cortex-builder",
        "kind": "account",
        **identity,
        "desired_sha256": _account_digest(identity),
    }

    result = LocalInstallBackend(require_root=False).apply_step(step)

    assert result["installed_sha256"] == step["desired_sha256"]
    assert calls == [
        ("groupadd", "--gid", "993", "--system", "cortex-builder"),
        (
            "useradd",
            "--uid",
            "993",
            "--gid",
            "993",
            "--home-dir",
            identity["home"],
            "--shell",
            "/usr/sbin/nologin",
            "--no-create-home",
            "--system",
            "cortex-builder",
        ),
    ]


def test_account_state_distinguishes_exact_orphan_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_user(name: str):
        raise KeyError(name)

    monkeypatch.setattr(backend_module.pwd, "getpwnam", missing_user)
    monkeypatch.setattr(
        backend_module.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_name=name, gr_gid=993, gr_mem=[]),
    )
    step = {
        "kind": "account",
        "name": "cortex-builder",
        "gid": 993,
    }

    assert LocalInstallBackend(require_root=False).inspect_step(step) == {
        "exists": False,
        "group_exists": True,
        "group_gid": 993,
        "group_members": [],
    }


@pytest.mark.parametrize(
    "late_state",
    [
        {"exists": True, "installed_sha256": "d" * 64},
        {
            "exists": False,
            "group_exists": True,
            "group_gid": 993,
            "group_members": [],
        },
    ],
)
def test_account_backend_rejects_a_late_exact_account_or_group(
    monkeypatch: pytest.MonkeyPatch,
    late_state: dict[str, object],
) -> None:
    step = {
        "step_id": "account:cortex-builder",
        "kind": "account",
        "name": "cortex-builder",
        "uid": 993,
        "gid": 993,
        "home": "/var/lib/cortex-builder",
        "login_program": "/usr/sbin/nologin",
        "desired_sha256": "d" * 64,
    }
    monkeypatch.setattr(
        backend_module,
        "_account_state",
        lambda _step: dict(late_state),
    )

    with pytest.raises(InstallDriftError, match="backend boundary"):
        LocalInstallBackend(require_root=False).apply_step_checkpointed(
            step,
            {"exists": False, "group_exists": False},
            lambda _authority: None,
        )


def test_asset_backend_rejects_a_leaf_that_appears_after_core_inspection(
    tmp_path: Path,
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    path = tmp_path / "late-unit.service"
    path.write_text("foreign but exact-looking\n", encoding="utf-8")
    step = {
        "step_id": "asset:late-unit",
        "kind": "asset",
        "asset_type": "file",
        "path": str(path),
        "content": "planned\n",
        "owner": account,
        "group": group,
        "mode": "0600",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)

    with pytest.raises(InstallDriftError, match="backend boundary"):
        LocalInstallBackend(require_root=False).apply_step_checkpointed(
            step,
            {"exists": False},
            lambda _authority: None,
        )

    assert path.read_text(encoding="utf-8") == "foreign but exact-looking\n"


def test_venv_step_verifies_locked_wheels_and_atomically_switches_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "candidate.whl"
    dependency = wheelhouse / "dependency.whl"
    wheel.write_bytes(b"candidate")
    dependency.write_bytes(b"dependency")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    dependency_sha = hashlib.sha256(dependency.read_bytes()).hexdigest()
    deploy = tmp_path / "opt/cortex"
    old_slot = deploy / "venvs/old"
    old_slot.mkdir(parents=True)
    active = deploy / "venv"
    active.symlink_to("venvs/old")
    calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        command = tuple(argv)
        calls.append(command)
        if command[:3] == ("python3", "-m", "venv"):
            temporary = Path(command[3])
            (temporary / "bin").mkdir(parents=True)
            (temporary / "bin/python").write_text(
                "verified interpreter", encoding="utf-8"
            )
            (temporary / "bin/cortex").write_text(
                f"#!{temporary}/bin/python\nprint('cortex')\n", encoding="utf-8"
            )
        return _completed(command)

    monkeypatch.setattr(backend_module, "_run", run)
    step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(deploy / "venvs" / wheel_sha),
        "active_link": str(active),
        "wheel_source": str(wheel),
        "wheel_sha256": wheel_sha,
        "wheelhouse": [
            {"source": str(wheel), "sha256": wheel_sha},
            {"source": str(dependency), "sha256": dependency_sha},
        ],
        "wheelhouse_locked": True,
        "desired_sha256": wheel_sha,
    }
    backend = LocalInstallBackend(require_root=False)

    result = backend.apply_step(step)

    assert result["installed_sha256"] == wheel_sha
    assert result["tree_sha256"] == backend_module._tree_sha256(Path(step["path"]))
    assert active.resolve() == Path(step["path"])
    assert (Path(step["path"]) / ".cortex-wheel.sha256").read_text().strip() == wheel_sha
    assert (Path(step["path"]) / "bin/cortex").read_text().splitlines()[0] == (
        f"#!{step['path']}/bin/python"
    )
    assert any("--no-index" in call for call in calls)
    first_call_count = len(calls)
    backend.apply_step(step)
    assert len(calls) == first_call_count, "matching content-addressed venv is adopted"

    backend.rollback_step({"step": step, "prior": {"exists": True, "link_target": "venvs/old"}})
    assert active.readlink() == Path("venvs/old")
    assert Path(step["path"]).is_dir(), "rollback retains the verified candidate slot"


def test_venv_step_rejects_a_wheelhouse_hash_mismatch_before_running_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        backend_module,
        "_run",
        lambda argv, **_kwargs: calls.append(tuple(argv)) or _completed(argv),
    )
    step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(tmp_path / "opt/cortex/venvs" / wheel_sha),
        "active_link": str(tmp_path / "opt/cortex/venv"),
        "wheel_source": str(wheel),
        "wheel_sha256": wheel_sha,
        "wheelhouse": [{"source": str(wheel), "sha256": "0" * 64}],
        "wheelhouse_locked": True,
        "desired_sha256": wheel_sha,
    }

    with pytest.raises(InstallDriftError, match="hash-mismatched"):
        LocalInstallBackend(require_root=False).apply_step(step)
    assert calls == []


def test_venv_step_replays_after_slot_rename_before_link_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    slot = tmp_path / "opt/cortex/venvs" / wheel_sha
    (slot / "bin").mkdir(parents=True)
    (slot / "bin/python").write_text("verified interpreter", encoding="utf-8")
    (slot / ".cortex-wheel.sha256").write_text(wheel_sha + "\n", encoding="ascii")
    (slot / ".cortex-tree.sha256").write_text(
        backend_module._tree_sha256(slot) + "\n", encoding="ascii"
    )
    active = tmp_path / "opt/cortex/venv"
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        backend_module,
        "_run",
        lambda argv, **_kwargs: calls.append(tuple(argv)) or _completed(argv),
    )
    step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(slot),
        "active_link": str(active),
        "wheel_source": str(wheel),
        "wheel_sha256": wheel_sha,
        "wheelhouse": [{"source": str(wheel), "sha256": wheel_sha}],
        "wheelhouse_locked": True,
        "desired_sha256": wheel_sha,
    }

    result = LocalInstallBackend(require_root=False).apply_step(step)

    assert result["installed_sha256"] == wheel_sha
    assert active.resolve() == slot
    assert calls == [], "a verified interrupted slot is adopted without reinstall"


def test_venv_slot_is_receipt_bound_before_final_name_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    slot = tmp_path / "opt/cortex/venvs" / wheel_sha
    active = tmp_path / "opt/cortex/venv"

    def run(argv, **_kwargs):
        command = tuple(argv)
        if command[:3] == ("python3", "-m", "venv"):
            temporary = Path(command[3])
            (temporary / "bin").mkdir()
            (temporary / "bin/python").write_text("python", encoding="utf-8")
        return _completed(command)

    monkeypatch.setattr(backend_module, "_run", run)
    step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(slot),
        "active_link": str(active),
        "wheel_source": str(wheel),
        "wheel_sha256": wheel_sha,
        "wheelhouse": [{"source": str(wheel), "sha256": wheel_sha}],
        "wheelhouse_locked": True,
        "desired_sha256": wheel_sha,
    }
    authorities: list[dict[str, object]] = []

    def checkpoint(authority: Mapping[str, object]) -> None:
        first_ready = not any(row.get("state") == "ready" for row in authorities)
        authorities.append(dict(authority))
        if authority.get("state") == "ready" and first_ready:
            assert not slot.exists()
            assert Path(str(authority["staging_path"])).is_dir()

    result = LocalInstallBackend(require_root=False).apply_step_checkpointed(
        step, {"exists": False}, checkpoint
    )

    assert result["installed_sha256"] == wheel_sha
    assert [row["state"] for row in authorities] == [
        "planned",
        "building",
        "ready",
        "ready",
    ]
    assert slot.is_dir()
    assert active.resolve() == slot


def test_venv_post_rename_interruption_replays_from_prepublished_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    slot = tmp_path / "opt/cortex/venvs" / wheel_sha
    active = tmp_path / "opt/cortex/venv"

    def run(argv, **_kwargs):
        command = tuple(argv)
        if command[:3] == ("python3", "-m", "venv"):
            temporary = Path(command[3])
            (temporary / "bin").mkdir()
            (temporary / "bin/python").write_text("python", encoding="utf-8")
        return _completed(command)

    monkeypatch.setattr(backend_module, "_run", run)
    step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(slot),
        "active_link": str(active),
        "wheel_source": str(wheel),
        "wheel_sha256": wheel_sha,
        "wheelhouse": [{"source": str(wheel), "sha256": wheel_sha}],
        "wheelhouse_locked": True,
        "desired_sha256": wheel_sha,
    }
    authorities: list[dict[str, object]] = []
    real_rename = backend_module.os.rename

    def rename_then_interrupt(source, destination):
        real_rename(source, destination)
        raise SystemExit("simulated interruption after final-slot rename")

    monkeypatch.setattr(backend_module.os, "rename", rename_then_interrupt)
    backend = LocalInstallBackend(require_root=False)
    with pytest.raises(SystemExit, match="after final-slot rename"):
        backend.apply_step_checkpointed(
            step,
            {"exists": False},
            lambda authority: authorities.append(dict(authority)),
        )

    assert slot.is_dir()
    assert not active.exists()
    assert authorities[-1]["state"] == "ready"
    durable = dict(authorities[-1])
    monkeypatch.setattr(backend_module.os, "rename", real_rename)
    replayed: list[dict[str, object]] = []
    result = backend.apply_step_checkpointed(
        step,
        {"exists": False},
        lambda authority: replayed.append(dict(authority)),
    )
    assert replayed == [durable, durable]
    assert result["installed_sha256"] == wheel_sha
    assert active.resolve() == slot


@pytest.mark.parametrize("parent_existed_before", [False, True])
def test_unknown_scanner_excludes_retained_receipt_bound_venv_slot(
    tmp_path: Path, parent_existed_before: bool
) -> None:
    venvs = tmp_path / "opt/cortex/venvs"
    slot = venvs / ("a" * 64)
    (slot / "bin").mkdir(parents=True)
    (slot / "bin/python").write_text("python", encoding="utf-8")
    directory_step = {
        "step_id": "asset:venvs",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(venvs),
    }
    venv_step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(slot),
    }
    receipt = InstallReceipt(
        {
            "journal": [],
            "rollback_journal": [
                {
                    "step": directory_step,
                    "prior": (
                        {"exists": True, "children": []}
                        if parent_existed_before
                        else {"exists": False}
                    ),
                },
                {"step": venv_step, "prior": {"exists": False}},
            ],
        }
    )

    assert LocalInstallBackend(require_root=False).list_unknown_state(receipt) == ()


def test_unknown_scanner_keeps_foreign_sibling_of_retained_managed_subtree(
    tmp_path: Path,
) -> None:
    managed_root = tmp_path / "opt/cortex"
    slot = managed_root / "venvs" / ("a" * 64)
    (slot / "bin").mkdir(parents=True)
    (slot / "bin/python").write_text("python", encoding="utf-8")
    unknown = managed_root / "operator" / "keep.txt"
    unknown.parent.mkdir()
    unknown.write_text("keep\n", encoding="utf-8")
    receipt = InstallReceipt(
        {
            "journal": [],
            "rollback_journal": [
                {
                    "step": {
                        "step_id": "asset:deploy-root",
                        "kind": "asset",
                        "asset_type": "directory",
                        "path": str(managed_root),
                    },
                    "prior": {"exists": False},
                },
                {
                    "step": {
                        "step_id": "candidate-venv",
                        "kind": "venv",
                        "path": str(slot),
                    },
                    "prior": {"exists": False},
                },
            ],
        }
    )

    retained = LocalInstallBackend(require_root=False).list_unknown_state(receipt)

    assert str(unknown) in retained
    assert str(managed_root) not in retained


def test_unknown_scanner_excludes_receipt_bound_fresh_repository_from_parent(
    tmp_path: Path,
) -> None:
    repositories = tmp_path / "var/lib/cortex/repos"
    checkout = repositories / "example"
    checkout.mkdir(parents=True)
    (checkout / "HEAD").write_text("candidate\n", encoding="utf-8")
    receipt = InstallReceipt(
        {
            "journal": [],
            "rollback_journal": [
                {
                    "step": {
                        "step_id": "asset:repositories",
                        "kind": "asset",
                        "asset_type": "directory",
                        "path": str(repositories),
                    },
                    "prior": {"exists": False},
                },
                {
                    "step": {
                        "step_id": "repository:example",
                        "kind": "repository",
                        "path": str(checkout),
                    },
                    "prior": {"exists": False},
                },
            ],
        }
    )

    assert LocalInstallBackend(require_root=False).list_unknown_state(receipt) == ()


def test_directory_acl_attestation_accounts_for_posix_mask(
    tmp_path: Path,
) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("requires acl tools")
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    path = tmp_path / "control"
    step = {
        "step_id": "asset:control-root-tree",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(path),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [
            {"account": "root", "perms": "rX", "default": False},
            {"account": "root", "perms": "rX", "default": True},
        ],
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)

    result = backend.apply_step(step)

    assert result["installed_sha256"] == step["desired_sha256"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o750
    assert backend.inspect_step(step)["installed_sha256"] == step["desired_sha256"]
    assert backend.apply_step(step)["installed_sha256"] == step["desired_sha256"]


@pytest.mark.parametrize("asset_type", ["file", "directory"])
def test_new_asset_checkpoints_exact_inode_before_followup_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asset_type: str,
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    path = tmp_path / f"managed-{asset_type}"
    step = {
        "step_id": f"asset:{asset_type}",
        "kind": "asset",
        "asset_type": asset_type,
        "path": str(path),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [],
    }
    if asset_type == "file":
        step["content"] = "managed\n"
    step["desired_sha256"] = _desired_digest(step)
    checkpoints: list[dict[str, object]] = []

    def checkpoint(authority) -> None:
        observed = path.lstat()
        assert authority == {
            "device": observed.st_dev,
            "inode": observed.st_ino,
            "file_type": asset_type,
        }
        checkpoints.append(dict(authority))

    def run(argv, **_kwargs):
        assert checkpoints, "inode authority must be durable before ACL mutation"
        return _completed(argv)

    monkeypatch.setattr(backend_module, "_run", run)
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    backend = LocalInstallBackend(require_root=False)

    outcome = backend.apply_step_checkpointed(
        step,
        {"exists": False},
        checkpoint,
    )

    assert checkpoints == [outcome["creation_authority"]]
    assert outcome["installed_sha256"] == step["desired_sha256"]
    assert backend.creation_authority_matches(step, checkpoints[0])


def test_directory_acl_attestation_rejects_an_undeclared_named_group(
    tmp_path: Path,
) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("requires acl tools")
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    path = tmp_path / "control"
    step = {
        "step_id": "asset:control-root-tree",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(path),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)
    backend.apply_step(step)
    subprocess.run(
        ("setfacl", "-m", f"g:{group}:rwx", str(path)),
        check=True,
        capture_output=True,
        text=True,
    )

    assert backend.inspect_step(step).get("installed_sha256") is None


def test_venv_requires_locked_manifest_and_installed_tree_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"candidate")
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    deploy = tmp_path / "opt/cortex"

    def run(argv, **_kwargs):
        command = tuple(argv)
        if command[:3] == ("python3", "-m", "venv"):
            (Path(command[3]) / "bin").mkdir(parents=True)
            (Path(command[3]) / "bin/python").write_text("python", encoding="utf-8")
        return _completed(command)

    monkeypatch.setattr(backend_module, "_run", run)
    step = {
        "step_id": "candidate-venv",
        "kind": "venv",
        "path": str(deploy / "venvs" / wheel_sha),
        "active_link": str(deploy / "venv"),
        "wheel_source": str(wheel),
        "wheel_sha256": wheel_sha,
        "wheelhouse": [{"source": str(wheel), "sha256": wheel_sha}],
        "wheelhouse_locked": False,
        "desired_sha256": wheel_sha,
    }
    backend = LocalInstallBackend(require_root=False)
    with pytest.raises(InstallPlanError, match="locked"):
        backend.apply_step(step)

    step["wheelhouse_locked"] = True
    backend.apply_step(step)
    assert stat.S_IMODE(Path(step["path"]).stat().st_mode) == 0o755
    (Path(step["path"]) / "bin/python").write_text("tampered", encoding="utf-8")
    assert backend.inspect_step(step).get("installed_sha256") is None


def test_new_directory_drops_inherited_acl_not_declared_by_plan(
    tmp_path: Path,
) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("requires acl tools")
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    parent = tmp_path / "control"
    parent.mkdir()
    subprocess.run(
        ("setfacl", "-m", "d:u:root:rx", str(parent)),
        check=True,
        capture_output=True,
        text=True,
    )
    path = parent / "requests"
    step = {
        "step_id": "asset:control-request-queue",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(path),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)

    result = backend.apply_step(step)

    assert result["installed_sha256"] == step["desired_sha256"]
    assert backend.inspect_step(step)["observed_acl"] == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_existing_directory_drift_is_not_overwritten(tmp_path: Path) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    path = tmp_path / "existing"
    path.mkdir(mode=0o755)
    step = {
        "step_id": "asset:existing",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(path),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)

    with pytest.raises(InstallDriftError, match="existing asset"):
        LocalInstallBackend(require_root=False).apply_step(step)

    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def test_file_replacement_binds_prior_inode_and_rolls_back_exact_bytes(
    tmp_path: Path,
) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("requires acl tools")
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    path = tmp_path / "cortex-manager.service"
    path.write_text("old unit\n", encoding="utf-8")
    path.chmod(0o640)
    step = {
        "step_id": "asset:cortex-manager-unit",
        "kind": "asset",
        "asset_type": "file",
        "path": str(path),
        "content": "new unit\n",
        "owner": account,
        "group": group,
        "mode": "0600",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)
    prior = dict(backend.inspect_step(step))
    old_inode = path.stat().st_ino
    checkpoints: list[dict[str, object]] = []

    outcome = backend.replace_step_checkpointed(
        step,
        prior,
        lambda authority: checkpoints.append(dict(authority)),
    )

    assert path.read_text(encoding="utf-8") == "new unit\n"
    assert outcome["installed_sha256"] == step["desired_sha256"]
    assert checkpoints == [outcome["replacement_authority"]]
    assert checkpoints[0]["inode"] == old_inode
    assert path.stat().st_ino != old_inode

    backend.rollback_step(
        {
            "step_id": step["step_id"],
            "step": step,
            "status": "completed",
            "prior": prior,
            "replacement_authority": checkpoints[0],
            **outcome,
        }
    )

    assert path.read_text(encoding="utf-8") == "old unit\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_rollback_restoring_a_unit_file_ends_with_systemd_daemon_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    path = systemd / "cortex-manager.service"
    path.write_text("old unit\n", encoding="utf-8")
    path.chmod(0o640)
    step = {
        "step_id": "generated:units/cortex-manager.service",
        "kind": "asset",
        "asset_type": "file",
        "path": str(path),
        "content": "new unit\n",
        "owner": account,
        "group": group,
        "mode": "0600",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    commands: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        commands.append(tuple(argv))
        return _completed(argv)

    monkeypatch.setattr(backend_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(backend_module, "_run", run)
    backend = LocalInstallBackend(require_root=False)
    prior = dict(backend.inspect_step(step))
    outcome = backend.replace_step_checkpointed(step, prior, lambda _authority: None)
    receipt = InstallReceipt(
        {
            "plan": {"roots": {"systemd": str(systemd)}},
            "state": "applied",
            "journal": [
                {
                    "step_id": step["step_id"],
                    "step": step,
                    "status": "completed",
                    "prior": prior,
                    **outcome,
                }
            ],
            "services_started": False,
            "credentials": [],
        }
    )
    commands.clear()

    report = rollback_receipt(receipt, backend=backend)

    assert path.read_text(encoding="utf-8") == "old unit\n"
    systemctl_calls = [row for row in commands if row[0] == "systemctl"]
    assert systemctl_calls == [("systemctl", "daemon-reload")]
    assert commands[-1] == ("systemctl", "daemon-reload")
    assert report.systemd_daemon_reload == "completed"
    assert receipt.to_dict()["state"] == "rolled-back"


def test_systemd_reload_failure_is_raised_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            list(argv), 1, "", "Failed to reload daemon: Access denied"
        )

    monkeypatch.setattr(backend_module.subprocess, "run", run)

    with pytest.raises(backend_module.InstallError, match="Access denied"):
        LocalInstallBackend(require_root=False).reload_systemd_units()

    assert calls == [["systemctl", "daemon-reload"]]


@pytest.mark.parametrize("staging_exact", [True, False])
def test_prepared_file_replacement_never_false_cleans_a_staging_leaf(
    tmp_path: Path,
    staging_exact: bool,
) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("requires acl tools")
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    path = tmp_path / "cortex-monitor.service"
    path.write_text("old unit\n", encoding="utf-8")
    path.chmod(0o640)
    step = {
        "step_id": "asset:cortex-monitor-unit",
        "kind": "asset",
        "asset_type": "file",
        "path": str(path),
        "content": "new unit\n",
        "owner": account,
        "group": group,
        "mode": "0600",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)
    prior = dict(backend.inspect_step(step))
    authority = backend_module._snapshot_authority(
        path.stat(), file_type="file"
    )
    staging = backend_module._replacement_staging_path(step)
    staging.write_text(
        "new unit\n" if staging_exact else "partial write",
        encoding="utf-8",
    )
    staging.chmod(0o600)
    receipt = InstallReceipt(
        {
            "state": "applying",
            "journal": [
                {
                    "step_id": step["step_id"],
                    "step": step,
                    "status": "prepared",
                    "prior": prior,
                    "adopted_from_receipt": True,
                    "replacement_authority": authority,
                }
            ],
            "services_started": False,
            "credentials": [],
        }
    )

    report = rollback_receipt(receipt, backend=backend)

    assert path.read_text(encoding="utf-8") == "old unit\n"
    if staging_exact:
        assert not staging.exists()
        assert report.retained_drift == ()
        assert receipt.to_dict()["state"] == "rolled-back"
    else:
        assert staging.read_text(encoding="utf-8") == "partial write"
        assert report.retained_drift[0]["step_id"] == step["step_id"]
        assert receipt.to_dict()["state"] == "rollback-blocked"


def test_repository_replacement_upgrades_and_restores_exact_prior_commit(
    tmp_path: Path,
) -> None:
    def fixture_git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            env=backend_module._REPOSITORY_GIT_ENV,
        )

    source = tmp_path / "source"
    source.mkdir()
    fixture_git("git", "init", "--quiet", str(source))
    fixture_git("git", "-C", str(source), "config", "user.name", "Cortex Test")
    fixture_git(
        "git",
        "-C",
        str(source),
        "config",
        "user.email",
        "cortex@example.invalid",
    )
    readme = source / "README.md"
    readme.write_text("old\n", encoding="utf-8")
    fixture_git("git", "-C", str(source), "add", "README.md")
    fixture_git("git", "-C", str(source), "commit", "--quiet", "-m", "old")
    old_commit = fixture_git(
        "git", "-C", str(source), "rev-parse", "HEAD"
    ).stdout.strip()
    readme.write_text("new\n", encoding="utf-8")
    fixture_git("git", "-C", str(source), "commit", "--quiet", "-am", "new")
    new_commit = fixture_git(
        "git", "-C", str(source), "rev-parse", "HEAD"
    ).stdout.strip()
    bundle = tmp_path / "source.bundle"
    fixture_git("git", "-C", str(source), "bundle", "create", str(bundle), "HEAD")

    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    destination = tmp_path / "installed"
    remote = "https://github.com/hamanpaul/paulsha-cortex.git"

    def repository_step(commit: str, digest: str) -> dict[str, object]:
        return {
            "step_id": "repository:paulsha-cortex",
            "kind": "repository",
            "slug": "paulsha-cortex",
            "source": str(bundle),
            "source_sha256": backend_module._sha256_file(bundle),
            "path": str(destination),
            "owner": account,
            "group": group,
            "mode": "0755",
            "commit": commit,
            "remote": remote,
            "desired_sha256": digest,
        }

    backend = LocalInstallBackend(require_root=False)
    old_step = repository_step(old_commit, "a" * 64)
    new_step = repository_step(new_commit, "b" * 64)
    backend.apply_step(old_step)
    prior = dict(backend.inspect_step(new_step))
    prior_inode = destination.stat().st_ino
    checkpoints: list[dict[str, object]] = []

    outcome = backend.replace_step_checkpointed(
        new_step,
        prior,
        lambda authority: checkpoints.append(dict(authority)),
    )

    assert outcome["installed_sha256"] == new_step["desired_sha256"]
    assert outcome["commit"] == new_commit
    assert (destination / "README.md").read_text(encoding="utf-8") == "new\n"
    assert checkpoints[0]["inode"] == prior_inode
    assert backend.creation_authority_matches(new_step, checkpoints[0])

    backend.rollback_step(
        {
            "step_id": new_step["step_id"],
            "step": new_step,
            "status": "completed",
            "prior": prior,
            "replacement_authority": checkpoints[0],
            **outcome,
        }
    )

    restored = backend.inspect_step(old_step)
    assert restored["installed_sha256"] == old_step["desired_sha256"]
    assert restored["commit"] == old_commit
    assert (destination / "README.md").read_text(encoding="utf-8") == "old\n"


# ---------------------------------------------------------------------------
# #1124：Manager 執行期寫進來源樹的 branch／ref／linked worktree allowlist
# ---------------------------------------------------------------------------


def test_repository_state_reconciles_manager_runtime_state(tmp_path: Path) -> None:
    upgrade = repository_runtime_fixtures.repository_upgrade(tmp_path)
    repository_runtime_fixtures.simulate_manager_runtime(upgrade)

    state = LocalInstallBackend(require_root=False).inspect_step(upgrade.old_step)

    assert state["drift"] == []
    assert state["clean"] is True
    assert state["installed_sha256"] == upgrade.old_step["desired_sha256"]
    runtime = state["runtime"]
    assert runtime["config"] == ["user.email", "user.name"]
    assert {(row["path"], row["present"]) for row in runtime["worktrees"]} == {
        (repository_runtime_fixtures.REVIEW_WORKTREE, True),
        (f".psc-verification-worktrees/s1124-{upgrade.old_commit[:12]}", False),
    }
    assert runtime["refs_count"] == len(
        repository_runtime_fixtures.refs(upgrade.repository)
    )


def test_repository_replacement_keeps_manager_runtime_state_through_rollback(
    tmp_path: Path,
) -> None:
    upgrade = repository_runtime_fixtures.repository_upgrade(tmp_path)
    repository_runtime_fixtures.simulate_manager_runtime(upgrade)
    review = upgrade.repository / repository_runtime_fixtures.REVIEW_WORKTREE
    refs_before = repository_runtime_fixtures.refs(upgrade.repository)
    backend = LocalInstallBackend(require_root=False)
    prior = dict(backend.inspect_step(upgrade.new_step))
    checkpoints: list[dict[str, object]] = []

    outcome = backend.replace_step_checkpointed(
        upgrade.new_step,
        prior,
        lambda authority: checkpoints.append(dict(authority)),
    )

    assert outcome["installed_sha256"] == upgrade.new_step["desired_sha256"]
    assert outcome["commit"] == upgrade.new_commit
    assert outcome["runtime"] == prior["runtime"]
    assert (upgrade.repository / "README.md").read_text(encoding="utf-8") == "new\n"
    # The Manager's review checkout, branches, and refs are untouched.
    assert (review / "README.md").read_text(encoding="utf-8") == "old\n"
    assert repository_runtime_fixtures.git(
        "-C", str(review), "rev-parse", "HEAD"
    ) == upgrade.old_commit
    assert repository_runtime_fixtures.refs(upgrade.repository) == refs_before

    backend.rollback_step(
        {
            "step_id": upgrade.new_step["step_id"],
            "step": upgrade.new_step,
            "status": "completed",
            "prior": prior,
            "replacement_authority": checkpoints[0],
            **outcome,
        }
    )

    restored = backend.inspect_step(upgrade.old_step)
    assert restored["installed_sha256"] == upgrade.old_step["desired_sha256"]
    assert restored["runtime"] == prior["runtime"]
    assert (upgrade.repository / "README.md").read_text(encoding="utf-8") == "old\n"
    assert (review / "README.md").read_text(encoding="utf-8") == "old\n"
    assert repository_runtime_fixtures.refs(upgrade.repository) == refs_before


def test_repository_replacement_refuses_candidate_tracking_runtime_worktree_root(
    tmp_path: Path,
) -> None:
    upgrade = repository_runtime_fixtures.repository_upgrade(
        tmp_path,
        candidate_files={".psc-review-worktrees/s1124-review-1124/README.md": "x\n"},
    )
    repository_runtime_fixtures.simulate_manager_runtime(upgrade)
    review = upgrade.repository / repository_runtime_fixtures.REVIEW_WORKTREE
    backend = LocalInstallBackend(require_root=False)
    prior = dict(backend.inspect_step(upgrade.new_step))

    with pytest.raises(InstallDriftError, match="runtime worktree root"):
        backend.replace_step_checkpointed(upgrade.new_step, prior, lambda _row: None)

    assert repository_runtime_fixtures.git(
        "-C", str(upgrade.repository), "rev-parse", "HEAD"
    ) == upgrade.old_commit
    assert (review / "README.md").read_text(encoding="utf-8") == "old\n"


def _drift_undeclared_config(key: str, value: str):
    def mutate(upgrade, _monkeypatch) -> None:
        repository_runtime_fixtures.git(
            "-C", str(upgrade.repository), "config", "--local", key, value
        )

    return mutate


def _drift_outside_worktree(upgrade, _monkeypatch) -> None:
    repository_runtime_fixtures.git(
        "-C",
        str(upgrade.repository),
        "worktree",
        "add",
        "--detach",
        str(upgrade.repository.parent / "elsewhere"),
        upgrade.old_commit,
    )


def _drift_in_tree_undeclared_worktree(upgrade, _monkeypatch) -> None:
    repository_runtime_fixtures.git(
        "-C",
        str(upgrade.repository),
        "worktree",
        "add",
        "--detach",
        str(upgrade.repository / "scratch"),
        upgrade.old_commit,
    )


def _drift_stray_runtime_root_entry(upgrade, _monkeypatch) -> None:
    (upgrade.repository / ".psc-review-worktrees" / "notes.txt").write_text(
        "operator note\n", encoding="utf-8"
    )


def _drift_worktree_metadata(upgrade, _monkeypatch) -> None:
    admin = upgrade.repository / ".git" / "worktrees" / "s1124-review-1124"
    (admin / "config.worktree").write_text(
        "[core]\n\thooksPath = /dev/null\n", encoding="utf-8"
    )


def _drift_undeclared_ref(refname: str):
    def mutate(upgrade, _monkeypatch) -> None:
        repository_runtime_fixtures.git(
            "-C", str(upgrade.repository), "update-ref", refname, upgrade.new_commit
        )

    return mutate


def _drift_world_writable_runtime_file(upgrade, _monkeypatch) -> None:
    target = (
        upgrade.repository / repository_runtime_fixtures.REVIEW_WORKTREE / "README.md"
    )
    target.chmod(0o646)


def _drift_foreign_owner(upgrade, monkeypatch) -> None:
    # An operator-owned file: the test account cannot chown, so the no-follow
    # stat the inspector performs reports a different uid for this one leaf.
    target = upgrade.repository / "README.md"
    real_lstat = Path.lstat

    def lstat(self: Path):
        observed = real_lstat(self)
        if self == target:
            values = list(observed)
            values[stat.ST_UID] = observed.st_uid + 4242
            return os.stat_result(values)
        return observed

    monkeypatch.setattr(Path, "lstat", lstat)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_drift_undeclared_config("core.hooksPath", "/dev/null"), "core.hookspath"),
        (_drift_undeclared_config("core.fsmonitor", "true"), "core.fsmonitor"),
        (_drift_undeclared_config("include.path", "/dev/null"), "include.path"),
        (_drift_undeclared_config("alias.co", "!true"), "alias.co"),
        (_drift_undeclared_config("filter.lfs.smudge", "cat"), "filter.lfs.smudge"),
        (_drift_undeclared_config("credential.helper", "store"), "credential.helper"),
        (
            _drift_undeclared_config("url.https://example.invalid/.insteadOf", "x"),
            "url.https://example.invalid/.insteadof",
        ),
        (
            _drift_undeclared_config(
                f"branch.{repository_runtime_fixtures.BUILD_BRANCH}.remote", "origin"
            ),
            f"branch.{repository_runtime_fixtures.BUILD_BRANCH}.remote",
        ),
        (_drift_undeclared_config("user.signingKey", "x"), "user.signingkey"),
        (_drift_outside_worktree, ".git/worktrees/elsewhere"),
        (_drift_in_tree_undeclared_worktree, ".git/worktrees/scratch"),
        (_drift_stray_runtime_root_entry, ".psc-review-worktrees/notes.txt"),
        (_drift_worktree_metadata, "config.worktree"),
        (_drift_undeclared_ref("refs/heads/operator-topic"), "refs/heads/operator-topic"),
        (_drift_undeclared_ref("refs/notes/commits"), "refs/notes/commits"),
        (_drift_world_writable_runtime_file, "world-writable"),
        (_drift_foreign_owner, "README.md"),
    ],
    ids=[
        "core-hookspath",
        "core-fsmonitor",
        "include",
        "alias",
        "filter",
        "credential",
        "url-insteadof",
        "branch-upstream",
        "user-signingkey",
        "worktree-outside-repository",
        "worktree-outside-runtime-roots",
        "stray-runtime-root-entry",
        "worktree-metadata",
        "undeclared-branch",
        "undeclared-ref-namespace",
        "world-writable-runtime-file",
        "foreign-owner",
    ],
)
def test_repository_state_keeps_drift_outside_runtime_allowlist_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate,
    expected: str,
) -> None:
    upgrade = repository_runtime_fixtures.repository_upgrade(tmp_path)
    repository_runtime_fixtures.simulate_manager_runtime(upgrade)
    mutate(upgrade, monkeypatch)

    state = LocalInstallBackend(require_root=False).inspect_step(upgrade.old_step)

    assert state["installed_sha256"] is None
    assert any(expected in reason for reason in state["drift"]), state["drift"]


def test_directory_acl_attestation_ignores_semantically_irrelevant_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    step = {
        "step_id": "asset:repo-source-tree",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(tmp_path / "repos"),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [
            {"account": "z-reader", "perms": "rX", "default": False},
            {"account": "a-reader", "perms": "rX", "default": False},
        ],
    }
    step["desired_sha256"] = _desired_digest(step)
    monkeypatch.setattr(
        backend_module,
        "_snapshot",
        lambda _path, **_kwargs: {
            "exists": True,
            "is_directory": True,
            "owner": account,
            "group": group,
            "mode": "0750",
            "acl": list(reversed(backend_module._expected_acls(step))),
        },
    )

    state = LocalInstallBackend(require_root=False).inspect_step(step)

    assert state["installed_sha256"] == step["desired_sha256"]


def test_service_identity_includes_live_systemd_active_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        command = tuple(argv)
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            "User=cortex-manager\nExecStart={ path=/usr/bin/true ; argv[]=/usr/bin/true ; }\nActiveState=active\n",
            "",
        )

    monkeypatch.setattr(backend_module, "_run", run)

    identities = LocalInstallBackend(require_root=False).service_identities()

    assert identities
    assert all(row["active_state"] == "active" for row in identities.values())
    assert all("--property=ActiveState" in command for command in calls)


def test_directory_acl_apply_keeps_external_target_safe_during_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    managed = tmp_path / "managed"
    displaced = tmp_path / "displaced-managed"
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    external_mode = stat.S_IMODE(external.stat().st_mode)
    calls: list[tuple[str, ...]] = []

    real_chmod = os.chmod
    real_fchmod = os.fchmod
    swapped = False

    def swap_leaf() -> None:
        nonlocal swapped
        if not swapped:
            managed.rename(displaced)
            managed.symlink_to(external, target_is_directory=True)
            swapped = True

    def chmod_and_swap(path: os.PathLike[str] | str, mode: int, **kwargs) -> None:
        real_chmod(path, mode, **kwargs)
        swap_leaf()

    def fchmod_and_swap(descriptor: int, mode: int) -> None:
        real_fchmod(descriptor, mode)
        swap_leaf()

    def run(argv, **_kwargs):
        command = tuple(argv)
        calls.append(command)
        if command[1] == "-m":
            # Model setfacl's target-following behavior with an observable mode
            # write. A pathname race would change ``external``; the held fd
            # changes only the displaced managed directory.
            real_chmod(command[-1], 0o711)
        return _completed(command)

    monkeypatch.setattr(backend_module, "_run", run)
    monkeypatch.setattr(backend_module.os, "chmod", chmod_and_swap)
    monkeypatch.setattr(backend_module.os, "fchmod", fchmod_and_swap)
    step = {
        "step_id": "asset:managed",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(managed),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [{"account": account, "perms": "rX", "default": False}],
    }
    step["desired_sha256"] = _desired_digest(step)

    with pytest.raises(UnsafeInstallPathError, match="symlink|changed"):
        LocalInstallBackend(require_root=False).apply_step(step)

    assert stat.S_IMODE(external.stat().st_mode) == external_mode
    assert not any(external.iterdir())
    assert calls
    assert all(command[-1].startswith("/proc/self/fd/") for command in calls)


def test_preflight_counts_active_jobs_from_plan_bound_durable_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "planned-state"
    registry = state_root / "coordinator/jobs.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "jobs": [
                    {"job_id": "job-dispatched", "status": "dispatched"},
                    {"job_id": "job-running", "status": "running"},
                    {"job_id": "job-exited", "status": "exited"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(backend_module, "_in_flight_process_count", lambda _rows: 0)
    monkeypatch.setattr(backend_module, "_run", lambda argv, **_kwargs: _completed(argv))
    plan = {
        "roots": {
            "state": str(state_root),
            "deploy": str(tmp_path / "deploy"),
        },
        "accounts": [],
        "service_accounts": [],
        "apply_order": [
            {
                "step_id": "asset:coordinator-root-tree",
                "kind": "asset",
                "asset_type": "directory",
                "path": str(registry.parent),
            }
        ],
        "minimum_disk_free_bytes": 0,
    }

    facts = LocalInstallBackend(require_root=False).preflight_facts(plan)
    report = validate_preflight(plan, facts)

    assert facts["in_flight_jobs"] == 2
    assert any(row["code"] == "in_flight_jobs" for row in report.failures)


def test_preflight_reports_oversized_prior_asset_file_before_any_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_in_flight_process_count", lambda _rows: 0)
    monkeypatch.setattr(backend_module, "_run", lambda argv, **_kwargs: _completed(argv))
    legacy = tmp_path / "bin/claude"
    legacy.parent.mkdir()
    with legacy.open("wb") as stream:
        stream.truncate(backend_module.ASSET_PRIOR_SNAPSHOT_MAX_BYTES + 1)
    plan = {
        "roots": {"state": str(tmp_path / "state"), "deploy": str(tmp_path / "deploy")},
        "accounts": [],
        "service_accounts": [],
        "apply_order": [
            {
                "step_id": "generated:toolchain_wrappers/claude",
                "kind": "asset",
                "asset_type": "file",
                "path": str(legacy),
            }
        ],
        "minimum_disk_free_bytes": 0,
    }

    facts = LocalInstallBackend(require_root=False).preflight_facts(plan)
    report = validate_preflight(plan, facts)

    assert facts["paths"][str(legacy)]["size"] == (
        backend_module.ASSET_PRIOR_SNAPSHOT_MAX_BYTES + 1
    )
    oversized = [
        row for row in report.failures if row["code"] == "asset_prior_too_large"
    ]
    assert len(oversized) == 1
    detail = oversized[0]["detail"]
    assert str(legacy) in detail
    assert str(backend_module.ASSET_PRIOR_SNAPSHOT_MAX_BYTES) in detail
    assert "move" in detail


def test_asset_inspection_never_embeds_an_oversized_prior_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    path = tmp_path / "cortex.env"
    with path.open("wb") as stream:
        stream.truncate(backend_module.ASSET_PRIOR_SNAPSHOT_MAX_BYTES + 1)
    step = {
        "step_id": "generated:environment/cortex.env",
        "kind": "asset",
        "asset_type": "file",
        "path": str(path),
    }

    with pytest.raises(InstallDriftError, match="too large") as caught:
        LocalInstallBackend(require_root=False).inspect_step(step)

    assert str(path) in str(caught.value)


def test_asset_inspection_still_refuses_a_symlinked_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    target = tmp_path / "toolchain/claude"
    target.parent.mkdir()
    target.write_bytes(b"binary")
    path = tmp_path / "claude"
    path.symlink_to(target)
    step = {
        "step_id": "generated:toolchain_wrappers/claude",
        "kind": "asset",
        "asset_type": "file",
        "path": str(path),
    }

    with pytest.raises(UnsafeInstallPathError, match="symlink"):
        LocalInstallBackend(require_root=False).inspect_step(step)


def test_installed_inventory_reads_large_generated_file_without_snapshot_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    path = tmp_path / "cortex.env"
    with path.open("wb") as stream:
        stream.truncate(backend_module.ASSET_PRIOR_SNAPSHOT_MAX_BYTES + 1)
    plan = {
        "generated": {"environment": {"cortex.env": {"path": str(path)}}},
    }

    installed = LocalInstallBackend(require_root=False).installed_inventory(plan)

    row = installed["environment"]["cortex.env"]
    assert len(row["content"]) == backend_module.ASSET_PRIOR_SNAPSHOT_MAX_BYTES + 1


def test_preflight_facts_capture_private_group_members_primary_users_and_gid_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planned = SimpleNamespace(
        pw_name="cortex-egress",
        pw_uid=995,
        pw_gid=995,
        pw_dir=str(tmp_path / "var/lib/cortex-egress"),
        pw_shell="/usr/sbin/nologin",
    )
    foreign = SimpleNamespace(
        pw_name="foreign-primary",
        pw_uid=1995,
        pw_gid=995,
        pw_dir=str(tmp_path / "var/lib/foreign-primary"),
        pw_shell="/usr/sbin/nologin",
    )
    private_group = SimpleNamespace(
        gr_name="cortex-egress",
        gr_gid=995,
        gr_mem=["foreign-supplementary"],
    )
    alias_group = SimpleNamespace(gr_name="legacy-alias", gr_gid=995, gr_mem=[])
    monkeypatch.setattr(backend_module.pwd, "getpwall", lambda: [planned, foreign])
    monkeypatch.setattr(backend_module.pwd, "getpwnam", lambda _name: planned)
    monkeypatch.setattr(
        backend_module.grp, "getgrall", lambda: [private_group, alias_group]
    )
    monkeypatch.setattr(backend_module, "_password_locked", lambda _name: True)
    monkeypatch.setattr(
        backend_module, "_in_flight_process_count", lambda _rows: 0
    )
    monkeypatch.setattr(backend_module, "_run", lambda argv, **_kwargs: _completed(argv))
    plan = {
        "roots": {
            "state": str(tmp_path / "state"),
            "deploy": str(tmp_path / "deploy"),
        },
        "accounts": [],
        "service_accounts": [
            {
                "name": "cortex-egress",
                "uid": 995,
                "gid": 995,
                "home": planned.pw_dir,
                "shell": planned.pw_shell,
            }
        ],
        "apply_order": [],
        "minimum_disk_free_bytes": 0,
    }

    facts = LocalInstallBackend(require_root=False).preflight_facts(plan)

    assert facts["groups"]["cortex-egress"]["members"] == [
        "foreign-supplementary"
    ]
    assert facts["primary_gid_users"][995] == [
        "cortex-egress",
        "foreign-primary",
    ]
    assert facts["group_names_by_gid"][995] == [
        "cortex-egress",
        "legacy-alias",
    ]


@pytest.mark.parametrize(
    ("fact_key", "value", "match"),
    [
        ("members", ["foreign-supplementary"], "member"),
        ("primary_gid_users", ["cortex-egress", "foreign-primary"], "primary"),
        ("group_names_by_gid", ["cortex-egress", "legacy-alias"], "shared gid"),
    ],
)
def test_preflight_rejects_non_private_service_group_membership_or_gid_alias(
    tmp_path: Path, fact_key: str, value: list[str], match: str
) -> None:
    desired = {
        "name": "cortex-egress",
        "uid": 995,
        "gid": 995,
        "home": str(tmp_path / "var/lib/cortex-egress"),
        "shell": "/usr/sbin/nologin",
    }
    plan = {
        "accounts": [],
        "service_accounts": [desired],
        "apply_order": [],
        "minimum_disk_free_bytes": 0,
    }
    facts: dict[str, object] = {
        "systemd": True,
        "polkit": True,
        "cgroup_v2": True,
        "acl": True,
        "disk_free_bytes": 1,
        "universal_nopasswd": False,
        "in_flight_jobs": 0,
        "services": {},
        "accounts": {},
        "account_uids": {},
        "group_gids": {995: "cortex-egress"},
        "groups": {
            "cortex-egress": {
                "name": "cortex-egress",
                "gid": 995,
                "members": [],
            }
        },
        "primary_gid_users": {995: ["cortex-egress"]},
        "group_names_by_gid": {995: ["cortex-egress"]},
        "paths": {},
    }
    if fact_key == "members":
        facts["groups"]["cortex-egress"]["members"] = value
    else:
        facts[fact_key][995] = value

    with pytest.raises(AccountCollisionError, match=match):
        validate_preflight(plan, facts)


def test_rollback_reports_unknown_child_of_adopted_managed_directory(
    tmp_path: Path,
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    managed = tmp_path / "adopted"
    managed.mkdir(mode=0o700)
    managed.chmod(0o700)
    step = {
        "step_id": "asset:adopted",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(managed),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)
    applied = backend.apply_step(step)
    entry = {"step": step, "prior": applied["prior"]}
    unknown = managed / "created-after-install"
    unknown.write_text("durable\n", encoding="utf-8")

    receipt = InstallReceipt(
        {
            "state": "applied",
            "journal": [entry],
            "services_started": False,
            "credentials": [],
        }
    )
    report = rollback_receipt(receipt, backend=backend)

    assert str(unknown) in report.retained_unknown


def test_directory_acl_apply_does_not_follow_swapped_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    authority = tmp_path / "authority"
    authority.mkdir()
    displaced = tmp_path / "displaced-authority"
    managed = authority / "managed"
    external_root = tmp_path / "external"
    external = external_root / "managed"
    external.mkdir(parents=True, mode=0o755)
    external_mode = stat.S_IMODE(external.stat().st_mode)
    real_open = os.open
    real_chmod = os.chmod
    swapped = False

    def open_after_ancestor_swap(path, flags, *args, **kwargs):
        nonlocal swapped
        candidate = os.fspath(path)
        if not swapped and (candidate == str(managed) or candidate == managed.name):
            authority.rename(displaced)
            authority.symlink_to(external_root, target_is_directory=True)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    def run(argv, **_kwargs):
        command = tuple(argv)
        if command[1] == "-m":
            real_chmod(command[-1], 0o711)
        return _completed(command)

    monkeypatch.setattr(backend_module.os, "open", open_after_ancestor_swap)
    monkeypatch.setattr(backend_module, "_run", run)
    step = {
        "step_id": "asset:managed-ancestor-race",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(managed),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [{"account": account, "perms": "rX", "default": False}],
    }
    step["desired_sha256"] = _desired_digest(step)

    with pytest.raises((UnsafeInstallPathError, InstallDriftError)):
        LocalInstallBackend(require_root=False).apply_step(step)

    assert swapped
    assert stat.S_IMODE(external.stat().st_mode) == external_mode


def test_rollback_reports_nested_unknown_child_from_recursive_no_follow_snapshot(
    tmp_path: Path,
) -> None:
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    managed = tmp_path / "adopted"
    baseline = managed / "known"
    baseline.mkdir(parents=True)
    (baseline / "existing.txt").write_text("existing\n", encoding="utf-8")
    managed.chmod(0o700)
    step = {
        "step_id": "asset:adopted-recursive",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(managed),
        "owner": account,
        "group": group,
        "mode": "0700",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    backend = LocalInstallBackend(require_root=False)
    applied = backend.apply_step(step)
    unknown = baseline / "nested" / "created-after-install.txt"
    unknown.parent.mkdir()
    unknown.write_text("durable\n", encoding="utf-8")
    receipt = InstallReceipt(
        {
            "state": "applied",
            "journal": [{"step": step, "prior": applied["prior"]}],
            "services_started": False,
            "credentials": [],
        }
    )
    # apply_plan binds the prior's descendant inventory before persisting the
    # entry; this direct backend test performs the same binding explicitly.
    backend.bind_rollback_inventory(receipt, step, applied["prior"])

    report = rollback_receipt(receipt, backend=backend)

    assert unknown.read_text(encoding="utf-8") == "durable\n"
    assert str(unknown) in report.retained_unknown
    assert str(managed) not in report.retained_unknown


def test_recursive_directory_inventory_never_descends_through_symlinks(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    external = tmp_path / "external"
    (managed / "real").mkdir(parents=True)
    external.mkdir()
    (managed / "real/inside.txt").write_text("inside\n", encoding="utf-8")
    (external / "outside.txt").write_text("outside\n", encoding="utf-8")
    (managed / "escape").symlink_to(external, target_is_directory=True)

    inventory = backend_module._directory_inventory(managed)

    assert inventory == ["escape", "real", "real/inside.txt"]
    assert "escape/outside.txt" not in inventory


def _managed_directory_step(path: Path) -> dict[str, object]:
    step = {
        "step_id": "asset:state-root",
        "kind": "asset",
        "asset_type": "directory",
        "path": str(path),
        "owner": pwd.getpwuid(os.getuid()).pw_name,
        "group": grp.getgrgid(os.getgid()).gr_name,
        "mode": "0700",
        "acls": [],
    }
    step["desired_sha256"] = _desired_digest(step)
    return step


def _populate_tree(root: Path, *, directories: int, files: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for index in range(directories):
        branch = root / f"branch-{index:04d}"
        branch.mkdir()
        for leaf in range(files):
            (branch / f"leaf-{leaf:04d}.txt").write_bytes(b"x")


def _durable_receipt_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: Mapping[str, object]
) -> InstallReceipt:
    """Persist a real receipt in a temporary root; ownership checks are faked."""

    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    plan = {"apply_order": [dict(step)], "repo_identity": {}, "candidate": {}}
    path = (tmp_path / "receipts" / "install.json").absolute()
    return new_install_receipt(plan, path=path)


def _prepare_directory_entry(
    backend: LocalInstallBackend,
    receipt: InstallReceipt,
    step: Mapping[str, object],
) -> dict[str, object]:
    """Mirror apply_plan: bind the prior inventory, then persist the entry."""

    prior = dict(backend.inspect_step(step))
    backend.bind_rollback_inventory(receipt, step, prior)
    entry = {
        "step_id": step["step_id"],
        "step": dict(step),
        "status": "completed",
        "prior": prior,
    }
    receipt._document["journal"].append(entry)
    receipt._document["state"] = "applied"
    receipt._persist()
    return prior


def _inventory_store(receipt: InstallReceipt) -> Path:
    assert receipt.path is not None
    return receipt.path.with_name(f".{receipt.path.name}.inventory")


def test_directory_snapshot_binds_descendants_by_digest_not_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    managed = tmp_path / "state"
    _populate_tree(managed, directories=3, files=2)
    rows = backend_module._directory_inventory(managed)

    state = LocalInstallBackend(require_root=False).inspect_step(
        _managed_directory_step(managed)
    )

    assert "children" not in state
    assert state["children_count"] == len(rows) == 9
    assert state["children_sha256"] == install_core._directory_inventory_sha256(rows)


def test_receipt_size_is_independent_of_directory_descendant_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    sizes: dict[str, int] = {}
    for label, directories, files in (("small", 2, 2), ("large", 60, 100)):
        managed = tmp_path / label / "state"
        _populate_tree(managed, directories=directories, files=files)
        step = _managed_directory_step(managed)
        receipt = _durable_receipt_for(tmp_path / label, monkeypatch, step)
        backend = LocalInstallBackend(require_root=False)

        prior = _prepare_directory_entry(backend, receipt, step)

        assert receipt.path is not None
        sizes[label] = receipt.path.stat().st_size
        blob = _inventory_store(receipt) / f"{prior['children_sha256']}.json"
        assert json.loads(blob.read_text(encoding="ascii")) == (
            backend_module._directory_inventory(managed)
        )
        assert stat.S_IMODE(blob.stat().st_mode) == 0o600
        assert stat.S_IMODE(_inventory_store(receipt).stat().st_mode) == 0o700
        assert "branch-0001" not in receipt.path.read_text(encoding="utf-8")

    assert prior["children_count"] == 60 * 101
    # 6,060 descendants cost the receipt only the extra digits of the count.
    assert sizes["large"] - sizes["small"] < 16


def test_unknown_scanner_reads_bound_inventory_after_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    managed = tmp_path / "state"
    _populate_tree(managed, directories=2, files=2)
    step = _managed_directory_step(managed)
    receipt = _durable_receipt_for(tmp_path, monkeypatch, step)
    _prepare_directory_entry(LocalInstallBackend(require_root=False), receipt, step)
    unknown = managed / "branch-0001" / "nested" / "created-after-install.txt"
    unknown.parent.mkdir()
    unknown.write_text("durable\n", encoding="utf-8")
    assert receipt.path is not None

    reloaded = InstallReceipt.load(receipt.path)
    retained = LocalInstallBackend(require_root=False).list_unknown_state(reloaded)

    assert retained == (str(unknown.parent), str(unknown))


@pytest.mark.parametrize(
    "corruption", ["missing", "tampered", "symlinked", "store-not-private"]
)
def test_unknown_scanner_fails_closed_without_trusted_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    managed = tmp_path / "state"
    _populate_tree(managed, directories=2, files=2)
    step = _managed_directory_step(managed)
    receipt = _durable_receipt_for(tmp_path, monkeypatch, step)
    backend = LocalInstallBackend(require_root=False)
    prior = _prepare_directory_entry(backend, receipt, step)
    store = _inventory_store(receipt)
    blob = store / f"{prior['children_sha256']}.json"
    if corruption == "missing":
        blob.unlink()
    elif corruption == "tampered":
        blob.write_text(json.dumps(["branch-0000"]) + "\n", encoding="ascii")
    elif corruption == "symlinked":
        decoy = tmp_path / "decoy.json"
        decoy.write_bytes(blob.read_bytes())
        blob.unlink()
        blob.symlink_to(decoy)
    else:
        store.chmod(0o755)
    unknown = managed / "created-after-install.txt"
    unknown.write_text("durable\n", encoding="utf-8")
    baseline = backend_module._directory_inventory(managed)
    assert receipt.path is not None

    reloaded = InstallReceipt.load(receipt.path)
    report = rollback_receipt(reloaded, backend=LocalInstallBackend(require_root=False))

    # Nothing under the directory can be classified without a trusted
    # baseline: the whole directory is retained and rollback is blocked.
    assert report.retained_unknown == (str(managed),)
    assert reloaded.to_dict()["state"] == "rollback-blocked"
    assert backend_module._directory_inventory(managed) == baseline


def test_unchanged_directory_needs_no_inventory_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    managed = tmp_path / "state"
    _populate_tree(managed, directories=2, files=2)
    step = _managed_directory_step(managed)
    receipt = _durable_receipt_for(tmp_path, monkeypatch, step)
    prior = _prepare_directory_entry(
        LocalInstallBackend(require_root=False), receipt, step
    )
    (_inventory_store(receipt) / f"{prior['children_sha256']}.json").unlink()
    assert receipt.path is not None

    reloaded = InstallReceipt.load(receipt.path)

    assert LocalInstallBackend(require_root=False).list_unknown_state(reloaded) == ()


def test_legacy_receipt_inside_managed_directory_rolls_back_without_side_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    managed = tmp_path / "state"
    _populate_tree(managed, directories=2, files=2)
    step = _managed_directory_step(managed)
    plan = {"apply_order": [dict(step)], "repo_identity": {}, "candidate": {}}
    receipt_path = (managed / "receipts" / "install.json").absolute()
    receipt = new_install_receipt(plan, path=receipt_path)
    backend = LocalInstallBackend(require_root=False)
    current = dict(backend.inspect_step(step))
    legacy_rows = backend_module._directory_inventory(managed)
    assert "receipts/install.json" in legacy_rows
    legacy_prior = {
        key: value
        for key, value in current.items()
        if key not in {"children_count", "children_sha256"}
    }
    legacy_prior["children"] = legacy_rows
    document = receipt.to_dict()
    document.update(
        {
            "schema_version": 1,
            "state": "applied",
            "journal": [
                {
                    "step_id": step["step_id"],
                    "step": dict(step),
                    "status": "completed",
                    "prior": legacy_prior,
                    **legacy_prior,
                }
            ],
        }
    )
    # A receipt written by the pre-digest installer: inline children, v1.
    receipt_path.write_bytes(install_core._canonical_bytes(document))

    loaded = InstallReceipt.load(receipt_path)
    report = rollback_receipt(loaded, backend=LocalInstallBackend(require_root=False))

    # The installer never creates side files inside a managed directory, so
    # the receipt's own directory cannot turn into unknown state.
    assert report.retained_unknown == ()
    assert report.retained_drift == ()
    assert loaded.to_dict()["state"] == "rolled-back"
    assert not (receipt_path.parent / ".install.json.inventory").exists()
    on_disk = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert on_disk["rollback_journal"][0]["prior"]["children"] == legacy_rows


def test_legacy_inline_children_receipt_is_migrated_on_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module, "_read_acl", lambda _path: [])
    managed = tmp_path / "state"
    _populate_tree(managed, directories=2, files=2)
    step = _managed_directory_step(managed)
    receipt = _durable_receipt_for(tmp_path, monkeypatch, step)
    backend = LocalInstallBackend(require_root=False)
    current = dict(backend.inspect_step(step))
    legacy_prior = {
        key: value
        for key, value in current.items()
        if key not in {"children_count", "children_sha256"}
    }
    legacy_prior["children"] = backend_module._directory_inventory(managed)
    receipt._document["journal"].append(
        {
            "step_id": step["step_id"],
            "step": dict(step),
            "status": "prepared",
            "prior": legacy_prior,
            **legacy_prior,
        }
    )
    receipt._document["state"] = "applying"
    receipt._persist()
    assert receipt.path is not None
    assert "branch-0001/leaf-0001.txt" in receipt.path.read_text(encoding="utf-8")
    unknown = managed / "created-after-install.txt"
    unknown.write_text("durable\n", encoding="utf-8")

    loaded = InstallReceipt.load(receipt.path)
    entry = loaded.to_dict()["journal"][0]

    # The legacy baseline compares exactly like a fresh digest-form inspection.
    assert entry["prior"] == current
    assert "children" not in entry
    assert LocalInstallBackend(require_root=False).list_unknown_state(loaded) == (
        str(unknown),
    )

    loaded._persist()
    on_disk = receipt.path.read_text(encoding="utf-8")
    assert "branch-0001/leaf-0001.txt" not in on_disk
    # Side-file receipts are unreadable to a pre-digest installer: it must
    # refuse the schema outright instead of misreading the directory states.
    assert json.loads(on_disk)["schema_version"] == 2
    assert LocalInstallBackend(require_root=False).list_unknown_state(
        InstallReceipt.load(receipt.path)
    ) == (str(unknown),)


def _write_toolchain_archive(path: Path, *, tool_mode: int = 0o755) -> None:
    payload = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(path, mode="w") as archive:
        directory = tarfile.TarInfo("bin")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
        tool = tarfile.TarInfo("bin/tool")
        tool.mode = tool_mode
        tool.size = len(payload)
        archive.addfile(tool, io.BytesIO(payload))


def _toolchain_tree_step(tmp_path: Path, archive: Path) -> dict[str, object]:
    reference = tmp_path / "reference"
    (reference / "bin").mkdir(parents=True)
    (reference / "bin").chmod(0o755)
    (reference / "bin/tool").write_bytes(b"#!/bin/sh\nexit 0\n")
    (reference / "bin/tool").chmod(0o755)
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    return {
        "step_id": "toolchain:demo",
        "kind": "toolchain",
        "shape": "tree",
        "name": "demo",
        "path": str(tmp_path / "installed/demo"),
        "source": str(archive),
        "source_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "desired_sha256": backend_module._tree_sha256(reference),
        "entrypoint": "bin/tool",
        "owner": account,
        "group": group,
        "mode": "0755",
    }


def test_toolchain_archive_rejects_nested_group_writable_member(tmp_path: Path) -> None:
    archive = tmp_path / "toolchain.tar"
    _write_toolchain_archive(archive, tool_mode=0o775)
    step = _toolchain_tree_step(tmp_path, archive)

    with pytest.raises(InstallDriftError, match="group|other|writ"):
        LocalInstallBackend(require_root=False).apply_step(step)

    assert not Path(step["path"]).exists()


def test_toolchain_tree_attests_nested_owner_recursively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "toolchain.tar"
    _write_toolchain_archive(archive)
    step = _toolchain_tree_step(tmp_path, archive)
    backend = LocalInstallBackend(require_root=False)
    backend.apply_step(step)
    tool = Path(step["path"]) / "bin/tool"
    real_lstat = Path.lstat

    def nested_owner_drift(path: Path):
        observed = real_lstat(path)
        if path == tool:
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_uid=observed.st_uid + 1,
                st_gid=observed.st_gid,
                st_nlink=observed.st_nlink,
            )
        return observed

    monkeypatch.setattr(Path, "lstat", nested_owner_drift)

    assert backend.inspect_step(step)["installed_sha256"] is None


def test_prepared_toolchain_rollback_removes_only_owned_leaves_and_retains_unknown(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "toolchain.tar"
    _write_toolchain_archive(archive)
    step = _toolchain_tree_step(tmp_path, archive)
    backend = LocalInstallBackend(require_root=False)
    backend.apply_step(step)
    installed = Path(step["path"])
    owned = installed / "bin/tool"
    unknown = installed / "operator/nested/keep.txt"
    unknown.parent.mkdir(parents=True)
    unknown.write_text("keep\n", encoding="utf-8")
    receipt = InstallReceipt(
        {
            "state": "applying",
            "journal": [
                {
                    "step_id": step["step_id"],
                    "step": step,
                    "status": "prepared",
                    "prior": {"exists": False},
                }
            ],
            "services_started": False,
            "credentials": [],
        }
    )

    report = rollback_receipt(receipt, backend=backend)

    assert not owned.exists()
    assert unknown.read_text(encoding="utf-8") == "keep\n"
    assert str(unknown) in report.retained_unknown


def test_prepared_toolchain_rollback_retains_and_reports_modified_owned_leaf(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "toolchain.tar"
    _write_toolchain_archive(archive)
    step = _toolchain_tree_step(tmp_path, archive)
    backend = LocalInstallBackend(require_root=False)
    backend.apply_step(step)
    modified = Path(step["path"]) / "bin/tool"
    modified.write_bytes(b"operator replacement\n")
    modified.chmod(0o755)
    receipt = InstallReceipt(
        {
            "state": "applying",
            "journal": [
                {
                    "step_id": step["step_id"],
                    "step": step,
                    "status": "prepared",
                    "prior": {"exists": False},
                }
            ],
            "services_started": False,
            "credentials": [],
        }
    )

    report = rollback_receipt(receipt, backend=backend)

    assert modified.read_bytes() == b"operator replacement\n"
    assert str(modified) in report.retained_unknown


def test_prepared_toolchain_rollback_never_follows_replaced_member_directory(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "toolchain.tar"
    _write_toolchain_archive(archive)
    step = _toolchain_tree_step(tmp_path, archive)
    backend = LocalInstallBackend(require_root=False)
    backend.apply_step(step)
    installed = Path(step["path"])
    displaced = tmp_path / "displaced-bin"
    (installed / "bin").rename(displaced)
    external = tmp_path / "external"
    external.mkdir()
    external_tool = external / "tool"
    external_tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    external_tool.chmod(0o755)
    (installed / "bin").symlink_to(external, target_is_directory=True)
    receipt = InstallReceipt(
        {
            "state": "applying",
            "journal": [
                {
                    "step_id": step["step_id"],
                    "step": step,
                    "status": "prepared",
                    "prior": {"exists": False},
                }
            ],
            "services_started": False,
            "credentials": [],
        }
    )

    report = rollback_receipt(receipt, backend=backend)

    assert external_tool.read_bytes() == b"#!/bin/sh\nexit 0\n"
    assert (installed / "bin").is_symlink()
    assert str(installed / "bin") in report.retained_unknown


def test_getfacl_failure_is_not_reported_as_an_empty_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        backend_module,
        "_run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(list(argv), 1, "", "denied"),
    )

    with pytest.raises(InstallDriftError, match="getfacl|ACL"):
        backend_module._read_acl(tmp_path)


# Real ``getfacl -cp`` output (acl 2.3, no ``-E``) for a directory whose ACL
# mask is stricter than its named/group entries.  Only the account name is
# replaced with a neutral placeholder; the tab + ``#effective:`` comment shape
# is exactly what the tool prints.
_GETFACL_MASKED_OUTPUT = (
    "user::rwx\n"
    "user:operator:r-x\t#effective:---\n"
    "group::r-x\t#effective:---\n"
    "mask::---\n"
    "other::---\n"
    "default:user::rwx\n"
    "default:user:operator:r-x\t#effective:---\n"
    "default:group::r-x\t#effective:---\n"
    "default:mask::---\n"
    "default:other::---\n"
    "\n"
)


def test_read_acl_ignores_effective_rights_comments_under_restrictive_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backend_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    def run(argv, **_kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(list(argv), 0, _GETFACL_MASKED_OUTPUT, "")

    monkeypatch.setattr(backend_module, "_run", run)

    rows = backend_module._read_acl(tmp_path)

    assert calls == [("getfacl", "-cpE", str(tmp_path))]
    assert rows == [
        {"account": "", "perms": "rx", "default": False, "entry_type": "group"},
        {"account": "", "perms": "", "default": False, "entry_type": "mask"},
        {"account": "operator", "perms": "rx", "default": False},
        {"account": "", "perms": "rx", "default": True, "entry_type": "group"},
        {"account": "", "perms": "", "default": True, "entry_type": "mask"},
        {"account": "", "perms": "", "default": True, "entry_type": "other"},
        {"account": "", "perms": "rwx", "default": True, "entry_type": "user"},
        {"account": "operator", "perms": "rx", "default": True},
    ]
    # Every parsed row must round-trip into a valid ``setfacl -m`` spec.
    assert [backend_module._acl_argument(row) for row in rows][2] == "u:operator:rx"


def test_read_acl_reports_named_perms_when_live_mask_restricts_them(
    tmp_path: Path,
) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("requires acl tools")
    account = pwd.getpwuid(os.getuid()).pw_name
    target = tmp_path / "masked"
    target.mkdir(mode=0o750)
    subprocess.run(["setfacl", "-m", f"u:{account}:rx", str(target)], check=True)
    subprocess.run(["setfacl", "-m", "m::---", str(target)], check=True)

    rows = backend_module._read_acl(target)

    assert {"account": account, "perms": "rx", "default": False} in rows
    assert all(set(str(row["perms"])) <= set("rwx") for row in rows)


def test_acl_argument_renders_an_empty_permission_set_as_a_dash() -> None:
    # ``_read_acl`` records ``---`` as ``""`` (receipts already hold that
    # form); ``setfacl -m "m::"`` is rejected as incomplete, ``m::-`` is not.
    render = backend_module._acl_argument
    assert render({"account": "", "perms": "", "default": False, "entry_type": "mask"}) == "m::-"
    assert render({"account": "operator", "perms": "", "default": False}) == "u:operator:-"
    assert render({"account": "", "perms": "", "default": True, "entry_type": "mask"}) == "d:m::-"
    assert render({"account": "", "perms": "rX", "default": False, "entry_type": "other"}) == "o::rx"


def test_acl_with_empty_permission_entries_round_trips_through_rollback_apply(
    tmp_path: Path,
) -> None:
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip("requires acl tools")
    account = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    target = tmp_path / "legacy-state"
    target.mkdir(mode=0o750)
    try:
        subprocess.run(
            [
                "setfacl",
                "-m",
                f"u:{account}:---,m::---,d:u::rwx,d:g::r-x,d:o::---,d:u:{account}:---,d:m::---",
                str(target),
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        pytest.skip("this filesystem does not support POSIX ACLs")
    prior = backend_module._read_acl(target)
    assert {"account": account, "perms": "", "default": False} in prior
    assert {"account": "", "perms": "", "default": False, "entry_type": "mask"} in prior
    mode = format(stat.S_IMODE(target.lstat().st_mode), "04o")
    # Metadata replacement changes it; rollback applies the recorded prior.
    subprocess.run(["setfacl", "-b", "-k", str(target)], check=True)
    subprocess.run(["setfacl", "-m", f"u:{account}:rwx", str(target)], check=True)
    assert backend_module._read_acl(target) != prior

    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        backend_module._apply_fd_asset_state(
            descriptor, owner=account, group=group, mode=mode, acls=prior, directory=True
        )
    finally:
        os.close(descriptor)

    assert backend_module._read_acl(target) == prior


def test_missing_getfacl_binary_is_not_reported_as_an_empty_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backend_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        backend_module,
        "_run",
        lambda argv, **_kwargs: calls.append(tuple(argv)) or _completed(argv),
    )

    with pytest.raises(InstallDriftError, match="getfacl|ACL|unavailable"):
        backend_module._read_acl(tmp_path)

    assert calls == []


def _write_visudo_valid_fixture(tmp_path: Path, policy: str) -> Path:
    sudoers = tmp_path / "sudoers"
    sudoers.write_text(policy, encoding="utf-8")
    visudo = shutil.which("visudo")
    assert visudo is not None, "sudo package must provide visudo"
    validated = subprocess.run(
        (visudo, "-c", "-f", str(sudoers)),
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr or validated.stdout
    return sudoers


@pytest.fixture
def _system_sbin_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The manager service PATH omits sbin; validation and detection need the same tools.
    existing_path = os.environ.get("PATH")
    parts = (existing_path, "/usr/sbin", "/sbin")
    monkeypatch.setenv("PATH", os.pathsep.join(part for part in parts if part))


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_catches_blanket_noauth_in_colon_host_spec(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "%operators buildhost=(ALL) /usr/bin/id : "
        "ALL=(ALL) NOPASSWD: ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_catches_defaults_noauth_for_blanket_authority(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Defaults !authenticate\n"
        "%operators ALL=(ALL) ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


@pytest.mark.parametrize("host", ["*", "0.0.0.0/0", "::/0"])
@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_catches_blanket_noauth_on_universal_host_expression(
    tmp_path: Path, host: str
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        f"%operators {host}=(ALL) NOPASSWD: ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_honors_explicit_passwd_override(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Defaults !authenticate\n"
        "%operators ALL=(ALL) PASSWD: ALL\n",
    )

    assert not backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_resolves_universal_command_alias_across_continuations(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Cmnd_Alias LIMITED = /usr/bin/id, /usr/bin/true\n"
        "Cmnd_Alias ROOT_COMMANDS = \\\n"
        "    LIMITED, \\\n"
        "    ALL\n"
        "%operators ALL=(ALL:ALL) NOPASSWD: ROOT_COMMANDS\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_does_not_treat_limited_alias_as_universal(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Cmnd_Alias LIMITED = /usr/bin/id, /usr/bin/true\n"
        "%operators ALL=(ALL:ALL) NOPASSWD: LIMITED\n",
    )

    assert not backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_resolves_host_alias_to_all(tmp_path: Path) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Host_Alias LOCAL = ALL\n"
        "%operators LOCAL=(ALL) NOPASSWD: ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_resolves_recursive_host_aliases(tmp_path: Path) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Host_Alias LOCAL = ALL\n"
        "Host_Alias EDGE = LOCAL\n"
        "%operators EDGE=(ALL) NOPASSWD: ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


def test_sudoers_detector_fails_closed_on_referenced_host_alias_cycle(
    tmp_path: Path,
) -> None:
    sudoers = tmp_path / "sudoers"
    sudoers.write_text(
        "Host_Alias FIRST = SECOND\n"
        "Host_Alias SECOND = FIRST\n"
        "%operators FIRST=(ALL) NOPASSWD: ALL\n",
        encoding="utf-8",
    )

    assert backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_does_not_treat_limited_host_alias_as_universal(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Host_Alias LOCAL = buildhost\n"
        "%operators LOCAL=(ALL) NOPASSWD: ALL\n",
    )

    assert not backend_module._universal_nopasswd(sudoers)


@pytest.mark.parametrize("directive", ["@include", "#include"])
def test_sudoers_detector_follows_authoritative_include(
    tmp_path: Path, directive: str
) -> None:
    included = tmp_path / "operators"
    included.write_text(
        "Host_Alias LOCAL = ALL\n"
        "%operators LOCAL=(ALL) NOPASSWD: ALL\n",
        encoding="utf-8",
    )
    sudoers = tmp_path / "sudoers"
    sudoers.write_text(f"{directive} {included}\n", encoding="utf-8")

    assert backend_module._universal_nopasswd(sudoers)


@pytest.mark.usefixtures("_system_sbin_on_path")
def test_sudoers_detector_does_not_scan_unincluded_sibling_policy(
    tmp_path: Path,
) -> None:
    sudoers = tmp_path / "sudoers"
    sudoers.write_text("%operators ALL=(ALL) /usr/bin/id\n", encoding="utf-8")
    (tmp_path / "unreferenced").write_text(
        "%operators ALL=(ALL) NOPASSWD: ALL\n", encoding="utf-8"
    )

    assert not backend_module._universal_nopasswd(sudoers)


def test_sudoers_detector_fails_closed_on_include_cycle(tmp_path: Path) -> None:
    sudoers = tmp_path / "sudoers"
    included = tmp_path / "included"
    sudoers.write_text(f"@include {included}\n", encoding="utf-8")
    included.write_text(f"@include {sudoers}\n", encoding="utf-8")

    assert backend_module._universal_nopasswd(sudoers)


def test_sudoers_detector_follows_authoritative_includedir(tmp_path: Path) -> None:
    sudoers_d = tmp_path / "sudoers.d"
    sudoers_d.mkdir()
    (sudoers_d / "operators").write_text(
        "%operators ALL=(ALL) NOPASSWD: ALL\n", encoding="utf-8"
    )
    sudoers = tmp_path / "sudoers"
    sudoers.write_text(f"#includedir {sudoers_d}\n", encoding="utf-8")

    assert backend_module._universal_nopasswd(sudoers)
