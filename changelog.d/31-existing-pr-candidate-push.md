fix(delivery): 既有 PR 的 remote HEAD 落後 fresh exact Candidate 時，先以 PR context 完成乾淨 preflight，再由 Manager 冪等 push 並重讀授權 feature ref。
