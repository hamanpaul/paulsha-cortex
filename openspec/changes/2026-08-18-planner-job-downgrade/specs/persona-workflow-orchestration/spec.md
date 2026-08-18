---
status: accepted
work_item: planner-job-downgrade
---

## MODIFIED Requirements

### Requirement: 不完整規格必須經異質雙模型brainstorm

Artifact只有在frontmatter `status: accepted`、必要章節存在且沒有blocking decision marker時才算accepted。Marker parser MUST只把獨立行`TBD`、`[TBD]`、`Decision: TBD`、`決策：未定`或Open Questions中的實際項目視為blocking，MUST忽略inline說明與fenced code。Accepted spec/design/plan缺失或有blocking marker時，primary planner MUST先產question pack；secondary planner MUST來自不同independence domain且只回evidence；primary MUST整合並落檔。Secondary選擇 MUST依三層解析鏈（operator overlay → 評估合格清單 → packaged fallback）排除primary domain；無異質model、unknown identity或malformed output MUST fail-closed。

Planner subprocess與manifest plan card MUST只在temporary disposable checkout執行，並以read-only executor模式啟動；Claude MUST使用停用tools的模式、Codex MUST使用`--sandbox read-only`。**降權啟動器啟用時（`PSC_JOB_RUNNER` ∈ {`systemd-run`, `systemd-template`}），planner的每一次模型呼叫與每一次capability probe MUST經job runner以`review`角色的root-owned模板unit執行，MUST NOT在Manager行程內直接執行任何executor argv**；角色MUST由呼叫端建構期固定，MUST NOT從prompt、job spec或模型輸出導出。降權模式下，「planner不得寫operator工作樹」這條性質MUST由unit的`ProtectSystem=strict`與不含來源樹的`ReadWritePaths=`在mount層保證，而非僅由Manager的前後快照比對；「planner不得弄髒自己的拋棄式sandbox」這條 MUST NOT 由job自身產生的證據滿足。`PSC_JOB_RUNNER=direct`時，現行的sandbox複製、前後兩次tree snapshot、drift收容與逐路徑還原三道fail-closed閘門 MUST 逐字保留。

Manager MUST在成功、nonzero或exception路徑驗sandbox與operator worktree的檔案、empty dirs、directory symlinks與stable metadata（direct模式）；snapshot遇權限錯誤也 MUST先恢復安全traversal再依baseline還原entries、mode與xattrs，restore fault MUST fail-closed。Primary只回傳structured artifact content；Manager MUST在scan時持久化canonical ref、kind、work item與content hash authority，replacement MUST逐欄符合該authority及manifest outputs，不得信任caller hash或filename推測。新檔 MUST no-clobber。Artifact、immutable或既存同內容brainstorm evidence、expected gate ref與registry phase update MUST由durable intent journal形成recoverable transaction；registry未commit的save fault MUST rollback，已commit的restart/resume MUST逐operation重驗type/hash/mode/evidence後保留產物，drift MUST設`needs_human`並保留journal，且不得覆蓋其他work item。

`no-heterogeneous-planner` MUST NOT 是一個不帶附加資訊的字面值：selection MUST 同時攜帶**逐候選拒因**（每個planning-capable identity為什麼落選：same-domain／probe-absent／probe-not-ready／probe-identity-mismatch，以及probe側的實際診斷），且brainstorm的blocking reason MUST 渲染該表。拒因中含環境級原因（job起不來、executor異常退出、probe快取不可讀）時，planning failure的classification MUST 判為`environment`而非`content`，使`recover-planning`可浮現。

#### Scenario: 降權模式下的planner呼叫

- **WHEN** `PSC_JOB_RUNNER=systemd-template`且Manager觸發define／brainstorm
- **THEN** 每一次模型呼叫落成一個root-owned模板unit的實例，執行身分由unit檔的`User=`決定
- **AND** Manager帳號的行程樹內不出現任何executor可執行檔

#### Scenario: direct模式回歸

- **WHEN** `PSC_JOB_RUNNER`未設或為`direct`
- **THEN** planning的sandbox複製、前後tree snapshot與drift收容行為逐字不變

#### Scenario: Agy可用且primary非Google

- **WHEN** completeness gate觸發且agy live capability/identity probe通過
- **THEN** secondary使用Google domain回傳evidence
- **THEN** primary負責final decisions與artifact write

#### Scenario: 只剩same-domain model

- **WHEN** 所有可用secondary都與primary同domain
- **THEN** WorkflowRun設needs_human且不進build
- **AND** blocking reason的拒因表對每個候選逐一標為`same-domain`，classification維持`content`

#### Scenario: probe輸出被code fence包住

- **WHEN** 某候選的probe stdout是被markdown code fence包住的正確JSON
- **THEN** blocking reason MUST 指出該候選是`probe-not-ready`且診斷為輸出格式問題，並帶出stdout前綴
- **AND** MUST NOT 只呈現`no-heterogeneous-planner`而讓格式問題被讀成拓撲問題

#### Scenario: executor在job unit下靜默非零退出

- **WHEN** job成功啟動但executor回非零且stdout與stderr皆空
- **THEN** 失敗 MUST 落`planning-executor-failed`並標記`executor-silent-exit`子類，classification為`environment`
- **AND** reason MUST 指名unit名、加固剖面與實際解析到的executor絕對路徑
- **AND** 診斷 MUST 帶「該加固面下被過濾的syscall是否致命」的機械答案，使「要不要懷疑seccomp」不必再靠猜

## ADDED Requirements

### Requirement: Planner capability probe MUST跨tick快取，快取判準MUST涵蓋執行後端且MUST fail-closed

