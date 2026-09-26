# Deployment canary probe

Cortex deployment canary 的一次性 private 目標 repo。canary 會對本 repo 走完
intake → build → verify → review → ship，真的開 PR 並 merge；每次 canary 都要從未修改的
範本建立新的 repo，不重複使用。

## Install

不需安裝；只用 Python 3 標準函式庫。

## Usage

```sh
python3 -m compileall -q src
python3 -m unittest discover -s tests -q
```

`src/canary_probe.py` 的 `normalize_label()` 去除標籤前後空白；canary 任務要求全空白標籤
改回傳 `"unnamed"`。

## Version

見 `VERSION`。
