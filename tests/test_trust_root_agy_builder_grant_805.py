"""#805 RED coverage for the Trust Root AGY builder contract.

An overlay may declare a build-capable identity without changing the packaged
roster.  That declaration is not sufficient to launch a builder: the effective
launcher, toolchain, and credential layers must all agree on the same
principal/executor pair.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

import pytest

from paulsha_cortex.coordinator import model_resolution
from paulsha_cortex.coordinator.model_identities import IdentityRegistry, ModelIdentity
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
    """Call the future shared seam while keeping this RED failure actionable.

    The implementation card must provide this one validator in
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
