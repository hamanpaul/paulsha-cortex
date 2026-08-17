"""#623：per-job clone 的信任根層——來源樹資產 ＋ job 帳號的 root-owned `.gitconfig`。

背景（#623 實測，已由 operator 裁決）：`git worktree` 的**共用 object store** 與三分
隔離互斥——builder 要 commit 就得能寫 object store，能寫就等於邊界在 git 這一層漏掉。
因此 job 工作區改為 **per-job 完整 clone**（0.5 秒／35MB），來源是一份 Manager-owned
樹內的 **working checkout**（monitor 要掃工作樹裡的檔案，bare 沒有工作樹）。

本檔釘住信任根層的四件事：

1. `repo-source-tree` 在登記表裡，且**兩個 job 帳號唯讀**——計畫一個 `w` 都不給；
2. 同一條唯讀也套在 **Manager／monitor** 身上（裁決：來源樹由 operator 以 root 更新，
   攻擊面最小）——機械落點是 owner_class＝DEPLOYMENT，因此 unit 的 ReadWritePaths
   （純由「誰可寫」導出）不會涵蓋它；
3. traverse 鏈完整（沿用 #624 的 `unreachable_hops()`）——葉節點權限對 ≠ 路徑走得通；
4. `.gitconfig` 是 root-owned、job 不可寫，且**內容**由 permgen 產生（比照 shim／
   polkit），未宣告來源 repo 時 fail-closed。
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
    build_job_gitconfig,
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


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


# ---------------------------------------------------------------------------
# 登記表：三項新資產的分類
# ---------------------------------------------------------------------------

def test_source_tree_is_registered_and_fully_classified() -> None:
    asset = registry.asset_by_id("repo-source-tree")
    assert asset.tier is AssetTier.TIER_0
    assert asset.tree is TrustTree.MANAGER_OWNED
    assert asset.ingress_kind is IngressKind.DEPLOYMENT_WRITE
    # writer 只有部署身分：來源樹由 operator 以 root 更新（裁決）。
    assert asset.writers == (Principal.INSTALLER,)
    # reader 面要涵蓋 Manager（fetch／解析）、monitor（掃描）與三個 headless persona
    # （clone 來源）——#624 的 traverse 鏈就是由 reader 宣告機械補出來的。
    assert set(asset.readers) >= {Principal.MANAGER, Principal.MONITOR, *JOB_PRINCIPALS}
    # 容器沒有 path_resolver：程式碼解析的是單一 repo（PSC_REPO_ROOT → <此樹>/<slug>）。
    assert asset.path_resolver is None


@pytest.mark.parametrize(
    "asset_id, readers",
    [
        ("builder-gitconfig", {Principal.BUILDER}),
        ("reviewer-planner-gitconfig", {Principal.REVIEWER, Principal.PLANNER}),
    ],
)
def test_gitconfig_assets_mirror_the_codex_hooks_shape(asset_id, readers) -> None:
    """比照既有的 `codex-hooks`：root-owned、落在 job 帳號 HOME 下、enforcement plane。"""
    asset = registry.asset_by_id(asset_id)
    hooks = registry.asset_by_id("codex-hooks")
    assert asset.tier is hooks.tier is AssetTier.TIER_0
    assert asset.tree is hooks.tree is TrustTree.MANAGER_OWNED
    assert asset.ingress_kind is hooks.ingress_kind is IngressKind.DEPLOYMENT_WRITE
    assert asset.writers == (Principal.INSTALLER,)
    assert set(asset.readers) == readers


def test_new_assets_introduce_no_new_deployment_decision() -> None:
    """#626 的 fail-closed 面：新資產只用得到已可解析的 principal。

    多一個沒對應的 principal 就會讓整個 `--commands` fail-closed；這條在登記表側先擋。
    """
    used: set[Principal] = set()
    for asset_id in ("repo-source-tree", "builder-gitconfig", "reviewer-planner-gitconfig"):
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
    # `.gitconfig` 落在各自 HOME 下，與 `~/.codex` 同一層。
    assert paths["builder-gitconfig"] == "/var/lib/cortex-builder/.gitconfig"
    assert paths["reviewer-planner-gitconfig"] == (
        "/var/lib/cortex-reviewer-planner/.gitconfig"
    )


def test_source_tree_paths_follow_a_relocated_layout() -> None:
    """路徑由 `agents_root` 導出，不得寫死——換部署位置不必改產生器一行程式碼。"""
    alt = PathLayout(agents_root="/srv/cx", worktree_root="/srv/cx/wt",
                     deploy_root="/srv/deploy", instance="cx", home_root="/srv/accounts")
    paths = alt.asset_paths()
    assert paths["repo-source-tree"] == "/srv/cx/repos"
    assert paths["builder-gitconfig"] == "/srv/accounts/cortex-builder/.gitconfig"
    assert alt.with_source_repo_slugs(("x",)).source_repo_paths() == ("/srv/cx/repos/x",)


def test_with_job_segment_preserves_the_new_layout_fields() -> None:
    """job layout 是由 setup layout 換 segment 得來的——欄位不得在中途被重設。

    逐欄位重建的舊寫法會讓每個新欄位都靜默掉回預設值，job unit 因此指到另一棵樹。
    """
    job_layout = LAYOUT.with_job_segment("%i")
    assert job_layout.source_repo_slugs == LAYOUT.source_repo_slugs
    assert job_layout.reviewer_planner_account == LAYOUT.reviewer_planner_account
    assert job_layout.repo_source_root == LAYOUT.repo_source_root
    assert job_layout.job_segment == "%i"


# ---------------------------------------------------------------------------
# 來源樹：兩個 job 帳號唯讀（本 PR 的核心驗收）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_accounts_get_no_write_on_the_source_tree(scheme) -> None:
    """驗收：產生的計畫不得給兩個 job 帳號 `w`——一個位元都不行。"""
    plan = generate_plan(scheme)
    entry = plan.by_id("repo-source-tree")
    writable = plan.all_writable_accounts(entry)
    for principal in JOB_PRINCIPALS:
        account = scheme.resolve(principal)
        assert account not in writable, (scheme.scheme_id, principal, sorted(writable))
    # ACL 面也不得有任何授寫條目（跨帳號授權在此類一律唯讀）。
    assert not [a for a in entry.acls if a.writable]
    # group／other 無寫入位（spec §R2）。
    assert not entry.mode & 0o022, entry.mode_str


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_source_tree_is_root_owned_so_every_service_account_is_read_only(scheme) -> None:
    """裁決：來源樹由 operator 以 root 更新、Manager 唯讀。

    機械落點就是 owner_class＝DEPLOYMENT：ReadWritePaths 純由「誰可寫」導出，若 owner
    是 `cortex-manager`，Manager unit 會**自動**拿到這棵樹的寫入權——「Manager 唯讀」
    與「owner＝Manager」在本產生器裡互斥，取前者。
    """
    plan = generate_plan(scheme)
    entry = plan.by_id("repo-source-tree")
    assert entry.owner_class is OwnerClass.DEPLOYMENT
    assert entry.owner == scheme.deploy_account == "root"
    assert plan.all_writable_accounts(entry) == {"root"}
    assert scheme.durable_state_owner not in plan.all_writable_accounts(entry)
    assert entry.is_directory is True
    assert entry.mode == 0o755


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_and_monitor_units_can_read_but_never_write_the_source_tree(scheme) -> None:
    """`ProtectSystem=strict` 下唯讀是預設，因此「讀得到」不需要任何指令；

    「寫不進去」則要求 ReadWritePaths **不涵蓋**它——這條就是取捨的可驗證形式。
    """
    source = DEFAULT_LAYOUT.asset_paths()["repo-source-tree"]
    for unit in (build_manager_unit(scheme, DEFAULT_LAYOUT),
                 build_monitor_unit(scheme, DEFAULT_LAYOUT)):
        assert not any(_within(source, rwp) for rwp in unit.read_write_paths), (
            scheme.scheme_id, unit.unit_name, unit.read_write_paths,
        )
        # 但兩份 unit 都必須說明「這棵樹在這裡是唯讀的」，否則下一個人會手加一條 RWP。
        assert source in unit.content, unit.unit_name


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_monitor_read_write_paths_stay_strictly_narrower_than_manager(scheme) -> None:
    """#622 的不變式不得被本 PR 動到（新資產對兩者都不可寫 ⇒ 兩邊都不變）。"""
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
    """鏈通得靠 root-owned 0755，不是靠 ACL——因此不得產生任何指向來源樹的 ACL。

    多產一條 `setfacl` 不會壞掉部署，但會讓「這棵樹是誰授權給誰」多一份真相。
    """
    plan = generate_plan(scheme)
    source = DEFAULT_LAYOUT.asset_paths()["repo-source-tree"]
    grants = permgen.derive_traverse_grants(plan, scheme=scheme)
    assert source not in {g.path for g in grants}
    assert plan.by_id("repo-source-tree").acls == ()


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_accounts_can_reach_their_own_gitconfig(scheme) -> None:
    plan = generate_plan(scheme)
    for asset_id, principal in (
        ("builder-gitconfig", Principal.BUILDER),
        ("reviewer-planner-gitconfig", Principal.REVIEWER),
    ):
        entry = plan.by_id(asset_id)
        assert entry.owner_class is OwnerClass.DEPLOYMENT
        assert entry.owner == "root"
        assert entry.is_directory is False
        assert entry.mode == permgen.JOB_GITCONFIG_MODE == 0o644
        assert plan.all_writable_accounts(entry) == {"root"}
        assert scheme.resolve(principal) not in plan.all_writable_accounts(entry)


