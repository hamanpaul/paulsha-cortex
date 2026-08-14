# 506-manager-scan-pressure

- **`#506` workstream 補上 2026-08-14 的量測結論**——主壓力源是 **manager**（`work_actions.py:3425`
  的 auto-claim scan 每 tick 對每個 mapped issue 各發一次即時 `gh api` 讀 label，實測
  30s × 57 issue ＝ **114 次／分鐘**），而不是先前一直在調的 monitor（1200s × 約 300 次
  ≈ 15 次／分鐘）。PR `#512` 的 `GitHubPressureGate` 只注入到 `monitor/providers.py`，
  **`coordinator/` 這一側完全不受節流也不受退避管**——monitor 退避期間 manager 照打。
  這是先前三輪 monitor 參數調校（1000ms → 2000ms → 收斂掃描面）都無法讓 provider 離開
  degraded 的原因。新增四項驗收任務：閘門涵蓋 coordinator、scan 改消費 monitor 既有資料
  或加 TTL 快取、`doctor` 呈現每分鐘請求數、重新檢視 `PSC_MANAGER_INTERVAL_SECONDS` 預設值。
- 純文件變更：不改動任何執行路徑程式碼。
