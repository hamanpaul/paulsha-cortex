"""issue #1261：三 UID 下 workflow lane 的 builder 工作區要能經 builder unit 回收。

#1167 的 owner-bound reclaim 只在工作區 marker 帶 `owner_identity`／`attempt_id`
時才走 builder unit。slice lane 派工有帶，workflow lane（`manager._dispatch_workflow_card`
→ `creator.create()`）沒有帶，於是會寫檔的 build 卡被採信之後，回收落回 Manager 身分的
dirty scan；三 UID 下 Manager 對 builder 建立的 inode 沒有 ACL，回收一律 fail closed。

本檔釘住：

1. 會寫檔的 workflow build 卡派工時，marker 與 job 記錄帶同一組 owner identity／attempt。
2. 唯讀卡（`BUILDER_WRITE_FORBIDDEN`，跑 `cortex-job-ro`）不帶，維持 Manager 直接回收。
3. trusted-build reclaim 對會寫檔的卡走 builder unit，Manager 不自己做 dirty scan。
4. marker 被竄改、身分不符、unit 失敗、完成紀錄無效時 fail closed，不刪任何東西。
"""

from __future__ import annotations

import contextlib
import grp
import json
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from paulsha_cortex.coordinator import (
    executor_auth,
    job_runner,
    job_workspace,
    manager,
    owner_reclaim,
    seams,
    spool_slot,
    terminal_contract,
    worktree_reclaim,
)
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import WorkflowStep


_REPO = "hamanpaul/paulsha-cortex"
_WORK_ID = "1261-workflow-owner-reclaim"


