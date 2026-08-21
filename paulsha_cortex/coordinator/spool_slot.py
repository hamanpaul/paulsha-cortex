"""兩個 spool 共用的 per-job 生命週期（建立 → producer 寫 → consumer 讀 → seal）。

`review-verdict-spool`（Phase 2a／#599）與 `commit-spool`（#623／#636）是**同一種
東西**：一個 Manager-owned 的容器，底下每個 job 一格；producer（reviewer／builder）
以 per-account ACL 取得 `wx` 無 `r` 的授權往自己那一格寫，consumer（Manager）讀，
落地後那一格封口。#638 的三個缺陷之所以有兩個實例，正是因為兩邊各自實作了這段
生命週期——本模組把它收斂成單一份，兩個 spool 都只是它的呼叫端。

三個缺陷與各自的修法（全部有 operator 的實機證據，見 #638）：

**缺陷 1——`mkdir(mode=...)` 把繼承來的 ACL 重設掉。** 在帶 default ACL 的樹上，
明確 mode 會連 **ACL mask** 一起寫掉，把繼承來的具名條目壓成 `#effective:---`：

```
user::rwx
user:cortex-builder:-wx    #effective:---     ← mkdir(mode=0o700) 之後
mask::---
```

producer 因此連建檔都不行（實機：``fatal: Unable to create
'…/commits.bundle.part.lock': Permission denied``）。修法是
:func:`create_slot` **不傳明確 mode**，讓 default ACL 決定初始權限，再由
:func:`narrow_inherited_mode` 事後**檢查**而不是無條件 `chmod`——見該函式對
「不比 0700 更鬆」與「不壓掉 mask」之間取捨的說明。

**缺陷 2——`wx` 無 `r` 的那一格上，consumer 讀不到 producer 建的檔。** 檔由
producer 擁有、又常帶降權 unit 的 `UMask=0077`，Manager 是**目錄**的 owner 但那
不給檔案內容的讀取權。修法沿用 #637 已在實機驗證過的繞法：**producer 自己在寫完
後把成果放寬到 0644**（:data:`PUBLISHED_FILE_MODE`），in-process 的 producer 用
:func:`publish_file`、由 wrapper script 驅動的用 :func:`publish_file_command`。
放寬不擴張暴露面——那一格的容器是 `0700 <manager>` ＋ per-account ACL，別的帳號
連 traverse 都進不來。

**缺陷 3——consumer `chmod` 不了 producer 擁有的檔。** 只有檔案 owner 或 root
能 `chmod`，所以「落地後轉唯讀」若實作成 `os.chmod(<producer 的檔>, 0o444)`，在
三分下必定 `PermissionError`；而該處刻意不 raise，於是**無聲失敗**（實機：
reviewer 可以在 Manager 判讀之後回頭覆寫自己的 verdict）。修法同樣沿用 #637：
:func:`seal_slot` 封的是**目錄**——Manager 是目錄的 owner，收掉目錄的 `w` 之後那
一格再也建不了、改不了名、刪不掉任何檔，而 `chmod` 同時把 ACL mask 收成 `---`，
producer 具名條目的 `x`（traverse）一併失效，連既有的檔都再也打不開。

**誠實邊界**：seal 的強制力來自「producer 進得去那一格的**唯一**路徑是具名 ACL
條目」。同 UID（Phase 2b 之前、或 `direct` 模式）下 producer 就是目錄 owner，本
模組任何一段都攔不住它——那是已知且已記錄的邊界，不是本模組宣稱要守的東西。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

#: `review-verdict-spool` 那一格裡的成果檔名（目錄本身以 reviewer job id 定址）。
#: 定義放在共用層是為了讓 `launcher` 組 wrapper 的發表段時不必回頭 import
#: `coordinator.review`（那條路會把整個 verification 依賴鏈拉進 launcher）。
REVIEW_VERDICT_FILENAME = "verdict.json"

#: `commit-spool` 那一格裡的成果檔名（目錄本身以 spool key 定址）。
COMMIT_BUNDLE_FILENAME = "commits.bundle"

#: producer 寫完成果後放寬到的 mode（缺陷 2）。consumer 讀得到即可，不需要 `w`。
PUBLISHED_FILE_MODE = 0o644

#: **job log 檔由 consumer（Manager）預先建立**時用的 mode（#686 → #708 起三個
#: principal 共用）。
#:
#: **不是 0600**：POSIX ACL 下新檔的 `mask` 由 open(2) 的 mode 參數之 group 位夾擠，
#: `0600` 會把繼承來的 `user:<job>:-wx` 壓成 `#effective:---`（＝缺陷 1 的同一個
#: 機制），job 於是連 append 都不行。
#:
#: **為什麼由 Manager 建而不是讓 job 自己建**：job 建的檔由 job 擁有、又帶降權 unit
#: 的 `UMask=0077`，Manager 是**目錄**的 owner 但那不給檔案內容的讀取權（缺陷 2）。
#: 另外兩個 spool 靠 producer 自己 `chmod 0644` 繞過（:func:`publish_file_command`），
#: 那需要一段跑在 job 之後的 wrapper；而 log 是 **shim 在 exec 之前**就接管的東西，
#: job 失敗得越早就越沒有機會放寬自己的 log——而那正是 #708 要修的那個時序。
JOB_LOG_FILE_MODE = 0o620

#: seal 時對那一格內既有檔案的 best-effort 唯讀化。**只有在 consumer 恰好也是檔案
#: owner 時才會成功**（同 UID／`direct` 模式）；三分下必然 `PermissionError`，屬
#: 預期內，真正的封口是 :data:`SEALED_SLOT_MODE`。
SEALED_FILE_MODE = 0o444

#: seal 之後 per-job 目錄的 mode。收掉 `w` ⇒ 那一格定版；`chmod` 同時把 ACL mask
#: 收成 `---` ⇒ producer 具名條目的授權一併失效。
SEALED_SLOT_MODE = 0o500

#: POSIX ACL 的 access ACL 落在這個 xattr。存在 ⇒ 這一項的 group 位是 **mask**，
#: 不是「群組的實際權限」。
ACCESS_ACL_XATTR = "system.posix_acl_access"

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class PerJobWritableSurface:
    """One row wired into path lookup, unit generation and runtime consumers."""

    surface_id: str
    path_accessor: str
    coordinator_relative: str
    provisioner: str
    consumer: str
    probe: str
    principals: tuple[str, ...]
    asset_id: str

    @property
    def writable_root(self) -> str:
        from ..config import paths

        if self.surface_id.endswith("-codex-home"):
            return str(paths.agents_root() / "runtime" / "codex-home" / self.principals[0])
        if self.surface_id.endswith("-runtime-cache"):
            return str(paths.agents_root() / "runtime" / "job-cache" / self.principals[0])
        accessor = getattr(paths, self.path_accessor)
        if self.surface_id.endswith(("-job-log", "-codex-home", "-runtime-cache")):
            return str(accessor(self.principals[0]))
        return str(accessor())

    @property
    def slot_template(self) -> str:
        return f"{self.writable_root}/%i"


PER_JOB_WRITABLE_SURFACES: tuple[PerJobWritableSurface, ...] = (
    PerJobWritableSurface("commit-spool", "commit_spool_root", "commit-spool", "create_slot", "commit_bundle_path", "render_job_writable_properties", ("builder",), "commit-spool"),
    PerJobWritableSurface("monitor-event-spool", "monitor_event_spool_root", "monitor/event-spool", "create_slot", "EventSpool", "render_job_writable_properties", ("builder",), "monitor-event-spool"),
    PerJobWritableSurface("review-verdict-spool", "review_verdict_spool_root", "review-verdicts", "create_slot", "review_verdict_spool_path", "render_job_writable_properties", ("reviewer",), "review-verdict-spool"),
    PerJobWritableSurface("gate-ledger-spool", "gate_ledger_spool_root", "gate-ledger-spool", "create_slot", "gate_spool_ledger_path", "render_job_writable_properties", ("gate",), "gate-ledger-spool"),
    PerJobWritableSurface("gate-worktree", "gate_worktree_root", "gate-worktree", "create_slot", "gate_worktree_dir", "render_job_writable_properties", ("gate",), "gate-worktree-pool"),
    PerJobWritableSurface("builder-job-log", "job_log_spool_root", "commit-spool/build-logs", "prepare_job_log", "prepare_job_log_spool", "build_job_log_probe", ("builder",), "build-job-log-spool"),
    PerJobWritableSurface("reviewer-job-log", "job_log_spool_root", "review-verdicts/planning-logs", "prepare_job_log", "PlanningJobInvoker", "build_job_log_probe", ("reviewer",), "planning-job-log-spool"),
    PerJobWritableSurface("gate-job-log", "job_log_spool_root", "gate-ledger-spool/gate-logs", "prepare_job_log", "prepare_gate_job_log", "build_job_log_probe", ("gate",), "gate-job-log-spool"),
    PerJobWritableSurface("builder-codex-home", "agents_root", "runtime/codex-home/builder", "create_slot", "build_job_env", "codex_runtime_probe", ("builder",), "job-codex-home"),
    PerJobWritableSurface("reviewer-codex-home", "agents_root", "runtime/codex-home/reviewer", "create_slot", "build_job_env", "codex_runtime_probe", ("reviewer",), "job-codex-home"),
    PerJobWritableSurface("builder-runtime-cache", "agents_root", "runtime/job-cache/builder", "create_slot", "build_job_env", "codex_runtime_probe", ("builder",), "job-runtime-cache"),
    PerJobWritableSurface("reviewer-runtime-cache", "agents_root", "runtime/job-cache/reviewer", "create_slot", "build_job_env", "codex_runtime_probe", ("reviewer",), "job-runtime-cache"),
)


def writable_surface(surface_id: str) -> PerJobWritableSurface:
    try:
        return next(row for row in PER_JOB_WRITABLE_SURFACES if row.surface_id == surface_id)
    except StopIteration as exc:
        raise ValueError(f"unknown writable surface: {surface_id!r}") from exc


def provision_runtime_surfaces(*, principal: str, job_id: str) -> tuple[Path, ...]:
    """Provision runtime rows by enumerating the canonical registry.

    Parent default ACLs installed by permgen grant only the row principal access.
    Control leaves are created before the unit starts and are additionally bind
    mounted read-only by the generated unit; auth.json intentionally remains a
    writable runtime leaf.
    """
    provisioned: list[Path] = []
    for row in PER_JOB_WRITABLE_SURFACES:
        if principal not in row.principals or not row.surface_id.endswith(
            ("-codex-home", "-runtime-cache")
        ):
            continue
        slot = canonical_job_slot(row.surface_id, job_id)
        if slot.exists():
            validate_job_slot_shape(slot)
        else:
            create_slot(slot, reset=False)
        if row.surface_id.endswith("-codex-home"):
            for dirname in ("plugins", "skills"):
                control_dir = slot / dirname
                control_dir.mkdir(exist_ok=True)
                control_dir.chmod(0o555)
            for filename, content in (
                ("config.toml", "# deployment-owned Codex configuration\n"),
                ("hooks.json", "{}\n"),
            ):
                control = slot / filename
                if not control.exists():
                    control.write_text(content, encoding="utf-8")
                control.chmod(0o444)
            auth = slot / "auth.json"
            if not auth.exists():
                auth.touch(mode=0o600)
        provisioned.append(slot)
    return tuple(provisioned)


def _lexical_root(path: str | Path) -> Path:
    """Return an absolute path without following a deployment symlink."""
    root = Path(path).absolute()
    # A redirected coordinator root is an input boundary, not a convenience path.
    # Resolving it here would make a symlink appear to be an owned slot later.
    current = root
    while current != current.parent:
        if current.is_symlink():
            raise SpoolSlotError("symlink", f"writable surface parent is a symlink: {current}")
        current = current.parent
    return root


def canonical_job_slot(
    surface_id: str,
    job_id: str,
    *,
    coordinator_root: str | Path | None = None,
    writable_root: str | Path | None = None,
) -> Path:
    """Return the Manager-selected instance slot for one registered surface.

    This is deliberately the only generic path join for per-job writable roots;
    callers must provide the registry identity, never payload text.
    """
    surface = writable_surface(surface_id)
    if coordinator_root is not None and writable_root is not None:
        raise ValueError("coordinator_root and writable_root are mutually exclusive")
    if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
        raise ValueError(f"unsafe job identity: {job_id!r}")
    from ..config import paths
    from .job_workspace import job_segment

    instance = job_segment(job_id)

    if writable_root is not None:
        root = Path(writable_root)
    elif coordinator_root is None:
        root = Path(surface.writable_root)
    elif surface_id == "gate-worktree":
        root = Path(coordinator_root)
    else:
        root = Path(coordinator_root) / surface.coordinator_relative
    return _lexical_root(root) / instance


def validate_job_slot_shape(slot: str | Path, *, allow_symlink: bool = False) -> Path:
    """Fail closed unless *slot* is an ordinary directory in its parent."""
    path = Path(slot)
    if path.is_symlink() and not allow_symlink:
        raise SpoolSlotError("symlink", f"job slot is a symlink: {path}")
    if not path.exists() or not path.is_dir():
        raise SpoolSlotError("shape", f"job slot is not a directory: {path}")
    if path.name in {"", ".", ".."} or _JOB_ID_RE.fullmatch(path.name) is None:
        raise SpoolSlotError("shape", f"unsafe job slot name: {path.name!r}")
    return path


class SpoolSlotError(Exception):
    """per-job 那一格的生命週期錯誤。

    `kind` 讓呼叫端把它翻成自己的錯誤型別與既有訊息（`review` 是 `RuntimeError`
    ＋「preseeded …」、`job_workspace` 是 `WorkspaceError`）——兩個 spool 對
    operator 的錯誤面不因為共用實作而改變。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def has_posix_acl(path: str | Path) -> bool:
    """這一項身上有沒有 access ACL（⇒ st_mode 的 group 位是 mask）。

    以 xattr 直接判定而不是呼叫 `getfacl`：本模組跑在 dispatch 的熱路徑上，且
    `getfacl` 不一定安裝。平台沒有 xattr（非 Linux）或檔案系統不支援時回 False，
    退化成「純 mode 語意」，與修法前的行為一致。
    """

    try:
        return ACCESS_ACL_XATTR in os.listxattr(path)
    except (OSError, AttributeError, ValueError):
        return False


