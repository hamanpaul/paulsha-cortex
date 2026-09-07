---
type: fix
scope: coordinator
---

**Issue #828：workflow status 現在從 registry job 綁定投影實際執行身份。**

`in_flight`、workflow `attention` 與 `recent_done` 會一致提供
`executor`、`model`、`job_id`、`card`、`identity_source` 與 `execution_state`。
同一 run 的當前 card 優先使用 in-flight job，其次使用該 card 的最後一次 terminal
execution；沒有 job 時只呈現明確的 planned identity，否則保持 unknown，不從 phase 或
persona 推測模型。跨 run、repo、phase 或 card 的 job 不會被借用。
