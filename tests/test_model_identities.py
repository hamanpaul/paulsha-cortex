from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator.model_identities import (
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    load_model_identities,
    probe_agy_capability,
    select_secondary_planner,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return type(
        "Completed",
        (),
        {"returncode": returncode, "stdout": stdout, "stderr": stderr},
    )()


def test_packaged_v2_registry_has_google_agy_default() -> None:
    registry = load_model_identities(use_packaged_default=True)

    agy = registry.require("agy", AGY_MODEL_ID)
    assert registry.schema_version == 2
    assert agy.independence_domain == "google"
    assert agy.capabilities == ("planning",)
    assert agy.live_probe == "agy-plan-sandbox"


def test_packaged_default_is_composed_with_existing_v1_primary_identities(tmp_path: Path) -> None:
    (tmp_path / "model-identities.yaml").write_text(
        """\
schema_version: 1
identities:
  - executor: codex
    model_id: gpt-primary
    independence_domain: openai
""",
        encoding="utf-8",
    )

    registry = load_model_identities(tmp_path, use_packaged_default=True)

    assert registry.schema_version == 2
    assert registry.require("agy", AGY_MODEL_ID).independence_domain == "google"
    assert registry.require("codex", "gpt-primary").independence_domain == "openai"


def test_v2_registry_is_strict_and_rejects_unknown_or_duplicate_rows(tmp_path: Path) -> None:
    path = tmp_path / "model-identities.yaml"
    path.write_text(
        """\
schema_version: 2
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
    unexpected: no
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected"):
        load_model_identities(tmp_path)

    path.write_text(
        """\
schema_version: 2
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate identity"):
        load_model_identities(tmp_path)

    path.write_text(
        """\
schema_version: 2
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="agy-plan-sandbox"):
        load_model_identities(tmp_path)

    path.write_text("schema_version: true\nidentities: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_model_identities(tmp_path)


def test_foreign_review_uses_v2_registry_without_leaking_planner_metadata(tmp_path: Path) -> None:
    from paulsha_cortex.coordinator import review

    (tmp_path / "model-identities.yaml").write_text(
        """\
schema_version: 2
identities:
  - executor: agy
    model_id: Gemini 3.1 Pro (High)
    independence_domain: google
    capabilities: [planning]
    live_probe: agy-plan-sandbox
""",
        encoding="utf-8",
    )

    assert review.load_model_identity_registry(tmp_path) == {
        ("agy", AGY_MODEL_ID): {
            "executor": "agy",
            "model_id": AGY_MODEL_ID,
            "independence_domain": "google",
        }
    }


def test_agy_probe_requires_model_listing_and_safe_headless_smoke() -> None:
    calls: list[dict] = []

    def runner(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if argv == ["agy", "models"]:
            return _completed(stdout=f"Gemini 3.1 Pro (Low)\n{AGY_MODEL_ID}\n")
        return _completed(
            stdout='{"capability":"cortex-plan-sandbox","model":"Gemini 3.1 Pro (High)"}\n'
        )

    probe = probe_agy_capability(runner=runner, timeout_seconds=11)

    assert probe.ready is True
    assert probe.identity == ("agy", AGY_MODEL_ID, "google")
    assert calls[1]["argv"][:2] == ["agy", "--print"]
    assert calls[1]["argv"][calls[1]["argv"].index("--mode") + 1] == "plan"
    assert "--sandbox" in calls[1]["argv"]
    assert "--dangerously-skip-permissions" not in calls[1]["argv"]
    assert calls[1]["shell"] is False
    assert calls[1]["timeout"] == 11


@pytest.mark.parametrize(
    ("model_stdout", "smoke_result", "reason"),
    [
        ("Gemini 3.1 Pro (Low)\n", _completed(), "model-not-listed"),
        (
            f"{AGY_MODEL_ID}\n",
            _completed(returncode=2, stderr="unsupported flag"),
            "smoke-failed",
        ),
        (f"{AGY_MODEL_ID}\n", _completed(stdout="not-json"), "malformed-output"),
        (
            f"{AGY_MODEL_ID}\n",
            _completed(stdout='{"capability":"wrong","model":"Gemini 3.1 Pro (High)"}'),
            "identity-mismatch",
        ),
    ],
)
def test_agy_probe_fails_closed_on_drift(model_stdout, smoke_result, reason) -> None:
    responses = iter([_completed(stdout=model_stdout), smoke_result])
    probe = probe_agy_capability(runner=lambda *args, **kwargs: next(responses))

    assert probe.ready is False
    assert probe.reason == reason


def test_secondary_selection_uses_priority_and_excludes_primary_domain() -> None:
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "agy",
                "model_id": AGY_MODEL_ID,
                "independence_domain": "google",
                "capabilities": ["planning"],
                "live_probe": "agy-plan-sandbox",
            },
            {
                "executor": "claude",
                "model_id": "claude-sonnet-4.6",
                "independence_domain": "anthropic",
                "capabilities": ["planning"],
            },
            {
                "executor": "codex",
                "model_id": "gpt-5.4",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
        ]
    )
    probes = {
        ("agy", AGY_MODEL_ID): CapabilityProbe.ready_for("agy", AGY_MODEL_ID, "google"),
        ("claude", "claude-sonnet-4.6"): CapabilityProbe.ready_for(
            "claude", "claude-sonnet-4.6", "anthropic"
        ),
        ("codex", "gpt-5.4"): CapabilityProbe.ready_for("codex", "gpt-5.4", "openai"),
    }

    selected = select_secondary_planner(
        registry=registry,
        primary=("codex", "gpt-5.4"),
        probes=probes,
    )
    assert selected.state == "ready"
    assert selected.identity and selected.identity.executor == "agy"

    selected = select_secondary_planner(
        registry=registry,
        primary=("agy", AGY_MODEL_ID),
        probes=probes,
    )
    assert selected.identity and selected.identity.executor == "claude"

    probes[("agy", AGY_MODEL_ID)] = CapabilityProbe(
        False,
        "agy",
        AGY_MODEL_ID,
        "google",
        "smoke-failed",
    )
    selected = select_secondary_planner(
        registry=registry,
        primary=("codex", "gpt-5.4"),
        probes=probes,
    )
    assert selected.identity and selected.identity.executor == "claude"


def test_secondary_selection_fails_closed_for_unknown_or_same_domain_only() -> None:
    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "primary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "codex",
                "model_id": "secondary",
                "independence_domain": "openai",
                "capabilities": ["planning"],
            },
        ]
    )
    probes = {
        ("codex", "secondary"): CapabilityProbe.ready_for("codex", "secondary", "openai")
    }

    unknown = select_secondary_planner(
        registry=registry,
        primary=("codex", "unknown"),
        probes=probes,
    )
    assert (unknown.state, unknown.reason) == ("needs_human", "primary-identity-unknown")

    same = select_secondary_planner(
        registry=registry,
        primary=("codex", "primary"),
        probes=probes,
    )
    assert (same.state, same.reason) == ("needs_human", "no-heterogeneous-planner")
