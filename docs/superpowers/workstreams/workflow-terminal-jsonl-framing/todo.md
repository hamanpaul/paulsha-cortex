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

# Workflow terminal JSONL framing 工作清單

Owner 為 [#860](https://github.com/hamanpaul/paulsha-cortex/issues/860)。Root 已依獨立內容審查接受本三件組，可進 repository intake；不代表產品 freeze／dispatch／qualification。[Spec](../../specs/workflow-terminal-jsonl-framing-spec.md) 與 [design](../../specs/workflow-terminal-jsonl-framing-design.md) 為同 work item。事故 #822／refine R13 primary、R06 secondary；以下都是未來 Cortex 產品工作，沒有任何產品 checkbox 已完成。

## Tasks

- [ ] **T1 tests／RED（R1–R3、R7–R8）**：新增 `tests/test_workflow_terminal_jsonl_framing.py`，以合成 payload、真 `tmp_path` JSONL 與 actual `_extract_terminal_json`，parameterize raw/escaped U+0085/U+2028/U+2029、inner/outer ensure_ascii 四組、只有 outer 副本含 separator 等；先證 baseline 是收集成功後的 assertion/ValueError RED。深度相等驗全部 details/reports 字串，不只 status。
- [ ] **T2 source／GREEN（R1–R6）**：只在 `paulsha_cortex/coordinator/manager.py::_extract_terminal_json` 採 design D1 的 newline-preserving file open 與 literal LF record split；維持 UTF-8、原錯誤映射、反向掃描、carrier／wrapper／schema 與 fence。不改 provider、status alias、其他 parser 或任何 state writer。
- [ ] **T3 tests／framing 相容（R1、R2、R6）**：真檔測 LF/CRLF、空行、末筆無換行、尾端空行、裸 CR 相接兩筆 JSON 拒收，以及單筆 JSON 合法 CR whitespace 相容；在讀取前後 hash 相同。測 Unicode 一般文字／組合字無變動；不得因 test serializer 預設 ensure_ascii 讓 raw 案例其實沒 raw 字元。
- [ ] **T4 tests／雙層反例（R2–R3、R8）**：分開驗 inner escaped+outer raw structured_output 的舊失敗、候選成功；`type=result` 只有任意 structured_output 而沒有已認可 carrier 時仍拒收；wrapper 的 unknown／多層偽 evidence 不被新 fallback 接住。
- [ ] **T5 tests／terminal 信任邊界（R4–R6）**：執行並擴充 `tests/test_workflow_production_wiring.py` 的 terminal extraction cases、`tests/test_terminal_result_contract.py` 的 wrapper cases。真 terminal 後接 turn.completed／文字雜訊／tool.execution_complete.data.content spoof，結果仍是前者；只有 spoof 則 no JSON evidence。保留既有完整 fenced JSON、AGY progress+trailing fence、progress+trailing JSON，以及內嵌 fake payload 後仍有 prose 的拒收。Design D4 已披露 generic top-level text 的既存接受，不假寫「所有 tool event 都拒收」。
- [ ] **T6 tests／schema 和純讀負例（R5–R8）**：parser fixture 以 missing path、missing file、invalid UTF-8、純非 JSON、錯 terminal shape、report 路徑／size 等既有 harness 驗失敗邊界。JSON extraction 與 per-phase schema/report validation 分開斷言；不跑 live terminalize／model／registry writer。需要 consumer integration 時僅用 tmp_path registry 和現有 fake gate，不能採信 fake 為產品 gate。
- [ ] **T7 tests／immutable 事故重播（R7–R8）**：用可公開合成 fixture 釘住原 shape、NEL 數量與 deep equality。Owner 若額外允許對私有 #94 replay，只讀指定 log，記原 hash／bytes／實體行／byte positions與讀後 hash；舊 parser FAIL／候選 parser PASS 是 parser-only 證據，不能填 verification、review、ship 或 CompletionRecord 已完成。
- [ ] **T8 documentation／操作與 CLI（R9）**：同步 `docs/unified-work-lifecycle.md` 的 terminal extraction／recovery 邊界，說明 LF/CRLF、Unicode data、只 escape inner result 不足及 ASCII 名稱暫避的限制。README 依真實接口影響核對，沒有新 CLI flag 不虛造新旗標；在 checkout 外用候選環境真跑 `python3 -m paulsha_cortex.cli work start --help`、`recover work --help`，保留退出碼，禁止送 live request。
- [ ] **T9 documentation／自有 OpenSpec 與 changelog（R10）**：以 owner #860 採產品 branch `feature/860-workflow-terminal-jsonl-framing`，沿用本次已備妥的 [自有 OpenSpec upfront baseline](../../../../openspec/changes/workflow-terminal-jsonl-framing/proposal.md)（proposal/design/tasks/spec delta），不在 freeze 後重新建立 planning 文件；核對全部 R1–R10 與 T1–T10，實跑 `openspec validate workflow-terminal-jsonl-framing --strict --no-interactive` 與 repo canonical-spec gate。Operator baseline 不改；candidate 只更新有證據的 checkbox，其餘內容變更須回 root 走正式 authority 流程。Active OpenSpec tasks 的 checkbox 只列 pre-archive 的實作／測試／文件／驗證；archive 本身與下游 reverify／review／CI／PR／merge／closure／runtime qualification 仍是未完成的交付要求，以 prose 記錄，不把自身 archive 或下游成功列成 archive 前置 checkbox，也不虛勾。此清單 T10 的全程交付要求保留，不整段複製成 active OpenSpec 的 pre-archive checkbox，避免自相依。不要修改／archive #822 的 active change。新增並 commit `changelog.d/workflow-terminal-jsonl-framing.md`、補 `CHANGELOG.md [Unreleased]`；不沿用 planning author branch 的 slug 當產品 fragment。
- [ ] **T10 tests／full、policy、獨立交付（R1–R10）**：focused 三檔 → `python3 -m pytest tests/ -q` → pinned preflight（`.project-policy.yml` canonical OpenSpec specs + full suite）與帶真 PR title/body/labels/base/head 的 policy_check → git diff --check → Python 3.10–3.13 既有 CI／build/smoke-install。Independent review 必須綁 exact Candidate 並逐項處置 finding，正式 Cortex 完成自有 OpenSpec archive／reverify／PR／merge／closure。Parser-only replay、author pure gate、CI、merged 與實際 Manager loaded revision 分別記錄；部署需共享 owner 維護窗口，不在本任務重啟或改寫 live。

## Testability

所有測試可不耗模型執行；合成 logs、tmp_path、pure schema helpers 是主要 oracle。未知 provider semantics、generic carrier authentication、O(bytes) 全檔資源、append/rotation、其餘 14 類與 live maintenance 是 design D4／母 refine 的列管殘餘，不把本 child 完成寫成整體 R06/R13 關閉。

## Gate status

本三件組已依 root 接受標示 accepted，現行真 helper completeness=true、6/Yellow；這是 repository intake 的內容證據，不是正式 freeze／dispatch／qualification。Domain/state 仍由 production scope 判定，不以「consumer 測試觸及別模組」抬高，也不因歷史 draft 的舊算法 raw score 較低宣稱 ready。首稿 accepted counterfactual 保留於歷史 report；#831 真載入後的 4/Yellow 仍是條件投影，兩者都仍須正式 completeness／Yellow plan review／readiness。
