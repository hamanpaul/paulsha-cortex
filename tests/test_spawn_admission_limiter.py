"""issue #381 回歸測試：per-provider spawn admission limiter。

背景：`autonomy.dispatch_ready` 的 fan-out 迴圈與 workflow lane（periodic tick
逐一 resume ongoing run）背靠背 spawn 同一 provider 的多個 executor——copilot
啟動時連續探測 GitHub `/user` 約 6-7 次；三個 slice 併發即打爆該 quota
bucket（與 core rate_limit 分離，既有診斷看不到），三個 builder 在同一秒全部
`builder-failed`。

本檔涵蓋三層：
1. `SpawnAdmissionLimiter` 本體（per-provider 最小間隔，注入 clock/sleep，
   不用真的 wall-clock sleep）。
2. `resolve_provider`／`resolve_limiter`／`build_default_limiter` 這幾個
   決定「這次 spawn 記在哪個 bucket」「未注入時等於沒接線」「production 預設
   怎麼從 CLI/env 組出來」的輔助函式。
3. 兩條 lane 的實際接線：`autonomy.dispatch_ready`（fanout）與
   `manager._dispatch_workflow_card` / `manager.dispatch_workflow_card`
   （workflow lane 的實際 spawn 點）、以及 `manager_daemon.build_periodic_tick_runner`
   把同一個 limiter instance 同時餵給 workflow resume 迴圈與 run_tick
   （驗證兩條 lane 共用同一把節流器，而非各自獨立、對同一 provider quota
   毫無協調）。

第 3 節的測試在接線完成前必然 FAIL（RED）：`dispatch_ready`／
`dispatch_workflow_card`／`build_periodic_tick_runner` 目前都不接受
`spawn_admission` 參數，呼叫會直接 TypeError；接線後（GREEN）則驗證同一
provider 的 spawn 之間確實錯開、不同 provider 之間不互相拖慢。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, manager_daemon
from paulsha_cortex.coordinator.autonomy import dispatch_ready
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.spawn_admission import (
    DEFAULT_MIN_INTERVAL_SECONDS,
    SpawnAdmissionLimiter,
    build_default_limiter,
    resolve_limiter,
    resolve_provider,
)


# --------------------------------------------------------------------------- #
# 共用 fixture：fake clock（sleep 只推進虛擬時鐘，從不真的等待）
# --------------------------------------------------------------------------- #
class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.value += seconds


# =============================================================================
# 1) SpawnAdmissionLimiter 本體
# =============================================================================
def test_first_admit_for_a_provider_does_not_wait() -> None:
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)

    waited = limiter.admit("copilot")

    assert waited == 0.0
    assert clock.sleep_calls == []


def test_second_admit_within_interval_waits_remaining_gap() -> None:
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)

    limiter.admit("copilot")
    clock.value = 0.5  # 只過了 0.5 秒，離 2 秒的最小間隔還差 1.5 秒
    waited = limiter.admit("copilot")

    assert waited == 1.5
    assert clock.sleep_calls == [1.5]
    assert clock.value == 2.0  # sleep 已把虛擬時鐘推到「可 admit」的時刻


def test_second_admit_after_interval_elapsed_does_not_wait() -> None:
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)

    limiter.admit("copilot")
    clock.value = 5.0  # 已超過最小間隔
    waited = limiter.admit("copilot")

    assert waited == 0.0
    assert clock.sleep_calls == []


def test_different_providers_have_independent_timelines() -> None:
    """#381 覆驗重點：不能全域序列化，不同 provider 互不阻塞。"""
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)

    limiter.admit("copilot")
    waited = limiter.admit("codex")  # 同一時刻，不同 provider

    assert waited == 0.0
    assert clock.sleep_calls == []


def test_per_provider_override_replaces_default_interval() -> None:
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(
        min_interval_seconds=2.0,
        provider_overrides={"copilot": 5.0, "codex": 0.0},
        clock=clock.now,
        sleep=clock.sleep,
    )

    limiter.admit("copilot")
    limiter.admit("codex")
    waited_copilot = limiter.admit("copilot")  # 覆寫的 5 秒間隔尚未跑滿
    waited_codex = limiter.admit("codex")  # 覆寫成 0 秒 → 從不等待

    assert waited_copilot == 5.0
    assert waited_codex == 0.0


