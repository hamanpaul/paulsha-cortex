"""Real-backend contracts that stay inside a temporary root or mocked argv seam."""

from __future__ import annotations

import grp
import hashlib
import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.trust_root.install import InstallDriftError
from paulsha_cortex.trust_root.install import backend as backend_module
from paulsha_cortex.trust_root.install.backend import LocalInstallBackend
from paulsha_cortex.trust_root.install.backend import _mode
from paulsha_cortex.trust_root.install.core import (
    InstallPlanError,
    _account_digest,
    _desired_digest,
)


def _completed(argv) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 0, "", "")


def test_mode_parser_accepts_registry_sticky_mode_and_rejects_invalid_values() -> None:
    assert _mode("1755") == 0o1755
    assert _mode("0700") == 0o700
    for invalid in ("755", "01755", "0788", "-700"):
        with pytest.raises(InstallPlanError, match="invalid mode"):
            _mode(invalid)


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
            (Path(command[3]) / "bin").mkdir(parents=True)
            (Path(command[3]) / "bin/python").write_text(
                "verified interpreter", encoding="utf-8"
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
    assert active.resolve() == Path(step["path"])
    assert (Path(step["path"]) / ".cortex-wheel.sha256").read_text().strip() == wheel_sha
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
