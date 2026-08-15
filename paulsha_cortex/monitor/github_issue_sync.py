"""#506 / D3：GitHub issues 增量同步——``state=all&since=`` ＋ ETag 條件請求。

## 問題

``GitHubWorkProvider`` 每輪對**每個** configured repo 全量分頁抓 issues
（``issues?state=all&per_page=100`` ＋ ``--paginate``）。D2 把 ``contents``／
``compare`` 歸零之後，這是 monitor 對 REST 配額剩下的主要常態消耗：每輪
per-repo 至少 1 次、issue 數過 100 的 repo 每 100 個再多 1 次，而其中絕大多數
回應與上一輪逐位元組相同。

## 本模組的契約

1. **``state=all`` 不可退讓**：``state=open&since=`` 看不到剛被關閉的 issue，
   closure reducer 因此拿不到 ``closed`` 證據，manager 可能 auto-claim 一件人類
   已經在網頁端關掉的工作。closed issue 的 ``updated_at`` 會隨關閉事件更新，
   ``state=all&since=`` 的增量天然攜帶關閉事件，且 delta 極小。
2. **``sort=updated&direction=desc`` 不可退讓**：預設排序是 ``created`` desc，
   在那個順序下「一個舊 issue 剛被更新」可能落在第 2 頁而**不改變第 1 頁**——
   第 1 頁的 ETag 就不再是整個 delta 的變更偵測器，條件請求會漏發。改成
   updated desc 後，任何 ``updated_at`` 前進的 issue 必然跳到第 1 筆，第 1 頁
   ETag 因此是 sound 的變更偵測器。
3. **ETag 條件請求**：``If-None-Match`` 命中回 304，且 **304 不計入 GitHub rate
   limit 配額**（實測 ``x-ratelimit-used`` 在 304 前後不變）。穩態時每輪每 repo
   就是一次免費的條件請求。ETag 綁定它所屬的 request path（``etag_request``），
   path 一變（``since`` 前進）就不再送舊 ETag。

   .. warning:: 304 回應帶回來的 ``Etag`` 與 200 給的**形式不同**（實測 GitHub
      200 回 ``W/"<hash>"``、304 回 ``"<hash>"`` 這個強形式）。因此 304 一路
      **不得**拿回應的 ETag 去覆蓋既存狀態，否則下一輪送出的 header 會跟伺服器
      認得的那顆對不上，條件請求從此永遠落空、悄悄退化成每輪全額計費。本模組的
      作法是 304 時整份 state 原封不動（連寫都不寫），一併滿足驗收 2。
4. **游標紀律**：``since`` 取自**回應**中最大的 ``updated_at``（不是本機時鐘），
   只在該輪增量**完整成功**（所有分頁都拿到且解析成功）後才推進，且永不倒退。
   分頁中斷、任一頁失敗——游標、ETag、鏡像三者一律原封不動。
   ``since`` 在 GitHub 是**閉區間**（``>=``），因此邊界那筆 issue 每輪都會被重送；
   這正是穩態下 body 穩定、ETag 穩定、304 成立的原因，重複合併本身是冪等的。
5. **每日一次全量 anti-entropy**：增量看不到「issue 被刪除／transfer 出去」這類
   不會留下 ``updated_at`` 痕跡的事件，也看不到任何一次被吞掉的漏發。因此每
   ``full_sync_interval_seconds``（預設 86400s）強制一次不帶 ``since``、不帶
   ``If-None-Match`` 的全量重讀，與鏡像對帳；有 drift 一律**以全量為準**，並記
   log 與 ``observations["issue_sync"]["drift"]``。
6. **fail closed**：durable 狀態缺失／損壞／``since`` 格式不合／entries 形狀不對
   ——一律退回全量重建，**絕不**拿半壞的游標去做增量。

7. **D4 的 per-object ETag**：``targeted_etags`` 存 ``repos/{repo}/issues/{number}``
   這條 path 的條件請求 ETag，與清單端點的 ``etag`` 分開存——兩者 path 不同，
   混用會讓條件請求永遠落空。它綁的 path 不含 ``since``，因此游標前進不會讓它
   作廢；反過來，targeted 驗證讀回來的新狀態**不得**推進 ``since`` 游標（游標
   只能由清單回應推進，否則會跳過那之間被更新的其他物件）。

``IssueSyncStore`` 是 per-repo durable 狀態（游標／ETag／鏡像投影）的唯一入口。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from paulsha_cortex.config.paths import github_issue_sync_path

from .work_models import WorkSource


SYNC_SCHEMA = "github-issue-sync/v1"

# 每日一次全量對帳（計畫 R0.5 原則 4 明文）。
DEFAULT_FULL_SYNC_INTERVAL_SECONDS = 86_400.0

# GitHub 回傳的 ``updated_at``／要送回去的 ``since`` 一律是這個形狀。嚴格比對，
# 因為這個字串會直接進 query string；同時它是純字典序可比的，max() 即時間最大。
_API_TIMESTAMP = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

# ``gh api --include`` 的第一行；gh 用 Fprintln 寫它（結尾是 LF，不是 CRLF），
# 其餘 header 才是 CRLF。實測 gh 2.45：``HTTP/2.0 304 Not Modified``。
_STATUS_LINE = re.compile(r"\AHTTP/[0-9.]+ (?P<status>[0-9]{3})(?: |\Z)")

HTTP_NOT_MODIFIED = 304
HTTP_NOT_FOUND = 404


class IssueSyncStateError(ValueError):
    """durable 增量狀態不可信；呼叫端一律 fail closed 退回全量重建。"""


# ---------------------------------------------------------------------------
# request path
# ---------------------------------------------------------------------------


def issues_request_path(repo: str, *, since: str | None = None, page: int = 1) -> str:
    """本模組是 issues 讀取 path 的唯一產生器（ETag 以 path 為 key，不容分歧）。"""

    if page < 1:
        raise ValueError("issue page number must be >= 1")
    query = "state=all&per_page=100&sort=updated&direction=desc"
    if since is not None:
        if _API_TIMESTAMP.match(since) is None:
            raise IssueSyncStateError(f"issue cursor is not an API timestamp: {since!r}")
        query += f"&since={quote(since, safe='')}"
    if page > 1:
        # 刻意**不**跟隨回應 Link header 給的絕對 URL：那是伺服器可控字串，餵給
        # ``gh api`` 等於讓對方指定 token 要送去哪。Link 只當「還有下一頁」的
        # 布林訊號用，path 永遠由本地重建。
        query += f"&page={page}"
    return f"repos/{repo}/issues?{query}"


def issue_request_path(repo: str, number: int) -> str:
    """#506 / D4：單一物件的 targeted 請求 path。

    ``issues/{number}`` 對 issue 與 PR 都成立（PR 在 issues 端點回一份帶
    ``pull_request`` 鍵的物件），因此 D4 的 targeted 驗證不需要先知道被點名的是
    哪一種——事件帶的 ``kind`` 只進診斷。path 與清單端點不同，它的 ETag 因此
    **不會**隨 ``since`` 游標作廢，可以一直沿用到該物件真的變動為止。
    """

    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("GitHub object number must be a positive int")
    return f"repos/{repo}/issues/{number}"


# ---------------------------------------------------------------------------
# gh --include 回應
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GitHubResponse:
    """``gh api --include`` 的一次回應（狀態行 ＋ header ＋ body）。"""

    status: int
    headers: Mapping[str, str]
    body: str

    @property
    def etag(self) -> str | None:
        value = self.headers.get("etag")
        return value or None

    @property
    def not_modified(self) -> bool:
        return self.status == HTTP_NOT_MODIFIED

    @property
    def has_next_page(self) -> bool:
        link = self.headers.get("link", "")
        return any(
            'rel="next"' in part or "rel=next" in part for part in link.split(",")
        )


def parse_include_response(stdout: str) -> GitHubResponse:
    """解析 ``gh api --include`` 的輸出。

    刻意不做「沒有狀態行就當成純 body」的寬容退化：那會把一個缺 header 的回應
    靜默當成「單頁、無 ETag」，也就是**靜默截斷鏡像**。缺狀態行一律是錯誤。
    """

    total = len(stdout)
    index = 0

    def next_line() -> str:
        nonlocal index
        end = stdout.find("\n", index)
        if end < 0:
            line, index = stdout[index:], total
        else:
            line, index = stdout[index:end], end + 1
        return line.rstrip("\r")

    match = _STATUS_LINE.match(next_line())
    if match is None:
        raise ValueError("gh response is missing an HTTP status line")
    headers: dict[str, str] = {}
    while index < total:
        line = next_line()
        if not line:
            break
        name, separator, value = line.partition(":")
        if not separator or not name.strip():
            raise ValueError("gh response header line is malformed")
        headers[name.strip().lower()] = value.strip()
    return GitHubResponse(
        status=int(match.group("status")), headers=headers, body=stdout[index:]
    )


# ---------------------------------------------------------------------------
# 鏡像投影
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueEntry:
    """issues 回應中我們真正用到的欄位投影（durable 鏡像的一列）。

    ``labels`` 整份留著而非只留一個 auto-claim 布林：D1 的
    ``observations["auto_label_issues"]`` 是從鏡像導出的，label 常數改名或日後
    多看一個 label 時，不該被迫把全 fleet 的游標作廢重抓。
    """

    number: int
    title: str
    state: str
    node_id: str
    updated_at: str
    is_pull_request: bool = False
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.number, bool) or not isinstance(self.number, int):
            raise ValueError("invalid issue number")
        if self.number <= 0:
            raise ValueError("invalid issue number")
        for value in (self.title, self.state, self.node_id, self.updated_at):
            if not isinstance(value, str) or not value:
                raise ValueError("invalid GitHub entity fields")
        if _API_TIMESTAMP.match(self.updated_at) is None:
            raise ValueError("invalid GitHub updated_at timestamp")
        if not isinstance(self.is_pull_request, bool):
            raise ValueError("invalid pull_request flag")
        object.__setattr__(self, "labels", tuple(self.labels))
        if any(not isinstance(name, str) or not name for name in self.labels):
            raise ValueError("invalid GitHub label entry")

    @property
    def kind(self) -> str:
        return "github_pr" if self.is_pull_request else "github_issue"

    @classmethod
    def from_api(cls, entity: object) -> "IssueEntry":
        """由 issues 回應的一個物件建投影；欄位形狀不合一律 raise ValueError。

        與改動前的 ``_entity_source`` / ``_auto_label_issue_numbers`` 同一套嚴格
        度——半壞的回應整包降級成 ``malformed JSON``，不靜默吞掉。
        """

        if not isinstance(entity, Mapping):
            raise ValueError("GitHub entity is not an object")
        labels = entity.get("labels", [])
        if not isinstance(labels, list):
            raise ValueError("invalid GitHub labels field")
        names: list[str] = []
        for label in labels:
            if not isinstance(label, Mapping) or not isinstance(label.get("name"), str):
                raise ValueError("invalid GitHub label entry")
            names.append(label["name"])
        return cls(
            number=entity["number"],
            title=entity["title"],
            state=entity["state"],
            node_id=entity["node_id"],
            updated_at=entity["updated_at"],
            is_pull_request="pull_request" in entity,
            labels=tuple(names),
        )

    def to_source(self, *, repo: str, provider_id: str) -> WorkSource:
        ref = f"{repo}#{self.number}"
        return WorkSource(
            source_id=f"{self.kind}:{ref}",
            kind=self.kind,
            ref=ref,
            revision=f"github:{self.node_id}:{self.updated_at}",
            status=self.state,
            confidence="confirmed",
            provider=provider_id,
            title=self.title,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "number": self.number,
            "title": self.title,
            "state": self.state,
            "node_id": self.node_id,
            "updated_at": self.updated_at,
        }
        if self.is_pull_request:
            payload["is_pull_request"] = True
        if self.labels:
            payload["labels"] = list(self.labels)
        return payload

    @classmethod
    def from_dict(cls, payload: object) -> "IssueEntry":
        if not isinstance(payload, Mapping):
            raise IssueSyncStateError("issue entry must be an object")
        labels = payload.get("labels", [])
        if not isinstance(labels, list):
            raise IssueSyncStateError("issue entry labels must be an array")
        try:
            return cls(
                number=payload["number"],
                title=payload["title"],
                state=payload["state"],
                node_id=payload["node_id"],
                updated_at=payload["updated_at"],
                is_pull_request=bool(payload.get("is_pull_request", False)),
                labels=tuple(labels),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise IssueSyncStateError(f"issue entry is invalid: {error}") from error

    def differs_from(self, other: "IssueEntry") -> bool:
        return (
            self.state != other.state
            or self.updated_at != other.updated_at
            or self.title != other.title
            or self.node_id != other.node_id
            or self.is_pull_request != other.is_pull_request
            or self.labels != other.labels
        )


def _parse_local_timestamp(value: str) -> datetime:
    """解析我們自己時鐘寫下的 ISO-8601（帶不帶微秒都吃）。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise IssueSyncStateError(f"timestamp is not ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        raise IssueSyncStateError(f"timestamp is missing a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class IssueSyncState:
    """單一 repo 的 durable 增量狀態。"""

    repo: str
    entries: tuple[IssueEntry, ...] = ()
    since: str | None = None
    etag: str | None = None
    etag_request: str | None = None
    last_full_sync_at: str | None = None
    # D4：per-object targeted 請求的 ETag（``(number, etag)`` 對，依編號排序）。
    # 與清單端點的 ``etag`` 分開存：兩者的 request path 不同，混用會讓條件請求
    # 永遠落空。以 tuple 而非 dict 保存，frozen dataclass 才維持可雜湊。
    targeted_etags: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.repo, str) or self.repo.count("/") != 1:
            raise IssueSyncStateError("issue sync state repo must be owner/name")
        entries = tuple(self.entries)
        numbers = [entry.number for entry in entries]
        if len(set(numbers)) != len(numbers):
            raise IssueSyncStateError("issue sync state has duplicate issue numbers")
        object.__setattr__(self, "entries", tuple(sorted(entries, key=lambda e: e.number)))
        if self.since is not None and _API_TIMESTAMP.match(self.since) is None:
            raise IssueSyncStateError(
                f"issue cursor is not an API timestamp: {self.since!r}"
            )
        if (self.etag is None) != (self.etag_request is None):
            raise IssueSyncStateError("issue ETag must travel with its request path")
        if self.last_full_sync_at is not None:
            _parse_local_timestamp(self.last_full_sync_at)
        targeted: dict[int, str] = {}
        for pair in self.targeted_etags:
            try:
                number, etag = pair
            except (TypeError, ValueError) as error:
                raise IssueSyncStateError("targeted ETag must be a (number, etag) pair") from error
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                raise IssueSyncStateError(f"invalid targeted ETag number: {number!r}")
            if not isinstance(etag, str) or not etag:
                raise IssueSyncStateError(f"invalid targeted ETag value: {etag!r}")
            targeted[number] = etag
        object.__setattr__(
            self, "targeted_etags", tuple(sorted(targeted.items(), key=lambda row: row[0]))
        )

    @property
    def by_number(self) -> dict[int, IssueEntry]:
        return {entry.number: entry for entry in self.entries}

    @property
    def targeted_etags_by_number(self) -> dict[int, str]:
        return dict(self.targeted_etags)

    def with_targeted_etags(self, etags: Mapping[int, str]) -> "IssueSyncState":
        """換掉 per-object ETag 表，並丟掉鏡像裡已經沒有的物件那幾顆。

        物件從鏡像消失（被刪除／transfer 走）之後留著它的 ETag 只會單調長大，
        而那顆 ETag 也永遠不會再被送出。
        """

        live = self.by_number
        return replace(
            self,
            targeted_etags=tuple(
                sorted(
                    (number, etag) for number, etag in etags.items() if number in live
                )
            ),
        )

    def needs_full_sync(self, *, now: str, interval_seconds: float) -> bool:
        """是否該跑每日全量對帳。

        ``last_full_sync_at`` 缺失、無法解析、或落在**未來**（時鐘回捲／狀態被
        竄改）一律回 True——fail closed 寧可多跑一次全量。
        """

        if self.last_full_sync_at is None:
            return True
        try:
            last = _parse_local_timestamp(self.last_full_sync_at)
            current = _parse_local_timestamp(now)
        except IssueSyncStateError:
            return True
        elapsed = (current - last).total_seconds()
        return elapsed < 0 or elapsed >= interval_seconds

    def merged(self, delta: Sequence[IssueEntry]) -> tuple[IssueEntry, ...]:
        """把增量 delta 疊到鏡像上（同號覆蓋——delta 依定義較新）。"""

        merged = self.by_number
        for entry in delta:
            merged[entry.number] = entry
        return tuple(sorted(merged.values(), key=lambda e: e.number))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"entries": [entry.to_dict() for entry in self.entries]}
        for field_name in ("since", "etag", "etag_request", "last_full_sync_at"):
            value = getattr(self, field_name)
            if value is not None:
                payload[field_name] = value
        if self.targeted_etags:
            payload["targeted_etags"] = {
                str(number): etag for number, etag in self.targeted_etags
            }
        return payload

    @classmethod
    def from_dict(cls, repo: str, payload: object) -> "IssueSyncState":
        if not isinstance(payload, Mapping):
            raise IssueSyncStateError("issue sync state must be an object")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise IssueSyncStateError("issue sync state entries must be an array")
        optional: dict[str, str | None] = {}
        for field_name in ("since", "etag", "etag_request", "last_full_sync_at"):
            value = payload.get(field_name)
            if value is not None and not isinstance(value, str):
                raise IssueSyncStateError(f"issue sync state {field_name} must be a string")
            optional[field_name] = value
        return cls(
            repo=repo,
            entries=tuple(IssueEntry.from_dict(row) for row in entries),
            targeted_etags=_targeted_etags_from_dict(payload.get("targeted_etags")),
            **optional,
        )


