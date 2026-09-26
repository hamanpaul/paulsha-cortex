from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import autonomy, claim, job_workspace, manager, work_actions
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.registry import JobRegistry


class RecoveryRegistry:
    def __init__(self, slices: list[dict], jobs: dict[str, dict] | None = None) -> None:
        self.slices = copy.deepcopy(slices)
        self.jobs = copy.deepcopy(jobs or {})
        self.actions: list[tuple[str, dict]] = []
        self._state_path = Path("jobs.json")

    def list_slices(self) -> list[dict]:
        return copy.deepcopy(self.slices)

    def list_slices_by_owner(self, *, repo: str, work_id: str) -> list[dict]:
        return [
            row
            for row in self.list_slices()
            if isinstance(row.get("owner_identity"), dict)
            and row["owner_identity"].get("repo") == repo
            and row["owner_identity"].get("work_id") == work_id
        ]

    def get_slice(self, slice_id: str) -> dict:
        for row in self.slices:
            if row["slice_id"] == slice_id:
                return copy.deepcopy(row)
        raise KeyError(slice_id)

    def get_job(self, job_id: str) -> dict:
        return copy.deepcopy(self.jobs[job_id])

    def record_action(self, slice_id: str, **kwargs) -> None:
        self.actions.append((slice_id, copy.deepcopy(kwargs)))
        for row in self.slices:
            if row["slice_id"] == slice_id:
                if kwargs.get("state") is not None:
                    row["state"] = kwargs["state"]
                if kwargs.get("gate_state") is not None:
                    row["gate_state"] = kwargs["gate_state"]
                if kwargs.get("clear_builder_binding"):
                    row["builder_job_id"] = None
                if kwargs.get("clear_candidate"):
                    row["candidate"] = None
                row.setdefault("actions", []).append(copy.deepcopy(kwargs))
                return

    def update_slice(self, slice_id: str, **kwargs) -> dict:
        for row in self.slices:
            if row["slice_id"] == slice_id:
                for key, value in kwargs.items():
                    if value is not None:
                        row[key] = value
                return copy.deepcopy(row)
        raise KeyError(slice_id)


def _owner(repo: str, work_id: str, slice_id: str) -> dict[str, str]:
    return {"repo": repo, "work_id": work_id, "slice_id": slice_id}


def _slice(slice_id: str, *, spec_path: str, owner: dict | None = None) -> dict:
    return {
        "slice_id": slice_id,
        "owner_identity": copy.deepcopy(owner),
        "attempt_id": f"attempt-{slice_id}",
        "spec": {"path": spec_path},
        "candidate": None,
        "state": "needs_human",
        "gate_state": "needs_human",
        "builder_job_id": f"job-{slice_id}",
        "branch": f"feature/{slice_id}",
        "actions": [],
    }


def _job(row: dict, worktree: Path) -> dict:
    return {
        "worktree": str(worktree),
        "owner_identity": copy.deepcopy(row.get("owner_identity")),
        "attempt_id": row.get("attempt_id"),
    }


def _recover_work(registry: RecoveryRegistry, *, state_path: Path, work_id: str = "work-42") -> dict:
    return work_actions._recover_pre_candidate_action(
        args={"action": "recover-pre-candidate"},
        authority=SimpleNamespace(
            repo="hamanpaul/project-a",
            work_id=work_id,
            mapped_issues=(),
        ),
        requested_by="operator",
        state_path=state_path,
        workflow_registry=registry,
    )


def test_work_recovery_uses_full_owner_identity_instead_of_spec_suffix(
    tmp_path: Path, monkeypatch
) -> None:
    foreign = _slice(
        "foreign-slice",
        spec_path="archive/work-42.md",
        owner=_owner("hamanpaul/project-b", "work-42", "foreign-slice"),
    )
    owned = _slice(
        "owned-slice",
        spec_path="specs/unrelated-name.md",
        owner=_owner("hamanpaul/project-a", "work-42", "owned-slice"),
    )
    registry = RecoveryRegistry(
        [foreign, owned],
        {
            "job-foreign-slice": {"worktree": str(tmp_path / "foreign")},
            "job-owned-slice": _job(owned, tmp_path / "owned"),
        },
    )
    registry.jobs["job-foreign-slice"] = _job(foreign, tmp_path / "foreign")
    reclaimed: list[str | None] = []

    def reclaim(**kwargs):
        reclaimed.append(kwargs.get("recorded_path"))
        return None

    monkeypatch.setattr(work_actions.worktree_reclaim, "reclaim_recorded_or_derived", reclaim)
    result = _recover_work(registry, state_path=tmp_path / "jobs.json")

    assert result["slice_id"] == "owned-slice"
    assert reclaimed == [str(tmp_path / "owned")]
    assert registry.actions[0][0] == "owned-slice"
    assert registry.get_slice("foreign-slice") == foreign


