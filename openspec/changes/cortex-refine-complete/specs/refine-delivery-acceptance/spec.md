## ADDED Requirements

### Requirement: R01 Monitor bounded refresh

系統 SHALL 合併相同 project 的 pending 事件、固定 worker 數、公平處理 local/remote 刷新，並保留 last-good 與具體 timeout/stale 診斷。

#### Scenario: R01 acceptance boundary
- **WHEN** 首個 refresh 受控阻塞時注入一百個同 project 事件，再放行
- **THEN** 不新增無界 worker，僅一份合併補跑；持續事件不使其他 provider 無限飢餓；stop 後不再發布。

### Requirement: R02 Periodic tick eligibility

系統 SHALL 對 skipped/success/failure 各自推進明確 eligibility，保留 skip 與 failure 的差別並曝光有效配置。

#### Scenario: R02 acceptance boundary
- **WHEN** fake clock 下持續 not-idle
- **THEN** periodic 呼叫遵守 interval、failure counter 不增加；非有限或非法配置可診斷。

### Requirement: R03 Evidence and terminal idempotency

系統 SHALL 對相同內容/狀態維持冪等並阻止非現行 attempt 重播影響 completion。

#### Scenario: R03 acceptance boundary
- **WHEN** 相同 evidence path 的內容改變，之後 terminal 舊 attempt 延遲到達
- **THEN** 真變更恰好記一次，舊 attempt 不改現行 candidate/證據；跨 restart 語意保持。

### Requirement: R04 Registry persistence hygiene

系統 SHALL 使無變更寫入 no-op、history 有界且必要稽核可追、暫存清理限於受控所有權與安全時點。

#### Scenario: R04 acceptance boundary
- **WHEN** 多次 no-op、history 超限或持久化失敗
- **THEN** 無變更不寫；輪替及 rollback/normalization 可驗證；不刪 active writer 暫存或 unrelated archive。

### Requirement: R05 Resource-aware dispatch acceptance

系統 SHALL 以 quota-aware-admission 契約驗收多池觀測、需求預估、原子預留與安全切換。

#### Scenario: R05 acceptance boundary
- **WHEN** 最小 executor cooldown 修復通過
- **THEN** R05 仍未完成，直到 forecast/reservation/多池與動態 fallback 的必要場景具證據。

### Requirement: R06 Actionable failure provenance

系統 SHALL 跨各 phase 保留可追溯原始原因、可信分類與 retryability，不以泛化文字覆蓋結構訊號。

#### Scenario: R06 acceptance boundary
- **WHEN** fake launcher 真正拋錯，經持久化/reload 後進 workflow poll
- **THEN** launch failure 原因保持；runtime-contract/sandbox drift 不被錯誤 reroute，缺 handle 不冒稱有 exception。

### Requirement: R07 Recovery transition semantics

系統 SHALL 明定每種 resume/retry/recover/abandon 的前置條件、CAS、attempt generation 與資源處置。

#### Scenario: R07 acceptance boundary
- **WHEN** 相同恢復請求重送並有 late evidence
- **THEN** 不重派已完成工作，不丟 operator artifacts；解除綁定與 supersession 原子有效。

### Requirement: R08 Extensible selection acceptance

系統 SHALL 依 execution-profile-qualification 契約分離任務需求與實際配置，保留明確 pin 的硬約束。

#### Scenario: R08 acceptance boundary
- **WHEN** 候選模型與原生 effort 新增或切換
- **THEN** 無永久角色/產品硬配對；requested/resolved/observed 可追，unsupported 在 spawn 前拒絕。

### Requirement: R09 Qualification lifecycle acceptance

系統 SHALL 讓真實評測報告、核可條目與派工 profile 可追溯，明示 TTL 探活及實務紀錄來源。

#### Scenario: R09 acceptance boundary
- **WHEN** 舊 report 缺 effort 或新角色未量測
- **THEN** 保持 legacy/unknown，不能把註冊、探活或其他角色成功當相符資格。

### Requirement: R10 Honest workflow status

系統 SHALL 一致呈現 actual/planned/last execution、registry facets、等待與選模理由；read model 不寫 workflow。

#### Scenario: R10 acceptance boundary
- **WHEN** 同卡 retry 換模型或 registry 標 needs_human
- **THEN** 各 status section 與 source job binding 一致，不由 Persona 推測實際 executor。

### Requirement: R11 Installed runtime identity

系統 SHALL 可驗證 CLI、服務已載入 artifact/config revision，並保留 upgrade/rollback job 處置證據。

#### Scenario: R11 acceptance boundary
- **WHEN** source 已 merge 但長駐程序未更新
- **THEN** 不宣稱 live 已部署；checkout 外的 installed CLI 與服務各自取證。

### Requirement: R12 Ownership and shared resources

系統 SHALL 隔離 instance 的 registry/workspaces/清理 writer，並對共享 quota 使用共同 authority。

#### Scenario: R12 acceptance boundary
- **WHEN** 多 instance 並行及重啟
- **THEN** 無非預期互寫；owner-aware stop/cleanup 不傷其他工作，共用 quota 不雙重預留。

### Requirement: R13 Launcher and parser contracts

系統 SHALL 驗 argv round-trip、session/process lifecycle、timeout/取消與 terminal schema，不以放寬 status 偽造通過。

#### Scenario: R13 acceptance boundary
- **WHEN** 引號含逗號或右括號、單 job 被取消、manager unit 重啟
- **THEN** argv 完整；單 job kill 不連坐 manager；session 測試不冒充 cgroup restart survival。

### Requirement: R14 End-to-end delivery accounting

系統 SHALL 維持十四類 requirement 到 child work/test/review/merge/installed 證據的完整 ledger，逐票裁決已修/取代/殘餘。

#### Scenario: R14 acceptance boundary
- **WHEN** intake 文檔或單一 PR 已完成但尚缺必要 live 驗收
- **THEN** 不能宣稱全部完成或批次關票；中斷後只重做缺失工作並保留原 receipts。
