修復 #1084：Architecture HTML workflow 只對 Chrome CDP `Target.getTargets: timed out after 15000ms` 暫時逾時最多重試兩次，逐次保存 visual-check 輸出與執行診斷；其他錯誤立即失敗，持續逾時仍 fail closed。
