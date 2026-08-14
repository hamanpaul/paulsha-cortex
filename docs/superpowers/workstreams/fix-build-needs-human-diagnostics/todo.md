---
status: accepted
work_item: fix-build-needs-human-diagnostics
---

# fix-build-needs-human-diagnostics Todo

`#527`：workflow run 在 `build` 階段被掛上 `needs_human`，但**沒有任何診斷訊號**——
`evidence_refs` 為空、未產生 slice、`cortex status` 不呈現、`tick`／`complete` 皆回報無錯誤
且不派工。operator 無從得知它在等什麼、還是已經壞掉。

## 實測（run `workflow-6607ac1307feb02ffe06`，work_id `fix-brainstorm-revalidation-diagnostics`）

```
run: 6607ac13 | phase: build | status: ongoing | facets: ['needs_human']
evidence_refs: []
passed cards: workflow-claim, brainstorming, openspec-propose, writing-plans
pending cards: worktree-isolation, tdd-red, subagent-build
resolved_model_chain: {'builder': {'executor': 'codex', 'model_id': 'gpt-5.6-luna', 'source': 'default-envelope'}}
```

時間軸：`14:58:28` 進入 `build`、facets 為**空**；約兩分鐘後變成 `['needs_human']`，
期間無 evidence 落檔、journal 無相關輸出。後續操作皆無效果且無訊息：
`tick` → `dispatched: []`／`errors: []`；`complete` → `errors: []`／`needs_human: null`；
`cortex status` → 本 run 不出現在 `slices`／`attention`／`ready`／`held` 任一清單；
`cortex work show` → `next_actions: []`。

## 系統性缺口（第四次命中）

與 `#511`（PR `#513` 已修）／`#514`／`#515` 屬**同一類、不同階段**：狀態轉換未強制附帶
可稽核理由。差別在於前三者至少留下一個（雖不完整的）reason 字串，**本情境連 reason 都沒有**
——`needs_human` 是唯一訊號。四次獨立命中顯示這不是個別疏漏，而是不變式缺失。

本 run 本身是好消息：它證明了 `#519` 的額度重置、`#524` 的 in-flight 保護傘與 artifact kind
修正都已生效（claim 成功、繼承前代 artifact、150 秒後未被自我 supersede、builder 正確解析到
`codex/gpt-5.6-luna`）。build 階段這道無聲停滯是鏈路上的下一層。

## 根因（2026-08-14 已定位）

`manager_daemon.py:983-996` 的 workflow resume 迴圈：

```python
except Exception as exc:
    _log_error(exc, context={"action": "resume-workflow", "work_id": ..., ...})
    registry._manager_update_workflow_run(
        workflow.run_id, facets=("needs_human",), gate_status="running",
    )
```

`_log_error()`（`:1404`）只 `print` 到 stderr，而 stderr 由 `scripts/service-manager.sh:149`
（`>>"$manager_log" 2>&1`）導向 `~/.agents/log/manager.log`——**不進 journal、不進 evidence、
不進 run**。run 上只留一個沒有理由的 `needs_human`，於是 `cortex status`／`work show`／
`tick`／`complete` 四個介面同時沉默。例外物件 `exc` 就在 except 區塊內，資訊一直在手上，
只是沒有寫進 run。

本次的實際失敗（log 中該行，時間與 `14:58:28 進入 build → 約兩分鐘後 needs_human` 吻合）：

```
2026-08-14T06:59:32Z manager_daemon error: ValueError: worktree target already exists
  (action=resume-workflow work_id=fix-brainstorm-revalidation-diagnostics ...)
```

`seams.py:70-77` `ScriptWorktreeCreator.create()` 對已存在的 target fail-closed。殘留物由
**前一代**（即被 `#524` 自我 supersede 的那一代）建立、run 死亡時未回收，新一代的
`worktree-isolation` 卡因此必然撞上。`manager.py:2180` 註解已記載此失敗模式（`#339`
冪等過濾），但那層防護只涵蓋 slice 重複 fanout，不涵蓋「前代 run 殘留 → 後代 run 開新 worktree」。

## Tasks

- [ ] **`needs_human` 必須永遠伴隨結構化理由**：所有設置該 facet 的路徑都要同時寫入 evidence（或在 run 記錄 `needs_human_reason`），無理由不得設置
- [ ] **以不變式測試強制**：任何把 `needs_human` 加進 facets 的更新都必須提供 reason，涵蓋現有全部設置點（而非只補本次踩到的那一處）——這是與 `#511`／`#514`／`#515` 逐案補洞的差別所在
- [ ] **`cortex status` 應呈現 needs_human 的 workflow run**，而非只呈現 slice；目前 run 停在 build 卻不出現在任何清單中
- [ ] **`cortex work show` 的 `next_actions` 在此情境需給出可行動作**，或明確說明「無可用動作，原因為 X」，而非回空陣列
- [ ] **釐清語意**：若此狀態實為設計上的等待（例如等待某外部條件），應以不同於 `needs_human` 的 facet 表達，避免與「需要人工介入」混淆
- [ ] **前代殘留回收**：run 被 supersede／abandon 時應回收其 build worktree，或讓後代能安全接手既有 worktree——否則 `#524` 每觸發一次就在磁碟留一顆地雷給下一代踩（與 `#478` 互為表裡：一邊只刪目錄未清 registry，一邊撞到目錄就 fail-closed）
- [ ] **`recover-pre-candidate` 應出現在此情境的 `next_actions`**：該動作正是為「回收 worktree 後重跑」設計，但 operator 無從得知它可用——有動作卻不呈現等同沒有
- [ ] **附帶：log 路徑未 instance-scope**：`scripts/service-manager.sh:135-136` 硬編 `$HOME/.agents/log/manager.log`，多 instance 錯誤交錯寫入同一檔案；與 `#518` 屬同一類 isolation 破口，修哪一邊需先裁定歸屬
