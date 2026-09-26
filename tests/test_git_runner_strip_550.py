from types import SimpleNamespace

from paulsha_cortex.coordinator import dispatcher, worktree_reclaim
from paulsha_cortex.config import paths


PORCELAIN_OUTPUT = " M README.md\n"


class _Registry:
    def __init__(self, job=None):
        self.job = job

    def create_job(self, **fields):
        self.job = {"status": "dispatched", **fields}
        return self.job

    def get_job(self, _job_id):
        return self.job

    def update_status(self, _job_id, status):
        self.job["status"] = status
        return self.job


class _Sender:
    def send(self, *_args):
        pass


class _WorktreeCreator:
    def create(self, branch, *, job_id=None):
        return f"worktree/{job_id or branch}"


def test_default_git_runner_preserves_leading_porcelain_space(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=PORCELAIN_OUTPUT, stderr=""
        ),
    )

    assert dispatcher._default_git_runner(["status", "--porcelain"]) == PORCELAIN_OUTPUT


def test_pinned_git_runner_preserves_leading_porcelain_space(monkeypatch, tmp_path):
    monkeypatch.setattr(
        worktree_reclaim.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=PORCELAIN_OUTPUT, stderr=""
        ),
    )

    runner = worktree_reclaim._pinned_git_runner(tmp_path)

    assert runner(["status", "--porcelain"]) == PORCELAIN_OUTPUT


def test_dispatch_strips_rev_parse_output():
    sha = "a" * 40
    dispatcher_instance = dispatcher.Dispatcher(
        _Registry(), _Sender(), _WorktreeCreator(), git_runner=lambda _args: sha + "\n"
    )

    job = dispatcher_instance.dispatch(
        task="strip-test", persona="builder", pane_id="pane", command="run"
    )

    assert job["dispatch_head"] == sha


def test_poll_done_strips_rev_parse_output():
    sha = "b" * 40
    registry = _Registry(
        {"job_id": "strip-test", "branch": "feature/strip-test", "dispatch_head": sha, "status": "dispatched"}
    )
    dispatcher_instance = dispatcher.Dispatcher(
        registry, _Sender(), _WorktreeCreator(), git_runner=lambda _args: sha + "\n"
    )

    result = dispatcher_instance.poll_done("strip-test")

    assert result["status"] == "dispatched"
