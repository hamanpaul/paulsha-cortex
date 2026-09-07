## Why

Cortex 精修原 P1 只涵蓋執行基礎的部分缺陷，未閉合全部十四類問題，且固定模型映射、評測配置與派工配置漂移、被動限流處理使接手工作反覆中斷。使用者已要求完整交代十四類修正並透過 Cortex workflow 持續交付；需要一份可追溯的跨批次驗收契約。

## What Changes

- 建立 R01–R14 覆蓋與 B0–B6 依賴／完成證據 ledger，保留既有 work_id 與已完成的修正，不重複啟動 umbrella workflow。
- 補齊 Monitor／tick／registry／evidence／launcher 的有界性、冪等與恢復驗收。
- 引入可擴充 execution profile、相符資格、版本化選模決策；任務 Persona 不永久綁定 executor/model/effort。
- 引入多池多時間窗 quota observation、來源明示的用量 forecast、原子 reservation 與安全 fallback；未知狀態維持可解釋的保守處置。
- 沿用 PatchMUD 評測 producer 與 Cortex 消費端，外部 producer 工作由 PatchMUD issue #37 列管；本變更不授權修改 PatchMUD code。
- 補齊狀態、installed runtime、instance 所有權與逐 issue delivery closure 的證據鏈。

## Capabilities

### New Capabilities

- `refine-delivery-acceptance`: 十四類精修的分批進件、執行、驗收與交付會計。
- `execution-profile-qualification`: 可擴充 executor/model/effort 配置與 profile-aware 評測核可消費契約。
- `quota-aware-admission`: 多額度池觀測、用量預估、並行預留、動態候選與安全限流恢復。

### Modified Capabilities

本 umbrella 新增交付契約；既有 lifecycle、Trust Root、status 與 release 的既定要求持續生效。child change 若需調整既有要求，須獨立 delta 與 migration 審查，不以本文件隱性放寬。

## Impact

- Cortex：coordinator、monitor、porcelain model/work/run/install/inspect、release/runtime、工作項目與測試。
- 外部檔案／CLI 契約：PatchMUD [#37](https://github.com/hamanpaul/paulsha-patchmud/issues/37)；維持零 runtime import 依賴。
- 保留 #828 producer 及並行下游工作的 ownership。自我部署須確認 active jobs 與版本，不能把 source merge 視為服務已更新。
- 正式計畫：`docs/superpowers/plans/2026-09-07-cortex-refine-complete.md`。新機制先 shadow，再有限 opt-in；不靜默修改既有明確 pin。
