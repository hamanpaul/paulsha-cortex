from paulsha_cortex.deploy.installer import render_units


def test_render_substitutes_instance_and_script(tmp_path):
    units = render_units(instance="alpha", interval=120)
    service = units["alpha-manager.service"]
    assert "__INSTANCE__" not in service and "__SERVICE_SCRIPT__" not in service
    assert "alpha persona manager service" in service
    timer = units["alpha-manager.timer"]
    assert "OnUnitActiveSec=120" in timer


def test_install_is_idempotent(tmp_path, monkeypatch):
    from paulsha_cortex.deploy import installer

    monkeypatch.setattr(installer, "_systemctl_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert installer.main(["service", "--instance", "beta"]) == 0
    first = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert installer.main(["service", "--instance", "beta"]) == 0
    second = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert first == second
