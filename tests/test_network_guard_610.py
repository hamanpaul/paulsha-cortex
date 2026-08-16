"""#610：網路守衛的自證測試。

守衛本身是「防第五次同族事件」的唯一機制，因此它必須被證明**會抓**（真的對外
連線／spawn 網路 client 時失敗並指名測試）而且**會放行**（loopback、AF_UNIX、
本機 git remote 一律不受影響）。

本檔案刻意**不發出任何真實封包**：守衛在 syscall／spawn 之前就 raise，所以
「會抓」的案例對 sandbox 是安全的；「會放行」的案例則用純判準函式或本機
transport（file path remote，git 走本機不開 socket）驗證。
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

import network_guard
from network_guard import NetworkGuardViolation

#: TEST-NET-3（RFC 5737），任何情況下都不該真的被連上；守衛會先擋下來。
UNROUTABLE = ("203.0.113.7", 443)

#: 逃生口打開時整個守衛不安裝，這份自證測試自然不適用。
pytestmark = pytest.mark.skipif(
    not network_guard.enabled(),
    reason=f"{network_guard.ALLOW_ENV} 已開，守衛停用；本檔案是守衛自身的自證測試",
)


@pytest.fixture
def swallowed_violations():
    """收下本測試刻意觸發的違規，讓 conftest 的 teardown 帳本不會誤判。"""

    network_guard.drain_violations()
    yield
    assert network_guard.drain_violations(), "預期至少記下一筆違規"


# --- 守衛預設啟用 --------------------------------------------------------------


def test_guard_is_installed_by_default() -> None:
    assert network_guard.enabled() is True
    assert network_guard.ALLOW_ENV == "PSC_TEST_ALLOW_NETWORK"


def test_violation_message_names_the_current_test(swallowed_violations) -> None:
    with pytest.raises(NetworkGuardViolation) as excinfo:
        network_guard.check_socket_address(socket.AF_INET, UNROUTABLE)
    message = str(excinfo.value)
    assert "test_violation_message_names_the_current_test" in message
    assert "203.0.113.7:443" in message
    assert network_guard.ALLOW_ENV in message


# --- socket 層 -----------------------------------------------------------------


def test_outbound_tcp_connect_is_blocked_before_any_packet(swallowed_violations) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkGuardViolation):
            sock.connect(UNROUTABLE)
    finally:
        sock.close()


def test_outbound_connect_ex_is_blocked(swallowed_violations) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkGuardViolation):
            sock.connect_ex(UNROUTABLE)
    finally:
        sock.close()


def test_create_connection_is_blocked(swallowed_violations) -> None:
    with pytest.raises(NetworkGuardViolation):
        socket.create_connection(UNROUTABLE, timeout=0.01)


@pytest.mark.parametrize(
    "family, address",
    [
        (socket.AF_INET, ("127.0.0.1", 8765)),
        (socket.AF_INET, ("localhost", 8765)),
        (socket.AF_INET, ("", 8765)),
        (socket.AF_INET, ("0.0.0.0", 8765)),
        (socket.AF_INET6, ("::1", 8765, 0, 0)),
        (socket.AF_INET6, ("::ffff:127.0.0.1", 8765, 0, 0)),
        (socket.AF_UNIX, "/run/whatever.sock"),
        (socket.AF_NETLINK, (0, 0)),
    ],
)
def test_local_socket_targets_are_allowed(family, address) -> None:
    network_guard.check_socket_address(family, address)


def test_loopback_tcp_round_trip_is_untouched() -> None:
    """真的起一個 loopback listener 並連上去——守衛不得干擾本機通訊。"""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            listener.bind(("127.0.0.1", 0))
        except OSError as exc:  # sandbox 可能連 bind 都擋（見 #586）
            pytest.skip(f"runtime forbids binding a loopback listener: {exc}")
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            client.settimeout(5)
            client.connect(listener.getsockname())
            served, _ = listener.accept()
            with served:
                served.sendall(b"pong")
            assert client.recv(4) == b"pong"
        finally:
            client.close()
    finally:
        listener.close()


# --- subprocess 層：git --------------------------------------------------------


def _init_repo(root: Path, *, origin: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    if origin is not None:
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    return root


def test_git_fetch_against_a_github_origin_is_blocked(tmp_path: Path, swallowed_violations) -> None:
    """#610 真兇一號的最小重現：origin 指到 github.com 的 repo 上跑 fetch。"""

    repo = _init_repo(tmp_path / "repo", origin="https://github.com/hamanpaul/paulsha-cortex.git")
    with pytest.raises(NetworkGuardViolation) as excinfo:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "--no-tags", "origin", "main"],
            capture_output=True,
            text=True,
        )
    assert "github.com" in str(excinfo.value)


