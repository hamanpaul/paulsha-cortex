# 738-ledger-candidate-verification

- **`#738` candidate 驗證下放 gate ledger——三分部署下帶 candidate 的 build 卡
  終於可被採信**（#641 預留、一直沒開的那張票）。`_verify_exact_candidate` 原以
  Manager 身分 `git -C <builder 樹>`，#641 收掉 `u:cortex-manager:rX` 後 builder
  產生的 loose objects 結構上讀不到，任何寫入卡的採信必落
  `candidate-worktree-unreadable-pending-gate-identity`。修法照 #629／#641 已裁定
  的方向：gate 執行身分在**受控 checkout**（快照副本）上收集
  `worktree_state`（`head`／`dirty`／`dirty_total`／`probe`／`ancestry_baseline`／
  `ancestry_ok`），寫進同一份 ledger；baseline 經封閉 argv（`--assert-ancestor`）
  由 Manager 的 job 記錄導出（自動路徑＝`dispatch_head`、`regenerate-gates`＝
  已採信 candidate 或 `dispatch_head`），argv 仍無任何來自 job 的可變輸入。
  `read_gate_spool` 對 state 逐項驗形狀（40-hex／有界清單），非法即
  `gate-spool-invalid`。Manager 端 `_verify_exact_candidate`／
  `_verify_build_candidate_transition` 先消費權威 ledger（head 等值、
  ancestry_ok；ledger 的 baseline 與當下算出的不符視同缺席）；ledger 缺席／probe
  非 ok 時逐字退回既有 git 路徑——direct 模式零回歸、三分模式維持既有
  fail-closed。reviewer lane 不動（#650 起走 Manager 自己 clone 的樹）。乾淨度
  本票只記錄不強制（現行路徑本就不驗，採信語意 parity；升級為 FAIL gate 另行
  裁決）。ancestry 的另一道守衛（`harvest_branch` refspec 無 `+` 的 fast-forward
  拒絕）不變，ledger 補的是新 branch 首張卡那一格。
