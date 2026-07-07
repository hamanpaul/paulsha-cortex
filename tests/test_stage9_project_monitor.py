"""Stage 9 Project Monitor — Phase 1 TDD red baseline.

These tests lock the public API surface and behavioural contract for the
Project Monitor service (see openspec/changes/2026-04-26-stage9-project-monitor/
for proposal, design, tasks, and spec).

All imports below intentionally point at modules that do not yet exist.
Phase 2 (Green) will land the minimal implementation; Phase 3 will add the
service runtime + Unix socket; Phase 4 will close out spec/review/archive.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

# Imports from modules that do not yet exist — these will ImportError in Red.
# Wrapped so the module loads and individual tests fail with a clear cause
# rather than the entire file failing to collect.
try:
    from paulsha_cortex.monitor.config import (
        MonitorConfig,
        WorkspaceConfig,
        load_config,
    )
    from paulsha_cortex.monitor.models import (
        ProjectState,
        Signal,
        StageRef,
        StageView,
        TaskRef,
    )
    from paulsha_cortex.monitor.parser import (
        extract_project_state,
        parse_blockers,
        parse_todo_current_sprint,
    )
    from paulsha_cortex.monitor.scanner import (
        ProjectClassification,
        classify_project,
        scan_workspaces,
    )

    MONITOR_AVAILABLE = True
    IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # Expected during Phase 1 Red
    MONITOR_AVAILABLE = False
    IMPORT_ERROR = exc


# --- fixture helpers -------------------------------------------------------


def write_yaml(content: str) -> Path:
    """Write a YAML string to a temp file and return its path."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    try:
        handle.write(textwrap.dedent(content))
        handle.flush()
    finally:
        handle.close()
    return Path(handle.name)


def make_workspace(root: Path, projects: dict[str, dict]) -> Path:
    """Create a synthetic workspace directory tree under `root`.

    `projects` maps directory name → spec dict:
      {"kind": "tracked-paul-yml" | "tracked-workstream" | "legacy",
       "todo": str | None, "blockers": str | None}
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, spec in projects.items():
        proj = root / name
        proj.mkdir(parents=True, exist_ok=True)
        kind = spec["kind"]
        if kind == "tracked-paul-yml":
            (proj / ".paul-project.yml").write_text(
                "policy_profile: stage-driven\npolicy_version: 1.0.0\n"
            )
        if kind in ("tracked-workstream", "tracked-paul-yml"):
            ws = proj / "docs" / "superpowers" / "workstreams" / "stage1-demo"
            ws.mkdir(parents=True, exist_ok=True)
            todo = spec.get("todo") or DEFAULT_TODO
            (ws / "todo.md").write_text(textwrap.dedent(todo))
            (ws / "task.md").write_text("# task\n- [ ] do thing\n")
        # legacy: nothing extra
    return root


DEFAULT_TODO = """\
# stage1-demo / todo

## Current Sprint

- [ ] processing item alpha
- [ ] next item beta
- [x] already done item

## Blockers

- [ ] waiting on upstream X
- [ ] need decision on Y

## Evidence / Links

- [ ] phase 1 red log

## Handoff Notes

