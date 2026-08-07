---
status: draft
work_item: design-adhoc-oneshot-dispatch
---

## ADDED Requirements

### Requirement: ad-hoc 一次性派工MUST繞過control queue且與宿主runtime物理隔離

系統MUST提供一個不需已安裝instance即可運作的一次性派工入口（`cortex run
once`）。該入口MUST NOT透過既有control-queue機制（`fanout`/`tick`/`work`
共用的request/poll-done流程）運作，MUST直接組裝既有的
`JobRegistry`/`Dispatcher`/`run_tick()`元件於呼叫行程內完成一輪派工。
job/slice狀態MUST落在系統暫存路徑而非任何已安裝instance的root，MUST NOT
寫入宿主`~/.agents`共用runtime狀態。既有`PSC_INSTANCE`/instance安裝機制
MUST NOT因此新增「免安裝合成instance」分支。

#### Scenario: 未安裝instance時執行一次性派工

- **WHEN** 呼叫端未曾執行過install，本機無任何instance bootstrap設定
- **THEN** 一次性派工入口仍可完成一輪派工至終局
- **THEN** 不要求、不讀取任何instance bootstrap檔

#### Scenario: 宿主已有現役instance時執行一次性派工

- **WHEN** 宿主已有現役manager daemon與其對應state
- **THEN** 一次性派工產生的狀態記錄於獨立暫存路徑，不出現在宿主state內
- **THEN** 現役daemon既有狀態不受影響

### Requirement: ad-hoc一次性派工MUST沿用既有combo治理約束，MUST NOT新增放寬七phase涵蓋的combo

一次性派工入口MUST消費既有combo（受`validate_manager_spine()`七phase
涵蓋、persona綁定、ship前reviewer三項約束管轄），MUST NOT新增違反上述
約束的更輕量combo。呼叫端提供的任務描述MUST作為既有plan phase卡片的
輸入內容，MUST NOT被設計為略過plan phase本身。

#### Scenario: 一次性派工建立WorkflowRun

- **WHEN** 一次性派工入口對某任務建立WorkflowRun
- **THEN** 其combo通過`validate_manager_spine()`七phase涵蓋檢查

### Requirement: ad-hoc一次性派工的builder identity放行MUST透過可疊加overlay，MUST NOT改動registry驗證邏輯

一次性派工入口MAY支援optional身分overlay，提供時MUST寫入該次呼叫專屬的
暫存project-config路徑後，經既有packaged+instance-local合併與shadow
衝突fail-closed邏輯驗證，MUST NOT新增另一條驗證路徑，MUST NOT寫入宿主
全域project-config路徑。未提供overlay時，身分驗證行為MUST與既有
`fanout`/`tick`對未註冊身分的fail-closed行為一致。

#### Scenario: 提供身分overlay

- **WHEN** 呼叫端提供overlay宣告registry未收錄的builder身分
- **THEN** 該次呼叫可使用該身分派工
- **THEN** 呼叫結束後宿主全域身分設定不含該筆身分

#### Scenario: overlay與packaged同鍵衝突

- **WHEN** overlay宣告的身分鍵與packaged registry相同但內容不同
- **THEN** 整次呼叫fail-closed拒絕，不啟動任何model session
