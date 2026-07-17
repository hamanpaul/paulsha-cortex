from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.claim import (
    AUTO_LABEL,
    ClaimCandidate,
    WorkAuthority,
    build_claim_key,
    build_label_argv,
    decide_auto_claim,
    decide_manual_start,
    load_work_authority,
)


def _authority(
    tmp_path: Path,
    *,
    last_success: float = 950,
    degraded: bool = False,
    issues=(14,),
) -> WorkAuthority:
    payload = {
        "schema": "work-items-snapshot/v1",
        "providers": {
            "github": {
                "provider_id": "github",
                "revision": "github-rev-1",
                "last_success_epoch": last_success,
                "degraded": degraded,
            }
        },
        "work_items": [
            {
                "repo": "acme/demo",
                "work_id": "lifecycle",
                "mapped_issues": list(issues),
                "mapped_prs": [8],
                "mapped_openspec": ["lifecycle"],
                "mapped_todo_paths": ["docs/todo.md"],
                "confirmed_todo": True,
                "auto_label": True,
                "source_revisions": ["issue:14@open", "openspec:lifecycle@abc"],
            }
        ],
    }
    path = tmp_path / f"snapshot-{last_success}-{degraded}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_work_authority(repo="acme/demo", work_id="lifecycle", snapshot_path=path)


def _candidate(authority: WorkAuthority) -> ClaimCandidate:
    return ClaimCandidate(
        authority=authority,
        repo="acme/demo",
        work_id="lifecycle",
        source_revisions=("issue:14@open", "openspec:lifecycle@abc"),
        confirmed_todo=True,
        confirmed_issue=14,
        auto_label=True,
        active_run_id=None,
        active_claim_key=None,
    )


def test_claim_key_is_stable_for_repo_work_and_sorted_revisions(tmp_path: Path) -> None:
    candidate = _candidate(_authority(tmp_path))
    first = build_claim_key(candidate)
    second = build_claim_key(
        replace(
            candidate,
            source_revisions=tuple(reversed(candidate.source_revisions)),
        )
    )
    assert first == second
    assert first.startswith("claim:v1:")


