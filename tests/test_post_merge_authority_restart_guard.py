from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import claim
from paulsha_cortex.coordinator.claim import (
    AuthorityValidationError,
    load_work_authority,
    work_authority_digest,
)

REPO = "example/acme"
WORK_ID = "archive-proof"
CHANGE = "canary"
ISSUE_NUMBER = 7
PR_NUMBER = 9
NOW = "2026-09-23T00:00:00Z"
_MISSING = object()


def _source(
    *,
    source_id: str,
    kind: str,
    ref: str,
    revision: str,
    provider: str,
    status: str | None = None,
) -> dict[str, str]:
    payload = {
        "source_id": source_id,
        "kind": kind,
        "ref": ref,
        "revision": revision,
        "confidence": "confirmed",
        "provider": provider,
    }
    if status is not None:
        payload["status"] = status
    return payload


def _default_remote_pr_row() -> dict[str, object]:
    return {
        "source_id": f"github_pr:{REPO}#{PR_NUMBER}",
        "candidate": "a" * 40,
        "merge_revision": "b" * 40,
        "merged_with_merge_commit": True,
    }


def _provider(
    *,
    revision: str,
    status: str = "ok",
    observations: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "last_attempt_at": NOW,
        "last_success_at": NOW,
        "revision": revision,
        "diagnostics": [],
        "sources": [],
        "observations": observations or {},
    }


def _snapshot_payload(
    *,
    include_local_active: bool = True,
    include_pr_source: bool = True,
    pr_status: str = "closed",
    reverse_sources: bool = False,
    terminal_status: str = "ok",
    remote_prs: object = None,
    swap_openspec_providers: bool = False,
) -> dict[str, object]:
    local_provider = f"repo:{REPO}"
    terminal_provider = f"github-terminal:{REPO}"
    if swap_openspec_providers:
        local_provider, terminal_provider = terminal_provider, local_provider

    sources: list[dict[str, str]] = [
        _source(
            source_id=f"github_issue:{REPO}#{ISSUE_NUMBER}",
            kind="github_issue",
            ref=f"{REPO}#{ISSUE_NUMBER}",
            revision="github:issue:7",
            status="closed",
            provider=f"github:{REPO}",
        )
    ]
    if include_pr_source:
        sources.append(
            _source(
                source_id=f"github_pr:{REPO}#{PR_NUMBER}",
                kind="github_pr",
                ref=f"{REPO}#{PR_NUMBER}",
                revision="github:pr:9",
                status=pr_status,
                provider=f"github:{REPO}",
            )
        )
    if include_local_active:
        sources.append(
            _source(
                source_id=f"openspec:{REPO}:{CHANGE}",
                kind="openspec",
                ref=CHANGE,
                revision="local-spec-revision",
                status="active",
                provider=local_provider,
            )
        )
    sources.append(
        _source(
            source_id=f"github_openspec:{REPO}:{CHANGE}:archived",
            kind="openspec",
            ref=CHANGE,
            revision="github-tree:" + ("d" * 40),
            status="archived",
            provider=terminal_provider,
        )
    )
    if reverse_sources:
        sources.reverse()

    observations: dict[str, object] = {
        "remote_openspec": {"active": [], "archived": [CHANGE]},
        "remote_openspec_observed": True,
    }
    if remote_prs is not _MISSING:
        observations["remote_prs"] = (
            [_default_remote_pr_row()] if remote_prs is None else remote_prs
        )

    return {
        "schema": "work-items-snapshot/v1",
        "providers": {
            f"github:{REPO}": _provider(
                revision="github-snapshot:proof",
                observations={},
            ),
            f"github-terminal:{REPO}": _provider(
                revision="github-tree:" + ("e" * 40),
                status=terminal_status,
                observations=observations,
            ),
        },
        "work_items": [
            {
                "repo": REPO,
                "work_id": WORK_ID,
                "state": "todo",
                "sources": sources,
                "next_actions": ["start"],
            }
        ],
    }


