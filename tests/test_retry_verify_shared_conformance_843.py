"""#843 R06：workflow 與 slice 的 ``retry-verify`` 共用同一組 conformance 斷言。

兩條路徑是**不同契約**，本檔不合併它們，也不改 slice API：

- work namespace：``work-action retry-verify`` 以 exact Candidate CAS 把 verify
  phase 打回 pending，再由 daemon 在**同一個請求**內以 ``force_new_card`` 強制派
  一顆新的 verification job（dispatch_mode ``daemon-forced-job``）。
- slice namespace：``slice-action retry-verify`` 在 action 內**同步**重跑一次
  verification runner，不建立 job；結果若是 ``reviewing`` 才會同步啟動 foreign
  review（dispatch_mode ``inline-verification``）。

兩者都從同一個正式入口 ``manager_daemon.build_request_executor`` 進入（control
request 先過 ``control.contract.validate_request``），各自的 adapter 只負責建立
「verify 失敗後停在 needs_human」的狀態、組出正式請求與回報可比較的觀測；所有
測試函式對兩個 namespace 跑同一組斷言，差異只依 adapter 宣告的
``dispatch_mode`` 分支，不以隱式條件抹平。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest

from git_fixtures import make_fake_repo
from paulsha_cortex.control import contract as control_contract
from paulsha_cortex.coordinator import manager, manager_daemon, verification, work_actions
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import ACTIVE_JOB_STATUSES, JobRegistry, TERMINAL_JOB_STATUSES

from test_coordinator_operator_actions import (
    _PaneSender,
    _WorktreeCreator,
    _create_slice_in_needs_human,
)
from test_reviewer_card_retry_569 import (
    REPO,
    WORK_ID,
    _ReviewLauncher,
    _reviewer_identities,
    _stuck_reviewer_run,
)


WORK_DISPATCH_MODE = "daemon-forced-job"
SLICE_DISPATCH_MODE = "inline-verification"
SLICE_ID = "slice-retry-verify"
SLICE_CANDIDATE = "b" * 40
REJECTION_CATEGORIES = (
    "wrong-phase",
    "not-awaiting-human",
    "candidate-cas-mismatch",
    "missing-candidate",
    "active-job",
)
# 每個 namespace 在各拒絕類別下的**具體**拒絕理由。work 的 admission 在
# control contract／execute_work_action／registry reset 各自報出精確原因；slice
# 的 admission 統一由 `allowed_slice_actions` 判定，因此另以 read model 驗證「正是
# 該前置條件」讓 retry-verify 從合法動作中消失。
REJECTION_REASONS: dict[str, dict[str, str]] = {
    "work": {
        "wrong-phase": "retry-verify requires verify-phase workflow",
        "not-awaiting-human": "retry-verify requires needs_human workflow",
        "candidate-cas-mismatch": "retry-verify expected Candidate CAS mismatch",
        "missing-candidate": "retry-verify requires exact expected_candidate",
        "active-job": "retry-verify reset refuses active workflow job",
    },
    "slice": {category: "action-not-allowed:retry-verify" for category in REJECTION_CATEGORIES},
}


@dataclass
class RetryVerifyScenario:
    """一個 namespace 在「verify 失敗後 needs_human」狀態下的正式入口 fixture。"""

    namespace: str
    dispatch_mode: str
    root: Path
    registry: JobRegistry
    executor: Callable[[dict[str, Any]], dict[str, Any]]
    old_job_id: str
    candidate: str
    request_args: dict[str, Any]
    prior_evidence: dict[str, bytes]
    subject_id: str
    # read model 的 next_actions 是否曝光 retry-verify：slice 會（allowed_slice_actions），
    # work 不會（`_phase_recovery_actions` 只投影 retry-card 等），這是兩契約的既有差異。
    retry_verify_projected: bool
    verification_runs: list[str] = field(default_factory=list)
    forced_dispatches: list[bool] = field(default_factory=list)
    launches: list[tuple[str, str]] = field(default_factory=list)

    @property
    def request_type(self) -> str:
        return "work-action" if self.namespace == "work" else "slice-action"

    def submit(self, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """經 control contract 驗證後交給 daemon request executor（正式入口）。"""

        request = control_contract.validate_request(
            control_contract.build_request(
                req_type=self.request_type,
                args=dict(self.request_args if args is None else args),
                requested_by="operator",
            )
        )
        return self.executor(request)

    def subject(self) -> dict[str, Any]:
        """持久化狀態（fresh registry 重讀），證明觀測不是 in-memory 殘影。"""

        fresh = JobRegistry(state_path=self.registry._state_path)
        if self.namespace == "work":
            return fresh.get_workflow_run(self.subject_id).to_dict()
        return fresh.get_slice(self.subject_id)

    def subject_candidate(self) -> str | None:
        subject = self.subject()
        return subject["candidate_head"] if self.namespace == "work" else subject["candidate"]

    def awaiting_human(self) -> bool:
        subject = self.subject()
        if self.namespace == "work":
            return subject["status"] == "ongoing" and "needs_human" in subject["facets"]
        return subject["state"] == "needs_human"

    def read_model_actions(self) -> list[str] | None:
        """attention read model：work 取 workflow_status_entry，slice 取 slice_status_entry。

        回傳 ``None`` 代表這個主體目前不在 attention 清單（已不需要人工處置）。
        """

        fresh = JobRegistry(state_path=self.registry._state_path)
        if self.namespace == "work":
            run = fresh.get_workflow_run(self.subject_id)
            if run.status != "ongoing" or "needs_human" not in run.facets:
                return None
            return list(manager.workflow_status_entry(fresh, run)["next_actions"])
        entry = manager.slice_status_entry(
            fresh,
            fresh.get_slice(self.subject_id),
            handoff_dir=str(self.root / "handoff"),
        )
        if entry.get("slice_state") != "needs_human":
            return None
        return list(entry["next_actions"])

    def executions(self) -> int:
        """本請求造成的 verification 執行次數（work＝新 verify job；slice＝runner 呼叫）。"""

        if self.dispatch_mode == WORK_DISPATCH_MODE:
            return len(self.launches)
        return len(self.verification_runs)


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _fingerprint(scenario: RetryVerifyScenario) -> dict[str, Any]:
    """拒絕前後必須完全相同的三帳：registry、evidence（含整棵 fixture 樹）、job 數。"""

    fresh = JobRegistry(state_path=scenario.registry._state_path)
    return {
        "registry": Path(scenario.registry._state_path).read_bytes(),
        "files": _file_bytes(scenario.root),
        "job_count": len(fresh.list_jobs()),
        "read_model": scenario.read_model_actions(),
    }


def _surviving_bytes(root: Path) -> set[bytes]:
    return set(_file_bytes(root).values())


# ---------------------------------------------------------------------------
# namespace adapters
# ---------------------------------------------------------------------------


def _work_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RetryVerifyScenario:
    snapshot, registry, run, old_job_id = _stuck_reviewer_run(tmp_path, with_git=True)
    prior = tmp_path / "evidence" / "verification" / "prior-verification-failure.json"
    prior.parent.mkdir(parents=True, exist_ok=True)
    prior.write_text('{"status": "needs_human", "summary": "no JSON envelope"}\n', encoding="utf-8")
    run = registry._manager_update_workflow_run(run.run_id, evidence_refs=(str(prior),))
    old_log = Path(registry.get_job(old_job_id)["log_path"])

    scenario = RetryVerifyScenario(
        namespace="work",
        dispatch_mode=WORK_DISPATCH_MODE,
        root=tmp_path,
        registry=registry,
        executor=lambda _request: {},
        old_job_id=old_job_id,
        candidate=run.candidate_head,
        request_args={
            "action": "retry-verify",
            "repo": REPO,
            "work_id": WORK_ID,
            "expected_candidate": run.candidate_head,
        },
        prior_evidence={
            prior.relative_to(tmp_path).as_posix(): prior.read_bytes(),
            old_log.relative_to(tmp_path).as_posix(): old_log.read_bytes(),
        },
        subject_id=run.run_id,
        retry_verify_projected=False,
    )

    # 正式 daemon 路徑：manager.apply_work_action（含 Manager 的 exact reviewer
    # recovery checker）→ execute_work_action。只注入 production 由 canonical
    # snapshot／state path 提供的兩個路徑。
    real_execute = work_actions.execute_work_action

    def execute_with_fixture_paths(**kwargs):
        kwargs.setdefault("snapshot_path", snapshot)
        kwargs.setdefault("state_path", tmp_path / "runs.json")
        return real_execute(**kwargs)

    monkeypatch.setattr(work_actions, "execute_work_action", execute_with_fixture_paths)
    monkeypatch.setattr(manager, "load_model_identities", _reviewer_identities)

    # 真的 dispatch_workflow_card（force_new_card、identity 重解析、sandbox 建立），
    # 只把 launcher 換成不啟動模型的記錄器。
    real_dispatch = manager.dispatch_workflow_card

    def recording_dispatch(dispatcher, **kwargs):
        scenario.forced_dispatches.append(kwargs.get("force_new_card"))
        kwargs["launcher_factory"] = lambda _identity: _ReviewLauncher(scenario.launches)
        return real_dispatch(dispatcher, **kwargs)

    monkeypatch.setattr(manager, "dispatch_workflow_card", recording_dispatch)
    scenario.executor = manager_daemon.build_request_executor(
        dispatcher=type("D", (), {"_registry": registry, "_git_runner": None})(),
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        workflow_identity_registry=_reviewer_identities(),
    )
    return scenario


def _slice_git_runner(args: list[str]) -> str:
    # `_candidate_for_evidence` 以 builder branch／worktree HEAD 取 candidate；
    # 回傳 slice 自己的 candidate，讓重跑的對象就是同一個未變的 Candidate。
    if args and "rev-parse" in args:
        return SLICE_CANDIDATE
    return ""


def _slice_evidence(coordinator_root: Path, *, candidate: str, status: str, summary: str) -> dict:
    return verification.write_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": SLICE_ID,
            "candidate": candidate,
            "status": status,
            "summary": summary,
            "details": {},
        },
        coordinator_root=coordinator_root,
    )


def _slice_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RetryVerifyScenario:
    repo_root = tmp_path / "repo"
    make_fake_repo(repo_root)
    registry = JobRegistry(state_path=tmp_path / "runtime" / "coordinator" / "jobs.json")
    coordinator_root = Path(registry._state_path).parent
    row = _create_slice_in_needs_human(
        registry, repo_root=repo_root, slice_id=SLICE_ID, builder_status="exited"
    )
    # production 的 current evidence 永遠在 canonical `(slice, candidate)` 路徑
    # （`_apply_verification_result` 寫入的就是它），fixture 照實放在那裡。
    prior = _slice_evidence(
        coordinator_root,
        candidate=SLICE_CANDIDATE,
        status="needs_human",
        summary="previous-verification-failed",
    )
    registry.update_slice(
        SLICE_ID,
        current_evidence_refs=[prior["path"]],
        current_verification_evidence_hash=prior["hash"],
    )
    prior_path = Path(prior["path"])

    scenario = RetryVerifyScenario(
        namespace="slice",
        dispatch_mode=SLICE_DISPATCH_MODE,
        root=tmp_path,
        registry=registry,
        executor=lambda _request: {},
        old_job_id=row["builder_job_id"],
        candidate=SLICE_CANDIDATE,
        request_args={"slice_id": SLICE_ID, "action": "retry-verify", "actor": "operator"},
        prior_evidence={prior_path.relative_to(tmp_path).as_posix(): prior_path.read_bytes()},
        subject_id=SLICE_ID,
        retry_verify_projected=True,
    )

    def recording_runner(**kwargs):
        # 與 `verification.run_result_verification` 相同：經正式 writer 落檔。
        scenario.verification_runs.append(kwargs["job"]["job_id"])
        return _slice_evidence(
            kwargs["coordinator_root"],
            candidate=SLICE_CANDIDATE,
            status="verified",
            summary="verification-passed",
        )

    monkeypatch.setattr(verification, "run_result_verification", recording_runner)
    dispatcher = Dispatcher(
        registry=registry,
        pane_sender=_PaneSender(),
        worktree_creator=_WorktreeCreator(tmp_path / "worktrees"),
        git_runner=_slice_git_runner,
    )
    scenario.executor = manager_daemon.build_request_executor(
        dispatcher=dispatcher,
        specs_dir=str(repo_root / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
    )
    return scenario


ADAPTERS: dict[str, Callable[[Path, pytest.MonkeyPatch], RetryVerifyScenario]] = {
    "work": _work_scenario,
    "slice": _slice_scenario,
}


@pytest.fixture(params=sorted(ADAPTERS))
def scenario(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RetryVerifyScenario:
    return ADAPTERS[request.param](tmp_path, monkeypatch)


def _apply_rejection(scenario: RetryVerifyScenario, category: str) -> dict[str, Any]:
    """把 fixture 推到該拒絕類別的狀態，回傳要送出的正式請求 args。"""

    registry = scenario.registry
    args = dict(scenario.request_args)
    if scenario.namespace == "work":
        run_id = scenario.subject_id
        if category == "wrong-phase":
            registry._manager_update_workflow_run(run_id, current_phase="review")
        elif category == "not-awaiting-human":
            registry._manager_update_workflow_run(
                run_id, facets=(), gate_status="running", needs_human_reason=None
            )
        elif category == "candidate-cas-mismatch":
            args["expected_candidate"] = "e" * 40
        elif category == "missing-candidate":
            args.pop("expected_candidate")
        elif category == "active-job":
            run = registry.get_workflow_run(run_id)
            registry.create_job(
                task="wf-verification-in-flight",
                persona="reviewer",
                kind="review",
                branch="feature/12-demo",
                pane="",
                worktree=str(scenario.root / "in-flight-sandbox"),
                subject_head=run.candidate_head,
                workflow_run_id=run.run_id,
                workflow_claim_key=run.claim_key,
                workflow_repo=run.repo,
                workflow_card="verification",
                workflow_phase="verify",
            )
        else:  # pragma: no cover - 參數表與這裡必須同步
            raise AssertionError(category)
        return args

    if category == "wrong-phase":
        # slice 沒有 phase 欄位；lifecycle 上仍在 building（尚未進入 verification）。
        registry.update_slice(SLICE_ID, state="building", gate_state="pending")
    elif category == "not-awaiting-human":
        registry.update_slice(SLICE_ID, state="verified", gate_state="passed")
    elif category == "candidate-cas-mismatch":
        # slice 的 candidate CAS：current evidence 必須綁在 slice 當下的 candidate。
        other = _slice_evidence(
            Path(registry._state_path).parent,
            candidate="c" * 40,
            status="needs_human",
            summary="evidence-for-other-candidate",
        )
        registry.update_slice(
            SLICE_ID,
            current_evidence_refs=[other["path"]],
            current_verification_evidence_hash=other["hash"],
        )
    elif category == "missing-candidate":
        # `update_slice(candidate=None)` 是「不更新」語意，只能直接構造缺席狀態。
        registry._find_slice(SLICE_ID)["candidate"] = None
        registry._persist()
    elif category == "active-job":
        active = registry.create_job(
            task=SLICE_ID,
            persona="builder",
            branch=f"feature/{SLICE_ID}",
            pane="",
            worktree=str(scenario.root / "in-flight-builder"),
        )
        registry._find_slice(SLICE_ID)["builder_job_id"] = active["job_id"]
        registry._persist()
    else:  # pragma: no cover
        raise AssertionError(category)
    return args


# ---------------------------------------------------------------------------
# shared conformance assertions
# ---------------------------------------------------------------------------


def test_adapters_declare_distinct_dispatch_modes() -> None:
    """兩個 namespace 的派工語意不同，必須由 adapter 明示而不是被測試抹平。"""

    assert {WORK_DISPATCH_MODE, SLICE_DISPATCH_MODE} == {
        "daemon-forced-job",
        "inline-verification",
    }
    assert sorted(ADAPTERS) == ["slice", "work"]


def test_retry_verify_reruns_verification_once_for_the_unchanged_candidate(
    scenario: RetryVerifyScenario,
) -> None:
    old_job = scenario.registry.get_job(scenario.old_job_id)
    jobs_before = {job["job_id"] for job in scenario.registry.list_jobs()}
    assert scenario.awaiting_human()
    listed = scenario.read_model_actions()
    assert listed is not None
    assert ("retry-verify" in listed) is scenario.retry_verify_projected

    response = scenario.submit()

    # 共用：同一個請求恰好一次新的 verification 執行，對象是未變的 Candidate。
    assert scenario.executions() == 1
    assert scenario.subject_candidate() == scenario.candidate
    fresh = JobRegistry(state_path=scenario.registry._state_path)
    new_jobs = [job for job in fresh.list_jobs() if job["job_id"] not in jobs_before]

    # 共用：舊 terminal job 保留（不刪、不復活），身分／log／evidence 綁定不變。
    kept = fresh.get_job(scenario.old_job_id)
    assert kept["status"] in TERMINAL_JOB_STATUSES
    for key in ("executor", "model_id", "log_path", "workflow_evidence", "worktree", "task"):
        assert kept.get(key) == old_job.get(key), key

    # 共用：先前的 evidence 內容一個位元組都沒被抹掉，原路徑保留。
    surviving = _surviving_bytes(scenario.root)
    for relpath, content in scenario.prior_evidence.items():
        assert content in surviving, relpath

    # 共用：同一時間最多一個進行中的 verification attempt。
    active = [
        job
        for job in fresh.list_jobs()
        if job["status"] in ACTIVE_JOB_STATUSES
    ]
    assert len(active) <= 1

    if scenario.dispatch_mode == WORK_DISPATCH_MODE:
        result = response["result"]
        assert scenario.forced_dispatches == [True]
        assert scenario.verification_runs == []
        assert len(new_jobs) == 1
        replacement = new_jobs[0]
        assert result["job_id"] == replacement["job_id"]
        assert result["dispatch"]["kind"] == "job"
        assert (replacement["workflow_phase"], replacement["workflow_card"]) == (
            "verify",
            "verification",
        )
        assert replacement["persona"] == "reviewer"
        assert replacement["subject_head"] == scenario.candidate
        assert active == [replacement]
        # #315：非精準可復原的舊 exited verify job 由 reset 標為 failed（仍保留）。
        assert kept["status"] == "failed"
        run = scenario.subject()
        assert "needs_human" not in run["facets"]
        assert all(
            step["gate_result"] == "pending"
            for step in run["steps"]
            if step["phase"] == "verify"
        )
        assert scenario.read_model_actions() is None
    else:
        assert scenario.dispatch_mode == SLICE_DISPATCH_MODE
        assert scenario.forced_dispatches == []
        assert scenario.launches == []
        assert scenario.verification_runs == [scenario.old_job_id]
        # slice 在 action 內同步重跑，不建立 verification job。
        assert new_jobs == []
        assert kept == old_job
        persisted = scenario.subject()
        assert response["action"] == "retry-verify"
        # 同一 Candidate 的新結果以 content hash 分出 evidence 路徑，舊 evidence
        # 原位保留；通過結果應被採信並離開 needs_human。
        assert response["gate_status"] in {"verified", "reviewing"}
        prior_path = scenario.root / next(iter(scenario.prior_evidence))
        assert Path(response["verification_evidence_path"]) != prior_path
        assert all((scenario.root / relpath).is_file() for relpath in scenario.prior_evidence)
        assert response["slice_state"] == persisted["state"]
        assert response["gate_state"] == persisted["gate_state"]
        assert response["verification_evidence_path"] == persisted["current_evidence_refs"][0]
        assert Path(response["verification_evidence_path"]).is_file()
        # read model 與持久狀態一致：仍需人工 ⇔ 出現在 attention。
        assert (scenario.read_model_actions() is not None) == (
            persisted["state"] == "needs_human"
        )


@pytest.mark.parametrize("category", REJECTION_CATEGORIES)
def test_retry_verify_rejections_leave_registry_evidence_and_jobs_unchanged(
    scenario: RetryVerifyScenario, category: str
) -> None:
    args = _apply_rejection(scenario, category)
    before = _fingerprint(scenario)
    if scenario.retry_verify_projected:
        # 拒絕前 read model 已不再提供 retry-verify：投影與 admission 同源。
        assert "retry-verify" not in (before["read_model"] or [])

    with pytest.raises(
        (ValueError, RuntimeError),
        match=re.escape(REJECTION_REASONS[scenario.namespace][category]),
    ):
        scenario.submit(args)

    assert _fingerprint(scenario) == before
    assert scenario.verification_runs == []
    assert scenario.forced_dispatches == []
    assert scenario.launches == []


def test_retry_verify_replay_never_opens_a_second_concurrent_attempt(
    scenario: RetryVerifyScenario,
) -> None:
    scenario.submit()
    after_first = JobRegistry(state_path=scenario.registry._state_path).list_jobs()
    executions_after_first = scenario.executions()

    if scenario.dispatch_mode == WORK_DISPATCH_MODE:
        # work：reset 已清掉 needs_human，同一請求重送必被 admission 拒絕，
        # 不得再派第二顆 verification job。
        before = _fingerprint(scenario)
        with pytest.raises(RuntimeError, match="needs_human"):
            scenario.submit()
        assert _fingerprint(scenario) == before
        assert scenario.executions() == executions_after_first
    else:
        # slice：沒有 job 可重複派；重送只可能是一次新的同步重跑（仍需人工時）
        # 或被 allowed-action 拒絕（已脫離 needs_human 時），兩者都不新增 job。
        still_awaiting = scenario.awaiting_human()
        if still_awaiting:
            scenario.submit()
            assert scenario.executions() == executions_after_first + 1
        else:
            with pytest.raises(ValueError, match="action-not-allowed"):
                scenario.submit()
            assert scenario.executions() == executions_after_first

    after_replay = JobRegistry(state_path=scenario.registry._state_path).list_jobs()
    assert len(after_replay) == len(after_first)
    assert [job for job in after_replay if job["job_id"] in {j["job_id"] for j in after_first}] == after_first
    assert len([job for job in after_replay if job["status"] in ACTIVE_JOB_STATUSES]) <= 1


def test_rejection_matrix_covers_both_namespaces_with_the_same_categories() -> None:
    """同一組拒絕類別對兩個 namespace 都要有具體狀態構造，不得以 N/A 略過。"""

    assert set(REJECTION_REASONS) == set(ADAPTERS)
    for namespace, reasons in REJECTION_REASONS.items():
        assert set(reasons) == set(REJECTION_CATEGORIES), namespace
    source = Path(__file__).read_text(encoding="utf-8")
    for category in REJECTION_CATEGORIES:
        # 每個類別在 `_apply_rejection` 的 work 與 slice 兩段各出現一次。
        assert source.count(f'category == "{category}"') == 2, category


def test_slice_retry_verify_adopts_a_passing_rerun_for_the_same_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契約：slice retry-verify 在 action 內重跑 verification；重跑通過就應該被採信。"""

    scenario = _slice_scenario(tmp_path, monkeypatch)

    response = scenario.submit()

    assert scenario.verification_runs == [scenario.old_job_id]
    persisted = scenario.subject()
    assert response["gate_status"] in {"verified", "reviewing"}
    assert persisted["state"] != "needs_human"
