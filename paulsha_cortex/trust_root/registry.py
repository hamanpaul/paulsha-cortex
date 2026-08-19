"""R1：trust-root 資產登記表——單一機器可讀真相。

spec §R1 要求把所有 security-relevant durable state 與 mutation ingress 固化為
**單一模組常數**（非散落字串），對每項資產宣告
`asset_id`／`tier`／`path_resolver`／`writers`／`readers`／`ingress_kind`，並以
機械測試釘住雙向等式：`config/paths.py`、`control/constants.py` 中**每一個回傳
durable path 的函式** SHALL 在登記表中有對應項目，新增一個 path 函式而未登記即 FAIL。

本登記表同時是 Phase 2 權限產生器的輸入（宣告式：哪些資產屬 Manager-owned 樹、
哪些屬 job-visible 樹），因此把裁決 10-1／10-2 的兩棵樹分類固化在此：

- **Manager-owned 樹**（`TrustTree.MANAGER_OWNED`）：只有受信任身分（Manager 專屬
  UID，裁決 10-1）可寫；全部 headless persona 不可寫。
- **job-visible 樹**（`TrustTree.JOB_VISIBLE`）：對應 headless UID 可在自己的
  worktree／staging 區寫入，但 reviewer 與 builder 互不可寫（裁決 10-2 硬性要求）。

Phase 1 **不**產生任何實際權限、**不**建 UID、**不** chmod；本模組純為宣告。
"""
from __future__ import annotations

import importlib
import inspect
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class AssetTier(Enum):
    """spec §資產分級 A。"""

    TIER_0 = 0  # authority-bearing：竄改後可直接偽造 acceptance 或取得 ship authority
    TIER_1 = 1  # integrity-bearing：竄改導致錯誤決策但不直接授權發佈
    TIER_2 = 2  # advisory：顯示性欄位、文字 marker、宣告式 persona write_paths


class TrustTree(Enum):
    """裁決 10-1／10-2 的兩棵樹。Phase 2 權限產生器以此決定目錄 owner／mode。"""

    MANAGER_OWNED = "manager-owned"  # 僅 Manager 專屬 UID 可寫；全 headless 不可寫
    JOB_VISIBLE = "job-visible"      # 對應 headless UID 可在自己區域寫；跨 persona 互不可寫


class Principal(Enum):
    """writer／reader 身分。headless persona 三者必須分別可辨（spec §R1）。"""

    MANAGER = "manager"
    MONITOR = "monitor"
    INSTALLER = "installer"          # 部署身分（Phase 2 收斂為 root／部署帳號）
    OPERATOR = "operator"
    BUILDER = "builder"              # headless
    REVIEWER = "reviewer"            # headless
    PLANNER = "planner"              # headless
    GATE = "gate"                    # #629：operator 宣告的 gate 命令的執行身分
    HEADLESS_HOOK = "headless-hook"  # D5 headless 事件 hook（寫 event-spool）
    EXTERNAL = "external"            # 外送管線／外部學習系統（唯讀）
    ANY_SAME_UID = "any-same-uid"    # 現況：同 UID 任何行程皆可寫（含全部 headless persona）


#: 三個不受信任 headless persona；spec §R1 要求盤點必須分別涵蓋，不能只封 builder。
#:
#: **判準是「這個 principal 會執行一個模型 CLI」**，因此 #629 的 :data:`Principal.GATE`
#: 不在其中：gate 不跑模型，它跑的是 operator 宣告的命令。兩者在「不受信任」這件事上
#: 完全同級（見 :data:`UNTRUSTED_EXECUTION_PRINCIPALS`），差別只在**需要哪些前置物**
#: ——模型 job 帳號要有 root-owned 的 `~/.codex` 與 executor 憑證檔，gate 一個都不要。
HEADLESS_PERSONAS: frozenset[Principal] = frozenset(
    {Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER}
)

#: **全部「執行不受信任程式碼」的身分**——Manager-owned／deployment 樹對這些帳號必須
#: 零寫入權（`permgen.UidScheme.headless_accounts()` 的來源，也是產生器的核心不變式）。
#:
#: #629 起它比 :data:`HEADLESS_PERSONAS` 多兩項：
#:
#: - `HEADLESS_HOOK`（D5 事件 hook，與 builder 同帳號）；
#: - `GATE`——operator 宣告的 gate 命令是在 **builder 完全掌控內容的工作樹**上跑的，
#:   `pytest` 會載入該樹的 `conftest.py`／plugin，所以「執行 gate」與「執行模型交出來
#:   的程式碼」在信任面上是同一件事。它必須被關進與 builder 同級的盒子，只是**不同的
#:   那一個**盒子（同一個盒子＝模型自證，見 issue #540 的 acceptance chain）。
UNTRUSTED_EXECUTION_PRINCIPALS: frozenset[Principal] = (
    HEADLESS_PERSONAS | {Principal.HEADLESS_HOOK, Principal.GATE}
)

#: **具備啟動面降權的 job principal**——各有一組 root-owned 模板 unit，因此各有一個
#: **自己的** spec spool（#657）。這張表是那一族的唯一清單：登記表資產
#: （:func:`job_spec_spool_asset_id`）、路徑（`config.paths.job_spec_spool_for`）、
#: unit 的 `Environment=PSC_JOB_SPEC_SPOOL=`、polkit 的 unit 字幹全部由它導出。
#:
#: - `BUILDER`（M1，#603／#584）：`cortex-job@.service`／`cortex-job-jit@.service`。
#: - `REVIEWER`（M2，#615）：`cortex-reviewer-job@.service`／`-jit`，
#:   `User=cortex-reviewer-planner`。
#: - `GATE`（#629）：`cortex-gate-job@.service`／`-jit`，`User=cortex-gate`。
#:
#: **`PLANNER` 刻意不在表內，而且不是遺漏**：三分／四分方案把 reviewer 與 planner
#: 映到**同一個帳號**，而 unit 與 spool 的全部內容差異都由帳號決定。為 planner 再
#: 產一份逐字相同、只是名字不同的 unit 與 spool，等於多一個要同步維護的放行面，
#: 卻換不到任何隔離。`REVIEWER` 在這裡是**那個帳號的代表 principal**，由
#: `permgen.JOB_PRINCIPAL_PERSONAS` 明載它代表誰。
#:
#: **本表是「產得出哪幾份 unit／spool」，不是「哪些執行路徑真的走上它」（#687）。**
#: `REVIEWER` 這一格從 #615 起就在表上，但 planner 的 define／brainstorm 直到
#: #672 票 A～F（#682-#687）才真的以它起 job——在那之前 `job-specs/reviewer/`
#: 從未被寫過任何一個 spec（#686 查證、#687 於實機複驗）。判定執行路徑的是
#: `coordinator/planning_runtime._select_planning_invoker()` 與
#: `coordinator/launcher.SubprocessLauncher._job_role()`，不是本表。
#: （本段與 `permgen.DOWNGRADED_JOB_PRINCIPALS` 的同一段是**成對**的，改一邊
#: 就要改另一邊——兩者是同一個 tuple 物件的兩份說明。）
#:
#: 定義在登記表而非 `permgen`：permgen import registry（反向不成立），而本清單同時
#: 要決定**登記表有哪些資產**。放在 permgen 會讓資產清單得從產生器 import 回來。
#: `permgen.DOWNGRADED_JOB_PRINCIPALS` 是指向本項的別名，不是第二份。
DOWNGRADED_JOB_PRINCIPALS: tuple[Principal, ...] = (
    Principal.BUILDER,
    Principal.REVIEWER,
    Principal.GATE,
)


@dataclass(frozen=True)
class JobLogSpool:
    """一個降權 job principal 的 **job log 落點**宣告（#708）。

    這張表的存在理由是一條**機制事實**，不是偏好：`coordinator/job_shim.py` 的
    `_take_over_stdio()` 在**接管 stdio 之前**就 `os.open(log_path)`，因此凡經模板
    unit 派出的 job，只要那個路徑寫不進去，它連一行 log 都寫不出來就死
    ——**失敗發生在它能記錄失敗之前**，Manager 端只看得到 `78/CONFIG`。

    ⇒ **凡在 :data:`DOWNGRADED_JOB_PRINCIPALS` 上的 principal，一律要有一格。**
    """

    #: :data:`DOWNGRADED_JOB_PRINCIPALS` 上的那一格（＝哪一份模板 unit／哪一個帳號）。
    principal: Principal
    #: 真正走這條通道的 persona。**reviewer 那一列是 `PLANNER`**——三分／四分下兩者
    #: 是同一個 OS 帳號（ACL 逐字相同），宣告成 PLANNER 是為了讓登記表講的是「誰在用
    #: 這條通道」：reviewer 的通道是 `review-verdict-spool` 本身，兩條不共用。
    writer: Principal
    asset_id: str
    #: 掛在**哪一條既有的輸出通道**底下。`_assert_job_log_spools_hang_off_existing_channels()`
    #: 在 import 當下強制它真的是一個已登記、且該 writer 已經寫得進去的資產。
    channel_asset_id: str
    #: 掛在通道根底下的目錄名。與 `config/paths.py` 的同名常數是**成對契約**
    #: （本模組不被 `config.paths` import，反向亦然）。
    dirname: str
    #: 這一格的專屬說明（其餘 note 由規則產生）。
    note: str


#: **#708 那條規則的表**：三個 principal 一格都不能少。
#:
#: 每一列的 `channel_asset_id` 都是該 principal **今天就已經寫得進去**的落點，因此：
#:
#: 1. **不新增任何寫入面**——那個帳號本來就寫得進這棵樹，多的只是它自己那一格；
#: 2. `permgen.read_write_paths()` 的 `_minimize()` 把被涵蓋的子路徑吃掉 ⇒ 模板 unit
#:    的 `ReadWritePaths=` **逐字不變、default ACL 自動繼承、零部署動作**；
#: 3. 仍是**獨立登記表資產**——各自有 note、writer／reader 面、ACL 命令，治理面沒有
#:    因為省下一條 RWP 而消失。
#:
#: **為什麼是一張表而不是三處手寫**：#686（票 E）只做了 planner 那一格，代價是 #708
#: ——define 首次收斂、builder job 第一次由 daemon 經正規路徑派出來就當場死。同一族的
#: 前例是 #679（修 PATH 沒看隔壁的 HOME ⇒ #692）。導出之後，「只修一格」在**結構上
#: 做不到**：新增第四個降權角色而漏了這裡，模組**載不起來**
#: （:func:`_assert_every_downgraded_principal_has_a_job_log_spool`）。
JOB_LOG_SPOOLS: tuple[JobLogSpool, ...] = (
    JobLogSpool(
        principal=Principal.BUILDER,
        writer=Principal.BUILDER,
        asset_id="build-job-log-spool",
        channel_asset_id="commit-spool",
        dirname="build-logs",
        note=(
            "**#708 的原症狀就在這一格。** builder job 的 log 原本落在 Manager 的 "
            "dispatch log 目錄（`<coordinator_root>/logs/workflow/<slice>.jsonl`），而"
            "那個目錄 `0700 cortex-manager`、零具名 ACL ⇒ shim 連 traverse 都進不去"
            "（實機 `[Errno 13] Permission denied`，unit `78/CONFIG`）。\n"
            "**那個目錄刻意不開放**：gate ledger（`<slice>.gates.json`）與 exit "
            "sentinel（`<slice>.exit`）住在同一層，而 #604 的整個保證就是「它們由 "
            "Manager 寫、採信端以 `foreign_evidence_author()` 檢查擁有者」。給 builder "
            "一條進得去那一層的 RWP，等於把剛關上的門重新打開——因此 log 改掛在 "
            "`commit-spool` 底下，Manager 端那條 harvest 路徑則以 **hard link** 對上"
            "同一個 inode（見 `coordinator/job_workspace.py:prepare_job_log_spool`）："
            "`log_path` 的字面量、exit sentinel、gate ledger、spool key 的推導"
            "**一個位元組都沒有變**。"
        ),
    ),
    JobLogSpool(
        principal=Principal.REVIEWER,
        writer=Principal.PLANNER,
        asset_id="planning-job-log-spool",
        channel_asset_id="review-verdict-spool",
        dirname="planning-logs",
        note=(
            "#686（#672 票 E）：降權 planning job 的輸出通道。planning 搬上 "
            "`cortex-reviewer-job@.service` 之後，模型 stdout 不再由 "
            "`subprocess.run(capture_output=True)` 取回；這一格就是 design D-i 的 job "
            "側對應，per-invocation 生命週期走 `coordinator/spool_slot.py`。\n"
            "**#708 起它是規則的一列而不是特例**，路徑與行為逐字不變（實機零遷移）。"
        ),
    ),
    JobLogSpool(
        principal=Principal.GATE,
        writer=Principal.GATE,
        asset_id="gate-job-log-spool",
        channel_asset_id="gate-ledger-spool",
        dirname="gate-logs",
        note=(
            "#708：gate job 的輸出通道。**gate 這一格的病徵與 builder 不同**——它的 "
            "log 原本就落在 ledger spool 的 per-job 那一格裡"
            "（`<gate-ledger-spool>/<key>/gate.log`），因此 gate **寫得進去**、不會像 "
            "builder 那樣當場死；但那個檔是 **job 自己建的**，帶降權 unit 的 "
            "`UMask=0077` ⇒ owner 是 `cortex-gate`、mode `0600`，Manager 是目錄的 owner "
            "但那不給檔案內容的讀取權（#638 缺陷 2 的同一個機制）。結果是 gate 失敗時"
            "**逐字原因只存在於一個 Manager 讀不到的檔裡**。\n"
            "改掛到獨立的一格之後，log 檔改由 **Manager 預先建立**（mode `0620`，與"
            "planning 逐字相同的理由），Manager 因此讀得到，`gate_runner` 的錯誤訊息"
            "也才帶得出 job 端的 tail。"
        ),
    ),
)


#: `JobLogSpool.dirname` 允許的形狀。理由見
#: :func:`_assert_every_downgraded_principal_has_a_job_log_spool`。
_JOB_LOG_DIRNAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def job_log_spool_for(principal: Principal) -> JobLogSpool:
    """該降權 principal 的 job log 落點宣告；查無即 fail-closed（#708）。"""

    for spool in JOB_LOG_SPOOLS:
        if spool.principal is principal:
            return spool
    raise KeyError(
        f"{principal.value} 沒有登記 job log spool——凡經模板 unit 派出的 job 都要"
        "寫 log（#708）。"
    )


def _assert_every_downgraded_principal_has_a_job_log_spool() -> None:
    """#708 的那條規則，在 **import 當下**強制。

    **規則**：`principal` 在 :data:`DOWNGRADED_JOB_PRINCIPALS` 上有一格
    ⇒ 它在 :data:`JOB_LOG_SPOOLS` 上**必須**也有恰好一格。

    為什麼是 import 當下而不是一條測試：#686 的破口不是「有人寫錯一行」，是「三個
    principal 的 log 落點**各自**被決定，於是其中一個先遷、另外兩個留在原地等著爆」。
    在展開點上強制，讓「只修一格」在**結構上做不到**——新增第四個降權角色時漏改的
    症狀是模組載不起來，而不是三個月後實機上的一次 `78/CONFIG`。

    （另一半——「掛的是不是既有通道」——需要 scheme 與 layout，因此由
    `permgen._assert_job_log_spools_hang_off_existing_channels()` 在它那一側強制。）
    """

    declared = [spool.principal for spool in JOB_LOG_SPOOLS]
    if len(declared) != len(set(declared)):
        raise ValueError(f"JOB_LOG_SPOOLS 有重複的 principal: {declared}")
    missing = [p for p in DOWNGRADED_JOB_PRINCIPALS if p not in declared]
    if missing:
        raise ValueError(
            f"降權 principal {[p.value for p in missing]} 沒有 job log spool 資產——"
            "模板 unit 的 shim 會在接管 stdio 之前 `os.open()` log，寫不進去的 job "
            "死在它能記錄失敗之前（#708 實機：builder `78/CONFIG`）。"
            "請往 JOB_LOG_SPOOLS 加一列，不要在別處手寫一個路徑。"
        )
    extra = [p for p in declared if p not in DOWNGRADED_JOB_PRINCIPALS]
    if extra:
        raise ValueError(
            f"JOB_LOG_SPOOLS 宣告了非降權 principal {[p.value for p in extra]}——"
            "本表的輸入只有 DOWNGRADED_JOB_PRINCIPALS（沒有模板 unit 就沒有 shim，"
            "也就沒有這一格要解決的問題）。"
        )
    for spool in JOB_LOG_SPOOLS:
        # `dirname` 會被逐字接進一條絕對路徑（`<通道根>/<dirname>`），而那條路徑會
        # 被 permgen 拿去出 `install -d`／`chown`／`setfacl`。容忍 `..`／`/`／空字串
        # 等於把「log 一定落在該 principal 的既有通道之內」交給打字精確度——形狀因此
        # 在**宣告當下**驗，與 `config.paths._SPOOL_PRINCIPAL_RE` 同一條理由。
        if not _JOB_LOG_DIRNAME_RE.match(spool.dirname):
            raise ValueError(
                f"{spool.asset_id}：dirname {spool.dirname!r} 形狀不合法"
                "（只允許 ^[a-z][a-z0-9-]*$）——它會被接進一條會被 chown／setfacl 的"
                "絕對路徑（#708）。"
            )
        if spool.writer not in UNTRUSTED_EXECUTION_PRINCIPALS:
            raise ValueError(
                f"{spool.asset_id}：writer {spool.writer.value} 不在 "
                "UNTRUSTED_EXECUTION_PRINCIPALS 上——那樣 `permgen.build_entry()` 不會"
                "為它產出任何 `wx` ACL，job 到了實機一樣寫不進去（#657 的失效模式）。"
            )


