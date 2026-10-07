# #1340 qualification lifecycle 測試固定時鐘

qualification lifecycle 測試改用注入的固定時鐘，並以固定時間驗證 `reviewed_at` 邊界，避免 operator approve／revoke ACL separation 測試偶發 future timestamp 失敗。
