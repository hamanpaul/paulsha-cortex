"""測試 fixture 用的結構化 needs_human 理由。

診斷 invariant（#527／#514／#515／#511／#482）把「把 run 轉入 needs_human」這件
事變成必須帶理由的狀態轉移。測試在建立「已經卡住的 run」這種前置狀態時同樣要
過這一關——這是刻意的：fixture 若能無理由地製造 needs_human，invariant 就有一個
測試看不到的後門，而後門正是本家族五張 issue 的共同形態。

這裡提供一個統一的 fixture 理由，讓測試不必各自發明一份。
"""

from __future__ import annotations

from paulsha_cortex.coordinator.diagnostics import DiagnosticReason, diagnostic_reason


def fixture_needs_human_reason(
    reason: str = "test-fixture-blocked",
    detail: str = "測試前置狀態：直接把 run 佈置成 needs_human",
    **context: object,
) -> DiagnosticReason:
    """測試前置狀態專用的結構化理由。"""

    return diagnostic_reason(
        reason,
        detail,
        source="tests.diagnostic_fixtures.fixture_needs_human_reason",
        **context,
    )
