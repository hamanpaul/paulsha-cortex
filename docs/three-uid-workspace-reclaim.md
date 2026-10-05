# 三 UID 部署的 builder 工作區回收（#1167）

## 問題

三 UID 部署下 per-job clone 由 Manager 建立，builder 以具名 ACL 取得讀寫權；builder 新建的 inode 只帶 builder／gate 的條目，Manager 沒有（`paulsha_cortex/trust_root/registry.py` `JOB_WORKSPACE_REACH` builder 列的既有邊界，#641 刻意不補）。Manager 因此讀不到 builder 的未追蹤檔、進不去 builder 建的子目錄，無法自己回收用過的 builder 工作區。

## 做法

回收拆成兩半，都走既有的降權邊界，不新增 root 執行面、polkit 放行或 Manager 對 builder inode 的 ACL：

1. **Manager 半**（`owner_reclaim.reclaim_through_builder_unit`，由 `worktree_reclaim.reclaim_worktree` 在 marker 帶 `owner_identity`／`attempt_id` 且 `PSC_JOB_RUNNER=systemd-template` 時呼叫）：
   - unit instance 就是工作區目錄名。那個名字本來就是當初那顆 job 的 `%i`（`job_workspace.job_segment(job_id)`），所以模板的 `ReadWritePaths=<pool>/%i` 恰好只涵蓋這一格。`job_runner.prepare_systemd_template(instance=...)` 逐字使用它，不再對已是 segment 的名字多算一次 hash。
   - 以 `spool_slot.provision_runtime_surfaces(instance=..., seed_credential=False)` 建齊模板所有 `ReadWritePaths`，否則 systemd 在 helper 起跑前就以 `226/NAMESPACE` 失敗。回收不跑模型，所以不複製 model credential。
   - 重設該 instance 的 commit-spool 格，在裡面開 `reclaim-preserved/`（Manager 與 builder 具名 ACL）與 reclaim log。build-log 格不重設，被回收工作區那顆 job 的 log 留著供診斷。接著寫 job spec，`systemctl start --wait` 起 unit；unit 結束後無論成敗都刪掉 spec。
   - unit 結束後先封住 commit-spool 格，把 preserve 區裡的封存全部移到 Manager-only 的 `<coordinator>/evidence/worktree-reclaim/`；以 systemd 回報的結果與工作區是否確實清空判定成敗，最後才交叉核對 helper 的完成紀錄，由呼叫端刪掉已清空的 pool 目錄（見下文「完成紀錄不是決策依據」）。
2. **builder 半**（`python -m paulsha_cortex.coordinator.owner_reclaim`，在 root-owned 模板 unit 內以 builder UID 執行）：驗證 Manager 核准，比對 marker digest，dirty scan、保存未提交內容與 `marker.base` 之後的本機 commit bundle，再清空工作區內容。掃描、保存或 bundle 任一步失敗就不刪任何東西。

## 涵蓋哪些工作區（#1261）

owner-bound 路徑只看 marker 有沒有 `owner_identity`／`attempt_id`，這兩個欄位由派工時傳給 `ScriptWorktreeCreator.create()` 寫入：

| 派工路徑 | marker 身分 | 三 UID 下的回收 |
| --- | --- | --- |
| slice lane（`autonomy._launcher_worktree`） | `{repo, work_id, slice_id}`，attempt 為 `uuid4().hex` | builder unit |
| workflow lane 會寫檔的 build 卡（`manager._dispatch_workflow_card`） | 同一形狀；`slice_id` 是這張卡的 job task（`wf-<run_id 雜湊>-<card>`），attempt 每張 job 一個 `uuid4().hex`，同時寫進 job 記錄 | builder unit |
| workflow lane 唯讀 build 卡（`BUILDER_WRITE_FORBIDDEN`，跑 `cortex-job-ro@`） | 不帶 | Manager 直接回收 |

唯讀卡維持原路徑，理由有兩個：

- `cortex-job-ro@` 把 `<pool>/%i` 列進 `ReadOnlyPaths`、不在 `ReadWritePaths`，helper 要清空工作區，在這份模板裡跑不動。
- 同樣因為外層掛載唯讀，唯讀卡的 job 寫不進工作區，工作區裡沒有 builder 擁有的 inode，Manager 自己的 dirty scan 讀得到全部內容。#716 的 deployment canary 實跑時，唯讀的 worktree-isolation 卡就是這樣回收成功的。

workflow lane 的 trusted-build reclaim（`manager._reclaim_trusted_build_workspace`）在呼叫 `reclaim_worktree` 之前，先以 Manager 自己的 job 記錄核對 marker：job 帶身分時，身分必須恰好是本 run、本卡導出的那一組，marker 的身分與 attempt 必須逐字相同；job 不帶身分時，marker 也不得帶。marker 被拿掉身分（想把回收逼回 Manager 的 dirty scan）、換成別人的身分，或自己長出身分，都以具名理由跳過，不刪任何東西。這一步只多加一道核對，Manager 撰寫的 job spec、marker digest、nonce、pool 範圍與 unit 身分這幾項綁定都照舊。

