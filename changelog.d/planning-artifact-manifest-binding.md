# planning-artifact-manifest-binding

- **#802：fix-standard 的 planning publication 現在對 spec／design／plan 使用
  work-item-bound canonical destinations，即使 combo manifest 沒有
  `brainstorming` card 也能落地三件套；內容型 planning failure 的
  `needs_human` 回應同步提供補件、`abandon` 與重新 intake 的下一步提示，
  並將提示持久化在 `needs_human_reason` payload；補齊多 combo、路徑邊界與
  超長提示的回歸保護。`DiagnosticReason` 以加法欄位 bump 至 schema v2，
  仍相容讀取缺少 `next_step_hint` 的 v1 payload；這是單向遷移，已寫入 hint
  記錄後不可將 Manager 降級回不認得該欄位的舊版本；三條 operator hint 分支改用
  正體中文，保留內嵌的 `cortex work abandon` 指令；kind-bound 判定以完整的
  canonical work-item basename 或合法日期前綴 basename 比對，額外前綴／後綴不得
  繞過 manifest 綁定。**
