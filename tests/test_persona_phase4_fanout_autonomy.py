from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# --------------------------------------------------------------------------- #
# helpers：寫 spec fixture / handoff fixture
# --------------------------------------------------------------------------- #
def _write_spec(dirpath: Path, name: str, frontmatter: str | None, body: str = "x") -> Path:
    """寫一份 spec markdown。frontmatter=None → 無 frontmatter（不以 --- 起頭）。"""
    p = dirpath / name
    if frontmatter is None:
        p.write_text(body + "\n", encoding="utf-8")
    else:
        p.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return p


def _v1_verification_block(*, docs_class: str = "code") -> str:
    return (
        "target_branch: main\n"
        "verification:\n"
        f"  docs_class: {docs_class}\n"
        "  required_artifacts: []\n"
        "  checks:\n"
        "    - kind: persona-scope\n"
        "    - kind: command\n"
        "      name: policy\n"
        "      argv: [python3, -m, pytest, -q]\n"
        "      cwd: .\n"
        "      timeout_seconds: 30\n"
        "  tests: []\n"
        "  full_suite:\n"
        "    argv: [python3, -m, pytest, -q]\n"
        "    cwd: .\n"
        "    timeout_seconds: 60\n"
        "    baseline: no-regression"
    )


def _meta(slice_id, *, dispatch="auto", plan="docs/plan.md", depends_on=None, path=None):
    spec_path = path or f"/specs/{slice_id}.md"
    return {
        "path": spec_path,
        "dispatch": dispatch,
        "slice_id": slice_id,
        "plan": plan,
        "depends_on": list(depends_on or []),
        "target_branch": "main",
        "verification": {
            "docs_class": "code",
            "review_policy": "required",
            "required_artifacts": [],
            "checks": [
                {"kind": "persona-scope"},
                {
                    "kind": "command",
                    "name": "policy",
                    "argv": ["python3", "-m", "pytest", "-q"],
                    "cwd": ".",
                    "timeout_seconds": 300,
                },
            ],
            "tests": [],
            "full_suite": {
                "argv": ["python3", "-m", "pytest", "-q"],
                "cwd": ".",
                "timeout_seconds": 300,
                "baseline": "no-regression",
            },
        },
        "parse_error": None,
        "_pinned_inputs": {
            "spec_path": spec_path,
            "spec_hash": "0" * 64,
            "plan_path": plan or f"/plans/{slice_id}.md",
            "plan_hash": "1" * 64,
            "target_branch": "main",
            "target_remote": "origin",
            "verification_hash": "2" * 64,
        },
    }


def _fake_target_git_runner(args: list[str]):
    if not args:
        return ""
    if args[0] == "rev-parse":
        return "f" * 40
    if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
        return ""
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        return "f" * 40
    if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base" and args[3] == "--is-ancestor":
        return ""
    return ""


