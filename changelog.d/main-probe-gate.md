---
type: feat
scope: coordinator
---
#987 main probe gate：ship validator 現在會在 Manager-owned ship clone 內以 bounded direct git probe `origin/main`，只允許 exact Candidate 已含最新 M 的交付繼續進 preflight／push／PR；clean-behind、conflict 與 fetch／merge-base／merge-tree／path-parser failure 會 fail-closed，並把 `candidate`／`stage`／`returncode`／`error_kind`／`main_head` 寫成 content-addressed `main-sync-probe` evidence，供 `main-sync-unavailable` stop 與 operator resume 讀回。重用既有 Manager ship workspace 時，若來源 repo 已無有效 `origin`，會先清掉工作區殘留的 `origin`，避免 stale remote 被錯誤當成最新 main。
