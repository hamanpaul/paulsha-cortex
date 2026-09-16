---
status: accepted
work_item: executor-backoff-store-core
---

# Executor backoff store 核心設計

## Decisions

### D1 Ownership and input contract

流程：可信 caller 的 immutable event/policy envelope → schema/完整性檢查 → root lock 內重讀 ledger → event-time pure fold → atomic store/ack commit → fresh observation；caller terminal inventory → ack 對帳 → 獨立 reconciliation 狀態。只有 C/D 能把最後結果接成 production admission；A 沒有 registry writer、job reader、log scanner、launcher 或模型呼叫。

輸入 envelope 必備：store scope、exact executor/model、terminal key、原 event epoch、原 payload 與其 canonical fingerprint、outcome/authority、policy revision/參數、reset provenance。caller 負責證明這是已持久且未受覆寫的真終局；A 驗證值一致，不把 JSON 形狀或 `trusted=true` 當 cryptographic/terminal authority。缺少任一必要證據即 unknown；不重讀現在的 job/settings 代填。

reset provenance 有明確 `structured`、`parsed`、`absent` 三類：前兩者須有原 reset epoch、source/evidence ref、解析規則版本；parsed 另有明示 timezone/base-year/原解析基準時刻。absent 是 caller 已可靠判斷無可用 hint 的明確結果，不是 metadata 沒傳。A 不解析文字，也不升級 classification authority；外層 envelope 不改既有 provider_outcome 四鍵＋可選 reset_at。原 payload/reset/provenance 相矛盾回 unknown。

policy envelope 凍結 rate base、quota base、margin、倍率／上限及算法 revision；所有數值有限且範圍合法，quota base > rate base，margin 非負且受該 revision 明定上界限制。v1 只接受有明確 interpreter 的 revision；支援新 revision 必須保留舊語意與 golden oracle，不以新增模型/agent/effort 重寫 key 或算法。本模組可重用現有 backoff helper，但須驗該 helper 語意等於 frozen revision，不能讓 helper/settings 升級改掉 replay 結果。

### D2 Schema and explicit observation

schema `executor-backoff/v1` 儲存 scope、canonical event map、每 identity aggregate、事件 fingerprint/ack、policy/provenance 與資料版本。terminal key 全 store 唯一；identity 用結構化二元 key，不靠可碰撞的分隔字串。同 key 重送先比完整 canonical envelope；缺欄位／變更 identity/time/policy/reset 皆拒絕且不覆蓋原記憶。

epoch/deadline/policy 數值拒絕 bool、NaN、infinity 與運算溢位，hits 為非 bool 的正整數；empty identity/key、非法 outcome/authority、fingerprint 不符與未知資料版本回 unknown。可採 reset 不早於其原解析基準時刻（Retry-After 0 可等於基準）；缺資料不默認 absent，A 不把現在時間代入驗舊事件。

`read_store` / `active_backoff` / `record_backoff` / `clear_backoff` 的確切 Python types 隨實作固定於同模組，但必須顯式保存兩個維度：

| 維度 | 值 | 禁止的推論 |
|---|---|---|
| store observation | missing / valid / unknown，附 last-good 與具體 diagnostic | missing 或 valid 無 active ≠ quota 足夠 |
| reconciliation | unverified / pending / complete / unknown，附 missing/conflicting keys 與 caller evidence ref | 沒提供 inventory ≠ complete；可解析旧檔 ≠ 新 intent 已 ack |

query `now` 只決定 active/expired，永不參與 event epoch、hits、policy 或 reset 的重建。未知輸入／IO error 回 unknown，不以 `None`、空 dict 或布林 false 混同正常無 active。`complete` 僅表示「本次 supplied、具 scope/revision/completeness 聲明的 inventory」已可靠合併；是否 canonical、fresh、retained 仍是 C/D 的外部責任，沒有通用 `allowed=true` API。

### D2.1 Capacity and computation profile

v1 使用固定 `capacity_profile=bounded-ledger/v1`；同模組所有 process 採同一 profile，不從現在 model/settings 或 caller 任意覆寫上限。這是資源限制，不是 frozen backoff policy。遇未知 profile 或 runtime 無法遵守其上限回 unknown；不得自動提高 cap／改 ledger policy。測試可在隔離同一 profile fixture 注入更小上限驗邊界，不能把測試小 cap 當 production policy。

