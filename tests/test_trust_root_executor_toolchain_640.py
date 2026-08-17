"""#640：真實 dispatch 的最後一哩——executor toolchain ＋ per-account 憑證。

背景（#640 實測）：Phase 2b 的 job unit 帶 `ProtectHome=yes`，而四個 executor 原本
全在 operator 的 HOME 底下（nvm 樹與 `~/.local/bin`）——

    $ sudo -u cortex-builder env HOME=<job HOME> codex exec --help
    /usr/bin/env: ‘node’: No such file or directory      rc=127

登記表對 toolchain 與 job 帳號的憑證**都沒有預留資產**，因此 permgen 也不會產生它們
的權限。0817 裁決：

- **(a) toolchain**：`node` 走系統層（通用 runtime）；四個模型 CLI 落進
  `<deploy_root>/toolchain`（root-owned，job／服務帳號唯讀＋可執行）。理由是「job 跑
  的是哪個版本的模型 CLI」會影響產出，那必須是**可稽核的部署決定**——實機盤點在同一
  台機器上就有兩份 `codex`（系統層 0.42.0 vs operator 的 0.147.0）。
- **(b) 憑證**：憑證**檔**由 job 帳號擁有（能自行 refresh 過期 token），**放它的目錄
  維持 root-owned**。淨效果：job 改得了自己那份憑證的內容，卻**建不了新檔、刪不掉、
  也換不掉**同目錄下的其他 root-owned 檔（例如 `codex-hooks`）。

本檔釘住六件事：

1. 兩個新資產都在登記表裡，且雙向等式仍綠；
2. toolchain 對 job 帳號**唯讀**（計畫一個 `w` 都不給）**且可執行**；
3. 憑證檔 owner 是 job 帳號、**父目錄** owner 是 root——並以**真的 OS 語意**驗
   「能改內容、不能增刪換」（見 `TestInPlaceCredentialOsSemantics` 的說明，
   單 UID 環境測得到，因為守的那條規則就是「目錄少了 `w` 位」）；
4. traverse 鏈完整（沿用 #624 的 `unreachable_hops()`），且有反向對照；
5. monitor 的 `ReadWritePaths` 仍**嚴格窄於** Manager（#622 不變式）；
6. `permgen` 仍為純函式（靜態不變式）。
"""
from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest

from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.__main__ import main
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    EXECUTOR_TOOLS,
    IN_PLACE_CONTENT_WRITE_ASSETS,
    JOB_PATH_SYSTEM_TAIL,
    TOOLCHAIN_SYSTEM_RUNTIMES,
    ExecutorShape,
    OwnerClass,
    PathLayout,
    account_can_reach,
    build_job_unit,
    build_manager_unit,
    build_monitor_unit,
    build_toolchain_plan,
    generate_plan,
    unreachable_hops,
)
from paulsha_cortex.trust_root.permgen import THREE_WAY_SCHEME as _THREE_WAY_BASE
from paulsha_cortex.trust_root.permgen import TWO_WAY_SCHEME as _TWO_WAY_BASE
from paulsha_cortex.trust_root.registry import (
    AssetTier,
    IngressKind,
    Principal,
    TrustTree,
)

#: #626：`operator`／`external` 是部署決定，模組層方案刻意留 `None`。
DEPLOYMENT_ACCOUNTS = {
    Principal.OPERATOR: "cortex-ops",
    Principal.EXTERNAL: "cortex-outbox-reader",
}
TWO_WAY_SCHEME = _TWO_WAY_BASE.with_principal_accounts(DEPLOYMENT_ACCOUNTS)
THREE_WAY_SCHEME = _THREE_WAY_BASE.with_principal_accounts(DEPLOYMENT_ACCOUNTS)
ALL_SCHEMES = [TWO_WAY_SCHEME, THREE_WAY_SCHEME]

TOOLCHAIN = "executor-toolchain"
CREDENTIAL = "builder-executor-credential"
NEW_ASSET_IDS = (TOOLCHAIN, CREDENTIAL)


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# 1. 登記表：兩個新資產存在，且雙向等式仍綠
# ---------------------------------------------------------------------------

