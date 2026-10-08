"""#1259：OpenSpec archive 之後，retry-build 必被 `builder-todo-authority-changed` 擋下。

現場（deployment canary #716）：run 已走到 ship，Manager 完成 OpenSpec archive 並開出
交付 PR；Copilot 留下 finding，operator 依正式出口下 `retry-build --expected-candidate`，
action 受理並 reset run，但接著的 Builder 派工在建 Job 前被擋下。

兩個阻斷點：

1. Manager archive 把本 run 的 mapped OpenSpec 從 ``state:active`` 推到
   ``state:archived``，WorkAuthority digest 前進；self-only drift 判準不認這個
   Manager 自產的 terminal transition。
2. 交付 PR 的 self-only 例外只在 review／ship phase 且 ``verified_head == candidate``
   時成立；retry-build reset 把 run 推回 build、清掉 ``verified_head``，同一個 PR
   就被當成外部 drift。

修正後的判準：

- archive 例外：只接受 ``run.openspec_refs`` 唯一的那個 change 從 active 變 archived，
  而且要有 Manager archive step＋registry 中唯一的 Manager archive job（綁 run、
  subject_head 為 Candidate 本身或祖先）。
- PR 例外（build phase）：retry-build receipt（綁本 run、exact Candidate 與 reset 後的
  build attempt）、delivery journal 推送證據（綁本 run、PR 編號、push head 為 Candidate
  本身或祖先）、authority 裡唯一且 open 的同一個 PR，三項缺一不可。
- Builder admission 與 retry-build admission（action 與 projection）共用同一個判準，
  後者以 reset 後的 run 形狀評估：受理就派得出 Builder，派不出去就在 reset 前拒絕。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from paulsha_cortex.control.contract import build_request
from paulsha_cortex.coordinator import manager, manager_daemon, work_actions, work_bridge
from paulsha_cortex.coordinator.claim import (
    claim_key_for_authority_digest,
    load_work_authority,
    work_authority_digest,
)
from paulsha_cortex.coordinator.runtime_preflight import ExecutorEnvironment
from paulsha_cortex.coordinator.launcher import LaunchHandle
from paulsha_cortex.coordinator.model_identities import IdentityRegistry
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.workflow import GateEvidenceRef
from paulsha_cortex.deck.compile import compile_combo
from paulsha_cortex.deck.schema import (
    DEFAULT_CARDS_PATH,
    DEFAULT_COMBOS_DIR,
    load_cards,
    load_combo,
)

from diagnostic_fixtures import fixture_needs_human_reason
from git_fixtures import StubWorktreeCreator


REPO = "acme/demo"
WORK_ID = "demo"
CHANGE = "demo"
ISSUE = 12
PR = 77
BRANCH = "feature/12-demo"
TASK = "archive drift retry"
PLAN_REF = "docs/superpowers/plans/archive-drift-retry.md"
TODO_REF = "docs/superpowers/workstreams/demo/todo.md"
NOW_ISO = "2026-10-05T00:00:00Z"
FINDING = "Copilot finding：空輸入需回傳空結果。"
_CODEX_ROW = {
    "executor": "codex",
    "model_id": "gpt-primary",
    "independence_domain": "openai",
    "capabilities": ["build"],
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").lower()


def _init_repo(root: Path) -> dict[str, str]:
    """base → pre-archive Candidate → Manager archive commit；另有一條無關的 stray commit。"""

    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    _write(root, "README.md", "fixture\n")
    _write(root, PLAN_REF, "# archive drift retry plan\n")
    _write(root, TODO_REF, "- [ ] 修正 archive 後的 retry-build\n")
    _write(root, f"openspec/changes/{CHANGE}/proposal.md", "# proposal\n")
    _write(root, f"openspec/changes/{CHANGE}/tasks.md", "- [x] 1.1 task\n")
    base = _commit(root, "base")
    _git(root, "checkout", "-q", "-b", BRANCH)
    _write(root, "src/feature.py", "VALUE = 1\n")
    # builder 在 build 卡勾選 workstream Todo 的 checkbox。
    _write(root, TODO_REF, "- [x] 修正 archive 後的 retry-build\n")
    pre_archive = _commit(root, "feat: implement")
    archived = root / "openspec" / "changes" / "archive" / f"2026-10-05-{CHANGE}"
    archived.parent.mkdir(parents=True)
    (root / "openspec" / "changes" / CHANGE).rename(archived)
    archive = _commit(root, f"chore(openspec): archive {CHANGE}")
    _git(root, "checkout", "-q", "-b", "stray", base)
    _write(root, "stray.txt", "unrelated\n")
    stray = _commit(root, "unrelated")
    _git(root, "checkout", "-q", BRANCH)
    return {"base": base, "pre_archive": pre_archive, "archive": archive, "stray": stray}


def _provider(revision: str) -> dict[str, object]:
    return {
        "status": "ok",
        "last_attempt_at": NOW_ISO,
        "last_success_at": NOW_ISO,
        "revision": revision,
        "diagnostics": [],
        "sources": [],
        "observations": {},
    }


def _source(
    source_id: str,
    kind: str,
    ref: str,
    revision: str,
    provider: str,
    status: str | None = None,
) -> dict[str, str]:
    row = {
        "source_id": source_id,
        "kind": kind,
        "ref": ref,
        "revision": revision,
        "confidence": "confirmed",
        "provider": provider,
    }
    if status is not None:
        row["status"] = status
    return row


def _write_snapshot(
    path: Path,
    *,
    openspec_status: str = "active",
    prs: tuple[int, ...] = (),
    pr_status: str = "open",
    issue_status: str = "open",
    todo_path: str = TODO_REF,
    todo_revision: str = "todo-blob:unchecked",
    extra_sources: tuple[dict[str, str], ...] = (),
    provider_revision: str = "github-snapshot:1",
) -> Path:
    sources = [
        _source(
            f"github_issue:{REPO}#{ISSUE}",
            "github_issue",
            f"{REPO}#{ISSUE}",
            f"github:issue:{ISSUE}",
            f"github:{REPO}",
            issue_status,
        ),
        _source(
            f"todo:{REPO}:{todo_path}", "todo", todo_path, todo_revision, f"repo:{REPO}"
        ),
        _source(
            f"openspec:{REPO}:{CHANGE}",
            "openspec",
            CHANGE,
            f"openspec-tree:{openspec_status}",
            f"repo:{REPO}",
            openspec_status,
        ),
    ]
    for number in prs:
        sources.append(
            _source(
                f"github_pr:{REPO}#{number}",
                "github_pr",
                f"{REPO}#{number}",
                f"github:pr:{number}",
                f"github:{REPO}",
                pr_status,
            )
        )
    sources.extend(extra_sources)
    payload = {
        "schema": "work-items-snapshot/v1",
        "providers": {
            f"github:{REPO}": _provider(provider_revision),
            f"repo:{REPO}": _provider("repo-snapshot:1"),
        },
        "work_items": [
            {"repo": REPO, "work_id": WORK_ID, "state": "in-progress", "sources": sources}
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _manifest_steps():
    cards = load_cards(DEFAULT_CARDS_PATH)
    combo = load_combo(DEFAULT_COMBOS_DIR / "feature-oneshot.yaml", cards)
    result = compile_combo(combo, cards, TASK, change=CHANGE)
    assert result.workflow_manifest is not None
    return result.workflow_manifest.steps


class _RecordingLauncher:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.as_commit_required = lambda: self

    def as_read_only(self):
        return self

    def as_review_only(self, *, terminal_kind: str):
        return self

    def executor_environment(self) -> ExecutorEnvironment:
        return ExecutorEnvironment(
            name="codex-workflow",
            interpreter=(sys.executable,),
            path=os.environ.get("PATH", ""),
            home=os.path.expanduser("~"),
            provider_identity="codex",
        )

    def launch(self, *, slice_id, prompt, worktree, log_dir):
        self._calls.append("codex/gpt-primary")
        return LaunchHandle(
            executor="codex",
            model_id="gpt-primary",
            session_name=slice_id,
            pid=4242,
            log_path=str(Path(log_dir) / f"{slice_id}.jsonl"),
        )


class _Fixture:
    """post-archive、已開交付 PR、Copilot 要求修正的 review run（未掛 needs_human）。"""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        with_pr: bool = True,
        archive_owner: str = "self",
        archive_subject: str = "archive",
        journal_owner: str = "self",
        push_heads: tuple[str, ...] = ("archive",),
        run_pr_number: int = PR,
        current_prs: tuple[int, ...] | None = None,
        current_pr_status: str = "open",
        issue_status: str = "open",
        current_todo_path: str = TODO_REF,
        journal_binding: bool = True,
        current_openspec_status: str = "archived",
        extra_sources: tuple[dict[str, str], ...] = (),
        needs_human_reason: str | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.coordinator = tmp_path / "coordinator"
        self.coordinator.mkdir(parents=True)
        self.journal = self.coordinator / "delivery-journal.json"
        monitor = tmp_path / "monitor"
        monkeypatch.setenv("PSC_MONITOR_STATE_ROOT", str(monitor))
        self.snapshot = monitor / "work-items.snapshot.json"
        self.repo_root = tmp_path / "repo"
        self.heads = _init_repo(self.repo_root)
        self.candidate = self.heads["archive"]

        claim_snapshot = _write_snapshot(tmp_path / "claim-snapshot.json")
        self.claim_authority = load_work_authority(
            repo=REPO, work_id=WORK_ID, snapshot_path=claim_snapshot
        )
        digest = work_authority_digest(self.claim_authority)
        self.registry = JobRegistry(state_path=self.coordinator / "jobs.json")
        run = self.registry._manager_create_workflow_run(
            work_id=WORK_ID,
            repo=REPO,
            claim_key=claim_key_for_authority_digest(
                repo=REPO, work_id=WORK_ID, authority_digest=digest
            ),
            source_revision=digest,
            workspace_root=str(self.repo_root),
            combo="feature-oneshot",
            current_phase="build",
            steps=_manifest_steps(),
            issue_refs=(f"{REPO}#{ISSUE}",),
            openspec_refs=self.claim_authority.mapped_openspec,
            attempts={"build": 1},
            gate_status="running",
        )
        passed = tuple(
            replace(
                step,
                executor="cortex-manager",
                model="deterministic",
                domain="cortex",
                gate_result="passed",
            )
            if step.phase == "ship" and step.card == "openspec-archive"
            else replace(step, gate_result="passed")
            if step.phase in {"claim", "define", "plan", "build", "verify", "review"}
            else step
            for step in run.steps
        )
        for phase in ("verify", "review"):
            self.registry._manager_update_workflow_run(run.run_id, current_phase=phase)
        values = {
            "steps": passed,
            "attempts": {"build": 1, "verify": 2, "review": 2},
            "candidate_head": self.candidate,
            "verified_head": self.candidate,
            "pr_refs": (f"{REPO}#{run_pr_number}",) if with_pr else (),
            "gate_refs": (
                GateEvidenceRef("foreign-review", "reports/review/accepted.md", "f" * 64),
            ),
            "gate_status": "passed",
            "facets": (),
        }
        if needs_human_reason is not None:
            values.update(
                facets=("needs_human",),
                gate_status="running",
                needs_human_reason=fixture_needs_human_reason(
                    needs_human_reason, candidate=self.candidate
                ),
            )
        self.run = self.registry._manager_update_workflow_run(run.run_id, **values)

        # Manager 的 archive job：沿用 production writer（ship evidence＋workflow_evidence）。
        archive_run = self.run
        if archive_owner == "other":
            other = self.registry._manager_create_workflow_run(
                work_id="other-work",
                repo=REPO,
                claim_key="claim:v1:" + "3" * 64,
                source_revision="4" * 64,
                workspace_root=str(self.repo_root),
                combo="feature-oneshot",
                current_phase="build",
                steps=_manifest_steps(),
                issue_refs=(),
                openspec_refs=(),
                attempts={"build": 1},
                gate_status="running",
            )
            archive_run = other
        if archive_owner != "none":
            work_bridge._record_manager_ship_job(
                registry=self.registry,
                state_root=self.coordinator,
                run=archive_run,
                worktree=self.repo_root,
                branch=BRANCH,
                card="openspec-archive",
                old_head=self.heads["pre_archive"],
                new_head=self.heads[archive_subject],
            )
        else:
            # 外部 archive：run 沒有任何 Manager archive 紀錄。
            self.run = self.registry._manager_update_workflow_run(
                run.run_id,
                steps=tuple(
                    replace(step, executor=None, model=None, domain=None, gate_result="pending")
                    if step.card == "openspec-archive"
                    else step
                    for step in self.run.steps
                ),
            )

        if with_pr and journal_owner != "none":
            state = work_actions._load_runs(self.journal)
            row = work_actions._delivery_journal_row(self.run, self.claim_authority)
            row["pushes"] = {
                self.heads[name]: {
                    "branch": BRANCH,
                    "ref": f"refs/heads/{BRANCH}",
                    "head": self.heads[name],
                }
                for name in push_heads
            }
            row["delivery_binding"] = {
                "pr_number": run_pr_number,
                "change": CHANGE,
                "todo_paths": [TODO_REF],
            }
            if not journal_binding:
                row.pop("delivery_binding")
            else:
                row["ship"] = {
                    "phase": "needs-fix",
                    "head": self.candidate,
                    "review_id": 1,
                    "finding_count": 1,
                    "findings": [{"path": "src/feature.py", "line": 1, "body": FINDING}],
                    "fix_rounds": 0,
                }
            key = self.run.run_id
            if journal_owner == "other":
                key = "workflow-" + "9" * 20
                row["run_id"] = key
            state["runs"][key] = row
            work_actions._save_runs(self.journal, state)

        if current_prs is None:
            current_prs = (PR,) if with_pr else ()
        _write_snapshot(
            self.snapshot,
            openspec_status=current_openspec_status,
            prs=current_prs,
            pr_status=current_pr_status,
            issue_status=issue_status,
            todo_path=current_todo_path,
            todo_revision="todo-blob:checked",
            extra_sources=extra_sources,
            provider_revision="github-snapshot:2",
        )
        self.authority = load_work_authority(
            repo=REPO, work_id=WORK_ID, snapshot_path=self.snapshot
        )
        self.launch_calls: list[str] = []
        # build 卡的 declared input（plan）從 builder 工作區快照；stub 指回 fixture repo。
        self.creator = StubWorktreeCreator(self.repo_root)
        self.dispatcher = type(
            "D",
            (),
            {
                "_registry": self.registry,
                "_git_runner": None,
                "_worktree_creator": self.creator,
            },
        )()

    def executor(self):
        def real_work_action(*, args, requested_by):
            return work_actions.execute_work_action(
                args=args,
                requested_by=requested_by,
                snapshot_path=self.snapshot,
                state_path=self.journal,
                workflow_registry=self.registry,
            )

        return manager_daemon.build_request_executor(
            dispatcher=self.dispatcher,
            specs_dir=str(self.tmp_path / "specs"),
            handoff_dir=str(self.tmp_path / "handoff"),
            workflow_identity_registry=IdentityRegistry.from_rows([_CODEX_ROW]),
            launcher=_RecordingLauncher(self.launch_calls),
            work_action_fn=real_work_action,
        )

    def retry_build_request(self):
        return build_request(
            req_type="work-action",
            args={
                "action": "retry-build",
                "repo": REPO,
                "work_id": WORK_ID,
                "issue": ISSUE,
                "actor": "operator",
                "expected_candidate": self.candidate,
                "reason": FINDING,
            },
            requested_by="operator",
        )

    def receipts(self) -> list[Path]:
        root = self.coordinator / "evidence" / "retry-build"
        return sorted(root.glob("*.json")) if root.is_dir() else []

    def dispatch_builder(self, run):
        return manager.dispatch_workflow_card(
            self.dispatcher,
            run=run,
            identities=IdentityRegistry.from_rows([_CODEX_ROW]),
            launcher_factory=lambda _identity: _RecordingLauncher(self.launch_calls),
            coordinator_root=self.coordinator,
            force_new_card=True,
            builder_todo_admission=manager_daemon._builder_todo_admission_for_run(run),
        )


def _assert_rejected_before_reset(
    fixture: _Fixture, *, match: str = "would not dispatch a builder: builder-todo-authority-changed"
) -> None:
    before_run = fixture.registry.get_workflow_run(fixture.run.run_id).to_dict()
    before_jobs = [dict(job) for job in fixture.registry.list_jobs()]

    with pytest.raises(RuntimeError, match=match):
        fixture.executor()(fixture.retry_build_request())

    assert fixture.registry.get_workflow_run(fixture.run.run_id).to_dict() == before_run
    assert [dict(job) for job in fixture.registry.list_jobs()] == before_jobs
    assert fixture.creator.calls == []
    assert fixture.launch_calls == []
    assert fixture.receipts() == []
    assert not (fixture.coordinator / "evidence" / "operator-adjudication").exists()


# ---------------------------------------------------------------------------
# 勾選 todo checkbox 不改變 work_authority_digest
# ---------------------------------------------------------------------------


def test_todo_checkbox_does_not_change_work_authority_digest(tmp_path: Path) -> None:
    unchecked = load_work_authority(
        repo=REPO,
        work_id=WORK_ID,
        snapshot_path=_write_snapshot(
            tmp_path / "unchecked.json", todo_revision="todo-blob:unchecked"
        ),
    )
    checked = load_work_authority(
        repo=REPO,
        work_id=WORK_ID,
        snapshot_path=_write_snapshot(
            tmp_path / "checked.json",
            todo_revision="todo-blob:checked",
            provider_revision="github-snapshot:2",
        ),
    )

    assert unchecked.snapshot_hash != checked.snapshot_hash
    assert work_authority_digest(checked) == work_authority_digest(unchecked)


# ---------------------------------------------------------------------------
# 正向整合：production daemon → work action → Manager builder dispatch
# ---------------------------------------------------------------------------


def test_retry_build_after_archive_and_open_pr_dispatches_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claim 時 active → Manager archive → 本 run 的 open PR → Copilot finding → retry-build。"""

    fixture = _Fixture(tmp_path, monkeypatch)
    assert work_authority_digest(fixture.authority) != fixture.run.source_revision

    result = fixture.executor()(fixture.retry_build_request())

    assert result["result"]["dispatch"]["kind"] == "job"
    job = fixture.registry.get_job(result["result"]["job_id"])
    assert job["workflow_run_id"] == fixture.run.run_id
    assert job["workflow_phase"] == "build"
    assert job["workflow_card"] == "subagent-build"
    assert job["dispatch_head"] == fixture.candidate
    assert fixture.launch_calls == ["codex/gpt-primary"]
    after = fixture.registry.get_workflow_run(fixture.run.run_id)
    assert after.current_phase == "build"
    assert after.candidate_head == fixture.candidate
    assert after.source_revision == fixture.run.source_revision
    assert after.claim_key == fixture.run.claim_key
    assert "needs_human" not in after.facets
    [receipt] = fixture.receipts()
    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert body["schema"] == "cortex-retry-build-receipt/v1"
    assert body["run_id"] == fixture.run.run_id
    assert body["expected_candidate"] == fixture.candidate
    assert body["build_attempt"] == after.attempts["build"] == 2
    assert body["pr_refs"] == [f"{REPO}#{PR}"]


