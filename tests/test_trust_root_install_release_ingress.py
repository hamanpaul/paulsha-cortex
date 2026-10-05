"""#1263：release ingress 逐項移植 runbook §1（REST metadata、asset、目錄與 fetcher）。"""
from __future__ import annotations

import io
import json
import os
import stat
import tarfile
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
    digest = tree_sha256(root, owner_uid=os.getuid())

    (root / "bin" / "x").write_text("y", encoding="utf-8")
    assert tree_sha256(root, owner_uid=os.getuid()) != digest
    (root / "bin" / "link").symlink_to("x")
    with pytest.raises(IngressError, match="unsafe candidate CLI"):
        tree_sha256(root, owner_uid=os.getuid())
