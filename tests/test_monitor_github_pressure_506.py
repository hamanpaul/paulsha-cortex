"""#506：GitHub 掃描 burst 減壓——請求節流與 403 分診／退避的回歸鎖。

背景（issue #506）：monitor 的 ``_github_refresh_loop`` 每
``github_refresh_interval_seconds``（預設 300s）對 workspace 內每個 GitHub
repo 各跑一次 ``GitHubWorkProvider.scan()`` 與 ``GitHubTerminalProvider.scan()``。
兩者單輪的 ``gh`` 呼叫都不是 O(1)（issues ``--paginate``、PR graphql 分頁、
逐檔 contents、逐 PR compare），約 40 個 repo 的 workspace 一輪會在數秒內
齊發數百次請求，穩定觸發 GitHub secondary（abuse detection）rate limit，
``github:`` 與 ``github-terminal:`` 兩個 provider 同時 degraded 超過 35 分鐘，
operator 的 ``cortex work`` 全被 ``claim.py`` 的
``provider-authority-rate-limited-canonical`` 擋下。

本檔鎖三件事：
1. 節流：請求之間確實被攤平，可停用、有本輪總預算上限，且測試不真的 sleep。
2. 403 分診：``gh api rate_limit`` 探測（此端點不計入配額）分辨 primary／
   secondary，給不同 diagnostic。
3. 退避：secondary 命中後指數退避，退避期間 scan 不發任何請求；且**所有**
   rate-limit diagnostic 仍必須被 ``is_rate_limit_signal`` 認得，
   否則 ``claim.py`` 既有的 reason code 行為會回歸（#370 的成果）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.claim import (
    REASON_PROVIDER_RATE_LIMITED_CANONICAL,
    AuthorityValidationError,
    load_work_authority,
)
from paulsha_cortex.github_rate_limit import is_auth_signal, is_rate_limit_signal
from paulsha_cortex.monitor.config import load_config
from paulsha_cortex.monitor.github_pressure import GitHubPressureGate
from paulsha_cortex.monitor.providers import GitHubTerminalProvider, GitHubWorkProvider
from paulsha_cortex.monitor.work_api import WorkModelRefresher, WorkReadModelStore
from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore


SECONDARY_STDERR = (
    "HTTP 403: You have exceeded a secondary rate limit for the OAuth App "
    "associated with this personal access token. Please wait a few minutes "
    "before you try again."
)
PRIMARY_STDERR = (
    "HTTP 403: API rate limit exceeded for user ID 1. "
    "(https://docs.github.com/rest/overview/rate-limits-for-the-rest-api)"
)


class FakeClock:
    """可注入的 clock + sleeper：測試永遠不真的 sleep（節流全走假時間）。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedRunner:
    """依序回應的 fake runner；``rate_limit`` 探測有專屬回應槽。"""

    def __init__(self, results, *, rate_limit=None) -> None:
        self.results = list(results)
        self.rate_limit = rate_limit
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, timeout):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[-1] == "rate_limit":
            if self.rate_limit is None:
                raise AssertionError("未預期的 rate_limit 探測")
            return self.rate_limit
        if not self.results:
            raise AssertionError(f"未預期的第 {len(self.calls)} 次 gh 呼叫：{argv}")
        return self.results.pop(0)

    @property
    def api_calls(self) -> list[tuple[str, ...]]:
        return [argv for argv in self.calls if argv[-1] != "rate_limit"]


def _gate(clock: FakeClock, *, jitter: float = 0.0, **kwargs) -> GitHubPressureGate:
    return GitHubPressureGate(
        sleeper=clock.sleep,
        clock=clock,
        jitter_source=lambda: jitter,
        **kwargs,
    )


def _completed(payload, *, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr=stderr,
    )


def _issues(*rows):
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=0,
        stdout="\n".join(json.dumps(row) for row in rows) + "\n",
        stderr="",
    )


_ISSUE_ROW = {
    "number": 506,
    "title": "monitor github burst",
    "state": "open",
    "node_id": "ISSUE_node",
    "updated_at": "2026-08-13T10:00:00Z",
}

