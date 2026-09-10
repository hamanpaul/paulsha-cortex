# workflow-terminal-jsonl-framing Specification

## Purpose
以保留 JSONL 實體記錄分界與既有 terminal carrier 的方式，確保 Manager
解析 terminal log 時保留 CRLF 與 Unicode JSON string data，並維持既有
fail-closed 的信任與驗證邊界。
## Requirements
### Requirement: R1 實體分界

系統 MUST 遵守下列契約：`_extract_terminal_json` 的 JSONL records 只以 literal LF 分界；CRLF 相容，CR 交由既有 JSON whitespace／fence 規則處理。末筆沒有換行、空行及尾端換行仍相容。不把裸 CR 當另一種 record delimiter；兩筆 JSON 只以裸 CR 相接不能被 reader 正規化成兩筆。單筆 JSON 外的合法 CR whitespace 不在此禁止。

#### Scenario: LF 與 CRLF 記錄及裸 CR 負例

- **WHEN** 輸入為 LF／CRLF、空行、無末尾換行，或僅以裸 CR 相接的兩筆 JSON
- **THEN** 前述合法實體 LF records 可依原順序解析；裸 CR 相接不被 reader 正規化為兩筆，單筆 JSON 外合法 CR whitespace 仍相容

### Requirement: R2 Unicode 資料保真

系統 MUST 遵守下列契約：JSON string 內合法的 raw 或 escaped U+0085 NEL、U+2028 LS、U+2029 PS 都是資料，解析後逐值保留；不得刪除、Unicode strip、替換成空白或以模型輸出 ASCII 作為產品修復。一般非 ASCII 字元與組合字也不能改寫。

#### Scenario: 三種合法 Unicode 資料

- **WHEN** JSON string 含 raw 或 escaped U+0085／U+2028／U+2029，另含一般非 ASCII／組合字
- **THEN** decoded payload 的每個值逐一深度相等，不 strip／刪字／替換

### Requirement: R3 雙層載體

系統 MUST 遵守下列契約：Claude `type=result` 的 `result` JSON string 與同 record 的 `structured_output` object 同時存在時，任一欄位值含上述字元不得破壞 record。仍沿現有 `result` carrier 解析；不得因此新增對任意 `structured_output` 的採信，也不要求兩欄永遠相等來取代既有契約。

#### Scenario: 雙層 serializer 反例

- **WHEN** inner result 已 escape，但同一 type=result record 的 outer structured_output 含 raw separator
- **THEN** record 完整保留，仍經 result carrier 解析；只有任意 structured_output 的 record 不新增採信

### Requirement: R4 選擇／信任邊界不擴張

系統 MUST 遵守下列契約：保留反向掃描、既有 recognized carrier、單層 wrapper 白名單與末尾 fence 規則。尾端非 terminal／既有不受信任的 tool `data.content` 不得覆蓋較早合法 terminal；只有此類 spoof 或未知 wrapper 時仍拒收。不新增遞迴 JSON 搜尋、任意 event/structured-output fallback。現有 generic `text` 等 carrier 並非完整 event-type allowlist，不能宣稱所有 tool spoof 已封鎖，見 design D4 的列管殘餘。

#### Scenario: 尾端 nonterminal 與已拒收 tool carrier

- **WHEN** 真正 terminal 後接 nonterminal／tool.execution_complete.data.content spoof，或只有 spoof／未知多層 wrapper
- **THEN** 前者仍選真正 terminal，後者拒收，不新增 recursive fallback；不宣稱 generic text 的既存接受已修

### Requirement: R5 授權不變

系統 MUST 遵守下列契約：`_is_workflow_terminal_payload`、per-phase schema、status enum、report path/size、Candidate／claim-era／identity／gate-ledger 綁定全部不改。能抽出 JSON 只代表 parser 成功，不能把 exit 0、`subtype=success` 或模型 `verified` 自報提升為 Manager 採信。