def test_registry_equation_stays_green() -> None:
    """兩個新資產都是 `path_resolver=None`（路徑在 permgen 的 layout），不影響等式；
    但等式必須被實際跑一次——這是「新增 durable path 未登記」那條 gate 的守門人。"""
    result = registry.check_registry_equation()
    assert result.ok, result.failure_summary()


@pytest.mark.parametrize("asset_id", NEW_ASSET_IDS)
def test_new_assets_are_registered_and_fully_classified(asset_id: str) -> None:
    asset = registry.asset_by_id(asset_id)
    assert asset.tier is AssetTier.TIER_0, "兩者被竄改都直接等於對 job 帳號的任意程式碼執行"
    assert asset.writers and asset.readers
    assert asset.note.strip(), "裁決理由必須跟著資產走"
    assert asset.derived_in, "路徑推導點必須指得出來"


def test_toolchain_is_deployment_owned_and_job_accounts_are_not_writers() -> None:
    asset = registry.asset_by_id(TOOLCHAIN)
    assert asset.tree is TrustTree.MANAGER_OWNED
    assert asset.ingress_kind is IngressKind.DEPLOYMENT_WRITE
    assert asset.writers == (Principal.INSTALLER,), "只有部署身分可寫"
    # 四個模型 executor 的讀者：兩個 job persona ＋ Manager／monitor。
    for reader in (
        Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER,
        Principal.MANAGER, Principal.MONITOR,
    ):
        assert reader in asset.readers, reader


def test_credential_writer_is_the_job_persona_not_the_installer() -> None:
    """裁決 (b) 的機械落點：writer 是 job persona，才會分到 `owner_class=JOB`。

    寫成 `INSTALLER` 會讓 `classify_owner()` 落到 `DEPLOYMENT`（owner＝root），
    憑證就 refresh 不了——與裁決正好相反。這條把那個反轉釘死。
    """
    asset = registry.asset_by_id(CREDENTIAL)
    assert asset.writers == (Principal.BUILDER,)
    assert Principal.INSTALLER not in asset.writers
    assert asset.tree is TrustTree.JOB_VISIBLE
    assert permgen.classify_owner(asset) is OwnerClass.JOB


# ---------------------------------------------------------------------------
# 2. toolchain：對 job 帳號唯讀 ＋ 可執行
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_toolchain_is_root_owned_and_no_account_can_write_it(scheme) -> None:
    plan = generate_plan(scheme)
    entry = plan.by_id(TOOLCHAIN)
    assert entry.owner_class is OwnerClass.DEPLOYMENT
    assert entry.owner == scheme.deploy_account == "root"
    assert entry.is_directory, "toolchain 是一棵樹，不是單一檔"
    # 可寫面只有 root——ACL 面也一條授寫都沒有。
    assert plan.all_writable_accounts(entry) == frozenset({scheme.deploy_account})
    assert not any(acl.writable for acl in entry.acls)
    for account in sorted(scheme.declared_accounts() - {scheme.deploy_account}):
        assert account not in plan.all_writable_accounts(entry), account


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_toolchain_mode_is_readable_and_executable_but_never_writable(scheme) -> None:
    """`0755`：group／other 都有 `r` 與 `x`、都沒有 `w`。

    `x` 這一半和 `r` 一樣重要——少了它，job 走得到目錄卻 exec 不了裡面的 CLI，
    症狀又是 rc=126／127，與 #640 原本那個 `node: No such file` 幾乎難以分辨。
    """
    entry = generate_plan(scheme).by_id(TOOLCHAIN)
    assert entry.mode == 0o755, entry.mode_str
    for shift in (3, 0):  # group, other
        bits = (entry.mode >> shift) & 0o7
        assert bits & 0o4, ("可讀", entry.mode_str, shift)
        assert bits & 0o1, ("可執行", entry.mode_str, shift)
        assert not bits & 0o2, ("不可寫", entry.mode_str, shift)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_toolchain_never_appears_in_any_unit_read_write_paths(scheme) -> None:
    """ProtectSystem=strict 下 /opt 唯讀：讀與執行不受影響，因此**不需要**任何 RWP。

    這不是靠寫死排除的——writer 只有部署身分，`required_write_targets()` 就機械地
    不會把它算進任何帳號的可寫面。
    """
    toolchain = DEFAULT_LAYOUT.asset_paths()[TOOLCHAIN]
    units = [
        build_manager_unit(scheme, DEFAULT_LAYOUT),
        build_monitor_unit(scheme, DEFAULT_LAYOUT),
        build_job_unit(scheme, DEFAULT_LAYOUT),
    ]
    for unit in units:
        assert not any(_within(toolchain, rwp) for rwp in unit.read_write_paths), (
            unit.unit_name, unit.read_write_paths,
        )


