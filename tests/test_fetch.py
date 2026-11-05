"""`helper fetch`: a real worker and runner, a scheduler, and a console's
control connection over a Unix socket, with no network."""

import hashlib
import json
import shlex
import threading
import time
import warnings

from pathlib import Path

import pytest

from rpyc.utils.helpers import classpartial
from rpyc.utils.server import ThreadedServer

import gxtest  # noqa: F401  (a temporary stats cache)
import ungem5.stats as gx

from ungem5.jobs.script import compile_objects
from ungem5.jobs.work import Experiment, Job
from ungem5.results.fetch import (
    DEFAULT_GLOBS,
    FetchError,
    SchedulerSource,
    fetch,
)
from ungem5.schedule.console import ConsoleError, ControlClient
from ungem5.schedule.daemon import ControlService, PeerUidAuthenticator
from ungem5.schedule.host import Host
from ungem5.schedule.runner import write_index
from ungem5.schedule.scheduler import Scheduler, current_user
from ungem5.schedule.state import socket_path
from ungem5.schedule.worker import PROTOCOL_CONFIG, Worker
from ungem5.cli.fetch import _process_fetch_args, parse_fetch_args

from test_scheduler import FakeConnection

SHAPES = Path(__file__).resolve().parent / "fixtures" / "shapes"
JOBS = 2
ME = current_user()


class WorkerRoot:
    """A real worker as a host's `conn.root`, called in this process."""

    def __init__(self, worker: Worker) -> None:
        self._worker = worker

    def __getattr__(self, name):
        return getattr(self._worker, f"exposed_{name}")


def job_command(outdir: Path, i: int) -> str:
    """What a gem5 job leaves behind, plus files fetch skips by default."""
    out = shlex.quote(str(outdir))
    params = shlex.quote(json.dumps({"workload": f"w{i}"}))
    return (
        f"cp {shlex.quote(str(SHAPES))}/config.json "
        f"{shlex.quote(str(SHAPES))}/stats.txt {out}/ && "
        f"echo {params} > {out}/params.json && "
        f"echo '{{\"ended_by\": \"roi\"}}' > {out}/window.json && "
        f"head -c 3000000 /dev/urandom > {out}/big.log && "
        f"touch {out}/simulation_finished"
    )


