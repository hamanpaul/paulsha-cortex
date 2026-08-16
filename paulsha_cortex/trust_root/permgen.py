"""Phase 2a：權限產生器——用 R1 登記表機械產生目錄 owner／group／mode 清單。

spec §R10 Phase 2 第 2 條要求「目錄 owner／mode **由 R1 登記表產生**（權限產生器以
登記表為輸入，不手寫）」。本模組即該產生器：吃 `registry.ASSET_REGISTRY` ＋一個
**UID 方案 config**（`UidScheme`，persona→OS 帳號的映射），機械算出每個資產路徑的
目標權限，輸出 (a) 結構化清單（可轉 JSON）與 (b) 可供 Phase 2b runbook 引用的
`chown`／`chmod`／`setfacl` 命令字串。

**本模組純為產生器：只回傳資料與字串，絕不執行任何 root 操作、不 chown、不 chmod、
不建 UID。** 命令字串供 operator 在 runbook 中手動 `sudo` 執行。

## UID 方案的參數化（operator 0816 裁決：路線 A、Manager 專屬 UID、現階段先二分、
## 但保留「二往三分」彈性）

`UidScheme` 把每個 `Principal` 映射到具體 OS 帳號名，並指定：
- `durable_state_owner`：擁有 Manager-owned durable state 樹的帳號；
- `deploy_account`：enforcement plane（部署面）的擁有者（root／部署帳號）。

**二分**（預設，`TWO_WAY_SCHEME`）：只有兩個 headless-相關帳號——
`cortex-builder`（builder）與 `cortex-svc`（Manager＋reviewer＋planner＋monitor
共用，且即 durable state owner）。reviewer 與 builder 因此落在互不可寫的不同帳號
（滿足 spec §R2 對 independence 的最低要求）。

**三分**（`THREE_WAY_SCHEME`）：把 `cortex-svc` 拆為 `cortex-manager`（僅 Manager／
monitor，且為 durable state owner）與 `cortex-reviewer-planner`（reviewer＋planner
的 job 帳號，**不**擁有 durable state）。這實現裁決要保留的「未來把 Manager 拆成
第三個 UID」彈性——**只換 config，不改本模組任何一行程式碼**。

兩個方案套用**同一套 policy 函式**，因此都能對登記表每一項產出一致（滿足同一組
不變式）的權限集合：Manager-owned／deployment 樹對任何 headless 帳號皆不可寫，
job-visible 樹由對應 job 帳號寫、跨 persona 互不可寫。
"""
from __future__ import annotations

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


#: 二分（預設）：builder 一個帳號，其餘 headless／Manager／monitor 共用 cortex-svc。
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

#: 三分：把 cortex-svc 拆成 cortex-manager（durable state owner）與
#: cortex-reviewer-planner（reviewer＋planner 的 job 帳號，不持有 durable state）。
#: **與二分共用同一套 policy，僅換 config。**
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
})
_FILE_ASSET_IDS = frozenset({
    "engineering-outcome-outbox",  # 單一 .jsonl 檔（名稱含 outbox 但是檔）
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
        )
        open_points.append(
            "部署路徑最終位置待 operator 定（pipx tree 遷到 root/svc-owned 部署路徑；"
            "bootstrap env／codex hooks 是否移出 operator HOME）。"
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
            owner = trusted_owner
            mode = _dir_file_mode(is_dir, 0o7 if is_dir else 0o6, 0, 0)
            for pacct in sorted(job_writers):
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
            is_dir = True  # 多 persona 容器一律視為目錄（per-job 子物件容器）
            mode = _dir_file_mode(True, 0o7, 0, 0o1)  # 0701：others 僅 traverse 進自己被 chown 的子目錄
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


def plan_to_commands(
    plan: PermissionPlan,
    path_of: Mapping[str, str] | None = None,
) -> list[str]:
    """把計畫轉成 runbook 可引用的命令序列（**只產生字串，絕不執行**）。

    `path_of`：asset_id→真實路徑字串；未提供者以 placeholder 呈現，供 runbook 以
    shell 變數替換。輸出含分節註解，方便 operator 對照登記表逐項核可。
    """
    lines: list[str] = [
        f"# trust-root Phase 2b 權限套用命令（scheme={plan.scheme_id}）",
        "# 由 permgen 機械產生；operator 逐項 review 後手動 sudo 執行。",
        "# 未提供真實路徑者以 <PATH:asset_id> placeholder 呈現。",
    ]
    for e in plan.entries:
        path = (path_of or {}).get(e.asset_id) or _placeholder_path(e)
        lines.append("")
        lines.append(f"# [{e.tier}] {e.asset_id} ({e.owner_class.value}) — {e.rationale}")
        if e.runtime_managed:
            lines.append("#   注意：per-child owner 由降權啟動器逐案 chown（本節僅容器層）。")
        for op in e.open_points:
            lines.append(f"#   未決：{op}")
        for cmd in e.commands(path):
            lines.append(cmd)
    return lines
