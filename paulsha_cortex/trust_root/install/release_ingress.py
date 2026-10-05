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
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Protocol

from .backend import _run
from .core import InstallError

OFFICIAL_REPOSITORY = "hamanpaul/paulsha-cortex"
GITHUB_API_ROOT = "https://api.github.com"
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


def _decode_metadata(raw: bytes, api_path: str) -> object:
    if len(raw) > _MAX_METADATA_BYTES:
        raise IngressError(f"GitHub REST metadata is too large: {api_path}")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IngressError(f"GitHub REST metadata is not JSON: {api_path}") from exc


class GitHubReleaseFetcher:
    """Public GitHub REST over HTTPS; no token is ever sent."""

    def __init__(
        self,
        repository: str = OFFICIAL_REPOSITORY,
        *,
        urlopen: Callable[..., object] = urllib.request.urlopen,
        timeout: float = 60.0,
    ) -> None:
        self._repository = repository
        self._urlopen = urlopen
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
            with self._urlopen(request, timeout=self._timeout) as response:  # type: ignore[attr-defined]
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
            with self._urlopen(request, timeout=self._timeout) as response:  # type: ignore[attr-defined]
                if not str(response.geturl()).startswith("https://"):
                    raise IngressError(f"release asset download left HTTPS: {asset.name}")
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
    """Every directory from ``path`` up to ``stop`` is owner-owned, unwritable by others."""

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