| Resource | v1 上限 | 執行點 |
|---|---|---|
| 已存／候選完整 encoded store | 2 MiB | fd size 預檢＋最多讀 cap+1 bytes；候選串流 encode 超限即停，replace 前拒收 |
| 單次 input／inventory encoded bytes | 1 MiB | bytes 長度先檢查；stream 最多 cap+1；不得先 loads/dumps 整份才量大小 |
| retained distinct events／identities | 1024／128 | 鎖內最新 ledger 與候選 union 驗證；duplicate 不算新增 |
| 單次 input records／canonical event bytes | 1024／8 KiB | 不無界消耗 iterable；逐 event canonicalize/hash 有界 |
| JSON depth／單 string bytes／key bytes／numeric token chars | 12／4 KiB／512／64 | decode 前 bounded lexical check；拒 bool-as-number、過深結構與巨型數字 |
| input＋store＋候選累計結構節點 | 131072 | bounded pre-count 與遍歷工作計數；不可只 decode 完後才檢查 |
| sort comparisons／fold event visits | 65536／2048 | 舊 store 驗證與候選 union 各最多一輪；不對每個 incoming event 重 fold 全 ledger |
| decode/hash/encode 累計 byte work | 16 MiB／operation | 所有通道共同計數；多 pass 不各自重置預算 |
| 合作式 compute deadline | 1 秒／operation | monotonic clock；每≤4096 bytes、≤128 nodes 或一個 fold event 檢查，排序比較亦檢查 |

只讀 regular file，fd 上限檢查後仍使用 bounded read，避免檔案增長／stat race 令 read 無界。大小只讀到 cap+1 就能判超限；超限舊 store 回 `store-too-large` unknown，不解析尾部、不截斷或 migration。shape/token 限制在一般 JSON decoder 大配置之前以有限掃描驗證；結構化 Python input 只接受可有界遍歷的資料形狀，不接受無界 generator/custom object hook，避免驗證前 deepcopy/json.dumps。caller 已配置的記憶體不在 A 所有權內，但 A 自己不得先完整複製／展開超限 input。

計算界為固定 byte/node/comparison/visit 上限；因此 retained ledger 即使永不 GC，A 單操作擁有的解碼／排序／fold／輸出配置也不隨運行歷史無限增長，不聲稱掌控外部 caller 數或 host 全域記憶體。deadline 超出回 `computation-budget-exceeded` unknown；不靠背景 worker 超時後棄置工作／另起 worker。所有可拒收的 CPU/容量檢查都在 replace 前完成，commit 後不再套新增事件拒收判定；post-replace sync failure 仍是 D3 durability-unknown，而非「零 replace」的計算超限。deadline 只約束可合作中止的 CPU 工作，不能宣稱能搶占阻塞的 filesystem syscall 或 OS scheduling；lock acquisition timeout 與 POSIX durability/IO 故障維持 D3 的獨立契約。

容量判斷順序：先 bounded input／fresh store validation → 同 key 完整 fingerprint/provenance 比對與去重 → 計候選 distinct union／output bytes/work budget → 單次 atomic commit。合法 store **恰達 cap** 時，size 合法的 exact duplicate 仍回 unchanged，bytes/acks/hits/deadline 不變；不得一看到 count==cap 就短路拒 duplicate。same-key 衝突仍 integrity unknown。任何新增事件使一項 cap 超出，整批回 `capacity-exceeded` unknown，不部分 ack、不更動原 bytes，也不從別 identity 刪資料騰位。若 input 自身超限或舊 store 已越安全 cap，則無法承諾辨認 duplicate，回具體 unknown，不突破讀取界。

飽和不是遺忘權：expiry/clear 不清 ledger 額度；跨 process pending intent 仍由 C/D 的 canonical terminal inventory 找出，不能用舊合法 at-cap store 把新事件說成已合併。正常 reader 仍可觀察 at-cap store 的已知事實，但有未合併新事件時 reconciliation 保持 pending/unknown，不能變成 admission 完成。滿額後拒新是一個明示的可用性限制；後续擴容量／安全 GC 需另裁，不在 A 自動恢復。

### D3 Cross-process commit and failures