# ---------------------------------------------------------------------------
# fixtures：真 git repo、真 `ScriptWorktreeCreator`、真 workflow 派工
# （形狀沿用 tests/test_immediate_worktree_reclaim_658.py）
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    _git(root, "config", "user.email", "manager@example.invalid")
    _git(root, "config", "user.name", "Cortex Manager")
    (root / "README.md").write_text("source\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root


def _step(card: str, *, read_only: bool = False) -> WorkflowStep:
    return WorkflowStep(
        phase="build",
        persona="builder",
        card=card,
        executor=None,
        model=None,
        domain=None,
        inputs=(),
        outputs=(),
        # 唯讀卡的契約判準（#716）：`commit_policy=forbidden` 且 `declared_outputs` 為空。
        commit_policy="forbidden" if read_only else "required",
        test_policy="none",
        gate_result="pending",
    )


def _identities() -> IdentityRegistry:
    return IdentityRegistry.from_rows(
        [{
            "executor": "copilot",
            "model_id": "gpt",
            "independence_domain": "openai",
            "capabilities": ["build"],
        }]
    )


class _Launcher:
    """記錄派工的 launcher；`_write_forbidden` 對應真 launcher 選 `cortex-job-ro` 的旗標。"""

    def __init__(self, *, write_forbidden: bool = False) -> None:
        self._write_forbidden = write_forbidden

    def as_commit_required(self) -> "_Launcher":
        return self

    def as_write_forbidden(self) -> "_Launcher":
        return _Launcher(write_forbidden=True)

    def launch(self, *, slice_id: str, prompt: str, worktree: str, log_dir: str) -> LaunchHandle:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        return LaunchHandle(
            executor="copilot",
            model_id="gpt",
            session_name=slice_id,
            pid=100,
            log_path=f"{log_dir}/{slice_id}.jsonl",
        )


def _run(registry: JobRegistry, *, workspace: Path, steps: tuple[WorkflowStep, ...]) -> Any:
    return registry._manager_create_workflow_run(
        work_id=_WORK_ID,
        repo=_REPO,
        claim_key="claim:v1:" + "1" * 64,
        source_revision="2" * 64,
        workspace_root=str(workspace),
        combo="feature-oneshot",
        current_phase="build",
        steps=steps,
        issue_refs=(f"{_REPO}#1261",),
        openspec_refs=(),
        pr_refs=(),
        attempts={"build": 1},
        gate_status="running",
    )


def _dispatch(
    registry: JobRegistry,
    *,
    run,
    workspace: Path,
    pool: Path,
    coordinator_root: Path,
    creator=None,
) -> dict[str, object]:
    creator = creator or seams.ScriptWorktreeCreator(repo=workspace, wt_root=pool, base="main")
    dispatcher = type(
        "D",
        (),
        {"_registry": registry, "_worktree_creator": creator, "_git_runner": None},
    )()
    dispatched = manager.dispatch_workflow_card(
        dispatcher,
        run=run,
        identities=_identities(),
        launcher_factory=lambda _identity: _Launcher(),
        coordinator_root=coordinator_root,
    )
    assert dispatched is not None
    return registry.get_job(str(dispatched["job_id"]))


def _harvest(*, workspace: Path, clone: Path, branch: str, spool_key: str, coordinator_root: Path) -> None:
    bundle = job_workspace.prepare_commit_spool(
        spool_key=spool_key, coordinator_root=coordinator_root
    )
    command = job_workspace.build_bundle_command(workspace=clone, bundle=bundle)
    produced = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert produced.returncode == 0, produced.stderr
    job_workspace.harvest_branch(source_repo=workspace, bundle=bundle, branch=branch)


def _finish_card(
    registry: JobRegistry, *, run, job: dict[str, object], workspace: Path, coordinator_root: Path
) -> tuple[Any, Path]:
    """跑完一張 build 卡的正式採信路徑；即時回收掛在 `apply_workflow_action` 裡。"""

    clone = Path(str(job["worktree"]))
    (clone / "one.txt").write_text("candidate\n", encoding="utf-8")
    _git(clone, "add", "one.txt")
    _git(clone, "commit", "-qm", "add one.txt")
    candidate = _git(clone, "rev-parse", "HEAD").lower()
    _harvest(
        workspace=workspace,
        clone=clone,
        branch=str(job["branch"]),
        spool_key=str(job["job_id"]),
        coordinator_root=coordinator_root,
    )
    log = Path(str(job["log_path"]))
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "workflow-card",
            "status": "passed",
            "run_id": run.run_id,
            "card_id": job["workflow_card"],
            "candidate": candidate,
            "outputs": [],
        }) + "\n",
        encoding="utf-8",
    )
    ledger = terminal_contract.gate_ledger_path(log)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({
            "schema_version": terminal_contract.GATE_LEDGER_SCHEMA_VERSION,
            "kind": "workflow-gate-ledger",
            "slice_id": log.stem,
            "gates": [],
        }),
        encoding="utf-8",
    )
    registry.update_headless_result(str(job["job_id"]), status="exited", exit_code=0)
    terminal = manager.terminalize_workflow_job(
        registry, job_id=str(job["job_id"]), coordinator_root=coordinator_root
    )
    manager.apply_workflow_action(
        registry,
        args={
            "action": "advance",
            "run_id": run.run_id,
            "card_id": str(job["workflow_card"]),
            "job_id": terminal["job_id"],
            "current_phase": "build",
        },
        identity_registry=_identities(),
        coordinator_root=coordinator_root,
        trusted_terminal=True,
    )
    return registry.get_workflow_run(run.run_id), clone


def _expected_owner(job: dict[str, object]) -> dict[str, str]:
    return {"repo": _REPO, "work_id": _WORK_ID, "slice_id": str(job["task"])}


@pytest.fixture(autouse=True)
def _no_copilot_cli_probe(monkeypatch) -> None:
    """派工時的 Copilot 模型可用性探測會真的呼叫本機 CLI；固定成 CI 上的 unknown。"""

    monkeypatch.setattr(
        executor_auth,
        "probe_copilot_model_availability",
        lambda *_args, **_kwargs: ("unknown", "test stub"),
    )


