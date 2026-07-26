---
status: accepted
work_item: driving-cortex-skill
---

# driving-cortex-skill Specification

#177：新增 Claude Code skill `driving-cortex`，教 agent 如何驅動 cortex dogfood 派工與交付，結晶 v0.1.0 實機驗收心得。

## 背景

2026-07-24 完成 porcelain CLI 七家族 + docs + release pipeline（epic #84，v0.1.0 發佈）的全程 cortex dogfood 實機驗收——所有實作批次由 cortex coordinator 自己派工建構。過程證明 cortex 的派工/交付模型可用，但一個新 agent 面對 cortex 沒有「怎麼開這台車」的簡明指引；本次是靠跑完 9 批 + 修 17 個 runtime bug + 大量恢復操作才把操作模型摸透。這些 hard-won 知識應固化成 skill，讓後續 agent 幾分鐘上手，而不是重新踩一遍。

與 B8 onboarding docs（`docs/onboarding/*`，人類 operator 的 install/bootstrap 旅程）互補——本 skill 是「agent 編排 coordinator」視角。

## Requirements

### R1 skill 產物與觸發

SHALL 新增 `skills/driving-cortex/SKILL.md`，為 Claude Code skill 格式（frontmatter 含 `description` 觸發詞，如「dogfood cortex」「派工 cortex」「cortex work start/resume」）。`description` MUST 讓 skill 在 agent 欲驅動 cortex dogfood 派工與交付時被選用。

### R2 七段內容骨架

SKILL.md MUST 涵蓋以下七段（issue #177 心得骨架）：

1. **心智模型**：manager daemon = 單一 writer（所有狀態變更走它、control queue、5s 等待逾時不代表失敗）；Monitor = 讀模型（`.cortex/work-items.yaml` + workstream `todo.md` → WorkAuthority）；deck skill 卡 + persona 交付鏈（claim→define→plan→build→verify→review→ship）。
2. **開一個 dogfood 批次**：規劃產物契約（YAML frontmatter `status: accepted` + `work_item`、必要標題、不可有 TBD、`assess_planning_completeness` 離線驗證）；`.cortex/work-items.yaml` 加 entry + workstream `todo.md` + openspec change；`systemctl --user restart cortex-monitor.service` 並等 snapshot 含新 work_id。
3. **驅動桿**：`cortex work start`、`cortex work resume --expected-run-id`、`cortex work retry-build --payload`、`cortex work review-attest --payload`、merge 卡 `review-thread-open` 的 GraphQL `resolveReviewThread` 後 resume。
4. **執行器設定**：`~/.agents/config/paulsha/model-identities.yaml`（build executor 取自 identity.executor、`model_id` 須與 `--model` 逐字同、builder/reviewer `independence_domain` 不同）；直接派 codex 修 bug 必帶 `-c model_reasoning_effort="high"`。
5. **每批 merge 後部署**：`pipx install --force <repo>` + 重啟 manager/monitor；部署時序陷阱（daemon 早於新檔案 mtime 起動→跑舊 code，驗證 lstart > deployed mtime）；F44 env/unit 可被別 venv 嵌入端覆寫（restart 前 grep env）。
6. **生命週期特性**：run closure 輸送帶延遲一批（批次 N 的 run 收尾需其 todo 在 remote main 全勾，慣例在 N+1 規劃 PR 才勾 → N 停在 ongoing/review/needs_human 直到 N+1 merge 後 resume；非故障、不擋 N+1 claim；最後一批需獨立 PR 勾 todo）。
7. **已知坑**：`#152` 5s timeout、`#100` manager.log 無時間戳、`#175` re-claim 後繼承已關閉 PR（別在已開 PR 後 re-claim）、`#158` archive spec Purpose 恆 TBD、`#83` worktree/branch 無 GC、`#99` daemon cwd 耦合；外部產物建後交叉驗證（`gh ... view` 確認）。

恢復桿與坑清單 MUST 以 checklist 形式呈現，可直接逐項操作。

### R3 內容治理

SKILL.md 的技術宣稱 MUST 可回溯到 v0.1.0 實機驗收心得（issue #177）、既有 CLI 行為、service 行為或關聯 issue；MUST NOT 把尚未落地的行為描述為既成事實。MUST 與 B8 `docs/onboarding/*` 交叉引用（人類 operator vs agent orchestrator 兩視角）。

### R4 路徑衛生（R-21）

SKILL.md MUST 一律以 `~`、`$HOME`、環境變數或相對路徑表示路徑；MUST NOT 出現任何個人絕對路徑、使用者名或雇主／廠商識別（本 repo `tier: shareable`）。

### R5 契約測試

SHALL 新增 `tests/test_driving_cortex_skill.py`，鎖定：SKILL.md 存在、frontmatter 含 `description`、涵蓋 R2 七段關鍵標題、全文無個人絕對路徑／使用者名／雇主識別（R-21）、且不得出現 unsafe bypass flag 作為日常指引。

### 驗收與限制

一個沒跑過 cortex 的 agent，讀完 skill 後能獨立完成一個 dogfood 批次（claim→build→review→attest→merge）而不需重新摸索恢復桿。`python3 -m pytest tests/ -q` 全綠；`python3 -m policy_check --repo .` 0 fail（含 R-18/R-21/R-22）；`git diff --check` 乾淨；不新增 runtime 依賴；不改動既有 planning-authority plan。