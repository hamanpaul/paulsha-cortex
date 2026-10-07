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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
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
from .workflow import (
    MAIN_SYNC_REPAIR_SCHEMA,
    MODEL_CHAIN_PERSONAS,
    validate_ship_stage_transition,
)


MAIN_SYNC_UNAVAILABLE_REASON = "main-sync-unavailable"
MAIN_SYNC_PROBE_TIMEOUT_SECONDS = 15.0
MAIN_SYNC_FULL_OBJECT_ID_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")
#: #1142：retry-build 當下取得的 main 釘在來源樹這條 run-scoped ref 上（不動
#: `FETCH_HEAD`／`refs/remotes/origin/main`），harvest 驗證前 M 不會被 gc 掉，
#: 也不會被同一棵來源樹上的其他 fetch 蓋掉。下一次 main-sync retry-build 覆寫它。
MAIN_SYNC_RETRY_PIN_REF_PREFIX = "refs/cortex/main-sync"
#: #1142：harvest 先把 bundle 的 branch 收進這條 run-scoped quarantine ref，驗過
#: 「候選是 retry 當下 M 的後代」才交給 `harvest_branch()` 推進 feature ref。
MAIN_SYNC_QUARANTINE_REF_PREFIX = "refs/cortex/main-sync-quarantine"
MAIN_SYNC_AUTOSYNC_PIN_REF_PREFIX = "refs/cortex/main-sync-autosync"
MAIN_SYNC_RETRY_EVIDENCE_SCHEMA = "cortex-main-sync-retry/v1"
MAIN_SYNC_AUTOSYNC_EVIDENCE_SCHEMA = "cortex-main-sync-autosync/v1"
MAIN_SYNC_AUTOSYNC_REASON = "main-sync-autosync-reverify"
MAIN_SYNC_AUTOSYNC_MAX_DEFAULT = 3
MAIN_SYNC_AUTOSYNC_MAX_BOUND = 10
MAIN_SYNC_AUTOSYNC_TIMEOUT_SECONDS = 120.0
_MAIN_SYNC_REF_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


@dataclass
class MainSyncProbeStageOutcome:
    stage: str
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    error_kind: str | None = None


@dataclass
class MainSyncProbe:
    candidate: str
    main_head: str
    merge_base: str
    relation: str
    conflict_paths: tuple[str, ...] = ()
    path_classification: str | None = None
    raw: dict[str, MainSyncProbeStageOutcome] | None = None


@dataclass
class MainSyncProbeFailure:
    candidate: str
    stage: str
    returncode: int | None
    error_kind: str
    main_head: str | None
    raw: dict[str, MainSyncProbeStageOutcome]


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
    quota_admission_context=None,
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
        planning,
        role="planning",
        context=identities.resolution_context,
        compatibility_for=model_resolution.compatibility_checker_for("planner"),
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
        quota_admission_context=quota_admission_context,
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


def _main_sync_output_bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="surrogateescape")
    return str(value).encode("utf-8", errors="surrogateescape")


def _main_sync_object_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if MAIN_SYNC_FULL_OBJECT_ID_RE.fullmatch(normalized) is None:
        return None
    return normalized