def test_toolchain_lives_in_the_deployment_tree_not_the_state_tree() -> None:
    """它是隨版本走的部署產物（跟 venv／shim 同一棵樹），不是隨 instance 走的資料。"""
    assert DEFAULT_LAYOUT.toolchain_root == "/opt/cortex/toolchain"
    assert _within(DEFAULT_LAYOUT.toolchain_root, DEFAULT_LAYOUT.deploy_root)
    assert not _within(DEFAULT_LAYOUT.toolchain_root, DEFAULT_LAYOUT.agents_root)
    assert DEFAULT_LAYOUT.asset_paths()[TOOLCHAIN] == DEFAULT_LAYOUT.toolchain_root


# ---------------------------------------------------------------------------
# 3. 憑證：檔 job-owned、目錄 root-owned
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_credential_file_is_owned_by_the_job_account(scheme) -> None:
    entry = generate_plan(scheme).by_id(CREDENTIAL)
    builder = scheme.resolve(Principal.BUILDER)
    assert entry.owner == builder, scheme.scheme_id
    assert entry.group == scheme.group_of(builder)
    assert not entry.is_directory, "憑證是單一檔——目錄型會讓整層被 chown 給 job"
    assert entry.mode == 0o600, entry.mode_str
    # 跨帳號一條 ACL 都不給：Manager 沒有理由讀 job 的登入態。
    assert entry.acls == ()
    assert plan_writers(scheme, CREDENTIAL) == frozenset({builder})


def plan_writers(scheme, asset_id: str) -> frozenset[str]:
    plan = generate_plan(scheme)
    return plan.all_writable_accounts(plan.by_id(asset_id))


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_credential_parent_directory_stays_root_owned(scheme) -> None:
    """裁決 (b) 的全部性質都落在這一層：目錄 root-owned ⇒ job 增／刪／換不了。"""
    scaffold = {
        path: (owner, mode)
        for path, owner, _group, mode in DEFAULT_LAYOUT.scaffold_directories(scheme)
    }
    builder = scheme.resolve(Principal.BUILDER)
    cred_dir = DEFAULT_LAYOUT.executor_credential_dir_of(DEFAULT_LAYOUT.builder_account)
    assert cred_dir in scaffold, (scheme.scheme_id, sorted(scaffold))
    owner, mode = scaffold[cred_dir]
    assert owner == scheme.deploy_account == "root"
    assert mode == 0o755
    # 「目錄沒有 w 位給 job」就是那條不變式本身。
    assert not mode & 0o020 and not mode & 0o002, format(mode, "04o")
    # 憑證檔確實落在那一層底下，而不是別處。
    cred = DEFAULT_LAYOUT.asset_paths()[CREDENTIAL]
    assert cred == f"{cred_dir}/auth.json"
    assert cred.startswith(DEFAULT_LAYOUT.home_of(DEFAULT_LAYOUT.builder_account) + "/")
    assert builder  # scheme 一定解析得出 builder 帳號


