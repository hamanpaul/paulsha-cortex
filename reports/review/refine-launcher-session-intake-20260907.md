# #823 session-only planning authoring report

## Authority and scope

本報告保留 author handoff 並同步 root 轉交的審查結果，沒有 author 自評 PASS。唯一 issue owner #823、canonical work_id
`launcher-session-and-timeout`；正式 builder branch `feature/823-launcher-session-and-timeout`，
fragment `changelog.d/launcher-session-and-timeout.md`。#824 timeout 與 #851 containment 完全另案。

原 author branch `feature/refine-launcher-session-intake-20260907`，基底
`984fce5b86604aa71c6e8ecf82cc253a1441a106`。先盤點 main worktree dirty/untracked 與 worktree
清單，確認本 branch/path 不存在後才由 origin/main 建隔離 worktree；新 worktree 的
`git pull --ff-only origin main` 為 already up to date。未改 operator 原工作、runtime-main 或服務。

原 authoring 恰四檔：新 [spec](../../docs/superpowers/specs/launcher-session-and-timeout-spec.md)、
[design](../../docs/superpowers/specs/launcher-session-and-timeout-design.md)、修訂
[todo](../../docs/superpowers/workstreams/launcher-session-and-timeout/todo.md)、本 report。
該 authoring 沒有產品 code/tests/CI/changelog/.cortex/issue 變更，無 commit/push、dispatch 或模型呼叫。
doc-coauthoring 用於 owner／三件組對齊；test-playbook 用於 deterministic matrix、安全負控制
與 process cleanup。fresh reader／獨立 review 由 root 安排並轉交結果，author 未另派 agent 或自評通過。

目前四件已由 root 逐 hash 複製至 `refine-runtime-intake-20260907` worktree，branch
`feature/refine-runtime-intake-20260907`、base `2c1bba01`。本 PR 是 repository intake／
unfrozen，未正式 freeze、claim 或 dispatch，仍 6/Yellow。本次僅在新 root worktree 同步
四件 metadata／歷史時態，原 author worktree 不變；root 的進件 links 與後續 full preflight／
PR CI/commit 證據另記，不能把原 author 的「未執行」誤讀為整個 root PR 永遠不做 gate。

## Readback and source condition

- 已全文讀 GitHub #823（open、updated `2026-09-07T10:01:40Z`、comments 空）與 #208 rubric。
  #823 的歷史錯行號不照抄，以下重新對照 author 基底 source。
- root 最新 session-only todo 來自 `refine-agy-dependencies-20260907` worktree，SHA-256
  `4fccca59be4efbada3f49b8e742acdc9063f999a7937baba53f14589f3a9b0e1`；只讀未回寫。
  新 todo 延續該拆分，補齊 spec/design、rubric/AC、helper 真接線與安全 fixture；名稱不變。
- `git merge-base --is-ancestor 79ba644780bf1c697c722ac24a297e7d02416100 HEAD` 成功，基底包含
  #820。正式 source-condition 還要在 freeze 前核對 fresh remote base，不能永久沿用此 snapshot。
- 原 author 基底 984fce5b 的 `.cortex/work-items.yaml:851–859` 曾在本 work_id 連 #823/#824，
  只有 todo path。Root 現回報本 PR 已整合 #823 唯一 owner 與三件 path links，#824 已移出，
  未登錄 #824 child，也未 claim 本件；本 metadata 同步不修改 `.cortex`。
  repository links 不等正式 frozen authority；真正 freeze 前重驗唯一 owner、三件 exact
  refs/hashes、fresh base 與 reviewer input。純 helper 無法證明 live admission/source-owner
  已更新，不能拿 ready=true 直接 dispatch。

## Anchored source trace

以下相對路徑／行號以 author 基底為當下佐證，不宣稱窮盡所有未來 caller。

