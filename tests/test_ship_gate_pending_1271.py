"""#1271：ship merge gate 在兩個 #1268 沒涵蓋的相鄰情況下仍要等待，不得進 needs_human。

1. branch protection 把仍在跑的 check 列為 required：GitHub 同時回報
   `mergeable_state=blocked`，gate 多一個 `not-mergeable`，原本照舊
   ``RuntimeError("merge authorization blocked: not-mergeable, checks-not-terminal-green")``
   → `resume-workflow-failed`。修正後只有在 GitHub 正向證明「blocked 來自仍在跑的
   required check」（同一 exact HEAD 的 required context 仍在跑、沒有 required context
   已終局非綠、reviewDecision 不是 REVIEW_REQUIRED／CHANGES_REQUESTED）時才等待；
   其餘 blocked 原因維持 fail-closed。
2. 採信既有 Copilot review 後 check 跑超過 15 分鐘：`ReviewLoop.record_review` 以
   `now - adopted_at` 判 timeout，回 `copilot-review-timeout` → needs_human，訊息誤導。
   修正後採信 review 的期限只看提交時間（與 remote final gate 同一判式），觀測時間
   不再把已採信的 review 轉成 timeout；check 一直不結束則在
   `CHECKS_PENDING_TIMEOUT_SECONDS` 後以非 review 的 `checks-pending-timeout` 交人工。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_delivery_orchestrator as orchestrator_fixture
import test_github_delivery_client as client_fixture
import test_maintainer_fallback_authorization_v2 as maintainer_fixture
import test_post_merge_run_resolution_1141 as ship_fixture
import test_ship_checks_pending_716 as pending_716
from paulsha_cortex.coordinator import github_delivery, manager, work_actions, work_bridge
from paulsha_cortex.coordinator.delivery import (
    REVIEW_TIMEOUT_SECONDS,
    ReviewLoop,
    ShipOrchestrator,
)
from paulsha_cortex.coordinator.github_delivery import (
    COPILOT_REVIEWER_LOGIN,
    CopilotReview,
    DeliveryFacts,
    GitHubCheck,
    GitHubDeliveryClient,
    ReviewThread,
)
from paulsha_cortex.coordinator.model_identities import IdentityRegistry


COPILOT_CHECK = pending_716.COPILOT_CHECK
BLOCKED_MESSAGE = "merge authorization blocked: not-mergeable, checks-not-terminal-green"


def _merge_block(
    *,
    head: str = ship_fixture.HEAD,
    review_decision: str | None = None,
    required_checks: tuple[GitHubCheck, ...] = (GitHubCheck("tests", "in_progress", None),),
    complete: bool = True,
):
    return github_delivery.MergeBlockFacts(
        head=head,
        review_decision=review_decision,
        required_checks=required_checks,
        complete=complete,
    )


def _required_check_running() -> tuple[GitHubCheck, ...]:
    return (
        GitHubCheck("tests", "in_progress", None),
        GitHubCheck(COPILOT_CHECK, "completed", "success"),
    )


def _all_green() -> tuple[GitHubCheck, ...]:
    return (
        GitHubCheck("tests", "completed", "success"),
        GitHubCheck(COPILOT_CHECK, "completed", "success"),
    )


def _operator_resume(lane, *, now: float):
    """operator 明示 `cortex work resume`：needs_human facet 下仍會跑 ship validator。"""

    def ship_validator(*, run, candidate):
        action = lane.ship(now=now, closure=False)
        return {
            "trusted": True,
            "status": work_bridge._delivery_adapter_status(action.get("action")),
            "head": candidate,
            "commit_id": candidate,
            "reason": action.get("reason"),
            "ref": "delivery-adapter.json",
            "hash": "f" * 64,
        }

    return manager.resume_workflow_run(
        SimpleNamespace(_registry=lane.registry, _git_runner=None),
        run_id=lane.run_id,
        identities=IdentityRegistry.from_rows([]),
        launcher_factory=lambda _: (_ for _ in ()).throw(AssertionError("must not launch")),
        coordinator_root=lane.tmp_path,
        ship_validator=ship_validator,
        operator_resume=True,
    )


# ---------------------------------------------------------------------------
# 1. required check 仍在跑、GitHub 回報 blocked
# ---------------------------------------------------------------------------


def _blocked_facts(**overrides) -> DeliveryFacts:
    facts = DeliveryFacts(
        head=ship_fixture.HEAD,
        mergeable=True,
        mergeable_state="blocked",
        checks=_required_check_running(),
        copilot_reviews=(),
        review_threads=(),
        closing_issues=(),
        active_openspec_absent=True,
        archive_present=True,
        openspec_required=False,
        merge_block=_merge_block(),
    )
    return replace(facts, **overrides)


def test_required_pending_check_is_positive_evidence_for_blocked_state() -> None:
    assert github_delivery.required_checks_pending_block(_blocked_facts()) == ("tests",)
    status_context = _blocked_facts(
        checks=(
            GitHubCheck("legacy/ci", "in_progress", "pending"),
            GitHubCheck(COPILOT_CHECK, "completed", "success"),
        ),
        merge_block=_merge_block(
            review_decision="APPROVED",
            required_checks=(
                GitHubCheck("legacy/ci", "in_progress", None),
                GitHubCheck("lint", "completed", "success"),
            ),
        ),
    )
    assert github_delivery.required_checks_pending_block(status_context) == ("legacy/ci",)


@pytest.mark.parametrize(
    "overrides",
    [
        lambda: {"merge_block": None},
        lambda: {"mergeable": False},
        lambda: {"mergeable_state": "clean"},
        lambda: {"mergeable_state": "unstable"},
        lambda: {"mergeable_state": "dirty"},
        lambda: {"merge_block": _merge_block(head="f" * 40)},
        lambda: {"merge_block": _merge_block(review_decision="REVIEW_REQUIRED")},
        lambda: {"merge_block": _merge_block(review_decision="CHANGES_REQUESTED")},
        lambda: {"merge_block": _merge_block(complete=False)},
        lambda: {"merge_block": _merge_block(required_checks=())},
        lambda: {
            "merge_block": _merge_block(
                required_checks=(GitHubCheck("lint", "completed", "success"),)
            )
        },
        lambda: {
            "merge_block": _merge_block(
                required_checks=(
                    GitHubCheck("tests", "in_progress", None),
                    GitHubCheck("lint", "completed", "failure"),
                )
            )
        },
        lambda: {
            "merge_block": _merge_block(
                required_checks=(GitHubCheck("tests", "", None),)
            )
        },
        # GraphQL 說 required check 在跑，REST 卻看不到同名仍在跑的 check（race）。
        lambda: {
            "merge_block": _merge_block(
                required_checks=(GitHubCheck("deploy", "in_progress", None),)
            )
        },
    ],
    ids=[
        "no-graphql-facts",
        "conflicting",
        "clean",
        "unstable",
        "dirty",
        "graphql-head-race",
        "review-required",
        "changes-requested",
        "rollup-incomplete",
        "no-required-checks",
        "required-checks-all-green",
        "required-check-failed",
        "required-check-unknown-status",
        "required-check-not-running-in-rest",
    ],
)
def test_blocked_state_without_positive_evidence_is_not_a_pending_required_check(
    overrides,
) -> None:
    assert github_delivery.required_checks_pending_block(_blocked_facts(**overrides())) == ()


def test_copilot_ship_waits_while_required_check_blocks_merge_then_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    pending_716._with_checks(
        lane,
        _required_check_running(),
        mergeable_state="blocked",
        merge_block=_merge_block(),
    )

    first = lane.ship(now=2000.0)

    assert first == {
        "action": "checks-pending",
        "reason": "checks-not-terminal-green",
        "pending_checks": ["tests"],
        "required_pending_checks": ["tests"],
        "head": ship_fixture.HEAD,
    }
    assert work_bridge._delivery_adapter_status(first["action"]) == "pending"
    row = pending_716._ship_row(lane)
    assert row["ship"]["phase"] == "review-requested"
    assert "merge_authorization" not in row["ship"]
    assert row.get("repair_rounds", 0) == 0
    assert pending_716._merge_authorization_files(tmp_path) == []
    run = lane.registry.get_workflow_run(lane.run_id)
    assert "needs_human" not in run.facets
    assert run.needs_human_reason is None

    pending_716._with_checks(lane, _all_green(), mergeable_state="clean")

    second = lane.ship(now=2100.0)

    assert second == {"action": "merged-awaiting-closure", "head": ship_fixture.HEAD}
    row = pending_716._ship_row(lane)
    assert row["ship"]["phase"] == "merged"
    assert row["ship"]["merge_authorization"]["payload"]["copilot_review_id"] == 42
    assert len(pending_716._merge_authorization_files(tmp_path)) == 1


def test_periodic_tick_keeps_blocked_run_in_review_while_required_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    pending_716._with_checks(
        lane,
        _required_check_running(),
        mergeable_state="blocked",
        merge_block=_merge_block(),
    )

    ship_fixture._tick_resume(lane)

    run = lane.registry.get_workflow_run(lane.run_id)
    assert run.current_phase == "review"
    assert "needs_human" not in run.facets
    assert run.needs_human_reason is None
    assert "merge_authorization" not in pending_716._ship_row(lane)["ship"]

    pending_716._with_checks(lane, _all_green(), mergeable_state="clean")

    ship_fixture._tick_resume(lane)

    assert pending_716._ship_row(lane)["ship"]["phase"] == "merged"
    assert "needs_human" not in lane.registry.get_workflow_run(lane.run_id).facets


@pytest.mark.parametrize(
    ("checks", "overrides", "message"),
    [
        (_required_check_running(), lambda: {"merge_block": None}, BLOCKED_MESSAGE),
        (
            _required_check_running(),
            lambda: {"merge_block": _merge_block(review_decision="REVIEW_REQUIRED")},
            BLOCKED_MESSAGE,
        ),
        (
            _required_check_running(),
            lambda: {"merge_block": _merge_block(review_decision="CHANGES_REQUESTED")},
            BLOCKED_MESSAGE,
        ),
        (
            _required_check_running(),
            lambda: {"merge_block": _merge_block(head="f" * 40)},
            BLOCKED_MESSAGE,
        ),
        (
            (
                GitHubCheck("tests", "in_progress", None),
                GitHubCheck("lint", "completed", "failure"),
            ),
            lambda: {},
            BLOCKED_MESSAGE,
        ),
        (
            _required_check_running(),
            lambda: {
                "merge_block": _merge_block(
                    required_checks=(GitHubCheck("deploy", "in_progress", None),)
                )
            },
            BLOCKED_MESSAGE,
        ),
        (_required_check_running(), lambda: {"mergeable": False}, BLOCKED_MESSAGE),
        (_all_green(), lambda: {}, "merge authorization blocked: not-mergeable"),
    ],
    ids=[
        "no-graphql-facts",
        "review-required",
        "changes-requested",
        "graphql-head-race",
        "failed-check-beside-running",
        "required-check-not-running-in-rest",
        "conflicting",
        "blocked-with-all-checks-green",
    ],
)
def test_copilot_ship_keeps_fail_closed_for_other_blocked_causes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checks: tuple[GitHubCheck, ...],
    overrides,
    message: str,
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    pending_716._with_checks(
        lane,
        checks,
        **{"mergeable_state": "blocked", "merge_block": _merge_block(), **overrides()},
    )

    with pytest.raises(RuntimeError, match=f"^{message}$"):
        lane.ship(now=2000.0)

    assert "merge_authorization" not in pending_716._ship_row(lane)["ship"]
    assert pending_716._merge_authorization_files(tmp_path) == []


def test_copilot_ship_routes_open_review_thread_to_fix_while_required_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    pending_716._with_checks(
        lane,
        _required_check_running(),
        mergeable_state="blocked",
        merge_block=_merge_block(),
        review_threads=(ReviewThread(thread_id="T1", resolved=False, outdated=False),),
    )

    result = lane.ship(now=2000.0)

    assert result["action"] == "fix-required"
    assert "merge_authorization" not in pending_716._ship_row(lane)["ship"]
    assert pending_716._merge_authorization_files(tmp_path) == []


def test_copilot_ship_keeps_head_race_fail_closed_while_required_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    pending_716._with_checks(
        lane,
        _required_check_running(),
        head="f" * 40,
        mergeable_state="blocked",
        merge_block=_merge_block(head="f" * 40),
    )

    with pytest.raises(RuntimeError, match="^ship HEAD differs from authenticated GitHub PR$"):
        lane.ship(now=2000.0)

    assert pending_716._merge_authorization_files(tmp_path) == []


def _patch_maintainer_facts(monkeypatch: pytest.MonkeyPatch, **overrides) -> None:
    base = work_actions.GitHubDeliveryClient

    class GitHub(base):
        def fetch_delivery_facts(self, **kwargs):
            return replace(super().fetch_delivery_facts(**kwargs), **overrides)

    monkeypatch.setattr(work_actions, "GitHubDeliveryClient", GitHub)


def test_maintainer_review_ship_waits_while_required_check_blocks_merge(
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
            GitHubCheck("pytest", "in_progress", None),
            GitHubCheck("lint", "completed", "success"),
        ),
    )
    _patch_maintainer_facts(
        monkeypatch,
        mergeable_state="blocked",
        merge_block=_merge_block(
            head=maintainer_fixture.HEAD,
            required_checks=(GitHubCheck("pytest", "in_progress", None),),
        ),
    )

    result = maintainer_fixture._ship(env)

    assert result["result"] == {
        "action": "checks-pending",
        "reason": "checks-not-terminal-green",
        "pending_checks": ["pytest"],
        "required_pending_checks": ["pytest"],
        "head": maintainer_fixture.HEAD,
    }
    assert runtime.merge_calls == []
    assert maintainer_fixture._journal_row(env.state, env.run_id)["ship"] == before["ship"]


def test_maintainer_review_ship_keeps_required_review_block_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = maintainer_fixture._bootstrap_review_env(tmp_path)
    maintainer_fixture._set_ship(env, phase="needs_human", reason="copilot-review-timeout")
    runtime = maintainer_fixture._patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=maintainer_fixture._merge_forbidden,
        checks=(GitHubCheck("pytest", "in_progress", None),),
    )
    _patch_maintainer_facts(
        monkeypatch,
        mergeable_state="blocked",
        merge_block=_merge_block(
            head=maintainer_fixture.HEAD,
            review_decision="REVIEW_REQUIRED",
            required_checks=(GitHubCheck("pytest", "in_progress", None),),
        ),
    )

    with pytest.raises(RuntimeError, match=f"^{BLOCKED_MESSAGE}$"):
        maintainer_fixture._ship(env)

    assert runtime.merge_calls == []


def test_maintainer_review_ship_keeps_open_thread_fail_closed_while_required_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = maintainer_fixture._bootstrap_review_env(tmp_path)
    maintainer_fixture._set_ship(env, phase="needs_human", reason="copilot-review-timeout")
    runtime = maintainer_fixture._patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=maintainer_fixture._merge_forbidden,
        checks=(GitHubCheck("pytest", "in_progress", None),),
    )
    _patch_maintainer_facts(
        monkeypatch,
        mergeable_state="blocked",
        merge_block=_merge_block(
            head=maintainer_fixture.HEAD,
            required_checks=(GitHubCheck("pytest", "in_progress", None),),
        ),
        review_threads=(ReviewThread(thread_id="T1", resolved=False, outdated=False),),
    )

    with pytest.raises(
        RuntimeError, match=f"^{BLOCKED_MESSAGE}, review-thread-open$"
    ):
        maintainer_fixture._ship(env)

    assert runtime.merge_calls == []


# --- GitHub seam：blocked 時才多讀一次 GraphQL，綁定 exact HEAD -----------------


def _merge_block_payload(*, nodes=None, has_next_page=False, oid=client_fixture.HEAD, decision=None):
    if nodes is None:
        nodes = [
            {
                "__typename": "CheckRun",
                "name": "pytest",
                "status": "IN_PROGRESS",
                "conclusion": None,
                "isRequired": True,
            },
            {
                "__typename": "CheckRun",
                "name": "copilot-pull-request-reviewer",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "isRequired": False,
            },
            {
                "__typename": "StatusContext",
                "context": "legacy/lint",
                "state": "PENDING",
                "isRequired": True,
            },
            {
                "__typename": "StatusContext",
                "context": "legacy/docs",
                "state": "SUCCESS",
                "isRequired": True,
            },
        ]
    return {
        "data": {
            "repository": {
                "pullRequest": {"reviewDecision": decision},
                "object": {
                    "oid": oid,
                    "statusCheckRollup": {
                        "contexts": {
                            "nodes": nodes,
                            "pageInfo": {"hasNextPage": has_next_page},
                        }
                    },
                },
            }
        }
    }


class _BlockedRunner(client_fixture.FakeRunner):
    def __init__(self, *, merge_block_payload=None, mergeable_state="blocked"):
        super().__init__()
        self.merge_block_payload = (
            _merge_block_payload() if merge_block_payload is None else merge_block_payload
        )
        self.mergeable_state = mergeable_state

    def __call__(self, argv, **kwargs):
        endpoint = " ".join(argv)
        if endpoint == "gh api repos/acme/demo/pulls/7":
            self.calls.append((list(argv), kwargs))
            return client_fixture.Result(
                {
                    "head": {"sha": client_fixture.HEAD},
                    "base": {"ref": "main"},
                    "mergeable": True,
                    "mergeable_state": self.mergeable_state,
                }
            )
        if f"commits/{client_fixture.HEAD}/check-runs" in endpoint:
            self.calls.append((list(argv), kwargs))
            return client_fixture.PaginatedResult(
                [
                    {
                        "total_count": 2,
                        "check_runs": [
                            {"name": "pytest", "status": "in_progress", "conclusion": None},
                            {
                                "name": "copilot-pull-request-reviewer",
                                "status": "completed",
                                "conclusion": "success",
                            },
                        ],
                    }
                ]
            )
        if f"commits/{client_fixture.HEAD}/statuses" in endpoint:
            self.calls.append((list(argv), kwargs))
            return client_fixture.PaginatedResult(
                [[
                    {"context": "legacy/lint", "state": "pending"},
                    {"context": "legacy/docs", "state": "success"},
                ]]
            )
        if argv[:3] == ["gh", "api", "graphql"] and "statusCheckRollup" in endpoint:
            self.calls.append((list(argv), kwargs))
            return client_fixture.Result(self.merge_block_payload)
        return super().__call__(argv, **kwargs)


def _merge_block_calls(runner) -> list[list[str]]:
    return [
        argv
        for argv, _kwargs in runner.calls
        if argv[:3] == ["gh", "api", "graphql"] and "statusCheckRollup" in " ".join(argv)
    ]


def test_fetch_delivery_facts_reads_required_checks_only_when_blocked() -> None:
    runner = _BlockedRunner()

    facts = GitHubDeliveryClient(runner=runner).fetch_delivery_facts(
        repo="acme/demo", pr_number=7, change="unified-work-lifecycle"
    )

    assert facts.mergeable_state == "blocked"
    assert facts.merge_block == github_delivery.MergeBlockFacts(
        head=client_fixture.HEAD,
        review_decision=None,
        required_checks=(
            GitHubCheck("pytest", "in_progress", None),
            GitHubCheck("legacy/lint", "in_progress", None),
            GitHubCheck("legacy/docs", "completed", "success"),
        ),
        complete=True,
    )
    assert github_delivery.required_checks_pending_block(facts) == ("pytest", "legacy/lint")
    calls = _merge_block_calls(runner)
    assert len(calls) == 1
    argv = calls[0]
    # exact HEAD 以 raw string 傳入，避免 `-F` 的型別轉換；required 判定綁定這條 PR。
    head_arg = argv.index(f"head={client_fixture.HEAD}")
    assert argv[head_arg - 1] == "-f"
    assert "isRequired(pullRequestNumber:$number)" in " ".join(argv)
    assert all(call[1]["shell"] is False for call in runner.calls)

    clean = _BlockedRunner(mergeable_state="clean")
    clean_facts = GitHubDeliveryClient(runner=clean).fetch_delivery_facts(
        repo="acme/demo", pr_number=7, change="unified-work-lifecycle"
    )
    assert clean_facts.merge_block is None
    assert _merge_block_calls(clean) == []


def test_fetch_delivery_facts_marks_rollup_incomplete_and_keeps_review_decision() -> None:
    runner = _BlockedRunner(
        merge_block_payload=_merge_block_payload(has_next_page=True, decision="REVIEW_REQUIRED")
    )

    facts = GitHubDeliveryClient(runner=runner).fetch_delivery_facts(
        repo="acme/demo", pr_number=7, change="unified-work-lifecycle"
    )

    assert facts.merge_block.complete is False
    assert facts.merge_block.review_decision == "REVIEW_REQUIRED"
    assert github_delivery.required_checks_pending_block(facts) == ()


def test_fetch_delivery_facts_treats_missing_rollup_as_no_required_checks() -> None:
    payload = _merge_block_payload()
    payload["data"]["repository"]["object"]["statusCheckRollup"] = None
    runner = _BlockedRunner(merge_block_payload=payload)

    facts = GitHubDeliveryClient(runner=runner).fetch_delivery_facts(
        repo="acme/demo", pr_number=7, change="unified-work-lifecycle"
    )

    assert facts.merge_block.required_checks == ()
    assert github_delivery.required_checks_pending_block(facts) == ()


def _malformed_payloads() -> list[object]:
    base = _merge_block_payload()
    errors = {**base, "errors": [{"message": "boom"}]}
    no_object = _merge_block_payload()
    no_object["data"]["repository"]["object"] = None
    bad_required = _merge_block_payload(
        nodes=[{"__typename": "CheckRun", "name": "pytest", "status": "IN_PROGRESS", "conclusion": None, "isRequired": "yes"}]
    )
    bad_status = _merge_block_payload(
        nodes=[{"__typename": "CheckRun", "name": "pytest", "status": 3, "conclusion": None, "isRequired": True}]
    )
    bad_context = _merge_block_payload(
        nodes=[{"__typename": "StatusContext", "context": None, "state": "PENDING", "isRequired": True}]
    )
    unknown_type = _merge_block_payload(
        nodes=[{"__typename": "Mystery", "name": "pytest", "isRequired": True}]
    )
    bad_decision = _merge_block_payload(decision=7)
    bad_page = _merge_block_payload()
    bad_page["data"]["repository"]["object"]["statusCheckRollup"]["contexts"]["pageInfo"] = {
        "hasNextPage": "no"
    }
    return [errors, no_object, bad_required, bad_status, bad_context, unknown_type, bad_decision, bad_page]


@pytest.mark.parametrize(
    "payload",
    _malformed_payloads(),
    ids=[
        "graphql-errors",
        "head-object-missing",
        "is-required-not-bool",
        "status-not-string",
        "context-not-string",
        "unknown-context-type",
        "review-decision-not-string",
        "page-info-not-bool",
    ],
)
def test_fetch_delivery_facts_fails_closed_on_malformed_merge_block_facts(payload) -> None:
    with pytest.raises(RuntimeError, match="GitHub merge block facts"):
        GitHubDeliveryClient(runner=_BlockedRunner(merge_block_payload=payload)).fetch_delivery_facts(
            repo="acme/demo", pr_number=7, change="unified-work-lifecycle"
        )


# ---------------------------------------------------------------------------
# 2. 採信 Copilot review 後 check 跑超過 15 分鐘
# ---------------------------------------------------------------------------


def test_adopted_review_does_not_time_out_while_ship_waits_on_observation() -> None:
    loop = ReviewLoop(
        head=ship_fixture.HEAD,
        fix_rounds=0,
        epoch_started_at=3400.0,
        requested_at=1000.0,
        adopted_at=3400.0,
    )

    decision = loop.record_review(
        head=ship_fixture.HEAD,
        now_epoch=3400.0 + 2 * 60 * 60,
        finding_count=0,
        review_id=42,
        submitted_at_epoch=1000.0,
    )

    assert decision.action == "passed"
    assert decision.reason is None


@pytest.mark.parametrize(
    ("submitted_at", "observed_at"),
    [
        # 採信之後才出現、且晚於採信 + 15 分鐘的 review：與 remote gate 的 deadline 一致。
        (3400.0 + REVIEW_TIMEOUT_SECONDS + 1, 3400.0 + REVIEW_TIMEOUT_SECONDS + 2),
        # 觀測時間早於採信時間：時鐘異常仍 fail-closed。
        (1000.0, 3300.0),
    ],
    ids=["submitted-after-adoption-deadline", "observed-before-adoption"],
)
def test_adopted_review_keeps_real_deadline_and_clock_guards(
    submitted_at: float, observed_at: float
) -> None:
    loop = ReviewLoop(
        head=ship_fixture.HEAD,
        fix_rounds=0,
        epoch_started_at=3400.0,
        requested_at=1000.0,
        adopted_at=3400.0,
    )

    decision = loop.record_review(
        head=ship_fixture.HEAD,
        now_epoch=observed_at,
        finding_count=0,
        review_id=42,
        submitted_at_epoch=submitted_at,
    )

    assert decision.action == "needs_human"
    assert decision.reason == "copilot-review-timeout"


class _AdmittingGitHub:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.policy = None

    def evaluate_final_gate(self, **kwargs):
        self.calls.append("evaluate_final_gate")
        self.policy = kwargs["policy"]
        return kwargs["policy"]

    def commit_merge(self, **kwargs):
        self.calls.append("commit_merge")
        return object()


def _merge_if_ready(tmp_path: Path, *, github, copilot, now: float):
    return ShipOrchestrator(github=github, now=lambda: now).merge_if_ready(
        repo="acme/demo",
        pr_number=7,
        change="work",
        expected_head=orchestrator_fixture.HEAD1,
        expected_tree_hash=orchestrator_fixture.HEAD2,
        authority=orchestrator_fixture._authority(tmp_path, last_success=now),
        preflight=orchestrator_fixture._preflight(),
        copilot=copilot,
        foreign_review=orchestrator_fixture._foreign_review(tmp_path),
    )


def test_ship_orchestrator_admits_adopted_review_observed_long_after_adoption(
    tmp_path: Path,
) -> None:
    observed = 3_400.0 + 30 * 60
    copilot = orchestrator_fixture._adopted_copilot_decision(
        submitted_at_epoch=1_000.0,
        adopted_at_epoch=3_400.0,
        observed_at_epoch=observed,
    )
    assert copilot.action == "passed"
    github = _AdmittingGitHub()

    result = _merge_if_ready(tmp_path, github=github, copilot=copilot, now=observed)

    assert result.expected_head == orchestrator_fixture.HEAD1
    assert github.calls == ["evaluate_final_gate", "commit_merge"]
    assert github.policy.copilot_adopted_at_epoch == 3_400.0


def test_ship_orchestrator_rejects_adopted_review_submitted_past_adoption_deadline(
    tmp_path: Path,
) -> None:
    late = 3_400.0 + REVIEW_TIMEOUT_SECONDS + 1
    copilot = replace(
        orchestrator_fixture._adopted_copilot_decision(
            submitted_at_epoch=1_000.0,
            adopted_at_epoch=3_400.0,
            observed_at_epoch=late + 1,
        ),
        submitted_at_epoch=late,
    )

    class GitHub:
        def evaluate_final_gate(self, **kwargs):
            raise AssertionError("late adopted submission must not reach GitHub")

        def commit_merge(self, **kwargs):
            raise AssertionError("late adopted submission must not reach GitHub")

    with pytest.raises(RuntimeError, match="Copilot review epoch has not passed"):
        _merge_if_ready(tmp_path, github=GitHub(), copilot=copilot, now=late + 1)


def test_adopted_copilot_ship_keeps_waiting_past_review_window_then_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    running = (
        GitHubCheck("tests", "completed", "success"),
        GitHubCheck(COPILOT_CHECK, "in_progress", None),
    )
    pending_716._with_checks(lane, running)

    assert lane.ship(now=2000.0)["action"] == "checks-pending"
    row = pending_716._ship_row(lane)
    assert row["ship"]["adopted_review_id"] == 42
    assert row["ship"]["adopted_at_epoch"] == 2000.0

    late = lane.ship(now=2000.0 + REVIEW_TIMEOUT_SECONDS + 1)

    assert late == {
        "action": "checks-pending",
        "reason": "checks-not-terminal-green",
        "pending_checks": [COPILOT_CHECK],
        "head": ship_fixture.HEAD,
    }
    row = pending_716._ship_row(lane)
    assert row["ship"]["phase"] == "review-requested"
    assert row["ship"]["adopted_at_epoch"] == 2000.0
    run = lane.registry.get_workflow_run(lane.run_id)
    assert "needs_human" not in run.facets
    assert run.needs_human_reason is None

    pending_716._with_checks(lane, _all_green())

    merged = lane.ship(now=2000.0 + 20 * 60)

    assert merged == {"action": "merged-awaiting-closure", "head": ship_fixture.HEAD}
    row = pending_716._ship_row(lane)
    assert row["ship"]["merge_authorization"]["payload"]["copilot_review_id"] == 42
    assert row.get("repair_rounds", 0) == 0


def test_copilot_ship_without_any_review_still_times_out_as_review_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch, copilot_reviewed=False)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    pending_716._with_checks(
        lane,
        (GitHubCheck("tests", "in_progress", None),),
        copilot_reviews=(),
    )

    assert lane.ship(now=2000.0)["action"] == "awaiting-copilot"
    result = lane.ship(now=2000.0 + REVIEW_TIMEOUT_SECONDS + 1)

    assert result["action"] == "needs_human"
    assert result["reason"] == "copilot-review-timeout"


def test_adopted_copilot_ship_surfaces_checks_pending_timeout_after_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound = github_delivery.CHECKS_PENDING_TIMEOUT_SECONDS
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    pending_716._with_checks(lane, _required_check_running())

    assert lane.ship(now=2000.0)["action"] == "checks-pending"
    before = pending_716._ship_row(lane)["ship"]
    # 恰好到界線仍等待（與既有 timeout 一樣採 `>`）。
    assert lane.ship(now=2000.0 + bound)["action"] == "checks-pending"

    stopped = lane.ship(now=2000.0 + bound + 1)

    assert stopped["action"] == "needs_human"
    assert stopped["reason"] == "checks-pending-timeout"
    assert stopped["pending_checks"] == ["tests"]
    assert stopped["head"] == ship_fixture.HEAD
    assert stopped["next_actions"] == ["resume"]
    assert "cortex work resume" in stopped["next_step_hint"]
    assert work_bridge._delivery_adapter_status(stopped["action"]) == "needs_human"
    # ship state 不動：仍是同一筆採信 review 的 review-requested，沒有 merge authorization。
    assert pending_716._ship_row(lane)["ship"] == before
    assert pending_716._merge_authorization_files(tmp_path) == []
    run = lane.registry.get_workflow_run(lane.run_id)
    assert "needs_human" in run.facets
    assert run.needs_human_reason["reason"] == "checks-pending-timeout"
    assert run.needs_human_reason["context"]["pending_checks"] == "tests"
    assert not run.needs_human_reason["reason"].startswith("copilot-")

    # check 結束後 operator resume：同一 exact HEAD、同一筆 review 正常 merge。
    pending_716._with_checks(lane, _all_green())

    _operator_resume(lane, now=2000.0 + bound + 600)

    row = pending_716._ship_row(lane)
    assert row["ship"]["phase"] == "merged"
    assert row["ship"]["merge_authorization"]["payload"]["copilot_review_id"] == 42


def test_request_bound_copilot_ship_surfaces_checks_pending_timeout_after_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound = github_delivery.CHECKS_PENDING_TIMEOUT_SECONDS
    lane = ship_fixture._ship_lane(tmp_path, monkeypatch, copilot_reviewed=False)
    ship_fixture._open_pr_snapshot(lane.snapshot)
    assert lane.ship(now=2000.0)["action"] == "awaiting-copilot"
    lane.github.reviews = (
        CopilotReview(
            review_id=43,
            commit_id=ship_fixture.HEAD,
            state="COMMENTED",
            body="LGTM",
            author=COPILOT_REVIEWER_LOGIN,
            submitted_at_epoch=2100.0,
        ),
    )
    pending_716._with_checks(lane, _required_check_running())

    assert lane.ship(now=2200.0)["action"] == "checks-pending"
    assert lane.ship(now=2000.0 + bound)["action"] == "checks-pending"

    stopped = lane.ship(now=2000.0 + bound + 1)

    assert stopped["action"] == "needs_human"
    assert stopped["reason"] == "checks-pending-timeout"
    ship = pending_716._ship_row(lane)["ship"]
    assert ship["phase"] == "review-requested"
    assert "adopted_review_id" not in ship
    assert "merge_authorization" not in ship


def test_maintainer_review_ship_surfaces_checks_pending_timeout_after_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound = github_delivery.CHECKS_PENDING_TIMEOUT_SECONDS
    env = maintainer_fixture._bootstrap_review_env(tmp_path)
    maintainer_fixture._set_ship(env, phase="needs_human", reason="copilot-review-timeout")
    before = maintainer_fixture._journal_row(env.state, env.run_id)
    runtime = maintainer_fixture._patch_ship_runtime(
        monkeypatch,
        env,
        merge_callback=maintainer_fixture._merge_forbidden,
        checks=(GitHubCheck("pytest", "in_progress", None),),
    )
    reviewed_at = 205.0

    def ship(now: float):
        return work_actions.execute_work_action(
            args=maintainer_fixture._ship_args(env),
            requested_by="operator",
            snapshot_path=env.snapshot,
            state_path=env.state,
            now=lambda: now,
            workflow_registry=env.registry,
        )

    assert ship(reviewed_at + bound)["result"]["action"] == "checks-pending"

    stopped = ship(reviewed_at + bound + 1)["result"]

    assert stopped["action"] == "needs_human"
    assert stopped["reason"] == "checks-pending-timeout"
    assert stopped["pending_checks"] == ["pytest"]
    assert runtime.merge_calls == []
    assert maintainer_fixture._journal_row(env.state, env.run_id)["ship"] == before["ship"]
    run = env.registry.get_workflow_run(env.run_id)
    assert run.needs_human_reason["reason"] == "checks-pending-timeout"


def test_checks_pending_timeout_bound_is_documented_and_longer_than_review_window() -> None:
    bound = github_delivery.CHECKS_PENDING_TIMEOUT_SECONDS
    assert bound == 6 * 60 * 60
    assert bound > REVIEW_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "waiting_since", [None, float("nan"), float("inf"), True, "2000"]
)
def test_checks_pending_wait_requires_a_finite_waiting_anchor(waiting_since) -> None:
    facts = _blocked_facts(mergeable_state="clean", merge_block=None)
    gate = github_delivery.GateResult(allowed=False, reasons=("checks-not-terminal-green",))

    assert (
        work_actions._checks_pending_response(
            remote_gate=gate,
            remote=facts,
            head=ship_fixture.HEAD,
            now_epoch=2000.0,
            waiting_since_epoch=waiting_since,
            canonical_run=None,
            authority=None,
            workflow_registry=None,
        )
        is None
    )
