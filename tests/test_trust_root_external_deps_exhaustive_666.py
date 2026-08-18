"""#666：pytest 與 gh 憑證進登記表，＋ 一次**窮舉盤點**。

本檔釘住三件事，第三件才是重點：

1. **`pytest`**（＋被測樹的 `PyYAML`）落在**系統層 python 發行版**這張新表上，版本
   是明示的部署決定（約束的唯一真相在 `pyproject.toml`，測試對著它比）；
2. **`manager-gh-credential`／`manager-gh-config`** 兩個資產，`hosts.yml` 與
   `config.yml` **owner 刻意不同**，而且「改得了內容、建不了新檔」以真的 OS 語意驗；
3. **窮舉盤點**（`RUN_EXTERNAL_DEPENDENCIES`）**兩個方向都封閉**——盤點列到的每一項
   都真的在某張表上，每張表上的每一項也都被盤點列到。這一條是本票真正要買的東西：
   #640→#661→#666 每一次都是「症狀出現才補一項」，而兩個方向都釘住之後，
   「加一支相依」與「說明它在 run 的哪一段被誰碰到」變成同一件事。
"""

from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest

from paulsha_cortex import doctor
from paulsha_cortex.coordinator import gate_ledger
from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    DEPLOYMENT_PYTHON_DISTRIBUTIONS,
    DEPLOYMENT_VENV_INTERPRETER,
    EXECUTOR_TOOLS,
    FOUR_WAY_SCHEME,
    GATE_COMMAND_DECLARATIONS,
    GATE_COMMAND_ENV_PREFIX,
    IN_PLACE_CONTENT_WRITE_ASSETS,
    JOB_PATH_SYSTEM_TAIL,
    RUN_EXTERNAL_DEPENDENCIES,
    SERVICE_TOOLS,
    SYSTEM_INTERPRETER,
    SYSTEM_PROGRAMS,
    SYSTEM_PYTHON_DISTRIBUTIONS,
    THREE_WAY_SCHEME,
    TWO_WAY_SCHEME,
    DependencyKind,
    Principal,
    RunStage,
    account_can_reach,
    build_toolchain_plan,
    deferred_run_dependencies,
    generate_plan,
    home_anchored_asset_ids,
    inapplicable_home_anchored_assets,
    read_write_paths,
    unlisted_roster_entries,
    unreachable_hops,
    uncovered_run_dependencies,
)
from paulsha_cortex.trust_root.registry import ASSET_REGISTRY, check_registry_equation

ALL_SCHEMES = (THREE_WAY_SCHEME, FOUR_WAY_SCHEME)

GH_CREDENTIAL = "manager-gh-credential"
GH_CONFIG = "manager-gh-config"
NEW_ASSET_IDS = (GH_CREDENTIAL, GH_CONFIG)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. 兩個新資產進登記表，等式仍綠
# ---------------------------------------------------------------------------

def test_registry_equation_stays_green() -> None:
    """新增資產不得讓 path 契約 ⟷ 登記表的雙向等式失衡。

    兩個新資產都是 `path_resolver=None`（路徑由 `PathLayout` 導出），因此等式的
    LHS／RHS 都不動——但這條仍要跑，因為「新增資產順手加了一個 path 函式卻沒登記」
    正是等式要抓的那個場景。
    """
    result = check_registry_equation()
    assert result.ok, result.failure_summary()