def test_zero_interval_never_waits_across_many_calls() -> None:
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=0.0, clock=clock.now, sleep=clock.sleep)

    waits = [limiter.admit("copilot") for _ in range(5)]

    assert waits == [0.0] * 5
    assert clock.sleep_calls == []


def test_back_to_back_same_provider_calls_reserve_before_sleeping() -> None:
    """Reservation 在鎖內完成：即使 clock 在兩次 admit 之間完全沒推進
    （模擬並發呼叫的最壞狀況），第二次仍要等滿整個 interval，而不是讀到
    同一個「上次時刻」算出 0 等待。"""
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)

    limiter.admit("copilot")
    waited = limiter.admit("copilot")

    assert waited == 2.0


def test_negative_configured_interval_is_clamped_to_zero() -> None:
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=-3.0, clock=clock.now, sleep=clock.sleep)

    assert limiter.min_interval_seconds == 0.0
    assert limiter.admit("copilot") == 0.0


# =============================================================================
# 2) resolve_provider / resolve_limiter / build_default_limiter
# =============================================================================
def test_resolve_provider_prefers_identity_executor() -> None:
    identity = SimpleNamespace(executor="codex")
    provider = resolve_provider(identity=identity, executor="copilot", launcher=SimpleNamespace(executor="claude"))
    assert provider == "codex"


def test_resolve_provider_falls_back_to_declared_executor_without_identity() -> None:
    provider = resolve_provider(identity=None, executor="copilot", launcher=SimpleNamespace(executor="claude"))
    assert provider == "copilot"


def test_resolve_provider_falls_back_to_launcher_executor_attribute() -> None:
    provider = resolve_provider(identity=None, executor=None, launcher=SimpleNamespace(executor="claude"))
    assert provider == "claude"


def test_resolve_provider_falls_back_to_default_bucket_when_nothing_known() -> None:
    provider = resolve_provider(identity=None, executor=None, launcher=object())
    assert provider == "default"


def test_resolve_provider_ignores_empty_string_identity_executor() -> None:
    identity = SimpleNamespace(executor="")
    provider = resolve_provider(identity=identity, executor="copilot", launcher=None)
    assert provider == "copilot"


def test_resolve_limiter_returns_injected_instance_unchanged() -> None:
    limiter = SpawnAdmissionLimiter()
    assert resolve_limiter(limiter) is limiter


def test_resolve_limiter_none_is_a_true_noop_not_just_free_first_call() -> None:
    """None 必須解析成永遠零等待，不是「單例、第一次不用等」的假安全。"""
    limiter = resolve_limiter(None)

    waits = [limiter.admit("copilot") for _ in range(10)]

    assert waits == [0.0] * 10


def test_resolve_limiter_none_never_calls_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import paulsha_cortex.coordinator.spawn_admission as spawn_admission_module

    def _forbidden_sleep(_seconds: float) -> None:
        raise AssertionError("resolve_limiter(None) 不應觸發任何真的 sleep")

    monkeypatch.setattr(spawn_admission_module.time, "sleep", _forbidden_sleep)

    limiter = resolve_limiter(None)
    for _ in range(5):
        limiter.admit("copilot")  # 若曾誤用真正的 time.sleep，這裡會 AssertionError


def test_build_default_limiter_uses_builtin_default_with_empty_env() -> None:
    limiter = build_default_limiter(env={})
    assert limiter.min_interval_seconds == DEFAULT_MIN_INTERVAL_SECONDS


def test_build_default_limiter_explicit_arg_wins_over_env() -> None:
    limiter = build_default_limiter(
        min_interval_seconds=9.0,
        env={"PSC_SPAWN_MIN_INTERVAL_SECONDS": "1.0"},
    )
    assert limiter.min_interval_seconds == 9.0


