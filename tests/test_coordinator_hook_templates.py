from __future__ import annotations

import json
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / "paulsha_cortex" / "scripts" / "hooks"


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

    def test_relay_path_resolvable_via_repo_root_env(self) -> None:
        # 相對路徑在 cwd≠repo 時不可解；範本改用 ${PSC_REPO_ROOT}（launcher 注入）。
        for f in ("copilot.json", "codex.json"):
            self.assertIn("PSC_REPO_ROOT", (HOOKS / f).read_text(encoding="utf-8"))


class BroReturnEntryTests(unittest.TestCase):
    def test_codex_stop_has_bro_return_and_preserves_relay(self) -> None:
        d = json.loads((HOOKS / "codex.json").read_text(encoding="utf-8"))
        hooks = d["hooks"]["Stop"][0]["hooks"]
        cmds = [h.get("command", "") for h in hooks]
        # existing relay preserved
        self.assertTrue(any("psc-relay-hook.sh" in c for c in cmds), "relay entry must be preserved")
        # new bro-return entry
        self.assertTrue(any("psc-bro-return.py --platform codex" in c for c in cmds),
                        "codex Stop must register psc-bro-return --platform codex")
        bro = next(h for h in hooks if "psc-bro-return.py" in h.get("command", ""))
        self.assertEqual(bro.get("managedBy"), "psc-bro-return")
        self.assertIn("${PSC_REPO_ROOT}", bro["command"])

    def test_copilot_agentstop_has_bro_return_and_preserves_relay(self) -> None:
        d = json.loads((HOOKS / "copilot.json").read_text(encoding="utf-8"))
        arr = d["hooks"]["agentStop"]
        cmds = [h.get("bash", "") for h in arr]
        self.assertTrue(any("psc-relay-hook.sh" in c for c in cmds), "relay entry must be preserved")
        self.assertTrue(any("psc-bro-return.py --platform copilot" in c for c in cmds),
                        "copilot agentStop must register psc-bro-return --platform copilot")
        bro = next(h for h in arr if "psc-bro-return.py" in h.get("bash", ""))
        self.assertIn("${PSC_REPO_ROOT}", bro["bash"])


if __name__ == "__main__":
    unittest.main()
