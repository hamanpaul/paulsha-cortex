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
    HEADLESS_HOOK = "headless-hook"  # D5 headless 事件 hook（寫 event-spool）
    EXTERNAL = "external"            # 外送管線／外部學習系統（唯讀）
    ANY_SAME_UID = "any-same-uid"    # 現況：同 UID 任何行程皆可寫（含全部 headless persona）


#: 三個不受信任 headless persona；spec §R1 要求盤點必須分別涵蓋，不能只封 builder。
HEADLESS_PERSONAS: frozenset[Principal] = frozenset(
    {Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER}
)


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
        (Principal.INSTALLER,),
        (
            Principal.MANAGER, Principal.MONITOR,
            Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER,
        ),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:PathLayout.repo_source_root",),
        note=(
            "`<agents_root>/repos/<slug>`——**Manager-owned 樹內的 working checkout**，"
            "同時是 monitor 的掃描目標（`workstreams/*/todo.md` 等要有工作樹，bare 沒有）"
            "與每個 job 的 **clone 來源**。#623 實測：共用 git object store 與三分隔離"
            "**互斥**（builder 要 commit 就得能寫 object store，能寫就等於邊界在 git 這層"
            "漏掉），因此 job 工作區由 `git worktree` 改為 **per-job 完整 clone**"
            "（0.5 秒／35MB）。\n"
            "**writer 只有部署身分（root）**：來源樹由 operator 以 root 更新，"
            "**全部服務帳號（含 Manager）唯讀**——owner_class 因此機械分到 DEPLOYMENT"
            "而非 MANAGER_STATE（permgen 的 ReadWritePaths 純由「誰可寫」導出，"
            "owner＝cortex-manager 會讓 Manager unit 自動拿到寫入權）。tree 仍是 "
            "MANAGER_OWNED（headless 零寫入），與 `runtime-agents-tree` 同形。\n"
            "**無 path_resolver**：程式碼解析的是**單一** repo（`PSC_REPO_ROOT` → "
            "`<此樹>/<slug>`，見 `config/paths.py:repo_root`），本資產是**容器**，"
            "沒有任何函式回傳它。"
        ),
    ),
    TrustRootAsset(
        "builder-gitconfig", _T0, _MO, None,
        (Principal.INSTALLER,), (Principal.BUILDER,),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:build_job_gitconfig",),
        note=(
            "builder 帳號 HOME 下的 root-owned `.gitconfig`（0644），內容含來源 repo 的 "
            "`safe.directory`——比照既有的 `codex-hooks`（root-owned、在 job 帳號 HOME 下），"
            "不是新概念。**為何必須存在**：per-job clone 的來源樹不屬於 job 帳號，git 的 "
            "dubious ownership 保護會讓 `git clone` 直接失敗；而 job 的 HOME 是 root-owned，"
            "它自己放不了這個檔。**為何是 Tier-0**：gitconfig 可指定 `core.fsmonitor`／"
            "`core.pager`／`alias.*` 等會執行外部命令的鍵，可寫者等於對該 job 帳號取得"
            "任意程式碼執行——與 `codex-hooks` 同一條性質。內容由 permgen 產生"
            "（`build_job_gitconfig()`），不手寫。"
        ),
    ),
    TrustRootAsset(
        "reviewer-planner-gitconfig", _T0, _MO, None,
        (Principal.INSTALLER,), (Principal.REVIEWER, Principal.PLANNER),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("trust_root/permgen.py:build_job_gitconfig",),
        note=(
            "同 `builder-gitconfig`，落在 reviewer＋planner 的 job 帳號 HOME 下"
            "（三分定案的 `cortex-reviewer-planner`）。兩個 job 帳號各一份是必要的："
            "`.gitconfig` 讀取端是 `$HOME`，而三分的硬性要求就是 reviewer 與 builder "
            "互不可寫、HOME 互不相同。"
        ),
    ),
    # ---- job-visible worktree 族 -------------------------------------------
    TrustRootAsset(
        "repo-worktree", _T1, _JV, "paulsha_cortex.config.paths:repo_root",
        (Principal.BUILDER,), (Principal.MANAGER,),
        IngressKind.STAGING_SPOOL,
        note=(
            "builder write_paths:['**']，可寫工作區內任何路徑（含 .cortex/.github）。"
            "#623 之後這個工作區是從 `repo-source-tree` 拉出來的 **per-job 完整 clone**"
            "（整個 clone 由該 job 帳號擁有），不再是共用 object store 的 git worktree。"
        ),
    ),
    TrustRootAsset(
        "dispatch-worktree-pool", _T1, _JV, "paulsha_cortex.config.paths:worktree_root",
        (Principal.BUILDER, Principal.REVIEWER, Principal.PLANNER), (Principal.MANAGER,),
        IngressKind.STAGING_SPOOL,
        note="派工 worktree pool；reviewer 與 builder 必須分屬互不可寫域（裁決 10-2）。",
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
        (Principal.REVIEWER, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=("coordinator/review.py:22-23,176-185",),
        note=(
            "§3 最短攻擊路徑：builder 同 UID 可直接代寫 reviewer 的 "
            ".psc-review-verdict.json。**Phase 2a 起已非權威來源**——權威通道改為 "
            "review-verdict-spool；本項僅保留為過渡期 legacy fallback（只對 Phase 2a "
            "之前派工、job row 無 review_verdict_channel 標記的 reviewer job 生效），"
            "採信時記 WARN＋DiagnosticReason。過渡期結束即應除役。"
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
        "job-spec-spool", _T0, _MO,
        "paulsha_cortex.config.paths:job_spec_spool_root",
        (Principal.MANAGER,), (Principal.MANAGER, Principal.BUILDER),
        IngressKind.MANAGER_INTERNAL,
        derived_in=(
            "config/paths.py:job_spec_spool_root",
            "coordinator/job_runner.py:job_spec_path",
            "coordinator/job_shim.py:load_spec",
        ),
        note=(
            "0816 第三輪裁決 A+B 的帶外通道：`<coordinator_root>/job-specs/"
            "<unit-instance-id>.json`。root-owned 的 `cortex-job@.service` 模板 unit 的 "
            "`ExecStart=` 固定為 shim，per-job 的命令／worktree／白名單 env／log 路徑改由"
            "本 spool 傳遞。**writer 只有 Manager**——builder 在 reader 面（唯讀 ACL），"
            "因此改不了自己的命令列；`User=` 完全不在本檔內（它硬寫死在 root-owned 的 "
            "unit 檔裡），spool 被竄改也無法選 UID。"
        ),
    ),
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
    TrustRootAsset(
        "codex-hooks", _T0, _MO, None,
        (Principal.INSTALLER, Principal.ANY_SAME_UID), (Principal.MANAGER,),
        IngressKind.DEPLOYMENT_WRITE,
        derived_in=("deploy/hooks.py:48-60",),
        note="$HOME/.codex/hooks.json；enforcement plane 的一部分。",
    ),
    TrustRootAsset(
        "work-items-yaml", _T1, _JV, None,
        (Principal.OPERATOR, Principal.BUILDER, Principal.ANY_SAME_UID), (Principal.MONITOR,),
        IngressKind.DIRECT_FILE_WRITE,
        derived_in=("monitor/correlation.py:79-80,463-487",),
        note="git-tracked correlation authority；builder ['**'] 可寫，即一次 correlation 變更。",
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
                    "#623 起另含 per-job clone 的來源樹與 job 帳號 HOME 下的 root-owned "
                    "`.gitconfig`——同一條性質：writer 只有部署身分，服務帳號一律唯讀。"),
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
