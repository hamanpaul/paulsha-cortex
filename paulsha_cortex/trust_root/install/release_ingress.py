"""Release ingress for `cortex upgrade` (#1263): runbook §1 as code.

Every expected value comes from GitHub Releases REST metadata (the annotated
tag's commit target and each asset ``digest``), never from downloaded bytes.
The qualification manifest is verified before the install-input archive is
opened, the archive topology before extraction, every bundle file before any
candidate code runs, and the candidate CLI is then sealed by a tree digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Protocol, Sequence

from .backend import _run
from .core import InstallError

OFFICIAL_REPOSITORY = "hamanpaul/paulsha-cortex"
GITHUB_API_ROOT = "https://api.github.com"
# Hosts each channel may be served from, over HTTPS on the default port only.
# REST metadata stays on the API host (a renamed repository redirects within
# it).  A release asset URL is github.com, which answers one 302 to
# release-assets.githubusercontent.com (checked 2026-10-06);
# objects.githubusercontent.com is the host it used before.  Redirects are
# refused before they are followed (#1270).
GITHUB_METADATA_HOSTS = frozenset({"api.github.com"})
GITHUB_ASSET_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_WHEEL_FILENAME = re.compile(r"paulsha_cortex-(?P<version>[^-]+)-py3-none-any\.whl")
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_METADATA_BYTES = 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_OPEN_NEW = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class IngressError(InstallError):
    """Release ingress refused metadata, an asset, a manifest or the input tree."""


def _hex(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def parse_version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise IngressError(f"version must be MAJOR.MINOR.PATCH: {value!r}")
    major, minor, patch = (int(part) for part in value.split("."))
    return major, minor, patch


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def wheel_version(wheel_path: object) -> tuple[int, int, int]:
    if not isinstance(wheel_path, str) or not wheel_path:
        raise IngressError("plan candidate wheel path is missing")
    match = _WHEEL_FILENAME.fullmatch(PurePosixPath(wheel_path).name)
    if match is None:
        raise IngressError(
            f"candidate wheel filename carries no release version: {wheel_path}"
        )
    return parse_version(match.group("version"))


def asset_names(version: str) -> tuple[str, str, str]:
    """Wheel, install-input archive and qualification manifest of one release."""

    return (
        f"paulsha_cortex-{version}-py3-none-any.whl",
        f"paulsha-cortex-{version}-install-input.tar.gz",
        f"paulsha-cortex-{version}-qualification.json",
    )


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    sha256: str
    size: int
    url: str


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    tag: str
    commit: str
    wheel: ReleaseAsset
    install_input: ReleaseAsset
    qualification: ReleaseAsset


class ReleaseFetcher(Protocol):
    def get_json(self, api_path: str) -> object: ...

    def copy_asset(self, asset: ReleaseAsset, destination: BinaryIO) -> None: ...


def _copy_limited(source: BinaryIO, destination: BinaryIO, asset: ReleaseAsset) -> None:
    remaining = asset.size
    while True:
        chunk = source.read(min(_CHUNK_BYTES, remaining + 1))
        if not chunk:
            break
        if len(chunk) > remaining:
            raise IngressError(f"release asset is larger than its metadata: {asset.name}")
        destination.write(chunk)
        remaining -= len(chunk)
    if remaining:
        raise IngressError(f"release asset is shorter than its metadata: {asset.name}")


def _https_on(url: object, hosts: frozenset[str]) -> bool:
    """``url`` is HTTPS on one of ``hosts``, default port, no userinfo."""

    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in hosts
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
    )


def _origin(url: object) -> str:
    """Scheme and host only: a signed asset URL's query string never reaches a message."""

    try:
        parts = urllib.parse.urlsplit(str(url))
        return f"{parts.scheme}://{parts.hostname or ''}"
    except ValueError:
        return "<unparseable URL>"