def test_credential_shares_its_directory_with_a_root_owned_tier0_file() -> None:
    """同目錄下就放著 root-owned 的 `codex-hooks`——那正是「換不掉」要守的東西。"""
    paths = DEFAULT_LAYOUT.asset_paths()
    cred_dir = DEFAULT_LAYOUT.executor_credential_dir_of(DEFAULT_LAYOUT.builder_account)
    assert _within(paths["codex-hooks"], cred_dir)
    assert _within(paths[CREDENTIAL], cred_dir)
    hooks = generate_plan(THREE_WAY_SCHEME).by_id("codex-hooks")
    assert hooks.owner == "root", "同目錄的 hooks 仍是 root 的"


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_credential_read_write_path_is_the_file_itself_not_its_parent(scheme) -> None:
    """一般規則把檔案折算成父目錄；這一族是**明示**的例外。

    折算成父目錄會讓 job unit 的 mount 層開放整個 `~/.codex`（裡面還有 root-owned 的
    `hooks.json`）；掛在檔案本身則讓「目錄 root-owned」在**檔案系統**與 **systemd
    mount** 兩層同時成立。#623 那條「兩個 root-owned 設定檔不可寫」的既有不變式因此
    仍然逐字成立。
    """
    assert CREDENTIAL in IN_PLACE_CONTENT_WRITE_ASSETS
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    cred = DEFAULT_LAYOUT.asset_paths()[CREDENTIAL]
    cred_dir = DEFAULT_LAYOUT.executor_credential_dir_of(DEFAULT_LAYOUT.builder_account)
    assert cred in unit.read_write_paths, unit.read_write_paths
    assert cred_dir not in unit.read_write_paths, unit.read_write_paths
    # 父目錄不得被任何一條 RWP 涵蓋（含更上層的 HOME）。
    assert not any(_within(cred_dir, rwp) for rwp in unit.read_write_paths), (
        unit.read_write_paths
    )


def test_credential_is_absent_from_the_manager_and_monitor_units() -> None:
    """job 的登入態不屬於 Manager 的可寫面（兩個 scheme 皆然）。"""
    cred = DEFAULT_LAYOUT.asset_paths()[CREDENTIAL]
    for scheme in ALL_SCHEMES:
        for unit in (
            build_manager_unit(scheme, DEFAULT_LAYOUT),
            build_monitor_unit(scheme, DEFAULT_LAYOUT),
        ):
            assert not any(_within(cred, rwp) for rwp in unit.read_write_paths), (
                scheme.scheme_id, unit.unit_name, unit.read_write_paths,
            )


def test_credential_relpath_is_a_validated_deployment_decision() -> None:
    """換 executor 只改一個值，且值的形狀在**建構當下**就驗（不等到 root 執行）。"""
    alt = PathLayout(executor_credential_relpath=".claude/credentials.json")
    account = alt.builder_account
    assert alt.executor_credential_of(account).endswith("/.claude/credentials.json")
    assert alt.executor_credential_dir_of(account).endswith("/.claude")
    # 換了 relpath，骨架的那條 root-owned 保護必須**跟著走**（不是寫死 `.codex`）。
    scaffold = {p: (o, m) for p, o, _g, m in alt.scaffold_directories(THREE_WAY_SCHEME)}
    assert scaffold[alt.executor_credential_dir_of("cortex-builder")] == ("root", 0o755)
    for bad in ("auth.json", "/etc/passwd", "../../etc/passwd", "..", ".codex/a b"):
        with pytest.raises(ValueError):
            PathLayout(executor_credential_relpath=bad)


def test_scaffold_gives_every_model_job_account_a_root_owned_credential_dir() -> None:
    """機制是 per-account 的：登記表只掛 builder 一份，保護面卻涵蓋全部 job 帳號。"""
    scaffold = {
        p: (o, m)
        for p, o, _g, m in DEFAULT_LAYOUT.scaffold_directories(THREE_WAY_SCHEME)
    }
    for account in ("cortex-builder", "cortex-reviewer-planner"):
        cred_dir = DEFAULT_LAYOUT.executor_credential_dir_of(account)
        assert scaffold[cred_dir] == ("root", 0o755), account


