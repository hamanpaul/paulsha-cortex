---
status: accepted
work_item: executor-durable-backoff
---

# Executor backoff 狀態與 admission 設計

## Decisions

### D1 Store observation 與 admission 不共用真假值

資料流：真 terminal job/classification → idempotent record → executor-backoff/v1 → fresh observation → workflow/slice admission → launch 或 reasoned skip。
store reader 的狀態區分 missing/valid/unknown；active_backoff 的「無 active entry」不可承載 unknown。可用 result type 或明確可識別 exception adapter，但 consumers 必須保留三態，不得 catch-all 回 None。missing 只表示本機沒有已知失敗記憶，不能提供 quota-positive 證據。
unknown 將該 coordinator store 管轄的新增 executor admission 全部暫停；last-good 僅補充已知期限，不把缺失 entry 當已知可用。已執行 job 仍可 poll/terminalize；有效檔且 D2 未合併 terminal intent 已可靠收斂後才重新裁決，無人工 override 捷徑。

### D2 冪等 ledger 與 writer 所有權

entry 至少有 executor/model、outcome、deadline、consecutive_hits、terminal identity 去重資訊及診斷來源。終局識別必須可跨 restart 驗證，不能靠 in-memory seen set；重複 terminal observation 要返回原結果。需要保留到能證明該 terminal 不會再被 replay 的邊界；不得只保存最後一個 job 而讓較舊重播再次延長。
在同 coordinator root 使用跨 process 的互斥 read-modify-write（或經機械證明的 single-writer owner），讀有效新狀態再合併，atomic replace；寫失敗保持舊檔/last-good並回 unknown/degraded，不截斷原檔。clear/expire 僅改 exact key 的已核可狀態，不誤刪其他 cooldown；無新 operator CLI。
schema 驗證 finite epoch/合法 hits/identity/terminal key；unknown schema 不做隱含 migration，corrupt 不自動寫 empty 蓋掉證據。last-good 的取得/保存若跨 process 不可證實，重啟直接保持 unknown，不聲稱重啟後知道全貌。
既有 durable job/provider_outcome 是 backoff intent 的回放權威；store 只有成功 atomic replace 後才記錄該 terminal 已合併的 acknowledgment。每次 admission/restart 先用持久 terminal identity 與 store acknowledgment 對帳，未合併則重放；對帳來源不可讀或合併再失敗時，整個 store 管轄範圍仍 unknown。禁止只讀舊合法 backoff 檔就解封，亦不能只靠未必寫得成功的新 error sidecar 或 process-local sticky flag。
terminal evidence 的引用/保留必須覆蓋這段未合併窗口；若既有 registry retention 不能證明保留，先停下處理相依邊界，不默認零 pending。重放以原 terminal 觀測時刻/identity 計算，不以現在時間反覆延長 cooldown。此對帳只讀 canonical terminal，不新增另一個 registry writer。

#### D2.1 同 identity 的亂序合併與期限不縮短

每個獨立 job 終局使用不可變的 terminal key 去重；同 key 的 payload/event-time 若不一致屬 integrity unknown，不改名當新命中。排序鍵為 `(terminal_event_epoch, terminal_key)`，event epoch 來自首次持久終局證據而非本次 record/read 的 now。缺少可信 event-time 時保持 unknown/待對帳，不用 arrival time 補值。
對同 identity 的完整已知去重事件集合依上述順序做純 fold；若無已證明可回放的 checkpoint，不得只折疊新到達的尾項：

1. 初始 deadline 無值、hits=0。對事件 i，若是首筆或 `event_epoch_i >= previous_deadline`，該事件的連續命中數 h_i=1；否則 h_i=h_previous+1。相同 epoch 用 terminal key 的穩定順序裁決。
2. 每事件候選 d_i 為可信 reset_i+margin；否則為 `event_epoch_i + tick_backoff_seconds(base(outcome_i), h_i)`。quota/rate-limited 的 base、margin 與算法 revision 為已記錄政策，replay 不套今天的新值。
3. 累積 `deadline_i=max(previous_deadline,d_i)`，最後 h_i 為當前 event-time episode 的 consecutive_hits。亂序首次到達需重新 fold 相同事件集合，不能以「最後到達的那筆」覆寫 aggregate。distinct 較舊事件可改變真正的 episode/hits，但重送不改任何 aggregate；結果不得依 arrival 順序。
4. 原先已持久的有效 deadline 仍是最低保護界；任何重建若算出更短值，要保留它並以 integrity/policy mismatch 診斷處理，不能自動降低。相同政策且完整事件集合的正常 fold 須以 permutation test 證明一致。

具體 oracle：較新事件 event_epoch=200/reset=1000 先寫，較舊未 ack 事件 event_epoch=100/reset=300 晚到。後者合併後仍為 deadline=1000+margin、hits=2；反向到達、同 epoch 的 key tie、跨 process/restart 皆按相同 fold 收斂，不能提前至300+margin。再加入無 reset 的亂序事件，期限只由 event-time fold 決定，不能用到達當下 now 延長。
active_backoff 到期回無 active 僅是時間查詢結果，不刪除已合併 terminal ack、policy/event fold 所需摘要。expired clear 只可移除 active projection，必須保留去重 tombstone／可驗證 fold checkpoint；除非證明舊 terminal 已不可再 replay 且未 ack 舊事件不會落在 checkpoint 前，不能依 TTL/期限已過回收。證明不足則保留或明示 unknown，不能遺忘後當新 job。
本票沒有提早縮短 active cooldown 的 reset-reconciliation authority；clear_backoff 對仍有效 entry 必須拒絕（具體 active-cooldown reason），較短新 reset、成功 terminal、auth probe 成功也不得清除。自然到期且未合併 intent 為零才可解除；能提早縮短的可信事件／policy 由後續 quota 規格另定，本票不暗加。

