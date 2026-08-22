"""Phase 2 trust-root installer RED contract: desired-state planning.

These tests are deliberately rootless.  Planning is a pure conversion from explicit
configuration plus exact artifacts into canonical JSON; it must never inspect the
operator's HOME or execute generated shell text.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paulsha_cortex.trust_root.install import (
    InstallPlanError,
    UnsafeInstallPathError,
    bind_bundle_artifacts,
    build_install_plan,
    canonical_plan_bytes,
    plan_sha256,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifacts(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / "paulsha_cortex-0.2.0-py3-none-any.whl"
    bundle = tmp_path / "paulsha-cortex-phase2.bundle"
    wheel.write_bytes(b"exact candidate wheel\n")
    bundle.write_bytes(b"exact source bundle\n")
    return wheel, bundle


def _safe_config(tmp_path: Path) -> dict[str, object]:
    roots = tmp_path / "target"
    return {
        "schema_version": 1,
        "scheme": "four-way",
        "instance": "cortex",
        "repo_identity": {
            "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
            "commit": "a" * 40,
        },
        "operator_account": "cortex-ops",
        "external_reader_account": "<absent>",
        "accounts": {
            "cortex-manager": {
                "uid": 991,
                "gid": 991,
                "home": str(roots / "var/lib/cortex-manager"),
                "shell": "/usr/sbin/nologin",
            },
            "cortex-reviewer-planner": {
                "uid": 992,
                "gid": 992,
                "home": str(roots / "var/lib/cortex-reviewer-planner"),
                "shell": "/usr/sbin/nologin",
            },
            "cortex-builder": {
                "uid": 993,
                "gid": 993,
                "home": str(roots / "var/lib/cortex-builder"),
                "shell": "/usr/sbin/nologin",
            },
            "cortex-gate": {
                "uid": 994,
                "gid": 994,
                "home": str(roots / "var/lib/cortex-gate"),
                "shell": "/usr/sbin/nologin",
            },
        },
        "service_accounts": {
            "cortex-egress": {
                "uid": 995,
                "gid": 995,
                "home": str(roots / "var/lib/cortex-egress"),
                "shell": "/usr/sbin/nologin",
            }
        },
        "roots": {
            "deploy": str(roots / "opt/cortex"),
            "state": str(roots / "var/lib/cortex"),
            "systemd": str(roots / "etc/systemd/system"),
            "polkit": str(roots / "etc/polkit-1/rules.d"),
        },
        "source_repositories": ["paulsha-cortex"],
        "legacy_policy": "quarantine",
        "providers": {
            "builder": ["codex"],
            "reviewer-planner": ["agy", "copilot"],
            "manager": ["github"],
        },
        "toolchain": {
            "codex": {"version": "0.150.0", "sha256": "1" * 64},
            "agy": {"version": "1.2.3", "sha256": "2" * 64},
            "copilot": {"version": "0.0.400", "sha256": "3" * 64},
        },
    }


def _plan_document(tmp_path: Path, config: dict[str, object] | None = None):
    wheel, bundle = _artifacts(tmp_path)
    plan = build_install_plan(
        config=config or _safe_config(tmp_path),
        candidate_wheel=wheel,
        bundle=bundle,
    )
    return plan, json.loads(canonical_plan_bytes(plan))


def test_same_inputs_produce_byte_identical_canonical_plan_and_hash(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)

    first = build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)
    second = build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)

    assert canonical_plan_bytes(first) == canonical_plan_bytes(second)
    assert plan_sha256(first) == plan_sha256(second)
    assert plan_sha256(first) == hashlib.sha256(canonical_plan_bytes(first)).hexdigest()
    # Canonical JSON is a single deterministic encoding, not pretty-printed output.
    assert canonical_plan_bytes(first).endswith(b"\n")
    assert b"\n  " not in canonical_plan_bytes(first)


def test_plan_is_exact_artifact_bound_four_way_structured_desired_state(
    tmp_path: Path,
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    plan = build_install_plan(
        config=_safe_config(tmp_path), candidate_wheel=wheel, bundle=bundle
    )
    doc = json.loads(canonical_plan_bytes(plan))

    assert doc["schema_version"] == 1
    assert doc["scheme"] == "four-way"
    assert doc["repo_identity"]["commit"] == "a" * 40
    assert doc["source_repositories"] == ["paulsha-cortex"]
    assert doc["candidate"]["wheel_sha256"] == _sha256(wheel)
    assert doc["candidate"]["bundle_sha256"] == _sha256(bundle)
    assert {row["name"] for row in doc["accounts"]} == {
        "cortex-manager",
        "cortex-reviewer-planner",
        "cortex-builder",
        "cortex-gate",
    }
    assert doc["assets"], "registry/permgen inventory must not be replaced by prose"
    assert doc["apply_order"], "apply must consume typed ordered steps"
    assert all("shell" not in step and "command" not in step for step in doc["apply_order"])
    assert set(doc["generated"]) >= {
        "units",
        "shim",
        "polkit",
        "gitconfigs",
        "toolchain_wrappers",
    }


def test_bundle_binding_preserves_repo_container_and_installs_named_leaf(
    tmp_path: Path,
) -> None:
    plan, _doc = _plan_document(tmp_path)
    tools = [
        {
            "name": name,
            "version": configured["version"],
            "shape": "file",
            "resolved_path": str(tmp_path / f"{name}.locked"),
            "sha256": configured["sha256"],
        }
        for name, configured in plan["toolchain_manifest"].items()
    ]
    repository = tmp_path / "paulsha-cortex.bundle"
    repository.write_bytes(b"exact source bundle\n")
    repositories = [{
        "slug": "paulsha-cortex",
        "commit": "a" * 40,
        "remote": "https://github.com/hamanpaul/paulsha-cortex.git",
        "resolved_path": str(repository),
        "sha256": _sha256(repository),
    }]

    bound = bind_bundle_artifacts(
        plan, {"toolchain": tools, "source_repositories": repositories}
    )
    step_ids = [step["step_id"] for step in bound["apply_order"]]
    container_index = step_ids.index("asset:repo-source-tree")
    repository_index = step_ids.index("repository:paulsha-cortex")
    repository_step = bound["apply_order"][repository_index]

    assert repository_index == container_index + 1
    assert repository_step["path"] == f"{plan['roots']['state']}/repos/paulsha-cortex"
    assert repository_step["owner"] == "cortex-manager"
    assert repository_step["group"] == "cortex-manager"

    plan["source_repositories"] = ["different-repository"]
    with pytest.raises(InstallPlanError, match="does not match the plan"):
        bind_bundle_artifacts(
            plan, {"toolchain": tools, "source_repositories": repositories}
        )


def test_plan_orders_accounts_and_content_addressed_venv_before_assets(
    tmp_path: Path,
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    plan = build_install_plan(
        config=_safe_config(tmp_path), candidate_wheel=wheel, bundle=bundle
    )
    steps = plan["apply_order"]

    assert [step["kind"] for step in steps[:5]] == ["account"] * 5
    assert {step["name"] for step in steps[:5]} == {
        "cortex-manager",
        "cortex-reviewer-planner",
        "cortex-builder",
        "cortex-gate",
        "cortex-egress",
    }
    venv = steps[5]
    wheel_sha = _sha256(wheel)
    assert venv["kind"] == "venv"
    assert venv["path"].endswith(f"/venvs/{wheel_sha}")
    assert venv["active_link"].endswith("/opt/cortex/venv")
    assert venv["wheel_source"] == str(wheel.absolute())
    assert venv["wheelhouse"] == [
        {"source": str(wheel.absolute()), "sha256": wheel_sha}
    ]
    assert next(index for index, step in enumerate(steps) if step["kind"] == "asset") > 5
    assert steps[-4]["action"] == "daemon-reload"
    assert [step["unit"] for step in steps[-3:]] == [
        "cortex-egress-proxy.service",
        "cortex-manager.service",
        "cortex-monitor.service",
    ]


def test_manager_gitconfig_in_plan_delivers_the_gh_credential_helper_763(
    tmp_path: Path,
) -> None:
    """#763: Manager owns Git operations, so its installed config must deliver auth."""
    _plan, doc = _plan_document(tmp_path)
    manager = doc["generated"]["gitconfigs"]["manager-gitconfig"]
    functional = [
        line.strip()
        for line in manager["content"].splitlines()
        if line.strip() and not line.lstrip().startswith(("#", ";"))
    ]
    assert "/usr/bin/gh auth git-credential" in "\n".join(functional)
    assert manager["owner"] == "root"
    assert manager["mode"] == "0644"