def test_build_default_limiter_reads_env_default() -> None:
    limiter = build_default_limiter(env={"PSC_SPAWN_MIN_INTERVAL_SECONDS": "7.5"})
    assert limiter.min_interval_seconds == 7.5


def test_build_default_limiter_reads_per_provider_env_override() -> None:
    limiter = build_default_limiter(
        env={
            "PSC_SPAWN_MIN_INTERVAL_SECONDS": "2.0",
            "PSC_SPAWN_MIN_INTERVAL_SECONDS__COPILOT": "6.0",
        }
    )
    assert limiter.min_interval_seconds == 2.0
    assert limiter.interval_for("copilot") == 6.0
    assert limiter.interval_for("codex") == 2.0


def test_build_default_limiter_ignores_malformed_env_values() -> None:
    limiter = build_default_limiter(
        env={
            "PSC_SPAWN_MIN_INTERVAL_SECONDS": "not-a-number",
            "PSC_SPAWN_MIN_INTERVAL_SECONDS__COPILOT": "also-not-a-number",
        }
    )
    assert limiter.min_interval_seconds == DEFAULT_MIN_INTERVAL_SECONDS
    assert limiter.interval_for("copilot") == DEFAULT_MIN_INTERVAL_SECONDS


# =============================================================================
# 3) Wiring：兩條 lane 實際接上同一個 limiter
# =============================================================================
def _meta(
    slice_id: str,
    *,
    dispatch: str = "auto",
    plan: str = "docs/superpowers/plans/example.md",
    depends_on: list[str] | None = None,
    executor: str | None = None,
    model_id: str | None = None,
) -> dict:
    spec_path = f"/specs/{slice_id}.md"
    return {
        "path": spec_path,
        "dispatch": dispatch,
        "slice_id": slice_id,
        "plan": plan,
        "depends_on": list(depends_on or []),
        "target_branch": "main",
        "verification": {
            "docs_class": "code",
            "review_policy": "required",
            "required_artifacts": [],
            "checks": [{"kind": "persona-scope"}],
            "tests": [],
            "full_suite": {
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 300,
                "baseline": "no-regression",
            },
        },
        "parse_error": None,
        "executor": executor,
        "model_id": model_id,
        "_pinned_inputs": {
            "spec_path": spec_path,
            "spec_hash": "0" * 64,
            "plan_path": plan,
            "plan_hash": "1" * 64,
            "target_branch": "main",
            "target_remote": "origin",
            "verification_hash": "2" * 64,
        },
    }


def _identity_registry() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {"executor": "codex", "model_id": "gpt-5.4-codex", "independence_domain": "openai"},
            {"executor": "copilot", "model_id": "claude-haiku-4.5", "independence_domain": "anthropic"},
        ]
    )


def _fake_git_runner(args: list[str]):
    if not args:
        return ""
    if args[0] == "rev-parse":
        return "f" * 40
    if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
        return ""
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        return "f" * 40
    if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
        return ""
    return ""