| 錨點 | 當下事實／本票處理 |
|---|---|
| `coordinator/launcher.py:1341` | `_ARGV_BUILDERS` 為五 executor；測試使用原 registry，不加 production 對照表 |
| `coordinator/launcher.py:1764` | launch 先 runner/role preflight，後續才 build argv；不移動安全前置條件 |
| `coordinator/launcher.py:2085` | direct builder `bash -lc`；review/degraded `bash -c` 原分支保留 |
| `coordinator/launcher.py:2086` | 共用 cwd/env/stderr kwargs，claude 加 PIPE；尚無 start_new_session/helper |
| `coordinator/launcher.py:2112`、`:2231` | systemd runner 仍覆寫 client env/stdin，template 另設 cwd=None；新 helper 不能蓋回去 |
| `coordinator/launcher.py:2257`、`:2262` | 正常與 stdin TypeError 兩處 Popen，共用同 dict；只 pop stdin，不擴退回 |
| `coordinator/launcher.py:609` | LaunchHandle 除 pid 已有多個既有 provenance/control 欄；本件不增 pgid、不改任何欄位 |
| `coordinator/job_runner.py:849` | runner unset=direct、非法 fail-closed；本件不改預設 |
| `coordinator/job_runner.py:696`、`:1959` | cg 刻意不在 template hardening profile，template launch 於 Popen 前拒絕；15 格 coverage 是14個合法配置＋此拒絕，不能新增權限湊15個成功 |
| `tests/test_trust_root_job_runner_p2a.py:104`、`test_trust_root_job_template_ab.py:105` | recording Popen 已收 **kwargs；沿現有 preflight/unwrap harness 驗最外層 wrapper |
| `tests/test_coordinator_launcher.py:758`、`test_headless_claude_hook_506.py:535` | 固定 fake 19+3=22 處；需兼容新 keyword，保留 stdin retry／原安全斷言 |
| `tests/test_coordinator_agy_launcher.py:344` | AGY recording fake 現在是 344，issue 舊 338 已漂移；不把只 stub argv 的案例冒充 launch 接線 |
| `tests/conftest.py:97`、`tests/network_guard.py:426` | tmp runtime/repo roots 與 network guard；真 sleep fixture 無 provider/network，不能關 guard 取巧 |
| `.github/workflows/tests.yml` | 既有 Python 3.10–3.13 full pytest＋build/twine＋wheel smoke；本 author 不執行或修改 CI |

production 路徑表的 `coordinator/` 前綴為 `paulsha_cortex/coordinator/`。
product focused/full 命令與 CLI help／installed fixture／完整 artifacts 已寫 Design D5、Todo T08–T12。
原 authoring 只 trace/source-read，未跑產品測試；新增 helper/session test 尚待正式產品工作。

## Rubric and pure verification

宣告 domain=0/state=0：唯一 launcher production module、local kwargs→既有 Popen；無 schema、
durable migration、跨 writer atomicity／reconciliation 或 Manager lifecycle protocol。測試真 OS
session 不等產品新增 concurrency state；若要求 cgroup/pgid persistence/cancel 則重裁，不能守 0 壓分。
Spec 的六個獨立 I1–I6 完整映射 Todo 與 matrix，artifact classes=source/tests/documentation。

root 指定現行 79 runtime 的 pure planning helper 與 packaged feature-oneshot：11 cards、
11 bindings、4 core gates；sizing rules=R-09/R-16/R-19，plan compatibility另含 R-22，
Tasks surfaces=source/tests/documentation/CLI。現行完整 accepted case 計 `[0,0,2,2,2]=6/Yellow`；
#831 `[0,0,2,0,2]=4/Yellow` 僅算術投影，未改 sizing 或 runtime。

原 authoring 實跑 root 提供的 `check-child-planning.py <author-worktree> launcher-session-and-timeout
feature-oneshot`，以明示 PYTHONPATH 指到 79 runtime，從 checkout 外 `python3 -B` 執行。
核對 module.__file__ 與 runtime HEAD=79ba6447，並以 git diff 證明 author 基底的 planning.py、
deck schema/cards/feature-oneshot 與該 runtime source 相同。僅載入純函式，無模型／dispatch。

