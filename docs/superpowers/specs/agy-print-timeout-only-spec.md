---
status: accepted
work_item: agy-print-timeout-only
authority_state: proposed-unregistered
dispatch_readiness: blocked-dependency
depends_on:
  - agy-probe-construction-containment
dependency_issues:
  - "hamanpaul/paulsha-cortex#851"
---

# AGY print timeout 單獨交付規格

## Requirements

本文件把 [#824](https://github.com/hamanpaul/paulsha-cortex/issues/824) 已定案的
print-mode 等待契約整理為獨立 child。初次需求核對為 2026-09-07、issue
`updatedAt=2026-09-07T10:01:44Z`；root 後續已正式同步 AC5／AC6、fullmatch／
range 與 #851 dependency，本輪唯讀確認最新 `updatedAt=2026-09-07T13:40:04Z`。
`status: accepted` 是需求文件的機械輸入狀態，
不表示這個 child 已註冊、已通過獨立 plan review、已凍結 authority 或已授予派工權。

目標是讓 Cortex 明確傳入可配置、可由 AGY 解析的 print deadline，解除
[#831](https://github.com/hamanpaul/paulsha-cortex/issues/831) 在全庫測試前後碰到
AGY 預設五分鐘上限的 bootstrap 依賴。這不是 #831 的 sizing 算法修正。

唯一 production 修改檔為 `paulsha_cortex/coordinator/launcher.py`。
`gate_ledger.py` 僅使用既有 `_gate_timeout()` 與 `DEFAULT_GATE_TIMEOUT_SECONDS`；
不得修改其內容。若現有 helper 不能滿足下面契約，先回報重新裁決 scope。

此 child 目前不可先派，必須先完成獨立 dependency
`agy-probe-construction-containment`，其唯一 owner 已由 root 建立為
[#851](https://github.com/hamanpaul/paulsha-cortex/issues/851)，其 registration 已由
root 納入本 PR 的 repository intake；#824 本 child 則刻意不登錄、不解除 block。
R1 對抗審查已用 fake 反證：目前
`model_identities.probe_agy_capability()` 在 smoke try 外建立 argv，R4 的
probe 也 resolve timeout，因此非法設定的 `ValueError` 會穿透
`planning_runtime` 的 roster probe loop，阻止原本 ready 的非 AGY primary
建立 runtime。這是必修缺陷，不列為可直接接受的殘餘風險。
dependency 要把 argv construction 納入既有失敗回傳邊界；本 child 保持
launcher-only，保留全部 env／range 驗證及 probe timeout，不以忽略 probe 值繞過。
root 確認 containment 已完成驗證且預定 base／實際 runtime 已含保護後，才可
解除本 child 的 dispatch block；機械 completeness／6 Yellow 不能取代此條件。

### Current integration gate

root 已將 #824 從母 #823 mapping 移除，但故意不登錄 `agy-print-timeout-only`；
owner issue #824 仍 OPEN，#851 的產品前置尚未完成。runtime `79ba6447` 不把
`dispatch_readiness`／`dependency_issues` 當 workflow guard，`depends_on`
只作用於舊 slice lane。本 todo 保留 root 的未決問題 marker，當前真
completeness=false、plan 被判 `blocking-decision`，舊反向分數為4 Yellow，
不是原 author 完整文件的6 Yellow，也不是降低了0／0／7宣告。
surface-only `plan_review_gate` 仍可能ready／envelope bypass；incomplete start
可能進 brainstorm，故 marker 本身不保證zero-spawn。root保持不登錄、不加
`cortex:auto-on-going`、不start #824；待#851產品、預定base及loaded runtime
保護均核對後，才可解除marker、重新接受並正式登錄，不能先派。

### R1：輸出形狀與數值界線

- `resolve_agy_print_timeout(env: Mapping[str, str]) -> str` 輸出 canonical
  正整數秒加 `s`，符合 `re.fullmatch(r"[1-9][0-9]*s", value)`。
- Go duration 為 signed 64-bit nanoseconds；本契約只輸出整秒，最大值為
  `floor((2**63 - 1) / 1_000_000_000) = 9223372036` 秒。`1s` 與
  `9223372036s` 可表示；`0s`、負值與 `9223372037s` 不可輸出。
- 顯式 env、顯式 keyword 與 default 推導結果都必須檢查此界線；超界
  `ValueError`，不得 wrap、truncate、clamp 或把無法解析的值交給 AGY。
- 錯誤訊息只識別設定名稱與錯誤類型，不 dump env，也不回顯任意原始值。

### R2：顯式 AGY override

- 新增 `AGY_PRINT_TIMEOUT_ENV = "PSC_AGY_PRINT_TIMEOUT"`。用 key 是否存在
  判斷顯式設定；存在但空白不是未設定。
- env 值去除頭尾空白後，只接受 ASCII 十進位數字；容許前導零，正規化後
  必須在 `1..9223372036`。例如 `" 0900 "` 輸出 `900s`。
- 空字串、全空白、`abc`、`0`、`-5`、`+900`、`1.5`、`1e3`、`900s`、
  Unicode 數字或超界值皆 `ValueError`。超長字串也只能產生可預測的
  `ValueError`，不依賴 Python 大整數字數限制的錯誤文字。
- 顯式設定優先，不受 gate 值提高或降低：override `900` 與 gate `3600`
  並存仍為 `900s`。此分支不需要解析 gate 值。

### R3：未設定 override 的推導

直接重用既有 helper：

```text
gate_seconds = gate_ledger._gate_timeout(env)
seconds = max(gate_ledger.DEFAULT_GATE_TIMEOUT_SECONDS, gate_seconds) + 600
```

| AGY override | Gate 設定 | 結果 |
|---|---|---|
| 未設定 | 未設定 | `2400s` |
| 未設定 | `900` | `2400s` |
| 未設定 | `3600` | `4200s` |
| 未設定 | `abc`、`0`、`-5`、空白 | helper fallback 後 `2400s` |
| `900` | `3600` 或非法值 | `900s` |
| 未設定 | `9223371436` | `9223372036s` |
| 未設定 | `9223371437` | 推導結果超界，`ValueError` |

非法／非正 gate 值維持 `_gate_timeout()` 的既有 fallback；不得另造 gate
parser 或改成 gate 設定一錯便拒派。合法正 gate 值使最後結果超過 AGY 可表示
範圍時，拒絕的是 AGY duration 推導結果，並未改變 gate parser 的語意。

### R4：全部 AGY argv 形狀

- `build_agy_argv()` 新增 keyword `print_timeout: str | None = None`。
  `None` 時自行從 `os.environ` 呼叫 resolver；直接呼叫者也必須收到 flag。
- 非 `None` keyword 必須是 canonical `Ns` 字串，使用 `fullmatch` 且
  數值符合 R1。`2400`、`abc`、`00s`、`01s`、`2400s\n`、有頭尾空白、
  非字串與超界值皆拒絕；keyword 不做 env 的 strip／前導零正規化。
- 所有 planner、reviewer、builder、unsafe builder、write-forbidden builder、
  write-forbidden planner、commit-required builder 形狀，恰有一組
  `--print-timeout <value>`。
- 插入點在可選的 `--output-format json` 之後、可選 `--model` 之前。
  prompt 仍是一個 argv 元素，既有 scope directories、permissions 與前綴切片不變。
- `json_envelope=False` 的 capability probe 形狀也帶 timeout，仍不帶
  `--output-format`；probe 本身的 45 秒 process deadline 不變。

### R5：正式 launcher 傳遞與失敗邊界

- `SubprocessLauncher.launch()` 只對 `executor == "agy"` 計算並顯式傳入
  `print_timeout=resolve_agy_print_timeout(os.environ)`。
- 檢查 caller→builder kwargs，也檢查 fake Popen 收到的實際 shell script
  中有正確的單一 `--print-timeout 2400s`，並測 override `900s`。
- 非法 override 或超界結果必須在 Popen 前 `ValueError`，不啟動 executor。
  此要求不擴充成新交易、rollback、registry 或 job lifecycle 設計。

### R6：原有界線與母範圍

- [#823](https://github.com/hamanpaul/paulsha-cortex/issues/823) 的
  `start_new_session`、process-group 測試與 Popen kwargs refactor 全部留在
  `launcher-session-and-timeout` 母範圍；本 child 不完成、關閉或吞併 #823。
- 保留 #820 的 `json_envelope`、JSON envelope 解析；不改 effort、timeout
  outcome taxonomy、gate timeout default、installer、runner default、Manager
  kill/cancel、`LaunchHandle` 或其他 executor 的 argv。
- 不修改 `planning_runtime.py` 的 120 秒或 probe 的 45 秒 timeout，不修改
  `job_runner.py` 的 env allowlist。timeout 已轉為 argv，不需將新 env 傳入 job。
- 不操作服務、安裝、共享 runtime、既有 worktree、active run 或其他會話。

### R7：可驗證交付

- pytest 覆蓋 R1–R6 的 resolver、exact argv、正式 launcher 轉發、其他 executor
  無 flag 的回歸，以及非法值不 spawn；全部 fake，不呼叫模型 CLI。
- containment dependency 完成後，本 candidate 必須用真 resolver 與非法
  `PSC_AGY_PRINT_TIMEOUT` 測兩條路徑：direct launch 仍 ValueError／零 Popen，
  真 capability probe 在 fake discovery 下回 failed AGY／不呼叫 smoke。
  再用 fresh tmp probe cache、真 runtime constructor、ready 非 AGY primary
  證 runtime 可建立；不能只沿用 dependency 的 builder fault injection。
  這些是本 child 的下游整合測試，不要求 containment child 先有 timeout resolver。
- CLI parser 證據必須有可區分的負控制；`--help` exit 0 不構成 flag 值被解析
  的證据。無憑證、無 prompt、stdin EOF 下，以 timeout flag 後的未知 sentinel
  flag 強制停止：非法 duration 先報 duration 錯，合法 duration 才報未知 flag。
- 本 child 的 canonical build branch 由 `manager.workflow_build_branch()` 固定
  產生為 `feature/824-agy-print-timeout-only`；candidate 使用
  `changelog.d/agy-print-timeout-only.md`，同步 `CHANGELOG.md [Unreleased]`。
  fragment 內容只宣告 #824 timeout；舊 `launcher-session-and-timeout` fragment
  責任留給 #823，不新增重複 fragment，也不改 Manager 的 branch 推導。
- 以上 child 契約已由 root 正式更新至 #824（2026-09-07T13:40:04Z）：AC5
  使用本 child fragment／branch，AC6 使用 sentinel parser proof，並明列
  fullmatch／range 與 #851 dependency。root 本 PR 已移除母 mapping 的 #824，
  但刻意不登錄本 child；dependency block 不解除。本 author 僅唯讀核對遠端更新，
  本 planning 分支也不是 build branch。
- 操作文件與 CLI help smoke、focused/full pytest、manifest 的 OpenSpec specs
  gate、pinned preflight、PR-context policy、exact-head review 均需完成。
  不把 parser proof 或 fake Popen 當成已安裝／live workflow 的驗收。

## Sizing 與 authority

依 [#208](https://github.com/hamanpaul/paulsha-cortex/issues/208) 原 rubric，
domain=0：單一 production 模組與 env→duration→argv 資料流；state=0：純解析／
local argv，無 schema、持久化相容層、concurrency、atomicity 或 migration。
既有 gate helper 已被 launcher import，使用其不變輸出不構成第二個修改模組。

原 author 的完整 accepted 三件套在舊runtime反向stability算法得2，總分
`0+0+2+2+2=6` Yellow；這是歷史與僅在記憶體移除marker的控制組結果。
當前磁碟保留marker，stability=0、總分`0+0+2+0+2=4` Yellow且不完整，
不可據較低分數派工。domain/state/invariant仍0／0／7，不改#831算法或門檻。
須先完成前置、正式登錄及Yellow真gate／authority流程，才可freeze／dispatch。
