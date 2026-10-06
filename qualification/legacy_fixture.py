#!/usr/bin/env python3
"""RC `legacy-adoption` profile 的 Phase 2b 形狀 fixture：manifest、驗證與布置。

`legacy_fixture.json` 描述一台「沒有 receipt 的 Phase 2b 舊主機」：非 release 預設 id
的五個 cortex 帳號、實體 venv 目錄、手工 unit 與 installer 不產生的 drop-in、polkit
rule、帶 ACL 的 state（含 plan 未宣告的頂層項目、受管目錄內未列管子目錄與暫存殘留）、
worktree pool、帶 branch／worktree 的非 canonical source repo，以及內容全為假值的
credential 類檔。#1282 起另含參考主機 adoption 遇到的形狀：服務停止後殘留的 UNIX
socket（state 頂層一個、被 quarantine 的目錄內一個）與 FIFO、Manager HOME 頂層的
retry 檔；review-sandboxes 的 census 發現不再需要 operator 例外。

布置只在兩種情況執行：
- 以 root 在 disposable qualification 容器內（`/`，有容器標記），由
  `legacy_adoption.py` 呼叫；絕不在主機上執行。
- 以一般使用者布置到暫存目錄（`--root DIR --rootless`），供單元測試以真正的
  inventory collector 與 plan 推導證明 fixture 的形狀；不建帳號、不 chown、不設 ACL。

本檔只用標準函式庫：`validate.py` 在 host 端也 import 它來綁定 fixture digest。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


MANIFEST_PATH = Path(__file__).with_name("legacy_fixture.json")
MANIFEST_KIND = "paulsha-cortex/legacy-adoption-fixture"
LEGACY_PROFILE = "legacy-adoption"

#: 帳號名稱與 principal（credential adapter 的鍵）。
CORTEX_ACCOUNTS = (
    "cortex-manager",
    "cortex-reviewer-planner",
    "cortex-builder",
    "cortex-gate",
    "cortex-egress",
)
PRINCIPAL_ACCOUNTS = {
    "builder": "cortex-builder",
    "reviewer-planner": "cortex-reviewer-planner",
    "manager": "cortex-manager",
}
#: 會被 writable census 檢查的 job 帳號（Manager 不在其內）。
CENSUS_PRINCIPALS = ("cortex-builder", "cortex-reviewer-planner", "cortex-gate", "cortex-egress")
#: v0.1.13 以前 release install config 寫死的帳號 id（#1286 起改由 plan 決定；Ubuntu 上
#: 這段屬於 systemd-resolve 與 udev 群組）。legacy fixture 必須避開，adoption 才證明是
#: overlay 宣告的號碼而不是預設值。
RELEASE_ACCOUNT_IDS = frozenset({991, 992, 993, 994, 995})
SERVICES = ("cortex-egress-proxy.service", "cortex-manager.service", "cortex-monitor.service")

#: harness 依序執行的步驟；evidence 與 validator 逐一核對名稱與順序。
LEGACY_STEPS = (
    "fixture",
    "inventory",
    "plan",
    "stop-legacy-services",
    "apply",
    "rollback",
    "restart-legacy-services",
    "reapply",
    "credentials",
    "activate",
    "verify",
    "launcher-authorities",
)
#: qualification.json 的 legacy-adoption 專屬測試名稱。
LEGACY_TESTS = (
    "legacy-fixture-seeded",
    "legacy-inventory",
    "legacy-plan-bound",
    "legacy-adoption-apply",
    "legacy-adoption-rollback",
    "legacy-host-restored",
    "legacy-adoption-reapply",
    "legacy-credential-reimport",
    "legacy-activate-verify",
    "legacy-launcher-authorities",
)
#: harness 產生、driver 帶進 evidence 的檔名。
LEGACY_EVIDENCE_FILES = (
    "legacy-adoption.json",
    "legacy-inventory.json",
    "legacy-rollback.json",
)


def provider_check_for_principal(principal: str) -> str:
    """Return the authority check recorded for an imported credential principal."""
    return "credential" if principal == "manager" else "launcher"


#: ``socket``／``fifo`` 是沒有 listener／reader 的殘留物件（#1282），以 mknod 建立。
_ENTRY_TYPES = frozenset({"directory", "file", "symlink", "sparse", "socket", "fifo"})
_MODE = re.compile(r"^0[0-7]{3}$")
_ACL_ENTRY = re.compile(r"^(?:[ug]:[a-z_][a-z0-9_-]*|[ugo]:):[r-][w-][x-]$")
_PERMS = re.compile(r"^[r-][w-][x-]$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ENV_VALUES = frozenset({"docker", "podman", "oci"})


class FixtureError(RuntimeError):
    """The legacy fixture manifest is malformed or cannot be laid out safely."""


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def manifest_sha256(path: Path | None = None) -> str:
    return hashlib.sha256((path or MANIFEST_PATH).read_bytes()).hexdigest()


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    try:
        document = json.loads((path or MANIFEST_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"legacy fixture manifest is unreadable: {exc}") from exc
    validate_manifest(document)
    return document


def _normalized(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and value != "/"
        and "\x00" not in value
        and posixpath.normpath(value) == value
    )


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _account(row: object, label: str) -> dict[str, Any]:
    if not isinstance(row, Mapping) or set(row) != {"name", "uid", "gid", "home", "shell"}:
        raise FixtureError(f"{label} must be {{name, uid, gid, home, shell}}")
    for key in ("uid", "gid"):
        value = row[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FixtureError(f"{label}.{key} must be a positive integer")
    if not _normalized(row["home"]) or not _normalized(row["shell"]):
        raise FixtureError(f"{label} home and shell must be normalized absolute paths")
    return dict(row)


def validate_manifest(document: object) -> None:
    """Fail closed on anything the seeder or the harness could misread."""

    if not isinstance(document, Mapping):
        raise FixtureError("legacy fixture manifest must be an object")
    if document.get("schema_version") != 1 or document.get("kind") != MANIFEST_KIND:
        raise FixtureError("legacy fixture manifest must be schema v1 of the fixture kind")
    for text in _strings(document):
        if "/home/" in text or text.startswith("/home"):
            raise FixtureError(
                "legacy fixture manifest must not name a /home/ path (tier shareable)"
            )

    accounts = [_account(row, f"accounts[{index}]") for index, row in enumerate(document.get("accounts") or [])]
    names = [row["name"] for row in accounts]
    if sorted(names) != sorted(CORTEX_ACCOUNTS):
        missing = sorted(set(CORTEX_ACCOUNTS) - set(names))
        raise FixtureError(
            "legacy fixture must define exactly the five cortex accounts"
            + (f"; missing {', '.join(missing)}" if missing else "")
        )
    extra = [
        _account(row, f"extra_accounts[{index}]")
        for index, row in enumerate(document.get("extra_accounts") or [])
    ]
    everyone = accounts + extra
    for key in ("name", "uid", "gid"):
        values = [row[key] for row in everyone]
        if len(set(values)) != len(values):
            raise FixtureError(f"legacy fixture accounts must have unique {key}s")
    for row in accounts:
        if row["uid"] in RELEASE_ACCOUNT_IDS or row["gid"] in RELEASE_ACCOUNT_IDS:
            raise FixtureError(
                f"account {row['name']} reuses a release config id; the Phase 2b "
                "shape keeps ids the release config does not use"
            )
    owners = {"root", *(row["name"] for row in everyone)}

    services = document.get("services")
    if services != list(SERVICES):
        raise FixtureError(f"legacy fixture services must be exactly {', '.join(SERVICES)}")

    repositories = document.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise FixtureError("legacy fixture must define a source repository")
    repository_paths: list[str] = []
    for index, repository in enumerate(repositories):
        label = f"repositories[{index}]"
        if not isinstance(repository, Mapping) or set(repository) != {
            "path", "owner", "group", "mode", "origin", "files", "branches", "worktrees"
        }:
            raise FixtureError(f"{label} has an unexpected shape")
        if not _normalized(repository["path"]) or not _MODE.match(str(repository["mode"])):
            raise FixtureError(f"{label} path and mode must be normalized")
        if repository["owner"] not in owners or repository["group"] not in owners:
            raise FixtureError(f"{label} owner/group is not a fixture account")
        if not str(repository["origin"]).startswith("https://example.invalid/"):
            raise FixtureError(f"{label}.origin must be a reserved example.invalid URL")
        files = repository["files"]
        if not isinstance(files, Mapping) or not files or not all(
            isinstance(name, str) and "/" not in name and isinstance(body, str)
            for name, body in files.items()
        ):
            raise FixtureError(f"{label}.files must map top-level names to text")
        if not isinstance(repository["branches"], list) or not repository["branches"]:
            raise FixtureError(f"{label} must carry at least one branch")
        repository_paths.append(repository["path"])
        for wt_index, worktree in enumerate(repository["worktrees"]):
            if (
                not isinstance(worktree, Mapping)
                or set(worktree) != {"path", "branch", "owner", "group"}
                or not _normalized(worktree["path"])
                or worktree["owner"] not in owners
                or worktree["group"] not in owners
            ):
                raise FixtureError(f"{label}.worktrees[{wt_index}] has an unexpected shape")
            repository_paths.append(worktree["path"])
        if not repository["worktrees"]:
            raise FixtureError(f"{label} must carry a worktree")

    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise FixtureError("legacy fixture must define entries")
    known: dict[str, Mapping[str, Any]] = {}
    for index, entry in enumerate(entries):
        label = f"entries[{index}]"
        if not isinstance(entry, Mapping):
            raise FixtureError(f"{label} must be an object")
        path = entry.get("path")
        if not _normalized(path):
            raise FixtureError(f"{label}.path must be a normalized absolute path")
        assert isinstance(path, str)
        if path in known or path in repository_paths:
            raise FixtureError(f"{label} duplicate path {path}")
        kind = entry.get("type")
        if kind not in _ENTRY_TYPES:
            raise FixtureError(f"{label}.type must be one of {sorted(_ENTRY_TYPES)}")
        if entry.get("owner") not in owners or entry.get("group") not in owners:
            raise FixtureError(f"{label} owner/group must be root or a fixture account: {path}")
        allowed = {"path", "type", "owner", "group", "class"}
        if kind == "symlink":
            allowed |= {"target"}
            if not isinstance(entry.get("target"), str) or not entry["target"]:
                raise FixtureError(f"{label}.target is required for a symlink")
        else:
            allowed |= {"mode"}
            if not _MODE.match(str(entry.get("mode", ""))):
                raise FixtureError(f"{label}.mode must be a 4-digit octal string")
        if kind == "directory":
            allowed |= {"existing", "acl", "default_acl", "mask"}
            if entry.get("existing", "keep") != "keep":
                raise FixtureError(f"{label}.existing may only be keep")
            for key in ("acl", "default_acl"):
                rows = entry.get(key, [])
                if not isinstance(rows, list) or not all(
                    isinstance(row, str) and _ACL_ENTRY.match(row) for row in rows
                ):
                    raise FixtureError(f"{label}.{key} must be setfacl short-form entries")
            if "mask" in entry and not _PERMS.match(str(entry["mask"])):
                raise FixtureError(f"{label}.mask must be rwx-style permissions")
        if kind == "file":
            allowed |= {"content"}
            if not isinstance(entry.get("content"), str):
                raise FixtureError(f"{label}.content is required for a file")
        if kind == "sparse":
            allowed |= {"size"}
            size = entry.get("size")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise FixtureError(f"{label}.size must be a positive integer")
        if set(entry) - allowed:
            raise FixtureError(f"{label} has unknown fields: {', '.join(sorted(set(entry) - allowed))}")
        if entry.get("class", "credential") != "credential":
            raise FixtureError(f"{label}.class may only be credential")
        if entry.get("class") == "credential" and kind != "file":
            raise FixtureError(f"{label} credential fixtures must be regular files")
        if entry.get("class") == "credential" and "legacy-fixture-secret-" not in entry["content"]:
            raise FixtureError(f"{label} credential content must carry a legacy-fixture-secret- marker")
        parent = posixpath.dirname(path)
        if "existing" not in entry and parent not in known and not any(
            _within(parent, root) for root in repository_paths
        ):
            raise FixtureError(f"{label} parent {parent} must be listed before {path}")
        known[path] = entry
    for repository in repositories:
        for path in [repository["path"], *(row["path"] for row in repository["worktrees"])]:
            if posixpath.dirname(path) not in known:
                raise FixtureError(f"repository parent of {path} must be an entry")

    credentials = document.get("credentials")
    if not isinstance(credentials, list) or not credentials:
        raise FixtureError("legacy fixture must list its reimportable credentials")
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(credentials):
        if (
            not isinstance(row, Mapping)
            or set(row) != {"principal", "provider", "path"}
            or row["principal"] not in PRINCIPAL_ACCOUNTS
        ):
            raise FixtureError(f"credentials[{index}] has an unexpected shape")
        entry = known.get(row["path"])
        if entry is None or entry.get("class") != "credential":
            raise FixtureError(f"credentials[{index}] must name a credential fixture file")
        key = (row["principal"], row["provider"])
        if key in seen:
            raise FixtureError(f"credentials[{index}] duplicates {key}")
        seen.add(key)

    block = document.get("legacy_adoption")
    if not isinstance(block, Mapping) or set(block) != {
        "quarantine_root",
        "inventory_directory",
        "host_overlay_path",
        "census_exceptions",
        "quarantine_paths",
    }:
        raise FixtureError("legacy_adoption must name the installer locations and review lists")
    for key in ("quarantine_root", "inventory_directory", "host_overlay_path"):
        if not _normalized(block[key]):
            raise FixtureError(f"legacy_adoption.{key} must be a normalized absolute path")
    for row in block["census_exceptions"]:
        if (
            not isinstance(row, Mapping)
            or set(row) != {"path", "principal"}
            or row["principal"] not in CENSUS_PRINCIPALS
            or row["path"] not in known
        ):
            raise FixtureError("legacy_adoption.census_exceptions must name fixture paths")
    for path in block["quarantine_paths"]:
        if path not in known:
            raise FixtureError(f"legacy_adoption.quarantine_paths names no fixture path: {path}")

    expected = document.get("expected")
    if not isinstance(expected, Mapping) or set(expected) != {"quarantine", "adopted_samples"}:
        raise FixtureError("expected must list quarantine and adopted_samples")
    quarantine = expected["quarantine"]
    if not isinstance(quarantine, list) or quarantine != sorted(set(quarantine)) or not quarantine:
        raise FixtureError("expected.quarantine must be sorted unique paths")
    for path in quarantine:
        if path not in known and path not in repository_paths:
            raise FixtureError(f"expected.quarantine names no fixture object: {path}")
    for path in expected["adopted_samples"]:
        entry = known.get(path)
        if entry is None or entry.get("type") != "file" or entry.get("class") == "credential":
            raise FixtureError(f"expected.adopted_samples must name non-credential files: {path}")
        if any(_within(path, root) for root in quarantine):
            raise FixtureError(f"expected.adopted_samples {path} lies in a quarantined object")


# ---------------------------------------------------------------------------
# derived views (shared by the harness, the driver, the validator and tests)
# ---------------------------------------------------------------------------


def rebase(path: str, root: Path | str = "/") -> str:
    """``path`` below ``root``; ``/`` leaves the path unchanged."""

    base = str(root).rstrip("/")
    return path if not base else base + path


def accounts(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: dict(row) for row in manifest["accounts"]}


def host_overlay(
    manifest: Mapping[str, Any],
    *,
    root: Path | str = "/",
    inventory_sha256: str | None = None,
) -> dict[str, Any]:
    """The persistent host overlay; with a digest, also the legacy_adoption block."""

    rows = accounts(manifest)
    egress = rows["cortex-egress"]
    overlay: dict[str, Any] = {
        "accounts": {
            name: {"uid": row["uid"], "gid": row["gid"]}
            for name, row in sorted(rows.items())
            if name != "cortex-egress"
        },
        "service_accounts": {
            "cortex-egress": {
                "uid": egress["uid"],
                "gid": egress["gid"],
                "home": rebase(egress["home"], root),
            }
        },
    }
    if inventory_sha256 is not None:
        if not _SHA256.match(inventory_sha256):
            raise FixtureError("inventory digest must be 64 lowercase hex characters")
        block = manifest["legacy_adoption"]
        overlay["legacy_adoption"] = {
            "inventory_sha256": inventory_sha256,
            "quarantine_root": rebase(block["quarantine_root"], root),
            "census_exceptions": [
                {"path": rebase(row["path"], root), "principal": row["principal"]}
                for row in block["census_exceptions"]
            ],
            "quarantine_paths": [rebase(path, root) for path in block["quarantine_paths"]],
        }
    return overlay


def credential_secrets(manifest: Mapping[str, Any]) -> list[bytes]:
    """Fake credential contents and their markers, for the redaction scan."""

    values: set[bytes] = set()
    for entry in manifest["entries"]:
        if entry.get("class") != "credential":
            continue
        content = entry["content"].encode()
        values.add(content)
        values.update(
            match.encode()
            for match in re.findall(r"legacy-fixture-secret-[a-z0-9-]+", entry["content"])
        )
    return sorted(values, key=len, reverse=True)


def service_users(manifest: Mapping[str, Any]) -> dict[str, str]:
    """``User=`` of each legacy service unit."""

    entries = {entry["path"]: entry for entry in manifest["entries"]}
    users: dict[str, str] = {}
    for service in SERVICES:
        entry = entries.get(f"/etc/systemd/system/{service}")
        if entry is None:
            raise FixtureError(f"legacy fixture lacks the {service} unit")
        match = re.search(r"(?m)^User=(\S+)$", entry["content"])
        if match is None:
            raise FixtureError(f"legacy {service} unit has no User=")
        users[service] = match.group(1)
    return users


def service_identities(manifest: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    """The (uid, gid) each legacy-adopted service must run as."""

    rows = accounts(manifest)
    return {
        service: (rows[user]["uid"], rows[user]["gid"])
        for service, user in service_users(manifest).items()
    }


def _perm_grants_write(perms: str) -> bool:
    return len(perms) == 3 and perms[1] == "w"


def writable_paths(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    """Fixture objects each job account may write (owner, group or ACL grant)."""

    result: dict[str, set[str]] = {name: set() for name in CENSUS_PRINCIPALS}

    def consider(path: str, owner: str, group: str, mode: str, acl: Sequence[str]) -> None:
        bits = int(mode, 8)
        for name in CENSUS_PRINCIPALS:
            if (
                (owner == name and bits & 0o200)
                or (group == name and bits & 0o020)
                or bits & 0o002
                or any(
                    row.startswith(f"u:{name}:") and _perm_grants_write(row.rsplit(":", 1)[1])
                    for row in acl
                )
            ):
                result[name].add(path)

    for entry in manifest["entries"]:
        if entry["type"] == "symlink":
            continue
        consider(entry["path"], entry["owner"], entry["group"], entry["mode"], entry.get("acl", []))
    for repository in manifest["repositories"]:
        consider(repository["path"], repository["owner"], repository["group"], repository["mode"], [])
        for worktree in repository["worktrees"]:
            consider(worktree["path"], worktree["owner"], worktree["group"], "0755", [])
    return {name: sorted(paths) for name, paths in result.items()}


def quarantine_steps(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(step)
        for step in plan.get("apply_order", []) or []
        if isinstance(step, Mapping) and step.get("kind") == "legacy-quarantine"
    ]


def credential_sources(
    manifest: Mapping[str, Any], plan: Mapping[str, Any], *, root: Path | str = "/"
) -> dict[tuple[str, str], str]:
    """Where each plan-required credential lies inside the quarantine.

    The legacy credential moved with its covering quarantine step; the source
    for the reimport is that step's destination plus the remaining suffix.
    """

    legacy_paths = {
        (row["principal"], row["provider"]): rebase(row["path"], root)
        for row in manifest["credentials"]
    }
    steps = quarantine_steps(plan)
    sources: dict[tuple[str, str], str] = {}
    for row in plan.get("required_credentials", []) or []:
        key = (str(row["principal"]), str(row["provider"]))
        path = legacy_paths.get(key)
        if path is None:
            raise FixtureError(f"legacy fixture has no credential for {key[0]}/{key[1]}")
        covering = [step for step in steps if _within(path, str(step["path"]))]
        if len(covering) != 1:
            raise FixtureError(f"credential {path} is not covered by exactly one quarantine step")
        step = covering[0]
        sources[key] = str(step["destination"]) + path[len(str(step["path"])):]
    return sources


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


Runner = Callable[..., subprocess.CompletedProcess]


def _container_markers() -> tuple[str, ...]:
    markers = []
    if os.environ.get("container") in _CONTAINER_ENV_VALUES:
        markers.append("container-env")
    for path in ("/.dockerenv", "/run/.containerenv"):
        if os.path.exists(path):
            markers.append(path)
    return tuple(markers)


def _run(argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> None:
    completed = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=dict(env) if env is not None else None,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[:400]
        raise FixtureError(f"{' '.join(argv[:3])} failed rc={completed.returncode}: {detail}")


def _ids(manifest: Mapping[str, Any], rootless: bool) -> tuple[dict[str, int], dict[str, int]]:
    users = {"root": 0}
    groups = {"root": 0}
    for row in manifest["accounts"] + manifest["extra_accounts"]:
        users[row["name"]] = row["uid"]
        groups[row["name"]] = row["gid"]
    if rootless:
        return {name: -1 for name in users}, {name: -1 for name in groups}
    return users, groups


def _shadow_locked(name: str) -> bool:
    for line in Path("/etc/shadow").read_text(encoding="utf-8").splitlines():
        account, _sep, rest = line.partition(":")
        if account == name:
            return rest.partition(":")[0].startswith(("!", "*"))
    return False


def _create_accounts(manifest: Mapping[str, Any]) -> None:
    import grp
    import pwd

    rows = list(manifest["accounts"]) + list(manifest["extra_accounts"])
    for row in rows:
        for lookup, value, label in (
            (pwd.getpwnam, row["name"], "account"),
            (grp.getgrnam, row["name"], "group"),
            (pwd.getpwuid, row["uid"], "uid"),
            (grp.getgrgid, row["gid"], "gid"),
        ):
            try:
                lookup(value)
            except KeyError:
                continue
            raise FixtureError(
                f"fixture {label} {value} is already taken in this container; "
                "pick ids the reference image does not use"
            )
    for row in rows:
        _run(("/usr/sbin/groupadd", "--gid", str(row["gid"]), row["name"]))
        _run(
            (
                "/usr/sbin/useradd",
                "--system",
                "--uid",
                str(row["uid"]),
                "--gid",
                str(row["gid"]),
                "--home-dir",
                row["home"],
                "--no-create-home",
                "--shell",
                row["shell"],
                "--comment",
                "legacy Phase 2b qualification fixture",
                row["name"],
            )
        )
        if not _shadow_locked(row["name"]):
            _run(("/usr/sbin/usermod", "--lock", row["name"]))
        if not _shadow_locked(row["name"]):
            raise FixtureError(f"fixture account {row['name']} password is not locked")


def _chown(path: str, owner: str, group: str, users: Mapping[str, int], groups: Mapping[str, int]) -> None:
    uid, gid = users[owner], groups[group]
    if uid < 0:
        return
    os.chown(path, uid, gid, follow_symlinks=False)


def _setfacl(path: str, entry: Mapping[str, Any]) -> None:
    if entry.get("acl"):
        _run(("/usr/bin/setfacl", "-m", ",".join(entry["acl"]), path))
    if entry.get("default_acl"):
        _run(("/usr/bin/setfacl", "-d", "-m", ",".join(entry["default_acl"]), path))
    if "mask" in entry:
        _run(("/usr/bin/setfacl", "-m", f"m::{entry['mask']}", path))


def _create_entry(
    entry: Mapping[str, Any],
    *,
    root: Path,
    rootless: bool,
    users: Mapping[str, int],
    groups: Mapping[str, int],
) -> None:
    path = rebase(entry["path"], root)
    kind = entry["type"]
    if entry.get("existing") == "keep":
        try:
            observed = os.lstat(path)
        except FileNotFoundError:
            os.mkdir(path, 0o700)
        else:
            if not stat.S_ISDIR(observed.st_mode):
                raise FixtureError(f"system directory {path} is not a directory")
            return
    else:
        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            raise FixtureError(f"fixture path already exists; the fixture needs a fresh host: {path}")
        if kind == "directory":
            os.mkdir(path, 0o700)
        elif kind == "socket":
            # A stale socket: the inode a stopped service leaves behind.
            os.mknod(path, 0o600 | stat.S_IFSOCK)
        elif kind == "fifo":
            os.mkfifo(path, 0o600)
        elif kind == "symlink":
            target = entry["target"]
            os.symlink(rebase(target, root) if target.startswith("/") else target, path)
        else:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                if kind == "sparse":
                    os.ftruncate(descriptor, int(entry["size"]))
                else:
                    payload = entry["content"].encode()
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    if kind != "symlink":
        os.chmod(path, int(entry["mode"], 8))
    _chown(path, entry["owner"], entry["group"], users, groups)
    if not rootless and kind == "directory":
        _setfacl(path, entry)


def _git_env(root: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "legacy fixture",
        "GIT_AUTHOR_EMAIL": "legacy-fixture@example.invalid",
        "GIT_COMMITTER_NAME": "legacy fixture",
        "GIT_COMMITTER_EMAIL": "legacy-fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2026-08-21T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-08-21T00:00:00Z",
    }


def _chown_tree(path: str, owner: str, group: str, users: Mapping[str, int], groups: Mapping[str, int]) -> None:
    if users[owner] < 0:
        return
    for current, directories, files in os.walk(path):
        _chown(current, owner, group, users, groups)
        for name in [*directories, *files]:
            _chown(os.path.join(current, name), owner, group, users, groups)


def _create_repository(
    repository: Mapping[str, Any],
    *,
    root: Path,
    users: Mapping[str, int],
    groups: Mapping[str, int],
) -> None:
    path = rebase(repository["path"], root)
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise FixtureError(f"fixture path already exists; the fixture needs a fresh host: {path}")
    env = _git_env(root)
    git = ("git", "-c", "init.defaultBranch=main")
    _run((*git, "init", "--quiet", path), env=env)
    for name, body in repository["files"].items():
        Path(path, name).write_text(body, encoding="utf-8")
    _run((*git, "-C", path, "add", "--all"), env=env)
    _run((*git, "-C", path, "commit", "--quiet", "--message", "legacy fixture checkout"), env=env)
    for branch in repository["branches"]:
        _run((*git, "-C", path, "branch", branch), env=env)
    _run((*git, "-C", path, "remote", "add", "origin", repository["origin"]), env=env)
    for worktree in repository["worktrees"]:
        target = rebase(worktree["path"], root)
        _run(
            (*git, "-C", path, "worktree", "add", "--quiet", "-b", worktree["branch"], target),
            env=env,
        )
        _chown_tree(target, worktree["owner"], worktree["group"], users, groups)
    os.chmod(path, int(repository["mode"], 8))
    _chown_tree(path, repository["owner"], repository["group"], users, groups)


def seed(manifest: Mapping[str, Any], *, root: Path, rootless: bool) -> None:
    """Lay out the fixture under ``root``.

    ``rootless=False`` is the container mode: it needs root, a container
    marker, and ``root`` must be ``/``.  ``rootless=True`` is for tests: any
    root except ``/``, no accounts, ownership or ACLs.
    """

    validate_manifest(manifest)
    root = Path(root)
    if rootless:
        if str(root) in {"/", ""} or not root.is_absolute():
            raise FixtureError("rootless seeding needs an absolute temporary root other than /")
    else:
        if os.geteuid() != 0:
            raise FixtureError("container seeding requires root")
        if not _container_markers():
            raise FixtureError(
                "refusing to seed a legacy host outside a disposable container "
                "(no container marker); the fixture must never touch a real host"
            )
        if str(root) != "/":
            raise FixtureError("container seeding lays the fixture out at /")
        _create_accounts(manifest)
    users, groups = _ids(manifest, rootless)
    repository_roots = [
        path
        for repository in manifest["repositories"]
        for path in (repository["path"], *(row["path"] for row in repository["worktrees"]))
    ]
    deferred = [
        entry
        for entry in manifest["entries"]
        if any(_within(entry["path"], path) for path in repository_roots)
    ]
    for entry in manifest["entries"]:
        if entry not in deferred:
            _create_entry(entry, root=root, rootless=rootless, users=users, groups=groups)
    for repository in manifest["repositories"]:
        _create_repository(repository, root=root, users=users, groups=groups)
    for entry in deferred:
        _create_entry(entry, root=root, rootless=rootless, users=users, groups=groups)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate the manifest and print its digest")
    seed_parser = sub.add_parser("seed", help="lay the fixture out (root in a container, or --rootless)")
    seed_parser.add_argument("--root", type=Path, default=Path("/"))
    seed_parser.add_argument("--rootless", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "check":
            print(json.dumps({"manifest_sha256": manifest_sha256(args.manifest)}))
            return 0
        seed(manifest, root=args.root, rootless=args.rootless)
    except (OSError, FixtureError) as exc:
        print(f"legacy fixture failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
