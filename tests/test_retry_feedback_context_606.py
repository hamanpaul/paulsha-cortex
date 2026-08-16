"""#606：retry-card 重派 prompt 不含前次採信失敗證據——無回饋重試是決定論的重複。

現場（0816，run ``workflow-7812abefede9d9b5d601``，#501 dogfooding）：
subagent-build 的 job 492 與 493，builder（codex／gpt-5.6-luna）兩次自稱
``pytest: passed``，Manager 的 gate ledger 兩次獨立重跑抓到**同一個**失敗
（``test_workflow_registry.py::test_v1_migration_creates_immutable_backup_and_isolates_legacy_records``），
兩次 ``GateContradictionError`` 內容逐字相同。

根因不是重派錯誤：``retry-card``（#545／#569）刻意用原卡 prompt，契約不可竄改是
對的。缺的是**回饋通道**——prompt 沒有任何欄位攜帶「上一次為什麼被拒」，於是
builder 每次都跑 focused tests 自認綠、自稱全套 passed，ledger 每次抓包，重試
只是決定論的重複。

本檔釘住三件事：

1. 重派時 ``_workflow_job_prompt`` 機械附上 retry-context，內容全部來自 **Manager
   自產證據**（自己的 gate ledger、自己的採信判準），一個字都不取自模型輸出
   （與 #540 的不可竄改性同一條紀律）；
2. **首派 prompt 逐字不變**——回饋只加在真的有前次失敗的那條路徑上；
3. status 語意補上範圍紀律：「focused 綠不得推定宣告的 gate 綠」，具體的 gate
   名稱與命令由 ``PSC_GATE_CMD_*`` 宣告機械導出（#541 的同一條生成紀律）。

計數（``attempt``／``redispatch_count``）是 #555（per-card 熔斷）的鉤子：本票不
實作熔斷，只保證計數存在且正確。
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import gate_ledger, manager
from paulsha_cortex.coordinator import terminal_contract as tc
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep

from diagnostic_fixtures import fixture_needs_human_reason


HEAD = "d" * 40
CARD = "subagent-build"
# 現場逐字的失敗測試名（issue #606 的 ledger detail）。
FIELD_FAILURE = (
    "FAILED tests/test_workflow_registry.py::"
    "test_v1_migration_creates_immutable_backup_and_isolates_legacy_records"
    " - AssertionError: legacy v1 slices 遷移 round-trip 不一致"
)


def _step(card: str = CARD, *, gate_result: str = "pending") -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result=gate_result,
        test_policy="focused",
        action="Implement the accepted plan with the minimum diff.",
        commit_policy="required",
    )


def _run(registry: JobRegistry, tmp_path: Path, **overrides):
    payload = {
        "work_id": "demo",
        "repo": "acme/demo",
        "claim_key": "claim:v1:" + "1" * 64,
        "source_revision": "2" * 64,
        "workspace_root": str(tmp_path),
        "combo": "feature-oneshot",
        "current_phase": "build",
        "steps": (_step(),),
        "issue_refs": (),
        "openspec_refs": (),
        "pr_refs": (),
        "candidate_head": HEAD,
        "attempts": {"build": 1},
        "facets": (),
        "gate_status": "running",
    }
    payload.update(overrides)
    return registry._manager_create_workflow_run(**payload)


def _terminal_payload(run, card: str = CARD) -> dict:
    """builder 自稱 passed 且自報 ``pytest: passed`` 的 canonical envelope。"""

    return {
        "schema_version": tc.TERMINAL_SCHEMA_VERSION,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": run.run_id,
        "card_id": card,
        "candidate": HEAD,
        "outputs": [],
        "diagnostics": {},
        "gate_evidence": [{"name": "pytest", "status": "passed"}],
    }


def _prior_job(
    registry: JobRegistry,
    run,
    tmp_path: Path,
    *,
    terminal: dict | None,
    ledger_detail: str | None = FIELD_FAILURE,
    card: str = CARD,
    name: str = "prior",
):
    """重建一顆「已終止但 evidence 綁不上」的舊 job（log ＋ manager 自產 ledger）。"""

    log = tmp_path / "logs" / "workflow" / f"{name}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "" if terminal is None else json.dumps(terminal) + "\n", encoding="utf-8"
    )
    if ledger_detail is not None:
        tc.gate_ledger_path(log).write_text(
            json.dumps(
                {
                    "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
                    "kind": tc.GATE_LEDGER_KIND,
                    "slice_id": name,
                    "gates": [
                        {
                            "name": "pytest",
                            "command": "python3 -m pytest -q",
                            "exit_code": 1,
                            "status": "failed",
                            "detail": ledger_detail,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    job = registry.create_job(
        task=f"wf-{card}",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        workflow_run_id=run.run_id,
        workflow_card=card,
        workflow_phase="build",
        workflow_test_policy="focused",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    return registry.get_job(job["job_id"])


def _contract(prompt: str) -> dict:
    return json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])


# ==========================================================================
# 段 1：重派 prompt 機械附上前次採信失敗證據
# ==========================================================================


def test_retry_prompt_carries_the_previous_gate_ledger_failure(tmp_path: Path) -> None:
    """RED（修復前）：重派 prompt 與首派逐字相同，builder 看不到自己被抓包的證據。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    prior = _prior_job(registry, run, tmp_path, terminal=_terminal_payload(run))

    context = manager._workflow_retry_context([prior], registry=registry)
    assert context is not None

    # 採信錯誤類別與 canonical 文字：來自 manager 自己的判準，不是模型文字。
    error = context["acceptance_error"]
    assert error["error_class"] == "GateContradictionError"
    assert error["reason"] == "gate-status-contradiction"
    assert "pytest" in error["message"]

    # gate ledger 的 failed gate：名稱＋exit code＋截尾輸出。
    assert context["failed_gates"] == [
        {"name": "pytest", "status": "failed", "exit_code": 1, "detail": FIELD_FAILURE}
    ]

    prompt = manager._workflow_job_prompt(
        run,
        run.steps[0],
        builder_job_id=None,
        coordinator_root=tmp_path,
        env={"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"},
        retry_context=context,
    )
    contract = _contract(prompt)
    assert contract["retry_context"] == context
    # 現場逐字的失敗測試名必須真的到得了模型眼前。
    assert "test_v1_migration_creates_immutable_backup_and_isolates_legacy_records" in prompt
    # 明示語句：先重現並修復，再完成本卡。
    assert "redispatched" in prompt
    assert "Reproduce that failure first" in prompt
    assert "not the previous attempt's self-report" in prompt


def test_retry_context_evidence_is_manager_generated_not_model_text(
    tmp_path: Path,
) -> None:
    """模型自報的 gate 結果不得成為 retry-context 的內容來源。

    舊 envelope 自報 ``pytest: passed``；retry-context 只能說 failed——它讀的是
    manager 在模型行程結束**之後**寫出的 ledger（``gate_ledger.write_gate_ledger``）。
    """

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    prior = _prior_job(registry, run, tmp_path, terminal=_terminal_payload(run))

    context = manager._workflow_retry_context([prior], registry=registry)

    assert context["evidence_source"] == "manager-independent"
    assert [row["status"] for row in context["failed_gates"]] == ["failed"]
    assert context["previous_job_id"] == str(prior["job_id"])


def test_retry_prompt_covers_the_no_json_terminal_shape(tmp_path: Path) -> None:
    """#569 形狀：job exit 0 但 log 完全沒有 JSON envelope，也要有回饋。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    prior = _prior_job(registry, run, tmp_path, terminal=None, ledger_detail=None)

    context = manager._workflow_retry_context([prior], registry=registry)

    assert context["acceptance_error"]["message"] == (
        "workflow terminal log has no JSON evidence"
    )
    # 沒有 ledger 就沒有 failed gate 可附；不得編造。
    assert context["failed_gates"] == []

    prompt = manager._workflow_job_prompt(
        run,
        run.steps[0],
        builder_job_id=None,
        coordinator_root=tmp_path,
        retry_context=context,
    )
    assert "workflow terminal log has no JSON evidence" in prompt


# ==========================================================================
# 段 2：首派逐字不變
# ==========================================================================


def test_first_dispatch_prompt_is_byte_identical(tmp_path: Path) -> None:
    """沒有前次失敗時，prompt 與加上 retry-context 通道之前**逐字相同**。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    env = {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"}

    # 空的 prior job 集合（＝首派）不得產生 retry-context。
    assert manager._workflow_retry_context([], registry=registry) is None

    baseline = manager._workflow_job_prompt(
        run, run.steps[0], builder_job_id=None, coordinator_root=tmp_path, env=env
    )
    with_default = manager._workflow_job_prompt(
        run,
        run.steps[0],
        builder_job_id=None,
        coordinator_root=tmp_path,
        env=env,
        retry_context=manager._workflow_retry_context([], registry=registry),
    )

    assert with_default == baseline
    assert "retry_context" not in baseline
    assert "redispatched" not in baseline


def test_first_dispatch_of_a_card_is_unaffected_by_other_cards(tmp_path: Path) -> None:
    """另一張卡失敗過，不得污染這張卡的首派 prompt。

    `_dispatch_workflow_card` 的 ``matching`` 已經以「同一張卡（verify／review 另
    以 candidate 定錨）」過濾；這裡釘住 retry-context 完全依賴那份過濾結果。
    """

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(
        registry,
        tmp_path,
        steps=(_step("tdd-red", gate_result="passed"), _step()),
    )
    other = _prior_job(
        registry,
        run,
        tmp_path,
        terminal=_terminal_payload(run, card="tdd-red"),
        card="tdd-red",
        name="other-card",
    )
    matching = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_card") == CARD
    ]

    assert other["workflow_card"] == "tdd-red"
    assert matching == []
    assert manager._workflow_retry_context(matching, registry=registry) is None