@pytest.fixture
def lane(tmp_path: Path) -> dict[str, Any]:
    workspace = _source_repo(tmp_path / "source")
    pool = tmp_path / "pool"
    coordinator_root = tmp_path / "coordinator"
    registry = JobRegistry(state_path=tmp_path / "jobs.json")
    return {
        "workspace": workspace,
        "pool": pool,
        "coordinator_root": coordinator_root,
        "registry": registry,
    }


def _dispatch_card(lane: dict[str, Any], *, read_only: bool = False, card: str = "tdd-red"):
    run = _run(lane["registry"], workspace=lane["workspace"], steps=(_step(card, read_only=read_only),))
    job = _dispatch(
        lane["registry"],
        run=run,
        workspace=lane["workspace"],
        pool=lane["pool"],
        coordinator_root=lane["coordinator_root"],
    )
    return run, job


@pytest.fixture
def three_uid_reclaim(monkeypatch, lane):
    """三 UID 回收前提：`systemd-template` runner、pool 即 `PSC_WORKTREE_ROOT`。

    builder unit 以 seam 模擬（`owner_reclaim.reclaim_through_builder_unit`）；Manager
    身分的 dirty scan 一旦被呼叫就記下來——會寫檔的卡不得走到那裡。
    """

    unit_calls: list[dict[str, object]] = []
    manager_scans: list[str] = []
    behaviour: dict[str, Any] = {"result": "clear"}

    def builder_unit(*, workspace, marker):
        target = Path(workspace)
        unit_calls.append({"workspace": str(target), "marker": dict(marker)})
        mode = behaviour["result"]
        if isinstance(mode, Exception):
            raise mode
        if mode == "clear":
            # helper 的形狀：掃描、保存之後清空工作區內容，目錄項留給 Manager 刪。
            for child in list(target.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            return {
                "schema_version": 1,
                "status": "cleared",
                "workspace_name": target.name,
                "preserved_files": 0,
                "preserved_commit": False,
                "preserve_path": None,
            }
        return dict(mode)

    original_scan = worktree_reclaim._dirty_entries

    def manager_scan(runner, target):
        manager_scans.append(str(target))
        return original_scan(runner, target)

    def activate() -> None:
        # 派工之後才切換：派工本身不依賴 runner 模式，回收才看。
        monkeypatch.setenv("PSC_JOB_RUNNER", "systemd-template")
        monkeypatch.setenv("PSC_WORKTREE_ROOT", str(lane["pool"]))
        monkeypatch.setattr(owner_reclaim, "reclaim_through_builder_unit", builder_unit)
        monkeypatch.setattr(worktree_reclaim, "_dirty_entries", manager_scan)

    return {
        "unit_calls": unit_calls,
        "manager_scans": manager_scans,
        "behaviour": behaviour,
        "activate": activate,
    }


def _tree(path: Path) -> dict[str, bytes]:
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


# ---------------------------------------------------------------------------
# 1／2：派工時 marker 與 job 記錄帶 owner identity；唯讀卡不帶
# ---------------------------------------------------------------------------


def test_workflow_write_card_marker_and_job_carry_the_same_owner_identity(lane) -> None:
    _run_row, job = _dispatch_card(lane)
    clone = Path(str(job["worktree"]))
    marker = job_workspace.read_marker(clone)

    assert isinstance(marker, dict)
    # 形狀比照 slice lane：{repo, work_id, slice_id}；workflow lane 的 slice_id 是 job
    # task（`wf-<run 雜湊>-<card>`），因此身分同時綁住 run 與 card。
    assert marker["owner_identity"] == _expected_owner(job)
    assert job["owner_identity"] == marker["owner_identity"]
    assert re.fullmatch(r"[0-9a-f]{32}", str(marker["attempt_id"]))
    assert job["attempt_id"] == marker["attempt_id"]


def test_each_workflow_build_job_gets_its_own_attempt(lane) -> None:
    """同一個 run 的下一張 build 卡另有自己的身分與 attempt；前一張照常被採信、回收。"""

    registry = lane["registry"]
    run = _run(
        registry,
        workspace=lane["workspace"],
        steps=(_step("tdd-red"), _step("subagent-build")),
    )
    first = _dispatch(
        registry, run=run, workspace=lane["workspace"], pool=lane["pool"],
        coordinator_root=lane["coordinator_root"],
    )
    run, first_clone = _finish_card(
        registry, run=run, job=first,
        workspace=lane["workspace"], coordinator_root=lane["coordinator_root"],
    )
    assert not first_clone.exists()
    second = _dispatch(
        registry, run=run, workspace=lane["workspace"], pool=lane["pool"],
        coordinator_root=lane["coordinator_root"],
    )

    assert second["workflow_card"] == "subagent-build"
    assert second["owner_identity"] == _expected_owner(second)
    assert first["attempt_id"] != second["attempt_id"]
    assert first["owner_identity"]["slice_id"] != second["owner_identity"]["slice_id"]
    marker = job_workspace.read_marker(Path(str(second["worktree"])))
    assert marker["attempt_id"] == second["attempt_id"]
    assert marker["owner_identity"] == second["owner_identity"]


def test_read_only_card_keeps_the_manager_reclaim_path(lane, three_uid_reclaim) -> None:
    """唯讀卡跑 `cortex-job-ro`：工作區掛成 ReadOnlyPaths，helper 在那裡清不掉東西。

    它的工作區也沒有 builder 擁有的 inode（外層掛載唯讀），Manager 自己就收得掉
    （#716 canary 實測 worktree-isolation 卡回收成功），因此不帶 owner identity。
    """

    run, job = _dispatch_card(lane, read_only=True, card="worktree-isolation")
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    marker = job_workspace.read_marker(clone)

    assert "owner_identity" not in marker and "attempt_id" not in marker
    assert job["owner_identity"] is None and job["attempt_id"] is None

    candidate = str(marker["base"])
    result = manager._reclaim_trusted_build_workspace(job, run=run, candidate=candidate)

    assert result is not None and result.ok
    assert not clone.exists()
    assert three_uid_reclaim["unit_calls"] == []
    assert three_uid_reclaim["manager_scans"] == [str(clone)]


def test_workspace_creator_without_identity_support_fails_closed_before_provisioning(lane) -> None:
    """建立工作區的 seam 收不下 owner identity ⇒ 不派工，不是默默派一個回收不掉的工作區。"""

    class _LegacyCreator:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, branch: str, *, job_id: str, base_sha: str | None = None) -> str:
            self.calls += 1
            raise AssertionError("legacy creator must not be asked to provision")

    run = _run(lane["registry"], workspace=lane["workspace"], steps=(_step("tdd-red"),))
    creator = _LegacyCreator()
    with pytest.raises((TypeError, ValueError)):
        _dispatch(
            lane["registry"],
            run=run,
            workspace=lane["workspace"],
            pool=lane["pool"],
            coordinator_root=lane["coordinator_root"],
            creator=creator,
        )
    assert creator.calls == 0
    assert lane["registry"].list_jobs() == []