def _write_snapshot(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _load(snapshot_path: Path):
    return load_work_authority(repo=REPO, work_id=WORK_ID, snapshot_path=snapshot_path)


def _assert_conflict(excinfo: pytest.ExceptionInfo[AuthorityValidationError]) -> None:
    assert excinfo.value.base_message == "confirmed semantic work authority revisions conflict"
    assert excinfo.value.reason_code == "row-malformed"
    assert excinfo.value.field == "source_revisions"
    assert excinfo.value.repo == REPO
    assert excinfo.value.work_id == WORK_ID


def _remove_pr_source(payload: dict[str, object]) -> None:
    work_item = payload["work_items"][0]
    work_item["sources"] = [
        source
        for source in work_item["sources"]
        if source["kind"] != "github_pr"
    ]


def _set_pr_open(payload: dict[str, object]) -> None:
    for source in payload["work_items"][0]["sources"]:
        if source["source_id"] == f"github_pr:{REPO}#{PR_NUMBER}":
            source["status"] = "open"


def _drop_remote_prs(payload: dict[str, object]) -> None:
    observations = payload["providers"][f"github-terminal:{REPO}"]["observations"]
    observations.pop("remote_prs", None)


def _mismatch_remote_pr_source_id(payload: dict[str, object]) -> None:
    payload["providers"][f"github-terminal:{REPO}"]["observations"]["remote_prs"] = [
        {
            **_default_remote_pr_row(),
            "source_id": f"github_pr:{REPO}#10",
        }
    ]


def _duplicate_true_remote_pr_rows(payload: dict[str, object]) -> None:
    row = _default_remote_pr_row()
    payload["providers"][f"github-terminal:{REPO}"]["observations"]["remote_prs"] = [
        row,
        dict(row),
    ]


def _duplicate_mixed_remote_pr_rows(payload: dict[str, object]) -> None:
    row = _default_remote_pr_row()
    payload["providers"][f"github-terminal:{REPO}"]["observations"]["remote_prs"] = [
        row,
        {**row, "merged_with_merge_commit": False},
    ]


def _degrade_terminal_provider(payload: dict[str, object]) -> None:
    payload["providers"][f"github-terminal:{REPO}"]["status"] = "degraded"


def _add_non_openspec_conflict(payload: dict[str, object]) -> None:
    payload["work_items"][0]["sources"].append(
        _source(
            source_id=f"github_pr:{REPO}#{PR_NUMBER}",
            kind="github_pr",
            ref=f"{REPO}#{PR_NUMBER}",
            revision="github:pr:9-merged",
            status="merged",
            provider=f"github:{REPO}",
        )
    )


def test_exact_remote_merge_proof_prefers_archived_authority(tmp_path: Path) -> None:
    expected_path = _write_snapshot(
        tmp_path / "archived-only.json",
        _snapshot_payload(include_local_active=False),
    )
    expected = _load(expected_path)

    snapshot = _write_snapshot(tmp_path / "active-plus-archived.json", _snapshot_payload())
    authority = _load(snapshot)

    assert authority.source_revisions == expected.source_revisions
    assert work_authority_digest(authority) == work_authority_digest(expected)
    assert authority.mapped_openspec == expected.mapped_openspec == (CHANGE,)


def test_archive_reconciliation_is_order_independent_and_warns_once_per_load(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected_path = _write_snapshot(
        tmp_path / "archived-only.json",
        _snapshot_payload(include_local_active=False),
    )
    expected = _load(expected_path)
    forward = _write_snapshot(tmp_path / "forward.json", _snapshot_payload())
    reversed_order = _write_snapshot(
        tmp_path / "reversed.json",
        _snapshot_payload(reverse_sources=True),
    )

    with caplog.at_level(logging.WARNING, logger=claim.__name__):
        forward_authority = _load(forward)
        reversed_authority = _load(reversed_order)

    assert forward_authority.source_revisions == expected.source_revisions
    assert reversed_authority.source_revisions == expected.source_revisions
    assert work_authority_digest(forward_authority) == work_authority_digest(expected)
    assert work_authority_digest(reversed_authority) == work_authority_digest(expected)

    messages = [
        record.getMessage()
        for record in caplog.records
        if REPO in record.getMessage()
        and WORK_ID in record.getMessage()
        and CHANGE in record.getMessage()
    ]
    assert len(messages) == 2
    for message in messages:
        assert str(tmp_path) not in message
        assert "openspec/changes/" not in message
        assert "docs/superpowers/" not in message


@pytest.mark.parametrize(
    "mutate",
    [
        _remove_pr_source,
        _set_pr_open,
        _drop_remote_prs,
        _mismatch_remote_pr_source_id,
        _duplicate_true_remote_pr_rows,
        _duplicate_mixed_remote_pr_rows,
        _degrade_terminal_provider,
        _add_non_openspec_conflict,
    ],
    ids=[
        "no-confirmed-pr-source",
        "pr-not-closed-or-merged",
        "remote-prs-missing",
        "remote-pr-source-id-mismatch",
        "duplicate-remote-pr-rows-both-true",
        "duplicate-remote-pr-rows-mixed-true-false",
        "terminal-provider-not-ok",
        "non-openspec-conflict",
    ],
)
def test_ineligible_archive_reconciliation_preserves_conflict_error(
    tmp_path: Path,
    mutate,
) -> None:
    payload = _snapshot_payload()
    mutate(payload)
    snapshot = _write_snapshot(tmp_path / f"{mutate.__name__}.json", payload)

    with pytest.raises(AuthorityValidationError) as excinfo:
        _load(snapshot)

    _assert_conflict(excinfo)


def test_provider_role_swap_preserves_conflict_error(tmp_path: Path) -> None:
    snapshot = _write_snapshot(
        tmp_path / "provider-role-swap.json",
        _snapshot_payload(swap_openspec_providers=True),
    )

    with pytest.raises(AuthorityValidationError) as excinfo:
        _load(snapshot)

    _assert_conflict(excinfo)
