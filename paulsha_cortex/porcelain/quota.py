"""`cortex quota observe`／`cortex quota import`／`cortex quota bindings`：
#836 額度觀測 collector CLI，以及 #1116 唯讀 binding 診斷報表。

`observe` 對設定檔內每個 executor 執行唯讀讀取並記錄觀測；`import` 由 Manager
帳號匯入 operator 以 `--output` 落地、之後帶到 Manager 帳號的 observation 檔。
`bindings --report` 唯讀彙總 admission decision store 裡『曾經派工用到、但用
目前設定檔重算仍然沒有任何 binding 涵蓋』的 resolved profile key（見 #1116），
不寫入任何狀態，也不啟動任何 provider CLI。三者都不讀任何 credential 檔，也
不把 provider 原始 payload 印到 stdout/stderr。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from paulsha_cortex.coordinator import quota_admission, quota_collectors, quota_ledger, quota_shadow

from . import COMMANDS, PorcelainCommand, register

_DEFAULT_TIMEOUT_S = 20.0
_KNOWN_EXECUTORS = ("codex", "agy", "copilot", "claude", "cg")


def register_commands() -> None:
    if "quota" in COMMANDS:
        return
    register(
        PorcelainCommand(
            name="quota",
            help="唯讀讀取／匯入 provider 額度觀測（observe/import；見 #836）",
            run=main,
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex quota", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)

    observe = sub.add_parser(
        "observe",
        help="依設定檔對 provider 執行唯讀額度讀取，預設記錄進 quota event ledger",
    )
    observe.add_argument("--config", required=True, help="cortex/quota-pools/v1 設定檔路徑")
    observe.add_argument(
        "--executor",
        action="append",
        choices=_KNOWN_EXECUTORS,
        default=None,
        help="只讀取指定 executor；可重複指定，預設讀取設定檔內有 collector target 的全部 executor",
    )
    observe.add_argument("--dry-run", action="store_true", help="不寫入任何檔案，只印分類結果")
    observe.add_argument("--json", action="store_true", help="輸出機讀摘要（cortex-porcelain/quota-observe-summary/v1）")
    observe.add_argument("--output", default=None, help="把 observation 寫成檔案，不落 ledger")
    observe.add_argument("--timeout-s", type=float, default=_DEFAULT_TIMEOUT_S, help="單次 provider 讀取逾時秒數")

    do_import = sub.add_parser(
        "import",
        help="由 Manager 帳號匯入既有的唯讀 observation 檔（--output 產物）",
    )
    do_import.add_argument("--config", required=True, help="cortex/quota-pools/v1 設定檔路徑")
    do_import.add_argument("--file", required=True, help="待匯入的 observation 檔路徑")
    do_import.add_argument("--json", action="store_true", help="輸出機讀摘要")

    bindings_cmd = sub.add_parser(
        "bindings",
        help="唯讀彙總目前派工用到、但沒有任何 binding 涵蓋的 resolved profile key（見 #1116）",
    )
    bindings_cmd.add_argument(
        "--report", action="store_true",
        help="執行報表（目前唯一支援的動作；明確要求避免未來語意混淆）",
    )
    bindings_cmd.add_argument("--config", required=True, help="cortex/quota-pools/v1 設定檔路徑（讀取目前 bindings）")
    bindings_cmd.add_argument(
        "--store", default=None,
        help="admission decision store 路徑；預設沿用既有 PSC_* 路徑（見 AdmissionDecisionStore）",
    )
    bindings_cmd.add_argument("--json", action="store_true", help="輸出機讀摘要")

    return parser


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    if args.command == "observe":
        return _run_observe(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "bindings":
        return _run_bindings_report(args)
    parser.error(f"unsupported quota command: {args.command}")
    return 2


def _classify_state(observations: int, gaps: list[str]) -> str:
    if not gaps:
        return "ok"
    if observations <= len(gaps):
        return "gap"
    return "partial"


def _run_observe(args: argparse.Namespace) -> int:
    try:
        config = quota_collectors.load_collector_config(args.config)
    except quota_collectors.CollectorConfigError as exc:
        print(f"錯誤: 設定檔載入失敗：{exc}", file=sys.stderr)
        return 1

    configured_executors = config.executors()
    requested = list(dict.fromkeys(args.executor)) if args.executor else list(configured_executors)
    now_ms = int(time.time() * 1000)
    ttl_ms = config.lease_ms or 300_000

    per_executor: dict[str, dict[str, Any]] = {}
    captures: list[quota_collectors.ProviderCapture] = []

    for executor in requested:
        groups = config.groups_for(executor)
        if not groups:
            per_executor[executor] = {
                "state": "skipped",
                "observations": 0,
                "gaps": ["no-collector-targets-configured"],
            }
            continue
        observations = 0
        gaps: list[str] = []
        for group in groups:
            capture = quota_collectors.collect_provider_quota(
                executor,
                profile_key=group.profile_key,
                targets=group.targets,
                descriptors=config.descriptors,
                unit_catalog=config.unit_catalog,
                observed_at_ms=now_ms,
                ttl_ms=ttl_ms,
                timeout_s=args.timeout_s,
            )
            captures.append(capture)
            observations += len(capture.observations)
            gaps.extend(gap.reason for gap in capture.gaps)
        per_executor[executor] = {
            "state": _classify_state(observations, gaps),
            "observations": observations,
            "gaps": gaps,
        }

    if args.dry_run:
        _print_summary(per_executor, json_output=args.json)
        return 0

    all_observations = [observation for capture in captures for observation in capture.observations]

    if args.output:
        try:
            payload = quota_collectors.observation_export_payload(
                all_observations, config_revision=config.config_revision, generated_at_ms=now_ms
            )
            Path(args.output).write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
        except OSError as exc:
            print(f"錯誤: 無法寫入 --output 檔案：{exc}", file=sys.stderr)
            return 1
        _print_summary(per_executor, json_output=args.json)
        return 0

    service = quota_shadow.QuotaShadowService(quota_ledger.QuotaEventLedger())
    ledger_status = quota_collectors.append_captures_to_ledger(service, captures)
    per_executor["_ledger"] = {"status": ledger_status}
    exit_code = 0
    if ledger_status == "ledger-unwritable":
        exit_code = 1
        print(
            "錯誤: quota ledger 路徑不可寫（例如目前執行帳號不是 Manager 帳號）；"
            "改用 --output 落地後由 Manager 帳號執行 `cortex quota import` 匯入。",
            file=sys.stderr,
        )
    _print_summary(per_executor, json_output=args.json)
    return exit_code


def _print_summary(per_executor: dict[str, dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"schema": "cortex-porcelain/quota-observe-summary/v1", "executors": per_executor},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    for executor, info in per_executor.items():
        if executor.startswith("_"):
            continue
        gaps = ",".join(info.get("gaps", [])) or "-"
        print(f"{executor}\tstate={info.get('state')}\tobservations={info.get('observations')}\tgaps={gaps}")
    ledger = per_executor.get("_ledger")
    if ledger:
        print(f"ledger: {ledger.get('status')}")


def _run_import(args: argparse.Namespace) -> int:
    try:
        config = quota_collectors.load_collector_config(args.config)
    except quota_collectors.CollectorConfigError as exc:
        print(f"錯誤: 設定檔載入失敗：{exc}", file=sys.stderr)
        return 1

    try:
        raw_text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"錯誤: 無法讀取匯入檔：{exc}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"錯誤: 匯入檔不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    # 信任邊界：只接受 `observe --output` 產生的 export envelope，並逐筆核對
    # source contract／config_revision／collector_targets／observed_at 新鮮度；
    # 任一筆不符即整份拒絕（不部分匯入）。錯誤訊息只帶 reason code，不含原始
    # 內容（見 #836 對抗審查第九輪 MAJOR-3；README 已記錄此處的信任邊界）。
    try:
        observations = quota_collectors.validate_import_payload(
            payload, config=config, now_ms=int(time.time() * 1000),
        )
    except quota_collectors.ImportValidationError as exc:
        print(f"錯誤: 匯入檔未通過信任邊界檢查（{exc}）", file=sys.stderr)
        return 1

    service = quota_shadow.QuotaShadowService(quota_ledger.QuotaEventLedger())
    results = {"accepted": 0, "duplicates": 0, "conflicts": 0, "invalid": 0}
    for item in observations:
        outcome = service.record_external_observation(
            item, descriptors=config.descriptors, unit_catalog=config.unit_catalog
        )
        if outcome.status == "accepted":
            results["accepted"] += outcome.accepted
        elif outcome.status == "duplicate":
            results["duplicates"] += outcome.duplicates
        elif outcome.status == "conflict":
            results["conflicts"] += outcome.conflicts
        else:
            results["invalid"] += 1

    if args.json:
        print(
            json.dumps(
                {"schema": "cortex-porcelain/quota-import-summary/v1", **results},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(
            f"accepted={results['accepted']} duplicates={results['duplicates']} "
            f"conflicts={results['conflicts']} invalid={results['invalid']}"
        )
    return 0 if results["invalid"] == 0 else 1


def _run_bindings_report(args: argparse.Namespace) -> int:
    """#1116：唯讀彙總『目前派工用到、但用目前設定檔重算仍然沒有任何
    binding 涵蓋』的 resolved profile key——只讀 admission decision store 與
    quota-pools 設定檔，不寫任何狀態，也不呼叫任何 provider CLI／shadow／
    reservation。"""
    if not args.report:
        print("錯誤: 目前 `cortex quota bindings` 只支援 --report", file=sys.stderr)
        return 2

    try:
        config = quota_admission.load_quota_pools_config(args.config)
    except quota_admission.QuotaPoolsConfigError as exc:
        print(f"錯誤: 設定檔載入失敗：{exc}", file=sys.stderr)
        return 1
    if config is None:
        print(f"錯誤: 設定檔不存在：{args.config}", file=sys.stderr)
        return 1

    try:
        store = (
            quota_admission.AdmissionDecisionStore(args.store)
            if args.store
            else quota_admission.AdmissionDecisionStore()
        )
        rows = store.all_rows()
    except quota_admission.AdmissionDecisionCorrupt as exc:
        print(f"錯誤: admission decision store 損毀：{exc}", file=sys.stderr)
        return 1

    usages = quota_admission.unbound_profile_keys_report(rows, bindings=config.bindings)

    if args.json:
        print(
            json.dumps(
                {
                    "schema": "cortex-porcelain/quota-bindings-report/v1",
                    "config_revision": config.config_revision,
                    "unbound": [usage.to_dict() for usage in usages],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if not usages:
        print("目前沒有任何派工用到、且現有設定沒有 binding 涵蓋的 resolved profile key。")
        return 0
    for usage in usages:
        print(
            f"executor={usage.executor}\tmodel_id={usage.model_id}\t"
            f"profile_key={usage.profile_key}\tdecision_count={usage.decision_count}\t"
            f"last_seen_ms={usage.last_seen_ms}"
        )
    return 0
