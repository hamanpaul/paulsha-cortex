---
status: accepted
work_item: driving-cortex-skill
---

# driving-cortex-skill Design

## Decisions

### D1 Claude Code skill 格式優先

選 Claude Code skill（`skills/driving-cortex/SKILL.md`，frontmatter `description`）為首要交付物；cortex 原生 deck skill 卡為後續選項，本批不交付。issue #177 訴求是「告訴 agent」，Claude Code skill 是 session 內可被路由選用的形式。

### D2 內容來源＝v0.1.0 實機驗收心得

七段骨架直接採 issue #177 心得；命令、時序陷阱、恢復桿回溯到該次 dogfood 實證與關聯 issue（#152/#100/#175/#158/#83/#99/#148/#142），不杜撰未落地行為。

### D3 與 B8 onboarding docs 交叉引用

頂部說明兩視角分工：B8 `docs/onboarding/*`＝人類 operator install/bootstrap 旅程；本 skill＝agent 編排 coordinator 視角。交叉引用避免重複維護。

### D4 契約測試鎖定結構與路徑衛生

`tests/test_driving_cortex_skill.py` TDD RED 先行：SKILL.md 存在、frontmatter `description`、七段標題、R-21 無個人路徑、`--dangerously-bypass-approvals-and-sandbox` 不作日常指引。

## 風險

- skill 引用未落地行為會誤導 agent：契約測試鎖關鍵標題 + R-21 路徑衛生測試。
- unsafe bypass flag 被寫成常態操作：D4 測試確保僅出現於情境命令脈絡。