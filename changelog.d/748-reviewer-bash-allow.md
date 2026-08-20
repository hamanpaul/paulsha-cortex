# 748-reviewer-bash-allow

- **`#748` 三分模式的 reviewer settings 補 `allow: ["Bash"]`——#746 關內層 sandbox
  後 `autoAllowBashIfSandboxed` 的放行跟著消失，`dontAsk` 下 Bash 只剩內建安全命令
  白名單（實機：`python3 --version` 過、`pytest` 不過，12 × permission_denied、
  零 gate 可跑）。** deny 規則優先於 allow，憑證／HOME 讀取拒絕逐字不變；Bash 的
  邊界照 #746 裁決由外層 unit＋egress 白名單＋採信端完整性檢查承擔。direct 模式
  逐字不變。
