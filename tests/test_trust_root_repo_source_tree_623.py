"""#623：per-job clone 的信任根層——來源樹、三份 root-owned `.gitconfig`、commit spool。

背景（#623 實測，已由 operator 裁決）：`git worktree` 的**共用 object store** 與三分
隔離互斥——builder 要 commit 就得能寫 object store，能寫就等於邊界在 git 這一層漏掉。
因此 job 工作區改為 **per-job 完整 clone**（0.5 秒／35MB），來源是一份 Manager-owned
樹內的 **working checkout**（monitor 要掃工作樹裡的檔案，bare 沒有工作樹）。

本檔釘住信任根層的五件事：

1. `repo-source-tree` 在登記表裡，且**兩個 job 帳號唯讀**——計畫一個 `w` 都不給；
2. 來源樹的 owner 是 **Manager**（0817 裁決推翻本票初版的 root-owned）：`git fetch`
   必須把 `FETCH_HEAD` 寫進目標 repo，而成果回收正是 fetch 進來源樹——「Manager 唯讀」
   與「Manager 回收成果」互斥。機械落點是 `owner_class=MANAGER_STATE`，因此 Manager
   unit 的 ReadWritePaths 涵蓋它、monitor unit **不**涵蓋（persona 過濾，#622 不變式）；
3. traverse 鏈完整（沿用 #624 的 `unreachable_hops()`）——葉節點權限對 ≠ 路徑走得通；
4. **三份** `.gitconfig`（builder／reviewer-planner／**manager**）皆 root-owned、對應
   帳號不可寫，內容由 permgen 產生，且每個來源 repo **兩條** `safe.directory`
   （工作樹根 ＋ `<root>/.git`）——實測從非 bare 來源 clone 時 git 檢查的是後者；
5. `commit-spool`（bundle 回收通道）形態逐條比照 `review-verdict-spool`：容器
   Manager-owned 0700，producer 只拿 `wx` 無 `r` 的 ACL。
"""
from __future__ import annotations

import inspect

import pytest

from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.__main__ import main
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    OwnerClass,
    PathLayout,
    UnresolvedSourceRepoError,
    account_can_reach,
    build_account_gitconfig,
    build_job_unit,
    build_manager_unit,
    build_monitor_unit,
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

#: #626：`operator`／`external` 是部署決定，模組層方案刻意留 `None`。要驗命令輸出的
#: 測試一律用注入後的方案（fail-closed 本身由 #626 的專屬檔釘住）。
DEPLOYMENT_ACCOUNTS = {
    Principal.OPERATOR: "cortex-ops",
    Principal.EXTERNAL: "cortex-outbox-reader",
}
TWO_WAY_SCHEME = _TWO_WAY_BASE.with_principal_accounts(DEPLOYMENT_ACCOUNTS)
THREE_WAY_SCHEME = _THREE_WAY_BASE.with_principal_accounts(DEPLOYMENT_ACCOUNTS)
ALL_SCHEMES = [TWO_WAY_SCHEME, THREE_WAY_SCHEME]

SOURCE_SLUG = "paulsha-cortex"
LAYOUT = DEFAULT_LAYOUT.with_source_repo_slugs((SOURCE_SLUG,))

#: 跑模型的兩個 job persona——「兩個 job 帳號對來源樹唯讀」的驗收對象。
JOB_PRINCIPALS = (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER)

#: 本票落地的全部新資產（`test_new_assets_introduce_no_new_deployment_decision` 等共用）。
NEW_ASSET_IDS = (
    "repo-source-tree",
    "builder-gitconfig",
    "reviewer-planner-gitconfig",
    "manager-gitconfig",
    "commit-spool",
)

#: persona → 該 persona 那份 `.gitconfig` 的登記表 asset_id。
GITCONFIG_CASES = (
    (Principal.BUILDER, "builder-gitconfig"),
    (Principal.REVIEWER, "reviewer-planner-gitconfig"),
    (Principal.MANAGER, "manager-gitconfig"),
)


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# 登記表：新資產的分類
# ---------------------------------------------------------------------------

