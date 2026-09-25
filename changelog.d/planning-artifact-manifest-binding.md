# planning-artifact-manifest-binding

- **#802：fix-standard 的 planning publication 現在對 spec／design／plan 使用
  work-item-bound canonical destinations，即使 combo manifest 沒有
  `brainstorming` card 也能落地三件套；內容型 planning failure 的
  `needs_human` 回應同步提供補件、`abandon` 與重新 intake 的下一步提示，
  並將提示持久化在 `needs_human_reason` payload；補齊多 combo、路徑邊界與
  超長提示的回歸保護。`DiagnosticReason` 以加法欄位 bump 至 schema v2，
  任何 `schema_version: 2` 記錄（不論是否帶 `next_step_hint`）都會被舊版
  `__post_init__` 拒收；Manager 一旦寫過 v2 `needs_human_reason` 就不可降級回舊版。
  三條 operator hint 分支改用正體中文，保留內嵌的 `cortex work abandon`
  指令；kind-bound 判定改由 #812 的精確 stem 文法比對 spec／design 的
  `<base>-<kind>.md` 與 plan 的 `<base>.md`／`<base>-plan.md`，並由四段相對路徑、
  目錄家族與正規化守衛限制作用範圍；包含 work item 的合法 slug 不得因 combo
  manifest 缺少 brainstorming 而被拒。**
