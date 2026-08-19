"""#641：收掉 Manager 對 job 工作樹的唯讀 ACL 之後的不變式。

## 這一票在修什麼

登記表資產 `repo-worktree` 原本宣告 `readers=(MANAGER,)`，permgen 因此機械產出

    setfacl -m u:cortex-manager:rX /var/lib/cortex/worktree/<job-id>

rationale 寫的是「交換面沿用 D2 git 讀」。**那個交換面在 #637 之後已經不存在**：
builder 在自己的 clone 產 bundle → 寫進 Manager-owned 的 `commit-spool` → Manager
從**那個檔案** fetch。#637 甚至為此加了不變式測試
`test_manager_never_touches_the_builder_clone_while_harvesting`（把 clone `chmod 000`
仍能完成回收）。

operator 0817 的實機複驗因此抓到一個**測試與部署互相矛盾**的狀態：

    只有 0700、無 ACL          → Manager `ls` 得到 Permission denied（#637 的不變式成立）
    套上登記表要求的那條 ACL    → Manager 讀得到（#637 的不變式在實機上不成立）

本檔釘住修正後的那一側：**照登記表部署，Manager 對 job 工作樹確實讀不到。**

## 為什麼三個資產一起收

job 樹底下原本有**三條**同型的跨帳號讀取授權，全部出自 permgen「單一 job writer」
分支那一句「trusted reader（Manager）以唯讀 ACL 讀取」：

| 資產 | 落點 | 原授權 | 真正的消費者 |
|---|---|---|---|
| `repo-worktree` | `<job 樹>` | `u:cortex-manager:rX` | 已改 bundle（#637）；殘留的讀取端見下 |
| `review-verdict` | `<job 樹>/.psc-review-verdict.json` | `u:cortex-manager:r` | 已改 `review-verdict-spool`（#599／#639） |
| `work-items-yaml` | `<job 樹>/.cortex/work-items.yaml` | `u:cortex-manager:r` | monitor 讀的是**來源樹**那一份（`repo-source-tree`） |

而 traverse ACL 是**機械導出**的（#620）：只要 job 樹裡還留著任何一條跨帳號 ACL，
`<job 樹>` 乃至其子目錄就會自動被補上 `--x`。只收 `repo-worktree` 一條，洞會從
traverse 那一側自己長回來——`test_no_traverse_grant_reaches_into_a_job_worktree`
（在 `test_trust_root_permgen_traverse_620.py`）釘的就是這件事。

## verification 那一組檢查怎麼辦

`coordinator/verification.py` 有一組**確實在讀 job 工作樹**的檢查（worktree HEAD ==
candidate、工作樹乾淨），而且同一個 `worktree` 還被當成 `cwd` 去**執行**宣告出來的
check／test／full-suite。收掉 ACL 之後這些在三分下全部 `Permission denied`。

處置是**方向 1**：搬到 #629 的第三執行身分。在那之前 fail-closed，並以專屬理由碼
`candidate-worktree-unreadable-pending-gate-identity` ＋ evidence 裡的
`blocked_on: "#629"` 指出下一步——**不是**靜默略過、**不是**改讀 bundle（同源會讓
檢查退化）、**不是**讓 builder 自報工作樹乾淨（#540／#628）。

## 環境相依的那一組（#638 的教訓）

「別的 uid 讀不到」是 **OS 層語意**，單 UID 環境根本測不出來——那正是 #637 CI 全綠
卻在實機第一步就斷掉的形狀。因此跨 UID 那一組需要 root 才借得到兩個身分，拿不到時
**明確 skip 並附理由**，絕不靜默通過；結構性的那一組（權限計畫與產生器輸出）在任何
環境都會跑。
"""

from __future__ import annotations

import os
import shutil
import stat
import struct
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager, spool_slot, verification
from paulsha_cortex.trust_root import permgen, registry
from paulsha_cortex.trust_root.permgen import DEFAULT_LAYOUT, generate_plan
from paulsha_cortex.trust_root.registry import ASSET_REGISTRY, Principal, asset_by_id


#: #626：`operator`／`external` 是部署決定，模組層方案刻意留 None；本檔只關心 job
#: 樹，但 `plan_to_commands` 會 fail-closed，故一律用注入後的方案。
DEPLOYMENT_ACCOUNTS = {
    Principal.OPERATOR: "cortex-ops",
    Principal.EXTERNAL: "cortex-outbox",
}
THREE_WAY = permgen.THREE_WAY_SCHEME.with_principal_accounts(DEPLOYMENT_ACCOUNTS)
TWO_WAY = permgen.TWO_WAY_SCHEME.with_principal_accounts(DEPLOYMENT_ACCOUNTS)
ALL_SCHEMES = [TWO_WAY, THREE_WAY]
SCHEME_IDS = [s.scheme_id for s in ALL_SCHEMES]


