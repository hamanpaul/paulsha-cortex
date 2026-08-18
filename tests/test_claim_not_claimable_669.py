"""`#669` 迴歸測試：claim 判定 `missing_issue` 不得建立 run。

實測背景：自我託管首輪掃描後，`cortex status` 的 `attention` 一口氣出現 **24 個內容
完全同型的 `needs_human` run**，全部停在 `current_phase: claim`、`gate_state:
running`、`evidence_refs: []`、`next_actions: []`，`detail` 逐字是「claim 判定需要
人工介入即建立 run：missing_issue」。它們永遠不會推進，卻把唯一真正需要人看的
blocker 壓成 1:24 的信噪比。

根因是類別錯誤：`missing_issue` 對 workstream 而言**是預期狀態，不是異常**——
`docs/superpowers/workstreams/cost-governance-cluster/todo.md` 開頭逐字寫著「本
workstream 不對應單一 issue」。系統卻把這個預期狀態物化成 durable state。

本檔釘住四件事：

1. `missing_issue` 時 **`workflow_starter` 一次都不得被呼叫**（不建 run）。
2. 跳過必須留下**可查詢**的 `not-claimable` 紀錄——否則只是把噪音換成盲區，
   fail-loud 變 fail-silent，方向是錯的。
3. **真的該建 run 的情況仍然建**（不得為了消噪音把正常路徑一併關掉）。
4. 修正前留下的 claim-blocked 殭屍 run 不被靜默忽略，而是帶著清理指令浮現；
   真正卡住的 needs_human run（build／verify／review）不得被誤判成殭屍。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from paulsha_cortex.coordinator import manager_daemon, not_claimable, work_actions
from paulsha_cortex.coordinator.diagnostics import diagnostic_reason
from paulsha_cortex.coordinator.registry import JobRegistry


def _init_repo(root: Path, repo: str = "acme/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if remote.returncode != 0:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{repo}.git"],
            check=True,
        )
    return root


def _snapshot(
    path: Path,
    *,
    work_id: str = "cost-governance-cluster",
    issues: tuple[int, ...] = (),
    source_revisions: tuple[str, ...] = ("openspec:cost-governance-cluster@1",),
) -> Path:
    """一個 workstream 形狀的 work item：有 confirmed todo、有 openspec，但**沒有
    mapped issue**（`docs/superpowers/workstreams/*` 的常態）。"""

    _init_repo(path.parent)
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {
                    "github": {
                        "provider_id": "github",
                        "revision": "gh-1",
                        "last_success_epoch": 100,
                        "degraded": False,
                    }
                },
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": work_id,
                        "mapped_issues": list(issues),
                        "mapped_prs": [],
                        "mapped_openspec": [work_id],
                        "mapped_todo_paths": [
                            f"docs/superpowers/workstreams/{work_id}/todo.md"
                        ],
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


def _authority(snapshot: Path, *, work_id: str = "cost-governance-cluster"):
    return work_actions.load_work_authority(
        repo="acme/demo", work_id=work_id, snapshot_path=snapshot
    )


def _refusing_starter(*_args, **_kwargs):
    raise AssertionError("missing_issue 不得建立 run（#669）")


def _ledger(state: Path) -> Path:
    return not_claimable.ledger_path(state.parent)


# --- 1. 不建 run ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["start", "resume"])
def test_missing_issue_never_calls_the_workflow_starter(tmp_path: Path, action: str) -> None:
    """核心不變式：判定 `missing_issue` 時 workflow_starter 一次都不得被呼叫。

    這條釘住的是 issue 標題那句話本身——「missing_issue 仍建立 run」。starter
    被叫到就直接 AssertionError，不必再推論 run 的形狀。
    """

    snapshot = _snapshot(tmp_path / "work" / "snapshot.json")
    state = tmp_path / "work" / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")

    result = work_actions._claim_action(
        args={"action": action},
        authority=_authority(snapshot),
        now_epoch=200,
        state_path=state,
        workflow_registry=registry,
        workflow_starter=_refusing_starter,
    )

    assert result["action"] == "not_claimable"
    assert result["reason"] == "missing_issue"
    assert result["run"] is None
    assert registry.list_workflow_runs() == []


def test_auto_scan_leaves_no_durable_run_for_a_workstream_without_issue(
    tmp_path: Path,
) -> None:
    """整條 auto-claim scan 走完，registry 與 delivery journal 皆零殘留。"""

    snapshot = _snapshot(tmp_path / "work" / "snapshot.json")
    state = tmp_path / "work" / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")

    results = work_actions.run_auto_claim_scan(
        snapshot_path=snapshot,
        state_path=state,
        now=lambda: 200,
        workflow_registry=registry,
        workflow_starter=_refusing_starter,
    )

    assert [row["action"] for row in results] == ["not_claimable"]
    assert registry.list_workflow_runs() == []
    assert not state.exists()


# --- 2. 跳過必須可查詢 ---------------------------------------------------------


def test_missing_issue_records_a_queryable_not_claimable_entry(tmp_path: Path) -> None:
    """「不建 run」不得等於「靜默略過」：ledger 必須落一筆可查的紀錄。"""

    snapshot = _snapshot(tmp_path / "work" / "snapshot.json")
    state = tmp_path / "work" / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")

    result = work_actions._claim_action(
        args={"action": "start"},
        authority=_authority(snapshot),
        now_epoch=200,
        state_path=state,
        workflow_registry=registry,
        workflow_starter=_refusing_starter,
    )

    entries = not_claimable.list_entries(_ledger(state))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["repo"] == "acme/demo"
    assert entry["work_id"] == "cost-governance-cluster"
    assert entry["reason"] == "missing_issue"
    assert entry["stale_run_id"] is None
    assert entry["observations"] == 1
    assert entry["first_observed_at"] == entry["last_observed_at"]
    # 下一步必須具體到可以照抄，否則 operator 只知道「被跳過了」。
    assert "cortex work link" in entry["next_step_hint"]
    assert result["not_claimable"] == entry

    on_disk = json.loads(_ledger(state).read_text(encoding="utf-8"))
    assert on_disk["schema"] == not_claimable.LEDGER_SCHEMA
    assert list(on_disk["items"]) == ["acme/demo::cost-governance-cluster"]


def test_repeated_scans_track_observations_without_multiplying_rows(tmp_path: Path) -> None:
    """每個 tick 都會再判一次；ledger 不得因此每輪長出一列。"""

    snapshot = _snapshot(tmp_path / "work" / "snapshot.json")
    state = tmp_path / "work" / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")

    for _ in range(3):
        work_actions._claim_action(
            args={"action": "auto-scan"},
            authority=_authority(snapshot),
            now_epoch=200,
            state_path=state,
            automatic=True,
            auto_label=True,
            workflow_registry=registry,
            workflow_starter=_refusing_starter,
        )

    entries = not_claimable.list_entries(_ledger(state))
    assert len(entries) == 1
    assert entries[0]["observations"] == 3
    # 第一次觀測時間必須保留——operator 靠它判斷「這件事卡多久了」。
    assert entries[0]["first_observed_at"] <= entries[0]["last_observed_at"]


def test_status_snapshot_exposes_the_not_claimable_ledger(tmp_path: Path) -> None:
    """`cortex status` 必須看得到；否則 ledger 只是另一個沒人會看的角落。"""

    snapshot = _snapshot(tmp_path / "work" / "snapshot.json")
    state = tmp_path / "work" / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")
    work_actions._claim_action(
        args={"action": "start"},
        authority=_authority(snapshot),
        now_epoch=200,
        state_path=state,
        workflow_registry=registry,
        workflow_starter=_refusing_starter,
    )

    provider = manager_daemon.build_runtime_status_provider(
        registry=registry,
        specs_dir=str(tmp_path / "specs"),
        handoff_dir=str(tmp_path / "handoff"),
        scan_specs_fn=lambda _: [],
        ready_units_fn=lambda metas, predicate: [],
    )
    payload = provider()

    # attention 只留可行動的項目——不可 claim 的 work item 不得再污染它。
    assert payload["attention"] == []
    assert [row["work_id"] for row in payload["not_claimable"]] == [
        "cost-governance-cluster"
    ]
    assert payload["not_claimable"][0]["reason"] == "missing_issue"


def test_status_text_mode_prints_the_not_claimable_reason(capsys) -> None:
    from paulsha_cortex.porcelain import inspect as porcelain_inspect

    porcelain_inspect._print_status(
        {
            "updated_at": "2026-08-18T00:00:00Z",
            "degraded": False,
            "attention": [],
            "not_claimable": [
                {
                    "repo": "acme/demo",
                    "work_id": "cost-governance-cluster",
                    "reason": "missing_issue",
                    "detail": "claim 判定 work item 目前不可 claim，且刻意不建立 run：missing_issue",
                    "first_observed_at": "2026-08-18T00:00:00+00:00",
                    "observations": 7,
                    "next_step_hint": "cortex work link cost-governance-cluster --repo acme/demo --issue <N>",
                }
            ],
        }
    )

    out = capsys.readouterr().out
    assert "not_claimable[acme/demo/cost-governance-cluster]: missing_issue" in out
    assert "next: cortex work link" in out


# --- 3. 正常路徑不得被關掉 -----------------------------------------------------


def test_work_item_with_an_issue_still_starts_a_run(tmp_path: Path) -> None:
    """為了消噪音而把可 claim 的項目一併擋掉，才是真正嚴重的迴歸。"""

    snapshot = _snapshot(
        tmp_path / "work" / "snapshot.json",
        work_id="fix-claim-provider-scope",
        issues=(12,),
        source_revisions=("issue:12@open", "openspec:fix-claim-provider-scope@1"),
    )
    state = tmp_path / "work" / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")

    result = work_actions._claim_action(
        args={"action": "start"},
        authority=_authority(snapshot, work_id="fix-claim-provider-scope"),
        now_epoch=200,
        state_path=state,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
    )

    assert result["action"] == "claim"
    assert result["run"]["status"] == "ongoing"
    assert len(registry.list_workflow_runs()) == 1
    assert not_claimable.list_entries(_ledger(state)) == []


def test_ledger_entry_is_cleared_once_the_issue_appears(tmp_path: Path) -> None:
    """work item 補上 issue 之後，`not-claimable` 那筆必須自動消失。

    否則修法只是把「永久的假 needs_human run」換成「永久的假 not-claimable 紀錄」。
    """

    root = tmp_path / "work"
    snapshot = _snapshot(root / "snapshot.json")
    state = root / "runs.json"
    registry = JobRegistry(state_path=state.parent / "jobs.json")
    work_actions._claim_action(
        args={"action": "start"},
        authority=_authority(snapshot),
        now_epoch=200,
        state_path=state,
        workflow_registry=registry,
        workflow_starter=_refusing_starter,
    )
    assert len(not_claimable.list_entries(_ledger(state))) == 1

    _snapshot(
        snapshot,
        issues=(208,),
        source_revisions=("issue:208@open", "openspec:cost-governance-cluster@1"),
    )
    result = work_actions._claim_action(
        args={"action": "start"},
        authority=_authority(snapshot),
        now_epoch=200,
        state_path=state,
        workflow_registry=registry,
        workflow_starter=work_actions._fallback_workflow_starter(registry, state),
    )

    assert result["action"] == "claim"
    assert not_claimable.list_entries(_ledger(state)) == []


# --- 4. 既有殭屍 run 的收口 ----------------------------------------------------


def _zombie_run(authority, *, run_id: str = "workflow-" + "c" * 20):
    """#669 修正前 `work_bridge.start_canonical_workflow` 會建出來的那種 run。"""

    return SimpleNamespace(
        run_id=run_id,
        repo=authority.repo,
        work_id=authority.work_id,
        claim_key=work_actions._expected_claim_key(authority),
        status="ongoing",
        current_phase="claim",
        facets=("needs_human",),
        gate_status="running",
        issue_refs=(),
        openspec_refs=authority.mapped_openspec,
        pr_refs=(),
        evidence_refs=(),
        source_revision=work_actions.work_authority_digest(authority),
        needs_human_reason=diagnostic_reason(
            "claim-blocked",
            "claim 判定需要人工介入即建立 run：missing_issue",
            source="work_bridge.start_workflow_for_authority",
            work_id=authority.work_id,
            repo=authority.repo,
        ).to_dict(),
        to_dict=lambda: {"run_id": run_id, "current_phase": "claim"},
    )


