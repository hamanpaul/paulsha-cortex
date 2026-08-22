from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION = REPO_ROOT / "qualification"
DOCKERFILE = QUALIFICATION / "Dockerfile"
RUNNER = QUALIFICATION / "run.sh"
SCHEMA = QUALIFICATION / "qualification.schema.json"
VALIDATOR = QUALIFICATION / "validate.py"
DRIVER = QUALIFICATION / "driver.py"


def _load_driver_module():
    spec = importlib.util.spec_from_file_location("cortex_qualification_driver", DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _required_text(path: Path) -> str:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} must exist"
    return path.read_text(encoding="utf-8")


def _valid_qualification() -> dict:
    return {
        "schema_version": 1,
        "status": "passed",
        "candidate_sha": "a" * 40,
        "wheel": {
            "filename": "paulsha_cortex-0.2.0-py3-none-any.whl",
            "sha256": "b" * 64,
        },
        "bundle": {"sha256": "c" * 64},
        "image": {"digest": "sha256:" + "d" * 64},
        "services": [
            {
                "name": "cortex-manager.service",
                "uid": 991,
                "gid": 991,
                "active": True,
            }
        ],
        "providers": [
            {
                "provider": "agy",
                "requested_model": "gemini-3.7-flash",
                "runtime_model": "gemini-3.7-flash",
                "requested_effort": "high",
                "runtime_effort": "high",
                "status": "passed",
                "quota": "available",
                "fallback": False,
            },
            {
                "provider": "copilot",
                "requested_model": "gpt-5.4",
                "runtime_model": "gpt-5.4",
                "requested_effort": "xhigh",
                "runtime_effort": "xhigh",
                "status": "passed",
                "quota": "available",
                "fallback": False,
            },
            {
                "provider": "codex",
                "requested_model": "gpt-5",
                "runtime_model": "gpt-5",
                "requested_effort": "normal",
                "runtime_effort": "normal",
                "status": "passed",
                "quota": "available",
                "fallback": False,
            },
        ],
        "tests": [
            {"name": "fresh-install", "status": "passed"},
            {"name": "full-dispatch-closeout", "status": "passed"},
        ],
        "artifacts": [
            {"path": "evidence/closeout.json", "sha256": "e" * 64},
        ],
    }