@pytest.mark.parametrize("asset_id", NEW_ASSET_IDS)
def test_new_assets_are_registered_with_a_derivation(asset_id: str) -> None:
    asset = next(a for a in ASSET_REGISTRY if a.asset_id == asset_id)
    assert asset.path_resolver is None
    assert asset.derived_in, asset_id
    assert asset.note.strip(), asset_id


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_gh_credential_is_owned_by_the_service_account_at_0600(scheme) -> None:
    """`hosts.yml`＝`gh` 唯一寫回 token 的檔 ⇒ 必須由使用它的帳號擁有才 refresh 得回來。"""
    entry = generate_plan(scheme).by_id(GH_CREDENTIAL)
    assert entry.owner == scheme.durable_state_owner
    assert entry.mode == 0o600
    assert not entry.is_directory
    # group／other 一個位都沒有——token 不得被同機任何其他帳號讀到。
    assert entry.mode & 0o077 == 0
    assert entry.acls == (), "同帳號的 Manager／monitor 不需要 ACL；出現 ACL 即為噪音"


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_gh_settings_stay_root_owned_read_only(scheme) -> None:
    """`config.yml` 的 `aliases` 可宣告 `!` shell alias ⇒ 服務帳號不得可寫。"""
    entry = generate_plan(scheme).by_id(GH_CONFIG)
    assert entry.owner == scheme.deploy_account
    assert entry.mode == 0o644
    assert entry.writer_accounts == frozenset({scheme.deploy_account})
    assert scheme.durable_state_owner not in entry.writer_accounts


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_the_two_files_deliberately_have_different_owners(scheme) -> None:
    """**本票最容易被下一個人做錯的那一條**：兩個檔不是同一種 owner。

    把 `config.yml` 也設成服務帳號 owned，就等於給 Manager 一條「自己把任意命令掛進
    每一次 `gh` 呼叫」的執行面（`aliases` 的 `!` 前綴）；把 `hosts.yml` 設成 root
    owned，則 token 過期後 refresh 不回來。兩個方向都會讓這條測試變紅。
    """
    plan = generate_plan(scheme)
    credential = plan.by_id(GH_CREDENTIAL)
    settings = plan.by_id(GH_CONFIG)
    assert credential.owner != settings.owner, (credential.owner, settings.owner)
    assert credential.owner == scheme.durable_state_owner
    assert settings.owner == scheme.deploy_account


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_the_gh_config_directory_stays_root_owned(scheme) -> None:
    """「檔案服務帳號 owned、目錄 root-owned」——性質全部落在目錄那一層。"""
    scaffold = {
        path: (owner, mode)
        for path, owner, _group, mode in DEFAULT_LAYOUT.scaffold_directories(scheme)
    }
    account = scheme.durable_state_owner
    gh_dir = DEFAULT_LAYOUT.gh_config_dir_of(account)
    parent = f"{DEFAULT_LAYOUT.home_of(account)}/.config"
    for path in (parent, gh_dir):
        assert path in scaffold, (scheme.scheme_id, path)
        owner, mode = scaffold[path]
        assert owner == scheme.deploy_account, path
        assert mode == 0o755, oct(mode)
        # group／other 皆無 `w`：增／刪／換需要的正是目錄的寫入權。
        assert mode & 0o022 == 0, oct(mode)


def test_job_accounts_get_no_gh_config_directory() -> None:
    """job 帳號刻意沒有這一層——job unit 已把 `GH_TOKEN` 清空，GitHub 寫入走 Manager。"""
    scheme = FOUR_WAY_SCHEME
    paths = {p for p, _o, _g, _m in DEFAULT_LAYOUT.scaffold_directories(scheme)}
    for principal in (Principal.BUILDER, Principal.REVIEWER, Principal.GATE):
        account = scheme.resolve(principal)
        if account is None:
            continue
        if account == scheme.durable_state_owner:
            continue
        assert DEFAULT_LAYOUT.gh_config_dir_of(account) not in paths, account


# ---------------------------------------------------------------------------
# 2. ReadWritePaths：掛在檔案本身，而且只有 Manager／monitor 拿得到
# ---------------------------------------------------------------------------

def test_the_credential_is_an_in_place_content_write_asset() -> None:
    """折算成父目錄會連 root-owned 的 `config.yml` 一起開放可寫。"""
    assert GH_CREDENTIAL in IN_PLACE_CONTENT_WRITE_ASSETS
    # `config.yml` 是 root-owned、任何 unit 都不會寫它，因此不需要（也不該）掛在
    # 任何 RWP 上；它列在 `_FILE_ASSET_IDS` 只是為了 file/dir 推斷正確。
    assert GH_CONFIG not in permgen.IN_PLACE_CONTENT_WRITE_ASSETS


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_and_monitor_units_open_only_the_credential_file(scheme) -> None:
    plan = generate_plan(scheme)
    account = scheme.durable_state_owner
    credential = DEFAULT_LAYOUT.asset_paths()[GH_CREDENTIAL]
    directory = DEFAULT_LAYOUT.gh_config_dir_of(DEFAULT_LAYOUT.manager_account)
    # Manager unit 走帳號全集（`principals=None`，既有行為），monitor 另過濾一層。
    for principals in (None, permgen.MONITOR_PRINCIPALS):
        rwp = read_write_paths(
            plan, DEFAULT_LAYOUT, account,
            extras=DEFAULT_LAYOUT.manager_extra_write_paths(account),
            principals=principals,
        )
        assert credential in rwp, (scheme.scheme_id, sorted(rwp))
        assert directory not in rwp, "父目錄不得出現——那會連 config.yml 一起開放"


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_no_job_unit_can_write_the_manager_credential(scheme) -> None:
    """Manager 的 token 洩漏面與 job 憑證不同級 ⇒ 任何 job 帳號都不得可寫。"""
    plan = generate_plan(scheme)
    entry = plan.by_id(GH_CREDENTIAL)
    writable = plan.all_writable_accounts(entry)
    for account in scheme.headless_accounts():
        assert account not in writable, (scheme.scheme_id, account)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
