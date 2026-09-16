# AGY timeout-only 規劃與證據報告

日期：2026-09-07。原 author 時期僅交付四份 planning 文件，未註冊、freeze、
派工、實作、commit／push；這些「僅四檔／未操作registration」描述屬歷史。
目前root已將文件納入本PR整合，但刻意不登錄timeout child，#824產品尚未修好。

目前 dispatch readiness：**blocked-dependency**，不得先派。
新增 prerequisite=`agy-probe-construction-containment`；R1 fake 反證已成立，
不是可直接接受的殘餘風險。containment 完成且 root 核對 base／實際 runtime
保護證據前，下面 completeness／sizing／機械 gate 結果都不代表 dispatch-ready。
dependency 的唯一 owner 已由 root 建立為
[#851](https://github.com/hamanpaul/paulsha-cortex/issues/851)，其registration已由
root納入本PR repository intake；這不改本child的#824 owner或核心契約。

## Current integration gate

本整合工作樹已從#852 merge `984fce5b`快轉。root已在本PR登錄#851三件套，
並從母#823 mapping移除#824；但故意不登錄`agy-print-timeout-only`，owner
issue #824仍OPEN。root當次live核對#824/#851 labels均空且無相關run；這是
該時點的觀測，不是不會產生run的永久保證。

runtime `79ba6447` 忽略workflow文件的`dispatch_readiness`／`dependency_issues`，
`depends_on`只在舊slice lane起作用。root在todo加入真正的未決問題marker，
本author保留其逐字內容，純文件實算如下：

| 輸入狀態 | 真 completeness／plan assessment | 舊runtime分數 |
|---|---|---|
| 原author完整三件套（歷史） | true／accepted | 6 Yellow |
| 當前磁碟保留root marker | false／blocking-decision、missing kind=plan | 4 Yellow |
| 僅在記憶體移除marker（控制組） | true／accepted | 6 Yellow |

當前詳細分數是0+0+2+0+2=4；下降來自舊反向stability公式，不是減少0／0／7
宣告或驗收面。surface-only `plan_review_gate`和Manager wrapper仍可能ready，
envelope仍bypass；不等於真正completeness通過。更重要的是incomplete start
可能進brainstorm，故marker／metadata本身不能證zero-spawn。

當前操作邊界是root保持不登錄timeout child、不加`cortex:auto-on-going`、
不start #824；新work auto路徑需要該label。待#851產品驗收、timeout預定base
及loaded runtime保護均核對後，才可解除marker、重新接受並正式登錄。
本輪沒有解除block，沒有start／模型／現場操作，也不改resolver或tests契約。

## Scope 裁決

新 child=`agy-print-timeout-only`；#824 是來源，#823 留既有
`launcher-session-and-timeout` 母範圍。原author時期未更動母todo、
`.cortex/work-items.yaml`、GitHub issue或runtime；目前root已另做本PR登錄／
mapping整合，本八檔metadata工作沒有代改它們。唯一預定production是`launcher.py`；它原本就
import `gate_ledger`，既有 `_gate_timeout` 可滿足 default/fallback，無須第二個
production 模組。若後續發現需修改 helper，scope 必須先重裁決。

R1 發現的另一模組缺陷獨立處理：`model_identities.py:1071-1078` 在 try 外
呼叫 AGY argv builder；新增 timeout env 驗證會使 `ValueError` 穿透
`planning_runtime.py:1664`，影響 ready 的非 AGY primary。新 dependency 的
唯一 production 模組是 `model_identities.py`，將 construction 納入既有
smoke try／`_failed_agy` 回傳。timeout-only 保持 launcher-only、所有驗證及
probe timeout 值都保留，不改假值／預設值掩蓋錯誤；dependency owner票 #851
已由root建立且registration已納入本PR；不代表其產品／loaded runtime已通過。

dependency 本身用 builder fault injection獨立驗收，不等待本child的resolver。
本timeout candidate仍須補真非法env的direct launch／probe雙路徑與fresh tmpcache
的非AGYprimary runtime regression；已同步spec R7、design D5、todo T8。
不以dependency的fake結果冒稱真env已測，也不忽略probe值或跨模組偷修。

原 author 規劃 worktree 從遠端 main 的
`25c9a4e4040476523d0bb5f2132236f87b8d65f8` 建立；分支
`feature/824-agy-print-timeout-only-planning`。先 inventory，僅在新 worktree
執行 `git pull --ff-only origin main`，結果 Already up to date；既有 operator／
runtime-main 的工作內容保持不變。

## 為什麼需要此 child

#831 run `workflow-edbde4754ca662936aea` 的兩筆 AGY TDD job，各有自己的
print timeout；不是已證實的 quota 故障，也不是 planning_runtime 的 120 秒。

| job | AGY 工具／測試證據 | 終局 |
|---|---|---|
| `wf-30ca23c08c-tdd-red-42` | 全庫 baseline 18:40:08 開始；task log 停在 95% | 18:43:19 print timeout，1496 polls |
| `wf-30ca23c08c-tdd-red-44` | baseline 5648 passed、44 skipped、173 subtests，201.33秒；18:52:54 完成 | 18:53:20 print timeout，1495 polls |

兩筆在 timeout 前皆未寫入 tracked RED test。原 AGY 1.1.27 的預設為五分鐘；
現行 runtime HEAD `79ba6447` 的 `build_agy_argv` 未傳 timeout flag。
root 可另裁決 focused TDD 執行順序減少浪費，但這不代表已修正 deadline。

僅以工作識別與日期保存上述摘要，不將原始 session、thinking、credentials、
個人路徑或完整 stdout 帶入 repo。

## Source 與母文件對照

- [#824](https://github.com/hamanpaul/paulsha-cortex/issues/824)，初次核對的更新
  時點為 `2026-09-07T10:01:44Z`；當時依六項 AC 整理 spec R1–R7／todo T1–T9。
  root 後續正式修訂，最新唯讀 readback 為 `2026-09-07T13:40:04Z`。
- `launcher.py:1179-1249`：AGY argv builder；`:1918` 為 builder kwargs 呼叫，
  `:2257`／`:2262` 為 spawn；`gate_ledger.py:331-339` 為現有 fallback。
- 原author再次讀operator母todo時，它已包含「非法gate重用helper fallback」
  與「不能以 help 作 duration proof」兩项更正。本 child 獨立固定同語意，沒有
  反向改母 scope；不把較舊版本的矛盾當成當下尚未修訂的事實。
- root 查證 `manager.workflow_build_branch:3510-3528` 固定使用最小 issue 與
  work_id，因此本 child 的 build branch 必為 `feature/824-agy-print-timeout-only`。
  已修正原規劃任意更名的真缺口；canonical fragment 改為
  `changelog.d/agy-print-timeout-only.md`，母 `launcher-session-and-timeout`
  fragment 責任留 #823，不建立兩份重複 fragment，不改 Manager。
- 歷史時點：本報告初稿時 #824 AC5 仍為舊 fragment，root 已裁決但尚未更新
  遠端。此待辦已由 root 於 `2026-09-07T13:40:04Z` 正式完成：AC5 使用本 child
  fragment／branch，AC6 使用 sentinel proof，另明列 fullmatch／range 及 #851
  dependency。本author當時已唯讀核對；目前root已移除母mapping的#824，但
  刻意不登錄timeout child，block不解除。沒有把issue更新冒充產品／runtime完成。

## 原 author Sizing 證據（歷史）

[#208](https://github.com/hamanpaul/paulsha-cortex/issues/208) 的真 rubric：
domain 0=單模組／單資料流；state 0=純函式／local state。本 child 明列一個
production 模組、既有 env 讀取與 local argv，沒有新 schema／持久化／migration／
concurrency，故兩者都為 0；pytest／docs／changelog 不偽裝成 production 模組。

`work_bridge.current_sizing_snapshot` 固定使用 R-09/R-16/R-19；實際載入現行
feature-oneshot 得核心 gate=4、card=11、persona binding=11。完整 spec/design/plan 在舊
`compute_sizing_score` 仍為 spec_stability=2；門檻沿用
`claim.sizing_band` 的 Green≤3、Yellow≤6。原author以該runtime實算完整文件：

```json
{"domain_breadth":0,"state_consistency":0,"acceptance_surfaces":2,"spec_stability":2,"orchestration":2,"total":6,"band":"yellow"}
```

這不是要求 runtime 套用 #831 的尚未落地新算法，也不省略任何 acceptance
surface；Yellow 強 plan review 與 owner／authority 裁決仍獨立必要。
四檔作者提供的 accepted frontmatter 不等於 reviewer 已PASS或work已授權。
本段6 Yellow只保留為歷史；當前帶marker的磁碟真狀態與記憶體控制組見current
integration gate，不可沿用此數字宣稱目前完整或可派。

## CLI parser 實測

使用已安裝 AGY 1.1.27；無憑證 env、無 prompt、stdin EOF，timeout flag 後加
`--cortex-timeout-parser-sentinel`，完整讀完 stderr保留退出碼，全部 exit 2：

| value | 第一個錯誤 |
|---|---|
| `abc` | `time: invalid duration` |
| `2400` | `time: missing unit in duration` |
| `2400s` | unknown sentinel flag |
| `9223372036s` | unknown sentinel flag |
| `9223372037s` | `time: invalid duration` |

這是 R1 上界的已安裝 parser 證據。#824 所列 `--print --input-format ...`
命令在這個已安裝版本會先把下一 flag 當 prompt，所有 value 都得到相同 prompt
形狀錯誤，不能據此判斷 duration 是否解析；child 使用可區分的 sentinel 負控制。
`--help` 一樣不能使用。曾嘗試 explicit 空 print 的合法值會進 startup，因
HOME=/dev/null 不可建檔而退出；該次不計為純 parser proof，沒有真實 prompt或
模型呼叫，後續只採用在 flag parser 必停的控制形狀。

後續獨立佐證：root 回報於 2026-09-07 21:38（Asia/Taipei）對 AGY 1.1.27
再跑同五列離線控制，全部 exit 2；`2400s` 與 `9223372036s` 的實際 first line
皆為 `flags provided but not defined: -cortex-timeout-parser-sentinel`。
這是 root 的再驗證，不是本 author 本輪重跑 CLI；舊 print 形狀不能作 proof
的限制也已正式寫入更新後 #824 AC6，不再是尚待遠端同步的建議。

## 原 author 規劃檢查（歷史）與後續 gate

原author使用外部cwd、Python `-B`與明確runtime import path，確認載入
`79ba644780bf1c697c722ac24a297e7d02416100` 的實際模組；沒有寫入 bytecode。

- spec/design/plan 三件套 `assess_planning_completeness.complete=true`，三份均
  accepted，missing kinds 與 blocking markers 皆空。
- `current_sizing_snapshot` 直接讀本四檔中的三份規劃與 runtime combo，回
  `(6, "yellow")`，詳細五維與上列一致。
- `_evaluate_yellow_plan_review` 走實際 Manager wrapper，completeness 與
  contract_compatibility 通過；另外將 R-22 加入契約檢查也通過。
- envelope 欄位明示 `bypass: envelope_unavailable`，因本輪沒有指定真 builder
  profile；不是 builder capability 已證實，也不是獨立強 plan review 已PASS。
  root 定案模型後仍需帶真 profile 與 authority 做正式 gate。
- 初次機械檢查抓到 Tasks 首行缺 `documentation`，已在 T6 首行補正並重跑
  上述檢查；沒有以降低 artifact_classes 或排除 policy 的方式繞過。
- 原author四份新檔逐一用 `git diff --no-index --check /dev/null <file>`驗whitespace；
  當時tracked diff為空、只有四份預期新文件，無個人絕對路徑或未定marker。
  目前root已新增真正marker，歷史complete=true結果不能代表當前磁碟。

原author未跑無產品差異的全庫測試或policy-preflight，不聲稱policy PASS。
本輪八檔整合仍不代跑root的fresh integration review／full preflight，root將在
本PR另期完整執行；其必要性已明列todo。

實作完成後以 `preflight-ci` 規範跑 manifest 的 `openspec validate --specs`
及 `python3 -m pytest tests/ -q`；policy版本為v1.0.17，CI workflow 的引擎 pin為
`9e7fabbf0b5eea9ad933fa6798764b723934a0b7`。root 另回報當下 local preflight skill
engine 為 `b281c5da`（v1.0.17 release-ledger merge），並已用 git diff 核對
`policy_check` bytes 與 CI pin 無差；兩者不宣稱為同一 SHA，本 author 本輪
沒有另跑 local engine 或把此比對當 policy PASS。提交 fragment 後帶真正 PR title、
body、labels、base main、implementation head 驗R-09與其餘PR-aware規則；
單純裸跑policy的零fail不採信。最終 exact-head審查與遠端CI、threads、merge
各自驗證；部署／既有run正式retry另由root依既有授權安排。
