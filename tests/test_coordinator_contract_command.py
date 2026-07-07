from __future__ import annotations

import unittest

from paulsha_cortex.coordinator.contract_command import build_dispatch_prompt


class BuildDispatchPromptTests(unittest.TestCase):
    def test_carries_contract_task_and_plan(self) -> None:
        p = build_dispatch_prompt("builder", task="persona-phase-b", plan_path="docs/p.md")
        self.assertIn("[PERSONA CONTRACT", p)
        self.assertIn("role: builder", p)
        self.assertIn("persona-phase-b", p)
        self.assertIn("docs/p.md", p)

    def test_no_shell_or_executor_wrapping(self) -> None:
        # executor-agnostic 純文字：不得含 shell/executor 包裝
        p = build_dispatch_prompt("builder", task="t", plan_path="p.md")
        self.assertNotIn("copilot", p)
        self.assertNotIn("--yolo", p)
        self.assertNotIn("-p ", p)

    def test_unknown_role_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_dispatch_prompt("nobody", task="t", plan_path="p.md")

    def test_pure_no_file_read(self) -> None:
        p = build_dispatch_prompt("builder", task="t", plan_path="/nope/x.md")
        self.assertIn("/nope/x.md", p)


if __name__ == "__main__":
    unittest.main()
