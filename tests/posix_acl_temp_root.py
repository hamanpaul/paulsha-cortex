from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

import pytest


@dataclass(frozen=True)
class AclProbe:
    target: Literal["dir", "file"]
    args: tuple[str, ...]


def dir_acl_probe(*args: str) -> AclProbe:
    return AclProbe("dir", tuple(args))


def file_acl_probe(*args: str) -> AclProbe:
    return AclProbe("file", tuple(args))


def _candidate_temp_bases() -> tuple[str, ...]:
    bases: list[str] = []
    for raw in ("/var/tmp", "/tmp", tempfile.gettempdir()):
        base = str(raw).strip()
        if not base or base in bases or not Path(base).is_dir():
            continue
        bases.append(base)
    return tuple(bases)


def pick_posix_acl_temp_root(
    *,
    prefix: str,
    root_mode: int,
    probes: Sequence[AclProbe],
    skip_reason: str,
) -> Path:
    if not probes:
        raise ValueError("at least one ACL probe is required")
    if shutil.which("setfacl") is None or shutil.which("getfacl") is None:
        pytest.skip(skip_reason)
    for base in _candidate_temp_bases():
        root = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        probe_dir = root / ".acl-probe-dir"
        probe_file = root / ".acl-probe-file"
        try:
            os.chmod(root, root_mode)
            probe_dir.mkdir(mode=0o700)
            probe_file.write_text("probe\n", encoding="utf-8")
            supported = True
            for probe in probes:
                target = probe_dir if probe.target == "dir" else probe_file
                completed = subprocess.run(
                    ["setfacl", *probe.args, str(target)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if completed.returncode != 0:
                    supported = False
                    break
            if supported:
                return root
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
            probe_file.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)
    pytest.skip(skip_reason)