使用與 state 檔分離且不隨 replace 更換 inode 的 lock 檔；同 root 所有 writers 使用 `fcntl.flock` 排他鎖，read/對帳使用相容鎖界。鎖取得需可控有限 timeout，timeout/permission error 回 unknown，finally 釋放 descriptor；不刪 lock 檔以免形成兩把鎖。

writer 取得鎖後才按 D2.1 bounded read 重讀最新 state，在 byte/node/work/deadline 界內驗證完整 ledger/aggregate，再有界寫同目錄唯一 temp，flush/fsync 後 atomic replace，並完成目錄 durability barrier。候選容量與計算預算不足在 replace 前拒絕，舊 bytes/acks 保留；不能因留住所有歷史而無限持鎖。不能共用固定 `.tmp` 路徑、用 process cache 當基底、把 corrupt 檔改成空檔，或先 ack 再提交事件。無變化的相同重送不重寫檔案。

故障切點分開驗：read／validation／temp write／temp sync／replace 前失敗保留舊檔 bytes、無新 ack；replace 失敗也不能回 success。replace 成功但後續 durability barrier 失敗時，檔案可能已是新版本，必須回 `commit-durability-unknown`，不可假稱 rollback 舊 bytes。另一 process 不得僅因新檔可解析而解除：所有 fresh valid observation 須在同 lock 內完成該 data fd/目錄所需 durability check，再由 caller 以 fresh terminal inventory 驗 ack。barrier 持續失敗即持續 unknown；barrier 恢復且全部 intent 可靠收斂才可回 valid/complete。這是明示的 store protocol，不靠可能寫不出的 error sidecar 或 process sticky flag。

read permission/IO/unknown schema、lock timeout、同 key 衝突與政策 mismatch 均不會清掉別的 identity。last-good 只可引用同 scope、曾驗證之已知內容；fresh process 拿不到可信 last-good 就明說 unknown，不憑舊 reader cache 宣稱全貌。crash、sync 與 lock 行為由 POSIX 本地檔案系統 fixture 驗證，不聲稱已認證所有網路檔案系統。

#### D3.1 Real process-death test boundary

T09 同時要求 exception fault 與真正 process-death，兩者不可互代。harness 先把 A/B immutable intent 寫入隔離 durable fixture inventory、store 僅有 A，再以 `multiprocessing` 的 fresh/spawn writer 操作 B；writer 用 Event/barrier 明確向 parent 確認以下切點。parent 僅對本測試直接建立、持有 Process handle/PID 的 writer 發 SIGKILL（或等效不可捕捉的 process kill），有界 join 並驗 exitcode；禁止搜尋／終止既有 daemon、provider、其他 agent 或任意 PID。不得讓 writer 捕捉例外後執行正常 rollback/finally 來替代死亡窗口。

| 切點 | Kill 的精確位置 | Fresh-process oracle（先觀察、再對帳） |
|---|---|---|
| K0 | temp 已寫/sync，atomic replace 尚未呼叫 | 舊 store bytes/ack A 保留、B 未 ack；B 仍在 durable fixture inventory，可靠 replay/commit 前 reconciliation 不完整 |
| K1 | replace 已回成功，directory durability barrier 尚未執行 | 不憑可見新 bytes/ack 就宣稱完成；fresh process 同 lock 內重讀 data、執行 data/directory sync，並重讀 durable inventory 核對；sync/merge 失敗保持 unknown |
| K2 | data 與 directory barrier 已成功，回傳 caller 前 | 在本測試持續運作的 filesystem 觀察已提交版本與 B ack；fresh sync/inventory 對帳後重送 B 仍 unchanged，不增加 hits/deadline |

每個切點都另啟不承接 writer 記憶體/cache 的 fresh reader/consumer，驗證死亡 writer 的 flock 已由 OS 釋放、data/directory sync、durable fixture inventory 的完整重讀，以及 pending→可靠合併的結果；不能只重開同一物件或沿用 parent 已讀 inventory。wait/kill/join 全有界，finally 清理只限自建 subprocess 與 tmp fixture。這證明 process-death/restart protocol，不是 machine power-loss、掉電後 filesystem replay，亦不宣稱重現任意 filesystem 的持久性行為。

### D4 Deterministic fold and expiry

