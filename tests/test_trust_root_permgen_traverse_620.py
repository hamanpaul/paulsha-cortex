"""#620：父目錄 traverse ACL 的機械導出。

Phase 2b 實機症狀：葉節點的跨帳號 ACL 全部正確（`u:cortex-builder:wx` 掛在
`<monitor>/event-spool` 上），但父目錄是 `0700 cortex-manager`，POSIX 要求路徑上
**每一層**都有 `x`（search）位，於是 builder 寫 event-spool、reviewer-planner 寫
review-verdicts 兩條正向路徑全部 `Permission denied`——而錯誤訊息指的是父目錄，
與真正缺的那條授權不在同一層。

驗收（對應 issue 的四條）：
- (a) 產生器輸出包含必要的父層 traverse ACL（實機手補的那三條逐字出現）；
- (b) 產生的是 `--x` 不是 `r-x`——只給 traverse，列目錄仍須被拒；
- (c) 已允許該帳號 traverse 的中間層不重複產生；
- (d) 三分方案下 builder→event-spool、reviewer-planner→review-verdicts、
      builder→job-specs 三條路徑的**完整鏈**每一層都有授權。
"""
from __future__ import annotations

import pytest

from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import (
    DEFAULT_LAYOUT,
    TRAVERSE_PERMS,
    PathLayout,
    account_can_reach,
    can_traverse,
    derive_traverse_grants,
    directory_facts,
    generate_plan,
    unreachable_hops,
)
from paulsha_cortex.trust_root.permgen import THREE_WAY_SCHEME as _THREE_WAY_BASE
from paulsha_cortex.trust_root.permgen import TWO_WAY_SCHEME as _TWO_WAY_BASE
from paulsha_cortex.trust_root.registry import (
    ASSET_REGISTRY,
    AssetTier,
    IngressKind,
    Principal,
    TrustRootAsset,
    TrustTree,
)

#: #626：`operator` 與 `external` 是**部署決定**，模組層方案刻意留 `None`，未注入時
#: 產生器 fail-closed。traverse 推導的驗收本來就涵蓋這兩個帳號的中間層授權
#: （`cortex-outbox` 對 `coordinator`／`coordinator/digest`、operator 對 `coordinator`），
#: 故本檔一律用**注入後**的方案；fail-closed 本身由
#: `tests/test_trust_root_principal_account_mapping_626.py` 專門釘住。
DEPLOYMENT_ACCOUNTS = {
    Principal.OPERATOR: "cortex-ops",
    Principal.EXTERNAL: "cortex-outbox",
}
TWO_WAY_SCHEME = _TWO_WAY_BASE.with_principal_accounts(DEPLOYMENT_ACCOUNTS)
THREE_WAY_SCHEME = _THREE_WAY_BASE.with_principal_accounts(DEPLOYMENT_ACCOUNTS)

ALL_SCHEMES = [TWO_WAY_SCHEME, THREE_WAY_SCHEME]

#: 三分下的三條正向路徑：(persona, 葉資產)。issue 驗收的核心就是這三條走得通。
FORWARD_PATHS = (
    (Principal.BUILDER, "monitor-event-spool"),
    (Principal.REVIEWER, "review-verdict-spool"),
    (Principal.BUILDER, "job-spec-spool"),
)


def _commands(scheme=THREE_WAY_SCHEME) -> list[str]:
    return permgen.plan_to_commands(
        generate_plan(scheme), path_of=permgen.asset_paths(), scheme=scheme
    )


