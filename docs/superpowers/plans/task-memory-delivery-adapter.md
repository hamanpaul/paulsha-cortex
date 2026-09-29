---
status: accepted
work_item: task-memory-delivery-adapter
---

# Task memory delivery Cortex adapter Plan

本計畫依賴 Hippo #155 的可選 `hippo task-memory provide/fetch` public CLI 與 `hippo/task-memory/v1` envelope；Cortex 不 import Hippo package、不新增 package dependency。adapter-specific candidate identity/hash/version/manifest 是可選 public extension，未提供時 fail closed。Manager dispatch 只有在 per-instance manager overlay 設 `PSC_TASK_MEMORY_ENABLED=1` 時才呼叫 provider，目前以 bounded inline excerpt 傳送；預設路徑維持原 prompt。read-model CLI 與 Manager-owned sidecar 使用 Cortex state，Hippo strict KPI 不變。

## 1. Authority and dependency

- [x] 將 Cortex #857 與 Hippo #146 綁為跨 repo dependency；Hippo 是 generic contract owner，Cortex 是首個 host adapter。
- [x] 將本 plan、spec、design、workstream Todo 與 `.cortex/work-items.yaml` registration 綁為同一 work item，讓 isolated worker 不依賴暫存檔。
- [x] receipt ingress 僅由 Manager-owned writer 驗證 persisted run/job routing 後寫 sidecar；未使用 deprecated low-level dispatch、未直接改 durable registry 或 Hippo ledger。

## 2. Contract and RED coverage

- [x] 消費已發布的 `hippo/task-memory/v1` envelope、0–3 個 candidate、delivery mode 與未知 optional fields；內容取得需要 adapter extension 的 manifest/hash/version，缺少時 fail closed，strict KPI 維持分離。
- [x] Cortex contract tests 涵蓋 Work Item/WorkflowRun/card → task envelope、identity 缺失或不符、跨 project、manifest/hash/capability mismatch、unsupported major 與 provider ineligible。
- [x] Receipt tests 涵蓋 inline/context-delivered、唯讀 snapshot-ready/read、manifest-bound note-fetch、permission-denied/read-failed、帶 evidence 的 applied 與冪等 replay。
- [x] 已保存初始 RED（測試模組因 adapter 尚不存在而無法 import）及後續 GREEN 證據，見 `docs/evidence/task-memory-delivery-adapter-857.md`。

## 3. Adapter implementation

- [x] Cortex thin adapter 精確映射 Work Item/WorkflowRun/card/Job identity 與宣告的 capability；不修改 central routing、trust-root、shared quota 或 Hippo lifecycle。
- [x] Capability matrix 每次只選一個 delivery mode；不接受任意 host path、不讀 global memory root、不用 allow-all；provider/mode/schema/scope failure 保留 bounded reason 且不 fallback 重試。
- [x] Manager-owned sidecar writer 將 receipt 綁定 persisted run/job/routing；事件順序驗證拒絕沒有內容交付前置證據的 applied receipt，並以 no-follow 唯讀方式從該 Job worktree 核對 artifact SHA。
- [x] 沿用目前 resolved executor/model identity；沒有 executor/model 特例或 model override。
- [x] 以 bounded subprocess 接上 Hippo CLI：argv 只解析為 argv array/shlex、不經 shell；stdin/stdout/timeout 有界；exit 10/11/14 特殊映射，其餘保留 bounded diagnostic code；stderr 僅採第一行符合格式的 allowlisted code。
- [x] 以 per-task callback 捕捉 request 與 Cortex validator 已接受的 manifest，拒絕 task id／manifest 外 note，呼叫 fetch CLI 後仍由 Cortex 驗 content hash。
- [x] 以 `PSC_TASK_MEMORY_ENABLED=1` 接到正式 Manager workflow dispatch；目前 capability 僅 inline，提供者缺席／permission denied 不注入 prompt、不改 lifecycle，receipt 仍限 Cortex sidecar。

## 4. Canary and utility readiness

- [x] 每種支援 delivery path 的 deterministic isolated fixture 各跑 5 次成功與 5 次 path-specific negative control；另測 permission denial 不洩漏 exception 與未知 permission layer。
- [x] Generic payload fixture 在兩個 repository、build/verify task kind 間通過；cross-scope mismatch 與 relay/legacy-schema blocker 有明確拒絕測試。
- [x] Canary summarizer 機械計算 authorized success、failure、permission denial、unknown/ineligible；離線正向 fixture 為每條路徑 5/5，strict KPI 不變。
- [x] 新增 `cortex task-memory canary`：至少兩個 repo，每 repo/path 跑 `--runs N`（預設 5）真 provide（note-fetch 另 fetch、snapshot 開啟、inline 確認 delivery），並跑 evidence-source 拒絕及跨 project mismatch；JSON 分列 provide 與 candidate-level 成功數及成功率，未指定 evidence path 時不落檔且永不輸出 note 正文。
- [x] canary 輸出以 #845 `cortex/task-memory-live-canary/v1` 契約為正本（`paths.{context-delivered,snapshot-ready,note-fetch}`、`negative_controls[]`、`cross_project[]`、`executor.id`、`--target-file` 的 `target`）；relay overwrite 與既有輸出 schema 破壞由實際觀測判定為 blocker；合成卡片 task kind 輪替 build／verify／review；`legacy_strict_kpi_mutated` 由 receipt 的 `counts_as_read` 計算。
- [ ] Installed/live provider canary 必須每 repo/path 至少五次 eligible authorized provide 和五次完整 delivery，provide 與 candidate 成功率 ≥95%；預設 `--runs 5` 要各 repo/path 5/5，且無 blocker。驗收命令：`cortex task-memory canary --repo <hippo-registered-repo-a> --repo <hippo-registered-repo-b> --runs 5 --target-file <acceptance-target.json> --evidence-path "$PSC_COORDINATOR_ROOT/evidence/requirement-delivery/live/task-memory-canary-857.json"`。
- [ ] 只在 canary gate 通過後設計 task-level control/treatment utility trial；retries 合併、至少明列實際 evidence、成本與誤引用，未完成不宣稱 utility improvement。

## 5. Verification and delivery gates

- [x] 完整 repo tests、PR-context policy/CI、review 與 merge：經 PR #1102（2026-09-27）與 #1146（2026-09-29）合入；focused/related 測試輸出記錄於 `docs/evidence/task-memory-delivery-adapter-857.md`。
- [x] Regression tests 確認未帶 opt-in flag 時 legacy `cortex-work/v1` 不變，inline/context delivery 不增加 strict Read。
- [x] Read model 以正式 WorkItem/WorkflowRun/Job fixture 串接 routing identity、plan/spec revisions、receipts 與 test/review evidence；live Monitor/Manager state 保留為 runtime 驗收。
- [x] Provider unavailable、project/scope mismatch 與 unsupported schema 都保留明確 `read-failed`/`ineligible`/`parked`；Trust Root 下 provider exit 10 記為 provider permission-denied，不變更全域權限、不偽造 evidence。

## Implementation status (2026-09-29)

本地實作驗證及其界線見 [`docs/evidence/task-memory-delivery-adapter-857.md`](../../evidence/task-memory-delivery-adapter-857.md)。Hippo #155 provider CLI 已在 Hippo 端合入；Cortex client 依其只讀契約以 subprocess 呼叫，沒有 runtime import。Production 只走 inline、本 work item 不補 `cortex work intake` 兩點為 owner 2026-09-29 裁決的範圍界線（見 workstream Todo）。Hippo CLI 在 installed service 帳號下的 permission、scope 與 ≥95% live threshold 仍待合入後的受治理 canary。