def test_scaffold_has_no_duplicate_paths() -> None:
    """預設 relpath 下憑證父目錄與 `~/.codex` 是同一層——去重後只出現一次。"""
    for scheme in ALL_SCHEMES:
        dirs = DEFAULT_LAYOUT.scaffold_directories(scheme)
        paths = [p for p, _o, _g, _m in dirs]
        assert len(paths) == len(set(paths)), sorted(paths)


# ---------------------------------------------------------------------------
# 3b. 「能改內容、不能增刪換」——真的 OS 語意（#638 的教訓）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.geteuid() == 0,
    reason=(
        "root 不受 DAC 限制：以 root 跑時目錄少了 w 位照樣建得了檔，"
        "本組驗的語意在 root 下不存在——刻意 skip 而非空過"
    ),
)
class TestInPlaceCredentialOsSemantics:
    """把裁決 (b) 的三條性質對**真的檔案系統**驗一次。

    #638 的教訓是「涉及 OS 層語意的不變式，測試要能真的驗到那個語意」。這裡不需要
    第二個 UID：裁決 (b) 守的規則是「**目錄沒有 `w` 位給這個行程**」——真實部署裡
    job 帳號落在 root-owned `0755` 的 `other` 位（`r-x`），本測試以 owner 位為
    `r-x`（`0555`）重現**同一條 kernel 檢查**（`inode_permission(dir, MAY_WRITE)`）。
    兩者走的是同一段判定，只是命中的是不同那一組權限位。

    真正需要第二個 UID 的只有「檔案 owner 不是本行程」那一半，而那一半由上面的
    產生器測試（`entry.owner == builder`）釘住，不重複用一個要 root 才跑得起來的
    測試去驗同一件事。
    """

    @pytest.fixture()
    def tree(self, tmp_path: Path):
        """`~/.codex` 的最小重現：目錄不可寫、憑證檔可寫、旁邊一個不可換的鄰居。"""
        cred_dir = tmp_path / ".codex"
        cred_dir.mkdir()
        cred = cred_dir / "auth.json"
        cred.write_text('{"token": "old"}\n', encoding="utf-8")
        cred.chmod(0o600)
        # 同目錄下的 root-owned 鄰居（真實部署裡是 `hooks.json`）。
        neighbour = cred_dir / "hooks.json"
        neighbour.write_text("{}\n", encoding="utf-8")
        # 目錄：對本行程 `r-x`——重現 job 帳號在 root-owned 0755 目錄上的有效權限。
        cred_dir.chmod(0o555)
        try:
            yield cred_dir, cred, neighbour
        finally:
            # 還原後 pytest 才收得掉這棵樹（不可寫的目錄 rmtree 會失敗）。
            cred_dir.chmod(0o755)

    def test_content_can_be_rewritten_in_place(self, tree) -> None:
        """refresh 走得通：憑證檔是自己的，`O_TRUNC` 覆寫不需要目錄的寫入權。"""
        _cred_dir, cred, _neighbour = tree
        cred.write_text('{"token": "refreshed"}\n', encoding="utf-8")
        assert "refreshed" in cred.read_text(encoding="utf-8")
        assert stat.S_IMODE(cred.stat().st_mode) == 0o600

    def test_new_files_cannot_be_created_in_the_directory(self, tree) -> None:
        """「建不了新檔」——這同時封掉「暫存檔 ＋ rename」那條原子替換路徑。"""
        cred_dir, _cred, _neighbour = tree
        with pytest.raises(PermissionError):
            (cred_dir / "auth.json.tmp").write_text("x", encoding="utf-8")

    def test_the_credential_cannot_be_unlinked(self, tree) -> None:
        """「刪不掉」——unlink 需要的是**目錄**的寫入權，不是檔案的。"""
        _cred_dir, cred, _neighbour = tree
        with pytest.raises(PermissionError):
            cred.unlink()
        assert cred.exists()

    def test_the_root_owned_neighbour_cannot_be_replaced(self, tree) -> None:
        """「換不掉同目錄下的其他 root-owned 檔」——rename 同樣要目錄的寫入權。"""
        _cred_dir, cred, neighbour = tree
        with pytest.raises(PermissionError):
            os.replace(cred, neighbour)
        assert neighbour.read_text(encoding="utf-8") == "{}\n"


