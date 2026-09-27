"""Issue #1106：整合 PR 透過 ``github_closing`` 把已有 override owner 的 issue

拖進整合 PR 的群組，造成 ``confirmed source collision``、repo 全域 degraded。

## 根因回顧

``GitHubTerminalWorkProvider.scan()``（``paulsha_cortex/monitor/providers.py``）
替每個 PR 產生的 ``closing_links`` 形狀是：把 PR 關閉的「第一張票」當作
*primary*，PR 本身與其餘每一張被關閉的票，全部指向這張 primary 票的
``source_id``（``links[pr] = primary``、``links[issueN] = primary``，
``N >= 1``）。

``correlate_work_sources()`` 處理 ``github_closing`` 時，會把 primary 票
目前的 owner（若尚無 owner 則現場造一個 ``issue:<ref>`` fallback 群組）
無條件套用到 mapping 裡每一個指向它的來源──包含那些「其餘票」。當某張
其餘票早就有自己的 override／frontmatter owner 時，它就同時落在兩個群組
裡，變成 ``confirmed source collision``，進而讓整個 repo 被標
``degraded``（hard gate ``auto_claim``／``merge`` 全關）。

live 案例：整合 PR ``hamanpaul/paulsha-cortex#1087``（歸屬
``retry-build-preserve-proof``，primary 票 ``#479``）與
``hamanpaul/paulsha-cortex#1090``（歸屬 ``wave4-large-bug-integration``，
primary 票 ``#475`` 本身無 override 故走 fallback）各自關閉了數張已有
override owner 的票，目前靠 ``.cortex/work-items.yaml`` 的 ``excludes``
逐一排除才沒有 degraded。

## 採用的規則

``github_closing`` 只是「同一個整合 PR 把這些票綁在一起」的推論式關聯，
權威性低於 override／frontmatter 明列的歸屬。修法：在套用 primary 票的
owner 之前，先檢查「這個來源自己是否已經有一個不同的權威 owner」；有的話
維持它自己的歸屬，不套用 primary 的 owner（也就不會製造 collision）。

PR 本身的歸屬因此是確定性的：
- PR 沒有自己的 override／frontmatter owner → 加入 primary 票目前的 owner
  群組（若 primary 票本身也沒有 owner，兩者一起落入 primary 票的
  ``issue:<ref>`` fallback 群組）。
- PR 自己已有 override owner → 維持自己的歸屬，不因為 closing 關聯被拖走。

真正的衝突（同一個 source 被兩個 override 明列，或同一個 source 同時被
override 與 frontmatter 指向不同群組）不受本次修法影響，仍會回報
collision、仍會 degraded。
"""
from __future__ import annotations

from pathlib import Path

from paulsha_cortex.monitor.correlation import (
    _fallback_work_id,
    correlate_work_sources,
)
from paulsha_cortex.monitor.work_models import WorkSource

_GENERIC_REPO = "example/acme"
_REAL_REPO = "hamanpaul/paulsha-cortex"
# tests/ 直接在 repo root 之下：用實際 checkout 的 .cortex/work-items.yaml，
# 才能驗證新規則對現行資料仍相容（compatibility 驗收）。
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _issue(number: int, *, repo: str = _GENERIC_REPO) -> WorkSource:
    ref = f"{repo}#{number}"
    return WorkSource(
        source_id=f"github_issue:{ref}",
        kind="github_issue",
        ref=ref,
        revision="rev-1",
        status="open",
        confidence="confirmed",
        provider=f"github:{repo}",
    )


def _pull(number: int, *, repo: str = _GENERIC_REPO) -> WorkSource:
    ref = f"{repo}#{number}"
    return WorkSource(
        source_id=f"github_pr:{ref}",
        kind="github_pr",
        ref=ref,
        revision="rev-1",
        status="open",
        confidence="confirmed",
        provider=f"github:{repo}",
    )


def _closing_shape(pr: WorkSource, issues: tuple[WorkSource, ...]) -> dict[str, str]:
    """重建 ``GitHubTerminalWorkProvider.scan()`` 產生 ``closing_links`` 的形狀：

    PR 與除第一張之外的每一張被關閉票，全部指向第一張票（primary）的
    ``source_id``。
    """

    primary = issues[0]
    links = {pr.source_id: primary.source_id}
    for issue in issues[1:]:
        links[issue.source_id] = primary.source_id
    return links