def _targeted_etags_from_dict(payload: object) -> tuple[tuple[int, str], ...]:
    """D4：`{"123": "W/\\"abc\\""}` → `((123, 'W/"abc"'),)`；壞形狀 fail closed。

    與其餘 durable 欄位同一套紀律：半壞的 ETag 表會讓條件請求送出對不上的
    header，退化成每次 targeted 驗證都全額計費且毫無察覺，所以寧可整份 state
    退回全量重建。
    """

    if payload is None:
        return ()
    if not isinstance(payload, Mapping):
        raise IssueSyncStateError("issue sync state targeted_etags must be an object")
    rows: list[tuple[int, str]] = []
    for key, value in payload.items():
        try:
            number = int(key)
        except (TypeError, ValueError) as error:
            raise IssueSyncStateError(
                f"targeted ETag key is not an issue number: {key!r}"
            ) from error
        if number <= 0 or not isinstance(value, str) or not value:
            raise IssueSyncStateError(f"invalid targeted ETag for {key!r}")
        rows.append((number, value))
    return tuple(sorted(rows))


def dedupe_entries(entries: Sequence[IssueEntry]) -> tuple[IssueEntry, ...]:
    """同號去重，保留 ``updated_at`` 最新的那筆。

    分頁跑的是一個**活的**、依 ``updated`` 排序的清單：某個 issue 在我們讀第 1 頁
    與第 2 頁之間被更新，就會在兩頁各出現一次。這是分頁本身的產物，不是壞回應——
    但重複進到 :class:`IssueSyncState` 會直接 raise，因此在傳輸層就收斂掉。
    （反向的漏抓由每日全量 anti-entropy 兜底。）
    """

    latest: dict[int, IssueEntry] = {}
    for entry in entries:
        current = latest.get(entry.number)
        if current is None or entry.updated_at >= current.updated_at:
            latest[entry.number] = entry
    return tuple(sorted(latest.values(), key=lambda entry: entry.number))


