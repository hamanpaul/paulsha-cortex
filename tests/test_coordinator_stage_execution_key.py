"""#214：stage 級 content-addressed StageExecutionKey（建立在既有 phase 級

checkpoint 之上，把顆粒度從 phase 降到 stage）。

驗收條件對應：
- StageExecutionKey 涵蓋 repo/work_id/card/phase/executor/model/base_sha/
  candidate_sha/frozen_input_hashes/action/test_policy
- 相同 key 第二次執行 reuse evidence，model invocation count 不增加
- authority／candidate／model 任一變更才精準 invalidate
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paulsha_cortex.coordinator.registry import (
    STAGE_EXECUTION_KEY_SCHEMA_VERSION,
    JobRegistry,
    compute_stage_execution_key,
    describe_stage_execution_mismatch,
    stage_execution_receipt,
)


def _key_kwargs(**overrides: object) -> dict:
    base = {
        "repo": "hamanpaul/paulsha-cortex",
        "work_id": "214-stage-execution-key",
        "run_id": "workflow-214-run",
        "claim_key": "claim:v1:" + "9" * 64,
        "card": "build",
        "phase": "build",
        "executor": "codex",
        "model": "gpt-5",
        "base_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "frozen_input_hashes": ("1" * 64, "2" * 64),
        "action": "propose-diff",
        "test_policy": "focused",
        "execution_profile_key": "epk:v1:resolved:" + "7" * 64,
    }
    base.update(overrides)
    return base


class TestComputeStageExecutionKey:
    def test_deterministic_for_identical_inputs(self) -> None:
        first = compute_stage_execution_key(**_key_kwargs())
        second = compute_stage_execution_key(**_key_kwargs())
        assert first == second
        assert len(first) == 64
        assert all(char in "0123456789abcdef" for char in first)

    def test_frozen_input_hashes_order_independent(self) -> None:
        forward = compute_stage_execution_key(
            **_key_kwargs(frozen_input_hashes=("1" * 64, "2" * 64))
        )
        backward = compute_stage_execution_key(
            **_key_kwargs(frozen_input_hashes=("2" * 64, "1" * 64))
        )
        assert forward == backward

    @pytest.mark.parametrize(
        "field,changed",
        [
            ("repo", "other/repo"),
            ("work_id", "different-work"),
            ("run_id", "workflow-other-run"),
            ("claim_key", "claim:v1:" + "8" * 64),
            ("card", "review"),
            ("phase", "verify"),
            ("executor", "claude"),
            ("model", "different-model"),
            ("base_sha", "c" * 40),
            ("candidate_sha", "d" * 40),
            ("action", "commit-diff"),
            ("test_policy", "full"),
            ("execution_profile_key", "epk:v1:resolved:" + "6" * 64),
        ],
    )
    def test_any_covered_field_change_invalidates_key(self, field: str, changed: str) -> None:
        baseline = compute_stage_execution_key(**_key_kwargs())
        mutated = compute_stage_execution_key(**_key_kwargs(**{field: changed}))
        assert baseline != mutated

    def test_frozen_input_hashes_change_invalidates_key(self) -> None:
        baseline = compute_stage_execution_key(**_key_kwargs())
        mutated = compute_stage_execution_key(
            **_key_kwargs(frozen_input_hashes=("1" * 64, "3" * 64))
        )
        assert baseline != mutated

    @pytest.mark.parametrize(
        "field",
        [
            "repo", "work_id", "run_id", "claim_key", "card", "phase",
            "executor", "model", "base_sha", "candidate_sha", "action",
            "test_policy", "execution_profile_key",
        ],
    )
    def test_rejects_empty_string_field(self, field: str) -> None:
        with pytest.raises(ValueError):
            compute_stage_execution_key(**_key_kwargs(**{field: ""}))

    def test_rejects_empty_frozen_input_hash_entry(self) -> None:
        with pytest.raises(ValueError):
            compute_stage_execution_key(**_key_kwargs(frozen_input_hashes=("1" * 64, "")))


class TestStageExecutionReceipt:
    """#844 S02／S03／S04：逐欄快照＋itemized 不相容診斷。"""

    def test_receipt_key_matches_compute_stage_execution_key(self) -> None:
        receipt = stage_execution_receipt(**_key_kwargs())
        assert receipt["key"] == compute_stage_execution_key(**_key_kwargs())
        assert receipt["schema_version"] == STAGE_EXECUTION_KEY_SCHEMA_VERSION
        assert receipt["frozen_input_hashes"] == sorted(_key_kwargs()["frozen_input_hashes"])

    def test_describe_mismatch_empty_for_identical_receipts(self) -> None:
        current = stage_execution_receipt(**_key_kwargs())
        stored = stage_execution_receipt(**_key_kwargs())
        assert describe_stage_execution_mismatch(current, stored) == ()

    @pytest.mark.parametrize(
        "field,changed",
        [
            ("run_id", "workflow-other-run"),
            ("claim_key", "claim:v1:" + "8" * 64),
            ("model", "a-different-model"),
            ("execution_profile_key", "epk:v1:resolved:" + "6" * 64),
        ],
    )
    def test_describe_mismatch_names_the_changed_field(self, field: str, changed: str) -> None:
        current = stage_execution_receipt(**_key_kwargs())
        stored = stage_execution_receipt(**_key_kwargs(**{field: changed}))
        assert field in describe_stage_execution_mismatch(current, stored)

    def test_describe_mismatch_ignores_frozen_input_hash_order(self) -> None:
        current = stage_execution_receipt(
            **_key_kwargs(frozen_input_hashes=("1" * 64, "2" * 64))
        )
        stored = stage_execution_receipt(
            **_key_kwargs(frozen_input_hashes=("2" * 64, "1" * 64))
        )
        assert describe_stage_execution_mismatch(current, stored) == ()

    def test_describe_mismatch_detects_frozen_input_hash_change(self) -> None:
        current = stage_execution_receipt(**_key_kwargs())
        stored = stage_execution_receipt(
            **_key_kwargs(frozen_input_hashes=("1" * 64, "3" * 64))
        )
        assert "frozen_input_hashes" in describe_stage_execution_mismatch(current, stored)

    def test_missing_receipt_field_counts_as_mismatch(self) -> None:
        current = stage_execution_receipt(**_key_kwargs())
        stored = dict(current)
        del stored["execution_profile_key"]
        assert "execution_profile_key" in describe_stage_execution_mismatch(current, stored)