def narrow_inherited_mode(path: str | Path) -> int:
    """把繼承來的初始權限收窄，但**不動 ACL mask**。回傳收窄後的 mode。

    `mkdir(mode=0o700)` 的本意是「不比 0700 更鬆」，但在帶 default ACL 的樹上它的
    實際效果是「把 producer 的授權關掉」（缺陷 1）。取捨如下：

    - **`other` 位一律收掉。** 它在 ACL 語意下就是 `other::`，與 mask 無關，收窄
      它不會影響任何具名條目——這一半的「不比 0700 更鬆」無條件保住。
    - **`group` 位只在這一項**沒有** access ACL 時才收掉。** 有 ACL 時 group 位
      **就是 mask**，把它壓成 0 正是缺陷 1 本身；此時 group 位的值由 operator 的
      default ACL（`default:mask::`）決定，那是**授權模型的一部分**，不是本函式
      該覆寫的東西。這一格的實際邊界是它的容器（`0700 <manager>`，別的帳號連
      traverse 都進不來）＋ per-account 具名條目，不是這一格自己的 group 位。

    因此「不比 0700 更鬆」在無 ACL 的部署下逐字成立（也就是所有既有測試環境），
    在有 ACL 的部署下被**明確地**讓給 default ACL——那正是 operator 宣告授權的
    地方。若 mode 已經合規則完全不呼叫 `chmod`（`chmod` 本身就是會重設 mask 的
    那個動作，能不做就不做）。
    """

    mode = stat.S_IMODE(os.stat(path).st_mode)
    target = mode & ~0o007
    if not has_posix_acl(path):
        target &= ~0o070
    if target != mode:
        os.chmod(path, target)
    return target


