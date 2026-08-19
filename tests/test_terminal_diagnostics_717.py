"""#717：build 卡的「我需要人」不得以 40-hex candidate 為入場券，模型 diagnostics 要落地。

## 這張票在修什麼

同一族診斷缺陷的第六輪（前五輪 #672 #679 #701 #704 #707）。08-19 實機 job
`wf-6c37c77ca1-worktree-isolation-7`：模型**正確地**回了 `needs_human`，還把病因
逐字寫進 envelope 的 `diagnostics`。Manager 端落成

    ATTENTION: build/card-terminal-schema-retry-exhausted
    D=同一張卡的 terminal envelope 連續 schema mismatch 已達上限（2/2）：
      workflow terminal payload did not satisfy the result contract

模型寫的病因一個字都沒進 attention。三個缺陷疊在一起：

**(1) 表達力**：`manager._retryable_nonpassing_workflow_terminal()` 是「模型明示
要求停止」的唯一入口，而 build phase 的入場券是「交得出 `git rev-parse HEAD` 的
40-hex SHA」。本次的失敗原因**正是「我一條命令都跑不了」**（#716），模型結構上取
不到 HEAD ⇒ 判準不過 ⇒ 掉進 `_malformed_workflow_card_terminal()` 被當成 schema
壞掉。裁決採票上的 (a)：非通過狀態下 `candidate` 不再是授權欄位。

**(2) 診斷不落地**：`manager._terminal_parse_diagnostics()` 只保留 `reason`／
`observed_head`／`validation_path`，沒讀 envelope 上模型寫的 `diagnostics`；
`_canonicalize_card_terminal()` 的註解宣稱那個欄位「已在
`_assert_terminal_gate_consistency` 消費完畢」，但 malformed／schema-retry 分支
根本走不到那條路徑。

**(3) 額度與重派語意不一致**（#717 追加觀察）：`retry-card` 重置只 bump
`attempts[phase]`，沒清 `schema-mismatch:<card>`，於是 operator 顯式重派之後
「這一輪一次自動重試都沒有」卻寫成「已達上限（2/2）」。

## 本檔釘住什麼

1. `ExplicitStopShapeTests`——`needs_human`／`failed` × candidate 為 null／64-hex／
   40-hex 六格全部是**合法的明示停止**，且**一格都不得**被判成 schema mismatch；
   型別根本不對（數字／物件／陣列）的仍 fail closed。
2. `ModelDiagnosticsTests`——模型 `diagnostics` 逐字落進唯讀診斷，且有界。
3. `ExplicitStopLandingTests`——端到端：實機那份 envelope 進來，attention 的 `D=`
   逐字含模型病因，且**不消耗** schema retry 額度。
4. `SchemaRetryBudgetTests`——`retry-card` 清本輪額度、累計不清；attention 的兩個
   數字各自正確。
"""

from __future__ import annotations

import json
from dataclasses import replace as _replace
from pathlib import Path

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator import terminal_contract as tc
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry

from git_fixtures import StubWorktreeCreator


# 逐字取自 job `wf-6c37c77ca1-worktree-isolation-7`（2026-08-19 17:28，exit_code=0）。
# `terminal_contract.validate_envelope()` 對這份 payload 回 OK；被判 malformed 的是
# 下游。candidate 是 64-hex——那是 contract 裡看得到的 `source_revision`，模型取不到
# HEAD 時唯一填得出來的東西。
REAL_ENVELOPE = {
    "schema_version": 2,
    "kind": "workflow-card",
    "status": "needs_human",
    "run_id": "workflow-c24a4e837b306e8c6c1a",
    "card_id": "worktree-isolation",
    "candidate": "22b88b01e9b25245014bae828e4c0577ac1fe840232dea94e938b53a67e20ac1",
    "outputs": [],
    "diagnostics": {
        "failure": (
            "唯讀 git 檢查連續兩次遭執行環境 sandbox runtime panic，"
            "無法確認 worktree 隔離狀態或執行 pytest。"
        ),
        "error": (
            "permission profiles requiring direct runtime enforcement are "
            "incompatible with --use-legacy-landlock"
        ),
    },
    "gate_evidence": [],
}

REAL_FAILURE_TEXT = REAL_ENVELOPE["diagnostics"]["failure"]
REAL_ERROR_TEXT = REAL_ENVELOPE["diagnostics"]["error"]


