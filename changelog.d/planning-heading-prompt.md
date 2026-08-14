---
type: fix
scope: coordinator
---
**Issue #520：必要標題要求改由驗收判準機械產生，消除雙讀法**

`coordinator/planning_runtime.py` 的 integrator prompt 舊句：

```
content must be complete UTF-8 Markdown with frontmatter status: accepted,
the matching work_item, and required headings: Requirements for spec,
Decisions for design, Tasks for plan.
```

原意是「kind=spec → `Requirements`；kind=design → `Decisions`；kind=plan →
`Tasks`」，但字面同樣可讀成「必要標題是 `Requirements for spec`」。實測 planner
採了後者、產出 `## Requirements for spec`，而 `coordinator/planning.py` 的
`_has_required_heading()` 是 casefold 後**完全相等**比對——標題正規化只剝編號前綴
（`^\d+(\.\d+)*[.)]?\s+`），不剝 ` for spec` 尾綴——於是必然
`required-section-missing`。實測 evidence：run `workflow-6b3e215f18c5b68b991c`，
`reasons: ['required-section-missing']`、`markers: []`、frontmatter 完全正確。

修法採 issue #520 建議 4 的結構性作法，而不只是改寫該句文字：判準與 prompt 過去是
**兩份各自維護的真實來源**，已因不同步造成 `#516` 與 `#520` 兩次確定性失敗。現在
`planning.py` 內：

- `_ACCEPTED_HEADINGS`（依「首選在前」排序的顯示形）是唯一真檔；
- `_REQUIRED_HEADINGS` 由它 casefold 派生（判準值一字未改，測試以 frozen
  expectation 鎖住）；
- 新增純函式 `required_heading_hint()` 由上述常數機械產生 prompt 文字，
  `planning_runtime.py` 直接呼叫它，prompt 端不再持有第二份真實來源。

產生出的文字逐 kind 給精確標題、明確禁止附加 kind 名稱，並揭露完整可接受集合
（給模型合法替代選項而非單點命中）：

> The required heading depends on the artifact kind: use exactly `## Requirements`
> for kind=spec, exactly `## Decisions` for kind=design, exactly `## Tasks` for
> kind=plan. The heading text is that word alone; do not append the kind name or
> any other suffix such as "for spec", "for design", "for plan" to it. Heading
> text is matched case-insensitively against a fixed set, so any one of these is
> also accepted — spec: Requirements, Requirement, Problem, Problem and Outcome,
> Goals; design: Decisions, Decision, Design, Architecture; plan: Tasks, Task.

判準本身（`_REQUIRED_HEADINGS` 的可接受集合）與 validator 邏輯不動；驗收行為零變更。

不在範圍（後續票）：`#516` 已修的 echo-back 欄位語意；其他 planning 失敗模式
（`#507`／`#511`／`#514`／`#515`／`#519`）。
