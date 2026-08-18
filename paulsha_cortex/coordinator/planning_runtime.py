from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from ..config import paths
from . import job_runner, planning_probe_cache
from .launcher import build_agy_argv
from .model_identities import (
    AGY_MODEL_ID,
    CapabilityProbe,
    IdentityRegistry,
    ModelIdentity,
    load_model_identities,
    probe_agy_capability,
)
from .planning import required_heading_hint


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductionPlanningRuntime:
    identity_registry: IdentityRegistry
    probes: Mapping[tuple[str, str], CapabilityProbe]
    primary_questioner: Callable[[Mapping[str, object]], object]
    secondary_planner: Callable[[Mapping[str, object], ModelIdentity], object]
    primary_integrator: Callable[[Mapping[str, object], Mapping[str, object]], object]


def _planning_argv(identity: ModelIdentity, prompt: str, temp_dir: str, worktree: Path) -> list[str]:
    if identity.executor == "agy":
        return build_agy_argv(
            prompt=prompt,
            slice_id="cortex-planning-runtime",
            log_dir=temp_dir,
            worktree=str(worktree),
            allow_unsafe=False,
            model=identity.model_id,
        )
    if identity.executor == "codex":
        return [
            "codex", "exec", prompt, "--json", "--sandbox", "read-only",
            "--model", identity.model_id, "-o", str(Path(temp_dir) / "last.json"),
            "-C", str(worktree), "--skip-git-repo-check",
        ]
    if identity.executor == "claude":
        # issue #404：刻意不帶 `--permission-mode plan`——plan 模式的系統
        # 提示要求模型「必須產出計畫或呼叫 ExitPlanMode」，與這裡「必須
        # 回傳純 JSON」的確定性回聲任務直接衝突（issue 404 實測矩陣：兩者
        # 同時存在時，模型會以「須先給一份計畫」為由拒絕直接回 JSON）。
        # 安全層改由其他機制共同承擔：`--tools ""` 讓模型完全沒有工具可
        # 呼叫；`_invoke_json` 的一次性 disposable sandbox 讓任何輸出頂多
        # 落在拋棄式複本；operator 樹在呼叫前後各做一次 `_tree_snapshot`
        # 比對，任何 operator 內容變化一律 fail-closed 並回滾；`_invoke_json`
        # 另外對 claude 身分注入 hermetic `CLAUDE_CONFIG_DIR`，同時隔離
        # operator 帳號下的 user MCP servers／plugins／hooks／使用者層
        # CLAUDE.md，避免這些注入項讓模型敘事跑題或繞過純 JSON 契約。
        return [
            "claude", "-p", prompt, "--output-format", "json",
            "--tools", "", "--model", identity.model_id,
            "--add-dir", str(worktree),
        ]
    raise ValueError(f"unsupported read-only planning executor: {identity.executor}")


def _snapshot_skipped(relative: Path, name: str) -> bool:
    """快照／drift 走訪共用的排除判準（`_tree_snapshot` 與 `_tree_manifest`）。

    兩者必須永遠看同一組節點：`_tree_snapshot` 負責判斷「有沒有變」，
    `_tree_manifest` 負責回答「變了什麼」。判準若各寫一份就會漂移，
    出現「偵測到 dirty 卻報不出任何 diff」（或反之）的失真報告。

    - `.git`：版控物件不在比對範圍。
    - `__pycache__` / `*.pyc`（issue #397）：本機部署常見拓撲是 daemon 與
      planning launcher 共用同一棵 operator 工作樹（daemon 以 repo 為
      WorkingDirectory 常駐），daemon 對既有模組的 lazy import 會隨時在快照
      窗口內重編 bytecode。這是可由原始碼 100% 重生的快取、不是 operator
      內容，計入雜湊會把正常 churn 誤判成「planner 汙染 operator worktree」。
      跳過的盲點取捨：CPython 的 .pyc 是 timestamp/hash-based 驗證（PEP 552），
      植入的孤兒 .pyc 與對應 .py 不符會被直接忽略重編，不會被 import 採用；
      真正的程式碼污染仍必須經過 .py／其他原始檔變更，不受此例外影響。
    - 快照 root 直下的 `runtime/`（issue #399）：`.gitignore:8` 宣告的 daemon
      狀態殘留（`runtime/handoff/wf-*.json` 每個 periodic tick 整份重寫，內容
      含時間戳必變）。用 relative path 判斷而非只比對 dir name，避免誤跳深層
      同名目錄（例如 `tests/fixtures/runtime/`）。
    """

    if name == ".git":
        return True
    if name == "__pycache__" or name.endswith(".pyc"):
        return True
    if relative == Path(".") and name == "runtime":
        return True
    return False


def _tree_snapshot(root: Path) -> str:
    """Hash the complete tree shape, content, links, and stable metadata.

    The planner runs in a disposable copy, but the operator checkout is also
    hashed before and after launch.  This catches direct writes through an
    absolute path even when the planner exits non-zero.
    """

    digest = hashlib.sha256()

    def add_metadata(path: Path) -> os.stat_result:
        metadata = path.lstat()
        digest.update(f"{metadata.st_mode}:{metadata.st_uid}:{metadata.st_gid}".encode())
        digest.update(b"\0")
        try:
            names = sorted(os.listxattr(path, follow_symlinks=False))
        except (AttributeError, OSError):
            names = []
        for name in names:
            digest.update(name.encode("utf-8", errors="surrogateescape"))
            digest.update(b"=")
            try:
                digest.update(os.getxattr(path, name, follow_symlinks=False))
            except OSError:
                digest.update(b"<unreadable>")
            digest.update(b"\0")
        return metadata

    def visit(path: Path, relative: Path) -> None:
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        metadata = add_metadata(path)
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"dir\0")
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                # 排除判準見 `_snapshot_skipped`（與 `_tree_manifest` 共用同
                # 一份，避免「偵測得到 dirty 卻報不出 diff」的判準漂移）。
                if _snapshot_skipped(relative, child.name):
                    continue
                visit(child, relative / child.name)
        elif stat.S_ISREG(metadata.st_mode):
            digest.update(b"file\0")
            digest.update(path.read_bytes())
        else:
            digest.update(b"special\0")
            digest.update(str(metadata.st_rdev).encode())
        digest.update(b"\0")

    visit(root, Path("."))
    return digest.hexdigest()


def _copy_planning_sandbox(worktree: Path, destination: Path) -> None:
    # issue #397：sandbox 是拋棄式複本，bytecode 可由原始碼重生、不必複製；
    # 排除它同時避免 copytree 過程中 daemon 正在改寫／汰換 __pycache__ 內容
    # 造成 race read（複製到一半 .pyc 消失或被截斷）引發與 planner 汙染無關
    # 的例外。
    pycache_ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(pycache_ignore(directory, names))
        # issue #399：與 `_tree_snapshot` 同語意排除 worktree root 直下的
        # `/runtime/`（daemon 狀態殘留，見該函式內註解）；sandbox 不需要
        # 這份內容，同時避免複製途中 daemon 正在改寫 handoff 檔案造成
        # race read。`shutil.ignore_patterns` 是按名稱全樹匹配，若直接
        # 加入 "runtime" pattern 會連深層同名目錄（例如
        # `pkg/runtime/`）一併誤殺，因此改用自訂 callable，只在走訪到
        # worktree 根目錄時才把 "runtime" 加進忽略清單。
        if Path(directory) == worktree and "runtime" in names:
            ignored.add("runtime")
        return ignored

    shutil.copytree(
        worktree,
        destination,
        symlinks=True,
        ignore=ignore,
    )