def test_source_tree_is_registered_and_fully_classified() -> None:
    asset = registry.asset_by_id("repo-source-tree")
    assert asset.tier is AssetTier.TIER_0
    assert asset.tree is TrustTree.MANAGER_OWNED
    # 0817 裁決：writer 是 Manager（成果回收要 fetch 進來，fetch 必寫 FETCH_HEAD），
    # 不再是部署身分——因此 ingress 也從 DEPLOYMENT_WRITE 改為 MANAGER_INTERNAL。
    assert asset.ingress_kind is IngressKind.MANAGER_INTERNAL
    assert asset.writers == (Principal.MANAGER,)
    # reader 面要涵蓋 Manager（fetch／解析）、monitor（掃描）與三個 headless persona
    # （clone 來源）——#624 的 traverse 鏈就是由 reader 宣告機械補出來的。
    assert set(asset.readers) >= {Principal.MANAGER, Principal.MONITOR, *JOB_PRINCIPALS}
    # 容器沒有 path_resolver：程式碼解析的是單一 repo（PSC_REPO_ROOT → <此樹>/<slug>）。
    assert asset.path_resolver is None
    # headless 一律不在 writer 面——這條不因 owner 換人而鬆動。
    assert not asset.headless_writable()


@pytest.mark.parametrize(
    "asset_id, readers",
    [
        ("builder-gitconfig", {Principal.BUILDER}),
        ("reviewer-planner-gitconfig", {Principal.REVIEWER, Principal.PLANNER}),
        ("manager-gitconfig", {Principal.MANAGER, Principal.MONITOR}),
    ],
)
def test_gitconfig_assets_mirror_the_codex_hooks_shape(asset_id, readers) -> None:
    """比照既有的 `codex-hooks`：root-owned、落在帳號 HOME 下、enforcement plane。

    Manager 那份也在其中：Manager 可寫的是**來源樹**，不是自己的 git 設定——
    `.gitconfig` 可指定 `core.fsmonitor`／`alias.*` 這類會執行外部命令的鍵。
    """
    asset = registry.asset_by_id(asset_id)
    hooks = registry.asset_by_id("codex-hooks")
    assert asset.tier is hooks.tier is AssetTier.TIER_0
    assert asset.tree is hooks.tree is TrustTree.MANAGER_OWNED
    assert asset.ingress_kind is hooks.ingress_kind is IngressKind.DEPLOYMENT_WRITE
    assert asset.writers == (Principal.INSTALLER,)
    assert set(asset.readers) == readers


def test_commit_spool_mirrors_the_review_verdict_spool_shape() -> None:
    """bundle 回收通道與既有 verdict 通道**同構**——同一條政策，換一個 producer。"""
    spool = registry.asset_by_id("commit-spool")
    verdict = registry.asset_by_id("review-verdict-spool")
    assert spool.tier is verdict.tier is AssetTier.TIER_0
    assert spool.tree is verdict.tree is TrustTree.JOB_VISIBLE
    assert spool.ingress_kind is verdict.ingress_kind is IngressKind.INTERPROCESS
    assert spool.path_resolver == "paulsha_cortex.config.paths:commit_spool_root"
    # producer 是 builder（下面那條測試論證為何不含 reviewer／planner）；consumer 是 Manager。
    assert set(spool.writers) == {Principal.MANAGER, Principal.BUILDER}
    assert set(spool.readers) == {Principal.MANAGER}
    assert Principal.ANY_SAME_UID not in spool.writers


def test_commit_spool_producer_is_exactly_the_committing_persona() -> None:
    """producer 面由登記表導出，不是憑印象選的。

    唯一以 git commit 交付的 persona 是 builder（`repo-worktree` 的 writer 只有它）；
    reviewer 的交付通道是 `review-verdict-spool`、planner 的是 `dispatch-specs-tree`。
    多授一個 `wx` 給沒有 producer 的帳號＝多開一條無人消費的寫入面。
    """
    spool = registry.asset_by_id("commit-spool")
    worktree_writers = set(registry.asset_by_id("repo-worktree").writers)
    assert worktree_writers == {Principal.BUILDER}
    headless_producers = set(spool.writers) & registry.HEADLESS_PERSONAS
    assert headless_producers == worktree_writers
    # reviewer／planner 的交付通道各自存在，因此不需要（也不該）出現在本 spool。
    assert Principal.REVIEWER in registry.asset_by_id("review-verdict-spool").writers
    assert Principal.PLANNER in registry.asset_by_id("dispatch-specs-tree").writers


def test_new_assets_introduce_no_new_deployment_decision() -> None:
    """#626 的 fail-closed 面：新資產只用得到已可解析的 principal。

    多一個沒對應的 principal 就會讓整個 `--commands` fail-closed；這條在登記表側先擋。
    """
    used: set[Principal] = set()
    for asset_id in NEW_ASSET_IDS:
        asset = registry.asset_by_id(asset_id)
        used.update(asset.writers)
        used.update(asset.readers)
    for principal in used:
        assert THREE_WAY_SCHEME.resolve(principal) is not None, principal
    assert THREE_WAY_SCHEME.unresolved_principals() == ()


