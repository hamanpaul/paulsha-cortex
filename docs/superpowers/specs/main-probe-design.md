---
status: accepted
work_item: main-probe-gate
---

# Main probe gate 設計

## Decisions

### D1. Probe result 與失敗資訊同型

以 `MainSyncProbe | MainSyncProbeFailure` 傳回 validator。不得透過 `base_sha_probe` 或 default Git runner 探測，因兩者沒有保留所有 command failure stage/returncode 與明確 timeout 的契約。使用 direct argument-vector subprocess calls，逐階段記錄 Candidate resolve/validation、fetch、`FETCH_HEAD` resolve/validation、merge-base、merge-tree、path parse；每次呼叫均設有限 timeout、捕捉原始 returncode/stdout/stderr，timeout 記 `returncode=None,error_kind=timeout`。Failure 的 `main_head` 在合法 M 尚未 resolve 時為 None；FETCH_HEAD 取得合法 M 後的任何錯誤都必須保留 M。驗證 M/C 是 repository object format 的 full object ID 且可解析為 commit，避免將縮寫或非 commit SHA 傳入 ancestry command。

### D2. 只守住 push gate，不處理 candidate repair

先在 preflight 前 probe；將要 push 再做一次 probe。兩次 probe 均成功且 C 包含各自讀到的最新 M 時，繼續既有 preflight/授權 ship action；專用 local bare-origin regression 必須證明 sync Candidate 可走到實際 local fixture push。若 C 落後（即使 merge-tree clean）、conflict 或 typed failure，則以 needs_human 結束該 tick。測試真跑 bare-origin fetch、merge-base、`merge-tree -z`，並用 NUL-safe parser 保留空白／換行檔名；不得在本票產生 merge commit。

fetch-failure recovery regression 先讓 origin/main fetch 失敗，確認 Manager 形成 `main-sync-unavailable` needs_human stop；再修復 fixture 的 remote/ref 條件，透過正式 workflow operator-resume entrypoint 重新執行 ship validator，斷言第二次 probe 發生且看到修復後 M。Child 04 才負責 nested context durable read-back；不得只呼叫 claim `workflow_starter` 並把 phase 非 define 的 no-op 算成 resume 成功。

### D3. Network isolation

既有 SSH-origin tests 用預設成功 typed stub；bare-origin marker tests 明確略過 stub。所有直接 subprocess commands 的 timeout 有上限且錯誤保留 stage/returncode；failure stub 必須帶 typed failure，不可 None。sync fixture push 只能寫入專用本機 bare origin，不觸碰外網。保留 #610 network guard。
