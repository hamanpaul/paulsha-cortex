---
status: draft
work_item: same-run-authority-restart
---

# 既有 Candidate run 的 same-run authority restart 設計（#1069）

## Decisions

### D1 — Recovery 走明確 operator 路徑

沿用現有 `cortex work resume <work_id> --repo <repo>` 作為 operator 意圖入口，不新增 CLI 命令。只有由 control request 指定的 `resume` 可進入此分支；periodic scan/automatic claim/start/intake 維持觀察或既有行為，不得消費此 recovery decision。Manager 以 canonical WorkAuthority、唯一 matching run 與完整 expected snapshot 決定 exact target；不以 work slug、候選檔名或 latest run 猜測。

### D2 — Fresh authority 由既有 accepted contract 提供

#1069 不重做 #1063 Todo qualification、#1064 Monitor generation 或 #1065 strict WorkAuthority reader。它們與 #1054 必須先 accepted/merged，Manager 才消費正式產出的 typed authority。reset 前固定 old/new sorted source revision vectors、snapshot hash、provider revision 與完整 digest。只從同代 successful Monitor generation 和 owner-published canonical Todo 取 authority；手動 link override、`todo.md` 名稱、registry sequence 及局部來源集合都不能代替。

### D3 — CAS 前凍結 exact run/Candidate/PR/journal tuple

一次觀察需包含 exact registry revision、run/repo/work/status/phase/retry classification/old claim key/source revision、Candidate SHA、verified head、gates/evidence refs、job refs/active-job 清單、PR refs；再唯讀讀取目前 delivery-journal row identity/revision，並以 authenticated GitHub 讀取唯一 open PR identity/head/state。PR head 必須等於原 Candidate。任何缺值、重複、run/claim/Candidate/PR 不一致、active job、merged/closed PR、journal unknown/conflict 或 authority drift 均在 CAS 前停止。

此票不提供 journal writer，也不變更 row。read-only row identity/revision 僅供判斷舊 run 是否仍是同一交付綁定；#1070 才消費 #983 conditional writer，負責完整 authority read-back/conditional delivery。如果現有唯讀介面無法證明 exact row identity/revision，列為跨 writer API 前置並更新 issue；不得私建 API 或把未知值當相符。

### D4 — WorkflowRun 原子變更只由 #1068 完成

在 pre-CAS tuple 唯一且 fresh 時，`work_actions.py` 將完整 expected snapshot 與由 verified WorkAuthority 得到的 new digest/source binding 交給 #1068 API 一次提交。呼叫端不改 `registry.py`，不組 patch 寫 registry，不拆成 status update 加 reset 的兩筆交易。#1068 負責 revision CAS、claim/source era、verify/review gate invalidation、verified-head clear 與 audit transition；stale revision 或欄位 drift 直接 typed conflict。

### D5 — CAS 後重新讀取才能 dispatch

CAS 成功不是 dispatch 許可。Manager 必須 fresh reload authority 和 exact run，重驗 authority digest/source vector/provider generation、run identity/new claim era、Candidate、原 PR/head/state 和無 active job，再以相同 Candidate 進入 verify/review。CAS 後任一值漂移都保留 gates stopped 並 typed fail closed，不派 job。工作卡可以建立新 verify/review job/evidence，但 run/repo/work/Candidate 綁定不變。不得重跑 build 或觸發 ship/delivery mutation。

### D6 — 證據世代分開保存

既有 Candidate、build/repair 結果、JobRegistry row、舊 claim bindings、verify/review evidence bytes/refs、PR refs 和 run ID 都保留為歷史。新 claim era 的 verify/review evidence 必須獨立綁定；舊 evidence 仍可稽核但不能填新 gate。#1068 transition 只 invalidates verify/review，不改 Candidate/build/PR/journal。

### D7 — 重送、併發與 crash 都 fail closed

相同 operator request 的重送只能沿 #1068 定義的相同 request identity/payload replay；不同 payload、stale CAS 或並行 resume 以 typed conflict 停止。CAS 前 crash 不改 run；CAS 後 crash/re-entry 先重讀 durable registry 與 authority/PR tuple，再決定能否繼續同一 Candidate 的 gates；不得二次增加 attempt/audit 或以新 run 掩蓋結果。任何 dispatch 前 unknown 均停住。

### D8 — Fixture 隔離與零 delivery write

使用真 Manager/WorkAuthority fixtures、stub GitHub read operations 及 issue 固定的 #983 fixture tuple。正向只驗明確 resume 可令 same-run verify/review 前進；負向覆蓋 stale/missing/ambiguous authority、wrong exact tuple、active job、authority publication drift、PR/journal drift、CAS conflict、crash/replay/repeated/concurrent request。所有 GitHub write、journal write、push、PR create/update/merge/close spies 為零；不讀寫正式 run、journal、issue 或 PR #1049。

### D9 — 硬依賴與 scope freeze

在 #1054 + #1063/#1064/#1065 和 #1068（其自身等 #966）完成前不 intake。#1070/#983 是後續 delivery lane；除 read-only identity/revision 之外，本票不得呼叫或擴寫 delivery API。production diff 僅 `work_actions.py`；如需另一 production module、registry/raw-file writer、journal writer 或尚未發布的跨 writer API，停止並 issue-backed re-scope。
