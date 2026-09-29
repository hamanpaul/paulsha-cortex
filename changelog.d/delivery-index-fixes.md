# delivery-index-fixes

- **`#845`：需求交付總帳補齊 test 綁定、installed 期限與 manifest owner**——
  - verification contract 的 `tests[*]` 可選填 `acceptance_ids`（非空、不重複；`checks`／`full_suite` 仍拒收），`run_result_verification` 把同一組 id 原樣寫進 evidence 的 `details.tests[*]`，讓 `cortex delivery` 的 test stage 能對該 criterion 判 verified；先前 contract 會把這個鍵當 unknown key 拒收，test stage 對正式 contract 產出的 record 永遠 missing。未宣告時 contract 形狀與 hash 不變。
  - `max_age_seconds.installed` 生效：以 loaded-runtime receipt 的 `recorded_at` 計算，過期或晚於現在為 `stale`／`installed-runtime-receipt-expired`，投影缺 `recorded_at` 為 `unknown`。installed／live 期限到期不再被同 generation 弱讀取保護擋住，reconcile 會把持久化索引降成 not-ready；暫時性讀取失敗仍維持 pending。
  - 索引 mapping row 內本版不認得的欄位在重驗同一 mapping 時保留，不再被重新投影靜默丟失。
  - `refine-requirements-v1.json` owner work_ids：R05 移除已 not planned 的 #837；R07 補 #843、#844；R09 補 #842；R13 移除 PR #820（由已列的 #807 承接）；R14 補 #844。requirement revision 與 policy_version 不變。
  - 文件改正：本 repo 沒有 evidence snapshot 與外層 live receipt 的 producer，`--snapshot` 需外部提供；production 不接受任何 waiver（manifest 無 authority／waivable stage，CLI 不注入核可 validator）。
  - 補 A01（一需求多 work、一 work 多需求）、A05（rejected／absent／身分 unknown review、舊 record 缺 runtime 欄位）、A10（row 未知欄位、legacy gaps 投影）、A11（authority／版本不在 policy、核可拒絕或缺席、stage 不可豁免、CLI 不採信 waiver）、A12（受治理 live validator 端到端、只補被移除的 entry、記錄 loaded receipt、`live-canary-receipt-expired` 負例）測試。