def ensure_container(container: str | Path) -> Path:
    """建立 spool 的容器（根目錄）；已存在時**完全不動**它。

    已存在時不 `chmod` 是刻意的：真正的 owner／mode／ACL 由 Phase 2b 的 permgen
    依 R1 登記表套用，本函式若回頭 `chmod` 一次就會把 operator 套好的 mask 壓掉
    ——那是缺陷 1 在容器層的同一個形狀。
    """

    path = Path(container)
    try:
        path.mkdir(parents=True)
    except FileExistsError:
        return path
    narrow_inherited_mode(path)
    return path


def create_slot(slot: str | Path, *, reset: bool) -> Path:
    """建立 per-job 那一格。

    `reset=False`（`review-verdict-spool`）：那一格**必須不存在**——已存在（或是
    symlink）即視為預埋，`SpoolSlotError` 往上丟由呼叫端翻成拒絕派工。這是既有的
    pre-seed 守衛語意，一個位元組都沒放寬。

    `reset=True`（`commit-spool`）：同一個 key 會被重跑（retry-card／forced
    retry 用同一個 slice_id），那一格必須重新可寫。做法是**整個移除再重建**，而
    不是 `chmod` 解封：seal 收掉目錄 `w` 的同時把 ACL mask 收成 `---`，而
    `chmod` 回去只能把 mask 設成「某個我們自己猜的值」——正確的 mask 是 default
    ACL 說了算的那一個，只有讓它重新繼承一次才拿得到。重建同時涵蓋了 #637 既有的
    「起跑前清掉殘留」（預埋一份成果的人得到的是自己的檔案被刪掉）。

    **不傳明確 mode** 是缺陷 1 的修法本身；初始權限交給 default ACL，事後才由
    :func:`narrow_inherited_mode` 檢查並收窄。
    """

    path = Path(slot)
    if path.is_symlink():
        raise SpoolSlotError("symlink", f"spool slot is a symlink: {path}")
    if path.exists():
        if not reset:
            raise SpoolSlotError("preseeded", f"spool slot already exists: {path}")
        _remove_slot(path)
    ensure_container(path.parent)
    try:
        path.mkdir()
    except FileExistsError as exc:
        raise SpoolSlotError("preseeded", f"spool slot already exists: {path}") from exc
    except OSError as exc:
        raise SpoolSlotError("unavailable", f"spool slot unavailable: {path}: {exc}") from exc
    try:
        narrow_inherited_mode(path)
    except OSError as exc:
        raise SpoolSlotError("unavailable", f"spool slot unavailable: {path}: {exc}") from exc
    if not path.is_dir():
        raise SpoolSlotError("unavailable", f"spool slot unavailable: {path}")
    return path