# ==========================================================================
# 段 3：證據截斷上限（prompt 不得被 gate 輸出撐爆）
# ==========================================================================


def test_gate_detail_is_truncated_to_the_evidence_budget(tmp_path: Path) -> None:
    """失敗的全套 pytest 可以吐出數萬字；retry-context 有硬上限且明示被截。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    detail = "x" * 5000 + FIELD_FAILURE
    prior = _prior_job(
        registry, run, tmp_path, terminal=_terminal_payload(run), ledger_detail=detail
    )

    context = manager._workflow_retry_context([prior], registry=registry)
    row = context["failed_gates"][0]

    assert len(row["detail"]) == manager.RETRY_CONTEXT_EVIDENCE_LIMIT
    assert row["detail_truncated"] is True
    # 保留尾段：pytest 的 short summary 在最後，那才是可重現的線索。
    assert row["detail"].endswith(FIELD_FAILURE)


def test_acceptance_error_message_is_truncated(tmp_path: Path) -> None:
    """``GateContradictionError`` 的訊息會把 ledger detail 內嵌進去，同樣要有上限。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    prior = _prior_job(
        registry,
        run,
        tmp_path,
        terminal=_terminal_payload(run),
        ledger_detail="y" * 4000,
    )

    context = manager._workflow_retry_context([prior], registry=registry)
    error = context["acceptance_error"]

    assert len(error["message"]) == manager.RETRY_CONTEXT_MESSAGE_LIMIT
    assert error["message_truncated"] is True


