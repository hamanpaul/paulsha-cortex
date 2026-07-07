from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

from . import handoff
from .guardrail import PersonaGuardrail
from .loader import load_catalog


def compute_changed_paths(base: str, head: str, repo: str | Path | None = None) -> list[str]:
    """git diff --name-only base...head。非零 returncode → fail-closed（RuntimeError）。"""
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    # core.quotepath=false：避免非 ASCII 路徑（zh-tw repo 常見）被
    # 引號包裹 + 八進位跳脫，導致 _normalize_path 誤判而出現 false violation。
    cmd += ["-c", "core.quotepath=false", "diff", "--name-only", f"{base}...{head}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git diff 失敗（fail-closed）: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def evaluate_diff(
    role: str,
    changed_paths: Sequence[str],
    catalog: Mapping[str, object] | None = None,
) -> list[dict[str, str]]:
    """逐檔 evaluate_filesystem，回傳越界清單 [{path, reason}]。"""
    rail = PersonaGuardrail(catalog) if catalog is not None else PersonaGuardrail()
    violations: list[dict[str, str]] = []
    for path in changed_paths:
        decision = rail.evaluate_filesystem(role=role, path=path)
        if not decision.allowed:
            violations.append({"path": path, "reason": decision.reason})
    return violations


def load_manifest_ok(
    role: str,
    manifest_path: str | Path,
    catalog: Mapping[str, object] | None = None,
) -> bool:
    """讀驗 handoff manifest；任何 fail-closed 例外 → False（不放行）。"""
    try:
        handoff.read_manifest(manifest_path, catalog)
    except (FileNotFoundError, ValueError):
        return False
    return True


def build_verdict(
    *,
    role: str,
    changed_paths: Sequence[str],
    manifest_ok: bool,
    catalog: Mapping[str, object] | None = None,
) -> dict[str, object]:
    violations = evaluate_diff(role, changed_paths, catalog)
    ok = (not violations) and manifest_ok
    return {
        "role": role,
        "changed_paths": list(changed_paths),
        "violations": violations,
        "handoff_ok": manifest_ok,
        "ok": ok,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m paulsha_cortex.persona.gate")
    parser.add_argument("--role", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--repo", default=None)
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="enforce 模式：ok 為 false 時 exit 1（預設 shadow 恆 exit 0）",
    )
    args = parser.parse_args(argv)

    catalog = load_catalog()
    try:
        changed_paths = compute_changed_paths(args.base, args.head, repo=args.repo)
        diff_error: str | None = None
    except RuntimeError as exc:  # fail-closed：無法取 diff 視為不可驗證
        changed_paths = []
        diff_error = str(exc)

    manifest_ok = load_manifest_ok(args.role, args.manifest, catalog)
    verdict = build_verdict(
        role=args.role,
        changed_paths=changed_paths,
        manifest_ok=manifest_ok,
        catalog=catalog,
    )
    verdict["mode"] = "enforce" if args.enforce else "shadow"
    if diff_error is not None:
        verdict["diff_error"] = diff_error
        verdict["ok"] = False

    print(json.dumps(verdict, ensure_ascii=False))

    if not args.enforce:
        return 0  # shadow：恆放行，僅觀測/記錄
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
