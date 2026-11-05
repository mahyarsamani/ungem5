"""A lab: one scheduler for many users, and workers run by root."""

import io
import json
import os
import socket
import sys
import time

import pytest

from rpyc.utils.authenticators import AuthenticationError

import ungem5.schedule.worker as worker_module

from ungem5.jobs.resources import Resources
from ungem5.jobs.script import compile_objects
from ungem5.jobs.work import Experiment, Job, JobStatus
from ungem5.schedule.daemon import ControlService, PeerUidAuthenticator
from ungem5.schedule.dashboard import COOKIE, Dashboard
from ungem5.schedule.host import Host
from ungem5.schedule.scheduler import (
    CommandError,
    Scheduler,
    current_user,
)
from ungem5.schedule.userio import HelperIO, LocalIO, resolve_status, serve
from ungem5.schedule.worker import Worker

from test_memory import MemoryRoot
from test_scheduler import make_host

G = 1 << 30
ME = current_user()


def lab_scheduler(root=None):
    scheduler = Scheduler("lab", polling_secs=0, lab=True, admins=["boss"])
    root = root or MemoryRoot(cores=64, total=100 * G)
    host = make_host(root, capacity=None)
    scheduler._add_host(host)
    assert host.connect()
    return scheduler, root


def compiled(tmp_path, count, guess=None, name="e", hosts=()):
    experiment = Experiment(name, tmp_path / name)
    for i in range(count):
        experiment.register_job(
            Job(name, tmp_path, f"c{i}", f"c{i}", tmp_path / name / str(i),
                Resources(memory=guess), f"{name}-{i}")
        )
    known = [Host("h", "x86_64", "localhost", insecure=True)]
    return compile_objects("/p/s.py", "", list(hosts), [experiment], known)


def _run_as(scheduler, who, name, **kwargs):
    """Run a command as user `who` on the calling thread."""
    scheduler._caller = scheduler.caller(who)
    try:
        return scheduler._handlers[name](**kwargs)
    finally:
        scheduler._caller = scheduler._itself


def test_users_have_namespaces_and_change_only_their_own(tmp_path):
    scheduler, root = lab_scheduler()
    # NOTE: Two users load the same script: their jobs don't collide.
    _run_as(scheduler, "alice", "load", compiled=compiled(tmp_path, 2))
    _run_as(scheduler, "bob", "load", compiled=compiled(tmp_path, 2))
    assert sorted(scheduler._jobs) == [
        "alice/e-0", "alice/e-1", "bob/e-0", "bob/e-1",
    ]
    assert set(scheduler._experiments) == {"alice/e", "bob/e"}
    scheduler._tick()
    # NOTE: Jobs run as whoever loaded them.
    assert sorted(root.users) == ["alice", "alice", "bob", "bob"]

    # NOTE: A short name means one's own job; another's needs its full name.
    assert _run_as(scheduler, "alice", "signal", jobs=["e-0"], signal="kill") == [
        "alice/e-: sent kill"
    ]
    assert _run_as(
        scheduler, "alice", "signal", jobs=["bob/e-0"], signal="kill"
    ) == ["bob/e-0: not yours"]
    with pytest.raises(CommandError, match="bob's"):
        _run_as(scheduler, "alice", "kill", experiment="bob/e")
    with pytest.raises(CommandError, match="bob's"):
        _run_as(scheduler, "alice", "delete", script="bob:/p/s.py")
    # NOTE: Admins may change anyone's.
    assert _run_as(scheduler, "boss", "kill", experiment="bob/e") == [
        "killing experiment bob/e"
    ]


