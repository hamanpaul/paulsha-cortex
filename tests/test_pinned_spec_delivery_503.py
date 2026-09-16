"""#503：slice-lane builder 必須拿到 pinned spec 的逐字內容，Cortex 自己交付並 attest。

舊 prompt 只有 `[TASK] <id>` 與 `[PLAN: path]`；spec body（含 controller 重釘後補進的
recovery 指示與必做回歸測試）在模型邊界被靜默丟掉，registry 卻顯示新 spec hash——稽核
只證明「選了哪個 hash」，不證明模型讀到了它。本檔鎖住：

- dispatch 逐字交付 spec body＋`[SPEC: path sha256=…]`，plan 沒寫的 recovery-only 要求出現在 prompt；
- job row 記錄交付的 spec／plan hash（attestation）；
- launch 時 spec 內容 hash 必須等於 pin 值，不等或不可讀就拒派（needs_human）；
- 只交 task id／plan 的 builder prompt 直接拒絕；
- 完成側：builder job 記錄的 hash 與 slice 釘住的不等 → pinned-input-mismatch，candidate 不得通過；
- reviewer prompt 也附同一份 spec 路徑與 hash。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from paulsha_cortex.coordinator import autonomy, manager, review, verification
from paulsha_cortex.coordinator.autonomy import DispatchReadyError, dispatch_ready
from paulsha_cortex.coordinator.contract_command import PINNED_SPEC_DIRECTIVE, build_dispatch_prompt
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.registry import JobRegistry

RECOVERY_ONLY = (
    "RECOVERY-ONLY REQUIREMENT (not in the plan): reproduce the out-of-tree unlink "
    "with output set to ../outside/ and add test_deliver_trim_refuses_outside_output."
)


def _fake_git_runner(args: list[str]):
    if args and args[0] == "rev-parse":
        return "f" * 40
    if len(args) >= 4 and args[0] == "-C" and args[2] == "rev-parse":
        return "f" * 40
    return ""


class _RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def as_commit_required(self):
        return self

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self.calls.append({"slice_id": slice_id, "prompt": prompt})
        return LaunchHandle(
            executor="copilot", model_id=None, session_name=slice_id, pid=100 + len(self.calls),
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)  # `_infer_repo_root` 只看 .git 存在
    return repo


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _meta(repo: Path, slice_id: str, *, spec_body: str, plan_body: str = "# plan\nBuild task 4.\n") -> dict:
    spec_path = repo / "specs" / f"{slice_id}.md"
    plan_rel = f"docs/superpowers/plans/{slice_id}.md"
    _write(spec_path, spec_body)
    _write(repo / plan_rel, plan_body)
    return {
        "path": str(spec_path),
        "dispatch": "auto",
        "slice_id": slice_id,
        "plan": plan_rel,
        "depends_on": [],
        "target_branch": "main",
        "verification": {"docs_class": "trivial", "review_policy": "not-required"},
        "parse_error": None,
    }


def _spec_text(extra: str = "") -> str:
    return f"---\ndispatch: auto\nslice_id: task-4\ntarget_branch: main\n---\n\n# Task 4\n\n{extra}\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = _repo(tmp_path)
    monkeypatch.setenv("PSC_REPO_ROOT", str(repo))
    return repo


def _dispatch(repo: Path, registry: JobRegistry, meta: dict, launcher: _RecordingLauncher):
    dispatcher = Dispatcher(registry, pane_sender=MagicMock(), worktree_creator=None)
    return dispatch_ready(
        [meta],
        is_satisfied=lambda _id: True,
        dispatcher=dispatcher,
        persona="builder",
        launcher=launcher,
        git_runner=_fake_git_runner,
        handoff_dir=str(repo / "handoff"),
    )


def test_recovery_only_requirement_in_spec_body_reaches_builder_prompt_verbatim(repo: Path, tmp_path: Path) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    meta = _meta(repo, "task-4", spec_body=_spec_text(RECOVERY_ONLY))
    launcher = _RecordingLauncher()

    jobs = _dispatch(repo, registry, meta, launcher)

    assert len(launcher.calls) == 1
    prompt = launcher.calls[0]["prompt"]
    spec_hash = hashlib.sha256((repo / "specs" / "task-4.md").read_bytes()).hexdigest()
    assert RECOVERY_ONLY in prompt
    assert f"[SPEC: {repo / 'specs' / 'task-4.md'} sha256={spec_hash}]" in prompt
    assert PINNED_SPEC_DIRECTIVE in prompt
    assert "[TASK] task-4" in prompt and "docs/superpowers/plans/task-4.md" in prompt
    # attestation：job row 記錄了實際交付的 spec／plan hash，且等於 slice 釘住的值。
    job = registry.get_job(jobs[0]["job_id"])
    slice_row = registry.get_slice("task-4")
    assert job["spec_hash"] == spec_hash == slice_row["spec"]["hash"]
    assert job["plan_hash"] == slice_row["plan"]["hash"]


def test_spec_changed_after_pinning_is_refused_at_launch(repo: Path, tmp_path: Path, monkeypatch) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    meta = _meta(repo, "task-4", spec_body=_spec_text("original"))
    launcher = _RecordingLauncher()
    real_pin = autonomy.pin_dispatch_inputs

    def pin_then_drift(m, **kwargs):
        pinned = real_pin(m, **kwargs)
        # pin 之後、交付之前 spec 被改寫（重釘未過 registry 的情境）。
        (repo / "specs" / "task-4.md").write_text(_spec_text("drifted"), encoding="utf-8")
        return pinned

    monkeypatch.setattr(autonomy, "pin_dispatch_inputs", pin_then_drift)

    with pytest.raises(DispatchReadyError) as info:
        _dispatch(repo, registry, meta, launcher)

    assert launcher.calls == []
    assert "#503" in str(info.value.errors[0][1]) and "changed between pinning and dispatch" in str(info.value.errors[0][1])
    assert registry.get_slice("task-4")["state"] == "needs_human"
    assert registry.list_jobs() == []


def test_unreadable_spec_is_refused_at_launch(repo: Path, tmp_path: Path, monkeypatch) -> None:
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    meta = _meta(repo, "task-4", spec_body=_spec_text("x"))
    launcher = _RecordingLauncher()
    real_pin = autonomy.pin_dispatch_inputs

    def pin_then_remove(m, **kwargs):
        pinned = real_pin(m, **kwargs)
        (repo / "specs" / "task-4.md").unlink()
        return pinned

    monkeypatch.setattr(autonomy, "pin_dispatch_inputs", pin_then_remove)
    with pytest.raises(DispatchReadyError):
        _dispatch(repo, registry, meta, launcher)
    assert launcher.calls == [] and registry.list_jobs() == []


def test_task_id_and_plan_only_builder_prompt_is_refused() -> None:
    with pytest.raises(ValueError, match="#503"):
        build_dispatch_prompt("builder", task="task-4", plan_path="docs/plan.md")
    body = "spec\n"
    with pytest.raises(ValueError, match="does not match spec_hash"):
        build_dispatch_prompt(
            "builder", task="task-4", plan_path="docs/plan.md",
            spec_path="/specs/task-4.md", spec_hash="0" * 64, spec_body=body,
        )


def test_non_builder_prompt_without_spec_is_unchanged() -> None:
    prompt = build_dispatch_prompt("reviewer", task="t", plan_path="p.md")
    assert prompt.endswith("[TASK] t\n[PLAN: p.md]\n請於本 worktree 內讀取上述 plan 並依 persona 契約邊界執行。")
    assert "[SPEC" not in prompt


def test_completion_rejects_candidate_whose_builder_saw_a_different_spec(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完成側：slice 釘住新 spec hash，但 builder job 記錄拿到的是舊 hash → pinned-input-mismatch。

    檔案漂移檢查（`_pinned_input_mismatches`）另有測試，這裡隔離掉只驗 attestation 這一條。
    """
    monkeypatch.setattr(manager, "_pinned_input_mismatches", lambda _slice: [])
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    spec_path = repo / "specs" / "task-4.md"
    spec_hash = _write(spec_path, _spec_text(RECOVERY_ONLY))
    plan_path = repo / "docs" / "superpowers" / "plans" / "task-4.md"
    plan_hash = _write(plan_path, "# plan\n")
    stale = registry.create_job(
        task="task-4", persona="builder", branch="feature/task-4", pane="",
        worktree=str(tmp_path / "wt"), spec_hash="0" * 64, plan_hash=plan_hash,
    )
    registry.update_headless_result(stale["job_id"], status="exited", exit_code=0)
    registry.create_slice(
        slice_id="task-4",
        spec_path=str(spec_path),
        spec_hash=spec_hash,
        plan_path="docs/superpowers/plans/task-4.md",
        plan_hash=plan_hash,
        target_branch="main",
        builder_job_id=stale["job_id"],
        reviewer_job_id=None,
        candidate=None,
        verification={"docs_class": "trivial", "review_policy": "not-required"},
    )
    slice_row = registry.get_slice("task-4")
    assert manager._builder_input_attestation_mismatches(slice_row, stale) == ["builder-input-spec-hash"]

    dispatcher = Dispatcher(registry, pane_sender=MagicMock(), worktree_creator=MagicMock())
    manager.complete_tick(dispatcher, handoff_dir=str(tmp_path / "handoff"))
    updated = registry.get_slice("task-4")
    assert updated["state"] == "needs_human"
    assert updated["gate_state"] == "needs_human"
    assert "verification-passed" not in [a["action"] for a in updated["actions"]]