# ---------------------------------------------------------------------------
# 共用：把「job 樹底下的跨帳號授權」抽成一個可被突變驗證重複呼叫的函式
# ---------------------------------------------------------------------------

def _job_tree_root(paths: dict[str, str]) -> str:
    """per-job 工作樹的落點（`<worktree pool>/<job-id>`）。"""

    return paths["repo-worktree"]


def _entries_inside_the_job_tree(plan, paths: dict[str, str]):
    """權限計畫中，落點在 per-job 工作樹**之內**（含樹根自己）的每一項。

    pool 容器（`dispatch-worktree-pool`）刻意不算：那一層是 Manager 擁有的容器，
    Manager 進得去是設計本身（`0701`＝別的帳號只能 traverse、列不出目錄）。
    """

    root = _job_tree_root(paths)
    return [
        (entry, paths[entry.asset_id])
        for entry in plan.entries
        if paths.get(entry.asset_id) == root or paths.get(entry.asset_id, "").startswith(root + "/")
    ]


def cross_account_grants_into_the_job_tree(assets, scheme) -> list[tuple[str, str, str]]:
    """job 樹底下所有「授給非 owner 帳號」的 ACL。空 list＝#641 的不變式成立。

    回傳 `(asset_id, 帳號, perms)`，方便突變驗證時直接看見復發的是哪一條。
    """

    plan = generate_plan(scheme, tuple(assets))
    paths = DEFAULT_LAYOUT.asset_paths()
    found: list[tuple[str, str, str]] = []
    for entry, _path in _entries_inside_the_job_tree(plan, paths):
        for acl in entry.acls:
            if acl.account != entry.owner:
                found.append((entry.asset_id, acl.account, acl.perms))
    return sorted(found)


#: 修法前的形狀：三個 job 樹資產各自把 Manager／Monitor 宣告為 reader。突變驗證用。
_PRE_641_READERS = {
    "repo-worktree": (Principal.MANAGER,),
    "review-verdict": (Principal.MANAGER,),
    "work-items-yaml": (Principal.MONITOR,),
}


def _pre_641_registry() -> tuple:
    return tuple(
        replace(asset, readers=_PRE_641_READERS[asset.asset_id])
        if asset.asset_id in _PRE_641_READERS
        else asset
        for asset in ASSET_REGISTRY
    )


# ---------------------------------------------------------------------------
# 1. 登記表／權限計畫（結構性——任何環境都跑）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_no_manager_grant_anywhere_inside_a_job_worktree(scheme) -> None:
    """#641 的驗收本身，**由 #710 重述**：job 樹底下不得有任何授給 Manager 的 ACL。

    ## 為什麼判準從「非 owner」改成「Manager」（逐條）

    #641 原本的形狀是「job 樹 owner＝job 帳號、零 ACL」，於是「非 owner 的 ACL」與
    「Manager 的 ACL」在那個形狀下是同一件事，寫成前者比較嚴。**#710 查證出那個形狀
    從來不曾存在於實機上，而且結構上不可能存在**：per-job clone 是 Manager 用
    `git clone` 建的，把它 `chown` 給 job 帳號需要 `CAP_CHOWN`，而 Manager unit 帶
    `CapabilityBoundingSet=`（空）。實機 `stat` 回的一直是
    `cortex-manager:cortex-manager 700`、零具名 ACL——builder job 因此連 `chdir` 都
    進不去（#710 的原症狀）。

    形態更正為 **Manager 擁有 ＋ job 具名 ACL** 之後，「非 owner 的 ACL」就必然非空
    （job 自己那條就是），照原樣留著只會讓這條測試表達「本修法不得存在」。因此判準
    收斂到 #641 真正在守的那一半——**`durable_state_owner` 不得經由 ACL 取得 job 樹的
    存取**，而「有哪些跨帳號授權」則由下一條測試逐條釘死（不是放寬成「隨便誰都行」）。

    ## 誠實邊界：這條測試守不住「Manager 進不去 job 樹」

    Manager 是那一格的 **owner**，owner 位給的存取收不掉。#641 真正想擋的提權路徑
    （`verification` 以 `cwd=<job 樹>` 執行 job 交出來的 `conftest.py`）因此**不是**
    由這棵樹的權限擋住的，而是由 `verification` 的 fail-closed
    （`candidate-worktree-unreadable-pending-gate-identity`）與 #629 的第三執行身分
    擋住。這在實機 0817 起就已經是事實；#710 只是讓登記表與產生器停止宣稱相反的事
    （#696 的同型）。
    """

    manager = scheme.durable_state_owner
    offenders = [
        row
        for row in cross_account_grants_into_the_job_tree(ASSET_REGISTRY, scheme)
        if row[1] == manager
    ]
    assert offenders == []


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_grants_into_the_job_tree_are_exactly_what_the_rule_declares(scheme) -> None:
    """job 樹底下的每一條跨帳號授權都必須出自 `JOB_WORKSPACE_REACH`（#710）。

    上一條只擋 Manager；這一條擋「除了規則宣告的那些以外，什麼都不准出現」——兩條
    合起來才等價於 #641 原本那句「零跨帳號 ACL」在新形態下的意思。
    """

    reach = registry.job_workspace_reach_for(Principal.BUILDER)
    job_account = scheme.resolve(reach.persona)
    # access ＋ default 兩條（perms 刻意不同：遞迴套在既有檔上用大寫 `X`，
    # default 沒有「既有檔」可參照因此用 `x`）。
    expected = {
        ("repo-worktree", job_account, reach.access_perms),
        ("repo-worktree", job_account, reach.default_perms),
    }
    for reader_principal, reader_perms in reach.extra_reader_perms:
        account = scheme.resolve(reader_principal)
        if account is None:  # two-way／three-way 沒有 gate
            continue
        expected.add(("repo-worktree", account, reader_perms))
    granted = set(cross_account_grants_into_the_job_tree(ASSET_REGISTRY, scheme))
    assert granted == expected


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_the_job_tree_invariant_actually_covers_something(scheme) -> None:
    """守衛：上一條若因為「job 樹底下一項資產都沒有」而空過，就什麼也沒證明。"""

    plan = generate_plan(scheme)
    paths = DEFAULT_LAYOUT.asset_paths()
    covered = {entry.asset_id for entry, _ in _entries_inside_the_job_tree(plan, paths)}
    assert covered >= {"repo-worktree", "review-verdict", "work-items-yaml", "handoff-manifest"}


