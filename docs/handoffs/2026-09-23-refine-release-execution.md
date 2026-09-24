# Refine 0.1.11 接手與定版執行表

日期：2026-09-23（Asia/Taipei）。本表是執行順序與驗收邊界；每次操作仍以 live issue、WorkAuthority、WorkflowRun、PR head、checks 與載入中的 runtime 為準。

## 範圍與基準

- 原始盤點以 `main` 的 `7fa4716b` 與 97 張 open issue 為基準，含總帳 #868。逐票 triage 覆蓋其餘 96 張；其中 25 張不在 #868 原 137 張分類表內。2026-09-23 14:51 UTC 最新合併的修復 PR 為 #984，`main` 是 `e74750dd`，`VERSION` 仍為 `0.1.10`；版本 `0.1.11` 尚未建立。
- 盤點後建立 #961–#973、#975–#980、#982–#983、#987–#990、#992–#994、#995–#997、#999–#1005、#1006–#1017 共 50 張 issue，分解 #887／#847／#818／#547／#943 的 Red 規模；它們不在原始 97 張之內。後續另開 #1020 記錄 Copilot review 準時提交卻因晚觀測被誤判 timeout，#1021 記錄舊 HEAD timeout 阻止新 candidate 重新請求 review；兩者各自獨立，不併入 #871／#862。#974／#981 是已合併規劃 PR，#984 是已合併的 preflight 環境修復 PR；#985 是待合併的第三批規劃 PR，#986／#991／#998 是產品 PR，皆不是 issue。父票維持 open，完整 aggregate acceptance 不因拆票縮減。
- 交付前置：#862 的 run `workflow-9dc654fef3850cc68deb` 與 PR #954；目前其舊 HEAD Copilot stop 需 #1021 的正式 recovery（或另有真正獲授權且可驗證的出口），不能靠重跑 `resume` 或 agent 自行 `review-attest` 越過。只有 PR merge、issue closed、run 合法收尾且後續原語可用，才解除 #818/#481 等依賴。#497 的其他 B/C/D 交付不由 #862 冒稱完成。
- 原始 14 張定版票、18 張可並行後續票、61 張 deferred 票、#564/#807 兩張待重新核對的關票候選，加上 #862 與 #868，共 97 張。新建的 50 張 issue 承接五張 Red 父票與其必要前置，不重複計入原始分類。這是排程分類，不變更 issue label 或關票狀態。

## 定版票與工作線

| 工作線 | 依賴順序 | 唯一實作 owner 與關鍵驗收 |
| --- | --- | --- |
| Ship/closure | #987 → #988 → #989 → #990（父票 #972）→ #973 的真 merge／D gates 切片（父票 #943）→ #885 → #810 | #987–#990 分別固定 main probe、typed C/M、recovery action、durable Manager context；#972 四票與整合驗收完成前不解除 #973 前置。#973 的完整 8/Red 範圍由 #1006–#1017 承接，其中 #1016/#1017 維持 Red aggregate；產品與整合驗收仍待閉合。真 Git 合併、雙方 CHANGELOG 條目、D 的 exact-head CI 與 merge/closure 分階段驗收。 |
| Authority | #992 → #993、#994；#979 等 #992+#993，#982 等 #992+#994，#980 等 #992–#994+#982+#983；#978 aggregate 後接 #964 → #965（父票 #847）；#961 → #975 → #976 → #977（父票 #962／#887） | #978 保持 Red umbrella，直到 schema/store/read-back 與 #979/#980 producers 全部驗收；#993 額外等 #966 全檔 CAS，不能以 #862 slice CAS 取代。#977 另等 #995 唯讀 closure inspection、#996 CompletionRecord 條件建立與 #997 Outbox CAS 合併，並需 #975 凍結完整 proof consumer shape；#847 AC10 loaded-runtime canary 保持獨立 gate。 |
| State integrity | #862 → #966 → #967（父票 #818）→ #481；#479 → #968 → #969 → #970 → #971（父票 #547） | #968 A1 與 #547 AC7 的 #999–#1005 已拆為無循環子票；#862 owner contract acceptance、#969 marker 與跨 UID proof 仍是前置，#547 保持 open。重疊的 registry／Manager recovery 修改由單一 owner 串行。 |
| Recovery/UI | #956、#874、#812、#871；#803 → #579；#1020 處理 review 晚觀測；#1021 處理舊 HEAD stop → 新 HEAD review | #579 job900 的 19 項 pytest 失敗均在 Claude review sandbox 的 `setfacl -R`；同候選 Manager full-suite ledger 為綠但 verifier 未採信。#803 的 exact-candidate ledger consumer 或經驗證的 ACL-capable disposable 測試環境是重新驗證前置；#1020 只修正準時提交的 exact-HEAD review 被晚輪詢誤判 timeout，不解除 #871 既有的 mergeable／CI gate。#1021 是 #862 新 candidate 合法重啟 Copilot review 的獨立前置，不以人工 attestation 代替。其他票依重疊模組錯開 merge，皆以正式 action 的可達性與錯誤終態驗收。 |

