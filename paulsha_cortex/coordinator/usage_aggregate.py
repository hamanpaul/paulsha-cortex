"""依 ``workflow_run_id`` 彙總 job 的 token usage（Issue #325）。"""

from __future__ import annotations

from typing import Any

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
)


def aggregate_usage_by_run(jobs: list[dict[str, Any]], workflow_run_id: str) -> dict[str, Any]:
    """篩出 ``job["workflow_run_id"] == workflow_run_id`` 的 job，逐欄位加總
    有 usage 的 job（``sum(v for v in ... if v is not None)``），回傳含
    ``job_count``（該 run 底下所有 job 數）／``jobs_with_usage``（其中有 usage
    的 job 數）的彙總 dict。"""
    run_jobs = [job for job in jobs if job.get("workflow_run_id") == workflow_run_id]
    usage_jobs = [job for job in run_jobs if isinstance(job.get("usage"), dict)]

    totals: dict[str, int] = {field: 0 for field in _USAGE_FIELDS}
    for job in usage_jobs:
        usage = job["usage"]
        for field in _USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int):
                totals[field] += value

    result: dict[str, Any] = dict(totals)
    result["job_count"] = len(run_jobs)
    result["jobs_with_usage"] = len(usage_jobs)
    return result
