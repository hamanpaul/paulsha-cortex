### Added
- **Issue #202：task_type combo selector 與 fix-standard workflow**：
  依 durable snapshot 的 issue title 與 deck taxonomy 自動選牌，讓 `feat` 對應
  `feature-oneshot`、`fix` 對應 `fix-standard`；`unknown_type`／`ambiguous` fail-closed，
  `absent`／`unparseable`／combo 缺口則留下 `combo_selection` provenance 並沿用預設 combo。
  `cortex work start`／`cortex run work start` 新增 `--combo` authoritative override，
  `cortex stat --combo-selections` 可彙總 source × task_type。

### Fixed
- **Issue #202 補遺：durable snapshot 不可用時 combo 選擇改走 fail-soft**：
  `claim.mapped_issue_titles` 先前只在 snapshot hash mismatch 時 bypass（回傳 `None`）；
  `_load_snapshot` 因 snapshot 不存在／不可讀／schema 損壞而 raise 的 `ValueError`
  （含其子類別 `AuthorityValidationError`）未被攔截，會一路炸穿到
  `work_bridge.start_canonical_workflow`，讓沒有現成 durable snapshot 的 claim 直接
  拋例外而非落回 `feature-oneshot` 預設 combo。`mapped_issue_titles` 現在對這些情況
  也回傳 `None`，與既有 hash-mismatch bypass 走同一條路徑；`load_work_authorities`／
  `load_work_authority` 仍維持 fail-hard（沒有安全的 WorkAuthority 預設值可退）。
- **Issue #202 code review 修復：override 驗證改用 `load_combo`、`combo` 收斂只在 start 可用**：
  `deck.selector.select_combo` 的 explicit override 先前只靠 taxonomy 反查
  （`task-types.yaml` 的 type→combo 映射）判定未知，會把 repo 內實際存在、可
  `load_combo` 但沒有 task_type 映射的 legacy combo（如 `mcu-feature`）誤判為
  unknown，違反 R3「override 只要 combo 存在就應可用」；改為直接以 `load_combo`
  驗證存在／合法，taxonomy 反查僅用於 provenance 的 task_type（查無則留 `None`）。
  另外 `--combo` 說明雖標「start 專用」，`coordinator/cli.py`、`porcelain/run.py`、
  `coordinator/manager.py.apply_work_action` 先前對所有 work action 都會把
  `combo` 轉交下去，`resume` 等動作在特定時序（run 仍卡在 `define` phase）下可能
  被未經驗證的 combo override 影響；四層同步收斂為只在 `action == "start"` 才夾帶
  `combo`，其中 `control/contract.py` 的 `validate_request` 新增 fail-closed 規則
  （非 start action 帶 `combo` 直接拒絕請求）作為所有入口的收斂防線。
  `coordinator/work_bridge.start_canonical_workflow` 同步移除與 `select_combo`
  重覆、含未使用中間變數的 override 驗證死碼，統一改為呼叫 `select_combo`。
