"""issue #618：`cortex service run` 是 trust-root Phase 2b system unit 的 ExecStart。

permgen 產生的 `ExecStart=<venv>/bin/cortex service run` 必須真的可執行——
這條契約在 #618 之前只存在於產生器端，CLI 沒有對應 verb，unit 一 start 就
以 `unsupported service command` 失敗。
"""

from __future__ import annotations

import argparse

import pytest

from paulsha_cortex.porcelain import service
from paulsha_cortex.trust_root import permgen


def test_run_verb_listed_in_service_help() -> None:
    parser = service._build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert "run" in action.choices


def test_run_forwards_argv_to_manager_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_main(argv: list[str]) -> int:
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(
        "paulsha_cortex.coordinator.manager_daemon.main", fake_main, raising=True
    )
    rc = service.main(["run", "--max-rounds", "1", "--no-require-idle"])

    assert rc == 0
    assert seen["argv"] == ["--max-rounds", "1", "--no-require-idle"]


def test_run_forwards_help_instead_of_consuming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--help` 也要原樣下放——service parser 不得攔截成自己的用法說明，
    否則 operator 看不到 daemon 的真實參數面。"""
    seen: dict[str, object] = {}

    def fake_main(argv: list[str]) -> int:
        seen["argv"] = list(argv)
        return 0

    monkeypatch.setattr(
        "paulsha_cortex.coordinator.manager_daemon.main", fake_main, raising=True
    )
    assert service.main(["run", "--help"]) == 0
    assert seen["argv"] == ["--help"]


def test_run_propagates_daemon_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "paulsha_cortex.coordinator.manager_daemon.main", lambda argv: 1, raising=True
    )
    assert service.main(["run"]) == 1


def test_manager_unit_execstart_matches_the_verb_cli_exposes() -> None:
    """產生器的 ExecStart 與 CLI 實際提供的 verb 必須同步——#618 就是這條斷掉。"""
    unit = permgen.build_manager_unit(permgen.SCHEMES["three-way"])
    verb = unit.exec_start.split("/bin/cortex ", 1)[1].split()

    parser = service._build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert verb[0] == "service"
    assert verb[1] in action.choices
