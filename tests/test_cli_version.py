import importlib.metadata

from paulsha_cortex.cli import main


def test_version_flag_prints_package_version(capsys, monkeypatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.2.3")

    assert main(["--version"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "cortex 1.2.3\n"
    assert captured.err == ""
