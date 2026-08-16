"""R3：Manager 啟動自檢（Phase 1 只 WARN）。

spec §R3 要求 Manager 啟動自檢驗證 enforcement plane 對 headless 不可寫。Phase 1
（不需 root）只實作**可先行部分**（spec §R10 Phase 1 第 5 條）：用 R1 登記表對照
**現行部署實況**，輸出結構化診斷——哪些該 Manager-owned 的路徑現在其實 headless
可寫。Phase 1 **只 WARN、不 fail-closed**（fail-closed 是 Phase 2 R3 切換）。

「headless 可寫」在 Phase 1（同 UID、尚無獨立 headless UID）以**檔案 mode 的
group／other 寫入位**近似：裁決 10-1／10-2 下 Phase 2 會把 headless job 降權到與
Manager 不同的 UID 但**同一個 group**（或無交集），因此一個 Manager-owned 目錄若
帶 `g+w`／`o+w`，在 Phase 2 的 shared-group 佈局下即等於 headless 可寫。這個近似
**足以反證 spec 背景段的盤點**（實測 control／coordinator／core／specs 為
`drwxrwxr-x`、bootstrap env 為 `-rw-rw-r--`）——這正是本自檢的驗收依據。

自檢是唯讀觀測，**永不改動任何檔案**、永不 raise（fire-and-forget）。
"""
from __future__ import annotations

import os
import pwd
import stat as stat_module
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from . import registry
from .registry import TrustRootAsset, TrustTree

GROUP_WRITE = 0o020
OTHER_WRITE = 0o002


class FindingStatus(Enum):
    OK = "ok"                      # Manager-owned 且無 group/other 寫入位
    JOB_WRITABLE = "job-writable"  # Manager-owned 但 group/other 可寫 → Phase 2 須收斂
    MISSING = "missing"            # 路徑尚未存在（未部署／未初始化）
    UNRESOLVED = "unresolved"      # path_resolver=None 或解析失敗，本階段無法就地檢查
    NOT_APPLICABLE = "n/a"         # job-visible 樹：本來就允許對應 headless 寫入


@dataclass(frozen=True)
class PathObservation:
    exists: bool
    is_symlink: bool
    mode: int          # st_mode & 0o777
    owner: str
    group: str
    group_writable: bool
    other_writable: bool


@dataclass(frozen=True)
class Finding:
    asset_id: str
    tier: str
    tree: str
    status: FindingStatus
    path: str          # 已遮蔽 $HOME 的展示字串
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "tier": self.tier,
            "tree": self.tree,
            "status": self.status.value,
            "path": self.path,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SelfCheckReport:
    findings: tuple[Finding, ...]
    #: Phase 1 只 WARN——本旗標永遠為 True 表示「即使有 job-writable 也不阻擋啟動」。
    warn_only: bool = True

    @property
    def job_writable(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.status is FindingStatus.JOB_WRITABLE)

    @property
    def ok(self) -> bool:
        """無任何 job-writable 的 Manager-owned 資產。Phase 1 不用它 gate，只供觀測。"""
        return not self.job_writable

    def to_dict(self) -> dict[str, object]:
        return {
            "check": "trust-root-selfcheck",
            "phase": 1,
            "enforcement": "warn-only",
            "ok": self.ok,
            "job_writable_count": len(self.job_writable),
            "findings": [f.to_dict() for f in self.findings],
        }

    def warning_lines(self) -> tuple[str, ...]:
        """人可讀的 WARN 行（每個 job-writable finding 一行）。"""
        lines: list[str] = []
        for f in self.job_writable:
            lines.append(
                f"trust-root WARN [{f.tier}] {f.asset_id}: {f.path} → {f.detail} "
                f"(Phase 1 僅告警；Phase 2 OS 邊界會強制收斂)"
            )
        return tuple(lines)


def _mask_home(path: Path, home: Path) -> str:
    try:
        rel = path.relative_to(home)
        return str(Path("$HOME") / rel)
    except ValueError:
        return str(path)


