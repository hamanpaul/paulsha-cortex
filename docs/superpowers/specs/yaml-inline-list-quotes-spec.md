---
status: accepted
work_item: yaml-inline-list-quotes
---

# YAML inline list 引號規格

## Requirements

- 修正 `_yaml._parse_scalar` 的 flow list 邊界辨識，讓字串中的逗號、右中括號與跳脫引號完整往返；保留既有純 scalar 轉型與空 list。
- 接受尾逗號；中間空元素、未閉合引號等畸形 list 必須拋 `YAMLError`，訊息含 `malformed inline list`。
- `deck.compile._format_inline_list` 與 `_format_scalar` 兩種 emitter 的 list 皆須讀回相同元素，不能只驗手寫字串。
- 合法 spec frontmatter 經 `autonomy.parse_spec_frontmatter` 後，tests 與 full_suite argv 必須逐項相等；verification 不得因逗號被拆而誤判。
- 範圍只改 flow sequence tokenizer 與相應測試／文件。保留 block parser、零依賴解析路径與 `verification.normalize_argv`；不擴充完整 YAML、inline mapping 或巢狀 flow list。
- 獨立 review、聚焦與完整測試、policy 及正式 Cortex completion 證據均為交付條件。Issue #822 僅在實作交付時關閉，規劃完成不關票。

## Acceptance

依 canonical `docs/superpowers/workstreams/yaml-inline-list-quotes/todo.md` 的單元、兩 emitter round-trip 與 frontmatter consumer 場景逐一取證。測試資料與 repo root 使用 tmp_path；不操作共享 live registry。
