"""#716：ship merge gate 遇到仍在跑的 check 要等待，不得擲例外變 needs_human。

現場（live canary run 37320144662）：PR 14:20:30Z 建立，`tests` check 14:21:22Z
success；`copilot-pull-request-reviewer` check 14:20:49Z 開始、14:23:11Z 才 success。
Manager 的 ship tick 約 14:23:00Z 進 merge gate：Copilot review 本身已送出（0 finding，
review loop 判 `passed`），但該 check run 還是 `in_progress`，`evaluate_delivery_gate`
給出 `checks-not-terminal-green`，`_ship_action` 直接
``RuntimeError("merge authorization blocked: checks-not-terminal-green")``，periodic tick
把它記成 `resume-workflow-failed` → needs_human。

修正後：gate 唯一的理由是 `checks-not-terminal-green`、且所有非綠 check 都還沒
completed 時，回非終局 `checks-pending`（不寫 merge authorization、不動 ship state、
不吃 repair round），下一個 tick 重新評估；completed 但非綠（真失敗）與其他 gate
理由維持原本的 fail-closed 擲例外。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import test_maintainer_fallback_authorization_v2 as maintainer_fixture
import test_post_merge_run_resolution_1141 as ship_fixture
from paulsha_cortex.coordinator import work_actions, work_bridge
from paulsha_cortex.coordinator.github_delivery import GitHubCheck


COPILOT_CHECK = "copilot-pull-request-reviewer"


def _with_checks(lane, checks: tuple[GitHubCheck, ...], **overrides) -> None:
    """讓 lane 的 fake GitHub 回傳指定 checks（其餘 facts 沿用 ship lane 的全綠）。"""

    original_fetch = type(lane.github).fetch_delivery_facts

    def fetch_delivery_facts(**kwargs):
        return replace(original_fetch(lane.github, **kwargs), checks=checks, **overrides)

    lane.github.fetch_delivery_facts = fetch_delivery_facts


def _ship_row(lane) -> dict:
    return json.loads(lane.journal.read_text(encoding="utf-8"))["runs"][lane.run_id]


def _merge_authorization_files(root: Path) -> list[Path]:
    merge_dir = root / "evidence" / "merge-authorization"
    return sorted(merge_dir.iterdir()) if merge_dir.is_dir() else []


@pytest.mark.parametrize("pending_status", ["queued", "in_progress", "waiting"])
def test_copilot_ship_waits_for_running_check_then_merges_on_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending_status: str
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    _with_checks(
        lane,
        (
            GitHubCheck("tests", "completed", "success"),
            GitHubCheck(COPILOT_CHECK, pending_status, None),
        ),
    )

    first = lane.ship(now=2000.0)

    assert first == {
        "action": "checks-pending",
        "reason": "checks-not-terminal-green",
        "pending_checks": [COPILOT_CHECK],
        "head": ship_fixture.HEAD,
    }
    assert work_bridge._delivery_adapter_status(first["action"]) == "pending"
    row = _ship_row(lane)
    # 不得寫 merge authorization、不得跳過 review-requested、不得吃 repair round。
    assert row["ship"]["phase"] == "review-requested"
    assert "merge_authorization" not in row["ship"]
    assert row.get("repair_rounds", 0) == 0
    assert _merge_authorization_files(tmp_path) == []
    run = lane.registry.get_workflow_run(lane.run_id)
    assert "needs_human" not in run.facets
    assert run.needs_human_reason is None

    # 同一筆 Copilot review（review_id 42）在 check 完成後的下一個 tick 必須能重入，
    # 並照常寫 merge authorization → merge。
    _with_checks(
        lane,
        (
            GitHubCheck("tests", "completed", "success"),
            GitHubCheck(COPILOT_CHECK, "completed", "success"),
        ),
    )

    second = lane.ship(now=2100.0)

    assert second == {"action": "merged-awaiting-closure", "head": ship_fixture.HEAD}
    row = _ship_row(lane)
    assert row["ship"]["phase"] == "merged"
    assert row["ship"]["review_id"] == 42
    assert row["ship"]["merge_authorization"]["payload"]["copilot_review_id"] == 42
    assert row.get("repair_rounds", 0) == 0
    assert len(_merge_authorization_files(tmp_path)) == 1


def test_periodic_tick_keeps_run_in_review_without_needs_human_while_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    _with_checks(
        lane,
        (
            GitHubCheck("tests", "completed", "success"),
            GitHubCheck(COPILOT_CHECK, "in_progress", None),
        ),
    )

    ship_fixture._tick_resume(lane)

    run = lane.registry.get_workflow_run(lane.run_id)
    assert run.current_phase == "review"
    assert run.status == "ongoing"
    assert "needs_human" not in run.facets
    assert run.needs_human_reason is None
    assert run.gate_status == "running"
    assert "merge_authorization" not in _ship_row(lane)["ship"]

    _with_checks(
        lane,
        (
            GitHubCheck("tests", "completed", "success"),
            GitHubCheck(COPILOT_CHECK, "completed", "success"),
        ),
    )

    ship_fixture._tick_resume(lane)

    assert _ship_row(lane)["ship"]["phase"] == "merged"
    assert "needs_human" not in lane.registry.get_workflow_run(lane.run_id).facets


@pytest.mark.parametrize(
    ("checks", "overrides", "message"),
    [
        (
            (
                GitHubCheck("tests", "completed", "failure"),
                GitHubCheck(COPILOT_CHECK, "in_progress", None),
            ),
            {},
            "merge authorization blocked: checks-not-terminal-green",
        ),
        (
            (GitHubCheck("tests", "completed", None),),
            {},
            "merge authorization blocked: checks-not-terminal-green",
        ),
        (
            (GitHubCheck("tests", "", None),),
            {},
            "merge authorization blocked: checks-not-terminal-green",
        ),
        (
            (),
            {},
            "merge authorization blocked: checks-not-terminal-green",
        ),
        (
            (GitHubCheck(COPILOT_CHECK, "in_progress", None),),
            {"mergeable": False, "mergeable_state": "dirty"},
            "merge authorization blocked: not-mergeable, checks-not-terminal-green",
        ),
    ],
    ids=[
        "completed-failure-beside-running",
        "completed-without-conclusion",
        "unknown-status",
        "no-checks",
        "running-plus-not-mergeable",
    ],
)
def test_copilot_ship_keeps_fail_closed_for_real_check_failures_and_other_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checks: tuple[GitHubCheck, ...],
    overrides: dict,
    message: str,
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    _with_checks(lane, checks, **overrides)

    with pytest.raises(RuntimeError, match=f"^{message}$"):
        lane.ship(now=2000.0)

    assert "merge_authorization" not in _ship_row(lane)["ship"]
    assert _merge_authorization_files(tmp_path) == []


def test_maintainer_review_ship_waits_for_running_check_then_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = maintainer_fixture._bootstrap_review_env(tmp_path)
    maintainer_fixture._set_ship(env, phase="needs_human", reason="copilot-review-timeout")
    before = maintainer_fixture._journal_row(env.state, env.run_id)
    runtime = maintainer_fixture._patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=maintainer_fixture._merge_forbidden,
        checks=(
            GitHubCheck("pytest", "completed", "success"),
            GitHubCheck("lint", "queued", None),
        ),
    )

    first = maintainer_fixture._ship(env)

    assert first["result"] == {
        "action": "checks-pending",
        "reason": "checks-not-terminal-green",
        "pending_checks": ["lint"],
        "head": maintainer_fixture.HEAD,
    }
    assert runtime.merge_calls == []
    after = maintainer_fixture._journal_row(env.state, env.run_id)
    # ship state 原封不動（仍是可由 maintainer 復原的 Copilot stop）、不吃 repair round。
    assert after["ship"] == before["ship"]
    assert after.get("repair_rounds", 0) == before.get("repair_rounds", 0)
    assert not maintainer_fixture._merge_dir(env.state).exists() or not any(
        maintainer_fixture._merge_dir(env.state).iterdir()
    )

    runtime = maintainer_fixture._patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=maintainer_fixture._merge_success,
        checks=(
            GitHubCheck("pytest", "completed", "success"),
            GitHubCheck("lint", "completed", "success"),
        ),
    )

    second = maintainer_fixture._ship(env)

    assert second["result"]["action"] == "merged-awaiting-closure"
    assert len(runtime.merge_calls) == 1
    ship = maintainer_fixture._journal_row(env.state, env.run_id)["ship"]
    assert ship["phase"] == "merged"
    assert ship["merge_authorization"]["payload"]["review_kind"] == "maintainer-review"


def test_maintainer_review_ship_keeps_fail_closed_for_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = maintainer_fixture._bootstrap_review_env(tmp_path)
    maintainer_fixture._set_ship(env, phase="needs_human", reason="copilot-review-timeout")
    runtime = maintainer_fixture._patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=maintainer_fixture._merge_forbidden,
        checks=(
            GitHubCheck("pytest", "completed", "failure"),
            GitHubCheck("lint", "in_progress", None),
        ),
    )

    with pytest.raises(
        RuntimeError, match="^merge authorization blocked: checks-not-terminal-green$"
    ):
        maintainer_fixture._ship(env)

    assert runtime.merge_calls == []
    assert "merge_authorization" not in maintainer_fixture._journal_row(
        env.state, env.run_id
    )["ship"]