# ---------------------------------------------------------------------------
# 3：trusted-build reclaim 走 builder unit
# ---------------------------------------------------------------------------


def test_trusted_workflow_reclaim_routes_through_the_builder_unit(lane, three_uid_reclaim) -> None:
    """正式採信路徑（`apply_workflow_action`）觸發的回收走 builder unit。"""

    run, job = _dispatch_card(lane)
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    marker = job_workspace.read_marker(clone)

    run, clone = _finish_card(
        lane["registry"], run=run, job=job,
        workspace=lane["workspace"], coordinator_root=lane["coordinator_root"],
    )

    [call] = three_uid_reclaim["unit_calls"]
    assert call["workspace"] == str(clone)
    assert call["marker"] == marker
    assert three_uid_reclaim["manager_scans"] == [], "Manager 不得自己做 dirty scan"
    assert not clone.exists()
    assert run.candidate_head is not None


def test_builder_unit_reclaim_reports_reclaimed(lane, three_uid_reclaim) -> None:
    run, job = _dispatch_card(lane)
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    candidate = str(job_workspace.read_marker(clone)["base"])

    result = manager._reclaim_trusted_build_workspace(job, run=run, candidate=candidate)

    assert result is not None
    assert result.status == worktree_reclaim.RECLAIM_RECLAIMED
    assert result.directory_removed
    assert len(three_uid_reclaim["unit_calls"]) == 1
    assert three_uid_reclaim["manager_scans"] == []


