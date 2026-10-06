"""#844 缺口補齊（G844-1／G844-2）：production stage-evidence reuse 的採信契約。

`tests/test_workflow_stage_execution_reuse_844.py` 已涵蓋 S01／S02 主幹與
builder job／人裁／model 三種不相容變動。本檔補 refine 盤點列出的缺塊：

- S01：經正式 `work-action` → daemon production executor（`build_request_executor`）
  → `resume_workflow_run` 的完整入口；verify／review evidence 一律由
  `terminalize_workflow_job` 從真實格式 terminal envelope harvest 產生，不手刻。
- S03／S04：test-policy、candidate、planning authority、claim-era、native effort、
  adapter runtime version、toolset 變動逐項拒絕沿用；pricing metadata 不影響 key。
- S05：偽造 provenance、他 run job、非零 exit、撤銷／未採信 evidence、過期 review、
  缺檔／壞 hash／symlink／越界各自拒絕，且不留下 `reused` receipt。
- S06：review 卡的 builder／identity／independence／authority 不符不得重用。
- S07：reuse 決策與採信之間注入 drift，由採信點以同一份 CAS 保護的 run 快照重新
  裁決（G844-2 的「採信時驗證」）。
- S08：各 crash 點重啟、重送與交錯雙 request 只得到一次有效接續；延遲 terminal
  不覆蓋已採信 evidence。
- S09：正式 retry-card 不被相同 key 吃掉；active attempt 不因 lookup miss 另開；
  authority restart 讓已接受 gate 回 pending 時，action result 與 receipt 列出原因。
- S10：build 卡不在 cohort、舊 schema receipt 不破壞 registry 讀取、ineligible 無 job。
- S11：reused receipt 連結來源 run／job／evidence hash（G844-2），並呈現在 action
  result 與 `cortex work show`；CompletionRecord 的 `reused_from` 不豁免完整驗證。

全程使用正式 producer（`_dispatch_workflow_card`）、production evidence reader 與
domain validator；launcher 是唯一的替身（它代表模型行程本身，用來計數 model
invocation）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import (
    completion,
    execution_adapters,
    manager,
    manager_daemon,
    quota_admission,
    runtime_preflight,
    verification,
    work_actions,
)
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import (
    STAGE_EXECUTION_KEY_SCHEMA_VERSION,
    JobRegistry,
)
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowRun

import test_work_actions_retry_invalidation as retry_invalidation
import test_workflow_stage_execution_reuse_844 as base
from diagnostic_fixtures import fixture_needs_human_reason

WORK_ID = base.WORK_ID
CLAIM_KEY = "claim:v1:" + "1" * 64
SOURCE_REVISION = "2" * 64
REPORTS = {
    "verification": "reports/verify/stage-reuse-844-verification.md",
    "code-review": "reports/review/stage-reuse-844-code-review.md",
    "adversarial-review": "reports/review/stage-reuse-844-adversarial.md",
}
OUTPUT_GLOBS = {
    "verification": "reports/verify/*stage-reuse-844-verification*.md",
    "code-review": "reports/review/*stage-reuse-844-code-review*.md",
    "adversarial-review": "reports/review/*stage-reuse-844-adversarial*.md",
}
BLOCKING_FINDING = {
    "category": "correctness",
    "severity": "important",
    "summary": "#844 fixture：阻擋性 finding。",
    "evidence": [{"path": base.PLAN_REF, "line": 1, "detail": "fixture"}],
    "recommendation": "修正後重審。",
}


class _Crash(BaseException):
    """模擬行程在某一點死亡：刻意不是 `Exception`，不會被 resume 的 fail-closed
    例外處理吃掉（真正的 crash 也不會有機會寫 needs_human）。"""


def _steps(*, verify_test_policy: str | None = None, build_domain: str = base.BUILDER_DOMAIN):
    rows = []
    for step in base._steps():
        if step.card == "subagent-build":
            rows.append(replace(step, domain=build_domain))
        elif step.card == "verification":
            rows.append(
                replace(step, outputs=(OUTPUT_GLOBS["verification"],), test_policy=verify_test_policy)
            )
        elif step.card == "code-review":
            rows.append(replace(step, outputs=(OUTPUT_GLOBS["code-review"],)))
            # 第二張 review 卡讓 code-review 採信後仍停在 review phase（不必牽入
            # ship validator），下一張卡的首次派工就是「接續」的可觀測證據。
            rows.append(
                replace(
                    base._step("review", "adversarial-review", persona="reviewer"),
                    outputs=(OUTPUT_GLOBS["adversarial-review"],),
                )
            )
        else:
            rows.append(step)
    return tuple(rows)


def _planning_authority(text: str = base.PLAN_TEXT) -> tuple[PlanningArtifactAuthority, ...]:
    return (
        PlanningArtifactAuthority(
            ref=base.PLAN_REF,
            kind="plan",
            work_id=WORK_ID,
            baseline_sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),
    )


def _seed_provider_auth(monkeypatch: pytest.MonkeyPatch, *providers: str) -> None:
    base._seed_auth_cache(monkeypatch)
    now = manager.time.time()
    for provider_id in providers:
        manager._EXECUTOR_AUTH_CACHE[provider_id] = runtime_preflight.ProviderFreshness(
            provider_id=provider_id,
            status="ok",
            observed_at=now,
            ttl_seconds=900.0,
            source="snapshot",
        )


class Harness:
    """一條停在 verify 的 canonical run：build 已 passed、candidate 在真實 git 樹。

    `finish()` 只做「模型行程結束」這件事：把真實格式的 terminal envelope 寫進
    producer 指派的 log、把 job 標成 exited/0。canonical evidence 一律留給正式的
    `terminalize_workflow_job` harvest 產生。
    """

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        identities: IdentityRegistry | None = None,
        repo: str = base.REPO,
        claim_key: str = CLAIM_KEY,
        source_revision: str = SOURCE_REVISION,
        planning_authority: tuple[PlanningArtifactAuthority, ...] = (),
        verify_test_policy: str | None = None,
        state_dir: Path | None = None,
    ) -> None:
        base._seed_auth_cache(monkeypatch)
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.candidate = base._init_source_repo(self.workspace)
        self.root = state_dir or (tmp_path / "coordinator")
        self.registry = JobRegistry(state_path=self.root / "jobs.json")
        self.identities = identities or base._identities()
        self.launches: list[str] = []
        run = self.registry._manager_create_workflow_run(
            work_id=WORK_ID,
            repo=repo,
            claim_key=claim_key,
            source_revision=source_revision,
            workspace_root=str(self.workspace),
            combo="feature-oneshot",
            current_phase="verify",
            steps=_steps(verify_test_policy=verify_test_policy),
            issue_refs=(f"{repo}#844",),
            openspec_refs=(WORK_ID,),
            candidate_head=self.candidate,
            attempts={"build": 1, "verify": 1},
            gate_status="running",
            planning_authority=planning_authority,
        )
        self.run_id = run.run_id
        self.builder_job_id = self.add_builder(self.candidate)
        self.dispatcher = base._Dispatcher(self.registry)

    # -- 建構 -----------------------------------------------------------------

    def add_builder(self, candidate: str, *, suffix: str = "") -> str:
        run = self.run()
        builder = self.registry.create_job(
            task=f"wf-subagent-build{suffix}",
            persona="builder",
            branch=f"feature/{WORK_ID}",
            pane="",
            worktree=str(self.workspace),
            dispatch_head="b" * 40,
            subject_head=candidate,
            executor="codex",
            model_id="gpt-primary",
            independence_domain=base.BUILDER_DOMAIN,
            workflow_run_id=run.run_id,
            workflow_claim_key=run.claim_key,
            workflow_repo=run.repo,
            workflow_card="subagent-build",
            workflow_phase="build",
            source_revision=run.source_revision,
        )
        self.registry.update_headless_result(builder["job_id"], status="exited", exit_code=0)
        return str(builder["job_id"])

    def launcher_factory(self, **launcher_attrs):
        def factory(identity):
            launcher = base._Launcher(
                identity.executor, identity.model_id, on_launch=self.launches.append
            )
            for name, value in launcher_attrs.items():
                setattr(launcher, name, value)
            return launcher

        return factory

    # -- 動作 -----------------------------------------------------------------

    def resume(self, *, identities=None, launcher_factory=None, registry=None, **kwargs):
        active = registry or self.registry
        return manager.resume_workflow_run(
            base._Dispatcher(active),
            run_id=self.run_id,
            identities=identities or self.identities,
            launcher_factory=launcher_factory or self.launcher_factory(),
            coordinator_root=self.root,
            **kwargs,
        )

    def finish(self, job_id: str, *, findings=(), exit_code: int = 0) -> None:
        job = self.registry.get_job(job_id)
        card = job["workflow_card"]
        report = {"path": REPORTS[card], "body": f"# {card}\n\n#844 fixture.\n"}
        if card == "verification":
            terminal = {
                "schema_version": 1,
                "kind": "workflow-verification-result",
                "status": "verified",
                "summary": "#844 fixture：驗證通過。",
                "details": {"pytest": "passed"},
                "reports": [report],
            }
        else:
            terminal = {
                "schema_version": 1,
                "kind": "workflow-review-result",
                "reason": "#844 fixture review。",
                "findings": list(findings),
                "reports": [report],
            }
        Path(job["log_path"]).write_text(json.dumps(terminal) + "\n", encoding="utf-8")
        self.registry.update_headless_result(job_id, status="exited", exit_code=exit_code)

    def adopt_verification(self) -> str:
        """正常 producer 派 verify → 模型結束 → 第二次 resume 採信（reused）。"""

        first = self.resume()
        assert first["reason"] == "in-flight"
        verify_job = first["job_id"]
        self.finish(verify_job)
        second = self.resume()
        assert second["current_phase"] == "review", second
        return verify_job

    # -- 觀測 -----------------------------------------------------------------

    def run(self):
        return self.registry.get_workflow_run(self.run_id)

    def jobs(self, card: str) -> list[dict]:
        return [
            job
            for job in self.registry.list_jobs()
            if job.get("workflow_run_id") == self.run_id and job.get("workflow_card") == card
        ]

    def gate(self, card: str) -> str:
        return {step.card: step.gate_result for step in self.run().steps}[card]

    def receipt(self, card: str) -> dict | None:
        return (self.run().stage_reuse_receipts or {}).get(card)

    def evidence_path(self, job_id: str) -> Path:
        locator = self.registry.get_job(job_id)["workflow_evidence"]
        return self.root / locator["path"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fresh_receipt(harness: Harness, job_id: str) -> dict:
    """正式派工當下寫的 fresh receipt（新 attempt 本身就是來源）。"""

    return {
        "decision": "fresh",
        "receipt_schema_version": 2,
        "stage_execution_key": harness.registry.get_job(job_id)["workflow_stage_execution_key"],
        "job_id": job_id,
    }


def _assert_reused_receipt(receipt: dict, *, harness: Harness, source_job_id: str, adoption: str = "accepted") -> None:
    """G844-2：reused receipt 必須逐欄連回來源 run／job／evidence hash。"""

    source = harness.registry.get_job(source_job_id)
    locator = source["workflow_evidence"]
    assert receipt["decision"] == "reused"
    assert receipt["receipt_schema_version"] == 2
    assert receipt["stage_execution_key"] == source["workflow_stage_execution_key"]
    assert receipt["stage_execution_key_schema_version"] == STAGE_EXECUTION_KEY_SCHEMA_VERSION
    assert receipt["compatibility"] == "exact-stage-execution-key"
    assert receipt["phase"] == source["workflow_phase"]
    assert receipt["candidate_sha"] == source["subject_head"]
    assert receipt["source_run_id"] == harness.run_id == source["workflow_run_id"]
    assert receipt["source_claim_key"] == source["workflow_claim_key"]
    assert receipt["source_job_id"] == source_job_id
    assert receipt["source_evidence_path"] == locator["path"]
    assert receipt["source_evidence_hash"] == locator["hash"]
    assert receipt["source_evidence_hash"] == _sha256(harness.root / locator["path"])
    assert receipt["adoption"] == adoption


# ===========================================================================
# S01：正式 work-action → daemon production executor → resume
# ===========================================================================

DAEMON_REPO = "acme/stage-reuse"


def _daemon_snapshot(path: Path, *, source_revisions: list[str] | None = None) -> Path:
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
                        "repo": DAEMON_REPO,
                        "work_id": WORK_ID,
                        "mapped_issues": [844],
                        "mapped_prs": [],
                        "mapped_openspec": [WORK_ID],
                        "mapped_todo_paths": ["docs/todo.md"],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": source_revisions
                        or ["issue:844@open", f"openspec:{WORK_ID}@1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class _DaemonHarness(Harness):
    """同一條 run，但 claim/source revision 由真實 WorkAuthority 導出，並經
    `manager_daemon.build_request_executor`（`serve` 使用的同一個 production
    factory）送 `work-action`。work action 本身是真的
    `work_actions.execute_work_action`，只注入 snapshot 位置。"""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.snapshot = _daemon_snapshot(tmp_path / "snapshot.json")
        authority = work_actions.load_work_authority(
            repo=DAEMON_REPO, work_id=WORK_ID, snapshot_path=self.snapshot
        )
        super().__init__(
            tmp_path,
            monkeypatch,
            repo=DAEMON_REPO,
            claim_key=work_actions._expected_claim_key(authority),
            source_revision=work_actions.work_authority_digest(authority),
        )

        def work_action_fn(*, args, requested_by):
            return work_actions.execute_work_action(
                args=args,
                requested_by=requested_by,
                snapshot_path=self.snapshot,
                state_path=tmp_path / "runs.json",
                workflow_registry=self.registry,
            )

        self.executor = manager_daemon.build_request_executor(
            dispatcher=self.dispatcher,
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=base._Launcher(
                base.REVIEWER_EXECUTOR, base.REVIEWER_MODEL, on_launch=self.launches.append
            ),
            workflow_identity_registry=self.identities,
            work_action_fn=work_action_fn,
        )

    def work(self, action: str = "resume", **extra) -> dict:
        return self.executor(
            build_request(
                req_type="work-action",
                args={"action": action, "repo": DAEMON_REPO, "work_id": WORK_ID, **extra},
                requested_by="operator",
            )
        )


class TestS01PublicEntry:
    def test_public_work_resume_reuses_harvested_evidence_without_new_invocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _DaemonHarness(tmp_path, monkeypatch)

        first = harness.work()["result"]
        assert first["reason"] == "in-flight"
        verify_job = first["job_id"]
        assert first["stage_reuse"]["decision"] == "fresh"
        assert first["stage_reuse"]["job_id"] == verify_job
        assert harness.launches == [verify_job]

        harness.finish(verify_job)
        second = harness.work()["result"]

        # 無新 verify invocation；gate 前進並接續派下一張卡。
        assert [job["job_id"] for job in harness.jobs("verification")] == [verify_job]
        assert harness.launches == [verify_job, second["job_id"]]
        assert second["current_phase"] == "review"
        assert harness.gate("verification") == "passed"
        # evidence 是 production harvest 產生並綁定的，不是測試手刻。
        assert harness.registry.get_job(verify_job)["workflow_evidence"]["kind"] == "verify"
        # S11：action result 與 run projection 都能辨認 reused 及其來源。
        assert second["stage_reuse"]["card"] == "verification"
        _assert_reused_receipt(second["stage_reuse"], harness=harness, source_job_id=verify_job)
        _assert_reused_receipt(
            second["run"]["stage_reuse_receipts"]["verification"],
            harness=harness,
            source_job_id=verify_job,
        )

    def test_public_work_resume_rejects_caller_supplied_stage_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = _DaemonHarness(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="resume rejects caller evidence/input"):
            harness.work(stage_execution_key="a" * 64)
        assert harness.launches == []

    def test_production_executor_legacy_start_never_adopts_caller_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """legacy `workflow-action/start` 仍收 key，但 production factory 沒有注入
        validator：即使呼叫端帶來的 key 與一顆成功且已綁 evidence 的 job 完全相同，
        也不得短路派工（caller key 不是 admission authority）。"""

        harness = Harness(tmp_path, monkeypatch)
        verify_job = harness.adopt_verification()
        stored_key = harness.registry.get_job(verify_job)["workflow_stage_execution_key"]
        run = harness.run()
        monkeypatch.setattr(
            manager,
            "apply_workflow_action",
            lambda *a, **k: {"run_id": run.run_id, "current_phase": run.current_phase},
        )
        dispatched: list[dict] = []

        def fake_dispatch(*args, **kwargs):
            dispatched.append(kwargs)
            return harness.registry.create_job(
                task="start-dispatch",
                persona="reviewer",
                branch=f"feature/{WORK_ID}",
                pane="",
                worktree=str(tmp_path / "start-dispatch"),
                workflow_run_id=run.run_id,
                workflow_card="adversarial-review",
            )

        monkeypatch.setattr(manager, "dispatch_workflow_card", fake_dispatch)
        executor = manager_daemon.build_request_executor(
            dispatcher=harness.dispatcher,
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            workflow_identity_registry=harness.identities,
        )
        result = executor(
            build_request(
                req_type="workflow-action",
                args={"action": "start", "stage_execution_key": stored_key},
                requested_by="operator",
            )
        )
        assert "reused_from" not in result
        assert len(dispatched) == 1


# ===========================================================================
# S03：逐欄不相容 → 拒絕沿用、走合法派工，不誤放行 gate
# ===========================================================================


class TestS03IncompatibleInputs:
    def _stale_verify(self, tmp_path, monkeypatch, **harness_kwargs):
        harness = Harness(tmp_path, monkeypatch, **harness_kwargs)
        first = harness.resume()
        verify_job = first["job_id"]
        harness.finish(verify_job)
        return harness, verify_job

    def test_test_policy_change_forces_fresh_attempt(self, tmp_path, monkeypatch) -> None:
        harness, old_job = self._stale_verify(tmp_path, monkeypatch)
        run = harness.run()
        harness.registry._manager_update_workflow_run(
            run.run_id,
            steps=tuple(
                replace(step, test_policy="focused") if step.card == "verification" else step
                for step in run.steps
            ),
        )

        result = harness.resume()

        jobs = harness.jobs("verification")
        assert [job["job_id"] for job in jobs][0] == old_job and len(jobs) == 2
        assert harness.gate("verification") == "pending"
        assert result["reason"] == "in-flight" and result["job_id"] == jobs[-1]["job_id"]
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "fresh"
        assert receipt["job_id"] == jobs[-1]["job_id"]
        assert receipt["mismatched_fields"] == ["test_policy"]
        assert result["stage_reuse"] == {"card": "verification", **receipt}

    def test_candidate_change_never_adopts_evidence_of_previous_candidate(
        self, tmp_path, monkeypatch
    ) -> None:
        harness, old_job = self._stale_verify(tmp_path, monkeypatch)
        (harness.workspace / "src.txt").write_text("next candidate\n", encoding="utf-8")
        base._run_git(["add", "."], harness.workspace)
        base._run_git(["commit", "-qm", "next"], harness.workspace)
        new_candidate = base._run_git(["rev-parse", "HEAD"], harness.workspace).stdout.strip()
        harness.add_builder(new_candidate, suffix="-next")
        harness.registry._manager_update_workflow_run(harness.run_id, candidate_head=new_candidate)

        result = harness.resume()

        jobs = harness.jobs("verification")
        assert len(jobs) == 2
        assert jobs[-1]["subject_head"] == new_candidate
        assert harness.registry.get_job(old_job).get("workflow_evidence") is None
        assert harness.gate("verification") == "pending"
        assert result["job_id"] == jobs[-1]["job_id"]
        assert harness.receipt("verification")["decision"] == "fresh"

    def test_planning_authority_change_rejects_old_evidence(self, tmp_path, monkeypatch) -> None:
        harness, old_job = self._stale_verify(
            tmp_path, monkeypatch, planning_authority=_planning_authority()
        )
        # authority 前進：plan 內容與 baseline 同步更新（沒有 drift stop 的合法路徑）。
        new_text = "# plan v2\n"
        (harness.workspace / base.PLAN_REF).write_text(new_text, encoding="utf-8")
        harness.registry._manager_update_workflow_run(
            harness.run_id, planning_authority=_planning_authority(new_text)
        )

        run = harness.run()
        kind, context = manager._workflow_stage_reuse_probe(
            run=run,
            step=manager._current_workflow_step(run),
            jobs=harness.jobs("verification"),
            identities=harness.identities,
            launcher_factory=harness.launcher_factory(),
            coordinator_root=harness.root,
            registry=harness.registry,
        )
        assert kind == "stale"
        assert context["mismatched_fields"] == ["frozen_input_hashes"]

        # 拒絕沿用後走既有派工決策：candidate 內的 plan 仍是舊版，verify 派工前
        # 的 planning drift guard fail closed（不派工、不放行 gate）。
        with pytest.raises(ValueError, match="planning drift"):
            harness.resume()

        assert harness.gate("verification") == "pending"
        assert [job["job_id"] for job in harness.jobs("verification")] == [old_job]
        assert harness.registry.get_job(old_job).get("workflow_evidence") is None
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "ineligible"
        assert receipt["reason"] == "stale-dispatch-failed"
        assert receipt["superseded_key"] == harness.registry.get_job(old_job)[
            "workflow_stage_execution_key"
        ]

    def test_claim_era_change_dispatches_new_era_job(self, tmp_path, monkeypatch) -> None:
        harness, old_job = self._stale_verify(tmp_path, monkeypatch)
        run = harness.run()
        harness.registry._manager_reset_workflow_for_authority_restart(
            run.run_id, expected_run=run, authority_digest="9" * 64
        )
        new_run = harness.run()
        assert new_run.claim_key != run.claim_key

        result = harness.resume()

        jobs = harness.jobs("verification")
        assert len(jobs) == 2
        assert jobs[-1]["workflow_claim_key"] == new_run.claim_key
        assert result["job_id"] == jobs[-1]["job_id"]
        assert harness.gate("verification") == "pending"
        assert harness.registry.get_job(old_job).get("workflow_evidence") is None


# ===========================================================================
# S04：執行語意（effort／adapter／toolset）變動重新裁決；pricing 不影響
# ===========================================================================


def _codex_reviewer_identities() -> IdentityRegistry:
    """reviewer 用有 effort 文法的 adapter；builder domain 改成 anthropic 以維持
    independence。"""

    return IdentityRegistry.from_rows(
        [
            {
                "executor": "claude",
                "model_id": "builder-844",
                "independence_domain": "anthropic",
                "capabilities": ["build"],
            },
            {
                "executor": "codex",
                "model_id": "reviewer-844",
                "independence_domain": "openai",
                "capabilities": ["review"],
            },
        ]
    )


class TestS04ExecutionSemantics:
    def _effort_harness(self, tmp_path, monkeypatch):
        _seed_provider_auth(monkeypatch)
        harness = Harness(tmp_path, monkeypatch, identities=_codex_reviewer_identities())
        run = harness.run()
        harness.registry._manager_update_workflow_run(
            run.run_id,
            steps=tuple(
                replace(step, executor="claude", model="builder-844", domain="anthropic")
                if step.card == "subagent-build"
                else step
                for step in run.steps
            ),
        )
        return harness

    def test_native_effort_change_invalidates_reuse(self, tmp_path, monkeypatch) -> None:
        harness = self._effort_harness(tmp_path, monkeypatch)
        first = harness.resume(launcher_factory=harness.launcher_factory(_effort="high"))
        harness.finish(first["job_id"])

        harness.resume(launcher_factory=harness.launcher_factory(_effort="low"))

        assert len(harness.jobs("verification")) == 2
        assert harness.gate("verification") == "pending"
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "fresh"
        assert receipt["mismatched_fields"] == ["execution_profile_key"]

    def test_same_effort_is_reused(self, tmp_path, monkeypatch) -> None:
        harness = self._effort_harness(tmp_path, monkeypatch)
        first = harness.resume(launcher_factory=harness.launcher_factory(_effort="high"))
        harness.finish(first["job_id"])

        harness.resume(launcher_factory=harness.launcher_factory(_effort="high"))

        assert len(harness.jobs("verification")) == 1
        _assert_reused_receipt(
            harness.receipt("verification"), harness=harness, source_job_id=first["job_id"]
        )

    def test_adapter_runtime_version_change_invalidates_reuse(self, tmp_path, monkeypatch) -> None:
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        harness.finish(first["job_id"])
        # #835 起 adapter 由 descriptor catalog 提供；以受信任程式碼註冊路徑替換
        # claude adapter 的 runtime_version（monkeypatch 會在測試後移除該註冊）。
        monkeypatch.setitem(
            execution_adapters._REGISTERED_ADAPTERS,
            "claude",
            replace(
                execution_adapters.adapter_for("claude"), runtime_version="cortex-adapter-v2"
            ),
        )

        harness.resume()

        assert len(harness.jobs("verification")) == 2
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "fresh"
        assert receipt["mismatched_fields"] == ["execution_profile_key"]

    def test_toolset_change_invalidates_reuse(self, tmp_path, monkeypatch) -> None:
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume(launcher_factory=harness.launcher_factory(_effective_tools=("read",)))
        harness.finish(first["job_id"])

        harness.resume(
            launcher_factory=harness.launcher_factory(_effective_tools=("read", "web-fetch"))
        )

        assert len(harness.jobs("verification")) == 2
        assert harness.receipt("verification")["mismatched_fields"] == ["execution_profile_key"]

    def test_pricing_metadata_does_not_change_stage_key_but_effort_does(self, tmp_path) -> None:
        registry, run, _candidate = base._build_verify_run(tmp_path)
        step = manager._current_workflow_step(run)
        identity = _codex_reviewer_identities().require("codex", "reviewer-844")

        def key(**profile_kwargs) -> str:
            binding = execution_adapters.resolve_profile(identity, "reviewer", **profile_kwargs)
            context = manager._workflow_stage_execution_context(
                run=run,
                step=step,
                identity=identity,
                profile_binding=binding,
                builder_job_id="builder-job",
                manager_gate_ledger=None,
                operator_adjudications=None,
            )
            return context["key"]

        cheap = key(effort="high", metadata={"pricing": {"input_per_mtok": 1}})
        pricey = key(effort="high", metadata={"pricing": {"input_per_mtok": 9}})
        assert cheap == pricey
        assert key(effort="low") != cheap

    def test_loadout_version_change_alone_changes_stage_key(self, tmp_path) -> None:
        """#844 S04：loadout 的 id 由 persona 決定，唯一能單獨變動的是版本；
        只改 loadout 版本也必須換 stage key，其餘條件相同時 key 穩定。"""

        registry, run, _candidate = base._build_verify_run(tmp_path)
        step = manager._current_workflow_step(run)
        identity = _codex_reviewer_identities().require("codex", "reviewer-844")

        def key(loadout_version: str) -> str:
            binding = execution_adapters.resolve_profile(
                identity,
                "reviewer",
                effort="high",
                launch_contract={"loadout_version": loadout_version},
            )
            assert binding.resolved.conditions["loadout"]["value"] == {
                "id": "reviewer",
                "version": loadout_version,
            }
            context = manager._workflow_stage_execution_context(
                run=run,
                step=step,
                identity=identity,
                profile_binding=binding,
                builder_job_id="builder-job",
                manager_gate_ledger=None,
                operator_adjudications=None,
            )
            return context["key"]

        assert key("1") == key("1")
        assert key("2") != key("1")


# ===========================================================================
# S05：偽造與損壞的來源各自拒絕，失敗不留 reused receipt
# ===========================================================================


class TestS05RejectedSources:
    def test_forged_key_without_matching_provenance_is_not_reused(self, tmp_path, monkeypatch) -> None:
        identities = IdentityRegistry.from_rows(
            [
                *(
                    {
                        "executor": row.executor,
                        "model_id": row.model_id,
                        "independence_domain": row.independence_domain,
                        "capabilities": list(row.capabilities),
                    }
                    for row in base._identities().identities
                ),
                # 登錄但不具 review capability：永遠不會被選中派工，只是讓偽造
                # job 的 identity 能通過 registry 查詢。
                {
                    "executor": base.REVIEWER_EXECUTOR,
                    "model_id": "sonnet-forged",
                    "independence_domain": base.REVIEWER_DOMAIN,
                    "capabilities": [],
                },
            ]
        )
        harness = Harness(tmp_path, monkeypatch, identities=identities)
        first = harness.resume()
        genuine = harness.registry.get_job(first["job_id"])
        # 真 job 先以其他方式終止（不採信），再造一顆偽造 job：抄同一把 key，
        # 但 receipt 缺席、實際 identity 不同——典型的「只有 key 相同」偽造。
        harness.registry.update_headless_result(genuine["job_id"], status="failed", exit_code=1)
        forged = harness.registry.create_job(
            task="wf-verification-forged",
            persona="reviewer",
            kind="review",
            branch=f"feature/{WORK_ID}",
            pane="",
            worktree=str(tmp_path / "forged-sandbox"),
            subject_head=harness.candidate,
            executor=base.REVIEWER_EXECUTOR,
            model_id="sonnet-forged",
            independence_domain=base.REVIEWER_DOMAIN,
            workflow_run_id=harness.run_id,
            workflow_claim_key=harness.run().claim_key,
            workflow_repo=harness.run().repo,
            workflow_card="verification",
            workflow_phase="verify",
            workflow_repo_root=str(harness.workspace),
            workflow_outputs=(OUTPUT_GLOBS["verification"],),
            workflow_output_baseline=genuine.get("workflow_output_baseline", []),
            source_revision=harness.run().source_revision,
            workflow_stage_execution_key=genuine["workflow_stage_execution_key"],
        )
        log = tmp_path / "forged.jsonl"
        log.write_text("", encoding="utf-8")
        harness.registry.attach_launch_handle(
            forged["job_id"], executor=base.REVIEWER_EXECUTOR, model_id="sonnet-forged", log_path=str(log)
        )
        harness.finish(forged["job_id"])

        result = harness.resume()

        assert harness.gate("verification") == "pending"
        jobs = harness.jobs("verification")
        assert jobs[-1]["job_id"] not in {forged["job_id"], genuine["job_id"]}
        assert result["job_id"] == jobs[-1]["job_id"]
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "fresh"
        assert "job_identity" in receipt["mismatched_fields"]
        assert harness.registry.get_job(forged["job_id"]).get("workflow_evidence") is None

    def test_other_run_successful_job_is_never_adopted(self, tmp_path, monkeypatch) -> None:
        harness = Harness(tmp_path, monkeypatch)
        other_root = tmp_path / "other"
        other_root.mkdir()
        other = harness.registry._manager_create_workflow_run(
            work_id="other-work",
            repo=base.REPO,
            claim_key="claim:v1:" + "3" * 64,
            source_revision=SOURCE_REVISION,
            workspace_root=str(harness.workspace),
            combo="feature-oneshot",
            current_phase="verify",
            steps=_steps(),
            issue_refs=(f"{base.REPO}#845",),
            openspec_refs=("other-work",),
            candidate_head=harness.candidate,
            attempts={"build": 1, "verify": 1},
            gate_status="running",
        )
        foreign = harness.registry.create_job(
            task="wf-verification-other-run",
            persona="reviewer",
            kind="review",
            branch=f"feature/{WORK_ID}",
            pane="",
            worktree=str(other_root),
            subject_head=harness.candidate,
            executor=base.REVIEWER_EXECUTOR,
            model_id=base.REVIEWER_MODEL,
            independence_domain=base.REVIEWER_DOMAIN,
            workflow_run_id=other.run_id,
            workflow_claim_key=other.claim_key,
            workflow_repo=other.repo,
            workflow_card="verification",
            workflow_phase="verify",
            workflow_repo_root=str(harness.workspace),
            source_revision=other.source_revision,
        )
        harness.registry.update_headless_result(foreign["job_id"], status="exited", exit_code=0)

        result = harness.resume()

        jobs = harness.jobs("verification")
        assert len(jobs) == 1 and jobs[0]["job_id"] != foreign["job_id"]
        assert result["job_id"] == jobs[0]["job_id"]
        assert harness.receipt("verification")["decision"] == "fresh"

    def test_nonzero_exit_is_not_reused(self, tmp_path, monkeypatch) -> None:
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        harness.finish(first["job_id"], exit_code=1)

        result = harness.resume()

        # 非零 exit 不進 probe（只有 exited/0 才可能是來源），走既有失敗恢復。
        assert result["reason"] == "job-failed"
        assert harness.run().needs_human_reason["reason"] == "job-failed"
        assert harness.gate("verification") == "pending"
        assert harness.receipt("verification") == {
            "decision": "fresh",
            "receipt_schema_version": 2,
            "stage_execution_key": harness.registry.get_job(first["job_id"])[
                "workflow_stage_execution_key"
            ],
            "job_id": first["job_id"],
        }
        assert harness.registry.get_job(first["job_id"]).get("workflow_evidence") is None

    def test_rejected_review_verdict_is_adopted_as_rejected_not_passed(
        self, tmp_path, monkeypatch
    ) -> None:
        """未採信的 evidence：阻擋性 review verdict 經 reuse 也只能以 rejected 採信。"""

        harness = Harness(tmp_path, monkeypatch)
        harness.adopt_verification()
        review_job = harness.jobs("code-review")[-1]["job_id"]
        harness.finish(review_job, findings=[BLOCKING_FINDING])

        result = harness.resume()

        assert result["reason"] == "automatic-retry-build"
        assert harness.gate("code-review") == "pending"
        assert "needs_human" not in harness.run().facets
        assert harness.run().current_phase == "build"
        assert harness.run().auto_retry_history[-1]["reason"] == "blocking-findings"
        _assert_reused_receipt(
            harness.receipt("code-review"),
            harness=harness,
            source_job_id=review_job,
            adoption="rejected",
        )
        assert harness.jobs("adversarial-review") == []

    def test_periodic_tick_launches_retry_for_control_queue_blocking_review(
        self, tmp_path, monkeypatch
    ) -> None:
        """A daemon tick carries a control-queue workflow through review failure to a build launch."""

        # This scenario isolates the retry route; the synthetic repo has no live
        # WorkAuthority snapshot for the unrelated builder-todo admission check.
        monkeypatch.setattr(manager_daemon, "_builder_todo_admission_for_run", lambda _run: None)
        harness = _DaemonHarness(tmp_path, monkeypatch)
        first = harness.work()["result"]
        verification_job = first["job_id"]
        harness.finish(verification_job)
        second = harness.work()["result"]
        assert second["current_phase"] == "review"
        review_job = second["job_id"]
        harness.finish(review_job, findings=[BLOCKING_FINDING])

        launches_before_tick = list(harness.launches)
        retry_launcher = base._Launcher(
            "codex", "gpt-primary", on_launch=harness.launches.append
        )
        periodic = manager_daemon.build_periodic_tick_runner(
            dispatcher=harness.dispatcher,
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
            launcher=retry_launcher,
            workflow_ship_validator=object(),
            require_idle=False,
            workflow_identity_registry=harness.identities,
            scan_specs_fn=lambda _root: [],
            run_tick_fn=lambda *args, **kwargs: {"dispatch_skipped": False},
            auto_claim_fn=lambda: [],
        )

        periodic()

        retried = harness.run()
        build_jobs = harness.jobs("subagent-build")
        assert retried.current_phase == "build"
        assert retried.candidate_head == harness.candidate
        assert "needs_human" not in retried.facets
        assert retried.auto_retry_history[-1]["reason"] == "blocking-findings"
        assert len(build_jobs) == 2
        assert launches_before_tick[-1] != build_jobs[-1]["job_id"]
        assert harness.launches[-1] == build_jobs[-1]["job_id"]

    def test_revoked_review_evidence_is_not_reused(self, tmp_path, monkeypatch) -> None:
        """撤銷：retry-review reset 把舊 review job 標 failed，之後的 resume 不得
        再把它當可重用來源。"""

        harness = Harness(tmp_path, monkeypatch)
        harness.adopt_verification()
        review_job = harness.jobs("code-review")[-1]["job_id"]
        harness.finish(review_job)
        run = harness.run()
        harness.registry._manager_update_workflow_run(
            run.run_id,
            facets=("needs_human",),
            gate_status="failed",
            needs_human_reason=fixture_needs_human_reason(),
            verified_head=harness.candidate,
        )
        harness.registry._manager_reset_workflow_for_retry_review(
            run.run_id, expected_candidate=harness.candidate
        )
        assert harness.registry.get_job(review_job)["status"] == "failed"

        result = harness.resume()

        assert result["reason"] == "job-failed"
        assert result["job_id"] == review_job
        assert harness.gate("code-review") == "pending"
        assert "foreign-review" not in {ref.kind for ref in harness.run().gate_refs}
        assert harness.receipt("code-review")["decision"] == "fresh"
        assert harness.receipt("code-review")["job_id"] == review_job
        assert harness.jobs("adversarial-review") == []

    def test_review_of_previous_candidate_is_not_reused(self, tmp_path, monkeypatch) -> None:
        harness = Harness(tmp_path, monkeypatch)
        harness.adopt_verification()
        review_job = harness.jobs("code-review")[-1]["job_id"]
        harness.finish(review_job)
        (harness.workspace / "src.txt").write_text("next\n", encoding="utf-8")
        base._run_git(["add", "."], harness.workspace)
        base._run_git(["commit", "-qm", "next"], harness.workspace)
        new_candidate = base._run_git(["rev-parse", "HEAD"], harness.workspace).stdout.strip()
        harness.add_builder(new_candidate, suffix="-next")
        harness.registry._manager_update_workflow_run(
            harness.run_id, candidate_head=new_candidate, verified_head=new_candidate
        )

        harness.resume()

        jobs = harness.jobs("code-review")
        assert len(jobs) == 2 and jobs[-1]["subject_head"] == new_candidate
        assert harness.gate("code-review") == "pending"
        assert harness.registry.get_job(review_job).get("workflow_evidence") is None

    @pytest.mark.parametrize("damage", ["missing", "tampered", "symlink"])
    def test_damaged_evidence_is_rejected_without_reused_receipt(
        self, tmp_path, monkeypatch, damage: str
    ) -> None:
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        verify_job = first["job_id"]
        harness.finish(verify_job)
        manager.terminalize_workflow_job(
            harness.registry, job_id=verify_job, coordinator_root=harness.root
        )
        evidence = harness.evidence_path(verify_job)
        original = evidence.read_bytes()
        evidence.chmod(0o600)
        if damage == "missing":
            evidence.unlink()
        elif damage == "tampered":
            evidence.write_bytes(original.replace(b"verified", b"verifiex"))
        else:
            copy = tmp_path / "evidence-copy.json"
            copy.write_bytes(original)
            evidence.unlink()
            evidence.symlink_to(copy)

        with pytest.raises((OSError, ValueError)):
            harness.resume()

        assert harness.gate("verification") == "pending"
        # receipt 仍是派工當下那張 fresh：沒有任何 reused 被預寫或落地。
        assert harness.receipt("verification") == _fresh_receipt(harness, verify_job)
        assert harness.run().needs_human_reason["reason"] == "workflow-advance-failed"
        assert harness.jobs("code-review") == []

    def test_evidence_artifact_escaping_repo_is_rejected(self, tmp_path, monkeypatch) -> None:
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        verify_job = first["job_id"]
        harness.registry.update_headless_result(verify_job, status="exited", exit_code=0)
        job = harness.registry.get_job(verify_job)
        outside = tmp_path / "outside.md"
        outside.write_text("escape\n", encoding="utf-8")
        envelope = {
            "schema_version": 1,
            "kind": "verify",
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
                **(
                    {"input_snapshot": job["workflow_input_snapshot"]}
                    if job.get("workflow_input_snapshot")
                    else {}
                ),
            },
            "payload": verification.validate_verification_evidence(
                {
                    "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                    "slice_id": f"{job['workflow_run_id']}-verification",
                    "candidate": job["subject_head"],
                    "status": "verified",
                    "summary": "escape",
                    "details": {},
                }
            ),
            "artifacts": [
                {
                    "path": "../outside.md",
                    "sha256": _sha256(outside),
                    "baseline_sha256": None,
                }
            ],
        }
        content = (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode()
        target = harness.root / "evidence" / "workflow" / f"{verify_job}-escape.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        harness.registry.bind_workflow_evidence(
            verify_job,
            locator={
                "kind": "verify",
                "path": f"evidence/workflow/{verify_job}-escape.json",
                "hash": hashlib.sha256(content).hexdigest(),
            },
            subject_head=job["subject_head"],
        )

        with pytest.raises(ValueError, match="escapes repo"):
            harness.resume()

        assert harness.gate("verification") == "pending"
        assert harness.receipt("verification") == _fresh_receipt(harness, verify_job)
        assert harness.run().needs_human_reason["reason"] == "workflow-advance-failed"


# ===========================================================================
# S06：review 卡的 builder／identity／independence／authority
# ===========================================================================


class TestS06ReviewCard:
    def _review_done(self, tmp_path, monkeypatch, **kwargs):
        harness = Harness(tmp_path, monkeypatch, **kwargs)
        harness.adopt_verification()
        review_job = harness.jobs("code-review")[-1]["job_id"]
        harness.finish(review_job)
        return harness, review_job

    def test_review_reuse_records_source_and_continues(self, tmp_path, monkeypatch) -> None:
        harness, review_job = self._review_done(tmp_path, monkeypatch)
        launches_before = list(harness.launches)

        result = harness.resume()

        assert harness.gate("code-review") == "passed"
        assert [job["job_id"] for job in harness.jobs("code-review")] == [review_job]
        assert harness.launches == [*launches_before, result["job_id"]]
        assert harness.jobs("adversarial-review")[-1]["job_id"] == result["job_id"]
        _assert_reused_receipt(harness.receipt("code-review"), harness=harness, source_job_id=review_job)
        # foreign-review gate ref 指向的正是被重用的那份 evidence。
        foreign = {ref.kind: ref for ref in harness.run().gate_refs}["foreign-review"]
        assert foreign.sha256 == harness.receipt("code-review")["source_evidence_hash"]

    def test_review_builder_job_change_rejects_reuse(self, tmp_path, monkeypatch) -> None:
        harness, review_job = self._review_done(tmp_path, monkeypatch)
        retried = harness.add_builder(harness.candidate, suffix="-retry")

        harness.resume()

        jobs = harness.jobs("code-review")
        assert len(jobs) == 2 and jobs[-1]["workflow_builder_job_id"] == retried
        receipt = harness.receipt("code-review")
        assert receipt["decision"] == "fresh"
        assert receipt["mismatched_fields"] == ["builder_job_id"]
        assert harness.gate("code-review") == "pending"

    def test_review_reviewer_identity_change_rejects_reuse(self, tmp_path, monkeypatch) -> None:
        harness, review_job = self._review_done(tmp_path, monkeypatch)

        harness.resume(identities=base._identities(reviewer_model="sonnet-844-v2"))

        jobs = harness.jobs("code-review")
        assert len(jobs) == 2 and jobs[-1]["model_id"] == "sonnet-844-v2"
        assert "model" in harness.receipt("code-review")["mismatched_fields"]
        assert harness.gate("code-review") == "pending"

    def test_review_authority_change_rejects_reuse(self, tmp_path, monkeypatch) -> None:
        harness, review_job = self._review_done(tmp_path, monkeypatch)
        adjudications = harness.root / "evidence" / "operator-adjudication"
        adjudications.mkdir(parents=True, exist_ok=True)
        (adjudications / f"{harness.run_id}-adjudication-1.json").write_text(
            json.dumps(
                {
                    "run_id": harness.run_id,
                    "actor": "operator",
                    "card": "code-review",
                    "created_at": "2026-09-29T00:00:00+00:00",
                    "reason": "以 design 為準。",
                }
            ),
            encoding="utf-8",
        )

        harness.resume()

        assert len(harness.jobs("code-review")) == 2
        assert harness.receipt("code-review")["mismatched_fields"] == [
            "operator_adjudications_digest"
        ]

    def test_review_independence_violation_blocks_reuse(self, tmp_path, monkeypatch) -> None:
        harness, review_job = self._review_done(tmp_path, monkeypatch)
        run = harness.run()
        # build step 的 domain 變成與 reviewer 相同：舊 review 不再滿足 independence。
        harness.registry._manager_update_workflow_run(
            run.run_id,
            steps=tuple(
                replace(step, domain=base.REVIEWER_DOMAIN) if step.card == "subagent-build" else step
                for step in run.steps
            ),
        )

        # 沒有任何 foreign reviewer 可選：probe 無法判定相容性 → 強制新 attempt
        # 也派不出去 → 既有 fail-closed（needs_human），舊 review 絕不被採信。
        with pytest.raises(ValueError, match="no configured identity"):
            harness.resume()

        assert harness.gate("code-review") == "pending"
        assert [job["job_id"] for job in harness.jobs("code-review")] == [review_job]
        assert "foreign-review" not in {ref.kind for ref in harness.run().gate_refs}
        assert harness.run().needs_human_reason["reason"] == "workflow-card-dispatch-failed"
        receipt = harness.receipt("code-review")
        assert receipt["decision"] == "ineligible"
        assert receipt["reason"] == "probe-exception"
        assert receipt["superseded_key"] == harness.registry.get_job(review_job)[
            "workflow_stage_execution_key"
        ]
        assert harness.jobs("adversarial-review") == []


# ===========================================================================
# S07：決策與採信之間的 drift（TOCTOU）以採信點的 CAS 快照重新裁決
# ===========================================================================


def _inject_before_adoption(monkeypatch, mutate) -> None:
    """在 probe 已判定 reused 之後、advance 採信之前注入 drift：
    `terminalize_workflow_job` 正位於這兩點之間。"""

    original = manager.terminalize_workflow_job
    state = {"done": False}

    def wrapper(registry, *, job_id, coordinator_root):
        if not state["done"]:
            state["done"] = True
            mutate()
        return original(registry, job_id=job_id, coordinator_root=coordinator_root)

    monkeypatch.setattr(manager, "terminalize_workflow_job", wrapper)


class TestS07AdoptionDrift:
    def _ready(self, tmp_path, monkeypatch):
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        harness.finish(first["job_id"])
        return harness, first["job_id"]

    def _assert_redecided(self, harness, result, verify_job, field: str) -> None:
        assert result["reason"] == "stage-reuse-adoption-drift"
        assert field in result["mismatched_fields"]
        assert harness.gate("verification") == "pending"
        assert "needs_human" not in harness.run().facets
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "ineligible"
        assert receipt["reason"] == "adoption-drift"
        assert field in receipt["mismatched_fields"]
        assert receipt["superseded_key"] == harness.registry.get_job(verify_job)[
            "workflow_stage_execution_key"
        ]
        assert result["stage_reuse"] == {"card": "verification", **receipt}
        assert harness.jobs("code-review") == []

    def test_authority_drift_between_decision_and_adoption(self, tmp_path, monkeypatch) -> None:
        harness, verify_job = self._ready(tmp_path, monkeypatch)

        def add_adjudication():
            directory = harness.root / "evidence" / "operator-adjudication"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{harness.run_id}-adjudication-1.json").write_text(
                json.dumps(
                    {
                        "run_id": harness.run_id,
                        "actor": "operator",
                        "card": "verification",
                        "created_at": "2026-09-29T00:00:00+00:00",
                        "reason": "drift",
                    }
                ),
                encoding="utf-8",
            )

        _inject_before_adoption(monkeypatch, add_adjudication)
        result = harness.resume()
        self._assert_redecided(harness, result, verify_job, "operator_adjudications_digest")

        # 下一次接續以新狀態重新裁決：不相容 → 新 attempt。
        after = harness.resume()
        assert len(harness.jobs("verification")) == 2
        assert after["job_id"] == harness.jobs("verification")[-1]["job_id"]
        assert harness.receipt("verification")["decision"] == "fresh"

    def test_candidate_drift_between_decision_and_adoption(self, tmp_path, monkeypatch) -> None:
        harness, verify_job = self._ready(tmp_path, monkeypatch)

        def move_candidate():
            (harness.workspace / "src.txt").write_text("drift\n", encoding="utf-8")
            base._run_git(["add", "."], harness.workspace)
            base._run_git(["commit", "-qm", "drift"], harness.workspace)
            head = base._run_git(["rev-parse", "HEAD"], harness.workspace).stdout.strip()
            harness.add_builder(head, suffix="-drift")
            harness.registry._manager_update_workflow_run(harness.run_id, candidate_head=head)

        _inject_before_adoption(monkeypatch, move_candidate)
        result = harness.resume()
        self._assert_redecided(harness, result, verify_job, "candidate_sha")

    def test_test_policy_drift_between_decision_and_adoption(self, tmp_path, monkeypatch) -> None:
        harness, verify_job = self._ready(tmp_path, monkeypatch)

        def change_policy():
            run = harness.run()
            harness.registry._manager_update_workflow_run(
                run.run_id,
                steps=tuple(
                    replace(step, test_policy="focused") if step.card == "verification" else step
                    for step in run.steps
                ),
            )

        _inject_before_adoption(monkeypatch, change_policy)
        result = harness.resume()
        self._assert_redecided(harness, result, verify_job, "test_policy")


# ===========================================================================
# S08：crash／restart、重送與雙 request、延遲 terminal
# ===========================================================================


def _patch_adoption_write(monkeypatch, *, crash: str) -> dict:
    """在 Manager 寫入採信（gate＋reused receipt 同一次 durable 更新）之前或之後
    模擬 crash。"""

    original = JobRegistry._manager_update_workflow_run
    state = {"crashed": False}

    def wrapper(self, run_id, **kwargs):
        receipts = kwargs.get("stage_reuse_receipts") or {}
        adopting = any(
            isinstance(row, dict) and row.get("decision") == "reused" and "adoption" in row
            for row in receipts.values()
        )
        if adopting and not state["crashed"] and crash == "before-write":
            state["crashed"] = True
            raise _Crash("crash before durable adoption")
        result = original(self, run_id, **kwargs)
        if adopting and not state["crashed"] and crash == "after-write":
            state["crashed"] = True
            raise _Crash("crash after durable adoption")
        return result

    monkeypatch.setattr(JobRegistry, "_manager_update_workflow_run", wrapper)
    return state


class TestS08CrashAndReplay:
    def _ready(self, tmp_path, monkeypatch):
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        harness.finish(first["job_id"])
        return harness, first["job_id"]

    def _assert_single_continuation(self, harness: Harness, verify_job: str, evidence_bytes: bytes) -> None:
        assert [job["job_id"] for job in harness.jobs("verification")] == [verify_job]
        assert len(harness.jobs("code-review")) == 1
        assert harness.gate("verification") == "passed"
        assert harness.launches == [verify_job, harness.jobs("code-review")[0]["job_id"]]
        assert harness.evidence_path(verify_job).read_bytes() == evidence_bytes
        _assert_reused_receipt(harness.receipt("verification"), harness=harness, source_job_id=verify_job)

    def _restart(self, harness: Harness) -> None:
        harness.registry = JobRegistry(state_path=harness.root / "jobs.json")
        harness.dispatcher = base._Dispatcher(harness.registry)

    def test_crash_before_durable_adoption_then_restart(self, tmp_path, monkeypatch) -> None:
        harness, verify_job = self._ready(tmp_path, monkeypatch)
        state = _patch_adoption_write(monkeypatch, crash="before-write")
        with pytest.raises(_Crash):
            harness.resume()
        assert state["crashed"]
        self._restart(harness)
        assert harness.gate("verification") == "pending"
        assert harness.receipt("verification") == _fresh_receipt(harness, verify_job)
        assert "needs_human" not in harness.run().facets
        evidence_bytes = harness.evidence_path(verify_job).read_bytes()

        harness.resume()
        harness.resume()  # 重送

        self._assert_single_continuation(harness, verify_job, evidence_bytes)

    def test_crash_after_durable_adoption_before_next_dispatch(self, tmp_path, monkeypatch) -> None:
        harness, verify_job = self._ready(tmp_path, monkeypatch)
        _patch_adoption_write(monkeypatch, crash="after-write")
        with pytest.raises(_Crash):
            harness.resume()
        self._restart(harness)
        assert harness.gate("verification") == "passed"
        assert harness.jobs("code-review") == []
        evidence_bytes = harness.evidence_path(verify_job).read_bytes()
        receipt_before = dict(harness.receipt("verification"))

        harness.resume()
        harness.resume()

        self._assert_single_continuation(harness, verify_job, evidence_bytes)
        assert harness.receipt("verification") == receipt_before

    def test_crash_after_next_dispatch_before_response(self, tmp_path, monkeypatch) -> None:
        harness, verify_job = self._ready(tmp_path, monkeypatch)
        original = manager.classify_dispatch_result

        def crash_on_final_classification(result, **kwargs):
            if "before_phase" in kwargs:
                raise _Crash("crash before response")
            return original(result, **kwargs)

        monkeypatch.setattr(manager, "classify_dispatch_result", crash_on_final_classification)
        with pytest.raises(_Crash):
            harness.resume()
        monkeypatch.setattr(manager, "classify_dispatch_result", original)
        self._restart(harness)
        evidence_bytes = harness.evidence_path(verify_job).read_bytes()

        again = harness.resume()

        assert again["reason"] == "in-flight"
        self._assert_single_continuation(harness, verify_job, evidence_bytes)

    def test_public_resend_after_adoption_is_single_continuation(
        self, tmp_path, monkeypatch
    ) -> None:
        harness = _DaemonHarness(tmp_path, monkeypatch)
        verify_job = harness.work()["result"]["job_id"]
        harness.finish(verify_job)
        first = harness.work()["result"]
        evidence_bytes = harness.evidence_path(verify_job).read_bytes()

        second = harness.work()["result"]

        assert second["job_id"] == first["job_id"]
        assert second["reason"] == "in-flight"
        self._assert_single_continuation(harness, verify_job, evidence_bytes)

    def test_interleaved_duplicate_request_does_not_double_adopt_or_stop_run(
        self, tmp_path, monkeypatch
    ) -> None:
        harness, verify_job = self._ready(tmp_path, monkeypatch)
        other_registry = JobRegistry(state_path=harness.root / "jobs.json")
        original = manager.terminalize_workflow_job
        state = {"interleaved": False}

        def interleave(registry, *, job_id, coordinator_root):
            if not state["interleaved"]:
                state["interleaved"] = True
                # 第二個 request（另一個 registry 實例）在第一個 request 決策後、
                # 採信前完成整段接續。
                harness.resume(registry=other_registry)
            return original(registry, job_id=job_id, coordinator_root=coordinator_root)

        monkeypatch.setattr(manager, "terminalize_workflow_job", interleave)

        loser = harness.resume()

        assert loser["reason"] == "duplicate-continuation"
        assert "needs_human" not in harness.run().facets
        evidence_bytes = harness.evidence_path(verify_job).read_bytes()
        self._assert_single_continuation(harness, verify_job, evidence_bytes)

    def test_late_terminal_of_superseded_attempt_does_not_override_accepted_evidence(
        self, tmp_path, monkeypatch
    ) -> None:
        harness = Harness(tmp_path, monkeypatch)
        stale = harness.resume()["job_id"]
        # 舊 attempt 已結束但尚未 harvest；model 換掉 → 新 attempt。
        harness.registry.update_headless_result(stale, status="exited", exit_code=0)
        new_identities = base._identities(reviewer_model="sonnet-844-v2")
        fresh = harness.resume(identities=new_identities)["job_id"]
        harness.finish(fresh)
        harness.resume(identities=new_identities)
        accepted = harness.run()
        accepted_receipt = dict(harness.receipt("verification"))
        fresh_evidence = harness.evidence_path(fresh).read_bytes()
        review_jobs = [job["job_id"] for job in harness.jobs("code-review")]

        # 舊 attempt 的 terminal 延遲抵達：production harvest 以 report baseline
        # CAS 拒絕它覆寫已採信 attempt 發佈的產物，之後的接續也不回頭碰它。
        harness.finish(stale)
        with pytest.raises(ValueError, match="baseline CAS conflict"):
            manager.terminalize_workflow_job(
                harness.registry, job_id=stale, coordinator_root=harness.root
            )
        assert harness.registry.get_job(stale).get("workflow_evidence") is None
        harness.resume(identities=new_identities)

        after = harness.run()
        assert after.verified_head == accepted.verified_head
        assert harness.gate("verification") == "passed"
        assert harness.receipt("verification") == accepted_receipt
        assert accepted_receipt["source_job_id"] == fresh
        assert harness.evidence_path(fresh).read_bytes() == fresh_evidence
        assert [job["job_id"] for job in harness.jobs("code-review")] == review_jobs


# ===========================================================================
# S09：retry-card／active attempt／authority restart 的失效原因
# ===========================================================================


class TestS09RetryAndInvalidation:
    def test_public_retry_card_forces_new_attempt_despite_matching_key(
        self, tmp_path, monkeypatch
    ) -> None:
        harness = _DaemonHarness(tmp_path, monkeypatch)
        old_job = harness.work()["result"]["job_id"]
        # 舊 attempt 成功結束但尚未採信；operator 明確要求新 attempt。
        harness.finish(old_job)
        run = harness.run()
        harness.registry._manager_update_workflow_run(
            run.run_id,
            facets=("needs_human",),
            gate_status="failed",
            needs_human_reason=fixture_needs_human_reason(),
        )

        result = harness.work(
            "retry-card", card="verification", expected_run_id=run.run_id, issue=844
        )["result"]

        jobs = harness.jobs("verification")
        assert len(jobs) == 2
        assert result["job_id"] == jobs[-1]["job_id"] != old_job
        assert jobs[-1]["workflow_stage_execution_key"] == jobs[0]["workflow_stage_execution_key"]
        assert harness.launches == [old_job, jobs[-1]["job_id"]]
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "fresh" and receipt["job_id"] == jobs[-1]["job_id"]

    def test_active_attempt_is_polled_not_duplicated_on_lookup_miss(
        self, tmp_path, monkeypatch
    ) -> None:
        harness = Harness(tmp_path, monkeypatch)
        active = harness.resume()["job_id"]
        # key 輸入改變（lookup 必然 miss），但 attempt 仍在跑。
        directory = harness.root / "evidence" / "operator-adjudication"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{harness.run_id}-adjudication-1.json").write_text(
            json.dumps(
                {
                    "run_id": harness.run_id,
                    "actor": "operator",
                    "card": "verification",
                    "created_at": "2026-09-29T00:00:00+00:00",
                    "reason": "lookup miss",
                }
            ),
            encoding="utf-8",
        )

        result = harness.resume()

        assert result == {
            "run_id": harness.run_id,
            "current_phase": "verify",
            "job_id": active,
            "reason": "in-flight",
        }
        assert [job["job_id"] for job in harness.jobs("verification")] == [active]
        assert harness.launches == [active]

    def test_authority_restart_lists_invalidated_stages_in_action_and_receipts(
        self, tmp_path
    ) -> None:
        """#844 2026-09-20 留言：一般 resume 讓同 candidate 已接受的 gate 回
        pending 時，action 與 status 必須列出原因、變更的 authority 與重跑範圍。"""

        old_authority, _ = retry_invalidation._authority(
            tmp_path, source_revisions=["issue:12@open", "openspec:demo@1"]
        )
        registry = JobRegistry(state_path=tmp_path / "jobs.json")
        old_claim_key = work_actions._expected_claim_key(old_authority)
        run = retry_invalidation._make_run(
            registry,
            authority=old_authority,
            claim_key=old_claim_key,
            current_phase="review",
            steps=retry_invalidation._base_steps(verify_result="passed", review_result="passed"),
            candidate_head=retry_invalidation.HEAD,
            verified_head=retry_invalidation.HEAD,
        )
        verify_key, review_key = "a" * 64, "b" * 64
        registry._manager_update_workflow_run(
            run.run_id,
            stage_reuse_receipts={
                "reviewer-verify": {"decision": "reused", "stage_execution_key": verify_key},
                "reviewer-review": {"decision": "fresh", "stage_execution_key": review_key},
            },
        )
        new_snapshot = retry_invalidation._snapshot(
            tmp_path / "snapshot.json", source_revisions=["issue:12@updated", "openspec:demo@1"]
        )
        new_authority = work_actions.load_work_authority(
            repo="acme/demo", work_id="demo", snapshot_path=new_snapshot
        )

        result = work_actions.execute_work_action(
            args={"action": "resume", "repo": "acme/demo", "work_id": "demo", "issue": 12},
            requested_by="operator",
            snapshot_path=new_snapshot,
            state_path=tmp_path / "runs.json",
            workflow_registry=registry,
        )["result"]

        invalidation = result["stage_invalidation"]
        assert invalidation["reason"] == "authority-restart"
        assert invalidation["previous_claim_key"] == old_claim_key
        assert invalidation["claim_key"] == work_actions._expected_claim_key(new_authority)
        assert invalidation["previous_source_revision"] == work_actions.work_authority_digest(
            old_authority
        )
        assert invalidation["source_revision"] == work_actions.work_authority_digest(new_authority)
        assert invalidation["candidate_head"] == retry_invalidation.HEAD
        assert invalidation["rerun_cards"] == ["reviewer-verify", "reviewer-review"]
        assert invalidation["preserved_cards"] == ["subagent-build"]
        receipts = result["run"]["stage_reuse_receipts"]
        assert receipts["reviewer-verify"] == {
            "decision": "ineligible",
            "receipt_schema_version": 2,
            "reason": "authority-restart",
            "superseded_key": verify_key,
        }
        assert receipts["reviewer-review"]["superseded_key"] == review_key
        # build candidate 保留（既有 #216 回歸不倒退）。
        assert result["run"]["candidate_head"] == retry_invalidation.HEAD
        by_card = {step["card"]: step["gate_result"] for step in result["run"]["steps"]}
        assert by_card["subagent-build"] == "passed"


# ===========================================================================
# S10：cohort 外、舊 schema、ineligible 無 job
# ===========================================================================


class TestS10Compatibility:
    def test_build_card_is_outside_cohort_even_with_a_key(self, tmp_path) -> None:
        registry, run, _candidate = base._build_verify_run(tmp_path)
        build_step = next(step for step in run.steps if step.card == "subagent-build")
        kind, context = manager._workflow_stage_reuse_probe(
            run=run,
            step=build_step,
            jobs=[{"status": "exited", "exit_code": 0, "workflow_stage_execution_key": "d" * 64}],
            identities=base._identities(),
            launcher_factory=lambda identity: None,
            coordinator_root=tmp_path / "coordinator",
            registry=registry,
        )
        assert (kind, context) == ("legacy", None)
        identity = base._identities().require("codex", "gpt-primary")
        assert (
            manager._workflow_stage_execution_context(
                run=run,
                step=build_step,
                identity=identity,
                profile_binding=types.SimpleNamespace(resolved_key="epk:v1:resolved:" + "7" * 64),
                builder_job_id=None,
                manager_gate_ledger=None,
                operator_adjudications=None,
            )
            is None
        )

    def test_old_schema_receipt_is_readable_and_forces_fresh_attempt(
        self, tmp_path, monkeypatch
    ) -> None:
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        harness.finish(first["job_id"])
        state_path = harness.root / "jobs.json"
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        rows = payload["jobs"] if "jobs" in payload else payload["legacy_records"]["jobs"]
        row = next(item for item in rows if item["job_id"] == first["job_id"])
        receipt = row["workflow_stage_execution_receipt"]
        for field in ("builder_job_id", "manager_gate_ledger_digest", "operator_adjudications_digest"):
            receipt.pop(field)
        receipt["schema_version"] = 2
        state_path.write_text(json.dumps(payload), encoding="utf-8")

        restarted = JobRegistry(state_path=state_path)
        assert restarted.get_job(first["job_id"])["workflow_stage_execution_receipt"]["schema_version"] == 2
        harness.registry = restarted

        harness.resume()

        assert len(harness.jobs("verification")) == 2
        receipt = harness.receipt("verification")
        assert receipt["decision"] == "fresh"
        assert "schema_version" in receipt["mismatched_fields"]

    def test_ineligible_probe_whose_forced_dispatch_yields_no_job_records_reason(
        self, tmp_path, monkeypatch
    ) -> None:
        harness = Harness(tmp_path, monkeypatch)
        first = harness.resume()
        harness.finish(first["job_id"])
        stored_key = harness.registry.get_job(first["job_id"])["workflow_stage_execution_key"]

        result = harness.resume(
            launcher_factory=lambda identity: None,
            quota_admission_context=quota_admission.QuotaConfigInvalid("fixture-invalid"),
        )

        assert "job_id" not in result or result.get("job_id") is None
        assert len(harness.jobs("verification")) == 1
        receipt = harness.receipt("verification")
        assert receipt == {
            "decision": "ineligible",
            "receipt_schema_version": 2,
            "reason": "launcher-unavailable",
            "superseded_key": stored_key,
        }
        assert harness.gate("verification") == "pending"


# ===========================================================================
# S11：呈現與相容
# ===========================================================================


class TestS11Presentation:
    def test_work_show_projects_stage_reuse_receipts(self, tmp_path, monkeypatch) -> None:
        from paulsha_cortex.cli import _format_stage_reuse
        from paulsha_cortex.monitor.providers import WorkflowRegistryProvider

        harness = Harness(tmp_path, monkeypatch)
        verify_job = harness.adopt_verification()

        observed = WorkflowRegistryProvider(
            base.REPO, state_path=harness.root / "jobs.json"
        ).scan().observations["stage_reuse"][WORK_ID]

        assert observed["run_id"] == harness.run_id
        _assert_reused_receipt(observed["cards"]["verification"], harness=harness, source_job_id=verify_job)
        assert observed["cards"]["code-review"]["decision"] == "fresh"
        text = "\n".join(_format_stage_reuse(observed))
        assert f"stage_reuse[verification]: decision=reused source_job={verify_job}" in text
        assert f"source_run={harness.run_id}" in text
        assert f"evidence_sha256={observed['cards']['verification']['source_evidence_hash']}" in text
        assert "stage_reuse[code-review]: decision=fresh" in text

    def test_monitor_get_work_item_carries_stage_reuse_for_exact_repo(
        self, tmp_path, monkeypatch
    ) -> None:
        import test_monitor_work_api as monitor_fixtures
        from paulsha_cortex.monitor.providers import WorkflowRegistryProvider
        from paulsha_cortex.monitor.work_api import WorkReadModelStore

        harness = Harness(tmp_path, monkeypatch)
        verify_job = harness.adopt_verification()
        provider = WorkflowRegistryProvider(base.REPO, state_path=harness.root / "jobs.json").scan()
        store = WorkReadModelStore(
            monitor_fixtures._snapshot(
                monitor_fixtures._item(WORK_ID, "ongoing", repo=base.REPO),
                monitor_fixtures._item(WORK_ID, "ongoing", repo="example/other"),
                providers={provider.provider_id: provider},
            )
        )

        envelope = store.get_work_item(WORK_ID, repo=base.REPO)

        assert envelope["stage_reuse"]["run_id"] == harness.run_id
        _assert_reused_receipt(
            envelope["stage_reuse"]["cards"]["verification"],
            harness=harness,
            source_job_id=verify_job,
        )
        assert "stage_reuse" not in store.get_work_item(WORK_ID, repo="example/other")

    def test_legacy_reused_receipt_without_source_stays_readable(self) -> None:
        run_payload = _minimal_run_payload(
            {"verification": {"decision": "reused", "stage_execution_key": "a" * 64}}
        )
        run = WorkflowRun.from_dict(run_payload)
        assert run.stage_reuse_receipts["verification"]["decision"] == "reused"

    def test_versioned_reused_receipt_requires_source_linkage(self) -> None:
        receipt = {
            "decision": "reused",
            "receipt_schema_version": 2,
            "stage_execution_key": "a" * 64,
        }
        with pytest.raises(ValueError, match="缺 source_run_id"):
            WorkflowRun.from_dict(_minimal_run_payload({"verification": receipt}))
        with pytest.raises(ValueError, match="source_evidence_hash"):
            WorkflowRun.from_dict(
                _minimal_run_payload(
                    {
                        "verification": {
                            **receipt,
                            "source_run_id": "run",
                            "source_job_id": "job",
                            "source_evidence_hash": "not-hex",
                            "adoption": "accepted",
                        }
                    }
                )
            )

    def test_deployed_runtime_reads_new_receipt_fields(self, tmp_path, monkeypatch) -> None:
        """rollback：目前部署的 runtime pin（#1129 merge，已含 #844 第一版）讀新
        receipt 欄位不 fail closed、也不丟整個欄位。"""

        harness = Harness(tmp_path, monkeypatch)
        harness.adopt_verification()
        payload = harness.run().to_dict()
        module = _load_workflow_module_at(_DEPLOYED_RUNTIME_PIN)
        old_run = module.WorkflowRun.from_dict(payload)
        assert old_run.stage_reuse_receipts["verification"]["source_job_id"] == payload[
            "stage_reuse_receipts"
        ]["verification"]["source_job_id"]
        assert old_run.to_dict()["stage_reuse_receipts"] == payload["stage_reuse_receipts"]

    def test_completion_reused_from_does_not_bypass_reference_validation(self, tmp_path) -> None:
        evidence_path = tmp_path / "verification.json"
        evidence = verification.validate_verification_evidence(
            {
                "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
                "slice_id": "stage-reuse-844",
                "candidate": "b" * 40,
                "status": "verified",
                "summary": "ok",
                "details": {},
            }
        )
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        record = {
            "schema_version": completion.COMPLETION_SCHEMA_VERSION,
            "slice_id": "stage-reuse-844",
            "spec_hash": "1" * 64,
            "plan_hash": "2" * 64,
            "verification_hash": "3" * 64,
            "builder_job_id": "builder-1",
            "reviewer_job_id": None,
            "dispatch_base": "a" * 40,
            "candidate": "b" * 40,
            "target_branch": "main",
            "target_remote": "origin",
            "target_ref": "refs/remotes/origin/main",
            "target_ref_sha": "c" * 40,
            "verification_evidence_path": str(evidence_path),
            "verification_evidence_hash": verification.canonical_json_hash(evidence),
            "review_policy": "not-required",
            "docs_class": "trivial",
            "review_evaluation_path": None,
            "review_evaluation_hash": None,
            "completed_at": "2026-09-29T00:00:00+00:00",
            "reused_from": {"run_id": "run-1", "job_id": "job-1", "evidence_hash": "5" * 64},
        }
        record_path = tmp_path / "record.json"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        assert completion.read_completion_record(record_path)["reused_from"]["job_id"] == "job-1"

        evidence_path.write_text(json.dumps({**evidence, "summary": "tampered"}), encoding="utf-8")
        with pytest.raises(ValueError, match="hash mismatch"):
            completion.read_completion_record(record_path)


def _minimal_run_payload(receipts: dict) -> dict:
    return {
        "run_id": "workflow-844",
        "work_id": WORK_ID,
        "repo": base.REPO,
        "claim_key": CLAIM_KEY,
        "source_revision": SOURCE_REVISION,
        "workspace_root": "/tmp/workspace",
        "combo": "feature-oneshot",
        "current_phase": "verify",
        "status": "ongoing",
        "steps": [step.to_dict() for step in _steps()],
        "issue_refs": [],
        "openspec_refs": [],
        "pr_refs": [],
        "attempts": {},
        "evidence_refs": [],
        "facets": [],
        "gate_status": "running",
        "created_at": "2026-09-29T00:00:00+00:00",
        "updated_at": "2026-09-29T00:00:00+00:00",
        "stage_reuse_receipts": receipts,
    }


#: 目前部署中的 runtime pin（PR #1129 merge，已含 #844 第一版的
#: `stage_reuse_receipts` 驗證）。固定完整 SHA；CI 的 tests job 以
#: `fetch-depth: 0` checkout，必定可解析（比照 `_ROLLBACK_BASE_REF`）。
_DEPLOYED_RUNTIME_PIN = "18ce7430f693c67fd88c5e6d72630ba3b35fec2e"


def _load_workflow_module_at(ref: str) -> types.ModuleType:
    repo_root = Path(__file__).resolve().parents[1]
    source = subprocess.run(
        ["git", "show", f"{ref}:paulsha_cortex/coordinator/workflow.py"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    name = f"paulsha_cortex.coordinator._workflow_snapshot_{ref[:12]}"
    module = types.ModuleType(name)
    module.__dict__.update(
        {"__package__": "paulsha_cortex.coordinator", "__name__": name, "__file__": f"{ref}:workflow.py"}
    )
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
