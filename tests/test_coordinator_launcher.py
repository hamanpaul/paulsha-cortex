from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import paulsha_cortex.coordinator.launcher as launcher_module
from paulsha_cortex.coordinator.launcher import (
    SubprocessLauncher,
    build_copilot_argv,
    build_claude_argv,
    build_codex_argv,
    build_cg_argv,
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
        self.assertEqual(argv.count("--effort"), 1)
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_copilot_builder_commit_required_scopes_tool_and_git_write_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            linked = root / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Launcher Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "launcher@example.invalid"],
                check=True,
            )
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(repo), "worktree", "add", "-q",
                    "-b", "feature/copilot-scope", str(linked), "HEAD",
                ],
                check=True,
            )

            argv = build_copilot_argv(
                prompt="P",
                slice_id="s",
                log_dir=str(root / "logs"),
                worktree=str(linked),
                commit_required=True,
            )

            self.assertIn("--allow-all-tools", argv)
            self.assertNotIn("--allow-all", argv)
            add_dirs = [
                argv[index + 1]
                for index, value in enumerate(argv)
                if value == "--add-dir"
            ]
            self.assertEqual(
                add_dirs,
                [
                    str(linked.resolve()),
                    str((repo / ".git" / "worktrees" / "linked").resolve()),
                    str((repo / ".git" / "objects").resolve()),
                    str((repo / ".git" / "refs" / "heads" / "feature").resolve()),
                    str((repo / ".git" / "logs" / "refs" / "heads" / "feature").resolve()),
                ],
            )

    def test_copilot_builder_commit_required_rejects_incompatible_modes(self) -> None:
        for kwargs in (
            {"read_only": True},
            {"review_only": True},
            {"allow_unsafe": True},
        ):
            with self.assertRaisesRegex(ValueError, "enforced workspace-write"):
                build_copilot_argv(
                    prompt="P",
                    slice_id="s",
                    log_dir="/lg",
                    worktree="/wt/slice-a",
                    commit_required=True,
                    **kwargs,
                )
        with self.assertRaisesRegex(ValueError, "requires a worktree"):
            build_copilot_argv(
                prompt="P",
                slice_id="s",
                log_dir="/lg",
                commit_required=True,
            )

    def test_copilot_builder_commit_required_false_preserves_existing_argv(self) -> None:
        baseline = build_copilot_argv(
            prompt="P",
            slice_id="s",
            log_dir="/lg",
            worktree="/wt/slice-a",
            model="claude-haiku-4.5",
            allow_unsafe=True,
        )
        argv = build_copilot_argv(
            prompt="P",
            slice_id="s",
            log_dir="/lg",
            worktree="/wt/slice-a",
            model="claude-haiku-4.5",
            allow_unsafe=True,
            commit_required=False,
        )

        self.assertEqual(argv, baseline)

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

    def test_claude_builder_commit_required_grants_linked_worktree_git_metadata(self) -> None:
        # #396 item 3：claude builder 完成後 sandbox 對 git add/commit 回 requires
        # approval（headless 無人可 approve）→ candidate-worktree-dirty。root cause：
        # build_copilot_argv／build_codex_argv 的 commit_required 分支都會把 linked
        # worktree 的外部 git 目錄（.git/worktrees/<name>、objects、refs、logs）透過
        # --add-dir 放行，唯獨 build_claude_argv 從未接這條路徑——worktree 的 .git
        # 只是個指向 repo 外部的檔案，實際 index/objects/refs 都在 --add-dir worktree
        # 範圍之外，sandbox 因此擋下 git add/commit。這裡驗證 claude 現在比照
        # copilot/codex 補齊同一份 --add-dir 清單。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            linked = root / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Launcher Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "launcher@example.invalid"],
                check=True,
            )
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(repo), "worktree", "add", "-q",
                    "-b", "feature/claude-scope", str(linked), "HEAD",
                ],
                check=True,
            )

            argv = build_claude_argv(
                prompt="P",
                slice_id="s",
                log_dir=str(root / "logs"),
                worktree=str(linked),
                commit_required=True,
            )

            add_dirs = [
                argv[index + 1]
                for index, value in enumerate(argv)
                if value == "--add-dir"
            ]
            self.assertEqual(
                add_dirs,
                [
                    str(linked.resolve()),
                    str((repo / ".git" / "worktrees" / "linked").resolve()),
                    str((repo / ".git" / "objects").resolve()),
                    str((repo / ".git" / "refs" / "heads" / "feature").resolve()),
                    str((repo / ".git" / "logs" / "refs" / "heads" / "feature").resolve()),
                ],
            )

    def test_claude_builder_commit_required_false_preserves_existing_argv(self) -> None:
        baseline = build_claude_argv(
            prompt="P",
            slice_id="s",
            log_dir="/lg",
            worktree="/wt/slice-a",
            model="opus",
        )
        argv = build_claude_argv(
            prompt="P",
            slice_id="s",
            log_dir="/lg",
            worktree="/wt/slice-a",
            model="opus",
            commit_required=False,
        )

        self.assertEqual(argv, baseline)

    def test_claude_builder_commit_required_rejects_incompatible_modes(self) -> None:
        for kwargs in (
            {"read_only": True},
            {"allow_unsafe": True},
        ):
            with self.assertRaisesRegex(ValueError, "enforced workspace-write"):
                build_claude_argv(
                    prompt="P",
                    slice_id="s",
                    log_dir="/lg",
                    worktree="/wt/slice-a",
                    commit_required=True,
                    **kwargs,
                )
        with self.assertRaisesRegex(ValueError, "enforced workspace-write"):
            build_claude_argv(
                prompt="P",
                slice_id="s",
                log_dir="/lg",
                worktree="/wt/reviewer",
                review_only=True,
                review_terminal_kind="workflow-verification-result",
                commit_required=True,
            )

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

    def test_planner_read_only_argv_never_uses_edit_permissions(self) -> None:
        claude = build_claude_argv(
            prompt="P", slice_id="s", log_dir="/lg", read_only=True
        )
        codex = build_codex_argv(
            prompt="P", slice_id="s", log_dir="/lg", read_only=True
        )

        self.assertEqual(claude[claude.index("--permission-mode") + 1], "plan")
        self.assertNotIn("acceptEdits", claude)
        self.assertEqual(claude[claude.index("--tools") + 1], "")
        self.assertEqual(codex[codex.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", codex)

    def test_reviewer_read_only_argv_allows_inspection_but_never_edit_permissions(self) -> None:
        with mock.patch.object(
            launcher_module,
            "_srt_runtime_root",
            return_value=Path("/tools/sandbox-runtime"),
        ):
            claude = build_claude_argv(
                prompt="P", slice_id="s", log_dir="/lg",
                worktree="/wt/reviewer", review_only=True,
                review_terminal_kind="workflow-verification-result",
            )
        codex = build_codex_argv(
            prompt="P", slice_id="s", log_dir="/lg", review_only=True
        )

        self.assertEqual(claude[claude.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("acceptEdits", claude)
        self.assertNotIn("--remote-control", claude)
        self.assertNotIn("--add-dir", claude)
        self.assertEqual(claude[claude.index("--tools") + 1], "Bash")
        self.assertNotIn("--allowedTools", claude)
        self.assertEqual(claude[claude.index("--setting-sources") + 1], "")
        settings = json.loads(claude[claude.index("--settings") + 1])
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertEqual(
            settings["sandbox"]["filesystem"]["denyWrite"],
            ["/wt/reviewer/candidate"],
        )
        self.assertEqual(
            settings["sandbox"]["filesystem"]["allowRead"][0],
            "/wt/reviewer/candidate",
        )
        self.assertIn(
            "/tools/sandbox-runtime",
            settings["sandbox"]["filesystem"]["allowRead"],
        )
        self.assertEqual(
            settings["sandbox"]["filesystem"]["denyRead"],
            [str(Path.home().resolve()), "/run/user", "/run/docker.sock"],
        )
        protected_files = {
            row["path"] for row in settings["sandbox"]["credentials"]["files"]
        }
        self.assertIn("/run/user", protected_files)
        self.assertIn("/run/docker.sock", protected_files)
        self.assertNotIn("/var/run/docker.sock", protected_files)
        self.assertIn("--strict-mcp-config", claude)
        schema = json.loads(claude[claude.index("--json-schema") + 1])
        self.assertEqual(
            schema["required"],
            ["schema_version", "kind", "status", "summary", "details", "reports"],
        )
        self.assertEqual(
            schema["properties"]["kind"]["enum"], ["workflow-verification-result"]
        )
        # #261 R1：三種終局狀態對等可達；只允許成功形狀會逼 verifier fail-open。
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["verified", "failed", "needs_human"],
        )
        review = build_claude_argv(
            prompt="P",
            slice_id="review", log_dir="/lg", worktree="/wt/reviewer", review_only=True,
            review_terminal_kind="workflow-review-result",
        )
        review_schema = json.loads(review[review.index("--json-schema") + 1])
        self.assertEqual(
            review_schema["required"],
            ["schema_version", "kind", "reason", "findings", "reports"],
        )
        self.assertEqual(
            review_schema["properties"]["kind"]["enum"], ["workflow-review-result"]
        )
        self.assertIn(
            "report-only",
            review_schema["properties"]["findings"]["items"]["properties"]
            ["category"]["description"],
        )
        self.assertIn("--safe-mode", claude)
        self.assertIn("--no-session-persistence", claude)
        self.assertEqual(codex[codex.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", codex)
        with self.assertRaisesRegex(ValueError, "Candidate checkout"):
            build_claude_argv(
                prompt="P", slice_id="s", log_dir="/lg", review_only=True
            )
        with self.assertRaisesRegex(ValueError, "terminal contract kind"):
            build_claude_argv(
                prompt="P", slice_id="s", log_dir="/lg",
                worktree="/wt/reviewer", review_only=True,
            )
        with self.assertRaisesRegex(ValueError, "read-only"):
            build_copilot_argv(prompt="P", slice_id="s", log_dir="/lg", review_only=True)

    def test_codex_builder_keeps_git_trust_check(self) -> None:
        argv = build_codex_argv(
            prompt="P", slice_id="s", log_dir="/lg", read_only=False
        )

        self.assertNotIn("--skip-git-repo-check", argv)

    def test_codex_builder_grants_only_linked_worktree_git_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            linked = root / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Launcher Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "launcher@example.invalid"],
                check=True,
            )
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(repo), "worktree", "add", "-q",
                    "-b", "feature/launcher-test", str(linked), "HEAD",
                ],
                check=True,
            )

            builder = build_codex_argv(
                prompt="P",
                slice_id="s",
                log_dir=str(root / "logs"),
                worktree=str(linked),
                commit_required=True,
            )
            planner = build_codex_argv(
                prompt="P",
                slice_id="s",
                log_dir=str(root / "logs"),
                worktree=str(linked),
                read_only=True,
            )

            # #716 B 後半：寫入卡的 mode 是 danger-full-access（字面值刻意寫死當釘子）。
            self.assertEqual(builder[builder.index("--sandbox") + 1], "danger-full-access")
            add_dirs = [
                builder[index + 1]
                for index, value in enumerate(builder)
                if value == "--add-dir"
            ]
            self.assertEqual(
                add_dirs,
                [
                    str((repo / ".git" / "worktrees" / "linked").resolve()),
                    str((repo / ".git" / "objects").resolve()),
                    str((repo / ".git" / "refs" / "heads" / "feature").resolve()),
                    str((repo / ".git" / "logs" / "refs" / "heads" / "feature").resolve()),
                ],
            )
            self.assertNotIn("--add-dir", planner)

    def test_codex_builder_rejects_detached_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            linked = root / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Launcher Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "launcher@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(linked)],
                check=True,
            )

            reviewer = build_codex_argv(
                prompt="P",
                slice_id="s",
                log_dir=str(root / "logs"),
                worktree=str(linked),
            )
            self.assertNotIn("--add-dir", reviewer)

            with self.assertRaisesRegex(ValueError, "gitdir escapes"):
                build_codex_argv(
                    prompt="P",
                    slice_id="s",
                    log_dir=str(root / "logs"),
                    worktree=str(linked),
                    commit_required=True,
                )

    def test_codex_builder_ignores_inherited_git_repository_selection(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            linked = root / "linked"
            decoy = root / "decoy"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "fixture"],
                check=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Launcher Test",
                    "GIT_AUTHOR_EMAIL": "launcher@example.invalid",
                    "GIT_COMMITTER_NAME": "Launcher Test",
                    "GIT_COMMITTER_EMAIL": "launcher@example.invalid",
                },
            )
            subprocess.run(
                [
                    "git", "-C", str(repo), "worktree", "add", "-q",
                    "-b", "feature/scope-test", str(linked), "HEAD",
                ],
                check=True,
            )
            subprocess.run(["git", "init", "-q", str(decoy)], check=True)

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_DIR": str(decoy / ".git"),
                    "GIT_WORK_TREE": str(linked),
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.worktree",
                    "GIT_CONFIG_VALUE_0": str(linked),
                },
                clear=False,
            ):
                argv = build_codex_argv(
                    prompt="P",
                    slice_id="s",
                    log_dir=str(root / "logs"),
                    worktree=str(linked),
                    commit_required=True,
                )

            add_dirs = [
                argv[index + 1]
                for index, value in enumerate(argv)
                if value == "--add-dir"
            ]
            self.assertEqual(add_dirs[0], str(repo / ".git" / "worktrees" / "linked"))
            self.assertNotIn(str(decoy / ".git"), add_dirs)

    def test_codex_builder_rejects_symlink_git_marker(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            metadata = root / "metadata"
            metadata.mkdir()
            (root / ".git").symlink_to(metadata, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                build_codex_argv(
                    prompt="P",
                    slice_id="s",
                    log_dir=str(root / "logs"),
                    worktree=str(root),
                    commit_required=True,
                )

    def test_codex_commit_required_rejects_sandbox_bypass(self) -> None:
        with self.assertRaisesRegex(ValueError, "enforced workspace-write"):
            build_codex_argv(
                prompt="P", slice_id="s", log_dir="/lg",
                allow_unsafe=True, commit_required=True,
            )

    def test_subprocess_launcher_commit_capability_is_explicit(self) -> None:
        base = SubprocessLauncher("codex")
        builder = base.as_commit_required()

        self.assertFalse(base._commit_required)
        self.assertTrue(builder._commit_required)
        self.assertFalse(builder._allow_unsafe)

    def test_cg_executor_is_supported_and_review_only(self) -> None:
        # issue #442：改寫 #396 item 1 釘住的「cg 刻意未支援」——operator 已提供並
        # smoke 驗證 cg 的 CLI 契約，cg 現已登記進 _ARGV_BUILDERS。cg 是 zero-tool
        # （見 build_cg_argv docstring），只服務 read-only 的 planner／reviewer；
        # 這裡改釘「cg 支援，但 builder 語境（既非 read_only 也非 review_only）
        # 建構期即拒絕」。
        self.assertIn("cg", launcher_module._ARGV_BUILDERS)
        with self.assertRaisesRegex(ValueError, "cg executor requires read-only or review-only"):
            SubprocessLauncher("cg")
        # read-only planner 與 review-only reviewer 兩種合法角色都能正常建構。
        SubprocessLauncher("cg", read_only=True)
        SubprocessLauncher(
            "cg", review_only=True, review_terminal_kind="workflow-review-result",
        )

    def test_cg_argv_default_model_and_effort(self) -> None:
        argv = build_cg_argv(
            prompt="P", slice_id="s", log_dir="/lg", read_only=True,
        )
        self.assertEqual(
            argv,
            ["cg", "--model", "glm-5.2", "--effort", "medium", "--headless", "--stdin"],
        )

    def test_cg_argv_explicit_model_and_effort(self) -> None:
        argv = build_cg_argv(
            prompt="P", slice_id="s", log_dir="/lg", read_only=True,
            model="glm-5.3", effort="xhigh",
        )
        self.assertEqual(
            argv,
            ["cg", "--model", "glm-5.3", "--effort", "xhigh", "--headless", "--stdin"],
        )

    def test_cg_argv_never_embeds_prompt(self) -> None:
        # 契約核心差異：其餘 builder 把 prompt 放進 argv（見
        # test_prompt_is_single_element）；cg 的 prompt 走 stdin，argv 本身完全
        # 不含 prompt 字面值。
        argv = build_cg_argv(
            prompt="SECRET-PROMPT-TOKEN", slice_id="s", log_dir="/lg", read_only=True,
        )
        self.assertNotIn("SECRET-PROMPT-TOKEN", argv)

    def test_cg_argv_rejects_invalid_effort(self) -> None:
        with self.assertRaisesRegex(ValueError, "effort must be one of"):
            build_cg_argv(
                prompt="P", slice_id="s", log_dir="/lg", read_only=True, effort="ultra",
            )

    def test_cg_argv_rejects_unsafe_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not support unsafe mode"):
            build_cg_argv(
                prompt="P", slice_id="s", log_dir="/lg", read_only=True, allow_unsafe=True,
            )

    def test_cg_argv_rejects_commit_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "zero-tool and cannot commit"):
            build_cg_argv(
                prompt="P", slice_id="s", log_dir="/lg", commit_required=True,
            )

    def test_cg_argv_rejects_builder_context(self) -> None:
        # 既非 read_only 也非 review_only：這是「builder 語境」，cg 的 zero-tool
        # 契約下永遠無法真正建置任何東西，必須在建構期就顯性失敗。
        with self.assertRaisesRegex(ValueError, "requires read-only or review-only"):
            build_cg_argv(prompt="P", slice_id="s", log_dir="/lg")

    def test_subprocess_launcher_cg_refuses_unsafe_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "cg executor refuses unsafe mode"):
            SubprocessLauncher("cg", read_only=True, allow_unsafe=True)

    def test_subprocess_launcher_cg_as_commit_required_rejected(self) -> None:
        # cg 只能是 read_only 或 review_only；as_commit_required() 對兩者皆既有的
        # 「commit-required 需要 enforced workspace-write」守衛一律拒絕——cg 永遠
        # 無法被轉換成 builder-persona 的 commit-required launcher。
        base = SubprocessLauncher("cg", read_only=True)
        with self.assertRaisesRegex(ValueError, "enforced workspace-write"):
            base.as_commit_required()

    def test_launch_cg_pipes_prompt_via_stdin_not_argv(self) -> None:
        # stdin plumbing（issue #442）：cg 的 wrapper script 必須把 prompt 經
        # `printf %s <prompt> |` 餵進內層 argv，而非把 prompt 當成 argv 的一個
        # 元素（既有 copilot/claude/codex/agy 的路徑）。
        calls = []

        class _FakeProc:
            pid = 999

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                SubprocessLauncher("cg", read_only=True).launch(
                    slice_id="slice-cg",
                    prompt="STDIN-PROMPT",
                    worktree=d,
                    log_dir=str(Path(d) / "logs"),
                )
        finally:
            launcher_module.subprocess.Popen = original

        script = calls[0]["argv"][2]
        inner = shlex.join(
            ["cg", "--model", "glm-5.2", "--effort", "medium", "--headless", "--stdin"],
        )
        # prompt 經 printf | 餵入，緊接著才是不含 prompt 的內層 argv
        self.assertIn(f"printf %s {shlex.quote('STDIN-PROMPT')} | {inner}", script)
        # 內層 argv 本身（不含 prompt）維持乾淨，不因 stdin 分支而混入 prompt
        self.assertNotIn("STDIN-PROMPT --model", script)
        # cg 的 stderr summary banner 顯式與 stdout 分離，不混入 JSONL log
        self.assertIn(f"{inner} 2>/dev/null", script)

    def test_launch_cg_end_to_end_stdin_delivers_prompt_to_process(self) -> None:
        # 比照既有 test_subprocess_launcher_sentinel_records_real_exit_code 的
        # 「真跑 bash -lc，但內層命令覆寫成無害替身」手法：以 `cat` 取代真 cg
        # binary（cat 原樣把 stdin 回顯到 stdout），驗證 prompt 真的經由 stdin
        # （而非 argv）抵達內層命令，端到端證實 stdin plumbing 而非只驗 script 字串。
        orig_builders = dict(launcher_module._ARGV_BUILDERS)
        launcher_module._ARGV_BUILDERS["cg"] = lambda **_kw: ["cat"]
        try:
            with tempfile.TemporaryDirectory() as d:
                log_dir = Path(d) / "logs"
                handle = SubprocessLauncher("cg", read_only=True).launch(
                    slice_id="slice-cg-e2e",
                    prompt="HELLO-FROM-STDIN",
                    worktree=d,
                    log_dir=str(log_dir),
                )
                try:
                    os.waitpid(handle.pid, 0)
                except ChildProcessError:
                    pass
                self.assertEqual(
                    Path(handle.log_path).read_text(encoding="utf-8"), "HELLO-FROM-STDIN",
                )
        finally:
            launcher_module._ARGV_BUILDERS.clear()
            launcher_module._ARGV_BUILDERS.update(orig_builders)

    def test_subprocess_launcher_passes_effort_to_cg_argv(self) -> None:
        calls = []

        class _FakeProc:
            pid = 741

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                SubprocessLauncher("cg", read_only=True, effort="high").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "logs"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        script = calls[0]["argv"][2]
        self.assertIn("--effort high", script)

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

    def test_launch_removes_inherited_git_repository_selection_env(self) -> None:
        calls = []

        class _FakeProc:
            pid = 223

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ,
                {
                    "GIT_DIR": "/tmp/decoy.git",
                    "GIT_COMMON_DIR": "/tmp/decoy-common",
                    "GIT_WORK_TREE": "/tmp/decoy-worktree",
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.worktree",
                    "GIT_CONFIG_VALUE_0": "/tmp/decoy-worktree",
                },
                clear=False,
            ):
                SubprocessLauncher("copilot").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original

        env = calls[0]["env"]
        for key in (
            "GIT_DIR",
            "GIT_COMMON_DIR",
            "GIT_WORK_TREE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
        ):
            self.assertNotIn(key, env)

    def test_launch_copilot_normalizes_credential_env_from_gh_token(self) -> None:
        # #396 item 2(b)：copilot executor 的 credential 注入契約——job env 沒有
        # COPILOT_GITHUB_TOKEN 時，從既有可設定來源（daemon 自身 process env 已有
        # 的 GH_TOKEN／GITHUB_TOKEN，例如 operator 佈署進
        # ~/.agents/core/runtime/<instance>.env 的 systemd EnvironmentFile）補上，
        # 不觸碰 OS keyring。
        calls = []

        class _FakeProc:
            pid = 231

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {"GH_TOKEN": "gh-token-value"}, clear=False
            ):
                SubprocessLauncher("copilot").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        env = calls[0]["env"]
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "gh-token-value")

    def test_launch_copilot_prefers_explicit_copilot_token_over_gh_token(self) -> None:
        calls = []

        class _FakeProc:
            pid = 232

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ,
                {"COPILOT_GITHUB_TOKEN": "explicit-value", "GH_TOKEN": "gh-token-value"},
                clear=False,
            ):
                SubprocessLauncher("copilot").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        env = calls[0]["env"]
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "explicit-value")

    def test_launch_copilot_falls_back_to_github_token(self) -> None:
        calls = []

        class _FakeProc:
            pid = 233

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {"GITHUB_TOKEN": "github-token-value"}, clear=False
            ):
                SubprocessLauncher("copilot").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        env = calls[0]["env"]
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "github-token-value")

    def test_launch_copilot_no_token_present_leaves_env_untouched(self) -> None:
        calls = []

        class _FakeProc:
            pid = 234

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {}, clear=False
            ):
                for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
                    os.environ.pop(name, None)
                SubprocessLauncher("copilot").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        env = calls[0]["env"]
        self.assertNotIn("COPILOT_GITHUB_TOKEN", env)

    def test_launch_non_copilot_executor_does_not_inject_copilot_token(self) -> None:
        calls = []

        class _FakeProc:
            pid = 235

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"env": env})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ, {"GH_TOKEN": "gh-token-value"}, clear=False
            ):
                SubprocessLauncher("codex").launch(
                    slice_id="s", prompt="P", worktree=d, log_dir=str(Path(d) / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original
        env = calls[0]["env"]
        # codex 不做 copilot 的 credential 正規化；GH_TOKEN 本身仍照既有 passthrough
        # 行為原樣傳遞（_git_scope_env 只過濾 GIT_* key），但不會被複寫成
        # COPILOT_GITHUB_TOKEN。
        self.assertNotIn("COPILOT_GITHUB_TOKEN", env)
        self.assertEqual(env["GH_TOKEN"], "gh-token-value")

    def test_reviewer_launch_uses_minimal_env_and_non_login_shell(self) -> None:
        calls = []

        class _FakeProc:
            pid = 225

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv, "env": env})
            return _FakeProc()

        inherited_secrets = {
            "PGPASSWORD": "postgres-secret",
            "MYSQL_PWD": "mysql-secret",
            "DATABASE_URL": "postgres://secret@example.invalid/db",
            "GITHUB_PAT": "github-secret",
            "BASH_ENV": "/tmp/credential-exporter",
            "LC_SECRET": "locale-shaped-secret",
        }
        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.dict(
                os.environ,
                inherited_secrets,
                clear=False,
            ):
                SubprocessLauncher("claude").as_review_only(
                    terminal_kind="workflow-verification-result"
                ).launch(
                    slice_id="review",
                    prompt="P",
                    worktree=d,
                    log_dir=str(Path(d) / "logs"),
                )
        finally:
            launcher_module.subprocess.Popen = original

        self.assertEqual(calls[0]["argv"][:2], ["bash", "-c"])
        for key in inherited_secrets:
            self.assertNotIn(key, calls[0]["env"])
        self.assertNotIn("PSC_REPO_ROOT", calls[0]["env"])
        self.assertNotIn("PSC_RELAY_TARGET", calls[0]["env"])
        self.assertLessEqual(
            set(calls[0]["env"]),
            {
                "HOME", "LANG", "LC_ADDRESS", "LC_ALL", "LC_COLLATE", "LC_CTYPE",
                "LC_IDENTIFICATION", "LC_MEASUREMENT", "LC_MESSAGES", "LC_MONETARY",
                "LC_NAME", "LC_NUMERIC", "LC_PAPER", "LC_TELEPHONE", "LC_TIME",
                "LOGNAME", "PATH", "SHELL", "TMPDIR", "USER", "VIRTUAL_ENV",
            },
        )

    def test_launch_resolves_worktree_before_argv_and_popen(self) -> None:
        calls = []

        class _FakeProc:
            pid = 224

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv, "cwd": cwd})
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                actual = root / "actual"
                alias = root / "alias"
                actual.mkdir()
                alias.symlink_to(actual, target_is_directory=True)
                SubprocessLauncher("codex").launch(
                    slice_id="s",
                    prompt="P",
                    worktree=str(alias),
                    log_dir=str(root / "lg"),
                )
        finally:
            launcher_module.subprocess.Popen = original

        self.assertEqual(calls[0]["cwd"], str(actual.resolve()))
        self.assertIn(f"-C {actual.resolve()}", calls[0]["argv"][2])
        self.assertNotIn(str(alias), calls[0]["argv"][2])

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

    def test_subprocess_launcher_copilot_commit_required_adds_scoped_permissions(self) -> None:
        calls = []

        class _FakeProc:
            pid = 226

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv})
            return _FakeProc()

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            linked = root / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Launcher Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "launcher@example.invalid"],
                check=True,
            )
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(repo), "worktree", "add", "-q",
                    "-b", "feature/copilot-launch", str(linked), "HEAD",
                ],
                check=True,
            )

            git_write_dirs = launcher_module._linked_worktree_git_write_dirs(str(linked))
            original = launcher_module.subprocess.Popen
            launcher_module.subprocess.Popen = _fake_popen
            try:
                with mock.patch.object(
                    launcher_module,
                    "_linked_worktree_git_write_dirs",
                    return_value=git_write_dirs,
                ):
                    SubprocessLauncher("copilot").as_commit_required().launch(
                        slice_id="s",
                        prompt="P",
                        worktree=str(linked),
                        log_dir=str(root / "lg"),
                    )
            finally:
                launcher_module.subprocess.Popen = original

            script = calls[0]["argv"][2]
            inner_argv = shlex.split(script.split(";", 1)[0])
            self.assertIn("--allow-all-tools", script)
            self.assertIn(f"--add-dir {shlex.quote(str(linked.resolve()))}", script)
            self.assertIn(
                f"--add-dir {shlex.quote(git_write_dirs[0])}",
                script,
            )
            self.assertNotIn("--allow-all", inner_argv)

    def test_subprocess_launcher_claude_commit_required_adds_git_write_dirs(self) -> None:
        # #396 item 3：SubprocessLauncher 端也要把 commit_required 接進 claude
        # builder_kwargs（先前只接了 codex/copilot），否則 as_commit_required()
        # 這個既有的 builder-persona 轉換（autonomy.dispatch_ready）對 claude 是
        # no-op，實際派工時 argv 仍缺 --add-dir。
        calls = []

        class _FakeProc:
            pid = 227

        def _fake_popen(argv, *, cwd, env, stdout, stderr):
            calls.append({"argv": argv})
            return _FakeProc()

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            linked = root / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Launcher Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "launcher@example.invalid"],
                check=True,
            )
            (repo / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-qm", "fixture"],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(repo), "worktree", "add", "-q",
                    "-b", "feature/claude-launch", str(linked), "HEAD",
                ],
                check=True,
            )

            git_write_dirs = launcher_module._linked_worktree_git_write_dirs(str(linked))
            original = launcher_module.subprocess.Popen
            launcher_module.subprocess.Popen = _fake_popen
            try:
                with mock.patch.object(
                    launcher_module,
                    "_linked_worktree_git_write_dirs",
                    return_value=git_write_dirs,
                ):
                    SubprocessLauncher("claude").as_commit_required().launch(
                        slice_id="s",
                        prompt="P",
                        worktree=str(linked),
                        log_dir=str(root / "lg"),
                    )
            finally:
                launcher_module.subprocess.Popen = original

            script = calls[0]["argv"][2]
            self.assertIn(f"--add-dir {shlex.quote(str(linked.resolve()))}", script)
            self.assertIn(
                f"--add-dir {shlex.quote(git_write_dirs[0])}",
                script,
            )

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
        # #623：模型的 `$?` 先存進 shell 變數（bundle 段夾在中間，見
        # `build_wrapper_script`），寫進 sentinel 的仍然是**模型**的 exit code。
        self.assertIn("__psc_rc=$?", script)
        self.assertIn(f'printf %s "$__psc_rc" > {shlex.quote(sentinel)}', script)
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


    def test_copilot_argv_model_and_default_effort(self) -> None:
        argv = build_copilot_argv(prompt="P", slice_id="s", log_dir="/lg", model="gpt-5.4")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.4")
        self.assertEqual(argv.count("--effort"), 1)
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertLess(argv.index("--model"), argv.index("--effort"))

    def test_copilot_argv_explicit_effort(self) -> None:
        argv = build_copilot_argv(
            prompt="P", slice_id="s", log_dir="/lg", model="gpt-5.4", effort="high",
        )
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_copilot_argv_rejects_invalid_effort(self) -> None:
        with self.assertRaisesRegex(ValueError, "effort must be one of"):
            build_copilot_argv(
                prompt="P", slice_id="s", log_dir="/lg", effort="ultra",
            )

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
                with mock.patch.object(
                    launcher_module.job_workspace,
                    "prepare_commit_spool",
                    return_value=Path(d) / "bundle",
                ):
                    SubprocessLauncher("copilot", model="gpt-5.4").launch(
                        slice_id="s", prompt="P", worktree=d, log_dir=d
                    )
        finally:
            launcher_module.subprocess.Popen = original
        script = captured["argv"][2]
        self.assertIn("--model gpt-5.4", script)
        self.assertIn("--effort xhigh", script)
        self.assertEqual(script.count("--effort"), 1)

    def test_subprocess_launcher_passes_effort_to_copilot_argv(self) -> None:
        captured = {}

        class _FakeProc:
            pid = 4322

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                with mock.patch.object(
                    launcher_module.job_workspace,
                    "prepare_commit_spool",
                    return_value=Path(d) / "bundle",
                ):
                    SubprocessLauncher("copilot", effort="high").launch(
                        slice_id="s", prompt="P", worktree=d, log_dir=d
                    )
        finally:
            launcher_module.subprocess.Popen = original
        script = captured["argv"][2]
        self.assertIn("--effort high", script)

    def _seed_template_builder_runtime(self, root: Path) -> None:
        canonical = root / "codex-controls" / "builder"
        (canonical / "plugins").mkdir(parents=True)
        (canonical / "skills").mkdir()
        (canonical / "config.toml").write_text("model = 'deployed'\n")
        (canonical / "hooks.json").write_text("{}\n")
        credentials = root / "credentials" / "builder"
        credentials.mkdir(parents=True, exist_ok=True)
        (credentials / "auth.json").write_text('{"refresh":"seed"}\n')

    def test_template_launch_uses_instance_named_log_slot_and_explicit_control_anchor(self) -> None:
        captured: dict[str, object] = {}

        class _FakeProc:
            pid = 7777

        def _fake_popen(argv, **kwargs):
            captured["argv"] = argv
            captured["popen_kwargs"] = kwargs
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                slice_id = "wf-" + ("x" * 96)
                instance = launcher_module.job_runner.template_instance_id(slice_id)
                expected_control_log = str(root / "runtime" / "dispatch" / f"{slice_id}.jsonl")
                plan = launcher_module.job_runner.SystemdTemplatePlan(
                    binary="systemctl",
                    template_unit="cortex-builder-job@.service",
                    instance=instance,
                    unit=f"cortex-builder-job@{instance}.service",
                    account="cortex-builder",
                    group="cortex-builder",
                    shim="/usr/bin/psc-job-shim",
                    spool_dir=str(root / "job-specs"),
                    spec_path=str(root / "job-specs" / f"{instance}.json"),
                    hardening_profile="strict",
                    executor="copilot",
                    base_template_unit="cortex-builder-job@.service",
                    role=launcher_module.job_runner.JOB_ROLE_BUILDER,
                )

                def _fake_build_job_env(**_kwargs):
                    return {
                        "PATH": os.environ.get("PATH", ""),
                        "HOME": str(root),
                        "PSC_JOB_ID": slice_id,
                        "PSC_SLICE_ID": slice_id,
                        "PSC_REPO_ROOT": str(root),
                    }

                def _fake_build_job_spec(**kwargs):
                    captured["spec"] = kwargs
                    return {
                        "instance": kwargs["instance"],
                        "unit": kwargs["unit"],
                        "log_path": kwargs["log_path"],
                    }

                def _fake_confirm_template_instance_started(**kwargs):
                    captured["confirmed"] = kwargs

                with mock.patch.dict(
                    os.environ,
                    {
                        "PSC_JOB_RUNNER": launcher_module.job_runner.RUNNER_SYSTEMD_TEMPLATE,
                        "PSC_AGENTS_ROOT": str(root),
                    },
                    clear=False,
                ), mock.patch.object(
                    launcher_module.job_runner, "prepare_systemd_template", return_value=plan
                ), mock.patch.object(
                    launcher_module.job_runner, "build_job_env", side_effect=_fake_build_job_env
                ), mock.patch.object(
                    launcher_module.job_runner, "build_job_spec", side_effect=_fake_build_job_spec
                ), mock.patch.object(
                    launcher_module.job_runner, "write_job_spec"
                ), mock.patch.object(
                    launcher_module.job_runner,
                    "build_systemctl_start_argv",
                    return_value=["systemctl", "start", plan.unit],
                ), mock.patch.object(
                    launcher_module.job_runner,
                    "build_manager_exit_recorder_argv",
                    side_effect=lambda *, client_argv, sentinel, cleanup_path: list(client_argv),
                ), mock.patch.object(
                    launcher_module.spool_slot,
                    "canonical_codex_controls",
                    return_value=str(root / "codex-home"),
                ), mock.patch.object(
                    launcher_module.spool_slot, "system_account_exists", return_value=False
                ), mock.patch.object(
                    launcher_module.spool_slot, "provision_runtime_surfaces"
                ), mock.patch.object(
                    launcher_module.job_runner, "ensure_workspace_reachable"
                ), mock.patch.object(
                    launcher_module.job_runner,
                    "confirm_template_instance_started",
                    side_effect=_fake_confirm_template_instance_started,
                ):
                    handle = SubprocessLauncher("copilot").launch(
                        slice_id=slice_id,
                        prompt="PROMPT",
                        worktree=str(root),
                        log_dir=str(root / "runtime" / "dispatch"),
                    )
        finally:
            launcher_module.subprocess.Popen = original

        spec = captured["spec"]
        confirmed = captured["confirmed"]
        self.assertNotEqual(instance, slice_id)
        self.assertEqual(Path(handle.log_path).parent.name, instance)
        self.assertEqual(Path(spec["log_path"]).parent.name, instance)
        self.assertEqual(spec["instance"], instance)
        self.assertTrue(spec["unit"].endswith(f"@{instance}.service"))
        self.assertEqual(spec["log_path"], handle.log_path)
        self.assertEqual(handle.control_log_path, expected_control_log)
        self.assertEqual(confirmed["log_path"], expected_control_log)
        self.assertEqual(confirmed["job_log_path"], handle.log_path)
        self.assertEqual(Path(handle.control_log_path).stem, slice_id)

    def test_template_launch_provisions_all_builder_surfaces_before_systemctl_start(self) -> None:
        captured: dict[str, object] = {}

        class _FakeProc:
            pid = 7777

        def _fake_popen(argv, **kwargs):
            expected_slots = {
                row.surface_id: launcher_module.spool_slot.canonical_job_slot(
                    row.surface_id, slice_id
                )
                for row in launcher_module.spool_slot.PER_JOB_WRITABLE_SURFACES
                if "builder" in row.principals
            }
            for surface_id, slot in expected_slots.items():
                self.assertTrue(
                    slot.is_dir(),
                    f"{surface_id} missing before systemctl start: {slot}",
                )
            self.assertTrue(Path(captured["spec"]["log_path"]).exists())
            captured["argv"] = argv
            captured["popen_kwargs"] = kwargs
            return _FakeProc()

        original = launcher_module.subprocess.Popen
        launcher_module.subprocess.Popen = _fake_popen
        try:
            with tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self._seed_template_builder_runtime(root)
                slice_id = "wf-" + ("y" * 96)
                instance = launcher_module.job_runner.template_instance_id(slice_id)
                plan = launcher_module.job_runner.SystemdTemplatePlan(
                    binary="systemctl",
                    template_unit="cortex-builder-job@.service",
                    instance=instance,
                    unit=f"cortex-builder-job@{instance}.service",
                    account="cortex-builder",
                    group="cortex-builder",
                    shim="/usr/bin/psc-job-shim",
                    spool_dir=str(root / "job-specs"),
                    spec_path=str(root / "job-specs" / f"{instance}.json"),
                    hardening_profile="strict",
                    executor="copilot",
                    base_template_unit="cortex-builder-job@.service",
                    role=launcher_module.job_runner.JOB_ROLE_BUILDER,
                )

                def _fake_build_job_env(**_kwargs):
                    return {
                        "PATH": os.environ.get("PATH", ""),
                        "HOME": str(root),
                        "PSC_JOB_ID": slice_id,
                        "PSC_SLICE_ID": slice_id,
                        "PSC_REPO_ROOT": str(root),
                    }

                def _fake_build_job_spec(**kwargs):
                    captured["spec"] = kwargs
                    return {
                        "instance": kwargs["instance"],
                        "unit": kwargs["unit"],
                        "log_path": kwargs["log_path"],
                    }

                with mock.patch.dict(
                    os.environ,
                    {
                        "PSC_JOB_RUNNER": launcher_module.job_runner.RUNNER_SYSTEMD_TEMPLATE,
                        "PSC_AGENTS_ROOT": str(root),
                        "PSC_CODEX_CONTROL_ROOT": str(root / "codex-controls"),
                        "PSC_CODEX_CREDENTIAL_ROOT": str(root / "credentials"),
                    },
                    clear=False,
                ), mock.patch.object(
                    launcher_module.job_runner, "prepare_systemd_template", return_value=plan
                ), mock.patch.object(
                    launcher_module.job_runner, "build_job_env", side_effect=_fake_build_job_env
                ), mock.patch.object(
                    launcher_module.job_runner, "build_job_spec", side_effect=_fake_build_job_spec
                ), mock.patch.object(
                    launcher_module.job_runner, "write_job_spec"
                ), mock.patch.object(
                    launcher_module.job_runner,
                    "build_systemctl_start_argv",
                    return_value=["systemctl", "start", plan.unit],
                ), mock.patch.object(
                    launcher_module.job_runner,
                    "build_manager_exit_recorder_argv",
                    side_effect=lambda *, client_argv, sentinel, cleanup_path: list(client_argv),
                ), mock.patch.object(
                    launcher_module.spool_slot, "system_account_exists", return_value=False
                ), mock.patch.object(
                    launcher_module.job_runner, "ensure_workspace_reachable"
                ), mock.patch.object(
                    launcher_module.job_runner, "confirm_template_instance_started"
                ):
                    SubprocessLauncher("copilot").launch(
                        slice_id=slice_id,
                        prompt="PROMPT",
                        worktree=str(root),
                        log_dir=str(root / "runtime" / "dispatch"),
                    )
        finally:
            launcher_module.subprocess.Popen = original

        self.assertEqual(captured["argv"], ["systemctl", "start", plan.unit])

    def test_template_launch_rejects_malformed_event_slot_before_systemctl_start(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._seed_template_builder_runtime(root)
            slice_id = "wf-" + ("z" * 96)
            instance = launcher_module.job_runner.template_instance_id(slice_id)
            plan = launcher_module.job_runner.SystemdTemplatePlan(
                binary="systemctl",
                template_unit="cortex-builder-job@.service",
                instance=instance,
                unit=f"cortex-builder-job@{instance}.service",
                account="cortex-builder",
                group="cortex-builder",
                shim="/usr/bin/psc-job-shim",
                spool_dir=str(root / "job-specs"),
                spec_path=str(root / "job-specs" / f"{instance}.json"),
                hardening_profile="strict",
                executor="copilot",
                base_template_unit="cortex-builder-job@.service",
                role=launcher_module.job_runner.JOB_ROLE_BUILDER,
            )
            with mock.patch.dict(
                os.environ,
                {
                    "PSC_JOB_RUNNER": launcher_module.job_runner.RUNNER_SYSTEMD_TEMPLATE,
                    "PSC_AGENTS_ROOT": str(root),
                    "PSC_CODEX_CONTROL_ROOT": str(root / "codex-controls"),
                    "PSC_CODEX_CREDENTIAL_ROOT": str(root / "credentials"),
                },
                clear=False,
            ):
                event_slot = launcher_module.spool_slot.canonical_job_slot(
                    "monitor-event-spool", slice_id
                )
                event_slot.parent.mkdir(parents=True, exist_ok=True)
                target = root / "foreign-slot"
                target.mkdir()
                event_slot.symlink_to(target, target_is_directory=True)
                with mock.patch.object(
                    launcher_module.subprocess,
                    "Popen",
                    side_effect=AssertionError("systemctl start must not run"),
                ), mock.patch.object(
                    launcher_module.job_runner, "prepare_systemd_template", return_value=plan
                ), mock.patch.object(
                    launcher_module.job_runner,
                    "build_job_env",
                    side_effect=AssertionError("build_job_env must not run"),
                ), mock.patch.object(
                    launcher_module.spool_slot, "system_account_exists", return_value=False
                ):
                    with self.assertRaises(launcher_module.spool_slot.SpoolSlotError) as raised:
                        SubprocessLauncher("copilot").launch(
                            slice_id=slice_id,
                            prompt="PROMPT",
                            worktree=str(root),
                            log_dir=str(root / "runtime" / "dispatch"),
                        )
                self.assertIn("monitor-event-spool", str(raised.exception))
                self.assertIn(str(event_slot), str(raised.exception))

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


class CopilotCredentialEnvTests(unittest.TestCase):
    """#396 item 2(b)：pure-function 單元測試，覆蓋 `_copilot_credential_env` 的
    優先序與 no-op 分支，獨立於 SubprocessLauncher.launch() 的 Popen 接線。
    """

    def test_prefers_existing_copilot_github_token(self) -> None:
        env = {"COPILOT_GITHUB_TOKEN": "a", "GH_TOKEN": "b", "GITHUB_TOKEN": "c"}
        self.assertEqual(launcher_module._copilot_credential_env(env), {})

    def test_falls_back_to_gh_token(self) -> None:
        env = {"GH_TOKEN": "b", "GITHUB_TOKEN": "c"}
        self.assertEqual(
            launcher_module._copilot_credential_env(env), {"COPILOT_GITHUB_TOKEN": "b"}
        )

    def test_falls_back_to_github_token_when_gh_token_absent(self) -> None:
        env = {"GITHUB_TOKEN": "c"}
        self.assertEqual(
            launcher_module._copilot_credential_env(env), {"COPILOT_GITHUB_TOKEN": "c"}
        )

    def test_empty_string_token_treated_as_absent(self) -> None:
        env = {"COPILOT_GITHUB_TOKEN": "", "GH_TOKEN": "", "GITHUB_TOKEN": ""}
        self.assertEqual(launcher_module._copilot_credential_env(env), {})

    def test_no_candidate_env_is_no_op(self) -> None:
        self.assertEqual(launcher_module._copilot_credential_env({}), {})


if __name__ == "__main__":
    unittest.main()