def test_only_admins_change_hosts_and_the_scheduler(tmp_path):
    scheduler, _ = lab_scheduler()
    for name, kwargs in [
        ("drain", {"host": "h"}),
        ("capacity", {"host": "h", "cores": "4"}),
        ("partition", {"host": "h", "user": "alice", "cores": "4"}),
        ("stop", {}),
    ]:
        with pytest.raises(CommandError, match="only an admin"):
            _run_as(scheduler, "alice", name, **kwargs)
    new_host = make_host(MemoryRoot(), name="other")
    with pytest.raises(CommandError, match="only an admin may add hosts"):
        _run_as(
            scheduler, "alice", "load",
            compiled=compiled(tmp_path, 1, hosts=[new_host]),
        )
    assert "added host other" in _run_as(
        scheduler, "boss", "load",
        compiled=compiled(tmp_path, 1, hosts=[new_host]),
    )


def test_partitions_lend_only_what_others_leave_idle(tmp_path):
    scheduler, _ = lab_scheduler()
    for user in ("alice", "bob"):
        _run_as(scheduler, "boss", "partition", host="h", user=user, memory="10G")
    # NOTE: Each job reserves 1G (a 0.8G guess plus 25%).
    _run_as(scheduler, "alice", "load", compiled=compiled(tmp_path, 12, 0.8 * G))
    scheduler._tick()
    running = [j for j in scheduler._jobs.values() if j.status().active()]
    # NOTE: 10G of her own, and 10% more borrowed from bob's idle share.
    assert len(running) == 11

    # NOTE: Once bob uses his share, there is nothing left to borrow.
    scheduler2, _ = lab_scheduler()
    for user in ("alice", "bob"):
        _run_as(scheduler2, "boss", "partition", host="h", user=user, memory="10G")
    _run_as(scheduler2, "bob", "load", compiled=compiled(tmp_path, 10, 0.8 * G))
    _run_as(scheduler2, "alice", "load", compiled=compiled(tmp_path, 12, 0.8 * G))
    scheduler2._tick()
    running = [j.owner() for j in scheduler2._jobs.values() if j.status().active()]
    assert running.count("bob") == 10 and running.count("alice") == 10
    view = scheduler2.snapshot().hosts[0]
    assert view["partitions"]["alice"] == {"cores": None, "memory": 10 * G}

    # NOTE: Without a share of their own or a share for everyone, a user
    # can't run on a partitioned host.
    _run_as(scheduler2, "carol", "load", compiled=compiled(tmp_path, 1, 0.8 * G))
    scheduler2._tick()
    assert scheduler2._jobs["carol/e-0"].status() == JobStatus.QUEUED
    _run_as(scheduler2, "boss", "partition", host="h", user="*", memory="2G")
    scheduler2._tick()
    assert scheduler2._jobs["carol/e-0"].status() == JobStatus.RUNNING


def test_new_jobs_keep_runs_that_already_ended(tmp_path):
    scheduler, root = lab_scheduler()
    # NOTE: e-0 ran under the old per-user scheduler, whose ids had no
    # "<user>/"; e-1's outdir holds someone else's run.
    root.finished[str(tmp_path / "e" / "0")] = (
        "e-0", ("exited", 0, None, 10.0, 20.0, (None, 3 * G))
    )
    root.finished[str(tmp_path / "e" / "1")] = (
        "other", ("exited", 0, None, 10.0, 20.0, None)
    )
    _run_as(scheduler, "alice", "load", compiled=compiled(tmp_path, 3))
    scheduler._tick()
    jobs = scheduler._jobs
    assert jobs["alice/e-0"].status() == JobStatus.EXITED
    assert jobs["alice/e-0"].end_time() == 20.0
    assert [jobs[f"alice/e-{i}"].status() for i in (1, 2)] == [
        JobStatus.RUNNING, JobStatus.RUNNING,
    ]
    assert root.users == ["alice", "alice"]
    # NOTE: Its run teaches the memory estimate like any other.
    assert scheduler._memory.runs("alice/e-0")[0]["peak"] == 3 * G