class _GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to HTTPS on ``hosts``; refuse it before connecting."""

    def __init__(self, hosts: frozenset[str]) -> None:
        super().__init__()
        self._hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not _https_on(newurl, self._hosts):
            try:
                fp.close()
            except Exception:  # noqa: BLE001 - the refusal below is what matters
                pass
            raise IngressError(
                "GitHub redirected outside "
                f"https://{{{', '.join(sorted(self._hosts))}}}: {_origin(newurl)}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _github_opener(
    hosts: frozenset[str], transport: Sequence[urllib.request.BaseHandler] = ()
) -> Callable[..., object]:
    return urllib.request.build_opener(_GitHubRedirectHandler(hosts), *transport).open


def _decode_metadata(raw: bytes, api_path: str) -> object:
    if len(raw) > _MAX_METADATA_BYTES:
        raise IngressError(f"GitHub REST metadata is too large: {api_path}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressError(f"GitHub REST metadata is not JSON: {api_path}") from exc


class GitHubReleaseFetcher:
    """Public GitHub REST over HTTPS; no token is ever sent.

    Each channel has its own urllib opener whose redirect handler follows only
    HTTPS redirects to that channel's GitHub hosts, and the final URL of every
    response is checked again.  ``urlopen`` replaces both openers outright
    (tests); ``transport`` adds urllib handlers to the real openers (tests swap
    HTTPS for an in-memory transport and keep the redirect handling real).
    """

    def __init__(
        self,
        repository: str = OFFICIAL_REPOSITORY,
        *,
        urlopen: Callable[..., object] | None = None,
        timeout: float = 60.0,
        transport: Sequence[urllib.request.BaseHandler] = (),
    ) -> None:
        self._repository = repository
        self._metadata_open = urlopen or _github_opener(GITHUB_METADATA_HOSTS, transport)
        self._asset_open = urlopen or _github_opener(GITHUB_ASSET_HOSTS, transport)
        self._timeout = timeout

    def get_json(self, api_path: str) -> object:
        request = urllib.request.Request(
            f"{GITHUB_API_ROOT}/{api_path}",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "paulsha-cortex-upgrade",
            },
        )
        try:
            with self._metadata_open(request, timeout=self._timeout) as response:  # type: ignore[attr-defined]
                if not _https_on(response.geturl(), GITHUB_METADATA_HOSTS):
                    raise IngressError(
                        f"GitHub REST response left HTTPS on {GITHUB_API_ROOT}: {api_path}"
                    )
                raw = response.read(_MAX_METADATA_BYTES + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise IngressError(f"GitHub REST request failed: {api_path}: {exc}") from exc
        return _decode_metadata(raw, api_path)

    def copy_asset(self, asset: ReleaseAsset, destination: BinaryIO) -> None:
        prefix = f"https://github.com/{self._repository}/releases/download/"
        if not asset.url.startswith(prefix) or not asset.url.endswith(f"/{asset.name}"):
            raise IngressError(
                f"release asset URL is outside {self._repository}: {asset.name}"
            )
        request = urllib.request.Request(
            asset.url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": "paulsha-cortex-upgrade",
            },
        )
        try:
            with self._asset_open(request, timeout=self._timeout) as response:  # type: ignore[attr-defined]
                if not _https_on(response.geturl(), GITHUB_ASSET_HOSTS):
                    raise IngressError(
                        "release asset download left HTTPS on the GitHub release hosts: "
                        f"{asset.name}"
                    )
                _copy_limited(response, destination, asset)
        except (urllib.error.URLError, OSError) as exc:
            raise IngressError(f"release asset download failed: {asset.name}: {exc}") from exc


class DirectoryReleaseFetcher:
    """GitHub-REST-shaped release source on local disk (RC qualification only).

    ``api/<api_path>.json`` holds the REST documents and ``assets/<name>`` the
    asset bytes, as written by ``qualification/release_source.py``.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _file(self, relative: PurePosixPath) -> Path:
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise IngressError(f"release source path is unsafe: {relative}")
        cursor = self._root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise IngressError(f"release source contains a symlink: {relative}")
        if not cursor.is_file():
            raise IngressError(f"release source lacks {relative}")
        return cursor

    def get_json(self, api_path: str) -> object:
        path = self._file(PurePosixPath("api") / f"{api_path}.json")
        return _decode_metadata(path.read_bytes(), api_path)

    def copy_asset(self, asset: ReleaseAsset, destination: BinaryIO) -> None:
        path = self._file(PurePosixPath("assets") / asset.name)
        with path.open("rb") as source:
            _copy_limited(source, destination, asset)