_assert_every_downgraded_principal_has_a_job_log_spool()


class WorkspaceReach(Enum):
    """job 取得**自己工作區可達性**的機制（#710）。

    三個降權 principal 各屬其一，而且**現況本來就不同**——`#710` 的要求不是把三者
    改成同一個形狀（那會為了對稱而破壞各自成立的理由），是讓「每個 job 都進得去自己
    的工作區」這條性質由**一張表**導出、由 import 期斷言強制。
    """

    #: pool 根的 owner 就是該 job 帳號，per-job 那一格由 job **自己**建立
    #: ⇒ 可達性在部署當下一次成立，執行期零動作。gate 屬此（`gate-worktree-pool`）。
    POOL_OWNED_BY_JOB = "pool-owned-by-job"

    #: pool 根帶該帳號的 **default ACL** ⇒ Manager 在裡面建的每一格自動繼承。
    #: reviewer／planner 屬此（`repo-source-tree` 的 review worktree、
    #: `planning-scratch-pool` 的 per-invocation scratch）。
    #:
    #: **它只在「pool 底下每一格都給同一個帳號」時才合法**：default ACL 是整棵樹的
    #: 性質，給不出 per-job 差別。
    INHERITED_DEFAULT_ACL = "inherited-default-acl"

    #: pool 根**刻意零 default ACL**（多個 job 帳號共用同一個容器），因此可達性只能
    #: 由 Manager 在 provision 當下對**那一格**下具名 ACL。builder 屬此
    #: （`dispatch-worktree-pool` 底下的 `repo-worktree`）。
    #:
    #: ⚠️ **不得改成往 pool 根下 default ACL**：那會讓**每個** job 帳號進得去
    #: **每個** job 的目錄，per-job 隔離當場歸零。這條由
    #: `permgen._assert_job_workspace_reach_matches_the_plan()` 在 import 當下強制。
    PER_JOB_NAMED_ACL = "per-job-named-acl"


@dataclass(frozen=True)
class JobWorkspaceReach:
    """一個降權 job principal 的**工作區可達性**宣告（#710）。

    這張表的存在理由與 :data:`JOB_LOG_SPOOLS` 逐字同型，是一條**機制事實**：
    `coordinator/job_shim.py` 在降權之後、exec 之前 `os.chdir(spec["working_directory"])`
    ——那一步走不進去，job 就死在它做任何事之前（#710 實機：
    `[Errno 13] Permission denied: '/var/lib/cortex/worktree/wf-…'`）。

    ⇒ **凡在 :data:`DOWNGRADED_JOB_PRINCIPALS` 上的 principal，一律要有一格。**
    """

    #: :data:`DOWNGRADED_JOB_PRINCIPALS` 上的那一格。
    principal: Principal
    #: 真正走這條路的 persona（reviewer 那一列同時代表 `PLANNER`，理由與
    #: :class:`JobLogSpool` 的同名欄位逐字相同：三分／四分下兩者是同一個 OS 帳號）。
    persona: Principal
    reach: WorkspaceReach
    #: 該 principal 的工作區**落在哪些登記表資產底下**。多於一項是誠實的：
    #: reviewer 帳號的工作區有兩種（foreign review 的 worktree 在 `repo-source-tree`
    #: 底下、planning 的 scratch 在 `planning-scratch-pool` 底下），兩者都必須成立。
    pool_asset_ids: tuple[str, ...]
    #: per-job 那一格自己的登記表資產 id。**只有 `PER_JOB_NAMED_ACL` 有**——另外
    #: 兩種形態的 per-job 那一格不是獨立資產（gate 自己建、reviewer 繼承）。
    per_job_asset_id: str | None
    #: job 帳號在自己的工作區上**最終必須擁有**的權限位。這是「進得去」這句話的
    #: 可驗形式：反向不變式與執行期的 preflight 檢查比對的就是它（mask 之後的
    #: **有效**權限，`coordinator/job_runner.effective_perms_for_account()`）。
    #: **三種形態一律非空**——不論可達性由 owner 位、繼承或具名 ACL 供給，
    #: 「job 要能做到什麼」都必須寫得出來。
    required_perms: str
    #: 具名 **access** ACL 的 perms（`setfacl -m` 的右手邊）。大寫 `X`＝只有目錄與
    #: **已經可執行**的檔拿到 `x`，遞迴套用時不會把整棵樹的一般檔案變成可執行檔。
    #: **空字串＝本形態不下任何 ACL**（`POOL_OWNED_BY_JOB`，可達性來自 owner 位）。
    access_perms: str
    #: 對應的 **default** ACL perms（`setfacl -d -m` 的右手邊）。
    default_perms: str
    #: 除了 job 自己以外，**同一次** per-job setfacl 還要授予誰（persona → perms）。
    #: builder 那一列的 `GATE: rX` 就是 #629 宣告、但在本票之前只存在於註解裡的那條。
    extra_reader_perms: tuple[tuple[Principal, str], ...]
    note: str


#: **#710 那條規則的表**：三個 principal 一格都不能少。
#:
#: 每一列都是**現況查證後**的形狀，不是統一過的理想形狀——#710 的 issue body 逐字
#: 要求「三者現況不同，不要假設一致」。查證結果：
#:
#: | principal | 工作區 | 誰建 | 可達性怎麼來 |
#: |---|---|---|---|
#: | builder | `<worktree pool>/<job-id>`（per-job clone） | Manager（`seams.ScriptWorktreeCreator`） | **本票補上的那一條**具名 ACL |
#: | reviewer | `<repos>/<slug>/.psc-review-worktrees/<…>`／`<planning-scratch>/<…>/cwd` | Manager（`review.prepare_review_worktree`／`planning_job._prepare_scratch`） | pool 根的 default ACL 繼承 |
#: | gate | `<gate-worktree>/<key>` | **gate 自己**（`gate_ledger.snapshot_worktree`） | pool 根 owner 就是 gate |
#:
#: **為什麼是一張表而不是三處各自成立**：#623／#648 決定 per-job clone 時，
#: 「整個 clone 由本 job 帳號擁有」只被寫進 unit 註解，沒有任何程式實作，而且
#: Manager **結構上做不到**（`chown` 給另一個使用者要 `CAP_CHOWN`，Manager unit 帶
#: `CapabilityBoundingSet=`）。三個 principal 各自被決定，於是其中兩個恰好成立、
#: 一個等到 builder job 第一次由 daemon 經正規路徑派出來才當場死。導出之後，
#: 「只修一格」在**結構上做不到**：新增第四個降權角色而漏了這裡，模組**載不起來**
#: （:func:`_assert_every_downgraded_principal_has_a_workspace_reach`）。
#: 前例：#698 的 `EXECUTOR_ENFORCEMENT_LEAVES`、#708 的 :data:`JOB_LOG_SPOOLS`。
JOB_WORKSPACE_REACH: tuple[JobWorkspaceReach, ...] = (
    JobWorkspaceReach(
        principal=Principal.BUILDER,
        persona=Principal.BUILDER,
        reach=WorkspaceReach.PER_JOB_NAMED_ACL,
        pool_asset_ids=("dispatch-worktree-pool",),
        per_job_asset_id="repo-worktree",
        required_perms="rwx",
        access_perms="rwX",
        default_perms="rwx",
        extra_reader_perms=((Principal.GATE, "rX"),),
        note=(
            "**#710 的原症狀就在這一格。** Manager 以 `git clone --no-hardlinks` 建出 "
            "`<worktree pool>/<job-id>`，而那個目錄的 owner 是 **Manager**、mode `0700`、"
            "零具名 ACL ⇒ builder job 的 shim 在 `os.chdir()` 就 `[Errno 13] "
            "Permission denied`（模板 unit 的 `ReadWritePaths=<pool>/%i` 在 **mount 層**"
            "放行，DAC 層擋死——兩層要同時成立）。\n"
            "**owner 為什麼不能改成 builder**：(a) `chown` 給另一個使用者需要 "
            "`CAP_CHOWN`，Manager unit 的 `CapabilityBoundingSet=` 是空的——這不是漏寫"
            "一行，是方案與降權模型衝突；(b) 就算做得到也不該做——`gc`／"
            "`worktree_reclaim` 要 `rmtree` 整棵樹，那需要樹**內**的寫入權，交出 owner "
            "等於讓工作區回收不了（#478／#601 的生產事故面）。\n"
            "因此 owner 維持 Manager，job 拿**具名 ACL**：`setfacl` 由目錄 owner 執行、"
            "**不需要任何 capability**。\n"
            "**誠實邊界（本票不解、但因為本票才第一次可觀察到）**：builder 終於寫得出"
            "東西之後，樹裡開始出現 **builder 擁有**的 inode。default ACL 讓它們自動帶上"
            "宣告的具名條目（POSIX：目錄帶 default ACL 時 **umask 不生效**，因此 unit 的 "
            "`UMask=0077` 不會把 mask 壓成 `---`——這一點與 #638 缺陷 1 的形狀相反，已實測"
            "確認），所以 **gate 的 `rX` 對 builder 新建的檔仍然成立**。但 **Manager 沒有"
            "在那些 inode 上的任何條目**（它不在 readers 面，#641 收掉了）⇒ "
            "`gc`／`worktree_reclaim` 的 `rmtree` 走不進 builder **新建的子目錄**，工作區"
            "會殘留在磁碟上。這條在「clone 由 job 帳號擁有」的原設計下**更嚴重**（Manager "
            "連樹根都進不去），因此不是本票造成的退步；補上 Manager 的 default ACL 會把 "
            "#641 關掉的那條「Manager 以 cwd=<job 樹> 執行 job 交出來的 conftest.py」提權面"
            "整條打開，故**刻意不補**——回收面的處置要另立一票（候選：由 #629 的 gate 身分"
            "或一支 root-owned 的回收 helper 執行，兩者都不是把讀取權交還給 Manager）。\n"
            "⚠️ **必須下在 per-job 那一格，不能下在 pool 根**：pool 根（`0701 "
            "cortex-manager`）是 builder／reviewer／planner 三個帳號共用的容器，在它身上"
            "下 default ACL 會讓每個 job 帳號進得去每個 job 的目錄，裁決 10-2 的 per-job "
            "隔離當場歸零。這條由 permgen 的 import 期斷言強制（pool 根上出現任何 job "
            "帳號的 default ACL ⇒ 模組載不起來）。"
        ),
    ),
    JobWorkspaceReach(
        principal=Principal.REVIEWER,
        persona=Principal.PLANNER,
        reach=WorkspaceReach.INHERITED_DEFAULT_ACL,
        pool_asset_ids=("repo-source-tree", "planning-scratch-pool"),
        per_job_asset_id=None,
        required_perms="rx",
        access_perms="rX",
        default_perms="rX",
        extra_reader_perms=(),
        note=(
            "reviewer 帳號的工作區**不在** worktree pool 底下（那是 builder 的），因此"
            "它不需要、也不該拿到本票為 builder 補上的那條 `rwx`：\n"
            "  - foreign review：Manager 以 `git worktree add --detach` 在 "
            "`<repo-source-tree>/<slug>/.psc-review-worktrees/` 底下開一棵 linked "
            "worktree（`coordinator/review.py:prepare_review_worktree`）；\n"
            "  - planning／define：Manager 在 `planning-scratch-pool` 底下開一格空的 "
            "per-invocation scratch（`coordinator/planning_job.py:_prepare_scratch`）。\n"
            "兩棵樹的**根**都已經帶 `setfacl -d -m u:<reviewer 帳號>:rX`（登記表的 "
            "`readers` 機械導出），Manager 在裡面建的每一格因此自動繼承 ⇒ 可達性已經"
            "成立，本票對這一格**不新增任何 ACL**。\n"
            "**唯讀是刻意的**：reviewer 的交付通道是 `review-verdict-spool`，planner 的"
            "是 `dispatch-specs-tree`；給工作區寫入權買不到任何東西，卻會把 #628／#639 "
            "關掉的「被驗方在自己的工作區裡產生自己的證據」重新打開。"
        ),
    ),
    JobWorkspaceReach(
        principal=Principal.GATE,
        persona=Principal.GATE,
        reach=WorkspaceReach.POOL_OWNED_BY_JOB,
        pool_asset_ids=("gate-worktree-pool",),
        per_job_asset_id=None,
        required_perms="rwx",
        access_perms="",
        default_perms="",
        extra_reader_perms=(),
        note=(
            "gate 的 pool 根 owner **就是** `cortex-gate`（單一 job writer ⇒ permgen 產出 "
            "`chown cortex-gate` ＋ `0700`），而 per-job 那一格是 gate **自己**用 "
            "`shutil.copytree` 建的（`gate_ledger.snapshot_worktree`）——它天生擁有自己"
            "產出的每一個 inode。可達性因此在部署當下就成立，執行期零動作、零 ACL。\n"
            "**gate 的 cwd 也不是那一格**：spec 的 `working_directory` 是 pool **根**"
            "（`gate_runner._run_as_gate_identity`），因為副本在 unit 起動的當下還不"
            "存在。要驗的性質因此是「gate 進得去自己的 pool 根」。\n"
            "**但 gate 要讀得到被驗的樹**：來源是 builder 的 clone，登記表 "
            "`repo-worktree` 的 `readers` 已宣告 `GATE`（#629），那條 `rX` 與 builder 的 "
            "`rwX` 由**同一次** per-job setfacl 一起落地——在本票之前它與 builder 那條"
            "一樣只存在於註解裡。"
        ),
    ),
)


def job_workspace_reach_for(principal: Principal) -> JobWorkspaceReach:
    """該降權 principal 的工作區可達性宣告；查無即 fail-closed（#710）。"""

    for reach in JOB_WORKSPACE_REACH:
        if reach.principal is principal:
            return reach
    raise KeyError(
        f"{principal.value} 沒有登記工作區可達性——凡經模板 unit 派出的 job 都要 "
        "`chdir` 進自己的工作區（#710）。"
    )


def per_job_named_acl_workspace_assets() -> dict[str, JobWorkspaceReach]:
    """`per_job_asset_id → 宣告`，只含 :attr:`WorkspaceReach.PER_JOB_NAMED_ACL` 那些。

    permgen 以它決定哪些 per-job 資產要走「Manager 擁有 ＋ job 具名 ACL」的形態，
    因此那個形態的**輸入只有本表**，不是產生器裡的一組 asset_id 字面量。
    """

    return {
        reach.per_job_asset_id: reach
        for reach in JOB_WORKSPACE_REACH
        if reach.reach is WorkspaceReach.PER_JOB_NAMED_ACL and reach.per_job_asset_id
    }


def _assert_every_downgraded_principal_has_a_workspace_reach() -> None:
    """#710 的那條規則，在 **import 當下**強制（registry 這一半）。

    **規則**：`principal` 在 :data:`DOWNGRADED_JOB_PRINCIPALS` 上有一格
    ⇒ 它在 :data:`JOB_WORKSPACE_REACH` 上**必須**也有恰好一格，且該格的形態自洽。

    為什麼是 import 當下而不是一條測試：理由與 #698／#708 逐字相同——#710 的破口
    不是「有人寫錯一行」，是「三個 principal 的工作區可達性**各自**被決定，於是其中
    兩個恰好成立、一個在 unit 註解裡宣稱成立但沒有任何程式實作」。在展開點上強制，
    讓「只修一格」在**結構上做不到**。

    （另一半——「宣告的形態與權限計畫是否真的一致」——需要 scheme 與 layout，因此由
    `permgen._assert_job_workspace_reach_matches_the_plan()` 在它那一側強制。）
    """

    declared = [reach.principal for reach in JOB_WORKSPACE_REACH]
    if len(declared) != len(set(declared)):
        raise ValueError(f"JOB_WORKSPACE_REACH 有重複的 principal: {declared}")
    missing = [p for p in DOWNGRADED_JOB_PRINCIPALS if p not in declared]
    if missing:
        raise ValueError(
            f"降權 principal {[p.value for p in missing]} 沒有工作區可達性宣告——"
            "模板 unit 的 shim 在降權之後 `os.chdir(working_directory)`，走不進去的 job "
            "死在它做任何事之前（#710 實機：builder `[Errno 13] Permission denied`）。"
            "請往 JOB_WORKSPACE_REACH 加一列，不要在別處手寫一條 setfacl。"
        )
    extra = [p for p in declared if p not in DOWNGRADED_JOB_PRINCIPALS]
    if extra:
        raise ValueError(
            f"JOB_WORKSPACE_REACH 宣告了非降權 principal {[p.value for p in extra]}——"
            "本表的輸入只有 DOWNGRADED_JOB_PRINCIPALS（沒有模板 unit 就沒有 shim，"
            "也就沒有這一格要解決的問題）。"
        )
    for reach in JOB_WORKSPACE_REACH:
        if not reach.pool_asset_ids:
            raise ValueError(
                f"{reach.principal.value}：沒有宣告任何 pool 資產——「工作區落在哪一棵"
                "已登記的樹底下」是本規則唯一的輸入（#710）。"
            )
        if reach.persona not in UNTRUSTED_EXECUTION_PRINCIPALS:
            raise ValueError(
                f"{reach.principal.value}：persona {reach.persona.value} 不在 "
                "UNTRUSTED_EXECUTION_PRINCIPALS 上——那樣 permgen 不會為它產出任何"
                "跨帳號 ACL，job 到了實機一樣進不去（#657 的失效模式）。"
            )
        has_leaf = reach.per_job_asset_id is not None
        wants_leaf = reach.reach is WorkspaceReach.PER_JOB_NAMED_ACL
        if has_leaf != wants_leaf:
            raise ValueError(
                f"{reach.principal.value}：`per_job_asset_id` 與 reach "
                f"{reach.reach.value} 不相容——具名 ACL 形態**必須**指名那一格是哪一個"
                "登記表資產（要下 setfacl 就要有落點），其餘形態**必須**留 None"
                "（那一格不是獨立資產：gate 自己建、reviewer 繼承）。"
            )
        if not reach.required_perms:
            raise ValueError(
                f"{reach.principal.value}：`required_perms` 為空——「job 進得去自己的"
                "工作區」這句話必須寫得出可驗形式，不論可達性由 owner 位、繼承還是"
                "具名 ACL 供給（#710）。"
            )
        if reach.extra_reader_perms and reach.reach is not WorkspaceReach.PER_JOB_NAMED_ACL:
            raise ValueError(
                f"{reach.principal.value}：只有 PER_JOB_NAMED_ACL 形態有「同一次 "
                "setfacl」可以搭便車，其餘形態的額外 reader 必須自己在登記表上宣告"
                "（#710）。"
            )
        owner_reach = reach.reach is WorkspaceReach.POOL_OWNED_BY_JOB
        if owner_reach == bool(reach.access_perms):
            raise ValueError(
                f"{reach.principal.value}：`access_perms`={reach.access_perms!r} 與 "
                f"reach {reach.reach.value} 對不上——`POOL_OWNED_BY_JOB` 的可達性來自 "
                "**owner 位**，用 ACL perms 描述它會讓人以為某處有一條 setfacl；其餘"
                "兩種形態則必須寫出 job 需要的那組權限，那是 permgen 拿去比對權限計畫"
                "（繼承形態）與執行期真的會下的那條命令（具名形態）的同一個值（#710）。"
            )


