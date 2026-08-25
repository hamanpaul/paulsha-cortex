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

import pytest

from paulsha_cortex.trust_root.install import (
    AccountCollisionError,
    InstallDriftError,
    UnsafeInstallPathError,
)
from paulsha_cortex.trust_root.install import backend as backend_module
from paulsha_cortex.trust_root.install.backend import LocalInstallBackend
from paulsha_cortex.trust_root.install.backend import _mode
from paulsha_cortex.trust_root.install.core import (
    InstallReceipt,
    InstallPlanError,
    _account_digest,
    _desired_digest,
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
    assert len(git_calls) == 7
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

    outcome = backend.apply_step_checkpointed(step, checkpoint)

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
        lambda _path: {
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

    report = rollback_receipt(receipt, backend=backend)

    assert unknown.read_text(encoding="utf-8") == "durable\n"
    assert str(unknown) in report.retained_unknown


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


def test_sudoers_detector_catches_blanket_noauth_in_colon_host_spec(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "%operators buildhost=(ALL) /usr/bin/id : "
        "ALL=(ALL) NOPASSWD: ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


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
def test_sudoers_detector_catches_blanket_noauth_on_universal_host_expression(
    tmp_path: Path, host: str
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        f"%operators {host}=(ALL) NOPASSWD: ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


def test_sudoers_detector_honors_explicit_passwd_override(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Defaults !authenticate\n"
        "%operators ALL=(ALL) PASSWD: ALL\n",
    )

    assert not backend_module._universal_nopasswd(sudoers)


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


def test_sudoers_detector_does_not_treat_limited_alias_as_universal(
    tmp_path: Path,
) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Cmnd_Alias LIMITED = /usr/bin/id, /usr/bin/true\n"
        "%operators ALL=(ALL:ALL) NOPASSWD: LIMITED\n",
    )

    assert not backend_module._universal_nopasswd(sudoers)


def test_sudoers_detector_resolves_host_alias_to_all(tmp_path: Path) -> None:
    sudoers = _write_visudo_valid_fixture(
        tmp_path,
        "Host_Alias LOCAL = ALL\n"
        "%operators LOCAL=(ALL) NOPASSWD: ALL\n",
    )

    assert backend_module._universal_nopasswd(sudoers)


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
