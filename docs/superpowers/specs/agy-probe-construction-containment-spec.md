---
status: accepted
work_item: agy-probe-construction-containment
authority_state: repository-intake-unfrozen
owner_issue: "hamanpaul/paulsha-cortex#851"
registration_state: repository-intake
dispatch_readiness: awaiting-freeze
---

# AGY probe argv 建構例外 containment 規格

## Requirements

本 child 修正 `probe_agy_capability()` 的既有 smoke 失敗邊界，使 argv 建構
失敗只令 AGY probe 不可用，不中斷 ready 非 AGY primary 的 runtime 建立。
這是 `agy-print-timeout-only` 的前置依賴；timeout child 不可先派。
唯一 production 修改檔為 `paulsha_cortex/coordinator/model_identities.py`。

唯一 owner 已由 root 建立為
[#851](https://github.com/hamanpaul/paulsha-cortex/issues/851)，root 已將其正式登錄
納入本 PR 的 repository intake，尚未 freeze。root 回報獨立 reviewer
`agy_containment_review` 的 R1 內容審查與 fresh-reader 五題均 PASS；
`status: accepted` 與這些 PASS 不代表 Cortex 正式 plan-review gate、
builder 封套、完整 preflight 或 frozen authority 已完成，更不授予派工權。
目前狀態為 `awaiting-freeze`；不得冒用 #824 或 #823 的 source owner。

### Current integration gate

本整合工作樹已從 #852 merge `984fce5b` 快轉；#851 的 registration 狀態為
`repository-intake`、authority 為 `repository-intake-unfrozen`，是本 PR 的
登錄內容，不等於 runtime 已 claim、產品已實作或已安裝。當前文件在 runtime
`79ba6447` 的真 completeness=true、6 Yellow；正式 gate／freeze 仍待 root
完成整合審查、full preflight 後以合法 start 流程處理。
下游 #824 刻意不登錄、不加 auto label、不 start；其 marker／metadata
不能單獨保證零派工，細節見 timeout 四檔的 current integration gate。

### R1：建構納入既有 smoke 失敗邊界

把 `build_agy_argv(...)` 呼叫移入目前包住 `process_runner(argv, **common)`
與 `_process_fields()` 的同一個 `try`，不新增 runtime 外層 catch。
建構拋出 `Exception` 子類時，沿用
`_failed_agy("smoke-failed", probe_exception_diagnostic(exc))` 回傳。
不得改動 discovery 的 models-probe-failed 邊界或捕捉 `BaseException`。

### R2：失敗不升格、診斷不擴張

建構失敗的 `CapabilityProbe` 必須為 `ready=False`，executor/model/domain
維持既有 `_failed_agy` 身分，reason 精確為 `smoke-failed`。
plain `ValueError` 的 diagnostic 為 `ValueError`；即使其 message 帶任意
marker，也不新增 `str(exc)`、env 原值、credentials 或完整 prompt 回顯。
保留既有 diagnostic helper 的有界 detail 投影與失敗分類，不新增 taxonomy。
models discovery 可已被呼叫一次，但 argv 建構失敗後不得呼叫 AGY smoke runner。

### R3：非 AGY primary runtime 可建立

對 isolated roster 內 ready 非 AGY primary 加 canonical AGY planning identity，
使用真正 `build_production_planning_runtime()`、真 AGY probe 函式與 fake runner；
在 cache miss 下令 argv builder 拋 `ValueError`，runtime 必須成功回傳，primary
probe 仍 ready、AGY probe 仍 failed。兩种 roster 順序都要測，避免僅證明已完成
primary probe 的一種先後關係。AGY 不得因此被挑為 ready secondary。

測試必須提供 `probe_cache_path=tmp_path / "probe-cache.json"`，使用新 cache、
暫存 worktree 與 isolated registry，不能讀寫實際 coordinator cache／registry。
不要 mock 掉 `probe_agy_capability()`，否則無法覆蓋這次漏掉的例外邊界。
這是 runtime construction 可用性，不保證一定有合格 heterogeneous secondary，
也不代表後續 primary 真模型呼叫或整個 workflow 已成功。

### R4：直接 launch 的拒絕邊界不變

本 child 不修改 launcher，也不吞掉直接 AGY launch 的 invalid 設定錯誤。
以 `_ARGV_BUILDERS["agy"]` fault injection 在直接 `SubprocessLauncher.launch()`
令 builder 拋 `ValueError`，必須仍向 caller 傳播，且 Popen 呼叫為零。
此 baseline 尚無 timeout resolver；fault injection 不冒充真非法 env 的驗證。
後續 timeout child 必須再以真 resolver／非法 `PSC_AGY_PRINT_TIMEOUT` 驗證
直接 launch 拋錯與 probe containment 兩條路徑，不得省略 env 或 probe timeout。

### R5：既有成功與例外契約保持

成功時保留既有兩段 discovery → smoke 流程、CLI token、prompt、
`read_only=True`、`json_envelope=False` 及所有 argv 參數；common process
timeout 預設仍為 45 秒，runtime 傳入的 `min(timeout_seconds, 45)` 不變。
既有 models discovery 失敗、model-not-listed、smoke runner 例外／非零 rc、
malformed-output、identity-mismatch 判準和 ready 身分核對保持。
建構的 `KeyboardInterrupt`／`SystemExit` 必須繼續傳播，不吞 cancellation。
不重試、不用預設值重建 argv、不忽略 env、不將 failed probe 變成 ready。

### R6：可交付的 source／tests／documentation 範圍

production 只改 `model_identities.py`；測試可改
`tests/test_model_identities.py`、`tests/test_planning_runtime.py`、
`tests/test_coordinator_agy_launcher.py`。同步 `docs/unified-work-lifecycle.md`
的 probe 失敗邊界說明、`CHANGELOG.md [Unreleased]` 及唯一 fragment
`changelog.d/agy-probe-construction-containment.md`。
canonical build branch 由 Manager 從唯一 owner #851 與 work_id 推導，固定為
`feature/851-agy-probe-construction-containment`，不得自行改 slug。
這是正式 builder 的預定名稱；本 PR 已有 registration，不代表該 branch 或 run 已建立。

### R7：驗收與 dependency 解除

先 focused RED，再最小 source change 與 focused green；最後按 repo policy
執行 full tests、OpenSpec、CLI help smoke、PR-aware policy 與 exact-head review。
任何 required gate 不通過不得聲稱完成。測試均為 offline fake，不啟動額外模型。
root 核對此 child candidate／base 及實際使用 runtime 已含 containment，才能
解除 timeout-only 的 dependency block；merge、部署與重試各自需另行授權和證據。
原 author 時期只新增四份規劃文件，沒有產品、issue、registration、commit、push
或服務操作；目前本 PR 的 registration 由 root 負責，本文件整合不代行該操作。

## Exclusions

不修改 `launcher.py`、`planning_runtime.py`、`gate_ledger.py`、probe cache
schema／fingerprint／TTL、registry、Manager、job runner 或 session lifecycle。
#823 的 `start_new_session` 留母範圍；#824 的 duration resolver 與 Go range
全部留 timeout child。若須改第二個 production 模組，先停下回 root 重裁決。

## Sizing declaration

依 [#208](https://github.com/hamanpaul/paulsha-cortex/issues/208) 原 rubric：
domain=0（單 production 模組／同一 probe 資料流），state=0（只改既有 local
exception-to-result 控制流）。本 probe 原本會呼叫 runner，不把它宣稱為純函式；
本次不新增 schema、持久化、cache protocol、concurrency、atomicity 或 migration。
跨 caller 的 regression 測試不等於跨 production 模組改動，但仍完整列入驗收面。
