### Fixed

- runbook 的「M2′ 之後仍未涵蓋的（不得順手宣稱）」清單有兩條長期陳舊，已更正並保留
  更正紀錄（#696）：
  - **gate 執行身分**寫「需要第四個帳號，屬 #629」——#629 早已 CLOSED，`cortex-gate`
    帳號與 `cortex-gate-job@`／`-jit` 兩份模板 unit 均已落地。
  - **reviewer 憑證無法就地 refresh** 寫「該檔不在 reviewer 模板 unit 的
    `ReadWritePaths=` 內」——#685（#672 票 D）已把三份登入態改走 `HOME_REDIRECT_TREE`，
    目標落在早已於 RWP 內的 `cache`，unit 的 RWP 逐字不變而憑證可寫。0818 實機於完整
    加固面下實測 `touch -c "$HOME/.codex/auth.json"` 通過。
- 同時加上一句約束：**not-covered 清單本身也是一種宣稱**，修好了要當場改掉，
  否則它會反向說謊——把已完成的工作讀成未完成。
