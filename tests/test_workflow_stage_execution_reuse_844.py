"""#844：production stage-evidence reuse 的 resume_workflow_run 整合驗收。

只覆蓋本票核心：正常 producer（`manager._dispatch_workflow_card`）自動計算受信
`workflow_stage_execution_key`／receipt，`manager.resume_workflow_run` 在既有
canonical dispatch/resume 決策點消費它，讓第二次接續在相容時不新增 model
invocation、gate 正確前進（S01／S02），在 executor/model/execution-profile
變更時逐欄拒絕沿用、強制新 attempt（S03／S04），且 legacy（無 key）job 維持
#844 之前的行為（S10）。不使用任何 `lambda _: True` 之類的假 validator，也不
monkeypatch admission 為永真——全程走真正的 `resume_workflow_run`／
`_dispatch_workflow_card`／`apply_workflow_action(action="advance")`。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, runtime_preflight
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowRun, WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason

REPO = "hamanpaul/paulsha-cortex"
WORK_ID = "stage-reuse-844"
BUILDER_DOMAIN = "openai"
REVIEWER_EXECUTOR = "claude"
REVIEWER_MODEL = "sonnet-844"
REVIEWER_DOMAIN = "anthropic"
PLAN_REF = "docs/plan.md"
PLAN_TEXT = "# plan\n"


def _step(
    phase: str,
    card: str,
    *,
    persona: str,
    gate_result: str = "pending",
    executor: str | None = None,
    model: str | None = None,
    domain: str | None = None,
    commit_policy: str | None = None,
) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=persona,
        card=card,
        executor=executor,
        model=model,
        domain=domain,
        inputs=(),
        outputs=(),
        gate_result=gate_result,
        commit_policy=commit_policy,
    )


def _steps() -> tuple[WorkflowStep, ...]:
    return (
        _step("claim", "manager-claim", persona="manager", gate_result="passed"),
        _step("define", "planner-define", persona="planner", gate_result="passed"),
        _step("plan", "planner-plan", persona="planner", gate_result="passed"),
        _step(
            "build",
            "subagent-build",
            persona="builder",
            gate_result="passed",
            executor="codex",
            model="gpt-primary",
            domain=BUILDER_DOMAIN,
            commit_policy="required",
        ),
        _step("verify", "verification", persona="reviewer", gate_result="pending"),
        _step("review", "code-review", persona="reviewer", gate_result="pending"),
        _step("ship", "manager-ship", persona="manager", gate_result="pending"),
    )


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_source_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _run_git(["config", "user.email", "t@example.com"], root)
    _run_git(["config", "user.name", "T"], root)
    (root / PLAN_REF).parent.mkdir(parents=True, exist_ok=True)
    (root / PLAN_REF).write_text(PLAN_TEXT, encoding="utf-8")
    _run_git(["add", "."], root)
    _run_git(["commit", "-qm", "candidate"], root)
    _run_git(["branch", f"feature/{WORK_ID}"], root)
    return _run_git(["rev-parse", "HEAD"], root).stdout.strip().lower()


class _Launcher:
    """比照 tests/test_executor_backoff_workflow_lane.py 的假 launcher。"""

    def __init__(self, executor: str, model_id: str, *, on_launch=None) -> None:
        self._executor = executor
        self._model_id = model_id
        self._on_launch = on_launch

    def as_commit_required(self):
        return self

    def as_read_only(self):
        return self

    def as_review_only(self, *, terminal_kind: str):
        return self

    def executor_environment(self) -> runtime_preflight.ExecutorEnvironment:
        return runtime_preflight.ExecutorEnvironment(
            name=f"{self._executor}-workflow",
            interpreter=(sys.executable,),
            path=os.environ.get("PATH", ""),
            home=os.path.expanduser("~"),
            provider_identity=self._executor,
        )

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        if self._on_launch is not None:
            self._on_launch(slice_id)
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        return LaunchHandle(
            executor=self._executor,
            model_id=self._model_id,
            session_name=slice_id,
            pid=4242,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


class _Dispatcher:
    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry
        self._git_runner = None

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.get_job(job_id)


def _identities(*, reviewer_model: str = REVIEWER_MODEL) -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": BUILDER_DOMAIN,
                "capabilities": ["build"],
            },
            {
                "executor": REVIEWER_EXECUTOR,
                "model_id": reviewer_model,
                "independence_domain": REVIEWER_DOMAIN,
                "capabilities": ["review"],
            },
        ]
    )


def _launcher_factory(launch_calls: list[str]):
    def factory(identity):
        return _Launcher(
            identity.executor,
            identity.model_id,
            on_launch=lambda slice_id: launch_calls.append(slice_id),
        )

    return factory


def _seed_auth_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider capability 檢查會探真實 CLI 登入態；種一份新鮮 snapshot 讓測試
    在沒有那些 CLI 的 sandbox 裡仍是 hermetic（沿用 #928 既有測試手法）。"""

    monkeypatch.setattr(manager, "_EXECUTOR_AUTH_CACHE", {})
    now = time.time()
    for provider_id in ("claude", "codex"):
        manager._EXECUTOR_AUTH_CACHE[provider_id] = runtime_preflight.ProviderFreshness(
            provider_id=provider_id,
            status="ok",
            observed_at=now,
            ttl_seconds=900.0,
            source="snapshot",
        )