- 三件組 complete=true，spec/design/plan accepted=true，reasons 空；三個 headings 精確匹配。
- 真 score=`[0,0,2,2,2]=6`、真 `sizing_band(6)=yellow`；cards=11/bindings=11/core gates=4。
  #831 另以 dataclass 的 spec_stability=0 算 4/Yellow，只投影不改 runtime。
- plan gate ready=true，completeness/contract_compatibility/envelope 全執行；最後一項為
  `bypass: envelope_unavailable`。不是 measured envelope、模型資格或進件／source-owner 證據。
- 12 組純 in-memory 負控制均拒絕：缺 spec、spec draft、design blocking、缺 Tasks、缺
  domain、缺 state、刪 source/tests/documentation/CLI/changelog Tasks 關鍵字、synthetic
  invariant limit=5。synthetic 只驗 6>5 會拒收，不能拿來做模型評測。所有變體未寫入文件。
- blocking design 控制使舊算法降到 4/Yellow，但 completeness=false；該錯向分數不能當可派。
- 四檔 newline/尾端 whitespace/無個人絕對路徑、7 個相對 Markdown links 與既有 focused
  test 路徑檢查通過；唯一不存在的 test path 正是明示待新增的 session fixture。
  Todo 12 項產品 Tasks 全未勾；git diff --check 通過，精確 scope 恰四檔。
- 原 authoring 的 production/tests/CI/.cortex/changelog/VERSION 與其 HEAD 無 diff；該階段
  未執行產品測試、CLI smoke、CI、policy_check、installed 或 merge。Root 本 PR 的 links 與
  後續 full preflight/CI 另記；不以裸 policy_check 或 source/helper 綠燈冒充。

author 自查後發現 cg/template 的既有拒絕，root 已獨立核對並採納 15 格 coverage inventory
的精確解讀；spec/design/todo 都已同步14個合法配置＋1拒絕。該 author 階段的 source 邊界
裁決不曾冒充獨立 review；後續獨立結果見下節。

## Reviewer handoff and residuals

固定判準：未處置 BLOCKER/MAJOR→FAIL；明示有界且文件列管 residual 不單獨 FAIL；
反對接受者須具體反駁其影響分析。Root 已轉交本 #823 四件的獨立結果：

- `agy_containment_review` R1＝PASS；另有獨立 offline constructor＋argv＋role/profile
  檢查，14 個配置 admit，cg/template 精確拒絕。這不是完整 launch／OS signal 證據，
  不能據此勾選產品 session fixture 或真 Popen 驗收。
- `agy_dependency_reader` fresh reader 五題全正確，未發現重大歧義；root 已全文讀回。
  審查歸因於上述獨立 reviewer／reader，結果由 root 轉交，不是 author 自評或其他 child 的 PASS。

以下保留 reader 的五個驗證問題：

1. canonical work_id／唯一 issue／builder branch／fragment 是什麼，#824/#851 是否本件？
2. 真 fixture 如何確實測 production helper，且哪個 oracle 能抓「helper 綠、launch 沒接」？
3. stdin retry 可以移除什麼，哪些 TypeError 必須立刻傳出？
4. 負控制缺 session flag 時，如何保證沒有對 parent group 送 SIGTERM，child 又如何 reap？
5. session 綠燈是否代表 systemd job/cgroup 或 Manager restart survival；純 Yellow 是否已可 dispatch？

有界 residual：不移 cgroup、不處理 Manager cancel／kill taxonomy、不保證任意 provider 孫程序
留在 group；完整 refine plan R13 其餘生命週期要求保留。真 fixture 採 `exec sleep`，使失敗路徑
只有本測試持有的一個 process handle，仍驗真 group signal，並明示不代表 descendants qualification。
本 PR repository source-owner/links 已由 root 整合；正式 authority/source-condition 仍在真正
freeze 時重驗，本 author 未自行補登、claim 或派工。產品 implemented/tests/CI/merged/installed/live
仍未完成；root planning PR 後續 full preflight 分帳，metadata 同步後四檔 hash 隨交接另報。