_assert_every_downgraded_principal_has_a_workspace_reach()


# ---------------------------------------------------------------------------
# #712：job 對**自己的工作區**跑 git——dubious-ownership 那一層
# ---------------------------------------------------------------------------

#: `GIT_CONFIG_*` 逐 job 注入時**唯一**放行的 git 設定鍵（#712）。
#:
#: 這個字串是本檔與 `coordinator/job_runner.ALLOWED_GIT_CONFIG_KEYS` 的成對契約
#: （`job_runner` 刻意不 import `trust_root`，同 `JOB_LOG_SPOOLS`／
#: :data:`JOB_WORKSPACE_REACH` 的既有模式），由
#: `tests/test_per_job_git_safe_directory_712.py` 釘住兩邊逐字相等。
#:
#: **為什麼是白名單而不是「不要放危險的鍵」**：`GIT_CONFIG_COUNT`／`GIT_CONFIG_KEY_i`
#: ／`GIT_CONFIG_VALUE_i` 是 git 的 **command scope**，與 `git -c` 同級，屬 git 所謂的
#: *protected configuration*——`safe.directory` 只吃這一層正是它的用途。但同一條管道
#: 也吃 `alias.*`／`core.fsmonitor`，而那些鍵**會執行外部命令**。0819 實測（git 2.43.0）::
#:
#:     GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_1=alias.pwn \
#:       GIT_CONFIG_VALUE_1='!echo PWNED-EXTERNAL-COMMAND' git pwn
#:     PWNED-EXTERNAL-COMMAND
#:
#: 三份 `.gitconfig` root-owned 的理由逐字就是這一條（`build_account_gitconfig` 的
#: 註解）。放開這條管道而不限鍵，等於把那條防線從檔案權限繞過去。
GIT_SAFE_DIRECTORY_KEY = "safe.directory"


class GitWorkspaceTrust(Enum):
    """job 在自己的工作區上跑 git 時，dubious-ownership 保護怎麼過（#712）。

    git 的判準只有一條：**repo 的 owner 是不是當下這個 uid**。因此本表不是一個新的
    決定——它是 :data:`JOB_WORKSPACE_REACH` 那一列「誰建那一格」的**直接後果**，由
    :func:`_assert_every_downgraded_principal_has_a_git_workspace_trust` 在 import
    當下把兩邊釘死。
    """

    #: 工作區由 **Manager** 建 ⇒ owner 是 Manager、job 是另一個 uid ⇒ git 必然看到
    #: 跨 owner。放行只能逐 job 給，值＝**這一個** job 的工作區絕對路徑。
    #:
    #: **靜態 `.gitconfig` 裝不下它**：路徑帶動態段（`<pool>/<job-id>`、
    #: `.psc-review-worktrees/<…>`），而 git 的 `safe.directory` 只認**逐字相等**的
    #: 路徑或字面 `*`（實測 git 2.43：`<repos>/*` 仍被拒；字面 `*` 是 opt-out，不是
    #: 授權）。
    PER_JOB_ENV = "per-job-env"

    #: per-job 那一格由 **job 自己**建 ⇒ 它擁有自己產出的每一個 inode ⇒ git 看到的
    #: owner 就是當下的 uid ⇒ **零動作**。gate 屬此。
    OWNED_BY_JOB = "owned-by-job"


@dataclass(frozen=True)
class JobGitWorkspaceTrust:
    """一個降權 job principal 的 **git 工作區信任**宣告（#712）。

    這條性質與 :data:`JOB_WORKSPACE_REACH` 是**兩層**，而 #712 就是「其中一層通了、
    另一層沒通」的實機證據：#710／PR #711 把檔案系統層補上之後（`getfacl` 實機確認
    `user:cortex-builder:rwx`／`mask::rwx`），builder job 真的跑起來了，然後死在::

        fatal: detected dubious ownership in repository at '/var/lib/cortex/worktree/wf-…'
        fatal: Need a repository to create a bundle.

    ⇒ **凡在 :data:`DOWNGRADED_JOB_PRINCIPALS` 上的 principal，一律要有一格。**
    """

    #: :data:`DOWNGRADED_JOB_PRINCIPALS` 上的那一格。
    principal: Principal
    #: 真正走這條路的 persona（理由與 :class:`JobWorkspaceReach` 的同名欄位逐字相同）。
    persona: Principal
    trust: GitWorkspaceTrust
    #: 該 principal 的工作區裡**實際存在的 git repo 形狀**（查證結果，非假設）。
    #: 空 tuple＝查證後確定該工作區裡沒有 repo；:attr:`GitWorkspaceTrust.PER_JOB_ENV`
    #: 仍可能為空（reviewer 的 planning scratch 就是），因為同一個帳號的**另一種**
    #: 工作區有 repo——形態是 per-principal 的，工作區不是。
    repo_shapes: tuple[str, ...]
    note: str


#: **#712 那條規則的表**：三個 principal 一格都不能少。
#:
#: 每一列都是**現況查證後**的形狀，不是統一過的理想形狀（#712 issue body 逐字要求
#: 「三者形狀不同，不要假設一致」）。查證結果：
#:
#: | principal | 工作區 | 誰建 ⇒ owner | 裡面有 repo 嗎 | git 要不要放行 |
#: |---|---|---|---|---|
#: | builder | `<worktree pool>/<job-id>` | Manager | **有**（`git clone --no-hardlinks` 的完整 clone，`.git` 是真目錄） | **要**——本票的原症狀 |
#: | reviewer | `<repos>/<slug>/.psc-review-worktrees/<…>` | Manager | **有**（`git worktree add --detach` 的 linked worktree，`.git` 是 gitfile） | **要** |
#: | reviewer | `<planning-scratch>/<…>/cwd` | Manager | **沒有**（空目錄，`_prepare_scratch`） | 注入無害的 no-op（見 note） |
#: | gate | `<gate-worktree>/<key>` | **gate 自己**（`shutil.copytree`） | 有（連 `.git` 一起複製），但 owner 就是 gate | **不要** |
#:
#: **為什麼是一張表而不是三處各自成立**：#623 決定 per-job clone 時，`safe.directory`
#: 只被寫進一份**靜態** `.gitconfig`（來源樹兩條），而 per-job clone 的路徑是動態的
#: ——`permgen` 的 `builder-gitconfig` note 卻宣稱它涵蓋了 per-job clone。三個
#: principal 各自被決定，於是其中一個恰好成立（gate）、一個從來沒有實作（builder）、
#: 一個與 builder 同型但沒有人查過（reviewer）。導出之後，「只修一格」在**結構上做
#: 不到**：新增第四個降權角色而漏了這裡，模組**載不起來**。
#: 前例：#698 的 `EXECUTOR_ENFORCEMENT_LEAVES`、#708 的 :data:`JOB_LOG_SPOOLS`、
#: #710 的 :data:`JOB_WORKSPACE_REACH`。
JOB_GIT_WORKSPACE_TRUST: tuple[JobGitWorkspaceTrust, ...] = (
    JobGitWorkspaceTrust(
        principal=Principal.BUILDER,
        persona=Principal.BUILDER,
        trust=GitWorkspaceTrust.PER_JOB_ENV,
        repo_shapes=("per-job clone（`git clone --no-hardlinks`，`.git` 是真目錄）",),
        note=(
            "**#712 的原症狀就在這一格。** #710／PR #711 把 ACL 補上之後 builder job "
            "真的跑起來了（`in_flight` 約 12 分鐘），然後死在 git 自己那一層：\n"
            "  `fatal: detected dubious ownership in repository at "
            "'/var/lib/cortex/worktree/wf-…-3-f7cef294'`\n"
            "  `fatal: Need a repository to create a bundle.`\n"
            "**owner 必然是 Manager**：clone 由 Manager 建，而把它交出去要 `CAP_CHOWN`"
            "——Manager unit 帶 `CapabilityBoundingSet=`（空），結構上做不到（#710 已"
            "論證，本票不重做那個診斷）。因此 git 必然看到跨 owner。\n"
            "**靜態 `.gitconfig` 裝不下**：`build_account_gitconfig()` 出的是**來源樹**"
            "那兩條逐字路徑，而 per-job clone 是 `<pool>/<job-id>`，每個 job 不同。"
            "萬用字元已被產生器自己的註解否決（實測 git 2.43：`<repos>/*` 仍被拒；"
            "字面 `*` 等於對這個帳號整個關掉該保護——**那是 opt-out，不是授權**）。\n"
            "因此放行逐 job 給，由 **Manager 算**、隨 spec 的 env 一起下去，job 改不了"
            "自己的 spec（#638／#639 的 spool 模型）。"
        ),
    ),
    JobGitWorkspaceTrust(
        principal=Principal.REVIEWER,
        persona=Principal.PLANNER,
        trust=GitWorkspaceTrust.PER_JOB_ENV,
        repo_shapes=(
            "foreign review 的 linked worktree（`git worktree add --detach`，`.git` 是 gitfile）",
        ),
        note=(
            "**查證後確認它與 builder 同型，不是照抄。** reviewer 帳號有兩種工作區，"
            "本票逐一查過：\n"
            "  - **foreign review**：`coordinator/review.py:prepare_review_worktree` 以 "
            "`git worktree add --detach` 在 `<repos>/<slug>/.psc-review-worktrees/` 底下"
            "開一棵 **linked worktree**——**Manager 建、Manager 擁有**，而 job 跑在 "
            "`cortex-reviewer-planner` 上 ⇒ **同樣跨 owner**。\n"
            "  - **planning／define scratch**：`coordinator/planning_job.py:_prepare_scratch` "
            "建的是一格**空目錄**，裡面沒有 repo。注入的 `safe.directory` 在那裡是"
            "無害的 no-op（git 只在真的找到 repo 時才查 ownership）。\n"
            "**形態是 per-principal 的，工作區不是**——同一個帳號的兩種工作區只要有"
            "一種跨 owner，這一格就必須是 `PER_JOB_ENV`。\n"
            "⚠️ **靜態檔那兩條蓋不到 linked worktree**：0819 實測（git 2.43.0）"
            "`safe.directory=<source repo>` ＋ `<source repo>/.git` 之下，對 "
            "`<source repo>/…/wt` 這棵 linked worktree 跑 `git status` 仍是 "
            "`fatal: detected dubious ownership in repository at '<…>/wt'`——git 查的是"
            "**工作樹自己的路徑**。反過來只給 worktree 那一條就過（gitdir 在別人的樹裡"
            "也一樣），因此逐 job 那一條是充分的。\n"
            "**唯讀不變**：本項不動任何檔案權限，reviewer 對工作區仍然只有 `rX`"
            "（#710 那一列的刻意設計，交付通道是 verdict spool）。`safe.directory` 是 "
            "git 的信任判準，不是權限。"
        ),
    ),
    JobGitWorkspaceTrust(
        principal=Principal.GATE,
        persona=Principal.GATE,
        trust=GitWorkspaceTrust.OWNED_BY_JOB,
        repo_shapes=("被驗樹的拋棄式副本（`shutil.copytree`，含 `.git`）",),
        note=(
            "**查證結論：gate 不需要，一個位元組都不要動。** per-job 那一格是 gate "
            "**自己**用 `shutil.copytree` 從 builder 的 clone 複製出來的"
            "（`gate_ledger.snapshot_worktree`）⇒ 副本的每一個 inode（含 `.git`）都由 "
            "gate 擁有 ⇒ git 看到的 owner 就是當下的 uid，dubious-ownership 根本不觸發。"
            "這與登記表既有的 deferred 項 `gate-gitconfig` 的 symptom 欄逐字一致"
            "（「複本由 gate 自己擁有、多數情況不會撞 dubious ownership」），本票把那句"
            "話從一則 deferred 的觀察升級成**受斷言保護**的宣告。\n"
            "**它靠的是 pool 根的 owner 位**（`gate-worktree-pool` 的 owner 就是 "
            "`cortex-gate`，#710 已查證並以 import 期斷言釘住）。owner 一旦漂走"
            "（例如有人把 pool 改成 Manager-owned「比照 dispatch pool」），gate 不只建"
            "不出副本，連 git 信任也一起塌——那正是 "
            ":func:`_assert_every_downgraded_principal_has_a_git_workspace_trust` 要在"
            "模組載入當下擋掉的漂移。\n"
            "**仍未涵蓋的那一面不變**：gate 的 `PSC_GATE_CMD_*` 若去碰**來源樹**"
            "（`git describe`／`setuptools-scm`），那是 `gate-gitconfig` 那個 deferred "
            "項的事，與本票的 per-job 那一格無關。"
        ),
    ),
)


def job_git_workspace_trust_for(principal: Principal) -> JobGitWorkspaceTrust:
    """該降權 principal 的 git 工作區信任宣告；查無即 fail-closed（#712）。"""

    for trust in JOB_GIT_WORKSPACE_TRUST:
        if trust.principal is principal:
            return trust
    raise KeyError(
        f"{principal.value} 沒有登記 git 工作區信任——凡經模板 unit 派出的 job 都在"
        "自己的工作區裡跑 git（bundle／status／模型自己叫的那些），而 git 的 "
        "dubious-ownership 保護是 owner 判準，不是權限判準（#712）。"
    )


def _assert_every_downgraded_principal_has_a_git_workspace_trust() -> None:
    """#712 的那條規則，在 **import 當下**強制（registry 這一半）。

    **規則**：`principal` 在 :data:`DOWNGRADED_JOB_PRINCIPALS` 上有一格
    ⇒ 它在 :data:`JOB_GIT_WORKSPACE_TRUST` 上**必須**也有恰好一格，**且該格與
    :data:`JOB_WORKSPACE_REACH` 對「誰建那一格」的宣告一致**。

    第二個條件才是這條規則的本體。git 的判準只有一條——**repo 的 owner 是不是當下這個
    uid**——而「誰建那一格」已經在 #710 的表上寫得清清楚楚：

    - :attr:`WorkspaceReach.POOL_OWNED_BY_JOB` ＝ per-job 那一格由 **job 自己**建
      ⇒ owner 就是 job ⇒ :attr:`GitWorkspaceTrust.OWNED_BY_JOB`；
    - 其餘兩種形態（具名 ACL／繼承 default ACL）的共同前提都是「**Manager 建、
      Manager 擁有**，job 靠 ACL 進得去」⇒ 跨 owner ⇒
      :attr:`GitWorkspaceTrust.PER_JOB_ENV`。

    因此本表**不是第二個各自為政的決定**，是同一個事實的第二個後果；兩張表一旦說出
    互相矛盾的話，模組就載不起來。這正是本票要消滅的那一類缺口：#623 決定 per-job
    clone 之後，「per-job clone 的 safe.directory」只活在一則 note 裡，而產生器實際
    只出來源樹那兩條——**note 反向說謊了兩個月**（#696 的同型，本票是第三個實例）。
    """

    declared = [trust.principal for trust in JOB_GIT_WORKSPACE_TRUST]
    if len(declared) != len(set(declared)):
        raise ValueError(f"JOB_GIT_WORKSPACE_TRUST 有重複的 principal: {declared}")
    missing = [p for p in DOWNGRADED_JOB_PRINCIPALS if p not in declared]
    if missing:
        raise ValueError(
            f"降權 principal {[p.value for p in missing]} 沒有 git 工作區信任宣告——"
            "job 在自己的工作區裡跑 git（builder 的 `git bundle create`、reviewer 的 "
            "`git status`／`git diff`），而工作區若由 Manager 建，git 的 "
            "dubious-ownership 保護會在**權限全部正確**的情況下照樣擋死整個 repo"
            "（#712 實機：`fatal: detected dubious ownership`）。"
            "請往 JOB_GIT_WORKSPACE_TRUST 加一列，不要在別處手寫一條 safe.directory。"
        )
    extra = [p for p in declared if p not in DOWNGRADED_JOB_PRINCIPALS]
    if extra:
        raise ValueError(
            f"JOB_GIT_WORKSPACE_TRUST 宣告了非降權 principal {[p.value for p in extra]}"
            "——本表的輸入只有 DOWNGRADED_JOB_PRINCIPALS（同 UID 下沒有跨 owner 可言）。"
        )
    for trust in JOB_GIT_WORKSPACE_TRUST:
        if trust.persona not in UNTRUSTED_EXECUTION_PRINCIPALS:
            raise ValueError(
                f"{trust.principal.value}：persona {trust.persona.value} 不在 "
                "UNTRUSTED_EXECUTION_PRINCIPALS 上——那樣它根本不是以另一個 uid 執行的，"
                "本表沒有可講的東西（#712）。"
            )
        if not trust.note:
            raise ValueError(
                f"{trust.principal.value}：note 為空——「這一格要不要放行」是查證結果，"
                "不是預設值；查證過程必須寫得出來（#712）。"
            )
        reach = job_workspace_reach_for(trust.principal)
        owned_by_job = reach.reach is WorkspaceReach.POOL_OWNED_BY_JOB
        wants_owned = trust.trust is GitWorkspaceTrust.OWNED_BY_JOB
        if owned_by_job != wants_owned:
            raise ValueError(
                f"{trust.principal.value}：git 信任宣告 {trust.trust.value} 與工作區"
                f"可達性宣告 {reach.reach.value} 互相矛盾——git 的判準只有「repo 的 "
                "owner 是不是當下這個 uid」，而「誰建那一格」已經在 JOB_WORKSPACE_REACH "
                "上宣告過了：`pool-owned-by-job` ⇒ job 自己建、自己擁有 ⇒ "
                f"`{GitWorkspaceTrust.OWNED_BY_JOB.value}`；其餘兩種形態 ⇒ Manager 建、"
                f"Manager 擁有 ⇒ 跨 owner ⇒ `{GitWorkspaceTrust.PER_JOB_ENV.value}`"
                "（#712）。"
            )
        # `repo_shapes` 刻意**不設限**：`OWNED_BY_JOB` 的工作區裡可以有 repo（gate 的
        # 副本就有），那正是它不需要放行的原因——**有 repo，但 owner 是自己**；而
        # `PER_JOB_ENV` 的某一種工作區可以沒有 repo（reviewer 的 planning scratch），
        # 注入在那裡是無害的 no-op。用 repo 的有無去推放行形態就會兩邊都推錯。


