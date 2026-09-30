### Fixed

- **#1237 archive 後補上新 spec 的 Purpose**：Manager 在 ship 階段執行 `openspec archive` 之後、commit 之前，會把 openspec 替新 capability 寫入的 TBD Purpose 佔位換成 archived proposal `## Why` 的第一段；中文換行接回時不補空格。只換本 change 的佔位，其他內容不動，替換後仍通過 `openspec validate --specs --strict`。修正前，每個新增 capability 的 PR 都帶著佔位，Copilot 必回 finding，ship 因此停住（#716 canary）（#1237）。
