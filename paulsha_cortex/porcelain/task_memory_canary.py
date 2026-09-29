"""Opt-in live canary for the Hippo task-memory subprocess contract.

輸出契約是 `cortex/task-memory-live-canary/v1`。形狀的正本是 #845 需求交付
總帳契約（`docs/superpowers/specs/requirement-delivery-accounting.md` 的 live
receipt 封閉登記表）：`paths` 以 #857 spec R3 的三個 delivery 結果
（`context-delivered`／`snapshot-ready`／`note-fetch`）為 key，每列帶
`attempts`／`successes`／`success_rate`；`negative_controls`、`cross_project`
是串列；頂層帶 `executor.id` 與（`--target-file` 提供時）`target`。產物可原封
不動作為 `cortex/live-canary-receipt/v1` 的 `evidence`，由
`coordinator/live_receipt_validators.py` 驗證。

除成功率外，canary 由實際觀測判定四種 blocker（#857 AC4／G857-3）：
scope leak、跨 project 誤歸因、relay overwrite（provider 覆寫 Cortex 轉交的
task identity／delivery mode，或 inline 交付覆寫了 Cortex 轉交給 executor 的
prompt）、既有輸出 schema 破壞（provider 回應不再符合支援的 v1 schema）；
legacy strict KPI 是否被改動由 receipt 實際的 `counts_as_read` 計算。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from paulsha_cortex.coordinator.task_memory import (
    CANARY_SUCCESS_EVENT_BY_MODE,
    CANARY_SUCCESS_SEMANTICS,
    PreparedTaskMemory,
    TaskMemoryAdapter,
    TaskMemoryCapabilities,
    TaskMemoryProviderError,
    summarize_canary,
    task_memory_context_from_cortex,
)
from paulsha_cortex.coordinator.task_memory_hippo import HippoTaskMemoryClient

CANARY_SCHEMA = "cortex/task-memory-live-canary/v1"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MODES = ("note_fetch", "snapshot", "inline")
#: #857 spec R3 的 delivery 結果名稱（#845 validator 的 `paths` key）↔ executor
#: capability（adapter 的 delivery mode）。
_PATH_BY_MODE = {
    "inline": "context-delivered",
    "snapshot": "snapshot-ready",
    "note_fetch": "note-fetch",
}
_RETRIEVAL_MODES = ("note_fetch", "snapshot")
#: canary 合成卡片輪替的 task kind：Manager 以 task memory 派工、且有 applied
#: 回報管道的 executor 卡片 phase（`manager.TASK_MEMORY_APPLIED_PHASES`，依
#: `WORKFLOW_PHASES` 順序）。同一 repo／path 的各次 provide 依序輪替。
_TASK_KINDS = ("build", "verify", "review")
_MIN_TASK_KINDS = 2
_MIN_SUCCESSES = 5
_MIN_RATE = 0.95
_TOOL = "cortex-task-memory-canary"
#: adapter receipt 的 bounded reason 中，代表 provider 覆寫了 Cortex 轉交的
#: task identity 或 delivery mode（relay overwrite）者。
_RELAY_OVERWRITE_REASONS = frozenset({"task-id-mismatch", "mode-mismatch"})
#: 代表 provider 輸出不再符合 Cortex 支援的 `hippo/task-memory/v1` schema 者
#: （unsupported major、非 JSON 或結構不符）。
_SCHEMA_BREAK_REASONS = frozenset({"unsupported-schema-major", "malformed-payload"})
_RELAY_BASE_PROMPT = (
    "Cortex task-memory canary dispatch prompt for {task_id} ({task_kind}).\n"
    "This relayed task prompt must reach the executor byte-identical.\n"
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex task-memory",
        description=(
            "對指定 Hippo projects 執行唯讀 provide/fetch live canary，輸出 bounded JSON。"
            "須以 PSC_TASK_MEMORY_HIPPO_CMD 明示 Hippo CLI 命令（第一個元素必須是絕對路徑）；"
            "未設、空白或非絕對路徑一律視為 provider 缺席，不會搜尋 PATH。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    canary = sub.add_parser("canary", help="驗證 provide/fetch、scope 與 permission controls")
    canary.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Hippo registry 已登記的 canonical owner/repo；至少提供兩個不同 repo",
    )
    canary.add_argument("--runs", type=int, default=5, help="每個 repo/path 的 provide 次數（預設 5）")
    canary.add_argument(
        "--evidence-path",
        help="選填 JSON evidence 輸出路徑；省略時不寫檔",
    )
    canary.add_argument(
        "--target-file",
        help=(
            "選填 JSON 檔：#845 acceptance target（repo、candidate_sha、artifact_sha256、"
            "source_revision、service、instance、profile_key、config_revision 八欄）；"
            "提供時原樣寫入輸出的 target，供 cortex/live-canary-receipt/v1 綁定"
        ),
    )
    return parser


def _context(
    repo: str,
    mode: str,
    index: int,
    *,
    allowed_sources: tuple[str, ...],
    task_kind: str,
):
    work_id = f"task-memory-canary-{mode}-{index:03d}"
    run_id = f"canary-{mode}-{index:03d}"
    card = f"canary-{mode}"
    step = SimpleNamespace(phase=task_kind, card=card)
    run = SimpleNamespace(repo=repo, work_id=work_id, run_id=run_id, steps=(step,))
    work_item = SimpleNamespace(repo=repo, work_id=work_id, workflow_run_id=run_id)
    job = {
        "job_id": f"job-{mode}-{index:03d}",
        "workflow_run_id": run_id,
        "workflow_card": card,
        "workflow_phase": task_kind,
        "executor": _TOOL,
        "tool": _TOOL,
        "model_id": _TOOL,
    }
    capabilities = TaskMemoryCapabilities(
        inline=mode == "inline",
        snapshot=mode == "snapshot",
        note_fetch=mode == "note_fetch",
    )
    return task_memory_context_from_cortex(
        work_item=work_item,
        run=run,
        step=step,
        job=job,
        capabilities=capabilities,
        goal=f"Verify the {mode} task-memory delivery path for {repo}.",
        allowed_evidence_sources=allowed_sources,
    )


def _payload_scope_ok(payload: object, repo: str) -> bool:
    if not isinstance(payload, Mapping) or payload.get("project") != repo:
        return False
    delivery = payload.get("delivery")
    manifest = delivery.get("manifest") if isinstance(delivery, Mapping) else None
    if not isinstance(manifest, Mapping) or manifest.get("project") != repo:
        return False
    candidates = payload.get("candidates")
    entries = manifest.get("entries")
    if not isinstance(candidates, list) or not isinstance(entries, list):
        return False
    return all(
        not isinstance(item, Mapping) or item.get("project", repo) == repo
        for item in (*candidates, *entries)
    )


def _caught_code(exc: BaseException) -> str | None:
    code = getattr(exc, "provider_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None


def _manager_prompt_composer(prompt: str, prepared: PreparedTaskMemory) -> str:
    """Manager 正式派工把 inline task memory 併入 executor prompt 的同一個函式。

    每次呼叫時才取 `manager._append_task_memory_inline`，canary 驗的就是目前
    載入的 runtime 實作；manager 很大，延遲 import 以免拖慢其他 CLI 命令。"""

    from paulsha_cortex.coordinator import manager

    return manager._append_task_memory_inline(prompt, prepared)


def _relay_outcome(base: str, composed: object, prepared: PreparedTaskMemory) -> tuple[bool, bool]:
    """回傳 ``(relay_preserved, memory_carried)``。

    relay 保留＝Cortex 轉交的原 prompt 逐字是組好 prompt 的前綴；沒有 offer 時
    組好的 prompt 必須與原 prompt 完全相同。memory 送達＝有 offer 時附加段落
    帶有每則 inline note 的標記。"""

    if not isinstance(composed, str) or not composed.startswith(base):
        return False, False
    appended = composed[len(base):]
    offered = prepared.status == "offered" and bool(prepared.inline_context)
    if not offered:
        return appended == "", False
    carried = bool(appended) and all(
        f"[{item['note_id']}]" in appended for item in prepared.inline_context
    )
    return True, carried


def _executor_identity() -> dict[str, Any]:
    """產生這份量測的執行者身分（`derive_canary_domain` 的導出來源）。

    以 canary 工具名與執行它的 OS uid 組成：canary 在哪個帳號下跑，就決定了
    Hippo memory root 的實際權限，與 reviewer 的 independence domain 無關。"""

    getuid = getattr(os, "getuid", None)
    uid = getuid() if callable(getuid) else None
    return {
        "id": f"{_TOOL}:uid-{uid}" if uid is not None else _TOOL,
        "tool": _TOOL,
    }


def _run_canary(
    repos: Sequence[str],
    runs: int,
    client: HippoTaskMemoryClient | None,
    *,
    target: Mapping[str, Any] | None = None,
    compose_prompt: Callable[[str, PreparedTaskMemory], str] | None = None,
) -> dict[str, Any]:
    compose = compose_prompt or _manager_prompt_composer
    path_events: dict[str, list[dict[str, Any]]] = {mode: [] for mode in _MODES}
    per_repo: dict[str, dict[str, dict[str, int]]] = {
        mode: {
            repo: {
                "attempted_provide": 0,
                "eligible_authorized_provides": 0,
                "successful_provides": 0,
                "eligible_authorized_attempts": 0,
                "successes": 0,
            }
            for repo in repos
        }
        for mode in _MODES
    }
    per_kind: dict[str, dict[str, dict[str, int]]] = {
        mode: {
            kind: {"eligible_authorized_attempts": 0, "successes": 0}
            for kind in _TASK_KINDS
        }
        for mode in _MODES
    }
    scope_checked = 0
    scope_leaks_by_repo = {repo: 0 for repo in repos}
    negative_attempts = 0
    negative_denials = 0
    cross_attempts_by_repo = {repo: 0 for repo in repos}
    cross_rejections_by_repo = {repo: 0 for repo in repos}
    cross_leaks_by_repo = {repo: 0 for repo in repos}
    prompts_checked = 0
    prompt_overwrites = 0
    identity_overwrites = 0
    payloads_checked = 0
    schema_breaks = 0

    for mode in _MODES:
        for repo_index, repo in enumerate(repos):
            for index in range(runs):
                task_kind = _TASK_KINDS[index % len(_TASK_KINDS)]
                context = _context(
                    repo,
                    mode,
                    repo_index * runs + index,
                    allowed_sources=("hippo",),
                    task_kind=task_kind,
                )
                raw_payloads: list[Mapping[str, Any]] = []
                fetch = None
                provider = None
                if client is not None:
                    provider, fetch = client.callbacks(context)

                    def capture(envelope, callback=provider):
                        payload = callback(envelope)
                        if isinstance(payload, Mapping):
                            raw_payloads.append(payload)
                        return payload

                    provider = capture
                else:
                    # Preserve the selected note-fetch mode so an absent CLI is
                    # reported as provider-unavailable rather than ineligible.
                    fetch = lambda _task_id, _note_id: (_ for _ in ()).throw(
                        RuntimeError("provider unavailable")
                    )
                with tempfile.TemporaryDirectory(prefix="cortex-task-memory-canary-") as temp_root:
                    adapter = TaskMemoryAdapter(provider=provider, note_fetch=fetch)
                    prepared = adapter.prepare(
                        context,
                        snapshot_root=temp_root if mode == "snapshot" else None,
                    )
                    events = list(prepared.events)
                    if raw_payloads:
                        scope_checked += 1
                        if not _payload_scope_ok(raw_payloads[-1], repo):
                            scope_leaks_by_repo[repo] += 1
                    if mode == "inline":
                        # inline 交付的實際通道是 Manager 組給 executor 的 prompt：
                        # 用正式 composer 組一次，確認原 prompt 沒被覆寫、memory
                        # 確實附加上去，才記 context-delivered。
                        prompts_checked += 1
                        base = _RELAY_BASE_PROMPT.format(
                            task_id=context.task_id, task_kind=task_kind
                        )
                        try:
                            composed = compose(base, prepared)
                        except Exception:
                            composed = None
                        preserved, carried = _relay_outcome(base, composed, prepared)
                        if not preserved:
                            prompt_overwrites += 1
                        if prepared.status == "offered" and preserved and carried:
                            events.extend(adapter.confirm_context_delivered(prepared))
                    elif prepared.status == "offered":
                        if mode == "note_fetch":
                            for note_id in tuple(prepared.candidates):
                                events.extend(adapter.fetch_note(prepared, note_id).events)
                        elif mode == "snapshot" and prepared.snapshot_id is not None:
                            for note_id in tuple(prepared.candidates):
                                events.extend(
                                    adapter.open_snapshot(
                                        prepared,
                                        prepared.snapshot_id,
                                        note_id,
                                    ).events
                                )
                    if client is not None:
                        payloads_checked += 1
                        failure_reasons = {
                            event.get("reason")
                            for event in events
                            if event.get("event") == "read-failed"
                        }
                        if failure_reasons & _RELAY_OVERWRITE_REASONS:
                            identity_overwrites += 1
                        if failure_reasons & _SCHEMA_BREAK_REASONS:
                            schema_breaks += 1
                    path_events[mode].extend(events)
                    rows = per_repo[mode][repo]
                    rows["attempted_provide"] += 1
                    identities = {
                        (event.get("task_id"), event.get("note_id"), event.get("content_hash"))
                        for event in events
                        if event.get("event") == "candidate-selected"
                        and event.get("eligible_authorized") is True
                    }
                    successful = {
                        (event.get("task_id"), event.get("note_id"), event.get("content_hash"))
                        for event in events
                        if event.get("event") == CANARY_SUCCESS_EVENT_BY_MODE[mode]
                    }
                    selected_by_task: dict[str, set[tuple[Any, Any, Any]]] = {}
                    successful_by_task: dict[str, set[tuple[Any, Any, Any]]] = {}
                    for task_id, note_id, content_hash in identities:
                        selected_by_task.setdefault(str(task_id), set()).add(
                            (task_id, note_id, content_hash)
                        )
                    for identity in successful:
                        successful_by_task.setdefault(str(identity[0]), set()).add(identity)
                    rows["eligible_authorized_attempts"] += len(identities)
                    rows["successes"] += len(identities & successful)
                    rows["eligible_authorized_provides"] += len(selected_by_task)
                    rows["successful_provides"] += sum(
                        selected_candidates <= successful_by_task.get(task_id, set())
                        for task_id, selected_candidates in selected_by_task.items()
                    )
                    kind_row = per_kind[mode][task_kind]
                    kind_row["eligible_authorized_attempts"] += len(identities)
                    kind_row["successes"] += len(identities & successful)

            control_kind = _TASK_KINDS[_MODES.index(mode) % len(_TASK_KINDS)]
            # Explicit negative control: Hippo must deny before project search
            # whenever the envelope does not authorize the "hippo" evidence source.
            negative_attempts += 1
            if client is not None:
                denied_context = _context(
                    repo,
                    mode,
                    100_000 + repo_index * len(_MODES) + _MODES.index(mode),
                    allowed_sources=("work-item",),
                    task_kind=control_kind,
                )
                try:
                    client.invoke_provide(denied_context.to_envelope(mode=mode))
                except (PermissionError, TaskMemoryProviderError) as exc:
                    if _caught_code(exc) == "permission-denied":
                        negative_denials += 1

            # Deliberately disagree top-level project and host scope. Hippo must
            # reject the request, never label results as the other project.
            cross_attempts_by_repo[repo] += 1
            if client is not None:
                other_repo = repos[(repo_index + 1) % len(repos)]
                cross_context = _context(
                    repo,
                    mode,
                    200_000 + repo_index * len(_MODES) + _MODES.index(mode),
                    allowed_sources=("hippo",),
                    task_kind=control_kind,
                )
                request = cross_context.to_envelope(mode=mode)
                request["project"] = other_repo
                try:
                    client.invoke_provide(request)
                    cross_leaks_by_repo[repo] += 1
                except (PermissionError, TaskMemoryProviderError) as exc:
                    if _caught_code(exc) == "scope-mismatch":
                        cross_rejections_by_repo[repo] += 1

    scope_leaks = sum(scope_leaks_by_repo.values())
    cross_scope_attempts = sum(cross_attempts_by_repo.values())
    cross_scope_rejections = sum(cross_rejections_by_repo.values())
    cross_scope_leaks = sum(cross_leaks_by_repo.values())
    observed_blockers: list[str] = []
    if scope_leaks:
        observed_blockers.append("scope-leak")
    if cross_scope_leaks:
        observed_blockers.append("cross-project-misattribution")
    if prompt_overwrites or identity_overwrites:
        observed_blockers.append("relay-overwrite")
    if schema_breaks:
        observed_blockers.append("legacy-output-schema-break")

    events = [event for mode in _MODES for event in path_events[mode]]
    summary = summarize_canary(
        events,
        minimum_successes=_MIN_SUCCESSES,
        minimum_rate=_MIN_RATE,
        observed_blockers=observed_blockers,
    )
    paths_by_mode: dict[str, dict[str, Any]] = {}
    for mode in _MODES:
        aggregate = summary["paths"].get(
            mode,
            {
                "authorized_attempts": 0,
                "successes": 0,
                "success_rate": None,
                "passed": False,
            },
        )
        repo_rows: dict[str, Any] = {}
        each_repo_passed = True
        for repo in repos:
            row = per_repo[mode][repo]
            eligible = row["eligible_authorized_attempts"]
            rate = row["successes"] / eligible if eligible else None
            eligible_provides = row["eligible_authorized_provides"]
            provide_rate = (
                row["successful_provides"] / eligible_provides
                if eligible_provides
                else None
            )
            repo_passed = bool(
                row["eligible_authorized_provides"] >= _MIN_SUCCESSES
                and row["successful_provides"] >= _MIN_SUCCESSES
                and provide_rate is not None
                and provide_rate >= _MIN_RATE
                and eligible >= _MIN_SUCCESSES
                and row["successes"] >= _MIN_SUCCESSES
                and rate is not None
                and rate >= _MIN_RATE
            )
            each_repo_passed = each_repo_passed and repo_passed
            repo_rows[repo] = {
                **row,
                "eligible_authorized_success_rate": rate,
                "eligible_authorized_provide_success_rate": provide_rate,
                "passed": repo_passed,
            }
        total_eligible_provides = sum(
            per_repo[mode][repo]["eligible_authorized_provides"] for repo in repos
        )
        total_successful_provides = sum(
            per_repo[mode][repo]["successful_provides"] for repo in repos
        )
        paths_by_mode[mode] = {
            "mode": mode,
            "metric_kind": "delivery" if mode == "inline" else "content-retrieval",
            "success_event": CANARY_SUCCESS_EVENT_BY_MODE[mode],
            "success_semantics": CANARY_SUCCESS_SEMANTICS[mode],
            "counts_as_read": mode == "note_fetch",
            # #845 validator 讀的三欄：eligible authorized candidate 層級的
            # 嘗試數、成功數與兩者相除的成功率（不另填數字）。
            "attempts": aggregate["authorized_attempts"],
            "successes": aggregate["successes"],
            "success_rate": aggregate["success_rate"],
            "attempted_provide": sum(
                per_repo[mode][repo]["attempted_provide"] for repo in repos
            ),
            "eligible_authorized_provides": total_eligible_provides,
            "successful_provides": total_successful_provides,
            "eligible_authorized_provide_success_rate": (
                total_successful_provides / total_eligible_provides
                if total_eligible_provides
                else None
            ),
            "eligible_authorized_attempts": aggregate["authorized_attempts"],
            "eligible_authorized_success_rate": aggregate["success_rate"],
            "eligible_authorized_delivery_rate": (
                aggregate["success_rate"] if mode == "inline" else None
            ),
            "eligible_authorized_content_retrieval_success_rate": (
                aggregate["success_rate"] if mode != "inline" else None
            ),
            "per_repo": repo_rows,
            "task_kinds": {kind: dict(row) for kind, row in per_kind[mode].items()},
            "passed": bool(aggregate["passed"] and each_repo_passed),
        }

    retrieval_attempts = sum(
        paths_by_mode[mode]["eligible_authorized_attempts"] for mode in _RETRIEVAL_MODES
    )
    retrieval_successes = sum(paths_by_mode[mode]["successes"] for mode in _RETRIEVAL_MODES)
    retrieval_rate = retrieval_successes / retrieval_attempts if retrieval_attempts else None
    # 聚合門檻＝成功率 ≥95% 且每條 retrieval path 各自通過（每 repo／path
    # 至少 5 次成功）；只看成功率會讓 --runs 1 的 canary 誤報通過。
    content_retrieval_passed = bool(
        retrieval_rate is not None
        and retrieval_rate >= _MIN_RATE
        and all(paths_by_mode[mode]["passed"] for mode in _RETRIEVAL_MODES)
    )
    inline_path = paths_by_mode["inline"]
    inline_delivery_rate = inline_path["eligible_authorized_success_rate"]

    scope_checks_passed = scope_checked >= _MIN_SUCCESSES and scope_leaks == 0
    permission_checks_passed = negative_attempts > 0 and negative_attempts == negative_denials
    cross_project_checks_passed = (
        cross_scope_attempts > 0
        and cross_scope_attempts == cross_scope_rejections
        and cross_scope_leaks == 0
    )
    relay_checks_passed = prompt_overwrites == 0 and identity_overwrites == 0
    schema_checks_passed = schema_breaks == 0
    kinds_with_successes = {
        _PATH_BY_MODE[mode]: [
            kind for kind in _TASK_KINDS if per_kind[mode][kind]["successes"] > 0
        ]
        for mode in _MODES
    }
    task_kind_coverage_passed = all(
        len(kinds) >= _MIN_TASK_KINDS for kinds in kinds_with_successes.values()
    )
    cross_project: list[dict[str, Any]] = []
    for repo in repos:
        repo_paths_passed = all(
            paths_by_mode[mode]["per_repo"][repo]["passed"] for mode in _MODES
        )
        repo_passed = bool(
            repo_paths_passed
            and scope_leaks_by_repo[repo] == 0
            and cross_attempts_by_repo[repo] > 0
            and cross_attempts_by_repo[repo] == cross_rejections_by_repo[repo]
            and cross_leaks_by_repo[repo] == 0
        )
        cross_project.append(
            {
                "repo": repo,
                "status": "passed" if repo_passed else "failed",
                "paths_passed": repo_paths_passed,
                "scope_leaks": scope_leaks_by_repo[repo],
                "cross_scope_attempts": cross_attempts_by_repo[repo],
                "cross_scope_rejections": cross_rejections_by_repo[repo],
                "misattribution_leaks": cross_leaks_by_repo[repo],
            }
        )
    passed = bool(
        client is not None
        and all(row["passed"] for row in paths_by_mode.values())
        and content_retrieval_passed
        and scope_checks_passed
        and permission_checks_passed
        and cross_project_checks_passed
        and relay_checks_passed
        and schema_checks_passed
        and task_kind_coverage_passed
        and not summary["blockers"]
    )
    return {
        "schema": CANARY_SCHEMA,
        "started_at": None,
        "finished_at": None,
        "requested_runs_per_repo_path": runs,
        "repositories": list(repos),
        "task_kinds": list(_TASK_KINDS),
        "executor": _executor_identity(),
        "target": dict(target) if target is not None else None,
        "paths": {_PATH_BY_MODE[mode]: paths_by_mode[mode] for mode in _MODES},
        "content_retrieval": {
            "paths": [_PATH_BY_MODE[mode] for mode in _RETRIEVAL_MODES],
            "attempts": retrieval_attempts,
            "eligible_authorized_attempts": retrieval_attempts,
            "successes": retrieval_successes,
            "success_rate": retrieval_rate,
            "minimum_rate": _MIN_RATE,
            "passed": content_retrieval_passed,
        },
        "inline_delivery": {
            "eligible_authorized_attempts": inline_path["eligible_authorized_attempts"],
            "successful_deliveries": inline_path["successes"],
            "delivery_rate": inline_delivery_rate,
            "minimum_rate": _MIN_RATE,
            "counts_as_read": False,
            "passed": inline_path["passed"],
        },
        "negative_controls": [
            {
                "case": "permission-denied",
                "attempted": negative_attempts,
                "observed": negative_denials,
                "status": "passed" if permission_checks_passed else "failed",
            },
            {
                "case": "cross-scope-rejection",
                "attempted": cross_scope_attempts,
                "observed": cross_scope_rejections,
                "misattribution_leaks": cross_scope_leaks,
                "status": "passed" if cross_project_checks_passed else "failed",
            },
        ],
        "cross_project": cross_project,
        "scope_checks": {
            "payloads_checked": scope_checked,
            "scope_leaks": scope_leaks,
            "passed": scope_checks_passed,
        },
        "relay_checks": {
            "executor_prompts_checked": prompts_checked,
            "executor_prompt_overwrites": prompt_overwrites,
            "provider_identity_overwrites": identity_overwrites,
            "passed": relay_checks_passed,
        },
        "schema_checks": {
            "provider_payloads_checked": payloads_checked,
            "schema_breaks": schema_breaks,
            "passed": schema_checks_passed,
        },
        "task_kind_coverage": {
            "minimum_task_kinds": _MIN_TASK_KINDS,
            "task_kinds_with_successes": kinds_with_successes,
            "passed": task_kind_coverage_passed,
        },
        "blockers": list(summary["blockers"]),
        "strict_read_violations": summary["strict_read_violations"],
        "legacy_strict_kpi_mutated": summary["legacy_strict_kpi_mutated"],
        "provider_available": client is not None,
        "passed": passed,
    }


def _write_evidence(path_text: str, encoded: bytes) -> None:
    path = Path(path_text).expanduser()
    parent = path.parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / path.name
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(destination, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _load_target(path_text: str) -> dict[str, Any]:
    """讀取並驗證 `--target-file`；規則與 #845 `_verify_live` 的 target 相同。"""

    from paulsha_cortex.coordinator.requirement_delivery import validate_acceptance_target

    raw = json.loads(Path(path_text).expanduser().read_text(encoding="utf-8"))
    return validate_acceptance_target(raw)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    repos = tuple(args.repo)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if len(repos) < 2 or any(not _REPO_RE.fullmatch(repo) for repo in repos):
        parser.error("--repo requires at least two canonical owner/repo values")
    if len({repo.casefold() for repo in repos}) != len(repos):
        parser.error("--repo values must be unique")
    target = None
    if args.target_file:
        try:
            target = _load_target(args.target_file)
        except (OSError, UnicodeDecodeError, ValueError):
            parser.error(
                "--target-file must be a readable JSON object with exactly the #845 "
                "acceptance target fields"
            )
    started = datetime.now(timezone.utc).isoformat()
    client = HippoTaskMemoryClient.from_environment()
    report = _run_canary(repos, args.runs, client, target=target)
    report["started_at"] = started
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    encoded = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if args.evidence_path:
        try:
            _write_evidence(args.evidence_path, encoded)
        except (OSError, ValueError):
            report["passed"] = False
            report["evidence_write_error"] = "write-failed"
            encoded = (
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
    sys.stdout.write(encoded.decode("utf-8"))
    return 0 if report["passed"] else 1


def register_commands() -> None:
    # Resolve the live registry during registration. Test and embedding hosts
    # can reload the porcelain package while keeping family modules cached.
    porcelain = importlib.import_module("paulsha_cortex.porcelain")
    porcelain.register(
        porcelain.PorcelainCommand(
            name="task-memory",
            help="驗證 Hippo task-memory live provider 與 scope canary",
            run=main,
        )
    )