def test_repo_worktree_is_manager_owned_with_a_named_job_acl() -> None:
    """job 樹本身：owner＝**Manager**、`0700`、job 帳號以具名 ACL 取得可寫面。

    **#710 修改本測試的理由**：原斷言是「owner＝job 帳號、`entry.acls == ()`」。
    那個形態從來沒有被實作過（全 `coordinator/` 零個 `chown`），而且 Manager
    結構上做不到它（`CAP_CHOWN` 不在 `CapabilityBoundingSet=` 內）——實機一直是
    `cortex-manager:cortex-manager 700` ＋ 零 ACL，於是 builder job 連 `chdir` 都
    進不去。斷言照原樣留著就是「測試綠、實機死」，正是 #641 自己記錄過的那個形狀
    （「測試裡成立的不變式在照登記表部署的實機上不成立」）。

    `0700` 與 `is_directory` 兩條**一行未改**：group／other 仍然零權限，跨帳號授權
    一律走具名 ACL。
    """

    entry = generate_plan(THREE_WAY).by_id("repo-worktree")
    assert entry.owner == THREE_WAY.durable_state_owner
    assert entry.mode == 0o700
    assert entry.is_directory is True
    reach = registry.job_workspace_reach_for(Principal.BUILDER)
    builder = THREE_WAY.resolve(Principal.BUILDER)
    assert [(a.account, a.perms, a.default, a.recursive) for a in entry.acls] == [
        (builder, reach.access_perms, False, True),
        (builder, reach.default_perms, True, True),
    ]


def test_registry_no_longer_declares_manager_as_a_reader_of_the_job_tree() -> None:
    """宣告層（不是產生層）：三個資產的 readers 都不再含 Manager／Monitor。

    產生器只是把登記表翻成命令；真正的單一真相是登記表這一欄。
    """

    for asset_id in _PRE_641_READERS:
        readers = set(asset_by_id(asset_id).readers)
        assert Principal.MANAGER not in readers, asset_id
        assert Principal.MONITOR not in readers, asset_id
        assert readers, f"{asset_id} 仍必須有 reader（登記表不允許無人消費的資產）"


def test_dispatch_worktree_pool_grants_nothing_extra_for_manager_reads() -> None:
    """容器層複驗：`0701` Manager-owned，零 ACL——沒有任何為「Manager 讀 job 樹」而設的授權。"""

    entry = generate_plan(THREE_WAY).by_id("dispatch-worktree-pool")
    assert entry.owner == THREE_WAY.durable_state_owner
    assert entry.mode == 0o701
    assert entry.acls == ()


