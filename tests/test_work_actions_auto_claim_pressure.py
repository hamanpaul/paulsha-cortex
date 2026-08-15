"""`#506` 迴歸測試：auto-claim scan 的 GitHub 請求壓力控制。

實測背景：`run_auto_claim_scan` 對每個 `confirmed_todo` authority 的每個 mapped
issue 各發一次即時 `gh api` 讀 label。cortex instance 有 57 個這樣的 issue，
`PSC_MANAGER_INTERVAL_SECONDS=30` 時等於 **114 次／分鐘的連發**，而 PR `#512` 的
`GitHubPressureGate` 只注入到 `monitor/providers.py`，coordinator 這一側完全不受
節流也不受退避管——monitor 在退避期間 manager 照打。實測把整個帳號推進 secondary
懲罰窗，`provider-authority-rate-limited-canonical` 因而擋下所有 claim。

本檔驗證兩件事：

1. **攤平**：連續的 issue 讀取之間插入設定的間隔（跨 authority 也算，因為壓力綁的
   是 token 不是 repo）。
2. **限流即停手**：一旦命中 rate-limit 型失敗就中止整輪掃描。舊行為是每個 authority
   各自撞一次才 break 自己那圈，於是限流期間每個 tick 仍送出 O(authorities) 次必定
   失敗的請求，每一次都在延長懲罰窗——「越限流越打、越打越限流」的正回饋。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.registry import JobRegistry


def _snapshot(path: Path, count: int = 3) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": f"demo-{index}",
                        "mapped_issues": [10 + index],
                        "mapped_prs": [],
                        "mapped_openspec": [f"demo-{index}"],
                        "mapped_todo_paths": [f"docs/todo-{index}.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": [
                            f"issue:{10 + index}@open",
                            f"openspec:demo-{index}@1",
                        ],
                    }
                    for index in range(1, count + 1)
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _ok_runner(argv, **kwargs):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"labels": [{"name": "cortex:auto-on-going"}]}),
        stderr="",
    )


def _rate_limited_runner(argv, **kwargs):
    return SimpleNamespace(
        returncode=1,
        stdout="",
        stderr=(
            "gh: API rate limit exceeded for user ID 1. "
            "If you reach out to GitHub Support for help ... (HTTP 403)"
        ),
    )


def test_issue_reads_are_throttled_across_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """三個 authority 各一個 issue ＝ 三次讀取，之間插入兩段間隔。

    間隔跨 authority 累計——secondary limit 綁 token 不綁 repo，per-authority
    重置節流等於沒有節流。
    """

    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "250")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    slept: list[float] = []

    work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=_ok_runner,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
        sleeper=slept.append,
    )

    assert slept == [0.25, 0.25], "第一次讀取前不睡，其後每次讀取前各睡一次"


def test_zero_interval_disables_throttle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "0")
    snapshot = _snapshot(tmp_path / "snapshot.json")
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    slept: list[float] = []

    work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=_ok_runner,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
        sleeper=slept.append,
    )

    assert slept == []


def test_rate_limit_aborts_whole_scan_after_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中限流後不得再對其餘 authority 發任何請求。"""

    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "0")
    snapshot = _snapshot(tmp_path / "snapshot.json", count=5)
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    calls: list[tuple[str, ...]] = []

    def counting_runner(argv, **kwargs):
        calls.append(tuple(argv))
        return _rate_limited_runner(argv, **kwargs)

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=counting_runner,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
        sleeper=lambda _seconds: None,
    )

    assert len(calls) == 1, "整輪只應送出一次請求就停手"
    assert result[0]["reason"] == "github-rate-limited"
    assert [row["reason"] for row in result[1:]] == [
        "github-rate-limited-scan-aborted"
    ] * 4
    assert registry.list_workflow_runs() == []


