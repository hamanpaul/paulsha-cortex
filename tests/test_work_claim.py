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
                "confirmed_todo": True,
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
        source_revisions=("issue:14@updated",),
        auto_label=False,
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


def test_authority_rejects_empty_issues_degraded_provider_and_issue_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mapped issues"):
        _authority(tmp_path, issues=())
    with pytest.raises(ValueError, match="provider authority invalid"):
        _authority(tmp_path, degraded=True)
    authority = _authority(tmp_path)
    with pytest.raises(ValueError, match="not authorized"):
        decide_manual_start(
            replace(_candidate(authority), confirmed_issue=99),
            now_epoch=1_000,
        )


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