def resolve_release(
    fetcher: ReleaseFetcher, version: str, *, repository: str = OFFICIAL_REPOSITORY
) -> ReleaseMetadata:
    """Expected identity and digests of release ``v<version>``, from REST only."""

    parse_version(version)
    tag = f"v{version}"
    ref = fetcher.get_json(f"repos/{repository}/git/ref/tags/{tag}")
    ref_object = ref.get("object") if isinstance(ref, dict) else None
    if (
        not isinstance(ref_object, dict)
        or ref_object.get("type") != "tag"
        or _hex(ref_object.get("sha"), _SHA40) is None
    ):
        raise IngressError(f"{tag} is not an annotated tag")
    tag_document = fetcher.get_json(f"repos/{repository}/git/tags/{ref_object['sha']}")
    target = tag_document.get("object") if isinstance(tag_document, dict) else None
    if (
        not isinstance(tag_document, dict)
        or tag_document.get("tag") != tag
        or not isinstance(target, dict)
        or target.get("type") != "commit"
        or _hex(target.get("sha"), _SHA40) is None
    ):
        raise IngressError(f"{tag} does not point at a commit")
    release = fetcher.get_json(f"repos/{repository}/releases/tags/{tag}")
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        raise IngressError(f"release {tag} metadata is invalid")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise IngressError(f"{tag} is not a published final release")
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise IngressError(f"release {tag} lists no assets")
    names = asset_names(version)
    found: dict[str, ReleaseAsset] = {}
    for raw in raw_assets:
        if not isinstance(raw, dict) or raw.get("name") not in names:
            continue
        name = str(raw["name"])
        if name in found:
            raise IngressError(f"release lists asset {name} twice")
        digest = raw.get("digest")
        size = raw.get("size")
        url = raw.get("browser_download_url")
        if (
            not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or _hex(digest[len("sha256:"):], _SHA256) is None
        ):
            raise IngressError(f"release asset {name} has no sha256 digest")
        if type(size) is not int or size <= 0:
            raise IngressError(f"release asset {name} has no size")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise IngressError(f"release asset {name} has no HTTPS download URL")
        found[name] = ReleaseAsset(name, digest[len("sha256:"):], size, url)
    missing = [name for name in names if name not in found]
    if missing:
        raise IngressError("release lacks assets: " + ", ".join(missing))
    return ReleaseMetadata(
        version=version,
        tag=tag,
        commit=str(target["sha"]),
        wheel=found[names[0]],
        install_input=found[names[1]],
        qualification=found[names[2]],
    )


class _HashingWriter:
    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return self._stream.write(data)


def download_asset(fetcher: ReleaseFetcher, asset: ReleaseAsset, release_dir: Path) -> Path:
    """Write one asset into a fresh private file and check it against REST."""

    path = release_dir / asset.name
    descriptor = os.open(path, _OPEN_NEW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            writer = _HashingWriter(stream)
            fetcher.copy_asset(asset, writer)  # type: ignore[arg-type]
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if writer.digest.hexdigest() != asset.sha256:
        path.unlink()
        raise IngressError(
            f"release asset digest does not match its REST metadata: {asset.name}"
        )
    return path


def assert_private_chain(path: Path, *, owner_uid: int, stop: Path = Path("/")) -> None:
    """Every directory from ``path`` up to ``stop`` is owner-owned, unwritable by others.

    ``path`` must be absolute without ``..``: a relative walk would end at "."
    without checking the real ancestors (#1270).
    """

    if not path.is_absolute() or ".." in path.parts:
        raise IngressError(f"release ingress path must be absolute without '..': {path}")
    cursor = path
    while True:
        observed = cursor.lstat()
        if stat.S_ISLNK(observed.st_mode):
            raise IngressError(f"release ingress path contains a symlink: {cursor}")
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != owner_uid
            or stat.S_IMODE(observed.st_mode) & 0o022
        ):
            raise IngressError(
                f"release ingress directory is not owned by uid {owner_uid} "
                f"or is group/other writable: {cursor}"
            )
        if cursor == stop or cursor.parent == cursor:
            return
        cursor = cursor.parent


