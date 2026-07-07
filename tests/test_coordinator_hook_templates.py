from __future__ import annotations

import importlib.resources
import json
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "paulsha_cortex" / "scripts" / "hooks"


def _copilot_commands(doc: dict) -> list[str]:
    return [entry.get("bash", "") for entries in doc["hooks"].values() for entry in entries]


def _codex_commands(doc: dict) -> list[str]:
    commands: list[str] = []
    for groups in doc["hooks"].values():
        for group in groups:
            commands.extend(entry.get("command", "") for entry in group["hooks"])
    return commands


class HookTemplateSchemaTests(unittest.TestCase):
    """smoke 實證的 schema：copilot 用 `bash` 鍵；codex 用 CamelCase 巢狀 + matcher。"""

    def test_copilot_uses_bash_key_not_command(self) -> None:
        d = json.loads((HOOKS / "copilot.json").read_text(encoding="utf-8"))
        self.assertEqual(d.get("version"), 1)
        for ev in ("sessionStart", "agentStop"):
            self.assertIn(ev, d["hooks"])
            for entry in d["hooks"][ev]:
                self.assertIn("bash", entry)        # copilot hook 用 bash 鍵
                self.assertNotIn("command", entry)  # 非 command

    def test_codex_uses_camelcase_nested_with_matcher(self) -> None:
        d = json.loads((HOOKS / "codex.json").read_text(encoding="utf-8"))
        self.assertIn("SessionStart", d["hooks"])   # CamelCase
        self.assertIn("Stop", d["hooks"])
        self.assertNotIn("session_start", d["hooks"])  # 非 snake_case
        self.assertNotIn("stop", d["hooks"])
        for ev in ("SessionStart", "Stop"):
            grp = d["hooks"][ev][0]
            self.assertIn("hooks", grp)              # 巢狀 hooks 陣列
            self.assertIn("matcher", grp)
            self.assertIn("command", grp["hooks"][0])

    def test_packaged_relay_hook_exists(self) -> None:
        relay = importlib.resources.files("paulsha_cortex") / "scripts" / "psc-relay-hook.sh"
        self.assertTrue(relay.is_file())

    def test_codex_hook_commands_use_cortex_relay_hook(self) -> None:
        d = json.loads((HOOKS / "codex.json").read_text(encoding="utf-8"))
        cmds = _codex_commands(d)
        self.assertTrue(any("cortex relay-hook" in cmd for cmd in cmds))
        self.assertFalse(any("psc-relay-hook.sh" in cmd for cmd in cmds))
        self.assertFalse(any("psc-bro-return" in cmd for cmd in cmds))

    def test_copilot_hook_commands_use_cortex_relay_hook(self) -> None:
        d = json.loads((HOOKS / "copilot.json").read_text(encoding="utf-8"))
        cmds = _copilot_commands(d)
        self.assertTrue(any("cortex relay-hook" in cmd for cmd in cmds))
        self.assertFalse(any("psc-relay-hook.sh" in cmd for cmd in cmds))
        self.assertFalse(any("psc-bro-return" in cmd for cmd in cmds))

    def test_claude_hook_commands_use_cortex_relay_hook(self) -> None:
        d = json.loads((HOOKS / "claude.json").read_text(encoding="utf-8"))
        cmds = _codex_commands(d)
        self.assertTrue(any("cortex relay-hook" in cmd for cmd in cmds))
        self.assertFalse(any("psc-relay-hook.sh" in cmd for cmd in cmds))
        self.assertFalse(any("psc-bro-return" in cmd for cmd in cmds))


if __name__ == "__main__":
    unittest.main()
