"""Phase 2a：權限產生器——用 R1 登記表機械產生目錄 owner／group／mode 清單。

spec §R10 Phase 2 第 2 條要求「目錄 owner／mode **由 R1 登記表產生**（權限產生器以
登記表為輸入，不手寫）」。本模組即該產生器：吃 `registry.ASSET_REGISTRY` ＋一個
**UID 方案 config**（`UidScheme`，persona→OS 帳號的映射），機械算出每個資產路徑的
目標權限，輸出 (a) 結構化清單（可轉 JSON）與 (b) 可供 Phase 2b runbook 引用的
`chown`／`chmod`／`setfacl` 命令字串。

**本模組純為產生器：只回傳資料與字串，絕不執行任何 root 操作、不 chown、不 chmod、
不建 UID。** 命令字串供 operator 在 runbook 中手動 `sudo` 執行。

## UID 方案的參數化（operator 0816 **第三輪**裁決：三分定案，二分保留為向後相容選項）

`UidScheme` 把每個 `Principal` 映射到具體 OS 帳號名，並指定：
- `durable_state_owner`：擁有 Manager-owned durable state 樹的帳號；
- `deploy_account`：enforcement plane（部署面）的擁有者（root／部署帳號）。

**三分**（**定案**，`THREE_WAY_SCHEME`＝:data:`DEFAULT_SCHEME`）：三個帳號——
`cortex-manager`（Manager＋monitor，durable state owner，**持 spawn 授權但不跑任何
模型程式碼**）／`cortex-reviewer-planner`（reviewer＋planner 的模型 job）／
`cortex-builder`（builder 的模型 job）。裁決的判準是 **「injection 可達的任何進程都
不得持有 spawn 授權」**：二分下 reviewer／planner 與 Manager 併帳，任一被 prompt
injection 攻陷即取得 polkit 的 start 授權；三分把模型執行面整個移出授權帳號。

**二分**（`TWO_WAY_SCHEME`，向後相容）：`cortex-builder`＋`cortex-svc`（Manager＋
reviewer＋planner＋monitor 共用）。保留是為了讓已按二分裝好的部署不必一次到位，
**不是**新部署的建議值。

兩個方案套用**同一套 policy 函式**，因此都能對登記表每一項產出一致（滿足同一組
不變式）的權限集合：Manager-owned／deployment 樹對任何 headless 帳號皆不可寫，
job-visible 樹由對應 job 帳號寫、跨 persona 互不可寫。全部既有不變式測試對兩案
逐一參數化跑（`tests/test_trust_root_permgen_p2a.py`／`_p2b.py` 的 `ALL_SCHEMES`）。

## 降權機制（0816 第三輪裁決 A+B）

- **A**＝上述三分。
- **B**＝root-owned 模板 unit：`build_job_unit()` 產出 `cortex-job@.service`，
  `User=` 硬寫死；`build_polkit_rule(plan=TEMPLATE)`（**預設**）只放行該模板實例的
  start／stop。per-job 參數走 Manager-owned spec spool（登記表資產 `job-spec-spool`），
  由 `build_job_shim()` 產出的 root-owned shim 讀取後 exec。
- C（Manager 端封閉 argv 產生器）自動保留為第三層，見 `coordinator/job_runner.py`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from . import registry
from .registry import (
    HEADLESS_PERSONAS,
    AssetTier,
    IngressKind,
    Principal,
    TrustRootAsset,
    TrustTree,
)


# ---------------------------------------------------------------------------
# UID 方案 config（參數化——二分為預設，同一資料結構可表達三分）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UidScheme:
    """persona→OS 帳號的映射方案。

    `account_of` 必須涵蓋登記表出現過的所有非 `ANY_SAME_UID` principal
    （`ANY_SAME_UID` 是「現況同 UID 任意行程」的標記，正是 Phase 2 要移除的對象，
    故**不**映射到任何目標帳號）。
    """

    scheme_id: str
    account_of: Mapping[Principal, str]
    #: 擁有 Manager-owned durable state 樹的帳號（Manager 本人的服務帳號）。
    durable_state_owner: str
    #: enforcement plane（unit／venv／launcher／env／codex hooks）的擁有者。
    deploy_account: str = "root"
    #: 外部唯讀消費者（digest／engineering-outcome outbox 的下游）帳號。
    external_reader: str = "cortex-outbox"

    def resolve(self, principal: Principal) -> str | None:
        """回傳 principal 的目標帳號；`ANY_SAME_UID` 與未映射者回傳 None。"""
        if principal is Principal.ANY_SAME_UID:
            return None
        if principal is Principal.INSTALLER:
            return self.deploy_account
        if principal is Principal.EXTERNAL:
            return self.external_reader
        return self.account_of.get(principal)

    def group_of(self, account: str) -> str:
        """帳號的 primary group（慣例：每帳號一個同名 group）。"""
        return account

    def headless_accounts(self) -> frozenset[str]:
        """全部 headless persona（含 headless hook）解析到的帳號集合。

        Manager-owned／deployment 樹對這些帳號**必須**零寫入權——這是本產生器的
        核心不變式。
        """
        accts: set[str] = set()
        for p in list(HEADLESS_PERSONAS) + [Principal.HEADLESS_HOOK]:
            a = self.resolve(p)
            if a is not None:
                accts.add(a)
        return frozenset(accts)


#: 二分（**向後相容選項**，非預設）：builder 一個帳號，其餘 headless／Manager／
#: monitor 共用 cortex-svc。0816 第三輪裁決前的方案；已按此裝好的部署可續用，
#: 但新部署一律走 :data:`DEFAULT_SCHEME`（三分）。
TWO_WAY_SCHEME = UidScheme(
    scheme_id="two-way",
    account_of={
        Principal.MANAGER: "cortex-svc",
        Principal.MONITOR: "cortex-svc",
        Principal.REVIEWER: "cortex-svc",
        Principal.PLANNER: "cortex-svc",
        Principal.BUILDER: "cortex-builder",
        Principal.HEADLESS_HOOK: "cortex-builder",
        Principal.OPERATOR: "operator",
    },
    durable_state_owner="cortex-svc",
    deploy_account="root",
)

#: 三分（**定案**）：把 cortex-svc 拆成 cortex-manager（durable state owner，持 spawn
#: 授權、不跑模型）與 cortex-reviewer-planner（reviewer＋planner 的模型 job 帳號，
#: 不持有 durable state、不持 spawn 授權）。**與二分共用同一套 policy，僅換 config。**
THREE_WAY_SCHEME = UidScheme(
    scheme_id="three-way",
    account_of={
        Principal.MANAGER: "cortex-manager",
        Principal.MONITOR: "cortex-manager",
        Principal.REVIEWER: "cortex-reviewer-planner",
        Principal.PLANNER: "cortex-reviewer-planner",
        Principal.BUILDER: "cortex-builder",
        Principal.HEADLESS_HOOK: "cortex-builder",
        Principal.OPERATOR: "operator",
    },
    durable_state_owner="cortex-manager",
    deploy_account="root",
)

SCHEMES: dict[str, UidScheme] = {
    TWO_WAY_SCHEME.scheme_id: TWO_WAY_SCHEME,
    THREE_WAY_SCHEME.scheme_id: THREE_WAY_SCHEME,
}

#: **定案方案**（0816 第三輪裁決 A）。CLI／產生器未指定 scheme 時一律用這個——
#: 「預設就是最安全的那一個」是刻意的：要退回二分必須顯式打出 `two-way`，
#: 打錯字不會靜默退回較寬鬆的方案（`SCHEMES` 查無即拒）。
DEFAULT_SCHEME: UidScheme = THREE_WAY_SCHEME
DEFAULT_SCHEME_ID: str = DEFAULT_SCHEME.scheme_id


# ---------------------------------------------------------------------------
# 權限模型
# ---------------------------------------------------------------------------

class OwnerClass(Enum):
    """資產的擁有類別，決定 owner 帳號來源。"""

    DEPLOYMENT = "deployment"        # enforcement plane：owner＝deploy/root
    MANAGER_STATE = "manager-state"  # Manager-owned durable state：owner＝durable_state_owner
    JOB = "job"                      # job-visible：owner＝對應 job 帳號（或 runtime 逐案 chown）


@dataclass(frozen=True)
class AclEntry:
    """單條 POSIX ACL（供跨帳號的精確授權；Manager-owned 上只會出現唯讀條目）。"""

    account: str
    perms: str          # "rX"（讀，dir 自動含 traverse）／"rwx"／"wx"
    default: bool = False  # 是否為 default ACL（dir 內新建物件繼承）

    @property
    def writable(self) -> bool:
        return "w" in self.perms

    def render(self, path: str) -> str:
        flag = "-d -m" if self.default else "-m"
        return f"setfacl {flag} u:{self.account}:{self.perms} {path}"


@dataclass(frozen=True)
class PermissionEntry:
    """單一資產路徑的目標權限（機械產生，不含任何實際 IO）。"""

    asset_id: str
    tier: str
    tree: str
    owner_class: OwnerClass
    owner: str
    group: str
    mode: int                     # 0o 值，僅 0o777 部分
    is_directory: bool
    #: 目標可寫帳號（含 owner 與 ACL 授寫者）——供不變式測試。
    writer_accounts: frozenset[str]
    reader_accounts: frozenset[str]
    acls: tuple[AclEntry, ...] = ()
    #: True＝容器的 per-child owner 由 launcher 在 spawn 時逐案 chown（如 worktree pool）。
    runtime_managed: bool = False
    #: 現況 writer（含 ANY_SAME_UID）——保留供對照，非目標。
    legacy_writers: tuple[str, ...] = ()
    rationale: str = ""
    #: 待 operator 拍板的未決點（例如部署路徑最終位置）。
    open_points: tuple[str, ...] = ()

    @property
    def mode_str(self) -> str:
        return format(self.mode, "04o")

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "tier": self.tier,
            "tree": self.tree,
            "owner_class": self.owner_class.value,
            "owner": self.owner,
            "group": self.group,
            "mode": self.mode_str,
            "is_directory": self.is_directory,
            "writer_accounts": sorted(self.writer_accounts),
            "reader_accounts": sorted(self.reader_accounts),
            "acls": [
                {"account": a.account, "perms": a.perms, "default": a.default}
                for a in self.acls
            ],
            "runtime_managed": self.runtime_managed,
            "legacy_writers": list(self.legacy_writers),
            "rationale": self.rationale,
            "open_points": list(self.open_points),
        }

    def commands(self, path: str) -> list[str]:
        """產生本資產的 chown／chmod／setfacl 命令字串（**只回傳字串，不執行**）。

        `path` 由呼叫端提供（runbook 以 shell 變數帶入真實路徑）；未提供具體路徑時
        以清楚標記的 placeholder 呈現。
        """
        cmds = [
            f"chown {self.owner}:{self.group} {path}",
            f"chmod {self.mode_str} {path}",
        ]
        for acl in self.acls:
            cmds.append(acl.render(path))
            # dir 需同時設 access 與 default ACL，讓新建物件繼承。
            if self.is_directory and not acl.default:
                cmds.append(AclEntry(acl.account, acl.perms, default=True).render(path))
        return cmds


# ---------------------------------------------------------------------------
# 目錄／檔案推斷（登記表未編碼 file/dir，以 resolver 名與 asset_id 機械推斷）
# ---------------------------------------------------------------------------

_DIR_ASSET_TOKENS = (
    "tree", "root", "queue", "pool", "spool", "proposals", "outbox", "combos",
)
_DIR_ASSET_IDS = frozenset({
    "combo-card-override",       # <agents_root>/config/combos/ 目錄
    "skill-park-proposals",
    "digest-outbox",
    # 以下皆為 evidence／journal **目錄**（逐 slice／逐 run 一檔），asset_id 的
    # token heuristic 會誤判成單檔，故明列（路徑對照見 `PathLayout.asset_paths`）：
    "verification-evidence",        # <coordinator>/evidence/verification/
    "maintainer-attestation",       # <coordinator>/evidence/maintainer-review/
    "completion-record",            # <coordinator>/evidence/completion/
    "full-suite-evidence",          # <coordinator>/evidence/full-suite/
    "workflow-inputs",              # <coordinator>/evidence/workflow-inputs/
    "workflow-evidence",            # <coordinator>/evidence/workflow/
    "workflow-report-journal",      # <coordinator>/workflow-report-transactions/
    "engineering-outcome-outbox",   # <coordinator>/engineering-outcomes/<repo>.jsonl 的容器
    "gate-ledger",                  # <agents_root>/runtime/dispatch/（manager log_dir）
    "review-verdict-spool",         # <coordinator>/review-verdicts/<reviewer_job_id>/
})
_FILE_ASSET_IDS = frozenset({
    "control-daemon-lock",
    "control-status",
})


def infer_is_directory(asset: TrustRootAsset) -> bool:
    """機械推斷資產是目錄或檔案。

    優先序：明列覆寫 → resolver 名後綴（`_path`＝檔、`_root`/`_dir`＝目錄）→
    asset_id token。path_resolver=None 的葉資產以 asset_id 推斷，**屬 heuristic**，
    runbook 標明 operator 應對 path_resolver=None 的葉逐一確認 file/dir。
    """
    if asset.asset_id in _DIR_ASSET_IDS:
        return True
    if asset.asset_id in _FILE_ASSET_IDS:
        return False
    if asset.path_resolver is not None:
        fn = asset.path_resolver.split(":", 1)[1]
        if fn.endswith("_path"):
            return False
        if fn.endswith("_root") or fn.endswith("_dir"):
            return True
    return any(tok in asset.asset_id for tok in _DIR_ASSET_TOKENS)


# ---------------------------------------------------------------------------
# owner class 分類 + policy
# ---------------------------------------------------------------------------

def classify_owner(asset: TrustRootAsset) -> OwnerClass:
    """把資產分到 DEPLOYMENT／MANAGER_STATE／JOB。

    - enforcement plane（`DEPLOYMENT_WRITE`，或 writer 含 INSTALLER 的 bootstrap env）
      → DEPLOYMENT（owner＝root/deploy）。
    - control file queue（`CONTROL_FILE_QUEUE`）：登記表現況標為 job-visible（任何同
      UID 可建檔），但 spec §R4 明定其提交改走 Manager-owned authenticated socket、
      queue 目錄不再世界可寫——故目標 owner 收斂為 MANAGER_STATE（附 open point）。
    - Manager-owned 樹的其餘資產 → MANAGER_STATE。
    - 其餘 job-visible 樹 → JOB。
    """
    if asset.ingress_kind is IngressKind.DEPLOYMENT_WRITE:
        return OwnerClass.DEPLOYMENT
    if Principal.INSTALLER in asset.writers:
        # runtime bootstrap env：現況 installer 裸寫、無 mode——目標由 deploy 身分持有。
        return OwnerClass.DEPLOYMENT
    if asset.ingress_kind is IngressKind.CONTROL_FILE_QUEUE:
        return OwnerClass.MANAGER_STATE
    if asset.tree is TrustTree.MANAGER_OWNED:
        return OwnerClass.MANAGER_STATE
    return OwnerClass.JOB


def _dir_file_mode(is_dir: bool, owner_bits: int, group_bits: int, other_bits: int) -> int:
    """組出 mode；owner/group/other 各給 rwx 位（0-7）。dir 才有 x 意義。"""
    return (owner_bits << 6) | (group_bits << 3) | other_bits


def _mask_write(bits: int) -> int:
    """移除 write 位（用於確保 group/other 永不可寫）。"""
    return bits & ~0o2 & 0o7


def build_entry(asset: TrustRootAsset, scheme: UidScheme) -> PermissionEntry:
    """對單一資產機械產生目標權限。純函式、無 IO。"""
    owner_class = classify_owner(asset)
    is_dir = infer_is_directory(asset)
    legacy = tuple(w.value for w in asset.writers)

    # 目標 reader 帳號（去掉 ANY_SAME_UID／未映射者）。
    reader_accounts = frozenset(
        a for a in (scheme.resolve(r) for r in asset.readers) if a is not None
    )

    open_points: list[str] = []
    acls: list[AclEntry] = []
    runtime_managed = False

    if owner_class is OwnerClass.DEPLOYMENT:
        owner = scheme.deploy_account
        # enforcement plane：owner（root）可寫，全部行程唯讀（spec §R3「全部行程唯讀」）。
        mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0o5 if is_dir else 0o4, 0o5 if is_dir else 0o4)
        writer_accounts = frozenset({owner})
        rationale = (
            "部署身分（root）擁有——enforcement plane（env／hooks），或 durable-state "
            "樹根（解析鏈即信任根，spec §1）：root 擁有使 headless／svc 皆無法 relink "
            "整棵樹；對全部 headless 唯讀，現況裸寫／group-writable 於此收斂。"
            "0816 裁決已定案路徑：部署樹＝/opt/cortex、bootstrap env 落 /opt/cortex/etc/、"
            "codex hooks 落 job 帳號 HOME 下的 root-owned .codex/（值見 PathLayout，勿手寫）。"
        )

    elif owner_class is OwnerClass.MANAGER_STATE:
        owner = scheme.durable_state_owner
        writer_accounts = {owner}
        # 基準 owner-only（dir 0700／file 0600）；跨帳號讀取一律走精確 ACL（唯讀）。
        mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
        for racct in sorted(reader_accounts):
            if racct == owner:
                continue
            acls.append(AclEntry(racct, "rX" if is_dir else "r"))
        rationale = (
            "Manager-owned durable state：owner＝durable_state_owner，headless 零寫入；"
            "跨帳號讀取以 per-account 唯讀 ACL 精確授予（不開 group/other 寫入位）。"
        )
        # control file queue 現況未認證、任何同 UID 可寫——目標改由 Manager 持有、
        # 提交改走 R7 authenticated socket（spec §R4）。
        if asset.ingress_kind is IngressKind.CONTROL_FILE_QUEUE:
            rationale += " 提交通道 Phase 2 改為 Manager-owned socket（R7），queue 目錄不再世界可寫。"
            open_points.append(
                "control queue：確認 operator 提交改走 authenticated socket 後，"
                "requests/ 目錄可收斂為 owner-only（本表已如此產生）。"
            )
        writer_accounts = frozenset(writer_accounts)

    else:  # JOB
        job_writers = frozenset(
            a
            for a in (
                scheme.resolve(w)
                for w in asset.writers
                if w in HEADLESS_PERSONAS or w is Principal.HEADLESS_HOOK
            )
            if a is not None
        )
        trusted_owner = scheme.durable_state_owner

        if asset.ingress_kind is IngressKind.INTERPROCESS:
            # spool：trusted consumer 擁有並讀＋unlink，untrusted producer 只准 append。
            # Phase 2a 的 review verdict 通道（`review-verdict-spool`）走同一條政策：
            # 容器 owner 是 Manager（durable_state_owner）、mode 0700，reviewer 只拿
            # **write-only** ACL（`wx`，無 `r`——寫得進自己那格、讀不到他人 verdict），
            # builder 不在 writer 面故完全拿不到權限。這正是 spec 10-6「headless 可寫、
            # 不可讀不可改他人」的 per-job 單向語意。
            owner = trusted_owner
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
            # owner 本身不需要 ACL（同帳號時 setfacl 只會是噪音；例如二分方案下
            # reviewer 與 Manager 併帳，此時 owner 位已涵蓋寫入權）。
            for pacct in sorted(a for a in job_writers if a != owner):
                acls.append(AclEntry(pacct, "wx" if is_dir else "w"))
            writer_accounts = frozenset({owner} | job_writers)
            rationale = (
                "job-visible spool：trusted consumer 擁有（讀＋消費），untrusted "
                "producer 僅以 ACL 授予 write（append），不得讀他人。"
            )
        elif len(job_writers) > 1:
            # 多 job persona 共享容器（worktree pool）：不得做成共寫目錄（會破 R2）。
            # 容器由 Manager 擁有，per-job 子目錄在 spawn 時逐案 chown 給該 job 帳號。
            owner = trusted_owner
            runtime_managed = True
            # 目錄容器：0701——others 僅 traverse 進自己被 chown 的子目錄，不可列目錄。
            # 檔案（如 per-job handoff manifest）：0600，owner-only；per-job owner 由
            # 降權啟動器在 spawn 時逐案 chown，容器層不預先開放。
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0o1 if is_dir else 0)
            writer_accounts = frozenset({owner})  # 容器層僅 Manager 建子目錄
            rationale = (
                "job-visible 多 persona 容器：Manager 擁有容器，per-job worktree 於 "
                "spawn 時由降權啟動器 chown 給該 job 帳號——R2 在**子目錄粒度**強制"
                "（reviewer 與 builder 互不可寫）。容器層零 group/other 寫入。"
            )
            open_points.append(
                f"{asset.asset_id}：per-job 子目錄 chown 由 Phase 2 降權啟動器負責；"
                "本表只定容器層權限。"
            )
        else:
            # 單一 job writer：owner＝該 job 帳號，Manager 以唯讀 ACL 讀取產出（D2 走 git）。
            owner = next(iter(job_writers), trusted_owner)
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
            for racct in sorted(reader_accounts):
                if racct == owner:
                    continue
                acls.append(AclEntry(racct, "rX" if is_dir else "r"))
            writer_accounts = frozenset({owner})
            rationale = (
                "job-visible 單一 writer：owner＝對應 job 帳號可寫；trusted reader "
                "（Manager）以唯讀 ACL 讀取（交換面沿用 D2 git 讀）。"
            )

    # 安全網：group/other 一律不得帶 write 位（spec §R2「group 寫入權 MUST 移除」）。
    group_bits = _mask_write((mode >> 3) & 0o7)
    other_bits = _mask_write(mode & 0o7)
    mode = (mode & 0o700) | (group_bits << 3) | other_bits

    return PermissionEntry(
        asset_id=asset.asset_id,
        tier=asset.tier.name,
        tree=asset.tree.value,
        owner_class=owner_class,
        owner=owner,
        group=scheme.group_of(owner),
        mode=mode,
        is_directory=is_dir,
        writer_accounts=writer_accounts,
        reader_accounts=reader_accounts,
        acls=tuple(acls),
        runtime_managed=runtime_managed,
        legacy_writers=legacy,
        rationale=rationale,
        open_points=tuple(open_points),
    )


# ---------------------------------------------------------------------------
# 產生器入口
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PermissionPlan:
    """完整權限計畫（登記表全項）。"""

    scheme_id: str
    entries: tuple[PermissionEntry, ...]

    def by_id(self, asset_id: str) -> PermissionEntry:
        for e in self.entries:
            if e.asset_id == asset_id:
                return e
        raise KeyError(asset_id)

    def all_writable_accounts(self, entry: PermissionEntry) -> frozenset[str]:
        """entry 上所有實際可寫的帳號（owner＋ACL 授寫者）。"""
        accts = set(entry.writer_accounts)
        for acl in entry.acls:
            if acl.writable:
                accts.add(acl.account)
        return frozenset(accts)

    def to_dict(self) -> dict[str, object]:
        return {
            "scheme_id": self.scheme_id,
            "asset_count": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
        }


def generate_plan(
    scheme: UidScheme,
    assets: tuple[TrustRootAsset, ...] = registry.ASSET_REGISTRY,
) -> PermissionPlan:
    """對登記表每一項機械產生權限，回傳完整計畫（涵蓋無遺漏）。"""
    entries = tuple(build_entry(a, scheme) for a in assets)
    return PermissionPlan(scheme_id=scheme.scheme_id, entries=entries)


def _placeholder_path(entry: PermissionEntry) -> str:
    """未提供真實路徑時的清楚標記 placeholder。"""
    return f"<PATH:{entry.asset_id}>"


#: per-job 路徑的標記 segment。帶此 segment 的資產由降權啟動器在 spawn 時逐案套用，
#: **不**在 setup 階段執行——命令因此以註解形式輸出（可讀、不可誤執行）。
PER_JOB_SEGMENT = "<job-id>"


def plan_to_commands(
    plan: PermissionPlan,
    path_of: Mapping[str, str] | None = None,
) -> list[str]:
    """把計畫轉成 runbook 可引用的命令序列（**只產生字串，絕不執行**）。

    `path_of`：asset_id→真實路徑字串；未提供者以 placeholder 呈現，供 runbook 以
    shell 變數替換（`PathLayout.asset_paths()` 可一次提供全部真實路徑）。輸出含
    分節註解，方便 operator 對照登記表逐項核可；目錄資產會先出 `install -d`，
    使整份輸出成為一份可直接執行的 setup script。
    """
    lines: list[str] = [
        f"# trust-root Phase 2b 權限套用命令（scheme={plan.scheme_id}）",
        "# 由 permgen 機械產生；operator 逐項 review 後手動 sudo 執行。",
        "# 帶 --paths 時路徑為 PathLayout 的真實絕對路徑；否則以 <PATH:asset_id> 呈現。",
        f"# 含 {PER_JOB_SEGMENT} 的資產屬 per-job（降權啟動器逐案套用），已註解不執行。",
    ]
    for e in plan.entries:
        path = (path_of or {}).get(e.asset_id) or _placeholder_path(e)
        per_job = PER_JOB_SEGMENT in path
        lines.append("")
        lines.append(f"# [{e.tier}] {e.asset_id} ({e.owner_class.value}) — {e.rationale}")
        if e.runtime_managed:
            lines.append("#   注意：per-child owner 由降權啟動器逐案 chown（本節僅容器層）。")
        for op in e.open_points:
            lines.append(f"#   後續依賴：{op}")
        if per_job:
            lines.append("#   per-job：由降權啟動器在 spawn 時套用，setup 階段不執行。")
        cmds = list(e.commands(path))
        if e.is_directory:
            # 目錄一定先建起來，後續 chown／chmod／setfacl 必然有對象。
            cmds.insert(0, f"install -d {path}")
        else:
            # 葉檔在 setup 當下多半尚未存在（由服務首次寫入時建立）。加 `[ ! -e ] ||`
            # 守衛：不存在就跳過（且在 `sh -e` 下不會中斷腳本），存在就套上目標權限。
            # 尚未存在也安全——容器目錄已是 owner-only，且 unit 的 UMask=0077 讓新檔
            # 出生即 0600。
            lines.append(
                f"#   葉檔守衛：{path} 尚未建立時跳過（服務以 UMask=0077 建立即符合目標）。"
            )
            cmds = [f"[ ! -e {path} ] || {cmd}" for cmd in cmds]
        for cmd in cmds:
            lines.append(f"#   {cmd}" if per_job else cmd)
    return lines


# ---------------------------------------------------------------------------
# Phase 2b：部署 layout（把登記表的抽象資產綁到目標主機的真實絕對路徑）
#
# operator 0816 第二輪裁決：durable state 落 `/var/lib/cortex`（worktree pool＝
# `/var/lib/cortex/worktree`）、Manager 部署落 `/opt/cortex`。本 layout 是那份裁決
# 的機器可讀形式——runbook 不再手寫路徑，全部從這裡取。
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtraWritePath:
    """非登記表資產、但服務身分確實需要寫的路徑（每條必須附理由）。

    這是「無多餘」等式的唯一合法例外通道：ReadWritePaths 由登記表機械導出，
    任何額外條目都必須在此明示宣告並說明理由，測試會強制理由非空。
    """

    path: str
    reason: str


@dataclass(frozen=True)
class PathLayout:
    """目標主機的絕對路徑 layout（0816 裁決值為預設）。"""

    agents_root: str = "/var/lib/cortex"
    worktree_root: str = "/var/lib/cortex/worktree"
    deploy_root: str = "/opt/cortex"
    instance: str = "cortex"
    #: 服務／job 帳號 HOME 的父目錄。**每個帳號的 HOME 由帳號名機械導出**
    #: （`home_of()`），不再是寫死的字面量——寫死會在換 scheme 時漂移：三分的
    #: Manager 帳號是 `cortex-manager`，HOME 卻還指著二分時代的 `/var/lib/cortex-svc`，
    #: unit 的 `Environment=HOME=` 與 scaffold 因此指向一個沒人擁有的目錄。
    home_root: str = "/var/lib"
    #: builder 的帳號名。只給 `asset_paths()` 用（`codex-hooks` 掛在 builder HOME 下），
    #: 因為 `asset_paths()` 刻意不吃 scheme——兩個 scheme 對 BUILDER 的映射相同。
    #: 其餘所有帳號相關路徑一律由 scheme 現場導出。
    builder_account: str = "cortex-builder"
    #: per-job 路徑的 segment；system unit 模板用 `%i`（systemd instance 名）。
    job_segment: str = PER_JOB_SEGMENT

    # -- 衍生根 -------------------------------------------------------------
    @property
    def control_root(self) -> str:
        return f"{self.agents_root}/control"

    @property
    def coordinator_root(self) -> str:
        return f"{self.agents_root}/coordinator"

    @property
    def specs_root(self) -> str:
        return f"{self.agents_root}/specs"

    @property
    def monitor_state_root(self) -> str:
        return f"{self.agents_root}/monitor"

    @property
    def project_config_root(self) -> str:
        return f"{self.agents_root}/config/paulsha"

    @property
    def skill_registry_root(self) -> str:
        return f"{self.agents_root}/registry"

    @property
    def run_root(self) -> str:
        return f"{self.agents_root}/run/{self.instance}"

    @property
    def dispatch_log_root(self) -> str:
        """Manager 的 job log_dir（`autonomy.py` 以相對 `runtime/dispatch/<slice>`
        推導，故由 unit 的 `WorkingDirectory` 決定落點）。gate ledger 住在這裡。"""
        return f"{self.agents_root}/runtime/dispatch"

    @property
    def job_spec_spool_root(self) -> str:
        """Manager 寫、job 只讀的 per-job 執行規格（`<unit-instance-id>.json`）。

        路徑與 `config.paths.job_spec_spool_root()` 是**成對契約**（登記表資產
        `job-spec-spool`），由 `asset_paths()` 而非本 property 供給權限計畫；本
        property 只是給 unit／shim 產生器引用的同一份字面量。
        """
        return f"{self.coordinator_root}/job-specs"

    @property
    def bin_root(self) -> str:
        """部署樹的可執行檔目錄（root-owned）——降權 shim 住這裡。"""
        return f"{self.deploy_root}/bin"

    @property
    def job_shim(self) -> str:
        """降權 job 模板 unit 的固定 `ExecStart=`（root-owned，內容由 permgen 產）。"""
        return f"{self.bin_root}/cortex-job-shim"

    @property
    def venv_root(self) -> str:
        return f"{self.deploy_root}/venv"

    @property
    def exec_start(self) -> str:
        return f"{self.venv_root}/bin/cortex service run"

    @property
    def env_file(self) -> str:
        return f"{self.deploy_root}/etc/{self.instance}-manager.env"

    # -- 帳號→HOME／cache（由帳號名機械導出，換 scheme 不會漂移）-------------
    def home_of(self, account: str) -> str:
        """該帳號的 HOME。HOME 本身 root-owned（見 `scaffold_directories`）。"""
        return f"{self.home_root}/{account}"

    def cache_of(self, account: str) -> str:
        """該帳號 HOME 底下唯一可寫的那一層（git／gh／uv 的 cache）。"""
        return f"{self.home_of(account)}/cache"

    def codex_hooks_dir_of(self, account: str) -> str:
        """該帳號的 `~/.codex`。root-owned——job 不得替換自己的 hooks。"""
        return f"{self.home_of(account)}/.codex"

    @property
    def builder_home(self) -> str:
        return self.home_of(self.builder_account)

    @property
    def builder_cache(self) -> str:
        return self.cache_of(self.builder_account)

    def with_job_segment(self, segment: str) -> "PathLayout":
        """換掉 per-job segment（system unit 模板用 `%i`）。"""
        return PathLayout(
            agents_root=self.agents_root,
            worktree_root=self.worktree_root,
            deploy_root=self.deploy_root,
            instance=self.instance,
            home_root=self.home_root,
            builder_account=self.builder_account,
            job_segment=segment,
        )

    # -- 資產→路徑 ----------------------------------------------------------
    def asset_paths(self) -> dict[str, str]:
        """登記表每一項 asset_id → 目標主機絕對路徑（涵蓋全部、無多餘）。"""
        a = self.agents_root
        c = self.coordinator_root
        ctl = self.control_root
        mon = self.monitor_state_root
        reg = self.skill_registry_root
        wt = self.worktree_root
        job = f"{wt}/{self.job_segment}"
        return {
            "runtime-agents-tree": a,
            "control-root-tree": ctl,
            "coordinator-root-tree": c,
            "dispatch-specs-tree": self.specs_root,
            "runtime-run-tree": self.run_root,
            "project-config-tree": self.project_config_root,
            "coverage-shadow-telemetry": f"{c}/coverage-shadow",
            "monitor-state-tree": mon,
            "monitor-work-items-snapshot": f"{mon}/work-items.snapshot.json",
            "monitor-github-sync-cursor": f"{mon}/github-issue-sync.json",
            "monitor-event-spool": f"{mon}/event-spool",
            "skill-registry-tree": reg,
            "skill-usage-ledger": f"{reg}/skill_usage.jsonl",
            "skill-park-state": f"{reg}/skill_park.json",
            "skill-park-proposals": f"{reg}/skill_park_proposals",
            "control-request-queue": f"{ctl}/requests",
            "control-done-queue": f"{ctl}/done",
            "control-status": f"{ctl}/status.json",
            "control-daemon-lock": f"{ctl}/manager.lock",
            "repo-worktree": job,
            "dispatch-worktree-pool": wt,
            "jobs-registry": f"{c}/jobs.json",
            "review-verdict": f"{job}/.psc-review-verdict.json",
            # Phase 2a 受控通道（PR #599）：<coordinator>/review-verdicts/<reviewer_job_id>/
            "review-verdict-spool": f"{c}/review-verdicts",
            # Phase 2b 方案 B（0816 第三輪 A+B）：模板 unit 的 per-job 執行規格。
            "job-spec-spool": self.job_spec_spool_root,
            "verification-evidence": f"{c}/evidence/verification",
            "maintainer-attestation": f"{c}/evidence/maintainer-review",
            "completion-record": f"{c}/evidence/completion",
            "full-suite-evidence": f"{c}/evidence/full-suite",
            "workflow-inputs": f"{c}/evidence/workflow-inputs",
            "workflow-evidence": f"{c}/evidence/workflow",
            "gate-ledger": self.dispatch_log_root,
            "delivery-journal": f"{c}/delivery-journal.json",
            "provider-backoff": f"{c}/provider-rate-limit-backoff.json",
            "workflow-report-journal": f"{c}/workflow-report-transactions",
            "digest-outbox": f"{c}/digest/outbox",
            "engineering-outcome-outbox": f"{c}/engineering-outcomes",
            "model-identity-overlay": f"{self.project_config_root}/model-identities.yaml",
            "combo-card-override": f"{a}/config/combos",
            "handoff-manifest": f"{job}/.psc-handoff.json",
            "runtime-bootstrap-env": self.env_file,
            "codex-hooks": f"{self.builder_home}/.codex/hooks.json",
            "work-items-yaml": f"{job}/.cortex/work-items.yaml",
        }

    # -- 非資產骨架目錄 -----------------------------------------------------
    def scaffold_directories(self, scheme: UidScheme) -> tuple[tuple[str, str, str, int], ...]:
        """`(path, owner, group, mode)`：不屬任何登記表資產、但必須先存在的父目錄。

        原則：**凡是保護資產的父目錄，一律 root 擁有**——父目錄可寫者能 unlink／
        rename 子物件，因此把 root-owned 檔放進 svc-owned 目錄等於沒保護。
        """
        svc = scheme.durable_state_owner
        root = scheme.deploy_account
        g = scheme.group_of
        # 每個 scheme 解析得到的帳號都要有 HOME／cache——**由 scheme 導出，不是列舉**。
        # 二分下這是 {cortex-svc, cortex-builder}（與改動前逐字相同）；三分下自動多出
        # `cortex-reviewer-planner`，不必在這裡補一行（補一行正是上一版漏掉它的原因）。
        service_accounts = [svc] + sorted(scheme.headless_accounts() - {svc})
        # 跑模型的 job 帳號還要一個 root-owned 的 ~/.codex（hooks 不得被 job 替換）。
        job_accounts = sorted(scheme.headless_accounts() - {svc})
        account_dirs: list[tuple[str, str, str, int]] = []
        for account in service_accounts:
            account_dirs.append((self.home_of(account), root, g(root), 0o755))
            if account in job_accounts:
                account_dirs.append(
                    (self.codex_hooks_dir_of(account), root, g(root), 0o755)
                )
            account_dirs.append((self.cache_of(account), account, g(account), 0o700))
        return (
            # 部署樹（enforcement plane）：全 root，對 svc／builder 唯讀。
            (self.deploy_root, root, g(root), 0o755),
            (f"{self.deploy_root}/etc", root, g(root), 0o755),
            # 降權 shim 的家：root-owned、對 svc／job 唯讀。模板 unit 的 ExecStart=
            # 指向這裡，因此持 spawn 授權的帳號也改不了 job 實際執行的第一支程式。
            (self.bin_root, root, g(root), 0o755),
            (self.venv_root, root, g(root), 0o755),
            # durable state 樹的 root-owned 骨架（svc 不得 relink 這幾層）。
            (f"{self.agents_root}/config", root, g(root), 0o755),
            (f"{self.agents_root}/run", root, g(root), 0o755),
            (f"{self.agents_root}/runtime", root, g(root), 0o755),
            # svc 自己建得出來、但先建好可讓權限一次到位的中間層。
            (f"{self.coordinator_root}/evidence", svc, g(svc), 0o700),
            (f"{self.coordinator_root}/digest", svc, g(svc), 0o700),
            # job spec spool 不在此列：它已是登記表資產（`job-spec-spool`），權限由
            # `plan_to_commands()` 依登記表機械產出（owner-only ＋ job 帳號唯讀 ACL），
            # 在骨架再寫一次會變成第二份真相。
            # 服務／job 帳號 HOME：root 擁有（job 不得替換自己的 ~/.codex），只開
            # cache 子目錄。清單由 scheme 導出，見上方 `account_dirs`。
            *account_dirs,
        )

    # -- 額外可寫路徑（非登記表資產，須附理由）------------------------------
    def manager_extra_write_paths(self, account: str) -> tuple[ExtraWritePath, ...]:
        # 註：job spec spool 曾經是這裡的一條 extra（`<agents_root>/jobs/<id>/run.sh`）。
        # 0816 第三輪 A+B 把它升格為登記表資產 `job-spec-spool`，因此改由
        # `required_write_targets()` 機械導出——例外通道少一條，等式多涵蓋一項。
        return (
            ExtraWritePath(
                self.cache_of(account),
                f"服務帳號 {account} 的 HOME 快取（git/gh/uv）；HOME 本身 root-owned，只開這一層。",
            ),
        )

    def job_extra_write_paths(self, account: str) -> tuple[ExtraWritePath, ...]:
        # 帳號由呼叫端（`build_job_unit` 的 principal）給：M2 要為 reviewer/planner
        # 開第二個模板 unit 時，這裡不必改一行——換 principal 即換帳號。
        return (
            ExtraWritePath(
                self.cache_of(account),
                f"job 帳號 {account} 的 HOME 快取（git/gh/uv）；HOME 與 ~/.codex 皆 root-owned 不可替換。",
            ),
        )


DEFAULT_LAYOUT = PathLayout()


def asset_paths(layout: PathLayout = DEFAULT_LAYOUT) -> dict[str, str]:
    """模組層便利函式（CLI 與 runbook 引用）。"""
    return layout.asset_paths()


# ---------------------------------------------------------------------------
# ReadWritePaths 的機械導出
# ---------------------------------------------------------------------------

def _parent_dir(path: str) -> str:
    head = path.rsplit("/", 1)[0]
    return head or "/"


def _is_within(child: str, parent: str) -> bool:
    """`child` 是否落在 `parent` 之內（含相等）——純字串前綴判定，無 IO。"""
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _minimize(paths: set[str]) -> tuple[str, ...]:
    """去掉被其他條目涵蓋的子路徑，回傳排序後的最小覆蓋集合。"""
    kept = [
        p for p in paths
        if not any(other != p and _is_within(p, other) for other in paths)
    ]
    return tuple(sorted(set(kept)))


def required_write_targets(
    plan: PermissionPlan,
    layout: PathLayout,
    account: str,
) -> dict[str, str]:
    """`asset_id → 該帳號必須可寫的目標路徑`（檔案取其父目錄）。

    ProtectSystem=strict 下整個檔案系統唯讀；要**建立／取代**一個檔，必須對其
    父目錄可寫，故檔案資產一律折算成父目錄。這就是「ReadWritePaths 由登記表機械
    導出」的全部規則——沒有第二條。
    """
    targets: dict[str, str] = {}
    paths = layout.asset_paths()
    for entry in plan.entries:
        if account not in plan.all_writable_accounts(entry):
            continue
        path = paths[entry.asset_id]
        targets[entry.asset_id] = path if entry.is_directory else _parent_dir(path)
    return targets


def read_write_paths(
    plan: PermissionPlan,
    layout: PathLayout,
    account: str,
    extras: tuple[ExtraWritePath, ...] = (),
) -> tuple[str, ...]:
    """該帳號 unit 的 `ReadWritePaths=` 最小覆蓋集合（登記表導出 ∪ 明示 extras）。"""
    wanted = set(required_write_targets(plan, layout, account).values())
    wanted |= {e.path for e in extras}
    return _minimize(wanted)


def read_write_path_owners(
    plan: PermissionPlan,
    layout: PathLayout,
    account: str,
    extras: tuple[ExtraWritePath, ...] = (),
) -> dict[str, tuple[str, ...]]:
    """每條 ReadWritePaths → 它涵蓋的 asset_id（或 `extra:<reason>`），供逐條註解。"""
    targets = required_write_targets(plan, layout, account)
    result: dict[str, tuple[str, ...]] = {}
    for rwp in read_write_paths(plan, layout, account, extras):
        covered = sorted(aid for aid, t in targets.items() if _is_within(t, rwp))
        covered += [f"extra:{e.reason}" for e in extras if _is_within(e.path, rwp)]
        result[rwp] = tuple(covered)
    return result


# ---------------------------------------------------------------------------
# systemd unit 產生（Manager system unit ＋ 降權 job 模板 unit）
# ---------------------------------------------------------------------------

#: 加固指令 →（值, 為何）。逐項附註解是 spec §R3 的可審查性要求。
_HARDENING: tuple[tuple[str, str, str], ...] = (
    ("NoNewPrivileges", "yes",
     "提權天花板：exec 後不得取得新特權，setuid 二進位／file capabilities 全部失效。"),
    ("CapabilityBoundingSet", "",
     "清空 capability 上界——服務永不具 root 能力（裁決：cortex 任何元件永不具 root）。"),
    ("AmbientCapabilities", "",
     "不夾帶任何 ambient capability；CAP_SETUID 路線已被裁決排除。"),
    ("ProtectSystem", "strict",
     "整個檔案系統唯讀，只有下方 ReadWritePaths 例外——/opt/cortex 部署樹因此唯讀。"),
    ("ProtectHome", "yes",
     "/home、/root、/run/user 一律不可見：state 已全數搬離 HOME，任何殘留的 HOME "
     "路徑必須立刻失敗，而不是靜默沿用舊樹。"),
    ("PrivateTmp", "yes",
     "私有 /tmp、/var/tmp：切斷經共用 tmp 的跨 persona 檔案交換與 symlink 攻擊。"),
    ("PrivateDevices", "yes",
     "只掛最小 /dev；封掉 raw device 與 /dev/mem 這類旁路。"),
    ("ProtectProc", "invisible",
     "看不到其他 UID 的 /proc/<pid>——直接封 R9 族 4 的 environ／mem 讀取。"),
    ("ProcSubset", "pid",
     "/proc 只保留 pid 子集，隱藏 /proc/kcore 等核心介面。"),
    ("ProtectControlGroups", "yes",
     "cgroup 樹唯讀：不可經 cgroup 改寫資源或逃逸 unit 界線。"),
    ("ProtectKernelModules", "yes", "禁止載入／卸載核心模組。"),
    ("ProtectKernelTunables", "yes", "/proc/sys、/sys 唯讀，禁止改核心參數。"),
    ("ProtectKernelLogs", "yes", "禁讀 kmsg，避免經核心日誌側錄他人資料。"),
    ("ProtectClock", "yes", "禁止改系統時鐘——時間是 evidence 排序不變式的輸入。"),
    ("ProtectHostname", "yes", "禁止改 hostname（稽核紀錄的主機標識）。"),
    ("RestrictSUIDSGID", "yes",
     "禁止建立 setuid/setgid 檔——關掉自製提權助手這條路。"),
    ("RestrictNamespaces", "yes",
     "禁止建立 namespace：user namespace 是 unprivileged 提權的常見起點。"),
    ("RestrictRealtime", "yes", "禁 realtime 排程，避免 DoS 宿主。"),
    ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6",
     "只留 unix socket 與 IP：封掉 AF_NETLINK／AF_PACKET 等旁路。"),
    ("LockPersonality", "yes", "鎖定執行域，禁止切換 personality 規避 seccomp。"),
    ("MemoryDenyWriteExecute", "yes",
     "禁 W+X 記憶體，封 JIT 型 shellcode。※ 若 Python C-extension（ctypes "
     "trampoline）啟動失敗，這是第一嫌疑：先單獨註解本行複測。"),
    ("SystemCallArchitectures", "native",
     "只允許原生 ABI，封掉經 32-bit compat 介面規避 seccomp。"),
    ("SystemCallFilter", "@system-service",
     "seccomp 白名單：只留一般服務所需 syscall。"),
    ("SystemCallErrorNumber", "EPERM",
     "被過濾的 syscall 回 EPERM 而非 SIGSYS——失敗可觀測，不是無聲當掉。"),
    ("RemoveIPC", "yes", "服務結束即清掉該 UID 的 IPC 物件，不留跨 job 殘留。"),
    ("KeyringMode", "private", "私有 kernel keyring：不共用、不繼承金鑰。"),
    ("UMask", "0077",
     "新建檔預設 0600／目錄 0700，與權限產生器的 owner-only 基準一致。"),
)


def job_unit_stem(
    layout: "PathLayout" = None,  # type: ignore[assignment]
    principal: Principal = Principal.BUILDER,
) -> str:
    """降權 job 模板 unit 的字幹（不含 `@.service`）。

    **M2（#615，reviewer/planner 啟動面降權）的擴充點就是這裡。** 現階段只有
    builder 走模板，字幹是 `cortex-job`（與 `coordinator/job_runner`
    的 `TEMPLATE_UNIT_PREFIX` ＋ polkit pattern 成對契約）。要開第二個模板實例時
    只需傳入另一個 `principal`：unit 名、`User=`、`Environment=HOME=`／
    `XDG_CACHE_HOME=`、`ReadWritePaths=` 全部跟著 scheme 導出，`build_job_unit()`／
    `build_polkit_rule()`／`build_job_shim()` 三支產生器**一行都不必改**。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    if principal is Principal.BUILDER:
        return f"{layout.instance}-job"
    return f"{layout.instance}-{principal.value}-job"


