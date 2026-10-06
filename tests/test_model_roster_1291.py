"""#1291 T3: packaged roster must cover supported system-deploy builder pins."""

from __future__ import annotations

from pathlib import Path

from paulsha_cortex.coordinator.model_identities import load_model_identities


def test_packaged_roster_includes_supported_system_deploy_builder_identities(
    tmp_path: Path,
) -> None:
    registry = load_model_identities(tmp_path, use_packaged_default=True)
    available = {
        (identity.executor, identity.model_id): identity
        for identity in registry.identities
    }
    expected = {
        ("agy", "gemini-3.8-flash-high"),
        ("codex", "gpt-6-luna"),
        ("copilot", "gpt-5.4-mini"),
    }

    missing = sorted(
        f"{executor}/{model_id}"
        for executor, model_id in expected - set(available)
    )
    assert not missing, (
        "packaged roster missing supported system-deploy builder identities: "
        + ", ".join(missing)
    )

    for executor, model_id in sorted(expected):
        assert "build" in available[(executor, model_id)].capabilities