def _remove_slot(path: Path) -> None:
    """移除上一輪那一格（含 seal 之後的 `0500`）。

    先把 owner 位補回來：sealed 目錄沒有 `w`，`rmtree` 進不去。只動 owner 位
    （`| 0o700`），group 位（＝mask）維持原樣——這裡不需要 mask，接下來整個目錄
    就要被刪掉了。裡面的檔可能是 producer 擁有的，但刪除只需要目錄的 `wx`，
    Manager 是目錄的 owner。
    """

    try:
        if not path.is_dir():
            path.unlink()
            return
        os.chmod(path, stat.S_IMODE(os.stat(path).st_mode) | 0o700)
    except OSError as exc:
        raise SpoolSlotError("unavailable", f"spool slot not reusable: {path}: {exc}") from exc
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise SpoolSlotError("unavailable", f"spool slot not reusable: {path}: {exc}") from exc



def prepare_job_log(slot: str | Path, log_path: str | Path) -> Path:
    """建出 per-job 的 log 一格，並**由 consumer 預先建立** log 檔（#686／#708）。

    這是三個降權 principal 共用的那一份實作（`registry.JOB_LOG_SPOOLS` 的三列各自
    只決定「掛在哪一條既有通道底下」）。少了共用，#638 的三個缺陷會在每一個呼叫端
    各長一次——那正是本模組存在的理由。

    `create_slot(reset=True)`：同一個 key／instance 撞名只可能是上一輪的殘骸（retry
    用同一個 slice_id、同一張卡重派），`reset=False` 的 pre-seed 守衛在這裡會把殘骸
    變成一次拒絕派工。commit-spool 走的就是 `reset=True`，同一個理由。

    回傳建好的 log 檔路徑（＝傳入的 `log_path`，供呼叫端串接）。
    """

    slot_path = Path(slot)
    slot_path.parent.mkdir(parents=True, exist_ok=True)
    create_slot(slot_path, reset=True)
    return preseed_job_writable_file(log_path)


