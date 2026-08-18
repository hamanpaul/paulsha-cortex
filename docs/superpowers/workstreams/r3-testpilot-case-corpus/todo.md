---
status: proposed
work_item: r3-testpilot-case-corpus
---

# r3-testpilot-case-corpus Todo

R3（原 v3 的 1-B）testpilot plugin case 的**素材盤點** workstream，對應 issue `#667`。

**status 刻意不是 `accepted`**：本 workstream 的產出是一份**候選清單**，不是已核可的實作
計畫。清單本身不進派工鏈、不當 gate、不擋 merge。要把其中任何一筆長成 case，需要另開
實作票並在該票上取得 accepted 的計畫——本 workstream 不代行那個核可。

本票的唯一產出是文件。**明確不做**（`#667` scope fence，越界即為失敗）：不寫任何 case
yaml、不建 mock provider／tick harness、不動 `paulsha_cortex/` 下任何程式、不預蓋框架。

## Tasks

- [x] 四路盲測 sweep 執行完成（症狀家族／子系統／生命週期階段／artifact 型別，四路互不
      看對方的發現）。原始筆數：症狀 37、子系統 49、生命週期 37、artifact 32（`#667`
      派工單記為 31，實際條目重算為 32）。
- [x] 跨軸去重並計算 `hit_by`：**101 筆**去重後候選。分佈為四路命中 1、三路 9、二路 31、
      單路 60。合成後的清單本體見 `case-candidates.md`。
- [x] `evidence-insufficient` 四路合併去重：原始 41 筆（症狀 9／子系統 11／生命週期 12／
      artifact 9）→ 去重後 **32 筆**，全部保留「缺什麼證據才能判定」。
- [x] 三個橫向發現落檔（oracle 品質分級／既有陷阱與 tier 拆分硬規則／define 八環攻關鏈
      整組存在）。見 `case-candidates.md` 的「三個橫向發現」一節。
- [x] 覆蓋缺口誠實記錄（08-12 波未深讀 6 張／ship-delivery 語意零覆蓋／porcelain 分不出
      穩定與繞過／deck-combo 次級缺口）。見 `case-candidates.md` 的「覆蓋缺口」一節。
- [ ] **下一輪優先**：補讀 08-12 波未深讀的 6 張（`#473`／`#475`／`#476`／`#478`／`#506`／
      `#508`）。成本約 1 次 `gh` 呼叫。其中 `#478` 與 `#506` 已由症狀路獨立取證進入候選
      （`recovery-reports-ok-while-git-registry-stale`／`secondary-rate-limit-response`），
      補讀可把它們從單路命中升級，並可能新增 deck 與 work-registry schema 兩格。
- [ ] **下一輪優先**：補 ship／delivery 的語意面。**不要再掃 issue**——語料已證實 08-12 波
      33 張深讀裡零張是 ship 事故。建議改讀 `github_delivery.py`(47KB) 的 PR metadata
      preflight／merge authorization／delivery journal／push readback／closed-unmerged PR
      五個表面，並以 `delivery-journal.json` 的 19 個 run 當 fixture 來源。
- [ ] **下一輪優先**：補 porcelain 的 operator 繞過手法。同樣**不要再掃 issue**——建議讀
      `docs/` 的 onboarding／quickstart／troubleshooting 與 driving-cortex skill
      （`#177`／`#192`），operator 的繞法沉澱在那裡而不在 issue tracker。
- [ ] 決定 T1 三筆（`review-identity-loader-asymmetry` `#490`／
      `porcelain-cli-verb-must-match-permgen-execstart` `#618`+`#619`／
      `unbounded-substring-marker-misclassifies-failure` `#487`+`#500`+`#554`）是否為 R3 的
      首批實作票。三筆皆為純函式＋凍結 fixture、oracle 型別為集合相等或差分、零 harness
      前置。**待 R2 Compact 收斂後才開實作票**（R3 本體依賴 R2，本盤點不依賴）。
- [ ] 確認 case report 與 `EvidenceAttestation` 的契約對齊（subject 綁 candidate、**不得
      自我背書**）。本盤點未涉及此契約的實作形狀，僅記錄依賴。
- [ ] 把「多 UID 不可用時標 `unsupported`，不得標 `pass`」寫進未來 case harness 的契約層
      （目前只是本文件裡的硬規則，尚無執行機制）。

## 一句話狀態

四路盲測 sweep 已完成並合成為 101 筆去重候選 ＋ 32 筆 evidence-insufficient；清單本體與
三個橫向發現、四個覆蓋缺口皆已落檔。**尚未有任何 case 被實作**，也不應在 R2 Compact
收斂前開始。

## 依賴

- **本 workstream 零前置**（`#667` 刻意如此設計）。
- **R3 本體**（把候選長成 case）依賴 **R2 Compact**。理由是 0814 實測「12 卡成本下小案派工
  不成立，每案 define 必死」——見候選清單的 define 八環攻關鏈。

## 相關

- issue：`#667`
- 計畫：`~/prj_pri/cortex-redesign-rollout-plan.md` → `Phase R3【原 1-B｜testpilot plugin
  cases，用 Compact 跑】`
- 清單本體：`docs/superpowers/workstreams/r3-testpilot-case-corpus/case-candidates.md`