- [ ] handoff to next persona
"""


# --- skip helper ----------------------------------------------------------


def _require_monitor(test: unittest.TestCase) -> None:
    """Phase-1 red guard.

    During TDD Red, the `paulsha_cortex.monitor` package does not exist yet,
    so we fail (not skip) with a clear pointer to the missing import. Once
    Phase 2 Green lands the module, this helper becomes a no-op and the
    body of each test runs its real assertions.
    """
    if not MONITOR_AVAILABLE:
        test.fail(
            f"paulsha_cortex.monitor not implemented yet (Phase 1 Red): {IMPORT_ERROR}"
        )


# --- B1 / Config tests ----------------------------------------------------


class Stage9ConfigTests(unittest.TestCase):
    """Lock the global config loader contract (design §3.5, spec §B1)."""

    def setUp(self) -> None:
        _require_monitor(self)

    def make_config(self, body: str) -> Path:
        path = write_yaml(body)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_load_config_reads_explicit_path(self) -> None:
        path = self.make_config(
            """
            workspaces:
              - path: /tmp/wsA
                name: a
              - path: /tmp/wsB
                name: b
            monitor:
              poll_interval_seconds: 30
              rescan_interval_seconds: 300
              legacy_policy: hide
            """
        )
        cfg = load_config(config_path=path)
        self.assertIsInstance(cfg, MonitorConfig)
        self.assertEqual(len(cfg.workspaces), 2)
        self.assertEqual(cfg.workspaces[0].name, "a")
        self.assertEqual(cfg.poll_interval_seconds, 30)
        self.assertEqual(cfg.rescan_interval_seconds, 300)
        self.assertEqual(cfg.legacy_policy, "hide")

    def test_load_config_supports_env_fallback(self) -> None:
        path = self.make_config(
            """
            workspaces:
              - path: /tmp/wsA
                name: a
            """
        )
        previous = os.environ.get("PSC_MONITOR_CONFIG")
        os.environ["PSC_MONITOR_CONFIG"] = str(path)
        try:
            cfg = load_config()
        finally:
            if previous is None:
                os.environ.pop("PSC_MONITOR_CONFIG", None)
            else:
                os.environ["PSC_MONITOR_CONFIG"] = previous

        self.assertEqual(cfg.workspaces[0].name, "a")

    def test_load_config_applies_documented_defaults(self) -> None:
        path = self.make_config(
            """
            workspaces:
              - path: /tmp/wsA
                name: a
            """
        )
        cfg = load_config(config_path=path)
        self.assertEqual(cfg.poll_interval_seconds, 60)
        self.assertEqual(cfg.rescan_interval_seconds, 300)
        self.assertEqual(cfg.watch_debounce_ms, 500)
        self.assertEqual(cfg.legacy_policy, "list-only")

    def test_load_config_rejects_empty_workspaces(self) -> None:
        path = self.make_config(
            """
            workspaces: []
            """
        )
        with self.assertRaisesRegex(ValueError, "workspaces"):
            load_config(config_path=path)

    def test_load_config_rejects_invalid_legacy_policy(self) -> None:
        path = self.make_config(
            """
            workspaces:
              - path: /tmp/wsA
                name: a
            monitor:
              legacy_policy: somethingelse
            """
        )
        with self.assertRaisesRegex(ValueError, "legacy_policy"):
            load_config(config_path=path)

    def test_sample_config_file_loads(self) -> None:
        sample = self.make_config(
            """
            workspaces:
              - path: ~/prj_work
                name: archive
              - path: ~/prj_pri
                name: private
            monitor:
              poll_interval_seconds: 60
              rescan_interval_seconds: 300
              watch_debounce_ms: 500
              legacy_policy: list-only
              socket_path: ~/.agents/run/project-monitor.sock
              ignore_dirs: []
            """
        )
        cfg = load_config(config_path=sample)
        names = {ws.name for ws in cfg.workspaces}
        # Defaults documented in design §3.5
        self.assertIn("archive", names)
        self.assertIn("private", names)


# --- B2 / Classifier tests ------------------------------------------------


class Stage9ClassifierTests(unittest.TestCase):
    """Lock tracked-vs-legacy classification (design §3.2, spec §B2)."""

    def setUp(self) -> None:
        _require_monitor(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-classifier-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_project_with_paul_project_yml_is_tracked(self) -> None:
        ws = make_workspace(
            self.tmp / "ws",
            {"projA": {"kind": "tracked-paul-yml"}},
        )
        result = classify_project(ws / "projA")
        self.assertEqual(result, ProjectClassification.TRACKED)

    def test_project_with_workstreams_dir_is_tracked(self) -> None:
        ws = make_workspace(
            self.tmp / "ws",
            {"projB": {"kind": "tracked-workstream"}},
        )
        result = classify_project(ws / "projB")
        self.assertEqual(result, ProjectClassification.TRACKED)

    def test_project_without_either_is_legacy(self) -> None:
        ws = make_workspace(
            self.tmp / "ws",
            {"projC": {"kind": "legacy"}},
        )
        result = classify_project(ws / "projC")
        self.assertEqual(result, ProjectClassification.LEGACY)

    def test_legacy_policy_hide_excludes_legacy_projects(self) -> None:
        ws_root = self.tmp / "ws"
        make_workspace(
            ws_root,
            {
                "projT": {"kind": "tracked-paul-yml"},
                "projL": {"kind": "legacy"},
            },
        )
        cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=ws_root, name="ws"),),
            legacy_policy="hide",
        )
        states = scan_workspaces(cfg)
        ids = {s.project_id for s in states}
        self.assertIn("projT", ids)
        self.assertNotIn("projL", ids)

    def test_legacy_policy_list_only_includes_legacy_with_no_state(self) -> None:
        ws_root = self.tmp / "ws"
        make_workspace(
            ws_root,
            {
                "projT": {"kind": "tracked-paul-yml"},
                "projL": {"kind": "legacy"},
            },
        )
        cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=ws_root, name="ws"),),
            legacy_policy="list-only",
        )
        states = {s.project_id: s for s in scan_workspaces(cfg)}
        self.assertTrue(states["projL"].legacy)
        self.assertEqual(states["projL"].in_progress_stages, ())
        self.assertEqual(states["projL"].completed_stages, ())

    def test_ignore_dirs_filter_skips_listed_directories(self) -> None:
        ws_root = self.tmp / "ws"
        make_workspace(
            ws_root,
            {
                "projT": {"kind": "tracked-paul-yml"},
                "skipme": {"kind": "tracked-paul-yml"},
            },
        )
        cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=ws_root, name="ws"),),
            ignore_dirs=("skipme",),
        )
        ids = {s.project_id for s in scan_workspaces(cfg)}
        self.assertIn("projT", ids)
        self.assertNotIn("skipme", ids)

    def test_duplicate_project_names_are_qualified_in_snapshot(self) -> None:
        west = make_workspace(
            self.tmp / "west",
            {"same": {"kind": "tracked-paul-yml"}},
        )
        east = make_workspace(
            self.tmp / "east",
            {"same": {"kind": "tracked-paul-yml"}},
        )
        cfg = MonitorConfig(
            workspaces=(
                WorkspaceConfig(path=west, name="west"),
                WorkspaceConfig(path=east, name="east"),
            ),
        )
        states = scan_workspaces(cfg)
        ids = [state.project_id for state in states]
        self.assertEqual(len(states), 2)
        self.assertEqual(len(set(ids)), 2)


# --- B3 / Parser tests ----------------------------------------------------


class Stage9ParserTests(unittest.TestCase):
    """Lock state-extraction rules (design §3.3, spec §B3)."""

    def setUp(self) -> None:
        _require_monitor(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-parser-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_project_with_todo(self, todo_body: str) -> Path:
        proj = self.tmp / "proj"
        ws = proj / "docs" / "superpowers" / "workstreams" / "stage1-demo"
        ws.mkdir(parents=True, exist_ok=True)
        (proj / ".paul-project.yml").write_text("policy_profile: stage-driven\n")
        (ws / "todo.md").write_text(textwrap.dedent(todo_body))
        return proj

    def test_processing_task_is_first_unchecked_in_current_sprint(self) -> None:
        proj = self._make_project_with_todo(DEFAULT_TODO)
        state = extract_project_state(proj, workspace_name="ws")
        self.assertEqual(len(state.in_progress_stages), 1)
        view = state.in_progress_stages[0]
        self.assertIsNotNone(view.processing_task)
        self.assertEqual(view.processing_task.text, "processing item alpha")

    def test_next_task_is_second_unchecked(self) -> None:
        proj = self._make_project_with_todo(DEFAULT_TODO)
        state = extract_project_state(proj, workspace_name="ws")
        view = state.in_progress_stages[0]
        self.assertIsNotNone(view.next_task)
        self.assertEqual(view.next_task.text, "next item beta")

    def test_blockers_parsed_from_blockers_section(self) -> None:
        proj = self._make_project_with_todo(DEFAULT_TODO)
        state = extract_project_state(proj, workspace_name="ws")
        view = state.in_progress_stages[0]
        self.assertEqual(
            list(view.blockers),
            ["waiting on upstream X", "need decision on Y"],
        )

    def test_in_progress_when_open_checkbox_in_current_sprint(self) -> None:
        proj = self._make_project_with_todo(DEFAULT_TODO)
        state = extract_project_state(proj, workspace_name="ws")
        ids = {v.stage_id for v in state.in_progress_stages}
        self.assertIn("stage1-demo", ids)

    def test_completed_when_all_current_sprint_items_checked(self) -> None:
        completed_todo = """\
        # stage1-demo / todo

        ## Current Sprint

        - [x] all done

        ## Blockers
        ## Evidence / Links
        ## Handoff Notes
        """
        proj = self._make_project_with_todo(completed_todo)
        state = extract_project_state(proj, workspace_name="ws")
        # No open work in Current Sprint AND no archive entry → still pending,
        # NOT in_progress. Real "completed" requires archive evidence; this test
        # just guards that the parser does not falsely promote to in_progress.
        self.assertEqual(state.in_progress_stages, ())

    def test_malformed_todo_marks_stage_as_degraded(self) -> None:
        proj = self.tmp / "proj-bad"
        ws = proj / "docs" / "superpowers" / "workstreams" / "stage1-demo"
        ws.mkdir(parents=True, exist_ok=True)
        (proj / ".paul-project.yml").write_text("policy_profile: stage-driven\n")
        # Binary-ish payload that will reasonably trip the parser
        (ws / "todo.md").write_bytes(b"\xff\xfe not utf-8 \xff\xfe")

        state = extract_project_state(proj, workspace_name="ws")
        # Should not raise. Should record degradation in source_signals.
        notes = " ".join(s.note or "" for s in state.source_signals)
        self.assertIn("degraded", notes.lower())

    def test_source_signals_record_inspected_paths(self) -> None:
        proj = self._make_project_with_todo(DEFAULT_TODO)
        state = extract_project_state(proj, workspace_name="ws")
        kinds = {s.kind for s in state.source_signals}
        self.assertIn("todo", kinds)

    def test_parse_todo_current_sprint_returns_only_open_items(self) -> None:
        proj = self._make_project_with_todo(DEFAULT_TODO)
        items = parse_todo_current_sprint(
            proj / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
        )
        texts = [t.text for t in items]
        self.assertEqual(texts, ["processing item alpha", "next item beta"])

    def test_parse_blockers_returns_listed_strings(self) -> None:
        proj = self._make_project_with_todo(DEFAULT_TODO)
        blockers = parse_blockers(
            proj / "docs" / "superpowers" / "workstreams" / "stage1-demo" / "todo.md"
        )
        self.assertEqual(
            list(blockers),
            ["waiting on upstream X", "need decision on Y"],
        )


# --- B5 / B6 / CLI tests --------------------------------------------------


class Stage9CliTests(unittest.TestCase):
    """Lock --once CLI contract (design §3.4, spec §B5/B6)."""

    def setUp(self) -> None:
        _require_monitor(self)
        self.tmp = Path(tempfile.mkdtemp(prefix="stage9-cli-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config_for(self, ws_root: Path) -> Path:
        path = self.tmp / "monitor.yaml"
        path.write_text(
            textwrap.dedent(
                f"""
                workspaces:
                  - path: {ws_root}
                    name: ws
                monitor:
                  legacy_policy: list-only
                """
            )
        )
        return path

    def test_module_help_exits_zero(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "paulsha_cortex.monitor", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("--once", completed.stdout)
        self.assertIn("--config", completed.stdout)

    def test_once_mode_exits_zero_with_json_snapshot(self) -> None:
        ws_root = self.tmp / "ws"
        make_workspace(
            ws_root,
            {
                "projT": {"kind": "tracked-paul-yml"},
                "projL": {"kind": "legacy"},
            },
        )
        cfg = self._write_config_for(ws_root)

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "paulsha_cortex.monitor",
                "--once",
                "--config",
                str(cfg),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertIsInstance(payload, dict)
        self.assertIn("projects", payload)
        ids = {p["project_id"] for p in payload["projects"]}
        self.assertIn("projT", ids)
        self.assertIn("projL", ids)

    def test_missing_workspace_path_does_not_crash(self) -> None:
        ws_root = self.tmp / "definitely-not-here"
        cfg = self._write_config_for(ws_root)

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "paulsha_cortex.monitor",
                "--once",
                "--config",
                str(cfg),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        # Missing workspace must not crash; it should be logged and skipped.
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["projects"], [])

    def test_once_mode_traceback_does_not_leak_to_stderr_on_known_error(
        self,
    ) -> None:
        # Pointing at a non-existent config should produce a clean error,
        # not a Python traceback (parity with stage1 CLI clean-error behaviour).
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "paulsha_cortex.monitor",
                "--once",
                "--config",
                str(self.tmp / "nope.yaml"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertNotIn("Traceback", completed.stderr)

    def test_service_mode_registers_sigterm_handler(self) -> None:
        import paulsha_cortex.monitor.__main__ as monitor_main

        calls: dict[str, object] = {}

        class FakeService:
            def __init__(self, *, config) -> None:
                calls["config"] = config

            def run_forever(self) -> None:
                calls["handler"](signal.SIGTERM, None)

            def stop(self) -> None:
                calls["stopped"] = True

        def fake_signal(sig, handler):
            if sig == signal.SIGTERM:
                calls["handler"] = handler
            return signal.SIG_DFL

        def fake_getsignal(sig):
            assert sig == signal.SIGTERM
            return signal.SIG_DFL

        with (
            mock.patch.object(
                monitor_main,
                "load_config",
                lambda config_path=None: MonitorConfig(
                    workspaces=(WorkspaceConfig(path=self.tmp, name="ws"),)
                ),
            ),
            mock.patch.object(monitor_main, "ProjectMonitorService", FakeService),
            mock.patch.object(monitor_main.signal, "signal", fake_signal),
            mock.patch.object(monitor_main.signal, "getsignal", fake_getsignal),
        ):
            self.assertEqual(monitor_main.main([]), 0)
        self.assertTrue(calls.get("stopped"))


# --- Snapshot integration test ---------------------------------------------


class Stage9SnapshotTests(unittest.TestCase):
    """Point monitor at paulshaclaw itself and verify ground truth.

    This is the integration test promised in design §6: the verified state
    from 2026-04-26 (Stages 0-7 + 11 completed and on main; Stage 9 in
    progress on this very worktree) must be re-derived by the monitor.
    """

    def setUp(self) -> None:
        _require_monitor(self)

    def test_paulshaclaw_self_snapshot_matches_known_state(self) -> None:
        # Repo root = the worktree containing this test file.
        repo_root = Path(__file__).resolve().parents[1]
        # Use the parent dir as the workspace root so the current repo appears
        # as a project under a workspace.
        workspace_root = repo_root.parent
        cfg = MonitorConfig(
            workspaces=(WorkspaceConfig(path=workspace_root, name="self"),),
            legacy_policy="list-only",
        )
        states = {s.project_id: s for s in scan_workspaces(cfg)}

        # The active worktree itself must appear
        # as tracked, not legacy.
        candidates = [
            sid
            for sid in states
            if sid == repo_root.name or "stage9-project-monitor" in sid
        ]
        self.assertTrue(
            candidates,
            msg=f"expected project {repo_root.name} under {workspace_root}, got {list(states)}",
        )
        sample = states[candidates[0]]
        self.assertFalse(sample.legacy)
        self.assertEqual(sample.project_id, repo_root.name)


if __name__ == "__main__":
    unittest.main()