class TestStageExecutionKeyRunAndEraBinding:
    """#844：run_id／claim_key 烤進雜湊，跨 run／跨 claim-era 在雜湊層即不可能相同。"""

    def test_different_run_id_never_collides(self) -> None:
        same_run = compute_stage_execution_key(**_key_kwargs())
        other_run = compute_stage_execution_key(**_key_kwargs(run_id="workflow-different-run"))
        assert same_run != other_run

    def test_different_claim_key_never_collides(self) -> None:
        same_era = compute_stage_execution_key(**_key_kwargs())
        other_era = compute_stage_execution_key(
            **_key_kwargs(claim_key="claim:v1:" + "5" * 64)
        )
        assert same_era != other_era


def _make_registry(tmp_path: Path) -> JobRegistry:
    return JobRegistry(state_path=tmp_path / "registry.json")


def _create_terminal_job_with_evidence(
    registry: JobRegistry,
    *,
    task: str,
    stage_execution_key: str,
    evidence_hash: str = "e" * 64,
) -> dict:
    job = registry.create_job(
        task=task,
        persona="review",
        branch=f"feature/{task}",
        pane="pane-1",
        worktree=f"/tmp/{task}",
        workflow_run_id=f"run-{task}",
        workflow_stage_execution_key=stage_execution_key,
    )
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    bound = registry.bind_workflow_evidence(
        job["job_id"],
        locator={"kind": "gate", "path": f"evidence/workflow/{task}.json", "hash": evidence_hash},
    )
    return bound


