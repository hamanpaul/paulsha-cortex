"""Phase 2a（權限產生器）：涵蓋等式、二分/三分不變式、非執行保證。

驗收（對應 task 產出 1 的等式測試）：
- 產生器涵蓋登記表每一項、無遺漏；
- 二分與三分 config 都能產出一致（滿足同一組不變式）的權限集合；
- builder（及三分下全部 headless）對 Manager-owned／deployment 樹零寫入；
- 只產生命令字串、絕不執行任何 root 操作。
"""
from __future__ import annotations

import json

import pytest

from paulsha_cortex.trust_root import permgen
from paulsha_cortex.trust_root.permgen import (
    THREE_WAY_SCHEME,
    TWO_WAY_SCHEME,
    OwnerClass,
    PermissionPlan,
    generate_plan,
)
from paulsha_cortex.trust_root.registry import ASSET_REGISTRY, Principal

ALL_SCHEMES = [TWO_WAY_SCHEME, THREE_WAY_SCHEME]

#: #626：`operator` 與 `external` 是部署決定，模組層方案刻意留 `None`——未注入時
#: `plan_to_commands()` 一律 fail-closed（由
#: `tests/test_trust_root_principal_account_mapping_626.py` 專門釘住）。本檔要驗
#: 命令輸出的測試先注入一組示範對應。
DEPLOYMENT_ACCOUNTS = {
    Principal.OPERATOR: "cortex-ops",
    Principal.EXTERNAL: "cortex-outbox",
}


def _resolved(scheme):
    return scheme.with_principal_accounts(DEPLOYMENT_ACCOUNTS)


def _plan(scheme) -> PermissionPlan:
    return generate_plan(scheme)


# ---------------------------------------------------------------------------
# 涵蓋：登記表每一項、無遺漏
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_plan_covers_every_asset_exactly_once(scheme) -> None:
    plan = _plan(scheme)
    plan_ids = [e.asset_id for e in plan.entries]
    registry_ids = [a.asset_id for a in ASSET_REGISTRY]
    assert plan_ids == registry_ids, "順序與內容必須逐項對齊登記表"
    assert len(plan_ids) == len(set(plan_ids)), "無重複"
    assert len(plan.entries) == len(ASSET_REGISTRY), "無遺漏"


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_every_entry_fully_populated(scheme) -> None:
    plan = _plan(scheme)
    for e in plan.entries:
        assert e.owner, e.asset_id
        assert e.group, e.asset_id
        # #698：mode 可帶 sticky（0o1000），但**永遠**不得帶 setuid／setgid。
        assert 0 <= e.mode <= 0o1777, e.asset_id
        assert not (e.mode & 0o6000), (e.asset_id, e.mode_str)  # setuid/setgid
        # sticky 只出現在那一族，且必然是目錄——別處長出一位 sticky 一定是 bug。
        if e.mode & permgen.STICKY_BIT:
            assert e.asset_id in permgen.STICKY_JOB_WRITABLE_DIR_ASSETS, e.asset_id
            assert e.is_directory, e.asset_id
        assert e.owner_class in OwnerClass, e.asset_id
        assert e.writer_accounts, e.asset_id  # 至少 owner 可寫
        assert e.rationale, e.asset_id


