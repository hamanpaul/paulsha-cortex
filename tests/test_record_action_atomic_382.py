"""#382 回歸測試：record_action／update_slice 非原子突變髒寫，
以及 gate_state 落 failed 後永久不可 repin。

根因（見 issue #382 2026-08-10 獨立複驗留言）：
`record_action`／`update_slice` 逐欄位「驗證→立刻寫入活物件」，任一後段欄位
（例如 gate_state）驗證失敗 raise 時，前段已合法驗證的欄位（例如 state）已經
寫進 `self._slices` 的活物件——raise 發生在 `_persist()` 之前，所以這次不會
被沖上磁碟；但 `_reload_if_changed()` 只看檔案 mtime/size，記憶體裡的髒 row
不會被自動修正，於是下一次**任何無關**的 `_persist()`（例如另一個 slice／job
的合法變動）都會把這個半突變髒 row 一併沖上磁碟。

修復後：`record_action`／`update_slice` 改成兩階段——先驗證所有欄位，全部合法
才一次寫入活物件並 persist；任一欄位不合法時，活物件（記憶體與磁碟）完全不變。

同時修正：
- `GATE_STATE_TRANSITIONS["failed"]` 補上 `"pending"`，與
  `SLICE_STATE_TRANSITIONS["failed"]`（已允許 `failed -> pending`）對齊。
- `repin_slice()` 的 slice-state 閘門把 `"failed"` 併入可 repin 集合，讓
  `retry-build`（底層即 `repin_slice`）對 failed/failed slice 真的可執行，
  不再是保證失敗的宣告動作。
- `manager.allowed_slice_actions()` 改用 `registry.slice_repin_eligible()`
  （與 `repin_slice()` 共用同一張表／同一判準）決定要不要宣告 `retry-build`，
  不再只看 `state` 就無條件宣告。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.registry import (
    GATE_STATE_TRANSITIONS,
    SLICE_STATE_TRANSITIONS,
    JobRegistry,
    slice_repin_eligible,
)


def _create_pending_slice(reg: JobRegistry, slice_id: str = "slice-a") -> None:
    reg.create_slice(
        slice_id=slice_id,
        spec_path=f"specs/{slice_id}.md",
        spec_hash="spec-sha",
        plan_path=f"plans/{slice_id}.md",
        plan_hash="plan-sha",
        target_branch="main",
        builder_job_id=None,
        reviewer_job_id=None,
        candidate=None,
    )


def _read_disk_slice(state_path: Path, slice_id: str) -> dict:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    for row in payload["slices"]:
        if row["slice_id"] == slice_id:
            return row
    raise AssertionError(f"slice {slice_id!r} 不在磁碟快照中")


class TestRecordActionAtomicity:
    """record_action 對多欄位 transition 必須「全驗證、再全突變」。"""

    def test_rejected_multi_field_transition_leaves_live_object_untouched(
        self, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        _create_pending_slice(reg)

        # 兩個獨立、各自合法的欄位轉換，把 slice 帶到
        # state=needs_human / gate_state=passed（此後 gate_state 已不可能
        # 再合法回到 pending 以外的少數值——passed 是單向 sink）。
        reg.update_slice("slice-a", state="needs_human")
        reg.update_slice("slice-a", gate_state="passed")

        before = reg.get_slice("slice-a")
        assert before["state"] == "needs_human"
        assert before["gate_state"] == "passed"

        # state: needs_human -> pending 合法；gate_state: passed -> failed
        # 非法（GATE_STATE_TRANSITIONS["passed"] == {"passed"}）。
        with pytest.raises(ValueError, match="gate_state"):
            reg.record_action(
                "slice-a",
                action="operator-recover-pre-candidate",
                actor="operator",
                state="pending",
                gate_state="failed",
            )

        # 記憶體中的活物件必須完全不變——不能半突變。
        live = reg.get_slice("slice-a")
        assert live["state"] == "needs_human", "被拒的 transition 不應把 state 寫進活物件"
        assert live["gate_state"] == "passed"

        # 觸發一次完全無關的 persist（另一個 job 的合法建立）。
        reg.create_job(
            task="unrelated",
            persona="builder",
            branch="feature/unrelated",
            pane="",
            worktree="/wt/unrelated",
        )

        # 磁碟快照也不能被沖出髒值。
        on_disk = _read_disk_slice(state_path, "slice-a")
        assert on_disk["state"] == "needs_human", "半突變髒 row 外洩到磁碟（#382 根因）"
        assert on_disk["gate_state"] == "passed"

        # 從磁碟重新開一個全新 registry 實例，讀到的也必須是乾淨狀態。
        reloaded = JobRegistry(state_path=state_path)
        reloaded_slice = reloaded.get_slice("slice-a")
        assert reloaded_slice["state"] == "needs_human"
        assert reloaded_slice["gate_state"] == "passed"

    def test_accepted_multi_field_transition_still_applies_and_persists(
        self, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        _create_pending_slice(reg)

        result = reg.record_action(
            "slice-a",
            action="operator-abandon",
            actor="operator",
            state="failed",
            gate_state="failed",
            result="ok",
        )
        assert result["state"] == "failed"
        assert result["gate_state"] == "failed"

        on_disk = _read_disk_slice(state_path, "slice-a")
        assert on_disk["state"] == "failed"
        assert on_disk["gate_state"] == "failed"
        assert on_disk["actions"][-1]["action"] == "operator-abandon"


class TestUpdateSliceAtomicity:
    """update_slice 同一族缺陷、同一種修法。"""

    def test_rejected_multi_field_transition_leaves_live_object_untouched(
        self, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        _create_pending_slice(reg)

        reg.update_slice("slice-a", state="needs_human")
        reg.update_slice("slice-a", gate_state="passed")

        with pytest.raises(ValueError, match="gate_state"):
            reg.update_slice("slice-a", state="pending", gate_state="failed")

        live = reg.get_slice("slice-a")
        assert live["state"] == "needs_human"
        assert live["gate_state"] == "passed"

        reg.create_job(
            task="unrelated-2",
            persona="builder",
            branch="feature/unrelated-2",
            pane="",
            worktree="/wt/unrelated-2",
        )

        on_disk = _read_disk_slice(state_path, "slice-a")
        assert on_disk["state"] == "needs_human"
        assert on_disk["gate_state"] == "passed"


class TestGateStateTableAlignment:
    """gate_state 轉換表補 failed 的合法離開路徑，與 slice state 表對齊。"""

    def test_gate_state_failed_allows_pending_matching_slice_state_table(self) -> None:
        assert "pending" in SLICE_STATE_TRANSITIONS["failed"]
        assert "pending" in GATE_STATE_TRANSITIONS["failed"], (
            "gate_state 的 failed 應與 slice state 一樣允許回到 pending"
            "（#382：兩表不對稱是死鎖根因之一）"
        )


class TestRepinSliceAcceptsFailedState:
    """retry-build 底層即 repin_slice；failed/failed slice 必須能一步復原。"""

    def test_repin_slice_recovers_failed_failed_slice_in_one_call(
        self, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        _create_pending_slice(reg)
        reg.update_slice("slice-a", state="running")
        reg.update_slice("slice-a", state="failed", gate_state="failed")

        before = reg.get_slice("slice-a")
        assert before["state"] == "failed"
        assert before["gate_state"] == "failed"

        repinned = reg.repin_slice(
            "slice-a",
            spec_path="specs/slice-a.md",
            spec_hash="spec-sha-2",
            plan_path="plans/slice-a.md",
            plan_hash="plan-sha-2",
            target_branch="feature/slice-a",
            target_remote="origin",
            verification_hash="verification-sha-2",
            verification={"docs_class": "code"},
            dispatch_base="base-sha-2",
        )

        # repin_slice() 刻意不動 slice state（既有行為，見
        # test_repin_slice_preserves_needs_human_until_explicit_retry_transition
        # ——needs_human 也是保留原值，直到派工路徑的下一步才真正推進）；
        # 只重置 gate_state，讓下一步（_mark_slice_building）能接手。
        assert repinned["state"] == "failed"
        assert repinned["gate_state"] == "pending"

        # 完整證明 retry-build 這條路徑走得完，不是只有 repin_slice 這一半：
        # autonomy.dispatch_ready 在 repin 成功後緊接著會呼叫
        # _mark_slice_building()，把 state 從 repin 前的原值直接轉成
        # "building"。這一步在修復前會 raise（SLICE_STATE_TRANSITIONS["failed"]
        # 原本不含 "building"），讓 retry-build 即使闖過 repin 也在下一步死掉。
        from paulsha_cortex.coordinator import autonomy
        from paulsha_cortex.coordinator.dispatcher import Dispatcher

        dispatcher = Dispatcher(reg, pane_sender=None, worktree_creator=None)
        autonomy._mark_slice_building(
            dispatcher=dispatcher,
            slice_id="slice-a",
            builder_job_id=None,
            dispatch_base="base-sha-2",
        )
        after_building = reg.get_slice("slice-a")
        assert after_building["state"] == "building"

    def test_repin_slice_still_rejects_active_running_state(self, tmp_path: Path) -> None:
        """安全底線：正在跑的 builder job 不能被 repin 蓋過去。"""
        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        _create_pending_slice(reg)
        reg.update_slice("slice-a", state="running")

        with pytest.raises(ValueError, match="slice state"):
            reg.repin_slice(
                "slice-a",
                spec_path="specs/slice-a.md",
                spec_hash="spec-sha-2",
                plan_path="plans/slice-a.md",
                plan_hash="plan-sha-2",
                target_branch="feature/slice-a",
                target_remote="origin",
                verification_hash="verification-sha-2",
                verification={"docs_class": "code"},
                dispatch_base="base-sha-2",
            )


class TestAllowedSliceActionsMatchesMutationGate:
    """allowed_slice_actions 宣告的 retry-build 必須與 repin_slice 實際會接受
    的組合一致（共用 slice_repin_eligible 判準），不能只看 state。"""

    def test_failed_failed_slice_declares_retry_build_and_it_actually_works(
        self, tmp_path: Path
    ) -> None:
        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        _create_pending_slice(reg)
        reg.update_slice("slice-a", state="running")
        reg.update_slice("slice-a", state="failed", gate_state="failed")

        slice_row = reg.get_slice("slice-a")
        actions = manager.allowed_slice_actions(reg, slice_row)
        assert "retry-build" in actions
        assert slice_repin_eligible(slice_row) is True

        # 宣告的動作必須真的能執行，不能是保證失敗的宣告（#382 核心投訴）。
        repinned = reg.repin_slice(
            "slice-a",
            spec_path="specs/slice-a.md",
            spec_hash="spec-sha-2",
            plan_path="plans/slice-a.md",
            plan_hash="plan-sha-2",
            target_branch="feature/slice-a",
            target_remote="origin",
            verification_hash="verification-sha-2",
            verification={"docs_class": "code"},
            dispatch_base="base-sha-2",
        )
        assert repinned["gate_state"] == "pending"

    def test_failed_state_with_non_repin_eligible_gate_does_not_declare_retry_build(
        self, tmp_path: Path
    ) -> None:
        """防禦性案例：即使 gate_state 落在一個 repin 不接受的值（例如非典型的
        passed——目前正常流程不會產生，但 registry API 本身不禁止），
        allowed_slice_actions 也不能宣告一個保證失敗的 retry-build。"""
        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        _create_pending_slice(reg)
        reg.update_slice("slice-a", gate_state="passed")
        reg.update_slice("slice-a", state="failed")

        slice_row = reg.get_slice("slice-a")
        assert slice_row["gate_state"] == "passed"
        assert slice_repin_eligible(slice_row) is False

        actions = manager.allowed_slice_actions(reg, slice_row)
        assert "retry-build" not in actions
        assert "abandon" in actions


class TestRecoverPreCandidateOnFreshFailure:
    """recover-pre-candidate 對剛失敗（未被舊 bug 弄髒過）的 failed/failed slice
    必須乾淨地一次到位（state/gate_state 同步回 pending），不留半復原中間態。"""

    def test_recover_pre_candidate_cleanly_resets_failed_failed_slice(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import subprocess
        from unittest.mock import MagicMock

        from paulsha_cortex.coordinator.dispatcher import Dispatcher

        # #612：`recover-pre-candidate` 會走 `worktree_reclaim`，其預設 git runner
        # 打的是 `paths.repo_root()`。舊實作未宣告 `PSC_REPO_ROOT` 時退回
        # `Path.cwd()`＝ operator 的真實 checkout，因此這個測試以前是在**真 repo**
        # 上跑 `git worktree list --porcelain`。改成自備一個空 repo 並顯式宣告。
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        monkeypatch.setenv("PSC_REPO_ROOT", str(repo))

        state_path = tmp_path / "jobs.json"
        reg = JobRegistry(state_path=state_path)
        builder_job = reg.create_job(
            task="slice-a",
            persona="builder",
            branch="feature/slice-a",
            pane="",
            worktree=str(tmp_path / "wt" / "feature-slice-a"),
        )
        reg.create_slice(
            slice_id="slice-a",
            spec_path="specs/slice-a.md",
            spec_hash="spec-sha",
            plan_path="plans/slice-a.md",
            plan_hash="plan-sha",
            target_branch="main",
            builder_job_id=builder_job["job_id"],
            reviewer_job_id=None,
            candidate=None,
        )
        reg.update_slice("slice-a", state="failed", gate_state="failed")

        dispatcher = Dispatcher(reg, pane_sender=MagicMock(), worktree_creator=MagicMock())
        result = manager.apply_slice_action(
            dispatcher=dispatcher,
            slice_id="slice-a",
            action="recover-pre-candidate",
            actor="test-operator",
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(tmp_path / "handoff"),
        )

        assert result["result"] == "ok"
        latest = reg.get_slice("slice-a")
        assert latest["state"] == "pending"
        assert latest["gate_state"] == "pending"

        # state=pending 不是操作者 next_actions 的死路：allowed_slice_actions
        # 對 pending 回 []（不需要人工介入）是設計如此——pending 由自動 tick／
        # ready_units + dispatch_ready 撿起重派，不需要顯式 operator action。
        # 這裡驗證的是「不再卡在 state=pending / gate_state=failed 的無出口
        # 中間態」，而不是 next_actions 本身要非空。
        actions = manager.allowed_slice_actions(reg, latest)
        assert actions == []
        assert slice_repin_eligible(latest) is True
