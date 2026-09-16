## Context

本次沿用既有 Cortex 工作生命週期、角色能力與核可分層。P1 intake 尚未實作，已有 W0 合併基底；本變更將十四類修正轉成可派工的有界批次。完整範圍與驗收見 `docs/superpowers/plans/2026-09-07-cortex-refine-complete.md`；語彙見同層 specs 的 `2026-09-07-cortex-execution-domain.md`。

## Goals / Non-Goals

Goals：全部 R01–R14 有明確 owner/scope/依賴與可機械驗收；可靠執行、可擴充 profile、quota-aware admission 及可追溯交付。

Non-Goals：固定供應商組合、繞過 Trust Root、偽造核可清單、手改 runtime registry、跨 repo 直接共享 import、以一次 benchmark 保證全部工作。PatchMUD 本輪僅開 producer issue；外部未交付的驗收不得被 fixture 通過取代。

## Decisions

1. **單一工作權威與分批交付**：保留既有 work_id；child Todo/規劃產物經現行 Cortex authority gate 進入 workflow。umbrella 只列管覆蓋，不再派一條重複實作線。相較整批重新註冊，這保留已完成審查與 evidence lineage。
2. **Profile 契約**：任務需求獨立於 executor/model/native effort/tools/isolation 的 execution profile。adapter 以資料宣告原生能力；新增協定需要 adapter/conformance，核心不加產品名稱分支。requested/resolved/observed 分開，未知不冒充實際採用。
3. **評測與即時資源分責**：PatchMUD CLI/檔案提供 profile-aware qualification/效率證據；Cortex 發布核可 candidate/receipt/roster 並收集實務 telemetry。保留角色、coverage、ranked validity、review approval、independence 約束。能力 fingerprint 不含純 pricing snapshot；成本報告另記 provenance。
4. **多池 admission**：候選先過品質/權限/獨立性/pin，再估計任務需求及所有 pool/window 約束；唯一 budget authority 原子預留完整需求才 spawn。provider 未提供餘量與外部消耗不可觀測時明示不確定性，不能用本地 token 數冒充 subscription 餘額。
5. **安全 fallback**：可在每 card/retry/review 的安全邊界重選；跨 attempt 留 supersession 與 artifact receipt。只因 lease 過期不釋放仍存活 job 的預留。全部候選不足即等待，不能降低 reviewer independence 或覆寫 explicit pin。
6. **漸進啟用**：先 deterministic fake adapter/quota/clock 與 crash/concurrency tests；telemetry/forecast 先 shadow；驗證預估誤差與 coverage 後有限 opt-in。既有 pin/legacy policy 具可回復路徑，回復不刪除已產生的 audit/reservations。
7. **不以文件代替完成**：RED/GREEN、獨立 review、policy、remote CI、精確 merge HEAD、installed/canary 各自記錄。正式進件 PR 僅引用工單，不因文件合併自動關閉實作 issue。

## Risks / Trade-offs

- provider 沒有可讀餘量介面 → unknown/partial 與 operator 風險政策、headroom、觀测更新對帳；不保證永不撞限流。
- 多 host 同帳號未共用 authority → 顯示 coverage gap；僅對已納管範圍保證不重複預留。
- 既有 effort/adapter 證據不足 → legacy/unknown；不得讓舊 high 成績充當新 max 的資格。
- 自我修復期間核心共享檔衝突 → B1 先單案 canary、隔離分支、依序整合；保留其他 workflow。
- PatchMUD 外部依賴尚未完成 → consumer 可先驗版本化 fixtures；live report→approval→dispatch 列為未完成 gate。
- cgroup/環境相依的資源預設 → #819 最小修復只改 periodic clock/明確配置；後續 cgroup 與 installer 對齊另有驗收，不能把 os.cpu_count 預設當容器配額。

## Migration Plan

B0 進件收斂 → B1 執行基礎 → B2 profile/ownership → B3 shadow telemetry/forecast → B4 opt-in admission/recovery → B5 狀態/installed runtime → B6 全部證據及 backlog closure。每批保留舊資料可讀；資料 schema 或 policy 行為改變須顯式版本化、rollback 測試與 operator receipt。

## Open Questions

供應商可讀 quota 的實際介面與覆蓋率、不同任務的 forecast 風險門檻與樣本量由 B3 baseline 決定；這些未知在未取證前不得宣稱解決。其餘實作選擇由各 child spec 在已核可的有界契約內裁決，不改變使用者的動態選模意圖。
