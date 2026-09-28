# #1096 canary closeout 綁定 gate ledger 與 delivery gate 到本次派工

- **gate ledger（`qualification/driver.py::_validate_dispatch_closeout`）**：過去只驗 `<control>.gates.json`
  的外層形狀（`schema_version`／`kind`／`slice_id` 是字串／`gates` 是 list），`gates` 列表內容
  完全沒人看——回歸把部署層宣告的 `PSC_GATE_CMD_PYTEST` gate 跳過、或跳過後仍讓 workflow 宣告
  `passed`，都會被目前的檢查放行。現在逐項驗證每個 `gates` 條目形狀合法（必須是帶 `name`／
  `status` 的物件），並要求 `DEPLOYMENT_CANARY_EXPECTED_GATE_NAMES`（目前只有部署層固定宣告的
  `pytest`，見 `docs/superpowers/runbooks/deployment-canary-probe.md` §5）裡的每個名稱都存在
  且為 terminal `passed`；缺任一個或非 `passed` 一律 fail closed。另外把 `slice_id` 的檢查從
  「型別是字串」升級為「逐字等於它自己 job 的 `job_id`」，把 ledger 綁回本次派工實際跑的那個
  job，不再接受綁到別的 job 的 ledger。
- **delivery gate（同函式的 `gate_refs` 段落）**：過去只以 `kind`／`ref`／`sha256` 採信 evidence
  檔——內容從未被讀，他 run 或舊 candidate 遺留、只要湊得出合法 path＋hash 的證據檔一樣能滿足
  closeout。現在把內容當 JSON 讀出來：凡是自報 `run_id`／`work_id`／`candidate` 的欄位都必須
  與本次派工相符（欄位不存在則不強求，避免對未帶這些欄位的既有 evidence adapter 產生新的形狀
  假設）；`foreign-review` 這個必要 kind 另外要求逐字等於本 run 已獨立驗過（`run_id`／`repo`／
  `candidate`／`reviewer_job_id` 皆核對過）的 review job workflow evidence（同一份 path＋
  hash），不接受任何「看起來合法」但不是那一份的檔案。
- 新增 8 個回歸測試（`tests/test_qualification_driver_hardening.py`）：缺 gate、gate 非
  `passed`、ledger 條目缺 `status`、`slice_id` 綁錯 job、delivery gate evidence 的
  `run_id`／`work_id`／`candidate` 與本次派工不符（3 個參數化案例）、`foreign-review` 換成他
  run 的替身檔，以及既有正向 fixture（疊加全部新檢查後）仍通過。修復前以 `git show
  HEAD:qualification/driver.py` 還原成修復前版本跑過，8 個新測試全部 RED；復原修復後全部
  GREEN，且 `test_qualification_driver_hardening.py` 既有 92 個測試與 `test_phase2_qualification.py`
  ／`test_requirement_delivery.py`／`test_requirement_delivery_cli.py`（#845 消費同一份
  `qualification/validate.py` receipt 契約）全數維持通過，closeout 的 receipt schema 未變動。
- **範圍外，留給後續票**：`brainstorm`／`copilot`／`maintainer-review` 三種 delivery gate kind
  在 production 由外部 adapter（`planning.py` 的 brainstorm evidence writer、GitHub ship
  validator 的 review 結果）產生，其 JSON schema 是否逐字帶 `run_id`／`work_id`／`candidate`
  未經 live run 證實；若要對它們做出等同 `foreign-review` 的逐字綁定，需要 Manager 端在寫出
  該證據時多帶這些欄位（例如 gate ledger 本身也可仿照 `worktree_state.head` 多帶 `run_id`／
  `work_id` 讓 ledger 自證候選）——本票不改動 Manager 派工行為，未實作，只在此列出。
