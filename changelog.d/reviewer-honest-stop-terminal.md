---
type: fix
scope: workflow
---
verify／review reviewer card 現在可把形狀合法的 `failed`／`needs_human` terminal 誠實落成
`verification-terminal-explicit-stop`／`review-terminal-explicit-stop`。Manager 會保留模型原文與
job log evidence ref、回收 reviewer sandbox，且不再把這類 stop 包成
`terminalize-workflow-job-failed`／`resume-workflow-failed`；periodic runner 與 explicit resume
都不會自動重派，實際重派出口仍是 `retry-card`／`retry-build`。
