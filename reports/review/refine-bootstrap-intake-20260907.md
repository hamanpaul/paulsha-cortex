# Refine bootstrap intake 驗證與 #223 接續缺口

Runtime 與 issue 查證：2026-09-07 08:38 UTC；報告整理及檔案檢查隨後完成。此報告只交付 intake 文件，不宣稱產品缺陷已修、已派工或已部署。

## 產物與邊界

隔離分支 `feature/refine-bootstrap-intake-20260907`，基底 `79ba644780bf1c697c722ac24a297e7d02416100`。
僅新增以下六件 accepted 規劃材料與本報告；未 commit/push、開 issue、改 code/tests、註冊 `.cortex` links、改其他 worktree、registry 或服務。

- #831：`docs/superpowers/specs/sizing-stability-direction-{spec,design}.md`、`docs/superpowers/workstreams/sizing-stability-direction/todo.md`。
- #830：`docs/superpowers/specs/dispatch-decision-contract-{spec,design}.md`、`docs/superpowers/workstreams/dispatch-decision-contract/todo.md`。

#831 production 邊界僅 `paulsha_cortex/coordinator/planning.py` 的純計分與算法識別常數；caller、durable schema、loader、CAS 不改。歷史算法來源在說明/交付證據辨識，不引入 migration writer。若需越界，先重拆/重評。
#830 至少涉及 manager/daemon 兩模組及持久化狀態回應一致性，故 domain=1/state=1，不能降成單模組小修。

## 實跑結果

使用現行 runtime checkout 的 Python 模組，從 checkout 外的暫存目錄執行；runtime HEAD 同上述基底。`git status` 唯一列出的既存修改是 `paulsha_cortex/scripts/service-manager.sh`，本次未讀其內容或改動，不能宣稱整個 runtime tree clean。
實際 import 的 `coordinator/planning.py`、`manager.py` 等計分路徑沒有列為 dirty。

| Work item | 三件組完整性 | 五維分數 domain/state/acceptance/stability/orchestration | Band | 純 Yellow gate |
|---|---|---|---|---|
| sizing-stability-direction | 三件均 accepted，無 missing kind/marker | 0 / 0 / 2 / 2 / 2 = 6 | Yellow | ready=true；三檢查皆執行 |
| dispatch-decision-contract | 三件均 accepted，無 missing kind/marker | 1 / 1 / 2 / 2 / 2 = 8 | Red | 單獨函式檢查 ready=true；不代表 Red 可派工 |

combo 為 packaged `fix-standard`：gate_spine_count=2、cards_count=9、persona_binding_count=9；適用規則使用 runtime 的完整 `ACCEPTANCE_SURFACE_RULES`：R-09/R-16/R-19。
沒有改 combo、宣告數值或刪欄位迴避門檻。

Yellow gate 的 completeness 與 contract_compatibility 通過；用真 `load_model_identities()` 加 `_plan_review_envelope_lookup` 讀取目前投影，envelope observation 是 `{"bypass":"envelope_unavailable"}`。
這是可觀測的既有 bypass，不是已取得 measured builder 能力證據；lookup 使用純記憶體 run fixture，沒有執行 capability probe 或啟動模型。
來源：`manager.py:9315`、`model_identities.py:1480`。正式 dispatch 仍須重新依真 run/identity/candidate 狀態裁決，不可拿本 report 當 runtime authority。

實際 read-only CLI smoke（checkout 外、同一候選 Python/PYTHONPATH）各退出碼 0、stderr 空：

```text
python3 -m paulsha_cortex.cli work start --help
python3 -m paulsha_cortex.cli run work --help
```

未執行 start、retry 或 stat 的 live 變更；未執行產品 full pytest、CI、含 PR 上下文 policy 或 installed-wheel smoke。上述是現況入口 smoke，候選修正交付時仍須重跑 todo 要求的 CLI/fixture/完整驗證。
此 intake 範圍不允許新增 changelog 檔或 commit；產品 todo 已要求對應 fragment、CHANGELOG、policy、CI 與 exact-head 交付證據。

doc-coauthoring 的 fresh-reader 檢查回 PASS，未報 BLOCKER/MAJOR；僅確認範圍、0/1/2 映射、歷史邊界、forced-retry fail-closed、所有列管 consumer 與 Red 後續重評可理解，不能替代產品測試。
檔案檢查：六件內容 hash 全相符；逐檔 `git diff --no-index --check` 無 whitespace 診斷；`git status --porcelain --untracked-files=all` 恰好等於授權的七件新增檔案，沒有其他修改。

## 重現純函式檢查