class _FakeRegistry:
    def __init__(self) -> None:
        self._jobs: list[dict] = []
        self._seq = 0
        self._slices: list[dict] = []

    def list_jobs(self) -> list[dict]:
        return [dict(job) for job in self._jobs]

    def create_job(self, *, task, persona, branch, pane, worktree, dispatch_head=None, executor=None,
                    session_name=None, pid=None, log_path=None, exit_code=None, kind="build",
                    model_id=None, independence_domain=None, subject_head=None, spec_hash=None,
                    plan_hash=None, verification_hash=None, workflow_repo=None) -> dict:
        self._seq += 1
        job = {
            "job_id": f"{task}-{self._seq}", "task": task, "persona": persona, "kind": kind,
            "branch": branch, "pane": pane, "worktree": worktree, "status": "dispatched",
            "dispatch_head": dispatch_head, "executor": executor, "model_id": model_id,
            "independence_domain": independence_domain, "session_name": session_name, "pid": pid,
            "log_path": log_path, "exit_code": exit_code, "subject_head": subject_head,
            "spec_hash": spec_hash, "plan_hash": plan_hash, "verification_hash": verification_hash,
            "workflow_repo": workflow_repo,
        }
        self._jobs.append(job)
        return dict(job)

    def attach_launch_handle(self, job_id, *, executor=None, model_id=None, session_name=None,
                              pid=None, log_path=None, template_instance=None) -> dict:
        for job in self._jobs:
            if job["job_id"] == job_id:
                job["executor"] = executor
                if model_id is not None:
                    job["model_id"] = model_id
                job["session_name"] = session_name
                job["pid"] = pid
                job["log_path"] = log_path
                job["template_instance"] = template_instance
                return dict(job)
        raise KeyError(job_id)

    def update_status(self, job_id, status) -> dict:
        for job in self._jobs:
            if job["job_id"] == job_id:
                job["status"] = status
                return dict(job)
        raise KeyError(job_id)

    def create_slice(self, *, slice_id, spec_path, spec_hash, plan_path, plan_hash, target_branch,
                      target_remote="origin", verification_hash=None, verification=None,
                      dispatch_base=None, builder_job_id=None, reviewer_job_id=None, candidate=None) -> dict:
        row = {
            "slice_id": slice_id, "spec": {"path": spec_path, "hash": spec_hash},
            "plan": {"path": plan_path, "hash": plan_hash}, "target_branch": target_branch,
            "target_remote": target_remote,
            "verification": {"hash": verification_hash or ("0" * 64), "contract": verification},
            "dispatch_base": dispatch_base, "builder_job_id": builder_job_id,
            "reviewer_job_id": reviewer_job_id, "candidate": candidate, "state": "pending",
            "gate_state": "pending", "actions": [],
        }
        self._slices.append(row)
        return dict(row)

    def update_slice(self, slice_id, **updates) -> dict:
        for row in self._slices:
            if row["slice_id"] == slice_id:
                for key, value in updates.items():
                    if value is None:
                        continue
                    if key == "verification_hash":
                        row["verification"]["hash"] = value
                    else:
                        row[key] = value
                return dict(row)
        raise KeyError(slice_id)

    def record_action(self, slice_id, **kwargs) -> dict:
        for row in self._slices:
            if row["slice_id"] == slice_id:
                row["actions"].append(dict(kwargs))
                return dict(row)
        raise KeyError(slice_id)

    def get_slice(self, slice_id) -> dict:
        for row in self._slices:
            if row["slice_id"] == slice_id:
                return dict(row)
        raise KeyError(slice_id)


class _FakeDispatcher:
    def __init__(self, registry: _FakeRegistry) -> None:
        self._registry = registry
        self._git_runner = _fake_git_runner


class _RecordingLauncher:
    """記錄每次 launch 呼叫當下的（fake）時鐘讀數，供斷言 spawn 是否錯開。"""

    def __init__(self, *, executor: str, model_id: str | None, clock: _FakeClock) -> None:
        self.executor = executor
        self.model_id = model_id
        self._clock = clock
        self.calls: list[str] = []
        self.spawn_times: list[float] = []
        self.commit_capability_requests = 0

    def as_commit_required(self):
        self.commit_capability_requests += 1
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str):
        from paulsha_cortex.coordinator.launcher import LaunchHandle

        self.calls.append(slice_id)
        self.spawn_times.append(self._clock.value)
        return LaunchHandle(
            executor=self.executor, model_id=self.model_id, session_name=slice_id,
            pid=1000 + len(self.calls), log_path=f"{log_dir}/{slice_id}.jsonl",
        )


