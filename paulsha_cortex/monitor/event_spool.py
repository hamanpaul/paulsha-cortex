"""#506 / D4：monitor 的本機事件入口（spool）——事件是 hint，不是 authority。

## 為什麼要有這個入口

D1–D3 把 monitor 對 GitHub 的常態讀取壓到「每 repo 每日 26 次計費請求」，代價是
**發現延遲**：一件事情發生在 GitHub 上，最壞要等一個 refresh 週期（30 分鐘）才會
進鏡像。對「fleet 自己剛剛動過的物件」而言這個延遲是白等的——動手的是我們自己，
我們當下就知道哪個物件被動了。

D5 的 headless agent hook 會把「我剛動了 GitHub 物件」寫成一個事件檔丟進本模組的
spool；monitor 每輪把 spool 掃一遍，對**被點名的物件**做一次 targeted 條件請求，
驗證通過才更新鏡像。漏發的事件由 D3 已落地的每日全量 anti-entropy 兜底。

## 契約（D5 hook 依此實作，本模組是唯一真值）

1. **位置**：`monitor_event_spool_root()`（預設 `<agents>/monitor/event-spool/`，
   隨 `PSC_MONITOR_STATE_ROOT`／`PSC_AGENTS_ROOT` 移動）。壞事件檔隔離到同層的
   `quarantine/` 子目錄。
2. **每事件一檔**：`<emitted_at 壓平>-<event_id 前綴>.json`，UTF-8 JSON 物件。
   一檔一事件，因此消費是 per-file 的 `unlink`，不需要任何鎖或 offset 檔。
3. **原子寫入**：temp 檔（前綴 `.`，掃描時跳過）→ fsync → `os.replace` 進 spool。
   消費端因此**不可能**讀到半寫入的檔案。
4. **fire-and-forget 寫入端語意**：:meth:`EventSpool.emit` 不等任何回應、不與
   monitor 交握、**永不 raise**——寫失敗只回 ``None`` 並記 debug log。hook 是掛在
   別人（agent job）的工作路徑上的，spool 寫不進去絕不能影響工作本體；掉一則
   hint 的後果只是退回原本的 refresh 週期延遲，而那正是 anti-entropy 的守備範圍。
5. **信封欄位**（缺一即壞檔）：``schema_version``／``event_id``／``event_type``／
   ``emitted_at``／``source``；選配 ``job_id``（D5 的 `PSC_JOB_ID` 自守標記）與
   ``payload``。
6. **事件是 hint 不是 authority**：`github_object` 事件只帶「哪個 repo 的哪個編號
   被動了」，**不帶新狀態**。payload 裡的 ``action`` 純屬診斷，consumer 永不據以
   寫鏡像——鏡像只寫 GitHub 自己的回應。對應 ``correlation`` 既有的
   inferred→confirmed 語彙：spool hint 是 inferred 訊號，只有 targeted 驗證回來的
   物件才是 confirmed，才進鏡像。

## #498 擴充點

``event_type`` 是封閉列舉的**擴充位**：本次只消費 ``github_object``。
``steering``／``job``（#498 的 remote-control 佇列與 job 心跳）已在
:data:`RESERVED_EVENT_TYPES` 佔位，本模組掃到時**原地保留、只記 log 與計數**，
絕不刪除——那些事件屬於未來的另一個 consumer，這裡刪掉就是替它們決定生命週期。
同理，未知的 ``event_type`` 與未知的 ``schema_version``（較新的 producer 對上較舊
的 consumer）一律保留不動，只有**結構壞掉**的檔案才會被隔離。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from paulsha_cortex.config.paths import monitor_event_spool_root
from paulsha_cortex.coordinator.spool_slot import canonical_job_slot


logger = logging.getLogger(__name__)


EVENT_SCHEMA = "monitor-event-spool/v1"

#: 本次唯一會被消費的事件型別。
EVENT_TYPE_GITHUB_OBJECT = "github_object"

#: #498 預留：headless steering 佇列與 job 心跳。本模組只記 log、原地保留。
EVENT_TYPE_STEERING = "steering"
EVENT_TYPE_JOB = "job"
RESERVED_EVENT_TYPES = frozenset({EVENT_TYPE_STEERING, EVENT_TYPE_JOB})

#: `github_object` 事件的物件類型，與 ``IssueEntry.kind`` 同語彙。
GITHUB_OBJECT_KINDS = frozenset({"github_issue", "github_pr"})

#: 沒有任何 consumer 認領的事件超過這個歲數就隔離——configured repo 清單變動、
#: 或 producer 打錯 repo 名，都會留下永遠等不到 consumer 的孤兒事件。
DEFAULT_EVENT_TTL_SECONDS = 7 * 86_400.0

QUARANTINE_DIRNAME = "quarantine"

_EVENT_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class SpoolEventError(ValueError):
    """事件檔結構壞掉；呼叫端一律隔離該檔並繼續處理其餘事件。"""


def parse_event_timestamp(value: object) -> datetime:
    """解析事件時間戳。

    spool 是**本機**目錄，producer 與 consumer 共用同一顆時鐘，因此
    ``emitted_at`` 與 monitor 自己的 ``attempted_at`` 可以直接比大小——這是
    「事件過期」判定成立的前提（見 ``providers`` 的 targeted refresh）。
    """

    if not isinstance(value, str) or not value:
        raise SpoolEventError("event timestamp must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SpoolEventError(f"event timestamp is not ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        raise SpoolEventError(f"event timestamp is missing a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpoolEvent:
    """一則 spool 事件的信封。

    信封（誰、什麼型別、何時）與 ``payload``（型別自有的欄位）刻意分層：消費端
    先看得懂信封才決定要不要碰 payload，未知型別因此不必也不該被解析。
    """

    event_id: str
    event_type: str
    emitted_at: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    job_id: str | None = None
    schema_version: str = EVENT_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or _EVENT_ID.fullmatch(self.event_id) is None:
            raise SpoolEventError(f"invalid event_id: {self.event_id!r}")
        for name in ("event_type", "source", "schema_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SpoolEventError(f"event {name} must be a non-empty string")
        parse_event_timestamp(self.emitted_at)
        if not isinstance(self.payload, Mapping):
            raise SpoolEventError("event payload must be an object")
        if self.job_id is not None and (
            not isinstance(self.job_id, str) or not self.job_id.strip()
        ):
            raise SpoolEventError("event job_id must be a non-empty string when present")
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def emitted_at_time(self) -> datetime:
        return parse_event_timestamp(self.emitted_at)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "emitted_at": self.emitted_at,
            "source": self.source,
        }
        if self.job_id is not None:
            payload["job_id"] = self.job_id
        if self.payload:
            payload["payload"] = dict(self.payload)
        return payload

    @classmethod
    def from_dict(cls, document: object) -> "SpoolEvent":
        if not isinstance(document, Mapping):
            raise SpoolEventError("event file must contain a JSON object")
        payload = document.get("payload", {})
        if not isinstance(payload, Mapping):
            raise SpoolEventError("event payload must be an object")
        job_id = document.get("job_id")
        return cls(
            event_id=document.get("event_id"),  # type: ignore[arg-type]
            event_type=document.get("event_type"),  # type: ignore[arg-type]
            emitted_at=document.get("emitted_at"),  # type: ignore[arg-type]
            source=document.get("source"),  # type: ignore[arg-type]
            payload=payload,
            job_id=job_id,
            schema_version=document.get("schema_version"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class GitHubObjectHint:
    """一個被點名的 GitHub 物件（＋它的來源事件檔）。"""

    repo: str
    kind: str
    number: int
    event: SpoolEvent
    path: Path

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


def github_object_hint(event: SpoolEvent, path: Path) -> GitHubObjectHint:
    """由 ``github_object`` 事件取出被點名的物件；欄位不合一律當壞檔。"""

    payload = event.payload
    repo = payload.get("repo")
    if not isinstance(repo, str) or repo.count("/") != 1 or not all(repo.split("/")):
        raise SpoolEventError(f"github_object repo must be owner/name: {repo!r}")
    kind = payload.get("kind")
    if kind not in GITHUB_OBJECT_KINDS:
        raise SpoolEventError(f"unknown github_object kind: {kind!r}")
    number = payload.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise SpoolEventError(f"github_object number must be a positive int: {number!r}")
    return GitHubObjectHint(repo=repo, kind=kind, number=number, event=event, path=path)


# ---------------------------------------------------------------------------
# 去重／收斂
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetedRefresh:
    """同一個物件的所有 hint 收斂成的一次 targeted 驗證。"""

    repo: str
    kind: str
    number: int
    emitted_at: str
    paths: tuple[Path, ...]
    event_ids: tuple[str, ...]

    @property
    def emitted_at_time(self) -> datetime:
        return parse_event_timestamp(self.emitted_at)


def coalesce_hints(
    hints: Sequence[GitHubObjectHint], *, repo: str | None = None
) -> tuple[TargetedRefresh, ...]:
    """把 hint 收斂成 per-object 一次驗證。

    - **去重**：同一個物件被點名 N 次只驗一次（N 次編輯的結果就是一個當下狀態，
      逐次驗證是純粹的浪費）。所有貢獻事件的檔案路徑一併帶出——它們**共同**在
      驗證成功後才被消費，避免「驗了一次、只刪一個檔、下輪又重驗」。
    - **亂序**：事件之間沒有全域順序（不同 producer 行程各寫各的），本函式因此
      不做任何順序推論，只取最大的 ``emitted_at`` 作為這次收斂的水位。
    - 回傳依（最舊事件先、其次編號）排序：per-cycle 上限截斷時先服務等最久的。
    """

    grouped: dict[tuple[str, int], TargetedRefresh] = {}
    for hint in hints:
        if repo is not None and hint.repo != repo:
            continue
        key = (hint.repo, hint.number)
        current = grouped.get(key)
        if current is None:
            grouped[key] = TargetedRefresh(
                repo=hint.repo,
                kind=hint.kind,
                number=hint.number,
                emitted_at=hint.event.emitted_at,
                paths=(hint.path,),
                event_ids=(hint.event.event_id,),
            )
            continue
        newer = hint.event.emitted_at_time > current.emitted_at_time
        grouped[key] = TargetedRefresh(
            repo=current.repo,
            # 同編號被兩種 kind 點名時以較新的事件為準；`issues/{number}` 對
            # issue 與 PR 是同一個端點，kind 只影響診斷字串。
            kind=hint.kind if newer else current.kind,
            number=current.number,
            emitted_at=hint.event.emitted_at if newer else current.emitted_at,
            paths=(*current.paths, hint.path),
            event_ids=(*current.event_ids, hint.event.event_id),
        )
    return tuple(
        sorted(
            grouped.values(),
            key=lambda refresh: (refresh.emitted_at_time, refresh.number),
        )
    )


# ---------------------------------------------------------------------------
# 掃描結果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpoolScan:
    """一次 spool 掃描的全貌。"""

    hints: tuple[GitHubObjectHint, ...] = ()
    ignored: Mapping[str, int] = field(default_factory=dict)
    foreign_schema: int = 0
    quarantined: tuple[str, ...] = ()
    unreadable: int = 0

    def for_repo(self, repo: str) -> tuple[GitHubObjectHint, ...]:
        return tuple(hint for hint in self.hints if hint.repo == repo)


# ---------------------------------------------------------------------------
# spool
# ---------------------------------------------------------------------------


class EventSpool:
    """本機事件目錄的唯一入口（寫入端與消費端共用）。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        job_id: str | None = None,
        ttl_seconds: float = DEFAULT_EVENT_TTL_SECONDS,
    ) -> None:
        base = Path(root) if root is not None else monitor_event_spool_root()
        # The monitor consumes the shared root; a job producer must be explicitly
        # bound to the Manager-selected slot and can never write the shared root.
        self.root = (
            canonical_job_slot("monitor-event-spool", job_id, writable_root=base)
            if job_id is not None
            else base
        )
        self.ttl_seconds = float(ttl_seconds)

    @property
    def quarantine_root(self) -> Path:
        return self.root / QUARANTINE_DIRNAME

    # -- 寫入端（fire-and-forget；D5 hook 走這裡）--------------------------

    def emit(self, event: SpoolEvent) -> Path | None:
        """原子寫入一則事件；**永不 raise**，失敗回 ``None``。

        寫入端掛在別人的工作路徑上（agent job 的 hook），因此這裡沒有任何
        「回報失敗給呼叫者處理」的語意——掉一則 hint 只是退回 refresh 週期的
        發現延遲，而讓 hook 的例外炸掉 job 本體是完全不成比例的代價。
        """

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            body = (
                json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            # temp 檔一律 `.` 前綴：掃描端跳過 dotfile，因此半寫入的檔案在
            # rename 之前對 consumer 不可見。
            handle_fd, temp_name = tempfile.mkstemp(
                prefix=".event-", suffix=".tmp", dir=self.root
            )
            temp_path = Path(temp_name)
            try:
                os.fchmod(handle_fd, 0o600)
                with os.fdopen(handle_fd, "wb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.root / _event_filename(event))
            except BaseException:
                try:
                    os.close(handle_fd)
                except OSError:
                    pass
                temp_path.unlink(missing_ok=True)
                raise
        except Exception as error:  # noqa: BLE001 - fire-and-forget 的全部意義
            # 目錄不可寫、磁碟滿、路徑被換成檔案……一律吞掉。刻意用 debug：
            # 寫入端可能是每個 tool call 都跑一次的 hook，warning 會變成噪音源。
            logger.debug("monitor event spool write dropped: %s", error)
            return None
        return self.root / _event_filename(event)

    def emit_github_object(
        self,
        *,
        repo: str,
        kind: str,
        number: int,
        source: str,
        action: str | None = None,
        job_id: str | None = None,
        now: str | None = None,
    ) -> Path | None:
        """D5 hook 的便利入口：宣告「這個 GitHub 物件剛被動過」。

        刻意**不收**新狀態（state／labels／body）：事件是 hint，鏡像只寫 GitHub
        自己回的內容。``action`` 只進診斷。
        """

        payload: dict[str, Any] = {"repo": repo, "kind": kind, "number": number}
        if action is not None:
            payload["action"] = action
        try:
            event = SpoolEvent(
                event_id=uuid.uuid4().hex,
                event_type=EVENT_TYPE_GITHUB_OBJECT,
                emitted_at=now or _utcnow(),
                source=source,
                payload=payload,
                job_id=job_id,
            )
        except SpoolEventError as error:
            logger.debug("monitor event spool rejected a malformed event: %s", error)
            return None
        return self.emit(event)

    # -- 消費端 -----------------------------------------------------------

    def scan(self, *, now: str | None = None) -> SpoolScan:
        """掃一遍 spool；**永不 raise**。

        壞檔在這裡就被隔離走，因此一個壞檔不會擋住同一輪的其他事件。目錄不存在
        （D5 尚未落地、或這台機器沒有 hook）回空掃描，且**不建目錄**——只有寫入端
        才有理由讓 spool 目錄出現。
        """

        try:
            paths: list[Path] = []
            for entry in os.scandir(self.root):
                if entry.name.startswith(".") or entry.name == QUARANTINE_DIRNAME:
                    continue
                if entry.is_file(follow_symlinks=False):
                    # Backward-compatible harvest of pre-isolation events.
                    paths.append(self.root / entry.name)
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                # Job slots are exactly one level below the shared root.  Never
                # follow a slot or child symlink and never recurse by prefix.
                for child in os.scandir(entry.path):
                    if child.is_file(follow_symlinks=False) and not child.name.startswith("."):
                        paths.append(Path(entry.path) / child.name)
            paths.sort(key=lambda path: str(path.relative_to(self.root)))
        except FileNotFoundError:
            return SpoolScan()
        except OSError as error:
            logger.warning("monitor event spool is unreadable: %s", error)
            return SpoolScan(unreadable=1)

        hints: list[GitHubObjectHint] = []
        ignored: dict[str, int] = {}
        quarantined: list[str] = []
        foreign = 0
        horizon = parse_event_timestamp(now) if now else datetime.now(timezone.utc)
        for path in paths:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # 另一個 consumer（或另一個 repo 的 provider）剛消費掉它。
                continue
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                quarantined.append(self._quarantine(path, reason=str(error)))
                continue
            schema = document.get("schema_version") if isinstance(document, Mapping) else None
            if isinstance(schema, str) and schema and schema != EVENT_SCHEMA:
                # 較新的 producer 對上較舊的 consumer：不是壞檔，也不是我們的。
                foreign += 1
                continue
            try:
                event = SpoolEvent.from_dict(document)
            except SpoolEventError as error:
                quarantined.append(self._quarantine(path, reason=str(error)))
                continue
            if (horizon - event.emitted_at_time).total_seconds() > self.ttl_seconds:
                # 沒有任何 consumer 認領（repo 打錯／已從 configured 清單移除）的
                # 孤兒事件，否則 spool 只會單調長大。隔離而非刪除：這是要給人看的。
                quarantined.append(self._quarantine(path, reason="expired"))
                continue
            if event.event_type != EVENT_TYPE_GITHUB_OBJECT:
                # #498 擴充點：steering／job／未知型別一律**原地保留**，只計數。
                ignored[event.event_type] = ignored.get(event.event_type, 0) + 1
                continue
            try:
                hints.append(github_object_hint(event, path))
            except SpoolEventError as error:
                quarantined.append(self._quarantine(path, reason=str(error)))
        if ignored:
            logger.info(
                "monitor event spool holding %s unconsumed event(s) by type: %s",
                sum(ignored.values()),
                ignored,
            )
        return SpoolScan(
            hints=tuple(hints),
            ignored=dict(ignored),
            foreign_schema=foreign,
            quarantined=tuple(quarantined),
        )

    def consume(self, paths: Iterable[Path]) -> int:
        """處理成功後才呼叫：移除這些事件檔。回傳實際移除數。

        移除失敗不 raise——最差的後果是下一輪重跑一次 targeted 驗證，而驗證本身
        是冪等的（條件請求命中 304，連配額都不花）。
        """

        removed = 0
        for path in paths:
            try:
                Path(path).unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                logger.warning("monitor event spool could not consume %s: %s", path, error)
                continue
            removed += 1
        return removed

    def _quarantine(self, path: Path, *, reason: str) -> str:
        """把壞檔移進 ``quarantine/``；移不動就留著並計數，絕不刪除證據。"""

        logger.warning("monitor event spool quarantining %s: %s", path.name, reason)
        try:
            self.quarantine_root.mkdir(parents=True, exist_ok=True)
            os.replace(path, self.quarantine_root / path.name)
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning("monitor event spool could not quarantine %s: %s", path.name, error)
        return path.name


def _event_filename(event: SpoolEvent) -> str:
    """``<emitted_at 壓平>-<event_id 前綴>.json``。

    時間戳打頭讓 `ls` 就是時間序（僅便於人工 triage——**消費端不依賴檔名順序**，
    順序一律取自 ``emitted_at`` 欄位）。名稱全程過濾成 ``[A-Za-z0-9._-]``，
    事件內容因此無法影響它落在哪個路徑。
    """

    stamp = _UNSAFE_NAME.sub("", event.emitted_at.replace(":", "").replace("-", ""))
    return f"{stamp or 'unknown'}-{event.event_id[:16]}.json"
