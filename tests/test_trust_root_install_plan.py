"""Phase 2 trust-root installer RED contract: desired-state planning.

These tests are deliberately rootless.  Planning is a pure conversion from explicit
configuration plus exact artifacts into canonical JSON; it must never inspect the
operator's HOME or execute generated shell text.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import (
    InstallPlanError,
    UnsafeInstallPathError,
    apply_plan,
    bind_bundle_artifacts,
    build_install_plan,
    canonical_plan_bytes,
    new_install_receipt,
    plan_sha256,
    validate_apply_plan,
)
from paulsha_cortex.trust_root.install import cli as install_cli
from paulsha_cortex.trust_root.install import core as install_core


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / "paulsha_cortex-0.2.0-py3-none-any.whl"
    bundle = tmp_path / "paulsha-cortex-phase2.bundle"
    wheel.write_bytes(b"exact candidate wheel\n")
    bundle.write_bytes(b"exact source bundle\n")
    return wheel, bundle


def _safe_config(tmp_path: Path) -> dict[str, object]:
    roots = tmp_path / "target"
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "instance": "cortex",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": "a" * 40,
        },
        "operator_account": "cortex-ops",
        "external_reader_account": "<absent>",
        "accounts": {
            "cortex-manager": {
                "uid": 991,
                "gid": 991,
                "home": str(roots / "var/lib/cortex-manager"),
                "shell": "/usr/sbin/nologin",
            },
            "cortex-reviewer-planner": {
                "uid": 992,
                "gid": 992,
                "home": str(roots / "var/lib/cortex-reviewer-planner"),
                "shell": "/usr/sbin/nologin",
            },
            "cortex-builder": {
                "uid": 993,
                "gid": 993,
                "home": str(roots / "var/lib/cortex-builder"),
                "shell": "/usr/sbin/nologin",
            },
            "cortex-gate": {
                "uid": 994,
                "gid": 994,
                "home": str(roots / "var/lib/cortex-gate"),
                "shell": "/usr/sbin/nologin",
            },
        },
        "service_accounts": {
            "cortex-egress": {
                "uid": 995,
                "gid": 995,
                "home": str(roots / "var/lib/cortex-egress"),
                "shell": "/usr/sbin/nologin",
            }
        },
        "roots": {
            "deploy": str(roots / "opt/cortex"),
            "state": str(roots / "var/lib/cortex"),
            "systemd": str(roots / "etc/systemd/system"),
            "polkit": str(roots / "etc/polkit-1/rules.d"),
        },
        "source_repositories": ["paulsha-cortex"],
        "legacy_policy": "quarantine",
        "providers": {
            "builder": ["codex"],
            "reviewer-planner": ["agy", "copilot"],
            "manager": ["github"],
        },
        "toolchain": {
            "codex": {"version": "0.150.0", "sha256": "1" * 64},
            "agy": {"version": "1.2.3", "sha256": "2" * 64},
            "copilot": {"version": "0.0.400", "sha256": "3" * 64},
        },
    }


def _plan_document(tmp_path: Path, config: dict[str, object] | None = None):
    wheel, bundle = _artifacts(tmp_path)
    plan = build_install_plan(
        config=config or _safe_config(tmp_path),
        candidate_wheel=wheel,
        bundle=bundle,
    )
    return plan, json.loads(canonical_plan_bytes(plan))


def _bound_plan_document(tmp_path: Path, config: dict[str, object] | None = None):
    plan, _document = _plan_document(tmp_path, config)
    tools = [
        {
            "name": name,
            "version": configured["version"],
            "shape": "file",
            "resolved_path": str(tmp_path / f"{name}.locked"),
            "sha256": configured["sha256"],
        }
        for name, configured in plan["toolchain_manifest"].items()
    ]
    repository = tmp_path / "paulsha-cortex.bundle"
    repository.write_bytes(b"exact source bundle\n")
    bound = bind_bundle_artifacts(
        plan,
        {
            "toolchain": tools,
            "source_repositories": [
                {
                    "slug": "paulsha-cortex",
                    "commit": "a" * 40,
                    "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
                    "resolved_path": str(repository),
                    "sha256": _sha256(repository),
                }
            ],
        },
    )
    return bound, json.loads(canonical_plan_bytes(bound))


def test_same_inputs_produce_byte_identical_canonical_plan_and_hash(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)

    first = build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)
    second = build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)

    assert canonical_plan_bytes(first) == canonical_plan_bytes(second)
    assert plan_sha256(first) == plan_sha256(second)
    assert plan_sha256(first) == hashlib.sha256(canonical_plan_bytes(first)).hexdigest()
    # Canonical JSON is a single deterministic encoding, not pretty-printed output.
    assert canonical_plan_bytes(first).endswith(b"\n")
    assert b"\n  " not in canonical_plan_bytes(first)


def test_plan_is_exact_artifact_bound_four_way_structured_desired_state(
    tmp_path: Path,
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    plan = build_install_plan(
        config=_safe_config(tmp_path), candidate_wheel=wheel, bundle=bundle
    )
    doc = json.loads(canonical_plan_bytes(plan))

    assert doc["schema_version"] == 1
    assert doc["scheme"] == "four-way"
    assert doc["repo_identity"]["commit"] == "a" * 40
    assert doc["source_repositories"] == ["paulsha-cortex"]
    assert doc["candidate"]["wheel_sha256"] == _sha256(wheel)
    assert doc["candidate"]["bundle_sha256"] == _sha256(bundle)
    assert {row["name"] for row in doc["accounts"]} == {
        "cortex-manager",
        "cortex-reviewer-planner",
        "cortex-builder",
        "cortex-gate",
    }
    assert doc["assets"], "registry/permgen inventory must not be replaced by prose"
    assert doc["scaffolds"], "permgen scaffold inventory must be part of the transaction"
    assert doc["apply_order"], "apply must consume typed ordered steps"
    assert all("shell" not in step and "command" not in step for step in doc["apply_order"])
    assert set(doc["generated"]) >= {
        "units",
        "shim",
        "polkit",
        "gitconfigs",
        "toolchain_wrappers",
    }
    state_root = next(
        step
        for step in doc["apply_order"]
        if step.get("path") == doc["roots"]["state"]
    )
    assert state_root["durable"] is True
    assert state_root["adoption_policy"] == "empty-managed-root-mount"


def test_scaffold_targets_are_applied_before_home_redirect_symlinks(
    tmp_path: Path,
) -> None:
    plan, _doc = _plan_document(tmp_path)
    steps = plan["apply_order"]
    step_ids = [step["step_id"] for step in steps]

    for asset_id in (
        "reviewer-planner-agy-state",
        "reviewer-planner-claude-state",
    ):
        symlink = next(asset for asset in plan["assets"] if asset["asset_id"] == asset_id)
        target_step = f"scaffold:{symlink['symlink_target']}"
        assert target_step in step_ids
        assert step_ids.index(target_step) < step_ids.index(f"asset:{asset_id}")


def test_every_managed_directory_is_applied_before_its_managed_descendants(
    tmp_path: Path,
) -> None:
    plan, _doc = _plan_document(tmp_path)
    directories = [
        (index, step)
        for index, step in enumerate(plan["apply_order"])
        if step.get("kind") == "asset" and step.get("asset_type") == "directory"
    ]
    positions = {step["path"]: index for index, step in directories}

    deploy = plan["roots"]["deploy"]
    state = plan["roots"]["state"]
    assert {
        f"{deploy}/venvs",
        f"{deploy}/toolchain/bin",
        f"{deploy}/toolchain/lib",
        f"{state}/runtime/codex-home",
        f"{state}/runtime/job-cache",
        f"{state}/coordinator/job-prompts",
    } <= set(positions)

    for child, child_position in positions.items():
        for parent, parent_position in positions.items():
            if child != parent and Path(child).is_relative_to(Path(parent)):
                assert parent_position < child_position, (parent, child)

    allowed_external_parents = {
        Path(plan["roots"]["deploy"]).parent,
        Path(plan["roots"]["state"]).parent,
        Path(plan["roots"]["systemd"]),
        Path(plan["roots"]["polkit"]),
        *(
            Path(row["home"]).parent
            for row in (*plan["accounts"], *plan["service_accounts"])
        ),
    }
    for index, step in enumerate(plan["apply_order"]):
        if step.get("kind") not in {"asset", "repository", "toolchain", "venv"}:
            continue
        parent = Path(step["path"]).parent
        assert (
            parent in allowed_external_parents
            or positions.get(parent.as_posix(), index) < index
        )


def test_plan_topology_guard_rejects_an_unmanaged_intermediate_parent(
    tmp_path: Path,
) -> None:
    plan, _doc = _plan_document(tmp_path)
    missing = f"{plan['roots']['state']}/runtime/codex-home"
    plan["apply_order"] = [
        step for step in plan["apply_order"] if step.get("path") != missing
    ]

    with pytest.raises(InstallPlanError, match="unmanaged immediate parent"):
        install_core._assert_managed_parent_topology(plan)


def test_apply_revalidates_serialized_plan_topology_before_backend_mutation(
    tmp_path: Path,
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    serialized = json.loads(canonical_plan_bytes(plan))
    missing = f"{serialized['roots']['state']}/runtime/codex-home"
    serialized["apply_order"] = [
        step for step in serialized["apply_order"] if step.get("path") != missing
    ]

    class MutationSentinelBackend:
        def __init__(self) -> None:
            self.applied: list[str] = []

        def preflight_facts(self, candidate):
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
                "paths": {
                    step["path"]: {"exists": False, "is_symlink": False}
                    for step in candidate["apply_order"]
                    if "path" in step
                },
            }

        def inspect_step(self, _step):
            return {"exists": False}

        def apply_step(self, step):
            self.applied.append(str(step["step_id"]))
            raise AssertionError("schema guard ran after privileged mutation")

    backend = MutationSentinelBackend()
    receipt = new_install_receipt(serialized)

    with pytest.raises(InstallPlanError, match="unmanaged immediate parent"):
        apply_plan(
            serialized,
            confirm_sha256=plan_sha256(serialized),
            receipt=receipt,
            backend=backend,
        )

    assert backend.applied == []


def test_public_apply_validation_is_pure_for_a_valid_plan(tmp_path: Path) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    before = canonical_plan_bytes(plan)

    steps = validate_apply_plan(plan, confirm_sha256=plan_sha256(plan))

    assert [step["step_id"] for step in steps] == [
        step["step_id"] for step in plan["apply_order"]
    ]
    assert canonical_plan_bytes(plan) == before


def test_canonical_receipt_path_binds_full_plan_identity(tmp_path: Path) -> None:
    plan, _doc = _plan_document(tmp_path)

    receipt_path = install_core.canonical_receipt_path(plan)

    assert plan["receipt_path"] == str(receipt_path)
    assert receipt_path.parent == Path(plan["roots"]["state"]).parent / (
        f"{Path(plan['roots']['state']).name}-install-receipts"
    )
    assert receipt_path.name == (
        f"{'a' * 40}-{plan['candidate']['wheel_sha256']}.json"
    )
    for field in ("state", "commit", "wheel"):
        changed = deepcopy(plan)
        if field == "state":
            changed["roots"]["state"] = f"{plan['roots']['state']}-other"
        elif field == "commit":
            changed["repo_identity"]["commit"] = "b" * 40
        else:
            changed["candidate"]["wheel_sha256"] = "d" * 64
        assert install_core.canonical_receipt_path(changed) != receipt_path


def test_apply_cli_rejects_noncanonical_plan_receipt_before_override_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    plan["receipt_path"] = str(tmp_path / "attacker-selected.json")
    plan_path = tmp_path / "noncanonical-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    override = tmp_path / "existing" / "receipt.json"
    override.parent.mkdir()
    original = b"DO-NOT-REPLACE-EXISTING-RECEIPT\n"
    override.write_bytes(original)
    override.chmod(0o600)

    class MutationSentinelBackend:
        preflight_calls = 0

        def preflight_facts(self, _plan):
            type(self).preflight_calls += 1
            raise AssertionError("noncanonical receipt path reached backend")

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "LocalInstallBackend", MutationSentinelBackend)

    assert install_cli.main(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--confirm-sha256",
            plan_sha256(plan),
            "--receipt",
            str(override),
        ]
    ) == 1
    assert override.read_bytes() == original
    assert MutationSentinelBackend.preflight_calls == 0


def test_apply_cli_refuses_same_path_receipt_for_a_different_valid_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    other = deepcopy(plan)
    other["candidate"]["wheel_sha256"] = "d" * 64
    other["receipt_path"] = str(install_core.canonical_receipt_path(other))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    override = (tmp_path / "override" / "receipt.json").absolute()
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )
    new_install_receipt(other, path=override)
    before = override.read_bytes()

    class MutationSentinelBackend:
        preflight_calls = 0

        def preflight_facts(self, _plan):
            type(self).preflight_calls += 1
            raise AssertionError("different-plan receipt reached backend")

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "LocalInstallBackend", MutationSentinelBackend)

    assert install_cli.main(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--confirm-sha256",
            plan_sha256(plan),
            "--receipt",
            str(override),
        ]
    ) == 1
    assert override.read_bytes() == before
    assert MutationSentinelBackend.preflight_calls == 0


@pytest.mark.parametrize(
    "case",
    [
        "candidate-missing",
        "candidate-extra",
        "candidate-step-id",
        "candidate-path",
        "candidate-active-link",
        "candidate-wheel-sha",
        "candidate-desired-sha",
        "candidate-unlocked",
        "accounts-empty",
        "account-step-missing",
        "account-step-mismatch",
        "repo-identity-extra",
        "repo-identity-query",
        "source-repository-unsafe",
        "source-repository-duplicate",
        "repository-step-missing",
        "repository-step-mismatch",
        "repository-step-extra",
        "provider-manifest-invalid",
        "credential-manifest-mismatch",
        "credential-manifest-order",
    ],
)
def test_apply_cli_rejects_cross_field_authority_drift_before_receipt_or_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    candidate = next(step for step in plan["apply_order"] if step["kind"] == "venv")
    repository = next(
        step for step in plan["apply_order"] if step["kind"] == "repository"
    )
    if case == "candidate-missing":
        plan["apply_order"].remove(candidate)
    elif case == "candidate-extra":
        shadow = deepcopy(candidate)
        shadow["step_id"] = "candidate-venv-shadow"
        shadow["path"] = f"{candidate['path']}-shadow"
        plan["apply_order"].append(shadow)
    elif case == "candidate-step-id":
        candidate["step_id"] = "candidate-venv-renamed"
    elif case == "candidate-path":
        candidate["path"] = f"{candidate['path']}-other"
    elif case == "candidate-active-link":
        candidate["active_link"] = f"{plan['roots']['deploy']}/other-venv"
    elif case == "candidate-wheel-sha":
        candidate["wheel_sha256"] = "d" * 64
    elif case == "candidate-desired-sha":
        candidate["desired_sha256"] = "d" * 64
    elif case == "candidate-unlocked":
        candidate["wheelhouse_locked"] = False
    elif case == "accounts-empty":
        plan["accounts"] = []
    elif case == "account-step-missing":
        account = next(step for step in plan["apply_order"] if step["kind"] == "account")
        plan["apply_order"].remove(account)
    elif case == "account-step-mismatch":
        account = next(step for step in plan["apply_order"] if step["kind"] == "account")
        account["uid"] += 1000
    elif case == "repo-identity-extra":
        plan["repo_identity"]["note"] = "INNOCUOUS-UNTRUSTED-METADATA"
    elif case == "repo-identity-query":
        plan["repo_identity"]["remote"] += "?ref=main"
    elif case == "source-repository-unsafe":
        plan["source_repositories"] = ["../escape"]
    elif case == "source-repository-duplicate":
        plan["source_repositories"].append(plan["source_repositories"][0])
    elif case == "repository-step-missing":
        plan["apply_order"].remove(repository)
    elif case == "repository-step-mismatch":
        repository["commit"] = "b" * 40
    elif case == "repository-step-extra":
        shadow = deepcopy(repository)
        shadow["step_id"] = "repository:shadow"
        shadow["slug"] = "shadow"
        shadow["path"] = f"{Path(repository['path']).parent}/shadow"
        shadow["desired_sha256"] = install_core._desired_digest(shadow)
        plan["apply_order"].append(shadow)
    elif case == "provider-manifest-invalid":
        plan["provider_manifest"]["builder"] = ["agy"]
    elif case == "credential-manifest-mismatch":
        plan["required_credentials"] = plan["required_credentials"][:-1]
    else:
        plan["required_credentials"].reverse()

    plan_path = tmp_path / f"cross-field-{case}.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    receipt_path = tmp_path / f"cross-field-{case}" / "receipt.json"

    class MutationSentinelBackend:
        preflight_calls = 0
        apply_calls = 0

        def preflight_facts(self, _plan):
            type(self).preflight_calls += 1
            raise AssertionError("cross-field drift reached backend preflight")

        def apply_step(self, _step):
            type(self).apply_calls += 1
            raise AssertionError("cross-field drift reached backend mutation")

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "LocalInstallBackend", MutationSentinelBackend)
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )

    assert install_cli.main(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--confirm-sha256",
            plan_sha256(plan),
            "--receipt",
            str(receipt_path),
        ]
    ) == 1
    assert not receipt_path.parent.exists()
    assert MutationSentinelBackend.preflight_calls == 0
    assert MutationSentinelBackend.apply_calls == 0


@pytest.mark.parametrize(
    "case",
    [
        "scalar-row",
        "empty-principal",
        "unknown-principal",
        "unknown-provider",
        "duplicate",
        "extra-plaintext",
        "nested-secret",
    ],
)
def test_apply_cli_rejects_invalid_required_credentials_before_receipt_or_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    sentinel = "PLAINTEXT-CREDENTIAL-SENTINEL"
    if case == "scalar-row":
        plan["required_credentials"] = ["builder/codex"]
    elif case == "empty-principal":
        plan["required_credentials"][0]["principal"] = ""
    elif case == "unknown-principal":
        plan["required_credentials"][0]["principal"] = "gate"
    elif case == "unknown-provider":
        plan["required_credentials"][0]["provider"] = "github"
    elif case == "duplicate":
        plan["required_credentials"].append(deepcopy(plan["required_credentials"][0]))
    elif case == "extra-plaintext":
        plan["required_credentials"][0]["note"] = sentinel
    elif case == "nested-secret":
        plan["required_credentials"][0]["metadata"] = {"api_token": sentinel}
    plan_path = tmp_path / f"credentials-{case}.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    receipt_path = tmp_path / f"credentials-{case}" / "receipt.json"

    class MutationSentinelBackend:
        preflight_calls = 0
        apply_calls = 0

        def preflight_facts(self, _plan):
            type(self).preflight_calls += 1
            raise AssertionError("invalid credentials reached backend preflight")

        def apply_step(self, _step):
            type(self).apply_calls += 1
            raise AssertionError("invalid credentials reached backend mutation")

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "LocalInstallBackend", MutationSentinelBackend)
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )

    assert install_cli.main(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--confirm-sha256",
            plan_sha256(plan),
            "--receipt",
            str(receipt_path),
        ]
    ) == 1

    assert sentinel not in capsys.readouterr().err
    assert not receipt_path.parent.exists()
    assert MutationSentinelBackend.preflight_calls == 0
    assert MutationSentinelBackend.apply_calls == 0


@pytest.mark.parametrize(
    ("inventory", "mutation"),
    [
        ("accounts", "extra-key"),
        ("accounts", "duplicate-name"),
        ("service_accounts", "boolean-uid"),
    ],
)
def test_apply_validation_rejects_malformed_account_inventory_rows(
    tmp_path: Path, inventory: str, mutation: str
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    rows = plan[inventory]
    if mutation == "extra-key":
        rows[0]["note"] = "INNOCUOUS-UNTRUSTED-METADATA"
    elif mutation == "duplicate-name":
        rows[-1]["name"] = rows[0]["name"]
    else:
        rows[0]["uid"] = True

    with pytest.raises(InstallPlanError):
        validate_apply_plan(plan, confirm_sha256=plan_sha256(plan))


@pytest.mark.parametrize("failure", ["unconfirmed", "topology"])
def test_apply_cli_rejects_before_creating_any_receipt_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    if failure == "topology":
        missing = f"{plan['roots']['state']}/runtime/codex-home"
        plan["apply_order"] = [
            step for step in plan["apply_order"] if step.get("path") != missing
        ]
    plan_path = tmp_path / f"{failure}-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    receipt_path = tmp_path / f"{failure}-receipt" / "receipt.json"
    confirm = "0" * 64 if failure == "unconfirmed" else plan_sha256(plan)

    class MutationSentinelBackend:
        preflight_calls = 0
        apply_calls = 0

        def preflight_facts(self, _plan):
            type(self).preflight_calls += 1
            raise AssertionError("invalid plan reached backend preflight")

        def apply_step(self, _step):
            type(self).apply_calls += 1
            raise AssertionError("invalid plan reached backend mutation")

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "LocalInstallBackend", MutationSentinelBackend)
    # Permit the old implementation to demonstrate its premature creation in
    # this rootless test.  The fixed CLI never reaches receipt persistence.
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )

    assert install_cli.main(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--confirm-sha256",
            confirm,
            "--receipt",
            str(receipt_path),
        ]
    ) == 1

    assert not receipt_path.parent.exists()
    assert MutationSentinelBackend.preflight_calls == 0
    assert MutationSentinelBackend.apply_calls == 0


@pytest.mark.parametrize(
    ("nested_key", "nested_value"),
    [
        ("api_token", "PLAINTEXT-SECRET-SENTINEL"),
        ("required_credentials", "PLAINTEXT-SECRET-SENTINEL"),
        (
            "mirror_url",
            "https://example.invalid/tool?access_token=PLAINTEXT-SECRET-SENTINEL",
        ),
    ],
)
def test_apply_cli_rejects_nested_secrets_before_receipt_or_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    nested_key: str,
    nested_value: str,
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    unit = next(iter(plan["generated"]["units"].values()))
    unit["untrusted_extension"] = {nested_key: nested_value}
    plan_path = tmp_path / f"secret-{nested_key}.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    receipt_path = tmp_path / f"secret-{nested_key}" / "receipt.json"

    class MutationSentinelBackend:
        preflight_calls = 0
        apply_calls = 0

        def preflight_facts(self, _plan):
            type(self).preflight_calls += 1
            raise AssertionError("secret-bearing plan reached backend preflight")

        def apply_step(self, _step):
            type(self).apply_calls += 1
            raise AssertionError("secret-bearing plan reached backend mutation")

    monkeypatch.setattr(install_cli, "_require_root", lambda: None)
    monkeypatch.setattr(install_cli, "LocalInstallBackend", MutationSentinelBackend)
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )

    assert install_cli.main(
        [
            "apply",
            "--plan",
            str(plan_path),
            "--confirm-sha256",
            plan_sha256(plan),
            "--receipt",
            str(receipt_path),
        ]
    ) == 1

    error = capsys.readouterr().err
    assert "PLAINTEXT-SECRET-SENTINEL" not in error
    assert not receipt_path.parent.exists()
    assert MutationSentinelBackend.preflight_calls == 0
    assert MutationSentinelBackend.apply_calls == 0


def test_apply_plan_secret_defense_runs_before_receipt_checkpoint_or_preflight(
    tmp_path: Path,
) -> None:
    plan, _doc = _bound_plan_document(tmp_path)
    receipt = new_install_receipt(plan)
    unit = next(iter(plan["generated"]["units"].values()))
    unit["untrusted_extension"] = {
        "nested_password": "PLAINTEXT-SECRET-SENTINEL"
    }
    receipt._document["plan_sha256"] = plan_sha256(plan)
    persist_calls = 0

    def record_persist() -> None:
        nonlocal persist_calls
        persist_calls += 1

    receipt._persist = record_persist  # type: ignore[method-assign]

    class MutationSentinelBackend:
        preflight_calls = 0

        def preflight_facts(self, _plan):
            type(self).preflight_calls += 1
            raise AssertionError("secret-bearing plan reached backend preflight")

    backend = MutationSentinelBackend()

    with pytest.raises(InstallPlanError, match="nested_password") as exc:
        apply_plan(
            plan,
            confirm_sha256=plan_sha256(plan),
            receipt=receipt,
            backend=backend,
        )

    assert "PLAINTEXT-SECRET-SENTINEL" not in str(exc.value)
    assert persist_calls == 0
    assert backend.preflight_calls == 0


@pytest.mark.parametrize("mounted", [False, True])
def test_default_receipt_does_not_populate_the_managed_state_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mounted: bool
) -> None:
    plan, _doc = _plan_document(tmp_path)
    state_root = Path(plan["roots"]["state"])
    if mounted:
        state_root.mkdir(parents=True)
    monkeypatch.setattr(
        install_core, "_validate_receipt_parent", lambda _observed, _path: None
    )
    monkeypatch.setattr(
        install_core, "_validate_receipt_file", lambda _observed, _path: None
    )

    receipt_path = Path(plan["receipt_path"])
    receipt = new_install_receipt(plan, path=receipt_path)

    assert receipt_path.is_file()
    assert not receipt_path.is_relative_to(state_root)
    if mounted:
        assert list(state_root.iterdir()) == []
        state_step = next(
            step for step in plan["apply_order"] if step.get("path") == str(state_root)
        )
        assert install_core._explicit_empty_managed_mount_is_adoptable(
            plan=plan,
            step=state_step,
            installed={
                "is_mountpoint": True,
                "device": 8,
                "inode": 42,
                "children": [],
            },
            receipt=receipt,
        )
    else:
        assert not state_root.exists()


def test_bundle_binding_preserves_repo_container_and_installs_named_leaf(
    tmp_path: Path,
) -> None:
    plan, _doc = _plan_document(tmp_path)
    tools = [
        {
            "name": name,
            "version": configured["version"],
            "shape": "file",
            "resolved_path": str(tmp_path / f"{name}.locked"),
            "sha256": configured["sha256"],
        }
        for name, configured in plan["toolchain_manifest"].items()
    ]
    repository = tmp_path / "paulsha-cortex.bundle"
    repository.write_bytes(b"exact source bundle\n")
    repositories = [{
        "slug": "paulsha-cortex",
        "commit": "a" * 40,
        "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
        "resolved_path": str(repository),
        "sha256": _sha256(repository),
    }]

    bound = bind_bundle_artifacts(
        plan, {"toolchain": tools, "source_repositories": repositories}
    )
    step_ids = [step["step_id"] for step in bound["apply_order"]]
    container_index = step_ids.index("asset:repo-source-tree")
    repository_index = step_ids.index("repository:paulsha-cortex")
    repository_step = bound["apply_order"][repository_index]

    assert repository_index == container_index + 1
    assert repository_step["path"] == f"{plan['roots']['state']}/repos/paulsha-cortex"
    assert repository_step["owner"] == "cortex-manager"
    assert repository_step["group"] == "cortex-manager"

    plan["source_repositories"] = ["different-repository"]
    with pytest.raises(InstallPlanError, match="does not match the plan"):
        bind_bundle_artifacts(
            plan, {"toolchain": tools, "source_repositories": repositories}
        )


def test_plan_orders_managed_deploy_root_before_content_addressed_venv(
    tmp_path: Path,
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    plan = build_install_plan(
        config=_safe_config(tmp_path), candidate_wheel=wheel, bundle=bundle
    )
    steps = plan["apply_order"]

    assert [step["kind"] for step in steps[:5]] == ["account"] * 5
    assert {step["name"] for step in steps[:5]} == {
        "cortex-manager",
        "cortex-reviewer-planner",
        "cortex-builder",
        "cortex-gate",
        "cortex-egress",
    }
    venv_index = next(
        index for index, step in enumerate(steps) if step["step_id"] == "candidate-venv"
    )
    deploy_root_index = next(
        index
        for index, step in enumerate(steps)
        if step["step_id"] == f"scaffold:{plan['roots']['deploy']}"
    )
    venv = steps[venv_index]
    wheel_sha = _sha256(wheel)
    assert deploy_root_index < venv_index
    assert venv["kind"] == "venv"
    assert venv["path"].endswith(f"/venvs/{wheel_sha}")
    assert venv["active_link"].endswith("/opt/cortex/venv")
    assert venv["active_link"] not in {row["path"] for row in plan["scaffolds"]}
    assert venv["wheel_source"] == str(wheel.absolute())
    assert venv["wheelhouse"] == [
        {"source": str(wheel.absolute()), "sha256": wheel_sha}
    ]
    assert venv_index < next(
        index for index, step in enumerate(steps) if step.get("action") == "daemon-reload"
    )
    assert steps[-4]["action"] == "daemon-reload"
    assert [step["unit"] for step in steps[-3:]] == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]


def test_manager_gitconfig_in_plan_delivers_the_gh_credential_helper_763(
    tmp_path: Path,
) -> None:
    """#763: Manager owns Git operations, so its installed config must deliver auth."""
    _plan, doc = _plan_document(tmp_path)
    manager = doc["generated"]["gitconfigs"]["manager-gitconfig"]
    functional = [
        line.strip()
        for line in manager["content"].splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]
    assert "/usr/bin/gh auth git-credential" in "\n".join(functional)
    assert manager["owner"] == "root"
    assert manager["mode"] == "0644"