# ---------------------------------------------------------------------------
# 核心不變式（兩個 scheme 都成立）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_builder_never_writes_manager_owned_or_deployment(scheme) -> None:
    """spec §R2：builder 對 Manager-owned／deployment 樹零寫入（兩 scheme 皆然）。

    **#698 起 `OwnerClass.STICKY_SHARED` 不在這條的涵蓋範圍內，而那是刻意的**：
    sticky 樹（`~/.codex`）由 root 擁有、但 job **必須**寫得進去（否則 codex 起不來）。
    它之所以不塞進 `DEPLOYMENT`，正是為了讓本測試維持「機械成立、零例外清單」。
    真正要守的那一半改由下一個測試釘住：樹**裡面**那個 root-owned 的 enforcement 檔，
    builder 一個位元都不能寫。
    """
    builder = scheme.resolve(Principal.BUILDER)
    plan = _plan(scheme)
    for e in plan.entries:
        if e.owner_class in (OwnerClass.MANAGER_STATE, OwnerClass.DEPLOYMENT):
            writable = plan.all_writable_accounts(e)
            assert builder not in writable, (scheme.scheme_id, e.asset_id, sorted(writable))


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_enforcement_leaves_are_never_writable_by_any_job_account(scheme) -> None:
    """#698：sticky 樹裡那個 root-owned 的 enforcement 檔，**任何** job 帳號零寫入。

    這是 sticky 樹換到的東西本身。0818 的 R9 T3.9 攻破的就是這一條——當時
    `cortex-reviewer-planner` 的 `~/.codex` 整棵 job-owned，`hooks.json` 想放也放不住。
    codex hooks 會執行命令 ⇒ 那不是「少一層防護」，是跨 job 持久化。
    """
    plan = _plan(scheme)
    job_accounts = {
        a for a in (scheme.resolve(p) for p in permgen.UNTRUSTED_EXECUTION_PRINCIPALS)
        if a is not None
    }
    assert permgen.ENFORCEMENT_LEAF_ASSETS, "enforcement 檔一族不得為空"
    for asset_id in permgen.ENFORCEMENT_LEAF_ASSETS:
        e = plan.by_id(asset_id)
        assert e.owner_class is OwnerClass.DEPLOYMENT, asset_id
        assert e.owner == scheme.deploy_account, asset_id
        writable = plan.all_writable_accounts(e)
        assert not (writable & job_accounts), (scheme.scheme_id, asset_id, sorted(writable))
        assert not e.acls, (asset_id, e.acls)  # 零跨帳號授權


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_sticky_trees_are_root_owned_with_named_acls_and_no_default_acl(scheme) -> None:
    """#698 方案 A 的三個條件，逐條釘住（少任何一條，整個裁決就是空的）。"""
    plan = _plan(scheme)
    for asset_id in permgen.STICKY_JOB_WRITABLE_DIR_ASSETS:
        try:
            e = plan.by_id(asset_id)
        except KeyError:  # 本方案沒有這個帳號
            continue
        # (1) owner 必須是 root：目錄 owner 對 sticky 免疫。
        assert e.owner == scheme.deploy_account, asset_id
        # (2) sticky bit 必須留在 mode 裡（安全網一度會吃掉它，那是本票修的東西）。
        assert e.sticky, (asset_id, e.mode_str)
        assert e.mode_str == "1755", (asset_id, e.mode_str)
        # (3) 具名 access ACL、**且沒有 default ACL**——default 會讓 root 日後放進去的
        #     enforcement 檔自動帶上 job 的 rwx，等於把整個形狀交還回去。
        assert e.acls, asset_id
        for acl in e.acls:
            assert acl.perms == "rwx", (asset_id, acl)
            assert acl.default is False, (asset_id, acl)
        rendered = "\n".join(e.commands(f"/tmp/{asset_id}"))
        assert "setfacl -d" not in rendered, rendered


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_reviewer_and_builder_are_different_accounts(scheme) -> None:
    """spec §R2 硬性要求：reviewer 與 builder 互不可寫 → 必為不同帳號。"""
    assert scheme.resolve(Principal.REVIEWER) != scheme.resolve(Principal.BUILDER)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_group_and_other_never_writable(scheme) -> None:
    """spec §R2：現存 group-writable 現況必須收斂——group/other 一律無 write 位。"""
    plan = _plan(scheme)
    for e in plan.entries:
        assert not (e.mode & 0o020), (e.asset_id, e.mode_str)  # group w
        assert not (e.mode & 0o002), (e.asset_id, e.mode_str)  # other w


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_manager_owned_acls_are_read_only(scheme) -> None:
    """Manager-owned/deployment 上的 ACL 只能是唯讀（跨帳號讀），永不授寫。"""
    plan = _plan(scheme)
    for e in plan.entries:
        if e.owner_class in (OwnerClass.MANAGER_STATE, OwnerClass.DEPLOYMENT):
            for acl in e.acls:
                assert not acl.writable, (e.asset_id, acl.account, acl.perms)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_review_verdict_shortest_attack_path_closed(scheme) -> None:
    """§3 最短攻擊路徑：builder 不得寫 reviewer 的 verdict 檔（兩 scheme 皆然）。"""
    plan = _plan(scheme)
    e = plan.by_id("review-verdict")
    builder = scheme.resolve(Principal.BUILDER)
    assert builder not in plan.all_writable_accounts(e)
    # verdict owner 必為 reviewer 所在帳號（reviewer 產出、builder 攻不進）。
    assert e.owner == scheme.resolve(Principal.REVIEWER)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_control_queue_headless_not_writable(scheme) -> None:
    """spec §R4：control queue 收斂為 Manager-owned，headless 不可寫。"""
    plan = _plan(scheme)
    e = plan.by_id("control-request-queue")
    assert e.owner_class is OwnerClass.MANAGER_STATE
    assert scheme.resolve(Principal.BUILDER) not in plan.all_writable_accounts(e)
    assert any("socket" in op for op in e.open_points), "須標記提交改走 socket 的未決點"


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_worktree_pool_enforces_r2_at_subdir(scheme) -> None:
    """多 persona 容器：不得做成共寫目錄；per-job chown 由 runtime 負責。"""
    plan = _plan(scheme)
    e = plan.by_id("dispatch-worktree-pool")
    assert e.runtime_managed is True
    assert e.is_directory is True
    # 容器層只有 owner 可寫（避免 reviewer/builder 互寫破 R2）。
    assert plan.all_writable_accounts(e) == {e.owner}
    assert scheme.resolve(Principal.BUILDER) not in {e.owner} or e.owner == scheme.durable_state_owner


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_spool_producer_gets_write_via_acl_only(scheme) -> None:
    """spool：builder（producer）以 ACL 取得 write，且非 owner（trusted consumer 擁有）。"""
    plan = _plan(scheme)
    e = plan.by_id("monitor-event-spool")
    builder = scheme.resolve(Principal.BUILDER)
    assert e.owner == scheme.durable_state_owner
    assert e.owner != builder
    acl_writers = {a.account for a in e.acls if a.writable}
    assert builder in acl_writers