def _card_job(log: Path, *, phase: str = "build") -> dict[str, object]:
    """最小的 job 投影——這一族判準函式只讀這幾個欄位。"""

    return {
        "job_id": "job-717",
        "workflow_evidence": None,
        "status": "exited",
        "exit_code": 0,
        "workflow_phase": phase,
        "workflow_run_id": "run",
        "workflow_card": "card",
        "log_path": str(log),
        "dispatch_head": "d" * 40,
    }


def _legacy_card(**overrides: object) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "needs_human",
        "run_id": "run",
        "card_id": "card",
        "candidate": None,
        "outputs": [],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# (1) 表達力：明示停止不以 40-hex candidate 為入場券
# --------------------------------------------------------------------------


def test_explicit_stop_accepts_every_candidate_shape(tmp_path: Path) -> None:
    """驗收：`needs_human`／`failed` × candidate 為 null／64-hex／40-hex 六格全通。

    64-hex 那一格就是實機現場——模型取不到 HEAD，退而填 contract 裡看得到的
    `source_revision`。它過去是六格裡唯一被判 schema mismatch 的形狀之一。
    """

    log = tmp_path / "terminal.jsonl"
    job = _card_job(log)
    for status in ("failed", "needs_human"):
        for candidate in (None, "b" * 64, "a" * 40):
            log.write_text(
                json.dumps(_legacy_card(status=status, candidate=candidate)) + "\n",
                encoding="utf-8",
            )
            assert manager._retryable_nonpassing_workflow_terminal(job) is True, (
                status,
                candidate,
            )
            # 迴歸釘住：合法的明示停止不得再被判成 schema mismatch。
            assert manager._malformed_workflow_card_terminal(job) is False, (
                status,
                candidate,
            )


def test_explicit_stop_accepts_plan_phase_candidate_shapes(tmp_path: Path) -> None:
    """plan 卡一併受惠：判準的理由與 phase 無關（#578 的另一半）。"""

    log = tmp_path / "terminal.jsonl"
    job = _card_job(log, phase="plan")
    for candidate in (None, "b" * 64, "a" * 40):
        log.write_text(
            json.dumps(_legacy_card(status="needs_human", candidate=candidate)) + "\n",
            encoding="utf-8",
        )
        assert manager._retryable_nonpassing_workflow_terminal(job) is True, candidate
        assert manager._malformed_workflow_card_terminal(job) is False, candidate


def test_explicit_stop_still_fails_closed_on_broken_shapes(tmp_path: Path) -> None:
    """放寬的只有 candidate 的**值**，不是形狀契約本身。"""

    log = tmp_path / "terminal.jsonl"
    job = _card_job(log)

    # candidate 型別根本不對 → 真的 schema 壞掉。
    for candidate in (7, {"sha": "a" * 40}, ["a" * 40], True):
        log.write_text(
            json.dumps(_legacy_card(candidate=candidate)) + "\n", encoding="utf-8"
        )
        assert manager._retryable_nonpassing_workflow_terminal(job) is False, candidate
        assert manager._malformed_workflow_card_terminal(job) is True, candidate

    # 綁定對不上（run_id／card_id）仍 fail closed——放寬不得變成「非通過就放行」。
    for key in ("run_id", "card_id"):
        log.write_text(
            json.dumps(_legacy_card(**{key: "somebody-else"})) + "\n", encoding="utf-8"
        )
        assert manager._retryable_nonpassing_workflow_terminal(job) is False, key
        assert manager._malformed_workflow_card_terminal(job) is True, key

    # 多／少欄位仍 fail closed。
    log.write_text(
        json.dumps({**_legacy_card(), "extra": 1}) + "\n", encoding="utf-8"
    )
    assert manager._retryable_nonpassing_workflow_terminal(job) is False
    assert manager._malformed_workflow_card_terminal(job) is True

    # `passed` 一個位元都沒動：仍必須是 40-hex。
    log.write_text(
        json.dumps(_legacy_card(status="passed", candidate="b" * 64)) + "\n",
        encoding="utf-8",
    )
    assert manager._retryable_nonpassing_workflow_terminal(job) is False
    assert manager._malformed_workflow_card_terminal(job) is True