@pytest.mark.parametrize("asset_id", NEW_ASSET_IDS)
def test_the_service_account_can_actually_reach_the_new_assets(scheme, asset_id) -> None:
    """葉節點權限對 **≠** 路徑走得通：整條鏈每一層都要有 search 位（#620／#624）。"""
    plan = generate_plan(scheme)
    account = scheme.durable_state_owner
    blocked = unreachable_hops(
        plan, DEFAULT_LAYOUT, scheme, account=account, asset_id=asset_id
    )
    assert blocked == (), (scheme.scheme_id, asset_id, blocked)
    assert account_can_reach(
        plan, DEFAULT_LAYOUT, scheme, account=account, asset_id=asset_id
    )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_stay_strictly_narrower_than_manager(scheme) -> None:
    """#622 的不變式不得因為兩者都拿到憑證那一條而退化。"""
    plan = generate_plan(scheme)
    account = scheme.durable_state_owner
    extras = DEFAULT_LAYOUT.manager_extra_write_paths(account)
    manager = set(
        read_write_paths(plan, DEFAULT_LAYOUT, account, extras=extras)
    )
    monitor = set(
        read_write_paths(plan, DEFAULT_LAYOUT, account, extras=extras,
                         principals=permgen.MONITOR_PRINCIPALS)
    )
    assert monitor < manager, (sorted(monitor), sorted(manager))


# ---------------------------------------------------------------------------
# 3. 二分相容：本方案不存在的帳號 HOME 下的資產不得進 RWP
# ---------------------------------------------------------------------------

def test_two_way_deployment_never_gets_a_path_under_a_nonexistent_account() -> None:
    """#640 當年「乾脆不登記第二份」的那個陷阱，這次由機械規則接住。

    systemd 對不存在的 `ReadWritePaths=` 目標會讓 unit **直接起不來**；二分部署沒有
    `cortex-manager` 這個帳號，因此掛在它 HOME 下的資產在二分下必須整條消失。
    """
    plan = generate_plan(TWO_WAY_SCHEME)
    account = TWO_WAY_SCHEME.durable_state_owner
    rwp = read_write_paths(
        plan, DEFAULT_LAYOUT, account,
        extras=DEFAULT_LAYOUT.manager_extra_write_paths(account),
    )
    manager_home = DEFAULT_LAYOUT.home_of(DEFAULT_LAYOUT.manager_account)
    assert not any(path.startswith(manager_home) for path in rwp), sorted(rwp)
    assert rwp, "扣掉不適用的那些之後仍必須有東西——全空代表過濾寫壞了"


def test_inapplicable_assets_are_enumerable_not_silent() -> None:
    """靜默扣掉一條 RWP 與「漏授一條 RWP」在輸出上長得一樣，因此必須可列舉。"""
    two_way = {
        asset_id
        for asset_id, _path in inapplicable_home_anchored_assets(
            generate_plan(TWO_WAY_SCHEME), DEFAULT_LAYOUT
        )
    }
    assert two_way == {
        GH_CREDENTIAL,
        GH_CONFIG,
        "manager-gitconfig",
        "reviewer-planner-gitconfig",
        # #685：per-(account, executor) 憑證表的三格全掛在 `cortex-reviewer-planner`
        # 的 HOME 下，而二分方案把 reviewer／planner 併進 `cortex-svc` ⇒ 那三條路徑在
        # 二分部署裡不存在。**這正是 #640 當年「乾脆不登記第二份憑證」的那個陷阱**，
        # 而 #671 的 `inapplicable_home_anchored_assets()` 已經把它變成一條機械規則：
        # 二分方案的產出因此不含它們，Manager unit 的 RWP 不會多出不存在的路徑。
        "reviewer-planner-codex-state",
        "reviewer-planner-agy-state",
        "reviewer-planner-claude-state",
        # #698：那個帳號的 codex hooks 也掛在同一個 HOME 下，因此同樣不適用於二分。
        "reviewer-planner-codex-hooks",
    }, sorted(two_way)
    # 定案的兩個方案完全沒有不適用項——有的話就是 layout 的帳號欄位與方案對不上。
    for scheme in ALL_SCHEMES:
        assert inapplicable_home_anchored_assets(generate_plan(scheme), DEFAULT_LAYOUT) == ()


# ---------------------------------------------------------------------------
# 4. OS 層語意：能改內容、不能增刪換（#638／#657 的教訓）
# ---------------------------------------------------------------------------