_GRAPH = {
    "data": {
        "repository": {
            "defaultBranchRef": {"name": "main", "target": {"oid": "d" * 40}},
            "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        }
    }
}
_TREE = {"truncated": False, "tree": []}


def _rate_limit_payload(*, remaining: int, reset: int = 1_800_000_000):
    return _completed(
        {
            "resources": {
                "core": {"limit": 5000, "remaining": remaining, "reset": reset},
                "graphql": {"limit": 5000, "remaining": 5000, "reset": reset},
            },
            "rate": {"limit": 5000, "remaining": remaining, "reset": reset},
        }
    )


# --------------------------------------------------------------------------
# A. 請求節流／攤平
# --------------------------------------------------------------------------


def test_throttle_spaces_each_request_instead_of_bursting():
    clock = FakeClock()
    runner = ScriptedRunner([_completed(_GRAPH), _completed(_TREE)])

    result = GitHubTerminalProvider(
        "example/acme",
        runner=runner,
        pressure_gate=_gate(clock, interval_seconds=0.2, jitter_seconds=0.1),
    ).scan()

    assert result.status == "ok"
    # 兩次 gh 請求 → 兩段節流間隔（jitter_source 固定 0 → 純 interval）。
    assert clock.sleeps == [0.2, 0.2]


def test_throttle_jitter_is_added_on_top_of_interval():
    clock = FakeClock()
    runner = ScriptedRunner([_completed(_GRAPH), _completed(_TREE)])

    GitHubTerminalProvider(
        "example/acme",
        runner=runner,
        pressure_gate=_gate(clock, jitter=1.0, interval_seconds=0.2, jitter_seconds=0.1),
    ).scan()

    assert clock.sleeps == pytest.approx([0.3, 0.3])


def test_throttle_zero_interval_disables_throttling():
    clock = FakeClock()
    runner = ScriptedRunner([_issues(_ISSUE_ROW)])

    result = GitHubWorkProvider(
        "example/acme",
        runner=runner,
        pressure_gate=_gate(clock, interval_seconds=0.0, jitter_seconds=0.0),
    ).scan()

    assert result.status == "ok"
    assert clock.sleeps == []


def test_throttle_budget_caps_total_sleep_and_resets_per_cycle():
    """極端情況（repo 數暴增）不得讓節流本身吃掉整個 refresh interval。"""
    clock = FakeClock()
    gate = _gate(clock, interval_seconds=0.2, jitter_seconds=0.0, budget_seconds=0.25)
    runner = ScriptedRunner([_completed(_GRAPH), _completed(_TREE)])

    GitHubTerminalProvider("example/acme", runner=runner, pressure_gate=gate).scan()

    # 第二次只剩 0.05 預算，之後即使還有請求也不再 sleep。
    assert clock.sleeps == pytest.approx([0.2, 0.05])

    gate.begin_cycle()
    runner = ScriptedRunner([_completed(_GRAPH), _completed(_TREE)])
    GitHubTerminalProvider("example/acme", runner=runner, pressure_gate=gate).scan()

    assert clock.sleeps == pytest.approx([0.2, 0.05, 0.2, 0.05])


def test_throttle_budget_never_exceeds_half_the_refresh_interval(tmp_path: Path):
    """config 的 budget 不得超過一輪 refresh interval 的一半（見 #506 預算計算）。"""
    config = tmp_path / "project-cortex.yaml"
    config.write_text(
        "workspaces:\n"
        "  - name: demo\n"
        "    path: /tmp/demo\n"
        "monitor:\n"
        "  github_refresh_interval_seconds: 60\n"
        "  github_throttle_budget_seconds: 120\n",
        encoding="utf-8",
    )

    cfg = load_config(config_path=config)

    assert cfg.github_throttle_budget_seconds == 120
    assert cfg.github_throttle_budget() == 30.0


def test_monitor_config_parses_throttle_settings(tmp_path: Path):
    config = tmp_path / "project-cortex.yaml"
    config.write_text(
        "workspaces:\n"
        "  - name: demo\n"
        "    path: /tmp/demo\n"
        "monitor:\n"
        "  github_request_interval_ms: 0\n"
        "  github_request_jitter_ms: 50\n"
        "  github_backoff_base_seconds: 30\n"
        "  github_backoff_max_seconds: 900\n",
        encoding="utf-8",
    )

    cfg = load_config(config_path=config)

    assert cfg.github_request_interval_ms == 0
    assert cfg.github_request_jitter_ms == 50
    assert cfg.github_backoff_base_seconds == 30
    assert cfg.github_backoff_max_seconds == 900


@pytest.mark.parametrize(
    "field, value",
    [
        ("github_request_interval_ms", -1),
        ("github_request_jitter_ms", -5),
        ("github_throttle_budget_seconds", 0),
        ("github_backoff_base_seconds", 0),
    ],
)
def test_monitor_config_rejects_invalid_throttle_settings(tmp_path: Path, field: str, value: int):
    config = tmp_path / "project-cortex.yaml"
    config.write_text(
        "workspaces:\n"
        "  - name: demo\n"
        "    path: /tmp/demo\n"
        f"monitor:\n  {field}: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_config(config_path=config)


def test_default_throttle_budget_fits_a_forty_repo_cycle():
    """預算計算（#506）：40 repo × 約 5 次呼叫 × 0.2s ≈ 40s ≪ 300s。"""
    gate = GitHubPressureGate()

    assert gate.interval_seconds * 40 * 5 < 300
    assert gate.budget_seconds <= 150


# --------------------------------------------------------------------------
# B. 403 分診（primary vs secondary）
# --------------------------------------------------------------------------


def test_secondary_rate_limit_is_triaged_via_rate_limit_endpoint():
    clock = FakeClock()
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=SECONDARY_STDERR)],
        rate_limit=_rate_limit_payload(remaining=4_900),
    )

    result = GitHubWorkProvider(
        "example/acme", runner=runner, pressure_gate=_gate(clock)
    ).scan()

    assert result.status == "degraded"
    assert any("secondary rate limit" in item for item in result.diagnostics)
    assert not any("primary" in item for item in result.diagnostics)
    # 探測走的是不計配額的 rate_limit 端點。
    assert runner.calls[-1] == ("gh", "api", "--method", "GET", "rate_limit")