# --- 3a) autonomy.dispatch_ready（fanout lane） -----------------------------
def test_dispatch_ready_same_provider_spawns_are_staggered_not_back_to_back() -> None:
    """RED→GREEN 核心案例：修正前這個呼叫連 spawn_admission 參數都不存在
    （TypeError）；接線後 3 個同 provider 的 ready slice 必須依 min_interval
    錯開，而不是像 #381 回報的那樣同一瞬間背靠背全部 spawn。"""
    registry = _FakeRegistry()
    dispatcher = _FakeDispatcher(registry)
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)
    launcher = _RecordingLauncher(executor="copilot", model_id=None, clock=clock)

    metas = [_meta(f"slice-{i}") for i in range(3)]

    jobs = dispatch_ready(
        metas,
        is_satisfied=lambda _sid: True,
        dispatcher=dispatcher,
        persona="builder",
        launcher=launcher,
        git_runner=_fake_git_runner,
        spawn_admission=limiter,
    )

    assert len(jobs) == 3
    # 驗 spawn *間隔*（非序列化整個 job 執行）：只斷言啟動瞬間的時間戳錯開。
    assert launcher.spawn_times == [0.0, 2.0, 4.0]
    assert clock.sleep_calls == [2.0, 2.0]


def test_dispatch_ready_without_spawn_admission_is_back_to_back_red_baseline() -> None:
    """記錄「不注入 limiter」時的既有行為：三個 spawn 仍然背靠背（無錯開），
    佐證 admission gate 本身必須顯式接上才會生效——不是意外副作用。"""
    registry = _FakeRegistry()
    dispatcher = _FakeDispatcher(registry)
    clock = _FakeClock()
    launcher = _RecordingLauncher(executor="copilot", model_id=None, clock=clock)

    metas = [_meta(f"slice-{i}") for i in range(3)]

    jobs = dispatch_ready(
        metas,
        is_satisfied=lambda _sid: True,
        dispatcher=dispatcher,
        persona="builder",
        launcher=launcher,
        git_runner=_fake_git_runner,
    )

    assert len(jobs) == 3
    assert launcher.spawn_times == [0.0, 0.0, 0.0]


def test_dispatch_ready_different_providers_spawn_without_extra_wait() -> None:
    """#381 覆驗警告：不能全域序列化。不同 provider 的 slice 即使共用同一個
    limiter instance，也不應互相拖慢。"""
    registry = _FakeRegistry()
    dispatcher = _FakeDispatcher(registry)
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)
    identities = _identity_registry()
    launchers: dict[str, _RecordingLauncher] = {}

    def launcher_factory(identity):
        launcher = _RecordingLauncher(executor=identity.executor, model_id=identity.model_id, clock=clock)
        launchers[identity.executor] = launcher
        return launcher

    metas = [
        _meta("slice-codex", executor="codex", model_id="gpt-5.4-codex"),
        _meta("slice-copilot", executor="copilot", model_id="claude-haiku-4.5"),
    ]

    jobs = dispatch_ready(
        metas,
        is_satisfied=lambda _sid: True,
        dispatcher=dispatcher,
        persona="builder",
        launcher=_RecordingLauncher(executor="unused-default", model_id=None, clock=clock),
        identity_registry=identities,
        launcher_factory=launcher_factory,
        git_runner=_fake_git_runner,
        spawn_admission=limiter,
    )

    assert len(jobs) == 2
    assert launchers["codex"].spawn_times == [0.0]
    assert launchers["copilot"].spawn_times == [0.0]
    assert clock.sleep_calls == []


def test_dispatch_ready_default_executor_fallback_uses_launcher_executor_attribute() -> None:
    """未宣告 per-slice executor 時，provider bucket 要能從注入的 launcher 自報
    的 ``.executor`` 取得（生產路徑：SubprocessLauncher 公開此屬性），
    而不是一律落到跟其他呼叫混在一起的通用 default bucket。"""
    registry = _FakeRegistry()
    dispatcher = _FakeDispatcher(registry)
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)
    limiter.admit("claude")  # 模擬另一個 provider 剛 spawn 過——不應影響 copilot
    clock.value = 0.0
    launcher = _RecordingLauncher(executor="copilot", model_id=None, clock=clock)

    jobs = dispatch_ready(
        [_meta("solo-slice")],
        is_satisfied=lambda _sid: True,
        dispatcher=dispatcher,
        persona="builder",
        launcher=launcher,
        git_runner=_fake_git_runner,
        spawn_admission=limiter,
    )

    assert len(jobs) == 1
    assert launcher.spawn_times == [0.0]


