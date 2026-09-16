# Executor backoff store child A authoring report

## Scope and status

- Work item：`executor-backoff-store-core`，#825 人工 child A；原 author 只交 spec/design/todo＋本 report 四檔；root 後續已建 #850 並於本批登錄。這不是 #833 自動 decomposition/re-entry 證據，也未 freeze／dispatch。
- 隔離 branch：`feature/refine-backoff-store-child-20260907`，base `fb31083e7325be86c6c9d217da56b9c4d799f5a5`（merged #846）。先盤點後由 origin/main 建 worktree，正常 `pull --ff-only origin main` 為 already up to date；主 worktree 原有 dirty 未動。
- 唯一未來 production scope：新 `paulsha_cortex/coordinator/executor_backoff.py`。目前無 store implementation、product tests、changelog、runtime/registry 變更；todo 全部未勾。
- Authoring context：root 已接受六拆作候選，但 C provenance/strict terminal seam 尚未裁決。本文使用 doc-coauthoring 的 context/結構檢查與 test-playbook 的 deterministic matrix/negative controls；fresh reader/獨立 review 由 root 執行，未另派模型。

## Parent AC map

| Parent | A 的交付 | 母工作尚需 owner |
|---|---|---|
| R1 durable cooldown | fresh store observation、持久 events/aggregate | D/F 每個 admission 接線，非只有 fixture query |
| R2 atomic/idempotent/replay | R2/R4/R5/R7/R8、T02–T09；ack 對 supplied intent | C immutable terminal/policy/strict inventory/retention；D 跨 consumer production 對帳 |
| R3 unknown | R1/R8；raw state 與 reconciliation 分開 | D/E/F unknown 停新增、不影響 active job／不耗 retry |
| R4 deadlines/provenance | R5/R6 frozen policy fold/max；不提早 clear | B 文字 parser/時區；C 固化真來源/解析上下文；D 送正確 envelope |
| R5 eligible terminal | shape/outcome/authority defensive rejection，不背書真 job | C durable authority；D slice/workflow failed 共用 helper |
| R6 workflow/pin | 無 admission/reroute 實作 | D candidate order/permission/pin/independence，known wait/unknown，不耗 provider retry |
| R7 slice/consumers | 無 launch/request side effect | B 公開 model；D manager retry/tick；E request/periodic；F pending 前 slice gate |
| R8 tests/observability | A pure/component/process fault/mutation、help/API 相容檢查 | D/E/F 雙 lane/request JSON；root merge/installed/live 分帳 |
| R9 quota boundary | 所有文件保留非目標 | R05/R08/R09、D4–D7 的 shared pool/forecast/reservation/dynamic profiles 仍未完成 |

## Candidate DAG and sizing boundaries

候選 DAG：A ∥ B → C → D；E 可並行準備；A+B+C+D+E → F → #825 母驗收。這是依賴方向，不是已建立的 child authority 或派工。

| Child | Production 修改 | domain/state/invariants | #831 完整case投影 |
|---|---|---|---|
| A store/event fold（本件） | executor_backoff.py only | 0/2/8 | 6 Yellow |
| B reset／identity 純輸入 | provider_outcome.py、launcher.py；若獨立 parser 合計最多3 | 1/0/5 | 5 Yellow；只在確為無 durable state 的 pure 邊界成立 |
| C terminal anchor／strict inventory | registry.py only | 0/2/4 | 6 Yellow，provenance/retention 尚需裁決 |
| D terminal reconcile/workflow/manager consumers | manager.py only | 0/2/6 | 6 Yellow，依 C 真契約 |
| E request/periodic consumers | manager_daemon.py only | 0/2/3 | 6 Yellow |
| F slice admission | autonomy.py only | 0/2/4 | 6 Yellow；D/E 先相容才啟用 |

C 若必須修改 `dispatcher.py` 以固化首次 terminal observation/reset parsing context/policy，需 root 修訂母 boundary、另列 producer child；不能 registry＋dispatcher 兩模組還寫 domain0。2–3 production 模組且 state2 →7/Red；≥4模組且 state2 →8/Red。母完整 plan 現行10/Red，#831後8/Red，不因候選六拆改成可派。

