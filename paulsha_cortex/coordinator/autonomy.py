from __future__ import annotations

import os
import inspect
import tempfile
from pathlib import Path
from typing import Callable

from paulsha_cortex.config import paths

from .._yaml import YAMLError, safe_load
from . import completion
from .contract_command import build_dispatch_prompt
from .diagnostics import DiagnosticReason, diagnostic_reason
from .dispatcher import _default_git_runner
from .launcher import AgentLauncher, LaunchHandle
from .model_identities import load_model_identities
from .spawn_admission import SpawnAdmissionLimiter, resolve_limiter, resolve_provider
from . import verification

# is_satisfied predicate 型別：收 slice_id，回該相依是否「已滿足」（可釋放下游）。
# 判定來源由呼叫者決定（merged-to-main vs handoff gate_status）——#104 留開放。
IsSatisfied = Callable[[str], bool]

# Dispatcher duck-type：只需有 dispatch(task, persona, pane_id, command) -> dict（Phase 2 介面）。
DEFAULT_HANDOFF_DIR = "runtime/handoff"


class DispatchReadyError(RuntimeError):
    _MAX_MESSAGE_LENGTH = 160

    @staticmethod
    def _compact_message(exc: Exception) -> str:
        raw = str(exc)
        if len(raw) > DispatchReadyError._MAX_MESSAGE_LENGTH:
            return f"{raw[: DispatchReadyError._MAX_MESSAGE_LENGTH - 3]}..."
        return raw

    def __init__(self, errors: list[tuple[str, Exception]], jobs: list[dict]) -> None:
        self.errors = tuple(errors)
        self.jobs = list(jobs)
        failed = ", ".join(
            f"{slice_id}({exc.__class__.__name__}: {self._compact_message(exc)})"
            for slice_id, exc in errors
        )
        super().__init__(f"dispatch_ready failed for slice(s): {failed}")


# --------------------------------------------------------------------------- #
# 1) frontmatter 解析（預設 HOLD）
# --------------------------------------------------------------------------- #
def _split_frontmatter(text: str) -> str | None:
    """回 frontmatter 區塊原文；無合法 frontmatter（不以 --- 起頭/無收尾 ---）→ None。"""
    if not text.startswith("---"):
        return None
    # 首行 --- 之後找下一個單獨成行的 ---
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None  # 無收尾 ---


