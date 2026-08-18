# 682-planner-rejection-table

- **#682（#672 票 A）：planner 失敗的錯誤語意三分 ＋ `no-heterogeneous-planner` 攜帶逐候選
  拒因表**——修法前 `select_secondary_planner()`（`model_identities.py`）失敗時只回一個
  沒有任何附加資訊的字面值 `no-heterogeneous-planner`，而迴圈裡四個 `continue`（同 domain／
  probe 缺席／probe 沒 ready／probe 身分不符）**全部靜默**，於是三類結構上完全不同的失敗
  （job／executor 起不來、executor 異常退出、輸出不合約）被壓成同一個「拓撲問題」。
  #670 就是這樣被誤診的：真因是 `agy models` 兩欄漂移造成 100% `model-not-listed`
  ＋ code fence 造成 parse 失敗，blocking reason 說的卻是「沒有異質 planner」，排查方向
  整個帶偏，最後靠人工重跑六遍才看出來。修法四件：
  - **三分的具名族**（`PLANNING_FAILURE_JOB_START`／`PLANNING_FAILURE_EXECUTOR`／
    `PLANNING_FAILURE_OUTPUT` ＋ `executor-silent-exit` 子類 ＋ fail-closed 的
    `planning-probe-unclassified`）與 `classify_probe_failure()` 的**單一明表**；票 C
    （probe 快取）與票 E（`JobPlanningInvoker`）直接消費同一組常數，不再各自發明。
    `probe_agy_capability` 的非零退出改帶 `_exit_diagnostic()`：rc≠0 且 stdout／stderr
    皆空時就地標記 `executor-silent-exit`（**stderr 內容本身不入 diagnostic**）。
  - **逐候選拒因表**：`CandidateRejection` dataclass ＋ `SecondarySelection.rejections`，
    四個 `continue` 各記一筆。`SecondarySelection.reason` **刻意維持原字面值**——它是下游
    既有的機器判準，拒因表走新欄位而不是把那個字串改長。
  - **渲染進 blocking reason**：`run_heterogeneous_brainstorm` 經
    `render_secondary_rejection_reason()` 產出
    `no-heterogeneous-planner grade=<environment|content> candidates=<N> (<逐條>)`，
    可用正規表示式釘住。PR #674 補的 probe stdout 節錄（`stdout_excerpt`）自此**端到端
    活著抵達** blocking reason，不再被 `continue` 吃掉——兩票的接縫由測試釘住。
    截斷策略：身分列（executor／model_id／domain ＋ 拒因 ＋ 族名）**永不被截、永不被整列
    丟棄**；只有 diagnostic 會被截，單條超限就地記帳 `…+Nc`，全表超預算時從最長的
    diagnostic 開始整格換成 `<detail-elided:Nc>`——「哪一條被截掉、少了多少」永遠讀得出來。
  - **分類改判**：`manager._classify_planning_failure()` 增第四條 environment 例外
    （比照 #416／#533／#554 三條既有例外的同一個模式）——拒因表含 environment 級拒因時
    整體改判 `environment`，`_resume_decision` 因而得以浮現 `recover-planning`；全部是
    拓撲／格式級拒因時仍為 `content`（反向誤報同樣不可接受）。判準讀的是渲染端算好、
    **錨在字串開頭**的 `grade=` 欄位，**不對整串 reason 做 substring-search**：拒因表的
    diagnostic 帶的是模型輸出，一個回「planning-executor-failed」的模型否則就能把 content
    失敗偽裝成 environment。
  - **機密面**：`CandidateRejection` 六個欄位沒有任何一個接得到 env、argv、檔案內容或
    stderr；自由文字入口只有 probe 的 `reason`／`diagnostic`，其最寬的來源是模型對一段
    **固定 probe prompt**（只含兩個常數）的 stdout 節錄與例外**型別名**
    （`type(exc).__name__`，不含訊息）。渲染時另剝除 C0／C1 控制字元並單行化，避免 ANSI
    escape 或換行污染單行 log。
  - 測試 `tests/test_planning_failure_taxonomy_672.py`（30 條）；**既有測試一行未改**。
