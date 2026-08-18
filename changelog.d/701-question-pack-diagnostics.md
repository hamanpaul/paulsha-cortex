# 701-question-pack-diagnostics

- **#701：question-pack 驗證把六種以上的失敗塌縮成一句話、且不保存模型輸出——define
  穩定卡住卻無人查得動**——`validate_question_pack()` 的最後一關是整份 `to_dict()` 相等
  （`planning.py:797`），`pack_id`／任一 `question_id`／`kind`／`prompt`／`source_refs`／
  questions 的順序／數量**全部**塌縮成 `question pack does not cover exact completeness
  blockers` 一句話；而模型實際回了什麼**沒有任何地方保存**。實機後果是 define 穩定卡在
  `question-pack-malformed`（兩筆 work item × `work start`／`work resume` 兩種觸發路徑，
  四次皆同），落檔的 `cortex-planning-failure/v1` evidence 只有那句話。#701 的外部重現
  12/12 逐位元 MATCH、R-5 已排除，唯一擋著下一步的就是「診斷面是空的」。修法三件，
  **判準逐位元不動**——本票只讓失敗變成可診斷：
  - **逐欄差異取代一句話**：`describe_question_pack_difference()` 回報**第一個**差異的
    `<locator> expected=<值> got=<值>`，locator 為 `pack_id`／`questions[2].kind` 這種
    可直接對位的座標；訊息開頭一律另帶 `rows expected=N got=M`，「有幾條」與「差在哪」
    兩件事都答得出來。原句逐字保留在最前面，operator 既有的 grep 錨點不變。
  - **同型塌縮逐處掃過**：`validate_secondary_evidence()`（identity 兩種、question_id
    三種、覆蓋率不列出缺哪幾題）、`_validate_primary_integration()`（#516 的兩個
    echo-back 欄位不說抄成了什麼、`invalid keys` 不說缺／多哪個鍵、artifact ref
    對不上不說是「寫了沒人引用」還是「引用了沒寫」）、`_strict_string_list()`（三種
    失敗同一句、不說第幾項）、plan frontmatter 三處（`invariant_count`／
    `artifact_classes`／`scope_excludes`）、`builder envelope 格式錯誤`（五個條件同一
    句），以及 `required-section-missing`（#520 的同型缺口：現在一併印出 planner 實際
    寫了哪些標題與可接受集合）。
  - **失敗時保存模型的實際輸出**：questioner／secondary／integrator 三個 adapter 共用的
    `planning_runtime._invoke_json()` 在 rc≠0 時只留得下 `planning launcher failed:
    <executor>/<model>`——rc 與模型輸出隨 invoker 的 tempdir 一起消失。改為沿用 #670／
    PR #674 已建立的 `stdout_excerpt()` 帶上 `rc=<N> stdout=<節錄>`，**只讀 stdout、
    不讀 stderr**（票 A／PR #688 立下的憑證邊界）。`_extract_json_candidates()` 的兩處
    片段也改走同一支函式，不再是會把換行帶進單行 reason 的裸切。
- **截斷策略沿用票 A**：locator（哪一列、哪一欄）**永不被截斷、永不被遮罩**；只有值會被
  截，且視窗**對齊到第一個相異字元**（長 prompt 的前 72 字往往兩邊一模一樣，取前綴等於
  印兩份相同的字），前後各犧牲幾個字以 `<+Nc>` 就地記帳。四個 `except` 分支的例外摘要
  收斂成 `summarize_planning_exception()`，預算由 #397 的 160 放寬到 480（逐欄差異裝不進
  160），截斷同樣就地記帳。
- **防偽：模型輸出不得偽裝成分類標記**。差異文字進的是 `blocking_reason`，而
  `manager._classify_planning_failure()` 除了票 A 那條**錨在字串開頭**的 `grade=`，還有
  三條**不錨定**的文字判準（#533／#554 的 taxonomy marker 詞界比對、#416 的 authority
  殘留、#507 的 worktree drift 裸子字串）。若把模型的值原樣丟進 reason，模型只要在某個
  `prompt` 裡寫上 `timeout` 或 `503` 就能把一個 content 失敗改判成 environment。修法在
  **產生端**堵住：詞界類 marker 兩側各加一個 `_` 破詞界（字面一個字都不少），裸子字串類
  的三條長句整段換成不含 marker 的佔位符。**launcher 轉印的服務錯誤刻意不走這條**——
  #533 的 503 自癒路徑正是要看見那一段。
- **測試**：`tests/test_planning_diagnostics_701.py`（67 條）。核心斷言是**反塌縮**本身
  ——八種 question-pack 變異 ⇒ 八個**互不相同**的訊息，`questions[i]` 的六種 scalar 失敗
  ⇒ 六個、artifact row 四種 ⇒ 四個；防偽面對 `TRANSIENT_SERVICE_MARKERS` **全表**逐一
  掃描（比照 #554 的同型測試），並正反兩向釘住票 A 的 `grade=` 錨定式（偽造的拒因表不
  成立、真的拒因表照常成立）；`_invoke_json` 的節錄以 #674 的 `stdout_excerpt()` 實際
  產生（不自造字串），另一條釘住 stderr 內容永不進診斷。既有測試**一行未改**。
