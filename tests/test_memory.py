"""Cores and memory: sizes, the runner's measurements, the estimator, and
placement by memory."""

import json
import sys
import time

from pathlib import Path

import pytest

from ungem5.jobs.resources import Resources, format_size, parse_size
from ungem5.jobs.work import Experiment, Job, JobStatus
from ungem5.schedule.host import Host
from ungem5.schedule.memory import MemoryHistory
from ungem5.schedule.scheduler import current_user
from ungem5.schedule.state import StateStore
from ungem5.schedule.userio import resolve_status
from ungem5.schedule.worker import Worker

from test_scheduler import FakeRoot, make_scheduler, run

G = 1 << 30


def test_sizes():
    assert parse_size("16G") == 16 * G
    assert parse_size("512MiB") == 512 << 20
    assert parse_size("1.5t") == int(1.5 * (1 << 40))
    assert parse_size(1234) == 1234 and parse_size(None) is None
    with pytest.raises(ValueError):
        parse_size("lots")
    assert format_size(12 * G) == "12G" and format_size(int(1.5 * G)) == "1.5G"
    assert Resources(memory="2G").memory == 2 * G
    with pytest.raises(TypeError, match="Resources"):
        Resources.of(2)
    with pytest.raises(ValueError):
        Resources(cores=0)


def job(name, guess=None, cores=1, command=None, experiment="e", tmp=Path("/tmp")):
    return Job(
        experiment, tmp, command or name, name, tmp / experiment / name,
        Resources(cores=cores, memory=guess), name,
    )


def ran(history, j, peak, outcome="exited", start=None):
    """Pretend `j` ran and peaked at `peak`."""
    j.assign("h", None)
    j.set_times(start or time.time(), time.time())
    j.set_memory(None, peak)
    assert history.record(j, "h", outcome, [], 0, 0)


def test_estimate_layers():
    history = MemoryHistory(None, default_memory=8 * G)
    fresh = job("a", guess=4 * G)
    assert history.estimate(job("x")).source == "default"
    assert history.estimate(job("x")).memory == 8 * G
    estimate = history.estimate(fresh)
    assert (estimate.source, estimate.memory) == ("guess", 5 * G)

    # NOTE: Three runs of the group guessed 4G and used 6G: guesses in
    # this group are 1.5x short.
    for i in range(3):
        ran(history, job(f"g{i}", guess=4 * G, command=f"c{i}"), 6 * G)
    estimate = history.estimate(job("b", guess=2 * G, command="new"))
    assert estimate.source == "corrected guess"
    assert estimate.memory == int(2 * G * 1.5 * 1.15)
    # NOTE: No guess: the group's largest peak.
    assert history.estimate(job("c", command="other")).source == "group"

    # NOTE: The same job again: its own history wins over its guess.
    again = job("g0", guess=100 * G, command="c0")
    estimate = history.estimate(again)
    assert (estimate.source, estimate.memory) == ("history", int(6 * G * 1.1))


def test_cut_short_runs_only_raise_the_floor():
    history = MemoryHistory(None, default_memory=1 * G)
    killed = job("k", command="same")
    ran(history, killed, 3 * G, outcome="killed")
    estimate = history.estimate(job("k", command="same"))
    assert estimate.memory == 3 * G and "raised" in estimate.source
    # NOTE: A killed run says nothing about the group.
    assert history.estimate(job("z", command="else")).source == "default"


def test_history_survives_a_reopen(tmp_path):
    path = tmp_path / "memory.sqlite"
    history = MemoryHistory(path, default_memory=1 * G)
    j = job("a", command="same")
    ran(history, j, 2 * G, start=100.0)
    # NOTE: The same run is kept once.
    assert not history.record(j, "h", "exited", [], 0, 0)
    history.close()
    reopened = MemoryHistory(path, default_memory=1 * G)
    assert reopened.estimate(job("a", command="same")).source == "history"
    assert reopened.runs("a")[0]["peak"] == 2 * G