以下等價 probe 以 `CORTEX_RUNTIME_ROOT` 指定現行 runtime checkout、`CORTEX_INTAKE_ROOT` 指定此 intake worktree；從 checkout 外執行。讀目前 identity 宣告但不寫任何 registry、不啟動模型。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$CORTEX_RUNTIME_ROOT" python3 - "$CORTEX_INTAKE_ROOT" <<'PY'
import json
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from paulsha_cortex.coordinator import planning, manager
from paulsha_cortex.coordinator.claim import sizing_band
from paulsha_cortex.coordinator.model_identities import load_model_identities
from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, load_cards, load_combo, resolve_combo_path

root = Path(sys.argv[1])
cards = load_cards(DEFAULT_CARDS_PATH)
combo = load_combo(resolve_combo_path('fix-standard'), cards)
identities = load_model_identities()
for slug in ('sizing-stability-direction', 'dispatch-decision-contract'):
    refs = (
        ('spec', f'docs/superpowers/specs/{slug}-spec.md'),
        ('design', f'docs/superpowers/specs/{slug}-design.md'),
        ('plan', f'docs/superpowers/workstreams/{slug}/todo.md'),
    )
    artifacts = tuple(planning.PlanningArtifact(k, r, (root / r).read_text()) for k, r in refs)
    complete = planning.assess_planning_completeness(artifacts)
    score = planning.compute_sizing_score(
        plan_artifact=artifacts[-1], completeness_report=complete,
        gate_spine_count=len(combo.gate_spine),
        applicable_contract_rules=planning.ACCEPTANCE_SURFACE_RULES,
        cards_count=len(combo.cards),
        persona_binding_count=sum(cards[c.ref].persona_binding is not None for c in combo.cards),
    )
    band = sizing_band(score.total)
    run = SimpleNamespace(steps=(), primary_domain=None, model_chain_override=None,
                          sizing_band=band, run_id='read-only-intake-check')
    gate = manager._evaluate_yellow_plan_review(
        artifacts, envelope_lookup=manager._plan_review_envelope_lookup(run, identities))
    print(json.dumps({'work_item': slug, 'complete': complete.complete,
                     'score': asdict(score), 'total': score.total, 'band': band,
                     'yellow_gate': asdict(gate) if gate else None}, sort_keys=True))
PY
```

## #223：已實作路由，不等於已接上自動拆分

GitHub 只讀核對：[原 #223](https://github.com/hamanpaul/paulsha-cortex/issues/223) 是 CLOSED，但 body 的「Red 自動回派 planner 拆分」驗收仍列未勾選。
08:38 UTC 核對 [#830](https://github.com/hamanpaul/paulsha-cortex/issues/830)、[#831](https://github.com/hamanpaul/paulsha-cortex/issues/831) 都 OPEN；兩者明文排除自動拆分修復。
以 open issues、limit=200 的 body 搜尋 `needs_decomposition|#223|Red.{0,40}planner|planner.{0,40}拆|自動.{0,15}拆分`，命中只有 #830/#831，未找到可直接承接的專票。這是該時間點/查詢範圍的佐證，不是「不存在任何其他 issue」的全稱證明；請 root 建 successor 前再次查重。

以下 source/test 行號都對上述 runtime 基底：

1. `coordinator/manager.py:9481` 在最後一個完整 plan step 檢查 Red；`:9494` 進拆分分支，`:9502` 呼叫純 `decomposition_route`，`:9508` 更新 facet，`:9529` 直接 return 無 job_id 決策。這條明確執行路徑沒有建立 planner Job，不能把它描述為「planner 已在跑，只需等待」。
2. `coordinator/work_bridge.py:568` 投影 facet 為 `needs_decomposition`；`coordinator/claim.py:1390` 的 active claim 分支只回 action/reason，`:1400` 是 `next_actions=()`，不走 ordinary resume。
3. `coordinator/claim.py:1524` 上限 2；`:1527` 的 `decomposition_route` 只選 needs_decomposition/needs_human，沒有 child lineage 或工作生成。
4. `tests/test_dispatch_needs_decomposition_223.py:94` 的 Red 測試使用 must-not-launch，`:108` 只斷言決策與 facet，`:116` 明定 jobs=[]；`:119` 驗深度 2，`:136` 驗重複 dispatch 仍停 plan。
5. `tests/test_claim_needs_decomposition_223.py:108`、`:117` 驗 manual/auto claim 浮現拆分而不 resume；沒有 planner 回傳子工作後的端到端接續證據。
6. bounded `rg` 掃 coordinator/control/deck 的 `decomposition_depth` 與 `decomposition_route(`，目前只找到 schema/registry 欄位、序列化、CLI 統計、上述路由。這份清冊是當下佐證；真正「不漏接續」必須由 successor 的 runtime invariant/integration tests 守護。

