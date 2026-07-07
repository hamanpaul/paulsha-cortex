from paulsha_cortex.cli import main


def test_delegates_to_coordinator_cli(monkeypatch):
    seen = {}

    def fake_main(argv=None):
        seen["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr("paulsha_cortex.coordinator.cli.main", fake_main)
    assert main(["status"]) == 0
    assert seen["argv"] == ["status"]


def test_unknown_empty_shows_usage(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()
