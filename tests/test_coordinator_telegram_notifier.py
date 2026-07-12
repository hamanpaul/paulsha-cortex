from __future__ import annotations

import json
from types import SimpleNamespace

from paulsha_cortex.coordinator.registry import COORDINATOR_STATE_SCHEMA_VERSION
from paulsha_cortex.scripts import coordinator_telegram_notifier as notifier


def test_latest_job_status_reads_versioned_jobs_json_from_state_root(tmp_path):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "jobs.json").write_text(
        json.dumps(
            {
                "schema_version": COORDINATOR_STATE_SCHEMA_VERSION,
                "seq": 3,
                "jobs": [
                    {"task": "topic-a", "status": "running"},
                    {"topic": "topic-a", "status": "exited"},
                    {"task": "topic-b", "status": "failed"},
                ],
                "slices": [],
            }
        ),
        encoding="utf-8",
    )

    assert notifier.latest_job_status_by_topic(state_root) == {"topic-a": "exited", "topic-b": "failed"}


def test_default_api_token_path_honors_psc_max_root(monkeypatch, tmp_path):
    monkeypatch.setenv("PSC_MAX_ROOT", str(tmp_path / "max"))
    assert notifier.default_api_token_path() == tmp_path / "max" / "api-token"


def test_main_treats_exited_as_successful_terminal_state(monkeypatch, tmp_path):
    sent_messages: list[str] = []
    meta = [notifier.TaskMeta("ws-a", "topic-a", tmp_path, 1)]
    args = SimpleNamespace(
        run_id="run-1",
        meta_file=str(tmp_path / "meta.tsv"),
        state_root=str(tmp_path / "state"),
        interval_sec=1800,
        api_token_path=str(tmp_path / "token"),
    )

    monkeypatch.setattr(notifier.argparse.ArgumentParser, "parse_args", lambda self: args)
    monkeypatch.setattr(notifier, "load_meta", lambda path: meta)
    monkeypatch.setattr(notifier, "latest_job_status_by_topic", lambda state_root: {"topic-a": "exited"})
    monkeypatch.setattr(notifier, "remaining_count", lambda item: 0)
    monkeypatch.setattr(notifier, "send_notify", lambda token_path, text: sent_messages.append(text))
    monkeypatch.setattr(notifier.time, "time", lambda: 0)
    monkeypatch.setattr(
        notifier.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(AssertionError("unexpected sleep")),
    )

    assert notifier.main() == 0
    assert len(sent_messages) == 1
    assert "成功=1" in sent_messages[0]
    assert "失敗或中止=0" in sent_messages[0]
    assert "狀態=exited" in sent_messages[0]