# ---------------------------------------------------------------------------
# 2. 產生器輸出（operator 真正會執行的那些字串）
# ---------------------------------------------------------------------------

def _command_lines(scheme) -> list[str]:
    """`plan_to_commands` 的輸出，逐行去掉註解前綴。

    per-job 資產的命令是以 `#   ` 註解形式輸出的（由降權啟動器逐案套用，不在 setup
    階段執行）——**那些也算數**，因此必須連註解一起檢查，不能只看未註解的行。
    """

    lines = permgen.plan_to_commands(
        generate_plan(scheme), path_of=DEFAULT_LAYOUT.asset_paths(), scheme=scheme
    )
    return [line.lstrip("#").strip() for line in lines]


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_generated_commands_grant_no_manager_setfacl_into_a_job_worktree(scheme) -> None:
    """產生的權限 script（含註解掉的 per-job 段）沒有任何**授給 Manager** 的 setfacl。

    **#710 修改本測試的理由**：原斷言是「job 樹底下零 setfacl」。#710 之後那一格
    **必然**有 setfacl——job 帳號的可寫面就是靠它（`chown` 需要 `CAP_CHOWN`，Manager
    沒有）。判準因此與上方 `test_no_manager_grant_anywhere_inside_a_job_worktree`
    對齊到 #641 真正在守的那一半；「那裡有哪些 setfacl」則由下一條逐字釘死，不是
    放寬成「隨便幾條都行」。
    """

    root = _job_tree_root(DEFAULT_LAYOUT.asset_paths())
    manager = scheme.durable_state_owner
    offenders = [
        line
        for line in _command_lines(scheme)
        if line.startswith("setfacl")
        and f"u:{manager}:" in line
        and (line.endswith(" " + root) or f" {root}/" in line)
    ]
    assert offenders == [], offenders


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_the_job_tree_setfacl_lines_are_exactly_the_rule(scheme) -> None:
    """job 樹那一段的 `setfacl` 逐字等於 `JOB_WORKSPACE_REACH` 導出的那幾條（#710）。

    這是上一條的另一半：Manager 不得出現，而**該出現的那幾條要逐字對上**——少一條
    是 #710 的原症狀（job `chdir` 不進去），多一條就是一個沒有人宣告過的授權面。
    順帶釘住 `-R`：只在樹根下一條 ACL 的話，job 進得去卻讀不到裡面任何東西。
    """

    root = _job_tree_root(DEFAULT_LAYOUT.asset_paths())
    reach = registry.job_workspace_reach_for(Principal.BUILDER)
    job_account = scheme.resolve(reach.persona)
    expected = [
        f"setfacl -R -m u:{job_account}:{reach.access_perms} {root}",
        f"setfacl -R -d -m u:{job_account}:{reach.default_perms} {root}",
    ]
    for reader_principal, reader_perms in reach.extra_reader_perms:
        account = scheme.resolve(reader_principal)
        if account is None:  # two-way／three-way 沒有 gate
            continue
        expected += [
            f"setfacl -R -m u:{account}:{reader_perms} {root}",
            f"setfacl -R -d -m u:{account}:{reader_perms} {root}",
        ]
    found = [
        line
        for line in _command_lines(scheme)
        if line.startswith("setfacl")
        and (line.endswith(" " + root) or f" {root}/" in line)
    ]
    assert found == expected


@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_the_job_tree_section_still_sets_owner_and_mode(scheme) -> None:
    """反向守衛：ACL 沒了，但 chown／chmod 這兩條**必須**還在（不是整段被刪掉）。"""

    root = _job_tree_root(DEFAULT_LAYOUT.asset_paths())
    lines = _command_lines(scheme)
    assert any(line.startswith("chmod 0700 ") and line.endswith(root) for line in lines)
    assert any(line.startswith("chown ") and line.endswith(root) for line in lines)


def test_the_rationale_no_longer_claims_a_manager_read() -> None:
    """operator review 的是 rationale 那一行——它不能還在宣稱「Manager 以唯讀 ACL 讀取」。

    **#710 換掉第三條斷言的理由**：`"owner-only" in rationale` 驗的是「job 樹是
    owner-only、零 ACL」那個**從未存在**的形態（見
    `test_repo_worktree_is_manager_owned_with_a_named_job_acl`）。換成驗它現在**必須
    講出來**的那件事——「owner 是 Manager、job 走具名 ACL、而 `chown` 不是選項」。
    前兩條（不得再宣稱 D2 git 讀／trusted reader）**一字未改**：#641 收掉的那條
    Manager 讀取權沒有回來。
    """

    entry = generate_plan(THREE_WAY).by_id("repo-worktree")
    assert "D2 git 讀" not in entry.rationale
    assert "trusted reader" not in entry.rationale
    assert "CAP_CHOWN" in entry.rationale
    assert "具名 ACL" in entry.rationale