def cursor_from(entries: Iterable[IssueEntry], *, floor: str | None = None) -> str | None:
    """游標＝回應中最大的 ``updated_at``（非本機時鐘），且永不倒退。

    ``updated_at`` 是零填充的 ``...THH:MM:SSZ``，字典序即時間序。
    """

    candidates = [entry.updated_at for entry in entries]
    if floor is not None:
        candidates.append(floor)
    return max(candidates) if candidates else None


def drift_between(
    previous: Sequence[IssueEntry], authoritative: Sequence[IssueEntry]
) -> dict[str, list[int]] | None:
    """全量對帳：鏡像相對於全量真相的偏差。無偏差回 ``None``。"""

    before = {entry.number: entry for entry in previous}
    after = {entry.number: entry for entry in authoritative}
    missing = sorted(set(before) - set(after))
    extra = sorted(set(after) - set(before))
    changed = sorted(
        number
        for number in set(before) & set(after)
        if after[number].differs_from(before[number])
    )
    if not (missing or extra or changed):
        return None
    return {"stale": missing, "unseen": extra, "changed": changed}


# ---------------------------------------------------------------------------
# durable store
# ---------------------------------------------------------------------------


class IssueSyncStore:
    """per-repo 游標／ETag／鏡像投影的 durable 存放點（單一檔案，repo 為 key）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else github_issue_sync_path()

    def _load_document(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"schema": SYNC_SCHEMA, "repos": {}}
        except (OSError, UnicodeError) as error:
            raise IssueSyncStateError(f"issue sync state unreadable: {error}") from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise IssueSyncStateError(f"issue sync state is not JSON: {error}") from error
        if not isinstance(payload, Mapping):
            raise IssueSyncStateError("issue sync state document must be an object")
        if payload.get("schema") != SYNC_SCHEMA:
            raise IssueSyncStateError(
                f"unsupported issue sync schema: {payload.get('schema')!r}"
            )
        repos = payload.get("repos")
        if not isinstance(repos, Mapping):
            raise IssueSyncStateError("issue sync state repos must be an object")
        return {"schema": SYNC_SCHEMA, "repos": dict(repos)}

    def load(self, repo: str) -> IssueSyncState | None:
        """回傳該 repo 的狀態；沒有紀錄回 ``None``，損壞則 raise。

        兩者的呼叫端行為相同（全量重建），分開只為了讓「第一次見到這個 repo」
        不會被記成一則 drift 診斷。單一 repo 的紀錄壞掉不會拖垮其他 repo；
        整份文件壞掉才會讓每個 repo 都退回全量。
        """

        record = self._load_document()["repos"].get(repo)
        if record is None:
            return None
        return IssueSyncState.from_dict(repo, record)

    def save(self, state: IssueSyncState) -> None:
        try:
            document = self._load_document()
        except IssueSyncStateError:
            # 正在覆寫的就是壞掉的那份文件——這是復原路徑，不是資料遺失。
            document = {"schema": SYNC_SCHEMA, "repos": {}}
        document["repos"][state.repo] = state.to_dict()
        self._write(document)

    def _write(self, document: Mapping[str, Any]) -> None:
        # 與 ``work_snapshot.WorkSnapshotStore._write_payload`` 同一套原子寫入
        # （temp + fsync + rename + dir fsync + 0600）。刻意不共用私有方法，
        # 避免為了 25 行工具碼把兩個 store 的生命週期綁在一起。
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        handle_fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            os.fchmod(handle_fd, 0o600)
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            directory_fd = os.open(
                self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.close(handle_fd)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise
