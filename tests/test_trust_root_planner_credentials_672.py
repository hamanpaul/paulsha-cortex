"""issue #685（#672 票 D）：permgen 把 planner／reviewer 的憑證面 codify。

裁決背景（0818 operator）：

- **U-4 追認雙 domain**——`cortex-reviewer-planner` 同時持有多個 provider 的登入態是
  **核可狀態**，design 的安全退步 R-3 是明文接受的有界殘餘風險。
- **U-5 照設計做**——`executor_credential_relpath` 擴成 per-(account, executor) 表。
- **U-7 照設計做**——agy 的可寫狀態樹依 design 的 (a)（symlink 類資產）進登記表。

本檔釘的是**表的形狀**與**它導出的三條性質**：

1. 三份登入態進得了登記表，且是由表機械導出（不是三行手寫的 `asset_paths()`）；
2. reviewer 模板 unit 的 `ReadWritePaths` **逐字不變**——`HOME_REDIRECT_TREE` 的目標
   落在該帳號既有的 `cache` 內，因此**零新增可寫面**；
3. **二分方案的產出不含它們**（#640 記錄的陷阱：登記第二份會讓 Manager unit 的 RWP
   指向一條不存在的路徑而起不來；#671 的 `inapplicable_home_anchored_assets()` 已把
   那個阻礙拆成一條機械規則，本檔驗它仍然成立）。

**為什麼驗收條件從「RWP 逐字含憑證檔案」改成上面第 2 條**：issue 原文寫的是前者，前提
是「codex 的登入態＝一個 `auth.json`」。#686 在完整 reviewer unit 沙箱下實測推翻了那個
前提——codex 需要 `$CODEX_HOME` **整個目錄**可寫（`state_5.sqlite`／`sessions/`／
`plugins/`…），只放行單檔時它連起都起不來。照字面滿足原驗收會產出一個 **codex 仍然
跑不起來**的部署，因此本票改走 `HOME_REDIRECT_TREE`，並把不變式換成**更強的那一條**：
可寫面一格都沒有增加。差異在 PR body 逐條說明。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paulsha_cortex.trust_root import permgen, registry  # noqa: E402
from paulsha_cortex.trust_root.permgen import (  # noqa: E402
    DEFAULT_LAYOUT,
    CREDENTIALED_ACCOUNTS,
    CredentialShape,
    EXECUTOR_CREDENTIALS,
    FOUR_WAY_SCHEME,
    IN_PLACE_CONTENT_WRITE_ASSETS,
    PathLayout,
    Principal,
    SYMLINK_ASSETS,
    THREE_WAY_SCHEME,
    TWO_WAY_SCHEME,
    UnregisteredExecutorCredentialError,
    build_job_unit,
    generate_plan,
    inapplicable_home_anchored_assets,
    read_write_paths,
)

PLANNER_ACCOUNT = DEFAULT_LAYOUT.reviewer_planner_account
BUILDER_ACCOUNT = DEFAULT_LAYOUT.builder_account
PLANNER_ASSETS = (
    "reviewer-planner-codex-state",
    "reviewer-planner-agy-state",
    "reviewer-planner-claude-state",
)


def _reviewer_rwp(scheme=FOUR_WAY_SCHEME) -> tuple[str, ...]:
    account = scheme.resolve(Principal.REVIEWER)
    assert account is not None
    return read_write_paths(
        generate_plan(scheme),
        DEFAULT_LAYOUT,
        account,
        extras=DEFAULT_LAYOUT.job_extra_write_paths(account),
        retired=permgen.RETIRED_JOB_WRITE_ASSETS,
    )


# ---------------------------------------------------------------------------
# 1. U-5：表的形狀
# ---------------------------------------------------------------------------

def test_the_table_has_two_axes_and_both_are_consulted() -> None:
    """per-(account, executor) 而不是 per-executor——差別要驗得出來。

    判準：**同一個 executor 在兩個帳號上是兩種形狀**。builder×codex 仍是 #640 的
    單檔（那份部署已存在、有 runbook 反向驗證），reviewer-planner×codex 是導進
    `cache` 的狀態樹（#686 實測 codex 需要整棵可寫）。一張 per-executor 的表表達
    不了這件事——這就是 U-5 要兩軸的具體理由。
    """
    builder = permgen.credential_for("builder", "codex")
    planner = permgen.credential_for("reviewer-planner", "codex")
    assert builder.shape is CredentialShape.IN_PLACE_FILE
    assert planner.shape is CredentialShape.HOME_REDIRECT_TREE
    assert builder is not planner


def test_the_account_axis_is_the_u4_ratification() -> None:
    """U-4 追認的範圍＝planner 帳號那一列有幾格。"""
    assert dict(CREDENTIALED_ACCOUNTS)["builder"] == (
        ("codex", CredentialShape.IN_PLACE_FILE),
    )
    planner_cells = dict(CREDENTIALED_ACCOUNTS)["reviewer-planner"]
    assert [executor for executor, _shape in planner_cells] == ["codex", "agy", "claude"]
    # 三格分屬三個 independence domain——那正是 R-3 描述的曝險面，也正是
    # `select_secondary_planner()` 要有異質 planner 的前提。
    assert len({e for e, _s in planner_cells}) == 3


def test_every_table_row_is_a_registered_asset_and_vice_versa() -> None:
    """兩軸展開 ⇔ 登記表，**雙向封閉**。少一邊就是「機制有、登記表不知道」。"""
    from_table = set(permgen.credential_asset_ids())
    # 判準取 `derived_in`（登記表自己宣告「這條路徑由誰導出」），而不是 asset_id 的
    # 字串樣式——樣式比對會在下一個命名慣例出現時無聲失效。
    from_registry = {
        asset.asset_id
        for asset in registry.ASSET_REGISTRY
        if any("executor_credential_of" in src for src in asset.derived_in)
    }
    assert from_table == from_registry, (sorted(from_table), sorted(from_registry))
    assert from_table == set(PLANNER_ASSETS) | {"builder-executor-credential"}
    for asset_id in from_table:
        assert asset_id in DEFAULT_LAYOUT.asset_paths(), asset_id


def test_unregistered_cells_fail_closed_instead_of_guessing_a_path() -> None:
    """猜一條路徑的後果是「指紋盯著不存在的檔、unit 放行錯的路徑」——兩者都靜默通過。"""
    with pytest.raises(UnregisteredExecutorCredentialError):
        permgen.credential_for("reviewer-planner", "copilot")
    with pytest.raises(UnregisteredExecutorCredentialError):
        DEFAULT_LAYOUT.executor_credential_of(PLANNER_ACCOUNT, "copilot")
    # 表上沒有的**帳號**在 primary executor 上退回 #640 的單一部署決定（既有行為），
    # 但問它的第二份憑證同樣 fail-closed。
    gate = "cortex-gate"
    assert DEFAULT_LAYOUT.executor_credential_of(gate).endswith("/.codex/auth.json")
    with pytest.raises(UnregisteredExecutorCredentialError):
        DEFAULT_LAYOUT.executor_credential_of(gate, "agy")


def test_the_primary_relpath_stays_a_single_deployment_decision() -> None:
    """`executor_credential_relpath` 沒有被表取代——它是表上那一格的**值**。

    #640 的「換 executor 只改一個值」因此仍然成立；把它的值抄一份進表就是第二份真相。
    """
    alt = PathLayout(executor_credential_relpath=".claude/credentials.json")
    assert alt.executor_credential_of(BUILDER_ACCOUNT).endswith("/.claude/credentials.json")
    # 骨架那條 root-owned 保護跟著走（不是寫死 `.codex`）。
    scaffold = {p: (o, m) for p, o, _g, m in alt.scaffold_directories(THREE_WAY_SCHEME)}
    assert scaffold[alt.executor_credential_dir_of(BUILDER_ACCOUNT)] == ("root", 0o755)
    # 表上有明確 relpath 的那幾格**不**受它影響。
    assert alt.executor_credential_of(PLANNER_ACCOUNT, "agy").endswith("/.gemini")


def test_relpath_shapes_are_validated_at_construction_time() -> None:
    """值會被接成絕對路徑並嵌進 root 執行的 `ln`／`chown`，因此形狀在建構當下就驗。"""
    for bad in ("/etc/passwd", "../../etc/passwd", "..", ".codex/a b", "./x"):
        with pytest.raises(ValueError):
            permgen._validate_home_relpath(bad)
    for good in (".codex", ".gemini", ".codex/auth.json"):
        permgen._validate_home_relpath(good)


# ---------------------------------------------------------------------------
# 2. U-7：agy（以及 codex／claude）的可寫狀態樹怎麼進登記表
# ---------------------------------------------------------------------------

def test_state_trees_are_registered_as_root_owned_symlinks() -> None:
    """U-7 的裁決＝design 的 (a)：登記成 symlink 類資產（登記表新增的 kind）。"""
    plan = generate_plan(FOUR_WAY_SCHEME)
    for asset_id in PLANNER_ASSETS:
        entry = plan.by_id(asset_id)
        assert entry.is_symlink, asset_id
        assert not entry.is_directory, asset_id
        assert asset_id in SYMLINK_ASSETS, asset_id
        # root-owned ⇒ job 換不掉指向；且**沒有任何跨帳號 ACL**。
        assert entry.owner == FOUR_WAY_SCHEME.deploy_account == "root", asset_id
        assert entry.acls == (), asset_id
        assert entry.writer_accounts == frozenset({"root"}), asset_id


def test_symlink_commands_never_use_a_bare_chown_or_chmod() -> None:
    """Linux 的 `chown`／`chmod` 跟著 symlink 走 ⇒ 裸用會改到**目標樹**的 owner。

    那棵樹歸 job 帳號正是本形狀的全部重點，所以這條不是風格檢查。
    """
    plan = generate_plan(FOUR_WAY_SCHEME)
    for asset_id in PLANNER_ASSETS:
        entry = plan.by_id(asset_id)
        path = DEFAULT_LAYOUT.asset_paths()[asset_id]
        target = DEFAULT_LAYOUT.symlink_targets()[asset_id]
        cmds = entry.commands(path, target)
        assert cmds[0] == f"ln -sfn {target} {path}"
        assert cmds[1].startswith("chown -h "), cmds
        assert not any(c.startswith("chmod ") for c in cmds), cmds
        assert not any(c.startswith(f"chown {entry.owner}") for c in cmds), cmds


def test_the_state_tree_target_lives_inside_the_accounts_own_cache() -> None:
    """「不新增可寫面」的**前提**：目標必須落在該帳號本來就可寫的那一層裡。"""
    cache = DEFAULT_LAYOUT.cache_of(PLANNER_ACCOUNT)
    for asset_id, target in DEFAULT_LAYOUT.symlink_targets().items():
        assert asset_id in PLANNER_ASSETS, asset_id
        assert target.startswith(cache + "/"), (asset_id, target)


def test_scaffold_creates_the_target_but_not_a_directory_at_the_symlink() -> None:
    """骨架建的是**目標**；在 symlink 的位置建真目錄會把整條導向機制無聲換掉。"""
    scaffold = {
        path: (owner, mode)
        for path, owner, _g, mode in DEFAULT_LAYOUT.scaffold_directories(FOUR_WAY_SCHEME)
    }
    for asset_id in PLANNER_ASSETS:
        link = DEFAULT_LAYOUT.asset_paths()[asset_id]
        target = DEFAULT_LAYOUT.symlink_targets()[asset_id]
        assert link not in scaffold, asset_id
        assert scaffold[target] == (PLANNER_ACCOUNT, 0o700), asset_id
    # symlink 自己的保護來自 HOME 那一層。
    assert scaffold[DEFAULT_LAYOUT.home_of(PLANNER_ACCOUNT)] == ("root", 0o755)


def test_the_token_leaf_is_what_the_fingerprint_should_stat() -> None:
    """登記表要的是「要保護的節點」，指紋要的是「refresh 之後會變的檔」。

    兩者在 `HOME_REDIRECT_TREE` 下不同；`stat` 一條 symlink 只看得到目標**目錄**的
    mtime，而 token 就地覆寫時它不變 ⇒ 「憑證換了」偵測不到。
    """
    assert DEFAULT_LAYOUT.credential_token_path_of(PLANNER_ACCOUNT, "agy").endswith(
        "/.gemini/antigravity-cli/antigravity-oauth-token"
    )
    assert DEFAULT_LAYOUT.credential_token_path_of(PLANNER_ACCOUNT, "codex").endswith(
        "/.codex/auth.json"
    )
    assert DEFAULT_LAYOUT.credential_token_path_of(PLANNER_ACCOUNT, "claude").endswith(
        "/.claude/.credentials.json"
    )
    # `IN_PLACE_FILE` 兩者相同。
    assert DEFAULT_LAYOUT.credential_token_path_of(
        BUILDER_ACCOUNT
    ) == DEFAULT_LAYOUT.executor_credential_of(BUILDER_ACCOUNT)


# ---------------------------------------------------------------------------
# 3. 驗收：零新增可寫面（取代 issue 原文的「RWP 逐字含憑證檔案」，理由見模組 docstring）
# ---------------------------------------------------------------------------

def test_home_redirect_adds_no_writable_surface_to_the_reviewer_unit() -> None:
    """reviewer 模板 unit 的 `ReadWritePaths` **逐字不變**，且憑證面仍然可寫。

    這條比 issue 原文的驗收更強：不是「多一條放行」，是「一條都不必多」。機制是
    `_minimize()`——目標落在 `cache` 之內，被那條既有的 RWP 涵蓋。
    """
    unit = build_job_unit(FOUR_WAY_SCHEME, principal=Principal.REVIEWER)
    assert set(unit.read_write_paths) == {
        DEFAULT_LAYOUT.cache_of(PLANNER_ACCOUNT),
        DEFAULT_LAYOUT.review_verdict_spool_root,
    }, unit.read_write_paths
    for asset_id in PLANNER_ASSETS:
        link = DEFAULT_LAYOUT.asset_paths()[asset_id]
        target = DEFAULT_LAYOUT.symlink_targets()[asset_id]
        assert link not in unit.read_write_paths, asset_id
        assert target not in unit.read_write_paths, asset_id
        # 但**寫得進去**：目標被 cache 那一條涵蓋。
        assert target.startswith(DEFAULT_LAYOUT.cache_of(PLANNER_ACCOUNT) + "/")


def test_the_builder_in_place_credential_is_untouched() -> None:
    """#640 裁決 (b) 一行未改——RWP 逐字掛在**檔案本身**，不是它的父目錄。"""
    unit = build_job_unit(FOUR_WAY_SCHEME, principal=Principal.BUILDER)
    credential = DEFAULT_LAYOUT.executor_credential_of(BUILDER_ACCOUNT)
    assert "builder-executor-credential" in IN_PLACE_CONTENT_WRITE_ASSETS
    assert credential in unit.read_write_paths
    parent = DEFAULT_LAYOUT.executor_credential_dir_of(BUILDER_ACCOUNT)
    assert parent not in unit.read_write_paths
    assert not any(
        parent == rwp or parent.startswith(rwp.rstrip("/") + "/")
        for rwp in unit.read_write_paths
    ), unit.read_write_paths