def _build_verify_run(tmp_path: Path):
    workspace = tmp_path / "workspace"
    candidate = _init_source_repo(workspace)
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    run = registry._manager_create_workflow_run(
        work_id=WORK_ID,
        repo=REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="verify",
        steps=_steps(),
        issue_refs=(f"{REPO}#844",),
        openspec_refs=(WORK_ID,),
        candidate_head=candidate,
        attempts={"build": 1, "verify": 1},
        gate_status="running",
    )
    builder = registry.create_job(
        task="wf-subagent-build",
        persona="builder",
        branch=f"feature/{WORK_ID}",
        pane="",
        worktree=str(workspace),
        dispatch_head="b" * 40,
        subject_head=candidate,
        executor="codex",
        model_id="gpt-primary",
        independence_domain=BUILDER_DOMAIN,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="subagent-build",
        workflow_phase="build",
        source_revision=run.source_revision,
    )
    registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
    return registry, run, candidate


def _bind_valid_verification_evidence(
    registry: JobRegistry, *, job_id: str, coordinator_root: Path
) -> None:
    """依 `_read_job_workflow_evidence()`／`verification.validate_verification_evidence()`
    的精確 schema 手刻一份合法 evidence，沿用 job 自己實際的欄位值（不是憑空
    猜一份）——與 tests/test_preflight_closeout_order.py 的 `_seed_foreign_review`
    同一套手法，只是換成 verify 階段用的 verification evidence。
    """

    import hashlib
    import json

    from paulsha_cortex.coordinator import verification

    job = registry.get_job(job_id)
    evaluation = verification.validate_verification_evidence(
        {
            "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
            "slice_id": f"{job['workflow_run_id']}-{job['workflow_card']}",
            "candidate": job["subject_head"],
            "status": "verified",
            "summary": "#844 fixture：一切通過。",
            "details": {},
        }
    )
    envelope = {
        "schema_version": 1,
        "kind": "gate",
        "job": {
            "job_id": job["job_id"],
            "run_id": job["workflow_run_id"],
            "claim_key": job["workflow_claim_key"],
            "repo": job["workflow_repo"],
            "source_revision": job["source_revision"],
            "card_id": job["workflow_card"],
            "phase": job["workflow_phase"],
            "inputs": job.get("workflow_inputs", []),
            "outputs": job.get("workflow_outputs", []),
            "output_baseline": job.get("workflow_output_baseline", []),
        },
        "payload": evaluation,
        "artifacts": [],
    }
    content = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    evidence_path = coordinator_root / "evidence" / "workflow" / f"{job_id}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    registry.bind_workflow_evidence(
        job_id,
        locator={
            "kind": "gate",
            "path": f"evidence/workflow/{job_id}.json",
            "hash": digest,
        },
        subject_head=job["subject_head"],
    )


