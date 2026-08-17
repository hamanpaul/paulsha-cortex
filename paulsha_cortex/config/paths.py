"""cortex 路徑契約——鏡射主 repo 治理平面所需的 paths 子集。"""
from __future__ import annotations

import os
from pathlib import Path

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


#: `job_spec_spool_root()` 在 `coordinator_root()` 底下的目錄名。獨立成常數是為了讓
#: `coordinator/job_runner.py` 的 per-job 定址、`trust_root/permgen.py` 的 layout 與本
#: resolver 共用同一個字面量（R1 登記表的「重複路徑推導」Scenario 要求單一真相）。
JOB_SPEC_SPOOL_DIRNAME = "job-specs"


def job_spec_spool_root() -> Path:
    """trust-root Phase 2b 方案 B：降權 job 的 per-job 執行規格 spool 根。

    operator 0816 第三輪裁決 **A+B**：builder job 改經 root-owned 的
    `cortex-job@.service` 模板實例起跑。模板 unit 的 `ExecStart=` 是**固定的**
    （`<deploy_root>/bin/cortex-job-shim %i`），因此「這個 job 要跑什麼命令、在哪個
    worktree、帶哪些 env、log 寫去哪」必須由一個**帶外通道**傳遞——就是本 spool：
    `<此根>/<unit-instance-id>.json`，Manager 原子寫入，job 帳號**唯讀**（permgen 依
    R1 登記表產出 owner＝durable_state_owner、mode 0700 ＋ per-account 唯讀 ACL）。

    這是「即使持 spawn 授權的帳號被攻陷也無法向上」的另一半：unit 檔 root-owned 改不了、
    spec 檔 job 帳號寫不了，因此 job **既不能選 UID、也不能改寫自己的命令列**。
    """
    return coordinator_root() / JOB_SPEC_SPOOL_DIRNAME


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