def observe_path(path: Path) -> PathObservation:
    """唯讀 stat 一個路徑（不跟隨 symlink 判斷 owner／mode）。"""
    st = os.lstat(path)  # raises if missing; caller guards
    is_link = stat_module.S_ISLNK(st.st_mode)
    mode = st.st_mode & 0o777
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        owner = str(st.st_uid)
    try:
        import grp

        group = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError, ImportError):
        group = str(st.st_gid)
    return PathObservation(
        exists=True,
        is_symlink=is_link,
        mode=mode,
        owner=owner,
        group=group,
        group_writable=bool(mode & GROUP_WRITE),
        other_writable=bool(mode & OTHER_WRITE),
    )


def _resolve_asset_path(asset: TrustRootAsset) -> Path | None:
    """呼叫 asset 的 canonical path_resolver 取得實際路徑。resolver=None → None。"""
    if asset.path_resolver is None:
        return None
    modname, _, funcname = asset.path_resolver.partition(":")
    try:
        import importlib

        mod = importlib.import_module(modname)
        func = getattr(mod, funcname)
        result = func()
    except Exception:  # noqa: BLE001 — 唯讀自檢永不 raise
        return None
    return result if isinstance(result, Path) else None


def _evaluate(asset: TrustRootAsset, home: Path) -> Finding:
    tier = asset.tier.name
    tree = asset.tree.value
    path = _resolve_asset_path(asset)
    if path is None:
        return Finding(
            asset.asset_id, tier, tree, FindingStatus.UNRESOLVED,
            asset.path_resolver or "(derived elsewhere)",
            "path_resolver=None 或解析失敗；就地 mode 檢查留待路徑收斂／Phase 2。",
        )
    shown = _mask_home(path, home)
    try:
        obs = observe_path(path)
    except (FileNotFoundError, OSError):
        return Finding(asset.asset_id, tier, tree, FindingStatus.MISSING, shown,
                       "路徑尚未存在（未部署／未初始化）。")

    if asset.tree is TrustTree.JOB_VISIBLE:
        # job-visible 樹本來就允許對應 headless 寫入；group/other 寫入非缺陷。
        return Finding(asset.asset_id, tier, tree, FindingStatus.NOT_APPLICABLE, shown,
                       f"job-visible（{'g+w ' if obs.group_writable else ''}"
                       f"{'o+w ' if obs.other_writable else ''}mode={oct(obs.mode)}）。")

    # Manager-owned 樹：group／other 寫入位即 Phase 2 shared-group 下的 headless 可寫。
    if obs.group_writable or obs.other_writable:
        bits = []
        if obs.group_writable:
            bits.append("g+w")
        if obs.other_writable:
            bits.append("o+w")
        return Finding(
            asset.asset_id, tier, tree, FindingStatus.JOB_WRITABLE, shown,
            f"Manager-owned 但 {'/'.join(bits)}（mode={oct(obs.mode)} "
            f"owner={obs.owner} group={obs.group}）。",
        )
    return Finding(asset.asset_id, tier, tree, FindingStatus.OK, shown,
                   f"mode={oct(obs.mode)} owner={obs.owner}。")


def run_self_check(*, home: Path | None = None) -> SelfCheckReport:
    """對登記表全部資產跑一次唯讀自檢，回傳結構化報告（Phase 1 WARN-only）。"""
    resolved_home = (home or Path.home()).expanduser()
    findings = tuple(_evaluate(asset, resolved_home) for asset in registry.ASSET_REGISTRY)
    return SelfCheckReport(findings=findings)


def emit_startup_warnings(
    *,
    emit: Callable[[str], None],
    home: Path | None = None,
) -> SelfCheckReport:
    """Manager 啟動時呼叫：跑自檢並把每個 job-writable finding 以 WARN 送出。

    `emit` 由呼叫端提供（daemon 傳入寫 stderr 的 callable）。**永不 raise**——
    自檢失敗不得阻擋 daemon 啟動（Phase 1 WARN-only 語意）。回傳報告供測試斷言。
    """
    try:
        report = run_self_check(home=home)
    except Exception:  # noqa: BLE001
        return SelfCheckReport(findings=())
    for line in report.warning_lines():
        try:
            emit(line)
        except Exception:  # noqa: BLE001
            pass
    return report