class TestStageExecutionReuseIntegration:
    """S01／S02／S07／S10／S11：正常 producer 兩次 resume 的相容重用。"""

    def test_second_resume_reuses_evidence_without_new_dispatch_and_advances_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_auth_cache(monkeypatch)
        registry, run, candidate = _build_verify_run(tmp_path)
        dispatcher = _Dispatcher(registry)
        coordinator_root = tmp_path / "coordinator"
        identities = _identities()
        launch_calls: list[str] = []

        # 第一次 resume：真正派工（唯一一次 model invocation）。
        first = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        assert first["reason"] == "in-flight"
        job_id = first["job_id"]
        job = registry.get_job(job_id)
        # S02：正常首次執行即持久化受信 key／schema／profile receipt。
        assert isinstance(job["workflow_stage_execution_key"], str)
        assert len(job["workflow_stage_execution_key"]) == 64
        receipt = job["workflow_stage_execution_receipt"]
        assert receipt["run_id"] == run.run_id
        assert receipt["claim_key"] == run.claim_key
        assert receipt["candidate_sha"] == candidate
        assert receipt["execution_profile_key"].startswith("epk:v1:resolved:")
        assert len(launch_calls) == 1

        # S02：重啟後（新 JobRegistry 實例重讀同一個 state_path）仍可找到同一
        # 來源，不是只存在記憶體或測試專用路徑。
        reloaded = JobRegistry(state_path=registry._state_path)
        assert reloaded.get_job(job_id)["workflow_stage_execution_key"] == (
            job["workflow_stage_execution_key"]
        )

        # 模擬 headless 完成＋harvest 綁定 canonical evidence（產品真正的完成
        # 路徑；這裡只是把「job 已經跑完」的事實寫進 registry，不假造任何
        # admission）。
        registry.update_headless_result(job_id, status="exited", exit_code=0)
        _bind_valid_verification_evidence(
            registry, job_id=job_id, coordinator_root=coordinator_root
        )

        # 第二次 resume：S01 的核心斷言——verify 卡不得新增 model invocation。
        # 既有 `resume_workflow_run` 在 advance 成功之後會在同一次呼叫內接著
        # 派下一張卡（見 manager.py `next_job = dispatch_or_stop(updated)`）；
        # review 卡是全新 stage、從未派工過，這一次是正常繼續，不是 reuse
        # 判定失敗——因此斷言的是「verify 卡沒有第二顆 job／沒有第二次
        # launch」，不是「launch_calls 完全不變」。
        second = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        assert launch_calls == [job_id, second.get("job_id")], (
            "verify 卡不得因為第二次接續而重新 launch；review 卡的第一次派工"
            "是正常繼續"
        )
        assert second["current_phase"] == "review"
        jobs = registry.list_jobs()
        verify_jobs = [job for job in jobs if job.get("workflow_card") == "verification"]
        review_jobs = [job for job in jobs if job.get("workflow_card") == "code-review"]
        assert len(verify_jobs) == 1
        assert verify_jobs[0]["job_id"] == job_id
        assert len(review_jobs) == 1  # 全新 stage 的第一次派工，合理存在。

        # S11：status／run 上可辨識這張卡是 reuse。
        persisted = registry.get_workflow_run(run.run_id)
        receipts = persisted.stage_reuse_receipts or {}
        assert receipts["verification"]["decision"] == "reused"
        assert receipts["verification"]["stage_execution_key"] == job["workflow_stage_execution_key"]

        # S08：crash／重送語意——review 卡此刻仍是 in-flight（尚未 exited），
        # 第三次 resume 重送只會 poll 同一顆 job，不得因為 verify 卡的
        # reuse receipt 還在而重複 spawn 或重複 advance。
        third = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        assert launch_calls == [job_id, second.get("job_id")]
        assert third.get("job_id") == second.get("job_id")
        assert len(
            [j for j in registry.list_jobs() if j.get("workflow_card") == "code-review"]
        ) == 1

    def test_model_change_forces_a_fresh_attempt_instead_of_reusing_stale_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S04／S07：既有 evidence 對應的 model 已不是現在會派出的 model，必須
        逐欄拒絕沿用並強制新 attempt，不得沿用舊 evidence。"""

        _seed_auth_cache(monkeypatch)
        registry, run, candidate = _build_verify_run(tmp_path)
        dispatcher = _Dispatcher(registry)
        coordinator_root = tmp_path / "coordinator"
        launch_calls: list[str] = []

        first = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=_identities(reviewer_model=REVIEWER_MODEL),
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        job_id = first["job_id"]
        stale_job = registry.get_job(job_id)
        registry.update_headless_result(job_id, status="exited", exit_code=0)
        _bind_valid_verification_evidence(
            registry, job_id=job_id, coordinator_root=coordinator_root
        )
        assert len(launch_calls) == 1

        # Operator 換了 reviewer 的 model（同一顆 identity registry 換了
        # model_id）；不是 retry-card，只是正常 resume。
        changed_identities = _identities(reviewer_model="sonnet-844-v2")
        second = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=changed_identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )

        # 不相容變動：強制新 attempt，不得沿用舊 evidence 直接 advance。
        assert len(launch_calls) == 2, "model 變更必須觸發新的 model invocation"
        jobs = [
            job
            for job in registry.list_jobs()
            if job.get("workflow_card") == "verification"
        ]
        assert len(jobs) == 2
        assert jobs[-1]["job_id"] != stale_job["job_id"]
        assert jobs[-1]["model_id"] == "sonnet-844-v2"

        persisted = registry.get_workflow_run(run.run_id)
        receipts = persisted.stage_reuse_receipts or {}
        assert receipts["verification"]["decision"] == "fresh"
        assert "model" in receipts["verification"]["mismatched_fields"]
        assert second["reason"] == "in-flight"

    def test_legacy_job_without_stage_execution_key_keeps_prior_behavior(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S10：#844 之前建立的 job（沒有 workflow_stage_execution_key）不補值、
        不新增拒絕面——維持既有『同 run/era/card/phase/candidate 就沿用』行為。"""

        _seed_auth_cache(monkeypatch)
        registry, run, candidate = _build_verify_run(tmp_path)
        legacy_sandbox = tmp_path / "legacy-sandbox"
        legacy_sandbox.mkdir(parents=True, exist_ok=True)
        legacy_job = registry.create_job(
            task="wf-verification-legacy",
            persona="reviewer",
            kind="review",
            branch=f"feature/{WORK_ID}",
            pane="",
            worktree=str(legacy_sandbox),
            subject_head=candidate,
            executor=REVIEWER_EXECUTOR,
            model_id=REVIEWER_MODEL,
            independence_domain=REVIEWER_DOMAIN,
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card="verification",
            workflow_phase="verify",
            # `_verify_exact_candidate()` 對 reviewer persona 讀
            # `workflow_repo_root`（不是 worktree）去驗 candidate 真的是那棵樹
            # 的 HEAD；必須指向真的含有 candidate commit 的來源樹。
            workflow_repo_root=str(run.workspace_root),
            source_revision=run.source_revision,
            # 刻意不帶 workflow_stage_execution_key：模擬 #844 之前的舊資料。
        )
        registry.update_headless_result(legacy_job["job_id"], status="exited", exit_code=0)
        coordinator_root = tmp_path / "coordinator"
        _bind_valid_verification_evidence(
            registry, job_id=legacy_job["job_id"], coordinator_root=coordinator_root
        )
        launch_calls: list[str] = []

        result = manager.resume_workflow_run(
            _Dispatcher(registry),
            run_id=run.run_id,
            identities=_identities(),
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )

        # legacy job（沒有 workflow_stage_execution_key）直接被既有
        # `jobs[-1]` 短路沿用，不因為「沒有 key」而被判定拒絕、也不重派 verify
        # 卡；advance 成功後 review 卡的第一次派工是正常繼續。
        assert result["current_phase"] == "review"
        verify_jobs = [
            job for job in registry.list_jobs() if job.get("workflow_card") == "verification"
        ]
        assert [job["job_id"] for job in verify_jobs] == [legacy_job["job_id"]]
        assert launch_calls == [result.get("job_id")]
        persisted = registry.get_workflow_run(run.run_id)
        # legacy 卡不在 probe 的可辨識範圍內（沒有 key 可比對），本張卡不會有
        # provenance receipt；不得為了「補全」而發明一個。
        assert "verification" not in (persisted.stage_reuse_receipts or {})


