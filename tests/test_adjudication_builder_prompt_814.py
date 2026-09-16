"""#814：`retry-build --reason` 的 operator 裁決必須以「必須執行的指令」進 builder prompt。

#752／#757 已把裁決放進 contract 的 `operator_adjudications` 鍵，但 prompt 沒有任何一句話
告訴 builder 那是 operator 的權威裁決、必須實作——retry_context 有明示語句、裁決沒有；
實機（run `workflow-05c65e8cd09879fc741f`）builder 因此交出 tree 與前候選相同的空 commit，
而 verifier 讀同一份 evidence 逐條判 failed，形成死結。本檔以真 writer→真 reader→真 prompt
builder 端到端鎖住：reason 逐字進 prompt、附帶指令語句、builder 與 reviewer 讀到同一份，
且 CLI 回傳告訴 operator 裁決會在下一次 dispatch 注入。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import manager, work_actions
from paulsha_cortex.coordinator.workflow import WorkflowStep

REASON = (
    "operator 裁決（獨立審查發現）：(1) rollback 診斷誤導 (2) _restore_file 用非原子的 "
    "path.write_bytes (3) installer.py:544/551 的區域變數 paths 覆蓋 module import "
    "(4) _load_project_config_payload 的 except 吞掉原始例外"
)


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="workflow-814aaaaaaaaaaaaaaaaa",
        work_id="installer-shared-config-guard",
        repo="owner/repo",
        source_revision="a" * 40,
        candidate_head="b" * 40,
        openspec_refs=("installer-shared-config-guard",),
        current_phase="verify",
    )


def _step(card: str, *, phase: str, persona: str) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona=persona,
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
    )


def _record(tmp_path: Path, run) -> dict:
    # 真 writer：與 `_retry_build_action` 內部同一條落地路徑（card=subagent-build）。
    evidence = work_actions._record_operator_adjudication(
        run=run,
        card="subagent-build",
        args={"reason": REASON, "actor": "operator"},
        state_path=tmp_path / "jobs.json",
        now_epoch=1_756_389_475.145,
    )
    assert evidence is not None and Path(evidence["ref"]).is_file()
    return evidence


def test_retry_build_reason_reaches_builder_prompt_verbatim_with_directive(tmp_path: Path) -> None:
    run = _run()
    _record(tmp_path, run)

    rows = manager._operator_adjudications(run, tmp_path)
    assert rows and rows[-1]["reason"] == REASON

    prompt = manager._workflow_job_prompt(
        run,
        _step("subagent-build", phase="build", persona="builder"),
        builder_job_id="job-1",
        coordinator_root=tmp_path,
        operator_adjudications=rows,
    )

    # 逐字進 prompt（票面第 4 點的驗收）。
    assert REASON in prompt
    # 不是只當 contract 資料塞著：必須有明示語句告訴 builder 這是權威裁決、要實作、
    # 空 commit 會被讀同一份 evidence 的 verifier 打回。
    assert manager.OPERATOR_ADJUDICATION_DIRECTIVE.strip() in prompt
    assert "operator_adjudications" in manager.OPERATOR_ADJUDICATION_DIRECTIVE
    assert "verif" in manager.OPERATOR_ADJUDICATION_DIRECTIVE  # 指出 verifier 讀同一份 evidence


def test_prompt_without_adjudications_is_byte_identical_to_before(tmp_path: Path) -> None:
    run = _run()
    step = _step("subagent-build", phase="build", persona="builder")
    prompt = manager._workflow_job_prompt(run, step, builder_job_id="job-1", coordinator_root=tmp_path)
    assert "operator_adjudications" not in prompt
    assert manager.OPERATOR_ADJUDICATION_DIRECTIVE.strip() not in prompt
    assert manager.OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE.strip() not in prompt


def test_builder_and_reviewer_prompts_consume_the_same_evidence(tmp_path: Path) -> None:
    """票面留言的死結：verifier 讀得到、builder 讀不到。兩側必須讀同一份。"""
    run = _run()
    _record(tmp_path, run)
    rows = manager._operator_adjudications(run, tmp_path)

    builder_prompt = manager._workflow_job_prompt(
        run, _step("subagent-build", phase="build", persona="builder"),
        builder_job_id="job-1", coordinator_root=tmp_path, operator_adjudications=rows,
    )
    reviewer_prompt = manager._workflow_job_prompt(
        run, _step("verification", phase="verify", persona="reviewer"),
        builder_job_id="job-1", coordinator_root=tmp_path, operator_adjudications=rows,
    )
    assert REASON in builder_prompt and REASON in reviewer_prompt
    # persona 各自的語意：builder 要實作；reviewer read-only，把未實作的裁決當 blocking finding。
    assert manager.OPERATOR_ADJUDICATION_DIRECTIVE.strip() in builder_prompt
    assert manager.OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE.strip() in reviewer_prompt
    assert manager.OPERATOR_ADJUDICATION_DIRECTIVE.strip() not in reviewer_prompt
    assert "read-only" in manager.OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE
    assert "blocking finding" in manager.OPERATOR_ADJUDICATION_REVIEWER_DIRECTIVE


def test_dispatch_still_passes_run_level_adjudications_to_every_card() -> None:
    """#757 的接線不得被本票退回：裁決是 run 級、隨每次派工出現，不做「消費後不再注入」。"""
    source = inspect.getsource(manager._dispatch_workflow_card)
    assert "operator_adjudications=_operator_adjudications(run, coordinator_root)" in source


def test_retry_actions_tell_operator_the_ruling_will_be_injected(tmp_path: Path) -> None:
    """票面第 3 點：`retry-build --reason` 成功時要明示裁決已記錄且會於下一次 dispatch 注入。"""
    run = _run()
    evidence = _record(tmp_path, run)

    receipt = work_actions.operator_adjudication_receipt(evidence, card="subagent-build")
    assert receipt["evidence"] == evidence
    assert "operator_adjudications" in receipt["next_step_hint"]
    assert "subagent-build" in receipt["next_step_hint"]
    assert work_actions.operator_adjudication_receipt(None, card="subagent-build") is None

    # 兩個帶 --reason 的 action 都必須把 receipt 放進回傳（CLI 原樣印出 JSON）。
    for fn in (work_actions._retry_build_action, work_actions._retry_card_action):
        assert "operator_adjudication_receipt(" in inspect.getsource(fn), fn.__name__
