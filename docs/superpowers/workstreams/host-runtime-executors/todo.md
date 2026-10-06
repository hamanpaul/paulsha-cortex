---
status: accepted
work_item: host-runtime-executors
---

# executor 改用主機上 runtime 的 CLI，release 不再封存 agent 執行檔

## Boundary

- Issue：`hamanpaul/paulsha-cortex#1293`。
- owner 裁決（2026-10-06）：保留 Trust Root。job 使用主機上 operator 實際在用、隨時更新的 executor CLI，
  不再由 release 封存。原因是 claude、codex、agy、copilot 這些 CLI 每天更新很多次，封存副本在發版當下
  就開始過時；而且 install-input 壓縮後約 433 MB，其中原始碼只有約 21 MB，每次升級或重試都要重新下載。
- 現況：job 只能執行 `/opt/cortex/toolchain/bin/` 下雜湊鎖定的 wrapper 與副本。這條規則來自 2026-08-25 的
  Phase 2 設計（`openspec/changes/archive/2026-08-25-phase2-install-docker-qualification`）。
  分 UID 的 job template unit 以 cortex 專用帳號執行，並設有 `ProtectHome=yes`。
- 參考主機上 CLI 的實際位置（只是範例，實作必須以發現機制為準，不得寫死）：claude、codex 是
  operator HOME 下 nvm node 的 npm global（`.nvm/versions/node/<ver>/`，codex 需要同目錄的 node）；
  agy、copilot 在 operator HOME 的 `.local/bin`。全部由 operator 擁有、會自動更新。
- 依賴：須在 `installer-launch-authorities`（#1289）合併之後開工，並建在它之上。兩者都會改 installer 與
  `qualification/run.sh`；production-parity 規則（RC 先啟動真實 job、harness 寫入走允許清單）由 #1289
  建立，本票沿用。
- 不在本票範圍：命令阻擋政策（#1283）。

## Tasks

- [ ] **T1 RED**：新增測試，固定現行行為：release／qualification 會封存四個 executor 執行檔；plan 不含
      executor 位置；job unit 看不到 operator HOME；job evidence 沒有 executor provenance。
- [ ] **T2 release 不再封存 executor**：release workflow、qualification 的 toolchain manifest 與 install-input
      不再包含 claude、codex、agy、copilot。install-input 只留原始碼 bundle、wheelhouse、install config，
      以及 cortex 自己需要的小工具（`srt`、`openspec` 是否保留，在實作時判斷並寫明理由）。
- [ ] **T3 plan 時發現 executor 位置**：以 operator 身分（非 root）解析每個 provider 的 CLI 實際路徑
      （PATH 加 realpath）與執行時需要的 runtime（例如 node），記錄需要掛入的最小目錄集合。寫進 plan
      讓 owner 審核，host overlay 可覆寫路徑。只記位置、不鎖雜湊；位置變動時（例如 nvm 換 node 版本）
      重新產生 plan，`cortex upgrade` 也會重新發現。
- [ ] **T4 job unit 唯讀掛入**：在分 UID 的 job template unit 中，以 `BindReadOnlyPaths`（或等價的 systemd
      機制）只把 T3 的目錄掛進 job namespace。`ProtectHome` 其餘範圍維持不可見；job 帳號不能寫入掛入的
      目錄。說明並測試 operator HOME 權限（0700／0750）與 bind mount 的互動。
- [ ] **T5 每個 job 的 provenance**：job 啟動時把實際執行的 CLI 路徑、`--version` 輸出與執行檔 sha256
      寫進 job evidence，供稽核。CLI 在 job 執行中途被自動更新替換時的行為要有定義與測試。
- [ ] **T6 升級遷移**：既有安裝經升級後，依 installer 的 drift／rollback 規則移除
      `/opt/cortex/toolchain` 的封存副本與 wrapper，改用 T4 的掛入。rollback 後能還原。
- [ ] **T7 RC**：容器建置時，在 operator 風格的位置安裝當下版本的 CLI（或明確標示的測試替身），證明 plan
      發現、唯讀掛入、job 啟動、provenance 記錄都可行；不再驗證封存的 toolchain。release 與 legacy-adoption
      兩個 profile 都綠燈。
- [ ] **T8 文件**：runbook 與 doctor／`service status` 說明 executor 來源與 provenance。新增 changelog
      fragment，並同步 `CHANGELOG.md [Unreleased]`。