class TestRetryCardBypassesStageReuse:
    """S09：明確 retry-card／force_new_card 要求新 attempt 時，不能被相同的
    stage_execution_key 意外吃掉。`force_new_card` 走 `_dispatch_workflow_card`
    自己的既有強制重派路徑（`retryable_latest` 短路），完全不經過
    `resume_workflow_run` 的 `_workflow_stage_reuse_probe`——即使新舊兩次的
    stage_execution_key 完全相同（同 executor/model/candidate/profile），
    operator 明確要求的新 attempt 仍必須真的建立新 job。
    """

    def test_force_new_card_dispatches_a_new_job_even_with_a_matching_stage_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_auth_cache(monkeypatch)
        registry, run, candidate = _build_verify_run(tmp_path)
        dispatcher = _Dispatcher(registry)
        coordinator_root = tmp_path / "coordinator"
        identities = _identities()
        launch_calls: list[str] = []

        first = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        job_id = first["job_id"]
        stale_job = registry.get_job(job_id)
        registry.update_headless_result(job_id, status="exited", exit_code=0)
        _bind_valid_verification_evidence(
            registry, job_id=job_id, coordinator_root=coordinator_root
        )
        assert len(launch_calls) == 1

        # Operator 明確要求 retry-build/retry-card 等效的強制重派：identities／
        # run 完全沒變（會算出與 stale_job 一模一樣的 stage_execution_key），
        # 但 force_new_card=True 必須無條件建立新 job，不得被「key 相同」擋下。
        replacement = manager.dispatch_workflow_card(
            dispatcher,
            run=registry.get_workflow_run(run.run_id),
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
            force_new_card=True,
        )

        assert replacement is not None
        assert replacement["job_id"] != stale_job["job_id"]
        assert len(launch_calls) == 2
        verify_jobs = [
            job for job in registry.list_jobs() if job.get("workflow_card") == "verification"
        ]
        assert len(verify_jobs) == 2
        # 新 job 一樣由正常 producer 算出受信 key（沒有因為是強制重派就跳過
        # S02 的 key／receipt 落地）。
        assert isinstance(replacement["workflow_stage_execution_key"], str)
        assert replacement["workflow_stage_execution_key"] == stale_job["workflow_stage_execution_key"]


