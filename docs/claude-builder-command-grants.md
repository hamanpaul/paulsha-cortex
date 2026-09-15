# Claude builder 的最小命令授權

## 適用範圍

一般 Claude headless builder 的 `acceptEdits` 不會自動批准 Bash 測試與 Git 提交。
對 `commit_policy=required` 的工作，launcher 在既有 per-job `--settings`
overlay 中加入下列授權，不修改 operator 的全域 Claude 設定：

- `git add` 與 `git commit` 子命令前綴。
- `PSC_GATE_CMD_*` 經 `gate_ledger.load_gate_specs()` 解析後，符合 Python
  `-m pytest`／`-m unittest` 或 `pytest` 的**精確完整命令**。
- 支援既有 `env -u PSC_REPO_ROOT` 前綴，不接受任意環境變數或 PATH 替換。

worker 必須使用宣告的完整測試命令；改參數的 focused test 不會因此自動取得授權。
其他 gate executable 不自動放行。規則不包含 push、reset、clean、任意 Git
子命令、任意 Python、shell wrapper 或全工具授權。測試參數含 shell／permission
pattern 特殊字元時 fail closed，不把引用過的字元誤當成安全的 permission pattern。

`acceptEdits`、PostToolUse hook、linked-worktree Git metadata 的 `--add-dir`
均保留。非 commit-required builder、planner 與 reviewer 不增加授權。

## 邊界與驗收

這是執行契約的可用性修復，**不是 OS sandbox**；Git hooks 與測試本身仍會執行
candidate 程式碼。既有 runtime isolation／獨立驗收仍不可省略，不能將 allowlist
當作對惡意 candidate 的完整防線。

本修補處理 issue #480 的 commit-required builder 阻塞，不宣稱完成整套 persona
`effective_tools` 翻譯、overlay 上限或 permission-denial 分類機制。

驗證包含 launcher 單元測試，以及暫存 repo 的 live headless canary：精確測試、
stage、commit 應成功，未宣告 `python3 -c` 應遭拒。原始失敗工作產物須保留，
正式恢復走帶 `expected-run-id` 的 `cortex work retry-card`，不得直接改 registry。

2026-09-08 live canary：使用修補後 `build_claude_argv` 產生的完整 argv，
Claude `claude-opus-5` 在獨立暫存 Git repo 逐條執行，pytest 回報 `1 passed`，
stage 成功，commit `021d173` 成功；未宣告的 `python3 -c` 實際回報
`This command requires approval`，僅嘗試一次。這是 launcher 真實執行證據，
不是 Hippo feature 的 Manager acceptance 或部署完成證據。
