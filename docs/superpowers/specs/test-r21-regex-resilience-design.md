---
status: accepted
work_item: test-r21-regex-resilience
---

# test-r21-regex-resilience Design

## Decisions

### D1 BASH_FENCE_RE 新 pattern

```python
BASH_FENCE_RE = re.compile(r"```bash[ \t]*\r?\n(.*?)```", re.DOTALL)
```

- `[ \t]*`：fence marker 後可有空白/tab。
- `\r?\n`：LF 或 CRLF。
- `re.DOTALL`：多行內容完整捕獲。

### D2 PERSONAL_ABSOLUTE_PATH_RE 新增 Windows

在既有 pattern 旁新增：
```python
|[A-Za-z]:\\Users\\
```

限定 `\\Users\\` 子路徑以避免誤匹配其他 `C:\` 出現處。

### 風險與 mitigation

- 既有測試可能因 regex 改動而匹配數量變化 → 全綠確認無 regression。
- Windows path pattern 需避免誤匹配 → 限定 `\\Users\\` 子路徑。