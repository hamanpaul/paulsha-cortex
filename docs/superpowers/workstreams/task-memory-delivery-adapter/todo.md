---
status: accepted
work_item: task-memory-delivery-adapter
---

# Task memory delivery adapter Todo

## Scope

Owner：Cortex #857；dependency：Hippo #146。Cortex 只做 generic host adapter/validation，不改 Hippo core、Cortex global routing 或任何 project special case。

## Tasks

- [x] accepted spec/design/plan、此 Todo 與 `.cortex/work-items.yaml` registration 綁定同一 work item；自動化測試確認唯一 #857 registration、Hippo #146 dependency 與可讀文件 refs。
- [x] task envelope 與 capability matrix consumer；缺 identity、跨 scope、unsupported schema、capability mismatch、manifest/hash mismatch 均 fail closed。
- [x] inline/context-delivered、readonly snapshot-ready/read、manifest-bound note-fetch、ineligible/read-failed receipt 與 retry idempotency focused tests；content-returned 僅在實際讀取後出現。
- [x] Manager receipt ingress 驗證 persisted WorkflowRun/Job routing、事件前序與 applied artifact 對該 Job worktree 的 no-follow SHA，再寫入 0700/0600 append-only sidecar；worker 不直寫 Hippo ledger，legacy strict KPI 分離。
- [x] 可選 Hippo subprocess provider：命令必須以 `PSC_TASK_MEMORY_HIPPO_CMD` 明示（JSON argv array 或 shlex），第一個元素須為絕對路徑；未設、空白或非絕對路徑一律視為 provider 缺席，不搜尋 PATH（28cdcf47 起）；stdin/stdout/timeout 有界；stderr 只保留符合形狀的第一行 code；exit 10/11/14 與其餘 bounded provider code 按契約回報。
- [x] per-task fetch closure 綁定原 envelope 與 validator 接受的 manifest，task ID／manifest membership 先核對，再呼叫 `task-memory fetch`；Cortex 仍驗 content hash。
- [x] `PSC_TASK_MEMORY_ENABLED=1` opt-in Manager dispatch inline transport；預設關閉，Hippo 缺席時 prompt/lifecycle 沿原路徑，provider-unavailable receipt 只寫 Cortex sidecar。未因 Trust Root 權限不足擴權。
- [x] 每條支援 path 的 deterministic fixture 有 5 次成功與 5 次負例，涵蓋兩個 repository、build/verify、permission denial、cross-scope 與 relay/舊輸出 blocker；fixture-only eligible success 為 5/5，不能代替 live gate。
- [x] 新增 `cortex task-memory canary`，接受兩個以上登記 repo 與 `--runs`（預設 5），真實呼叫三條 capability path、permission-denied 負例與 project mismatch；JSON 分列 provide/candidate 成功數及成功率，可寫指定 evidence 路徑且不含 note 正文或本機絕對路徑。
- [x] canary 輸出對齊 #845 `cortex/task-memory-live-canary/v1` 契約（`paths.{context-delivered,snapshot-ready,note-fetch}`、`negative_controls[]`、`cross_project[]`、`executor.id`、`--target-file` 的 `target`），真 canary 產物包成 live receipt 可走 `_verify_live` 到 `verified`（#845 B5）。
- [x] canary 以實際觀測判定 relay overwrite（provider 覆寫 task id／delivery mode、Manager composer 未逐字保留原 prompt）與既有輸出 schema 破壞（unsupported major、非 JSON、結構不符），皆為 blocker 並有負例；合成卡片 task kind 輪替 build／verify／review，每條 path 至少兩種 task kind 成功；`legacy_strict_kpi_mutated` 改由 receipt 的 `counts_as_read` 計算（G857-3）。
- [ ] 對 installed/live provider 跑 canary：`cortex task-memory canary --repo <hippo-registered-repo-a> --repo <hippo-registered-repo-b> --runs 5 --target-file <acceptance-target.json> --evidence-path "$PSC_COORDINATOR_ROOT/evidence/requirement-delivery/live/task-memory-canary-857.json"`。需每 repo/path 至少五次 eligible provide 與完整 delivery，provide 與 retrieval rate ≥95%，預設各 repo/path 5/5，且 `blockers` 為空。現況：2026-09-26 兩次在合入前分支（輸出只在 session 暫存區）`passed=true`；2026-09-27 合入後重跑 `passed=false`（其中一個 repo 三條 path 皆 0 個 eligible candidate）；尚無合入後、存於 delivery evidence root 的通過紀錄。
- [x] live read-model evidence、全綠 repo test gate、PR-context policy/CI、review/merge：PR #1102（2026-09-27 merged）與 #1146（#1136 applied receipt，2026-09-29 merged）已合入，含兩者的 main（b55c4f7a）CI `Tests` pytest 3.10–3.13 全綠；live `cortex work show <work> --task-memory --json` 已對 #1099 dogfood run 回查到 run／job routing、receipt sidecar 與 gate refs。
- [ ] live `applied-with-evidence` receipt：daemon runtime pin 升到含 #1146 的 commit 後，由一張 live 卡產生至少一筆（G857-2）。
- [ ] Canary 通過後才設計 task-level control/treatment utility trial；另計 retry 去重、實際 action evidence、成本與誤引用。live canary gate 尚未通過，未開始。

## 範圍界線（owner 裁決，2026-09-29）

- Production Manager 派工只宣告 inline capability（`_prepare_task_memory_dispatch` 固定 `inline=True`）；snapshot 與 note-fetch 只由 `cortex task-memory canary` 以真 CLI 驗證。owner 裁決此範圍可接受，不另開 executor-facing snapshot／note-fetch 派工能力。
- 本 work item 由 operator worktree 直接實作、經 PR #1102／#1146 交付，未經 system Cortex `cortex work intake` 產生 WorkflowRun。owner 裁決接受 operator 直接交付，票面「正式規劃與交付」段要求的 intake→WorkflowRun 不補走。spec R8（adapter 在 production 派工時從正式 Work Item／WorkflowRun 讀取輸入）不受此裁決影響。

## 交付狀態（2026-09-29）

已完成並合入：Cortex public payload consumer、bounded Hippo CLI client、manifest-bound fetch callback、預設關閉的 Manager inline dispatch、Cortex receipt sidecar、read-only `cortex work show --task-memory`、真 CLI canary 入口（#1102），以及 terminal `task_memory_applied` → applied receipt（#1146）。Hippo #155 provider CLI 已於 Hippo 端合入。canary 輸出契約對齊 #845 validator、relay／schema blocker 偵測、task kind 輪替與 KPI 旗標計算於 `feature/857-task-memory-canary-gaps` 補齊。尚待 live：合入後的受治理 canary 通過紀錄（存於 delivery evidence root）與第一筆 live applied receipt。預設 dispatch prompt 維持原樣。
