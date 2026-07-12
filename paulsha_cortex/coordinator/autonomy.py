from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from paulsha_cortex.config import paths

from .._yaml import YAMLError, safe_load
from . import completion
from .contract_command import build_dispatch_prompt
from .dispatcher import _default_git_runner
from .launcher import AgentLauncher, LaunchHandle
from . import verification

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
        "target_branch": None,
        "verification": None,
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
        meta["parse_error"] = exc.as_payload()
        return meta


def _normalize_frontmatter(path: Path, data: dict) -> dict:
    allowed = {"dispatch", "slice_id", "plan", "depends_on", "target_branch", "verification"}
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
    return meta


def _normalize_depends_on(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def _infer_repo_root(spec_path: Path) -> Path:
    configured = paths.repo_root().resolve()
    try:
        spec_path.resolve().relative_to(configured)
        return configured
    except ValueError:
        pass
    for parent in [spec_path.resolve(), *spec_path.resolve().parents]:
        if (parent / ".git").exists():
            return parent
    return spec_path.resolve().parent


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
        pinned_inputs: dict | None = None
        try:
            prompt = build_dispatch_prompt(persona, task=slice_id, plan_path=m["plan"])
            pinned_inputs = pin_dispatch_inputs(m)
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
            _record_pending_slice(
                dispatcher=dispatcher,
                slice_id=slice_id,
                pinned_inputs=pinned_inputs,
                dispatch_base=base_sha or dispatch_head,
            )
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
            _mark_slice_building(
                dispatcher=dispatcher,
                slice_id=slice_id,
                builder_job_id=job.get("job_id"),
                dispatch_base=base_sha or dispatch_head,
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
    worktree_creator = getattr(dispatcher, "_worktree_creator", None)
    if worktree_creator is None:
        return str(Path.cwd())
    branch = _branch_for_slice(slice_id)
    if base_sha is None:
        return worktree_creator.create(branch)
    try:
        return worktree_creator.create(branch, base_sha=base_sha)
    except TypeError:
        return worktree_creator.create(branch)


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
        kind="build",
        branch=_branch_for_slice(slice_id),
        pane="",
        worktree=worktree,
        dispatch_head=dispatch_head,
        executor=None,
        session_name=None,
        pid=None,
        log_path=None,
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
        }
    return registry.attach_launch_handle(
        job["job_id"],
        executor=handle.executor,
        model_id=handle.model_id,
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