def test_layout_places_the_source_tree_in_the_durable_state_tree() -> None:
    paths = DEFAULT_LAYOUT.asset_paths()
    assert paths["repo-source-tree"] == "/var/lib/cortex/repos"
    assert DEFAULT_LAYOUT.repo_source_root == "/var/lib/cortex/repos"
    # 每個受治理 repo 一格；slug 是部署決定（見 gitconfig 那節）。
    assert LAYOUT.source_repo_paths() == ("/var/lib/cortex/repos/paulsha-cortex",)
    # 三份 `.gitconfig` 各落在自己帳號的 HOME 下，與 `~/.codex` 同一層。
    assert paths["builder-gitconfig"] == "/var/lib/cortex-builder/.gitconfig"
    assert paths["reviewer-planner-gitconfig"] == (
        "/var/lib/cortex-reviewer-planner/.gitconfig"
    )
    assert paths["manager-gitconfig"] == "/var/lib/cortex-manager/.gitconfig"
    # commit spool 與 verdict spool 同層（都掛在 coordinator 樹底下）。
    assert paths["commit-spool"] == "/var/lib/cortex/coordinator/commit-spool"
    assert DEFAULT_LAYOUT.commit_spool_root == paths["commit-spool"]


def test_commit_spool_layout_matches_the_path_contract() -> None:
    """`PathLayout` 與 `config.paths` 是成對契約——目錄名只有一份字面量。"""
    from paulsha_cortex.config import paths as path_contract

    assert path_contract.COMMIT_SPOOL_DIRNAME == "commit-spool"
    assert DEFAULT_LAYOUT.commit_spool_root.endswith(
        f"/{path_contract.COMMIT_SPOOL_DIRNAME}"
    )
    assert DEFAULT_LAYOUT.commit_spool_root.startswith(DEFAULT_LAYOUT.coordinator_root)


def test_source_tree_paths_follow_a_relocated_layout() -> None:
    """路徑由 `agents_root` 導出，不得寫死——換部署位置不必改產生器一行程式碼。"""
    alt = PathLayout(agents_root="/srv/cx", worktree_root="/srv/cx/wt",
                     deploy_root="/srv/deploy", instance="cx", home_root="/srv/accounts")
    paths = alt.asset_paths()
    assert paths["repo-source-tree"] == "/srv/cx/repos"
    assert paths["builder-gitconfig"] == "/srv/accounts/cortex-builder/.gitconfig"
    assert paths["manager-gitconfig"] == "/srv/accounts/cortex-manager/.gitconfig"
    assert paths["commit-spool"] == "/srv/cx/coordinator/commit-spool"
    assert alt.with_source_repo_slugs(("x",)).source_repo_paths() == ("/srv/cx/repos/x",)


def test_with_job_segment_preserves_the_new_layout_fields() -> None:
    """job layout 是由 setup layout 換 segment 得來的——欄位不得在中途被重設。

    逐欄位重建的舊寫法會讓每個新欄位都靜默掉回預設值，job unit 因此指到另一棵樹。
    """
    job_layout = LAYOUT.with_job_segment("%i")
    assert job_layout.source_repo_slugs == LAYOUT.source_repo_slugs
    assert job_layout.reviewer_planner_account == LAYOUT.reviewer_planner_account
    assert job_layout.manager_account == LAYOUT.manager_account
    assert job_layout.repo_source_root == LAYOUT.repo_source_root
    assert job_layout.commit_spool_root == LAYOUT.commit_spool_root
    assert job_layout.job_segment == "%i"


# ---------------------------------------------------------------------------
# 來源樹：兩個 job 帳號唯讀（本 PR 的核心驗收）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_source_tree_grants_no_write_beyond_its_owner(scheme) -> None:
    """驗收：可寫面**只有** owner——沒有任何授寫 ACL、group／other 零寫入位。

    二分下 reviewer／planner 與 Manager 併帳（`cortex-svc`），因此那個方案裡「job 帳號
    對來源樹唯讀」本來就不成立——這正是三分定案的理由之一，逐帳號的驗收放在下一條。
    """
    plan = generate_plan(scheme)
    entry = plan.by_id("repo-source-tree")
    assert plan.all_writable_accounts(entry) == {entry.owner}
    # ACL 面不得有任何授寫條目（跨帳號授權在此類一律唯讀）。
    assert not [a for a in entry.acls if a.writable]
    # group／other 無寫入位（spec §R2）。
    assert not entry.mode & 0o022, entry.mode_str