@pytest.fixture
def served(tmp_path):
    """A scheduler that ran two jobs to the end on a real worker, and a
    console's client connected to it."""
    worker = Worker(store=tmp_path / "store")
    host = Host("h", "x86_64", "localhost", 4, insecure=True)
    host._open = lambda: FakeConnection(WorkerRoot(worker))
    scheduler = Scheduler("test", polling_secs=0.05)
    scheduler._add_host(host)
    assert host.connect()
    experiment = Experiment("e", tmp_path / "out" / "e")
    for i in range(JOBS):
        outdir = tmp_path / "out" / "e" / str(i)
        experiment.register_job(
            Job("e", tmp_path, job_command(outdir, i), f"job {i}", outdir,
                None, f"e-{i}")
        )
    scheduler._handlers["load"](
        compiled=compile_objects("/p/s.py", "", [], [experiment], [host])
    )
    thread = threading.Thread(target=scheduler.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    for job in experiment.jobs():
        # NOTE: The runner indexes the outdir just after the job ends.
        while not _ended(scheduler, job.id()) or json.loads(
            worker.exposed_job_index(ME, str(job.outdir()))
        ) is None:
            assert time.time() < deadline, f"{job.id()} did not end"
            time.sleep(0.05)

    # NOTE: Where `helper fetch` looks for its user's scheduler; conftest
    # puts it under tmp_path.
    socket = socket_path()
    server = ThreadedServer(
        classpartial(ControlService, scheduler, None),
        socket_path=str(socket),
        authenticator=PeerUidAuthenticator(),
        protocol_config=PROTOCOL_CONFIG,
    )
    threading.Thread(target=server.start, daemon=True).start()
    client = ControlClient(socket)
    deadline = time.time() + 5
    while not client.connect():
        assert time.time() < deadline
        time.sleep(0.05)
    try:
        yield client, tmp_path / "out" / "e"
    finally:
        client.close()
        server.close()
        scheduler.stop()
        thread.join(5)


def _ended(scheduler: Scheduler, job_id: str) -> bool:
    jobs = {job["id"]: job for job in scheduler.snapshot().jobs}
    return job_id in jobs and jobs[job_id]["status"] == "exited"


class Counting(SchedulerSource):
    """Counts the bytes that cross the control connection."""

    def __init__(self, client) -> None:
        super().__init__(client)
        self.moved = 0

    def read_file(self, job_id, path, offset, length):
        data = super().read_file(job_id, path, offset, length)
        self.moved += len(data)
        return data


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


DEFAULT_FILES = {
    "config.json", "stats.txt", "params.json", "simulation_finished",
    "window.json", ".job/exit.json", ".job/launch.json",
}


def test_fetch_copies_what_results_and_stats_read(served, tmp_path):
    client, source_dir = served
    into = tmp_path / "fetched"
    lines = []
    results = fetch(Counting(client), ["e"], into, say=lines.append)

    assert [r.job_id for r in results] == ["e-0", "e-1"]
    assert all(r.error is None and r.skipped is None for r in results)
    for i in range(JOBS):
        source, local = source_dir / str(i), into / "e" / str(i)
        got = {
            str(p.relative_to(local)) for p in local.rglob("*") if p.is_file()
        }
        assert got == DEFAULT_FILES
        for name in got:
            assert _sha(local / name) == _sha(source / name), name
            assert (local / name).stat().st_mtime_ns == pytest.approx(
                (source / name).stat().st_mtime_ns, abs=1000
            ), name
    assert len(lines) == JOBS + 1
    assert lines[0].startswith("e-0: fetched 7 of 7 file(s), ")
    assert lines[-1].startswith("total: fetched 14 of 14 file(s), ")

    # NOTE: The stats layer reads the copy as it reads the outdir.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        here = gx.RunSet.from_experiment(into / "e")["numCycles"].rows()
        there = gx.RunSet.from_experiment(source_dir)["numCycles"].rows()
    assert len(here) == JOBS and here == there


def test_a_second_fetch_moves_nothing_and_a_change_moves_again(
    served, tmp_path
):
    client, source_dir = served
    into = tmp_path / "fetched"
    fetch(SchedulerSource(client), ["e"], into, say=lambda line: None)
    local = into / "e" / "0" / "stats.txt"
    cached_mtime = local.stat().st_mtime_ns

    again = Counting(client)
    lines = []
    results = fetch(again, ["e"], into, say=lines.append)
    assert again.moved == 0
    assert [r.fetched for r in results] == [0, 0]
    assert lines[-1].startswith("total: fetched 0 of 14 file(s), 0B")
    assert local.stat().st_mtime_ns == cached_mtime

    # NOTE: The job ran again and changed one file: only it moves.
    source = source_dir / "0" / "stats.txt"
    source.write_text(source.read_text().replace(" 1000 ", " 2000 ", 1))
    write_index(source_dir / "0")
    changed = Counting(client)
    results = fetch(changed, ["e"], into, say=lambda line: None)
    assert [r.fetched for r in results] == [1, 0]
    assert changed.moved == source.stat().st_size
    assert local.read_bytes() == source.read_bytes()
    assert local.stat().st_mtime_ns == pytest.approx(
        source.stat().st_mtime_ns, abs=1000
    )

    # NOTE: A local copy that was edited is fetched again.
    (into / "e" / "1" / "params.json").write_text("{}")
    results = fetch(SchedulerSource(client), ["e-1"], into, say=lambda l: None)
    assert [r.fetched for r in results] == [1]
    assert (into / "e" / "1" / "params.json").read_bytes() == (
        source_dir / "1" / "params.json"
    ).read_bytes()


def test_match_adds_files_and_leaves_the_rest(served, tmp_path):
    client, _ = served
    into = tmp_path / "fetched"
    fetch(
        SchedulerSource(client), ["e-0"], into,
        DEFAULT_GLOBS + ("stdout",), say=lambda line: None,
    )
    local = into / "e" / "0"
    got = {str(p.relative_to(local)) for p in local.rglob("*") if p.is_file()}
    # NOTE: The 3 MB log, stderr and the runner's other records stay on
    # the host.
    assert got == DEFAULT_FILES | {"stdout"}
    assert not (into / "e" / "1").exists()

    # NOTE: Globs alone, without the defaults; as in the console's `files
    # --match`, "*" also matches "/".
    only = tmp_path / "only"
    fetch(SchedulerSource(client), ["e-1"], only, ("*.log",), say=print)
    local = only / "e" / "1"
    got = {str(p.relative_to(local)) for p in local.rglob("*") if p.is_file()}
    assert got == {"big.log", ".job/runner.log"}


def test_unknown_names_fail_with_the_consoles_words(served, tmp_path):
    client, _ = served
    source = SchedulerSource(client)
    with pytest.raises(
        ConsoleError, match="no experiment named 'nope', and no job id "
        "starts with 'nope'"
    ):
        fetch(source, ["nope"], tmp_path / "x", say=lambda line: None)
    with pytest.raises(ConsoleError, match="'e-' matches 2 jobs"):
        fetch(source, ["e-"], tmp_path / "x", say=lambda line: None)
    with pytest.raises(FetchError, match="no job"):
        source.job_index("e-9")
    assert not (tmp_path / "x").exists()


def test_helper_fetch_reaches_its_users_scheduler(served, tmp_path, capsys):
    into = tmp_path / "fetched"

    def helper_fetch(*args):
        return _process_fetch_args(*parse_fetch_args(list(args)))

    assert helper_fetch("e", "--into", str(into), "--match", "stdout") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("e-0: fetched 8 of 8 file(s), ")
    assert out[-1].startswith("total: fetched 16 of 16 file(s), ")
    assert (into / "e" / "1" / "stdout").is_file()
    assert helper_fetch("e", "--into", str(into)) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith(
        "total: fetched 0 of 14 file(s), 0B"
    )
    assert helper_fetch("nope", "--into", str(into)) == 1
    assert "no experiment named 'nope'" in capsys.readouterr().err


def test_two_jobs_that_would_land_in_one_place_are_refused(tmp_path):
    class Clash:
        def resolve(self, targets):
            return [
                {"id": f"{user}/e-0", "outdir": "/x/e/0", "host": "h",
                 "status": "exited", "experiment": f"{user}/e"}
                for user in ("alice", "bob")
            ]

    with pytest.raises(FetchError, match="would both land in"):
        fetch(Clash(), ["e"], tmp_path, say=lambda line: None)


def test_index_paths_never_leave_the_job_directory(tmp_path):
    class Forged:
        def resolve(self, targets):
            return [{"id": "e-0", "outdir": "/x/e/0", "host": "h",
                     "status": "exited", "experiment": "e"}]

        def job_index(self, job_id):
            return {"files": {"../../escape.json": [2, 1.0, "0" * 64]}}

    (result,) = fetch(
        Forged(), ["e"], tmp_path / "in", ("*.json",), say=lambda l: None
    )
    assert "not a path inside" in result.error
    assert not (tmp_path / "escape.json").exists()