class TestGhCredentialOsSemantics:
    """把「目錄 root-owned」那組性質對**真的檔案系統**驗一次。

    與 #642 的 `TestInPlaceCredentialOsSemantics` 同一個手法、同一個理由：裁決守的
    規則是「**目錄沒有 `w` 位給這個行程**」，真實部署裡服務帳號落在 root-owned
    `0755` 的 `other` 位（`r-x`），本測試以 owner 位為 `r-x`（`0555`）重現**同一段
    kernel 檢查**（`inode_permission(dir, MAY_WRITE)`），只是命中不同那組權限位。
    不需要第二個 UID。
    """

    @pytest.fixture()
    def tree(self, tmp_path: Path):
        """`~/.config/gh` 的最小重現：目錄不可寫、`hosts.yml` 可寫、`config.yml` 不可換。"""
        gh_dir = tmp_path / ".config" / "gh"
        gh_dir.mkdir(parents=True)
        hosts = gh_dir / "hosts.yml"
        hosts.write_text("github.com:\n    oauth_token: old\n", encoding="utf-8")
        hosts.chmod(0o600)
        settings = gh_dir / "config.yml"
        settings.write_text("editor:\n", encoding="utf-8")
        settings.chmod(0o644)
        gh_dir.chmod(0o555)
        try:
            yield gh_dir, hosts, settings
        finally:
            gh_dir.chmod(0o755)

    def test_the_token_can_be_refreshed_in_place(self, tree) -> None:
        """refresh 走得通：`hosts.yml` 是自己的，`O_TRUNC` 覆寫不需要目錄的寫入權。"""
        _gh_dir, hosts, _settings = tree
        hosts.write_text("github.com:\n    oauth_token: refreshed\n", encoding="utf-8")
        assert "refreshed" in hosts.read_text(encoding="utf-8")
        assert stat.S_IMODE(hosts.stat().st_mode) == 0o600

    def test_new_files_cannot_be_created_in_the_directory(self, tree) -> None:
        """「建不了新檔」——同時封掉「暫存檔 ＋ rename」那條原子替換路徑。"""
        gh_dir, _hosts, _settings = tree
        with pytest.raises(PermissionError):
            (gh_dir / "hosts.yml.tmp").write_text("x", encoding="utf-8")

    def test_the_credential_cannot_be_unlinked(self, tree) -> None:
        """「刪不掉」——unlink 需要的是**目錄**的寫入權，不是檔案的。"""
        _gh_dir, hosts, _settings = tree
        with pytest.raises(PermissionError):
            hosts.unlink()
        assert hosts.exists()

    def test_the_root_owned_settings_file_cannot_be_replaced(self, tree) -> None:
        """「換不掉同目錄下的另一個 root-owned 檔」——rename 同樣要目錄的寫入權。

        這一條正是 `config.yml` 維持 root-owned 買到的東西：服務帳號沒辦法用自己那份
        可寫的 `hosts.yml` 蓋掉它，因此宣告不了 `!` shell alias。
        """
        _gh_dir, hosts, settings = tree
        with pytest.raises(PermissionError):
            os.replace(hosts, settings)
        assert settings.read_text(encoding="utf-8") == "editor:\n"


@pytest.mark.skip(
    reason=(
        "#638／#657 的教訓：涉及 OS 層語意、單 UID 測不出來的，明確 skip 並說明理由，"
        "不得靜默通過。這裡待驗的命題是「**檔案 owner 不是本行程**」那一半——"
        "`config.yml` 由 root 擁有、服務帳號連內容都改不了（DAC 的 owner/group/other "
        "三組位裡命中 `other` 的 `r--`）。上面 `TestGhCredentialOsSemantics` 重現得了"
        "「目錄沒有 `w` 位」那一半（同一段 `inode_permission()`），但重現不了這一半："
        "本行程就是檔案的 owner，chmod 0444 之後它仍改得掉（先 chmod 回來即可），"
        "而以 root 執行時 DAC 對它整個不適用。真正驗得到它需要第二個 UID ＋ 真的 "
        "systemd 加固面，兩者本測試環境都沒有。\n"
        "**那一半改由兩個地方守**：(1) 產生器測試 `test_gh_settings_stay_root_owned_"
        "read_only`（`entry.owner == deploy_account` 且 writer 不含服務帳號）；"
        "(2) runbook 第 4e 步的實機驗證，以 `sudo -u <服務帳號>` 實跑並期望 "
        "`Permission denied`。在這裡跑一次「同 UID 下改得掉 config.yml」只會證明一件"
        "與待驗命題無關的事，卻會讓人以為驗過了。"
    )
)
def test_the_service_account_cannot_rewrite_the_root_owned_settings_file() -> None:  # pragma: no cover
    raise AssertionError("需要第二個 UID；見 skip 理由")


# ---------------------------------------------------------------------------
# 5. pytest／PyYAML：系統層 python 發行版，版本是明示的部署決定
# ---------------------------------------------------------------------------

def test_pytest_is_a_system_layer_python_distribution() -> None:
    by_name = {d.name: d for d in SYSTEM_PYTHON_DISTRIBUTIONS}
    assert set(by_name) == {"pytest", "PyYAML"}, sorted(by_name)
    for dist in SYSTEM_PYTHON_DISTRIBUTIONS:
        assert dist.interpreter == SYSTEM_INTERPRETER, dist.name
        assert dist.requirement.strip() and dist.declared_in.strip(), dist.name
        assert dist.required_by, dist.name
        assert dist.note.strip(), dist.name
    assert by_name["pytest"].module == "pytest"
    # 發行版名與模組名不同的那一族——寫錯會讓 runbook 的驗證指令 import 錯東西。
    assert by_name["PyYAML"].module == "yaml"