def parse_spec_frontmatter(path) -> dict:
    """解析 superpowers spec 開頭 --- frontmatter。

    回 {path, dispatch, slice_id, plan, depends_on, target_branch, verification,
    executor, model_id, repo, parse_error}。
    硬約束：dispatch 僅在字面值為 'auto' 時為 'auto'，其餘一律 'hold'（fail-safe）。
    容忍無 frontmatter（視為 hold），不 raise。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    block = _split_frontmatter(text)

    meta: dict = {
        "path": str(p),
        "dispatch": "hold",
        "slice_id": None,
        "plan": None,
        "depends_on": [],
        "target_branch": None,
        "verification": None,
        "executor": None,
        "model_id": None,
        "repo": None,
        "parse_error": None,
    }
    if block is None:
        return meta

    try:
        data = safe_load(block)
    except YAMLError as exc:
        meta["parse_error"] = {
            "code": "invalid-frontmatter",
            "field": "frontmatter",
            "message": str(exc),
        }
        return meta
    if not isinstance(data, dict):
        meta["parse_error"] = {
            "code": "invalid-frontmatter",
            "field": "frontmatter",
            "message": "frontmatter must be a mapping",
        }
        return meta
    try:
        return _normalize_frontmatter(p, data)
    except verification.ContractValidationError as exc:
        meta["slice_id"] = data.get("slice_id") if isinstance(data.get("slice_id"), str) else None
        meta["plan"] = data.get("plan") if isinstance(data.get("plan"), str) else None
        meta["depends_on"] = _normalize_depends_on(data.get("depends_on"))
        meta["target_branch"] = (
            data.get("target_branch") if isinstance(data.get("target_branch"), str) else None
        )
        meta["executor"] = data.get("executor") if isinstance(data.get("executor"), str) else None
        meta["model_id"] = data.get("model_id") if isinstance(data.get("model_id"), str) else None
        meta["repo"] = data.get("repo") if isinstance(data.get("repo"), str) else None
        meta["parse_error"] = exc.as_payload()
        return meta
    except RepoRootResolutionError as exc:
        # #612：spec 路徑推不出 repo 根（相對路徑／無 git 根且未宣告
        # PSC_REPO_ROOT）。掃描不該因此炸掉整輪，但這份 spec 也**不得**被派工——
        # 落成 parse_error，`dispatch` 就永遠停在 hold，理由則由 DiagnosticReason
        # 帶著走。
        meta["slice_id"] = data.get("slice_id") if isinstance(data.get("slice_id"), str) else None
        meta["parse_error"] = {
            "code": exc.diagnostic.reason,
            "field": "path",
            "message": exc.diagnostic.detail,
        }
        return meta


def _normalize_frontmatter(path: Path, data: dict) -> dict:
    allowed = {
        "dispatch",
        "slice_id",
        "plan",
        "depends_on",
        "target_branch",
        "verification",
        "executor",
        "model_id",
        "repo",
        "parse_error",
    }
    extras = set(data) - allowed
    if extras:
        extra = sorted(extras)[0]
        raise verification.ContractValidationError(extra, f"unknown frontmatter key: {extra}")

    dispatch = "auto" if data.get("dispatch") == "auto" else "hold"
    repo_root = _infer_repo_root(path)
    meta: dict = {
        "path": str(path),
        "dispatch": dispatch,
        "slice_id": data.get("slice_id") if isinstance(data.get("slice_id"), str) else None,
        "plan": None,
        "depends_on": _normalize_depends_on(data.get("depends_on")),
        "target_branch": None,
        "verification": None,
        "executor": None,
        "model_id": None,
        "repo": None,
        "parse_error": None,
    }
    plan = data.get("plan")
    if isinstance(plan, str) and plan.strip():
        meta["plan"] = verification.normalize_repo_relative_path(
            plan,
            repo_root=repo_root,
            field="plan",
        )
    elif dispatch == "auto":
        raise verification.ContractValidationError("plan", "auto dispatch requires a plan path")

    target_branch = data.get("target_branch")
    if target_branch is not None:
        meta["target_branch"] = verification.normalize_non_empty_string(
            target_branch,
            field="target_branch",
        )
    elif dispatch == "auto":
        raise verification.ContractValidationError(
            "target_branch", "auto dispatch requires a target_branch"
        )

    verification_value = data.get("verification")
    if verification_value is not None:
        meta["verification"] = verification.validate_verification_contract(
            verification_value,
            repo_root=repo_root,
            auto_dispatch=(dispatch == "auto"),
        )
    elif dispatch == "auto":
        raise verification.ContractValidationError(
            "verification", "auto dispatch requires a verification contract"
        )
    has_executor = data.get("executor") is not None
    has_model_id = data.get("model_id") is not None
    if has_executor != has_model_id:
        missing = "model_id" if has_executor else "executor"
        counterpart = "executor" if missing == "model_id" else "model_id"
        raise verification.ContractValidationError(
            missing,
            f"{missing} must be declared together with {counterpart}",
        )
    if has_executor:
        meta["executor"] = verification.normalize_non_empty_string(
            data.get("executor"),
            field="executor",
        )
        meta["model_id"] = verification.normalize_non_empty_string(
            data.get("model_id"),
            field="model_id",
        )
    # #469：optional 顯式 repo 歸屬宣告（owner/repo）。派工時投影進 builder job
    # 的 workflow_repo，終局 manifest / recent_done / slices 才有 repo 歸屬。
    # 未宣告 → None：依 #230/#349 契約不從路徑或 git remote 推斷。
    # shape 比照 completion.py 的 work_authority.repo：恰一個 '/' 且兩段非空。
    repo_value = data.get("repo")
    if repo_value is not None:
        if (
            not isinstance(repo_value, str)
            or repo_value.count("/") != 1
            or not all(repo_value.split("/"))
        ):
            raise verification.ContractValidationError(
                "repo", "repo must be an explicit owner/repo string"
            )
        meta["repo"] = repo_value
    if data.get("parse_error") is not None:
        raise verification.ContractValidationError(
            "parse_error",
            "parse_error is runtime-owned and must be null when present",
        )
    return meta


def _normalize_depends_on(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _is_git_repo_root(candidate: Path) -> bool:
    """`<candidate>/.git` 是否為**有效** git repo 標記（#565）。

    原判準是 `(candidate / ".git").exists()`，把「存在」等同「是 repo」。實測
    （#565）agent sandbox 基礎設施會在 `/tmp` **暫態**建立一個 `mkdir` 出來的
    **空 `.git` 目錄**（sandbox 存活期間存在、teardown 消失），於是任何 `/tmp`
    底下的 spec 路徑在向上搜尋時都會停在 `/tmp`，repo 根被全域劫持成 `/tmp`。

    真 repo 的兩種合法形狀都能不開 subprocess 就分辨：
    - 正規 repo：`.git` 是目錄且必含 `HEAD`（`git init` 寫的第一批檔案之一）；
    - linked worktree／submodule：`.git` 是內含 `gitdir: <path>` 的檔案。

    刻意不呼叫 `git rev-parse --git-dir`：`_infer_repo_root` 落在派工熱路徑上，
    對每個 parent 都 fork 一次 git 的代價與 flakiness 都不划算，而 `HEAD`／
    `gitdir:` 這兩個檔案級判準已足以排除「空目錄」這唯一實測到的偽陽性。
    """
    git_path = candidate / ".git"
    if git_path.is_dir():
        return (git_path / "HEAD").exists()
    if git_path.is_file():
        try:
            head = git_path.read_bytes()[:8]
        except OSError:
            return False
        return head.startswith(b"gitdir:")
    return False


def _repo_search_boundaries() -> frozenset[Path]:
    """向上搜尋 repo 根時的上界——共享暫存根（#565）。

    `/tmp`（與 `TMPDIR` 指到的任何目錄）是全機器互不相干的行程共用的，誰都能
    在其下留下 `.git`。共享根本身永遠不是任何 spec 的 repo 根，因此搜尋到這裡
    就停：`/tmp/<something>/repo` 這種真 repo 仍照常命中（它在上界**之下**），
    但 `/tmp` 自己與其之上不再是候選。

    另外開一個函式（而非 inline 常數）是為了讓測試能 monkeypatch 出一個 tmp_path
    內的假共享根，驗證上界語意時不必依賴 host `/tmp` 的實際狀態。
    """
    roots: set[Path] = set()
    for raw in (tempfile.gettempdir(), "/tmp", "/var/tmp"):
        try:
            roots.add(Path(raw).resolve())
        except OSError:  # pragma: no cover - resolve 對不存在路徑不拋，僅防禦
            continue
    return frozenset(roots)


class RepoRootResolutionError(ValueError):
    """spec 路徑推不出 repo 根，且拒絕退回 cwd（#612）。

    繼承 `ValueError` 是為了沿用既有的處置面：`complete_tick` 的 per-job
    `except Exception`、`work` action 的 `ValueError` 出口、daemon 的 tick
    isolation（#246）都已經接得住，因此本例外只改變「打在錯的樹上」這件事，
    不改變任何呼叫端原本的錯誤處置形狀。

    `diagnostic` 是 #570／#527 的 :class:`DiagnosticReason`：呼叫端要落 evidence
    或推 `needs_human` 時直接取用，不必從字串反推理由。
    """

    def __init__(self, diagnostic: DiagnosticReason) -> None:
        self.diagnostic = diagnostic
        super().__init__(f"{diagnostic.reason}: {diagnostic.detail}")


def _infer_repo_root(spec_path: Path) -> Path:
    """從 spec 路徑解析它所屬的 repo 根——**推不出來就失敗，絕不退回 cwd**（#612）。

    舊實作有兩條靜默的 cwd 通道，兩條都會讓 production 動作打在錯的樹上：

    1. `spec_path.resolve()` 對**相對**路徑是相對 cwd 解析的。daemon 的
       `WorkingDirectory` 正是 operator 的真實 checkout，因此任何從 slice
       spec／config／payload 滲入的相對路徑，都會把 repo 根解析成那個 checkout；
    2. `paths.repo_root()` 未設 `PSC_REPO_ROOT` 時預設 `Path.cwd()`，於是連
       第一段的 `relative_to(configured)` 早退也會命中同一個 checkout。

    #610 實測的後果：`manager.complete_tick → _completion_candidate_ref` 對
    operator 的真實 repo 跑 `git fetch --no-tags origin main`（連帶打真實
    github.com）。fetch 本身良性，但同一條路徑家族還有 `_resolve_target_base_sha`
    的 fetch、`_candidate_ancestry_summary` 的 `rev-parse`／`merge-base`、
    verification／review 的 worktree 操作——只要其中一個有寫入語意就是事故。

    #623 的 Phase 2b 佈局讓這條更緊：Manager unit 帶 `ProtectHome=yes`，repo 源碼
    樹要搬進 Manager-owned 樹，任何「落回 cwd 或 operator checkout」的解析都是
    **無聲的錯誤目標**。因此路徑正規化一律在進件邊界完成：進來的 spec 路徑必須
    是絕對路徑，推不出 repo 根時 fail-closed 並帶 :class:`DiagnosticReason`。
    """
    if not spec_path.is_absolute():
        raise RepoRootResolutionError(
            diagnostic_reason(
                "spec-path-not-absolute",
                "spec 路徑為相對路徑，解析 repo 根會落在當下工作目錄（#612）；"
                "spec 路徑必須在進件邊界正規化成絕對路徑後才能推斷 repo 根",
                source="autonomy._infer_repo_root:relative-spec-path",
                spec_path=str(spec_path),
            )
        )

    configured_raw = paths.configured_repo_root()
    configured = configured_raw.resolve() if configured_raw is not None else None
    resolved_spec = spec_path.resolve()
    if configured is not None:
        try:
            resolved_spec.relative_to(configured)
            return configured
        except ValueError:
            pass

    agents_dir = Path.home() / ".agents"
    boundaries = _repo_search_boundaries()
    for parent in [resolved_spec, *resolved_spec.parents]:
        if parent in boundaries:
            break
        if _is_git_repo_root(parent):
            if parent == agents_dir or parent.name in {".agents", "agents"}:
                continue
            return parent

    if configured is not None:
        return configured
    raise RepoRootResolutionError(
        diagnostic_reason(
            "repo-root-unresolved",
            "spec 路徑向上找不到 git repo 根，且未宣告 PSC_REPO_ROOT（#612）；"
            "拒絕退回 spec 所在目錄／cwd——production 動作必須有顯式的目標 repo",
            source="autonomy._infer_repo_root:unresolved",
            spec_path=str(resolved_spec),
        )
    )


def _resolve_contract_path(path_value: str | None, repo_root: Path) -> Path | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    return (repo_root / path_value).resolve()


def pin_dispatch_inputs(meta: dict, *, target_remote: str | None = None) -> dict:
    precomputed = meta.get("_pinned_inputs")
    if isinstance(precomputed, dict):
        return {
            "spec_path": precomputed["spec_path"],
            "spec_hash": precomputed["spec_hash"],
            "plan_path": precomputed["plan_path"],
            "plan_hash": precomputed["plan_hash"],
            "target_branch": precomputed.get("target_branch") or meta.get("target_branch") or "main",
            "target_remote": verification.normalize_remote_name(
                precomputed.get("target_remote")
                if target_remote is None
                else target_remote
            ),
            "verification_hash": precomputed["verification_hash"],
            "verification": meta.get("verification"),
            "review_policy": (
                meta.get("verification", {}).get("review_policy")
                if isinstance(meta.get("verification"), dict)
                else None
            ),
        }
    raw_spec_path = meta.get("path") or str(Path("specs") / f"{meta.get('slice_id', 'unknown')}.md")
    repo_root = _infer_repo_root(Path(raw_spec_path))
    spec_path = Path(raw_spec_path).resolve()
    plan_path = _resolve_contract_path(meta.get("plan"), repo_root)
    if plan_path is None:
        raise ValueError(f"slice 缺 plan path，無法 pin dispatch inputs: {meta.get('slice_id')}")
    try:
        spec_bytes = spec_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"spec file unreadable for dispatch pinning: {spec_path}") from exc
    try:
        plan_bytes = plan_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"plan file unreadable for dispatch pinning: {plan_path}") from exc
    spec_hash = verification.sha256_bytes(spec_bytes)
    plan_hash = verification.sha256_bytes(plan_bytes)
    verification_contract = meta.get("verification")
    verification_hash = (
        verification.canonical_json_hash(verification_contract)
        if verification_contract is not None
        else verification.canonical_json_hash(None)
    )
    return {
        "spec_path": str(spec_path),
        "spec_hash": spec_hash,
        "plan_path": str(plan_path),
        "plan_hash": plan_hash,
        "target_branch": meta.get("target_branch") or "main",
        "target_remote": verification.normalize_remote_name(
            os.environ.get("PSC_TARGET_REMOTE") if target_remote is None else target_remote
        ),
        "verification_hash": verification_hash,
        "verification": verification_contract,
        "review_policy": (
            verification_contract.get("review_policy")
            if isinstance(verification_contract, dict)
            else None
        ),
    }


# --------------------------------------------------------------------------- #
# 2) scan_specs（確定性）
# --------------------------------------------------------------------------- #
def scan_specs(specs_dir) -> list[dict]:
    """掃 specs_dir 下 *.md，逐檔 parse_spec_frontmatter，確定性排序。

    目錄不存在 → []（非錯誤）。
    """
    d = Path(specs_dir)
    if not d.is_dir():
        return []
    return [parse_spec_frontmatter(p) for p in sorted(d.glob("*.md"))]


# --------------------------------------------------------------------------- #
# 3) detect_cycles（DAG 回邊偵測，refuse）
# --------------------------------------------------------------------------- #
def _build_graph(metas: list[dict]) -> dict[str, list[str]]:
    """以 slice_id 為節點、depends_on 為有向邊建圖。

    重複 slice_id → raise ValueError（身分不明確的 DAG 直接拒絕，不靜默合併）。
    兩份 spec 誤用同一 slice_id 是現實的 copy-paste 錯誤：若靜默以後者覆寫前者的
    邊，會遮蔽真實的環；下游 fan-out 也會對同一 `feature/<slice_id>` 重複派工
    （第二次 `git worktree add` 必失敗、且違反「一單位一 job」）。故 fail-safe 提前拒絕。
    不含 slice_id（None/非字串）的 meta 不入圖（無身分，不可為相依目標）。
    """
    graph: dict[str, list[str]] = {}
    for m in metas:
        sid = m.get("slice_id")
        if not isinstance(sid, str):
            continue
        if sid in graph:
            raise ValueError(f"depends_on 偵測到重複 slice_id: {sid}")
        graph[sid] = [d for d in m.get("depends_on", [])]
    return graph


def classify_batch_dependency(
    dep: str,
    *,
    batch_ids: set[str],
    handoff_dir: str,
) -> str | None:
    if dep in batch_ids:
        return None
    manifest_path = Path(handoff_dir) / f"{dep}.json"
    if manifest_path.is_file():
        return f"deps-external:{dep}"
    return f"deps-unknown:{dep}"


def detect_cycles(metas: list[dict]) -> None:
    """以 slice_id 為節點、depends_on 為有向邊偵測循環相依。

    成環 → raise ValueError（帶 cycle path）。
    重複 slice_id → raise ValueError（先於 DFS，見 _build_graph）。
    指向不在 metas 的 slice_id 的邊不算環（外部/未掃到；診斷責任在
    classify_batch_dependency）。
    """
    graph = _build_graph(metas)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {sid: WHITE for sid in graph}
    stack: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue  # 外部相依 → 不算環
            if color[dep] == GRAY:
                cycle = stack[stack.index(dep):] + [dep]
                raise ValueError(f"depends_on 偵測到循環相依: {' -> '.join(cycle)}")
            if color[dep] == WHITE:
                visit(dep)
        stack.pop()
        color[node] = BLACK

    for sid in graph:
        if color[sid] == WHITE:
            visit(sid)


# --------------------------------------------------------------------------- #
# 4) ready_units（三條件 + 先偵測環）
# --------------------------------------------------------------------------- #
def ready_units(metas: list[dict], is_satisfied: IsSatisfied) -> list[dict]:
    """回就緒單位：有 slice_id ∧ dispatch=='auto' ∧ plan 非空 ∧ depends_on 全滿足。

    MUST 先 detect_cycles（成環/重複 slice_id 整批 raise，不回部分集）。
    無 slice_id（None/非字串/空字串）的單位無身分——無法成為 depends_on 目標、
    無法被追蹤或交接——依 fail-safe 立場 MUST NOT 就緒；此檢查也使 dispatch_ready
    存取 m['slice_id'] / m['plan'] 必為合法非空字串。
    is_satisfied 為必注入參數（呼叫者決定判定來源）。確定性序（沿 metas 順序）。
    """
    detect_cycles(metas)  # 先 refuse 環/重複 slice_id
    ready: list[dict] = []
    for m in metas:
        if not (isinstance(m.get("slice_id"), str) and m["slice_id"]):
            continue
        if m.get("dispatch") != "auto":
            continue
        if not (isinstance(m.get("plan"), str) and m["plan"]):
            continue
        deps = m.get("depends_on", [])
        if all(is_satisfied(dep) for dep in deps):
            ready.append(m)
    return ready


# --------------------------------------------------------------------------- #
# 5) default_is_satisfied（預設來源 = handoff gate_status；保持可注入覆寫）
# --------------------------------------------------------------------------- #
def default_is_satisfied(
    slice_id: str,
    handoff_dir: str = DEFAULT_HANDOFF_DIR,
    *,
    repo_root: str | Path | None = None,
    git_runner=None,
) -> bool:
    """預設判定：handoff 指向有效 CompletionRecord 且 candidate 仍為 target ancestor。"""
    return (
        completion.load_completion_from_handoff(
            slice_id,
            handoff_dir=handoff_dir,
            repo_root=repo_root,
            git_runner=git_runner,
        )
        is not None
    )


# --------------------------------------------------------------------------- #
# 6) dispatch_ready（fan-out，reuse Phase 2 Dispatcher）
# --------------------------------------------------------------------------- #
class DispatchReadyRequiresLauncherError(RuntimeError):
    """fan-out 需 headless launcher 卻未提供時 fail-fast 拋出（zh-tw）。"""


def dispatch_ready(
    metas: list[dict],
    is_satisfied: IsSatisfied,
    dispatcher,
    persona: str = "builder",
    git_runner=None,
    launcher: AgentLauncher | None = None,
    handoff_dir: str = DEFAULT_HANDOFF_DIR,
    identity_registry=None,
    launcher_factory=None,
    spawn_admission: SpawnAdmissionLimiter | None = None,
) -> list[dict]:
    """算就緒集，對每單位經注入的 headless AgentLauncher 各啟一個 agent（一單位一 job）。

    隔離靠 per-worktree headless session，故並行安全。

    spawn_admission（#381）：就緒集在同一次呼叫內背靠背 spawn 多個 agent 時，
    同一 provider（executor CLI，例如 copilot 啟動時連續探測 GitHub `/user`
    約 6-7 次）會瞬間把獨立於 core rate_limit 的 quota bucket 打爆。真正 launch
    前先呼叫 ``limiter.admit(provider)``：同一 provider 未跑滿最小間隔就等待，
    不同 provider 互不阻塞，spawn 成功即釋放（不佔住 job 的整個執行期）。
    未注入（``None``）時解析為零間隔 no-op，與過去「完全沒有這個參數」的行為
    等價——只有呼叫端顯式建構並注入 limiter 才會真的節流。

    fail-fast（reviewer #112-3）：manager 自主 fan-out 一律走 headless launcher。
    persona 契約 prompt 是多行文字，舊 tmux pane 路徑用 `send-keys -l` 會把每個
    `\\n` 變成 Enter、把 prompt 打散；故就緒集非空卻無 launcher 時，直接拒絕並
    指示改用 `--executor`（headless），不再 silently 經 pane 送多行 prompt。
    （git_runner 為歷史相容參數，headless 路徑不使用。）

    prompt 構建（build_dispatch_prompt）置於 per-slice try/except 內（reviewer #112-2）：
    未知 role / render 失敗只影響該單位，被收進 errors，不破壞其他就緒單位的派工隔離。
    回 dispatched jobs；有任何單位失敗 → 收齊後 raise DispatchReadyError（帶成功 jobs）。

    dispatch_head baseline（#131）：worktree 建好、launch 前取 `feature/<slice>` 的
    branch head 持久化於 job，complete_tick 的預設 shadow gate 才有 base 可算
    `compute_changed_paths(base, branch)`；取不到（git 例外）→ None，shadow 降級不阻釋放。
    git_runner 注入即沿用（預設 `_default_git_runner`，與 dispatcher.dispatch 同源）。
    """
    ready = ready_units(metas, is_satisfied)
    if ready and launcher is None:
        raise DispatchReadyRequiresLauncherError(
            "manager 自主 fan-out 需 headless launcher："
            "persona 契約為多行 prompt，經 tmux pane send-keys -l 會被換行打散。"
            "請以 --executor（copilot/claude/codex）走 headless 路徑派工。"
        )
    if ready and persona == "builder":
        commit_required_factory = getattr(launcher, "as_commit_required", None)
        if callable(commit_required_factory):
            launcher = commit_required_factory()
    runner = git_runner or _default_git_runner
    resolved_identity_registry = identity_registry
    limiter = resolve_limiter(spawn_admission)
    jobs: list[dict] = []
    errors: list[tuple[str, Exception]] = []
    for m in ready:
        slice_id = m["slice_id"]
        job: dict | None = None
        pinned_inputs: dict | None = None
        try:
            prompt = build_dispatch_prompt(persona, task=slice_id, plan_path=m["plan"])
            pinned_inputs = pin_dispatch_inputs(m)
            # best-effort baseline（reviewer #333-1）：identity/launcher_factory 檢查
            # 或 base_sha 解析若晚點失敗，slice 落 needs_human 後 dispatch_base 不會
            # 再被更新（見下方 _mark_slice_needs_human），故先嘗試取現有 branch head
            # 存底；branch 尚未建立（首次派工常態）時取不到，None 為預期落點。
            try:
                early_dispatch_head: str | None = runner(["rev-parse", _branch_for_slice(slice_id)])
            except Exception:
                early_dispatch_head = None
            _record_pending_slice(
                dispatcher=dispatcher,
                slice_id=slice_id,
                pinned_inputs=pinned_inputs,
                dispatch_base=early_dispatch_head,
            )
            active_launcher = launcher
            executor = m.get("executor")
            model_id = m.get("model_id")
            identity = None
            if isinstance(executor, str) and executor and isinstance(model_id, str) and model_id:
                if resolved_identity_registry is None:
                    resolved_identity_registry = load_model_identities()
                identity = resolved_identity_registry.get(executor, model_id)
                if identity is None:
                    available = ", ".join(
                        f"{item.executor}/{item.model_id}"
                        for item in resolved_identity_registry.identities
                    ) or "(none)"
                    raise ValueError(
                        f"spec identity unknown: {executor}/{model_id}（可用 candidates: {available}）"
                    )
                if launcher_factory is None:
                    raise ValueError(
                        f"slice {slice_id} declares executor/model_id but launcher_factory is unavailable"
                    )
                active_launcher = launcher_factory(identity)
                if persona == "builder":
                    commit_required_factory = getattr(active_launcher, "as_commit_required", None)
                    if callable(commit_required_factory):
                        active_launcher = commit_required_factory()
            base_sha = _resolve_target_base_sha(
                meta=m,
                pinned_inputs=pinned_inputs,
                handoff_dir=handoff_dir,
                git_runner=runner,
            )
            worktree = _launcher_worktree(dispatcher, slice_id, base_sha=base_sha)
            # baseline 須在 agent 動工前取（launch 前），否則含進 agent 的 commit → 空 diff。
            try:
                dispatch_head: str | None = runner(["rev-parse", _branch_for_slice(slice_id)])
            except Exception:
                dispatch_head = None
            log_dir = str(Path("runtime/dispatch") / slice_id)
            # 在 launch 前先落地 registry row：Popen 之後、記錄完成之前若 daemon
            # 崩潰，仍有可回收的 job 列（否則 agent 在跑卻無 job / in_flight / 輪詢）。
            job = _record_launching_job(
                dispatcher=dispatcher,
                slice_id=slice_id,
                persona=persona,
                worktree=worktree,
                dispatch_head=dispatch_head,
                # #469：spec frontmatter 顯式宣告的 repo 歸屬（未宣告 → None，
                # 不推斷）；寫進 job record 既有 workflow_repo 欄，終局 manifest
                # 與 slices/attention 讀取端（#465/#349）即可投影。
                workflow_repo=m.get("repo"),
            )
            _mark_slice_building(
                dispatcher=dispatcher,
                slice_id=slice_id,
                builder_job_id=job.get("job_id"),
                dispatch_base=base_sha or dispatch_head,
            )
            # #381：真正 spawn 前才 admit——記錄下這次要用的 job row 之後、
            # Popen 之前，讓等待時間不計入「job 已在跑」的錯覺。
            limiter.admit(
                resolve_provider(identity=identity, executor=executor, launcher=active_launcher)
            )
            handle = active_launcher.launch(
                slice_id=slice_id,
                prompt=prompt,
                worktree=worktree,
                log_dir=log_dir,
            )
            job = _attach_launch_handle(dispatcher=dispatcher, job=job, handle=handle)
            jobs.append(job)
        except Exception as exc:
            if job is not None:
                _fail_launching_job(dispatcher, job)
            if pinned_inputs is not None:
                _mark_slice_needs_human(dispatcher, slice_id, reason=str(exc))
            errors.append((slice_id, exc))
    if errors:
        raise DispatchReadyError(errors, jobs)
    return jobs


def _branch_for_slice(slice_id: str) -> str:
    return f"feature/{slice_id}"


def _resolve_target_base_sha(
    *,
    meta: dict,
    pinned_inputs: dict,
    handoff_dir: str,
    git_runner,
) -> str:
    repo_root = _infer_repo_root(Path(pinned_inputs["spec_path"]))
    target_branch = str(pinned_inputs["target_branch"])
    target_remote = str(pinned_inputs["target_remote"])
    target_ref = f"refs/remotes/{target_remote}/{target_branch}"
    fetch = verification._run_git(
        ["-C", str(repo_root), "fetch", "--no-tags", target_remote, target_branch],
        git_runner,
    )
    if fetch["status"] != "ok":
        raise ValueError(f"target fetch failed: {target_remote}/{target_branch}")
    target_head = verification._run_git(["-C", str(repo_root), "rev-parse", target_ref], git_runner)
    target_sha = target_head["stdout"].strip().lower()
    if target_head["status"] != "ok" or verification.SAFE_SHA_RE.fullmatch(target_sha) is None:
        raise ValueError(f"target ref unreadable: {target_ref}")
    dependency_target: str | None = None
    for dep in meta.get("depends_on", []):
        dep_record = completion.load_completion_from_handoff(
            str(dep),
            handoff_dir=handoff_dir,
            repo_root=repo_root,
            git_runner=git_runner,
        )
        if dep_record is None:
            raise ValueError(f"dependency unsatisfied: {dep}")
        dep_target = str(dep_record["target_branch"])
        if dependency_target is None:
            dependency_target = dep_target
        elif dependency_target != dep_target:
            raise ValueError("dependency target branch mismatch")
        if dep_target != target_branch:
            raise ValueError("dependency chain target branch mismatch")
        dep_candidate = str(dep_record["candidate"])
        ancestor = verification._run_git(
            ["-C", str(repo_root), "merge-base", "--is-ancestor", dep_candidate, target_sha],
            git_runner,
        )
        if ancestor["status"] != "ok":
            raise ValueError(f"dependency candidate stale: {dep}")
    return target_sha


def _launcher_worktree(dispatcher, slice_id: str, *, base_sha: str | None = None) -> str:
    """provision 這條 slice 的 build 工作區。

    #645：目錄名由 **slice_id** 導出（`job_workspace.job_segment()`），而
    `launcher.launch(slice_id=…)` 又把同一個字串交給
    `job_runner.prepare_systemd_template(job_id=…)` 產 instance 名——兩者因此是同一個
    推導點的同一個輸出，模板 unit 的 `ReadWritePaths=<pool>/%i` 必然對得上。branch 名
    仍是 `feature/<slice_id>`，只是不再決定目錄叫什麼。
    """

    worktree_creator = getattr(dispatcher, "_worktree_creator", None)
    if worktree_creator is None:
        return str(Path.cwd())
    branch = _branch_for_slice(slice_id)
    if base_sha is None:
        return worktree_creator.create(branch, job_id=slice_id)
    try:
        return worktree_creator.create(branch, job_id=slice_id, base_sha=base_sha)
    except TypeError:
        return worktree_creator.create(branch, job_id=slice_id)


def _record_launching_job(
    *,
    dispatcher,
    slice_id: str,
    persona: str,
    worktree: str,
    dispatch_head: str | None = None,
    workflow_repo: str | None = None,
) -> dict:
    """Persist the job row *before* launch (handle fields filled in later)."""
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        return {
            "task": slice_id,
            "persona": persona,
            "worktree": worktree,
            "status": "dispatched",
            "dispatch_head": dispatch_head,
            "executor": None,
            "session_name": None,
            "pid": None,
            "log_path": None,
            "workflow_repo": workflow_repo,
        }
    return registry.create_job(
        task=slice_id,
        persona=persona,
        kind="build",
        branch=_branch_for_slice(slice_id),
        pane="",
        worktree=worktree,
        dispatch_head=dispatch_head,
        executor=None,
        session_name=None,
        pid=None,
        log_path=None,
        # #469：slice-lane 的 repo 歸屬只來自 spec 顯式宣告；lane 判定看
        # workflow_run_id（manager._is_workflow_lane_job），帶此欄不會誤判 lane。
        workflow_repo=workflow_repo,
    )


def _record_pending_slice(
    *,
    dispatcher,
    slice_id: str,
    pinned_inputs: dict,
    dispatch_base: str | None,
) -> None:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        return
    try:
        registry.create_slice(
            slice_id=slice_id,
            spec_path=pinned_inputs["spec_path"],
            spec_hash=pinned_inputs["spec_hash"],
            plan_path=pinned_inputs["plan_path"],
            plan_hash=pinned_inputs["plan_hash"],
            target_branch=pinned_inputs["target_branch"],
            target_remote=pinned_inputs["target_remote"],
            verification_hash=pinned_inputs["verification_hash"],
            verification=pinned_inputs.get("verification"),
            dispatch_base=dispatch_base,
            builder_job_id=None,
            reviewer_job_id=None,
            candidate=None,
        )
    except ValueError as exc:
        if "slice 已存在" not in str(exc):
            raise
        registry.repin_slice(
            slice_id,
            spec_path=pinned_inputs["spec_path"],
            spec_hash=pinned_inputs["spec_hash"],
            plan_path=pinned_inputs["plan_path"],
            plan_hash=pinned_inputs["plan_hash"],
            target_branch=pinned_inputs["target_branch"],
            target_remote=pinned_inputs["target_remote"],
            verification_hash=pinned_inputs["verification_hash"],
            verification=pinned_inputs.get("verification"),
            dispatch_base=dispatch_base,
        )


def _mark_slice_building(
    *,
    dispatcher,
    slice_id: str,
    builder_job_id: str | None,
    dispatch_base: str | None,
) -> None:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        return
    registry.update_slice(
        slice_id,
        state="building",
        builder_job_id=builder_job_id,
        dispatch_base=dispatch_base,
    )


def _mark_slice_needs_human(dispatcher, slice_id: str, *, reason: str) -> None:
    registry = getattr(dispatcher, "_registry", None)
    if registry is None:
        return
    try:
        registry.update_slice(slice_id, state="needs_human", gate_state="needs_human")
    except Exception:
        return
    try:
        registry.record_action(
            slice_id,
            action="dispatch-failed",
            actor="manager",
            state="needs_human",
            gate_state="needs_human",
        )
    except Exception:
        _ = reason


def _attach_launch_handle(*, dispatcher, job: dict, handle: LaunchHandle) -> dict:
    """Fill in the launch handle on the pre-launch job row."""
    registry = getattr(dispatcher, "_registry", None)
    if registry is None or "job_id" not in job:
        return {
            **job,
            "executor": handle.executor,
            "session_name": handle.session_name,
            "pid": handle.pid,
            "log_path": handle.log_path,
            "runtime_principal": handle.runtime_principal,
            "runtime_mode": handle.runtime_mode,
            "runtime_surface": handle.runtime_surface,
            "credential_publish": handle.credential_publish,
            "prompt_path": handle.prompt_path,
        }
    kwargs = {
        "executor": handle.executor,
        "model_id": handle.model_id,
        "session_name": handle.session_name,
        "pid": handle.pid,
        "log_path": handle.log_path,
        "runtime_principal": handle.runtime_principal,
        "runtime_mode": handle.runtime_mode,
        "runtime_surface": handle.runtime_surface,
        "credential_publish": handle.credential_publish,
        "prompt_path": handle.prompt_path,
    }
    # External callers and older test registries may still expose the pre-
    # runtime metadata signature.  Filter only at this duck-typed seam; the
    # production JobRegistry accepts and persists every typed field above.
    try:
        signature = inspect.signature(registry.attach_launch_handle)
        if not any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            kwargs = {
                name: value
                for name, value in kwargs.items()
                if name in signature.parameters
            }
    except (TypeError, ValueError):
        pass
    return registry.attach_launch_handle(job["job_id"], **kwargs)


def _fail_launching_job(dispatcher, job: dict) -> None:
    """Reconcile a pre-launch row whose launch raised (mark failed)."""
    registry = getattr(dispatcher, "_registry", None)
    if registry is None or "job_id" not in job:
        return
    try:
        registry.update_status(job["job_id"], "failed")
    except Exception:
        pass