def make_attempt_dir(
    installer_root: Path,
    version: str,
    *,
    owner_uid: int,
    chain_stop: Path = Path("/"),
) -> Path:
    """A new attempt directory per run; a rerun of the same version never reuses one."""

    parse_version(version)
    for directory in (installer_root, installer_root / version):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    attempt = installer_root / version / f"attempt-{stamp}-{secrets.token_hex(4)}"
    os.mkdir(attempt, 0o700)
    os.mkdir(attempt / "release", 0o700)
    assert_private_chain(attempt / "release", owner_uid=owner_uid, stop=chain_stop)
    return attempt


# --- qualification manifest, archive and input tree (runbook §1) ---------------

_INPUT_ROOT_ENTRIES = frozenset(
    {"bundle.json", "install-config.yaml", "dist", "wheelhouse", "toolchain", "source"}
)
_BUNDLE_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "wheel",
        "wheelhouse",
        "generated_artifacts",
        "toolchain",
        "source_repositories",
    }
)
_TOOL_NAMES = frozenset({"codex", "claude", "copilot", "agy", "srt", "openspec"})
_VENV_ENV = {
    "HOME": "/root",
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONNOUSERSITE": "1",
}


@dataclass(frozen=True)
class QualificationAuthority:
    candidate_sha: str
    wheel_filename: str
    wheel_sha256: str
    bundle_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_qualification_authority(path: Path) -> QualificationAuthority:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressError("qualification manifest is not readable JSON") from exc
    wheel = document.get("wheel") if isinstance(document, dict) else None
    bundle = document.get("bundle") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 2
        or document.get("profile") != "release"
        or document.get("status") != "passed"
        or not isinstance(wheel, dict)
        or not isinstance(bundle, dict)
    ):
        raise IngressError("qualification manifest is not a passed release attestation")
    candidate_sha = _hex(document.get("candidate_sha"), _SHA40)
    filename = wheel.get("filename")
    wheel_sha = _hex(wheel.get("sha256"), _SHA256)
    bundle_sha = _hex(bundle.get("sha256"), _SHA256)
    if (
        candidate_sha is None
        or not isinstance(filename, str)
        or not filename
        or "/" in filename
        or wheel_sha is None
        or bundle_sha is None
    ):
        raise IngressError("qualification manifest identity is invalid")
    return QualificationAuthority(candidate_sha, filename, wheel_sha, bundle_sha)


# Generous bounds on the install-input archive (#1270).  The REST digest already
# pins the archive; these only bound a polluted release before extraction.
# v0.1.13's archive has 18 members and about 0.9 GB of file content.
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_BYTES = 8 * 1024**3


def check_archive_topology(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            seen: set[str] = set()
            total = 0
            for member in archive:
                if len(seen) >= _MAX_ARCHIVE_MEMBERS:
                    raise IngressError(
                        "install-input archive has too many members "
                        f"(limit {_MAX_ARCHIVE_MEMBERS})"
                    )
                if member.isfile():
                    total += member.size
                    if total > _MAX_ARCHIVE_BYTES:
                        raise IngressError(
                            "install-input archive exceeds the extracted size limit "
                            f"({_MAX_ARCHIVE_BYTES} bytes)"
                        )
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or pure.parts[0] != "qualification-input"
                    or ".." in pure.parts
                    or member.name in seen
                    or not (member.isdir() or member.isfile())
                ):
                    raise IngressError("install-input archive topology is unsafe")
                seen.add(member.name)
    except (OSError, tarfile.TarError) as exc:
        raise IngressError(f"install-input archive is unreadable: {exc}") from exc


