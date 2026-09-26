"""issue #561：長 argv 不得擠掉 TimeoutExpired 摘要尾端的診斷字。"""

from __future__ import annotations

import subprocess

from paulsha_cortex.coordinator.diagnostics import summarize_exception
from paulsha_cortex.coordinator.planning import summarize_planning_exception


def test_timeout_expired_long_argv_summary_preserves_tail_and_type() -> None:
    exc = subprocess.TimeoutExpired(cmd=["runner", "x" * 400], timeout=37)

    summary = summarize_exception(exc, limit=160)

    assert summary.startswith("TimeoutExpired: Command ")
    assert "…" in summary
    assert summary.endswith("timed out after 37 seconds")


def test_planning_timeout_expired_long_argv_summary_preserves_tail_and_type() -> None:
    exc = subprocess.TimeoutExpired(cmd=["runner", "x" * 1000], timeout=37)

    summary = summarize_planning_exception(exc, limit=480)

    assert summary.startswith("TimeoutExpired: Command ")
    assert "timed out after 37 seconds" in summary
    assert summary.endswith("…+571c")