def test_work_recovery_rejects_legacy_unbound_row_without_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    legacy = _slice("legacy-slice", spec_path="specs/legacy.md")
    registry = RecoveryRegistry([legacy], {"job-legacy-slice": _job(legacy, tmp_path / "legacy")})
    before = copy.deepcopy(registry.slices)
    reclaimed: list[dict] = []
    monkeypatch.setattr(
        work_actions.worktree_reclaim,
        "reclaim_recorded_or_derived",
        lambda **kwargs: reclaimed.append(kwargs) or None,
    )

    with pytest.raises(RuntimeError, match="owner identity"):
        _recover_work(registry, state_path=tmp_path / "jobs.json")

    assert registry.slices == before
    assert registry.actions == []
    assert reclaimed == []


def test_work_recovery_rejects_ambiguous_owner_before_reclaim(tmp_path: Path, monkeypatch) -> None:
    owner = "hamanpaul/project-a"
    rows = [
        _slice("slice-a", spec_path="specs/a.md", owner=_owner(owner, "work-42", "slice-a")),
        _slice("slice-b", spec_path="specs/b.md", owner=_owner(owner, "work-42", "slice-b")),
    ]
    registry = RecoveryRegistry(
        rows,
        {
            "job-slice-a": _job(rows[0], tmp_path / "a"),
            "job-slice-b": _job(rows[1], tmp_path / "b"),
        },
    )
    reclaimed: list[dict] = []
    monkeypatch.setattr(
        work_actions.worktree_reclaim,
        "reclaim_recorded_or_derived",
        lambda **kwargs: reclaimed.append(kwargs) or None,
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        _recover_work(registry, state_path=tmp_path / "jobs.json")

    assert registry.actions == []
    assert reclaimed == []


def test_work_recovery_requires_explicit_owner_lookup_api(tmp_path: Path, monkeypatch) -> None:
    owner = _owner("hamanpaul/project-a", "work-42", "slice-a")
    row = _slice("slice-a", spec_path="specs/a.md", owner=owner)
    registry = RecoveryRegistry([row], {"job-slice-a": _job(row, tmp_path / "workspace")})
    registry.list_slices_by_owner = None
    reclaimed: list[dict] = []
    monkeypatch.setattr(
        work_actions.worktree_reclaim,
        "reclaim_recorded_or_derived",
        lambda **kwargs: reclaimed.append(kwargs) or None,
    )

    with pytest.raises(RuntimeError, match="owner lookup API unavailable"):
        _recover_work(registry, state_path=tmp_path / "jobs.json")

    assert registry.actions == []
    assert reclaimed == []


def test_work_recovery_rejects_workspace_marker_attempt_mismatch_before_reclaim(
    tmp_path: Path, monkeypatch
) -> None:
    owner = _owner("hamanpaul/project-a", "work-42", "slice-a")
    row = _slice("slice-a", spec_path="specs/other.md", owner=owner)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = RecoveryRegistry([row], {"job-slice-a": _job(row, workspace)})
    reclaimed: list[dict] = []
    monkeypatch.setattr(
        manager.job_workspace,
        "read_marker",
        lambda _path: {"owner_identity": owner, "attempt_id": "another-attempt"},
    )
    monkeypatch.setattr(
        manager.worktree_reclaim,
        "reclaim_recorded_or_derived",
        lambda **kwargs: reclaimed.append(kwargs) or None,
    )

    with pytest.raises(RuntimeError, match="workspace marker identity mismatch"):
        _recover_work(registry, state_path=tmp_path / "jobs.json")

    assert registry.actions == []
    assert reclaimed == []


def test_dispatch_registry_and_workspace_marker_share_owner_attempt_identity(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    dispatcher = SimpleNamespace(_registry=registry)
    owner = _owner("hamanpaul/project-a", "work-42", "slice-a")
    attempt_id = "attempt-fixed-before-workspace"

    autonomy._record_pending_slice(
        dispatcher=dispatcher,
        slice_id="slice-a",
        pinned_inputs={
            "spec_path": str(tmp_path / "spec.md"),
            "spec_hash": "spec-hash",
            "plan_path": "plans/work.md",
            "plan_hash": "plan-hash",
            "target_branch": "main",
            "target_remote": "origin",
            "verification_hash": "verification-hash",
            "verification": None,
        },
        dispatch_base=None,
        owner_identity=owner,
        attempt_id=attempt_id,
    )
    row = registry.get_slice("slice-a")
    assert row["owner_identity"] == owner
    assert row["attempt_id"] == attempt_id

    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    job_workspace.write_marker(
        workspace,
        branch="feature/slice-a",
        base="a" * 40,
        source_repo=tmp_path / "repo",
        owner_identity=owner,
        attempt_id=attempt_id,
    )
    marker = job_workspace.read_marker(workspace)
    assert marker is not None
    assert marker["owner_identity"] == row["owner_identity"]
    assert marker["attempt_id"] == row["attempt_id"]


def test_dispatch_ready_persists_owner_attempt_before_creating_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    owner = _owner("hamanpaul/project-a", "work-42", "slice-a")
    workspace = tmp_path / "workspace"
    created: list[tuple[dict, str]] = []
    authority_checks: list[tuple[str, str]] = []

    def load_authority(*, repo, work_id):
        authority_checks.append((repo, work_id))
        return SimpleNamespace(repo=repo, work_id=work_id)

    monkeypatch.setattr(claim, "load_work_authority", load_authority)

    class Creator:
        def create(self, branch, *, job_id, base_sha=None, owner_identity=None, attempt_id=None):
            row = registry.get_slice(job_id)
            assert row["owner_identity"] == owner_identity == owner
            assert row["attempt_id"] == attempt_id
            created.append((copy.deepcopy(owner_identity), attempt_id))
            (workspace / ".git").mkdir(parents=True)
            job_workspace.write_marker(
                workspace,
                branch=branch,
                base=base_sha or "a" * 40,
                source_repo=tmp_path / "repo",
                owner_identity=owner_identity,
                attempt_id=attempt_id,
            )
            return str(workspace)

    class Launcher:
        executor = "codex"
        model = "test-model"

        def launch(self, **_kwargs):
            return LaunchHandle(
                executor="codex",
                model_id="test-model",
                session_name="session",
                pid=123,
                log_path=str(tmp_path / "job.log"),
            )

    pinned = {
        "spec_path": str(tmp_path / "spec.md"),
        "spec_hash": "spec-hash",
        "plan_path": "plans/work.md",
        "plan_hash": "plan-hash",
        "target_branch": "main",
        "target_remote": "origin",
        "verification_hash": "verification-hash",
        "verification": None,
    }
    monkeypatch.setattr(autonomy, "pin_dispatch_inputs", lambda _meta: pinned)
    monkeypatch.setattr(autonomy, "_read_pinned_spec_body", lambda _pinned: "spec body")
    monkeypatch.setattr(autonomy, "_resolve_target_base_sha", lambda **_kwargs: "a" * 40)
    monkeypatch.setattr(autonomy, "build_dispatch_prompt", lambda *_args, **_kwargs: "prompt")
    dispatcher = Dispatcher(
        registry,
        pane_sender=SimpleNamespace(),
        worktree_creator=Creator(),
    )

    jobs = autonomy.dispatch_ready(
        [
            {
                "slice_id": "slice-a",
                "dispatch": "auto",
                "plan": "plans/work.md",
                "depends_on": [],
                "repo": owner["repo"],
                "work_id": owner["work_id"],
            }
        ],
        lambda _slice_id: True,
        dispatcher,
        launcher=Launcher(),
    )

    row = registry.get_slice("slice-a")
    job = registry.get_job(jobs[0]["job_id"])
    marker = job_workspace.read_marker(workspace)
    assert authority_checks == [(owner["repo"], owner["work_id"])]
    assert row["owner_identity"] == job["owner_identity"] == marker["owner_identity"] == owner
    assert row["attempt_id"] == job["attempt_id"] == marker["attempt_id"]
    assert created == [(owner, row["attempt_id"])]


def test_dispatch_ready_rejects_unconfirmed_owner_before_pinning_or_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    pin_calls: list[str] = []
    workspace_calls: list[str] = []

    def reject_authority(*, repo, work_id):
        raise ValueError(f"unconfirmed: {repo}/{work_id}")

    class Creator:
        def create(self, _branch, *, job_id, **_kwargs):
            workspace_calls.append(job_id)
            return str(tmp_path / "workspace")

    class Launcher:
        executor = "codex"
        model = "test-model"

        def launch(self, **_kwargs):
            raise AssertionError("must not launch")

    monkeypatch.setattr(claim, "load_work_authority", reject_authority)
    monkeypatch.setattr(
        autonomy,
        "pin_dispatch_inputs",
        lambda meta: pin_calls.append(meta["slice_id"]) or {},
    )
    dispatcher = Dispatcher(registry, pane_sender=SimpleNamespace(), worktree_creator=Creator())

    with pytest.raises(autonomy.DispatchReadyError, match="unconfirmed"):
        autonomy.dispatch_ready(
            [
                {
                    "slice_id": "slice-a",
                    "dispatch": "auto",
                    "plan": "plans/work.md",
                    "depends_on": [],
                    "repo": "hamanpaul/project-a",
                    "work_id": "work-42",
                }
            ],
            lambda _slice_id: True,
            dispatcher,
            launcher=Launcher(),
        )

    assert pin_calls == []
    assert workspace_calls == []
    assert registry.list_slices() == []


def test_slice_spec_owner_metadata_requires_repo_and_work_id_pair(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(autonomy, "_infer_repo_root", lambda _path: tmp_path)
    spec = tmp_path / "slice.md"
    spec.write_text(
        "---\ndispatch: hold\nslice_id: slice-a\nrepo: hamanpaul/project-a\n"
        "work_id: work-42\n---\n",
        encoding="utf-8",
    )
    metadata = autonomy.parse_spec_frontmatter(spec)
    assert metadata["repo"] == "hamanpaul/project-a"
    assert metadata["work_id"] == "work-42"
    assert metadata["parse_error"] is None

    spec.write_text(
        "---\ndispatch: hold\nslice_id: slice-a\nwork_id: work-42\n---\n",
        encoding="utf-8",
    )
    incomplete = autonomy.parse_spec_frontmatter(spec)
    assert incomplete["parse_error"]["field"] == "repo"


def test_recovery_clear_flags_preserve_update_slice_none_noop(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    builder = registry.create_job(
        task="slice-a",
        persona="builder",
        branch="feature/slice-a",
        pane="",
        worktree=str(tmp_path / "workspace"),
    )
    registry.create_slice(
        slice_id="slice-a",
        spec_path="specs/a.md",
        spec_hash="spec-hash",
        plan_path="plans/a.md",
        plan_hash="plan-hash",
        target_branch="main",
        builder_job_id=builder["job_id"],
        reviewer_job_id=None,
        candidate=None,
    )
    registry.update_slice("slice-a", builder_job_id=None, candidate=None)
    assert registry.get_slice("slice-a")["builder_job_id"] == builder["job_id"]

    registry.record_action(
        "slice-a",
        action="operator-recover-pre-candidate",
        actor="operator",
        state="pending",
        gate_state="pending",
        clear_builder_binding=True,
        clear_candidate=True,
        result="ok",
    )
    row = registry.get_slice("slice-a")
    assert row["builder_job_id"] is None
    assert row["candidate"] is None
    assert row["state"] == row["gate_state"] == "pending"


def test_dispatch_repin_does_not_migrate_legacy_unbound_slice(tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    registry.create_slice(
        slice_id="slice-a",
        spec_path="specs/a.md",
        spec_hash="old-spec-hash",
        plan_path="plans/a.md",
        plan_hash="old-plan-hash",
        target_branch="main",
        builder_job_id=None,
        reviewer_job_id=None,
        candidate=None,
    )
    before = registry.get_slice("slice-a")

    with pytest.raises(ValueError, match="cannot migrate legacy owner identity"):
        autonomy._record_pending_slice(
            dispatcher=SimpleNamespace(_registry=registry),
            slice_id="slice-a",
            pinned_inputs={
                "spec_path": "specs/new.md",
                "spec_hash": "new-spec-hash",
                "plan_path": "plans/new.md",
                "plan_hash": "new-plan-hash",
                "target_branch": "main",
                "target_remote": "origin",
                "verification_hash": "verification-hash",
                "verification": None,
            },
            dispatch_base=None,
            owner_identity=_owner("hamanpaul/project-a", "work-42", "slice-a"),
            attempt_id="next-attempt",
        )

    assert registry.get_slice("slice-a") == before


def test_manager_and_work_recovery_both_supersede_the_same_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    owner = _owner("hamanpaul/project-a", "work-42", "work-42")
    row = _slice("work-42", spec_path="specs/work-42.md", owner=owner)
    jobs = {"job-work-42": _job(row, tmp_path / "workspace")}
    reclaimed: list[dict] = []
    monkeypatch.setattr(
        manager.worktree_reclaim,
        "reclaim_recorded_or_derived",
        lambda **kwargs: reclaimed.append(kwargs) or None,
    )
    monkeypatch.setattr(manager.autonomy, "DEFAULT_HANDOFF_DIR", str(tmp_path / "work-handoff"))

    results = []
    for lane in ("manager", "work"):
        handoff_dir = tmp_path / f"{lane}-handoff"
        handoff_dir.mkdir()
        manifest_path = handoff_dir / "work-42.json"
        manifest_path.write_text(
            json.dumps({"slice_id": "work-42", "job_id": "job-work-42", "gate_status": "failed"}),
            encoding="utf-8",
        )
        registry = RecoveryRegistry([row], jobs)
        registry._state_path = tmp_path / lane / "jobs.json"
        if lane == "manager":
            result = manager.apply_slice_action(
                SimpleNamespace(_registry=registry, _git_runner=None),
                slice_id="work-42",
                action="recover-pre-candidate",
                actor="operator",
                specs_dir=str(tmp_path / "specs"),
                handoff_dir=str(handoff_dir),
            )
            replay = manager.apply_slice_action(
                SimpleNamespace(_registry=registry, _git_runner=None),
                slice_id="work-42",
                action="recover-pre-candidate",
                actor="operator",
                specs_dir=str(tmp_path / "specs"),
                handoff_dir=str(handoff_dir),
            )
            assert replay["reason"] == "already-recovered"
        else:
            monkeypatch.setattr(manager.autonomy, "DEFAULT_HANDOFF_DIR", str(handoff_dir))
            result = _recover_work(registry, state_path=tmp_path / lane / "jobs.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        results.append((registry.get_slice("work-42"), manifest, registry.actions))

    assert results[0][1]["superseded_reason"] == "operator-recover-pre-candidate"
    assert results[1][1]["superseded_reason"] == "operator-recover-pre-candidate"
    assert results[0][0]["state"] == results[1][0]["state"] == "pending"
    assert results[0][0]["builder_job_id"] is results[1][0]["builder_job_id"] is None
    assert len(results[0][2]) == len(results[1][2]) == 1
    assert len(reclaimed) == 2


def test_manager_recovery_does_not_report_success_when_manifest_readback_fails(
    tmp_path: Path, monkeypatch
) -> None:
    owner = _owner("hamanpaul/project-a", "work-42", "work-42")
    row = _slice("work-42", spec_path="specs/work-42.md", owner=owner)
    registry = RecoveryRegistry([row], {"job-work-42": _job(row, tmp_path / "workspace")})
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    (handoff_dir / "work-42.json").write_text(
        json.dumps({"slice_id": "work-42", "gate_status": "failed"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        manager.worktree_reclaim,
        "reclaim_recorded_or_derived",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(manager, "_supersede_handoff_manifest", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="manifest read-back mismatch"):
        manager.apply_slice_action(
            SimpleNamespace(_registry=registry, _git_runner=None),
            slice_id="work-42",
            action="recover-pre-candidate",
            actor="operator",
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(handoff_dir),
        )


@pytest.mark.parametrize(("candidate", "recoverable"), [("not-a-sha", True), ("a" * 40, False)])
def test_recovery_core_matches_action_list_candidate_validity(
    tmp_path: Path, monkeypatch, candidate: str, recoverable: bool
) -> None:
    """動作清單把非合法 SHA 殘值視同尚無 candidate；共用 core 必須用同一判準。"""
    owner = _owner("hamanpaul/project-a", "work-42", "work-42")
    row = {**_slice("work-42", spec_path="specs/work-42.md", owner=owner), "candidate": candidate}
    jobs = {"job-work-42": _job(row, tmp_path / "workspace")}
    monkeypatch.setattr(
        manager.worktree_reclaim, "reclaim_recorded_or_derived", lambda **kwargs: None
    )
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()
    registry = RecoveryRegistry([row], jobs)
    registry._state_path = tmp_path / "jobs.json"

    def run():
        return manager.apply_slice_action(
            SimpleNamespace(_registry=registry, _git_runner=None),
            slice_id="work-42",
            action="recover-pre-candidate",
            actor="operator",
            specs_dir=str(tmp_path / "specs"),
            handoff_dir=str(handoff_dir),
        )

    if recoverable:
        assert registry.get_slice("work-42")["state"] != "pending"
        run()
        assert registry.get_slice("work-42")["state"] == "pending"
    else:
        with pytest.raises(Exception, match="null candidate|action-not-allowed"):
            run()
