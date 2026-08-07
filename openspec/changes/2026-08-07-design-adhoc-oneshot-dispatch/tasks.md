---
status: draft
work_item: design-adhoc-oneshot-dispatch
---

# Tasks

design-doc 票，非 code TDD RED/GREEN；驗收為文件三件套完整＋經至少一輪
review，比照 `2026-08-04-design-task-type-taxonomy`／
`2026-08-07-builder-task-boundary-segmentation`／
`2026-08-07-design-model-capability-envelope` 等既有 design-doc 票慣例。

- [ ] 1.1 `proposal.md`／`design.md`／
      `specs/trusted-dispatch-completion/spec.md` 三件套完整，且與
      `docs/superpowers/specs/adhoc-oneshot-dispatch-{design,spec}.md`
      內容一致（openspec 三件套為摘要、docs/superpowers 為完整論證，
      兩者不得互相矛盾）。
- [ ] 1.2 `docs/superpowers/specs/adhoc-oneshot-dispatch-spec.md` 的
      R1-R5 逐條可對應到 issue #279 原文四個阻礙之一，且每條皆指出
      對應 D 決策與至少一個 main 上現有檔案／函式作為改動錨點（非空泛
      陳述）。
- [ ] 1.3 D4（跨 repo persona catalog 缺口已解）附上 #341 commit
      `0264f3f` 為 `a2e8d0c`（本票查證基準）祖先的核驗方式（
      `git merge-base --is-ancestor`）與 #338／`0264f3f` 的建立/落地
      時間對照，作為「阻礙已消失、issue 未關閉」判定的可稽核依據。
- [ ] 1.4 本設計文件經至少一輪 review（人工或 reviewer persona）才可
      勾完此清單；不可自我勾完就 claim done。
- [ ] 1.5 `changelog.d/adhoc-oneshot-dispatch-design.md` fragment 與
      `CHANGELOG.md [Unreleased]` entry（#279）。
- [ ] 1.6 `python3 -m pytest -q` 全綠（docs-only 變更，不應影響既有
      測試）；帶 PR 上下文的 `policy_check` 0 fail；`git diff --check`
      乾淨。

## 驗收

三件套（openspec proposal/design/spec）與 docs/superpowers 完整文件皆
存在且互相一致；D1-D6／R1-R5 皆可證偽（每條指出改動錨點與「不做的
後果」）；D4 明確記錄 #338 現況已過期且附可稽核依據；D6 明確記錄
`conflict_files` 原列五檔中三檔（`config/runtime.py`／
`deck/data/combos/feature-oneshot.yaml`／`model_identities.py`）在本
設計下不需修改；不動 `paulsha_cortex/` 任何程式檔。

## 後續應拆分的 code 票（建議，非本票範圍）

比照 #276 design-doc 票拆碼的既有慣例，避免單票過大：

1. **`cortex run once` 核心入口**（D1+D2+D3 最小組合，issue #279 核心
   訴求）：
   - 新 CLI 子指令（`coordinator/cli.py` 或 `cli.py`）：
     `--repo-root`／`--executor`／`--model`／`--prompt-file`／
     `--verify`／`--timeout`／`--keep-state`。
   - 組裝 ephemeral `JobRegistry`／`Dispatcher`／輪詢外殼呼叫
     `manager.run_tick()` 至終局或 timeout（D1）。
   - `--prompt-file` 內容接入 `small-fix` combo 的
     `brainstorming`／`writing-plans-light` 卡輸入（D3）。
   - 驗收：對一個已知外部 repo（無 repo-local persona override）跑
     `run once` 端到端完成一次 `passed`／`needs_human` 終局；未安裝任何
     instance 的環境可執行；ephemeral state 不寫入宿主 `~/.agents`（
     以偽造 `$HOME` 迴歸測試核驗，比照 `changelog.d/
     fix-test-production-state-leak.md` 的隔離測試模式）；既有
     `fanout`/`tick`/`work` 行為位元不變。
   - 這張票落地後，issue #279 阻礙 1／阻礙 2 後半／阻礙 3（在 D3 定義
     的部分滿足範圍內）即已解掉。
2. **`--identity-overlay` 臨時放行**（D5，可獨立於票 1 開發測試，但
   實際生效依賴票 1 的 ephemeral `PSC_PROJECT_CONFIG_ROOT` 存在）：
   - `run once` 新增 `--identity-overlay <path>`，複製進 ephemeral
     project-config root，dispatch 前呼叫既有 `load_model_identities()`。
   - 不修改 `model_identities.py` 任何驗證邏輯。
   - 驗收：overlay 身分可用於該次呼叫；shadow 衝突 fail-closed（既有
     行為的迴歸驗證）；宿主全域身分設定不受影響。
3. **驗證＋關閉 #338**（D4，非 code，屬外層任務清單既有的發版驗證
   步驟，此處僅記錄依賴關係）：
   - 用票 1 落地的 `run once`（或現行 `tick`）對一個無 repo-local
     override 的外部 repo 跑一次派工，確認 evidence
     `persona_catalog.source == "packaged"` 且不落 `needs_human`。
   - 附 evidence 於 #338 comment 後關閉。
4. **（backlog，明確 v1 非目標）「in-place」派工**（D2 風險段落）：
   - 若未來真的需要「在呼叫方既有 branch/worktree 內工作」，需獨立
     設計評估 `Dispatcher` 新增不重建 worktree 的變體（類比 #276 D1
     的 `redispatch()` 先例，但針對呼叫方任意既有路徑而非前一個 job
     自己的 worktree），並重新核對 `poll_done` baseline、`cortex work
     gc` 對這類 worktree 的回收語意——風險等級不宜與票 1 捆綁。

四張候選票各自可獨立驗收；票 1 是唯一解掉 issue #279 核心訴求的必要
票，票 2／3 可平行或稍後跟進，票 4 除非有新的明確需求，否則不建議
主動立案。
