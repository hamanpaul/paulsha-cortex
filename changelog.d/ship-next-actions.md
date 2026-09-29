### Fixed

- **#1184 ship needs_human 復原建議**：Copilot 類 ship 停止的五個回應點改由同一 helper 導出 `next_actions`：`abandon` 只在 pre-delivery admission 會受理時列出（帶 PR 或已進 ship 的 run 不再收到），`review-attest` 與該動作共用結構前置 predicate，兩者皆不成立時建議重跑 `ship`；`next_step_hint` 對齊首個建議動作。claim／status／Monitor 共用的 `_phase_recovery_actions` 也不再對不在 exact-HEAD review 的 run 投影 `review-attest`。
