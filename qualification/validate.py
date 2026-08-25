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
R9_HEADLESS_PRINCIPALS = {"cortex-builder", "cortex-reviewer-planner"}
R9_DENY_ONLY_ASSET_IDS = {"review-verdict"}
R9_MUTATIONS = {
    "modify",
    "truncate",
    "delete",
    "replace",
    "symlink-swap",
    "rollback",
}
R9_DENIAL_RETURNCODES = {1, 13, 30}  # EPERM, EACCES, EROFS


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


def _required_fields(value: Any, path: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{path} must be an object")
    missing = fields - set(value)
    if missing:
        _fail(f"{path} missing required fields: {', '.join(sorted(missing))}")
    return value


def _artifact_json(evidence_root: Path, relative: str) -> dict[str, Any]:
    path = evidence_root / PurePosixPath(relative)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"{relative} is not readable canonical JSON: {exc}")
    if not isinstance(value, dict):
        _fail(f"{relative} must contain a JSON object")
    return value


def _normalized_keys(value: Any, keys: set[str]) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if re.sub(r"[^a-z0-9]", "", str(key).lower()) in keys and isinstance(
                child, str
            ):
                found.add(child.lower())
            found.update(_normalized_keys(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.update(_normalized_keys(child, keys))
    return found


def _validate_full_suite_artifacts(
    *,
    qualification: dict[str, Any],
    evidence_root: Path,
    artifact_hashes: dict[str, str],
) -> None:
    """Validate evidence semantics, not merely candidate-supplied filenames and hashes."""

    install = _artifact_json(evidence_root, "evidence/install-verification.json")
    _required_fields(
        install,
        "install-verification",
        {
            "schema_version",
            "result",
            "candidate",
            "attestation",
            "artifact_hashes",
            "service_identities",
        },
    )
    if install["schema_version"] != 1 or install["result"] != "pass":
        _fail("install-verification must be schema v1 with result=pass")
    candidate = _required_fields(
        install["candidate"],
        "install-verification.candidate",
        {"wheel_sha256", "bundle_sha256"},
    )
    if candidate["wheel_sha256"] != qualification["wheel"]["sha256"]:
        _fail("install-verification wheel hash does not match qualification")
    if candidate["bundle_sha256"] != qualification["bundle"]["sha256"]:
        _fail("install-verification bundle hash does not match qualification")
    attestation = _required_fields(
        install["attestation"], "install-verification.attestation", {"ok", "failures"}
    )
    if attestation["ok"] is not True or attestation["failures"] != []:
        _fail("install-verification generated-installed attestation did not pass")
    if (
        not isinstance(install["artifact_hashes"], dict)
        or not install["artifact_hashes"]
    ):
        _fail("install-verification artifact hashes must be non-empty")

    generated = _artifact_json(
        evidence_root, "evidence/generated-installed-attestation.json"
    )
    generated = _mapping(
        generated,
        "generated-installed-attestation",
        {
            "schema_version",
            "ok",
            "attestation",
            "artifact_hashes",
            "service_identities",
        },
    )
    if generated["schema_version"] != 1 or generated["ok"] is not True:
        _fail("generated-installed-attestation must pass")
    if generated["attestation"] != install["attestation"]:
        _fail("generated-installed-attestation does not match install verification")
    if generated["artifact_hashes"] != install["artifact_hashes"]:
        _fail("generated-installed artifact hash inventory drifted")
    if generated["service_identities"] != install["service_identities"]:
        _fail("generated-installed service identity inventory drifted")

    attack = _mapping(
        _artifact_json(evidence_root, "evidence/attack-matrix.json"),
        "attack-matrix",
        {
            "schema_version",
            "status",
            "families",
            "cases",
            "negative_controls",
            "authorized_mutations",
            "deny_only_assets",
            "covered_assets",
            "registry_asset_ids",
        },
    )
    required_families = {
        "capability",
        "durable-state",
        "enforcement-plane",
        "process",
        "gate",
    }
    if attack["schema_version"] != 1 or attack["status"] != "passed":
        _fail("attack-matrix must pass")
    if set(_list(attack["families"], "attack-matrix.families")) != required_families:
        _fail("attack-matrix must cover exactly five required families")
    cases = _list(attack["cases"], "attack-matrix.cases")
    seen_case_ids: dict[str, set[str]] = {family: set() for family in required_families}
    durable_counts: dict[str, int] = {}
    durable_rows: dict[str, list[dict[str, Any]]] = {}
    for index, raw in enumerate(cases):
        row = _mapping(
            raw,
            f"attack-matrix.cases[{index}]",
            {"family", "case", "principal", "status", "returncode"},
        )
        family = _nonempty_string(row["family"], f"attack-matrix.cases[{index}].family")
        if family not in required_families or row["status"] != "passed":
            _fail(f"attack-matrix case {index} is not a passed required-family result")
        case_id = _nonempty_string(row["case"], f"attack-matrix.cases[{index}].case")
        seen_case_ids[family].add(case_id)
        if family == "durable-state":
            durable_counts[case_id] = durable_counts.get(case_id, 0) + 1
            durable_rows.setdefault(case_id, []).append(row)
    required_prefixes = {
        "capability": {"T1.1", "T1.2", "T1.3", "T1.4"},
        "enforcement-plane": {f"T3.{index}" for index in range(1, 11)},
        "process": {"T4.1", "T4.2", "T4.3", "T4.4"},
        "gate": {f"T5.{index}" for index in range(1, 11)},
    }
    for family, prefixes in required_prefixes.items():
        missing = {
            prefix
            for prefix in prefixes
            if not any(case_id.startswith(prefix) for case_id in seen_case_ids[family])
        }
        if missing:
            _fail(f"attack-matrix {family} missing cases: {', '.join(sorted(missing))}")
    asset_ids = _list(attack["registry_asset_ids"], "attack-matrix.registry_asset_ids")
    if len(asset_ids) != len(set(asset_ids)) or attack["covered_assets"] != len(
        asset_ids
    ):
        _fail("attack-matrix durable registry coverage is inconsistent")
    deny_only_raw = attack["deny_only_assets"]
    if not isinstance(deny_only_raw, list):
        _fail("attack-matrix.deny_only_assets must be an array")
    deny_only_assets = deny_only_raw
    if len(deny_only_assets) != len(set(deny_only_assets)):
        _fail("attack-matrix.deny_only_assets contains duplicates")
    for asset_id in deny_only_assets:
        _nonempty_string(asset_id, "attack-matrix.deny_only_assets[]")
        if asset_id not in asset_ids:
            _fail(f"deny-only asset is not in registry coverage: {asset_id}")
    expected_deny_only = sorted(R9_DENY_ONLY_ASSET_IDS.intersection(asset_ids))
    if deny_only_assets != expected_deny_only:
        _fail(
            "attack-matrix.deny_only_assets does not match the declared legacy "
            f"boundary: expected {expected_deny_only}, got {deny_only_assets}"
        )
    for asset_id in asset_ids:
        _nonempty_string(asset_id, "attack-matrix.registry_asset_ids[]")
        for operation in R9_MUTATIONS:
            case_id = f"{asset_id}:{operation}"
            if durable_counts.get(case_id) != 2:
                _fail(
                    f"durable-state matrix must test {case_id} for both headless accounts"
                )

    authorized_raw = attack["authorized_mutations"]
    if not isinstance(authorized_raw, list):
        _fail("attack-matrix.authorized_mutations must be an array")
    authorized: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(authorized_raw):
        row = _mapping(
            raw,
            f"attack-matrix.authorized_mutations[{index}]",
            {"asset_id", "principal", "operation"},
        )
        asset_id = _nonempty_string(
            row["asset_id"], f"attack-matrix.authorized_mutations[{index}].asset_id"
        )
        principal = _nonempty_string(
            row["principal"], f"attack-matrix.authorized_mutations[{index}].principal"
        )
        operation = _nonempty_string(
            row["operation"], f"attack-matrix.authorized_mutations[{index}].operation"
        )
        if asset_id not in asset_ids:
            _fail(f"authorized mutation names an uncovered asset: {asset_id}")
        if principal not in R9_HEADLESS_PRINCIPALS:
            _fail(f"authorized mutation names an unsupported headless principal: {principal}")
        if operation not in R9_MUTATIONS:
            _fail(f"authorized mutation names an unsupported operation: {operation}")
        key = (asset_id, principal, operation)
        if key in authorized:
            _fail(f"duplicate authorized mutation: {asset_id}:{operation}:{principal}")
        authorized.add(key)

    for asset_id in asset_ids:
        for operation in R9_MUTATIONS:
            case_id = f"{asset_id}:{operation}"
            rows = durable_rows[case_id]
            if {str(row["principal"]) for row in rows} != R9_HEADLESS_PRINCIPALS:
                _fail(f"durable-state matrix must cover both declared headless principals: {case_id}")
            for row in rows:
                key = (asset_id, str(row["principal"]), operation)
                returncode = row["returncode"]
                if not isinstance(returncode, int):
                    _fail(f"durable-state returncode is not an integer: {case_id}")
                if key in authorized:
                    if returncode != 0:
                        _fail(f"authorized durable mutation did not succeed: {case_id}")
                elif returncode not in R9_DENIAL_RETURNCODES:
                    _fail(f"unauthorized durable mutation was not denied: {case_id}")
    controls = _list(attack["negative_controls"], "attack-matrix.negative_controls")
    control_families: set[str] = set()
    for index, raw in enumerate(controls):
        row = _mapping(
            raw,
            f"attack-matrix.negative_controls[{index}]",
            {"family", "case", "principal", "status", "returncode"},
        )
        if row["status"] != "passed" or row["returncode"] != 0:
            _fail(f"attack-matrix negative control {index} did not pass")
        control_families.add(str(row["family"]))
    if control_families != required_families:
        _fail(
            "attack-matrix must include a successful negative control for every family"
        )

    provider_evidence = _mapping(
        _artifact_json(evidence_root, "evidence/provider-capabilities.json"),
        "provider-capabilities",
        {"schema_version", "providers"},
    )
    if provider_evidence["schema_version"] != 1 or not isinstance(
        provider_evidence["providers"], dict
    ):
        _fail("provider-capabilities schema is invalid")
    if set(provider_evidence["providers"]) != set(REQUIRED_PROVIDERS):
        _fail("provider-capabilities must contain exactly the required providers")
    qualification_providers = {
        row["provider"]: row for row in qualification["providers"]
    }
    for name, raw in provider_evidence["providers"].items():
        row = _mapping(
            raw,
            f"provider-capabilities.providers.{name}",
            {
                "preflight",
                "returncode",
                "models",
                "efforts",
                "native_metadata",
                "response_token",
            },
        )
        expected = qualification_providers[name]
        preflight = _mapping(
            row["preflight"],
            f"provider-capabilities.providers.{name}.preflight",
            {
                "returncode",
                "status",
                "authenticated",
                "quota",
                "fallback",
                "skipped",
            },
        )
        if (
            row["returncode"] != 0
            or row["native_metadata"] is not True
            or row["response_token"] is not True
            or preflight["returncode"] != 0
            or preflight["status"] not in {"ready", "passed", "authenticated"}
            or preflight["authenticated"] is not True
            or preflight["quota"] != "available"
            or preflight["fallback"] is not False
            or preflight["skipped"] is not False
        ):
            _fail(
                f"provider {name} did not return successful native metadata and response token"
            )
        if row["models"] != [expected["runtime_model"]]:
            _fail(f"provider {name} native evidence must name one exact runtime model")
        if row["efforts"] != [expected["runtime_effort"]]:
            _fail(f"provider {name} native evidence must name one exact runtime effort")
        if (
            expected["quota"] != preflight["quota"]
            or expected["fallback"] != preflight["fallback"]
        ):
            _fail(f"provider {name} verdict is not bound to native preflight")

    dispatch = _mapping(
        _artifact_json(evidence_root, "evidence/dispatch-closeout.json"),
        "dispatch-closeout",
        {
            "schema_version",
            "status",
            "repository",
            "work_id",
            "issue",
            "terminal",
            "required_markers",
            "artifacts",
        },
    )
    if dispatch["schema_version"] != 1 or dispatch["status"] != "passed":
        _fail("dispatch-closeout must pass")
    terminal_states = _normalized_keys(
        dispatch["terminal"], {"state", "status", "lifecycle"}
    )
    if not terminal_states & {"done", "delivered", "closed"}:
        _fail("dispatch-closeout terminal payload is not terminal")
    required_markers = {
        "candidate",
        "bundle",
        "verdict",
        "ledger",
        "evidence",
        "completion",
    }
    if not required_markers <= set(
        _list(dispatch["required_markers"], "dispatch-closeout.required_markers")
    ):
        _fail("dispatch-closeout is missing a required terminal artifact class")
    dispatch_artifacts = _list(dispatch["artifacts"], "dispatch-closeout.artifacts")
    for index, raw in enumerate(dispatch_artifacts):
        row = _mapping(raw, f"dispatch-closeout.artifacts[{index}]", {"path", "sha256"})
        _nonempty_string(row["path"], f"dispatch-closeout.artifacts[{index}].path")
        _digest(row["sha256"], f"dispatch-closeout.artifacts[{index}].sha256")

    github = _mapping(
        _artifact_json(evidence_root, "evidence/manager-github-auth.json"),
        "manager-github-auth",
        {
            "schema_version",
            "status",
            "repository",
            "authenticated",
            "dry_run",
            "remote_refs_unchanged",
            "before_sha256",
            "after_sha256",
        },
    )
    if (
        github["schema_version"] != 1
        or github["status"] != "passed"
        or github["authenticated"] is not True
        or github["dry_run"] is not True
        or github["remote_refs_unchanged"] is not True
    ):
        _fail("manager-github-auth did not pass its authenticated dry-run probe")
    before = _digest(github["before_sha256"], "manager-github-auth.before_sha256")
    after = _digest(github["after_sha256"], "manager-github-auth.after_sha256")
    if before != after:
        _fail("manager-github-auth remote refs changed")

    inventory = _mapping(
        _artifact_json(evidence_root, "evidence/artifact-inventory.json"),
        "artifact-inventory",
        {"schema_version", "status", "artifacts"},
    )
    if inventory["schema_version"] != 1 or inventory["status"] != "passed":
        _fail("artifact-inventory must pass")
    inventory_hashes: dict[str, str] = {}
    for index, raw in enumerate(
        _list(inventory["artifacts"], "artifact-inventory.artifacts")
    ):
        row = _mapping(
            raw, f"artifact-inventory.artifacts[{index}]", {"path", "sha256"}
        )
        path = _nonempty_string(
            row["path"], f"artifact-inventory.artifacts[{index}].path"
        )
        if path in inventory_hashes:
            _fail(f"artifact-inventory duplicates {path}")
        inventory_hashes[path] = _digest(
            row["sha256"], f"artifact-inventory.artifacts[{index}].sha256"
        )
    expected_inventory = {
        path: digest
        for path, digest in artifact_hashes.items()
        if path != "evidence/artifact-inventory.json"
    }
    if inventory_hashes != expected_inventory:
        _fail(
            "artifact-inventory does not exactly attest every non-inventory evidence file"
        )


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
    artifact_hashes: dict[str, str] = {}
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
        artifact_hashes[artifact_path] = expected_artifact_sha
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
        assert evidence_root is not None
        _validate_full_suite_artifacts(
            qualification=root,
            evidence_root=evidence_root,
            artifact_hashes=artifact_hashes,
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
        if (
            args.bundle_sha256 is not None
            and SHA256.fullmatch(args.bundle_sha256) is None
        ):
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