#1261 之前派出的 workflow build 卡沒有身分，三 UID 下仍回收不了，需要 operator 處理既有殘留。

## 威脅模型

### reclaim 以什麼身分、什麼權限執行

- helper 的身分只有一個來源：root-owned 模板 unit 的 `User=`（builder 帳號）。Manager 經 polkit 只能對列舉的模板 `start`／`stop`，選不了 UID，也帶不了屬性。
- unit 帶 `ProtectSystem=strict`，可寫面只有 `<pool>/<instance>` 與該 instance 的幾個 spool 格。helper 在 unit 內碰不到任何其他 pool 工作區。
- helper 沒有提權：它能做的，跑它的 builder UID 本來就能做。

### builder UID 本來能不能刪那個工作區

能。per-job 具名 ACL 給的是同一個 builder 帳號，所以就 DAC 而言，builder 帳號對每一個 builder 工作區都有寫權。真正把 job 限在自己那一格的，是 mount namespace（每顆 job 的 unit 只把自己的 `%i` 掛成可寫）。

因此「builder 直接呼叫這支 CLI」本身不構成提權：在自己的 job unit 內它只刪得到自己那一格，而那一格它用 `rm -rf` 也刪得掉。

### 那為什麼還要修

原本的「Manager 核准快照」名實不符：helper 只比對 `--marker-json` 與工作區 marker 是否相同，而 marker 對 builder 可讀，所在的 `.git/` 對 builder 可寫（可以整檔替換）。helper 無從分辨「這組參數是 Manager 決定的」還是「builder 自己抄來的」，所以它會把 builder 自己組的呼叫當成 Manager 已核准。

### 修法：以 builder 無法偽造的 Manager 核准為前提

核准就是 Manager 為這個 unit instance 寫的 job spec：

- 位置固定為 `APPROVAL_SPEC_SPOOL/<工作區目錄名>.json`（已安裝部署的 builder spec spool）。位置刻意不接受參數或環境變數，因為直接呼叫 CLI 的一方兩者都能控制。Manager 半若發現自己的 spec spool 不是這個位置，就不起 unit。
- spool 由 Manager 擁有（`0700` 加 builder 的唯讀 ACL），builder 不能新增、改名或替換其中任何檔案。helper 要求：
  - spool 與核准檔都屬 Manager UID，且沒有 group／other 寫入位（含 ACL mask）；
  - 核准檔是普通檔，以 `O_NOFOLLOW` 開啟；
  - spec 的 `instance` 等於工作區目錄名，`working_directory` 等於工作區；
  - `command` 是 `-m paulsha_cortex.coordinator.owner_reclaim` 後面逐字接著 helper 自己收到的 argv。
- argv 綁定工作區、pool、保存位置、marker digest（`sha256` of canonical JSON，涵蓋 `attempt_id`、`owner_identity` 與 `created_at`）與每次呼叫的隨機 nonce。
- 單次使用：unit 一結束 Manager 就刪 spec。刪除失敗而殘留的舊核准，也清不掉之後重新 provision 的同名工作區，因為新 attempt 的 marker digest 不同。
- Manager 只採信帶有本次 nonce 的完成紀錄；log 裡預先塞進的或舊的完成紀錄都不算數。

### 為什麼不選「CLI 拒絕非模板環境的呼叫」

builder 的模型 job 本身就跑在同一個模板（`cortex-job@<instance>`）裡。「在模板 unit 內」這件事分辨不出這次呼叫是 Manager 要求的回收，還是 builder 自己在 job 裡發起的。能表達 Manager 意圖的只有 Manager 寫、builder 唯讀的 job spec，而它本來就是既有、已安裝的資產，不需要新的權限計畫資產或安裝步驟。

### 完成紀錄不是決策依據

helper 把完成紀錄（含 nonce、保存數量、封存路徑）寫進 reclaim log，但這份紀錄出自 builder 可寫的位置：

- **DAC**：commit-spool 格對 builder 有 `wx`，log 檔是 Manager 預建的 `0620`，帶 builder 的寫入 ACL。任何 builder UID 行程只要走得到這條路徑，就能追加或截斷它。
- **mount namespace**：模板只把 `commit-spool/%i` 掛成可寫，所以其他 instance 的 builder job 看到的是唯讀，寫不進去。實際能寫的只有 instance 相同的 builder unit：reclaim unit 本身（裡面只跑 helper），以及另一個 stem（另一種加固剖面，或唯讀模板）仍在執行的舊 job。`prepare_systemd_template` 只檢查自己選的那個 stem。
- **nonce**：寫在 builder 可讀的 spec 裡，擋不住上述的共用者。

