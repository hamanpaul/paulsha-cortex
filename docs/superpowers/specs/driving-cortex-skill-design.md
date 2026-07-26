---
status: accepted
work_item: driving-cortex-skill
---

# driving-cortex-skill Design

## Decisions

### D1 Claude Code skill 格式優先（非 cortex 原生 deck skill 卡）

選 **Claude Code skill**（`skills/driving-cortex/SKILL.md`，frontmatter `description` 觸發詞）為首要交付物。issue #177 明確訴求是「告訴 agent」，Claude Code skill 是 agent 在 session 內可被路由選用的形式；cortex 原生 deck skill 卡（`kind: skill`）可為後續選項，本批不交付，避免擴大爆炸半徑。

不選「同步作 cortex deck skill 卡」：需動 deck schema 與 persona 載入路徑，超出 #177 訴求範圍。

### D2 內容來源＝v0.1.0 實機驗收心得（issue #177）

七段骨架直接採 issue #177 提供的心得（心智模型／開批次／驅動桿／執行器設定／部署／生命週期／已知坑）。所有命令、時序陷阱、恢復桿均回溯到該次 dogfood 實證與關聯 issue（#152/#100/#175/#158/#83/#99/#148/#142），不杜撰未落地行為。

### D3 與 B8 onboarding docs 交叉引用

SKILL.md 於頂部說明兩視角分工：B8 `docs/onboarding/*` 是人類 operator 的 install/bootstrap 旅程；本 skill 是 agent 編排 coordinator 視角。兩者交叉引用，避免重複維護兩份操作模型。

### D4 契約測試鎖定結構與路徑衛生

新增 `tests/test_driving_cortex_skill.py`，TDD RED 先行：斷言 SKILL.md 存在、frontmatter `description` 存在、七段關鍵標題（心智模型／開一個 dogfood 批次／驅動桿／執行器設定／每批 merge 後部署／生命週期特性／已知坑）皆出現、全文無個人絕對路徑與使用者名／雇主識別（R-21）、且不含 `--dangerously-bypass-approvals-and-sandbox` 作為「日常指引」（issue #177 提及直接派 codex 修 bug 時該 flag 為情境命令，非日常推薦；測試確保不被寫成常態操作）。

### D5 不改動範圍

- 不改 cortex runtime（coordinator/manager/monitor/dispatcher）。
- 不改 model-identities.yaml schema。
- 不改既有 planning-authority plan 檔。
- README 僅補 skill 導覽一行（R-18 docs 對齊），不重寫 onboarding 段。

## 風險

- skill 內容若引用尚未落地的命令行為會誤導 agent：以契約測試鎖定關鍵標題存在性，並以 R-21 路徑衛生測試防止洩漏個人路徑。
- 「直接派 codex 修 bug」段含 `--dangerously-bypass-approvals-and-sandbox`：D4 測試確保該 flag 僅出現於明確標示的情境命令區，不作為日常指引推薦。