def test_real_regression_envelope_is_an_explicit_stop(tmp_path: Path) -> None:
    """實機現場的那一份 canonical envelope 直接當 fixture。"""

    log = tmp_path / "terminal.jsonl"
    log.write_text(json.dumps(REAL_ENVELOPE) + "\n", encoding="utf-8")
    job = {
        **_card_job(log),
        "workflow_run_id": REAL_ENVELOPE["run_id"],
        "workflow_card": REAL_ENVELOPE["card_id"],
    }
    # 契約層本來就對這份 payload 回 OK；問題一直在下游。
    assert tc.validate_envelope(REAL_ENVELOPE).status == "needs_human"
    assert manager._retryable_nonpassing_workflow_terminal(job) is True
    assert manager._malformed_workflow_card_terminal(job) is False


# --------------------------------------------------------------------------
# (2) 診斷落地
# --------------------------------------------------------------------------


def test_parse_diagnostics_carries_model_diagnostics_verbatim(tmp_path: Path) -> None:
    """模型逐字寫的病因要進唯讀診斷，且仍不授予任何 authority。"""

    log = tmp_path / "terminal.jsonl"
    log.write_text(json.dumps(REAL_ENVELOPE) + "\n", encoding="utf-8")
    diagnostics = manager._terminal_parse_diagnostics(_card_job(log))

    payload = diagnostics.as_dict()
    assert payload["model_diagnostics"]["failure"] == REAL_FAILURE_TEXT
    assert payload["model_diagnostics"]["error"] == REAL_ERROR_TEXT
    # R4／D6 不變：可觀測 ≠ 可授權。
    assert payload["authority_granted"] is False
    assert diagnostics.candidate_authority() is None
    assert "candidate" not in payload

    text = diagnostics.model_diagnostics_text()
    assert REAL_FAILURE_TEXT in text
    assert REAL_ERROR_TEXT in text


