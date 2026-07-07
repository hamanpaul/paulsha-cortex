from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

from . import gate, handoff
from .loader import load_catalog

HANDOFF_GLOB = "runtime/handoff/*.json"


def resolve_base(env: Mapping[str, str]) -> str:
    """PR base ref：origin/<GITHUB_BASE_REF>（缺省 main）。

    對齊 actions/checkout@v4 + fetch-depth:0 後 base 分支以 remote-tracking
    ref（origin/<branch>）存在的事實。
    """
    base_ref = env.get("GITHUB_BASE_REF") or "main"
    return f"origin/{base_ref}"


def resolve_head(env: Mapping[str, str]) -> str:
    """PR head：GITHUB_SHA（缺省 HEAD）。"""
    return env.get("GITHUB_SHA") or "HEAD"


def find_latest_manifest(repo_root: str | Path | None = None) -> Path | None:
    """找 runtime/handoff/*.json 中 mtime 最新者；無則 None（不報錯）。"""
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    candidates = [Path(p) for p in glob.glob(str(root / HANDOFF_GLOB))]
    files = [p for p in candidates if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Shadow persona-scope CI runner：恆 return 0（observe/annotate-only）。

    無 manifest（常態）→ 印 skipped 通知並放行。
    有 manifest → reuse gate verdict 邏輯、印 JSON，shadow 仍恆放行。
    """
    env = os.environ if env is None else env

    parser = argparse.ArgumentParser(prog="python -m paulsha_cortex.persona.scope_ci")
    parser.add_argument("--repo", default=None, help="repo root（預設 cwd；測試可注入 temp dir）")
    args = parser.parse_args(argv)

    repo_root = args.repo
    manifest = find_latest_manifest(repo_root)
    if manifest is None:
        print(
            json.dumps(
                {"mode": "shadow", "skipped": True,
                 "notice": "no manifest, skipped (shadow)"},
                ensure_ascii=False,
            )
        )
        return 0  # 無 manifest 為常態 → 乾淨放行

    base = resolve_base(env)
    head = resolve_head(env)

    # catalog 壞掉（缺 personas.yaml / YAML 壞 / schema 不過）→ 視為「不可驗證」：
    # 印 verdict（ok=false + catalog_error）、shadow 仍恆放行。
    # 註：import 期 contract.PERSONA_CATALOG 已 fail-closed 退空（見 contract._load_catalog_import_safe），
    # 故 import 不會逃逸；此處再 guard 直接 load_catalog() 的 fail-closed 例外。
    try:
        catalog = load_catalog()
    except (FileNotFoundError, ValueError) as exc:
        verdict = {
            "mode": "shadow",
            "manifest": str(manifest),
            "base": base,
            "head": head,
            "catalog_error": str(exc),
            "ok": False,
        }
        print(json.dumps(verdict, ensure_ascii=False))
        return 0  # shadow：catalog 壞掉仍乾淨放行，僅觀測/annotate

    # manifest 存在但壞掉 → 視為「存在但不可信」：印 verdict（ok=false）、shadow 放行。
    try:
        payload = handoff.read_manifest(manifest, catalog)
        from_role = str(payload.get("from_role", ""))
        manifest_error: str | None = None
    except (FileNotFoundError, ValueError) as exc:
        from_role = ""
        manifest_error = str(exc)

    try:
        changed_paths = gate.compute_changed_paths(base, head, repo=repo_root)
        diff_error: str | None = None
    except RuntimeError as exc:  # fail-closed：取 diff 失敗 → 標記但 shadow 不擋
        changed_paths = []
        diff_error = str(exc)

    manifest_ok = gate.load_manifest_ok(from_role, manifest, catalog)
    verdict = gate.build_verdict(
        role=from_role,
        changed_paths=changed_paths,
        manifest_ok=manifest_ok,
        catalog=catalog,
    )
    verdict["mode"] = "shadow"
    verdict["manifest"] = str(manifest)
    verdict["base"] = base
    verdict["head"] = head
    if manifest_error is not None:
        verdict["manifest_error"] = manifest_error
        verdict["ok"] = False
    if diff_error is not None:
        verdict["diff_error"] = diff_error
        verdict["ok"] = False

    print(json.dumps(verdict, ensure_ascii=False))
    return 0  # shadow：恆放行，僅觀測/annotate


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
