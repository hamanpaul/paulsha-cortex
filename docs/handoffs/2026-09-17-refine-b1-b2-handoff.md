# Refine B1 收尾／B2 開工 handoff

日期：2026-09-17（Asia/Taipei）
機器：9900X（WSL2）
前一份：`docs/handoffs/2026-09-16-open-issue-triage-refine-handoff.md`
接手目標：依 #868 §3 繼續 B2；本文件記錄 09-16 → 09-17 做了什麼、使用者裁決、runtime 現況、還沒做的工作、以及兩條 cortex dogfood run 撞到的缺陷。

## 1. 使用者裁決（本輪新增，優先於本文件其他建議）

- **#829 不縮範圍**：by ticket，每張照它自己的 accepted spec 整張做、整張 merge；child 純核心（#849／#850／#866／#862）可獨立落地。
- **只 focus `cortex` instance**：`hippo-manager`／`conventions-manager` 先不動（仍跑 pipx 0.1.8 的 service-manager.sh）。
- **後續票派進 cortex 處理**：operator 只做進件（三件套至少 todo）、intake、needs_human 裁決、ship 卡住時場外交付；不在 job 內修平台。

## 2. 本輪落地（2026-09-16 → 09-17）

| 票 | 交付 | 方式 |
|---|---|---|
| #496 dirty recheck 冪等 | PR #916 | operator 直做 |
| #830 派工非 Job 決策契約 | PR #917 | operator 直做 |
| #814 裁決明示指令進 builder prompt | PR #918 | operator 直做（實證根因是缺明示語句，不是裁決沒進 prompt） |
| #503 slice-lane pinned spec 交付與 attestation | PR #919 | operator 直做 |
| #826 outcome taxonomy 三類訊號 | PR #921 | **cortex 派工**（run `workflow-22b00a5937e88c0090ab`），人工介入 5 次 |
| #922 reviewer `authority_hashes` 回聲 | PR #923（進件）＋ #924（bootstrap） | **cortex build＋verify、operator 場外交付**（run `workflow-d6e1901288f91577ecd9`） |
| 一次性動作 | PR #915（archive installer-shared-config-guard）；abandon `fix-rate-limit-classification`；roster 確認可跑 | — |

新開／補證據的票：**#922**（新）、#847（PR 建立觸發 authority-restart）、#911（fix-standard／feature-oneshot 無 openspec change 卡 ship）、#912（stale not_claimable ledger）、#882（review 卡的 bootstrap 死結）。
#831 早於 09-10 由 PR #869 完成，前一份 handoff 的 B1 清單過時。

## 3. 現況快照（2026-09-17 05:25Z）

- `main` = `811411ab`（PR #924）。Open issue 93。
- `cortex-manager`／`cortex-monitor`：pin `~/.agents/runtime-pins/cortex-811411ab-20260917a`（= main），drop-in `~/.config/systemd/user/<unit>.service.d/zz-refine-runtime-pin.conf`，舊 drop-in 皆有 `.bak-<sha>-<date>`；升 pin 手順見 `~/.claude/.../memory/cortex-runtime-pin-upgrade.md`（worktree add --detach → import 煙霧 → 改兩個 drop-in → daemon-reload → restart → 驗 cwd／PYTHONPATH／NRestarts）。
- pipx `cortex` CLI：本 handoff 同日由 0.1.8 重裝為 main（0.1.10）；`hippo-manager`／`conventions-manager` 的行程仍是舊版，**下次重啟才會載入新版**。
- daemon idle、attention 0、in_flight 0；`not_claimable` 3 筆（`bucket-c-evidence-dedup`、`installer-shared-config-guard`、`trust-root-agy-builder-grant`）全是 #912 的 stale ledger，`last_observed_at` 不再推進，不需處理。
- roster（`~/.agents/config/paulsha/model-identities.yaml`）：planning／review = claude sonnet（＋agy planning）、build = copilot gpt-5.4 → agy；codex spark 與 cg parked。

