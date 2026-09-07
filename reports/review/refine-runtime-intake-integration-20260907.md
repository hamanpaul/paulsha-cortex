# Launcher／watcher 進件整合核對

本批是 repository planning intake，不是產品修正。Root 在獨立
`feature/refine-runtime-intake-20260907` 整合 #823 四件與 #853 四件，
原 author 工作樹、operator、runtime、既有 run／candidate／evidence 均保留。
產品 Tasks 全未勾，不以本 PR 合併關閉產品 issue。

## Scope 與獨立內容驗收

- #823 仍使用 `launcher-session-and-timeout`，唯一 issue 823；新增 spec/design links，
  timeout 已移至 #824 候選，probe containment 由 #851 先行。只定義 launcher-local
  helper/共用 kwargs、兩處 Popen 與 stdin 窄 retry；不改 Manager/cgroup/cancel。
  獨立 `agy_containment_review` R1 PASS，fresh-reader 五題均正確。
- #823 reviewer 另以真 constructor／argv／角色及 profile admission 的離線矩陣核對：
  14 admitted、cg/template 原 `job-runner-hardening-profile-unknown` 拒絕。
  這不是完整 launch/Popen 或 OS 證據；正式 recording matrix 與真 PGID/SID fixture
  仍須產品驗收。負控制須在 group signal 前失敗，finally 只清自建 exec child。
- #853 是 #827 的 watcher-only A child；R4 獨立內容 PASS，fresh-reader 五題均正確。
  受控 polling/raw0、generation/shared backend、固定 callback/owner、partial-scan latch
  與逐層 nonfollow dirfd VFS 有完整 W1–W12/T1–T15 契約；不能只驗外層 snapshot catch。
  POSIX 能力要求、非原子 snapshot、短命／同 metadata 變化、真延遲組成與 FD 界線均保留。
- #853 的 B work-model、C service 公平／durable publication／整體 drain 與各池合計，
  仍由母票後續 child 承接；A 規劃或日後產品通過均不足以關閉 #827。

## 真 gate 與 authority

| 工作 | 現行真計算 | 後續條件 |
|---|---|---|
| #823 session-only | complete、0+0+2+2+2=6 Yellow，feature-oneshot 11 cards／4 core gates | 正式 owner/fresh base、builder 選擇、Cortex plan gate／freeze 後才產品派工 |
| #853 watcher A | complete、0+2+2+2+2=8 Red，fix-standard 9 cards／2 core gates | 不派 builder；#831 真 loaded 後重算，6 Yellow 僅投影，不能提前採用 |

Root 以現行 79 runtime 純函式重算並驗 Tasks 缺失負控制；surface-only helper 的
`envelope_unavailable` bypass 不等 measured capability 或獨立 review。
當下實讀 Codex Luna builder 的四項 envelope source 皆 default，plan-review projection
為 None；不是遺漏傳 lookup 才創造的未知狀態，也不替 PatchMUD 量測／qualification 背書。
兩個 work 的 repository links 不代表 Monitor 已 confirmed 或既有 run 已 freeze。
#824 真 dependency marker、不登錄／不 auto-label／不 start 的邊界保持。

## 交付門檻

Root metadata 整合不得變更已審核心；本批另做 fresh 整合 review、精確 staging、
changelog fragment commit 後完整 preflight，再核對 current-head 遠端 CI／threads／
mergeability。CI pin 與本機 engine 執行身份分開保留，未取得的 gate 不預填 PASS。
安裝、loaded runtime、真模型與產品 run 的 RED/GREEN/verify/review/ship 都不由本報告代寫。
