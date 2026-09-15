---
status: accepted
work_item: workflow-terminal-jsonl-framing
---

# Workflow terminal JSONL framing 自有 OpenSpec proposal

Owner 為 [#860](https://github.com/hamanpaul/paulsha-cortex/issues/860)，work_id 為 `workflow-terminal-jsonl-framing`。本 upfront baseline 承接 root 已接受的 [spec](../../../docs/superpowers/specs/workflow-terminal-jsonl-framing-spec.md)／[design](../../../docs/superpowers/specs/workflow-terminal-jsonl-framing-design.md) 等價內容；accepted 只指 planning 內容，不表示產品工作、正式 freeze／dispatch 或 qualification 完成。所有 [pre-archive tasks](tasks.md) 都未完成，產品由正式 Cortex 工作流程執行。此 baseline 新增本身仍交 root 做最終 integration review。

## Why

事故 [#822](https://github.com/hamanpaul/paulsha-cortex/issues/822) 的合法 JSONL record 在 JSON string 中含 raw NEL，Manager 的 `str.splitlines()` 把資料錯切為 records，報 no JSON evidence。本件修 representation fidelity，不改 schema、provider quota 或授權。Refine 映射為 R13 primary／R06 secondary；這不是 policy 規則編號，也不是宣告 #822 或母 refine 已完成。

## What Changes

只在 `paulsha_cortex/coordinator/manager.py::_extract_terminal_json` 使用保留 newline 的 UTF-8 reader 與 literal LF record 分界；保留 Unicode string data、既有 carrier／wrapper／fence、terminal schema 和原 fail-closed 行為。搭配合成 regression、文件／CLI help、own OpenSpec、changelog 與完整候選交付。不重寫其他 parser，不建立 state writer，不變更 live evidence。

## Capabilities

### New Capabilities

- `workflow-terminal-jsonl-framing`：terminal JSONL 實體 record 分界與 Unicode data 保真。

### Modified Capabilities

不隱性修改既有 terminal 採信或 workflow authority 要求；carrier-authentication 與 streaming 資源等既存殘餘在下列 Requirements 與自有 design 列管。

## Impact

唯一 production module／function 是 `coordinator/manager.py::_extract_terminal_json`。Tests 集中新增 `tests/test_workflow_terminal_jsonl_framing.py` 並重用既有 production-wiring／terminal-contract harness；documentation 依真接口影響更新 lifecycle 說明並實跑 CLI help。完整 fix-standard 的 R-09／R-16／R-19、full suite、policy、CI、獨立 exact-Candidate review 與下游交付全部保留。其他 owner 的 jobs、registry、operator baseline、live service 與原事故 log 均不在此次文件操作範圍。詳見 [design](design.md)、[tasks](tasks.md) 及 [delta](specs/workflow-terminal-jsonl-framing/spec.md)。

## Requirements

1. **R1 實體分界**：`_extract_terminal_json` 的 JSONL records 只以 literal LF 分界；CRLF 相容，CR 交由既有 JSON whitespace／fence 規則處理。末筆沒有換行、空行及尾端換行仍相容。不把裸 CR 當另一種 record delimiter；兩筆 JSON 只以裸 CR 相接不能被 reader 正規化成兩筆。單筆 JSON 外的合法 CR whitespace 不在此禁止。
2. **R2 Unicode 資料保真**：JSON string 內合法的 raw 或 escaped U+0085 NEL、U+2028 LS、U+2029 PS 都是資料，解析後逐值保留；不得刪除、Unicode strip、替換成空白或以模型輸出 ASCII 作為產品修復。一般非 ASCII 字元與組合字也不能改寫。
3. **R3 雙層載體**：Claude `type=result` 的 `result` JSON string 與同 record 的 `structured_output` object 同時存在時，任一欄位值含上述字元不得破壞 record。仍沿現有 `result` carrier 解析；不得因此新增對任意 `structured_output` 的採信，也不要求兩欄永遠相等來取代既有契約。
4. **R4 選擇／信任邊界不擴張**：保留反向掃描、既有 recognized carrier、單層 wrapper 白名單與末尾 fence 規則。尾端非 terminal／既有不受信任的 tool `data.content` 不得覆蓋較早合法 terminal；只有此類 spoof 或未知 wrapper 時仍拒收。不新增遞迴 JSON 搜尋、任意 event/structured-output fallback。現有 generic `text` 等 carrier 並非完整 event-type allowlist，不能宣稱所有 tool spoof 已封鎖，見 design D4 的列管殘餘。
5. **R5 授權不變**：`_is_workflow_terminal_payload`、per-phase schema、status enum、report path/size、Candidate／claim-era／identity／gate-ledger 綁定全部不改。能抽出 JSON 只代表 parser 成功，不能把 exit 0、`subtype=success` 或模型 `verified` 自報提升為 Manager 採信。
6. **R6 失敗相容**：缺 log path、I/O／UTF-8 解碼錯誤、找不到可用 terminal 的既有 fail-closed 行為與錯誤訊息保留；不得用重試模型或改 failure classification 掩蓋 framing failure。不改尚未完成 append／truncated record 的既有選擇政策。
7. **R7 純讀與不可變重播**：同一組原始 bytes 以舊 parser 重現失敗、候選 framing 回傳與原 terminal payload 完全相等；讀前／讀後 SHA256 相同。測試使用合成 fixture、記憶體或 `tmp_path`；禁止修改真實 log、evidence、registry、frozen planning 或 live run。私有事故檔只能作 owner 授權的額外唯讀重播，不是 CI 必備依賴。
8. **R8 反例完整**：raw/escaped 三 code points、雙層序列化、LF/CRLF、missing final newline、尾端非 terminal、tool spoof 既有拒收路徑、未知／多層 wrapper、fence 相容、錯誤讀取均有 deterministic regression；RED 必須是被收集且實際失敗，不以 import／collection failure 代替。
9. **R9 使用說明**：文件說明實體 JSONL record 與 JSON string 內容的差別，以及 parser 成功／schema 通過／gate 通過／runtime 已載入四種不同證據。臨時以 ASCII 名稱描述 separator 的新卡恢復不等於本產品修復，不能改舊 evidence 或只 escape 內層 result 後保證成功。
10. **R10 完整交付**：未來 Cortex 只在 `paulsha_cortex/coordinator/manager.py` 的 `_extract_terminal_json` 內修 framing／其必要的無 newline 轉換讀取。搭配 tests、documentation、自有 OpenSpec 與 changelog，通過 focused/full tests、PR-context policy、CI、獨立 review 和 exact-head delivery；實際 Manager 載入／live 驗證另由 root 在共享 owner 同意的安全窗口處理，不於本草稿操作。

## Scope

唯一 production module／function 是 `coordinator/manager.py::_extract_terminal_json`。不改 launcher、provider serializer、terminal_contract、registry、service、sizing、其他 parser 的 `splitlines()` 或整體 log streaming 架構。若實作需擴張任何 production 邊界，先回 root 重裁／重算，不以單模組數值掩蓋。

## Evidence

基底 `b1b44bc476dc49481d8467c9595e7c4db3f1a87d` 與事故 runtime `79ba644780bf1c697c722ac24a297e7d02416100` 的 `manager.py:4355–4405` 同型：`:4362` 呼叫 `content.splitlines()`，`:4367` 對被切開的片段做 `json.loads`，最後報 `workflow terminal log has no JSON evidence`。

#822 job `wf-855aa5366b-verification-94` 原 log SHA256 `ba8efd04a7f61c265a684699fae353bff950334edf5c6c339548eb433c26f378`，246816 bytes；203 個實體 LF records／206 個 Python `splitlines()` 片段。整檔 3 個 NEL，最後一筆有 2 個。記憶體 LF framing 回傳逐值等於原 `structured_output`，只證明 parser，不是完整 verification acceptance。精確位置與來源見 [作者報告](../../../reports/review/refine-terminal-framing-20260908.md)。