class TestStageReuseProbeIneligibleClassification:
    """對抗審查 MAJOR-1 單元測試：`_workflow_stage_reuse_probe` 對「帶
    producer 產生的 key，但探測階段無法判定相容性」的 job，必須回
    ``"ineligible"``（視為不可重用），不能回 ``"legacy"``（那會被呼叫端
    誤判為可以沿用既有 `jobs[-1]`，等同放行過期 evidence）。只有真正沒
    有 key 的 legacy job 才維持 ``"legacy"``（S10 迴歸）。
    """

    @staticmethod
    def _stale_job(key: str) -> dict[str, object]:
        return {
            "status": "exited",
            "exit_code": 0,
            "workflow_stage_execution_key": key,
            "workflow_stage_execution_receipt": None,
        }

    def test_unconfigured_identity_registry_is_ineligible_not_legacy(
        self, tmp_path: Path
    ) -> None:
        """沒有任何 reviewer identity 可選（`_workflow_identity_candidates`
        對此 fail-loud 拋例外，不是回空清單）——probe 必須把這個例外也接
        進 "ineligible"（走本函式最外層的 except 分支，`reason` 因此是
        "probe-exception"，不是更細的 "no-eligible-candidate"；兩者都必須
        不是 "legacy"）。"""

        registry, run, _ = _build_verify_run(tmp_path)
        step = manager._current_workflow_step(run)
        stored_key = "a" * 64
        kind, context = manager._workflow_stage_reuse_probe(
            run=run,
            step=step,
            jobs=[self._stale_job(stored_key)],
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=lambda identity: None,
            coordinator_root=tmp_path / "coordinator",
            registry=registry,
        )
        assert kind == "ineligible"
        assert context["reason"] == "probe-exception"
        assert context["superseded_key"] == stored_key

    def test_launcher_unavailable_is_ineligible_not_legacy(self, tmp_path: Path) -> None:
        registry, run, _ = _build_verify_run(tmp_path)
        step = manager._current_workflow_step(run)
        stored_key = "b" * 64
        kind, context = manager._workflow_stage_reuse_probe(
            run=run,
            step=step,
            jobs=[self._stale_job(stored_key)],
            identities=_identities(),
            launcher_factory=lambda identity: None,
            coordinator_root=tmp_path / "coordinator",
            registry=registry,
        )
        assert kind == "ineligible"
        assert context["reason"] == "launcher-unavailable"
        assert context["superseded_key"] == stored_key

    def test_probe_exception_is_ineligible_not_legacy(self, tmp_path: Path) -> None:
        registry, run, _ = _build_verify_run(tmp_path)
        step = manager._current_workflow_step(run)
        stored_key = "c" * 64

        def boom(identity):
            raise RuntimeError("simulated launcher_factory failure")

        kind, context = manager._workflow_stage_reuse_probe(
            run=run,
            step=step,
            jobs=[self._stale_job(stored_key)],
            identities=_identities(),
            launcher_factory=boom,
            coordinator_root=tmp_path / "coordinator",
            registry=registry,
        )
        assert kind == "ineligible"
        assert context["reason"] == "probe-exception"
        assert context["superseded_key"] == stored_key

    def test_job_without_key_still_returns_legacy(self, tmp_path: Path) -> None:
        """S10 迴歸：真正沒有 key 的 legacy job 不受本次修法影響。"""

        registry, run, _ = _build_verify_run(tmp_path)
        step = manager._current_workflow_step(run)
        legacy_job = {"status": "exited", "exit_code": 0}
        kind, context = manager._workflow_stage_reuse_probe(
            run=run,
            step=step,
            jobs=[legacy_job],
            identities=IdentityRegistry.from_rows([]),
            launcher_factory=lambda identity: None,
            coordinator_root=tmp_path / "coordinator",
            registry=registry,
        )
        assert kind == "legacy"
        assert context is None


