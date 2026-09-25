---
status: accepted
work_item: main-probe-gate
domain_breadth: 0
state_consistency: 2
acceptance_surfaces: 2
spec_stability: 0
orchestration: 2
total_score: 6
sizing: yellow
---

# Main probe gate 設計

## Decisions

### D1. Probe result 與失敗資訊同型

以 `MainSyncProbe | MainSyncProbeFailure` 傳回 validator。不得透過 `base_sha_probe` 或 default Git runner 探測，因兩者沒有保留所有 command failure stage/returncode 與明確 timeout 的契約。使用 direct argument-vector subprocess calls，逐階段記錄 Candidate resolve/validation、fetch、`FETCH_HEAD` resolve/validation、merge-base、merge-tree、path parse；每次呼叫均設有限 timeout、捕捉原始 returncode/stdout/stderr，timeout 記 `returncode=None,error_kind=timeout`。Failure 的 `main_head` 在合法 M 尚未 resolve 時為 None；FETCH_HEAD 取得合法 M 後的任何錯誤都必須保留 M。此處的 `None` 是 Python 物件值；寫入 D2 的 JSON evidence 時序列化為 `null`。驗證 M/C 是 repository object format 的 full object ID 且可解析為 commit，避免將縮寫或非 commit SHA 傳入 ancestry command。

### D2. Durable delivery evidence preserves the typed failure

經 production Manager needs_human wrapper 的每個 probe failure 都透過既有 `work_bridge._write_json_evidence` 寫入 content-addressed JSON envelope，並把 ref/hash 隨 stop 結果提供。Payload 結構化保留 Candidate 與 failure 的 `stage`、`returncode`、`error_kind`、`main_head`；不可只存 exception/detail prose。測試要從實際 stop 結果取得 ref、重新開啟 persisted file、驗證 envelope hash，再逐欄比對 payload，而非檢查 writer 輸入或 transient `MainSyncProbeFailure`。

裸 origin fetch failure 的 read-back 必須證明 `stage=fetch`、`returncode` 與失敗 fetch 子程序實際非零值相同、`error_kind` 相符且 `main_head=null`。在已成功取得合法 M 後注入 merge-base、merge-tree 或 NUL path parser failure，read-back 必須保留相同精確 M、正確 stage/returncode/error_kind；timeout 仍為 `returncode=null`。此 delivery evidence 與 child 04 的 WorkflowRun `needs_human_reason.context.main_sync` 是兩份不同持久化證據，不能以後者代替前者。

### D3. Push gate，不處理 candidate repair

先在 preflight 前 probe；將要 push 再做一次 probe。兩次 probe 均成功且 C 包含各自讀到的最新 M 時，繼續既有 preflight/授權 ship action；專用 local bare-origin regression 必須證明 sync Candidate 可走到實際 local fixture push。若 C 落後（即使 merge-tree clean）、conflict 或 typed failure，則以 needs_human 結束該 tick。測試真跑 bare-origin fetch、merge-base、`merge-tree -z`，並用 NUL-safe parser 保留空白／換行檔名；不得在本票產生 merge commit。

fetch-failure recovery regression 先讓 origin/main fetch 失敗，經真正 Manager wrapper 形成 `main-sync-unavailable` needs_human stop，從該 stop 的 evidence ref 讀回第一次 fetch failure，再修復 fixture remote/ref，透過正式 workflow operator-resume entrypoint 重新執行 ship validator，斷言第二次 probe 發生且取得修復後新 M。Child 04 才負責 nested context durable read-back；不得只呼叫 claim `workflow_starter` 並把 phase 非 define 的 no-op 算成 resume 成功。

### D4. Network isolation

既有 SSH-origin tests 用預設成功 typed stub；bare-origin marker tests 明確略過 stub。所有直接 subprocess commands 的 timeout 有上限且錯誤保留 stage/returncode；failure stub 必須帶 typed failure，不可 None。sync fixture push 只能寫入專用本機 bare origin，不觸碰外網。保留 `tests/network_guard.py` 對應 #610 的 egress guard。


### D5. Sizing (#208)

依 `fix-standard` 計分：production scope 只有 `work_bridge.py`（domain breadth 0）；main 可能在兩次 probe 間前進（state consistency 2）；兩個 gate_spine 加 R-09/R-16/R-19 得 acceptance surfaces 2；三件 accepted artifacts 無 blocker 得 spec stability 0；9 張 workflow cards 且 9 張皆有 persona binding 得 orchestration 2。總分 `0+2+2+0+2=6`，Yellow。`invariant_count: 7` 對應 live #987 的七項驗收條件。
