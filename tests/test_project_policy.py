from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator.review import read_repo_tier
from paulsha_cortex.monitor.scanner import (
    ProjectClassification,
    classify_project_detailed,
)
from paulsha_cortex.project_policy import (
    CANONICAL_NAME,
    LEGACY_NAME,
    ProjectPolicyError,
    resolve_project_policy,
)


def _write(path: Path, *, tier: str = "shareable") -> None:
    path.write_text(
        f"policy_profile: flat\npolicy_version: 1.0.14\ntier: {tier}\n",
        encoding="utf-8",
    )


def test_canonical_manifest_is_preferred_and_drives_tier(tmp_path: Path) -> None:
    _write(tmp_path / ".project-policy.yml", tier="personal")

    resolution = resolve_project_policy(tmp_path)

    assert resolution.path == tmp_path / ".project-policy.yml"
    assert not resolution.legacy_only
    assert read_repo_tier(tmp_path) == "personal"
    assert classify_project_detailed(tmp_path) == (
        ProjectClassification.TRACKED,
        None,
    )


def test_legacy_only_manifest_remains_compatible(tmp_path: Path) -> None:
    _write(tmp_path / LEGACY_NAME, tier="work")

    resolution = resolve_project_policy(tmp_path)

    assert resolution.legacy_only
    assert read_repo_tier(tmp_path) == "work"
    assert classify_project_detailed(tmp_path) == (
        ProjectClassification.TRACKED,
        None,
    )


def test_dual_identical_manifest_uses_canonical(tmp_path: Path) -> None:
    _write(tmp_path / ".project-policy.yml")
    (tmp_path / LEGACY_NAME).write_text(
        '# legacy syntax differs but parsed policy is identical\n'
        'tier: "shareable"\n'
        'policy_version: "1.0.14"\n'
        'policy_profile: flat\n',
        encoding="utf-8",
    )

    resolution = resolve_project_policy(tmp_path)

    assert resolution.dual_identical
    assert resolution.path == tmp_path / ".project-policy.yml"


def test_dual_conflict_fails_closed_for_review_and_monitor(tmp_path: Path) -> None:
    _write(tmp_path / ".project-policy.yml", tier="shareable")
    _write(tmp_path / LEGACY_NAME, tier="personal")

    with pytest.raises(ProjectPolicyError, match="different YAML semantics"):
        resolve_project_policy(tmp_path)
    with pytest.raises(ValueError, match="different YAML semantics"):
        read_repo_tier(tmp_path)
    classification, diagnostic = classify_project_detailed(tmp_path)
    assert classification == ProjectClassification.LEGACY
    assert diagnostic is not None
    assert diagnostic.startswith("degraded:")
    assert "different YAML semantics" in diagnostic


def test_malformed_canonical_manifest_fails_closed(tmp_path: Path) -> None:
    (tmp_path / ".project-policy.yml").write_text(
        "tier: [unterminated\n",
        encoding="utf-8",
    )

    with pytest.raises(ProjectPolicyError, match="unreadable"):
        resolve_project_policy(tmp_path)


def test_canonical_manifest_without_tier_reports_actionable_review_diagnostic(
    tmp_path: Path,
) -> None:
    """#492：缺 tier 要在 foreign review 前以可操作訊息 fail closed。"""
    (tmp_path / ".project-policy.yml").write_text(
        "policy_profile: flat\npolicy_version: 1.0.17\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        read_repo_tier(tmp_path)

    diagnostic = str(exc_info.value)
    assert ".project-policy.yml" in diagnostic
    assert "shareable" in diagnostic
    assert "work" in diagnostic
    assert "personal" in diagnostic


@pytest.mark.parametrize("manifest_name", [CANONICAL_NAME, LEGACY_NAME])
def test_symlinked_manifest_is_rejected_fail_closed(
    tmp_path: Path,
    manifest_name: str,
) -> None:
    outside = tmp_path / "outside.yml"
    _write(outside)
    (tmp_path / manifest_name).symlink_to(outside)

    with pytest.raises(ProjectPolicyError, match="must not be a symlink"):
        resolve_project_policy(tmp_path)
