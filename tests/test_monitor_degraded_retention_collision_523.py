"""`#523`：degraded 保留分支的 ownership collision 與其永不自癒的死鎖。

## 實測現場（2026-08-14）

在 `.cortex/work-items.yaml` 新增三筆 registry 條目（把 `github_issue` source 由
fallback work item 轉交給新宣告的 work item）之後，monitor 每一輪都拋：

```
ValueError: ownership collision for github_issue:hamanpaul/paulsha-cortex#507:
  hamanpaul/paulsha-cortex::fix-planning-rollback-destroys-operator-work,
  hamanpaul/paulsha-cortex::issue:hamanpaul/paulsha-cortex#507
```

全部 52 個 provider 的 `last_attempt_at` 同時凍在同一個時刻（含根本不碰 GitHub 的
`repo:`／`workflow:` provider），而 `work-items.snapshot.json` 的 mtime 仍每 30 秒更新
——外觀完全正常，與單純限流難以區分。

## 兩個獨立缺陷

1. **保留分支只比對 work_id，不比對 sources**（`lifecycle.py`）。source 歸屬從
   `correlation.py:_fallback_work_id` 產生的 `issue:<ref>` 轉移到新宣告的 work item 時，
   本輪 correlation 不再產生該 fallback → 它「不在 projected_ids 裡」→ 連同**舊的
   sources** 被整筆放回 → 兩個 work item 宣稱擁有同一個 source。

2. **projection 失敗會連帶丟棄 provider 觀測**（`work_api.py`）。例外發生在
   `WorkSnapshot.__post_init__`、早於 `replace_durably()`，於是那一輪算出的
   provider 新狀態（包含「backoff 已結束」）一併被丟棄，`previous` 永遠停在崩潰前
   那一版、`degraded` 永遠為真 → 下一輪以相同輸入重演 → **provider 無法離開
   degraded，因為記錄它恢復的那次寫入正是拋例外的那次寫入**。

先前把成因誤判為「時序競態」（unlink → recover → re-link 就好）；那次實驗會成功只是
因為當下 provider 恰好健康、保留分支根本沒跑。真正的觸發條件是 `correlation.degraded`。
"""

from __future__ import annotations

from paulsha_cortex.monitor.correlation import CorrelationResult, CorrelatedWork
from paulsha_cortex.monitor.lifecycle import project_work_items
from paulsha_cortex.monitor.work_models import WorkItem, WorkSource
from paulsha_cortex.monitor.work_snapshot import WorkSnapshot


def _issue_source() -> WorkSource:
    return WorkSource(
        source_id="github_issue:example/acme#507",
        kind="github_issue",
        ref="example/acme#507",
        revision="github:NODE:2026-08-14T00:00:00Z",
        status="open",
        confidence="confirmed",
        provider="github:example/acme",
    )


def _degraded_correlation_owning_the_issue() -> CorrelationResult:
    """本輪：issue 已改由宣告的 work item 持有，fallback 不再產生。"""

    source = _issue_source()
    return CorrelationResult(
        groups=(
            CorrelatedWork(
                work_id="declared-work",
                title="Declared Work",
                sources=(source,),
                confidence="confirmed",
            ),
        ),
        source_owners={source.source_id: "declared-work"},
        exclusions=(),
        explanations={},
        degraded=True,
        diagnostics=("github rate limit backoff active; retry in 64s",),
    )


def _previous_fallback_item() -> WorkItem:
    """上一版：同一個 issue 由 fallback work item `issue:<ref>` 持有。"""

    return WorkItem(
        work_id="issue:example/acme#507",
        repo="example/acme",
        title="example/acme#507",
        state="topic",
        phase=None,
        facets=(),
        sources=(_issue_source(),),
        next_actions=(),
        workflow_run_id=None,
        updated_at="2026-08-14T08:00:00Z",
    )