def test_python_distributions_are_not_smuggled_into_the_executable_rosters() -> None:
    """它們不是可執行檔：塞進 `SYSTEM_PROGRAMS` 會讓「名冊上每一項都 which 得到」變假。

    這與 #661 對「不要把 `srt` 併進 `EXECUTOR_TOOLS`」是同一條論證：盤點完整性不可以
    用「往別張表塞東西」來換，那會弄壞那張表原本承載的性質。
    """
    executables = (
        {t.name for t in EXECUTOR_TOOLS}
        | {t.name for t in SERVICE_TOOLS}
        | {p.name for p in SYSTEM_PROGRAMS}
    )
    for dist in SYSTEM_PYTHON_DISTRIBUTIONS + DEPLOYMENT_PYTHON_DISTRIBUTIONS:
        assert dist.name not in executables, dist.name


def test_the_version_constraint_has_exactly_one_source_of_truth() -> None:
    """「版本是明示的部署決定」＝約束由 `pyproject.toml` 宣告，表上不得另寫一份。

    這條測試就是那句話的機器可讀形式：改了 `pyproject.toml` 的下限而沒同步表，
    或反過來，都會紅。
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    by_name = {d.name: d for d in SYSTEM_PYTHON_DISTRIBUTIONS}
    for name in ("pytest", "PyYAML"):
        requirement = by_name[name].requirement
        assert f'"{requirement}"' in text, (name, requirement)


def test_deployment_venv_distributions_need_no_new_filesystem_asset() -> None:
    """部署 venv 是既有的 root-owned 資產 ⇒ 這一族**不新增**任何登記表項（#661 裁決）。"""
    asset_ids = {a.asset_id for a in ASSET_REGISTRY}
    for dist in DEPLOYMENT_PYTHON_DISTRIBUTIONS:
        assert dist.interpreter == DEPLOYMENT_VENV_INTERPRETER, dist.name
        assert dist.name not in asset_ids, dist.name


# ---------------------------------------------------------------------------
# 6. gate 宣告：每一段都必須落在某張表上
# ---------------------------------------------------------------------------

def test_gate_env_prefix_matches_the_runtime_contract() -> None:
    """`permgen` 與 `coordinator` 刻意不互相 import ⇒ 由契約測試釘住兩邊逐字相等。"""
    assert GATE_COMMAND_ENV_PREFIX == gate_ledger.GATE_ENV_PREFIX


def test_every_segment_of_the_gate_declaration_is_on_some_roster() -> None:
    """#666 的核心不變式：gate 宣告用到的東西不得有任何一段不在盤點內。

    實機症狀正是這條不成立——`python3` 沒進過任何名冊、`pytest` 也沒有，於是
    「宣告合法」與「跑得起來」之間沒有任何機械關係。
    """
    system_programs = {p.name for p in SYSTEM_PROGRAMS}
    system_modules = {d.module for d in SYSTEM_PYTHON_DISTRIBUTIONS}
    assert GATE_COMMAND_DECLARATIONS, "至少要有一個宣告，否則 ledger 永遠是空的"
    for name, argv in GATE_COMMAND_DECLARATIONS.items():
        assert argv, name
        assert argv[0] in system_programs, (name, argv[0])
        if "-m" in argv:
            module = argv[argv.index("-m") + 1]
            assert module in system_modules, (name, module)


def test_the_declaration_covers_every_gate_the_deck_requires() -> None:
    """否則照 runbook 裝出來的部署一開機 doctor 的 `gate-declarations` 就是紅的（#540）。"""
    required = doctor._deck_required_gate_names()
    assert required <= set(GATE_COMMAND_DECLARATIONS), sorted(
        required - set(GATE_COMMAND_DECLARATIONS)
    )


def test_the_generated_declaration_passes_the_runtime_validator() -> None:
    """產生器出的值必須真的被 `gate_ledger` 收得下（typed argv、非 shell wrapper）。"""
    env = DEFAULT_LAYOUT.gate_command_env()
    specs = {spec.name: spec.argv for spec in gate_ledger.load_gate_specs(env)}
    assert specs == {
        name: tuple(argv) for name, argv in GATE_COMMAND_DECLARATIONS.items()
    }, specs
    assert set(gate_ledger.declared_gate_names(env)) == set(GATE_COMMAND_DECLARATIONS)


def test_the_gate_interpreter_resolves_to_the_system_layer() -> None:
    """宣告用相對名 ⇒ 由 `PSC_GATE_PATH` 解析 ⇒ 落在系統層，不是部署 venv。

    這就是「pytest 為什麼必須裝到系統層」的機械成因；改成絕對路徑的 venv
    interpreter 會讓這條測試變紅，那時 `SYSTEM_PYTHON_DISTRIBUTIONS` 也該一起改。
    """
    path_value = DEFAULT_LAYOUT.job_path_value()
    assert path_value.endswith(":".join(JOB_PATH_SYSTEM_TAIL))
    assert DEFAULT_LAYOUT.venv_root not in path_value
    for argv in GATE_COMMAND_DECLARATIONS.values():
        assert not argv[0].startswith("/"), argv[0]


