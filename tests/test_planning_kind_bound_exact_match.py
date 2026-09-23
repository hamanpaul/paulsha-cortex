from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.workflow import GateEvidenceRef, PlanningArtifactAuthority
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    load_cards,
    load_combo,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "a" * 64
WORK_ID = "feat-work-gc"
ANCHOR_WORK_ID = "workflow-execution-identity-producer"
ANCHOR_SLUG = "cortex-refine-complete"


def _compiled_manifest(combo_name: str, *, task_slug: str, change: str | None = None):
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / f"{combo_name}.yaml", cards)
    result = compile_combo(
        combo,
        cards,
        task_slug,
        change=change or task_slug,
        allow_external=True,
        repo_root=REPO_ROOT,
    )
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _manifest_outputs(
    combo_name: str, *, task_slug: str, change: str | None = None
) -> tuple[str, ...]:
    manifest = _compiled_manifest(combo_name, task_slug=task_slug, change=change)
    return tuple(output for step in manifest.steps for output in step.outputs)


def _artifact_content(kind: str) -> str:
    if kind == "spec":
        return (
            "---\nstatus: accepted\n---\n# Spec\n\n"
            "## Requirements\n\nBind exact stems only.\n"
        )
    if kind == "design":
        return (
            "---\nstatus: accepted\n---\n# Design\n\n"
            "## Decisions\n\nKeep docs paths exact.\n"
        )
    if kind == "plan":
        return (
            "---\nstatus: accepted\n---\n# Plan\n\n"
            "## Task 1\n\nShip the exact binding.\n"
        )
    return "# Notes\n\nThis should never pass publication.\n"


def _artifact_row(kind: str, path: str) -> dict[str, str]:
    return {"kind": kind, "path": path, "content": _artifact_content(kind)}


def _write_artifact(root: Path, *, kind: str, ref: str) -> dict[str, str]:
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_artifact_content(kind), encoding="utf-8")
    return {"kind": kind, "ref": ref, "sha256": manager._sha256_path(path)}


def _write_brainstorm_evidence(
    coordinator_root: Path,
    *,
    repo: str,
    work_id: str,
    artifacts: list[dict[str, str]],
) -> Path:
    evidence = coordinator_root / "evidence" / "planning" / "brainstorm.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "brainstorm-peer",
                "scope": {
                    "repo": repo,
                    "work_id": work_id,
                    "source_revision": SOURCE_REVISION,
                },
                "artifacts": artifacts,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return evidence


@pytest.mark.parametrize(
    ("kind", "path"),
    (
        ("spec", f"docs/superpowers/specs/{WORK_ID}-v2-spec.md"),
        ("design", f"docs/superpowers/specs/{WORK_ID}-v2-design.md"),
        ("plan", f"docs/superpowers/plans/{WORK_ID}-v2.md"),
        ("plan", f"docs/superpowers/plans/{WORK_ID}-v2-plan.md"),
        ("spec", f"docs/superpowers/specs/evil-{WORK_ID}-spec.md"),
        ("spec", f"docs/superpowers/specs/x-{WORK_ID}-y-spec.md"),
        ("spec", f"docs/superpowers/specs/{WORK_ID}-extra-spec.md"),
    ),
)
def test_planning_kind_bound_rejects_prefix_suffix_middle_and_v2_destinations(
    kind: str, path: str
) -> None:
    assert manager.planning_kind_bound(kind, path, WORK_ID) is False


@pytest.mark.parametrize(
    ("kind", "path"),
    (
        ("spec", f"docs/superpowers/specs/{WORK_ID}-spec.md"),
        ("design", f"docs/superpowers/specs/{WORK_ID}-design.md"),
        ("spec", f"docs/superpowers/specs/2026-08-27-{WORK_ID}-spec.md"),
        ("design", f"docs/superpowers/specs/2026-08-27-{WORK_ID}-design.md"),
        ("plan", f"docs/superpowers/plans/{WORK_ID}.md"),
        ("plan", f"docs/superpowers/plans/2026-08-27-{WORK_ID}.md"),
        ("plan", f"docs/superpowers/plans/{WORK_ID}-plan.md"),
    ),
)
def test_planning_kind_bound_keeps_canonical_dated_and_plan_alias_destinations(
    kind: str, path: str
) -> None:
    assert manager.planning_kind_bound(kind, path, WORK_ID) is True