# ---------------------------------------------------------------------------
# 二分 vs 三分：結構一致 + 三分嚴格收緊（保留彈性有實效）
# ---------------------------------------------------------------------------

def test_owner_class_is_scheme_independent() -> None:
    """兩 scheme 對每個資產分到相同 owner_class——分類只依登記表，不依 UID 方案。"""
    two = {e.asset_id: e.owner_class for e in _plan(TWO_WAY_SCHEME).entries}
    three = {e.asset_id: e.owner_class for e in _plan(THREE_WAY_SCHEME).entries}
    assert two == three


def test_two_way_merges_reviewer_planner_into_durable_owner() -> None:
    """二分（現階段）：reviewer＋planner 併入 durable_state_owner（可寫 durable state
    的既知殘餘），這正是三分要移除的對象。"""
    s = TWO_WAY_SCHEME
    assert s.resolve(Principal.REVIEWER) == s.durable_state_owner
    assert s.resolve(Principal.PLANNER) == s.durable_state_owner


def test_three_way_splits_manager_out_without_code_change() -> None:
    """三分：Manager（durable_state_owner）與 reviewer/planner 帳號分離——僅換
    config、未改任何程式碼即達成 spec 風險段的『待驗證後再細分』。"""
    s = THREE_WAY_SCHEME
    rp = s.resolve(Principal.REVIEWER)
    assert s.resolve(Principal.PLANNER) == rp
    assert rp != s.durable_state_owner
    assert s.resolve(Principal.MANAGER) == s.durable_state_owner


def test_three_way_strictly_tightens_manager_owned() -> None:
    """三分嚴格收緊：**沒有任何 headless 帳號**（含 reviewer/planner）可寫
    Manager-owned／deployment——這是保留彈性的實質收益。"""
    plan = _plan(THREE_WAY_SCHEME)
    headless = THREE_WAY_SCHEME.headless_accounts()
    for e in plan.entries:
        if e.owner_class in (OwnerClass.MANAGER_STATE, OwnerClass.DEPLOYMENT):
            assert not (plan.all_writable_accounts(e) & headless), (e.asset_id,)


