---
status: accepted
work_item: execution-profile-contract
---

# Execution profile 可擴充契約規格

## Authority

對應 [#835](https://github.com/hamanpaul/paulsha-cortex/issues/835)，屬 #829／R08／B2。
基準是已合併 [PR #832](https://github.com/hamanpaul/paulsha-cortex/pull/832)
的 `60a3ffa867377b0c86fa2f10e91fb9820d91938c`，沿用
[完整 refine plan](../plans/2026-09-07-cortex-refine-complete.md) 與
[execution-domain 語彙](2026-09-07-cortex-execution-domain.md)。
`accepted` 只表示完整規劃輸入，不代表已註冊、freeze、build-ready、實作或驗收；
本件 sizing 仍為 Red，須先完成獨立審查與有 owner 的人工拆分。

## Requirements

### R1 — 版本化配置與三層真值

建立 adapter、model、原生 effort、工具、sandbox、權限、角色需求，以及
adapter/loadout/toolchain 版本的共同 execution-profile 契約。
requested 表示請求；resolved 表示 descriptor／政策解析後準備執行的配置；
observed 表示執行器實際回報的配置。每層各自保存來源與版本，不能互相冒充。
未觀測的 model revision、effort 或其他欄位明示 unknown，不能以 requested 補成實測。
task role、最低品質與 independence 等要求與配置分欄，能力證據不能由 descriptor 自授。

### R2 — 描述資料可擴充，adapter 隔離協定差異

對已支援協定新增 model 或其原生 effort，只需 descriptor 變更；不在 central resolver、
quota、workflow 或 reporting 增加產品名稱分支。新 runtime 可新增 adapter 與 conformance，
不是承諾任意新協定零程式碼。adapter 宣告原生 effort 的型別／可接受值或範圍；不建立
跨產品通用 effort 枚舉、同名等價假設或固定大小排序，也不固定 model／agent 名單。
unsupported 的配置必須在 spawn 前拒絕；不偷偷刪 effort、改 sandbox 或換模型。

### R3 — 硬條件先於選擇偏好

保留 #205 per-work chain、#534 層次排序與 preference／explicit pin 差別。
pin、最低品質、reviewer independence、Trust Root、sandbox／工具權限均為硬條件；
quota 足夠不構成豁免，明示 pin 無可用合格配置時等待／阻塞而非靜默 fallback。
unknown role 不得落入 build 候選池；與 #581 的 role 修復同步切換，保持 legacy manifest
欄位與凍結 chain 的原始意圖，不重新解釋或改寫舊 receipt。
歷史 explicit pin 先於 measured 篩選的路徑，不得成為新版 profile 的品質豁免。

### R4 — Canonical key 與證據責任

本票擁有版本化 canonical execution-profile key、actual-condition fingerprint 與 migration seam。
fingerprint 綁實際執行條件；effort、adapter/version、loadout/toolset、sandbox／權限條件、
model 或 toolchain 變更須可區別。時間戳、價格與 pricing provenance 不進能力 fingerprint；
報表仍可獨立保存它們。缺 observed 的資料可保存結構化 unknown，但不得宣稱完整 actual key
或借用另一配置的資格。canonicalization 與輸入順序無關，版本／型別錯誤明確拒收。

### R5 — 無回寫的相容遷移

legacy manifest／凍結 model chain 保持可讀，重新載入或重啟不換 pin、不補寫假 observed。
新版配置以獨立版本化 binding 接合現有資料；未知版本與殘缺 descriptor fail-closed。
已有 attempt/evidence/receipt 不回填、不重算覆蓋；新解析紀錄引用既有原件。
舊資料缺完整 profile 或 qualification 時明示 legacy/unknown；保留可讀性不等於取得新版資格。
只有可信 snapshot 足以重建當時 schema／policy 的既有 frozen run，才能依該可證版本保留語意。
缺此證據時標記 legacy/unversioned/unknown，不猜填今天或某個舊版本、不回寫原 chain，
也不宣稱已重建可執行政策；必須經正式且具 authority 的重新接受，才可切到新版契約。

### R6 — 生產接線與明確外部邊界

一般派工、workflow card、純規劃、review 模式均須在各自實際入口消費相同契約，
維持其 sandbox、工具、last-message、terminal、cancel/timeout 行為；不能只更新資料類型或展示層。
觀測／usage／quota capability 的 adapter 契約只描述資料可用性，不在本票實作額度觀測、
預估、預留、admission 或自動 fallback。
[PatchMUD #37](https://github.com/hamanpaul/paulsha-patchmud/issues/37) 提供版本化
CLI/file report、profile-aware fingerprint 與 immutable fixture/revision；Cortex 零 PatchMUD
runtime import、不代修 producer，不執行新的 benchmark 來補規劃證據。

## Ownership

| Owner | 本件使用的邊界；不接管的工作 |
|---|---|
| #835 | execution-profile schema、canonical key、adapter conformance、production 消費與相容 binding |
| #842 | report → candidate → 明示 human-review receipt → approved roster 的發布、撤銷、有效期、CAS/crash、legacy qualification migration |
| #581 | 原五項：doctor planning 身分、unknown-role、非 dispatch provenance、doctor overlay 公開 API、PatchMUD eval producer 管線 |
| #534／#205 | 既有核可／分層排序政策與 per-work explicit chain，不重新授權或擴 scope |
| PatchMUD #37 | 外部 producer schema、requested/resolved/observed 證據與 immutable fixture，僅 CLI/file 接合 |
| #483／#475 | 既有 effort repro／自訂 executable 原 scope，不藉本件重開同類修復 |
| #600／#836–#840 | availability、quota、forecast、reservation、admission、decision status 下游，各自保留 owner |

#835 原票記載的 qualification owner gap，已由 root 後續裁定 #842 接手，不再是無 owner 缺口，
也不能塞回 #581。沿用 `envelope_mapping.map_report_to_envelope` 的純 mapping，
不把測量 pass 或 descriptor 登錄當作批准。#842 若依政策要求真人核可，live gate 必須取得
對 exact qualification 的有效真人 receipt；測試 fixture、agent review、issue／plan 核可均不能代替。

## Invariants

- I1：requested／resolved／observed 分立，unknown 不得被補成實測或當作零。
- I2：既有協定擴充 model／原生 effort 為 descriptor-only，中央 consumer 不增產品分支。
- I3：unsupported configuration 零 spawn；adapter 不能降低工具、sandbox 或 timeout/cancel 契約。
- I4：pin 與 preference 不混用，無合格 pin 不靜默換配置。
- I5：最低品質、role、independence、Trust Root 與權限是硬條件，quota 不能覆蓋。
- I6：canonical actual-condition key 隨執行條件改變，對順序、價格、時間戳穩定。
- I7：profile key 不授予 qualification；不跨 profile／role／coverage 借用成績。
- I8：未知版本／殘缺輸入 fail-closed；legacy 可讀不等於新版合格。
- I9：重啟保留凍結 chain 與既有 attempt/evidence/receipt，無歷史回寫。
- I10：各生產入口消費同一配置契約，fixture／source 成功不冒充 real launcher 或 live 證據。

## Acceptance Criteria

以下 A1–A6 與 #835 六條機械驗收逐項對應，均未執行，不得預先勾選。

- [ ] A1：虛構 model＋新原生 effort 的 descriptor-only fixture，exact requested/resolved profile；
      不改 central resolver 型號分支。重排 descriptor 欄位不改 key；不同 adapter 的同名 effort 不被視為等價。
- [ ] A2：虛構 adapter 驗 argv、terminal、usage、quota capability、cancel/timeout、工具／sandbox；
      unsupported 零 spawn。受控本地 subprocess 可驗生命週期，不呼叫模型 API。
- [ ] A3：unknown role、pin 不符、qualification 不足、同 domain reviewer、Trust Root 不合均拒絕；
      以「quota 足夠」的 negative fixture 證明仍不能繞過，保留無副作用的拒絕原因。
- [ ] A4：effort/adapter/loadout/toolchain 改變會改 actual-condition key；時間戳與價格不漂移；
      requested 不冒充 observed，缺任一必要 observed 欄位不能獲得完整實測資格。
- [ ] A5：migration/restart 後 legacy manifest、凍結 chain 語意不變；未知版本／殘缺 descriptor 拒收；
      隔離 state 的 crash/reload 不產生矛盾 binding，歷史 receipt／attempt/evidence bytes 或內容 digest 不變。
      負例：歷史 run 缺足以重建原 schema／policy 的可信 snapshot 時，結果為 legacy/unversioned/unknown，
      不猜版、不回寫 chain、不沿用推測的品質豁免；未經正式 authority 重新接受不得切到新版。
- [ ] A6：既有 model-resolution/per-work-chain/launcher 回歸，加一般派工／workflow／純規劃／review
      的 production producer/consumer contract；與 PatchMUD #37 immutable fixture/revision 實際互通。
      fake 結果不能冒充真 launcher 驗收；real launcher、installed、live 各需獨立授權與證據。

## Delivery Gates

依 [設計](execution-profile-contract-design.md) 與 [Todo](../workstreams/execution-profile-contract/todo.md)
人工拆分後才可註冊／freeze／Cortex build。規劃完整性、真正 qualification、產品測試、PR 合併、
installed artifact 與 live acceptance 分帳；任一外部依賴未驗證不能標成整件完成。
本 intake 不授權修改現場 registry、重啟 active Manager、付費 probe 或 benchmark。
