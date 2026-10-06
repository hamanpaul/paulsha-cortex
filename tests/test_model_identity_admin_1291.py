from __future__ import annotations

from pathlib import Path

import yaml

from paulsha_cortex.coordinator.model_identities import load_model_identities
from paulsha_cortex.porcelain.model_profile import main


def _add_identity(root: Path, *extra: str) -> int:
    return main(
        [
            "identity",
            "add",
            "--config-root",
            str(root),
            "--executor",
            "copilot",
            "--model-id",
            "gpt-custom-build",
            "--independence-domain",
            "openai",
            "--capability",
            "build",
            *extra,
        ]
    )


def test_identity_add_creates_valid_overlay_for_system_config_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "system-config"
    root.mkdir()

    assert _add_identity(root) == 0

    overlay = root / "model-identities.yaml"
    assert overlay.stat().st_mode & 0o777 == 0o600
    registry = load_model_identities(root)
    identity = registry.require("copilot", "gpt-custom-build")
    assert identity.capabilities == ("build",)
    assert identity.origin == "operator-overlay"


def test_identity_add_preserves_existing_overlay_policy_and_mode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    overlay = root / "model-identities.yaml"
    overlay.write_text(
        "schema_version: 3\n"
        "resolution_policy:\n"
        "  packaged_fallback: deny\n"
        "identities:\n"
        "  - executor: codex\n"
        "    model_id: gpt-existing\n"
        "    independence_domain: openai\n"
        "    capabilities: [build]\n",
        encoding="utf-8",
    )
    overlay.chmod(0o640)
    owner = overlay.stat().st_uid, overlay.stat().st_gid

    assert _add_identity(root) == 0

    document = yaml.safe_load(overlay.read_text(encoding="utf-8"))
    assert document["resolution_policy"] == {"packaged_fallback": "deny"}
    assert [row["model_id"] for row in document["identities"]] == [
        "gpt-existing",
        "gpt-custom-build",
    ]
    metadata = overlay.stat()
    assert (metadata.st_uid, metadata.st_gid) == owner
    assert metadata.st_mode & 0o777 == 0o640
    registry = load_model_identities(root)
    assert registry.require("codex", "gpt-existing").capabilities == ("build",)


def test_identity_add_rejects_duplicate_without_rewriting_overlay(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    overlay = root / "model-identities.yaml"
    overlay.write_text(
        "schema_version: 3\nidentities:\n"
        "  - executor: copilot\n"
        "    model_id: gpt-custom-build\n"
        "    independence_domain: openai\n"
        "    capabilities: [build]\n",
        encoding="utf-8",
    )
    before = overlay.read_bytes()

    assert _add_identity(root) == 2

    assert overlay.read_bytes() == before
    assert "already exists" in capsys.readouterr().err


def test_identity_add_validation_failure_leaves_overlay_absent(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "config"
    root.mkdir()

    status = main(
        [
            "identity",
            "add",
            "--config-root",
            str(root),
            "--executor",
            "agy",
            "--model-id",
            "gemini-custom",
            "--independence-domain",
            "openai",
            "--capability",
            "planning",
        ]
    )

    assert status == 2
    assert not (root / "model-identities.yaml").exists()
    assert "agy planning requires google" in capsys.readouterr().err


def test_identity_add_refuses_overlay_symlink_without_touching_target(
    tmp_path: Path, capsys
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    target = tmp_path / "operator-overlay.yaml"
    target.write_text("schema_version: 3\nidentities: []\n", encoding="utf-8")
    (root / "model-identities.yaml").symlink_to(target)
    before = target.read_bytes()

    assert _add_identity(root) == 2

    assert target.read_bytes() == before
    assert "single-link regular file" in capsys.readouterr().err
