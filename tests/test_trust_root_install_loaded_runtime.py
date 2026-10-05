"""#1263：loaded↔installed 比對由 paulsha_cortex 提供，driver 只沿用。"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import loaded_runtime
from paulsha_cortex.trust_root.install.core import InstallError


def _driver():
    path = Path(__file__).parents[1] / "qualification" / "driver.py"
    spec = importlib.util.spec_from_file_location("qualification_driver_loaded_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(*, receipt_id: str = "prior", wheel: str = "b" * 64, commit: str = "c" * 40):
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


EXPECTED = {"receipt_id": "prior", "wheel_sha256": "b" * 64, "candidate_commit": "c" * 40}


def test_matching_runtime_reports_no_mismatch() -> None:
    assert loaded_runtime.loaded_runtime_mismatch(_payload(), EXPECTED) == ""


def test_receipt_and_wheel_mismatch_are_named() -> None:
    mismatch = loaded_runtime.loaded_runtime_mismatch(
        _payload(receipt_id="other", wheel="d" * 64), EXPECTED
    )

    assert "manager_receipt=mismatch" in mismatch
    assert "manager_loaded_wheel=mismatch" in mismatch


def test_missing_trust_root_is_unknown() -> None:
    payload = _payload()
    del payload["service"]["loaded_runtime"]["manager"]["trust_root"]

    assert "manager=unknown" in loaded_runtime.loaded_runtime_mismatch(payload, EXPECTED)


def test_unavailable_payload_is_unknown() -> None:
    assert loaded_runtime.loaded_runtime_mismatch(None, EXPECTED) == "loaded_runtime=unknown"


def test_expected_runtime_comes_from_the_receipt_plan() -> None:
    receipt = {
        "receipt_id": "r1",
        "plan": {
            "candidate": {"wheel_sha256": "b" * 64},
            "repo_identity": {"commit": "c" * 40},
        },
    }

    assert loaded_runtime.runtime_expected(receipt) == {
        "receipt_id": "r1",
        "wheel_sha256": "b" * 64,
        "candidate_commit": "c" * 40,
    }


def test_driver_reuses_the_packaged_comparison() -> None:
    driver = _driver()

    assert driver._rollback_loaded_runtime_mismatch is loaded_runtime.loaded_runtime_mismatch
    assert driver._rollback_runtime_expected is loaded_runtime.runtime_expected


def test_installed_runtime_env_reads_only_psc_values(tmp_path: Path) -> None:
    deploy = tmp_path / "opt/cortex"
    (deploy / "etc").mkdir(parents=True)
    env_file = deploy / "etc/cortex-manager.env"
    env_file.write_text(
        'PSC_COORDINATOR_ROOT="/srv/state/coordinator"\nOTHER="ignored"\n',
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    env = loaded_runtime.installed_runtime_env(
        deploy, tmp_path / "var/lib/cortex", owner_uid=os.getuid()
    )

    assert env["PSC_COORDINATOR_ROOT"] == "/srv/state/coordinator"
    assert env["PSC_CONTROL_ROOT"] == str(tmp_path / "var/lib/cortex/control")
    assert "OTHER" not in env
    assert env["PATH"].startswith(f"{deploy}/venv/bin:")


def test_installed_runtime_env_refuses_a_writable_file(tmp_path: Path) -> None:
    deploy = tmp_path / "opt/cortex"
    (deploy / "etc").mkdir(parents=True)
    env_file = deploy / "etc/cortex-manager.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o666)

    with pytest.raises(InstallError, match="not root-controlled"):
        loaded_runtime.installed_runtime_env(
            deploy, tmp_path / "var/lib/cortex", owner_uid=os.getuid()
        )


def test_driver_diagnostic_token_is_the_packaged_function() -> None:
    driver = _driver()

    assert driver._diagnostic_token is loaded_runtime.diagnostic_token


def test_driver_installed_runtime_env_wraps_install_error_as_qualification_failure(
    monkeypatch,
) -> None:
    driver = _driver()

    def _raise(*_args, **_kwargs):
        raise InstallError("installed Manager environment is not root-controlled")

    monkeypatch.setattr(driver.loaded_runtime, "installed_runtime_env", _raise)

    with pytest.raises(driver.QualificationFailure, match="not root-controlled"):
        driver._installed_runtime_env()
