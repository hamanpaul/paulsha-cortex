---
status: accepted
work_item: dispatch-decision-contract
---

# 派工結果的非 Job 決策契約

## Requirements

對應 [Cortex #830](https://github.com/hamanpaul/paulsha-cortex/issues/830)，屬 refine #829 的 R14 接續契約修正。
accepted 表示需求與測試邊界已定案，不表示已實作、可繞過 sizing，或已完成交付。

1. **R1 結果分類**：所有正式派工消費端須辨識真正 Job、合法非 Job 決策、確定性 phase transition 與 None。不能以「非 None」推論為 Job；未知／malformed 結果須具體拒絕，不得降格為成功。
2. **R2 非 Job 真實性**：needs-decomposition、runtime preflight refusal、plan-output missing／plan-review 待處理等合法決策不產生假 job_id、dispatched 或 model session。回應保留原 reason、run ID、最新持久化 phase/facets。
3. **R3 入口一致性**：work start/intake、workflow-action 同步續推、explicit resume、periodic continuation、terminal 後續 dispatch／provider retry 的同型消費端使用同一契約；不能只修最早一個 KeyError。
4. **R4 狀態保全**：合法拆分／等待不得因 adapter 例外變成一般 needs_human/resume-workflow-failed。確定性 transition 若已落盤、即使沒有 Job，回應仍反映新 phase；None 且沒有 transition 不假稱推進。
5. **R5 冪等性**：start 已建立 run 後的無 Job 結果，重送不得新增重複 claim/run、改寫已採信 evidence 或重複派模型；真 Job 保留 exact job_id 與原有綁定。
6. **R6 Forced retry**：retry-build/retry-card 要求 replacement Job 時，合法非 Job／None仍是不滿足重派要求；保留原有 fail-closed 補償和具體診斷，不能回假 redispatched。
7. **R7 不放寬治理**：不改 reviewer independence、權限、quota、sizing 或 recovery CAS。不可手造 evidence、修改 live registry 或新增隱含重試來掩蓋契約錯誤。
8. **R8 可驗證交付**：真 producer→consumer RED/GREEN、全套、policy、CLI smoke 與 bounded Cortex canary 各留獨立證據；request 完成、job 啟動與 workflow terminal 不互相替代。

## Scope

- Production：`paulsha_cortex/coordinator/manager.py` 與 `manager_daemon.py` 的派工結果產生／消費接線；如需共用內部結果型別，可在同一 coordinator 邊界新增小型 helper，但不得改 durable Job/WorkflowRun schema。
- Tests：既有 producer、daemon request、resume/periodic、provider retry、forced retry fixture；新增集中 contract matrix。
- Documentation：`docs/unified-work-lifecycle.md`、必要 CLI 操作說明與 changelog。
- 非目標：#831 stability 量表、#223 Red 自動拆分、quota 預測、#822 parser、語意 recovery 擴權。

## Evidence

基底 `79ba644780bf1c697c722ac24a297e7d02416100`：`manager.py:9494-9537` 合法回傳無 job_id 的 needs-decomposition；`manager_daemon.py:809-810` 直接取 job_id。
`tests/test_dispatch_needs_decomposition_223.py:94-115` 已驗 producer 零 Job；`tests/test_manager_daemon_intake_dispatch.py:50` 只涵蓋真正 Job。
另須實作前重新定位 `resume_workflow_run` 及後續 dispatch 的所有消費點，避免行號漂移被當成漏查理由。
