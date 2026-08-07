def test_phases_is_frozen_stage3_vocabulary():
    from paulsha_cortex.persona.contract import PHASES

    # 共用詞彙聯集：含 hippo 的 research 首階段，與 paulsha-hippo
    # lib/lifecycle/schema.PHASES 必須逐字相等（由 paulshaclaw 對齊測試守）。
    assert PHASES == (
        "claim",
        "research",
        "define",
        "plan",
        "build",
        "verify",
        "review",
        "ship",
    )


def test_workflow_phases_stays_the_executed_pipeline():
    """詞彙表擴充不得外溢到執行序列——cortex 管線仍是 7 個、自 claim 起。"""
    from paulsha_cortex.coordinator.workflow import WORKFLOW_PHASES
    from paulsha_cortex.persona.contract import PHASES

    assert WORKFLOW_PHASES == ("claim", "define", "plan", "build", "verify", "review", "ship")
    assert set(WORKFLOW_PHASES).issubset(set(PHASES))
    assert "research" not in WORKFLOW_PHASES


def test_no_hippo_import():
    import inspect

    import paulsha_cortex.persona.contract as m

    assert "paulsha_hippo" not in inspect.getsource(m)
