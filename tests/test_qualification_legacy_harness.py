"""RC `legacy-adoption` profile: the in-container harness orchestration (#1122, PR-5).

The harness drives the installer CLI inside the disposable container.  Here it
runs against a fake command runner and a fake inode table, so the ordering,
the fail-closed checks and the evidence it writes are exercised without a
container, root or systemd.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION = REPO_ROOT / "qualification"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, QUALIFICATION / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fixture = _load("cortex_qualification_legacy_fixture", "legacy_fixture.py")
harness_module = _load("cortex_qualification_legacy_adoption", "legacy_adoption.py")

MANIFEST = fixture.load_manifest()
INVENTORY_SHA = "1" * 64
QUARANTINE_ROOT = MANIFEST["legacy_adoption"]["quarantine_root"]


class Result:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeHost:
    """The installer CLI and systemctl as seen through their outputs."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, ...]] = []
        self.identities: dict[str, tuple[int, int]] = {}
        self.services = {name: "inactive" for name in fixture.SERVICES}
        self.need_reload = "no"
        self.rollback_payload = {
            "legacy_restored": True,
            "restore_safe": True,
            "retained_unknown": [],
            "retained_drift": [],
            "systemd_daemon_reload": "completed",
        }
        self.rollback_rc = 0
        self.plan_sha_override: str | None = None
        self.leave_source_after_apply = False
        self.verify_result = "pass"
        self.authority_probe_failure = False
        self.authority_probe_output: str | None = None
        self.extra_quarantine: list[str] = []
        self.plan: dict | None = None
        self.sample_changes: dict[str, str] = {}
        self.environments: list[tuple[tuple[str, ...], object]] = []
        self.plan_preview: dict = {
            "ready": True,
            "failures": [],
            "quarantine": len(MANIFEST["expected"]["quarantine"]),
            "summary": {"adopt": 1},
        }
        self.sudoers: dict = {"accounts": [], "unproven": None}
        inode = 1000
        for path in MANIFEST["expected"]["quarantine"] + MANIFEST["expected"]["adopted_samples"]:
            inode += 1
            self.identities[path] = (77, inode)

    # -- filesystem view -------------------------------------------------
    def lstat(self, path: str):
        return self.identities.get(path)

    def sha256(self, path: str) -> str:
        return self.sample_changes.get(path, hashlib.sha256(path.encode()).hexdigest())

    # -- plan ------------------------------------------------------------
    def _plan(self) -> dict:
        steps = []
        for path in MANIFEST["expected"]["quarantine"] + self.extra_quarantine:
            dev, ino = self.identities.get(path, (77, 9))
            steps.append(
                {
                    "step_id": f"legacy-quarantine:{path}",
                    "kind": "legacy-quarantine",
                    "path": path,
                    "destination": f"{QUARANTINE_ROOT}/{INVENTORY_SHA[:16]}/root{path}",
                    "expected": {"type": "file", "dev": dev, "ino": ino},
                }
            )
        return {
            "apply_order": steps,
            "legacy_adoption": {"inventory_sha256": INVENTORY_SHA, "quarantine": []},
            "required_credentials": [
                {"principal": "builder", "provider": "codex"},
                {"principal": "manager", "provider": "github"},
                {"principal": "reviewer-planner", "provider": "agy"},
                {"principal": "reviewer-planner", "provider": "copilot"},
            ],
        }

    def plan_digest(self, plan: dict) -> str:
        return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()

    def _move(self, into_quarantine: bool) -> None:
        assert self.plan is not None
        for step in self.plan["apply_order"]:
            source, destination = step["path"], step["destination"]
            if into_quarantine:
                identity = self.identities.pop(source)
                self.identities[destination] = identity
                if self.leave_source_after_apply:
                    self.identities[source] = identity
                else:
                    self.identities[source] = (77, 50000 + len(self.identities))
            else:
                self.identities.pop(source, None)
                self.identities[source] = self.identities.pop(destination)

    # -- command runner ------------------------------------------------------
    def run(self, argv, env=None):
        argv = tuple(argv)
        if argv[0].endswith("/cortex"):
            # The harness may call the installer by absolute path (main()).
            argv = ("cortex", *argv[1:])
        self.calls.append(argv)
        self.environments.append((argv, env))
        if argv[0] == "systemctl":
            return self._systemctl(argv)
        if argv[0].endswith("/cortex-launch-authority-probe"):
            if self.authority_probe_failure:
                return Result(1, stderr="injected authority probe failure")
            if self.authority_probe_output is not None:
                return Result(stdout=self.authority_probe_output)
            plan_path = Path(argv[argv.index("--plan") + 1])
            required = json.loads(plan_path.read_text(encoding="utf-8"))[
                "required_credentials"
            ]
            providers = [
                {
                    "principal": str(row["principal"]),
                    "provider": str(row["provider"]),
                    "check": fixture.provider_check_for_principal(str(row["principal"])),
                }
                for row in required
            ]
            return Result(
                stdout=json.dumps({"status": "passed", "providers": providers})
            )
        assert argv[:3] == ("cortex", "install", "trust-root"), argv
        command = argv[3:]
        if command[:2] == ("legacy", "inventory"):
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps({"inventory_sha256": INVENTORY_SHA, "schema_version": 1}),
                encoding="utf-8",
            )
            return Result(
                stdout=json.dumps(
                    {
                        "output": str(output),
                        "inventory_sha256": INVENTORY_SHA,
                        "scope_sha256": "2" * 64,
                        "host_binding_sha256": "3" * 64,
                        "census_stable": True,
                        "plan_preview": self.plan_preview,
                        "cortex_account_universal_nopasswd": self.sudoers,
                    }
                )
            )
        if command[:2] == ("legacy", "show"):
            return Result(stdout="legacy inventory summary\n")
        if command[0] == "plan":
            self.plan = self._plan()
            output = Path(command[command.index("--output") + 1])
            raw = json.dumps(self.plan, sort_keys=True).encode()
            output.write_bytes(raw)
            reported = self.plan_sha_override or hashlib.sha256(raw).hexdigest()
            return Result(
                stdout=json.dumps(
                    {
                        "plan_sha256": reported,
                        "legacy_adoption": {
                            "inventory_sha256": INVENTORY_SHA,
                            "quarantine": len(self.plan["apply_order"]),
                            "summary": {"adopt": 1},
                        },
                    }
                )
            )
        if command[0] == "apply":
            self._move(into_quarantine=True)
            return Result(stdout=json.dumps({"state": "applied", "receipt_id": "r1"}))
        if command[0] == "rollback":
            payload = dict(self.rollback_payload)
            if payload["legacy_restored"] and payload["restore_safe"]:
                self._move(into_quarantine=False)
            return Result(self.rollback_rc, stdout=json.dumps(payload))
        if command[:2] == ("credentials", "import"):
            return Result(
                stdout=json.dumps(
                    {
                        "principal": command[command.index("--principal") + 1],
                        "provider": command[command.index("--provider") + 1],
                        "mode": "0600",
                        "sha256": "f" * 64,
                    }
                )
            )
        if command[0] == "activate":
            for name in self.services:
                self.services[name] = "active"
            return Result(stdout=json.dumps({"services_started": True}))
        if command[0] == "verify":
            evidence = Path(command[command.index("--evidence") + 1])
            evidence.write_text(json.dumps({"result": self.verify_result}), encoding="utf-8")
            return Result(
                0 if self.verify_result == "pass" else 1,
                stdout=json.dumps({"result": self.verify_result}),
            )
        raise AssertionError(f"unexpected command {argv}")

    def _systemctl(self, argv):
        action = argv[1]
        if action == "daemon-reload":
            return Result()
        if action in {"start", "stop"}:
            for name in argv[2:]:
                self.services[name] = "active" if action == "start" else "inactive"
            return Result()
        if action == "is-active":
            state = self.services[argv[2]]
            return Result(0 if state == "active" else 3, stdout=state + "\n")
        if action == "show":
            return Result(stdout=self.need_reload + "\n")
        raise AssertionError(f"unexpected systemctl {argv}")