def _executable(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# (a) 產生器輸出包含必要的父層 traverse ACL
# ---------------------------------------------------------------------------

def test_generator_emits_the_three_manually_patched_acls() -> None:
    """Phase 2b 實機用來解封的那三條，必須由產生器導出、逐字出現在可執行行裡。"""
    emitted = _executable(_commands())
    for expected in (
        "setfacl -m u:cortex-builder:--x /var/lib/cortex/monitor",
        "setfacl -m u:cortex-builder:--x /var/lib/cortex/coordinator",
        "setfacl -m u:cortex-reviewer-planner:--x /var/lib/cortex/coordinator",
    ):
        assert expected in emitted, expected


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_every_cross_account_acl_gets_its_traverse_chain(scheme) -> None:
    """涵蓋等式：**每一個**授了跨帳號 ACL 的資產，該帳號都走得到（不只那三條）。"""
    plan = generate_plan(scheme)
    for entry in plan.entries:
        for acl in entry.acls:
            if acl.default or acl.account == entry.owner:
                continue
            blocked = unreachable_hops(
                plan, scheme=scheme, account=acl.account, asset_id=entry.asset_id
            )
            assert not blocked, (entry.asset_id, acl.account, blocked)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_without_the_derived_grants_the_paths_are_broken(scheme) -> None:
    """反向對照：拿掉導出的授權，實機那兩條正向路徑就是 issue 描述的斷法。

    沒有這條，(d) 可能只是在測「本來就通」的東西。
    """
    plan = generate_plan(scheme)
    builder = scheme.resolve(Principal.BUILDER)
    blocked = unreachable_hops(
        plan, scheme=scheme, account=builder, asset_id="monitor-event-spool", grants=()
    )
    assert blocked == (f"{DEFAULT_LAYOUT.monitor_state_root}",)
    blocked = unreachable_hops(
        plan, scheme=scheme, account=builder, asset_id="job-spec-spool", grants=()
    )
    assert blocked == (f"{DEFAULT_LAYOUT.coordinator_root}",)


# ---------------------------------------------------------------------------
# (b) `--x` 不是 `r-x`：只給 traverse，不給列目錄
# ---------------------------------------------------------------------------

def test_traverse_perms_are_search_only() -> None:
    assert TRAVERSE_PERMS == "--x"
    assert "r" not in TRAVERSE_PERMS


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_no_traverse_grant_confers_read(scheme) -> None:
    """每條導出的授權都是 `--x`——`r-x` 會讓 job 帳號列得出 coordinator/ 底下有哪些
    Manager 資產，那是 issue 明確要求不得發生的事。"""
    for grant in derive_traverse_grants(generate_plan(scheme), scheme=scheme):
        assert grant.acl.perms == TRAVERSE_PERMS
        assert "r" not in grant.acl.perms and "w" not in grant.acl.perms
        assert not grant.acl.writable
        assert grant.render().startswith(f"setfacl -m u:{grant.account}:{TRAVERSE_PERMS} ")


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_job_accounts_still_cannot_list_the_coordinator_tree(scheme) -> None:
    """`ls /var/lib/cortex/coordinator` 對 job 帳號仍須是 Permission denied：
    coordinator 自身 mode 不給 other 讀，且 job 帳號在其上只有 `--x`。"""
    plan = generate_plan(scheme)
    facts = directory_facts(plan, scheme=scheme)[DEFAULT_LAYOUT.coordinator_root]
    assert facts.mode & 0o007 == 0, "coordinator 不得對 other 開任何位"
    assert facts.owner == scheme.durable_state_owner
    job_accounts = scheme.headless_accounts() - {facts.owner}
    assert job_accounts
    grants = {
        (g.path, g.account): g
        for g in derive_traverse_grants(plan, scheme=scheme)
    }
    for account in job_accounts:
        # 目錄本身沒給該帳號任何讀取條目……
        assert account not in facts.acl_perms
        # ……導出的授權也只有 traverse，沒有 r。
        grant = grants.get((DEFAULT_LAYOUT.coordinator_root, account))
        if grant is not None:
            assert "r" not in grant.acl.perms
        assert not can_traverse(
            permgen.DirectoryFacts(
                path=facts.path, owner=facts.owner, group=facts.group,
                mode=facts.mode & 0o770,  # 只留 owner/group，模擬「沒有 ACL」
            ),
            account,
            scheme,
        )


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_traverse_grants_never_emit_a_default_acl(scheme) -> None:
    """default ACL 會讓該目錄底下**新建的每個物件**繼承這條授權，等於把一條
    traverse 放大成整棵子樹的授權——與目的正好相反，故一條都不能有。"""
    for grant in derive_traverse_grants(generate_plan(scheme), scheme=scheme):
        assert grant.acl.default is False
        assert " -d " not in grant.render()
    section = _commands(scheme)
    tail = section[section.index([
        ln for ln in section if "父目錄 traverse ACL" in ln
    ][0]):]
    assert not any("setfacl -d" in ln for ln in tail), "traverse 節不得出現 default ACL"


# ---------------------------------------------------------------------------
# (c) 已允許 traverse 的目錄不重複產生
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_already_traversable_directories_are_skipped(scheme) -> None:
    """三種「本來就走得過去」的情形都不得重複產生：
    root-owned 0755、others 帶 x（worktree pool 0701）、以及既有 ACL 已含 x。"""
    grants = derive_traverse_grants(generate_plan(scheme), scheme=scheme)
    paths = {g.path for g in grants}
    # root-owned 0755（樹根與骨架中間層）。
    assert DEFAULT_LAYOUT.agents_root not in paths
    assert f"{DEFAULT_LAYOUT.agents_root}/run" not in paths
    assert f"{DEFAULT_LAYOUT.agents_root}/config" not in paths
    # 0701：others 只 traverse、不可列目錄——已足夠，不必再補。
    assert DEFAULT_LAYOUT.worktree_root not in paths
    # 既有 `rX` ACL 已含 traverse（operator 讀 registry／control 底下的葉檔）。
    operator = scheme.resolve(Principal.OPERATOR)
    assert (DEFAULT_LAYOUT.skill_registry_root, operator) not in {
        (g.path, g.account) for g in grants
    }
    assert (DEFAULT_LAYOUT.control_root, operator) not in {
        (g.path, g.account) for g in grants
    }


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_grants_are_deduplicated_and_deterministic(scheme) -> None:
    """同一個中間層服務多個葉時只產生一條（reason 合併），且順序決定性。"""
    plan = generate_plan(scheme)
    grants = derive_traverse_grants(plan, scheme=scheme)
    keys = [(g.path, g.account) for g in grants]
    assert len(keys) == len(set(keys)), "同一 (path, account) 不得重複產生"
    assert keys == sorted(keys)
    assert grants == derive_traverse_grants(plan, scheme=scheme)
    # coordinator 上的 cortex-outbox 一條同時服務 digest 與 engineering-outcome。
    merged = [
        g for g in grants
        if g.path == DEFAULT_LAYOUT.coordinator_root and len(g.required_by) > 1
    ]
    assert merged and all(len(set(g.required_by)) == len(g.required_by) for g in merged)


def test_owner_of_the_leaf_never_gets_a_redundant_grant() -> None:
    """二分把 reviewer 併進 durable-state owner——此時 verdict spool 的 traverse
    本來就由 owner 位提供，不得再產生一條。這條同時證明推導是 config 驅動的。"""
    two = derive_traverse_grants(generate_plan(TWO_WAY_SCHEME), scheme=TWO_WAY_SCHEME)
    three = derive_traverse_grants(
        generate_plan(THREE_WAY_SCHEME), scheme=THREE_WAY_SCHEME
    )
    reviewer_two = TWO_WAY_SCHEME.resolve(Principal.REVIEWER)
    assert reviewer_two == TWO_WAY_SCHEME.durable_state_owner
    assert not [
        g for g in two
        if g.account == reviewer_two and g.path == DEFAULT_LAYOUT.coordinator_root
    ], "coordinator 由 cortex-svc 自己擁有，二分下不需要任何 traverse ACL"
    assert [
        g for g in three
        if g.account == THREE_WAY_SCHEME.resolve(Principal.REVIEWER)
        and g.path == DEFAULT_LAYOUT.coordinator_root
    ]


def test_unknown_intermediate_directory_is_fail_closed() -> None:
    """登記表／骨架都沒描述的中間層保守視為不可 traverse——寧可多產一條 `--x`，
    也不要漏掉而讓正向路徑靜默斷掉。

    #641 之前這條以 `work-items-yaml`（落點 `<job 樹>/.cortex/work-items.yaml`）
    當實例，因為它是當時登記表裡唯一「跨帳號 ACL 的路徑經過一個沒人描述的中間層」
    的資產。#641 收掉 job 樹上全部三條跨帳號讀取 ACL 之後，登記表**剛好**沒有這種
    資產了——但被驗的性質（`can_traverse(None, …) is False` ⇒ 導出一條 `--x`）完全
    沒變。因此改以**合成資產**驗，不再綁在某一項登記表資料上：那個耦合正是這條測試
    會隨無關變更一起紅掉的原因。
    """
    assert can_traverse(None, "cortex-builder", THREE_WAY_SCHEME) is False

    synthetic = TrustRootAsset(
        "synthetic-nested-leaf", AssetTier.TIER_1, TrustTree.MANAGER_OWNED, None,
        (Principal.MANAGER,), (Principal.MANAGER, Principal.BUILDER),
        IngressKind.MANAGER_INTERNAL,
    )
    nested = f"{DEFAULT_LAYOUT.coordinator_root}/undescribed/leaf.json"
    plan = generate_plan(THREE_WAY_SCHEME, ASSET_REGISTRY + (synthetic,))
    grants = derive_traverse_grants(
        plan,
        scheme=THREE_WAY_SCHEME,
        path_of={**permgen.asset_paths(), "synthetic-nested-leaf": nested},
    )
    hidden = [g for g in grants if g.path.endswith("/undescribed")]
    assert hidden, "沒人描述的中間層必須被推導出一條 traverse"
    assert hidden[0].account == THREE_WAY_SCHEME.resolve(Principal.BUILDER)


def test_no_traverse_grant_reaches_into_a_job_worktree() -> None:
    """#641：job 的工作樹底下不得有任何導出的 traverse 授權。

    traverse 是機械導出的（#620）——只要登記表在 job 樹裡留下**任何一條**跨帳號
    ACL，`<job 樹>` 乃至其子目錄就會自動被補上 `--x`。因此「收掉 `repo-worktree`
    的 `rX`」若沒有把 `review-verdict`／`work-items-yaml` 一起收掉，洞會從 traverse
    這一側自己長回來。這條就是釘住那件事。
    """
    job_root = permgen.asset_paths()["repo-worktree"]
    for scheme in ALL_SCHEMES:
        grants = derive_traverse_grants(generate_plan(scheme), scheme=scheme)
        intruders = [g for g in grants if g.path == job_root or g.path.startswith(job_root + "/")]
        assert not intruders, (scheme.scheme_id, [(g.path, g.account, g.required_by) for g in intruders])


# ---------------------------------------------------------------------------
# (d) 三條正向路徑的完整鏈
# ---------------------------------------------------------------------------

def test_three_way_forward_paths_have_a_complete_chain() -> None:
    """三分下三條正向路徑：從樹根到葉，每一層都可 search。"""
    plan = generate_plan(THREE_WAY_SCHEME)
    paths = permgen.asset_paths()
    facts = directory_facts(plan, scheme=THREE_WAY_SCHEME)
    grants = derive_traverse_grants(plan, scheme=THREE_WAY_SCHEME)
    granted = {(g.path, g.account) for g in grants}
    for principal, asset_id in FORWARD_PATHS:
        account = THREE_WAY_SCHEME.resolve(principal)
        assert account_can_reach(plan, account=account, asset_id=asset_id), (
            account, asset_id
        )
        # 逐層列出鏈，證明「每一層」都被覆蓋，而不是只有葉節點對。
        chain = permgen._ancestors_within(
            paths[asset_id], permgen.managed_roots(facts)
        )
        assert chain, asset_id
        for hop in chain:
            assert (
                (hop, account) in granted
                or can_traverse(facts.get(hop), account, THREE_WAY_SCHEME)
            ), (asset_id, account, hop)
        # 葉節點本身的權限沒被改掉（wx／rX 照舊，traverse 是**額外**的一層）。
        leaf = plan.by_id(asset_id)
        assert any(a.account == account and "x" in a.perms.lower() for a in leaf.acls)


def test_forward_paths_survive_a_relocated_layout() -> None:
    """推導是純 config 驅動：換部署位置不必改產生器一行程式碼。"""
    alt = PathLayout(
        agents_root="/srv/cortex",
        worktree_root="/srv/cortex/wt",
        deploy_root="/usr/local/cortex",
        instance="alpha",
    )
    plan = generate_plan(THREE_WAY_SCHEME)
    grants = derive_traverse_grants(plan, alt, THREE_WAY_SCHEME)
    assert grants
    assert all(g.path.startswith(("/srv/cortex", "/usr/local/cortex")) for g in grants)
    for principal, asset_id in FORWARD_PATHS:
        assert account_can_reach(
            plan, alt, THREE_WAY_SCHEME,
            account=THREE_WAY_SCHEME.resolve(principal), asset_id=asset_id,
        )


# ---------------------------------------------------------------------------
# 命令序列：位置、形狀、非執行保證
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_traverse_section_comes_after_every_chmod(scheme) -> None:
    """順序是正確性的一部分：`chmod` 在帶 ACL 的物件上會重寫 ACL **mask**，
    先 setfacl 再 chmod 會讓具名條目的有效權限被 mask 成空（靜默失效）。"""
    lines = _commands(scheme)
    executable = _executable(lines)
    traverse_at = [
        i for i, ln in enumerate(executable)
        if ln.startswith("setfacl") and f":{TRAVERSE_PERMS} " in ln
    ]
    assert traverse_at
    last_chmod = max(
        i for i, ln in enumerate(executable)
        if ln.startswith("chmod ") or ln.startswith("install -d ")
    )
    assert min(traverse_at) > last_chmod
    # 而且是連續的一節（不散落在各資產之間）。
    assert traverse_at == list(range(min(traverse_at), max(traverse_at) + 1))


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_traverse_commands_are_strings_only(scheme) -> None:
    """維持 permgen 的非執行不變式：本節仍只有 setfacl 字串。"""
    lines = _commands(scheme)
    for line in _executable(lines):
        assert line.startswith(("install -d ", "chown ", "chmod ", "setfacl ", "[ ! -e "))
    assert not any("<PATH:" in ln for ln in _executable(lines))


def test_per_job_traverse_grants_are_commented_out() -> None:
    """含 per-job segment 的路徑由降權啟動器逐案套用，setup 階段不得執行。"""
    for line in _commands():
        if permgen.PER_JOB_SEGMENT in line:
            assert line.lstrip().startswith("#"), line


def test_placeholder_mode_emits_no_traverse_section() -> None:
    """未帶真實路徑時沒有路徑階層可推——不得憑空產生指向真實絕對路徑的命令。"""
    lines = permgen.plan_to_commands(generate_plan(THREE_WAY_SCHEME))
    assert not any(f":{TRAVERSE_PERMS} " in ln for ln in lines)