# ---------------------------------------------------------------------------
# 7. 窮舉盤點：**兩個方向**都封閉
# ---------------------------------------------------------------------------

def test_every_listed_dependency_really_is_covered_by_some_roster() -> None:
    """盤點列到但表上查無 ⇒ 紅。這條擋的是「寫了一列就算盤到了」。"""
    assert uncovered_run_dependencies() == (), [
        (d.name, d.covered_by) for d in uncovered_run_dependencies()
    ]


def test_every_roster_entry_is_accounted_for_in_the_inventory() -> None:
    """**反方向**——本票真正要買的東西。

    往 `SERVICE_TOOLS`／`SYSTEM_PROGRAMS`／兩張 python 發行版表加一項、或新增一個掛在
    帳號 HOME 下的登記表資產，而沒有說明「它在 run 的哪一段被誰碰到」，這條就會紅。
    #640→#661→#666 每一次的形態都是後者只做了一半。
    """
    assert unlisted_roster_entries() == (), unlisted_roster_entries()


def test_the_inventory_rows_are_well_formed() -> None:
    seen: set[tuple[str, str]] = set()
    for dep in RUN_EXTERNAL_DEPENDENCIES:
        assert dep.key not in seen, dep.key
        seen.add(dep.key)
        assert isinstance(dep.kind, DependencyKind), dep.name
        assert dep.principals, dep.name
        assert dep.stages, dep.name
        for principal in dep.principals:
            assert isinstance(principal, Principal), dep.name
        for stage in dep.stages:
            assert isinstance(stage, RunStage), dep.name
        assert dep.note.strip(), dep.name


def test_the_same_distribution_can_appear_once_per_interpreter() -> None:
    """PyYAML 在系統層與部署 venv 各有一份，是**兩個**部署決定，不得被去重掉。"""
    pyyaml = [d for d in RUN_EXTERNAL_DEPENDENCIES if d.name == "PyYAML"]
    assert len(pyyaml) == 2, pyyaml
    assert {d.covered_by for d in pyyaml} == {
        "SYSTEM_PYTHON_DISTRIBUTIONS",
        "DEPLOYMENT_PYTHON_DISTRIBUTIONS",
    }


def test_the_inventory_covers_every_review_sandbox_executable() -> None:
    """doctor 要求的每一支都必須在盤點內——#664 那條的加強版。

    #661 當時把 `python3` 排除在外，理由寫的是「它是部署 venv 自己的 interpreter，
    不是外部相依」。**#666 查證後那個前提不成立**：`review-sandbox` probe 是以
    `PATH` 解析它的，gate 宣告也是——兩處拿到的都是系統層那一支。因此這條不再有
    例外。
    """
    listed = {dep.name for dep in RUN_EXTERNAL_DEPENDENCIES}
    assert set(doctor.REVIEW_SANDBOX_EXECUTABLES) <= listed, sorted(
        set(doctor.REVIEW_SANDBOX_EXECUTABLES) - listed
    )


def test_every_downgraded_principal_appears_somewhere_in_the_inventory() -> None:
    """四個降權角色都要有相依被盤到；某個角色一項都沒有就代表那一面沒盤。"""
    covered = {p for dep in RUN_EXTERNAL_DEPENDENCIES for p in dep.principals}
    for principal in (
        Principal.MANAGER, Principal.MONITOR,
        Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER, Principal.GATE,
    ):
        assert principal in covered, principal


def test_home_anchored_assets_are_derived_not_hand_written() -> None:
    """憑證／per-account 設定那一族由 `asset_paths()` 機械導出，不是手抄清單。"""
    assert home_anchored_asset_ids() == frozenset({
        # #698：builder 的 codex 憑證改成 sticky 樹（舊 id `builder-executor-credential`），
        # 而 `codex-hooks` 拆成 per-account 兩份——兩者都由憑證表那條規則導出。
        "builder-codex-state",
        "builder-codex-hooks",
        "reviewer-planner-codex-hooks",
        "builder-gitconfig",
        GH_CONFIG,
        GH_CREDENTIAL,
        "manager-gitconfig",
        "reviewer-planner-gitconfig",
        # #685（#672 票 D）：per-(account, executor) 憑證表為 `cortex-reviewer-planner`
        # 展開的三格。它們**是機械導出的結果**——這條測試本身就是在驗那件事：新增一個
        # 掛在帳號 HOME 下的資產而沒有把它列進 `RUN_EXTERNAL_DEPENDENCIES`，
        # `unlisted_roster_entries()` 就會非空（見下一條）。
        "reviewer-planner-codex-state",
        "reviewer-planner-agy-state",
        "reviewer-planner-claude-state",
    }), sorted(home_anchored_asset_ids())


