# P1 runtime intake：完整性、真實 sizing 與人工分拆建議

日期：2026-09-07；純 gate 初次查證09:14 UTC，同 identity 亂序校正後於09:27 UTC重跑。隔離分支 `feature/refine-p1-runtime-intake-20260907`，authoring 基底 `60a3ffa867377b0c86fa2f10e91fb9820d91938c`（#832 merge）。
隔離分支的 intake 子任務範圍只有三組 accepted spec/design/todo 與本報告，不是整合 PR 的完整 diff 清單。隔離 authoring 及此輪文件校正當下，作者未執行 commit/push、建 issue/run、改產品 code/tests、寫 `.cortex`/operator/runtime/服務，也未操作 #822；主流程後續提交/整合另含 `.cortex` links、plan/OpenSpec ledger、changelog，以其獨立紀錄為準，這段不是對未來 Git 狀態的宣稱。

## 已交付 intake 與保留範圍

- #827：`docs/superpowers/specs/monitor-refresh-thread-backlog-{spec,design}.md` 及同 work_item todo。
- #819：`docs/superpowers/specs/daemon-tick-clock-not-idle-{spec,design}.md` 及同 work_item todo。
- #825：`docs/superpowers/specs/executor-durable-backoff-{spec,design}.md` 及同 work_item todo。

所有 spec/design 的必要標題為 Requirements/Decisions；todo 保留 Tasks，僅新增 truthful sizing metadata、source/tests/documentation/CLI 任務與補強負例。
機械讀取基底 todo，比對每個原始行仍按原順序存在：#827 68 行、#819 69 行、#825 69 行全部保留，未刪改 #832 的驗收或 residual。

doc-coauthoring 用於結構與 fresh-reader；test-playbook 用於 actor/side-effect owner、狀態/非同步邊界、Event/fake clock、故障注入、明確 oracle 與測試分層。未另建測試框架，產品測試仍待 Cortex 實作。

## 現行 runtime 純 gate：三項均 Red

執行時 import 現行 runtime checkout 的 `paulsha_cortex.coordinator.planning/manager`，runtime HEAD=`79ba644780bf1c697c722ac24a297e7d02416100`，從 checkout 外執行 Python 並設 PYTHONDONTWRITEBYTECODE=1；不是用 authoring checkout 假裝已部署修正。
輸入 packaged fix-standard：gate_spine=2、cards=9、persona_binding=9；規則為 runtime 全集 R-09/R-16/R-19。三組 artifact 都逐件 accepted、complete=True、零 missing/marker。

| Work item | domain | state | acceptance | stability | orchestration | 現行真分數 |
|---|---:|---:|---:|---:|---:|---|
| monitor-refresh-thread-backlog | 1 | 2 | 2 | 2 | 2 | 9/Red |
| daemon-tick-clock-not-idle | 0 | 1 | 2 | 2 | 2 | 7/Red |
| executor-durable-backoff | 2 | 2 | 2 | 2 | 2 | 10/Red |