def test_git_fetch_with_implicit_origin_is_blocked(tmp_path: Path, swallowed_violations) -> None:
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:hamanpaul/paulsha-cortex.git")
    with pytest.raises(NetworkGuardViolation):
        subprocess.run(["git", "-C", str(repo), "fetch"], capture_output=True, text=True)


def test_git_clone_from_a_remote_url_is_blocked(tmp_path: Path, swallowed_violations) -> None:
    with pytest.raises(NetworkGuardViolation):
        subprocess.run(
            ["git", "clone", "https://github.com/hamanpaul/paulsha-cortex.git", str(tmp_path / "x")],
            capture_output=True,
            text=True,
        )


def test_git_ls_remote_against_a_remote_url_is_blocked(tmp_path: Path, swallowed_violations) -> None:
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(NetworkGuardViolation):
        subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "https://github.com/example/acme.git"],
            capture_output=True,
            text=True,
        )


def test_local_git_remote_is_allowed(tmp_path: Path) -> None:
    """本機 bare repo 當 remote：fetch 全程走檔案，守衛不得擋。"""

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)
    seed = _init_repo(tmp_path / "seed", origin=str(bare))
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "--quiet", "-m", "seed"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "--quiet", "origin", "main:main"], check=True)

    consumer = _init_repo(tmp_path / "consumer", origin=str(bare))
    completed = subprocess.run(
        ["git", "-C", str(consumer), "fetch", "--no-tags", "origin", "main"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_insteadof_rewritten_github_origin_is_allowed(git_origin) -> None:
    """conftest 的 `git_origin`：origin 字面值是 GitHub，transport 被 insteadOf
    改寫到本機 bare。守衛必須看**改寫後**的 URL，否則整批既有 hermetic 測試會
    被自己的守衛誤殺。"""

    origin = git_origin("example/acme")
    origin.commit({"README.md": "seed\n"})
    origin.publish()
    completed = subprocess.run(
        ["git", "-C", str(origin.checkout), "fetch", "--no-tags", "origin", "main"],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_local_git_subcommands_are_not_treated_as_network(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo", origin="https://github.com/example/acme.git")
    for argv in (
        ["git", "-C", str(repo), "status", "--porcelain"],
        ["git", "-C", str(repo), "rev-parse", "--git-dir"],
        ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
        ["git", "-C", str(repo), "for-each-ref", "refs/remotes"],
    ):
        completed = subprocess.run(argv, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr


# --- subprocess 層：純網路 client ----------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "api", "repos/hamanpaul/paulsha-cortex"],
        ["gh", "pr", "list", "--head", "feature/x"],
        ["curl", "https://example.invalid"],
        ["wget", "https://example.invalid"],
        ["pip", "install", "requests"],
    ],
)
def test_network_clients_are_blocked(argv, swallowed_violations) -> None:
    with pytest.raises(NetworkGuardViolation):
        network_guard.check_command(argv)


@pytest.mark.parametrize("argv", [["gh", "--version"], ["curl", "--version"], ["pip", "--help"]])
def test_offline_only_invocations_of_network_clients_are_allowed(argv) -> None:
    network_guard.check_command(argv)


def test_shell_string_commands_are_inspected(swallowed_violations) -> None:
    with pytest.raises(NetworkGuardViolation):
        network_guard.check_command("gh api repos/hamanpaul/paulsha-cortex", shell=True)


# --- 帳本：例外被吞掉仍要失敗 --------------------------------------------------


def test_swallowed_violation_is_still_recorded_in_the_ledger() -> None:
    """`verification._run_git` 這種 `except Exception:` 形狀會吞掉守衛的例外；
    帳本是後盾，conftest 的 teardown 會據此讓測試失敗。"""

    network_guard.drain_violations()
    try:
        network_guard.check_command(["gh", "api", "repos/x/y"])
    except Exception:  # noqa: BLE001 - 刻意模擬受測程式吞例外
        pass
    recorded = network_guard.drain_violations()
    assert len(recorded) == 1
    assert "test_swallowed_violation_is_still_recorded_in_the_ledger" in recorded[0]
    assert not network_guard.drain_violations(), "drain 後帳本必須清空"


# --- 逃生口 --------------------------------------------------------------------


def test_allow_network_context_manager_disables_the_guard() -> None:
    with network_guard.allow_network():
        network_guard.check_socket_address(socket.AF_INET, UNROUTABLE)
        network_guard.check_command(["gh", "api", "repos/x/y"])
        network_guard.check_command(["git", "clone", "https://github.com/example/acme.git", "/tmp/x"])
    assert not network_guard.drain_violations(), "放行期間不得留下違規紀錄"
    with pytest.raises(NetworkGuardViolation):
        network_guard.check_command(["gh", "api", "repos/x/y"])
    network_guard.drain_violations()


def test_network_marker_is_registered(pytestconfig: pytest.Config) -> None:
    markers = pytestconfig.getini("markers")
    assert any(marker.startswith("network:") for marker in markers)