def test_deferred_dependencies_stay_enumerable() -> None:
    """比照 #661 的 `unresolved_node_execution_surfaces()`：不裁決，但不得靜默消失。

    補上或悄悄拿掉都會讓這條紅——那正是要的：它們必須是一個**被看見的**決定。

    **#685（#672 票 D）縮短了這份清單，並更正了另外兩項的理由**：

    - `reviewer-planner-executor-credential` **已關閉**——per-(account, executor) 憑證表
      （U-5 裁決）為那個帳號登記了三格登入態（codex／agy／claude）。原本那條 deferred
      的 `disposition` 寫的是「補登記表第二列（產生器一行都不必改）」，而 #686 的實測
      證明那句話是錯的：codex 需要 `$CODEX_HOME` 整棵可寫，單檔不夠。
    - `reviewer-planner-codex-hooks` 在 #685 當時**留著**，理由是它與 codex 的可用性
      **在 `$CODEX_HOME` 這一層互斥**（升為 U-9）。**#698 把它關掉了**，而且不是
      「刪一列」：operator 裁決採方案 A（sticky ＋ root-owned hooks）之後那條張力
      **真的不存在了**——兩件事同時成立，兩份 hooks 都成為登記表資產。
      關閉的前提有兩個，兩個都已在本 PR 內完成：mode 管線能表達 sticky
      （`build_entry()` 的安全網），以及「codex 在一個它不擁有的 `$CODEX_HOME` 下
      跑得起來」的實機證據（runbook 第 4e-2b 步，完整模板 unit 加固面）。
    - `manager-claude-credential` 在 #685 當時**留著**：U-5 解除了它的機械阻礙
      （表達得了了），但「要不要給 Manager 一份模型憑證」是 #672 的核心裁決，答案是
      不要；本項由票 F（#687）切換之後隨 direct 路徑一起消失，**不是**當時刪掉。

    **#687（票 F）把它移除了，而且是「消失」不是「登記」。** 四分部署的
    `PSC_JOB_RUNNER=systemd-template` 讓 `_select_planning_invoker()` 恆回
    `JobPlanningInvoker`，模型 CLI 只在 `cortex-reviewer-job@` 實例內執行，Manager
    不再 exec 任何 executor ⇒ 它不需要 executor 憑證。清單因此**再縮短一項**，剩兩項。
    """
    deferred = deferred_run_dependencies()
    assert {item.name for item in deferred} == {
        "gate-gitconfig",
    }, sorted(item.name for item in deferred)
    # #698：關閉的那一項不得以任何形式「改個名字留下來」，而且它必須真的變成資產。
    assert "reviewer-planner-codex-hooks" not in {item.name for item in deferred}
    assert "reviewer-planner-codex-hooks" in {a.asset_id for a in ASSET_REGISTRY}
    # #687：被移除的那一項不得以任何形式「改個名字留下來」。
    assert "manager-claude-credential" not in {item.name for item in deferred}
    for item in deferred:
        assert item.principals, item.name
        assert item.reason.strip() and item.symptom.strip(), item.name
        assert item.disposition.strip(), item.name
    # 未決項**不得**混進主盤點——主盤點的每一列都必須真的有歸宿。
    listed = {dep.name for dep in RUN_EXTERNAL_DEPENDENCIES}
    for item in deferred:
        assert item.name not in listed, item.name


