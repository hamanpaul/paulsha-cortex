### Fixed

- **#1210 direct 模式 Claude reviewer 的 Bash allow**：direct runner 的 reviewer settings 只靠 `autoAllowBashIfSandboxed`，dontAsk 會拒絕複合命令，reviewer 沒有 Read／Grep 可替代而以 needs_human 結束。補上與模板 runner（#748）相同的 `permissions.allow: ["Bash"]`；沙箱設定與憑證讀取拒絕不變（#1210）。