# --- 3b) manager._dispatch_workflow_card（workflow lane 的實際 spawn 點） --
def _workflow_manifest():
    from paulsha_cortex.deck.compile import compile_combo
    from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo

    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, "spawn admission wiring", change="production-wiring")
    assert result.workflow_manifest is not None
    return result.workflow_manifest


def _write_planning_artifacts(root: Path, *, missing: set[str] | None = None):
    from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority

    missing = missing or set()
    proposal = root / "openspec/changes/production-wiring/proposal.md"
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text("# Proposal\n", encoding="utf-8")
    bodies = {
        "spec": "---\nstatus: accepted\n---\n# Spec\n## Requirements\nFixed.\n",
        "design": "---\nstatus: accepted\n---\n# Design\n## Decisions\nFixed.\n",
        "plan": "---\nstatus: accepted\n---\n# Plan\n## Task 1\nBuild.\n",
    }
    authority = []
    for kind, body in bodies.items():
        ref = (
            "docs/superpowers/plans/production-wiring.md"
            if kind == "plan"
            else f"docs/{kind}.md"
        )
        path = root / ref
        if kind not in missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        else:
            digest = "0" * 64
        authority.append(
            PlanningArtifactAuthority(ref=ref, kind=kind, work_id="production-wiring", baseline_sha256=digest)
        )
    return tuple(authority)


def _dispatch_one_incomplete_plan_run(*, tmp_path: Path, label: str, clock: _FakeClock, spawn_admission=None):
    """複製 test_workflow_production_wiring.py 已驗證過的最小成功派工情境
    （artifacts 缺 plan → 派 planner），跑一次會真的走到 launcher.launch()。"""
    from paulsha_cortex.coordinator.launcher import LaunchHandle

    repo = tmp_path / f"repo-{label}"
    repo.mkdir()
    registry = JobRegistry(state_path=tmp_path / f"registry-{label}.json")
    authority = _write_planning_artifacts(repo, missing={"plan"})
    run = registry._manager_create_workflow_run(
        work_id="production-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(repo),
        combo="feature-oneshot",
        current_phase="plan",
        steps=_workflow_manifest().steps,
        issue_refs=("hamanpaul/paulsha-cortex#14",),
        openspec_refs=("production-wiring",),
        pr_refs=(),
        attempts={"plan": 1},
        gate_status="running",
        planning_authority=authority,
    )

    class _Launcher:
        def __init__(self) -> None:
            self.spawn_times: list[float] = []

        def as_read_only(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            self.spawn_times.append(clock.value)
            return LaunchHandle(
                executor="codex", model_id="gpt-primary", session_name=slice_id, pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    launcher = _Launcher()
    kwargs = dict(
        run=run,
        identities=IdentityRegistry.from_rows(
            [{
                "executor": "codex", "model_id": "gpt-primary", "independence_domain": "openai",
                "capabilities": ["planning"],
            }]
        ),
        launcher_factory=lambda _: launcher,
        coordinator_root=tmp_path / f"coordinator-{label}",
    )
    if spawn_admission is not None:
        kwargs["spawn_admission"] = spawn_admission
    job = manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        **kwargs,
    )
    assert job is not None
    return launcher


def test_dispatch_workflow_card_staggers_same_provider_spawns_across_runs(tmp_path: Path) -> None:
    """Workflow lane 的實際 spawn 點：兩個不同 workflow run（模擬 periodic tick
    resume 迴圈逐一 resume 多個 ongoing run）宣告同一個 executor/model_id，
    共用同一個 limiter instance 時必須被錯開。"""
    clock = _FakeClock()
    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0, clock=clock.now, sleep=clock.sleep)

    launcher_a = _dispatch_one_incomplete_plan_run(
        tmp_path=tmp_path, label="a", clock=clock, spawn_admission=limiter
    )
    launcher_b = _dispatch_one_incomplete_plan_run(
        tmp_path=tmp_path, label="b", clock=clock, spawn_admission=limiter
    )

    assert launcher_a.spawn_times == [0.0]
    assert launcher_b.spawn_times == [2.0]
    assert clock.sleep_calls == [2.0]