# ---------------------------------------------------------------------------
# 3. 突變驗證：把修法前的形狀放回去，上面那些斷言必須當場紅
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scheme", ALL_SCHEMES, ids=SCHEME_IDS)
def test_the_pre_641_registry_shape_would_still_fail_this_fixture(scheme) -> None:
    """沒有這一條，上面的斷言有可能哪天只是因為 fixture 不再涵蓋 job 樹而空過。"""

    manager = scheme.durable_state_owner
    grants = [
        row
        for row in cross_account_grants_into_the_job_tree(_pre_641_registry(), scheme)
        if row[1] == manager
    ]
    # **#710 只加了一道過濾，判準本身未變**：修法後 job 樹底下本來就會有 job 帳號
    # 自己那兩條具名 ACL（那是 #710 的修法本體），突變驗證要看的仍然是「Manager
    # 有沒有跟著回來」——不濾掉的話這條會被修法本體的那幾列蓋過去，變成永遠成立。
    if scheme is TWO_WAY:
        # 二分下 reviewer／planner 與 Manager 併帳，`review-verdict` 的 owner 就是
        # Manager 自己，那一條退化成 no-op；另外兩條照樣復發。
        #
        # #710：`repo-worktree` 的 owner 在二分下也是 Manager（`durable_state_owner`
        # ＝`cortex-svc`），因此把 MANAGER 放回 readers 同樣是 no-op，只剩
        # `work-items-yaml` 復發。少掉的那一格由本檔第 1 節的形態斷言承接。
        assert {asset_id for asset_id, _, _ in grants} == {"work-items-yaml"}
        return
    # 三分／四分：`repo-worktree` 的 owner 是 Manager，因此把 MANAGER 放回它的
    # readers 一樣是 no-op（產生器對 owner 自己不出 ACL）——這正是 #710 之後
    # 「Manager 讀 job 樹」不再是一條**可被 ACL 表達**的東西的直接後果，而不是
    # 突變驗證失效：另外兩格照樣復發，且全部指向 Manager。
    assert {asset_id for asset_id, _, _ in grants} == {
        "review-verdict",
        "work-items-yaml",
    }
    assert grants, "突變後一條都沒復發 ⇒ 這條測試證不出東西"


def test_the_pre_641_shape_reopens_the_derived_traverse_into_the_job_tree() -> None:
    """#620 的 traverse 是機械導出的：把讀取權放回去，job 樹裡的 `--x` 也跟著回來。

    這正是「三條必須一起收」的理由——留任何一條，traverse 那一側就會自己長回來。
    """

    paths = DEFAULT_LAYOUT.asset_paths()
    root = _job_tree_root(paths)
    grants = permgen.derive_traverse_grants(
        generate_plan(THREE_WAY, _pre_641_registry()), scheme=THREE_WAY, path_of=paths
    )
    reopened = [g for g in grants if g.path.startswith(root + "/") or g.path == root]
    assert reopened, "突變後 traverse 沒有復發 ⇒ 這條測試證不出東西"


# ---------------------------------------------------------------------------
# 4. OS 層不變式：照登記表建出來的樹，別的 uid 真的讀不到
#
# 這一組是 #638 明說的那類「單 UID 環境測不出來」的東西：非 root 時**明確 skip**，
# 不靜默通過。結構性的保證由上面第 1／2 節承擔。
# ---------------------------------------------------------------------------

_CROSS_UID_SKIP = (
    "「Manager 讀不到 job 工作樹」是 OS 層語意，需要 root 才借得到 job／Manager 兩個 uid "
    "實跑；單 UID 環境下 owner 就是自己，這條不變式在那裡沒有可驗的語意——"
    "刻意 skip 而非空過（#638 的教訓）。結構性保證由本檔的權限計畫／產生器斷言承擔。"
)

_JOB_UID = 60011
_MANAGER_UID = 60012

_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_MASK, _ACL_OTHER = 0x01, 0x02, 0x04, 0x10, 0x20
_ACL_UNDEFINED_ID = 0xFFFFFFFF
_R, _W, _X = 4, 2, 1


