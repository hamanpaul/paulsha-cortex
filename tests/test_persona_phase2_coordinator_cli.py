from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path


class JobRegistryTests(unittest.TestCase):
    def test_create_get_update_deterministic_id(self) -> None:
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            job1 = reg.create_job(
                task="mytask", persona="builder",
                branch="feature/mytask", pane="%1", worktree="/wt/mytask",
            )
            # 確定性 job_id：task + 單調計數器，非時間/亂數
            self.assertEqual(job1["job_id"], "mytask-1")
            self.assertEqual(job1["task"], "mytask")
            self.assertEqual(job1["persona"], "builder")
            self.assertEqual(job1["branch"], "feature/mytask")
            self.assertEqual(job1["pane"], "%1")
            self.assertEqual(job1["worktree"], "/wt/mytask")
            self.assertEqual(job1["status"], "dispatched")
            self.assertIn("created_at", job1)

            # 同 task 再建 → 單調遞增，不撞號
            job2 = reg.create_job(
                task="mytask", persona="builder",
                branch="feature/mytask-2", pane="%2", worktree="/wt/mytask-2",
            )
            self.assertEqual(job2["job_id"], "mytask-2")

            self.assertEqual(reg.get_job("mytask-1"), job1)
            self.assertEqual(len(reg.list_jobs()), 2)

            updated = reg.update_status("mytask-1", "done")
            self.assertEqual(updated["status"], "done")
            self.assertEqual(reg.get_job("mytask-1")["status"], "done")

    def test_update_status_rejects_unknown(self) -> None:
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            reg.create_job(task="t", persona="builder",
                           branch="b", pane="%1", worktree="/wt/t")
            with self.assertRaises(ValueError):
                reg.update_status("t-1", "bogus")        # 非法 status
            with self.assertRaises(KeyError):
                reg.update_status("nope-9", "done")       # 不存在 job

    def test_persistence_round_trip(self) -> None:
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            reg = JobRegistry(state_path=state)
            reg.create_job(task="alpha", persona="builder",
                           branch="feature/alpha", pane="%3", worktree="/wt/alpha")
            # 新 registry 指向同一檔 → 讀回
            reg2 = JobRegistry(state_path=state)
            jobs = reg2.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["job_id"], "alpha-1")
            self.assertEqual(reg2.get_job("alpha-1")["worktree"], "/wt/alpha")
            # 重載後新 job 續編、不撞號
            job_b = reg2.create_job(task="alpha", persona="builder",
                                    branch="feature/alpha-b", pane="%4", worktree="/wt/alpha-b")
            self.assertEqual(job_b["job_id"], "alpha-2")

    def test_corrupt_state_fails_closed(self) -> None:
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            state.write_text("{ this is not valid json", encoding="utf-8")
            with self.assertRaises(ValueError):
                JobRegistry(state_path=state)   # fail-closed：不可靜默清空

    def test_non_dict_job_element_fails_closed(self) -> None:
        from paulsha_cortex.coordinator.registry import JobRegistry

        # 結構性損壞但 JSON 合法：jobs 內含非 dict 元素 → 必須 fail-closed（明確 branded 訊息）
        for jobs in (["not-a-dict", 42], [123], [None], [[["a", "b"]]]):
            with tempfile.TemporaryDirectory() as d:
                state = Path(d) / "jobs.json"
                state.write_text(
                    json.dumps({"seq": 1, "jobs": jobs}), encoding="utf-8"
                )
                with self.assertRaises(ValueError) as ctx:
                    JobRegistry(state_path=state)
                # MUST 為 fail-closed branded 訊息，非 dict() 拋的混淆訊息，且不可靜默接受
                self.assertIn("fail-closed", str(ctx.exception))

    def test_missing_state_is_empty_registry(self) -> None:
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "absent.json")
            self.assertEqual(reg.list_jobs(), [])   # 不存在非錯誤