_assert_every_downgraded_principal_has_a_git_workspace_trust()


def job_spec_spool_asset_id(principal: Principal) -> str:
    """該降權 principal 的 per-principal spec spool 資產 id（#657）。

    容器（`job-spec-spool`）與子 spool（`job-spec-spool-<principal>`）是**兩種不同
    的東西**：容器 owner-only、對 job 只有機械導出的 `--x` traverse（走得進去、列不
    出來）；子 spool 才帶該帳號的唯讀 ACL。
    """

    return f"job-spec-spool-{principal.value}"


class IngressKind(Enum):
    """spec §C mutation ingress 盤點的種類。"""

    MANAGER_INTERNAL = "manager-internal"      # 僅 Manager 自身寫入
    MONITOR_INTERNAL = "monitor-internal"      # 僅 Monitor 自身寫入
    CONTROL_FILE_QUEUE = "control-file-queue"  # <control_root>/requests/：未認證入口
    CLI_STATE_PATH = "cli-state-path"          # CLI --state 直指
    DIRECT_FILE_WRITE = "direct-file-write"    # 任何同 UID 行程直寫
    ENV_REDIRECT = "env-redirect"              # PSC_* / bootstrap env 重導
    ENV_NAMED_EXEC = "env-named-exec"          # env 指名並執行任意程式
    CONFIG_OVERLAY = "config-overlay"          # overlay 覆蓋（model-identity/combo/persona）
    DEPLOYMENT_WRITE = "deployment-write"      # unit/venv/launcher/sitecustomize/.pth/hooks
    INTERPROCESS = "interprocess"              # inherited FD / ptrace / /proc / signal / spool
    STAGING_SPOOL = "staging-spool"            # headless 寫自己 worktree / staging 區


@dataclass(frozen=True)
class TrustRootAsset:
    """單一 durable state 資產的宣告。"""

    asset_id: str
    tier: AssetTier
    tree: TrustTree
    #: `"module:function"` 形式，指向 `config.paths`／`control.constants` 中回傳
    #: 該資產路徑的**單一** canonical 函式；若路徑在別的模組以字面量推導（尚未收斂
    #: 到 path 契約模組），則為 None 並在 `derived_in` 記錄實際推導位置。
    path_resolver: str | None
    writers: tuple[Principal, ...]
    readers: tuple[Principal, ...]
    ingress_kind: IngressKind
    #: 呼叫 `path_resolver` 時要帶的引數（#657）。同一支 resolver 可以服務**一族**
    #: 資產（per-principal spool：`job_spec_spool_for("gate")`），此時 asset 之間的
    #: 差別就是這一組引數。空 tuple＝零引數 resolver（絕大多數）。
    #:
    #: 為什麼不是「一族三支零引數函式」：那會把「哪些 principal 有自己的 spool」
    #: 複製成第二份清單，而那份清單漏一項的症狀正是本票要修的東西（漏授＝該身分
    #: 每個 job 以 78/CONFIG 收場）。resolver 帶引數之後，這族資產與它們的路徑同樣
    #: **由單一 principal 清單機械導出**。
    path_resolver_args: tuple[str, ...] = ()
    #: 現況路徑推導位置（`檔案:行號`），供 Phase 2 收斂與交叉複驗；spec §R1 要求
    #: 把重複推導收斂到登記表，故一律登記全部已知推導點。
    derived_in: tuple[str, ...] = ()
    note: str = ""

    def is_manager_owned(self) -> bool:
        return self.tree is TrustTree.MANAGER_OWNED

    def headless_writable(self) -> bool:
        """現況是否有任何 headless persona（或同 UID 任意行程）可寫。

        Phase 2 權限產生器據此決定哪些 Manager-owned 資產必須移出 headless 寫入域。
        """
        return any(
            w is Principal.ANY_SAME_UID or w in HEADLESS_PERSONAS for w in self.writers
        )


# ---------------------------------------------------------------------------
# 登記表本體。順序：先 config.paths／control.constants 有 resolver 的容器與葉，
# 再列在別處以字面量推導、path_resolver=None 的 Tier-0／Tier-1 資產。
# ---------------------------------------------------------------------------

_T0 = AssetTier.TIER_0
_T1 = AssetTier.TIER_1
_MO = TrustTree.MANAGER_OWNED
_JV = TrustTree.JOB_VISIBLE


def _job_log_spool_assets() -> tuple[TrustRootAsset, ...]:
    """job log spool 的登記表項——由 :data:`JOB_LOG_SPOOLS` **機械導出**。

    形態與 :func:`_job_spec_spool_assets` 逐條相同（同樣是一支 resolver ＋
    per-principal 引數），差別只在方向：spec spool 是 Manager 寫、job 讀；log spool
    是 job 寫、Manager 讀，因此 `ingress_kind` 是 `INTERPROCESS`，產生器據此出
    **write-only** ACL（`wx` 無 `r`——寫得進自己那一格、讀不到別人的 log）。
    """

    return tuple(
        TrustRootAsset(
            spool.asset_id, _T0, _JV,
            "paulsha_cortex.config.paths:job_log_spool_root",
            (Principal.MANAGER, spool.writer), (Principal.MANAGER,),
            IngressKind.INTERPROCESS,
            path_resolver_args=(spool.principal.value,),
            derived_in=(
                "config/paths.py:job_log_spool_root",
                "trust_root/registry.py:JOB_LOG_SPOOLS",
                "trust_root/permgen.py:PathLayout.job_log_spool_root",
                "coordinator/spool_slot.py:prepare_job_log",
                "coordinator/job_shim.py:_take_over_stdio",
            ),
            note=(
                f"**#708 的規則：凡經模板 unit 派出的 job 都要寫得出 log** ⇒ "
                f"`{spool.principal.value}` 這一格。路徑掛在既有通道 "
                f"`{spool.channel_asset_id}` 底下（`<那條通道>/{spool.dirname}/"
                "<unit-instance>/…`）：那個帳號本來就寫得進這棵樹，因此 (i) 不新增任何"
                "寫入面、(ii) `_minimize()` 把它從模板 unit 的 `ReadWritePaths=` 吃掉 ⇒ "
                "**逐字不變、default ACL 自動繼承、零部署動作**、(iii) 仍是獨立資產，"
                "治理面沒有因為省下一條 RWP 而消失。\n"
                "**log 檔由 Manager 預先建立（mode `0620`），不是由 job 建**：job 自己"
                "建的檔由 job 擁有、又帶 unit 的 `UMask=0077`，Manager 是**目錄**的 owner "
                "但那不給檔案內容的讀取權（#638 缺陷 2）。`0620` 不是隨手挑的——POSIX ACL "
                "下新檔的 mask 由 open(2) 的 mode 參數之 group 位夾擠，用 `0600` 建檔會把"
                "繼承來的 `user:<job>:-wx` 壓成 `#effective:---`，job 於是連 append 都不行"
                "（#638 缺陷 1 的同一個機制）。\n"
                f"{spool.note}"
            ),
        )
        for spool in JOB_LOG_SPOOLS
    )


def _job_spec_spool_assets() -> tuple[TrustRootAsset, ...]:
    """per-principal spec spool 的登記表項（#657）——由
    :data:`DOWNGRADED_JOB_PRINCIPALS` **機械導出**，不逐項手寫。

    手寫三項的代價不是打字量，是**漏一項的失效模式**：漏掉的那個 principal 不會有
    任何 ACL，於是它的每一個 job 都在 shim 讀 spec 時 `EACCES` → `78/CONFIG`，而
    產生器、CI、單 UID 的測試全部是綠的（那正是 #657 的病史）。導出之後，新增一個
    降權角色只要動那張表一行，資產／路徑／unit env／traverse ACL 一起跟上。

    每一項的 reader 面**只有它自己**（＋Manager，它是 writer 兼消費者）：跨 principal
    互讀 spec 在本設計下不成立，而那正是選 per-principal 而非「共用 spool 擴大 reader
    面」的理由之一。
    """

    return tuple(
        TrustRootAsset(
            job_spec_spool_asset_id(principal), _T0, _MO,
            "paulsha_cortex.config.paths:job_spec_spool_for",
            (Principal.MANAGER,), (Principal.MANAGER, principal),
            IngressKind.MANAGER_INTERNAL,
            path_resolver_args=(principal.value,),
            derived_in=(
                "config/paths.py:job_spec_spool_for",
                "trust_root/permgen.py:PathLayout.job_spec_spool_for",
                "coordinator/job_runner.py:JOB_ROLE_CONFIG",
                "coordinator/job_runner.py:job_spec_path",
                "coordinator/job_shim.py:load_spec",
            ),
            note=(
                f"#657：`{principal.value}` **專屬**的 per-job 執行規格 spool"
                f"（`<coordinator_root>/job-specs/{principal.value}/"
                "<unit-instance-id>.json`）。root-owned 模板 unit 的 `ExecStart=` 固定"
                "為 shim，per-job 的命令／worktree／白名單 env／log 路徑由本 spool 傳遞；"
                "**writer 只有 Manager**，該 job 帳號在 reader 面（唯讀 ACL），因此改不了"
                "自己的命令列，`User=` 也完全不在本檔內（硬寫死在 root-owned unit 檔裡）。\n"
                "**為什麼一個 principal 一個 spool**：shim 是 systemd 套完 `User=` 之後才"
                "執行的，它以 job 身分讀 spec；共用一個 spool 時「哪個身分讀得到」變成一組"
                "共用目錄上多條 ACL 的交集，漏授的症狀是該身分每個 job 以 78/CONFIG 收場"
                "（#657 實機）。拆開之後這件事是 root-owned unit 上可逐字稽核的一行"
                "`Environment=PSC_JOB_SPEC_SPOOL=`，而且不必新開「跨 persona 互讀 spec」"
                "這個性質。"
            ),
        )
        for principal in DOWNGRADED_JOB_PRINCIPALS
    )


