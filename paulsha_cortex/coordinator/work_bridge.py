"""Narrow integration seam joining WorkAuthority, WorkflowRun, and delivery.

The JobRegistry aggregate is the only workflow truth.  Delivery keeps a
run-keyed journal, but never invents a second run identity or lifecycle state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from paulsha_cortex.config import paths
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.selector import ComboSelection, ComboSelectionError, select_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    iter_combo_files,
    load_cards,
    load_combo,
    resolve_combo_path,
)
from paulsha_cortex.deck.task_types import load_task_types

from . import job_workspace
from . import verification
from .claim import (
    WorkAuthority,
    load_work_authority,
    mapped_issue_titles,
    openspec_refs_compatible,
    sizing_band,
    work_authority_digest,
)
from . import model_resolution
from .diagnostics import diagnostic_reason
from .github_delivery import GitHubDeliveryClient
from .model_identities import IdentityRegistry, load_model_identities
from .planning import (
    ACCEPTANCE_SURFACE_RULES,
    PlanningArtifact,
    assess_planning_completeness,
    compute_sizing_score,
)
from .preflight import PreflightRequest, load_preflight_command, run_preflight
from .workflow import MODEL_CHAIN_PERSONAS, validate_ship_stage_transition


def extract_model_chain_override(args: Mapping[str, object]) -> dict[str, dict[str, str]] | None:
    """#205 R1：從 work-action args 抽出 run-scoped planner/builder/reviewer
    模型鏈覆寫（``<persona>_executor``／``<persona>_model`` 成對出現）。

    只做「有沒有指定」的語法層抽取；executor/model 是否真的合法（capability、
    independence domain）留給 dispatch 時的 ``manager._select_workflow_identity``
    做 fail-closed 檢查（D4），這裡不重複判準也不預先做語意驗證。
    """
    override: dict[str, dict[str, str]] = {}
    for persona in sorted(MODEL_CHAIN_PERSONAS):
        executor = args.get(f"{persona}_executor")
        model_id = args.get(f"{persona}_model")
        if executor is None and model_id is None:
            continue
        if (
            not isinstance(executor, str)
            or not executor.strip()
            or not isinstance(model_id, str)
            or not model_id.strip()
        ):
            raise ValueError(
                f"model chain override for {persona} requires both executor and model"
            )
        override[persona] = {"executor": executor.strip(), "model_id": model_id.strip()}
    return override or None


def _remote_repo(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return None
    value = result.stdout.strip()
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if value.startswith(prefix):
            repo = value[len(prefix) :].removesuffix(".git").rstrip("/")
            return repo if repo.count("/") == 1 else None
    return None


def resolve_trusted_repo_root(repo: str, *, explicit: object = None) -> Path:
    """Resolve owner/name only through installed repo/Monitor configuration."""

    candidates: list[Path] = []
    if isinstance(explicit, (str, Path)) and explicit:
        raw = Path(explicit).expanduser()
        try:
            root = raw.resolve(strict=True)
        except OSError as exc:
            raise ValueError("explicit repo root unavailable") from exc
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            shell=False,
            capture_output=True,
            text=True,
        )
        try:
            top_root = Path(top.stdout.strip()).resolve(strict=True)
        except (AttributeError, OSError):
            top_root = Path()
        if (
            raw.is_symlink()
            or not root.is_dir()
            or top.returncode != 0
            or top_root != root
            or _remote_repo(root) != repo
        ):
            raise ValueError(
                "explicit repo root must be the canonical git top-level and its remote must match owner/name"
            )
        return root
    # #612：候選只收「顯式宣告的」repo 根。舊實作用 `paths.repo_root()`，未宣告
    # `PSC_REPO_ROOT` 時它會退回 cwd——daemon 的 cwd 就是 operator 的真實
    # checkout，於是 owner/name 會被「解析」到一個沒人指定過的樹。改讀
    # `configured_repo_root()`：沒宣告就不進候選，最後由下方「必須恰好命中一個」
    # 的檢查 fail-closed。
    configured = paths.configured_repo_root()
    if configured is not None:
        candidates.append(configured)
    try:
        from paulsha_cortex.monitor.config import load_config

        config = load_config()
        candidates.extend(item.path for item in config.workspaces)
        candidates.extend(Path(item.path) for item in config.hippo_projects)
    except (AttributeError, FileNotFoundError, OSError, TypeError, ValueError):
        pass
    matches: list[Path] = []
    for raw in candidates:
        try:
            root = raw.resolve(strict=True)
        except OSError:
            continue
        if raw.is_symlink() or not root.is_dir() or _remote_repo(root) != repo:
            continue
        if root not in matches:
            matches.append(root)
    if len(matches) != 1:
        raise ValueError("trusted repo registry did not resolve exactly one owner/name root")
    return matches[0]


def _combo_catalog(cards) -> dict[str, object]:
    catalog: dict[str, object] = {}
    for combo_id, combo_file in iter_combo_files():
        combo = load_combo(combo_file, cards)
        catalog[combo.id] = combo
    return catalog


def _combo_selection_payload(selection: ComboSelection) -> dict[str, str | None]:
    return {
        "source": selection.source,
        "task_type": selection.task_type,
        "combo": selection.combo_id,
        "reason": selection.reason,
    }


def default_workflow_manifest(work_id: str, *, change: str | None, combo_name: str = "feature-oneshot"):
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(resolve_combo_path(combo_name), cards)
    result = compile_combo(
        combo,
        cards,
        work_id,
        change=change or work_id,
        allow_external=True,
    )
    if result.workflow_manifest is None:  # pragma: no cover - compile contract
        raise RuntimeError(f"{combo_name} did not produce a workflow manifest")
    result.workflow_manifest.validate_manager_spine()
    return result.workflow_manifest


def _superpowers_spec_kind(ref: str) -> str:
    """把 monitor 的 ``superpowers_spec`` source 還原成 planning 產線的 kind。

    #524（B）：monitor 的 provider 規則把 ``docs/superpowers/specs/**/*.md``
    一律標成 ``superpowers_spec``（見 ``monitor/providers.py``），但 planning
    產線的 canonical destinations（見 ``planning_runtime.py`` 的
    ``{"spec": ...-spec.md, "design": ...-design.md, "plan": ...}``）把同一個
    目錄底下的 ``*-design.md`` 定義為 kind ``design``。

    這條差異過去被 ``_artifact_rows`` 直接抹平成 ``spec``，於是新世代 claim 時
    seed 進 ``run.planning_authority`` 的 design 檔掛著 kind ``spec``；等
    brainstorming 用 kind ``design`` 對同一路徑重新發佈，
    ``manager._publish_planning_artifacts`` 的 ``owner.kind != row["kind"]``
    立刻 fail-closed，訊息即生產現場 ``workflow-7bb3a83c2c1fc37359d5`` 的
    ``primary-artifact-write-rejected: ValueError: planning artifact lacks
    current planning authority: ...-design.md``。下一代因此永遠承接不了前一代
    的 artifact authority，連 ``recover-planning`` 重跑也是同一堵牆。

    只用檔名尾綴判定（不讀內容）：``-design.md`` 對應 kind ``design``，其餘
    維持 ``spec``。這與 ``planning_runtime`` 的 destinations 是同一組字面約定，
    也是唯一能在「尚未讀檔」的 claim 時點取得的訊號。
    """

    return "design" if ref.endswith("-design.md") else "spec"


def _artifact_rows(root: Path, authority: WorkAuthority) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, ref: str) -> None:
        key = (kind, ref)
        if key not in seen and (root / ref).is_file():
            seen.add(key)
            rows.append({"kind": kind, "ref": ref})

    for revision in authority.source_revisions:
        source_id = revision.rsplit("@", 1)[0]
        prefix = f"superpowers_spec:{authority.repo}:"
        if source_id.startswith(prefix):
            add(_superpowers_spec_kind(source_id[len(prefix) :]), source_id[len(prefix) :])
        prefix = f"superpowers_plan:{authority.repo}:"
        if source_id.startswith(prefix):
            add("plan", source_id[len(prefix) :])
    for change in authority.mapped_openspec:
        base = f"openspec/changes/{change}"
        add("spec", f"{base}/proposal.md")
        add("design", f"{base}/design.md")
        add("plan", f"{base}/tasks.md")
    for ref in authority.mapped_todo_paths:
        add("plan", ref)
    return rows


def current_sizing_snapshot(
    *,
    workspace_root: str | Path,
    combo_name: str,
    artifact_rows: Iterable[Mapping[str, str]],
) -> tuple[int | None, str | None]:
    """五維 sizing 重算的共用 fail-soft helper（#208 收口 wiring 1/3）。

    ``artifact_rows`` 是 ``_artifact_rows()``／``PlanningArtifactAuthority`` 已產出
    的 ``{"kind": ..., "ref": ...}`` 形狀——從 ``workspace_root`` 讀出內容餵給
    ``planning.compute_sizing_score()``。任何一步不可得（檔案缺席、讀取失敗、
    plan 缺 domain_breadth／state_consistency 宣告欄位、combo 無法解析）一律
    fail-soft 回傳 ``(None, None)``——呼叫端維持現行為，不掛 band（#208 紅線）。

    ``applicable_contract_rules`` 固定餵 ``planning.ACCEPTANCE_SURFACE_RULES``
    全集：R-09（changelog fragment）／R-16（CLI help 同步）／R-19（CI 測試）三條
    process-level 規則對本 repo 任何程式碼變動類工作項目皆適用，不需要每個呼叫端
    重新從 scope/code_paths 反推一次適用子集。
    """

    try:
        root = Path(workspace_root)
        artifacts: list[PlanningArtifact] = []
        plan_artifact: PlanningArtifact | None = None
        for row in artifact_rows:
            kind = row["kind"]
            ref = row["ref"]
            text = (root / ref).read_text(encoding="utf-8")
            artifact = PlanningArtifact(kind=kind, ref=ref, text=text)
            artifacts.append(artifact)
            if kind == "plan":
                plan_artifact = artifact
        if plan_artifact is None:
            return None, None
        cards = load_cards(DEFAULT_CARDS_PATH)
        combo = load_combo(resolve_combo_path(combo_name), cards)
        # combo.cards 只存 ComboEntry(ref, depends_on)——persona_binding 是card
        # 本身（load_cards 的字典）的欄位，須用 ref 查表，見 deck/schema.py。
        persona_binding_count = sum(
            1
            for entry in combo.cards
            if cards.get(entry.ref) is not None and cards[entry.ref].persona_binding is not None
        )
        completeness_report = assess_planning_completeness(artifacts)
        score = compute_sizing_score(
            plan_artifact=plan_artifact,
            completeness_report=completeness_report,
            gate_spine_count=len(combo.gate_spine),
            applicable_contract_rules=ACCEPTANCE_SURFACE_RULES,
            cards_count=len(combo.cards),
            persona_binding_count=persona_binding_count,
        )
        return score.total, sizing_band(score.total)
    except (OSError, UnicodeDecodeError, ValueError, KeyError):
        return None, None


def _write_manifest(root: Path, claim_key: str, manifest) -> Path:
    directory = root / "workflow-manifests"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{claim_key.removeprefix('claim:v1:')}.json"
    body = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    if target.exists():
        if target.read_text(encoding="utf-8") != body:
            raise RuntimeError("canonical workflow manifest conflicts with persisted claim")
        return target
    temporary = directory / f".{target.name}.{os.getpid()}.tmp"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def _other_owner_ongoing_runs(registry, authority: WorkAuthority) -> list:
    """Ongoing runs owned by a *different* work_id that still claim one of
    this authority's mapped issues.

    Source-owner transfers (issue #217, design #208 D) move ``mapped_issues``
    from one work_id to another through the ``.cortex/work-items.yaml``
    unlink/link override. ``JobRegistry._manager_create_workflow_run`` only
    auto-supersedes an *ongoing* run for the same ``(repo, work_id)`` pair, so
    a different work_id's stale claim on the same issue is never protected
    there. Without this guard a new-owner claim could start while the old
    owner's run is still live — the exact "舊 owner 仍在 snapshot 而新 run 已
    start" intermediate state hippo #41 (v3→v4) hit as a missing_issue /
    human-intervention-required run.
    """

    expected_issue_refs = {
        f"{authority.repo}#{number}" for number in authority.mapped_issues
    }
    if not expected_issue_refs:
        return []
    return [
        run
        for run in registry.list_workflow_runs()
        if run.repo == authority.repo
        and run.work_id != authority.work_id
        and run.status == "ongoing"
        and expected_issue_refs & set(run.issue_refs)
    ]


def _claimable_existing_runs(registry, claim_key: str) -> list:
    """#299：同 claim_key 既有 run 中，過濾掉 abandon 已釋放（``superseded`` 且帶
    ``planning_released`` facet）的歷史紀錄。

    #256 D4 的釋放語意允許同識別重新 claim；released run 若仍參與 reuse guard
    短路，abandon→reclaim 即永久死路（registry 層的
    ``_manager_create_workflow_run`` 本已支援以 attempt 鹽化 run_id 建新 run）。
    其餘狀態（ongoing／done／未釋放的 superseded）維持原短路行為。
    """
    return [
        run
        for run in registry.list_workflow_runs()
        if run.claim_key == claim_key
        and not (run.status == "superseded" and "planning_released" in run.facets)
    ]


def start_canonical_workflow(
    *,
    registry,
    authority: WorkAuthority,
    claim_key: str,
    coordinator_root: str | Path,
    explicit_repo_root: object = None,
    identity_registry: IdentityRegistry | None = None,
    runtime_factory=None,
    needs_human_reason: str | None = None,
    model_chain_override: dict[str, dict[str, str]] | None = None,
    combo_override: str | None = None,
):
    """Create/resume the real WorkflowRun for a WorkAuthority claim."""

    existing = _claimable_existing_runs(registry, claim_key)
    existing_run = None
    if existing:
        if len(existing) != 1 or existing[0].repo != authority.repo or existing[0].work_id != authority.work_id:
            raise RuntimeError("canonical workflow claim collision")
        existing_run = existing[0]
        if existing_run.status != "ongoing":
            return existing_run
        if existing_run.current_phase != "define":
            return existing_run
    # #205 R2：覆寫於 claim（或首次 dispatch）時凍結——operator 這次沒有明確
    # 再次覆寫時，沿用既有 run（仍在 define phase 的重試路徑）已凍結的覆寫，
    # 不得因為這次呼叫沒帶覆寫參數就悄悄清空既有意圖。
    effective_model_chain_override = (
        model_chain_override
        if model_chain_override is not None
        else (existing_run.model_chain_override if existing_run is not None else None)
    )
    # #390：combo 比照上面 model_chain_override 的凍結語意——resume 等後續動作
    # 依 contract.py 的 fail-closed 設計不會轉發 combo（見 manager.apply_work_action
    # 的 work_action_combo_override 只在 start/intake 有效），這裡若沒收到明確
    # override 就沿用既有 run（仍在 define phase 的重試路徑）已持久化的 combo，
    # 避免重新對 mapped_issue_titles 跑 select_combo 選出不同 combo、
    # 導致 default_workflow_manifest 產生不同 bytes、被 _write_manifest 的
    # byte 比對判為 conflict（canonical workflow manifest conflicts with
    # persisted claim）。
    effective_combo_override = (
        combo_override
        if combo_override is not None
        else (existing_run.combo if existing_run is not None else None)
    )
    stale_owners = _other_owner_ongoing_runs(registry, authority)
    if stale_owners:
        blocking = ", ".join(sorted({run.work_id for run in stale_owners}))
        raise RuntimeError(
            "source-owner transfer incomplete: "
            f"{blocking} still owns an overlapping issue"
        )
    root = resolve_trusted_repo_root(authority.repo, explicit=explicit_repo_root)
    change = authority.mapped_openspec[0] if authority.mapped_openspec else authority.work_id
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo_catalog = _combo_catalog(cards)
    taxonomy = load_task_types(combos=combo_catalog)
    # combo_override（或凍結沿用的 effective_combo_override）的存在性／合法性
    # 驗證單一交給 select_combo（deck/selector.py R3：經 load_combo 驗證，
    # taxonomy 反查找不到 task_type 時保留 None，legacy combo 如 mcu-feature
    # 不會被誤判 unknown）；此處不再重覆一份驗證邏輯。凍結沿用時直接餵
    # override，走的是 select_combo 既有的 explicit-override 分支——
    # ComboSelection.source 因此如實標記為 explicit-override，不需另開
    # schema 不認得的新 source 字面值。
    try:
        combo_selection = select_combo(
            mapped_issue_titles(authority),
            taxonomy=taxonomy,
            override=effective_combo_override,
        )
    except ComboSelectionError as exc:
        if effective_combo_override is not None:
            raise RuntimeError(f"combo override unavailable: {effective_combo_override}") from exc
        raise
    manifest = default_workflow_manifest(
        authority.work_id,
        change=change,
        combo_name=combo_selection.combo_id,
    )
    # #208 收口 wiring 1：claim 時嘗試算 sizing（若能取得既有 plan artifact 與
    # combo 資訊——多半是 openspec-propose 已先在 disk 上落地 tasks.md 的情境）。
    # fail-soft：拿不到就維持 None，run 不掛 band，行為與現行完全相同。
    artifact_rows = _artifact_rows(root, authority)
    claim_sizing_score, claim_sizing_band = current_sizing_snapshot(
        workspace_root=root, combo_name=manifest.combo, artifact_rows=artifact_rows
    )
    if needs_human_reason is not None:
        # #669：這條路徑**不再由 claim 的 `missing_issue` 判定觸發**。
        #
        # 舊行為是「先建 run 再宣告 blocked」，於是「workstream 本來就不對應單一
        # issue」這個預期狀態被物化成 24 個 `current_phase: claim`／
        # `evidence_refs: []`／`next_actions: []` 的 needs_human 殭屍 run。
        # `work_actions._claim_action` 現在在判定 `missing_issue` 時直接回
        # `not_claimable`（記進 `not-claimable` ledger、**不呼叫本函式**），
        # 那條不變式由 `tests/test_claim_not_claimable_669.py` 釘住：
        # workflow_starter 在 missing_issue 時一次都不得被呼叫。
        #
        # 分支本身保留：它是「以指定理由建立一個 needs_human run」的通用設施，
        # 與 claim 判定的語意無關。
        if existing_run is not None:
            return existing_run
        run = registry._manager_create_workflow_run(
            work_id=authority.work_id,
            repo=authority.repo,
            claim_key=claim_key,
            source_revision=work_authority_digest(authority),
            workspace_root=str(root),
            combo=manifest.combo,
            current_phase="claim",
            steps=manifest.steps,
            issue_refs=tuple(f"{authority.repo}#{number}" for number in authority.mapped_issues),
            openspec_refs=authority.mapped_openspec,
            pr_refs=(),
            attempts={"claim": 1},
            facets=("needs_human",),
            gate_status="running",
            sizing_score=claim_sizing_score,
            sizing_band=claim_sizing_band,
            model_chain_override=effective_model_chain_override,
            combo_selection=_combo_selection_payload(combo_selection),
            # 診斷 invariant：`needs_human_reason` 本來就是這條路徑的參數名，
            # 過去只是個沒有落地的字串——run 建出來就掛 needs_human，理由卻
            # 留在呼叫端。這裡把它升格成結構化理由寫進 run。
            needs_human_reason=diagnostic_reason(
                "claim-blocked",
                f"claim 判定需要人工介入即建立 run：{needs_human_reason}",
                source="work_bridge.start_workflow_for_authority",
                work_id=authority.work_id,
                repo=authority.repo,
                claim_key=claim_key,
            ),
        )
        return run
    manifest_path = _write_manifest(Path(coordinator_root), claim_key, manifest)
    identities = identity_registry or load_model_identities()
    planning = [identity for identity in identities.identities if "planning" in identity.capabilities]
    if not planning:
        raise RuntimeError("no primary planning identity configured")
    # #534：primary planner 改依三層解析鏈挑（operator overlay → 評估合格清單 →
    # packaged fallback）。舊實作寫死 executor 順序 ("codex", "claude", "agy")，
    # 與 operator 在 host overlay 宣告的順序無關——人工指定形同不存在。
    ranked = model_resolution.rank_candidates(
        planning, role="planning", context=identities.resolution_context
    )
    if not ranked.ordered:
        raise RuntimeError(
            "no resolvable primary planning identity"
            f"（{ranked.exclusion_detail()}）"
        )
    primary = ranked.ordered[0]
    from . import manager

    result = manager.apply_workflow_action(
        registry,
        args={
            "action": "start",
            "work_id": authority.work_id,
            "repo": authority.repo,
            "claim_key": claim_key,
            "source_revision": work_authority_digest(authority),
            "artifact_root": str(root),
            "evidence_dir": str(Path(coordinator_root) / "evidence" / "planning"),
            "manifest_path": str(manifest_path),
            "planning_artifacts": artifact_rows,
            "sizing_score": claim_sizing_score,
            "sizing_band": claim_sizing_band,
            "primary_executor": primary.executor,
            "primary_model": primary.model_id,
            "primary_domain": primary.independence_domain,
            "issue_refs": [f"{authority.repo}#{number}" for number in authority.mapped_issues],
            "openspec_refs": list(authority.mapped_openspec),
            "pr_refs": [],
            "model_chain_override": effective_model_chain_override,
            "combo_selection": _combo_selection_payload(combo_selection),
        },
        identity_registry=identities,
        runtime_factory=runtime_factory,
        coordinator_root=coordinator_root,
    )
    return registry.get_workflow_run(str(result["run_id"]))


def workflow_status(run) -> str:
    if getattr(run, "status", "ongoing") == "done":
        return "done"
    if getattr(run, "status", "ongoing") == "superseded":
        if "planning_released" in run.facets:
            return "ongoing"
        return "blocked"
    if "needs_human" in run.facets:
        return "needs_human"
    if "needs_decomposition" in run.facets:
        # #223（design #208 H.3）：Red band 收斂路由；claim.py 是唯一消費此
        # active_status 的呼叫端（透過 work_actions._claim_action）。
        return "needs_decomposition"
    if "blocked" in run.facets:
        return "blocked"
    return "ongoing"


def _write_json_evidence(root: Path, category: str, payload: dict) -> dict[str, str]:
    digest = verification.canonical_json_hash(payload)
    directory = root.resolve() / "evidence" / category
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    envelope = {"payload": payload, "hash": digest}
    if target.exists():
        if target.is_symlink() or json.loads(target.read_text(encoding="utf-8")) != envelope:
            raise RuntimeError(f"{category} evidence conflict")
    else:
        temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(envelope, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            target.chmod(0o400)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ref": str(target), "hash": digest}


def _pr_metadata(run) -> dict[str, object]:
    issues = []
    for ref in run.issue_refs:
        prefix = f"{run.repo}#"
        if not ref.startswith(prefix) or not ref[len(prefix) :].isdigit():
            raise ValueError("workflow issue ref malformed")
        issues.append(int(ref[len(prefix) :]))
    if not issues:
        raise ValueError("workflow delivery requires a confirmed issue")
    body = "\n".join(
        [
            f"## 摘要\n\n完成 `{run.work_id}` 統一工作生命週期。",
            "## 驗證\n\n- [x] Manager exact-HEAD delivery gates",
            *(f"Closes #{number}" for number in sorted(issues)),
        ]
    )
    return {
        "title": f"feat(workflow): 完成 {run.work_id}",
        "body": body,
        "labels": ["enhancement"],
    }


def _metadata_file(root: Path, run, metadata: dict[str, object]) -> Path:
    directory = root.resolve() / "evidence" / "pr-metadata"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run.run_id}.json"
    body = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if target.exists():
        if target.is_symlink() or target.read_text(encoding="utf-8") != body:
            raise RuntimeError("workflow PR metadata conflict")
        return target
    temporary = directory / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _preflight_result_evidence(
    *,
    state_root: Path,
    run,
    candidate: str,
    stage: str,
    preflight,
    status: str,
    reason: str,
    next_action: str | None = None,
) -> dict[str, object]:
    payload = {
        "schema": "cortex-pr-preflight/v1",
        "run_id": run.run_id,
        "candidate": candidate,
        "stage": stage,
        "status": status,
        "reason": reason,
        "head": preflight.head,
        "tree_hash": preflight.tree_hash,
        "failed_stage": preflight.failed_stage,
        "policy": {
            "argv": list(preflight.policy.argv),
            "returncode": preflight.policy.returncode,
            # #759：失敗的逐字原因必須在 evidence 裡——「只記 returncode」讓每一次
            # preflight 失敗都要靠實機重現才能定位（本日三環皆如此）。有界尾段，
            # short summary 在尾巴（與 gate ledger detail 同向）。
            "stdout_tail": str(preflight.policy.stdout or "")[-2000:],
            "stderr_tail": str(preflight.policy.stderr or "")[-2000:],
        },
        "ci_parity": (
            None
            if preflight.ci_parity is None
            else {
                "argv": list(preflight.ci_parity.argv),
                "returncode": preflight.ci_parity.returncode,
                "stdout_tail": str(preflight.ci_parity.stdout or "")[-2000:],
                "stderr_tail": str(preflight.ci_parity.stderr or "")[-2000:],
            }
        ),
    }
    if next_action is not None:
        payload["next_action"] = next_action
    evidence = _write_json_evidence(state_root, "pr-preflight", payload)
    return {
        "trusted": True,
        "status": status,
        "head": candidate,
        "commit_id": candidate,
        "reason": reason,
        **evidence,
    }


def _builder_binding(
    registry,
    run,
    candidate: str,
    *,
    state_root: Path,
    foreign_ref,
) -> str:
    """Use the foreign-review edge to select the exact builder job.

    A feature workflow legitimately has several build cards, so counting all
    successful build jobs is ambiguous. The terminal review evidence names the
    one builder job whose candidate it actually reviewed.

    #653：回傳的是 **delivery branch**，不是那個 job 的工作區。

    這個函式原本回 `(Path(row["worktree"]).resolve(strict=True), branch)`，而
    ship 段接著就在那棵樹裡 `git diff`／`add`／`commit`／`rev-parse`／`push`、跑
    preflight、跑 `_ship_action` 的測試。Phase 2b 三分下那棵樹是 `cortex-builder`
    擁有的 `0700` clone，而 **#641 已把登記表裡 Manager 對 job 工作樹殘留的讀取
    授權全部收掉**（runbook 稽核 5b 要求 `/var/lib/cortex/worktree/` 底下零
    `setfacl`）⇒ 降權模式下 ship phase 會在**第一個 `git -C`** 就
    `Permission denied`。

    **不得把那條 ACL 加回來**：#644 的論證是那條授權唯一的消費端（在 builder
    掌控的樹裡執行命令）本身就是一條提權路徑。因此改成「ship 段自己 provision
    一棵 Manager-owned 的樹」（:func:`_manager_ship_workspace`），而這個函式只保留
    它真正不可取代的職責——**採信鏈**：foreign review 指名的那一個 builder（或
    post-archive 的 manager archive）job，必須是 `subject_head == candidate` 的
    那一筆，delivery branch 由它決定。

    `row["worktree"]` 因此**刻意不再被讀**：ship 段不需要它，而讀它就等於重新
    宣告一次「Manager 進得去 job 的樹」這個已被撤銷的前提。連
    `Path(...).resolve(strict=True)` 都不做——那會讓一棵已被 `cortex work gc`
    回收的工作區把一條合法的交付卡死。
    """

    from . import review

    review_payload, review_job = _workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=run,
        phase="review",
        expected_ref=foreign_ref.ref,
        expected_hash=foreign_ref.sha256,
    )
    evaluation = review.validate_gate_evaluation(review_payload)
    builder_job_id = evaluation.get("builder_job_id")
    if (
        evaluation.get("state") != "passed"
        or evaluation.get("candidate") != candidate
        or evaluation.get("reviewer_job_id") != review_job.get("job_id")
        or not isinstance(builder_job_id, str)
    ):
        raise RuntimeError("delivery foreign-review builder binding malformed")
    row = registry.get_job(builder_job_id)
    normal_builder = (
        row.get("workflow_phase") == "build"
        and row.get("persona") == "builder"
    )
    manager_archive = (
        row.get("workflow_phase") == "ship"
        and row.get("workflow_card") == "openspec-archive"
        and row.get("persona") == "manager"
        and row.get("executor") == "cortex-manager"
        and row.get("model_id") == "deterministic"
        and row.get("independence_domain") == "cortex"
        and isinstance(row.get("workflow_evidence"), dict)
    )
    if (
        row.get("workflow_run_id") != run.run_id
        or not (normal_builder or manager_archive)
        or row.get("status") != "exited"
        or row.get("exit_code") != 0
        or row.get("subject_head") != candidate
    ):
        raise RuntimeError("delivery requires the reviewed exact-candidate builder job")
    branch = row.get("branch")
    if not isinstance(branch, str) or not branch:
        raise RuntimeError("builder delivery binding malformed")
    return branch


def _ship_workspace_id(run, candidate: str) -> str:
    """ship 段那棵 Manager-owned 工作區的識別（#653）——**唯一推導點**。

    形狀與 `_manager_ship_job_task()` 對齊（`wf-<run 摘要>-…`），但多帶
    candidate 的前綴：ship 段的工作區是綁在 **(run, 這個 candidate)** 上的，而
    `openspec-archive` 一旦回收成功，`candidate_head` 就會前進到 archive commit
    ——下一輪 ship 因此拿到**另一個**識別、另一棵樹，前一棵原地留著（它正是
    `_record_manager_ship_job()` 記在 archive 卡上的 `worktree`，是那張卡的稽核
    定錨）。#650 之後 post-archive 的 verify／review 卡**不再讀這棵樹**：它們自己
    從來源樹 clone 一棵 `wf-<run 摘要>-review-<candidate 前綴>`（見
    `manager._reviewer_candidate_workspace()`），archive commit 已由 #649 的回收
    通道搬進來源樹。

    反過來說，**同一個 candidate 的每一次 ship tick 共用同一棵樹**：ship phase 會
    被 tick 很多次（等 preflight、等 PR、等 copilot、等 merge），每次都 clone 一
    份 35MB 是白燒；而識別穩定之後「這棵樹是不是我的」就有一條可驗證的判準
    （工作區標記檔的 `branch`／`base`），不必靠猜。
    """

    if verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError("ship workspace candidate is invalid")
    digest = hashlib.sha256(run.run_id.encode()).hexdigest()[:10]
    return f"wf-{digest}-ship-{candidate[:12]}"


def _require_pristine_ship_workspace(worktree: Path, *, branch: str, candidate: str) -> None:
    """ship 段開工前的三條不變式：branch 對、HEAD ＝ candidate、工作區乾淨。

    這三條是 ship 段其餘每一步的前提，集中在這裡驗一次而不是散在各處：

    - **branch**：`_push_exact_candidate()` 推的是 `HEAD:refs/heads/<branch>`，而
      `_harvest_manager_ship_commit()` 的回收 refspec 也是 `refs/heads/<branch>`
      ——detached HEAD 或 checkout 在別條 branch 上時兩者都會搬錯東西。
    - **HEAD ＝ candidate**：archive commit 必須恰好長在被採信的 candidate 上。
    - **乾淨**：archive 的 allowlist 判準是 `git diff HEAD` ＋
      `ls-files --others`，任何開工前就存在的殘留都會被算進那個集合。這條同時
      取代了舊模型裡 `_remove_canonical_untracked_reports()` 的角色——那時 ship
      段借用 builder 的 clone，reviewer 發佈在裡面的 canonical report 是**未追蹤
      檔**，必須先刪掉才不會弄髒 exact candidate；現在 ship 段有自己的 pristine
      clone，「候選樹被 report 弄髒」在結構上不再可能發生。
    """

    for argv, expected, failure in (
        (["symbolic-ref", "--quiet", "--short", "HEAD"], branch, "branch"),
        (["rev-parse", "HEAD"], candidate, "head"),
    ):
        probe = subprocess.run(
            ["git", "-C", str(worktree), *argv],
            shell=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0 or probe.stdout.strip().lower() != expected.lower():
            raise RuntimeError(f"manager ship workspace {failure} mismatch")
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise RuntimeError("manager ship workspace is not pristine")


def _reset_ship_workspace(worktree: Path, *, branch: str, candidate: str) -> bool:
    """把一棵**既有的** ship 工作區打回 `candidate` 的原狀；做不到就回 False。

    ship 段的每一次進入都要求 pristine（見
    :func:`_require_pristine_ship_workspace`）。前一次 tick 可能在裡面套用了
    `openspec archive` 卻在 commit 之前掛掉——#653 明載的
    `archive-applied-needs-commit` 重入路徑就是這條。**新模型的處置是「在新樹裡
    重跑 archive」**（票上給的兩個選項之一）：套用 archive 是一個對同一個
    candidate 完全可重現的確定性動作，把樹打回原狀再跑一次，比帶著一堆來歷不明
    的 dirty 檔往下走安全得多，也讓「崩在中間」與「從沒跑過」收斂成同一個狀態。

    已經 commit、但**還沒回收**的 archive commit 同樣被丟掉——那是對的：回收
    （`_harvest_manager_ship_commit()`）是採信這件事發生的唯一時點，沒回收就代表
    沒有任何下游依賴它，重做一次得到的是等價的 commit。回收**成功**之後
    `candidate_head` 才前進，那時識別已經換了一個（見 :func:`_ship_workspace_id`），
    根本走不到這裡。

    best-effort：任何一步失敗都回 False，由呼叫端整棵重建，不在這裡 raise。
    """

    for argv in (
        ["checkout", "--quiet", "--force", "-B", branch, candidate],
        ["reset", "--quiet", "--hard", candidate],
        ["clean", "-qffdx"],
    ):
        done = subprocess.run(
            ["git", "-C", str(worktree), *argv],
            shell=False,
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            return False
    try:
        _require_pristine_ship_workspace(worktree, branch=branch, candidate=candidate)
    except RuntimeError:
        return False
    return True


def _manager_ship_workspace(
    *,
    run,
    branch: str,
    candidate: str,
    creator=None,
) -> Path:
    """#653：ship 段動手的那棵樹——**Manager-owned**，不是 builder 的 clone。

    ## 為什麼一定要換一棵樹

    ship 段（`openspec-archive`／`policy-commit`／preflight／push／`_ship_action`）
    不是降權派工的對象：`manager._dispatch_workflow_card()` 對 ship phase 一律回
    `None`，這兩張卡由 Manager 自己在本模組內以 `cortex-manager` 身分同步執行
    （查證見 #654）。但它們原本全程在 `_builder_binding()` 交回來的 **builder 的
    clone** 裡動手，而 #641 已把 Manager 對 job 工作樹的讀取授權全部收掉 ⇒ 降權
    模式下第一個 `git -C` 就 `Permission denied`。**症狀是權限，不是 mount
    namespace。**

    ## 形狀

    以 `run.candidate_head`（＝這一輪被採信的 candidate）為 base、用
    `seams.ScriptWorktreeCreator` 在**來源樹**上 provision 一份自己的完整 clone。
    來源樹 `/var/lib/cortex/repos/<slug>` 是 `cortex-manager` 擁有且可寫（0817
    裁決），Manager 對自己 clone 出來的樹自然是 owner——commit／preflight／push
    全部沒有權限問題，也**不需要**任何指向 job 工作樹的 ACL。

    creator 的兩道既有守衛在這條 lane 上剛好就是我們要的：

    - `rev-parse --verify <candidate>^{commit}`：來源樹必須已經有這個 commit
      ——那正是 #654 的 build／ship 回收通道所保證的不變式。回收沒走完就 provision
      不起來，而不是在很遠的地方以看不懂的訊息炸開。
    - `merge-base --is-ancestor <branch> <candidate>`：來源樹的 delivery branch
      不得帶著 candidate 以外的 commit。

    ## 生命週期

    識別由 :func:`_ship_workspace_id` 決定（穩定於 (run, candidate)）：同一個
    candidate 的多次 tick 重用同一棵樹（重用前一律打回 pristine，見
    :func:`_reset_ship_workspace`），candidate 前進時換一棵新的。**不在這裡刪**
    ——archive 卡的 job 記錄指著這棵樹（`_record_manager_ship_job()` 的
    `worktree`／`workflow_repo_root`）；回收交給 `cortex work gc`，與 build 卡的
    clone 同一套。

    ## 紅線

    沒有 `--reference`／`--shared`／任何把 object store 接回共用的優化（#623 判定
    共用 object store 與三分隔離互斥），也沒有「Manager fetch 一棵 job 的 clone」
    （`job_workspace` 模組 docstring 已判定該形狀在三分下結構性不成立）。archive
    commit 回到來源樹走的是 #654 的 bundle ＋ append-only spool，consumer 仍是全
    repo 唯一的 `job_workspace.harvest_branch()`。
    """

    from . import seams

    source_repo = getattr(run, "workspace_root", None)
    if not isinstance(source_repo, str) or not source_repo:
        raise RuntimeError("manager ship workspace source repo missing")
    source = Path(source_repo)
    pool = paths.worktree_root_for(source)
    workspace_id = _ship_workspace_id(run, candidate)
    target = job_workspace.workspace_path(pool, workspace_id)
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise RuntimeError("manager ship workspace path is not a directory")
    if target.is_dir():
        marker = job_workspace.read_marker(target)
        reusable = (
            job_workspace.is_job_clone(target)
            and isinstance(marker, dict)
            and marker.get("branch") == branch
            and str(marker.get("base", "")).lower() == candidate.lower()
        )
        if reusable and _reset_ship_workspace(
            target, branch=branch, candidate=candidate
        ):
            return target
        if not job_workspace.is_job_clone(target):
            # 認不出這是什麼就**不刪**（#478 的爆炸半徑教訓）。這條路徑只該在
            # operator 手動放了東西進 pool 時走到。
            raise RuntimeError("manager ship workspace path is occupied")
        job_workspace.remove_clone(target)
    if creator is None:
        creator = seams.ScriptWorktreeCreator(repo=source, wt_root=pool, base="main")
    created = Path(creator.create(branch, job_id=workspace_id, base_sha=candidate))
    _require_pristine_ship_workspace(created, branch=branch, candidate=candidate)
    return created


def _manager_archive_applied(run) -> bool:
    # 對齊 manager._manager_archive_applied 的語意：必須「恰好一筆」passed 的
    # openspec-archive step 才算已完成；crash/retry 造成的多筆 passed step 視為
    # 尚未完成（fail-closed），不得靠第二套判定漂移出不同結論。單一真實實作放在
    # manager，這裡改為委派而非重寫一份，避免兩處各自演化。
    from . import manager

    return manager._manager_archive_applied(run)


def _push_exact_candidate(
    *,
    registry,
    run,
    authority: WorkAuthority,
    state_root: Path,
    worktree: Path,
    branch: str,
    candidate: str,
    runner,
    pre_push: Callable[[], None] | None = None,
) -> None:
    if re.fullmatch(r"feature/[a-z0-9][a-z0-9._/-]*", branch) is None:
        raise ValueError("workflow delivery branch is not an authorized feature ref")
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0 or head.stdout.strip().lower() != candidate:
        raise RuntimeError("delivery push requires exact local Candidate HEAD")
    from . import work_actions

    state_path = state_root / "delivery-journal.json"
    current_digest = work_authority_digest(authority)
    if run.source_revision != current_digest:
        run = registry._manager_update_workflow_run(
            run.run_id,
            source_revision=current_digest,
        )
    journal = work_actions._load_runs(state_path)
    if run.run_id in journal["runs"]:
        _rebase_delivery_journal_authority(
            state_root=state_root,
            run=run,
            authority=authority,
        )
    state, row, _canonical = work_actions._load_work_run(
        state_path=state_path,
        workflow_registry=registry,
        authority=authority,
    )
    ref = f"refs/heads/{branch}"

    def read_remote() -> str | None:
        completed = runner(
            ["git", "-C", str(worktree), "ls-remote", "--exit-code", "origin", ref],
            shell=False,
            capture_output=True,
            text=True,
        )
        if getattr(completed, "returncode", 1) == 2:
            return None
        if getattr(completed, "returncode", 1) != 0:
            raise RuntimeError("delivery remote branch readback failed")
        fields = str(getattr(completed, "stdout", "")).strip().split()
        if len(fields) != 2 or fields[1] != ref or verification.SAFE_SHA_RE.fullmatch(fields[0]) is None:
            raise RuntimeError("delivery remote branch readback malformed")
        return fields[0].lower()

    remote_head = read_remote()
    pushes = row.setdefault("pushes", {})
    persisted = pushes.get(candidate)
    expected = {"branch": branch, "ref": ref, "head": candidate}
    if persisted is not None and persisted != expected:
        raise RuntimeError("delivery push journal conflicts with Candidate")
    if remote_head != candidate:
        if pre_push is not None:
            pre_push()
        pushed = runner(
            ["git", "-C", str(worktree), "push", "origin", f"HEAD:{ref}"],
            shell=False,
            capture_output=True,
            text=True,
        )
        if getattr(pushed, "returncode", 1) != 0:
            raise RuntimeError("delivery exact Candidate push failed")
        remote_head = read_remote()
    if remote_head != candidate:
        raise RuntimeError("delivery remote branch does not match exact Candidate")
    if persisted is None:
        pushes[candidate] = expected
        work_actions._save_runs(state_path, state)


def _rebase_delivery_journal_authority(
    *, state_root: Path, run, authority: WorkAuthority
) -> None:
    from . import work_actions

    state_path = state_root / "delivery-journal.json"
    state = work_actions._load_runs(state_path)
    row = state["runs"].get(run.run_id)
    if not isinstance(row, dict):
        raise RuntimeError("delivery push journal missing canonical run")
    row.update(
        {
            "source_revisions": list(authority.source_revisions),
            "snapshot_hash": authority.snapshot_hash,
            "provider_revision": authority.github_provider_revision,
            "authority_digest": work_authority_digest(authority),
            # #765：claim_key 必須跟著 run 的現值走——journal 停在建列時代的 claim
            # 會讓 delivery/resume 的 canonical 視圖與 run row 分屬兩個 era，
            # 下游 job 選擇撿到舊 era terminal、binding 每 tick 必炸。
            "claim_key": run.claim_key,
            "mapped_issues": list(authority.mapped_issues),
            "mapped_prs": list(authority.mapped_prs),
            "mapped_openspec": list(authority.mapped_openspec),
            "mapped_todo_paths": list(authority.mapped_todo_paths),
        }
    )
    work_actions._save_runs(state_path, state)


def _archive_path_allowed(path: str, *, change: str) -> bool:
    return (
        path == "CHANGELOG.md"
        or path == "README.md"
        or path.startswith("changelog.d/")
        or path.startswith("docs/")
        or path.startswith("openspec/specs/")
        or path.startswith("openspec/changes/archive/")
        or path.startswith(f"openspec/changes/{change}/")
    )


def _manager_ship_job_task(*, run, card: str) -> str:
    """ship 卡那一筆 job 記錄的 `task`——**唯一推導點**（#649）。

    `reserve_job_id()` 與 `create_job()` 必須拿到逐字相同的 task（registry 會驗
    「這個 id 確實屬於同一個 task」），因此不再讓兩處各自組一次字串。與
    `manager._dispatch_workflow_card()` 的 build／verify／review 卡同一個公式。
    """

    return f"wf-{hashlib.sha256(run.run_id.encode()).hexdigest()[:10]}-{card}"


def _harvest_manager_ship_commit(
    *,
    state_root: Path,
    run,
    worktree: Path,
    branch: str,
    spool_key: str,
    baseline: str,
    new_head: str,
) -> str:
    """#649：把 ship phase 的 Manager commit 收進**來源樹**的 `refs/heads/<branch>`。

    ## 為什麼 ship phase 需要這條通道

    build phase 的每一張卡在被採信之後都會走 `manager._harvest_build_candidate()`，
    因此

        來源樹的 `refs/heads/<branch>` == `run.candidate_head`

    在每一張 build 卡之後成立。`openspec-archive` 是**唯一會產生 commit 的 ship
    卡**（`policy-commit` 的 `new_head == old_head`，不 commit），而它做完 commit 之後
    `_manager_reset_workflow_after_archive()` 會把 `candidate_head` 推進到那個新
    commit——**但那個 commit 只存在於做 commit 的那棵工作樹裡**。#623 把工作區從
    `git worktree`（共用 object store）換成 per-job 完整 clone 之後，「順便就在來源樹
    裡」這件事不再成立，上面那條不變式因此在 ship phase 斷掉。三個已知後果：

    1. `manager._validated_ship_steps()` 的 `matches_candidate()` 對 post-archive
       repair 允許 `openspec-archive` 的 `subject_head` 是 final candidate 的祖先，
       而那條 ancestry 檢查跑在 `run.workspace_root` 上——來源樹沒有 archive commit
       時 `git merge-base --is-ancestor` 回 128（`Not a valid commit name`），
       `matches_candidate()` 回 False，整個 ship audit fail-closed。
    2. #651 之後 build 卡改成 per-job clone，base ＝ `run.candidate_head`。
       post-archive 的 `retry-build` 因此會拿 archive commit 當 base 去 provision，
       而 `ScriptWorktreeCreator.create()` 的 `rev-parse --verify <base>` 在來源樹
       上找不到它 ⇒ `git worktree base invalid`，重派直接起不來。
    3. 「成果只在磁碟上的某一棵工作區裡」本身就是 #637／#651 要消除的形狀——
       那棵樹被回收（`cortex work gc`）之後 commit 就沒了。

    ## 通道形狀：沿用 #637 的 bundle ＋ append-only spool

    producer 換成 Manager 自己（見 `job_workspace.publish_commit_bundle()`），
    consumer 那一半（`harvest_branch()`）一個位元組不變：**「commit 進來源樹」全
    repo 仍然只有一個實作**，fail-closed 分類、非 fast-forward 拒絕、prerequisite
    診斷全部沿用。刻意不走 `git -C <來源樹> fetch <那棵工作區>`——`job_workspace`
    模組 docstring 已判定那個形狀在三分部署下結構性不成立（Manager 走不進 job 的樹、
    `safe.directory` 不吃路徑 glob），寫回來等於預先埋一顆下一階段必炸的雷。

    ## 回收後的不變式

    來源樹的 `refs/heads/<branch>` 必須**恰等於**剛做出來的 commit，對不上即
    fail-closed——與 `_harvest_build_candidate()` 逐條相同的判準。呼叫端在這之後
    才推進 `candidate_head`，因此「採信值」與「來源樹實況」不會分岔。

    `baseline` 是 bundle 的排除點（＝ archive 之前那個已被採信的 candidate）。
    來源樹沒有它時（升級前既存的 run、或沒走過 build harvest 的路徑）改為不排除，
    bundle 帶完整歷史——寧可多搬一點，也不要因為缺 prerequisite 而讓一次**合法**的
    回收失敗。
    """

    source_repo = getattr(run, "workspace_root", None)
    if not isinstance(source_repo, str) or not source_repo:
        raise RuntimeError("manager ship commit harvest source repo missing")
    # 回收的 refspec 是 `refs/heads/<branch>`——commit 若不在那條 ref 上（detached
    # HEAD、或第三方在 commit 之後動過 ref），bundle 帶的就是別的東西。與
    # `_harvest_build_candidate()` 的 head mismatch 同一條判準，先擋在這裡讓訊息說
    # 得出成因，而不是讓 `git bundle create` 丟一句 `ambiguous argument`。
    if job_workspace.source_branch_head(worktree, branch) != new_head.lower():
        raise RuntimeError("manager ship commit is not on the recorded branch")
    bundle = job_workspace.prepare_commit_spool(
        spool_key=spool_key, coordinator_root=state_root
    )
    job_workspace.publish_commit_bundle(
        workspace=worktree,
        bundle=bundle,
        branch=branch,
        exclude=baseline if job_workspace.commit_present(source_repo, baseline) else None,
    )
    harvested = job_workspace.harvest_branch(
        source_repo=source_repo, bundle=bundle, branch=branch
    )
    if harvested.lower() != new_head.lower():
        raise RuntimeError("manager ship commit harvest head mismatch")
    job_workspace.seal_commit_spool(bundle)
    return harvested


def _record_manager_ship_job(
    *,
    registry,
    state_root: Path,
    run,
    worktree: Path,
    branch: str,
    card: str,
    old_head: str,
    new_head: str,
    job_id: str | None = None,
):
    existing = [
        job
        for job in registry.list_jobs()
        if job.get("workflow_run_id") == run.run_id
        and job.get("workflow_phase") == "ship"
        and job.get("workflow_card") == card
        and job.get("subject_head") == new_head
        and job.get("status") == "exited"
        and job.get("exit_code") == 0
        and isinstance(job.get("workflow_evidence"), dict)
    ]
    if len(existing) == 1:
        return existing[0]
    if existing:
        raise RuntimeError("manager ship card audit is ambiguous")
    job = registry.create_job(
        task=_manager_ship_job_task(run=run, card=card),
        # #649：job_id 由呼叫端先 `reserve_job_id()` 取得——回收那一格的 spool key
        # 就是它，而 spool 必須在 job 之前建立（bundle 得先落地才 harvest 得動）。
        # 順序與 #648 的 build 卡逐條相同：先配 id → 用它定址 → 再建 job。
        job_id=job_id,
        persona="manager",
        kind="build",
        branch=branch,
        pane="",
        worktree=str(worktree),
        dispatch_head=old_head,
        executor="cortex-manager",
        model_id="deterministic",
        independence_domain="cortex",
        subject_head=new_head,
        workflow_run_id=run.run_id,
        workflow_claim_key=run.claim_key,
        workflow_repo=run.repo,
        workflow_card=card,
        workflow_phase="ship",
        workflow_repo_root=str(worktree),
        source_revision=run.source_revision,
    )
    job = registry.update_headless_result(job["job_id"], status="exited", exit_code=0)
    envelope = {
        "schema_version": 1,
        "kind": "ship",
        "job": {
            "job_id": job["job_id"],
            "run_id": run.run_id,
            "claim_key": run.claim_key,
            "repo": run.repo,
            "source_revision": run.source_revision,
            "card_id": card,
            "phase": "ship",
            "inputs": [],
            "outputs": [],
            "output_baseline": [],
        },
        "payload": {
            "schema_version": 1,
            "kind": "workflow-card",
            "status": "passed",
            "run_id": run.run_id,
            "card_id": card,
            "candidate": new_head,
            "outputs": [],
        },
        "artifacts": [],
    }
    content = (
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    relative = Path("evidence") / "workflow" / f"{hashlib.sha256(str(job['job_id']).encode()).hexdigest()}.json"
    target = state_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_symlink() or target.read_bytes() != content:
            raise RuntimeError("manager archive evidence conflict")
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    return registry.bind_workflow_evidence(
        str(job["job_id"]),
        locator={
            "kind": "ship",
            "path": relative.as_posix(),
            "hash": hashlib.sha256(content).hexdigest(),
        },
        subject_head=new_head,
    )


def _commit_archive_and_require_reverification(
    *, registry, state_root: Path, run, authority: WorkAuthority, worktree: Path, branch: str, candidate: str, runner
):
    if len(authority.mapped_openspec) != 1:
        raise RuntimeError("archive commit requires one OpenSpec change")
    change = authority.mapped_openspec[0]
    tracked = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", "HEAD"],
        shell=False,
        capture_output=True,
    )
    untracked = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
        shell=False,
        capture_output=True,
    )
    if tracked.returncode != 0 or untracked.returncode != 0:
        raise RuntimeError("archive diff inspection failed")
    changed = {
        value.decode("utf-8")
        for value in tracked.stdout.split(b"\0") + untracked.stdout.split(b"\0")
        if value
    }
    if not changed or any(not _archive_path_allowed(path, change=change) for path in changed):
        raise RuntimeError("archive diff escaped strict OpenSpec/docs/changelog allowlist")
    added = subprocess.run(
        ["git", "-C", str(worktree), "add", "-A", "--", *sorted(changed)],
        shell=False,
        capture_output=True,
        text=True,
    )
    if added.returncode != 0:
        raise RuntimeError("archive allowlist staging failed")
    staged = subprocess.run(
        ["git", "-C", str(worktree), "diff", "--cached", "--name-only", "--no-renames", "-z"],
        shell=False,
        capture_output=True,
    )
    staged_paths = {value.decode("utf-8") for value in staged.stdout.split(b"\0") if value}
    if staged.returncode != 0 or staged_paths != changed:
        raise RuntimeError("archive staged diff differs from inspected allowlist")
    committed = subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", f"chore(openspec): archive {change}"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if committed.returncode != 0:
        raise RuntimeError("archive allowlist commit failed")
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        shell=False,
        capture_output=True,
        text=True,
    )
    new_head = head.stdout.strip().lower()
    if head.returncode != 0 or verification.SAFE_SHA_RE.fullmatch(new_head) is None or new_head == candidate:
        raise RuntimeError("archive commit did not produce a new exact Candidate")
    # #649：ship phase 的**成果回收**。這一步排在 `_record_manager_ship_job()` 與
    # `_manager_reset_workflow_after_archive()` **之前**：candidate_head 一旦推進到
    # archive commit，整條鏈（ship audit 的 ancestry、post-archive retry-build 的
    # clone base、下一張卡的 handoff base）就會假設來源樹有它。回收失敗時必須在推
    # 進之前 fail-closed，而不是先推進再讓後面某一段以看不懂的訊息炸開。
    job_id = registry.reserve_job_id(
        _manager_ship_job_task(run=run, card="openspec-archive")
    )
    _harvest_manager_ship_commit(
        state_root=state_root,
        run=run,
        worktree=worktree,
        branch=branch,
        spool_key=job_id,
        baseline=candidate,
        new_head=new_head,
    )
    _record_manager_ship_job(
        registry=registry,
        state_root=state_root,
        run=run,
        worktree=worktree,
        branch=branch,
        card="openspec-archive",
        old_head=candidate,
        new_head=new_head,
        job_id=job_id,
    )
    return registry._manager_reset_workflow_after_archive(
        run.run_id,
        candidate_head=new_head,
    )


def _authority_with_manager_pr(authority: WorkAuthority, pr_number: int) -> WorkAuthority:
    pr_ref = f"{authority.repo}#{pr_number}"
    source_revisions = set(authority.source_revisions)
    source_prefix = f"github_pr:{pr_ref}@"
    if not any(value.startswith(source_prefix) for value in source_revisions):
        source_revisions.add(f"{source_prefix}identity:{pr_ref};state:open")
    return WorkAuthority._verified(
        repo=authority.repo,
        work_id=authority.work_id,
        mapped_issues=authority.mapped_issues,
        mapped_prs=(pr_number,),
        mapped_openspec=authority.mapped_openspec,
        mapped_todo_paths=authority.mapped_todo_paths,
        confirmed_todo=authority.confirmed_todo,
        auto_label=authority.auto_label,
        source_revisions=tuple(sorted(source_revisions)),
        provider_revision=authority.github_provider_revision,
        provider_id=authority.github_provider_id,
        last_success_epoch=authority.github_last_success_epoch,
        snapshot_hash=authority.snapshot_hash,
    )


def _workflow_evidence_envelope(
    *,
    registry,
    state_root: Path,
    run,
    phase: str,
    expected_ref: str | None = None,
    expected_hash: str | None = None,
) -> tuple[dict, dict]:
    rows = [
        row
        for row in registry.list_jobs()
        if row.get("workflow_run_id") == run.run_id
        and row.get("workflow_phase") == phase
        and row.get("status") == "exited"
        and row.get("exit_code") == 0
        and isinstance(row.get("workflow_evidence"), dict)
        and (
            phase not in {"verify", "review"}
            or row.get("subject_head") == run.candidate_head
        )
    ]
    if expected_ref is not None:
        rows = [
            row
            for row in rows
            if str(
                (
                    state_root / str(row["workflow_evidence"]["path"])
                ).resolve()
            )
            == str(Path(expected_ref).resolve())
        ]
    if len(rows) != 1:
        raise RuntimeError(f"delivery requires one canonical {phase} evidence job")
    job = rows[0]
    locator = job["workflow_evidence"]
    relative = Path(str(locator.get("path")))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("workflow evidence locator escapes coordinator root")
    path = (state_root / relative).resolve(strict=True)
    path.relative_to(state_root)
    content = path.read_bytes()
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != locator.get("hash") or (
        expected_hash is not None and actual_hash != expected_hash
    ):
        raise RuntimeError("workflow evidence hash drift")
    envelope = json.loads(content.decode("utf-8"))
    expected_job = {
        "job_id": job["job_id"],
        "run_id": job["workflow_run_id"],
        "claim_key": job["workflow_claim_key"],
        "repo": job["workflow_repo"],
        "source_revision": job["source_revision"],
        "card_id": job["workflow_card"],
        "phase": job["workflow_phase"],
        "inputs": job.get("workflow_inputs", []),
        "outputs": job.get("workflow_outputs", []),
        "output_baseline": job.get("workflow_output_baseline", []),
    }
    envelope_job = envelope.get("job") if isinstance(envelope, dict) else None
    if job.get("workflow_input_snapshot") or (
        isinstance(envelope_job, dict) and "input_snapshot" in envelope_job
    ):
        expected_job["input_snapshot"] = job.get("workflow_input_snapshot", [])
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version") != 1
        or envelope.get("kind") != phase
        or envelope.get("job") != expected_job
        or not isinstance(envelope.get("artifacts"), list)
        or not isinstance(payload, dict)
    ):
        raise RuntimeError("workflow evidence envelope malformed")
    return envelope, job


def _workflow_evidence_payload(
    *,
    registry,
    state_root: Path,
    run,
    phase: str,
    expected_ref: str | None = None,
    expected_hash: str | None = None,
) -> tuple[dict, dict]:
    envelope, job = _workflow_evidence_envelope(
        registry=registry,
        state_root=state_root,
        run=run,
        phase=phase,
        expected_ref=expected_ref,
        expected_hash=expected_hash,
    )
    payload = envelope["payload"]
    payload = dict(payload)
    payload.pop("outputs", None)
    return payload, job


def _candidate_skip_tests_request(
    *, worktree: Path, candidate: str, now
) -> bool:
    """#760：candidate 的全套綠已由 gate 環境獨立驗過時，delivery 請求 --skip-tests。

    manager 環境是第三個 env-red 執行面（#723 第五例），在此第三跑全套是把已驗訊號
    變成結構性 block。這裡只做預判（tree-hash 定址＋age）；最終驗證由
    `preflight.build_preflight_argv` 的既有 `--skip-tests` 契約把關。任何讀取失敗
    一律 False——行為退回「老老實實再跑一次」。
    """

    try:
        tree = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", f"{candidate}^{{tree}}"],
            shell=False,
            capture_output=True,
            text=True,
        )
        tree_hash = tree.stdout.strip().lower()
        if tree.returncode != 0:
            return False
        now_epoch = now()
        evidence = load_fresh_full_suite_evidence(
            tree_hash=tree_hash, now_epoch=float(now_epoch)
        )
        return evidence is not None
    except Exception:
        return False


def load_fresh_full_suite_evidence(*, tree_hash: str, now_epoch: float):
    from .preflight import fresh_full_suite_evidence

    return fresh_full_suite_evidence(tree_hash=tree_hash, now_epoch=now_epoch)


def _run_exact_candidate_preflight(
    *,
    worktree: Path,
    branch: str,
    candidate: str,
    command: tuple[str, ...],
    request: PreflightRequest,
    runner,
    now,
):
    """Run initial metadata preflight in a clean detached exact-Candidate checkout."""

    if verification.SAFE_SHA_RE.fullmatch(candidate) is None:
        raise ValueError("initial preflight Candidate is invalid")
    branch_match = re.fullmatch(r"feature/([a-z0-9][a-z0-9-]*)", branch)
    if branch_match is None:
        raise ValueError("initial preflight delivery branch violates feature policy")
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0 or head.stdout.strip().lower() != candidate:
        raise RuntimeError("initial preflight requires exact local Candidate HEAD")
    parent = Path(tempfile.mkdtemp(prefix="cortex-preflight-"))
    checkout = parent / "candidate"
    checkout_branch = f"feature/preflight-{candidate[:12]}-{uuid4().hex[:8]}"
    added = False
    try:
        created = subprocess.run(
            [
                "git", "-C", str(worktree), "worktree", "add", "-b",
                checkout_branch, str(checkout), candidate,
            ],
            shell=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            raise RuntimeError("exact Candidate preflight checkout failed")
        added = True
        return run_preflight(
            repo_root=checkout,
            command=command,
            request=request,
            runner=runner,
            now=now,
        )
    finally:
        cleanup_error = False
        if added:
            removed = subprocess.run(
                ["git", "-C", str(worktree), "worktree", "remove", "--force", str(checkout)],
                shell=False,
                capture_output=True,
                text=True,
            )
            cleanup_error = removed.returncode != 0
            if not cleanup_error:
                deleted = subprocess.run(
                    [
                        "git", "-C", str(worktree), "update-ref", "-d",
                        f"refs/heads/{checkout_branch}", candidate,
                    ],
                    shell=False,
                    capture_output=True,
                    text=True,
                )
                cleanup_error = deleted.returncode != 0
        shutil.rmtree(parent, ignore_errors=True)
        if cleanup_error:
            raise RuntimeError("exact Candidate preflight checkout cleanup failed")


def _completion_draft(
    *,
    registry,
    state_root: Path,
    run,
    authority: WorkAuthority,
    candidate: str,
    pr_number: int,
    foreign_ref,
    runner,
    now,
) -> Path | None:
    journal_path = state_root / "delivery-journal.json"
    if not journal_path.is_file() or journal_path.is_symlink():
        return None
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    row = journal.get("runs", {}).get(run.run_id) if isinstance(journal, dict) else None
    ship = row.get("ship") if isinstance(row, dict) else None
    if not isinstance(ship, dict) or ship.get("phase") not in {"merged", "done"}:
        return None
    authorization = ship.get("merge_authorization")
    if not isinstance(authorization, dict):
        raise RuntimeError("merged delivery journal lacks authorization")
    from . import completion, review
    from . import work_actions

    verification_payload, _verify_job = _workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=run,
        phase="verify",
    )
    verification_payload["slice_id"] = run.run_id
    verification_payload["status"] = "reviewing"
    verification_record = verification.write_verification_evidence(
        verification_payload,
        coordinator_root=state_root,
    )
    review_payload, review_job = _workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=run,
        phase="review",
        expected_ref=foreign_ref.ref,
    )
    review_payload["slice_id"] = run.run_id
    review_record = review.write_gate_evaluation(
        review_payload,
        coordinator_root=state_root,
    )
    builder_job_id = review_record["payload"]["builder_job_id"]
    builder_job = registry.get_job(builder_job_id)
    dispatch_base = builder_job.get("dispatch_head")
    branch = builder_job.get("branch")
    if (
        not isinstance(dispatch_base, str)
        or verification.SAFE_SHA_RE.fullmatch(dispatch_base) is None
        or not isinstance(branch, str)
        or not branch
        or review_job.get("job_id") != review_record["payload"]["reviewer_job_id"]
    ):
        raise RuntimeError("workflow completion job binding malformed")
    github = GitHubDeliveryClient(runner=runner)
    closure = github.fetch_remote_closure(
        repo=authority.repo,
        pr_number=pr_number,
        change=authority.mapped_openspec[0],
        required_issues=authority.mapped_issues,
        todo_paths=authority.mapped_todo_paths,
    )
    default_branch = github.fetch_default_branch(repo=authority.repo)
    by_kind: dict[str, list[str]] = {"spec": [], "plan": []}
    for item in run.planning_authority:
        if item.kind in by_kind:
            by_kind[item.kind].append(item.baseline_sha256)
    if not by_kind["spec"] or not by_kind["plan"]:
        raise RuntimeError("completion requires canonical spec and plan authority")
    trusted_refs = work_actions._trusted_evidence_refs(authorization)
    payload = {
        "schema_version": completion.COMPLETION_SCHEMA_VERSION,
        "slice_id": run.run_id,
        "spec_hash": verification.canonical_json_hash(sorted(by_kind["spec"])),
        "plan_hash": verification.canonical_json_hash(sorted(by_kind["plan"])),
        "verification_hash": verification_record["hash"],
        "builder_job_id": builder_job_id,
        "reviewer_job_id": review_record["payload"]["reviewer_job_id"],
        "dispatch_base": dispatch_base,
        "candidate": candidate,
        "target_branch": default_branch,
        "target_remote": "origin",
        "target_ref": f"refs/remotes/origin/{default_branch}",
        "target_ref_sha": closure.default_head,
        "verification_evidence_path": verification_record["path"],
        "verification_evidence_hash": verification_record["hash"],
        "review_policy": "required",
        "docs_class": "code",
        "review_evaluation_path": review_record["path"],
        "review_evaluation_hash": review_record["hash"],
        "completed_at": datetime.fromtimestamp(float(now()), timezone.utc).isoformat(),
        "work_authority": {
            "repo": authority.repo,
            "work_id": authority.work_id,
            "snapshot_hash": authority.snapshot_hash,
            "provider_id": authority.github_provider_id,
            "provider_revision": authority.github_provider_revision,
            "source_revisions": sorted(authority.source_revisions),
            "mapped_issues": sorted(authority.mapped_issues),
            "mapped_prs": sorted(authority.mapped_prs),
            "mapped_openspec": sorted(authority.mapped_openspec),
            "mapped_todo_paths": sorted(authority.mapped_todo_paths),
            "pr_number": pr_number,
            "change": authority.mapped_openspec[0],
            "todo_paths": sorted(authority.mapped_todo_paths),
            "merge_commit": closure.merge_commit,
            "run_id": run.run_id,
            "workflow_step_ids": sorted(row["workflow_step_ids"]),
            "trusted_evidence_refs": [dict(item) for item in trusted_refs],
        },
    }
    # #216：把整趟 run 期間最後一次 retry 的分類（#215/#216 落地在
    # WorkflowRun.retry_classification 上的 provenance）帶進 CompletionRecord，
    # 讓 cortex stat 之類的彙總面可依原因分類；沒有發生過 retry 的 run 保持
    # schema 原本「該欄位不存在」的可選語意，不強塞 None。
    if run.retry_classification is not None:
        payload["retry_classification"] = run.retry_classification
    # #208 收口 wiring 4：sizing 是 work item 屬性（#222 H.2 已進 CompletionRecord
    # schema），比照 retry_classification 的 provenance-only 可選欄位模式寫入。
    # sizing_declaration_drift 需要的 declared_modules（plan 宣告的模組數）目前
    # plan frontmatter 沒有這個宣告欄位，fail-soft 省略整個欄位（見 PR body）。
    if run.sizing_score is not None:
        payload["sizing_score"] = run.sizing_score
        payload["sizing_band"] = run.sizing_band
    normalized = completion.validate_completion_record(payload)
    directory = state_root / "evidence" / "completion-drafts"
    directory.mkdir(parents=True, exist_ok=True)
    semantic_payload = dict(normalized)
    semantic_payload.pop("completed_at")
    semantic_hash = verification.canonical_json_hash(semantic_payload)
    target = directory / f"{run.run_id}-{candidate}-{semantic_hash}.json"

    def reuse_existing() -> Path:
        if target.is_symlink() or not target.is_file():
            raise RuntimeError("completion draft conflict: target is not a regular file")
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            existing_normalized = completion.validate_completion_record(existing)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("completion draft conflict: existing draft is invalid") from exc
        existing_semantic = dict(existing_normalized)
        existing_semantic.pop("completed_at")
        if existing_semantic != semantic_payload:
            raise RuntimeError("completion draft conflict: semantic content mismatch")
        return target

    if target.exists() or target.is_symlink():
        return reuse_existing()
    try:
        verification.atomic_write_json(target, normalized)
    except verification.AtomicWriteConflictError:
        return reuse_existing()
    return target


def _delivery_adapter_status(action: object) -> str:
    if not isinstance(action, str):
        return "pending"
    if action == "done":
        return "passed"
    if action in {"fix-required", "needs_human"}:
        return "needs_human"
    return "pending"


def build_production_ship_validator(
    *,
    registry,
    coordinator_root: str | Path,
    runner: Callable[..., object] = subprocess.run,
    now: Callable[[], float] = time.time,
    snapshot_path: str | Path | None = None,
    workspace_creator=None,
):
    """Bind review completion to the authenticated, resumable delivery state machine.

    `workspace_creator` 是 ship 段那棵 Manager-owned 工作區的 provisioning seam
    （#653）。留 `None` 時由 `_manager_ship_workspace()` 依 `run.workspace_root`
    自行組一個 `seams.ScriptWorktreeCreator`——生產路徑不必也不應該由呼叫端指定
    工作區從哪來。
    """

    state_root = Path(coordinator_root).resolve()

    def validate(*, run, candidate: str | None) -> dict[str, object]:
        terminal_refresh = (
            run.current_phase == "ship" and getattr(run, "status", None) == "done"
        )
        if (
            not isinstance(candidate, str)
            or run.candidate_head != candidate
            or run.verified_head != candidate
            or (run.current_phase != "review" and not terminal_refresh)
        ):
            raise ValueError("ship adapter requires review-complete exact candidate")
        foreign = [ref for ref in run.gate_refs if ref.kind == "foreign-review"]
        if len(foreign) != 1 or foreign[0].sha256 is None:
            raise ValueError("ship adapter requires canonical foreign-review evidence")
        maintainer = [ref for ref in run.gate_refs if ref.kind == "maintainer-review"]
        if len(maintainer) > 1 or (maintainer and maintainer[0].sha256 is None):
            raise ValueError("ship adapter maintainer-review evidence is ambiguous")
        authority = load_work_authority(
            repo=run.repo,
            work_id=run.work_id,
            snapshot_path=snapshot_path,
        )
        expected_issues = tuple(f"{run.repo}#{number}" for number in authority.mapped_issues)
        # #776：openspec 比對走相容判定——authority 多出的 refs 若皆為 run 自產
        # planning 產物（含舊 manifest 的 openspec-propose 慣例名授與），run 身分
        # 未被外部重新定義；run.openspec_refs 是 claim 時快照，authority-restart
        # 不回寫它，全等比對會把合法 ship 擋成 refs-differ。
        if run.issue_refs != expected_issues or not openspec_refs_compatible(run, authority):
            raise RuntimeError("WorkflowRun refs differ from current WorkAuthority")
        branch = _builder_binding(
            registry,
            run,
            candidate,
            state_root=state_root,
            foreign_ref=foreign[0],
        )
        # #653：ship 段從這一行起全部在 **Manager-owned** 的樹裡動手。以下每一段
        # （archive 套用／commit／回收、preflight、push、`_ship_action` 連測試）
        # 拿到的都是這棵樹，**沒有任何一處**再指向 builder 的 clone。
        worktree = _manager_ship_workspace(
            run=run,
            branch=branch,
            candidate=candidate,
            creator=workspace_creator,
        )
        change = authority.mapped_openspec[0] if len(authority.mapped_openspec) == 1 else None
        active_change = worktree / "openspec" / "changes" / str(change) if change else None
        if active_change is not None and active_change.is_dir() and not _manager_archive_applied(run):
            from . import work_actions

            validate_ship_stage_transition("local-closeout", "pr-preflight")
            # #776：舊 manifest 世代的 change 只有 tasks.md——archive gate 的
            # strict validate 會以 Unknown item 結構性失敗；gate 前補齊指標式
            # scaffold（隨 archive commit 收進並經 re-verification 檢視）。
            work_actions._ensure_openspec_change_scaffold(
                repo_root=worktree,
                change=str(change),
            )
            work_actions._validate_local_archive_inputs(
                repo_root=worktree,
                change=str(change),
                runner=runner,
            )
            archived = runner(
                work_actions.build_openspec_archive_argv(str(change)),
                cwd=str(worktree),
                shell=False,
                capture_output=True,
                text=True,
            )
            if getattr(archived, "returncode", None) != 0:
                raise RuntimeError("official OpenSpec archive failed")
            reset = _commit_archive_and_require_reverification(
                registry=registry,
                state_root=state_root,
                run=run,
                authority=authority,
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                runner=runner,
            )
            evidence = _write_json_evidence(
                state_root,
                "delivery-adapter",
                {
                    "schema": "cortex-delivery-adapter/v1",
                    "run_id": run.run_id,
                    "candidate": candidate,
                    "action": "candidate-reverification-required",
                    "next_candidate": reset.candidate_head,
                    "stage": "local-closeout",
                },
            )
            return {
                "trusted": True,
                "status": "pending",
                "head": candidate,
                "commit_id": candidate,
                "reason": "archive-commit-invalidated-candidate-evidence",
                **evidence,
            }
        metadata = _pr_metadata(run)
        metadata_path = _metadata_file(state_root, run, metadata)
        pr_numbers = []
        for ref in run.pr_refs:
            prefix = f"{run.repo}#"
            if not ref.startswith(prefix) or not ref[len(prefix) :].isdigit():
                raise ValueError("workflow PR ref malformed")
            pr_numbers.append(int(ref[len(prefix) :]))
        if len(pr_numbers) > 1:
            raise RuntimeError("workflow delivery supports one PR")
        if not pr_numbers:
            initial = _run_exact_candidate_preflight(
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                command=load_preflight_command(),
                request=PreflightRequest(
                    metadata_path=str(metadata_path),
                    # #760：gate 已於自己的環境獨立跑過全套且綠時請求 --skip-tests。
                    skip_tests=_candidate_skip_tests_request(
                        worktree=worktree, candidate=candidate, now=now
                    ),
                ),
                runner=runner,
                now=now,
            )
            if not initial.passed or initial.head != candidate:
                return _preflight_result_evidence(
                    state_root=state_root,
                    run=run,
                    candidate=candidate,
                    stage="metadata",
                    preflight=initial,
                    status="needs_human",
                    reason="pr-preflight-blocked",
                    next_action="resume-after-preflight-fix",
                )
            _push_exact_candidate(
                registry=registry,
                run=run,
                authority=authority,
                state_root=state_root,
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                runner=runner,
            )
            github = GitHubDeliveryClient(runner=runner)
            number = github.create_or_get_pull_request(
                repo=run.repo,
                branch=branch,
                expected_head=candidate,
                title=str(metadata["title"]),
                body=str(metadata["body"]),
                labels=tuple(metadata["labels"]),
            )
            authority = _authority_with_manager_pr(authority, number)
            updated = registry._manager_update_workflow_run(
                run.run_id,
                source_revision=work_authority_digest(authority),
                pr_refs=(f"{run.repo}#{number}",),
            )
            _rebase_delivery_journal_authority(
                state_root=state_root,
                run=updated,
                authority=authority,
            )
            evidence = _write_json_evidence(
                state_root,
                "delivery-adapter",
                {
                    "schema": "cortex-delivery-adapter/v1",
                    "run_id": updated.run_id,
                    "candidate": candidate,
                    "action": "pr-created",
                    "pr_number": number,
                    "authority_digest": updated.source_revision,
                },
            )
            return {
                "trusted": True,
                "status": "pending",
                "head": candidate,
                "commit_id": candidate,
                **evidence,
            }
        number = pr_numbers[0]
        if authority.mapped_prs not in {(), (number,)}:
            raise RuntimeError("workflow PR differs from current WorkAuthority")
        authority = _authority_with_manager_pr(authority, number)
        if run.source_revision != work_authority_digest(authority):
            run = registry._manager_update_workflow_run(
                run.run_id,
                source_revision=work_authority_digest(authority),
            )
        _rebase_delivery_journal_authority(
            state_root=state_root,
            run=run,
            authority=authority,
        )
        existing = _run_exact_candidate_preflight(
            worktree=worktree,
            branch=branch,
            candidate=candidate,
            command=load_preflight_command(),
            request=PreflightRequest(
                pr_number=number,
                skip_tests=_candidate_skip_tests_request(
                    worktree=worktree, candidate=candidate, now=now
                ),
            ),
            runner=runner,
            now=now,
        )
        if not existing.passed or existing.head != candidate:
            return _preflight_result_evidence(
                state_root=state_root,
                run=run,
                candidate=candidate,
                stage="existing-pr",
                preflight=existing,
                status="needs_human",
                reason="pr-preflight-blocked",
                next_action="resume-after-preflight-fix",
            )

        _push_exact_candidate(
            registry=registry,
            run=run,
            authority=authority,
            state_root=state_root,
            worktree=worktree,
            branch=branch,
            candidate=candidate,
            runner=runner,
        )
        from . import work_actions
        from . import review as review_evidence

        foreign_payload, _foreign_job = _workflow_evidence_payload(
            registry=registry,
            state_root=state_root,
            run=run,
            phase="review",
            expected_ref=foreign[0].ref,
            expected_hash=foreign[0].sha256,
        )
        foreign_record = review_evidence.write_gate_evaluation(
            foreign_payload,
            coordinator_root=state_root,
        )

        completion_draft = _completion_draft(
            registry=registry,
            state_root=state_root,
            run=run,
            authority=authority,
            candidate=candidate,
            pr_number=number,
            foreign_ref=foreign[0],
            runner=runner,
            now=now,
        )
        ship_args = {
            "repo_root": str(worktree),
            "pr_number": number,
            "change": authority.mapped_openspec[0] if len(authority.mapped_openspec) == 1 else None,
            "todo_paths": list(authority.mapped_todo_paths),
            "foreign_review_path": foreign_record["path"],
            "foreign_review_hash": foreign_record["hash"],
            "pr_metadata_path": str(metadata_path),
            "skip_tests": False,
        }
        if maintainer:
            ship_args["maintainer_review_path"] = maintainer[0].ref
            ship_args["maintainer_review_hash"] = maintainer[0].sha256
        if completion_draft is not None:
            ship_args["completion_record_path"] = str(completion_draft)
        action = work_actions._ship_action(
            args=ship_args,
            authority=authority,
            runner=runner,
            now=now,
            state_path=state_root / "delivery-journal.json",
            workflow_registry=registry,
        )
        if action.get("action") == "archive-applied-needs-commit":
            reset = _commit_archive_and_require_reverification(
                registry=registry,
                state_root=state_root,
                run=run,
                authority=authority,
                worktree=worktree,
                branch=branch,
                candidate=candidate,
                runner=runner,
            )
            action = {
                "action": "candidate-reverification-required",
                "head": reset.candidate_head,
                "reason": "archive-commit-invalidated-candidate-evidence",
            }
        if action.get("action") == "done":
            _record_manager_ship_job(
                registry=registry,
                state_root=state_root,
                run=run,
                worktree=worktree,
                branch=branch,
                card="policy-commit",
                old_head=candidate,
                new_head=candidate,
            )
        status = _delivery_adapter_status(action.get("action"))
        evidence = _write_json_evidence(
            state_root,
            "delivery-adapter",
            {
                "schema": "cortex-delivery-adapter/v1",
                "run_id": run.run_id,
                "candidate": candidate,
                "action": action.get("action"),
                "pr_number": number,
            },
        )
        result: dict[str, object] = {
            "trusted": True,
            "status": status,
            "head": candidate,
            "commit_id": candidate,
            "reason": action.get("reason"),
            **evidence,
        }
        if maintainer:
            result.update(
                {
                    "review_kind": "maintainer-review",
                    "review_ref": maintainer[0].ref,
                    "review_hash": maintainer[0].sha256,
                }
            )
        if status == "passed":
            record = action.get("completion_record")
            merge_revision = action.get("merge_commit")
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("path"), str)
                or not isinstance(record.get("hash"), str)
                or not isinstance(merge_revision, str)
            ):
                raise RuntimeError("delivery completion result malformed")
            result["completion"] = {
                "record_path": record["path"],
                "record_hash": record["hash"],
                "record_revision": candidate,
                "source_revisions": {
                    value.rsplit("@", 1)[0]: value.rsplit("@", 1)[1]
                    for value in authority.source_revisions
                    if "@" in value
                },
                "pr_candidate": candidate,
                "merge_revision": merge_revision,
            }
        return result

    return validate