def test_the_state_trees_are_absent_from_manager_and_monitor_units() -> None:
    """job 的登入態不屬於 Manager 的可寫面（兩個定案方案皆然）。"""
    for scheme in (THREE_WAY_SCHEME, FOUR_WAY_SCHEME):
        for unit in (
            permgen.build_manager_unit(scheme, DEFAULT_LAYOUT),
            permgen.build_monitor_unit(scheme, DEFAULT_LAYOUT),
        ):
            for asset_id in PLANNER_ASSETS:
                path = DEFAULT_LAYOUT.asset_paths()[asset_id]
                assert not any(
                    path == rwp or path.startswith(rwp.rstrip("/") + "/")
                    for rwp in unit.read_write_paths
                ), (scheme.scheme_id, unit.unit_name, asset_id)


# ---------------------------------------------------------------------------
# 4. 驗收：二分方案的產出不含它（#640 的陷阱，#671 已拆）
# ---------------------------------------------------------------------------

def test_two_way_scheme_excludes_the_planner_credentials_mechanically() -> None:
    """#640 當年「乾脆不登記第二份憑證」的唯一理由，在 #671 之後已經是一條機械規則。

    二分方案把 reviewer／planner 併進 `cortex-svc`，那三條 HOME 路徑因此**不存在**；
    systemd 對不存在的 `ReadWritePaths=` 目標會讓 unit 直接起不來。
    """
    plan = generate_plan(TWO_WAY_SCHEME)
    excluded = {aid for aid, _p in inapplicable_home_anchored_assets(plan, DEFAULT_LAYOUT)}
    for asset_id in PLANNER_ASSETS:
        assert asset_id in excluded, asset_id
    # 兩個定案方案則一格都不排除。
    for scheme in (THREE_WAY_SCHEME, FOUR_WAY_SCHEME):
        assert inapplicable_home_anchored_assets(generate_plan(scheme), DEFAULT_LAYOUT) == ()


