# 506-manager-scan-pressure

- **`#506` workstream 補上 2026-08-14 的量測結論**——主壓力源是 **manager**（`work_actions.py:3425`
  的 auto-claim scan 每 tick 對每個 mapped issue 各發一次即時 `gh api` 讀 label，實測
  30s × 57 issue ＝ **114 次／分鐘**），而不是先前一直在調的 monitor（1200s × 約 300 次
  ≈ 15 次／分鐘）。PR `#512` 的 `GitHubPressureGate` 只注入到 `monitor/providers.py`，
  **`coordinator/` 這一側完全不受節流也不受退避管**——monitor 退避期間 manager 照打。
  這是先前三輪 monitor 參數調校（1000ms → 2000ms → 收斂掃描面）都無法讓 provider 離開
  degraded 的原因。新增四項驗收任務：閘門涵蓋 coordinator、scan 改消費 monitor 既有資料
  或加 TTL 快取、`doctor` 呈現每分鐘請求數、重新檢視 `PSC_MANAGER_INTERVAL_SECONDS` 預設值。
- **`#506` 修正：auto-claim scan 的 GitHub 請求壓力控制**——
  - **攤平**：逐 issue 讀 label 之間插入 `PSC_MANAGER_GITHUB_INTERVAL_MS`（預設 1000ms）的
    間隔，且間隔跨 authority 累計（secondary limit 綁 token 不綁 repo，per-authority 重置
    等於沒有節流）。設 0 停用，保留舊行為。
  - **限流即停手**：一旦命中 rate-limit 型失敗（沿用 `github_rate_limit.is_rate_limit_signal`）
    就中止整輪掃描，其餘 authority 標成 `github-rate-limited-scan-aborted`。舊行為是每個
    authority 各自撞一次才 break 自己那圈，於是限流期間每個 tick 仍送出 O(authorities) 次
    必定失敗的請求，每一次都在延長帳號層級懲罰窗——「越限流越打、越打越限流」的正回饋。
  - 非限流的讀取失敗維持舊語意（只擋該 authority，其餘照跑），以測試釘住。
