"""Shared fakes for `cortex upgrade` tests: no root, no network, no systemd."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, TypeVar

from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install import upgrade
from paulsha_cortex.trust_root.install.core import (
    InstallReceipt,
    new_install_receipt,
    plan_sha256,
)
from paulsha_cortex.trust_root.install.release_ingress import (
    ReleaseAsset,
    ReleaseMetadata,
    SealedCandidate,
    asset_names,
    tree_sha256,
)
from paulsha_cortex.trust_root.install.release_ingress import parse_version

PRIOR_WHEEL = "1" * 64
PRIOR_COMMIT = "b" * 40
NEW_WHEEL = "2" * 64
NEW_COMMIT = "a" * 40

_T = TypeVar("_T")


def call_without_blocking_on(fifo: Path, call: Callable[[], _T], *, seconds: float = 3.0) -> _T:
    """Run ``call``; fail (instead of hanging) if it blocks opening ``fifo`` for reading.

    A reader blocked in ``open(fifo, O_RDONLY)`` is released by a non-blocking
    writer open and close, so the watchdog never leaves a stuck thread behind.
    """

    outcome: dict[str, object] = {}

    def target() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        thread.join(seconds)
        raise AssertionError(f"blocked opening a FIFO for reading: {fifo.name}")
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]  # type: ignore[return-value]


def plan_document(
    tmp_path: Path,
    *,
    version: str,
    wheel_sha256: str,
    commit: str,
    overlay_sha: str | None = None,
) -> dict[str, object]:
    plan: dict[str, object] = {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": commit,
        },
        "candidate": {
            "candidate_sha": commit,
            "wheel_sha256": wheel_sha256,
            "bundle_sha256": "c" * 64,
            "wheel": {
                "path": f"dist/paulsha_cortex-{version}-py3-none-any.whl",
                "sha256": wheel_sha256,
            },
        },
        "accounts": [
            {
                "name": "cortex-builder",
                "uid": 993,
                "gid": 993,
                "home": "/var/lib/cortex-builder",
                "shell": "/usr/sbin/nologin",
            }
        ],
        "roots": {
            "deploy": str(tmp_path / "opt/cortex"),
            "state": str(tmp_path / "var/lib/cortex"),
            "systemd": str(tmp_path / "etc/systemd/system"),
            "polkit": str(tmp_path / "etc/polkit-1/rules.d"),
        },
        "apply_order": [],
        "required_credentials": [],
    }
    if overlay_sha is not None:
        plan["host_overlay_sha256"] = overlay_sha
    plan["receipt_path"] = str(install_core.canonical_receipt_path(plan))
    return plan


def durable_prior(
    tmp_path: Path, *, version: str = "0.1.12", qualified: bool = True
) -> InstallReceipt:
    """A real on-disk receipt; callers patch `_validate_receipt_parent/_file`."""

    plan = plan_document(
        tmp_path, version=version, wheel_sha256=PRIOR_WHEEL, commit=PRIOR_COMMIT
    )
    path = (tmp_path / "var/lib/cortex-install-receipts" / "prior.json").absolute()
    receipt = new_install_receipt(plan, path=path)
    receipt._document.update(state="applied", qualified=qualified)
    receipt._persist()
    return receipt


def make_prior(
    tmp_path: Path, *, version: str = "0.1.12", overlay_sha: str | None = None
) -> upgrade.PriorReceipt:
    """An in-memory prior receipt for coordinator tests."""

    plan = plan_document(
        tmp_path,
        version=version,
        wheel_sha256=PRIOR_WHEEL,
        commit=PRIOR_COMMIT,
        overlay_sha=overlay_sha,
    )
    path = tmp_path / "var/lib/cortex-install-receipts" / "prior.json"
    document = {
        "receipt_id": "prior-receipt",
        "plan": plan,
        "plan_sha256": plan_sha256(plan),
        "state": "applied",
        "qualified": True,
        "credentials": [],
    }
    return upgrade.PriorReceipt(
        path=path,
        receipt=InstallReceipt(document, path=path),
        version=parse_version(version),
    )


def make_sealed(
    tmp_path: Path,
    *,
    version: str = "0.1.13",
    commit: str = NEW_COMMIT,
    wheel_sha256: str = NEW_WHEEL,
) -> SealedCandidate:
    attempt = tmp_path / "installer" / version / "attempt-test"
    venv = attempt / "venv"
    (venv / "bin").mkdir(parents=True)
    cli = venv / "bin" / "cortex"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    input_root = attempt / "input"
    input_root.mkdir()
    (input_root / "bundle.json").write_text("{}\n", encoding="utf-8")
    (input_root / "install-config.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    wheel_name, input_name, qualification_name = asset_names(version)

    def asset(name: str, digest: str = "0" * 64) -> ReleaseAsset:
        return ReleaseAsset(
            name,
            digest,
            1,
            f"https://github.com/hamanpaul/paulsha-cortex/releases/download/v{version}/{name}",
        )

    metadata = ReleaseMetadata(
        version=version,
        tag=f"v{version}",
        commit=commit,
        wheel=asset(wheel_name, wheel_sha256),
        install_input=asset(input_name),
        qualification=asset(qualification_name),
    )
    return SealedCandidate(
        metadata=metadata,
        attempt_dir=attempt,
        input_root=input_root,
        bundle=input_root / "bundle.json",
        install_config=input_root / "install-config.yaml",
        venv=venv,
        cli=cli,
        tree_sha256=tree_sha256(venv, owner_uid=os.getuid()),
        owner_uid=os.getuid(),
    )


class FakePlanCli:
    """Stands in for `<sealed>/bin/cortex install trust-root plan ...` run unprivileged."""

    def __init__(self, plan: dict[str, object], *, reported_sha: str | None = None) -> None:
        self.plan = plan
        self.reported_sha = reported_sha
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, *, check=False, env=None, uid=None, gid=None, **_kwargs):
        argv = tuple(argv)
        self.calls.append({"argv": argv, "env": dict(env or {}), "uid": uid, "gid": gid})
        output = Path(argv[argv.index("--output") + 1])
        payload = install_core.canonical_plan_bytes(self.plan)
        output.write_bytes(payload)
        sha = self.reported_sha or hashlib.sha256(payload).hexdigest()
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"output": str(output), "plan_sha256": sha}), ""
        )


def account(uid: int, gid: int) -> SimpleNamespace:
    return SimpleNamespace(pw_uid=uid, pw_gid=gid)


SERVICES = install_cli._MAINTENANCE_SERVICES


def status_payload(receipt_id: str, wheel: str, commit: str) -> dict[str, object]:
    return {
        "service": {
            "loaded_runtime": {
                name: {
                    "comparison": {
                        "artifact_status": "match",
                        "config_status": "match",
                        "process_status": "match",
                        "loaded_wheel_sha256": wheel,
                    },
                    "trust_root": {
                        "status": "verified",
                        "receipt_id": receipt_id,
                        "wheel_sha256": wheel,
                        "candidate_commit": commit,
                    },
                    "installed_artifact": {"wheel_sha256": wheel},
                }
                for name in ("manager", "monitor")
            }
        }
    }


@dataclass
class FakeSystemd:
    active: set[str] = field(default_factory=lambda: set(SERVICES))
    fail_stop: set[str] = field(default_factory=set)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        verb, service = args[0], args[-1]
        if verb == "show":
            return subprocess.CompletedProcess(args, 0, "loaded\n", "")
        if verb == "is-active":
            return subprocess.CompletedProcess(args, 0 if service in self.active else 3, "", "")
        if verb == "stop":
            if service in self.fail_stop:
                return subprocess.CompletedProcess(args, 1, "", "stop failed")
            self.active.discard(service)
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "start":
            self.active.add(service)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected systemctl call: {args}")


class FakeCandidateCli:
    """Stands in for the sealed candidate installer and the installed status probe."""

    def __init__(self, systemd: FakeSystemd) -> None:
        self.systemd = systemd
        self.calls: list[tuple[str, ...]] = []
        # One entry per `self.calls` entry, in the same order: the `env`
        # mapping `_candidate`/`_service_status` actually passed to `_run`.
        self.envs: list[dict[str, str]] = []
        self.fail: dict[str, str] = {}
        self.interrupt_after: str | None = None
        # A verify that runs and FAILs: exit 1 with its JSON result on stdout.
        self.verify_failure: dict[str, object] | None = None
        self.rollback_result: dict[str, object] = {
            "restore_safe": True,
            "retained_unknown": [],
            "retained_drift": [],
            "systemd_daemon_reload": "not-required",
        }
        self.loaded: dict[str, tuple[str, str, str]] = {}

    @staticmethod
    def _value(argv: tuple[str, ...], flag: str) -> str:
        return argv[argv.index(flag) + 1]

    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]

    def __call__(self, argv, *, check=False, env=None, uid=None, gid=None, **_kwargs):
        argv = tuple(argv)
        self.envs.append(dict(env or {}))
        if argv[1:4] == ("service", "status", "--system"):
            receipt = self._value(argv, "--install-receipt")
            self.calls.append(("status", receipt))
            identity = self.loaded.get(receipt, ("unknown", "0" * 64, "0" * 40))
            return subprocess.CompletedProcess(argv, 0, json.dumps(status_payload(*identity)), "")
        assert argv[1:3] == ("install", "trust-root"), argv
        command = "credentials inherit" if argv[3] == "credentials" else argv[3]
        self.calls.append((command, *argv[4:]))
        self.systemd.calls.append(("candidate", command))
        if command in self.fail:
            return subprocess.CompletedProcess(
                argv, 1, "", f"trust-root install failed: {self.fail[command]}\n"
            )
        if command == "apply":
            receipt = Path(self._value(argv, "--receipt"))
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text("{}\n", encoding="utf-8")
            if self.interrupt_after == "apply":
                raise upgrade.UpgradeInterrupted("SIGTERM")
            payload: dict[str, object] = {
                "receipt": str(receipt),
                "receipt_id": "new-receipt",
                "state": "applied",
            }
        elif command == "credentials inherit":
            payload = {
                "receipt_id": "new-receipt",
                "inherited": [
                    {"principal": "builder", "provider": "codex", "inherited_from": "prior-receipt"}
                ],
            }
        elif command == "activate":
            self.systemd.active.update(SERVICES)
            payload = {"receipt_id": "new-receipt", "services_started": True, "qualified": False}
        elif command == "verify":
            if self.verify_failure is not None:
                Path(self._value(argv, "--evidence")).write_text(
                    '{"result":"fail"}\n', encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    argv, 1, json.dumps(self.verify_failure), ""
                )
            Path(self._value(argv, "--evidence")).write_text('{"result":"pass"}\n', encoding="utf-8")
            payload = {"ok": True}
        elif command == "rollback":
            self.systemd.active.clear()
            code = 0 if self.rollback_result.get("restore_safe") is True else 1
            return subprocess.CompletedProcess(argv, code, json.dumps(self.rollback_result), "")
        else:
            raise AssertionError(f"unexpected candidate command: {argv}")
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def make_bound(tmp_path: Path) -> upgrade.BoundPlan:
    plan = plan_document(tmp_path, version="0.1.13", wheel_sha256=NEW_WHEEL, commit=NEW_COMMIT)
    sha = plan_sha256(plan)
    canonical = Path(str(plan["receipt_path"]))
    return upgrade.BoundPlan(
        plan=plan,
        sha256=sha,
        durable_path=tmp_path / "installer" / "plans" / f"{sha}.json",
        receipt_path=canonical.with_name(f"{canonical.stem}.run-{'0' * 32}.json"),
        overlay_sha256=None,
    )
