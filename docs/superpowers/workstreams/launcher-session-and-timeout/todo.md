---
status: accepted
work_item: launcher-session-and-timeout
domain_breadth: 0
state_consistency: 0
invariant_count: 6
artifact_classes:
  - source
  - tests
  - documentation
---

# Headless launcher session（保留 canonical work_id）

## Authority

唯一 owner [#823](https://github.com/hamanpaul/paulsha-cortex/issues/823)；
[Spec](../../specs/launcher-session-and-timeout-spec.md)／
[Design](../../specs/launcher-session-and-timeout-design.md) 為完整契約。
正式 branch `feature/823-launcher-session-and-timeout`、fragment
`changelog.d/launcher-session-and-timeout.md`。原四檔 authoring 未寫產品／tests／CI／changelog／
registry，未 commit/push 或呼叫模型、操作服務與 live state。本 PR 是 repository intake：
root 已整合 #823 唯一 owner 及三件 path links、移出 #824，未登錄 #824 child，未 claim 本件。
以下仍是未交付的正式產品工作；`accepted` 不表示 freeze、資格或已可 dispatch。

## Boundary

- production 只改 `coordinator/launcher.py` 共用 kwargs/helper 與兩處 headless Popen；
  domain=0/state=0 的完整理由見 Design D6，不以測試檔數冒充 production 跨模組。
- #824 timeout／parser／CLI 值域與 #851 probe containment 均另案；不在本票實作或關閉。
  相關 #813/#568/#826 也不關閉。base 須含已合併 #820 的 AGY JSON／argv 修正。
- 不改 dispatcher、service、Manager kill/cancel、LaunchHandle pgid、registry、effort、
  gate timeout、runner 預設或權限；不承諾 systemd cgroup／daemon restart survival。
- 正式 artifacts 包含必要 tests/docs/CLI/changelog/policy/verification/review 與原 combo 的
  OpenSpec/archive／emitted plan；不能因單 production module 限制刪掉完整交付 gate。
- 現行 feature-oneshot 真純函式為 6/Yellow；#831 完整case 4/Yellow 僅投影。
  Root 已轉交獨立規劃 R1 PASS／fresh reader 結果；source-condition、exact authority 與
  審查結果仍須於真正 freeze／正式 gate 重驗採信，author 不派 builder。

## Tasks

- [ ] source/documentation T01：讀回 #823 與 exact spec/design/todo，記 source revisions/hashes；由 root 核對 #823 唯一 owner、本 canonical work_id、#824 移交、三件 path links 與 fresh base 含 #820，正式 branch/fragment 不漂移。
- [ ] tests T02：先建立 helper／launch recording RED，盤點 `_ARGV_BUILDERS` 五 executor × 三 runner 共15格；14個現有合法配置每次 Popen `start_new_session is True`，cg/template 保留 unknown-hardening 拒絕且 Popen=0，不 stub 掉該 guard。codex direct `bash -lc`、claude PIPE、degraded env/stdin/cwd 原 oracle 保留。
- [ ] source/tests T03：只在 launcher 抽出 `build_headless_popen_kwargs` 並讓 launch 實際使用；共同加入 literal True，保留 runner 後續覆寫與 stdout log。只修 helper 而漏接 launch 必被 T02 抓到，不改其他 production module。
- [ ] source/tests T04：正常與 stdin-only TypeError retry 都保留 session flag；dedicated fake 驗第一錯 stdin 只移除 stdin、第二次成功、非 stdin TypeError 立即傳出與第二次再錯停止。不得放寬 TypeError 來吞 session 參數。
- [ ] tests T05：按實際 base 盤點並最小更新 22 個固定 keyword-only fake（launcher 19/hook 3），保留原始安全斷言與 stdin retry coverage；既有 **kwargs fake 簽名維持，必要新增 flag 斷言。
- [ ] tests T06：新增 `tests/test_coordinator_launcher_session.py`，從 production helper 取得 kwargs，真自建 `bash -c 'exec sleep 30'` fixture；按 Design D3 先驗 PGID/SID 所有權再 group SIGTERM，5 秒內 wait/reap，parent 繼續，finally 有界且只清自建 handle／PIPE。
- [ ] tests T07：隔離負控制驗漏 helper 接線、漏 runner/executor flag、retry 掉旗標、吞非 stdin TypeError 皆 RED；去掉 session flag 的真 fixture 在 group signal 前 RED，killpg 次數為 0，finally 只 kill/reap 自建 child。不得用 mutant 殺 parent／真 job。
- [ ] tests T08：依 Design D5 跑 focused launcher/session/hook/AGY、trust-root runner/template、inner sandbox、reviewer/accounting、#820 planning 回歸，再 full `python -m pytest tests/ -q`；保留 network guard／tmp roots，mock AGY probe 與 systemd preflight，不呼叫真模型／daemon。
- [ ] documentation/CLI T09：核對 README/docs 的 session／cgroup／restart 邊界與 #824/#851 owner；跑候選 `cortex --help`、`cortex status --help`、`cortex stat --help`、`cortex dispatch --help`，後者只驗停用舊入口 help。無新 cancel/timeout 旗標或 operator 訊號動作。
- [ ] documentation/changelog T10：正式產品 PR 新增並 commit `changelog.d/launcher-session-and-timeout.md`、同步 `CHANGELOG.md [Unreleased]`；不改 VERSION，產品 PR 只 `Closes #823`。planning authoring 不代填產品交付紀錄。
- [ ] tests/documentation T11：依 feature-oneshot 保留 emitted plan、OpenSpec/archive、verification/review reports 與 Yellow adversarial gate；真 PR context 跑 policy_check，R-09/R-16/R-19/R-22 與其他適用規則、symlinks、diff check、四版 pytest/build/twine/wheel CI 全部留結果，不裸跑假綠或刪 cards 壓分。
- [ ] tests/documentation T12：完成 root 的獨立 plan review/source-condition/freeze 後才由正式 Cortex workflow 開始產品；candidate exact-head review/CI/merge 分帳，再從 checkout 外安裝候選 wheel、核對 helper module path 並重跑隔離真 session fixture。無 live Manager 重啟／signal；未執行項目不標完成。

## Verification status

原 authoring 僅執行 planning 純函式、source/static scope 與文件檢查，詳見
[報告](../../../../reports/review/refine-launcher-session-intake-20260907.md)。
獨立 reviewer R1 PASS 與 fresh reader 結果由 root 轉交，不是 author 自評；本 PR 後續
full preflight／CI 由 root 另留證據。產品 RED/GREEN、產品 review、freeze、merge、installed/live
仍未完成，12 項 Tasks 保持未勾。
