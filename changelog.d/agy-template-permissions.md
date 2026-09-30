### Fixed

- **#716 模板 unit 內的 agy reviewer／builder 放行 command**：headless agy 只能靠 settings.json 的 `permissions.allow` 放行 command；Trust Root 模板 unit 的 job 帳號沒有 operator 的 allowlist，每個 command 都被自動拒絕，reviewer 以空 response 結束（canary run 36661289336 的 verification 連三次 `card-terminal-schema-retry-exhausted`）。已通過 preflight 的模板 unit 內，agy 的 reviewer 與 builder 形態附加 `--dangerously-skip-permissions`（外層 unit 是唯一邊界，與 codex 的 danger-full-access、claude reviewer 的 Bash allow 同一處置）；零工具的 planner 與 direct 模式不變（#716）。