def test_model_diagnostics_are_bounded(tmp_path: Path) -> None:
    """一個亂寫的模型不得撐爆 attention 欄位（沿用 #606 的 2000 字預算慣例）。"""

    log = tmp_path / "terminal.jsonl"
    log.write_text(
        json.dumps(
            {
                **REAL_ENVELOPE,
                "diagnostics": {"a": "甲" * 5000, "b": "乙" * 5000, "c": "丙" * 5000},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    diagnostics = manager._terminal_parse_diagnostics(_card_job(log))
    rows = dict(diagnostics.model_diagnostics)
    # 預算是**全體**的：所有 value 加總不超過上限（`…` 截斷標記另計）。
    budget_used = sum(len(value.rstrip("…")) for value in rows.values())
    assert budget_used <= manager.TERMINAL_MODEL_DIAGNOSTICS_LIMIT
    assert any(value.endswith("…") for value in rows.values())


def test_model_diagnostics_tolerate_missing_or_odd_shapes(tmp_path: Path) -> None:
    """診斷是加值：讀不到不得害死已經失敗的採信路徑。"""

    log = tmp_path / "terminal.jsonl"

    # 完全讀不到 terminal JSON。
    log.write_text("not json at all\n", encoding="utf-8")
    assert manager._terminal_parse_diagnostics(_card_job(log)).model_diagnostics == ()

    # legacy envelope 根本沒有 diagnostics 欄位。
    log.write_text(json.dumps(_legacy_card()) + "\n", encoding="utf-8")
    assert manager._terminal_parse_diagnostics(_card_job(log)).model_diagnostics == ()

    # diagnostics 不是物件。
    log.write_text(
        json.dumps({**REAL_ENVELOPE, "diagnostics": "壞掉了"}) + "\n", encoding="utf-8"
    )
    assert manager._terminal_parse_diagnostics(_card_job(log)).model_diagnostics == ()

    # 巢狀值落成 canonical JSON，空值與空 key 略去。
    log.write_text(
        json.dumps(
            {
                **REAL_ENVELOPE,
                "diagnostics": {"nested": {"b": 2, "a": 1}, "blank": "  ", "": "x"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rows = dict(manager._terminal_parse_diagnostics(_card_job(log)).model_diagnostics)
    assert rows == {"nested": '{"a": 1, "b": 2}'}


# --------------------------------------------------------------------------
# 端到端：明示停止的落地與 schema retry 額度
# --------------------------------------------------------------------------


def _workflow_fixture(tmp_path: Path):
    """比照 `test_terminal_result_contract` 的 R3 端到端骨架建一個 build phase run。"""

    from paulsha_cortex.deck.compile import compile_combo
    from paulsha_cortex.deck.schema import (
        DEFAULT_CARDS_PATH,
        DEFAULT_COMBOS_DIR,
        load_cards,
        load_combo,
    )

    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    manifest = compile_combo(
        combo, cards, "explicit stop", change="explicit-stop"
    ).workflow_manifest
    assert manifest is not None
    steps = tuple(
        _replace(
            step,
            executor="codex",
            model="gpt-primary",
            domain="openai",
            gate_result="passed" if step.card == "worktree-isolation" else step.gate_result,
        )
        for step in manifest.steps
    )
    for ref in (
        "docs/superpowers/plans/explicit-stop.md",
        "docs/superpowers/specs/explicit-stop-spec.md",
        "docs/superpowers/specs/explicit-stop-design.md",
    ):
        doc = tmp_path / ref
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# explicit-stop\n", encoding="utf-8")
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="explicit-stop",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#717",),
        openspec_refs=("explicit-stop",),
        pr_refs=(),
        attempts={"build": 1},
        facets=(),
        gate_status="running",
    )

    class Launcher:
        def as_commit_required(self):
            return self

        def launch(self, *, slice_id, prompt, worktree, log_dir):
            return LaunchHandle(
                executor="codex",
                model_id="gpt-primary",
                session_name=slice_id,
                pid=100,
                log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
            )

    class ResumeDispatcher:
        _registry = registry
        _git_runner = None
        _worktree_creator = StubWorktreeCreator(tmp_path)

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    identities = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex",
                "model_id": "gpt-primary",
                "independence_domain": "openai",
                "capabilities": ["build"],
            }
        ]
    )

    seeded = registry.create_job(
        task="wf-tdd-red",
        persona="builder",
        branch="feature/717-explicit-stop",
        pane="",
        worktree=str(tmp_path),
        dispatch_head="b" * 40,
        executor="codex",
        model_id="gpt-primary",
        independence_domain="openai",
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card="tdd-red",
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_input_root=str(tmp_path),
        source_revision=run.source_revision,
    )
    registry.attach_launch_handle(seeded["job_id"], log_path=str(tmp_path / "seed.jsonl"))
    return registry, run, ResumeDispatcher, Launcher, identities, seeded


def _resume(dispatcher_cls, run, launcher_cls, identities, tmp_path: Path):
    return manager.resume_workflow_run(
        dispatcher_cls(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _: launcher_cls(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )


def _terminalize_as(registry, job_id: str, payload: dict[str, object]) -> None:
    path = Path(registry.get_job(job_id)["log_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    registry.update_headless_result(job_id, status="exited", exit_code=0)


def test_explicit_stop_lands_model_diagnostics_and_spends_no_retry(tmp_path: Path) -> None:
    """驗收兩條：attention 的 `D=` 逐字含模型病因，且**不消耗** schema retry 額度。"""

    registry, run, dispatcher_cls, launcher_cls, identities, seeded = _workflow_fixture(
        tmp_path
    )
    _terminalize_as(
        registry,
        seeded["job_id"],
        {
            **REAL_ENVELOPE,
            "run_id": run.run_id,
            "card_id": "tdd-red",
        },
    )

    result = _resume(dispatcher_cls, run, launcher_cls, identities, tmp_path)

    assert result["reason"] == "card-terminal-explicit-stop"
    assert result["declared_status"] == "needs_human"
    assert result["job_id"] == seeded["job_id"]

    # 驗收 1：attention 的 D= 逐字含模型 diagnostics 的內容。
    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets
    reason = dict(persisted.needs_human_reason)
    assert reason["reason"] == "card-terminal-explicit-stop"
    assert REAL_FAILURE_TEXT in reason["detail"]
    assert REAL_ERROR_TEXT in reason["detail"]
    # 「不符契約」那句廢話不得再是 operator 唯一看得到的東西。
    assert "did not satisfy the result contract" not in reason["detail"]

    # 驗收 2：不消耗 schema retry 額度——那是給「模型寫壞 JSON」的。
    assert manager._schema_retry_attempt_key("tdd-red") not in persisted.attempts
    assert result.get("schema_retry_count") is None
    # 也不得自動回派：模型已經講清楚它要人。
    assert len(
        [job for job in registry.list_jobs() if job.get("workflow_run_id") == run.run_id]
    ) == 1

    # R4／D6 不變：診斷不授權，candidate 沒有因此被綁上去。
    observed = result["terminal_diagnostics"]
    assert observed["authority_granted"] is False
    assert observed["model_diagnostics"]["failure"] == REAL_FAILURE_TEXT
    assert persisted.candidate_head is None


def test_schema_retry_exhausted_attention_carries_model_diagnostics(
    tmp_path: Path,
) -> None:
    """真的形狀壞掉時，只要 envelope 帶得動 diagnostics 就一併帶進 attention。"""

    registry, run, dispatcher_cls, launcher_cls, identities, seeded = _workflow_fixture(
        tmp_path
    )
    # 綁定對不上（run_id 是別人的）⇒ 真的 schema mismatch，但 diagnostics 讀得到。
    malformed = {**REAL_ENVELOPE, "card_id": "tdd-red"}

    _terminalize_as(registry, seeded["job_id"], malformed)
    reasons: list[str] = []
    for _ in range(tc.MAX_SCHEMA_RETRIES + 1):
        result = _resume(dispatcher_cls, run, launcher_cls, identities, tmp_path)
        reasons.append(result["reason"])
        if result["reason"] != "card-terminal-malformed-retry":
            break
        _terminalize_as(registry, result["job_id"], malformed)

    assert reasons[-1] == "card-terminal-schema-retry-exhausted"
    reason = dict(registry.get_workflow_run(run.run_id).needs_human_reason)
    assert reason["reason"] == "card-terminal-schema-retry-exhausted"
    assert REAL_FAILURE_TEXT in reason["detail"]
    assert REAL_ERROR_TEXT in reason["detail"]


def test_retry_card_grants_a_fresh_schema_retry_round(tmp_path: Path) -> None:
    """#717 追加觀察 (i)(ii)：`retry-card` 重新給一輪額度，兩個數字各自正確。"""

    registry, run, dispatcher_cls, launcher_cls, identities, seeded = _workflow_fixture(
        tmp_path
    )
    malformed = {**REAL_ENVELOPE, "card_id": "tdd-red"}

    _terminalize_as(registry, seeded["job_id"], malformed)
    last = None
    for _ in range(tc.MAX_SCHEMA_RETRIES + 1):
        last = _resume(dispatcher_cls, run, launcher_cls, identities, tmp_path)
        if last["reason"] != "card-terminal-malformed-retry":
            break
        _terminalize_as(registry, last["job_id"], malformed)

    assert last["reason"] == "card-terminal-schema-retry-exhausted"
    exhausted = registry.get_workflow_run(run.run_id)
    retry_key = manager._schema_retry_attempt_key("tdd-red")
    total_key = manager._schema_mismatch_total_key("tdd-red")
    assert exhausted.attempts[retry_key] == tc.MAX_SCHEMA_RETRIES
    # 累計鍵在 `retry-card` 之前還沒被寫過（本輪就是全部）。
    assert total_key not in exhausted.attempts
    # (ii)：兩個數字各自出現，不共用同一個 (n/N)。
    detail = dict(exhausted.needs_human_reason)["detail"]
    assert f"本輪 {tc.MAX_SCHEMA_RETRIES}/{tc.MAX_SCHEMA_RETRIES}" in detail
    assert f"該卡累計 {tc.MAX_SCHEMA_RETRIES} 次" in detail

    # (i)：operator 的顯式重派＝重新給一輪額度。
    after = registry._manager_reset_workflow_for_retry_card(
        run.run_id,
        expected_run_id=run.run_id,
        card="tdd-red",
    )
    assert retry_key not in after.attempts
    assert after.attempts[total_key] == tc.MAX_SCHEMA_RETRIES
    assert after.attempts["build"] == exhausted.attempts.get("build", 0) + 1

    # 重派後真的又有一輪：這一輪的第一顆壞 terminal 走的是 retry，而不是像修正前
    # 那樣「一次自動重試都沒有」就直接判 exhausted。
    fresh = _resume(dispatcher_cls, after, launcher_cls, identities, tmp_path)
    assert fresh["reason"] == "card-terminal-malformed-retry"
    assert fresh["schema_retry_count"] == 1
    # 累計則跨世代單調遞增，供成本診斷與 #555 之後的熔斷接手。
    assert fresh["schema_mismatch_observed"] == tc.MAX_SCHEMA_RETRIES + 1