def test_permission_commands_cover_the_new_assets() -> None:
    """runbook 引用形式：三項新資產都出現在命令輸出，且輸出仍只有字串命令。"""
    lines = permgen.plan_to_commands(
        generate_plan(THREE_WAY_SCHEME), path_of=permgen.asset_paths(),
        scheme=THREE_WAY_SCHEME,
    )
    joined = "\n".join(lines)
    for asset_id in ("repo-source-tree", "builder-gitconfig", "reviewer-planner-gitconfig"):
        assert asset_id in joined, asset_id
    executable = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    assert "install -d /var/lib/cortex/repos" in executable
    assert "chown root:root /var/lib/cortex/repos" in executable
    assert "chmod 0755 /var/lib/cortex/repos" in executable
    # 葉檔守衛：`.gitconfig` 在 setup 當下多半還沒落檔，不得讓 `sh -e` 中止整份 script。
    assert (
        "[ ! -e /var/lib/cortex-builder/.gitconfig ] || "
        "chown root:root /var/lib/cortex-builder/.gitconfig"
    ) in executable
    # 自我檢查：輸出裡沒有任何未宣告的帳號名（#626 的不變式）。
    assert permgen.unknown_accounts_in(lines, THREE_WAY_SCHEME) == ()


# ---------------------------------------------------------------------------
# `.gitconfig` 的內容產生（比照 shim／polkit：內容也由 permgen 出）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "principal, asset_id",
    [
        (Principal.BUILDER, "builder-gitconfig"),
        (Principal.REVIEWER, "reviewer-planner-gitconfig"),
    ],
)
def test_gitconfig_content_is_generated_and_lands_on_the_registry_path(
    principal, asset_id,
) -> None:
    """內容由產生器出，落點與登記表資產逐字對齊（三分＝定案方案）。"""
    blob = build_job_gitconfig(THREE_WAY_SCHEME, LAYOUT, principal)
    assert blob.install_path == LAYOUT.asset_paths()[asset_id]
    assert blob.account == THREE_WAY_SCHEME.resolve(principal)
    assert blob.owner == "root" and blob.group == "root"
    assert blob.mode == 0o644
    assert permgen.JOB_GITCONFIG_ASSETS[principal] == asset_id
    # 內容：逐字的 safe.directory，指向來源樹底下那一格。
    assert blob.safe_directories == ("/var/lib/cortex/repos/paulsha-cortex",)
    assert "[safe]" in blob.content
    assert "\tdirectory = /var/lib/cortex/repos/paulsha-cortex\n" in blob.content
    # 安裝命令仍只是字串。
    assert blob.commands() == [
        f"chown root:root {blob.install_path}",
        f"chmod 0644 {blob.install_path}",
    ]


