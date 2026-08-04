---
status: accepted
work_item: feat-slice-executor-model
---

# feat-slice-executor-model Design

## Decisions

### D1 採 per-slice frontmatter 宣告（#294 期望選項 1），不採多 specs-dir root（選項 2）；選項 3 併入本票

executor/model 是 slice 的工作屬性，與 `plan`/`target_branch`/`verification` 同層，該住在 spec 契約內，而非 invocation 旗標。frontmatter 新增 optional `executor`/`model_id`，fanout 層 `--executor`/`--model` 降為預設值，spec 有宣告則覆寫。

理由：選項 2（`--specs-dir` 多 root）只解掉依賴圖分裂，沒解掉「slice 層宣告 identity」的表達力缺口，且會把「一個 dir＝一個 batch」的既有語意複雜化——`--specs-dir` 出現在 fanout/tick/complete/status/dispatch 多處，全部要處理多 root 合併與重複 slice_id 仲裁。選項 1 落地後單一 dir 天然容納異質 executor，依賴圖保持完整，選項 2 的主要動機即消失。選項 3（depends_on 診斷）issue 明言「無論 1/2 是否採用，建議獨立處理」——本票一併做（D4），因為即使選項 1 落地，歷史批次與漸進遷移仍會存在跨 dir 依賴，typo fail-silent 仍需獨立解。

### D2 宣告成對且 fail-closed；語法驗證在 parse 層、registry 驗證在 dispatch 層（比照 #205 D4）

`executor` 與 `model_id` 必須成對宣告：registry 的身分 key 是 `(executor, model_id)` 二元組（`model_identities.py:167-177`），單獨宣告其一沒有可驗證的身分——`executor` 單獨配 fanout 預設 model 會拼出 registry 可能不存在的混血身分，fail-closed 立場直接拒為 `invalid-frontmatter`。語法驗證（非空字串、成對）放 `_normalize_frontmatter`；registry 存在性驗證放 `dispatch_ready` per-slice 流程。

理由：`parse_spec_frontmatter`／`scan_specs` 是純函式且被 status provider 高頻呼叫（`manager_daemon.py:313`），在 parse 層引入 registry 載入會把 I/O 與部署狀態摻進確定性解析路徑；dispatch 層本來就是身分落地點（launcher 構建處），驗證放這裡與 #205 的「override 於 dispatch 時 fail closed 判定」同一模式（`porcelain/run.py:61-64` 注解明言此分層）。unknown identity 的錯誤訊息列出可用清單，訊息語意比照 `manager.py:5278-5283`——operator 打錯字要能立刻看出來，靜默退回預設是最糟行為（#205 D4 原文理由）。

### D3 per-slice launcher 經 `launcher_factory` 注入，沿用 workflow 路徑既有 lambda 模式

`dispatch_ready` 新增 optional `identity_registry`／`launcher_factory` 參數；meta 無宣告 → 沿用傳入的預設 `launcher`（行為位元不變）；有宣告 → registry 驗證通過後以 `launcher_factory(identity)` 建 per-slice launcher。daemon 側 factory 即 `lambda identity: _resolve_launcher(identity.executor, launcher, allow_unsafe=<該路徑現值>, model=identity.model_id)`——與 `manager_daemon.py:417-422`、`:524-529`、`:804-809` 三處 workflow 既有 lambda 完全同形，注入 fake launcher 的測試自然相容（`_resolve_launcher` 注入優先）。

理由：`autonomy.py` 不 import `SubprocessLauncher` 具體類別（只依賴 `AgentLauncher` protocol），launcher 構建知識留在 coordinator/cli 與 daemon 層，維持既有分層。builder persona 的 `as_commit_required` 轉換對 per-slice launcher 於解析點套用，與預設路徑（`autonomy.py:439-442`）同語意；`allow_unsafe` 沿 request 現值傳遞、不新增旁路——`_refuse_unsafe_fanout` 的單一 canary 限制在 ready 集層面先行生效，覆寫不改變 ready 集大小。

### D4 depends_on 診斷三分類：`deps-unsatisfied`／`deps-external`／`deps-unknown`；可觀測診斷，不新增 hard gate

新增 autonomy 層 pure helper（handoff probe 可注入）分類批外 dep：handoff 目錄存在 `<id>.json` manifest → `deps-external:<id>`（曾在某處走過 lifecycle，多半是合法跨 dir 依賴）；無任何 trace → `deps-unknown:<id>`（高度可疑，typo 的 signature）。`_held_reasons`（`manager_daemon.py:232-243`）帶入本批 slice_id 集合與 probe，取代對批外 dep 的籠統 `deps-unsatisfied`；`cortex ready` 對 `deps-unknown` 印 stderr 診斷行。

