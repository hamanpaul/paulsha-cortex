from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paulsha_cortex.coordinator import job_runner, job_workspace, spool_slot, verification
from paulsha_cortex.coordinator.dispatcher import Dispatcher
from paulsha_cortex.coordinator.registry import JobRegistry
from paulsha_cortex.coordinator.seams import ScriptWorktreeCreator

_BRANCH = "feature/623-bundle-harvest"
_JOB_ID = "623-bundle-harvest"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repos" / "paulsha-cortex"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    _git(repo, "config", "user.email", "manager@example.invalid")
    _git(repo, "config", "user.name", "Cortex Manager")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _workspace(repo: Path, pool: Path) -> Path:
    return Path(
        ScriptWorktreeCreator(repo=repo, wt_root=pool, base="main").create(
            _BRANCH, job_id=_JOB_ID
        )
    )


def _builder_commit(workspace: Path, name: str = "builder.txt") -> str:
    (workspace / name).write_text("builder work\n", encoding="utf-8")
    _git(workspace, "add", name)
    _git(workspace, "commit", "-qm", f"builder: {name}")
    return _git(workspace, "rev-parse", "HEAD")


def _run_bundle_step(workspace: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", job_workspace.build_bundle_command(workspace=workspace, bundle=bundle)],
        cwd=str(workspace),
        check=False,
        capture_output=True,
        text=True,
    )


def _trivial_contract() -> dict[str, object]:
    return {
        "docs_class": "trivial",
        "review_policy": "not-required",
        "required_artifacts": [],
        "checks": [],
        "tests": [],
        "full_suite": {
            "argv": ["true"],
            "cwd": ".",
            "timeout_seconds": 5,
            "baseline": "no-regression",
        },
    }


