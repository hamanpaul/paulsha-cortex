修正 #481 與 #497：`complete_tick` 只終局化仍綁定 slice 的 builder／reviewer；slice 已反映終局的同 job manifest 不因錯誤 gate 重播，recovery 後的舊 terminal job 持續保留稽核且不再改寫 slice 或 evidence。state mutation 未落地時仍允許修復重試。
