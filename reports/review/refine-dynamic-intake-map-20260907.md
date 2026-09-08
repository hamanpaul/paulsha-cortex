# B2–B5 動態派工進件對照（2026-09-07）

本檔是唯讀查證後的規劃建議，不是 accepted todo、註冊資料或實作證據。未執行模型探測、
評測、產品測試、部署、服務重啟或 GitHub 寫入。產品實作仍須由 Cortex 正式進件執行。

## 查證基線與結論

- 原始碼基線：`79ba644780bf1c697c722ac24a297e7d02416100`；同時讀取整合 worktree
  的 `docs/superpowers/plans/2026-09-07-cortex-refine-complete.md` B2–B5、R05/R08/R09
  及 `docs/superpowers/specs/2026-09-07-cortex-execution-domain.md`。取樣時主 agent
  尚在整理計畫；後續已由 #832 合併。本文行號仍是原查證快照，不代表 installed runtime。
- GitHub 狀態為本次 `gh issue view`／issue list 唯讀結果；搜尋不是全稱性的無重複證明，
  正式註冊前仍須用 exact work_id／issue 再查 authority。下列新 ID 均只是建議，尚無新 issue。
- 既有能力可沿用：用量擷取、終局 outcome、分層候選排序、runtime preflight、spawn 間隔節流、
  PatchMUD CLI／envelope mapping、Trust Root 安裝 attestation。缺口是把這些組成
  **profile 可追、資格核可、額度有來源、預估有不確定性、並行保留、派前准入**的生產鏈。
- 最多 10 個進件單位如下；這是最小責任切面，不保證每單都符合 Green／Yellow。
  由 Cortex 真實 sizing 決定是否再拆，不能降低分數或刪去必要驗收以便啟動。
- B5 已有獨立 issue 的 instance／installer／restart 缺陷保留原所有權，列為既有依賴，
  不暗中塞進這 10 單，也不能因這 10 單完成就宣稱 B5 或整個 plan 已完成。

## 既有工作應重用，不能被過時文字帶回重做

