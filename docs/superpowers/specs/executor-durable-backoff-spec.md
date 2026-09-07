---
status: accepted
work_item: executor-durable-backoff
---

# Executor 已知限流的持久退避規格

## Requirements

對應 [#825](https://github.com/hamanpaul/paulsha-cortex/issues/825)。#832 已審查 todo 與本 spec 明確取代原 issue「corrupt file→empty」條款；原 GitHub provider backoff 語意不改。

1. **R1 Durable identity cooldown**：executor/model_id 的已知 rate_limited/quota 記入獨立 executor-backoff/v1 store；每次 admission 重讀磁碟，跨 tick/card/run/Manager restart 保留，不能只用 auth process cache。
2. **R2 冪等與 atomicity**：同一終局 job 重送不增 hits、不延後 deadline；不同真 job 以 immutable event-time／去重 key 的確定排序合併，不能按 arrival time 更新。record/clear/expire 不得縮短同一或其他 identity 仍有效的 deadline；較新 reset=1000 已持久後，較舊未 ack 的 reset=300 晚到仍須保留至少1000+margin。deadline/hits 對相同事件集合與所有到達次序必須一致；到期不得遺忘 ack 後把舊終局重算為新命中。原子替換失敗後，以既有持久 terminal evidence 作未合併 intent，跨 consumer/restart 保持 unknown，直到可靠合併才解封；舊檔仍可解析不算恢復。
3. **R3 Unknown 非可用**：缺檔只有「無本機已知 backoff」，不是額度已確認充足。corrupt/unreadable/unknown schema 回 unknown/degraded；保留可得 last-good 並暫停 store 管轄範圍新增派工，不動已執行 job、不捏造 deadline、不耗 provider retry。有效 store 恢復後可重新 admission。
4. **R4 期限與 provenance**：每個終局事件優先以可信結構化 reset_at+固定小 margin 產生候選期限；無可靠 hint 用該事件時刻加有上限指數退避，quota base 大於 rate_limited。identity 的生效期限採既有期限與事件候選期限的保守最大值，不被較短 reset 覆寫。本票不授權任何事件提早縮短仍有效的 cooldown：後來較短 reset、成功 job、登入成功均不算解封權威；只有期限自然屆滿且 pending intent 已收斂才無 active backoff，主動 reset reconciliation 交後續 quota 工作。文字支援 Retry-After 秒與 Codex 月日/AM-PM，記解析 timezone/基準年；不可靠/過去/非法時刻回 None。文字不升 authority、不新增 outcome vocabulary，不改 provider_outcome 四必要 keys＋可選 reset_at。
5. **R5 終局寫入資格**：slice/workflow failed 共用 helper，只接受 rate_limited/quota 且 authority 非 hint；使用終局 job 的真 executor/model_id/job identity。缺欄位不造 key、不炸掉終局收斂，留下診斷。
6. **R6 Workflow admission**：preflight 與 reroute 都排除 cooldown；有合法候選沿原順序和資格/permission/pin/independence 規則選擇並存 dispatch_reroute/skipped。全候選 cooldown 回 executor-backoff/min retry_after/skipped，不轉 needs_human、不耗 provider-retry；unknown 使用不同 reason，不偽裝有期限。
7. **R7 Slice admission／consumer**：在 pending slice/worktree/job 副作用前讀 store；identity 優先 spec，否則 launcher 公開 executor/model，不讀 private _model。skip 保持 dispatchable；dispatch/retry-build/fanout/tick 回 skip 明細，未知另診斷；不取空 dispatched[0]、不誤拋 retry-build-dispatch-failed。
8. **R8 測試與觀測**：純 parser/clock/store、跨 process/原子寫入故障/同終局重播、兩 lane 和全部 request consumer 的 RED/GREEN；inspect/tick 可觀察 reason/skipped/retry_after，tests 不使用真 provider/模型；未知餘量不得顯示已確認足夠。
9. **R9 後續 quota 邊界**：本票只是被動最小安全層。共享 account/quota pool、需求 forecast、reservation、動態 model/agent/effort 選擇仍由 refine 後續 D4–D7／R05/R08/R09 承接；executor/model key 不等於獨立額度池，#825 merge 不代表第 5 類問題完成。

## Boundary

Production 涵蓋新 executor_backoff、reset hint 純函式、provider_outcome、manager、autonomy、manager_daemon、launcher 的接線，必要時 registry 只處理新 job evidence 的相容性。保留 GitHub backoff namespace、taxonomy、spawn admission、候選順序；不新增 override/clear CLI、不降低 pin/independence，不能用換模型保證同池有餘量。
這是 ≥4 模組加 durability/concurrency 的真實整包，不得直接派 Red build；#831 落地後重評仍 Red 必須真拆，各 child 保持本規格安全不變量。

## Evidence

基底 `60a3ffa867377b0c86fa2f10e91fb9820d91938c`：provider_backoff.py:1 僅 GitHub，:42 的 fail-soft reader 不能複製為本票語意；manager.py:3975 的 reroute、:8466 runtime preflight；autonomy.py:592 dispatch_ready、:662 pending side effect；launcher.py:1430 只有 executor 公開 property；provider_outcome.py:89/126 既有可選 reset_at。新 store 尚未存在，intake 不等於產品已接線。
