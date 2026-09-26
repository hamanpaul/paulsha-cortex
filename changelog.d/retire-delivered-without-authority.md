#1093：WorkAuthority 缺席時，`retire-delivered` 可依 exact registry run 與 GitHub PR terminal proof 退休已交付孤兒 run；保留 actor/reason、exact CAS、稽核與 replay，並維持 `abandon` 的原有嚴格門檻。Status/work list 只顯示正式入口當下可接受的 action，monitor 對不存在的 workspace 顯示診斷。

對抗審查修正：`load_work_authority` 改以結構化例外型別 `WorkAuthorityConfirmedAbsent` 區分「snapshot 健康、非限流、非 ambiguous 下確定沒有該 (repo, work_id)」與「歧義／衝突／limits／provider 不健康」，`work_actions._is_work_authority_absence` 改用 `isinstance` 型別檢查取代錯誤訊息字串比對；重複 `(repo, work_id)`、跨 work 的 issue owner 衝突、canonical provider degraded／限流（即使該 repo 在 snapshot 中完全沒有列出任何 work item）皆維持 fail-closed，不再被誤判成可放行 registry-only 退休。

第二輪對抗審查修正（各兩條 MAJOR）：
- `claim.load_work_authority` 的 canonical provider 最後健康檢查改用 `in` 判斷「該 provider 條目是否存在」，條目存在但格式不正確（`null`／list／字串等非 mapping）時一律 fail-closed（`AuthorityValidationError`），不再被 `isinstance(..., dict)` 悄悄跳過而落到確定缺席、放行 registry-only 退休。
- 驗證 authority 缺席路徑新寫的 `cortex-work-retire-delivered/v2` evidence 對回退相容性：新增測試把 `origin/main`（本票之前）的 `work_actions.py` 載入為獨立 module，直接呼叫其 `_superseded_retire_delivered_body`——確認舊 reader 對本 PR 未改動的 v1 evidence 仍能正常讀回，對新增的 v2 evidence 則是乾淨的 `RuntimeError`（safe fail-closed），不是未攔截例外或資料誤讀；現有程式碼本身已具備此性質，此輪補上可重驗的回歸測試。

第三輪對抗審查修正（BLOCKER＋MAJOR）：
- `work_authority_projection_state()` 補上 `snapshot_path` 參數（預設沿用 canonical durable snapshot，行為不變）：先前完全沒有這個參數，對抗審查建的畸形 snapshot fixture 永遠寫在測試自己的 tmp 路徑，測試斷言實際驅動的是（可能不存在，或執行機器上真正的）canonical snapshot，斷言恆真、驗不到回歸；`tests/test_work_actions.py` 的相依斷言與新增的 `provider-timestamp-unparseable` 情境改用該參數，直接以本次建的畸形 snapshot 驅動。
- `claim.load_work_authority` 對「目標 (repo, work_id) 完全沒有列在 work_items」的 canonical provider 最後健康檢查，補上時間可解析性驗證：抽出共用的 `_validate_canonical_provider_timestamp`（沿用 `_authority_from_canonical_row` 既有的 ISO8601 解析規則，不另寫一套），`_authority_from_canonical_row` 與 `load_work_authority` 兩處共用同一個判斷。先前只檢查 `last_success_at` 是非空字串就視為健康，像 `"not-a-timestamp"` 這種無法解析的畸形值會被誤判為健康，讓 canonical provider 誤判成「根本不存在」而放行 `WorkAuthorityConfirmedAbsent`（registry-only 退休）；現在無法解析一律 `AuthorityValidationError`，fail-closed。
