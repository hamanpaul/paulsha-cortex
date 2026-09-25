# planning-kind-bound-exact-match

- **#812 planning 產出目的地綁定改為精確 stem**：`planning_kind_bound`、planning publication 與 authority 重驗現在一致只接受 canonical／dated／anchor slug 的 docs 目的地，關閉 prefix／suffix／middle／`-v2` 這類 substring 誤放行，同時保留 change slug ≠ work_id 的合法 planning anchor。combo manifest 的 `*<task-slug>*` outputs pattern 仍供 `openspec/changes/<change>/...` 使用，但不再單獨放行 `docs/superpowers/{specs,plans}`；本次也同步更正 #802 對 DiagnosticReason v2 降級風險與 kind-bound 文法的文字敘述。
