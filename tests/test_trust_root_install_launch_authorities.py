"""RED contract for installer launch authorities (#1289)."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator.spool_slot import canonical_codex_controls
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install.core import new_install_receipt
from test_trust_root_install_legacy_adoption import LegacyCase
from test_trust_root_install_legacy_inventory import _plan_for


@pytest.fixture(params=("first-install", "legacy-adoption"), ids=("fresh", "adoption"))
def installer_plan(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    if request.param == "first-install":
        plan = _plan_for(tmp_path, None)
    else:
        plan = LegacyCase(tmp_path).plan

    # This test executes the root-owned CLI path as the current test user.
    # Keep the planned identity intact apart from the uid/gid needed by the
    # rootless credential-import seam.
    for account in plan["accounts"]:
        if account["name"] in {"cortex-builder", "cortex-reviewer-planner"}:
            account["uid"] = os.getuid()
            account["gid"] = os.getgid()
    return plan


def _materialize_planned_control_assets(plan: dict) -> Path:
    """Apply only controls assets declared by the install plan, rootlessly."""
    root = Path(plan["roots"]["state"]) / "config/codex-controls"
    for step in plan["apply_order"]:
        path_value = step.get("path")
        if not isinstance(path_value, str):
            continue
        path = Path(path_value)
        if path != root and not path.is_relative_to(root):
            continue
        if step.get("kind") != "asset":
            continue
        asset_type = step.get("asset_type")
        if asset_type == "directory":
            path.mkdir(parents=True, exist_ok=True)
        elif asset_type == "file":
            content = step.get("content")
            assert isinstance(content, str), f"planned control file lacks content: {path}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            path.chmod(int(str(step["mode"]), 8))
        else:
            pytest.fail(f"unsupported planned controls asset: {path} ({asset_type})")
    return root


@pytest.mark.parametrize("principal", ("builder", "reviewer"))
def test_first_install_and_legacy_adoption_provision_canonical_codex_controls(
    installer_plan: dict, principal: str
) -> None:
    root = _materialize_planned_control_assets(installer_plan)

    assert canonical_codex_controls(
        principal,
        manager_env={"PSC_CODEX_CONTROL_ROOT": str(root)},
    ) == root / principal


def _applied_receipt(plan: dict):
    receipt = new_install_receipt(plan)
    receipt._document["state"] = "applied"
    return receipt


def _manager_environment_file(plan: dict, tmp_path: Path) -> Path:
    step = next(
        step
        for step in plan["apply_order"]
        if step.get("kind") == "asset"
        and step.get("path", "").endswith("/cortex-manager.env")
        and isinstance(step.get("content"), str)
    )
    # Model the applied EnvironmentFile in a writable test-owned root. The
    # legacy fixture deliberately seeds a protected old EnvironmentFile that
    # adoption would quarantine before creating the candidate's copy.
    path = tmp_path / "applied-manager-env" / "cortex-manager.env"
    step["path"] = str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(step["content"], encoding="utf-8")
    return path


def _run_credential_import(
    monkeypatch: pytest.MonkeyPatch,
    plan: dict,
    *,
    source: Path,
    principal: str,
    provider: str,
) -> None:
    receipt = _applied_receipt(plan)

    @contextmanager
    def opened_receipt(_path: Path, *, maintenance_token: str | None):
        del maintenance_token
        yield receipt, plan

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "_locked_receipt", opened_receipt)
    install_cli._credential_command(
        SimpleNamespace(
            receipt=Path("unused-receipt.json"),
            maintenance_token=None,
            principal=principal,
            provider=provider,
            source=source,
        )
    )


def _simulate_adoption_quarantining_prior_codex_auth(plan: dict) -> None:
    builder = next(
        account for account in plan["accounts"] if account["name"] == "cortex-builder"
    )
    prior_auth = Path(builder["home"]) / ".codex/auth.json"
    if prior_auth.is_symlink() or prior_auth.is_file():
        prior_auth.unlink()
    prior_auth.parent.mkdir(parents=True, exist_ok=True)
    prior_auth.parent.chmod(0o700)


def test_first_install_and_legacy_adoption_import_codex_into_canonical_authority(
    installer_plan: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manager_environment_file(installer_plan, tmp_path)
    _simulate_adoption_quarantining_prior_codex_auth(installer_plan)
    source = tmp_path / "auth.json"
    source.write_text(json.dumps({"OPENAI_API_KEY": "test-token"}), encoding="utf-8")

    _run_credential_import(
        monkeypatch,
        installer_plan,
        source=source,
        principal="builder",
        provider="codex",
    )

    expected = (
        Path(installer_plan["roots"]["state"])
        / "config/codex-credentials/builder/auth.json"
    )
    assert expected.is_file(), "Codex import must populate the launcher's canonical authority"


def test_imported_copilot_credential_is_bound_into_manager_environment(
    installer_plan: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager_env = _manager_environment_file(installer_plan, tmp_path)
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps(
            {
                "loggedInUsers": [{"host": "https://github.com", "login": "fixture"}],
                "lastLoggedInUser": {"host": "https://github.com", "login": "fixture"},
                "copilotTokens": {"https://github.com:fixture": "test-token"},
            }
        ),
        encoding="utf-8",
    )

    _run_credential_import(
        monkeypatch,
        installer_plan,
        source=source,
        principal="reviewer-planner",
        provider="copilot",
    )

    assert "PSC_COPILOT_OAUTH_CONFIG=" in manager_env.read_text(encoding="utf-8")
