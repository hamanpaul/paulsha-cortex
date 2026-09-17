---
type: fix
scope: workflow
---
Review gate 的 `authority_hashes` 回聲改成「缺席補齊、帶值仍嚴格比對」：review terminal 缺少
`authority_hashes` 時，Manager 會用同一份 pinned planning-authority snapshot 補進 review verdict；review
evidence 的讀取面則標記 `authority_hashes_source=manager-snapshot` 或 `reviewer-echo`，不改 gate evaluation
持久化 schema。review 卡 prompt 同步把該欄位從 `required` 移除，但保留 expected mapping 供模型照抄；
review terminal 採信失敗的 `terminalize-workflow-job-failed`／
`resume-workflow-failed` 診斷則補上 envelope keys、findings 數、reason 摘要、log 路徑與 parse error。