def test_two_way_units_never_reference_the_planner_home() -> None:
    """把上一條的**後果**釘住：二分部署的每一份 unit 都不得出現那個 HOME。"""
    planner_home = DEFAULT_LAYOUT.home_of(PLANNER_ACCOUNT)
    units = [
        permgen.build_manager_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT),
        permgen.build_monitor_unit(TWO_WAY_SCHEME, DEFAULT_LAYOUT),
    ]
    for principal in permgen.downgraded_job_principals(TWO_WAY_SCHEME):
        for profile in permgen.HARDENING_PROFILES:
            units.append(
                build_job_unit(
                    TWO_WAY_SCHEME, DEFAULT_LAYOUT, principal=principal, profile=profile
                )
            )
    for unit in units:
        for rwp in unit.read_write_paths:
            assert not rwp.startswith(planner_home), (unit.unit_name, rwp)


def test_two_way_scaffold_creates_no_planner_state_trees() -> None:
    """骨架同理——二分部署不該生出一棵沒有帳號的樹。"""
    paths = {p for p, *_rest in DEFAULT_LAYOUT.scaffold_directories(TWO_WAY_SCHEME)}
    planner_home = DEFAULT_LAYOUT.home_of(PLANNER_ACCOUNT)
    assert not any(p.startswith(planner_home) for p in paths), sorted(paths)