def test_integration_pr_closing_owned_issues_does_not_collide(tmp_path):
    """整合 PR 關閉 3 張各自已有 override owner 的票：不得 collision，各自維持自己的歸屬。"""

    primary = _issue(100)
    secondary_b = _issue(101)
    secondary_c = _issue(102)
    pull = _pull(200)
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """version: 1
work_items:
  team-a:
    title: Team A
    links: [{kind: github_issue, ref: example/acme#100}]
    excludes: []
  team-b:
    title: Team B
    links: [{kind: github_issue, ref: example/acme#101}]
    excludes: []
  team-c:
    title: Team C
    links: [{kind: github_issue, ref: example/acme#102}]
    excludes: []
""",
    )

    result = correlate_work_sources(
        tmp_path,
        _GENERIC_REPO,
        (pull, primary, secondary_b, secondary_c),
        closing_links=_closing_shape(pull, (primary, secondary_b, secondary_c)),
    )

    assert result.diagnostics == ()
    assert not result.degraded
    assert result.source_owners[primary.source_id] == "team-a"
    assert result.source_owners[secondary_b.source_id] == "team-b"
    assert result.source_owners[secondary_c.source_id] == "team-c"
    # PR 沒有自己的 override，歸屬 primary 票目前的 owner——確定性、可解釋。
    assert result.source_owners[pull.source_id] == "team-a"


def test_integration_pr_closing_fallback_primary_without_owner(tmp_path):
    """primary 票沒有 override（走 fallback）：PR 與 primary 一起落 fallback 群組，
    其他已有 owner 的票維持自己的歸屬，不 collision。"""

    primary = _issue(300)
    secondary = _issue(301)
    pull = _pull(400)
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """version: 1
work_items:
  team-b:
    title: Team B
    links: [{kind: github_issue, ref: example/acme#301}]
    excludes: []
""",
    )

    result = correlate_work_sources(
        tmp_path,
        _GENERIC_REPO,
        (pull, primary, secondary),
        closing_links=_closing_shape(pull, (primary, secondary)),
    )

    assert result.diagnostics == ()
    assert not result.degraded
    fallback_id = _fallback_work_id(primary)
    assert result.source_owners[primary.source_id] == fallback_id
    assert result.source_owners[pull.source_id] == fallback_id
    assert result.source_owners[secondary.source_id] == "team-b"


def test_integration_pr_with_own_override_keeps_it_over_closing_link(tmp_path):
    """PR 自己已有 override owner：closing 關聯不得把它拖去 primary 票的群組。"""

    primary = _issue(500)
    pull = _pull(600)
    _write(
        tmp_path / ".cortex/work-items.yaml",
        """version: 1
work_items:
  team-a:
    title: Team A
    links: [{kind: github_issue, ref: example/acme#500}]
    excludes: []
  team-x:
    title: Team X
    links: [{kind: github_pr, ref: example/acme#600}]
    excludes: []
""",
    )

    result = correlate_work_sources(
        tmp_path,
        _GENERIC_REPO,
        (pull, primary),
        closing_links=_closing_shape(pull, (primary,)),
    )

    assert result.diagnostics == ()
    assert not result.degraded
    assert result.source_owners[primary.source_id] == "team-a"
    assert result.source_owners[pull.source_id] == "team-x"


def test_true_collision_two_overrides_listing_same_issue_still_degrades(tmp_path):
    """真正的衝突：同一 issue 被兩個 override 明列——仍要回報 collision、仍 degraded。"""

    _write(
        tmp_path / ".cortex/work-items.yaml",
        """version: 1
work_items:
  team-a:
    title: Team A
    links: [{kind: github_issue, ref: example/acme#700}]
    excludes: []
  team-b:
    title: Team B
    links: [{kind: github_issue, ref: example/acme#700}]
    excludes: []
""",
    )

    result = correlate_work_sources(tmp_path, _GENERIC_REPO, ())

    assert result.degraded
    assert any("confirmed source collision" in message for message in result.diagnostics)