### 建議 successor 最小接續契約（尚未實作）

- Parent 保持 plan/needs_decomposition，專用 decomposition generation 以 parent run + exact source authority 做 CAS/idempotency key；不可將它當原 builder 的 resume 或把等待判成 done。
- 明確且可持久恢復的 planner decomposition Job；身份須通過 planner 的真 capability/independence/資格規則，禁止為 Red 偷用 builder 或假 envelope。planner 429、重播或 crash 不得重複發 Job/子工作。
- Planner 輸出 bounded child proposals，逐件具有獨立 scope、requirements、testability、task、真實 sizing 宣告、parent lineage/depth+1；manager 做結構、重疊、權威與深度檢查，不能只相信自然語言「已拆」。
- 子工作必須經正式受治理 intake/claim，保留 exact accepted revisions；parent issue authority 與子 issue/工作 ownership 的映射需在設計中明定，不得讓多 child 共用同一 claim key 衝突。若需新 GitHub 寫入，必須由已授權的正式動作處理，不由未審核 planner 任意發送。
- 深度已達 2，或 proposal 無法合法接納時 fail-closed，提供具體 needs_human/replan 理由；parent 完成需等待子工作正式 terminal 證據及 parent closure，不以建立子工作代替完成。
- RED/GREEN integration 至少驗 Red→planner→validated proposals→正式 child admission、depth 0/1/2、duplicate delivery、partial crash/restart、429 recovery、權威 stale、child scope overlap、parent closure；所有測試使用隔離 fixture，實際 canary 另留證據。

此契約可能需要真實多子項拆分，不能在沒有量測前猜成 Yellow。新 CLI/registry schema 是否必要、正式 child admission 動作名稱、單一或多 successor 的最小可交付切點皆未證實，應由 root 的新票設計裁決。

## 接手次序

1. Root 將 #831 intake 依正式來源/link/freshness 規則納管後，由 Cortex 派工；本報告不建立或釋放 authority。
2. #831 修正經 tests/review/CI/exact-head/runtime 交付後，使用該實際 runtime 對 #830 原始 scope 重新計分。
3. #830 若仍 Red，先拆出有獨立驗收的子工作；不得直接把目前 8/red 整包派進 build。純 Yellow gate 單獨 ready 不覆蓋 band 路由。
4. #223 successor 由 root 查重開票；#830 只保住合法非 Job 的結果契約，不聲稱補上 planner 接續。

## 受測文件 SHA-256

```text
3658370c6d1002711f6e797681dbff1acef9d79ae21d6ad88e11c5986674a710  docs/superpowers/specs/dispatch-decision-contract-spec.md
dd1f091f9fbf15b025741ed05030e09cd5418dd4ced6ab7d6c8b5bd4ec654857  docs/superpowers/specs/dispatch-decision-contract-design.md
5ec7b4f5990a750233acd520bfd8e3da932d7de61696adb556ce0f176bc3acb6  docs/superpowers/workstreams/dispatch-decision-contract/todo.md
7a6661453d2aef10a2e9799e3e5d516595243716a50332e7a756ef2dc4aa505b  docs/superpowers/specs/sizing-stability-direction-spec.md
2576596be86e30dce70669973051d3d49d02e4cfd5c4c7c76945e4651d3fa193  docs/superpowers/specs/sizing-stability-direction-design.md
10c1db60a64cee927ee09273c52ece6f076683ffaac3717937d080e1c5126ed5  docs/superpowers/workstreams/sizing-stability-direction/todo.md
```

這些是本次 intake 內容雜湊，不是手造 runtime gate evidence；後續若材料變更需重新計算並重跑驗證。

## 主流程整合與獨立複核

作者子任務完成後，主流程將本分支以ff-only推進至#832 merge
`60a3ffa867377b0c86fa2f10e91fb9820d91938c`，保留六件規劃bytes不變；補正式
work_id links、changelog、B0真實完成證據與後續動態進件地圖。#223接續專票已另建#833。

獨立reviewer再次讀完整bootstrap diff並重跑純gate，結果PASS、無未處置
BLOCKER/MAJOR；#831仍6/Yellow、#830仍8/Red，envelope unavailable仍明示。
Root亦獨立重跑同一純函式取得一致結果。B0只對應規劃交付，非產品完成；
凍結歷史、#828 ownership、PatchMUD pricing與未量測資格邊界均未放寬。
本地policy-only gate PASS；產品full tests／commit-aware preflight仍由主流程另跑，
不能把這次純gate結果代為填入。