def _pre_641_read_acl(reader_uid: int) -> bytes:
    """修法前那一條 `setfacl -m u:cortex-manager:rX <job 樹>` 的 xattr 形式。

    走 `system.posix_acl_access`（`setfacl` 用的同一個核心介面），不依賴 `acl`
    套件裝了沒。
    """

    payload = struct.pack("<I", 2)
    for tag, perm, ident in (
        (_ACL_USER_OBJ, _R | _W | _X, _ACL_UNDEFINED_ID),
        (_ACL_USER, _R | _X, reader_uid),
        (_ACL_GROUP_OBJ, 0, _ACL_UNDEFINED_ID),
        (_ACL_MASK, _R | _W | _X, _ACL_UNDEFINED_ID),
        (_ACL_OTHER, 0, _ACL_UNDEFINED_ID),
    ):
        payload += struct.pack("<HHI", tag, perm, ident)
    return payload


def _run_as(uid: int, fn) -> int:
    """在 fork 出來的子進程裡以 `uid` 執行 `fn`。0＝成功、1＝被拒、2＝其他例外。"""

    pid = os.fork()
    if pid == 0:  # pragma: no cover - 子進程
        code = 2
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            fn()
            code = 0
        except PermissionError:
            code = 1
        except OSError as exc:
            code = 1 if exc.errno in {1, 13} else 2
        except BaseException:
            code = 2
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status)


@pytest.fixture()
def job_tree():
    """照登記表把 `repo-worktree` 那一格建出來：`0700`、owner＝job 帳號、零 ACL。

    刻意用 `tempfile.mkdtemp()` 而不是 `tmp_path`：借來的 uid 必須 traverse 得進來，
    而 pytest 的 tmp 根鏈不保證是可穿越的（與 #638 同一個理由）。
    """

    root = Path(tempfile.mkdtemp(prefix="psc-641-"))
    os.chmod(root, 0o755)
    tree = root / "job-641-0001"
    entry = generate_plan(THREE_WAY).by_id("repo-worktree")
    tree.mkdir()
    os.chmod(tree, entry.mode)
    (tree / "committed.txt").write_text("builder-owned\n", encoding="utf-8")
    os.chown(tree / "committed.txt", _JOB_UID, _JOB_UID)
    os.chown(tree, _JOB_UID, _JOB_UID)
    try:
        yield tree
    finally:
        os.chmod(tree, 0o700)
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(os.geteuid() != 0, reason=_CROSS_UID_SKIP)
def test_manager_cannot_read_a_job_worktree_built_to_the_registry(job_tree: Path) -> None:
    """照登記表部署之後，Manager 對 job 工作樹**確實**讀不到（#637 的不變式在實機成立）。"""

    assert stat.S_IMODE(os.stat(job_tree).st_mode) == 0o700
    assert os.stat(job_tree).st_uid == _JOB_UID

    def _list() -> None:
        os.listdir(job_tree)

    def _open() -> None:
        (job_tree / "committed.txt").read_text(encoding="utf-8")

    assert _run_as(_MANAGER_UID, _list) == 1, "Manager 列得出 job 樹的內容"
    assert _run_as(_MANAGER_UID, _open) == 1, "Manager 讀得到 job 樹裡的檔案"
    # 正向對照：job 帳號自己當然讀得到——不是把整棵樹鎖到誰都不能用。
    assert _run_as(_JOB_UID, _list) == 0
    assert _run_as(_JOB_UID, _open) == 0


@pytest.mark.skipif(os.geteuid() != 0, reason=_CROSS_UID_SKIP)
def test_the_pre_641_acl_would_have_made_this_fixture_pass_wrongly(job_tree: Path) -> None:
    """突變驗證（OS 層）：把修法前那條 ACL 套回去，上一條的斷言必須反轉。

    沒有這一條，上面那個 `Permission denied` 有可能只是因為 fixture 根本沒建對東西。
    """

    try:
        os.setxattr(job_tree, spool_slot.ACCESS_ACL_XATTR, _pre_641_read_acl(_MANAGER_UID))
    except OSError as exc:  # pragma: no cover - 取決於執行環境的檔案系統
        pytest.skip(
            f"此檔案系統不支援 POSIX ACL（設定 {spool_slot.ACCESS_ACL_XATTR} 失敗：{exc}）；"
            "沒有 ACL 就重現不了修法前的形狀——刻意 skip 而非空過"
        )

    def _list() -> None:
        os.listdir(job_tree)

    assert _run_as(_MANAGER_UID, _list) == 0, (
        "套上修法前的 `u:<manager>:rX` 之後 Manager 仍讀不到 ⇒ 這個 fixture 分辨不出"
        "兩種形狀，前一條測試證不出東西"
    )


# ---------------------------------------------------------------------------
# 5. verification：讀不到工作樹時明確 fail-closed，理由碼指向 #629
# ---------------------------------------------------------------------------

