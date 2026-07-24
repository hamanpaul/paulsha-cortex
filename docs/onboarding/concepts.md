# Concepts

這份文件只整理上手階段一定會碰到的四個名詞，定義直接沿用 UX 規格 §9，不額外發明新詞。

## 引用來源

- `docs/superpowers/specs/2026-07-21-porcelain-cli-ux-design.md` §9
- `docs/superpowers/specs/onboarding-docs-spec.md`
- issue #94

## 四個核心名詞

`spec`
: deck 產出的派工單，frontmatter 控制 `dispatch: hold` 或 `dispatch: auto`。

`job`
: 一次 executor 執行；例如 builder 或 reviewer 被派出去跑一次，就是一個 job。

`slice`
: 工作切片，包住 build、verification、review 等 gate 的單位。

`work`
: 跨 PR / issue 的統一生命週期 read model，給人類與 monitor 看整體工作狀態。

## 一句話串起來

從使用者角度，可以把它看成：

`spec` -> `job` -> `slice` -> `work`

- 你先建立 `spec`
- manager 依 `spec` 派出 `job`
- 多個 `job` 與 gate 組成一個 `slice`
- monitor 再把跨來源事實投影成 `work`

## 誰負責寫入

- Manager daemon 是 workflow lifecycle 的唯一 writer
- Monitor 把多來源事實投影成 work read model

這也是為什麼日常 mutation 要走 `cortex run ...`、`cortex recover ...`、`cortex work ...` 之類的命令，而不是直接改內部狀態檔。

## 什麼時候需要知道這些

- Quickstart：知道 `dispatch: hold` 為什麼要改成 `dispatch: auto`
- 排錯：知道自己是在查 request、job、slice 還是 work
- 維運：知道 `cortex status` 與 `cortex list` 看的是不同層次
