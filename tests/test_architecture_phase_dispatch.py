"""Phase/Card/Persona/Job documentation must agree with pinned executable contracts."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "docs/architecture"
PHASES = ("claim", "define", "plan", "build", "verify", "review", "ship")


def documents():
    return tuple(json.loads((ARCH / name).read_text(encoding="utf-8"))
                 for name in ("facts.json", "architecture.json"))


def source(facts, path):
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "show", facts["repository"]["revision"] + ":" + path],
        text=True, encoding="utf-8",
    )


def assert_phase_contract(facts, ir):
    """Compare phases and roles to the actual pinned combo, not diagram captions."""
    review = facts["workflow_review"]
    catalog = {x["id"]: x for x in yaml.safe_load(source(
        facts, "paulsha_cortex/deck/data/cards.yaml"))["cards"]}
    combo = yaml.safe_load(source(
        facts, "paulsha_cortex/deck/data/combos/feature-oneshot.yaml"))["combo"]
    personas = yaml.safe_load(source(
        facts, "paulsha_cortex/persona/personas.yaml"))["roles"]
    expected = {phase: [] for phase in PHASES}
    for entry in combo["cards"]:
        card = catalog[entry["ref"]]
        expected[card["phase"]].append(card["id"])
    assert tuple(s["id"] for s in review["steps"]) == PHASES
    nodes = {n["id"]: n for n in ir["components"]}
    for step in review["steps"]:
        assert step["cards"] == expected[step["id"]]
        assert step["id"] in personas[step["persona"]]["allowed_phases"]
        for card_id in step["cards"]:
            assert catalog[card_id]["persona_binding"] == step["persona"]
        assert nodes["phase-" + step["id"]]["sublabel"] == step["diagram_summary"]
    conditional = [c["id"] for s in review["steps"] for c in s["conditional_cards"]]
    assert conditional == [c["ref"] for c in combo["band_triggered"]["cards"]]
    # One Run is expanded once. A Job must not restart the Claim->Ship spine.
    edges = {e["id"]: e for e in ir["connections"]}
    assert edges["run-spine"]["from"] == review["controller"] == "manager"
    assert edges["run-spine"]["to"] == "phase-claim"
    assert edges["run-to-card"]["from"] == review["state_store"]
    assert edges["run-to-card"]["to"] == "workflow-card"
    assert edges["card-to-attempt"]["from"] == "workflow-card"
    assert edges["card-to-attempt"]["to"] == "job-attempt"
    assert not any(e["from"] == "job-attempt" and e["to"].startswith("phase-")
                   for e in edges.values())


def test_phase_card_bindings_match_the_pinned_deck_and_personas():
    assert_phase_contract(*documents())


@pytest.mark.parametrize("mutation", ["wrong-persona", "collapsed-build", "job-restarts-run", "stale-caption"])
def test_phase_dispatch_misrepresentations_are_rejected(mutation):
    facts, ir = (deepcopy(x) for x in documents())
    steps = {s["id"]: s for s in facts["workflow_review"]["steps"]}
    if mutation == "wrong-persona":
        steps["build"]["persona"] = "reviewer"
    elif mutation == "collapsed-build":
        steps["build"]["cards"] = ["subagent-build"]
    elif mutation == "job-restarts-run":
        next(e for e in ir["connections"] if e["id"] == "run-spine")["from"] = "job-attempt"
    else:
        next(n for n in ir["components"] if n["id"] == "phase-plan")["sublabel"] = "always launch a new job"
    with pytest.raises(AssertionError):
        assert_phase_contract(facts, ir)


def test_every_core_and_phase_caption_has_exact_source_projection():
    facts, ir = documents()
    expected = {record["id"]: record for record in facts["components"]}
    nodes = {node["id"]: node for node in ir["components"]}
    phase_ids = {"phase-" + phase for phase in PHASES}
    assert set(nodes) == set(expected) | phase_ids
    for component_id, record in expected.items():
        node = nodes[component_id]
        for key in ("id", "type", "label", "sublabel", "tag"):
            assert record.get(key) == node.get(key)
        assert node["sources"] == [
            {k: a[k] for k in ("path", "line", "end_line")}
            for a in record["evidence"][:3]
        ]


def test_local_adoption_and_ship_job_exceptions_are_visible_and_evidenced():
    facts, ir = documents()
    nodes = {n["id"]: n for n in ir["components"]}
    steps = {s["id"]: s for s in facts["workflow_review"]["steps"]}
    assert "本地" in nodes["phase-claim"]["sublabel"]
    assert "採用" in nodes["phase-define"]["sublabel"]
    assert "採用" in nodes["phase-plan"]["sublabel"]
    assert "本地" in nodes["phase-ship"]["sublabel"]
    assert "0..N" in nodes["job-attempt"]["sublabel"]
    assert "本地稽核" in nodes["job-attempt"]["sublabel"]
    assert steps["claim"]["actor"] == steps["ship"]["actor"] == "manager"
    # The cited ship implementation has deterministic audit Jobs; no zero-Job claim.
    record = next(c for c in facts["components"] if c["id"] == "job-attempt")
    anchors = [a for a in record["evidence"] if a["path"].endswith("work_bridge.py")]
    assert anchors
    text = source(facts, anchors[0]["path"]).splitlines()
    excerpt = "\n".join(text[anchors[0]["line"] - 1:anchors[0]["end_line"]])
    assert 'executor="cortex-manager"' in excerpt
    assert 'model_id="deterministic"' in excerpt
    assert any("deterministic" in gate for gate in steps["ship"]["gates"])
