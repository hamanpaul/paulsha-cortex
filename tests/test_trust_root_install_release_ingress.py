"""#1263：release ingress 逐項移植 runbook §1（REST metadata、asset、目錄與 fetcher）。"""
from __future__ import annotations

import email.message
import io
import json
import os
import shutil
import stat
import tarfile
import urllib.request
import urllib.response
from pathlib import Path

import pytest

import release_ingress_fixtures as fixtures
from paulsha_cortex.trust_root.install import release_ingress
from paulsha_cortex.trust_root.install.release_ingress import (
    DirectoryReleaseFetcher,
    GitHubReleaseFetcher,
    IngressError,
    ReleaseAsset,
    asset_names,
    assert_private_chain,
    build_sealed_venv,
    check_archive_topology,
    download_asset,
    format_version,
    ingest_release,
    make_attempt_dir,
    parse_version,
    read_qualification_authority,
    resolve_release,
    tree_sha256,
    verify_input_tree,
    wheel_version,
)
from qualification.verify_bundle import validate_bundle as qualification_validate_bundle

API = "api/repos/hamanpaul/paulsha-cortex"


@pytest.mark.parametrize("value", ["0.1.13", "1.0.0", "10.20.30"])
def test_parse_version_accepts_major_minor_patch(value: str) -> None:
    assert format_version(parse_version(value)) == value


@pytest.mark.parametrize(
    "value", ["0.1", "0.1.13rc1", "01.2.3", "v0.1.13", "0.1.13.dev0", "", None]
)
def test_parse_version_rejects_everything_else(value: object) -> None:
    with pytest.raises(IngressError, match="MAJOR.MINOR.PATCH"):
        parse_version(value)


def test_versions_compare_numerically_and_come_from_the_wheel_filename() -> None:
    assert parse_version("0.1.13") > parse_version("0.1.9")
    assert wheel_version("dist/paulsha_cortex-0.1.13-py3-none-any.whl") == (0, 1, 13)
    with pytest.raises(IngressError, match="no release version"):
        wheel_version("dist/other-0.1.13-py3-none-any.whl")


def test_release_source_lays_out_github_rest_shapes(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)

    assert set(release.assets) == set(asset_names(fixtures.VERSION))
    metadata = json.loads(fixtures.release_json_path(release.source).read_text())
    assert metadata["draft"] is False
    assert metadata["prerelease"] is False
    assert {row["name"]: row["digest"] for row in metadata["assets"]} == {
        name: f"sha256:{digest}" for name, digest in release.assets.items()
    }


