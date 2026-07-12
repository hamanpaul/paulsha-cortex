from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.autonomy import dispatch_ready
from paulsha_cortex.coordinator.registry import JobRegistry


class FakeDispatcher:
    """包真 JobRegistry；poll_headless_done 依 poll_map 腳本化轉態。"""

    def __init__(self, registry: JobRegistry, poll_map: dict | None = None,
                 raise_on: set | None = None) -> None:
        self._registry = registry
        self._poll_map = poll_map or {}   # job_id -> "exited"/"failed"
        self._raise_on = raise_on or set()  # job_id -> 模擬 poll 例外

    def poll_headless_done(self, job_id: str) -> dict:
        if job_id in self._raise_on:
            raise RuntimeError(f"poll 爆炸: {job_id}")
        status = self._poll_map.get(job_id)
        if status is None:
            return self._registry.get_job(job_id)  # 仍在跑
        return self._registry.update_headless_result(
            job_id, status=status, exit_code=0 if status == "exited" else 1
        )


def _reg(tmp: str) -> JobRegistry:
    return JobRegistry(state_path=Path(tmp) / "jobs.json")


def _make_job(reg: JobRegistry, slice_id: str) -> dict:
    return reg.create_job(
        task=slice_id, persona="builder", branch=f"feature/{slice_id}",
        pane="", worktree=f"/wt/{slice_id}",
        executor="copilot", session_name=slice_id, pid=4242,
        log_path=f"/logs/{slice_id}.jsonl",
    )


class CompleteTickDoneTests(unittest.TestCase):
    def test_exited_job_writes_passed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-a")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads((hdir / "slice-a.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(manifest["completion"], "exited")
            self.assertEqual(manifest["slice_id"], "slice-a")
            self.assertEqual(manifest["completed_at"], "T0")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-a", "gate_status": "passed"}])
            self.assertEqual(summary["errors"], [])


class CompleteTickFailedAndInFlightTests(unittest.TestCase):
    def test_failed_job_writes_failed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-b")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "failed"})
            hdir = Path(d) / "handoff"
            manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            manifest = json.loads((hdir / "slice-b.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "failed")
            self.assertEqual(manifest["completion"], "failed")

    def test_in_flight_job_not_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-c")
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir))
            self.assertFalse((hdir / "slice-c.json").exists())
            self.assertEqual(summary["completed"], [])
            self.assertIn(job["job_id"], summary["polled"])