@pytest.mark.parametrize("scheme", ["two-way", "three-way", "five-way"])
def test_new_install_rejects_every_non_four_way_scheme(
    tmp_path: Path, scheme: str
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config["scheme"] = scheme

    with pytest.raises(InstallPlanError, match="four-way"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("github_token", "test-secret-value"),
        ("password", "do-not-render-this"),
        ("operator_home", "/srv/example-operator-home"),
        ("credential_bytes", "do-not-render-this"),
    ],
)
def test_plan_rejects_secret_like_or_home_discovery_fields(
    tmp_path: Path, field: str, value: str
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config[field] = value

    with pytest.raises(InstallPlanError, match=field):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_plan_rejects_nested_api_key_even_under_a_toolchain_entry(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    config["toolchain"]["codex"]["api_key"] = "PLAINTEXT-CREDENTIAL"

    with pytest.raises(InstallPlanError, match="api_key"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_plan_document_never_contains_secret_bytes_or_operator_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", "/srv/sentinel-must-not-be-discovered")
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-from-parent-env")
    plan, _doc = _plan_document(tmp_path)
    rendered = canonical_plan_bytes(plan).decode("utf-8")

    assert "sentinel-must-not-be-discovered" not in rendered
    assert "test-secret-from-parent-env" not in rendered


def test_lexical_path_escape_is_rejected_before_plan_is_emitted(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    config = _safe_config(tmp_path)
    roots = dict(config["roots"])
    roots["state"] = str(tmp_path / "target/var/lib/cortex/../../../../escape")
    config["roots"] = roots

    with pytest.raises(UnsafeInstallPathError, match="state"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_symlinked_install_parent_is_rejected(tmp_path: Path) -> None:
    wheel, bundle = _artifacts(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "target-link"
    link.symlink_to(outside, target_is_directory=True)
    config = _safe_config(tmp_path)
    roots = dict(config["roots"])
    roots["deploy"] = str(link / "opt/cortex")
    config["roots"] = roots

    with pytest.raises(UnsafeInstallPathError, match="symlink"):
        build_install_plan(config=config, candidate_wheel=wheel, bundle=bundle)


def test_candidate_bundle_symlink_is_rejected_before_content_is_consumed(
    tmp_path: Path,
) -> None:
    wheel, bundle = _artifacts(tmp_path)
    bundle_link = tmp_path / "candidate.bundle"
    bundle_link.symlink_to(bundle)

    with pytest.raises(UnsafeInstallPathError, match="symlink"):
        build_install_plan(
            config=_safe_config(tmp_path),
            candidate_wheel=wheel,
            bundle=bundle_link,
        )
