from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import yaml

from .contract_command import build_dispatch_prompt
from .dispatcher import _default_git_runner
from .launcher import AgentLauncher, LaunchHandle

# is_satisfied predicate 型別：收 slice_id，回該相依是否「已滿足」（可釋放下游）。
# 判定來源由呼叫者決定（merged-to-main vs handoff gate_status）——#104 留開放。
IsSatisfied = Callable[[str], bool]

# Dispatcher duck-type：只需有 dispatch(task, persona, pane_id, command) -> dict（Phase 2 介面）。
DEFAULT_HANDOFF_DIR = "runtime/handoff"


class DispatchReadyError(RuntimeError):
    def __init__(self, errors: list[tuple[str, Exception]], jobs: list[dict]) -> None:
        self.errors = tuple(errors)
        self.jobs = list(jobs)
        failed = ", ".join(slice_id for slice_id, _ in errors)
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

    回 {path, dispatch, slice_id, plan, depends_on}。
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
    }
    if block is None:
        return meta

    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return meta  # 壞 frontmatter → 視為 hold（fail-safe，不 raise）
    if not isinstance(data, dict):
        return meta

    # dispatch：只認字面 'auto'
    if data.get("dispatch") == "auto":
        meta["dispatch"] = "auto"

    sid = data.get("slice_id")
    meta["slice_id"] = sid if isinstance(sid, str) else None

    plan = data.get("plan")
    meta["plan"] = plan if isinstance(plan, str) else None

    dep = data.get("depends_on")
    if isinstance(dep, list):
        meta["depends_on"] = [str(x) for x in dep]
    elif isinstance(dep, str):
        meta["depends_on"] = [dep]  # 單一字串容錯成單元素 list
    else:
        meta["depends_on"] = []

    return meta


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


def detect_cycles(metas: list[dict]) -> None:
    """以 slice_id 為節點、depends_on 為有向邊偵測循環相依。

    成環 → raise ValueError（帶 cycle path）。
    重複 slice_id → raise ValueError（先於 DFS，見 _build_graph）。
    指向不在 metas 的 slice_id 的邊不算環（外部/未掃到，交給 is_satisfied）。
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
def default_is_satisfied(slice_id: str, handoff_dir: str = DEFAULT_HANDOFF_DIR) -> bool:
    """預設判定：runtime/handoff/<slice_id>.json 存在且 gate_status=='passed'。

    檔不存在/壞檔/非 passed → False（fail-closed：未證明滿足即不釋放下游）。
    這只是預設 impl；ready_units/dispatch_ready 一律收注入 predicate，
    未來換 merged-to-main 來源只需換注入物（同 Callable[[str], bool] 介面）。
    """
    p = Path(handoff_dir) / f"{slice_id}.json"
    if not p.is_file():
        return False
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(payload, dict) and payload.get("gate_status") == "passed"


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
) -> list[dict]:
    """算就緒集，對每單位經注入的 headless AgentLauncher 各啟一個 agent（一單位一 job）。

    隔離靠 per-worktree headless session，故並行安全。

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
    runner = git_runner or _default_git_runner
    jobs: list[dict] = []
    errors: list[tuple[str, Exception]] = []
    for m in ready:
        slice_id = m["slice_id"]
        job: dict | None = None
        try:
            prompt = build_dispatch_prompt(persona, task=slice_id, plan_path=m["plan"])
            worktree = _launcher_worktree(dispatcher, slice_id)
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
            )
            handle = launcher.launch(
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
            errors.append((slice_id, exc))
    if errors:
        raise DispatchReadyError(errors, jobs)
    return jobs


def _branch_for_slice(slice_id: str) -> str:
    return f"feature/{slice_id}"


def _launcher_worktree(dispatcher, slice_id: str) -> str:
    worktree_creator = getattr(dispatcher, "_worktree_creator", None)
    if worktree_creator is None:
        return str(Path.cwd())
    return worktree_creator.create(_branch_for_slice(slice_id))


def _record_launching_job(
    *,
    dispatcher,
    slice_id: str,
    persona: str,
    worktree: str,
    dispatch_head: str | None = None,
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
        }
    return registry.create_job(
        task=slice_id,
        persona=persona,
        branch=_branch_for_slice(slice_id),
        pane="",
        worktree=worktree,
        dispatch_head=dispatch_head,
        executor=None,
        session_name=None,
        pid=None,
        log_path=None,
    )


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
        }
    return registry.attach_launch_handle(
        job["job_id"],
        executor=handle.executor,
        session_name=handle.session_name,
        pid=handle.pid,
        log_path=handle.log_path,
    )


def _fail_launching_job(dispatcher, job: dict) -> None:
    """Reconcile a pre-launch row whose launch raised (mark failed)."""
    registry = getattr(dispatcher, "_registry", None)
    if registry is None or "job_id" not in job:
        return
    try:
        registry.update_status(job["job_id"], "failed")
    except Exception:
        pass
