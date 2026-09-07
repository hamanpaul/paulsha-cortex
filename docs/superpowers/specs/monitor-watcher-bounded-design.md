---
status: accepted
work_item: monitor-watcher-bounded
---

# Watcher 排程、失效通知與停止設計

Owner [#853](https://github.com/hamanpaul/paulsha-cortex/issues/853)，work item `monitor-watcher-bounded`；parent #827、umbrella #829。Root已建立/readback OPEN，並在本PR加入唯一`.cortex` spec/design/todo links；當前為repository intake／unfrozen，未merge、未正式freeze、未dispatch。本文件及repository links不授權繞過8/Red gate，不等於已凍結的run authority。

## Decisions

### D0 單一裁決：受控polling與直接root ingress

Root已選watcher.py內受控polling：local BaseObserver subclass＋小型PollingEmitter adapter＋直接root-coalescing ingress，保留`WatchdogFileWatcher`公開名稱。native私有buffer/cache復原面更大，已不採用；不修改watchdog套件或全域monkeypatch。原K-ring/overflow queue方案撤回，不能與D4.2同時保留。此accepted規劃的R4 PASS／0 MAJOR由root轉交fresh reviewer `watcher_polling_r4` 結果，不是產品GREEN；現行完整combo仍8/Red，所有產品驗收未勾選。

### D1 單檔 owner 與資源界

`WatchdogFileWatcher`在watcher.py內擁有固定callback workers、一個backend owner、一把condition/state lock、logical/backend state map及唯一ready membership。資料流：受控PollingEmitter → immutable backend-generation proxy → 同一短鎖直接root pending → deadline/FIFO → callback active lease → consumer callback → generation-bound completion。Observer dispatch thread不運送data callback，只等stop控制latch；沒有第二個data backlog。backend owner只處理Observer控制/回收，不執行consumer callback；callback/Observer操作、join和等待均在state lock外。

預設W=2、quiet debounce d=500ms、最大D=2000ms、shutdown總budget S=2s，poll interval P=1s。既有debounce_ms可覆寫；新增instance-only poll_interval_seconds與測試seams：W為正整數且非bool，d≥0、D>0、S>0皆有限，P為非bool有限數且0<P≤60秒；d>D時取D為有效quiet上界。monotonic clock可注入，不新增CLI/env。W workers共用condition等最早deadline，不建每event/watch Timer。P由BaseObserver timeout傳到PollingEmitter，與D/S不同；不能把S=2秒或D=2秒寫成端到端freshness。

callback active≤W、每key active≤1/pending≤1；新增backend owner B=1，不是每次cleanup啟動thread。控制輸入為每個既有logical/backend key最多一份desired-state/cleanup intent，加最多一個新註冊in-flight槽；不用Future/每次request append queue。callback tombstones≤W，backend retiring records另列，不混充已移除的live key。

只有backend owner執行Observer的schedule/start/remove_handler/unschedule/stop/join。採用的backend inventory為一個Observer dispatch thread＋每實際ObservedWatch一個PollingEmitter，另加W callback workers與B=1；沒有InotifyBuffer/helper/cache。owner阻塞或retirement未收斂時，新watch不配置backend、以可重試OSError拒收；既有key的unwatch/stop仍合併intent。因此阻塞開始時N0個backend加至多一個in-flight註冊，後續註冊churn不增thread/tombstone數。每個poll僅保留上一snapshot、當次snapshot/diff/暫存；其峰值依有限拓樸M計量，不隨事件總數累積。診斷只保留bounded當前原因/數值，不存event/exception traceback歷史；統計counter須固定寬度飽和或等价不隨event總數增配置。

### D2 不漏路徑的 payload

維持 `WatcherCallback = Callable[[Path], None]`。目錄 subscription 永遠 callback 該 watch root；它是可重建整個監看範圍的 invalidation，不回報任意 latest child。檔案 subscription callback 固定 watch file；註冊時保存 file/directory matching 類型，使 file 暫時被 rename/delete 時不被誤當目錄。Observer watch 範圍沿既有 file→parent、directory→self，recursive 匹配契約不擴張。

src/dest 不能在第一個 match 就丟掉其餘適用 watch；每個 handler 可按自己的 root 合併，但 rename 跨 A/B watches 必須分別產生 A/B invalidation。目錄非 recursive 只匹配直接 child/root；不同 project 共用 workspace root 時收到 workspace invalidation，既有 service 的無 project match 分支執行全量 refresh，而非誤只刷新最後 project。

`StubWatcher` 保留同步手動觸發角色；production 的 root invalidation 契約須另外用真 WatchdogFileWatcher/fake Observer 驗，不要求 stub 模擬非同步。若 repo consumer 實測依賴逐事件 Path 而不接受 root invalidation，先回報契約衝突、改規劃，不在本 child 暗改第二個 production 模組。

遞迴拓樸沿現有native不跟隨descendant directory symlink的邊界：link entry自身可使root失效，external target的子項不進snapshot；file symlink同樣只記link metadata，沒有target內容監看。明確指定backend root可跟隨symlink，仍保留logical lexical Path；每scan重新開root，不把上一scan的root inode/fd當永久authority。D4.3的dirfd/no-follow supplied VFS還須防止檢查後directory被換symlink，不能只先lstat再以完整字串scandir。

### D3 Deadline 與 active/pending state machine

狀態：idle→pending(f,e,generation)→ready(FIFO)→active(lease)→idle；active期間可另掛一份pending，completion只把同generation pending加回ready。第一次ingress提交設f=e=now；後來只改e，deadline=`min(e+d,f+D)`。pending/ready共用同一record及membership，已ready不退回debounce；deadline先到者入隊，同時到以穩定順序裁決。補跑進隊尾，不重排既有其他key。raw event在put返回前已合併，不可能100次put完成後還留一批backend data稍晚生成第三輪；控制burst oracle以這個提交點為準。

ready 是 deadline 到期後的排程資格，不是假稱 callback 已開始。若全部 W workers 正在 callback，仍保留原 deadline/首次pending；worker可用時先按到期時間/穩定序轉入ready，再取工作，不能把worker醒來時間冒充ready_at或重設D。唯讀diagnostics可據clock顯示已到期但尚未取得worker的等待；T2分別斷言ready資格與有限L前提下的開始界。

lease在鎖內驗stopped/key generation後取得，是callback admission的線性化點。鎖外呼叫callback的前後都可設測試barrier。**內容invalidation**只設root dirty/pending，不撤銷registration；**stop/unwatch revoke**才使generation失效。revoke在該點之前則不得取得lease；之後算既有active，stop/unwatch只撤銷、不等該lease，drain才在安全外部上下文等待。完成時只按所帶lease generation更新自己的狀態，不碰同path的新registration，亦不因舊callback完成而新增worker。

### D4 Stop/unwatch 與可見 residual

**撤銷與等待分開。** `unwatch(path, recursive=...)` 只在state lock內invalidate目標logical generation、清pending/ready、合併cleanup intent、notify即返回；不進observer lock、不呼叫unschedule、不等callback。`stop()`同樣非阻塞撤銷全部keys、設定stopped並喚醒owner/workers；既有無參數signature/None回傳保留，返回語意明訂為revoked/requested，非drained。重複呼叫不增加工作。真service `_install_watches` 持`_watch_state_lock`呼叫unwatch，而callback讀project又要同鎖；此循環必須消除，不得等S才返回或標成非合作provider。

新增watcher-local `drain(*, timeout_seconds=S, path=None, recursive=True)`：必須由不持consumer lock的外部呼叫者使用；path指定時只快照該path已revoked generations的callback/backend拆除，排除已成功re-watch的新generation；省略path等待當下全部撤銷工作，且stopped時包含owner/worker退出。drain在鎖內只取snapshot，鎖外按同一absolute deadline等待；不得直接呼叫可能阻塞的Observer操作。回傳JSON-safe診斷（drained、pending generations、alive callback workers、backend owner/operation、未釋放backend資源、timeout reason）；callback/owner執行緒呼叫drain明確拒絕，不self-join。N個worker/backend不能各花S變成N×S。

backend owner自治處理cleanup，即使既有service不呼叫drain，合作式工作仍自行完成；服務整體等待與publication fence由C接線。A驗收用stop→drain或unwatch→指定path drain，明示兩階段。PollingEmitter可能卡stat/listdir，owner在emitter.join(None)等待；外部drain仍在S返回且保留B=1與實際inventory，不增owner或丟記錄，fixture放行後再次drain成功。source正常poll暫時OSError依D4.3保活，不能與不可取消scan混為同一故障。

BaseObserver的unschedule仍可持全域lock等emitter，但本設計data ingress不取得該鎖：A teardown卡住時，健康B backend仍應能提交root pending並在W/L前提下執行callback，T10/T13必驗，不能把B被observer鎖卡住列residual。卡住scan本身沒有事件可提交，該backend的freshness與新registration/cleanup沒有有限完成保證；它及所有活資源必須degraded/tracked，不能假報healthy。固定拓樸memory有界是本票硬gate，只有不可取消外部scan的有限availability是母票列管殘餘。

backend operation拋錯或部分清理時保留具體operation/handle/generation與unknown/retiring狀態，不能用Observer已先刪map條目當清理完成（emitter可能仍alive）。新註冊持續fail-closed，owner按有限既有intent對帳，不新增重試record；diagnostics每key只保留當前必要狀態/原因，不建立無界error歷史。

drain/diagnostics只讀watcher-owned shadow state及已持有的thread/handle引用，不為取得狀態再進可能被卡住的Observer lock。owner在危險操作前保存operation、backend generation及已知資源；尚未返回的schedule若有未確認配置，明示in-flight/unknown、算入唯一註冊槽上界，不能回clean/drained或宣稱活資源已是零。部分失敗後若無法證明資源已釋放，記錄與新watch拒收都保留。

**註冊成功不能造假。** `watch`保留同步成功/失敗語意：重複已成功active key沿既有idempotent/no-op，不換handler或新增槽；新註冊只在owner健康、retirement已收斂且唯一新註冊槽空閒時接納，caller放開state lock後最多等S；owner不等consumer callback。完成且generation仍有效才publish logical-active並回成功；busy/unhealthy/timeout回可重試OSError，使既有service不把失敗key加入`_watched_paths`。timeout要撤銷該註冊generation，late schedule結果只補償自己的實際handle，不能重新啟用已revoked key。slot/未釋放handle仍計數，直到cleanup ack才可接下一筆；不為每次重試保留新intent或thread。

stop/unwatch只管watcher admission、pending與callback completion。consumer已開始的DB/file/socket/event side effect需B/C真正publication fence；本項不以入口generation check冒充它。真service鎖循環與共享handler被誤刪不是可接受residual；只有已隔離的非合作外部backend/callback、資源有界且可診斷的等待可列residual。

### D4.1 Handler 引用與 backend generation

logical key=(canonical watch path,recursive,logical generation)；backend key按實際ObservedWatch的root/recursive/event_filter決定，不能把logical path當backend唯一鍵。每backend record保存實際handle、固定長度backend token、source/emitter引用、logical handler集合/refcount、live/retiring/disposed。file A/B同parent可共享同backend。handler仍向BaseObserver正式註冊供shared-watch/refcount管理，但data callback只經直接ingress的shadow logical registry，不由Observer.dispatch_events再執行一遍。公開內部測試所讀`_watches/_handlers`移除logical claim時可立即清，尚活backend/handler引用另留retiring inventory，不假裝資源消失。

owner處理撤A時，先只移除A的handler（`remove_handler_for_watch`或等价受控adapter），refcount仍有B則不unschedule；最後一個有效handler撤銷才可commit backend teardown。提交前重查desired refs/backend generation，所有Observer操作由單owner序列化。teardown已committed的backend不能被同key新watch復用，回可重試OSError，等待舊handle確實disposed後才建立新backend generation。完成比對實際handle+generation，而非只用path pop；舊detach/completion不得移除B或新generation。

re-watch在尚未commit teardown時也不得繞過owner搶接；先收斂舊logical detach，再由新註冊槽安裝新handler，必要時復用仍有B的健康backend。保持每個logical key的generation fence，舊handler即使仍收到event也不可取得新lease。Observer內部私有結構只供fixture/adapter相容檢查，不能由其他consumer直接改；若必須改watchdog套件或service才能成立，先回root擴scope/re-sizing。

### D4.2 直接ingress：一份data狀態與獨立stop latch

在local BaseObserver構造完成、任何schedule/start之前，把該instance `_event_queue`設為watcher-owned ingress facade；不替換運行中的queue，不改套件全域。emitter factory在owner已預留backend record時建立proxy：proxy封存backend token及該實際emitter/handle身分，之後每次put不可依當下同path查新token。共享ObservedWatch的新logical handler沿用健康backend token；舊backend disposed後即使watch.key/path相等，新emitter必取新token。owner預先追蹤constructor/start中的proxy/emitter引用，供partial failure/late schedule精確補償。

proxy.put((event,watch),block=True,timeout=None)在短state lock驗instance未stop、backend token仍有效，按該backend目前已准入logical generations逐一匹配src/dest（以註冊時保存的file/directory/recursive資訊做lexical匹配，不做stat/resolve）。相關每key直接更新D3的同一pending record並notify；同event兩端/多key各自完整合併，file仍固定file Path、directory仍root。put返回時data已提交，raw event不再持有；無K-ring、overflow list、future或第二批稍後入pending的data。暫態呼叫stack/poll snapshot-diff另計D1上界。被revoke/late backend proxy的event只drop，不建立未知key，也不喚醒新generation。

Observer dispatch thread僅控制通道，facade不是一般用途work queue。有限狀態OPEN→DATA_CLOSED→TERMINATED；data close由watcher.stop在state lock內完成，撤銷logical pending/ready並喚醒W workers與owner。控制latch `wake_pending`最多一bit；put/put_nowait收到EventDispatcher.stop_event只set/notify，重複合併。get(block/timeout)只等待/取走此bit並回原stop sentinel，無bit時依參數等待或queue.Empty；永不返回data，不因DATA_CLOSED就連續回sentinel/Empty忙迴圈。

local Observer僅另覆寫on_thread_stop：先置stop latch/notify，再呼叫super清emitters。此hook只由backend owner的Observer.stop觸發，且BaseThread.stop已先設stopped_event；因此真dispatch_events收到sentinel後在進Observer lock及task_done之前返回，run loop讀stop flag退出。EventDispatcher.stop在hook返回後還會put一次sentinel：此時仍只佔同一bit；owner確認Observer已join後把facade設TERMINATED、清bit，後來重複stop put為no-op，不留下待消費task或復活dispatch thread。TERMINATED才允許get回Empty而不等待，此時沒有存活的Observer消費loop。owner若已卡另一unschedule而尚未執行stop，dispatch thread可維持單一parked waiter，計入drain residual；不能為喚醒它另啟thread或從caller執行阻塞stop。

facade不保留Queue的data unfinished計數：所有put在ingress同步完成data admission，`unfinished_tasks=0`恆定；task_done若意外被呼叫即像空Queue般拒絕，join只反映零deferred queue tasks、不能當callback/backend drain。qsize/empty只報控制latch0/1，另用diagnostics報真正logical pending/ready/active及raw_backlog=0，不拿空控制queue隱藏工作。T13須驗此實際get/put/stop/task_done路徑、closed無busy-spin、無第二次data callback，禁止復用普通無界Queue作暗中backlog。

### D4.3 Polling snapshot保活與相容面

使用BaseObserver公開emitter_class seam；受控PollingEmitter重用原constructor/queue_events與DirectorySnapshot/diff演算法，包instance `_take_snapshot/_snapshot`、on_thread_start並提供窄化stat/listdir。**外層catch不足**：watchdog6的DirectorySnapshot.walk會吞child stat OSError、recursive listdir PermissionError及部分errno；必須在supplied VFS先記錯，再於DirectorySnapshot返回後驗scan latch，不能把「constructor返回」視為完整成功。不複製walk/diff/poll loop，也不新增通用FS engine。

每次_take_snapshot新建scan context，由backend owner的初次註冊或原emitter的poll執行緒取得root目錄fd，不持watcher state/consumer lock，沿用PollingEmitter自身鎖；root可跟隨明確指定的symlink，但每scan重開、finally關閉，不跨scan沿用。snapshot路徑仍為原lexical root；root stat取fstat(rootfd)。descendant stat先從此rootfd以逐component `os.open(...,dir_fd=...,O_DIRECTORY|O_NOFOLLOW)`走到parent，再以`os.stat(name,dir_fd=parentfd,follow_symlinks=False)`讀entry。descendant listdir同樣no-follow開到該目錄，再對fd使用os.scandir；只消費DirEntry.name，不利用跟隨symlink的DirEntry.stat。相對component不得含逸出root的`..`；不以resolve把descendant symlink重寫成外部路徑。中途換symlink/非目錄可使scan失敗，但不能掃入target；不在本票另做retry-walk引擎。

rootfd、component走訪current/next fd與scandir iterator皆有finally/context-managed close；同時活descriptor保守上界每scan≤4（root＋走訪暫存＋scandir持有），不為每entry或depth永久留fd。DirectorySnapshot會先完整消費某層listdir才stat/遞迴，因此listdir generator必須在耗盡/raise時即關閉。候選snapshot保存的wrapper context不得保留已關fd/exception traceback；scan結束後不能重用其I/O。每scan額外path-component走訪成本按實際深度與entries計入Tscan，記憶體按該scan拓樸M及最大深度H計量；不宣稱不同拓樸/churn使M全域固定。卡住I/O仍只占原emitter及該有界fd inventory，直到合作式返回才可close。

stat、listdir建立iterator及每次迭代皆用同一scan context攔OSError，**先**按下表記第一份bounded failure(operation/relative path/errno)，再原樣raise讓library走原路。不能只包呼叫listdir取得iterator而漏掉next()的PermissionError。failure只留有限字串/errno、不append錯誤或traceback；若library吞例外後回partial candidate，外層仍檢latch並丟棄candidate，拋合成OSError進既有initial/steady guard。其他例外照fatal路徑，不標healthy。任何失敗之後同一scan的成功操作都不能清latch；只有新scan完整成功才恢復。

| 操作／錯誤 | 唯一裁決 |
|---|---|
| 任一root open/stat/listdir/iteration OSError，包括ENOENT | scan失敗，不偽造空root或成功註冊 |
| no-follow descendant stat/listdir/iteration的ENOENT | 目錄列舉後entry/ancestor已消失的競態可略過；在該scan omission可合法形成deletion hint，不跟隨dangling link、不把permission failure轉ENOENT |
| descendant EACCES/EPERM/EIO、EINVAL、ENOTDIR/ELOOP及其他OSError | 一律latch不完整scan；尤其ENOTDIR/ELOOP可能為directory→symlink/file競態，採保守重試下一poll，不假empty成功 |
| 非OSError | fatal、不假last-good健康；不在本票無限自動重啟 |

這裡「完整成功」只表示全部已嘗試的scan I/O沒有未准許錯誤，不是原子FS snapshot；並發新增/刪除可能形成best-effort視圖，下一poll/既有rescan補觀察。第一次on_thread_start只有latch通過才標initial_snapshot_done；不完整scan OSError傳回owner，watch失敗並精確清理，EmptyDirectorySnapshot不是last-good。

initial成功之後，guard只catch OSError（含scan latch產生的錯誤）：保留本emitter上一成功`_snapshot`，記當前bounded degraded reason/last_error_at，首次degraded轉換提交該backend有效logical roots的invalidation，使真consumer依其last-good規則讀現況；不stop/restart或新增emitter，不每次失敗append事件歷史。後續每poll仍新建scan context/重錨root，latch通過才回新snapshot並clear自己的degraded，degraded→healthy轉換另提交root invalidation，即使metadata未變也讓consumer可清degraded觀察；正常無變化poll不強制refresh。partial candidate不得進diff/成功基線，因讀取失敗而假產生的FileDeletedEvent必須為零；一次degraded root hint與真consumer自己的重讀結果分開驗。此wrapper避免stock PollingEmitter將一次OSError當DirDeletedEvent後永久stop。

非OSError不得轉成空/last-good後假healthy：記有界fatal診斷並保留原失敗路徑，該backend不宣稱watch健康，新同keywatch不得假成功；已註冊consumer可由既有rescan續讀，backend需明示unwatch→watch或外部修復，不私建無限自動restart。initial與steady-state未知例外、PermissionError暫時失敗、恢復成功均獨立測。只保留型別/有界訊息，不持exception traceback形成歷史引用。

版本相容面限定目前watchdog6.0.0：BaseObserver constructor的emitter_class、schedule在emitter建立時傳event_queue、EventEmitter.queue_event呼叫put、EventDispatcher.stop_event identity、BaseObserver.dispatch_events sentinel先於lock/task_done、BaseThread.stop先設flag再hook；私有接點仍只有pre-schedule `_event_queue`及PollingEmitter `_take_snapshot/_snapshot`，新增VFS用DirectorySnapshot公開stat/listdir參數。支援平台必須有os.open/os.stat的supports_dir_fd、os.stat的supports_follow_symlinks、os.scandir的supports_fd及O_NOFOLLOW/O_DIRECTORY；能力缺席在watch成功前明確拒絕，不宣稱PollingEmitter的platform-independent描述等同本adapter跨平台相容。當下Linux/Python3.12.3能力均有，其他平台未證實；不靜默回native/普通字串scandir/無界queue。candidate/CI以真library和能力缺席fake測此契約，若現有平台承諾衝突須回root重裁。測試不得因未裝watchdog而全skip後算過關。

### D4.4 延遲、成本與接受的觀察殘餘

保留service現有workspace/project非recursive與.git HEAD(file)/refs(recursive)範圍，不擴成整repo recursive。每backend每P秒取得一次snapshot，實際週期含Tscan；固定有限拓樸M下前後snapshot/diff峰值O(M)，全instance成本依各backend的M相加（重疊backend不假設零成本）。新queue不使source因消費變慢累積多輪snapshot，非合作scan仍占原emitter且有bounded資源診斷。P=1秒不是已證能在任何拓樸跑完的承諾。

可觀察變化的延遲包含P+Tscan+D+callback排隊/執行，再加service自己的debounce及scan；不等於D或S的2秒。polling看不到兩scan間完整消失的短命變化，也可能漏相同inode/mtime/size的內容改写；root已接受它們可能延後到既有rescan，而非新增每poll強制refresh。當下MonitorConfig預設full poll60秒、targeted rescan300秒，實際可配置/受慢scan影響，不能寫成永久硬期限。service把Path當重掃hint、SnapshotStore比較最終signature發ChangeEvent，沒有逐FS event log契約；不得把這個當下source佐證擴成所有未來consumer的全稱承諾。新增consumer若要求逐事件/瞬態，runtime不變量或相容測試紅燈必須重新裁決。

### D5 離線 oracle 與實際消費端

新`tests/test_monitor_watcher_bounded.py`重用pytest，提供fake Observer、monotonic fake clock、condition seam、Event/barrier、唯讀diagnostics；不新增測試框架。新核心fixture缺watchdog應明確失敗，不沿用既有optional全skip當GREEN。現有tests.yml只安裝.[test]，test extra只有pytest；產品PR須在該既有test job明列安裝watchdog6.0.0與import/version確認，這是test-only基礎設施，不增加production模組或修改runtime依賴。candidate focused/完整CI須實際跑新fixture與OS smoke，原無watchdogfallback另用隔離mock測。

| Case | 操作／harness | 必要 oracle |
|---|---|---|
| T1 W/pending | W=2，兩個callback進入 barrier後卡住；對A注入100事件 | callback-owned worker≤2、A pending=1、無 Timer；放行後A首輪+補跑≤2 |
| T2 D/FIFO | d=0.1s/D=0.5s，fake clock連續注入A；B已ready；顯式wake | A首次pending後0.5s前ready，B不被後來A插隊；L=0.2s時用Q/L/W推開始界 |
| T3 多路徑/rename | workspace下A/B同窗、rename A→B、file HEAD.lock→HEAD | root invalidation覆蓋A/B；兩個獨立watch各通知；HEAD檔仍為固定payload；非recursive深層事件拒收 |
| T4 真consumer相容 | production watcher+fake Observer，真service `_handle_fs_event`、fake store/禁用背景網路 | workspace root觸發full refresh，其結果含A/B最終狀態；project root觸發對應project既有入口，不能只有callback spy |
| T5 停止時序 | admission前/lease後/completion前barriers，stop/unwatch、舊handler再event、同path re-watch | revoke前後分支可重現；內容invalidation仍准入、revoke才使pending=0；舊completion不重排新generation；stop後watch拒收 |
| T6 自呼叫/例外 | callback內unwatch或stop、另一key仍active；另callback拋錯 | revoke不self-join/鎖死，callback內drain拒絕；錯誤歸自身key且worker可續其他key |
| T7 不合作 | W callbacks gate永不自放行、stop後外部drain S=1s，finally釋放 | revoke立即返回；drain有界回≤W callback residual、零替代worker/新admission；finally後舊completion零重排 |
| T8 長串/真thread | 固定seed虛擬60秒、反覆註冊/取消；短真watchdog tmpdir變更 | owned W+B及Observer/helper/retiring資源有界，合作stop→drain回基準，保留全部既有回歸 |
| T9 真service鎖 | 真 `_install_watches` 持watch-state lock撤key；active callback已到 `_project_id_for_path` 等同鎖 | unwatch在callback gate尚未釋放時就返回，不等待S/不生stuck；caller釋鎖後callback完成，外部drain成功 |
| T10 backend超S | 真BaseObserver+受控emitter，A scan/內部join(None)阻塞；另一B透過proxy持續提交；重複stop/unwatch/re-watch | revoke不等backend；drain≤S+明示排程容差返回backend-cleanup-timeout；owner=1/無新registration或資源增長；A unschedule持Observer鎖不得擋B ingress/callback（W/L前提）；finally放行後回收 |
| T11 共享與re-watch | 真BaseObserver+fake emitter，不啟OS backend；同parent A/B handlers共享ObservedWatch，以真queue_event注入B/rename，撤A並drain；最後B撤銷時hold teardown/re-watch | B handler/backend及通知仍在；最後ref才拆；teardown中re-watch明確OSError，放行後正式重試成功；舊handle completion不拆新generation。真OS tmpdir/rename另屬T8，不把純fixture叫OS驗證 |
| T12 late register | owner schedule進barrier→watch等S逾時→stop/unwatch→放行late結果，交錯健康新註冊 | 逾時未假成功/未加入service watched集合；late handle精確補償、無admission復活；slot/retiring refs不洩漏 |
| T13 直接ingress與stop狀態 | 真BaseObserver/EventEmitter＋本地proxy/facade，不啟OS；A teardown持Observer鎖，B交替兩path 10k/100k，兩root rename/固定seed60秒；另W callbacks卡住而全部100次put已返回後才放行 | raw_backlog=0；每generation pending/ready membership/active各≤1、burst第一輪+補跑≤2；wake latch≤1、unfinished_tasks恆0、get從不返回data，stop sentinel先於Observer鎖/task_done、closed不busy-spin；同path重建後舊backend proxy不能誤投新代。root完整覆蓋；object inventory＋有容差tracemalloc retained plateau，不只thread/qsize/RSS |
| T14 polling來源/錯誤/成本 | 真PollingEmitter/DirectorySnapshot＋instrumented memory stat/listdir：child stat EACCES、recursive listdir及iterator中途EACCES、EIO/EINVAL、root ENOENT、descendant ENOENT、非OSError，初次/後續/恢復各跑；scan barrier；另真OS HEAD.lock→HEAD/rename/recreate | 不完整初次watch失敗；partial candidate不進diff/成功基線；後續同emitterlast-good物件保留、degraded一次root hint且無假FileDeletedEvent，完整成功才healthy/root通知恢復。descendant消失可合法deletion，root ENOENT與ENOTDIR/ELOOP不假empty。P非法值拒絕；snapshot/diff/M/H、fd峰值及component走訪成本/Tscan分帳，短命/同metadata仍按rescan殘餘驗 |
| T15 nonfollow拓樸/支援面 | 真DirectorySnapshot＋lstat-mode memory VFS及syscall spy；另真OS tmpdir的external directory link、self/ancestor cycle、dangling/file link、nonrecursive、HEAD/refs與明確symlink root；barrier把已stat目錄換成symlink、scan間更換root target；能力缺席fake | descendant link本身可入snapshot但不listdir其target、不讀outside或形成cycle；每component O_NOFOLLOW且stat follow_symlinks=False，競態不逸出、失敗latch與last-good/finally fd歸零。明確root每scan重錨，不沿用舊inode；最多4 scan fds，不隨M/H/次數積累；不支援平台watch明確失敗。memory與真OS結果分列，不以簡化VFS代替syscall/OS證明 |

所有 barrier/wait 均有 bounded timeout；finally 先釋放 callbacks 再 join，測試失敗也不得遺留 thread。T7 真時間只驗明示 shutdown budget並給有限測試排程容差，非用 sleep 掩蓋競態；T2/T8主要靠fake clock。

### D6 交付與範圍判定

spec W1–W12對應12條機械不變量，domain=0（唯一watcher.py）、state=2（concurrency/lifecycle），artifact classes=source/tests/documentation。R3補強既有W2拓樸/W7健康判定，未另藏第二production模組；T15是這兩項的新反例面，不降低invariants或state。單一polling設計已由root裁決，三件組accepted的R4獨立內容審查已PASS；完整fix-standard仍8/Red，#831真正部署後完整accepted才條件式6/Yellow。R3兩MAJOR經fresh R4重新裁決為0 MAJOR，但pure/記憶體seam不是產品/真OS/壓力/停止PASS，#853 OPEN也不給現在dispatch許可。

產品提交仍由 Cortex 完成 TDD→focused/full pytest→changelog/doc/真CLI smoke→PR-context policy/review/merge。CLI不新增功能，僅驗候選環境的 `python3 -m paulsha_cortex.cli monitor --help`、隔離 config 的 `monitor --once`；不發明 monitor stop/status 命令，不連 live socket。母 #827 的真服務全來源公平、durable publication fence/部署負載 gate 留在C，A綠燈不得關母票。
