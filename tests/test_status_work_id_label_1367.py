from __future__ import annotations

import json

from paulsha_cortex.porcelain import inspect as porcelain_inspect


def _status(*, work_id: str | None, run_id: str, slice_id: str) -> dict[str, object]:
    entry: dict[str, object] = {
        "run_id": run_id,
        "slice_id": slice_id,
        "blocking_reason": {
            "reason": "resume-workflow-failed",
            "detail": "worker failed",
            "source": "manager_daemon.resume_workflow",
        },
        "quota_decision": {
            "wait": {"reason": "quota_wait", "detail": "quota unavailable"},
            "personas": {
                "builder": {
                    "available": False,
                    "stale": False,
                    "stale_reason": "quota exhausted",
                }
            },
        },
        "provider_outcome": {
            "outcome": "transient",
            "authority": "provider",
            "retryable": True,
        },
    }
    if work_id is not None:
        entry["work_id"] = work_id
    return {"updated_at": "2026-10-09T00:00:00Z", "attention": [entry]}


def test_status_summary_rows_label_work_and_run_id(capsys) -> None:
    porcelain_inspect._print_status(
        _status(work_id="status-work-id-label", run_id="workflow-1367", slice_id="slice-1367")
    )

    lines = capsys.readouterr().out.splitlines()
    assert any(
        line.startswith("  provider_failure[status-work-id-label (workflow-1367)]: transient ")
        for line in lines
    )
    assert any(
        line.startswith(
            "  needs_human[status-work-id-label (workflow-1367)]: resume-workflow-failed: worker failed "
        )
        for line in lines
    )
    assert any(
        line.startswith("  quota_wait[status-work-id-label (workflow-1367)]: quota_wait: quota unavailable")
        for line in lines
    )
    assert any(
        line.startswith("  quota_decision[status-work-id-label (workflow-1367)/builder]: unavailable ")
        for line in lines
    )


def test_status_summary_rows_without_work_id_keep_existing_labels(capsys) -> None:
    porcelain_inspect._print_status(
        _status(work_id=None, run_id="workflow-legacy", slice_id="slice-legacy")
    )

    lines = capsys.readouterr().out.splitlines()
    assert any(line.startswith("  provider_failure[slice-legacy]: transient ") for line in lines)
    assert any(
        line.startswith("  needs_human[workflow-legacy]: resume-workflow-failed: worker failed ")
        for line in lines
    )
    assert any(line.startswith("  quota_wait[workflow-legacy]: quota_wait: quota unavailable") for line in lines)
    assert any(line.startswith("  quota_decision[workflow-legacy/builder]: unavailable ") for line in lines)


def test_status_json_output_is_unchanged(monkeypatch, capsys) -> None:
    status = _status(work_id="status-work-id-label", run_id="workflow-1367", slice_id="slice-1367")
    monkeypatch.setattr(porcelain_inspect, "status_summary", lambda: status)

    assert porcelain_inspect.main(["status", "--json"]) == 0

    expected = json.dumps(
        {"schema": "cortex-porcelain/inspect/v1", "command": "status", "status": status},
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"
    assert capsys.readouterr().out == expected