### D3 Reset parser 的 authority 不升級

結構化 resetsAt 與文字互斥時保留結構化；可信未來 reset 加固定 margin。Retry-After 非負秒的 0 表示 now，再加 margin；負值/非有限/溢位不採。月日無年份只依已明示 timezone 與基準年解析，若該年所得時刻已過就回 None，不自動滾到明年。「跨年」正例需上游可信來源明示年份/reset 時刻；沒有該證據的年末→年初文字案例是保守拒收負例。無可靠 timezone/日期或模糊 DST 時不猜，採本機 backoff。
parse_reset_hint 對外維持 int/None；內部解析結果或 store/診斷 metadata 記 timezone/base-year/source，不擴充 provider_outcome 五鍵形狀。取得 timezone/now 的 seam 必須可注入 deterministic fixture，不依 CI host 時區。文字命中不改 classification.authority，僅填既有 reset_at。

### D4 最後副作用前共用 admission

workflow preflight/reroute 讀最新 observation，把 active cooldown 投影為 ProviderFreshness(degraded, executor-backoff)；all-known-cooldown 是等待，不是 provider retry failure。全候選結果中若有 unknown，不能把已知最早期限當全體可恢復時間；須回 unknown 診斷並可附已知 skipped。
slice 在 _record_pending_slice/worktree/launch 前先取得 exact identity 並查 store，缺 identity 只給診斷不可假造資格；fallback 使用 launcher 公開 model property。dispatch/retry-build/fanout/tick 和 inspect 必須區別 launched、known skip、unknown skip；共用空列表安全處理，不偽造 job_id。
適用 pin/independence 等限制仍由既有 gate 最後裁決；所有候選 backoff 的等待不能偷偷建立新 run/attempt 或消耗 provider-retry。已進行的正式 forced-retry 若沒有 replacement Job，不得為本票假裝成功，與 #830 非 Job 契約整合時需保留其較嚴格後置條件。

### D5 風險／測試矩陣

| Surface／風險 | Harness | Oracle |
|---|---|---|
| deadline／命中數 | fake clock + tmp store | reset+margin；quota base 更大；指數有上限；到期可再評 |
| 重送／restart／不同卡 | fresh process/JobRegistry + terminal fixture | 同終局 bytes/hits/deadline 不變；跨卡仍 skip |
| corruption／原子故障 | malformed/schema/permission fixture + replace fault | unknown、不 launch、不寫 empty、不清已知期限；有效檔且 pending intent 合併後可派 |
| 舊合法檔掩蓋新寫入故障 | store 僅 A、B terminal 已持久、replace fault、fresh process | B 未 acknowledged 時 admission 仍 unknown；可靠合併後才按 B cooldown 裁決 |
| 交錯 writer／clear | barrier-controlled processes | 兩 identity 更新皆保留；clear 不丟別 key；同 terminal 只計一次 |
| 同 identity／亂序首次到達 | 新 job reset1000→舊未 ack job reset300；反向/permutation/fresh process | deadline仍1000+margin、hits按 event-time fold 一致；now 改變不重新延長 |
| 到期／ack 回收／舊終局重播 | expire/clear→restart→同 terminal 重送＋未 ack 舊事件 | ack/tombstone 仍有效，重送不變 hits/deadline、不復活已過期冷卻；有缺口則 unknown |
| 非授權提早解封 | active entry + 較短 reset/成功 job/auth success/clear | 不縮短、不提前 launch；自然 expiry 且 pending=0 才解除 |
| parser／時區 | aware fake now/operator timezone fixture | 跨日/年/DST/過去/非法/Retry-After 0/負數；結構化優先 |
| workflow／pin | 既有 provider recovery harness | 合法下一候選帶 receipt；全冷卻不加 retry；unknown 無假 deadline |
| slice/request adapter | fake launcher + registry/worktree spies | 副作用皆零、仍 dispatchable、無 IndexError/假 retry 成功 |
| CLI／read model | 真 parser + 隔離 fixture result JSON | reason/skipped/deadline 可見；unknown 不等於額度足夠 |

不使用真額度打滿作回歸；先 pure/store，再故障/replay，再雙 lane/request wiring，最後短 CLI/integration 和文檔。既有 GitHub provider_backoff 的 corrupt→none 測試保留，它不是新 executor store 的 oracle。

### D6 Sizing 與後續

≥4 production 模組 → domain_breadth=2；跨 process concurrency/atomicity/durability → state_consistency=2；原9組驗收不變量加上亂序合併一致性、到期後去重保存兩組，invariant_count=11，不以原先較小宣告遮蔽新增驗收。現行完整 fix-standard 預期 10/Red；#831 後仍可能 Red，正式實跑後真拆 store/parser、terminal 記錄、workflow admission、slice/request consumers，拆分不可讓尚未接 gate 的 half-feature宣稱安全層已交付。
共享 quota pool、forecast、reservation、動態 model/agent/effort 不在此最小層；上位 `docs/superpowers/plans/2026-09-07-cortex-refine-complete.md` 的 R05/R08/R09 與 D4–D7 後續維持未完成，不因本票 merge 關閉。
