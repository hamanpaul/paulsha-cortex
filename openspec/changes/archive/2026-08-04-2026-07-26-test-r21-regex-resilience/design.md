---
status: accepted
work_item: test-r21-regex-resilience
---

# test-r21-regex-resilience Design

## Decisions

### D1 BASH_FENCE_RE 容忍 CRLF + trailing whitespace

改為 `re.compile(r"```bash[ \t]*\r?\n(.*?)```", re.DOTALL)`：
- `[ \t]*` 容忍 fence marker 後的 trailing whitespace。
- `\r?\n` 同時匹配 LF 與 CRLF。
- `re.DOTALL` 確保多行 code block 被完整捕獲。

### D2 PERSONAL_ABSOLUTE_PATH_RE 涵蓋 Windows

在既有 `/home` 和 `/Users` pattern 旁新增 Windows drive-letter path：
- `[A-Za-z]:\\Users\\`（如 `C:\Users\paul`）
- 或更廣泛 `[A-Za-z]:\\` + 已知使用者目錄模式。

### D3 測試案例

新增 fixture/parametrize：
- CRLF bash fence 的 markdown 內容。
- fence marker 後有 trailing whitespace 的 markdown。
- Windows `C:\Users\someone\...` 路徑在 docs 內容中。

### 風險與 mitigation

- regex 改動可能影響既有匹配 → 既有測試全綠確認無 regression。
- Windows path pattern 需避免誤匹配 URL 中的 `C:\` → 限定 `\\Users\\` 子路徑。