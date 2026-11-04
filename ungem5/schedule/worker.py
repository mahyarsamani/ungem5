"""The service each worker machine runs; the scheduler drives it over rpyc.

Everything the worker needs to know about a job lives in the job's outdir
(see `runner.py`), so the worker can be restarted while jobs run. Exposed
methods take and return only immutable builtins (str, int, float, bytes,
tuples) so rpyc passes them by value rather than as remote references.

Every job has an owner, a user name. A worker run by a user launches only
that user's jobs, as its own children. A worker run by root (the lab setup)
launches each job as its owner, in a transient systemd unit of its own in
the slice experiment-<owner>.slice, so jobs outlive the worker and get their
own cgroup; owners must have a uid of at least `min_uid` and be in one of
the groups `allow_gids`. Either way, a job's files are only ever touched as its owner
(see `userio.py`).
"""

import hashlib
import json
import os
import pwd
import re
import signal as signal_module
import subprocess
import sys
import threading
import time

from pathlib import Path
from typing import Iterable

import psutil
import rpyc

from .userio import HelperIO, LocalIO, leader_alive

ALLOWED_SIGNALS = {
    int(signal_module.SIGTERM),
    int(signal_module.SIGINT),
    int(signal_module.SIGQUIT),
    int(signal_module.SIGKILL),
}
# NOTE: Lowest uid a root worker launches for; below are system accounts.
DEFAULT_MIN_UID = 1000
# NOTE: How long a launch waits for the runner to write launch.json.
LAUNCH_WAIT_SECS = 15

PROTOCOL_CONFIG = {
    "allow_public_attrs": False,
    "allow_all_attrs": False,
    "allow_pickle": False,
    "allow_setattr": False,
    "allow_delattr": False,
    "sync_request_timeout": 30,
}


def default_store() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "experiment" / "store"


def unit_name(user: str) -> str:
    """A user name as it appears in systemd unit names; "-" would nest
    slices, so anything but letters, digits and "_" becomes "_"."""
    return re.sub(r"[^A-Za-z0-9_]", "_", user)


def _status(result) -> tuple:
    """A status from userio (lists, if it crossed as JSON) as tuples."""
    *head, memory = result
    return (*head, tuple(memory) if memory is not None else None)