def test_multiple_failed_gates_share_one_evidence_budget(tmp_path: Path) -> None:
    """預算是**全體**的：第一個 gate 吃滿之後，後續 gate 的 detail 被丟掉但明示。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    log = tmp_path / "logs" / "workflow" / "multi.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(_terminal_payload(run)) + "\n", encoding="utf-8")
    tc.gate_ledger_path(log).write_text(
        json.dumps(
            {
                "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
                "kind": tc.GATE_LEDGER_KIND,
                "slice_id": "multi",
                "gates": [
                    {"name": "pytest", "exit_code": 1, "status": "failed", "detail": "a" * 4000},
                    {"name": "openspec", "exit_code": 2, "status": "failed", "detail": "b" * 100},
                    {"name": "policy", "exit_code": 0, "status": "passed", "detail": ""},
                ],
            }
        ),
        encoding="utf-8",
    )
    job = registry.create_job(
        task="wf-multi",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        workflow_run_id=run.run_id,
        workflow_card=CARD,
        workflow_phase="build",
        workflow_test_policy="focused",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    rows = manager._prior_card_failed_gates(registry.get_job(job["job_id"]))

    # passed 的 gate 不入列（判準與採信端 `_ledger_outcomes` 同一份）。
    assert [row["name"] for row in rows] == ["pytest", "openspec"]
    assert sum(len(row["detail"]) for row in rows) == manager.RETRY_CONTEXT_EVIDENCE_LIMIT
    assert rows[1]["detail"] == ""
    assert rows[1]["detail_truncated"] is True
    assert rows[1]["exit_code"] == 2


# ==========================================================================
# 段 4：重派計數（#555 per-card 熔斷的鉤子）
# ==========================================================================


def test_redispatch_count_tracks_the_burned_jobs(tmp_path: Path) -> None:
    """#606 現場燒了兩顆 job（492／493）：第三次派工必須自報 attempt=3。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    first = _prior_job(
        registry, run, tmp_path, terminal=_terminal_payload(run), name="job-492"
    )
    second = _prior_job(
        registry, run, tmp_path, terminal=_terminal_payload(run), name="job-493"
    )

    one = manager._workflow_retry_context([first], registry=registry)
    two = manager._workflow_retry_context([first, second], registry=registry)

    assert (one["attempt"], one["redispatch_count"]) == (2, 1)
    assert (two["attempt"], two["redispatch_count"]) == (3, 2)
    # 最新那顆才是證據來源；全部 job id 仍列出供稽核（#555 的計數來源）。
    assert two["previous_job_id"] == str(second["job_id"])
    assert two["previous_job_ids"] == [str(first["job_id"]), str(second["job_id"])]

    prompt = manager._workflow_job_prompt(
        run,
        run.steps[0],
        builder_job_id=None,
        coordinator_root=tmp_path,
        retry_context=two,
    )
    assert "attempt 2 was rejected" in prompt


# ==========================================================================
# 段 5：status 語意的範圍紀律（#541 的同一條機械生成紀律）
# ==========================================================================


def test_status_policy_forbids_inferring_the_full_gate_from_a_focused_subset(
    tmp_path: Path,
) -> None:
    """現場的行為模式：focused 綠 → 自稱全套綠。prompt 過去對此隻字未提。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    env = {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"}

    schema = _contract(
        manager._workflow_job_prompt(
            run, run.steps[0], builder_job_id=None, coordinator_root=tmp_path, env=env
        )
    )["terminal_schema"]
    policy = schema["status_policy"]

    assert "focused subset" in policy
    # 機械生成：實際會被 manager 重跑的命令逐字出現，不是手寫的泛稱。
    assert "python3 -m pytest -q" in policy
    assert policy.endswith(gate_ledger.gate_scope_honesty_hint(env))


def test_status_policy_scope_hint_tracks_the_declaration(tmp_path: Path) -> None:
    """宣告改動自動同步進 prompt——與 #541 的 allowed_names 同一條導出路徑。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)

    changed = _contract(
        manager._workflow_job_prompt(
            run,
            run.steps[0],
            builder_job_id=None,
            coordinator_root=tmp_path,
            env={"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q -x"},
        )
    )["terminal_schema"]["status_policy"]
    assert "python3 -m pytest -q -x" in changed

    empty = _contract(
        manager._workflow_job_prompt(
            run, run.steps[0], builder_job_id=None, coordinator_root=tmp_path, env={}
        )
    )["terminal_schema"]["status_policy"]
    assert "never authorizes claiming the declared gate is green" in empty


# ==========================================================================
# 段 6：真正的重派路徑（retry-card／forced retry 共用的唯一組裝點）
# ==========================================================================


class _Launcher:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self._sink.append(prompt)
        return LaunchHandle(
            executor="codex",
            model_id="gpt-primary",
            session_name=slice_id,
            pid=100,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }
        ]
    )