#: git 2.43.0 對一個 `0700`、屬於別的 uid 的真實 repo 逐字吐出來的東西（實測，不是
#: 猜的——`rev-parse HEAD`／`status --porcelain`／`merge-base --is-ancestor` 三種形狀
#: 全部相同、rc 皆為 128）。理由碼的判定就靠這個字串，因此它必須是實測值。
_DENIED_STDERR = (
    "fatal: cannot change to '/var/lib/cortex/worktree/job-641-0001': Permission denied"
)


def _git_ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _git_denied() -> SimpleNamespace:
    return SimpleNamespace(returncode=128, stdout="", stderr=_DENIED_STDERR)


def _contract() -> dict:
    return {
        "docs_class": "code",
        "review_policy": "required",
        "required_artifacts": [],
        "checks": [
            {"kind": "persona-scope"},
            {
                "kind": "command",
                "name": "policy",
                "argv": ["python3", "-m", "pytest", "-q", "tests/policy.py"],
                "cwd": ".",
                "timeout_seconds": 30,
            },
        ],
        "tests": [],
        "full_suite": {
            "argv": ["python3", "-m", "pytest", "-q"],
            "cwd": ".",
            "timeout_seconds": 60,
            "baseline": "no-regression",
        },
    }


def _slice_row(contract: dict, *, dispatch_base: str, worktree: Path) -> dict:
    return {
        "slice_id": "slice-641",
        "target_branch": "main",
        "dispatch_base": dispatch_base,
        "builder_job_id": "slice-641-1",
        "state": "building",
        "gate_state": "pending",
        "worktree": str(worktree),
        "verification": {
            "hash": verification.canonical_json_hash(contract),
            "contract": contract,
        },
    }


def _job(worktree: Path) -> dict:
    return {
        "job_id": "slice-641-1",
        "task": "slice-641",
        "persona": "builder",
        "branch": "feature/slice-641",
        "worktree": str(worktree),
        "status": "exited",
        "exit_code": 0,
    }


class _WorktreeDenyingGitRunner:
    """來源樹的 git 一切正常；凡是 `-C <job 樹>` 一律回實機那句 Permission denied。"""

    def __init__(self, *, repo_root: Path, worktree: Path, candidate: str, deny: bool = True) -> None:
        self._repo_root = str(repo_root)
        self._worktree = str(worktree)
        self._candidate = candidate
        self._deny = deny
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]):
        self.calls.append(list(args))
        if len(args) >= 2 and args[0] == "-C" and args[1] == self._worktree:
            if self._deny:
                return _git_denied()
            return SimpleNamespace(returncode=1, stdout="", stderr="fatal: not a git repository")
        if args[:2] == ["-C", self._repo_root] and args[2:] == ["rev-parse", "feature/slice-641"]:
            return _git_ok(self._candidate)
        raise AssertionError(f"unexpected git call: {args!r}")


class _ExplodingSubprocessRunner:
    """verification 一旦以 `cwd=<job 樹>` 執行任何命令就當場失敗。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        raise AssertionError(
            f"Manager 在讀不到工作樹的情況下仍執行了命令：{argv!r} cwd={kwargs.get('cwd')!r}"
        )


def _run_verification(tmp_path: Path, *, deny: bool = True):
    repo_root = tmp_path / "source"
    repo_root.mkdir()
    worktree = tmp_path / "worktree" / "job-641-0001"
    worktree.mkdir(parents=True)
    candidate = "b" * 40
    git_runner = _WorktreeDenyingGitRunner(
        repo_root=repo_root, worktree=worktree, candidate=candidate, deny=deny
    )
    proc_runner = _ExplodingSubprocessRunner()
    evidence = verification.run_result_verification(
        slice_row=_slice_row(_contract(), dispatch_base="a" * 40, worktree=worktree),
        job=_job(worktree),
        repo_root=repo_root,
        coordinator_root=tmp_path / "coordinator",
        git_runner=git_runner,
        subprocess_runner=proc_runner,
    )
    return evidence["payload"], git_runner, proc_runner


def test_verification_fails_closed_with_a_reason_pointing_at_629(tmp_path: Path) -> None:
    """讀不到 job 工作樹 ⇒ needs_human ＋ 專屬理由碼 ＋ evidence 裡逐字帶出 #629。"""

    payload, _git, proc = _run_verification(tmp_path)

    assert payload["status"] == "needs_human"
    assert payload["summary"] == verification.WORKTREE_READ_BLOCKED_SUMMARY
    blocked = payload["details"]["candidate_worktree_blocked"]
    assert blocked["blocked_on"] == "#629"
    assert "#629" in blocked["detail"]
    # 處置說明必須真的指得出下一步，不是只丟一個票號。
    assert "第三執行身分" in blocked["detail"]
    assert blocked["git"]["stderr"] == _DENIED_STDERR
    # 而且**沒有**在讀不到的情況下執行任何 builder 掌控的程式碼。
    assert proc.calls == []