def _run_validator(tmp_path: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    assert VALIDATOR.is_file(), "qualification/validate.py must exist"
    evidence = tmp_path / "qualification.json"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--qualification",
            str(evidence),
            "--candidate-sha",
            "a" * 40,
            "--wheel-sha256",
            "b" * 64,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _valid_full_qualification(tmp_path: Path) -> dict:
    payload = _valid_qualification()
    payload["services"] = [
        {"name": "cortex-egress-proxy.service", "uid": 995, "gid": 995, "active": True},
        {"name": "cortex-manager.service", "uid": 991, "gid": 991, "active": True},
        {"name": "cortex-monitor.service", "uid": 991, "gid": 991, "active": True},
    ]
    payload["tests"] = [
        {"name": name, "status": "passed"}
        for name in (
            "fresh-install",
            "idempotent-apply",
            "drift-detection",
            "rollback",
            "reinstall",
            "selfcheck",
            "registry-equation",
            "generated-installed-attestation",
            "service-identity-hardening",
            "capability-attack-matrix",
            "durable-state-attack-matrix",
            "enforcement-plane-attack-matrix",
            "process-attack-matrix",
            "gate-attack-matrix",
            "negative-controls",
            "provider-capability-smoke",
            "full-dispatch-closeout",
            "manager-github-dry-run-push",
        )
    ]
    attestation = {"ok": True, "failures": [], "warnings": []}
    installed = {
        "schema_version": 1,
        "result": "pass",
        "candidate": {"wheel_sha256": "b" * 64, "bundle_sha256": "c" * 64},
        "attestation": attestation,
        "artifact_hashes": {"units/cortex-manager.service": "1" * 64},
        "service_identities": {"cortex-manager.service": {"user": "cortex-manager"}},
    }
    generated = {
        "schema_version": 1,
        "ok": True,
        "attestation": attestation,
        "artifact_hashes": installed["artifact_hashes"],
        "service_identities": installed["service_identities"],
    }
    cases = []
    required = {
        "capability": ("T1.1", "T1.2", "T1.3", "T1.4"),
        "enforcement-plane": tuple(f"T3.{index}" for index in range(1, 11)),
        "process": ("T4.1", "T4.2", "T4.3", "T4.4"),
        "gate": tuple(f"T5.{index}" for index in range(1, 11)),
    }
    for family, case_ids in required.items():
        for case_id in case_ids:
            cases.append(
                {
                    "family": family,
                    "case": f"{case_id}-probe",
                    "principal": "probe",
                    "status": "passed",
                    "returncode": 1,
                }
            )
    for operation in (
        "modify",
        "truncate",
        "delete",
        "replace",
        "symlink-swap",
        "rollback",
    ):
        for principal in ("cortex-builder", "cortex-reviewer-planner"):
            cases.append(
                {
                    "family": "durable-state",
                    "case": f"jobs-registry:{operation}",
                    "principal": principal,
                    "status": "passed",
                    "returncode": 13,
                }
            )
    attack = {
        "schema_version": 1,
        "status": "passed",
        "families": [
            "capability",
            "durable-state",
            "enforcement-plane",
            "process",
            "gate",
        ],
        "cases": cases,
        "negative_controls": [
            {
                "family": family,
                "case": f"{family}-control",
                "principal": "trusted",
                "status": "passed",
                "returncode": 0,
            }
            for family in (
                "capability",
                "durable-state",
                "enforcement-plane",
                "process",
                "gate",
            )
        ],
        "covered_assets": 1,
        "registry_asset_ids": ["jobs-registry"],
    }
    provider_evidence = {
        "schema_version": 1,
        "providers": {
            row["provider"]: {
                "preflight": {
                    "returncode": 0,
                    "status": "ready",
                    "authenticated": True,
                    "quota": row["quota"],
                    "fallback": row["fallback"],
                    "skipped": False,
                },
                "returncode": 0,
                "models": [row["runtime_model"]],
                "efforts": [row["runtime_effort"]],
                "native_metadata": True,
                "response_token": True,
            }
            for row in payload["providers"]
        },
    }
    dispatch = {
        "schema_version": 1,
        "status": "passed",
        "repository": "acme/probe",
        "work_id": "qualification",
        "issue": 1,
        "terminal": {"state": "done"},
        "required_markers": [
            "candidate",
            "bundle",
            "verdict",
            "ledger",
            "evidence",
            "completion",
        ],
        "artifacts": [{"path": "coordinator/jobs.json", "sha256": "2" * 64}],
    }
    github = {
        "schema_version": 1,
        "status": "passed",
        "repository": "acme/probe",
        "authenticated": True,
        "dry_run": True,
        "remote_refs_unchanged": True,
        "before_sha256": "3" * 64,
        "after_sha256": "3" * 64,
    }
    evidence = tmp_path / "evidence"
    documents = {
        "install-verification.json": installed,
        "generated-installed-attestation.json": generated,
        "attack-matrix.json": attack,
        "provider-capabilities.json": provider_evidence,
        "dispatch-closeout.json": dispatch,
        "manager-github-auth.json": github,
    }
    for name, value in documents.items():
        _write_json(evidence / name, value)
    inventory_rows = [
        {"path": f"evidence/{name}", "sha256": _sha256(evidence / name)}
        for name in sorted(documents)
    ]
    _write_json(
        evidence / "artifact-inventory.json",
        {"schema_version": 1, "status": "passed", "artifacts": inventory_rows},
    )
    payload["artifacts"] = inventory_rows + [
        {
            "path": "evidence/artifact-inventory.json",
            "sha256": _sha256(evidence / "artifact-inventory.json"),
        }
    ]
    return payload


def _refresh_full_hashes(tmp_path: Path, payload: dict) -> None:
    evidence = tmp_path / "evidence"
    inventory_path = evidence / "artifact-inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    for row in inventory["artifacts"]:
        row["sha256"] = _sha256(tmp_path / row["path"])
    _write_json(inventory_path, inventory)
    payload["artifacts"] = [
        {"path": row["path"], "sha256": _sha256(tmp_path / row["path"])}
        for row in payload["artifacts"]
    ]


def _run_full_validator(
    tmp_path: Path, payload: dict
) -> subprocess.CompletedProcess[str]:
    qualification = tmp_path / "qualification.json"
    _write_json(qualification, payload)
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--qualification",
            str(qualification),
            "--candidate-sha",
            "a" * 40,
            "--wheel-sha256",
            "b" * 64,
            "--bundle-sha256",
            "c" * 64,
            "--evidence-root",
            str(tmp_path),
            "--require-full-suite",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_reference_image_is_ubuntu_2404_systemd_pid1_with_os_dependencies() -> None:
    raw = _required_text(DOCKERFILE)
    lowered = raw.lower()

    assert re.search(r"(?m)^\s*from\s+ubuntu:24\.04(?:\s|$)", lowered)
    for package in (
        "systemd",
        "acl",
        "sudo",
        "git",
        "gh",
        "bubblewrap",
        "socat",
        "nodejs",
    ):
        assert re.search(
            rf"\b{re.escape(package)}\b", lowered
        ), f"Dockerfile must install {package}"
    assert re.search(
        r"\b(?:polkitd|policykit-1)\b", lowered
    ), "Dockerfile must install polkit"
    assert re.search(
        r"(?im)^\s*(?:entrypoint|cmd)\s+\[\s*[\"'](?:/sbin/init|/lib/systemd/systemd)[\"']",
        raw,
    ), "systemd must be container PID 1"

    assert not re.search(
        r"(?im)^\s*(?:copy|add)\s+\.\s", raw
    ), "the image must not copy the checkout as runtime code"
    assert "driver.py" in raw and "cortex-release-qualification" in raw


def test_runner_uses_exact_artifacts_and_never_an_editable_checkout() -> None:
    raw = _required_text(RUNNER)
    lowered = raw.lower()

    assert "docker build" in lowered
    assert "docker run" in lowered
    assert "/artifacts" in raw
    assert re.search(
        r"(?:/artifacts[^\n]*\b(?:ro|readonly)\b|\breadonly\b[^\n]*/artifacts)", raw
    ), "candidate artifacts must be mounted read-only"
    assert re.search(
        r"\.whl\b|wheel", lowered
    ), "the candidate wheel must be selected explicitly"
    assert (
        "bundle" in lowered
    ), "the exact candidate bundle must be passed to qualification"
    assert (
        "sha256sum" in lowered
    ), "candidate artifacts must be hash checked before installation"
    assert "pip install" in lowered, "qualification must install the candidate wheel"

    forbidden = {
        "editable install": r"(?:pip\s+install[^\n]*(?:\s-e\s|--editable)|uv\s+pip[^\n]*--editable)",
        "host docker socket": r"(?:/var/run|/run)/docker\.sock",
        "PYTHONPATH injection": r"(?:--env|-e)\s+(?:PYTHONPATH|[\"']PYTHONPATH)",
        "host HOME mount": r"(?:--volume|-v|\bsrc=)\s*[\"']?\$\{?HOME\}?",
        "checkout PWD mount": r"(?:--volume|-v|\bsrc=)\s*[\"']?(?:\$\{?PWD\}?|\$\(pwd\))",
        "checkout workspace mount": (
            r"(?:--volume|-v|\bsrc=)\s*[\"']?\$\{?"
            r"(?:GITHUB_WORKSPACE|REPO_ROOT|CHECKOUT_DIR|SOURCE_DIR)\}?"
        ),
    }
    for label, pattern in forbidden.items():
        assert not re.search(pattern, raw, re.IGNORECASE), f"run.sh must forbid {label}"


def test_runner_keeps_preinstall_control_files_outside_managed_state() -> None:
    raw = _required_text(RUNNER)
    dockerfile = _required_text(DOCKERFILE)

    assert "plan_path=/run/cortex-install/install-plan.json" in raw
    assert "receipt_path=/run/cortex-install/install-receipt.json" in raw
    assert "install -d -o root -g root -m 0700 /run/cortex-install" in raw
    assert "qualification_root=/run/cortex-qualification" in raw
    assert "/var/lib/cortex/qualification" not in raw
    assert "/var/lib/cortex/qualification" not in dockerfile
    assert "plan_path=/var/lib/cortex" not in raw
    assert "receipt_path=/var/lib/cortex" not in raw


def test_runner_declares_disposable_systemd_container_boundaries() -> None:
    raw = _required_text(RUNNER)

    assert "--privileged" in raw
    assert re.search(r"--cgroupns(?:=|\s+)host", raw)
    assert "/sys/fs/cgroup" in raw
    assert re.search(r"/sys/fs/cgroup[^\n]*(?:rw|readwrite)", raw)
    assert re.search(r"--tmpfs(?:=|\s+)[\"']?/run(?::|[\"'\s])", raw)
    assert re.search(r"--tmpfs(?:=|\s+)[\"']?/run/lock(?::|[\"'\s])", raw)
    assert re.search(
        r"(?:--mount\s+[^\n]*type=volume|--volume\s+[^\n]*:/var/lib/cortex)", raw
    ), "durable test data must use an independent Docker volume"

    bind_targets = re.findall(
        r"(?:target|dst|destination)=([/][^,\s\"']+)",
        raw,
        re.IGNORECASE,
    )
    volume_targets = re.findall(
        r"(?:--volume|-v)(?:=|\s+)[^\n]*?:([/][^,:\s\"']+)",
        raw,
        re.IGNORECASE,
    )
    for target in (*bind_targets, *volume_targets):
        assert (
            target == "/artifacts"
            or target == "/sys/fs/cgroup"
            or target.startswith("/var/lib/cortex")
        ), f"unexpected host bind/volume target: {target}"

    mode = os.stat(RUNNER).st_mode
    assert mode & 0o100, "qualification/run.sh must be executable"


def test_qualification_schema_binds_release_evidence_and_runtime_identity() -> None:
    raw = _required_text(SCHEMA)
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    assert payload.get("type") == "object"

    required = set(payload.get("required", []))
    assert {
        "schema_version",
        "status",
        "candidate_sha",
        "wheel",
        "bundle",
        "image",
        "services",
        "providers",
        "tests",
        "artifacts",
    } <= required

    normalized = json.dumps(payload, sort_keys=True).lower()
    for field in (
        "sha256",
        "requested_model",
        "runtime_model",
        "requested_effort",
        "runtime_effort",
        "quota",
        "fallback",
        "uid",
        "gid",
    ):
        assert field in normalized, f"qualification schema must cover {field}"
    assert "^[0-9a-f]{40}$" in normalized
    assert "^[0-9a-f]{64}$" in normalized


def test_qualification_validator_accepts_only_matching_passed_evidence(
    tmp_path: Path,
) -> None:
    completed = _run_validator(tmp_path, _valid_qualification())
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _mutate_overall_skip(payload: dict) -> None:
    payload["status"] = "skipped"


def _mutate_test_skip(payload: dict) -> None:
    payload["tests"][0]["status"] = "skipped"


def _mutate_provider_skip(payload: dict) -> None:
    payload["providers"][0]["status"] = "skipped"


def _mutate_quota(payload: dict) -> None:
    payload["providers"][2]["quota"] = "exhausted"


def _mutate_fallback(payload: dict) -> None:
    payload["providers"][1]["fallback"] = True


def _mutate_model(payload: dict) -> None:
    payload["providers"][1]["runtime_model"] = "gpt-5-mini"


def _mutate_effort(payload: dict) -> None:
    payload["providers"][0]["runtime_effort"] = "medium"


@pytest.mark.parametrize(
    "mutator",
    [
        _mutate_overall_skip,
        _mutate_test_skip,
        _mutate_provider_skip,
        _mutate_quota,
        _mutate_fallback,
        _mutate_model,
        _mutate_effort,
    ],
    ids=[
        "overall-skip",
        "test-skip",
        "provider-skip",
        "quota-rejected",
        "fallback",
        "model-mismatch",
        "effort-mismatch",
    ],
)
def test_qualification_validator_fails_closed(tmp_path: Path, mutator) -> None:
    payload = copy.deepcopy(_valid_qualification())
    mutator(payload)
    completed = _run_validator(tmp_path, payload)
    assert completed.returncode != 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("--candidate-sha", "f" * 40),
        ("--wheel-sha256", "f" * 64),
    ],
)
def test_qualification_validator_rejects_release_binding_mismatch(
    tmp_path: Path, argument: str, value: str
) -> None:
    assert VALIDATOR.is_file(), "qualification/validate.py must exist"
    evidence = tmp_path / "qualification.json"
    evidence.write_text(json.dumps(_valid_qualification()), encoding="utf-8")
    argv = [
        sys.executable,
        str(VALIDATOR),
        "--qualification",
        str(evidence),
        "--candidate-sha",
        "a" * 40,
        "--wheel-sha256",
        "b" * 64,
    ]
    argv[argv.index(argument) + 1] = value
    completed = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0, completed.stdout + completed.stderr


