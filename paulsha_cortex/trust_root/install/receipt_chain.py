"""Locate the one receipt in force by following receipt-chain links (#1263).

`cortex service status` picks the newest receipt by mtime, which is wrong right
after a rollback.  The chain is authoritative instead: the effective receipt is
the applied and qualified receipt that no applied and qualified successor names
as its ``parent_receipt``.  Links are compared exactly as
``legacy_purge._successor_qualified_at`` does (path, receipt_id and plan_sha256
together).  Anything that prevents a unique answer stops the caller.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .core import InstallError, InstallReceipt

_SCAN_LIMIT = 1024


def receipt_directory(state_root: Path) -> Path:
    return state_root.parent / f"{state_root.name}-install-receipts"


def _link(path: Path, document: Mapping[str, object]) -> dict[str, object]:
    return {
        "path": str(path),
        "receipt_id": document.get("receipt_id"),
        "plan_sha256": document.get("plan_sha256"),
    }


def effective_receipt(state_root: Path) -> InstallReceipt:
    directory = receipt_directory(state_root)
    if directory.is_symlink() or not directory.is_dir():
        raise InstallError(f"install receipt directory is unavailable: {directory}")
    paths = sorted(
        path
        for path in directory.iterdir()
        if path.name.endswith(".json") and not path.name.startswith(".")
    )
    if len(paths) > _SCAN_LIMIT:
        raise InstallError(f"install receipt directory exceeds the scan limit: {directory}")
    qualified: list[tuple[Path, InstallReceipt]] = []
    for path in paths:
        try:
            receipt = InstallReceipt.load(path)
        except InstallError as exc:
            raise InstallError(
                f"cannot decide the effective receipt; {path.name} is unreadable: {exc}"
            ) from exc
        document = receipt.to_dict()
        plan = document.get("plan")
        roots = plan.get("roots") if isinstance(plan, Mapping) else None
        if not isinstance(roots, Mapping) or roots.get("state") != str(state_root):
            raise InstallError(
                f"cannot decide the effective receipt; {path.name} belongs to another state root"
            )
        if document.get("state") == "applied" and document.get("qualified") is True:
            qualified.append((path, receipt))
    successor_links = [receipt.to_dict().get("parent_receipt") for _path, receipt in qualified]
    heads = [
        (path, receipt)
        for path, receipt in qualified
        if _link(path, receipt.to_dict()) not in successor_links
    ]
    if len(heads) != 1:
        names = ", ".join(path.name for path, _receipt in heads) or "none"
        raise InstallError(
            "cannot decide the effective receipt: "
            f"{len(heads)} applied and qualified receipts have no qualified successor ({names})"
        )
    return heads[0][1]
