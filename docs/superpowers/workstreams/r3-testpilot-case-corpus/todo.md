---
status: accepted
work_item: r3-testpilot-case-corpus
issue: 904
---

# r3-testpilot-case-corpus Todo

R3（原 v3 的 1-B）testpilot plugin case 的**素材盤點** workstream，對應 issue `#667`。

本 workstream 的產出是一份**候選清單**，不是已核可的實作計畫。清單本身不進派工鏈、不當
gate、不擋 merge。要把其中任何一筆長成 case，需要另開實作票並在該票上取得 accepted 的計畫——
本 workstream 不代行那個核可。第二輪（`#904`，2026-09-15 進件）承接下列 6 項未勾任務，規劃
三件套見 `docs/superpowers/specs/r3-testpilot-case-corpus-{spec,design}.md` 與
`docs/superpowers/plans/r3-testpilot-case-corpus.md`；`status: accepted` 指第二輪盤點計畫已核可，
不代表任何 case 進入實作。

本票的唯一產出是文件。**明確不做**（`#667` scope fence，越界即為失敗）：不寫任何 case
yaml、不建 mock provider／tick harness、不動 `paulsha_cortex/` 下任何程式、不預蓋框架。

## Tasks

- [x] 四路盲測 sweep 執行完成（症狀家族／子系統／生命週期階段／artifact 型別，四路互不
      看對方的發現）。原始筆數：症狀 37、子系統 49、生命週期 37、artifact 32（`#667`
      派工單記為 31，實際條目重算為 32）。
- [x] 跨軸去重並計算 `hit_by`：**109 筆**去重後候選。分佈為四路命中 1、三路 9、二路 32、
      單路 67。合成後的清單本體見 `case-candidates.md`。
- [x] `evidence-insufficient` 四路合併去重：原始 41 筆（症狀 9／子系統 11／生命週期 12／
      artifact 9）→ 去重後 **31 筆**，全部保留「缺什麼證據才能判定」。
- [x] 三個橫向發現落檔（oracle 品質分級／既有陷阱與 tier 拆分硬規則／define 八環攻關鏈
      整組存在）。見 `case-candidates.md` 的「三個橫向發現」一節。
- [x] 覆蓋缺口誠實記錄（08-12 波未深讀 6 張／ship-delivery 語意零覆蓋／porcelain 分不出
      穩定與繞過／deck-combo 次級缺口）。見 `case-candidates.md` 的「覆蓋缺口」一節。
- [x] **第二輪：補讀 08-12 波未深讀的 6 張**（`#473`／`#475`／`#476`／`#478`／`#506`／
      `#508`）。
      完成摘要：新增候選 103／104／105／106，候選 77 升級為雙路；`#506` 補到 manager／monitor
      呼叫形狀證據後仍維持 EI 9。
- [x] **第二輪：補 ship／delivery 的語意面**（`github_delivery.py` 五個表面）。
      完成摘要：PR metadata preflight／merge authorization／push readback 新增候選
      107／108／109，delivery journal 與 closed-unmerged PR 分別落在候選 92／78。
- [x] **第二輪：補 porcelain 的 operator 繞過手法**（onboarding／quickstart／troubleshooting
      ＋ driving-cortex `#177`／`#192`）。
      完成摘要：supported path 與 operator bypass 已分群；`systemctl --user`／`gh api
      graphql`／`pipx install --force` 等旁路都已明記。
- [x] **決定 T1 三筆是否列為 R3 首批實作票**（`#490`／`#618`+`#619`／`#487`+`#500`+`#554`）。
      完成摘要：三筆全數決定為「首批（待 R2）」；理由與依賴已寫成四欄表，不在本盤點內開票。
- [x] **確認 case report ↔ `EvidenceAttestation` 契約對齊**（subject 綁 candidate、不得自我背書）。
      完成摘要：以 `verification.py` 的 `schema_version`／`slice_id`／`candidate`／`status`／
      `summary`／`details` 六欄記下 subject 綁定與禁止自我背書。
- [x] **把 `unsupported` 寫進未來 case harness 契約層**。
      完成摘要：補上 unsupported vs pass 可區分、production generator provenance 與效果斷言等
      四條硬規則，明記目前尚無執行機制。

## 一句話狀態

第二輪完成：候選清單已收斂為 **109 筆**去重候選 ＋ **31 筆** evidence-insufficient；08-12 波
補讀、ship／delivery 五表面、porcelain 穩定 vs 繞過、T1 首批決定與 attestation／harness
契約備註皆已落檔。**尚未有任何 case 被實作**，也不應在 R2 Compact 收斂前開始。

## 依賴

- **本 workstream 零前置**（`#667` 刻意如此設計）。
- **R3 本體**（把候選長成 case）依賴 **R2 Compact**。理由是 0814 實測「12 卡成本下小案派工
  不成立，每案 define 必死」——見候選清單的 define 八環攻關鏈。

## 相關

- issue：`#667`（第一輪）、`#904`（第二輪）
- 計畫：`~/prj_pri/cortex-redesign-rollout-plan.md` → `Phase R3【原 1-B｜testpilot plugin
  cases，用 Compact 跑】`
- 清單本體：`docs/superpowers/workstreams/r3-testpilot-case-corpus/case-candidates.md`