# ---- fakes（dispatcher / cli 測試共用；真實作不進任何測試）----
class FakePaneSender:
    """記錄 send 呼叫的 fake；結構相容 seams.PaneSender。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))


class FakeWorktreeCreator:
    """回固定路徑並記錄 branch 的 fake；結構相容 seams.WorktreeCreator。"""

    def __init__(self, root: str = "/fake/wt") -> None:
        self.root = root
        self.created: list[str] = []

    def create(self, branch: str) -> str:
        self.created.append(branch)
        return f"{self.root}/{branch.replace('/', '-')}"


class SeamProtocolTests(unittest.TestCase):
    def test_fakes_satisfy_protocols(self) -> None:
        from paulsha_cortex.coordinator import seams

        sender: seams.PaneSender = FakePaneSender()
        creator: seams.WorktreeCreator = FakeWorktreeCreator()
        sender.send("%9", "hello")
        path = creator.create("feature/x")
        self.assertEqual(path, "/fake/wt/feature-x")
        # 真實作存在且為對應型別（不呼叫其副作用方法）
        self.assertTrue(hasattr(seams, "TmuxPaneSender"))
        self.assertTrue(hasattr(seams, "ScriptWorktreeCreator"))


class _RaisingWorktreeCreator:
    def create(self, branch: str) -> str:
        raise ValueError("boom: worktree add failed")


class DispatcherTests(unittest.TestCase):
    def _make(self, tmp: Path):
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.registry import JobRegistry

        reg = JobRegistry(state_path=tmp / "jobs.json")
        sender = FakePaneSender()
        creator = FakeWorktreeCreator()
        return Dispatcher(reg, sender, creator), reg, sender, creator

    def test_dispatch_records_job_and_sends_command(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            disp, reg, sender, creator = self._make(Path(d))
            command = 'copilot --model gpt-5.4 --yolo -p "<contract+PROMPT>"'
            job = disp.dispatch(task="slice-a", persona="builder",
                                pane_id="%5", command=command)

            self.assertEqual(job["job_id"], "slice-a-1")
            self.assertEqual(job["status"], "dispatched")
            self.assertEqual(job["pane"], "%5")
            # worktree 被建立、branch 由 task 推導
            self.assertEqual(creator.created, ["feature/slice-a"])
            self.assertEqual(job["worktree"], "/fake/wt/feature-slice-a")
            self.assertEqual(job["branch"], "feature/slice-a")
            # 送入 pane 的文字 = 呼叫者給的 command（一字不差）
            self.assertEqual(sender.sent, [("%5", command)])
            # registry 確實記了
            self.assertEqual(len(reg.list_jobs()), 1)
            self.assertEqual(reg.get_job("slice-a-1")["status"], "dispatched")

    def test_multiple_dispatch_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            disp, reg, sender, creator = self._make(Path(d))
            j1 = disp.dispatch(task="a", persona="builder", pane_id="%1", command="cmd-a")
            j2 = disp.dispatch(task="b", persona="builder", pane_id="%2", command="cmd-b")
            # job_id 用 registry-wide 單調計數器：a→1、b→2（確定性、跨 task 唯一）
            self.assertEqual(j1["job_id"], "a-1")
            self.assertEqual(j2["job_id"], "b-2")
            self.assertEqual({j["job_id"] for j in reg.list_jobs()}, {"a-1", "b-2"})
            self.assertEqual(creator.created, ["feature/a", "feature/b"])
            self.assertEqual(sender.sent, [("%1", "cmd-a"), ("%2", "cmd-b")])

    def test_worktree_failure_records_no_job_and_sends_nothing(self) -> None:
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            sender = FakePaneSender()
            disp = Dispatcher(reg, sender, _RaisingWorktreeCreator())
            with self.assertRaises(ValueError):
                disp.dispatch(task="x", persona="builder", pane_id="%9", command="cmd")
            # fail-closed：不送命令、不記 job
            self.assertEqual(sender.sent, [])
            self.assertEqual(reg.list_jobs(), [])

    def test_poll_done_marks_done_on_new_commit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            disp, reg, sender, creator = self._make(Path(d))
            # baseline head 記在 dispatch 時（fake git_runner 回 baseline，持久化於 job）
            job = disp.dispatch(task="c", persona="builder", pane_id="%3", command="cmd-c",
                                git_runner=lambda args: "baselinehead")
            # 之後 git_runner 回新 head（異於 dispatch_head）→ 標 done
            new_head_runner = lambda args: "deadbeefcafe"
            updated = disp.poll_done(job["job_id"], git_runner=new_head_runner)
            self.assertEqual(updated["status"], "done")
            self.assertEqual(reg.get_job("c-1")["status"], "done")

    def test_poll_done_no_new_commit_keeps_status(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            disp, reg, sender, creator = self._make(Path(d))
            # dispatch 時以固定 head 記 baseline；poll 回同 head → 維持 dispatched
            disp.dispatch(task="e", persona="builder", pane_id="%4", command="cmd-e",
                          git_runner=lambda args: "samehead")
            updated = disp.poll_done("e-1", git_runner=lambda args: "samehead")
            self.assertEqual(updated["status"], "dispatched")

    def test_dispatch_persists_dispatch_head_on_job(self) -> None:
        # D5：baseline（dispatch 當下的 branch head）MUST 記在 job 上（持久化），
        # 而非只存在 Dispatcher 實例的記憶體 dict。
        with tempfile.TemporaryDirectory() as d:
            disp, reg, sender, creator = self._make(Path(d))
            job = disp.dispatch(task="f", persona="builder", pane_id="%5",
                                command="cmd-f", git_runner=lambda args: "baseline-f")
            self.assertEqual(job["dispatch_head"], "baseline-f")
            # 持久化：指向同檔的新 registry 讀回 dispatch_head 一致
            self.assertEqual(reg.get_job("f-1")["dispatch_head"], "baseline-f")

    def test_poll_done_survives_process_boundary(self) -> None:
        # 設計 CLI 用法：dispatch 與後續 poll 為兩次獨立進程 → 各自全新 Dispatcher，
        # 記憶體 baseline dict 為空。poll_done MUST 從 job 記錄讀 dispatch_head，
        # 故同 head（零新 commit）時 MUST 維持 dispatched，不可誤標 done。
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.registry import JobRegistry

        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            reg1 = JobRegistry(state_path=state)
            disp1 = Dispatcher(reg1, FakePaneSender(), FakeWorktreeCreator())
            disp1.dispatch(task="g", persona="builder", pane_id="%6",
                           command="cmd-g", git_runner=lambda args: "same-g")

            # 全新進程：新 registry（同檔）+ 新 Dispatcher（_baseline_head 為空）
            reg2 = JobRegistry(state_path=state)
            disp2 = Dispatcher(reg2, FakePaneSender(), FakeWorktreeCreator())
            same = disp2.poll_done("g-1", git_runner=lambda args: "same-g")
            self.assertEqual(same["status"], "dispatched")   # 零新 commit → 維持
            # 真有新 commit（head 異於記錄的 dispatch_head）→ 標 done
            done = disp2.poll_done("g-1", git_runner=lambda args: "new-g")
            self.assertEqual(done["status"], "done")
            self.assertEqual(reg2.get_job("g-1")["status"], "done")

    def test_poll_done_none_baseline_does_not_autocomplete(self) -> None:
        # dispatch_head 為 None（dispatch 時取不到 head）→ poll_done MUST NOT 自動完成。
        with tempfile.TemporaryDirectory() as d:
            disp, reg, sender, creator = self._make(Path(d))

            def boom(args):
                raise RuntimeError("rev-parse failed at dispatch")

            job = disp.dispatch(task="h", persona="builder", pane_id="%7",
                                command="cmd-h", git_runner=boom)
            self.assertIsNone(job["dispatch_head"])
            updated = disp.poll_done("h-1", git_runner=lambda args: "any-head")
            self.assertEqual(updated["status"], "dispatched")   # baseline 不明 → 不自動完成


class CliTests(unittest.TestCase):
    def _fakes(self, tmp: Path):
        from paulsha_cortex.coordinator.registry import JobRegistry

        reg = JobRegistry(state_path=tmp / "jobs.json")
        return reg, FakePaneSender(), FakeWorktreeCreator()

    def test_main_dispatch_with_fakes(self) -> None:
        from paulsha_cortex.coordinator import cli

        with tempfile.TemporaryDirectory() as d:
            reg, sender, creator = self._fakes(Path(d))
            command = 'copilot --model gpt-5.4 --yolo -p "go"'
            argv = ["dispatch", "--task", "slice-z", "--persona", "builder",
                    "--pane", "%7", "--command", command]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(argv, registry=reg, pane_sender=sender, worktree_creator=creator)
            self.assertEqual(rc, 0)
            # 送出的命令一字不差
            self.assertEqual(sender.sent, [("%7", command)])
            # registry 多一筆 job 且 stdout 為該 job 的 JSON
            self.assertEqual(len(reg.list_jobs()), 1)
            printed = json.loads(buf.getvalue())
            self.assertEqual(printed["job_id"], "slice-z-1")
            self.assertEqual(printed["pane"], "%7")

    def test_main_jobs_and_stat(self) -> None:
        from paulsha_cortex.coordinator import cli

        with tempfile.TemporaryDirectory() as d:
            reg, sender, creator = self._fakes(Path(d))
            cli.main(["dispatch", "--task", "j", "--persona", "builder",
                      "--pane", "%1", "--command", "c"],
                     registry=reg, pane_sender=sender, worktree_creator=creator)

            # jobs：列出既有 job
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["jobs"], registry=reg, pane_sender=sender, worktree_creator=creator)
            self.assertEqual(rc, 0)
            listed = json.loads(buf.getvalue())
            self.assertEqual([j["job_id"] for j in listed], ["j-1"])

            # stat：存在的 job 回 0
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["stat", "j-1"], registry=reg, pane_sender=sender, worktree_creator=creator)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["job_id"], "j-1")

            # stat：不存在的 job 回非零
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(["stat", "nope-9"], registry=reg, pane_sender=sender, worktree_creator=creator)
            self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