def test_resolve_release_takes_expected_values_from_rest_metadata(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)

    metadata = resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)

    wheel_name, input_name, qualification_name = asset_names(fixtures.VERSION)
    assert metadata.tag == "v0.1.13"
    assert metadata.commit == fixtures.CANDIDATE_SHA
    assert metadata.wheel.name == wheel_name
    assert metadata.wheel.sha256 == release.tree.wheel_sha256
    assert metadata.install_input.sha256 == release.assets[input_name]
    assert metadata.qualification.sha256 == release.assets[qualification_name]


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_resolve_release_refuses_a_release_that_is_not_final(
    tmp_path: Path, field: str
) -> None:
    release = fixtures.write_release_source(tmp_path)
    path = fixtures.release_json_path(release.source)
    document = json.loads(path.read_text())
    document[field] = True
    path.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="not a published final release"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_resolve_release_refuses_a_lightweight_tag(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    ref = release.source / API / "git/ref/tags/v0.1.13.json"
    document = json.loads(ref.read_text())
    document["object"]["type"] = "commit"
    ref.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="not an annotated tag"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_resolve_release_refuses_a_missing_asset(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    path = fixtures.release_json_path(release.source)
    document = json.loads(path.read_text())
    document["assets"] = [
        row for row in document["assets"] if not row["name"].endswith("-qualification.json")
    ]
    path.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="release lacks assets"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def _edit_json(path: Path, mutate) -> None:
    document = json.loads(path.read_text())
    mutate(document)
    path.write_text(json.dumps(document))


def test_resolve_release_refuses_a_tag_object_named_for_another_tag(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    tag_document = next((release.source / API / "git/tags").iterdir())
    _edit_json(tag_document, lambda document: document.update(tag="v0.1.12"))

    with pytest.raises(IngressError, match="v0.1.13 does not point at a commit"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_resolve_release_refuses_release_metadata_for_another_tag(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    _edit_json(
        fixtures.release_json_path(release.source),
        lambda document: document.update(tag_name="v0.1.12"),
    )

    with pytest.raises(IngressError, match="release v0.1.13 metadata is invalid"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_resolve_release_refuses_an_asset_listed_twice(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    _edit_json(
        fixtures.release_json_path(release.source),
        lambda document: document["assets"].append(dict(document["assets"][0])),
    )

    with pytest.raises(IngressError, match="lists asset .* twice"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("digest", None, "has no sha256 digest"),
        ("digest", "sha1:" + "0" * 40, "has no sha256 digest"),
        ("digest", "sha256:" + "0" * 63, "has no sha256 digest"),
        ("digest", "sha256:" + "A" * 64, "has no sha256 digest"),
        ("size", 0, "has no size"),
        ("size", True, "has no size"),
        ("size", "2762", "has no size"),
        ("browser_download_url", None, "has no HTTPS download URL"),
        (
            "browser_download_url",
            "http://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/x",
            "has no HTTPS download URL",
        ),
    ],
)
def test_resolve_release_refuses_a_malformed_asset_row(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    release = fixtures.write_release_source(tmp_path)
    _wheel, _input, qualification_name = asset_names(fixtures.VERSION)

    def corrupt(document: dict) -> None:
        for row in document["assets"]:
            if row["name"] == qualification_name:
                row[field] = value

    _edit_json(fixtures.release_json_path(release.source), corrupt)

    with pytest.raises(IngressError, match=f"{qualification_name} {message}"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_release_metadata_larger_than_one_mib_is_refused(tmp_path: Path) -> None:
    oversized = b'{"pad": "' + b"a" * (1024 * 1024) + b'"}'
    fetcher = GitHubReleaseFetcher(
        urlopen=lambda request, timeout: _Response(oversized, request.full_url)
    )

    with pytest.raises(IngressError, match="metadata is too large"):
        fetcher.get_json("repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13")

    release = fixtures.write_release_source(tmp_path)
    fixtures.release_json_path(release.source).write_bytes(oversized)
    with pytest.raises(IngressError, match="metadata is too large"):
        resolve_release(DirectoryReleaseFetcher(release.source), fixtures.VERSION)


def test_download_stops_when_bytes_differ_from_the_rest_digest(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    fetcher = DirectoryReleaseFetcher(release.source)
    metadata = resolve_release(fetcher, fixtures.VERSION)
    tampered = release.source / "assets" / metadata.qualification.name
    original = tampered.read_bytes()
    tampered.write_bytes(original[:-2] + b"X\n")
    target = tmp_path / "release"
    target.mkdir()

    with pytest.raises(IngressError, match="does not match its REST metadata"):
        download_asset(fetcher, metadata.qualification, target)
    assert list(target.iterdir()) == []


def test_download_refuses_more_bytes_than_the_metadata_size(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "assets").mkdir(parents=True)
    (source / "assets" / "big.bin").write_bytes(b"abcd")
    asset = ReleaseAsset(
        "big.bin",
        "0" * 64,
        3,
        "https://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/big.bin",
    )
    target = tmp_path / "release"
    target.mkdir()

    with pytest.raises(IngressError, match="larger than its metadata"):
        download_asset(DirectoryReleaseFetcher(source), asset, target)
    assert not (target / "big.bin").exists()


def test_download_refuses_fewer_bytes_than_the_metadata_size(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "assets").mkdir(parents=True)
    (source / "assets" / "short.bin").write_bytes(b"abcd")
    asset = ReleaseAsset(
        "short.bin",
        "0" * 64,
        5,
        "https://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/short.bin",
    )
    target = tmp_path / "release"
    target.mkdir()

    with pytest.raises(IngressError, match="shorter than its metadata"):
        download_asset(DirectoryReleaseFetcher(source), asset, target)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_download_never_reuses_or_follows_an_existing_asset_path(
    tmp_path: Path, existing: str
) -> None:
    # O_CREAT|O_EXCL|O_NOFOLLOW: a file or symlink already at the asset path is
    # neither overwritten nor followed.
    release = fixtures.write_release_source(tmp_path)
    fetcher = DirectoryReleaseFetcher(release.source)
    metadata = resolve_release(fetcher, fixtures.VERSION)
    target = tmp_path / "release"
    target.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("untouched\n", encoding="utf-8")
    path = target / metadata.qualification.name
    if existing == "file":
        path.write_text("earlier\n", encoding="utf-8")
    else:
        path.symlink_to(outside)

    with pytest.raises(FileExistsError):
        download_asset(fetcher, metadata.qualification, target)
    assert outside.read_text(encoding="utf-8") == "untouched\n"
    if existing == "file":
        assert path.read_text(encoding="utf-8") == "earlier\n"
    else:
        assert path.is_symlink()


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, url: str) -> None:
        super().__init__(payload)
        self._url = url

    def geturl(self) -> str:
        return self._url


def test_github_fetcher_uses_the_public_rest_api_without_a_token() -> None:
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return _Response(b'{"ok": true}', request.full_url)

    fetcher = GitHubReleaseFetcher(urlopen=urlopen)

    assert fetcher.get_json("repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13") == {
        "ok": True
    }
    assert seen[0].full_url == (
        "https://api.github.com/repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13"
    )
    assert seen[0].get_header("Accept") == "application/vnd.github+json"
    assert seen[0].get_header("Authorization") is None


def test_github_fetcher_downloads_an_asset_with_its_headers_and_timeout() -> None:
    seen: list[tuple[object, float]] = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        return _Response(b"abc", request.full_url)

    url = "https://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/x.whl"
    destination = io.BytesIO()

    GitHubReleaseFetcher(urlopen=urlopen, timeout=12.5).copy_asset(
        ReleaseAsset("x.whl", "0" * 64, 3, url), destination
    )

    assert destination.getvalue() == b"abc"
    ((request, timeout),) = seen
    assert timeout == 12.5
    assert request.full_url == url
    assert request.get_header("Accept") == "application/octet-stream"
    assert request.get_header("User-agent") == "paulsha-cortex-upgrade"
    assert request.get_header("Authorization") is None


def test_github_fetcher_sends_metadata_requests_with_its_timeout() -> None:
    timeouts: list[float] = []

    def urlopen(request, timeout):
        timeouts.append(timeout)
        return _Response(b"{}", request.full_url)

    GitHubReleaseFetcher(urlopen=urlopen, timeout=7.0).get_json("repos/x/y/releases/tags/v1.0.0")

    assert timeouts == [7.0]


def test_github_fetcher_refuses_asset_urls_outside_the_release() -> None:
    def urlopen(_request, timeout):
        raise AssertionError("must not download")

    asset = ReleaseAsset("x.whl", "0" * 64, 1, "https://example.invalid/x.whl")

    with pytest.raises(IngressError, match="outside hamanpaul/paulsha-cortex"):
        GitHubReleaseFetcher(urlopen=urlopen).copy_asset(asset, io.BytesIO())


def test_github_fetcher_refuses_a_download_that_leaves_https() -> None:
    url = "https://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/x.whl"
    fetcher = GitHubReleaseFetcher(
        urlopen=lambda request, timeout: _Response(b"x", "http://objects.invalid/x.whl")
    )

    with pytest.raises(IngressError, match="left HTTPS"):
        fetcher.copy_asset(ReleaseAsset("x.whl", "0" * 64, 1, url), io.BytesIO())


class _Transport(urllib.request.HTTPSHandler):
    """In-memory HTTPS for the real urllib opener: an optional first 302, then bytes."""

    def __init__(self, location: str | None, body: bytes = b"x") -> None:
        super().__init__()
        self.location = location
        self.body = body
        self.urls: list[str] = []

    def https_open(self, request):
        self.urls.append(request.full_url)
        headers = email.message.Message()
        if self.location is not None and len(self.urls) == 1:
            headers["Location"] = self.location
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
        else:
            response = urllib.response.addinfourl(
                io.BytesIO(self.body), headers, request.full_url, 200
            )
            response.msg = "OK"
        return response


_ASSET_URL = "https://github.com/hamanpaul/paulsha-cortex/releases/download/v0.1.13/x.whl"


def _asset(body: bytes = b"x") -> ReleaseAsset:
    return ReleaseAsset("x.whl", "0" * 64, len(body), _ASSET_URL)


def test_github_asset_download_follows_the_release_assets_redirect() -> None:
    # How github.com serves a release asset today (checked 2026-10-06): one 302
    # to release-assets.githubusercontent.com with a signed query string.
    target = (
        "https://release-assets.githubusercontent.com/github-production-release-asset/"
        "1/2?sp=r&sig=x"
    )
    transport = _Transport(target, body=b"wheel")
    destination = io.BytesIO()

    GitHubReleaseFetcher(transport=(transport,)).copy_asset(_asset(b"wheel"), destination)

    assert transport.urls == [_ASSET_URL, target]
    assert destination.getvalue() == b"wheel"


@pytest.mark.parametrize(
    "location",
    [
        "http://release-assets.githubusercontent.com/github-production-release-asset/1/2",
        "https://evil.example/github-production-release-asset/1/2",
        "https://release-assets.githubusercontent.com.evil.example/x",
        "https://user@release-assets.githubusercontent.com/x",
        "https://release-assets.githubusercontent.com:8443/x",
    ],
)
def test_github_asset_download_refuses_a_redirect_off_the_release_hosts(location: str) -> None:
    transport = _Transport(location)

    with pytest.raises(IngressError, match="redirected outside"):
        GitHubReleaseFetcher(transport=(transport,)).copy_asset(_asset(), io.BytesIO())
    assert transport.urls == [_ASSET_URL]


def test_github_metadata_follows_redirects_only_within_the_rest_api() -> None:
    moved = "https://api.github.com/repositories/1/releases/tags/v0.1.13"
    transport = _Transport(moved, body=b'{"ok": true}')

    fetcher = GitHubReleaseFetcher(transport=(transport,))
    assert fetcher.get_json("repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13") == {
        "ok": True
    }
    assert transport.urls[-1] == moved

    elsewhere = _Transport("https://github.com/hamanpaul/paulsha-cortex/releases/tag/v0.1.13")
    with pytest.raises(IngressError, match="redirected outside"):
        GitHubReleaseFetcher(transport=(elsewhere,)).get_json(
            "repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13"
        )


def test_github_metadata_refuses_a_response_from_outside_the_rest_api() -> None:
    fetcher = GitHubReleaseFetcher(
        urlopen=lambda request, timeout: _Response(b"{}", "https://evil.example/api")
    )

    with pytest.raises(IngressError, match="left HTTPS"):
        fetcher.get_json("repos/hamanpaul/paulsha-cortex/releases/tags/v0.1.13")


def test_github_asset_download_refuses_a_final_url_off_the_release_hosts() -> None:
    fetcher = GitHubReleaseFetcher(
        urlopen=lambda request, timeout: _Response(b"x", "https://evil.example/x.whl")
    )

    with pytest.raises(IngressError, match="left HTTPS"):
        fetcher.copy_asset(_asset(), io.BytesIO())


def test_private_chain_refuses_a_relative_path(tmp_path: Path) -> None:
    # #1270: a relative path ends at "." without checking the real ancestors.
    with pytest.raises(IngressError, match="absolute"):
        assert_private_chain(Path("installer/0.1.13"), owner_uid=os.getuid(), stop=tmp_path)
    with pytest.raises(IngressError, match="absolute"):
        assert_private_chain(tmp_path / ".." / tmp_path.name, owner_uid=os.getuid())


def test_private_chain_accepts_owner_only_directories(tmp_path: Path) -> None:
    target = tmp_path / "installer" / "0.1.13"
    target.mkdir(parents=True)
    os.chmod(tmp_path / "installer", 0o755)
    os.chmod(target, 0o700)

    assert assert_private_chain(target, owner_uid=os.getuid(), stop=tmp_path) is None


def test_private_chain_refuses_a_group_writable_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "installer" / "0.1.13"
    target.mkdir(parents=True)
    os.chmod(tmp_path / "installer", 0o775)

    with pytest.raises(IngressError, match="group/other writable"):
        assert_private_chain(target, owner_uid=os.getuid(), stop=tmp_path)


def test_private_chain_refuses_a_symlinked_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "0.1.13").mkdir(parents=True)
    # Explicit modes: under umask 0002 the directory would be group writable and
    # refused for that before the walk ever reached the symlink (#1270).
    real.chmod(0o755)
    (real / "0.1.13").chmod(0o755)
    (tmp_path / "installer").symlink_to(real)

    with pytest.raises(IngressError, match="symlink"):
        assert_private_chain(
            tmp_path / "installer" / "0.1.13", owner_uid=os.getuid(), stop=tmp_path
        )


def test_each_ingress_attempt_gets_a_fresh_private_directory(tmp_path: Path) -> None:
    installer = tmp_path / "installer"

    first = make_attempt_dir(installer, "0.1.13", owner_uid=os.getuid(), chain_stop=tmp_path)
    second = make_attempt_dir(installer, "0.1.13", owner_uid=os.getuid(), chain_stop=tmp_path)

    assert first != second
    assert first.parent == second.parent == installer / "0.1.13"
    assert first.name.startswith("attempt-")
    assert stat.S_IMODE((second / "release").stat().st_mode) == 0o700


def _ingest(tmp_path: Path, release, calls=None):
    return ingest_release(
        fixtures.VERSION,
        fetcher=DirectoryReleaseFetcher(release.source),
        installer_root=tmp_path / "installer",
        owner_uid=os.getuid(),
        chain_stop=tmp_path,
        run=fixtures.fake_venv_runner(calls if calls is not None else []),
    )


def test_ingest_seals_the_candidate_cli_after_every_check(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    calls: list = []

    sealed = _ingest(tmp_path, release, calls)

    _wheel, input_name, qualification_name = asset_names(fixtures.VERSION)
    assert sorted(path.name for path in (sealed.attempt_dir / "release").iterdir()) == sorted(
        [input_name, qualification_name]
    )
    assert sealed.metadata.commit == fixtures.CANDIDATE_SHA
    assert sealed.cli == sealed.venv / "bin" / "cortex"
    assert sealed.bundle == sealed.input_root / "bundle.json"
    assert sealed.install_config == sealed.input_root / "install-config.yaml"
    assert sealed.tree_sha256 == tree_sha256(sealed.venv, owner_uid=os.getuid())
    requirements = (sealed.attempt_dir / "bootstrap-requirements.txt").read_text().splitlines()
    assert requirements == sorted(requirements)
    assert all(
        line.startswith("file://") and " --hash=sha256:" in line for line in requirements
    )
    assert stat.S_IMODE((sealed.input_root / "toolchain" / "codex").stat().st_mode) == 0o755
    assert stat.S_IMODE((sealed.input_root / "bundle.json").stat().st_mode) == 0o644
    assert calls[0][0][:6] == ("/usr/bin/python3", "-I", "-S", "-m", "venv", "--copies")
    pip = next(argv for argv, _env in calls if "pip" in argv)
    for flag in ("--no-index", "--no-deps", "--only-binary=:all:", "--require-hashes"):
        assert flag in pip


def test_sealed_candidate_detects_a_changed_cli_tree(tmp_path: Path) -> None:
    sealed = _ingest(tmp_path, fixtures.write_release_source(tmp_path))

    sealed.assert_unchanged()
    (sealed.venv / "bin" / "cortex").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(IngressError, match="changed since ingress"):
        sealed.assert_unchanged()


def test_rerunning_the_same_version_ingests_into_a_new_attempt(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)

    first = _ingest(tmp_path, release)
    second = _ingest(tmp_path, release)

    assert first.attempt_dir != second.attempt_dir
    assert second.tree_sha256 == tree_sha256(second.venv, owner_uid=os.getuid())


def test_manifest_must_name_the_tag_target_before_extraction(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    tag_document = next((release.source / API / "git/tags").iterdir())
    document = json.loads(tag_document.read_text())
    document["object"]["sha"] = "f" * 40
    tag_document.write_text(json.dumps(document))

    with pytest.raises(IngressError, match="manifest candidate does not match the tag target"):
        _ingest(tmp_path, release)
    attempts = list((tmp_path / "installer" / fixtures.VERSION).iterdir())
    assert attempts and all(not (attempt / "input").exists() for attempt in attempts)


def test_bundle_must_match_the_manifest_digest(tmp_path: Path) -> None:
    release = fixtures.write_release_source(tmp_path)
    fixtures.rewrite_manifest(release, bundle_sha256="0" * 64)

    with pytest.raises(IngressError, match="bundle.json does not match"):
        _ingest(tmp_path, release)


def test_manifest_must_be_a_passed_release_attestation(tmp_path: Path) -> None:
    path = tmp_path / "qualification.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": "deployment-canary",
                "status": "passed",
                "candidate_sha": "a" * 40,
                "wheel": {"filename": "x.whl", "sha256": "b" * 64},
                "bundle": {"sha256": "c" * 64},
            }
        )
    )

    with pytest.raises(IngressError, match="not a passed release attestation"):
        read_qualification_authority(path)


def _tar(path: Path, members: list[tuple[str, bytes | None, str | None]]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                archive.addfile(info)
            elif payload is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
    return path


@pytest.mark.parametrize(
    "members",
    [
        [("qualification-input", None, None), ("qualification-input/link", None, "/etc/passwd")],
        [("qualification-input", None, None), ("qualification-input/../escape", b"x", None)],
        [("other-root/bundle.json", b"{}", None)],
        [("/qualification-input/bundle.json", b"{}", None)],
        [("qualification-input/a", b"1", None), ("qualification-input/a", b"2", None)],
    ],
)
def test_archive_topology_refuses_unsafe_members(tmp_path: Path, members) -> None:
    with pytest.raises(IngressError, match="topology is unsafe"):
        check_archive_topology(_tar(tmp_path / "input.tar.gz", members))


def test_archive_topology_caps_the_member_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = [("qualification-input", None, None)] + [
        (f"qualification-input/{index}", b"x", None) for index in range(3)
    ]
    archive = _tar(tmp_path / "input.tar.gz", members)
    check_archive_topology(archive)

    monkeypatch.setattr(release_ingress, "_MAX_ARCHIVE_MEMBERS", 3)

    with pytest.raises(IngressError, match="too many members"):
        check_archive_topology(archive)


def test_archive_topology_caps_the_extracted_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _tar(
        tmp_path / "input.tar.gz",
        [
            ("qualification-input", None, None),
            ("qualification-input/a", b"1234", None),
            ("qualification-input/b", b"5678", None),
        ],
    )
    check_archive_topology(archive)

    monkeypatch.setattr(release_ingress, "_MAX_ARCHIVE_BYTES", 7)

    with pytest.raises(IngressError, match="size limit"):
        check_archive_topology(archive)


def test_archive_size_caps_are_generous_for_a_real_install_input() -> None:
    # v0.1.13's install-input has 18 members and ~0.9 GB of file content.
    assert release_ingress._MAX_ARCHIVE_MEMBERS >= 1000
    assert release_ingress._MAX_ARCHIVE_BYTES >= 4 * 1024**3


def _extra_wheelhouse(root: Path) -> None:
    (root / "wheelhouse" / "extra.whl").write_bytes(b"x")


def _missing_tool(root: Path) -> None:
    (root / "toolchain" / "srt").unlink()


def _tampered_wheel(root: Path) -> None:
    (root / "dist" / f"paulsha_cortex-{fixtures.VERSION}-py3-none-any.whl").write_bytes(
        b"tampered\n"
    )


def _extra_root_entry(root: Path) -> None:
    (root / "notes.txt").write_text("x", encoding="utf-8")


def _symlinked_source(root: Path) -> None:
    source = root / "source" / "paulsha-cortex.bundle"
    real = root.parent / "elsewhere.bundle"
    real.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(real)


def test_input_tree_validation_accepts_what_verify_bundle_accepts(tmp_path: Path) -> None:
    tree = fixtures.write_input_tree(tmp_path / "base")

    assert (
        verify_input_tree(
            tree.bundle,
            candidate_sha=fixtures.CANDIDATE_SHA,
            wheel_sha256=tree.wheel_sha256,
            owner_uid=os.getuid(),
        )
        is None
    )
    assert (
        qualification_validate_bundle(
            tree.bundle, candidate_sha=fixtures.CANDIDATE_SHA, wheel_sha256=tree.wheel_sha256
        )
        is None
    )


@pytest.mark.parametrize(
    "mutate",
    [_extra_wheelhouse, _missing_tool, _tampered_wheel, _extra_root_entry, _symlinked_source],
    ids=lambda mutate: mutate.__name__,
)
def test_input_tree_mutations_are_refused_like_verify_bundle(tmp_path: Path, mutate) -> None:
    tree = fixtures.write_input_tree(tmp_path / "case")
    mutate(tree.root)

    with pytest.raises(IngressError):
        verify_input_tree(
            tree.bundle,
            candidate_sha=fixtures.CANDIDATE_SHA,
            wheel_sha256=tree.wheel_sha256,
            owner_uid=os.getuid(),
        )
    with pytest.raises((ValueError, OSError)):
        qualification_validate_bundle(
            tree.bundle, candidate_sha=fixtures.CANDIDATE_SHA, wheel_sha256=tree.wheel_sha256
        )


_WHEEL = f"paulsha_cortex-{fixtures.VERSION}-py3-none-any.whl"


def _sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hardlink(path: Path) -> None:
    os.link(path, path.parent.parent.parent / f"{path.name}.second-link")


def _directory_instead_of_file(path: Path) -> None:
    path.unlink()
    path.mkdir()


def _file_instead_of_directory(path: Path) -> None:
    shutil.rmtree(path)
    path.write_text("x", encoding="utf-8")


def _drop_wheelhouse_candidate(root: Path, document: dict) -> None:
    (root / "wheelhouse" / _WHEEL).unlink()
    document["wheelhouse"] = [
        row for row in document["wheelhouse"] if row["path"] != f"wheelhouse/{_WHEEL}"
    ]


def _drop_tool(root: Path, document: dict) -> None:
    (root / "toolchain" / "srt").unlink()
    document["toolchain"] = [row for row in document["toolchain"] if row["name"] != "srt"]


def _generated_under_a_symlinked_directory(root: Path, document: dict) -> None:
    (root / "source" / "link").symlink_to(root / "toolchain", target_is_directory=True)
    document["generated_artifacts"] = [
        {"path": "source/link/codex", "sha256": _sha256_of(root / "toolchain" / "codex")}
    ]


def _duplicate_generated(root: Path, document: dict) -> None:
    row = {"path": "install-config.yaml", "sha256": _sha256_of(root / "install-config.yaml")}
    document["generated_artifacts"] = [row, dict(row)]


def _tree_tool_with_absolute_entrypoint(_root: Path, document: dict) -> None:
    document["toolchain"][0].update(
        shape="tree", entrypoint="/bin/agy", installed_sha256="0" * 64
    )


# Every refusal branch of `qualification/verify_bundle.py::validate_bundle`
# and its port `release_ingress._validate_bundle_inventory`: (id, mutation of
# the input root and/or bundle document, validator argument overrides,
# message both must raise, exception type).  The mutation may return raw text
# to write as bundle.json instead of the (mutated) document.
_PARITY_CASES = [
    ("hardlinked-bundle", lambda root, doc: _hardlink(root / "bundle.json"), {},
     "bundle must be a single-link regular file", None),
    ("extra-root-entry", lambda root, doc: (root / "notes.txt").write_text("x"), {},
     "qualification input root inventory is not exact", None),
    ("install-config-directory",
     lambda root, doc: _directory_instead_of_file(root / "install-config.yaml"), {},
     "install config must be a single-link regular file", None),
    ("source-root-file", lambda root, doc: _file_instead_of_directory(root / "source"), {},
     "source root must be a real directory", None),
    ("bundle-not-json", lambda root, doc: "{not json", {}, "bundle is not JSON", None),
    ("unknown-root-field", lambda root, doc: doc.update(extra=1), {},
     "bundle has missing or unknown root fields", None),
    ("boolean-schema-version", lambda root, doc: doc.update(schema_version=True), {},
     "bundle schema_version must be 1", None),
    ("foreign-candidate", lambda root, doc: doc.update(candidate_sha="f" * 40), {},
     "bundle candidate_sha does not match", None),
    ("non-hex-candidate", lambda root, doc: doc.update(candidate_sha="z" * 40),
     {"candidate_sha": "z" * 40}, "bundle candidate_sha does not match", None),
    ("wheel-extra-field", lambda root, doc: doc["wheel"].update(size=1), {},
     "wheel must contain only path and sha256", None),
    ("wheel-empty-path", lambda root, doc: doc["wheel"].update(path=""), {},
     "wheel.path must be a non-empty string", None),
    ("wheel-escaping-path", lambda root, doc: doc["wheel"].update(path=f"../{_WHEEL}"), {},
     "wheel.path is unsafe", None),
    ("wheel-invalid-sha", lambda root, doc: doc["wheel"].update(sha256="Z" * 64), {},
     "wheel.sha256 is invalid", None),
    ("wheel-missing", lambda root, doc: (root / "dist" / _WHEEL).unlink(), {},
     "No such file or directory", FileNotFoundError),
    ("wheel-tampered", lambda root, doc: (root / "dist" / _WHEEL).write_bytes(b"tampered\n"),
     {}, "wheel.sha256 does not match dist/", None),
    ("other-selected-wheel", lambda root, doc: None, {"wheel_sha256": "9" * 64},
     "bundle wheel sha256 does not match the selected candidate", None),
    ("wheel-outside-dist",
     lambda root, doc: doc["wheel"].update(path=f"wheelhouse/{_WHEEL}"), {},
     "bundle wheel must be under dist/", None),
    ("extra-dist-file", lambda root, doc: (root / "dist" / "other.whl").write_bytes(b"x"), {},
     "dist inventory must contain only the declared candidate wheel", None),
    ("dist-subdirectory", lambda root, doc: (root / "dist" / "sub").mkdir(), {},
     "dist entry must be a single-link regular file", None),
    ("empty-wheelhouse", lambda root, doc: doc.update(wheelhouse=[]), {},
     "bundle wheelhouse must be a non-empty array", None),
    ("non-wheel-in-wheelhouse",
     lambda root, doc: doc["wheelhouse"][1].update(path="wheelhouse/notes.txt"), {},
     "bundle wheelhouse must list wheels directly under wheelhouse/", None),
    ("nested-wheelhouse-entry",
     lambda root, doc: doc["wheelhouse"][1].update(path="wheelhouse/sub/x.whl"), {},
     "bundle wheelhouse must list wheels directly under wheelhouse/", None),
    ("wheelhouse-entry-tampered",
     lambda root, doc: (root / "wheelhouse" / "PyYAML-6.0.2-py3-none-any.whl").write_bytes(b"x"),
     {}, r"wheelhouse\[1\].sha256 does not match", None),
    ("duplicate-wheelhouse-entry",
     lambda root, doc: doc["wheelhouse"].append(dict(doc["wheelhouse"][1])), {},
     "wheelhouse manifest paths are duplicated", None),
    ("undeclared-wheelhouse-file",
     lambda root, doc: (root / "wheelhouse" / "extra.whl").write_bytes(b"x"), {},
     "wheelhouse inventory is incomplete or contains an undeclared file", None),
    ("wheelhouse-without-candidate", _drop_wheelhouse_candidate, {},
     "wheelhouse does not contain the exact candidate wheel", None),
    ("generated-not-array", lambda root, doc: doc.update(generated_artifacts={}), {},
     "generated_artifacts must be an array", None),
    ("generated-symlink-ancestor", _generated_under_a_symlinked_directory, {},
     r"generated_artifacts\[0\].path has a symlink ancestor", None),
    ("duplicate-generated", _duplicate_generated, {},
     "bundle contains duplicate generated artifact paths", None),
    ("empty-toolchain", lambda root, doc: doc.update(toolchain=[]), {},
     "toolchain must be a non-empty array", None),
    ("toolchain-row-not-object", lambda root, doc: doc["toolchain"].__setitem__(0, "agy"), {},
     r"toolchain\[0\] must be an object", None),
    ("toolchain-unknown-field", lambda root, doc: doc["toolchain"][0].update(extra=1), {},
     r"toolchain\[0\] has missing or unknown fields", None),
    ("duplicate-tool-name",
     lambda root, doc: doc["toolchain"][1].update(name=doc["toolchain"][0]["name"]), {},
     r"toolchain\[1\] identity is invalid", None),
    ("tree-tool-absolute-entrypoint", _tree_tool_with_absolute_entrypoint, {},
     r"toolchain\[0\] tree metadata is invalid", None),
    ("hardlinked-tool", lambda root, doc: _hardlink(root / "toolchain" / "codex"), {},
     r"toolchain\[2\].path must be a single-link regular file", None),
    ("missing-tool", _drop_tool, {}, "toolchain inventory is incomplete", None),
    ("undeclared-toolchain-file",
     lambda root, doc: (root / "toolchain" / "extra").write_bytes(b"x"), {},
     "toolchain directory has undeclared or missing artifacts", None),
    ("empty-source-repositories", lambda root, doc: doc.update(source_repositories=[]), {},
     "source_repositories must be a non-empty array", None),
    ("source-unknown-field",
     lambda root, doc: doc["source_repositories"][0].update(branch="main"), {},
     r"source_repositories\[0\] fields are invalid", None),
    ("source-http-remote",
     lambda root, doc: doc["source_repositories"][0].update(remote="http://example.invalid/x"),
     {}, r"source_repositories\[0\] identity is invalid", None),
    ("symlinked-source", lambda root, doc: _symlinked_source(root), {},
     r"source_repositories\[0\].path must be a single-link regular file", None),
    ("undeclared-source-file",
     lambda root, doc: (root / "source" / "extra.bundle").write_bytes(b"x"), {},
     "source directory has undeclared or missing artifacts", None),
]


@pytest.mark.parametrize(
    ("mutate", "overrides", "message", "error"),
    [case[1:] for case in _PARITY_CASES],
    ids=[case[0] for case in _PARITY_CASES],
)
def test_bundle_port_refuses_every_branch_with_verify_bundles_message(
    tmp_path: Path, mutate, overrides: dict, message: str, error: type | None
) -> None:
    # #1270: the hand-kept port and the RC validator must agree branch by
    # branch, with the same message -- not only both raise something.
    tree = fixtures.write_input_tree(tmp_path / "case")
    document = json.loads(tree.bundle.read_text(encoding="utf-8"))
    raw = mutate(tree.root, document)
    tree.bundle.write_text(
        raw if isinstance(raw, str) else json.dumps(document, sort_keys=True),
        encoding="utf-8",
    )
    arguments = {
        "candidate_sha": fixtures.CANDIDATE_SHA,
        "wheel_sha256": tree.wheel_sha256,
        **overrides,
    }

    with pytest.raises(error or IngressError, match=message):
        release_ingress._validate_bundle_inventory(tree.bundle, **arguments)
    with pytest.raises(error or ValueError, match=message):
        qualification_validate_bundle(tree.bundle, **arguments)


def test_bundle_parity_table_covers_every_refusal_message_of_verify_bundle() -> None:
    # A refusal added to verify_bundle.py without a parity case fails here.
    import ast
    import inspect

    import qualification.verify_bundle as verify_bundle

    def literal_parts(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.JoinedStr):
            return [part.value for part in node.values if isinstance(part, ast.Constant)]
        return []

    covered = " ".join(case[3] for case in _PARITY_CASES).replace("\\", "")
    tree = ast.parse(inspect.getsource(verify_bundle.validate_bundle))
    templates = [
        node.args[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ValueError"
        and node.args
    ]
    entry = ast.parse(inspect.getsource(verify_bundle._entry))
    templates += [
        node.args[0]
        for node in ast.walk(entry)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ValueError"
        and node.args
    ]
    assert len(templates) >= 25
    for template in templates:
        parts = [part.strip() for part in literal_parts(template) if len(part.strip()) > 3]
        assert parts, ast.unparse(template)
        for part in parts:
            assert part in covered, ast.unparse(template)


def test_input_tree_refuses_group_writable_files(tmp_path: Path) -> None:
    tree = fixtures.write_input_tree(tmp_path / "case")
    (tree.root / "install-config.yaml").chmod(0o664)

    with pytest.raises(IngressError, match="ownership/mode is unsafe"):
        verify_input_tree(
            tree.bundle,
            candidate_sha=fixtures.CANDIDATE_SHA,
            wheel_sha256=tree.wheel_sha256,
            owner_uid=os.getuid(),
        )


def test_sealed_venv_is_built_offline_with_copies_and_no_symlinks(tmp_path: Path) -> None:
    calls: list = []
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("", encoding="utf-8")

    cli = build_sealed_venv(
        tmp_path / "venv",
        requirements,
        owner_uid=os.getuid(),
        run=fixtures.fake_venv_runner(calls),
    )

    assert cli == tmp_path / "venv/bin/cortex"
    assert not os.path.lexists(tmp_path / "venv/lib64")
    assert stat.S_IMODE((tmp_path / "venv").stat().st_mode) == 0o755
    assert stat.S_IMODE(cli.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "venv/lib/site.py").stat().st_mode) == 0o644
    assert calls[0][1] == {
        "HOME": "/root",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
    }


def test_sealed_venv_refuses_an_existing_path(tmp_path: Path) -> None:
    (tmp_path / "venv").mkdir()

    with pytest.raises(IngressError, match="already exists"):
        build_sealed_venv(
            tmp_path / "venv",
            tmp_path / "requirements.txt",
            owner_uid=os.getuid(),
            run=fixtures.fake_venv_runner([]),
        )


def test_tree_digest_tracks_content_and_refuses_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "venv"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "x").write_text("x", encoding="utf-8")
    fixtures.drop_group_other_write(root)
    digest = tree_sha256(root, owner_uid=os.getuid())

    (root / "bin" / "x").write_text("y", encoding="utf-8")
    assert tree_sha256(root, owner_uid=os.getuid()) != digest
    (root / "bin" / "link").symlink_to("x")
    with pytest.raises(IngressError, match="unsafe candidate CLI"):
        tree_sha256(root, owner_uid=os.getuid())
