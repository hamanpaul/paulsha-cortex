"""issue #608：AF_UNIX `sun_path` 108-byte 上限的環境敏感性（ledger gate 家族第三例）。

家族背景：manager 的 gate ledger 對 candidate 重跑全套 pytest 是採信的硬 gate
（#540）。凡是「全套 pytest 的結果會隨 host 環境形狀改變」的測試都是 P1——它們
污染的是**採信判斷**，一次環境噪音就會讓合格 candidate 撞上
`GateContradictionError`。#565（`/tmp/.git` 污染）、#586（sandbox 擋 `bind`）之後
這是第三例：`TMPDIR` 一長，pytest `tmp_path` 底下的 socket 路徑就超過
`sun_path` 的 107 bytes 可用上限，`bind()` 失敗。

本檔釘四層：

1. **常數契約**——107／108 與 byte（非字元）計算。
2. **fixture 的保證**——`socket_fixtures` 產生的路徑與 `TMPDIR` 長度無關。
3. **production fail-closed**——真的超限時，錯誤是指名道姓的
   `SocketPathTooLongError`（附 byte 數），不是一句沒有出處的 `OSError`，也不是
   「服務沒起來」「socket 沒在聽」這種與缺陷混淆的空結果。
4. **最重要的不變式**——長 `TMPDIR` 不得在 gate ledger 上產生一筆看起來像
   「gate 失敗」的記錄。第 4 層附**負控制**：同一條路徑上，真正失敗的 gate 仍然
   必須記成 `failed`。沒有負控制，第 4 層可以靠「什麼都記成 passed」作弊通過。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from socket_fixtures import (
    SOCKET_NAME_HEADROOM_BYTES,
    short_socket_dir,
    short_socket_root,
)

from paulsha_cortex.coordinator import gate_ledger
from paulsha_cortex.doctor import _monitor_path_probes
from paulsha_cortex.monitor.config import MonitorConfig
from paulsha_cortex.monitor.server import MonitorServer
from paulsha_cortex.monitor.snapshot import SnapshotStore
from paulsha_cortex.monitor.socket_path import (
    SUN_PATH_MAX_BYTES,
    SUN_PATH_MAX_USABLE_BYTES,
    SocketPathTooLongError,
    socket_path_fits,
    socket_path_length,
    validate_socket_path,
)
from paulsha_cortex.monitor.work_api import MonitorSocketClient

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: 一個「CI／sandbox 給了長暫存根」的忠實模擬。長度刻意落在實測會讓
#: `test_monitor_work_api` 三測與 `test_doctor` 一測全紅的區間（見
#: `socket_fixtures` 模組 docstring 的表）。
_HOSTILE_TMPDIR_NAME = "psc608-hostile-" + ("a" * 32)

#: 修復前會因 `sun_path` 超限而紅掉的測試（#608 內文點名三個，實測還有第四個）。
_FRAGILE_NODE_IDS = (
    "tests/test_monitor_work_api.py::test_socket_work_item_read_apis_and_subscription_preserve_legacy",
    "tests/test_monitor_work_api.py::test_work_subscription_can_scope_duplicate_work_id_by_repo",
    "tests/test_monitor_work_api.py::test_old_server_teardown_does_not_unlink_replacement_socket",
    "tests/test_doctor.py::test_monitor_protocol_probe_rejects_transport_only_listener",
)


# --- 1. 常數與計算契約 --------------------------------------------------------


def test_sun_path_limits_are_the_kernel_numbers() -> None:
    """108 是 `struct sockaddr_un.sun_path` 的長度，可用的是 107（最後一 byte 是 NUL）。"""

    assert SUN_PATH_MAX_BYTES == 108
    assert SUN_PATH_MAX_USABLE_BYTES == 107


def test_length_is_counted_in_bytes_not_characters() -> None:
    """非 ASCII 路徑一個字元不只一個 byte；kernel 收的是 bytes。

    用 `len(str(path))` 會低估，剛好在中文目錄名底下把超限的路徑判成合法——
    那正是「量錯了所以 fail-closed 沒有生效」的形狀。
    """

    path = Path("/tmp/" + "目錄" * 20)  # 40 個字元，UTF-8 下 120 bytes
    assert len(str(path)) < SUN_PATH_MAX_USABLE_BYTES
    assert socket_path_length(path) > SUN_PATH_MAX_USABLE_BYTES
    assert not socket_path_fits(path)


def test_boundary_is_inclusive_at_107_and_rejects_108() -> None:
    fits = Path("/" + "a" * (SUN_PATH_MAX_USABLE_BYTES - 1))
    assert socket_path_length(fits) == SUN_PATH_MAX_USABLE_BYTES
    assert socket_path_fits(fits)
    assert validate_socket_path(fits) == fits

    over = Path("/" + "a" * SUN_PATH_MAX_USABLE_BYTES)
    assert socket_path_length(over) == SUN_PATH_MAX_BYTES
    assert not socket_path_fits(over)
    with pytest.raises(SocketPathTooLongError):
        validate_socket_path(over)


def test_too_long_error_is_not_an_oserror() -> None:
    """`SocketPathTooLongError` 不得被 `except OSError` 撈走。

    呼叫端普遍用 `except OSError` 表示「transport 出事（沒在聽／連不上）」。路徑
    過長是環境前置條件不成立，兩者的處置完全不同；掛在 `OSError` 下就會被既有的
    transport 錯誤處理吸收，重新變回無法區分的症狀。
    """

    assert not issubclass(SocketPathTooLongError, OSError)
    assert issubclass(SocketPathTooLongError, ValueError)


def test_diagnostic_names_the_numbers_and_the_environment_knob() -> None:
    over = Path("/tmp/" + "a" * 200)
    with pytest.raises(SocketPathTooLongError) as excinfo:
        validate_socket_path(over, role="monitor socket")
    message = str(excinfo.value)
    assert "monitor socket" in message
    assert str(SUN_PATH_MAX_USABLE_BYTES) in message
    assert str(socket_path_length(over)) in message
    # 讀到訊息的人要能當場判斷「這是環境，不是缺陷」，並知道該調哪個旋鈕。
    assert "PSC_RUN_ROOT" in message and "TMPDIR" in message
    assert "#608" in message


# --- 2. fixture 對 TMPDIR 免疫 ------------------------------------------------


def test_short_socket_root_ignores_a_hostile_tmpdir(monkeypatch, tmp_path: Path) -> None:
    """`TMPDIR` 再長，fixture 給的 socket 目錄仍然短。"""

    hostile = tmp_path / _HOSTILE_TMPDIR_NAME
    hostile.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(hostile))
    monkeypatch.setattr("tempfile.tempdir", str(hostile))

    root = short_socket_root()
    assert str(hostile) not in str(root)

    with short_socket_dir(prefix="probe") as directory:
        socket_path = directory / "project-monitor.sock"
        assert socket_path_fits(socket_path)
        # 還要留得下更深一層（stage9 的 `run/project-monitor.sock`）。
        assert (
            socket_path_length(directory) + 1 + SOCKET_NAME_HEADROOM_BYTES
            <= SUN_PATH_MAX_USABLE_BYTES
        )


def test_short_socket_dirs_are_unique_and_cleaned_up() -> None:
    with short_socket_dir(prefix="a") as first, short_socket_dir(prefix="b") as second:
        assert first != second
        assert first.is_dir() and second.is_dir()
        leaked = first
    assert not leaked.exists()


def test_a_real_bind_succeeds_under_a_hostile_tmpdir(monkeypatch, tmp_path: Path) -> None:
    """端到端：`TMPDIR` 惡意加長時，透過 fixture 建的 socket 仍然 bind 得起來。"""

    import socket as socket_module

    hostile = tmp_path / _HOSTILE_TMPDIR_NAME
    hostile.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(hostile))
    monkeypatch.setattr("tempfile.tempdir", str(hostile))

    with short_socket_dir(prefix="bind") as directory:
        sock = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        try:
            sock.bind(str(directory / "p.sock"))
        finally:
            sock.close()


def test_negative_control_tmpdir_rooted_socket_would_have_failed(tmp_path: Path) -> None:
    """負控制：證明模擬的敵意環境是忠實的。

    同一個長 `TMPDIR` 下，**沿用舊寫法**（socket 掛在 `tmp_path` 底下）產生的路徑
    確實超過 `sun_path` 上限。沒有這條，上面那些「修好了」的斷言可能只是因為模擬
    的環境根本不夠長。
    """

    old_style = tmp_path / _HOSTILE_TMPDIR_NAME / "pytest-of-user" / "pytest-1" / "monitor.sock"
    assert not socket_path_fits(old_style)


# --- 3. production fail closed 且說得出原因 -----------------------------------


def _over_long_socket_path() -> Path:
    root = short_socket_root()
    padding = "p" * (SUN_PATH_MAX_USABLE_BYTES - socket_path_length(root))
    path = root / padding / "monitor.sock"
    assert not socket_path_fits(path)
    return path


def test_server_refuses_an_over_long_socket_path_with_a_named_error() -> None:
    """`serve_forever` 必須 fail closed 在 `SocketPathTooLongError` 上。

    修復前是 `OSError: AF_UNIX path too long`——沒有 byte 數、沒有上限、沒有路徑，
    而且在 threaded 呼叫端只表現為 `wait_until_ready()` 回 `False`。
    """

    server = MonitorServer(
        store=SnapshotStore(config=MonitorConfig(workspaces=())),
        socket_path=_over_long_socket_path(),
    )
    with pytest.raises(SocketPathTooLongError) as excinfo:
        server.serve_forever()
    assert str(SUN_PATH_MAX_USABLE_BYTES) in str(excinfo.value)


# thread 內 raise 正是本測試要驗的行為，pytest 的 unhandled-thread 警告是預期噪音。
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_server_records_startup_error_for_threaded_callers() -> None:
    """背景 thread 跑 `serve_forever` 時，原因不得就這樣消失。

    `wait_until_ready()` 回 `False` 對「環境不合格」與「服務有缺陷」是同一個空
    結果；`startup_error` 是讓兩者可區分的那條線。
    """

    server = MonitorServer(
        store=SnapshotStore(config=MonitorConfig(workspaces=())),
        socket_path=_over_long_socket_path(),
    )
    assert server.startup_error is None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    thread.join(timeout=5.0)

    assert not server.wait_until_ready(timeout=0.1)
    assert isinstance(server.startup_error, SocketPathTooLongError)


def test_socket_client_reports_length_rather_than_a_transport_error() -> None:
    """client 端超限時不得退化成「monitor 沒在聽」。"""

    client = MonitorSocketClient(socket_path=_over_long_socket_path(), timeout=0.1)
    with pytest.raises(SocketPathTooLongError):
        client.request({"kind": "list_work_items"})


def test_doctor_names_the_sun_path_limit_instead_of_not_listening(tmp_path: Path) -> None:
    """`cortex doctor` 要在 live probe 之前就說出「路徑太長」。

    否則 bind／connect 的失敗會被記成 `monitor socket is not listening`——與
    「monitor 根本沒在跑」無法區分，排障方向直接被帶偏。
    """

    _state, monitor_socket = _monitor_path_probes(
        state_root=tmp_path / "state",
        socket_path=_over_long_socket_path(),
        live=True,
    )
    assert monitor_socket.status == "fail"
    assert "sun_path" in monitor_socket.detail
    assert "not listening" not in monitor_socket.detail


# --- 4. ledger 不變式：環境形狀不得偽造出一筆 gate 失敗 -----------------------


def _run_gate_under_hostile_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    argv: str,
) -> dict[str, object]:
    """以敵意長 `TMPDIR` 跑一個宣告出來的 gate，回傳 manager 寫出的 ledger。

    走的是 production 的 `gate_ledger.write_gate_ledger`，不是自己模擬——要驗的
    正是「manager 實際寫進 ledger 的那份東西」。
    """

    hostile = tmp_path / _HOSTILE_TMPDIR_NAME
    hostile.mkdir(parents=True)
    assert len(str(hostile)) > 40, "模擬的 TMPDIR 不夠長，測試會失去意義"

    monkeypatch.setenv("TMPDIR", str(hostile))
    ledger_path = tmp_path / "gate-ledger.json"
    env = dict(os.environ)
    env["PSC_GATE_CMD_PYTEST"] = argv
    env["PSC_SLICE_ID"] = "slice-608"

    gate_ledger.write_gate_ledger(
        ledger_path=ledger_path,
        worktree=_REPO_ROOT,
        env=env,
    )
    return json.loads(ledger_path.read_text(encoding="utf-8"))


def test_long_tmpdir_cannot_manufacture_a_failed_gate_ledger_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**本 issue 的核心不變式。**

    以敵意長 `TMPDIR` 讓 manager 重跑修復前必紅的那四個測試，ledger 必須記成
    `passed`。若這裡出現 `failed`，manager 會把「這台機器的暫存根太長」讀成
    「這次交付真的沒過」，合格 candidate 撞 `GateContradictionError`——正是
    #565／#586／#608 這一族要根除的東西。
    """

    argv = " ".join(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *_FRAGILE_NODE_IDS]
    )
    ledger = _run_gate_under_hostile_tmpdir(tmp_path, monkeypatch, argv=argv)

    rows = {row["name"]: row for row in ledger["gates"]}
    assert set(rows) == {"pytest"}
    row = rows["pytest"]
    assert row["status"] == "passed", (
        "長 TMPDIR 在 gate ledger 上偽造出一筆 gate 失敗（#608）：" f"{row['detail']}"
    )
    assert row["exit_code"] == 0


