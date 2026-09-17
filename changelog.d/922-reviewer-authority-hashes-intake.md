---
type: docs
scope: refine
---
進件 #922：登記 work item `reviewer-authority-hashes-echo`（issue link＋accepted todo）。todo 依 #826 run
`workflow-22b00a5937e88c0090ab` 的現場（4 次 sonnet reviewer、3 次 envelope 缺 `authority_hashes`、
通過的 review 被整份判 schema invalid）定出 T1–T6：採信端把 `authority_hashes` 改選填、缺席由 Manager
以 job snapshot 補齊並標來源、帶值仍精確比對；prompt 端從 required 移除；採信失敗的 diagnostic 附
envelope keys／findings 數／reason 摘要／log 路徑。純進件，不含實作。
