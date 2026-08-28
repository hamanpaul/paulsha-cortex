"""#805 regression coverage for the Trust Root AGY builder contract.

An overlay may declare a build-capable identity without changing the packaged
roster.  That declaration is not sufficient to launch a builder: the effective
launcher, toolchain, and credential layers must all agree on the same
principal/executor pair.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

import pytest

from paulsha_cortex.coordinator import model_resolution
from paulsha_cortex.coordinator.model_identities import (
    AGY_MODEL_ID,
    IdentityRegistry,
    ModelIdentity,
    load_model_identities,
)
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.permgen import CredentialShape


@dataclass(frozen=True)
class BuilderContractFixture:
    """The three cross-layer facts required by an effective builder identity."""

    launcher_profile: Mapping[str, object] | None
    toolchain_grant: Mapping[str, object] | None
    credential_grant: Mapping[str, object] | None


@pytest.fixture
def declared_agy_builder_identity() -> ModelIdentity:
    """An operator overlay declaration, not a packaged fallback identity."""

    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "agy",
                "model_id": "gemini-3.7-flash-high",
                "independence_domain": "google",
                "capabilities": ["build"],
            }
        ]
    )
    return identities.require("agy", "gemini-3.7-flash-high")


@pytest.fixture
def complete_builder_contract() -> BuilderContractFixture:
    """A complete contract, with exactly one layer removed per RED case."""

    return BuilderContractFixture(
        launcher_profile={
            "executor": "agy",
            "persona": "builder",
            "mode": "accept-edits",
            "requires_worktree": True,
            "commit_required": True,
        },
        toolchain_grant={
            "principal": "builder",
            "executor": "agy",
            "asset_id": "executor-toolchain",
            "executable": True,
        },
        credential_grant={
            "principal": "builder",
            "executor": "agy",
            "shape": CredentialShape.HOME_REDIRECT_TREE,
        },
    )


@pytest.fixture(
    params=(
        ("launcher profile", "launcher_profile"),
        ("builder toolchain grant", "toolchain_grant"),
        ("builder credential grant", "credential_grant"),
    ),
    ids=lambda item: item[0].replace(" ", "-"),
)
def missing_builder_layer(request, complete_builder_contract: BuilderContractFixture):
    missing_layer, field_name = request.param
    return missing_layer, replace(complete_builder_contract, **{field_name: None})


def _validate_builder_contract(
    *,
    identity: ModelIdentity,
    contract: BuilderContractFixture,
    missing_layer: str,
) -> None:
    """Call the shared seam while keeping each missing layer actionable.

    The implementation must provide this one validator in
    ``model_resolution`` and reuse it from model resolution, doctor, and
    dispatch preflight.  Until then, fail the individual case with the layer
    name instead of turning the whole module into an opaque import error.
    """

    checker = getattr(model_resolution, "validate_persona_executor_compatibility", None)
    if checker is None:
        pytest.fail(
            f"missing layer {missing_layer}: shared persona/executor compatibility "
            "validator is not implemented"
        )
    checker(
        persona="builder",
        identity=identity,
        launcher_profile=contract.launcher_profile,
        toolchain_grant=contract.toolchain_grant,
        credential_grant=contract.credential_grant,
    )


def test_declared_agy_builder_identity_rejects_each_missing_trust_root_layer(
    declared_agy_builder_identity: ModelIdentity,
    missing_builder_layer,
) -> None:
    missing_layer, contract = missing_builder_layer

    with pytest.raises(ValueError, match=missing_layer):
        _validate_builder_contract(
            identity=declared_agy_builder_identity,
            contract=contract,
            missing_layer=missing_layer,
        )


def test_complete_builder_contract_is_accepted(
    declared_agy_builder_identity: ModelIdentity,
    complete_builder_contract: BuilderContractFixture,
) -> None:
    _validate_builder_contract(
        identity=declared_agy_builder_identity,
        contract=complete_builder_contract,
        missing_layer="none",
    )


def test_agy_builder_launcher_dependency_is_explicit_until_writable_support_lands() -> None:
    """#805 owns the grant; #799/#806 owns the writable AGY argv contract."""

    assert model_resolution.launcher_profile_for("builder", "agy") is None
    codex = model_resolution.launcher_profile_for("builder", "codex")
    assert codex is not None
    assert codex["requires_worktree"] is True
    assert codex["commit_required"] is True