def test_degraded_retention_does_not_duplicate_a_reowned_source() -> None:
    """歸屬轉移的 source 不得同時出現在新舊兩個 work item。"""

    projection = project_work_items(
        _degraded_correlation_owning_the_issue(),
        repo="example/acme",
        updated_at="2026-08-14T09:00:00Z",
        previous_items=(_previous_fallback_item(),),
    )

    owners = {
        source.source_id: item.work_id
        for item in projection.items
        for source in item.sources
    }
    assert owners == {"github_issue:example/acme#507": "declared-work"}

    # 舊 fallback 的 source 已完整轉移，整筆丟棄而非留下空殼。
    assert [item.work_id for item in projection.items] == ["declared-work"]


def test_projection_result_passes_ownership_validation() -> None:
    """端到端：`WorkSnapshot` 建構不得因這個 projection 而 raise。

    這是 `#523` 真正致命的那一步——`validate_ownership()` 在
    `__post_init__` 拋出，整個 refresh 隨之失敗。
    """

    projection = project_work_items(
        _degraded_correlation_owning_the_issue(),
        repo="example/acme",
        updated_at="2026-08-14T09:00:00Z",
        previous_items=(_previous_fallback_item(),),
    )

    snapshot = WorkSnapshot(
        sequence=2,
        written_at="2026-08-14T09:00:00Z",
        providers={},
        work_items=projection.items,
        source_owners={"github_issue:example/acme#507": "example/acme::declared-work"},
        exclusions=(),
    )
    assert len(snapshot.work_items) == 1


def test_partially_reowned_previous_item_keeps_its_remaining_sources() -> None:
    """只有部分 source 被轉移時，舊 work item 保留其餘 source 而非整筆消失。"""

    moved = _issue_source()
    kept = WorkSource(
        source_id="todo:example/acme:docs/other/todo.md",
        kind="todo",
        ref="docs/other/todo.md",
        revision="local-sha256:beef",
        status="active",
        confidence="confirmed",
        provider="repo:example/acme",
    )
    previous = WorkItem(
        work_id="issue:example/acme#507",
        repo="example/acme",
        title="example/acme#507",
        state="topic",
        phase=None,
        facets=(),
        sources=(moved, kept),
        next_actions=(),
        workflow_run_id=None,
        updated_at="2026-08-14T08:00:00Z",
    )

    projection = project_work_items(
        _degraded_correlation_owning_the_issue(),
        repo="example/acme",
        updated_at="2026-08-14T09:00:00Z",
        previous_items=(previous,),
    )

    by_id = {item.work_id: item for item in projection.items}
    assert set(by_id) == {"declared-work", "issue:example/acme#507"}
    assert [s.source_id for s in by_id["issue:example/acme#507"].sources] == [kept.source_id]


def test_previous_item_without_sources_is_still_retained() -> None:
    """原本就沒有 source 的 previous item 維持既有保留語意。

    丟棄條件是「原本有 source、且全部被本輪認領」，不是「現在沒有 source」——
    否則會誤傷僅由 workflow_run 衍生的項目。
    """

    sourceless = WorkItem(
        work_id="sourceless-work",
        repo="example/acme",
        title="Sourceless",
        state="done",
        phase=None,
        facets=(),
        sources=(),
        next_actions=(),
        workflow_run_id=None,
        updated_at="2026-08-14T08:00:00Z",
    )

    projection = project_work_items(
        _degraded_correlation_owning_the_issue(),
        repo="example/acme",
        updated_at="2026-08-14T09:00:00Z",
        previous_items=(sourceless,),
    )

    assert "sourceless-work" in {item.work_id for item in projection.items}


def test_retention_filter_is_inert_when_not_degraded() -> None:
    """非 degraded 時保留分支本來就不跑，行為不得因本修正改變。"""

    correlation = _degraded_correlation_owning_the_issue()
    healthy = CorrelationResult(
        groups=correlation.groups,
        source_owners=correlation.source_owners,
        exclusions=(),
        explanations={},
        degraded=False,
        diagnostics=(),
    )

    projection = project_work_items(
        healthy,
        repo="example/acme",
        updated_at="2026-08-14T09:00:00Z",
        previous_items=(_previous_fallback_item(),),
    )

    assert [item.work_id for item in projection.items] == ["declared-work"]


