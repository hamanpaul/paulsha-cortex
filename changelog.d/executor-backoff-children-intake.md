---
type: docs
scope: refine
---
進件 #928／#929：#825 母票（work item `executor-durable-backoff`）依其 todo「#831 後實跑重評，仍 Red 真拆」於
2026-09-17 實算 sizing 8／Red 後拆出兩個子 work item——`executor-backoff-terminal-admission`（#928，母 design C＋D＋parser：
reset hint 解析、兩 lane 終局寫入與對帳 seam、workflow lane admission）與 `executor-backoff-slice-consumers`（#929，E＋F：
slice admission、`launcher.model`、request／tick 的 `dispatch_skipped_by_backoff`），各自 accepted spec／design／todo，
實算 sizing 6／Yellow。母 todo 補拆分紀錄。純進件，不含實作；依賴順序 #850 → #928 → #929。
