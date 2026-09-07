---
status: accepted
date: 2026-09-07
---

# Cortex 精修完整範圍與動態派工計畫

交付總帳：[Cortex #829](https://github.com/hamanpaul/paulsha-cortex/issues/829)。PatchMUD producer：[PatchMUD #37](https://github.com/hamanpaul/paulsha-patchmud/issues/37)。

本版把前述 14 類問題全部納入具體交代，保留既有 P1 工單並補齊未涵蓋部分。本版為使用者核可方向後的執行計畫；各項交付進度以末節 ledger 與 Cortex receipts 為準，不能以文件接受代替實作完成。

原始基線：2026-09-06 精修筆記與 2026-09-07 Claude→Codex handoff（operator 保存的唯讀歷史證據）。
本版語彙：[動態派工語彙](../specs/2026-09-07-cortex-execution-domain.md)。既有 repo `CONTEXT.md` 仍是工作生命週期與權責的基線。

## 1. 使用者已明確指定的要求

1. 前述 14 類問題都要有修正範圍、驗收與剩餘限制，不能用 P1 完成代替全部完成。
2. 額度處理包含偵測與任務用量預估，能在耗盡前選擇尚可承擔工作的 fallback。
3. 不採固定 model／agent／effort 配對作為系統規則；新模型、新執行器、新 effort 能經擴充契約加入。
4. 核對並沿用已引入的 paulsha-patchmud 評估機制，避免重做平行評測系統。

以下動態路由、額度預留及分批安排為執行設計；尚未宣稱功能已發布。先前「Claude 優先」視為本批選擇偏好；不把它寫成所有工作永久固定使用 Claude 的 invariant。明確指定不可替換的 pin 仍保留。

## 2. PatchMUD 確實已引入：目前實作與缺口

本輪直接核對 Cortex operator HEAD `002196d7`、runtime/main `79ba6447` 與 PatchMUD checkout `1184c9e`。以下 Cortex profile／resolution／mapping 檔案在兩個 Cortex revisions 之間沒有差異；未執行新 benchmark，也未消耗模型額度。

### 2.1 已有的資料流

1. `cortex model profile` porcelain → `coordinator.model_profile.run_model_profile()`。
2. 透過 CLI 執行 `patchmud run`、`patchmud report`，優先讀 `report.json`。
3. `map_report_to_envelope()` 將符合條件的 ranked report 映射為能力封套與 provenance。
4. 預設產 diff，明確 `--apply` 才寫 identity registry；現行預設目的地是 packaged `data/model-identities.yaml`。
5. 正式派工已有 `operator-overlay → evaluated-roster → packaged-fallback` 分層，以及 persona、independence domain、launcher/toolchain/credential 相容性篩選。
6. 第 2 層需要 `model-eval-roster.yaml` 的 `verdict=pass`、`review_status=approved` 與適用 role。這保留既有人工複核政策；動態選擇是在合格候選中選，不自動把評測成功等同授權。

本機有歷史報告：

operator 本機保存的 `runs/profile-claude-sonnet-20260812T140439Z/report/report.json`（未宣稱已上傳 GitHub）

它記錄 `claude:claude-sonnet-5 / P0T0R0` 的 8 runs、8 clears，並有 `tokens_per_clear`、`cost_per_clear`、QATY 等榜。這只證明當時那組評測有報告；未在本輪重跑或將它外推為今日所有任務、模型、effort 的能力。

### 2.2 接線不完整的具體證據

| 缺口 | 現有證據／入口 | 本版處置 |
|---|---|---|
| profile producer 與 evaluated-roster consumer 未形成完整發布鏈 | Cortex `model_profile.py:350,606` 預設寫 packaged registry；`model_resolution.py:650` 讀 host eval roster；模組開頭明記供給管線屬後續工作 | 建立 report → qualification candidate → review receipt → approved roster 的版本化發布／匯入流程；沿用現有機械驗證。 |
| active root 無評估合格檔案 | 本輪確認 `$HOME/.agents/config/paulsha/model-eval-roster.yaml` 不存在 | 顯示 empty／unqualified 的實況，補真實評測供給；不得造資料填空。 |
| Cortex profile 只支援少數固定配對 | `model_profile.py:54` 的 aliases 僅 claude/sonnet、agy/gemini-3.1-pro-high；`:454` 查不到就 adapter-unavailable | 改由 adapter capability 與 profile descriptor 解析；新模型在既有協定內以資料宣告加入。 |
| 評測 effort 與 production effort 不一致 | PatchMUD `cli.py:1438,1513,1522` 固定 CLI high；Cortex launcher 另有硬編與 ambient 設定 | 評測及生產共享同一個 execution profile 語意，adapter 分別轉譯並回報實際值；禁止以 high 成績冒充 max。 |
| 評測識別不足以避免不同設定混用 | Cortex `_fingerprint()` `model_profile.py:260` 未含 effort／loadout／實際 adapter 版本 | 新版 fingerprint 納入所有影響結果的已解析設定、版本與環境條件，舊證據缺欄位保持 legacy/unknown。 |
| Persona 與能力維度量測仍有限 | pilot-v1 在 `model_profile.py:60` 僅 builder；`envelope_mapping.py:184` 有三欄 default 理由 | 逐角色建立可重現測量；未量測維度不由其他角色分數代填。 |
| 實務品質與用量尚未回饋到選擇 | `model_identities.py:1655` 的 track_record=bypass；registry `:1384` 只在 headless 結束時抽 usage | 增加獨立 operational telemetry 與預估器，作為派工證據來源；不覆寫 benchmark 原始排名。 |
| 現行用量來源品質不一致 | `usage_extractors.py`：AGY unsupported、Copilot input 缺值；PatchMUD Claude adapter `:102` 缺 usage 時用字元數估算 | 所有量測帶 observed/estimated/unknown 與來源；歷史資料若無法辨識來源，不提升為權威額度事實。 |

PatchMUD 的責任是可重現的能力／效率評測與報告；Cortex 的責任是把資格、任務需求、當前額度及運作紀錄合併成派工決策。品質合格不表示此刻額度足夠，用量較低也不表示品質合格。

PatchMUD producer 由 [issue #37](https://github.com/hamanpaul/paulsha-patchmud/issues/37) 追蹤，沿用 #13 角色 deck、#23 token 語意、#16 pricing provenance 等既有工作。pricing snapshot 屬成本報告 provenance，不納入能力 fingerprint；單純調價不要求重新量測能力。effort、loadout、工具及 adapter 等影響實驗條件的變動仍須重新判定證據相容性。

## 3. 全部 14 類問題的覆蓋契約

R01–R14 是本文件的需求識別碼，**不是新建的 GitHub issue**。既有 issues 保留原 scope；超出其範圍的內容另拆 successor/child 工單，不偷偷擴大既有驗收。

| ID／問題 | 本版明確納入的修正 | 既有落點／需要補的範圍 | 必須取得的驗收證據 |
|---|---|---|---|
| R01 Monitor 堆積與 freshness | 有界 refresh worker、事件合併、各 provider 執行公平性與時間上限、具體 stale 原因；一次刷新完成後仍有新事件要再刷新 | #827；補慢 provider／持續 churn／shutdown 的案例 | fake clock 與持續事件壓力下 thread/backlog 有界；GitHub 更新不被本地刷新無限餓死；停止可退出；故障時保留 last-good。 |
| R02 Manager tick 熱迴圈 | skip/success/failure 都有明確下次 eligibility；max_load／require_idle 的有效值可見、可設定 | #819；補 manual/periodic 預設與 scope 決定 | 非 idle 持續一段時間，periodic 呼叫次數受 interval 約束；skip 不算 failure；配置錯誤與非有限值可診斷；bypass 不顯示為實際閒置。 |
| R03 evidence／terminal 冪等 | 同內容與狀態不重複產 evidence；變更恰好一次；舊 attempt 不能改現行候選或 completion | #496／#497；#501 只驗已修版本與既存污染 | 同 path 不同內容、重複 tick、重啟、delayed terminal、retry generation 的回歸；evidence hash 與 contract hash 各自保持語意。 |
| R04 registry 寫入放大 | 無變更 no-op、history 上限與可追溯封存、安全 tmp sweep、rollback 成本與相容性；高頻寫入量測 | #821；更大 storage 遷移須有 profiling 依據，不預設重寫資料庫 | 無變更零寫入；history 有界而必要 audit 證據可追；crash recovery／migration／讀者 schema 全過；真變更仍正確持久化。 |
| R05 額度／預估／fallback | 多額度池與時間窗觀測、任務需求預估、並行額度預留、動態候選選擇、持久退避與 reset reconciliation | #825 保留最小退避修復；新增 quota observation、forecast、reservation 與 admission 範圍 | 派前拒絕不足候選；同池換模型仍排除；不同池合格候選可選；並行不超額預留；重啟不丟；預估不足／外部消耗可修正；未知餘量不假裝足夠。 |
| R06 失敗分類失真 | 原始結構化原因、phase、retryability、reset 與診斷鏈一致保留；planning／build／review 共用語意 | #826 補訊號；另補 planning launcher error 被包成 content 等消費端 | auth、quota、rate-limit、effort unsupported、127、缺 handle、schema/content 各有真實 fixture；同一底層原因不因 phase 換名而丟失恢復資訊。 |
| R07 Recovery 語意與可恢復性 | 定義 resume/retry/recover/abandon 各自 precondition、effect、generation、資源處置與合法 next_actions；解除綁定必須真的生效 | #497 + 現有 recovery 家族（含 #545/#546/#547/#577 等需先查現況再拆票） | 狀態轉換矩陣、重送冪等、CAS mismatch、重啟續跑、late evidence、已合併工作禁止重派；只清受控 attempt 資源，不丟 operator 修改。 |
| R08 model/agent/effort 彈性 | 可擴充 capability/adapter 契約；任務需求與執行配置分離；effort 實際解析與 operator preference/pin 語意 | #483/#581 與新 execution-profile 範圍；替換分散硬編，不把本次型號寫成 invariant | 新虛構模型與新 effort 只改 descriptor 即能在既有 adapter 執行；新 runtime 只加 adapter 與測試、不改 central resolver；unsupported 在 spawn 前拒絕；requested/resolved 可對照。 |
| R09 評測／探活／實務紀錄 | PatchMUD qualification 供給、角色 deck、完整 profile fingerprint、版本化核可清單、TTL probe 與分層 track-record | 沿用 #452/#454/#466/#534 成果；補後續管線；PatchMUD 端獨立 PR | 一份真實 report 可追到核可條目，再追到實際派工；未測量維度為 unknown；更換 effort/adapter/toolchain 後不沿用不相容評分；評測不中斷 tick 熱路徑。 |
| R10 狀態真實性與可解釋性 | actual/planned/last execution、完整 facets、等待額度/恢復原因、有效配置與選模依據；read model 不寫 workflow | #828 負責 identity producer；另補 needs_human/freshness/decision provenance | 同卡 retry 換模型、多卡同 phase、跨 repo、未派工／已退出均不猜測；registry needs_human 保留；所有 status sections 一致；下游 fixture 獨立驗收。 |
| R11 runtime／release 一致性 | CLI 安裝、服務載入 revision、設定 revision、artifact digest 可辨識；受治理 upgrade/restart/rollback 與 source override receipt | P4 擴充成部署一致性工作；先核對既有 installer/release 實作，避免重建 | 從 checkout 外驗 installed CLI；真正服務報告載入 artifact；未重啟舊程序不算已更新；upgrade/rollback 不丟 active jobs；#820 僅計已完成的部分。 |
| R12 instance／工作所有權 | instance roots、workspaces、writer、資源與共享 quota authority 的邊界；installer/doctor 同源檢查，owner-aware 清理 | #818/#800/#476 等既有票；P0 手動配置轉成產品契約 | 多 instance 並行與重啟不互寫 registry；共享 quota 仍能協調；stop/cleanup 只作用於指定 owner；dirty artifacts 與下游工作不被混入 commit。 |
| R13 launcher／parser 基礎可靠性 | argv 引號／逗號解析、process/session 與 cgroup 生命週期、timeout/取消、terminal envelope/enum 契約、adapter conformance | #822/#823/#824 + #807/#820 residual；systemd 取消語意若超出 #823 另票 | 實際 argv round-trip；kill 單 job 不連坐 manager；manager restart 對 child 的處置有證據；timeout 可設定且 parse 正確；terminal 不靠寬鬆 status 別名繞過驗證。 |
| R14 進件／驗證／交付脫節 | 規範文件唯一來源、coverage matrix、逐項 evidence-backed checklist、可恢復分批審查與人力/用量預算、issue/PR/installed 狀態分開 | #830 非 Job 派工決策契約、#831 sizing 方向；補 Red 分解接續與 delivery-accounting；P5 backlog hygiene 沿用 | 缺任一驗收、未 commit、policy 佔位字時不可宣告完成；合法等待／轉換不冒充 Job；完整 spec 不因算法反向變高風險；Red 真正產出可追溯子工作；中斷只重跑缺失工作；同 issue 不重複註冊；已修未關逐票證據關閉。 |

「全部交代」的意思是每列均有責任模組、交付邊界、驗收與殘餘限制；不代表用有限的靜態掃描證明今後不存在同類缺陷。關鍵全稱需求由 runtime 不變量與負面測試守護。

## 4. R05 與 R08/R09 的共同設計

### 4.1 可擴充候選與資格

- Persona 與權限要求屬於任務；executor/model/effort 是每次選擇的結果。planner/builder/reviewer 不綁任何固定產品。
- 每個 executor adapter 宣告可列出的 models、每個模型的原生 effort options、工具／sandbox 能力、probe、terminal、usage、quota observation 能力與版本。
- 偏好可指向供應商、資格群組、成本／延遲條件；保留 allow/deny 與 explicit pin。相容性、獨立 reviewer domain、權限與最低品質是硬條件，不被額度或偏好覆蓋。
- 新模型／effort：既有 adapter 協定可表達時，只新增/更新 descriptor、probe 與相符 qualification；不修改 central resolver 的型號表。
- 新 agent runtime：允許新增 adapter 實作並通過 conformance／Trust Root qualification。不能承諾任意新協定零程式碼，但核心排序、額度、workflow 與 reporting 不需加入該產品的特判。
- 非已知 Persona 不自動視為 build；新角色要有明確 role capability／artifact／qualification 契約。既有相容行為需做 migration，不能一次破壞舊 manifest。
- `high`、`max` 等字串是 adapter 原生選項，不形成全域硬編枚舉或等價大小關係。使用者可指定抽象品質/預算意圖，但到實際 effort 的映射必須有宣告與證據。

### 4.2 Quota observation 與用量預估

觀測按來源分級：provider 官方可讀狀態／結構化事件優先，其次執行器實際 usage，最後才是明示估計；不抓取憑證內容、不繞過 provider 限制。實作各 adapter 時需再核對當時可用介面，本版不宣稱四家都有可查餘量 API。

Quota observation 至少包含：pool/account 的非機敏識別、適用 profile/group、unit、window、remaining 或 remaining bounds、reset_at、observed_at、TTL、authority、來源與缺值原因。同一候選可能同時受帳號短期池、週額度、模型專屬池與 concurrency limit 約束。

Usage forecast 按 task type／Persona／sizing／context 大小／工具與預期步驟／execution profile 產生需求範圍，涵蓋 planning、build、review、重試及 CLI 固定 overhead。PatchMUD report 可作冷啟動先驗，實務紀錄用於校準；跨任務分布轉移須降低信心。

- 保留 token、request、premium request、credit、時間及 API-equivalent cost 的不同單位；只有可驗證 mapping 才可換算，不能拿 tokens 直接減 subscription quota。
- 分開 input/output/cache/reasoning 的原生定義，防止累計值與增量相加、reasoning 重複計數；失敗 job 已消耗的量仍計入。
- 預估器輸出上界／分位數、樣本數、信心、模型版本與資料期間。採用哪個風險分位數由政策設定，不硬釘某個模型或 effort。
- 低樣本：採有來源的保守先驗與限額探索；完全不可比或未知時，標明 unknown，依 operator 政策選已知候選、有限 canary 或等待。不得用零代表未知，也不承諾絕不撞 limit。
- 外層互動 session／未受管 CLI：可用唯讀量測接入同一 pool，無法觀測的消耗列 coverage gap，增加 headroom 並於新 provider 觀測到來時 reconcile。

### 4.3 派工決策流程

每個安全派工點（claim/planning dispatch、每張 card、retry/review）依同一服務取得決策：

1. 凍結該次 task demand 與 policy revision；讀取最新 descriptor、qualification 與 quota observations。
2. 篩選角色能力、工具／隔離、credentials grant、review independence、品質、context capacity、明確 allow/deny/pin 等硬條件。
3. 針對每個合格 profile 估算需求；檢查它依賴的所有 pools/windows，扣除已預留額度並保留不確定性 headroom。
4. 在 feasible 候選中，依 operator preference、可驗證品質、需求估計與期限排序。保留既有來源/核可政策，動態策略另有版本與明確啟用範圍，不靜默改變 legacy pin 行為。
5. 原子取得全部必要 reservations 才可 spawn。任一池競爭失敗就重新評估，不能因先前讀到的舊餘量繼續派出。
6. 記錄決策 receipt、job/attempt/profile 綁定及預留識別。執行期間可收到增量用量與限流事件；結束後以實際用量對帳。
7. 必須切換時，先判定原 attempt 已終止或可安全交接，再建立有 supersession 關係的新 attempt。保留已驗證 artifact；中途不在未結束的工具／寫入交易上直接換模型。

若所有合格候選都無法承擔工作，呈現「等待哪個 pool、最早重評時刻、缺哪項資料」。不得以不合格模型、不同名字但相同已耗盡 pool、或降低 reviewer independence 來假造可繼續。

### 4.4 預留、持久退避與權責

- 每個共享 quota pool 指定唯一 budget authority；受管 instance 的 admission 經同一原子仲裁，避免各自持有可雙花的剩餘數字。broker 介面與 Manager 單一 writer 模型一致，非授權 job 不寫預算狀態。
- Pool 可以跨 executor/model/profile；短期與長期限制同時存在。#825 的 executor×model cooldown 作為 fallback 訊號之一，不能成為唯一額度邊界。
- 狀態持久化包含 reservation、lease/liveness、observed consumption、reset 與 decision id；敏感帳號資訊不進公開報告。
- Crash/restart 對 reservation 的處置須查 job liveness／已提交輸出與消耗；不能只因 TTL 到期就釋放仍在執行的工作額度。無法判定時保持 uncertain 並限制新的預留。
- 退避檔損毀或過期資料不得被解讀為「額度全滿」；#825 初稿的 `corrupt file → empty` 必須在擴充設計中明確區分 unknown 與可用。
- 本機多 instance 先取得原子仲裁證據；多 host 同帳號若未共用 authority，必須呈現外部消耗／分散一致性限制，不宣稱全域 reservation 保證。

### 4.5 最少 12 個必要場景

1. A 模型品質合格但短期池不足，B 有獨立池且合格：spawn B，A 不產失敗 job。
2. A/B 是同池兩模型，池耗盡：兩者皆排除，不能靠換名稱繞過。
3. 短期池足、週額度不足：仍不派；使用同單位檢查所有限制。
4. 兩個 Manager instance 同時要求剩餘一份額度：只有一方取得 reservation。
5. run 主控、worker、reviewer 共用帳號：所有已觀測消耗與預留合併；未受管 session 消耗以 coverage gap 顯示。
6. 新模型與新的非既有 effort 字串進既有 adapter：無 central resolver diff，能探活、評測並參與動態選擇。
7. 新 executor adapter 支援同樣合約：workflow 與 budget core 不改即可選用；工具或 Trust Root 不相容則拒絕。
8. profile 評的是 high、生產要求 max：不沿用舊資格/用量成績冒充相符；排程 qualification 或套明示的未量測政策。
9. provider 不提供剩餘量：unknown 有界處置，receipt 不顯示「已確認足夠」。
10. job 失敗前已花額度、隨後 Manager 重啟：消耗不歸零、不重派已完成 attempt、不重複預留。
11. reviewer 只有同 independence domain 候選有額度：等待並明示品質/獨立性阻擋，不自動放寬。
12. 預估誤差與外部消耗導致執行中限流：保存現況、記錄 pool 狀態、在安全邊界 reroute；用可追溯資料修正 estimator，不把 infrastructure failure 全算模型能力失敗。

以上使用 deterministic fake quota/adapter/clock、concurrency/crash fixtures 先驗契約；最後才安排有預算、可追溯的 live canary，避免測試本身耗盡額度。

## 5. 分批交付與每批責任邊界

此為新計畫批次，與舊筆記 P0–P5 名稱分開。以下批次不指定固定 agent 或模型；各次派工由當時合格候選與資源條件決定。實作時採隔離 worktree，跨 repo producer/consumer 分別 PR。

| 批次 | 主要範圍 | 前置依賴／交付邊界 |
|---|---|---|
| B0 現場與進件收斂 | R14 的缺驗證補齊、canonical todo 整合、scope ownership；建立 R01–R14 coverage 清單 | 保存指定 Claude session 已完成結果；不重跑整批；不動既有 paulshaclaw active jobs 與 #828 scope。 |
| B1 執行基礎穩定 | R01/R02/R03/R04/R13/R14：#822 canary、#830/#831 bootstrap、既有 P1 修復 | #820 merge 基底；canary 後優先補派工決策與 sizing，再進複雜任務；Red 分解未接續前須真實拆分，不改低分避開。monitor／daemon／registry／launcher 的共享檔案分批整合；退出暫時 bypass 要有 live 證據。#825 最小持久退避不代表 R05 完成。 |
| B2 可擴充執行與資格契約 | R08/R09/R12 的 profile、adapter、role、pool identity 與 ownership 接口 | Cortex 定義消費契約，PatchMUD 以 producer PR 支援 profile-aware 評測；既有資料 migration 與 qualification review 保留。 |
| B3 遙測與預估 | R05/R06/R09：quota observations、usage provenance、需求估計、實務紀錄 | 用既有 usage_extractors／PatchMUD reports 作來源；先 shadow 記錄不改派工；量測 forecast 誤差、可用率與來源缺口。 |
| B4 動態選模與安全恢復 | R05/R07/R08：feasible candidates、atomic reservation、fallback、recover matrix | B2/B3 具證據後小範圍 opt-in；可回復 legacy policy，但不可刪除已形成的 evidence／reservation。 |
| B5 狀態與部署契約 | R10/R11/R12：#828 producer、其餘 facets/decision receipt、installed runtime 與 instance 管理 | #828 可先獨立交付；下游顯示根據 upstream immutable fixture/pin 驗收；部署操作與 code PR 分開，不隨意重啟 active manager。 |
| B6 完整閉環與 backlog | R14 加全部 R 的端到端驗收、issue closure、文件與 runtime 一致性 | 每類 requirement → tests → merge SHA → installed/canary evidence 完整；逐票判斷已修／取代／殘餘，不批次假關閉。 |

B1 的主要目標是讓 Cortex 能可靠承接後續工作。B2–B4 完成後才可宣稱「可擴充、考慮額度與預估的動態派工」落地。B5/B6 完成後才可宣稱本版 14 類整體交付。

## 6. 可觀察指標與證據清單

- R01/R02：thread/backlog 上限、provider refresh age、poll/tick 次數與 skip 原因；持續負載壓測及停止時間。
- R03/R04/R07：每個語意轉換的 evidence/action 次數、registry bytes/writes、replayed/superseded attempt、恢復耗時與資料保存證據。
- R05/R08/R09：觀測覆蓋率、預估區間覆蓋率／誤差、同 pool double-reservation 次數、quota-caused failure／重複嘗試、fallback 原因、qualification 年齡與 profile 相符率。
- R06/R10：可診斷失敗比例、原始原因保留率、registry→status facet 一致性、actual job identity 證據缺漏率；unknown 必須保留。
- R11/R12：CLI/service/artifact/config revision 一致性、跨 instance 非預期寫入、active jobs 在 upgrade/rollback 的保存／終止 receipts。
- R14：14/14 requirement 有 owner/scope/test/evidence、未驗證 checkbox 零容忍、中斷後重跑比例、逐 issue 真實 closure。

效能／預估門檻先取 baseline，再在對應 spec 定義有理由的門檻與例外；不能用「CPU 必歸零」「所有 provider 都能精確預測」這類不可支持的保證代替驗收。

## 7. 執行契約與完成 ledger

使用者於 2026-09-07 要求修正計畫後接手 Claude 工作，持續派入 Cortex 開發直到完成。PatchMUD 端本輪授權為由 subagent 建 issue；Cortex 端承接 producer contract 消費與相容性驗證，不越界替 PatchMUD 實作。

- 實作只走 Cortex workflow；主 agent 負責進件、觀察、核對、失敗裁決與整合，禁止繞過 authority 手改 registry、手造通過 evidence 或把未驗證工作勾成完成。
- 完成門檻：每個 R 對應的必要工作均具 spec/plan、RED/GREEN、獨立 review、policy/CI、merge revision 與適當 runtime/installed 證據；R14 backlog 逐票裁決。
- 既有 #828 及 paulshaclaw 工作保留獨立所有權，不混入 intake commit、不重派、不重啟它們所依賴的 manager。
- Cortex self-hosting 修正須先單案 canary；共享 coordinator 核心依序整合。未足夠的新模型/agent 資格不得以 bypass 取得。
- PatchMUD producer 尚未交付時，Cortex 可以 fixture 契約及 legacy migration 推進；live integration 仍明列外部依賴，不能將開 issue 當完成。
- 此計畫與 OpenSpec 為 umbrella 規劃權威；保留既有 work_id，child todo 為每次 Cortex 啟動來源。umbrella 不另啟動一條重複實作 workflow。

| 批次 | 狀態 | 證據／下一動作 |
|---|---|---|
| B0 | complete | #832 規劃／進件已合併；review、PR-context preflight、CI通過，原稿及其他工作所有權保留。不是產品修正完成。 |
| B1 | in-progress | #822 RED/GREEN已採信，verify因terminal schema拒收，等待合格reviewer重試；未review/merge/installed。#831為6/Yellow、#830為8/Red；#819/#825/#827 accepted intake均仍Red，#833未實作。 |
| B2 | pending | PatchMUD #37已開；Cortex #835 profile與#842 qualification分工明確，契約待拆分／派工。 |
| B3 | pending | 觀測與預估先 shadow。 |
| B4 | pending | 資格、預估與所有權契約先過，再有限啟用動態路由。 |
| B5 | pending | #828 獨立處理；其餘狀態／部署待分拆。 |
| B6 | pending | 所有驗收證據及逐票 closure 尚未取得。 |

### 已核對的進度更新（2026-09-07 08:47 UTC）

- B0 已完成規劃／進件交付：[PR #832](https://github.com/hamanpaul/paulsha-cortex/pull/832)
  merge `60a3ffa867377b0c86fa2f10e91fb9820d91938c`。兩輪獨立 review、commit 後
  PR-context preflight、Python3.10–3.13 tests、build與四版installed smoke全部通過；
  未關閉任何尚未實作的精修 issue。
- B1 仍在進行：#822 run `workflow-97a9aa661e4816e38964` 的隔離卡經Claude429後
  正式fallback到Codex並被Manager採信；TDD RED卡已派，尚未取得產品RED/GREEN或交付。
- #831 accepted三件組在現行runtime為6/Yellow；#830為8/Red，待#831真正載入runtime
  後重評。兩者純gate的 `envelope_unavailable` 仍是既有bypass，不代表量測能力合格。
- [#833](https://github.com/hamanpaul/paulsha-cortex/issues/833) 是R14/B1的Red planner
  接續successor；#830/#831不代替它，完整parent→children→closure仍是未完成gate。
- 後續工作依[動態進件對照](../../../reports/review/refine-dynamic-intake-map-20260907.md)
  查重拆票；D1–D10只是責任切面，仍須真實sizing及accepted child plan，不直接整包派工。

### 已核對的進度更新（2026-09-07 09:30 UTC）

- [PR #834](https://github.com/hamanpaul/paulsha-cortex/pull/834) 已合併，
  merge `217ff5b701f2a6ae54a20ab07212d3acef1d2114`；修正文句後的新head
  `04418f7f09ad6ad360c93866be2bb1867fb9db70` 通過post-commit preflight、
  全CI及resolved review thread。#830/#831是進件交付，產品修正仍未完成。
- #822 RED `357dece2a328acb43022d23539f06aaca91a2598`：root集中10 failed/4 passed，
  production diff為空；GREEN `7d72dee3acd8cc36ea0a96be8713e1e52f2c7346`：
  集中14/14與Manager pytest gate通過，worker全套5662 passed/44 skipped。
  verifier AGY terminal因details不是object而拒收；candidate未變，不採用該結果。
  尚待合法verify/review/ship，不能因GREEN就關#822或宣稱B1完成。
- 既有fallback曾選到本批列出型號以外的packaged AGY 3.1；目前停在needs_human，
  後續以run-scoped選擇受本批許可且具review資格的候選，不改共享capability或放寬
  independence。這是舊runtime限制，不是本計畫新動態admission已完成。
- #819/#825/#827的accepted intake見[獨立審查／真實sizing](../../../reports/review/refine-p1-runtime-intake-20260907.md)：
  舊runtime依序7/10/9 Red。#825已補同identity不同terminal亂序的event-time合併、
  deadline不縮短及expire後ack保留；獨立複審PASS不代表產品實作。#831真正載入後
  才重新計分；#827/#825的人工child候選不是已接受的child authority。
- Monitor於09:21 UTC再次只重啟使用者層服務，GitHub freshness恢復；舊程序曾3029 threads。
  Manager與既有jobs未重啟。此為temporary recovery，不是#827有界性驗收。

### 新增 child 責任總帳（issue 已建立，產品交付未完成）

| Work scope | Owner／依賴與不可混淆的邊界 |
|---|---|
| execution profile | #835；schema-key先供#842，未知原生effort/observed不補成實測；各child仍需真實sizing。 |
| quota observation | #836；只產來源/單位/窗口/TTL/unknown，不做reservation或選模。 |
| operational forecast | #837；消費#835/#836，與benchmark分帳，不拿token直接扣訂閱額度。 |
| shared reservation | #838；共同authority/all-or-none，#818 instance契約仍獨立。 |
| quota admission | #839；依#835–#838/#842，保留pin、最低品質及review independence。 |
| decision projection | #840；消費#828 producer與#839 receipts，不改workflow真值。 |
| loaded-runtime attestation | #841；沿用installed/Trust Root receipt，另證實實際已載入程序。 |
| qualification publication | #842；report→candidate→human-review receipt→approved roster、撤銷/到期/CAS；不擴#581五項原scope。 |
| recovery conformance | #843；公開action矩陣與不變量測試；producer缺陷仍由#497/#547/#577等原票修。 |
| production stage reuse | #844，#214 successor；先限同run/claim-era安全cohort，跨run新採信未支援，不倒退既有candidate保留政策。 |
| requirement delivery accounting | #845；消費CompletionRecord與#841證據，不重建ship引擎、不代替#808/#810勾完成。 |

#835–#842的查重與read-back見[動態issue對照](../../../reports/review/refine-dynamic-issues-20260907.md)；
#843–#845各票保存不可變source與10/12/12項AC，root已全文讀回。所有新scope都尚未
implemented/tests/merge/installed/live；開票只補owner，不增加已完成數量。
PatchMUD #37仍是外部producer gate，真人qualification核可若生效也需真receipt，兩者不由agent捏造。

### Canary sizing 校正紀錄

首輪 #822 的 domain_breadth 被主 agent 設為 1，將 emitter/frontmatter 的回歸測試消費端誤算為 production 模組。依 #208 原始 rubric（0=單模組／單資料流、1=2–3 模組），本工作 production 只改 `_yaml._parse_scalar`，正確為 0；state_consistency=0、其餘實際條件不變。更正不直接改凍結 run；使用正式 abandon/重新接受流程保留原 receipts，且 yellow plan review 仍須執行。spec_stability 與原 rubric 方向不一致由 #831 列管，不以刪欄位/捏造數值繞過 sizing gate。

第二輪 plan-review completeness 因 task 首行缺 source/documentation 標記及 CLI 契約被拒。已補實際文件同步與 CLI help smoke 工作、以既有純函式確認 ready，再透過正式 abandon/重新接受處理；不把純規劃檢查當成產品測試通過。#830 修復必須區分 Job、非 Job 決策、deterministic transition 及無派工結果，保留 forced retry 的 fail-closed。

## 8. 精確來源索引

- Cortex 評測 CLI：[model_profile.py](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/porcelain/model_profile.py#L33)。
- Cortex profile producer：[核心](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/model_profile.py#L329)；aliases 在同檔 54；fingerprint 260；寫回 606。
- 能力映射：[envelope_mapping.py](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/envelope_mapping.py#L184)。
- 評估清單／排序：[model_resolution.py](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/model_resolution.py#L650)；分層 844；排序 882。
- 實際 Manager 消費：[manager.py](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L8199)。
- track-record 缺口：[model_identities.py](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/model_identities.py#L1655)。
- production usage：[usage_extractors.py](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/usage_extractors.py#L1)。
- PatchMUD 固定 effort／adapter：[cli.py](https://github.com/hamanpaul/paulsha-patchmud/blob/1184c9e33d5511648b9adbab147780cbd3a0322f/patchmud/cli.py#L1436)；report per-run usage/cost 2278。
- PatchMUD 現存報告：operator 本機 `runs/profile-claude-sonnet-20260812T140439Z/report/report.json`；對外只引用上述非機敏摘要，取得新報告後記 artifact digest。
- PatchMUD 估計 fallback：[claude_cli.py](https://github.com/hamanpaul/paulsha-patchmud/blob/1184c9e33d5511648b9adbab147780cbd3a0322f/patchmud/adapters/claude_cli.py#L102)。
