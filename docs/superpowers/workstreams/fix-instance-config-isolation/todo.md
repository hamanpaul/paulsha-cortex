---
status: accepted
work_item: fix-instance-config-isolation
---

# fix-instance-config-isolation Todo

`#518`：`hippo` instance 的 monitor 讀到 `cortex` instance 的 `project-cortex.yaml`、
繼承其 workspaces，掃描出**與 cortex 完全相同的 13 個 GitHub repo**——GitHub API 壓力
平白翻倍且完全隱形（兩邊 snapshot 各自看起來都正常）。

## Scope 修正（0815，取代初版；經 adversarial review 與 installer 現況查核）

初版 todo 要求「installer 讓 `PSC_PROJECT_CONFIG_ROOT` 隨 `PSC_AGENTS_ROOT` instance-scope」
——**這個派生現行 `deploy/installer.py:280-307` 已經具備**。實際缺口是三件別的事：

1. **legacy instance 的 env 未遷移**：`hippo` 的 env 檔是舊版 installer 產的
   （`cortex doctor` 的 `managed-path-drift` 警告即此徵兆：「legacy install predates this
   key becoming instance-scoped managed state; rerun `cortex install service` to adopt」）。
   本 work item 的主體是**遷移路徑**，不是重新實作派生。
2. **workspace 語意未裁決**：scanner 把 workspace 當「多 repo 父目錄」掃描；instance 專屬
   config 若以「猜 repo parent」自動生成，會把 sibling repo 全部重新掃進來（正是本 issue
   要消除的行為）。0814 的 operator workaround 用「空 sentinel 目錄＋`project-hippo.yaml`
   明確 projects 宣告」達成單 repo 監控——正式修法需要一級語意，不靠猜。
3. **診斷不可見**：兩個 instance 解析到同一 config root 時沒有任何告警；operator 得靠
   `/proc/<pid>/environ` 對照才能發現。

## Tasks

- [ ] **legacy env 遷移**：`cortex install service` 對既有 instance 提供明確的 adopt/遷移
      流程——偵測 env 檔缺 instance-scoped `PSC_PROJECT_CONFIG_ROOT`（或指向共用根）時，
      產生遷移後的 env 與最小 instance config（含非空 `workspaces` 的合法
      `project-cortex.yaml`——`monitor/config.py:_parse_workspaces` 拒絕空清單——與
      `model-identities.yaml`），遷移必須原子（寫暫存、驗證可載入、再替換）且可回滾
- [ ] **workspace 語意**：monitor config 支援 **exact-project 宣告**（明列 repo 路徑，
      不掃父目錄），或等價機制（如僅含 `project-*.yaml` projects、workspaces 允許為空）；
      instance 遷移產生的 config 一律用 exact-project，**禁止以猜測 parent 目錄生成
      workspaces**
- [ ] **doctor 檢查**：偵測多個 instance 解析到同一 project config root 時明確告警，
      列出涉及的 instance 名稱與重複掃描的 repo 數；診斷須在單一 instance 內可判定
      （不得要求 operator 讀其他行程的 environ）
- [ ] **測試**：legacy env 遷移（含回滾）、exact-project 不掃 sibling、共用 config root
      告警、遷移後 `managed-path-drift` 警告消失、hippo 情境 fixture
      （instance-scoped agents root ＋ 共用 config root → 遷移後單 repo 監控）
- [ ] **驗證現場**：0814 的 operator workaround（hippo 專屬 config root，provider 13→1）
      以正式機制取代後，行為等價且 workaround 檔案可移除

## 現場紀錄（供實作者參考）

- operator workaround 細節與量測：issue `#518` 首則與 0814 留言
- 初版 todo 的錯誤前提與更正：issue `#518` 的 0814-15 兩則更正留言
  （含「artifact 引用要查 planning_authority／evidence／planning-transactions journal
  三處」的教訓）
- 前一世代 run `workflow-7a430d31eff66ef13630` 已依 v4 計畫結算（superseded、outputs
  隔離於 `prj_pri/.cortex-artifact-backups/f13630-settled/`），其產出不得成為本 work item
  的 build authority
