"""#499／#500／#487／#485：planning／build／review 三個 lane 共用的 outcome 分類 taxonomy。

Root cause（四張 issue 的同一個根）：executor 失敗「該歸哪一類」這件事在三個
lane 各自實作了一次——

- planning lane：`manager._is_planning_transient_service_failure`（#533 先行
  實作，本模組收編）。
- build lane：`provider_outcome.classify_provider_failure`（#384）。
- review lane：`manager._review_log_has_only_json_lines` ＋
  `manager._finalize_review_job` 的 absent 判定。

三份判準各自漂移，於是同一型缺陷被踩了六次：分類器把「不該當證據的文字」
（nested tool result、init metadata、CLI banner）餵進了關鍵字比對，或者反過來
把「該當證據的結構化終局記錄」整個忽略掉。四張 issue 的實測現場：

- **#499**：Claude stream-json 已有 `rate_limit_event.status = rejected`
  （`rateLimitType = five_hour`、`resetsAt`）＋終局 `api_error_status = 429`
  這種**機器可讀的**限流證據，review lane 卻完全不看，一律投影成
  `foreign-review-absent`／`provider_outcome = null`。
- **#500**：build lane 把 64 KiB log tail 整段丟給關鍵字比對，`\\btimeout\\b`
  命中了 nested tool-result 裡的 `Parser aborted (timeout, resource limit, or
  over-length)`，於是一個被 controller SIGTERM 中斷的 job 被判成可重試的
  network transient。
- **#487**：同一段 tail 含 Claude init 的 skill 清單，`oauth` 這個無界 pattern
  命中了正常技能名 `doc-coauthoring` 裡的 `coauthoring`，非 auth 失敗被判成
  不可重試的 auth 失敗。
- **#485**：Codex `exec --json` 會先印 `Reading additional input from
  stdin...` 這行 adapter 自有 banner，review lane 的 JSONL 純度檢查對每行做
  `json.loads()`，於是**每一次**成功的 Codex foreign review 都被判
  `invalid-process-output`。

本模組因此把分類拆成兩層，兩層都只有一份實作：

1. **證據分層**（:func:`parse_stream_evidence`）——先決定「什麼文字有資格當
   provider 層證據」。結構化終局記錄、CLI 原生 stderr、error 記錄屬
   provider 證據；模型自己的話（assistant/user 訊息）只拿來判 content；nested
   tool result 與 init metadata 兩者皆不是，直接丟棄。#500 與 #487 修的正是
   這一層——不是關鍵字表寫錯，是餵給關鍵字表的東西一開始就不該進來。
2. **共用 markers 表**（:data:`TRANSIENT_SERVICE_MARKERS` 等）與其上的
   :func:`classify_text`／:func:`classify_structured_evidence`——結構化證據
   優先於文字關鍵字；文字關鍵字的判定順序（rate limit → quota → auth →
   content → transient）沿用 #369/#370 的教訓，限流訊息常同時帶
   "authenticate"／"login" 字樣，rate limit 必須先判。

四大類 outcome family（:class:`OutcomeFamily`）是跨 lane 的共同語彙；各 lane
仍保有自己的既有詞彙（build lane 的六值 :class:`ProviderOutcome`、planning lane
的 content／environment 二值），由本模組提供對照表，**不改各 lane 對每一類
outcome 的後續處置**（retry／needs_human／終止一律維持現狀）——本模組只修
「分錯類」本身。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Sequence

from paulsha_cortex.github_rate_limit import is_auth_signal, is_rate_limit_signal

from . import executor_auth

__all__ = [
    "OutcomeFamily",
    "TextSignal",
    "StructuredKind",
    "TRANSIENT_SERVICE_MARKERS",
    "TRANSIENT_SERVICE_MARKER_RE",
    "INTERRUPTION_MARKERS",
    "KNOWN_PROCESS_BANNERS",
    "QUOTA_RE",
    "TRANSIENT_RE",
    "CONTENT_RE",
    "FAMILY_BY_TEXT_SIGNAL",
    "FAMILY_BY_STRUCTURED_KIND",
    "TextClassification",
    "StructuredSignal",
    "StreamEvidence",
    "matches_transient_service_markers",
    "strip_known_process_banners",
    "parse_stream_evidence",
    "classify_structured_evidence",
    "classify_text",
]


class OutcomeFamily(str, Enum):
    """跨 lane 的四大類 outcome 語彙（＋ UNKNOWN 哨兵）。

    - ``TRANSIENT_SERVICE``：服務層暫時性失敗（限流、503、逾時、連線層錯誤）。
      等時間窗過去重跑即可，不該燒世代、不該落死路。
    - ``CONTENT``：模型輸出本身的問題（內容政策拒答、回散文不回 JSON、schema
      不合）。重跑同一個 candidate 不會變，維持既有 fail-closed 意圖。
    - ``ENVIRONMENT``：本機／狀態層問題（殘留 worktree、額度需人工處理的帳單
      與方案上限）。要人動手，但不是模型內容缺陷。
    - ``AUTH``：憑證失效，需要人工重新登入。
    """

    TRANSIENT_SERVICE = "transient-service"
    CONTENT = "content"
    ENVIRONMENT = "environment"
    AUTH = "auth"
    UNKNOWN = "unknown"


class TextSignal(str, Enum):
    """文字關鍵字層的細分訊號——比 family 細，因為 build lane 的既有六值詞彙
    需要區分 rate_limit 與 quota（兩者同屬 environment/transient-service 家族，
    但 backoff 策略不同）。"""

    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    AUTH = "auth"
    CONTENT = "content"
    TRANSIENT = "transient"
    NONE = "none"


class StructuredKind(str, Enum):
    """結構化終局證據層的訊號。文字關鍵字沒有 ``INTERRUPTED``——「這個 job 是
    被 controller 中斷的」只有結構化終局記錄講得準（#500：中斷的 job 之所以被
    誤判成 transient，正是因為沒人看終局記錄）。"""

    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    AUTH = "auth"
    INTERRUPTED = "interrupted"


# --------------------------------------------------------------- 共用 markers 表

# planner launcher 的暫時性服務失敗樣態（原 `manager._PLANNING_TRANSIENT_SERVICE_MARKERS`，
# #533 先行實作，本模組收編為單一真源）。
#
# 實測（2026-08-14，run `workflow-88d089d71416a754dda8`）：agy 服務暫時回
# `Error: Eligibility check failed: UNAVAILABLE (code 503)`——**印錯誤文字但
# exit 0**，launcher 因此去 parse stdout、找不到 JSON，失敗以
# `primary-integration-malformed: ValueError: planning launcher returned no
# JSON object` 收場，預設分類 `content` → `recover-planning` 被 #393 的
# fail-closed 禁止 → 一個幾分鐘後自癒的 503 變成永久死路。
#
# 判準刻意窄：只認 CLI/service 層的暫時性錯誤樣態。模型「內容不從」（回散文
# 不回 JSON、schema 不合）不在此列，維持 `content` 分類與 fail-closed 意圖
# ——分辨依據是這些字樣出自 launcher 轉印的服務錯誤，不會出現在合法的規劃
# 輸出裡。
#
# #554：本表**以詞界比對**（見 :data:`TRANSIENT_SERVICE_MARKER_RE`），不再是
# 裸子字串。裸子字串比對是 #500／#487 同族的無界 token 缺陷——短 marker 對長
# 訊息的誤中面極大：
#   - `"503"`／`"429"` 會命中 run id／digest／路徑裡的任意數字片段
#     （`workflow-1a503f…`、`…/run-4290/report.json`）；
#   - `"unavailable"` 會命中 `envelope_unavailable`、`provider_unavailable`
#     這類與服務層無關的內部欄位值。
# 詞界化的代價是「靠子字串巧合命中的真陽性變體」會落空（`rate limited`、
# `TimeoutExpired`、`ServiceUnavailable`……）。那些是真訊號，因此改為**顯式
# 列舉**：表變長是刻意的——寧可每個變體都看得見，也不要再靠子字串巧合。
TRANSIENT_SERVICE_MARKERS: tuple[str, ...] = (
    "unavailable",
    "503",
    "429",
    "rate limit",
    # `rate limit` 的英文屈折變體。詞界化前靠 `rate limit` 的子字串涵蓋，
    # 詞界化後必須各自列出（provider 訊息實測三種都出現過）。
    "rate limits",
    "rate limited",
    "rate limiting",
    "too many requests",
    "timed out",
    "timeout",
    "timeouts",
    # CamelCase 例外類名。planning lane 的 reason 格式是
    # `<stage>-<kind>: <ExceptionTypeName>: <str(exc)[:160]>`，其中
    # `subprocess.TimeoutExpired` 的訊息（`Command '[...]' timed out after
    # N seconds`）常因 argv 過長而在 160 字截斷處被切掉「timed out」，此時
    # **只剩型別名帶得動訊號**。詞界化前靠 `timeout` 的子字串涵蓋。
    "timeouterror",
    "timeoutexpired",
    "connection reset",
    "connection refused",
    "overloaded",
    "temporarily",
    "service_unavailable",
    # 詞界化前靠 `unavailable` 的子字串涵蓋（`_` 與駝峰都算 word char，詞界
    # 比對不會從 `ServiceUnavailable` 中間切出 `unavailable`）。
    "serviceunavailable",
    "eligibility check failed",
)


def _compile_marker_pattern(markers: Sequence[str]) -> re.Pattern[str]:
    """把字面 marker 表編成**詞界**比對的單一 pattern（#554）。

    `\\b` 是「word char ↔ 非 word char 的交界」。因此：

    - `\\b503\\b` 不再命中 `1a503f`（前後都是 word char），但仍命中
      `code 503`、`(503)`、`HTTP/1.1 503`；
    - `\\bunavailable\\b` 不再命中 `envelope_unavailable`（`_` 是 word char），
      但**仍會命中 `<unavailable>`**（`<`／`>` 不是 word char）——詞界解決不了
      「整個 token 就是 marker」的佔位符相撞，那一半由呼叫端負責：佔位符本身
      不得含 marker（見 `planning_runtime.
      PLANNING_WORKTREE_DRIFT_EVIDENCE_PLACEHOLDER`）。兩邊都修才擋得住。

    長 marker 排前面只是為了讓 alternation 的命中片段可讀（本函式只回布林，
    順序不影響結果）。
    """

    ordered = sorted(markers, key=len, reverse=True)
    return re.compile(
        r"\b(?:" + "|".join(re.escape(marker) for marker in ordered) + r")\b",
        re.IGNORECASE,
    )


#: :data:`TRANSIENT_SERVICE_MARKERS` 的詞界比對 pattern（#554）。
TRANSIENT_SERVICE_MARKER_RE = _compile_marker_pattern(TRANSIENT_SERVICE_MARKERS)

# Quota：固定週期用量上限（月配額、bulk usage limit），與 rate_limit（滑動時間窗、
# 通常數十秒到數分鐘內重置）語意不同，值得分開分類以利未來對 quota 採不同的
# backoff 策略。
#
# 刻意不收 "quota exceeded"／"usage limit" 這兩個措辭：`executor_auth`
# 複用的 rate-limit 正則（見 executor_auth._RATE_LIMIT_RE／
# github_rate_limit._RATE_LIMIT_PATTERN）已經把它們算進 rate-limit（兩者
# 語意接近——都是「等時間窗過了就會恢復」），而 rate-limit 判定排在
# quota 檢查之前，故這兩個措辭實際上永遠命中不到這裡；本類別只收「不是等
# 時間窗、而是要人工處理（帳單、方案升級）」的措辭，避免死碼與誤導。
QUOTA_RE = re.compile(
    r"""
    monthly\ limit
    | plan\ limit
    | billing
    | insufficient\ credits?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Transient：網路/服務暫時性錯誤，與 rate limit 不同——這裡沒有「額度」語意，
