---
type: fix
scope: yaml
---
**Issue #822：YAML inline list 保留引號元素**

zero-dependency YAML subset parser 現在以 quote-aware tokenizer 解析 flow list，讓含逗號或
`]` 的 `argv` 元素、雙引號跳脫與尾逗號能正確 round-trip；中間空元素與未閉合引號會以
`malformed inline list` 拒絕。同步補上 frontmatter `argv` 的引號限制說明。