def test_legacy_claim_blocked_run_surfaces_with_an_exact_cleanup_command(
    tmp_path: Path,
) -> None:
    """修正前建立的 24 個殭屍 run 仍在 durable state 裡。

    修正不會自行清除它們（`#373`：auto-claim 不得自動清除或重試 needs_human
    run），但**必須讓 operator 查得到、且拿得到可照抄的清理指令**——否則修完之後
    那批 run 依舊在 attention 裡卡著，只是沒人知道該怎麼收。
    """

    snapshot = _snapshot(tmp_path / "work" / "snapshot.json")
    state = tmp_path / "work" / "runs.json"
    authority = _authority(snapshot)
    zombie = _zombie_run(authority)
    registry = SimpleNamespace(list_workflow_runs=lambda: [zombie])

    result = work_actions._claim_action(
        args={"action": "auto-scan"},
        authority=authority,
        now_epoch=200,
        state_path=state,
        automatic=True,
        auto_label=True,
        workflow_registry=registry,
        workflow_starter=_refusing_starter,
    )

    assert result["action"] == "not_claimable"
    assert result["reason"] == "claim-blocked-stale-run"
    assert result["stale_run_id"] == zombie.run_id
    assert result["legal_next_steps"] == ("abandon",)
    hint = result["next_step_hint"]
    assert f"cortex work abandon {authority.work_id}" in hint
    assert f"--expected-run-id {zombie.run_id}" in hint

    entry = not_claimable.list_entries(_ledger(state))[0]
    assert entry["reason"] == "claim-blocked-stale-run"
    assert entry["stale_run_id"] == zombie.run_id