實作可使用 Copilot `gpt-5.4`（較大修改）、Codex `gpt-6-luna(max)`（較窄修改），或使用者確認的 agy `gemini-3.8-flash-high`；`gpt-6-sol` 做獨立對抗審查。agy Builder fallback 曾出現無 terminal envelope 的失敗（#928／#945），派工前須核對目前 runtime 健康，不能將 job exit 0 當作完成。每票使用獨立 worktree、唯一 work item 與 run owner；merge 前重新檢查 exact PR head 和 review threads。並行數量以實際資源與重疊模組風險調度。

## 進件材料狀態

PR #958／#959／#960／#974／#981 已合併，正式發布 #579／#812／#874／#956／#479／#481／#810／#871／#885／#961／#966 的 accepted 規劃與 work item。#579／#812／#874／#956／#479／#871／#885／#961 有 Cortex 產品 run；#481 等 #862，#810 等 #943，#966 等 #862。2026-09-24 的 #862 run `workflow-9dc654fef3850cc68deb` 已完成新 candidate `a9cd95f7` 的 verify/review，PR #954 clean、11 個 checks 成功、review threads 全部 resolved；ship 仍因舊 HEAD 的 Copilot timeout journal stop 需 #1021 正式 recovery。#862 的 PR、issue 與 run 均未交付。#812 PR #998 的 exact-head、同配置隔離 preflight 重跑 `policy`／`openspec`／完整 `tests` 皆 PASS，但原 run 的歷史 `ci-parity` 失敗未留 stage 輸出，不能單靠重跑結果宣稱 run 已通過或 PR 已交付。

#975／#976／#983 的 accepted 三件套與 work item 已在 PR #985，產品實作各待前置合併。#987–#990、#992–#994、#995–#997 的 live issue 已建立，accepted 三件套與 work item 分別在後續獨立規劃分支；合併前不得正式 intake。#978、#973 維持 Red umbrella；#973 的 #1006–#1017 已 issue-backed，其中 #1016/#1017 仍為 Red aggregate，十張 Yellow 子票有 accepted 規劃。#968 A1 與 #547 的 #999–#1005 在獨立規劃分支；#972 的四張 Yellow 子票需依序落地並回父票整合驗收。#979／#980／#982 的 live issue 依賴已分別改接 #992+#993、#992–#994+#982+#983、#992+#994，解除等待 #978 aggregate 完成的循環。#977 的三張前置 #995–#997 已建 live issue；產品實作仍等 #975 proof shape 與全部前置合併。#964／#965／#967／#969／#970／#971 依前置合併順序進件。草稿 frontmatter 的 `accepted` 不等於已發布 authority。

#972 須完成精確 main SHA 的 durable binding，以及 probe 失敗階段與 return code 的結構化結果；#973 須補 archive 後 Candidate 來源，涵蓋「main 落後但 merge-tree clean」及「CHANGELOG 衝突」兩種路徑，並以真 merge 產物驗證兩邊條目保留。每張 planning PR 的 body 只用 `Refs #N` 時，需依 policy 附 `policy-exempt:issue-link` 與理由，實作 PR 才使用 `Closes #N`。

## Release gate

1. 每張定版票：規劃材料正式發布、Cortex intake 成功；產品候選通過所需測試、OpenSpec、PR-context `policy_check`、跨 domain review、exact-head CI；PR merge、issue closed、run 走到完成或合法 `retire-delivered`。中間任一綠燈不能單獨算交付。
2. 把已 merge 的修復載入 Cortex runtime；核對 service `ExecStart` 的 pin、CLI 與實際配置，並跑一條「持久受理後提交者離場」的真實接續 canary，涵蓋已知例外、review、ship 與 closure。保留 run/job/PR/CompletionRecord 證據。
3. 定版 PR 才把 `VERSION` 升到 `0.1.11`，收攏 CHANGELOG fragment，帶 `release:0.1.11` label，跑完整 CI/policy 與 RC qualification。資格證據未齊時維持 `0.1.10`，不建立 tag 或宣稱 release。

## 其餘 open 票

18 張容量允許時處理、可順延下一版：#486、#492、#577、#600、#613、#808、#821、#864、#875、#876、#878、#881、#883、#935、#936、#938、#945、#953。另 61 張留在 triage 的 defer 集合，維持 open；#564/#807 的「已修可關」主張曾被對抗審查推翻，須有新證據才改狀態。
