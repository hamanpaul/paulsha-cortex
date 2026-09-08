---
status: accepted
work_item: yaml-inline-list-quotes
---

# YAML inline list 引號設計

## Decisions

1. 在現有 scalar parser 內新增小型 quote-aware flow-sequence 掃描；只有引號外的逗號是分隔符。追蹤單／雙引號與既有 quoted-scalar 支援的跳脫，保留元素原文交由既有 scalar decoder。
2. 不引入完整 YAML 相依或重寫 block parser；此修正涵蓋 repo 自有 emitter 產出的 quoting 契約。未支援的巢狀 flow/mapping 不列為新增功能，不能藉此放寬 verifier。
3. 畸形輸入在 tokenizer 層拒絕並維持可診斷的 list 錯誤，不容許空中間元素偷偷改 argv。空引號字串與空 list 為合法值，尾逗號是明確相容政策。
4. RED 先驗含逗號元素在舊實作被 split 破壞，再以正負例、兩種 emitter、真 frontmatter consumer 驗 GREEN。完整測試保留所有既有消費端。
5. 工作在 Cortex 管理的隔離 clone／branch 執行；不得改 operator 工作樹、模型設定或服務。依當時合格可用候選派工，本工作不永久指定 model／agent／effort。
