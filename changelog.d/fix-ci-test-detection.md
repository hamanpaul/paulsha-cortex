### Fixed
- **CI 測試閘門形同虛設（tests.yml 偵測誤判）**：偵測式 `ls tests/test_*.py tests/*_test.py`
  只要任一 glob 沒配到就回傳非零，本 repo 有 156 個 `test_*.py`、0 個 `*_test.py`，
  因而恆判為「無測試套件」，Setup Python／Install／Run test suite 三步全被 skip，
  job 卻仍回報 success——自 repo 初始 commit 起所有 PR 的 pytest 綠燈都是假的。
  改用 `find -print -quit` 偵測，兩種命名慣例皆可辨識且無測試時仍正確跳過。