def test_builder_agy_state_is_a_separate_trust_root_asset() -> None:
    credential = permgen.credential_for("builder", "agy")
    assert credential.shape is CredentialShape.HOME_REDIRECT_TREE
    assert "builder-agy-state" in permgen.credential_asset_ids()

    asset = registry.asset_by_id("builder-agy-state")
    assert asset.writers == (registry.Principal.INSTALLER,)
    assert asset.readers == (registry.Principal.BUILDER,)
    assert asset.ingress_kind is registry.IngressKind.DEPLOYMENT_WRITE
    assert asset.derived_in == (
        "trust_root/permgen.py:PathLayout.executor_credential_of",
    )


def test_planner_and_reviewer_contracts_remain_valid(tmp_path: Path) -> None:
    identities = load_model_identities(tmp_path)

    model_resolution.validate_identity_compatibility(
        "planner", identities.require("agy", AGY_MODEL_ID)
    )
    model_resolution.validate_identity_compatibility(
        "reviewer", identities.require("claude", "sonnet")
    )


def test_packaged_roster_does_not_advertise_agy_build(tmp_path: Path) -> None:
    identities = load_model_identities(tmp_path)
    agy = identities.require("agy", AGY_MODEL_ID)

    assert "build" not in agy.capabilities
    assert not any(
        identity.executor == "agy" and "build" in identity.capabilities
        for identity in identities.identities
        if identity.origin == model_resolution.IDENTITY_ORIGIN_PACKAGED
    )


def test_doctor_rejects_an_incomplete_agy_builder_overlay(tmp_path: Path) -> None:
    from paulsha_cortex.doctor import _model_resolution_probe

    (tmp_path / "model-identities.yaml").write_text(
        """\
schema_version: 3
identities:
  - executor: agy
    model_id: gemini-3.7-flash-high
    independence_domain: google
    capabilities: [build]
""",
        encoding="utf-8",
    )

    result = _model_resolution_probe(
        {"PSC_PROJECT_CONFIG_ROOT": str(tmp_path)}, tmp_path
    )

    assert result.status == "fail"
    assert "builder" in result.detail
    assert "missing builder launcher profile" in result.detail


def test_builder_job_env_is_role_scoped_and_drops_provider_credentials(
    tmp_path: Path,
) -> None:
    from _home_paths import BUILDER_HOME, fake_account_ids

    from paulsha_cortex.coordinator import job_runner

    manager_env = {
        job_runner.BUILDER_PATH_ENV: "/opt/cortex/toolchain/bin:/usr/bin:/bin",
        job_runner.BUILDER_HOME_ENV: BUILDER_HOME,
        "PATH": "/manager/venv/bin:/usr/bin",
        "HOME": "/manager-home",
        "OPENAI_API_KEY": "operator-codex-secret",
        "GEMINI_API_KEY": "operator-agy-secret",
        "AGY_OAUTH_TOKEN": "operator-agy-oauth-secret",
        "GH_TOKEN": "operator-github-secret",
    }

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(job_runner, "_account_ids", fake_account_ids)
        env = job_runner.build_job_env(
            manager_env=manager_env,
            job_id="job-805",
            slice_id="slice-805",
            repo_root=str(tmp_path),
            workspace=None,
            role=job_runner.JOB_ROLE_BUILDER,
        )

    assert env["PATH"] == manager_env[job_runner.BUILDER_PATH_ENV]
    assert env["HOME"] == BUILDER_HOME
    assert "PATH" not in manager_env or env["PATH"] != manager_env["PATH"]
    assert "HOME" not in manager_env or env["HOME"] != manager_env["HOME"]
    assert all(job_runner.CREDENTIAL_ENV_RE.search(key) is None for key in env)