def _make_tree_traversable(root: Path) -> None:
    """Restore enough owner access to inspect and discard a hostile tree.

    The launcher can chmod directories through an absolute path.  Never follow
    symlinks while recovering access.

    issue #507：本函式**只能指向拋棄式 sandbox**。它把整棵樹的目錄 mode 強制
    改成 0o700，本身就是一次寫入；修法前它被用在 operator worktree 上（靠事後
    整棵還原把 mode 蓋回去）是可行的，移除整棵還原後就不再成立——對 operator
    工作區只讀不寫是本 issue 的核心約束，drift 分析改走完全唯讀、對讀取失敗
    容錯的 `_tree_manifest`。
    """

    if root.is_symlink():
        raise RuntimeError("planning recovery root cannot be a symlink")
    os.chmod(root, 0o700, follow_symlinks=False)

    def visit(directory: Path) -> None:
        os.chmod(directory, 0o700, follow_symlinks=False)
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == ".git" or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    visit(path)

    visit(root)


# --- issue #507：operator worktree drift 的收斂處置 ---------------------------
#
# 修法前：`_invoke_json` 的 finally 區塊只要偵測到 T0→T1 之間 operator worktree
# 有任何差異，就呼叫 `_restore_operator_tree()`——刪掉 worktree 內除 `.git` 以外
# 的**全部內容**再從 T0 baseline 複本整棵還原。實測（2026-08-14，run
# `workflow-0529388d8e290c8fb938`）兩種資料遺失：
#   (1) operator 在 planning 視窗內新建的未追蹤檔（work item 的 todo 來源）
#       被靜默銷毀，且 `.cortex/work-items.yaml` 留下懸空連結；
#   (2) 前一代 planning **成功**產出的三份 artifact（未追蹤、不在本次 baseline
#       內）被下一次失敗的 rollback 抹除，run 的 `planning_authority` 指向不
#       存在的檔案 → `workflow planning input missing` → work item 卡死。
#
# 根因是歸因錯誤：launcher 以 `cwd=sandbox`（拋棄式複本）執行，operator 樹比對
# 只是「防越界」的安全網；把安全網的補救動作設成整棵樹抹除，在多方並行（operator
# 手動編輯、其他 agent、編輯器自動儲存、背景建置）的真實環境下誤傷機率遠高於
# 真正的越界。加上 baseline 由非原子 `copytree` 取樣，歸因本身就不可靠。
#
# R0 修法（本檔）：
#   1. 整棵還原的程式路徑**移除**，改為 `_contain_operator_drift()`。
#   2. 預設不改寫 operator worktree 一個位元組——只做唯讀 diff、把受影響檔案
#      完整備份進 run-scoped evidence，並落一份結構化報告供 operator 判讀。
#   3. 還原改成需明示 opt-in 且逐路徑收斂（`rollback_scope`），並由三道
#      fail-closed 閘門把守：不在本次 diff 內／命中受保護的權威文件／備份未
#      成功者一律拒絕還原。
#
# 結構解（planning 產出完全不進 operator 樹）屬 R2 evidence 模型範疇，不在此。
PLANNING_WORKTREE_DRIFT_SCHEMA = "cortex-planning-worktree-drift/v1"
# evidence 目錄名刻意不用 `planning-recovery`——`work_actions.
# _read_planning_failure_record` 用 `path.parent.name == "planning-recovery"`
# 當 recover-planning 的收容判準，混進去會多出一筆無法解析的候選、撞上
# `planning failure evidence ambiguous` 的 fail-closed（同 #511 對
# `planning-artifacts` 目錄的處理）。
PLANNING_WORKTREE_DRIFT_DIRNAME = "planning-worktree-drift"
# 備份總量上限：drift 正常只有寥寥數檔，這個上限只用來擋住病態情境（例如
# launcher 把整棵樹改寫）把 evidence 目錄灌爆。超出預算的檔案在報告內標記
# `backed_up: false`，並且**一律逐出 rollback 範圍**——備份不成功就不准抹除，
# 是本 issue 最低限度的保命索。
PLANNING_DRIFT_BACKUP_MAX_BYTES = 64_000_000
# #554：drift 失敗訊息的**穩定前綴**。這串字是下游分類（`manager.
# _is_planning_worktree_drift_failure`）唯一可以依賴的部分——訊息尾段已經在
# #543 改過一次（`changes rolled back` → `operator content preserved`），計數與
# evidence 路徑更是每次都不同。改動本常數等同改動分類契約，必須同步改判準與
# 其回歸測試。
PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX = "planning launcher modified operator worktree"
# #554：備份與報告雙雙寫不出去時，evidence 欄位的退化佔位符。
#
# 原值是 `<unavailable>`，而 `outcome_taxonomy.TRANSIENT_SERVICE_MARKERS` 含裸
# `"unavailable"`（#533 為 agy 的 `UNAVAILABLE (code 503)` 而收）：於是「備份寫
# 入失敗」這個純環境事件，會因為佔位符裡有 `unavailable` 六個字而被判成
# transient-service。詞界比對擋不住這一種——整個 token 就是 marker——所以佔位符
# 這一側必須自己保證**不含任何 taxonomy marker**。
#
# 換佔位符時務必重跑 `tests/test_planning_drift_classification_554.py` 的佔位符
# 不變式測試；注意 `<evidence-unavailable>` 這種看似安全的寫法仍會命中
# （`-` 不是 word char，詞界照樣成立）。
PLANNING_WORKTREE_DRIFT_EVIDENCE_PLACEHOLDER = "<not-written>"
# 受保護的權威文件前綴：這些是 work item 的 source 文件與 cortex 自己登記在
# `planning_authority` 內的受管產物，不論 `rollback_scope` 怎麼要求都不得被
# rollback 動到。
#   - `docs/superpowers/workstreams/**/todo.md`：work item 的 todo 來源
#     （monitor repo provider 的 `todo` kind），被抹除即 `active_todo` 為假、
#     lifecycle 退回 `topic`、不可 claim。
#   - `docs/superpowers/specs/**`、`docs/superpowers/plans/**`：planning 產出
#     的落地位置，同時是 provider 的 `superpowers_spec`／`superpowers_plan`
#     來源；前代 run 的產出被抹除即造成 `workflow planning input missing`。
#   - `openspec/changes/**`：openspec 變更提案，`_planning_destinations` 的
#     首選錨點。
#   - `.cortex/**`：work registry（`work-items.yaml`）本身。
_PROTECTED_AUTHORITY_PREFIXES = (
    ("docs", "superpowers", "workstreams"),
    ("docs", "superpowers", "specs"),
    ("docs", "superpowers", "plans"),
    ("openspec", "changes"),
    (".cortex",),
)


def _is_protected_authority_path(relative: str) -> bool:
    """路徑是否落在受保護的權威文件範圍（見 `_PROTECTED_AUTHORITY_PREFIXES`）。"""

    parts = PurePosixPath(relative).parts
    return any(parts[: len(prefix)] == prefix for prefix in _PROTECTED_AUTHORITY_PREFIXES)