@dataclass(frozen=True)
class SystemdUnit:
    """產生出來的 unit：**只有內容字串與結構化欄位，沒有任何寫檔／執行**。"""

    unit_name: str
    install_path: str
    account: str
    exec_start: str
    environment_file: str | None
    read_write_paths: tuple[str, ...]
    content: str

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_name": self.unit_name,
            "install_path": self.install_path,
            "account": self.account,
            "exec_start": self.exec_start,
            "environment_file": self.environment_file,
            "read_write_paths": list(self.read_write_paths),
            "content": self.content,
        }


def _hardening_lines(overrides: Mapping[str, str] | None = None) -> list[str]:
    lines: list[str] = []
    over = dict(overrides or {})
    for key, value, why in _HARDENING:
        effective = over.get(key, value)
        lines.append(f"# {why}")
        lines.append(f"{key}={effective}")
    return lines


def _rwp_lines(owners: Mapping[str, tuple[str, ...]]) -> list[str]:
    lines = [
        "# --- ReadWritePaths：由 R1 登記表機械導出（permgen），勿手擴 ---",
        "# 每條後面列出它涵蓋的登記表資產；新增 durable state 時改登記表、重跑產生器。",
    ]
    for path, covered in owners.items():
        lines.append(f"#   涵蓋：{', '.join(covered) if covered else '（無）'}")
        lines.append(f"ReadWritePaths={path}")
    return lines


