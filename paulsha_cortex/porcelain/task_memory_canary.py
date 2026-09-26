"""Opt-in live canary for the Hippo task-memory subprocess contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from paulsha_cortex.coordinator.task_memory import (
    CANARY_SUCCESS_EVENT_BY_MODE,
    CANARY_SUCCESS_SEMANTICS,
    TaskMemoryAdapter,
    TaskMemoryCapabilities,
    TaskMemoryProviderError,
    _validate_payload,
    summarize_canary,
    task_memory_context_from_cortex,
)
from paulsha_cortex.coordinator.task_memory_hippo import HippoTaskMemoryClient
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MODES = ("note_fetch", "snapshot", "inline")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex task-memory",
        description="對指定 Hippo projects 執行唯讀 provide/fetch live canary，輸出 bounded JSON。",
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
    return parser


def _context(repo: str, mode: str, index: int, *, allowed_sources: tuple[str, ...]):
    work_id = f"task-memory-canary-{mode}-{index:03d}"
    run_id = f"canary-{mode}-{index:03d}"
    card = f"canary-{mode}"
    step = SimpleNamespace(phase="build", card=card)
    run = SimpleNamespace(repo=repo, work_id=work_id, run_id=run_id, steps=(step,))
    work_item = SimpleNamespace(repo=repo, work_id=work_id, workflow_run_id=run_id)
    job = {
        "job_id": f"job-{mode}-{index:03d}",
        "workflow_run_id": run_id,
        "workflow_card": card,
        "workflow_phase": "build",
        "executor": "cortex-task-memory-canary",
        "tool": "cortex-task-memory-canary",
        "model_id": "cortex-task-memory-canary",
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


def _run_canary(repos: Sequence[str], runs: int, client: HippoTaskMemoryClient | None) -> dict[str, Any]:
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
    scope_checked = 0
    scope_leaks = 0
    negative_attempts = 0
    negative_denials = 0
    cross_scope_attempts = 0
    cross_scope_rejections = 0
    cross_scope_leaks = 0

    for mode in _MODES:
        for repo_index, repo in enumerate(repos):
            for index in range(runs):
                context = _context(repo, mode, repo_index * runs + index, allowed_sources=("hippo",))
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
                            scope_leaks += 1
                        try:
                            _validate_payload(
                                raw_payloads[-1], context=context, requested_mode=mode
                            )
                        except Exception:
                            # The adapter already emits a bounded failure event;
                            # validation details and payload text stay private.
                            pass
                    if prepared.status == "offered":
                        if mode == "inline":
                            events.extend(adapter.confirm_context_delivered(prepared))
                        elif mode == "note_fetch":
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

            # Explicit negative control: Hippo must deny before project search
            # whenever the envelope does not authorize the "hippo" evidence source.
            negative_attempts += 1
            if client is not None:
                denied_context = _context(
                    repo,
                    mode,
                    100_000 + repo_index * len(_MODES) + _MODES.index(mode),
                    allowed_sources=("work-item",),
                )
                try:
                    client.invoke_provide(denied_context.to_envelope(mode=mode))
                except (PermissionError, TaskMemoryProviderError) as exc:
                    if _caught_code(exc) == "permission-denied":
                        negative_denials += 1

            # Deliberately disagree top-level project and host scope. Hippo must
            # reject the request, never label results as the other project.
            cross_scope_attempts += 1
            if client is not None:
                other_repo = repos[(repo_index + 1) % len(repos)]
                cross_context = _context(
                    repo,
                    mode,
                    200_000 + repo_index * len(_MODES) + _MODES.index(mode),
                    allowed_sources=("hippo",),
                )
                request = cross_context.to_envelope(mode=mode)
                request["project"] = other_repo
                try:
                    client.invoke_provide(request)
                    cross_scope_leaks += 1
                except (PermissionError, TaskMemoryProviderError) as exc:
                    if _caught_code(exc) == "scope-mismatch":
                        cross_scope_rejections += 1

    events = [event for mode in _MODES for event in path_events[mode]]
    summary = summarize_canary(events, minimum_successes=5, minimum_rate=0.95)
    paths: dict[str, Any] = {}
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
            repo_minimum = 5
            repo_passed = bool(
                row["eligible_authorized_provides"] >= repo_minimum
                and row["successful_provides"] >= repo_minimum
                and provide_rate is not None
                and provide_rate >= 0.95
                and eligible >= repo_minimum
                and row["successes"] >= repo_minimum
                and rate is not None
                and rate >= 0.95
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
        paths[mode] = {
            "metric_kind": "delivery" if mode == "inline" else "content-retrieval",
            "success_event": CANARY_SUCCESS_EVENT_BY_MODE[mode],
            "success_semantics": CANARY_SUCCESS_SEMANTICS[mode],
            "counts_as_read": mode == "note_fetch",
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
            "successes": aggregate["successes"],
            "eligible_authorized_success_rate": aggregate["success_rate"],
            "eligible_authorized_delivery_rate": (
                aggregate["success_rate"] if mode == "inline" else None
            ),
            "eligible_authorized_content_retrieval_success_rate": (
                aggregate["success_rate"] if mode != "inline" else None
            ),
            "per_repo": repo_rows,
            "passed": bool(aggregate["passed"] and each_repo_passed),
        }

    retrieval_modes = ("note_fetch", "snapshot")
    retrieval_attempts = sum(
        paths[mode]["eligible_authorized_attempts"] for mode in retrieval_modes
    )
    retrieval_successes = sum(paths[mode]["successes"] for mode in retrieval_modes)
    retrieval_rate = retrieval_successes / retrieval_attempts if retrieval_attempts else None
    content_retrieval_passed = bool(
        retrieval_rate is not None and retrieval_rate >= 0.95
    )
    inline_path = paths["inline"]
    inline_delivery_rate = inline_path["eligible_authorized_success_rate"]

    scope_checks_passed = scope_checked >= 5 and scope_leaks == 0
    permission_checks_passed = negative_attempts == negative_denials
    cross_project_checks_passed = (
        cross_scope_attempts == cross_scope_rejections and cross_scope_leaks == 0
    )
    passed = (
        client is not None
        and all(row["passed"] for row in paths.values())
        and content_retrieval_passed
        and scope_checks_passed
        and permission_checks_passed
        and cross_project_checks_passed
        and summary["legacy_strict_kpi_mutated"] is False
    )
    return {
        "schema": "cortex/task-memory-live-canary/v1",
        "started_at": None,
        "finished_at": None,
        "requested_runs_per_repo_path": runs,
        "repositories": list(repos),
        "paths": paths,
        "content_retrieval": {
            "paths": list(retrieval_modes),
            "eligible_authorized_attempts": retrieval_attempts,
            "successes": retrieval_successes,
            "success_rate": retrieval_rate,
            "minimum_rate": 0.95,
            "passed": content_retrieval_passed,
        },
        "inline_delivery": {
            "eligible_authorized_attempts": inline_path["eligible_authorized_attempts"],
            "successful_deliveries": inline_path["successes"],
            "delivery_rate": inline_delivery_rate,
            "minimum_rate": 0.95,
            "counts_as_read": False,
            "passed": inline_path["passed"],
        },
        "permission_negative_control": {
            "attempted": negative_attempts,
            "permission_denied": negative_denials,
            "passed": permission_checks_passed,
        },
        "scope_checks": {
            "payloads_checked": scope_checked,
            "scope_leaks": scope_leaks,
            "passed": scope_checks_passed,
        },
        "cross_project_checks": {
            "attempted": cross_scope_attempts,
            "scope_mismatch_rejections": cross_scope_rejections,
            "misattribution_leaks": cross_scope_leaks,
            "passed": cross_project_checks_passed,
        },
        "legacy_strict_kpi_mutated": False,
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
    started = datetime.now(timezone.utc).isoformat()
    client = HippoTaskMemoryClient.from_environment()
    report = _run_canary(repos, args.runs, client)
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
