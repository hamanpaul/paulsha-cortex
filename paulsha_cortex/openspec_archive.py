from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


_TBD_PURPOSE_RE = re.compile(
    r"^## Purpose\n(?P<purpose>.+)\n", re.MULTILINE
)


def _is_change_spec_repo(repo_root: Path) -> bool:
    return (repo_root / "openspec" / "changes").is_dir()


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if _is_change_spec_repo(candidate):
            return candidate
    raise FileNotFoundError(f"openspec workspace root not found for {start}")


def _iter_specs(repo_root: Path):
    return tuple((repo_root / "openspec" / "specs").glob("*/spec.md"))


def _snapshot_specs(repo_root: Path):
    specs = {}
    for path in _iter_specs(repo_root):
        specs[path] = path.stat().st_mtime_ns
    return specs


def _touched_specs(repo_root: Path, previous: dict[Path, int]):
    touched = []
    for path, old_mtime in previous.items():
        if not path.exists():
            continue
        if path.stat().st_mtime_ns != old_mtime:
            touched.append(path)
    for path in _iter_specs(repo_root):
        if path not in previous:
            touched.append(path)
    return touched


def _section_body(markdown: str, heading: str) -> str | None:
    pattern = rf"(?ms)^##\s*{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)"
    match = re.search(pattern, markdown)
    if not match:
        return None
    return match.group("body").strip()


def _goal_line(markdown: str) -> str | None:
    goals = _section_body(markdown, "Goals")
    if not goals:
        return None
    for line in goals.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("-"):
            text = text.lstrip("-").strip()
        return text
    return None


def _extract_purpose_from_change(repo_root: Path, change_id: str) -> str | None:
    proposal_path = repo_root / "openspec" / "changes" / change_id / "proposal.md"
    if not proposal_path.exists():
        matches = tuple(
            path
            for path in repo_root.glob(f"openspec/changes/**/*/proposal.md")
            if path.parent.name == change_id
        )
        if not matches:
            return None
        proposal_path = matches[0]

    markdown = proposal_path.read_text(encoding="utf-8")
    purpose = _goal_line(markdown)
    if purpose:
        return purpose

    reason = _section_body(markdown, "Why")
    if reason:
        for line in reason.splitlines():
            text = line.strip()
            if text:
                return text

    return None


def _resolve_purpose(change_id: str, repo_root: Path) -> str:
    purpose = _extract_purpose_from_change(repo_root, change_id)
    if purpose:
        return purpose
    return f"從 openspec archive 產生的 {change_id} 規格。"


def _apply_purpose(path: Path, purpose: str) -> bool:
    text = path.read_text(encoding="utf-8")
    match = re.search(_TBD_PURPOSE_RE, text)
    if not match:
        return False
    if match.group("purpose").startswith("TBD - created by archiving change"):
        updated = text[: match.start("purpose")] + purpose + text[match.end("purpose") :]
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def _update_changed_specs(repo_root: Path, touched_specs: list[Path]) -> None:
    for spec_path in touched_specs:
        content = spec_path.read_text(encoding="utf-8")
        match = re.search(r"(?m)^## Purpose$", content)
        if not match:
            continue
        purpose_line = content[match.end() + 1 :]
        purpose_match = re.match(r"(?P<line>.+)\n", purpose_line)
        if not purpose_match:
            continue
        line = purpose_match.group("line")
        if not line.startswith("TBD - created by archiving change "):
            continue
        change_id = line.removeprefix("TBD - created by archiving change ").removesuffix(". Update Purpose after archive.")
        purpose = _resolve_purpose(change_id, repo_root)
        _apply_purpose(spec_path, purpose)


def _resolve_real_openspec_path() -> str:
    env_path = os.environ.get("PAULSHA_REAL_OPENSPEC")
    if env_path:
        return env_path

    wrapper_parent = Path(__file__).resolve().parent.parent / "scripts"
    path = os.environ.get("PATH", "")
    filtered_parts = [
        item
        for item in path.split(os.pathsep)
        if item and Path(item).resolve() != wrapper_parent.resolve()
    ]
    real_openspec = shutil.which("openspec", path=os.pathsep.join(filtered_parts))
    if real_openspec is None:
        raise RuntimeError("openspec executable not found")
    return real_openspec


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit("openspec wrapper needs at least one argument")

    real_openspec = _resolve_real_openspec_path()

    repo_root = Path.cwd()
    specs_snapshot = None
    if args[0] == "archive":
        try:
            repo_root = _find_repo_root(repo_root)
            specs_snapshot = _snapshot_specs(repo_root)
        except FileNotFoundError:
            specs_snapshot = None

    result = subprocess.run([real_openspec, *args], check=False)

    if args[0] == "archive" and result.returncode == 0 and specs_snapshot is not None:
        _update_changed_specs(repo_root, _touched_specs(repo_root, specs_snapshot))

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
