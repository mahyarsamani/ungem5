"""Enforcing memory: grace, pausing, `on_memory_exceed`, `resume` and
`retry`, low host memory, and the kernel's backstop."""

import json
import signal
import time

import psutil

from experiment.api.resources import Resources
from experiment.api.scheduler.scheduler import current_user
from experiment.api.userio import resolve_status
from experiment.api.work import Experiment, Job, JobStatus
from experiment.api.worker import Worker

from test_memory import MemoryRoot
from test_scheduler import install, make_scheduler, run

G = 1 << 30
ME = current_user()


def setup(tmp_path, limit="20G", cls=Job, count=1):
    """A host whose jobs may hold `limit`, and `count` jobs that guess
    4G (so reserve 5G)."""
    root = MemoryRoot(cores=8, total=100 * G)
    scheduler, host = make_scheduler(tmp_path, root, [], capacity=8)
    host.set_limits(memory=limit)
    experiment = Experiment("e", tmp_path / "e")
    for i in range(count):
        experiment.register_job(
            cls("e", tmp_path, f"c{i}", f"c{i}", tmp_path / "e" / str(i),
                Resources(memory=4 * G), f"j{i}")
        )
    install(scheduler, experiment)
    scheduler._tick()
    return scheduler, root


def grow(scheduler, root, job_id, now):
    job = scheduler._jobs[job_id]
    root.memory[str(job.outdir())] = (now, now)
    scheduler._tick()
    return job


def test_a_job_that_outgrows_its_reservation_gets_more_if_there_is_room(
    tmp_path,
):
    scheduler, root = setup(tmp_path)
    job = grow(scheduler, root, "j0", 6 * G)
    assert job.status() == JobStatus.RUNNING
    assert job.reserved() == int(6 * G * 1.25)


def test_without_room_it_pauses_until_resumed(tmp_path):
    scheduler, root = setup(tmp_path, limit="6G")
    job = grow(scheduler, root, "j0", 6 * G)
    assert job.status() == JobStatus.PAUSED and job.paused_for() == "memory"
    assert root.frozen[str(job.outdir())] is True
    assert "no room" in job.message()
    # NOTE: The worker still sees it alive; it stays paused.
    scheduler._tick()
    assert job.status() == JobStatus.PAUSED
    assert run(scheduler, "resume", jobs=["j0"]) == ["j0: resumed"]
    assert job.status() == JobStatus.RUNNING
    assert root.frozen[str(job.outdir())] is False
    assert job.reserved() == 6 * G


class Killed(Job):
    on_memory_exceed = "kill"


class Retried(Job):
    on_memory_exceed = "retry"


def test_on_memory_exceed_kill_fails_the_job(tmp_path):
    scheduler, root = setup(tmp_path, limit="6G", cls=Killed)
    job = grow(scheduler, root, "j0", 6 * G)
    assert job.status() == JobStatus.FAILED
    assert job.message().startswith("memory:")
    assert root.signals[-1][1] == signal.SIGKILL


def test_on_memory_exceed_retry_runs_it_again_with_more(tmp_path):
    scheduler, root = setup(tmp_path, limit="7G", cls=Retried)
    job = grow(scheduler, root, "j0", 6 * G)
    # NOTE: Killed and queued with 7.5G, more than this host's 7G.
    assert job.status() == JobStatus.QUEUED
    assert "retry with more memory" in job.message()
    assert scheduler.snapshot().jobs[0]["estimate"] == int(6 * G * 1.25)
    scheduler._hosts["h"].set_limits(memory="8G")
    root.status[str(job.outdir())] = "missing"
    scheduler._tick()
    assert job.status() == JobStatus.RUNNING
    assert job.reserved() == int(6 * G * 1.25)


def test_retry_with_more_memory(tmp_path):
    scheduler, root = setup(tmp_path, limit="40G")
    lines = run(scheduler, "retry", jobs=["j0"], memory="16G")
    assert lines == ["j0: queued again with at least 16G"]
    job = scheduler._jobs["j0"]
    assert root.signals[-1][1] == signal.SIGKILL
    root.status[str(job.outdir())] = "missing"
    scheduler._tick()
    assert job.reserved() == 16 * G
    assert scheduler.snapshot().jobs[0]["reserved"] == 16 * G


def test_a_host_low_on_memory_pauses_its_newest_job(tmp_path):
    scheduler, root = setup(tmp_path, count=2)
    jobs = [scheduler._jobs["j0"], scheduler._jobs["j1"]]
    for i, job in enumerate(jobs):
        root.starts[str(job.outdir())] = 100.0 + i
    host = scheduler._hosts["h"]
    root.available = 1 * G  # NOTE: under 3% of 100G
    host.refresh_resources(force=True)
    scheduler._tick()
    assert [j.status() for j in jobs] == [JobStatus.RUNNING, JobStatus.PAUSED]
    assert jobs[1].paused_for() == "host"
    # NOTE: Once per report, not once per tick.
    scheduler._tick()
    assert jobs[0].status() == JobStatus.RUNNING
    root.available = 50 * G
    host.refresh_resources(force=True)
    scheduler._tick()
    assert jobs[1].status() == JobStatus.RUNNING


def test_partitions_cap_each_users_slice(tmp_path):
    scheduler, root = setup(tmp_path, count=0)
    run(scheduler, "partition", host="h", user=ME, memory="10G")
    assert root.caps[ME] == int(10 * G * 1.1)
    run(scheduler, "partition", host="h", user=ME, clear=True)
    assert root.caps[ME] is None


def test_a_users_worker_freezes_by_stopping_the_process_group(tmp_path):
    worker = Worker()
    outdir = tmp_path / "job"
    worker.exposed_launch_job(
        "j", ME, str(tmp_path), "sleep 30", str(outdir), (), ()
    )
    pid = json.loads((outdir / ".job" / "launch.json").read_text())["pid"]
    assert worker.exposed_freeze_job(ME, str(outdir), True)
    deadline = time.time() + 5
    while psutil.Process(pid).status() != psutil.STATUS_STOPPED:
        assert time.time() < deadline
        time.sleep(0.05)
    assert resolve_status(outdir)[0] == "running"
    assert worker.exposed_freeze_job(ME, str(outdir), False)
    assert worker.exposed_signal_job(ME, str(outdir), int(signal.SIGKILL))
    # NOTE: Not a worker run by root, so no slices to cap.
    assert worker.exposed_set_user_memory(ME, 1 * G) is False
