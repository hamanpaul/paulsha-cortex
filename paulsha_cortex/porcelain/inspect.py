from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from paulsha_cortex.config import paths
from paulsha_cortex.control.client import read_status
from paulsha_cortex.coordinator import autonomy
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.doctor import run_doctor
from paulsha_cortex.monitor.config import default_socket_path, load_config
from paulsha_cortex.monitor.work_api import MonitorSocketClient

from . import COMMANDS, PorcelainCommand, register
from ._runtime_probe import probe_service_runtime

INSPECT_SCHEMA = "cortex-porcelain/inspect/v1"


def register_commands() -> None:
    if "inspect" in COMMANDS:
        return
    register(PorcelainCommand(name="inspect", help="唯讀檢查 status/job/ready/work/doctor/service/models", run=main))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex inspect")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="讀取 manager status 快照")
    status.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    job = sub.add_parser("job", help="顯示單一 Job snapshot")
    job.add_argument("job_id")
    job.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    ready = sub.add_parser("ready", help="列出目前 ready slice")
    ready.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    work = sub.add_parser("work", help="顯示單一 Work Item")
    work.add_argument("work_id")
    work.add_argument("--repo", default=None, help="指定 owner/repo")
    work.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    doctor = sub.add_parser("doctor", help="讀取 doctor probe 摘要")
    doctor.add_argument("--probe-live", action="store_true", help="執行 live probe")
    doctor.add_argument("--repo", default=None, help="live probe 使用的 owner/repo")
    doctor.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    doctor.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    service = sub.add_parser("service", help="檢查 service runtime 與 unit exec path")
    service.add_argument("--instance", default=os.environ.get("PSC_INSTANCE", "cortex"))
    service.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")

    models = sub.add_parser(
        "models", help="顯示每個 model identity × persona 的能力封套值與 provenance 來源"
    )
    models.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/inspect/v1 JSON")
    return parser