def test_a_genuinely_stuck_run_is_not_mistaken_for_a_claim_blocked_zombie(
    tmp_path: Path,
) -> None:
    """判準必須窄到不會誤傷：停在 build 的 needs_human run 握有真正的工作成果，
    不得被重新分類成「不可 claim、可直接清掉」。"""

    snapshot = _snapshot(
        tmp_path / "work" / "snapshot.json",
        work_id="fix-claim-provider-scope",
        issues=(12,),
        source_revisions=("issue:12@open", "openspec:fix-claim-provider-scope@1"),
    )
    state = tmp_path / "work" / "runs.json"
    authority = _authority(snapshot, work_id="fix-claim-provider-scope")
    stuck = SimpleNamespace(
        run_id="workflow-" + "d" * 20,
        repo=authority.repo,
        work_id=authority.work_id,
        claim_key=work_actions._expected_claim_key(authority),
        status="ongoing",
        current_phase="build",
        facets=("needs_human",),
        gate_status="running",
        steps=(),
        issue_refs=("acme/demo#12",),
        openspec_refs=authority.mapped_openspec,
        pr_refs=(),
        evidence_refs=("/tmp/evidence/build.json",),
        source_revision=work_actions.work_authority_digest(authority),
        needs_human_reason=diagnostic_reason(
            "resume-workflow-failed",
            "ValueError: worktree target already exists",
            source="manager_daemon.periodic_tick:resume-workflow",
        ).to_dict(),
        to_dict=lambda: {"run_id": "workflow-" + "d" * 20, "current_phase": "build"},
    )
    registry = SimpleNamespace(list_workflow_runs=lambda: [stuck], list_jobs=lambda: [])

    result = work_actions._claim_action(
        args={"action": "auto-scan"},
        authority=authority,
        now_epoch=200,
        state_path=state,
        automatic=True,
        auto_label=True,
        workflow_registry=registry,
        workflow_starter=_refusing_starter,
    )

    assert result["action"] == "needs_human"
    assert result["reason"] == "human-intervention-required"
    assert result["run"]["run_id"] == stuck.run_id
    assert not_claimable.list_entries(_ledger(state)) == []