v1 保存容量內已接收的完整事件，無 GC/checkpoint 最佳化；超限新事件按 D2.1 原子拒收，不能要求先全讀無限 ledger 才判斷。驗 byte/node/event cap 後，在固定 comparison/fold budget 內按 `(event_epoch, terminal_key)` 排序，從 deadline 無值/hits=0 開始：

1. 首事件，或 `event_epoch >= previous_deadline`：本事件 hits=1；否則 previous hits+1。
2. structured/parsed reset 已由 caller 證明可採時，候選為 reset+frozen margin；明示 absent 才用 `event_epoch + frozen_backoff(base(outcome), hits)`。
3. `deadline=max(previous_deadline,candidate)`；輸出末事件的 episode hits。若加入較舊事件，從完整 ledger 重 fold；不按 arrival order 或 record now 更新。
4. 驗 store aggregate 與 event ledger 一致；不一致一律 integrity unknown，若舊 persisted deadline 仍 active 另保留其最低保護界，不因 expiry 掩蓋完整性問題。正常同政策完整集合的 aggregate 必須 arrival permutation 不變。

固定測試 policy：rate base=10、quota base=40、margin=5、倍率2、max exponent4。先寫 event=200/reset=1000，再寫 event=100/reset=300，最終 deadline=1005、hits=2；反向、同時刻 key tie、混合 absent 及 fresh process 皆依相同 fold。`event_epoch == previous_deadline` 重開 episode；query now 遠晚於 deadline 不改 events/hits。

`clear_backoff` 遇 active entry 回 `active-cooldown` 拒絕；expired 時可冪等 no-op，因 active projection 本就由 query clock 導出。ack/event/policy 與歷史 aggregate 保留，不按期限清除，也不因此釋放事件額度。成功 job、auth success 不在 record 的 eligible outcomes，不能化為 clear；較短新 reset 是新事件候選，不是縮短權。ledger 在 D2.1 cap 停止接收新事件，容量／計算／磁碟不足均回 unknown、不丟 bytes/acks；安全 compaction 與擴容留後續契約，不再用 disk-full residual 代替記憶體／CPU 保護。

### D5 Reconciliation seam and delivery boundary

原子替換失敗後，未合併 intent 的 durable authority 位於 caller 的 canonical terminal evidence，不在 A 新 error 檔。A 接收 caller 提供的完整／不可讀／未完整 inventory 描述與其 immutable events，核對 event keys、fingerprints、policy 與 ack。缺對帳／不可讀／缺 retention 證明不能默認零 pending；record 再失敗仍 unknown。old-valid-A＋new-unacked-B 的跨 process 測試必須明確把 B 以 fixture caller 的 durable inventory 送給 fresh consumer，不能僅在單 process 呼叫兩次 raw reader。

fixture caller 是測試邊界，不是聲稱 C/D 已存在。A 不掃 live/installed state，不新增 registry writer，不修可覆寫 exited_at。C 必須提供 immutable terminal/provenance、strict fresh reader、retention；D 必須在所有 admission 前取 fresh inventory 並對帳，E/F 接 consumers/slice。A 無 API 可以獨自證明「沒有其他未 ack terminal」；該全稱性交 C/D runtime invariant tests，不由 source scan 或測試注入 always-complete inventory 背書。

### D6 Risk matrix and negative controls

所有測試使用 tmp_path、fake clock、multiprocessing Event/barrier；等待有界，finally 放行/join，禁止真 provider、daemon、模型或 live store。沿用 pytest，不新增框架。