def test_three_way_keeps_both_job_accounts_off_the_source_tree_write_face() -> None:
    """定案方案的核心驗收：兩個 job 帳號對來源樹一個 `w` 都拿不到。"""
    plan = generate_plan(THREE_WAY_SCHEME)
    entry = plan.by_id("repo-source-tree")
    writable = plan.all_writable_accounts(entry)
    job_accounts = {THREE_WAY_SCHEME.resolve(p) for p in JOB_PRINCIPALS}
    assert job_accounts == {"cortex-builder", "cortex-reviewer-planner"}
    assert not (job_accounts & writable), (sorted(job_accounts), sorted(writable))


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_source_tree_is_manager_owned_so_harvest_can_write_it(scheme) -> None:
    """0817 裁決：來源樹的 owner 是 Manager，不是 root。

    理由是實測——`git fetch` 必須把 `FETCH_HEAD` 寫進**目標 repo**，而成果回收正是
    「fetch 進來源樹」；provision 那半邊的 `git branch -f` 也是對來源樹的寫入：

        error: cannot open '.git/FETCH_HEAD': Permission denied

    「Manager 唯讀」與「Manager 回收成果」互斥，取後者。機械落點就是
    `owner_class=MANAGER_STATE`——ReadWritePaths 純由「誰可寫」導出，owner 換人的
    同一刻 Manager unit 就拿到這棵樹的寫入權。
    """
    plan = generate_plan(scheme)
    entry = plan.by_id("repo-source-tree")
    assert entry.owner_class is OwnerClass.MANAGER_STATE
    assert entry.owner == scheme.durable_state_owner
    assert plan.all_writable_accounts(entry) == {scheme.durable_state_owner}
    assert entry.is_directory is True
    # Manager-owned durable state 的基準：owner-only，跨帳號一律走精確唯讀 ACL。
    assert entry.mode == 0o700
    # deploy 帳號（root）不在可寫面裡——它在 OS 層本來就無所不能，登記表不為它發權限。
    assert scheme.deploy_account not in plan.all_writable_accounts(entry) or (
        scheme.deploy_account == scheme.durable_state_owner
    )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_accounts_get_read_only_acls_on_the_source_tree(scheme) -> None:
    """兩個 job 帳號拿到的是 `rX`——讀得到 clone 來源，一個 `w` 都沒有。"""
    plan = generate_plan(scheme)
    entry = plan.by_id("repo-source-tree")
    acl_by_account = {a.account: a.perms for a in entry.acls if not a.default}
    for principal in JOB_PRINCIPALS:
        account = scheme.resolve(principal)
        if account == entry.owner:
            continue  # 二分：reviewer／planner 與 Manager 併帳，owner 位已涵蓋
        assert acl_by_account.get(account) == "rX", (scheme.scheme_id, account)
    assert all("w" not in perms for perms in acl_by_account.values())


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_unit_can_write_the_source_tree_but_monitor_cannot(scheme) -> None:
    """Manager 需要寫（回收／provision）；monitor 只掃描，因此**不得**拿到寫入權。

    兩者同帳號（`cortex-manager`），檔案層權限相同——差別完全由 monitor unit 的
    persona 過濾產生（#622 建立的機制）。這條是那個機制第一次被「同一資產、兩種
    persona 結論不同」直接驗到。
    """
    source = DEFAULT_LAYOUT.asset_paths()["repo-source-tree"]
    manager = build_manager_unit(scheme, DEFAULT_LAYOUT)
    monitor = build_monitor_unit(scheme, DEFAULT_LAYOUT)
    assert any(_within(source, rwp) for rwp in manager.read_write_paths), (
        scheme.scheme_id, manager.read_write_paths,
    )
    assert not any(_within(source, rwp) for rwp in monitor.read_write_paths), (
        scheme.scheme_id, monitor.read_write_paths,
    )
    # 兩份 unit 都必須說明這棵樹在自己這側是什麼待遇，否則下一個人會手加一條 RWP。
    for unit in (manager, monitor):
        assert source in unit.content, unit.unit_name


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_stay_strictly_narrower_than_manager(scheme) -> None:
    """#622 的不變式不得被本 PR 動到——來源樹只進 Manager 那一側。"""
    manager = set(build_manager_unit(scheme, DEFAULT_LAYOUT).read_write_paths)
    monitor = set(build_monitor_unit(scheme, DEFAULT_LAYOUT).read_write_paths)
    assert monitor < manager, (sorted(monitor), sorted(manager))


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_template_unit_excludes_the_source_tree_but_keeps_its_own_clone(scheme) -> None:
    """job 側：來源樹唯讀（不在 RWP），clone 落點 `<worktree>/%i` 在 RWP 內。"""
    job_layout = DEFAULT_LAYOUT.with_job_segment("%i")
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    source = job_layout.asset_paths()["repo-source-tree"]
    clone = job_layout.asset_paths()["repo-worktree"]
    assert not any(_within(source, rwp) for rwp in unit.read_write_paths), (
        scheme.scheme_id, unit.read_write_paths,
    )
    assert any(_within(clone, rwp) for rwp in unit.read_write_paths), unit.read_write_paths
    # 兩個 root-owned 設定檔（hooks／gitconfig）同樣不可寫。
    for protected in (job_layout.gitconfig_of(unit.account),
                      job_layout.codex_hooks_dir_of(unit.account)):
        assert not any(_within(protected, rwp) for rwp in unit.read_write_paths), protected


