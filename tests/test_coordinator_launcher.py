from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator.launcher import (
    SubprocessLauncher,
    build_copilot_argv,
    build_claude_argv,
    build_codex_argv,
)


class ArgvTests(unittest.TestCase):
    def test_copilot_argv(self) -> None:
        argv = build_copilot_argv(prompt="PROMPT", slice_id="slice-a", log_dir="/lg")
        self.assertEqual(argv[0], "copilot")
        self.assertIn("-p", argv)
        self.assertIn("PROMPT", argv)                 # prompt 為單一元素
        self.assertIn("--remote", argv)
        self.assertIn("--name", argv)
        self.assertIn("slice-a", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)

    def test_claude_argv(self) -> None:
        argv = build_claude_argv(
            prompt="PROMPT",
            slice_id="slice-a",
            log_dir="/lg",
            worktree="/wt/slice-a",
        )
        self.assertEqual(argv[0], "claude")
        self.assertIn("-p", argv)
        self.assertIn("PROMPT", argv)
        self.assertIn("--remote-control", argv)
        self.assertIn("--add-dir", argv)
        self.assertIn("/wt/slice-a", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)  # smoke: -p+stream-json 必須帶 --verbose
        self.assertIn("--name", argv)
        self.assertIn("slice-a", argv)
        self.assertIn("--permission-mode", argv)
        self.assertIn("acceptEdits", argv)

    def test_codex_argv(self) -> None:
        argv = build_codex_argv(
            prompt="PROMPT",
            slice_id="slice-a",
            log_dir="/lg",
            worktree="/wt/slice-a",
            remote="unix:/tmp/psc.sock",
        )
        self.assertEqual(argv[0], "codex")
        self.assertIn("exec", argv)
        self.assertIn("PROMPT", argv)
        self.assertNotIn("--remote", argv)  # smoke: codex exec 不吃 --remote（unexpected argument）
        self.assertNotIn("unix:/tmp/psc.sock", argv)
        self.assertIn("-C", argv)
        self.assertIn("/wt/slice-a", argv)
        self.assertIn("--json", argv)
        self.assertIn("-o", argv)

    def test_codex_argv_default_has_no_sandbox_bypass(self) -> None:
        # 預設（allow_unsafe 未開）不得帶 --dangerously-bypass-approvals-and-sandbox（高風險）
        argv = build_codex_argv(prompt="P", slice_id="s", log_dir="/lg")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_codex_argv_allow_unsafe_adds_sandbox_bypass(self) -> None:
        # 明確 opt-in allow_unsafe=True 才加入 sandbox bypass flag
        argv = build_codex_argv(prompt="P", slice_id="s", log_dir="/lg", allow_unsafe=True)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_codex_argv_default_no_hook_trust_bypass(self) -> None:
        argv = build_codex_argv(prompt="P", slice_id="s", log_dir="/lg")
        self.assertNotIn("--dangerously-bypass-hook-trust", argv)

    def test_codex_argv_allow_unsafe_adds_hook_trust_bypass(self) -> None:
        # smoke 實證：headless codex 帶 relay hook 時，未過信任閘會卡死 timeout。
        # autonomous（allow_unsafe）派工須一併 bypass hook trust。
        argv = build_codex_argv(prompt="P", slice_id="s", log_dir="/lg", allow_unsafe=True)
        self.assertIn("--dangerously-bypass-hook-trust", argv)

    def test_launch_sets_repo_root_env_for_relay_hook(self) -> None:
        # 相對 relay 路徑在 cwd=worktree(≠repo) 不可解；launcher 注入 PSC_REPO_ROOT
        # 讓已安裝 hook 的 ${PSC_REPO_ROOT}/scripts/... 可解。
        calls = []

        class _FakeProc:
            pid = 222

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                SubprocessLauncher("copilot").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        env = calls[0]["env"]
        self.assertIn("PSC_REPO_ROOT", env)
        self.assertTrue(env["PSC_REPO_ROOT"])

    def test_subprocess_launcher_codex_default_no_sandbox_bypass(self) -> None:
        import shlex

        calls = []

        class _FakeProc:
            pid = 111

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                SubprocessLauncher("codex").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        script = calls[0]["argv"][2]
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", script)

    def test_subprocess_launcher_codex_allow_unsafe_adds_sandbox_bypass(self) -> None:
        calls = []

        class _FakeProc:
            pid = 222

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                SubprocessLauncher("codex", allow_unsafe=True).launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        script = calls[0]["argv"][2]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", script)

    def test_prompt_is_single_element(self) -> None:
        # prompt 含換行也是單一 argv 元素（headless 的核心保證）
        argv = build_copilot_argv(prompt="line1\nline2", slice_id="s", log_dir="/lg")
        self.assertIn("line1\nline2", argv)

    def test_subprocess_launcher_injects_slice_and_relay_target_env(self) -> None:
        calls = []

        class _FakeProc:
            pid = 456

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv, "cwd": cwd, "env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                log_dir = Path(d) / "logs"
                handle = SubprocessLauncher(
                    "copilot",
                    relay_target="/tmp/relay.out",
                ).launch(
                    slice_id="slice-a",
                    prompt="PROMPT",
                    worktree=d,
                    log_dir=str(log_dir),
                )
        finally:
            launcher_module.subprocess.Popen = original

        self.assertEqual(handle.pid, 456)
        self.assertEqual(calls[0]["env"]["PSC_SLICE_ID"], "slice-a")
        self.assertEqual(calls[0]["env"]["PSC_RELAY_TARGET"], "/tmp/relay.out")

    def test_subprocess_launcher_wraps_with_exit_sentinel(self) -> None:
        import shlex

        from paulsha_cortex.coordinator.dispatcher import exit_sentinel_path

        calls = []

        class _FakeProc:
            pid = 789

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                log_dir = Path(d) / "logs"
                handle = SubprocessLauncher("copilot").launch(
                    slice_id="slice-a",
                    prompt="PROMPT",
                    worktree=d,
                    log_dir=str(log_dir),
                )
        finally:
            launcher_module.subprocess.Popen = original

        argv = calls[0]["argv"]
        # 子進程經 bash -lc 包裝，結束時把 $? 寫到 sentinel（跨進程 durable 完成判定）
        self.assertEqual(argv[0], "bash")
        self.assertEqual(argv[1], "-lc")
        script = argv[2]
        sentinel = str(exit_sentinel_path(handle.log_path))
        self.assertIn(shlex.quote(sentinel), script)
        self.assertIn('"$?"', script)
        # 內層 argv 經 shlex.join 安全嵌入；含 -p PROMPT
        inner = shlex.join(["copilot", "-p", "PROMPT"])
        self.assertIn(inner, script)

    def test_subprocess_launcher_clears_stale_sentinel_and_truncates_log(self) -> None:
        # 同一 slice_id 重跑：上一輪殘留的 .exit/.jsonl 必須在 launch 前清掉，
        # 否則 poll_headless_done 會讀到舊 sentinel → 誤判「還沒開始就完成了」。
        from paulsha_cortex.coordinator.dispatcher import exit_sentinel_path

        class _FakeProc:
            pid = 333

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                log_dir = Path(d) / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                stale_log = log_dir / "slice-a.jsonl"
                stale_exit = log_dir / "slice-a.exit"
                stale_log.write_text("STALE-PREV-ROUND\n", encoding="utf-8")
                stale_exit.write_text("0", encoding="utf-8")

                handle = SubprocessLauncher("copilot").launch(
                    slice_id="slice-a",
                    prompt="PROMPT",
                    worktree=d,
                    log_dir=str(log_dir),
                )

                # 舊 sentinel 在 launch 當下/前已被移除（fail-closed 防誤判完成）
                self.assertFalse(
                    exit_sentinel_path(handle.log_path).is_file(),
                    "stale .exit sentinel must be cleared before launch",
                )
                # log 以 wb 開啟（truncate）→ 不含上一輪內容
                self.assertNotIn("STALE-PREV-ROUND", Path(handle.log_path).read_text())
        finally:
            launcher_module.subprocess.Popen = original

    def test_subprocess_launcher_sentinel_records_real_exit_code(self) -> None:
        # 真跑 bash -lc 包裝，但內層 argv 覆寫成無害的 `exit 7`（絕不啟動真 copilot/codex），
        # 驗證 sentinel 確實寫下內層命令的真實 exit code（跨進程 durable 機制端到端）。
        from paulsha_cortex.coordinator.dispatcher import exit_sentinel_path

        orig_builders = dict(launcher_module._ARGV_BUILDERS)
        launcher_module._ARGV_BUILDERS["copilot"] = (
            lambda **_kw: ["sh", "-c", "exit 7"]
        )
        try:
            with tempfile.TemporaryDirectory() as d:
                log_dir = Path(d) / "logs"
                handle = SubprocessLauncher("copilot").launch(
                    slice_id="slice-z",
                    prompt="PROMPT",
                    worktree=d,
                    log_dir=str(log_dir),
                )
                sentinel = exit_sentinel_path(handle.log_path)
                # 根治 flaky：等包裝子進程「真正結束」再斷言，不靠固定輪詢預算（原本
                # 50×0.05s=2.5s 在 CI 高負載下會超時 → flaky）。bash 包裝在退出前必已
                # 寫出 sentinel（launcher：`<inner>; printf %s "$?" > <sentinel>`），故子
                # 進程一被 reap，sentinel 必然就緒；os.waitpid 同時回收 zombie（消除
                # 先前的 `subprocess still running` ResourceWarning）。test 進程即 spawn
                # 該子進程的父進程，故可 waitpid。
                try:
                    os.waitpid(handle.pid, 0)
                except ChildProcessError:
                    # 已被 subprocess 模組內部回收 → 能被回收代表已結束，sentinel 亦已寫出。
                    pass
                # 斷言 MUST 在 with 內（tmpdir 尚未清除）
                self.assertTrue(sentinel.is_file(), "sentinel exit 檔應由 bash 包裝寫出")
                self.assertEqual(sentinel.read_text().strip(), "7")
        finally:
            launcher_module._ARGV_BUILDERS.clear()
            launcher_module._ARGV_BUILDERS.update(orig_builders)


    def test_copilot_argv_model(self) -> None:
        argv = build_copilot_argv(prompt="P", slice_id="s", log_dir="/lg", model="claude-haiku-4.5")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-4.5")

    def test_argv_no_model_when_unset(self) -> None:
        for build in (build_copilot_argv, build_claude_argv, build_codex_argv):
            argv = build(prompt="P", slice_id="s", log_dir="/lg")
            self.assertNotIn("--model", argv, msg=build.__name__)

    def test_launch_handle_records_explicit_model_id(self) -> None:
        calls = []

        class _FakeProc:
            pid = 654

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv, "env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                handle = SubprocessLauncher("codex", model="gpt-5.4").launch(
                    slice_id="slice-review",
                    prompt="PROMPT",
                    worktree=d,
                    log_dir=str(Path(d) / "logs"),
                )
        finally:
            launcher_module.subprocess.Popen = original

        self.assertEqual(handle.executor, "codex")
        self.assertEqual(handle.model_id, "gpt-5.4")
        self.assertEqual(calls[0]["env"]["PSC_SLICE_ID"], "slice-review")

    def test_claude_codex_argv_model(self) -> None:
        ca = build_claude_argv(prompt="P", slice_id="s", log_dir="/lg", model="opus")
        self.assertEqual(ca[ca.index("--model") + 1], "opus")
        xa = build_codex_argv(prompt="P", slice_id="s", log_dir="/lg", model="gpt-5.4")
        self.assertEqual(xa[xa.index("--model") + 1], "gpt-5.4")

    def test_subprocess_launcher_passes_model_to_argv(self) -> None:
        captured = {}

        class _FakeProc:
            pid = 4321

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                SubprocessLauncher("copilot", model="claude-haiku-4.5").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=d
                )
        finally:
            launcher_module.subprocess.Popen = original
        script = captured["argv"][2]
        self.assertIn("--model claude-haiku-4.5", script)

    def test_launch_sentinel_is_absolute_cwd_independent(self) -> None:
        # bug：相對 log_dir + 子進程 cwd=worktree → sentinel 寫到 worktree（poller 找不到）。
        # 修：launch 把 log_dir resolve 成絕對 → script 內 sentinel 與回傳 log_path 皆絕對。
        import os
        import re as _re

        captured = {}

        class _FakeProc:
            pid = 5555

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        original_cwd = os.getcwd()
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                os.chdir(d)  # launcher 在某 cwd，log_dir 給相對路徑
                handle = SubprocessLauncher("copilot").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir="runtime/dispatch/s"
                )
        finally:
            os.chdir(original_cwd)
            launcher_module.subprocess.Popen = original
        script = captured["argv"][2]
        m = _re.search(r">\s*(\S*s\.exit)", script)
        self.assertIsNotNone(m, script)
        self.assertTrue(m.group(1).startswith("/"), f"sentinel 非絕對: {m.group(1)}")
        self.assertTrue(handle.log_path.startswith("/"), f"log_path 非絕對: {handle.log_path}")


if __name__ == "__main__":
    unittest.main()
