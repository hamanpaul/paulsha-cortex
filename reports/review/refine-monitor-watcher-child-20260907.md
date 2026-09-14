# Watcher-only A child intake：邊界、驗收與 gate

日期2026-09-07。隔離authoring branch `feature/refine-monitor-watch-child-20260907`；當時local `origin/main`基底 `fb31083e7325be86c6c9d217da56b9c4d799f5a5`。先唯讀inventory確認operator已有其他變更、target branch/worktree不存在，再建立新worktree。當時未pull/fetch或改operator/runtime/live；作者只新增兩份spec/design、todo及此report，未commit/push、開issue、註冊`.cortex`、建立run或派模型/產品code。這是authoring當時紀錄；root後續已建立owner issue如下，不把歷史敘述當成目前Git/GitHub狀態。

**最新狀態：owner [#853](https://github.com/hamanpaul/paulsha-cortex/issues/853) OPEN；本PR repository intake／unfrozen，R4內容PASS／0 MAJOR，仍8/Red禁派。** Root已查重後建立/readback本child，parent #827、umbrella #829，work item `monitor-watcher-bounded`；本PR已加入唯一`.cortex` spec/design/todo links，未merge、未正式freeze、未dispatch，不等於已凍結的run authority。R4結果由root轉交fresh reviewer `watcher_polling_r4`，僅規劃內容審查，不是產品GREEN或live驗收。Root採納只重讀final state的consumer證據及native復原擴面成本，選watcher.py內BaseObserver＋小PollingEmitter adapter＋直接root-coalescing ingress；R3再准許窄化per-scan dirfd nonfollow VFS與failure latch，沒有複製watchdog engine。原K-ring與native待選方案均撤回，raw backlog=0契約保留。W1–W12產品任務仍全未勾，不用helper ready、開票或repository links宣稱交付。未來canonical build branch=`feature/853-monitor-watcher-bounded`，fragment=`changelog.d/monitor-watcher-bounded.md`，不是authoring／本次planning整合branch或已建立的產品產物。

## Authoring 方法與適用邊界

原author時點依doc-coauthoring使用既定受眾/accepted模板/邊界直接起草；test-playbook將actor、state/side-effect owner、非同步race轉成Event/barrier/fake-clock與真consumer oracle。當時root禁止作者派模型，故未由本作者啟fresh-reader；文件閱讀與adversarial檢查交回主流程獨立review，未冒稱自批PASS。以下source/fixture、四檔scope與未commit／未CI敘述均屬當時紀錄；本次整合狀態與root轉交reader結果另見末節。

本項只交付單一backend設計的intake候選，產品W1–W12未實作。A與work-model B可各自驗收，最後仍由C整合service與母#827全AC。基線是fb31083e父accepted三件組，不把原M1–M5候選表當已完成自動分拆；#833尚未提供本項自動child authority。

已取回 `patchmud_issue` agent的既有最終A∥B→C分析（透過agent結果，沒有另跑現場）：A唯一watcher.py；B唯一work_api.py，負責provider I/O/commit隔離、source粒度generation/revision與latest-state合併、last-good/owner/error、正常production路徑的commit fence；C唯一service.py，依A/B已交付契約統一全來源排程及durable/server publication停止線性化。其殘餘一併保留：scan有限L仍需證明、snapshot store也持鎖掃描、correlation若需改輸入快照就是額外production模組、診斷若改頂層durable schema要重裁scope。C須合計watcher與refresh pools，不能各池各稱W而漏算實際refresh並行數。

## Source 錨點與原因

基底fb31083e的 `paulsha_cortex/monitor/watcher.py:117–137` 每事件Timer/latest_path；`:150–163` src/dest首match即return；`:165–169` cancel只取消Timer；`:209–235` unwatch/stop沒有join callback worker契約。`WatcherCallback`於`:17`仍是Path通知。

`monitor/service.py:155–201`安裝workspace/project watches，`:280–284`未映射Path走全量refresh，`:301–315`project Path走project；故directory root invalidation有現成consumer seam可驗多project覆蓋。這只是目前source佐證，跨path完整性由spec T3/T4運行不變量驗收，不以靜態掃描作「不存在其他consumer」全稱背書。

`service.py:317–330`仍有第二層Timer；A不修它，亦不解決work_api provider global lock/late durable publication。既有watchdog真測試位於 `tests/test_stage9_project_monitor_service.py:1147–1210`，但尚不足以證明高churn/stop races。

## Parent R1–R8 mapping

| Parent | A可獨立驗收 | 仍由B/C與母票持有 |
|---|---|---|
| R1有界owner | W1/W10/W12，callback W＋固定backend B=1、bounded intents/註冊槽與全部採用事件暫存 | service第二Timer/未映射scan、poll/rescan/GitHub統一排程與各池加總；W12不可向母票卸責 |
| R2 coalesce | W2/W3/W12，raw0直接watch-root coverage、每generation唯一pending/ready與D | project/source最終snapshot coalesce與服務層batch/fullscan合併 |
| R3公平 | W4，watcher ready FIFO在有限callback L前提下的界 | 全來源公平、慢provider鎖外I/O與無飢餓整合 |
| R4 timeout/last-good | W6/W7僅callback/backend收尾與poll snapshot暫時OSError保活 | work provider timeout、last-good/authority/source revision與晚到結果 |
| R5診斷隔離 | W7/W10/W12，revoked/drained、poll scan health/P/Tscan、callback/backend/pending/ready/stop-latch counts | 完整snapshot/read-model相容、per-source freshness及錯誤合併 |
| R6 stop fence | W5/W6/W9/W11/W12 admission/pending/generation/completion、共享backend保護、獨立stop latch與unfinished0 | consumer已開始event/durable commit線性化publication fence |
| R7有界收尾 | W6/W9/W10，非阻塞revoke、總S drain、不循環等鎖、backend tracked residual | service/provider/adapter cooperation、安全上下文drain接線與整體shutdown |
| R8可重現 | W8/T1–T15，fake-clock/barrier/真consumer鎖/真BaseObserver純fixture、descendant failure/nonfollow VFS與所選OS backend另驗 | 真CLI/全鏈路服務公平/publication/部署負载最終gate（A仍做自身CLI相容） |

## Truthful sizing 與純 gate

domain_breadth=0：唯一production watcher.py；state_consistency=2：並行atomicity/lifecycle，不降成純函式。invariant_count=12對應W1–W12；artifact_classes=source/tests/documentation。R3補強原W2範圍/W7健康契約並增T15反例面，未降低數值或另藏模組；新的dirfd stat/listdir adapter與failure latch仍在watcher.py，重用DirectorySnapshot的公開VFS及原walk/diff/poll loop，不改套件。固定完整fix-standard為0/2+2/2/2=8/Red；#831真部署且完整accepted後才條件式6/Yellow。若實作需要第二production仍須回root重評，不能以文件accepted遮過scope差異。

R4審查版於`2026-09-07T13:42:53.006278+00:00`在checkout外`/tmp`設`PYTHONDONTWRITEBYTECODE=1`，import當時runtime checkout（HEAD=`79ba644780bf1c697c722ac24a297e7d02416100`）的planning/manager純函式。三件組逐件accepted=True、complete=True、missing_kinds=[]、reasons=[]、blocking_markers=[]。真score=`domain0/state2/acceptance2/stability2/orchestration2`，total=8、band=red；不是派工許可。其後author #853 owner metadata版pure/hash與本次repository-intake metadata版hash分節記錄，不混為同一版或root planning preflight結果。

真packaged fix-standard：gate_spine_count=2、cards_count=9、persona_binding_count=9；規則完整R-09/R-16/R-19。`_evaluate_yellow_plan_review`回ready=True、checks_run=(completeness,contract_compatibility,envelope)，真identity lookup得到`bypass=envelope_unavailable`，不是能力量測PASS、不覆盖Red。記憶體run僅steps=()/primary_domain=None/model_chain_override=None/sizing_band=red，未寫registry/執行model。此helper的completeness只找task surfaces（planning.py:526–543），不能單獨證明三件組accepted或設計正確；本次另實跑三件組完整性。

重跑鏈：三個真檔→PlanningArtifact(spec/design/plan)→assess_planning_completeness→compute_sizing_score（真combo/完整規則）→claim.sizing_band→Yellow helper。identity讀取不輸出credentials。歷史10:19/10:42曾accepted/8Red，第二輪發現queue缺口後11:05改draft/complete=False；當時反向stability卻算6Yellow、helper仍ready，已明記NO-GO，沒有依它派工。以下保留R4所review、加入#853 owner/status之前的歷史三件組hash，不是目前intake metadata版hash或正式authority/evidence receipt；後續兩版hash於本report後方分列。

| Artifact | SHA-256 |
|---|---|
| `docs/superpowers/specs/monitor-watcher-bounded-spec.md` | `8e92009a650f5ade491d2b3d3c9416538958420e656f4913c07d1de0c61c6482` |
| `docs/superpowers/specs/monitor-watcher-bounded-design.md` | `c6ca2c9836ac53446f34cea8ec879e5ac6b940a787989e384681644b399e8a5f` |
| `docs/superpowers/workstreams/monitor-watcher-bounded/todo.md` | `c716f829cd1b3cf47a2f5b186010de9bdebb0ddf621214fca123d722e041add3` |

原author時點沒有執行產品test、CLI smoke、CI/policy或live驗收，也未執行commit/push；當時四份新文件的whitespace以逐檔no-index檢查驗證，不能以普通`git diff --check`忽略untracked檔當唯一證據。當時作者scope inventory僅四檔、authoring base未變；不宣稱本次root整合PR仍只有四檔或沒有planning preflight／CI。todo的未來產品gate仍待Cortex交付，root planning檢查不代替它們。

## 離線可執行驗收設計

候選產品測試命令：`python3 -m pytest -q tests/test_monitor_watcher_bounded.py tests/test_stage9_project_monitor_service.py`，再 `python3 -m pytest -q`。新tests檔由Cortex依todo建立，不是authoring已存在的測試證據。fixture保證network/model launcher禁用、watchdog dependency存在、fake Observer event→production admission→real worker callback，必要時真service+fake store。

T1/T13在W callback barrier已取得lease時注入100事件，全部put返回才放gate，驗raw0與首輪+最多一次補跑；T2假clock驗D/FIFO；T3/T4驗多root/rename/真consumer最終store；T5/T6驗revoke、self-call、exception；T7驗非合作callback總S drain；T8固定seed虛擬60秒另真OS smoke；T9真service lock負例；T10持Observer鎖等A cleanup時健康B仍可ingress/通知；T11真BaseObserver+fake emitter shared/generation不是OS驗證；T12 late schedule補償。T13另驗stop facade無data get/unfinished累積/closed busy-spin，10k/100k retained-memory plateau。T14新增child stat、recursive listdir及iterator中途失敗，驗完整性latch/last-good與無假刪除；T15區分memory nonfollow、syscall spy與真OS external/cycle/symlink swap/root更換/fd回收/平台缺能力。所有產品cases未執行，wait均有限/finally清理。

## 獨立 review：三 MAJOR 採納與證據來源

首版review結果FAIL，三項均採納，不當false positive或provider residual。Reviewer `refine_plan_review` 提供的fixture全部是stdin純記憶體，**沒有fixture檔案路徑**；root另以相同真Python/watchdog完整重跑確認。作者當時只讀service與installed watchdog上述source/這些既有結果來修文件，未把它說成新產品GREEN。當時修後待主流程重審，不沿用首版pure PASS代替；後續R4內容PASS見末節，產品仍未驗。

環境：`/usr/bin/python3`、Python3.12.3、watchdog6.0.0，module位於`$HOME/.local/lib/python3.12/site-packages/watchdog`；不用`-s/-I`（fixture需user site），設`PYTHONDONTWRITEBYTECODE=1`。Case1用真service+模擬首版「unwatch等待lease」的fake；Case2/3用真`watchdog.observers.api.BaseObserver`+fake emitter，不啟OS observer/backend，不連live。

| MAJOR | 精確source/既有fixture輸出 | 本輪處置與新增oracle |
|---|---|---|
| 持consumer鎖等callback | service.py:156–185持`_watch_state_lock`呼叫unwatch；:303–306 callback取同鎖。`callback_finished_inside_unwatch: False`，caller返回釋鎖後才能完成 | W9/T9：unwatch/stop只revoke，drain分離；必須在未放callback barrier前返回，不能熬S或生stuck |
| 外層timed join不包backend清理 | watchdog observers/api.py:353 unschedule→:234 `_remove_emitter`之`emitter.join()`，utils/__init__.py:61 stop同步呼叫hook；inotify.py:121→inotify_buffer.py:44還有內部join。`join_timeouts: [None]`、`still_running_after_S: True` | W10/T10/T12：固定owner B=1、caller等待有界、註冊槽與cleanup intent有界、卡住新watch拒收、latehandle補償 |
| 撤A誤拆共享B | observers/api.py:60 ObservedWatch.key、:270 schedule、:335 remove_handler_for_watch、:353 unschedule。`same_backend_watch: True handlers: 2`；撤A後`B_backend_retained: False B_handler_retained: False` | W11/T11：logical refs/backend generation、只detach A、最後ref才teardown；re-watch碰retiring backend拒收待重試，舊completion不拆新generation |

重跑入口為 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<本child checkout> /usr/bin/python3 -B -`：Case1讓真service `_install_watches()`持鎖進fake unwatch，該fake啟callback跑真`_project_id_for_path`，以`attempting` Event確認進入後`done.wait(.1)`必False；Case2 FakeEmitter.join(timeout=None)記參數並在Event gate等候，thread執行真`BaseObserver.unschedule`，`joined.wait(1)`後`thread.join(.1)`仍alive；Case3同`/virtual/parent`對A/B handlers各schedule，檢共享及unschedule後B消失。所有gate於finally放行/threads join；這些舊行為負例已由reviewer/root執行，不替代T9–T12的新產品驗收。

上述首輪修正目標仍留watcher.py：drain為watcher-local API，service既有stop僅要求撤銷，C才接整體drain。第二輪已反駁「固定owner即可宣稱backend全部資源有界」：全域lock卡住時其它watch event delivery停滯，EventQueue仍無界入列。因此先前資源有界的residual判定撤回；只有W12全部採用source queues通過後，availability才可單列殘餘。既有handler不被誤拆、caller不被backend拖住、owner/queue/資源不增長均屬本票硬不變量；不暗改service或watchdog套件、不降state。

## 第二輪歷史：無界來源成立、未以residual放行

Reviewer在雙backend A teardown卡observer lock、B連續事件時觀察10k事件全積壓且零delivery；root另用真BaseObserver/EventEmitter但不啟OS backend、固定一backend交替兩FileModifiedEvent重跑，`qsize=10000/maxsize=0`。這是memory無界，不是單純availability residual。native額外source：watchdog6.0.0 `observers/api.py:140` queue_event→put，`:183`建立EventQueue()；`utils/bricks.py:71–79`只跳過相鄰相同事件；`inotify_buffer.py:27–33`建立DelayedQueue且立即start、`:98`逐筆put；`utils/delayed_queue.py:23–31`無界deque/append；`inotify_c.py:188/210–229`move-cookie cache有clear方法但此installed observers目錄的當下rg僅找到定義，source_for_move不移除entry。此靜態範圍證據不是所有版本/所有caller的全稱結論。

作者另於11:02:09Z前在`/tmp`執行純記憶體installed-library負例（未啟OS或新thread、不跑產品code）：真BaseObserver(EventEmitter) schedule但不start，兩path交替queue_event一萬次；DelayedQueue.put一萬次；Inotify以`__new__`只建move map並呼叫remember_move_from_event一萬cookie。輸出依序`central_qsize 10000 maxsize 0`、`delayed_queue_records 10000`、`move_cookie_records 10000`、`started_threads False False`。只證明當前buffer方法沒有容量界，不宣稱真OS固定拓樸記憶體壓力E2E已測。

| 裁決 | 真代價與source理由 |
|---|---|
| native未採用 | `inotify.py:116–119`直接建buffer，buffer直接建queue/start；即使只覆寫少量constructor，raw/cache溢出也要維護inotify_c.py:343–357的recursive wd/path改名，再加generation-safe backend rearm/reconcile。root invalidation alone不能修backend拓樸，復原面比polling大，不作本child第二條路徑 |
| 受控polling已採用 | `polling.py:71–115`每輪snapshot/diff→事件，無DelayedQueue/move-cache；直接ingress替換中央data queue。root接受P+Tscan延遲/有限拓樸峰值、短命與same-metadata盲點可能等既有rescan；仍保留HEAD/rename/final-store及真OS驗收，不因接受取捨跳過產品gate |

原K-ring候選已撤回，不能與新設計並存：它會把event先留backend queue、再陸續轉callback pending，讓全部100次ingress完成後仍可能分批晚送、破壞active首輪＋一次補跑的oracle。Root採直接root-coalescing ingress：raw0、每generation pending/ready membership/active各≤1，只有一套data狀態；Observer facade只保留獨立stop latch≤1、unfinished_tasks=0。backend-generation proxy封存token，不靠ObservedWatch的path相等去找現在的新代。

此前確實停在draft/design-not-ready且未派工；現已按root裁決收斂成單一accepted候選。以下source/記憶體證據支持規劃可行性，不代表產品通過。

## Root裁決的consumer/source證據與最小seams

當下repo production引用掃描只定位service作watcher consumer：service.py:280–301由Path選全量/單project重讀，snapshot.py:187–211/288–309只在最終state signature改變時發ChangeEvent，sequence是觀察diff而非filesystem event log。service.py:221–243只監workspace/project非recursive、.git HEAD file與refs recursive；tests/test_stage9_project_monitor_service.py:981明文burst合成單事件，:1081的HEAD trigger驗最終todo、:1187只要求HEAD.lock replacement後通知HEAD。GitHub durable event_spool是另一consumer來源，不能混作watcher逐事件需求。此為當下佐證，T3/T4和未來consumer相容gate維持機械守護。

兩個polling差異均已source＋純記憶體實證：DirectorySnapshotDiff只比較inode/mtime/size（watchdog utils/dirsnapshot.py:113–129），兩次snapshot相同metadata輸出全部空；HEAD同path換inode輸出deleted+created，跨path保留inode輸出moved(src,dest)，足以投影各root。stock PollingEmitter的OSError分支（polling.py:84–88）會emit DirDeletedEvent再stop；service.py:193不重裝已watched key，故裸PollingObserver替換不合格。

最小選定seams：BaseObserver公開emitter_class注入小受控PollingEmitter；constructor後、首次schedule前替換該instance `_event_queue`（api.py:316把它交emitter）；小wrapper包instance `_take_snapshot/_snapshot`及on_thread_start，R3補DirectorySnapshot公開stat/listdir窄化VFS與per-scan failure latch。initial完整scan成功才啟用steady-state OSError保活，不吞其他例外。BaseThread.stop先設flag再on_thread_stop（utils/__init__.py:61–64）；local Observer hook先wake latch再super cleanup；BaseObserver.dispatch_events在sentinel分支先返回、尚未取Observer lock或task_done（api.py:378–391）。因此不需拷貝整套walk/poll/diff或全域patch。這些private/public相容面必須在candidate/CI測真watchdog6.0.0；介面/dirfd能力不合在watch成功前拒絕，不能fallback回native/無界queue。

原author時點CI來源已核對：pyproject.toml:22的test extra只含pytest；.github/workflows/tests.yml:55–60安裝.[test]後直接pytest，沒有watchdog安裝。故todo明列在該既有test job加入watchdog6.0.0與import/version確認、核心fixture缺依賴FAIL，不能沿用舊optional skip假GREEN。此是未來產品test-only基礎設施，仍屬tests artifact，production唯一watcher.py；當時作者只改四份文件，未改CI/test-extra/正式runtime套件。這不是本次root planning preflight的結果；若未來產品交付不允許這個test job變更，須先回root解決真CI依賴，不可宣稱CI已驗watcher。

離線fixture全部stdin、無檔案產物：11:29:13Z前的memory VFS以真DirectorySnapshot/DirectorySnapshotDiff與未start的PollingEmitter執行queue_events(0)；輸出HEAD replace=[deleted HEAD,created HEAD]，rename=moved HEAD→new，stock error stopped=True。`BaseObserver(PollingEmitter)`第一次schedule前替换instance queue，實際emitter收到同一queue：pre_schedule_queue_injected=True。真service以__new__配fake store/不啟服務，三個Path依序得到full-current-scan/current-project-scan:A/current-project-scan:B。

同輪追加memory fixture：兩個真PollingEmitter各10k個虛擬snapshot、簡化root-coalescing sink，40,000次offer只保留2backend roots、每snapshot2records；包單一emitter snapshot seam後暫時PermissionError回last-good、stopped=False/degraded，下一次成功仍stopped=False/healthy並保留root dirty。Observer/emitter均is_alive=False，未啟OS/backend/thread。此sink未含完整generation/stop/refcount/W排程，不把它冒稱T13 PASS；錯誤僅注入_take_snapshot外層，R3已反證它不能覆蓋descendant內部吞錯，故不再以此作完整snapshot保活證據。initial/descendant failure、完整facade、真OS壓力仍未勾產品AC。

## R3：兩個真缺陷、窄化修正與證據界線

Reviewer `watcher_polling_r3` 回傳既有stdin程式與stdout，沒有fixture檔案；當時未請它重跑。Root亦獨立讀installed watchdog6的DirectorySnapshot.walk與native symlink排除，兩項皆採納為MAJOR，不能當bounded residual。作者當時只改這四份規劃文件，並留待新fresh review；後續R4內容PASS見末節，不以pure helper或下列memory機制驗證覆蓋產品gate。

| R3缺陷 | 已有精確證據 | 修正／未完成驗收 |
|---|---|---|
| partial snapshot假健康 | watchdog utils/dirsnapshot.py:318–329吞listdir ENOENT/ENOTDIR/EINVAL、:331–336吞child stat OSError、:338–342吞recursive PermissionError。Reviewer真DirectorySnapshot＋memory VFS令`/v/refs` listdir拒絕：`initial_snapshot_returned_success=True`、paths僅`/v,/v/refs`；真PollingEmitter外層guard `guard_catches=0/degraded=False/last_good_replaced=True`，發`FileDeletedEvent:/v/refs/main`。child stat suppression為source確認，reviewer當時未實跑 | D4.3每操作/iterator failure latch＋candidate返回後驗；initial拒絕、steady保last-good、不進partial diff，T14真產品待驗 |
| recursive symlink擴張 | polling.py:58–69預設os.stat；dirsnapshot.py:333–342按跟隨後S_ISDIR遞迴；native inotify_c.py:402–411明文islink skip。Reviewer真library memory VFS：polling列舉`/virtual/refs`與`/virtual/refs/linked`並收入`linked/outside`；native模擬只註冊`/virtual/refs`。這是external-link模型，不是OS/cycle實測 | D2/D4.3 nofollow descriptor VFS，明確root可跟隨且每scan重錨，descendant只link entry；T15 external/cycle/交換/root替換/fd與平台gate待產品驗 |

R3另有一項**窄seam成功**：真BaseObserver.dispatch_events配一bit facade及ForbiddenLock，輸出`sentinel_bypassed_observer_lock=True/gets=1/task_dones=0/unfinished=0/threads_started=False`。它僅證stop sentinel在Observer鎖/task_done前返回；沒有證完整raw0/scheduler、closed busy-spin、stop hook阻塞或generation/memory產品PASS。D4.2原契約保留，這些T13仍全部未勾。

作者於`2026-09-07 13:39:56 UTC`前以`/usr/bin/python3 -B -`、PYTHONDONTWRITEBYTECODE=1在`/tmp`跑窄機制fixture，真DirectorySnapshot/未start PollingEmitter＋memory stat/listdir：stat/listdir generator先記第一個非准許OSError，再raise；snapshot返回後若有latch就raise合成OSError。沒有新thread/OS backend/檔案產物，沒有產品code執行。

```text
initial stat_denied rejected:13
initial list_denied rejected:13
initial iter_denied rejected:13
initial list_invalid rejected:22
initial root_missing rejected:2
initial entry_missing accepted:/v,/v/cycle,/v/link,/v/refs
steady stat_denied/list_denied/iter_denied/list_invalid:
  last_good=True degraded=True stopped=False（四項各別相同）
recovery degraded=False transitions=[degraded-root,healthy-root]
paths=/v,/v/cycle,/v/link,/v/refs,/v/refs/main
listed=/v,/v/refs threads=False
```

重現fixture的最小seam：rows提供root/refs目錄、refs/main regular、link/cycle均S_IFLNK的stat模式；stat可對main拋EACCES或ENOENT，listdir可在refs建立或yield一筆後拋EACCES/EINVAL；stat/listdir wrapper共同`mark(op,path,errno)`，唯`path!=root and errno==ENOENT`不置failure。`candidate=DirectorySnapshot(...,stat=wrapper,listdir=generator_wrapper)`後檢failure；initial直接拒絕，steady guard回emitter._snapshot且只在health轉換記root hint。真DirectorySnapshot只listdir `/v,/v/refs`，證明link-mode可阻斷walk；**不是dirfd/syscall競態/真OS symlink證明**。後者仍由T15實際產品與OS smoke驗。

Root採納的最小source選擇為watcher.py內scan-context/兩個supplied VFS wrappers，不複製watchdog traversal：每scan os.open明確root並fstat；descendant父鏈逐component dir_fd＋O_DIRECTORY/O_NOFOLLOW，最後stat follow_symlinks=False，listdir用scandir(fd)。支持面經本輪唯讀capability確認：os.open/stat∈supports_dir_fd、os.stat∈supports_follow_symlinks、os.scandir∈supports_fd、O_NOFOLLOW/O_DIRECTORY均True。watcher/tests/pyproject/README/既有tests.yml的定點檢索未找到watcher明確Windows/macOS相容驗收；這只是所列範圍當下佐證，不宣稱repo不存在其他平台承諾。新的能力缺席fail-closed是明示平台限制，不把watchdog stock的platform-independent敘述套到此adapter。

FD保守同時上界≤4/scan且finally關閉；snapshot context不留活fd/traceback，不跨scan沿舊root inode。component走訪的深度H/實際entries成本計Tscan，有限拓樸M下的memory界不等於所有目錄大小或churn都有固定M。不可取消scan占原emitter及其有界fd，屬有界資源下availability殘餘；descendant拒讀誤判healthy、初次假註冊、跟隨external/cycle及fixed-topology backlog仍是硬FAIL，不挪給母票。

接受的有界殘餘：P預設1秒，實際延遲P+Tscan+D+callback排隊/執行＋consumer debounce/scan，不是2秒；成本依Nbackend及各自有限拓樸M加總，snapshot/diff峰值需測。短命事件與相同inode/mtime/size改寫可能等既有rescan；當下config.py:53–55為full60秒/targeted300秒，實際配置/慢scan會改時間，不是硬保證。正常poll不強制refresh、不改service；暫時OSError同emitter保活/恢復，未知fatal明示不健康、保留診斷與unwatch→watch路徑，不永久空snapshot或無限restart。卡住stat/listdir不可強取消，原emitter仍占資源，drain在S返回tracked residual；健康B不能因A佔Observer lock而被擋ingress。這些影響可由母票繼續列管，但固定拓樸memory無界、late-generation誤投、漏已接納root、busy-spin均仍FAIL。

## Adversarial 標準與殘餘

未處置的A範圍缺陷/缺口→FAIL；明文承認、影響有界、母票列管的residual不單獨FAIL。不得以residual名義接受A漏掉跨project/root覆蓋、per-eventTimer、pending無界、D被延後、watcher自己late requeue或state未釋放。不得以A部分修正/測試綠燈宣稱母#827完成。

R3修版當時要求root先做新fresh compact獨立review，不以未重審版本開child派工；後續R4內容PASS及root建立#853已完成。本PR又加入repository intake links，仍unfrozen／未merge／未正式freeze／未dispatch，須在#831真runtime重評再按正式Cortex流程派工。若實作需改consumer或第二production模組，先回報擴scope/真sizing，不能維持domain0。產品真OS/壓力/CLI、跨來源fairness、durable fence與live thread/freshness成果仍未證實。

## R4 review addendum（不變更核心規劃）

Root於R4回報fresh reviewer `watcher_polling_r4` 結果 **PASS，0 MAJOR**，對象為上表spec/design/todo三份歷史hash的R3修正版。reviewer的memory failure-latch、nonfollow link-mode、sentinel seam支持設計內容可行性；這是root轉交的獨立review結果，本作者未重新跑該review，未把source或記憶體fixture說成dirfd OS／fd上限／完整scheduler產品proof。

R4 addendum在當時取代「R3兩MAJOR尚待新review」的規劃審查狀態，**不**取代產品checkbox、真8/Red gate或#831後6/Yellow條件投影。當時三件組bytes不變、只追加report歸因；其後author版補#853 owner/link、歷史時態、R4歸因與當時未register/dispatch狀態，技術核心及數值未變。Root已據草稿建立/readback唯一child #853 OPEN，未由本作者執行GitHub mutation；本PR再進repository intake，不代表產品真T13–T15、OS/symlink競態/fd界、CI/CLI/merge與母#827完整整合已完成。

## 歷史author #853 owner metadata版檢核與hash

該author輪次只更新四檔owner/link與狀態敘述；當時作者未執行GitHub action、`.cortex`註冊、CH/fragment新增、commit/push、product/live或dispatch。#853 OPEN建立/readback證據由root提供，本作者未另查GitHub。當時authoring branch為`feature/refine-monitor-watch-child-20260907`，不可混作本次planning整合branch或未來canonical build branch `feature/853-monitor-watcher-bounded`。

以修改前記憶體字串逐段比較：spec從W1開始的全部Requirements/Boundary、design D1–D5、todo從Tests/TDD RED起的全部產品Tasks，結果均`technical_core_identical=True`。只改owner/status/R4歸因與歷史時態，未降低state/invariants、未改raw0/nonfollow/failure-latch或其他產品AC。

`2026-09-07T14:00:29.422022+00:00`於`/tmp`用同一runtime HEAD `79ba644780bf1c697c722ac24a297e7d02416100`重跑完整pure鏈：三件組accepted/complete=True、missing/reasons/blocking皆空；score=`0/2/2/2/2`、total=8/red。Yellow helper仍ready=True、envelope_unavailable，不覆蓋Red或產品gate。該歷史owner/status版三件組hash如下；上表R4歷史hash保留其原審查對象，不冒稱owner metadata或本PR intake版曾重新跑R4。

| Artifact | #853 metadata版 SHA-256 |
|---|---|
| `docs/superpowers/specs/monitor-watcher-bounded-spec.md` | `31f7d300565b3c3eb2128fa668ca71c07cc0c44f77c7b1fe2afeeff45367774b` |
| `docs/superpowers/specs/monitor-watcher-bounded-design.md` | `0db7a7b2c9f5baac611860946b08088bee8d474bd7ed6b4503b55cfb647a595d` |
| `docs/superpowers/workstreams/monitor-watcher-bounded/todo.md` | `ce094f7ef9ceb1ca227cfc80f2b36509d7b1458ed3ff4e13680f9a38da5a6982` |

## 本PR repository intake／unfrozen metadata版

本次只在root的整合worktree／branch `feature/refine-runtime-intake-20260907`修這四份watcher文件，進場唯讀確認base為`2c1bba019a63fdfee68479ef6b4b18f2c9c11c06`。這是進場base紀錄，不是待定或未發生的merge SHA；後續整合／fast-forward由root另留真實證據。原author worktree未改。

Root已在本PR加入`.cortex/work-items.yaml`的#853唯一spec/design/todo links及母plan/tasks，本作者只讀核對links，未改這些root-owned檔案。當前狀態為repository intake／unfrozen：未merge、未正式freeze、未dispatch，仍8/Red禁派；repository links不是immutable run authority。

Root另轉交fresh reader `schema_fresh_reader` 的五題結果：全部回答正確、未發現歧義。本作者未啟動或自批該reader，亦不把此readability結果抬升為產品／dirfd OS／fd上限proof。R4技術內容PASS歸因保留；W1–W12、D0–D6、T1–T15與sizing核心不變。

這次metadata作者只做核心文字比對、hash與whitespace檢核，不執行產品／CI／`.cortex`／CH／root整合report／commit／live／issue操作。Root的planning preflight／CI結果與此作者歷史未CI分開；未來Cortex產品TDD、focused/full tests、OS／CLI／policy／review／merge gates也另留證，兩者均不由reader PASS或本次metadata檢查代替。

以修改前逐hash複製的內容為基準，spec從W1起至EOF、design從Decisions（完整D0–D6/T1–T15）起至EOF、todo從Tasks起至EOF的字串比較皆`technical_core_identical=True`。本PR repository-intake metadata版hash如下；只表示當次文件bytes，不是正式freeze或新產品驗收receipt。

| Artifact | 本PR intake metadata版 SHA-256 |
|---|---|
| `docs/superpowers/specs/monitor-watcher-bounded-spec.md` | `2096a58b493915908c266dfa78301d3fea67008bbd80cb82af761aae809d9c42` |
| `docs/superpowers/specs/monitor-watcher-bounded-design.md` | `1f7def514771a84871a75e295f817a19bc757ae15a6693bfdc2132434588e217` |
| `docs/superpowers/workstreams/monitor-watcher-bounded/todo.md` | `a837f7bcdd5a13b080810a7ddf0b0eec3e7feaba08d4852ceb8b60dd3e353aab` |