# ---------------------------------------------------------------------------
# commit spool：`wx` 無 `r`（比照 review-verdict-spool 的既有測試）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_commit_spool_container_is_manager_owned_and_owner_only(scheme) -> None:
    plan = generate_plan(scheme)
    entry = plan.by_id("commit-spool")
    verdict = plan.by_id("review-verdict-spool")
    assert entry.owner == verdict.owner == scheme.durable_state_owner
    assert entry.owner_class is verdict.owner_class is OwnerClass.JOB
    assert entry.mode == verdict.mode == 0o700
    assert entry.is_directory is True


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_commit_spool_grants_producer_write_only_never_read(scheme) -> None:
    """單向語意：builder 寫得進自己那格，但讀不到別人的 bundle。"""
    plan = generate_plan(scheme)
    entry = plan.by_id("commit-spool")
    builder = scheme.resolve(Principal.BUILDER)
    assert builder in plan.all_writable_accounts(entry)
    for acl in entry.acls:
        assert "r" not in acl.perms, (scheme.scheme_id, acl.account, acl.perms)
        assert acl.perms == "wx", (scheme.scheme_id, acl.account, acl.perms)


def test_commit_spool_excludes_the_non_producing_personas() -> None:
    """三分下 reviewer／planner 帳號在本 spool 上完全沒有權限。"""
    plan = generate_plan(THREE_WAY_SCHEME)
    entry = plan.by_id("commit-spool")
    reviewer_planner = THREE_WAY_SCHEME.resolve(Principal.REVIEWER)
    assert reviewer_planner not in plan.all_writable_accounts(entry)
    assert reviewer_planner not in {a.account for a in entry.acls}


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_builder_can_reach_the_commit_spool(scheme) -> None:
    """葉節點 ACL 正確 ≠ 路徑走得通：整條鏈都要有 `--x`（#620／#624）。"""
    plan = generate_plan(scheme)
    builder = scheme.resolve(Principal.BUILDER)
    assert unreachable_hops(
        plan, scheme=scheme, account=builder, asset_id="commit-spool"
    ) == ()
    # 反向對照：拿掉導出的授權，coordinator 那一層就是斷的。
    assert unreachable_hops(
        plan, scheme=scheme, account=builder, asset_id="commit-spool", grants=()
    ) == (DEFAULT_LAYOUT.coordinator_root,)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_builder_job_unit_can_write_the_commit_spool(scheme) -> None:
    """回收通道要成立，builder 的模板 unit 必須寫得進 spool（RWP 機械導出）。"""
    unit = build_job_unit(scheme, DEFAULT_LAYOUT)
    spool = DEFAULT_LAYOUT.asset_paths()["commit-spool"]
    assert any(_within(spool, rwp) for rwp in unit.read_write_paths), (
        scheme.scheme_id, unit.read_write_paths,
    )
    assert spool in unit.content