def test_claim_blocked_signature_requires_every_marker(tmp_path: Path) -> None:
    """`_claim_blocked_stale_run` 的判準逐項為必要條件——少任何一項都不算殭屍。"""

    snapshot = _snapshot(tmp_path / "work" / "snapshot.json")
    authority = _authority(snapshot)
    zombie = _zombie_run(authority)
    assert work_actions._claim_blocked_stale_run(zombie) is zombie

    for field, value in (
        ("status", "superseded"),
        ("current_phase", "define"),
        ("facets", ()),
        ("evidence_refs", ("/tmp/evidence/x.json",)),
        ("pr_refs", ("acme/demo#9",)),
        (
            "needs_human_reason",
            diagnostic_reason(
                "claim-blocked",
                "另一支來源寫的",
                source="manager.some_other_path",
            ).to_dict(),
        ),
    ):
        mutated = _zombie_run(authority)
        setattr(mutated, field, value)
        assert work_actions._claim_blocked_stale_run(mutated) is None, field


# --- ledger 本身的耐久性契約 ---------------------------------------------------


def test_ledger_is_fail_closed_on_corruption_but_status_stays_alive(tmp_path: Path) -> None:
    path = tmp_path / not_claimable.LEDGER_FILENAME
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(ValueError, match="not-claimable ledger unreadable"):
        not_claimable.load_ledger(path)
    # 呈現面不得因為一份輔助紀錄壞掉就整份 status 死掉。
    assert not_claimable.list_entries(path) == []


def test_clear_is_a_no_op_when_nothing_was_recorded(tmp_path: Path) -> None:
    path = tmp_path / not_claimable.LEDGER_FILENAME
    assert not_claimable.clear(path, repo="acme/demo", work_id="demo") is False
    assert not path.exists()
