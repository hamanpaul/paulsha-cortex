# D4：monitor 事件入口（spool）＋targeted refresh——事件是 hint，不是 authority

## 問題

D1–D3 把 monitor 對 GitHub 的常態讀取壓到每 repo 每日 26 次計費請求，代價是**發現
延遲**：一件事發生在 GitHub 上，最壞要等一個 refresh 週期（預設 300s，實務上更久）
才進鏡像。對「fleet 自己剛剛動過的物件」而言，這個延遲是白等的——動手的是我們自己，
當下就知道哪個物件被動了，卻還是只能等下一次輪詢把整個清單再問一遍。

## 改法

新增 `paulsha_cortex/monitor/event_spool.py`，是本機事件入口的契約與唯一入口。D5
（headless agent hook，**不在本次**）會依這份契約把「我剛動了 GitHub 物件」寫進
spool；monitor 每輪消費它，對被點名的物件做 targeted 條件驗證後才更新鏡像。

### spool 契約（D5 依此實作）

- **位置**：`monitor_event_spool_root()`（預設 `<agents>/monitor/event-spool/`，隨
  `PSC_MONITOR_STATE_ROOT`／`PSC_AGENTS_ROOT` 移動）；壞事件檔隔離到同層
  `quarantine/`。目錄由**寫入端**建立——monitor 掃到目錄不存在就是「這台機器沒有
  hook」，不是錯誤，也不會替它建目錄。
- **每事件一檔**：`<emitted_at 壓平>-<event_id 前綴>.json`，因此消費就是 per-file
  `unlink`，不需要鎖、offset 檔或任何跨行程協調。檔名全程過濾成 `[A-Za-z0-9._-]`，
  事件內容無法影響它落在哪個路徑。
- **原子寫入**：temp 檔（`.` 前綴，掃描端跳過 dotfile）→ fsync → `os.replace`；
  消費端因此**不可能**讀到半寫入的檔案。權限 0600。
- **fire-and-forget 寫入端語意**：`EventSpool.emit()` 不等回應、不與 monitor 交握、
  **永不 raise**——寫失敗只回 `None` 並記 debug log。hook 掛在別人（agent job）的
  工作路徑上，spool 寫不進去絕不能影響工作本體；掉一則 hint 的後果只是退回原本的
  refresh 週期延遲，而那正是 D3 每日 anti-entropy 的守備範圍。
- **信封欄位**（缺一即壞檔）：`schema_version`／`event_id`／`event_type`／
  `emitted_at`／`source`；選配 `job_id`（D5 的 `PSC_JOB_ID` 自守標記）與 `payload`。
- **hint 不是 authority**：`github_object` 事件只帶「哪個 repo 的哪個編號被動了」，
  **契約層就不給 producer 塞新狀態的欄位**；`action` 純屬診斷，consumer 永不據以
  寫鏡像。對應 `correlation` 既有的 inferred→confirmed 語彙：spool hint 是 inferred
  訊號，只有 targeted 驗證回來的物件才是 confirmed、才進鏡像。

### 消費端（`GitHubWorkProvider`）

D3 的清單同步跑完之後才消費 spool——先做便宜的批次讀取，被它涵蓋到的事件就不必
再各花一次請求。

- **targeted 條件請求**：單物件 `repos/{repo}/issues/{number}`（PR 走同一端點），
  沿用 D3 的 ETag／計費紀律。per-object ETag 存進 `IssueSyncState.targeted_etags`，
  與清單端點的 `etag` **分開存**：兩者 request path 不同，混用會讓條件請求永遠
  落空。這條 path 不含 `since`，因此游標前進不會讓它作廢；304 一路**不**取回應的
  ETag（與 D3 同一顆地雷）。
- **游標不受 targeted 影響**：targeted 讀回來的新狀態**不得**推進 `since`——游標
  只能由清單回應推進，否則會跳過那之間被更新的其他物件。
- **順序與去重**：同物件多事件收斂成**一次**驗證，所有貢獻事件檔一起消費（否則
  下輪重驗）。事件之間本來就沒有全域順序，consumer 也不推論順序——每個物件只問
  GitHub「你現在長怎樣」，答案與事件先後無關。
- **過期安全跳過**：事件早於本輪請求、且該物件已被本輪讀取涵蓋（出現在增量 delta，
  或本輪是全量），鏡像就已經至少和事件一樣新——直接消費事件、**不花請求**。spool
  是本機目錄，producer 與 consumer 共用同一顆時鐘，這個時間比較才成立；GitHub 端的
  replication lag 是唯一殘餘風險，而那本來就歸每日 anti-entropy。清單回 304 **不算**
  一次讀取（它什麼都沒讀回來），不得算進涵蓋範圍。
- **處理成功才消費**：事件檔一路留到鏡像真的落地（state 存檔成功）為止。中途 crash
  的代價只是下一輪重驗一次，而條件請求命中 304 連配額都不花。
- **fail safe**：targeted 請求失敗／回壞 JSON／回的不是問的那個物件——一律不寫鏡像
  **也不消費事件**；回 404 則不從鏡像刪任何東西（刪除／transfer 只有每日全量對帳
  看得到，一次 404 不足以當證據），但消費該事件以免無限重試燒配額。第一個 targeted
  失敗即停掉本輪其餘 targeted 請求（多半是限流／認證，繼續打只會把退避窗撐更深）。
- **壞檔隔離不阻塞**：JSON 壞掉、信封缺欄位、payload 形狀不合、超過 TTL 的孤兒事件
  ——一律移進 `quarantine/`（隔離而非刪除：那是要給人看的），同一輪其餘事件照常處理。
- **記帳**：`observations["event_spool"]`（未接 spool 時整個鍵不出現）記
  pending／objects／superseded／verified／confirmed／not_modified／unverified／
  requests／billed_requests／consumed／deferred／quarantined／ignored。
- **per-cycle 上限**：預設一輪最多驗 20 個物件。hook 是 per-tool-call 觸發的，沒有
  上限等於把 D1–D3 省下的配額交還給事件量決定；超出的事件留在 spool，下一輪依
  `emitted_at` 先來先服務。

## #498 擴充點

`event_type` 是封閉列舉的擴充位。本次只消費 `github_object`；`steering`／`job`
（#498 的 remote-control 佇列與 job 心跳）已在 `RESERVED_EVENT_TYPES` 佔位，掃到時
**原地保留、只記 log 與計數，絕不刪除**——那些事件屬於未來的另一個 consumer，這裡
刪掉就是替它們決定生命週期。同理，未知 `event_type` 與未知 `schema_version`（較新的
producer 對上較舊的 consumer）一律保留不動，只有**結構壞掉**的檔案才會被隔離。
#498 落地時只需新增一個消費該型別的 consumer，spool 契約、原子寫入與隔離機制照用。

## 範圍

只做 monitor 側的 spool 消費機制與契約。**D5 的 hook 注入不在本次**，launcher 未動；
沒有 spool 目錄時 provider 行為與 D3 逐位元組相同（事件入口是加速器，不是任何東西的
必要條件）。寫入路徑、label API、events API 均不在本次。

新增 `tests/test_monitor_event_spool_506.py`（51 個測試，涵蓋 spool 寫入／消費／
去重／亂序／過期跳過／壞檔隔離／targeted 驗證失敗 fail-safe，全程不打真實 GitHub
API）。
