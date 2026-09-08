# AGY probe containment 規劃報告

日期：2026-09-07。原 author 時期只交付 spec／design／todo／report 四份規劃，
未註冊、freeze、dispatch、實作、commit、push、merge或安裝；該「僅四檔、
未操作registration」描述保留為歷史，不再當成本 PR 的當前登錄狀態。
root 已建立唯一 owner [#851](https://github.com/hamanpaul/paulsha-cortex/issues/851)，
並將 registration 納入本 PR：`repository-intake`／`repository-intake-unfrozen`。
產品與 Cortex 正式 gate／freeze 仍未完成，本 author 的整合只修改八份文件。

目前為 `awaiting-freeze`。root 全文讀回四檔與 source，並回報獨立 reviewer
`agy_containment_review` 的 R1 內容審查 PASS；此 PASS 歸因該 reviewer，
不是本 author 自行批准，也不代替真 builder 封套、Cortex 正式 gate、完整
preflight 或 frozen authority。root 另回報 fresh-reader 五題 PASS，將再做
fresh integration review 與 full preflight，之後才由合法 start 路徑處理正式 gate。
#851 原 author 時期唯讀 readback：OPEN、updatedAt=`2026-09-07T13:32:04Z`，
當時產品 AC 均未完成；本輪未再操作 issue。

## Current integration gate

整合工作樹已從 #852 merge `984fce5b` 快轉，root 已在本 PR 登錄 #851 三件套。
當前用 runtime `79ba6447` 純文件函式重算仍 complete=true、6 Yellow；
正式封套／plan review／freeze／start 不能由文件 frontmatter 或機械 bypass 代替。
root 當次 live 核對 #824／#851 labels 均空且無相關 run；這是該時點的觀測，
不是永久不會派工的不變量。本 author 本輪未呼叫 start／模型或修改現場。

下游 #824 已由 root 從母 #823 mapping 移除，故意不登錄 timeout child。
其現有 marker 令 true completeness=false、plan blocking-decision、4 Yellow；
surface-only gate 仍可能 ready，incomplete start 也可能進 brainstorm。
因此 metadata／marker 不保證zero-spawn；root保持不登錄、不auto-label、不start
#824，待本child產品、預定base及loaded runtime保護完成才解除並正式登錄。

## 問題與切面

R1 獨立 reviewer 已由 root 的 fake 反證核實：timeout-only 的 R4 要求 probe
`json_envelope=False` 一樣呼叫 resolver；然而既有
`model_identities.py:1071-1078` 在 smoke try 外建構 argv，非法設定
`ValueError` 可以從 `planning_runtime.py:1664` 穿透，連 ready 非 AGY primary
都無法建立 runtime。這是實質前置缺陷，不是可直接接受的殘餘風險。

原 author 已將 timeout-only 四檔標記 `blocked-dependency`，depends_on 為本 child；
這些宣告不是現行workflow lane的機械依賴閘，當前控制見上段。
timeout-only 仍只改 launcher，全部 env／Go range 驗證和 probe timeout 值保留。
本 child 唯一 production 是 `model_identities.py`，只移動 argv assignment
到既有 smoke try，沿用 failed result與bounded diagnostic，沒有第二個prod模組。

現有呼叫鏈的 source 證據在規劃 base與現行 runtime一致：

- `model_identities.py:16` 的 imported builder alias 是真 fault injection seam。
- `model_identities.py:717` 的 `_failed_agy` 保持 failed identity／reason。
- `model_identities.py:956` 的 `probe_exception_diagnostic` 不回顯 plain
  ValueError message；本child不能另增raw env/error/prompt輸出。
- `model_identities.py:1024-1101` 已有 discovery與smoke兩段例外邊界；只補後者。
- `planning_runtime.py:1632-1683` 遍歷roster／cache miss時呼叫probe，並非只probe
  primary；tests要兩種roster順序、真constructor和新tmpcache，不能mock整個probe。

本報告原始證據是author時期已讀source與root提供的獨立fake反證；本輪沒有另跑產品fake
reproducer或真正模型。未實作的 regression 全部明列todo，沒有宣称已測PASS。

## 隔離與 authority

原 author 時期先 inventory確認新path／branch不存在與既有worktrees，再從origin/main
`25c9a4e4040476523d0bb5f2132236f87b8d65f8` 新開
`feature/agy-probe-construction-containment-planning`。僅新worktree執行
`git pull --ff-only origin main`，結果Already up to date；operator／runtime
不切branch、不pull、不改內容。

正式work_id=`agy-probe-construction-containment`，唯一owner已確定為#851。
Manager固定以最小同repo issue與work_id導出
`feature/851-agy-probe-construction-containment`；planning branch不是
正式builder branch。canonical fragment固定
`changelog.d/agy-probe-construction-containment.md`，不複製timeout或#823 fragment。
這些是未來candidate要求，本輪沒有新增fragment或產品變更。

## Sizing 與 dispatch 分離

live核對 [#208](https://github.com/hamanpaul/paulsha-cortex/issues/208)，
updatedAt=`2026-07-27T12:42:35Z`：domain0=單模組／單資料流，state0=純函式／
local state。本切面單一production module、同一probe的local exception-to-result
flow，沒有schema／backward compatibility的新資料契約、cache protocol、
concurrency、atomicity或migration；故domain=0／state=0，不是把原subprocess
probe聲稱為pure function。跨caller regression、docs與changelog完整保留。

機械計算採現行runtime而非#831候選算法：core gates／policy rules／cards／
persona bindings從真feature-oneshot讀取；完整文件仍按舊spec_stability公式。
驗證結果在本報告後續段記錄，不以規劃者自行指定總分冒充runtime結果。

## 原 author 純文件機械檢查（歷史）

從checkout外cwd、Python `-B`、明確runtime import path執行；讀到的runtime
HEAD為`79ba644780bf1c697c722ac24a297e7d02416100`，没有寫bytecode或cache。

- `assess_planning_completeness`：complete=true；spec/design/plan三份accepted，
  missing kinds、reasons、blocking markers均空。
- 真`feature-oneshot`：必要core gates=4、cards=11、persona bindings=11；
  `work_bridge.current_sizing_snapshot`回`(6, "yellow")`。
- `compute_sizing_score`詳細值：domain=0、state=0、acceptance=2、stability=2、
  orchestration=2；沿用`claim.sizing_band`的Green≤3／Yellow≤6，不改算法或門檻。
- 真Manager `_evaluate_yellow_plan_review`與額外加入R-22的`plan_review_gate`
  均ready=true，completeness／contract compatibility通過；envelope明示
  `bypass: envelope_unavailable`。這不是builder封套已核准，也不是獨立強審PASS
  的來源；獨立R1內容審查PASS另由root所委派的reviewer回報。
- 原 author 工作樹的tracked product diff為空，只有四個預期untracked規劃檔；四檔逐一以
  `git diff --no-index --check /dev/null <file>`驗whitespace，無個人絕對路徑。

因此可用舊runtime做6 Yellow的正式進件準備；owner已建立、獨立內容R1已PASS，
但仍是`awaiting-freeze`，不得直接dispatch。正式gate仍須以真builder封套核對
0／0宣告與七組不變式，不能把先前機械bypass補寫成能力核准。

## 驗證邊界與後續

四檔使用`doc-coauthoring`做跨文件一致性與reader問題自查，使用`preflight-ci`
把required CLI／changelog／full tests／PR-aware policy先列入Tasks首行。
root既有獨立reviewer的R1內容審查已PASS，本輪不另外啟動模型；正式Cortex
plan-review與freeze仍由root後續依法定流程完成。

尚無產品candidate，故不跑full pytest或policy-preflight，也不宣稱它們PASS。
future candidate按manifest跑OpenSpec/full tests、帶真PR context跑pinned
v1.0.17 policy；fragment需commit，exact-head final及遠端CI／threads／merge
各自驗證。accepted frontmatter與機械ready不等於獨立review或capability ready。
CI workflow engine pin為`9e7fabbf0b5eea9ad933fa6798764b723934a0b7`；root核對
local preflight skill engine `b281c5da` 的 `policy_check` bytes與CI pin相同，
不代表同一SHA／執行身份或本PR已通過policy。

containment code acceptance用offline fault injection覆蓋真exception boundary；
直接AGY launch的ValueError仍傳播／零Popen。真非法timeout env的雙路徑proof是
下游timeout child責任，不讓本child反向等待尚未落地resolver。
本child通過後，root核對timeout預定base及實際runtime含保護再解除dependency；
安裝和#831 retry不在本輪授權範圍。