def _entry_digest(path: Path, metadata: os.stat_result) -> dict[str, object]:
    """單一節點的結構化描述；任何讀取錯誤都轉成欄位而非例外。"""

    mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            return {"kind": "symlink", "mode": mode, "error": type(exc).__name__}
        return {"kind": "symlink", "mode": mode, "target": target}
    if stat.S_ISDIR(metadata.st_mode):
        return {"kind": "dir", "mode": mode}
    if stat.S_ISREG(metadata.st_mode):
        try:
            payload = path.read_bytes()
        except OSError as exc:
            return {"kind": "file", "mode": mode, "error": type(exc).__name__}
        return {
            "kind": "file",
            "mode": mode,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return {"kind": "special", "mode": mode, "rdev": metadata.st_rdev}


def _tree_manifest(root: Path) -> dict[str, dict[str, object]]:
    """唯讀走訪整棵樹，產出 `relative posix path -> 節點描述`。

    與 `_tree_snapshot` 共用 `_snapshot_skipped` 的排除判準——前者回答「有沒有
    變」，本函式回答「變了什麼」。

    兩個刻意的設計約束：

    1. **絕不寫入**。修法前為了讀取被 launcher chmod 0 的目錄，會先跑
       `_make_tree_traversable()` 把整棵樹的目錄 mode 改成 0o700（再靠整棵還原
       蓋回去）。移除整棵還原後這條路不能再走，否則 drift 分析本身就在改
       operator 的工作區。
    2. **對讀取失敗容錯**。任何 `OSError` 都記成節點上的 `error` 欄位並繼續，
       報告不會因為樹裡有一個不可讀的角落就整份消失。
    """

    manifest: dict[str, dict[str, object]] = {}

    def visit(path: Path, relative: Path) -> None:
        key = relative.as_posix()
        try:
            metadata = path.lstat()
        except OSError as exc:
            manifest[key] = {"kind": "unreadable", "error": type(exc).__name__}
            return
        entry = _entry_digest(path, metadata)
        # xattr 也納入描述：`_tree_snapshot` 把它算進雜湊，這裡不看的話會出現
        # 「偵測到 dirty 卻報不出任何 diff」的失真報告。
        try:
            names = sorted(os.listxattr(path, follow_symlinks=False))
        except (AttributeError, OSError):
            names = []
        if names:
            attributes: dict[str, str] = {}
            for name in names:
                try:
                    value = os.getxattr(path, name, follow_symlinks=False)
                except OSError:
                    attributes[name] = "<unreadable>"
                    continue
                attributes[name] = hashlib.sha256(value).hexdigest()
            entry["xattrs"] = attributes
        manifest[key] = entry
        if entry.get("kind") != "dir":
            return
        try:
            children = sorted(path.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            entry["children"] = f"unreadable:{type(exc).__name__}"
            return
        for child in children:
            if _snapshot_skipped(relative, child.name):
                continue
            visit(child, relative / child.name)

    visit(root, Path("."))
    return manifest


def _diff_tree_manifests(
    baseline: Mapping[str, dict[str, object]],
    observed: Mapping[str, dict[str, object]],
) -> tuple[dict[str, object], ...]:
    """兩份 manifest 的結構化 diff（`added` / `modified` / `removed`）。"""

    rows: list[dict[str, object]] = []
    for relative in sorted(set(baseline) | set(observed)):
        before = baseline.get(relative)
        after = observed.get(relative)
        if before == after:
            continue
        if before is None:
            change = "added"
        elif after is None:
            change = "removed"
        else:
            change = "modified"
        rows.append(
            {"path": relative, "change": change, "baseline": before, "observed": after}
        )
    return tuple(rows)


def _write_evidence_bytes(target: Path, payload: bytes) -> None:
    """原子寫入 + 0400（比照 `manager._write_planning_failure_evidence`）。"""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o400)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_drift_entries(
    *,
    worktree: Path,
    baseline: Path,
    entries: tuple[dict[str, object], ...],
    destination: Path,
) -> tuple[dict[str, dict[str, object]], bool]:
    """把 diff 命中的每個一般檔案的**兩個版本**完整備份進 evidence。

    `observed/` 收 T1（也就是萬一被抹除就永久消失的那一版），`baseline/` 收
    T0。symlink／目錄／特殊檔沒有位元組內容可存，其 target 與 mode 已完整記在
    報告的 `baseline`／`observed` 欄位裡。

    回傳 `(每條路徑的備份結果, 是否全數備份成功)`。任何一條備份失敗（讀不到、
    超出預算）都會讓該路徑被逐出 rollback 範圍。
    """

    results: dict[str, dict[str, object]] = {}
    complete = True
    budget = PLANNING_DRIFT_BACKUP_MAX_BYTES
    for row in entries:
        relative = str(row["path"])
        outcome: dict[str, object] = {"observed": None, "baseline": None}
        for side, source_root in (("observed", worktree), ("baseline", baseline)):
            descriptor = row.get(side)
            if not isinstance(descriptor, dict) or descriptor.get("kind") != "file":
                continue
            source = source_root / PurePosixPath(relative)
            try:
                # 先看 size 再讀：病態情境（launcher 塞進一個超大檔）不該讓
                # 備份本身先把記憶體吃光。
                if source.lstat().st_size > budget:
                    outcome[side] = {"backed_up": False, "reason": "budget-exceeded"}
                    complete = False
                    continue
                payload = source.read_bytes()
            except OSError as exc:
                outcome[side] = {"backed_up": False, "reason": type(exc).__name__}
                complete = False
                continue
            if len(payload) > budget:
                outcome[side] = {"backed_up": False, "reason": "budget-exceeded"}
                complete = False
                continue
            target = destination / side / PurePosixPath(relative)
            try:
                _write_evidence_bytes(target, payload)
            except OSError as exc:
                outcome[side] = {"backed_up": False, "reason": type(exc).__name__}
                complete = False
                continue
            budget -= len(payload)
            outcome[side] = {
                "backed_up": True,
                "path": str(target),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        results[relative] = outcome
    return results, complete


def _restore_scoped_path(*, worktree: Path, baseline: Path, relative: str) -> None:
    """把單一路徑還原成 baseline 版本（不在 baseline 內者刪除）。

    只處理一般檔案與 symlink；目錄與特殊檔不在可還原範圍（見
    `_contain_operator_drift` 的閘門）。
    """

    target = worktree / PurePosixPath(relative)
    source = baseline / PurePosixPath(relative)
    if target.is_symlink() or target.is_file():
        target.unlink()
    if source.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(os.readlink(source))
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _contain_operator_drift(
    worktree: Path,
    baseline: Path,
    *,
    evidence_root: Path | None,
    run_id: str,
    rollback_scope: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """收斂處置 operator worktree drift：唯讀 diff → 備份 → 報告 →（選擇性）逐路徑還原。

    `rollback_scope` 是呼叫端**能證明由本次 run 自產**的 repo-relative 路徑；
    預設空集合＝一個位元組都不動。三道 fail-closed 閘門：

    1. 不在本次 diff 內的路徑不還原（避免把無關檔案捲進來）。
    2. 命中 `_is_protected_authority_path` 的權威文件永不還原（todo.md 等 work
       item source 文件、前代 planning artifact、work registry）。
    3. 備份未成功（含 evidence 根不可用）的路徑永不還原——不可回復的抹除正是
       本 issue 的核心傷害。

    本函式不 raise：drift 報告是診斷面，寫不出去也不得掩蓋上游真正的失敗。
    """

    observed_manifest = _tree_manifest(worktree)
    baseline_manifest = _tree_manifest(baseline)
    entries = _diff_tree_manifests(baseline_manifest, observed_manifest)
    counts = {
        "added": sum(1 for row in entries if row["change"] == "added"),
        "modified": sum(1 for row in entries if row["change"] == "modified"),
        "removed": sum(1 for row in entries if row["change"] == "removed"),
    }
    drifted = {str(row["path"]) for row in entries}
    summary: dict[str, object] = {
        "schema": PLANNING_WORKTREE_DRIFT_SCHEMA,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "worktree": str(worktree),
        "counts": counts,
        "entries": list(entries),
        "rollback_scope_requested": sorted(rollback_scope),
        "rollback_scope_applied": [],
        "rollback_scope_refused": [],
        "backup_root": None,
        "backup_complete": False,
        "report_path": None,
    }

    refused: list[dict[str, str]] = []
    candidates: list[str] = []
    for relative in sorted(rollback_scope):
        if relative not in drifted:
            refused.append({"path": relative, "reason": "outside-observed-drift"})
        elif _is_protected_authority_path(relative):
            refused.append({"path": relative, "reason": "protected-authority-document"})
        else:
            candidates.append(relative)

    backups: dict[str, dict[str, object]] = {}
    destination: Path | None = None
    if evidence_root is not None and entries:
        digest = hashlib.sha256(
            json.dumps(list(entries), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        destination = (
            Path(evidence_root).resolve()
            / "evidence"
            / PLANNING_WORKTREE_DRIFT_DIRNAME
            / f"{run_id}-{digest}"
        )
        try:
            destination.mkdir(parents=True, exist_ok=True)
            backups, complete = _backup_drift_entries(
                worktree=worktree,
                baseline=baseline,
                entries=entries,
                destination=destination,
            )
            summary["backup_root"] = str(destination)
            summary["backup_complete"] = complete
        except OSError as exc:
            logger.error(
                "planning-worktree-drift-backup-failed run_id=%s error=%s: %s",
                run_id,
                type(exc).__name__,
                str(exc)[:200],
            )
            destination = None
            backups = {}

    for row in entries:
        relative = str(row["path"])
        row["backup"] = backups.get(relative)

    rows_by_path = {str(row["path"]): row for row in entries}
    applied: list[str] = []
    for relative in candidates:
        outcome = backups.get(relative)
        row = rows_by_path[relative]
        kinds: set[object] = set()
        for side in ("baseline", "observed"):
            descriptor = row.get(side)
            if isinstance(descriptor, dict):
                kinds.add(descriptor.get("kind"))
        # 目錄／特殊檔／不可讀節點的還原語意不明（要不要遞迴？要不要重建
        # mode？），一律不碰——這正是修法前整棵還原最容易造成附帶損害的部分。
        if not kinds <= {"file", "symlink"}:
            refused.append({"path": relative, "reason": "unsupported-node-kind"})
            continue
        if outcome is None or any(
            isinstance(side, dict) and side.get("backed_up") is False
            for side in outcome.values()
        ):
            refused.append({"path": relative, "reason": "backup-unavailable"})
            continue
        try:
            _restore_scoped_path(worktree=worktree, baseline=baseline, relative=relative)
        except OSError as exc:
            refused.append({"path": relative, "reason": type(exc).__name__})
            continue
        applied.append(relative)

    summary["rollback_scope_applied"] = applied
    summary["rollback_scope_refused"] = refused

    if destination is not None:
        report = destination / "report.json"
        try:
            _write_evidence_bytes(
                report,
                (
                    json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8"),
            )
        except OSError as exc:
            logger.error(
                "planning-worktree-drift-report-failed run_id=%s error=%s: %s",
                run_id,
                type(exc).__name__,
                str(exc)[:200],
            )
        else:
            summary["report_path"] = str(report)
    return summary


def _operator_drift_message(summary: Mapping[str, object]) -> str:
    """drift 失敗訊息：計數在前、evidence 路徑在後。

    上游 `run_heterogeneous_brainstorm` 會把本例外壓成 `secondary-output-
    malformed: ...` 之類的短字串並截斷（`str(exc)[:160]`），因此把最短且最關鍵
    的計數排在前面，即使被截掉尾巴也還看得到「動了幾個檔」；完整訊息另以
    `logger.error` 落 log，evidence 也在固定目錄用 run_id 可查。
    """

    counts = summary.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    location = (
        summary.get("report_path")
        or summary.get("backup_root")
        or PLANNING_WORKTREE_DRIFT_EVIDENCE_PLACEHOLDER
    )
    return (
        f"{PLANNING_WORKTREE_DRIFT_MESSAGE_PREFIX}; operator content preserved "
        f"(added={counts.get('added', 0)} modified={counts.get('modified', 0)} "
        f"removed={counts.get('removed', 0)}); evidence={location}"
    )


_FENCED_JSON = re.compile(
    r"```(?:json)?\s*\n(?P<body>\{.*\})\s*\n```",
    flags=re.DOTALL | re.IGNORECASE,
)

# issue #401 巢狀欄位名——CLI launcher（claude/codex/agy）的成功 envelope
# 用來裝「模型實際輸出」的欄位名不一而足，沿用既有偵測順序。
_ENVELOPE_KEYS = ("result", "content", "message", "text")

# issue #401：questioner／integrator／secondary planner 的 prompt 過去只用
# 「Return only ... JSON」這類軟性措辭，模型（實測 sonnet 對 questioner
# prompt）偶爾仍回散文推理夾雜 JSON，甚至純散文。附加這段明確的輸出契約，
# 降低模型不遵守純 JSON 格式的機率；即使模型仍不遵守，`_extract_json` 也
# 已改為 fail-closed（見上）而非把 CLI envelope 誤當輸出本體。
_JSON_OUTPUT_CONTRACT = (
    "Output contract: reply with exactly one JSON object and nothing else — "
    "no prose, no explanation, no code fences. Your reply MUST start with '{'."
)


def _find_json_object(text: str, *, allow_partial: bool = False) -> object | None:
    """從字串中盡量抽出一個 JSON 物件；找不到回傳 ``None``（不拋例外）。

    - 先去除整段以 ```/```json 包裹的 code fence（沿用既有 regex，只接受
      「整串」剛好是單一 fenced code block 的情形）。
    - 再嘗試對整串做 `json.loads`。
    - `allow_partial=True` 時才進一步做**平衡大括號掃描**：找到第一個
      `{`，逐字元計數 `{`/`}` 深度（用簡單狀態機忽略字串字面值內、含跳脫
      序列 `\"`/`\\` 的大括號），抓出第一個平衡區塊後再嘗試 `json.loads`。
      這個平衡掃描刻意只在 `allow_partial=True` 時啟用——只給「從散文中
      抽取內嵌 JSON」的呼叫端使用（見 `_extract_json` 對 envelope 巢狀
      欄位的處理）。頂層候選字串（CLI 原始 stdout／`--output-file` 內容）
      必須維持既有的嚴格「整串才算」語意，否則像
      `"Commentary.\\n```json\\n{...}\\n```\\n"` 這種帶前言的輸出會被
      誤判為合法 JSON，弱化既有防呆
      （見 `test_planning_json_parser_accepts_only_whole_fenced_object`）。
    """
    text = text.strip()
    fenced = _FENCED_JSON.fullmatch(text)
    if fenced is not None:
        text = fenced.group("body")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    if not allow_partial:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                block = text[start : index + 1]
                try:
                    return json.loads(block)
                except (json.JSONDecodeError, TypeError):
                    return None
    return None


def _extract_json(stdout: str, output_path: Path) -> object:
    """就地讀取第二輸出候選（codex 的 `-o last.json`）後交給共用抽取層。

    issue #683：抽取本身移到 `_extract_json_candidates`，因為降權之後
    「第二候選是哪來的」由 invoker 決定（in-process 是 tempdir 裡的檔案，
    job 模式下 Manager 讀不到 job-owned scratch，只剩 stdout——design D-j 的
    退步）。本函式維持既有簽章，供直接持有 `output_path` 的呼叫端使用。
    """

    output_text = (
        output_path.read_text(encoding="utf-8") if output_path.is_file() else None
    )
    return _extract_json_candidates(stdout, output_text)


def _extract_json_candidates(stdout: str, output_text: str | None) -> object:
    """兩個候選（第二輸出優先、再 stdout）→ 模型輸出本體。

    JSON 抽取刻意**留在共用層**、不進 invoker：兩個 invoker 吃同一份
    envelope 處理與 fail-closed 判準（design D1）。本 repo 已經在 #401／#516／
    #520 買過三次「同一件事兩份真相」的單。
    """

    candidates = [stdout.strip()]
    if output_text is not None:
        candidates.insert(0, output_text.strip())
    for candidate in candidates:
        value = _find_json_object(candidate)
        if not isinstance(value, dict):
            continue
        if any(key in value for key in _ENVELOPE_KEYS):
            # issue #401：這是 CLI launcher 的成功 envelope（例如 claude CLI
            # 的 `api_error_status` 等 20+ 鍵），不是模型輸出本體。模型有時
            # 不遵守「只回 JSON」的指示、在 envelope 的巢狀欄位裡回散文推理
            # （內容可能正確、格式不從）。依序嘗試每個巢狀欄位：先整串
            # `json.loads`，失敗再從散文中抽取內嵌 JSON 物件；任何一個成功
            # 就回傳抽出的物件。
            fallback_snippet: str | None = None
            for key in _ENVELOPE_KEYS:
                nested = value.get(key)
                if not isinstance(nested, str):
                    continue
                extracted = _find_json_object(nested, allow_partial=True)
                if extracted is not None:
                    return extracted
                if fallback_snippet is None:
                    fallback_snippet = nested[:160]
            # 全部抽取失敗：絕不能 fall through 把整個 envelope dict 當成
            # 輸出本體回傳（修復前的行為）——那會讓下游驗證（例如
            # `validate_question_pack`）報出 `unexpected key: api_error_status`
            # 這種完全誤導的診斷。改為明確 raise，訊息帶散文片段方便除錯。
            detail = fallback_snippet if fallback_snippet is not None else "<no string field>"
            raise ValueError(f"planning launcher result is not JSON: {detail}")
        return value
    # 2026-08-14 實測：agy 服務暫時性 503 時**印錯誤文字但 exit 0**
    # （`Error: Eligibility check failed: UNAVAILABLE (code 503)`），launcher
    # 因此走到這裡。修法前這行不帶任何 stdout 內容，錯誤文字隨 temp_dir 一起
    # 被丟棄——operator 只看得到「no JSON object」，診斷得靠手動重現。帶上
    # 截斷片段後：(1) 503 當場可見；(2) 上游 `_is_planning_transient_service_failure`
    # 能據此把分類從 `content` 改判 `environment`，recover-planning 才有路。
    snippet = next(
        (candidate[:160] for candidate in candidates if candidate),
        "<empty output>",
    )
    raise ValueError(f"planning launcher returned no JSON object: {snippet}")


def _seed_hermetic_claude_env(temp_dir: str) -> dict[str, str] | None:
    """issue #404：拿掉 `--permission-mode plan` 後，claude 呼叫若不做任何
    額外隔離，會直接繼承 operator `~/.claude`（superpowers plugin、記憶
    hooks、user 層 CLAUDE.md、user MCP servers 全部注入），讓 planning 呼叫
    的模型輸出摻雜與本次規劃無關的敘事。改為在本次呼叫專用的 tempdir 下
    建一個一次性 hermetic config 目錄，只播種登入所需的 credentials，藉此
    同時隔離上述注入項，但不影響登入態。

    查無登入憑證（`~/.claude/.credentials.json` 不存在）時不代為猜測——
    回傳 ``None``，維持不設 `CLAUDE_CONFIG_DIR`，讓 claude CLI 依原生行為
    自行回報 not logged in。`--bare` 與空的 `CLAUDE_CONFIG_DIR` 都會直接
    弄丟登入態（issue 404 實測矩陣已驗證不可用），因此缺檔時不得改用空
    目錄頂替，只能整組跳過。
    """

    source_credentials = Path.home() / ".claude" / ".credentials.json"
    if not source_credentials.is_file():
        return None
    config_dir = Path(temp_dir) / "claude-config"
    config_dir.mkdir()
    config_dir.chmod(0o700)
    destination_credentials = config_dir / ".credentials.json"
    shutil.copy2(source_credentials, destination_credentials)
    destination_credentials.chmod(0o600)
    return {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}


# ---------------------------------------------------------------------------
# issue #683（#672 票 B）：`PlanningInvoker` 抽象
#
# 修法前 `_invoke_json` 把三件事揉在一起：**怎麼跑一個 executor**（sandbox、
# cwd、env、逾時、雙向快照、drift 收容）、**跑之前準備什麼**（prompt）、
# **跑完之後怎麼解讀**（JSON 抽取）。票 E（#686）要把第一件事換成降權 job，
# 而第二／第三件事逐字不變——沒有接縫時那次改動會落在同一支函式裡，變成
# `if degraded:` 分岔，於是「哪幾條防線在哪個模式下生效」只能靠讀 `if` 才知道
# （design D1）。
#
# 切面因此切在「拿 identity ＋ prompt，回傳 stdout／stderr／rc」這一層：
#
#   進 invoker（＝**怎麼跑**）：一次性 sandbox、`cwd`、hermetic claude env、
#     逾時、以及**執行前後的雙向快照與 drift 收容**——後者看似屬於呼叫端，
#     但它們的作用對象（sandbox 與 operator 樹的關係）完全由「怎麼跑」決定：
#     job 模式下 operator 樹由 `ProtectSystem=strict` 在 kernel 層擋掉（D-e
#     從偵測升級為阻擋），那時這幾條就**不該**再由呼叫端執行一次。
#   留呼叫端（＝**跑之前跑之後**）：prompt 組裝、rc 判定、JSON 抽取與 envelope
#     處理（design D1 明文要求兩個 invoker 吃同一份）。
#
# 本票**只**新增接縫，不新增第二個實作、不改任何行為。
# ---------------------------------------------------------------------------

#: `PlanningInvocation.purpose` 的四個 production 值（design D9）。只進 instance
#: 名與診斷，**不參與任何決策**——票 E 用它讓
#: `systemctl list-units 'cortex-reviewer-job@*'` 的輸出直接說得出「這一批 job
#: 在做什麼」，不必回頭查 spec。
PLANNING_PURPOSE_PROBE = "probe"
PLANNING_PURPOSE_QUESTIONER = "questioner"
PLANNING_PURPOSE_SECONDARY = "secondary"
PLANNING_PURPOSE_INTEGRATOR = "integrator"
#: 直呼叫（測試、診斷）的預設。production 的四條路徑一律明示 purpose。
PLANNING_PURPOSE_UNSPECIFIED = "planning"


@dataclass(frozen=True)
class PlanningInvocation:
    """一次 planning 模型呼叫的完整輸入（design D1）。

    `evidence_root`／`run_id` 對 in-process 是 drift 報告的落點，對票 E 的 job
    invoker 則是 instance 命名的來源（`plan-<run_id 前 12 字>-<purpose>-<序號>`）
    ——兩邊都需要，因此屬於 invocation 而非 invoker 的建構參數。
    """

    identity: ModelIdentity
    prompt: str
    purpose: str
    timeout_seconds: int
    worktree: Path
    evidence_root: str | Path | None = None
    run_id: str = "ephemeral"


@dataclass(frozen=True)
class PlanningOutcome:
    """一次呼叫的原始結果——**不含任何解讀**。

    `output_text` 是 codex `-o last.json` 這個第二輸出候選的內容（無則 ``None``）。
    它必須在這裡被讀出來，因為 in-process 的落點在 invoker 自己的一次性 tempdir
    裡，invoker 一返回就消失；job 模式下 Manager 讀不到 job-owned scratch，這一格
    恆為 ``None``（design D-j 的 R-2 退步）。

    `diagnostics` 本票不產出任何內容（direct 模式沒有第二個資訊來源）；票 E 的
    D8 會在這裡放 `unit`／`hardening_profile`／`resolved_binary`，讓
    `executor-silent-exit` 這種「連錯誤訊息都沒有」的失敗有方向可查。
    """

    returncode: int | None
    stdout: str | None
    stderr: str | None = None
    output_text: str | None = None
    diagnostics: Mapping[str, str] = field(default_factory=dict)


class PlanningInvoker(Protocol):
    """planning 的執行後端。票 E 換掉的就是這個介面的實作。"""

    def run(self, invocation: PlanningInvocation) -> PlanningOutcome:
        """跑一次 identity＋prompt 呼叫，回傳原始結果。

        契約：executor 非零退出**不是**本方法的錯誤（交呼叫端判定）；但
        「執行過程違反了隔離契約」（弄髒拋棄式 sandbox、動到 operator 樹）
        MUST 由本方法 fail-closed 拋出——那是 invoker 才有的視角。
        """

    def capability_probe_runner(self) -> Callable[..., object]:
        """`probe_agy_capability` 兩次裸 CLI 呼叫用的執行接縫。

        為什麼不是 `run()`：agy 的能力探測不是「一個 prompt」，而是一段兩步
        CLI 協定（`agy models` 列出 model id，再拿解析到的 token 跑 smoke）。
        那段協定的真相在 `model_identities.probe_agy_capability`，把它複製一份
        到這裡就是第二份真相。因此 invoker 交出一個 `ProcessRunner` 形狀的
        callable，讓既有 probe 原樣使用——**兩次 CLI 呼叫各算一次 invocation**
        （plan 票 B）。票 E 在這裡回傳的是「一個 argv → 一個降權 job」的閉包。
        """


class InProcessPlanningInvoker:
    """在呼叫端行程內執行（＝**現行行為**，direct 模式）。

    design D2 的十條防線在 direct 模式下全部由本類別保證：D-a 一次性 tempdir、
    D-b sandbox 複本、D-c `cwd=sandbox`、D-d sandbox 弄髒即 fail-closed、
    D-e operator 樹雙向快照、D-f drift 唯讀收容、D-g claude hermetic env、
    D-h `subprocess` 逾時、D-i `capture_output`、D-j codex 第二輸出候選。

    本檔唯一持有 `subprocess.run` 的地方（issue #683 驗收第二條）。
    """

    def __init__(self, runner: Callable[..., object] | None = None) -> None:
        self._runner = runner if runner is not None else subprocess.run

    def capability_probe_runner(self) -> Callable[..., object]:
        # direct 模式下「一次 agy CLI 呼叫」就是「一次 process 呼叫」，因此
        # 接縫退化成底層 runner 本身——行為與修法前逐字相同（本票是純重構）。
        # 注意 `probe_agy_capability` 刻意**不**帶 cwd／sandbox：那是它繞過
        # `_invoke_json` 全部防線的既有事實（#672 母票已記錄），本票不改變它，
        # 只讓它從此有一個能被票 E 換掉的接縫。
        return self._runner

    def run(self, invocation: PlanningInvocation) -> PlanningOutcome:
        identity = invocation.identity
        worktree = invocation.worktree
        run_id = invocation.run_id
        evidence_root = invocation.evidence_root
        operator_before = _tree_snapshot(worktree)
        with tempfile.TemporaryDirectory(prefix="cortex-planning-") as temp_dir:
            baseline = Path(temp_dir) / "baseline"
            sandbox = Path(temp_dir) / "checkout"
            _copy_planning_sandbox(worktree, baseline)
            shutil.copytree(baseline, sandbox, symlinks=True)
            sandbox_before = _tree_snapshot(sandbox)
            output_path = Path(temp_dir) / "last.json"
            argv = _planning_argv(identity, invocation.prompt, temp_dir, sandbox)
            run_kwargs: dict[str, object] = {}
            if identity.executor == "claude":
                # 僅 claude 路徑帶 env 覆寫；其他 executor（codex/agy）維持不帶，
                # 避免行為外溢。
                env = _seed_hermetic_claude_env(temp_dir)
                if env is not None:
                    run_kwargs["env"] = env
            failure: BaseException | None = None
            outcome: PlanningOutcome | None = None
            try:
                raw = self._runner(
                    argv,
                    cwd=str(sandbox),
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=invocation.timeout_seconds,
                    **run_kwargs,
                )
                returncode = getattr(raw, "returncode", None)
                stdout = getattr(raw, "stdout", None)
                # 第二候選必須在 tempdir 消失之前讀出來（見 `PlanningOutcome`）。
                # 讀取條件與修法前逐字相同：修法前 `_extract_json` 只在
                # 「rc==0 且 stdout 是字串」時才被呼叫，因此 `last.json`
                # 在失敗路徑上**從不被讀**。無條件讀會讓「檔案存在但讀不出來」
                # （非 UTF-8、權限）在失敗路徑上換掉例外型別，而例外型別正是
                # `_probe_identity` 的 `safe-probe-failed` diagnostic 唯一內容、
                # 也是票 A `classify_probe_failure()` 的分類輸入。
                output_text: str | None = None
                if returncode == 0 and isinstance(stdout, str) and output_path.is_file():
                    output_text = output_path.read_text(encoding="utf-8")
                outcome = PlanningOutcome(
                    returncode=returncode,
                    stdout=stdout,
                    stderr=getattr(raw, "stderr", None),
                    output_text=output_text,
                )
            except BaseException as exc:
                failure = exc
            finally:
                try:
                    sandbox_dirty = _tree_snapshot(sandbox) != sandbox_before
                except BaseException:
                    sandbox_dirty = True
                    try:
                        _make_tree_traversable(sandbox)
                    except BaseException:
                        pass
                if sandbox_dirty:
                    failure = ValueError(
                        "planning launcher modified disposable read-only sandbox"
                    )

                operator_dirty = False
                try:
                    operator_dirty = _tree_snapshot(worktree) != operator_before
                except BaseException:
                    operator_dirty = True
                if operator_dirty:
                    # issue #507：偵測到 drift 一律 fail-closed（本次 planning 呼叫
                    # 的結果不可信），但**不得改寫 operator worktree**。
                    # `rollback_scope` 傳空集合是刻意的：launcher 以 `cwd=sandbox`
                    # 執行、`--add-dir` 也只指向 sandbox，這條路徑在 operator 樹裡
                    # 沒有任何「本次 run 自產」的產物可言，因此可證明的還原範圍就是
                    # 空的。planning artifact 的落地另走
                    # `manager._publish_planning_artifacts`（有交易與 authority 把
                    # 關），不在此。任何差異都當成 operator／其他 agent 的並行工作
                    # 保留原地，只做備份與報告。
                    try:
                        summary = _contain_operator_drift(
                            worktree,
                            baseline,
                            evidence_root=(
                                Path(evidence_root) if evidence_root is not None else None
                            ),
                            run_id=run_id,
                            rollback_scope=frozenset(),
                        )
                    except BaseException as exc:  # noqa: BLE001 - 診斷面 fail-open
                        # drift 收斂只負責診斷；它自己壞掉時仍要拋出可辨識的
                        # planning 失敗，不得換成一個與現場無關的例外（修法前那條
                        # 「restore failed」出口就是這個反例）。
                        logger.error(
                            "planning-worktree-drift-containment-failed run_id=%s error=%s: %s",
                            run_id,
                            type(exc).__name__,
                            str(exc)[:200],
                        )
                        summary = {"counts": {}, "report_path": None, "backup_root": None}
                    message = _operator_drift_message(summary)
                    logger.error("planning-worktree-drift run_id=%s %s", run_id, message)
                    failure = ValueError(message)
            if failure is not None:
                raise failure
            if outcome is None:  # pragma: no cover - failure 為 None ⇒ 必已賦值
                raise ValueError("planning invoker produced no outcome")
            return outcome


def _select_planning_invoker(env: Mapping[str, str]) -> PlanningInvoker:
    """依部署宣告挑執行後端。**選擇點只有這一個**（design D1）。

    唯一輸入是 `PSC_JOB_RUNNER`，與 launcher 共用同一支解析函式——非法值在該
    函式已 fail-closed，這裡不重寫一份判定。刻意**不**新增
    `PSC_PLANNING_INVOKER` 之類的 planning 專屬開關：第二個開關的失效模式是
    「以為降權了、其實沒有」，而那種失敗看起來是成功的。

    issue #686（票 E）接上 `JobPlanningInvoker`：`systemd-template` ⇒ 降權 job。

    **`systemd-run`（A 案）刻意 fail-closed，不退回 in-process。** A 案下 Manager 自己
    組 `--property=`／`--setenv=`，「身分與加固面只由 root-owned unit 決定」這條性質
    不成立——而那正是 planning 這個**吃 untrusted issue 內容**的角色最需要的性質。
    退回 in-process 的失敗會**看起來像成功**（planning 照跑、產出正常），那正是 #672
    整張票要消除的失效模式；因此這裡寧可讓部署當場以可讀理由停下來。
    """

    mode = job_runner.resolve_runner_mode(env)
    if mode == job_runner.RUNNER_DIRECT:
        return InProcessPlanningInvoker()
    if mode == job_runner.RUNNER_SYSTEMD_TEMPLATE:
        from .planning_job import JobPlanningInvoker

        return JobPlanningInvoker(env=env)
    raise ValueError(
        f"{job_runner.JOB_RUNNER_ENV}={mode} 尚未支援 planning 降權"
        "（#686 只實作模板模式；A 案下加固面由呼叫端而非 root-owned unit 決定，"
        "planning 不走它）。改用 systemd-template，或以 direct 明示不降權——"
        "**本函式不會靜默退回行程內執行**。"
    )


def _invoke_json(
    identity: ModelIdentity,
    prompt: str,
    *,
    worktree: Path,
    runner: Callable[..., object] | None = None,
    invoker: PlanningInvoker | None = None,
    purpose: str = PLANNING_PURPOSE_UNSPECIFIED,
    timeout_seconds: int,
    evidence_root: str | Path | None = None,
    run_id: str = "ephemeral",
) -> object:
    """呼叫端這一半：組 invocation → 交 invoker → 判 rc → 抽 JSON。

    `runner` 與 `invoker` 互斥。前者是既有呼叫端（與大量既有測試）注入 process
    runner 的方式，等同「用 in-process invoker 跑」；兩者同時給等於同一件事有
    兩份真相，fail-closed。
    """

    if invoker is None:
        invoker = InProcessPlanningInvoker(runner)
    elif runner is not None:
        raise ValueError("planning invoker and runner are mutually exclusive")
    outcome = invoker.run(
        PlanningInvocation(
            identity=identity,
            prompt=prompt,
            purpose=purpose,
            timeout_seconds=timeout_seconds,
            worktree=Path(worktree),
            evidence_root=evidence_root,
            run_id=run_id,
        )
    )
    if outcome.returncode != 0 or not isinstance(outcome.stdout, str):
        raise ValueError(
            f"planning launcher failed: {identity.executor}/{identity.model_id}"
        )
    return _extract_json_candidates(outcome.stdout, outcome.output_text)


def _probe_failure(identity: ModelIdentity, exc: BaseException) -> CapabilityProbe:
    """一次失敗的 probe → `CapabilityProbe`，**族名活著抵達拒因表**。

    #683 之前（也就是 direct 模式今天仍走的那條路）只留得下 `type(exc).__name__`，
    而票 A 的 `classify_probe_failure()` 就是靠那個型別名把失敗分成三族。job 模式有
    **更好的資訊**：`PlanningJobError` 自己就帶著族名（job 起不來 vs executor 死），
    那是型別名推不出來的區分。

    `_PROBE_REASON_FAMILIES` 已經預留了這條路（票 A：「票 C／票 E 之後 probe 自己就會
    回族名，這裡讓它原樣通過」），因此這裡把族名放進 `reason`、把 detail 放進
    `diagnostic`，分類器原樣採用，不需要第二張表。

    direct 模式逐字不變——那條路上不會出現 `PlanningJobError`。
    """

    from .planning_job import PlanningJobError

    if isinstance(exc, PlanningJobError):
        return CapabilityProbe(
            False,
            identity.executor,
            identity.model_id,
            identity.independence_domain,
            exc.family,
            exc.detail,
        )
    return CapabilityProbe(
        False,
        identity.executor,
        identity.model_id,
        identity.independence_domain,
        "safe-probe-failed",
        type(exc).__name__,
    )


def _probe_identity(
    identity: ModelIdentity,
    *,
    worktree: Path,
    invoker: PlanningInvoker,
    timeout_seconds: int,
    evidence_root: str | Path | None = None,
    run_id: str = "ephemeral",
) -> CapabilityProbe:
    expected = {
        "capability": "cortex-planning-json",
        "executor": identity.executor,
        "model": identity.model_id,
    }
    prompt = "Return only this JSON object and do not call tools: " + json.dumps(
        expected, ensure_ascii=False, separators=(",", ":")
    )
    try:
        value = _invoke_json(
            identity,
            prompt,
            worktree=worktree,
            invoker=invoker,
            purpose=PLANNING_PURPOSE_PROBE,
            timeout_seconds=timeout_seconds,
            evidence_root=evidence_root,
            run_id=run_id,
        )
    except Exception as exc:
        return _probe_failure(identity, exc)
    if value != expected:
        return CapabilityProbe(
            False,
            identity.executor,
            identity.model_id,
            identity.independence_domain,
            "identity-mismatch",
        )
    return CapabilityProbe.ready_for(
        identity.executor, identity.model_id, identity.independence_domain
    )


def _planning_source_material(
    pack: Mapping[str, object], *, root: Path, max_bytes: int = 262_144
) -> dict[str, str]:
    questions = pack.get("questions")
    if not isinstance(questions, list):
        raise ValueError("planning question pack has no questions")
    refs = sorted(
        {
            ref
            for question in questions
            if isinstance(question, dict)
            for ref in question.get("source_refs", [])
            if isinstance(ref, str)
        }
    )
    material: dict[str, str] = {}
    total = 0
    for ref in refs:
        pure = PurePosixPath(ref)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != ref:
            raise ValueError("planning source ref is not canonical repo-relative")
        current = root
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("planning source ref traverses symlink")
        try:
            target = current.resolve(strict=True)
            target.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("planning source ref is unavailable") from exc
        if not target.is_file():
            raise ValueError("planning source ref is not a file")
        try:
            body = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError("planning source ref is unreadable") from exc
        total += len(body.encode("utf-8"))
        if total > max_bytes:
            raise ValueError("planning source material exceeds bounded context")
        material[ref] = body
    return material


def _planning_destinations(pack: Mapping[str, object]) -> dict[str, str]:
    questions = pack.get("questions")
    if not isinstance(questions, list):
        return {}
    slugs = {
        parts[2]
        for question in questions if isinstance(question, dict)
        for ref in question.get("source_refs", [])
        if isinstance(ref, str)
        and (parts := PurePosixPath(ref).parts)[:2] == ("openspec", "changes")
        and len(parts) >= 4
    }
    if not slugs:
        # #408：small-fix 等無 openspec-propose 卡的 combo，work item 錨點是
        # workstream todo（docs/superpowers/workstreams/<slug>/todo.md）——
        # 沒有這個 fallback 時 destinations 恆為空，integrator 被要求
        # 「Use the supplied destination paths」卻拿到空 dict，只能自行發明
        # 路徑、必被 _publish_planning_artifacts 的 governed-roots 驗證拒收。
        # openspec 錨點優先；兩者皆無或歧義（多 slug）維持空 dict 的
        # 既有 fail-closed 行為。
        slugs = {
            parts[3]
            for question in questions if isinstance(question, dict)
            for ref in question.get("source_refs", [])
            if isinstance(ref, str)
            and (parts := PurePosixPath(ref).parts)[:3]
            == ("docs", "superpowers", "workstreams")
            and len(parts) >= 5
            and parts[4] == "todo.md"
        }
    if len(slugs) != 1:
        return {}
    slug = next(iter(slugs))
    return {
        "spec": f"docs/superpowers/specs/{slug}-spec.md",
        "design": f"docs/superpowers/specs/{slug}-design.md",
        "plan": f"docs/superpowers/plans/{slug}.md",
    }


def build_production_planning_runtime(
    *,
    primary: tuple[str, str],
    worktree: str | Path,
    runner: Callable[..., object] | None = None,
    invoker: PlanningInvoker | None = None,
    timeout_seconds: int = 120,
    evidence_root: str | Path | None = None,
    run_id: str = "ephemeral",
    probe_cache_path: str | Path | None = None,
) -> ProductionPlanningRuntime:
    """Build the daemon's real, safe, heterogeneous planning adapters.

    issue #507：`evidence_root`／`run_id` 供 operator worktree drift 的備份與
    報告落地（`<evidence_root>/evidence/planning-worktree-drift/<run_id>-<digest>/`）。
    未帶入時（直呼叫、探測、測試）**不寫任何 evidence**——刻意不 fallback 到
    `paths.coordinator_root()`，避免非 daemon 的呼叫端在 operator 的執行期狀態
    目錄下留下非預期檔案。

    issue #683：執行後端改由 `PlanningInvoker` 承載。三種注入方式的優先序
    （由高到低、互斥）：

    1. `invoker=`——直接指定後端（票 E 的 `JobPlanningInvoker` 與測試用）。
    2. `runner=`——注入 process runner，等同「用 in-process invoker 跑」。
       這是既有呼叫端與大量既有測試的用法，逐字保留。
    3. 兩者皆無（＝daemon 的實際呼叫形態）——由 `_select_planning_invoker`
       依 `PSC_JOB_RUNNER` 決定。**這是全庫唯一的執行後端選擇點。**

    四個 adapter 與兩種 probe 全部走同一個 invoker——票 E 因此只換一個物件，
    不必再碰任何呼叫端。

    issue #684（票 C）：probe 結果跨 tick 快取。`probe_cache_path` 未給時落在
    `<coordinator_root>/planning-probe-cache.json`（登記表資產
    `planning-probe-cache`，Manager-owned 0600）。快取層**永不 raise**：讀不回來
    ／指紋不符／TTL 過期一律視為 miss 重探，因此本函式的失敗語意與 #684 之前逐字
    相同（見 `planning_probe_cache` 的模組 docstring）。
    """

    if invoker is not None and runner is not None:
        raise ValueError("planning invoker and runner are mutually exclusive")
    if invoker is None:
        invoker = (
            InProcessPlanningInvoker(runner)
            if runner is not None
            else _select_planning_invoker(os.environ)
        )
    root = Path(worktree).resolve()
    registry = load_model_identities()
    cache = planning_probe_cache.ProbeCache.open(
        probe_cache_path
        if probe_cache_path is not None
        else planning_probe_cache.cache_path(paths.coordinator_root())
    )
    roster = planning_probe_cache.roster_digest(registry)
    probes: dict[tuple[str, str], CapabilityProbe] = {}
    planning_keys: list[tuple[str, str]] = []
    for identity in registry.identities:
        if "planning" not in identity.capabilities:
            continue
        key = (identity.executor, identity.model_id)
        planning_keys.append(key)
        # #684：指紋是「這一格的 probe 前提」——`PSC_JOB_RUNNER`、PATH 解析到的
        # executor 絕對路徑與 stat、憑證檔 stat、加固剖面、**模板 unit 檔本身**的
        # stat、roster 摘要。任何一格變了就重探；算不出來的那一格落
        # `<unresolved:…>`，同樣是一個會變的值。
        fingerprint = planning_probe_cache.compute_fingerprint(identity, roster=roster)
        cached = cache.get(identity, fingerprint=fingerprint)
        if cached is not None:
            probes[key] = cached
            continue
        if identity.executor == "agy" and identity.model_id == AGY_MODEL_ID:
            # agy 的能力探測是兩步 CLI 協定，不是一個 prompt——它拿的是 invoker
            # 的 `capability_probe_runner()` 接縫（見該方法的 docstring），
            # 而不再是呼叫端手上的裸 runner。
            probe = probe_agy_capability(
                runner=invoker.capability_probe_runner(),
                timeout_seconds=min(timeout_seconds, 45),
            )
        else:
            probe = _probe_identity(
                identity,
                worktree=root,
                invoker=invoker,
                timeout_seconds=timeout_seconds,
                evidence_root=evidence_root,
                run_id=run_id,
            )
        probes[key] = probe
        cache.put(identity, probe, fingerprint=fingerprint)
    # roster 不再含有的 identity 自動清除（`not_claimable.clear()` 的同型）：
    # 沒有消費者的探測結果留著只會讓 operator 對一筆永不更新的紀錄猜。
    cache.flush(keep=planning_keys)

    primary_identity = registry.get(*primary)

    def invoke_primary(prompt: str, *, purpose: str) -> object:
        if primary_identity is None:
            raise ValueError("primary planning identity is not configured")
        return _invoke_json(
            primary_identity,
            prompt,
            worktree=root,
            invoker=invoker,
            purpose=purpose,
            timeout_seconds=timeout_seconds,
            evidence_root=evidence_root,
            run_id=run_id,
        )

    def questioner(report: Mapping[str, object]) -> object:
        return invoke_primary(
            "Return only the exact question-pack JSON required to resolve this completeness report. "
            + _JSON_OUTPUT_CONTRACT
            + " Input: "
            + json.dumps(report, ensure_ascii=False, sort_keys=True),
            purpose=PLANNING_PURPOSE_QUESTIONER,
        )

    def secondary(pack: Mapping[str, object], identity: ModelIdentity) -> object:
        source_material = _planning_source_material(pack, root=root)
        return _invoke_json(
            identity,
            "Do not call tools, run commands, make decisions, or edit files. Use only the supplied "
            "source material. Return exactly one JSON object with keys schema_version=1, "
            "question_pack_id, and evidence. Evidence must contain every question exactly once; "
            "each row has only question_id, non-empty claims string list, and non-empty source_refs "
            "string list naming supplied sources. " + _JSON_OUTPUT_CONTRACT + " Input: "
            + json.dumps(
                {"question_pack": pack, "source_material": source_material},
                ensure_ascii=False,
                sort_keys=True,
            ),
            worktree=root,
            invoker=invoker,
            purpose=PLANNING_PURPOSE_SECONDARY,
            timeout_seconds=timeout_seconds,
            evidence_root=evidence_root,
            run_id=run_id,
        )

    def integrator(pack: Mapping[str, object], evidence: Mapping[str, object]) -> object:
        # #406：prompt 必須把 validate_primary_integration 的結構約束逐條講給模型，
        # 只列欄位名（不給語意）時模型會把不確定的 artifact_refs 留空 → 必然驗證失敗。
        # #516：同一教訓的第二輪，這次補的是 question_pack_id 與 secondary_evidence_hash
        # 兩個 echo-back 欄位——兩個值都已在輸入裡（question_pack.pack_id、
        # secondary_evidence.evidence_hash），模型只需原樣複製；但輸入欄位名
        # （evidence_hash）與輸出欄位名（secondary_evidence_hash）不同，後者字面上
        # 像是要模型自己算 hash，只列欄位名時會反覆撞 evidence hash mismatch。
        # #520：第三輪，這次是必要標題。舊句「required headings: Requirements for
        # spec, ...」字面上可讀成「標題就是 `Requirements for spec`」，模型照抄後必然
        # 撞 required-section-missing。標題要求現改由 `planning.required_heading_hint()`
        # 依驗收判準（`_ACCEPTED_HEADINGS` / `_REQUIRED_HEADINGS`）機械產生——prompt 端
        # 不再持有第二份真實來源，判準改動會自動同步到 prompt。
        return invoke_primary(
            "Do not call tools or edit files. Integrate only the supplied evidence. Return exactly one "
            "JSON object with schema_version=1, question_pack_id, secondary_evidence_hash, resolutions, "
            "and artifacts. question_pack_id must be copied verbatim from the input question_pack.pack_id "
            "value. secondary_evidence_hash must be copied verbatim from the input "
            "secondary_evidence.evidence_hash field; do not compute, derive, or invent a hash. "
            "Each resolution has only question_id, decision, artifact_kind, artifact_refs. "
            "Resolve every question exactly once. artifact_kind must equal the question kind without its "
            "'missing-' prefix. artifact_refs must be a NON-EMPTY list of the destination path(s) this "
            "resolution's artifact(s) are written to — the same strings used as artifacts[].path. "
            "The set of all artifacts[].path values must equal the union of all artifact_refs. "
            "Each artifact has only kind, path, content; content must be complete UTF-8 Markdown with "
            "frontmatter status: accepted and the matching work_item. "
            + required_heading_hint()
            + " Use the supplied destination paths. "
            + _JSON_OUTPUT_CONTRACT + " Input: "
            + json.dumps(
                {
                    "question_pack": pack,
                    "secondary_evidence": evidence,
                    "destinations": _planning_destinations(pack),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            purpose=PLANNING_PURPOSE_INTEGRATOR,
        )

    return ProductionPlanningRuntime(registry, probes, questioner, secondary, integrator)
