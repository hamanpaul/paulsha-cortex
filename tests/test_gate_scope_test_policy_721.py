"""#721：派工 prompt 的 gate **適用範圍**必須與 harvest 端同源。

現場 job ``wf-6c37c77ca1-worktree-isolation-8``（2026-08-19）：``worktree-isolation``
這張卡的 ``test_policy`` 逐字是 ``"none"``，harvest 端
``terminal_contract.expected_gate_names_for_test_policy()`` 對它回 ``frozenset()``
（docstring 甚至逐字點名這張卡），dispatch 端卻從 operator 的 ``PSC_GATE_CMD_*``
全量導出，於是 prompt 告訴模型「after your process exits the Manager re-runs
exactly these commands ("pytest" = ``python3 -m pytest -q``), and a passed status is
judged against those real results」。模型照做，在 ``-s read-only`` 沙箱下死於
``No usable temporary directory available``，terminal 回 ``failed``，Manager 依
``_retryable_nonpassing_workflow_terminal`` 自動重派 ⇒ 確定性無限迴圈。

#540 把 gate **名稱**從 prompt 端手寫改成機械產生；本票是同一個錯誤的另一半——
名字機械化了，**適用範圍**沒有。

本檔釘住四件事：

1. 契約不要求模型交出 gate 結果的卡（``test_policy`` 為 ``"none"``／``None``）：
   ``allowed_names`` 為空、兩段文字都逐字要求 ``gate_evidence: []``、且不得出現
   任何逼模型自己去跑 gate 的句子。
2. ``focused``／``full``／``red-required`` 的卡：兩段文字**逐字不變**（下面的
   ``_FOCUSED_*`` 常數是修改前的原文複本，任何改動都會在這裡紅）。
3. 四種 ``test_policy`` × ``allowed_names``／``status_policy`` 兩段文字全覆蓋。
4. dispatch 端與 harvest 端對「這張卡要驗哪些 gate」**來自同一支函式**
   （``terminal_contract.expected_gate_names_for_test_policy``），且 dispatch 講的
   判定範圍**不得比 harvest 真正判的範圍窄**——後者是本票的鏡像缺陷，見
   ``test_multi_gate_card_prompt_shows_every_gate_harvest_judges``。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import gate_ledger, manager
from paulsha_cortex.coordinator import terminal_contract as tc
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


PYTEST_ENV = {"PSC_GATE_CMD_PYTEST": "python3 -m pytest -q"}
MULTI_GATE_ENV = {
    "PSC_GATE_CMD_PYTEST": "python3 -m pytest -q",
    "PSC_GATE_CMD_OPENSPEC": "openspec validate --strict",
    "PSC_GATE_CMD_POLICY": "python3 -m policy_check",
}

# --------------------------------------------------------------------------
# 有宣告 gate 時的原文複本（逐字釘住；#740 補上 environment honesty 一維——
# 判準來自 Manager 在 gate 環境的重跑，sandbox-only 失敗省略該 gate 而非自報 failed）
# --------------------------------------------------------------------------

_FOCUSED_STATUS_POLICY = (
    "Report passed only when every deterministic gate you ran (OpenSpec / pytest / "
    "policy) actually passed. Natural-language confidence, an exit code of 0, and "
    "the absence of an explicit error do NOT authorize passed. If a gate failed "
    "because of your change, report failed; the Manager re-reads the gate ledger "
    "and fails closed on any contradiction, so a dishonest passed only costs you "
    "a retry. "
    "Scope discipline: after your process exits the Manager re-runs exactly these commands "
    '("pytest" = `python3 -m pytest -q`), and a passed status is judged against those real '
    "results. Running a focused subset first is fine, but then report only the scope you "
    "actually ran: a green focused subset is NOT evidence that the declared gate is green, "
    "and inferring the full gate from it fails the card closed. Environment honesty: those "
    "judged results come from the Manager's re-run in its own gate environment, not from "
    "your sandbox — your sandbox is deliberately more restricted (no general network, "
    "hardened unit), so tests untouched by your change can fail there and nowhere else. "
    "Such sandbox-only failures do not make this card failed: leave that gate out of "
    "gate_evidence, record what you observed in diagnostics, and still deliver the "
    "candidate; claiming the gate green stays forbidden — the Manager's ledger supplies "
    "the judged result."
)

_FOCUSED_GATE_EVIDENCE_DESCRIPTION = (
    "Declare every deterministic gate you actually ran and its real result. "
    "The Manager independently re-runs the declared gate commands after your "
    "process exits and compares; claiming a gate you did not run, or claiming "
    "passed for a gate that failed, fails the card closed. "
    "The Manager's gate ledger for this card can only contain these gate names: "
    '"pytest". Every gate_evidence[].name must be one of those exact strings, copied '
    "verbatim; the Manager rejects any name that is not in the ledger, so a descriptive "
    'label of your own (for example "focused pytest RED expectation") fails the card '
    "closed. If you did not run one of them, leave it out instead of renaming it."
)

# 現場那句把模型推去跑 pytest 的話；不要求 gate 的卡不得出現任何一句。
_COERCIVE_FRAGMENTS = (
    "the Manager re-runs exactly these commands",
    "a passed status is judged against those real results",
    "Declare every deterministic gate you actually ran",
    "every deterministic gate you ran (OpenSpec / pytest / policy)",
)


_PERSONA_BY_PHASE = {"build": "builder"}


def _step(card: str, *, test_policy: str | None) -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        gate_result="pending",
        test_policy=test_policy,
    )


def _prompt_schema(
    tmp_path: Path,
    *,
    card: str,
    test_policy: str | None,
    env: dict[str, str],
    state_name: str = "registry.json",
) -> dict:
    registry = JobRegistry(state_path=tmp_path / state_name)
    run = registry._manager_create_workflow_run(
        work_id="demo",
        repo="acme/demo",
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(tmp_path),
        combo="feature-oneshot",
        current_phase="build",
        steps=(_step(card, test_policy=test_policy),),
        issue_refs=(),
        openspec_refs=(),
        pr_refs=(),
        attempts={},
        facets=(),
        gate_status="running",
    )
    prompt = manager._workflow_job_prompt(
        run,
        run.steps[0],
        builder_job_id=None,
        coordinator_root=tmp_path,
        env=env,
    )
    contract = json.loads(prompt[prompt.index("{") : prompt.rindex("}") + 1])
    return contract["terminal_schema"]


# ==========================================================================
# 段 1：test_policy=none／None 的卡
# ==========================================================================


@pytest.mark.parametrize(
    "card, test_policy",
    [
        # 現場那張卡：step 沒帶 test_policy，`_LEGACY_CARD_EXECUTION` 的 fallback
        # 逐字是 "none"。
        ("worktree-isolation", None),
        ("worktree-isolation", "none"),
        # 表上沒有的卡 + step 也沒宣告 ⇒ effective_test_policy 真的是 None。
        ("custom-readonly-card", None),
    ],
)
def test_card_without_test_policy_declares_no_gate_for_the_model(
    tmp_path: Path, card: str, test_policy: str | None
) -> None:
    """RED（修復前）：``allowed_names`` 是 operator 全量宣告，prompt 逼模型跑 pytest。"""

    schema = _prompt_schema(
        tmp_path, card=card, test_policy=test_policy, env=PYTEST_ENV
    )

    assert schema["gate_evidence"]["allowed_names"] == []

    description = schema["gate_evidence"]["description"]
    status_policy = schema["status_policy"]
    for segment in (description, status_policy):
        assert "gate_evidence must be exactly []" in segment or (
            "gate_evidence must stay exactly []" in segment
        )
        for fragment in _COERCIVE_FRAGMENTS:
            assert fragment not in segment, fragment
    # 「不要你跑」必須是明說的，不是靠沒提到。
    assert "requires no test run from you" in status_policy
    assert "you are not asked to reproduce it" in status_policy
    assert "Do not run a project test suite for it" in status_policy


def test_none_policy_card_still_discloses_the_managers_own_gate_run(
    tmp_path: Path,
) -> None:
    """「不要你跑」不等於「沒有 gate 會判你」——後者會把範圍講小。

    ``gate_runner.ensure_gate_ledger()`` 只看 phase（``build``）不看 ``test_policy``，
    所以 ``test_policy="none"`` 的 build 卡照樣會被 gate 執行身分重跑宣告的 gate；
    下面 :func:`test_harvest_still_judges_a_none_policy_card_against_the_ledger`
    釘住 harvest 端也照樣拿它 fail closed。prompt 因此必須據實揭露那一次執行，
    命令逐字由宣告機械導出。
    """

    schema = _prompt_schema(
        tmp_path, card="worktree-isolation", test_policy="none", env=PYTEST_ENV
    )
    status_policy = schema["status_policy"]

    assert "the Manager runs its own declared gate commands" in status_policy
    assert '"pytest" = `python3 -m pytest -q`' in status_policy
    assert "still fails this card closed on a non-passing result" in status_policy


def test_none_policy_card_without_any_declaration_says_so(tmp_path: Path) -> None:
    """operator 一個 gate 都沒宣告時，兩邊都沒有 gate——照實說，不要編造一次執行。"""

    schema = _prompt_schema(
        tmp_path, card="worktree-isolation", test_policy="none", env={}
    )

    assert schema["gate_evidence"]["allowed_names"] == []
    assert (
        "no deterministic gate result is expected from either side"
        in schema["status_policy"]
    )


def test_harvest_still_judges_a_none_policy_card_against_the_ledger(
    tmp_path: Path,
) -> None:
    """實機事實的回歸釘子（本票的文案就是照這條寫的）。

    實機 ledger ``wf-6c37c77ca1-worktree-isolation-3.gates.json`` 內確實有一列
    ``pytest``（``status: failed``），證明 ``test_policy="none"`` 的卡照樣被跑 gate。
    這裡釘住第二半：``authorize_terminal`` 的矛盾偵測對 ledger 裡**任何**非 passed
    的 gate 都 fail closed，與 ``test_policy`` 無關。這兩件事一旦不成立（例如未來
    改成 none 卡不跑 gate），prompt 的揭露句就該一併收掉。
    """

    log = tmp_path / "job.jsonl"
    log.write_text("", encoding="utf-8")
    ledger = tc.gate_ledger_path(log)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
                "kind": tc.GATE_LEDGER_KIND,
                "slice_id": "s",
                "gates": [
                    {"name": "pytest", "status": "failed", "exit_code": 1, "detail": ""}
                ],
            }
        ),
        encoding="utf-8",
    )
    envelope = tc.validate_envelope(
        {
            "schema_version": tc.TERMINAL_SCHEMA_VERSION,
            "kind": "workflow-card",
            "status": "passed",
            "run_id": "r",
            "card_id": "worktree-isolation",
            "candidate": "a" * 40,
            "outputs": [],
            "diagnostics": {},
            # 契約不要求 gate 結果 ⇒ 模型照 prompt 交空的 gate_evidence。
            "gate_evidence": [],
        }
    )

    assert tc.expected_gate_names_for_test_policy("none") == frozenset()
    with pytest.raises(tc.GateContradictionError):
        tc.authorize_terminal(
            envelope,
            ledger_path=ledger,
            require_ledger=True,
            test_policy="none",
            expected_gate_names=tc.expected_gate_names_for_test_policy("none"),
        )


# ==========================================================================
# 段 2：其餘三種 test_policy 逐字不變
# ==========================================================================


@pytest.mark.parametrize("test_policy", ["focused", "full", "red-required"])
def test_gate_requiring_cards_keep_both_segments_verbatim(
    tmp_path: Path, test_policy: str
) -> None:
    """回歸釘子：本票不得動到任何一張真的要跑測試的卡的文字。"""

    schema = _prompt_schema(
        tmp_path, card="tdd-red", test_policy=test_policy, env=PYTEST_ENV
    )

    assert schema["gate_evidence"]["allowed_names"] == ["pytest"]
    assert schema["status_policy"] == _FOCUSED_STATUS_POLICY
    assert schema["gate_evidence"]["description"] == _FOCUSED_GATE_EVIDENCE_DESCRIPTION


def test_multi_gate_card_prompt_shows_every_gate_harvest_judges(
    tmp_path: Path,
) -> None:
    """**本票真正要守的不變式**：dispatch 講的判定範圍 == harvest 真正判的範圍。

    ``expected_gate_names_for_test_policy()`` 回的是「**測試**這一個訊號」
    （``RED_REQUIRED_TEST_GATE_NAME``），不是「這張卡會被判哪些 gate」。若拿它去
    ∩ ledger 的 gate 名稱集合，多宣告一個 ``openspec``／``policy`` 的 operator 就會
    收到「Manager 只重跑 pytest」的 prompt，而 harvest 照樣拿 ``openspec`` 的失敗把
    ``passed`` 打掉——那是 #721 的鏡像缺陷（dispatch 講的比 harvest 小）。
    """

    schema = _prompt_schema(
        tmp_path, card="tdd-red", test_policy="focused", env=MULTI_GATE_ENV
    )

    ledger_names = list(gate_ledger.ledger_gate_names(MULTI_GATE_ENV))
    assert ledger_names == ["openspec", "policy", "pytest"]
    assert schema["gate_evidence"]["allowed_names"] == ledger_names
    for command in (
        "python3 -m pytest -q",
        "openspec validate --strict",
        "python3 -m policy_check",
    ):
        assert command in schema["status_policy"]

    # harvest 端確實會判宣告集合裡的每一個 gate，不只 expected 的那一個。
    log = tmp_path / "job.jsonl"
    log.write_text("", encoding="utf-8")
    ledger = tc.gate_ledger_path(log)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": tc.GATE_LEDGER_SCHEMA_VERSION,
                "kind": tc.GATE_LEDGER_KIND,
                "slice_id": "s",
                "gates": [
                    {"name": "pytest", "status": "passed", "exit_code": 0, "detail": ""},
                    {
                        "name": "openspec",
                        "status": "failed",
                        "exit_code": 1,
                        "detail": "",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    envelope = tc.validate_envelope(
        {
            "schema_version": tc.TERMINAL_SCHEMA_VERSION,
            "kind": "workflow-card",
            "status": "passed",
            "run_id": "r",
            "card_id": "tdd-red",
            "candidate": "a" * 40,
            "outputs": [],
            "diagnostics": {},
            "gate_evidence": [{"name": "pytest", "status": "passed"}],
        }
    )
    with pytest.raises(tc.GateContradictionError) as excinfo:
        tc.authorize_terminal(
            envelope,
            ledger_path=ledger,
            require_ledger=True,
            test_policy="focused",
            expected_gate_names=tc.expected_gate_names_for_test_policy("focused"),
        )
    assert excinfo.value.gate == "openspec"
    assert "openspec" not in tc.expected_gate_names_for_test_policy("focused")


# ==========================================================================
# 段 3：兩端同一支函式
# ==========================================================================


@pytest.mark.parametrize(
    "test_policy, requires",
    [(None, False), ("none", False), ("focused", True), ("full", True), ("red-required", True)],
)
def test_dispatch_scope_is_the_harvest_judge(test_policy: str | None, requires: bool) -> None:
    """dispatch 的範圍判準就是 harvest 的那一支，不是另算一份。"""

    assert gate_ledger.card_requires_gate_evidence(test_policy) is requires
    assert gate_ledger.card_requires_gate_evidence(test_policy) is bool(
        tc.expected_gate_names_for_test_policy(test_policy)
    )
    # harvest 端（`_assert_terminal_gate_consistency` 用的入口）指向同一個實作。
    assert manager._expected_gate_names_for_test_policy(
        test_policy
    ) == tc.expected_gate_names_for_test_policy(test_policy)


def test_dispatch_prompt_follows_the_harvest_judge_when_it_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把判準搬走，prompt 必須跟著搬——證明 dispatch 真的在呼叫那支函式。"""

    # 判準改口說 focused 不要求任何 gate ⇒ focused 卡必須收到無 gate 版本。
    monkeypatch.setattr(
        tc, "expected_gate_names_for_test_policy", lambda test_policy: frozenset()
    )
    schema = _prompt_schema(
        tmp_path, card="tdd-red", test_policy="focused", env=PYTEST_ENV
    )
    assert schema["gate_evidence"]["allowed_names"] == []
    assert "requires no test run from you" in schema["status_policy"]

    # 反向：判準改口說 none 也要求 gate ⇒ none 卡必須收到既有的完整版本。
    monkeypatch.setattr(
        tc,
        "expected_gate_names_for_test_policy",
        lambda test_policy: frozenset({tc.RED_REQUIRED_TEST_GATE_NAME}),
    )
    schema = _prompt_schema(
        tmp_path,
        card="worktree-isolation",
        test_policy="none",
        env=PYTEST_ENV,
        state_name="registry2.json",
    )
    assert schema["gate_evidence"]["allowed_names"] == ["pytest"]
    assert schema["status_policy"] == _FOCUSED_STATUS_POLICY


def test_prompt_generator_does_not_compute_a_second_scope(tmp_path: Path) -> None:
    """程式碼層的釘子：dispatch 不得再從 env 全量導出 gate 名稱。

    比照 #629 的 ``test_the_action_calls_the_shared_entry_point`` ——用原始碼確認
    唯一的導出入口，避免有人「順手」把 `ledger_gate_names(env)` 放回來。
    """

    import inspect

    source = inspect.getsource(manager._workflow_job_prompt)
    assert "gate_ledger.card_gate_names(" in source
    assert "gate_ledger.ledger_gate_names(" not in source
