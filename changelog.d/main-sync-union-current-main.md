# main-sync-union-current-main

- **`#1142`：CHANGELOG union 納入 repo 與 main-sync probe**——新增追蹤的
  `.gitattributes`，宣告 `CHANGELOG.md merge=union`（git 內建 union driver）。
  ship main-sync probe 的 merge-tree 改為 `git --attr-source=<M> merge-tree
  --write-tree …`，merge driver 一律讀 M（目標分支）tree 上的 attributes：不隨
  ship clone 工作樹漂移，Candidate 自己新增的 union 宣告也無法把真衝突矇成
  clean。CHANGELOG 同位置插入因此收斂成 clean-behind，非 union 路徑的衝突照判
  conflict；clean-behind 仍停在 `needs_human`（本次不改）。此 probe 需要
  git >= 2.43（全域 `--attr-source` 自 2.41 起才有，merge-tree 搭配它在 2.43
  前會 segfault），舊版一律 fail-closed 成 `main-sync-unavailable`。
- **`#1142`：merge-tree 只看 M commit 的 attributes**——`--attr-source` 只取代
  工作樹 `.gitattributes`，git 仍會疊上來源 repo 的 `$GIT_DIR/info/attributes`、
  `core.attributesFile`（含 XDG 預設）與 system attributes。probe 的 merge-tree
  因此改在用完即刪的私有暫存 bare GIT_DIR 執行：以 `rev-parse --path-format=
  absolute --git-path objects`（新 stage `merge-tree-objects`）取得來源 object
  store，寫入 `objects/info/alternates` 唯讀共用（不複製；merge 結果 tree 只寫進
  暫存 repo），不走 `git init` template、沒有 `info/attributes`；環境剝掉呼叫端
  所有 `GIT_*`，設 `GIT_CONFIG_NOSYSTEM=1`、`GIT_CONFIG_GLOBAL=/dev/null`、
  `GIT_ATTR_NOSYSTEM=1`，並以 `-c core.attributesFile=/dev/null` 執行
  `git --attr-source=<M> merge-tree --write-tree …`。取 object store 失敗、輸出
  不是既存絕對目錄或建立暫存目錄失敗（stage `merge-tree-isolation`）一律是 probe
  failure，落 `main-sync-unavailable`。
- **`#1142`：retry-build 修復改綁重試當下的 main**——main-sync stop 後的
  `retry-build` 在 reset 前於 Manager 來源樹重新 fetch `origin/main`，只寫
  run-scoped pin ref `refs/cortex/main-sync/<run_id>`（`--no-write-fetch-head`
  ＋`--refmap=`，不動 `FETCH_HEAD`／remote-tracking ref），把當下的 exact M 寫進
  新的 content-addressed `main-sync-retry` evidence、修復指令與新增的
  `WorkflowRun.main_sync_repair` 綁定；停機 evidence 與停機時的 M 原樣保留作稽核。
  fetch 失敗時不重置 run，維持 `needs_human`。修復卡 terminalization 失敗後的
  build phase retry-build 沿用同一綁定，其他 retry 清除它。
- **`#1142`：harvest 驗證修復候選含 retry 當下的 M**——`_harvest_build_candidate()`
  推進 feature ref 前，先把 bundle 收進 quarantine ref
  `refs/cortex/main-sync-quarantine/<run_id>`，要求其 tip 恰為被採信的
  candidate，再以來源樹 `merge-base --is-ancestor <M> <candidate>` 判定；不含 M
  （例如只合入停機時的 M，或沒有新 commit）即拒絕，feature ref 不動。
- **`#1142` 回滾語意**：`main_sync_repair` 是只在有值時寫出的新頂層欄位。回滾到
  本變更之前的 Manager 時，舊版載入會丟掉這個欄位、舊 harvest 也不驗 M——修復候選
  不再被要求含 retry 當下的 M，等同 #1142 之前的行為；ship main-sync probe 仍是
  delivery 前最後一道 main-sync 關卡，不會比改動前更差。
- **`#1142` 測試**：新增 `tests/test_main_sync_union_current_main_1142.py`（16
  項，真 git／bare origin／bundle）：repo 追蹤的 union 宣告、probe 對 CHANGELOG
  同位置插入判 clean-behind 且不受工作樹 attributes 影響、非 union 路徑真衝突照判、
  Candidate 自宣 union 無效、來源 repo `info/attributes`／`core.attributesFile`／
  XDG attributes 宣告 README union 時 README 真衝突仍判 conflict 而 M 宣告的
  CHANGELOG union 仍 clean-behind、merge-tree 在隔離暫存 GIT_DIR 執行且用完即刪
  （結果 tree 不寫進來源 repo）、隔離前置失敗落 `main-sync-unavailable`、main 停機
  後又前進時 retry-build 綁定當下 M（evidence／指令／pin ref／registry round-trip）、
  fetch 失敗 fail-closed 不重置、build phase 重派沿用綁定、harvest 對含當下 M 的
  候選通過而只含停機 M 或無新 commit 的候選拒絕。首版以 base commit 的
  production 程式跑同一檔確認 8 項 RED；隔離修正先以 036807b3 跑新增測試確認
  6 項 RED。`tests/test_main_probe_gate_987.py` 以假 runner 走到 merge-tree 的
  序列補上 `merge-tree-objects` stage。
