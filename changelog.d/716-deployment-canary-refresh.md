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
