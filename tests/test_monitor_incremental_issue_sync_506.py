"""#506 / D3：GitHub issues 增量同步（``state=all&since=`` ＋ ETag）的驗收鎖。

改動前：``GitHubWorkProvider`` 每輪對**每個** configured repo 全量分頁抓 issues，
configured repos 約 13 個，是 D2 之後 REST 配額剩下的主要常態消耗。

改動後的協定（契約全文見 ``monitor/github_issue_sync`` 模組 docstring）：

- 增量請求 ``issues?state=all&per_page=100&sort=updated&direction=desc&since=<游標>``
- 第 1 頁帶 ``If-None-Match``；304 不計 GitHub rate limit 配額
- 游標取自**回應**中最大的 ``updated_at``，只在整輪完整成功後推進，且永不倒退
- 每日一次不帶 ``since``／不帶 ETag 的全量 anti-entropy 對帳，drift 以全量為準
- 游標／ETag／鏡像損壞一律 fail closed 退回全量重建

本檔逐條鎖住計畫明文的六項驗收。
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.monitor.github_issue_sync import (
    IssueSyncStateError,
    IssueSyncStore,
    issues_request_path,
)
from paulsha_cortex.monitor.providers import AUTO_CLAIM_LABEL, GitHubWorkProvider


REPO = "acme/demo"
BASE_PATH = "repos/acme/demo/issues?state=all&per_page=100&sort=updated&direction=desc"


# ---------------------------------------------------------------------------
# 測試替身：mock 的是 HTTP 層（`gh api` 的輸出），不打真實 GitHub API
# ---------------------------------------------------------------------------


def issue(
    number: int,
    *,
    state: str = "open",
    updated_at: str = "2026-08-15T00:00:00Z",
    labels: tuple[str, ...] = (),
    title: str | None = None,
    pull_request: bool = False,
) -> dict:
    entity: dict = {
        "number": number,
        "title": title or f"issue {number}",
        "state": state,
        "node_id": f"NODE{number}",
        "updated_at": updated_at,
        "labels": [{"name": name} for name in labels],
    }
    if pull_request:
        entity["pull_request"] = {"url": f"https://api.github.test/pulls/{number}"}
    return entity


def ok(*entities: dict, etag: str = 'W/"page1"', next_page: bool = False):
    """200 回應。狀態行以 LF 結尾、其餘 header CRLF——實測 gh 2.45 的真實形狀。"""
    headers = [f"Etag: {etag}\r"] if etag else []
    if next_page:
        # 伺服器給的是絕對 URL；provider 只把它當「還有下一頁」的布林訊號，
        # path 一律本地重建（見 test_pagination_never_follows_server_urls）。
        headers.append(
            'Link: <https://evil.test/repositories/1/issues?page=2>; rel="next"\r'
        )
    stdout = "\n".join(["HTTP/2.0 200 OK", *headers, "\r"]) + "\n" + json.dumps(list(entities))
    return subprocess.CompletedProcess(args=("gh",), returncode=0, stdout=stdout, stderr="")


def not_modified(etag: str = '"page1"'):
    """304：gh 以**非零**離開，stdout 仍有 header 區塊，stderr 是 `gh: HTTP 304`。

    回的 ETag 刻意用強形式（GitHub 實測行為），鎖住「304 不得把它寫回狀態」。
    """
    stdout = f"HTTP/2.0 304 Not Modified\nEtag: {etag}\r\n\r\n"
    return subprocess.CompletedProcess(
        args=("gh",), returncode=1, stdout=stdout, stderr="gh: HTTP 304"
    )


def failure(stderr: str = "HTTP 500: upstream exploded"):
    return subprocess.CompletedProcess(
        args=("gh",), returncode=1, stdout="", stderr=stderr
    )


class Runner:
    """依序回應的 fake `gh` runner，記錄每次 argv。"""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, timeout=None):  # noqa: ARG002 - 契約簽章
        argv = tuple(argv)
        self.calls.append(argv)
        if not self.responses:
            raise AssertionError(f"未預期的第 {len(self.calls)} 次 gh 呼叫：{argv}")
        return self.responses.pop(0)

    @property
    def paths(self) -> list[str]:
        return [argv[-1] for argv in self.calls]

    def conditional_headers(self) -> list[str]:
        headers = []
        for argv in self.calls:
            for index, token in enumerate(argv):
                if token == "--header" and argv[index + 1].startswith("If-None-Match:"):
                    headers.append(argv[index + 1])
        return headers


def provider(store, runner, *, now="2026-08-15T12:00:00Z", **kwargs):
    return GitHubWorkProvider(
        REPO, runner=runner, sync_store=store, now=lambda: now, **kwargs
    )


@pytest.fixture()
def store(tmp_path: Path) -> IssueSyncStore:
    return IssueSyncStore(tmp_path / "github-issue-sync.json")


def statuses(snapshot) -> dict[str, str]:
    return {source.ref: source.status for source in snapshot.sources}


STEADY_ETAG = 'W/"steady"'


def bootstrap(store, *entities, now="2026-08-15T12:00:00Z"):
    """把 store 跑到穩態（下一輪就會送條件請求）。

    ETag 綁定它所屬的 request path，所以全量之後的**第一個**增量輪次必然是一次
    無條件 200——那一輪的 path 多了 `&since=`，與全量那顆 ETag 的 path 不同。
    第二個增量輪次起 path 不再變動，才進入每輪 304 的穩態。這一輪的成本是
    每次全量／每次游標前進各多一次計費請求，見
    test_cursor_advance_retires_the_stale_etag。
    """
    provider(store, Runner(ok(*entities)), now=now).scan()
    provider(store, Runner(ok(*entities, etag=STEADY_ETAG)), now=now).scan()


# ---------------------------------------------------------------------------
# A. 協定形狀：第一輪全量、之後增量
# ---------------------------------------------------------------------------


def test_first_cycle_without_state_is_a_full_scan(store):
    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))

    snapshot = provider(store, runner).scan()

    assert snapshot.status == "ok"
    # 沒有 durable 游標 → 全量：不帶 since、不帶條件請求。
    assert runner.paths == [BASE_PATH]
    assert runner.conditional_headers() == []
    assert snapshot.observations["issue_sync"]["mode"] == "full"
    # 游標與 ETag 落成 durable 狀態，供下一輪續讀。
    state = store.load(REPO)
    assert state.since == "2026-08-15T09:00:00Z"
    assert state.etag == 'W/"page1"'
    assert state.etag_request == BASE_PATH
    assert state.last_full_sync_at == "2026-08-15T12:00:00Z"


def test_second_cycle_is_incremental_with_since_but_no_stale_etag(store):
    """全量之後的第一個增量輪次帶 `since`，但**不**帶條件請求。

    全量那顆 ETag 屬於沒有 `&since=` 的 path；拿它去問一個不同的 URL，伺服器的
    回答對不上我們手上的資料。ETag 因此必須跟著它的 request path 一起被作廢。
    """
    provider(store, Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))).scan()

    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))
    snapshot = provider(store, runner).scan()

    assert snapshot.observations["issue_sync"]["mode"] == "incremental"
    assert runner.paths == [f"{BASE_PATH}&since=2026-08-15T09%3A00%3A00Z"]
    assert runner.conditional_headers() == []


def test_steady_state_cycle_sends_the_conditional_request(store):
    bootstrap(store, issue(1, updated_at="2026-08-15T09:00:00Z"))

    runner = Runner(not_modified())
    snapshot = provider(store, runner).scan()

    assert runner.paths == [f"{BASE_PATH}&since=2026-08-15T09%3A00%3A00Z"]
    assert runner.conditional_headers() == [f"If-None-Match: {STEADY_ETAG}"]
    assert snapshot.observations["issue_sync"]["not_modified"] is True


def test_cursor_advance_retires_the_stale_etag(store):
    """游標一前進，request path 就變了——舊 ETag 屬於舊 path，不得再送。

    這是穩態被打斷後重新暖機的完整序列：有活動的那一輪仍是條件請求（path 尚未
    變），它把游標推前；下一輪 path 變了，舊 ETag 一律作廢、退回無條件 200；
    再下一輪 path 穩定下來，才重新回到 304。
    """
    bootstrap(store, issue(1, updated_at="2026-08-15T09:00:00Z"))

    # 有新活動：這一輪仍問 since=09:00，游標被推到 10:00。
    advanced = Runner(ok(issue(1, updated_at="2026-08-15T10:00:00Z"), etag='W/"page2"'))
    provider(store, advanced).scan()
    assert advanced.conditional_headers() == [f"If-None-Match: {STEADY_ETAG}"]

    # path 換成 since=10:00：`W/"page2"` 屬於 since=09:00 那個 path，作廢。
    retired = Runner(ok(issue(1, updated_at="2026-08-15T10:00:00Z"), etag='W/"page3"'))
    provider(store, retired).scan()
    assert retired.paths == [f"{BASE_PATH}&since=2026-08-15T10%3A00%3A00Z"]
    assert retired.conditional_headers() == []

    # path 穩定，重回 304 穩態。
    runner = Runner(not_modified())
    provider(store, runner).scan()
    assert runner.paths == [f"{BASE_PATH}&since=2026-08-15T10%3A00%3A00Z"]
    assert runner.conditional_headers() == ['If-None-Match: W/"page3"']


# ---------------------------------------------------------------------------
# B. 驗收 1：網頁端關閉 issue → 一個 refresh 週期內 mirror 轉 closed 且不再 auto-claim
# ---------------------------------------------------------------------------


def test_web_closed_issue_flips_mirror_and_leaves_auto_claim_within_one_cycle(store):
    """`state=open&since=` 看不到剛被關閉的 issue——這正是必須用 `state=all` 的理由。

    「不得再被 auto-claim」在鏡像這一層的具體判準就是退出
    ``observations["auto_label_issues"]``：D1 之後 claim.py 的 canonical
    ``WorkAuthority.auto_label`` 就是從這份名單導出的
    （由 test_auto_label_mirror_d1.py 鎖住）。
    """
    opened = issue(
        7, state="open", updated_at="2026-08-15T09:00:00Z", labels=(AUTO_CLAIM_LABEL,)
    )
    first = provider(store, Runner(ok(opened))).scan()
    assert statuses(first) == {"acme/demo#7": "open"}
    assert first.observations["auto_label_issues"] == [7]

    # 人類在網頁端按下 Close：closed issue 的 updated_at 會前進，因此這件事
    # 天然搭著 `state=all&since=` 的增量進來。
    closed = issue(
        7, state="closed", updated_at="2026-08-15T11:00:00Z", labels=(AUTO_CLAIM_LABEL,)
    )
    runner = Runner(ok(closed, etag='W/"closed"'))
    second = provider(store, runner).scan()

    assert runner.paths == [f"{BASE_PATH}&since=2026-08-15T09%3A00%3A00Z"]
    assert statuses(second) == {"acme/demo#7": "closed"}
    assert second.observations["auto_label_issues"] == []
    assert store.load(REPO).by_number[7].state == "closed"


# ---------------------------------------------------------------------------
# C. 驗收 2：304 不改 mirror、不亂動游標、計為條件請求
# ---------------------------------------------------------------------------


def test_not_modified_leaves_mirror_cursor_and_etag_untouched(store):
    bootstrap(
        store, issue(1, updated_at="2026-08-15T09:00:00Z", labels=(AUTO_CLAIM_LABEL,))
    )
    before = store.path.read_text(encoding="utf-8")

    runner = Runner(not_modified())
    snapshot = provider(store, runner).scan()

    assert snapshot.status == "ok"
    # mirror 照樣完整投影出來（304 是「沒有變更」，不是「沒有資料」）。
    assert statuses(snapshot) == {"acme/demo#1": "open"}
    assert snapshot.observations["auto_label_issues"] == [1]
    # 游標、ETag、鏡像三者連檔案位元組都沒動。
    assert store.path.read_text(encoding="utf-8") == before
    state = store.load(REPO)
    assert state.since == "2026-08-15T09:00:00Z"
    # 特別是 ETag 不得被 304 回應的強形式 `"page1"` 覆蓋掉。
    assert state.etag == STEADY_ETAG


def test_not_modified_is_counted_as_a_free_conditional_request(store):
    bootstrap(store, issue(1, updated_at="2026-08-15T09:00:00Z"))

    snapshot = provider(store, Runner(not_modified())).scan()

    sync = snapshot.observations["issue_sync"]
    assert sync["not_modified"] is True
    assert sync["conditional_requests"] == 1
    assert sync["requests"] == 1
    # 304 不計入 GitHub rate limit 配額，因此計費請求數是 0。
    assert sync["billed_requests"] == 0


def test_unsolicited_304_without_a_conditional_request_is_degraded(store):
    """沒送 If-None-Match 卻收到 304：協定被破壞，不得當成「沒有變更」。"""
    snapshot = provider(store, Runner(not_modified())).scan()

    assert snapshot.status == "degraded"
    assert any("304" in item for item in snapshot.diagnostics)


# ---------------------------------------------------------------------------
# D. 驗收 3：增量回應含新開／更新／關閉三類，mirror 收斂正確
# ---------------------------------------------------------------------------


def test_incremental_delta_converges_opened_updated_and_closed(store):
    provider(
        store,
        Runner(
            ok(
                issue(1, state="open", updated_at="2026-08-15T09:00:00Z", title="one"),
                issue(2, state="open", updated_at="2026-08-15T08:00:00Z", title="two"),
                issue(3, state="open", updated_at="2026-08-15T07:00:00Z", title="three"),
            )
        ),
    ).scan()

    runner = Runner(
        ok(
            # 新開
            issue(4, state="open", updated_at="2026-08-15T11:00:00Z", title="four"),
            # 更新（改標題、仍 open）
            issue(2, state="open", updated_at="2026-08-15T10:30:00Z", title="two-v2"),
            # 關閉
            issue(3, state="closed", updated_at="2026-08-15T10:00:00Z", title="three"),
            etag='W/"delta"',
        )
    )
    snapshot = provider(store, runner).scan()

    assert statuses(snapshot) == {
        "acme/demo#1": "open",  # 不在 delta 內 → 沿用鏡像
        "acme/demo#2": "open",
        "acme/demo#3": "closed",
        "acme/demo#4": "open",
    }
    titles = {source.ref: source.title for source in snapshot.sources}
    assert titles["acme/demo#2"] == "two-v2"
    assert titles["acme/demo#1"] == "one"
    sync = snapshot.observations["issue_sync"]
    assert (sync["delta_entries"], sync["mirror_entries"]) == (3, 4)
    # 游標推進到 delta 的最大 updated_at。
    assert store.load(REPO).since == "2026-08-15T11:00:00Z"


# ---------------------------------------------------------------------------
# E. 驗收 4：游標／ETag 狀態損壞 → fail closed 全量重建
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload, label",
    [
        ("{ not json at all", "檔案不是 JSON"),
        (json.dumps({"schema": "github-issue-sync/v99", "repos": {}}), "schema 不認得"),
        (json.dumps({"schema": "github-issue-sync/v1", "repos": []}), "repos 形狀不對"),
        (
            json.dumps(
                {
                    "schema": "github-issue-sync/v1",
                    "repos": {REPO: {"entries": [], "since": "not-a-timestamp"}},
                }
            ),
            "游標格式不合",
        ),
        (
            json.dumps(
                {
                    "schema": "github-issue-sync/v1",
                    "repos": {REPO: {"entries": [{"number": 1}]}},
                }
            ),
            "entries 缺欄位",
        ),
        (
            json.dumps(
                {
                    "schema": "github-issue-sync/v1",
                    # ETag 沒有它所屬的 request path，就無從判斷該不該送
                    "repos": {REPO: {"entries": [], "etag": 'W/"orphan"'}},
                }
            ),
            "ETag 與 path 失聯",
        ),
    ],
)
def test_corrupt_state_fails_closed_to_a_full_rebuild(store, payload, label):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(payload, encoding="utf-8")

    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))
    snapshot = provider(store, runner).scan()

    assert snapshot.status == "ok", label
    # 全量重建：不帶 since、不帶條件請求——絕不拿半壞的游標去做增量。
    assert runner.paths == [BASE_PATH], label
    assert runner.conditional_headers() == [], label
    assert snapshot.observations["issue_sync"]["mode"] == "full", label
    assert any("unusable" in item for item in snapshot.diagnostics), label
    # 壞掉的那份被健康的狀態覆蓋，下一輪恢復增量。
    assert store.load(REPO).since == "2026-08-15T09:00:00Z", label


def test_one_repo_corruption_does_not_disturb_another_repo(store):
    """單一 repo 的紀錄壞掉不該讓其他 repo 一起退回全量。"""
    bootstrap(store, issue(1, updated_at="2026-08-15T09:00:00Z"))
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["repos"]["other/repo"] = {"entries": [{"number": "not-an-int"}]}
    store.path.write_text(json.dumps(document), encoding="utf-8")

    runner = Runner(not_modified())
    snapshot = provider(store, runner).scan()

    assert snapshot.status == "ok"
    assert snapshot.observations["issue_sync"]["not_modified"] is True
    with pytest.raises(IssueSyncStateError):
        store.load("other/repo")


def test_missing_state_file_is_a_full_rebuild_not_an_error(store):
    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))

    snapshot = provider(store, runner).scan()

    assert snapshot.status == "ok"
    assert runner.paths == [BASE_PATH]
    # 「第一次見到這個 repo」不該被記成 drift／損壞診斷。
    assert snapshot.diagnostics == ()


def test_provider_without_a_store_always_runs_a_full_scan(store):
    """無 durable 狀態即無增量——這是誠實的契約，不是靜默降級。"""
    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))

    snapshot = GitHubWorkProvider(REPO, runner=runner).scan()

    assert snapshot.status == "ok"
    assert runner.paths == [BASE_PATH]
    assert snapshot.observations["issue_sync"]["mode"] == "full"


def test_unwritable_state_does_not_degrade_the_snapshot(store, monkeypatch):
    """游標存不下來是效能退化（下輪重來一次全量），不是正確性問題。"""

    def explode(_state):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(store, "save", explode)
    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))

    snapshot = provider(store, runner).scan()

    assert snapshot.status == "ok"
    assert statuses(snapshot) == {"acme/demo#1": "open"}
    assert snapshot.observations["issue_sync"]["persisted"] is False
    assert any("not persisted" in item for item in snapshot.diagnostics)


# ---------------------------------------------------------------------------
# F. 驗收 5：每日全量 anti-entropy 的觸發與對帳
# ---------------------------------------------------------------------------


def test_daily_full_sync_triggers_after_the_interval(store):
    bootstrap(store, issue(1, updated_at="2026-08-15T09:00:00Z"))

    # 未滿 24h：仍是增量。
    runner = Runner(not_modified())
    snapshot = provider(store, runner, now="2026-08-16T11:59:00Z").scan()
    assert snapshot.observations["issue_sync"]["mode"] == "incremental"

    # 滿 24h：強制全量。
    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))
    snapshot = provider(store, runner, now="2026-08-16T12:00:01Z").scan()

    assert snapshot.observations["issue_sync"]["mode"] == "full"
    assert snapshot.observations["issue_sync"]["reason"] == "anti-entropy"
    assert runner.paths == [BASE_PATH]
    # 全量刻意不帶 If-None-Match：對帳的職責就是真的重讀一次。
    assert runner.conditional_headers() == []
    assert store.load(REPO).last_full_sync_at == "2026-08-16T12:00:01Z"


def test_a_clock_that_ran_backwards_forces_a_full_sync(store):
    provider(
        store, Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z"))), now="2026-08-15T12:00:00Z"
    ).scan()

    runner = Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))
    snapshot = provider(store, runner, now="2026-08-14T00:00:00Z").scan()

    assert snapshot.observations["issue_sync"]["mode"] == "full"


def test_full_sync_reconciles_drift_in_favour_of_the_full_read(store, caplog):
    """增量看不到刪除／transfer，也看不到任何一次被吞掉的漏發——全量兜底。"""
    provider(
        store,
        Runner(
            ok(
                issue(1, state="open", updated_at="2026-08-15T09:00:00Z"),
                issue(2, state="open", updated_at="2026-08-15T08:00:00Z"),
            )
        ),
        now="2026-08-15T12:00:00Z",
    ).scan()

    # 24h 後的全量看到的真相：#2 消失（被刪除／transfer）、#3 從沒進過增量、
    # #1 的狀態與鏡像不符。
    runner = Runner(
        ok(
            issue(1, state="closed", updated_at="2026-08-16T09:00:00Z"),
            issue(3, state="open", updated_at="2026-08-16T08:00:00Z"),
            etag='W/"full"',
        )
    )
    with caplog.at_level(logging.WARNING, logger="paulsha_cortex.monitor.providers"):
        snapshot = provider(store, runner, now="2026-08-16T12:00:01Z").scan()

    # 以全量為準：鏡像就是全量結果，沒有殘留的 #2。
    assert statuses(snapshot) == {"acme/demo#1": "closed", "acme/demo#3": "open"}
    drift = snapshot.observations["issue_sync"]["drift"]
    assert drift == {"stale": [2], "unseen": [3], "changed": [1]}
    # log 與 observation 兩邊都要留痕。
    assert any("drift" in record.getMessage() for record in caplog.records)
    assert any("drift" in item for item in snapshot.diagnostics)


def test_full_sync_without_drift_reports_none(store):
    provider(
        store,
        Runner(ok(issue(1, state="open", updated_at="2026-08-15T09:00:00Z"))),
        now="2026-08-15T12:00:00Z",
    ).scan()

    runner = Runner(ok(issue(1, state="open", updated_at="2026-08-15T09:00:00Z")))
    snapshot = provider(store, runner, now="2026-08-16T12:00:01Z").scan()

    assert snapshot.observations["issue_sync"]["drift"] is None
    assert snapshot.diagnostics == ()


# ---------------------------------------------------------------------------
# G. 游標推進紀律
# ---------------------------------------------------------------------------


def test_pagination_failure_does_not_advance_the_cursor(store):
    provider(store, Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))).scan()
    before = store.path.read_text(encoding="utf-8")

    # 第 1 頁成功、第 2 頁炸掉：整輪 degraded，游標／ETag／鏡像原封不動。
    runner = Runner(
        ok(issue(9, updated_at="2026-08-15T11:00:00Z"), next_page=True),
        failure(),
    )
    snapshot = provider(store, runner).scan()

    assert snapshot.status == "degraded"
    assert snapshot.sources == ()
    assert store.path.read_text(encoding="utf-8") == before
    assert store.load(REPO).since == "2026-08-15T09:00:00Z"


def test_cursor_comes_from_the_response_not_the_local_clock(store):
    runner = Runner(
        ok(
            issue(1, updated_at="2026-08-15T09:00:00Z"),
            issue(2, updated_at="2026-08-15T10:30:00Z"),
        )
    )

    provider(store, runner, now="2026-08-15T23:59:59Z").scan()

    # 本機時鐘是 23:59:59，游標必須是回應中最大的 updated_at。用本機時鐘會把
    # 「回應產生後、本輪結束前」發生的更新永久跳過。
    assert store.load(REPO).since == "2026-08-15T10:30:00Z"


def test_cursor_never_regresses(store):
    provider(store, Runner(ok(issue(1, updated_at="2026-08-15T10:00:00Z")))).scan()

    # 全量回應裡最新的那筆被刪掉了，最大 updated_at 因此倒退——游標不得跟著退。
    runner = Runner(ok(issue(2, updated_at="2026-08-15T08:00:00Z")))
    provider(store, runner, now="2026-08-16T12:00:01Z").scan()

    assert store.load(REPO).since == "2026-08-15T10:00:00Z"


def test_empty_delta_keeps_the_cursor(store):
    provider(store, Runner(ok(issue(1, updated_at="2026-08-15T09:00:00Z")))).scan()

    runner = Runner(ok(etag='W/"empty"'))
    snapshot = provider(store, runner).scan()

    assert snapshot.observations["issue_sync"]["mode"] == "incremental"
    assert store.load(REPO).since == "2026-08-15T09:00:00Z"
    # 空 delta 不得把鏡像清空。
    assert statuses(snapshot) == {"acme/demo#1": "open"}


# ---------------------------------------------------------------------------
# H. 分頁
# ---------------------------------------------------------------------------


def test_pagination_never_follows_server_supplied_urls(store):
    """Link header 只當「還有下一頁」的布林訊號；path 一律本地重建。

    跟隨伺服器給的絕對 URL 等於讓對方指定 `gh` 要把 token 送去哪。
    """
    runner = Runner(
        ok(issue(1, updated_at="2026-08-15T09:00:00Z"), next_page=True),
        ok(issue(2, updated_at="2026-08-15T08:00:00Z")),
    )

    snapshot = provider(store, runner).scan()

    assert runner.paths == [BASE_PATH, f"{BASE_PATH}&page=2"]
    assert all("evil.test" not in path for path in runner.paths)
    assert set(statuses(snapshot)) == {"acme/demo#1", "acme/demo#2"}
    assert snapshot.observations["issue_sync"]["pages"] == 2


def test_continuation_pages_do_not_carry_the_conditional_header(store):
    bootstrap(store, issue(1, updated_at="2026-08-15T09:00:00Z"))

    runner = Runner(
        ok(issue(1, updated_at="2026-08-15T09:00:00Z"), next_page=True),
        ok(issue(2, updated_at="2026-08-15T08:00:00Z")),
    )
    provider(store, runner).scan()

    # 只有第 1 頁是條件請求；續頁若回 304 會是協定破壞。
    assert len(runner.conditional_headers()) == 1
    assert "--header" not in runner.calls[1]


def test_an_issue_updated_mid_pagination_does_not_crash_the_scan(store):
    """分頁跑的是一個**活的**、依 updated 排序的清單。

    某個 issue 在我們讀第 1 頁與第 2 頁之間被更新，就會在兩頁各出現一次。這是分頁
    本身的產物，不是壞回應——但重複的 issue 號會讓 durable 狀態的驗證直接 raise，
    例外若逸出 provider 會打斷整個 refresh 迴圈。收斂時保留較新的那筆。
    """
    runner = Runner(
        ok(
            issue(1, state="open", updated_at="2026-08-15T09:00:00Z"),
            issue(2, state="open", updated_at="2026-08-15T08:00:00Z"),
            next_page=True,
        ),
        # #2 在翻頁期間被關閉，於是在第 2 頁又出現一次（updated_at 更新）。
        ok(issue(2, state="closed", updated_at="2026-08-15T09:30:00Z")),
    )

    snapshot = provider(store, runner).scan()

    assert snapshot.status == "ok"
    assert statuses(snapshot) == {"acme/demo#1": "open", "acme/demo#2": "closed"}
    assert store.load(REPO).since == "2026-08-15T09:30:00Z"


def test_runaway_pagination_fails_closed(store):
    runner = Runner(
        *[ok(issue(n + 1, updated_at="2026-08-15T09:00:00Z"), next_page=True) for n in range(60)]
    )

    snapshot = provider(store, runner).scan()

    assert snapshot.status == "degraded"
    assert any("pagination incomplete" in item for item in snapshot.diagnostics)
    assert len(runner.calls) == GitHubWorkProvider._PAGE_LIMIT


# ---------------------------------------------------------------------------
# I. 驗收 6：每輪 API 呼叫數對比（量化樁）
# ---------------------------------------------------------------------------


def test_steady_state_cycle_costs_one_free_conditional_request(store):
    """改動前：每輪每 repo 至少 1 次計費請求（issue 數過 100 再每 100 筆加 1 次）。

    改動後穩態：每輪每 repo 1 次條件請求，而 304 **不計入 rate limit 配額**，
    因此計費請求數為 0；配額只在真的有 issue 活動或每日 anti-entropy 時消耗。
    """
    bootstrap(store, issue(1, updated_at="2026-08-15T09:00:00Z"))

    billed = 0
    requests = 0
    for _ in range(10):
        snapshot = provider(store, Runner(not_modified())).scan()
        sync = snapshot.observations["issue_sync"]
        requests += sync["requests"]
        billed += sync["billed_requests"]

    assert requests == 10
    assert billed == 0


def test_a_full_day_of_thirteen_repos_costs_far_less_than_the_old_full_scan(tmp_path):
    """13 個 configured repo、每 300s 一輪 → 每日 288 輪。

    改動前：每輪每 repo 至少 1 次計費請求 → 13 × 288 = **3744 次／日**（issue 數
    過 100 的 repo 再按頁數加倍）。

    改動後（repo 全日無 issue 活動的穩態）：條件請求同樣是 13 × 288 次，但除了
    每日 1 次 anti-entropy 全量、以及它後面那一次無法沿用 ETag 的增量之外，
    全部是免費 304 → **每 repo 每日 2 次計費請求**，合計 26 次／日。
    """
    repos = [f"acme/repo{index}" for index in range(13)]
    cycles_per_day = 288
    now = "2026-08-15T00:00:00Z"
    billed = 0
    conditional = 0

    for repo in repos:
        repo_store = IssueSyncStore(tmp_path / f"{repo.replace('/', '_')}.json")

        def scan(response):
            return GitHubWorkProvider(
                repo,
                runner=Runner(response),
                sync_store=repo_store,
                now=lambda: now,
            ).scan()

        # 第 1 輪：當日的 anti-entropy 全量（計費）。
        # 第 2 輪：path 多了 &since=，沿用不到全量那顆 ETag，無條件 200（計費）。
        cycle_responses = [
            ok(issue(1, updated_at="2026-08-15T09:00:00Z")),
            ok(issue(1, updated_at="2026-08-15T09:00:00Z"), etag=STEADY_ETAG),
        ]
        # 其餘輪次進入穩態，每輪一次免費 304。
        cycle_responses += [not_modified()] * (cycles_per_day - 2)
        for response in cycle_responses:
            sync = scan(response).observations["issue_sync"]
            billed += sync["billed_requests"]
            conditional += sync["conditional_requests"]

    assert billed == 2 * len(repos) == 26
    assert conditional == len(repos) * (cycles_per_day - 2)
    # 相對舊路徑（每輪每 repo 至少 1 次計費）降兩個數量級。
    assert billed * 100 < len(repos) * cycles_per_day


# ---------------------------------------------------------------------------
# J. request path 契約
# ---------------------------------------------------------------------------


def test_request_path_pins_state_all_and_updated_sort():
    """兩個不可退讓的 query 參數。

    - `state=all`：`state=open&since=` 看不到剛被關閉的 issue，closure reducer
      因此拿不到 closed 證據。
    - `sort=updated&direction=desc`：預設的 created desc 排序下，一個舊 issue 剛被
      更新可能落在第 2 頁而不改變第 1 頁，第 1 頁的 ETag 就不再是整個 delta 的
      變更偵測器，條件請求會漏發。
    """
    path = issues_request_path(REPO, since="2026-08-15T09:00:00Z")

    assert "state=all" in path
    assert "sort=updated" in path and "direction=desc" in path
    # 游標進 query string 前一律 percent-encode。
    assert "since=2026-08-15T09%3A00%3A00Z" in path


def test_request_path_rejects_a_non_api_timestamp_cursor():
    with pytest.raises(IssueSyncStateError):
        issues_request_path(REPO, since="2026-08-15 09:00:00")