class _FakeDispatcher:
    """記錄 dispatch 呼叫的 fake；duck-typed 相容 Phase 2 Dispatcher.dispatch。

    headless fail-fast 後（reviewer #112-3），dispatch_ready fan-out 不再經 pane
    dispatch；保留此 fake 供「無 launcher 應 fail-fast / refuse」一類測試斷言不派工。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def dispatch(self, *, task, persona, pane_id, command):
        job = {
            "job_id": f"{task}-{len(self.calls) + 1}",
            "task": task,
            "persona": persona,
            "pane": pane_id,
            "command": command,
            "status": "dispatched",
        }
        self.calls.append(job)
        return job


class _RecordingLauncher:
    """記錄 launch 呼叫的 fake headless launcher（headless fan-out 路徑）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        from paulsha_cortex.coordinator.launcher import LaunchHandle

        self.calls.append({"slice_id": slice_id, "prompt": prompt, "worktree": worktree})
        return LaunchHandle(
            executor="copilot", model_id=None, session_name=slice_id, pid=100 + len(self.calls),
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


# --------------------------------------------------------------------------- #
# FrontmatterTests
# --------------------------------------------------------------------------- #
class FrontmatterTests(unittest.TestCase):
    def test_parse_target_branch_and_verification_contract(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            p = _write_spec(
                Path(d),
                "verification.md",
                "dispatch: auto\n"
                "slice_id: verification-slice\n"
                "plan: docs/superpowers/plans/verification.md\n"
                "depends_on: [upstream]\n"
                "target_branch: main\n"
                "verification:\n"
                "  docs_class: normative\n"
                "  required_artifacts:\n"
                "    - path: paulsha_cortex/example.py\n"
                "      must_change: true\n"
                "  checks:\n"
                "    - kind: persona-scope\n"
                "    - kind: command\n"
                "      name: policy\n"
                "      argv: [python3, -m, pytest, -q, tests/test_example.py]\n"
                "      cwd: .\n"
                "      timeout_seconds: 300\n"
                "  tests:\n"
                "    - argv: [python3, -m, pytest, -q, tests/test_example.py]\n"
                "      cwd: .\n"
                "      timeout_seconds: 120\n"
                "  full_suite:\n"
                "    argv: [python3, -m, pytest, -q]\n"
                "    cwd: .\n"
                "    timeout_seconds: 600\n"
                "    baseline: no-regression",
            )

            meta = parse_spec_frontmatter(p)

            self.assertEqual(meta["dispatch"], "auto")
            self.assertEqual(meta["target_branch"], "main")
            self.assertIsNone(meta["parse_error"])
            self.assertEqual(meta["verification"]["docs_class"], "normative")
            self.assertEqual(meta["verification"]["review_policy"], "required")
            self.assertEqual(
                meta["verification"]["required_artifacts"],
                [{"path": "paulsha_cortex/example.py", "must_change": True}],
            )
            self.assertEqual(meta["verification"]["checks"][0], {"kind": "persona-scope"})
            self.assertEqual(meta["verification"]["checks"][1]["name"], "policy")
            self.assertEqual(
                meta["verification"]["tests"][0]["argv"],
                ["python3", "-m", "pytest", "-q", "tests/test_example.py"],
            )
            self.assertEqual(meta["verification"]["full_suite"]["baseline"], "no-regression")

    def test_parse_invalid_verification_holds_with_structured_error(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(
                    Path(d),
                    "invalid.md",
                    "dispatch: auto\n"
                    "slice_id: invalid-slice\n"
                    "plan: docs/superpowers/plans/invalid.md\n"
                    "target_branch: main\n"
                    "verification:\n"
                    "  docs_class: code\n"
                    "  required_artifacts: []\n"
                    "  checks:\n"
                    "    - kind: unknown-check\n"
                    "  tests: []\n"
                    "  full_suite:\n"
                    "    argv: [python3, -m, pytest, -q]\n"
                    "    cwd: .\n"
                    "    timeout_seconds: 60\n"
                    "    baseline: no-regression",
                )
            )

            self.assertEqual(meta["dispatch"], "hold")
            self.assertIsInstance(meta["parse_error"], dict)
            self.assertEqual(meta["parse_error"]["code"], "invalid-frontmatter")
            self.assertEqual(meta["parse_error"]["field"], "verification.checks[0].kind")
            self.assertIn("unknown-check", meta["parse_error"]["message"])

    def test_parse_docs_class_sets_review_policy(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        expected = {
            "normative": "required",
            "code": "required",
            "informational": "not-required",
            "trivial": "not-required",
        }
        with tempfile.TemporaryDirectory() as d:
            for docs_class, review_policy in expected.items():
                with self.subTest(docs_class=docs_class):
                    meta = parse_spec_frontmatter(
                        _write_spec(
                            Path(d),
                            f"{docs_class}.md",
                            "dispatch: auto\n"
                            f"slice_id: {docs_class}\n"
                            "plan: docs/superpowers/plans/example.md\n"
                            "target_branch: main\n"
                            "verification:\n"
                            f"  docs_class: {docs_class}\n"
                            "  required_artifacts: []\n"
                            "  checks:\n"
                            "    - kind: persona-scope\n"
                            "    - kind: command\n"
                            "      name: policy\n"
                            "      argv: [python3, -m, pytest, -q]\n"
                            "      cwd: .\n"
                            "      timeout_seconds: 30\n"
                            "  tests: []\n"
                            "  full_suite:\n"
                            "    argv: [python3, -m, pytest, -q]\n"
                            "    cwd: .\n"
                            "    timeout_seconds: 60\n"
                            "    baseline: no-regression",
                        )
                    )
                    self.assertEqual(meta["verification"]["review_policy"], review_policy)

    def test_parse_auto_with_depends_on(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            p = _write_spec(
                Path(d), "a.md",
                "dispatch: auto\n"
                "slice_id: persona-phase1-shadow-gate\n"
                "plan: docs/superpowers/plans/p1.md\n"
                "depends_on: [persona-phase0-config-loader, other]\n"
                + _v1_verification_block(),
            )
            meta = parse_spec_frontmatter(p)
            self.assertEqual(meta["dispatch"], "auto")
            self.assertEqual(meta["slice_id"], "persona-phase1-shadow-gate")
            self.assertEqual(meta["plan"], "docs/superpowers/plans/p1.md")
            self.assertEqual(meta["depends_on"], ["persona-phase0-config-loader", "other"])
            self.assertEqual(meta["path"], str(p))

    def test_parse_hold_and_default(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            hold = parse_spec_frontmatter(
                _write_spec(Path(d), "hold.md", "dispatch: hold\nslice_id: s1")
            )
            self.assertEqual(hold["dispatch"], "hold")
            self.assertEqual(hold["depends_on"], [])

            typo = parse_spec_frontmatter(
                _write_spec(Path(d), "typo.md", "dispatch: AUTO_TYPO\nslice_id: s2")
            )
            self.assertEqual(typo["dispatch"], "hold")  # 非字面 auto → hold

            nokey = parse_spec_frontmatter(
                _write_spec(Path(d), "nokey.md", "slice_id: s3\nplan: docs/p.md")
            )
            self.assertEqual(nokey["dispatch"], "hold")  # 缺 dispatch key → hold
            self.assertEqual(nokey["slice_id"], "s3")

    def test_parse_missing_frontmatter_is_hold(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(Path(d), "plain.md", None, body="# 純內文，無 frontmatter")
            )
            self.assertEqual(meta["dispatch"], "hold")
            self.assertIsNone(meta["slice_id"])
            self.assertIsNone(meta["plan"])
            self.assertEqual(meta["depends_on"], [])

    def test_parse_depends_on_scalar_coerced(self) -> None:
        from paulsha_cortex.coordinator.autonomy import parse_spec_frontmatter

        with tempfile.TemporaryDirectory() as d:
            meta = parse_spec_frontmatter(
                _write_spec(
                    Path(d),
                    "scalar.md",
                    "dispatch: auto\nslice_id: s\nplan: docs/p.md\ndepends_on: only-one\n"
                    + _v1_verification_block(),
                )
            )
            self.assertEqual(meta["depends_on"], ["only-one"])  # 單一字串容錯成 list


# --------------------------------------------------------------------------- #
# ScanTests
# --------------------------------------------------------------------------- #
class ScanTests(unittest.TestCase):
    def test_scan_specs_deterministic(self) -> None:
        from paulsha_cortex.coordinator.autonomy import scan_specs

        with tempfile.TemporaryDirectory() as d:
            _write_spec(Path(d), "b.md", "dispatch: auto\nslice_id: b\nplan: p\n" + _v1_verification_block())
            _write_spec(Path(d), "a.md", "dispatch: hold\nslice_id: a")
            _write_spec(Path(d), "c.md", None)  # 無 frontmatter
            metas = scan_specs(d)
            self.assertEqual(len(metas), 3)
            # 確定性：依 path 排序 → a, b, c
            slugs = [Path(m["path"]).name for m in metas]
            self.assertEqual(slugs, ["a.md", "b.md", "c.md"])

    def test_scan_missing_dir_returns_empty(self) -> None:
        from paulsha_cortex.coordinator.autonomy import scan_specs

        self.assertEqual(scan_specs("/no/such/dir/xyz"), [])


# --------------------------------------------------------------------------- #
# CycleTests
# --------------------------------------------------------------------------- #
class CycleTests(unittest.TestCase):
    def test_detect_cycle_raises(self) -> None:
        from paulsha_cortex.coordinator.autonomy import detect_cycles

        direct = [_meta("A", depends_on=["B"]), _meta("B", depends_on=["A"])]
        with self.assertRaises(ValueError):
            detect_cycles(direct)

        indirect = [
            _meta("A", depends_on=["B"]),
            _meta("B", depends_on=["C"]),
            _meta("C", depends_on=["A"]),
        ]
        with self.assertRaises(ValueError):
            detect_cycles(indirect)

        # 非環圖：A→B、C→B（DAG）→ 不 raise
        acyclic = [
            _meta("A", depends_on=["B"]),
            _meta("B", depends_on=[]),
            _meta("C", depends_on=["B"]),
        ]
        detect_cycles(acyclic)  # MUST NOT raise

    def test_external_dep_not_a_cycle(self) -> None:
        from paulsha_cortex.coordinator.autonomy import detect_cycles

        # depends_on 指向不在 metas 的 id（外部/未掃到）→ 不算環
        detect_cycles([_meta("A", depends_on=["not-scanned"])])  # MUST NOT raise

    def test_ready_units_refuses_on_cycle(self) -> None:
        from paulsha_cortex.coordinator.autonomy import ready_units

        metas = [_meta("A", depends_on=["B"]), _meta("B", depends_on=["A"])]
        with self.assertRaises(ValueError):
            ready_units(metas, is_satisfied=lambda _id: True)

    def test_duplicate_slice_id_refused(self) -> None:
        from paulsha_cortex.coordinator.autonomy import detect_cycles, ready_units

        # 重複 slice_id 本身 → 直接 refuse（身分不明確），不靜默以後者覆寫前者的邊
        dup = [_meta("dupX", depends_on=[]), _meta("dupX", depends_on=[])]
        with self.assertRaisesRegex(ValueError, "重複 slice_id"):
            detect_cycles(dup)
        with self.assertRaisesRegex(ValueError, "重複 slice_id"):
            ready_units(dup, is_satisfied=lambda _id: True)

    def test_duplicate_slice_id_does_not_mask_cycle(self) -> None:
        from paulsha_cortex.coordinator.autonomy import detect_cycles

        # A->B, A->[], B->A：第二個 A 不得覆寫掉 A->B 而遮蔽 A<->B 真環
        masking = [
            _meta("A", depends_on=["B"]),
            _meta("A", depends_on=[]),
            _meta("B", depends_on=["A"]),
        ]
        with self.assertRaises(ValueError):  # 重複 slice_id 先擋下（亦不漏環）
            detect_cycles(masking)


# --------------------------------------------------------------------------- #
# ReadyTests
# --------------------------------------------------------------------------- #
class ReadyTests(unittest.TestCase):
    def test_hold_not_ready(self) -> None:
        from paulsha_cortex.coordinator.autonomy import ready_units

        metas = [
            _meta("held", dispatch="hold"),          # hold → 不就緒
            _meta("noplan", dispatch="auto", plan=None),  # 無 plan → 不就緒
        ]
        ready = ready_units(metas, is_satisfied=lambda _id: True)
        self.assertEqual(ready, [])

    def test_depends_on_gates_readiness(self) -> None:
        from paulsha_cortex.coordinator.autonomy import ready_units

        metas = [
            _meta("free", depends_on=[]),                  # 無相依 → 就緒
            _meta("blocked", depends_on=["upstream"]),     # 相依未滿足 → 不就緒
        ]
        # upstream 未滿足
        ready = ready_units(metas, is_satisfied=lambda _id: _id != "upstream")
        self.assertEqual([m["slice_id"] for m in ready], ["free"])

        # upstream 滿足 → blocked 釋放；確定性序（沿 metas 順序：free 在前、blocked 在後）
        ready2 = ready_units(metas, is_satisfied=lambda _id: True)
        self.assertEqual([m["slice_id"] for m in ready2], ["free", "blocked"])

    def test_no_slice_id_not_ready(self) -> None:
        from paulsha_cortex.coordinator.autonomy import ready_units

        # auto + 有 plan 但 slice_id 缺（None/空字串）→ 無身分 → MUST NOT 就緒
        metas = [
            {"path": "/s/x.md", "dispatch": "auto", "slice_id": None,
             "plan": "docs/p.md", "depends_on": []},
            {"path": "/s/y.md", "dispatch": "auto", "slice_id": "",
             "plan": "docs/p.md", "depends_on": []},
        ]
        self.assertEqual(ready_units(metas, is_satisfied=lambda _id: True), [])

    def test_default_is_satisfied_delegates_to_completion_record_loader(self) -> None:
        from paulsha_cortex.coordinator.autonomy import default_is_satisfied

        with tempfile.TemporaryDirectory() as d, mock.patch(
            "paulsha_cortex.coordinator.autonomy.completion.load_completion_from_handoff"
        ) as load_record:
            hd = Path(d) / "handoff"
            hd.mkdir()
            load_record.side_effect = lambda slice_id, **kwargs: (
                {"slice_id": slice_id} if slice_id == "passed-slice" else None
            )
            self.assertTrue(default_is_satisfied("passed-slice", handoff_dir=str(hd)))
            self.assertFalse(default_is_satisfied("failed-slice", handoff_dir=str(hd)))
            self.assertFalse(default_is_satisfied("missing-slice", handoff_dir=str(hd)))


# --------------------------------------------------------------------------- #
# FanoutTests
# --------------------------------------------------------------------------- #
class FanoutTests(unittest.TestCase):
    def test_dispatch_ready_dispatches_exactly_ready_set(self) -> None:
        from paulsha_cortex.coordinator.autonomy import dispatch_ready

        metas = [
            _meta("ready-1", depends_on=[]),
            _meta("held", dispatch="hold"),
            _meta("noplan", dispatch="auto", plan=None),
            _meta("blocked", depends_on=["down"]),     # down 未滿足 → 不就緒
            _meta("ready-2", depends_on=[]),
        ]
        launcher = _RecordingLauncher()
        # ready-1 / ready-2（皆無相依）應就緒；
        # held（hold）/noplan（無 plan）/blocked（dep=down 未滿足）皆不就緒
        jobs = dispatch_ready(
            metas,
            is_satisfied=lambda _id: _id == "up",
            dispatcher=_FakeDispatcher(),
            persona="builder",
            launcher=launcher,
            git_runner=_fake_target_git_runner,
        )
        dispatched_tasks = [c["slice_id"] for c in launcher.calls]
        self.assertEqual(sorted(dispatched_tasks), ["ready-1", "ready-2"])
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j["persona"] == "builder" for j in jobs))
        # 非就緒一個都沒派
        self.assertNotIn("held", dispatched_tasks)
        self.assertNotIn("noplan", dispatched_tasks)
        self.assertNotIn("blocked", dispatched_tasks)

    def test_dispatch_ready_without_launcher_fails_fast(self) -> None:
        # headless fail-fast（reviewer #112-3）：就緒集非空卻無 launcher → 直接拒絕，
        # 不經 tmux pane 送多行 persona prompt（send-keys -l 會被換行打散）。
        from paulsha_cortex.coordinator.autonomy import (
            DispatchReadyRequiresLauncherError,
            dispatch_ready,
        )

        fake = _FakeDispatcher()
        metas = [_meta("ready-1", depends_on=[])]
        with self.assertRaisesRegex(DispatchReadyRequiresLauncherError, "--executor"):
            dispatch_ready(
                metas, is_satisfied=lambda _id: True, dispatcher=fake, persona="builder",
            )
        self.assertEqual(fake.calls, [])  # 一筆都沒經 pane 派

    def test_dispatch_ready_empty_ready_set_without_launcher_ok(self) -> None:
        # 就緒集為空時無 launcher 不應 fail-fast（無事可派）
        from paulsha_cortex.coordinator.autonomy import dispatch_ready

        fake = _FakeDispatcher()
        metas = [_meta("held", dispatch="hold")]
        jobs = dispatch_ready(metas, is_satisfied=lambda _id: True, dispatcher=fake)
        self.assertEqual(jobs, [])
        self.assertEqual(fake.calls, [])

    def test_dispatch_ready_command_carries_persona_contract(self) -> None:
        from paulsha_cortex.coordinator.autonomy import dispatch_ready

        launcher = _RecordingLauncher()
        metas = [_meta("slice-a", plan="docs/superpowers/plans/a.md")]
        dispatch_ready(
            metas, is_satisfied=lambda _id: True, dispatcher=_FakeDispatcher(),
            persona="builder", launcher=launcher, git_runner=_fake_target_git_runner,
        )

        self.assertEqual(len(launcher.calls), 1)
        command = launcher.calls[0]["prompt"]
        self.assertIn("[PERSONA CONTRACT", command)        # 不再是 "# dispatch ..." 佔位
        self.assertIn("role: builder", command)
        self.assertIn("docs/superpowers/plans/a.md", command)
        self.assertNotIn("copilot", command)               # executor wrapping moved to launcher

    def test_dispatch_ready_bad_role_isolated_from_other_slices(self) -> None:
        # reviewer #112-2：未知 role 的 prompt 構建失敗只該影響該單位，
        # 其餘就緒單位照常派工，失敗被收進 DispatchReadyError（per-slice 隔離）。
        from paulsha_cortex.coordinator.autonomy import (
            DispatchReadyError,
            build_dispatch_prompt as _real_build,
            dispatch_ready,
        )
        import paulsha_cortex.coordinator.autonomy as autonomy_mod

        launcher = _RecordingLauncher()
        metas = [_meta("bad-role-slice", plan="docs/a.md"), _meta("ok-slice", plan="docs/b.md")]

        def _build(persona, *, task, plan_path):
            if task == "bad-role-slice":
                raise ValueError("unknown persona role: bogus")
            return _real_build(persona, task=task, plan_path=plan_path)

        original = autonomy_mod.build_dispatch_prompt
        autonomy_mod.build_dispatch_prompt = _build
        try:
            with self.assertRaises(DispatchReadyError) as ctx:
                dispatch_ready(
                    metas, is_satisfied=lambda _id: True, dispatcher=_FakeDispatcher(),
                    persona="builder", launcher=launcher, git_runner=_fake_target_git_runner,
                )
        finally:
            autonomy_mod.build_dispatch_prompt = original

        # 好的單位照常派；壞 role 被收進 error，不破壞隔離
        self.assertEqual([c["slice_id"] for c in launcher.calls], ["ok-slice"])
        self.assertEqual([job["task"] for job in ctx.exception.jobs], ["ok-slice"])
        self.assertIn("bad-role-slice", str(ctx.exception))

    def test_dispatch_ready_launches_via_agent_launcher(self) -> None:
        from paulsha_cortex.coordinator.autonomy import dispatch_ready

        calls = []

        class _FakeLauncher:
            def launch(self, *, slice_id, prompt, worktree, log_dir):
                calls.append({"slice_id": slice_id, "prompt": prompt, "worktree": worktree})
                from paulsha_cortex.coordinator.launcher import LaunchHandle
                return LaunchHandle(executor="copilot", model_id=None, session_name=slice_id, pid=123, log_path=f"{log_dir}/x")

        metas = [_meta("slice-a", plan="docs/p.md")]
        dispatch_ready(
            metas, is_satisfied=lambda _id: True, dispatcher=_FakeDispatcher(),
            persona="builder", launcher=_FakeLauncher(), git_runner=_fake_target_git_runner,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["slice_id"], "slice-a")
        self.assertIn("[PERSONA CONTRACT", calls[0]["prompt"])
        self.assertIn("docs/p.md", calls[0]["prompt"])

    def test_dispatch_ready_launcher_records_headless_job_without_pane_send(self) -> None:
        from paulsha_cortex.coordinator.autonomy import dispatch_ready
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.launcher import LaunchHandle
        from paulsha_cortex.coordinator.registry import JobRegistry

        class _FakeSender:
            def __init__(self):
                self.sent = []

            def send(self, pane_id, text):
                self.sent.append((pane_id, text))

        class _FakeWt:
            def __init__(self):
                self.created = []

            def create(self, branch):
                self.created.append(branch)
                return f"/fake/wt/{branch.replace('/', '-')}"

        class _FakeLauncher:
            def __init__(self):
                self.calls = []

            def launch(self, *, slice_id, prompt, worktree, log_dir):
                self.calls.append({"slice_id": slice_id, "worktree": worktree, "log_dir": log_dir})
                return LaunchHandle(executor="copilot", model_id=None, session_name=slice_id, pid=123, log_path=f"{log_dir}/x")

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            sender = _FakeSender()
            wt = _FakeWt()
            launcher = _FakeLauncher()
            disp = Dispatcher(reg, sender, wt)
            jobs = dispatch_ready(
                [_meta("slice-a", plan="docs/p.md")],
                is_satisfied=lambda _id: True,
                dispatcher=disp,
                persona="builder",
                launcher=launcher,
                git_runner=_fake_target_git_runner,
            )

            self.assertEqual(sender.sent, [])
            self.assertEqual(wt.created, ["feature/slice-a"])
            self.assertEqual(launcher.calls[0]["worktree"], "/fake/wt/feature-slice-a")
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["executor"], "copilot")
            self.assertEqual(reg.get_job("slice-a-1")["pid"], 123)

    def test_dispatch_ready_launcher_failure_does_not_block_other_slices(self) -> None:
        from paulsha_cortex.coordinator.autonomy import DispatchReadyError, dispatch_ready
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.launcher import LaunchHandle
        from paulsha_cortex.coordinator.registry import JobRegistry

        class _FakeSender:
            def send(self, pane_id, text):
                raise AssertionError("launcher path must not send to pane")

        class _FakeWt:
            def create(self, branch):
                return f"/fake/wt/{branch.replace('/', '-')}"

        class _FailFirstLauncher:
            def __init__(self):
                self.calls = []

            def launch(self, *, slice_id, prompt, worktree, log_dir):
                self.calls.append(slice_id)
                if slice_id == "slice-a":
                    raise RuntimeError("executor missing")
                return LaunchHandle(executor="copilot", model_id=None, session_name=slice_id, pid=123, log_path=f"{log_dir}/x")

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            launcher = _FailFirstLauncher()
            disp = Dispatcher(reg, _FakeSender(), _FakeWt())
            with self.assertRaises(DispatchReadyError) as ctx:
                dispatch_ready(
                    [_meta("slice-a", plan="docs/a.md"), _meta("slice-b", plan="docs/b.md")],
                    is_satisfied=lambda _id: True,
                    dispatcher=disp,
                    persona="builder",
                    launcher=launcher,
                    git_runner=_fake_target_git_runner,
                )

            self.assertEqual(launcher.calls, ["slice-a", "slice-b"])
            self.assertEqual([job["task"] for job in ctx.exception.jobs], ["slice-b"])
            # slice-a's row is persisted before launch (crash-recovery) and
            # reconciled to "failed" when launch raises; slice-b dispatches fine.
            jobs_by_task = {job["task"]: job for job in reg.list_jobs()}
            self.assertEqual(sorted(jobs_by_task), ["slice-a", "slice-b"])
            self.assertEqual(jobs_by_task["slice-a"]["status"], "failed")
            self.assertIsNone(jobs_by_task["slice-a"]["pid"])
            self.assertEqual(jobs_by_task["slice-b"]["pid"], 123)
            self.assertIn("slice-a", str(ctx.exception))

    def test_dispatch_ready_persists_job_before_launch(self) -> None:
        # #187 review fix #2: the registry row must be persisted BEFORE launch
        # so a crash between Popen and the post-launch record leaves a
        # recoverable job (not an orphaned agent with no job row).
        from paulsha_cortex.coordinator.autonomy import dispatch_ready
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.launcher import LaunchHandle
        from paulsha_cortex.coordinator.registry import JobRegistry

        class _FakeSender:
            def send(self, pane_id, text):
                raise AssertionError("launcher path must not send to pane")

        class _FakeWt:
            def create(self, branch):
                return f"/fake/wt/{branch.replace('/', '-')}"

        class _OrderLauncher:
            def __init__(self, reg):
                self.reg = reg
                self.rows_at_launch = None

            def launch(self, *, slice_id, prompt, worktree, log_dir):
                self.rows_at_launch = [j["task"] for j in self.reg.list_jobs()]
                return LaunchHandle(
                    executor="copilot", model_id=None, session_name=slice_id, pid=123, log_path=f"{log_dir}/x"
                )

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            launcher = _OrderLauncher(reg)
            disp = Dispatcher(reg, _FakeSender(), _FakeWt())
            jobs = dispatch_ready(
                [_meta("slice-a", plan="docs/a.md")],
                is_satisfied=lambda _id: True,
                dispatcher=disp,
                persona="builder",
                launcher=launcher,
                git_runner=_fake_target_git_runner,
            )
            # row existed when launch ran; handle (pid) attached afterwards.
            self.assertEqual(launcher.rows_at_launch, ["slice-a"])
            self.assertEqual(jobs[0]["pid"], 123)
            self.assertEqual(reg.get_job(jobs[0]["job_id"])["pid"], 123)

    def test_dispatch_ready_no_slice_id_not_dispatched(self) -> None:
        from paulsha_cortex.coordinator.autonomy import dispatch_ready

        # auto + plan 但無 slice_id → 不就緒 → 不得以 task=None 派工（branch feature/None）
        metas = [
            {"path": "/s/x.md", "dispatch": "auto", "slice_id": None,
             "plan": "docs/p.md", "depends_on": []},
        ]
        fake = _FakeDispatcher()
        jobs = dispatch_ready(metas, is_satisfied=lambda _id: True, dispatcher=fake)
        self.assertEqual(jobs, [])
        self.assertEqual(fake.calls, [])

    def test_dispatch_ready_refuses_duplicate_slice_id(self) -> None:
        from paulsha_cortex.coordinator.autonomy import dispatch_ready

        # 兩份 spec 誤用同一 slice_id → refuse（否則對同一 feature/<id> 重複派工）
        dup = [_meta("dupX", depends_on=[]), _meta("dupX", depends_on=[])]
        fake = _FakeDispatcher()
        with self.assertRaisesRegex(ValueError, "重複 slice_id"):
            dispatch_ready(dup, is_satisfied=lambda _id: True, dispatcher=fake)
        self.assertEqual(fake.calls, [])  # refuse → 一筆都沒派

    def test_dispatch_ready_with_real_dispatcher_headless_launcher(self) -> None:
        # headless fan-out（reviewer #112-3）：經 launcher 啟 agent、registry 記 job，
        # 全程不送 tmux pane、不碰真 git。
        import subprocess as _subprocess

        from paulsha_cortex.coordinator.autonomy import dispatch_ready
        from paulsha_cortex.coordinator.dispatcher import Dispatcher
        from paulsha_cortex.coordinator.launcher import LaunchHandle
        from paulsha_cortex.coordinator.registry import JobRegistry

        class _FakeSender:
            def __init__(self):
                self.sent = []

            def send(self, pane_id, text):
                self.sent.append((pane_id, text))

        class _FakeWt:
            def create(self, branch):
                return f"/fake/wt/{branch.replace('/', '-')}"

        class _FakeLauncher:
            def __init__(self):
                self.calls = []

            def launch(self, *, slice_id, prompt, worktree, log_dir):
                self.calls.append(slice_id)
                return LaunchHandle(
                    executor="copilot", model_id=None, session_name=slice_id, pid=200 + len(self.calls),
                    log_path=f"{log_dir}/{slice_id}.jsonl",
                )

        # spy 真 subprocess.run：本測試全程不得對 git 起任何真子行程
        real_calls: list = []
        orig_run = _subprocess.run

        def _spy_run(args, *a, **k):
            if args and args[0] == "git":
                real_calls.append(list(args))
            return orig_run(args, *a, **k)

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            sender = _FakeSender()
            disp = Dispatcher(reg, sender, _FakeWt())
            launcher = _FakeLauncher()
            metas = [_meta("real-a", depends_on=[]), _meta("real-b", depends_on=[])]
            git_calls: list = []

            def _fake_git(args):  # 注入 git runner，避免起真 git（target + baseline）
                git_calls.append(list(args))
                if args and args[0] == "rev-parse":
                    return "f" * 40
                if len(args) >= 5 and args[0] == "-C" and args[2] == "fetch":
                    return ""
                if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
                    return "e" * 40
                if len(args) >= 6 and args[0] == "-C" and args[2] == "merge-base":
                    return ""
                return ""

            _subprocess.run = _spy_run
            try:
                jobs = dispatch_ready(
                    metas,
                    is_satisfied=lambda _id: True,
                    dispatcher=disp,
                    launcher=launcher,
                    git_runner=_fake_git,
                )
            finally:
                _subprocess.run = orig_run
            self.assertEqual(len(jobs), 2)
            self.assertEqual({j["status"] for j in jobs}, {"dispatched"})
            self.assertEqual(len(reg.list_jobs()), 2)
            self.assertEqual(launcher.calls, ["real-a", "real-b"])
            self.assertEqual(sender.sent, [])   # headless：不送 tmux pane
            self.assertEqual(real_calls, [])     # 注入 git_runner → 全程不啟動真 git
            baseline_calls = [call for call in git_calls if call[:1] == ["rev-parse"]]
            self.assertEqual(baseline_calls, [["rev-parse", "feature/real-a"], ["rev-parse", "feature/real-b"]])
            self.assertEqual(sum(1 for call in git_calls if len(call) >= 3 and call[2] == "fetch"), 2)
            self.assertEqual(
                sum(
                    1
                    for call in git_calls
                    if len(call) >= 4 and call[2] == "rev-parse" and call[3].startswith("refs/remotes/")
                ),
                2,
            )
            self.assertEqual({j["dispatch_head"] for j in jobs}, {"f" * 40})


# --------------------------------------------------------------------------- #
# CliTests
# --------------------------------------------------------------------------- #
class CliTests(unittest.TestCase):
    def test_main_ready_lists_ready_units(self) -> None:
        from paulsha_cortex.coordinator.cli import main

        with tempfile.TemporaryDirectory() as d:
            _write_spec(Path(d), "r.md", "dispatch: auto\nslice_id: r\nplan: docs/p.md\n" + _v1_verification_block())
            _write_spec(Path(d), "h.md", "dispatch: hold\nslice_id: h")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(["ready", "--specs-dir", d], is_satisfied=lambda _id: True)
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual([m["slice_id"] for m in payload], ["r"])

    def test_main_fanout_with_fakes(self) -> None:
        from paulsha_cortex.coordinator.cli import main
        submitted: list[tuple[str, dict, str]] = []

        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(
                    ["fanout", "--specs-dir", d, "--executor", "copilot"],
                    control_read_status=lambda: {"degraded": False, "degraded_reason": None},
                    control_submit_request=lambda req_type, args, requested_by: submitted.append(
                        (req_type, dict(args), requested_by)
                    )
                    or "req-fanout-1",
                    control_poll_done=lambda req_id, timeout, poll_interval=0.5: {
                        "status": "ok",
                        "result": {
                            "dispatched": [
                                {"job_id": "fa-1", "task": "fa", "dispatch_head": "BASE_SHA"},
                                {"job_id": "fb-2", "task": "fb", "dispatch_head": "BASE_SHA"},
                            ]
                        },
                    },
                )
            self.assertEqual(rc, 0)
            summary = json.loads(out.getvalue())
            self.assertEqual([j["task"] for j in summary["dispatched"]], ["fa", "fb"])
            self.assertEqual(
                submitted,
                [
                    (
                        "fanout",
                        {
                            "specs_dir": d,
                            "persona": "builder",
                            "allow_unsafe": False,
                            "model": None,
                            "executor": "copilot",
                        },
                        "coordinator-cli",
                    )
                ],
            )

    def test_main_fanout_executor_uses_headless_launcher_no_pane_send(self) -> None:
        from paulsha_cortex.coordinator.cli import main
        from paulsha_cortex.coordinator.registry import JobRegistry

        class _FakeSender:
            def __init__(self):
                self.sent = []

            def send(self, pane_id, text):
                self.sent.append((pane_id, text))

        class _FakeWt:
            def create(self, branch):
                return f"/fake/wt/{branch.replace('/', '-')}"

        with tempfile.TemporaryDirectory() as d:
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            sender = _FakeSender()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = main(
                    ["fanout", "--specs-dir", d, "--executor", "copilot"],
                    registry=reg,
                    pane_sender=sender,
                    worktree_creator=_FakeWt(),
                    control_read_status=lambda: {"degraded": False, "degraded_reason": None},
                    control_submit_request=lambda *_args: "req-fanout-2",
                    control_poll_done=lambda req_id, timeout, poll_interval=0.5: {
                        "status": "ok",
                        "result": {"dispatched": [{"job_id": "fa-1", "task": "fa"}]},
                    },
                )
            self.assertEqual(rc, 0)
            summary = json.loads(out.getvalue())
            self.assertEqual(summary["dispatched"], [{"job_id": "fa-1", "task": "fa"}])
            self.assertEqual(sender.sent, [])  # mutation CLI 不得直接送 pane
            self.assertEqual(reg.list_jobs(), [])  # 也不得成為第二個 mutable writer

    def test_main_fanout_without_executor_fails_fast(self) -> None:
        # CLI 不再本地做 fanout；control plane 的錯誤應直接回傳給操作者。
        from paulsha_cortex.coordinator.cli import main
        from paulsha_cortex.coordinator.registry import JobRegistry

        class _FakeSender:
            def __init__(self):
                self.sent = []

            def send(self, pane_id, text):
                self.sent.append((pane_id, text))

        class _FakeWt:
            def create(self, branch):
                return f"/fake/wt/{branch.replace('/', '-')}"

        with tempfile.TemporaryDirectory() as d:
            _write_spec(Path(d), "a.md", "dispatch: auto\nslice_id: fa\nplan: docs/p.md\n" + _v1_verification_block())
            reg = JobRegistry(state_path=Path(d) / "jobs.json")
            sender = _FakeSender()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main(
                    ["fanout", "--specs-dir", d],
                    registry=reg,
                    pane_sender=sender,
                    worktree_creator=_FakeWt(),
                    control_read_status=lambda: {"degraded": False, "degraded_reason": None},
                    control_submit_request=lambda *_args: "req-fanout-3",
                    control_poll_done=lambda req_id, timeout, poll_interval=0.5: {
                        "status": "error",
                        "error": "請以 --executor 走 headless fanout",
                    },
                )
            self.assertNotEqual(rc, 0)
            self.assertIn("--executor", err.getvalue())
            self.assertEqual(sender.sent, [])
            self.assertEqual(reg.list_jobs(), [])

    def test_main_refuses_on_cycle(self) -> None:
        from paulsha_cortex.coordinator.cli import main

        with tempfile.TemporaryDirectory() as d:
            _write_spec(Path(d), "a.md", "dispatch: auto\nslice_id: A\nplan: p\ndepends_on: [B]\n" + _v1_verification_block())
            _write_spec(Path(d), "b.md", "dispatch: auto\nslice_id: B\nplan: p\ndepends_on: [A]\n" + _v1_verification_block())
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main(["ready", "--specs-dir", d], is_satisfied=lambda _id: True)
            self.assertNotEqual(rc, 0)  # refuse
            self.assertIn("循環", err.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
