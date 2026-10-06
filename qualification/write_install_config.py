#!/usr/bin/env python3
"""Write the canonical non-secret Ubuntu 24.04 qualification install config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    tools = bundle["toolchain"]
    payload = {
        "schema_version": 1,
        "scheme": "four-way",
        "instance": "cortex",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": bundle["candidate_sha"],
        },
        "operator_account": "root",
        "external_reader_account": "<absent>",
        # #1286：帳號不寫死 uid／gid。plan 依主機 passwd／group 決定：overlay 指定的照用，
        # 已有同名帳號就沿用，否則自動配空號（避開發行版慣用的 990–999）。
        "accounts": {
            "cortex-manager": {"home": "/var/lib/cortex-manager", "shell": "/usr/sbin/nologin"},
            "cortex-reviewer-planner": {"home": "/var/lib/cortex-reviewer-planner", "shell": "/usr/sbin/nologin"},
            "cortex-builder": {"home": "/var/lib/cortex-builder", "shell": "/usr/sbin/nologin"},
            "cortex-gate": {"home": "/var/lib/cortex-gate", "shell": "/usr/sbin/nologin"},
        },
        "service_accounts": {
            "cortex-egress": {"home": "/var/lib/cortex-egress", "shell": "/usr/sbin/nologin"},
        },
        "roots": {
            "deploy": "/opt/cortex",
            "state": "/var/lib/cortex",
            "systemd": "/etc/systemd/system",
            "polkit": "/etc/polkit-1/rules.d",
        },
        "source_repositories": ["paulsha-cortex"],
        "legacy_policy": "quarantine",
        "providers": {
            "builder": ["codex"],
            "reviewer-planner": ["agy", "copilot"],
            "manager": ["github"],
        },
        "toolchain": {
            row["name"]: {
                "version": row["version"],
                "sha256": row["sha256"],
                "shape": row["shape"],
                **({"entrypoint": row["entrypoint"]} if row["shape"] == "tree" else {}),
            }
            for row in tools
        },
    }
    args.output.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
