#!/usr/bin/env python3
"""Release preflight helpers for the RC qualification profiles (stdlib only).

`release.yml` asks two questions here instead of in inline jq:

- `legacy-requirement`: does the release diff against the previous release
  tag touch `paulsha_cortex/trust_root/install/` or `qualification/`?  Then
  the exact release SHA also needs a passing `legacy-adoption` run (#1122).
  Without a previous release tag the answer is yes (fail closed).
- `select-run`: which completed, successful rc-qualification.yml run of the
  requested profile, on the exact SHA and no older than three days, carries
  the evidence.  Runs are told apart by their run title, which the RC workflow
  derives from its `profile` input.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LEGACY_TRIGGER_PREFIXES = ("paulsha_cortex/trust_root/install/", "qualification/")
RELEASE_TAG_PATTERN = "v[0-9]*"
MAX_RUN_AGE_SECONDS = 3 * 24 * 60 * 60
RC_WORKFLOW_NAME = "RC qualification"
#: Run titles per profile.  A bare workflow name is a run dispatched before the
#: RC workflow had a profile input, which always ran the release profile.
RUN_TITLES = {
    "release": (f"{RC_WORKFLOW_NAME} (release)", RC_WORKFLOW_NAME),
    "legacy-adoption": (f"{RC_WORKFLOW_NAME} (legacy-adoption)",),
}
ARTIFACT_PREFIXES = {
    "release": "rc-qualification-",
    "legacy-adoption": "rc-qualification-legacy-adoption-",
}
_SHA40 = re.compile(r"^[0-9a-f]{40}$")


class GateError(RuntimeError):
    """The release preflight cannot find the qualification it requires."""


def legacy_trigger_paths(paths: Iterable[str]) -> list[str]:
    return sorted(
        path for path in paths if any(path.startswith(prefix) for prefix in LEGACY_TRIGGER_PREFIXES)
    )


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def previous_release_tag(repo: Path, head: str) -> str | None:
    """The newest release tag reachable from ``head``'s first parent.

    A tag on the release commit itself (a stale transaction of this very
    release) is never the previous release.
    """

    result = _git(
        repo, "describe", "--tags", "--abbrev=0", "--match", RELEASE_TAG_PATTERN, f"{head}^"
    )
    tag = result.stdout.strip()
    return tag if result.returncode == 0 and tag else None


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    result = _git(repo, "diff", "--name-only", "-z", base, head, "--")
    if result.returncode != 0:
        raise GateError(f"cannot diff {base}..{head}: {result.stderr.strip()[:400]}")
    return sorted(path for path in result.stdout.split("\0") if path)


def legacy_requirement(repo: Path, head: str) -> dict[str, Any]:
    if _SHA40.fullmatch(head) is None:
        raise GateError("--head must be the 40-hex release commit")
    base = previous_release_tag(repo, head)
    watched = " or ".join(LEGACY_TRIGGER_PREFIXES)
    if base is None:
        return {
            "required": True,
            "base_tag": None,
            "paths": [],
            "reason": (
                "no previous release tag is reachable from the release commit's parent; "
                "legacy-adoption qualification is required"
            ),
        }
    paths = legacy_trigger_paths(changed_paths(repo, base, head))
    if not paths:
        return {
            "required": False,
            "base_tag": base,
            "paths": [],
            "reason": f"no change under {watched} since {base}",
        }
    return {
        "required": True,
        "base_tag": base,
        "paths": paths,
        "reason": f"{len(paths)} change(s) under {watched} since {base}",
    }


def _epoch(value: object) -> float:
    if not isinstance(value, str):
        raise GateError("workflow run has no created_at timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def select_run(
    runs: Mapping[str, Any],
    *,
    sha: str,
    profile: str,
    now: float,
    max_age: int = MAX_RUN_AGE_SECONDS,
) -> dict[str, Any]:
    if profile not in RUN_TITLES:
        raise GateError(f"unknown RC qualification profile: {profile}")
    if _SHA40.fullmatch(sha) is None:
        raise GateError("--sha must be the 40-hex release commit")
    rows = runs.get("workflow_runs") if isinstance(runs, Mapping) else None
    if not isinstance(rows, list):
        raise GateError("workflow runs response has no workflow_runs list")
    candidates = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("head_sha") == sha
        and row.get("status") == "completed"
        and row.get("conclusion") == "success"
        and row.get("display_title") in RUN_TITLES[profile]
    ]
    if not candidates:
        raise GateError(
            f"no successful {profile} rc-qualification.yml run has head_sha={sha}"
        )
    selected = max(candidates, key=lambda row: (_epoch(row.get("created_at")), row.get("id", 0)))
    created = _epoch(selected.get("created_at"))
    if created > now:
        raise GateError(f"the exact-SHA {profile} RC qualification is future-dated")
    if now - created > max_age:
        raise GateError(f"the exact-SHA {profile} RC qualification is older than 3 days")
    return {
        "id": selected.get("id"),
        "created_at": selected.get("created_at"),
        "display_title": selected.get("display_title"),
        "profile": profile,
        "artifact_name": ARTIFACT_PREFIXES[profile] + sha,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    requirement = sub.add_parser("legacy-requirement")
    requirement.add_argument("--repo", type=Path, default=Path("."))
    requirement.add_argument("--head", required=True)
    select = sub.add_parser("select-run")
    select.add_argument("--runs", required=True, type=Path)
    select.add_argument("--sha", required=True)
    select.add_argument("--profile", required=True, choices=tuple(RUN_TITLES))
    select.add_argument("--now", type=float)
    args = parser.parse_args(argv)
    try:
        if args.command == "legacy-requirement":
            payload = legacy_requirement(args.repo, args.head)
        else:
            runs = json.loads(args.runs.read_text(encoding="utf-8"))
            payload = select_run(
                runs,
                sha=args.sha,
                profile=args.profile,
                now=args.now if args.now is not None else time.time(),
            )
    except (GateError, OSError, ValueError) as exc:
        print(f"release gate: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
