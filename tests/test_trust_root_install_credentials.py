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
    activate_receipt,
    apply_plan,
    import_credential,
    new_install_receipt,
    plan_sha256,
)


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
    assert backend.stopped == ["cortex-egress-proxy.service"]
    doc = receipt.to_dict()
    assert doc["activated"] is False
    assert doc["qualified"] is False


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
