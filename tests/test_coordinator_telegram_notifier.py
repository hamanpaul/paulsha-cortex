from __future__ import annotations

import json

from paulsha_cortex.scripts import coordinator_telegram_notifier as notifier


def test_latest_job_status_reads_jobs_json_from_state_root(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "jobs.json").write_text(
        json.dumps(
            {
                "seq": 3,
                "jobs": [
                    {"task": "topic-a", "status": "running"},
                    {"topic": "topic-a", "status": "done"},
                    {"task": "topic-b", "status": "failed"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert notifier.latest_job_status_by_topic(state_root) == {"topic-a": "done", "topic-b": "failed"}


def test_default_api_token_path_honors_psc_max_root(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_MAX_ROOT", str(tmp_path / "max"))
    assert notifier.default_api_token_path() == tmp_path / "max" / "api-token"
