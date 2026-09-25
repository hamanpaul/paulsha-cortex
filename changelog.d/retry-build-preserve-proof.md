---
type: fix
scope: coordinator
---
**#479 retry-build launch 前失敗保留既有 proof**

票面主缺陷已由 #941 修正；本次補齊恢復態 slice 在 `retry-build` 的 launch 前失敗時
保留既有 candidate、verification／review refs、builder／reviewer 綁定與 slice
state 的殘項。只有本次失敗的未綁定 `launch-failed` build job 會保留在 job 稽核中，
`complete_tick` 不再讓它覆寫現任 slice 的 handoff manifest。operator 修正 launch
barrier 後可直接再次重試。
