from __future__ import annotations

import math
import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from paulsha_cortex.coordinator import manager_daemon


def test_default_max_load_env_matrix(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 20)

    # unset / 空字串 / abc / NaN / nan / Inf / -Inf / 0 / -1 -> 回 10.0
    for env_val in (None, "", "abc", "NaN", "nan", "Inf", "-Inf", "0", "-1"):
        if env_val is None:
            monkeypatch.delenv("PSC_MANAGER_MAX_LOAD", raising=False)
        else:
            monkeypatch.setenv("PSC_MANAGER_MAX_LOAD", env_val)
        assert manager_daemon.default_max_load() == 10.0

    # 4.5 -> 回 4.5
    monkeypatch.setenv("PSC_MANAGER_MAX_LOAD", "4.5")
    assert manager_daemon.default_max_load() == 4.5

    # cpu_count=None -> 回 1.0
    monkeypatch.delenv("PSC_MANAGER_MAX_LOAD", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert manager_daemon.default_max_load() == 1.0


def test_main_max_load_precedence_and_validation(monkeypatch):
    # CLI 非法值均拒絕 (argparse exit code 2)
    for bad in ("abc", "NaN", "nan", "Inf", "-Inf", "0", "-1"):
        with pytest.raises(SystemExit) as exc:
            manager_daemon.main(["--max-load", bad])
        assert exc.value.code == 2

    seen: dict[str, object] = {}

    def fake_run_loop(**kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(manager_daemon, "run_loop", fake_run_loop)

    # CLI 7 覆寫 env 4.5
    seen.clear()
    monkeypatch.setenv("PSC_MANAGER_MAX_LOAD", "4.5")
    exit_code = manager_daemon.main(["--max-load", "7"])
    assert exit_code == 0
    assert seen["default_max_load"] == 7.0

    # 未帶旗標則使用 env 4.5
    seen.clear()
    monkeypatch.setenv("PSC_MANAGER_MAX_LOAD", "4.5")
    exit_code = manager_daemon.main([])
    assert exit_code == 0
    assert seen["default_max_load"] == 4.5


def test_run_loop_periodic_runner_default_max_load_kwarg(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_CONTROL_ROOT", str(tmp_path))
    captured_kwargs: dict[str, object] = {}

    def fake_build_periodic_tick_runner(**kwargs):
        captured_kwargs.update(kwargs)
        return lambda: {"dispatch_skipped": "not-idle"}

    monkeypatch.setattr(
        manager_daemon, "build_periodic_tick_runner", fake_build_periodic_tick_runner
    )

    # 顯式 7.0 有傳入
    captured_kwargs.clear()
    started = manager_daemon.run_loop(max_rounds=1, default_max_load=7.0)
    assert started is True
    assert captured_kwargs.get("default_max_load") == 7.0

    # 未提供參數時 key 不存在
    captured_kwargs.clear()
    started = manager_daemon.run_loop(max_rounds=1)
    assert started is True
    assert "default_max_load" not in captured_kwargs

    # 非法顯式值拒絕
    for bad in (0, -1, float("nan"), float("inf"), float("-inf"), "invalid"):
        with pytest.raises(ValueError):
            manager_daemon.run_loop(max_rounds=1, default_max_load=bad)


def test_build_periodic_tick_runner_passes_max_load_to_run_tick():
    called_kwargs: dict[str, object] = {}

    def fake_run_tick(*args, **kwargs):
        called_kwargs.update(kwargs)
        return {"dispatched": []}

    fake_dispatcher = MagicMock()
    runner = manager_daemon.build_periodic_tick_runner(
        dispatcher=fake_dispatcher,
        specs_dir="",
        handoff_dir="",
        scan_specs_fn=lambda _: [],
        auto_claim_fn=lambda: [],
        run_tick_fn=fake_run_tick,
        default_max_load=7.0,
    )
    runner()
    assert called_kwargs.get("max_load") == 7.0


def test_require_idle_defaults_and_main_paths(monkeypatch):
    # helper 大小寫與空白驗證
    monkeypatch.delenv("PSC_MANAGER_REQUIRE_IDLE", raising=False)
    assert manager_daemon.default_require_idle() is True

    for falsey in ("0", "false", "False", " FALSE ", "off", "OFF", "no", "NO"):
        monkeypatch.setenv("PSC_MANAGER_REQUIRE_IDLE", falsey)
        assert manager_daemon.default_require_idle() is False

    for truthy in ("", "1", "true", "TRUE", "yes", "2"):
        monkeypatch.setenv("PSC_MANAGER_REQUIRE_IDLE", truthy)
        assert manager_daemon.default_require_idle() is True

    # main 路徑測試
    seen: dict[str, object] = {}

    def fake_run_loop(**kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(manager_daemon, "run_loop", fake_run_loop)

    # env 0 -> False
    seen.clear()
    monkeypatch.setenv("PSC_MANAGER_REQUIRE_IDLE", "0")
    manager_daemon.main([])
    assert seen["require_idle"] is False

    # unset -> True
    seen.clear()
    monkeypatch.delenv("PSC_MANAGER_REQUIRE_IDLE", raising=False)
    manager_daemon.main([])
    assert seen["require_idle"] is True

    # unset 加 --no-require-idle -> False
    seen.clear()
    monkeypatch.delenv("PSC_MANAGER_REQUIRE_IDLE", raising=False)
    manager_daemon.main(["--no-require-idle"])
    assert seen["require_idle"] is False

    # env 1 加 --no-require-idle -> False
    seen.clear()
    monkeypatch.setenv("PSC_MANAGER_REQUIRE_IDLE", "1")
    manager_daemon.main(["--no-require-idle"])
    assert seen["require_idle"] is False


@pytest.mark.parametrize("bad_val", ["NaN", "nan", "Inf", "-Inf", "0", "-1"])
def test_subprocess_main_rejects_illegal_max_load(bad_val):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "paulsha_cortex.coordinator.manager_daemon",
            "--max-load",
            bad_val,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "error:" in result.stderr