def test_a_worker_reports_runs_that_ended(tmp_path):
    worker = Worker()
    outdir = tmp_path / "job"
    worker.exposed_launch_job("j", ME, str(tmp_path), "true", str(outdir), (), ())
    deadline = time.time() + 10
    while resolve_status(outdir)[0] == "running" and time.time() < deadline:
        time.sleep(0.05)
    (run,) = worker.exposed_finished_runs(((ME, str(outdir)),))
    assert run[0] == "j" and run[1][0] == "exited"
    assert worker.exposed_finished_runs(((ME, str(tmp_path / "x")),)) == (None,)


def test_dashboard_tokens_act_as_their_user(tmp_path):
    import threading

    scheduler, _ = lab_scheduler()
    _run_as(scheduler, "bob", "load", compiled=compiled(tmp_path, 1))
    thread = threading.Thread(target=scheduler.run)
    thread.start()
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        dashboard = Dashboard(scheduler, port, "lab")
        assert dashboard.url("alice").endswith(dashboard.token("alice"))
        assert dashboard._user_of("alice.forged") is None
        client = dashboard._app.test_client()
        client.set_cookie(COOKIE, dashboard.token("alice"), domain="localhost")
        base = f"http://localhost:{port}"
        reply = client.post(
            "/api/action", json={"action": "reset", "job_ids": ["bob/e-0"]},
            base_url=base,
        )
        assert reply.status_code == 409 and "bob's" in reply.get_json()["error"]
        reply = client.get("/files?job=bob/e-0&label=stdout", base_url=base)
        assert reply.status_code == 403
        assert client.get("/api/state", base_url=base).status_code == 200
    finally:
        scheduler.stop()
        thread.join(5)


def test_only_owners_and_admins_resolve_or_read_a_jobs_files(tmp_path):
    scheduler, _ = lab_scheduler()
    _run_as(scheduler, "bob", "load", compiled=compiled(tmp_path, 1))
    scheduler._tick()
    with pytest.raises(CommandError, match="only the owner of bob/e-0"):
        _run_as(scheduler, "alice", "resolve", targets=["bob/e"])
    (job,) = _run_as(scheduler, "bob", "resolve", targets=["e"])
    assert (job["id"], job["experiment"], job["host"]) == ("bob/e-0", "bob/e", "h")
    assert _run_as(scheduler, "boss", "resolve", targets=["bob/e-0"]) == [job]
    with pytest.raises(CommandError, match="no experiment named 'bob/x', and "):
        _run_as(scheduler, "bob", "resolve", targets=["x"])

    # NOTE: What a console reads goes through the scheduler, which checks
    # who asks.
    service = ControlService(scheduler, None)
    service._caller = scheduler.caller("alice")
    with pytest.raises(ValueError, match="only the owner of bob/e-0"):
        service.exposed_job_index("bob/e-0")
    with pytest.raises(ValueError, match="only the owner of bob/e-0"):
        service.exposed_read_file("bob/e-0", "stdout", 0, 10)


def test_the_socket_admits_members_of_the_group_only():
    left, right = socket.socketpair(socket.AF_UNIX)
    try:
        # NOTE: The peer is this process, so this user.
        _, credentials = PeerUidAuthenticator()(left)
        assert credentials["user"] == ME
        _, credentials = PeerUidAuthenticator([os.getgid()])(left)
        assert credentials["user"] == ME
        others = set(range(1, 60000)) - set(os.getgroups()) - {os.getgid()}
        with pytest.raises(AuthenticationError):
            PeerUidAuthenticator([min(others)])(left)
    finally:
        left.close()
        right.close()


def test_the_helper_answers_one_line_per_request(tmp_path):
    (tmp_path / ".job").mkdir()
    requests = io.BytesIO(
        b'{"op": "statuses", "args": [["%s"]]}\n' % str(tmp_path).encode()
        + b'{"op": "read_file", "args": ["%s", "x", 0, 1]}\n'
        % str(tmp_path).encode()
    )
    replies = io.BytesIO()
    serve(requests, replies)
    first, second = [json.loads(line) for line in replies.getvalue().splitlines()]
    assert first["result"][0][0] == "missing"
    assert second["type"] == "PermissionError"