def test_forced_redispatch_prompt_carries_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retry-card／daemon forced retry 真的派出去的那份 prompt 必須帶回饋。

    兩條路徑都收斂在 `manager._dispatch_workflow_card` 這一個組裝點，所以這裡
    只需要釘住那一點。
    """

    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -m pytest -q")
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    anchor = registry.create_job(
        task="wf-anchor",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        subject_head=HEAD,
        workflow_run_id=run.run_id,
        workflow_card="worktree-isolation",
        workflow_phase="build",
    )
    registry.update_headless_result(anchor["job_id"], status="exited", exit_code=0)
    _prior_job(registry, run, tmp_path, terminal=_terminal_payload(run))
    # 重派前的狀態：該卡被打回 pending（`_manager_reset_workflow_for_retry_card`
    # 做的就是這件事），run 回到 ongoing。
    run = registry._manager_update_workflow_run(
        run.run_id,
        steps=tuple(replace(step, gate_result="pending") for step in run.steps),
    )
    prompts: list[str] = []

    replacement = manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=registry.get_workflow_run(run.run_id),
        identities=_identities(),
        launcher_factory=lambda _: _Launcher(prompts),
        coordinator_root=tmp_path / "coordinator",
        force_new_card=True,
    )

    assert replacement is not None
    assert len(prompts) == 1
    contract = _contract(prompts[0])
    assert contract["retry_context"]["attempt"] == 2
    assert contract["retry_context"]["failed_gates"][0]["name"] == "pytest"
    assert (
        "test_v1_migration_creates_immutable_backup_and_isolates_legacy_records"
        in prompts[0]
    )
    # 卡片契約本身一個字都沒被改（重派的是原卡）。
    assert contract["card_id"] == CARD
    assert contract["action"] == "Implement the accepted plan with the minimum diff."


def test_first_dispatch_through_the_real_path_has_no_retry_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一條 dispatch 路徑，首派 prompt 不得出現 retry_context。"""

    monkeypatch.setenv("PSC_GATE_CMD_PYTEST", "python3 -m pytest -q")
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    # worktree-isolation 已錨定 worktree／branch（現場即如此）；這張卡自己還沒派過。
    anchor = registry.create_job(
        task="wf-anchor",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        subject_head=HEAD,
        workflow_run_id=run.run_id,
        workflow_card="worktree-isolation",
        workflow_phase="build",
    )
    registry.update_headless_result(anchor["job_id"], status="exited", exit_code=0)
    prompts: list[str] = []

    manager.dispatch_workflow_card(
        type("D", (), {"_registry": registry, "_git_runner": None})(),
        run=registry.get_workflow_run(run.run_id),
        identities=_identities(),
        launcher_factory=lambda _: _Launcher(prompts),
        coordinator_root=tmp_path / "coordinator",
    )

    assert len(prompts) == 1
    assert "retry_context" not in prompts[0]
    assert "redispatched" not in prompts[0]


# ==========================================================================
# 段 7：fail-soft——證據取不到不得害死一次合法重派
# ==========================================================================


def test_unreadable_evidence_does_not_block_the_redispatch(tmp_path: Path) -> None:
    """log 不存在／ledger 壞掉時仍回傳計數，只是沒有證據可附。"""

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = _run(registry, tmp_path)
    log = tmp_path / "logs" / "workflow" / "broken.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("{}\n", encoding="utf-8")
    tc.gate_ledger_path(log).write_text("not json", encoding="utf-8")
    job = registry.create_job(
        task="wf-broken",
        persona="builder",
        branch="feature/12-demo",
        pane="",
        worktree=str(tmp_path),
        workflow_run_id=run.run_id,
        workflow_card=CARD,
        workflow_phase="build",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    context = manager._workflow_retry_context(
        [registry.get_job(job["job_id"])], registry=registry
    )

    assert context["attempt"] == 2
    assert context["failed_gates"] == []
    # log 有內容但沒有 terminal payload：仍是可用的 canonical 診斷。
    assert context["acceptance_error"]["error_class"] == "ValueError"

    prompt = manager._workflow_job_prompt(
        run,
        run.steps[0],
        builder_job_id=None,
        coordinator_root=tmp_path,
        retry_context=context,
    )
    assert "retry_context" in prompt
