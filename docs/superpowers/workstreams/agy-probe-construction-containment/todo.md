---
status: accepted
work_item: agy-probe-construction-containment
authority_state: repository-intake-unfrozen
owner_issue: "hamanpaul/paulsha-cortex#851"
registration_state: repository-intake
dispatch_readiness: awaiting-freeze
domain_breadth: 0
state_consistency: 0
invariant_count: 7
artifact_classes:
  - source
  - tests
  - documentation
---

# AGY probe 建構 containment 工作清單

## Tasks

- [ ] **T0 authority/policy**：唯一 owner #851 已由root建立、獨立 agy_containment_review R1內容審查與fresh-reader五題PASS；registration已納入本PR repository intake，整合審查／full preflight待root後續完成，確認 spec R1–R7、design D1–D6、source revisions與 exact base；指定真 builder envelope 並由合法start流程完成 Cortex Yellow plan-review gate 後才 freeze／dispatch，不沿用 #824/#823 的 source owner，不自行啟動模型或改現場。
- [ ] **T1 tests/RED**：先在 `tests/test_model_identities.py` patch `model_identities.build_agy_argv` alias 拋 plain ValueError，fake discovery 回 canonical model，assert failed／smoke-failed／ValueError、原 identity、只有 models runner；舊 source 因例外外洩為真 RED。這張 focused RED 卡不先跑全庫 baseline、不修改 source／frozen plan。
- [ ] **T2 source/containment**：唯一 production `model_identities.py`，只把原 `build_agy_argv(...)` assignment 納入既有 smoke try；沿用 except Exception 與 `_failed_agy("smoke-failed", probe_exception_diagnostic(exc))`，不改 prompt／kwargs／common timeout／payload驗證，不改 launcher／planning_runtime／gate_ledger／cache。
- [ ] **T3 tests/runtime**：在 `tests/test_planning_runtime.py` 使用真 runtime constructor＋真 AGY probe、isolated registry與tmp worktree/cache、fake runner；兩種 roster順序均證 ready非AGYprimary仍可取得runtime、AGYfailed且不選為readysecondary、無AGYsmoke；patch alias而非mock整個probe，explicit新probe_cache_path避免cache假green。
- [ ] **T4 tests/boundaries**：在 `tests/test_coordinator_agy_launcher.py` 以 direct模式fake `_ARGV_BUILDERS["agy"]` 拋ValueError、fake Popen零呼叫證直接launch仍傳播；model identity測試另證KeyboardInterrupt/SystemExit不吞、原成功/發現失敗/runner失敗/rc/payload/fence/token/diagnostic不變。此fault injection不冒稱真timeoutenvproof，下游timeout候選另須真env雙路徑測試。
- [ ] **T5 documentation/CLI/docs**：更新 `docs/unified-work-lifecycle.md` 說明probe建構失敗只降AGYready、非AGYruntime可建但不保證異質secondary；以candidate環境checkout外cwd跑 `python3 -m paulsha_cortex.cli --help` 與 `python3 -m paulsha_cortex.cli run work --help` 並留退出碼，不啟動AGY模型／不改CLIproduction。
- [ ] **T6 changelog/docs**：新增並commit唯一 `changelog.d/agy-probe-construction-containment.md`、同步 `CHANGELOG.md [Unreleased]`；canonical builder branch從Manager以唯一owner #851導出 `feature/851-agy-probe-construction-containment`，不能拿planning branch當正式head；保持VERSION、policy pin、agent symlinks不變，核對README/docs引用與zh-tw PR `Closes #851` closing link。
- [ ] **T7 tests/policy/CLI**：先focused三個修改test檔＋`tests/test_planning_job_argv_687.py`；再完整 `python3 -m pytest tests/ -q`、`openspec validate --specs`、`git diff --check`。candidate commit後按preflight-ci帶真PR title/body/labels/base/head跑v1.0.17 pinned policy-preflight，核對R-09/R-16/R-19/R-22及R-14/R-20/R-23；不拿裸跑policy零fail或focused結果冒充full gate。
- [ ] **T8 review/dependency**：exact候選final review、remote current-head CI、threads、mergeability分別留證；修好／merged／installed不混用。root核對此child的containment證據、timeout預定base與實際runtime後才解除timeout-only block；安裝／retry需另外authority。本child不等待尚未落地resolver才可驗收，也不替下游忽略env／probe值。

## Boundary

- Spec：`docs/superpowers/specs/agy-probe-construction-containment-spec.md`。
- Design：`docs/superpowers/specs/agy-probe-construction-containment-design.md`。
- 唯一 production 模組：`paulsha_cortex/coordinator/model_identities.py`；其餘只限上述tests／documentation／changelog同步。
- 不修改 registry／schema／cache protocol／Manager／launcher／planning_runtime／gate_ledger／session；若需要第二個production模組先停止，回root重裁決。
- domain=0指單模組／單probe流；state=0指這次只改local exception-to-result控制流，不把既有runner宣稱為純函式。
- `invariant_count: 7` 對應R1–R7；source、tests、documentation全部列入，CLI／changelog／full policy也在Tasks首行，不為壓低band刪驗收面。
- 唯一 owner [#851](https://github.com/hamanpaul/paulsha-cortex/issues/851) 已由root建立並納入本PR repository intake；原author時期僅四檔、未操作registration的敘述是歷史。本整合僅更新八份文件，不代行root登錄、commit、push、產品實作或現場操作；`awaiting-freeze`與獨立內容審查PASS不等於Cortex正式gate已通過。

## Current integration gate

#851 當前真 completeness=true、6 Yellow，宣告仍為0／0／7；獨立R1與fresh-reader
PASS由root回報，正式gate／freeze待root完成本PR整合審查與full preflight後start。
root刻意不把下游timeout child登錄或auto-label／start；其marker不是zero-spawn
保證，需待#851產品、預定base與loaded runtime保護完成才可解除並正式登錄#824。
