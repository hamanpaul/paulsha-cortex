---
status: accepted
work_item: registry-persist-hygiene
---

# Registry persistence design

## Decisions

1. **One production module**：主要產品 scope 僅 `coordinator/registry.py`；必要 wrapper 修改屬 tests，不灌入 production domain。domain_breadth=0，state_consistency=2：disk/memory/digest/history/rollback 的一致性與 crash 邊界必須共同維持。
2. **One canonical serialization**：`json.dumps(ensure_ascii=False, indent=2, sort_keys=True)` 的 UTF-8 bytes 為 digest 與落盤共同來源。`_persist` 可傳 optional serialized 給保留 dict positional 的 writer；缺 serialized 的 v1 caller 使用同一格式。相同 bytes 且 state 存在才 no-op，metadata 保持不動。
3. **Digest lifecycle**：constructor 初始化未知 digest；load 完成 schema/內容驗證且尚未 normalization-persist 前用 original bytes 建 baseline。Writer 只有 replace 及必要 durability steps 成功才更新 digest；fault 依既有 rollback/reload 恢復正確值。非 canonical 格式 load 可能正規化一次，不能用 memory snapshot 誤當磁碟內容。
4. **History configuration**：constructor 明示 history_limit 優先於 env，env 優先 default 500；constructor bool/非整數、env 非正十進位整數及 ≤0 均拒絕。Counter 僅允許三種 history key 的非負整數，bool 非合法 integer；缺欄位/缺 key 按零，未知格式拒絕而不靜默重設。
5. **Shared trim helper**：record_action 真正 append 後 trim；load 在新 row/list/counter 上 trim，保留原 payload 比較。limit=1 最新；其他首筆+最新N。Current refs 與 completion 引用資料不依 history 截斷消失，counter 僅說明丟棄量、不冒稱全歷史可重播。
6. **Copy invariants**：_copy_slice 與 loader 為 nested history_truncated 建獨立 dict；沿用 list item copy 契約。No-op 判定、normalization-once 與其他 additive row 欄位相容，不加 payload root keys。
7. **Hardlink rollback**：backup 使用同目錄唯一名稱，不能直接對仍存在的 mkstemp placeholder 做 os.link；安全釋放佔位並建立 link，保留必要 fd/path 清理與 error propagation。任何 link OSError 回既有 copy+fsync，無法安全備份則不 replace。Replace 後 fsync 故障恢復舊 inode/bytes，rollback 失敗仍報真正 durable fault，不吞掉。
8. **Conservative cleanup admission**：age+pattern 僅候選條件。刪除前必須有可信的 inactive writer 身分證據，或由既有 owner authority 證明此 instance state 目錄獨占；無法取得時預設 skip+具體診斷。不得新增「呼叫者傳 true 就當獨占」的旁路，也不在本票建立 #818 的跨 process lock。舊 random tmp 缺 owner metadata 時保持 unknown；這是明示安全 residual，不宣稱 cleanup rate 已改善。
9. **One-level non-following sweep**：construction 一次、regular files only、no symlink follow、grace 不低於30；匹配限定 tmp*.tmp/tmp*.backup.tmp/tmp*.rollback.bak。掃描錯誤只影響 cleanup 診斷，不能改 _load 對 state 無權/壞檔的拒收結果。重啟再次掃同一已處置檔安全 no-op。
10. **Archive boundary**：保留首/近期與計數不是完整 archive。需要完整歷史的部署在 root/operator 另行保存已驗證原 registry 備份前不得套用 destructive truncation；不在本票添加 archive 引擎。主流程對 #829 記錄 missing full-retention 能力，不因 H04 綠燈消除此 residual。

## Verification

新 `tests/test_coordinator_registry_persist_hygiene.py` 使用隔離 state、fake clock、monkeypatch writer/link/fsync/fault、可計數 tmp 建立；沒有 live process 或模型 probe。Sweep 的正例必須附可驗 exclusive/terminated-owner fixture，負例含老檔但活躍 writer；mtime-old alone 絕不充分。明示「production owner proof 缺席會 skip」與 unit positive proof 的差別。

H01–H11 完整映射原 todo T01–T11；documentation/CLI/實際 sizing 在新增 T12–T13。如果要分解，report 的 digest、bounded-history、rollback/sweep 三候選各須具獨立 acceptance；parent 不因任何一件完成而關閉。

## Compatibility and Risks

不動 schema version/root key/mtime-size reload策略，不宣稱多 manager stale snapshot 已解決；#818 仍是共享寫入依賴。Hardlink 不支援走 copy，因此某 filesystem 不一定取得相同效能。History 限額減單次檔案大小，#496 決定寫入頻率；digest 僅減真 no-op，不能將其測試冒稱 E1 現場頻率改善。
