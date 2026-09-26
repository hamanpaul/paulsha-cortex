#!/usr/bin/env python3
"""Release qualification／deployment canary 的 toolchain 與模型單一真相。

兩條 workflow（`--github-env`）、容器內的 driver／validator，以及 canary 的 model
identity overlay（`--model-identity-overlay`）都由本檔導出；其他地方不得再寫第二份
版本、integrity 或模型字面值。本檔刻意只用標準函式庫：GitHub runner 與 host 端的
`validate.py` 執行時沒有安裝 `paulsha_cortex`。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping


TOOLCHAIN: Mapping[str, Mapping[str, str]] = {
    "codex": {
        "version": "0.157.1",
        "npm_package": "@openai/codex@0.157.1-linux-x64",
        "integrity": "sha512-Eac8XlC0nCXSeUjDU9l8yLJ6P9evv1mO+AnvILoNwlegBC7B3AVXqJ05QcMhQX7RcJ3Lk2618ykCb2X2ui8VAQ==",
        "archive_path": "package/vendor/x86_64-unknown-linux-musl/bin/codex",
    },
    "claude": {
        "version": "2.1.239",
        "npm_package": "@anthropic-ai/claude-code-linux-x64@2.1.239",
        "integrity": "sha512-glj5zramK4ebmNa0EVx1iAtBfWHKnZ1q0Ti+1veMR4GHR9ttSranQoMIsLIuJUdRX/hErJnArjwe9hanuxE+dg==",
        "archive_path": "package/claude",
    },
    "copilot": {
        "version": "1.0.88",
        "npm_package": "@github/copilot-linux-x64@1.0.88",
        "integrity": "sha512-wNigl8rqixvtYoRtygNf1NQIXx77MWC6XyqoV7fcfvSwWI3GJCltnczaNLbC611K9Hex39KyiUR5ZmGhQ+LzLw==",
        "archive_path": "package/copilot",
    },
    "agy": {
        "version": "1.2.11",
        "url": "https://github.com/google-antigravity/antigravity-cli/releases/download/1.2.11/agy_cli_linux_x64.tar.gz",
        "sha256": "c91c62c5e6fa954f5a7e1d7b9ad417d749db4aa60a4ba0b3d604dec1b645d190",
        "archive_path": "antigravity",
    },
    "srt": {
        "version": "0.0.73",
        "npm_package": "@anthropic-ai/sandbox-runtime@0.0.73",
        "integrity": "sha512-F608iUirrCqwvInZYGRRgJWDQj0tt6fNVE9aPagpotLJ5LhC4JbrMFIIZww5MFjb+HRCkpE0+xdI79c30tdVYg==",
        "entrypoint": "node_modules/@anthropic-ai/sandbox-runtime/dist/cli.js",
    },
    "openspec": {
        "version": "1.10.0",
        "npm_package": "@fission-ai/openspec@1.10.0",
        "integrity": "sha512-fuL3Rz7Jv+NnHeUM1XkbaXFo4bUdPttOWOC66/6SyfJr9rPOvGE47oBp+8XdDtPiiWZawa0Z9RDzGasetFu2eQ==",
        "entrypoint": "node_modules/@fission-ai/openspec/bin/openspec.js",
    },
}

#: provider smoke 要求的 (model, effort) 與執行帳號；operator 2026-09-26 實測可用。
#: codex 的 effort 另由 production `launcher._codex_default_effort()` 對同一模型發出，
#: 兩者一致由測試釘住（此檔不能 import `paulsha_cortex`）。
PROVIDERS: Mapping[str, Mapping[str, str]] = {
    "agy": {"model_id": "gemini-3.8-flash", "effort": "high", "account": "cortex-reviewer-planner"},
    "copilot": {"model_id": "gpt-5.4", "effort": "xhigh", "account": "cortex-reviewer-planner"},
    "codex": {"model_id": "gpt-6-luna", "effort": "max", "account": "cortex-builder"},
}

#: canary 派工的 builder：`--builder-executor codex --builder-model gpt-6-luna`。
CANARY_BUILDER: Mapping[str, object] = {
    "provider": "codex",
    "independence_domain": "openai",
    "capabilities": ("build",),
}
#: canary 的 planner／reviewer：必須與 builder 不同 independence domain，且容器內
#: `cortex-reviewer-planner` 有它的憑證。copilot 不行：packaged roster 的 gpt-5.4
#: 屬 openai domain，且 hardened 模式下 copilot 沒有 planner／reviewer toolchain grant。
#: AGY identity id 以 `<model>-<effort>` 表達 effort（launcher 原樣發 `--model <id>`）；
#: planning 能力依 roster 規則必須帶 google domain 與 `agy-plan-sandbox` live probe。
CANARY_REVIEWER: Mapping[str, object] = {
    "provider": "agy",
    "independence_domain": "google",
    "capabilities": ("planning", "review"),
    "live_probe": "agy-plan-sandbox",
}


def canary_identity(role: Mapping[str, object]) -> tuple[str, str]:
    """回傳 canary 身分的 `(executor, model_id)`，由 PROVIDERS 導出。"""

    provider = str(role["provider"])
    row = PROVIDERS[provider]
    if provider == "agy":
        return provider, f"{row['model_id']}-{row['effort']}"
    return provider, row["model_id"]


def model_identity_overlay() -> dict[str, object]:
    """canary 容器的 host overlay：只列 canary 用得到、且有憑證的兩個身分。

    `packaged_fallback: deny` 讓解析不會落到 packaged roster 裡容器沒有憑證的身分
    （claude、codex spark、agy 3.1-pro）。
    """

    rows: list[dict[str, object]] = []
    for role in (CANARY_BUILDER, CANARY_REVIEWER):
        executor, model_id = canary_identity(role)
        row: dict[str, object] = {
            "executor": executor,
            "model_id": model_id,
            "independence_domain": role["independence_domain"],
            "capabilities": list(role["capabilities"]),  # type: ignore[arg-type]
        }
        if "live_probe" in role:
            row["live_probe"] = role["live_probe"]
        rows.append(row)
    return {
        "schema_version": 3,
        "resolution_policy": {"packaged_fallback": "deny"},
        "identities": rows,
    }


def render_model_identity_overlay() -> str:
    """以 block YAML 輸出 overlay（純量以 JSON 字面值表示，仍是合法 YAML）。"""

    payload = model_identity_overlay()
    policy = payload["resolution_policy"]
    assert isinstance(policy, Mapping)
    lines = [
        f"schema_version: {payload['schema_version']}",
        "resolution_policy:",
        f"  packaged_fallback: {json.dumps(policy['packaged_fallback'])}",
        "identities:",
    ]
    rows = payload["identities"]
    assert isinstance(rows, list)
    for row in rows:
        lines.append(f"  - executor: {json.dumps(row['executor'])}")
        for key in ("model_id", "independence_domain"):
            lines.append(f"    {key}: {json.dumps(row[key])}")
        lines.append(
            "    capabilities: ["
            + ", ".join(json.dumps(value) for value in row["capabilities"])
            + "]"
        )
        if "live_probe" in row:
            lines.append(f"    live_probe: {json.dumps(row['live_probe'])}")
    return "\n".join(lines) + "\n"


def github_environment() -> dict[str, str]:
    """把釘選的 toolchain 攤平成 GitHub Actions 環境變數（`TOOL_<NAME>_<FIELD>`）。"""

    values: dict[str, str] = {}
    for name, row in TOOLCHAIN.items():
        prefix = f"TOOL_{name.upper()}_"
        for field, value in row.items():
            values[prefix + field.upper()] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--github-env", type=Path)
    target.add_argument("--model-identity-overlay", type=Path)
    args = parser.parse_args()
    if args.github_env is not None:
        with args.github_env.open("a", encoding="utf-8") as stream:
            for name, value in github_environment().items():
                if "\n" in name or "\n" in value:
                    raise ValueError("qualification contract values must be single-line")
                stream.write(f"{name}={value}\n")
        return 0
    descriptor = os.open(
        args.model_identity_overlay,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(render_model_identity_overlay())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
