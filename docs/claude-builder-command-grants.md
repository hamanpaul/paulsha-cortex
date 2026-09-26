# Claude builder 的最小命令授權

## 適用範圍

一般 Claude headless builder 的 `acceptEdits` 不會自動批准 Bash 測試與 Git 提交。
launcher 會把 builder persona 的 `effective_tools` 投影到每個 job 自己的
`--settings` overlay，不修改 operator 的全域 Claude 設定。支援的宣告包括 `edit`
（Claude `Edit`）、`rg`、`python -m unittest`、`git add`、`git commit`、
`file executable bit` 與 `git restore`；未支援的必需工具會在啟動 provider 前拒絕。
`git add`／`git commit` 只在 `commit_policy=required` 時加入授權：

- `rg`、Python unittest 與 Git 寫入能力各自只允許對應的窄命令前綴。
- `PSC_GATE_CMD_*` 經 `gate_ledger.load_gate_specs()` 解析後，符合 Python
  `-m pytest`／`-m unittest` 或 `pytest` 的**精確完整命令**。
- 支援既有 `env -u PSC_REPO_ROOT` 前綴，不接受任意環境變數或 PATH 替換。
- `file executable bit` 只呼叫 `cortex headless-hook set-executable --path`，限於
  builder `write_paths` 內的單一一般檔案，且拒絕 symlink 路徑。
- `git restore` 只呼叫 `cortex headless-hook restore-file --path`，將一個宣告檔的
  index 與 worktree 內容還原到目前 `HEAD`；此操作會覆蓋該檔現有的 staged 與
  unstaged 變更。

worker 必須使用宣告的完整 gate 命令；改參數的 focused test 不會因此自動取得授權。
其他 gate executable 不自動放行。規則不包含 push、reset、clean、任意 Git 子命令、
任意 Python、shell wrapper 或全工具授權。測試參數含 shell／permission pattern
特殊字元時 fail closed，不把引用過的字元誤當成安全的 permission pattern。

`acceptEdits`、PostToolUse hook、linked-worktree Git metadata 的 `--add-dir`
均保留。非 commit-required builder 不取得 Git add／commit 權限；禁止寫入的工作不投影
builder 工具授權。

## 邊界與驗收

這是執行契約的可用性修復，**不是 OS sandbox**；Git hooks 與測試本身仍會執行
candidate 程式碼。既有 runtime isolation／獨立驗收仍不可省略，不能將 allowlist
當作對惡意 candidate 的完整防線。

Edit／Write／MultiEdit 的 PreToolUse hook 會在呼叫工具前拒絕新內容（`new_string`／`content`）超過 32 KiB 的寫入，不執行
該次 Edit。`required_artifacts` 可選擇宣告 Git mode `100644` 或 `100755`；驗收會讀取
Candidate tree 的 mode 並要求完全相符。這些命令授權不是 OS sandbox，也不替原生
Edit 建立 `write_paths` 路徑限制。

驗證包含 launcher 單元測試，以及暫存 repo 的 live headless canary：精確測試、
stage、commit 應成功，未宣告 `python3 -c` 應遭拒。原始失敗工作產物須保留，
正式恢復走帶 `expected-run-id` 的 `cortex work retry-card`，不得直接改 registry。

2026-09-08 live canary：使用修補後 `build_claude_argv` 產生的完整 argv，
Claude `claude-opus-5` 在獨立暫存 Git repo 逐條執行，pytest 回報 `1 passed`，
stage 成功，commit `021d173` 成功；未宣告的 `python3 -c` 實際回報
`This command requires approval`，僅嘗試一次。這是 launcher 真實執行證據，
不是 Hippo feature 的 Manager acceptance 或部署完成證據。