def build_manager_unit(
    scheme: UidScheme,
    layout: PathLayout = DEFAULT_LAYOUT,
    plan: PermissionPlan | None = None,
) -> SystemdUnit:
    """Manager 的 system-level unit（`User=<durable_state_owner>`）。"""
    plan = plan or generate_plan(scheme)
    account = scheme.durable_state_owner
    group = scheme.group_of(account)
    extras = layout.manager_extra_write_paths(account)
    owners = read_write_path_owners(plan, layout, account, extras)
    unit_name = f"{layout.instance}-manager.service"

    body = [
        f"# {'/etc/systemd/system/' + unit_name}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；改登記表後重跑：",
        f"#   python3 -m paulsha_cortex.trust_root unit {scheme.scheme_id} --manager",
        "",
        "[Unit]",
        "Description=cortex Manager (trust-root Phase 2b, system-level)",
        "Documentation=file://docs/superpowers/runbooks/trust-root-phase2b-setup.md",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "# 受信任服務身分。Manager 永不以 root 執行——root 操作只由 operator 手動 sudo。",
        f"User={account}",
        f"Group={group}",
        "",
        "# 部署樹在 root-owned 樹內，對本服務唯讀（ProtectSystem=strict 再加一層）：",
        "# 改寫 verifier／注入 sitecustomize.py／.pth 皆 EACCES（spec §R3）。",
        f"ExecStart={layout.exec_start}",
        "# 相對 log_dir（runtime/dispatch/<slice>）由此解析，必須落在 ReadWritePaths 內。",
        f"WorkingDirectory={layout.agents_root}",
        "",
        "# EnvironmentFile 無 '-' 前綴＝fail-closed：檔案缺席即拒絕啟動，",
        "# MUST NOT 靜默落回 $HOME/.agents 預設（spec §R3 Scenario「刪除 EnvironmentFile」）。",
        f"EnvironmentFile={layout.env_file}",
        "# HOME 由 unit 指定；HOME 本身 root-owned，只有 cache 子目錄可寫。",
        "# 路徑由帳號名導出（`layout.home_of`）——換 scheme 時不會停在舊帳號的樹上。",
        f"Environment=HOME={layout.home_of(account)}",
        f"Environment=XDG_CACHE_HOME={layout.cache_of(account)}",
        "",
        "# --- 加固（spec §R3；逐項附理由供審查）---",
    ]
    body += _hardening_lines()
    body += [""]
    body += _rwp_lines(owners)
    body += [
        "",
        "Restart=on-failure",
        "RestartSec=5s",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return SystemdUnit(
        unit_name=unit_name,
        install_path=f"/etc/systemd/system/{unit_name}",
        account=account,
        exec_start=layout.exec_start,
        environment_file=layout.env_file,
        read_write_paths=tuple(owners.keys()),
        content="\n".join(body) + "\n",
    )


def build_job_unit(
    scheme: UidScheme,
    layout: PathLayout = DEFAULT_LAYOUT,
    principal: Principal = Principal.BUILDER,
    plan: PermissionPlan | None = None,
) -> SystemdUnit:
    """降權 job 的**模板** unit（`cortex-job@.service`）。

    這是降權/提權分界線的另一半：`User=` 在 root-owned 的 unit 檔裡**硬寫死**，
    呼叫端（Manager）只能給 instance 名，**無法選擇 UID、無法夾帶任何屬性**。
    polkit 規則只放行這個模板的實例（見 `build_polkit_rule`）。

    `ExecStart=` 同樣固定：永遠是 root-owned 的 shim（`build_job_shim()` 產出），
    per-job 的命令／worktree／env／log 路徑改由 Manager-owned 的 spec spool
    （登記表資產 `job-spec-spool`）傳遞，shim 讀完才 exec。

    **為什麼不用 `StandardOutput=append:<log>`**：`file:`／`append:` 的目標檔是由
    **PID 1（root）** 在降權之前開啟的；路徑裡只要有任何一段由 Manager 帳號掌控
    （spec spool 或 log 目錄都是），Manager 就能在該位置放一個 symlink 讓 root 對
    任意檔案 append——那是把「Manager 不具 root」這條裁決整個賣掉。模板是**單一
    靜態檔**、per-job 的 log 路徑又必須維持 harvest 既有的
    `<log_dir>/<slice>.jsonl`（`%i` 推不出來），兩者無法同時成立。因此 log 導引改由
    **shim 在已降權之後**依 spec 的 `log_path` 自行接管（見 `coordinator/job_shim.py`），
    unit 這層只留 journal 給 shim 讀 spec 失敗時的診斷。
    """
    plan = plan or generate_plan(scheme)
    account = scheme.resolve(principal)
    if account is None:
        raise ValueError(f"principal 未映射到帳號: {principal}")
    group = scheme.group_of(account)
    # per-job 路徑在模板 unit 中以 systemd 的 %i 表示。
    job_layout = layout.with_job_segment("%i")
    extras = job_layout.job_extra_write_paths(account)
    owners = read_write_path_owners(plan, job_layout, account, extras)
    unit_name = f"{job_unit_stem(layout, principal)}@.service"

    body = [
        f"# {'/etc/systemd/system/' + unit_name}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root unit {scheme.scheme_id} --job",
        "#",
        "# 降權/提權分界線：User= 在本 root-owned 檔內硬寫死。Manager（cortex-svc）",
        "# 只能 `systemctl start cortex-job@<id>.service`，**不能**選 UID、不能傳屬性。",
        "",
        "[Unit]",
        "Description=cortex headless job %i (downgraded, trust-root Phase 2b)",
        f"After={layout.instance}-manager.service",
        "",
        "[Service]",
        "Type=exec",
        "# 硬寫死的 job 身分——這行是整套降權的唯一 UID 來源。",
        f"User={account}",
        f"Group={group}",
        "",
        "# ExecStart 也是固定的：永遠是 root-owned 的 shim，呼叫端連命令列都給不了。",
        "# per-job 執行規格由 Manager 原子寫入 spec spool（Manager-owned，job 帳號唯讀）：",
        f"#   {job_layout.job_spec_spool_root}/%i.json",
        "# job 因此無法改寫自己的命令列，也無法為下一個 job 埋伏。",
        f"ExecStart={job_layout.job_shim} %i",
        "# 工作目錄：shim 會依 spec 的 working_directory 再 chdir 到該 job 的 worktree；",
        "# 這裡只給恆存在的 pool 根（0701＝可 traverse、不可列目錄），避免 unit 因",
        "# per-job 目錄尚未建立而在 exec 前就失敗（那會讓 log 裡沒有任何線索）。",
        f"WorkingDirectory={job_layout.worktree_root}",
        "# shim 讀 spec 的唯一合法來源：這一行在 root-owned 的 unit 檔裡，",
        "# 因此持 spawn 授權的帳號也改不掉 spec 要從哪個目錄讀。shim 對未設此",
        "# 變數的情況 fail-closed（不猜、不落回 $HOME 推導的預設）。",
        f"Environment=PSC_JOB_SPEC_SPOOL={job_layout.job_spec_spool_root}",
        "# job 永不取得 gh token：GitHub 寫入由 Manager 代理（D1 outbox）。",
        "Environment=GH_TOKEN=",
        "Environment=GITHUB_TOKEN=",
        f"Environment=HOME={job_layout.home_of(account)}",
        f"Environment=XDG_CACHE_HOME={job_layout.cache_of(account)}",
        "",
        "# --- 加固（與 Manager 同一套；job 這側只多不少）---",
    ]
    body += _hardening_lines()
    body += [""]
    body += _rwp_lines(owners)
    body += [
        "",
        "# job 為一次性：結束即回收 unit 狀態，不留可被重用的殘骸。",
        "CollectMode=inactive-or-failed",
        "Restart=no",
        "# 刻意**不**用 StandardOutput=append:<log>——那個檔由 PID 1（root）在降權前開啟，",
        "# 路徑中只要有一段由 Manager 帳號掌控就成了 root-follows-symlink 的提權面。",
        "# job 的 JSONL log 由 shim 在**已降權之後**依 spec 的 log_path 自行接管；",
        "# 這裡的 journal 只承接 shim 讀 spec 失敗（尚未接管前）的診斷輸出。",
        "StandardOutput=journal",
        "StandardError=journal",
    ]
    return SystemdUnit(
        unit_name=unit_name,
        install_path=f"/etc/systemd/system/{unit_name}",
        account=account,
        exec_start=f"{job_layout.job_shim} %i",
        environment_file=None,
        read_write_paths=tuple(owners.keys()),
        content="\n".join(body) + "\n",
    )


# ---------------------------------------------------------------------------
# 降權 shim（root-owned，模板 unit 的固定 ExecStart）
# ---------------------------------------------------------------------------

#: shim 真正的實作模組。**刻意不是 heredoc 產出的一大段程式碼**：shim 要做的事
#: （驗 instance 名／驗 spec 檔不是 symlink／驗 schema／接管 log／chdir／execve）
#: 每一條都是可測的邏輯，塞進字串就只剩「字串比對」這種驗收方式。把邏輯放進
#: repo 內的模組，它跟其他程式碼一樣被單元測試、被 lint、被 review；permgen 只
#: 產出那支 3 行的 root-owned 啟動 stub。兩者都落在 root-owned 的部署樹裡，
#: 「job 改不了自己執行的第一支程式」這條性質完全不變。
JOB_SHIM_MODULE = "paulsha_cortex.coordinator.job_shim"


@dataclass(frozen=True)
class ShimScript:
    """產生出來的 shim stub：**只有內容字串**，本模組不寫任何系統路徑。"""

    install_path: str
    interpreter: str
    module: str
    mode: int
    owner: str
    group: str
    content: str

    @property
    def mode_str(self) -> str:
        return format(self.mode, "04o")

    def to_dict(self) -> dict[str, object]:
        return {
            "install_path": self.install_path,
            "interpreter": self.interpreter,
            "module": self.module,
            "mode": self.mode_str,
            "owner": self.owner,
            "group": self.group,
            "content": self.content,
        }

    def commands(self) -> list[str]:
        """安裝命令字串（**只回傳字串，不執行**）。"""
        return [
            f"chown {self.owner}:{self.group} {self.install_path}",
            f"chmod {self.mode_str} {self.install_path}",
        ]


def build_job_shim(
    scheme: UidScheme = DEFAULT_SCHEME,
    layout: PathLayout = DEFAULT_LAYOUT,
) -> ShimScript:
    """產生 `<deploy_root>/bin/cortex-job-shim` 的內容（root-owned 啟動 stub）。

    stub 只做一件事：以部署 venv 的 interpreter 執行 :data:`JOB_SHIM_MODULE`，把
    模板 unit 傳進來的 `%i`（instance 名）原封不動交過去。**不解析參數、不組命令、
    不碰 spec 檔**——所有判斷都在那個模組裡，這裡沒有可被注入的表面。

    interpreter 寫成部署 venv 的絕對路徑而不是 `/usr/bin/env python3`：後者會走
    job 帳號的 `PATH`，等於讓 job 決定用哪個 interpreter 執行 root-owned 的 shim。
    """
    account = scheme.deploy_account
    interpreter = f"{layout.venv_root}/bin/python3"
    body = [
        "#!/bin/sh",
        f"# {layout.job_shim}",
        f"# 由 permgen 機械產生（scheme={scheme.scheme_id}）——勿手改；重跑：",
        f"#   python3 -m paulsha_cortex.trust_root shim {scheme.scheme_id}",
        "#",
        "# root-owned、mode 0755：Manager 與 job 帳號皆唯讀。這是模板 unit 固定的",
        f"# ExecStart=，因此持 spawn 授權的帳號也換不掉 job 執行的第一支程式。",
        "#",
        f"# $1 ＝ systemd 模板實例名（%i）。spec 由此推導：",
        f"#   {layout.job_spec_spool_root}/$1.json（Manager 寫、job 唯讀）",
        "set -eu",
        f'exec "{interpreter}" -m {JOB_SHIM_MODULE} "$@"',
    ]
    return ShimScript(
        install_path=layout.job_shim,
        interpreter=interpreter,
        module=JOB_SHIM_MODULE,
        mode=0o755,
        owner=account,
        group=scheme.group_of(account),
        content="\n".join(body) + "\n",
    )


# ---------------------------------------------------------------------------
# polkit 規則產生（授權面嚴格收窄）
# ---------------------------------------------------------------------------

#: 唯一被授權的 systemd polkit action。
POLKIT_ACTION = "org.freedesktop.systemd1.manage-units"
#: 唯一被授權的 verb（起／停 job；reload、mask、set-property 等一律拒）。
POLKIT_ALLOWED_VERBS: tuple[str, ...] = ("start", "stop")


class PolkitPlan(Enum):
    """降權的兩個方案。**0816 第三輪裁決：B 定案**（`TEMPLATE` 為預設）。

    - `TEMPLATE`（B，**定案／預設**）：root-owned 模板 unit（`<instance>-job@.service`）
      把 `User=` 與 `ExecStart=` 都硬寫死，polkit 只放行該模板的實例。**「降到哪個
      帳號」「執行哪支程式」因此都由 OS 強制**，`plan_residual_risk()` 回傳空 tuple。
      Manager 端對應的啟動器模式是 `PSC_JOB_RUNNER=systemd-template`
      （`coordinator/job_runner.py`）。
    - `TRANSIENT`（A，保留為對照／過渡）：Manager 以 `systemd-run` 起 transient unit
      （`PSC_JOB_RUNNER=systemd-run`）。polkit 能收窄的**只有**呼叫者 UID ＋ unit 名
      前綴；`User=`／`--uid=` **不在** polkit detail 內（#603 實測），故「只能降到 job
      帳號」這一半只能由 Manager 端封閉的 argv 產生器在 code level 保證——那正是本次
      改採 B 案的原因，殘餘風險見 `plan_residual_risk`。
    """

    TRANSIENT = "transient"
    TEMPLATE = "template"


#: 明確被拒的特權 unit 屬性——列在規則檔開頭，讓審查者一眼看到邊界在哪。
POLKIT_FORBIDDEN_PROPERTIES: tuple[str, ...] = (
    "User=root",
    "User=<任何非 job 帳號>",
    "AmbientCapabilities=",
    "CapabilityBoundingSet=",
    "PrivateUsers=",
    "SystemCallFilter=",
    "ExecStart=（任意 argv）",
)


@dataclass(frozen=True)
class PolkitRule:
    """產生出來的 polkit 規則：**只有內容字串**，本模組不寫任何系統路徑。"""

    install_path: str
    plan: PolkitPlan
    subject_account: str
    target_account: str
    unit_pattern: str
    allowed_verbs: tuple[str, ...]
    content: str
    #: 本方案在 OS 層**未**強制的部分（空 tuple＝無殘餘）。
    residual_risks: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "install_path": self.install_path,
            "plan": self.plan.value,
            "subject_account": self.subject_account,
            "target_account": self.target_account,
            "unit_pattern": self.unit_pattern,
            "allowed_verbs": list(self.allowed_verbs),
            "residual_risks": list(self.residual_risks),
            "content": self.content,
        }