class TestStageReuseIneligibleIntegration:
    """對抗審查 MAJOR-1 整合測試：既有 verify job 帶 producer 產生的 key，
    但這次 resume 的探測階段暫時綁不出 launcher（模擬 CLI 短暫不可用等
    情境）——修法前這種情況被誤判為 "legacy"，讓既有 `jobs[-1]` 短路直接
    沿用過期 evidence；修法後必須視為 "ineligible"，強制新 attempt。
    """

    def test_ineligible_probe_forces_fresh_attempt_instead_of_silent_reuse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_auth_cache(monkeypatch)
        registry, run, candidate = _build_verify_run(tmp_path)
        dispatcher = _Dispatcher(registry)
        coordinator_root = tmp_path / "coordinator"
        identities = _identities()
        launch_calls: list[str] = []

        first = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        job_id = first["job_id"]
        stale_job = registry.get_job(job_id)
        registry.update_headless_result(job_id, status="exited", exit_code=0)
        _bind_valid_verification_evidence(
            registry, job_id=job_id, coordinator_root=coordinator_root
        )
        assert len(launch_calls) == 1

        # 探測階段（第二次 resume 的第一次 launcher_factory 呼叫）暫時綁不
        # 出 launcher；實際派工（後續呼叫，不論 `_dispatch_workflow_card`
        # 內部再呼叫幾次）用同一份 identity 綁得出來——證明「探測失敗」與
        # 「派工失敗」是兩件事，不能把前者誤判成「反正沒變」。
        probe_calls = {"n": 0}

        def flaky_then_ok_factory(identity):
            probe_calls["n"] += 1
            if probe_calls["n"] == 1:
                return None
            return _Launcher(
                identity.executor,
                identity.model_id,
                on_launch=lambda slice_id: launch_calls.append(slice_id),
            )

        second = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=flaky_then_ok_factory,
            coordinator_root=coordinator_root,
        )

        # 不得沿用舊 evidence：verify 卡必須真的產生第二顆 job／第二次 launch。
        assert len(launch_calls) == 2, (
            "探測階段綁不出 launcher 不得被當成可以沿用舊 evidence"
        )
        verify_jobs = [
            job for job in registry.list_jobs() if job.get("workflow_card") == "verification"
        ]
        assert len(verify_jobs) == 2
        assert verify_jobs[-1]["job_id"] != stale_job["job_id"]
        assert second["reason"] == "in-flight"

        persisted = registry.get_workflow_run(run.run_id)
        receipts = persisted.stage_reuse_receipts or {}
        assert receipts["verification"]["decision"] == "fresh"
        assert (
            receipts["verification"]["stage_execution_key"]
            == verify_jobs[-1]["workflow_stage_execution_key"]
        )


class TestForceNewCardOverwritesStaleReceipt:
    """對抗審查 MAJOR-2：任何為這張卡建立新 attempt 的路徑都要覆寫 receipt
    成 "fresh"，不能留著前一輪的 "reused"。`dispatch_workflow_card(
    force_new_card=True)` 是 manager_daemon.py 對 retry-build／retry-card／
    retry-verify 三個 action 共用的同一條 dispatch 路徑（`forced_card_retry`
    集合），這裡直接呼叫等同覆蓋這三者共用的機制；retry-review 見下一個
    測試類別（reset 機制不同，但落點是同一個 `_dispatch_workflow_card`
    spawn 點）。
    """

    def test_force_new_card_after_reused_resume_overwrites_receipt_to_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_auth_cache(monkeypatch)
        registry, run, candidate = _build_verify_run(tmp_path)
        dispatcher = _Dispatcher(registry)
        coordinator_root = tmp_path / "coordinator"
        identities = _identities()
        launch_calls: list[str] = []

        first = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        job_id = first["job_id"]
        registry.update_headless_result(job_id, status="exited", exit_code=0)
        _bind_valid_verification_evidence(
            registry, job_id=job_id, coordinator_root=coordinator_root
        )

        # 模擬「上一次 resume 的探測已經判定這張卡 reused」留下的 receipt
        # ——不透過第二次 `resume_workflow_run`（那會在同一次呼叫內連著把
        # phase advance 到 review，讓 current_phase 離開 verify，force_new_card
        # 就無法再命中 verify 卡本身；見 `_manager_reset_workflow_for_retry_verify`
        # 一樣要求 `current_phase == "verify"` 才能重派）。這裡直接寫入 receipt，
        # 只驗證 `dispatch_workflow_card(force_new_card=True)`（retry-build／
        # retry-card／retry-verify 三個 action 在 manager_daemon.py 共用的同一
        # 條路徑）本身的覆寫契約。
        registry._manager_update_workflow_run(
            run.run_id,
            stage_reuse_receipts={
                "verification": {
                    "decision": "reused",
                    "stage_execution_key": registry.get_job(job_id)[
                        "workflow_stage_execution_key"
                    ],
                }
            },
        )

        # Operator 明確要求 retry-build／retry-card／retry-verify 等效的強
        # 制重派：manager_daemon.py 對這三個 action 共用
        # `manager.dispatch_workflow_card(..., force_new_card=forced_card_retry)`
        # 這一條路徑（見 manager_daemon.py 的 `forced_card_retry` 集合）。
        replacement = manager.dispatch_workflow_card(
            dispatcher,
            run=registry.get_workflow_run(run.run_id),
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
            force_new_card=True,
        )
        assert replacement is not None and "job_id" in replacement
        assert replacement["job_id"] != job_id

        persisted_after_retry = registry.get_workflow_run(run.run_id)
        receipt = persisted_after_retry.stage_reuse_receipts["verification"]
        assert receipt["decision"] == "fresh", (
            "force_new_card 建立新 attempt 後，receipt 不得停留在前一輪的 'reused'"
        )
        assert receipt["stage_execution_key"] == replacement["workflow_stage_execution_key"]