def extract_install_input(archive: Path, input_root: Path) -> None:
    """Strip ``qualification-input/``; private modes, only the user-x bit survives."""

    try:
        with tarfile.open(archive, mode="r:gz") as stream:
            for member in stream.getmembers():
                parts = PurePosixPath(member.name).parts[1:]
                if not parts:
                    continue
                target = input_root.joinpath(*parts)
                if member.isdir():
                    os.mkdir(target, 0o700)
                    continue
                source = stream.extractfile(member)
                if source is None:
                    raise IngressError(f"install-input member is unreadable: {member.name}")
                mode = 0o700 if member.mode & 0o100 else 0o600
                descriptor = os.open(target, _OPEN_NEW, mode)
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := source.read(_CHUNK_BYTES):
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
    except (OSError, tarfile.TarError) as exc:
        raise IngressError(f"install-input extraction failed: {exc}") from exc


def _single_link_regular(path: Path, *, label: str) -> None:
    observed = path.lstat()
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise IngressError(f"{label} must be a single-link regular file")


def _plain_directory(path: Path, *, label: str) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise IngressError(f"{label} must be a real directory")


def _bundle_entry(raw: object, *, root: Path, label: str) -> str:
    if not isinstance(raw, dict) or set(raw) != {"path", "sha256"}:
        raise IngressError(f"{label} must contain only path and sha256")
    relative = raw["path"]
    if not isinstance(relative, str) or not relative:
        raise IngressError(f"{label}.path must be a non-empty string")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\x00" in relative:
        raise IngressError(f"{label}.path is unsafe")
    expected = _hex(raw["sha256"], _SHA256)
    if expected is None:
        raise IngressError(f"{label}.sha256 is invalid")
    path = root / pure
    _single_link_regular(path, label=f"{label}.path")
    cursor = root
    for part in pure.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise IngressError(f"{label}.path has a symlink ancestor")
    if _sha256_file(path) != expected:
        raise IngressError(f"{label}.sha256 does not match {relative}")
    return relative


def _directory_files(root: Path, directory: str, *, label: str) -> set[str]:
    found: set[str] = set()
    for path in (root / directory).iterdir():
        _single_link_regular(path, label=label)
        found.add(path.relative_to(root).as_posix())
    return found