# ---------------------------------------------------------------------------
# 4. traverse 鏈（#624 的 `unreachable_hops()`）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
@pytest.mark.parametrize("asset_id", NEW_ASSET_IDS)
def test_the_job_account_can_actually_reach_the_new_assets(scheme, asset_id) -> None:
    """葉節點權限對 **≠** 路徑走得通：整條鏈必須每一層都可 search。"""
    plan = generate_plan(scheme)
    builder = scheme.resolve(Principal.BUILDER)
    blocked = unreachable_hops(
        plan, DEFAULT_LAYOUT, scheme, account=builder, asset_id=asset_id
    )
    assert blocked == (), (scheme.scheme_id, asset_id, blocked)
    assert account_can_reach(
        plan, DEFAULT_LAYOUT, scheme, account=builder, asset_id=asset_id
    )


def test_reverse_control_a_closed_deployment_tree_breaks_the_toolchain_path() -> None:
    """反向對照：把部署樹收成 owner-only，toolchain 立刻走不到（不是永遠回 `()`）。"""
    scheme = THREE_WAY_SCHEME
    plan = generate_plan(scheme)
    builder = scheme.resolve(Principal.BUILDER)
    deploy_root = DEFAULT_LAYOUT.deploy_root

    class _ClosedDeployTree(PathLayout):
        def scaffold_directories(self, scheme_):  # type: ignore[override]
            return tuple(
                (path, owner, group, 0o700 if path == deploy_root else mode)
                for path, owner, group, mode in PathLayout.scaffold_directories(
                    self, scheme_
                )
            )

    closed = _ClosedDeployTree()
    blocked = unreachable_hops(
        plan, closed, scheme, account=builder, asset_id=TOOLCHAIN
    )
    assert deploy_root in blocked, blocked


# ---------------------------------------------------------------------------
# 5. #622 不變式：monitor 的可寫面仍嚴格窄於 Manager
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_stay_strictly_narrower_than_manager(scheme) -> None:
    manager = set(build_manager_unit(scheme, DEFAULT_LAYOUT).read_write_paths)
    monitor = set(build_monitor_unit(scheme, DEFAULT_LAYOUT).read_write_paths)
    assert monitor < manager, (scheme.scheme_id, sorted(monitor - manager))


# ---------------------------------------------------------------------------
# 6. executor 形態表 ＋ PATH 的取捨
# ---------------------------------------------------------------------------

def test_executor_table_covers_the_four_dispatched_executors() -> None:
    names = [t.name for t in EXECUTOR_TOOLS]
    assert names == ["codex", "claude", "copilot", "agy"]
    assert len(set(names)) == len(names)
    for tool in EXECUTOR_TOOLS:
        assert isinstance(tool.shape, ExecutorShape)
        assert tool.note.strip(), tool.name


def test_only_the_node_script_executor_depends_on_the_system_runtime() -> None:
    """「node 的版本風險只涵蓋 codex 一個」這句話的機器可讀形式。

    `claude`／`agy` 自帶原生執行檔、`copilot` 是 shell script——它們不會因為系統層
    node 換版本而行為改變。這條同時擋住「以後有人順手把某個 CLI 標成 needs_node」。
    """
    needs_node = {t.name for t in EXECUTOR_TOOLS if t.needs_node}
    assert needs_node == {"codex"}
    assert TOOLCHAIN_SYSTEM_RUNTIMES == ("node",)
    by_name = {t.name: t for t in EXECUTOR_TOOLS}
    assert by_name["codex"].shape is ExecutorShape.NODE_SCRIPT
    assert by_name["codex"].copy_tree, "node 套件單搬進入點會缺 node_modules"
    assert by_name["claude"].shape is ExecutorShape.NATIVE_ELF
    assert by_name["agy"].shape is ExecutorShape.NATIVE_ELF
    assert by_name["copilot"].shape is ExecutorShape.SHELL_SCRIPT


