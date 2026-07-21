import importlib.metadata

from paulsha_cortex.cli import main


def test_version_flag_prints_package_version(capsys, monkeypatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")

    assert main(["--version"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "cortex 1.2.3\n"
    assert captured.err == ""


def test_version_flag_falls_back_when_package_metadata_missing(capsys, monkeypatch) -> None:
    def raise_package_not_found(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", raise_package_not_found)

    assert main(["--version"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "cortex 0.0.0+unknown\n"
    assert captured.err == ""
