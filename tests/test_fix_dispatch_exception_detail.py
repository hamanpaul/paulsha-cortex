from __future__ import annotations

import contextlib
import io
from datetime import datetime
from types import SimpleNamespace

from paulsha_cortex.coordinator import autonomy, manager, manager_daemon


def test_dispatch_ready_error_str_includes_slice_id_and_exception_details() -> None:
    exc = FileNotFoundError("/tmp/no_such_dispatch_input.yml")
    err = autonomy.DispatchReadyError(errors=[("slice-100", exc)], jobs=[])

    rendered = str(err)

    assert "slice-100" in rendered
    assert "FileNotFoundError" in rendered
    assert "/tmp/no_such_dispatch_input.yml" in rendered


class _Registry:
    def list_jobs(self) -> list[dict]:
        return []


class _Dispatcher:
    def __init__(self) -> None:
        self._registry = _Registry()


def test_tick_handler_keeps_jobs_and_exposes_per_slice_error_fields(monkeypatch, tmp_path) -> None:
    exc = FileNotFoundError("/tmp/missing.toml")
    err = autonomy.DispatchReadyError(
        errors=[("slice-100", exc)],
        jobs=[{"job_id": "job-100"}],
    )

    monkeypatch.setattr(manager.autonomy, "dispatch_ready", lambda *_args, **_kwargs: (_ for _ in ()).throw(err))
    monkeypatch.setattr(
        manager,
        "complete_tick",
        lambda *_args, **_kwargs: {"completed": [], "errors": [], "warnings": []},
    )

    summary = manager.run_tick(
        _Dispatcher(),
        metas=[{"slice_id": "slice-100", "dispatch": "auto", "plan": "docs/superpowers/plans/slice-100.md", "depends_on": []}],
        is_satisfied=lambda _sid: True,
        handoff_dir=str(tmp_path),
        launcher=SimpleNamespace(launch=lambda **_kwargs: None),
        require_idle=False,
        clock=lambda: "2026-07-26T00:00:00+00:00",
    )

    assert summary["dispatched"] == [{"job_id": "job-100"}]
    assert summary["errors"][0]["slice_id"] == "slice-100"
    assert summary["errors"][0]["type"] == "FileNotFoundError"
    assert "missing.toml" in summary["errors"][0]["message"]


def test_manager_daemon_log_lines_are_iso8601_prefixed() -> None:
    sink = io.StringIO()
    with contextlib.redirect_stderr(sink):
        manager_daemon._log_error(RuntimeError("dispatch_failed"))

    line = sink.getvalue().strip()
    assert line, "manager_daemon._log_error should emit one line"
    first_field = line.split(" ", maxsplit=1)[0]
    # ISO-8601 must be parseable from first field
    datetime.fromisoformat(first_field.replace("Z", "+00:00"))