# ---------------------------------------------------------------------------
# 4：負向——一律 fail closed、不刪任何東西
# ---------------------------------------------------------------------------


def _leave_builder_content(clone: Path) -> dict[str, bytes]:
    (clone / "untracked.txt").write_text("builder output\n", encoding="utf-8")
    return _tree(clone)


@pytest.mark.parametrize(
    "tamper",
    ["other-owner", "other-attempt", "identity-stripped", "attempt-stripped"],
)
def test_tampered_marker_is_refused_without_touching_the_workspace(
    lane, three_uid_reclaim, tamper: str
) -> None:
    """marker 位於 builder 可寫的 `.git/`：被換掉時以 Manager 自己的 job 記錄為準。

    拿掉身分（想把回收逼回 Manager 路徑）或換成別人的身分，都不得回收。
    """

    run, job = _dispatch_card(lane)
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    marker_path = job_workspace.marker_path(clone)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if tamper == "other-owner":
        marker["owner_identity"] = {**marker["owner_identity"], "work_id": "someone-else"}
    elif tamper == "other-attempt":
        marker["attempt_id"] = "f" * 32
    elif tamper == "identity-stripped":
        marker.pop("owner_identity")
        marker.pop("attempt_id")
    else:
        marker.pop("attempt_id")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    before = _leave_builder_content(clone)

    result = manager._reclaim_trusted_build_workspace(
        job, run=run, candidate=str(marker["base"])
    )

    assert result is None
    target, refusal = manager._trusted_build_workspace_target(
        job, run=run, candidate=str(marker["base"])
    )
    assert target is None
    assert refusal == "workspace-owner-identity-mismatch"
    assert _tree(clone) == before
    assert three_uid_reclaim["unit_calls"] == []
    assert three_uid_reclaim["manager_scans"] == []


def test_marker_identity_on_a_job_without_one_is_refused(lane, three_uid_reclaim) -> None:
    """唯讀卡（或升級前的 job）沒有身分：marker 自己長出身分也不能把回收導進 builder unit。"""

    run, job = _dispatch_card(lane, read_only=True, card="worktree-isolation")
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    marker_path = job_workspace.marker_path(clone)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["owner_identity"] = _expected_owner(job)
    marker["attempt_id"] = "e" * 32
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    before = _leave_builder_content(clone)

    assert manager._reclaim_trusted_build_workspace(
        job, run=run, candidate=str(marker["base"])
    ) is None
    _target, refusal = manager._trusted_build_workspace_target(
        job, run=run, candidate=str(marker["base"])
    )
    assert refusal == "workspace-owner-identity-mismatch"
    assert _tree(clone) == before
    assert three_uid_reclaim["unit_calls"] == []


