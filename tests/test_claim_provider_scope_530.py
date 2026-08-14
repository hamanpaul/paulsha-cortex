"""`#530`：claim 的 GitHub provider 檢查必須 scope 到 work item 實際依賴的 provider。

背景（2026-08-14 實測）：GitHub REST 遭 abuse-detection 封鎖期間，**所有** work item
都無法 claim——包含權威完全來自本機檔案系統 provider（`repo:<owner>/<name>`）、
與 GitHub 毫無關係的 work item：

```
$ cortex work start fix-instance-config-isolation --repo hamanpaul/paulsha-cortex
錯誤: AuthorityValidationError: durable GitHub provider authority rate-limited
  (reason=provider-authority-rate-limited-canonical, provider_id=github:hamanpaul/paulsha-cortex)
```

該 work item 的 confirmed sources 只有一筆 `kind=todo`、`provider=repo:...`。
`_authority_from_canonical_row` 卻無條件要求 `github:<repo>` 為 `ok`，於是一次 GitHub
可用性事故被放大成整個 fleet 的派工全面停擺——連「修 GitHub 壓力問題」本身都做不了。

fail-closed 對**確實掛著 GitHub source** 的 work item 是必要的（provider 過時代表可能去做
一張已被關閉、或 label 已被移除的 issue），錯的是把它套在整個 repo 上。

本檔釘住三件事：
1. 純本機來源的 work item 在 GitHub provider degraded 時仍可取得 authority；
2. 有 GitHub 來源的 work item 維持既有的嚴格 fail-closed（不得因本修正而鬆掉）；
3. `provider` 欄位缺席時保守視為需要 GitHub 權威（資訊缺席 ≠ 正面證據）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paulsha_cortex.coordinator.claim import (
    PROVIDER_MAX_AGE_SECONDS,
    REASON_PROVIDER_RATE_LIMITED_CANONICAL,
    AuthorityValidationError,
    _authority_is_fresh,
    load_work_authority,
)

_DEGRADED_GITHUB = {
    "status": "degraded",
    "revision": "gh-rev-last-known-good",
    "last_success_at": "2026-08-14T08:59:31Z",
    "diagnostics": [
        "github rate limit backoff active; retry in 64s",
        "github:acme/demo stale",
    ],
}


def _snapshot(path: Path, *, sources: list[dict], providers: dict | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": providers or {"github:acme/demo": dict(_DEGRADED_GITHUB)},
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "local-only-work",
                        "sources": sources,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _local_todo_source() -> dict:
    return {
        "confidence": "confirmed",
        "kind": "todo",
        "ref": "docs/superpowers/workstreams/local-only-work/todo.md",
        "source_id": "todo:acme/demo:docs/superpowers/workstreams/local-only-work/todo.md",
        "revision": "local-sha256:abc123",
        "provider": "repo:acme/demo",
    }


def test_local_only_work_item_claimable_while_github_provider_degraded(
    tmp_path: Path,
) -> None:
    """本案的核心：權威全來自本機檔案的 work item 不得被 GitHub 狀態擋死。"""

    snapshot = _snapshot(tmp_path / "snapshot.json", sources=[_local_todo_source()])

    authority = load_work_authority(
        repo="acme/demo", work_id="local-only-work", snapshot_path=snapshot
    )

    assert authority.requires_github_authority is False
    assert authority.confirmed_todo is True
    assert authority.mapped_todo_paths == (
        "docs/superpowers/workstreams/local-only-work/todo.md",
    )
    # last-known-good 仍被沿用，authority 的識別欄位不因豁免而改變形狀。
    assert authority.github_provider_id == "github:acme/demo"
    assert authority.github_provider_revision == "gh-rev-last-known-good"


def test_local_only_authority_is_never_stale_by_github_clock(tmp_path: Path) -> None:
    """第二層放大：`_authority_is_fresh` 用 GitHub 的 last-success 當時鐘。

    只修 `_authority_from_canonical_row` 而不修這裡，claim 仍會在
    `decide_manual_start`／auto-claim 被 `authority-stale` 擋下。
    """

    snapshot = _snapshot(tmp_path / "snapshot.json", sources=[_local_todo_source()])
    authority = load_work_authority(
        repo="acme/demo", work_id="local-only-work", snapshot_path=snapshot
    )

    # GitHub 觀測已經舊了一整天，對純本機來源的 work item 不構成過期。
    ancient = authority.github_last_success_epoch + PROVIDER_MAX_AGE_SECONDS * 100
    assert _authority_is_fresh(authority, now_epoch=ancient) is True


def test_github_sourced_work_item_still_fails_closed(tmp_path: Path) -> None:
    """回歸防護：真的掛著 GitHub source 的 work item 不得因本修正而放行。"""

    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        sources=[
            _local_todo_source(),
            {
                "confidence": "confirmed",
                "kind": "github_issue",
                "ref": "acme/demo#42",
                "source_id": "github_issue:acme/demo#42",
                "revision": "github:NODE:2026-08-14T00:00:00Z",
                "provider": "github:acme/demo",
            },
        ],
    )

    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(
            repo="acme/demo", work_id="local-only-work", snapshot_path=snapshot
        )

    assert excinfo.value.reason_code == REASON_PROVIDER_RATE_LIMITED_CANONICAL
    assert excinfo.value.provider_id == "github:acme/demo"


def test_remote_todo_from_github_terminal_provider_still_fails_closed(
    tmp_path: Path,
) -> None:
    """`github-terminal:` 供應的 remote todo／openspec 也是 GitHub 來源。

    只看 `kind` 會漏掉它們（kind 是 `todo`／`openspec`，看不出來源），
    所以判準以 provider id 前綴為主。
    """

    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        sources=[
            {
                "confidence": "confirmed",
                "kind": "todo",
                "ref": "docs/superpowers/workstreams/remote-work/todo.md",
                "source_id": "todo:acme/demo:remote",
                "revision": "github-blob:deadbeef",
                "provider": "github-terminal:acme/demo",
            }
        ],
    )

    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(
            repo="acme/demo", work_id="local-only-work", snapshot_path=snapshot
        )

    assert excinfo.value.reason_code == REASON_PROVIDER_RATE_LIMITED_CANONICAL


def test_missing_provider_field_is_treated_conservatively(tmp_path: Path) -> None:
    """資訊缺席 ≠ 正面證據：沒宣告 `provider` 時維持嚴格 fail-closed。

    放寬只發生在 source 明確標示了非 GitHub provider 的情況。
    """

    source = _local_todo_source()
    del source["provider"]
    snapshot = _snapshot(tmp_path / "snapshot.json", sources=[source])

    with pytest.raises(AuthorityValidationError) as excinfo:
        load_work_authority(
            repo="acme/demo", work_id="local-only-work", snapshot_path=snapshot
        )

    assert excinfo.value.reason_code == REASON_PROVIDER_RATE_LIMITED_CANONICAL


def test_waiver_still_requires_last_known_good(tmp_path: Path) -> None:
    """豁免不是無條件放行：缺 revision／timestamp 仍嚴格拒絕。

    authority 的 `provider_revision`／`last_success_epoch` 是必要欄位，沒有
    last-known-good 就無法建構——那是真正的 authority 損毀，不是可用性問題。
    """

    snapshot = _snapshot(
        tmp_path / "snapshot.json",
        sources=[_local_todo_source()],
        providers={
            "github:acme/demo": {
                "status": "degraded",
                "revision": "",
                "last_success_at": "2026-08-14T08:59:31Z",
                "diagnostics": ["github rate limit backoff active; retry in 64s"],
            }
        },
    )

    with pytest.raises(AuthorityValidationError):
        load_work_authority(
            repo="acme/demo", work_id="local-only-work", snapshot_path=snapshot
        )
