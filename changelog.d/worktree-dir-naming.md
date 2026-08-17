# worktree-dir-naming

- **`#645`：模板 unit 的 `%i` 與 worktree 目錄名永遠對不上——降權派工從未經正式路徑
  成功啟動過任何 job**——兩條命名鏈各自導出：`seams.ScriptWorktreeCreator.create()`
  以 **branch slug** 命名工作區（`autonomy._branch_for_slice(slice_id)` →
  `feature/<slice_id>` → 目錄 `<pool>/feature-<slice_id>`），而模板 unit 的
  `ReadWritePaths=<pool>/%i`（permgen `with_job_segment("%i")`）期望的是
  `job_runner.prepare_systemd_template(job_id=…)` 由 **job id** 算出的 instance 名。
  兩者永遠差一個 `feature-` 前綴 ⇒ `ReadWritePaths` 指向不存在的路徑 ⇒ systemd 在建立
  mount namespace 時就失敗（`Failed to set up mount namespacing: …: No such file or
  directory`，`226/NAMESPACE`），job 連起都起不來。operator 0817 以**真實 dispatch
  路徑**的功能 smoke 撞到；M1 的正向 smoke 用的是**手工組的 job spec**，而手工組時
  自然會挑一個與 instance 名相符的 worktree 路徑，等於把這個 bug 繞過去（#584／#623
  記錄的同一條方法論教訓：手工 spec 只能驗隔離，驗不了功能）。
- **修法（operator 裁決）：改目錄名這一側，不改 instance 名**——模板 unit 只有 `%i`
  可用、推不出 branch slug，所以要對齊只能讓目錄名讓步；而且「job 的工作區以 job id
  定址」與登記表既有的 per-job 模型一致（spool、sentinel、gate ledger 全都以 job id
  定址）。**branch 名完全不變**（仍是 `feature/<slice_id>`），只有磁碟上的目錄名改。
- **單一推導點，不是「兩邊各自算、剛好相等」**——新增
  `coordinator/job_workspace.py:job_segment(job_id)`，它是全 repo 唯一產生這個字串的
  地方；`job_runner.template_instance_id()` 改為委派給它（形狀逐字不變，因此既有部署
  的 spec spool 檔名、polkit pattern 與 unit 名都不改變），`job_runner.INSTANCE_NAME_RE`
  改為別名到 `job_workspace.JOB_SEGMENT_RE`。`seams.ScriptWorktreeCreator.create()` 的
  簽章加上**必填**的 `job_id` 關鍵字（`WorktreeCreator` 協定同步），目錄名一律由
  `job_workspace.workspace_path()` 導出。留預設值等於留一條「忘了傳就退回舊命名」的
  復發路徑，因此刻意不留。
- **不變式測試**（本票的全部價值）：新增 `tests/test_worktree_dir_naming_645.py`——
  直接比對**兩個真實推導函式**的輸出（真 git repo 上 provision 出來的目錄名 vs
  `job_runner.template_instance_id()`），不對常數斷言；另加一條突變守衛（修法前的
  branch-slug 目錄不得再出現）、一條接線測試（`autonomy._launcher_worktree()` 交給
  provisioning 的 id，就是 `launch(slice_id=…)` 之後交給
  `prepare_systemd_template(job_id=…)` 的那一個）。**完整 `prepare_systemd_template()`
  的那一條**還會多跑一層 preflight（帳號／group／模板 unit／shim／spool），這些是 OS 層
  前置物，單 UID 的開發機與 CI 沒有——比照 #638 的教訓**明確 skip 並列出缺哪一項**，
  不靜默通過。
- **既有部署的殘留（`feature-<slice_id>` 形狀的舊目錄）**：`gc.py` 與
  `worktree_reclaim.py` 的判準本來就是**形狀**（`job_workspace` 標記檔／`.git` 檔）而不是
  名字，兩者因此完全不受影響，舊目錄照樣掃得到、回收得掉。真正會漏的是
  `manager.apply_slice_action` 與 `work_actions` 的 `recover-pre-candidate`：job／slice
  記錄沒有 `worktree` 欄位時它們**由 branch slug 反推路徑**（第三、四個各自導出的來源）。
  兩處收斂到新的 `worktree_reclaim.reclaim_recorded_or_derived()`——記錄有路徑就逐字用
  它（行為與修法前相同），沒有才由 `job_workspace.reclaim_candidate_paths()` 反推，
  **新舊兩種形狀都試**。少了這一步，升級當下磁碟上的舊殘留會被當成「不存在」而靜默
  略過，下一次 provision 直接撞 `worktree target already exists`（#601 的生產現場）。
  刪除仍完全走 `reclaim_worktree()` 的安全閘：**形狀不明的目錄一律 fail-closed，不刪**
  （#478 的 `.project-policy.yml` 資料遺失回報）。
- **canonical（workflow）lane 的已知邊界**：那條 lane 的工作區是 **per-run** 的——build
  卡 provision 之後，同 run 後續的卡沿用 `builder_jobs[-1]["worktree"]`，一個工作區對
  多個 job_id。因此「目錄名 ＝ 該 job 的 instance 名」在那裡**結構上不可能**成立，本次
  只把它的目錄名同樣收進單一推導點（傳 run 層級的 build 身分），並在程式碼與 PR 上
  明寫：canonical lane 要在 `PSC_JOB_RUNNER=systemd-template` 底下跑，得先把工作區從
  per-run 改成 per-job。slice lane 沒有這個一對多，不變式在那裡成立且有測試守著。
- **附帶（同一個 unit）**：`permgen.build_job_unit()` 把 `CollectMode=inactive-or-failed`
  從 `[Service]` 搬到 `[Unit]`。實機 `systemd-analyze verify` 報
  `Unknown key name 'CollectMode' in section 'Service', ignoring.`——語法不算錯，但
  「失敗的 instance 自動回收」這個用意整個沒生效，失敗殘骸會一直掛在
  `systemctl list-units --failed` 上、擋住同名 instance 的下一次 start。新增測試釘住
  它落在 `[Unit]`、且同段的 `Restart=` 仍在 `[Service]`（確保不是整段被搬錯）。
- **runbook**：`docs/superpowers/runbooks/trust-root-phase2b-setup.md` 第 5 步補兩條
  `systemctl cat` / `systemd-analyze verify` 稽核（RWP 的 `%i` 與 `CollectMode`），並在
  「5-6 正向驗證」前加上明確警語：那一段的 worktree 是 operator **手工**建在與 instance
  名相符的路徑上，**通過不代表降權派工起得來**，必須另跑真實 dispatch 路徑的功能 smoke。