declared rubric 來源為 [#208](https://github.com/hamanpaul/paulsha-cortex/issues/208)：domain 0=單模組/單流，1=2–3 模組，2=≥4/跨 subsystem；state 0=純/local，1=schema/backward compatibility，2=concurrency/atomicity/migration 任一。
#827 的三 production 模組與 publication concurrency、#819 的單 daemon 模組與 backward-compatible lane、#825 的多模組/cross-process durability 均按此宣告，沒有為入場降分。

另以 `_evaluate_yellow_plan_review` 單獨檢查三組：ready=True、checks_run=(completeness,contract_compatibility,envelope)。
envelope 使用目前 `load_model_identities()` 及真 `_plan_review_envelope_lookup`，回 `bypass=envelope_unavailable`；不是 measured capability PASS，也不是 Red 的 dispatch 許可。
#819 初次 gate 回 missing-task-for-rule:R-09：原 changelog 敘述在 continuation line，機械 task reader 沒採到；新增獨立明確 changelog task 後重新驗過，沒有改 sizing 欄位或刪 gate。

純檢查用的呼叫鏈與參數：

```text
PlanningArtifact(spec/design/plan=todo，逐檔真文字)
 → assess_planning_completeness(artifacts)
 → compute_sizing_score(plan_artifact=todo, completeness_report=report,
       gate_spine_count=2, applicable_contract_rules=ACCEPTANCE_SURFACE_RULES,
       cards_count=9, persona_binding_count=9)
 → sizing_band(score.total)
 → _evaluate_yellow_plan_review(artifacts,
       envelope_lookup=_plan_review_envelope_lookup(memory_run, load_model_identities()))
```

memory_run 只有 steps=()、primary_domain=None、model_chain_override=None、上述 band 與非 live 的診斷 run_id，未寫 registry、執行 probe 或啟動模型。
目前未跑產品 unit/integration/full tests、CLI 進程、CI/policy、installed/live canary；真 CLI/fixture smoke 已列為各 todo 的交付任務。未以本報告或內容 hash 偽造 runtime authority/evidence。

## Fresh-reader 發現、處置與重審

首輪 FAIL：兩項 #825 MAJOR；作者逐條核對後補強，重審 PASS（只限九件 intake 一致性）。

1. 無年份 reset hint 的「最近未來」可能把今天稍早的過期訊息推至明年。設計改為明示基準年解析，過去即 None；跨年正例須可信年份/reset 證據，未證實 rollover 是負例。todo 增同日15:00/12:23、年界/DST 反例。
2. atomic replace 失敗保留舊合法檔，不代表新 identity 的 cooldown 已記住。設計改以既有 durable terminal/provider_outcome 作 pending intent，store acknowledgment 只在成功合併後更新；每次 admission/restart 先對帳，無法合併仍 unknown。新增舊檔僅 A、B 終局已持久但寫入故障、fresh process 查 B 的負例；不能只靠 process-local flag/error sidecar。若 terminal retention 不足，明示相依缺口並停下處理。

#819 另把 D1 明訂只補 not-idle，不擴張其他 skipped reason。#827 公平依賴/非合作呼叫殘餘、#819 cgroup/manual/installer residual、#825 D4–D7 全部保留。

主流程再審另指出一項 MAJOR：原 R2 只明文保護別的 identity，同 identity 較舊未 ack 終局晚到可能覆寫較新較長 reset。已在 R2/R4、design D2.1 與 todo 補 immutable event-time/key 去重排序 fold、aggregate deadline 單調保護、expire 後 ack/tombstone 不遺忘、跨 process/restart/permutation 的具體負例。reset=1000 先寫、reset=300 的舊事件晚到仍保留1000+margin，hits 依 event-time episode 計算，不依 arrival now。
本票不授權提早縮短 active cooldown；較短新 reset、成功 job/auth probe 或 active clear 都不能解封，只有自然到期且 pending intent 收斂才可無 active backoff。主動 reset reconciliation 留後續 quota 工作。
這兩組新增機械不變量使 #825 invariant_count 由9增至11；domain/state 仍2/2，未降低 sizing 或跳 gate。先前 fresh-reader PASS 是該輪文件狀態，本次再審是否通過由主流程 reviewer 另行記錄，不能沿用舊 PASS 冒充。

## Source 與不耗模型的測試錨點

行號對 authoring 基底；後續變更以函式名重新定位。

- #827：`monitor/watcher.py:130` 每事件 Timer；`monitor/service.py:279` 未映射事件同步 scan、`:317` 第二層 Timer、`:332` refresh_project；`monitor/work_api.py:496` refresh、`:502` 全域鎖裡 provider scan。既有 `tests/test_stage9_project_monitor_service.py` 的 burst/rename/watch/rescan/last-good 保留，新增 backlog fixture 用 Event 阻首輪→100 events→最多一次補跑；fake clock 60 秒、owned worker/pending、跨來源公平與 commit fence，各 bounded wait/finally 收尾。
- #819：`coordinator/manager_daemon.py:1456` skipped 判定、`:1458` not skipped 才推時鐘、`:1474` failure 已推時鐘；`:1344` periodic_kwargs、`:1661` main。重用 `tests/test_manager_daemon_tick_backoff.py:16` _FakeClock、`:208` cadence；85 rounds 的精確 oracle 為 [10,20,30,40,50,60,70,80]。env/CLI 值域與 kwargs 以 monkeypatch/stub，真 parser 負例只能在候選修正後隔離執行。
- #825：`coordinator/provider_backoff.py:42` 的 corrupt→empty 僅為既有 GitHub 契約，不能當新 store oracle；`manager.py:3975` reroute、`:8466` preflight；`autonomy.py:592` dispatch_ready、`:662` pending 副作用；`launcher.py:1430` executor property；`provider_outcome.py:89/126` 既有 reset_at。重用 `tests/test_provider_failure_recovery.py:206`、`:398`、`:454` 的候選/independence fixtures 及 slice/provider backoff tests，新增 store fault/restart/replay 與全部 request consumers，無真 quota 打滿測試。

## #831 後的重新裁決

若 #831 實際修正完整 accepted stability=0、其他維度/組合不改，純算術預期 #827=7/Red、#819=5/Yellow、#825=8/Red。這只是明示條件的預估，不是已實跑的新 runtime 分數；必須待真部署 revision 再算。
#819 保留整體 scope，不預先拆。#827/#825 若仍 Red，以下由 root 人工建立受治理 child spec/issue，逐件正式派入 Cortex。
#833 自動 planner decomposition 尚未實作，本報告沒有建立 child、issue、authority 或 run，不宣稱 Cortex 已自動完成拆分。

## #827 人工分拆候選與 parent coverage

以下是可獨立驗收的最小責任切點建議，不是已 accepted 的 child。每件假設完整 fix-standard：acceptance+stability+orchestration 三維小計現行為6、#831生效後為4；加上各件 domain=0/state=2，總分才是表格的8/Red→條件式6/Yellow，現行均不可直接派builder。domain=0 只在真的限定單 production 模組成立；concurrency/atomicity 一律 state=2，沒有降掉。

| 候選／依賴 | Production 邊界 | 獨立驗收與 parent AC | domain/state | 現行投影 → #831條件投影 |
|---|---|---|---|---|
| M1 排程核心 | 新 `monitor/refresh_scheduler.py` 單模組 | W/pending bound、D、ready FIFO、generation/stop 協議；R1/R2/R3/R6/R7 核心 | 0/2 | 8/Red → 6/Yellow |
| M2 watcher 接線，依 M1 | `monitor/watcher.py` | 移除事件 Timer、watch/unwatch/rename/recreate、無旁路增 thread；R1/R2/R8 | 0/2 | 8/Red → 6/Yellow |
| M3 work-model commit，依 M1 協議 | `monitor/work_api.py` | I/O 與 commit 鎖分離、source revision 合併、last-good、source 診斷、stop durable fence；R3/R4/R5/R6 | 0/2 | 8/Red → 6/Yellow |
| M4 service 接線，依 M1–M3 | `monitor/service.py` | local/poll/rescan/GitHub/全量 scan 同邊界、server event fence、公平時限、S 收尾；R1–R7 wiring | 0/2 | 8/Red → 6/Yellow |
| M5 parent 整合驗收，依 M1–M4 | tests/docs-only，不新增產品修補 | 原全部 todo + 60 秒 fake churn、短 real-thread、CLI 與 snapshot/stop harness、實際部署量測；R1–R8 | 0/2 | 8/Red → 6/Yellow |

M1/M3 必須先明定 source-generation/publication 協議，使後續單模組接線可重用；若真正實作需同時改另一模組，不可照抄此 0 分，回 1 分並重評/再拆。M3 的 provider finite timeout 能力未證實，需在 child 設計先處理相依，不用更多 thread 吸收風險。
M1–M3 的 core 單測通過不代表 monitor 功能已交付；parent 保留全部驗收，直到 M4/M5 接線與最後 runtime gate 才可宣稱 thread/freshness/stop 問題完成。

## #825 人工分拆候選與 parent coverage

| 候選／依賴 | Production 邊界 | 獨立驗收與 parent AC | domain/state | 現行投影 → #831條件投影 |
|---|---|---|---|---|
| E1 durable store core | 新 `coordinator/executor_backoff.py` | schema/三態、atomic writer、terminal ack/replay、交錯更新、期限計算；R1/R2/R3/R4 | 0/2 | 8/Red → 6/Yellow |
| E2 reset parser | 新 `coordinator/reset_hint.py` 純函式 | timezone/year、Retry-After、過期/DST 拒收、provenance；R4 | 0/0 | 6/Yellow → 4/Yellow |
| E3 classification 接線，依 E2 | `coordinator/provider_outcome.py` | 不改 taxonomy/authority/五鍵形狀，structured 優先；R4/R5 | 0/1 | 7/Red → 5/Yellow |
| E4 terminal reconciliation + workflow，依 E1/E3 | `coordinator/manager.py` | 兩 lane 終局 intent、admission前對帳、preflight/reroute、全冷卻/unknown、pin與forced retry；R1–R6/R8 | 0/2 | 8/Red → 6/Yellow |
| E5 launcher 公開 model | `coordinator/launcher.py` | 公開唯讀 property 與 executor/model 相符，原行為相容；R7 seam | 0/1 | 7/Red → 5/Yellow |
| E6 slice admission，依 E1/E4/E5 | `coordinator/autonomy.py` | pending/worktree 前共用 observation、skip 保持可派、無副作用；R1/R3/R7 | 0/2 | 8/Red → 6/Yellow |
| E7 request consumers，依 E4/E6 | `coordinator/manager_daemon.py` | dispatch/retry-build/fanout/tick/inspect 的 skip/unknown 與空列表安全；R7/R8 | 0/2 | 8/Red → 6/Yellow |
| E8 parent 整合驗收，依 E1–E7 | tests/docs-only，不新增產品修補 | 全原 todo、fresh process+fault+兩lane真接線、CLI、部署重啟；R1–R9 | 0/2 | 8/Red → 6/Yellow |

E2 的 state=0 只因它是無 writer 的純 parser；所有涉及 concurrency/atomicity 的 child 保留 state=2。E3/E5 是單純 backward compatibility，沒有把 durable work 藏在其中。
E1 應提供必要 reconciliation observation/ack 協議但不自行新增 registry writer；E4 必須證明 canonical terminal retention 與 fresh-process對帳，未完成前 core 不可被當作安全 admission。E4 若 review 發現單檔含兩個不能一起驗收的流程，可再分 manager 終局記錄與 workflow admission，仍各 state=2，不能用少報不變量硬包。
`dispatch_reroute` 是否需要 registry 新欄位 gate 尚未證實；若必要，另立 `registry.py` 單模組相容/atomic evidence child，不能偷塞進 E1/E4 的模組計數。E7 也需核對 inspect 真正 owner，若在其他檔則另拆 read-model child，保留 R8 coverage。
在 E4/E6/E7 接線完整前，不把部分上線宣稱為跨 lane 安全層；parent 最終保留全部原驗收與獨立整合 gate。#830 非 Job 決策修正與本票 skip/wait 必須相容，不用假 job_id 補洞。

## 未完成與禁止宣稱

- child 表只供 root 人工規劃，尚無正式 authority/issue/sizing gate receipt；待 #831 真 runtime 重新計分及每件自身 spec/design/todo 完整性驗證。
- parent 不因 child core 綠燈而 done，須保留跨 consumer、reset/replay/restart、stop/publication 與實際 runtime 的整合證據。
- #825 不交付共享 quota pool、forecast、reservation 或動態 model/agent/effort。refine plan D4–D7／R05/R08/R09 仍未完成；未知餘量不等於滿額，換 model 不證明換了帳號池。
- 本次未跑 full pytest/CI/PR-context policy 或真 CLI，亦未提供產品 merge/部署/現場改善數據。intake accepted、reader PASS 與純函式 ready 不可提升成那些證據。

## 主流程補充的 canary 證據（此子任務未操作或重新查 live）

主流程於 2026-09-07 17:16 Asia/Taipei 左右提供：#822 isolation、tdd-red、GREEN 每張新卡均再碰 Claude 429，各由合法 Codex fallback 接續；verify 的 reviewer/anthropic 新卡又碰 Claude 429，builder 此時為 Codex。
原事件欄位為 rate_limit_info.resetsAt=1788774600、rateLimitType=five_hour，unifiedWindows 涵蓋五小時/七天；主流程觀測 registry provider_outcome 只有 structured 429，沒有 reset_at。
這是主流程提供的現場摘要，不是本報告作者新取得的原始 log/hash，也不假造 terminal evidence。

本票既有 R1 跨 tick/card/run/restart、R4 structured reset 優先與 R6 pin/independence 已涵蓋對應回歸：同一 cooldown 必須跨 phase/card admission 生效，不能每張新卡重燒；reviewer 不得 fallback 回 builder 的 independence domain。若唯一合格 reviewer 仍在 cooldown，應回有診斷的等待/恢復路由，不犧牲獨立性。
reset 欄位從原事件到 classification/registry 遺失是 #825/#826 的 producer→consumer 交叉錨點，實作時先用這種 shape 的匿名 fixture 驗證；本 intake 不聲稱已修 extractor 或已取得 usable reset。五小時/七天多窗口只作來源保留，本票仍不等於 D4–D7 共享 quota window/forecast 完成。

## 最終九件受測內容 SHA-256

```text
366ececf977ace98d280bc92c5d8241134f75065c07373a529e62a8ba15071b1  docs/superpowers/specs/monitor-refresh-thread-backlog-spec.md
df334de08c1ae0f03e7ef53a8d20648f256dd94ec0d6026a20c026c0c1978633  docs/superpowers/specs/monitor-refresh-thread-backlog-design.md
8e4eca204fea3cce70de35f4cad8908fc1081a7d1f98559c74ee0dcf3852f04d  docs/superpowers/workstreams/monitor-refresh-thread-backlog/todo.md
46b4a089b9b8f86278c443484db9f6ad55d7392fa88a0667979514d2f727b688  docs/superpowers/specs/daemon-tick-clock-not-idle-spec.md
6bbc7d2f2089b130709ba8107255d66da55601bc18eef5c79bddf405de222c90  docs/superpowers/specs/daemon-tick-clock-not-idle-design.md
de12c8460c109ec7c054c536cb28021ead4f3b68030a9d799aca28d09e3abaa8  docs/superpowers/workstreams/daemon-tick-clock-not-idle/todo.md
419bea738e10cf9cde961172491a23a09785cb98610e6e2a557850b251af08d9  docs/superpowers/specs/executor-durable-backoff-spec.md
30db7e8d79de4bde83c6cd1388658a17699bab2b6e4864572c46b80bbb4d65ad  docs/superpowers/specs/executor-durable-backoff-design.md
0ed7c907f4185e32286c009df5c54a0c55ba8f8e793b0c244a2216e67bf587ec  docs/superpowers/workstreams/executor-durable-backoff/todo.md
```

這是 intake 內容核對資料，不是 gate evidence；後續修改須重跑。
09:27 UTC校正後機械檢查：九件 SHA-256 全相符；git status 恰為授權十檔（3 個 tracked todo 加 7 個新檔）；逐檔 whitespace 無診斷，三份原 todo 行完整保留。三組 planning/sizing/Yellow 純 probe 重跑為9/7/10 Red、gate ready=True、envelope_unavailable bypass；#825 invariant_count=11已被本輪讀取，沒有沿用舊檔驗證結果。

## 主流程整合與獨立重驗（2026-09-07 09:34 UTC）

作者交付後，root以ff-only將authoring基底推進至#834 merge
`217ff5b701f2a6ae54a20ab07212d3acef1d2114`；九件intake bytes保留。
補三組spec/design的正式links、changelog、十四類總帳的#835–#845 owners與真實canary狀態；
不包含產品code或#828 operator artifacts，也未啟動這三票。

獨立review首輪同identity亂序MAJOR已逐條核對並修正，複審PASS；reviewer以純模型
確認含無reset事件的六種排列收斂，仍未驗產品持久化/跨process。
Root另用現行runtime79ba6447的真planning函式重跑，#819/#825/#827依序7/10/9 Red，
完整性/contract通過、envelope unavailable bypass仍明示；OpenSpec strict與diff-check通過。
這是規劃check，不是產品RED/GREEN或實際能力評測。post-commit preflight/CI另由提交流程記錄。

doc-coauthoring fresh-reader以無前文上下文核對五題，正確區分B0與產品交付、Red與
Yellow helper、最小退避與完整quota、producer/qualification授權，以及recovery/delivery分工。
初讀把三維機械小計6/4誤讀為child總分；root核對公式無誤後補明三維範圍與
domain/state加總8/6，原Red限制不變。fresh-reader重讀PASS，歧義已處置。