class TestRetryReviewOverwritesStaleReceipt:
    """對抗審查 MAJOR-2：retry-review（`_manager_reset_workflow_for_retry_review`
    的既有重置機制，非本票修改）之後的新 attempt 同樣要覆寫 receipt 成
    "fresh"。reset 本身不直接派工（只把 review step 打回 pending、把舊
    exited job 標 failed），實際新 job 一律經同一個 `_dispatch_workflow_card`
    spawn 點——用 `force_new_card=True` 呼叫它，等同任何觸發 retry-review
    後續派工的production路徑都會落地的那一步。
    """

    def test_retry_review_reset_then_forced_dispatch_overwrites_receipt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_auth_cache(monkeypatch)
        workspace = tmp_path / "workspace"
        candidate = _init_source_repo(workspace)
        registry = JobRegistry(state_path=tmp_path / "jobs.json")
        steps = (
            _step("claim", "manager-claim", persona="manager", gate_result="passed"),
            _step("define", "planner-define", persona="planner", gate_result="passed"),
            _step("plan", "planner-plan", persona="planner", gate_result="passed"),
            _step(
                "build",
                "subagent-build",
                persona="builder",
                gate_result="passed",
                executor="codex",
                model="gpt-primary",
                domain=BUILDER_DOMAIN,
                commit_policy="required",
            ),
            _step("verify", "verification", persona="reviewer", gate_result="passed"),
            _step("review", "code-review", persona="reviewer", gate_result="needs_human"),
            _step("ship", "manager-ship", persona="manager", gate_result="pending"),
        )
        run = registry._manager_create_workflow_run(
            work_id=WORK_ID,
            repo=REPO,
            claim_key="claim:v1:" + "1" * 64,
            source_revision="2" * 64,
            workspace_root=str(workspace),
            combo="feature-oneshot",
            current_phase="review",
            steps=steps,
            issue_refs=(f"{REPO}#844",),
            openspec_refs=(WORK_ID,),
            candidate_head=candidate,
            verified_head=candidate,
            attempts={"build": 1, "verify": 1, "review": 1},
            facets=("needs_human",),
            gate_status="failed",
            needs_human_reason=fixture_needs_human_reason(),
        )
        builder_job = registry.create_job(
            task="wf-subagent-build",
            persona="builder",
            branch=f"feature/{WORK_ID}",
            pane="",
            worktree=str(workspace),
            dispatch_head="b" * 40,
            subject_head=candidate,
            executor="codex",
            model_id="gpt-primary",
            independence_domain=BUILDER_DOMAIN,
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card="subagent-build",
            workflow_phase="build",
            source_revision=run.source_revision,
        )
        registry.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)
        stale_key = "5" * 64
        review_job = registry.create_job(
            task="wf-code-review",
            persona="reviewer",
            kind="review",
            branch=f"feature/{WORK_ID}",
            pane="",
            worktree=str(workspace),
            subject_head=candidate,
            executor=REVIEWER_EXECUTOR,
            model_id=REVIEWER_MODEL,
            independence_domain=REVIEWER_DOMAIN,
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card="code-review",
            workflow_phase="review",
            workflow_repo_root=str(workspace),
            source_revision=run.source_revision,
            workflow_stage_execution_key=stale_key,
        )
        registry.update_headless_result(review_job["job_id"], status="exited", exit_code=0)
        # 模擬「上一輪 resume 已經判定過 reused」留下的 receipt——這是本測
        # 試要證明會被蓋掉的過期狀態。
        registry._manager_update_workflow_run(
            run.run_id,
            stage_reuse_receipts={
                "code-review": {"decision": "reused", "stage_execution_key": stale_key}
            },
        )

        # 產品 `_retry_review_action` 實際呼叫的重置函式（非本票修改）：把
        # review step 打回 pending、舊 exited job 標 failed、清 needs_human。
        reset_run = registry._manager_reset_workflow_for_retry_review(
            run.run_id, expected_candidate=candidate
        )
        assert registry.get_job(review_job["job_id"])["status"] == "failed"
        assert "needs_human" not in reset_run.facets

        launch_calls: list[str] = []
        replacement = manager.dispatch_workflow_card(
            _Dispatcher(registry),
            run=registry.get_workflow_run(run.run_id),
            identities=_identities(),
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=tmp_path / "coordinator",
            force_new_card=True,
        )

        assert replacement is not None and "job_id" in replacement
        assert replacement["job_id"] != review_job["job_id"]
        assert len(launch_calls) == 1

        persisted = registry.get_workflow_run(run.run_id)
        receipt = persisted.stage_reuse_receipts["code-review"]
        assert receipt["decision"] == "fresh", (
            "retry-review 的新 attempt 後，receipt 不得停留在前一輪的 'reused'"
        )
        assert receipt["stage_execution_key"] == replacement["workflow_stage_execution_key"]
        assert receipt["stage_execution_key"] != stale_key