class TestFindReusableStageEvidence:
    def test_returns_none_for_malformed_key(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        assert registry.find_reusable_stage_evidence("not-a-hex-key") is None

    def test_fail_closed_without_validator_even_with_matching_job(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        _create_terminal_job_with_evidence(registry, task="job-a", stage_execution_key=key)
        # 沒帶 is_evidence_still_valid callback：無法確認 evidence 是否仍然
        # 有效，fail-closed 必須回 None，不能自作主張判定可以 reuse。
        assert registry.find_reusable_stage_evidence(key) is None

    def test_returns_none_when_no_job_matches_key(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        other_key = compute_stage_execution_key(**_key_kwargs(model="other-model"))
        _create_terminal_job_with_evidence(registry, task="job-a", stage_execution_key=other_key)
        assert registry.find_reusable_stage_evidence(key, is_evidence_still_valid=lambda _e: True) is None

    def test_returns_none_when_job_did_not_exit_successfully(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        job = registry.create_job(
            task="job-failed",
            persona="review",
            branch="feature/job-failed",
            pane="pane-1",
            worktree="/tmp/job-failed",
            workflow_stage_execution_key=key,
        )
        registry.update_headless_result(job["job_id"], status="failed", exit_code=1)
        assert registry.find_reusable_stage_evidence(key, is_evidence_still_valid=lambda _e: True) is None

    def test_returns_none_when_evidence_not_bound(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        job = registry.create_job(
            task="job-no-evidence",
            persona="review",
            branch="feature/job-no-evidence",
            pane="pane-1",
            worktree="/tmp/job-no-evidence",
            workflow_stage_execution_key=key,
        )
        registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
        assert registry.find_reusable_stage_evidence(key, is_evidence_still_valid=lambda _e: True) is None

    def test_returns_none_when_validator_rejects_evidence(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        _create_terminal_job_with_evidence(registry, task="job-a", stage_execution_key=key)
        assert registry.find_reusable_stage_evidence(key, is_evidence_still_valid=lambda _e: False) is None

    def test_returns_none_when_validator_raises(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        _create_terminal_job_with_evidence(registry, task="job-a", stage_execution_key=key)

        def _boom(_evidence: dict) -> bool:
            raise RuntimeError("evidence store unavailable")

        assert registry.find_reusable_stage_evidence(key, is_evidence_still_valid=_boom) is None

    def test_reuses_evidence_for_identical_key_second_execution(self, tmp_path: Path) -> None:
        """AC2：相同 key 第二次執行時，不需要建立第二個 job（不增加 model
        invocation count）就能取得可重用的 evidence locator。"""
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        first_job = _create_terminal_job_with_evidence(
            registry, task="job-first", stage_execution_key=key, evidence_hash="f" * 64
        )
        jobs_before = registry.list_jobs()
        reused = registry.find_reusable_stage_evidence(key, is_evidence_still_valid=lambda _e: True)
        jobs_after = registry.list_jobs()

        assert reused == {
            "run_id": "run-job-first",
            "job_id": first_job["job_id"],
            "evidence": {"kind": "gate", "path": "evidence/workflow/job-first.json", "hash": "f" * 64},
            "evidence_hash": "f" * 64,
        }
        # 沒有因為「第二次執行」而多出任何 job（= 沒有多一次 model invocation）。
        assert jobs_after == jobs_before

        # 形狀相容：registry 回傳可直接組出 completion reused_from payload，
        # 不需任何欄位轉換（consumer 接線時不會在 _normalize_reused_from 掛掉）。
        from paulsha_cortex.coordinator.completion import _normalize_reused_from

        normalized = _normalize_reused_from(
            {
                "run_id": reused["run_id"],
                "job_id": reused["job_id"],
                "evidence_hash": reused["evidence_hash"],
            }
        )
        assert normalized["evidence_hash"] == "f" * 64

    def test_authority_candidate_model_change_invalidates_reuse(self, tmp_path: Path) -> None:
        """AC4：authority／candidate／model 任一變更都必須精準 invalidate，
        不能沿用舊 key 的 evidence。"""
        registry = _make_registry(tmp_path)
        key = compute_stage_execution_key(**_key_kwargs())
        _create_terminal_job_with_evidence(registry, task="job-a", stage_execution_key=key)

        changed_model_key = compute_stage_execution_key(**_key_kwargs(model="a-new-model"))
        changed_candidate_key = compute_stage_execution_key(**_key_kwargs(candidate_sha="f" * 40))
        changed_repo_key = compute_stage_execution_key(**_key_kwargs(repo="other/repo"))

        for invalidated_key in (changed_model_key, changed_candidate_key, changed_repo_key):
            assert (
                registry.find_reusable_stage_evidence(
                    invalidated_key, is_evidence_still_valid=lambda _e: True
                )
                is None
            )


def test_create_job_rejects_malformed_stage_execution_key(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    with pytest.raises(ValueError):
        registry.create_job(
            task="job-bad-key",
            persona="review",
            branch="feature/job-bad-key",
            pane="pane-1",
            worktree="/tmp/job-bad-key",
            workflow_stage_execution_key="too-short",
        )


def test_create_job_persists_stage_execution_key(tmp_path: Path) -> None:
    registry = _make_registry(tmp_path)
    key = compute_stage_execution_key(**_key_kwargs())
    job = registry.create_job(
        task="job-persisted",
        persona="review",
        branch="feature/job-persisted",
        pane="pane-1",
        worktree="/tmp/job-persisted",
        workflow_stage_execution_key=key,
    )
    assert registry.get_job(job["job_id"])["workflow_stage_execution_key"] == key

    reloaded = JobRegistry(state_path=registry._state_path)
    assert reloaded.get_job(job["job_id"])["workflow_stage_execution_key"] == key
