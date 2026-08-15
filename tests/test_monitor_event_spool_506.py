"""#506 / D4：monitor 事件入口（spool）＋targeted refresh 的驗收鎖。

契約全文見 ``monitor/event_spool`` 模組 docstring。本檔逐條鎖住：

- **spool 契約**：一事件一檔、原子寫入、信封欄位、fire-and-forget 寫入端語意
- **消費端**：targeted 條件請求 → 驗證通過才更新鏡像 → 處理成功才消費事件檔
- **順序與去重**：同物件多事件收斂為一次驗證；亂序／過期事件安全跳過
- **壞檔隔離**：壞事件檔隔離到 ``quarantine/``，不阻塞同一輪的其他事件
- **fail-safe**：targeted 驗證失敗（請求錯誤／404／壞 JSON）一律不寫鏡像

全程不打真實 GitHub API——mock 的是 ``gh api`` 的輸出（HTTP 層）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from paulsha_cortex.monitor.event_spool import (
    EVENT_SCHEMA,
    EVENT_TYPE_GITHUB_OBJECT,
    EVENT_TYPE_JOB,
    EVENT_TYPE_STEERING,
    RESERVED_EVENT_TYPES,
    EventSpool,
    SpoolEvent,
    SpoolEventError,
    coalesce_hints,
    github_object_hint,
)
from paulsha_cortex.monitor.github_issue_sync import (
    IssueSyncState,
    IssueSyncStore,
    issue_request_path,
)
from paulsha_cortex.monitor.providers import GitHubWorkProvider


REPO = "acme/demo"
CYCLE = "2026-08-15T12:00:00Z"
BEFORE = "2026-08-15T11:59:00Z"
AFTER = "2026-08-15T12:00:30Z"


# ---------------------------------------------------------------------------
# 測試替身
# ---------------------------------------------------------------------------


def issue(
    number: int,
    *,
    state: str = "open",
    updated_at: str = "2026-08-15T09:00:00Z",
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


def _response(status_line: str, payload: object, *, etag: str | None) -> subprocess.CompletedProcess:
    headers = [f"Etag: {etag}\r"] if etag else []
    body = "" if payload is None else json.dumps(payload)
    stdout = "\n".join([status_line, *headers, "\r"]) + "\n" + body
    returncode = 0 if status_line.endswith("200 OK") else 1
    return subprocess.CompletedProcess(
        args=("gh",), returncode=returncode, stdout=stdout, stderr=""
    )


def ok_list(*entities: dict, etag: str = 'W/"list"'):
    return _response("HTTP/2.0 200 OK", list(entities), etag=etag)


def ok_object(entity: dict, *, etag: str = 'W/"object"'):
    """單物件 targeted 回應。"""
    return _response("HTTP/2.0 200 OK", entity, etag=etag)


def not_modified(etag: str = '"object"'):
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=1,
        stdout=f"HTTP/2.0 304 Not Modified\nEtag: {etag}\r\n\r\n",
        stderr="gh: HTTP 304",
    )


def not_found():
    return subprocess.CompletedProcess(
        args=("gh",),
        returncode=1,
        stdout='HTTP/2.0 404 Not Found\r\n\r\n{"message":"Not Found"}',
        stderr="gh: Not Found (HTTP 404)",
    )


def failure(stderr: str = "HTTP 500: upstream exploded"):
    return subprocess.CompletedProcess(args=("gh",), returncode=1, stdout="", stderr=stderr)


class Runner:
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


@pytest.fixture()
def spool(tmp_path: Path) -> EventSpool:
    return EventSpool(tmp_path / "event-spool")


@pytest.fixture()
def store(tmp_path: Path) -> IssueSyncStore:
    return IssueSyncStore(tmp_path / "github-issue-sync.json")


def provider(store, runner, spool=None, *, now=CYCLE, **kwargs):
    return GitHubWorkProvider(
        REPO,
        runner=runner,
        sync_store=store,
        event_spool=spool,
        now=lambda: now,
        **kwargs,
    )


def seed_mirror(store: IssueSyncStore, *entries, since="2026-08-15T09:00:00Z") -> None:
    """把 store 直接推到穩態：有鏡像、有游標、有清單 ETag。

    目的是讓每個 targeted 測試只需要餵「清單端點的 304」＋targeted 回應，
    專注在 D4 的行為上而不重跑 D3 的協定。
    """

    from paulsha_cortex.monitor.github_issue_sync import IssueEntry

    store.save(
        IssueSyncState(
            repo=REPO,
            entries=tuple(IssueEntry.from_api(entity) for entity in entries),
            since=since,
            etag='W/"list"',
            etag_request=(
                "repos/acme/demo/issues?state=all&per_page=100&sort=updated"
                f"&direction=desc&since={since.replace(':', '%3A')}"
            ),
            last_full_sync_at=CYCLE,
        )
    )


def hint(spool: EventSpool, number: int, *, emitted_at=BEFORE, kind="github_issue", repo=REPO):
    path = spool.emit_github_object(
        repo=repo,
        kind=kind,
        number=number,
        source="agent-hook:claude",
        action="commented",
        job_id="job-1",
        now=emitted_at,
    )
    assert path is not None
    return path


def spool_files(spool: EventSpool) -> list[str]:
    return sorted(
        p.name for p in spool.root.iterdir() if p.is_file() and not p.name.startswith(".")
    )


def observations(snapshot) -> dict:
    return dict(snapshot.observations["event_spool"])


def statuses(snapshot) -> dict[str, str]:
    return {source.ref: source.status for source in snapshot.sources}


# ---------------------------------------------------------------------------
# A. spool 契約：寫入端
# ---------------------------------------------------------------------------


def test_each_event_is_one_self_describing_file(spool):
    hint(spool, 11)
    hint(spool, 12)
    names = spool_files(spool)
    assert len(names) == 2
    document = json.loads((spool.root / names[0]).read_text(encoding="utf-8"))
    assert document["schema_version"] == EVENT_SCHEMA
    assert document["event_type"] == EVENT_TYPE_GITHUB_OBJECT
    assert document["source"] == "agent-hook:claude"
    assert document["job_id"] == "job-1"
    assert document["emitted_at"] == BEFORE
    assert set(document["payload"]) == {"repo", "kind", "number", "action"}
    assert document["event_id"]


def test_event_files_are_0600_and_leave_no_temp_files(spool):
    path = hint(spool, 11)
    assert path is not None and path.exists()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not [p for p in spool.root.iterdir() if p.name.startswith(".")]


def test_events_name_an_object_and_never_carry_its_state(spool):
    """hint 不是 authority：契約層就不給 producer 塞狀態的欄位。"""

    document = json.loads(Path(hint(spool, 11)).read_text(encoding="utf-8"))
    assert set(document["payload"]) & {"state", "labels", "title", "updated_at"} == set()


def test_emit_is_fire_and_forget_when_the_directory_is_unusable(tmp_path, caplog):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    spool = EventSpool(blocked / "event-spool")
    assert (
        spool.emit_github_object(
            repo=REPO, kind="github_issue", number=11, source="agent-hook:claude"
        )
        is None
    )


def test_emit_rejects_a_malformed_envelope_without_raising(spool):
    assert (
        spool.emit_github_object(repo="not-a-repo", kind="github_issue", number=11, source="h")
        is not None
    ), "repo 形狀由消費端判定為壞檔，寫入端不做語意驗證"
    assert (
        spool.emit_github_object(repo=REPO, kind="github_issue", number=11, source=" ")
        is None
    ), "信封欄位缺失在寫入端就擋下，不讓壞檔進 spool"


def test_scan_never_creates_the_spool_directory(tmp_path):
    """D5 尚未部署的機器上，monitor 掃到目錄不存在是常態而非錯誤。"""

    spool = EventSpool(tmp_path / "never-created")
    assert spool.scan().hints == ()
    assert not (tmp_path / "never-created").exists()


# ---------------------------------------------------------------------------
# B. 消費端：targeted 條件驗證 → 更新鏡像 → 消費事件
# ---------------------------------------------------------------------------


def test_a_named_object_is_verified_by_a_single_object_request(store, spool):
    seed_mirror(store, issue(11, state="open"))
    hint(spool, 11)
    runner = Runner(
        not_modified('"list"'),  # 清單端點：本輪沒有增量
        ok_object(issue(11, state="closed", updated_at="2026-08-15T11:59:30Z")),
    )
    snapshot = provider(store, runner, spool).scan()

    assert runner.paths[1] == issue_request_path(REPO, 11) == "repos/acme/demo/issues/11"
    assert statuses(snapshot) == {"acme/demo#11": "closed"}
    assert store.load(REPO).by_number[11].state == "closed"
    report = observations(snapshot)
    assert report["objects"] == 1 and report["verified"] == 1 and report["confirmed"] == 1
    assert report["consumed"] == 1
    assert spool_files(spool) == []


def test_targeted_etag_is_persisted_and_replayed_as_a_conditional_request(store, spool):
    seed_mirror(store, issue(11))
    hint(spool, 11)
    provider(
        store,
        Runner(not_modified('"list"'), ok_object(issue(11, updated_at="2026-08-15T11:59:30Z"), etag='W/"o1"')),
        spool,
    ).scan()
    assert store.load(REPO).targeted_etags_by_number == {11: 'W/"o1"'}

    hint(spool, 11, emitted_at=AFTER)
    runner = Runner(not_modified('"list"'), not_modified('"o1"'))
    snapshot = provider(store, runner, spool, now="2026-08-15T12:01:00Z").scan()
    assert "If-None-Match: W/\"o1\"" in runner.conditional_headers()
    report = observations(snapshot)
    assert report["not_modified"] == 1 and report["billed_requests"] == 0
    assert report["consumed"] == 1


def test_targeted_304_never_overwrites_the_stored_etag(store, spool):
    """與 D3 同一顆地雷：304 回強形式 ETag，寫回去會讓條件請求永遠落空。"""

    seed_mirror(store, issue(11))
    hint(spool, 11)
    provider(
        store,
        Runner(not_modified('"list"'), ok_object(issue(11, updated_at="2026-08-15T11:59:30Z"), etag='W/"o1"')),
        spool,
    ).scan()
    hint(spool, 11, emitted_at=AFTER)
    provider(
        store,
        Runner(not_modified('"list"'), not_modified('"strong-form"')),
        spool,
        now="2026-08-15T12:01:00Z",
    ).scan()
    assert store.load(REPO).targeted_etags_by_number == {11: 'W/"o1"'}


def test_targeted_reads_never_advance_the_list_cursor(store, spool):
    """游標只能由清單回應推進——否則會跳過那之間被更新的其他物件。"""

    seed_mirror(store, issue(11), since="2026-08-15T09:00:00Z")
    hint(spool, 11)
    provider(
        store,
        Runner(not_modified('"list"'), ok_object(issue(11, updated_at="2026-08-15T11:59:30Z"))),
        spool,
    ).scan()
    state = store.load(REPO)
    assert state.since == "2026-08-15T09:00:00Z"
    assert state.by_number[11].updated_at == "2026-08-15T11:59:30Z"


def test_verification_without_a_difference_leaves_the_mirror_alone(store, spool):
    seed_mirror(store, issue(11, updated_at="2026-08-15T09:00:00Z"))
    hint(spool, 11)
    snapshot = provider(
        store,
        Runner(not_modified('"list"'), ok_object(issue(11, updated_at="2026-08-15T09:00:00Z"))),
        spool,
    ).scan()
    report = observations(snapshot)
    assert report["verified"] == 1 and report["confirmed"] == 0
    assert report["consumed"] == 1


def test_targeted_verification_can_admit_an_object_the_mirror_never_saw(store, spool):
    seed_mirror(store, issue(11))
    hint(spool, 77)
    snapshot = provider(
        store,
        Runner(not_modified('"list"'), ok_object(issue(77, title="brand new"))),
        spool,
    ).scan()
    assert sorted(statuses(snapshot)) == ["acme/demo#11", "acme/demo#77"]
    assert observations(snapshot)["confirmed"] == 1


def test_pull_request_hints_use_the_same_issues_endpoint(store, spool):
    seed_mirror(store, issue(11))
    hint(spool, 42, kind="github_pr")
    snapshot = provider(
        store,
        Runner(not_modified('"list"'), ok_object(issue(42, pull_request=True))),
        spool,
    ).scan()
    assert {source.ref: source.kind for source in snapshot.sources}["acme/demo#42"] == "github_pr"


def test_a_provider_without_a_spool_behaves_exactly_like_d3(store, spool):
    seed_mirror(store, issue(11))
    hint(spool, 11)
    runner = Runner(not_modified('"list"'))
    snapshot = provider(store, runner, None).scan()
    assert len(runner.calls) == 1
    assert "event_spool" not in snapshot.observations
    assert spool_files(spool), "事件檔原封不動"


# ---------------------------------------------------------------------------
# C. 順序與去重
# ---------------------------------------------------------------------------


def test_many_events_for_one_object_collapse_into_one_verification(store, spool):
    seed_mirror(store, issue(11))
    for stamp in ("2026-08-15T11:57:00Z", "2026-08-15T11:58:00Z", BEFORE):
        hint(spool, 11, emitted_at=stamp)
    runner = Runner(not_modified('"list"'), ok_object(issue(11, updated_at="2026-08-15T11:59:30Z")))
    snapshot = provider(store, runner, spool).scan()

    assert runner.paths.count(issue_request_path(REPO, 11)) == 1
    report = observations(snapshot)
    assert report["pending"] == 3 and report["objects"] == 1
    assert report["consumed"] == 3, "收斂掉的事件必須一起被消費，否則下輪重驗"
    assert spool_files(spool) == []


def test_out_of_order_events_do_not_change_the_outcome(store, spool):
    """事件之間沒有全域順序；每個物件只問 GitHub「你現在長怎樣」。"""

    seed_mirror(store, issue(11))
    hint(spool, 11, emitted_at=BEFORE)
    hint(spool, 11, emitted_at="2026-08-15T11:50:00Z")
    runner = Runner(not_modified('"list"'), ok_object(issue(11, state="closed", updated_at="2026-08-15T11:59:30Z")))
    snapshot = provider(store, runner, spool).scan()
    assert statuses(snapshot) == {"acme/demo#11": "closed"}
    assert observations(snapshot)["objects"] == 1


def test_an_event_already_covered_by_this_cycle_costs_no_request(store, spool):
    """事件比本輪請求早、物件又在本輪 delta 裡 → 鏡像已至少同樣新。"""

    seed_mirror(store, issue(11, state="open"))
    hint(spool, 11, emitted_at=BEFORE)
    runner = Runner(ok_list(issue(11, state="closed", updated_at="2026-08-15T11:59:30Z")))
    snapshot = provider(store, runner, spool).scan()

    assert len(runner.calls) == 1, "targeted 請求完全省下"
    assert statuses(snapshot) == {"acme/demo#11": "closed"}
    report = observations(snapshot)
    assert report["superseded"] == 1 and report["requests"] == 0 and report["consumed"] == 1


def test_a_full_sync_covers_every_earlier_event(store, spool):
    seed_mirror(store, issue(11), issue(12))
    hint(spool, 11, emitted_at=BEFORE)
    hint(spool, 12, emitted_at=BEFORE)
    runner = Runner(ok_list(issue(11), issue(12)))
    snapshot = provider(
        store, runner, spool, full_sync_interval_seconds=0.0
    ).scan()
    assert len(runner.calls) == 1
    report = observations(snapshot)
    assert report["superseded"] == 2 and report["consumed"] == 2


def test_a_list_304_is_not_a_read_and_never_covers_an_event(store, spool):
    """304 什麼都沒讀回來，不能當成「這個物件我剛讀過」。"""

    seed_mirror(store, issue(11))
    hint(spool, 11, emitted_at=BEFORE)
    runner = Runner(not_modified('"list"'), ok_object(issue(11, updated_at="2026-08-15T11:59:30Z")))
    snapshot = provider(store, runner, spool).scan()
    assert runner.paths[1] == issue_request_path(REPO, 11)
    assert observations(snapshot)["superseded"] == 0


def test_an_event_emitted_after_the_request_is_not_covered(store, spool):
    seed_mirror(store, issue(11))
    hint(spool, 11, emitted_at=AFTER)
    runner = Runner(
        ok_list(issue(11, updated_at="2026-08-15T11:59:30Z")),
        ok_object(issue(11, state="closed", updated_at="2026-08-15T12:00:40Z")),
    )
    snapshot = provider(store, runner, spool).scan()
    assert runner.paths[1] == issue_request_path(REPO, 11)
    assert statuses(snapshot) == {"acme/demo#11": "closed"}


def test_events_for_another_repo_are_left_untouched(store, spool):
    seed_mirror(store, issue(11))
    hint(spool, 11, repo="other/repo")
    runner = Runner(not_modified('"list"'))
    snapshot = provider(store, runner, spool).scan()
    assert len(runner.calls) == 1
    assert observations(snapshot)["pending"] == 0
    assert spool_files(spool), "留給那個 repo 的 provider"


def test_events_past_the_per_cycle_limit_wait_for_the_next_cycle(store, spool):
    seed_mirror(store, issue(11), issue(12), issue(13))
    hint(spool, 11, emitted_at="2026-08-15T11:50:00Z")
    hint(spool, 12, emitted_at="2026-08-15T11:51:00Z")
    hint(spool, 13, emitted_at="2026-08-15T11:52:00Z")
    runner = Runner(
        not_modified('"list"'),
        ok_object(issue(11, updated_at="2026-08-15T11:59:30Z")),
    )
    snapshot = provider(store, runner, spool, targeted_refresh_limit=1).scan()
    report = observations(snapshot)
    assert report["objects"] == 3 and report["verified"] == 1
    assert report["consumed"] == 1 and report["deferred"] == 2
    assert len(spool_files(spool)) == 2
    assert any("deferred" in note for note in snapshot.diagnostics)


def test_coalescing_serves_the_oldest_event_first(spool):
    paths = [
        hint(spool, 30, emitted_at="2026-08-15T11:58:00Z"),
        hint(spool, 10, emitted_at="2026-08-15T11:50:00Z"),
        hint(spool, 20, emitted_at="2026-08-15T11:55:00Z"),
    ]
    assert paths
    refreshes = coalesce_hints(spool.scan(now=CYCLE).for_repo(REPO), repo=REPO)
    assert [refresh.number for refresh in refreshes] == [10, 20, 30]


# ---------------------------------------------------------------------------
# D. fail-safe：驗證不到的變更不寫鏡像
# ---------------------------------------------------------------------------


def test_a_failed_verification_writes_nothing_and_consumes_nothing(store, spool):
    seed_mirror(store, issue(11, state="open"))
    hint(spool, 11)
    runner = Runner(not_modified('"list"'), failure())
    snapshot = provider(store, runner, spool).scan()

    assert snapshot.status == "ok", "清單同步成功；hint 驗不到不是鏡像故障"
    assert statuses(snapshot) == {"acme/demo#11": "open"}
    assert store.load(REPO).by_number[11].state == "open"
    report = observations(snapshot)
    assert report["confirmed"] == 0 and report["consumed"] == 0 and report["deferred"] == 1
    assert spool_files(spool), "事件留著，下一輪或每日對帳再處理"


def test_the_first_failure_stops_the_remaining_targeted_requests(store, spool):
    seed_mirror(store, issue(11), issue(12))
    hint(spool, 11, emitted_at="2026-08-15T11:50:00Z")
    hint(spool, 12, emitted_at="2026-08-15T11:51:00Z")
    runner = Runner(not_modified('"list"'), failure())
    snapshot = provider(store, runner, spool).scan()
    assert len(runner.calls) == 2
    assert observations(snapshot)["deferred"] == 2


def test_a_404_never_deletes_anything_from_the_mirror(store, spool):
    """刪除／transfer 只有每日全量對帳看得到；一次 404 不足以當證據。"""

    seed_mirror(store, issue(11), issue(12))
    hint(spool, 12)
    runner = Runner(not_modified('"list"'), not_found())
    snapshot = provider(store, runner, spool).scan()

    assert sorted(statuses(snapshot)) == ["acme/demo#11", "acme/demo#12"]
    report = observations(snapshot)
    assert report["unverified"] == 1 and report["confirmed"] == 0
    assert report["consumed"] == 1, "重試同一個 404 只是燒配額"
    assert any("anti-entropy" in note for note in snapshot.diagnostics)


def test_malformed_targeted_json_writes_nothing_and_consumes_nothing(store, spool):
    seed_mirror(store, issue(11, state="open"))
    hint(spool, 11)
    runner = Runner(
        not_modified('"list"'),
        _response("HTTP/2.0 200 OK", {"number": 11, "title": "no node id"}, etag=None),
    )
    snapshot = provider(store, runner, spool).scan()
    assert statuses(snapshot) == {"acme/demo#11": "open"}
    assert observations(snapshot)["deferred"] == 1
    assert spool_files(spool)


def test_a_response_for_a_different_object_is_not_trusted(store, spool):
    seed_mirror(store, issue(11, state="open"))
    hint(spool, 11)
    runner = Runner(not_modified('"list"'), ok_object(issue(99, state="closed")))
    snapshot = provider(store, runner, spool).scan()
    assert statuses(snapshot) == {"acme/demo#11": "open"}
    assert observations(snapshot)["confirmed"] == 0
    assert spool_files(spool)


def test_the_backoff_window_leaves_the_spool_untouched(store, spool):
    from paulsha_cortex.monitor.github_pressure import GitHubPressureGate

    seed_mirror(store, issue(11))
    hint(spool, 11)
    gate = GitHubPressureGate(interval_seconds=0, jitter_seconds=0, sleeper=lambda _: None)
    gate.note_rate_limited()
    runner = Runner()
    snapshot = provider(store, runner, spool, pressure_gate=gate).scan()
    assert snapshot.status == "degraded" and runner.calls == []
    assert spool_files(spool)


def test_events_are_not_consumed_when_the_state_was_not_persisted(store, spool, monkeypatch):
    seed_mirror(store, issue(11, state="open"))
    hint(spool, 11)

    def explode(self, state):  # noqa: ANN001 - monkeypatch
        raise OSError("read-only file system")

    monkeypatch.setattr(IssueSyncStore, "save", explode)
    runner = Runner(not_modified('"list"'), ok_object(issue(11, state="closed", updated_at="2026-08-15T11:59:30Z")))
    snapshot = provider(store, runner, spool).scan()
    report = observations(snapshot)
    assert report["consumed"] == 0 and report["deferred"] == 1
    assert spool_files(spool), "鏡像沒落地，事件就還沒被處理完"


# ---------------------------------------------------------------------------
# E. 壞事件檔隔離
# ---------------------------------------------------------------------------


def test_a_bad_event_file_is_quarantined_without_blocking_the_rest(store, spool):
    seed_mirror(store, issue(11))
    hint(spool, 11)
    spool.root.joinpath("00000000-broken.json").write_text("{ not json", encoding="utf-8")

    runner = Runner(not_modified('"list"'), ok_object(issue(11, updated_at="2026-08-15T11:59:30Z")))
    snapshot = provider(store, runner, spool).scan()

    assert observations(snapshot)["quarantined"] == 1
    assert observations(snapshot)["verified"] == 1
    assert (spool.quarantine_root / "00000000-broken.json").exists()
    assert spool_files(spool) == []


@pytest.mark.parametrize(
    ("document", "label"),
    [
        ({"schema_version": EVENT_SCHEMA}, "信封全缺"),
        ({"schema_version": EVENT_SCHEMA, "event_id": "a", "event_type": "github_object",
          "emitted_at": "2026-08-15T11:59:00", "source": "h",
          "payload": {"repo": REPO, "kind": "github_issue", "number": 1}}, "時間戳無時區"),
        ({"schema_version": EVENT_SCHEMA, "event_id": "a", "event_type": "github_object",
          "emitted_at": BEFORE, "source": "h",
          "payload": {"repo": "no-slash", "kind": "github_issue", "number": 1}}, "repo 形狀"),
        ({"schema_version": EVENT_SCHEMA, "event_id": "a", "event_type": "github_object",
          "emitted_at": BEFORE, "source": "h",
          "payload": {"repo": REPO, "kind": "wiki", "number": 1}}, "未知 kind"),
        ({"schema_version": EVENT_SCHEMA, "event_id": "a", "event_type": "github_object",
          "emitted_at": BEFORE, "source": "h",
          "payload": {"repo": REPO, "kind": "github_issue", "number": "1"}}, "編號非整數"),
        ({"schema_version": EVENT_SCHEMA, "event_id": "a", "event_type": "github_object",
          "emitted_at": BEFORE, "source": "h",
          "payload": {"repo": REPO, "kind": "github_issue", "number": True}}, "bool 不是編號"),
        ([1, 2, 3], "根不是物件"),
    ],
)
def test_structurally_broken_events_are_quarantined(spool, document, label):
    spool.root.mkdir(parents=True, exist_ok=True)
    spool.root.joinpath("bad.json").write_text(json.dumps(document), encoding="utf-8")
    scan = spool.scan(now=CYCLE)
    assert scan.hints == (), label
    assert scan.quarantined == ("bad.json",), label
    assert (spool.quarantine_root / "bad.json").exists(), label


def test_expired_orphan_events_are_quarantined_not_hoarded(spool):
    hint(spool, 11, emitted_at="2026-07-01T00:00:00Z")
    scan = spool.scan(now=CYCLE)
    assert scan.hints == () and len(scan.quarantined) == 1
    assert list(spool.quarantine_root.iterdir())


def test_the_quarantine_directory_is_never_scanned_as_events(spool):
    hint(spool, 11)
    spool.quarantine_root.mkdir(parents=True, exist_ok=True)
    spool.quarantine_root.joinpath("old.json").write_text("{ broken", encoding="utf-8")
    scan = spool.scan(now=CYCLE)
    assert len(scan.hints) == 1 and scan.quarantined == ()


def test_a_half_written_temp_file_is_invisible_to_the_consumer(spool):
    spool.root.mkdir(parents=True, exist_ok=True)
    spool.root.joinpath(".event-halfwritten.tmp").write_text('{"schema', encoding="utf-8")
    scan = spool.scan(now=CYCLE)
    assert scan.hints == () and scan.quarantined == ()


# ---------------------------------------------------------------------------
# F. #498 擴充點：其他事件型別記 log 不處理
# ---------------------------------------------------------------------------


def test_steering_and_job_events_are_held_in_place_and_only_counted(store, spool, caplog):
    seed_mirror(store, issue(11))
    for event_type in (EVENT_TYPE_STEERING, EVENT_TYPE_JOB):
        assert spool.emit(
            SpoolEvent(
                event_id=f"{event_type}-1",
                event_type=event_type,
                emitted_at=BEFORE,
                source="agent-hook:claude",
                payload={"job_id": "job-1"},
            )
        )
    with caplog.at_level("INFO"):
        snapshot = provider(store, Runner(not_modified('"list"')), spool).scan()

    report = observations(snapshot)
    assert report["ignored"] == {EVENT_TYPE_STEERING: 1, EVENT_TYPE_JOB: 1}
    assert report["consumed"] == 0
    assert len(spool_files(spool)) == 2, "屬於未來的 consumer，這裡不得刪"
    assert not spool.quarantine_root.exists()
    assert any("unconsumed event" in record.message for record in caplog.records)


def test_reserved_event_types_stay_distinct_from_github_objects():
    assert EVENT_TYPE_GITHUB_OBJECT not in RESERVED_EVENT_TYPES
    assert RESERVED_EVENT_TYPES == {"steering", "job"}


def test_unknown_types_and_schemas_are_held_not_quarantined(spool):
    assert spool.emit(
        SpoolEvent(
            event_id="future-1",
            event_type="workflow_run",
            emitted_at=BEFORE,
            source="agent-hook:claude",
        )
    )
    spool.root.joinpath("future-schema.json").write_text(
        json.dumps(
            {
                "schema_version": "monitor-event-spool/v2",
                "event_id": "future-2",
                "event_type": "github_object",
                "emitted_at": BEFORE,
                "source": "agent-hook:claude",
            }
        ),
        encoding="utf-8",
    )
    scan = spool.scan(now=CYCLE)
    assert scan.hints == () and scan.quarantined == ()
    assert scan.ignored == {"workflow_run": 1} and scan.foreign_schema == 1
    assert len(spool_files(spool)) == 2


# ---------------------------------------------------------------------------
# G. 契約單元
# ---------------------------------------------------------------------------


def test_targeted_path_is_a_single_object_endpoint():
    assert issue_request_path(REPO, 11) == "repos/acme/demo/issues/11"
    with pytest.raises(ValueError):
        issue_request_path(REPO, 0)
    with pytest.raises(ValueError):
        issue_request_path(REPO, True)


def test_targeted_etags_are_pruned_with_the_mirror():
    from paulsha_cortex.monitor.github_issue_sync import IssueEntry

    state = IssueSyncState(
        repo=REPO, entries=(IssueEntry.from_api(issue(11)),)
    ).with_targeted_etags({11: 'W/"a"', 99: 'W/"b"'})
    assert state.targeted_etags_by_number == {11: 'W/"a"'}


def test_targeted_etag_table_round_trips_and_fails_closed(store):
    from paulsha_cortex.monitor.github_issue_sync import IssueEntry, IssueSyncStateError

    state = IssueSyncState(
        repo=REPO, entries=(IssueEntry.from_api(issue(11)),), targeted_etags=((11, 'W/"a"'),)
    )
    store.save(state)
    assert store.load(REPO).targeted_etags_by_number == {11: 'W/"a"'}

    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["repos"][REPO]["targeted_etags"] = {"eleven": 'W/"a"'}
    store.path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(IssueSyncStateError):
        store.load(REPO)


def test_event_filenames_cannot_be_steered_by_event_content(spool):
    event = SpoolEvent(
        event_id="a" * 64,
        event_type=EVENT_TYPE_GITHUB_OBJECT,
        emitted_at=BEFORE,
        source="../../escape",
        payload={"repo": REPO, "kind": "github_issue", "number": 1},
    )
    path = spool.emit(event)
    assert path is not None and path.parent == spool.root
    assert "/" not in path.name and ".." not in path.name


def test_the_envelope_rejects_a_path_bearing_event_id():
    with pytest.raises(SpoolEventError):
        SpoolEvent(
            event_id="../../etc/passwd",
            event_type=EVENT_TYPE_GITHUB_OBJECT,
            emitted_at=BEFORE,
            source="h",
        )


def test_a_hint_keeps_its_originating_event_for_diagnostics(spool):
    hint(spool, 11)
    scanned = spool.scan(now=CYCLE).hints[0]
    assert scanned.ref == "acme/demo#11"
    assert scanned.event.source == "agent-hook:claude"
    assert scanned.event.payload["action"] == "commented"
    assert scanned.event.job_id == "job-1"


def test_github_object_hint_validates_its_payload():
    event = SpoolEvent(
        event_id="e1",
        event_type=EVENT_TYPE_GITHUB_OBJECT,
        emitted_at=BEFORE,
        source="h",
        payload={"repo": REPO, "kind": "github_issue", "number": 11},
    )
    assert github_object_hint(event, Path("x.json")).number == 11
    with pytest.raises(SpoolEventError):
        github_object_hint(
            SpoolEvent(
                event_id="e2",
                event_type=EVENT_TYPE_GITHUB_OBJECT,
                emitted_at=BEFORE,
                source="h",
                payload={},
            ),
            Path("x.json"),
        )
