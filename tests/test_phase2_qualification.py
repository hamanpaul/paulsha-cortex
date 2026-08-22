from __future__ import annotations

import copy
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


def test_reference_image_is_ubuntu_2404_systemd_pid1_with_os_dependencies() -> None:
    raw = _required_text(DOCKERFILE)
    lowered = raw.lower()

    assert re.search(r"(?m)^\s*from\s+ubuntu:24\.04(?:\s|$)", lowered)
    for package in ("systemd", "acl", "sudo", "git", "bubblewrap", "socat", "nodejs"):
        assert re.search(rf"\b{re.escape(package)}\b", lowered), f"Dockerfile must install {package}"
    assert re.search(r"\b(?:polkitd|policykit-1)\b", lowered), "Dockerfile must install polkit"
    assert re.search(
        r"(?im)^\s*(?:entrypoint|cmd)\s+\[\s*[\"'](?:/sbin/init|/lib/systemd/systemd)[\"']",
        raw,
    ), "systemd must be container PID 1"

    assert not re.search(r"(?im)^\s*(?:copy|add)\s+\.\s", raw), (
        "the image must not copy the checkout as runtime code"
    )


def test_runner_uses_exact_artifacts_and_never_an_editable_checkout() -> None:
    raw = _required_text(RUNNER)
    lowered = raw.lower()

    assert "docker build" in lowered
    assert "docker run" in lowered
    assert "/artifacts" in raw
    assert re.search(r"(?:/artifacts[^\n]*\b(?:ro|readonly)\b|\breadonly\b[^\n]*/artifacts)", raw), (
        "candidate artifacts must be mounted read-only"
    )
    assert re.search(r"\.whl\b|wheel", lowered), "the candidate wheel must be selected explicitly"
    assert "bundle" in lowered, "the exact candidate bundle must be passed to qualification"
    assert "sha256sum" in lowered, "candidate artifacts must be hash checked before installation"
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


def test_runner_declares_disposable_systemd_container_boundaries() -> None:
    raw = _required_text(RUNNER)

    assert "--privileged" in raw
    assert re.search(r"--cgroupns(?:=|\s+)host", raw)
    assert "/sys/fs/cgroup" in raw
    assert re.search(r"/sys/fs/cgroup[^\n]*(?:rw|readwrite)", raw)
    assert re.search(r"--tmpfs(?:=|\s+)[\"']?/run(?::|[\"'\s])", raw)
    assert re.search(r"--tmpfs(?:=|\s+)[\"']?/run/lock(?::|[\"'\s])", raw)
    assert re.search(r"(?:--mount\s+[^\n]*type=volume|--volume\s+[^\n]*:/var/lib/cortex)", raw), (
        "durable test data must use an independent Docker volume"
    )

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
        assert target == "/artifacts" or target == "/sys/fs/cgroup" or target.startswith(
            "/var/lib/cortex"
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


def test_qualification_validator_accepts_only_matching_passed_evidence(tmp_path: Path) -> None:
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
