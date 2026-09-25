---
status: accepted
work_item: merged-run-completion-finalizer
---

# Manager merged-run completion finalizer 設計（#977）

## Decisions

### D1 單一 Manager owner 與入口順序

finalizer 只由 #967 owner-locked daemon path 與 per-run critical section 進入；於 provider-backoff 之後辨認 #975 嚴格 admission，於 `needs_human`／planning reconcile／一般 verify dispatch 之前路由。`operator_resume` 只處理既有人工 facet，不讓 periodic tick 越權。新的 review mismatch 交原 review→ship closure；舊 verify-reset 才進本 finalizer。proof 失敗仍已知 merged 時返回可診斷 stop，不 dispatch verify；無 merged binding 才使用 #975 fallback。這個位置避免 reset 後 pending verify 被誤派，也保留 operator gate。

### D2 Fresh evidence bundle 每次重建

在 Manager 私有 helper 內收 current WorkAuthority + source revisions、#975 完整 consumer proof、#995 第一階段 raw remote facts/default_head；以 proof 的原 run/step/gate/authorization/CompletionRecord inputs 和這次 default_head 建記憶體 draft，再交 #995 第二階段純驗證與 evaluator。bundle 僅限本次 boundary 使用；path/hash/identity/完整 normalized WorkAuthority 與上一 boundary 預期 payload逐欄比較。任何 drift 回 stop。不能由 remote facts 補造 job、gate 或原 Manager authorization。先取得 proof，後取得本次 default_head，再組 draft，避免猜測 target_ref_sha。

### D3 三個明確 durable boundary

順序固定：(1) #996 no-follow read → 以既有固定 completed_at 或新 timestamp 組 expected CompletionRecord → conditional create/reuse → exact read-back；(2) #997 canonical outbox snapshot → 以既有固定 emitted_at 或新 timestamp 組 expected shipped outcome → conditional append/reuse；(3) #976 restricted registry transition。每個 boundary 前重新執行 D2，並核對前一 store 已 durable 的精確 payload。若 #996/#997/#976 回 typed conflict、read race 或 CAS drift，當次停止；下一次 tick 重新取得全部外部證據和 store snapshot。這是單向重入狀態機，不宣稱跨 store/GitHub transaction，也不主動回滾已存在且合法的 record/outcome。

### D4 正確 store path 與安全重入

outbox path 從實際注入 runtime/registry config 投影，不能從 default helper 重算。#996 既存 record、#997 既存 outcome 分別先安全讀固定 timestamp，再建立本次 expected payload；精確相同才零寫入重用。#997 的已存在相同 ID 仍要先驗整檔 schema、duplicate ID 與 path identity；新 ID append 需 fresh revision。#967 鎖住 jobs.json 的 owner，不取代 #996 conditional create 或 #997 outbox CAS。重入判準是完整 payload equality，不是只比 record hash 或 outcome ID。

### D5 Registry 僅比對本地 binding

#976 API 接受由已驗 CompletionRecord 投影的 terminal_binding、#975 authorization hash 與 expected candidate。Manager 在呼叫前重新驗外部 bundle/record/outcome；registry 只比較當下 run/job/claim/head/auth/generation/terminal fields。`completion_source_revisions` 以 validated record 的 frozen map 為準；latest WorkAuthority 用來判斷 proof/identity 是否仍有效，不替換該 map。相同 terminal row 的 exact re-entry 不寫，其他 done row 或任一欄不同即 fail closed。

### D6 Proof-bound terminal shape

原 manifest 的 step phase/card/persona 骨架不變；job-backed steps 用唯一已成功 job 及其 evidence，Manager-only steps 用原生 durable provenance。只把有證據的 verify/review/ship steps 標 passed，並設 verified_head=candidate。ship gate refs 必須有可核對 foreign-review、適用 brainstorm，以及恰一種 current-HEAD delivery-review；builder/reviewer 互斥仍由既有 WorkflowRun validator 驗。不得利用 pending reset run、`completion_record_valid=True` 或 journal merged 文本填補缺失。

### D7 驗證與保留的父票責任

測試將三處 crash seam 分開注入，固定三次以上重入並檢查 byte/hash/row/terminal equality；負例逐一覆蓋 authority/provider/default-head/Todo/PR/proof/evidence、unsafe record/outbox、同 ID collision、registry active-job/state drift。新 mismatch route 與舊 verify-reset route 分開測；所有網路都用 provider stubs。#962 保留 R1–R4/R6/R8(a–d) aggregate，#887 保留全部 AC。此設計只為 #977 Manager 切片定義責任，不提前實作或驗收前置 API。

## Five-dimension sizing decision

此切片預期僅 `manager.py` 一個 production module，`domain_breadth=0`。跨 CompletionRecord、OutcomeStore、Registry 三個 durable boundary 且需 crash/re-entry exactness，`state_consistency=2`。repo `fix-standard` 的 2 gate_spine 加 R-09/R-16/R-19 得 `acceptance_surfaces=2`；完整 accepted triad 無阻塞標記得 `spec_stability=0`；9 cards、9 persona bindings 得 `orchestration=2`。總分 **6 / Yellow**，與 issue #977 的獨立五維估算一致。這不是 #962/#887 aggregate 降分；前置 API 不符合或 production 擴到第二模組，需停止並重新 sizing/split。

## Rejected approaches

- 舊 side-effecting ship validator／`verify_remote_closure`：會在只讀 proof 前改動 workspace 或 CompletionRecord。
- 只用 journal merged、PR closed 或 reset 後 pending steps：不能證明本 run 原授權與 passed gates。
- 僅靠 #967 owner lock／outcome ID dedup：其他 canonical outbox path 可有獨立 writer，payload 也可能衝突。
- Registry 直接查 GitHub、Todo、record 或 outbox：超過 #976 的 Registry-local CAS 責任，無跨 store transaction 保證。
- CAS conflict 同 tick 自動重試：會重用舊 authority/remote/evidence snapshot；下次 eligible tick 必須從頭重驗。
