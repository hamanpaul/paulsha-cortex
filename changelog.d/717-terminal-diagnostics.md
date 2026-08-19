### Fixed

- **build 卡要說「我需要人」得先交出 40-hex candidate——而失敗原因正是「一條命令都
  跑不了」時結構上做不到，模型 diagnostics 全被丟掉（#717）。** 實機 job
  `wf-6c37c77ca1-worktree-isolation-7`：模型**正確地**回了 `needs_human`，把病因逐字
  寫進 envelope 的 `diagnostics`（`唯讀 git 檢查連續兩次遭執行環境 sandbox runtime
  panic…` ／ `permission profiles requiring direct runtime enforcement are
  incompatible with --use-legacy-landlock`），operator 端只看到
  `ATTENTION: build/card-terminal-schema-retry-exhausted` ＋
  `workflow terminal payload did not satisfy the result contract`。同一族診斷缺陷
  的第六輪（#672 #679 #701 #704 #707）。
- **表達力（裁決 (1a)）**：`manager._retryable_nonpassing_workflow_terminal()` 是
  「模型明示要求停止」的唯一入口，過去 build phase 的入場券是「交得出
  `git rev-parse HEAD` 的 40-hex SHA」——於是**唯一一種本契約無法表達的失敗模式，
  恰好是最需要被表達的那一種**（#716：模型取不到 HEAD，只能填 contract 裡看得到
  的 64-hex `source_revision`）。非通過狀態（`failed`／`needs_human`）下 `candidate`
  改為只收斂型別（`null` 或字串），不再驗 40-hex；`passed` 的判準一個位元都沒動。
- **診斷落地（缺陷 2）**：`manager._terminal_parse_diagnostics()` 新增
  `model_diagnostics`，直接從**原始** envelope 讀模型寫的 `diagnostics`（刻意不經
  `_canonicalize_card_terminal()`——那個投影正是把它丟掉的地方；該函式宣稱這兩個
  欄位「已在 `_assert_terminal_gate_consistency` 消費完畢」的註解對 `diagnostics`
  不成立，malformed／schema-retry 分支根本走不到那條路徑）。內容有界
  （`TERMINAL_MODEL_DIAGNOSTICS_LIMIT = 2000`，沿用 #606 `RETRY_CONTEXT_EVIDENCE_LIMIT`
  的同一個理由與量級），被截的項目以 `…` 明示。
- **明示停止的落地分支**：`resume_workflow_run` 新增 `card-terminal-explicit-stop`
  ——模型明示停止時直接落 `needs_human`，attention 的 `D=` 逐字帶模型病因，
  **不消耗** schema retry 額度（那份額度是給「模型寫壞 JSON」的，不是給「環境壞掉」
  的）、也不自動回派。此前這一組 job 要一路走到 `terminalize_workflow_job` 才被
  `workflow card terminal evidence did not pass` 擋下，operator 收到的是離病因兩層
  遠的 `terminalize-workflow-job-failed`，而且例外會往上擲。
- **retry 額度與重派語意（#717 追加觀察 (i)(ii)）**：
  `registry._manager_reset_workflow_for_retry_card()` 重置時只 bump
  `attempts[phase]`，沒清同一個 dict 上的 `schema-mismatch:<card>` ⇒ operator 顯式
  重派之後「這一輪一次自動重試都沒有」卻寫成「已達上限（2/2）」。現在 `retry-card`
  清本輪額度（operator 的顯式重派＝重新給一輪），值搬到新的
  `schema-mismatch-total:<card>` 累加、**永不清零**；attention 文案改為
  「本輪 n/N，該卡累計 m 次」，兩個數字不再共用同一個 `(n/N)`。

### Changed

- `run.attempts` 兩個 schema mismatch 計數鍵的前綴收斂到
  `terminal_contract.{SCHEMA_RETRY_ATTEMPT_PREFIX,SCHEMA_MISMATCH_TOTAL_PREFIX}` 與
  對應的 key 函式——registry 的 `retry-card` 重置需要認得同一個鍵，而 registry 不
  import manager；字面量再多一處抄寫就是漂移的溫床。
- `resume_workflow_run` 的 status surface 新增 `schema_mismatch_observed`（累計）與
  `declared_status`（明示停止時模型自述的狀態）。

### Notes

- 本 PR **不新增**任何熔斷。`retry-card` 本身仍無次數上限（#555 仍 open）；清
  `schema-mismatch:<card>` 並未移除任何**既有的**熔斷，只移除了跨 `retry-card`
  世代的意外殘留（該值除了 `resume_workflow_run` 的額度判定，全庫只有
  `monitor.providers._schema_retry_rows()` 這個唯讀呈現面在讀）。