def wait_ended(outdir, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = resolve_status(outdir)
        if status[0] not in ("running", "missing"):
            return status
        time.sleep(0.1)
    raise AssertionError(f"still {status}")


class Sampled(Job):
    memory_interval = 1
    nice = 7


def test_runner_measures_memory_and_applies_nice(tmp_path):
    outdir = tmp_path / "j"
    # NOTE: Holds about 200 MB for 2.5 s, and notes its nice value.
    command = (
        "ps -o ni= -p $$ > nice.txt; "
        f"{sys.executable} -c 'import time; b = bytearray(200 << 20); "
        "b[::4096] = b\"x\" * len(b[::4096]); time.sleep(2.5)'"
    )
    j = Sampled("e", tmp_path, command, "hog", outdir, None, "hog")
    Worker().exposed_launch_job(
        j.id(), current_user(), str(outdir), j.command(), str(outdir),
        tuple((label, str(p)) for label, p in j.files()),
        tuple((content, str(p)) for _, content, p in j.dumps()),
    )
    time.sleep(1.8)
    running = resolve_status(outdir)
    assert running[0] == "running"
    assert running[5] is not None and running[5][0] > 100 << 20
    status, *_, memory = wait_ended(outdir)
    assert status == "exited"
    now, peak = memory
    assert now is None and 200 << 20 <= peak < 1 * G
    trace = (outdir / ".job" / "memory.jsonl").read_text().splitlines()
    assert len(trace) >= 1 and json.loads(trace[0])["rss"] > 0
    assert (outdir / "nice.txt").read_text().strip() == "7"
    assert Worker().exposed_memory_trace(current_user(), str(outdir)).count(
        b"\n"
    ) == len(trace)


class MemoryRoot(FakeRoot):
    """A fake worker that reports memory: `memory` maps outdirs to their
    (now, peak), and the machine has `total` bytes."""

    def __init__(self, cores=8, total=100 * G) -> None:
        super().__init__()
        self.memory: dict[str, tuple] = {}
        self.cores, self.total, self.available = cores, total, total
        # NOTE: outdir -> when its job started (1.0 if not given).
        self.starts: dict[str, float] = {}

    def host_resources(self):
        return self.cores, self.total, self.available

    def job_statuses(self, jobs):
        self._check()
        return tuple(
            (
                self.status.get(o, "missing"), 0, None,
                self.starts.get(o, 1.0), None, self.memory.get(o),
            )
            for _, o in jobs
        )

    def memory_trace(self, user, outdir):
        return b'{"time": 1, "rss": 5}\n'


def experiment(n, tmp_path, guess=None):
    e = Experiment("e", tmp_path / "e")
    for i in range(n):
        e.register_job(job(f"j{i}", guess=guess, command=f"c{i}", tmp=tmp_path))
    return e


def test_memory_limits_placement(tmp_path):
    root = MemoryRoot(cores=8, total=100 * G)
    e = experiment(3, tmp_path, guess=4 * G)
    scheduler, host = make_scheduler(tmp_path, root, [e], capacity=8)
    # NOTE: 5G reserved per job (4G guess + 25%): two fit in 12G.
    host.set_limits(memory="12G")
    scheduler._tick()
    assert [j.status() for j in e.jobs()].count(JobStatus.RUNNING) == 2
    assert {j.reserved() for j in e.jobs() if j.reserved()} == {5 * G}
    view = scheduler.snapshot().hosts[0]
    assert (view["memory"], view["memory_used"]) == (12 * G, 10 * G)

    # NOTE: A job growing past its reservation holds what it uses.
    first = e.jobs()[0]
    root.memory[str(first.outdir())] = (7 * G, 7 * G)
    scheduler._tick()
    assert scheduler._usage()["h"]["memory"] == 12 * G
    assert first.memory_now() == 7 * G


def test_finished_runs_teach_the_next(tmp_path):
    root = MemoryRoot()
    e = experiment(1, tmp_path, guess=40 * G)
    store = StateStore("test")
    scheduler, _ = make_scheduler(tmp_path, root, [e], store=store, capacity=8)
    scheduler._tick()
    (only,) = e.jobs()
    root.status[str(only.outdir())] = "exited"
    root.memory[str(only.outdir())] = (None, 3 * G)
    scheduler._tick()
    assert only.status() == JobStatus.EXITED
    lines = run(scheduler, "memory", job="j0")
    assert "peak 3.0G" in lines[1] and "1 recorded run(s)" in lines[2]
    run(scheduler, "reset", jobs=["j0"])
    scheduler._tick()
    # NOTE: Its own run wins over the 40G guess.
    assert only.reserved() == int(3 * G * 1.1)
    assert (store.directory() / "memory.sqlite").exists()


def test_a_job_bigger_than_every_host_says_so(tmp_path):
    root = MemoryRoot(total=10 * G)
    e = experiment(1, tmp_path, guess=40 * G)
    scheduler, _ = make_scheduler(tmp_path, root, [e], capacity=8)
    scheduler._tick()
    (view,) = scheduler.snapshot().jobs
    assert view["status"] == "queued"
    assert "more than any compatible host offers" in view["message"]
    assert view["estimate_source"] == "guess"


def test_host_limits_round_trip():
    host = Host("h", "x86_64", "localhost", 4, "64G", insecure=True)
    assert host.spec()["cores"] == 4 and host.spec()["memory"] == 64 * G
    assert Host.from_spec(host.spec()).memory() == 64 * G
    # NOTE: Without a limit or a report, memory isn't limited.
    assert Host("h", "x86_64", "localhost", insecure=True).memory() is None


def test_capacity_command(tmp_path):
    scheduler, host = make_scheduler(tmp_path, MemoryRoot(cores=16), [], capacity=4)
    assert run(scheduler, "capacity", host="h", cores="auto", memory="50G") == [
        "host h: 16 core(s), 50G for jobs"
    ]
    run(scheduler, "capacity", host="h", memory="auto")
    assert host.memory() == int(100 * G * 0.9)