# 純粹是這一次呼叫失敗，重試通常會成功。
#
# #500：`\btimeout\b` 這種無界 token 本身沒寫錯，錯的是**餵什麼文字進來**。
# 修法在 `parse_stream_evidence`：nested tool result（例如
# `Parser aborted (timeout, resource limit, or over-length)` 這種工具層診斷）
# 不屬 provider 證據，根本不會走到這張表。
TRANSIENT_RE = re.compile(
    r"""
    connection\ reset
    | econnreset
    | timed?\ ?out
    | \btimeout\b
    | temporarily\ unavailable
    | service\ unavailable
    | bad\ gateway
    | gateway\ time-?out
    | \b50[0234]\b
    | network\ error
    | dns\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Content：模型基於內容政策拒答，屬於「這次呼叫本身不該被無腦重試」的類別。
CONTENT_RE = re.compile(
    r"""
    content\ (policy|filtered)
    | refus(e|ed|ing)\ to\ (assist|help|continue)
    | cannot\ assist
    | violates\ (our|the)\ (usage\ )?polic
    | safety\ (guidelines|system|filter)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# #500：controller 中斷（SIGTERM 停掉 no-progress 迴圈）的終局樣態。命中者是
# 「這個 job 被我們自己停掉」，既不是 provider 失敗也不是模型內容缺陷——不得
# 被判成可重試的 transient，否則 recovery policy 會排錯重試、真正的
# no-progress/tool-use 缺陷被 provider-degraded 標籤蓋掉。
INTERRUPTION_MARKERS: tuple[str, ...] = (
    "aborted_streaming",
    "request interrupted by user",
    "interrupted by user",
)

# #485：adapter 自有、且會出現在 evidence log 裡的 banner。**只收精確字面值**
# ——這張表存在的目的是讓「已知的、adapter 自己印的那一行」不再讓整份合法
# JSONL 被判 invalid-process-output，而不是放寬 JSONL 純度檢查本身：任何不在
# 這張表上的非 JSON 文字仍然 fail closed。
#
# 實測環境：Codex CLI 0.147.0，`codex exec ... --json`。
KNOWN_PROCESS_BANNERS: tuple[str, ...] = ("Reading additional input from stdin...",)

# 文字訊號 → 四大 family 的對照。quota 落 ENVIRONMENT 而非 TRANSIENT_SERVICE：
# 固定週期額度不會「等一下就好」，要人去處理帳單／方案，語意上與服務暫時性
# 失敗相反。
FAMILY_BY_TEXT_SIGNAL: dict[TextSignal, OutcomeFamily] = {
    TextSignal.RATE_LIMIT: OutcomeFamily.TRANSIENT_SERVICE,
    TextSignal.QUOTA: OutcomeFamily.ENVIRONMENT,
    TextSignal.AUTH: OutcomeFamily.AUTH,
    TextSignal.CONTENT: OutcomeFamily.CONTENT,
    TextSignal.TRANSIENT: OutcomeFamily.TRANSIENT_SERVICE,
    TextSignal.NONE: OutcomeFamily.UNKNOWN,
}

FAMILY_BY_STRUCTURED_KIND: dict[StructuredKind, OutcomeFamily] = {
    StructuredKind.RATE_LIMITED: OutcomeFamily.TRANSIENT_SERVICE,
    StructuredKind.TRANSIENT: OutcomeFamily.TRANSIENT_SERVICE,
    StructuredKind.AUTH: OutcomeFamily.AUTH,
    # 中斷不是四大類的任何一類——它是「我們自己停的」，故落 UNKNOWN 哨兵，
    # 由呼叫端維持既有的不自動重試處置。
    StructuredKind.INTERRUPTED: OutcomeFamily.UNKNOWN,
}


def matches_transient_service_markers(reason: str | None) -> bool:
    """判斷一段失敗描述是否命中服務層暫時性樣態（:data:`TRANSIENT_SERVICE_MARKERS`）。

    planning lane（`manager._is_planning_transient_service_failure`）的判準即
    本函式；收編自 #533 的先行實作。

    #554：比對由裸子字串改為**詞界**（:data:`TRANSIENT_SERVICE_MARKER_RE`），
    真陽性樣態逐一保留在表內、不靠子字串巧合。
    """

    if reason is None:
        return False
    return TRANSIENT_SERVICE_MARKER_RE.search(reason) is not None


def strip_known_process_banners(lines: Sequence[str]) -> list[str]:
    """剝離 evidence log **開頭**的已知 adapter banner（#485）。

    只剝離開頭連續、且與 :data:`KNOWN_PROCESS_BANNERS` 精確相等（去除首尾空白
    後）的行；一碰到任何其他內容就停止。兩個刻意的窄化：

    - **精確字面值**：非 JSON 的意外雜訊仍然留在原地讓上游 fail closed
      （#485 acceptance：`Unexpected non-JSON text still fails closed`）。
    - **只看開頭**：banner 的語意就是「JSONL 串流開始前印的那一行」。出現在
      串流中段的同一句話並非 banner，不予剝離。
    """

    kept: list[str] = []
    for index, line in enumerate(lines):
        candidate = line.strip()
        if not candidate:
            kept.append(line)
            continue
        if candidate in KNOWN_PROCESS_BANNERS:
            continue
        return kept + list(lines[index:])
    return kept


# --------------------------------------------------------------- 證據分層

# Claude stream-json 的 init 記錄（`{"type":"system","subtype":"init", ...}`）
# 帶 tools／slash_commands／skills 清單。#487：正常技能名 `doc-coauthoring`
# 裡的 `coauthoring` 命中了無界的 `oauth` pattern，一次無關的工具失敗因此被判
# 成不可重試的 auth 失敗。init 是啟動 metadata，不是任何失敗的證據——整筆丟棄。
_INIT_RECORD_TYPE = "system"
_INIT_RECORD_SUBTYPE = "init"

# assistant/user 訊息裡的 content block：`tool_use`／`tool_result` 是工具層的
# 輸入輸出，既不是 provider 診斷也不是模型自己的話。#500 的 `Parser aborted
# (timeout, ...)` 正是一筆 permission-denied 的 `tool_result`。
_TOOL_BLOCK_TYPES = frozenset({"tool_use", "tool_result"})

# 模型自己的話：只拿來判 content-policy 拒答，不得驅動 transient／auth／
# rate-limit 判定（#500：`distinguish provider/API diagnostics from model prose`）。
_MODEL_RECORD_TYPES = frozenset({"assistant", "user"})

_TERMINAL_RECORD_TYPE = "result"

# HTTP status → 結構化訊號。刻意只收沒有歧義的三種：429 限流、5xx 服務端
# 暫時性失敗、401 憑證失效。403 不收——GitHub 用它表限流、其他 provider 用它
# 表權限，沒有共識時交給文字層判。
_STRUCTURED_KIND_BY_HTTP_STATUS: dict[int, StructuredKind] = {
    401: StructuredKind.AUTH,
    429: StructuredKind.RATE_LIMITED,
    500: StructuredKind.TRANSIENT,
    502: StructuredKind.TRANSIENT,
    503: StructuredKind.TRANSIENT,
    504: StructuredKind.TRANSIENT,
    529: StructuredKind.TRANSIENT,
}

_RATE_LIMIT_EVENT_KEY = "rate_limit_event"
_RATE_LIMIT_REJECTED_STATUSES = frozenset({"rejected"})
_RESET_AT_KEYS = ("resetsAt", "resets_at", "reset_at", "resetAt")


@dataclass(frozen=True)
class StreamEvidence:
    """一份 executor log tail 拆出的分層證據（純資料）。

    ``provider_text``／``model_text`` 的分野即本模組存在的理由：關鍵字表沒寫
    錯，錯的是餵進去的東西。
    """

    provider_text: str
    model_text: str
    terminal: dict[str, Any] | None
    rate_limit_events: tuple[dict[str, Any], ...]
    structured: bool


@dataclass(frozen=True)
class TextClassification:
    signal: TextSignal
    detail: str

    @property
    def family(self) -> OutcomeFamily:
        return FAMILY_BY_TEXT_SIGNAL[self.signal]


@dataclass(frozen=True)
class StructuredSignal:
    kind: StructuredKind
    detail: str
    # #499：provider 給的權威重置時刻（Claude `rate_limit_event.resetsAt`，
    # epoch 秒）。保存下來，operator 與無人值守流程才不必自己翻 JSONL、也不會
    # 在權威重置時刻之前就重試。
    reset_at: int | None = None

    @property
    def family(self) -> OutcomeFamily:
        return FAMILY_BY_STRUCTURED_KIND[self.kind]


def _text_blocks(record: dict[str, Any]) -> list[str]:
    """從 assistant/user 記錄抽出模型自己的話，跳過 tool_use／tool_result。"""

    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else record.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _TOOL_BLOCK_TYPES:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _collect_rate_limit_events(record: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    nested = record.get(_RATE_LIMIT_EVENT_KEY)
    if isinstance(nested, dict):
        events.append(nested)
    if record.get("type") == _RATE_LIMIT_EVENT_KEY or record.get("subtype") == _RATE_LIMIT_EVENT_KEY:
        events.append(record)
    return events


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def parse_stream_evidence(output: str | None) -> StreamEvidence:
    """把 executor 的 log tail 拆成分層證據。

    非 JSON 行一律視為 provider 證據——那是 CLI 自己印到 stdout/stderr 的東西
    （agy 的 `Error: Eligibility check failed: UNAVAILABLE (code 503)` 即是），
    也讓「呼叫端直接丟一段純文字進來」的既有用法行為完全不變。

    64 KiB tail 的**第一行**常是被切半的 JSON。只要其他行解析得出 JSON 記錄，
    這種開頭殘行就是截斷產物而非 CLI 輸出，予以丟棄——否則被切半的 tool
    result 又會把 #500 的坑原樣搬回來。
    """

    text = output or ""
    lines = text.splitlines()
    records: list[dict[str, Any]] = []
    raw_lines: list[str] = []
    first_content_index: int | None = None
    first_content_parsed = False
    parsed_any = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if first_content_index is None:
            first_content_index = index
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            raw_lines.append(stripped)
            continue
        if isinstance(payload, dict):
            records.append(payload)
            parsed_any = True
            if index == first_content_index:
                first_content_parsed = True
        else:
            raw_lines.append(stripped)

    if parsed_any and not first_content_parsed and raw_lines:
        # 開頭殘行：丟棄（見 docstring）。
        raw_lines = raw_lines[1:]

    provider_chunks: list[str] = list(raw_lines)
    model_chunks: list[str] = []
    terminal: dict[str, Any] | None = None
    rate_limit_events: list[dict[str, Any]] = []

    for record in records:
        record_type = record.get("type")
        subtype = record.get("subtype")
        events = _collect_rate_limit_events(record)
        if events:
            rate_limit_events.extend(events)
            provider_chunks.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
            continue
        if record_type == _INIT_RECORD_TYPE and subtype == _INIT_RECORD_SUBTYPE:
            # #487：init metadata（skill/tool 清單）不是任何失敗的證據。
            continue
        if record_type in _MODEL_RECORD_TYPES:
            # #500：模型自己的話只判 content；nested tool result 直接跳過。
            model_chunks.extend(_text_blocks(record))
            continue
        if record_type == _TERMINAL_RECORD_TYPE:
            # 終局記錄是權威證據，整筆納入 provider 證據（後者仍是最後一筆勝出）。
            terminal = record
            provider_chunks.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
            continue
        provider_chunks.append(json.dumps(record, ensure_ascii=False, sort_keys=True))

    return StreamEvidence(
        provider_text="\n".join(provider_chunks),
        model_text="\n".join(model_chunks),
        terminal=terminal,
        rate_limit_events=tuple(rate_limit_events),
        structured=parsed_any,
    )


def _rejected_rate_limit_event(
    events: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], int | None] | None:
    for event in events:
        status = event.get("status")
        if isinstance(status, str) and status.strip().lower() in _RATE_LIMIT_REJECTED_STATUSES:
            reset_at = None
            for key in _RESET_AT_KEYS:
                reset_at = _coerce_int(event.get(key))
                if reset_at is not None:
                    break
            return event, reset_at
    return None


def _terminal_http_status(terminal: dict[str, Any]) -> int | None:
    for key in ("api_error_status", "status_code", "http_status"):
        status = _coerce_int(terminal.get(key))
        if status is not None:
            return status
    return None


def _terminal_is_interrupted(terminal: dict[str, Any]) -> bool:
    haystack = json.dumps(terminal, ensure_ascii=False, sort_keys=True).lower()
    return any(marker in haystack for marker in INTERRUPTION_MARKERS)


def classify_structured_evidence(evidence: StreamEvidence) -> StructuredSignal | None:
    """結構化終局證據的分類——沒有可判定的結構化訊號時回 ``None``（呼叫端退回
    文字關鍵字層）。

    判定順序：provider 明講的限流事件 → 終局 HTTP status → 中斷樣態。前兩者
    是 provider 對「發生了什麼」的權威陳述（#499 的 429 即在此浮現）；中斷樣態
    排最後，因為一個先被限流、後被我們停掉的 job，限流才是根因。
    """

    rejected = _rejected_rate_limit_event(evidence.rate_limit_events)
    if rejected is not None:
        event, reset_at = rejected
        limit_type = event.get("rateLimitType") or event.get("rate_limit_type")
        suffix = f" ({limit_type})" if isinstance(limit_type, str) and limit_type else ""
        return StructuredSignal(
            kind=StructuredKind.RATE_LIMITED,
            detail=f"structured rate_limit_event rejected{suffix}",
            reset_at=reset_at,
        )

    terminal = evidence.terminal
    if terminal is None:
        return None

    status = _terminal_http_status(terminal)
    kind = _STRUCTURED_KIND_BY_HTTP_STATUS.get(status) if status is not None else None
    if kind is not None:
        return StructuredSignal(
            kind=kind,
            detail=f"structured terminal result api status {status}",
        )

    if _terminal_is_interrupted(terminal):
        return StructuredSignal(
            kind=StructuredKind.INTERRUPTED,
            detail="structured terminal result reports controller interruption",
        )
    return None


def classify_text(
    *,
    exit_code: int,
    provider_text: str,
    model_text: str = "",
) -> TextClassification:
    """文字關鍵字層的共用分類器。

    判定順序沿用 #369/#370：rate limit 必須排在 auth 之前——GitHub／provider
    的限流訊息常同時帶 "authenticate"／"login" 字樣（例如「請重新授權以取得
    更高額度」），auth 先判會把可重試的限流誤判成死掉的憑證。

    ``model_text`` 只參與 content-policy 判定：模型的散文不是 provider 診斷
    （#500），但拒答本來就只會出現在模型自己的話裡。
    """

    cli_status, cli_detail = executor_auth.classify_cli_output(exit_code, provider_text)

    if cli_status == "rate_limited" or is_rate_limit_signal(provider_text):
        return TextClassification(
            TextSignal.RATE_LIMIT,
            f"rate limit signal detected in executor output ({cli_detail})",
        )
    if QUOTA_RE.search(provider_text):
        return TextClassification(TextSignal.QUOTA, "quota signal detected in executor output")
    if cli_status == "logged_out" or is_auth_signal(provider_text):
        return TextClassification(
            TextSignal.AUTH,
            f"auth/login signal detected in executor output ({cli_detail})",
        )
    if CONTENT_RE.search(provider_text) or CONTENT_RE.search(model_text):
        return TextClassification(
            TextSignal.CONTENT, "content-policy signal detected in executor output"
        )
    if TRANSIENT_RE.search(provider_text):
        return TextClassification(
            TextSignal.TRANSIENT, "transient/network signal detected in executor output"
        )
    return TextClassification(TextSignal.NONE, f"no definitive signal (exit {exit_code})")
