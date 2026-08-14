---
status: accepted
work_item: fix-rate-limit-classification
---

# fix-rate-limit-classification Todo

## Tasks

- [x] providers.py 分類順序修正（rate limit 判定先於 auth 字樣）
- [x] `_authority_from_canonical_row` 對 rate-limit degraded 給專屬 reason code
- [x] doctor gh-auth 區分限流與憑證失效
- [x] durable backoff deadline，operator resume 不再立即重撞

## Tasks（`#506`：secondary rate limit 防護，2026-08-13 fleet 升級實測事故）

`#370` 解決的是「限流被誤判成憑證失效」的**分類**問題；`#506` 是同一條路徑上的**用量**問題：
monitor 每 5 分鐘對全 fleet 掃描、auto-claim scan 每 tick 對每個 mapped issue 各發一次
`gh api` 讀 label（`coordinator/work_actions.py:3425`，per-tick O(n)），與 auto-advance／
foreign review／digest 共用同一個帳號配額。實測（2026-08-13 fleet 1.0.15→1.0.17 批次升級）
中 provider 反覆 degraded，`github:hamanpaul/paulsha-cortex` 長時間停在
`github rate limit exceeded`，導致 claim 全面 fail-closed——**primary 配額其實還剩 4800+**，
是 burst 觸發的 secondary limit。

- [ ] 403 分診：收到 rate-limit 型 403 時先查 `rate_limit` 端點（不計配額）分辨 primary／secondary；`remaining > 0` 即 secondary → 指數退避（尊重 `Retry-After`／`x-ratelimit-reset`），**不得**觸發任何憑證類 recovery
- [ ] 憑證失效判定雙重驗證：`gh auth status` 回報 invalid 時必須與 `rate_limit` 端點交叉確認才可下結論（限流期間 `gh auth status` 實測會誤報 token invalid）
- [ ] 禁止緊迴圈輪詢：CI／label／PR 狀態監看一律稀疏單次輪詢（≥3 分鐘＋jitter），不使用 `--watch` 類阻塞輪詢
- [ ] label 讀取批次化：scan 改以單一 GraphQL query 批量讀全部 mapped issues 的 labels（或 REST conditional request 吃 ETag／304），per-tick API 呼叫數自 O(n) 降為 O(1)
- [ ] 全域 API 預算節流：scan／auto-advance／foreign review／digest 共用一個 in-process rate budget，逼近上限時降頻而非硬撞
- [ ] 測試涵蓋：secondary 403（remaining>0）不得走憑證路徑、primary 403（remaining=0）睡到 reset、`gh auth status` 誤報 invalid 時的交叉驗證分支

## 2026-08-14 量測：主壓力源是 manager，不是 monitor，且節流閘門沒涵蓋它

PR `#512` 把 `GitHubPressureGate` 注入到 `monitor/providers.py` 的 provider，**`coordinator/`
這一側完全沒有節流也沒有退避**。實測數字：

| 來源 | 週期 | 每輪呼叫 | 換算 |
|---|---|---|---|
| cortex **manager**（`work_actions.py:3425` per-issue label 讀取） | **30s** | 57 個 mapped issue | **114 次／分鐘** |
| hippo manager | 60s | 10 | 10 次／分鐘 |
| cortex monitor（有節流） | 1200s | 約 300 | 約 15 次／分鐘 |

manager 一支就是 monitor 的七倍以上、24 小時不停。**monitor 進入退避時 manager 照打**——
一邊踩煞車一邊踩油門，這是先前反覆調 monitor 參數（1000ms → 2000ms → 收斂掃描面）都無法讓
provider 離開 degraded 的原因。

複現：`gh api --method GET --paginate --jq '.[]' "repos/<owner>/<repo>/issues?state=all&per_page=100"`
**0.4 秒即回 403**（懲罰窗作用中），同時 `gh api rate_limit` 顯示 `core remaining 4991/5000`
——再次印證這是 secondary 而非配額耗盡。

- [ ] **`GitHubPressureGate` 必須涵蓋 coordinator 側所有 `gh` 呼叫**（退避窗既是帳號層級，就該由所有打 GitHub 的路徑共用；現況 monitor 退避期間 manager 仍照打）
- [ ] **auto-claim scan 不得 per-tick per-issue 打即時 API**：monitor 的 `GitHubWorkProvider` 每輪已把 issues 連同 labels 全撈回來，coordinator 應消費那份資料或加 TTL 快取，而不是同一份資料由兩個子系統各自向 GitHub 索取、其中一個還高頻
- [ ] **`doctor` 呈現「manager 週期 × mapped issue 數 ＝ 每分鐘請求數」**：這個數字目前對 operator 完全不可見，是本次繞遠路的直接原因
- [ ] **重新檢視 `PSC_MANAGER_INTERVAL_SECONDS` 預設值**，並讓它隨 mapped issue 數自適應（或至少在超過安全速率時 fail-loud 告警）

