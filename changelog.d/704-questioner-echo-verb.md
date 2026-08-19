# 704-questioner-echo-verb

- **#704：questioner 的 prompt 要模型「產出所需題目」，驗證卻要求逐位元 echo——模型
  合理地創作，於是 define 擲骰子**。`validate_question_pack()` 的最後一關是整份
  `to_dict()` 等於 `report.default_question_pack`，而那份 pack **已經**併在 questioner
  的輸入裡；也就是說這個呼叫的任務只有謄寫，沒有任何創作空間。舊 prompt 卻寫
  `Return only the exact question-pack JSON **required to resolve** this completeness
  report.`——「所需的」是創作型動詞。實機（#701／PR #702 的逐欄診斷落地後第一次跑）抓到
  的正是這件事：模型把通用模板題目**特化到 work item** 並追加約 599 字
  （`questions[0].prompt`：`…an accepted spec?` → `…an accepted spec for fixing
  read-repo tier fail-closed behavior? Please …`）。**以任何一般標準看模型做對了事
  ——是指令錯了。** 這是 `#406`／`#516`／`#520` 同一教訓的第四輪，而且是最直接的一種：
  指令的動詞就是錯的。
- **動詞改對，且約束句由判準機械產生**（比照 `#520` 的 `required_heading_hint()`）。
  新增 `planning.question_pack_echo_hint()`，句子裡的欄位名全部由型別自身導出：
  `QUESTION_PACK_KEYS`（＝`QuestionPack.to_dict()` 的頂層鍵）與 `QUESTION_FIELDS`
  （＝`PlanningQuestion` 的 dataclass 欄位），指路用的輸入鍵名走
  `QUESTIONER_INPUT_PACK_KEY`——`run_heterogeneous_brainstorm()` 組輸入時用的就是同一個
  常數。**prompt 端不再持有第二份真實來源**：欄位改名時驗證與 prompt 一起改。三個常數
  同時是 `validate_question_pack()` 的 extras 檢查與
  `describe_question_pack_difference()` 的掃描順序來源，不是為 prompt 另開的一份表。
- **同型掃描：另外三個 adapter 逐條查過**（`#679` 修 PATH 沒看隔壁 HOME、代價是 `#692`）。
  - `invoke_primary`——**不改**。它沒有自己的 prompt 文字，只是 questioner／integrator
    共用的傳輸層（identity ＋ purpose ＋ 逾時），沒有任何動詞可言。
  - `secondary`——**改**。`question_pack_id` 與每列 `question_id` 都是 echo-back 欄位
    （`validate_secondary_evidence()` 對前者逐位元 `!=` 直接拒、對後者要求落在 pack 的
    識別碼集合內），值也全部已在輸入裡；但修法前兩者**只被列了欄位名**——`#516` 為
    integrator 補的那層語意，secondary 從來沒有拿到。`claims`／`source_refs` 維持創作型
    動詞：那兩欄本來就該由模型自己寫，對它們用抄寫型動詞才是新的自相矛盾。
  - `integrator`——**改一半**。`#516` 補的 `question_pack_id`／`secondary_evidence_hash`
    兩句本來就對，但 `resolutions[].question_id` 同樣是 echo-back、同樣只被列了欄位名。
    `question_pack_id` 那句改由新的 `planning.echoed_identifier_hint()` 產生（欄位名同源），
    順帶補上 `question_id`；`secondary_evidence_hash` 的專屬句與 `#520` 的
    `required_heading_hint()` 逐字保留。
- **判準本身逐位元不動**。什麼算合法、什麼算不合法完全沒變；本票只改指令的動詞。
- **實機驗證**（真實 reviewer unit 加固面，`psc_run_under`／
  `permgen.unit_replica_properties()` 全量導出 40 條 property，不自組 `--property=`、
  不自帶 `--setenv=PATH=`；argv 由 `_planning_argv()` 產生、判準由 `validate_question_pack()`
  判定）：新舊 prompt 各 8 次 × 兩種 fixture × 兩個模型的逐位元 MATCH 次數見 PR body。
  **對照組（舊 prompt）在本機重現不到實機的 MISMATCH**——這與 `#701` 開票前的外部 12/12
  MATCH 一致，照實記錄，不調整次數。
- **測試**：`tests/test_planning_completeness.py` 兩條（約束句由判準機械產生：判準比對的
  每個鍵／欄位都被點名、指路鍵名與輸入鍵名同源，且「prompt 禁止的那件事」＝「驗證真的
  會拒的那件事」，以實機那個特化尾巴為 fixture）；`tests/test_planning_runtime.py` 兩條
  （questioner prompt 不得再含 `required to resolve`、必須含機械產生的整句；secondary
  prompt 必須含識別碼約束句，且 `claims` 仍是創作型）。**既有測試一行未改。**
