from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
from paulsha_cortex.coordinator import manager
from paulsha_cortex.deck.schema import DeckSchemaError, validate_foreign_review_tier


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


@pytest.mark.parametrize("tier", ["unknown", "", "null"])
def test_invalid_tier_reports_manifest_and_allowed_values(tmp_path: Path, tier: str) -> None:
    (tmp_path / ".project-policy.yml").write_text(
        f"policy_profile: flat\npolicy_version: 1.0.17\ntier: {tier}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        read_repo_tier(tmp_path)

    diagnostic = str(exc_info.value)
    assert ".project-policy.yml" in diagnostic
    assert "allowed" in diagnostic
    assert all(value in diagnostic for value in ("shareable", "work", "personal"))


def test_missing_manifest_keeps_shareable_default(tmp_path: Path) -> None:
    assert read_repo_tier(tmp_path) == "shareable"


def test_explicit_shareable_tier_is_accepted(tmp_path: Path) -> None:
    _write(tmp_path / ".project-policy.yml", tier="shareable")
    assert read_repo_tier(tmp_path) == "shareable"


def test_deck_tier_surface_uses_same_fail_closed_contract(tmp_path: Path) -> None:
    (tmp_path / ".project-policy.yml").write_text(
        "policy_profile: flat\npolicy_version: 1.0.17\n", encoding="utf-8"
    )
    with pytest.raises(DeckSchemaError, match=r"\.project-policy\.yml"):
        validate_foreign_review_tier(tmp_path)


def test_slice_tick_rejects_missing_tier_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slice lane must fail closed before autonomy records a builder job."""
    (tmp_path / ".project-policy.yml").write_text(
        "policy_profile: flat\npolicy_version: 1.0.17\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(manager.autonomy, "_infer_repo_root", lambda _path: tmp_path)
    dispatched = False

    def unexpected_dispatch(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("builder dispatch must not be reached")

    monkeypatch.setattr(manager.autonomy, "dispatch_ready", unexpected_dispatch)

    class Registry:
        def list_jobs(self):
            return []

        def list_slices(self):
            return []

        def update_slice(self, slice_id, **kwargs):
            assert slice_id == "slice-492"
            assert kwargs == {"state": "needs_human", "gate_state": "needs_human"}

    dispatcher = SimpleNamespace(_registry=Registry(), _git_runner=None)
    result = manager.run_tick(
        dispatcher,
        metas=[{"slice_id": "slice-492", "path": str(tmp_path / "spec.md"), "dispatch": "auto", "plan": "plan.md", "depends_on": []}],
        launcher=object(),
        is_satisfied=lambda _slice_id: True,
        handoff_dir=str(tmp_path / "handoff"),
    )

    assert dispatched is False
    assert result["dispatched"] == []
    assert result["needs_human"][0]["slice_id"] == "slice-492"
    assert "allowed values" in result["needs_human"][0]["gate_reason"]


def test_valid_tier_preflight_does_not_create_needs_human_candidate_state(
    tmp_path: Path,
) -> None:
    """A passing pre-build check must leave candidate dispatch untouched."""
    _write(tmp_path / ".project-policy.yml", tier="shareable")
    updates: list[dict[str, object]] = []

    class Registry:
        def _manager_update_workflow_run(self, run_id: str, **kwargs: object) -> object:
            updates.append({"run_id": run_id, **kwargs})
            return SimpleNamespace(
                run_id=run_id,
                current_phase="build",
            )

    dispatcher = SimpleNamespace(_registry=Registry())
    run = SimpleNamespace(
        run_id="run-492",
        work_id="work-492",
        workspace_root=str(tmp_path),
        facets=(),
    )
    step = SimpleNamespace(phase="build")

    assert manager._validate_foreign_review_policy_before_build(
        dispatcher,
        run=run,
        step=step,
    ) is None
    assert updates == []


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
