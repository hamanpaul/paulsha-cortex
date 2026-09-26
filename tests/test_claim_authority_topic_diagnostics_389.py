"""Focused tests for issue #389: work intake against an issue-only work item
must not fail with the generic ``confirmed work authority missing or
ambiguous`` message.

Root cause (see #389): the lifecycle reducer (``monitor/lifecycle.py``) only
ever projects ``next_actions=("start",)`` when ``state == "todo" and
confidence == "confirmed"``; an issue-only work item has no todo-kind source
so it is permanently stuck in ``topic`` and never gets ``start``.
``claim._authority_from_canonical_row`` then silently ``return None``s for
such a row (one of three bare-``return None`` exits), so it is dropped from
``authorities`` *without* leaving any diagnostic behind. ``load_work_authority``
can't find a match and can't find a skip diagnostic either, so it falls
through to the generic ``"confirmed work authority missing or ambiguous"``
message -- indistinguishable from "row doesn't exist" or "issue claimed by
two work_ids".

This file locks in that all three ``return None`` exits (all-inferred / not
startable / no confirmed todo-kind source) leave a distinct, row-scoped
``AuthorityValidationError`` diagnostic instead, *and* that the two genuine
error shapes (row missing entirely, duplicate-identity ambiguous) keep their
own behaviour -- unswallowed by the new per-row diagnostics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.claim import (
    AuthorityValidationError,
    load_work_authorities,
    load_work_authority,
)


def _healthy_provider(
    *, revision: str = "gh-rev-1", last_success_at: str = "2026-08-10T00:00:00Z"
) -> dict:
    return {"status": "ok", "revision": revision, "last_success_at": last_success_at}


def _write_snapshot(path: Path, *, providers: dict, work_items: list[dict]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": providers,
                "work_items": work_items,
            }
        ),
        encoding="utf-8",
    )
    return path


def _issue_only_topic_row(*, work_id: str = "issue-only", issue: int = 99) -> dict:
    """Reproduces #389: a work item with only a *confirmed* GitHub issue
    source, no todo-kind source, and no active workflow -- the exact shape
    ``open-issue`` intake produces. The lifecycle reducer can never emit
    ``start`` for this shape, so ``state`` stays ``topic`` and
    ``next_actions`` stays empty.
    """
    return {
        "work_id": work_id,
        "repo": "acme/demo",
        "title": "Issue-only work item",
        "state": "topic",
        "phase": None,
        "facets": [],
        "sources": [
            {
                "source_id": f"github_issue:acme/demo#{issue}",
                "kind": "github_issue",
                "ref": f"acme/demo#{issue}",
                "revision": "issue-r1",
                "status": "open",
                "confidence": "confirmed",
                "provider": "github:acme/demo",
            }
        ],
        "next_actions": [],
        "workflow_run_id": None,
        "updated_at": "2026-08-10T00:00:00Z",
    }


def _todo_backed_row(*, work_id: str, issue: int, todo_ref: str = "docs/todo.md") -> dict:
    """A fully claimable canonical row: confirmed todo source + confirmed
    issue source + ``next_actions=["start"]``, mirroring what the lifecycle
    reducer emits once a workstream todo.md anchor exists (#388 pattern).
    """
    return {
        "work_id": work_id,
        "repo": "acme/demo",
        "title": "Todo-backed work item",
        "state": "todo",
        "phase": None,
        "facets": [],
        "sources": [
            {
                "source_id": f"todo:{todo_ref}",
                "kind": "todo",
                "ref": todo_ref,
                "revision": "todo-r1",
                "status": "active",
                "confidence": "confirmed",
                "provider": "local:acme/demo",
            },
            {
                "source_id": f"github_issue:acme/demo#{issue}",
                "kind": "github_issue",
                "ref": f"acme/demo#{issue}",
                "revision": "issue-r1",
                "status": "open",
                "confidence": "confirmed",
                "provider": "github:acme/demo",
            },
        ],
        "next_actions": ["start"],
        "workflow_run_id": None,
        "updated_at": "2026-08-10T00:00:00Z",
    }


# --- #389 核心情境：issue-only work item 卡在 topic，需要專屬診斷 ------------


def test_issue_only_topic_work_item_gets_specific_diagnostic_not_generic(
    tmp_path: Path,
) -> None:
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={"github:acme/demo": _healthy_provider()},
        work_items=[_issue_only_topic_row()],
    )
    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(repo="acme/demo", work_id="issue-only", snapshot_path=snapshot)
    exc = excinfo.value
    assert exc.repo == "acme/demo"
    assert exc.work_id == "issue-only"
    assert exc.reason_code == "authority-not-startable"
    rendered = str(exc)
    assert "issue-only" in rendered
    assert "missing or ambiguous" not in rendered
    assert "todo" in rendered.lower()


def test_issue_only_topic_work_item_does_not_appear_in_bulk_load(tmp_path: Path) -> None:
    """``load_work_authorities`` (used by bulk/background scans) must keep
    silently dropping the unclaimable row -- only the *targeted* lookup via
    ``load_work_authority`` needs the new diagnostic surfaced.
    """
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={"github:acme/demo": _healthy_provider()},
        work_items=[_issue_only_topic_row()],
    )
    authorities = load_work_authorities(snapshot_path=snapshot)
    assert authorities == ()


# --- 其餘兩個 return-None 分支：全 inferred／有 start 但無 todo-kind source --


def test_all_inferred_sources_gets_specific_diagnostic(tmp_path: Path) -> None:
    row = _issue_only_topic_row(work_id="inferred-only", issue=7)
    row["sources"][0]["confidence"] = "inferred"
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={"github:acme/demo": _healthy_provider()},
        work_items=[row],
    )
    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(repo="acme/demo", work_id="inferred-only", snapshot_path=snapshot)
    assert excinfo.value.reason_code == "authority-all-inferred"
    assert excinfo.value.work_id == "inferred-only"


def test_start_action_without_confirmed_todo_source_gets_specific_diagnostic(
    tmp_path: Path,
) -> None:
    """Defense in depth: the parser doesn't just trust an upstream-projected
    ``next_actions=["start"]`` -- a confirmed todo-kind source is still
    independently required (mirrors the belt-and-suspenders style already
    used for the other row-shape checks in this function).
    """
    row = _issue_only_topic_row(work_id="weird-start", issue=5)
    row["state"] = "todo"
    row["next_actions"] = ["start"]
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={"github:acme/demo": _healthy_provider()},
        work_items=[row],
    )
    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(repo="acme/demo", work_id="weird-start", snapshot_path=snapshot)
    assert excinfo.value.reason_code == "authority-no-confirmed-todo-source"
    assert excinfo.value.work_id == "weird-start"


def test_unsafe_work_id_never_leaks_into_diagnostic_message(tmp_path: Path) -> None:
    """`work_id` is only confirmed to be ``str`` at this point in
    ``_authority_from_canonical_row`` -- the identity safety regex hasn't run
    yet. The new #389 diagnostic messages must route through
    ``_diagnostic_label`` (same AI-SEC-001 contract as every other error in
    this module) instead of embedding the raw value, so a malformed row can
    never smuggle a path-like string into a durable error message.
    """
    row = _issue_only_topic_row(work_id="/etc/passwd", issue=1)
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={"github:acme/demo": _healthy_provider()},
        work_items=[row],
    )
    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(repo="acme/demo", work_id="/etc/passwd", snapshot_path=snapshot)
    rendered = str(excinfo.value)
    assert "/etc/passwd" not in rendered
    assert excinfo.value.work_id is None


# --- 對照組：真正 missing／ambiguous 不得被新分支吃掉 ------------------------


def test_genuinely_missing_row_keeps_generic_message(tmp_path: Path) -> None:
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={"github:acme/demo": _healthy_provider()},
        work_items=[_todo_backed_row(work_id="unrelated", issue=1)],
    )
    with pytest.raises(ValueError, match="missing or ambiguous"):
        load_work_authority(repo="acme/demo", work_id="does-not-exist", snapshot_path=snapshot)


def test_ambiguous_duplicate_owner_keeps_generic_message(tmp_path: Path) -> None:
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={"github:acme/demo": _healthy_provider()},
        work_items=[
            _todo_backed_row(work_id="owner-old", issue=41),
            _todo_backed_row(work_id="owner-new", issue=41),
        ],
    )
    with pytest.raises(ValueError, match="missing or ambiguous"):
        load_work_authority(repo="acme/demo", work_id="owner-new", snapshot_path=snapshot)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        load_work_authorities(snapshot_path=snapshot)


def test_malformed_repo_field_with_matching_work_id_blocks_confirmed_absent(
    tmp_path: Path,
) -> None:
    """#1093 對抗審查第四輪 MAJOR2：``load_work_authority`` 比對 skipped row
    時只信任 ``exc.repo``（已過 ``_diagnostic_label`` 篩選）——row 的 ``repo``
    欄位缺失／畸形時 ``exc.repo`` 是 ``None``，與目標 repo 字串比對恆為
    False。即使 ``exc.work_id`` 與目標完全相符，這一列仍會被當成「與目標
    無關」跳過，若 canonical GitHub provider 又剛好不存在（或健康），
    ``load_work_authority`` 會落到 ``WorkAuthorityConfirmedAbsent``——誤判
    確定缺席、放行 registry-only 退休，即使目標 row 其實還在，只是
    ``repo`` 欄位本身損毀。任何 work_id 相符（或無法判定是否相符）的畸形
    row 都必須維持一般 fail-closed，不得被誤判成確定缺席。
    """
    snapshot = _write_snapshot(
        tmp_path / "snapshot.json",
        providers={},
        work_items=[{"work_id": "demo", "repo": None, "sources": []}],
    )
    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(repo="acme/demo", work_id="demo", snapshot_path=snapshot)
    assert excinfo.value.reason_code == "row-malformed"
