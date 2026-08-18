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
# #698：builder 的 codex 憑證從 `IN_PLACE_FILE` 單檔（舊 id `builder-executor-credential`）
# 改成 root-owned ＋ sticky 的**整棵樹**，與 reviewer-planner 那一格共用同一列憑證表。
CREDENTIAL = "builder-codex-state"
HOOKS = "builder-codex-hooks"
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


def test_credential_writers_are_both_the_installer_and_the_job_persona() -> None:
    """#698：writer 面**兩個都要有**，而兩個各自撐住一半的形狀。

    - `INSTALLER`：這棵樹由 root 建立、root 擁有。少了它，`classify_owner()` 不會
      落到 `STICKY_SHARED`，owner 會變成 job 帳號——而目錄 owner 對 sticky 免疫
      （POSIX：目錄 owner 刪得掉裡面任何檔），`hooks.json` 當場守不住。
    - `BUILDER`：job 必須寫得進整棵樹，否則 codex 起不來（#686）。它同時是
      `required_write_targets()` 產出 RWP 的依據，以及 `build_entry()` 產出那條
      具名 `rwx` ACL 的依據（走 `UNTRUSTED_EXECUTION_PRINCIPALS`）。

    **#640 的原斷言是相反的**（「writer 是 job persona，**不是** INSTALLER，否則會
    落到 DEPLOYMENT、憑證就 refresh 不了」）。那條在 `IN_PLACE_FILE` 形狀下正確：
    當時登記的節點是**憑證檔自己**，它必須 job-owned。#698 之後登記的節點是**樹**，
    而 token 葉檔在樹裡、仍由 job 擁有（unit 的 `UMask=0077`）——refresh 照樣走得通，
    見 `test_the_enforcement_leaf_lives_inside_the_state_tree_and_belongs_to_root`。
    """
    asset = registry.asset_by_id(CREDENTIAL)
    assert set(asset.writers) == {Principal.INSTALLER, Principal.BUILDER}
    assert asset.tree is TrustTree.JOB_VISIBLE
    assert permgen.classify_owner(asset) is OwnerClass.STICKY_SHARED
    # enforcement 檔則相反：writer **只有** INSTALLER ⇒ 它落回 DEPLOYMENT、零 job 授權。
    hooks = registry.asset_by_id(HOOKS)
    assert hooks.writers == (Principal.INSTALLER,)
    assert permgen.classify_owner(hooks) is OwnerClass.DEPLOYMENT


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
def test_the_state_tree_is_root_owned_and_sticky(scheme) -> None:
    """#698：`$CODEX_HOME` 整棵——目錄 root-owned ＋ sticky，job 以具名 ACL 取得 rwx。

    **owner 必須是 root**：POSIX 的 sticky 規則對**目錄 owner** 免疫（目錄 owner 刪得掉
    裡面任何檔），把這一層交給 job 等於整條規則不存在。
    """
    entry = generate_plan(scheme).by_id(CREDENTIAL)
    builder = scheme.resolve(Principal.BUILDER)
    assert entry.owner == scheme.deploy_account == "root", scheme.scheme_id
    assert entry.is_directory, "#698 起它是整棵樹，不是單檔"
    assert entry.sticky and entry.mode_str == "1755", entry.mode_str
    assert [(a.account, a.perms, a.default) for a in entry.acls] == [(builder, "rwx", False)]
    assert plan_writers(scheme, CREDENTIAL) == frozenset({"root", builder})


def plan_writers(scheme, asset_id: str) -> frozenset[str]:
    plan = generate_plan(scheme)
    return plan.all_writable_accounts(plan.by_id(asset_id))


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_the_state_trees_parent_home_stays_root_owned(scheme) -> None:
    """整棵樹**換不掉**的那一層仍在骨架上：HOME 是 root-owned 0755。

    #698 把樹本身從骨架移進登記表（sticky ＋ ACL 表達不了在骨架那個四元組裡），
    但「job 換不掉整棵 `~/.codex`」這條性質沒有搬家——它一直是 HOME 那一層守的。
    """
    scaffold = {
        path: (owner, mode)
        for path, owner, _group, mode in DEFAULT_LAYOUT.scaffold_directories(scheme)
    }
    home = DEFAULT_LAYOUT.home_of(DEFAULT_LAYOUT.builder_account)
    assert scaffold[home] == ("root", 0o755)
    # 樹自己**不在**骨架（第二份真相會被 `_dedupe_scaffold()` 靜默取前者）。
    cred_dir = DEFAULT_LAYOUT.asset_paths()[CREDENTIAL]
    assert cred_dir not in scaffold, (scheme.scheme_id, cred_dir)
    assert cred_dir == f"{home}/.codex"