def test_full_suite_validator_checks_evidence_semantics(tmp_path: Path) -> None:
    payload = _valid_full_qualification(tmp_path)
    completed = _run_full_validator(tmp_path, payload)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-gate-case",
        "missing-family-control",
        "provider-self-attestation",
        "nonterminal-dispatch",
        "changed-remote-refs",
    ],
)
def test_full_suite_validator_rejects_self_consistent_forged_artifacts(
    tmp_path: Path, mutation: str
) -> None:
    payload = _valid_full_qualification(tmp_path)
    evidence = tmp_path / "evidence"
    if mutation in {"missing-gate-case", "missing-family-control"}:
        path = evidence / "attack-matrix.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "missing-gate-case":
            document["cases"] = [
                row for row in document["cases"] if not row["case"].startswith("T5.10")
            ]
        else:
            document["negative_controls"] = [
                row for row in document["negative_controls"] if row["family"] != "gate"
            ]
    elif mutation == "provider-self-attestation":
        path = evidence / "provider-capabilities.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["providers"]["codex"]["native_metadata"] = False
    elif mutation == "nonterminal-dispatch":
        path = evidence / "dispatch-closeout.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["terminal"] = {"state": "ongoing"}
    else:
        path = evidence / "manager-github-auth.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["after_sha256"] = "4" * 64
    _write_json(path, document)
    _refresh_full_hashes(tmp_path, payload)

    completed = _run_full_validator(tmp_path, payload)
    assert completed.returncode != 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("mutation", ["mixed-model", "quota-denied", "fallback"])
