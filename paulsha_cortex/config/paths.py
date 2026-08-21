"""cortex 路徑契約——鏡射主 repo 治理平面所需的 paths 子集。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from .runtime import resolve_project_config_root, resolve_run_root, resolve_runtime_root


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return Path(value).expanduser()


def _resolve_root(name: str, default: Path) -> Path:
    return _env_path(name) or default


def agents_root() -> Path:
    return resolve_runtime_root("PSC_AGENTS_ROOT")


def builder_job_codex_home_root() -> Path:
    return agents_root() / "runtime" / "codex-home" / "builder"


def reviewer_job_codex_home_root() -> Path:
    return agents_root() / "runtime" / "codex-home" / "reviewer"


def builder_job_cache_root() -> Path:
    return agents_root() / "runtime" / "job-cache" / "builder"


def reviewer_job_cache_root() -> Path:
    return agents_root() / "runtime" / "job-cache" / "reviewer"


def control_root() -> Path:
    return resolve_runtime_root("PSC_CONTROL_ROOT")


def coordinator_root() -> Path:
    return resolve_runtime_root("PSC_COORDINATOR_ROOT")


def coverage_shadow_telemetry_root() -> Path:
    """v4 R1（方案 A）coverage validator shadow 的 disagreement telemetry 落點。

    每次 shadow 比對落一檔（原子寫入，見 `coordinator/coverage.py`），供兩週觀測期
    的 disagreement 分析。掛在 `coordinator_root()`（`PSC_COORDINATOR_ROOT`）底下——
    這是 coordinator 產出的 telemetry，與 monitor 的傳輸層狀態分族。
    """
    return coordinator_root() / "coverage-shadow"


#: `review_verdict_spool_root()` 在 `coordinator_root()` 底下的目錄名。獨立成常數
#: 是為了讓 `coordinator/review.py` 的 per-job 定址與本 resolver 共用同一個字面量
#: （R1 登記表的「重複路徑推導」Scenario 要求單一真相）。
REVIEW_VERDICT_SPOOL_DIRNAME = "review-verdicts"


def review_verdict_spool_root() -> Path:
    """trust-root Phase 2a（spec §R2）：reviewer verdict 的 per-job 單向 spool 根。

    背景 §3 的最短攻擊路徑是「verdict 由模型寫在 **worktree 內**」——同 UID 下
    builder 可直接代寫 reviewer 的 `.psc-review-verdict.json`，不需偷任何
    capability。§R2 因此要求 verdict「MUST NOT 停留在同 UID 可寫的 worktree 內
    作為權威來源」，改由 reviewer 經**受控通道**交付、Manager 落地。

    本函式即該通道的根：每個 reviewer job 在 `<此根>/<reviewer_job_id>/` 有且只有
    自己那一格（`coordinator/review.py` 的 `review_verdict_spool_dir()` 是唯一
    定址點）。掛在 `coordinator_root()` 底下——它是 Manager-owned 樹，Phase 2b
    的 chown／ACL 由 `trust_root/permgen.py` 依 R1 登記表機械產生：Manager 擁有
    並消費，reviewer 只獲 **write-only** ACL（寫得進自己那格、讀不到別人的），
    builder 零寫入。
    """
    return coordinator_root() / REVIEW_VERDICT_SPOOL_DIRNAME


#: `commit_spool_root()` 在 `coordinator_root()` 底下的目錄名。獨立成常數的理由與
#: `REVIEW_VERDICT_SPOOL_DIRNAME` 逐字相同：coordinator 側的 per-job 定址與本
#: resolver 必須共用同一個字面量（R1 登記表的「重複路徑推導」Scenario）。
COMMIT_SPOOL_DIRNAME = "commit-spool"


def commit_spool_root() -> Path:
    """#623／#634：builder 成果回收的 **bundle spool** 根（append-only，per-job 一格）。

    ## 為什麼成果回收要走 spool 而不是「Manager 直接 fetch builder 的 clone」

    per-job clone 模型下 builder 的整棵 clone 由 `cortex-builder` 擁有、mode `0700`。
    Manager 要 `git -C <來源樹> fetch <clone> …` 就必須：

    1. **traverse 進 builder 的樹**——實測 `cannot change to '…': Permission denied`；
    2. 為 `<clone>` 加 `safe.directory`——而 clone 路徑是 **per-job** 的，git 2.43 實測
       **不吃路徑 glob**（只認逐字相等或字面 `*`），因此每起一個 job 就要動一次
       Manager 的 gitconfig，把一個 Tier-0 檔案變成執行期可變狀態。

    operator 裁決改走 **bundle ＋ append-only spool**：builder 在**自己的** clone 裡
    `git bundle create <此根>/<job-id>/<name>.bundle …`，Manager 再從那個 **bundle 檔**
    `fetch`。Manager 全程不碰 builder 的樹（實測 `ls` 仍 `Permission denied`），而且
    Manager 讀的是一個**普通檔案**、不是 repo——dubious-ownership 與 traverse 兩個問題
    同時消失。

    ## 權限形態（與 `review_verdict_spool_root()` 逐條相同）

    掛在 `coordinator_root()` 底下的 Manager-owned 樹；Phase 2b 的 chown／ACL 由
    `trust_root/permgen.py` 依 R1 登記表資產 `commit-spool` 機械產生：容器
    owner＝durable_state_owner、mode 0700，producer（builder）只獲 **write-only** ACL
    （`wx`，無 `r`——寫得進自己那格、讀不到別人的 bundle），Manager 擁有並消費。
    per-job 目錄由 Manager 在 dispatch 當下建立、落地後轉唯讀（pre-seed／seal，與
    review verdict 通道同一套語意）。

    **本 resolver 只定義路徑**；bundle 的產生與消費在 coordinator 側，屬後續變更。
    """
    return coordinator_root() / COMMIT_SPOOL_DIRNAME


#: `gate_ledger_spool_root()` 在 `coordinator_root()` 底下的目錄名。獨立成常數的理由
#: 與 `COMMIT_SPOOL_DIRNAME` 逐字相同（R1 登記表的「重複路徑推導」Scenario）。
GATE_LEDGER_SPOOL_DIRNAME = "gate-ledger-spool"


def gate_ledger_spool_root() -> Path:
    """#629：gate 執行身分交付 ledger 的 **per-job 單向 spool** 根。

    `#604`／PR `#628` 把 gate ledger 的**作者**收斂到 Manager，但執行面留在原地：
    operator 宣告的 gate 命令（`PSC_GATE_CMD_*`）跑在 builder 完全掌控內容的工作樹
    上，`pytest` 會載入該樹的 `conftest.py`／plugin——誰執行 gate，誰就對 builder 開
    放一條任意程式碼執行。因此執行身分既不能是 builder（模型自證，違反 #540 的
    acceptance chain）、也不能是 Manager 或 reviewer／planner（兩者都在授權線內側：
    前者持 spawn 授權，後者寫 verdict）。#629 的答案是**第四個帳號** `cortex-gate`。

    本函式即它的交付通道根：每個 job 在 `<此根>/<job-id>/ledger.json` 有且只有自己
    那一格。掛在 `coordinator_root()` 底下的 Manager-owned 樹；Phase 2b 的 chown／ACL
    由 `trust_root/permgen.py` 依 R1 登記表資產 `gate-ledger-spool` 機械產生：容器
    owner＝durable_state_owner、mode 0700，gate 只獲 **write-only** ACL（`wx` 無
    `r`），Manager 擁有並消費。

    **為什麼要多一跳而不讓 gate 直接寫 `gate-ledger`**：#628 的採信端以
    `terminal_contract.foreign_evidence_author()` 檢查檔案擁有者，非 Manager 產生的
    ledger 一律不採信；而那個目錄同時是 exit sentinel 的落點。權威 ledger 因此一律
    由 Manager 依本 spool 的內容**自己重寫一份**（`coordinator/gate_runner.py`）。
    """
    return coordinator_root() / GATE_LEDGER_SPOOL_DIRNAME


#: `job_log_spool_root("reviewer")` 在 `review_verdict_spool_root()` 底下的目錄名。
#: 獨立成常數的理由與 `REVIEW_VERDICT_SPOOL_DIRNAME` 逐字相同（R1 登記表的「重複
#: 路徑推導」Scenario 要求單一真相）。
PLANNING_JOB_LOG_SPOOL_DIRNAME = "planning-logs"

#: `job_log_spool_root("builder")` 在 `commit_spool_root()` 底下的目錄名（#708）。
BUILD_JOB_LOG_SPOOL_DIRNAME = "build-logs"

#: `job_log_spool_root("gate")` 在 `gate_ledger_spool_root()` 底下的目錄名（#708）。
GATE_JOB_LOG_SPOOL_DIRNAME = "gate-logs"

#: **降權 job principal → (該 principal 既有的輸出通道, 掛在它底下的目錄名)。**
#:
#: 這是 #708 那條規則的 path 側一半：**凡經模板 unit 派出的 job，shim 都會在接管
#: stdio 之前 `os.open()` 它的 log**（`coordinator/job_shim.py:_take_over_stdio`），
#: 因此每個降權 principal 都必須有一個它寫得進去的 log 落點——否則那個 job 死在
#: 「它能記錄失敗之前」，Manager 端只看得到 `78/CONFIG`。
#:
#: **每一列的通道都是既有的，不新開。** #686 為 planner 做這件事時的論證逐字適用於
#: 另外兩個（完整說明見 :func:`job_log_spool_root`）：掛在該 principal
#: 今天**唯一**既是 Manager-owned、又對它開放寫入的落點底下，於是
#: `permgen.read_write_paths()` 的 `_minimize()` 把它從模板 unit 的 `ReadWritePaths=`
#: 吃掉 ⇒ **可寫面逐字不變、default ACL 自動繼承、零部署動作**。
#:
#: - `builder` → `commit-spool`（成果 bundle 的 per-job spool，#623／#634）
#: - `reviewer` → `review-verdict-spool`（#599／#638；planning 的通道，#686）
#: - `gate` → `gate-ledger-spool`（#629 的 ledger 交付通道）
#:
#: **本表與 `trust_root.registry.JOB_LOG_SPOOLS` 是成對契約**（比照
#: `job_spec_spool_for()` 與 `permgen.PathLayout.job_spec_spool_for()`）：本模組
#: 刻意不 import `trust_root`（path 契約對治理平面零依賴），因此兩邊各有一份字面量，
#: 由 `tests/test_job_log_spool_708.py` 逐列釘住相等。
_JOB_LOG_SPOOL_CHANNELS: dict[str, tuple[Callable[[], Path], str]] = {
    "builder": (commit_spool_root, BUILD_JOB_LOG_SPOOL_DIRNAME),
    "reviewer": (review_verdict_spool_root, PLANNING_JOB_LOG_SPOOL_DIRNAME),
    "gate": (gate_ledger_spool_root, GATE_JOB_LOG_SPOOL_DIRNAME),
}


def job_log_spool_root(principal_id: str) -> Path:
    """#708：**該降權 principal 專屬**的 job log spool 根。

    `principal_id` 是 `trust_root.registry.Principal` 的 `value`（`builder`／
    `reviewer`／`gate`），由登記表機械導出；形狀驗證與
    :func:`job_spec_spool_for` 共用同一條 regex——這兩個值都會被接進絕對路徑，
    容忍 `..`／`/` 等於把「log 一定落在 Manager-owned 樹裡」交給呼叫端自律。

    表上查無這個 principal 時 **fail-closed**（不猜、不落回預設）：猜出來的路徑
    會讓 permgen 對一條不存在的 ACL 出命令、而 job 在實機上以 `78/CONFIG` 收場
    ——那正是 #657 與本票各買過一次的失效模式。

    ## 為什麼掛在既有通道**底下**而不是自己一個根（#686 的原始論證，逐字適用三格）

    #686 design D3 的第一句是「**不新開通道**」，U-3 更把「新開一條 job→Manager 的
    寫入面」明列為**未決、待 operator 裁決**的事項。每個降權帳號今天**唯一**既是
    Manager-owned、又對它開放寫入的落點就是它自己那條輸出通道
    （`user:<帳號>:wx`、default ACL 會傳給子項）。掛在它底下因此：

    1. **不新增任何寫入面**——那個帳號本來就寫得進這棵樹，多的只是它自己那一格；
    2. **不需要任何部署動作**——`permgen.read_write_paths()` 的 `_minimize()` 會把被
       涵蓋的子路徑吃掉，模板 unit 的 `ReadWritePaths=` 逐字不變，default ACL 自動繼承；
    3. 仍是**登記表資產**——各自有 note、writer／reader 面、ACL 命令，治理面沒有因為
       省了一條 RWP 而消失。

    真正**不能**沿用的是既有的 Manager dispatch log 目錄（builder 是
    `<coordinator_root>/logs/workflow/`，reviewer 是 `<agents_root>/runtime/review/`）：
    前者住著 gate ledger 與 exit sentinel（開放它等於把 #604 的作者性保證賣掉），
    後者 Manager 自己都建不出那層目錄（`/var/lib/cortex/runtime` 是 root-owned 0755）。

    ## per-invocation 那一格的形態

    每次派工在 `<此根>/<key>/` 有且只有自己那一格（生命週期走
    `coordinator/spool_slot.py`，與三個 spool 同一份實作），log 檔由 **Manager 預先
    建立**（mode `0620`，見 `spool_slot.JOB_LOG_FILE_MODE`），job 只是以 `O_APPEND`
    接管它——理由與那個 mode 的完整論證見該常數。
    """

    name = str(principal_id or "").strip()
    if not _SPOOL_PRINCIPAL_RE.match(name):
        raise ValueError(
            f"principal id 不合法（只允許 ^[a-z][a-z0-9-]*$）: {principal_id!r}"
        )
    channel = _JOB_LOG_SPOOL_CHANNELS.get(name)
    if channel is None:
        raise ValueError(
            f"principal {name!r} 沒有登記 job log spool 通道——"
            "凡經模板 unit 派出的 job 都要寫 log，因此每個降權 principal 都必須在 "
            "`_JOB_LOG_SPOOL_CHANNELS` 上有一列（#708）。"
        )
    channel_root, dirname = channel
    return channel_root() / dirname


#: `planning_scratch_root()` 在 `coordinator_root()` 底下的目錄名（同上，單一真相）。
PLANNING_SCRATCH_DIRNAME = "planning-scratch"


def planning_scratch_root() -> Path:
    """#686（#672 票 E）：降權 planning job 的**唯讀** per-invocation scratch pool 根。

    這一格是 design D-a／D-c 的 job 側對應：模型的 cwd 必須是一個一次性的、不是
    operator 樹的目錄。U-2 的裁決是**唯讀**（design 的傾向 (2)）——本根**刻意不出現在
    任何 job 模板 unit 的 `ReadWritePaths=`** 中，因此 `ProtectSystem=strict` 下模型連
    寫都寫不進去：design D-d 的「模型是否弄髒了自己的拋棄式 sandbox」偵測需求因此
    **結構上消失**，而不是退步成一條做不到的偵測（R-1）。

    登記表資產 `planning-scratch-pool` 的 writers 只有 MANAGER、readers 含 PLANNER
    ——「不進 RWP」因此也是**機械導出**的結果，不是靠註解約定：`required_write_targets()`
    只收 writer 面。

    executor 需要的可寫落點（codex 的 `-o`、agy 的 log/state）改指向 unit 的
    `PrivateTmp=yes` 私有 `/tmp`：那是 per-invocation、job-owned、unit 結束即消失的，
    而 Manager 看不到它——「弄髒它不產生任何後果」在這裡是結構事實。
    """
    return coordinator_root() / PLANNING_SCRATCH_DIRNAME


def gate_worktree_root() -> Path:
    """#629：gate 執行身分的**拋棄式工作區** pool 根（`<agents_root>/gate-worktree`）。

    gate 命令一律在**副本**上跑，不在 builder 交付的那棵樹上跑：

    - **唯讀不可行**——`pytest` 要寫 `.pytest_cache`／`__pycache__`，`npm test`／
      `cargo test`／`make` 更是必寫。把工作樹掛成唯讀只會讓每個真實 gate 以 EROFS
      收場，那正是 #629 要修掉的「安全但不能用」。
    - 副本另外買到：gate 的寫入不污染 builder 交付的樹（harvest 讀到的仍是 builder
      自己的成果），且快照在單一時點取得，builder 留下的背景行程改不了跑到一半的樹。

    複製由 **gate 自己**執行（它是唯一同時讀得到來源、寫得進目的地的身分）；#641 之後
    Manager 讀不到 builder 的樹，那條刻意不回頭放寬。
    """
    return agents_root() / "gate-worktree"


#: `job_spec_spool_root()` 在 `coordinator_root()` 底下的目錄名。獨立成常數是為了讓
#: `coordinator/job_runner.py` 的 per-job 定址、`trust_root/permgen.py` 的 layout 與本
#: resolver 共用同一個字面量（R1 登記表的「重複路徑推導」Scenario 要求單一真相）。
JOB_SPEC_SPOOL_DIRNAME = "job-specs"


def job_spec_spool_root() -> Path:
    """trust-root Phase 2b 方案 B：降權 job 的 per-job 執行規格 spool **容器**。

    operator 0816 第三輪裁決 **A+B**：builder job 改經 root-owned 的
    `cortex-job@.service` 模板實例起跑。模板 unit 的 `ExecStart=` 是**固定的**
    （`<deploy_root>/bin/cortex-job-shim %i`），因此「這個 job 要跑什麼命令、在哪個
    worktree、帶哪些 env、log 寫去哪」必須由一個**帶外通道**傳遞——就是本 spool。

    **#657 起本函式回傳的是容器，不是任何 job 讀得到的目錄。** 實際的 spec 落在
    per-principal 的子 spool（:func:`job_spec_spool_for`），容器本身維持
    owner-only 0700、對每個降權帳號只有機械導出的 `--x` traverse——因此沒有任何
    job 帳號列得出「這台機器上還有誰的 job」。

    這是「即使持 spawn 授權的帳號被攻陷也無法向上」的另一半：unit 檔 root-owned 改不了、
    spec 檔 job 帳號寫不了，因此 job **既不能選 UID、也不能改寫自己的命令列**。
    """
    return coordinator_root() / JOB_SPEC_SPOOL_DIRNAME


#: per-principal spool 的目錄名形狀。只允許小寫字母／數字／`-`，且必須以字母開頭
#: ——這個字串會被接進絕對路徑並寫進 root-owned unit 的 `Environment=`，容忍
#: `..`／`/` 等於把「spec 一定落在 Manager-owned 樹裡」這條性質交給呼叫端自律。
_SPOOL_PRINCIPAL_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def job_spec_spool_for(principal_id: str) -> Path:
    """#657：**該降權 principal 專屬**的 spec spool（`<容器>/<principal>`）。

    ## 為什麼不是一個共用 spool

    #629 的 gate 執行身分落地後，實機上每個 gate job 都以 `78/CONFIG` 收場：
    `cortex-gate-job@.service` 與 builder 的模板 unit 指向**同一個** spool，而登記表
    只授 builder 唯讀 ACL。shim 是 systemd 套完 `User=` **之後**才跑的，因此它以 job
    身分讀 spec ⇒ 必然 `EACCES`。reviewer／planner 是同一型（#652 未驗到這層）。

    修法有兩個候選：把共用 spool 的 reader 面擴成「全部降權 principal」，或每個
    principal 一個 spool 根。取後者，理由是**可稽核性**：這樣「哪個身分讀哪個 spool」
    是 root-owned unit 檔上可以逐字讀懂的一行（`Environment=PSC_JOB_SPEC_SPOOL=`），
    而不是一組共用目錄上三條 ACL 的交集；而且它不必新開「跨 persona 互讀 spec」這個
    性質——那在共用 spool 下是無法避免的副作用（spec 內容是命令列與白名單 env）。

    第三個候選（spec 檔由 root 預先 chown 給該 job 帳號）不成立：spec 是 Manager 寫的，
    chown 給別的 owner 需要 root，而「cortex 任何元件永不具 root」是既有裁決。

    `principal_id` 是 `trust_root.registry.Principal` 的 `value`（`builder`／
    `reviewer`／`gate`），由登記表機械導出；本模組不 import trust_root（path 契約
    對治理平面零依賴），故以字串傳入並在此驗形狀。
    """

    name = str(principal_id or "").strip()
    if not _SPOOL_PRINCIPAL_RE.match(name):
        raise ValueError(
            f"principal id 不合法（只允許 ^[a-z][a-z0-9-]*$）: {principal_id!r}"
        )
    return job_spec_spool_root() / name


def specs_root() -> Path:
    return resolve_runtime_root("PSC_SPECS_ROOT")


def run_root() -> Path:
    return resolve_run_root()


def monitor_state_root() -> Path:
    """Durable Monitor state; distinct from the runtime socket directory."""
    return resolve_runtime_root("PSC_MONITOR_STATE_ROOT")


def work_items_snapshot_path() -> Path:
    return monitor_state_root() / "work-items.snapshot.json"


def github_issue_sync_path() -> Path:
    """#506 / D3：GitHub issues 增量同步的 per-repo 游標／ETag／鏡像投影。

    與 `work_items_snapshot_path()` 分開存放：這份是 monitor 對 GitHub 的**傳輸層
    狀態**（下一次要從哪個 `since` 續讀、上次的 ETag），不是讀模型；讀模型損壞時
    重建的代價是一次全量掃描，這份損壞時的代價也一樣，但兩者的生命週期無關。
    """
    return monitor_state_root() / "github-issue-sync.json"


def monitor_event_spool_root() -> Path:
    """#506 / D4：本機事件入口（spool）目錄。

    與 `github_issue_sync_path()` 同一族（monitor 的傳輸層狀態），但生命週期相反：
    這裡的檔案是**別的行程**（D5 的 headless agent hook）寫進來、monitor 消費掉即
    消失的一次性 hint，不是 monitor 自己的 durable 狀態。目錄由寫入端建立——
    monitor 掃到目錄不存在就是「這台機器沒有 hook」，不是錯誤。
    """
    return monitor_state_root() / "event-spool"


def skill_registry_root() -> Path:
    """Skill governance 治理平面根目錄（issue #204）：ledger／park state／proposal 共用。

    未宣告獨立 `PSC_*` override——沿用 `agents_root()`（已支援 `PSC_AGENTS_ROOT`
    覆寫）底下的 `registry` 子目錄，理由：這是 `~/.agents` 樹的 mutable runtime
    狀態，跟 coordinator/control/specs/monitor 屬同一族，沒必要另開一個環境變數
    造成 path 契約碎片化。
    """
    return agents_root() / "registry"


def skill_usage_ledger_path() -> Path:
    """Append-only skill usage event ledger（`schema_version`/`event_id`/... 見
    `paulsha_cortex.coordinator.skill_ledger`）。"""
    return skill_registry_root() / "skill_usage.jsonl"


def skill_park_state_path() -> Path:
    """目前已 park 的 skill 清單（可逆狀態，不含歷史紀錄／ledger 本身）。"""
    return skill_registry_root() / "skill_park.json"


def skill_park_proposals_root() -> Path:
    """Janitor 產生、尚待 operator 核准/已核准的 park proposal 檔案目錄。"""
    return skill_registry_root() / "skill_park_proposals"


def config_root() -> Path:
    return _resolve_root("PSC_CONFIG_ROOT", Path.home() / ".config" / "paulshaclaw")


def config_path(*parts: str) -> Path:
    return config_root().joinpath(*parts)


def project_config_root() -> Path:
    return resolve_project_config_root()


class RepoRootUnresolvedError(RuntimeError):
    """`PSC_REPO_ROOT` 未宣告，且呼叫端沒有顯式表態要用 cwd（#612）。

    刻意繼承 `RuntimeError` 而非 `ValueError`：daemon 的 tick isolation（#246）
    攔的是 `(ValueError, RuntimeError, OSError)`，兩者都在其中，但 `RuntimeError`
    可與 registry 那族「契約驗證失敗」的 `ValueError` 區分開來——這條不是資料不
    合法，是**執行環境沒有宣告目標 repo**。
    """


def configured_repo_root() -> Path | None:
    """只回 `PSC_REPO_ROOT` 顯式宣告的值；未宣告回 `None`（**不猜**）。

    #612：`repo_root()` 舊實作的預設值是 `Path.cwd()`，於是「沒有宣告」與「宣告
    成當下工作目錄」在型別上無從分辨，呼叫端也就無從 fail-closed。把「有沒有宣告」
    這個資訊獨立出來，讓需要判斷的呼叫端（`autonomy._infer_repo_root`）能先問
    「宣告了嗎」再決定要不要走推斷。
    """
    return _env_path("PSC_REPO_ROOT")


def repo_root(*, allow_cwd: bool = False) -> Path:
    """本 instance 治理的目標 repo 根。

    #612：**預設 fail-closed**。舊實作 `_resolve_root("PSC_REPO_ROOT", Path.cwd())`
    在未宣告時靜默退回 `Path.cwd()`，而 daemon 的 `WorkingDirectory` 正是 operator
    的真實 checkout——於是任何解析不出目標的呼叫（相對 spec 路徑、缺 env 的
    unit）都不是失敗，而是**打在錯的樹上**：`git fetch`／`rev-parse`／
    `merge-base`／worktree 建立全部落到 operator 的工作區。#623 之後 repo 源碼樹
    會搬進 Manager-owned 樹，「猜 cwd」只會更錯。

    需要 cwd 語意的呼叫端（operator 手動 CLI）必須顯式傳 `allow_cwd=True`——意圖
    寫在呼叫點上，不再由預設值默默生效。
    """
    explicit = configured_repo_root()
    if explicit is not None:
        return explicit
    if allow_cwd:
        return Path.cwd()
    raise RepoRootUnresolvedError(
        "PSC_REPO_ROOT 未宣告，拒絕退回 cwd（#612）："
        "production 動作必須有顯式的目標 repo 才能執行。"
        "請設定 PSC_REPO_ROOT，或由呼叫端顯式傳入 repo root；"
        "operator 手動 CLI 若確實要以當下工作目錄為準，請顯式 allow_cwd=True。"
    )


def _canonical_repo_root(repo: Path) -> Path:
    if repo.parent.name == ".worktrees":
        return repo.parent.parent
    return repo


def worktree_root_for(repo: Path) -> Path:
    """依給定 repo 計算 worktree pool，預設為 sibling `<repo>-worktrees`。"""
    override = _env_path("PSC_WORKTREE_ROOT")
    if override is not None:
        return override
    repo = _canonical_repo_root(repo)
    return repo.parent / f"{repo.name}-worktrees"


def worktree_root(*, allow_cwd: bool = False) -> Path:
    """coordinator 派工 worktree pool 的預設路徑。

    `allow_cwd` 原樣傳給 `repo_root()`——worktree pool 是從 repo 根推導的，repo 根
    猜錯，pool 就跟著建在錯的地方（#612）。
    """
    return worktree_root_for(repo_root(allow_cwd=allow_cwd))
