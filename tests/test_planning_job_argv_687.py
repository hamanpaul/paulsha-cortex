"""issue #687（#672 票 F）：planning argv 與 job spec 的 argv 合法性契約。

## 這組測試釘的是什麼

票 F 的第一件事是「查清楚 planning 現在到底走哪條路」，而查證的方式是在四分部署上
實跑一輪 define。那一跑立刻撞到一條**票 E（#686）從未被執行過的路徑**上的阻斷：

```
brainstorm-not-ready … reason=question-pack-malformed: JobRunnerError:
  job-runner-job-spec-invalid: spec 的 command 不得為空、且每個元素都必須是非空字串
  (source=job_runner.build_job_spec)
```

根因：`planning_runtime._planning_argv()` 對 `claude` 產出的 argv 含一個**空字串
元素**——`["claude", "-p", …, "--tools", "", …]`——而 `job_runner.build_job_spec()`
與 `job_shim.load_spec()` 兩端都以 `all(argv)` 要求「每個元素都是非空字串」。

`--tools ""` **不是筆誤，是 CLI 的成文 API**（`claude --help` 逐字：
`Use "" to disable all tools`），也是 #404 之後 planning 唯一的「模型完全沒有工具
可呼叫」保證。它必須逐字保留。

## 為什麼不是把 `--tools ""` 改寫成 `--tools=`

因為那個「等價寫法」**不等價，而且失敗方向是靜默放寬**。本票在真實 reviewer unit
的完整加固面下（`psc_run_under`／`permgen.unit_replica_properties()` 全量導出，38 條
property）做過三臂對照，同一個 prompt：

===============================  ==========================================
argv                             模型回報
===============================  ==========================================
`--tools ""`（兩個 token）        `NOTOOLS`（工具真的不存在）
`--tools=`（單一 token）          **發出 Bash 工具呼叫**（工具全開）
不帶 `--tools`                    有工具、三個 turn（對照組）
===============================  ==========================================

`--tools <tools...>` 在 commander 是 variadic；`--tools=` 不會被解成「空清單」。
把它當成等價寫法，等於讓**吃 untrusted issue 內容**的 planner 在降權 job 內拿回
Bash——而症狀是「planning 跑起來了」，看起來像成功。這正是 #672 整張票要消除的
失效模式，因此本檔有一條測試把 `["--tools", ""]` 逐字釘死。

## 因此修的是那條過嚴的守衛

`argv[0]` 必須非空（`execvpe("")` 是沒有意義的呼叫，失敗訊息也不可讀）；**其餘元素
可以是任何字串**，那是 POSIX argv 本來的語意。放寬的安全論證見
`job_runner.malformed_job_command()` 的 docstring：spec 的信任控制是
`forbidden_spec_keys()`（身分／剖面）與 `reject_unsafe_env()`（憑證），不是元素長度；
而 argv 裡**早就**有任意的攻擊者可影響字串（planning prompt 逐字含 issue 內容），
多允許一個空字串換不到任何新能力。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paulsha_cortex.coordinator import job_runner, job_shim  # noqa: E402
from paulsha_cortex.coordinator.model_identities import ModelIdentity  # noqa: E402
from paulsha_cortex.coordinator.planning_runtime import _planning_argv  # noqa: E402


def _identity(executor: str, model_id: str) -> ModelIdentity:
    return ModelIdentity(
        executor=executor,
        model_id=model_id,
        independence_domain="test-domain",
        capabilities=frozenset({"planning"}),
    )


_SPEC_BASE = {
    "job_id": "j",
    "instance": "j-deadbeef",
    "unit": "cortex-reviewer-job@j-deadbeef.service",
    "working_directory": "/wt",
    "log_path": "/logs/j.jsonl",
    "env": {"PATH": "/usr/bin"},
}


class PlanningArgvSurvivesJobSpec(unittest.TestCase):
    """票 E 的路徑實跑之後才會撞到的那一條（RED）。"""

    def test_every_planning_executor_argv_builds_a_valid_job_spec(self) -> None:
        """三個 planning executor 的 production argv 都必須組得出 job spec。

        這條之所以到票 F 才紅，是因為票 E 的驗收矩陣走的是 `psc_run_under`——它
        複製的是**加固面**，完全繞過 Manager 側的 `build_job_spec()`。機械導出的
        複本證明得了「executor 在那個沙箱下跑得起來」，證明不了「Manager 派得出
        那個 job」。`job-specs/reviewer/` 在票 F 之前**一個檔都沒寫過**。
        """

        for executor, model_id in (
            ("claude", "claude-opus-5"),
            ("codex", "gpt-5.3-codex-spark"),
            ("agy", "gemini-3.1-pro-high"),
        ):
            with self.subTest(executor=executor):
                argv = _planning_argv(
                    _identity(executor, model_id),
                    "PROMPT",
                    "/tmp",
                    Path("/scratch/cwd"),
                    last_message_path=Path("/lg/inst/planning.last.json"),
                )
                spec = job_runner.build_job_spec(command=argv, **_SPEC_BASE)
                self.assertEqual(spec["command"], argv)

    def test_claude_planning_argv_keeps_the_documented_empty_tools_value(self) -> None:
        """`--tools ""` 逐字不得被「等價寫法」換掉。

        `claude --help` 逐字：`Use "" to disable all tools`。本票在真實 reviewer
        unit 的完整加固面下實測，`--tools=` 會讓模型**拿回全部工具**（發出 Bash
        呼叫），而 planning 照樣跑得完——一個看起來像成功的失敗。
        """

        argv = _planning_argv(
            _identity("claude", "claude-opus-5"),
            "PROMPT",
            "/tmp",
            Path("/scratch/cwd"),
            last_message_path=Path("/lg/inst/planning.last.json"),
        )
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--tools=", argv)


class JobCommandWellformednessContract(unittest.TestCase):
    """放寬之後**還剩下什麼**——以及寫端讀端不得漂移。"""

    def test_argv0_must_still_be_non_empty(self) -> None:
        for command in ([], [""], ["", "-c", "true"]):
            with self.subTest(command=command):
                with self.assertRaises(job_runner.JobRunnerError) as ctx:
                    job_runner.build_job_spec(command=command, **_SPEC_BASE)
                self.assertEqual(
                    ctx.exception.diagnostic.reason, "job-runner-job-spec-invalid"
                )

    def test_non_leading_empty_arguments_are_accepted(self) -> None:
        spec = job_runner.build_job_spec(command=["bash", ""], **_SPEC_BASE)
        self.assertEqual(spec["command"], ["bash", ""])

    def test_write_end_and_read_end_agree_on_every_case(self) -> None:
        """同一組 argv 在 `build_job_spec()` 與 `job_shim.load_spec()` 判定相同。

        #679 的教訓是「兩份會漂移的真相」。本票把判準收進一支
        `malformed_job_command()`，兩端各呼叫一次；這條測試釘的是**呼叫端真的
        改了**——只要有人把任一端改回自寫判準，這裡就紅。

        比對範圍是 `list[str]`：那是兩端的實際交集（寫端的型別契約是
        `Sequence[str]` 且會先 `str()` 正規化，讀端拿到的是 JSON 解出來的值）。
        非 list／非 str 的情形由 `test_read_end_still_rejects_non_string_arrays`
        單獨守。
        """

        cases: tuple[list[str], ...] = (
            ["bash", "-c", "true"],
            ["bash", ""],
            ["claude", "-p", "x", "--tools", "", "--model", "m"],
            [],
            [""],
            ["", "-c", "true"],
        )
        for command in cases:
            with self.subTest(command=command):
                try:
                    job_runner.build_job_spec(command=command, **_SPEC_BASE)
                    write_rejected = False
                except job_runner.JobRunnerError:
                    write_rejected = True
                self.assertEqual(write_rejected, self._shim_rejects(command), command)

    def test_read_end_still_rejects_non_string_arrays(self) -> None:
        """讀端沒有型別契約可依賴（bytes 剛從磁碟讀回來），因此仍要自己擋型別。"""

        for command in ("bash -c true", ["bash", 3], {"argv": ["bash"]}, None):
            with self.subTest(command=command):
                self.assertTrue(self._shim_rejects(command), command)

    def _shim_rejects(self, command: object) -> bool:
        with tempfile.TemporaryDirectory() as spool:
            spec = {
                "spec_version": job_runner.JOB_SPEC_VERSION,
                "instance": "j-deadbeef",
                "job_id": "j",
                "unit": "cortex-reviewer-job@j-deadbeef.service",
                "command": command,
                "working_directory": "/wt",
                "log_path": "/logs/j.jsonl",
                "env": {"PATH": "/usr/bin"},
            }
            path = Path(spool) / "j-deadbeef.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            try:
                job_shim.load_spec("j-deadbeef", spool)
            except job_shim.ShimError as exc:
                self.assertIn("command", str(exc))
                return True
            return False


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
