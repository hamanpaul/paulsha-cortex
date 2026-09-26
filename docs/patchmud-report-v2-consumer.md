# PatchMUD report v2 消費契約

## 榜列 cohort 查詢

Cortex 在 runtime 不匯入 PatchMUD，直接讀取 PatchMUD report v2。clear-rate 榜列
以完整 cohort identity 查詢：

```text
role + benchmark_type + profile_id + deck_digest + evaluator_revision
```

`profile_id` 必須符合 Cortex `execution_profile.profile_key()` 的格式
`epk:v1:<plane>:<sha256>`，而 `role` 必須對應 Cortex persona。`model` 與
`loadout` 保留為報表觀測值，不作查詢鍵。相同完整 identity 出現多列、cohort
identity 不完整、榜列未排名，或 coverage 不完整時，不產生實測封套。

`cortex model profile` 從封存的 `runs[]` 取得五個 identity 欄位。這條評測巷道
每次只執行一個 model、persona、deck 與 loadout；若報表的 runs 混有不同 cohort、
role、deck、loadout 或 model，便 fail-closed。之後 mapper 仍以完整 identity
選榜列，不會把其他榜列併入目標 cohort。

## 版本相容

| PatchMUD report | Cortex 處理方式 |
| --- | --- |
| v1 | Cortex mapper 明確拒絕排名，因為 v1 缺少 v2 cohort identity。依上游契約，v1 只能由 PatchMUD opaque reader 保留；要取得排名資格，須從 RunStore 重建 v2 report。 |
| v2 | 只有完整 cohort identity 相符、榜列可排名且 coverage 完整時才接受。 |
| 未知版本 | Fail-closed；不猜版本，也不退回 `(model, loadout)`。 |

這遵循 PatchMUD report 遷移契約：v1 僅 opaque 保留，不得升格進 v2 排名。Cortex
的 `model-eval-roster.yaml` 是另一份人工核可契約；其 schema v1 仍可作既有核可紀錄
讀取。

## Eval roster schema v2

Schema v2 每列綁定一個 PatchMUD role、execution profile 與完整 cohort evidence。
不同 `benchmark_type`、`deck_digest` 或 `evaluator_revision` 可有各自的核可列，
resolver 必須以五個 cohort 欄位完整比對；只有完全相同的 cohort 才會匹配。相同
完整 identity 的重複列會拒收。缺少當前 cohort identity 時，v2 entry 不授予資格。

```yaml
schema_version: 2
entries:
  - executor: claude
    model_id: sonnet
    role: builder
    execution_profile_key: epk:v1:resolved:<64 lowercase hex digits>
    benchmark_type: issue-resolution
    deck_digest: sha256:<64 lowercase hex digits>
    evaluator_revision: sha256:<64 lowercase hex digits>
    verdict: pass
    evaluated_at: "2026-09-26"
    eval_source: patchmud-report-v2
    eval_ref: <report fingerprint 或 evidence 參照>
    review_status: approved
    reviewer: operator
    reviewed_at: "2026-09-26"
```

Resolver 將 `builder`／`planner`／`reviewer` 對應到既有的 `build`／`planning`／
`review` role。它從 identity 的 `profile_provenance.observation` 讀取 mapper 保存的
完整 cohort identity；也可由呼叫端明確提供 profile key 與 cohort identity。缺欄位或
任一欄不符時會落回 packaged-fallback。Schema v1 保留原有 `roles` 清單與
identity-level approval 語意。

## Fixture 來源

Golden fixture 正反例與 manifest 複製自 PatchMUD revision
`421fadc7dc16b6ee9020bdc2333ff6fe856accaa`。測試會比對來源 revision，並驗證 manifest
列出的每個相對路徑 fixture 的 SHA-256。