# ---------------------------------------------------------------------------
# traverse 鏈（#624 的檢查沿用）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_every_reader_can_actually_reach_the_source_tree(scheme) -> None:
    """葉節點權限正確 ≠ 路徑走得通——每個 reader 的整條鏈都要通（#620／#624）。"""
    plan = generate_plan(scheme)
    readers = registry.asset_by_id("repo-source-tree").readers
    accounts = {scheme.resolve(p) for p in readers} - {None}
    assert accounts
    for account in sorted(accounts):
        blocked = unreachable_hops(
            plan, scheme=scheme, account=account, asset_id="repo-source-tree"
        )
        assert blocked == (), (scheme.scheme_id, account, blocked)
        assert account_can_reach(
            plan, scheme=scheme, account=account, asset_id="repo-source-tree"
        )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_source_tree_needs_no_traverse_acl_of_its_own(scheme) -> None:
    """父層是 root-owned 0755，因此不得產生任何指向來源樹**本身**的 traverse ACL。

    多產一條 `setfacl` 不會壞掉部署，但會讓「這棵樹是誰授權給誰」多一份真相。
    """
    plan = generate_plan(scheme)
    source = DEFAULT_LAYOUT.asset_paths()["repo-source-tree"]
    grants = permgen.derive_traverse_grants(plan, scheme=scheme)
    assert source not in {g.path for g in grants}


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_accounts_can_reach_their_own_gitconfig(scheme) -> None:
    plan = generate_plan(scheme)
    for principal, asset_id in GITCONFIG_CASES:
        entry = plan.by_id(asset_id)
        assert entry.owner_class is OwnerClass.DEPLOYMENT
        assert entry.owner == "root"
        assert entry.is_directory is False
        assert entry.mode == permgen.ACCOUNT_GITCONFIG_MODE == 0o644
        assert plan.all_writable_accounts(entry) == {"root"}
        assert scheme.resolve(principal) not in plan.all_writable_accounts(entry)


def test_permission_commands_cover_the_new_assets() -> None:
    """runbook 引用形式：新資產都出現在命令輸出，且輸出仍只有字串命令。"""
    lines = permgen.plan_to_commands(
        generate_plan(THREE_WAY_SCHEME), path_of=permgen.asset_paths(),
        scheme=THREE_WAY_SCHEME,
    )
    joined = "\n".join(lines)
    for asset_id in NEW_ASSET_IDS:
        assert asset_id in joined, asset_id
    executable = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    # 來源樹：Manager 擁有、owner-only，兩個 job 帳號各一條唯讀 ACL。
    assert "install -d /var/lib/cortex/repos" in executable
    assert "chown cortex-manager:cortex-manager /var/lib/cortex/repos" in executable
    assert "chmod 0700 /var/lib/cortex/repos" in executable
    assert "setfacl -m u:cortex-builder:rX /var/lib/cortex/repos" in executable
    assert (
        "setfacl -m u:cortex-reviewer-planner:rX /var/lib/cortex/repos" in executable
    )
    # commit spool：`wx` 無 `r`，且只給 builder。
    assert (
        "setfacl -m u:cortex-builder:wx /var/lib/cortex/coordinator/commit-spool"
        in executable
    )
    assert not [
        ln for ln in executable
        if "commit-spool" in ln and "cortex-reviewer-planner" in ln
    ]
    # 葉檔守衛：`.gitconfig` 在 setup 當下多半還沒落檔，不得讓 `sh -e` 中止整份 script。
    for home in ("cortex-builder", "cortex-reviewer-planner", "cortex-manager"):
        assert (
            f"[ ! -e /var/lib/{home}/.gitconfig ] || "
            f"chown root:root /var/lib/{home}/.gitconfig"
        ) in executable
    # 自我檢查：輸出裡沒有任何未宣告的帳號名（#626 的不變式）。
    assert permgen.unknown_accounts_in(lines, THREE_WAY_SCHEME) == ()


# ---------------------------------------------------------------------------
# `.gitconfig` 的內容產生（比照 shim／polkit：內容也由 permgen 出）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("principal, asset_id", GITCONFIG_CASES)
def test_gitconfig_content_is_generated_and_lands_on_the_registry_path(
    principal, asset_id,
) -> None:
    """內容由產生器出，落點與登記表資產逐字對齊（三分＝定案方案）。"""
    blob = build_account_gitconfig(THREE_WAY_SCHEME, LAYOUT, principal)
    assert blob.install_path == LAYOUT.asset_paths()[asset_id]
    assert blob.account == THREE_WAY_SCHEME.resolve(principal)
    assert blob.owner == "root" and blob.group == "root"
    assert blob.mode == 0o644
    assert permgen.ACCOUNT_GITCONFIG_ASSETS[principal] == asset_id
    # 內容：逐字的 safe.directory，**工作樹根 ＋ `<root>/.git` 兩條**。
    assert blob.safe_directories == (
        "/var/lib/cortex/repos/paulsha-cortex",
        "/var/lib/cortex/repos/paulsha-cortex/.git",
    )
    assert "[safe]" in blob.content
    assert "\tdirectory = /var/lib/cortex/repos/paulsha-cortex\n" in blob.content
    assert "\tdirectory = /var/lib/cortex/repos/paulsha-cortex/.git\n" in blob.content
    # 安裝命令仍只是字串。
    assert blob.commands() == [
        f"chown root:root {blob.install_path}",
        f"chmod 0644 {blob.install_path}",
    ]