def preseed_job_writable_file(path: str | Path) -> Path:
    """由 **consumer（Manager）預建**、job 寫得進去、consumer 讀得回來的一個空檔。

    這是 :func:`prepare_job_log` 對 log 檔做的那一段，抽出來讓 **codex 的 `-o`
    落點**（#727）沿用同一份論證與同一個 mode，而不是在呼叫端再寫一次 `os.open`
    ＋`fchmod`——那就是第二份真相。

    為什麼非預建不可（#638 缺陷 2 的同一個機制）：job 自己建的檔由 job 擁有、又帶
    降權 unit 的 `UMask=0077` ⇒ `0600 <job 帳號>`，Manager 是**目錄**的 owner 但那
    不給檔案內容的讀取權。`-o` 那一格與 log 的差別在於 **Manager 真的要讀它**
    （它是 `_extract_json` 的第二輸出候選），因此「讀不回來」不是診斷面的損失，
    是功能面的損失。

    mode 見 :data:`JOB_LOG_FILE_MODE`（**不是 0600**：那會把繼承來的具名 ACL 條目
    壓成 `#effective:---`）。回傳建好的路徑供呼叫端串接。
    """

    target = Path(path)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, JOB_LOG_FILE_MODE)
    try:
        # umask 會把 open(2) 的 mode 夾掉（Manager unit 帶 `UMask=0077`），因此一定要
        # 再 fchmod 一次；那一次同時把 ACL mask 設成 group 位＝`w`，繼承來的
        # `user:<job>:wx` 於是 effective `-w-`。
        os.fchmod(fd, JOB_LOG_FILE_MODE)
    finally:
        os.close(fd)
    return target