def test_primary_quota_exhaustion_is_reported_apart_from_secondary():
    clock = FakeClock()
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=PRIMARY_STDERR)],
        rate_limit=_rate_limit_payload(remaining=0),
    )

    result = GitHubWorkProvider(
        "example/acme", runner=runner, pressure_gate=_gate(clock)
    ).scan()

    assert result.status == "degraded"
    assert any("primary rate limit exhausted" in item for item in result.diagnostics)
    assert not any("secondary" in item for item in result.diagnostics)


def test_probe_failure_falls_back_to_generic_rate_limit_diagnostic():
    clock = FakeClock()
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=SECONDARY_STDERR)],
        rate_limit=_completed({}, returncode=1, stderr="gh: rate_limit unavailable"),
    )

    result = GitHubWorkProvider(
        "example/acme", runner=runner, pressure_gate=_gate(clock)
    ).scan()

    assert result.status == "degraded"
    assert any("github rate limit exceeded" in item for item in result.diagnostics)


def test_auth_failure_does_not_trigger_the_rate_limit_probe():
    clock = FakeClock()
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr="HTTP 401: Bad credentials")]
    )

    result = GitHubWorkProvider(
        "example/acme", runner=runner, pressure_gate=_gate(clock)
    ).scan()

    assert any("authentication" in item for item in result.diagnostics)
    assert all(argv[-1] != "rate_limit" for argv in runner.calls)


@pytest.mark.parametrize(
    "stderr, remaining",
    [(SECONDARY_STDERR, 4_900), (PRIMARY_STDERR, 0)],
)
def test_triaged_diagnostics_remain_rate_limit_signals(stderr: str, remaining: int):
    """#370 回歸鎖：primary／secondary 都必須維持 ``is_rate_limit_signal`` 為真，
    否則 ``claim.py`` 的 ``provider-authority-rate-limited-canonical`` 會消失。"""
    clock = FakeClock()
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=stderr)],
        rate_limit=_rate_limit_payload(remaining=remaining),
    )

    result = GitHubWorkProvider(
        "example/acme", runner=runner, pressure_gate=_gate(clock)
    ).scan()

    assert result.diagnostics
    assert all(is_rate_limit_signal(item) for item in result.diagnostics)
    assert not any(
        is_auth_signal(item) and not is_rate_limit_signal(item)
        for item in result.diagnostics
    )


