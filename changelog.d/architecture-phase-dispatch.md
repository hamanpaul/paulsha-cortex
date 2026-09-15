---
type: docs
---
- 修正原生 Archify JSON／HTML 的 Phase、Card 與 Job 層級；一個 WorkflowRun 的七階段不等於每次派工都跑七個 Job。新增 Card／attempt 節點、逐階段角色及多卡數量，保留已存在 planning artifact 的採用與 Manager 本地 Ship 稽核 Job 例外。
- 增加 pinned Combo／Persona 比對與負控制、瀏覽器實際節點文字驗證；移除前次遺留的臨時 source-export workflow。不改 runtime、skill 或 github.io。
