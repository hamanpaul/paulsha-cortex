from __future__ import annotations

import os
from pathlib import Path

import pytest

from paulsha_cortex.monitor.correlation import (
    CorrelationError,
    InferredSignal,
    SourceLink,
    correlate_work_sources,
    load_work_item_overrides,
    read_frontmatter_work_item,
    unlink_work_source,
)
from paulsha_cortex.monitor.work_models import WorkSource


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _source(kind: str, ref: str, *, confidence="confirmed") -> WorkSource:
    return WorkSource(
        source_id=f"{kind}:example/acme:{ref}",
        kind=kind,
        ref=ref,
        revision="rev-1",
        status="active" if kind != "github_issue" else "open",
        confidence=confidence,
        provider="repo:example/acme" if not kind.startswith("github") else "github:example/acme",
    )


def test_override_schema_loads_confirmed_links_and_exclusions(tmp_path):
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """\
version: 1
work_items:
  unified-work-lifecycle:
    title: Unified lifecycle
    links:
      - kind: github_issue
        ref: example/acme#14
      - kind: path
        ref: docs/superpowers/specs/work.md
    excludes:
      - kind: github_pr
        ref: example/acme#999
""",
    )
    _write(tmp_path / "docs/superpowers/specs/work.md", "# work\n")

    loaded = load_work_item_overrides(tmp_path)

    item = loaded.work_items["unified-work-lifecycle"]
    assert item.title == "Unified lifecycle"
    assert item.links[0] == SourceLink("github_issue", "example/acme#14")
    assert item.excludes == (SourceLink("github_pr", "example/acme#999"),)


@pytest.mark.parametrize(
    "body,match",
    [
        ("version: 2\nwork_items: {}\n", "version"),
        ("version: 1\nwork_items: {}\nextra: true\n", "unknown"),
        (
            "version: 1\nwork_items:\n  BAD_ID:\n    title: bad\n    links: []\n    excludes: []\n",
            "work ID",
        ),
        (
            "version: 1\nwork_items:\n  okay:\n    title: bad\n    links:\n      - {kind: path, ref: ../escape.md}\n    excludes: []\n",
            "escape",
        ),
    ],
)
def test_override_rejects_invalid_or_unknown_schema(tmp_path, body, match):
    _write(tmp_path / ".cortex/work-items.yaml", body)
    with pytest.raises(CorrelationError, match=match):
        load_work_item_overrides(tmp_path)


def test_override_rejects_symlink_path_escape(tmp_path):
    outside = _write(tmp_path.parent / f"{tmp_path.name}-outside.md", "outside\n")
    link = tmp_path / "linked.md"
    link.symlink_to(outside)
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """\
version: 1
work_items:
  okay:
    title: bad
    links:
      - {kind: path, ref: linked.md}
    excludes: []
""",
    )
    try:
        with pytest.raises(CorrelationError, match="escape"):
            load_work_item_overrides(tmp_path)
    finally:
        outside.unlink(missing_ok=True)


def test_override_rejects_cross_work_item_confirmed_collision(tmp_path):
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """\
version: 1
work_items:
  one:
    title: one
    links: [{kind: github_issue, ref: example/acme#14}]
    excludes: []
  two:
    title: two
    links: [{kind: github_issue, ref: example/acme#14}]
    excludes: []
""",
    )
    with pytest.raises(CorrelationError, match="collision"):
        load_work_item_overrides(tmp_path)


def test_frontmatter_accepts_only_scalar_work_item_slug(tmp_path):
    valid = _write(tmp_path / "valid.md", "---\nwork_item: alpha-work\n---\n# body\n")
    missing = _write(tmp_path / "missing.md", "# body\n")
    listed = _write(tmp_path / "listed.md", "---\nwork_item: [one, two]\n---\n")

    assert read_frontmatter_work_item(valid) == "alpha-work"
    assert read_frontmatter_work_item(missing) is None
    with pytest.raises(CorrelationError, match="scalar"):
        read_frontmatter_work_item(listed)