def _json_dump(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _inspect_envelope(command: str, **payload: Any) -> dict[str, Any]:
    return {"schema": INSPECT_SCHEMA, "command": command, **payload}


def status_summary() -> dict[str, Any]:
    return read_status()


def doctor_summary(
    *,
    probe_live: bool = False,
    repo: str | None = None,
    instance: str | None = None,
) -> dict[str, Any]:
    effective_instance = instance if instance is not None else os.environ.get("PSC_INSTANCE", "cortex")
    return run_doctor(probe_live=probe_live, repo=repo, instance=effective_instance).to_dict()


def _print_status(status: dict[str, Any]) -> None:
    sys.stdout.write(f"updated_at: {status.get('updated_at')}\n")
    sys.stdout.write(f"degraded: {status.get('degraded')}\n")
    sys.stdout.write("ready: " + json.dumps(status.get("ready", []), ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.write("held: " + json.dumps(status.get("held", []), ensure_ascii=False, sort_keys=True) + "\n")
    # issue #372 複驗點名的缺漏：attention（needs_human slice）過去只有 --json
    # 輸出看得到，文字模式漏印，讓操作者巡檢時容易錯過需要人工介入的項目。
    sys.stdout.write(
        "attention: " + json.dumps(status.get("attention", []), ensure_ascii=False, sort_keys=True) + "\n"
    )
    # #384：typed provider failure 分類（見 slice_status_entry 的
    # ``provider_outcome`` 投影）已經包含在上面的 JSON 裡，這裡再加一行可掃視
    # 摘要——不必展開整包 JSON 就能看出是哪個 slice、哪一種 outcome（auth／
    # rate_limited／transient／content／quota／unknown）、可不可 retry。
    for entry in status.get("attention", []) or []:
        if not isinstance(entry, dict):
            continue
        outcome = entry.get("provider_outcome")
        if not isinstance(outcome, dict) or not outcome.get("outcome"):
            continue
        sys.stdout.write(
            f"  provider_failure[{entry.get('slice_id', '-')}]: {outcome.get('outcome')} "
            f"(authority={outcome.get('authority')}, retryable={outcome.get('retryable')})\n"
        )
    # 診斷 invariant（#527／#514／#515／#511／#482）：把 run 轉入 needs_human 的
    # 那一刻必須落一份結構化理由。既然理由已經在 attention 條目上，文字模式就
    # 該直接印出來——五個現場的共同症狀正是「理由存在於某處，但沒有任何一個
    # operator 會看的介面印它」。格式比照上面的 provider_failure 摘要行。
    for entry in status.get("attention", []) or []:
        if not isinstance(entry, dict):
            continue
        blocking = entry.get("blocking_reason")
        if not isinstance(blocking, dict) or not blocking.get("reason"):
            continue
        subject = entry.get("run_id") or entry.get("slice_id") or "-"
        sys.stdout.write(
            f"  needs_human[{subject}]: {blocking.get('reason')}: {blocking.get('detail')} "
            f"(source={blocking.get('source')})\n"
        )
        for ref in blocking.get("evidence_refs") or []:
            sys.stdout.write(f"    evidence: {ref}\n")
    sys.stdout.write(
        "in_flight: " + json.dumps(status.get("in_flight", []), ensure_ascii=False, sort_keys=True) + "\n"
    )
    sys.stdout.write(
        "recent_done: " + json.dumps(status.get("recent_done", []), ensure_ascii=False, sort_keys=True) + "\n"
    )
    # #262 R5：顯示缺少的 capability、使用中的 executor environment 與 snapshot 新鮮度。
    preflight = status.get("runtime_preflight")
    if preflight:
        from paulsha_cortex.coordinator.runtime_preflight import render_preflight_status

        for line in render_preflight_status(preflight):
            sys.stdout.write(line + "\n")


def _print_job(job: dict[str, Any]) -> None:
    for key in (
        "job_id",
        "task",
        "persona",
        "kind",
        "status",
        "exit_code",
        "branch",
        "workflow_run_id",
        "workflow_card",
        "workflow_phase",
    ):
        if key in job:
            sys.stdout.write(f"{key}: {job.get(key)}\n")


def _print_ready(ready_rows: list[dict[str, Any]]) -> None:
    for row in ready_rows:
        sys.stdout.write(
            f"{row.get('slice_id', '-')}\t{row.get('path', '-')}\t{row.get('plan', '-')}\n"
        )


def _print_work(item: dict[str, Any], *, schema_retry: dict[str, Any] | None = None) -> None:
    sys.stdout.write(f"repo: {item.get('repo')}\n")
    sys.stdout.write(f"work_id: {item.get('work_id')}\n")
    sys.stdout.write(f"title: {item.get('title')}\n")
    sys.stdout.write(f"state: {item.get('state')}\n")
    sys.stdout.write(f"phase: {item.get('phase')}\n")
    sys.stdout.write(
        "facets: " + json.dumps(item.get("facets", []), ensure_ascii=False, sort_keys=True) + "\n"
    )
    # #261 D5：只在真的發生過 schema mismatch retry 時才列出，避免在正常情況下
    # 為每個 work item 多印一行雜訊。
    if schema_retry:
        limit = schema_retry.get("limit")
        for card, count in sorted(schema_retry.get("by_card", {}).items()):
            exhausted = " (exhausted)" if isinstance(limit, int) and count >= limit else ""
            sys.stdout.write(f"schema_retry[{card}]: {count}/{limit}{exhausted}\n")


def _print_doctor(report: dict[str, Any]) -> None:
    for probe in report.get("probes", []):
        sys.stdout.write(f"{str(probe.get('status', '')).upper():4} {probe.get('name')}: {probe.get('detail')}\n")


def _print_service(service: dict[str, Any]) -> None:
    sys.stdout.write(f"instance: {service.get('instance')}\n")
    sys.stdout.write(f"mode: {service.get('mode')}\n")
    sys.stdout.write(f"version: {service.get('version')}\n")
    for unit_name, row in sorted(service.get("units", {}).items()):
        line = (
            f"{unit_name}\tstatus={row.get('status')}\tpid={row.get('pid') or '-'}"
            f"\texec_path={row.get('exec_path') or '-'}\tstale={row.get('stale')}"
        )
        suggestion = row.get("suggestion")
        if suggestion:
            line += f"\tsuggestion={suggestion}"
        sys.stdout.write(line + "\n")


def _run_status(*, json_output: bool) -> int:
    status = status_summary()
    if json_output:
        _json_dump(_inspect_envelope("status", status=status))
        return 0
    _print_status(status)
    return 0


def _run_job(job_id: str, *, json_output: bool) -> int:
    try:
        job = JobRegistry().get_job(job_id)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
    if json_output:
        _json_dump(_inspect_envelope("job", job=job))
        return 0
    _print_job(job)
    return 0


def _run_ready(*, json_output: bool) -> int:
    ready_rows = autonomy.ready_units(
        autonomy.scan_specs(str(paths.specs_root())),
        autonomy.default_is_satisfied,
    )
    if json_output:
        _json_dump(_inspect_envelope("ready", ready=ready_rows))
        return 0
    _print_ready(list(ready_rows))
    return 0


def _monitor_socket_path() -> str:
    try:
        return str(load_config().socket_path)
    except FileNotFoundError:
        return str(default_socket_path())


def _load_work_item(work_id: str, *, repo: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    request = {"kind": "get_work_item", "work_id": work_id}
    if repo is not None:
        request["repo"] = repo
    response = MonitorSocketClient(socket_path=_monitor_socket_path()).request(request)
    if not response.get("ok"):
        raise ValueError(str(response.get("error", f"work item not found: {work_id}")))
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("Monitor response data 不是 JSON object")
    item = data.get("item")
    if not isinstance(item, dict):
        raise ValueError(f"work item not found: {work_id}")
    # #261：schema retry 摘要與 item 平行放在 envelope 上（非 item 欄位），
    # 沿用 Monitor 既有的 work item 讀模型，不需要新增 WorkItem 欄位。
    schema_retry = data.get("schema_retry")
    return item, schema_retry if isinstance(schema_retry, dict) else {}


def _run_work(work_id: str, *, repo: str | None, json_output: bool) -> int:
    item, schema_retry = _load_work_item(work_id, repo=repo)
    if json_output:
        payload: dict[str, Any] = {"item": item}
        if schema_retry:
            payload["schema_retry"] = schema_retry
        _json_dump(_inspect_envelope("work", **payload))
        return 0
    _print_work(item, schema_retry=schema_retry)
    return 0


def _run_doctor(
    *,
    probe_live: bool,
    repo: str | None,
    instance: str,
    json_output: bool,
) -> int:
    doctor = doctor_summary(probe_live=probe_live, repo=repo, instance=instance)
    if json_output:
        _json_dump(_inspect_envelope("doctor", doctor=doctor))
        return 0
    _print_doctor(doctor)
    return 0 if bool(doctor.get("ok")) else 1


def _run_service(*, instance: str, json_output: bool) -> int:
    service = probe_service_runtime(instance)
    if json_output:
        _json_dump(_inspect_envelope("service", service=service))
        return 0
    _print_service(service)
    return 0


def _run_models(*, json_output: bool) -> int:
    """#452 驗收 1：顯示每身分封套值、來源（measured/default）與評測出處。"""

    from paulsha_cortex.coordinator.model_identities import load_model_identities
    from paulsha_cortex.coordinator.model_profile import envelope_display_rows

    rows = envelope_display_rows(load_model_identities())
    if json_output:
        _json_dump(_inspect_envelope("models", models=rows))
        return 0
    for row in rows:
        envelope = row["envelope"]
        source = row["source"]
        ceiling = envelope.get("invariant_ceiling")
        sys.stdout.write(
            f"{row['executor']}/{row['model_id']}\t{row['persona']}\t"
            f"layer={row.get('resolution_layer')}\t"
            f"accepts_bands={envelope.get('accepts_bands')}({source.get('accepts_bands')})\t"
            f"invariant_ceiling={'∅(bypass)' if ceiling is None else ceiling}"
            f"({source.get('invariant_ceiling')})\t"
            f"consistency_scope=({source.get('consistency_scope')})\t"
            f"acceptance_modes=({source.get('acceptance_modes')})\n"
        )
        provenance = row.get("provenance")
        if provenance and provenance.get("fingerprint"):
            fingerprint = provenance["fingerprint"]
            sys.stdout.write(
                f"  provenance: deck={fingerprint.get('deck_id')} "
                f"sha256={str(fingerprint.get('deck_content_sha256'))[:12]}… "
                f"patchmud={fingerprint.get('patchmud_version')} "
                f"profiled_at={provenance.get('profiled_at')}\n"
            )
    return 0


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    try:
        if args.command == "status":
            return _run_status(json_output=args.json)
        if args.command == "job":
            return _run_job(args.job_id, json_output=args.json)
        if args.command == "ready":
            return _run_ready(json_output=args.json)
        if args.command == "work":
            return _run_work(args.work_id, repo=args.repo, json_output=args.json)
        if args.command == "doctor":
            return _run_doctor(
                probe_live=args.probe_live,
                repo=args.repo,
                instance=args.instance,
                json_output=args.json,
            )
        if args.command == "service":
            return _run_service(instance=args.instance, json_output=args.json)
        if args.command == "models":
            return _run_models(json_output=args.json)
    except ValueError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 1
    parser.error(f"unsupported inspect command: {args.command}")
    return 2
