# Recovery action 契約一致性

新增 13 個 recovery action 家族的版本化清冊，並機械核對 workflow dispatcher、
control contract、coordinator CLI 與 `cortex recover` work/slice 有界 alias 的
註冊名稱。矩陣記錄各 action 的 authority、CAS、派工時點、資源處置、next_actions
及已知 producer/live-runtime 缺口；另補 crash/restart 特性測試、正式入口測試、
candidate CAS help 覆蓋，以及 frozen same-domain reviewer pin 的零派工負控制。
修正 recover-superseded：run status、authority restart gate/source revision 與 audit
reference 現由單一 registry revision CAS 持久化；三個 crash/reload 邊界可重送，
同 run/actor/reason 的 audit hash exact replay 不重寫 registry 或 audit。
更新 R07 與 action 矩陣為已完成，#497/#547/#577 關閉項不再列為外部阻塞。
不變更 recovery schema 或 Trust Root 規則。