def _harness(tmp_path: Path, host: FakeHost, **options):
    output = tmp_path / "output"
    output.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    return harness_module.Harness(
        **options,
        manifest=MANIFEST,
        config=tmp_path / "install-config.yaml",
        bundle=tmp_path / "bundle.json",
        output_dir=output,
        work_dir=work,
        installer_dir=tmp_path / "installer",
        candidate={
            "candidate_sha": "a" * 40,
            "wheel_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        },
        runner=host.run,
        lstat=host.lstat,
        sha256_file=host.sha256,
        plan_digest=host.plan_digest,
        seeder=lambda manifest: None,
        sleep=lambda _seconds: None,
    )


def _evidence(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "output" / "legacy-adoption.json").read_text())


def test_harness_runs_every_step_in_order_and_records_passing_evidence(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    harness = _harness(tmp_path, host)

    assert harness.run() == 0

    evidence = _evidence(tmp_path)
    assert evidence["status"] == "passed"
    assert evidence["profile"] == "legacy-adoption"
    assert [row["name"] for row in evidence["steps"]] == list(fixture.LEGACY_STEPS)
    assert all(row["status"] == "passed" for row in evidence["steps"])
    assert evidence["fixture"]["sha256"] == fixture.manifest_sha256()
    assert evidence["candidate"]["candidate_sha"] == "a" * 40

    plan = evidence["plan"]
    assert plan["reported_sha256"] == plan["observed_sha256"] == plan["recomputed_sha256"]
    assert plan["confirmed_sha256"] == plan["reported_sha256"]
    assert sorted(row["path"] for row in plan["quarantine"]) == MANIFEST["expected"]["quarantine"]

    assert evidence["apply"]["state"] == "applied"
    assert all(row["moved"] for row in evidence["apply"]["quarantine"])
    rollback = evidence["rollback"]
    assert rollback["legacy_restored"] is True and rollback["restore_safe"] is True
    assert all(row["restored"] for row in rollback["restored"])
    assert set(rollback["need_daemon_reload"].values()) == {"no"}
    assert all(row["unchanged"] for row in rollback["samples"])
    assert evidence["legacy_services"]["restarted"] == {
        name: "active" for name in fixture.SERVICES
    }
    assert evidence["reapply"]["state"] == "applied"
    imported = {(row["principal"], row["provider"]) for row in evidence["credentials"]}
    assert imported == {
        ("builder", "codex"),
        ("manager", "github"),
        ("reviewer-planner", "agy"),
        ("reviewer-planner", "copilot"),
    }
    assert all(row["source"].startswith(QUARANTINE_ROOT + "/") for row in evidence["credentials"])
    assert all("sha256" not in row for row in evidence["credentials"])
    assert evidence["verify"]["result"] == "pass"
    assert evidence["launcher_authorities"] == {
        "status": "passed",
        "providers": [
            {
                "principal": str(row["principal"]),
                "provider": str(row["provider"]),
                "check": fixture.provider_check_for_principal(str(row["principal"])),
            }
            for row in MANIFEST["credentials"]
        ],
    }

    # The rollback payload and the reviewed inventory become evidence files.
    rollback_file = json.loads((tmp_path / "output" / "legacy-rollback.json").read_text())
    assert rollback_file["legacy_restored"] is True
    inventory_file = json.loads((tmp_path / "output" / "legacy-inventory.json").read_text())
    assert inventory_file["inventory_sha256"] == INVENTORY_SHA
    # The published inventory and overlay live in the installer-owned tree.
    assert (tmp_path / "installer" / "legacy" / f"{INVENTORY_SHA}.json").is_file()
    overlay = json.loads((tmp_path / "installer" / "host-overlay.yaml").read_text())
    assert overlay["legacy_adoption"]["inventory_sha256"] == INVENTORY_SHA


def test_harness_runs_the_installer_with_the_tools_only_in_sbin(tmp_path: Path) -> None:
    # #1282: the RC run calls the installer the way the runbook did on the
    # reference host -- by absolute path, with no sbin directory on PATH --
    # so a tool resolved through PATH (visudo, useradd, groupadd) fails here.
    host = FakeHost(tmp_path)
    cli_path = "/usr/local/bin:/usr/bin:/bin"
    harness = _harness(
        tmp_path,
        host,
        cli=("/usr/local/bin/cortex", "install", "trust-root"),
        cli_env={"PATH": cli_path},
    )

    assert harness.run() == 0

    installer = [env for argv, env in host.environments if argv[0] == "cortex"]
    assert installer and all(env == {"PATH": cli_path} for env in installer)
    evidence = _evidence(tmp_path)
    assert evidence["cli_environment"] == {"path": cli_path, "sbin_on_path": False}
    # The S2 review facts the capture reported are evidence too.
    assert evidence["inventory"]["plan_preview"] == {
        "ready": True,
        "quarantine": len(MANIFEST["expected"]["quarantine"]),
    }
    assert evidence["inventory"]["cortex_account_universal_nopasswd"] == {
        "accounts": [],
        "unproven": None,
    }


def test_harness_main_resolves_the_installer_and_drops_sbin_from_its_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    class Recorder:
        def __init__(self, **kwargs) -> None:
            seen.update(kwargs)

        def run(self) -> int:
            return 0

    monkeypatch.setattr(harness_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        harness_module.shutil,
        "which",
        lambda name, path=None: "/usr/local/bin/cortex" if name == "cortex" else None,
    )
    monkeypatch.setattr(harness_module, "Harness", Recorder)

    assert harness_module.main(
        [
            "--config",
            "/artifacts/install-config.yaml",
            "--bundle",
            "/artifacts/bundle.json",
            "--output-dir",
            "/qualification-output",
            "--candidate-sha",
            "a" * 40,
            "--wheel-sha256",
            "b" * 64,
            "--bundle-sha256",
            "c" * 64,
        ]
    ) == 0
    assert seen["cli"] == ("/usr/local/bin/cortex", "install", "trust-root")
    assert seen["cli_env"] == {"PATH": "/usr/local/bin:/usr/bin:/bin"}
    assert "sbin" not in seen["cli_env"]["PATH"]


def test_harness_order_rolls_back_before_credentials_and_activation(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    assert _harness(tmp_path, host).run() == 0
    commands = [call[3] for call in host.calls if call[:3] == ("cortex", "install", "trust-root")]
    first_apply = commands.index("apply")
    rollback = commands.index("rollback")
    second_apply = commands.index("apply", rollback)
    credentials = commands.index("credentials")
    activate = commands.index("activate")
    assert first_apply < rollback < second_apply < credentials < activate
    apply_call = next(call for call in host.calls if call[3:4] == ("apply",))
    assert "--legacy-inventory" in apply_call and "--prior-receipt" not in apply_call
    # Services stop before the first apply and after the legacy restart.
    stops = [index for index, call in enumerate(host.calls) if call[:2] == ("systemctl", "stop")]
    assert len(stops) == 2


@pytest.mark.parametrize(
    ("configure", "step", "message"),
    [
        (lambda host: setattr(host, "plan_sha_override", "9" * 64), "plan", "plan SHA"),
        (
            lambda host: host.extra_quarantine.append("/var/lib/cortex/unexpected"),
            "plan",
            "quarantine",
        ),
        (lambda host: setattr(host, "leave_source_after_apply", True), "apply", "still holds"),
        (
            lambda host: host.rollback_payload.update(
                legacy_restored=False,
                restore_safe=False,
                retained_drift=[{"step_id": "legacy-inventory", "observed": {}}],
            ),
            "rollback",
            "legacy_restored",
        ),
        (lambda host: setattr(host, "need_reload", "yes"), "rollback", "NeedDaemonReload"),
        (lambda host: setattr(host, "verify_result", "fail"), "verify", "verify"),
        (
            lambda host: setattr(host, "authority_probe_failure", True),
            "launcher-authorities",
            "authority probe failed",
        ),
        (
            lambda host: setattr(host, "authority_probe_output", ""),
            "launcher-authorities",
            "returned no evidence",
        ),
        (
            lambda host: host.plan_preview.update(
                ready=False, failures=["unclassified: /var/lib/cortex-manager/x"]
            ),
            "inventory",
            "plan preview",
        ),
        (
            lambda host: host.sudoers.update(accounts=["cortex-builder"]),
            "inventory",
            "NOPASSWD",
        ),
        (
            lambda host: host.sudoers.update(unproven="visudo not found"),
            "inventory",
            "NOPASSWD",
        ),
    ],
    ids=[
        "plan-sha",
        "quarantine-set",
        "source-not-moved",
        "rollback-not-restored",
        "stale-units",
        "verify-failed",
        "authority-probe",
        "authority-probe-no-evidence",
        "preview-not-ready",
        "sudoers-offender",
        "sudoers-unproven",
    ],
)
def test_harness_fails_closed_and_names_the_failing_step(
    tmp_path: Path, configure, step: str, message: str
) -> None:
    host = FakeHost(tmp_path)
    configure(host)

    assert _harness(tmp_path, host).run() == 1

    evidence = _evidence(tmp_path)
    assert evidence["status"] == "failed"
    failed = [row for row in evidence["steps"] if row["status"] == "failed"]
    assert [row["name"] for row in failed] == [step]
    assert message in failed[0]["error"]
    names = [row["name"] for row in evidence["steps"]]
    assert names == list(fixture.LEGACY_STEPS[: fixture.LEGACY_STEPS.index(step) + 1])


def test_harness_rollback_rejects_a_changed_adopted_sample(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    sample = MANIFEST["expected"]["adopted_samples"][0]
    harness = _harness(tmp_path, host)
    original = harness._rollback

    def tamper_then_rollback():
        host.sample_changes[sample] = "0" * 64
        return original()

    harness._rollback = tamper_then_rollback
    assert harness.run() == 1
    failed = [row for row in _evidence(tmp_path)["steps"] if row["status"] == "failed"]
    assert failed[0]["name"] == "rollback"
    assert sample in failed[0]["error"]


def test_harness_never_writes_credential_digests_or_contents(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    assert _harness(tmp_path, host).run() == 0
    for path in (tmp_path / "output").iterdir():
        content = path.read_bytes()
        for secret in fixture.credential_secrets(MANIFEST):
            assert secret not in content
        assert b"f" * 64 not in content


def test_harness_cli_requires_root_inside_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness_module.os, "geteuid", lambda: 1000)
    assert (
        harness_module.main(
            [
                "--config",
                "/artifacts/install-config.yaml",
                "--bundle",
                "/artifacts/bundle.json",
                "--output-dir",
                "/qualification-output",
                "--candidate-sha",
                "a" * 40,
                "--wheel-sha256",
                "b" * 64,
                "--bundle-sha256",
                "c" * 64,
            ]
        )
        == 2
    )