def test_legacy_job_without_attestation_is_not_flagged() -> None:
    slice_row = {"spec": {"hash": "a" * 64}, "plan": {"hash": "b" * 64}}
    assert manager._builder_input_attestation_mismatches(slice_row, {"job_id": "legacy"}) == []
    assert manager._builder_input_attestation_mismatches(slice_row, None) == []
    good = {"spec_hash": "a" * 64, "plan_hash": "b" * 64}
    assert manager._builder_input_attestation_mismatches(slice_row, good) == []
    assert manager._builder_input_attestation_mismatches(slice_row, {"plan_hash": "c" * 64}) == ["builder-input-plan-hash"]


def test_review_prompt_carries_the_same_spec_authority() -> None:
    prompt = review.build_review_prompt(
        slice_id="task-4", plan_path="docs/plan.md", verdict_path="/spool/v.json",
        builder_job_id="b-1", reviewer_job_id="r-1", candidate="c" * 40,
        launch_identity={"executor": "agy", "model_id": "m", "independence_domain": "google"},
        spec_path="/repo/specs/task-4.md", spec_hash="d" * 64,
    )
    assert f"[SPEC: /repo/specs/task-4.md sha256={'d' * 64}]" in prompt
    without = review.build_review_prompt(
        slice_id="task-4", plan_path="docs/plan.md", verdict_path="/spool/v.json",
        builder_job_id="b-1", reviewer_job_id="r-1", candidate="c" * 40,
        launch_identity={"executor": "agy", "model_id": "m", "independence_domain": "google"},
    )
    assert "[SPEC" not in without
