---
status: accepted
work_item: intake-model-flags
---

# work intake 套用 --builder-executor 等模型指定旗標，不支援的 action 明確拒絕

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1296`。
- 現況：`coordinator/cli.py` 的 `cortex work` parser 對所有 action 都定義
  `--planner-executor／--planner-model／--builder-executor／--builder-model／--reviewer-executor／--reviewer-model`，
  但只有 `rechain` 會讀取。`intake` 收到這些旗標時不報錯也不套用，run 的 `model_chain_override` 是
  `None`。目前要指定模型只能走 `--payload <json 檔>`，由 `work_bridge.extract_model_chain_override` 抽取，
  manager 在派工時做 fail-closed 驗證（#205）。
- 已知案例（2026-10-06 dogfood）：四個 run 指定了不同 builder，結果全部用預設的 copilot gpt-5.4。
- 不改 `rechain` 的語意與前置條件；不放寬 identity、capability、reviewer independence 的任何驗證。

## Tasks

- [x] **T1 RED**：測試 `cortex work intake ... --builder-executor codex --builder-model gpt-6-luna` 送出的 request
      args 含 `builder_executor`／`builder_model`，建立的 run `model_chain_override` 為指定值；`start` 同理。
      現行應失敗。
- [x] **T2 intake／start 套用旗標**：CLI 把這些旗標轉成 request args，與 `--payload` 走同一條
      `extract_model_chain_override` 路徑；旗標與 payload 同時給、但值不同時拒絕。只給 executor 或只給 model
      時明確報錯。
- [x] **T3 不支援的 action 明確拒絕**：其他 action 帶上這些旗標時，以非 0 結束，並說明哪些 action 支援。
- [x] **T4 intake 輸出解析結果**：intake 成功後，輸出解析後的 planner／builder／reviewer（executor 與 model），
      方便 operator 立即核對。
- [x] **T5 文件**：CLI help 與 operator 文件更新。新增 changelog fragment，並同步 `CHANGELOG.md [Unreleased]`。
