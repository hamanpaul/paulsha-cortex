from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from paulsha_cortex.porcelain import headless_hook


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class ExecutableModeToolTests(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        _git(repo, "config", "user.name", "Builder Test")
        _git(repo, "config", "user.email", "builder@example.invalid")
        script = repo / "script" / "deliver.py"
        script.parent.mkdir()
        script.write_text("print('ready')\n", encoding="utf-8")
        (repo / "other.txt").write_text("baseline\n", encoding="utf-8")
        _git(repo, "add", "script/deliver.py", "other.txt")
        _git(repo, "commit", "-qm", "add script")
        return repo

    def test_narrow_executable_operation_commits_git_mode_100755(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repository(Path(directory))

            headless_hook.set_executable_file(
                "script/deliver.py",
                worktree_root=repo,
                write_paths=("script/*.py",),
            )
            _git(repo, "add", "script/deliver.py")

            self.assertTrue(stat.S_IMODE((repo / "script/deliver.py").stat().st_mode) & stat.S_IXUSR)
            self.assertTrue(_git(repo, "ls-files", "--stage", "script/deliver.py").startswith("100755 "))

            _git(repo, "commit", "-qm", "make script executable")
            self.assertTrue(_git(repo, "ls-tree", "HEAD", "--", "script/deliver.py").startswith("100755 blob "))

    def test_restore_file_changes_only_one_declared_file_and_keeps_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = self._repository(Path(directory))
            original_head = _git(repo, "rev-parse", "HEAD").strip()
            script = repo / "script" / "deliver.py"
            script.write_text("corrupted\n", encoding="utf-8")
            _git(repo, "add", "script/deliver.py")
            other = repo / "other.txt"
            other.write_text("keep this edit\n", encoding="utf-8")

            with (
                mock.patch.dict(os.environ, {"PSC_JOB_ID": "job-test"}),
                mock.patch.object(Path, "cwd", return_value=repo),
                redirect_stdout(StringIO()),
            ):
                result = headless_hook.main(["restore-file", "--path", "script/deliver.py"])

            self.assertEqual(result, 0)
            self.assertEqual(script.read_text(encoding="utf-8"), "print('ready')\n")
            self.assertEqual(other.read_text(encoding="utf-8"), "keep this edit\n")
            self.assertEqual(_git(repo, "rev-parse", "HEAD").strip(), original_head)
            self.assertEqual(
                _git(repo, "-c", "color.status=false", "status", "--short"),
                " M other.txt\n",
            )

    def test_pre_tool_use_denies_oversized_edit_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.py"
            target.write_text("before\n", encoding="utf-8")
            payload = {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(target),
                    "old_string": "before",
                    "new_string": "x" * 40000,
                },
            }
            output = StringIO()
            with mock.patch("sys.stdin", StringIO(json.dumps(payload))), redirect_stdout(output):
                result = headless_hook.main(["pre-tool-use"])

            decision = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(decision["hookSpecificOutput"]["hookEventName"], "PreToolUse")
            self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("32768 bytes", decision["hookSpecificOutput"]["permissionDecisionReason"])
            self.assertEqual(target.read_text(encoding="utf-8"), "before\n")

    def test_pre_tool_use_leaves_bounded_edit_to_normal_permissions(self) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "target.py", "old_string": "a", "new_string": "b"},
        }
        output = StringIO()
        with mock.patch("sys.stdin", StringIO(json.dumps(payload))), redirect_stdout(output):
            result = headless_hook.main(["pre-tool-use"])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "")

    def test_pre_tool_use_denies_oversized_write(self) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "target.py", "content": "y" * 40000},
        }
        output = StringIO()
        with mock.patch("sys.stdin", StringIO(json.dumps(payload))), redirect_stdout(output):
            result = headless_hook.main(["pre-tool-use"])

        decision = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pre_tool_use_allows_medium_edit_with_large_old_string(self) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "target.py",
                "old_string": "o" * 5000,
                "new_string": "n" * 900,
            },
        }
        output = StringIO()
        with mock.patch("sys.stdin", StringIO(json.dumps(payload))), redirect_stdout(output):
            result = headless_hook.main(["pre-tool-use"])

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "")

    def test_headless_hook_help_lists_bounded_file_operations(self) -> None:
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            headless_hook.main(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("set-executable", output.getvalue())
        self.assertIn("restore-file", output.getvalue())

    def test_narrow_executable_operation_rejects_out_of_scope_and_symlink_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = self._repository(root)
            outside = repo / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")
            outside_mode = stat.S_IMODE(outside.stat().st_mode)
            with self.assertRaisesRegex(ValueError, "outside declared write_paths"):
                headless_hook.set_executable_file(
                    "outside.py",
                    worktree_root=repo,
                    write_paths=("script/*.py",),
                )
            self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)

            external = root / "external"
            external.mkdir()
            external_file = external / "payload.py"
            external_file.write_text("print('external')\n", encoding="utf-8")
            external_mode = stat.S_IMODE(external_file.stat().st_mode)
            (repo / "linked").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                headless_hook.set_executable_file(
                    "linked/payload.py",
                    worktree_root=repo,
                    write_paths=("**",),
                )
            self.assertEqual(stat.S_IMODE(external_file.stat().st_mode), external_mode)
