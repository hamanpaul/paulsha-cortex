"""#506：GitHub 請求壓力閘門——把掃描 burst 攤平，並在限流時退避。

## 問題

Monitor 的 ``_github_refresh_loop``（``monitor/service.py``）每
``github_refresh_interval_seconds``（預設 300s）會以 ``include_github=True``
跑一次 work model refresh，對 workspace 內**每個** GitHub repo 各跑一次
``GitHubWorkProvider.scan()`` 與 ``GitHubTerminalProvider.scan()``。單一 repo
單輪的 ``gh`` 呼叫並不是 O(1)：

- ``GitHubWorkProvider``：1 次 ``gh api --paginate``（issues；gh 內部再分頁）
- ``GitHubTerminalProvider``：1 次 graphql（PR，可再分頁）＋ 1 次 git tree
  ＋ **每個** remote todo／archived tasks.md 一次 ``contents`` ＋ **每個**
  workflow-linked merged PR 一次 ``compare``

  .. note:: #506 / D2 之後，上面那兩項「每個……一次」已改走本機 git
     （``monitor/git_mirror``），不再消耗 REST 配額；``GitHubTerminalProvider``
     的 REST 只剩 graphql 分頁與 1 次 git tree。下面的問題描述與預算計算保留
     當時的實測基準，因為節流／退避機制本身沒有變。

  .. note:: #506 / D3 之後，``GitHubWorkProvider`` 改走
     ``state=all&since=`` ＋ ETag 條件請求的增量協定（``monitor/github_issue_sync``），
     穩態下每輪每 repo 是 **1 次免費 304**；全量只在每日一次的 anti-entropy
     對帳、以及游標／ETag 狀態損壞的 fail-closed 重建時發生。分頁也從
     ``--paginate``（gh 在行程內自己連發，閘門完全管不到）改成本地逐頁重建，
     因此**每一頁**都會經過 :meth:`GitHubPressureGate.throttle`。

亦即 per-repo per-cycle 是 O(issues 分頁 + todo 檔數)。實際 workspace 約 40 個
repo，一輪數百次請求在數秒內齊發，穩定觸發 GitHub secondary（abuse detection）
rate limit——實測 ``github:`` 與 ``github-terminal:`` 兩個 provider 同時
degraded 超過 35 分鐘，operator 的 ``cortex work`` 全被 ``coordinator/claim.py``
的 ``provider-authority-rate-limited-canonical`` 擋下（dogfooding 死結）。

## 本模組提供的兩個機制

1. **節流（攤平）**：每次 ``gh`` 請求前插入 ``interval + jitter`` 的間隔，讓
   一輪的數百次請求攤平而非齊發。GitHub 的 secondary limit 主要抓「短時間內
   的併發／連發」，攤平比降低總量更直接有效。
2. **退避**：命中 rate limit 後記錄 next-attempt 時點，退避期間 provider 的
   ``scan()`` 直接跳過（**不發任何請求**），而不是每輪硬撞一次 403。

### 節流預算計算（務必隨參數調整同步更新）

預設 ``interval=0.2s``：40 repo × 約 5 次呼叫／repo × 0.2s ≈ **40s**，遠低於
一輪 ``github_refresh_interval_seconds=300s``，掃描不會追不上自己的週期。
但 repo 數／todo 檔數是會長大的，所以另設 ``budget_seconds``（每輪節流可花掉
的總睡眠上限，預設 120s，且由 ``MonitorConfig.github_throttle_budget()`` 進一步
夾在 refresh interval 的一半以下）：預算用完後節流自動失效，寧可讓那一輪的尾
段恢復齊發，也不能讓節流本身把掃描週期撐爆。

### 為什麼退避是帳號層級而非 per-repo

GitHub 的 secondary rate limit 綁的是 **token／帳號**，不是 repo。若退避狀態
以 provider_id 為 key，40 個 repo 會各自燒掉一次 403 才進入退避，減壓形同虛設。
因此本閘門的退避窗是單一共享窗；``blocked_seconds()`` 對所有 GitHub provider
一視同仁。

所有時間來源（``clock``／``sleeper``／``jitter_source``）皆可注入，測試不會真的
sleep。
"""

from __future__ import annotations

import math
import random
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - 僅型別，避免 config ↔ providers 迴圈匯入
    from .config import MonitorConfig


# 保守預設：見上方「節流預算計算」。interval 設 0 即完全停用節流。
DEFAULT_INTERVAL_SECONDS = 0.2
DEFAULT_JITTER_SECONDS = 0.1
DEFAULT_BUDGET_SECONDS = 120.0
DEFAULT_BACKOFF_BASE_SECONDS = 60.0
DEFAULT_BACKOFF_MAX_SECONDS = 1800.0

# 退避延遲加上的相對 jitter 比例（避免多個 monitor 實例同步醒來再次齊發）。
_BACKOFF_JITTER_RATIO = 0.1

