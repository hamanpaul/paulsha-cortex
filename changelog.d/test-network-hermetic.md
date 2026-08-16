# test-network-hermetic

- **`#610` 測試套件不再打真實 github.com，並加上永久守衛**——現場 run
  `workflow-7812abefede9d9b5d601`（job 494）：builder 在 codex sandbox（network allowlist）
  單體 `python3 -m pytest -q` 跑到約 71% 被 `Network access to "github.com" was blocked`
  直接殺掉整個 process（`exit -1`），誠實的 builder（#609 honesty hint 生效後）因此永遠
  無法宣告 `passed`，合格 candidate 卡在契約外。這是繼 #565（`/tmp` 空 `.git`）、#586
  （AF_UNIX `bind` EPERM）、#608（`sun_path` 長度）之後**第四個 ledger gate 環境敏感點**，
  且是唯一一個屬於「測試自身 hermeticity 缺陷」的。
- **定位手法**：全套跑在 socket monkeypatch ＋ `PATH` 上的 `git`／`gh` 記錄 shim 底下
  （shim 記 argv、cwd、`ls-remote --get-url` 解出的**實際** remote URL，並以
  `pytest_runtest_protocol` 掛上 nodeid），另以 `unshare -rn` 斷網對照。命中兩處：
  - `tests/test_pre_candidate_recovery.py::test_candidate_worktree_dirty_reevaluation_on_tick`
    ——**真兇**，collection 順序 69.0%，緊接在 `test_porcelain_*` 批次之後（與 issue 描述的
    「約 71%／porcelain 批次條件性命中」吻合）。slice 的 `spec_path` 寫成相對路徑
    `specs/slice-3b.md`，`autonomy._infer_repo_root` 對它 `Path.resolve()` 時接到「當下
    cwd ＝ 真實 cortex checkout」上，於是 `manager.complete_tick` 的 completion 判準對
    **真實 repo** 跑 `git -C <真實 checkout> fetch --no-tags origin main`，origin 就是
    `https://github.com/hamanpaul/paulsha-cortex.git`。正常環境靜默成功、sandbox 整個
    process 被殺——測試結果從來看不出來。改用 conftest 既有的 `git_origin` fixture
    （origin 字面值仍是 GitHub HTTPS，transport 由 `url.<local>.insteadOf` 改寫到同一個
    tmp 目錄下的 bare repo），並把 `spec_path` 指到該 fixture repo 內的絕對路徑。
  - `tests/test_work_gc.py::test_cli_main_dry_run_text_and_json`——`gc.main` 是唯一會把
    `default_pr_status_provider` 接上去的入口，而該 provider 直接 spawn 真的
    `gh pr list --head <branch> --state all`（讀 operator 的真 token）。本次環境下該 repo
    沒有 remote，`gh` 在送出請求前就以 `no git remotes found` 短路，因此不是 71% 那次的
    死因，但它是同族的活火山。改注入本機假 provider，並反過來斷言 CLI 真的有把 provider
    佈線進 `run_gc`（比原本更嚴）。
- **永久守衛 `tests/network_guard.py`（session-scope，預設啟用）**：兩層攔截，違規當場
  raise 並**指名測試 nodeid**。
  - **socket 層**：`socket.socket.connect` / `connect_ex` / `socket.create_connection`。
    白名單 AF_UNIX（#586 家族）、AF_INET/AF_INET6 的 loopback（`127.0.0.0/8`、`::1`、
    `::ffff:127.x`、`localhost`、未指定位址），其餘 family 一律放行。
  - **subprocess 層**：`subprocess.Popen.__init__`（`run`／`call`／`check_output` 全部
    收斂於此）。實測四起事故全在這一層——socket patch 看不到子行程自己開的 socket。
    `git` 只檢查真的會走 transport 的 subcommand（`clone`／`fetch`／`pull`／`push`／
    `ls-remote`），並用 `git ls-remote --get-url`（該旗標不與遠端通訊）把
    `url.<local>.insteadOf` 改寫後的**實際** URL 解出來再判本機性，因此既有那批「字面值
    是 GitHub、transport 在本機」的 hermetic fixture 一個都不會被自己的守衛誤殺；`gh`、
    `curl`、`wget`、`pip` 等純網路 client 則一律視為違規（`--version`／`--help` 例外）。
  - **帳本後盾**：`verification._run_git` 這類 `except Exception:` 形狀會吞掉守衛的例外，
    因此違規同時記進 per-test 帳本，conftest 的 autouse teardown 無論如何都讓該測試失敗。
  - **逃生口**：`PSC_TEST_ALLOW_NETWORK=1`（在 `pytest_configure` 讀，早於會清掉 `PSC_*`
    的 `_clear_runtime_env`）整場停用；`@pytest.mark.network` 標記「本質上需要網路的整合
    測試」，預設排除於全套，`--run-network` 才跑。
  - **已知邊界**：守衛住在 pytest process 內，只看得到本 process 直接 spawn 的子行程；
    測試若先 spawn python 子行程、再由它去 `git fetch` 則看不到（實測目前全套無此路徑）。
- 新增 `tests/test_network_guard_610.py`（33 測試）自證守衛**會抓**（對外 TCP connect、
  origin 指向 github.com 的 `git fetch`、隱式 origin、遠端 URL `clone`／`ls-remote`、
  `gh api`、shell 字串形式、被吞掉的例外仍留帳）與**會放行**（loopback 往返、AF_UNIX、
  AF_NETLINK、本機 bare remote、`insteadOf` 改寫後的 GitHub origin、`status`／`rev-parse`
  等本機 subcommand、逃生口）。這些「會抓」案例在 syscall／spawn 之前就 raise，
  **不發出任何真實封包**，sandbox 內同樣安全。
