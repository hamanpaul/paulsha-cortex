from __future__ import annotations

from pathlib import Path
from typing import Iterator, Mapping
import os
import shutil
import subprocess

import pytest

import network_guard
from socket_fixtures import short_socket_dir


# --- #608：AF_UNIX socket 路徑不得吃 TMPDIR 長度 ------------------------------


@pytest.fixture
def socket_dir() -> "Iterator[Path]":
    """短固定根底下的空目錄，專供要 `bind()` / `connect()` 的測試放 socket。

    `tmp_path` 掛在 `TMPDIR` 下，長度由環境決定，超過 `sun_path` 的 107 bytes
    就整批紅掉——而那種紅會被 manager 的 gate ledger 記成「交付沒過」。理由與
    實測數據見 `tests/socket_fixtures.py` 的模組 docstring。

    只把 **socket 本身**搬過來；工作區／設定檔／快照照舊留在 `tmp_path`。
    """

    with short_socket_dir(prefix="pytest") as path:
        yield path


# --- #610：測試不得出實網 ------------------------------------------------------
#
# builder 在 codex sandbox 內跑全套會被 egress 攔截整個殺掉（run 7812 死在 69%
# 的 `git -C <真實 repo checkout> fetch origin main`），而正常環境網路是通的，
# 缺陷從測試結果上完全看不出來。守衛把「靜默出網」變成當場失敗並指名測試。
# 逃生口 `PSC_TEST_ALLOW_NETWORK=1` 在這裡讀——早於底下會清掉 `PSC_*` 的
# `_clear_runtime_env`。


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "network: 本質上需要真實網路的整合測試；預設全套不跑（需 --run-network）。",
    )
    network_guard.install()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="#610：連 @pytest.mark.network 的整合測試一起跑（會打真實網路）。",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-network"):
        return
    skip_network = pytest.mark.skip(
        reason="#610：需要真實網路的整合測試，預設排除於全套（--run-network 才跑）"
    )
    for item in items:
        if item.get_closest_marker("network") is not None:
            item.add_marker(skip_network)


def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """讓守衛在任何 setup/call/teardown 出網時都能指名是哪個測試。"""

    network_guard.set_current_test(item.nodeid)
    return None


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest):
    """守衛的 per-test 帳本。

    直接 raise 有可能被受測程式的 ``except Exception`` 吞掉（`_run_git` 就是
    這種形狀），所以違規同時記在帳本裡，teardown 時無論如何都讓該測試失敗。
    """

    network_guard.drain_violations()
    if request.node.get_closest_marker("network") is not None:
        with network_guard.allow_network():
            yield
        network_guard.drain_violations()
        return
    yield
    violations = network_guard.drain_violations()
    if violations:
        pytest.fail("\n\n".join(violations), pytrace=False)


