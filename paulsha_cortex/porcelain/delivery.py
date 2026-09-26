"""#845 requirement coverage 查詢與可重建衍生索引入口。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from paulsha_cortex import runtime_attestation
from paulsha_cortex.config import paths
from paulsha_cortex.config.runtime import selected_instance
from paulsha_cortex.coordinator import (
    claim,
    github_delivery,
    live_receipt_validators,
    requirement_delivery,
)

from . import COMMANDS, PorcelainCommand, register


def register_commands() -> None:
    if "delivery" in COMMANDS:
        return
    register(
        PorcelainCommand(
            name="delivery",
            help="檢查需求交付缺額；reconcile 只以 CAS 更新衍生索引",
            run=main,
        )
    )


def _build_parser():
    parser = argparse.ArgumentParser(prog="cortex delivery")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="唯讀顯示最近一次需求交付索引與 gap")
    status.add_argument("--index", default=None, help="索引檔（預設 PSC_COORDINATOR_ROOT/requirement-delivery/index.json）")

    for name, help_text in (
        ("gaps", "重新驗證可信來源並輸出逐條需求缺額；唯讀"),
        ("reconcile", "重新驗證可信來源並 CAS 更新可重建索引"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--manifest", required=True, help="版本化 requirement manifest JSON")
        command.add_argument("--snapshot", required=True, help="producer 輸出的 evidence mapping snapshot JSON")
        command.add_argument("--source-root", default=".", help="accepted source locator 的根目錄")
        command.add_argument("--checkout", action="append", default=[], metavar="REPO=PATH", help="canonical checkout；可重複指定")
        if name == "reconcile":
            command.add_argument("--expected-index-revision", default=None, help="只在索引目前 SHA-256 相同時更新，供操作者 CAS")
    return parser


def _load_json_file(path_value: str) -> object:
    path = Path(path_value).expanduser().absolute()
    value, _raw = requirement_delivery._load_json_beneath(path.parent, path.name)
    return value


def _absolute_file(value: str | None, default: Path) -> Path:
    return (Path(value).expanduser() if value else default).absolute()


def _checkout_map(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        repo, separator, raw_path = value.partition("=")
        if not separator or not repo or not raw_path or repo in result:
            raise ValueError("checkout 必須是唯一的 REPO=PATH")
        result[repo] = Path(raw_path).expanduser().absolute()
    return result


def _runtime_state_root(service: str, instance: str, _untrusted_hint: object) -> Path:
    """只從現行 runtime path contract 選 state root，不採用 snapshot 提供的路徑。"""
    if instance != selected_instance():
        raise ValueError("loaded runtime instance differs from active instance")
    if service == "manager":
        return paths.coordinator_root()
    if service == "monitor":
        return paths.monitor_state_root()
    raise ValueError("loaded runtime service is not supported")


def _runtime_status_report(service: str, instance: str) -> dict[str, Any]:
    """消費既有 service status/doctor 投影，沿用其 unit PID 與 loaded receipt 比對。"""
    if service not in {"manager", "monitor"} or instance != selected_instance():
        return {"status": "unknown", "reason": "loaded-runtime-service-or-instance-unavailable"}
    try:
        from .service import _status_payload

        status = _status_payload(instance)
        loaded_runtime = status.get("loaded_runtime")
        report = loaded_runtime.get(service) if isinstance(loaded_runtime, dict) else None
        if isinstance(report, dict):
            return report
    except Exception:
        pass
    return {"status": "unknown", "reason": "service-loaded-runtime-projection-unavailable"}


def _emit(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _status(index_path: Path) -> dict[str, Any]:
    index = requirement_delivery.read_index(index_path)
    mappings = [row for row in index["mappings"] if isinstance(row, dict)]
    return {
        "schema": "cortex/requirement-delivery-status/v1",
        "view": "last-reconciled-index",
        "index_generation": index["generation"],
        "index_revision": index.get("revision"),
        "manifest_id": index.get("manifest_id"),
        "manifest_sha256": index.get("manifest_sha256"),
        "snapshot_sha256": index.get("snapshot_sha256"),
        "reconcile_receipt": index.get("reconcile_receipt"),
        "closure_readiness": (index.get("reconcile_receipt") or {}).get("coverage"),
        "mapping_status_counts": dict(sorted(Counter(row.get("status", "unknown") for row in mappings).items())),
        "mappings": mappings,
        "gaps": index.get("gaps", []),
        "extensions": index.get("extensions", {}),
    }


def main(argv: Sequence[str]) -> int:
    args = _build_parser().parse_args(list(argv))
    coordinator_root = paths.coordinator_root()
    default_index = paths.requirement_delivery_index_root() / "index.json"
    index_path = _absolute_file(getattr(args, "index", None), default_index)
    try:
        if args.command == "status":
            _emit(_status(index_path))
            return 0

        manifest = _load_json_file(args.manifest)
        snapshot = _load_json_file(args.snapshot)
        checkouts = _checkout_map(args.checkout)

        def resolve_checkout(repo: str) -> Path:
            checkout = checkouts.get(repo)
            if checkout is None:
                raise ValueError("canonical checkout is not declared for mapped repository")
            return checkout

        def load_authority(repo: str, work_id: str):
            return claim.load_work_authority(
                repo=repo,
                work_id=work_id,
            )

        common = {
            "source_root": Path(args.source_root).expanduser().absolute(),
            "evidence_root": coordinator_root,
            "coordinator_root": coordinator_root,
            "authority_loader": load_authority,
            "github_client": github_delivery.GitHubDeliveryClient(),
            "checkout_resolver": resolve_checkout,
            "current_artifact": runtime_attestation.artifact_identity(),
            "now_epoch": time.time(),
            "runtime_state_resolver": _runtime_state_root,
            "runtime_status_resolver": _runtime_status_report,
            "live_receipt_validator": live_receipt_validators.governed_live_receipt_validator,
        }
        if args.command == "gaps":
            _emit(requirement_delivery.inspect_delivery(manifest, snapshot, **common))
            return 0
        if args.command == "reconcile":
            result = requirement_delivery.reconcile_delivery(
                manifest,
                snapshot,
                index_path=index_path,
                expected_index_revision=args.expected_index_revision,
                **common,
            )
            _emit({
                "schema": "cortex/requirement-delivery-reconcile/v1",
                "changed": result["changed"],
                "index_generation": result["index"].get("generation"),
                "index_revision": result.get("index_revision"),
                "pending_reason": result.get("pending_reason"),
                "report": result["report"],
            })
            return 0 if result["report"].get("closure_readiness") == "ready" else 1
    except requirement_delivery.IndexConflict:
        _emit({
            "schema": "cortex/requirement-delivery-reconcile/v1",
            "closure_readiness": "pending",
            "pending_reason": "index-revision-changed-during-reconcile",
        })
        return 3
    except Exception as exc:
        # 不透出 gh/authenticator 原始 stderr、環境值或本機絕對路徑。
        _emit({
            "schema": "cortex/requirement-delivery-error/v1",
            "error": type(exc).__name__,
            "reason": "input-or-trusted-evidence-unavailable",
        })
        return 2
    return 2
