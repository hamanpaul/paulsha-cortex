"""Phase 2 trust-root installer RED contract: credentials and activation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import (
    ActivationError,
    CredentialImportError,
    InstallReceipt,
    activate_receipt,
    apply_plan,
    import_credential,
    new_install_receipt,
    plan_sha256,
    rollback_receipt,
)
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core
from paulsha_cortex.trust_root.install.backend import LocalInstallBackend


def _plan(*, required_credentials=None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "repo_identity": {"commit": "a" * 40},
        "candidate": {
            "wheel_sha256": "b" * 64,
            "bundle_sha256": "c" * 64,
        },
        "accounts": [],
        "apply_order": [],
        "required_credentials": required_credentials or [],
    }


class CredentialBackend:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.fail_start: str | None = None
        self.fail_stop: str | None = None

    def preflight_facts(self, _plan):
        return {
            "systemd": True,
            "polkit": True,
            "cgroup_v2": True,
            "acl": True,
            "disk_free_bytes": 2 * 1024 * 1024 * 1024,
            "universal_nopasswd": False,
            "in_flight_jobs": 0,
            "services": {
                "cortex-egress-proxy.service": "inactive",
                "cortex-manager.service": "inactive",
                "cortex-monitor.service": "inactive",
            },
            "accounts": {},
            "paths": {},
        }

    def inspect_step(self, _step):  # pragma: no cover - empty plan contract
        raise AssertionError("empty apply plan must not inspect a step")

    def apply_step(self, _step):  # pragma: no cover - empty plan contract
        raise AssertionError("empty apply plan must not mutate a step")

    def start_service(self, name: str) -> None:
        self.started.append(name)
        if self.fail_start == name:
            raise RuntimeError(f"injected start failure: {name}")

    def stop_service(self, name: str) -> None:
        self.stopped.append(name)
        if self.fail_stop == name:
            raise RuntimeError(f"injected stop failure: {name}")


def _applied_receipt(*, required_credentials=None):
    plan = _plan(required_credentials=required_credentials)
    backend = CredentialBackend()
    receipt = new_install_receipt(plan)
    apply_plan(
        plan,
        confirm_sha256=plan_sha256(plan),
        receipt=receipt,
        backend=backend,
    )
    return plan, receipt, backend


def test_codex_import_accepts_only_the_allowlisted_regular_filename(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    secret = "test-secret-must-never-be-rendered"
    source.write_text(json.dumps({"OPENAI_API_KEY": secret}), encoding="utf-8")
    destination_root = tmp_path / "installed-credentials"

    imported = import_credential(
        receipt,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=destination_root,
    )

    metadata = imported.to_dict()
    assert metadata == {
        "principal": "builder",
        "provider": "codex",
        "mode": "0600",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    rendered = json.dumps(
        {"result": metadata, "receipt": receipt.to_dict()}, sort_keys=True
    )
    assert secret not in rendered
    assert str(source) not in rendered

    installed = [path for path in destination_root.rglob("*") if path.is_file()]
    assert len(installed) == 1
    assert installed[0].read_bytes() == source.read_bytes()
    assert os.stat(installed[0], follow_symlinks=False).st_mode & 0o777 == 0o600
    assert not list(destination_root.rglob("*.tmp")), "atomic temp must not be stranded"


def test_credential_adapter_rejects_a_non_allowlisted_source_name(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "token.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")

    with pytest.raises(CredentialImportError, match="allowlist|auth.json"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=tmp_path / "installed",
        )
    assert not (tmp_path / "installed").exists()


def test_agy_import_writes_physical_cache_target_without_following_home_symlink(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "oauth_creds.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    home = tmp_path / "cortex-reviewer-planner"
    target = home / "cache/gemini"
    target.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".gemini").symlink_to(outside, target_is_directory=True)

    import_credential(
        receipt,
        principal="reviewer-planner",
        provider="agy",
        source=source,
        destination_root=home,
    )

    assert (target / "oauth_creds.json").read_bytes() == source.read_bytes()
    assert not (outside / "oauth_creds.json").exists()


def test_credential_adapter_rejects_a_symlink_before_reading_content(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    real = tmp_path / "real-auth.json"
    real.write_text('{"token":"test-secret"}', encoding="utf-8")
    source = tmp_path / "auth.json"
    source.symlink_to(real)

    with pytest.raises(CredentialImportError, match="symlink"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=tmp_path / "installed",
        )
    assert not (tmp_path / "installed").exists()


def test_provider_principal_pair_is_fail_closed(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "hosts.yml"
    source.write_text("github.com:\n  oauth_token: test-secret\n", encoding="utf-8")

    with pytest.raises(CredentialImportError, match="builder|github"):
        import_credential(
            receipt,
            principal="builder",
            provider="github",
            source=source,
            destination_root=tmp_path / "installed",
        )


def test_credential_replace_then_receipt_persist_crash_recovers_without_orphan(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "installed"
    durable_snapshots: list[dict[str, object]] = []
    persist_count = 0

    def crash_after_replace() -> None:
        nonlocal persist_count
        persist_count += 1
        if persist_count == 1:
            durable_snapshots.append(receipt.to_dict())
            return
        raise RuntimeError("injected receipt persist crash")

    receipt._persist = crash_after_replace  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="receipt persist crash"):
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    installed = destination_root / ".codex/auth.json"
    assert installed.read_bytes() == source.read_bytes()
    assert receipt.to_dict()["credentials"] == []
    assert receipt.to_dict()["credential_journal"] == durable_snapshots[0][
        "credential_journal"
    ]
    assert durable_snapshots[0]["credentials"] == []
    assert durable_snapshots[0]["credential_journal"] == [
        {
            "principal": "builder",
            "provider": "codex",
            "mode": "0600",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "status": "prepared",
        }
    ]

    recovered = InstallReceipt(durable_snapshots[0])
    metadata = import_credential(
        recovered,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=destination_root,
    )
    assert recovered.to_dict()["credentials"] == [metadata.to_dict()]
    assert recovered.to_dict()["credential_journal"] == []


def test_credential_source_os_error_does_not_disclose_source_path(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "private-source-name" / "auth.json"

    with pytest.raises(CredentialImportError) as exc:
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=tmp_path / "installed",
        )

    assert str(source) not in str(exc.value)
    assert "private-source-name" not in str(exc.value)
    assert str(exc.value) == "credential source is not readable"


def test_credential_cli_stderr_redacts_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    receipt._document["plan"]["accounts"] = [  # type: ignore[index]
        {
            "name": "cortex-builder",
            "home": str(tmp_path / "cortex-builder"),
            "uid": 991,
            "gid": 991,
        }
    ]
    source = tmp_path / "private-source-name" / "auth.json"
    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(
        install_cli.InstallReceipt,
        "load",
        classmethod(lambda _cls, _path: receipt),
    )

    assert install_cli.main(
        [
            "credentials",
            "import",
            "--receipt",
            str(tmp_path / "receipt.json"),
            "--principal",
            "builder",
            "--provider",
            "codex",
            "--source",
            str(source),
        ]
    ) == 1
    error = capsys.readouterr().err
    assert str(source) not in error
    assert "private-source-name" not in error
    assert "credential source is not readable" in error


def test_credential_destination_os_error_uses_fixed_redacted_category(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination_root = tmp_path / "private-destination-name"

    def fail_replace(_source, destination):
        raise OSError(f"cannot replace private path {destination}")

    monkeypatch.setattr(install_core.os, "replace", fail_replace)

    with pytest.raises(CredentialImportError) as exc:
        import_credential(
            receipt,
            principal="builder",
            provider="codex",
            source=source,
            destination_root=destination_root,
        )

    assert str(destination_root) not in str(exc.value)
    assert "private-destination-name" not in str(exc.value)
    assert str(exc.value) == "credential destination write failed"


def test_activation_refuses_missing_required_credential_without_starting_services(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, backend = _applied_receipt(
        required_credentials=[{"principal": "builder", "provider": "codex"}]
    )

    with pytest.raises(ActivationError, match="builder.*codex|codex.*builder"):
        activate_receipt(receipt, backend=backend)
    assert backend.started == []
    assert backend.stopped == []


def test_manager_start_failure_reverse_stops_egress_and_never_starts_monitor(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, backend = _applied_receipt(
        required_credentials=[{"principal": "builder", "provider": "codex"}]
    )
    source = tmp_path / "auth.json"
    source.write_text('{"token":"test-secret"}', encoding="utf-8")
    import_credential(
        receipt,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=tmp_path / "installed",
    )
    backend.fail_start = "cortex-manager.service"

    with pytest.raises(ActivationError, match="cortex-manager.service"):
        activate_receipt(receipt, backend=backend)

    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
    ]
    assert backend.stopped == [
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]
    doc = receipt.to_dict()
    assert doc["activated"] is False
    assert doc["qualified"] is False


def test_activation_surfaces_reverse_stop_failure_and_records_remaining_service(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, backend = _applied_receipt()
    backend.fail_start = "cortex-manager.service"
    backend.fail_stop = "cortex-egress-proxy.service"

    with pytest.raises(ActivationError, match="stop.*cortex-egress-proxy"):
        activate_receipt(receipt, backend=backend)

    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
    ]
    assert backend.stopped == [
        "cortex-manager.service",
        "cortex-egress-proxy.service",
    ]
    document = receipt.to_dict()
    assert document["services_started"] is True
    assert document["running_services"] == ["cortex-egress-proxy.service"]
    assert document["activation_failure"]["compensation_failures"] == [
        "cortex-egress-proxy.service"
    ]


def test_rollback_removes_hash_bound_prepared_credential(
    tmp_path: Path,
) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    home = tmp_path / "cortex-builder"
    destination = home / ".codex/auth.json"
    destination.parent.mkdir(parents=True)
    destination.write_text('{"token":"test-secret"}', encoding="utf-8")
    destination.chmod(0o600)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    receipt._document["plan"]["accounts"] = [  # type: ignore[index]
        {
            "name": "cortex-builder",
            "home": str(home),
            "uid": os.getuid(),
            "gid": os.getgid(),
        }
    ]
    receipt._document["credential_journal"] = [
        {
            "principal": "builder",
            "provider": "codex",
            "mode": "0600",
            "sha256": digest,
            "status": "prepared",
        }
    ]

    report = rollback_receipt(
        receipt, backend=LocalInstallBackend(require_root=False)
    )

    assert report.retained_drift == ()
    assert not destination.exists()
    assert receipt.to_dict()["credential_journal"] == []


def test_live_credential_validation_redacts_destination_path(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt()
    private_home = tmp_path / "private-destination-name"
    receipt._document["plan"]["accounts"] = [  # type: ignore[index]
        {
            "name": "cortex-builder",
            "home": str(private_home),
            "uid": os.getuid(),
            "gid": os.getgid(),
        }
    ]
    receipt._document["credentials"] = [
        {
            "principal": "builder",
            "provider": "codex",
            "mode": "0600",
            "sha256": "0" * 64,
        }
    ]

    failures = LocalInstallBackend(require_root=False).validate_credentials(receipt)

    assert failures == ("builder/codex unavailable",)
    assert str(private_home) not in " ".join(failures)


def test_successful_start_order_is_still_unverified_not_qualified() -> None:
    _plan_doc, receipt, backend = _applied_receipt()

    activate_receipt(receipt, backend=backend)

    assert backend.started == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]
    doc = receipt.to_dict()
    assert doc["activated"] is False
    assert doc["qualified"] is False
    assert doc["services_started"] is True


def test_activation_revalidates_imported_credential_bytes(tmp_path: Path) -> None:
    _plan_doc, receipt, _backend = _applied_receipt(
        required_credentials=[{"principal": "builder", "provider": "codex"}]
    )
    source = tmp_path / "auth.json"
    source.write_text('{"token":"original"}', encoding="utf-8")
    home = tmp_path / "cortex-builder"
    import_credential(
        receipt,
        principal="builder",
        provider="codex",
        source=source,
        destination_root=home,
    )
    installed = home / ".codex/auth.json"
    installed.write_text('{"token":"tampered"}', encoding="utf-8")

    class ValidatingBackend(CredentialBackend):
        def validate_credentials(self, _receipt):
            return ("builder/codex hash mismatch",)

    backend = ValidatingBackend()
    with pytest.raises(ActivationError, match="hash mismatch"):
        activate_receipt(receipt, backend=backend)
    assert backend.started == []