@pytest.mark.parametrize("field", ["repo", "work_id", "slice_id"])
def test_job_identity_that_does_not_bind_this_run_and_card_is_refused(
    lane, three_uid_reclaim, field: str
) -> None:
    """job 記錄與 marker 一致但不屬於這個 run／card ⇒ 拒絕（身分不符）。"""

    run, job = _dispatch_card(lane)
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    forged = {**job["owner_identity"], field: "other-owner/other-repo" if field == "repo" else "other"}
    marker_path = job_workspace.marker_path(clone)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["owner_identity"] = forged
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    before = _leave_builder_content(clone)
    forged_job = {**job, "owner_identity": forged}

    assert manager._reclaim_trusted_build_workspace(
        forged_job, run=run, candidate=str(marker["base"])
    ) is None
    _target, refusal = manager._trusted_build_workspace_target(
        forged_job, run=run, candidate=str(marker["base"])
    )
    assert refusal == "job-owner-identity-invalid"
    assert _tree(clone) == before
    assert three_uid_reclaim["unit_calls"] == []


def test_builder_unit_failure_fails_closed_and_keeps_the_workspace(
    lane, three_uid_reclaim
) -> None:
    run, job = _dispatch_card(lane)
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    before = _leave_builder_content(clone)
    three_uid_reclaim["behaviour"]["result"] = RuntimeError(
        "builder reclaim unit failed rc=1: owner reclaim failed"
    )

    result = manager._reclaim_trusted_build_workspace(
        job, run=run, candidate=str(job_workspace.read_marker(clone)["base"])
    )

    assert result is not None
    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert "builder-owner-reclaim-failed" in str(result.detail)
    assert _tree(clone) == before
    assert three_uid_reclaim["manager_scans"] == [], "unit 失敗不得退回 Manager dirty scan"


@pytest.mark.parametrize(
    "record",
    [
        {"status": "cleared", "workspace_name": "another-slot"},
        {"status": "refused"},
        "claims-cleared-but-left-content",
    ],
)
def test_invalid_completion_record_fails_closed_and_keeps_the_workspace(
    lane, three_uid_reclaim, record
) -> None:
    run, job = _dispatch_card(lane)
    three_uid_reclaim["activate"]()
    clone = Path(str(job["worktree"]))
    before = _leave_builder_content(clone)
    if record == "claims-cleared-but-left-content":
        record = {"status": "cleared", "workspace_name": clone.name}
    three_uid_reclaim["behaviour"]["result"] = record

    result = manager._reclaim_trusted_build_workspace(
        job, run=run, candidate=str(job_workspace.read_marker(clone)["base"])
    )

    assert result is not None
    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert _tree(clone) == before
    assert three_uid_reclaim["manager_scans"] == []


# ---------------------------------------------------------------------------
# 真的 Manager 半（#1167）＋ helper：只有 `systemctl start --wait` 以 seam 模擬
# ---------------------------------------------------------------------------