def test_correlation_combines_override_and_frontmatter_as_confirmed(tmp_path):
    spec_ref = "docs/superpowers/specs/work.md"
    _write(tmp_path / spec_ref, "---\nwork_item: unified-work-lifecycle\n---\n# work\n")
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """\
version: 1
work_items:
  unified-work-lifecycle:
    title: Unified lifecycle
    links: [{kind: github_issue, ref: example/acme#14}]
    excludes: []
""",
    )
    issue = _source("github_issue", "example/acme#14")
    spec = _source("superpowers_spec", spec_ref)

    result = correlate_work_sources(tmp_path, "example/acme", (issue, spec))

    assert not result.degraded
    group = result.groups[0]
    assert group.work_id == "unified-work-lifecycle"
    assert group.title == "Unified lifecycle"
    assert {source.source_id for source in group.sources} == {issue.source_id, spec.source_id}
    assert result.source_owners == {
        issue.source_id: "unified-work-lifecycle",
        spec.source_id: "unified-work-lifecycle",
    }


def test_confirmed_collision_degrades_instead_of_choosing_priority(tmp_path):
    spec_ref = "docs/superpowers/specs/work.md"
    _write(tmp_path / spec_ref, "---\nwork_item: frontmatter-owner\n---\n")
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """\
version: 1
work_items:
  override-owner:
    title: override
    links: [{kind: path, ref: docs/superpowers/specs/work.md}]
    excludes: []
""",
    )

    result = correlate_work_sources(
        tmp_path, "example/acme", (_source("superpowers_spec", spec_ref),)
    )

    assert result.degraded
    assert result.source_owners == {}
    assert any("collision" in note for note in result.diagnostics)


def test_two_independent_inferred_signals_group_for_display_only(tmp_path):
    issue = _source("github_issue", "example/acme#14", confidence="inferred")
    spec = _source("superpowers_spec", "docs/superpowers/specs/work.md", confidence="inferred")
    _write(tmp_path / spec.ref, "# work\n")
    signals = (
        InferredSignal("display-work", "title", "same title", (issue.source_id, spec.source_id), 0.5),
        InferredSignal("display-work", "slug", "display-work", (issue.source_id, spec.source_id), 0.5),
    )

    result = correlate_work_sources(
        tmp_path, "example/acme", (issue, spec), inferred_signals=signals
    )

    group = next(group for group in result.groups if group.work_id == "display-work")
    assert group.confidence == "inferred"
    assert result.source_owners == {}
    assert all(signal["accepted"] for signal in result.explanations["display-work"]["inferred_signals"])


def test_competing_inferred_candidate_is_explained_but_not_grouped(tmp_path):
    issue = _source("github_issue", "example/acme#14", confidence="inferred")
    signals = (
        InferredSignal("candidate-a", "title", "same", (issue.source_id,), 0.5),
        InferredSignal("candidate-a", "slug", "a", (issue.source_id,), 0.5),
        InferredSignal("candidate-b", "title", "same", (issue.source_id,), 0.5),
        InferredSignal("candidate-b", "issue_token", "14", (issue.source_id,), 0.5),
    )

    result = correlate_work_sources(
        tmp_path, "example/acme", (issue,), inferred_signals=signals
    )

    assert all(group.work_id not in {"candidate-a", "candidate-b"} for group in result.groups)
    assert result.explanations["candidate-a"]["competing_candidates"] == ["candidate-b"]


def test_unlink_persists_exclusion_and_survives_restart(tmp_path):
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """\
version: 1
work_items:
  work:
    title: Work
    links: [{kind: github_issue, ref: example/acme#14}]
    excludes: []
""",
    )
    source = SourceLink("github_issue", "example/acme#14")

    unlink_work_source(tmp_path, "work", source)
    loaded = load_work_item_overrides(tmp_path)

    assert loaded.work_items["work"].links == ()
    assert loaded.work_items["work"].excludes == (source,)
    assert os.stat(tmp_path / ".cortex/work-items.yaml").st_mode & 0o777 == 0o600