def test_verification_does_not_silently_pass_or_fall_back(tmp_path: Path) -> None:
    """三條不得走的路：不靜默通過、不改讀 bundle、不採信 builder 自報。"""

    payload, git, _proc = _run_verification(tmp_path)

    assert payload["status"] not in {"verified", "reviewing"}
    assert payload["details"]["checks"] == []
    assert payload["details"]["tests"] == []
    assert payload["details"]["full_suite"]["status"] == "skipped"
    # 讀不到之後就停手：不會再有第二次對工作樹的嘗試（改用別的方式硬取）。
    worktree_calls = [c for c in git.calls if c[:1] == ["-C"] and "worktree" in c[1]]
    assert len(worktree_calls) == 1, worktree_calls


def test_a_plain_broken_worktree_keeps_the_original_reason(tmp_path: Path) -> None:
    """反向：不是權限問題時，理由碼**不得**被換成指向 #629 的那一個。

    兩者的處置完全不同——一個是等 #629，一個是那棵樹壞了要人去看。
    """

    payload, _git, _proc = _run_verification(tmp_path, deny=False)

    assert payload["summary"] == "candidate-worktree-unreadable"
    assert "candidate_worktree_blocked" not in payload["details"]


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: cannot change to '/x': Permission denied",
        "PermissionError: [Errno 13] Permission denied: '/x'",
        "ls: cannot access '/x': Permission denied",
        "mkdir: cannot create directory: Operation not permitted",
    ],
)
def test_worktree_read_blocked_recognises_the_real_shapes(stderr: str) -> None:
    assert verification.worktree_read_blocked({"status": "non-zero", "stderr": stderr}) is True


@pytest.mark.parametrize(
    "result",
    [
        {"status": "ok", "stderr": "Permission denied"},  # 讀到了就是讀到了
        {"status": "non-zero", "stderr": "fatal: not a git repository"},
        {"status": "non-zero", "stderr": ""},
        {"status": "runner-error", "stderr": "boom"},
    ],
)
def test_worktree_read_blocked_does_not_over_claim(result: dict) -> None:
    assert verification.worktree_read_blocked(result) is False


# ---------------------------------------------------------------------------
# 6. canonical lane（`manager._verify_exact_candidate`）：同一條規則
# ---------------------------------------------------------------------------

def test_workflow_candidate_verification_points_at_629_when_denied() -> None:
    """canonical lane 也是 Manager 伸手進 builder 的樹——照樣 fail-closed，訊息指向 #629。"""

    job = {
        "subject_head": "b" * 40,
        "worktree": "/var/lib/cortex/worktree/job-641-0001",
        "persona": "builder",
    }

    def git_runner(argv, **_kwargs):
        return _git_denied()

    with pytest.raises(ValueError) as excinfo:
        manager._verify_exact_candidate(job, git_runner=git_runner)
    assert "#629" in str(excinfo.value)
    assert "unreadable by manager" in str(excinfo.value)


def test_workflow_candidate_verification_keeps_the_original_message_otherwise() -> None:
    """不是權限問題時訊息一字未變（既有處置與既有稽核字串不受影響）。"""

    job = {
        "subject_head": "b" * 40,
        "worktree": "/tmp/does-not-matter",
        "persona": "builder",
    }

    def git_runner(argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="fatal: not a git repository")

    with pytest.raises(ValueError, match="workflow candidate does not exist"):
        manager._verify_exact_candidate(job, git_runner=git_runner)


def test_reviewer_lane_never_reads_the_reviewer_worktree() -> None:
    """reviewer 側同型殘留的複驗：candidate 驗證讀的是 Manager 自己的來源樹。

    `_verify_exact_candidate` 對 reviewer persona 取 `workflow_repo_root`（
    `repo-source-tree`，Manager-owned）而不是 `worktree`——因此 reviewer 的工作樹
    從頭到尾沒有被 Manager 讀過，#641 的問題在這條 lane 上不存在。
    """

    seen: list[str] = []

    def git_runner(argv, **_kwargs):
        seen.append(argv[2])
        return _git_ok("b" * 40)

    job = {
        "subject_head": "b" * 40,
        "persona": "reviewer",
        "workflow_repo_root": "/var/lib/cortex/repos/paulsha-cortex",
        "worktree": "/var/lib/cortex/worktree/reviewer-job-0001",
    }
    assert manager._verify_exact_candidate(job, git_runner=git_runner) == "b" * 40
    assert seen and all(path == "/var/lib/cortex/repos/paulsha-cortex" for path in seen)