Planning runtime的建構 MUST NOT 在每次建構時重跑全部capability probe。probe結果 MUST 落成Manager-owned的durable快取，其資產 MUST 登記在trust-root登記表、writers與readers只有Manager principal，且 MUST NOT 出現在任何降權job模板unit的`ReadWritePaths=`中。

快取判準 MUST 至少涵蓋：`(executor, model_id)`、降權啟動器模式（`PSC_JOB_RUNNER`的值）、以該角色PATH解析出的executor可執行檔絕對路徑與其inode指紋、該帳號憑證檔的大小與mtime指紋、加固剖面名與**模板unit檔本身**的指紋、以及model identity roster解析結果的內容雜湊。其中「降權啟動器模式」為硬性項：direct模式取得的probe結論 MUST NOT 被降權模式採信，反之亦然。

快取檔不存在、無法解析、schema版本不符或指紋不符時 MUST 一律視為miss並重探；重探失敗 MUST 判為not ready。系統 MUST NOT 因為快取中的前一次結論為ready，而在無法重探時沿用ready。快取 MUST 同時保存失敗側的完整診斷（reason、diagnostic、退出碼、stdout前綴、unit名、加固剖面、解析到的可執行檔與版本字串），供blocking reason的拒因表消費。

#### Scenario: 模板unit換版後的重探

- **WHEN** operator重跑權限產生器並落下新的job模板unit檔
- **THEN** 全部probe快取因模板unit指紋改變而失效並重探
- **AND** 不需要任何手動清快取的步驟

#### Scenario: 快取檔損毀

- **WHEN** 快取JSON無法解析
- **THEN** 視為miss並重探，MUST NOT 沿用任何舊結論
- **AND** 落一筆可與「probe失敗」區分的結構化診斷

#### Scenario: 跨執行後端不得互相採信

- **WHEN** 部署由`PSC_JOB_RUNNER=direct`切換為`systemd-template`
- **THEN** 全部probe快取失效，切換後的第一輪planning重新探測

### Requirement: 降權planning job的加固剖面MUST由executor單一判定，逾時MUST由Manager強制終止

降權planning job的加固剖面 MUST 由既有的「executor → 剖面」單一判定點取得，其唯一輸入為Manager在dispatch當下決定的executor；未登記的executor MUST fail-closed，MUST NOT 落到較寬鬆的剖面。planning路徑 MUST NOT 自帶第二份executor→剖面對應表。

降權planning job MUST 有Manager側強制的執行上限：等待逾時後 MUST 主動停止該unit並確認其離開active狀態，並落一個與「job起不來」「executor異常退出」「輸出不合約」三族可區分的逾時原因，classification為`environment`。系統 MUST NOT 僅放棄等待而讓job繼續執行。

任何形如「某executor在某加固剖面下可用**或不可用**」的宣稱，其驗證環境 MUST 由**已落檔unit機械讀出全部property**再複製，MUST NOT 手抄property子集當作真實加固面的複本，亦 MUST NOT 自行組裝property清單——加固面複本 MUST 由既有的全量導出機制產生，且該機制在落檔unit缺任一加固鍵時 MUST fail-closed（產出為空而非降級）。此要求**雙向適用**：手抄得比production寬會產生假綠（宣稱可用而實機不可用），手抄得比production嚴會產生假紅（宣稱不可用而實機其實可用），兩者皆 MUST 被擋。

驗證環境若帶`SystemCallFilter=`，MUST 一併帶入`SystemCallErrorNumber=`——只帶前者會把「被過濾的syscall回`EPERM`、呼叫方可fallback」偷換成「行程直接被殺」，而兩者的可觀測症狀（空輸出）相同。syscall過濾語意 MUST 被視為加固剖面**之外的第二個維度**：兩份剖面在該組鍵上逐字相同，因此任何以「剖面名」為索引的判斷（含probe快取判準）MUST NOT 只憑剖面名，MUST 涵蓋落檔unit本身。

矩陣每一格 MUST 記錄退出碼、輸出前綴，以及**實際解析到的可執行檔絕對路徑與版本字串**；最後一項是必要的，因為「解析到非預期版本」不會表現為失敗，只會安靜地產出來自另一份CLI的結果。

#### Scenario: 未登記的executor

- **WHEN** planning以一個不在剖面表上的executor派出
- **THEN** 派工在任何per-job產物產生之前fail-closed
- **AND** MUST NOT 落到放寬的剖面

#### Scenario: 模型呼叫逾時

- **WHEN** 降權planning job超過Manager設定的上限仍未結束
- **THEN** Manager停止該unit並確認其離開active
- **AND** 下一次同角色的planning呼叫 MUST NOT 因殘留的active實例而失敗

#### Scenario: 以property子集複本取得的宣稱

- **WHEN** 驗收僅以帶部分property的transient unit執行
- **THEN** 該結果 MUST NOT 被當作「可用」的證據
- **AND** 同樣 MUST NOT 被當作「不可用」的證據——偏嚴的複本會產生假紅，而假紅會導致去放寬一條實際有效的加固項

#### Scenario: 驗證環境帶了syscall過濾卻沒帶錯誤碼設定

- **WHEN** 驗證環境宣告`SystemCallFilter=`而未宣告`SystemCallErrorNumber=`
- **THEN** 該環境 MUST NOT 被視為production加固面的複本
- **AND** 由它取得的任何可用性結論 MUST 重驗

#### Scenario: executor解析到非預期版本

- **WHEN** 驗證矩陣中某格rc=0，但解析到的可執行檔不是部署toolchain中的那一份
- **THEN** 該格 MUST NOT 記為通過
- **AND** 矩陣 MUST 呈現實際解析到的絕對路徑與版本字串，使差異可見