def test_non_rate_limit_failure_still_isolates_per_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非限流的讀取失敗維持舊語意：只擋該 authority，其餘照跑。"""

    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "0")
    snapshot = _snapshot(tmp_path / "snapshot.json", count=3)
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    def selective_runner(argv, **kwargs):
        if argv[-1].endswith("/issues/12"):
            return SimpleNamespace(returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")
        return _ok_runner(argv, **kwargs)

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=selective_runner,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
        sleeper=lambda _seconds: None,
    )

    by_work = {row["work_id"]: row for row in result}
    assert by_work["demo-2"]["reason"] == "github-label-read-failed"
    assert by_work["demo-1"]["action"] == "claim"
    assert by_work["demo-3"]["action"] == "claim"
    assert {run.work_id for run in registry.list_workflow_runs()} == {"demo-1", "demo-3"}


def test_rate_limited_scan_still_claims_authorities_needing_no_github_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停手只影響真的要打 GitHub 的 authority。

    沒有 mapped issue 的 authority 本來就不讀 label，限流與它無關——若連它也一併
    中止，等於讓一次無關的 403 停掉本來完全可行的派工。
    """

    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "0")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "with-issue",
                        "mapped_issues": [11],
                        "mapped_prs": [],
                        "mapped_openspec": ["with-issue"],
                        "mapped_todo_paths": ["docs/todo-1.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["issue:11@open", "openspec:with-issue@1"],
                    },
                    {
                        "repo": "acme/demo",
                        "work_id": "no-issue",
                        "mapped_issues": [],
                        "mapped_prs": [],
                        "mapped_openspec": ["no-issue"],
                        "mapped_todo_paths": ["docs/todo-2.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": ["openspec:no-issue@1"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=_rate_limited_runner,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
        sleeper=lambda _seconds: None,
    )

    by_work = {row["work_id"]: row for row in result}
    assert by_work["with-issue"]["reason"] == "github-rate-limited"
    # 沒有 mapped issue 的 authority 仍走完 _claim_action（此處因 auto_label 為 False
    # 而落在 needs_human——那是既有語意，與限流無關）；重點是它**沒有**被停手擋掉。
    assert by_work["no-issue"].get("reason") != "github-rate-limited-scan-aborted"
    assert by_work["no-issue"]["action"] == "needs_human"


def test_invalid_interval_env_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "-1")
    with pytest.raises(ValueError, match="PSC_MANAGER_GITHUB_INTERVAL_MS"):
        work_actions._auto_claim_github_interval_seconds()

    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "abc")
    with pytest.raises(ValueError, match="PSC_MANAGER_GITHUB_INTERVAL_MS"):
        work_actions._auto_claim_github_interval_seconds()


# ---------------------------------------------------------------------------
# R0.5 D1：鏡像優先——auto_label False 的 authority 零 API
# ---------------------------------------------------------------------------


def test_mirror_false_authorities_make_zero_github_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """鏡像（authority.auto_label）為 False 時不得發出任何 gh 呼叫。

    這是 D1 的核心承諾：先前的 O(mapped issues) sweep（實測 57 次/tick）
    降為 O(鏡像為 True 的 authority 數)，常態為零。
    """

    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "0")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": f"demo-{index}",
                        "mapped_issues": [10 + index],
                        "mapped_prs": [],
                        "mapped_openspec": [f"demo-{index}"],
                        "mapped_todo_paths": [f"docs/todo-{index}.md"],
                        "confirmed_todo": True,
                        "auto_label": False,
                        "source_revisions": [
                            f"issue:{10 + index}@open",
                            f"openspec:demo-{index}@1",
                        ],
                    }
                    for index in range(1, 4)
                ],
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    calls: list[tuple[str, ...]] = []

    def counting_runner(argv, **kwargs):
        calls.append(tuple(argv))
        return _ok_runner(argv, **kwargs)

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=counting_runner,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
        sleeper=lambda _s: None,
    )

    assert calls == [], "鏡像 False 不得有任何 GitHub 呼叫"
    # ignore 類決策不進 results（既有語意）：無可行動列＝正確
    assert result == []
    assert registry.list_workflow_runs() == []


def test_mirror_true_but_label_removed_live_declines_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """targeted 複驗以 live 為準：鏡像說有、live 已被人類移除 → 不 claim。"""

    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "0")
    snapshot = _snapshot(tmp_path / "snapshot.json", count=1)  # rows auto_label=True
    state = tmp_path / "runs.json"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")

    def no_label_runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"labels": []}), stderr="")

    result = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        runner=no_label_runner,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
        sleeper=lambda _s: None,
    )

    # live 複驗未過 → decide_auto_claim ignore（不進 results）、不得建立 run
    assert result == []
    assert registry.list_workflow_runs() == []
