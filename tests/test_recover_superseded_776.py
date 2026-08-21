"""#776：resume 識別對 planning 自產 openspec 落地 authority 全盲的兩瓣修法。

實機現場：run（candidate=verified、PR 已建、review 全 passed）在 operator 把
planning 自產的 openspec change 回寫 authority checkout 後 resume，三層識別
全 miss → 走全新 claim → registry supersede 迴圈把已驗證 run 作廢。本檔釘：
(A) 穩定識別的 openspec 比對容忍「authority 多出的 refs 皆為 run 自產」；
(B) ``recover-superseded`` 把被誤作廢的 run 以 official authority-restart 撿回。
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from paulsha_cortex.coordinator import work_actions
from paulsha_cortex.coordinator.claim import claim_key_for_authority_digest
from paulsha_cortex.coordinator.registry import JobRegistry, WorkflowStep

_REPO = "o/r"
_WORK_ID = "fix-demo"
_DIGEST = "a" * 64


def _step(card: str, *, phase: str, gate_result: str, outputs: tuple[str, ...] = ()) -> WorkflowStep:
    return WorkflowStep(
        phase=phase,
        persona="builder" if phase == "build" else "planner",
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=outputs,
        commit_policy=None,
        test_policy=None,
        gate_result=gate_result,
    )


def _planning_run(*, openspec_refs=(), declared_change=None):
    outputs = (
        (f"openspec/changes/{declared_change}/tasks.md",) if declared_change else ()
    )
    return SimpleNamespace(
        openspec_refs=tuple(openspec_refs),
        steps=(
            _step("brainstorming", phase="define", gate_result="passed", outputs=outputs),
            _step("subagent-build", phase="build", gate_result="passed"),
        ),
    )


class OpenspecRefsCompatibleTests(unittest.TestCase):
    def test_equal_refs_are_compatible(self) -> None:
        authority = SimpleNamespace(mapped_openspec=("c1",))
        self.assertTrue(
            work_actions._openspec_refs_compatible(
                _planning_run(openspec_refs=("c1",)), authority
            )
        )
        empty = SimpleNamespace(mapped_openspec=())
        self.assertTrue(
            work_actions._openspec_refs_compatible(_planning_run(), empty)
        )

    def test_authority_gaining_run_declared_change_is_compatible(self) -> None:
        """run 自產 change 落地 authority → 同一份工作，識別不得斷。"""

        authority = SimpleNamespace(mapped_openspec=("c1",))
        run = _planning_run(openspec_refs=(), declared_change="c1")
        self.assertTrue(work_actions._openspec_refs_compatible(run, authority))

    def test_authority_gaining_foreign_change_is_incompatible(self) -> None:
        authority = SimpleNamespace(mapped_openspec=("other",))
        run = _planning_run(openspec_refs=(), declared_change="c1")
        self.assertFalse(work_actions._openspec_refs_compatible(run, authority))

    def test_authority_losing_run_ref_is_incompatible(self) -> None:
        authority = SimpleNamespace(mapped_openspec=())
        run = _planning_run(openspec_refs=("c1",), declared_change="c1")
        self.assertFalse(work_actions._openspec_refs_compatible(run, authority))

    def test_claim_lookup_uses_the_compatible_predicate(self) -> None:
        """source-pin：第二層穩定識別必須改走 _openspec_refs_compatible。"""

        source = inspect.getsource(work_actions._claim_action)
        self.assertIn("_openspec_refs_compatible(run, authority)", source)
        anchor = source.index("_openspec_refs_compatible(run, authority)")
        self.assertNotIn(
            "run.openspec_refs == authority.mapped_openspec",
            source[max(0, anchor - 800) : anchor],
        )


def _make_registry(tmp: Path) -> JobRegistry:
    return JobRegistry(state_path=tmp / "jobs.json")


def _create_run(registry: JobRegistry, *, phase="verify", candidate="c" * 40,
                prs=("o/r#1",), facets=()):
    return registry._manager_create_workflow_run(
        work_id=_WORK_ID,
        repo=_REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root="/tmp/ws",
        combo="feature-oneshot",
        current_phase=phase,
        steps=(
            _step("subagent-build", phase="build", gate_result="passed"),
            _step("verification", phase="verify", gate_result="passed"),
            _step("code-review", phase="review", gate_result="passed"),
        ),
        issue_refs=(f"{_REPO}#9",),
        openspec_refs=(),
        pr_refs=tuple(prs),
        attempts={"build": 1},
        candidate_head=candidate,
        verified_head=candidate,
        facets=tuple(facets),
        gate_status="running",
    )


def _args(run_id: str, **overrides):
    base = {
        "action": "recover-superseded",
        "repo": _REPO,
        "work_id": _WORK_ID,
        "actor": "operator",
        "expected_run_id": run_id,
        "reason": "recover verified run",
    }
    base.update(overrides)
    return base


_AUTHORITY = SimpleNamespace(repo=_REPO, work_id=_WORK_ID, mapped_openspec=())


class RecoverSupersededActionTests(unittest.TestCase):
    def _recover(self, registry, state, run_id, **overrides):
        with mock.patch.object(
            work_actions, "work_authority_digest", return_value=_DIGEST
        ):
            return work_actions._recover_superseded_action(
                args=_args(run_id, **overrides),
                authority=_AUTHORITY,
                state_path=state,
                workflow_registry=registry,
            )

    def test_recovers_run_via_official_authority_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root)
            run = _create_run(registry)
            registry._manager_update_workflow_run(
                run.run_id, status="superseded", facets=("blocked",)
            )
            result = self._recover(registry, root / "jobs.json", run.run_id)
            self.assertEqual(result["action"], "recovered-superseded")
            updated = registry.get_workflow_run(run.run_id)
            self.assertEqual(updated.status, "ongoing")
            self.assertEqual(updated.current_phase, "verify")
            self.assertEqual(
                updated.claim_key,
                claim_key_for_authority_digest(
                    repo=_REPO, work_id=_WORK_ID, authority_digest=_DIGEST
                ),
            )
            self.assertEqual(updated.source_revision, _DIGEST)
            self.assertIsNone(updated.verified_head)
            self.assertEqual(updated.candidate_head, "c" * 40)
            self.assertEqual(updated.pr_refs, ("o/r#1",))
            self.assertNotIn("blocked", updated.facets)
            by_phase = {step.phase: step.gate_result for step in updated.steps}
            self.assertEqual(by_phase["build"], "passed")
            self.assertEqual(by_phase["verify"], "pending")
            self.assertEqual(by_phase["review"], "pending")
            evidence = json.loads(
                Path(result["evidence"]["ref"]).read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["schema"], "cortex-work-recover-superseded/v1")
            self.assertEqual(evidence["run_id"], run.run_id)

    def test_rejects_non_superseded_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root)
            run = _create_run(registry)
            with self.assertRaisesRegex(RuntimeError, "superseded"):
                self._recover(registry, root / "jobs.json", run.run_id)

    def test_rejects_run_without_candidate_or_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root)
            run = _create_run(registry, candidate=None, prs=())
            registry._manager_update_workflow_run(run.run_id, status="superseded")
            with self.assertRaisesRegex(RuntimeError, "candidate"):
                self._recover(registry, root / "jobs.json", run.run_id)

    def test_rejects_while_another_run_is_ongoing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root)
            run = _create_run(registry)
            registry._manager_update_workflow_run(run.run_id, status="superseded")
            _create_run(registry)
            with self.assertRaisesRegex(RuntimeError, "ongoing"):
                self._recover(registry, root / "jobs.json", run.run_id)

    def test_rejects_pre_verify_phase_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root)
            run = _create_run(registry, phase="build")
            registry._manager_update_workflow_run(run.run_id, status="superseded")
            with self.assertRaisesRegex(RuntimeError, "verify/review"):
                self._recover(registry, root / "jobs.json", run.run_id)

    def test_rejects_malformed_cas_or_unbounded_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = _make_registry(root)
            with self.assertRaisesRegex(ValueError, "expected_run_id"):
                self._recover(registry, root / "jobs.json", "workflow-zzz")
            run = _create_run(registry)
            registry._manager_update_workflow_run(run.run_id, status="superseded")
            with self.assertRaisesRegex(ValueError, "reason"):
                self._recover(registry, root / "jobs.json", run.run_id, reason="  ")
            with self.assertRaisesRegex(ValueError, "caller evidence"):
                self._recover(
                    registry, root / "jobs.json", run.run_id, payload="x"
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class ControlContractWhitelistTests(unittest.TestCase):
    """#776 補遺：control contract 的 verb 白名單與 CAS/actor/reason 驗證分支。

    漏接 contract 白名單時 CLI submit 直接 `work-action action invalid`——
    verb 在 coordinator 端存在但 control 通道進不去。
    """

    def test_recover_superseded_is_a_valid_work_action(self) -> None:
        from paulsha_cortex.control import contract

        self.assertIn("recover-superseded", contract.WORK_ACTIONS)

    def test_contract_enforces_cas_actor_reason(self) -> None:
        from paulsha_cortex.control import contract

        base = {
            "schema_version": 1,
            "type": "work-action",
            "req_id": "r" * 20,
            "requested_by": "coordinator-cli",
            "created_at": contract.utcnow(),
            "args": {
                "action": "recover-superseded",
                "repo": _REPO,
                "work_id": _WORK_ID,
                "actor": "operator",
                "reason": "recover verified run",
                "expected_run_id": "workflow-" + "a" * 20,
            },
        }
        validated = contract.validate_request(dict(base))
        self.assertEqual(validated["args"]["action"], "recover-superseded")
        broken = dict(base)
        broken["args"] = {**base["args"], "expected_run_id": "workflow-zzz"}
        with self.assertRaisesRegex(ValueError, "expected_run_id"):
            contract.validate_request(broken)

class LegacyManifestOpenspecDeclarationTests(unittest.TestCase):
    """#776 補遺：舊版 combo manifest 的 openspec-propose 卡 outputs 未列
    openspec 路徑（實機 workflow-85114100 只列 spec/design）——有這張卡即視
    慣例名（= work_id）的 change 為 run 自產，識別不得再 miss。"""

    def _legacy_run(self, work_id="fix-demo"):
        propose = _step("openspec-propose", phase="define", gate_result="passed",
                        outputs=("docs/superpowers/specs/x-design.md",))
        return SimpleNamespace(
            work_id=work_id,
            openspec_refs=(),
            steps=(propose, _step("subagent-build", phase="build", gate_result="passed")),
        )

    def test_openspec_propose_card_declares_conventional_change_name(self) -> None:
        declared = work_actions._planning_declared_openspec_changes(self._legacy_run())
        self.assertIn("fix-demo", declared)

    def test_legacy_run_stays_compatible_when_its_change_lands(self) -> None:
        authority = SimpleNamespace(mapped_openspec=("fix-demo",))
        self.assertTrue(
            work_actions._openspec_refs_compatible(self._legacy_run(), authority)
        )

    def test_foreign_change_is_still_incompatible_for_legacy_run(self) -> None:
        authority = SimpleNamespace(mapped_openspec=("other-change",))
        self.assertFalse(
            work_actions._openspec_refs_compatible(self._legacy_run(), authority)
        )

    def test_run_without_openspec_propose_gets_no_conventional_grant(self) -> None:
        run = SimpleNamespace(
            work_id="fix-demo",
            openspec_refs=(),
            steps=(_step("brainstorming", phase="define", gate_result="passed"),),
        )
        self.assertNotIn(
            "fix-demo", work_actions._planning_declared_openspec_changes(run)
        )

class ShipAdapterRefsCompatTests(unittest.TestCase):
    """#776 補遺：ship adapter 的 refs 守衛改走相容判定。

    run.openspec_refs 是 claim 時快照、authority-restart 不回寫；authority 因
    run 自產 change 落地而多出 ref 時，全等比對把合法 ship 擋成
    `WorkflowRun refs differ`（實機 workflow-advance-failed）。helper 下沉
    claim.py 供 work_bridge 使用（不得反向 import work_actions）。
    """

    def test_ship_validate_uses_compatible_predicate(self) -> None:
        from paulsha_cortex.coordinator import work_bridge

        source = inspect.getsource(work_bridge)
        anchor = source.index("WorkflowRun refs differ from current WorkAuthority")
        window = source[max(0, anchor - 600) : anchor]
        self.assertIn("openspec_refs_compatible(run, authority)", window)
        self.assertNotIn("run.openspec_refs != authority.mapped_openspec", window)

    def test_helpers_live_in_claim_and_alias_in_work_actions(self) -> None:
        from paulsha_cortex.coordinator import claim

        self.assertIs(
            work_actions._openspec_refs_compatible, claim.openspec_refs_compatible
        )
        self.assertIs(
            work_actions._planning_declared_openspec_changes,
            claim.planning_declared_openspec_changes,
        )
