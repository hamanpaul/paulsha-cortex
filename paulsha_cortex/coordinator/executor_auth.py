"""#369：executor 登入態的共用分類與 dispatch 前探測。

背景：`runtime_preflight.py` 的 provider capability 探測（#262 設計）在生產環境
從未真正被接線——`manager.py` 呼叫 `evaluate_dispatch_gate`／
`_runtime_preflight_gate` 時未曾傳入 `snapshot_lookup`／`provider_prober`，
provider 這條路徑因此永遠是死碼。複驗同時發現 `porcelain/bootstrap.py` 的
copilot 登入態判定對輸出做字串匹配（"login"／"authenticate"／"device code"），
而 GitHub 的限流訊息常常同時帶有這些字樣（例如「再次授權才能提升額度」），
導致限流被誤判成「尚未登入」。

本模組把「CLI 輸出應該分類成 rate-limit／logged-out／ok」的判斷抽成一個
與呼叫端無關的共用函式（`classify_cli_output`），`bootstrap.py` 的 copilot
分支與這裡的 `check_executor_auth`（供 `runtime_preflight` 的
`provider:executor` sentinel 使用，見 `runtime_preflight.PROVIDER_EXECUTOR_SENTINEL`）
共用同一份分類邏輯，避免兩處各自實作、各自漂移。

Rate limit 訊號必須先於 login 訊號判定——這是 #369 真正修的缺口。
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Callable

from .diagnostics import diagnostic_reason
from .runtime_preflight import DEFAULT_PROVIDER_TTL_SECONDS, ProviderFreshness

__all__ = [
    "EXECUTOR_CANDIDATES",
    "EXECUTOR_AUTH_TTL_SECONDS",
    "classify_cli_output",
    "check_executor_auth",
]

EXECUTOR_CANDIDATES: tuple[str, ...] = ("claude", "codex", "copilot")

# 沿用既有 provider snapshot 的預設 TTL（見 runtime_preflight.py），讓 executor
# 登入態探測與既有 provider 新鮮度語意（D3：快照 + TTL + 有界 probe）一致。
EXECUTOR_AUTH_TTL_SECONDS = DEFAULT_PROVIDER_TTL_SECONDS

# Rate limit／流量限制訊號：主要與次要（abuse detection）額度限制、標準
# rate-limit HTTP 訊號。刻意寫寬——這裡誤判成 rate limit 的代價只是把一次真正
# 的登入失效當成限流重試一輪，遠比把限流誤判成登出（#369 實際案例）安全。
_RATE_LIMIT_RE = re.compile(
    r"""
    rate[\s-]?limit          # "rate limit exceeded", "secondary rate limit"
    | abuse\ detection        # "You have triggered an abuse detection mechanism"
    | x-ratelimit             # 迴響進 stderr 的 X-RateLimit-* header
    | retry-after             # 迴響進 stderr 的 Retry-After header
    | quota\ exceeded
    | usage\ limit
    | too\ many\ requests
    | \b403\b
    | \b429\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

_LOGIN_SIGNAL_RE = re.compile(
    r"login|authenticate|authorization|device code",
    re.IGNORECASE,
)


def classify_cli_output(returncode: int, output: str) -> tuple[str, str]:
    """把一次 CLI 呼叫的 (returncode, 合併輸出) 分類成登入態訊號。

    回傳 ``(status, detail)``；``status`` ∈ ``{"ok", "rate_limited",
    "logged_out", "unknown"}``。

    Rate limit 判定必須排在 login 判定之前：GitHub／provider 的限流訊息常常
    同時帶有 "authenticate"／"login" 字樣（例如「請重新授權以取得更高額度」），
    若 login 判定排在前面，限流會被誤判成登出——這正是 #369 在
    `porcelain/bootstrap.py` 複驗到的實際案例。
    """

    if returncode == 0:
        return "ok", "command exited 0"
    text = output or ""
    if _RATE_LIMIT_RE.search(text):
        return "rate_limited", "rate limit signal detected in output"
    if _LOGIN_SIGNAL_RE.search(text):
        return "logged_out", "login signal detected in output"
    return "unknown", f"no definitive signal (exit {returncode})"


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv 為內部組裝，非 shell
        argv, check=False, capture_output=True, text=True, timeout=timeout
    )


_EXECUTOR_AUTH_ARGV: dict[str, tuple[str, ...]] = {
    "copilot": (
        "copilot",
        "-p",
        "Respond with OK only.",
        "--silent",
        "--disable-builtin-mcps",
        "--no-custom-instructions",
        "--output-format",
        "json",
    ),
    "claude": ("claude", "auth", "status"),
    "codex": ("codex", "doctor", "--json"),
}


def check_executor_auth(
    executor: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _default_runner,
    timeout_seconds: float = 20.0,
    now: Callable[[], float] = time.time,
    ttl_seconds: float = EXECUTOR_AUTH_TTL_SECONDS,
) -> ProviderFreshness:
    """dispatch 前對單一 executor 做一次有界的登入態探測（#369 item 3）。

    單次、不快取——快取與 TTL 由呼叫端（`manager.py` 的
    provider snapshot_lookup／provider_prober closure）負責，這裡只回答
    「現在問一次，答案是什麼」，維持與 `runtime_preflight` 既有 D3/D6
    （快照 + TTL + 有界 probe，budget 以 provider identity 為鍵）的分工一致。

    這是刻意簡化過的通用探測，不重現 `porcelain/bootstrap.py` 對 codex JSON
    body 的欄位級解析——那屬於 `cortex bootstrap` 一次性 preflight 的精確度，
    這裡是 dispatch 熱路徑上的輕量 best-effort 探測，靠 `classify_cli_output`
    的 returncode/文字訊號即可正確區分 rate-limit／logged-out／ok。
    """

    if executor not in EXECUTOR_CANDIDATES:
        return ProviderFreshness(
            provider_id=executor,
            status="degraded",
            observed_at=now(),
            ttl_seconds=ttl_seconds,
            source="live-probe",
            reason=f"unsupported executor: {executor}",
            diagnostic_reason=diagnostic_reason(
                "executor-auth-unsupported",
                f"unsupported executor: {executor}",
                source="coordinator.executor_auth.check_executor_auth",
                executor=executor,
            ),
        )
    argv = list(_EXECUTOR_AUTH_ARGV[executor])
    try:
        completed = runner(argv, timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProviderFreshness(
            provider_id=executor,
            status="degraded",
            observed_at=now(),
            ttl_seconds=ttl_seconds,
            source="live-probe",
            reason=f"executor auth probe failed: {type(exc).__name__}: {exc}",
            diagnostic_reason=diagnostic_reason(
                "executor-auth-probe-failed",
                f"executor auth probe failed: {type(exc).__name__}",
                source="coordinator.executor_auth.check_executor_auth",
                executor=executor,
            ),
        )
    combined = (completed.stdout or "") + (completed.stderr or "")
    status, detail = classify_cli_output(completed.returncode, combined)
    freshness_status = "ok" if status == "ok" else "degraded"
    reason = None if status == "ok" else f"{executor}: {detail}"
    return ProviderFreshness(
        provider_id=executor,
        status=freshness_status,
        observed_at=now(),
        ttl_seconds=ttl_seconds,
        source="live-probe",
        reason=reason,
        diagnostic_reason=(
            None
            if status == "ok"
            else diagnostic_reason(
                f"executor-auth-{status.replace('_', '-')}",
                reason or f"{executor}: {detail}",
                source="coordinator.executor_auth.check_executor_auth",
                executor=executor,
            )
        ),
    )
