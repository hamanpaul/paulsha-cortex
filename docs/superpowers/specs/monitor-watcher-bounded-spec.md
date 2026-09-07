---
status: accepted
work_item: monitor-watcher-bounded
---

# Watcher 有界 callback 排程規格

設計裁決：採watcher-local受控polling＋直接root-coalescing ingress；不裸換PollingObserver，不修改全域watchdog。此accepted規劃已由root轉交fresh reviewer `watcher_polling_r4` 的R4 PASS／0 MAJOR結果，非產品PASS；產品驗收全部仍未完成，現行8/Red不得派builder。原K-ring方案已撤回：原始event backlog=0，只留唯一logical pending/ready狀態，見design D4.2。

## Requirements

本項owner為 [#853](https://github.com/hamanpaul/paulsha-cortex/issues/853)，是 [#827](https://github.com/hamanpaul/paulsha-cortex/issues/827) 的人工規劃 A child，umbrella為 [#829](https://github.com/hamanpaul/paulsha-cortex/issues/829)；母規格基線為 `fb31083e7325be86c6c9d217da56b9c4d799f5a5` 的 `monitor-refresh-thread-backlog` accepted 三件組。A 可獨立驗收 watcher 邊界，不代表母票完成。A 與 work-model B 可分別完成，service C 最後整合。Root已查重後建立/readback #853 OPEN，並在本PR加入唯一`.cortex` spec/design/todo links；當前為repository intake／unfrozen，未merge、未正式freeze、未dispatch，仍8/Red禁派。Repository links不等於已凍結的run authority或產品交付。

1. **W1 固定資源**：唯一production變動為`paulsha_cortex/monitor/watcher.py`，保留公開`WatchdogFileWatcher`名稱。backend為BaseObserver＋小型受控PollingEmitter，不使用native InotifyBuffer/DelayedQueue/move-cookie cache。無每事件Timer/thread或無界executor queue；每instance callback workers上限W固定，每logical generation active≤1/pending≤1/ready membership≤1，卡住worker仍占W，不增替代者。固定backend owner B=1處理可能阻塞的Observer控制操作；一個新註冊槽、每既有key cleanup intent≤1。實際Observer dispatch thread、每backend polling emitter與未釋放資源全計量；不能只報W/B或把revoked-but-alive當回收。
2. **W2 完整失效範圍**：key 是正規化 watch path、recursive 與 registration generation，不按每個事件新增 key。目錄 callback 的 `Path` 表示整個 watch root 失效，檔案 watch 表示該檔案失效；不得拿最後一條 event path 代表其他路徑。一次 rename 的 src/dest 分別比對所有相關 watches，來源與目的地均不漏；同 key 的 root invalidation 可合併，跨 key 不覆蓋。非 recursive 只接受 root/直接子項。遞迴不跟隨descendant directory symlink：只記link本身metadata，不掃其external/cycle target；明確指定的backend root可為symlink，每scan重新錨定它當次指向的目錄。即使scan途中directory被換成symlink，也不得沿該descendant逸出範圍；檔案symlink只觀察link entry，不新增對外部target內容的監看承諾。
3. **W3 有限 debounce**：emitter事件在直接ingress的同一短鎖內更新首次pending時間f、最近時間e，ready deadline=`min(e+d,f+D)`，不經另一data queue才進pending。新事件不重設f；active期間只合併一份後續invalidation。第一輪已開始且阻塞、100事件全部完成ingress提交後才放行、之後不再注入時，首輪加補跑最多兩次；不同時間窗可合法多輪。poll interval P預設1秒，instance-only合法範圍0<P≤60秒且有限、非bool；P不是D。實際偵測／消費延遲還包括P、Tscan、D、worker等待與service自己的debounce，不宣稱2秒端到端。
4. **W4 Watcher 內就緒順序**：同 key 不重入；ready queue 每 key 一項，已 ready key 不被後來熱點事件插隊，補跑排在既有 ready 項後。對有限 N、callbacks 合作式在 L 內完成，已 ready key 的保守開始界為 active 剩餘時間加 Q×L；首次事件另計 D。這不是 service local/poll/rescan/GitHub 的公平承諾。非合作式 callback 下只承諾資源有界與可見等待，不假稱有限進度。
5. **W5 生命周期拒收**：stop/unwatch 在同一排程鎖下拒收舊 generation、取消 pending、記錄有界cleanup intent即返回，不等待callback、observer lock或backend cleanup。stop 後 watch 拒絕；unwatch 後成功重新 watch 使用新 generation，旧 handler、待取出項、callback 結束都不能替新 generation 排入補跑。重複 stop/unwatch 安全，pending 不復活；backend尚未安全拆除時re-watch可回可重試OSError，不假稱已註冊。
6. **W6 已開始 callback 與收尾**：callback 開始的線性化點是鎖內獲取 active lease；用 callback 前/完成後 barrier 驗 stop/unwatch 兩側。已取得 lease 是 in-flight，不宣稱 Python callback 或其外部副作用可被強殺。另提供顯式 `drain` 在不持consumer lock的外部上下文，以單一總 deadline S 等合作式收尾；stop/unwatch返回只表示撤銷完成，不代表drained。callback內drain拒絕、stop/unwatch不self-join。非合作式殘餘包含既有callback≤W、backend owner≤1及其未釋放backend資源，全部tracked且不補thread；遲到 completion 不重新排程。
7. **W7 可觀測與相容**：唯讀instance診斷保存W/active/pending/ready、backend generation/scan health/P/Tscan、callback failure、stopped/residual；錯誤按key隔離且不存無界歷史。保留watch/unwatch/stop signature、同步StubWatcher用途與watchdog缺席行為；constructor可加instance-only poll_interval_seconds，不加CLI/env。supplied stat/listdir逐操作及iterator消費共用per-scan failure latch，不能只catch外層_take_snapshot：descendant EACCES/EPERM/EIO等即使被watchdog吞掉仍拒絕整份partial candidate。只有descendant真正消失的ENOENT可略過；root錯誤與ENOTDIR/ELOOP/EINVAL等不當空目錄成功。初次任何不完整scan讓同步watch失敗；後續僅OSError保留同emitter上一成功snapshot、degraded且繼續poll，完整成功才healthy及root恢復通知，不產生partial snapshot假刪除。此成功是best-effort非原子FS快照，不宣稱跨路徑同時一致。不得吞非OSError、永久空snapshot或靜默殺死emitter。nonfollow dirfd能力缺席的平台須在watch成功前明確拒絕，不能宣稱跨平台無行為改變。
8. **W8 可重現交付**：離線 Event/barrier/fake clock 驗 W1–W7；虛擬 60 秒 churn 查 owned resources；真 watchdog 短 smoke 與現有 rename/watch/unwatch/recreate/Git-control/rescan/last-good 回歸。所有 wait 有界且 finally 放行。透過 Cortex 保存真正 TDD RED/GREEN、tests、文件/CLI、policy/review/merge gates，不啟動模型/GitHub 呼叫作為測試 fixture。
9. **W9 Consumer 鎖不循環**：真service `_install_watches` 持 `_watch_state_lock` 呼叫unwatch，callback的 `_project_id_for_path` 需要同鎖。unwatch不能等待該callback或藉S timeout才釋鎖；用此真caller負例驗撤銷立即完成、caller釋鎖後callback正常結束、不產生虛假stuck/provider residual。backend owner不執行consumer callback、不持watcher state lock進Observer操作；event handler只做短狀態提交。
10. **W10 Backend 清理全程有界觀測**：Observer `stop/unschedule/remove_handler/schedule/start` 的鎖、stop hook與內部join可能在外層timed join之前阻塞，必須全部留在固定backend owner。drain至S返回backend-cleanup-timeout與精確活資源，不等其真正完成；owner卡住後新註冊拒收，舊key cleanup intents合併，不因重試、stop或re-watch增owner/queue/emitter。合作式放行後正常清理；清理未完成不得從diagnostics刪除引用。
11. **W11 共享 backend 與世代安全**：logical watch key/handler與Observer實際ObservedWatch分開記錄，依實際backend key（root/recursive及adapter參數）共享、引用計數。撤銷A只detach A；同parent的B仍存活時不得unschedule整個ObservedWatch。只有最後handler且generation仍相符才拆backend；teardown已開始則同backend新註冊先拒收/待正式重試，不能讓舊completion拆掉新generation。雙檔共享、撤A留B、最後一個撤銷、同path re-watch/遲到detach均須真watchdog fixture覆蓋。
12. **W12 固定拓樸下事件記憶體有界**：pre-schedule安裝instance-local直接ingress；raw event backlog=0，emitter的immutable backend-generation proxy直接匹配src/dest並合併到每logical generation唯一pending/ready，不另存K-ring或overflow Path集合。入口只取短watcher鎖，不等容量、Observer lock、consumer或stop，不把Full送至emitter。Observer queue facade只保留一個stop latch，未完成queue task計數恆0；get不返回data，不能closed後忙迴圈。舊backend proxy不因ObservedWatch.path相等誤投新backend代。保留完整相關roots，內容invalidation不撤銷subscription；stop/unwatch才revoke。固定拓樸下交替兩path/跨A-B/持續rename/teardown卡住必須以pending/ready/retained-memory oracle驗有界，不能把無界記憶體列availability residual。

## Boundary

callback 是失效通知，不是檔案事件日誌。目錄 root 通知由既有 service `_handle_fs_event` 解析；workspace root 走全量 refresh，可覆蓋多 project，project root/子目錄走所屬 project。A 必須用真 consumer 加 fake store 證明這個相容性，不能只測最後 Path。

明示 residual：service 第二層 Timer、同步全量 scan 的成本、poll/rescan/GitHub 全來源公平性、provider timeout/last-good、新舊 source revision 合併、durable snapshot/event publication fence，全部仍屬 B/C 與母 #827 R1–R8。watcher 無權撤销 consumer 已開始的外部寫入，本項 stop 只 fence watcher admission/pending/completion。既有service可呼叫非阻塞stop；其整體shutdown/drain整合由C在安全lock邊界完成，A以自己的stop→drain測試證明合作式收尾，不暗改service或宣稱其stop同步等待全部資源。A 不宣稱 Monitor 整體 thread/freshness 或部署 gate 完成。C 必須合計 watcher callback、backend owner與 refresh workers，不能各池各自稱 W 而漏算總量。

接受的觀察取捨：polling不保證兩次scan間短命create/delete/rename、或相同inode/mtime/size的改寫即刻觸發；這些可能延後至既有rescan。當下config預設full poll=60秒、targeted rescan=300秒，不是永遠固定值或硬端到端期限。此consumer重讀最終狀態，不是filesystem event log；不新增每poll強制refresh、不宣稱不漏瞬態或偵測仍與native同延遲。P與snapshot/diff成本依有限監看拓樸M、backend數N計量；短命事件盲點與scan阻塞須明列診斷/成本，不能擴大成漏已接納invalidation的豁免。

本候選單production domain=0/state=2，完整fix-standard現行8/Red；只有#831真正落地後完整accepted stability改0才条件式6/Yellow，仍須獨立review與重新gate。新增第二個production模組需重評domain，不降低concurrency state以求入場。