#: A 方案的 transient unit 名前綴——與 `coordinator/job_runner.UNIT_NAME_PREFIX`
#: 是**成對契約**：改任一邊都必須同步改另一邊，否則 polkit 會拒掉所有 job。
def transient_unit_prefix(layout: "PathLayout") -> str:
    return f"{layout.instance}-job-"


def job_unit_pattern(
    layout: "PathLayout" = None,  # type: ignore[assignment]
    plan: PolkitPlan = PolkitPlan.TEMPLATE,
    principal: Principal = Principal.BUILDER,
) -> str:
    """被授權的 unit 名 regex（錨定）。`principal` 是 M2 的第二實例化擴充點。"""
    layout = layout if layout is not None else DEFAULT_LAYOUT
    if plan is PolkitPlan.TRANSIENT:
        return r"^" + layout.instance + r"-job-[a-z0-9][a-z0-9._-]{0,62}\.service$"
    stem = job_unit_stem(layout, principal)
    return r"^" + stem + r"@[a-z0-9][a-z0-9._-]{0,62}\.service$"


def plan_residual_risk(plan: PolkitPlan, scheme: UidScheme) -> tuple[str, ...]:
    """本方案在 OS 層未強制的部分（誠實標註，runbook 與 PR 皆引用）。"""
    if plan is PolkitPlan.TEMPLATE:
        return ()
    svc = scheme.durable_state_owner
    # 只列會跑模型的 persona——它們是「被攻陷」的實際入口。
    same_uid = sorted(
        {
            p.value
            for p in (Principal.REVIEWER, Principal.PLANNER)
            if scheme.resolve(p) == svc
        }
    )
    risks = [
        f"polkit 的 {POLKIT_ACTION} 只暴露 unit 名稱，**不暴露 User=／--uid=**；"
        f"授權後 systemd 會照請求的任意 User= 起 unit。「只能降到 job 帳號」這一半"
        f"由 Manager 端封閉的 argv 產生器在 code level 保證，OS 層未強制。",
        f"因此**與 {svc} 同 UID 的任何行程**都持有這個 grant，可請求任意 User= 的"
        f" transient unit（含 User=root）。",
    ]
    if same_uid:
        risks.append(
            f"在 {scheme.scheme_id} 方案下，跑模型的 {'／'.join(same_uid)} 與 {svc} 同帳號，"
            f"故其中任一被攻陷即取得上一條的能力——這正是「是否提前三分」要衡量的東西。"
        )
    return tuple(risks)


