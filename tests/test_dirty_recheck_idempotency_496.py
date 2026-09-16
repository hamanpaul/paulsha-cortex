"""#496：dirty recheck 結果未變時 `complete_tick` 不得每 tick 追加 action／evidence_history。

fixture 沿 `tests/test_pre_candidate_recovery.py::test_candidate_worktree_dirty_reevaluation_on_tick`：
本機 fixture repo、needs_human＋`candidate-worktree-dirty` 的 slice、可計數的假 verification runner。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from paulsha_cortex.coordinator import manager, verification
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import JobRegistry

SLICE_ID = "slice-496"
STALE_CANDIDATE = "0" * 40
NEW_CANDIDATE = "1" * 40


def _dirty_payload(candidate: str = STALE_CANDIDATE, details: dict | None = None) -> dict:
    return {
        "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
        "slice_id": SLICE_ID,
        "candidate": candidate,
        "status": "needs_human",
        "summary": "candidate-worktree-dirty",
        "details": details or {},
    }


class _Fixture:
    def __init__(self, tmp_path: Path, monkeypatch, git_origin) -> None:
        monkeypatch.setattr(manager, "_pinned_input_mismatches", lambda _slice: [])
        origin = git_origin("example/acme")
        origin.commit({"README.md": "seed\n"})
        origin.publish()
        self.repo_root = origin.checkout
        self.tmp_path = tmp_path
        self.registry = JobRegistry(state_path=tmp_path / "jobs.json")

        builder_job = self.registry.create_job(
            task=SLICE_ID,
            persona="builder",
            branch=f"feature/{SLICE_ID}",
            pane="",
            worktree=str(tmp_path / "wt" / f"feature-{SLICE_ID}"),
        )
        self.registry.update_headless_result(builder_job["job_id"], status="exited", exit_code=0)
        self.registry.create_slice(
            slice_id=SLICE_ID,
            spec_path=str(self.repo_root / "specs" / f"{SLICE_ID}.md"),
            spec_hash="spec-sha",
            plan_path=f"plans/{SLICE_ID}.md",
            plan_hash="plan-sha",
            target_branch="main",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=None,
            candidate=STALE_CANDIDATE,
            verification={"docs_class": "trivial", "review_policy": "not-required"},
        )
        # contract hash 由 registry 決定；本票只斷言它在整個 recheck 過程不被改動（D09）。
        self.contract_hash = self.registry.get_slice(SLICE_ID)["verification"]["hash"]
        self.dirty_evidence = verification.write_verification_evidence(
            _dirty_payload(), coordinator_root=tmp_path
        )
        self.registry.update_slice(
            SLICE_ID,
            state="needs_human",
            gate_state="needs_human",
            current_verification_evidence_hash=self.dirty_evidence["hash"],
            current_evidence_refs=[self.dirty_evidence["path"]],
        )
        # 預置 terminal builder job 的 handoff manifest（design D07「tick isolation」）：
        # `complete_tick` 對同 job_id 的既存 manifest 真冪等跳過，於是每個 tick 只剩
        # dirty recheck 這一條路徑會呼叫 verification runner，不被 terminal replay 干擾。
        self.handoff_dir = tmp_path / "handoff"
        self.handoff_dir.mkdir()
        (self.handoff_dir / f"{SLICE_ID}.json").write_text(
            json.dumps(
                {
                    "job_id": builder_job["job_id"],
                    "gate_status": "needs_human",
                    "gate_reason": None,
                    "verification_evidence_path": self.dirty_evidence["path"],
                }
            ),
            encoding="utf-8",
        )
        self.runner_calls = 0
        self.runner_result = lambda: verification.write_verification_evidence(
            _dirty_payload(), coordinator_root=tmp_path
        )
        self.dispatcher = Dispatcher(
            self.registry, pane_sender=MagicMock(), worktree_creator=MagicMock()
        )

    def _runner(self, *_args, **_kwargs):
        self.runner_calls += 1
        return self.runner_result()

    def tick(self, times: int = 1) -> None:
        for _ in range(times):
            manager.complete_tick(
                self.dispatcher,
                handoff_dir=str(self.handoff_dir),
                verification_runner=self._runner,
            )

    def row(self) -> dict:
        return self.registry.get_slice(SLICE_ID)

    def counts(self) -> tuple[int, int]:
        row = self.row()
        return len(row["actions"]), len(row["evidence_history"])


@pytest.fixture
def fx(tmp_path: Path, monkeypatch, git_origin) -> _Fixture:
    return _Fixture(tmp_path, monkeypatch, git_origin)


def test_unchanged_dirty_result_is_noop_across_ten_ticks(fx: _Fixture) -> None:
    """D06：同候選／同內容連續 10 tick，runner 仍每次執行，但 action／history 不增長。"""
    baseline = fx.counts()
    fx.tick(10)
    assert fx.runner_calls == 10
    assert fx.counts() == baseline
    row = fx.row()
    assert row["state"] == "needs_human"
    assert row["gate_state"] == "needs_human"
    assert row["current_verification_evidence_hash"] == fx.dirty_evidence["hash"]
    assert row["current_evidence_refs"] == [fx.dirty_evidence["path"]]
    assert row["verification"]["hash"] == fx.contract_hash


def test_changed_content_at_same_path_records_exactly_once(fx: _Fixture) -> None:
    """D07：同 evidence path 但 canonical 內容改變 → 恰好追加一筆；之後相同結果再度 no-op。

    這裡直接覆寫隔離 fixture 內的 evidence 檔模擬「同路徑不同內容」，不經 production
    writer（writer 對既有路徑的內容衝突本就 fail-closed）。
    """
    fx.tick(3)
    baseline = fx.counts()
    path = Path(fx.dirty_evidence["path"])
    changed = verification.validate_verification_evidence(
        _dirty_payload(details={"dirty_paths": ["README.md"]})
    )
    changed_hash = verification.canonical_json_hash(changed)
    assert changed_hash != fx.dirty_evidence["hash"]
    path.write_text(json.dumps(changed), encoding="utf-8")
    fx.runner_result = lambda: {"path": str(path), "hash": changed_hash, "payload": changed}

    fx.tick(1)
    actions, history = fx.counts()
    assert (actions, history) == (baseline[0] + 1, baseline[1] + 1)
    row = fx.row()
    assert row["current_verification_evidence_hash"] == changed_hash
    assert row["current_evidence_refs"] == [str(path)]
    assert row["actions"][-1]["action"] == "verification-failed"

    fx.tick(4)
    assert fx.counts() == (actions, history)
    assert fx.row()["verification"]["hash"] == fx.contract_hash


def test_dirty_to_verified_with_new_candidate_transitions_once(fx: _Fixture) -> None:
    """D08：dirty→verified（候選改變）恰好記一次，原 cleanup 脫困行為保留。"""
    fx.tick(2)
    baseline = fx.counts()
    verified_payload = {
        "schema_version": verification.VERIFICATION_SCHEMA_VERSION,
        "slice_id": SLICE_ID,
        "candidate": NEW_CANDIDATE,
        "status": "verified",
        "summary": "verification-succeeded",
        "details": {},
    }
    fx.runner_result = lambda: verification.write_verification_evidence(
        verified_payload, coordinator_root=fx.tmp_path
    )
    fx.tick(1)
    actions, history = fx.counts()
    assert (actions, history) == (baseline[0] + 1, baseline[1] + 1)
    row = fx.row()
    assert row["candidate"] == NEW_CANDIDATE
    assert row["state"] in {"verified", "completed"}
    assert row["actions"][-1]["action"] == "verification-passed"


def test_same_content_with_missing_hash_or_stale_refs_repairs_exactly_once(fx: _Fixture) -> None:
    """D02／D04／D08：內容相同但 slice 的 hash 缺失或 refs 指向別處，仍算一次真實轉換，之後 no-op。"""
    # (a) refs 指向同內容的另一個檔案：ref path 不同不得視為未變。
    copy_path = fx.tmp_path / "copy-of-dirty-evidence.json"
    shutil.copy(fx.dirty_evidence["path"], copy_path)
    fx.registry.update_slice(SLICE_ID, current_evidence_refs=[str(copy_path)])
    baseline = fx.counts()
    fx.tick(1)
    assert fx.counts() == (baseline[0] + 1, baseline[1] + 1)
    assert fx.row()["current_evidence_refs"] == [fx.dirty_evidence["path"]]
    fx.tick(3)
    assert fx.counts() == (baseline[0] + 1, baseline[1] + 1)

    # (b) 缺合法 current hash：不能以「None == None」或空值當相等。
    row = fx.registry._find_slice(SLICE_ID)
    row["current_verification_evidence_hash"] = None
    baseline = fx.counts()
    fx.tick(1)
    assert fx.counts() == (baseline[0] + 1, baseline[1] + 1)
    assert fx.row()["current_verification_evidence_hash"] == fx.dirty_evidence["hash"]
    fx.tick(3)
    assert fx.counts() == (baseline[0] + 1, baseline[1] + 1)


def test_invalid_new_evidence_stays_fail_closed_and_writes_nothing(fx: _Fixture) -> None:
    """D05／D09：新 evidence hash／payload 不符時沿既有保守路徑：不放行、不寫入、不改 hash。"""
    fx.tick(1)
    baseline = fx.counts()
    before = fx.row()
    good = fx.dirty_evidence
    fx.runner_result = lambda: {"path": good["path"], "hash": "f" * 64, "payload": good["payload"]}
    fx.tick(3)
    assert fx.runner_calls == 4
    assert fx.counts() == baseline
    after = fx.row()
    assert after["state"] == "needs_human"
    assert after["current_verification_evidence_hash"] == before["current_verification_evidence_hash"]
    assert after["current_evidence_refs"] == before["current_evidence_refs"]
    assert after["verification"]["hash"] == fx.contract_hash

    # 不可讀的 evidence 檔同樣 fail-closed。
    Path(good["path"]).write_text("{not json", encoding="utf-8")
    fx.runner_result = lambda: dict(good)
    fx.tick(2)
    assert fx.counts() == baseline
    assert fx.row()["current_verification_evidence_hash"] == before["current_verification_evidence_hash"]