def test_job_path_puts_the_toolchain_first() -> None:
    """實機上系統層另有一份舊很多的同名 CLI——排在後面就會被它蓋掉。"""
    value = DEFAULT_LAYOUT.job_path_value()
    segments = value.split(":")
    assert segments[0] == DEFAULT_LAYOUT.toolchain_bin
    assert tuple(segments[1:]) == JOB_PATH_SYSTEM_TAIL
    # node／git／python3 的所在必須在 PATH 上；管理工具（sbin）一律不在。
    assert "/usr/bin" in segments
    assert not any(seg.endswith("/sbin") for seg in segments)


def test_job_unit_documents_where_path_actually_comes_from() -> None:
    """取捨寫在產物本身：模板 unit 的 `Environment=PATH=` 會被 shim 丟掉。

    shim 以 `execvpe(argv[0], argv, spec['env'])` 整份換掉環境，job 解析命令用的
    PATH 因此來自 **spec 的 env**（Manager 端 `PSC_BUILDER_PATH`），不是 unit。
    在 unit 裡寫一行 `Environment=PATH=` 只會是一個看起來承載作用、實際無效的設定。
    """
    unit = build_job_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert "\nEnvironment=PATH=" not in unit.content, "不得產生無效的 PATH 指令"
    assert f"PSC_BUILDER_PATH={DEFAULT_LAYOUT.job_path_value()}" in unit.content
    assert DEFAULT_LAYOUT.toolchain_root in unit.content
    assert "execvpe" in unit.content, "取捨的理由必須留在產物裡"


def test_builder_path_env_name_matches_the_job_runner_contract() -> None:
    """`PSC_BUILDER_PATH` 是 Manager 端**既有**的注入點，不是本票新造的。"""
    from paulsha_cortex.coordinator import job_runner

    assert job_runner.BUILDER_PATH_ENV == "PSC_BUILDER_PATH"
    assert f"{job_runner.BUILDER_PATH_ENV}=" in build_job_unit(
        THREE_WAY_SCHEME, DEFAULT_LAYOUT
    ).content


# ---------------------------------------------------------------------------
# 7. toolchain 落位計畫（產生器＝單一真相）＋ 純函式不變式
# ---------------------------------------------------------------------------

def test_toolchain_plan_is_deterministic_strings_only() -> None:
    a = build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    b = build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert a == b
    assert all(isinstance(line, str) for line in a)
    allowed = ("install -d ", "chown ", "chmod ")
    for line in a:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert stripped.startswith(allowed), stripped


def test_toolchain_plan_names_every_executor_and_the_version_pitfall() -> None:
    text = "\n".join(build_toolchain_plan(THREE_WAY_SCHEME, DEFAULT_LAYOUT))
    for tool in EXECUTOR_TOOLS:
        assert f"--- {tool.name}（" in text, tool.name
    # 來源判準：取 operator 實際在用的那一份，而不是另裝一份系統的。
    assert "command -v" in text
    assert "npm install -g" in text, "「不要另裝一份」這句必須寫在產物裡"
    assert DEFAULT_LAYOUT.job_path_value() in text


def test_toolchain_cli_verb_prints_the_plan(capsys) -> None:
    assert main(["toolchain", "three-way"]) == 0
    out = capsys.readouterr().out
    assert DEFAULT_LAYOUT.toolchain_root in out
    assert main(["toolchain", "--nope"]) == 2


def test_generators_still_touch_no_filesystem() -> None:
    """`permgen` 仍是純函式：沒有 subprocess、沒有 chown／chmod 的實作面。"""
    src = inspect.getsource(permgen)
    for forbidden in ("subprocess", "os.system", "os.chown", "os.chmod", "shutil."):
        assert forbidden not in src, forbidden
