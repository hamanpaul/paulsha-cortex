"""#847 的 self-only planning drift 判準要接到 builder todo 准入。

live 缺陷：`quota-snapshot-window-refinement` 進件時 authority 只有
todo＋issue；planner 自產三份 planning 產物（plan／spec／design）後，
Monitor 把它們掃成新的 confirmed source，`authority_revision` 因此改變。
`_builder_todo_admission_stop` 原本以裸 digest 相等判定
`builder-todo-authority-changed`，把 run 自己造成的漂移也擋下來。

本檔驗證 `_builder_todo_admission_stop` 沿用
`claim.authority_matches_claim_era()`（#847 既有 helper）：只有「新增的
source 恰好是本 run 已接受的自產 planning 產物、且內容 sha256 與釘住的
baseline 相同」才視為未變動放行；其他任何差異（planning 內容漂移、混入
非本 run 的 source、todo 本身變動）仍維持既有 `builder-todo-authority-changed`
gate。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from paulsha_cortex.coordinator import manager
from paulsha_cortex.coordinator.claim import (
    claim_key_for_authority_digest,
    load_work_authority,
    work_authority_digest,
)
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import PlanningArtifactAuthority, WorkflowStep


_REPO = "acme/demo"
_WORK_ID = "quota-snapshot-window-refinement"
_TODO_REF = f"docs/superpowers/workstreams/{_WORK_ID}/todo.md"
_PLAN_REF = f"docs/superpowers/plans/{_WORK_ID}.md"
_SPEC_REF = f"docs/superpowers/specs/{_WORK_ID}-spec.md"
_DESIGN_REF = f"docs/superpowers/specs/{_WORK_ID}-design.md"

_ISSUE_SOURCE = f"github_issue:{_REPO}#12@identity:{_REPO}#12;state:open"
_TODO_SOURCE = f"todo:{_REPO}:{_TODO_REF}@identity:{_TODO_REF}"
_BASELINE_SOURCES = (_ISSUE_SOURCE, _TODO_SOURCE)

_PLAN_BYTES = b"# plan\n"
_SPEC_BYTES = b"# spec\n"
_DESIGN_BYTES = b"# design\n"


def _self_planning_source(ref: str) -> str:
    prefix = "superpowers_plan" if ref == _PLAN_REF else "superpowers_spec"
    return f"{prefix}:{_REPO}:{ref}@identity:{ref}"


class _RecordingCreator:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls: list[str] = []

    def create(self, branch: str, *, job_id: str | None = None, base_sha: str | None = None) -> str:
        self.calls.append(branch)
        return str(self.repo)


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        self.calls.append({"slice_id": slice_id, "worktree": worktree, "log_dir": log_dir})
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)


def _write_snapshot(path: Path, *, source_revisions: tuple[str, ...], provider_revision: str = "gh-1") -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": provider_revision,
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": _REPO,
                        "work_id": _WORK_ID,
                        "mapped_issues": [12],
                        "mapped_prs": [],
                        "mapped_openspec": [],
                        "mapped_todo_paths": [_TODO_REF],
                        "confirmed_todo": True,
                        "auto_label": True,
                        "source_revisions": list(source_revisions),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_artifact(root: Path, ref: str, content: bytes) -> str:
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _fixture(tmp_path: Path):
    """live 形狀：claim 時 authority 只有 todo＋issue，run 已接受三份自產 planning。"""

    repo_root = tmp_path / "repo"
    _init_repo(repo_root)

    baseline_snapshot = _write_snapshot(tmp_path / "baseline.json", source_revisions=_BASELINE_SOURCES)
    baseline_authority = load_work_authority(
        repo=_REPO, work_id=_WORK_ID, snapshot_path=baseline_snapshot
    )
    baseline_digest = work_authority_digest(baseline_authority)
    claim_key = claim_key_for_authority_digest(
        repo=_REPO, work_id=_WORK_ID, authority_digest=baseline_digest
    )

    plan_hash = _write_artifact(repo_root, _PLAN_REF, _PLAN_BYTES)
    spec_hash = _write_artifact(repo_root, _SPEC_REF, _SPEC_BYTES)
    design_hash = _write_artifact(repo_root, _DESIGN_REF, _DESIGN_BYTES)

    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    step = WorkflowStep(
        phase="build",
        persona="builder",
        card="tdd-red",
        executor="copilot",
        model="gpt",
        domain="openai",
        inputs=(),
        outputs=(),
        commit_policy="required",
        test_policy="red-required",
    )
    run = registry._manager_create_workflow_run(
        work_id=_WORK_ID,
        repo=_REPO,
        claim_key=claim_key,
        source_revision=baseline_digest,
        workspace_root=str(repo_root),
        combo="fix-standard",
        current_phase="build",
        steps=(step,),
        issue_refs=(f"{_REPO}#12",),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
        planning_authority=(
            PlanningArtifactAuthority(
                ref=_PLAN_REF, kind="plan", work_id=_WORK_ID, baseline_sha256=plan_hash
            ),
            PlanningArtifactAuthority(
                ref=_SPEC_REF, kind="spec", work_id=_WORK_ID, baseline_sha256=spec_hash
            ),
            PlanningArtifactAuthority(
                ref=_DESIGN_REF, kind="design", work_id=_WORK_ID, baseline_sha256=design_hash
            ),
        ),
    )
    creator = _RecordingCreator(repo_root)
    launcher = _RecordingLauncher()
    dispatcher = type(
        "Dispatcher",
        (),
        {"_registry": registry, "_worktree_creator": creator, "_git_runner": None},
    )()
    identities = IdentityRegistry.from_rows(
        [{
            "executor": "copilot",
            "model_id": "gpt",
            "independence_domain": "openai",
            "capabilities": ["build"],
        }]
    )
    return registry, run, dispatcher, identities, creator, launcher


def _dispatch_with_current_sources(
    tmp_path: Path, *, current_source_revisions: tuple[str, ...]
):
    registry, run, dispatcher, identities, creator, launcher = _fixture(tmp_path)
    current_snapshot = _write_snapshot(
        tmp_path / "current.json",
        source_revisions=current_source_revisions,
        provider_revision="gh-2",
    )
    current_authority = load_work_authority(
        repo=_REPO, work_id=_WORK_ID, snapshot_path=current_snapshot
    )
    admission = manager.BuilderTodoAdmission(
        authority_revision=work_authority_digest(current_authority),
        mapped_todo_paths=current_authority.mapped_todo_paths,
        error=None,
        authority=current_authority,
    )
    result = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
        builder_todo_admission=admission,
    )
    return registry, run, creator, launcher, result


def test_self_only_planning_drift_still_admits_builder_dispatch(tmp_path: Path) -> None:
    """live 形狀：多出的三份 self planning source 內容等於 baseline → 放行。"""

    registry, run, creator, launcher, result = _dispatch_with_current_sources(
        tmp_path,
        current_source_revisions=(
            *_BASELINE_SOURCES,
            _self_planning_source(_PLAN_REF),
            _self_planning_source(_SPEC_REF),
            _self_planning_source(_DESIGN_REF),
        ),
    )

    assert isinstance(result, dict) and result.get("job_id")
    assert len(registry.list_jobs()) == 1
    assert len(creator.calls) == 1
    assert len(launcher.calls) == 1
    stopped = registry.get_workflow_run(run.run_id)
    assert "needs_human" not in stopped.facets


def test_planning_content_drift_from_baseline_still_blocks(tmp_path: Path) -> None:
    """plan 檔內容與 run 已接受的 baseline sha256 不同 → 仍擋。"""

    registry, run, dispatcher, identities, creator, launcher = _fixture(tmp_path)
    # 模擬 planning 檔在 frozen baseline 之後又被改寫（bytes 已不等於
    # run.planning_authority 釘住的 baseline_sha256）。
    (Path(run.workspace_root) / _PLAN_REF).write_bytes(b"# plan changed after freeze\n")
    current_snapshot = _write_snapshot(
        tmp_path / "current.json",
        source_revisions=(
            *_BASELINE_SOURCES,
            _self_planning_source(_PLAN_REF),
            _self_planning_source(_SPEC_REF),
            _self_planning_source(_DESIGN_REF),
        ),
        provider_revision="gh-2",
    )
    current_authority = load_work_authority(
        repo=_REPO, work_id=_WORK_ID, snapshot_path=current_snapshot
    )
    admission = manager.BuilderTodoAdmission(
        authority_revision=work_authority_digest(current_authority),
        mapped_todo_paths=current_authority.mapped_todo_paths,
        error=None,
        authority=current_authority,
    )
    result = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=identities,
        launcher_factory=lambda _identity: launcher,
        coordinator_root=tmp_path / "coordinator",
        builder_todo_admission=admission,
    )

    assert result == {
        "run_id": run.run_id,
        "current_phase": "build",
        "reason": "builder-todo-authority-changed",
    }
    assert registry.list_jobs() == []
    assert creator.calls == []
    assert launcher.calls == []
    stopped = registry.get_workflow_run(run.run_id)
    assert "needs_human" in stopped.facets
    assert stopped.needs_human_reason["reason"] == "builder-todo-authority-changed"


def test_extra_foreign_source_still_blocks(tmp_path: Path) -> None:
    """混入非本 run 自產的 source（例如另一個 issue link）→ 仍擋。"""

    foreign_source = f"github_issue:{_REPO}#99@identity:{_REPO}#99;state:open"
    registry, run, creator, launcher, result = _dispatch_with_current_sources(
        tmp_path,
        current_source_revisions=(
            *_BASELINE_SOURCES,
            _self_planning_source(_PLAN_REF),
            _self_planning_source(_SPEC_REF),
            _self_planning_source(_DESIGN_REF),
            foreign_source,
        ),
    )

    assert result == {
        "run_id": run.run_id,
        "current_phase": "build",
        "reason": "builder-todo-authority-changed",
    }
    assert registry.list_jobs() == []
    assert creator.calls == []
    assert launcher.calls == []


def test_todo_drift_still_blocks(tmp_path: Path) -> None:
    """todo 本身的內容／識別變動（非本 run 自產）→ 仍擋。"""

    drifted_todo_source = f"todo:{_REPO}:{_TODO_REF}@identity:{_TODO_REF};content:v2"
    registry, run, creator, launcher, result = _dispatch_with_current_sources(
        tmp_path,
        current_source_revisions=(
            _ISSUE_SOURCE,
            drifted_todo_source,
            _self_planning_source(_PLAN_REF),
            _self_planning_source(_SPEC_REF),
            _self_planning_source(_DESIGN_REF),
        ),
    )

    assert result == {
        "run_id": run.run_id,
        "current_phase": "build",
        "reason": "builder-todo-authority-changed",
    }
    assert registry.list_jobs() == []
    assert creator.calls == []
    assert launcher.calls == []