## Evidence and unresolved producer responsibility

已全文讀 parent spec/design/todo 與 canonical policy。下列 `79ba6447` source 與 authoring base 的對應 production 檔無差異；source trace 是當下佐證，不宣稱窮盡所有未來／外部 caller。

- [provider_backoff.py:42](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/provider_backoff.py#L42)：GitHub fail-soft reader 不能複製成 executor unknown 語意。
- [registry.py:1333](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L1333)：terminal self-transition 可重寫 exited_at/provider_outcome；A 不把它升成 immutable authority。[既有 timestamp 測試](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/tests/test_coordinator_registry_headless.py#L925) 只驗存在/時間順序。
- [registry.py:376](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/registry.py#L376)：stat error 可留下 reader cache；C 需 strict fresh snapshot，不能空集合/舊 cache 代表 pending=0。
- [dispatcher.py:438](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/dispatcher.py#L438)：現有 classification→dict→registry 生產接線；需要新增 provenance producer 時顯式另裁，不由 A 修改。
- [manager.py:9636](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L9636)：forced_identity bypass 與無 capability 路徑必須由 D 最後副作用前 gate 涵蓋。
- [manager_daemon.py:908](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager_daemon.py#L908)、[manager.py:1861](https://github.com/hamanpaul/paulsha-cortex/blob/79ba644780bf1c697c722ac24a297e7d02416100/paulsha_cortex/coordinator/manager.py#L1861)：empty dispatch/retry 需 D/E 先相容，F 不可先啟用造成 IndexError 或假 retry failure/success。

Residual：A 可驗 envelope 內在一致但不能證明 caller authority/完整性；C/D 缺口是外部交付依賴，不用 always-complete fixture 冒充解決。v1 只保留已在有限容量內接收的全 ledger，不實作 GC；飽和停止接收新事件、保留 bytes/acks、未合併 intent 回 caller，這是有界可用性限制而非假恢復。資源 profile 與 POSIX filesystem 支援範圍明示，IO/鎖/durability 不確定即 unknown；合作式 compute deadline 不假稱能搶占 kernel IO。所有模型/effort 名稱保持資料，不加入固定路由表。

## Review R1 disposition

Root 轉交一條 MAJOR，獨立核對成立：舊 design D3 全量 fresh read/validation、D4 完整 ledger sort/fold 與永久 retention 組合，沒有 bytes/events/decode/compute cap；只列 disk-full 不能排除磁碟未滿前的 OOM／長期持鎖。因此不駁回、不以已列 residual 宣稱該問題可接受。

四檔此次修訂：新增固定 `bounded-ledger/v1` resource profile（store 2 MiB、input 1 MiB、retained events 1024／identities128、input1024、event8KiB、JSON depth12/string4KiB/key512/numeric64、累計nodes131072、comparisons65536/fold visits2048、byte work16MiB、合作式compute1秒）。先 bounded read/lexical count 再配置/decode；舊 store/候選最多各一轮 fold；完整候選超限在 replace 前原子拒收，不部分 ack。已合法達 cap 的 duplicate 先比對去重，仍 unchanged；新 distinct event 回 capacity unknown，原 bytes/acks 不動。已超讀取 cap 的檔不為辨認 duplicate 而越界解析；expiry 不釋放額度，仍不 GC。

新增產品測試契約 T13–T16：超大/成長 fd/深度與節點超限、multi-process 最後額度競爭、duplicate-replay-at-cap 與 expired replay、同 key 衝突／新 event、計算 counter/deadline 中止與釋鎖；配套 mutant 必須抓到晚檢查、拒 duplicate、刪最舊 ack、忽略預算。這些是尚未實作的 RED/GREEN Tasks，不是此次已跑產品測試。

多程序 freshness/flock、post-replace durability-unknown、immutable policy/time、raw store≠reconciled admission 皆保留。domain0/state2/八組不變量不變：本修訂具體收緊 R1/R2/R4/R7 的既有責任，沒有新增 production 模組。此處僅記 author 修訂；**不宣稱獨立 review PASS**，交 root 重審。

## Review R2 and fresh-reader clarification

Root 已回報 R2 獨立審查 PASS、R1 原 MAJOR 已處置，且全文讀回四檔；fresh reader 五個問題回答正確。這是 root 回傳的審查結果，不是 author 自評或產品已完成。

唯一真歧義為 crash 是否僅以可捕捉 sync/replace exception 模擬。本次在 spec R2、design D3.1/T09、既有 process-atomicity Task 明訂真正終止測試自建 writer：barrier 定位 replace 前 K0、replace 成功但 directory barrier 前 K1、barrier 後 K2，fresh process 重讀 data 並完成 data/directory sync、重讀 durable fixture inventory 對帳，驗死亡釋放 flock 與重播不加 hits/deadline。只准 kill 本 fixture 建立且持有 handle/PID 的子程序，bounded join/finally cleanup；不碰任何 live process。SIGKILL 僅證明 process-death/restart protocol，不等真斷電或任意 filesystem 持久性驗證。

此為測試契約澄清，維持四檔、domain0/state2/8 invariants、15個產品 Tasks 未勾；T09 產品測試尚未實作或執行，未新增 issue/code/registry/runtime 變更。最終文件 hash 交 root，停止 author 寫入。

## Author verification

產品待辦與下面 authoring checks 分帳；此 report 不宣稱 store RED/GREEN、CI、獨立 review、merge 或 installed/live 完成。

- 已用 `PYTHONDONTWRITEBYTECODE=1 python3` 載入 checkout 的 `assess_planning_completeness`、`plan_review_gate`、`compute_sizing_score`、`sizing_band`，以及 packaged `load_cards/load_combo` 作一次性純運算；未讀 live registry、不啟模型、不產生產品 test evidence。
- 三件組 completeness=True、missing=()、blocking=0；plan-review 的 source/tests/documentation coverage 與 R-09/R-16/R-19/R-22 compatibility 通過。envelope 明確為 `bypass: envelope_unavailable`，不是已評測 builder 資格；另以明示 synthetic invariant limit=7 負控制，8 invariant 正確 `envelope-exceeded`，不將 synthetic fixture 冒充真 envelope。
- packaged fix-standard 實讀 cards=9/bindings=9/gates=2；sizing rules 為 R-09/R-16/R-19 全集。完整 A 真五維 `[0,2,2,2,2]`＝8/Red；`dataclasses.replace(score, spec_stability=0)` 僅投影 #831 完整case 為6/Yellow，非 loaded gate，不改 production 算法。
- 11 組 authoring 負控制均拒絕：刪 spec、spec 改 draft、design 加獨立 blocking marker（completeness）；缺 domain、缺 state（sizing ValueError）；移除 Tasks 的 source/tests/documentation/CLI/changelog coverage（plan-review）；synthetic envelope 7（envelope-exceeded）。改 blocking design 的舊 score 會降到6/Yellow，但 completeness=False，明確不能當 admission 成功。
- CLI 由真 source 確認 status/stat/tick help 與 deprecated dispatch：top-level 沒有通用 inspect，已修正 child todo，不沿母文件泛稱發明新指令；本 authoring 沒有發 control request。產品 help/API smoke 仍未勾。
- R1 修訂後已重跑純 completeness、plan-review coverage、真 sizing 與同11組 authoring 負控制，結果維持上述8/Red／#831投影6/Yellow、envelope_unavailable。四檔逐一 `git diff --no-index --check /dev/null <file>` 無 whitespace 問題；相對 Markdown links 均存在、無個人絕對路徑、exact scope 仍四檔、8組 Requirements／15個產品 Tasks 全未勾。T13–T16／容量字面值只做文件存在檢查，不等產品資源限制已驗證；最終 hash 交 root。sizing/deck 純函式來源以 `git diff --exit-code 79ba6447 HEAD -- <paths>` 驗為同版。
- 完整 authority 仍須 root reader review；未處置缺陷/缺口為 FAIL，已明示且影響有界、文件列管 residual 不單獨 FAIL（不同意須具體反駁影響分析）。
- 本 authoring 僅四檔，無 changelog/policy 豁免寫入；root 整合 PR 需自行帶正確 changelog 或既有白名單豁免理由，未跑裸 `policy_check` 宣稱通過。
