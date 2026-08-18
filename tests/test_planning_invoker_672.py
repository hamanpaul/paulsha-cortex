"""issue #683（#672 票 B）：`PlanningInvoker` 抽象的回歸釘子。

本票是**純重構**：planning 的執行方式抽出一層 `PlanningInvoker`，讓票 E
（#686）能把 in-process `subprocess.run` 換成降權 job，而不必再碰四個 adapter
與 probe。因此這裡的每一條測試都在釘「切面切在哪裡」，而不是釘新行為：

- 十條防線（design D2）在 direct 模式下**逐條仍由 `InProcessPlanningInvoker`
  保證**——sandbox 契約與 operator drift 收容是其中最容易在搬家途中掉件的兩條，
  因為它們活在 `finally` 裡、正常路徑不會經過。
- 選擇點只有 `job_runner.resolve_runner_mode()` 一個（design D1）。第二個開關的
  失效模式是「以為降權了、其實沒有」，而那種失敗**看起來是成功的**。
- `subprocess.run` 在 `planning_runtime.py` 內只剩一處（issue #683 驗收第二條）。

行為零改變的主證據不在本檔，而在「既有 planning／probe 測試一行不改全綠」——
本檔只補既有測試涵蓋不到的**結構**斷言。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import job_runner, planning_runtime
from paulsha_cortex.coordinator.model_identities import AGY_MODEL_ID, IdentityRegistry, ModelIdentity


def _completed(stdout: str = "", returncode: int = 0):
    return type("Completed", (), {"stdout": stdout, "stderr": "", "returncode": returncode})()


def _module_tree() -> ast.Module:
    return ast.parse(Path(planning_runtime.__file__).read_text(encoding="utf-8"))


def _code_string_literals(tree: ast.Module) -> set[str]:
    """模組內**程式碼**用到的字串字面值（排除 docstring）。

    註解與 docstring 會談到本票刻意不做的事（例如「不新增第二個開關」），
    對它們做 substring 比對只會得到反向的答案——因此一律走 AST。
    """

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def _invocation(
    identity: ModelIdentity,
    *,
    worktree: Path,
    prompt: str = "return JSON",
    purpose: str = planning_runtime.PLANNING_PURPOSE_PROBE,
    timeout_seconds: int = 30,
    evidence_root: Path | None = None,
    run_id: str = "workflow-683",
) -> planning_runtime.PlanningInvocation:
    return planning_runtime.PlanningInvocation(
        identity=identity,
        prompt=prompt,
        purpose=purpose,
        timeout_seconds=timeout_seconds,
        worktree=worktree,
        evidence_root=evidence_root,
        run_id=run_id,
    )


def test_in_process_invoker_preserves_sandbox_contract(tmp_path: Path) -> None:
    """D2 的 D-d：模型弄髒自己的拋棄式 sandbox ⇒ 該次呼叫 fail-closed。

    這條防線活在 `finally` 裡，成功路徑完全不會經過它——搬家時最容易變成
    「程式還在、但沒有人再呼叫它」。訊息字面值同時被釘住：它是 `_invoke_json`
    唯一能分辨「模型違反 read-only 契約」與其他失敗的字串。
    """

    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "tracked.md").write_text("operator\n", encoding="utf-8")

    def runner(argv, **kwargs):
        # 模型透過 cwd（＝sandbox）寫檔：合法的 argv、不合法的行為。
        (Path(kwargs["cwd"]) / "leak.md").write_text("sandbox mutation\n", encoding="utf-8")
        return _completed(json.dumps({"ok": True}))

    invoker = planning_runtime.InProcessPlanningInvoker(runner)
    with pytest.raises(ValueError, match="planning launcher modified disposable read-only sandbox"):
        invoker.run(_invocation(identity, worktree=worktree))

    # operator 樹本身未被波及（sandbox 是拋棄式複本，不是 operator 本尊）。
    assert (worktree / "tracked.md").read_text(encoding="utf-8") == "operator\n"
    assert not (worktree / "leak.md").exists()


def test_in_process_invoker_preserves_operator_drift_containment(tmp_path: Path) -> None:
    """D2 的 D-e／D-f：operator 樹任何內容變化 ⇒ fail-closed ＋ 唯讀收容。

    `PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX` 是**下游分類契約**
    （`manager._is_planning_worktree_drift_failure` 唯一能依賴的部分，見 #554），
    因此逐字釘住；同時釘住「內容一個位元組都沒被改寫」與「evidence 報告落地」
    ——#507 的教訓是這條防線的補救動作曾經比它要防的傷害更貴。
    """

    identity = ModelIdentity("codex", "primary", "openai", ("planning",))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    evidence_root = tmp_path / "coordinator"
    tracked = worktree / "tracked.md"
    tracked.write_text("operator\n", encoding="utf-8")

    def runner(argv, **kwargs):
        # 經絕對路徑越界寫 operator 樹（cwd 是 sandbox，這是繞過 cwd 的寫入）。
        tracked.write_text("polluted\n", encoding="utf-8")
        return _completed(json.dumps({"ok": True}))

    invoker = planning_runtime.InProcessPlanningInvoker(runner)
    with pytest.raises(ValueError) as excinfo:
        invoker.run(_invocation(identity, worktree=worktree, evidence_root=evidence_root))

    message = str(excinfo.value)
    assert message.startswith(planning_runtime.PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX)
    # 唯讀收容：operator 的內容原地保留，不得被「還原」抹掉。
    assert tracked.read_text(encoding="utf-8") == "polluted\n"

    reports = sorted(
        (evidence_root / "evidence" / planning_runtime.PLANNING_WORKTREE_DRIFT_DIRNAME).glob(
            "*/report.json"
        )
    )
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["run_id"] == "workflow-683"
    assert report["counts"] == {"added": 0, "modified": 1, "removed": 0}
    assert report["rollback_scope_applied"] == []


def test_invoker_selection_follows_resolve_runner_mode(monkeypatch) -> None:
    """design D1：選擇點只有 `job_runner.resolve_runner_mode()` 一個。

    三件事各釘一條：

    1. `PSC_JOB_RUNNER` 是**唯一**輸入——選擇函式對它以外的 env 完全不敏感。
    2. 非法值 fail-closed（借用 `resolve_runner_mode` 既有的 `JobRunnerError`，
       不自建第二套判定）。
    3. **不存在第二個開關**：`PSC_PLANNING_INVOKER` 這類 planning 專屬旗標若被
       加進來，它的失效模式是「以為降權了、其實沒有」，而那種失敗看起來是成功的。
    """

    # 1／2：唯一輸入 ＋ 非法值 fail-closed。
    for value in (None, "", job_runner.RUNNER_DIRECT, job_runner.RUNNER_SYSTEMD_TEMPLATE):
        env = {} if value is None else {job_runner.JOB_RUNNER_ENV: value}
        assert isinstance(
            planning_runtime._select_planning_invoker(env),
            planning_runtime.InProcessPlanningInvoker,
        )
    with pytest.raises(job_runner.JobRunnerError):
        planning_runtime._select_planning_invoker({job_runner.JOB_RUNNER_ENV: "definitely-not-a-mode"})

    # 3：沒有第二個開關。`planning_runtime` 的程式碼不得自行命名任何 `PSC_*`
    #    env——env 的解讀一律外包給 `job_runner`，那裡的判定是全庫唯一一份。
    tree = _module_tree()
    assert not [name for name in _code_string_literals(tree) if name.startswith("PSC_")]
    selection_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resolve_runner_mode"
    ]
    assert len(selection_calls) == 1

    # 選擇函式只讀 `PSC_JOB_RUNNER`：其他 env 一律不影響結果。
    monkeypatch.setenv("PSC_PLANNING_INVOKER", "systemd-template")
    assert isinstance(
        planning_runtime._select_planning_invoker({}),
        planning_runtime.InProcessPlanningInvoker,
    )


def test_subprocess_run_survives_only_inside_the_in_process_invoker() -> None:
    """issue #683 驗收第二條：`planning_runtime.py` 內 `subprocess.run` 只剩一處。

    這是「接縫真的接上了」的機械證據：只要還有第二處直接 `subprocess.run`，
    票 E 換掉 invoker 之後就會留下一條仍在 Manager 行程內跑模型的暗路——而那
    正是 #672 整張母票要消滅的東西。
    """

    tree = _module_tree()
    hits = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "run"
        and isinstance(node.value, ast.Name)
        and node.value.id == "subprocess"
    ]
    assert len(hits) == 1, f"預期只剩 InProcessPlanningInvoker 一處，實得行號 {hits}"

    invoker = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "InProcessPlanningInvoker"
    )
    assert invoker.lineno <= hits[0] <= (invoker.end_lineno or hits[0])


def test_every_planning_call_site_routes_through_the_invoker(monkeypatch, tmp_path: Path) -> None:
    """四個 adapter ＋ 兩種 probe 全部經同一個接縫（plan 票 B 的第三／第四條）。

    票 E 只換 invoker、不再碰 `planning_runtime` 的呼叫端——前提是**今天沒有任何
    一條 planning 路徑繞過它**。`probe_agy_capability` 是最容易漏的一條：它不吃
    prompt、吃的是兩次裸 CLI 呼叫（`agy models` ＋ smoke），過去直接拿 `runner`
    就跑，繞過了 `_invoke_json` 的全部防線。
    """

    registry = IdentityRegistry.from_rows(
        [
            {
                "executor": "codex", "model_id": "primary", "independence_domain": "openai",
                "capabilities": ["planning"],
            },
            {
                "executor": "agy", "model_id": AGY_MODEL_ID, "independence_domain": "google",
                "capabilities": ["planning"], "live_probe": "agy-plan-sandbox",
            },
        ]
    )
    monkeypatch.setattr(planning_runtime, "load_model_identities", lambda: registry)

    purposes: list[str] = []
    capability_argvs: list[list[str]] = []

    def _echo(prompt: str) -> str:
        for marker in (
            "Return only this compact JSON object and perform no tool calls: ",
            "Return only this JSON object and do not call tools: ",
        ):
            if marker in prompt:
                return prompt.split(marker, 1)[1] + "\n"
        return json.dumps({"ok": True})

    class RecordingInvoker:
        def run(self, invocation: planning_runtime.PlanningInvocation):
            purposes.append(invocation.purpose)
            return planning_runtime.PlanningOutcome(
                returncode=0, stdout=_echo(invocation.prompt), stderr="", output_text=None
            )

        def capability_probe_runner(self):
            def runner(argv, **kwargs):
                capability_argvs.append(list(argv))
                if list(argv) == ["agy", "models"]:
                    return _completed(f"{AGY_MODEL_ID}\n")
                prompt = argv[argv.index("--print") + 1]
                return _completed(_echo(prompt))

            return runner

    runtime = planning_runtime.build_production_planning_runtime(
        primary=("codex", "primary"), worktree=tmp_path, invoker=RecordingInvoker()
    )

    assert runtime.probes[("codex", "primary")].ready is True
    assert runtime.probes[("agy", AGY_MODEL_ID)].ready is True
    # agy 的 capability probe 是兩次 CLI 呼叫，各算一次 invocation。
    assert [argv[0] for argv in capability_argvs] == ["agy", "agy"]
    assert capability_argvs[0] == ["agy", "models"]

    runtime.primary_questioner({"gaps": []})
    runtime.primary_integrator({"pack_id": "qp"}, {"evidence_hash": "h"})
    assert purposes == [
        planning_runtime.PLANNING_PURPOSE_PROBE,
        planning_runtime.PLANNING_PURPOSE_QUESTIONER,
        planning_runtime.PLANNING_PURPOSE_INTEGRATOR,
    ]

    # 明示 invoker 與明示 runner 互斥：兩者同時給等於有兩個真相，fail-closed。
    with pytest.raises(ValueError, match="mutually exclusive"):
        planning_runtime.build_production_planning_runtime(
            primary=("codex", "primary"),
            worktree=tmp_path,
            runner=lambda *a, **k: _completed("{}"),
            invoker=RecordingInvoker(),
        )