def test_gitconfig_uses_literal_paths_not_a_wildcard() -> None:
    """git 的 safe.directory 不吃目錄萬用字元（實測 git 2.43），字面 `*` 又等於

    對該帳號整個關掉 dubious-ownership 保護——兩者都不得出現在產出裡。
    """
    content = build_job_gitconfig(THREE_WAY_SCHEME, LAYOUT).content
    directives = [
        ln.strip() for ln in content.splitlines()
        if ln.strip().startswith("directory")
    ]
    assert directives == ["directory = /var/lib/cortex/repos/paulsha-cortex"]
    for value in directives:
        assert not value.endswith("*"), value


def test_gitconfig_fails_closed_without_a_declared_source_repo() -> None:
    """未宣告來源 repo 時一行都不產：空的 `[safe]` 段會讓每個 job 在 clone 才失敗。"""
    with pytest.raises(UnresolvedSourceRepoError) as exc:
        build_job_gitconfig(THREE_WAY_SCHEME, DEFAULT_LAYOUT)
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
    blob = build_job_gitconfig(THREE_WAY_SCHEME, layout)
    assert blob.safe_directories == (
        "/var/lib/cortex/repos/alpha", "/var/lib/cortex/repos/beta",
    )
    assert blob.content.count("directory = ") == 2