class TestStageReuseReceiptsRollbackCompat:
    """對抗審查 MAJOR-3（記錄但不修）：新頂層欄位 `stage_reuse_receipts`
    是否會讓舊版 Monitor 的 canonical 讀取路徑炸掉？答案是不會——舊版
    `WorkflowRun.from_dict()` 走既有白名單 dataclass 建構，逐欄
    `payload.get(...)` 讀取，未知頂層鍵直接被忽略（不是
    reject-unknown-keys 的 strict schema），與既有 `model_qualification`
    等既有欄位新增時完全同一種相容模式。

    這裡直接載入 `feature/refine-wave-2`（#844 之前、`stage_reuse_receipts`
    尚未存在的基底）那份 workflow.py 原始碼本身，而不是憑印象重寫一份可
    能失真的舊版驗證邏輯，證明：本分支寫出的 registry payload 給那份舊
    `WorkflowRun.from_dict()` 讀仍不會炸，且它自己的 `to_dict()` 輸出裡
    沒有新欄位。
    """

    @staticmethod
    def _load_refine_wave_2_workflow_module() -> types.ModuleType:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "show", "feature/refine-wave-2:paulsha_cortex/coordinator/workflow.py"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        module_name = "paulsha_cortex.coordinator._workflow_refine_wave_2_snapshot_844"
        module = types.ModuleType(module_name)
        # `__package__` 指到現有的 `paulsha_cortex.coordinator`，讓這份舊原
        # 始碼裡的 `from . import verification` 等 relative import 解析到
        # 「現在這個 checkout、沒有為 #844 動過」的同名 submodule，而不必自
        # 己重建一整套 import 環境。
        module.__dict__["__package__"] = "paulsha_cortex.coordinator"
        module.__dict__["__name__"] = module_name
        module.__dict__["__file__"] = "feature/refine-wave-2:paulsha_cortex/coordinator/workflow.py"
        # `from __future__ import annotations` 讓這份舊原始碼的 dataclass 欄位
        # annotation 都是字串；`dataclasses` 在處理 class body 時會用
        # `sys.modules[cls.__module__]` 反查型別（例如判斷是不是 ClassVar／
        # KW_ONLY）——不先掛進 `sys.modules`，這一步會直接炸掉。獨一無二的
        # module name，測試結束後不影響其他模組。
        sys.modules[module_name] = module
        try:
            exec(compile(result.stdout, module.__dict__["__file__"], "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        return module

    def test_old_workflow_run_from_dict_tolerates_new_field_and_omits_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_module = self._load_refine_wave_2_workflow_module()

        _seed_auth_cache(monkeypatch)
        registry, run, candidate = _build_verify_run(tmp_path)
        dispatcher = _Dispatcher(registry)
        coordinator_root = tmp_path / "coordinator"
        identities = _identities()
        launch_calls: list[str] = []

        first = manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )
        job_id = first["job_id"]
        registry.update_headless_result(job_id, status="exited", exit_code=0)
        _bind_valid_verification_evidence(
            registry, job_id=job_id, coordinator_root=coordinator_root
        )
        manager.resume_workflow_run(
            dispatcher,
            run_id=run.run_id,
            identities=identities,
            launcher_factory=_launcher_factory(launch_calls),
            coordinator_root=coordinator_root,
        )

        persisted = registry.get_workflow_run(run.run_id)
        payload = persisted.to_dict()
        # 先確認新版自己真的寫了這個欄位——否則下面「舊版讀了會怎樣」的
        # 驗證就是驗證了一個沒發生的情境。
        assert "stage_reuse_receipts" in payload
        assert payload["stage_reuse_receipts"]["verification"]["decision"] == "reused"

        old_run = old_module.WorkflowRun.from_dict(payload)
        assert old_run.run_id == persisted.run_id
        assert old_run.work_id == persisted.work_id
        assert old_run.current_phase == persisted.current_phase
        assert not hasattr(old_run, "stage_reuse_receipts")

        old_payload = old_run.to_dict()
        assert "stage_reuse_receipts" not in old_payload

        # 反向也要成立：新版讀舊版（沒有這個欄位）的 payload 一樣正常，
        # 缺席時維持 None（S10 的另一面）。
        new_run_from_old_payload = WorkflowRun.from_dict(old_payload)
        assert new_run_from_old_payload.stage_reuse_receipts is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