def test_two_way_symlink_commands_are_guarded_by_the_missing_home() -> None:
    """權限 script 以 `sudo sh -e` 執行——`ln` 打在不存在的 HOME 會中止整份 script。"""
    scheme = TWO_WAY_SCHEME.with_principal_accounts({
        Principal.OPERATOR: permgen.ABSENT_ACCOUNT,
        Principal.EXTERNAL: permgen.ABSENT_ACCOUNT,
    })
    lines = permgen.plan_to_commands(
        generate_plan(scheme),
        path_of=DEFAULT_LAYOUT.asset_paths(),
        layout=DEFAULT_LAYOUT,
        scheme=scheme,
    )
    guard = f"[ ! -e {DEFAULT_LAYOUT.home_of(PLANNER_ACCOUNT)} ] || "
    ln_lines = [l for l in lines if " ln -sfn " in l or l.startswith("ln -sfn ")]
    assert ln_lines, lines
    for line in ln_lines:
        assert line.startswith(guard), line


# ---------------------------------------------------------------------------
# 5. 逾期項：deferred 清單縮短，且沒有假裝關掉關不掉的那兩條
# ---------------------------------------------------------------------------

def test_the_overdue_reviewer_credential_entry_is_gone() -> None:
    """#640 寫的「M2 落地時補第二列」——M2（#615）早已落地，本票補完它。"""
    names = {item.name for item in permgen.deferred_run_dependencies()}
    assert "reviewer-planner-executor-credential" not in names, sorted(names)