def test_retry_build_after_autosync_push_without_legacy_binding_dispatches_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production Manager push journal plus run PR ref survives an autosync Candidate."""

    fixture = _Fixture(tmp_path, monkeypatch, journal_binding=False)
    before_candidate = fixture.candidate
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    _git(fixture.repo_root, "remote", "add", "origin", str(origin))
    _git(fixture.repo_root, "push", "-q", "origin", BRANCH)

    _git(fixture.repo_root, "branch", "main", fixture.heads["base"])
    _git(fixture.repo_root, "checkout", "-q", "main")
    _write(fixture.repo_root, "main.txt", "main update\n")
    main_head = _commit(fixture.repo_root, "main update")
    _git(fixture.repo_root, "checkout", "-q", BRANCH)
    _git(fixture.repo_root, "merge", "--no-ff", "-m", "Manager autosync", "main")
    autosync_candidate = _git(fixture.repo_root, "rev-parse", "HEAD").lower()

    before_sync = fixture.registry.get_workflow_run(fixture.run.run_id)
    work_bridge._record_manager_ship_job(
        registry=fixture.registry,
        state_root=fixture.coordinator,
        run=before_sync,
        worktree=fixture.repo_root,
        branch=BRANCH,
        card="main-sync-autosync",
        old_head=fixture.candidate,
        new_head=autosync_candidate,
    )
    synced = fixture.registry._manager_reset_workflow_for_main_sync(
        before_sync.run_id,
        expected_candidate=fixture.candidate,
        expected_updated_at=before_sync.updated_at,
        candidate_head=autosync_candidate,
        steps=before_sync.steps,
        gate_refs=before_sync.gate_refs,
        evidence_refs=("evidence/main-sync-autosync/merged.json",),
    )
    subprocess.run(
        ["git", "-C", str(fixture.repo_root), "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}"],
        check=True,
    )
    state = work_actions._load_runs(fixture.journal)
    row = state["runs"][synced.run_id]
    row["pushes"][autosync_candidate] = {
        "branch": BRANCH,
        "ref": f"refs/heads/{BRANCH}",
        "head": autosync_candidate,
    }
    work_actions._save_runs(fixture.journal, state)
    reverified = fixture.registry._manager_update_workflow_run(
        synced.run_id,
        current_phase="review",
        verified_head=autosync_candidate,
        steps=tuple(
            replace(step, gate_result="passed")
            if step.phase in {"verify", "review"}
            else step
            for step in synced.steps
        ),
        gate_refs=(
            GateEvidenceRef("foreign-review", "reports/review/accepted.md", "f" * 64),
        ),
        facets=("needs_human",),
        gate_status="running",
        needs_human_reason=fixture_needs_human_reason(
            "review-terminal-explicit-stop", candidate=autosync_candidate
        ),
    )
    fixture.candidate = autosync_candidate

    journal_row = work_actions._load_runs(fixture.journal)["runs"][reverified.run_id]
    assert journal_row.get("delivery_binding") is None
    assert journal_row["run_id"] == reverified.run_id
    assert journal_row["claim_key"] == reverified.claim_key
    assert reverified.pr_refs == (f"{REPO}#{PR}",)
    assert autosync_candidate in journal_row["pushes"]
    parents = _git(
        fixture.repo_root, "rev-list", "--parents", "-n", "1", autosync_candidate
    ).split()
    assert parents == [autosync_candidate, before_candidate, main_head]
    assert _git(origin, "rev-parse", f"refs/heads/{BRANCH}") == autosync_candidate

    result = fixture.executor()(fixture.retry_build_request())

    assert result["result"]["dispatch"]["kind"] == "job"
    job = fixture.registry.get_job(result["result"]["job_id"])
    assert job["workflow_run_id"] == reverified.run_id
    assert job["dispatch_head"] == autosync_candidate
    assert fixture.launch_calls == ["codex/gpt-primary"]


def test_retry_build_after_archive_without_pr_dispatches_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """post-archive 的 verify／review 停止點（PR 尚未開出）：只有 archive 漂移。"""

    fixture = _Fixture(tmp_path, monkeypatch, with_pr=False)

    result = fixture.executor()(fixture.retry_build_request())

    assert result["result"]["dispatch"]["kind"] == "job"
    assert fixture.launch_calls == ["codex/gpt-primary"]


def test_projection_lists_retry_build_only_when_builder_would_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitted = _Fixture(
        tmp_path / "admitted",
        monkeypatch,
        needs_human_reason="review-terminal-explicit-stop",
    )
    assert "retry-build" in work_actions._phase_recovery_actions(
        admitted.run, admitted.registry, admitted.authority
    )

    rejected = _Fixture(
        tmp_path / "rejected",
        monkeypatch,
        needs_human_reason="review-terminal-explicit-stop",
        journal_owner="none",
    )
    assert "retry-build" not in work_actions._phase_recovery_actions(
        rejected.run, rejected.registry, rejected.authority
    )


# ---------------------------------------------------------------------------
# 負向：在 reset、Job、worktree、launch 之前拒絕
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        # archive 半邊
        {"archive_owner": "none"},  # 外部 archive：沒有本 run 的 Manager archive 證據
        {"archive_owner": "other"},  # archive job 屬於別的 run
        {"archive_subject": "stray"},  # archive job 的 subject 不是 Candidate 本身或祖先
        # PR 半邊
        {"journal_owner": "none"},  # 沒有推送證據
        {"journal_owner": "other"},  # 推送證據屬於別的 run
        {"push_heads": ("stray",)},  # push head 不是 Candidate 的祖先
        {"push_heads": ("archive", "stray")},  # 任一 push 不在 Candidate 歷史上
        {"current_prs": (PR, PR + 1)},  # authority 的 PR 不唯一
        {"current_pr_status": "closed"},  # PR 不是 open
        {"run_pr_number": PR + 1},  # run 綁的 PR 與 authority 不同（非 exact）
        {"issue_status": "closed"},  # 已關閉 issue 不因 Manager 的 push 證據獲准
        {"current_todo_path": "docs/superpowers/workstreams/other/todo.md"},  # Todo 換綁
        # 外部新增 link
        {
            "extra_sources": (
                _source(
                    f"superpowers_plan:{REPO}:docs/superpowers/plans/foreign.md",
                    "superpowers_plan",
                    "docs/superpowers/plans/foreign.md",
                    "plan-blob:1",
                    f"repo:{REPO}",
                ),
            )
        },
        {
            "extra_sources": (
                _source(
                    f"openspec:{REPO}:foreign-change",
                    "openspec",
                    "foreign-change",
                    "openspec-tree:active",
                    f"repo:{REPO}",
                    "active",
                ),
            )
        },
    ],
    ids=[
        "external-archive",
        "archive-job-other-run",
        "archive-job-other-candidate",
        "no-push-evidence",
        "push-evidence-other-run",
        "push-head-not-ancestor",
        "one-push-not-ancestor",
        "pr-not-unique",
        "pr-not-open",
        "pr-not-exact",
        "issue-closed",
        "todo-rebound",
        "external-plan-link",
        "external-openspec-link",
    ],
)
def test_retry_build_rejects_unproven_drift_before_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: dict
) -> None:
    # Exercise the workflow PR evidence fallback that omits the legacy work-action
    # delivery_binding field; every changed target must still fail closed.
    fixture = _Fixture(tmp_path, monkeypatch, journal_binding=False, **case)

    _assert_rejected_before_reset(
        fixture,
        # 非 run 自產的 OpenSpec change 在 canonical run 選取（`openspec_refs_compatible`）
        # 就已拒絕，比 Builder admission 更早；其餘一律由共用的 Builder admission 拒絕。
        match=(
            "one active canonical WorkflowRun"
            if any(source["kind"] == "openspec" for source in case.get("extra_sources", ()))
            else "would not dispatch a builder: builder-todo-authority-changed"
        ),
    )


def test_builder_admission_requires_retry_build_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reset 後的 build phase 沒有 retry-build receipt：PR 例外不適用，不建 Job。"""

    fixture = _Fixture(tmp_path, monkeypatch)
    reset = fixture.registry._manager_reset_workflow_for_retry_build(
        fixture.run.run_id,
        expected_candidate=fixture.candidate,
        repair_action="Repair the exact Candidate.",
        post_pass_adjudicated=True,
    )
    jobs_before = [dict(job) for job in fixture.registry.list_jobs()]

    result = fixture.dispatch_builder(reset)

    assert result == {
        "run_id": reset.run_id,
        "current_phase": "build",
        "reason": "builder-todo-authority-changed",
    }
    assert [dict(job) for job in fixture.registry.list_jobs()] == jobs_before
    assert fixture.creator.calls == []
    assert fixture.launch_calls == []


