from __future__ import annotations

from dataclasses import replace

from paulsha_cortex.coordinator.claim import (
    AUTO_LABEL,
    ClaimCandidate,
    build_claim_key,
    build_label_argv,
    decide_auto_claim,
    decide_manual_start,
)


def _candidate() -> ClaimCandidate:
    return ClaimCandidate(
        repo="acme/demo",
        work_id="lifecycle",
        source_revisions=("issue:14@open", "openspec:lifecycle@abc"),
        confirmed_todo=True,
        confirmed_issue=14,
        auto_label=True,
        provider_fresh=True,
        active_run_id=None,
    )


def test_claim_key_is_stable_for_repo_work_and_sorted_revisions() -> None:
    first = build_claim_key(_candidate())
    second = build_claim_key(
        replace(
            _candidate(),
            source_revisions=tuple(reversed(_candidate().source_revisions)),
        )
    )
    assert first == second
    assert first.startswith("claim:v1:")


def test_manual_start_requires_confirmed_todo_issue_and_fresh_provider() -> None:
    assert decide_manual_start(_candidate()).action == "claim"
    assert decide_manual_start(
        replace(_candidate(), confirmed_todo=False)
    ).reason == "confirmed-todo-required"
    missing = decide_manual_start(replace(_candidate(), confirmed_issue=None))
    assert missing.action == "needs_human"
    assert missing.reason == "missing_issue"
    assert decide_manual_start(
        replace(_candidate(), provider_fresh=False)
    ).reason == "provider-degraded-or-stale"


def test_auto_claim_requires_todo_issue_label_and_fresh_provider() -> None:
    assert decide_auto_claim(_candidate()).action == "claim"
    assert decide_auto_claim(
        replace(_candidate(), confirmed_todo=False)
    ).action == "ignore"
    assert decide_auto_claim(
        replace(_candidate(), auto_label=False)
    ).action == "ignore"
    assert decide_auto_claim(
        replace(_candidate(), confirmed_issue=None)
    ).action == "needs_human"
    assert decide_auto_claim(
        replace(_candidate(), provider_fresh=False)
    ).action == "blocked"


def test_active_run_is_idempotent_and_label_removal_does_not_cancel() -> None:
    active = replace(_candidate(), active_run_id="run-1", auto_label=False)
    assert decide_manual_start(active).action == "resume"
    assert decide_auto_claim(active).action == "resume"
    assert decide_auto_claim(active).run_id == "run-1"


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