## 4. 未完成／下一步（照 #868 §3，不跳批）

1. **#911 ship lane**（建議下一張）：`fix-standard`／`feature-oneshot` 的 `openspec-propose` 確定性 passed 卻無產物 → `openspec_refs: []` → ship validator `multiple-delivery-targets-unsupported`，attention 只給 `abandon`。兩條 dogfood run 最後一步都靠 operator 場外 merge＋`retire-delivered`（#826）或 `abandon`（#922）收。修了它，cortex run 才可能零介入到 merge。
2. **B2 依序**：#825（quota 持久退避）→ #850 → #866 → #836 → #839；再 #600／#849／#835；#838 在共池並行前。
3. **B3**：#497、#862、#843、#547／#555／#577／#578。
4. **B4**：#895、#912、#808／#810、#841、#845。
5. 尚未處理的觀察：#847（PR 建立／operator 推分支都會觸發 authority-restart 重驗一輪，兩條 run 各多燒一次 verifier＋reviewer）；#546 家族（`blocking-findings`／`delivery-needs-human` 的 `next_actions` 只列 `abandon`）。

## 5. cortex 派工實測要訣（09-16／09-17 兩條 run）

1. **進件**：只有 todo（accepted、有 Boundary／Tasks）就能跑，planner 自己補 spec／design／plan（寫進 operator 本地 checkout、未 commit）。docs 進件 PR merge 後要等 monitor snapshot refresh（≤5 分鐘）才能 `work intake`，否則 `confirmed work authority missing or ambiguous`。
2. **監看**：`cortex status` 的列用 `slice_id`（`wf-<hash>-<card>`），不帶 work_id；用 run 建立後的 slice 前綴過濾。builder 卡 1–2 小時、log 100MB+ 是正常，看 pid 活著與 log mtime，不要重派。
3. **review 卡**：pin < `811411ab` 時 claude/sonnet reviewer 約 3/4 機率漏 `authority_hashes`，通過的 review 被整份拒絕 → `retry-card --card code-review`。`811411ab` 起 Manager 以 snapshot 補值，不應再卡；再卡就是新缺陷。
4. **裁決**：`retry-build --reason <單行> --payload <檔案路徑>`（payload 是**檔案路徑**，內容 `{"expected_candidate":"<40 hex>"}`）；#814 起裁決會以明示指令進 builder／reviewer prompt。
5. **ship**：卡 `multiple-delivery-targets-unsupported` → 等 PR CI 綠 → `gh pr merge --merge --match-head-commit <完整 40 碼 SHA>` → `retire-delivered`（run 有 pr_refs）或 `abandon`（無 pr_refs）。
6. **修採信端自己的票**（verify／review 採信、schema）：預期 review 卡過不了，一開始就規劃 bootstrap：cortex 跑 build＋verify 拿候選，operator `git worktree add --detach <candidate>` 審後場外開 PR；**先 abandon run 再開 PR**，否則 PR 觸發 authority-restart 多燒 job。
7. **並行 PR 固定衝突點**：`CHANGELOG.md [Unreleased]`、`docs/unified-work-lifecycle.md`（都插在「**升級與運維。**」前）；兩邊保留即可。

## 6. 新 session 開工 checklist

- [ ] `hostname`、`systemctl --user show cortex-manager.service -p ExecStart`、`git log -1 origin/main` 三者核對；pin 落後 main 就照手順升。
- [ ] `cortex status`（pin 或 pipx CLI 皆可，兩者現已同源）確認 attention／in_flight 為空。
- [ ] 讀 #868 §0.1–§0.3 與本文件 §1、§4；下一張從 #911 開始，派進 cortex（進件 PR → intake → 監看 → 裁決／場外交付）。
- [ ] 每張票先 `gh issue view N --comments`；修完要走到 issue closed（PR `Closes #N`），run 用 `retire-delivered`／`abandon` 收乾淨。
- [ ] hippo／conventions instance 不動；若要升，先確認使用者同意。
