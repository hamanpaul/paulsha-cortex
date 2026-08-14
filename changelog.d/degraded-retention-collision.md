# degraded-retention-collision

- **`#523`：degraded 保留分支造成 ownership collision，且該 collision 會讓 work model
  refresh 永久失敗**——兩個獨立缺陷疊成一個永不自癒的死鎖：
  - `monitor/lifecycle.py` 的 degraded 保留分支只比對 **work_id**、不比對 **sources**。
    當某個 source 的歸屬從 fallback work item（`correlation.py:_fallback_work_id` 產生的
    `issue:<ref>`）轉移到新宣告的 work item 時，本輪 correlation 已不再產生該 fallback，
    於是它「不在 `projected_ids` 裡」→ 連同**舊的 sources** 被整筆放回 → 兩個 work item
    同時宣稱擁有同一個 source → `work_snapshot.validate_ownership()` raise。
    修法：保留時剝除已被本輪認領的 source；原本有 source 而全數被認領者整筆丟棄
    （內容已完整轉移，留空殼無意義）。原本就沒有 source 的 previous item 維持既有語意。
  - `monitor/work_api.py`：上述例外發生在 `WorkSnapshot.__post_init__`、**早於**
    `replace_durably()`，於是那一輪算出的 provider 新狀態（包含「rate limit backoff
    已結束、provider 恢復 ok」這個事實）**一併被丟棄**。`previous` 因此永遠停在崩潰前
    那一版、`correlation.degraded` 永遠為真，下一輪以相同輸入重演——**provider 無法離開
    degraded，因為記錄它恢復的那次寫入正是拋例外的那次寫入**。修法：projection 是衍生
    資料、provider 觀測是第一手事實，兩者不該同生共死；projection 驗證失敗時降級為
    「保留上一版 projection ＋ 讓新的 provider 觀測落地」，並把失敗原因寫進該輪
    provider 的 `diagnostics` 讓 operator 看得見，而非無聲降級。
- 實測現場：全部 52 個 provider 的 `last_attempt_at` 同時凍在同一時刻（含根本不碰 GitHub
  的 `repo:`／`workflow:` provider），而 `work-items.snapshot.json` 的 mtime 仍每 30 秒更新
  ——外觀完全正常，與單純限流難以區分；唯一線索在 `journalctl -u <instance>-monitor`。
- **成因更正**：先前把它記為「時序競態」（unlink → recover → re-link 即可）。那次受控實驗
  之所以成功，只是因為當下 provider 恰好健康、保留分支根本沒跑。真正的觸發條件是
  `correlation.degraded`——亦即**限流本身**。`#506` 的限流與本缺陷因此互鎖：限流讓
  provider degraded → registry 一有變更就 collision → collision 讓 provider 永遠 degraded。
