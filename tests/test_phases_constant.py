def test_phases_is_frozen_stage3_vocabulary():
    from paulsha_cortex.persona.contract import PHASES

    assert PHASES == ("claim", "define", "plan", "build", "verify", "review", "ship")


def test_no_hippo_import():
    import inspect

    import paulsha_cortex.persona.contract as m

    assert "paulsha_hippo" not in inspect.getsource(m)