def test_planning_kind_bound_preserves_demo_plan_aliases() -> None:
    assert (
        manager.planning_kind_bound(
            "plan",
            "docs/superpowers/plans/demo-plan.md",
            "demo-plan",
        )
        is True
    )
    assert (
        manager.planning_kind_bound(
            "plan",
            "docs/superpowers/plans/demo-plan-plan.md",
            "demo-plan",
        )
        is True
    )
    assert (
        manager.planning_kind_bound(
            "plan",
            "docs/superpowers/plans/demo.md",
            "demo-plan",
        )
        is False
    )


@pytest.mark.parametrize("combo_name", ("fix-standard", "small-fix", "feature-oneshot"))
def test_publish_rejects_glob_only_docs_destinations(
    tmp_path: Path, combo_name: str
) -> None:
    rows = [
        _artifact_row("spec", f"docs/superpowers/specs/{WORK_ID}-v2-spec.md"),
        _artifact_row("design", f"docs/superpowers/specs/{WORK_ID}-v2-design.md"),
        _artifact_row("plan", f"docs/superpowers/plans/{WORK_ID}-v2.md"),
        _artifact_row("plan", f"docs/superpowers/plans/{WORK_ID}-v2-plan.md"),
        _artifact_row("spec", f"docs/superpowers/specs/evil-{WORK_ID}-spec.md"),
        _artifact_row("spec", f"docs/superpowers/specs/x-{WORK_ID}-y-spec.md"),
        _artifact_row("spec", f"docs/superpowers/specs/{WORK_ID}-extra-spec.md"),
    ]

    for row in rows:
        with pytest.raises(ValueError, match="outside governed roots"):
            manager._publish_planning_artifacts(
                str(tmp_path),
                [row],
                work_id=WORK_ID,
                allowed_refs=_manifest_outputs(combo_name, task_slug=WORK_ID),
            )
        assert not (tmp_path / row["path"]).exists()


@pytest.mark.parametrize("combo_name", ("small-fix", "feature-oneshot"))
def test_publish_rejects_cross_kind_and_unknown_docs_kinds(
    tmp_path: Path, combo_name: str
) -> None:
    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path),
            [
                _artifact_row(
                    "spec",
                    f"docs/superpowers/specs/{WORK_ID}-design.md",
                )
            ],
            work_id=WORK_ID,
            allowed_refs=_manifest_outputs(combo_name, task_slug=WORK_ID),
        )

    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(tmp_path),
            [
                _artifact_row(
                    "notes",
                    f"docs/superpowers/specs/{WORK_ID}-spec.md",
                )
            ],
            work_id=WORK_ID,
            allowed_refs=_manifest_outputs(combo_name, task_slug=WORK_ID),
        )


def test_fix_standard_anchor_triplet_requires_anchor_slugs_and_revalidates(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    coordinator_root = tmp_path / "coordinator"
    rows = [
        _artifact_row("spec", f"docs/superpowers/specs/{ANCHOR_SLUG}-spec.md"),
        _artifact_row("design", f"docs/superpowers/specs/{ANCHOR_SLUG}-design.md"),
        _artifact_row("plan", f"docs/superpowers/plans/{ANCHOR_SLUG}.md"),
    ]
    manifest = _compiled_manifest("fix-standard", task_slug=ANCHOR_WORK_ID)
    allowed_refs = tuple(output for step in manifest.steps for output in step.outputs)

    with pytest.raises(ValueError, match="outside governed roots"):
        manager._publish_planning_artifacts(
            str(workspace),
            rows,
            work_id=ANCHOR_WORK_ID,
            allowed_refs=allowed_refs,
        )

    assert "anchor_slugs" in inspect.signature(
        manager._publish_planning_artifacts
    ).parameters
    rollback = manager._publish_planning_artifacts(
        str(workspace),
        rows,
        work_id=ANCHOR_WORK_ID,
        allowed_refs=allowed_refs,
        anchor_slugs=(ANCHOR_SLUG,),
    )

    evidence_rows = [
        {
            "kind": row["kind"],
            "ref": row["path"],
            "sha256": manager._sha256_path(workspace / row["path"]),
        }
        for row in rows
    ]
    evidence = _write_brainstorm_evidence(
        coordinator_root,
        repo="acme/demo",
        work_id=ANCHOR_WORK_ID,
        artifacts=evidence_rows,
    )
    run = SimpleNamespace(
        run_id="workflow-" + "a" * 20,
        repo="acme/demo",
        work_id=ANCHOR_WORK_ID,
        workspace_root=str(workspace),
        steps=manifest.steps,
        openspec_refs=(ANCHOR_SLUG,),
        planning_source_revision=SOURCE_REVISION,
        planning_authority=(),
        gate_refs=(GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),),
    )

    try:
        authority, source_revision = manager._validated_brainstorm_planning_authority(
            run,
            coordinator_root=coordinator_root,
        )
    finally:
        rollback()

    assert source_revision == SOURCE_REVISION
    assert {(item.kind, item.ref) for item in authority} == {
        (row["kind"], row["path"]) for row in rows
    }


