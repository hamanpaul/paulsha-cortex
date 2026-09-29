"""Receipt-chain-gated purge of trust-root legacy quarantine entries."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from . import backend
from .core import InstallError, InstallReceipt, validate_prior_receipt_handoff

_RETENTION_SECONDS = 30 * 24 * 60 * 60


def _read_receipt(path: Path) -> dict[str, object]:
    return InstallReceipt.load(path).to_dict()


def _qualified_at(document: Mapping[str, object]) -> datetime | None:
    value = document.get("qualified_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _successor_qualified_at(
    adoption_path: Path, adoption: Mapping[str, object], receipts_dir: Path
) -> datetime | None:
    target = {
        "path": str(adoption_path),
        "receipt_id": adoption.get("receipt_id"),
        "plan_sha256": adoption.get("plan_sha256"),
    }
    # Only a direct, receipt-linked successor is used as the retention anchor.
    qualified_successors: list[datetime] = []
    for candidate_path in sorted(receipts_dir.glob("*.json")):
        if candidate_path == adoption_path:
            continue
        try:
            candidate = _read_receipt(candidate_path)
        except (InstallError, OSError, ValueError):
            continue
        if candidate.get("parent_receipt") != target:
            continue
        parent_link = candidate.get("parent_receipt")
        if not isinstance(parent_link, Mapping) or set(parent_link) != {
            "path",
            "receipt_id",
            "plan_sha256",
        }:
            continue
        if candidate.get("state") != "applied" or candidate.get("qualified") is not True:
            continue
        child_plan = candidate.get("plan")
        if not isinstance(child_plan, Mapping):
            continue
        try:
            validate_prior_receipt_handoff(child_plan, InstallReceipt(adoption))
        except InstallError:
            continue
        qualified_at = _qualified_at(candidate)
        if qualified_at is not None:
            qualified_successors.append(qualified_at)
    # If there are multiple direct successors, retain from the newest proof.
    return max(qualified_successors) if qualified_successors else None


def _canonical_report_digest(document: Mapping[str, object]) -> str:
    payload = {key: value for key, value in document.items() if key != "report_sha256"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def build_legacy_purge_report(
    receipt_path: Path, *, receipts_dir: Path | None = None, now: datetime | None = None
) -> dict[str, object]:
    """Build a stable report; absent chain or identity evidence always retains."""

    receipt_path = receipt_path.absolute()
    adoption = _read_receipt(receipt_path)
    plan = adoption.get("plan")
    legacy = adoption.get("legacy_adoption")
    if (
        not isinstance(plan, Mapping)
        or not isinstance(plan.get("legacy_adoption"), Mapping)
        or not isinstance(legacy, Mapping)
        or adoption.get("state") != "applied"
    ):
        raise InstallError("receipt is not an applied legacy adoption receipt")
    quarantine_root = legacy.get("quarantine_root")
    if not isinstance(quarantine_root, str) or not Path(quarantine_root).is_absolute():
        raise InstallError("adoption receipt quarantine root is invalid")
    anchor = _successor_qualified_at(
        receipt_path, adoption, receipts_dir or receipt_path.parent
    )
    evaluated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_ok = anchor is not None and (evaluated - anchor).total_seconds() >= _RETENTION_SECONDS
    entries: list[dict[str, object]] = []
    journal = adoption.get("journal")
    if isinstance(journal, list):
        for entry in journal:
            if not isinstance(entry, Mapping):
                continue
            step = entry.get("step")
            if not isinstance(step, Mapping) or step.get("kind") != "legacy-quarantine":
                continue
            destination = step.get("destination")
            identity = entry.get("quarantine_identity")
            reason: str | None = None
            if entry.get("status") != "completed":
                reason = "adoption-entry-incomplete"
            elif anchor is None:
                reason = "no-qualified-successor-in-receipt-chain"
            elif not age_ok:
                reason = "qualified-successor-retention-under-30-days"
            elif not isinstance(destination, str) or not isinstance(identity, Mapping):
                reason = "adoption-inode-or-tree-digest-missing"
            else:
                try:
                    actual = backend._legacy_quarantine_identity(Path(destination))
                    if actual != dict(identity):
                        reason = "quarantine-drift"
                except (InstallError, OSError, ValueError):
                    reason = "quarantine-drift-or-mount-boundary"
            entries.append(
                {
                    "step_id": entry.get("step_id"),
                    "path": destination,
                    "action": "retain" if reason else "delete",
                    "reason": reason,
                    "identity": dict(identity) if isinstance(identity, Mapping) else None,
                    "qualified_at": anchor.isoformat().replace("+00:00", "Z") if anchor else None,
                }
            )
    report: dict[str, object] = {
        "schema_version": 1,
        "receipt": str(receipt_path),
        "receipt_id": adoption.get("receipt_id"),
        "plan_sha256": adoption.get("plan_sha256"),
        "quarantine_root": quarantine_root,
        "retention_seconds": _RETENTION_SECONDS,
        "entries": entries,
    }
    report["report_sha256"] = _canonical_report_digest(report)
    return report


def purge_legacy_entries(
    receipt: InstallReceipt, report: Mapping[str, object]
) -> list[dict[str, object]]:
    """Delete only report-approved, revalidated entries and audit each outcome."""

    document = receipt._document
    plan = document.get("plan")
    legacy = document.get("legacy_adoption")
    if not isinstance(plan, Mapping) or not isinstance(legacy, Mapping):
        raise InstallError("receipt is not a legacy adoption receipt")
    if report.get("receipt_id") != document.get("receipt_id") or report.get(
        "plan_sha256"
    ) != document.get("plan_sha256"):
        raise InstallError("purge report does not identify the locked adoption receipt")
    root = Path(str(legacy["quarantine_root"]))
    journal = document.setdefault("legacy_purge_journal", [])
    if not isinstance(journal, list):
        raise InstallError("legacy purge journal is invalid")
    results: list[dict[str, object]] = []
    for item in report.get("entries", []):
        if not isinstance(item, Mapping) or item.get("action") != "delete":
            continue
        path = Path(str(item.get("path")))
        identity = item.get("identity")
        step_id = item.get("step_id")
        if not isinstance(identity, Mapping) or not isinstance(step_id, str):
            raise InstallError("purge report entry lacks receipt-bound identity")
        prior = next(
            (
                row
                for row in journal
                if isinstance(row, Mapping) and row.get("step_id") == step_id
            ),
            None,
        )
        if isinstance(prior, Mapping) and prior.get("status") == "deleted":
            continue
        pending = (
            prior
            if isinstance(prior, dict)
            else {"step_id": step_id, "path": str(path), "status": "pending"}
        )
        pending.update(
            {
                "path": str(path),
                "status": "pending",
                "report_sha256": report.get("report_sha256"),
                "successor_qualified_at": item.get("qualified_at"),
            }
        )
        if not isinstance(prior, dict):
            journal.append(pending)
        receipt._persist()
        try:
            backend._discard_legacy_quarantine(
                path,
                identity,
                quarantine_root=root,
                key=f"legacy-purge:{document['receipt_id']}:{step_id}",
            )
        except InstallError as exc:
            pending.update({"status": "retained-drift", "error": str(exc)})
            results.append(dict(pending))
            receipt._persist()
            continue
        pending["status"] = "deleted"
        results.append(dict(pending))
        receipt._persist()
    return results