def build_polkit_rule(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    plan: PolkitPlan = PolkitPlan.TEMPLATE,
    principal: Principal = Principal.BUILDER,
) -> PolkitRule:
    """產生降權授權的 polkit 規則內容（A／B 兩方案共用同一套產生邏輯）。

    兩案的授權面都收窄到「`<svc>` 對特定 unit 名 pattern 的 start/stop」，且
    **unit／verb 明細缺席即拒**；差別在「降到哪個帳號」由誰強制（見 `PolkitPlan`）。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    svc = scheme.durable_state_owner
    target = scheme.resolve(principal)
    if target is None:
        raise ValueError(f"principal 未映射到帳號: {principal}")
    pattern = job_unit_pattern(layout, plan, principal)
    verbs = POLKIT_ALLOWED_VERBS
    verb_check = " && ".join(f'verb !== "{v}"' for v in verbs)
    residual = plan_residual_risk(plan, scheme)

    if plan is PolkitPlan.TRANSIENT:
        headline = (
            f"// 方案 A（transient unit）：{svc} 可 start/stop 名為\n"
            f"//   {transient_unit_prefix(layout)}<job 片段>-<sha8>.service 的 transient unit。\n"
            f"// unit 名前綴與 coordinator/job_runner.UNIT_NAME_PREFIX 是**成對契約**——\n"
            f"// 改任一邊都必須同步改另一邊，否則所有 job 會被 polkit 拒掉（fail-closed）。\n"
            f"//\n"
            f"// ===== 本方案在 OS 層未強制的部分（務必知悉）=====\n"
            + "\n".join(f"// - {r}" for r in residual)
            + "\n//\n"
            f"// 要把這一半也搬到 OS 層，改用方案 B（root-owned 模板 unit，User= 寫死）：\n"
            f"//   python3 -m paulsha_cortex.trust_root polkit {scheme.scheme_id} --template\n"
        )
    else:
        headline = (
            f"// 方案 B（root-owned 模板 unit）：{svc} 只能 start/stop\n"
            f"//   {job_unit_stem(layout, principal)}@<id>.service 的實例。\n"
            f"// 模板檔 /etc/systemd/system/{job_unit_stem(layout, principal)}@.service 由 root 擁有，\n"
            f"// 內容硬寫死 User={target}、NoNewPrivileges=yes、CapabilityBoundingSet=（空），\n"
            f"// 以及固定的 ExecStart={layout.job_shim} %i（root-owned shim）。\n"
            f"// per-job 參數走 Manager-owned spec spool（{layout.job_spec_spool_root}/<id>.json，\n"
            f"// job 帳號唯讀）——{svc} 給得出參數，但給不出 UID、也給不出命令列。\n"
            f"// 因此 {svc} **無法選擇 job 的 UID**，也**無法夾帶任何特權屬性**：\n"
            + "\n".join(f"//     - {p}" for p in POLKIT_FORBIDDEN_PROPERTIES)
            + "\n"
            f"// 這些屬性全部只存在於 root-owned 的模板檔裡，呼叫端連提都提不了。\n"
            f"//\n"
            f"// ===== 為什麼 transient unit 在本方案下一律拒 =====\n"
            f"// StartTransientUnit 的 polkit 檢查**不帶 unit 屬性明細**（規則只看得到\n"
            f"// action id，看不到 User=／AmbientCapabilities=／ExecStart=）。放行 transient\n"
            f"// unit 就等於允許 {svc} 傳 User=root。下方「unit／verb 明細缺席即拒」與\n"
            f"// 只認 `@` 實例名的 pattern 一起把這條路關死。\n"
        )

    content = f"""// /etc/polkit-1/rules.d/49-{layout.instance}-downgrade.rules
