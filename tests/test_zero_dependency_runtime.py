from pathlib import Path


def test_runtime_modules_do_not_import_yaml_directly():
    repo_root = Path(__file__).resolve().parents[1]
    for rel in ("paulsha_cortex/persona/loader.py", "paulsha_cortex/coordinator/autonomy.py"):
        source = (repo_root / rel).read_text(encoding="utf-8")
        assert "import yaml" not in source