def test_the_reviewer_credential_gap_is_closed_without_widening_the_write_surface() -> None:
    """#685 把上面那條 deferred 的**症狀**翻面：缺口關了，而且沒有付出可寫面。

    本測試的前身是 `test_the_reviewer_credential_gap_is_real_not_theoretical`，它的
    docstring 逐字寫著「哪天有人補上登記表第二列，這條會紅——那正是提醒去刪掉這條測試
    與那筆 deferred」。#685 就是那一天，所以它按設計被翻成正向的形態。

    **翻面的方式與 #640 當初預期的不同，這一點必須釘住**：預期是「reviewer 模板 unit
    的 RWP 逐字含憑證**檔案**」，而 #686 實測 codex 需要 `$CODEX_HOME` 整棵可寫 ⇒ 那個
    帳號的三份登入態改走 `HOME_REDIRECT_TREE`（root-owned symlink → 該帳號既有的
    `cache`）。因此正確的不變式是**更強的那一條**：

      unit 的 `ReadWritePaths` **逐字不變**，而三份登入態全部落在它已經涵蓋的 `cache`
      底下 ⇒ 憑證面可寫、可 refresh，且**零新增可寫面**。
    """
    scheme = FOUR_WAY_SCHEME
    plan = generate_plan(scheme)
    account = scheme.resolve(Principal.REVIEWER)
    assert account is not None
    rwp = read_write_paths(
        plan, DEFAULT_LAYOUT, account,
        extras=DEFAULT_LAYOUT.job_extra_write_paths(account),
        principals=permgen.JOB_PRINCIPAL_PERSONAS[Principal.REVIEWER],
        retired=permgen.RETIRED_JOB_WRITE_ASSETS,
    )
    cache = DEFAULT_LAYOUT.cache_of(account)
    for asset_id, target in DEFAULT_LAYOUT.symlink_targets().items():
        if not asset_id.startswith("reviewer-planner-"):
            continue
        # (a) 落在 cache 底下 ⇒ 已被既有那條 RWP 涵蓋；
        assert target.startswith(cache + "/"), (asset_id, target)
        # (b) 自己**不**出現在 RWP 上（`_minimize()` 吃掉子路徑，且 writer 只有 root）。
        assert target not in rwp, (asset_id, sorted(rwp))
        assert DEFAULT_LAYOUT.asset_paths()[asset_id] not in rwp, asset_id
    assert cache in rwp, sorted(rwp)
    # 對照：builder 那一份仍是 `IN_PLACE_FILE`，RWP 逐字掛在**檔案本身**（#640 裁決 (b)
    # 一行未改）——同一張表，兩種形狀，這正是 U-5 要 per-(account, executor) 的理由。
    builder = scheme.resolve(Principal.BUILDER)
    builder_rwp = read_write_paths(
        plan, DEFAULT_LAYOUT, builder,
        extras=DEFAULT_LAYOUT.job_extra_write_paths(builder),
        principals=permgen.JOB_PRINCIPAL_PERSONAS[Principal.BUILDER],
        retired=permgen.RETIRED_JOB_WRITE_ASSETS,
    )
    assert DEFAULT_LAYOUT.executor_credential_of(builder) in builder_rwp


# ---------------------------------------------------------------------------
# 8. 落位計畫：能重現實機那兩項，且仍是純字串
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_toolchain_plan_reproduces_the_two_manual_fixes(scheme) -> None:
    """驗收條件是「重跑計畫後零漂移」⇒ 計畫本身必須帶得出這兩項。"""
    text = "\n".join(build_toolchain_plan(scheme, DEFAULT_LAYOUT))
    # 漂移項 1：系統層 pytest（＋被測樹的 PyYAML），版本約束與來源都要在計畫裡。
    for dist in SYSTEM_PYTHON_DISTRIBUTIONS:
        assert f"--break-system-packages '{dist.requirement}'" in text, dist.name
        assert dist.declared_in in text, dist.name
    # 漂移項 2：gh 憑證的兩個檔、兩種 owner、以該身分實測的驗證步驟。
    credential = DEFAULT_LAYOUT.gh_credential_of(DEFAULT_LAYOUT.manager_account)
    settings = DEFAULT_LAYOUT.gh_settings_of(DEFAULT_LAYOUT.manager_account)
    assert f"-m 0600 \\" in text and credential in text
    assert f"-m 0644 \\" in text and settings in text
    assert "gh auth status" in text
    assert "洩漏面不同級" in text


def test_toolchain_plan_prints_the_exhaustive_inventory() -> None:
    text = "\n".join(build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT))
    assert f"目前共 {len(RUN_EXTERNAL_DEPENDENCIES)} 項" in text
    for dep in RUN_EXTERNAL_DEPENDENCIES:
        assert f"{dep.name}  ({dep.kind.value};" in text, dep.name
    for item in deferred_run_dependencies():
        assert item.name in text, item.name


def test_toolchain_plan_lines_are_still_comments_or_the_three_allowed_commands() -> None:
    """新增的兩段全是註解——`install`／`chown`／`chmod` 之外的行不得出現。"""
    allowed = ("install -d ", "chown ", "chmod ")
    for scheme in (TWO_WAY_SCHEME,) + ALL_SCHEMES:
        for line in build_toolchain_plan(scheme, DEFAULT_LAYOUT):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert stripped.startswith(allowed), (scheme.scheme_id, stripped)


def test_toolchain_plan_is_deterministic() -> None:
    assert build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT) == build_toolchain_plan(
        FOUR_WAY_SCHEME, DEFAULT_LAYOUT
    )


def test_the_new_generators_are_still_pure_functions() -> None:
    """產生器不得取得任何實作面——本票新增的幾個函式一樣。"""
    src = inspect.getsource(permgen)
    for forbidden in ("subprocess", "os.system", "os.chown", "os.chmod", "shutil."):
        assert forbidden not in src, forbidden
    # 實跑一次確認沒有 IO（有 IO 會在無權限的環境炸掉，而不是靜默通過）。
    uncovered_run_dependencies()
    unlisted_roster_entries()
    deferred_run_dependencies()
    DEFAULT_LAYOUT.gate_command_env()
    build_toolchain_plan(FOUR_WAY_SCHEME, DEFAULT_LAYOUT)