class Worker(rpyc.Service):
    def __init__(
        self,
        allow_gids: Iterable[int] = (),
        min_uid: int = DEFAULT_MIN_UID,
        store: Path | None = None,
    ) -> None:
        """
        :param store: where bundles and working directories go, under
            <store>/<hostname>/<user>; by default ~/.cache/experiment/store
            of the user running the worker.
        """
        super().__init__()
        self._root = os.geteuid() == 0
        self._allow_gids = frozenset(allow_gids)
        if self._root and not self._allow_gids:
            raise ValueError(
                "a worker run by root needs the groups whose members may run "
                "jobs"
            )
        if self._root and store is None:
            raise ValueError("a worker run by root needs a store directory")
        self._store = Path(store) if store else default_store()
        self._min_uid = min_uid
        self._user = pwd.getpwuid(os.getuid()).pw_name
        self._local = LocalIO()
        self._helpers: dict[str, HelperIO] = dict()
        self._children: dict[str, subprocess.Popen] = dict()
        self._lock = threading.Lock()

    def _reap(self) -> None:
        # NOTE: Runners are our children until we restart; poll them so they
        # don't linger as zombies.
        with self._lock:
            for key, child in list(self._children.items()):
                if child.poll() is not None:
                    del self._children[key]

    def _account(self, user: str) -> pwd.struct_passwd:
        """`user`'s account, if this worker may run jobs for them."""
        try:
            account = pwd.getpwnam(str(user))
        except KeyError:
            raise PermissionError(f"no user {user!r} on this host") from None
        if not self._root:
            if account.pw_uid != os.getuid():
                raise PermissionError(
                    f"this worker runs as {self._user} and runs only their "
                    f"jobs, not {user}'s"
                )
            return account
        if account.pw_uid == 0 or account.pw_uid < self._min_uid:
            raise PermissionError(f"{user} may not run jobs here")
        if self._allow_gids.isdisjoint(os.getgrouplist(user, account.pw_gid)):
            raise PermissionError(
                f"{user} is not in group "
                f"{', '.join(map(str, sorted(self._allow_gids)))}, so may "
                "not run jobs here"
            )
        return account

    def _io(self, user: str) -> "LocalIO | HelperIO":
        self._account(user)
        if not self._root:
            return self._local
        with self._lock:
            helper = self._helpers.get(user)
            if helper is None:
                helper = self._helpers[user] = HelperIO(user)
        return helper

    def _user_store(self, account: pwd.struct_passwd) -> str:
        """The user's store on this host, made if needed. As root, the
        host's directory is root's and the user's is theirs, so helpers
        write only their own user's."""
        host = self._store / os.uname().nodename
        store = host / account.pw_name
        if store.is_dir():
            return str(store)
        if not self._root:
            store.mkdir(parents=True, exist_ok=True)
            return str(store)
        host.mkdir(parents=True, exist_ok=True, mode=0o755)
        store.mkdir(mode=0o700, exist_ok=True)
        os.chown(store, account.pw_uid, account.pw_gid)
        return str(store)

    def exposed_has_bundle(self, user: str, bundle_id: str) -> bool:
        store = self._user_store(self._account(user))
        return bool(self._io(user).call("has_bundle", store, str(bundle_id)))

    def exposed_put_bundle(self, user: str, bundle_id: str, data: bytes) -> None:
        store = self._user_store(self._account(user))
        self._io(user).call("put_bundle", store, str(bundle_id), bytes(data))

    def exposed_job_index(self, user: str, outdir: str) -> str:
        """The job's outdir index as JSON, or "null" before it ends."""
        return json.dumps(self._io(user).call("job_index", str(outdir)))

    def exposed_ping(self) -> str:
        return os.uname().nodename

    def exposed_host_resources(self) -> tuple:
        """(cores, total memory, available memory) of this machine, in
        logical CPUs and bytes."""
        memory = psutil.virtual_memory()
        return os.cpu_count() or 1, memory.total, memory.available

    def exposed_memory_trace(self, user: str, outdir: str) -> bytes:
        """The job's memory samples, one JSON object per line."""
        return self._io(user).call("memory_trace", str(outdir))

    def exposed_launch_job(
        self,
        job_id: str,
        user: str,
        cwd: str,
        command: str,
        outdir: str,
        files: tuple,
        dumps: tuple,
        bundle: tuple | None = None,
    ) -> int:
        """Launch the job as `user`; returns its runner's pid. A job with
        a bundle, (id, root, data paths), runs in the bundle's working
        directory instead of `cwd`; the bundle must be here already
        (`put_bundle`)."""
        self._reap()
        account = self._account(user)
        io = self._io(user)
        outdir, job_id = str(outdir), str(job_id)
        if bundle is not None:
            bundle_id, root, data = bundle
            cwd = io.call(
                "workdir",
                self._user_store(account),
                str(bundle_id),
                str(root),
                [str(path) for path in tuple(data)],
            )
        io.call(
            "prepare",
            outdir,
            str(cwd),
            [[str(label), str(path)] for label, path in tuple(files)],
            [[str(content), str(path)] for content, path in tuple(dumps)],
        )
        runner = [
            sys.executable,
            "-m",
            "ungem5.schedule.runner",
            "--outdir",
            outdir,
            "--cwd",
            str(cwd),
            "--job-id",
            job_id,
            "--",
            str(command),
        ]
        child = None
        if self._root:
            unit = self._start_unit(account, outdir, runner)
            where = f"`journalctl -u {unit}`"
        else:
            # NOTE: Until the runner redirects its own output, a failure
            # to start shows in this worker's output.
            child = subprocess.Popen(
                runner,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            with self._lock:
                self._children[outdir] = child
            where = "this worker's output"
        deadline = time.time() + LAUNCH_WAIT_SECS
        while time.time() < deadline:
            record = io.call("launch_record", outdir)
            if record is not None and record.get("job_id") == job_id:
                return int(record["pid"])
            if child is not None and child.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeError(
            f"the runner of {job_id[:12]} did not start; see "
            f"{outdir}/.job/runner.log or {where}"
        )

    def _start_unit(
        self, account: pwd.struct_passwd, outdir: str, runner: list[str]
    ) -> str:
        user = unit_name(account.pw_name)
        digest = hashlib.sha1(f"{outdir} {time.time()}".encode()).hexdigest()
        unit = f"experiment-{user}-{digest[:12]}"
        result = subprocess.run(
            [
                "systemd-run",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                f"--slice=experiment-{user}.slice",
                f"--uid={account.pw_uid}",
                f"--gid={account.pw_gid}",
                "--",
                *runner,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"systemd-run failed: {result.stderr.strip() or result.returncode}"
            )
        return unit

    def _per_user(self, operation: str, jobs: tuple) -> list:
        """`operation` on the outdirs of ((user, outdir), ...), each as its
        user; results in the order of `jobs`."""
        jobs = [(str(user), str(outdir)) for user, outdir in tuple(jobs)]
        by_user: dict[str, list[int]] = {}
        for index, (user, _) in enumerate(jobs):
            by_user.setdefault(user, []).append(index)
        results: list = [None] * len(jobs)
        for user, indices in by_user.items():
            answers = self._io(user).call(
                operation, [jobs[index][1] for index in indices]
            )
            for index, answer in zip(indices, answers):
                results[index] = answer
        return results

    def exposed_job_statuses(self, jobs: tuple) -> tuple:
        """Statuses of ((user, outdir), ...); see `userio.resolve_status`."""
        self._reap()
        return tuple(_status(s) for s in self._per_user("statuses", jobs))

    def exposed_finished_runs(self, jobs: tuple) -> tuple:
        """For ((user, outdir), ...): (job id, status) of the run that
        ended in each outdir, or None; see `userio.finished_runs`."""
        return tuple(
            None if run is None else (str(run[0]), _status(run[1]))
            for run in self._per_user("finished_runs", jobs)
        )

    def _leader(self, user: str, outdir: str) -> tuple[int, int] | None:
        """(pid, pgid) of the job's runner, or None if it isn't running.

        The owner wrote launch.json, so this checks that it names a
        process group the owner leads before anyone acts on it, as root
        or not.
        """
        account = self._account(user)
        launch = self._io(user).call("launch_record", str(outdir))
        if launch is None or not leader_alive(launch):
            return None
        try:
            leader = psutil.Process(launch["pid"])
            owner = leader.uids().real
            pgid = os.getpgid(launch["pid"])
        except (psutil.NoSuchProcess, ProcessLookupError):
            return None
        if owner != account.pw_uid or pgid != launch["pgid"]:
            raise PermissionError(
                f"the launch record in {outdir} names a process group "
                f"{user} doesn't lead"
            )
        return launch["pid"], pgid

    def exposed_signal_job(self, user: str, outdir: str, signum: int) -> bool:
        """Returns False if the job wasn't running (nothing to signal)."""
        signum = int(signum)
        if signum not in ALLOWED_SIGNALS:
            raise ValueError(f"signal {signum} is not allowed")
        leader = self._leader(user, outdir)
        if leader is None:
            return False
        try:
            os.killpg(leader[1], signum)
        except ProcessLookupError:
            return False
        return True

    def exposed_freeze_job(self, user: str, outdir: str, frozen: bool) -> bool:
        """Freeze (or thaw) the whole job: as root through its unit's
        cgroup, else by stopping (or continuing) its process group. A
        frozen job keeps its memory and stops using CPU; SIGKILL still
        ends it. Returns False if the job wasn't running."""
        leader = self._leader(user, outdir)
        if leader is None:
            return False
        pid, pgid = leader
        if not self._root:
            stop = signal_module.SIGSTOP if frozen else signal_module.SIGCONT
            os.killpg(pgid, stop)
            return True
        with open(f"/proc/{pid}/cgroup") as f:
            path = f.read().strip().partition("::")[2]
        slice_dir = f"/experiment.slice/experiment-{unit_name(user)}.slice/"
        if not path.startswith(slice_dir):
            raise PermissionError(f"{outdir}'s job runs outside {slice_dir}")
        with open(f"/sys/fs/cgroup{path}/cgroup.freeze", "w") as f:
            f.write("1" if frozen else "0")
        return True

    def exposed_set_user_memory(self, user: str, limit: int | None) -> bool:
        """Cap the memory of all `user`'s jobs here (their slice), or lift
        the cap with None; the kernel kills a job that pushes past it.
        Only a worker run by root has slices; others return False."""
        self._account(user)
        if not self._root:
            return False
        result = subprocess.run(
            [
                "systemctl",
                "set-property",
                "--runtime",
                f"experiment-{unit_name(user)}.slice",
                f"MemoryMax={'infinity' if limit is None else int(limit)}",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0

    def exposed_read_file(
        self, user: str, outdir: str, label: str, offset: int, length: int
    ) -> bytes:
        return self._io(user).call(
            "read_file", str(outdir), str(label), int(offset), int(length)
        )