@pytest.mark.parametrize("principal, _asset_id", GITCONFIG_CASES)
def test_gitconfig_covers_both_the_worktree_root_and_its_dot_git(
    principal, _asset_id,
) -> None:
    """實測：從**非 bare** 來源 clone 時，git 檢查的是 `<repo>/.git` 而不是工作樹根。

        fatal: detected dubious ownership in repository at
               '/var/lib/cortex/repos/paulsha-cortex/.git'

    而 `git -C <repo> rev-parse`／`fetch` 報的又是工作樹根。`safe.directory` 只認逐字
    相等的值，因此兩個位置就是兩條——只給一條會讓另一半的操作在完全不同的時機才失敗。
    """
    blob = build_account_gitconfig(THREE_WAY_SCHEME, LAYOUT, principal)
    values = [
        ln.split("=", 1)[1].strip()
        for ln in blob.content.splitlines()
        if ln.strip().startswith("directory")
    ]
    for root in LAYOUT.source_repo_paths():
        assert root in values, (principal, root, values)
        assert f"{root}/.git" in values, (principal, root, values)
    assert len(values) == 2 * len(LAYOUT.source_repo_paths())


def test_layout_derives_both_safe_directory_entries() -> None:
    """兩條值的推導在 layout，而不是散在三個產生器裡。"""
    assert LAYOUT.source_repo_safe_directories() == (
        "/var/lib/cortex/repos/paulsha-cortex",
        "/var/lib/cortex/repos/paulsha-cortex/.git",
    )
    assert DEFAULT_LAYOUT.source_repo_safe_directories() == ()


def test_gitconfig_uses_literal_paths_not_a_wildcard() -> None:
    """git 的 safe.directory 不吃目錄萬用字元（實測 git 2.43），字面 `*` 又等於

    對該帳號整個關掉 dubious-ownership 保護——兩者都不得出現在產出裡。
    """
    content = build_account_gitconfig(THREE_WAY_SCHEME, LAYOUT).content
    directives = [
        ln.strip() for ln in content.splitlines()
        if ln.strip().startswith("directory")
    ]
    assert directives == [
        "directory = /var/lib/cortex/repos/paulsha-cortex",
        "directory = /var/lib/cortex/repos/paulsha-cortex/.git",
    ]
    for value in directives:
        assert not value.endswith("*"), value


