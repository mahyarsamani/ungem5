"""Load scripts are compiled to data by the console; the scheduler only
ever sees that data."""

from pathlib import Path

import pytest

from experiment.api.resources import Resources
from experiment.api.scheduler.console import Console, ConsoleError
from experiment.api.scheduler.scheduler import Scheduler
from experiment.api.scheduler.state import StateStore
from experiment.api.script import (
    ScriptError,
    compile_in_subprocess,
    compile_objects,
)
from experiment.api.work import Experiment, Job, JobStatus, LoadedJob

from test_scheduler import FakeRoot, make_host, run

SCRIPT = """
from pathlib import Path
from experiment.api.host import Host
from experiment.api.work import Experiment, Job
from sizes import COMMAND

worker = Host("w1", "x86_64", "localhost", 4, "8G", insecure=True)
e = Experiment("e", Path("{tmp}/e"))
for i in range(2):
    e.register_job(Job("e", Path("{tmp}"), f"{{COMMAND}} {{i}}", "c",
                       Path("{tmp}/e/{{i}}"), None, f"e-{{i}}"))
"""


def write(tmp_path: Path, command: str) -> Path:
    # NOTE: The script imports a module next to it, like job_defs.py.
    (tmp_path / "sizes.py").write_text(f"COMMAND = {command!r}\n")
    script = tmp_path / "jobs.py"
    script.write_text(SCRIPT.format(tmp=tmp_path))
    return script


def commands(scheduler):
    return sorted(job.command() for job in scheduler._jobs.values())


class Watched(Job):
    nice = 5
    max_retries = 1

    def watcher(self):
        return "echo ok"

    def features(self):
        return {"size": 3}

    def memory_group(self):
        return "watched"

    def compatible_with_host(self, host):
        return host.isa() == "aarch64"


def test_a_loaded_job_says_what_the_job_said(tmp_path):
    parent = Job("e", tmp_path, "p", "p", tmp_path / "p", None, "p")
    job = Watched(
        "e", tmp_path, "run", "r", tmp_path / "r", Resources(2, "3G"), "r",
        after=[parent],
    )
    loaded = LoadedJob(job.compile(["a1"]))
    assert loaded.files() == job.files() and loaded.dumps() == job.dumps()
    assert (loaded.features(), loaded.memory_group()) == ({"size": 3}, "watched")
    assert (loaded.cores(), loaded.memory_guess()) == (2, 3 << 30)
    assert (loaded.nice, loaded.max_retries) == (5, 1)
    assert loaded.dependency_ids() == (["p"], [])
    arm = make_host(FakeRoot(), name="a1", isa="aarch64")
    other = make_host(FakeRoot(), name="a2", isa="aarch64")
    # NOTE: Only the hosts it was compiled against.
    assert loaded.compatible_with_host(arm)
    assert not loaded.compatible_with_host(other)


def test_scripts_compile_in_a_fresh_interpreter(tmp_path):
    script = write(tmp_path, "old")
    compiled = compile_in_subprocess(script, [])
    assert compiled["script"] == str(script.resolve())
    assert [h["name"] for h in compiled["hosts"]] == ["w1"]
    jobs = compiled["experiments"][0]["jobs"]
    assert [j["command"] for j in jobs] == ["old 0", "old 1"]
    assert jobs[0]["hosts"] == ["w1"]

    # NOTE: An imported module's edit shows up on the next compile.
    write(tmp_path, "new")
    jobs = compile_in_subprocess(script, [])["experiments"][0]["jobs"]
    assert jobs[0]["command"] == "new 0"

    script.write_text("raise RuntimeError('broken script')\n")
    with pytest.raises(ScriptError, match="failed"):
        compile_in_subprocess(script, [])


def test_the_scheduler_never_reads_the_script(tmp_path):
    store = StateStore("test")
    scheduler = Scheduler("test", polling_secs=0, store=store)
    script = write(tmp_path, "old")
    compiled = compile_in_subprocess(script, [])
    script.unlink()
    lines = run(scheduler, "load", compiled=compiled)
    assert "added host w1" in lines and "e: 2 new job(s)" in lines
    scheduler._persist(force=True)

    # NOTE: A restart restores the compiled jobs; nothing runs again.
    resumed = Scheduler("test", polling_secs=0, store=store)
    resumed.resume(store.load())
    assert commands(resumed) == ["old 0", "old 1"]


def test_a_reload_updates_jobs_that_are_not_running(tmp_path):
    root = FakeRoot()
    scheduler = Scheduler("test", polling_secs=0)
    host = make_host(root, name="w1", capacity=1)
    scheduler._add_host(host)
    assert host.connect()

    def define(command):
        experiment = Experiment("e", tmp_path / "e")
        for i in range(3):
            experiment.register_job(
                Job("e", tmp_path, f"{command} {i}", "c", tmp_path / "e" / str(i),
                    None, f"e-{i}")
            )
        return compile_objects(
            "s.py", "", [], [experiment], scheduler._hosts.values()
        )

    run(scheduler, "load", compiled=define("old"))
    scheduler._tick()
    jobs = scheduler._jobs
    # NOTE: One core: e-0 runs, then finishes; e-1 runs, e-2 waits.
    root.status[str(jobs["e-0"].outdir())] = "exited"
    scheduler._tick()
    scheduler._tick()
    assert [jobs[i].status() for i in ("e-0", "e-1", "e-2")] == [
        JobStatus.EXITED, JobStatus.RUNNING, JobStatus.QUEUED,
    ]
    lines = run(scheduler, "reload", compiled=define("new"))
    assert "e: 0 new job(s), 2 updated, 1 running with their old definition" in lines
    jobs = scheduler._jobs
    assert [jobs[i].command() for i in ("e-0", "e-1", "e-2")] == [
        "new 0", "old 1", "new 2",
    ]
    # NOTE: An update keeps the job's status; `reset` reruns it.
    assert jobs["e-0"].status() == JobStatus.EXITED
    assert run(scheduler, "reload", compiled=define("new"))[0] == (
        "e: 0 new job(s), 1 running with their old definition"
    )


class FakeClient:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.calls = []

    def snapshot(self, max_age=1.0):
        return self._snapshot

    def execute(self, name, **kwargs):
        self.calls.append((name, kwargs))
        return ["ok"]


def test_the_console_sends_compiled_data(tmp_path, capsys):
    script = write(tmp_path, "c")
    view = {"name": "x9", "isa": "x86_64", "domain": "x9", "cores": 8,
            "memory": 1 << 34}
    client = FakeClient({"info": {"scripts": []}, "hosts": [view]})
    console = Console(client, {})
    console._load("load", script.resolve())
    ((name, kwargs),) = client.calls
    assert name == "load"
    jobs = kwargs["compiled"]["experiments"][0]["jobs"]
    # NOTE: Jobs may run on the scheduler's hosts and the script's own.
    assert jobs[0]["hosts"] == ["w1", "x9"]

    client._snapshot["info"]["scripts"] = [str(script.resolve())]
    with pytest.raises(ConsoleError, match="already loaded"):
        console._load("load", script.resolve())