class RuntimeInstanceAuthorityTests(unittest.TestCase):
    def test_spool_key_prefers_typed_instance_and_fails_closed_on_bad_template_lane(self) -> None:
        slice_id = "phase2-plan-manager-gitconfig-763"
        job_id = f"{slice_id}-132"
        instance = job_runner.template_instance_id(slice_id)

        self.assertEqual(
            job_workspace.spool_key_for_job(
                {
                    "job_id": job_id,
                    "runtime_mode": "systemd-template",
                    "template_instance": instance,
                    "session_name": slice_id,
                    "log_path": f"/tmp/{job_id}.jsonl",
                }
            ),
            instance,
        )
        self.assertEqual(
            job_workspace.spool_key_for_job(
                {
                    "job_id": job_id,
                    "runtime_mode": "direct",
                    "template_instance": None,
                }
            ),
            job_id,
        )
        self.assertIsNone(
            job_workspace.spool_key_for_job(
                {
                    "job_id": job_id,
                    "runtime_mode": "systemd-template",
                    "template_instance": None,
                }
            )
        )
        self.assertIsNone(
            job_workspace.spool_key_for_job(
                {"job_id": job_id, "runtime_mode": "systemd-template"}
            )
        )
        self.assertIsNone(
            job_workspace.spool_key_for_job(
                {
                    "job_id": job_id,
                    "runtime_mode": "systemd-template",
                    "template_instance": "../foreign-slot",
                }
            )
        )

    def test_attach_launch_handle_persists_template_instance(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "jobs.json"
            reg = JobRegistry(state_path=state)
            created = reg.create_job(
                task="phase2-plan-manager-gitconfig-763",
                persona="builder",
                branch="feature/phase2-plan-manager-gitconfig-763",
                pane="%0",
                worktree="/wt/phase2-plan-manager-gitconfig-763",
            )
            instance = job_runner.template_instance_id("phase2-plan-manager-gitconfig-763")

            attached = reg.attach_launch_handle(
                created["job_id"],
                executor="copilot",
                session_name="phase2-plan-manager-gitconfig-763",
                pid=7,
                log_path=f"/tmp/{created['job_id']}.jsonl",
                runtime_mode="systemd-template",
                template_instance=instance,
            )

            self.assertEqual(attached["template_instance"], instance)
            self.assertEqual(
                JobRegistry(state_path=state).get_job(created["job_id"])["template_instance"],
                instance,
            )

    def test_template_credential_harvest_uses_concrete_instance_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            raw_slice_id = "phase2-plan-manager-gitconfig-763"
            job_id = f"{raw_slice_id}-132"
            instance = job_runner.template_instance_id(raw_slice_id)
            surface_id = "builder-codex-home"

            class Registry:
                def __init__(self) -> None:
                    self.job = {
                        "job_id": job_id,
                        "executor": "codex",
                        "runtime_mode": "systemd-template",
                        "template_instance": instance,
                        "runtime_principal": "builder",
                        "runtime_surface": surface_id,
                        "credential_publish": True,
                    }
                    self.updated: dict[str, object] | None = None

                def get_job(self, requested_job_id: str) -> dict[str, object]:
                    self.assert_job_id(requested_job_id)
                    return dict(self.job)

                def assert_job_id(self, requested_job_id: str) -> None:
                    if requested_job_id != job_id:
                        raise AssertionError(requested_job_id)

                def update_headless_result(
                    self, requested_job_id: str, **kwargs: object
                ) -> dict[str, object]:
                    self.assert_job_id(requested_job_id)
                    self.updated = kwargs
                    return {**self.job, **kwargs}

            with mock.patch.dict(
                "os.environ", {"PSC_AGENTS_ROOT": str(root)}, clear=False
            ):
                authority = spool_slot.credential_authority("builder")
                authority.parent.mkdir(parents=True)
                authority.write_text("old\n", encoding="utf-8")

                producer_slot = spool_slot.canonical_job_slot(surface_id, raw_slice_id)
                exact_slot = spool_slot.exact_job_slot(surface_id, instance)
                self.assertEqual(producer_slot, exact_slot)
                producer_slot.mkdir(parents=True)
                (producer_slot / "auth.json").write_text(
                    '{"refresh":"exact"}\n', encoding="utf-8"
                )

                double_hashed_slot = spool_slot.canonical_job_slot(surface_id, instance)
                self.assertNotEqual(double_hashed_slot, producer_slot)
                double_hashed_slot.mkdir(parents=True)
                (double_hashed_slot / "auth.json").write_text(
                    '{"refresh":"sibling"}\n', encoding="utf-8"
                )

                log_path = root / "job.jsonl"
                log_path.write_text("", encoding="utf-8")
                registry = Registry()
                result = Dispatcher(registry, None, None)._finalize_headless(
                    job_id, exit_code=0, log_path=str(log_path)
                )

                self.assertEqual(result["status"], "exited")
                self.assertIsNone(result.get("runtime_diagnostic"))
                self.assertEqual(authority.read_text(encoding="utf-8"), '{"refresh":"exact"}\n')
                self.assertEqual(
                    (double_hashed_slot / "auth.json").read_text(encoding="utf-8"),
                    '{"refresh":"sibling"}\n',
                )

    def test_template_credential_authority_missing_or_malformed_fails_closed(self) -> None:
        for template_instance in (None, "../foreign-slot"):
            with self.subTest(template_instance=template_instance):
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    job_id = "phase2-plan-manager-gitconfig-763-132"
                    job = {
                        "job_id": job_id,
                        "executor": "codex",
                        "runtime_mode": "systemd-template",
                        "runtime_principal": "builder",
                        "runtime_surface": "builder-codex-home",
                        "credential_publish": True,
                    }
                    if template_instance is not None:
                        job["template_instance"] = template_instance

                    class Registry:
                        def __init__(self) -> None:
                            self.updated: dict[str, object] | None = None

                        def get_job(self, requested_job_id: str) -> dict[str, object]:
                            if requested_job_id != job_id:
                                raise AssertionError(requested_job_id)
                            return dict(job)

                        def update_headless_result(
                            self, requested_job_id: str, **kwargs: object
                        ) -> dict[str, object]:
                            if requested_job_id != job_id:
                                raise AssertionError(requested_job_id)
                            self.updated = kwargs
                            return {**job, **kwargs}

                    with mock.patch.dict(
                        "os.environ", {"PSC_AGENTS_ROOT": str(root)}, clear=False
                    ):
                        authority = spool_slot.credential_authority("builder")
                        authority.parent.mkdir(parents=True)
                        authority.write_text("old\n", encoding="utf-8")

                        # Populate the raw-id fallback sibling. A template row must
                        # never consult it when its exact authority is absent/bad.
                        fallback = spool_slot.canonical_job_slot(
                            "builder-codex-home", job_id
                        )
                        fallback.mkdir(parents=True)
                        (fallback / "auth.json").write_text(
                            '{"refresh":"fallback"}\n', encoding="utf-8"
                        )
                        log_path = root / "job.jsonl"
                        log_path.write_text("", encoding="utf-8")
                        registry = Registry()
                        result = Dispatcher(registry, None, None)._finalize_headless(
                            job_id, exit_code=0, log_path=str(log_path)
                        )

                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(
                            registry.updated["runtime_diagnostic"]["reason"],
                            "runtime-credential-harvest-failed",
                        )
                        self.assertEqual(authority.read_text(encoding="utf-8"), "old\n")

    def test_raw_runtime_credential_api_remains_job_id_based(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with mock.patch.dict(
                "os.environ", {"PSC_AGENTS_ROOT": str(root)}, clear=False
            ):
                authority = spool_slot.credential_authority("builder")
                authority.parent.mkdir(parents=True)
                authority.write_text("old\n", encoding="utf-8")
                raw_job_id = "legacy-job"
                raw_slot = spool_slot.canonical_job_slot(
                    "builder-codex-home", raw_job_id
                )
                raw_slot.mkdir(parents=True)
                (raw_slot / "auth.json").write_text(
                    '{"refresh":"legacy"}\n', encoding="utf-8"
                )

                self.assertEqual(
                    spool_slot.commit_runtime_credential(
                        principal="builder", job_id=raw_job_id
                    ),
                    authority,
                )
                self.assertEqual(
                    authority.read_text(encoding="utf-8"), '{"refresh":"legacy"}\n'
                )

    def test_verification_harvest_uses_persisted_template_instance_slot_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp_path = Path(d)
            raw_slice_id = "phase2-plan-manager-gitconfig-763"
            job_id = f"{raw_slice_id}-132"
            instance = job_runner.template_instance_id(raw_slice_id)
            self.assertEqual(instance, "phase2-plan-manager-gitconfig-763-50f62414")

            repo = _source_repo(tmp_path)
            base = _git(repo, "rev-parse", "main")
            workspace = _workspace(repo, tmp_path / "pool")
            coordinator_root = tmp_path / "coordinator"
            job = {
                "job_id": job_id,
                "task": "bundle-623",
                "branch": _BRANCH,
                "worktree": str(tmp_path / "pool" / job_id),
                "session_name": raw_slice_id,
                "log_path": str(tmp_path / "logs" / f"{job_id}.jsonl"),
                "runtime_mode": "systemd-template",
                "template_instance": instance,
            }

            bundle = job_workspace.prepare_commit_spool(
                spool_key=raw_slice_id, coordinator_root=coordinator_root
            )
            expected_bundle = job_workspace.commit_bundle_path_for_job(
                job, coordinator_root=coordinator_root
            )
            self.assertEqual(expected_bundle, bundle)
            candidate = _builder_commit(workspace)
            bundle_result = _run_bundle_step(workspace, bundle)
            self.assertEqual(bundle_result.returncode, 0, bundle_result.stderr)

            sibling_bundle = job_workspace.prepare_commit_spool(
                spool_key=instance, coordinator_root=coordinator_root
            )
            self.assertEqual(
                sibling_bundle.parent.name,
                "phase2-plan-manager-gitconfig-763-50f62414-61200d73",
            )
            foreign_candidate = _builder_commit(workspace, name="foreign.txt")
            foreign_result = _run_bundle_step(workspace, sibling_bundle)
            self.assertEqual(foreign_result.returncode, 0, foreign_result.stderr)
            self.assertNotEqual(candidate, foreign_candidate)
            self.assertFalse(sibling_bundle.parent.samefile(bundle.parent))
            self.assertNotEqual(bundle, sibling_bundle)
            self.assertEqual(bundle.parent.name, instance)
            self.assertFalse(
                job_workspace.commit_bundle_path(
                    spool_key=job_id, coordinator_root=coordinator_root
                ).exists()
            )

            result = verification.run_result_verification(
                slice_row={
                    "slice_id": raw_slice_id,
                    "dispatch_base": base,
                    "verification": {"contract": _trivial_contract()},
                },
                job=job,
                repo_root=repo,
                coordinator_root=coordinator_root,
            )

            payload = result["payload"]
            self.assertEqual(payload["candidate"], candidate)
            harvested = _git(repo, "rev-parse", f"refs/heads/{_BRANCH}")
            self.assertEqual(harvested, candidate)
            self.assertNotEqual(harvested, foreign_candidate)
            self.assertFalse(
                job_workspace.commit_bundle_path(
                    spool_key=instance, coordinator_root=coordinator_root
                ).samefile(bundle)
            )
