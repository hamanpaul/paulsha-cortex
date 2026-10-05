"""#1263：release ingress 逐項移植 runbook §1（REST metadata、asset、目錄與 fetcher）。"""
from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

import release_ingress_fixtures as fixtures
from paulsha_cortex.trust_root.install.release_ingress import (
    DirectoryReleaseFetcher,
    GitHubReleaseFetcher,
    IngressError,
    ReleaseAsset,
    asset_names,
    assert_private_chain,
    download_asset,
    format_version,
    make_attempt_dir,
    parse_version,
    resolve_release,
    wheel_version,
)

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
