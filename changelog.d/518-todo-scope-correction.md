# 518-todo-scope-correction

- **`#518` workstream todo scope 修正**——初版要求「installer 讓 `PSC_PROJECT_CONFIG_ROOT`
  隨 `PSC_AGENTS_ROOT` instance-scope」，但該派生現行 `deploy/installer.py:280-307` 已具備
  （adversarial review 查核）。scope 收斂為實際缺口：**legacy instance env 遷移**（原子＋
  可回滾，對應 doctor `managed-path-drift` 徵兆）、**workspace 語意裁決**（exact-project
  宣告，禁止猜 parent 目錄重掃 sibling repo）、**共用 config root 的 doctor 告警**。
  前一世代 run（authority 為錯誤前提的初版 todo）已依 v4 計畫結算，本修正即「於
  supersede/reclaim 邊界修正 todo」的執行。
