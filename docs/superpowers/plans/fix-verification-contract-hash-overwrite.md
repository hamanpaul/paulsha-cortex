---
status: accepted
work_item: fix-verification-contract-hash-overwrite
---

# fix-verification-contract-hash-overwrite Todo

`#501`：**verification 一旦跑出任何結果，slice 的「pinned contract 身分」就被證據
身分覆寫掉**——`slice_row["verification"]["hash"]` 這個欄位同時被兩種語意寫入，
下一 tick 必然自舉出 `pinned-input-mismatch: verification-hash`，即使 spec／plan／
verification contract 一個 byte 都沒動。

## 現況查核（0816，對 main `48b0205`）

**缺陷仍完整成立，未被 R0／R0.5 任何一批動到。** 三個座標互相咬合：

1. `paulsha_cortex/coordinator/manager.py:404-409` —— `_apply_verification_result()`
   把**證據 payload 的 canonical hash** 當 `verification_hash` 餵進 `update_slice()`：
   ```python
   registry.update_slice(
       slice_id,
       verification_hash=evidence["hash"],   # ← 這是 evidence hash，不是 contract hash
       current_evidence_refs=refs,
       candidate=payload["candidate"],
   )
   ```
2. `paulsha_cortex/coordinator/registry.py:1305-1306` —— `update_slice()` 把它直接
   寫進 `slice_row["verification"]["hash"]`。
3. `paulsha_cortex/coordinator/manager.py:278-281` —— `_pinned_input_mismatches()`
   把**同一欄位**當成不可變的 pinned contract hash，拿去和
   `verification.canonical_json_hash(current_meta["verification"])` 比對，不等就
   `mismatches.append("verification-hash")`。

也就是說：欄位的**唯一合法寫入者**應該只有派工當下的 pin
（`registry.repin_slice()`，`registry.py:1172`／`create_slice()` `registry.py:1149-1152`，
由 `autonomy._pin_slice_inputs()` `autonomy.py:742,760` 呼叫），
而 `update_slice(verification_hash=...)` 這條路徑**全 repo 只有 `manager.py:406`
一個呼叫點**（已全庫確認），正是缺陷本身。這讓修復邊界異常乾淨。

補充現況：`manager.py:1158` 的 `verification_hash=slice_row["verification"]["hash"]`
是寫進 **reviewer job row** 的欄位（`create_job`），不是 slice contract 欄位，
語意正確，**不要動**。

### 為什麼平常看不到

只有在 `_apply_verification_result()` 之後**沒有**成功綁上 reviewer job 時才會爆——
綁上時 `complete_tick` 的 builder 完成分支會被 `manager.py:1886-1887`
（`slice_row.get("reviewer_job_id")` → `continue`）跳過，缺陷被遮住。
`#501` 現場是 foreign-review dispatch 因 tier config-error launch 失敗
（`manager.py:2064-2082` 的 `_launch_foreign_review` 未 `launched`），遮罩消失，
下一 tick 立刻自舉 mismatch。

### 活現場

現有 slice `add-cortex-version-flag-build` 正卡在這個 `pinned-input-mismatch`，
是可直接觀察的活體：`slice.verification.hash` 已是證據 hash，spec frontmatter 的
contract hash 未變。修完後這個 slice 應該不必改任何 spec／plan bytes 就能脫困。

## Scope（明確邊界）

**本 work item 的主體是「欄位語意分離」，不是「pinned-input 檢查邏輯重寫」，
也不是「terminal job 重播治理」。**

- 要做：讓 contract 身分與 execution evidence 身分成為**兩個不同欄位**，
  且證據側寫入**永不觸碰** contract 側。
- 要做：`update_slice()` 不再提供「用證據 hash 覆寫 contract hash」的能力
  （移除該參數，或改名為明確的 evidence 欄位；`create_slice`／`repin_slice`
  的 `verification_hash` 參數維持原樣，那是合法的 pin 入口）。
- 要做：證據 hash 改存進與 current evidence ref 同層的獨立欄位
  （建議 `current_verification_evidence_hash`），讓 `manager.py:2096`
  `_current_verification_ref()` 一類讀取端仍取得到。
- **不要做**：改 `_pinned_input_mismatches()` 的比對規則、放寬 fail-closed、
  或為了讓現場過關而略過 `verification-hash` 檢查——那是把體溫計摔了。
- **不要做**：處理 `#497` 的 superseded terminal job 重播，或 `#496` 的
  dirty recheck 冪等。那兩張是獨立 work item；本張只負責「就算被重播，
  contract 身分也不該被寫壞」。
- **不要做**：改動 `manager.py:1158` 的 reviewer job `verification_hash` 欄位、
  或 `_review_inputs_drifted()`（`manager.py:967-973`）的比對語意——它們讀的是
  正確的 contract 值，本修復只是讓那個值真的維持正確。
- 資料相容：既有 `jobs.json` 內已被寫壞的 slice row 需要有可判定的處理方式
  （migration 或 repin 路徑），不得讓升級後既有 instance 直接 crash。

## Tasks

- [ ] **欄位分離**：`slice_row["verification"]["hash"]` 收斂為唯一語意＝pinned
      verification contract hash；新增獨立欄位承載 current verification evidence
      hash，兩者在 registry schema 層各有明確歸屬
- [ ] **切斷覆寫路徑**：`_apply_verification_result()`（`manager.py:386-409`）不再
      經 `update_slice(verification_hash=...)` 寫 contract 欄位；`update_slice()`
      移除／改名該參數，使「證據覆寫 contract」在型別層就不可表達
- [ ] **讀取端對齊**：確認 `_current_verification_ref()`、completion record
      （`manager.py:833`／`843`）、handoff manifest（`manager.py:2142-2146`）三處
      各自讀到語意正確的欄位——completion record 的 `verification_hash` 應為
      contract hash，`verification_evidence_hash` 應為證據 hash，不得互串
- [ ] **既有壞資料**：對 `verification.hash` 已被寫成證據 hash 的既有 slice row
      提供可判定的復原路徑（migration 或明確的 operator 指示），並確保
      `add-cortex-version-flag-build` 這類活現場可脫困
- [ ] **測試**：
      - contract hash 在 `_apply_verification_result()` 前後**完全相同**，
        而 evidence hash 可獨立取回（`#501` 建議驗收第 5 條）
      - verification pass → foreign-review launch 失敗（config-error）→ 下一 tick
        **不得**回報 `pinned-input-mismatch`，且原成功證據不被替換
      - 失敗／狀態證據（`_write_status_evidence` 路徑，`manager.py:1949-1961`）
        同樣不得污染 contract hash——`#501` 0812 留言確認失敗證據也會寫壞
      - repin（`retry-build` 走 `registry.repin_slice`）仍能合法更新 contract hash

## 現場紀錄（供實作者參考）

- issue `#501` 首則：Task 4 candidate `77e13f2` 的完整量測，contract hash
  `f30e5bfe…` vs 寫入值 `98b73f70…`（證據 payload hash）
- issue `#501` 0812-18:05 留言：candidate `6421bc98…` 的復發，寫入值
  `c64340c4…` 恰為 **pinned-input-mismatch 證據 payload** 的 canonical hash——
  證明失敗證據也走同一條污染路徑；每約 4 秒一 tick、隔離前累積 18+ 份 quarantine
- 本張與 `#496`／`#497` 同屬「桶C slice 迴圈家族」，三者都經 `_apply_verification_result()`
  放大。**本張建議最先派工**：它是三張裡邊界最窄、單一呼叫點、可完全由單元測試
  判定的一張，另外兩張的驗收都會踩到它