@pytest.fixture(autouse=True)
def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep tests hermetic against operator shell/runtime bootstrap variables.

    Clearing PSC_* alone is not enough: paulsha_cortex.config.runtime falls
    back to the *installed* instance under the real ``$HOME`` (bootstrap env
    file, then ``Path.home() / ".agents"``) whenever a root is unset. Without
    an explicit redirect, any test that forgets to isolate its own coordinator
    root (JobRegistry(), IdentityRegistry(), paths.coordinator_root(), ...)
    silently reads (and, on schema migration, could even write) the operator's
    real production state — see #303. Point the whole PSC_AGENTS_ROOT family
    (coordinator/control/specs/monitor/project-config/run root all derive
    from it, see config/runtime.py RUNTIME_ROOT_DEFAULTS) plus PSC_CONFIG_ROOT
    at an empty per-test directory by default; tests that need specific fixture
    data still monkeypatch these explicitly afterwards, which overrides this.
    """
    for name in tuple(os.environ):
        if name.startswith("PSC_") or name == "PAULSHACLAW_CONFIG":
            monkeypatch.delenv(name, raising=False)
    unset_root = tmp_path / "unset-psc-root-guard"
    # #612：`PSC_REPO_ROOT` 與 `PSC_AGENTS_ROOT` 是同一類洩漏，只是洩漏的目標不同。
    # `paths.repo_root()` 舊實作未宣告時退回 `Path.cwd()`，而跑測試的 cwd 就是
    # operator 的**真實 cortex checkout**，於是任何忘了指定目標 repo 的測試都在真
    # repo 上跑 git——#610 的實網事故（`git -C <真 checkout> fetch origin main`
    # 打到 github.com）就是這樣來的，`worktree_reclaim` 的
    # `git worktree remove --force`／`prune` 更是**寫入**動作。production 側自
    # #612 起 fail-closed（未宣告即 `RepoRootUnresolvedError`），測試側則比照
    # `PSC_AGENTS_ROOT` 指向 per-test 暫存路徑：需要真 repo 的測試自行 setenv／
    # 建 fixture repo 覆寫，需要驗「未宣告」行為的測試自行 delenv。
    monkeypatch.setenv("PSC_REPO_ROOT", str(unset_root / "repo"))
    monkeypatch.setenv("PSC_AGENTS_ROOT", str(unset_root / "agents"))
    monkeypatch.setenv("PSC_CONFIG_ROOT", str(unset_root / "config"))
    # Degraded-launch tests exercise production provisioning. Give them a real
    # deployment-owned control/credential authority rather than permitting the
    # old silent fallback to role HOME or generated stubs.
    roots: set[Path] = set()
    import sys
    for module in tuple(sys.modules.values()):
        isolated = getattr(module, "_ISOLATED_AGENTS_ROOT", None)
        if isolated:
            roots.add(Path(isolated).resolve())
    for agents in roots:
        for principal in ("builder", "reviewer"):
            for controls in (agents / "config" / "codex-controls" / principal,):
                (controls / "plugins").mkdir(parents=True, exist_ok=True)
                (controls / "skills").mkdir(exist_ok=True)
                (controls / "config.toml").write_text("# hermetic deployment policy\n")
                (controls / "hooks.json").write_text("{}\n")
            for credential in (
                agents / "config" / "codex-credentials" / principal / "auth.json",
            ):
                credential.parent.mkdir(parents=True, exist_ok=True)
                credential.write_text("{}\n")
    # #506：auto-claim scan 的 GitHub 節流在生產預設 1000ms／請求。測試不打真的
    # GitHub，也不該為了節流而真的 sleep——預設關閉，需要驗證節流行為的測試自行
    # setenv 覆寫並注入 sleeper。
    monkeypatch.setenv("PSC_MANAGER_GITHUB_INTERVAL_MS", "0")


@pytest.fixture(autouse=True)
def _prefer_local_openspec(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    wrapper = repo_root / "scripts" / "openspec"
    if not wrapper.exists():
        return

    real_openspec = shutil.which("openspec")
    if real_openspec:
        monkeypatch.setenv("PAULSHA_REAL_OPENSPEC", real_openspec)

    original_path = os.environ.get("PATH", "")
    wrapper_parent = str(wrapper.parent.resolve())
    if wrapper_parent not in original_path.split(os.pathsep):
        monkeypatch.setenv("PATH", f"{wrapper_parent}{os.pathsep}{original_path}")


# --- #506 / D2：git-native remote reads 用的真 git fixture --------------------
#
# monitor 的 remote 檔案讀取與 merge ancestry 已改走本機 git（``monitor/git_mirror``）。
# 這類測試一律用本機 tmp git repo，**不打**任何真實 GitHub API／網路：
# ``origin`` 的 URL 字面值寫成 GitHub HTTPS（鏡像的身分驗證讀的是 raw config），
# 實際 transport 由 ``url.<local>.insteadOf`` 改寫到同一個 tmp 目錄下的 bare repo。


class GitOriginFixture:
    """一組 bare ``origin`` ＋ 一個本機 checkout，對外偽裝成 GitHub repo。"""

    def __init__(self, root: Path, repo: str) -> None:
        self.repo = repo
        self.root = root
        self.origin = root / "origin.git"
        self.checkout = root / "checkout"
        self.url = f"https://github.com/{repo}.git"
        # ``-b main``：bare repo 的 HEAD 必須指向真的會存在的分支，否則
        # ``git clone --depth=1`` 會把它當成空 repo。
        self._git(("init", "--quiet", "--bare", "-b", "main", str(self.origin)), cwd=root)
        self._git(("init", "--quiet", "-b", "main", str(self.checkout)), cwd=root)
        self.git("remote", "add", "origin", self.url)
        self.git("config", f"url.{self.origin}.insteadOf", self.url)
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "fixture")

    @staticmethod
    def _git(argv: tuple[str, ...], *, cwd: Path) -> str:
        completed = subprocess.run(
            ("git", *argv),
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def git(self, *argv: str) -> str:
        return self._git(argv, cwd=self.checkout)

    def commit(self, files: Mapping[str, str], *, message: str = "commit") -> str:
        for relative, text in files.items():
            target = self.checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            self.git("add", "--", relative)
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def branch_commit(
        self,
        branch: str,
        files: Mapping[str, str],
        *,
        message: str = "branch commit",
        base: str = "main",
    ) -> str:
        self.git("checkout", "--quiet", "-b", branch, base)
        head = self.commit(files, message=message)
        self.git("checkout", "--quiet", "main")
        return head

    def merge(self, branch: str, *, message: str = "merge") -> str:
        """真的 merge commit（``--no-ff``），parents >= 2。"""

        self.git("merge", "--quiet", "--no-ff", "-m", message, branch)
        return self.git("rev-parse", "HEAD")

    def blob_sha(self, path: str, *, revision: str = "HEAD") -> str:
        return self.git("rev-parse", f"{revision}:{path}")

    def head(self, revision: str = "HEAD") -> str:
        return self.git("rev-parse", revision)

    def publish(self, branch: str = "main") -> None:
        """把 checkout 的分支推到 bare origin（模擬遠端已經前進）。"""

        self.git("push", "--quiet", "origin", f"{branch}:{branch}")

    def publish_pull_head(self, number: int, revision: str) -> None:
        """在 origin 上建立 ``refs/pull/<n>/head``（GitHub 才有的唯讀 ref）。"""

        self.git("push", "--quiet", "origin", f"{revision}:refs/pull/{number}/head")

    def detach(self) -> Path:
        """回傳一個『只有 origin 設定、還沒有任何物件』的空 checkout。"""

        empty = self.root / "empty-checkout"
        self._git(("init", "--quiet", "-b", "main", str(empty)), cwd=self.root)
        self._git(("remote", "add", "origin", self.url), cwd=empty)
        self._git(("config", f"url.{self.origin}.insteadOf", self.url), cwd=empty)
        return empty


@pytest.fixture
def git_origin(tmp_path: Path):
    """factory：``git_origin("example/acme")`` → :class:`GitOriginFixture`。"""

    created: list[GitOriginFixture] = []

    def _make(repo: str = "example/acme") -> GitOriginFixture:
        root = tmp_path / f"gitfixture-{len(created)}"
        root.mkdir(parents=True, exist_ok=True)
        fixture = GitOriginFixture(root, repo)
        created.append(fixture)
        return fixture

    return _make