class CompleteTickReconcileTests(unittest.TestCase):
    def test_terminal_job_missing_manifest_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-d")
            reg.update_headless_result(job["job_id"], status="exited", exit_code=0)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertTrue((hdir / "slice-d.json").exists())
            self.assertEqual(summary["completed"], [{"slice_id": "slice-d", "gate_status": "passed"}])
            self.assertEqual(summary["polled"], [])

    def test_same_job_rescan_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-e")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            manifest_path = hdir / "slice-e.json"
            first_text = manifest_path.read_text(encoding="utf-8")
            first_mtime_ns = manifest_path.stat().st_mtime_ns
            second = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T1")
            second_text = manifest_path.read_text(encoding="utf-8")
            second_mtime_ns = manifest_path.stat().st_mtime_ns
            manifest = json.loads(second_text)
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["completed_at"], "T0")
            self.assertEqual(first_text, second_text)
            self.assertEqual(first_mtime_ns, second_mtime_ns)
            self.assertEqual(second["completed"], [])
            self.assertEqual(second["polled"], [])

    def test_concurrent_same_slice_terminals_warn_and_dedup(self) -> None:
        # 不變量已由 registry 擋住；此處人工污染狀態檔，釘住 complete_tick 面對既有壞狀態
        # 仍會去重與記 warning 的降級行為。
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            first = _make_job(reg, "slice-dup")
            second = {**first, "job_id": "slice-dup-2", "worktree": "/wt/slice-dup-2"}
            reg._jobs.append(second)
            disp = FakeDispatcher(
                reg,
                poll_map={first["job_id"]: "failed", second["job_id"]: "exited"},
            )
            hdir = Path(d) / "handoff"

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            # 去重：兩個 terminal job 同 slice 只回一筆 completed。
            self.assertEqual(len(summary["completed"]), 1)
            self.assertEqual(summary["completed"][0]["slice_id"], "slice-dup")
            # warning 恰記一次。
            self.assertEqual(
                summary["warnings"],
                [{"slice_id": "slice-dup", "warning": "same-slice concurrent terminals"}],
            )
            # 後者勝一致性：manifest 與 completed 的 gate_status 一致，job_id 為兩者之一。
            manifest = json.loads((hdir / "slice-dup.json").read_text(encoding="utf-8"))
            self.assertIn(manifest["job_id"], {first["job_id"], second["job_id"]})
            self.assertEqual(summary["completed"][0]["gate_status"], manifest["gate_status"])

    def test_requeue_overwrites_manifest_for_new_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            first = _make_job(reg, "slice-requeue")
            disp = FakeDispatcher(reg, poll_map={first["job_id"]: "failed"})
            hdir = Path(d) / "handoff"

            manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            second_job = _make_job(reg, "slice-requeue")
            disp = FakeDispatcher(
                reg,
                poll_map={first["job_id"]: "failed", second_job["job_id"]: "exited"},
            )
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T1")

            manifest = json.loads((hdir / "slice-requeue.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], second_job["job_id"])
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(manifest["completion"], "exited")
            self.assertEqual(manifest["completed_at"], "T1")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-requeue", "gate_status": "passed"}])
            from paulsha_cortex.coordinator import autonomy
            self.assertTrue(autonomy.default_is_satisfied("slice-requeue", handoff_dir=str(hdir)))

    def test_legacy_manifest_without_job_id_is_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-legacy")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-legacy.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "slice_id": "slice-legacy",
                        "gate_status": "failed",
                        "completion": "failed",
                        "exit_code": 1,
                        "branch": "feature/legacy",
                        "gate_verdict": None,
                        "completed_at": "OLD",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(manifest["completed_at"], "T0")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-legacy", "gate_status": "passed"}])

    def test_corrupt_manifest_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-corrupt")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-corrupt.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text("{not json", encoding="utf-8")

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-corrupt", "gate_status": "passed"}])
            self.assertEqual(summary["errors"], [])

    def test_invalid_utf8_manifest_is_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-invalid-utf8")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-invalid-utf8.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(b"\x80not-utf8")

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["job_id"], job["job_id"])
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-invalid-utf8", "gate_status": "passed"}])
            self.assertEqual(summary["errors"], [])

    def test_symlink_manifest_path_is_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-symlink")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "slice-symlink.json"
            target_path = Path(d) / "outside.json"
            target_path.write_text('{"outside": true}\n', encoding="utf-8")
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.symlink_to(target_path)

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")

            self.assertEqual(target_path.read_text(encoding="utf-8"), '{"outside": true}\n')
            self.assertEqual(summary["completed"], [])
            self.assertEqual([e["job_id"] for e in summary["errors"]], [job["job_id"]])

    def test_symlink_handoff_dir_still_writes_manifest(self) -> None:
        # 迴歸釘死：本專案部署 handoff_dir 落在 symlink 樹下（~/.agents → ~/notes/...）。
        # complete_tick MUST 正常寫盤，不得因上層 symlink 誤拒（原 _is_safe_handoff_root P0）。
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-hdir-link")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            real_dir = Path(d) / "real_state"
            real_dir.mkdir()
            hdir = Path(d) / "agents_link"  # symlink → real_state
            hdir.symlink_to(real_dir, target_is_directory=True)

            summary = manager.complete_tick(disp, handoff_dir=str(hdir / "handoff"), clock=lambda: "T0")

            manifest = json.loads((real_dir / "handoff" / "slice-hdir-link.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(summary["completed"], [{"slice_id": "slice-hdir-link", "gate_status": "passed"}])
            self.assertEqual(summary["errors"], [])


class CompleteTickShadowGateTests(unittest.TestCase):
    def test_shadow_gate_verdict_recorded_but_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-f")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            fake_gate = lambda j: {"ok": False, "violations": [{"path": "x", "reason": "out"}],
                                   "handoff_ok": False}
            manager.complete_tick(disp, gate_runner=fake_gate, handoff_dir=str(hdir), clock=lambda: "T0")
            manifest = json.loads((hdir / "slice-f.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertEqual(manifest["gate_verdict"]["ok"], False)

    def test_gate_runner_exception_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-g")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            def boom(j):
                raise RuntimeError("gate 爆炸")
            manager.complete_tick(disp, gate_runner=boom, handoff_dir=str(hdir), clock=lambda: "T0")
            manifest = json.loads((hdir / "slice-g.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["gate_status"], "passed")
            self.assertIsNone(manifest["gate_verdict"])


class CompleteTickErrorAndReleaseTests(unittest.TestCase):
    def test_per_job_poll_error_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            a = _make_job(reg, "slice-h")
            b = _make_job(reg, "slice-i")
            disp = FakeDispatcher(reg, poll_map={b["job_id"]: "exited"}, raise_on={a["job_id"]})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertTrue((hdir / "slice-i.json").exists())
            self.assertFalse((hdir / "slice-h.json").exists())
            self.assertEqual(summary["completed"], [{"slice_id": "slice-i", "gate_status": "passed"}])
            self.assertEqual([e["job_id"] for e in summary["errors"]], [a["job_id"]])

    def test_downstream_released_after_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            up = _make_job(reg, "up")
            disp = FakeDispatcher(reg, poll_map={up["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            metas = [
                {"slice_id": "up", "dispatch": "auto", "plan": "p-up.md", "depends_on": []},
                {"slice_id": "down", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["up"]},
            ]
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), metas=metas, clock=lambda: "T0")
            self.assertIn("down", summary["released"])
            from paulsha_cortex.coordinator import autonomy
            self.assertTrue(autonomy.default_is_satisfied("up", handoff_dir=str(hdir)))

    def test_invalid_utf8_manifest_repair_still_reports_released(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            up = _make_job(reg, "up")
            disp = FakeDispatcher(reg, poll_map={up["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "up.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(b"\x80not-utf8")
            metas = [
                {"slice_id": "down", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["up"]},
            ]

            summary = manager.complete_tick(disp, handoff_dir=str(hdir), metas=metas, clock=lambda: "T0")

            self.assertEqual(summary["completed"], [{"slice_id": "up", "gate_status": "passed"}])
            self.assertIn("down", summary["released"])
            self.assertEqual(summary["errors"], [])


class CompleteTickGuardTests(unittest.TestCase):
    def test_job_without_valid_slice_id_goes_to_errors(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "slice-x")
            # 直接污染 registry 內部 job 的 task 成 None，模擬 corrupt 狀態
            reg._jobs[0]["task"] = None
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertFalse((hdir / "None.json").exists())
            self.assertEqual(summary["completed"], [])
            self.assertEqual([e["job_id"] for e in summary["errors"]], [job["job_id"]])

    def test_cyclic_metas_does_not_crash_tick(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "a")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            cyclic = [
                {"slice_id": "a", "dispatch": "auto", "plan": "pa.md", "depends_on": ["b"]},
                {"slice_id": "b", "dispatch": "auto", "plan": "pb.md", "depends_on": ["a"]},
            ]
            summary = manager.complete_tick(disp, handoff_dir=str(hdir), metas=cyclic, clock=lambda: "T0")
            # 完成側仍寫出 manifest；released 因環被停用而省略
            self.assertTrue((hdir / "a.json").exists())
            self.assertEqual(summary["completed"], [{"slice_id": "a", "gate_status": "passed"}])
            self.assertNotIn("released", summary)

    def test_unsafe_slice_id_rejected_no_escape_write(self) -> None:
        for bad in ["../evil", "/abs/evil", "a/b", "..", ".", "x/../y", "with space"]:
            with tempfile.TemporaryDirectory() as d:
                reg = _reg(d)
                job = _make_job(reg, "ok")
                reg._jobs[0]["task"] = bad   # 模擬不安全/corrupt slice_id
                disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
                hdir = Path(d) / "handoff"
                summary = manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
                self.assertEqual(summary["completed"], [], f"{bad!r} 應被拒")
                self.assertEqual([e["job_id"] for e in summary["errors"]], [job["job_id"]])
                # 確認沒有任何檔案被寫到 hdir 外或 hdir 內
                self.assertFalse(hdir.exists() and any(hdir.iterdir()), f"{bad!r} 不應寫出檔案")


class RunTickTests(unittest.TestCase):
    def test_not_idle_skips_fanout_but_still_completes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "x")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], require_idle=True, max_load=1.0,
                idle_probe=lambda: (99.0, 99.0, 99.0), handoff_dir=str(hdir), clock=lambda: "T0",
            )
            # fanout 被 idle gate 擋，但完成側仍跑（review F-C）
            self.assertEqual(summary["dispatch_skipped"], "not-idle")
            self.assertEqual(summary["dispatched"], [])
            self.assertEqual(summary["completed"], [{"slice_id": "x", "gate_status": "passed"}])
            self.assertTrue((hdir / "x.json").exists())

    def test_runs_fanout_and_complete_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "y")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], require_idle=True, max_load=1.0,
                idle_probe=lambda: (0.0, 0.0, 0.0), handoff_dir=str(hdir), clock=lambda: "T0",
            )
            self.assertFalse(summary["dispatch_skipped"])
            self.assertEqual(summary["completed"], [{"slice_id": "y", "gate_status": "passed"}])
            self.assertTrue((hdir / "y.json").exists())

    def test_fanout_failure_does_not_block_complete(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "done-slice")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            metas = [{"slice_id": "ready-one", "dispatch": "auto", "plan": "p.md", "depends_on": []}]
            summary = manager.run_tick(
                disp, metas=metas, launcher=None, is_satisfied=lambda s: True,
                handoff_dir=str(hdir), clock=lambda: "T0",
            )
            self.assertFalse(summary["dispatch_skipped"])
            self.assertTrue(any(e.get("stage") == "fanout" for e in summary["errors"]))
            self.assertEqual(summary["completed"], [{"slice_id": "done-slice", "gate_status": "passed"}])

    def test_invalid_utf8_dependency_manifest_does_not_create_fanout_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            manifest_path = hdir / "up.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(b"\x80not-utf8")
            metas = [
                {"slice_id": "down", "dispatch": "auto", "plan": "p-down.md", "depends_on": ["up"]},
            ]

            summary = manager.run_tick(
                disp, metas=metas, launcher=None, handoff_dir=str(hdir), clock=lambda: "T0",
            )

            self.assertEqual(summary["dispatched"], [])
            self.assertFalse(any(e.get("stage") == "fanout" for e in summary["errors"]))


    def test_in_flight_slice_not_redispatched(self) -> None:
        # slice 已有 dispatched job → 本趟 fanout 不得再對它派工（review F-A 冪等）
        class _RecordingLauncher:
            def __init__(self) -> None:
                self.launched: list[str] = []

            def launch(self, *, slice_id, prompt, worktree, log_dir):  # pragma: no cover
                self.launched.append(slice_id)
                raise AssertionError(f"in-flight slice 不應被重派: {slice_id}")

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            _make_job(reg, "s")  # status=dispatched（in-flight）
            disp = FakeDispatcher(reg, poll_map={})  # s 維持 dispatched
            hdir = Path(d) / "handoff"
            launcher = _RecordingLauncher()
            metas = [{"slice_id": "s", "dispatch": "auto", "plan": "p.md", "depends_on": []}]
            summary = manager.run_tick(
                disp, metas=metas, launcher=launcher, is_satisfied=lambda x: True,
                handoff_dir=str(hdir), clock=lambda: "T0",
            )
            self.assertEqual(launcher.launched, [])
            self.assertEqual(summary["dispatched"], [])
            self.assertFalse(summary["dispatch_skipped"])

    def test_reaper_result_recorded_in_summary(self) -> None:
        # 收尾 janitor（#161）：傳入 reaper → complete 後呼叫一次，結果進 summary["reaped"]
        calls = []
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "z")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0",
                reaper=lambda: calls.append(1) or {"ran": True, "applied": True, "returncode": 0},
            )
            self.assertEqual(calls, [1])
            self.assertEqual(summary["reaped"], {"ran": True, "applied": True, "returncode": 0})
            self.assertEqual(summary["completed"], [{"slice_id": "z", "gate_status": "passed"}])

    def test_reaper_exception_does_not_break_tick(self) -> None:
        # janitor 失敗一律不破壞 tick：reaped=None、errors 收 stage=reap、完成側照常
        def _boom():
            raise RuntimeError("reap 爆炸")

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            job = _make_job(reg, "w")
            disp = FakeDispatcher(reg, poll_map={job["job_id"]: "exited"})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(
                disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0", reaper=_boom,
            )
            self.assertIsNone(summary["reaped"])
            self.assertTrue(any(e.get("stage") == "reap" for e in summary["errors"]))
            self.assertEqual(summary["completed"], [{"slice_id": "w", "gate_status": "passed"}])

    def test_no_reaper_disables_janitor(self) -> None:
        # 預設不傳 reaper → reaped=None，且不產生 reap 相關 error（單測不誤觸真實回收）
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = FakeDispatcher(reg, poll_map={})
            hdir = Path(d) / "handoff"
            summary = manager.run_tick(disp, metas=[], handoff_dir=str(hdir), clock=lambda: "T0")
            self.assertIsNone(summary["reaped"])
            self.assertFalse(any(e.get("stage") == "reap" for e in summary["errors"]))


class _HeadlessDispatcher:
    """有 _registry 的 fake：供 dispatch_ready 記 job + complete_tick poll。"""

    def __init__(self, registry: JobRegistry) -> None:
        self._registry = registry

    def poll_headless_done(self, job_id: str) -> dict:
        return self._registry.update_headless_result(job_id, status="exited", exit_code=0)


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        from paulsha_cortex.coordinator.launcher import LaunchHandle

        self.calls.append({"slice_id": slice_id, "worktree": worktree})
        return LaunchHandle(
            executor="copilot", session_name=slice_id, pid=100 + len(self.calls),
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


class DispatchHeadBaselineTests(unittest.TestCase):
    """#131：headless 派工側須持久化 dispatch_head，否則 complete_tick 的
    預設 shadow gate 對真實 headless job 恒回 null（對要完成的 job 形同失明）。"""

    def test_dispatch_ready_persists_dispatch_head(self) -> None:
        # 注入 git_runner 回固定 baseline → 應寫進 job 與 registry（修前為 None）
        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = _HeadlessDispatcher(reg)
            launcher = _RecordingLauncher()
            metas = [{"slice_id": "slice-x", "dispatch": "auto", "plan": "p.md", "depends_on": []}]
            jobs = dispatch_ready(
                metas, is_satisfied=lambda _id: True, dispatcher=disp,
                launcher=launcher, git_runner=lambda args: "BASE_SHA",
            )
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["dispatch_head"], "BASE_SHA")
            self.assertEqual(reg.list_jobs()[0]["dispatch_head"], "BASE_SHA")

    def test_dispatch_ready_git_failure_records_none_not_crash(self) -> None:
        # baseline 取不到（git 例外）→ dispatch_head=None，但不破壞派工（graceful）
        def boom(args):
            raise RuntimeError("git 爆炸")

        with tempfile.TemporaryDirectory() as d:
            reg = _reg(d)
            disp = _HeadlessDispatcher(reg)
            launcher = _RecordingLauncher()
            metas = [{"slice_id": "slice-y", "dispatch": "auto", "plan": "p.md", "depends_on": []}]
            jobs = dispatch_ready(
                metas, is_satisfied=lambda _id: True, dispatcher=disp,
                launcher=launcher, git_runner=boom,
            )
            self.assertEqual(len(jobs), 1)
            self.assertIsNone(jobs[0]["dispatch_head"])

    def test_default_gate_verdict_non_null_after_dispatch_through_pipeline(self) -> None:
        # 整合：dispatch_ready 記 baseline → branch 出現新 commit → complete_tick
        # 走「預設」gate runner → 有 git diff 時 verdict 非 null（#131 核心斷言）。
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "tester")
            (repo / "seed.txt").write_text("0\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-qm", "C0")
            git("branch", "feature/slice-x")  # baseline 落在 C0

            reg = JobRegistry(state_path=repo / "jobs.json")
            disp = _HeadlessDispatcher(reg)
            launcher = _RecordingLauncher()
            metas = [{"slice_id": "slice-x", "dispatch": "auto", "plan": "p.md", "depends_on": []}]
            jobs = dispatch_ready(
                metas, is_satisfied=lambda _id: True, dispatcher=disp,
                launcher=launcher, git_runner=lambda args: git(*args),
            )
            self.assertTrue(jobs[0]["dispatch_head"], "baseline 應非 null")

            # 模擬 agent 在 branch 上完成工作（新增 commit C1）
            git("checkout", "-q", "feature/slice-x")
            (repo / "seed.txt").write_text("1\n", encoding="utf-8")
            git("add", "-A")
            git("commit", "-qm", "C1")

            hdir = repo / "handoff"
            cwd = os.getcwd()
            os.chdir(repo)  # 預設 gate runner 的 git diff 無 -C，需在 repo 內跑
            try:
                manager.complete_tick(disp, handoff_dir=str(hdir), clock=lambda: "T0")
            finally:
                os.chdir(cwd)

            manifest = json.loads((hdir / "slice-x.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(manifest["gate_verdict"], "#131：預設 gate 不應再恒 null")
            self.assertIn("seed.txt", manifest["gate_verdict"]["changed_paths"])


if __name__ == "__main__":
    unittest.main()
