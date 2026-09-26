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
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager, runtime_preflight
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