# ---------------------------------------------------------------------------
# 第二個缺陷：projection 失敗不得連帶丟棄 provider 觀測（否則永不自癒）
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from paulsha_cortex.monitor.models import ProjectState  # noqa: E402
from paulsha_cortex.monitor.work_api import (  # noqa: E402
    WorkModelRefresher,
    WorkReadModelStore,
)
from paulsha_cortex.monitor.work_models import ProviderSnapshot  # noqa: E402
from paulsha_cortex.monitor.work_snapshot import WorkSnapshotStore  # noqa: E402

_NOW = "2026-08-14T09:00:00Z"


class _StaticProvider:
    def __init__(self, snapshot: ProviderSnapshot):
        self.snapshot = snapshot

    def scan(self) -> ProviderSnapshot:
        return self.snapshot


def _ok_provider(provider_id: str) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider_id=provider_id,
        status="ok",
        last_attempt_at=_NOW,
        last_success_at=_NOW,
        revision="revision:recovered",
        diagnostics=(),
        sources=(),
        observations={},
    )


def _colliding_projection(*_args, **_kwargs):
    """模擬任何導致 ownership collision 的 projection 結果。

    本 PR 已修掉已知成因（保留分支），但死鎖性質本身是獨立缺陷：只要**任何**
    collision 發生，`WorkSnapshot.__post_init__` 就會在 `replace_durably()` 之前
    拋出，把該輪的 provider 觀測一併丟掉，使 provider 永遠離不開 degraded。
    """

    from paulsha_cortex.monitor.lifecycle import LifecycleProjection

    duplicated = _issue_source()
    return LifecycleProjection(
        items=(
            WorkItem(
                work_id="alpha",
                repo="example/acme",
                title="Alpha",
                state="topic",
                phase=None,
                facets=(),
                sources=(duplicated,),
                next_actions=(),
                workflow_run_id=None,
                updated_at=_NOW,
            ),
            WorkItem(
                work_id="beta",
                repo="example/acme",
                title="Beta",
                state="topic",
                phase=None,
                facets=(),
                sources=(duplicated,),
                next_actions=(),
                workflow_run_id=None,
                updated_at=_NOW,
            ),
        ),
        explanations={},
    )


def test_projection_failure_does_not_discard_provider_observations(
    tmp_path, monkeypatch
) -> None:
    """refresh 不得因 projection 失敗而整輪拋出、丟棄 provider 進展。

    這是 `#523` 之所以「永不自癒」的機制：provider 恢復的事實與壞掉的 projection
    寫在同一次 `replace_durably()`，於是 provider 無法離開 degraded——記錄它恢復的
    那次寫入正是拋例外的那次寫入。
    """

    monkeypatch.setattr(
        "paulsha_cortex.monitor.work_api.project_work_items", _colliding_projection
    )
    refresher = WorkModelRefresher(
        durable_store=WorkSnapshotStore(tmp_path / "snapshot.json"),
        read_store=WorkReadModelStore.empty(),
        github_provider_factory=lambda _repo: _StaticProvider(
            _ok_provider("github:example/acme")
        ),
        github_terminal_provider_factory=lambda _repo, **_kw: _StaticProvider(
            _ok_provider("github-terminal:example/acme")
        ),
        workflow_provider_factory=lambda _repo: _StaticProvider(
            _ok_provider("workflow:example/acme")
        ),
        now=lambda: datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc),
    )

    # 不得 raise。
    refresher.refresh(
        (
            ProjectState(
                project_id="example/acme", workspace="ws", path=str(tmp_path)
            ),
        ),
        include_github=True,
    )

    snapshot = refresher.read_store.current_snapshot()
    # provider 觀測已落地（這正是先前被丟棄的東西）。
    assert snapshot.providers, "provider 觀測必須落地，否則 degraded 永遠解不開"
    # 失敗原因對 operator 可見，不是無聲降級。
    assert any(
        "work model projection retained" in diagnostic
        for provider in snapshot.providers.values()
        for diagnostic in provider.diagnostics
    )