理由：這正是 #294 期望選項 3 的建議形狀（「有 handoff record 時標 external、無則標 unknown」）。不把 `deps-unknown` 升級為 hard refuse：跨 dir「先宣告、後補 handoff」是合法時序（上游批次尚未跑完時，下游批次先掃描是正常操作），unknown 與 external 的差別只有「當下有無 trace」，hard-fail 會把合法時序一起擋掉。in-batch 未完成維持 `deps-unsatisfied:<id>` 原字串，既有消費者（status 面板、測試斷言）不受影響；批外情境的新字串是對「原本就無法區分的錯誤訊號」的細化，屬加法。`detect_cycles` 的「外部邊不算環」判定正確、不動（`autonomy.py:333-334`），僅 docstring 明確化與診斷路徑補上。

### D5 fanout/tick request 層 builder identity 驗證：只驗「明確 (executor, model) 對」，model None 維持現行

`build_request_executor` 的 fanout/tick/dispatch 分支與 `build_periodic_tick_runner`，在 executor 與 model（含 default 帶入後）皆為明確字串時，派工前經 `load_model_identities()` 查 `(executor, model)`；查無 → request 回 error／periodic tick 記 tick error 且本輪不派工，訊息帶可用清單。model 為 None 時不觸 registry——executor 白名單既有兩層把關（CLI choices＋`SubprocessLauncher` 建構子）維持不變。

理由：#276 同場發現的缺口是「workflow 路徑吃 registry、slice fanout 路徑不吃」的不對稱；收口方式是讓「明確宣告的身分」全部過同一 registry，而不是強迫所有呼叫都先註冊身分——後者會把「不帶 model 的日常 fanout」也變成 breaking change。packaged registry 目前只含 agy planning identity（`coordinator/data/model-identities.yaml`），builder 身分靠 instance 的 `PSC_PROJECT_CONFIG_ROOT/model-identities.yaml` 擴充（`load_model_identities` 允許 custom additions 合併，`model_identities.py:253-271`）；錯誤訊息因此必須列出可用清單並讓 operator 知道去哪裡補註冊。

### D6 `EMITTED_FRONTMATTER_FIELDS` 同步擴充，但 deck compile 不輸出新欄

`deck/schema.py:12-20` 的 tuple 加入 `executor`/`model_id`——`tests/test_deck_contract_alignment.py` 以「tuple ＝ parse meta keys − path」雙向等式鎖住兩邊，parse 層加欄位後 tuple 不動會直接紅。但 `_render_frontmatter`（`deck/compile.py:287`）不輸出這兩欄：deck 產物是 task 結構的編譯結果，identity 是 operator／instance 的部署決策，兩者不同生命週期；emit 後由 operator 或上游工具視需要補宣告。

理由：tuple 的語意是「runtime 契約接受的欄位全集」（其上方注解即指向 `parse_spec_frontmatter` 為真相源），不是「deck 必然輸出的欄位」；讓 deck 開始輸出 identity 欄位反而把部署決策焊死進編譯產物。

## 風險與緩解

- **既有流程以 `--model` 傳未註冊 model 會開始被拒（R5 的預期行為變更）**：這正是 #276 同場發現的收口目的——現況下 typo 的 model 會原樣進 argv、由 executor CLI 在 session 內才失敗（燒 session）。緩解：錯誤訊息列出可用 identity 清單並指向 instance `model-identities.yaml` 的擴充路徑（README「model-identities」段，`README.md:458` 附近）；changelog fragment 明確標注此行為變更。
- **parse 層行為改變破壞既有 specs**：新欄位是 optional 加法，未宣告時 meta 只是多兩個 None key；風險集中在 `EMITTED_FRONTMATTER_FIELDS` 等式與依賴 meta shape 的測試，以 `tests/test_deck_contract_alignment.py`、`tests/test_persona_phase4_fanout_autonomy.py` 全綠為驗收鎖。
- **per-slice launcher 與注入 fake 的測試相容性**：factory 沿用 `_resolve_launcher(identity.executor, injected, ...)` 注入優先模式（D3），單測注入的 fake launcher 在覆寫路徑同樣被回傳，不需要真的起 subprocess。
- **held reasons 新字串影響下游消費者**：`deps-unsatisfied` 對 in-batch dep 原樣保留；只有「原本就無法區分」的批外情境改出 `deps-external`/`deps-unknown`，status 消費端（Monitor read model）對 reasons 陣列為透傳展示，不做字面 switch；以既有 daemon status 測試全綠驗證。
- **registry 載入失敗把 fanout 整批拖垮**：`load_model_identities()` 對壞損檔案 raise `ValueError`（fail-closed 是 registry 既定語意）；本票將載入放在「有宣告才觸發」的路徑（per-slice lazy／request 層明確對驗證），未宣告的批次不觸 registry，爆炸半徑限定在真的要用 identity 驗證的呼叫。
- **`deps-external` 誤導 operator 以為依賴健康**：external 只代表「有 trace」，不代表「會被滿足」（stale handoff 仍不滿足）；診斷字串是分類不是背書，`ready_units` 的滿足性判定完全不變，文件與 CLI help 措辭明確此界線。
