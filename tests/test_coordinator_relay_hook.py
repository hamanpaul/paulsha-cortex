from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOK = "paulsha_cortex/scripts/psc-relay-hook.sh"

# 注入用：把 reply_bridge 換成記錄 argv 的 stub（避免碰真 Telegram）。
_STUB = (
    "#!/usr/bin/env python3\n"
    "import sys, os\n"
    "open(os.environ.get('BRIDGE_LOG', '/dev/null'), 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n"
)


def _write_stub(p: Path) -> None:
    p.write_text(_STUB, encoding="utf-8")
    p.chmod(0o755)


class RelayHookTests(unittest.TestCase):
    def test_emits_slice_tagged_payload(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "relay.out"
            env = {**os.environ, "PSC_SLICE_ID": "slice-a",
                   "PSC_RELAY_TARGET": str(out), "PSC_RELAY_EVENT": "stop",
                   "PSC_REPLY_BRIDGE": str(Path(d) / "nope.py")}  # 不存在 → 跳過 bridge
            subprocess.run(["bash", HOOK], env=env, check=True)
            text = out.read_text(encoding="utf-8")
            self.assertIn("slice-a", text)
            self.assertIn("stop", text)

    def test_missing_target_does_not_fail(self) -> None:
        env = {**os.environ, "PSC_SLICE_ID": "slice-a", "PSC_RELAY_EVENT": "stop",
               "PSC_REPLY_BRIDGE": "/nonexistent/reply_bridge.py"}
        env.pop("PSC_RELAY_TARGET", None)
        r = subprocess.run(["bash", HOOK], env=env)
        self.assertEqual(r.returncode, 0)

    def test_slice_set_pushes_telegram(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "bridge.py"
            _write_stub(stub)
            log = Path(d) / "bridge.log"
            env = {**os.environ, "PSC_SLICE_ID": "slice-a", "PSC_RELAY_EVENT": "stop",
                   "PSC_RELAY_TARGET": str(Path(d) / "relay.out"),
                   "PSC_REPLY_BRIDGE": str(stub), "BRIDGE_LOG": str(log)}
            subprocess.run(["bash", HOOK], env=env, check=True)
            argv = log.read_text(encoding="utf-8")
            self.assertIn("--text", argv)
            self.assertIn("slice-a", argv)
            self.assertNotIn("--source-user-id", argv)  # broadcast

    def test_no_slice_does_not_push(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "bridge.py"
            _write_stub(stub)
            log = Path(d) / "bridge.log"
            env = {**os.environ, "PSC_RELAY_EVENT": "stop",
                   "PSC_RELAY_TARGET": str(Path(d) / "relay.out"),
                   "PSC_REPLY_BRIDGE": str(stub), "BRIDGE_LOG": str(log)}
            env.pop("PSC_SLICE_ID", None)
            subprocess.run(["bash", HOOK], env=env, check=True)
            self.assertFalse(log.exists(), "互動 session（無 slice）不應推 Telegram")

    def test_unknown_slice_does_not_push(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "bridge.py"
            _write_stub(stub)
            log = Path(d) / "bridge.log"
            env = {**os.environ, "PSC_SLICE_ID": "unknown", "PSC_RELAY_EVENT": "stop",
                   "PSC_RELAY_TARGET": str(Path(d) / "relay.out"),
                   "PSC_REPLY_BRIDGE": str(stub), "BRIDGE_LOG": str(log)}
            subprocess.run(["bash", HOOK], env=env, check=True)
            self.assertFalse(log.exists())

    def test_empty_slice_does_not_push(self) -> None:
        # review m-1：PSC_SLICE_ID="" 經 ${:-unknown} → "unknown" → no-op（與 unset 同）
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "bridge.py"
            _write_stub(stub)
            log = Path(d) / "bridge.log"
            env = {**os.environ, "PSC_SLICE_ID": "", "PSC_RELAY_EVENT": "stop",
                   "PSC_RELAY_TARGET": str(Path(d) / "relay.out"),
                   "PSC_REPLY_BRIDGE": str(stub), "BRIDGE_LOG": str(log)}
            subprocess.run(["bash", HOOK], env=env, check=True)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
