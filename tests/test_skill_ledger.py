"""`paulsha_cortex.coordinator.skill_ledger`（issue #204）。

全程用 tmp_path 隔離的 ledger 檔案；`tests/conftest.py` 的 autouse fixture 已把
`PSC_AGENTS_ROOT` 導向每測試獨立的 tmp 目錄，不需再手動 monkeypatch env。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import skill_ledger
from paulsha_cortex.deck.schema import Card


def _card(card_id: str, *, card_class: str = "niche", skill_ref: str | None = None) -> Card:
    return Card(
        id=card_id,
        kind="skill",
        type="headless",
        card_class=card_class,
        skill_ref=skill_ref or f"skills/{card_id}",
    )


def _job(
    *,
    job_id: str = "wf-abc-card-1",
    workflow_card: str | None = "card-a",
    status: str = "exited",
    exit_code: int | None = 0,
    workflow_run_id: str = "wf-abc",
    workflow_claim_key: str | None = "claim-abc",
) -> dict:
    return {
        "job_id": job_id,
        "workflow_card": workflow_card,
        "status": status,
        "exit_code": exit_code,
        "workflow_run_id": workflow_run_id,
        "workflow_claim_key": workflow_claim_key,
    }


def _read_lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestDeriveOutcome:
    def test_exited_zero_is_completed(self) -> None:
        assert skill_ledger.derive_outcome(_job(status="exited", exit_code=0)) == "completed"

    def test_exited_nonzero_is_failed(self) -> None:
        assert skill_ledger.derive_outcome(_job(status="exited", exit_code=1)) == "failed"

    def test_failed_status_is_failed(self) -> None:
        assert skill_ledger.derive_outcome(_job(status="failed", exit_code=None)) == "failed"

    def test_cancelled_status_is_cancelled(self) -> None:
        assert skill_ledger.derive_outcome(_job(status="cancelled", exit_code=None)) == "cancelled"

    def test_non_terminal_status_is_none(self) -> None:
        assert skill_ledger.derive_outcome(_job(status="running", exit_code=None)) is None
        assert skill_ledger.derive_outcome(_job(status="dispatched", exit_code=None)) is None


class TestBuildEvent:
    def test_fields_are_exactly_the_whitelist(self) -> None:
        event = skill_ledger.build_event(
            _job(), card_id="card-a", skill_ref="skills/card-a", outcome="completed", clock=lambda: "T0"
        )
        assert set(event) == set(skill_ledger.LEDGER_FIELDS)
        assert event["event_id"] == "wf-abc-card-1:card-a:completed"
        assert event["schema_version"] == skill_ledger.SCHEMA_VERSION
        assert event["recorded_at"] == "T0"

    def test_rejects_invalid_outcome(self) -> None:
        with pytest.raises(ValueError):
            skill_ledger.build_event(_job(), card_id="card-a", skill_ref="skills/card-a", outcome="bogus")


class TestAppendUsageEvent:
    def test_non_terminal_job_not_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "skill_usage.jsonl"
        result = skill_ledger.append_usage_event(
            _job(status="running"), cards={"card-a": _card("card-a")}, path=path
        )
        assert result is None
        assert not path.exists()

    def test_missing_card_not_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "skill_usage.jsonl"
        result = skill_ledger.append_usage_event(_job(), cards={}, path=path)
        assert result is None
        assert not path.exists()

    def test_missing_workflow_card_not_recorded(self, tmp_path: Path) -> None:
        path = tmp_path / "skill_usage.jsonl"
        result = skill_ledger.append_usage_event(
            _job(workflow_card=None), cards={"card-a": _card("card-a")}, path=path
        )
        assert result is None
        assert not path.exists()

    def test_replay_same_terminal_job_does_not_duplicate(self, tmp_path: Path) -> None:
        # (1) ledger 去重測試：同一 terminal job 重放兩次，ledger 行數不變。
        path = tmp_path / "skill_usage.jsonl"
        cards = {"card-a": _card("card-a")}
        job = _job()
        first = skill_ledger.append_usage_event(job, cards=cards, path=path, clock=lambda: "T0")
        second = skill_ledger.append_usage_event(job, cards=cards, path=path, clock=lambda: "T1")
        assert first is not None
        assert second == first  # 冪等回讀既有事件，不是重寫成新的 recorded_at
        lines = _read_lines(path)
        assert len(lines) == 1
        assert lines[0]["event_id"] == "wf-abc-card-1:card-a:completed"

    def test_different_outcome_is_a_different_event(self, tmp_path: Path) -> None:
        path = tmp_path / "skill_usage.jsonl"
        cards = {"card-a": _card("card-a")}
        skill_ledger.append_usage_event(_job(status="exited", exit_code=0), cards=cards, path=path)
        skill_ledger.append_usage_event(
            _job(job_id="wf-abc-card-2", status="failed", exit_code=1), cards=cards, path=path
        )
        assert len(_read_lines(path)) == 2

    def test_event_never_carries_free_form_payload_fields(self, tmp_path: Path) -> None:
        # 白名單斷言：即便 job 字典夾帶 stdout/log_path 這類自由格式欄位，
        # 落盤事件也絕對不含它們。
        path = tmp_path / "skill_usage.jsonl"
        job = _job()
        job["stdout"] = "super secret build log with a token in it"
        job["log_path"] = "$HOME/.agents/logs/secret.log"
        skill_ledger.append_usage_event(job, cards={"card-a": _card("card-a")}, path=path)
        (line,) = _read_lines(path)
        assert set(line) == set(skill_ledger.LEDGER_FIELDS)
        assert "stdout" not in line
        assert "log_path" not in line


class TestRecordUsageEvents:
    def test_batch_skips_non_recordable_jobs(self, tmp_path: Path) -> None:
        path = tmp_path / "skill_usage.jsonl"
        cards = {"card-a": _card("card-a")}
        jobs = [
            _job(job_id="j1", status="exited", exit_code=0),
            _job(job_id="j2", status="running"),  # 非 terminal，應被跳過
            _job(job_id="j3", workflow_card=None),  # 無 card，應被跳過
        ]
        events = skill_ledger.record_usage_events(jobs, cards=cards, path=path)
        assert len(events) == 1
        assert len(_read_lines(path)) == 1


class TestLoadUsageSummary:
    def test_aggregates_count_last_used_and_outcomes(self, tmp_path: Path) -> None:
        path = tmp_path / "skill_usage.jsonl"
        cards = {"card-a": _card("card-a")}
        skill_ledger.append_usage_event(
            _job(job_id="j1", status="exited", exit_code=0), cards=cards, path=path, clock=lambda: "2026-01-01T00:00:00Z"
        )
        skill_ledger.append_usage_event(
            _job(job_id="j2", status="failed"), cards=cards, path=path, clock=lambda: "2026-02-01T00:00:00Z"
        )
        summary = skill_ledger.load_usage_summary(path)
        stats = summary["card-a"]
        assert stats.sample_count == 2
        assert stats.last_used_at == "2026-02-01T00:00:00Z"
        assert stats.outcome_counts == {"completed": 1, "failed": 1}

    def test_since_filters_out_older_events(self, tmp_path: Path) -> None:
        path = tmp_path / "skill_usage.jsonl"
        cards = {"card-a": _card("card-a")}
        skill_ledger.append_usage_event(
            _job(job_id="j1"), cards=cards, path=path, clock=lambda: "2026-01-01T00:00:00Z"
        )
        skill_ledger.append_usage_event(
            _job(job_id="j2"), cards=cards, path=path, clock=lambda: "2026-03-01T00:00:00Z"
        )
        summary = skill_ledger.load_usage_summary(path, since="2026-02-01T00:00:00Z")
        assert summary["card-a"].sample_count == 1
        assert summary["card-a"].last_used_at == "2026-03-01T00:00:00Z"

    def test_empty_ledger_returns_empty_summary(self, tmp_path: Path) -> None:
        assert skill_ledger.load_usage_summary(tmp_path / "does-not-exist.jsonl") == {}
