---
status: accepted
work_item: monitor-watcher-bounded
domain_breadth: 0
state_consistency: 2
invariant_count: 12
artifact_classes:
  - source
  - tests
  - documentation
---

# Watcher-only A child：固定 worker 與完整失效通知

## Boundary

Owner [#853](https://github.com/hamanpaul/paulsha-cortex/issues/853)，work item `monitor-watcher-bounded`；母項`monitor-refresh-thread-backlog` / #827，umbrella #829，基線fb31083e。未來唯一production檔`paulsha_cortex/monitor/watcher.py`；tests/docs/changelog及既有`.github/workflows/tests.yml`的test-only watchdog安裝可依下列任務變更，不改runtime依賴。不得改service/work_api/provider或新增scheduler第二production模組仍宣稱domain0。A∥B→C，C承接母R1–R8整合。Root已查重後建立/readback #853 OPEN，並在本PR加入唯一`.cortex` spec/design/todo links；當前為repository intake／unfrozen，未merge、未正式freeze、未dispatch，仍8/Red禁派。未來canonical build branch為`feature/853-monitor-watcher-bounded`，changelog fragment為`changelog.d/monitor-watcher-bounded.md`；此處不建立分支或fragment。

下列「四檔authoring」是原作者時點，不代表整合PR只有四檔或不需planning preflight。當前整合branch為`feature/refine-runtime-intake-20260907`；root的planning preflight／CI與未來Cortex產品TDD／OS／CLI／policy／review gates分別留證，前者不代替後者，以下產品Tasks仍全未完成。

## Tasks

- [ ] **Intake source/tests/documentation**：讀accepted候選W1–W12、D0–D6及母R1–R8映射。Root已選watcher-local受控polling＋直接root ingress，不裸換PollingObserver、不改全域watchdog；原K-ring與native待選設計已撤回。Root轉交fresh reviewer `watcher_polling_r4` 的R4內容PASS／0 MAJOR，非產品PASS；保存本版pure gate/hash，正式進件與產品review仍須留證。完整fix-standard目前8/Red、禁派builder，#831真runtime後6/Yellow只是條件預估，不降state或刪欄位。產品驗收以下仍全未完成。
- [ ] **Tests/TDD RED**：新增 `tests/test_monitor_watcher_bounded.py` 的fake Observer/clock、barrier與owned診斷fixture，先在舊產品碼可靠重現W/pending/最大D、latest-path漏跨project、stop/re-watch late completion缺陷，保存真RED，不只檢查字串或複製演算法。
- [ ] **Source W1/W3/W4**：只在watcher.py內移除事件Timer，固定callback W=2、每generation active≤1/pending≤1/ready membership≤1、FIFO及補跑尾排。d=500ms/D=2000ms/S=2s，poll_interval_seconds預設P=1秒、0<P≤60且有限/非bool；不加CLI/env。deadline=min(e+d,f+D)，P/Tscan/consumer debounce不混入D或冒稱S=端到端。卡住worker占W，不建無界queue/替代thread。
- [ ] **Source W2 + tests T3/T4/T15**：目錄用整watch-root invalidation、file用固定watch file；src/dest分別cover相關watches，非recursive邊界/file刪除重建不漂移。recursive不跟隨descendant directory symlink，file/dangling link只記link entry；明確backend root可跟隨symlink但每scan重錨，保留lexical callback Path。真service消費端加fake store驗workspace root全量包含A/B、project入口相容，不改consumer production；不能只保留最後一event。
- [ ] **Source W5/W6/W7/W9 + tests T5/T6/T7/T9**：generation/active lease線性化；stop/unwatch只非阻塞revoke/cancel，不進backend、不等callback，重複安全。顯式drain在consumer lock外以總S等合作式收尾，callback內drain拒絕；舊callback完成不能重排。保留callback≤W及backend owner/活資源完整residual，區分revoked/drained，不宣稱外部publication已fence。
- [ ] **Source W10 + tests T10/T12**：watcher.py內單一backend owner B=1處理所有Observer阻塞控制操作；每既有key cleanup intent1、唯一新註冊in-flight槽，無每操作thread/Future無界queue。backend卡住/未收斂retirement時新watch回可重試OSError；同步watch只有真backend完成且generation有效才成功，timeout撤銷且late schedule結果精確補償。drain≤總S返回精確backend-cleanup-timeout與未釋放inventory，重試不可加owner/emitter/queue。
- [ ] **Source W11 + tests T11**：logical handler與backend ObservedWatch分層、refcount和backend generation；只撤A handler不傷同parent B，最後有效ref才unschedule。teardown開始後同backend re-watch明確拒收待重試，completion按實際handle+generation，不讓舊detach拆掉新watch；全部Observer操作留single owner。
- [ ] **Source W12 + tests T13**：在任何schedule/start前替換該local BaseObserver instance `_event_queue`為直接ingress facade，小PollingEmitter的proxy封存實際backend token。put在短watcher鎖匹配src/dest/註冊時file-dir資訊，直接合併唯一logical pending，raw backlog=0，不另留K-ring/Future/overflow Path集合。Observer只等一個stop latch；local on_thread_stop先wake再super cleanup，真stop flag→sentinel→run退出；unfinished_tasks恆0/get不返回data/task_done意外呼叫拒絕，closed不得busy-spin。old backend即使path相等也不能投新代；ingress不等容量/Observer鎖或拋Full殺emitter。
- [ ] **Source W2/W7 + tests T14/T15**：BaseObserver公開emitter_class seam配小PollingEmitter adapter及DirectorySnapshot supplied stat/listdir，不複製walk/diff/poll loop。每scan root-dirfd重錨、descendant逐component O_NOFOLLOW/O_DIRECTORY、final stat follow_symlinks=False、scandir(fd)，descriptor≤4/finally關閉、component成本計Tscan。stat/listdir建立與iterator中途OSError先記同一bounded failure latch再raise，library吞錯返回partial也必須丟棄；僅descendant消失ENOENT可略過，root錯誤與EACCES/EPERM/EIO/EINVAL/ENOTDIR/ELOOP等不可當empty成功。初次不完整snapshot令watch失敗；後續OSError保同emitterlast-good/degraded/一次root hint，完整成功才healthy與恢復通知，partial不得生假FileDeletedEvent。未知fatal不吞、不永久空snapshot；保留inventory及unwatch→watch路徑。缺dirfd/follow_symlinks/scandir(fd)/O_NOFOLLOW/O_DIRECTORY能力在watch成功前拒絕，不偷回字串follow/native或聲称全平台相容。
- [ ] **Tests/真鎖負例 T9**：真service `_install_watches`持 `_watch_state_lock`呼叫unwatch，同時callback的 `_project_id_for_path`等待同鎖；斷言unwatch在gate未放行前即返回而非熬到S，釋鎖後callback完成/drain成功。此自造循環不是provider residual，不得以timeout過關。
- [ ] **Tests/真watchdog負例 T10–T12**：真BaseObserver配受控fake emitter、不啟OS backend，重現內部`join(None)`超S及同parent A/B共享ObservedWatch；修正後外部revoke/drain有界，阻塞100次重試不增owner/queue/資源。驗撤A留B通知、last-ref teardown/re-watch、schedule timeout後latehandle補償與失敗不進service watched集合；finally釋放全部gate/join。
- [ ] **Tests/固定拓樸backlog T13/T14**：真BaseObserver/EventEmitter雙backend鎖阻塞、W callback barrier與直接ingress，交替兩path10k/100k、兩root rename、固定seed60秒；object inventory驗raw0、每generation pending/ready/active≤1、control latch≤1、unfinished0、零closed busy-spin。全部100次put返回後才放callback gate，驗最多一次補跑；舊backend proxy/path-equal re-watch不得污染新代。tracemalloc retained delta有明示容差/不隨event倍增線性成長；A teardown持Observer鎖時健康B仍提交/通知。真polling OS source-stall/smoke另驗，不把fake emitter說成OS結果。
- [ ] **Tests/R3 regression T14/T15**：初次與steady-state各注入child stat EACCES、recursive listdir即時/迭代中途EACCES、EIO/EINVAL、root/descendant ENOENT與非OSError；真library先驗RED吞錯，再驗latch不進partial diff、last-good物件/健康轉換/恢復hint。memory VFS另驗link mode不递迴、syscall spy驗每component nofollow；真OS external/cycle/dangling/file link、directory→symlink barrier競態、root symlink target跨scan更換及HEAD/refs/nonrecursive分開跑。FD≤4且finally歸零、無跨scan stale fd/inode，缺平台能力明確失敗；初次/恢復都須可機械反證，不能以root層PermissionError fixture代替descendant覆蓋。
- [ ] **Tests W bound/D/FIFO**：W=2 barrier占滿、A首輪開始後注入100次再放行，驗A首輪+補跑≤2、pending1；不同窗允許多輪。fake clock d=0.1/D=0.5/L=0.2秒驗首次pending+D ready與B不被A插隊；record owned counts，不用全程序active_count當唯一oracle。
- [ ] **Tests 60秒虛擬churn/real smoke**：固定seed含watch/unwatch/re-watch/rename，W+B、pending/tombstone/retiring backend及底層helpers均有界；backend卡住後資源以N0+既有in-flight上界凍結，不用live key減少掩蓋殘餘。短真watchdog tmpdir smoke與 `tests/test_stage9_project_monitor_service.py` 全部既有burst/rename/watch/unwatch/project recreate/Git-control/rescan/last-good回歸，stop後顯式drain再查owned基準。watchdog缺席不得全skip後宣稱production已驗；所有wait bounded/finally釋放join。
- [ ] **Documentation**：同步內容invalidation vs subscription revoke、受控polling W+B/D/S/P、raw0/唯一pending與ready/stop latch、watchdog6及dirfd平台能力相容面、同步watch成功/失敗、per-scan failure latch/last-good恢復、nonfollow descendant與明確root symlink邊界、drain/inventory/refcount/generation。成功scan是允許消失競態的best-effort視圖，不宣稱原子一致；有限fd與component/depth成本計入Tscan，不假M對所有拓樸固定。明列短命與sameinode/mtime/size盲點可能等既有rescan，當下full60秒/targeted300秒非永久或硬期限；成本P+Tscan+D+排隊＋consumer debounce，正常poll不強制refresh。service第二Timer、全來源公平、durable fence/整體drain與部署仍B/C/母票；固定拓樸記憶體無界不能列residual，不關母票。
- [ ] **Changelog R-09**：產品PR新增並commit `changelog.d/monitor-watcher-bounded.md` 及 `CHANGELOG.md [Unreleased]`；本次intake四檔authoring不冒稱完成這項產品交付。
- [ ] **CLI R-16 + tests R-19**：不新增CLI選項，核對monitor help/usage仍相容；在候選checkout外實跑 `python3 -m paulsha_cortex.cli monitor --help` 與隔離fixture config `monitor --once` JSON smoke，不連live socket/模型/GitHub。先focused pytest再full pytest與現有CI，附命令/環境/實際輸出，不能用help成功代替watcher行為驗收。
- [ ] **Test-only CI dependency**：既有`.github/workflows/tests.yml`的pytest job安裝watchdog6.0.0並確認import/version；當下pyproject test extra僅pytest，不可假設CI已有watchdog。新core watcher tests缺依賴明確FAIL，不能全部skip偽GREEN；真OS smoke與原缺watchdogfallback的隔離mock分開。此為tests artifact，不擴production module/domain或變更全域/正式runtime套件。
- [ ] **Cortex delivery/review**：保存真RED/GREEN、全部tests/CLI、PR-context policy、git diff --check、expected head/CI/review/merge證據。未處置缺陷或gap判FAIL；明文有界residual且母票列管不單獨FAIL。產品code只能Cortex派工；任何部署/共享服務重啟由root另按in-flight ownership處理。