RATE_LIMIT_KIND_PRIMARY = "primary"
RATE_LIMIT_KIND_SECONDARY = "secondary"
RATE_LIMIT_KIND_UNKNOWN = "unknown"


def _positive_number(value: object, *, field: str, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必須是數值")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{field} 必須是有限的非負數值，得到 {value!r}")
    return number


class GitHubPressureGate:
    """GitHub 請求節流 + 限流退避的共享閘門（可注入、可停用、thread-safe）。"""

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        jitter_seconds: float = DEFAULT_JITTER_SECONDS,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter_source: Callable[[], float] = random.random,
    ) -> None:
        self.interval_seconds = _positive_number(
            interval_seconds, field="interval_seconds", allow_zero=True
        )
        self.jitter_seconds = _positive_number(
            jitter_seconds, field="jitter_seconds", allow_zero=True
        )
        self.budget_seconds = _positive_number(
            budget_seconds, field="budget_seconds", allow_zero=True
        )
        self.backoff_base_seconds = _positive_number(
            backoff_base_seconds, field="backoff_base_seconds"
        )
        self.backoff_max_seconds = _positive_number(
            backoff_max_seconds, field="backoff_max_seconds"
        )
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds 不可小於 backoff_base_seconds")
        self._sleeper = sleeper
        self._clock = clock
        self._jitter_source = jitter_source
        self._lock = threading.Lock()
        self._spent_seconds = 0.0
        self._blocked_until: float | None = None
        self._consecutive_failures = 0

    @classmethod
    def from_config(cls, config: "MonitorConfig", **overrides) -> "GitHubPressureGate":
        """由 monitor config 建構；缺省值一律走本模組的保守預設。"""

        settings: dict[str, float] = {
            "interval_seconds": config.github_request_interval_ms / 1000.0,
            "jitter_seconds": config.github_request_jitter_ms / 1000.0,
            "budget_seconds": config.github_throttle_budget(),
            "backoff_base_seconds": float(config.github_backoff_base_seconds),
            "backoff_max_seconds": float(config.github_backoff_max_seconds),
        }
        settings.update(overrides)
        return cls(**settings)

    # ------------------------------------------------------------------
    # 節流
    # ------------------------------------------------------------------

    def begin_cycle(self) -> None:
        """一輪 GitHub 掃描開始：重置本輪節流預算（退避窗不受影響）。"""

        with self._lock:
            self._spent_seconds = 0.0

    def throttle(self) -> float:
        """在送出一次 ``gh`` 請求前呼叫；回傳實際 sleep 的秒數。

        回傳值只為了診斷／測試；呼叫端不需要處理。
        """

        with self._lock:
            if self.interval_seconds == 0 and self.jitter_seconds == 0:
                return 0.0
            delay = self.interval_seconds + self.jitter_seconds * self._jitter_source()
            remaining_budget = self.budget_seconds - self._spent_seconds
            if remaining_budget <= 0:
                # 預算用盡：寧可讓尾段恢復齊發，也不讓節流撐爆 refresh 週期。
                return 0.0
            delay = min(delay, remaining_budget)
            self._spent_seconds += delay
        if delay > 0:
            self._sleeper(delay)
        return delay

    # ------------------------------------------------------------------
    # 退避
    # ------------------------------------------------------------------

    def blocked_seconds(self) -> float:
        """退避剩餘秒數；``0.0`` 代表可以送請求。"""

        with self._lock:
            if self._blocked_until is None:
                return 0.0
            remaining = self._blocked_until - self._clock()
            if remaining <= 0:
                self._blocked_until = None
                return 0.0
            return remaining

    def note_rate_limited(
        self,
        *,
        kind: str = RATE_LIMIT_KIND_SECONDARY,
        retry_after_seconds: float | None = None,
    ) -> float:
        """登記一次限流命中並開啟／延長退避窗；回傳本次退避秒數。

        ``retry_after_seconds`` 來自回應透出的 ``Retry-After`` 或
        ``x-ratelimit-reset``（primary 配額耗盡時是 reset 的剩餘秒數）；有給就
        尊重它（取 max），沒有就純指數退避。
        """

        with self._lock:
            self._consecutive_failures += 1
            exponent = min(self._consecutive_failures - 1, 32)
            delay = min(
                self.backoff_base_seconds * (2.0**exponent), self.backoff_max_seconds
            )
            if retry_after_seconds is not None:
                hint = _positive_number(
                    retry_after_seconds, field="retry_after_seconds", allow_zero=True
                )
                delay = max(delay, hint)
            delay += delay * _BACKOFF_JITTER_RATIO * self._jitter_source()
            delay = min(delay, self.backoff_max_seconds)
            candidate = self._clock() + delay
            if self._blocked_until is None or candidate > self._blocked_until:
                self._blocked_until = candidate
            return delay

    def note_success(self) -> None:
        """一次成功的 GitHub 掃描：清空退避窗與連續失敗計數。"""

        with self._lock:
            self._blocked_until = None
            self._consecutive_failures = 0