def test_gitconfig_fails_closed_without_a_declared_source_repo() -> None:
    """未宣告來源 repo 時一行都不產：空的 `[safe]` 段會讓每次 git 操作才失敗。"""
    with pytest.raises(UnresolvedSourceRepoError) as exc:
        build_account_gitconfig(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
    message = str(exc.value)
    assert "--source-repo" in message
    assert "PSC_SOURCE_REPO_SLUGS" in message
    assert DEFAULT_LAYOUT.repo_source_root in message


@pytest.mark.parametrize("bad", ["../etc", "a b", "x;rm -rf /", "$(id -u)", ".hidden", ""])
def test_source_repo_slugs_must_look_like_a_path_segment(bad) -> None:
    """slug 會被逐字寫進 root 產生的檔案——形狀在產生階段就驗，不等到落檔。"""
    with pytest.raises(ValueError):
        DEFAULT_LAYOUT.with_source_repo_slugs((bad,))


def test_multiple_source_repos_are_all_declared() -> None:
    layout = DEFAULT_LAYOUT.with_source_repo_slugs(("alpha", "beta"))
    blob = build_account_gitconfig(THREE_WAY_SCHEME, layout)
    assert blob.safe_directories == (
        "/var/lib/cortex/repos/alpha", "/var/lib/cortex/repos/alpha/.git",
        "/var/lib/cortex/repos/beta", "/var/lib/cortex/repos/beta/.git",
    )
    assert blob.content.count("directory = ") == 4


def test_gitconfig_is_deterministic_and_strings_only() -> None:
    a = build_account_gitconfig(THREE_WAY_SCHEME, LAYOUT)
    b = build_account_gitconfig(THREE_WAY_SCHEME, LAYOUT)
    assert a.content == b.content
    assert a.to_dict() == b.to_dict()
    assert isinstance(a.content, str) and a.content


def test_two_way_scheme_moves_the_gitconfig_off_the_registry_path() -> None:
    """已記錄的不對稱：二分把 reviewer／planner／Manager 併成同一個 `cortex-svc`。

    `asset_paths()` 刻意不吃 scheme（既有設計），取的是**定案的三分**帳號；二分是向後
    相容選項，其 reviewer／planner 尚未經模板 unit 降權起 job，本資產不適用於那個形態。
    builder 兩案相同，因此不受影響。
    """
    assert build_account_gitconfig(
        TWO_WAY_SCHEME, LAYOUT, Principal.BUILDER
    ).install_path == LAYOUT.asset_paths()["builder-gitconfig"]
    for principal, asset_id in (
        (Principal.REVIEWER, "reviewer-planner-gitconfig"),
        (Principal.MANAGER, "manager-gitconfig"),
    ):
        two_way = build_account_gitconfig(TWO_WAY_SCHEME, LAYOUT, principal)
        assert two_way.account == TWO_WAY_SCHEME.durable_state_owner
        assert two_way.install_path != LAYOUT.asset_paths()[asset_id]


def test_generators_still_touch_no_filesystem() -> None:
    """靜態保證：新增的 gitconfig 產生器仍不含任何 IO 或執行面。"""
    src = inspect.getsource(permgen)
    for forbidden in ("subprocess", "os.system", "os.chown", "os.chmod",
                      "open(", "write_text", "shutil"):
        assert forbidden not in src, forbidden


# ---------------------------------------------------------------------------
# CLI（runbook 的落檔來源）
# ---------------------------------------------------------------------------

def test_cli_emits_the_gitconfig(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gitconfig", "three-way", "--builder", "--source-repo", SOURCE_SLUG]) == 0
    out = capsys.readouterr().out
    assert out == build_account_gitconfig(
        permgen.THREE_WAY_SCHEME, LAYOUT, Principal.BUILDER
    ).content
    assert "/var/lib/cortex-builder/.gitconfig" in out

    assert main([
        "gitconfig", "--reviewer-planner", f"--source-repo={SOURCE_SLUG}",
    ]) == 0
    assert "/var/lib/cortex-reviewer-planner/.gitconfig" in capsys.readouterr().out


def test_cli_emits_the_manager_gitconfig(capsys: pytest.CaptureFixture[str]) -> None:
    """本票初版缺的就是這一份——實機複驗時 Manager 的每一個 git 操作全部失效。"""
    assert main(["gitconfig", "three-way", "--manager", "--source-repo", SOURCE_SLUG]) == 0
    out = capsys.readouterr().out
    assert out == build_account_gitconfig(
        permgen.THREE_WAY_SCHEME, LAYOUT, Principal.MANAGER
    ).content
    assert "/var/lib/cortex-manager/.gitconfig" in out
    assert out.count("directory = ") == 2


def test_cli_reads_slugs_from_env_when_no_flag_is_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PSC_SOURCE_REPO_SLUGS", "alpha,beta")
    assert main(["gitconfig", "three-way", "--builder"]) == 0
    out = capsys.readouterr().out
    assert "directory = /var/lib/cortex/repos/alpha" in out
    assert "directory = /var/lib/cortex/repos/beta" in out


def test_cli_flag_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PSC_SOURCE_REPO_SLUGS", "from-env")
    assert main(["gitconfig", "--source-repo", "from-flag"]) == 0
    out = capsys.readouterr().out
    assert "from-flag" in out
    assert "from-env" not in out


@pytest.mark.parametrize(
    "argv",
    [
        ["gitconfig", "three-way", "--builder"],            # 未宣告來源 repo
        ["gitconfig", "three-way", "--manager"],            # 同上，Manager 那份
        ["gitconfig", "--source-repo"],                     # 旗標缺值
        ["gitconfig", "--source-repo", "../etc"],           # slug 形狀不合法
        ["gitconfig", "--nope"],                            # 未知旗標
    ],
)
def test_cli_fail_closed_prints_nothing_to_stdout(
    argv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """重導成檔案時必須是**空檔**，而不是一份看起來裝好、實際擋掉所有操作的設定。"""
    monkeypatch.delenv("PSC_SOURCE_REPO_SLUGS", raising=False)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
