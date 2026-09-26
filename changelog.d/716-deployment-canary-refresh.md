# #716 deployment canary 基礎設施更新

- qualification toolchain 與 provider 模型改由 `qualification/contract.py` 單一宣告：codex
  0.157.1、copilot 1.0.88、agy 1.2.11；codex `gpt-6-luna`（max）、copilot `gpt-5.4`（xhigh）、
  agy `gemini-3.8-flash`（high）。兩條 workflow、driver、validator 全部由它導出。
- codex provider smoke 與 canary builder 的預期 argv 由 `registry.SANDBOX_MODE_DERIVATION`
  導出，不再帶 codex 0.157 起不來的 `--enable use_legacy_landlock`。
- canary 容器改裝由同一契約產生的 model identity overlay（codex builder、agy planner／
  reviewer，`packaged_fallback: deny`），intake 前先驗 roster。
- Trust Root credential adapter 對齊現行 CLI：agy 改收 `antigravity-oauth-token`（由 permgen
  credential row 導出），copilot 改收 `~/.copilot/config.json`；CLI 狀態目錄交由該帳號擁有。
- 新增 disposable probe repository 範本與操作手冊。
- canary driver 在 intake 前以 Manager 身分與其 gh 登入態把 probe repo clone 進已安裝 plan 的
  `repo-source-tree`，設定 checkout 本地 git identity，寫入 `project-cortex.yaml` 的
  exact-project workspace 並以 installed runtime 驗證 Manager 解析得到它，重啟 Monitor 後等
  `load_work_authority()` 出現含 issue link 的 confirmed authority 才進件。
- intake 前預檢 probe repo 設定（push 權限、merge commit、Issues、default branch、PR label、
  approving review 規則）與 image 工具；dispatch 停在 needs_human 時失敗訊息帶出
  `blocking_reason`。
- reference image 以 apt 釘版本提供 gate 用的系統層 pytest 7.4.4 與 policy-check R-22 需要的
  universal-ctags；兩條 workflow 從 paulsha-conventions v1.0.17 的 cp312 runtime bundle 以
  archive／wheel 雙重 sha256 取出 `policy-check` 放進 wheelhouse，部署 venv 與系統層都可用。