def test_the_enforcement_leaf_lives_inside_the_state_tree_and_belongs_to_root() -> None:
    """樹裡放著 root-owned 的 `hooks.json`——那正是 sticky 換到的東西。"""
    paths = DEFAULT_LAYOUT.asset_paths()
    tree = paths[CREDENTIAL]
    assert _within(paths[HOOKS], tree)
    assert paths[HOOKS] == f"{tree}/hooks.json"
    hooks = generate_plan(THREE_WAY_SCHEME).by_id(HOOKS)
    assert hooks.owner == "root"
    assert hooks.mode_str == "0644"
    assert hooks.acls == (), "零跨帳號授權——job 連讀寫 ACL 都不該有"
    # token 葉檔則相反：它必須是 job 的（token 過期要 refresh 得回來）。
    token = DEFAULT_LAYOUT.credential_token_path_of(DEFAULT_LAYOUT.builder_account)
    assert token == f"{tree}/auth.json"


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_the_state_tree_is_writable_but_the_hooks_file_is_mount_read_only(scheme) -> None:
    """#698：整棵進 RWP（codex 才起得來），樹裡的 hooks 再以 ReadOnlyPaths 收回。

    #640 的 `IN_PLACE_FILE` 讓 hooks 同時被 DAC 與 systemd mount 兩層擋住。本形狀若
    只做 DAC 就是**淨退一層**，因此 mount 那一層改由巢狀 `ReadOnlyPaths=` 提供。
    """
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    tree = DEFAULT_LAYOUT.asset_paths()[CREDENTIAL]
    hooks = DEFAULT_LAYOUT.asset_paths()[HOOKS]
    assert tree in unit.read_write_paths, unit.read_write_paths
    assert hooks in unit.read_only_paths, unit.read_only_paths
    # 巢狀關係是前提：ReadOnlyPaths 要覆蓋掉的就是外層那條 RWP。
    assert any(_within(hooks, rwp) for rwp in unit.read_write_paths)
    # HOME 那一層仍然不在 RWP 內（換不掉整棵樹）。
    home = DEFAULT_LAYOUT.home_of(DEFAULT_LAYOUT.builder_account)
    assert not any(_within(home, rwp) for rwp in unit.read_write_paths)


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
    """換 executor 只改一個值，且值的形狀在**建構當下**就驗（不等到 root 執行）。

    **#698 起那一個值被切成 head／tail 兩半**：head 是 sticky 樹（登記節點），
    tail 是 token 葉檔。兩半來自同一個字串 ⇒ 不可能各改一半。
    """
    alt = PathLayout(executor_credential_relpath=".claude/credentials.json")
    account = alt.builder_account
    assert alt.executor_credential_of(account).endswith("/.claude")
    assert alt.credential_token_path_of(account).endswith("/.claude/credentials.json")
    # hooks 跟著樹走——換了 relpath 之後它不能還留在 `.codex` 底下（那樣 codex 根本
    # 不會去讀它，是一個**不會報錯**的 enforcement 缺口）。
    hooks = {aid: path for aid, _a, path, _c in alt.enforcement_placements()}
    assert hooks[HOOKS].endswith("/.claude/hooks.json")
    assert alt.codex_hooks_dir_of(account).endswith("/.claude")
    for bad in ("auth.json", "/etc/passwd", "../../etc/passwd", "..", ".codex/a b"):
        with pytest.raises(ValueError):
            PathLayout(executor_credential_relpath=bad)


def test_scaffold_has_no_duplicate_paths() -> None:
    """骨架清單去重後每條路徑只出現一次（`_dedupe_scaffold` 只留第一筆）。"""
    for scheme in ALL_SCHEMES:
        dirs = DEFAULT_LAYOUT.scaffold_directories(scheme)
        paths = [p for p, _o, _g, _m in dirs]
        assert len(paths) == len(set(paths)), sorted(paths)