#### Scenario: 解析成功不是授權

- **WHEN** payload 被抽取但 phase schema、report path/size 或 Candidate／era／identity 綁定不符
- **THEN** 既有 gate 保持 fail-closed，不能把 parser 成功或模型 verified 自報轉為採信

### Requirement: R6 失敗相容

系統 MUST 遵守下列契約：缺 log path、I/O／UTF-8 解碼錯誤、找不到可用 terminal 的既有 fail-closed 行為與錯誤訊息保留；不得用重試模型或改 failure classification 掩蓋 framing failure。不改尚未完成 append／truncated record 的既有選擇政策。

#### Scenario: 讀取與缺證據錯誤

- **WHEN** 缺 log path／檔案、I/O、invalid UTF-8、無合法 terminal 或末筆 truncated
- **THEN** 既有錯誤映射與 terminal 選擇政策不被本 framing 修正放寬

### Requirement: R7 純讀與不可變重播

系統 MUST 遵守下列契約：同一組原始 bytes 以舊 parser 重現失敗、候選 framing 回傳與原 terminal payload 完全相等；讀前／讀後 SHA256 相同。測試使用合成 fixture、記憶體或 `tmp_path`；禁止修改真實 log、evidence、registry、frozen planning 或 live run。私有事故檔只能作 owner 授權的額外唯讀重播，不是 CI 必備依賴。

#### Scenario: 原始 bytes 不可變重播

- **WHEN** 合成或 owner 額外授權私有 log 以舊 parser 及候選 framing 純讀重播
- **THEN** 舊失敗可重現且候選逐值等於原 payload，before/after SHA256 相同，不改 logs/evidence/registry

### Requirement: R8 反例完整

系統 MUST 遵守下列契約：raw/escaped 三 code points、雙層序列化、LF/CRLF、missing final newline、尾端非 terminal、tool spoof 既有拒收路徑、未知／多層 wrapper、fence 相容、錯誤讀取均有 deterministic regression；RED 必須是被收集且實際失敗，不以 import／collection failure 代替。

#### Scenario: 收集成功的 deterministic regression

- **WHEN** 執行 raw/escaped、雙層 serializer、framing、carrier/fence、invalid reader 的正負矩陣
- **THEN** 先以被收集且實際 assertion/ValueError RED 證缺陷，再以候選 GREEN 保護全部對照，不拿 collection failure 當 RED

### Requirement: R9 使用說明

系統 MUST 遵守下列契約：文件說明實體 JSONL record 與 JSON string 內容的差別，以及 parser 成功／schema 通過／gate 通過／runtime 已載入四種不同證據。臨時以 ASCII 名稱描述 separator 的新卡恢復不等於本產品修復，不能改舊 evidence 或只 escape 內層 result 後保證成功。

#### Scenario: 文件與實際 CLI

- **WHEN** 候選文件說明 JSONL record 與 Unicode string data 並執行既有 CLI help
- **THEN** 讀者能區分 parser/schema/gate/runtime loaded，知道只 escape inner 不足；不虛造新旗標或 live request

### Requirement: R10 完整交付

系統 MUST 遵守下列契約：未來 Cortex 只在 `paulsha_cortex/coordinator/manager.py` 的 `_extract_terminal_json` 內修 framing／其必要的無 newline 轉換讀取。搭配 tests、documentation、自有 OpenSpec 與 changelog，通過 focused/full tests、PR-context policy、CI、獨立 review 和 exact-head delivery；實際 Manager 載入／live 驗證另由 root 在共享 owner 同意的安全窗口處理，不於本草稿操作。

#### Scenario: 單模組範圍與完整交付

- **WHEN** Cortex 實作本 child 並推進候選驗證及正式下游交付
- **THEN** production 只改 manager.py 的指定函式；tests/docs/changelog/own OpenSpec/full/policy/CI/review/exact-head 與安全 loaded gate 全保留，未完成不能假勾或當已部署