def test_stale_receipt_from_failed_reset_cannot_admit_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """receipt 已寫、reset 失敗：留下的 receipt 綁 attempt N+1，對 attempt N 的 run 無效。"""

    fixture = _Fixture(tmp_path, monkeypatch)

    def failing_reset(*_args, **_kwargs):
        raise ValueError("simulated reset failure")

    monkeypatch.setattr(
        fixture.registry, "_manager_reset_workflow_for_retry_build", failing_reset
    )
    with pytest.raises(ValueError, match="simulated reset failure"):
        fixture.executor()(fixture.retry_build_request())
    monkeypatch.undo()

    [receipt] = fixture.receipts()
    assert json.loads(receipt.read_text(encoding="utf-8"))["build_attempt"] == 2
    unchanged = fixture.registry.get_workflow_run(fixture.run.run_id)
    assert unchanged.current_phase == "review"
    assert unchanged.attempts["build"] == 1

    # 以 attempt N 進 build phase（不經 retry-build reset）的 run 不得借用這份 receipt。
    stale_shape = replace(unchanged, current_phase="build", verified_head=None)
    assert manager._retry_build_receipt_candidate(
        stale_shape, coordinator_root=fixture.coordinator
    ) is None
    assert not manager.builder_authority_matches_claim_era(
        fixture.authority,
        stale_shape,
        registry=fixture.registry,
        coordinator_root=fixture.coordinator,
    )
    # 同一份 receipt 只對 reset 後（attempt N+1）的形狀有效。
    reset_shape = replace(
        stale_shape, attempts={**unchanged.attempts, "build": 2}
    )
    assert manager._retry_build_receipt_candidate(
        reset_shape, coordinator_root=fixture.coordinator
    ) == fixture.candidate
    assert manager.builder_authority_matches_claim_era(
        fixture.authority,
        reset_shape,
        registry=fixture.registry,
        coordinator_root=fixture.coordinator,
    )


def test_receipt_rejects_symlink_and_writable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(tmp_path, monkeypatch)
    fixture.executor()(fixture.retry_build_request())
    run = fixture.registry.get_workflow_run(fixture.run.run_id)
    [receipt] = fixture.receipts()
    assert manager._retry_build_receipt_candidate(
        run, coordinator_root=fixture.coordinator
    ) == fixture.candidate

    content = receipt.read_bytes()
    receipt.chmod(0o644)
    assert manager._retry_build_receipt_candidate(
        run, coordinator_root=fixture.coordinator
    ) is None

    receipt.unlink()
    elsewhere = tmp_path / "forged.json"
    elsewhere.write_bytes(content)
    elsewhere.chmod(0o444)
    receipt.symlink_to(elsewhere)
    assert manager._retry_build_receipt_candidate(
        run, coordinator_root=fixture.coordinator
    ) is None