| 既有項目 | 本次對帳與處置 |
|---|---|
| [#137](https://github.com/hamanpaul/paulsha-cortex/issues/137)、[#138](https://github.com/hamanpaul/paulsha-cortex/issues/138)、[#210](https://github.com/hamanpaul/paulsha-cortex/issues/210) | CLOSED 設計票；不重開為實作票。`model_identities.py:1656` 的 track-record-not-landed 註記仍是局部缺口，但不能推論「完全沒有 outcome」。後續實作以 #829 child 交付。 |
| [#325](https://github.com/hamanpaul/paulsha-cortex/issues/325) | CLOSED；`usage_extractors.py:205`、`registry.py:1384` 已擷取並持久化 job usage。沿用原生 missing／unsupported 語意，不新增另一套 token parser。 |
| [#205](https://github.com/hamanpaul/paulsha-cortex/issues/205)、[#534](https://github.com/hamanpaul/paulsha-cortex/issues/534) | 前者 CLOSED；後者 OPEN，但 `model_resolution.py:844,882` 已有 overlay／evaluated／packaged 分層排序。新選擇不得抹掉 per-work explicit pin、凍結集及既有 provenance；#534 需證據化收口而非重建全部排序。 |
| [#452](https://github.com/hamanpaul/paulsha-cortex/issues/452)、[#454](https://github.com/hamanpaul/paulsha-cortex/issues/454)、[#466](https://github.com/hamanpaul/paulsha-cortex/issues/466) | CLOSED；沿用 CLI／report 持久化／純 mapping／缺值 default。新補 execution-profile qualification，不把未測欄位升為測量通過。 |
| [#262](https://github.com/hamanpaul/paulsha-cortex/issues/262)、[#381](https://github.com/hamanpaul/paulsha-cortex/issues/381) | CLOSED；`_runtime_preflight_gate` 與 `SpawnAdmissionLimiter` 已存在。後者只是 per-provider spawn 間隔，不是 shared quota reservation，也不是執行期全序列化。 |
| [#483](https://github.com/hamanpaul/paulsha-cortex/issues/483) | OPEN，但 `launcher.py:1072` 已有特定型號 effort override。原票先做窄範圍驗收；一般化 descriptor／adapter 契約另屬 D1，不能擴原票 scope。 |
| [#581](https://github.com/hamanpaul/paulsha-cortex/issues/581)、[#600](https://github.com/hamanpaul/paulsha-cortex/issues/600) | OPEN，可直接重用下列 D2／D3；不要新開同義 producer／availability 票。 |
| [#490](https://github.com/hamanpaul/paulsha-cortex/issues/490) | OPEN，原 scope 是 retry-review 讀取 packaged+overlay 一致性。保留為相鄰回歸／既有票驗收，不把全域動態配置塞入此票。 |
| [#830](https://github.com/hamanpaul/paulsha-cortex/issues/830) | OPEN，合法非 Job 派工決策的消費契約；D7 可依賴其結果。原票明確排除 quota／fallback，不擴 scope。 |

`docs/superpowers/workstreams/cost-governance-cluster/todo.md`、舊
`docs/superpowers/plans/cost-governance-judge.md` 仍含過時 issue state、舊固定 roster、
`capable()` 未落地及四因子恆真 stub 等歷史描述；`dispatch-runtime-preflight/todo.md`
也仍未勾且記載舊固定路由。它們只能作歷史設計來源，不能覆蓋新版 #829 的資格與 unknown
契約。這次不改它們；root 整理權威時應標註 successor／歷史狀態，不重派已交付設計。

## 建議 child 邊界（最多 10 個）

表中 coordinator 檔名皆位於 `paulsha_cortex/coordinator/`；其他位置明列。

| ID／建議 work_id／issue | 最小範圍與 production 入口 | 驗收邊界、最小 fixture |
|---|---|---|
| D1 `execution-profile-contract`；#829 新 child | R08／B2。版本化 execution-profile、descriptor、角色需求與 adapter conformance；requested／resolved／observed 分開。`model_resolution.py:151,311,422` 相容性及 launcher contract；`launcher.py:797,1072,1341,1912` 的 effort／builder seam。此單擁有 profile key／canonical fingerprint 契約，D2 擁有 qualification 消費。 | 既有協定加入虛構 model+effort 僅需 descriptor；新協定允許新增 adapter，但不改 central resolver／quota／workflow 的產品分支。fake adapter 驗 unsupported 在 spawn 前拒絕、權限不得提升、工具／sandbox／terminal／usage conformance。舊 manifest migration、explicit pin 不變；未知角色不默認 build 的切換與 D2 同步。 |
| D2 `evaluated-roster-producer`；重用 #581，註冊前核 exact work_id | R08/R09／B2。限定原票五項：doctor 不硬要求單一 planning identity、unknown persona fail-loud、provenance 到 launcher、doctor 用公開 overlay API、評測 producer 接 evaluated roster。`model_profile.py:146,260,329`；`model_resolution.py:476,497,500,569`；`model_resolution.py:96`；`doctor.py:434,444`。D1 提供 profile schema。 | 版本化 report→qualification candidate→human review receipt→approved roster；未核可不得自動授權。fake report 覆蓋 effort／loadout／adapter 變更不能偷用舊評分、role coverage 缺漏保持 unknown；舊格式可讀但不自動升格。真正跨 repo 驗收需 PatchMUD #37 immutable fixture；完整角色覆蓋另依 #13。 |
| D3 `model-availability-preflight`；重用 #600，註冊前核 exact work_id | R09／B2→B3。沿用 `model_identities.py:688` CapabilityProbe、`manager.py:8466` runtime preflight；增加 profile-aware availability 快取、TTL、原因與 doctor 顯示。只回答「目前能否使用」，不授予品質資格或臆測餘額。 | fake clock／fake adapter 測 available、unavailable、unknown、expired、reset 後重探；doctor 與 dispatch 使用同一觀測。熱路徑不能重跑 PatchMUD 或付費推論探測；TTL 過期不無聲沿用成功。 |
| D4 `quota-observation-provenance`；#829 新 child | R05／B3。pool/account 非機敏 ID、profile 對多 pool/window 的映射、原生 unit、bounds、reset、TTL、authority、來源／缺值原因；涵蓋受管控制器／worker／review 與可讀的外部 session。接 `usage_extractors.py:205`、`registry.py:1384` 作 usage 證據，而非把 usage 當 remaining API；adapter 的 quota 能力入口由 D1 提供。 | fake provider 狀態檔／事件：短窗足夠但週窗不足、同池不同 model、不同單位、stale／損毀／missing；unknown 不變零或無限。tokens／request／credits 不任意互換；外部 host／session 無法讀取時保留 coverage gap 與安全 headroom。只做 shadow，不改排序。 |
| D5 `operational-usage-forecast`；#829 新 child，引用已關閉 #137/#138/#325 | R05/R06/R09／B3。在既有使用量與終局 outcome 上建立 attempt/profile/task-cohort 歸屬、成功品質與 infra 原因分層聚合，輸出可版本化的需求範圍、分位數、樣本數、信心及資料期間；不實作泛用 lesson-loop／RL。`engineering_outcome.py:123,157,199,221,418,527`；`work_actions.py:3384,5383` 終局 emitter。現有 job projection 沒有完整 profile/cohort，弱時間關聯不可當精確歸屬。 | fixture 含 planning/build/review、retry、固定 overhead、失敗已消耗、累計／增量及 reasoning subset；同事件 replay 不重計、重啟不歸零、infra failure 不降品質分。冷啟動／分布偏移輸出 unknown 或有來源的保守先驗，不能用零。先離線回測及 shadow 校準，PatchMUD prior 可缺省，不是唯一來源。 |
| D6 `shared-quota-reservation`；#829 新 child | R05/R12／B4。共享 account/pool 有唯一 reservation authority；一個候選涉及的全部 pool 原子預留，job/attempt 綁定、consume／release／uncertain、crash/restart reconciliation。沿用 registry job lifecycle seam，但不把 #818 的 jobs.json writer lock 冒充跨 quota authority。`registry.py:1384` terminal usage 是 reconcile 輸入之一。 | 兩 process barrier 同讀一單位，僅一個成功；多 pool 任一失敗全回滾；spawn 前後 crash、重複 terminal、clock reset。lease 到期但 job 仍活不能釋放；無法確認 liveness 則 uncertain 並拒絕超額預留。跨 instance 同 pool 同 authority，不以 model/executor 名稱分裂帳號。 |
| D7 `quota-aware-admission`；#829 新 child | R05/R07/R08／B4。在 `manager.py:8219,8285` 選擇及 `manager.py:9381,10006` 實際 spawn 之前、`autonomy.py:602` fanout 同步接線：資格／權限／獨立性／pin 硬濾→可用與需求範圍→可行集依既有分層排序→所選候選全部 pool 原子預留→決策 receipt；競爭落敗須重算，不先替每個候選扣額度。沿用 #825 最小退避、#826 原因分類、#497 generation、#830 非 Job 契約與 #381 spawn 間隔。 | 有獨立池合格候選才 fallback；同池換型號不算恢復；唯一合格 reviewer 同 domain 時等待。fake executor 注入派前可用、spawn 時 429／外部消耗，保留消耗與校準訊號；僅安全 attempt 邊界重選、不搬動執行中的 job。policy rollback 不丟 reservation／usage／decision。全不合格回合法等待及理由，不造 job_id。 |
| D8 `decision-status-projection`；#829 新 child | R10／B5。消費 D7 的不可變 decision receipt、等待／恢復原因、候選排除理由、有效 policy/config 與 freshness／needs_human；所有 section 同一 read model。接 `monitor/work_snapshot.py:36,188` 及 #828 producer；不得在 read model 修 workflow，也不另造 #828 的 actual/planned/last identity。 | fixture 綁 run/card/attempt/decision，顯示分別為 confirmed／estimated／unknown；等待與 Job 明確不同，needs_human 不消失，stale last-good 不偽裝現在值。消費 #828 immutable fixture 驗 retry、多卡、跨 repo；讀取前後 registry 不變。 |
| D9 `workflow-execution-identity-producer`；重用 #828，保留現有 owner | R10／B5。只做原 issue 的 actual executor/model/job/card/source/status 與 planned/last 分離。此 work 已有獨立現場所有權；不重派、不接收 D7/整份 R10 擴張，也不為本圖重啟其 manager。 | 沿用原票的 current-card in-flight→same-card last→unknown 規則；多卡／retry／跨 repo／未派工 fixture。下游 paulshaclaw #328 只按 upstream immutable merge SHA／fixture 驗收；issue open、程式已改、merged、consumer pass 各別記帳。 |
| D10 `loaded-runtime-attestation`；#829 新 child | R11／B5。報告正在執行的 CLI／service artifact、載入 revision、有效 config revision、instance/root identity 與 source-override receipt；重用 Trust Root installed inventory／activate／rollback receipt，不重造安裝器。入口 `trust_root/install/core.py:5950,6173,6268`、`trust_root/install/backend.py:1693`、`doctor.py:751,1129`、`deploy/installer.py:363`。 | 隔離 prefix 安裝、從 checkout 外執行 CLI；假服務／受控短生命週期 service 驗 disk 新 artifact≠舊 process 已載入、改 config 也有 revision 差異、rollback 可追。真正服務 fixture 必須另經部署 gate；本單不授權重啟 active manager，不包含 #800 配置重寫或 #582 drain 的修復。 |

## 最小依賴 DAG 與 owner

箭頭是 production 接線前置，不表示等待前置後才能寫 fake fixture。D1 契約先凍結，
D2/D3/D4 可並行；D5 可先沿既有 usage/outcome 做隔離 fixture，再依 D4 的 unit/authority
契約對接。D6 可在 D4 schema 穩定後以假需求獨立實作，不必等待預估器訓練資料。

```text
D1 ─┬→ D2 ───────────────────┐
    ├→ D3 ───────────────────┤
    └→ D4 ─┬→ D5 ────────────┤
           └→ D6 ────────────┼→ D7 ──┐
已交付 P1／#497／#830 契約 ────┘       ├→ D8
D9（#828，獨立現有 owner）──────────────┘
D10（可獨立準備）──────────────→ B5 installed/live gate
```

- shared files 要序列整合：D1→D2/D3 的 `model_resolution`／launcher 契約；D4/D5/D6
  的 registry 欄位以 additive schema 管理；D7 最後接 Manager/fanout，不能三線同改派工權威。
- reservation authority 是額度狀態的唯一寫入者；Manager 是 workflow 與 attempt 的寫入者；
  adapter 產觀測／執行結果；forecast 是可重建投影；Monitor 僅讀；human receipt 才核可資格。
- 設計上的 reservation 狀態至少能區分預留、已綁 job、已結算／釋放及 uncertain；
  transition 必須可重送、可恢復，不能靠 TTL 或 process 名稱猜測終局。
- 切換 policy 只影響新決策；既有 attempt/decision/profile/fingerprint 不原地改寫。

## B5 既有依賴：保留原票，不另建同義 child

| 既有 issue | 在 DAG 的位置與可驗證邊界 |
|---|---|
| [#818](https://github.com/hamanpaul/paulsha-cortex/issues/818) OPEN | 多 Manager live gate 前需 canonical jobs.json single-writer 或跨 process serialization/CAS；兩 process stale snapshot mutation 都保留或明確 conflict。D6 的 quota authority 不能代替它，它也不能代替 D6。 |
| [#800](https://github.com/hamanpaul/paulsha-cortex/issues/800) OPEN | D10 真安裝／B5 部署前處理原票配置保留、root 隔離與 builder 不寫 operator config。`deploy/installer.py:291,302,345` 仍有 migration 重建配置入口；只把 artifact revision 報對不等於不再覆寫配置。 |
| [#582](https://github.com/hamanpaul/paulsha-cortex/issues/582) OPEN | 任何 active-job upgrade/restart 驗收前，原票須證明 drain／明確 abort 或可恢復的 manager broker 生命週期。#823 session isolation 不等於 systemd cgroup 隔離或 manager RPC 連線存活。 |
| [#476](https://github.com/hamanpaul/paulsha-cortex/issues/476) OPEN | fresh-instance project config scaffold 或 fail-fast、manager/monitor 均可診斷；當前 installer 已有 scaffold 路徑，需重驗原 repro，不能重造後再撞 #800。 |
| [#548](https://github.com/hamanpaul/paulsha-cortex/issues/548) OPEN | `doctor.py:751` 已依有序 EnvironmentFiles 合成 effective env，`run_doctor:1129` 已消費；不能再宣稱 source 一律只看 operator shell。原票重驗 service 環境一致性；這不證明正在跑的 process 載入哪個 artifact。 |
| [#695](https://github.com/hamanpaul/paulsha-cortex/issues/695) CLOSED | generated installed assets attestation 可沿用。`trust_root/install/backend.py:1236,1290,1814` 已提供 process＋durable job 的 in-flight facts；新部署 gate 應消費它，不另寫寬鬆「沒有 active job」判定。 |

以上是既有依賴清單，不是額外 6 個新進件提案。若任何必要依賴尚未獲有效驗收，
B5/whole-plan ledger 應保持 pending／blocked-by-具體項目，不能以文件收斂視為完成。

## PatchMUD #37 的精確外部位置

[PatchMUD #37](https://github.com/hamanpaul/paulsha-patchmud/issues/37) 已開且 OPEN，
Cortex 不承包其產品碼，也不把本次授權延伸成付費 benchmark campaign。

1. **D1/D2 契約依賴**：producer 的 requested/resolved/observed execution-profile、native
   effort、adapter/loadout/toolchain version、canonical fingerprint 與版本化 CLI/file schema。
   Cortex 以自己的 qualification 契約消費；維持零 PatchMUD runtime import。
2. **D2 真整合 gate**：先用固定 fake/golden report 開發 importer；真正
   report→candidate→human receipt→approved roster→實際派工，須等 #37 可追的 schema／
   immutable fixture 與 producer revision。未回報 observed effort 可以是 unknown，不能
   當 requested effort 的實測；不自動放寬最低品質或任意批准。
3. **D5 可選先驗**：#37 的 observed/estimated/unknown usage、unit、來源、累計/增量及
   failure consumption。可先用 Cortex 實務資料校準，不必讓全部 B3 等 benchmark；
   但聲稱「PatchMUD 冷啟動先驗已接通」必須取得真正契約 fixture。
4. **不是 #37 自動解決的項目**：完整角色 deck 是 PatchMUD #13，難度分布是 #21，
   agent-native end-to-end 是 #12；各自保持 coverage unknown／有界依賴。價格快照只影響
   cost report，不進入 capability fingerprint；同模型不同 effort 不合併成同一實測資格。

## 最小測試矩陣與執行順序

下列全為待實作／待執行案例，沒有 PASS 宣稱。沿用 pytest、tmp_path、fake runner，
clock／Event／barrier 控制時序；不以大量 sleep 或真實 provider 呼叫製造回歸。

| 風險／owner | 觸發與 harness | 必須斷言的 oracle／層 |
|---|---|---|
| 配置擴充 D1 | 虛構 profile、新原生 effort、fake adapter | 舊 adapter 新 descriptor 不改 central resolver；unsupported 零 spawn；contract/unit。 |
| 誤授權 D2 | 修改 effort/adapter、缺 role coverage、無 review receipt | 舊 qualification 不匹配、unknown 不變合格；contract。 |
| 過時探活 D3 | fake clock 越 TTL、adapter 不可用或回 unknown | 精確 status/reason，不假裝 available；unit/component。 |
| 額度誤判 D4 | 同池不同型號、短窗足夠/週窗不足、unknown/損毀、混合 unit | 每個約束均滿足才可確認；未知保留來源與 gap；contract。 |
| 用量重計 D5 | fail+retry、cumulative+delta、reasoning subset、replay/restart | 已消耗不消失、不重計；quality/infra 分開、forecast version 可追；unit/durability。 |
| 競爭超額 D6 | 兩 process barrier 共一份 state，多 pool 中途失敗 | 僅一份 grant，all-or-none；兩 instance 同 pool 共 authority；integration。 |
| 孤兒預留 D6 | spawn 前後 crash、lease 過期但 fake job 活著 | 不重複 grant；存活／uncertain 不釋放，終局結算冪等；recovery。 |
| 錯誤 fallback D7 | 耗盡原 pool；另一同池及另一獨立池候選 | 同池拒絕、獨立池合格才選；pin/independence 優先；component。 |
| 無安全候選 D7 | 唯一 reviewer 與 builder 同 domain | 零 spawn，合法等待 reason，#830 consumer 不讀假 job_id；integration。 |
| 預估落空 D7 | 預留後 fake provider 回 429、外部消耗提高 | 記錄消耗/退避/reset，安全 attempt 邊界再選，不污染品質；recovery。 |
| 狀態冒充 D8/D9 | 多卡/retry/跨 repo、stale snapshot、needs_human | actual/planned/last/decision 不混用、精確 key、read 不改 registry；contract/integration。 |
| 部署冒充 D10 | 隔離安裝後 disk 更新但舊服務 process 存活 | loaded revision≠disk revision 可見；checkout 外 CLI、source override、rollback receipt；installed E2E。 |

執行順序：先 deterministic unit/contract，再 crash/recovery、barrier concurrency，
再 bounded stress（跨多次觀測／重啟，檢查 ledger 不重計且持久化大小有治理），最後隔離
installed E2E 與經核可的 live canary。真正 provider 的可用介面與 quota 單位應在 adapter
實作時另查官方當時契約；這次未驗證各家是否有 remaining API。

可重用的已存在測試入口（命令列僅建議，**本次未執行**）：

```bash
python3 -m pytest tests/test_model_resolution_chain_534.py tests/test_per_work_model_chain.py tests/test_model_identities.py tests/test_model_identities_envelope_v3.py tests/test_model_profile_cli.py
python3 -m pytest tests/test_coordinator_usage_extractors.py tests/test_engineering_outcome.py tests/test_coordinator_launcher.py
python3 -m pytest tests/test_stage9_project_monitor_service.py
python3 -m pytest
```

新測試檔名由各 Cortex child 實作時定案；不得把上列既有測試名當成已涵蓋所有新矩陣。
root 整合時還需做 PR-context policy、文件／schema／fixture 對齊，並個別記錄
implemented、tests passed、merged、installed、live accepted；完整文件不是其中任何一項的替代品。