def test_dispatch_workflow_card_without_spawn_admission_defaults_to_noop(tmp_path: Path) -> None:
    clock = _FakeClock()
    launcher_a = _dispatch_one_incomplete_plan_run(tmp_path=tmp_path, label="noop-a", clock=clock)
    launcher_b = _dispatch_one_incomplete_plan_run(tmp_path=tmp_path, label="noop-b", clock=clock)

    assert launcher_a.spawn_times == [0.0]
    assert launcher_b.spawn_times == [0.0]


# --- 3c) manager_daemon.build_periodic_tick_runner：兩條 lane 共用同一把鎖 --
def _resumable_workflow(run_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        work_id=f"demo-{run_id}",
        repo="acme/demo",
        status="ongoing",
        facets=(),
        current_phase="build",
        claim_key="claim:legacy:demo",
        source_revision="",
    )


def test_periodic_tick_threads_same_spawn_admission_into_workflow_resume_and_fanout_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """接線正確性：periodic tick 的 workflow resume 迴圈與 run_tick（fanout）
    必須拿到*同一個* limiter instance——否則兩條 lane 對同一個 provider quota
    各自為政，達不到 #381 要求的協調節流。"""
    workflows = [_resumable_workflow("run-1"), _resumable_workflow("run-2")]
    registry = SimpleNamespace(
        _state_path=str(tmp_path / "jobs.json"),
        list_workflow_runs=lambda: workflows,
    )
    dispatcher = SimpleNamespace(_registry=registry, _git_runner=lambda args: "")

    resume_spawn_admissions: list[object] = []

    def fake_resume_workflow_run(dispatcher_arg, **kwargs):
        resume_spawn_admissions.append(kwargs.get("spawn_admission"))

    tick_spawn_admissions: list[object] = []

    def fake_run_tick(dispatcher_arg, **kwargs):
        tick_spawn_admissions.append(kwargs.get("spawn_admission"))
        return {"dispatch_skipped": False, "dispatched": [], "completed": [], "errors": [], "reaped": None}

    monkeypatch.setattr(manager_daemon.manager, "resume_workflow_run", fake_resume_workflow_run)

    limiter = SpawnAdmissionLimiter(min_interval_seconds=2.0)
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        run_tick_fn=fake_run_tick,
        scan_specs_fn=lambda specs_dir: [],
        auto_claim_fn=lambda: [],
        workflow_identity_registry=object(),
        spawn_admission=limiter,
    )

    runner()

    assert resume_spawn_admissions == [limiter, limiter]
    assert tick_spawn_admissions == [limiter]


def test_periodic_tick_resume_loop_defaults_to_noop_when_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """沒接 spawn_admission 時（例如尚未升級 CLI 的舊呼叫端）必須完全不影響
    既有行為：resume/loop 拿到的是 None，而不是憑空冒出的真實節流器。"""
    workflows = [_resumable_workflow("run-1")]
    registry = SimpleNamespace(
        _state_path=str(tmp_path / "jobs.json"),
        list_workflow_runs=lambda: workflows,
    )
    dispatcher = SimpleNamespace(_registry=registry, _git_runner=lambda args: "")

    resume_spawn_admissions: list[object] = []

    def fake_resume_workflow_run(dispatcher_arg, **kwargs):
        resume_spawn_admissions.append(kwargs.get("spawn_admission"))

    monkeypatch.setattr(manager_daemon.manager, "resume_workflow_run", fake_resume_workflow_run)

    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=dispatcher,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        launcher=object(),
        run_tick_fn=lambda dispatcher_arg, **kwargs: {
            "dispatch_skipped": False, "dispatched": [], "completed": [], "errors": [], "reaped": None,
        },
        scan_specs_fn=lambda specs_dir: [],
        auto_claim_fn=lambda: [],
        workflow_identity_registry=object(),
    )

    runner()

    assert resume_spawn_admissions == [None]
