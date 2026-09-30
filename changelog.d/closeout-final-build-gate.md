### Fixed

- **#716 canary 結案只要求最後一個 build 的 gate passed**：driver 原本要求每一個 build ledger 的 pytest 都必須 passed。但 worktree-isolation 本來就不跑 gate，tdd-red 是 red-required，pytest 依設計就會 failed。現在只要求最後一個 build job（產出交付 candidate 的那一個；有修正回合時就是 repair builder）的部署宣告 gate 全部 passed；其他 build ledger 只驗格式與綁定（#716）。
