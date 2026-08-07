"""`paulsha_cortex.coordinator.skill_janitor`（issue #204）。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import skill_janitor
from paulsha_cortex.coordinator.skill_ledger import SkillUsageStats
from paulsha_cortex.deck.schema import Card


def _card(card_id: str, *, card_class: str = "niche") -> Card:
    return Card(id=card_id, kind="skill", type="headless", card_class=card_class, skill_ref=f"skills/{card_id}")


def _stats(card_id: str, *, sample_count: int, last_used_at: str | None) -> SkillUsageStats:
    return SkillUsageStats(card_id=card_id, sample_count=sample_count, last_used_at=last_used_at, outcome_counts={})


NOW = "2026-08-07T00:00:00Z"
STALE = "2026-07-01T00:00:00Z"  # 早於 now - 30 天
RECENT = "2026-08-06T00:00:00Z"  # 觀測窗內


def _now() -> str:
    return NOW


class TestFindColdSkillsThresholdBoundary:
    # (2) 閾值邊界測試——min_samples 邊界 ±1。

    def test_sample_count_equal_to_min_samples_is_sufficient(self) -> None:
        cards = {"card-a": _card("card-a")}
        usage = {"card-a": _stats("card-a", sample_count=5, last_used_at=STALE)}
        cold = skill_janitor.find_cold_skills(usage, cards, min_samples=5, observation_window_days=30, now=_now)
        assert cold == ["card-a"]

    def test_sample_count_one_below_min_samples_is_insufficient(self) -> None:
        cards = {"card-a": _card("card-a")}
        usage = {"card-a": _stats("card-a", sample_count=4, last_used_at=STALE)}
        cold = skill_janitor.find_cold_skills(usage, cards, min_samples=5, observation_window_days=30, now=_now)
        assert cold == []

    def test_no_usage_record_at_all_is_insufficient(self) -> None:
        cards = {"card-a": _card("card-a")}
        cold = skill_janitor.find_cold_skills({}, cards, min_samples=5, observation_window_days=30, now=_now)
        assert cold == []

    def test_recent_use_within_window_is_not_cold(self) -> None:
        cards = {"card-a": _card("card-a")}
        usage = {"card-a": _stats("card-a", sample_count=10, last_used_at=RECENT)}
        cold = skill_janitor.find_cold_skills(usage, cards, min_samples=5, observation_window_days=30, now=_now)
        assert cold == []


class TestFindColdSkillsExemption:
    def test_core_and_emergency_are_never_cold_even_with_stale_heavy_usage(self) -> None:
        cards = {
            "core-a": _card("core-a", card_class="core"),
            "emergency-a": _card("emergency-a", card_class="emergency"),
        }
        usage = {
            "core-a": _stats("core-a", sample_count=100, last_used_at=STALE),
            "emergency-a": _stats("emergency-a", sample_count=100, last_used_at=STALE),
        }
        cold = skill_janitor.find_cold_skills(usage, cards, min_samples=5, observation_window_days=30, now=_now)
        assert cold == []

    def test_flipping_card_class_from_exempt_to_niche_flips_the_verdict(self) -> None:
        # (3) 豁免測試：同一 fixture 只把 card_class 從 core/emergency 改成 niche，
        # cold 判定必須翻紅（True）——證明豁免真的在擋，不是巧合通過。
        usage = {"skill-x": _stats("skill-x", sample_count=10, last_used_at=STALE)}

        exempt_cards = {"skill-x": _card("skill-x", card_class="core")}
        assert skill_janitor.find_cold_skills(
            usage, exempt_cards, min_samples=5, observation_window_days=30, now=_now
        ) == []

        niche_cards = {"skill-x": _card("skill-x", card_class="niche")}
        assert skill_janitor.find_cold_skills(
            usage, niche_cards, min_samples=5, observation_window_days=30, now=_now
        ) == ["skill-x"]


class TestProposePark:
    def test_creates_one_proposal_file_per_skill(self, tmp_path: Path) -> None:
        cards = {"a": _card("a"), "b": _card("b")}
        out_dir = tmp_path / "proposals"
        result = skill_janitor.propose_park(
            ["a", "b"], reason="stale", evidence_window_days=30, cards=cards, out_dir=out_dir, clock=lambda: NOW
        )
        assert {p["skill_id"] for p in result["created"]} == {"a", "b"}
        assert result["skipped_already_pending"] == []
        files = sorted(out_dir.glob("*.json"))
        assert len(files) == 2
        for f in files:
            payload = json.loads(f.read_text(encoding="utf-8"))
            assert payload["status"] == "pending"
            assert payload["schema"] == skill_janitor.PROPOSAL_SCHEMA

    def test_rejects_exempt_skill_even_if_caller_bypassed_find_cold_skills(self, tmp_path: Path) -> None:
        # 第二道防呆：propose_park 自己也要擋 core/emergency，不能只信任呼叫端。
        cards = {"core-a": _card("core-a", card_class="core")}
        with pytest.raises(ValueError):
            skill_janitor.propose_park(
                ["core-a"], reason="stale", evidence_window_days=30, cards=cards, out_dir=tmp_path / "proposals",
                clock=lambda: NOW,
            )
        assert not (tmp_path / "proposals").exists() or list((tmp_path / "proposals").glob("*.json")) == []

    def test_repeated_call_does_not_duplicate_pending_proposal(self, tmp_path: Path) -> None:
        cards = {"a": _card("a")}
        out_dir = tmp_path / "proposals"
        skill_janitor.propose_park(
            ["a"], reason="stale", evidence_window_days=30, cards=cards, out_dir=out_dir, clock=lambda: NOW
        )
        second = skill_janitor.propose_park(
            ["a"], reason="stale", evidence_window_days=30, cards=cards, out_dir=out_dir, clock=lambda: "2026-08-08T00:00:00Z"
        )
        assert second["created"] == []
        assert second["skipped_already_pending"] == ["a"]
        assert len(list(out_dir.glob("*.json"))) == 1


class TestRunJanitorTickProposalOnly:
    def test_two_consecutive_ticks_never_change_park_state(self, tmp_path: Path) -> None:
        # (5) proposal-only 測試：未核准 proposal 時連跑兩次 janitor tick，
        # park 狀態不得改變。
        ledger_path = tmp_path / "skill_usage.jsonl"
        cards = {"a": _card("a")}
        from paulsha_cortex.coordinator import skill_ledger

        job = {
            "job_id": "wf-1-a-1", "workflow_card": "a", "status": "exited", "exit_code": 0,
            "workflow_run_id": "wf-1", "workflow_claim_key": "claim-1",
        }
        skill_ledger.append_usage_event(job, cards=cards, path=ledger_path, clock=lambda: STALE)
        for extra in range(1, 5):
            skill_ledger.append_usage_event(
                {**job, "job_id": f"wf-1-a-{extra + 1}"}, cards=cards, path=ledger_path, clock=lambda: STALE
            )

        state_path = tmp_path / "skill_park.json"
        proposals_dir = tmp_path / "proposals"
        assert not state_path.exists()

        skill_janitor.run_janitor_tick(
            cards=cards, ledger_path=ledger_path, proposals_dir=proposals_dir,
            min_samples=5, observation_window_days=30, now=_now,
        )
        assert not state_path.exists()
        skill_janitor.run_janitor_tick(
            cards=cards, ledger_path=ledger_path, proposals_dir=proposals_dir,
            min_samples=5, observation_window_days=30, now=_now,
        )
        assert not state_path.exists()
        # 依然只有一張 pending proposal（不因重跑而膨脹）。
        proposals = skill_janitor.list_proposals(proposals_dir)
        assert len(proposals) == 1
        assert proposals[0]["status"] == "pending"


class TestApplyParkAndRestore:
    def _seed_proposal(self, tmp_path: Path, *, skill_id: str = "a") -> tuple[Path, dict]:
        cards = {skill_id: _card(skill_id)}
        proposals_dir = tmp_path / "proposals"
        result = skill_janitor.propose_park(
            [skill_id], reason="stale", evidence_window_days=30, cards=cards, out_dir=proposals_dir,
            clock=lambda: NOW,
        )
        return proposals_dir, result["created"][0]

    def test_apply_park_requires_approved_by(self, tmp_path: Path) -> None:
        proposals_dir, proposal = self._seed_proposal(tmp_path)
        cards = {"a": _card("a")}
        with pytest.raises(ValueError):
            skill_janitor.apply_park(
                proposal["proposal_id"], approved_by="", cards=cards, proposals_dir=proposals_dir,
                state_path=tmp_path / "skill_park.json",
            )

    def test_apply_park_rejects_exempt_skill_even_if_proposal_predates_reclassification(self, tmp_path: Path) -> None:
        proposals_dir, proposal = self._seed_proposal(tmp_path)
        # skill 在 proposal 開立後被重新分類為 core——apply_park 必須用「現在」的
        # card_class 二次防呆，不得只信任 proposal 檔內容。
        cards = {"a": _card("a", card_class="core")}
        with pytest.raises(ValueError):
            skill_janitor.apply_park(
                proposal["proposal_id"], approved_by="operator", cards=cards, proposals_dir=proposals_dir,
                state_path=tmp_path / "skill_park.json",
            )

    def test_apply_park_rejects_non_pending_proposal(self, tmp_path: Path) -> None:
        proposals_dir, proposal = self._seed_proposal(tmp_path)
        cards = {"a": _card("a")}
        state_path = tmp_path / "skill_park.json"
        skill_janitor.apply_park(
            proposal["proposal_id"], approved_by="operator", cards=cards, proposals_dir=proposals_dir,
            state_path=state_path,
        )
        with pytest.raises(ValueError):
            skill_janitor.apply_park(
                proposal["proposal_id"], approved_by="operator", cards=cards, proposals_dir=proposals_dir,
                state_path=state_path,
            )

    def test_park_then_restore_is_reversible_and_does_not_touch_source_or_ledger(self, tmp_path: Path) -> None:
        # (4) park/restore 可逆性——比對操作前後來源 skill 檔案與 ledger 檔案的
        # 內容/hash 應相同。
        proposals_dir, proposal = self._seed_proposal(tmp_path)
        cards = {"a": _card("a")}
        state_path = tmp_path / "skill_park.json"

        skill_source = tmp_path / "skills" / "a" / "SKILL.md"
        skill_source.parent.mkdir(parents=True)
        skill_source.write_text("# skill a\n", encoding="utf-8")
        ledger_path = tmp_path / "skill_usage.jsonl"
        ledger_path.write_text('{"event_id": "x"}\n', encoding="utf-8")

        def _hash(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        source_hash_before = _hash(skill_source)
        ledger_hash_before = _hash(ledger_path)

        state = skill_janitor.apply_park(
            proposal["proposal_id"], approved_by="operator", cards=cards, proposals_dir=proposals_dir,
            state_path=state_path,
        )
        assert "a" in state["parked"]
        assert _hash(skill_source) == source_hash_before
        assert _hash(ledger_path) == ledger_hash_before

        restored = skill_janitor.restore("a", approved_by="operator", state_path=state_path)
        assert "a" not in restored["parked"]
        assert _hash(skill_source) == source_hash_before
        assert _hash(ledger_path) == ledger_hash_before
        # history 保留 park 與 restore 兩筆 audit trail。
        actions = [entry["action"] for entry in restored["history"]]
        assert actions == ["park", "restore"]


class TestManualPark:
    def test_manual_park_blocks_exempt_class(self, tmp_path: Path) -> None:
        cards = {"core-a": _card("core-a", card_class="core")}
        with pytest.raises(ValueError):
            skill_janitor.manual_park(
                "core-a", reason="manual", approved_by="operator", cards=cards, state_path=tmp_path / "skill_park.json"
            )

    def test_manual_park_requires_known_skill(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            skill_janitor.manual_park(
                "unknown", reason="manual", approved_by="operator", cards={}, state_path=tmp_path / "skill_park.json"
            )

    def test_manual_park_then_restore_round_trips(self, tmp_path: Path) -> None:
        cards = {"a": _card("a")}
        state_path = tmp_path / "skill_park.json"
        state = skill_janitor.manual_park(
            "a", reason="operator override", approved_by="operator", cards=cards, state_path=state_path
        )
        assert state["parked"]["a"]["approved_by"] == "operator"
        restored = skill_janitor.restore("a", approved_by="operator", state_path=state_path)
        assert restored["parked"] == {}


class TestRestore:
    def test_restore_requires_approved_by(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            skill_janitor.restore("a", approved_by="", state_path=tmp_path / "skill_park.json")

    def test_restore_unparked_skill_is_a_noop(self, tmp_path: Path) -> None:
        state_path = tmp_path / "skill_park.json"
        state = skill_janitor.restore("never-parked", approved_by="operator", state_path=state_path)
        assert state["parked"] == {}
        assert not state_path.exists()
