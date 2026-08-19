"""issue #684（#672 票 C）：planning capability probe 的跨 tick 結果快取。

## 為什麼要有這個檔

`build_production_planning_runtime()` 對**每一個** planning-capable identity 各跑一次
probe：`_probe_identity` 每次做兩次整棵 repo 的 `copytree`，`probe_agy_capability`
則是兩次 CLI 呼叫。這個函式由 `manager.run_auto_claim_scan()`（periodic tick，實機
`PSC_MANAGER_INTERVAL_SECONDS=600`）與 `apply_work_action()` 兩條路徑呼叫。

票 E（#686）把 planning 搬上降權 job 之後，**每一次 probe 就是一個 systemd unit
實例**（agy 是兩個）。沒有快取的話，那等於每 10 分鐘起一批 job 去問模型「你是誰」
——成本不可接受，而且那批 job 的產出在絕大多數輪次裡與上一輪逐字相同。

## 指紋：為什麼不能只放剖面名

design D-C／D5 明文要求指紋放**模板 unit 檔本身**而不是剖面名。理由是 #677 落地的
`PROFILE_LOCKED_KEYS`：兩份剖面的加固鍵逐字相同，剖面名相同**不代表** unit 內容
相同（operator 重新落檔、產生器升級、RWP 增列都會改 unit 而不改剖面名）。只認剖面
名會讓一個對新 unit 不成立的 `ready` 被沿用下去——而那正是「綠燈不承載語意」。

把 unit 檔的 `st_size/st_mtime_ns` 放進指紋，等於把**部署動作**與**快取失效**綁成
同一件事：operator 重跑產生器落新 unit 的那一刻，全部快取自動失效並重探，不需要
任何人記得清快取。

指紋另含 `PSC_JOB_RUNNER`：direct 與 job 兩種模式的執行環境（PATH／HOME／憑證／
seccomp／MDWE）完全不同，切換一次就必須重探。這同時是 plan 要求「票 F 的生產切換
必須一次到位、不能一半走 job」的機械保證——混用時兩種語意的結論不會在同一份快取裡
並存，因為它們的指紋不同。

## fail-closed：壞掉的快取永遠不會回答 ready

`not_claimable.load_ledger()`（PR #675）對壞檔 **raise**，理由是「靜默當成空的等於
把盲區再造一次」。本模組**刻意不同**——design D5 已寫明這處差異與理由：probe 快取
只是一份輔助紀錄，它壞掉時若把整個 planning 拖垮，等於一份輔助紀錄取得了它不該有的
否決權。因此改為「視為 miss ＋ 落一筆可辨識的診斷（`planning-probe-cache-unreadable`）」。

兩者是**同一條原則**（不得靜默產生有利答案）在不同後果下的兩種實作：

- `not_claimable`：壞檔 ⇒ raise（不可 claim 的項目必須查得到）；
- 本模組：壞檔 ⇒ miss ＋ 重探（**絕不**因為讀不到而回答 ready）。

「有利答案」在這裡就是 `ready`。本模組**沒有任何一條路徑**會在讀檔失敗、schema 不
符、指紋不符、TTL 過期、或 row 形狀不合時回傳一個 ready 的 `CapabilityProbe`——
:func:`ProbeCache.get` 只有一個 `return`，而它的輸入是一列**逐欄驗過**的 row。

## 為什麼快取不進任何 job 模板 unit 的 RWP

登記表資產 `planning-probe-cache` 的 writers／readers 只有 `Principal.MANAGER`
（Manager-owned、`<coordinator_root>/planning-probe-cache.json`）。job 不該知道別的
provider 的探測結果，更不該寫得動它——快取一旦可由 job 寫，「這個 provider 是
ready 的」就變成模型可以自證的東西。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from . import job_runner
from .model_identities import CapabilityProbe, IdentityRegistry, ModelIdentity

__all__ = [
    "CACHE_FILENAME",
    "CACHE_SCHEMA",
    "CACHE_UNREADABLE_REASON",
    "CACHE_UNWRITABLE_REASON",
    "DEFAULT_NOT_READY_TTL_SECONDS",
    "DEFAULT_READY_TTL_SECONDS",
    "FINGERPRINT_ABSENT",
    "FINGERPRINT_DIGEST_FIELDS",
    "FINGERPRINT_UNRESOLVED_PREFIX",
    "NOT_READY_TTL_ENV",
    "READY_TTL_ENV",
    "ProbeCache",
    "ProbeFingerprint",
    "cache_path",
    "compute_fingerprint",
    "roster_digest",
]


logger = logging.getLogger(__name__)


CACHE_SCHEMA = "cortex-planning-probe-cache/v1"
CACHE_FILENAME = "planning-probe-cache.json"

#: 快取檔讀不回來時落的**可辨識**診斷。與「probe 失敗」分開是硬性要求（design D5）
#: ——否則會出現「快取檔壞了，症狀卻報成 provider 不可用」這種把排查方向整個帶偏的
#: 誤報（#670 就是同一型的教訓）。
CACHE_UNREADABLE_REASON = "planning-probe-cache-unreadable"
#: 寫不進去時落的診斷。寫入失敗**不得**讓 planning 失敗：那一輪只是沒有快取。
CACHE_UNWRITABLE_REASON = "planning-probe-cache-unwritable"

READY_TTL_ENV = "PSC_PLANNING_PROBE_CACHE_READY_TTL_SECONDS"
NOT_READY_TTL_ENV = "PSC_PLANNING_PROBE_CACHE_NOT_READY_TTL_SECONDS"

#: ready 的預設保鮮期。成功不需要頻繁重確認——每一次重確認就是一批 job 的成本。
DEFAULT_READY_TTL_SECONDS = 3600
#: not-ready 的預設保鮮期。失敗要**快速**重試：暫時性的服務錯誤、限流、以及模型
#: 輸出的隨機不從，短時間內就會自己好；把它們鎖在一小時的快取裡等於讓一次抖動
#: 決定接下來六輪 tick 的 planning 拓撲。
DEFAULT_NOT_READY_TTL_SECONDS = 300

#: 指紋分量取不到時的標記。**只帶例外型別名**，不帶訊息——例外訊息會夾帶路徑與
#: env，型別名不會（這條邊界與 `model_identities.classify_probe_failure()` 依賴的
#: 是同一條，見票 A 的論證）。
FINGERPRINT_UNRESOLVED_PREFIX = "<unresolved:"
#: 指紋分量指向的檔案不存在。與 `<unresolved:…>` 是**兩件事**：前者是「查得到答案，
#: 答案是沒有這個檔」，後者是「連查都查不動」。憑證從無到有必須重探，因此兩者都要
#: 進指紋、且必須可分辨。
FINGERPRINT_ABSENT = "<absent>"

#: 參與 digest 計算的欄位。:class:`ProbeFingerprint` 另有兩個**不參與** digest 的
#: 診斷欄位（`resolved_binary`／`unit`），它們的內容已經包含在對應的 digest 欄位裡，
#: 單獨列出只是為了讓快取 row 不必再解析字串就能回答「當時解到哪一支、哪一份 unit」。
FINGERPRINT_DIGEST_FIELDS: tuple[str, ...] = (
    "job_runner_mode",
    "roster_digest",
    "executor_binary",
    "executor_credential",
    "hardening_profile",
    "template_unit",
)


def cache_path(coordinator_root: str | Path) -> Path:
    """`<coordinator_root>/planning-probe-cache.json`（登記表資產 `planning-probe-cache`）。

    與 `not_claimable.ledger_path()`／`provider_backoff._state_path()` 同一個慣例：
    路徑在本模組推導、由 `trust_root.registry` 以 `derived_in` 登記，權限則由
    `permgen` 依登記表機械產出（Manager-owned，writers／readers 只有 Manager）。
    """

    return Path(coordinator_root) / CACHE_FILENAME


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unresolved(exc: BaseException) -> str:
    return f"{FINGERPRINT_UNRESOLVED_PREFIX}{type(exc).__name__}>"


def roster_digest(registry: IdentityRegistry) -> str:
    """roster 解析結果的 canonical JSON 雜湊（design D5 的第五個指紋輸入）。

    來源刻意是**解析結果**而不是 overlay 檔的 mtime：overlay 可以被改了又改回來、
    也可以由 packaged roster 提供同一列，真正會改變 probe 結論的是「合併之後的
    候選集合長什麼樣」。`ModelIdentity.to_dict()` 是既有的 canonical 投影
    （`write_registry_file` 用的同一支），這裡不另造一份序列化。
    """

    payload = {
        "schema_version": registry.schema_version,
        "identities": [identity.to_dict() for identity in registry.identities],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _stat_marker(path: str, *, fields: tuple[str, ...]) -> str:
    """`<path>|k=v,…`；查得動但沒有這個檔回 `<path>|<absent>`，查不動回
    `<path>|<unresolved:…>`。**不讀任何內容。**

    憑證那一格只取 `st_size/st_mtime_ns` 是刻意的（design D5）：讀內容才能算雜湊，
    而那會讓 token 出現在本行程的記憶體與任何中間狀態裡。size＋mtime 已足以偵測
    refresh 與換帳號，而它們**不是**祕密。

    ## #727：`<absent>` 曾經同時代表兩件事

    :data:`FINGERPRINT_ABSENT` 與 :data:`FINGERPRINT_UNRESOLVED_PREFIX` 的分工在本
    模組開頭寫得很清楚（「查得到答案，答案是沒有這個檔」vs「連查都查不動」），但本
    函式把**所有** `OSError` 都收成前者。實機 0819 因此逐字量到：

        fp.executor_credential = /var/lib/cortex-reviewer-planner/.claude/.credentials.json|<absent>

    ——**而那個檔已經存在**。`.claude` 是指向 `cache/claude` 的 symlink，`cache/` 是
    `0700 cortex-reviewer-planner`，由 Manager 身分 stat 會在 traverse 那一步拿到
    `EACCES`。指紋因此對 operator 說了一句假話（「沒有登入態」），而真正的狀況是
    「Manager 這個身分看不到那一格」。

    ⚠️ **這只修掉假話，沒有修掉那一格的真正缺陷**：無論落 `<absent>` 還是
    `<unresolved:PermissionError>`，兩者在同一個部署上都是**恆定**的 ⇒ **憑證輪替
    仍然不會讓 probe cache 失效**。要修那一條得換一個 Manager 看得見的失效訊號，
    或讓指紋由看得到那一格的身分算（＝每次算指紋多派一個降權 job，在 planning 的
    熱路徑上）——兩條都會擴散到部署面，因此 #727 只記錄、不順手做。
    """

    try:
        info = os.stat(path)
    except FileNotFoundError:
        return f"{path}|{FINGERPRINT_ABSENT}"
    except OSError as exc:
        return f"{path}|{FINGERPRINT_UNRESOLVED_PREFIX}{type(exc).__name__}>"
    parts = [f"{name}={getattr(info, f'st_{name}')}" for name in fields]
    return f"{path}|{','.join(parts)}"


_BINARY_STAT_FIELDS = ("dev", "ino", "size", "mtime_ns")
_FILE_STAT_FIELDS = ("size", "mtime_ns")


@dataclass(frozen=True)
class ProbeFingerprint:
    """一個 identity 在**當下這個部署**上的 probe 前提。

    「快取命中」的定義就是「指紋逐欄相同」——任何一格變了就重探。欄位少一個的
    失效模式是**沿用一個對新環境不成立的 ready**，那種失敗看起來是成功的。
    """

    job_runner_mode: str
    roster_digest: str
    executor_binary: str
    executor_credential: str
    hardening_profile: str
    template_unit: str
    #: 診斷用（不進 digest）：PATH 解析到的絕對路徑，供 D8 的拒因表指名「跑的是哪一支」。
    resolved_binary: str | None = None
    #: 診斷用（不進 digest）：本 identity 在 job 模式下會用的模板 unit 名。
    unit: str | None = None

    def as_dict(self) -> dict[str, str]:
        """digest 欄位的明表。存進 row 供 operator 直接看出「是哪一格變了」。"""

        return {name: getattr(self, name) for name in FINGERPRINT_DIGEST_FIELDS}

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()


def _search_path(mode: str, env: Mapping[str, str]) -> str:
    """本模式下 executor 會被**誰的** PATH 解析。

    - direct：probe 在 Manager 行程內跑，解析用的就是 Manager 自己的 `PATH`；
    - job：probe 在降權 job 裡跑，PATH 由 `resolve_job_path(role=review)` 宣告，
      未宣告即 fail-closed（#679）——那時指紋落 `<unresolved:JobRunnerError>`，
      而 PATH 一旦補上，指紋就變、快取自動失效重探。
    """

    if mode == job_runner.RUNNER_DIRECT:
        return env.get("PATH") or os.defpath
    return job_runner.resolve_job_path(env, role=job_runner.JOB_ROLE_REVIEW)


def _credential_path(mode: str, env: Mapping[str, str], executor: str) -> str:
    """本模式下**這個 executor** 的登入態落點。

    - direct：probe 用的是 Manager 行程的 `$HOME`，因此登入態就在那底下；
    - job：`permgen` 的部署決定欄位（`reviewer_planner_account`）導出。

    **票 D（#685）之後這裡是 per-(account, executor) 的。** 票 C 的原註解寫著
    「`executor_credential_relpath` 目前是單一值……票 D 把憑證面 codify 之後，這裡跟著
    `executor_credential_of` 的新簽章走即可，不必在本模組另造一張表」——本函式做的就是
    那件事：多傳一個 `executor`，形狀（單檔 vs 導進 `cache` 的狀態樹）由 permgen 那張表
    決定，本模組**不知道**也不需要知道差別。agy 的 `~/.gemini` 與 claude 的 `~/.claude`
    因此**現在進得了指紋**（票 C 當時明列的已知限制解除）。

    `(account, executor)` 不在表上時（例如 `copilot`，它不做 planning）permgen 會 raise
    `UnregisteredExecutorCredentialError`，由 `compute_fingerprint` 的 `_safe` 收成
    `<unresolved:UnregisteredExecutorCredentialError>`。那個標記是**穩定**的（同一個部署
    上恆定），因此快取照常運作；而它一旦被登記進表，指紋就變、快取自動失效——與
    「PATH 未宣告」那一格是同一條原則：**取不到答案本身也是一個會變的答案**。

    取的是 **token 葉檔**而不是登記表資產那個節點，兩者在三種形狀下都可能不同：
    `HOME_REDIRECT_TREE` 的資產是一條 symlink、`HOME_STICKY_TREE`（#698）的資產是一棵
    目錄——`stat` 它們只看得到目錄的 mtime，而 token 就地覆寫時目錄 mtime 不變 ⇒
    「憑證換了」偵測不到。

    **#698 修掉了本函式的一份複本**：direct 那一支原本自己拼 `relpath + token_leaf`，
    而 #698 讓 codex 那一列的樹與葉**都**由 `executor_credential_relpath` 的 head／tail
    導出（`relpath is None`／`token_leaf` 留空）。那份複本因此拼出目錄本身，指紋退化成
    「stat `~/.codex`」——refresh 偵測不到。改走 `credential_token_relpath_of()` 這個
    唯一來源之後，本模組同樣**不知道**也不需要知道三種形狀的差別。
    """

    from ..trust_root import permgen

    layout = permgen.DEFAULT_LAYOUT
    if mode == job_runner.RUNNER_DIRECT:
        home = (env.get("HOME") or "").strip()
        if not home:
            raise ValueError("HOME undeclared")
        # direct 模式跑在 Manager 帳號上，而 Manager **刻意沒有**任何登記表憑證資產
        # （#672 的核心裁決）。因此這裡取的是「同一個 executor 在 job 帳號上的相對
        # 落點」套到 Manager 的 HOME——那正是 direct 模式實際會解到的路徑。
        prefix = layout.credential_prefix_of(layout.reviewer_planner_account)
        credential = permgen.credential_for(prefix, executor)
        return os.path.join(home, layout.credential_token_relpath_of(credential))
    return layout.credential_token_path_of(layout.reviewer_planner_account, executor)


def compute_fingerprint(
    identity: ModelIdentity,
    *,
    roster: str,
    env: Mapping[str, str] | None = None,
) -> ProbeFingerprint:
    """算出這個 identity 的 probe 前提指紋。**本函式永不 raise。**

    永不 raise 是硬性要求：指紋是**輔助**設施，它算不出來時正確的行為是「這一格
    落一個可辨識的標記、快取視為 miss、照常重探」，不是把整條 planning 拖垮。
    每個分量各自守備，因此「PATH 未宣告」只會讓 `executor_binary` 那一格變成
    `<unresolved:JobRunnerError>`，其餘五格照常參與比對；而 PATH 補上之後那一格
    會變，快取隨之失效——**取不到答案本身也是一個會變的答案**。
    """

    environ = env if env is not None else os.environ

    def _safe(compute) -> str:
        try:
            return compute()
        except Exception as exc:  # noqa: BLE001 — 見 docstring：指紋不得拖垮 planning
            return _unresolved(exc)

    mode = _safe(lambda: job_runner.resolve_runner_mode(environ))
    resolved_binary: str | None = None
    unit_name: str | None = None

    def _binary() -> str:
        nonlocal resolved_binary
        found = shutil.which(identity.executor, path=_search_path(mode, environ))
        if found is None:
            return f"{identity.executor}|{FINGERPRINT_ABSENT}"
        resolved_binary = found
        return _stat_marker(found, fields=_BINARY_STAT_FIELDS)

    def _credential() -> str:
        return _stat_marker(
            _credential_path(mode, environ, identity.executor), fields=_FILE_STAT_FIELDS
        )

    def _profile() -> str:
        return job_runner.resolve_hardening_profile(identity.executor)

    def _template() -> str:
        nonlocal unit_name
        profile = job_runner.resolve_hardening_profile(identity.executor)
        base = job_runner.resolve_template_unit(environ, role=job_runner.JOB_ROLE_REVIEW)
        unit_name = job_runner.template_unit_for_profile(base, profile)
        return _stat_marker(
            os.path.join(job_runner.DEFAULT_TEMPLATE_UNIT_DIR, unit_name),
            fields=_FILE_STAT_FIELDS,
        )

    return ProbeFingerprint(
        job_runner_mode=mode,
        roster_digest=roster,
        executor_binary=_safe(_binary),
        executor_credential=_safe(_credential),
        hardening_profile=_safe(_profile),
        template_unit=_safe(_template),
        resolved_binary=resolved_binary,
        unit=unit_name,
    )


def _entry_key(executor: str, model_id: str) -> str:
    return f"{executor}::{model_id}"


def _empty() -> dict[str, Any]:
    return {"schema": CACHE_SCHEMA, "items": {}}


def _ttl_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    """TTL 的 env 覆寫。**非法值一律當 0（＝永遠 miss）**，不落回預設。

    落回預設會讓一個打錯的 `PSC_..._TTL_SECONDS=3O0` 靜默地維持一小時的快取，
    operator 以為自己調短了卻沒有——那是 #643／#679 反覆買過單的形態。當 0 的代價
    只是多付探測成本（而且 log 會指名），方向與本模組其餘部分一致：不確定就重探。
    """

    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.error("planning-probe-cache-ttl-invalid variable=%s value=%r", name, raw)
        return 0
    if value < 0:
        logger.error("planning-probe-cache-ttl-invalid variable=%s value=%r", name, raw)
        return 0
    return value


@dataclass
class ProbeCache:
    """`<coordinator_root>/planning-probe-cache.json` 的讀寫。**建構與讀取永不 raise。**

    形狀沿用 `not_claimable`（PR #675）：`schema` 版本字串 ＋ `items` 以穩定 key
    索引 ＋ 每筆帶 `first_observed_at`／`last_observed_at`／`observations`（operator
    因此看得出「這個 provider 掛多久了」）＋ 條件解除時自動清除（roster 不再有這個
    identity ⇒ :meth:`flush` 把它移掉）。原子寫入（temp ＋ `os.replace` ＋ 目錄
    fsync）也照抄 `not_claimable._save()`。

    唯一刻意的不同是「壞檔不得 raise」，理由見模組 docstring。
    """

    path: Path
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    clock: Any = time.time
    _items: dict[str, Any] = field(default_factory=dict, repr=False)
    #: 本次開檔時檔案是否讀不回來（壞檔／schema 不符）。呼叫端可據此在診斷面上把
    #: 「快取壞了」與「provider 不可用」分開。
    unreadable: bool = False
    _dirty: bool = False

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        env: Mapping[str, str] | None = None,
        clock: Any = time.time,
    ) -> "ProbeCache":
        target = Path(path)
        cache = cls(path=target, env=env if env is not None else os.environ, clock=clock)
        cache._load()
        return cache

    # -- 讀 -----------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._corrupt(f"{type(exc).__name__}")
            return
        if not isinstance(payload, dict):
            self._corrupt("payload-not-object")
            return
        if payload.get("schema") != CACHE_SCHEMA:
            self._corrupt(f"schema={payload.get('schema')!r}")
            return
        items = payload.get("items")
        if not isinstance(items, dict):
            self._corrupt("items-not-object")
            return
        self._items = {key: row for key, row in items.items() if isinstance(key, str)}

    def _corrupt(self, detail: str) -> None:
        """壞檔：視為空 ＋ 落一筆可辨識的診斷。**絕不 raise、絕不沿用任何 ready。**"""

        self.unreadable = True
        self._items = {}
        # 下一次 flush 會以「空的既有內容」為底重寫，等於就地修好這份檔案；在那之前
        # 這一輪的每一格都是 miss，全部重探。
        self._dirty = True
        logger.error("%s path=%s detail=%s", CACHE_UNREADABLE_REASON, self.path, detail)

    def ttl_seconds(self, *, ready: bool) -> int:
        if ready:
            return _ttl_from_env(self.env, READY_TTL_ENV, DEFAULT_READY_TTL_SECONDS)
        return _ttl_from_env(self.env, NOT_READY_TTL_ENV, DEFAULT_NOT_READY_TTL_SECONDS)

    def get(
        self, identity: ModelIdentity, *, fingerprint: ProbeFingerprint
    ) -> CapabilityProbe | None:
        """命中回一個與當初逐欄相同的 `CapabilityProbe`；任何不確定一律回 `None`。

        本函式只有一個回傳 `CapabilityProbe` 的出口，而它的輸入是一列**逐欄驗過**
        的 row。指紋不符、TTL 過期、`probed_at` 在未來（時鐘倒退）、row 形狀不合、
        身分欄位與 roster 對不上——全部回 `None`（＝miss ＝重探）。
        """

        row = self._items.get(_entry_key(identity.executor, identity.model_id))
        if not isinstance(row, dict):
            return None
        if row.get("fingerprint") != fingerprint.digest:
            return None
        ready = row.get("ready")
        if not isinstance(ready, bool):
            return None
        # 身分三欄必須與 roster 這一列逐字相同。roster digest 已在指紋裡，因此這是
        # 第二層；但快取 row 是**檔案**，而檔案是可以被就地改的——一份手動改過
        # `executor` 的 row 不該讓別人的 ready 落到這一格上。
        if (
            row.get("executor") != identity.executor
            or row.get("model_id") != identity.model_id
            or row.get("independence_domain") != identity.independence_domain
        ):
            return None
        probed_at = row.get("probed_at_epoch")
        if not isinstance(probed_at, (int, float)) or isinstance(probed_at, bool):
            return None
        age = float(self.clock()) - float(probed_at)
        if age < 0:
            # 時鐘倒退（NTP 校正、VM 還原）。fail-closed：寧可重探。
            return None
        ttl = self.ttl_seconds(ready=ready)
        if ttl <= 0 or age >= ttl:
            return None
        reason = row.get("reason")
        diagnostic = row.get("diagnostic")
        if reason is not None and not isinstance(reason, str):
            return None
        if diagnostic is not None and not isinstance(diagnostic, str):
            return None
        if ready and (reason is not None or diagnostic is not None):
            # ready 的 probe 依定義沒有失敗欄位（`CapabilityProbe.ready_for`）。
            # 一列同時帶 ready 與失敗診斷是矛盾的 row，fail-closed。
            return None
        return CapabilityProbe(
            ready,
            identity.executor,
            identity.model_id,
            identity.independence_domain,
            reason,
            diagnostic,
        )

    # -- 寫 -----------------------------------------------------------------

    def put(
        self,
        identity: ModelIdentity,
        probe: CapabilityProbe,
        *,
        fingerprint: ProbeFingerprint,
        now: str | None = None,
    ) -> dict[str, Any]:
        """記一次實際探測的結果（含失敗側的完整診斷，design D5）。

        `reason`／`diagnostic` **逐字沿用** `CapabilityProbe`，不在快取層再造一份
        節錄邏輯——#674 的 `stdout_excerpt()`／`strip_code_fence()` 已經是那件事的
        唯一真相，複製一份到這裡就是第二份。

        `returncode`／`stdout_prefix`／`binary_version` 三欄先立在 schema 上但目前
        恆為 `None`：它們的來源是票 E 的 `PlanningOutcome.diagnostics`（direct 模式
        沒有第二個資訊來源）。這不是缺口，是票的邊界——票 E 落地時只要填這三格，
        schema 版本不必動。
        """

        key = _entry_key(identity.executor, identity.model_id)
        observed_at = now if isinstance(now, str) and now else _utcnow()
        previous = self._items.get(key)
        # 「同一件事的第 N 次觀測」的判準：指紋、ready、reason 三者皆同。任何一格變
        # 了就是**新的**一件事，`first_observed_at` 重新起算——否則「這個 provider
        # 掛多久了」會把兩段不同原因的失敗接成一段，那個數字就不再是真話。
        same = (
            isinstance(previous, dict)
            and previous.get("fingerprint") == fingerprint.digest
            and previous.get("ready") is probe.ready
            and previous.get("reason") == probe.reason
        )
        row: dict[str, Any] = {
            "executor": identity.executor,
            "model_id": identity.model_id,
            "independence_domain": identity.independence_domain,
            "ready": bool(probe.ready),
            "reason": probe.reason,
            "diagnostic": probe.diagnostic,
            "family": (
                None
                if probe.ready or probe.reason is None
                else _classify(probe.reason, probe.diagnostic)
            ),
            # 票 E 的 D8 會填這三格（來源＝`PlanningOutcome.diagnostics`）。
            "returncode": None,
            "stdout_prefix": None,
            "binary_version": None,
            "unit": fingerprint.unit,
            "hardening_profile": fingerprint.hardening_profile,
            "resolved_binary": fingerprint.resolved_binary,
            "fingerprint": fingerprint.digest,
            "fingerprint_inputs": fingerprint.as_dict(),
            "probed_at": observed_at,
            "probed_at_epoch": float(self.clock()),
            "first_observed_at": (
                previous.get("first_observed_at")
                if same and isinstance(previous.get("first_observed_at"), str)
                else observed_at
            ),
            "last_observed_at": observed_at,
            "observations": (
                previous.get("observations", 0) + 1
                if same and isinstance(previous.get("observations"), int)
                else 1
            ),
        }
        self._items[key] = row
        self._dirty = True
        return dict(row)

    def flush(self, *, keep: Iterable[tuple[str, str]] | None = None) -> bool:
        """落盤。`keep` 給定時，**不在**該集合裡的 row 一併清除（自我收斂）。

        清除是 `not_claimable.clear()` 的同型：roster 移掉一個 identity 之後，它的
        探測結果沒有任何消費者，留著只會讓 operator 對著一筆永遠不會更新的紀錄猜。

        寫入前**重讀一次**磁碟並以它為底疊上本次的異動：兩個行程同時 miss 時，先寫
        的那一份不會被後寫的整份蓋掉。這**不是**鎖（見 :meth:`put` 上方的說明），
        只是把「並行的代價」收斂成「多探一次」，而不是「掉一批別人剛寫好的結果」。
        """

        keep_keys = (
            {_entry_key(executor, model_id) for executor, model_id in keep}
            if keep is not None
            else None
        )
        stale = (
            [key for key in self._items if key not in keep_keys]
            if keep_keys is not None
            else []
        )
        if not self._dirty and not stale:
            return False
        for key in stale:
            del self._items[key]
        merged = self._merged_payload(keep_keys)
        try:
            self._save(merged)
        except OSError as exc:
            logger.error(
                "%s path=%s error=%s", CACHE_UNWRITABLE_REASON, self.path, type(exc).__name__
            )
            return False
        self._dirty = False
        return True

    def _merged_payload(self, keep_keys: set[str] | None) -> dict[str, Any]:
        """磁碟現況（讀不回來即空）＋ 本行程異動；`keep_keys` 之外的一律不留。"""

        disk = ProbeCache(path=self.path, env=self.env, clock=self.clock)
        disk._load()
        items: dict[str, Any] = {}
        for source in (disk._items, self._items):
            for key, row in source.items():
                if keep_keys is not None and key not in keep_keys:
                    continue
                items[key] = row
        return {"schema": CACHE_SCHEMA, "items": items}

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / f".{self.path.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            # Manager-owned 0600（登記表 `planning-probe-cache` 的宣告）。權限由
            # permgen 依登記表機械產出，這裡是**建檔當下**的同一個值——一份新建的
            # 快取不該在 permgen 下一次跑之前是 group-readable 的。
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    # -- 呈現面 -------------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        """穩定排序的 row 清單（診斷用；壞檔時為空）。"""

        return [dict(self._items[key]) for key in sorted(self._items)]


def _classify(reason: str, diagnostic: str | None) -> str:
    """票 A 的 `classify_probe_failure()`——延後 import 避免無謂的模組層耦合。"""

    from .model_identities import classify_probe_failure

    return classify_probe_failure(reason, diagnostic)