ASSET_REGISTRY: tuple[TrustRootAsset, ...] = (
    # ---- resolver-backed 容器樹 --------------------------------------------
    TrustRootAsset(
        "runtime-agents-tree", _T0, _MO, "paulsha_cortex.config.paths:agents_root",
        (Principal.INSTALLER, Principal.MANAGER, Principal.ANY_SAME_UID),
        (Principal.MANAGER, Principal.MONITOR),
        IngressKind.ENV_REDIRECT,
        note="~/.agents 樹根；解析鏈本身即信任根（runtime.py:89），實測 group-writable。",
    ),
    TrustRootAsset(
        "control-root-tree", _T0, _MO, "paulsha_cortex.config.paths:control_root",
        (Principal.MANAGER, Principal.ANY_SAME_UID),
        (Principal.MANAGER, Principal.OPERATOR),
        IngressKind.ENV_REDIRECT,
        note="control 樹容器；子資產 requests/done/status/lock 各自登記。",
    ),
    TrustRootAsset(
        "coordinator-root-tree", _T0, _MO, "paulsha_cortex.config.paths:coordinator_root",
        (Principal.MANAGER, Principal.ANY_SAME_UID),
        (Principal.MANAGER,),
        IngressKind.ENV_REDIRECT,
        note="jobs.json／evidence 樹／journal 的容器；實測 drwxrwxr-x（group-writable）。",
    ),
    TrustRootAsset(
        "dispatch-specs-tree", _T0, _MO, "paulsha_cortex.config.paths:specs_root",
        (Principal.MANAGER, Principal.PLANNER, Principal.ANY_SAME_UID),
        (Principal.MANAGER,),
        IngressKind.DIRECT_FILE_WRITE,
        note="staged dispatch specs；planner 產出的 spec/plan 直接餵給 Compact reuse。",
    ),
    TrustRootAsset(
        "runtime-run-tree", _T0, _MO, "paulsha_cortex.config.paths:run_root",
        (Principal.MANAGER, Principal.MONITOR),
        (Principal.MANAGER, Principal.OPERATOR),
        IngressKind.MANAGER_INTERNAL,
        note="socket／lock run dir；monitor socket 已 0600、run dir 0700（正面前例）。",
    ),
    TrustRootAsset(
        "project-config-tree", _T0, _MO, "paulsha_cortex.config.paths:project_config_root",
        (Principal.OPERATOR, Principal.ANY_SAME_UID),
        (Principal.MANAGER,),
        IngressKind.CONFIG_OVERLAY,
        note="model-identities.yaml overlay 所在；overlay 可壓過 packaged registry。",
    ),
    TrustRootAsset(
        "coverage-shadow-telemetry", _T1, _MO,
        "paulsha_cortex.config.paths:coverage_shadow_telemetry_root",
        (Principal.MANAGER,), (Principal.MANAGER, Principal.OPERATOR),
        IngressKind.MANAGER_INTERNAL,
        note="v4 R1 coverage shadow disagreement telemetry；本票撰寫期間新增的 durable state。",
    ),
    # ---- monitor state 族 --------------------------------------------------
    TrustRootAsset(
        "monitor-state-tree", _T1, _MO, "paulsha_cortex.config.paths:monitor_state_root",
        (Principal.MONITOR,), (Principal.MANAGER, Principal.MONITOR),
        IngressKind.MONITOR_INTERNAL,
        note="monitor 讀模型與傳輸層狀態的容器。",
    ),
    TrustRootAsset(
        "monitor-work-items-snapshot", _T1, _MO,
        "paulsha_cortex.config.paths:work_items_snapshot_path",
        (Principal.MONITOR,), (Principal.MANAGER,),
        IngressKind.MONITOR_INTERNAL,
        derived_in=("config/paths.py:56-57", "coordinator/claim.py:225-228"),
        note="fallback 推導分歧（claim.py 另有一處）；Phase 2 前須收斂為單一 resolver。",
    ),
    TrustRootAsset(
        "monitor-github-sync-cursor", _T1, _MO,
        "paulsha_cortex.config.paths:github_issue_sync_path",
        (Principal.MONITOR,), (Principal.MANAGER,),
        IngressKind.MONITOR_INTERNAL,
        note="GitHub 增量同步游標／ETag（D3）。",
    ),
    TrustRootAsset(
        "monitor-event-spool", _T1, _JV,
        "paulsha_cortex.config.paths:monitor_event_spool_root",
        (Principal.HEADLESS_HOOK, Principal.ANY_SAME_UID), (Principal.MONITOR,),
        IngressKind.INTERPROCESS,
        note="D5 headless 事件 hint spool；別的行程寫入、monitor 消費即消失（一次性）。",
    ),
    # ---- skill governance 族 -----------------------------------------------
    TrustRootAsset(
        "skill-registry-tree", _T1, _MO, "paulsha_cortex.config.paths:skill_registry_root",
        (Principal.MANAGER, Principal.ANY_SAME_UID), (Principal.MANAGER, Principal.OPERATOR),
        IngressKind.DIRECT_FILE_WRITE,
        note="skill ledger／park／proposal 容器；現況寫入皆無 chmod。",
    ),
    TrustRootAsset(
        "skill-usage-ledger", _T1, _MO, "paulsha_cortex.config.paths:skill_usage_ledger_path",
        (Principal.MANAGER, Principal.ANY_SAME_UID), (Principal.MANAGER, Principal.OPERATOR),
        IngressKind.DIRECT_FILE_WRITE,
    ),
    TrustRootAsset(
        "skill-park-state", _T1, _MO, "paulsha_cortex.config.paths:skill_park_state_path",
        (Principal.MANAGER, Principal.ANY_SAME_UID), (Principal.MANAGER, Principal.OPERATOR),
        IngressKind.DIRECT_FILE_WRITE,
    ),
    TrustRootAsset(
        "skill-park-proposals", _T1, _MO, "paulsha_cortex.config.paths:skill_park_proposals_root",
        (Principal.MANAGER, Principal.ANY_SAME_UID), (Principal.MANAGER, Principal.OPERATOR),
        IngressKind.DIRECT_FILE_WRITE,
    ),
    # ---- control queue 族（resolver 在 control.constants）--------------------
    TrustRootAsset(
        "control-request-queue", _T0, _JV, "paulsha_cortex.control.constants:requests_dir",
        (Principal.OPERATOR, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.CONTROL_FILE_QUEUE,
        derived_in=("control/constants.py:21-26", "manager_daemon.py:1431-1436"),
        note="全部 7 種 request／22 種 work-action 的入口；未認證，處理順序由 mtime 決定。",
    ),
    TrustRootAsset(
        "control-done-queue", _T0, _MO, "paulsha_cortex.control.constants:done_dir",
        (Principal.MANAGER,), (Principal.OPERATOR,),
        IngressKind.MANAGER_INTERNAL,
    ),
    TrustRootAsset(
        "control-status", _T0, _MO, "paulsha_cortex.control.constants:status_path",
        (Principal.MANAGER,), (Principal.OPERATOR,),
        IngressKind.MANAGER_INTERNAL,
    ),
    TrustRootAsset(
        "control-daemon-lock", _T0, _MO, "paulsha_cortex.control.constants:lock_path",
        (Principal.MANAGER,), (Principal.OPERATOR,),
        IngressKind.MANAGER_INTERNAL,
        note="lock 檔實測 0o644；owner 可 unlink 後重建。",
    ),
    # ---- per-job clone 的來源樹（#623）--------------------------------------
    TrustRootAsset(
        "repo-source-tree", _T0, _MO, None,
        (Principal.MANAGER,),
        (
            Principal.MANAGER, Principal.MONITOR,
            Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER,
        ),
        IngressKind.MANAGER_INTERNAL,
        derived_in=("trust_root/permgen.py:PathLayout.repo_source_root",),
        note=(
            "`<agents_root>/repos/<slug>`——**Manager-owned 樹內的 working checkout**，"
            "同時是 monitor 的掃描目標（`workstreams/*/todo.md` 等要有工作樹，bare 沒有）"
            "與每個 job 的 **clone 來源**。#623 實測：共用 git object store 與三分隔離"
            "**互斥**（builder 要 commit 就得能寫 object store，能寫就等於邊界在 git 這層"
            "漏掉），因此 job 工作區由 `git worktree` 改為 **per-job 完整 clone**"
            "（0.5 秒／35MB）。\n"
            "**writer＝Manager（0817 裁決，推翻本票初版的 root-owned）**。初版把 writer "
            "定成只有部署身分，機械分到 `owner_class=DEPLOYMENT`（root 0755）；實測那條"
            "**走不通**：\n"
            "    $ sudo -u cortex-manager git -C <來源樹> fetch <bundle> …\n"
            "    error: cannot open '.git/FETCH_HEAD': Permission denied\n"
            "`git fetch` 必須把 `FETCH_HEAD` 寫進**目標 repo**，而 #634 的成果回收正是"
            "「fetch 進來源樹」；provisioning 那半邊也一樣要寫（`seams.py` 的 "
            "`git branch -f <branch> <base>` 是對來源樹的寫入）。「Manager 唯讀」與"
            "「Manager 回收成果」互斥，取後者。\n"
            "**為何隔離沒有變弱**：威脅模型裡不受信任的是 **job 帳號**，而 Manager 本來"
            "就擁有 gate ledger、evidence 樹與 jobs.json——Manager 被攻陷的話那些全都完了，"
            "多這一棵樹不改變攻擊面。root-owned 買到的是「Manager 被攻陷後仍污染不了下一輪"
            "job 的原始碼」這條供應鏈保護，但它讓成果回收整個不成立，因此取回收。改成 "
            "Manager 可寫後實測整條通（`fetch` 真實 rc=0），而兩個 job 帳號對來源樹**仍"
            "只有唯讀 ACL**（`rX`，一個 `w` 都沒有）——這條才是本資產真正在守的邊界。\n"
            "**無 path_resolver**：程式碼解析的是**單一** repo（`PSC_REPO_ROOT` → "
            "`<此樹>/<slug>`，見 `config/paths.py:repo_root`），本資產是**容器**，"
            "沒有任何函式回傳它。"
        ),
    ),
    TrustRootAsset(
        "builder-gitconfig", _T0, _MO, None,
        (Principal.INSTALLER,), (Principal.BUILDER,),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:build_account_gitconfig",),
        note=(
            "builder 帳號 HOME 下的 root-owned `.gitconfig`（0644），內容含來源 repo 的 "
            "`safe.directory`——比照既有的 `codex-hooks`（root-owned、在 job 帳號 HOME 下），"
            "不是新概念。**為何必須存在**：per-job clone 的來源樹不屬於 job 帳號，git 的 "
            "dubious ownership 保護會讓 `git clone` 直接失敗；而 job 的 HOME 是 root-owned，"
            "它自己放不了這個檔。**為何是 Tier-0**：gitconfig 可指定 `core.fsmonitor`／"
            "`core.pager`／`alias.*` 等會執行外部命令的鍵，可寫者等於對該 job 帳號取得"
            "任意程式碼執行——與 `codex-hooks` 同一條性質。內容由 permgen 產生"
            "（`build_account_gitconfig()`），不手寫；每個來源 repo **兩條** "
            "`safe.directory`（工作樹根 ＋ `<root>/.git`），理由見該函式。"
        ),
    ),
    TrustRootAsset(
        "reviewer-planner-gitconfig", _T0, _MO, None,
        (Principal.INSTALLER,), (Principal.REVIEWER, Principal.PLANNER),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:build_account_gitconfig",),
        note=(
            "同 `builder-gitconfig`，落在 reviewer＋planner 的 job 帳號 HOME 下"
            "（三分定案的 `cortex-reviewer-planner`）。兩個 job 帳號各一份是必要的："
            "`.gitconfig` 讀取端是 `$HOME`，而三分的硬性要求就是 reviewer 與 builder "
            "互不可寫、HOME 互不相同。"
        ),
    ),
    TrustRootAsset(
        "manager-gitconfig", _T0, _MO, None,
        (Principal.INSTALLER,), (Principal.MANAGER, Principal.MONITOR),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:build_account_gitconfig",),
        note=(
            "與上面兩份**同構**（root-owned 0644、落在帳號 HOME 下、內容由 permgen 產），"
            "但讀者是 `cortex-manager`（Manager ＋ monitor 同帳號、同 HOME）。\n"
            "**為何必須存在**：Manager 也要對來源樹跑 git（provision 的 "
            "`git branch -f`、成果回收的 `git fetch`、dispatch baseline 的 `rev-parse`），"
            "而來源樹是由 root 建立、再 chown 給 `cortex-manager` 的——**chown 之前**或"
            "任何 owner 不相符的中途狀態，git 一樣擋：\n"
            "    fatal: detected dubious ownership in repository at '<來源樹>/<slug>'\n"
            "owner 相符時這個檔是無害的冗餘；owner 不相符時它是「服務起得來但每一次 git "
            "都失敗」與「正常運轉」的差別。缺這一份正是本票初版被實機複驗抓到的 blocking "
            "缺口。\n"
            "**writer 仍只有部署身分**：Manager 可寫的是**來源樹**，不是自己的 gitconfig"
            "——`.gitconfig` 可指定 `core.fsmonitor`／`alias.*` 這類會執行外部命令的鍵，"
            "讓 Manager 能改自己的 git 設定等於把 Tier-0 的執行面交還給它。"
        ),
    ),
    # ---- executor 執行面：toolchain ＋ per-account 憑證（#640）--------------
    TrustRootAsset(
        "executor-toolchain", _T0, _MO, None,
        (Principal.INSTALLER,),
        (
            Principal.MANAGER, Principal.MONITOR,
            Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER,
        ),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.toolchain_root",),
        note=(
            "`<deploy_root>/toolchain`——**job／服務執行面需要的外部程式**的部署樹"
            "落點。root-owned 0755：全部 job／服務帳號**唯讀＋可執行**，一個 `w` 都"
            "沒有（機械結果：writer 只有部署身分 → `owner_class=DEPLOYMENT`，與 "
            "`runtime-agents-tree`／venv 同一類）。\n"
            "**#661：涵蓋範圍不只 executor。** #640 落表時這裡寫的是「四個模型 "
            "executor」，而那是一個真實的盤點缺口——實機 doctor 的 `review-sandbox` "
            "FAIL 就是 `srt`（Claude sandbox runtime）沒被盤到，ship 段的 "
            "`openspec archive` 同理。判準不是「是不是 executor」而是「**這支程式的"
            "版本會不會影響治理產出**」，因此登記表現在有兩張名冊、同一棵樹："
            "`permgen.EXECUTOR_TOOLS`（dispatch 直接執行的模型 CLI）與 "
            "`permgen.SERVICE_TOOLS`（`srt`／`openspec` 這種由別人 exec 的）。"
            "**刻意分兩張**：`EXECUTOR_TOOLS` 同時是 dispatch 的 executor 名字判準"
            "（`executor_hardening_profile()` 對表外的名字 fail-closed，spec §R8），"
            "把非 executor 併進去等於讓 `executor: srt` 這種派工變成合法。\n"
            "**為何必須存在（#640 實測）**：Phase 2b 的 job unit 帶 `ProtectHome=yes`，"
            "而四個 executor 原本全在 operator 的 HOME 底下（nvm 樹與 `~/.local/bin`），"
            "`/home` 整個不可見——\n"
            "    $ sudo -u cortex-builder env HOME=<job HOME> codex exec --help\n"
            "    /usr/bin/env: ‘node’: No such file or directory      rc=127\n"
            "沒有任何一個 executor 可用，dispatch 會一路走到**呼叫模型**那一步才失敗。\n"
            "**0817 裁決 (a)**：`node` 走**系統層**（它是通用 runtime，換版本幾乎不影響"
            "產出），四個模型 CLI 則落進本資產。理由是「job 跑的是哪個版本的模型 CLI」"
            "**會**影響產出，那必須是一個**可稽核的部署決定**，而不是跟著 operator 自己"
            "的環境漂移。這不是假設——實機盤點在同一台機器上就有兩份 `codex`（系統層 "
            "0.42.0 vs operator 實際在用的 0.147.0，差 100 個以上小版本）；照「系統層有"
            "什麼就用什麼」的做法，job 會跑一份 operator 從未判讀過的版本。因此安裝步驟"
            "是**從 operator 實際使用的那一份複製進來**，不是另外 `npm install -g` 裝一份。\n"
            "**哪些吃系統層 node 的版本風險**：形態表見 `permgen.EXECUTOR_TOOLS` 與 "
            "`permgen.SERVICE_TOOLS` 的 `needs_node`（#640 初版只認得 `codex`；#643 "
            "回填 `copilot`，#661 再回填 `srt`／`openspec`）。系統層那一半同樣進表"
            "（`permgen.SYSTEM_PROGRAMS`：`node`／`git`／`gh`／`bwrap`／`socat`），"
            "版本本身仍是**部署決定**——某個 CLI 哪天提高下限時要一併升。\n"
            "**#661 的第二個發現（尚待裁決，不在本資產的權限面）**：`needs_node` 的"
            "非 executor 程式跑在**消費者的**加固面上，而 #643 的剖面推導只看 executor "
            "名——`srt` 由 `claude`（strict 剖面）exec、`openspec` 由 Manager 的 system "
            "unit exec，兩者目前都是 `MemoryDenyWriteExecute=yes`，與 V8 的 JIT 互斥。"
            "可列舉的形式在 `permgen.unresolved_node_execution_surfaces()`，實機量測"
            "步驟在 runbook 第 4e 步。\n"
            "**ProtectSystem=strict 下 `/opt` 唯讀**：讀取與執行完全不受影響，被擋的只有"
            "寫入——因此本資產機械地不會出現在任何 unit 的 `ReadWritePaths` 上。"
        ),
    ),
    # ---- 兩個 job 帳號的 codex 狀態樹（#698：形狀由同一條規則導出）----
    #
    # **這兩項在 #698 之前是兩種形狀**：`builder-executor-credential`（`IN_PLACE_FILE`，
    # 單檔 `~/.codex/auth.json`）與 `reviewer-planner-codex-state`
    # （`HOME_REDIRECT_TREE`，導進 job-owned `cache/codex` 的 symlink）。0818 的 R9 實測
    # 證明那個差異是一個**尚未爆的洞**而不是設計，兩邊各壞一半：
    #
    #   - `IN_PLACE_FILE` 那一半：hooks 守得住，但 codex 在降權 unit 下**起不來**
    #     （#686：`$CODEX_HOME` 唯讀時連 in-process app-server 都初始化不了）。
    #   - `HOME_REDIRECT_TREE` 那一半：codex 起得來，但整棵樹 job-owned ⇒ 該帳號
    #     **成功植入 `hooks.json`**（T3.9 `!! SUCCEEDED`）。codex hooks 會執行命令 ⇒
    #     跨 job 持久化 ⇒ 四分隔離「每個 job 一次性」的前提在該帳號上不成立，而它正是
    #     吃 untrusted issue 內容的那個帳號。
    #
    # operator 裁決（#698，方案 A）＝ `CredentialShape.HOME_STICKY_TREE`：**root-owned
    # ＋ sticky bit ＋ job 一條 `rwx` access ACL 的真目錄**，裡面住著 root-owned 的
    # `<前綴>-codex-hooks`。整棵可寫 ⇒ codex 起得來；sticky ⇒ job 刪不掉／改不掉名字別人
    # 的檔，而 hooks 的 mode 讓它連內容都改不了。
    #
    # **兩個帳號共用同一列憑證表**（`permgen.EXECUTOR_CREDENTIALS` 的 codex 那一列），
    # 因此形狀不可能只改一格——`_assert_shape_follows_enforcement_rule()` 在 import 當下
    # 強制 `EXECUTOR_ENFORCEMENT_LEAVES` 那條規則。
    #
    # **writers 含 INSTALLER 是刻意的**（與 #640 的 `IN_PLACE_FILE` 相反）：owner 必須是
    # root，否則 sticky 什麼也擋不住（目錄 owner 本來就刪得掉裡面任何檔）。job 的寫入權
    # 改由 `build_entry()` 依 `UNTRUSTED_EXECUTION_PRINCIPALS` 產生具名 ACL——它**也**在
    # writers 裡，因此 `required_write_targets()` 仍然機械地把這棵樹放進該帳號的 RWP。
    #
    # **R-6 不變**：樹裡的 **token 葉檔**（`auth.json`）仍由 job 擁有、仍可被它刪除或
    # 替換。那是必要的（token 過期要 refresh 得回來），sticky 保護的是 root 擁有的那些檔。
    TrustRootAsset(
        "builder-codex-state", _T0, _JV, None,
        (Principal.INSTALLER, Principal.BUILDER), (Principal.BUILDER,),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.executor_credential_of",),
        note=(
            "builder 帳號的 `$CODEX_HOME` 整棵（`~/.codex`，落點由 "
            "`PathLayout.executor_credential_relpath` 這個**部署決定**的 head 導出；"
            "token 葉檔是它的 tail，預設 `auth.json`）。\n"
            "**#698 起與 `reviewer-planner-codex-state` 逐字同形**。在此之前它是 #640 "
            "裁決 (b) 的 `IN_PLACE_FILE`（資產 id `builder-executor-credential`），"
            "而那個形態的代價是**降權 unit 下的 codex 起不來**——#685 把它記成「已知並"
            "記錄的代價」，#698 的裁決把它連同 reviewer-planner 那半邊一起收掉。\n"
            "**這買到的是什麼、不是什麼**：把 operator 的憑證複製給 job 帳號，代表 job "
            "用的是**同一個 provider 帳號**。四分買到的是**檔案系統層**的隔離（job 偷不到"
            "Manager 的 token、改不了 Manager 的 state），**不是** provider 層的獨立——與 "
            "`model-identity-overlay` 的 `independence_domain` 想表達的不是同一件事。"
            "真正的 provider 層獨立需要每個 job 帳號各自的 provider 帳號，屬未來選項"
            "（spec §R1 有完整說明）。\n"
            "**#640 的「以暫存檔 ＋ rename 原子替換的 refresh 走不通」這條限制隨形狀消失**"
            "：sticky 樹裡 job 建得了自己的檔，也 rename 得動自己的檔。它擋不住的是"
            "**別人的**檔——那正是要擋的那一個。"
        ),
    ),
    TrustRootAsset(
        "reviewer-planner-codex-state", _T0, _JV, None,
        (Principal.INSTALLER, Principal.REVIEWER, Principal.PLANNER),
        (Principal.REVIEWER, Principal.PLANNER),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.executor_credential_of",),
        note=(
            "reviewer／planner 帳號的 `$CODEX_HOME` 整棵（`~/.codex`），token 葉檔是它"
            "底下的 `auth.json`。與 `builder-codex-state` **逐字同形**（#698）。\n"
            "**為何是整棵而不是一個檔**（#686 實測，design 未預期）：唯讀時 codex 回 "
            "`Error: failed to initialize in-process app-server client: Read-only file "
            "system (os error 30)`；把 cwd 換成可寫的 `/tmp` **症狀完全相同**，維持唯讀 "
            "cwd 只把 `CODEX_HOME` 指到可寫目錄則 **rc=0、輸出正確**。阻斷點是 "
            "`CODEX_HOME`，不是 cwd。它在底下建 `state_5.sqlite`／`logs_2.sqlite`／"
            "`queue_1.sqlite`／`memories_1.sqlite`／`sessions/`／`skills/`／`plugins/`／"
            "`thread-writer-locks/`／`models_cache.json`／`installation_id`。\n"
            "**#685→#698 的形狀變更**：#685 把它做成導進 `cache/codex` 的 root-owned "
            "symlink。那個形狀讓 codex 起得來，代價是整棵樹 job-owned ⇒ 該帳號 0818 "
            "**實測植入了 `hooks.json`**（R9 T3.9）。sticky 樹保住前者、收掉後者。\n"
            "**為何不用 `CODEX_HOME=` 這個環境變數**：env 覆寫要經 "
            "`job_runner.build_job_env()` 的白名單，那是第二條要維護的放行面；讓 "
            "executor 的**預設**路徑解析就落在對的地方，job 側零改動。\n"
            "**這一格的 RWP 不再被 `cache` 那一條吃掉**（#685 時會）：樹搬出 `cache`、"
            "成為 HOME 底下的真目錄，因此 reviewer 模板 unit 多一條 `ReadWritePaths=`。"
            "**可寫面沒有變大**——同一棵 codex 狀態樹換了位置，`cache` 那一條原本就涵蓋"
            "它；換到的是「這棵樹的目錄由 root 擁有」，也就是 hooks 守得住的前提。"
        ),
    ),
    # ---- reviewer／planner 帳號的三份登入態（#685／#672 票 D；U-4 追認雙 domain）----
    #
    # ---- reviewer／planner 帳號的登入態（#685／#672 票 D；U-4 追認雙 domain）----
    #
    # **#698 起這一段只涵蓋 agy 與 claude 兩項**：同帳號的 codex 已改走
    # `HOME_STICKY_TREE`（見上方那一對 `*-codex-state`），因為只有它的狀態樹裡住著一個
    # root-owned 的 enforcement 檔。兩者的分界是 `permgen.EXECUTOR_ENFORCEMENT_LEAVES`
    # 那條規則，不是帳號——同一條規則同時決定了 builder 那一格。
    #
    # 兩項同形，因此共用這一段說明，各自的 note 只寫「這個 executor 特有的部分」。
    #
    # **形狀**：`permgen.CredentialShape.HOME_REDIRECT_TREE`——HOME 底下一條 root-owned
    # symlink，指向該帳號 `cache` 裡的一格。三條性質同時成立，而且都是機械結果：
    #
    #   1. **不新增任何可寫面**：`cache` 早已在 reviewer 模板 unit 的 `ReadWritePaths` 內
    #      （`PathLayout.job_extra_write_paths`），`_minimize()` 因此吃掉目標路徑 ⇒ unit 的
    #      `ReadWritePaths=` **逐字不變**。executor 能做的事，該帳號今天就已經能做。
    #   2. **job 換不掉指向**：symlink 落在 root-owned 的 HOME（`scaffold_directories()`
    #      產出 root:root 0755），換 symlink 需要對父目錄的寫入權。
    #   3. **writers 只有 INSTALLER**：`required_write_targets()` 只收 writer 面，因此
    #      這兩項機械地不會產生任何 RWP 條目，也不會產生任何跨帳號 ACL。
    #
    # **為什麼不是「把憑證檔登記成葉節點」**（#640 對 builder 的做法）：#686 實測 codex
    # 與 agy 要寫的都是**一整棵**狀態樹，檔名還帶版本序號（`state_5.sqlite`）——逐項列舉
    # 會在下一次 CLI 升版時無聲失效。claude 同理。
    #
    # **U-4 的追認範圍**：連同上方的 codex 那一格，`cortex-reviewer-planner` 同時持有
    # openai／google／anthropic 三個 provider 的登入態。design 的安全退步 **R-3**（該帳號
    # 被攻陷時多邊 token 一起失，而 planner 正是吃 untrusted issue 內容的角色）是**明文
    # 接受的有界殘餘風險**——它在後續任何「planner 攻擊面」討論中不得被當成未知。
    #
    # **本形狀新增的退步（R-6，明講）**：目標樹由 job 帳號擁有 ⇒ 樹裡的 token 葉檔可被
    # 該 job 刪除或替換。影響面限於該帳號自己的登入態；換到的是 agy／claude **能不能起
    # 得來**。直接後果：同一棵樹裡**不得**再放任何 root-owned 的 enforcement 檔——這條在
    # #698 之前是註解，現在由 `permgen._assert_shape_follows_enforcement_rule()` 在 import
    # 當下強制（宣告了 enforcement 檔就必須改走 `HOME_STICKY_TREE`）。
    TrustRootAsset(
        "reviewer-planner-agy-state", _T0, _JV, None,
        (Principal.INSTALLER,), (Principal.REVIEWER, Principal.PLANNER),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.executor_credential_of",),
        note=(
            "`<HOME>/.gemini` → `<HOME>/cache/gemini` 的 root-owned symlink：agy 的可寫"
            "狀態樹，token 葉檔是 `antigravity-cli/antigravity-oauth-token`。**U-7 的裁決"
            "就是這一項**（design 的選項 (a)：登記成 symlink 類資產）。\n"
            "agy 執行時往 `~/.gemini/antigravity-cli/` 寫 conversations SQLite、crashes、"
            "presence lock、builtin skills，並**自解出一個 17 MB 的可執行檔** "
            "`bin/webm_encoder`——這正是「單檔憑證」表達不了的那一類。\n"
            "**這是三項裡唯一端到端實測全綠的一格**：0818 以 `cortex-reviewer-planner` "
            "身分、在逐條複製落檔 unit 全部 property 的沙箱下跑 "
            "`agy --print … --mode plan --sandbox --model gemini-3.1-pro-high`，rc=0、輸出"
            "逐位元等於 `probe_agy_capability()` 的 expected；#686 以 `JobPlanningInvoker` "
            "端到端複驗同一結論。**0818 的部署是手動落位的，本項讓重跑產生器能重現它。**"
        ),
    ),
    TrustRootAsset(
        "reviewer-planner-claude-state", _T0, _JV, None,
        (Principal.INSTALLER,), (Principal.REVIEWER, Principal.PLANNER),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.executor_credential_of",),
        note=(
            "`<HOME>/.claude` → `<HOME>/cache/claude` 的 root-owned symlink：claude 的"
            "登入態與狀態樹，token 葉檔是 `.credentials.json`。\n"
            "**這一格是 #686 驗收矩陣裡唯一「CLI 全綠、卻做不了事」的那一列**：job 沙箱下"
            "claude 走得完整條（rc=0），回的是 "
            "`{\"is_error\":true,…,\"result\":\"Not logged in · Please run /login\"}`"
            "——擋住的是憑證，不是加固面。它同時是 `reviewer-planner` 這個帳號**唯一**"
            "在 M2（#615）之後就該有、卻從來沒有過的登入態：reviewer 的預設 executor 是 "
            "`claude`，因此本項缺席時「reviewer 已降權」這句話買到的是一個跑不動的 job。\n"
            "**job 模式下 `CLAUDE_CONFIG_DIR` 不可用**（它在 `job_runner.DENIED_ENV_NAMES` "
            "內，design D-g 的帳號隔離取代了 in-process 的一次性 config dir），因此 claude "
            "解到的就是 `$HOME/.claude`——也就是本項。\n"
            "**`$HOME` 必須在 job 內解得到**：模板模式下 shim 以 `os.execvpe` 整份換掉環境"
            "，unit 的 `Environment=HOME=` 到不了模型（#686 更正）。三項憑證的路徑**全部**"
            "以 `$HOME` 為根，因此它們與 `PSC_REVIEWER_HOME` 的宣告**必須一起成立**；"
            "產生器出的值見 `permgen.PathLayout.job_home_value()`，runbook 第 5-5c 步。"
        ),
    ),
    # ---- Manager 的 GitHub 傳輸層憑證（#666）--------------------------------
    TrustRootAsset(
        "manager-gh-credential", _T0, _MO, None,
        (Principal.MANAGER, Principal.MONITOR),
        (Principal.MANAGER, Principal.MONITOR),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.gh_credential_of",),
        note=(
            "Manager／monitor 帳號 HOME 下的 `gh` 登入態（`~/.config/gh/hosts.yml`）。"
            "形態沿用 #640 裁決 (b)：**檔案由使用它的帳號擁有（0600）、放它的目錄維持 "
            "root-owned 0755**——`hosts.yml` 是 `gh` 唯一寫回 token 的檔（`gh auth "
            "login`／`refresh` 就地覆寫它），不歸該帳號就 refresh 不回來。\n"
            "**為何必須存在（#666 實機）**：Manager system unit 帶 `ProtectHome=yes`，"
            "而 operator 的登入態在 `~/.config/gh/` 底下，`/home` 整個不可見——\n"
            "    $ sudo -u cortex-manager env HOME=<manager HOME> gh auth status\n"
            "    You are not logged into any GitHub hosts.\n"
            "monitor 起來之後兩個 github provider 一起 `degraded`；doctor 的 "
            "`gh-auth`／`gh-permissions`／`auto-label` 三個 required probe 也全紅。"
            "這是 #623／#640／#661 那一族的第五個成員，形態逐字相同：**job／服務需要"
            "的外部相依散落在 operator home，`ProtectHome` 之後不可達**。\n"
            "**與 #640 的 job 憑證形狀相同、洩漏面不同級——不得混為一談。** "
            "`builder-executor-credential` 是給 **job 帳號**的模型 provider 憑證：job "
            "拿它只能呼叫模型，且 job unit 另有 `Environment=GH_TOKEN=`／"
            "`GITHUB_TOKEN=` 把 GitHub token 清空，成果一律走 `commit-spool` 由 "
            "Manager 代理推送（D1 outbox）。本資產是給 **Manager** 的，而 Manager 是 "
            "durable state owner：這個 token **推得動 PR、關得掉 issue、改得了 "
            "label、merge 得了分支**——它洩漏的是治理平面對上游 repo 的寫入權，不是"
            "一次模型呼叫的額度。因此\n"
            "  1. 它**只**掛在 durable state owner 的 HOME 下（job 帳號刻意沒有這一層"
            "     目錄，見 `scaffold_directories()`）；\n"
            "  2. 它列在 `permgen.IN_PLACE_CONTENT_WRITE_ASSETS`——`ReadWritePaths` 掛"
            "     在**檔案本身**，父目錄連 mount 層都不開放可寫；\n"
            "  3. reader／writer 都只有 MANAGER 與 MONITOR（同一個帳號、同一個 HOME），"
            "     沒有任何 job persona 在其中，因此機械上不會產生任何跨帳號 ACL。\n"
            "**monitor 也是 writer 而不是只有 reader**：monitor 的 GitHub provider 直接"
            "跑 `gh api`（`monitor/providers.py`），token 過期時 refresh 發生在**哪一個"
            "行程**不由我們決定。只給讀取權會做出一個「平常好好的、token 到期那天 "
            "monitor 整條 provider 靜默 degraded」的部署。#622 的不變式（monitor 的 "
            "`ReadWritePaths` 嚴格窄於 Manager）不受影響：兩者都拿到這一條，而 Manager "
            "另有整棵 durable state 樹。"
        ),
    ),
    TrustRootAsset(
        "manager-gh-config", _T1, _MO, None,
        (Principal.INSTALLER,),
        (Principal.MANAGER, Principal.MONITOR),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.gh_settings_of",),
        note=(
            "同一個目錄下的**非憑證**設定（`~/.config/gh/config.yml`：editor／pager／"
            "prompt／aliases）。root-owned 0644，服務帳號唯讀。\n"
            "**它與 `manager-gh-credential` 的 owner 刻意不同，這不是疏漏**——下一個"
            "讀到這裡的人最可能做的事就是「兩個都設成同一種」，因此把理由寫在這裡：\n"
            "  - `hosts.yml` **承載 token 且會被 `gh` 自己寫回**（refresh／re-login），"
            "    不歸服務帳號就 refresh 不回來 ⇒ 檔案必須是服務帳號的、0600；\n"
            "  - `config.yml` **不承載任何憑證、也沒有任何寫回它的執行期路徑**，但它的 "
            "    `aliases` 可以宣告 `!` 開頭的 **shell alias**——讓服務帳號改得了它，"
            "    等於給 Manager 一條「自己把任意命令掛進每一次 `gh` 呼叫」的執行面。"
            "    這與三份 `.gitconfig` 維持 root-owned 的理由（`core.fsmonitor`／"
            "    `alias.*` 同樣會執行外部命令）**逐字相同**，因此結論也相同：root 擁有、"
            "    唯讀就夠。\n"
            "機械落點：`writers` 只有部署身分 ⇒ `classify_owner()` → `DEPLOYMENT` ⇒ "
            "owner＝root、`0644`、不出現在任何 unit 的 `ReadWritePaths` 上。與 "
            "`builder-gitconfig` 那一族完全同構。\n"
            "**Tier-1 而不是 Tier-0**：它改不了 token、也不直接授權發佈；但它能改變 "
            "`gh` 的行為（alias 執行面），因此不是 advisory。"
        ),
    ),
    # ---- job-visible worktree 族 -------------------------------------------
    TrustRootAsset(
        "repo-worktree", _T1, _JV, "paulsha_cortex.config.paths:repo_root",
        (Principal.BUILDER,), (Principal.BUILDER, Principal.GATE),
        IngressKind.STAGING_SPOOL,
        note=(
            "builder write_paths:['**']，可寫工作區內任何路徑（含 .cortex/.github）。"
            "#623 之後這個工作區是從 `repo-source-tree` 拉出來的 **per-job 完整 clone**，"
            "不再是共用 object store 的 git worktree。\n"
            "**#710：owner 是 Manager，builder 的可寫面走具名 ACL——不是「整個 clone 由該 "
            "job 帳號擁有」。** 那句話從 #623／#648 起就寫在本 note 與模板 unit 的註解裡，"
            "但**沒有任何程式實作它**，而且 Manager 結構上做不到：`chown` 給另一個使用者"
            "需要 `CAP_CHOWN`，Manager unit 帶 `CapabilityBoundingSet=`（空）。代價是 "
            "builder job 第一次由 daemon 經正規路徑派出來就死在 shim 的 `os.chdir()`"
            "（實機 `[Errno 13] Permission denied`）。形態因此更正為 **Manager 擁有 ＋ "
            "job 具名 ACL**（`WorkspaceReach.PER_JOB_NAMED_ACL`，見 "
            ":data:`JOB_WORKSPACE_REACH`）：`setfacl` 由目錄 owner 執行、不需要任何 "
            "capability，且 `gc`／`worktree_reclaim` 仍 `rmtree` 得掉整棵樹（交出 owner "
            "反而讓工作區回收不了）。\n"
            "**Manager 進得去這棵樹，因此不是一條可以靠 DAC 守住的邊界**——#641 收掉的是"
            "「登記表**主動授予** Manager 的跨帳號 ACL」，那條仍然不存在（本項的 readers "
            "不含 MANAGER，產生器也不為它出任何一條 `u:cortex-manager:` 的 setfacl）；"
            "但 owner 位給的存取不是 ACL 給的，收不掉。`verification` 那條「以 "
            "`cwd=<job 樹>` 執行宣告出來的命令」的提權路徑因此**只由 fail-closed ＋ #629 "
            "的第三執行身分**擋住（`candidate-worktree-unreadable-pending-gate-identity` "
            "與 gate 的拋棄式副本），不由這棵樹的權限擋住。這是實機 0817 起就成立的狀態，"
            "#710 只是讓登記表與產生器停止宣稱相反的事（#696 的同型）。\n"
            "**#641：reader 只剩 job 帳號自己，Manager 不在其中。** 本項原宣告 "
            "`readers=(MANAGER,)`，permgen 因此產出 `setfacl -m u:cortex-manager:rX "
            "<job 樹>`，理由是「交換面沿用 D2 git 讀」。#637 已把交換面整條換掉："
            "builder 在自己的 clone `git bundle create` → 寫進 Manager-owned 的 "
            "`commit-spool` → Manager 從**那個檔案** fetch。Manager 全程不需要、也不應該"
            "走進 builder 的樹（#637 的不變式測試 `test_manager_never_touches_the_builder_"
            "clone_while_harvesting` 就是釘這件事），而登記表卻主動授了那條讀取權——"
            "**測試裡成立的不變式在照登記表部署的實機上不成立**，operator 0817 實測複驗過。\n"
            "**為什麼不是「留著也沒差」**：那條 `rX` 不只是一條沒有消費者的讀取權。它"
            "唯一還在使用的消費端是 `coordinator/verification.py`——那裡不但讀 HEAD／"
            "dirty 狀態，還以 `cwd=<job 樹>` **執行**宣告出來的 check／test／full-suite "
            "命令（`_resolve_worktree_cwd`）。`pytest` 會載入該樹的 `conftest.py`／plugin，"
            "等於 builder 在 `cortex-manager` 身分下取得任意程式碼執行——正是 #629 要解的"
            "那條提權路徑（同一段論證已寫在 `gate-ledger` 的 note 與 `launcher."
            "_should_run_gates` 的 docstring）。收掉這條 ACL 因此不是「拿掉一個沒用的東西」，"
            "而是**移除該提權路徑的成立條件**：沒有它，那些命令在三分下連 spawn 都不會成功。\n"
            "**那組檢查怎麼辦**：搬到 #629 的第三執行身分（既非 builder 也非 Manager）。"
            "在那之前 `verification` 讀不到工作樹時**明確 fail-closed** 並回 "
            "`candidate-worktree-unreadable-pending-gate-identity`（理由碼與 evidence 的 "
            "`blocked_on` 欄逐字指向 #629），**不得**靜默略過、**不得**改讀 bundle "
            "（bundle 也是 builder 產的，兩邊同源會讓那道檢查退化）、更**不得**讓 builder "
            "自報「我的工作樹是乾淨的」（違反 #540 的 acceptance chain 與 #628 的作者歸屬）。\n"
            "**#629：`readers` 補回一個帳號，但不是 Manager——是 `GATE`。** gate 執行身分"
            "要重跑 operator 宣告的命令，就必須讀得到被驗的那棵樹；而它讀完做的第一件事是"
            "**複製到自己的拋棄式工作區**（`gate-worktree-pool`），命令一律在副本上跑。"
            "授的是 `rX`（讀＋traverse），**沒有 `w`**——gate 改不了 builder 的樹，因此"
            "後續 harvest 拿到的仍是 builder 自己交付的那份成果。這條與 #641 收掉的那條"
            "**形狀相同、方向相反**：`cortex-manager` 的 `rX` 是把 ACE 引到授權線內側，"
            "`cortex-gate` 的 `rX` 是把同一個 ACE 引到一個**除了自己的副本與自己那格 "
            "spool 以外什麼都碰不到**的帳號上。"
        ),
    ),
    # ---- #629：gate 執行身分的兩個資產 --------------------------------------
    TrustRootAsset(
        "gate-worktree-pool", _T1, _JV,
        "paulsha_cortex.config.paths:gate_worktree_root",
        (Principal.GATE,), (Principal.GATE,),
        IngressKind.STAGING_SPOOL,
        derived_in=(
            "config/paths.py:gate_worktree_root",
            "trust_root/permgen.py:PathLayout.gate_worktree_root",
            "coordinator/gate_runner.py:gate_worktree_dir",
        ),
        note=(
            "gate 執行身分的**拋棄式工作區** pool：`<agents_root>/gate-worktree/<job-id>/`。"
            "\n**#710 更正**：本項的形態**不**比照 `dispatch-worktree-pool`。gate 是本 pool "
            "的**唯一** writer ⇒ 單一 job writer 分支 ⇒ 產生器出的是 `chown cortex-gate` ＋ "
            "`0700`、**零 ACL**，容器 owner 就是 gate 自己；per-job 那一格由 gate 用 "
            "`shutil.copytree` 自己建，因此天生屬於它。`dispatch-worktree-pool` 之所以要"
            "容器 owner＝Manager，是因為那一棵是**三個帳號共用**的容器（見該項的 note 與 "
            ":data:`JOB_WORKSPACE_REACH`）——本項沒有那個約束，多抄一次它的形態只會讓"
            "讀的人去找一個不存在的 per-job chown 步驟。\n"
            "**為什麼是拋棄式副本而不是「工作樹對 gate 唯讀」**：唯讀在可行性上不成立"
            "——`pytest` 要寫 `.pytest_cache`／`__pycache__`，`npm test`／`cargo test`／"
            "`make` 更是必寫；把工作樹掛成唯讀只會讓每一個真實 gate 以 EROFS 收場，那正是"
            "#629 要修掉的「安全但不能用」。副本另外買到兩件事：(a) gate 的寫入**不會**"
            "污染 builder 交付的那棵樹（harvest 讀到的仍是 builder 自己的成果）；(b) 快照"
            "在單一時點取得，builder 留下的背景行程改不了 gate 跑到一半的樹（TOCTOU）。\n"
            "**誰複製**：gate 自己（它是唯一同時讀得到來源、寫得進目的地的身分）。Manager "
            "不複製——它在 #641 之後**讀不到** builder 的樹，這條刻意不回頭放寬。\n"
            "**回收**：每次 gate 執行前整格重建（`spool_slot.create_slot(reset=True)`），"
            "因此殘留副本不會累積，也不會被下一輪採信。"
        ),
    ),
    TrustRootAsset(
        "gate-ledger-spool", _T0, _JV,
        "paulsha_cortex.config.paths:gate_ledger_spool_root",
        (Principal.MANAGER, Principal.GATE), (Principal.MANAGER,),
        IngressKind.INTERPROCESS,
        derived_in=(
            "config/paths.py:gate_ledger_spool_root",
            "trust_root/permgen.py:PathLayout.gate_ledger_spool_root",
            "coordinator/gate_runner.py:gate_ledger_spool_dir",
        ),
        note=(
            "#629 gate 執行結果的**單向 spool**：`<coordinator_root>/gate-ledger-spool/"
            "<job-id>/ledger.json`。形態**逐條比照 `commit-spool`**——tree 分類 job-visible"
            "（單向 spool 一律如此），permgen 產出的實質是 Manager-owned：容器 owner＝"
            "durable_state_owner、mode 0700，producer（gate）僅獲 **`wx` 無 `r`** 的 "
            "per-account ACL；per-job 目錄由 Manager 在起 gate 當下建立（pre-seed 守衛與 "
            "seal 共用 `coordinator/spool_slot.py`，不另寫一份），消費後封口。\n"
            "**為什麼 gate 不直接寫 `gate-ledger`**：#628 已把採信端改成 "
            "`terminal_contract.foreign_evidence_author()`——**非 Manager 擁有的 ledger 一律"
            "不採信**。讓 gate 直接寫那個目錄，寫出來的檔 owner 是 `cortex-gate`，採信端會"
            "當場 `gate-ledger-foreign-author` 拒掉；要讓它被採信就得放寬那條檢查，等於把 "
            "#628 拆掉。而且 `gate-ledger` 這個資產**同時**是 exit sentinel 的落點，開放"
            "寫入面等於讓一個跑 untrusted code 的帳號能偽造**任何** job 的完成狀態。\n"
            "因此權威 ledger 一律由 **Manager 自己**依本 spool 的內容重寫一份到 "
            "`gate-ledger`（`coordinator/gate_runner.py`），作者歸屬與 #628 逐字不變；"
            "spool 內容一律以**不受信任輸入**對待（schema 嚴格驗證 ＋ gate 名稱必須落在 "
            "operator 的 `PSC_GATE_CMD_*` 宣告集合內）。"
        ),
    ),
    TrustRootAsset(
        "dispatch-worktree-pool", _T1, _JV, "paulsha_cortex.config.paths:worktree_root",
        (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER), (Principal.MANAGER,),
        IngressKind.STAGING_SPOOL,
        note=(
            "派工 worktree pool；reviewer 與 builder 必須分屬互不可寫域（裁決 10-2）。"
            "**#641 複驗**：容器層 `0701`（owner＝Manager）給的是「別的帳號只能 traverse "
            "進自己那格、列不出目錄」。容器層沒有任何為了「Manager 讀 job 樹」而設的額外"
            "授權（產生器對本項只出 `install -d`／`chown`／`chmod`，零 `setfacl`）。\n"
            "⚠️ **#710：本項的「零 `setfacl`」是硬性質，不是巧合**。per-job 那一格的可達性"
            "走**具名 ACL**（`repo-worktree`，見 :data:`JOB_WORKSPACE_REACH`）；只要有人為了"
            "省事把那條授權往**本項**（pool 根）下成 default ACL，**每個** job 帳號就進得去"
            "**每個** job 的目錄，裁決 10-2 的 per-job 隔離當場歸零。因此「pool 根上不得出現"
            "任何 job 帳號的 default ACL」由 "
            "`permgen._assert_job_workspace_reach_matches_the_plan()` 在 import 當下強制。\n"
            "**#710 更正**：每個 per-job 子目錄**不是** `0700 <job 帳號>`，而是 `0700 "
            "<Manager>` ＋ 該 job 帳號的具名 ACL——Manager 沒有 `CAP_CHOWN`，「spawn 時 "
            "chown 給 job 帳號」從來不曾、也不可能發生（見 `repo-worktree` 的 note）。"
        ),
    ),
    # ---- path_resolver=None：在別處以字面量推導的 Tier-0／Tier-1 資產 --------
    TrustRootAsset(
        "jobs-registry", _T0, _MO, None,
        (Principal.MANAGER, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=(
            "coordinator/registry.py:185-186", "coordinator/work_actions.py:4184,5097",
            "coordinator/manager_daemon.py:1199", "porcelain/request.py:11",
        ),
        note="Job/Slice/workflow-run registry；四處建立 registry，monitor/providers.py:264 繞過直讀。",
    ),
    TrustRootAsset(
        "review-verdict", _T0, _JV, None,
        (Principal.REVIEWER, Principal.ANY_SAME_UID), (Principal.REVIEWER,),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=("coordinator/review.py:22-23,176-185",),
        note=(
            "§3 最短攻擊路徑：builder 同 UID 可直接代寫 reviewer 的 "
            ".psc-review-verdict.json。**Phase 2a 起已非權威來源**——權威通道改為 "
            "review-verdict-spool；本項僅保留為過渡期 legacy fallback（只對 Phase 2a "
            "之前派工、job row 無 review_verdict_channel 標記的 reviewer job 生效），"
            "採信時記 WARN＋DiagnosticReason。過渡期結束即應除役。\n"
            "**#641：reader 不再含 Manager——這是 `repo-worktree` 的同型殘留。** 本項的"
            "落點是 reviewer 工作樹內的 `.psc-review-verdict.json`，原宣告 "
            "`readers=(MANAGER,)` 會產出 `setfacl -m u:cortex-manager:r <reviewer 樹>/"
            ".psc-review-verdict.json`。但那條授權在**它會被套用的部署上永遠沒有消費者**："
            "per-job ACL 只存在於 Phase 2b（三分）部署，而 `manager._review_verdict_source()`"
            "對帶 `review_verdict_channel == \"spool\"` 標記的 job **只**認 spool 落點、"
            "明確不回退讀 worktree；Phase 2b 部署派出的每個 reviewer job 都帶那個標記，"
            "legacy 分支只對「Phase 2a 之前派工的 in-flight job」成立——那批 job 不可能"
            "出現在一台已經套了三分 ACL 的機器上。留 `readers=(REVIEWER,)` 是誠實的：這個"
            "檔今天唯一的讀者就是擁有它的那個 job 帳號自己。"
        ),
    ),
    TrustRootAsset(
        "review-verdict-spool", _T0, _JV,
        "paulsha_cortex.config.paths:review_verdict_spool_root",
        (Principal.MANAGER, Principal.REVIEWER), (Principal.MANAGER,),
        IngressKind.INTERPROCESS,
        derived_in=(
            "config/paths.py:review_verdict_spool_root",
            "coordinator/review.py:review_verdict_spool_dir",
        ),
        note=(
            "Phase 2a §R2 受控通道：`<coordinator_root>/review-verdicts/<reviewer_job_id>/"
            "verdict.json`。**tree 分類比照 `monitor-event-spool`（單向 spool 一律 "
            "job-visible）**，但 permgen 產出的實質是 Manager-owned：容器 owner＝"
            "durable_state_owner、mode 0700，reviewer 僅獲 write-only ACL（寫得進自己"
            "那格、讀不到他人 verdict），**builder 不在 writer 面故零寫入**。dispatch 前"
            "該格必須不存在（pre-seed 守衛），Manager 落地後轉唯讀。"
        ),
    ),
    TrustRootAsset(
        "commit-spool", _T0, _JV,
        "paulsha_cortex.config.paths:commit_spool_root",
        (Principal.MANAGER, Principal.BUILDER), (Principal.MANAGER,),
        IngressKind.INTERPROCESS,
        derived_in=(
            "config/paths.py:commit_spool_root",
            "trust_root/permgen.py:PathLayout.commit_spool_root",
        ),
        note=(
            "#623／#634 成果回收的 **bundle spool**：`<coordinator_root>/commit-spool/"
            "<job-id>/`。形態**逐條比照 `review-verdict-spool`**——tree 分類 job-visible"
            "（單向 spool 一律如此），permgen 產出的實質是 Manager-owned：容器 owner＝"
            "durable_state_owner、mode 0700，producer 僅獲 **`wx` 無 `r`** 的 per-account "
            "ACL（寫得進自己那格、讀不到他人的 bundle）；per-job 目錄由 Manager 在 dispatch "
            "當下建立、落地後轉唯讀（pre-seed／seal 同一套語意）。\n"
            "**為何需要這條通道**：#634 現行的回收是「Manager 伸手進 builder 的 clone "
            "`fetch`」，那需要 (a) traverse 進 builder-owned 的 `0700` 樹——實測 "
            "`Permission denied`；(b) 為**每個 job 路徑**加 `safe.directory`——而 git 2.43 "
            "實測不吃路徑 glob，等於把 Manager 的 Tier-0 gitconfig 變成執行期可變狀態。"
            "改走 bundle 之後 builder 在自己的 clone `git bundle create` 寫進本 spool，"
            "Manager 從那個 **bundle 檔** fetch：Manager 全程不碰 builder 的樹，讀的是一個"
            "**檔案**而非 repo，dubious-ownership 與 traverse 兩個問題同時消失。\n"
            "**producer 只有 builder**：登記表裡唯一以 git commit 交付的 persona 就是它"
            "（`repo-worktree` 的 writer 只有 BUILDER）；reviewer 的交付通道是 "
            "`review-verdict-spool`、planner 的是 `dispatch-specs-tree`。多授一個 `wx` "
            "ACL 給沒有 producer 的帳號，只是多開一條無人消費的寫入面。要納入時改登記表"
            "並重跑產生器，不在此預留。\n"
            "**本項只定義資產與權限**；bundle 的產生與消費在 coordinator 側，屬後續變更。"
        ),
    ),
    TrustRootAsset(
        "job-spec-spool", _T0, _MO,
        "paulsha_cortex.config.paths:job_spec_spool_root",
        (Principal.MANAGER,), (Principal.MANAGER,),
        IngressKind.MANAGER_INTERNAL,
        derived_in=(
            "config/paths.py:job_spec_spool_root",
            "trust_root/permgen.py:PathLayout.job_spec_spool_root",
        ),
        note=(
            "0816 第三輪裁決 A+B 的帶外通道**容器**：`<coordinator_root>/job-specs/`。"
            "**#657 起它本身不再是任何 job 讀得到的目錄**——reader 只有 Manager，"
            "mode 0700、零跨帳號 ACL；降權帳號在這一層只會拿到 `derive_traverse_grants()` "
            "機械導出的 `--x`（走得進自己那格、列不出這台機器上還有誰的 job）。"
            "實際的 spec 落在 per-principal 子 spool（`job-spec-spool-<principal>`）。"
        ),
    ),
    *_job_spec_spool_assets(),
    TrustRootAsset(
        "verification-evidence", _T0, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/verification.py:305-316",),
    ),
    TrustRootAsset(
        "maintainer-attestation", _T0, _MO, None,
        (Principal.MANAGER, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=("coordinator/work_actions.py:1027-1060",),
        note="_review_attest_action：誰能寫 queue，誰就能發 maintainer review attestation。",
    ),
    TrustRootAsset(
        "completion-record", _T0, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/completion.py:74-83",),
    ),
    TrustRootAsset(
        "full-suite-evidence", _T0, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/preflight.py:68-76",),
    ),
    TrustRootAsset(
        "workflow-inputs", _T0, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/manager.py:4039",),
    ),
    TrustRootAsset(
        "workflow-evidence", _T0, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/work_bridge.py:947,514,553-579,605-620,1245,1503",),
    ),
    TrustRootAsset(
        "gate-ledger", _T0, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=(
            "coordinator/terminal_contract.py:gate_ledger_path",
            "coordinator/dispatcher.py:exit_sentinel_path",
        ),
        note=(
            "manager 的 dispatch log 目錄，**同時**是 gate ledger（`<slice>.gates.json`）"
            "與 exit sentinel（`<slice>.exit`）的落點——兩者共用同一個資產，因為它們"
            "共用同一個目錄與同一條信任性質。刻意放在 manager log_dir（模型 cwd 拿不到）"
            "——D6 要系統化的正面前例。**#604**：`writers=(MANAGER,)` 這條宣告在 Phase 2b "
            "之前只是同 UID 的巧合（wrapper script 是在 job 進程內寫這兩個檔的）；現在由"
            "程式碼強制——sentinel 改由 Manager 側的 exit 記帳 shell 寫，且採信端一律以 "
            "`terminal_contract.foreign_evidence_author()` 檢查檔案擁有者，非 Manager 產生"
            "的 ledger/sentinel 不採信。gate **執行面**尚未移出 builder（見 issue #604 的"
            "後續：需要一個既非 builder 也非 Manager 的 gate 執行身分），在那之前降權模式"
            "不產生 ledger，build 卡照 `require_ledger` fail closed。"
        ),
    ),
    TrustRootAsset(
        "delivery-journal", _T0, _MO, None,
        (Principal.MANAGER, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=(
            "coordinator/work_actions.py:516-517", "coordinator/work_bridge.py:771,838,1382,1815",
            "coordinator/manager.py:7289,8290",
        ),
        note="六處獨立字面量推導；spec §R1 要求收斂到登記表以免權限產生器有盲點。",
    ),
    TrustRootAsset(
        "provider-backoff", _T1, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/provider_backoff.py:22,38-39",),
    ),
    TrustRootAsset(
        "planning-probe-cache", _T1, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/planning_probe_cache.py:cache_path",),
        note=(
            "#684（#672 票 C）：planning capability probe 的跨 tick 結果快取"
            "（`<coordinator_root>/planning-probe-cache.json`）。\n"
            "**writers／readers 刻意只有 Manager，且刻意不進任何 job 模板 unit 的 "
            "`ReadWritePaths`**：job 不該知道別的 provider 的探測結果，更不該寫得動它"
            "——快取一旦可由 job 寫，「這個 provider 是 ready 的」就變成模型可以自證的"
            "東西，而 `select_secondary_planner()` 的整個異質性論證就建立在那個判定不是"
            "模型說了算上。tier 是 T1 而非 T0：它改不了 acceptance、也給不了 ship "
            "authority，最壞情況是讓 planning 選到一個實際不可用的 secondary（下一次"
            "呼叫即失敗）。\n"
            "快取本身 fail-closed：讀不回來一律視為 miss 重探，**絕不**沿用 ready"
            "（見 `planning_probe_cache` 的模組 docstring 對 `not_claimable` 那處"
            "刻意差異的說明）。"
        ),
    ),
    # #708：三個降權 principal 的 job log spool——由 `JOB_LOG_SPOOLS` 那一條規則
    # **機械導出**，不逐項手寫。#686 手寫了 planner 那一格（原本就在這個位置），
    # 代價是 builder／gate 兩格從缺，而 builder 那格在 define 首次收斂當天就爆
    # （#708 實機 `78/CONFIG`）。導出之後「只修一格」在結構上做不到。
    *_job_log_spool_assets(),
    TrustRootAsset(
        "planning-scratch-pool", _T1, _MO,
        "paulsha_cortex.config.paths:planning_scratch_root",
        (Principal.MANAGER,), (Principal.MANAGER, Principal.PLANNER),
        IngressKind.MANAGER_INTERNAL,
        derived_in=(
            "config/paths.py:planning_scratch_root",
            "coordinator/planning_job.py:JobPlanningInvoker",
        ),
        note=(
            "#686（#672 票 E）：降權 planning job 的 per-invocation **唯讀** scratch "
            "（`<coordinator_root>/planning-scratch/<instance>`，模型的 cwd）。\n"
            "**writers 只有 MANAGER 是本項的全部重點**——U-2 的裁決是「scratch 對 job "
            "唯讀」，而 `required_write_targets()` 只收 writer 面，因此「本根不出現在任何 "
            "job 模板 unit 的 `ReadWritePaths=`」是**機械導出**的結果，不是靠註解約定。"
            "`ProtectSystem=strict` 於是讓「模型弄髒自己的拋棄式 sandbox」**結構上不可能**"
            "，design D-d 的偵測需求隨之消失（那條偵測在 job 側由 Manager 執行本來就"
            "不可行——scratch 若可寫，弄髒它的是 job、看得到的也只有 job）。\n"
            "readers 含 PLANNER：模型要 chdir 進去、要讀得到裡面的東西，因此需要 `rX`；"
            "產生器據此出 traverse／讀取 ACL，而**不**出任何寫入授權。\n"
            "executor 需要的可寫落點（codex 的 `-o`、agy 的 log／state）改指向 unit 的 "
            "`PrivateTmp=yes` 私有 `/tmp`——per-invocation、job-owned、unit 結束即消失，"
            "且 Manager 看不到它。"
        ),
    ),
    TrustRootAsset(
        "workflow-report-journal", _T1, _MO, None,
        (Principal.MANAGER,), (Principal.MANAGER,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/manager.py:4434",),
    ),
    TrustRootAsset(
        "digest-outbox", _T1, _MO, None,
        (Principal.MANAGER,), (Principal.EXTERNAL,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/digest.py:158-161,181-188",),
        note="外送管線唯讀；發佈類 outbox mutation 屬 R7 敏感 action。",
    ),
    TrustRootAsset(
        "engineering-outcome-outbox", _T1, _MO, None,
        (Principal.MANAGER,), (Principal.EXTERNAL,), IngressKind.MANAGER_INTERNAL,
        derived_in=("coordinator/engineering_outcome.py:508-524,451-468",),
    ),
    TrustRootAsset(
        "model-identity-overlay", _T0, _MO, None,
        (Principal.OPERATOR, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.CONFIG_OVERLAY,
        derived_in=("coordinator/model_identities.py:564-565,604-631",),
        note="independence_domain 的來源；host overlay 可壓過 packaged registry（R5／R8）。",
    ),
    TrustRootAsset(
        "combo-card-override", _T0, _MO, None,
        (Principal.OPERATOR, Principal.PLANNER, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.CONFIG_OVERLAY,
        derived_in=("deck/schema.py:70-87", "deck/compile.py:201-208,740-800"),
        note="instance-local combo 壓過 packaged；workflow 定義本身即同 UID 可寫 YAML。",
    ),
    TrustRootAsset(
        "handoff-manifest", _T0, _JV, None,
        (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER), (Principal.MANAGER,),
        IngressKind.STAGING_SPOOL,
        derived_in=("persona/handoff.py:17-20", "coordinator/autonomy.py:23", "coordinator/manager.py:185"),
        note="裸 write_text、無原子性、無 mode；staging→Manager 收（Phase 2）。",
    ),
    TrustRootAsset(
        "runtime-bootstrap-env", _T0, _MO, None,
        (Principal.INSTALLER, Principal.ANY_SAME_UID), (Principal.MANAGER, Principal.MONITOR),
        IngressKind.ENV_REDIRECT,
        derived_in=("config/runtime.py:61-66", "deploy/installer.py:162"),
        note="裸寫、無 mode；實測 -rw-rw-r--。改此檔的 PSC_* 即重導整棵 durable state。",
    ),
    # ---- codex hooks：**兩個 job 帳號各一份**（#698）----
    #
    # #698 之前這裡只有一項 `codex-hooks`，路徑寫死在 builder 的 HOME 底下，理由是
    # 「reviewer／planner 的 `~/.codex` 是 job-owned 的樹，root-owned 的 hooks 放進去
    # 擋不住替換」。0818 的 R9 T3.9 把那個理由的**後果**量了出來：該帳號真的植入了
    # `hooks.json`。codex hooks **會執行命令**，因此那不是「少一層防護」而是**跨 job
    # 持久化**——上一個 job 留得下在下一個 job 啟動時執行的東西。
    #
    # 方案 A 之後兩份都登記得起來，而且是同一條規則長出來的兩列
    # （`permgen.enforcement_placements()`）：加一個跑 codex 的帳號 ⇒ 這裡自動多一份，
    # 不必也不能手寫第三項。
    #
    # **這個檔必須先存在，sticky 才擋得住什麼**：sticky 管的是「刪／改名**別人的**檔」，
    # 不管「建一個還不存在的檔」。缺這個檔時 R9 T3.9 會以「建得出新檔」翻紅——那是
    # 正確的紅字，不是誤報。部署順序見 runbook 第 4e-2b 步。
    *[
        TrustRootAsset(
            asset_id, _T0, _MO, None,
            (Principal.INSTALLER,), (Principal.MANAGER,) + tuple(job_principals),
            IngressKind.DEPLOYMENT_WRITE,
            derived_in=("deploy/hooks.py:48-60", "trust_root/permgen.py:PathLayout.enforcement_placements"),
            note=(
                f"{home_hint}/hooks.json；enforcement plane 的一部分，**root-owned**。\n"
                "它住在一棵 job 可寫的 sticky 樹裡（`*-codex-state`），三個動詞同時關上："
                "unlink／rename 被 sticky 擋（非 owner 只動得了自己的檔）、改內容被自己的 "
                "mode 擋（root:root 0644 ⇒ job 落在 `other` 位）。\n"
                "**writers 只有 INSTALLER**：`required_write_targets()` 只收 writer 面，"
                "因此本項機械地不進任何 job unit 的 `ReadWritePaths`；job 帳號對 codex 樹的"
                "寫入權來自 `*-codex-state` 那一項的具名 ACL，涵蓋不到這個檔。"
            ),
        )
        for asset_id, home_hint, job_principals in (
            ("builder-codex-hooks", "<builder HOME>/.codex", (Principal.BUILDER,)),
            (
                "reviewer-planner-codex-hooks",
                "<reviewer-planner HOME>/.codex",
                (Principal.REVIEWER, Principal.PLANNER),
            ),
        )
    ],
    TrustRootAsset(
        "work-items-yaml", _T1, _JV, None,
        (Principal.OPERATOR, Principal.BUILDER, Principal.ANY_SAME_UID), (Principal.BUILDER,),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=("monitor/correlation.py:79-80,463-487",),
        note=(
            "git-tracked correlation authority；builder ['**'] 可寫，即一次 correlation 變更。"
            "**#641：reader 不再含 Monitor——第三個同型殘留。** 本項在 `PathLayout` 的落點是"
            "**job 工作樹裡的那一份**（`<job 樹>/.cortex/work-items.yaml`，因為那裡才有不受"
            "信任的 writer），原宣告 `readers=(MONITOR,)` 會產出 `setfacl -m "
            "u:cortex-manager:r` 加一條導出的 `--x` traverse（#620）打進 job 樹。但 monitor "
            "讀的**不是那一份**：`correlation.load_work_item_overrides(repo_root)` 與 "
            "`work_actions._work_override_action` 都以 **Manager 進程自己的 `PSC_REPO_ROOT`**"
            "為根，也就是 `repo-source-tree`（Manager-owned，monitor 已列在該項的 readers "
            "裡）。builder 對這個檔的修改是 git-tracked 的，因此循 #637 的 bundle 回到來源樹"
            "——以 commit 旅行，不以跨帳號檔案讀取旅行。留 `readers=(BUILDER,)` 對應的正是"
            "「job 樹裡那一份的讀者只有 job 帳號自己」。"
        ),
    ),
)


#: spec §R1 Scenario「重複路徑推導」：同一資產在多處以字面量重複推導，Phase 2 前
#: 必須收斂。此處把已知的重複點固化為單一真相（key＝canonical asset_id）。
KNOWN_DUPLICATE_DERIVATIONS: dict[str, tuple[str, ...]] = {
    "delivery-journal": (
        "coordinator/work_actions.py:516-517", "coordinator/work_bridge.py:771",
        "coordinator/work_bridge.py:838", "coordinator/work_bridge.py:1382",
        "coordinator/work_bridge.py:1815", "coordinator/manager.py:7289",
        "coordinator/manager.py:8290",
    ),
    "jobs-registry": (
        "coordinator/registry.py:185-186", "coordinator/work_actions.py:4184",
        "coordinator/work_actions.py:5097", "coordinator/manager_daemon.py:1199",
        "porcelain/request.py:11",
    ),
    "monitor-work-items-snapshot": (
        "config/paths.py:56-57", "coordinator/claim.py:225-228",
    ),
}


@dataclass(frozen=True)
class MutationIngress:
    """spec §C mutation ingress 盤點的一項。"""

    ingress_id: str
    kind: IngressKind
    authenticated: bool          # 現況是否認證 issuer
    headless_reachable: bool     # headless persona 是否結構性可達
    note: str = ""


MUTATION_INGRESS: tuple[MutationIngress, ...] = (
    MutationIngress("control-file-queue", IngressKind.CONTROL_FILE_QUEUE, False, True,
                    "唯一門檻是能否在該目錄建檔；處理順序由 mtime 決定。"),
    MutationIngress("cli-state-path", IngressKind.CLI_STATE_PATH, False, True,
                    "work_actions.py:4184,5097 接受 --state。"),
    MutationIngress("direct-file-write", IngressKind.DIRECT_FILE_WRITE, False, True,
                    "任何同 UID 行程直寫 durable state 檔。"),
    MutationIngress("env-redirect", IngressKind.ENV_REDIRECT, False, True,
                    "PSC_* / bootstrap env 重導（runtime.py:98-134）。"),
    MutationIngress("env-named-exec", IngressKind.ENV_NAMED_EXEC, False, True,
                    "PSC_MANAGER_INSTALLER／PSC_REPLY_BRIDGE／PSC_DIGEST_DELIVERY_CMD 無 typed-argv 守衛。"),
    MutationIngress("config-overlay", IngressKind.CONFIG_OVERLAY, False, True,
                    "model-identity／combo／persona tool_allowlist_additions（只加不減、無上限）。"),
    MutationIngress("deployment-write", IngressKind.DEPLOYMENT_WRITE, False, True,
                    "unit／venv／launcher／sitecustomize／.pth／PATH／~/.codex/hooks.json；"
                    "#623 起另含**三份**帳號 HOME 下的 root-owned `.gitconfig`"
                    "（builder／reviewer-planner／manager）——同一條性質：writer 只有部署"
                    "身分，服務帳號對這些檔一律唯讀。來源樹本身**不在**此類（0817 裁決把"
                    "它的 writer 改為 Manager，見 `repo-source-tree`）；#640 起另含 "
                    "`executor-toolchain`（四個模型 CLI 的部署樹落點，root-owned、"
                    "全部 job／服務帳號唯讀＋可執行）。executor **憑證**同樣不在此類——"
                    "檔案由 job 帳號擁有才 refresh 得了，見 `builder-executor-credential`。"),
    MutationIngress("interprocess", IngressKind.INTERPROCESS, False, True,
                    "inherited FD／ptrace／/proc/<pid>/mem／signal。"),
)


# ---------------------------------------------------------------------------
# 雙向等式（spec §R1）——供 CI 機械測試。
# ---------------------------------------------------------------------------

#: config.paths／control.constants 中回傳 Path 但**不是** trust-root durable-state
#: 資產的函式；每項附理由。新增到此清單等同「明示豁免」，仍在 review 視野內。
ACKNOWLEDGED_NON_ASSET_PATHS: dict[str, str] = {
    "paulsha_cortex.config.paths:config_path": "helper（接受 *parts），非單一資產。",
    "paulsha_cortex.config.paths:worktree_root_for": "helper（接受 repo 參數），由 worktree_root 登記。",
    "paulsha_cortex.config.paths:config_root": "~/.config/paulshaclaw app 設定根，非治理 durable-state 資產。",
    "paulsha_cortex.control.constants:control_root": "委派給 config.paths.control_root；由 control-root-tree 登記。",
}

_PATH_FUNCTION_MODULES: tuple[str, ...] = (
    "paulsha_cortex.config.paths",
    "paulsha_cortex.control.constants",
)


def discover_path_functions() -> dict[str, Callable[..., object]]:
    """反射列舉 config.paths／control.constants 中每一個回傳 `Path` 的公開函式。

    回傳 `"module:function" -> callable`。這是 spec §R1 雙向等式的 LHS。
    """
    found: dict[str, Callable[..., object]] = {}
    for modname in _PATH_FUNCTION_MODULES:
        mod = importlib.import_module(modname)
        for name, obj in vars(mod).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if obj.__module__ != modname:
                continue
            # `from __future__ import annotations` → return annotation 為字串。
            if obj.__annotations__.get("return") != "Path":
                continue
            found[f"{modname}:{name}"] = obj
    return found


def registered_path_resolvers() -> set[str]:
    """登記表 RHS：所有 asset 宣告的非 None path_resolver。"""
    return {a.path_resolver for a in ASSET_REGISTRY if a.path_resolver is not None}


@dataclass(frozen=True)
class EquationResult:
    """雙向等式檢查結果。`ok` 為 True 表示登記表與 path 契約完全對齊。"""

    ok: bool
    unregistered_functions: tuple[str, ...]   # path fn 存在但未登記且未豁免 → Scenario 1 FAIL
    dangling_resolvers: tuple[str, ...]        # asset 指向不存在的 path fn
    stale_acknowledgements: tuple[str, ...]    # 豁免清單指向已不存在的 path fn

    def failure_summary(self) -> str:
        parts: list[str] = []
        if self.unregistered_functions:
            parts.append("未登記的 path 函式: " + ", ".join(self.unregistered_functions))
        if self.dangling_resolvers:
            parts.append("指向不存在函式的 resolver: " + ", ".join(self.dangling_resolvers))
        if self.stale_acknowledgements:
            parts.append("已失效的豁免項: " + ", ".join(self.stale_acknowledgements))
        return "; ".join(parts)


def check_registry_equation() -> EquationResult:
    """spec §R1：釘住 path 契約 ⟷ 登記表的雙向等式。

    - **Scenario 1**（新增 durable path 未登記）：某 path fn 既非 resolver 也未在
      `ACKNOWLEDGED_NON_ASSET_PATHS` → `unregistered_functions` 非空、FAIL 並指名。
    - resolver 指向不存在的 path fn → `dangling_resolvers`（打錯字／函式被刪）。
    - 豁免清單指向已不存在的 fn → `stale_acknowledgements`（清單過期）。
    """
    discovered = set(discover_path_functions().keys())
    resolvers = registered_path_resolvers()
    acknowledged = set(ACKNOWLEDGED_NON_ASSET_PATHS.keys())

    accounted = resolvers | acknowledged
    unregistered = tuple(sorted(discovered - accounted))
    dangling = tuple(sorted(resolvers - discovered))
    stale = tuple(sorted(acknowledged - discovered))
    return EquationResult(
        ok=not (unregistered or dangling or stale),
        unregistered_functions=unregistered,
        dangling_resolvers=dangling,
        stale_acknowledgements=stale,
    )


# ---------------------------------------------------------------------------
# 便利查詢（Phase 2 權限產生器與 R3 自檢共用）。
# ---------------------------------------------------------------------------

def asset_by_id(asset_id: str) -> TrustRootAsset:
    for a in ASSET_REGISTRY:
        if a.asset_id == asset_id:
            return a
    raise KeyError(asset_id)


def manager_owned_assets() -> tuple[TrustRootAsset, ...]:
    return tuple(a for a in ASSET_REGISTRY if a.is_manager_owned())


def job_visible_assets() -> tuple[TrustRootAsset, ...]:
    return tuple(a for a in ASSET_REGISTRY if a.tree is TrustTree.JOB_VISIBLE)


def assets_by_tier(tier: AssetTier) -> tuple[TrustRootAsset, ...]:
    return tuple(a for a in ASSET_REGISTRY if a.tier is tier)


def headless_writable_manager_owned() -> tuple[TrustRootAsset, ...]:
    """Manager-owned 但現況 headless 可寫的資產——Phase 2 必須收斂的核心清單。"""
    return tuple(a for a in manager_owned_assets() if a.headless_writable())


def personas_covered() -> frozenset[Principal]:
    """登記表 writer 面實際涵蓋到的 headless persona（spec §R1 要求三者都在）。"""
    covered: set[Principal] = set()
    for a in ASSET_REGISTRY:
        for w in a.writers:
            if w in HEADLESS_PERSONAS:
                covered.add(w)
    return frozenset(covered)