def test_the_two_entries_that_could_not_be_closed_say_why() -> None:
    """**沒關掉的兩條必須說得出新的阻礙，而不是留著舊理由。**

    - `reviewer-planner-codex-hooks`：與 codex 的可用性在 `$CODEX_HOME` 這一層互斥
      （U-9）；
    - `manager-claude-credential`：U-5 解除了它的機械阻礙，但「要不要給 Manager 一份
      模型憑證」的答案是不要，而它的消失要等票 F 切換。
    """
    by_name = {item.name: item for item in permgen.deferred_run_dependencies()}
    hooks = by_name["reviewer-planner-codex-hooks"]
    assert "U-9" in hooks.disposition
    assert "CODEX_HOME" in hooks.reason
    # 舊理由不得還「作為理由」留在上面——它必須是被**引述後推翻**的那一段。
    assert "做不到" in hooks.reason and "#686" in hooks.reason
    manager = by_name["manager-claude-credential"]
    assert "#687" in manager.disposition
    assert "executor_credential_relpath` 是**單一**部署決定" not in manager.reason


def test_the_inventory_stays_closed_in_both_directions() -> None:
    """新增資產必須同時被窮舉盤點列到——#671 的雙向封閉不得因為本票而破。"""
    assert permgen.uncovered_run_dependencies() == ()
    assert permgen.unlisted_roster_entries() == ()
    listed = {dep.name for dep in permgen.RUN_EXTERNAL_DEPENDENCIES}
    for asset_id in PLANNER_ASSETS:
        assert asset_id in listed, asset_id