@pytest.fixture
def manager_half(tmp_path: Path, monkeypatch, lane):
    """與 tests/test_owner_reclaim_1167.py 的 `manager_runtime` 同一套 runtime 形狀。"""

    agents = tmp_path / "agents"
    spool = tmp_path / "job-specs" / "builder"
    spool.mkdir(parents=True, mode=0o700)
    home = tmp_path / "builder-home"
    home.mkdir()
    controls = tmp_path / "codex-controls" / "builder"
    (controls / "plugins").mkdir(parents=True)
    (controls / "skills").mkdir()
    (controls / "config.toml").write_text("model = 'fixture'\n", encoding="utf-8")
    (controls / "hooks.json").write_text("{}\n", encoding="utf-8")
    lane["pool"].mkdir(parents=True, exist_ok=True)
    env = {
        "PSC_AGENTS_ROOT": str(agents),
        "PSC_REPO_ROOT": str(lane["workspace"]),
        "PSC_WORKTREE_ROOT": str(lane["pool"]),
        "PSC_JOB_RUNNER": "systemd-template",
        "PSC_MANAGER_EXECUTOR": "codex",
        "PSC_BUILDER_ACCOUNT": pwd.getpwuid(os.getuid()).pw_name,
        "PSC_BUILDER_GROUP": grp.getgrgid(os.getgid()).gr_name,
        "PSC_BUILDER_HOME": str(home),
        "PSC_BUILDER_PATH": "/usr/bin:/bin",
        "PSC_JOB_SPEC_SPOOL": str(spool),
        "PSC_CODEX_CONTROL_ROOT": str(controls.parent),
    }
    started: list[dict[str, object]] = []

    def fake_unit(argv):
        unit = argv[-1]
        instance = unit.split("@", 1)[1].removesuffix(".service")
        spec = json.loads((spool / f"{instance}.json").read_text(encoding="utf-8"))
        started.append({"argv": list(argv), "spec": spec})
        with open(spec["log_path"], "a", encoding="utf-8") as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                rc = owner_reclaim.main(
                    spec["command"][3:], approval_spool=spool, manager_uid=os.getuid()
                )
        return subprocess.CompletedProcess(list(argv), rc, "", "")

    def activate() -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(owner_reclaim, "APPROVAL_SPEC_SPOOL", str(spool))
        monkeypatch.setattr(
            job_runner, "preflight_systemd_template", lambda **_kwargs: "/usr/bin/systemctl"
        )
        monkeypatch.setattr(job_runner, "_unit_is_active", lambda *_args: False)
        monkeypatch.setattr(spool_slot, "_apply_slot_acl", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(owner_reclaim, "_grant_archive_acl", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(owner_reclaim, "_start_builder_unit", fake_unit)

    return {"activate": activate, "started": started, "spool": spool}


def test_workflow_workspace_is_cleared_by_the_real_builder_half(lane, manager_half) -> None:
    """workflow lane 的 marker（身分形狀、instance 名）通過 #1167 的核准與 helper 檢查。"""

    run, job = _dispatch_card(lane)
    clone = Path(str(job["worktree"]))
    (clone / "untracked.txt").write_text("builder output\n", encoding="utf-8")
    candidate = str(job_workspace.read_marker(clone)["base"])
    manager_half["activate"]()

    result = manager._reclaim_trusted_build_workspace(job, run=run, candidate=candidate)

    assert result is not None and result.status == worktree_reclaim.RECLAIM_RECLAIMED, result
    assert not clone.exists()
    [started] = manager_half["started"]
    spec = started["spec"]
    # unit instance 就是工作區目錄名，也就是當初那顆 workflow job 的 `%i`。
    assert spec["instance"] == clone.name == job_workspace.job_segment(str(job["job_id"]))
    assert spec["command"][1:3] == ["-m", owner_reclaim.MODULE]
    assert not (manager_half["spool"] / f"{clone.name}.json").exists(), "核准單次使用"
    archive = Path(str(result.preserved_ref))
    assert archive.parent == owner_reclaim.reclaim_evidence_root()
    assert (archive / "untracked.txt").read_text(encoding="utf-8") == "builder output\n"


def test_marker_swapped_after_approval_is_refused_by_the_helper(lane, manager_half, monkeypatch) -> None:
    """Manager 核准的是它讀到的 marker digest；unit 起跑前 marker 被換掉 ⇒ helper 拒絕。"""

    run, job = _dispatch_card(lane)
    clone = Path(str(job["worktree"]))
    before = _leave_builder_content(clone)
    candidate = str(job_workspace.read_marker(clone)["base"])
    manager_half["activate"]()
    real_unit = owner_reclaim._start_builder_unit
    marker_path = job_workspace.marker_path(clone)

    def swap_then_start(argv):
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["created_at"] = "1970-01-01T00:00:00Z"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        return real_unit(argv)

    monkeypatch.setattr(owner_reclaim, "_start_builder_unit", swap_then_start)

    result = manager._reclaim_trusted_build_workspace(job, run=run, candidate=candidate)

    assert result is not None
    assert result.status == worktree_reclaim.RECLAIM_FAILED
    assert "does not match the Manager-approved digest" in str(result.detail)
    after = _tree(clone)
    assert {key: value for key, value in after.items() if key != str(marker_path.relative_to(clone))} == {
        key: value for key, value in before.items() if key != str(marker_path.relative_to(clone))
    }