def test_gitconfig_is_deterministic_and_strings_only() -> None:
    a = build_job_gitconfig(THREE_WAY_SCHEME, LAYOUT)
    b = build_job_gitconfig(THREE_WAY_SCHEME, LAYOUT)
    assert a.content == b.content
    assert a.to_dict() == b.to_dict()
    assert isinstance(a.content, str) and a.content


def test_two_way_scheme_moves_the_gitconfig_off_the_registry_path() -> None:
    """已記錄的不對稱：二分把 reviewer／planner 併進 Manager 帳號。

    `asset_paths()` 刻意不吃 scheme（既有設計），取的是**定案的三分**帳號；二分是向後
    相容選項，其 reviewer／planner 尚未經模板 unit 降權起 job，本資產不適用於那個形態。
    builder 兩案相同，因此不受影響。
    """
    assert build_job_gitconfig(
        TWO_WAY_SCHEME, LAYOUT, Principal.BUILDER
    ).install_path == LAYOUT.asset_paths()["builder-gitconfig"]
    two_way_rp = build_job_gitconfig(TWO_WAY_SCHEME, LAYOUT, Principal.REVIEWER)
    assert two_way_rp.account == TWO_WAY_SCHEME.durable_state_owner
    assert two_way_rp.install_path != LAYOUT.asset_paths()["reviewer-planner-gitconfig"]


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
    assert out == build_job_gitconfig(
        permgen.THREE_WAY_SCHEME, LAYOUT, Principal.BUILDER
    ).content
    assert "/var/lib/cortex-builder/.gitconfig" in out

    assert main([
        "gitconfig", "--reviewer-planner", f"--source-repo={SOURCE_SLUG}",
    ]) == 0
    assert "/var/lib/cortex-reviewer-planner/.gitconfig" in capsys.readouterr().out


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
        ["gitconfig", "--source-repo"],                     # 旗標缺值
        ["gitconfig", "--source-repo", "../etc"],           # slug 形狀不合法
        ["gitconfig", "--nope"],                            # 未知旗標
    ],
)
def test_cli_fail_closed_prints_nothing_to_stdout(
    argv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """重導成檔案時必須是**空檔**，而不是一份看起來裝好、實際擋掉所有 job 的設定。"""
    monkeypatch.delenv("PSC_SOURCE_REPO_SLUGS", raising=False)
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip()
