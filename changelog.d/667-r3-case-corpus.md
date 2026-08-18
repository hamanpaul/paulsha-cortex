# 667-r3-case-corpus

### Added
- **R3 testpilot case 素材盤點（`#667`）——四路盲測 sweep 合成為 102 筆去重候選清單**
  ——`docs/superpowers/workstreams/r3-testpilot-case-corpus/` 新增 `todo.md` 與
  `case-candidates.md` 兩份文件。**唯一產出是文件**：不寫任何 case yaml、不建 mock
  provider／tick harness、不動 `paulsha_cortex/` 下任何程式（`#667` scope fence）。
  - **四路互相盲測**（症狀家族／子系統／生命週期階段／artifact 型別，執行期間彼此不看對方的
    發現）共產出 **155 個原始條目**（37／49／37／32），跨軸以「真實事件」為單位去重後
    **102 筆**。`hit_by` 分佈：四路命中 **1**、三路 **9**、二路 **31**、單路 **61**。
    **61 筆單路命中全數保留並標示**——artifact 路的 24 筆純實測發現（0-byte evidence、
    雜湊方案不一致、log 截斷、run journal 與正典 manifest 對不上…）**沒有對應 issue，
    掃 issue 的三路在結構上不可能命中**。
  - **排序依「可以最早開始長」而非 `hit_by` 數量**，分 T1–T6 六個 tier（純函式／fs 佈置／
    tick 與真實 git／crash 注入／多 UID 殘量／期望值待定）。第 1 名是單路命中的 `#490`
    （review 與 tick 的 identity 集合必須相等），唯一的四路命中 `#501` 排第 59——因為前者
    零 harness 前置而後者需要 tick 推進與可控的 review-launch 失敗。
  - **oracle 品質分級**：每筆標注 oracle 型別。最高等級是**差分／集合相等**型
    （`#490` identity 集合、`#509` doctor 與 tick 同源、`#383` tick 與 fanout dispatch 集合、
    `#486` prompt severity enum、`#420` 兩條 claim 入口到達同一 phase）——共同性質是
    **無法靠放寬任一邊來滿足**，結構上擋得住 fail-open；閾值型與存在性型則容易被放寬成空過。
  - **既有陷阱寫進 `harness_needs` 並強制拆 tier**：多條 trust-root case 的原 issue 逐字
    記錄了「上次是怎麼被繞過去的」（`#657`「測試環境是單 UID，spool 的 ACL 不影響任何事」、
    `#645`「我在手工組時自己挑了一個與 instance 名相符的 worktree 路徑——等於把這個 bug
    繞過去了」、`#638`「per-job 的正常流程沒有任何一條測試涵蓋」、`#478`「existing recovery
    test uses a normal temporary directory rather than a real Git worktree」）。文件層級訂五條
    硬規則，其中兩條為本次新增的具名約束：**多 UID／root／`direct` 模式／缺 `acl` 時必須標
    `unsupported`，不得標 `pass`，也不得 skip 成綠**；以及**手抄 property 子集 ＝ 驗證無效，
    且兩個方向都會錯**——後者現有四個實例，`#638`／`#657`／`#673` 原 body 是假綠，
    而 `#673` 的 repro（漏抄 `SystemCallErrorNumber=EPERM`，比 production 更嚴格）是**假紅**。
  - **define 八環串聯攻關鏈**（`#391`→`#393`→`#397`→`#399`→`#401`→`#404`→`#406`→`#408`，
    每修一環露下一環、每環確定性、每環燒一個世代）明載為**應整組存在的 case 套件**，
    不得拆成八個獨立 case——tier 是動工成本，不是拆包單位。
  - **`evidence-insufficient` 32 筆**（四路原始 41 筆去重），每筆保留「缺什麼證據才能判定」。
    其中 3 筆與候選清單重疊（`#502`／`#524`／`#488`），那是**四路之間真實的判斷分歧**，
    兩邊都保留並明載。
  - **覆蓋缺口四格，刻意不淡化**：(1) 08-12 波 6 張未深讀（`#473`／`#475`／`#476`／`#478`／
    `#506`／`#508`，補齊成本約 1 次 `gh` 呼叫，下一輪最高投報）；(2) **ship／delivery 是
    覆蓋度與風險落差最大的一格**——`github_delivery.py`(47KB)＋`delivery.py`(21KB) 是全庫
    第三大功能面，卻**零條 delivery 語意候選**，成因是 08-12 波深讀的 33 張裡零張是 ship
    事故（全部卡在 define/build/verify/review 就死了）且 ship 卡由 Manager deterministic
    執行不經 launcher，無 LLM 參與故 dogfooding 難顯現，而 **ship 是離 merge 最近的那道閘**；
    (3) porcelain 七個 verb 家族零原生事故，現有語料**分不出「真的穩定」還是「operator 當場
    繞過而不開票」**，補這格應去讀 `docs/` 的 onboarding／troubleshooting 與 driving-cortex
    skill 而非再掃 issue；(4) deck-combo 自動選型面零事故（次級）。
  - 另記錄四路的**誠實負面結果**（查過、乾淨的比對），避免後續重工——包含
    `evidence/workflow-inputs/` 是全庫唯一完全自洽的 content-addressed store，可作正面對照組。