def test_a_helper_process_does_the_work(tmp_path, monkeypatch):
    # NOTE: Only root may start a helper as someone else; as ourselves,
    # skip the switch.
    helper = HelperIO(ME)
    monkeypatch.setattr(
        helper,
        "_start",
        lambda: __import__("subprocess").Popen(
            [sys.executable, "-m", "ungem5.schedule.userio"],
            stdin=-1, stdout=-1,
        ),
    )
    LocalIO().call("prepare", str(tmp_path), str(tmp_path), [["out", str(tmp_path / "out")]], [["hi", str(tmp_path / "out")]])
    assert helper.call("read_file", str(tmp_path), "out", 0, 10) == b"hi"
    assert helper.call("memory_trace", str(tmp_path)) == b""
    with pytest.raises(PermissionError):
        helper.call("read_file", str(tmp_path), "nope", 0, 10)
    # NOTE: A helper that died is started again.
    helper._process.kill()
    helper._process.wait()
    assert helper.call("statuses", [str(tmp_path)])[0][0] == "missing"
    helper.close()


@pytest.fixture
def as_root(tmp_path, monkeypatch):
    """A Worker that believes it is root: systemd-run is a stand-in that
    logs its options and starts the runner, and the per-user helpers do
    their work in this process."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemd-run.log"
    fake = bin_dir / "systemd-run"
    fake.write_text(
        "#!/bin/bash\n"
        f'while [ "$1" != "--" ]; do echo "$1" >> {log}; shift; done\n'
        'shift\nsetsid "$@" > /dev/null 2>&1 < /dev/null &\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(worker_module.os, "geteuid", lambda: 0)

    class InProcessHelper(LocalIO):
        def __init__(self, user):
            pass

    monkeypatch.setattr(worker_module, "HelperIO", InProcessHelper)
    return log


def test_a_root_worker_runs_jobs_as_their_owner_in_a_unit(tmp_path, as_root):
    with pytest.raises(ValueError, match="groups"):
        Worker()
    store = tmp_path / "store"
    with pytest.raises(ValueError, match="store"):
        Worker(allow_gids=[os.getgid()])
    worker = Worker(allow_gids=[os.getgid()], store=store)
    outdir = tmp_path / "job"
    worker.exposed_launch_job(
        "j", ME, str(tmp_path), "sleep 0.5", str(outdir), (), ()
    )
    options = as_root.read_text().splitlines()
    assert f"--uid={os.getuid()}" in options
    assert f"--slice=ungem5-{worker_module.unit_name(ME)}.slice" in options
    deadline = time.time() + 10
    while resolve_status(outdir)[0] == "running" and time.time() < deadline:
        time.sleep(0.1)
    assert resolve_status(outdir)[0] == "exited"

    # NOTE: Owners outside the group, or system accounts, are refused.
    with pytest.raises(PermissionError, match="not in group"):
        Worker(allow_gids=[-5], store=store).exposed_launch_job(
            "j", ME, str(tmp_path), "true", str(outdir), (), ()
        )
    with pytest.raises(PermissionError, match="may not run jobs"):
        Worker(
            allow_gids=[os.getgid()], min_uid=os.getuid() + 1, store=store
        )._account(ME)


def test_signals_go_only_to_the_owners_processes(tmp_path):
    worker = Worker()
    outdir = tmp_path / "job"
    (outdir / ".job").mkdir(parents=True)
    # NOTE: A launch record pointing at init, as a user could forge one.
    init = __import__("psutil").Process(1)
    (outdir / ".job" / "launch.json").write_text(
        json.dumps({"pid": 1, "pgid": 1, "create_time": init.create_time()})
    )
    with pytest.raises(PermissionError, match="doesn't lead"):
        worker.exposed_signal_job(ME, str(outdir), 15)
