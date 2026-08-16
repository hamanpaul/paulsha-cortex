# infer-repo-root-hermetic

- **`#565` `_infer_repo_root` 不再被空 `.git` 目錄劫持，推斷測試 hermetic 化**——
  agent sandbox 基礎設施會在 sandbox 存活期間於 `/tmp` 暫態 `mkdir` 一個**空的**
  `.git`（teardown 後消失、`rm` 被防護 hook 擋下），舊判準
  `(parent / ".git").exists()` 把它當 repo 根，於是任何 `/tmp` 底下（含 pytest
  `tmp_path`）的 spec 路徑都被推斷成 `/tmp`。後果是 Manager gate ledger 對 builder
  candidate 重跑全套 pytest 時，只要那一刻 sandbox 存活，
  `tests/test_fix_dispatch_spec_path.py` 兩測必紅，**合格 candidate 被
  `GateContradictionError` 拒絕**（0816 run `workflow-7812abefede9d9b5d601` 實測，
  且與 builder 真實缺陷混在一起干擾判讀）。

  production 兩道判準：**(1) 有效性**——新增 `_is_git_repo_root()`，`.git` 為目錄時
  必須含 `HEAD`（`git init` 必寫），為檔案時必須以 `gitdir:` 開頭（linked
  worktree／submodule）；刻意不 fork `git rev-parse`，`_infer_repo_root` 在派工熱
  路徑上、對每個 parent 開 subprocess 的代價與 flakiness 都不划算，而檔案級判準已
  足以排除唯一實測到的偽陽性。**(2) 搜尋上界**——新增
  `_repo_search_boundaries()`（`TMPDIR` / `/tmp` / `/var/tmp`），共享暫存根本身永遠
  不是任何 spec 的 repo 根，向上搜尋到此即停；上界**之下**的真 repo（`/tmp/x/repo`）
  照常命中。

  測試 hermetic 化：新增 `tests/git_fixtures.py`（`make_fake_repo()` 造含
  `.git/HEAD` 的完整假 repo、`make_empty_git_dir()` 造污染形狀），全 repo 六個測試
  檔改用它，不再以「`mkdir .git` 空目錄」冒充 repo 根。
  `tests/test_fix_dispatch_spec_path.py` 補 8 個回歸：鏈上有空 `.git` 時必須穿過、
  只有空 `.git` 時落回既有 fallback、worktree 的 `gitdir:` 檔案仍算 repo 根、非
  `gitdir:` 檔案不算、共享根即使是**有效** repo 也不落錨、上界之下的 repo 照常命中、
  `/tmp` 與 `TMPDIR` 確實在上界集合內。污染一律由測試自備，host `/tmp` 當下的狀態
  不再影響任何判定。
