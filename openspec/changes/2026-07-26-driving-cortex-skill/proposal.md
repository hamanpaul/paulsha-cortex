---
status: accepted
work_item: driving-cortex-skill
---

## Goals

新增 Claude Code skill `driving-cortex`，教 agent 如何驅動 cortex dogfood 派工與交付，結晶 v0.1.0 實機驗收心得，讓後續 agent 幾分鐘上手而非重新踩坑。

## Why

v0.1.0 全程 cortex dogfood 實機驗收證明派工/交付模型可用，但新 agent 面對 cortex 沒有「怎麼開這台車」的簡明指引——本次靠跑完 9 批 + 修 17 個 runtime bug + 大量恢復操作才摸透操作模型。這些 hard-won 知識應固化成 skill（#177）。與 B8 onboarding docs（人類 operator 視角）互補，本 skill 是 agent 編排 coordinator 視角。

## What Changes

- 新增 `skills/driving-cortex/SKILL.md`：Claude Code skill，frontmatter `description` 觸發詞；七段骨架（心智模型／開一個 dogfood 批次／驅動桿／執行器設定／每批 merge 後部署／生命週期特性／已知坑）；恢復桿與坑以 checklist 呈現。
- 新增 `tests/test_driving_cortex_skill.py`：契約測試鎖定 SKILL.md 存在、七段標題、R-21 路徑衛生、unsafe bypass flag 不作日常指引。
- README 補 skill 導覽一行；`changelog.d/driving-cortex-skill.md` 與 `CHANGELOG.md [Unreleased] ### Added`。

## Capabilities

### Modified Capabilities
- 無 runtime capability 變更；本 change 為 agent 慣例檔（skill）與其契約測試。