def test_claim_still_returns_the_rate_limited_reason_code(tmp_path: Path):
    """端到端鎖：新 diagnostic 落進 durable snapshot 後，claim.py 的
    canonical reason code 必須維持 ``provider-authority-rate-limited-canonical``。"""
    clock = FakeClock()
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=SECONDARY_STDERR)],
        rate_limit=_rate_limit_payload(remaining=4_900),
    )
    result = GitHubWorkProvider(
        "acme/demo", runner=runner, pressure_gate=_gate(clock)
    ).scan()

    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github:acme/demo": {
                        "status": "degraded",
                        "revision": "gh-rev-1",
                        "last_success_at": "2026-08-13T11:19:26Z",
                        "diagnostics": list(result.diagnostics),
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "rate-limited-work",
                        "sources": [
                            {
                                "confidence": "confirmed",
                                "kind": "todo",
                                "ref": "docs/todo.md",
                                "source_id": "todo:rate-limited-work",
                                "revision": "todo-rev-1",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(
            repo="acme/demo", work_id="rate-limited-work", snapshot_path=snapshot
        )

    assert excinfo.value.reason_code == REASON_PROVIDER_RATE_LIMITED_CANONICAL


# --------------------------------------------------------------------------
# C. 退避（退避期間不發請求）
# --------------------------------------------------------------------------


def test_backoff_window_skips_scan_without_issuing_requests():
    clock = FakeClock()
    gate = _gate(clock, backoff_base_seconds=60.0)
    first_runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=SECONDARY_STDERR)],
        rate_limit=_rate_limit_payload(remaining=4_900),
    )
    GitHubWorkProvider("example/acme", runner=first_runner, pressure_gate=gate).scan()

    second_runner = ScriptedRunner([])
    result = GitHubWorkProvider(
        "example/acme", runner=second_runner, pressure_gate=gate
    ).scan()

    assert second_runner.calls == []
    assert result.status == "degraded"
    assert any("backoff" in item for item in result.diagnostics)
    assert all(is_rate_limit_signal(item) for item in result.diagnostics)


def test_backoff_is_account_scoped_across_repos_and_providers():
    """GitHub 的 secondary limit 綁 token 而非 repo：若退避只綁單一 provider，
    40 個 repo 會各燒一次 403，減壓等於沒做。"""
    clock = FakeClock()
    gate = _gate(clock)
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=SECONDARY_STDERR)],
        rate_limit=_rate_limit_payload(remaining=4_900),
    )
    GitHubWorkProvider("example/acme", runner=runner, pressure_gate=gate).scan()

    other_runner = ScriptedRunner([])
    other = GitHubWorkProvider(
        "example/other", runner=other_runner, pressure_gate=gate
    ).scan()
    terminal_runner = ScriptedRunner([])
    terminal = GitHubTerminalProvider(
        "example/acme", runner=terminal_runner, pressure_gate=gate
    ).scan()

    assert other_runner.calls == []
    assert terminal_runner.calls == []
    assert other.status == terminal.status == "degraded"


def test_scan_resumes_after_the_backoff_window_expires():
    clock = FakeClock()
    gate = _gate(clock, backoff_base_seconds=60.0)
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=SECONDARY_STDERR)],
        rate_limit=_rate_limit_payload(remaining=4_900),
    )
    GitHubWorkProvider("example/acme", runner=runner, pressure_gate=gate).scan()

    clock.advance(61.0)
    recovered_runner = ScriptedRunner([_issues(_ISSUE_ROW)])
    result = GitHubWorkProvider(
        "example/acme", runner=recovered_runner, pressure_gate=gate
    ).scan()

    assert result.status == "ok"
    assert recovered_runner.api_calls


