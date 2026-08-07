"""#261：terminal/result contract 必須誠實表達 gate failure。

對應 docs/superpowers/specs/terminal-result-contract-spec.md 的 R1~R4。
每個測試都刻意造出「錯的東西可能被放過去」的情境，斷言 fail closed 與矛盾原因，
而不是只斷言欄位存在。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import terminal_contract as tc


CANONICAL_CARD = {
    "schema_version": 2,
    "kind": "workflow-card",
    "status": "passed",
    "run_id": "run-1",
    "card_id": "tdd-red",
    "candidate": "a" * 40,
    "outputs": [],
    "diagnostics": {},
    "gate_evidence": [],
}


def _card(**overrides: object) -> dict[str, object]:
    return {**CANONICAL_CARD, **overrides}


def _write_ledger(
    log_path: Path,
    *,
    gates: list[dict[str, object]],
) -> str:
    """寫入 manager 側的 gate ledger（模擬 wrapper 在模型結束後產生的那份）。"""

    path = tc.gate_ledger_path(log_path)
    payload = {
        "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
        "kind": "workflow-gate-ledger",
        "slice_id": "slice",
        "gates": gates,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return tc.gate_ledger_digest(payload)
# --------------------------------------------------------------------------
# R2 passed 必須有 gate evidence 背書
# --------------------------------------------------------------------------


def test_passed_without_gate_evidence_is_rejected(tmp_path: Path) -> None:
    """R2：會跑 gate 的 phase 宣稱 passed 但沒有 manager 產生的 ledger → fail closed。"""

    log = tmp_path / "job.jsonl"
    envelope = tc.validate_envelope(_card(status="passed", gate_evidence=[]))

    with pytest.raises(tc.TerminalContractError) as excinfo:
        tc.authorize_terminal(
            envelope, ledger_path=tc.gate_ledger_path(log), require_ledger=True
        )
    assert excinfo.value.reason == "gate-evidence-missing"
    # 保留可操作原因：operator 要看得出是哪一份 ledger 沒生出來。
    assert "job.gates.json" in str(excinfo.value)

    # 對稱性：failed／needs_human 不需要 gate evidence，才不會逼模型只能回成功形狀。
    for status in ("failed", "needs_human"):
        stopped = tc.authorize_terminal(
            tc.validate_envelope(_card(status=status, gate_evidence=[])),
            ledger_path=tc.gate_ledger_path(log),
            require_ledger=True,
        )
        assert stopped.authorized is False
        assert stopped.status == status

    # 模型自己在 worktree 裡偽造一份「ledger 長相」的檔案也沒用：manager 讀的是
    # 由 log_path 推導、模型拿不到的路徑，偽造檔不在那個位置。
    (tmp_path / "gates.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-gate-ledger",
                "gates": [{"name": "pytest", "status": "passed", "exit_code": 0}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(tc.TerminalContractError) as forged:
        tc.authorize_terminal(
            envelope, ledger_path=tc.gate_ledger_path(log), require_ledger=True
        )
    assert forged.value.reason == "gate-evidence-missing"


def test_passed_contradicting_failed_gate_is_rejected(tmp_path: Path) -> None:
    """R2：確定性 gate 已失敗而 terminal 自稱 passed 時 fail closed，並保留矛盾原因。"""

    log = tmp_path / "job.jsonl"
    _write_ledger(
        log,
        gates=[
            {"name": "openspec", "status": "passed", "exit_code": 0},
            {"name": "pytest", "status": "failed", "exit_code": 1, "detail": "3 failed"},
        ],
    )
    envelope = tc.validate_envelope(
        _card(
            status="passed",
            gate_evidence=[
                {"name": "openspec", "status": "passed"},
                # 模型自述 pytest 通過——但 manager 獨立產生的 ledger 記的是 failed。
                {"name": "pytest", "status": "passed"},
            ],
        )
    )
    with pytest.raises(tc.GateContradictionError) as excinfo:
        tc.authorize_terminal(
            envelope, ledger_path=tc.gate_ledger_path(log), require_ledger=True
        )
    err = excinfo.value
    assert err.gate == "pytest"
    assert err.expected == "passed"
    assert err.actual == "failed"
    message = str(err)
    assert "pytest" in message and "passed" in message and "failed" in message
    # 矛盾原因必須是 machine-readable，才能進 status surface。
    assert err.errors[0]["gate"] == "pytest"
    assert err.errors[0]["actual"] == "failed"
    assert "3 failed" in err.errors[0]["detail"]

    # 「沒提到」不能當作「沒失敗」：terminal 完全不引用失敗的 gate 也一樣被否決。
    with pytest.raises(tc.GateContradictionError) as silent:
        tc.authorize_terminal(
            tc.validate_envelope(_card(status="passed", gate_evidence=[])),
            ledger_path=tc.gate_ledger_path(log),
            require_ledger=True,
        )
    assert silent.value.gate == "pytest"

    # ledger 自身矛盾（exit_code 非 0 卻標 passed）同樣不得被採信。
    inconsistent_log = tmp_path / "inconsistent.jsonl"
    _write_ledger(
        inconsistent_log,
        gates=[{"name": "policy", "status": "passed", "exit_code": 2}],
    )
    with pytest.raises(tc.GateContradictionError) as excinfo2:
        tc.authorize_terminal(
            tc.validate_envelope(
                _card(
                    status="passed",
                    gate_evidence=[{"name": "policy", "status": "passed"}],
                )
            ),
            ledger_path=tc.gate_ledger_path(inconsistent_log),
            require_ledger=True,
        )
    assert excinfo2.value.gate == "policy"
    assert excinfo2.value.actual == "failed"

    # 模型宣稱跑了 ledger 中根本沒有的 gate → 自述不可信，fail closed。
    green_log = tmp_path / "green.jsonl"
    digest = _write_ledger(
        green_log,
        gates=[{"name": "pytest", "status": "passed", "exit_code": 0}],
    )
    with pytest.raises(tc.TerminalContractError) as unknown:
        tc.authorize_terminal(
            tc.validate_envelope(
                _card(
                    status="passed",
                    gate_evidence=[
                        {"name": "pytest", "status": "passed"},
                        {"name": "openspec", "status": "passed"},
                    ],
                )
            ),
            ledger_path=tc.gate_ledger_path(green_log),
            require_ledger=True,
        )
    assert unknown.value.reason == "gate-evidence-unknown-gate"

    # 正向對照：ledger 全綠且自述一致才授權，且回報 manager 重算的 digest。
    granted = tc.authorize_terminal(
        tc.validate_envelope(
            _card(
                status="passed",
                gate_evidence=[{"name": "pytest", "status": "passed"}],
            )
        ),
        ledger_path=tc.gate_ledger_path(green_log),
        require_ledger=True,
    )
    assert granted.authorized is True
    assert granted.verified_gates == ("pytest",)
    assert granted.ledger_digest == digest


def test_natural_language_and_exit_zero_do_not_authorize_success(tmp_path: Path) -> None:
    """R2：模型文字、exit code 0、無明確錯誤，三者皆不得單獨構成成功授權。"""

    log = tmp_path / "job.jsonl"
    envelope = tc.validate_envelope(
        _card(
            status="passed",
            diagnostics={
                "note": "All gates ran successfully and everything passed.",
                "exit_code": 0,
                "errors": [],
            },
            gate_evidence=[],
        )
    )
    with pytest.raises(tc.TerminalContractError) as excinfo:
        tc.authorize_terminal(
            envelope, ledger_path=tc.gate_ledger_path(log), require_ledger=True
        )
    assert excinfo.value.reason == "gate-evidence-missing"


# --------------------------------------------------------------------------
# R3 schema mismatch 為有上限的確定性失敗
# --------------------------------------------------------------------------


def test_schema_mismatch_normalizes_known_wrapper_once() -> None:
    """R3：白名單 wrapper 只 normalize 一次即成功，且不回派模型。"""

    for key in tc.WRAPPER_KEYS:
        result = tc.normalize_structured_output({key: dict(CANONICAL_CARD)})
        assert result.payload == CANONICAL_CARD
        assert result.unwrapped_key == key
        assert result.attempts == 1
        assert result.requires_model_retry is False

    # 已是 canonical → 完全不需要修復。
    passthrough = tc.normalize_structured_output(dict(CANONICAL_CARD))
    assert passthrough.attempts == 0
    assert passthrough.unwrapped_key is None

    # 巢狀雙層包裝：同一確定性 mismatch 只嘗試一次，不得遞迴剝殼。
    with pytest.raises(tc.TerminalContractError) as excinfo:
        tc.normalize_structured_output({"input": {"params": dict(CANONICAL_CARD)}})
    assert excinfo.value.reason == "wrapper-shape-unrecognized"
    assert excinfo.value.attempts == tc.MAX_NORMALIZE_ATTEMPTS


def test_unknown_wrapper_shape_terminates_with_actionable_error() -> None:
    """R3：未知形狀不得被寬鬆解析吞掉，必須終止為可操作錯誤。"""

    hidden = {"mystery_envelope": dict(CANONICAL_CARD)}
    with pytest.raises(tc.TerminalContractError) as excinfo:
        tc.normalize_structured_output(hidden)
    err = excinfo.value
    assert err.reason == "wrapper-shape-unrecognized"
    assert err.requires_model_retry is False
    assert err.validation_path == "$"
    # 可操作：錯誤要說出實際觀察到的鍵與白名單，operator 才知道要不要擴充白名單。
    assert err.errors[0]["observed_keys"] == ["mystery_envelope"]
    assert set(err.errors[0]["allowed_wrapper_keys"]) == set(tc.WRAPPER_KEYS)

    # 深層藏匿也不得被撈出來（寬鬆解析會把契約破口變成安靜的錯誤資料）。
    with pytest.raises(tc.TerminalContractError):
        tc.normalize_structured_output({"a": {"b": dict(CANONICAL_CARD)}})

    # 白名單鍵但內容不是 canonical → 一樣終止，不得回傳半成品。
    with pytest.raises(tc.TerminalContractError) as excinfo2:
        tc.normalize_structured_output({"input": {"foo": 1}})
    assert excinfo2.value.validation_path == "$.input"


def test_schema_retry_has_bounded_counter() -> None:
    """R3：同一確定性 mismatch 的 retry 有上限與計數器，且可從 status surface 觀察。"""

    ledger = tc.SchemaRetryLedger()
    signature = "$.input|wrapper-shape-unrecognized"
    counts = [
        ledger.record(signature, validation_path="$.input", reason="wrapper-shape-unrecognized")
        for _ in range(tc.MAX_SCHEMA_RETRIES + 3)
    ]
    # 計數單調遞增但被上限夾住，不會無限成長。
    assert counts[: tc.MAX_SCHEMA_RETRIES] == list(range(1, tc.MAX_SCHEMA_RETRIES + 1))
    assert max(counts) == tc.MAX_SCHEMA_RETRIES
    assert ledger.exhausted(signature) is True

    # 不同的確定性 mismatch 各自獨立計數，不被前一種耗盡。
    other = "$|payload-not-object"
    assert ledger.exhausted(other) is False
    ledger.record(other, validation_path="$", reason="payload-not-object")
    assert ledger.exhausted(other) is False

    fields = ledger.status_fields()
    assert fields["schema_retry_count"] == tc.MAX_SCHEMA_RETRIES + 1
    assert fields["schema_retry_limit"] == tc.MAX_SCHEMA_RETRIES
    assert fields["last_validation_path"] == "$"
    assert fields["last_validation_reason"] == "payload-not-object"
    assert fields["schema_retry_exhausted"] is True


# --------------------------------------------------------------------------
# R4 診斷不因 parse 失敗而遺失，也不因此授予 authority
# --------------------------------------------------------------------------


def test_parse_failure_keeps_diagnostics_without_authority() -> None:
    """R4：parse 失敗保留唯讀診斷，但不得授予 candidate authority。"""

    diagnostics = tc.TerminalDiagnostics(
        job_id="job-7",
        observed_head="c" * 40,
        reason="workflow terminal log has no JSON evidence",
        validation_path="$",
    )
    payload = diagnostics.as_dict()
    assert payload["job_id"] == "job-7"
    assert payload["observed_head"] == "c" * 40
    assert payload["reason"] == "workflow terminal log has no JSON evidence"
    assert payload["validation_path"] == "$"

    # 可觀測 ≠ 可授權：診斷欄位與授權欄位分離儲存（D6）。
    assert payload["authority_granted"] is False
    assert payload["kind"] == "terminal-parse-diagnostics"
    for authority_field in ("candidate", "candidate_head", "verified_head", "authority"):
        assert authority_field not in payload

    # observed_head 不得被誤讀為 candidate authority。
    assert diagnostics.candidate_authority() is None


# --------------------------------------------------------------------------
# manager 整合：契約必須真的接在 harvest 上，否則 fail-open 依然存在
# --------------------------------------------------------------------------


def _build_card_job(
    registry,
    tmp_path: Path,
    *,
    log: Path,
    run_id="run",
    card_id="card",
    outputs: tuple[str, ...] = ("reports/build/work.md",),
):
    job = registry.create_job(
        task="build",
        persona="builder",
        branch="feature/work",
        pane="",
        worktree=str(tmp_path),
        executor="codex",
        model_id="builder",
        independence_domain="openai",
        subject_head="d" * 40,
        workflow_run_id=run_id,
        workflow_claim_key="claim",
        workflow_repo="owner/repo",
        workflow_card=card_id,
        workflow_phase="build",
        workflow_repo_root=str(tmp_path),
        workflow_outputs=outputs,
        source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    return job


def test_manager_harvest_fails_closed_on_passed_contradicting_gate_ledger(tmp_path: Path) -> None:
    """R2 端到端：gate ledger 記錄 pytest 失敗而 terminal 自稱 passed → harvest fail closed。"""

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.registry import JobRegistry

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    log = tmp_path / "build.jsonl"
    log.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": "run",
                "card_id": "card",
                "candidate": "a" * 40,
                "outputs": ["reports/build/work.md"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    job = _build_card_job(registry, tmp_path, log=log)
    _write_ledger(
        log,
        gates=[{"name": "pytest", "status": "failed", "exit_code": 1, "detail": "3 failed"}],
    )

    with pytest.raises(ValueError) as excinfo:
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    message = str(excinfo.value)
    # 保留矛盾的具體原因：哪一個 gate、期望值、實際值。
    assert "pytest" in message and "failed" in message
    # fail closed：未綁定任何 evidence，candidate 未取得 authority。
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None
    assert not (tmp_path / "evidence" / "workflow").exists()


def test_manager_harvest_accepts_passed_backed_by_verified_gate_ledger(tmp_path: Path) -> None:
    """R2 正向對照：ledger 全數通過時不得因新規則誤殺合法輸出。"""

    from paulsha_cortex.coordinator import manager

    log = tmp_path / "build.jsonl"
    log.write_text("", encoding="utf-8")
    _write_ledger(log, gates=[{"name": "pytest", "status": "passed", "exit_code": 0}])
    raw = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    # 不應拋出任何例外。
    manager._assert_terminal_gate_consistency(
        raw, job={"log_path": str(log), "workflow_phase": "build"}
    )


def test_manager_harvest_fails_closed_when_gate_ledger_absent(tmp_path: Path) -> None:
    """R2：build phase 沒有 ledger（wrapper 的 gate 階段沒跑完）→ passed 不得放行。"""

    from paulsha_cortex.coordinator import manager

    log = tmp_path / "build.jsonl"
    log.write_text("", encoding="utf-8")
    raw = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": "a" * 40,
        "outputs": [],
    }
    with pytest.raises(tc.TerminalContractError) as excinfo:
        manager._assert_terminal_gate_consistency(
            raw, job={"log_path": str(log), "workflow_phase": "build"}
        )
    assert excinfo.value.reason == "gate-evidence-missing"

    # plan card 不跑 gate，不受此要求約束（否則會誤殺純規劃的 card）。
    manager._assert_terminal_gate_consistency(
        raw, job={"log_path": str(log), "workflow_phase": "plan"}
    )


def test_manager_terminal_json_unwraps_known_wrapper_but_not_unknown(tmp_path: Path) -> None:
    """R3 端到端：白名單 wrapper 可被解出；未知形狀不得被寬鬆解析吞掉。"""

    from paulsha_cortex.coordinator import manager

    canonical = {
        "schema_version": 1,
        "kind": "workflow-card",
        "status": "passed",
        "run_id": "run",
        "card_id": "card",
        "candidate": None,
        "outputs": [],
    }
    wrapped = tmp_path / "wrapped.jsonl"
    wrapped.write_text(json.dumps({"input": canonical}) + "\n", encoding="utf-8")
    assert manager._extract_terminal_json(str(wrapped)) == canonical

    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text(json.dumps({"mystery_envelope": canonical}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        manager._extract_terminal_json(str(unknown))


def test_manager_parse_failure_keeps_diagnostics_without_authority(tmp_path: Path) -> None:
    """R4 端到端：terminal 無法解析時保留 observed HEAD／job id／reason，且不授權。"""

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.registry import JobRegistry

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    log = tmp_path / "garbage.jsonl"
    log.write_text("not json at all\n", encoding="utf-8")
    job = _build_card_job(registry, tmp_path, log=log)

    diagnostics = manager._terminal_parse_diagnostics(registry.get_job(job["job_id"]))
    payload = diagnostics.as_dict()
    assert payload["job_id"] == job["job_id"]
    assert payload["observed_head"] == "d" * 40
    assert payload["reason"]
    assert payload["authority_granted"] is False
    # 可觀測 ≠ 可授權：診斷不得帶出 candidate authority。
    assert diagnostics.candidate_authority() is None
    assert "candidate" not in payload


def test_resume_bounds_schema_mismatch_retry_with_observable_counter(tmp_path: Path) -> None:
    """R3 端到端：同一確定性 schema mismatch 不得無限回派模型；計數可被觀察。"""

    from dataclasses import replace as _replace

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.launcher import LaunchHandle
    from paulsha_cortex.coordinator.model_identities import IdentityRegistry
    from paulsha_cortex.coordinator.registry import JobRegistry
    from paulsha_cortex.deck.compile import compile_combo
    from paulsha_cortex.deck.schema import (
        DEFAULT_CARDS_PATH,
        DEFAULT_COMBOS_DIR,
        load_cards,
        load_combo,
    )

    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    manifest = compile_combo(combo, cards, "retry bound", change="retry-bound").workflow_manifest
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
        "docs/superpowers/plans/retry-bound.md",
        "docs/superpowers/specs/retry-bound-spec.md",
        "docs/superpowers/specs/retry-bound-design.md",
    ):
        doc = tmp_path / ref
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("# retry-bound\n", encoding="utf-8")
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="retry-bound",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=("hamanpaul/paulsha-cortex#261",),
        openspec_refs=("retry-bound",),
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

        def poll_headless_done(self, job_id):
            return registry.get_job(job_id)

    identities = IdentityRegistry.from_rows([{
        "executor": "codex",
        "model_id": "gpt-primary",
        "independence_domain": "openai",
        "capabilities": ["build"],
    }])

    def _malform(job_id: str) -> None:
        """讓 job 以「同一個確定性 schema mismatch」終止。"""

        path = Path(registry.get_job(job_id)["log_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"unrecognised_wrapper": {"status": "passed"}}) + "\n", encoding="utf-8")
        registry.update_headless_result(job_id, status="exited", exit_code=0)

    seeded = registry.create_job(
        task="wf-tdd-red",
        persona="builder",
        branch="feature/261-retry-bound",
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
    _malform(seeded["job_id"])

    reasons: list[str] = []
    counts: list[int] = []
    for _ in range(tc.MAX_SCHEMA_RETRIES + 2):
        result = manager.resume_workflow_run(
            ResumeDispatcher(),
            run_id=run.run_id,
            identities=identities,
            launcher_factory=lambda _: Launcher(),
            coordinator_root=tmp_path / "coordinator",
            operator_resume=True,
        )
        reasons.append(result["reason"])
        counts.append(result.get("schema_retry_count"))
        if result["reason"] != "card-terminal-malformed-retry":
            break
        _malform(result["job_id"])

    # 重試次數被上限夾住，最後以可操作的終止理由收尾，而不是無限 retry storm。
    assert reasons.count("card-terminal-malformed-retry") == tc.MAX_SCHEMA_RETRIES
    assert reasons[-1] == "card-terminal-schema-retry-exhausted"
    assert counts[: tc.MAX_SCHEMA_RETRIES] == list(range(1, tc.MAX_SCHEMA_RETRIES + 1))

    # status surface 看得到 validation path／reason 與上限。
    final = manager.resume_workflow_run(
        ResumeDispatcher(),
        run_id=run.run_id,
        identities=identities,
        launcher_factory=lambda _: Launcher(),
        coordinator_root=tmp_path / "coordinator",
        operator_resume=True,
    )
    assert final["reason"] == "card-terminal-schema-retry-exhausted"
    assert final["schema_retry_limit"] == tc.MAX_SCHEMA_RETRIES
    assert final["last_validation_path"] == "$"
    assert final["last_validation_reason"]
    # R4：診斷與授權分離——status surface 拿得到 observed HEAD／job id／reason，
    # 但同一份 payload 不得帶出任何 authority。
    observed = final["terminal_diagnostics"]
    assert observed["job_id"] == final["job_id"]
    assert observed["reason"]
    assert observed["authority_granted"] is False
    assert "candidate" not in observed
    assert registry.get_workflow_run(run.run_id).candidate_head is None
    # 逾限後轉需人工介入，且計數持久化在 run.attempts 上可被觀察。
    persisted = registry.get_workflow_run(run.run_id)
    assert "needs_human" in persisted.facets
    assert persisted.attempts[manager._schema_retry_attempt_key("tdd-red")] == tc.MAX_SCHEMA_RETRIES


@pytest.mark.parametrize("status", ["failed", "needs_human"])
def test_verify_card_can_report_non_passing_without_being_called_malformed(
    tmp_path: Path, status: str
) -> None:
    """R1：verifier 不得只有成功形狀合法；非通過狀態要 fail closed 為可操作錯誤。"""

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.registry import JobRegistry

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    log = tmp_path / "verify.jsonl"
    log.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-verification-result",
                "status": status,
                "summary": "pytest 失敗，無法宣稱通過",
                "details": {"pytest": "3 failed"},
                "reports": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    job = registry.create_job(
        task="verify", persona="reviewer", kind="review", branch="feature/work",
        pane="", worktree=str(tmp_path), executor="claude", model_id="reviewer",
        independence_domain="anthropic", subject_head="a" * 40,
        workflow_run_id="run", workflow_claim_key="claim", workflow_repo="owner/repo",
        workflow_card="card", workflow_phase="verify", workflow_repo_root=str(tmp_path),
        workflow_outputs=("reports/verify/work.md",), source_revision="rev",
    )
    registry.attach_launch_handle(job["job_id"], log_path=str(log))
    registry.update_headless_result(job["job_id"], status="exited", exit_code=0)

    with pytest.raises(ValueError) as excinfo:
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    # 訊息必須指出「回報了非通過狀態」，而不是把誠實的失敗誤判成 schema 壞掉。
    message = str(excinfo.value)
    assert "non-passing status" in message and status in message
    # fail closed：不綁 evidence、不授權。
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None
    assert not (tmp_path / "evidence" / "workflow").exists()


def test_prompt_contract_matches_what_manager_enforces(tmp_path: Path) -> None:
    """#261：派給模型的 terminal_schema 必須就是 manager 實際驗的那份契約。

    契約文件與實作漂移是這張票的原始成因之一（模型被教成只能回成功形狀）。
    這裡直接拿 prompt 宣告的 schema 造一份 terminal，斷言它能通過 harvest。
    """

    from dataclasses import replace as _replace

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.registry import JobRegistry
    from paulsha_cortex.deck.compile import compile_combo
    from paulsha_cortex.deck.schema import (
        DEFAULT_CARDS_PATH,
        DEFAULT_COMBOS_DIR,
        load_cards,
        load_combo,
    )

    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    manifest = compile_combo(combo, cards, "contract sync", change="contract-sync").workflow_manifest
    assert manifest is not None
    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run = registry._manager_create_workflow_run(
        work_id="contract-sync",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=tuple(
            _replace(step, executor="codex", model="gpt-primary", domain="openai")
            for step in manifest.steps
        ),
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={},
        facets=(),
        gate_status="running",
    )
    step = next(item for item in run.steps if item.phase == "build" and not item.outputs)
    prompt = manager._workflow_job_prompt(
        run, step, builder_job_id=None, coordinator_root=tmp_path
    )
    schema = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])["terminal_schema"]

    # prompt 宣告的就是 canonical envelope，且三種終局狀態俱在。
    assert schema["schema_version"] == tc.TERMINAL_SCHEMA_VERSION
    assert set(schema["status"]) == set(tc.TERMINAL_STATUSES)
    assert {"diagnostics", "gate_evidence"} <= set(schema["required"])

    # 依 prompt 宣告的 required 欄位造一份 terminal，harvest 必須認得。
    terminal = {
        "schema_version": schema["schema_version"],
        "kind": "workflow-card",
        "status": "passed",
        "run_id": run.run_id,
        "card_id": step.card,
        "candidate": "a" * 40,
        "outputs": [],
        "diagnostics": {"gates": "all green"},
        "gate_evidence": [{"name": "pytest", "status": "passed"}],
    }
    assert set(terminal) == set(schema["required"])

    log = tmp_path / "build.jsonl"
    log.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
    _write_ledger(log, gates=[{"name": "pytest", "status": "passed", "exit_code": 0}])
    job = _build_card_job(registry, tmp_path, log=log, run_id=run.run_id, card_id=step.card)

    # cross-check 放行（不得因為 v2 多兩個欄位就被判 malformed）。
    manager._assert_terminal_gate_consistency(raw=terminal, job=registry.get_job(job["job_id"]))
    assert manager._malformed_workflow_card_terminal(registry.get_job(job["job_id"])) is False

    # 同一份 v2 terminal 若 ledger 說 pytest 失敗，則 fail closed。
    _write_ledger(log, gates=[{"name": "pytest", "status": "failed", "exit_code": 1}])
    with pytest.raises(tc.GateContradictionError) as excinfo:
        manager._assert_terminal_gate_consistency(
            raw=terminal, job=registry.get_job(job["job_id"])
        )
    assert excinfo.value.gate == "pytest"


# --------------------------------------------------------------------------
# #307：test_policy=red-required 卡與 R2 gate 一致性檢查的語意反轉
# --------------------------------------------------------------------------
#
# tdd-red 卡（execution.test_policy=red-required）的正確產出是「新增並 commit
# 會失敗的 RED regression test」。宣告 PSC_GATE_CMD_PYTEST 時，這張卡的 pytest
# gate *理應* failed——R2 的一般 fail-closed 規則會把這個預期中的 failed 誤判為
# 與 terminal 自稱 passed 矛盾，結構性地讓 red-required 卡永遠不可能通過。
#
# 語意反轉只精準命中 `RED_REQUIRED_TEST_GATE_NAME`（"pytest"）這一項 ledger 結
# 果，且只在 exit_code 精確等於 1（pytest 的 TESTS_FAILED：測試被收集、確實執
# 行，且至少一個失敗）時才視為合格 RED；其餘 exit_code（0＝全綠、2/3/4/5＝
# collection error／interrupted／internal error／usage error／no tests
# collected）一律維持 failed，避免「builder 根本沒寫測試」或「測試檔壞掉」被誤
# 判為合格 RED。一般卡（test_policy 非 red-required）完全不受影響。


def test_red_required_gate_failed_as_expected_is_authorized(tmp_path: Path) -> None:
    """(a) red-required 卡＋pytest gate 如預期 failed（exit_code=1）→ 應該通過。"""

    log = tmp_path / "job.jsonl"
    _write_ledger(
        log,
        gates=[
            {"name": "openspec", "status": "passed", "exit_code": 0},
            {"name": "pytest", "status": "failed", "exit_code": 1, "detail": "1 failed"},
        ],
    )
    envelope = tc.validate_envelope(
        _card(
            status="passed",
            gate_evidence=[
                {"name": "openspec", "status": "passed"},
                # 模型誠實自述觀察到的原始事實（RED test 確實 failed）——不要求
                # 模型自己做語意反轉；manager 端的反轉只作用在矛盾偵測那一步。
                {"name": "pytest", "status": "failed"},
            ],
        )
    )
    granted = tc.authorize_terminal(
        envelope,
        ledger_path=tc.gate_ledger_path(log),
        require_ledger=True,
        test_policy="red-required",
    )
    assert granted.authorized is True
    assert granted.status == "passed"


def test_red_required_gate_passed_green_is_rejected(tmp_path: Path) -> None:
    """(b) red-required 卡＋pytest gate passed（全綠、未產生 RED）→ 仍須 fail closed。

    red-required 的必要條件是「測試確實失敗」；全綠代表沒有交付要求的 RED
    regression test，terminal 自稱的 passed 與這個事實矛盾。
    """

    log = tmp_path / "job.jsonl"
    _write_ledger(log, gates=[{"name": "pytest", "status": "passed", "exit_code": 0}])
    envelope = tc.validate_envelope(
        _card(status="passed", gate_evidence=[{"name": "pytest", "status": "passed"}])
    )
    with pytest.raises(tc.GateContradictionError) as excinfo:
        tc.authorize_terminal(
            envelope,
            ledger_path=tc.gate_ledger_path(log),
            require_ledger=True,
            test_policy="red-required",
        )
    assert excinfo.value.gate == "pytest"
    assert excinfo.value.actual == "failed"


@pytest.mark.parametrize(
    "exit_code,label",
    [
        (2, "collection-error-or-interrupted"),
        (3, "internal-error"),
        (4, "usage-error"),
        (5, "no-tests-collected"),
    ],
)
def test_red_required_broken_test_is_never_authorized(
    tmp_path: Path, exit_code: int, label: str
) -> None:
    """red-required 卡＋pytest 非 0/1 的 exit_code（測試檔壞掉／根本沒寫測試）
    → 不得被當成合格 RED，一律維持 fail closed。"""

    log = tmp_path / f"job-{label}.jsonl"
    _write_ledger(
        log,
        gates=[{"name": "pytest", "status": "failed", "exit_code": exit_code}],
    )
    envelope = tc.validate_envelope(_card(status="passed", gate_evidence=[]))
    with pytest.raises(tc.GateContradictionError) as excinfo:
        tc.authorize_terminal(
            envelope,
            ledger_path=tc.gate_ledger_path(log),
            require_ledger=True,
            test_policy="red-required",
        )
    assert excinfo.value.gate == "pytest"
    assert excinfo.value.actual == "failed"


def test_red_required_semantics_do_not_leak_into_general_cards(tmp_path: Path) -> None:
    """(c) 一般卡（test_policy 非 red-required）＋pytest gate failed＋terminal 自稱
    passed → 必須仍 fail closed；語意反轉不得外溢到一般卡。"""

    log = tmp_path / "job.jsonl"
    _write_ledger(log, gates=[{"name": "pytest", "status": "failed", "exit_code": 1}])
    envelope = tc.validate_envelope(_card(status="passed", gate_evidence=[]))

    for test_policy in (None, "none", "focused", "full"):
        with pytest.raises(tc.GateContradictionError) as excinfo:
            tc.authorize_terminal(
                envelope,
                ledger_path=tc.gate_ledger_path(log),
                require_ledger=True,
                test_policy=test_policy,
            )
        assert excinfo.value.gate == "pytest"
        assert excinfo.value.actual == "failed"


def test_red_required_semantics_only_touch_pytest_gate(tmp_path: Path) -> None:
    """red-required 反轉只精準命中 pytest 這一項；其他 gate（例如 openspec）
    failed 時，red-required 卡仍須 fail closed，不得整張卡放行。"""

    log = tmp_path / "job.jsonl"
    _write_ledger(
        log,
        gates=[
            {"name": "pytest", "status": "failed", "exit_code": 1, "detail": "1 failed"},
            {"name": "openspec", "status": "failed", "exit_code": 1, "detail": "invalid"},
        ],
    )
    envelope = tc.validate_envelope(_card(status="passed", gate_evidence=[]))
    with pytest.raises(tc.GateContradictionError) as excinfo:
        tc.authorize_terminal(
            envelope,
            ledger_path=tc.gate_ledger_path(log),
            require_ledger=True,
            test_policy="red-required",
        )
    assert excinfo.value.gate == "openspec"


def _tdd_red_step_run(registry, tmp_path: Path):
    """建一個掛完整 manifest（含真實 tdd-red 卡）的 WorkflowRun，回傳 (run, step)。"""

    from dataclasses import replace as _replace

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
        combo, cards, "red required wiring", change="red-required-wiring"
    ).workflow_manifest
    assert manifest is not None
    run = registry._manager_create_workflow_run(
        work_id="red-required-wiring",
        repo="hamanpaul/paulsha-cortex",
        claim_key="claim:v1:" + "3" * 64,
        source_revision="4" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=tuple(
            _replace(step, executor="codex", model="gpt-primary", domain="openai")
            for step in manifest.steps
        ),
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={},
        facets=(),
        gate_status="running",
    )
    step = next(item for item in run.steps if item.card == "tdd-red")
    assert step.test_policy == "red-required"
    return run, step


def test_manager_harvest_authorizes_tdd_red_card_with_expected_red_pytest(
    tmp_path: Path,
) -> None:
    """端到端：真正的 tdd-red 卡 + PSC_GATE_CMD_PYTEST 宣告 + pytest 如預期
    failed（exit_code=1）→ terminalize_workflow_job 必須成功（issue #307 的
    原始回歸情境：W1 batch run workflow-b512cbfc7609c4b13c01 / tdd-red-416）。
    """

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.registry import JobRegistry

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run, step = _tdd_red_step_run(registry, tmp_path)

    log = tmp_path / "build.jsonl"
    log.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": step.card,
                "candidate": "a" * 40,
                "outputs": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_ledger(
        log, gates=[{"name": "pytest", "status": "failed", "exit_code": 1, "detail": "1 failed"}]
    )
    job = _build_card_job(
        registry, tmp_path, log=log, run_id=run.run_id, card_id=step.card, outputs=step.outputs
    )

    terminal = manager.terminalize_workflow_job(
        registry, job_id=job["job_id"], coordinator_root=tmp_path
    )
    assert terminal["workflow_evidence"] is not None


def test_manager_harvest_still_rejects_tdd_red_card_with_green_pytest(
    tmp_path: Path,
) -> None:
    """端到端對照：真正的 tdd-red 卡若 pytest 全綠（沒有產生 RED），
    terminalize_workflow_job 仍必須 fail closed，不得被語意反轉放行。"""

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.registry import JobRegistry

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run, step = _tdd_red_step_run(registry, tmp_path)

    log = tmp_path / "build.jsonl"
    log.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": step.card,
                "candidate": "a" * 40,
                "outputs": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_ledger(log, gates=[{"name": "pytest", "status": "passed", "exit_code": 0}])
    job = _build_card_job(
        registry, tmp_path, log=log, run_id=run.run_id, card_id=step.card, outputs=step.outputs
    )

    with pytest.raises(tc.GateContradictionError) as excinfo:
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    assert excinfo.value.gate == "pytest"
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None


def test_manager_harvest_general_build_card_unaffected_by_red_required_wiring(
    tmp_path: Path,
) -> None:
    """端到端對照：同一個 run 內的一般 build 卡（test_policy 非 red-required）
    ＋pytest failed＋terminal 自稱 passed → registry 有 test_policy 可查也仍須
    fail closed；registry 佈線本身不得弱化一般卡。"""

    from paulsha_cortex.coordinator import manager
    from paulsha_cortex.coordinator.registry import JobRegistry

    registry = JobRegistry(state_path=tmp_path / "registry.json")
    run, _tdd_red = _tdd_red_step_run(registry, tmp_path)
    general_step = next(
        item for item in run.steps if item.phase == "build" and item.card != "tdd-red"
    )
    assert general_step.test_policy != "red-required"

    log = tmp_path / "build.jsonl"
    log.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "workflow-card",
                "status": "passed",
                "run_id": run.run_id,
                "card_id": general_step.card,
                "candidate": "a" * 40,
                "outputs": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_ledger(
        log, gates=[{"name": "pytest", "status": "failed", "exit_code": 1, "detail": "1 failed"}]
    )
    job = _build_card_job(
        registry,
        tmp_path,
        log=log,
        run_id=run.run_id,
        card_id=general_step.card,
        outputs=general_step.outputs,
    )

    with pytest.raises(tc.GateContradictionError) as excinfo:
        manager.terminalize_workflow_job(
            registry, job_id=job["job_id"], coordinator_root=tmp_path
        )
    assert excinfo.value.gate == "pytest"
    assert registry.get_job(job["job_id"])["workflow_evidence"] is None
