# d1a-manager-reads-mirror

- **R0.5 D1（部分）：auto-claim 的 label 判定改走 monitor 鏡像，manager 不再逐 issue 輪詢
  GitHub**——先前 canonical 路徑的 `WorkAuthority.auto_label` 硬編 `False`，auto-claim scan
  因此每 tick 對每個 mapped issue 各發一次 live `gh api` 讀 label（實測 57 次/tick、24 小時
  不停，是 fleet 對 GitHub 最大的持續壓力源，#506），且 coordinator 側完全不受
  `GitHubPressureGate` 管轄。三段鏈路：
  - `monitor/providers.py`：`GitHubWorkProvider` 把持有 `cortex:auto-on-going` 的 open issue
    編號寫進 provider `observations["auto_label_issues"]`——issues 回應本來就含 labels，
    **零額外 API**；labels 欄位形狀不合 fail-closed（整包降級 malformed JSON）；PR 與
    closed issue 不參與。
  - `coordinator/claim.py`：`_authority_from_canonical_row` 由 observations 導出
    `auto_label`（新增 `_auto_label_from_observations`）；observations 缺失／形狀不合一律
    保守 `False`——auto 派工少跑一輪無害、誤跑才有害。
  - `coordinator/work_actions.py`：`run_auto_claim_scan` 廢除 O(mapped issues) sweep——
    鏡像為 `False` 的 authority **零 API** 直接進 claim 決策；鏡像為 `True` 才做**一次
    targeted 複驗**（label 可能在兩次 refresh 之間被人類移除，claim 是不可逆動作），
    以 live 結果為準，並在**第一次確認後 early-break**。#529 的節流與限流停手語意
    完整保留於複驗路徑。
- 穩態效果：常態（無 auto label 案件）manager 對 GitHub 的持續呼叫 **57 次/tick → 0**；
  有 auto 案件時為 O(掛 label 的 authority 數)（通常 ≤2）。
- 語意更新記錄於測試：`test_periodic_auto_scan_fails_closed_if_any_mapped_issue_read_fails`
  改為「確認前的讀取失敗 fail-closed」（early-break 後不再讀取其餘 issue）。