def test_consecutive_secondary_hits_back_off_exponentially_with_a_ceiling():
    clock = FakeClock()
    gate = _gate(clock, backoff_base_seconds=60.0, backoff_max_seconds=200.0)

    delays = []
    for _ in range(4):
        delays.append(gate.note_rate_limited(kind="secondary"))
        clock.advance(delays[-1] + 1.0)

    assert delays == [60.0, 120.0, 200.0, 200.0]


def test_successful_scan_clears_backoff_state():
    clock = FakeClock()
    gate = _gate(clock, backoff_base_seconds=60.0)
    gate.note_rate_limited(kind="secondary")
    clock.advance(61.0)

    runner = ScriptedRunner([_issues(_ISSUE_ROW)])
    assert (
        GitHubWorkProvider(
            "example/acme", runner=runner, pressure_gate=gate
        ).scan().status
        == "ok"
    )

    # 成功後重新計數：下一次 secondary 回到 base，而不是延續指數。
    assert gate.note_rate_limited(kind="secondary") == 60.0


def test_retry_after_wins_over_exponential_backoff():
    clock = FakeClock()
    gate = _gate(clock, backoff_base_seconds=60.0, backoff_max_seconds=1800.0)
    runner = ScriptedRunner(
        [
            _completed(
                {},
                returncode=1,
                stderr=SECONDARY_STDERR + "\nRetry-After: 300\n",
            )
        ],
        rate_limit=_rate_limit_payload(remaining=4_900),
    )

    GitHubWorkProvider("example/acme", runner=runner, pressure_gate=gate).scan()

    assert gate.blocked_seconds() == pytest.approx(300.0)


def test_terminal_provider_rate_limit_is_classified_and_backed_off():
    """終局 provider 是請求量最大的一支（graphql + tree + 逐檔 contents），
    它若不參與退避，減壓形同虛設。"""
    clock = FakeClock()
    gate = _gate(clock)
    runner = ScriptedRunner(
        [_completed({}, returncode=1, stderr=SECONDARY_STDERR)],
        rate_limit=_rate_limit_payload(remaining=4_900),
    )

    result = GitHubTerminalProvider(
        "example/acme", runner=runner, pressure_gate=gate, retry_delays=()
    ).scan()

    assert result.status == "degraded"
    assert any("secondary rate limit" in item for item in result.diagnostics)
    assert all(is_rate_limit_signal(item) for item in result.diagnostics)
    assert gate.blocked_seconds() > 0


def test_terminal_provider_non_rate_limit_failure_keeps_existing_diagnostic():
    """既有行為不得回歸：非 rate-limit 失敗仍是 evidence unavailable，且不退避。"""
    clock = FakeClock()
    gate = _gate(clock)
    runner = ScriptedRunner(
        [_completed({"message": "bad credentials"}, returncode=1, stderr="gh: bad credentials (HTTP 401)")]
    )

    result = GitHubTerminalProvider(
        "example/acme", runner=runner, pressure_gate=gate, retry_delays=()
    ).scan()

    assert result.status == "degraded"
    assert result.diagnostics == ("github terminal evidence unavailable",)
    assert gate.blocked_seconds() == 0.0


def test_refresher_resets_the_throttle_budget_once_per_github_cycle(tmp_path: Path):
    """節流預算是「每輪」的：只有 ``include_github=True`` 的那條迴圈算一輪。"""

    class _SpyGate(GitHubPressureGate):
        def __init__(self) -> None:
            super().__init__()
            self.cycles = 0

        def begin_cycle(self) -> None:
            self.cycles += 1
            super().begin_cycle()

    gate = _SpyGate()
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "state/work-items.snapshot.json"),
        read_store=WorkReadModelStore.empty(),
        github_pressure_gate=gate,
    )

    refresher.refresh((), include_github=False)
    assert gate.cycles == 0

    refresher.refresh((), include_github=True)
    refresher.refresh((), include_github=True)
    assert gate.cycles == 2


def test_provider_without_gate_behaves_exactly_as_before():
    """gate 可停用（不注入即完全沒有節流／退避），既有呼叫端不受影響。"""
    runner = ScriptedRunner([_issues(_ISSUE_ROW)])

    result = GitHubWorkProvider("example/acme", runner=runner).scan()

    assert result.status == "ok"
    assert len(runner.calls) == 1
