# Refine 0.1.11 接手與定版執行表

日期：2026-09-23（Asia/Taipei）。本表是執行順序與驗收邊界；每次操作仍以 live issue、WorkAuthority、WorkflowRun、PR head、checks 與載入中的 runtime 為準。

## 範圍與基準

- 原始盤點以 `main` 的 `7fa4716b` 與 97 張 open issue 為基準，含總帳 #868。逐票 triage 覆蓋其餘 96 張；其中 25 張不在 #868 原 137 張分類表內。2026-09-23 13:38 UTC 最新已合併的規劃 PR 為 #960，`main` 是 `ea3f81ef`，`VERSION` 仍為 `0.1.10`；版本 `0.1.11` 尚未建立。
- 盤點後另建立 #961–#973 共 13 張子票，分解 #887／#847／#818／#547／#943 的 Red 規模；這些是上述 97 張以外的新票。父票維持 open，完整 aggregate acceptance 不因拆票縮減。
- 交付前置：#862 的 run `workflow-9dc654fef3850cc68deb` 與 PR #954。只有 PR merge、issue closed、run 合法收尾且後續原語可用，才解除 #818/#481 等依賴。#497 的其他 B/C/D 交付不由 #862 冒稱完成。
- 原始 14 張定版票、18 張可並行後續票、61 張 deferred 票、#564/#807 兩張待重新核對的關票候選，加上 #862 與 #868，共 97 張。新建的 13 張子票承接五張 Red 父票，不重複計入原始分類。這是排程分類，不變更 issue label 或關票狀態。

## 定版票與工作線

| 工作線 | 依賴順序 | 唯一實作 owner 與關鍵驗收 |
| --- | --- | --- |
| Ship/closure | #972 → #973（父票 #943）→ #885 → #810 | 各票獨立 Cortex run；#972 固定 main probe／durable C/M recovery，#973 以真 Git 驗證 push 前合入 main 並保留兩方 CHANGELOG；#885 要證明 archive/retry 不迴圈；#810 要證明 merge 後自行完成 closure。 |
| Authority | #963 → #964 → #965（父票 #847）；#961 → #962（父票 #887） | 每張子票獨立 owner；先落地自產發布收據與精確 remote archive authority 裁決，後續 consumer／completion 路由各自驗收。父票在子票與整合驗收全部完成後才關閉。 |
| State integrity | #862 → #966 → #967（父票 #818）→ #481；#479 → #968 → #969 → #970 → #971（父票 #547） | Registry CAS 與 slice identity 是各自後續子票前置；同一時間只讓一位 owner 修改重疊的 registry／Manager recovery 面。雙 writer、舊 terminal 重播、retry proof 與跨 work item recovery 須有失敗注入或多 process 回歸。 |
| Recovery/UI | #956、#874、#812、#871、#579 | 依重疊模組錯開 merge；每票以正式 action 的可達性與錯誤終態作驗收。 |

實作可使用 Copilot `gpt-5.4`（較大修改）或 Codex `gpt-6-luna(max)`（較窄修改）。`gpt-6-sol` 做獨立對抗審查；agy 指定的 `gemini-2.8-flash (high)` 未在現行 roster，現行可見的是 `gemini-3.8-flash-high`，不得自行視為同型號替代。每票使用獨立 worktree、唯一 work item 與 run owner；merge 前重新檢查 exact PR head 和 review threads。並行數量以實際資源與重疊模組風險調度。

## 進件材料狀態

PR #958／#959／#960 已合併，正式發布 #579／#812／#874／#956／#479／#481／#810／#871／#885 九組 accepted 規劃及 work item。#579／#812／#874／#956／#479／#871／#885 已建立 Cortex 產品 run；#481 等 #862，#810 等 #943。#862 的 PR #954 與 run 仍未交付，不能用本地候選或 pytest 通過冒稱完成。

#961 已通過獨立對抗審查與 fix-standard 實算 4／Yellow，本次 planning PR 發布其 accepted 三件套與唯一 work item；合併後才可 intake。#966／#968 的首批子票材料仍在審核。#962／#963／#972 的初次子票規劃各實算 7／Red、8／Red、7／Red，仍須 issue-backed 再拆分，不能直接 dispatch。#964／#965／#967／#969／#970／#971／#973 依前置合併順序進件。#943 的對抗審查阻擋點已併入 #972／#973 的分工，尚未實作。草稿 frontmatter 的 `accepted` 不等於已發布 authority。

#972 須完成精確 main SHA 的 durable binding，以及 probe 失敗階段與 return code 的結構化結果；#973 須補 archive 後 Candidate 來源，涵蓋「main 落後但 merge-tree clean」及「CHANGELOG 衝突」兩種路徑，並以真 merge 產物驗證兩邊條目保留。每張 planning PR 的 body 只用 `Refs #N` 時，需依 policy 附 `policy-exempt:issue-link` 與理由，實作 PR 才使用 `Closes #N`。

## Release gate

1. 每張定版票：規劃材料正式發布、Cortex intake 成功；產品候選通過所需測試、OpenSpec、PR-context `policy_check`、跨 domain review、exact-head CI；PR merge、issue closed、run 走到完成或合法 `retire-delivered`。中間任一綠燈不能單獨算交付。
2. 把已 merge 的修復載入 Cortex runtime；核對 service `ExecStart` 的 pin、CLI 與實際配置，並跑一條「持久受理後提交者離場」的真實接續 canary，涵蓋已知例外、review、ship 與 closure。保留 run/job/PR/CompletionRecord 證據。
3. 定版 PR 才把 `VERSION` 升到 `0.1.11`，收攏 CHANGELOG fragment，帶 `release:0.1.11` label，跑完整 CI/policy 與 RC qualification。資格證據未齊時維持 `0.1.10`，不建立 tag 或宣稱 release。

## 其餘 open 票

18 張容量允許時處理、可順延下一版：#486、#492、#577、#600、#613、#808、#821、#864、#875、#876、#878、#881、#883、#935、#936、#938、#945、#953。另 61 張留在 triage 的 defer 集合，維持 open；#564/#807 的「已修可關」主張曾被對抗審查推翻，須有新證據才改狀態。
