# Changelog

本專案所有重大變更都會記錄在此檔案。

格式基於 [Keep a Changelog 1.1.0](https://keepachangelog.com/zh-TW/1.1.0/)，
本專案遵循 hamanpaul project policy v1.0.7。

## [Unreleased]

### Changed
- **builder `exited` 不再直接走 completion shadow path**：coordinator 現在會先固定 Candidate、重新驗 pinned inputs，並以 deterministic ResultVerification 執行 required artifacts、persona scope、typed argv checks、task tests 與 base/candidate full-suite 比較；只有成功驗證才把 slice 推進 `verified` 或 `reviewing`，其餘一律 fail-closed 到 `needs_human`，不再讓 `exited` 單獨滿足 DAG。
- **review-required slice 改為 exact-HEAD foreign review gate**：manager 現在會依 `PSC_PROJECT_CONFIG_ROOT/model-identities.yaml` 選擇不同 independence domain 的 reviewer，建立固定 Candidate 的 detached reviewer worktree，並把 `passed|rejected|absent` verdict 以 immutable GateEvaluation 落盤到 `evidence/review/`；缺 model / 同 domain / malformed verdict / stale HEAD / reviewer failure 一律 fail-closed，且只有 formal category enum 中的 blocking finding 會拒絕通過。
- **dependency release 改為 CompletionRecord + target ancestry gate**：manager 在 `passed|verified` 後會先對 target ref 做 fetch/ancestor 檢查，寫入 immutable CompletionRecord 並把 slice 標記 `completed`；readiness 只接受 `slice_state=completed` 且 CompletionRecord/hash 對齊、Candidate 仍是當前 target ancestor 的 upstream，dispatch 下游 worktree 也改以 target ref SHA 當 base 並在發車前重驗 ancestry，避免未合併或 stale-head upstream 被提前釋放。
- **新增 persisted `slice-action` recovery 與 attention status**：`cortex slice-action <slice-id> <retry-build|retry-verify|retry-review|abandon> --actor <text>` 會透過既有 control request queue 交給 manager 單一 writer 執行；action entry 會保存 `requested_at/consumed_at/result`，`status` 快照新增每個 slice 的 job/gate/ancestry/evidence 摘要與 `next_actions`，並額外彙整 `attention` 一次列出全部 `needs_human` slice。
- **新增 dispatch discipline disposable canary 與 README 操作契約**：新增 `tests/test_coordinator_dispatch_discipline_e2e.py`，集中覆蓋 missing artifacts、same-domain foreign review absent、stale reviewer input audit-only、candidate merge ancestry、dependency base pin、completion restart 補完與 reaper negative safety；README 同步補齊 Job/Slice/Gate 語意、verification/frontmatter trust boundary、identity 設定、completion/restart 以及 operator action/status 用法與 reaper best-effort 限制。
- **deck frontmatter emit 契約與 runtime parser keyset 對齊**：`EMITTED_FRONTMATTER_FIELDS`、deck compile frontmatter 與 `parse_spec_frontmatter()` 現在一致包含 `target_branch` / `verification` / `parse_error`；compile 產生 hold spec 時固定輸出 `null` 欄位，runtime 僅接受 `parse_error: null`（non-null fail-closed），避免 deck contract alignment 漂移。

### Fixed
- **CompletionRecord 會重新驗證並綁定全部 evidence**：readiness 現在會嚴格驗證 GateEvaluation schema，並要求 verification/review evidence 的 Slice、Candidate、builder/reviewer job、狀態與 CompletionRecord 一致；target ref 也必須對應宣告的 remote/branch，避免以跨 Slice 或跨 Candidate 的合法 hash 證據繞過 dependency gate。
- **manager 會重新驗證 verification evidence 後才套用結果**：`complete_tick()` 現在會自行驗 schema、candidate、證據檔 path/hash 與落盤內容一致性；`verification_runner` 回傳 forged payload/path/hash 時一律 fail-closed 到 `needs_human`，不再把 Slice 或 handoff manifest 誤推進 `reviewing` / `verified`。
- **Task 3 剩餘 fail-closed 缺口已補齊**：pinned-input mismatch 重讀 spec 時若遇到 non-UTF-8 / parse failure，現在會回傳明確 mismatch reason 並照常把 slice 轉進 `needs_human`；verification evidence finalize 改為 no-clobber，若 create-after-check race 期間冒出衝突檔案，會隔離既有證據並 fail-closed 拒絕覆寫。
- **slice repin 不再繞過合法狀態轉移**：`JobRegistry.repin_slice()` 現在只允許 `pending` / `needs_human` slice 重派；它會保留 slice state、透過 validator 合法地把 `gate_state` 重設為 `pending`，並拒絕 terminal slice 的非法 rewind。
- **Task 3 review fixes now fail closed on contract drift and full commit IDs**：`verification.required_artifacts[].must_change` 現在只接受實際 boolean；verification evidence candidate 只接受完整 40-char commit SHA；manager 對 builder `exited`/`failed` 兩種終態都會先做 pinned-input drift 檢查，drift 一律升級為 `needs_human`。
- **Task 3 verification follow-up 會嚴格 fail-closed**：spec frontmatter 的 `target_branch` 只要存在就必須是非空字串，`dispatch:hold` 不再默默吞掉 malformed value；既有 verification evidence 若是可解析 JSON 但 schema 無效，現在也會先隔離到 quarantine 再拒絕覆寫。
- **Task 2 review follow-up 對齊 notifier/registry state contracts**：`coordinator_telegram_notifier` 改以 `exited|failed` 判定 Task 2 終態；`JobRegistry` 現在會拒絕持久化或更新指向不存在 job 的 `builder_job_id` / `reviewer_job_id` slice 參照。
- **coordinator slice read path 不再回傳共享 history refs**：`JobRegistry.get_slice()` / `list_slices()` 現在會複製 history/action entries 內的巢狀 `refs` 清單，避免呼叫端 mutate 回傳資料時污染 live registry state。
- **control queue 會正確尊重 request override 與 dead-daemon 狀態**：queued `dispatch`/`fanout`/`tick` 現在以 request 自帶的 `handoff_dir` 建 readiness predicate，`complete` 在未提供 `specs_dir` 時不再多做 spec scan；`control.client.read_status()` 若看到 daemon pid 已死亡，會立即回報 `degraded_reason=dead`，不再短暫誤報健康。
- **`cortex reap-brokers` 失敗時改回 non-zero exit**：操作員手動執行 cleanup 時，若腳本缺失、無法 exec，或腳本以非零碼結束，CLI 仍會印出 JSON summary，但現在會回傳 exit 1，避免把未執行/失敗的 cleanup 誤報成成功。
- **service installer 會持久化 manager Python 解譯器**：`cortex install service` 現在會把 `PY=<sys.executable>` 寫入 `~/.agents/core/runtime/<instance>-manager.env`，避免 pipx / venv 搭配 user systemd 時落回系統 `python3` 而找不到 `paulsha_cortex` 模組。
- **service installer 會持久化正確 repo root**：`cortex install service` 新增 `--repo-root`，會先驗證目標是否為 git repo，再把解析後的 top-level 路徑寫入 `PSC_REPO_ROOT`，避免 manager daemon 在 systemd cwd 下把 worktree 建到錯誤目錄。
- **hook 模板改為透過 `cortex relay-hook` 定位封裝腳本**：三份 hook JSON 不再硬編不存在的 repo 內路徑，也移除了不屬於 cortex 的 `psc-bro-return` glue；`relay-hook` 子命令會直接執行封裝內的 `psc-relay-hook.sh`，安裝位置改變時仍可正確解析。
- **停止 periodic automatic reaper，改為 scoped operator cleanup**：`tick` 與 manager daemon 不再自動回收 codex broker；新增 `cortex reap-brokers` dry-run/operator 路徑，`--apply` 必須搭配 `--cwd-root`，腳本會在送 `SIGTERM` 前重驗 `ppid/start-time/cmdline/cwd`，只清理同 project scope 內、身份未變的 broker。

### Changed
- **dispatch 會固定 v1 verification contract 與輸入 hashes**：`parse_spec_frontmatter()` 現在嚴格解析 `target_branch` / `verification` v1 contract、對未知鍵與非法 check 回報 structured parse error 並強制 `dispatch=hold`；spec-driven `dispatch` request 會把 spec/plan/verification SHA-256、target branch/remote 與 review policy 釘進 Slice，再由 manager 在 builder 結束時檢查 pinned-input mismatch 並 fail-closed 到 `needs_human`。同時新增 versioned verification evidence writer，對相同內容冪等重讀、對衝突內容隔離後拒絕覆寫。
- **coordinator state 改為 versioned `jobs+slices` foundation**：`jobs.json` 現在要求 `schema_version`/`jobs`/`slices` 根結構，legacy `done` 狀態與無版本舊檔會 fail-closed 要求 clean start；headless 完成語意改為 `exited|failed`，SliceRecord 會持久化 spec/plan hash、branch/base、builder/reviewer、candidate 與 evidence/action history。
- **mutable coordinator CLI 全改走 control request queue**：`fanout`/`tick`/`complete` 不再本地寫 registry，daemon 未就緒時會明確失敗；低階 `cortex dispatch --task ...` 因缺少 spec metadata 已拒用，只保留 `jobs`/`stat`/`ready`/`status` 為讀取路徑。
- **同步 policy 1.0.6 → 1.0.7（R-24 moc-alignment）**：`policy_version` 1.0.6 → 1.0.7；`Policy Check` workflow re-pin 引擎到 1.0.7 SHA `e24fbd6`（尾註 `# v1.0.7` 供 R-23 對齊）、`policy_version` / `policy_engine_ref` 同步；CLAUDE.md 補 v1.0.7 新增規則段（R-24）與白名單 `policy-exempt:moc-alignment`。
- **採用 policy 1.0.6 新模型（agent 慣例檔 symlink 單一真檔 + 引擎 pin attestation）**：`AGENTS.md` / `GEMINI.md` / `.github/copilot-instructions.md` 改為指向 canonical `CLAUDE.md` 的 symlink；`.paul-project.yml` 設 `agent_files.mode: symlink` 與 `conventions_engine.repo`，`policy_version` 1.0.2 → 1.0.6；`Policy Check` workflow re-pin 引擎到 1.0.6 SHA `261f3f6`（尾註 `# v1.0.6` 供 R-23 對齊）、`policy_version` / `policy_engine_ref` 同步；CLAUDE.md 補 v1.0.3–v1.0.6 新增規則段。修正 P0 傳播漂移（本 template 先前停在 1.0.2）。

### Added
- **新增 `tests.yml` CI 骨架**：生成的新 repo 出生即帶測試 gate——`tests/` 尚不存在時 job 自動跳過（綠燈），加入測試套件後 pytest 自動成為 PR gate，同時滿足 policy R-19 的 workflow 偵測
- 建立 `hamanpaul/new-project-template` 新專案 bootstrap skeleton
- 新增釘選到 `hamanpaul/paulsha-conventions` 的 `Policy Check` reusable workflow
- 新增同步的 agent convention files 與基本 policy metadata

### Changed
- **同步 policy 1.0.2**：bump `policy_version` 1.0.1 → 1.0.2（`.paul-project.yml` 與四份 agent convention files、`managed-by@v1.0.2`），caller `Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@98487868a098e22647074c677a58633ce4fa19be`（= engine tag `v1.0.2`，含 R-19 / R-20）；agent 檔追加 R-19（CI 必須跑測試）/ R-20（workflow policy_version 同步）說明與 `policy-exempt:ci-tests` 白名單項
- **同步 policy 1.0.1**：bump `policy_version` 1.0.0 → 1.0.1（`.paul-project.yml` 與四份 agent convention files、`managed-by@v1.0.1`），caller `Policy Check` workflow 的 `uses:` 與 `policy_engine_ref` 重新雙重釘選至 `hamanpaul/paulsha-conventions@4ff59b6c35a46a87af3c3e641975743ee8fa0858`（含 R-17 / R-18）；agent 檔追加 R-17（PR↔issue closing-keyword）、R-18（docs 對齊 WARN）與語言規範說明
- `Policy Check` workflow 改為雙重釘選 `hamanpaul/paulsha-conventions@8454aa1967b752ea38c82edd79a8439b5bde915b`，同步設定 reusable workflow `uses:` 與 `policy_engine_ref`

### Fixed
- 移除超出需求範圍的 `pyproject.toml` 與相關 package 化敘述
