---
type: fix
scope: coordinator
---
**診斷 invariant 家族（#527／#514／#515／#511／#482）：把「理由」從慣例升格成型別**

0813–0814 **五次**獨立命中同一條缺口：狀態被推向「人要接手」的那一刻，理由沒有
跟著落地。五個現場形態不同、根卻同一條，逐案補洞已證明無效（#397／#408／#513 各
補過一次，缺口照樣在下一個地方冒出來）。

| issue | 現場 |
| --- | --- |
| #527 | build 階段無聲掛 `needs_human`——無 evidence、無 slice、`cortex status` 不呈現、`next_actions` 空 |
| #514 | brainstorm artifact 重驗失敗的例外不含路徑與原因 |
| #515 | `_post_integration_artifact_evidence()` 的 **14 個裸 `return None`** 塌縮成不透明的 `primary-artifact-invalid` |
| #511 | planning artifact 拒收未帶原因也不留存內容（#513 已修訊息與 evidence，但那份結構化理由到不了 run） |
| #482 | retry-review 的 absent evidence key 不含原因或 identity，合法重試撞 immutable artifact |

### invariant

> 任何把 run 轉入 `needs_human`、或把 evidence 標為 absent 的狀態變更，必須同時
> 落一份結構化理由（機器可讀 reason ＋ 人可讀 detail ＋ 來源位置）到 run 或
> evidence，並可由 `cortex status`／`work show` 曝光。

### 修法：驗證層擋在唯一的進入點上

新增 `coordinator/diagnostics.py` 的 `DiagnosticReason`（reason／detail／source
＋可選的 evidence_refs／context），並在 registry 的狀態轉移 API
（`_manager_update_workflow_run`／`_manager_create_workflow_run`——全庫**唯一**兩
個能把 `needs_human` 寫進 run row 的入口）強制三條規則：把 facet 加進去必須帶理由
（否則 `DiagnosticInvariantError`）、facet 移出時理由一併清掉（陳舊理由比沒有理由
更糟）、facet 已在而這次沒帶新理由則沿用既有理由（大量呼叫端會重複寫同一個
facet，第一次的理由才是根因）。`WorkflowRun` 新增 `needs_human_reason` 欄位持有這
份理由，並鎖住「理由不得與 facet 脫鉤」；「facet 有、理由沒有」則刻意放行，既有
部署的狀態檔裡就躺著這種 run，載入時 fail-closed 會把 manager 打掛。

配套的**掃描式 invariant 測試**以 AST 枚舉全庫所有把 `needs_human` 寫進 facets 的
設置點，斷言每一個都同時帶理由——新增一條忘了帶理由的設置點會在單元測試就炸，不
必等到 dogfooding 現場。掃描器本身另有反證測試（一個刻意違規的樣本必須被抓出來），
避免「掃描器壞掉」偽裝成「invariant 通過」。

### 五張 issue 的補洞點

- **#527**：`manager_daemon` resume 迴圈的例外過去只被 `_log_error` print 到
  stderr（由 service-manager 導向 `~/.agents/log/manager.log`），run 上只剩一個沒
  有理由的 facet；`exc` 就在手上，現在寫進 run。呈現面補上：狀態快照 provider 過
  去只走 `list_slices()`，而 workflow lane 從不建立 slice row，run 因此在
  `ready`／`held`／`slices`／`attention`／`recent_done` 五份清單裡**一份都不出現**
  ——新增 `manager.workflow_status_entry()` 把 ongoing 且 needs_human 的 run 投影進
  同一份 `attention` 清單。
- **#514**：`_validated_brainstorm_planning_authority()` 迴圈裡每一條 raise 都補上
  `ref=`；assessment 拒收沿用 #513 的 `(reasons=...; markers=Lnn:...; evidence=...)`
  格式與 `cortex-planning-artifact-rejection/v1` evidence 落檔。依 0814 adversarial
  review 的修正，診斷同時做在**真正會被走到的** hash drift 分支上（「artifact 在磁
  碟上被改動」走不到 assessment），訊息帶 evidence／disk 兩邊的 digest。
- **#515**：14 個裸 `return None` 全部改為帶原因的 `ArtifactEvidenceFailure`，環境
  類（symlink／路徑逃逸／非一般檔案／解碼失敗）與內容類（assessment 不合格／整合
  後仍不完整）分得開；assessment 類拒收透過新的 `rejection_recorder` 注入點沿用
  #513 的 evidence 落檔（被拒內容會在緊接著的 `rollback_publication()` 被撤下，不
  先存一份就再也看不到）。另加 AST 回歸樁鎖住「這個函式裡不得再出現裸
  `return None`」。
- **#511**：#513 的結構化理由現在經由 invariant 層真的落到 run 上（過去只剩上游
  `str(exc)[:160]` 截斷後的字串殘骸），並經 monitor observations 曝光到
  `cortex work show` 的 `blocking_reason`。
- **#482**：pre-launch absent evaluation 的落點納入**原因與請求身分**的指紋
  （`absent_evaluation_key()`）。同原因＋同身分仍是冪等重寫；不同原因或不同身分則
  各自落地，`missing → unknown → registered` 這條合法的設定推進不再需要刪 evidence
  才能前進，前一份 absent evidence 原位保留。reviewer job 已存在時的
  `{slice_id}-{reviewer_job_id}.json` 一字未動。

### 範圍紀律

只修「診斷與理由」。後續處置（retry／needs_human／fail-closed 邏輯）一律不變——每
個呼叫端原本會做什麼，改完之後照樣做什麼，只是多落一份可稽核的理由。呈現面沿用既
有欄位機制（`blocking_reason`／`provider_outcome`／`gate_reason`），沒有另立平行欄
位體系；`next_actions` 只沿用既有的 `_build_phase_recovery_actions`（只宣告會被受
理的動作，#382 的教訓），不新增任何處置判定。

新增 `tests/test_diagnostic_invariant_family_527.py`（32 個測試：掃描式 invariant
＋執行期強制＋五張 issue 的原始現場 fixture）。
