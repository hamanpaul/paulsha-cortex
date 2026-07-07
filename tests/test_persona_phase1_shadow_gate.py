from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


def _valid_manifest(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "from_role": "builder",
        "to_role": "reviewer",
        "phase": "review",
        "gate_status": "passed",
        "slice_id": "persona-phase1-shadow-gate",
        "summary": "phase1 shadow gate building blocks",
        "artifact_refs": ["feature/persona-phase1-shadow-gate"],
        "created_at": "2026-06-18T00:00:00+08:00",
        "base": "main",
        "head": "feature/persona-phase1-shadow-gate",
    }
    payload.update(overrides)
    return payload


class HandoffManifestTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        from paulsha_cortex.persona import handoff

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "runtime" / "handoff" / "persona-phase1-shadow-gate.json"
            payload = _valid_manifest()
            handoff.write_manifest(path, payload)
            self.assertTrue(path.is_file())
            loaded = handoff.read_manifest(path)
            self.assertEqual(loaded, payload)

    def test_write_creates_parent_dirs(self) -> None:
        from paulsha_cortex.persona import handoff

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a" / "b" / "c.json"
            handoff.write_manifest(path, _valid_manifest())
            self.assertTrue(path.is_file())

    def test_invalid_manifest_raises(self) -> None:
        from paulsha_cortex.persona import handoff

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            # 缺 created_at 且 gate_status 非法 → validate_handoff_message 不過
            bad = _valid_manifest()
            del bad["created_at"]
            bad["gate_status"] = "bogus"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                handoff.read_manifest(path)

    def test_missing_file_fails_closed(self) -> None:
        from paulsha_cortex.persona import handoff

        with self.assertRaises(Exception) as ctx:
            handoff.read_manifest("/nonexistent/handoff/x.json")
        # fail-closed：FileNotFoundError 或 ValueError 皆可，重點是 raise 而非回傳空
        self.assertIsInstance(ctx.exception, (FileNotFoundError, ValueError))


class RenderContractPromptTests(unittest.TestCase):
    def test_contains_role_scope(self) -> None:
        from paulsha_cortex.persona import render
        from paulsha_cortex.persona.loader import load_catalog

        catalog = load_catalog()
        prompt = render.render_contract_prompt("builder", catalog=catalog)
        self.assertIsInstance(prompt, str)
        self.assertIn("builder", prompt)
        # 角色名與各 write_path 子字串皆須出現
        for write_path in catalog["builder"].write_paths:
            self.assertIn(write_path, prompt)
        # allowed_phases 與 effective_tools 也須體現（① 契約注入）
        self.assertIn("build", prompt)
        self.assertIn("git commit", prompt)

    def test_deterministic(self) -> None:
        from paulsha_cortex.persona import render

        self.assertEqual(
            render.render_contract_prompt("reviewer"),
            render.render_contract_prompt("reviewer"),
        )

    def test_unknown_role_raises(self) -> None:
        from paulsha_cortex.persona import render

        with self.assertRaises(ValueError):
            render.render_contract_prompt("nope")


def _write_manifest(tmp: Path) -> Path:
    from paulsha_cortex.persona import handoff

    path = tmp / "runtime" / "handoff" / "persona-phase1-shadow-gate.json"
    handoff.write_manifest(path, _valid_manifest())
    return path


