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
- [x] 可選 Hippo subprocess provider：命令可用 JSON argv array/shlex 覆寫，預設 `hippo` 由 PATH 解析；stdin/stdout/timeout 有界；stderr 只保留符合形狀的第一行 code；exit 10/11/14 與其餘 bounded provider code 按契約回報。
- [x] per-task fetch closure 綁定原 envelope 與 validator 接受的 manifest，task ID／manifest membership 先核對，再呼叫 `task-memory fetch`；Cortex 仍驗 content hash。
- [x] `PSC_TASK_MEMORY_ENABLED=1` opt-in Manager dispatch inline transport；預設關閉，Hippo 缺席時 prompt/lifecycle 沿原路徑，provider-unavailable receipt 只寫 Cortex sidecar。未因 Trust Root 權限不足擴權。
- [x] 每條支援 path 的 deterministic fixture 有 5 次成功與 5 次負例，涵蓋兩個 repository、build/verify、permission denial、cross-scope 與 relay/舊輸出 blocker；fixture-only eligible success 為 5/5，不能代替 live gate。
- [x] 新增 `cortex task-memory canary`，接受兩個以上登記 repo 與 `--runs`（預設 5），真實呼叫三條 capability path、permission-denied 負例與 project mismatch；JSON 分列 provide/candidate 成功數及成功率，可寫指定 evidence 路徑且不含 note 正文或本機絕對路徑。
- [ ] 對 installed/live provider 跑 canary：`cortex task-memory canary --repo <hippo-registered-repo-a> --repo <hippo-registered-repo-b> --runs 5 --evidence-path "$HOME/.agents/core/runtime/task-memory-canary-857.json"`。需每 repo/path 至少五次 eligible provide 與完整 delivery，provide 與 retrieval rate ≥95%，預設各 repo/path 5/5。
- [ ] live read-model evidence、全綠 repo test gate（本次已跑，83 個既有案例受 AF_UNIX/ACL sandbox 限制失敗）、PR-context policy/CI、review/merge；任何 blocker 保留 needs_human，不以 issue/plan/open PR 冒稱產品完成。
- [ ] Canary 通過後才設計 task-level control/treatment utility trial；另計 retry 去重、實際 action evidence、成本與誤引用。

## 本地交付狀態（2026-09-26）

已完成 Cortex public payload consumer、bounded Hippo CLI client、manifest-bound fetch callback、預設關閉的 Manager inline dispatch、Cortex receipt sidecar、read-only `cortex work show --task-memory` 與真 CLI canary 入口。Hippo #155 尚未 merge/install；fake CLI contract 與 dispatch 行為已在本 worktree 驗證，實際 project registry、Trust Root 權限和 live ≥95% gate 待上述 canary。預設 dispatch prompt 維持原樣。