def test_full_suite_validator_binds_native_provider_preflight_and_identity(
    tmp_path: Path, mutation: str
) -> None:
    payload = _valid_full_qualification(tmp_path)
    path = tmp_path / "evidence" / "provider-capabilities.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    provider = document["providers"]["codex"]
    if mutation == "mixed-model":
        provider["models"].append("fallback-model")
    elif mutation == "quota-denied":
        provider["preflight"]["quota"] = "exhausted"
    else:
        provider["preflight"]["fallback"] = True
    _write_json(path, document)
    _refresh_full_hashes(tmp_path, payload)

    completed = _run_full_validator(tmp_path, payload)
    assert completed.returncode != 0, completed.stdout + completed.stderr


def test_qualification_driver_and_runner_are_fail_closed_on_live_inputs() -> None:
    driver = _required_text(DRIVER)
    runner = _required_text(RUNNER)

    for case in ("T1.1", "T1.2", "T1.3", "T1.4", "T4.1", "T4.2", "T4.3", "T4.4"):
        assert case in driver
    for index in range(1, 11):
        assert f"T3.{index}" in driver
        assert f"T5.{index}" in driver
    assert "registry_asset_ids" in driver
    assert "native_metadata" in driver and "QUALIFICATION_OK" in driver
    assert "--require-full-suite" in runner
    for variable in (
        "CORTEX_RC_CODEX_AUTH",
        "CORTEX_RC_AGY_AUTH",
        "CORTEX_RC_COPILOT_AUTH",
        "CORTEX_RC_MANAGER_GITHUB_AUTH",
        "CORTEX_RC_PROBE_REPOSITORY",
        "CORTEX_RC_PROBE_WORK_ID",
        "CORTEX_RC_PROBE_ISSUE",
    ):
        assert variable in runner


