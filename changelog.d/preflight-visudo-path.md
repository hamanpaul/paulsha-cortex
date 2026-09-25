---
type: fix
scope: tests
---
sudoers 整合測試將系統 sbin 加入個別測試的 PATH，避免 service PATH 差異造成 preflight 誤判。