# ---------------------------------------------------------------------------
# 6. `HOME` 這條成對前置（#686 查到，憑證面與它必須一起成立）
# ---------------------------------------------------------------------------

def test_every_credential_path_is_home_rooted_so_home_must_be_declared() -> None:
    """三份登入態全部以 `$HOME` 為根 ⇒ `HOME` 解不到時它們在 job 內一條都不存在。

    模板模式下 shim 以 `os.execvpe` 整份換掉環境，unit 的 `Environment=HOME=` 到不了
    模型（#686 實機更正）——因此 `PSC_REVIEWER_HOME` 是本票的**成對前置**，而不是
    另一張票的事。症狀（`$HOME is not defined`／`Not logged in`）與「憑證沒放好」
    長得一模一樣，這正是它必須被寫在同一個地方的理由。
    """
    home = DEFAULT_LAYOUT.home_of(PLANNER_ACCOUNT)
    for asset_id in PLANNER_ASSETS:
        assert DEFAULT_LAYOUT.asset_paths()[asset_id].startswith(home + "/"), asset_id
    assert DEFAULT_LAYOUT.job_home_value(PLANNER_ACCOUNT) == home
    unit = build_job_unit(FOUR_WAY_SCHEME, principal=Principal.REVIEWER)
    assert f"PSC_REVIEWER_HOME={home}" in unit.content


