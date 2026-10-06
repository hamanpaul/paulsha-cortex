"""RC `legacy-adoption` profile: runner, image, driver, schema, validator, redaction (#1122, PR-5)."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION = REPO_ROOT / "qualification"
RUNNER = QUALIFICATION / "run.sh"
DOCKERFILE = QUALIFICATION / "Dockerfile"
SCHEMA = QUALIFICATION / "qualification.schema.json"
VALIDATOR = QUALIFICATION / "validate.py"
DRIVER = QUALIFICATION / "driver.py"
REDACTION = QUALIFICATION / "redaction_scan.py"
CANDIDATE = "a" * 40
WHEEL = "b" * 64
BUNDLE = "c" * 64
INVENTORY_SHA = "d" * 64
PLAN_SHA = "e" * 64


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fixture = _load("cortex_qualification_legacy_fixture", QUALIFICATION / "legacy_fixture.py")
launch_authority_probe = _load(
    "cortex_qualification_launch_authority_probe",
    QUALIFICATION / "launch_authority_probe.py",
)
MANIFEST = fixture.load_manifest()
QUARANTINE_ROOT = MANIFEST["legacy_adoption"]["quarantine_root"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def test_provider_check_mapping_is_shared_across_qualification_paths() -> None:
    assert {
        principal: fixture.provider_check_for_principal(principal)
        for principal in ("manager", "builder", "reviewer-planner")
    } == {
        "manager": "credential",
        "builder": "launcher",
        "reviewer-planner": "launcher",
    }

    def is_shared_helper_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "provider_check_for_principal"
        )

    def is_duplicated_mapping(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.IfExp)
            and isinstance(node.body, ast.Constant)
            and node.body.value == "credential"
            and isinstance(node.orelse, ast.Constant)
            and node.orelse.value == "launcher"
        )

    production_files = (
        "launch_authority_probe.py",
        "legacy_adoption.py",
        "validate.py",
        "driver.py",
    )
    all_files = production_files + (
        "../tests/test_qualification_legacy_harness.py",
        "../tests/test_qualification_legacy_profile.py",
    )
    trees: dict[str, ast.Module] = {}
    for relative_path in all_files:
        path = QUALIFICATION / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[relative_path] = tree
        assert not any(is_duplicated_mapping(node) for node in ast.walk(tree)), relative_path
        assert any(is_shared_helper_call(node) for node in ast.walk(tree)), relative_path

    for filename in production_files:
        tree = trees[filename]
        check_fields = [
            value
            for node in ast.walk(tree)
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "check"
        ]
        assert check_fields, filename
        if filename == "launch_authority_probe.py":
            assert len(check_fields) == 2
            assert all(
                isinstance(value, ast.Name) and value.id == "check"
                for value in check_fields
            )
            assert any(
                isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "check" for target in node.targets)
                and is_shared_helper_call(node.value)
                for node in ast.walk(tree)
            ), filename
        else:
            assert all(is_shared_helper_call(value) for value in check_fields), filename


def test_launch_authority_probe_uses_core_layout_version(monkeypatch: pytest.MonkeyPatch) -> None:
    core = launch_authority_probe.core
    monkeypatch.setattr(core, "LAUNCH_AUTHORITY_LAYOUT_VERSION", 17)

    launch_authority_probe._validate_launch_authority_layout({"launch_layout_version": 17})
    with pytest.raises(ValueError, match="layout v17"):
        launch_authority_probe._validate_launch_authority_layout({"launch_layout_version": 1})


# ---------------------------------------------------------------------------
# a complete, self-consistent legacy-adoption evidence set
# ---------------------------------------------------------------------------


def _quarantine_rows() -> list[dict]:
    rows = []
    for index, path in enumerate(MANIFEST["expected"]["quarantine"]):
        rows.append(
            {
                "path": path,
                "destination": f"{QUARANTINE_ROOT}/{INVENTORY_SHA[:16]}/root{path}",
                "type": "file",
                "dev": 64,
                "ino": 5000 + index,
            }
        )
    return rows


def _legacy_adoption_document() -> dict:
    quarantine = _quarantine_rows()
    samples = [
        {"path": path, "dev": 64, "ino": 9000 + index, "unchanged": True}
        for index, path in enumerate(MANIFEST["expected"]["adopted_samples"])
    ]
    applied = {
        "state": "applied",
        "quarantine": [
            {"path": row["path"], "destination": row["destination"], "moved": True}
            for row in quarantine
        ],
        "samples": samples,
    }
    return {
        "schema_version": 1,
        "profile": "legacy-adoption",
        "status": "passed",
        "candidate": {"candidate_sha": CANDIDATE, "wheel_sha256": WHEEL, "bundle_sha256": BUNDLE},
        "steps": [{"name": name, "status": "passed"} for name in fixture.LEGACY_STEPS],
        "fixture": {
            "sha256": fixture.manifest_sha256(),
            "accounts": {
                name: {"uid": row["uid"], "gid": row["gid"]}
                for name, row in sorted(fixture.accounts(MANIFEST).items())
            },
            "services": {name: "active" for name in fixture.SERVICES},
        },
        "inventory": {
            "path": f"/var/lib/cortex-installer/legacy/{INVENTORY_SHA}.json",
            "inventory_sha256": INVENTORY_SHA,
            "scope_sha256": "1" * 64,
            "host_binding_sha256": "2" * 64,
            "census_stable": True,
            "summary_sha256": "3" * 64,
            "summary_lines": 40,
        },
        "plan": {
            "path": "/run/cortex-install/install-plan.json",
            "host_overlay_path": "/var/lib/cortex-installer/host-overlay.yaml",
            "reported_sha256": PLAN_SHA,
            "observed_sha256": PLAN_SHA,
            "recomputed_sha256": PLAN_SHA,
            "confirmed_sha256": PLAN_SHA,
            "summary": {"adopt": 12},
            "quarantine": quarantine,
        },
        "legacy_services": {
            "stopped_before_apply": {name: "inactive" for name in fixture.SERVICES},
            "restarted": {name: "active" for name in fixture.SERVICES},
            "stopped_before_reapply": {name: "inactive" for name in fixture.SERVICES},
        },
        "apply": deepcopy(applied),
        "rollback": {
            "legacy_restored": True,
            "restore_safe": True,
            "retained_unknown": [],
            "retained_drift": [],
            "systemd_daemon_reload": "completed",
            "restored": [
                {"path": row["path"], "dev": row["dev"], "ino": row["ino"], "restored": True}
                for row in quarantine
            ],
            "need_daemon_reload": {name: "no" for name in fixture.SERVICES},
            "samples": samples,
        },
        "reapply": deepcopy(applied),
        "credentials": [
            {
                "principal": row["principal"],
                "provider": row["provider"],
                "source": f"{QUARANTINE_ROOT}/{INVENTORY_SHA[:16]}/root{row['path']}",
                "mode": "0600",
            }
            for row in sorted(
                MANIFEST["credentials"], key=lambda row: (row["principal"], row["provider"])
            )
        ],
        "activate": {"services_started": True},
        "verify": {"result": "pass", "evidence": "install-verification.json"},
        "launcher_authorities": {
            "status": "passed",
            "providers": [
                {
                    "principal": str(row["principal"]),
                    "provider": str(row["provider"]),
                    "check": fixture.provider_check_for_principal(str(row["principal"])),
                }
                for row in MANIFEST["credentials"]
            ],
        },
    }


def _legacy_services() -> list[dict]:
    identities = fixture.service_identities(MANIFEST)
    return [
        {"name": name, "uid": uid, "gid": gid, "active": True}
        for name, (uid, gid) in sorted(identities.items())
    ]


def _valid_legacy_qualification(tmp_path: Path) -> dict:
    attestation = {"ok": True, "failures": [], "warnings": []}
    installed = {
        "schema_version": 1,
        "result": "pass",
        "candidate": {"wheel_sha256": WHEEL, "bundle_sha256": BUNDLE},
        "attestation": attestation,
        "artifact_hashes": {"units/cortex-manager.service": "1" * 64},
        "service_identities": {"cortex-manager.service": {"user": "cortex-manager"}},
    }
    documents = {
        "install-verification.json": installed,
        "generated-installed-attestation.json": {
            "schema_version": 1,
            "ok": True,
            "attestation": attestation,
            "artifact_hashes": installed["artifact_hashes"],
            "service_identities": installed["service_identities"],
        },
        "install-semantic-checks.json": {"schema_version": 1, "selfcheck": {"ok": True}},
        "legacy-adoption.json": _legacy_adoption_document(),
        "legacy-inventory.json": {
            "schema_version": 1,
            "kind": "paulsha-cortex/trust-root-legacy-inventory",
            "inventory_sha256": INVENTORY_SHA,
        },
        "legacy-rollback.json": {
            "returncode": 0,
            "legacy_restored": True,
            "restore_safe": True,
            "retained_unknown": [],
            "retained_drift": [],
            "systemd_daemon_reload": "completed",
        },
    }
    _write_evidence(tmp_path, documents)
    payload = {
        "schema_version": 2,
        "profile": "legacy-adoption",
        "status": "passed",
        "candidate_sha": CANDIDATE,
        "wheel": {"filename": "paulsha_cortex-0.1.12-py3-none-any.whl", "sha256": WHEEL},
        "bundle": {"sha256": BUNDLE},
        "image": {"digest": "sha256:" + "9" * 64},
        "services": _legacy_services(),
        "providers": [],
        "tests": [
            {"name": name, "status": "passed"}
            for name in (
                *fixture.LEGACY_TESTS,
                "selfcheck",
                "registry-equation",
                "generated-installed-attestation",
                "service-identity-hardening",
            )
        ],
        "artifacts": [],
    }
    _refresh(tmp_path, payload)
    return payload


def _write_evidence(tmp_path: Path, documents: dict) -> None:
    evidence = tmp_path / "evidence"
    for name, value in documents.items():
        _write_json(evidence / name, value)


def _refresh(tmp_path: Path, payload: dict) -> None:
    evidence = tmp_path / "evidence"
    rows = [
        {"path": f"evidence/{path.name}", "sha256": _sha256(path)}
        for path in sorted(evidence.iterdir())
        if path.name != "artifact-inventory.json"
    ]
    _write_json(
        evidence / "artifact-inventory.json",
        {"schema_version": 1, "status": "passed", "artifacts": rows},
    )
    payload["artifacts"] = rows + [
        {
            "path": "evidence/artifact-inventory.json",
            "sha256": _sha256(evidence / "artifact-inventory.json"),
        }
    ]


def _mutate_evidence(tmp_path: Path, payload: dict, name: str, change) -> None:
    path = tmp_path / "evidence" / name
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    _write_json(path, document)
    _refresh(tmp_path, payload)


def _validate(tmp_path: Path, payload: dict, *flags: str) -> subprocess.CompletedProcess[str]:
    qualification = tmp_path / "qualification.json"
    _write_json(qualification, payload)
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--qualification",
            str(qualification),
            "--candidate-sha",
            CANDIDATE,
            "--wheel-sha256",
            WHEEL,
            "--bundle-sha256",
            BUNDLE,
            "--evidence-root",
            str(tmp_path),
            *flags,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# schema and validator
# ---------------------------------------------------------------------------


def test_schema_admits_the_legacy_profile_without_providers() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert set(schema["properties"]["profile"]["enum"]) == {
        "release",
        "deployment-canary",
        "legacy-adoption",
    }
    assert {
        "if": {"properties": {"profile": {"const": "legacy-adoption"}}},
        "then": {"properties": {"providers": {"maxItems": 0}}},
    } in schema["allOf"]


def test_validator_accepts_complete_legacy_adoption_evidence(tmp_path: Path) -> None:
    payload = _valid_legacy_qualification(tmp_path)
    completed = _validate(tmp_path, payload, "--require-legacy-profile")
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_legacy_and_release_profiles_cannot_unlock_each_other(tmp_path: Path) -> None:
    payload = _valid_legacy_qualification(tmp_path)
    for flag in ("--require-release-profile", "--require-canary-profile"):
        completed = _validate(tmp_path, payload, flag)
        assert completed.returncode != 0, flag
        assert "profile" in completed.stderr

    release = deepcopy(payload)
    release["profile"] = "release"
    completed = _validate(tmp_path, release, "--require-legacy-profile")
    assert completed.returncode != 0
    assert "not legacy-adoption" in completed.stderr


def test_legacy_profile_flags_are_mutually_exclusive(tmp_path: Path) -> None:
    payload = _valid_legacy_qualification(tmp_path)
    completed = _validate(
        tmp_path, payload, "--require-legacy-profile", "--require-release-profile"
    )
    assert completed.returncode != 0


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda tmp, payload: payload["services"][1].update(uid=991, gid=991), "identity"),
        (
            lambda tmp, payload: payload.update(
                providers=[
                    {
                        "provider": "codex",
                        "requested_model": "m",
                        "runtime_model": "m",
                        "requested_effort": "e",
                        "runtime_effort": "e",
                        "status": "passed",
                        "quota": "available",
                        "fallback": False,
                    }
                ]
            ),
            "provider",
        ),
        (
            lambda tmp, payload: payload.update(
                tests=[row for row in payload["tests"] if row["name"] != "legacy-adoption-rollback"]
            ),
            "legacy-adoption-rollback",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["rollback"].update(legacy_restored=False),
            ),
            "legacy_restored",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-rollback.json",
                lambda doc: doc.update(restore_safe=False),
            ),
            "restore_safe",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["rollback"]["restored"][0].update(ino=1),
            ),
            "inode",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["steps"].reverse(),
            ),
            "steps",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["plan"]["quarantine"].pop(),
            ),
            "quarantine",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["plan"].update(observed_sha256="0" * 64),
            ),
            "plan SHA",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["fixture"].update(sha256="0" * 64),
            ),
            "fixture",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["candidate"].update(candidate_sha="f" * 40),
            ),
            "candidate",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-inventory.json",
                lambda doc: doc.update(inventory_sha256="0" * 64),
            ),
            "inventory",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["credentials"].pop(),
            ),
            "credential",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["credentials"][0].update(source="/run/auth.json"),
            ),
            "quarantine",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["reapply"]["samples"][0].update(unchanged=False),
            ),
            "sample",
        ),
        (
            lambda tmp, payload: _mutate_evidence(
                tmp, payload, "legacy-adoption.json",
                lambda doc: doc["rollback"]["need_daemon_reload"].update(
                    {"cortex-manager.service": "yes"}
                ),
            ),
            "NeedDaemonReload",
        ),
        (
            lambda tmp, payload: (tmp / "evidence" / "legacy-rollback.json").unlink()
            or _refresh(tmp, payload),
            "legacy-rollback",
        ),
    ],
    ids=[
        "release-service-identity",
        "providers",
        "missing-test",
        "not-restored",
        "not-restore-safe",
        "inode-changed",
        "step-order",
        "quarantine-set",
        "plan-sha",
        "fixture-digest",
        "candidate-binding",
        "inventory-binding",
        "credential-set",
        "credential-source",
        "adopted-sample",
        "stale-units",
        "missing-rollback-evidence",
    ],
)
def test_legacy_validator_fails_closed(tmp_path: Path, mutate, message: str) -> None:
    payload = _valid_legacy_qualification(tmp_path)
    mutate(tmp_path, payload)
    completed = _validate(tmp_path, payload, "--require-legacy-profile")
    assert completed.returncode != 0, completed.stdout
    assert message in completed.stderr, completed.stderr


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def _driver():
    return _load("cortex_qualification_driver_legacy", DRIVER)


def _driver_argv(tmp_path: Path, profile: str, *extra: str) -> list[str]:
    return [
        str(DRIVER),
        "--receipt",
        str(tmp_path / "receipt.json"),
        "--install-evidence",
        str(tmp_path / "legacy" / "install-verification.json"),
        "--candidate-sha",
        CANDIDATE,
        "--wheel-sha256",
        WHEEL,
        "--bundle-sha256",
        BUNDLE,
        "--image-digest",
        "sha256:" + "9" * 64,
        "--wheel-filename",
        "paulsha_cortex-0.1.12-py3-none-any.whl",
        "--profile",
        profile,
        "--output",
        str(tmp_path / "qualification.json"),
        "--evidence-dir",
        str(tmp_path / "evidence"),
        *extra,
    ]


def _driver_inputs(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    _write_json(legacy_dir / "legacy-adoption.json", _legacy_adoption_document())
    _write_json(
        legacy_dir / "legacy-inventory.json",
        {"schema_version": 1, "kind": "paulsha-cortex/trust-root-legacy-inventory",
         "inventory_sha256": INVENTORY_SHA},
    )
    _write_json(
        legacy_dir / "legacy-rollback.json",
        {"returncode": 0, "legacy_restored": True, "restore_safe": True,
         "retained_unknown": [], "retained_drift": [], "systemd_daemon_reload": "completed"},
    )
    _write_json(
        tmp_path / "receipt.json",
        {
            "state": "applied",
            "qualified": True,
            "legacy_adoption": {
                "inventory_sha256": INVENTORY_SHA,
                "apply_inventory_sha256": INVENTORY_SHA,
                "inventory_path": f"/var/lib/cortex-installer/legacy/{INVENTORY_SHA}.json",
                "host_binding_sha256": "2" * 64,
                "quarantine_root": QUARANTINE_ROOT,
            },
        },
    )


def _patch_driver(driver, monkeypatch: pytest.MonkeyPatch, *, quarantined: bool = True) -> None:
    monkeypatch.setattr(
        driver,
        "_installed_checks",
        lambda **_kwargs: [
            {"name": name, "status": "passed"}
            for name in (
                "selfcheck",
                "registry-equation",
                "generated-installed-attestation",
                "service-identity-hardening",
            )
        ],
    )
    monkeypatch.setattr(driver, "_service_rows", _legacy_services)
    rows = {row["destination"]: (row["dev"], row["ino"]) for row in _quarantine_rows()}
    monkeypatch.setattr(
        driver,
        "_path_identity",
        lambda path: rows.get(path) if quarantined else None,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy-adoption profile ran a release/canary-only probe")

    for name in (
        "_permission_attack_matrix",
        "_provider_smokes",
        "_manager_github_probe",
        "_prepare_probe_dispatch",
        "_full_dispatch",
    ):
        monkeypatch.setattr(driver, name, forbidden)


def test_driver_turns_legacy_evidence_into_a_legacy_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()
    _driver_inputs(tmp_path)
    _patch_driver(driver, monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        _driver_argv(tmp_path, "legacy-adoption", "--legacy-evidence", str(tmp_path / "legacy")),
    )

    assert driver.main() == 0

    payload = json.loads((tmp_path / "qualification.json").read_text(encoding="utf-8"))
    assert payload["profile"] == "legacy-adoption"
    assert payload["providers"] == []
    names = [row["name"] for row in payload["tests"]]
    assert set(fixture.LEGACY_TESTS) <= set(names)
    assert "fresh-install" not in names
    evidence = {row["path"]: row["sha256"] for row in payload["artifacts"]}
    for name in fixture.LEGACY_EVIDENCE_FILES:
        copied = tmp_path / "evidence" / name
        assert evidence[f"evidence/{name}"] == _sha256(copied)
        assert json.loads(copied.read_text(encoding="utf-8")) == json.loads(
            (tmp_path / "legacy" / name).read_text(encoding="utf-8")
        )
    assert payload["services"] == _legacy_services()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda tmp: _rewrite(tmp / "legacy/legacy-adoption.json", lambda doc: doc.update(status="failed")), "passed"),
        (lambda tmp: _rewrite(tmp / "receipt.json", lambda doc: doc["legacy_adoption"].update(apply_inventory_sha256="0" * 64)), "receipt"),
        (lambda tmp: _rewrite(tmp / "receipt.json", lambda doc: doc.update(qualified=False)), "qualified"),
        (lambda tmp: _rewrite(tmp / "legacy/legacy-inventory.json", lambda doc: doc.update(inventory_sha256="0" * 64)), "inventory"),
        (lambda tmp: _rewrite(tmp / "legacy/legacy-rollback.json", lambda doc: doc.update(legacy_restored=False)), "rollback"),
    ],
    ids=["harness-failed", "receipt-digest", "not-qualified", "inventory-file", "rollback-file"],
)
def test_driver_rejects_inconsistent_legacy_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, change, message: str
) -> None:
    driver = _driver()
    _driver_inputs(tmp_path)
    change(tmp_path)
    _patch_driver(driver, monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        _driver_argv(tmp_path, "legacy-adoption", "--legacy-evidence", str(tmp_path / "legacy")),
    )
    assert driver.main() == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "qualification.json").exists()


def test_driver_requires_the_live_host_to_keep_legacy_objects_in_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    driver = _driver()
    _driver_inputs(tmp_path)
    _patch_driver(driver, monkeypatch, quarantined=False)
    monkeypatch.setattr(
        sys,
        "argv",
        _driver_argv(tmp_path, "legacy-adoption", "--legacy-evidence", str(tmp_path / "legacy")),
    )
    assert driver.main() == 1
    assert "quarantine" in capsys.readouterr().err


def _rewrite(path: Path, change) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    _write_json(path, document)


def test_driver_binds_legacy_evidence_to_the_legacy_profile_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = _driver()
    for argv in (
        _driver_argv(tmp_path, "legacy-adoption"),
        _driver_argv(tmp_path, "release", "--legacy-evidence", str(tmp_path)),
        _driver_argv(
            tmp_path,
            "legacy-adoption",
            "--legacy-evidence",
            str(tmp_path),
            "--probe-repository",
            "acme/probe",
        ),
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as caught:
            driver.main()
        assert caught.value.code == 2


# ---------------------------------------------------------------------------
# redaction, image and runner
# ---------------------------------------------------------------------------


def test_legacy_redaction_scan_rejects_leaked_fixture_credentials(tmp_path: Path) -> None:
    redaction = _load("cortex_qualification_redaction_legacy", REDACTION)
    (tmp_path / "qualification.json").write_text("{}", encoding="utf-8")
    redaction.scan(tmp_path, profile="legacy-adoption")

    secret = fixture.credential_secrets(MANIFEST)[-1]
    (tmp_path / "evidence.json").write_bytes(b'{"leak": "' + secret + b'"}')
    with pytest.raises(ValueError, match="credential-like"):
        redaction.scan(tmp_path, profile="legacy-adoption")


def test_reference_image_ships_the_legacy_fixture_and_harness() -> None:
    raw = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY legacy_fixture.py /usr/local/libexec/qualification/legacy_fixture.py" in raw
    assert "COPY legacy_fixture.json /usr/local/libexec/qualification/legacy_fixture.json" in raw
    assert "COPY legacy_adoption.py /usr/local/libexec/cortex-legacy-adoption" in raw
    assert "/usr/local/libexec/cortex-legacy-adoption" in raw.split("RUN chmod", 1)[1]


def _runner_env(tmp_path: Path) -> dict[str, str]:
    """A PATH whose docker only records that it was reached."""

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    marker = tmp_path / "docker-reached"
    docker = fake_bin / "docker"
    docker.write_text(f"#!/bin/sh\ntouch {marker}\nexit 97\n", encoding="utf-8")
    docker.chmod(0o755)
    return {**os.environ, "PATH": f"{fake_bin}:/usr/bin:/bin"}


def _run_runner(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return subprocess.run(
        [
            "bash",
            str(RUNNER),
            "--artifacts",
            str(artifacts),
            "--output",
            str(tmp_path / "out"),
            *arguments,
        ],
        env=_runner_env(tmp_path),
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_runner_accepts_the_legacy_profile_and_rejects_unknown_profiles(tmp_path: Path) -> None:
    unknown = _run_runner(tmp_path, "--profile", "legacy", "--candidate-sha", "x")
    assert unknown.returncode == 2
    assert "legacy-adoption" in unknown.stderr  # the usage names the profile

    # A valid profile passes the profile gate and fails only at the next input
    # check, before any container tooling is reached.
    legacy_run = _run_runner(tmp_path, "--profile", "legacy-adoption", "--candidate-sha", "x")
    assert legacy_run.returncode == 1
    assert "candidate SHA" in legacy_run.stderr
    assert not (tmp_path / "docker-reached").exists()


def test_runner_legacy_branch_keeps_state_on_the_root_filesystem_and_offline() -> None:
    raw = RUNNER.read_text(encoding="utf-8")
    assert re.search(
        r'if \[\[ "\$profile" == release \|\| "\$profile" == legacy-adoption \]\]; then\n'
        r"(?:\s*#[^\n]*\n)*\s*network_args=\(--network none\)",
        raw,
    )
    # legacy-quarantine renames objects between /etc, /opt and /var/lib: the
    # legacy profile must not mount /var/lib/cortex as a separate volume.
    assert 'state_mount_args=(--mount "type=volume,source=$volume_name,target=/var/lib/cortex")' in raw
    assert re.search(r'legacy-adoption \]\]; then\n(?:\s*#[^\n]*\n)*\s*state_mount_args=\(\)', raw)
    assert '"${state_mount_args[@]}"' in raw

    legacy = raw[raw.index("run_legacy_adoption_profile() {"):]
    legacy = legacy[: legacy.index("\n}\n")]
    assert "/usr/local/libexec/cortex-legacy-adoption" in legacy
    assert "--profile legacy-adoption" in legacy
    assert "--legacy-evidence" in legacy
    assert legacy.count("--require-legacy-profile") == 2
    for forbidden in ("CORTEX_RC_", "--probe-", "import_secret", "provider"):
        assert forbidden not in legacy
    # The branch runs right after the candidate wheel install and ends the run.
    install = raw.index("pip install --break-system-packages")
    dispatch = raw.index('if [[ "$profile" == legacy-adoption ]]; then\n    run_legacy_adoption_profile')
    first_plan = raw.index("cortex install trust-root plan")
    assert install < dispatch < first_plan
    assert "run_legacy_adoption_profile\n    exit 0" in raw
