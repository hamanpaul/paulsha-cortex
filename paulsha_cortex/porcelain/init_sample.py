from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from . import COMMANDS, PorcelainCommand, register

INIT_SAMPLE_SCHEMA = "cortex-porcelain/init-sample/v1"
DEFAULT_COMBO = "feature-oneshot"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def register_commands() -> None:
    if "init-sample" in COMMANDS:
        return
    register(
        PorcelainCommand(
            name="init-sample",
            help="建立第一個 dispatch: hold sample workflow",
            run=main,
        )
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex init-sample")
    parser.add_argument("--task", required=True, help="sample workflow 的人類可讀任務描述")
    parser.add_argument("--combo", default=DEFAULT_COMBO, help="deck combo ID（預設 feature-oneshot）")
    parser.add_argument("--change", help="OpenSpec change ID；省略時由 task slug 化")
    parser.add_argument("--json", action="store_true", help="輸出 cortex-porcelain/init-sample/v1 JSON")
    return parser


def _json_dump(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or "sample"


def _combo_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "deck" / "data" / "combos"


def _available_combos() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in _combo_dir().glob("*.yaml")))


def _validate_combo(parser: argparse.ArgumentParser, combo: str) -> None:
    if combo in _available_combos():
        return
    choices = ", ".join(_available_combos()) or "(none)"
    parser.error(f"unknown --combo {combo!r}; available combos: {choices}")


def _load_compile_result(*, combo: str, task: str, change: str):
    from paulsha_cortex.deck.compile import compile_combo
    from paulsha_cortex.deck.schema import DEFAULT_CARDS_PATH, DEFAULT_COMBOS_DIR, load_cards, load_combo

    cards = load_cards(DEFAULT_CARDS_PATH)
    loaded_combo = load_combo(DEFAULT_COMBOS_DIR / f"{combo}.yaml", cards)
    return compile_combo(
        loaded_combo,
        cards,
        task,
        change=change,
        allow_external=True,
    )


def _specs_root() -> Path:
    from paulsha_cortex.deck.compile import specs_dir

    return specs_dir()


def _find_emitted_specs(*, task_slug: str, change: str) -> list[Path]:
    root = _specs_root()
    candidates: list[Path] = []
    for prefix in dict.fromkeys((change, task_slug)):
        candidates.extend(sorted(root.glob(f"{prefix}-*.md")))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _read_frontmatter(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError(f"{path.name}: missing frontmatter")
    lines = content.splitlines()
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def _ensure_hold(paths: Sequence[Path]) -> None:
    for path in paths:
        frontmatter = _read_frontmatter(path)
        if frontmatter.get("dispatch") != "hold":
            raise ValueError(f"{path.name}: dispatch must remain hold")


def _required_fields() -> list[dict[str, str]]:
    return [
        {
            "name": "plan",
            "current": "glob path from deck emit",
            "required": "replace with the exact accepted plan path",
        },
        {
            "name": "target_branch",
            "current": "null",
            "required": "main",
        },
        {
            "name": "verification",
            "current": "null",
            "required": "object with persona-scope, name=policy check, and full_suite baseline=no-regression",
        },
    ]


def _next_steps() -> list[str]:
    return [
        "Edit the emitted spec and keep dispatch: hold until the checklist is complete.",
        "Replace plan with an exact accepted plan path.",
        "Set target_branch to main.",
        "Replace verification with the full contract, including persona-scope, policy, and full_suite.",
        "Run the listed deck verify commands.",
        "When everything is ready, manually change dispatch: hold to dispatch: auto.",
    ]


def _print_human_summary(payload: dict[str, Any]) -> None:
    sys.stdout.write(f"sample: {payload['combo']} -> {payload['task_slug']}\n")
    sys.stdout.write("specs:\n")
    for path in payload["specs"]:
        sys.stdout.write(f"  - {path}\n")
    sys.stdout.write("required before dispatch can become auto:\n")
    sys.stdout.write("  - plan: replace the glob with the exact accepted plan path\n")
    sys.stdout.write("  - target_branch: main\n")
    sys.stdout.write("  - verification: include persona-scope, policy, and full_suite (baseline=no-regression)\n")
    sys.stdout.write("deck verify:\n")
    for command in payload["verify_commands"]:
        sys.stdout.write(f"  - {command}\n")
    sys.stdout.write("next:\n")
    sys.stdout.write("  - keep dispatch: hold until the checklist is complete\n")
    sys.stdout.write("  - manually flip dispatch to auto only after the verify commands pass\n")


def main(argv: Sequence[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    _validate_combo(parser, args.combo)

    effective_change = args.change or _slugify(args.task)
    result = _load_compile_result(combo=args.combo, task=args.task, change=effective_change)

    from paulsha_cortex.deck.cli import main as deck_main

    exit_code = int(
        deck_main(
            [
                "compile",
                args.combo,
                "--task",
                args.task,
                "--change",
                effective_change,
                "--allow-external",
                "--emit",
            ]
        )
        or 0
    )
    if exit_code != 0:
        return exit_code

    try:
        specs = _find_emitted_specs(task_slug=result.task_slug, change=effective_change)
        if not specs:
            raise ValueError(
                f"no emitted specs found under {os.fspath(_specs_root())} for {effective_change}/{result.task_slug}"
            )
        _ensure_hold(specs)
    except (OSError, ValueError) as exc:
        print(f"init-sample: {exc}", file=sys.stderr)
        return 1

    payload = {
        "schema": INIT_SAMPLE_SCHEMA,
        "command": "init-sample",
        "combo": args.combo,
        "task": args.task,
        "change": effective_change,
        "task_slug": result.task_slug,
        "specs": [path.name for path in specs],
        "required_fields": _required_fields(),
        "verify_commands": list(result.verify_commands),
        "next_steps": _next_steps(),
    }
    if args.json:
        _json_dump(payload)
        return 0
    _print_human_summary(payload)
    return 0
