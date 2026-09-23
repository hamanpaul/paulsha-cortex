# Refine 0.1.11 接手與定版執行表

日期：2026-09-23（Asia/Taipei）。本表是執行順序與驗收邊界；每次操作仍以 live issue、WorkAuthority、WorkflowRun、PR head、checks 與載入中的 runtime 為準。

## 範圍與基準

- `main` 基準 `7fa4716b`；`VERSION` 仍為 `0.1.10`。Manager 已載入該 revision；版本 `0.1.11` 尚未建立。
- GitHub 有 97 張 open issue，含總帳 #868。逐票 triage 覆蓋其餘 96 張；其中 25 張不在 #868 原 137 張分類表內。未發現 triage 後新增的票。
- 交付前置：#862 的 run `workflow-9dc654fef3850cc68deb` 與 PR #954。只有 PR merge、issue closed、run 合法收尾且後續原語可用，才解除 #818/#481 等依賴。#497 的其他 B/C/D 交付不由 #862 冒稱完成。
- 14 張定版票、18 張可並行後續票、61 張 deferred 票、#564/#807 兩張待重新核對的關票候選，加上 #862 與 #868，共 97 張。這是排程分類，不變更 issue label 或關票狀態。

## 定版票與工作線

| 工作線 | 依賴順序 | 唯一實作 owner 與關鍵驗收 |
| --- | --- | --- |
| Ship/closure | #943 → #885 → #810 | 各票獨立 Cortex run；#943 要證明落後的 main 在 push 前合入、保留兩方 CHANGELOG；#885 要證明 archive/retry 不迴圈；#810 要證明 merge 後自行完成 closure。 |
| Authority | #847 → #887 | 各票獨立 run；自產 planning 的等價證明與已 merge run 的 completion 路由各自驗收，不以跳過 restart 代替完成。 |
| State integrity | #862 → #818 → #481；#479 → #547 | 同一時間只讓一位 owner 修改 registry/manager recovery 面；雙 writer、舊 terminal 重播、retry proof 與跨 work item recovery 均須有失敗注入或多 process 回歸。 |
| Recovery/UI | #956、#874、#812、#871、#579 | 依重疊模組錯開 merge；每票以正式 action 的可達性與錯誤終態作驗收。 |

實作可使用 Copilot `gpt-5.4`（較大修改）或 Codex `gpt-6-luna(max)`（較窄修改）。`gpt-6-sol` 做獨立對抗審查；agy `gemini-2.8-flash (high)` 先通過目前 roster、launcher、terminal contract 的 admission 才派工。建議最多三條互不衝突的 run 同時進行。每票使用獨立 worktree、唯一 work item 與 run owner；merge 前重新檢查 exact PR head 和 review threads。

## 進件材料狀態

14 票各有 spec/design/todo 草稿。本次進件 PR 先帶 #579/#812/#874/#956 四組及 work item；其他十組仍在 session scratchpad，均無 Cortex run。對抗意見後的修訂版須逐票確認：issue 最新正文與留言、互相一致的 Requirements/Decisions/Tasks、無 open question、實算 sizing 與所需測試。草稿 frontmatter 的 `accepted` 不等於已發布的 authority；本 PR merge 前，四組也尚未發布。

下一批優先修訂並發布 #943 的三件套與 work item；它的規格須涵蓋「main 落後但 merge-tree clean」及「CHANGELOG 衝突」兩種路徑，並以真 merge 產物驗證兩邊條目保留。其餘按工作線分批進件；每張 planning PR 的 body 只用 `Refs #N` 時，需依 policy 附 `policy-exempt:issue-link` 與理由，實作 PR 才使用 `Closes #N`。

## Release gate

1. 每張定版票：規劃材料正式發布、Cortex intake 成功；產品候選通過所需測試、OpenSpec、PR-context `policy_check`、跨 domain review、exact-head CI；PR merge、issue closed、run 走到完成或合法 `retire-delivered`。中間任一綠燈不能單獨算交付。
2. 把已 merge 的修復載入 Cortex runtime；核對 service `ExecStart` 的 pin、CLI 與實際配置，並跑一條「持久受理後提交者離場」的真實接續 canary，涵蓋已知例外、review、ship 與 closure。保留 run/job/PR/CompletionRecord 證據。
3. 定版 PR 才把 `VERSION` 升到 `0.1.11`，收攏 CHANGELOG fragment，帶 `release:0.1.11` label，跑完整 CI/policy 與 RC qualification。資格證據未齊時維持 `0.1.10`，不建立 tag 或宣稱 release。

## 其餘 open 票

18 張容量允許時處理、可順延下一版：#486、#492、#577、#600、#613、#808、#821、#864、#875、#876、#878、#881、#883、#935、#936、#938、#945、#953。另 61 張留在 triage 的 defer 集合，維持 open；#564/#807 的「已修可關」主張曾被對抗審查推翻，須有新證據才改狀態。