class GateVerdictTests(unittest.TestCase):
    def test_in_scope_ok(self) -> None:
        from paulsha_cortex.persona import gate
        from paulsha_cortex.persona.loader import load_catalog

        catalog = load_catalog()
        verdict = gate.build_verdict(
            role="builder",
            changed_paths=["paulsha_cortex/persona/gate.py", "tests/test_x.py"],
            manifest_ok=True,
            catalog=catalog,
        )
        self.assertEqual(verdict["role"], "builder")
        self.assertEqual(verdict["changed_paths"], ["paulsha_cortex/persona/gate.py", "tests/test_x.py"])
        self.assertEqual(verdict["violations"], [])
        self.assertTrue(verdict["handoff_ok"])
        self.assertTrue(verdict["ok"])

    def test_out_of_scope_violation(self) -> None:
        from paulsha_cortex.persona import gate
        from paulsha_cortex.persona.loader import load_catalog

        catalog = load_catalog()
        verdict = gate.build_verdict(
            role="builder",
            changed_paths=["paulsha_cortex/persona/gate.py", "docs/secret.md"],
            manifest_ok=True,
            catalog=catalog,
        )
        self.assertFalse(verdict["ok"])
        offending = [v["path"] for v in verdict["violations"]]
        self.assertIn("docs/secret.md", offending)
        for v in verdict["violations"]:
            self.assertTrue(v["reason"])  # 帶可審計原因

    def test_manifest_not_ok_makes_verdict_fail(self) -> None:
        from paulsha_cortex.persona import gate
        from paulsha_cortex.persona.loader import load_catalog

        verdict = gate.build_verdict(
            role="builder",
            changed_paths=["paulsha_cortex/persona/gate.py"],
            manifest_ok=False,
            catalog=load_catalog(),
        )
        self.assertFalse(verdict["handoff_ok"])
        self.assertFalse(verdict["ok"])

    def test_compute_changed_paths_real_git(self) -> None:
        from paulsha_cortex.persona import gate

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                            capture_output=True, env={**__import__("os").environ, **env})
            run("init", "-q", "-b", "main")
            (repo / "base.txt").write_text("x\n", encoding="utf-8")
            run("add", "-A")
            run("commit", "-q", "-m", "base")
            run("checkout", "-q", "-b", "feature/x")
            (repo / "paulsha_cortex").mkdir()
            (repo / "paulsha_cortex" / "new.py").write_text("y\n", encoding="utf-8")
            run("add", "-A")
            run("commit", "-q", "-m", "feat")
            paths = gate.compute_changed_paths("main", "feature/x", repo=repo)
            self.assertEqual(paths, ["paulsha_cortex/new.py"])

    def test_non_ascii_path_no_false_violation(self) -> None:
        # 回歸：core.quotepath 預設 true 會把 docs/正體.md 變成
        # '"docs/\346\255\243\351\253\224.md"'，經 _normalize_path 後誤判越界。
        # compute_changed_paths 必須回傳未跳脫的原始路徑，manager scope 下不得有 false violation。
        from paulsha_cortex.persona import gate
        from paulsha_cortex.persona.loader import load_catalog

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
            run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                            capture_output=True, env={**__import__("os").environ, **env})
            run("init", "-q", "-b", "main")
            # 明確開 quotepath（模擬 git 預設），確保即使環境 config 已關仍能重現
            run("config", "core.quotepath", "true")
            (repo / "base.txt").write_text("x\n", encoding="utf-8")
            run("add", "-A")
            run("commit", "-q", "-m", "base")
            run("checkout", "-q", "-b", "feature/x")
            (repo / "docs").mkdir()
            (repo / "docs" / "正體.md").write_text("zh\n", encoding="utf-8")
            run("add", "-A")
            run("commit", "-q", "-m", "feat")

            paths = gate.compute_changed_paths("main", "feature/x", repo=repo)
            self.assertEqual(paths, ["docs/正體.md"])

            # manager scope 含 docs/** → 不得有 false violation
            verdict = gate.build_verdict(
                role="manager",
                changed_paths=paths,
                manifest_ok=True,
                catalog=load_catalog(),
            )
            self.assertEqual(verdict["violations"], [])
            self.assertTrue(verdict["ok"])


class GateExitCodeTests(unittest.TestCase):
    def _patch_diff(self, gate_mod, paths: list[str]):
        original = gate_mod.compute_changed_paths
        gate_mod.compute_changed_paths = lambda base, head, repo=None: list(paths)
        self.addCleanup(setattr, gate_mod, "compute_changed_paths", original)

    def test_shadow_always_zero_enforce_one_on_violation(self) -> None:
        from paulsha_cortex.persona import gate

        self._patch_diff(gate, ["docs/secret.md"])  # builder 越界
        with tempfile.TemporaryDirectory() as d:
            manifest = _write_manifest(Path(d))
            argv = ["--role", "builder", "--base", "main",
                    "--head", "feature/x", "--manifest", str(manifest)]
            self.assertEqual(gate.main(argv), 0)              # shadow 恆 0
            self.assertEqual(gate.main([*argv, "--enforce"]), 1)  # enforce 違規 → 1

    def test_in_scope_zero_in_both_modes(self) -> None:
        from paulsha_cortex.persona import gate

        self._patch_diff(gate, ["paulsha_cortex/persona/gate.py"])  # in-scope
        with tempfile.TemporaryDirectory() as d:
            manifest = _write_manifest(Path(d))
            argv = ["--role", "builder", "--base", "main",
                    "--head", "feature/x", "--manifest", str(manifest)]
            self.assertEqual(gate.main(argv), 0)
            self.assertEqual(gate.main([*argv, "--enforce"]), 0)


if __name__ == "__main__":
    unittest.main()