def publish_file(path: str | Path) -> bool:
    """producer 寫完後把成果放寬給 consumer（缺陷 2）；成功回 True。

    best-effort：這一步失敗只代表 consumer 可能讀不到，不該讓 producer 這一輪的
    工作反而失敗（而讀不到會在 consumer 端 fail-closed，不會被靜默吸收）。
    """

    return _chmod_regular_file(path, PUBLISHED_FILE_MODE)


def publish_file_command(path: str | Path) -> str:
    """由 wrapper script（producer 的身分）執行的 :func:`publish_file` 等價段。

    `[ -f … ]` 先判存在：producer 沒產出成果是**多數失敗路徑**的常態，不該在
    JSONL log 裡留下一行 chmod 錯誤。整段以 `|| :` 收尾，確保它永遠不會決定
    script 的 exit code——exit code 的權威是 wrapper 存下來的模型 `$?`（#604）。
    """

    quoted = shlex.quote(str(path))
    return f"{{ [ -f {quoted} ] && chmod {PUBLISHED_FILE_MODE:04o} {quoted}; }} 2>/dev/null || :"


def seal_slot(slot: str | Path) -> bool:
    """成果落地後把那一格封口（缺陷 3）；目錄真的被封起來才回 True。

    封的是**目錄**：那是 consumer 唯一擁有 owner 權的東西。目錄內既有的檔另做一次
    best-effort 唯讀化（:data:`SEALED_FILE_MODE`）——同 UID／`direct` 模式下
    consumer 就是檔案 owner，那一次 `chmod` 會成功並多擋一層；三分下它必定失敗，
    屬預期內，封口的效力全部來自目錄那一次。

    best-effort：封存失敗不得讓一次**已經成功**的收割反而失敗。權威副本此時已經
    落地（verdict → immutable gate evaluation；bundle → 來源樹的 `refs/heads/*`）。
    """

    path = Path(slot)
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        entries = sorted(path.iterdir())
    except OSError:
        return False
    for entry in entries:
        _chmod_regular_file(entry, SEALED_FILE_MODE)
    try:
        os.chmod(path, SEALED_SLOT_MODE)
    except OSError:
        return False
    return True


def _chmod_regular_file(path: str | Path, mode: int) -> bool:
    target = Path(path)
    try:
        if target.is_symlink() or not target.is_file():
            return False
        os.chmod(target, mode)
    except OSError:
        return False
    return True


__all__ = [
    "ACCESS_ACL_XATTR",
    "COMMIT_BUNDLE_FILENAME",
    "JOB_LOG_FILE_MODE",
    "PUBLISHED_FILE_MODE",
    "REVIEW_VERDICT_FILENAME",
    "SEALED_FILE_MODE",
    "SEALED_SLOT_MODE",
    "SpoolSlotError",
    "create_slot",
    "ensure_container",
    "has_posix_acl",
    "narrow_inherited_mode",
    "prepare_job_log",
    "publish_file",
    "publish_file_command",
    "seal_slot",
]