# ---------------------------------------------------------------------------
# 3b. 「整棵可寫、但動不了 root 的檔」——真的 OS 語意（#638 的教訓）
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "#698：sticky bit 的語意**需要第二個 UID**，CI 重現不了——因此具名 skip，"
        "不靜默通過。\n"
        "sticky 的 kernel 判定（`fs/namei.c: check_sticky()`）是：目錄帶 S_ISVTX 時，"
        "只有『**檔案 owner** == 行程 uid』或『**目錄 owner** == 行程 uid』或 "
        "CAP_FOWNER，才准 unlink／rename 那個檔。它比對的是 **uid**，因此無法像 "
        "#640 的 `IN_PLACE_FILE` 那樣用『把 owner 位調成 r-x』來重現：那一條守的是"
        "『目錄有沒有 w 位』（同一個 uid 就驗得到），這一條守的是『檔案屬不屬於我』"
        "（同一個 uid 永遠為真 ⇒ 測試必然全綠，是**假綠**）。\n"
        "本組因此改由 runbook 第 8 步的 R9 T3.9 在**真實部署、真實兩個 headless "
        "帳號、完整模板 unit 加固面**下實跑（#627 的身分鎖確保它只能以那兩個帳號執行）。"
        "0818 的實測結果逐字記在 runbook 的第 8 步表格：兩個 subject 都 "
        "`denied (OK) rc=2`（修前 reviewer-planner 是 `!! SUCCEEDED (FAIL)`）。\n"
        "另有一組 OS 層量測記在 runbook 第 4e-2b 步：以 `systemd-run` 起兩個剖面，"
        "分別驗『巢狀 ReadOnlyPaths 生效（Read-only file system）』與『DAC 三個動詞"
        "全關（Permission denied／Operation not permitted ×2）』。\n"
        "⛔ 不要把本組改成『用同一個 uid 建一棵 sticky 目錄』就當驗過了——那正是"
        "#638 那一族假綠的形態（單 UID 讓斷言真空）。"
    )
)
class TestStickyTreeOsSemantics:
    """佔位：sticky 的真實語意在 CI 環境（單一 uid）結構性驗不到。見上方 skip 理由。"""

    def test_root_owned_neighbour_cannot_be_unlinked_by_another_uid(self) -> None:
        raise AssertionError("需要第二個 UID——見 class 的 skip 理由")


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
    """「哪幾個 CLI 吃系統層 node 的版本風險」這句話的機器可讀形式。

    `claude`／`agy` 自帶原生執行檔，不會因為系統層 node 換版本而行為改變。
    `codex`／`copilot` 會。

    **#643 把 `copilot` 從 False 改為 True**：#640 落表時只知道它是 shell script、
    還沒查它內部 exec 什麼（表上的 note 當時就寫著「安裝時務必 `head -n 20` 查一次」）。
    #643 在真實加固面下量到 `copilot --version` 在 `MemoryDenyWriteExecute=yes` 下
    空輸出、拿掉即正常，與 `codex` 的症狀逐字相同——它內部 exec 的就是 node。
    這條同時是 #643 加固剖面的分類基準，因此改動它會連帶改動兩份 job unit 的對應
    關係（見 `test_trust_root_hardening_profile_643.py`）。

    **#661 放寬了本測試的後半段**：`TOOLCHAIN_SYSTEM_RUNTIMES` 原本斷言逐字等於
    `("node",)`，而那不是一條性質，是「當時只盤點到 node」的快照——`srt` 實際還要
    `bwrap`／`socat`，Manager 還要 `git`／`gh`。現在它由 `SYSTEM_PROGRAMS` 導出，
    本測試只保留真正的性質：**`node` 必須在系統層**（＝不進部署樹），因為那才是
    裁決 (a) 對「通用 runtime」的處置。完整盤點的斷言在
    `test_trust_root_external_deps_661.py`。
    """
    needs_node = {t.name for t in EXECUTOR_TOOLS if t.needs_node}
    assert needs_node == {"codex", "copilot"}
    assert "node" in TOOLCHAIN_SYSTEM_RUNTIMES
    assert "node" not in {tool.name for tool in EXECUTOR_TOOLS}
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


def test_job_unit_carries_path_as_the_second_layer() -> None:
    """#679：模板 unit **必須**有 `Environment=PATH=`，且值由產生器導出。

    #640 當時的判斷是「shim 以 `execvpe(argv[0], argv, spec['env'])` 整份換掉環境，
    寫在 unit 上會被丟掉」——對了一半：spec 的 env 確實是 job 的完整環境，但產生它的
    `build_job_env()` 當時 fail-open，於是「spec 沒給 PATH」與「unit 沒有 PATH」同時
    成立，兩層皆空。#679 兩層都補：spec 那層 fail-closed，unit 這層是 shim 的退路
    （root-owned、比 spec 更可信），並讓加固面複本自動帶上 production 的 PATH。
    """
    unit = build_job_unit(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    assert f"\nEnvironment=PATH={DEFAULT_LAYOUT.job_path_value()}\n" in unit.content
    assert f"PSC_BUILDER_PATH={DEFAULT_LAYOUT.job_path_value()}" in unit.content
    assert DEFAULT_LAYOUT.toolchain_root in unit.content
    assert "execvpe" in unit.content, "取捨的理由必須留在產物裡"


def test_job_unit_path_is_derived_not_hardcoded() -> None:
    """值必須跟著 `PathLayout` 走——改部署根就跟著改，不是寫死的字面量。"""
    from paulsha_cortex.trust_root.permgen import PathLayout

    layout = PathLayout(instance="acme", deploy_root="/srv/acme")
    unit = build_job_unit(THREE_WAY_SCHEME, layout)
    assert f"\nEnvironment=PATH={layout.job_path_value()}\n" in unit.content
    emitted = unit.content.split("\nEnvironment=PATH=", 1)[1].split("\n", 1)[0]
    assert emitted.startswith("/srv/acme/toolchain/bin:"), emitted
    assert DEFAULT_LAYOUT.toolchain_bin not in emitted


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
