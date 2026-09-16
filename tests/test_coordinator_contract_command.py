from __future__ import annotations

import hashlib
import unittest

from paulsha_cortex.coordinator.contract_command import build_dispatch_prompt

_BODY = "spec body\n"
_SPEC = {
    "spec_path": "/specs/t.md",
    "spec_hash": hashlib.sha256(_BODY.encode("utf-8")).hexdigest(),
    "spec_body": _BODY,
}


class BuildDispatchPromptTests(unittest.TestCase):
    def test_carries_contract_task_and_plan(self) -> None:
        p = build_dispatch_prompt("builder", task="persona-phase-b", plan_path="docs/p.md", **_SPEC)
        self.assertIn("[PERSONA CONTRACT", p)
        self.assertIn("role: builder", p)
        self.assertIn("persona-phase-b", p)
        self.assertIn("docs/p.md", p)

    def test_no_shell_or_executor_wrapping(self) -> None:
        # executor-agnostic 純文字：不得含 shell/executor 包裝
        p = build_dispatch_prompt("builder", task="t", plan_path="p.md", **_SPEC)
        self.assertNotIn("copilot", p)
        self.assertNotIn("--yolo", p)
        self.assertNotIn("-p ", p)

    def test_builder_without_pinned_spec_is_refused(self) -> None:
        # #503：task id + plan 路徑不是 authority；builder 缺 spec 逐字內容即拒絕。
        with self.assertRaisesRegex(ValueError, "#503"):
            build_dispatch_prompt("builder", task="t", plan_path="p.md")

    def test_unknown_role_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_dispatch_prompt("nobody", task="t", plan_path="p.md")

    def test_pure_no_file_read(self) -> None:
        p = build_dispatch_prompt("builder", task="t", plan_path="/nope/x.md", **_SPEC)
        self.assertIn("/nope/x.md", p)


if __name__ == "__main__":
    unittest.main()