| ID / AC | Surface / Harness | Oracle / 負控制 |
|---|---|---|
| T01 R1 | 真 missing、corrupt bytes、unknown schema、read fault | 三態不同；corrupt 不寫 empty；把 unknown 壓成 missing 的 mutant 必紅 |
| T02 R2 | fresh processes 同 root，鎖內 pause 後第二 writer | 寫入序列互斥、無 lost update；移除 flock/reload 的 mutant 必紅 |
| T03 R2/R3 | 兩 identity record 與 expired clear 交錯 | 他 key bytes/ack/deadline 保留；固定 temp/stale RMW mutant 必紅 |
| T04 R4 | 原事件 replay、同 key/time/policy/identity 衝突 | replay bytes/hits/deadline 不變；衝突 unknown；僅 last-key 去重 mutant 必紅 |
| T05 R5/R6 | 小集合全 permutation、same-time tie、event-time=deadline | reset1000/300→1005/hits2；arrival-now 與 last-arrival-wins mutant 必紅 |
| T06 R6 | 改 caller 現在 settings/now，重送 frozen envelope | 原結果不變；缺/未知 policy/provenance unknown；套當前 settings mutant 必紅 |
| T07 R7 | expiry/clear→restart→舊 replay＋未 ack 舊事件 | 不復活、不丟 ack；按 TTL 刪 ledger mutant 必紅；active clear 拒絕 |
| T08 R2/R8 | durable fixture inventory A+B、store 只有 A，replace fault，fresh consumer | B 未合併仍 unknown；只讀舊合法檔當恢復的 mutant 必紅 |
| T09 R2 | temp sync／replace／dir sync exception faults，另依 D3.1 在 K0/K1/K2 真 kill 自建 writer＋fresh process | 三切點以 barrier 定位；驗 old/committed bytes、flock 釋放、fresh data+directory sync 與 durable inventory 對帳；K1 不僅靠可解析新檔，K2 replay unchanged；exception-only 不算 crash，SIGKILL 不算斷電 |
| T10 R1/R8 | inventory omitted、unreadable、stale/incomplete window、fake last-good | unverified/unknown 不等 complete；預設空 inventory mutant 必紅 |
| T11 R4/R6/R7 | hint、success、auth success、短 reset、缺 identity | 不造 key、不清 active；可採短 reset 仍只做 max；fallback policy 不猜 |
| T12 R2/R8 | lock timeout、exception cleanup、有限多輪 process replay | descriptors/locks/processes 收斂；無死鎖；測試失敗 finally 仍放行 |
| T13 R1/R2 | store/input cap+1、成長中 fd、深度/node/string/number 超限 fixture | 讀取≤cap+1；decode/copy 前拒；store bytes/acks 不變；先無界 read/loads/dumps 後檢查 mutant 必紅 |
| T14 R2/R3/R7 | 合法 ledger 填到 count/identity/encoded-byte cap；兩 process 同搶最後額度 | 最多合法一筆新增被 ack；另一筆 unknown，無 lost update/越限/部分 batch；expiry 不釋放額度；自動丟最舊 event mutant 必紅 |
| T15 R4/R7/R8 | at-cap exact duplicate→fresh process→expiry/clear→duplicate；同 key 衝突、新 distinct event | duplicate bytes/hits/deadline/acks 不變；衝突 integrity unknown；新事件 capacity unknown，未 ack intent 仍可見；先 count==cap 就拒 duplicate mutant 必紅 |
| T16 R2/R5/R8 | 受控 byte/node/comparison/fold counter 與 fake monotonic deadline；近 cap 單操作 | 在各有限界停止、釋放鎖，超預算 unknown 且零 replace；無每新增 event 重 fold 全 ledger；忽略 counters/deadline mutant 必紅 |

negative controls 在隔離測試 harness 或 disposable mutation 環境跑，記 oracle 名、失敗原因與還原；不把 mutant 提交到 production。本 authoring 不執行產品 RED/GREEN。

### D7 Sizing and acceptance

唯一 production 模組→domain=0；跨 process lock/atomicity/durable ack→state=2；spec 八組→invariant_count=8。容量修正強化 R1/R2/R4/R7 的 bounded observation／atomic refusal／duplicate／retention，不另藏跨模組或降低 state。fix-standard 保留 cards=9、bindings=9、core gates=2、R-09/R-16/R-19 全集：`[0,2,2,2,2]=8/Red`；#831 完整 accepted case 僅 stability→0 時投影 `[0,2,2,0,2]=6/Yellow`。不得減 cards、改 combo、降 state 或用缺規格換低分；實際正式 gate 仍 Red 就停，不派 build。

A 的完成界線是單模組 store 的規格、unit/component/跨 process 故障測試、文件／CLI 相容檢查、獨立 review、policy/CI、merge 與隔離 installed import/API smoke 分別有證據。尚未 C/D/E/F wiring 的 live 安全層與 #825 母票保持未完成；root 才能裁決真正 admission rollout。第二 production 模組會使 domain≥1/state2，#831 後≥7/Red，必須再拆或重新裁決。