def _run_main_sync_stage(
    *,
    worktree: Path,
    stage: str,
    argv: tuple[str, ...],
    runner: Callable[..., object],
    timeout_seconds: float,
    git_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> MainSyncProbeStageOutcome:
    command = (
        ("git", "-C", str(worktree), *argv)
        if git_dir is None
        else ("git", f"--git-dir={git_dir}", *argv)
    )
    extra: dict[str, object] = {} if env is None else {"env": dict(env)}
    try:
        completed = runner(
            list(command),
            shell=False,
            capture_output=True,
            timeout=timeout_seconds,
            **extra,
        )
    except subprocess.TimeoutExpired as exc:
        return MainSyncProbeStageOutcome(
            stage=stage,
            argv=command,
            returncode=None,
            stdout=_main_sync_output_bytes(exc.stdout),
            stderr=_main_sync_output_bytes(exc.stderr),
            error_kind="timeout",
        )
    except OSError as exc:
        return MainSyncProbeStageOutcome(
            stage=stage,
            argv=command,
            returncode=None,
            stdout=b"",
            stderr=str(exc).encode("utf-8", errors="replace"),
            error_kind="launch-failed",
        )
    return MainSyncProbeStageOutcome(
        stage=stage,
        argv=command,
        returncode=int(getattr(completed, "returncode", 1)),
        stdout=_main_sync_output_bytes(getattr(completed, "stdout", b"")),
        stderr=_main_sync_output_bytes(getattr(completed, "stderr", b"")),
    )


def _main_sync_failure(
    *,
    candidate: str,
    stage: str,
    outcome: MainSyncProbeStageOutcome,
    error_kind: str,
    raw: dict[str, MainSyncProbeStageOutcome],
    main_head: str | None,
) -> MainSyncProbeFailure:
    return MainSyncProbeFailure(
        candidate=candidate,
        stage=stage,
        returncode=outcome.returncode,
        error_kind=error_kind,
        main_head=main_head,
        raw=dict(raw),
    )


def _main_sync_command_failure_kind(stage: str, outcome: MainSyncProbeStageOutcome) -> str:
    if outcome.error_kind is not None:
        return outcome.error_kind
    text = (
        f"{outcome.stdout.decode('utf-8', errors='replace')}\n"
        f"{outcome.stderr.decode('utf-8', errors='replace')}"
    ).lower()
    if stage == "merge-tree" and (
        outcome.returncode == 129 or "unknown option" in text or "usage:" in text
    ):
        return "unsupported-option"
    return "command-failed"


def _parse_main_sync_merge_tree(
    outcome: MainSyncProbeStageOutcome,
) -> tuple[str, tuple[str, ...]]:
    separator = outcome.stdout.find(b"\0")
    if separator <= 0:
        raise ValueError("merge-tree output missing tree separator")
    tree_oid = _main_sync_object_id(
        outcome.stdout[:separator].decode("ascii", errors="strict")
    )
    if tree_oid is None:
        raise ValueError("merge-tree tree object malformed")
    remainder = outcome.stdout[separator + 1 :]
    if outcome.returncode == 0:
        if remainder:
            raise ValueError("clean merge-tree output carried trailing records")
        return tree_oid, ()
    if outcome.returncode != 1:
        raise ValueError("merge-tree output parsed for unexpected returncode")
    if not remainder:
        return tree_oid, ()
    parts = remainder.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    if any(part == b"" for part in parts):
        raise ValueError("merge-tree conflicted file list malformed")
    return tree_oid, tuple(
        part.decode("utf-8", errors="surrogateescape") for part in parts
    )


def _git_show_text(
    worktree: Path,
    *,
    revision: str,
    path: str,
    runner: Callable[..., object],
    timeout_seconds: float,
) -> str | None:
    outcome = _run_main_sync_stage(
        worktree=worktree,
        stage="path-classifier",
        argv=("show", f"{revision}:{path}"),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if outcome.error_kind is not None or outcome.returncode != 0:
        return None
    try:
        return outcome.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _split_unreleased_section(text: str) -> tuple[str, str, str] | None:
    match = re.search(r"(?ms)^## \[Unreleased\]\s*(.*?)(?=^## |\Z)", text)
    if match is None:
        return None
    return text[: match.start(1)], match.group(1), text[match.end(1) :]


def _is_changelog_top_insert_conflict(
    *,
    worktree: Path,
    merge_base: str,
    candidate: str,
    main_head: str,
    runner: Callable[..., object],
    timeout_seconds: float,
) -> bool:
    base_text = _git_show_text(
        worktree,
        revision=merge_base,
        path="CHANGELOG.md",
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    candidate_text = _git_show_text(
        worktree,
        revision=candidate,
        path="CHANGELOG.md",
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    main_text = _git_show_text(
        worktree,
        revision=main_head,
        path="CHANGELOG.md",
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if base_text is None or candidate_text is None or main_text is None:
        return False
    base_parts = _split_unreleased_section(base_text)
    candidate_parts = _split_unreleased_section(candidate_text)
    main_parts = _split_unreleased_section(main_text)
    if base_parts is None or candidate_parts is None or main_parts is None:
        return False
    base_prefix, base_body, base_suffix = base_parts
    candidate_prefix, candidate_body, candidate_suffix = candidate_parts
    main_prefix, main_body, main_suffix = main_parts
    return bool(
        candidate_prefix == main_prefix == base_prefix
        and candidate_suffix == main_suffix == base_suffix
        and candidate_body != base_body
        and main_body != base_body
        and candidate_body.endswith(base_body)
        and main_body.endswith(base_body)
    )


def _classify_main_sync_conflicts(
    *,
    worktree: Path,
    merge_base: str,
    candidate: str,
    main_head: str,
    conflict_paths: tuple[str, ...],
    runner: Callable[..., object],
    timeout_seconds: float,
) -> str:
    if not conflict_paths:
        return "conflict"
    if len(conflict_paths) != 1:
        return "multiple-conflicts"
    if conflict_paths[0] == "CHANGELOG.md" and _is_changelog_top_insert_conflict(
        worktree=worktree,
        merge_base=merge_base,
        candidate=candidate,
        main_head=main_head,
        runner=runner,
        timeout_seconds=timeout_seconds,
    ):
        return "changelog-top-insert"
    return "conflict"


def _main_sync_objects_dir(stdout: bytes) -> Path | None:
    """`rev-parse --path-format=absolute --git-path objects` 的輸出 → 既存的絕對目錄。"""

    text = stdout.decode("utf-8", errors="surrogateescape")
    if text.endswith("\n"):
        text = text[:-1]
    if not text or "\n" in text or "\0" in text:
        return None
    objects_dir = Path(text)
    if not objects_dir.is_absolute() or not objects_dir.is_dir():
        return None
    return objects_dir


def _create_isolated_merge_git_dir(*, objects_dir: Path, sha256: bool) -> Path:
    """#1142：merge-tree 專用、用完即刪的私有 bare GIT_DIR。

    不走 `git init`（template 可能帶 `info/attributes`），直接寫最小 bare repo：
    HEAD、refs/、只含 `repositoryformatversion`／`bare`（sha256 另加
    `extensions.objectformat`）的 config，以及指向來源 repo object store 的
    `objects/info/alternates`——唯讀共用、不複製。沒有 `info/attributes`，沒有
    工作樹。`tempfile.mkdtemp` 以 0700 建立，呼叫端負責刪除。
    """

    root = Path(tempfile.mkdtemp(prefix="cortex-main-sync-"))
    try:
        (root / "objects" / "info").mkdir(parents=True)
        (root / "refs" / "heads").mkdir(parents=True)
        (root / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        config = (
            "[core]\n"
            f"\trepositoryformatversion = {1 if sha256 else 0}\n"
            "\tbare = true\n"
        )
        if sha256:
            config += "[extensions]\n\tobjectformat = sha256\n"
        (root / "config").write_text(config, encoding="utf-8")
        (root / "objects" / "info" / "alternates").write_text(
            f"{objects_dir}\n", encoding="utf-8"
        )
    except OSError:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def _main_sync_isolated_git_env() -> dict[str, str]:
    """#1142：merge-tree 的隔離環境——剝掉呼叫端所有 `GIT_*`，關掉 system／global
    config 與 system attributes；全域 attributes 檔另以 `-c core.attributesFile`
    關掉（它的預設值是 XDG 路徑，不隨 `GIT_CONFIG_GLOBAL` 消失）。"""

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    return env


def _main_sync_autosync_git_identity(source_repo: Path) -> tuple[str, str] | None:
    """Resolve the identity for a Manager-owned clean-behind merge.

    ``PSC_MAIN_SYNC_GIT_IDENTITY`` accepts the conventional ``Name <email>`` form
    and takes precedence. Otherwise resolve the source checkout's effective Git
    config before entering the isolated merge environment.
    """

    configured = os.environ.get("PSC_MAIN_SYNC_GIT_IDENTITY")
    if configured is not None:
        if any(char in configured for char in "\r\n\0"):
            return None
        match = re.fullmatch(r"([^<>]+?)\s*<([^<>]+)>", configured.strip())
        if match is None:
            return None
        name, email = (part.strip() for part in match.groups())
        return (name, email) if name and email else None

    values: list[str] = []
    identity_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        }
    }
    try:
        for key in ("user.name", "user.email"):
            result = subprocess.run(
                ["git", "-C", str(source_repo), "config", "--get", key],
                shell=False,
                capture_output=True,
                text=True,
                timeout=MAIN_SYNC_AUTOSYNC_TIMEOUT_SECONDS,
                env=identity_env,
            )
            if result.returncode != 0:
                return None
            value = result.stdout.strip()
            if not value or any(char in value for char in "\r\n\0"):
                return None
            values.append(value)
    except (OSError, subprocess.SubprocessError):
        return None
    return values[0], values[1]


def _probe_main_sync(
    *,
    worktree: Path,
    candidate: str,
    runner: Callable[..., object] = subprocess.run,
    timeout_seconds: float = MAIN_SYNC_PROBE_TIMEOUT_SECONDS,
) -> MainSyncProbe | MainSyncProbeFailure:
    raw: dict[str, MainSyncProbeStageOutcome] = {}
    candidate_resolve = _run_main_sync_stage(
        worktree=worktree,
        stage="candidate-resolve",
        argv=("rev-parse", "--verify", "--quiet", candidate),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    raw[candidate_resolve.stage] = candidate_resolve
    if candidate_resolve.error_kind is not None or candidate_resolve.returncode != 0:
        return _main_sync_failure(
            candidate=candidate,
            stage=candidate_resolve.stage,
            outcome=candidate_resolve,
            error_kind=_main_sync_command_failure_kind(
                candidate_resolve.stage, candidate_resolve
            ),
            raw=raw,
            main_head=None,
        )
    resolved_candidate = candidate_resolve.stdout.decode(
        "ascii", errors="replace"
    ).strip().lower()
    candidate_validate = _run_main_sync_stage(
        worktree=worktree,
        stage="candidate-validate",
        argv=("cat-file", "-t", resolved_candidate),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    raw[candidate_validate.stage] = candidate_validate
    if candidate_validate.error_kind is not None or candidate_validate.returncode != 0:
        return _main_sync_failure(
            candidate=candidate,
            stage=candidate_validate.stage,
            outcome=candidate_validate,
            error_kind=_main_sync_command_failure_kind(
                candidate_validate.stage, candidate_validate
            ),
            raw=raw,
            main_head=None,
        )
    candidate_oid = _main_sync_object_id(resolved_candidate)
    if candidate_oid is None or candidate_oid != candidate.lower():
        return _main_sync_failure(
            candidate=candidate,
            stage=candidate_validate.stage,
            outcome=candidate_validate,
            error_kind="bad-sha",
            raw=raw,
            main_head=None,
        )
    if candidate_validate.stdout.decode("utf-8", errors="replace").strip() != "commit":
        return _main_sync_failure(
            candidate=candidate,
            stage=candidate_validate.stage,
            outcome=candidate_validate,
            error_kind="non-commit",
            raw=raw,
            main_head=None,
        )

    fetch = _run_main_sync_stage(
        worktree=worktree,
        stage="fetch",
        argv=("fetch", "--quiet", "--no-tags", "origin", "main"),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    raw[fetch.stage] = fetch
    if fetch.error_kind is not None or fetch.returncode != 0:
        return _main_sync_failure(
            candidate=candidate,
            stage=fetch.stage,
            outcome=fetch,
            error_kind=_main_sync_command_failure_kind(fetch.stage, fetch),
            raw=raw,
            main_head=None,
        )

    fetch_head_resolve = _run_main_sync_stage(
        worktree=worktree,
        stage="fetch-head-resolve",
        argv=("rev-parse", "--verify", "--quiet", "FETCH_HEAD"),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    raw[fetch_head_resolve.stage] = fetch_head_resolve
    if fetch_head_resolve.error_kind is not None or fetch_head_resolve.returncode != 0:
        return _main_sync_failure(
            candidate=candidate,
            stage=fetch_head_resolve.stage,
            outcome=fetch_head_resolve,
            error_kind=_main_sync_command_failure_kind(
                fetch_head_resolve.stage, fetch_head_resolve
            ),
            raw=raw,
            main_head=None,
        )
    resolved_main_head = fetch_head_resolve.stdout.decode(
        "ascii", errors="replace"
    ).strip().lower()
    fetch_head_validate = _run_main_sync_stage(
        worktree=worktree,
        stage="fetch-head-validate",
        argv=("cat-file", "-t", resolved_main_head),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    raw[fetch_head_validate.stage] = fetch_head_validate
    if fetch_head_validate.error_kind is not None or fetch_head_validate.returncode != 0:
        return _main_sync_failure(
            candidate=candidate,
            stage=fetch_head_validate.stage,
            outcome=fetch_head_validate,
            error_kind=_main_sync_command_failure_kind(
                fetch_head_validate.stage, fetch_head_validate
            ),
            raw=raw,
            main_head=None,
        )
    main_head = _main_sync_object_id(resolved_main_head)
    if main_head is None:
        return _main_sync_failure(
            candidate=candidate,
            stage=fetch_head_validate.stage,
            outcome=fetch_head_validate,
            error_kind="bad-sha",
            raw=raw,
            main_head=None,
        )
    if fetch_head_validate.stdout.decode("utf-8", errors="replace").strip() != "commit":
        return _main_sync_failure(
            candidate=candidate,
            stage=fetch_head_validate.stage,
            outcome=fetch_head_validate,
            error_kind="non-commit",
            raw=raw,
            main_head=None,
        )

    merge_base = _run_main_sync_stage(
        worktree=worktree,
        stage="merge-base",
        argv=("merge-base", candidate_oid, main_head),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    raw[merge_base.stage] = merge_base
    if merge_base.error_kind is not None or merge_base.returncode != 0:
        return _main_sync_failure(
            candidate=candidate,
            stage=merge_base.stage,
            outcome=merge_base,
            error_kind=_main_sync_command_failure_kind(merge_base.stage, merge_base),
            raw=raw,
            main_head=main_head,
        )
    merge_base_oid = _main_sync_object_id(
        merge_base.stdout.decode("ascii", errors="replace")
    )
    if merge_base_oid is None:
        return _main_sync_failure(
            candidate=candidate,
            stage=merge_base.stage,
            outcome=merge_base,
            error_kind="output-malformed",
            raw=raw,
            main_head=main_head,
        )
    if merge_base_oid == main_head:
        return MainSyncProbe(
            candidate=candidate_oid,
            main_head=main_head,
            merge_base=merge_base_oid,
            relation="in-sync",
            raw=dict(raw),
        )

    # #1142：merge driver 由 attributes 決定（repo 追蹤的 `.gitattributes` 宣告
    # `CHANGELOG.md merge=union`），判定只能看 M（目標分支已合入、已審的版本）
    # 的 tree。`--attr-source=<M>` 只取代工作樹 `.gitattributes`；git 仍會疊上
    # `$GIT_DIR/info/attributes`、`core.attributesFile`（含 XDG 預設）與 system
    # attributes，任一處把 README 之類宣告成 union 都能把真衝突矇成 clean。因此
    # merge-tree 改在用完即刪的私有暫存 GIT_DIR 裡跑：object store 以 alternates
    # 唯讀共用來源 repo（不複製、merge 結果 tree 只寫進暫存 repo），環境剝掉
    # 呼叫端的 `GIT_*` 並設 `GIT_CONFIG_NOSYSTEM`／`GIT_CONFIG_GLOBAL=/dev/null`／
    # `GIT_ATTR_NOSYSTEM`，再以 `-c core.attributesFile=/dev/null` 關掉全域
    # attributes 檔。任何一步失敗都是 probe failure（`main-sync-unavailable`）。
    # 需要 git >= 2.43：全域 `--attr-source` 自 2.41 起才有，而 merge-tree 搭配它
    # 在 2.43 之前會 segfault——舊版一律落成 merge-tree stage failure，fail-closed，
    # 不會誤判 in-sync。
    objects_stage = _run_main_sync_stage(
        worktree=worktree,
        stage="merge-tree-objects",
        argv=("rev-parse", "--path-format=absolute", "--git-path", "objects"),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    raw[objects_stage.stage] = objects_stage
    if objects_stage.error_kind is not None or objects_stage.returncode != 0:
        return _main_sync_failure(
            candidate=candidate,
            stage=objects_stage.stage,
            outcome=objects_stage,
            error_kind=_main_sync_command_failure_kind(objects_stage.stage, objects_stage),
            raw=raw,
            main_head=main_head,
        )
    objects_dir = _main_sync_objects_dir(objects_stage.stdout)
    if objects_dir is None:
        raw[objects_stage.stage] = MainSyncProbeStageOutcome(
            stage=objects_stage.stage,
            argv=objects_stage.argv,
            returncode=objects_stage.returncode,
            stdout=objects_stage.stdout,
            stderr=objects_stage.stderr,
            error_kind="output-malformed",
        )
        return _main_sync_failure(
            candidate=candidate,
            stage=objects_stage.stage,
            outcome=raw[objects_stage.stage],
            error_kind="output-malformed",
            raw=raw,
            main_head=main_head,
        )
    try:
        isolated_git_dir = _create_isolated_merge_git_dir(
            objects_dir=objects_dir,
            sha256=len(main_head) == 64,
        )
    except OSError as exc:
        isolation = MainSyncProbeStageOutcome(
            stage="merge-tree-isolation",
            argv=(),
            returncode=None,
            stdout=b"",
            stderr=str(exc).encode("utf-8", errors="replace"),
            error_kind="isolation-failed",
        )
        raw[isolation.stage] = isolation
        return _main_sync_failure(
            candidate=candidate,
            stage=isolation.stage,
            outcome=isolation,
            error_kind="isolation-failed",
            raw=raw,
            main_head=main_head,
        )
    try:
        merge_tree = _run_main_sync_stage(
            worktree=worktree,
            stage="merge-tree",
            git_dir=isolated_git_dir,
            env=_main_sync_isolated_git_env(),
            argv=(
                "-c",
                f"core.attributesFile={os.devnull}",
                f"--attr-source={main_head}",
                "merge-tree",
                "--write-tree",
                "--name-only",
                "--no-messages",
                "-z",
                f"--merge-base={merge_base_oid}",
                candidate_oid,
                main_head,
            ),
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
    finally:
        shutil.rmtree(isolated_git_dir, ignore_errors=True)
    raw[merge_tree.stage] = merge_tree
    if merge_tree.error_kind is not None or merge_tree.returncode not in {0, 1}:
        return _main_sync_failure(
            candidate=candidate,
            stage=merge_tree.stage,
            outcome=merge_tree,
            error_kind=_main_sync_command_failure_kind(merge_tree.stage, merge_tree),
            raw=raw,
            main_head=main_head,
        )
    path_parser = MainSyncProbeStageOutcome(
        stage="path-parser",
        argv=merge_tree.argv,
        returncode=merge_tree.returncode,
        stdout=merge_tree.stdout,
        stderr=merge_tree.stderr,
    )
    raw[path_parser.stage] = path_parser
    try:
        _tree_oid, conflict_paths = _parse_main_sync_merge_tree(merge_tree)
    except ValueError:
        raw[path_parser.stage] = MainSyncProbeStageOutcome(
            stage=path_parser.stage,
            argv=path_parser.argv,
            returncode=path_parser.returncode,
            stdout=path_parser.stdout,
            stderr=path_parser.stderr,
            error_kind="output-malformed",
        )
        return _main_sync_failure(
            candidate=candidate,
            stage=path_parser.stage,
            outcome=raw[path_parser.stage],
            error_kind="output-malformed",
            raw=raw,
            main_head=main_head,
        )
    if merge_tree.returncode == 0:
        return MainSyncProbe(
            candidate=candidate_oid,
            main_head=main_head,
            merge_base=merge_base_oid,
            relation="clean-behind",
            raw=dict(raw),
        )
    return MainSyncProbe(
        candidate=candidate_oid,
        main_head=main_head,
        merge_base=merge_base_oid,
        relation="conflict",
        conflict_paths=conflict_paths,
        path_classification=_classify_main_sync_conflicts(
            worktree=worktree,
            merge_base=merge_base_oid,
            candidate=candidate_oid,
            main_head=main_head,
            conflict_paths=conflict_paths,
            runner=runner,
            timeout_seconds=timeout_seconds,
        ),
        raw=dict(raw),
    )


def _main_sync_probe_payload(
    probe: MainSyncProbe | MainSyncProbeFailure,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "cortex-main-sync-probe/v1",
        "candidate": probe.candidate,
        "main_head": probe.main_head,
    }
    if isinstance(probe, MainSyncProbeFailure):
        payload.update(
            {
                "outcome": "failure",
                "stage": probe.stage,
                "returncode": probe.returncode,
                "error_kind": probe.error_kind,
            }
        )
        return payload
    payload.update(
        {
            "outcome": probe.relation,
            "merge_base": probe.merge_base,
            "conflict_paths": list(probe.conflict_paths),
            "path_classification": probe.path_classification,
        }
    )
    return payload


def _main_sync_stop_result(
    *,
    state_root: Path,
    probe: MainSyncProbe | MainSyncProbeFailure,
) -> dict[str, object]:
    payload = _main_sync_probe_payload(probe)
    evidence = _write_json_evidence(state_root, "main-sync-probe", payload)
    if isinstance(probe, MainSyncProbeFailure):
        reason = MAIN_SYNC_UNAVAILABLE_REASON
    elif probe.relation == "clean-behind":
        reason = "candidate-behind-main"
    else:
        reason = "candidate-conflicts-with-main"
    return {
        "trusted": True,
        "status": "needs_human",
        "head": probe.candidate,
        "commit_id": probe.candidate,
        "reason": reason,
        "main_sync": payload,
        **evidence,
    }


def _main_sync_autosync_limit() -> int:
    raw = os.environ.get("PSC_MAIN_SYNC_AUTOSYNC_MAX")
    if raw is None:
        return MAIN_SYNC_AUTOSYNC_MAX_DEFAULT
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", raw) is None:
        raise ValueError("PSC_MAIN_SYNC_AUTOSYNC_MAX must be an integer from 0 to 10")
    value = int(raw)
    if value > MAIN_SYNC_AUTOSYNC_MAX_BOUND:
        raise ValueError("PSC_MAIN_SYNC_AUTOSYNC_MAX must be an integer from 0 to 10")
    return value


def _main_sync_autosync_completed_count(events: Iterable[Mapping[str, object]]) -> int:
    """The limit counts successful syncs that create a new Candidate.

    A failed merge does not move the Candidate and therefore does not consume the
    per-run clean-behind movement budget.
    """

    return sum(event.get("outcome") == "merged" for event in events)


def _read_main_sync_autosync_events(
    *, state_root: Path, run_id: str, evidence_refs: Iterable[str]
) -> list[dict[str, object]]:
    directory = state_root.resolve() / "evidence" / "main-sync-autosync"
    if directory.is_symlink():
        raise RuntimeError("main-sync autosync evidence directory is a symlink")
    events: list[dict[str, object]] = []
    required = {
        "schema",
        "run_id",
        "outcome",
        "sync_number",
        "candidate_before",
        "main_head",
        "candidate_after",
        "review_candidate",
        "review_evidence_ref",
        "review_evidence_hash",
    }
    optional = {"failure_reason", "failure_detail", "observed_main_head", "limit"}
    allowed_outcomes = {
        "merged",
        "limit-exceeded",
        "autosync-disabled",
        "autosync-limit-invalid",
        "main-advanced-during-sync",
        "merge-failed",
        "harvest-failed",
    }
    for reference in evidence_refs:
        if not isinstance(reference, str) or "/evidence/main-sync-autosync/" not in reference:
            continue
        path = Path(reference)
        if (
            not path.is_absolute()
            or path.parent != directory
            or path.is_symlink()
            or not path.is_file()
            or re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is None
        ):
            raise RuntimeError("main-sync autosync evidence reference is invalid")
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("main-sync autosync evidence is unreadable") from exc
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"payload", "hash"}
            or not isinstance(payload, dict)
            or not required <= set(payload)
            or set(payload) - required - optional
            or payload.get("schema") != MAIN_SYNC_AUTOSYNC_EVIDENCE_SCHEMA
            or payload.get("run_id") != run_id
            or payload.get("outcome") not in allowed_outcomes
            or isinstance(payload.get("sync_number"), bool)
            or not isinstance(payload.get("sync_number"), int)
            or payload["sync_number"] < 1
            or not isinstance(payload.get("review_evidence_ref"), str)
            or not payload["review_evidence_ref"]
            or not isinstance(payload.get("review_evidence_hash"), str)
            or re.fullmatch(r"[0-9a-f]{64}", payload["review_evidence_hash"]) is None
        ):
            raise RuntimeError("main-sync autosync evidence payload is malformed")
        for key in ("candidate_before", "review_candidate", "main_head"):
            value = payload.get(key)
            if not isinstance(value, str) or MAIN_SYNC_FULL_OBJECT_ID_RE.fullmatch(value) is None:
                raise RuntimeError("main-sync autosync evidence object id is malformed")
        after = payload.get("candidate_after")
        if payload["outcome"] == "merged":
            if not isinstance(after, str) or MAIN_SYNC_FULL_OBJECT_ID_RE.fullmatch(after) is None:
                raise RuntimeError("main-sync autosync result candidate is malformed")
        elif after is not None:
            raise RuntimeError("failed main-sync autosync evidence has a candidate result")
        observed = payload.get("observed_main_head")
        if observed is not None and (
            not isinstance(observed, str)
            or MAIN_SYNC_FULL_OBJECT_ID_RE.fullmatch(observed) is None
        ):
            raise RuntimeError("main-sync autosync observed main id is malformed")
        limit_value = payload.get("limit")
        if limit_value is not None and (
            isinstance(limit_value, bool)
            or not isinstance(limit_value, int)
            or not 0 <= limit_value <= MAIN_SYNC_AUTOSYNC_MAX_BOUND
        ):
            raise RuntimeError("main-sync autosync limit is malformed")
        digest = verification.canonical_json_hash(payload)
        if envelope.get("hash") != digest or path.stem != digest:
            raise RuntimeError("main-sync autosync evidence hash mismatch")
        events.append(dict(payload))
    return events


def _main_sync_autosync_chain(
    *,
    events: Iterable[Mapping[str, object]],
    candidate: str,
    review_ref: str,
    review_hash: str,
    source_repo: str | Path,
) -> list[dict[str, object]]:
    successful = [
        dict(event)
        for event in events
        if event.get("outcome") == "merged"
        and event.get("review_evidence_ref") == review_ref
        and event.get("review_evidence_hash") == review_hash
    ]
    expected = candidate.lower()
    reverse_chain: list[dict[str, object]] = []
    for event in reversed(successful):
        after = str(event["candidate_after"]).lower()
        if after != expected:
            if reverse_chain:
                break
            continue
        reverse_chain.append(event)
        expected = str(event["candidate_before"]).lower()
    if not reverse_chain:
        return []
    chain = list(reversed(reverse_chain))
    first = chain[0]
    original_review_candidate = str(first["review_candidate"]).lower()
    if (
        str(first["candidate_before"]).lower() != original_review_candidate
        or any(str(event["review_candidate"]).lower() != original_review_candidate for event in chain)
        or any(
            str(chain[index]["candidate_after"]).lower()
            != str(chain[index + 1]["candidate_before"]).lower()
            for index in range(len(chain) - 1)
        )
    ):
        raise RuntimeError("main-sync autosync candidate chain is discontinuous")
    repo = str(source_repo)
    for event in chain:
        candidate_after = str(event["candidate_after"]).lower()
        expected_parents = [
            candidate_after,
            str(event["candidate_before"]).lower(),
            str(event["main_head"]).lower(),
        ]
        parents = subprocess.run(
            ["git", "-C", repo, "rev-list", "--parents", "-n", "1", candidate_after],
            shell=False,
            capture_output=True,
            text=True,
            timeout=MAIN_SYNC_PROBE_TIMEOUT_SECONDS,
        )
        if parents.returncode != 0 or parents.stdout.strip().lower().split() != expected_parents:
            raise RuntimeError("main-sync autosync merge ancestry is invalid")
    return chain


def main_sync_run_ref(prefix: str, run_id: str) -> str:
    """#1142：main-sync 在來源樹上的 run-scoped ref 名（pin／quarantine 共用推導）。"""

    if (
        prefix
        not in {
            MAIN_SYNC_RETRY_PIN_REF_PREFIX,
            MAIN_SYNC_QUARANTINE_REF_PREFIX,
            MAIN_SYNC_AUTOSYNC_PIN_REF_PREFIX,
        }
        or not isinstance(run_id, str)
        or _MAIN_SYNC_REF_SEGMENT_RE.fullmatch(run_id) is None
    ):
        raise ValueError("main-sync run ref malformed")
    return f"{prefix}/{run_id}"


@dataclass(frozen=True)
class MainSyncRetryMain:
    main_head: str
    pin_ref: str


def fetch_retry_main(
    *,
    source_repo: Path,
    run_id: str,
    runner: Callable[..., object] = subprocess.run,
    timeout_seconds: float = MAIN_SYNC_PROBE_TIMEOUT_SECONDS,
) -> MainSyncRetryMain:
    """#1142：retry-build 下達當下重新取得 `origin/main`，回傳 exact M 與它的 pin ref。

    停機 evidence 記錄的 M 只代表停機那一刻；活躍 repo 的 main 會繼續前進，鎖住
    停機時的 M 會讓修復後的候選在下一次 ship probe 必然又落後。這裡以與 ship probe
    同一組 bounded direct git stage 在 Manager 來源樹 fetch main，但只寫入
    run-scoped pin ref（`--no-write-fetch-head`＋`--refmap=`：不碰 `FETCH_HEAD`，
    也不順手更新 `refs/remotes/origin/main`），讓 harvest 驗證前 M 一直可達。

    任何一段失敗都 raise：呼叫端尚未重置 run，run 維持 `needs_human`（fail-closed），
    不會退回停機時的 M。
    """

    pin_ref = main_sync_run_ref(MAIN_SYNC_RETRY_PIN_REF_PREFIX, run_id)
    fetch = _run_main_sync_stage(
        worktree=source_repo,
        stage="retry-fetch",
        argv=(
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--refmap=",
            "origin",
            f"+refs/heads/main:{pin_ref}",
        ),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if fetch.error_kind is not None or fetch.returncode != 0:
        raise RuntimeError(
            "retry-build could not fetch current origin/main "
            f"(stage=retry-fetch, error_kind="
            f"{_main_sync_command_failure_kind(fetch.stage, fetch)}, "
            f"returncode={fetch.returncode})"
        )
    resolve = _run_main_sync_stage(
        worktree=source_repo,
        stage="retry-main-resolve",
        argv=("rev-parse", "--verify", "--quiet", f"{pin_ref}^{{commit}}"),
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if resolve.error_kind is not None or resolve.returncode != 0:
        raise RuntimeError(
            "retry-build could not resolve current origin/main "
            f"(stage=retry-main-resolve, error_kind="
            f"{_main_sync_command_failure_kind(resolve.stage, resolve)}, "
            f"returncode={resolve.returncode})"
        )
    main_head = _main_sync_object_id(resolve.stdout.decode("ascii", errors="replace"))
    if main_head is None:
        raise RuntimeError(
            "retry-build could not resolve current origin/main "
            "(stage=retry-main-resolve, error_kind=bad-sha)"
        )
    return MainSyncRetryMain(main_head=main_head, pin_ref=pin_ref)


def _delivery_closure_phase(
    *, state_root: Path, run_id: str, candidate: str
) -> str | None:
    journal_path = state_root / "delivery-journal.json"
    if journal_path.is_symlink() or not journal_path.is_file():
        return None
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    rows = journal.get("runs") if isinstance(journal, dict) else None
    row = rows.get(run_id) if isinstance(rows, dict) else None
    ship = row.get("ship") if isinstance(row, dict) else None
    phase = ship.get("phase") if isinstance(ship, dict) else None
    if (
        phase in {"merged", "done"}
        and ship.get("head") == candidate
        and isinstance(ship.get("merge_commit"), str)
    ):
        return str(phase)
    return None


def _should_probe_main_sync(*, run, state_root: Path, candidate: str) -> bool:
    terminal_refresh = (
        run.current_phase == "ship" and getattr(run, "status", None) == "done"
    )
    if terminal_refresh:
        return False
    return _delivery_closure_phase(
        state_root=state_root, run_id=run.run_id, candidate=candidate
    ) is None


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

    autosync_events = _read_main_sync_autosync_events(
        state_root=state_root,
        run_id=run.run_id,
        evidence_refs=getattr(run, "evidence_refs", ()),
    )
    autosync_chain = _main_sync_autosync_chain(
        events=autosync_events,
        candidate=candidate,
        review_ref=foreign_ref.ref,
        review_hash=foreign_ref.sha256,
        source_repo=run.workspace_root,
    )
    review_candidate = (
        str(autosync_chain[0]["review_candidate"]).lower()
        if autosync_chain
        else candidate
    )
    review_payload, review_job = _workflow_evidence_payload(
        registry=registry,
        state_root=state_root,
        run=run,
        phase="review",
        expected_ref=foreign_ref.ref,
        expected_hash=foreign_ref.sha256,
        expected_candidate=review_candidate,
    )
    evaluation = review.validate_gate_evaluation(review_payload)
    builder_job_id = evaluation.get("builder_job_id")
    if (
        evaluation.get("state") != "passed"
        or evaluation.get("candidate") != review_candidate
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
        or row.get("subject_head") != review_candidate
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


def _sync_ship_workspace_origin(*, source_repo: Path, worktree: Path) -> None:
    source_remote = subprocess.run(
        ["git", "-C", str(source_repo), "remote", "get-url", "origin"],
        shell=False,
        capture_output=True,
        text=True,
    )
    upstream = source_remote.stdout.strip() if source_remote.returncode == 0 else ""
    worktree_remote = subprocess.run(
        ["git", "-C", str(worktree), "remote", "get-url", "origin"],
        shell=False,
        capture_output=True,
        text=True,
    )
    if not upstream:
        if worktree_remote.returncode != 0:
            return
        removed = subprocess.run(
            ["git", "-C", str(worktree), "remote", "remove", "origin"],
            shell=False,
            capture_output=True,
            text=True,
        )
        if removed.returncode != 0:
            raise RuntimeError("manager ship workspace origin sync failed")
        return
    if worktree_remote.returncode == 0 and worktree_remote.stdout.strip() == upstream:
        return
    if worktree_remote.returncode == 0:
        updated = subprocess.run(
            ["git", "-C", str(worktree), "remote", "set-url", "origin", upstream],
            shell=False,
            capture_output=True,
            text=True,
        )
    else:
        updated = subprocess.run(
            ["git", "-C", str(worktree), "remote", "add", "origin", upstream],
            shell=False,
            capture_output=True,
            text=True,
        )
    if updated.returncode != 0:
        raise RuntimeError("manager ship workspace origin sync failed")


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
            _sync_ship_workspace_origin(source_repo=source, worktree=target)
            return target
        if not job_workspace.is_job_clone(target):
            # 認不出這是什麼就**不刪**（#478 的爆炸半徑教訓）。這條路徑只該在
            # operator 手動放了東西進 pool 時走到。
            raise RuntimeError("manager ship workspace path is occupied")
        job_workspace.remove_clone(target)
    if creator is None:
        creator = seams.ScriptWorktreeCreator(repo=source, wt_root=pool, base="main")
    created = Path(creator.create(branch, job_id=workspace_id, base_sha=candidate))
    _sync_ship_workspace_origin(source_repo=source, worktree=created)
    _require_pristine_ship_workspace(created, branch=branch, candidate=candidate)
    return created


def _matching_archive_entries(root: Path, *, change: str) -> tuple[str, ...]:
    archive_root = root / "openspec" / "changes" / "archive"
    if archive_root.is_symlink() or not archive_root.is_dir():
        return ()
    suffix = f"-{change}"
    matches = [
        entry.name
        for entry in archive_root.iterdir()
        if not entry.is_symlink()
        and entry.is_dir()
        and (entry.name == change or entry.name.endswith(suffix))
    ]
    return tuple(sorted(matches))


def _archived_spec_purpose_placeholder(change: str) -> str:
    """openspec 1.10 `buildSpecSkeleton` 替新 capability 寫入的 Purpose 佔位（逐字）。"""

    return f"TBD - created by archiving change {change}. Update Purpose after archive."


def _proposal_why(proposal: Path) -> str | None:
    """archived proposal `## Why` 的第一段，空白收斂成單行；找不到就回 None。"""

    try:
        lines = proposal.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    paragraph: list[str] = []
    in_why = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if in_why and paragraph:
                break
            in_why = stripped.lstrip("#").strip().lower() == "why"
            continue
        if not in_why:
            continue
        if not stripped:
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    text = ""
    for part in (" ".join(line.split()) for line in paragraph):
        # 中文段落換行只是排版，接回時不補空格；兩側都是 ASCII 才以空格相接。
        if text and not (ord(text[-1]) > 127 or ord(part[0]) > 127):
            text += " "
        text += part
    return text or None


def _fill_archived_spec_purposes(worktree: Path, *, change: str) -> tuple[str, ...]:
    """#1237：archive 為新 capability 留下的 TBD Purpose 換成 proposal 的 Why。

    delta spec 沒寫 `## Purpose` 時，openspec 會替新的主 spec 寫入佔位；Manager 的
    archive commit 原樣收下，之後的 re-verification 唯讀也不會補，PR 因此必帶佔位，
    Copilot 必回 finding。只替換本 change 的佔位，其他內容一律不動。"""

    specs_root = worktree / "openspec" / "specs"
    entries = _matching_archive_entries(worktree, change=change)
    if not entries or specs_root.is_symlink() or not specs_root.is_dir():
        return ()
    proposal = worktree / "openspec" / "changes" / "archive" / entries[-1] / "proposal.md"
    if proposal.is_symlink() or not proposal.is_file():
        return ()
    purpose = _proposal_why(proposal)
    if purpose is None:
        return ()
    placeholder = _archived_spec_purpose_placeholder(change)
    filled: list[str] = []
    for spec in sorted(specs_root.glob("*/spec.md")):
        if spec.is_symlink() or not spec.is_file():
            continue
        text = spec.read_text(encoding="utf-8")
        lines = text.split("\n")
        if placeholder not in lines:
            continue
        spec.write_text(
            "\n".join(purpose if line == placeholder else line for line in lines),
            encoding="utf-8",
        )
        filled.append(spec.relative_to(worktree).as_posix())
    return tuple(filled)


def _path_exists_or_is_symlink(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError as exc:  # pragma: no cover - fail-closed filesystem guard
        raise RuntimeError("active OpenSpec change path inspection failed") from exc


def _changed_contains_archive_relocation(changed: set[str], *, change: str) -> bool:
    suffix = f"-{change}"
    return any(
        len(parts) > 4
        and parts[:3] == ("openspec", "changes", "archive")
        and (parts[3] == change or parts[3].endswith(suffix))
        for parts in (PurePosixPath(path).parts for path in changed)
    )


def _manager_archive_applied(run, *, registry=None) -> bool:
    # 對齊 manager._manager_archive_applied 的語意：必須「恰好一筆」passed 的
    # openspec-archive step 才算已完成；crash/retry 造成的多筆 passed step 視為
    # 尚未完成（fail-closed），不得靠第二套判定漂移出不同結論。單一真實實作放在
    # manager，這裡改為委派而非重寫一份，避免兩處各自演化。
    from . import manager

    return manager._manager_archive_applied(run, registry=registry)


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
            "confirmed_todo": authority.confirmed_todo,
            "provider_id": authority.github_provider_id,
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
    active_change = worktree / "openspec" / "changes" / change
    if _path_exists_or_is_symlink(active_change) or not _changed_contains_archive_relocation(
        changed,
        change=change,
    ):
        raise RuntimeError("official OpenSpec archive relocation missing")
    if any(not _archive_path_allowed(path, change=change) for path in changed):
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
    expected_candidate: str | None = None,
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
            or row.get("subject_head") == (
                run.candidate_head if expected_candidate is None else expected_candidate
            )
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
    expected_candidate: str | None = None,
) -> tuple[dict, dict]:
    envelope, job = _workflow_evidence_envelope(
        registry=registry,
        state_root=state_root,
        run=run,
        phase=phase,
        expected_ref=expected_ref,
        expected_hash=expected_hash,
        expected_candidate=expected_candidate,
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
    canonical_checkout: str | Path,
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
    change = authority.mapped_openspec[0] if len(authority.mapped_openspec) == 1 else None
    closure = github.fetch_remote_closure(
        repo=authority.repo,
        pr_number=pr_number,
        change=change,
        required_issues=authority.mapped_issues,
        todo_paths=authority.mapped_todo_paths,
        canonical_checkout=canonical_checkout,
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
            "change": change,
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


def _delivery_adapter_adoption_fields(*, state_root: Path, run_id: str) -> dict[str, object]:
    journal_path = state_root / "delivery-journal.json"
    if not journal_path.exists():
        return {}
    if journal_path.is_symlink() or not journal_path.is_file():
        raise RuntimeError("delivery journal path is not a regular file")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("delivery journal payload malformed") from exc
    if not isinstance(journal, dict):
        raise RuntimeError("delivery journal payload malformed")
    runs = journal.get("runs")
    if not isinstance(runs, dict):
        raise RuntimeError("delivery journal runs payload malformed")
    row = runs.get(run_id)
    if row is None:
        return {}
    if not isinstance(row, dict):
        raise RuntimeError("delivery journal run payload malformed")
    ship = row.get("ship")
    if ship is None:
        return {}
    if not isinstance(ship, dict):
        raise RuntimeError("delivery journal ship payload malformed")
    payload: dict[str, object] = {}
    for field in ("adopted_review_id", "adopted_at_epoch"):
        value = ship.get(field)
        if value is not None:
            payload[field] = value
    return payload


def build_production_ship_validator(
    *,
    registry,
    coordinator_root: str | Path,
    runner: Callable[..., object] = subprocess.run,
    now: Callable[[], float] = time.time,
    snapshot_path: str | Path | None = None,
    workspace_creator=None,
    probe_runner: Callable[..., object] = subprocess.run,
    probe_timeout_seconds: float = MAIN_SYNC_PROBE_TIMEOUT_SECONDS,
):
    """Bind review completion to the authenticated, resumable delivery state machine.

    `workspace_creator` 是 ship 段那棵 Manager-owned 工作區的 provisioning seam
    （#653）。留 `None` 時由 `_manager_ship_workspace()` 依 `run.workspace_root`
    自行組一個 `seams.ScriptWorktreeCreator`——生產路徑不必也不應該由呼叫端指定
    工作區從哪來。
    """

    state_root = Path(coordinator_root).resolve()
    autosync_contexts: dict[tuple[str, str], dict[str, object]] = {}

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

        def main_sync_stop(probe):
            stopped = _main_sync_stop_result(state_root=state_root, probe=probe)
            if isinstance(probe, MainSyncProbe) and probe.relation == "clean-behind":
                autosync_contexts[(run.run_id, candidate)] = {
                    "probe": probe,
                    "worktree": worktree,
                    "branch": branch,
                    "probe_evidence_ref": stopped["ref"],
                    "probe_evidence_hash": stopped["hash"],
                }
            return stopped

        change = authority.mapped_openspec[0] if len(authority.mapped_openspec) == 1 else None
        active_change = worktree / "openspec" / "changes" / str(change) if change else None
        archive_applied = _manager_archive_applied(run, registry=registry)
        active_change_present = (
            _path_exists_or_is_symlink(active_change) if active_change is not None else False
        )
        if active_change_present and archive_applied:
            if _matching_archive_entries(worktree, change=str(change)):
                raise RuntimeError(
                    "post-archive candidate re-created active OpenSpec change alongside its official archive"
                )
        if active_change_present and not archive_applied:
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
            output = (
                f"{getattr(archived, 'stdout', '') or ''}"
                f"{getattr(archived, 'stderr', '') or ''}"
            )
            if "Aborted" in output:
                raise RuntimeError("official OpenSpec archive aborted: no files were changed")
            _fill_archived_spec_purposes(worktree, change=str(change))
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
        should_probe_main_sync = _should_probe_main_sync(
            run=run,
            state_root=state_root,
            candidate=candidate,
        )
        if should_probe_main_sync:
            probe = _probe_main_sync(
                worktree=worktree,
                candidate=candidate,
                runner=probe_runner,
                timeout_seconds=probe_timeout_seconds,
            )
            if isinstance(probe, MainSyncProbeFailure) or probe.relation != "in-sync":
                return main_sync_stop(probe)

        def post_preflight_main_sync() -> dict[str, object] | None:
            probe = _probe_main_sync(
                worktree=worktree,
                candidate=candidate,
                runner=probe_runner,
                timeout_seconds=probe_timeout_seconds,
            )
            if isinstance(probe, MainSyncProbeFailure) or probe.relation != "in-sync":
                return main_sync_stop(probe)
            return None

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
            second_probe = post_preflight_main_sync()
            if second_probe is not None:
                return second_probe
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
                    "authority_digest": work_authority_digest(authority),
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
        _rebase_delivery_journal_authority(
            state_root=state_root,
            run=run,
            authority=authority,
        )
        closure_only = terminal_refresh or _delivery_closure_phase(
            state_root=state_root,
            run_id=run.run_id,
            candidate=candidate,
        ) in {"merged", "done"}
        if not closure_only:
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
            second_probe = post_preflight_main_sync()
            if second_probe is not None:
                return second_probe
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
            canonical_checkout=worktree,
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
                **_delivery_adapter_adoption_fields(
                    state_root=state_root,
                    run_id=run.run_id,
                ),
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

    def main_sync_autosync(*, run, candidate: str, stop: Mapping[str, object]):
        context_key = (str(run.run_id), candidate)
        context = autosync_contexts.pop(context_key, None)
        if not isinstance(context, dict):
            return None
        probe = context.get("probe")
        worktree = context.get("worktree")
        branch = context.get("branch")
        if (
            not isinstance(probe, MainSyncProbe)
            or probe.relation != "clean-behind"
            or probe.candidate.lower() != candidate.lower()
            or not isinstance(worktree, Path)
            or not isinstance(branch, str)
            or not branch
        ):
            return None
        foreign_refs = [ref for ref in run.gate_refs if ref.kind == "foreign-review"]
        if len(foreign_refs) != 1 or not foreign_refs[0].sha256:
            return None
        foreign_ref = foreign_refs[0]
        current = registry.get_workflow_run(run.run_id)
        if (
            current.current_phase != "review"
            or current.candidate_head != candidate
            or current.updated_at != run.updated_at
        ):
            return None

        events = _read_main_sync_autosync_events(
            state_root=state_root,
            run_id=run.run_id,
            evidence_refs=getattr(run, "evidence_refs", ()),
        )
        chain = _main_sync_autosync_chain(
            events=events,
            candidate=candidate,
            review_ref=foreign_ref.ref,
            review_hash=foreign_ref.sha256,
            source_repo=run.workspace_root,
        )
        completed_count = _main_sync_autosync_completed_count(events)
        review_candidate = (
            str(chain[0]["review_candidate"]).lower() if chain else candidate.lower()
        )

        def write_attempt(
            *,
            outcome: str,
            reason: str,
            limit: int | None,
            candidate_after: str | None = None,
            observed_main_head: str | None = None,
            failure_detail: str | None = None,
        ) -> dict[str, object]:
            payload: dict[str, object] = {
                "schema": MAIN_SYNC_AUTOSYNC_EVIDENCE_SCHEMA,
                "run_id": run.run_id,
                "outcome": outcome,
                "sync_number": completed_count + 1,
                "candidate_before": candidate.lower(),
                "main_head": probe.main_head.lower(),
                "candidate_after": candidate_after,
                "review_candidate": review_candidate,
                "review_evidence_ref": foreign_ref.ref,
                "review_evidence_hash": foreign_ref.sha256,
                "failure_reason": reason,
            }
            if limit is not None:
                payload["limit"] = limit
            if observed_main_head is not None:
                payload["observed_main_head"] = observed_main_head.lower()
            if failure_detail:
                payload["failure_detail"] = failure_detail[:500]
            evidence = _write_json_evidence(
                state_root,
                "main-sync-autosync",
                payload,
            )
            result: dict[str, object] = {
                "outcome": outcome,
                "reason": reason,
                "count": completed_count,
                "main_head": probe.main_head.lower(),
                "candidate_before": candidate.lower(),
                "review_candidate": review_candidate,
                "review_evidence_ref": foreign_ref.ref,
                "review_evidence_hash": foreign_ref.sha256,
                "probe_evidence_ref": context.get("probe_evidence_ref"),
                "probe_evidence_hash": context.get("probe_evidence_hash"),
                "evidence_ref": evidence["ref"],
                "evidence_hash": evidence["hash"],
            }
            if limit is not None:
                result["limit"] = limit
            if candidate_after is not None:
                result["candidate_after"] = candidate_after.lower()
                result["count"] = completed_count + 1
            if observed_main_head is not None:
                result["observed_main_head"] = observed_main_head.lower()
            if failure_detail:
                result["failure_detail"] = failure_detail[:500]
            return result

        try:
            limit = _main_sync_autosync_limit()
        except ValueError as exc:
            result = write_attempt(
                outcome="autosync-limit-invalid",
                reason="candidate-behind-main",
                limit=None,
                failure_detail=str(exc),
            )
            return result
        if limit == 0:
            return write_attempt(
                outcome="autosync-disabled",
                reason="candidate-behind-main",
                limit=limit,
            )
        if completed_count >= limit:
            return write_attempt(
                outcome="limit-exceeded",
                reason="main-moving-too-fast",
                limit=limit,
            )

        source_repo = Path(run.workspace_root)
        git_identity = _main_sync_autosync_git_identity(source_repo)
        if git_identity is None:
            return write_attempt(
                outcome="merge-failed",
                reason="main-sync-identity-missing",
                limit=limit,
                failure_detail=(
                    "PSC_MAIN_SYNC_GIT_IDENTITY or effective source checkout "
                    "user.name/user.email is required"
                ),
            )
        pin_ref = main_sync_run_ref(MAIN_SYNC_AUTOSYNC_PIN_REF_PREFIX, run.run_id)

        def run_git(args: list[str]):
            return probe_runner(
                [
                    "git",
                    "-c",
                    "core.attributesFile=/dev/null",
                    "-C",
                    str(worktree),
                    *args,
                ],
                shell=False,
                capture_output=True,
                text=True,
                timeout=MAIN_SYNC_AUTOSYNC_TIMEOUT_SECONDS,
                env=_main_sync_isolated_git_env(),
            )

        def clean_workspace() -> bool:
            try:
                return _reset_ship_workspace(worktree, branch=branch, candidate=candidate)
            except Exception:
                return False

        fetch = None
        observed_main = None
        try:
            _require_pristine_ship_workspace(
                worktree,
                branch=branch,
                candidate=candidate,
            )
            fetch = run_git(
                [
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--refmap=",
                    "origin",
                    f"+refs/heads/main:{pin_ref}",
                ]
            )
            if fetch.returncode != 0:
                return write_attempt(
                    outcome="merge-failed",
                    reason="candidate-behind-main",
                    limit=limit,
                    failure_detail=(fetch.stderr or fetch.stdout or "main fetch failed"),
                )
            resolved = run_git(["rev-parse", "--verify", "--quiet", f"{pin_ref}^{{commit}}"])
            observed_main = _main_sync_object_id(resolved.stdout)
            if resolved.returncode != 0 or observed_main is None:
                return write_attempt(
                    outcome="merge-failed",
                    reason="candidate-behind-main",
                    limit=limit,
                    failure_detail="fetched origin/main did not resolve to a full commit id",
                )
            if observed_main != probe.main_head.lower():
                return write_attempt(
                    outcome="main-advanced-during-sync",
                    reason="candidate-behind-main",
                    limit=limit,
                    observed_main_head=observed_main,
                    failure_detail="origin/main advanced after the clean-behind probe",
                )
            merged = run_git(
                [
                    "-c",
                    f"user.name={git_identity[0]}",
                    "-c",
                    f"user.email={git_identity[1]}",
                    "merge",
                    "--no-edit",
                    "--no-ff",
                    "--no-stat",
                    probe.main_head.lower(),
                ]
            )
            if merged.returncode != 0:
                clean = clean_workspace()
                detail = (merged.stderr or merged.stdout or "git merge failed").strip()
                if not clean:
                    detail = f"{detail}; ship workspace reset failed"
                return write_attempt(
                    outcome="merge-failed",
                    reason="candidate-behind-main",
                    limit=limit,
                    failure_detail=detail,
                )
            head = run_git(["rev-parse", "HEAD"])
            candidate_after = _main_sync_object_id(head.stdout)
            parents = run_git(["rev-list", "--parents", "-n", "1", "HEAD"])
            expected_parents = [
                candidate_after,
                candidate.lower(),
                probe.main_head.lower(),
            ]
            if (
                head.returncode != 0
                or candidate_after is None
                or parents.returncode != 0
                or parents.stdout.strip().lower().split() != expected_parents
            ):
                clean = clean_workspace()
                detail = "Manager merge did not produce the expected exact two-parent Candidate"
                if not clean:
                    detail += "; ship workspace reset failed"
                return write_attempt(
                    outcome="merge-failed",
                    reason="candidate-behind-main",
                    limit=limit,
                    failure_detail=detail,
                )

            success_payload: dict[str, object] = {
                "schema": MAIN_SYNC_AUTOSYNC_EVIDENCE_SCHEMA,
                "run_id": run.run_id,
                "outcome": "merged",
                "sync_number": completed_count + 1,
                "candidate_before": candidate.lower(),
                "main_head": probe.main_head.lower(),
                "candidate_after": candidate_after,
                "review_candidate": review_candidate,
                "review_evidence_ref": foreign_ref.ref,
                "review_evidence_hash": foreign_ref.sha256,
                "limit": limit,
            }
            success_evidence = _write_json_evidence(
                state_root,
                "main-sync-autosync",
                success_payload,
            )
            job_id = registry.reserve_job_id(
                _manager_ship_job_task(run=run, card="main-sync-autosync")
            )
            try:
                _harvest_manager_ship_commit(
                    state_root=state_root,
                    run=run,
                    worktree=worktree,
                    branch=branch,
                    spool_key=job_id,
                    baseline=candidate,
                    new_head=candidate_after,
                )
                _record_manager_ship_job(
                    registry=registry,
                    state_root=state_root,
                    run=run,
                    worktree=worktree,
                    branch=branch,
                    card="main-sync-autosync",
                    old_head=candidate,
                    new_head=candidate_after,
                    job_id=job_id,
                )
            except Exception as exc:
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(source_repo),
                        "update-ref",
                        f"refs/heads/{branch}",
                        candidate.lower(),
                        candidate_after,
                    ],
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=MAIN_SYNC_PROBE_TIMEOUT_SECONDS,
                    env=_main_sync_isolated_git_env(),
                )
                clean = clean_workspace()
                detail = f"main-sync Candidate harvest failed: {type(exc).__name__}: {exc}"
                if not clean:
                    detail += "; ship workspace reset failed"
                return write_attempt(
                    outcome="harvest-failed",
                    reason="candidate-behind-main",
                    limit=limit,
                    failure_detail=detail,
                )
            return {
                "outcome": "merged",
                "reason": MAIN_SYNC_AUTOSYNC_REASON,
                "count": completed_count + 1,
                "limit": limit,
                "main_head": probe.main_head.lower(),
                "candidate_before": candidate.lower(),
                "candidate_after": candidate_after,
                "review_candidate": review_candidate,
                "review_evidence_ref": foreign_ref.ref,
                "review_evidence_hash": foreign_ref.sha256,
                "probe_evidence_ref": context.get("probe_evidence_ref"),
                "probe_evidence_hash": context.get("probe_evidence_hash"),
                "evidence_ref": success_evidence["ref"],
                "evidence_hash": success_evidence["hash"],
            }
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            clean = clean_workspace()
            detail = f"{type(exc).__name__}: {exc}"
            if not clean:
                detail += "; ship workspace reset failed"
            return write_attempt(
                outcome="merge-failed",
                reason="candidate-behind-main",
                limit=limit,
                observed_main_head=observed_main,
                failure_detail=detail,
            )
        finally:
            try:
                run_git(["update-ref", "-d", pin_ref])
            except Exception:
                pass

    setattr(validate, "main_sync_autosync", main_sync_autosync)
    return validate