因此 Manager 的每個決定都改以自己觀察到的事實為準：

1. 起 unit 前，確認同一個 instance 的所有 builder 模板 stem（strict／jit × 一般／唯讀）都不在 active，否則不起 unit。
2. unit 結束後先封住 commit-spool 格。ACL mask 歸零，任何 builder 行程都走不進 preserve 區與 log。
3. 不論完成紀錄怎麼說，把 preserve 區裡每一個 helper 會建立的封存（`<instance>-<32 位 hex>` 目錄）都移進 Manager-only evidence。其他形狀的項目（symlink、檔案、名稱不符）留在原地，並讓回收失敗。
4. 以 systemd 回報的結果（`systemctl start --wait` 的回傳碼）判定 helper 成敗。成功時，Manager 再自己確認工作區目錄已經是空的，才讓呼叫端刪除它。
5. 最後才核對完成紀錄：帶本次 nonce 的紀錄必須恰好一筆，工作區名稱、保存數量、有無 commit bundle 都要與 Manager 自己讀到的封存一致（bundle 還要有 git bundle 檔頭）。任何不符都 fail closed，此時證據已經移走。

偽造的紀錄因此只能讓回收失敗，不能讓 Manager 跳過證據移交，也不能讓它刪掉沒有清空的工作區。

### 重送收斂：已清空的空 slot

helper 清空工作區時，位於 `.git/` 的 marker 也一起清掉。若 Manager 在刪除目錄項之前中斷（完成紀錄被判不符而 fail closed，或 Manager 程序本身死亡），pool 裡只剩一個空目錄，重送無法再走 owner-bound 路徑。`worktree_reclaim.reclaim_worktree` 因此把「pool 直屬、真目錄、沒有任何項目」的路徑直接 `rmdir` 並回報 `reclaimed`：判準是 Manager 自己列目錄看到的結果，不讀任何 builder 可寫的紀錄；空目錄不含證據，封存早已移進 Manager-only evidence。第一次失敗仍照常回報，由 operator 檢查後重送。pool 以外的空目錄照舊判為 `worktree-path-not-a-worktree`。

### 保存下來的證據

- helper 建的封存目錄以 `0770`（ACL mask 含 `w`）建立，Manager 的具名條目因此可以移動與清理它。
- unit 結束後（不論成敗），Manager 把封存從 builder 可寫的 commit-spool 格移到 `<coordinator>/evidence/worktree-reclaim/`（Manager `0700`，builder 無 traverse）。好處有兩個：之後同一 slice 重派時 `prepare_commit_spool(reset=True)` 不會把證據一起清掉，也不會卡在刪不掉 builder 目錄；builder 也碰不到已保存的內容。
- 若 `reclaim-preserved/` 裡還留著 Manager 沒有移走的項目（例如上一輪被判為非預期形狀而留在原地的東西），Manager 會拒絕重設該格，保留給 operator 檢查。

### 殘餘風險

- Manager 讀取的 marker 位於 builder 可寫的 `.git/`。builder 可以替換自己工作區的 marker，讓回收失敗（等同對自己那一格 DoS），或改動 `source_repo`／`base`。這些欄位只影響在 builder UID 下產生的保存內容，而那本來就是 builder 自己的產出。
- 回收依賴已安裝部署沒有覆寫 `PSC_JOB_SPEC_SPOOL`。若覆寫了，Manager 半會在起 unit 前 fail closed。

## 驗證

- 單元測試：`tests/test_owner_reclaim_1167.py`，涵蓋：
  - 核准的正向路徑；
  - 無核准、偽造核准（非 Manager 擁有、群組可寫、spool 可寫、symlink）、他人工作區、重放舊核准；
  - Manager 半：instance 名、單次核准、封存移出、拒絕重設未檢查的封存；
  - 偽造完成紀錄（追加、覆寫、helper 未執行）、preserve 區被塞入其他項目、同 instance 另有 builder unit 在執行；
  - 完成紀錄被拒後重送收斂於已清空的空 slot，pool 以外的空目錄不受影響。
- RC `release` profile 的 `owner-bound-reclaim` installed check（`qualification/driver.py::_installed_owner_bound_reclaim`）：
  - 以已安裝 Manager 的身分、runtime 環境與 `UMask` 建出兩格 production 形狀的工作區；
  - 以 builder UID commit 並留下未追蹤內容；
  - 驗證 builder 直接呼叫 helper 會因沒有核准而被拒；
  - 由 Manager 的 `reclaim_worktree` 經 polkit 起模板 unit 回收；
  - 核對核准已消耗、封存在 Manager-only evidence 樹且 builder 讀不到、foreign 工作區的 bytes／owner／ACL 不變，重送結果為 `absent`。

  CI 單元測試不能代替這份容器證據。