def _validate_bundle_inventory(bundle: Path, *, candidate_sha: str, wheel_sha256: str) -> None:
    """Port of ``qualification/verify_bundle.py::validate_bundle`` plus runbook §1.

    ``verify_bundle.py`` runs stand-alone during RC qualification, before the
    candidate wheel is installed, so it cannot import this package: keep the
    two validators hand-in-sync on any change.  Both are equally strict and
    raise the same messages.  ``tests/test_trust_root_install_release_ingress.py``
    pins that branch by branch in
    ``test_bundle_port_refuses_every_branch_with_verify_bundles_message``; its
    case table is kept complete by
    ``test_bundle_parity_table_covers_every_refusal_message_of_verify_bundle``,
    and ``test_input_tree_validation_accepts_what_verify_bundle_accepts`` covers
    an accepted tree.
    """

    _single_link_regular(bundle, label="bundle")
    root = bundle.parent
    _plain_directory(root, label="qualification input root")
    if {path.name for path in root.iterdir()} != _INPUT_ROOT_ENTRIES:
        raise IngressError("qualification input root inventory is not exact")
    _single_link_regular(root / "install-config.yaml", label="install config")
    for directory in ("dist", "wheelhouse", "toolchain", "source"):
        _plain_directory(root / directory, label=f"{directory} root")
    try:
        payload = json.loads(bundle.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressError("bundle is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _BUNDLE_ROOT_KEYS:
        raise IngressError("bundle has missing or unknown root fields")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise IngressError("bundle schema_version must be 1")
    if payload["candidate_sha"] != candidate_sha or _hex(candidate_sha, _SHA40) is None:
        raise IngressError("bundle candidate_sha does not match")
    wheel_path = _bundle_entry(payload["wheel"], root=root, label="wheel")
    if payload["wheel"]["sha256"] != wheel_sha256:
        raise IngressError("bundle wheel sha256 does not match the selected candidate")
    if not wheel_path.startswith("dist/"):
        raise IngressError("bundle wheel must be under dist/")
    if _directory_files(root, "dist", label="dist entry") != {wheel_path}:
        raise IngressError("dist inventory must contain only the declared candidate wheel")
    wheelhouse = payload["wheelhouse"]
    if not isinstance(wheelhouse, list) or not wheelhouse:
        raise IngressError("bundle wheelhouse must be a non-empty array")
    if any(
        not isinstance(raw, dict)
        or not isinstance(raw.get("path"), str)
        or not raw["path"].endswith(".whl")
        or PurePosixPath(raw["path"]).parent != PurePosixPath("wheelhouse")
        for raw in wheelhouse
    ):
        raise IngressError("bundle wheelhouse must list wheels directly under wheelhouse/")
    declared = [
        _bundle_entry(raw, root=root, label=f"wheelhouse[{index}]")
        for index, raw in enumerate(wheelhouse)
    ]
    if len(set(declared)) != len(declared):
        raise IngressError("wheelhouse manifest paths are duplicated")
    if set(declared) != _directory_files(root, "wheelhouse", label="wheelhouse entry"):
        raise IngressError("wheelhouse inventory is incomplete or contains an undeclared file")
    if not any(raw.get("sha256") == wheel_sha256 for raw in wheelhouse):
        raise IngressError("wheelhouse does not contain the exact candidate wheel")
    generated = payload["generated_artifacts"]
    if not isinstance(generated, list):
        raise IngressError("generated_artifacts must be an array")
    generated_paths = [
        _bundle_entry(raw, root=root, label=f"generated_artifacts[{index}]")
        for index, raw in enumerate(generated)
    ]
    if len(set(generated_paths)) != len(generated_paths):
        raise IngressError("bundle contains duplicate generated artifact paths")
    tools = payload["toolchain"]
    if not isinstance(tools, list) or not tools:
        raise IngressError("toolchain must be a non-empty array")
    tool_names: set[str] = set()
    tool_paths: set[str] = set()
    for index, raw in enumerate(tools):
        if not isinstance(raw, dict):
            raise IngressError(f"toolchain[{index}] must be an object")
        required = {"name", "version", "shape", "path", "sha256"}
        if raw.get("shape") == "tree":
            required |= {"entrypoint", "installed_sha256"}
        if set(raw) != required:
            raise IngressError(f"toolchain[{index}] has missing or unknown fields")
        name = raw.get("name")
        version = raw.get("version")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or name in tool_names
            or not isinstance(version, str)
            or not version
            or raw.get("shape") not in {"file", "tree"}
        ):
            raise IngressError(f"toolchain[{index}] identity is invalid")
        if raw.get("shape") == "tree":
            entrypoint = raw.get("entrypoint")
            if (
                not isinstance(entrypoint, str)
                or PurePosixPath(entrypoint).is_absolute()
                or ".." in PurePosixPath(entrypoint).parts
                or _hex(raw.get("installed_sha256"), _SHA256) is None
            ):
                raise IngressError(f"toolchain[{index}] tree metadata is invalid")
        tool_names.add(name)
        tool_paths.add(
            _bundle_entry(
                {"path": raw["path"], "sha256": raw["sha256"]},
                root=root,
                label=f"toolchain[{index}]",
            )
        )
    if tool_names != _TOOL_NAMES:
        raise IngressError("toolchain inventory is incomplete")
    if tool_paths != _directory_files(root, "toolchain", label="toolchain entry"):
        raise IngressError("toolchain directory has undeclared or missing artifacts")
    repositories = payload["source_repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise IngressError("source_repositories must be a non-empty array")
    repository_paths: set[str] = set()
    slugs: set[str] = set()
    for index, raw in enumerate(repositories):
        if not isinstance(raw, dict) or set(raw) != {
            "slug",
            "commit",
            "remote",
            "path",
            "sha256",
        }:
            raise IngressError(f"source_repositories[{index}] fields are invalid")
        slug = raw.get("slug")
        if (
            not isinstance(slug, str)
            or not slug
            or "/" in slug
            or slug in slugs
            or _hex(raw.get("commit"), _SHA40) is None
            or not isinstance(raw.get("remote"), str)
            or not raw["remote"].startswith("https://")
        ):
            raise IngressError(f"source_repositories[{index}] identity is invalid")
        slugs.add(slug)
        repository_paths.add(
            _bundle_entry(
                {"path": raw["path"], "sha256": raw["sha256"]},
                root=root,
                label=f"source_repositories[{index}]",
            )
        )
    if repository_paths != _directory_files(root, "source", label="source entry"):
        raise IngressError("source directory has undeclared or missing artifacts")


def verify_input_tree(
    bundle: Path, *, candidate_sha: str, wheel_sha256: str, owner_uid: int
) -> None:
    root = bundle.parent
    try:
        for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
            observed = path.lstat()
            if stat.S_ISLNK(observed.st_mode):
                raise IngressError("qualification input contains a symlink")
            if observed.st_uid != owner_uid or stat.S_IMODE(observed.st_mode) & 0o022:
                raise IngressError("qualification input ownership/mode is unsafe")
            if stat.S_ISDIR(observed.st_mode):
                continue
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise IngressError("qualification input contains an unsafe object")
        _validate_bundle_inventory(
            bundle, candidate_sha=candidate_sha, wheel_sha256=wheel_sha256
        )
    except OSError as exc:
        raise IngressError(f"qualification input is incomplete: {exc}") from exc


def write_bootstrap_requirements(bundle: Path, requirements: Path) -> None:
    root = bundle.parent
    if os.path.lexists(requirements) or requirements.parent != root.parent:
        raise IngressError("bootstrap requirements path is unsafe")
    document = json.loads(bundle.read_text(encoding="utf-8"))
    rows = sorted(document["wheelhouse"], key=lambda item: item["path"])
    descriptor = os.open(requirements, _OPEN_NEW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        for row in rows:
            uri = (root / PurePosixPath(row["path"])).as_uri()
            stream.write(f"{uri} --hash=sha256:{row['sha256']}\n")


def _open_for_readers(root: Path) -> None:
    """``chmod -R u=rwX,go=rX`` that refuses symlinks instead of following them."""

    for path in [root, *root.rglob("*")]:
        observed = path.lstat()
        if stat.S_ISLNK(observed.st_mode):
            raise IngressError(f"unexpected symlink: {path}")
        executable = stat.S_ISDIR(observed.st_mode) or bool(
            stat.S_IMODE(observed.st_mode) & 0o111
        )
        os.chmod(path, 0o755 if executable else 0o644)


def tree_sha256(root: Path, *, owner_uid: int) -> str:
    """Deterministic digest of relative path, mode and content (runbook §1)."""

    digest = hashlib.sha256()
    paths = [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        observed = path.lstat()
        if observed.st_uid != owner_uid or stat.S_IMODE(observed.st_mode) & 0o022:
            raise IngressError("unsafe candidate CLI ownership/mode")
        digest.update(relative.encode() + b"\0")
        digest.update(format(stat.S_IMODE(observed.st_mode), "04o").encode() + b"\0")
        if stat.S_ISDIR(observed.st_mode):
            digest.update(b"D\0")
        elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
            digest.update(b"F\0" + hashlib.sha256(path.read_bytes()).digest())
        else:
            raise IngressError("unsafe candidate CLI tree object")
    return digest.hexdigest()


def build_sealed_venv(
    venv: Path,
    requirements: Path,
    *,
    owner_uid: int,
    run: Callable[..., object] = _run,
) -> Path:
    if os.path.lexists(venv):
        raise IngressError(f"sealed venv path already exists: {venv}")
    run(
        ("/usr/bin/python3", "-I", "-S", "-m", "venv", "--copies", str(venv)),
        check=True,
        env=dict(_VENV_ENV),
    )
    run(
        (
            str(venv / "bin" / "python"),
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--only-binary=:all:",
            "--require-hashes",
            "--requirement",
            str(requirements),
        ),
        check=True,
        env={**_VENV_ENV, "PATH": f"{venv}/bin:/usr/bin:/bin"},
    )
    lib64 = venv / "lib64"
    if lib64.is_symlink():
        if os.readlink(lib64) != "lib":
            raise IngressError("sealed venv lib64 link does not point at lib")
        lib64.unlink()
    _open_for_readers(venv)
    cli = venv / "bin" / "cortex"
    if cli.is_symlink() or not cli.is_file() or not os.access(cli, os.X_OK):
        raise IngressError("sealed venv has no executable cortex CLI")
    tree_sha256(venv, owner_uid=owner_uid)
    return cli


@dataclass(frozen=True)
class SealedCandidate:
    metadata: ReleaseMetadata
    attempt_dir: Path
    input_root: Path
    bundle: Path
    install_config: Path
    venv: Path
    cli: Path
    tree_sha256: str
    owner_uid: int

    def assert_unchanged(self) -> None:
        if tree_sha256(self.venv, owner_uid=self.owner_uid) != self.tree_sha256:
            raise IngressError("sealed candidate CLI changed since ingress")


def ingest_release(
    version: str,
    *,
    fetcher: ReleaseFetcher,
    installer_root: Path,
    repository: str = OFFICIAL_REPOSITORY,
    owner_uid: int = 0,
    chain_stop: Path = Path("/"),
    run: Callable[..., object] = _run,
) -> SealedCandidate:
    """Runbook §1 end to end: nothing from the release runs before this returns."""

    metadata = resolve_release(fetcher, version, repository=repository)
    attempt = make_attempt_dir(
        installer_root, version, owner_uid=owner_uid, chain_stop=chain_stop
    )
    release_dir = attempt / "release"
    archive = download_asset(fetcher, metadata.install_input, release_dir)
    manifest = download_asset(fetcher, metadata.qualification, release_dir)
    for path in (archive, manifest):
        observed = path.lstat()
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or observed.st_uid != owner_uid
            or stat.S_IMODE(observed.st_mode) & 0o022
        ):
            raise IngressError(f"downloaded release asset is not private: {path.name}")
    authority = read_qualification_authority(manifest)
    if authority.candidate_sha != metadata.commit:
        raise IngressError("qualification manifest candidate does not match the tag target")
    if authority.wheel_filename != metadata.wheel.name:
        raise IngressError("qualification manifest wheel is not the release wheel asset")
    if authority.wheel_sha256 != metadata.wheel.sha256:
        raise IngressError("qualification manifest wheel digest is not the release wheel digest")
    check_archive_topology(archive)
    input_root = attempt / "input"
    os.mkdir(input_root, 0o700)
    extract_install_input(archive, input_root)
    bundle = input_root / "bundle.json"
    if _sha256_file(bundle) != authority.bundle_sha256:
        raise IngressError("bundle.json does not match the qualification manifest")
    verify_input_tree(
        bundle,
        candidate_sha=authority.candidate_sha,
        wheel_sha256=authority.wheel_sha256,
        owner_uid=owner_uid,
    )
    requirements = attempt / "bootstrap-requirements.txt"
    write_bootstrap_requirements(bundle, requirements)
    # The input is not a credential surface: once verified, the unprivileged
    # plan identity may traverse and read it (runbook §1).
    for directory in (installer_root, installer_root / version, attempt):
        os.chmod(directory, 0o755)
    _open_for_readers(input_root)
    venv = attempt / "venv"
    cli = build_sealed_venv(venv, requirements, owner_uid=owner_uid, run=run)
    return SealedCandidate(
        metadata=metadata,
        attempt_dir=attempt,
        input_root=input_root,
        bundle=bundle,
        install_config=input_root / "install-config.yaml",
        venv=venv,
        cli=cli,
        tree_sha256=tree_sha256(venv, owner_uid=owner_uid),
        owner_uid=owner_uid,
    )