// 由 permgen 機械產生（scheme={scheme.scheme_id}, plan={plan.value}）——勿手改；重跑：
//   python3 -m paulsha_cortex.trust_root polkit {scheme.scheme_id} --{plan.value}
//
// ===== 這是 cortex 的降權/提權分界線 =====
{headline}//
// ===== 審查者的一眼結論 =====
// 唯一的放行出口需要同時滿足：
//   (1) subject 是 {svc}；(2) action 是 {POLKIT_ACTION}；
//   (3) unit／verb 明細存在；(4) verb ∈ {{{", ".join(verbs)}}}；
//   (5) unit 名匹配 {pattern}
// 任一不成立即拒絕。函式只有最後一行放行。

polkit.addRule(function(action, subject) {{
    if (subject.user !== "{svc}") {{
        // 不干涉 operator／其他帳號的既有授權（交回 polkit 預設）。
        return polkit.Result.NOT_HANDLED;
    }}
    if (action.id !== "{POLKIT_ACTION}") {{
        // {svc} 的其他 polkit action 一律拒（含 login1／hostname1／systemd1 其他面）。
        return polkit.Result.NO;
    }}
    var unit = action.lookup("unit");
    var verb = action.lookup("verb");
    if (!unit || !verb) {{
        // 明細缺席就無從判斷，一律拒（fail-closed）。
        return polkit.Result.NO;
    }}
    if ({verb_check}) {{
        return polkit.Result.NO;
    }}
    if (!/{pattern}/.test(unit)) {{
        // 只有上述 pattern 的 job unit；{layout.instance}-manager.service 等一律拒。
        return polkit.Result.NO;
    }}
    return polkit.Result.YES;
}});
"""
    return PolkitRule(
        install_path=f"/etc/polkit-1/rules.d/49-{layout.instance}-downgrade.rules",
        plan=plan,
        subject_account=svc,
        target_account=target,
        unit_pattern=pattern,
        allowed_verbs=verbs,
        content=content,
        residual_risks=residual,
    )


def transient_unit_properties(
    scheme: UidScheme,
    layout: "PathLayout" = None,  # type: ignore[assignment]
    principal: Principal = Principal.BUILDER,
    plan: PermissionPlan | None = None,
) -> tuple[str, ...]:
    """A 方案的 `--property=` 建議清單（與 B 方案模板 unit 同源，機械產生）。

    `job_runner` 目前只送 `NoNewPrivileges=yes`；本函式把同一套加固表與**由登記表
    導出的 ReadWritePaths** 展開成 `systemd-run --property=` 形式，供 operator 在
    A 方案下逐條加固，或作為「A 與 B 的加固面是否等價」的對照表。
    """
    layout = layout if layout is not None else DEFAULT_LAYOUT
    plan = plan or generate_plan(scheme)
    account = scheme.resolve(principal)
    if account is None:
        raise ValueError(f"principal 未映射到帳號: {principal}")
    job_layout = layout.with_job_segment("%i")
    props = [f"--property={key}={value}" for key, value, _why in _HARDENING]
    for rwp in read_write_paths(
        plan, job_layout, account, job_layout.job_extra_write_paths(account)
    ):
        props.append(f"--property=ReadWritePaths={rwp}")
    return tuple(props)


def evaluate_polkit(
    rule: PolkitRule,
    *,
    user: str,
    action_id: str,
    unit: str | None = None,
    verb: str | None = None,
) -> str:
    """規則決策的 Python 鏡像（polkit 無法本機執行，故以純函式測產生邏輯）。

    與 `build_polkit_rule` 產出的 JS **共用同一組常數**（subject／action／verbs／
    pattern），因此決策矩陣測到的就是規則檔的語意。回傳 `"YES"`／`"NO"`／
    `"NOT_HANDLED"`。
    """
    if user != rule.subject_account:
        return "NOT_HANDLED"
    if action_id != POLKIT_ACTION:
        return "NO"
    if not unit or not verb:
        return "NO"
    if verb not in rule.allowed_verbs:
        return "NO"
    if re.search(rule.unit_pattern, unit) is None:
        return "NO"
    return "YES"
