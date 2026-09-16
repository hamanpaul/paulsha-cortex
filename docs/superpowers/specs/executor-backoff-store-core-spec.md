---
status: accepted
work_item: executor-backoff-store-core
---

# Executor backoff store／event-fold 核心規格

## Requirements

獨立 owner 為 [#850](https://github.com/hamanpaul/paulsha-cortex/issues/850)，本批僅登錄規劃，
尚未 freeze／dispatch；`accepted` 不代表產品驗收或可派工。

本件是 [#825](https://github.com/hamanpaul/paulsha-cortex/issues/825) 母工作 `executor-durable-backoff` 的人工 child A，承接母 spec R1–R4 的 store 部分及 R8 component 驗收。母 [spec](executor-durable-backoff-spec.md)、[design](executor-durable-backoff-design.md) 與 [todo](../workstreams/executor-durable-backoff/todo.md) 的其餘 AC 不取消。`accepted` 表示本 child 文件契約，不表示已註冊、通過 sizing、實作、merge、installed 或 live。

1. **R1 Three-state bounded observation**：獨立 `<coordinator_root>/executor-backoff.json`、schema `executor-backoff/v1`，每次 store query 重新讀磁碟，遵守 design D2.1 的固定容量 profile。先作有限讀取／結構計數，再 decode；不可先無界 read/loads/dumps 才檢查長度。只有真缺檔可回 `missing`；可解析且 schema／資料／durability 驗證通過才 `valid`；read error、corrupt、未知 schema／policy、超限或無法確定持久狀態均 `unknown`。missing 只是無本機已知 backoff，valid 無 active entry 也不是 quota-positive 證據。可附已驗證 last-good，但不能把其缺少的 identity 當可用；不得以 corrupt→empty 自癒覆寫。
2. **R2 Cross-process atomic ownership**：所有 record／expire／clear 的 read-modify-write 都在同 root 的穩定 `flock` lock 邊界內重讀最新檔，使用同目錄唯一暫存檔、完整寫入與 atomic replace；不得用 stale instance cache 覆蓋。input/store bytes、事件／identity 數、解碼深度／節點與 sort/fold/encode 工作量皆有固定有限上限，另有合作式計算 deadline；檢查在大配置與 replace 前完成，超限保留舊 bytes/ack，回具體 unknown，不部分接收 batch。read／serialize／write／sync／replace／lock 失敗也不得截斷既有檔或提前發 acknowledgment。持久性未確認時不得僅以檔案可解析解封；原子 commit 與 durability 故障窗口按 design D3 驗證。crash 驗收必須在 replace 前、replace 後而 directory barrier 前、barrier 後三個精確切點，真正終止測試自建 writer process，再由 fresh process 重讀 data、完成 data/directory sync 與 durable fixture inventory 對帳；只拋可捕捉例外不算 process-death 證據，也不把 SIGKILL 說成真斷電。
3. **R3 Identity isolation**：key 是 exact `(executor, model_id)`，不是 quota pool。不同 identity 交錯 record、expired clear 都保留其他 entries／events／ack；禁止拼接歧義碰撞或推導共享帳號獨立性。缺 executor/model、terminal identity 或 scope 不能捏造 key，回 unknown／input diagnostic，不使已知 cooldown 消失。
4. **R4 Immutable events and acknowledgment**：只接收可信 caller 提供的不可變 terminal key、原 terminal event-time、identity、非 hint 的 rate_limited/quota payload、payload fingerprint 及 frozen policy/reset provenance。去重 key 在 store scope 內唯一；同 key 的時間、identity、payload 或 policy 不同是 integrity unknown，不改名計第二次。成功 record 與 ack 一起原子提交；完全相同重送不改 bytes/hits/deadline。在合法容量上限內的 store 即使已飽和，仍先核對 duplicate，再裁決新事件容量；exact duplicate 不消耗新容量、維持冪等，不因已滿而被當新事件拒收。超出讀取安全上限的既有檔則保持 unknown，不能為辨認 duplicate 而無界解析。A 不驗真 job authority、不掃 registry/log、不從可覆寫 `exited_at`、arrival now 或今天 settings 補證據。
5. **R5 Event-time fold and episode hits**：對完整已知去重事件集合按 `(terminal_event_epoch, terminal_key)` 穩定排序。首事件或 event-time ≥ 上一 fold deadline 時 hits=1，否則上一 hits+1；期限按 R6。較舊 distinct event 晚到必須重 fold，不只追加尾項。相同事件集合與政策的所有 arrival permutation、跨 process／restart 得相同 aggregate；不同 arrival now 不影響結果。同時刻 key tie、episode 邊界相等與混合無 reset 都有機械 oracle。
6. **R6 Frozen policy and conservative deadline**：每事件使用其記錄的 policy revision、rate/quota base、margin、指數曲線／上限與 reset 解析 provenance；quota base 大於 rate base。可信 reset 候選為 reset_at+margin；caller 明示已可靠判定無 reset 時才以 event-time 加有界 backoff。缺失／矛盾 provenance 不等於「無 hint」，未知政策也不能套今天公式。累積 deadline 取保守最大值，已持久仍有效 deadline 是最低保護界；重建更短則保留保護界並報 integrity unknown。success、auth success、較短 reset 不授予提早縮短權。
7. **R7 Bounded retention without forgetting**：active 僅由 query now 與 deadline 比較。v1 保留已接收的完整 dedupe events／ack／frozen policy，但不再無限接收：達事件數、identity 數、encoded store bytes 或其他資源上限時，distinct 新事件／超限 batch 回 `capacity-exceeded` unknown，原 store bytes/ack/deadline 不變，未接收 intent 留 caller 對帳。無 TTL GC 或未證明安全的 checkpoint 壓縮；expired clear 不釋放 ledger 額度。`clear_backoff` 對 active entry 明確拒絕；對 expired entry 至多清 active projection／回冪等 no-op，不刪 ledger、ack 或其他 identity。到期後舊 terminal 重送不能復活 cooldown；未 ack 舊事件在容量允許時按原 event-time fold，否則仍 unknown 而不遺忘。future GC／容量 migration 必須另有 authority，不能因磁碟或記憶體壓力静默丟事件。
8. **R8 Honest reconciliation seam and component evidence**：raw store observation 與 caller 的 terminal 對帳結果分開。query 可回 missing/valid，但 readiness 必須保留 `reconciliation=unverified|pending|complete|unknown`；缺對帳輸入預設 unverified，不能預設 pending=0。A 可驗 supplied immutable intent 集合對 ack 的 missing/conflict 及合併結果，但不背書 caller inventory 的完整性、freshness、retention 或真 admission。可信 caller 說來源不可讀／窗口不完整、pending 未合併或持久寫失敗時有效決策保持 unknown；恢復須 fresh 可信 inventory＋可靠 merge，不能靠 process-local sticky flag 或 error sidecar。所有 component tests 僅用隔離 fixture；C/D production 尚未接線必須明示。

## Boundary

- 唯一未來 production 新增：`paulsha_cortex/coordinator/executor_backoff.py`；同模組容納 types、schema validator、pure fold、store 和對帳 seam。可以唯讀重用 `backoff.tick_backoff_seconds`，不修改它或其他 production 模組。
- 本件 authoring 僅 spec/design/todo 與 `reports/review/refine-backoff-store-child-20260907.md` 四檔；產品工作、tests、changelog、registry 登錄由後續正式工作執行，現在全部未交付。
- 不實作 reset 文字 parser、launcher property、terminal producer/registry reader、Manager hooks、workflow/slice admission、request/CLI projection、registry migration/GC、新 override/clear CLI、quota forecast/reservation 或模型/effort 固定名單。
- C 的 immutable terminal/provenance／strict reader／retention 契約與 D 的 production 對帳仍未裁決或接線；A 的 component green 不得關閉 #825。任何第二 production 模組變更須先重裁 child boundary/sizing。

## Evidence

基底 `79ba644780bf1c697c722ac24a297e7d02416100`：[GitHub backoff reader](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/provider_backoff.py#L42) 的 corrupt→empty 不是 A 的 oracle；[純 backoff 曲線](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/backoff.py#L23) 為現行算法佐證。Registry [terminal self-transition](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L65)、[重寫 exited_at/payload](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L1333)、[stat error 保留 reader cache](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L376) 是 caller seam 尚不可當成既有安全保證的精確理由，不是靜態掃描背書全稱不存在。
