---
status: accepted
work_item: workflow-terminal-jsonl-framing
domain_breadth: 0
state_consistency: 0
invariant_count: 10
artifact_classes:
  - source
  - tests
  - documentation
---

# Workflow terminal JSONL framing 自有 OpenSpec tasks

Owner [#860](https://github.com/hamanpaul/paulsha-cortex/issues/860)，work_id `workflow-terminal-jsonl-framing`。本 upfront baseline 與 [proposal](proposal.md)／[design](design.md) 是完整 planning 輸入，並與 [workstream todo](../../../docs/superpowers/workstreams/workflow-terminal-jsonl-framing/todo.md) 的 R1–R10／T1–T10 同義對齊。Status accepted 是 root 接受的 planning scope，不是工作完成；本次所有 checkbox 都保持未勾。

## Tasks

- [x] **T1 tests／RED（R1–R3、R7–R8）**：新增 `tests/test_workflow_terminal_jsonl_framing.py`，以合成 payload、真 `tmp_path` JSONL 與 actual `_extract_terminal_json`，parameterize raw/escaped U+0085/U+2028/U+2029、inner/outer ensure_ascii 四組、只有 outer 副本含 separator 等；先證 baseline 是收集成功後的 assertion/ValueError RED。深度相等驗全部 details/reports 字串，不只 status。
- [x] **T2 source／GREEN（R1–R6）**：只在 `paulsha_cortex/coordinator/manager.py::_extract_terminal_json` 採 design D1 的 newline-preserving file open 與 literal LF record split；維持 UTF-8、原錯誤映射、反向掃描、carrier／wrapper／schema 與 fence。不改 provider、status alias、其他 parser 或任何 state writer。
- [ ] **T3 tests／framing 相容（R1、R2、R6）**：真檔測 LF/CRLF、空行、末筆無換行、尾端空行、裸 CR 相接兩筆 JSON 拒收，以及單筆 JSON 合法 CR whitespace 相容；在讀取前後 hash 相同。測 Unicode 一般文字／組合字無變動；不得因 test serializer 預設 ensure_ascii 讓 raw 案例其實沒 raw 字元。
- [ ] **T4 tests／雙層反例（R2–R3、R8）**：分開驗 inner escaped+outer raw structured_output 的舊失敗、候選成功；`type=result` 只有任意 structured_output 而沒有已認可 carrier 時仍拒收；wrapper 的 unknown／多層偽 evidence 不被新 fallback 接住。
- [ ] **T5 tests／terminal 信任邊界（R4–R6）**：執行並擴充 `tests/test_workflow_production_wiring.py` 的 terminal extraction cases、`tests/test_terminal_result_contract.py` 的 wrapper cases。真 terminal 後接 turn.completed／文字雜訊／tool.execution_complete.data.content spoof，結果仍是前者；只有 spoof 則 no JSON evidence。保留既有完整 fenced JSON、AGY progress+trailing fence、progress+trailing JSON，以及內嵌 fake payload 後仍有 prose 的拒收。Design D4 已披露 generic top-level text 的既存接受，不假寫「所有 tool event 都拒收」。
- [ ] **T6 tests／schema 和純讀負例（R5–R8）**：parser fixture 以 missing path、missing file、invalid UTF-8、純非 JSON、錯 terminal shape、report 路徑／size 等既有 harness 驗失敗邊界。JSON extraction 與 per-phase schema/report validation 分開斷言；不跑 live terminalize／model／registry writer。需要 consumer integration 時僅用 tmp_path registry 和現有 fake gate，不能採信 fake 為產品 gate。
- [ ] **T7 tests／immutable 事故重播（R7–R8）**：用可公開合成 fixture 釘住原 shape、NEL 數量與 deep equality。Owner 若額外允許對私有 #94 replay，只讀指定 log，記原 hash／bytes／實體行／byte positions與讀後 hash；舊 parser FAIL／候選 parser PASS 是 parser-only 證據，不能填 verification、review、ship 或 CompletionRecord 已完成。
- [ ] **T8 documentation／操作與 CLI（R9）**：同步 `docs/unified-work-lifecycle.md` 的 terminal extraction／recovery 邊界，說明 LF/CRLF、Unicode data、只 escape inner result 不足及 ASCII 名稱暫避的限制。README 依真實接口影響核對，沒有新 CLI flag 不虛造新旗標；在 checkout 外用候選環境真跑 `python3 -m paulsha_cortex.cli work start --help`、`recover work --help`，保留退出碼，禁止送 live request。
- [ ] **T9 documentation／既有 baseline 驗證與 changelog（R10／R-09）**：沿用本次 upfront proposal/design/tasks/spec delta，不重新建立另一份 change，不修改 #822 的 active change。核對 R1–R10／T1–T10 coverage，真跑 `openspec validate workflow-terminal-jsonl-framing --strict --no-interactive` 及 `openspec validate --specs`；只在 candidate 記錄已證實的 checkbox 完成。新增並 commit `changelog.d/workflow-terminal-jsonl-framing.md`、補 `CHANGELOG.md [Unreleased]`；產品 branch 是 `feature/860-workflow-terminal-jsonl-framing`，不以 planning author slug 取代。
- [ ] **T10 tests／pre-archive 候選驗證（R1–R10／R-09/R-16/R-19）**：focused 三檔 → `python3 -m pytest tests/ -q` → pinned preflight（canonical OpenSpec specs＋full suite）與帶候選 PR title/body/labels/base/head 上下文的 policy_check → `git diff --check`；實跑 local build/smoke-install，核對 Python 3.10–3.13 既有 CI 會收集新增 tests。PR context 必須與真候選相符，若當時尚未開 PR 須明標 intended context，不宣稱遠端 CI 或 PR 已完成。任一 pre-archive gate 未通過不得勾此項；archive 後的再次驗證另屬下游要求。

## Testability

Source/tests/documentation 均有實際任務；R-09 由 T9/T10 保留 changelog，R-16 由 T8 真 CLI help 與 T10 policy 檢查，R-19 由 T1/T5/T6/T10 的測試收集、full suite 與後續既有 CI 覆蓋。所有 focused fixture 都可離線且不耗模型；無 live registry／model／service mutation，原事故 log 不改。Private replay 不是 CI 必備依賴或完整驗收。

## Coverage

R1→T1/T2/T3/T10；R2→T1/T2/T3/T4/T10；R3→T1/T2/T4/T10；R4→T2/T5/T10；R5→T2/T5/T6/T10；R6→T2/T3/T5/T6/T10；R7→T1/T6/T7/T10；R8→T1/T4/T6/T7/T10；R9→T8/T10；R10→T9/T10 與下游交付要求。上位 todo 的 T1–T8 原文保留，本文件 T9/T10 只分離 pre-archive 與下游階段，不刪除任何交付 AC。

## Baseline 與下游交付（pending；不是 archive 前置 checkbox）

Operator baseline 自正式 freeze 後始終不改。Candidate builder 只有經證據支持的 checkbox toggles 可採既有 tolerance；其他內容變化必須走 root 正式 authority 流程，不能用重新生成 tasks 或更動數值繞過 drift。所有 sizing 欄位與完整 fix-standard（gate spine=2、cards=9、persona bindings=9、R-09/R-16/R-19）維持原值：現行完整 accepted=6 Yellow；#831 真 loaded 後才有條件投影 4 Yellow，envelope bypass 不是資格證據。

以下完整交付仍 pending：exact-Candidate 的產品獨立 verification/review、逐項 finding 處置、own OpenSpec archive、archive 後 reverify／preflight、Python 3.10–3.13 既有 CI、PR／merge／closure，以及 root 與共享 owner 同意安全窗口後的實際 Manager loaded revision／live qualification。這些依正式 Cortex 時序執行，不宣稱一律在 archive 之後才准 review，但不放進 active OpenSpec 的 pre-archive checkbox，避免把下游成功或 archive 自己變成 archive 前置條件。本段不取消原 todo T10；parser replay、author helper、CI、merged、deployed／loaded 各自分開記證據。