def test_custom_scheme_needs_no_code_change() -> None:
    """資料結構足以表達任意分法：臨時造一個把 planner 也獨立的『四分』方案，
    產生器不改一行即可運作。"""
    four_way = permgen.UidScheme(
        scheme_id="four-way",
        account_of={
            Principal.MANAGER: "cortex-manager",
            Principal.MONITOR: "cortex-manager",
            Principal.REVIEWER: "cortex-reviewer",
            Principal.PLANNER: "cortex-planner",
            Principal.BUILDER: "cortex-builder",
            Principal.HEADLESS_HOOK: "cortex-builder",
        },
        durable_state_owner="cortex-manager",
        # #626：部署決定型 principal 走專屬欄位，不得混進 `account_of`。
        operator_account="cortex-ops",
        external_reader_account="cortex-outbox",
    )
    plan = generate_plan(four_way)
    assert len(plan.entries) == len(ASSET_REGISTRY)
    # 四分下 reviewer 與 planner 也互為不同帳號。
    assert four_way.resolve(Principal.REVIEWER) != four_way.resolve(Principal.PLANNER)


# ---------------------------------------------------------------------------
# 決定性 + 非執行保證
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_generation_is_deterministic(scheme) -> None:
    assert _plan(scheme).to_dict() == _plan(scheme).to_dict()


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_plan_is_json_serializable(scheme) -> None:
    blob = json.dumps(_plan(scheme).to_dict(), ensure_ascii=False)
    assert json.loads(blob)["asset_count"] == len(ASSET_REGISTRY)


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=lambda s: s.scheme_id)
def test_commands_are_strings_only_and_never_execute(scheme) -> None:
    """產出只能是 install -d／chown／chmod／setfacl（可帶 `[ ! -e ] ||` 存在性守衛）
    字串——絕不執行任何 root 操作。"""
    plan = _plan(_resolved(scheme))
    lines = permgen.plan_to_commands(plan)
    # #698 擴了三個動詞，逐條有理由——這張清單就是「這份 script 會做什麼」的全部：
    #   `[ ! -L `：sticky 樹與它的 enforcement 檔都住在 **job 可寫**的位置，root 對
    #             它們動手之前必須先確認不是 symlink（job 預埋一條懸空 symlink，
    #             root 的 `cat >`／`chown` 就會跟著它跑到別處去）。
    #   `[ -e `  ：enforcement 檔的 create-if-absent 守衛（**不是**「不存在就跳過」）。
    #   `cat > ` ：把最小內容種進 enforcement 檔。它是唯一會寫入內容的動詞，目標永遠
    #             是登記表上的 enforcement 路徑，內容是常數。
    allowed = (
        "install -d ", "chown ", "chmod ", "setfacl ",
        "[ ! -e ", "[ ! -L ", "[ -e ",
    )
    heredoc_end = None
    for line in lines:
        stripped = line.strip()
        if heredoc_end is not None:
            # heredoc 內容區：逐字就是 `CODEX_HOOKS_SEED_CONTENT`，不是命令。
            if stripped == heredoc_end:
                heredoc_end = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if "<<'" in stripped:
            assert stripped.startswith("[ -e ") and "cat > " in stripped, stripped
            heredoc_end = stripped.split("<<'", 1)[1].rstrip("'")
            continue
        assert stripped.startswith(allowed), stripped
    assert heredoc_end is None, "heredoc 沒有收尾——產出的 script 會吃掉後面所有命令"
    seeded = "\n".join(lines)
    assert permgen.CODEX_HOOKS_SEED_CONTENT.splitlines()[0] in seeded
    # 每個資產都要在命令輸出裡出現（以 placeholder 或註解形式）。
    joined = "\n".join(lines)
    for a in ASSET_REGISTRY:
        assert a.asset_id in joined, a.asset_id


def test_commands_honour_supplied_paths() -> None:
    """runbook 帶入真實路徑時，命令引用該路徑；未帶入者以 placeholder。"""
    plan = _plan(_resolved(TWO_WAY_SCHEME))
    lines = permgen.plan_to_commands(plan, path_of={"jobs-registry": "/srv/agents/coordinator/jobs.json"})
    joined = "\n".join(lines)
    assert "/srv/agents/coordinator/jobs.json" in joined
    assert "<PATH:review-verdict>" in joined  # 未帶入者仍是 placeholder


def test_permgen_module_does_not_import_subprocess() -> None:
    """靜態保證：permgen 不觸及任何執行面（無 subprocess/os.system）。"""
    import inspect

    src = inspect.getsource(permgen)
    assert "subprocess" not in src
    assert "os.system" not in src
    assert "os.chown" not in src
    assert "os.chmod" not in src