@pytest.mark.parametrize("scheme", ["two-way", "three-way", "five-way"])
def test_new_install_rejects_every_non_four_way_scheme(
    tmp_path: Path, scheme: str
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config["scheme"] = scheme

    with pytest.raises(InstallPlanError, match="four-way"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


@pytest.mark.parametrize(
    ("providers", "message"),
    [
        ([], "providers must be an object"),
        (
            {"builder": "codex", "reviewer-planner": ["agy"], "manager": ["github"]},
            "providers.builder must be a list",
        ),
        (
            {"builder": ["codex", 7], "reviewer-planner": ["agy"], "manager": ["github"]},
            "providers.builder entries must be strings",
        ),
        (
            {"builder": ["github"], "reviewer-planner": ["agy"], "manager": ["github"]},
            "provider is not allowed for builder",
        ),
        (
            {"builder": ["codex"], "reviewer-planner": ["agy", "agy"], "manager": ["github"]},
            "providers.reviewer-planner contains a duplicate",
        ),
        (
            {"builder": ["codex"], "reviewer-planner": ["agy"], "manager": []},
            "providers.manager must not be empty",
        ),
    ],
)
def test_plan_rejects_malformed_or_unauthorized_provider_manifests(
    tmp_path: Path, providers: object, message: str
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config["providers"] = deepcopy(providers)

    with pytest.raises(InstallPlanError, match=message):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_plan_provider_manifest_accepts_only_allowlisted_four_way_pairs(
    tmp_path: Path,
) -> None:
    config = _safe_config(tmp_path)
    config["providers"] = {
        "builder": ["codex"],
        "reviewer-planner": ["codex", "agy", "copilot"],
        "manager": ["github"],
    }

    plan, document = _plan_document(tmp_path, config)

    assert document["provider_manifest"] == config["providers"]
    assert plan["required_credentials"] == [
        {"principal": "builder", "provider": "codex"},
        {"principal": "manager", "provider": "github"},
        {"principal": "reviewer-planner", "provider": "codex"},
        {"principal": "reviewer-planner", "provider": "agy"},
        {"principal": "reviewer-planner", "provider": "copilot"},
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("github_token", "test-secret-value"),
        ("password", "do-not-render-this"),
        ("operator_home", "/srv/example-operator-home"),
        ("credential_bytes", "do-not-render-this"),
    ],
)
def test_plan_rejects_secret_like_or_home_discovery_fields(
    tmp_path: Path, field: str, value: str
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config[field] = value

    with pytest.raises(InstallPlanError, match=field):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_plan_rejects_nested_api_key_even_under_a_toolchain_entry(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config["toolchain"]["codex"]["api_key"] = "PLAINTEXT-CREDENTIAL"

    with pytest.raises(InstallPlanError, match="api_key"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_plan_rejects_unknown_nested_secret_like_field(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config["toolchain"]["codex"]["session_cookie"] = "PLAINTEXT-CREDENTIAL"

    with pytest.raises(InstallPlanError, match="session_cookie"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


@pytest.mark.parametrize(
    ("layer", "unknown_key", "sentinel"),
    [
        ("top-level", "deployment_note", "INNOCUOUS-TOP-LEVEL-SENTINEL"),
        ("repo", "license", "LICENSE-SENTINEL-MUST-STAY-REDACTED"),
        ("accounts", "description", "INNOCUOUS-ACCOUNT-SENTINEL"),
        (
            "service_accounts",
            "access_code",
            "ACCESS-CODE-SENTINEL-MUST-STAY-REDACTED",
        ),
        ("roots", "backup", "INNOCUOUS-ROOT-SENTINEL"),
        ("providers", "manager_note", "INNOCUOUS-PROVIDER-SENTINEL"),
        ("toolchain", "sig", "SIG-SENTINEL-MUST-STAY-REDACTED"),
    ],
)
def test_plan_rejects_unknown_keys_at_every_config_layer_without_value_disclosure(
    tmp_path: Path, layer: str, unknown_key: str, sentinel: str
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    if layer == "top-level":
        target = config
    elif layer == "repo":
        target = config["repo_identity"]
    elif layer == "accounts":
        target = config["accounts"]["cortex-manager"]
    elif layer == "service_accounts":
        target = config["service_accounts"]["cortex-egress"]
    elif layer == "roots":
        target = config["roots"]
    elif layer == "providers":
        target = config["providers"]
    else:
        target = config["toolchain"]["codex"]
    target[unknown_key] = sentinel

    with pytest.raises(InstallPlanError, match=unknown_key) as exc:
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)

    assert sentinel not in str(exc.value)


@pytest.mark.parametrize(
    "remote",
    [
        "https://oauth-user:plaintext-credential@example.invalid/repo.git",
        "https://example.invalid/repo.git?access_token=plaintext-credential",
        "https://example.invalid/repo.git#access_token=plaintext-credential",
        "https://example.invalid/repo.git?ref=main",
        "https://example.invalid/repo.git#readme",
    ],
)
def test_plan_rejects_credential_bearing_repository_url(
    tmp_path: Path, remote: str
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config["repo_identity"]["remote"] = remote

    with pytest.raises(InstallPlanError, match="repo_identity.remote") as exc:
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)
    assert "plaintext-credential" not in str(exc.value)


def test_plan_cli_persists_plan_with_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    output = tmp_path / "install-plan.json"
    candidate_sha = "a" * 40
    wheel_sha = _sha256(wheel)
    config = {"repo_identity": {"commit": candidate_sha}}
    plan = {
        "schema_version": 1,
        "candidate": {},
        "apply_order": [{"kind": "venv"}],
    }
    captured: dict[str, object] = {}

    monkeypatch.setattr(install_cli, "_load_mapping", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        install_cli,
        "validate_bundle_manifest",
        lambda _path: {
            "candidate_sha": candidate_sha,
            "manifest_path": str(bundle),
            "wheel": {
                "path": wheel.name,
                "resolved_path": str(wheel),
                "sha256": wheel_sha,
            },
            "wheelhouse": [
                {
                    "path": wheel.name,
                    "resolved_path": str(wheel),
                    "sha256": wheel_sha,
                }
            ],
            "generated_artifacts": [],
        },
    )
    monkeypatch.setattr(install_cli, "build_install_plan", lambda **_kwargs: deepcopy(plan))
    monkeypatch.setattr(install_cli, "bind_bundle_artifacts", lambda value, _manifest: value)

    def capture(path, value, *, mode):
        captured.update({"path": path, "value": value, "mode": mode})

    monkeypatch.setattr(install_cli, "atomic_write_json", capture)

    assert install_cli.main(
        ["plan", "--config", "config.yml", "--bundle", str(bundle), "--output", str(output)]
    ) == 0
    assert captured["path"] == output.absolute()
    assert captured["mode"] == 0o600


def test_plan_document_never_contains_secret_bytes_or_operator_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/srv/sentinel-must-not-be-discovered")
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-from-parent-env")
    plan, _doc = _plan_document(tmp_path)
    rendered = canonical_plan_bytes(plan).decode("utf-8")

    assert "sentinel-must-not-be-discovered" not in rendered
    assert "test-secret-from-parent-env" not in rendered


def test_lexical_path_escape_is_rejected_before_plan_is_emitted(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    roots = dict(config["roots"])
    roots["state"] = str(tmp_path / "target/var/lib/cortex/../../../../escape")
    config["roots"] = roots

    with pytest.raises(UnsafeInstallPathError, match="state"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_symlinked_install_parent_is_rejected(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "target-link"
    link.symlink_to(outside, target_is_directory=True)
    config = _safe_config(tmp_path)
    roots = dict(config["roots"])
    roots["deploy"] = str(link / "opt/cortex")
    config["roots"] = roots

    with pytest.raises(UnsafeInstallPathError, match="symlink"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_candidate_bundle_symlink_is_rejected_before_content_is_consumed(
    tmp_path: Path,
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    bundle_link = tmp_path / "candidate.bundle"
    bundle_link.symlink_to(bundle)

    with pytest.raises(UnsafeInstallPathError, match="symlink"):
        build_install_plan(
            config=_safe_config(tmp_path),
            candidate_wheel=wheel,
            bundle=bundle_link,
        )
