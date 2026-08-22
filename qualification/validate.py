#!/usr/bin/env python3
"""Fail-closed validator for Cortex release qualification evidence.

This module intentionally uses only the Python standard library so a release
job can validate evidence before installing the release candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn


SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
WHEEL_NAME = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")
ROOT_KEYS = {
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
}
REQUIRED_PROVIDERS = {
    "agy": ("gemini-3.7-flash", "high"),
    "copilot": ("gpt-5.4", "xhigh"),
    "codex": ("gpt-5", "normal"),
}
REQUIRED_TESTS = {"fresh-install", "full-dispatch-closeout"}
REQUIRED_RELEASE_SERVICES = {
    "cortex-egress-proxy.service",
    "cortex-manager.service",
    "cortex-monitor.service",
}
REQUIRED_RELEASE_ARTIFACTS = {
    "evidence/install-verification.json",
    "evidence/generated-installed-attestation.json",
    "evidence/attack-matrix.json",
    "evidence/provider-capabilities.json",
    "evidence/dispatch-closeout.json",
    "evidence/manager-github-auth.json",
    "evidence/artifact-inventory.json",
}
REQUIRED_RELEASE_TESTS = {
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
}


class ValidationError(ValueError):
    """Qualification evidence is malformed or is not releasable."""


def _fail(message: str) -> NoReturn:
    raise ValidationError(message)


def _mapping(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    actual = set(value)
    missing = keys - actual
    extra = actual - keys
    if missing:
        _fail(f"{path} missing required fields: {', '.join(sorted(missing))}")
    if extra:
        _fail(f"{path} has unknown fields: {', '.join(sorted(extra))}")
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{path} must be a non-empty string")
    return value


def _digest(value: Any, path: str) -> str:
    text = _nonempty_string(value, path)
    if SHA256.fullmatch(text) is None:
        _fail(f"{path} must be a lowercase SHA-256 digest")
    return text


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        _fail(f"{path} must be a non-empty array")
    return value


def validate(
    payload: Any,
    *,
    candidate_sha: str,
    wheel_sha256: str,
    bundle_sha256: str | None = None,
    evidence_root: Path | None = None,
    require_full_suite: bool = False,
) -> None:
    """Validate schema, release binding, and every fail-closed verdict."""

    root = _mapping(payload, "$", ROOT_KEYS)
    if root["schema_version"] != 1 or isinstance(root["schema_version"], bool):
        _fail("$.schema_version must be 1")
    if root["status"] != "passed":
        _fail("$.status must be passed")

    evidence_sha = _nonempty_string(root["candidate_sha"], "$.candidate_sha")
    if SHA40.fullmatch(evidence_sha) is None:
        _fail("$.candidate_sha must be a lowercase 40-hex commit")
    if evidence_sha != candidate_sha:
        _fail("qualification candidate SHA does not match the release commit")

    wheel = _mapping(root["wheel"], "$.wheel", {"filename", "sha256"})
    wheel_name = _nonempty_string(wheel["filename"], "$.wheel.filename")
    if WHEEL_NAME.fullmatch(wheel_name) is None:
        _fail("$.wheel.filename must be a basename ending in .whl")
    evidence_wheel_sha = _digest(wheel["sha256"], "$.wheel.sha256")
    if evidence_wheel_sha != wheel_sha256:
        _fail("qualification wheel SHA-256 does not match the release wheel")

    bundle = _mapping(root["bundle"], "$.bundle", {"sha256"})
    evidence_bundle_sha = _digest(bundle["sha256"], "$.bundle.sha256")
    if bundle_sha256 is not None and evidence_bundle_sha != bundle_sha256:
        _fail("qualification bundle SHA-256 does not match the release bundle")
    image = _mapping(root["image"], "$.image", {"digest"})
    image_digest = _nonempty_string(image["digest"], "$.image.digest")
    if IMAGE_DIGEST.fullmatch(image_digest) is None:
        _fail("$.image.digest must be a sha256-prefixed image digest")

    service_names: set[str] = set()
    for index, raw_service in enumerate(_list(root["services"], "$.services")):
        path = f"$.services[{index}]"
        service = _mapping(raw_service, path, {"name", "uid", "gid", "active"})
        name = _nonempty_string(service["name"], f"{path}.name")
        if name in service_names:
            _fail(f"duplicate service identity: {name}")
        service_names.add(name)
        for key in ("uid", "gid"):
            value = service[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                _fail(f"{path}.{key} must be a non-negative integer")
        if service["active"] is not True:
            _fail(f"{path}.active must be true")
    if require_full_suite and service_names != REQUIRED_RELEASE_SERVICES:
        _fail("$.services must contain exactly egress proxy, Manager, and Monitor")
    if require_full_suite:
        services_by_name = {service["name"]: service for service in root["services"]}
        manager = services_by_name["cortex-manager.service"]
        monitor = services_by_name["cortex-monitor.service"]
        egress = services_by_name["cortex-egress-proxy.service"]
        if (manager["uid"], manager["gid"]) != (991, 991):
            _fail("Manager service identity must be uid=991 gid=991")
        if (monitor["uid"], monitor["gid"]) != (991, 991):
            _fail("Monitor service identity must be uid=991 gid=991")
        if egress["uid"] == 0 or egress["gid"] == 0:
            _fail("egress proxy must run as a non-root uid and gid")
        if egress["uid"] == 991 or egress["gid"] == 991:
            _fail("egress proxy must not share the Manager uid or gid")
        if egress["uid"] != egress["gid"]:
            _fail("egress proxy must use its dedicated same-numbered uid and gid")

    provider_names: set[str] = set()
    provider_keys = {
        "provider",
        "requested_model",
        "runtime_model",
        "requested_effort",
        "runtime_effort",
        "status",
        "quota",
        "fallback",
    }
    for index, raw_provider in enumerate(_list(root["providers"], "$.providers")):
        path = f"$.providers[{index}]"
        provider = _mapping(raw_provider, path, provider_keys)
        name = _nonempty_string(provider["provider"], f"{path}.provider")
        if name in provider_names:
            _fail(f"duplicate provider verdict: {name}")
        provider_names.add(name)
        requested_model = _nonempty_string(
            provider["requested_model"], f"{path}.requested_model"
        )
        runtime_model = _nonempty_string(
            provider["runtime_model"], f"{path}.runtime_model"
        )
        requested_effort = _nonempty_string(
            provider["requested_effort"], f"{path}.requested_effort"
        )
        runtime_effort = _nonempty_string(
            provider["runtime_effort"], f"{path}.runtime_effort"
        )
        if provider["status"] != "passed":
            _fail(f"{path}.status must be passed")
        if provider["quota"] != "available":
            _fail(f"{path}.quota must be available")
        if provider["fallback"] is not False:
            _fail(f"{path}.fallback must be false")
        if runtime_model != requested_model:
            _fail(f"{path} runtime model does not match requested model")
        if runtime_effort != requested_effort:
            _fail(f"{path} runtime effort does not match requested effort")
    if provider_names != set(REQUIRED_PROVIDERS):
        _fail("$.providers must contain exactly agy, copilot, and codex verdicts")
    for provider in root["providers"]:
        expected_model, expected_effort = REQUIRED_PROVIDERS[provider["provider"]]
        if provider["requested_model"] != expected_model:
            _fail(
                f"provider {provider['provider']} must request model {expected_model}"
            )
        if provider["requested_effort"] != expected_effort:
            _fail(
                f"provider {provider['provider']} must request effort {expected_effort}"
            )

    test_names: set[str] = set()
    for index, raw_test in enumerate(_list(root["tests"], "$.tests")):
        path = f"$.tests[{index}]"
        test = _mapping(raw_test, path, {"name", "status"})
        name = _nonempty_string(test["name"], f"{path}.name")
        if name in test_names:
            _fail(f"duplicate qualification test: {name}")
        test_names.add(name)
        if test["status"] != "passed":
            _fail(f"{path}.status must be passed")
    missing_tests = REQUIRED_TESTS - test_names
    if missing_tests:
        _fail(
            "$.tests missing release-critical results: "
            + ", ".join(sorted(missing_tests))
        )
    if require_full_suite:
        missing_release_tests = REQUIRED_RELEASE_TESTS - test_names
        if missing_release_tests:
            _fail(
                "$.tests missing full release qualification results: "
                + ", ".join(sorted(missing_release_tests))
            )

    artifact_paths: set[str] = set()
    for index, raw_artifact in enumerate(_list(root["artifacts"], "$.artifacts")):
        path = f"$.artifacts[{index}]"
        artifact = _mapping(raw_artifact, path, {"path", "sha256"})
        artifact_path = _nonempty_string(artifact["path"], f"{path}.path")
        pure = PurePosixPath(artifact_path)
        if pure.is_absolute() or ".." in pure.parts or "\x00" in artifact_path:
            _fail(f"{path}.path must be a safe relative path")
        if artifact_path in artifact_paths:
            _fail(f"duplicate evidence artifact path: {artifact_path}")
        artifact_paths.add(artifact_path)
        expected_artifact_sha = _digest(artifact["sha256"], f"{path}.sha256")
        if evidence_root is not None:
            candidate = evidence_root / pure
            if candidate.is_symlink() or not candidate.is_file():
                _fail(f"{path}.path does not identify a regular evidence file")
            actual_artifact_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual_artifact_sha != expected_artifact_sha:
                _fail(f"{path}.sha256 does not match the evidence file")

    if require_full_suite and evidence_root is None:
        _fail("full release validation requires --evidence-root")
    if require_full_suite:
        missing_artifacts = REQUIRED_RELEASE_ARTIFACTS - artifact_paths
        if missing_artifacts:
            _fail(
                "$.artifacts missing release-critical evidence: "
                + ", ".join(sorted(missing_artifacts))
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification", required=True, type=Path)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--bundle-sha256")
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--require-full-suite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if SHA40.fullmatch(args.candidate_sha) is None:
            _fail("--candidate-sha must be a lowercase 40-hex commit")
        if SHA256.fullmatch(args.wheel_sha256) is None:
            _fail("--wheel-sha256 must be a lowercase SHA-256 digest")
        if args.bundle_sha256 is not None and SHA256.fullmatch(args.bundle_sha256) is None:
            _fail("--bundle-sha256 must be a lowercase SHA-256 digest")
        with args.qualification.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        validate(
            payload,
            candidate_sha=args.candidate_sha,
            wheel_sha256=args.wheel_sha256,
            bundle_sha256=args.bundle_sha256,
            evidence_root=args.evidence_root,
            require_full_suite=args.require_full_suite,
        )
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"qualification validation failed: {exc}", file=sys.stderr)
        return 1
    print("qualification evidence is valid and release-bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
