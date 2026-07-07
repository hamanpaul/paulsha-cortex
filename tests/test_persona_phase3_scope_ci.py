from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PERSONAS_YAML = REPO_ROOT / "paulsha_cortex" / "persona" / "personas.yaml"


def _valid_manifest(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "from_role": "builder",
        "to_role": "reviewer",
        "phase": "review",
        "gate_status": "passed",
        "slice_id": "persona-phase3-scope-gate",
        "summary": "phase3 scope ci shadow",
        "artifact_refs": ["feature/persona-phase3-scope-gate"],
        "created_at": "2026-06-18T00:00:00+08:00",
        "base": "main",
        "head": "feature/persona-phase3-scope-gate",
    }
    payload.update(overrides)
    return payload


def _write_manifest(repo_root: Path, **overrides: object) -> Path:
    from paulsha_cortex.persona import handoff

    path = repo_root / "runtime" / "handoff" / "persona-phase3-scope-gate.json"
    handoff.write_manifest(path, _valid_manifest(**overrides))
    return path


def _run(repo_root: Path, env: dict[str, str]) -> tuple[int, dict[str, object]]:
    """跑 scope_ci.main 並擷取 stdout JSON。"""
    from paulsha_cortex.persona import scope_ci

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = scope_ci.main(argv=["--repo", str(repo_root)], env=env)
    out = buf.getvalue().strip().splitlines()
    payload = json.loads(out[-1]) if out else {}
    return code, payload


class NoManifestTests(unittest.TestCase):
    def test_empty_handoff_dir_skips_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / "runtime" / "handoff").mkdir(parents=True)  # 空目錄
            code, payload = _run(repo, {"GITHUB_BASE_REF": "main"})
            self.assertEqual(code, 0)
            self.assertTrue(payload.get("skipped"))
            self.assertIn("skipped", json.dumps(payload))

    def test_no_runtime_dir_at_all_skips_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)  # 連 runtime/ 都沒有 → 仍須乾淨跳過、不報錯
            code, payload = _run(repo, {})
            self.assertEqual(code, 0)
            self.assertTrue(payload.get("skipped"))


class InScopeShadowTests(unittest.TestCase):
    def _patch_diff(self, paths: list[str]):
        from paulsha_cortex.persona import gate

        original = gate.compute_changed_paths
        gate.compute_changed_paths = lambda base, head, repo=None: list(paths)
        self.addCleanup(setattr, gate, "compute_changed_paths", original)

    def test_in_scope_diff_ok_and_exit_zero(self) -> None:
        self._patch_diff(["paulsha_cortex/persona/scope_ci.py", "tests/test_x.py"])
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write_manifest(repo)  # from_role=builder
            code, payload = _run(repo, {"GITHUB_BASE_REF": "main", "GITHUB_SHA": "deadbeef"})
            self.assertEqual(code, 0)
            self.assertEqual(payload["role"], "builder")
            self.assertEqual(payload["violations"], [])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "shadow")
            self.assertEqual(payload["base"], "origin/main")
            self.assertEqual(payload["head"], "deadbeef")


class OutOfScopeShadowTests(unittest.TestCase):
    def _patch_diff(self, paths: list[str]):
        from paulsha_cortex.persona import gate

        original = gate.compute_changed_paths
        gate.compute_changed_paths = lambda base, head, repo=None: list(paths)
        self.addCleanup(setattr, gate, "compute_changed_paths", original)

    def test_out_of_scope_diff_violations_but_still_exit_zero(self) -> None:
        # builder 不可寫 docs/** → 越界
        self._patch_diff(["paulsha_cortex/persona/scope_ci.py", "docs/secret.md"])
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write_manifest(repo)  # from_role=builder
            code, payload = _run(repo, {"GITHUB_BASE_REF": "main"})
            self.assertEqual(code, 0)  # shadow 恆 0，即使越界
            self.assertFalse(payload["ok"])
            offending = [v["path"] for v in payload["violations"]]
            self.assertIn("docs/secret.md", offending)
            for v in payload["violations"]:
                self.assertTrue(v["reason"])  # 帶可審計原因

    def test_diff_failure_fail_closed_but_shadow_exit_zero(self) -> None:
        from paulsha_cortex.persona import gate

        def _raise(base, head, repo=None):
            raise RuntimeError("git diff 失敗（fail-closed）: no merge base")

        original = gate.compute_changed_paths
        gate.compute_changed_paths = _raise
        self.addCleanup(setattr, gate, "compute_changed_paths", original)

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write_manifest(repo)
            code, payload = _run(repo, {"GITHUB_BASE_REF": "main"})
            self.assertEqual(code, 0)  # shadow 仍放行
            self.assertIn("diff_error", payload)
            self.assertFalse(payload["ok"])


class CatalogErrorShadowTests(unittest.TestCase):
    """壞掉的 personas.yaml 不得讓 shadow workflow 報錯（設計 §8 硬安全保證）。"""

    def test_main_with_broken_catalog_emits_catalog_error_and_exit_zero(self) -> None:
        from paulsha_cortex.persona import loader, scope_ci

        def _raise(path=None):
            raise ValueError("persona catalog 解析失敗: broken")

        original = loader.load_catalog
        loader.load_catalog = _raise
        # scope_ci 以 `from .loader import load_catalog` 綁定 → 須同步 patch 模組屬性
        original_sc = scope_ci.load_catalog
        scope_ci.load_catalog = _raise
        self.addCleanup(setattr, loader, "load_catalog", original)
        self.addCleanup(setattr, scope_ci, "load_catalog", original_sc)

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write_manifest(repo)  # manifest 存在，但 catalog 壞掉
            code, payload = _run(repo, {"GITHUB_BASE_REF": "main"})
            self.assertEqual(code, 0)  # shadow 恆 0，即使 catalog 壞掉
            self.assertIn("catalog_error", payload)
            self.assertTrue(payload["catalog_error"])
            self.assertFalse(payload["ok"])

    def test_subprocess_with_malformed_yaml_still_exits_zero(self) -> None:
        """以真實 entry point (`python -m ...`) 驗 import-time 不報錯。

        壞掉的 personas.yaml 在 import 期（__init__ → context → contract:174）
        即 raise，早於 main() 的 guard；故須以 subprocess 還原真實 workflow 呼叫。
        """
        original = PERSONAS_YAML.read_text(encoding="utf-8")
        self.addCleanup(PERSONAS_YAML.write_text, original, "utf-8")
        try:
            with tempfile.TemporaryDirectory() as d:
                # 故意寫入 schema-malformed YAML（builder 自己 write scope 內的常見人為錯誤）
                PERSONAS_YAML.write_text("roles: [unclosed\n", encoding="utf-8")
                proc = subprocess.run(
                    [sys.executable, "-m", "paulsha_cortex.persona.scope_ci", "--repo", d],
                    capture_output=True,
                    text=True,
                    cwd=str(REPO_ROOT),
                    env={**os.environ, "GITHUB_BASE_REF": "main"},
                )
        finally:
            PERSONAS_YAML.write_text(original, encoding="utf-8")
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"shadow entry point 必須恆 exit 0；stderr=\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