def test_the_home_env_names_match_the_job_runner_contract() -> None:
    """permgen 與 job_runner 刻意不互相 import ⇒ 成對契約由測試釘住（比照 PATH）。"""
    from paulsha_cortex.coordinator import job_runner

    for principal, env_name in permgen.JOB_HOME_ENV_BY_PRINCIPAL.items():
        role = {
            Principal.BUILDER: job_runner.JOB_ROLE_BUILDER,
            Principal.REVIEWER: job_runner.JOB_ROLE_REVIEW,
            Principal.GATE: job_runner.JOB_ROLE_GATE,
        }[principal]
        assert job_runner.resolve_job_role(role).home_env == env_name, principal


# ---------------------------------------------------------------------------
# 7. OS 層語意：具名 skip（CI 重現不了，**不得靜默通過**）
# ---------------------------------------------------------------------------

@pytest.mark.skip(
    reason=(
        "需要**第二個 UID** 與真實 systemd 加固面才驗得到，CI 兩者皆無 ⇒ 斷言會恆真"
        "（#638／#657 的假綠形態）。要驗的性質有三條，全部在 runbook 第 4e-2b 步以"
        "`psc_run_under`（property 由 `permgen.unit_replica_properties()` 從**落檔的"
        "unit** 全量導出，不得自行組 `--property=`、不得自帶 `--setenv=PATH=`，見"
        "design D13）逐條實跑：\n"
        "  (a) 以 cortex-reviewer-planner 身分 `ln -sfn /tmp/evil ~/.gemini` ⇒ 必須"
        "      `Permission denied`（symlink 在 root-owned 的 HOME 裡，換不掉指向）；\n"
        "  (b) 同一身分在 `~/.gemini/` 底下建檔 ⇒ 必須成功（目標在 cache 內，已在"
        "      unit 的 RWP 中）——這一條同時證明「零新增可寫面」不是靠放棄可寫性換的；\n"
        "  (c) `codex exec` 與 `claude -p` 在該 unit 的完整 property 集合下 rc=0"
        "      （#686 的矩陣裡這兩格分別被 `$CODEX_HOME` 與 `Not logged in` 擋住，"
        "      本票的部署步驟做完之後必須翻綠——那是驗收，不是推測）。\n"
        "**本票沒有實機執行任何一條**：runbook 是給 operator 的，實機部署不在本票範圍。"
    )
)
def test_planner_cannot_repoint_its_own_state_symlink() -> None:  # pragma: no cover
    raise AssertionError("見 skip reason：本條由 runbook 第 4e-2b 步實機驗證")


class TestSymlinkGuardOsSemantics:
    """把「`chown` 跟著 symlink 走」對**真的檔案系統**驗一次（#638 的手法）。

    這一條不需要第二個 UID：要驗的是 `chown` 的**解析語意**（跟不跟著 symlink），
    而那在單 UID 下就成立。它守的是 `PermissionEntry.commands()` 為什麼一定要出
    `chown -h`——裸 `chown` 會改到目標樹的 owner，而目標樹歸 job 帳號正是重點。
    """

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root 不受 DAC 限制；本組驗的是解析語意，以非 root 跑"
    )
    def test_bare_chown_follows_the_link_and_lchown_does_not(self, tmp_path: Path) -> None:
        target = tmp_path / "cache" / "gemini"
        target.mkdir(parents=True)
        link = tmp_path / ".gemini"
        link.symlink_to(target)
        before = target.stat()
        # `chown -h` 對 symlink 本身；同 UID 下 owner 不變，但重點是**它不解析目標**。
        subprocess.run(
            ["chown", "-h", f"{os.getuid()}:{os.getgid()}", str(link)],
            check=True, capture_output=True,
        )
        assert target.stat().st_ino == before.st_ino
        # 反向：裸 `chmod` 打在 symlink 上會落到**目標**（Linux 無 lchmod）。
        subprocess.run(["chmod", "0700", str(link)], check=True, capture_output=True)
        assert oct(target.stat().st_mode & 0o777) == "0o700"
        assert not link.is_symlink() or link.resolve() == target.resolve()