def test_fragile_tests_run_rather_than_skip_under_a_long_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """靜默 skip 也是失敗的一種——覆蓋沒了，而理由還是假的。

    #586 的 `af_unix_bind_available()` 探針原本自己也建在 `TMPDIR` 底下：`TMPDIR`
    夠長時探針先超限，`bind()` 失敗被判成「sandbox 禁止 bind」，整個 AF_UNIX 家族
    帶著**不成立的理由**靜默 skip。套件綠、ledger 綠、覆蓋卻是空的。
    """

    hostile = tmp_path / _HOSTILE_TMPDIR_NAME
    hostile.mkdir(parents=True)
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", *_FRAGILE_NODE_IDS],
        cwd=str(_REPO_ROOT),
        env={**os.environ, "TMPDIR": str(hostile)},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"{len(_FRAGILE_NODE_IDS)} passed" in completed.stdout
    assert "skipped" not in completed.stdout


def test_negative_control_a_genuinely_failing_gate_is_still_recorded_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """負控制：上面那條不得靠「什麼都記成 passed」作弊。

    同一條 ledger 路徑、同一個敵意 `TMPDIR`，真正失敗的 gate 仍然必須是 `failed`。
    這條與上面兩條合起來才構成「環境失敗與交付失敗可區分」的證明。
    """

    argv = f'{sys.executable} -c "import sys; sys.exit(3)"'
    ledger = _run_gate_under_hostile_tmpdir(tmp_path, monkeypatch, argv=argv)

    row = {item["name"]: item for item in ledger["gates"]}["pytest"]
    assert row["status"] == "failed"
    assert row["exit_code"] == 3
