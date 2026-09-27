# #1097 qualification clock watermark 補上其他 durable 系統時間證據

`qualification_lifecycle` 的 `clock_watermarks` 只在「某次查詢或寫入」實際觀測到 `now >= expires_at`
後才落盤；若資格已在真實時間過期，但**第一次 qualification 查詢就發生在系統時鐘回撥之後**（回撥前完全
沒有任何 qualification 觀測），watermark 無從得知真實時間已經越過 `expires_at`，查詢仍可能誤判為
`approved`。新增 `_clock_evidence_floor()`：讀取兩個 Trust Root 登記、Manager-only、帶時間戳的 durable
落盤來源——寫入頻率最高的 `jobs-registry`（`<coordinator_root>/jobs.json`，幾乎每次派工或狀態轉移都會
重寫）與 writer 僅 `Principal.MANAGER` 的 `quota-admission-decisions`（append-only 准入決策 receipt）——
取兩者最新 mtime 的較大值，作為 `now` 的下界（只會墊高、不會壓低）；只做單一 `os.stat`，不開檔、不解析
內容，成本與檔案大小無關。當這個下界已經越過 `expires_at`、但呼叫端傳入的（回撥後）`now` 本身還沒到，
查詢改以 `reason="clock-evidence-expired"` fail-closed。任何來源不存在、不可讀、或不是一般檔案都視為
「這個來源沒有證據」而跳過，絕不因此讓查詢丟例外；完全沒有可用證據時行為與 #1097 之前一致，並在查詢結果
多帶一個可機讀的 `clock_evidence` 診斷欄位（`clock-evidence-checked` / `clock-evidence-unavailable`），
明示這次判定有沒有第二層佐證。既有的 per-binding watermark 回退判定不受影響，仍用原始 `now` 比對。
