#!/usr/bin/env python3
"""RC `legacy-adoption` profile harness: adopt a Phase 2b-shaped legacy host.

Runs as root inside the disposable qualification container (never on a host),
after the exact candidate wheel is installed.  It lays out the fixture from
`legacy_fixture.json`, then drives only the public installer CLI:

  fixture -> legacy inventory (plan preview and sudoers preflight must be
  clean before any review list) -> host overlay -> plan (three-way plan SHA)
  -> stop the legacy services -> apply --legacy-inventory
  -> rollback (legacy_restored, restore_safe, every quarantined object back at
     its path with the same inode, no stale unit definitions)
  -> restart and stop the legacy services -> apply again (re-adoption)
  -> credential reimport from the quarantine -> activate -> verify

The rollback proof runs before credentials and activation: once the new
services run they write runtime state the rollback must (by design) retain as
unknown, so a post-activation rollback can never be restore-safe.  Every step
is written to `legacy-adoption.json`; the driver turns it into
qualification.json and the validator re-checks it.  No provider credential,
provider smoke or GitHub access is involved.

The installer runs the way the runbook ran it on the reference host (#1282):
by absolute path, with a PATH that holds the installer's own directory and
`/usr/bin:/bin` but no sbin directory, so any tool the installer still looked
up through PATH (`visudo`, `useradd`, `groupadd` live in `/usr/sbin`) fails
the profile instead of a host adoption.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from qualification import legacy_fixture as fixture
except ModuleNotFoundError:  # run as qualification/legacy_adoption.py from a checkout
    import legacy_fixture as fixture  # type: ignore[no-redef]


CLI = ("cortex", "install", "trust-root")
#: The `docker exec` PATH the installer itself is resolved on.
FULL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
#: The system half of the installer PATH: the runbook's, without any sbin.
CLI_SYSTEM_PATH = "/usr/bin:/bin"
DEFAULT_WORK_DIR = Path("/run/cortex-install")
SERVICE_WAIT_ATTEMPTS = 30
STOPPED_STATES = frozenset({"inactive", "failed"})


class HarnessError(RuntimeError):
    """A legacy adoption step did not meet its qualification check."""


class CommandResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_command(argv: Sequence[str], env: Mapping[str, str] | None = None) -> CommandResult:
    import subprocess

    # The same environment `docker exec` gives the release profile's CLI calls.
    process_env = {
        "PATH": FULL_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": "/root",
    }
    if env:
        process_env.update(env)
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=process_env,
        timeout=1800,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _identity(path: str) -> tuple[int, int] | None:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    return observed.st_dev, observed.st_ino


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_plan_digest(plan: Mapping[str, Any]) -> str:
    from paulsha_cortex.trust_root.install.core import plan_sha256

    return plan_sha256(plan)


def _seed_container(manifest: Mapping[str, Any]) -> None:
    fixture.seed(manifest, root=Path("/"), rootless=False)


def _write_new(path: Path, payload: bytes, *, mode: int) -> None:
    """Write a file that must not exist yet (no-overwrite publication)."""

    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class Harness:
    def __init__(
        self,
        *,
        manifest: Mapping[str, Any],
        config: Path,
        bundle: Path,
        output_dir: Path,
        candidate: Mapping[str, str],
        work_dir: Path = DEFAULT_WORK_DIR,
        installer_dir: Path | None = None,
        runner: Callable[..., Any] = _run_command,
        cli: Sequence[str] = CLI,
        cli_env: Mapping[str, str] | None = None,
        lstat: Callable[[str], tuple[int, int] | None] = _identity,
        sha256_file: Callable[[str], str] = _sha256_file,
        plan_digest: Callable[[Mapping[str, Any]], str] = _installed_plan_digest,
        seeder: Callable[[Mapping[str, Any]], None] = _seed_container,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.manifest = manifest
        self.config = Path(config)
        self.bundle = Path(bundle)
        self.output_dir = Path(output_dir)
        self.work_dir = Path(work_dir)
        block = manifest["legacy_adoption"]
        inventory_dir = Path(block["inventory_directory"])
        overlay_path = Path(block["host_overlay_path"])
        if installer_dir is not None:
            inventory_dir = Path(installer_dir) / inventory_dir.name
            overlay_path = Path(installer_dir) / overlay_path.name
        self.inventory_dir = inventory_dir
        self.overlay_path = overlay_path
        self.candidate = dict(candidate)
        self.runner = runner
        self.cli = tuple(cli)
        self.cli_env = dict(cli_env) if cli_env is not None else None
        self.lstat = lstat
        self.sha256_file = sha256_file
        self.plan_digest = plan_digest
        self.seeder = seeder
        self.sleep = sleep
        self.plan_path = self.work_dir / "install-plan.json"
        self.receipt_path = self.work_dir / "install-receipt.json"
        self.verify_path = self.output_dir / "install-verification.json"
        self.services = list(fixture.SERVICES)
        self.evidence: dict[str, Any] = {
            "schema_version": 1,
            "profile": fixture.LEGACY_PROFILE,
            "status": "failed",
            "candidate": dict(candidate),
            "steps": [],
        }
        if self.cli_env is not None:
            path = self.cli_env.get("PATH", "")
            self.evidence["cli_environment"] = {
                "path": path,
                "sbin_on_path": any(
                    part.rstrip("/").endswith("sbin") for part in path.split(":")
                ),
            }
        self.inventory_sha256: str | None = None
        self.inventory_path: Path | None = None
        self.plan: dict[str, Any] | None = None
        self.plan_sha256: str | None = None
        self.quarantine: list[dict[str, Any]] = []
        self.samples: dict[str, dict[str, Any]] = {}

    # -- helpers ---------------------------------------------------------

    def _cli(self, *arguments: str, allow_failure: bool = False) -> tuple[CommandResult, Any]:
        result = self.runner((*self.cli, *arguments), env=self.cli_env)
        payload: Any = None
        text = (result.stdout or "").strip()
        if text:
            try:
                payload = json.loads(text.splitlines()[-1])
            except json.JSONDecodeError:
                payload = None
        if result.returncode != 0 and not allow_failure:
            detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")[:800]
            raise HarnessError(
                f"cortex install trust-root {' '.join(arguments[:2])} failed "
                f"rc={result.returncode}: {detail}"
            )
        return result, payload

    def _systemctl(self, *arguments: str) -> CommandResult:
        return self.runner(("systemctl", *arguments))

    def _service_state(self, service: str) -> str:
        return (self._systemctl("is-active", service).stdout or "").strip() or "unknown"

    def _wait_services(self, wanted: frozenset[str]) -> dict[str, str]:
        states: dict[str, str] = {}
        for _attempt in range(SERVICE_WAIT_ATTEMPTS):
            states = {service: self._service_state(service) for service in self.services}
            if all(state in wanted for state in states.values()):
                return states
            self.sleep(1)
        raise HarnessError(
            "legacy services did not reach "
            + "/".join(sorted(wanted))
            + ": "
            + ", ".join(f"{name} is {state}" for name, state in sorted(states.items()))
        )

    def _start_legacy(self) -> dict[str, str]:
        result = self._systemctl("start", *self.services)
        if result.returncode != 0:
            raise HarnessError(f"legacy services failed to start: {result.stderr.strip()[:400]}")
        return self._wait_services(frozenset({"active"}))

    def _stop_legacy(self) -> dict[str, str]:
        result = self._systemctl("stop", *reversed(self.services))
        if result.returncode != 0:
            raise HarnessError(f"legacy services failed to stop: {result.stderr.strip()[:400]}")
        states = self._wait_services(STOPPED_STATES)
        return states

    def _sample_rows(self) -> list[dict[str, Any]]:
        rows = []
        for path, baseline in sorted(self.samples.items()):
            identity = self.lstat(path)
            digest = self.sha256_file(path) if identity is not None else None
            unchanged = identity == (baseline["dev"], baseline["ino"]) and digest == baseline["sha256"]
            if not unchanged:
                raise HarnessError(
                    f"adopted legacy state {path} changed: it must stay in place with "
                    "the same inode and bytes"
                )
            rows.append({"path": path, "dev": baseline["dev"], "ino": baseline["ino"], "unchanged": True})
        return rows

    def _check_quarantined(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.quarantine:
            legacy = (row["dev"], row["ino"])
            if self.lstat(row["destination"]) != legacy:
                raise HarnessError(
                    f"quarantine destination {row['destination']} does not hold the "
                    f"legacy inode of {row['path']}"
                )
            if self.lstat(row["path"]) == legacy:
                raise HarnessError(
                    f"{row['path']} still holds the legacy inode after apply; it was not quarantined"
                )
            rows.append({"path": row["path"], "destination": row["destination"], "moved": True})
        return rows

    def _apply(self, section: str) -> dict[str, Any]:
        assert self.plan_sha256 is not None and self.inventory_path is not None
        _result, payload = self._cli(
            "apply",
            "--plan",
            str(self.plan_path),
            "--confirm-sha256",
            self.plan_sha256,
            "--receipt",
            str(self.receipt_path),
            "--legacy-inventory",
            str(self.inventory_path),
        )
        if not isinstance(payload, Mapping) or payload.get("state") != "applied":
            raise HarnessError(f"{section} did not leave the receipt applied: {payload}")
        return {
            "state": "applied",
            "quarantine": self._check_quarantined(),
            "samples": self._sample_rows(),
        }

    # -- steps -----------------------------------------------------------

    def _fixture(self) -> None:
        self.seeder(self.manifest)
        for path in self.manifest["expected"]["adopted_samples"]:
            identity = self.lstat(path)
            if identity is None:
                raise HarnessError(f"fixture sample {path} was not laid out")
            self.samples[path] = {
                "dev": identity[0],
                "ino": identity[1],
                "sha256": self.sha256_file(path),
            }
        reload = self._systemctl("daemon-reload")
        if reload.returncode != 0:
            raise HarnessError("systemctl daemon-reload failed after seeding the fixture")
        states = self._start_legacy()
        self.evidence["fixture"] = {
            "sha256": fixture.manifest_sha256(),
            "accounts": {
                name: {"uid": row["uid"], "gid": row["gid"]}
                for name, row in sorted(fixture.accounts(self.manifest).items())
            },
            "services": states,
        }

    def _inventory(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        base_overlay = self.work_dir / "host-overlay.base.yaml"
        base_overlay.unlink(missing_ok=True)
        _write_new(base_overlay, _canonical(fixture.host_overlay(self.manifest)), mode=0o600)
        capture = self.work_dir / "legacy-inventory.capture.json"
        capture.unlink(missing_ok=True)
        _result, payload = self._cli(
            "legacy",
            "inventory",
            "--config",
            str(self.config),
            "--host-overlay",
            str(base_overlay),
            "--bundle",
            str(self.bundle),
            "--output",
            str(capture),
        )
        if not isinstance(payload, Mapping):
            raise HarnessError("legacy inventory printed no JSON result")
        if payload.get("census_stable") is not True:
            raise HarnessError("legacy inventory census is unstable; the capture cannot bind a plan")
        # #1282: the root capture is the S2 review.  The fixture's census
        # finding, stale sockets/FIFO and HOME residue are all covered by the
        # installer itself, so the plan preview is ready before any review
        # list, and the sudoers preflight (unreadable without root) is clean.
        preview = payload.get("plan_preview")
        if not isinstance(preview, Mapping) or preview.get("ready") is not True:
            failures = preview.get("failures") if isinstance(preview, Mapping) else None
            raise HarnessError(
                "legacy inventory plan preview is not ready before review: "
                f"{json.dumps(failures)[:1200]}"
            )
        sudoers = payload.get("cortex_account_universal_nopasswd")
        if sudoers != {"accounts": [], "unproven": None}:
            raise HarnessError(
                "legacy inventory sudoers preflight is not clean (universal NOPASSWD "
                f"for cortex accounts or unproven): {json.dumps(sudoers)[:400]}"
            )
        digest = payload.get("inventory_sha256")
        raw = capture.read_bytes()
        document = json.loads(raw)
        if not isinstance(digest, str) or document.get("inventory_sha256") != digest:
            raise HarnessError("legacy inventory file does not carry the reported digest")
        self.inventory_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        published = self.inventory_dir / f"{digest}.json"
        _write_new(published, raw, mode=0o644)
        (self.output_dir / "legacy-inventory.json").write_bytes(raw)
        show, _payload = self._cli("legacy", "show", "--inventory", str(published))
        summary = show.stdout.encode()
        self.inventory_sha256 = digest
        self.inventory_path = published
        self.evidence["inventory"] = {
            "path": str(published),
            "inventory_sha256": digest,
            "scope_sha256": payload.get("scope_sha256"),
            "host_binding_sha256": payload.get("host_binding_sha256"),
            "census_stable": True,
            "summary_sha256": hashlib.sha256(summary).hexdigest(),
            "summary_lines": len(show.stdout.splitlines()),
            "plan_preview": {"ready": True, "quarantine": preview.get("quarantine")},
            "cortex_account_universal_nopasswd": {"accounts": [], "unproven": None},
        }

    def _plan(self) -> None:
        assert self.inventory_sha256 is not None and self.inventory_path is not None
        overlay = fixture.host_overlay(self.manifest, inventory_sha256=self.inventory_sha256)
        self.overlay_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_new(self.overlay_path, _canonical(overlay), mode=0o600)
        self.plan_path.unlink(missing_ok=True)
        _result, payload = self._cli(
            "plan",
            "--config",
            str(self.config),
            "--host-overlay",
            str(self.overlay_path),
            "--bundle",
            str(self.bundle),
            "--legacy-inventory",
            str(self.inventory_path),
            "--output",
            str(self.plan_path),
        )
        if not isinstance(payload, Mapping):
            raise HarnessError("plan printed no JSON result")
        raw = self.plan_path.read_bytes()
        plan = json.loads(raw)
        reported = payload.get("plan_sha256")
        observed = hashlib.sha256(raw).hexdigest()
        recomputed = self.plan_digest(plan)
        if not (reported == observed == recomputed):
            raise HarnessError(
                "plan SHA-256 three-way confirmation failed: "
                f"reported={reported} observed={observed} recomputed={recomputed}"
            )
        legacy_block = payload.get("legacy_adoption")
        if not isinstance(legacy_block, Mapping) or legacy_block.get(
            "inventory_sha256"
        ) != self.inventory_sha256:
            raise HarnessError("plan is not bound to the captured legacy inventory")
        steps = fixture.quarantine_steps(plan)
        paths = sorted(str(step["path"]) for step in steps)
        expected = sorted(self.manifest["expected"]["quarantine"])
        if paths != expected:
            raise HarnessError(
                "plan quarantine set differs from the fixture: unexpected "
                f"{sorted(set(paths) - set(expected))}, missing {sorted(set(expected) - set(paths))}"
            )
        self.quarantine = [
            {
                "path": str(step["path"]),
                "destination": str(step["destination"]),
                "type": step["expected"].get("type"),
                "dev": int(step["expected"]["dev"]),
                "ino": int(step["expected"]["ino"]),
            }
            for step in steps
        ]
        self.plan = plan
        self.plan_sha256 = observed
        self.evidence["plan"] = {
            "path": str(self.plan_path),
            "host_overlay_path": str(self.overlay_path),
            "reported_sha256": reported,
            "observed_sha256": observed,
            "recomputed_sha256": recomputed,
            "confirmed_sha256": observed,
            "summary": legacy_block.get("summary"),
            "quarantine": self.quarantine,
        }

    def _stop_legacy_services(self) -> None:
        self.evidence["legacy_services"] = {"stopped_before_apply": self._stop_legacy()}

    def _apply_step(self) -> None:
        self.evidence["apply"] = self._apply("apply")

    def _rollback(self) -> None:
        result, payload = self._cli("rollback", "--receipt", str(self.receipt_path), allow_failure=True)
        if not isinstance(payload, Mapping):
            detail = (result.stderr or "").strip().replace("\n", " ")[:800]
            raise HarnessError(
                f"rollback printed no JSON result (rc={result.returncode}): {detail}"
            )
        (self.output_dir / "legacy-rollback.json").write_bytes(
            _canonical({"returncode": result.returncode, **payload})
        )
        if (
            result.returncode != 0
            or payload.get("legacy_restored") is not True
            or payload.get("restore_safe") is not True
            or payload.get("retained_unknown") != []
            or payload.get("retained_drift") != []
        ):
            raise HarnessError(
                "rollback did not restore the legacy host: "
                f"legacy_restored={payload.get('legacy_restored')} "
                f"restore_safe={payload.get('restore_safe')} "
                f"retained_unknown={payload.get('retained_unknown')} "
                f"retained_drift={json.dumps(payload.get('retained_drift'))[:1200]}"
            )
        restored = []
        for row in self.quarantine:
            if self.lstat(row["path"]) != (row["dev"], row["ino"]):
                raise HarnessError(
                    f"{row['path']} is not the legacy inode again after rollback"
                )
            if self.lstat(row["destination"]) is not None:
                raise HarnessError(f"quarantine destination {row['destination']} still exists")
            restored.append(
                {"path": row["path"], "dev": row["dev"], "ino": row["ino"], "restored": True}
            )
        need_reload: dict[str, str] = {}
        for service in self.services:
            value = (
                self._systemctl("show", "--property=NeedDaemonReload", "--value", service).stdout
                or ""
            ).strip()
            if value != "no":
                raise HarnessError(
                    f"NeedDaemonReload={value or 'unknown'} for {service} after rollback; "
                    "systemd still holds the adopted unit definitions"
                )
            need_reload[service] = value
        self.evidence["rollback"] = {
            "legacy_restored": True,
            "restore_safe": True,
            "retained_unknown": [],
            "retained_drift": [],
            "systemd_daemon_reload": payload.get("systemd_daemon_reload"),
            "restored": restored,
            "need_daemon_reload": need_reload,
            "samples": self._sample_rows(),
        }

    def _restart_legacy_services(self) -> None:
        restarted = self._start_legacy()
        stopped = self._stop_legacy()
        services = self.evidence.setdefault("legacy_services", {})
        services["restarted"] = restarted
        services["stopped_before_reapply"] = stopped

    def _reapply(self) -> None:
        self.evidence["reapply"] = self._apply("reapply")

    def _credentials(self) -> None:
        assert self.plan is not None
        rows = []
        for (principal, provider), source in sorted(
            fixture.credential_sources(self.manifest, self.plan).items()
        ):
            _result, payload = self._cli(
                "credentials",
                "import",
                "--receipt",
                str(self.receipt_path),
                "--principal",
                principal,
                "--provider",
                provider,
                "--source",
                source,
            )
            if not isinstance(payload, Mapping) or (
                payload.get("principal"),
                payload.get("provider"),
            ) != (principal, provider):
                raise HarnessError(f"credential import for {principal}/{provider} returned no metadata")
            # The content digest of even a fake credential stays out of evidence.
            rows.append(
                {
                    "principal": principal,
                    "provider": provider,
                    "source": source,
                    "mode": payload.get("mode"),
                }
            )
        self.evidence["credentials"] = rows

    def _activate(self) -> None:
        _result, payload = self._cli("activate", "--receipt", str(self.receipt_path))
        if not isinstance(payload, Mapping) or payload.get("services_started") is not True:
            raise HarnessError("activate did not start the adopted services")
        self.evidence["activate"] = {"services_started": True}

    def _verify(self) -> None:
        self.verify_path.unlink(missing_ok=True)
        result, payload = self._cli(
            "verify",
            "--receipt",
            str(self.receipt_path),
            "--json",
            "--evidence",
            str(self.verify_path),
            allow_failure=True,
        )
        written = json.loads(self.verify_path.read_text(encoding="utf-8")) if self.verify_path.is_file() else None
        if (
            result.returncode != 0
            or not isinstance(payload, Mapping)
            or payload.get("result") != "pass"
            or not isinstance(written, Mapping)
            or written.get("result") != "pass"
        ):
            failures = (
                payload.get("attestation", {}).get("failures")
                if isinstance(payload, Mapping) and isinstance(payload.get("attestation"), Mapping)
                else None
            )
            detail = (result.stderr or "").strip().replace("\n", " ")[:600]
            raise HarnessError(
                f"trust-root verify of the re-adopted host did not pass (rc={result.returncode}); "
                f"failures={json.dumps(failures)[:1200]} {detail}"
            )
        self.evidence["verify"] = {"result": "pass", "evidence": self.verify_path.name}

    def _launcher_authorities(self) -> None:
        assert self.plan is not None
        required = self.plan.get("required_credentials")
        if not isinstance(required, list):
            raise HarnessError("install plan has no required credential roster")
        expected = [
            {
                "principal": str(row["principal"]),
                "provider": str(row["provider"]),
                "check": fixture.provider_check_for_principal(str(row["principal"])),
            }
            for row in required
            if isinstance(row, Mapping)
        ]
        result = self.runner(
            (
                "/usr/local/libexec/cortex-launch-authority-probe",
                "--plan",
                str(self.plan_path),
                "--receipt",
                str(self.receipt_path),
            )
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")[:800]
            raise HarnessError(f"Manager launcher authority probe failed: {detail}")
        output = (result.stdout or "").strip()
        if not output:
            raise HarnessError("Manager launcher authority probe returned no evidence")
        try:
            payload = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise HarnessError("Manager launcher authority probe returned invalid JSON") from exc
        if not isinstance(payload, Mapping) or payload.get("status") != "passed" or payload.get(
            "providers"
        ) != expected:
            raise HarnessError("Manager launcher authority probe did not cover every imported provider")
        self.evidence["launcher_authorities"] = {
            "status": "passed",
            "providers": expected,
        }

    # -- driver ------------------------------------------------------------

    def _write_evidence(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "legacy-adoption.json").write_bytes(_canonical(self.evidence))

    def run(self) -> int:
        handlers = {
            "fixture": "_fixture",
            "inventory": "_inventory",
            "plan": "_plan",
            "stop-legacy-services": "_stop_legacy_services",
            "apply": "_apply_step",
            "rollback": "_rollback",
            "restart-legacy-services": "_restart_legacy_services",
            "reapply": "_reapply",
            "credentials": "_credentials",
            "activate": "_activate",
            "verify": "_verify",
            "launcher-authorities": "_launcher_authorities",
        }
        assert tuple(handlers) == fixture.LEGACY_STEPS
        for name in fixture.LEGACY_STEPS:
            try:
                getattr(self, handlers[name])()
            except (HarnessError, fixture.FixtureError, OSError, ValueError, KeyError) as exc:
                self.evidence["steps"].append({"name": name, "status": "failed", "error": str(exc)})
                self._write_evidence()
                print(f"legacy-adoption qualification failed at {name}: {exc}", file=sys.stderr)
                return 1
            self.evidence["steps"].append({"name": name, "status": "passed"})
            print(f"legacy-adoption step passed: {name}", file=sys.stderr)
        self.evidence["status"] = "passed"
        self._write_evidence()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("legacy-adoption harness must run as root inside the qualification container", file=sys.stderr)
        return 2
    try:
        manifest = fixture.load_manifest()
    except fixture.FixtureError as exc:
        print(f"legacy-adoption harness: {exc}", file=sys.stderr)
        return 2
    installer = shutil.which(CLI[0], path=FULL_PATH)
    if installer is None or not os.path.isabs(installer):
        print("legacy-adoption harness: the candidate cortex CLI is not installed", file=sys.stderr)
        return 2
    harness = Harness(
        manifest=manifest,
        config=args.config,
        bundle=args.bundle,
        output_dir=args.output_dir,
        work_dir=args.work_dir,
        cli=(installer, *CLI[1:]),
        cli_env={"PATH": f"{os.path.dirname(installer)}:{CLI_SYSTEM_PATH}"},
        candidate={
            "candidate_sha": args.candidate_sha,
            "wheel_sha256": args.wheel_sha256,
            "bundle_sha256": args.bundle_sha256,
        },
    )
    return harness.run()


if __name__ == "__main__":
    raise SystemExit(main())