def test_account_runtime_env_uses_installed_psc_roots_without_manager_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver_module()
    monkeypatch.setattr(
        driver,
        "_account_env",
        lambda account: {
            "HOME": f"/home/{account}",
            "PATH": "/opt/cortex/toolchain/bin:/usr/bin:/bin",
            "CI": "true",
        },
    )
    monkeypatch.setattr(
        driver,
        "_installed_runtime_env",
        lambda: {
            "HOME": "/root",
            "PATH": "/opt/cortex/venv/bin:/usr/bin:/bin",
            "PSC_CONTROL_ROOT": "/var/lib/cortex/control",
            "PSC_COORDINATOR_ROOT": "/var/lib/cortex/coordinator",
            "UNRELATED_MANAGER_VALUE": "must-not-leak",
        },
    )

    env = driver._account_runtime_env("cortex-builder")

    assert env["HOME"] == "/home/cortex-builder"
    assert env["PATH"] == "/opt/cortex/toolchain/bin:/usr/bin:/bin"
    assert env["PSC_CONTROL_ROOT"] == "/var/lib/cortex/control"
    assert env["PSC_COORDINATOR_ROOT"] == "/var/lib/cortex/coordinator"
    assert "UNRELATED_MANAGER_VALUE" not in env


def test_filesystem_denial_accepts_sticky_directory_eperm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _load_driver_module()
    monkeypatch.setattr(
        driver,
        "_run",
        lambda argv, **_kwargs: driver.CommandResult(tuple(argv), 1, "", ""),
    )
    cases: list[dict[str, object]] = []

    driver._fs_denied(
        cases,
        family="durable-state",
        case_id="sticky-delete",
        user="cortex-builder",
        expression="Path('/protected/probe').unlink()",
    )

    assert cases == [
        {
            "family": "durable-state",
            "case": "sticky-delete",
            "principal": "cortex-builder",
            "status": "passed",
            "returncode": 1,
        }
    ]


