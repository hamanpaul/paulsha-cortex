"""R0.5 D1：auto label 走鏡像的三段鏈路（monitor 捕捉 → snapshot → claim 導出）。

先前：canonical 路徑的 `WorkAuthority.auto_label` 硬編 `False`（claim.py），auto-claim
scan 因此每 tick 對每個 mapped issue 各發一次 live `gh api` 讀 label——實測 57 次/tick，
是 fleet 對 GitHub 最大的持續壓力源（#506），且 `GitHubPressureGate` 管不到 coordinator。

D1 之後：`GitHubWorkProvider` 把持有 `cortex:auto-on-going` 的 open issue 編號寫進
provider `observations["auto_label_issues"]`（issues 回應本來就含 labels，零額外 API）；
`_authority_from_canonical_row` 據此導出 `auto_label`；scan 只對鏡像為 True 的 authority
做一次 targeted 複驗。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from paulsha_cortex.coordinator import claim as claim_module
from paulsha_cortex.coordinator.claim import load_work_authority
from paulsha_cortex.monitor.providers import (
    AUTO_CLAIM_LABEL,
    GitHubWorkProvider,
)
from paulsha_cortex import doctor as doctor_module


# ---------------------------------------------------------------------------
# 常數對齊：三份複本不得漂移
# ---------------------------------------------------------------------------


def test_auto_label_constant_alignment() -> None:
    assert AUTO_CLAIM_LABEL == claim_module.AUTO_LABEL == doctor_module.AUTO_LABEL


# ---------------------------------------------------------------------------
# monitor：GitHubWorkProvider 捕捉 label observation
# ---------------------------------------------------------------------------


def _issue(number: int, *, state: str = "open", labels: list[str] | None = None,
           pull_request: bool = False) -> dict:
    entity: dict = {
        "number": number,
        "title": f"issue {number}",
        "state": state,
        "node_id": f"NODE{number}",
        "updated_at": "2026-08-15T00:00:00Z",
        "labels": [{"name": name} for name in (labels or [])],
    }
    if pull_request:
        entity["pull_request"] = {"url": "x"}
    return entity


def _issues_response(*entities: dict) -> str:
    """D3：issues 讀取改走 `gh api --include`（狀態行 + header + JSON 陣列）。"""
    return 'HTTP/2.0 200 OK\nEtag: W/"d1"\r\n\r\n' + json.dumps(list(entities))


class _Runner:
    def __init__(self, stdout: str):
        self._stdout = stdout

    def run(self, argv, timeout):  # noqa: ARG002 - 契約簽章
        return SimpleNamespace(returncode=0, stdout=self._stdout, stderr="")


def test_work_provider_records_auto_label_issue_numbers() -> None:
    stdout = _issues_response(
        _issue(7, labels=[AUTO_CLAIM_LABEL, "bug"]),
        _issue(9, labels=["bug"]),
        # closed 不參與 auto 派工
        _issue(11, state="closed", labels=[AUTO_CLAIM_LABEL]),
        # PR 不參與
        _issue(13, labels=[AUTO_CLAIM_LABEL], pull_request=True),
        _issue(21, labels=[AUTO_CLAIM_LABEL]),
    )
    snapshot = GitHubWorkProvider("acme/demo", runner=_Runner(stdout)).scan()
    assert snapshot.status == "ok"
    assert snapshot.observations["auto_label_issues"] == [7, 21]


def test_work_provider_malformed_labels_fail_closed() -> None:
    entity = _issue(7)
    entity["labels"] = "not-a-list"
    snapshot = GitHubWorkProvider(
        "acme/demo", runner=_Runner(_issues_response(entity))
    ).scan()
    assert snapshot.status == "degraded"
    assert "malformed" in snapshot.diagnostics[0]


# ---------------------------------------------------------------------------
# claim：canonical 路徑由 observations 導出 auto_label
# ---------------------------------------------------------------------------


def _canonical_snapshot(path: Path, *, observations: object) -> Path:
    provider: dict = {
        "status": "ok",
        "revision": "gh-rev-1",
        "last_success_at": "2026-08-15T00:00:00Z",
    }
    if observations is not None:
        provider["observations"] = observations
    path.write_text(
        json.dumps(
            {
                "schema": "work-items-snapshot/v1",
                "providers": {"github:acme/demo": provider},
                "work_items": [
                    {
                        "repo": "acme/demo",
                        "work_id": "auto-work",
                        "sources": [
                            {
                                "confidence": "confirmed",
                                "kind": "github_issue",
                                "ref": "acme/demo#7",
                                "source_id": "github_issue:acme/demo#7",
                                "revision": "github:NODE7:2026-08-15T00:00:00Z",
                                "status": "open",
                                "provider": "github:acme/demo",
                            },
                            {
                                "confidence": "confirmed",
                                "kind": "todo",
                                "ref": "docs/superpowers/workstreams/auto-work/todo.md",
                                "source_id": "todo:acme/demo:docs/x",
                                "revision": "local-sha256:abc",
                                "status": "active",
                                "provider": "repo:acme/demo",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_canonical_auto_label_derived_from_observations(tmp_path: Path) -> None:
    snapshot = _canonical_snapshot(
        tmp_path / "s.json", observations={"auto_label_issues": [7, 42]}
    )
    authority = load_work_authority(
        repo="acme/demo", work_id="auto-work", snapshot_path=snapshot
    )
    assert authority.auto_label is True


def test_canonical_auto_label_false_when_issue_not_listed(tmp_path: Path) -> None:
    snapshot = _canonical_snapshot(
        tmp_path / "s.json", observations={"auto_label_issues": [42]}
    )
    authority = load_work_authority(
        repo="acme/demo", work_id="auto-work", snapshot_path=snapshot
    )
    assert authority.auto_label is False


def test_canonical_auto_label_conservative_on_missing_or_malformed(
    tmp_path: Path,
) -> None:
    for observations in (None, {}, {"auto_label_issues": "x"},
                         {"auto_label_issues": [7, "x"]}, "not-a-dict"):
        snapshot = _canonical_snapshot(tmp_path / "s.json", observations=observations)
        authority = load_work_authority(
            repo="acme/demo", work_id="auto-work", snapshot_path=snapshot
        )
        assert authority.auto_label is False, f"observations={observations!r} 應保守 False"
