"""Shared projection helpers for Manager-owned automatic workflow retries."""

from __future__ import annotations

from typing import Any, Mapping


AUTO_RETRY_HISTORY_SCHEMA = "cortex-workflow-auto-retry/v1"


def automatic_retry_summary(run_or_row: object) -> dict[str, Any]:
    """Return the persisted retry count, current allowance, and builder changes."""

    if isinstance(run_or_row, Mapping):
        limit = run_or_row.get("auto_retry_limit", 2)
        history = run_or_row.get("auto_retry_history", ())
    else:
        limit = getattr(run_or_row, "auto_retry_limit", 2)
        history = getattr(run_or_row, "auto_retry_history", ())
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        limit = 0
    if not isinstance(history, (list, tuple)):
        history = ()
    records = [
        dict(item)
        for item in history
        if isinstance(item, Mapping)
        and item.get("schema") == AUTO_RETRY_HISTORY_SCHEMA
    ]
    switches = [
        {
            "card": row.get("card"),
            "reason": row.get("reason"),
            "from": row.get("builder_before"),
            "to": row.get("builder_after"),
            "job_id": row.get("job_id"),
        }
        for row in records
        if row.get("decision") == "switch-builder"
    ]
    latest = records[-1] if records else None
    remaining = limit
    if latest is not None:
        current_builder = latest.get("builder_after")
        used = max(
            (
                row.get("builder_retry_number", 0)
                for row in records
                if row.get("builder_after") == current_builder
                and isinstance(row.get("builder_retry_number"), int)
                and not isinstance(row.get("builder_retry_number"), bool)
            ),
            default=0,
        )
        remaining = 0 if latest.get("decision") == "exhausted" else max(0, limit - used)
    return {
        "count": sum(row.get("decision") in {"retry", "switch-builder"} for row in records),
        "limit_per_builder": limit,
        "remaining_for_current_builder": remaining,
        "builder_switches": switches,
        "attempts": records,
    }