def test_runtime_workspace_provisioning_uses_the_registry_acl_contract() -> None:
    driver = _load_driver_module()
    assets = [
        {
            "asset_id": "repo-worktree",
            "tier": "TIER_1",
            "runtime_managed": True,
            "is_directory": True,
            "path": "/var/lib/cortex/worktree/<job-id>",
            "acls": [
                {"account": "cortex-builder", "default": False, "perms": "rwX"},
                {"account": "cortex-builder", "default": True, "perms": "rwx"},
                {"account": "cortex-gate", "default": False, "perms": "rX"},
                {"account": "cortex-gate", "default": True, "perms": "rX"},
            ],
        },
        {
            "asset_id": "work-items-yaml",
            "tier": "TIER_1",
            "runtime_managed": False,
            "is_directory": False,
            "path": "/var/lib/cortex/worktree/<job-id>/.cortex/work-items.yaml",
            "acls": [],
        },
    ]

    workspace, grants = driver._runtime_workspace_provisioning_spec(assets)

    assert workspace == Path("/var/lib/cortex/worktree/qualification-probe")
    assert grants == (
        ("cortex-builder", "rwX", "rwx"),
        ("cortex-gate", "rX", "rX"),
    )


def test_runtime_workspace_provisioning_rejects_an_incomplete_acl_pair() -> None:
    driver = _load_driver_module()
    assets = [
        {
            "asset_id": "repo-worktree",
            "tier": "TIER_1",
            "runtime_managed": True,
            "is_directory": True,
            "path": "/var/lib/cortex/worktree/<job-id>",
            "acls": [
                {"account": "cortex-builder", "default": False, "perms": "rwX"},
            ],
        }
    ]

    with pytest.raises(driver.QualificationFailure, match="access/default ACL pair"):
        driver._runtime_workspace_provisioning_spec(assets)