def test_planning_kind_bound_accepts_anchor_slug_only_when_explicitly_provided() -> None:
    anchor_spec = f"docs/superpowers/specs/{ANCHOR_SLUG}-spec.md"

    assert manager.planning_kind_bound("spec", anchor_spec, ANCHOR_WORK_ID) is False
    assert "anchor_slugs" in inspect.signature(manager.planning_kind_bound).parameters
    assert (
        manager.planning_kind_bound(
            "spec",
            anchor_spec,
            ANCHOR_WORK_ID,
            anchor_slugs=(ANCHOR_SLUG,),
        )
        is True
    )


def test_authority_revalidation_rejects_glob_only_new_docs_ref(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    coordinator_root = tmp_path / "coordinator"
    manifest = _compiled_manifest("small-fix", task_slug=WORK_ID)
    ref = f"docs/superpowers/specs/{WORK_ID}-v2-spec.md"
    artifact = _write_artifact(workspace, kind="spec", ref=ref)
    evidence = _write_brainstorm_evidence(
        coordinator_root,
        repo="acme/demo",
        work_id=WORK_ID,
        artifacts=[artifact],
    )
    run = SimpleNamespace(
        run_id="workflow-" + "b" * 20,
        repo="acme/demo",
        work_id=WORK_ID,
        workspace_root=str(workspace),
        steps=manifest.steps,
        openspec_refs=(WORK_ID,),
        planning_source_revision=SOURCE_REVISION,
        planning_authority=(),
        gate_refs=(GateEvidenceRef("brainstorm", str(evidence), manager._sha256_path(evidence)),),
    )

    with pytest.raises(ValueError, match=f"outside planner outputs: ref={ref}"):
        manager._validated_brainstorm_planning_authority(
            run,
            coordinator_root=coordinator_root,
        )


@pytest.mark.parametrize(
    ("kind", "path", "work_id"),
    (
        ("spec", "docs/superpowers/specs/feat_work_gc-spec.md", "feat_work_gc"),
        ("spec", f"docs/superpowers/specs/nested/{WORK_ID}-spec.md", WORK_ID),
        ("spec", f"docs/superpowers/specs/./{WORK_ID}-spec.md", WORK_ID),
        ("spec", f"docs/superpowers/specs/\uff12\uff10\uff12\uff16-08-27-{WORK_ID}-spec.md", WORK_ID),
        ("spec", f"/tmp/{WORK_ID}-spec.md", WORK_ID),
        ("plan", f"docs/superpowers/plans/../plans/{WORK_ID}.md", WORK_ID),
    ),
)
def test_planning_kind_bound_rejects_guard_only_inputs(
    kind: str, path: str, work_id: str
) -> None:
    assert manager.planning_kind_bound(kind, path, work_id) is False


def test_planning_anchor_slugs_collects_and_filters_all_sources() -> None:
    run = SimpleNamespace(
        work_id=ANCHOR_WORK_ID,
        openspec_refs=("zzz-anchor", "bad/slug", "aaa-anchor", "zzz-anchor"),
        planning_authority=(
            PlanningArtifactAuthority(
                ref="docs/superpowers/workstreams/bbb-anchor/todo.md",
                kind="plan",
                work_id=ANCHOR_WORK_ID,
                baseline_sha256="b" * 64,
            ),
            PlanningArtifactAuthority(
                ref="docs/superpowers/workstreams/not_valid/todo.md",
                kind="plan",
                work_id=ANCHOR_WORK_ID,
                baseline_sha256="c" * 64,
            ),
            PlanningArtifactAuthority(
                ref="docs/superpowers/workstreams/too/deep/todo.md",
                kind="plan",
                work_id=ANCHOR_WORK_ID,
                baseline_sha256="d" * 64,
            ),
            PlanningArtifactAuthority(
                ref="docs/superpowers/workstreams/other-work/todo.md",
                kind="plan",
                work_id="another-work",
                baseline_sha256="e" * 64,
            ),
            PlanningArtifactAuthority(
                ref="docs/superpowers/specs/aaa-anchor-spec.md",
                kind="spec",
                work_id=ANCHOR_WORK_ID,
                baseline_sha256="f" * 64,
            ),
        ),
    )

    assert hasattr(manager, "_planning_anchor_slugs")
    assert manager._planning_anchor_slugs(run) == (
        "aaa-anchor",
        "bbb-anchor",
        "zzz-anchor",
    )