def test_true_collision_frontmatter_vs_override_still_degrades(tmp_path):
    """真正的衝突：同一來源被 frontmatter 與另一個 override 指向不同群組——
    與 github_closing 無關，本次修法不得連帶壓掉它。"""

    ref = "docs/superpowers/specs/collide.md"
    _write(tmp_path / ref, "---\nwork_item: frontmatter-owner\n---\n# Work\n")
    _write(
        tmp_path / ".cortex/work-items.yaml",
        f"""version: 1
work_items:
  override-owner:
    title: Override owner
    links: [{{kind: path, ref: {ref}}}]
    excludes: []
""",
    )
    source = WorkSource(
        source_id=f"superpowers_spec:{_GENERIC_REPO}:{ref}",
        kind="superpowers_spec",
        ref=ref,
        revision="rev-1",
        status="active",
        confidence="confirmed",
        provider=f"repo:{_GENERIC_REPO}",
    )

    result = correlate_work_sources(tmp_path, _GENERIC_REPO, (source,))

    assert result.degraded
    assert any("confirmed source collision" in message for message in result.diagnostics)


def test_real_work_items_yaml_pr_1087_1090_closing_shapes_do_not_collide():
    """用目前 repo 實際的 ``.cortex/work-items.yaml``，重建 PR #1087／#1090 的
    ``closing_links`` 形狀（github closingIssuesReferences 順序：primary=第一張票，
    詳見 ``retry-build-preserve-proof``／``wave4-large-bug-integration`` 的
    excludes 註解），驗證新規則下這批既有整合 PR 不再需要靠 excludes 才能避免
    collision——不引入新衝突（compatibility 驗收）。
    """

    pr_1087 = _pull(1087, repo=_REAL_REPO)
    issue_479 = _issue(479, repo=_REAL_REPO)  # primary；retry-build-preserve-proof 直接 override
    pr_1087_others = tuple(
        _issue(number, repo=_REAL_REPO)
        for number in (812, 862, 871, 874, 956, 961, 983)
    )

    pr_1090 = _pull(1090, repo=_REAL_REPO)
    issue_475 = _issue(475, repo=_REAL_REPO)  # primary；wave4-large-bug-integration 直接 override
    pr_1090_others = tuple(
        _issue(number, repo=_REAL_REPO)
        for number in (481, 492, 497, 579, 810, 821)
    )

    closing_links: dict[str, str] = {}
    closing_links.update(_closing_shape(pr_1087, (issue_479, *pr_1087_others)))
    closing_links.update(_closing_shape(pr_1090, (issue_475, *pr_1090_others)))

    sources = (
        pr_1087,
        issue_479,
        *pr_1087_others,
        pr_1090,
        issue_475,
        *pr_1090_others,
    )

    result = correlate_work_sources(
        _REPO_ROOT,
        _REAL_REPO,
        sources,
        closing_links=closing_links,
    )

    assert result.diagnostics == ()
    assert not result.degraded
    assert result.source_owners[pr_1087.source_id] == "retry-build-preserve-proof"
    assert result.source_owners[issue_479.source_id] == "retry-build-preserve-proof"
    assert result.source_owners[pr_1090.source_id] == "wave4-large-bug-integration"
    assert result.source_owners[issue_475.source_id] == "wave4-large-bug-integration"

    own_owner_by_number = {
        812: "planning-kind-bound-exact-match",
        862: "recovery-registry-receipt",
        871: "maintainer-fallback-authorization-v2",
        874: "reviewer-honest-stop-terminal",
        956: "review-gate-adjudication-exit",
        961: "openspec-remote-archive-authority-reconciliation",
        983: "delivery-journal-conditional-commit",
        481: "terminal-replay-manifest-fence",
        492: "fix-read-repo-tier-fail-closed",
        497: "fix-superseded-terminal-replay",
        579: "reviewer-sandbox-job-scoped-name",
        810: "closure-todo-advisory",
        821: "registry-persist-hygiene",
    }
    for issue in (*pr_1087_others, *pr_1090_others):
        number = int(issue.ref.rsplit("#", 1)[-1])
        assert result.source_owners[issue.source_id] == own_owner_by_number[number]