def test_work_authority_preserves_exact_confirmed_delivery_refs(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    assert authority.mapped_prs == (8,)
    assert authority.mapped_openspec == ("lifecycle",)
    assert authority.mapped_todo_paths == ("docs/todo.md",)


def test_manual_start_requires_confirmed_todo_issue_and_fresh_provider(tmp_path: Path) -> None:
    candidate = _candidate(_authority(tmp_path))
    assert decide_manual_start(candidate, now_epoch=1_000).action == "claim"
    with pytest.raises(ValueError, match="does not match WorkAuthority"):
        decide_manual_start(replace(candidate, confirmed_todo=False), now_epoch=1_000)
    missing = decide_manual_start(
        replace(candidate, confirmed_issue=None), now_epoch=1_000
    )
    assert missing.action == "needs_human"
    assert missing.reason == "missing_issue"
    assert decide_manual_start(
        _candidate(_authority(tmp_path, last_success=0)), now_epoch=1_000
    ).reason == "provider-degraded-or-stale"


def test_auto_claim_requires_todo_issue_label_and_fresh_provider(tmp_path: Path) -> None:
    candidate = _candidate(_authority(tmp_path))
    assert decide_auto_claim(candidate, now_epoch=1_000).action == "claim"
    with pytest.raises(ValueError, match="does not match WorkAuthority"):
        decide_auto_claim(replace(candidate, confirmed_todo=False), now_epoch=1_000)
    assert decide_auto_claim(
        replace(candidate, auto_label=False), now_epoch=1_000
    ).action == "ignore"
    assert decide_auto_claim(
        replace(candidate, confirmed_issue=None), now_epoch=1_000
    ).action == "needs_human"
    assert decide_auto_claim(
        _candidate(_authority(tmp_path, last_success=0)), now_epoch=1_000
    ).action == "blocked"


def test_active_run_is_idempotent_and_label_removal_does_not_cancel(tmp_path: Path) -> None:
    candidate = _candidate(_authority(tmp_path))
    original_key = build_claim_key(candidate)
    active = replace(
        candidate,
        active_run_id="run-1",
        active_claim_key=original_key,
        auto_label=False,
        active_status="ongoing",
        active_snapshot_hash=candidate.authority.snapshot_hash,
        active_source_revisions=candidate.authority.source_revisions,
        active_provider_revision=candidate.authority.github_provider_revision,
    )
    assert decide_manual_start(active, now_epoch=1_000).action == "resume"
    assert decide_auto_claim(active, now_epoch=1_000).action == "resume"
    assert decide_auto_claim(active, now_epoch=1_000).run_id == "run-1"
    assert decide_auto_claim(active, now_epoch=1_000).claim_key == original_key


def test_new_claim_requires_revisions_and_active_run_requires_persisted_key(tmp_path: Path) -> None:
    candidate = _candidate(_authority(tmp_path))
    with pytest.raises(ValueError, match="source revisions"):
        decide_manual_start(replace(candidate, source_revisions=()), now_epoch=1_000)
    with pytest.raises(ValueError, match="persisted claim key"):
        decide_auto_claim(replace(candidate, active_run_id="run-1"), now_epoch=1_000)


@pytest.mark.parametrize("field", ["confirmed_todo", "auto_label"])
def test_claim_boolean_fields_are_strict(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match="must be boolean"):
        decide_auto_claim(
            replace(_candidate(_authority(tmp_path)), **{field: 1}),
            now_epoch=1_000,
        )


def test_authority_allows_missing_issue_for_needs_human_and_rejects_issue_mismatch(tmp_path: Path) -> None:
    authority_without_issue = _authority(tmp_path, issues=())
    missing = decide_auto_claim(
        replace(
            _candidate(authority_without_issue),
            confirmed_issue=None,
            source_revisions=authority_without_issue.source_revisions,
        ),
        now_epoch=1_000,
    )
    assert missing.action == "needs_human"
    assert missing.reason == "missing_issue"
    with pytest.raises(ValueError, match="provider authority invalid"):
        _authority(tmp_path, degraded=True)
    authority = _authority(tmp_path)
    with pytest.raises(ValueError, match="not authorized"):
        decide_manual_start(
            replace(_candidate(authority), confirmed_issue=99),
            now_epoch=1_000,
        )


def test_resume_requires_exact_persisted_authority_and_terminal_is_not_active(tmp_path: Path) -> None:
    candidate = _candidate(_authority(tmp_path))
    claim_key = build_claim_key(candidate)
    active = replace(
        candidate,
        active_run_id="run-1",
        active_claim_key=claim_key,
        active_status="ongoing",
        active_snapshot_hash=candidate.authority.snapshot_hash,
        active_source_revisions=candidate.authority.source_revisions,
        active_provider_revision=candidate.authority.github_provider_revision,
    )
    assert decide_manual_start(active, now_epoch=1_000).action == "resume"
    with pytest.raises(ValueError, match="claim key"):
        decide_manual_start(
            replace(active, active_claim_key=f"claim:v1:{'0' * 64}"),
            now_epoch=1_000,
        )
    assert decide_manual_start(
        replace(active, active_status="done"), now_epoch=1_000
    ).action == "done"
    assert decide_manual_start(
        replace(active, active_status="needs_human"), now_epoch=1_000
    ).action == "needs_human"


def test_label_rest_argv_is_typed() -> None:
    assert build_label_argv(
        repo="acme/demo", issue=14, enabled=True
    ) == [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/acme/demo/issues/14/labels",
        "-f",
        f"labels[]={AUTO_LABEL}",
    ]
    disabled = build_label_argv(repo="acme/demo", issue=14, enabled=False)
    assert disabled[:4] == ["gh", "api", "--method", "DELETE"]
    assert disabled[-1].endswith("/labels/cortex%3Aauto-on-going")


def test_loader_accepts_pr_a_canonical_durable_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "canonical.json"
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "sequence": 7,
                "written_at": "2026-07-17T10:00:00Z",
                "providers": {
                    "github:acme/demo": {
                        "status": "ok",
                        "last_attempt_at": "2026-07-17T10:00:00Z",
                        "last_success_at": "2026-07-17T10:00:00Z",
                        "revision": "github-snapshot:abc",
                        "diagnostics": [],
                        "sources": [],
                        "observations": {},
                    }
                },
                "work_items": [
                    {
                        "work_id": "lifecycle",
                        "repo": "acme/demo",
                        "title": "Lifecycle",
                        "state": "todo",
                        "phase": None,
                        "facets": [],
                        "sources": [
                            {
                                "source_id": "github_issue:acme/demo#14",
                                "kind": "github_issue",
                                "ref": "acme/demo#14",
                                "revision": "issue-r1",
                                "status": "open",
                                "confidence": "confirmed",
                                "provider": "github:acme/demo",
                            },
                            {
                                "source_id": "openspec:lifecycle",
                                "kind": "openspec",
                                "ref": "lifecycle",
                                "revision": "spec-r1",
                                "status": "active",
                                "confidence": "confirmed",
                                "provider": "local:acme/demo",
                            },
                            {
                                "source_id": "github_pr:acme/demo#8",
                                "kind": "github_pr",
                                "ref": "acme/demo#8",
                                "revision": "pr-r1",
                                "status": "open",
                                "confidence": "confirmed",
                                "provider": "github:acme/demo",
                            },
                            {
                                "source_id": "todo:docs/todo.md",
                                "kind": "todo",
                                "ref": "docs/todo.md",
                                "revision": "todo-r1",
                                "status": "active",
                                "confidence": "confirmed",
                                "provider": "local:acme/demo",
                            },
                        ],
                        "next_actions": ["start"],
                        "workflow_run_id": None,
                        "updated_at": "2026-07-17T10:00:00Z",
                    }
                ],
                "source_owners": {
                    "github_issue:acme/demo#14": "acme/demo::lifecycle",
                    "github_pr:acme/demo#8": "acme/demo::lifecycle",
                    "openspec:lifecycle": "acme/demo::lifecycle",
                    "todo:docs/todo.md": "acme/demo::lifecycle",
                },
                "exclusions": [],
            }
        ),
        encoding="utf-8",
    )
    authority = load_work_authority(
        repo="acme/demo", work_id="lifecycle", snapshot_path=path
    )
    assert authority.github_provider_id == "github:acme/demo"
    assert authority.mapped_issues == (14,)
    assert authority.confirmed_todo
    assert authority.mapped_prs == (8,)
    assert authority.mapped_openspec == ("lifecycle",)
    assert authority.mapped_todo_paths == ("docs/todo.md",)
    assert authority.source_revisions == (
        "github_issue:acme/demo#14@issue-r1",
        "github_pr:acme/demo#8@pr-r1",
        "openspec:lifecycle@spec-r1",
        "todo:docs/todo.md@todo-r1",
    )
