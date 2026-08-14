---
status: accepted
work_item: fix-instance-config-isolation
---

# fix-instance-config-isolation Todo

`#518`：`PSC_AGENTS_ROOT` 已隨 instance 隔離，但 `PSC_PROJECT_CONFIG_ROOT` 沒有。
`hippo` instance 因此讀到 `cortex` instance 的 `project-cortex.yaml`、繼承其
`workspaces`（operator 的多專案根目錄），掃出**與 cortex 完全相同的 13 個 GitHub repo**
（實測兩者 provider 集合逐一相同）。

危害不是重複工作，而是 **GitHub API 壓力平白翻倍且完全隱形**：`#512` 的
`GitHubPressureGate` 是 per-process 的，兩個 instance 各自節流到設定速率、互不知情
彼此的退避窗，合計卻是兩倍。這是 `#506` secondary rate limit 難以靠節流壓下來的
結構性成因之一——實測 primary 配額幾乎未動（`core remaining 4879/5000`）而 secondary
反覆觸發，正符合「總量不高、瞬時併發過密」的特徵。兩個 instance 的 snapshot
各自看起來都正常，operator 沒有任何訊號可察覺。

與 `scripts/service-manager.sh` `#375` 註解記載的是同一類 isolation 破口
（`PSC_MANAGER_SPECS_DIR`／`PSC_COORDINATOR_ROOT`／`PSC_SPECS_ROOT` 刻意留在 operator 域）；
差別在於 specs 共掃只造成派工範圍重疊，project config 共用則直接放大 API 壓力。

已套用的 operator workaround（2026-08-14，非本 work item 的交付內容，僅為止血）：
為 `hippo` instance 建立專屬 config root，provider 數 13 → 1，fleet 合計掃描面
28 → 16。本 work item 要修的是**讓這件事不再需要 operator 手動發現與修補**。

## Tasks

- [ ] installer／`service-manager.sh` 建立 instance 時，`PSC_PROJECT_CONFIG_ROOT` 隨 `PSC_AGENTS_ROOT` 一併 instance-scope（含既有 instance 的遷移路徑，不得靜默沿用共用根）
- [ ] 新 instance 的 config root 需自動具備最小可用內容：`project-cortex.yaml`（`workspaces` 不得為空清單，見 `monitor/config.py:_parse_workspaces`）與 `model-identities.yaml`，避免落入 `#476` 的 monitor restart loop
- [ ] `cortex doctor` 新增檢查：偵測到多個 instance 解析到同一個 project config root 時明確告警，並列出重複掃描的 repo 數與涉及的 instance 名稱
- [ ] 診斷需可在**單一 instance 內**判斷（不能要求 operator 去讀別的行程的 `/proc/<pid>/environ` 才發現問題）
- [ ] 測試涵蓋：兩 instance 共用 config root（應告警）、各自隔離（應通過）、instance config root 缺 `project-cortex.yaml`（應給出可行動的錯誤而非 restart loop）
- [ ] 與 `#506` 建議 7／8（token 層級共享退避窗與速率預算）互為補件：設定隔離後，多 instance 共用同一顆 token 的本質問題仍在，需在本 work item 的文件中明確指出殘餘風險與後續 issue
