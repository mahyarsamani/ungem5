"""The scheduler: places queued jobs on hosts and tracks them to completion.

Threading model: all scheduler state is owned by the thread running
`Scheduler.run`. Other threads (the console server, the dashboard) never
touch it directly; they `submit` commands, which run on the scheduler thread
between ticks, and read an immutable `Snapshot` published after every tick.

The scheduler never runs a script's code: the console compiles a script to
data (`script.compile_script`) and loads that.

Every command comes from a `Caller`. A scheduler run by a user (the default)
has one caller, that user, who may do anything. A lab scheduler (`lab=True`)
serves many users: each user's experiments, jobs and scripts are named
"<user>/<name>" (scripts "<user>:<path>"), everyone sees everything, users
change only their own, and only admins change hosts, partitions and the
scheduler itself. Jobs run as the user who loaded them.
"""

import base64
import hashlib
import logging
import os
import pwd
import tarfile
import tempfile
import threading
import time

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from queue import Empty, Queue
from typing import Iterable

from ..jobs.bundle import BundleError, unpack
from ..jobs.resources import format_size, parse_size
from ..jobs.work import Experiment, Job, JobStatus, LoadedJob, PerHostJob
from .host import (
    SIGNAL,
    Host,
    HostUnreachable,
    JobError,
    job_status_from_worker,
)
from .memory import Estimate, MemoryHistory
from .state import StateStore

# NOTE: What a job nobody knows anything about may use.
DEFAULT_MEMORY = 8 << 30
MEMORY_HISTORY_FILE = "memory.sqlite"
BLOB_DIR = "blobs"
# NOTE: How many of a job's files `files` lists.
FILES_SHOWN = 50
# NOTE: How far past their partition users may go, as a share of it, taken
# only from other users' idle partitions.
BORROW_MARGIN = 0.1
# NOTE: The partition of users who have none of their own on a host.
EVERYONE = "*"
# NOTE: How many new jobs' outdirs one host checks per tick for runs that
# already ended.
CHECK_BATCH = 500
# NOTE: A job that outgrows its reservation is granted this much more than
# it uses, if its host and its owner's partition have room.
GRACE_FACTOR = 1.25
# NOTE: Below this share of a host's memory free, its newest job is
# paused; at twice it, the jobs paused that way go on.
LOW_MEMORY_SHARE = 0.03


def current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


class CommandError(Exception):
    """A command was refused; the message is meant for the user."""


@dataclass(frozen=True)
class Caller:
    user: str
    admin: bool


class HostMode(Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    REMOVING = "removing"

    def __str__(self) -> str:
        return self.value


@dataclass
class Command:
    name: str
    kwargs: dict
    caller: Caller
    future: Future = field(default_factory=Future)


class EventLog:
    """Recent notable events, for consoles to show and catch up on."""

    LEVELS = {
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }

    def __init__(self, logger: logging.Logger, size: int = 1000) -> None:
        self._logger = logger
        self._events: deque = deque(maxlen=size)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, level: str, message: str) -> None:
        with self._lock:
            self._seq += 1
            self._events.append((self._seq, time.time(), level, message))
        self._logger.log(self.LEVELS[level], message)

    def info(self, message: str) -> None:
        self.add("info", message)

    def warn(self, message: str) -> None:
        self.add("warn", message)

    def error(self, message: str) -> None:
        self.add("error", message)

    def since(self, seq: int) -> list[tuple[int, float, str, str]]:
        with self._lock:
            return [event for event in self._events if event[0] > seq]

    def last_seq(self) -> int:
        with self._lock:
            return self._seq


@dataclass(frozen=True)
class Snapshot:
    taken_at: float
    info: dict
    hosts: tuple
    experiments: tuple
    jobs: tuple
    # NOTE: Not serialized; used by the dashboard to proxy file reads.
    job_locations: dict = field(default_factory=dict)
    host_objects: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "taken_at": self.taken_at,
            "info": self.info,
            "hosts": list(self.hosts),
            "experiments": list(self.experiments),
            "jobs": list(self.jobs),
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Scheduler:
    SIGNALS = {signal.name.lower(): signal.value for signal in SIGNAL}

    def __init__(
        self,
        name: str,
        polling_secs: float = 1.0,
        store: StateStore | None = None,
        logger: logging.Logger | None = None,
        default_memory: int | str = DEFAULT_MEMORY,
        lab: bool = False,
        admins: Iterable[str] = (),
    ) -> None:
        """
        :param lab: serve many users (see the module's docstring); else
            only the user running the scheduler.
        :param admins: in a lab, the users who may change hosts,
            partitions and anyone's jobs.
        """
        self._name = name
        self._polling_secs = polling_secs
        self._store = store
        self._logger = logger or logging.getLogger(f"{name}.scheduler")
        self._events = EventLog(self._logger)
        self._lab = lab
        self._admins = frozenset(admins)
        self._user = current_user()
        # NOTE: Who runs the command being handled; the scheduler itself
        # (e.g. resuming) acts as an admin.
        self._itself = Caller(self._user, True)
        self._caller = self._itself
        self._info: dict = {
            "name": name,
            "started_at": time.time(),
            "lab": lab,
            "admins": sorted(self._admins),
        }
        self._memory = MemoryHistory(
            store.directory() / MEMORY_HISTORY_FILE if store else None,
            parse_size(default_memory),
        )
        # NOTE: Jobs and memory reserved on a job's host when it launched,
        # for its memory record; lost on a restart.
        self._launch_context: dict[str, tuple[int, int]] = dict()
        # NOTE: Estimates of queued jobs, made once per tick.
        self._estimates: dict[str, Estimate] = dict()

        self._hosts: dict[str, Host] = dict()
        self._host_mode: dict[str, HostMode] = dict()
        self._experiments: dict[str, Experiment] = dict()
        self._jobs: dict[str, Job] = dict()
        self._killing: set[str] = set()
        # NOTE: Loaded scripts: their sha256 and what they compiled to.
        self._scripts: dict[str, str] = dict()
        self._compiled: dict[str, dict] = dict()
        # NOTE: The script that first defined each job, for `delete --script`.
        self._job_origin: dict[str, str] = dict()
        # NOTE: Deleted by the user; loading a script skips them.
        self._deleted_jobs: set[str] = set()
        self._deleted_experiments: set[str] = set()
        # NOTE: Per-host jobs by id, as {"experiment", "outdir"}; their
        # instances are ordinary jobs in self._jobs, with ids
        # PerHostJob.instance_id(template, host).
        self._templates: dict[str, dict] = dict()
        # NOTE: Each job's dependencies: (plain job ids, per-host job ids).
        self._deps: dict[str, tuple[list[str], list[str]]] = dict()
        # NOTE: Hosts where every per-host dependency of a job is done;
        # missing means the job has no per-host dependencies.
        self._ready_hosts: dict[str, set[str]] = dict()
        # NOTE: Per host, per user (or EVERYONE): {"cores", "memory"}, each
        # None for no limit. A host without partitions is shared freely.
        self._partitions: dict[str, dict[str, dict]] = dict()
        # NOTE: Jobs new to this scheduler whose outdirs nobody has checked
        # for a run that already ended; they don't launch until checked.
        self._unchecked: set[str] = set()
        # NOTE: The least memory a job's next runs reserve, set by `retry`.
        self._memory_floors: dict[str, int] = dict()
        # NOTE: Packed bundles by id: files next to the state, or in memory
        # without a store (tests).
        self._blob_dir = store.directory() / BLOB_DIR if store else None
        self._blobs: dict[str, bytes] = dict()
        # NOTE: Per host, the resources report the low-memory check last
        # acted on, so it acts once per report.
        self._low_memory_seen: dict[str, float] = dict()
        self._dirty = True

        self._connect_pool = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix=f"{name}.connect"
        )
        self._connecting: dict[str, Future] = dict()
        self._reported_errors: dict[str, str | None] = dict()

        self._commands: Queue[Command] = Queue()
        self._stop = threading.Event()
        self._stopped = threading.Event()
        self._snapshot_lock = threading.Lock()
        self._snapshot = Snapshot(time.time(), dict(self._info), (), (), ())

        self._handlers = {
            "load": self._cmd_load,
            "reload": self._cmd_reload,
            "kill": self._cmd_kill,
            "drain": self._cmd_drain,
            "undrain": self._cmd_undrain,
            "remove": self._cmd_remove,
            "capacity": self._cmd_capacity,
            "partition": self._cmd_partition,
            "memory": self._cmd_memory,
            "files": self._cmd_files,
            "resolve": self._cmd_resolve,
            "signal": self._cmd_signal,
            "resume": self._cmd_resume,
            "retry": self._cmd_retry,
            "reset": self._cmd_reset,
            "stop": self._cmd_stop,
            "delete": self._cmd_delete,
            "undelete": self._cmd_undelete,
        }

    # ---- Thread-safe interface -------------------------------------------

    def events(self) -> EventLog:
        return self._events

    def set_info(self, **info) -> None:
        # NOTE: Called before `run` starts, e.g. with the dashboard URL.
        self._info.update(info)

    def snapshot(self) -> Snapshot:
        with self._snapshot_lock:
            return self._snapshot

    def commands(self) -> list[str]:
        return list(self._handlers)

    def lab(self) -> bool:
        return self._lab

    def caller(self, user: str) -> Caller:
        """`user` as the commands they send see them."""
        return Caller(user, not self._lab or user in self._admins)

    def may_read(self, caller: Caller, job_id: str) -> bool:
        """Everyone sees every job; only its owner and admins read its
        files."""
        location = self.snapshot().job_locations.get(job_id)
        return caller.admin or (
            location is not None and location[2] == caller.user
        )

    def submit(self, name: str, caller: Caller | None = None, **kwargs) -> Future:
        """Run command `name` for `caller`, by default the scheduler
        itself."""
        if name not in self._handlers:
            raise CommandError(f"unknown command {name!r}")
        command = Command(name, kwargs, caller or self._itself)
        if self._stopped.is_set():
            command.future.set_exception(
                CommandError("the scheduler is stopping")
            )
        else:
            self._commands.put(command)
        return command.future

    def call(
        self,
        name: str,
        timeout: float = 60,
        caller: Caller | None = None,
        **kwargs,
    ) -> list[str]:
        return self.submit(name, caller, **kwargs).result(timeout=timeout)

    def stop(self) -> None:
        self._stop.set()

    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)

    def _located(self, job_id: str) -> tuple[Host, Path, str]:
        """(host, outdir, owner) of a job, from the latest snapshot, for
        the threads that read its files."""
        snapshot = self.snapshot()
        location = snapshot.job_locations.get(job_id)
        if location is None:
            raise CommandError(f"no job {job_id}")
        outdir, host_name, owner = location
        host = snapshot.host_objects.get(host_name)
        if host is None or not host.up():
            raise HostUnreachable(f"host {host_name} is not connected")
        return host, outdir, owner

    def read_job_file(
        self, job_id: str, label: str, offset: int, length: int
    ) -> bytes:
        host, outdir, owner = self._located(job_id)
        return host.read_file(owner, outdir, label, offset, length)

    def read_job_index(self, job_id: str) -> dict | None:
        """The index of the job's outdir, from its host, or None before
        the job ends."""
        host, outdir, owner = self._located(job_id)
        return host.job_index(owner, outdir)

    # ---- Scheduler thread ------------------------------------------------

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                deadline = time.time() + self._polling_secs
                try:
                    self._tick()
                except Exception:
                    self._logger.exception("Unhandled error in scheduler tick.")
                self._handle_commands(deadline)
        finally:
            self._shutdown()

    def _handle_commands(self, deadline: float) -> None:
        """Run commands as they arrive until `deadline`.

        Returns early after a command so its effects are scheduled promptly.
        Pending commands always run, even if the tick used up the interval.
        """
        while not self._stop.is_set():
            timeout = deadline - time.time()
            try:
                if timeout <= 0:
                    command = self._commands.get_nowait()
                else:
                    command = self._commands.get(timeout=min(timeout, 0.2))
            except Empty:
                if timeout <= 0:
                    return
                continue
            self._run_command(command)
            while True:
                try:
                    self._run_command(self._commands.get_nowait())
                except Empty:
                    break
            # NOTE: So a view right after a command reflects it.
            self._publish()
            return

    def _run_command(self, command: Command) -> None:
        if not command.future.set_running_or_notify_cancel():
            return
        self._caller = command.caller
        try:
            result = self._handlers[command.name](**command.kwargs)
        except CommandError as e:
            command.future.set_exception(e)
        except Exception as e:
            self._logger.exception(f"Command {command.name} failed.")
            command.future.set_exception(
                CommandError(f"{type(e).__name__}: {e}")
            )
        else:
            self._dirty = True
            command.future.set_result(result)
        finally:
            self._caller = self._itself

    def _shutdown(self) -> None:
        self._stopped.set()
        while True:
            try:
                command = self._commands.get_nowait()
            except Empty:
                break
            command.future.set_exception(
                CommandError("the scheduler is stopping")
            )
        try:
            self._persist(force=True)
        except Exception:
            self._logger.exception("Could not save state on shutdown.")
        for host in self._hosts.values():
            host.disconnect()
        self._connect_pool.shutdown(wait=False, cancel_futures=True)
        self._memory.close()
        self._events.info("Scheduler stopped.")

    def _tick(self) -> None:
        self._estimates.clear()
        self._reconnect_hosts()
        for host in list(self._hosts.values()):
            if host.up():
                try:
                    host.refresh_resources()
                except HostUnreachable as e:
                    self._host_lost(host, e)
                    continue
                self._poll_host(host)
        self._process_killing()
        self._process_host_removal()
        self._check_memory()
        self._adopt_finished_runs()
        self._update_readiness()
        self._schedule()
        self._publish()
        self._persist()

    def _active_jobs(self, host_name: str) -> list[Job]:
        return [
            job
            for job in self._jobs.values()
            if job.host() == host_name and job.status().active()
        ]

    def _cores(self, job: Job, host_name: str | None) -> int:
        """Cores `job` takes on `host_name`. A job pinned to a host (e.g.
        a per-host build) takes at most the whole host."""
        host = self._hosts.get(host_name or "")
        if job.pinned_host() is not None and host is not None:
            return min(job.cores(), host.cores())
        return job.cores()

    @staticmethod
    def _held(job: Job) -> int:
        """Memory a running job holds: its reservation, or more if it
        already uses more."""
        return max(job.reserved() or 0, job.memory_now() or 0)

    def _usage(self) -> dict[str, dict]:
        """Per host: cores and memory held by its active jobs, how many
        jobs, how much reserved memory they haven't touched yet, and the
        cores and memory each user holds."""
        usage = {
            name: {
                "cores": 0, "memory": 0, "jobs": 0, "untouched": 0,
                "users": {},
            }
            for name in self._hosts
        }
        for job in self._jobs.values():
            if job.status().active() and job.host() in usage:
                self._hold(
                    usage[job.host()],
                    job,
                    self._cores(job, job.host()),
                    self._held(job),
                    max(0, (job.reserved() or 0) - (job.memory_now() or 0)),
                )
        return usage

    def _hold(
        self, used: dict, job: Job, cores: int, memory: int, untouched: int
    ) -> None:
        used["cores"] += cores
        used["memory"] += memory
        used["jobs"] += 1
        used["untouched"] += untouched
        mine = used["users"].setdefault(
            job.owner() or self._user, {"cores": 0, "memory": 0}
        )
        mine["cores"] += cores
        mine["memory"] += memory

    def _share(self, host_name: str, user: str) -> dict | None:
        """`user`'s partition of a host: theirs, or EVERYONE's."""
        shares = self._partitions.get(host_name, {})
        return shares.get(user) or shares.get(EVERYONE)

    def _within_partition(
        self, job: Job, host: Host, used: dict, cores: int, memory: int
    ) -> bool:
        """Whether `cores` and `memory` more for `job` fit in its owner's
        partition of `host`: up to the partition, or up to BORROW_MARGIN
        past it while other users leave enough of theirs idle."""
        shares = self._partitions.get(host.name())
        if not shares:
            return True
        user = job.owner() or self._user
        share = self._share(host.name(), user)
        if share is None:
            return False
        mine = used["users"].get(user, {"cores": 0, "memory": 0})
        need = {"cores": cores, "memory": memory}
        for key, amount in need.items():
            limit = share[key]
            if limit is None or mine[key] + amount <= limit:
                continue
            idle = sum(
                max(0, (other[key] or 0) - used["users"].get(name, {}).get(key, 0))
                for name, other in shares.items()
                if name not in (user, EVERYONE)
            )
            total = mine[key] + amount
            if total > limit * (1 + BORROW_MARGIN) or total - limit > idle:
                return False
        return True

    def _estimate(self, job: Job) -> Estimate:
        estimate = self._estimates.get(job.id())
        if estimate is None:
            estimate = self._memory.estimate(job)
            floor = self._memory_floors.get(job.id(), 0)
            if floor > estimate.memory:
                estimate = Estimate(
                    floor, "retry", f"at least {format_size(floor)} (`retry`)"
                )
            self._estimates[job.id()] = estimate
        return estimate

    def _fits(self, job: Job, host: Host, used: dict) -> bool:
        """Whether `job` fits next to what `used` holds on `host`. Memory
        must fit in the host's limit and in what the machine has free,
        less what running jobs reserved but haven't used yet."""
        if used["cores"] + self._cores(job, host.name()) > host.cores():
            return False
        memory = self._estimate(job).memory
        limit = host.memory()
        if limit is not None and used["memory"] + memory > limit:
            return False
        available = host.available_memory()
        if available is not None and memory > available - used["untouched"]:
            return False
        return self._within_partition(
            job, host, used, self._cores(job, host.name()), memory
        )

    def _too_big(self, job: Job) -> bool:
        """No host could ever take `job`, even empty."""
        hosts = [h for h in self._hosts.values() if self._placeable(job, h)]
        if not hosts:
            return False
        memory = self._estimate(job).memory
        return all(
            job.cores() > h.cores() and job.pinned_host() is None
            or (h.memory() is not None and memory > h.memory())
            for h in hosts
        )

    def _update_readiness(self) -> None:
        """Mark each pending job with dependencies queued, waiting or
        blocked, and note the hosts where its per-host ones are done.

        A job may run on host H when it is compatible with H, all its
        plain dependencies exited, and all its per-host dependencies
        exited on H.
        """
        self._ready_hosts.clear()
        for job_id, (plain, per_host) in self._deps.items():
            job = self._jobs.get(job_id)
            if job is None or not job.status().pending():
                continue
            if not plain and not per_host:
                continue
            waiting, blocked = [], []
            for parent_id in plain:
                parent = self._jobs.get(parent_id)
                if parent is None:
                    blocked.append(f"{parent_id[:12]} was deleted")
                elif parent.status() in (JobStatus.FAILED, JobStatus.KILLED):
                    blocked.append(f"{parent_id[:12]} {parent.status()}")
                elif parent.status() == JobStatus.BLOCKED:
                    blocked.append(f"{parent_id[:12]} is blocked")
                elif parent.status() != JobStatus.EXITED:
                    waiting.append(parent_id[:12])
            if per_host:
                ready, possible = set(), False
                for host in self._hosts.values():
                    if not self._compatible(job, host):
                        continue
                    done = can = True
                    for template_id in per_host:
                        instance = self._jobs.get(
                            PerHostJob.instance_id(template_id, host.name())
                        )
                        if instance is None or instance.status() in (
                            JobStatus.FAILED,
                            JobStatus.KILLED,
                        ):
                            done = can = False
                            break
                        if instance.status() != JobStatus.EXITED:
                            done = False
                    if done:
                        ready.add(host.name())
                    elif can:
                        possible = True
                self._ready_hosts[job_id] = ready
                if not ready:
                    names = ", ".join(per_host)
                    if possible:
                        waiting.append(f"{names} on a compatible host")
                    else:
                        blocked.append(
                            f"no compatible host where {names} succeeded"
                        )
            if blocked:
                status, reason = JobStatus.BLOCKED, "; ".join(blocked)
            elif waiting:
                status = JobStatus.WAITING
                reason = "waiting for " + ", ".join(waiting)
            else:
                status, reason = JobStatus.QUEUED, None
            if (job.status(), job.message()) != (status, reason):
                job.set_pending(status, reason)
                self._dirty = True

    def _placeable(self, job: Job, host: Host) -> bool:
        if job.pinned_host() not in (None, host.name()):
            return False
        ready = self._ready_hosts.get(job.id())
        if ready is not None and host.name() not in ready:
            return False
        return self._compatible(job, host)

    def _reconnect_hosts(self) -> None:
        for name, future in list(self._connecting.items()):
            if not future.done():
                continue
            del self._connecting[name]
            host = self._hosts.get(name)
            if host is None:
                continue
            if host.up():
                self._events.info(f"Host {name} connected.")
                self._reported_errors.pop(name, None)
                self._dirty = True
            elif (
                self._reported_errors.get(name) != host.last_error()
                or host.failures() % 10 == 0
            ):
                # NOTE: Report each new kind of error, and repeats only now
                # and then.
                self._reported_errors[name] = host.last_error()
                self._events.warn(
                    f"Can't reach host {name} ({host.last_error()}); "
                    "retrying in the background."
                )
        now = time.time()
        for name, host in self._hosts.items():
            if name not in self._connecting and host.due_for_reconnect(now):
                self._connecting[name] = self._connect_pool.submit(
                    host.connect
                )

    def _host_lost(self, host: Host, error: Exception) -> None:
        affected = 0
        for job in self._active_jobs(host.name()):
            if job.status() != JobStatus.UNKNOWN:
                job.set_status(JobStatus.UNKNOWN)
                affected += 1
        self._dirty = True
        self._events.warn(
            f"Lost host {host.name()} ({error}); {affected} job(s) are now "
            "unknown and will be checked when it reconnects."
        )

    def _poll_host(self, host: Host) -> None:
        jobs = self._active_jobs(host.name())
        try:
            results = host.statuses(jobs)
        except HostUnreachable as e:
            self._host_lost(host, e)
            return
        except JobError as e:
            self._events.error(f"Polling {host.name()} failed: {e}")
            return
        for job, (status, returncode, message, start, end, memory) in zip(
            jobs, results
        ):
            before = job.status()
            if (start, end) != (job.start_time(), job.end_time()) and (
                start is not None or end is not None
            ):
                job.set_times(start, end)
                self._dirty = True
            if memory is not None and tuple(memory) != (
                job.memory_now(),
                job.memory_peak(),
            ):
                job.set_memory(*memory)
                self._dirty = True
            if status == "missing":
                if before == JobStatus.UNKNOWN:
                    # NOTE: The host has no record of this job, so it never
                    # started (e.g. the scheduler died mid-launch).
                    job.forget()
                    self._events.info(
                        f"Job {job.id()[:8]} never started on {host.name()}; "
                        "queued it again."
                    )
                else:
                    job.set_status(
                        JobStatus.FAILED,
                        message="launch record disappeared "
                        "(was the outdir deleted?)",
                    )
                self._dirty = True
                continue
            if status == "retry":
                self._record_memory(job, host, "retry")
                self._retry(job, host, message or "watcher asked to retry")
                continue
            new = job_status_from_worker(status)
            if new is None or new == before:
                continue
            if before == JobStatus.PAUSED and new == JobStatus.RUNNING:
                # NOTE: Frozen processes are alive; we know it's paused.
                continue
            job.set_status(new, returncode, message)
            if new.terminal() and job.end_time() is None:
                # NOTE: No exit record (e.g. SIGKILL): when we noticed.
                job.set_times(None, time.time())
            if new.terminal():
                self._record_memory(job, host, new.value)
            self._dirty = True
            if new == JobStatus.RUNNING:
                self._events.info(
                    f"Job {job.id()[:8]} is running on {host.name()}."
                )
            elif new == JobStatus.EXITED:
                self._events.info(
                    f"Job {job.id()[:8]} ({job.shorthand_command()}) finished."
                )
            elif new == JobStatus.FAILED:
                self._events.error(
                    f"Job {job.id()[:8]} ({job.shorthand_command()}) failed "
                    f"on {host.name()}: returncode={returncode}"
                    + (f", {message}" if message else "")
                )
            elif new == JobStatus.KILLED:
                self._events.warn(
                    f"Job {job.id()[:8]} ({job.shorthand_command()}) was killed"
                    + (f": {message}" if message else ".")
                )

    def _check_memory(self) -> None:
        """Deal with running jobs that outgrew their reservation, and with
        hosts running out of memory."""
        usage = self._usage()
        for host in self._hosts.values():
            if not host.up():
                continue
            used = usage[host.name()]
            for job in self._active_jobs(host.name()):
                if job.status() != JobStatus.RUNNING:
                    continue
                now, reserved = job.memory_now(), job.reserved()
                if now is None or reserved is None or now <= reserved:
                    continue
                self._outgrown(job, host, used)
            self._check_host_memory(host)

    def _outgrown(self, job: Job, host: Host, used: dict) -> None:
        """`job` uses more than it reserved: grant it more if there's room,
        else do what its `on_memory_exceed` says."""
        now, reserved = job.memory_now(), job.reserved()
        grant = int(now * GRACE_FACTOR)
        # NOTE: The job already holds `now`; it needs the rest.
        extra = grant - now
        limit = host.memory()
        available = host.available_memory()
        if (
            (limit is None or used["memory"] + extra <= limit)
            and (available is None or extra <= available - used["untouched"])
            and self._within_partition(job, host, used, 0, extra)
        ):
            job.set_reserved(grant)
            self._hold(used, job, 0, extra, extra)
            self._dirty = True
            self._events.info(
                f"Job {job.id()[:12]} uses {format_size(now)}, past the "
                f"{format_size(reserved)} reserved for it; reserved "
                f"{format_size(grant)} now."
            )
            return
        message = (
            f"uses {format_size(now)} of the {format_size(reserved)} "
            f"reserved, and {host.name()} has no room for more"
        )
        action = job.on_memory_exceed
        if action == "pause":
            self._pause(job, host, "memory", message)
            return
        try:
            host.signal(job, SIGNAL.KILL.value)
        except (HostUnreachable, JobError) as e:
            self._events.error(f"Killing job {job.id()[:12]}: {e}")
            return
        self._dirty = True
        if action == "kill":
            job.set_status(JobStatus.FAILED, message=f"memory: {message}")
            job.set_times(None, time.time())
            self._record_memory(job, host, JobStatus.FAILED.value)
            self._events.error(f"Job {job.id()[:12]} {message}; killed it.")
            return
        self._record_memory(job, host, JobStatus.KILLED.value)
        self._memory_floors[job.id()] = grant
        job.set_status(JobStatus.KILLED)
        job.reset()
        job.set_pending(JobStatus.QUEUED, f"retry with more memory: {message}")
        self._events.warn(
            f"Job {job.id()[:12]} {message}; killed it, and queued it again "
            f"with {format_size(grant)}."
        )

    def _check_host_memory(self, host: Host) -> None:
        """Once per resources report: if `host` is low on free memory
        (e.g. from processes we don't run), pause its newest job; once it
        has plenty again, let the oldest job paused that way go on."""
        reported = host.reported()
        if reported is None or self._low_memory_seen.get(
            host.name()
        ) == host.reported_at():
            return
        self._low_memory_seen[host.name()] = host.reported_at()
        _, total, available = reported
        low = total * LOW_MEMORY_SHARE
        jobs = self._active_jobs(host.name())
        if available < low:
            running = [j for j in jobs if j.status() == JobStatus.RUNNING]
            if running:
                newest = max(running, key=lambda j: j.start_time() or 0)
                self._pause(
                    newest,
                    host,
                    "host",
                    f"{host.name()} had only {format_size(available)} free; "
                    "goes on once it has more",
                )
        elif available > 2 * low:
            paused = [j for j in jobs if j.paused_for() == "host"]
            if paused:
                oldest = min(paused, key=lambda j: j.start_time() or 0)
                if self._thaw(oldest, host):
                    self._events.info(
                        f"Job {oldest.id()[:12]} goes on: {host.name()} has "
                        f"{format_size(available)} free again."
                    )

    def _pause(self, job: Job, host: Host, reason: str, message: str) -> None:
        try:
            frozen = host.freeze(job, True)
        except (HostUnreachable, JobError) as e:
            self._events.error(f"Pausing job {job.id()[:12]}: {e}")
            return
        if not frozen:
            return
        job.pause(reason, message)
        self._dirty = True
        short = job.id()[:12]
        self._events.warn(
            f"Paused job {short}: it {message}. `resume {short}` lets it go "
            f"on, `retry {short} --memory SIZE` runs it again with more, "
            f"`signal kill {short}` ends it."
            if reason == "memory"
            else f"Paused job {short}: {message}."
        )

    def _thaw(self, job: Job, host: Host) -> bool:
        try:
            host.freeze(job, False)
        except (HostUnreachable, JobError) as e:
            self._events.error(f"Resuming job {job.id()[:12]}: {e}")
            return False
        job.unpause()
        self._dirty = True
        return True

    def _adopt_finished_runs(self) -> None:
        """New jobs whose outdir holds a run of theirs that ended (e.g.
        under another scheduler) take its outcome instead of running again.

        Each job is checked once, through any up host it is compatible
        with, at most CHECK_BATCH per host and tick. A run of the old
        per-user scheduler counts too: its job id lacks the "<user>/".
        """
        for job_id in list(self._unchecked):
            job = self._jobs.get(job_id)
            if job is None or not job.status().pending():
                self._unchecked.discard(job_id)
        adopted = 0
        for host in self._hosts.values():
            if not self._unchecked:
                break
            if not host.up():
                continue
            jobs = [
                self._jobs[job_id]
                for job_id in sorted(self._unchecked)
                if self._compatible(self._jobs[job_id], host)
            ][:CHECK_BATCH]
            if not jobs:
                continue
            try:
                runs = host.finished_runs(jobs)
            except HostUnreachable as e:
                self._host_lost(host, e)
                continue
            except JobError as e:
                self._events.error(f"Checking outdirs on {host.name()}: {e}")
                continue
            for job, run in zip(jobs, runs):
                self._unchecked.discard(job.id())
                if run is None or run[0] not in (
                    job.id(),
                    job.id().split("/", 1)[-1],
                ):
                    continue
                status, returncode, message, start, end, memory = run[1]
                new = job_status_from_worker(status)
                if new is None or not new.terminal():
                    continue
                job.assign(host.name())
                job.set_status(new, returncode, message)
                job.set_times(start, end)
                if memory is not None:
                    job.set_memory(*memory)
                self._record_memory(job, host, new.value)
                adopted += 1
        if adopted:
            self._dirty = True
            self._events.info(
                f"{adopted} new job(s) had already run (their outdirs hold "
                "the record); they keep that outcome, and `reset` runs them "
                "again."
            )

    def _record_memory(self, job: Job, host: Host, outcome: str) -> None:
        """Add a run that ended to the memory history."""
        if job.memory_peak() is None:
            return
        try:
            trace = host.memory_trace(job)
        except (HostUnreachable, JobError) as e:
            self._logger.info(f"No memory trace for {job.id()[:8]}: {e}")
            trace = []
        co_jobs, co_reserved = self._launch_context.pop(
            job.id(), (None, None)
        )
        try:
            self._memory.record(
                job, host.name(), outcome, trace, co_jobs, co_reserved
            )
        except Exception:
            self._logger.exception(f"Recording memory of {job.id()[:8]}.")
            return
        reserved = job.reserved()
        if reserved and job.memory_peak() > reserved:
            self._events.warn(
                f"Job {job.id()[:8]} used {format_size(job.memory_peak())}, "
                f"more than the {format_size(reserved)} reserved for it."
            )

    def _retry(self, job: Job, host: Host, message: str) -> None:
        self._dirty = True
        if job.retry(message):
            self._events.warn(
                f"Job {job.id()[:8]} ({job.shorthand_command()}) on "
                f"{host.name()}: {message}; queued it again "
                f"({job.retries()}/{job.max_retries})."
            )
            return
        job.set_status(
            JobStatus.FAILED,
            message=f"{message} (gave up after {job.max_retries} retries)",
        )
        if job.end_time() is None:
            job.set_times(None, time.time())
        self._events.error(
            f"Job {job.id()[:8]} ({job.shorthand_command()}) on "
            f"{host.name()}: {message}; gave up after {job.max_retries} "
            "retries."
        )

    def _process_killing(self) -> None:
        for name in list(self._killing):
            experiment = self._experiments[name]
            active = [job for job in experiment.jobs() if job.status().active()]
            if not active:
                self._remove_experiment(name)
                self._events.info(f"Experiment {name} killed and removed.")
                continue
            for job in active:
                host = self._hosts.get(job.host())
                if host is None or not host.up():
                    continue
                try:
                    host.signal(job, SIGNAL.KILL.value)
                except HostUnreachable as e:
                    self._host_lost(host, e)
                except JobError as e:
                    self._events.error(
                        f"Killing job {job.id()[:8]} on {host.name()}: {e}"
                    )

    def _remove_experiment(self, name: str) -> None:
        experiment = self._experiments.pop(name)
        self._killing.discard(name)
        for job in experiment.jobs():
            self._forget_job(job)
        for template_id, template in list(self._templates.items()):
            if template["experiment"] == name:
                self._forget_template(template_id)
        self._dirty = True

    def _forget_template(self, template_id: str) -> None:
        self._templates.pop(template_id, None)
        self._job_origin.pop(template_id, None)
        self._dirty = True

    def _pending_dependents(self, job_ids: set[str]) -> list[Job]:
        """Jobs outside `job_ids` that haven't run yet and need one of
        them (directly, or as the instance of a per-host job)."""
        dependents = []
        for job_id, (plain, per_host) in self._deps.items():
            job = self._jobs.get(job_id)
            if job is None or job_id in job_ids or not job.status().pending():
                continue
            if any(parent in job_ids for parent in plain) or any(
                PerHostJob.instance_id(template, host) in job_ids
                for template in per_host
                for host in self._hosts
            ):
                dependents.append(job)
        return dependents

    def _refuse_if_needed(self, job_ids: set[str], what: str) -> None:
        dependents = self._pending_dependents(job_ids)
        if dependents:
            ids = ", ".join(job.id()[:12] for job in dependents[:10])
            more = (
                f" and {len(dependents) - 10} more"
                if len(dependents) > 10
                else ""
            )
            raise CommandError(
                f"can't delete {what}: {len(dependents)} job(s) that haven't "
                f"run yet depend on it ({ids}{more}); delete them first"
            )

    def _forget_job(self, job: Job) -> None:
        self._memory.forget(job.id())
        self._unchecked.discard(job.id())
        self._memory_floors.pop(job.id(), None)
        self._launch_context.pop(job.id(), None)
        self._jobs.pop(job.id(), None)
        self._job_origin.pop(job.id(), None)
        self._deps.pop(job.id(), None)
        self._ready_hosts.pop(job.id(), None)
        experiment = self._experiments.get(job.experiment())
        if experiment is not None:
            experiment.remove_job(job.id())
        self._dirty = True

    def _process_host_removal(self) -> None:
        for name, mode in list(self._host_mode.items()):
            if mode == HostMode.REMOVING and not self._active_jobs(name):
                self._drop_host(name)
                self._events.info(f"Host {name} removed.")

    def _drop_host(self, name: str) -> None:
        host = self._hosts.pop(name)
        self._host_mode.pop(name)
        self._connecting.pop(name, None)
        host.disconnect()
        self._dirty = True

    def _schedule(self) -> None:
        queued = [
            job
            for name, experiment in self._experiments.items()
            if name not in self._killing
            for job in experiment.queued()
            if job.id() not in self._unchecked
        ]
        usage = self._usage()
        progress = True
        while progress and queued:
            progress = False
            hosts = sorted(
                (
                    host
                    for name, host in self._hosts.items()
                    if host.up() and self._host_mode[name] == HostMode.ACTIVE
                ),
                key=lambda host: host.cores() - usage[host.name()]["cores"],
                reverse=True,
            )
            for host in hosts:
                used = usage[host.name()]
                # NOTE: Largest first (cores, then memory), so big jobs
                # aren't starved by small ones filling every host.
                job = max(
                    (
                        job
                        for job in queued
                        if self._placeable(job, host)
                        and self._fits(job, host, used)
                    ),
                    key=lambda job, host=host: (
                        self._cores(job, host.name()),
                        self._estimate(job).memory,
                    ),
                    default=None,
                )
                if job is None:
                    continue
                progress = True
                queued.remove(job)
                memory = self._estimate(job).memory
                self._launch_context[job.id()] = (
                    used["jobs"],
                    used["memory"],
                )
                self._launch(host, job, memory)
                if job.status().active():
                    self._hold(
                        used, job, self._cores(job, host.name()), memory, memory
                    )
                if not host.up():
                    break

    def _job_view(self, job: Job) -> dict:
        view = job.view()
        if job.status().pending():
            estimate = self._estimate(job)
            view["estimate"] = estimate.memory
            view["estimate_source"] = estimate.source
            view["estimate_detail"] = estimate.detail
        if job.status() == JobStatus.QUEUED and not any(
            self._placeable(job, host) for host in self._hosts.values()
        ):
            view["message"] = (
                "no compatible host (reload its script after adding hosts)"
            )
        elif job.status() == JobStatus.QUEUED and self._too_big(job):
            view["message"] = (
                f"needs {job.cores()} core(s) and "
                f"{format_size(self._estimate(job).memory)}, more than any "
                "compatible host offers"
            )
        plain, per_host = self._deps.get(job.id(), ([], []))
        view["depends_on"] = list(plain)
        view["depends_on_per_host"] = list(per_host)
        ready = self._ready_hosts.get(job.id())
        view["ready_hosts"] = sorted(ready) if ready is not None else None
        return view

    @staticmethod
    def _compatible(job: Job, host: Host) -> bool:
        # NOTE: For a LoadedJob, what the console decided when it compiled
        # the job's script; hosts added since then are incompatible.
        return job.compatible_with_host(host)

    def _launch(self, host: Host, job: Job, memory: int) -> None:
        job.assign(host.name(), memory)
        self._dirty = True
        # NOTE: Write-ahead: if we die during the launch, the next start
        # knows to ask this host about the job instead of launching it again.
        self._persist(force=True)
        try:
            self._ship(host, job)
            host.launch(job)
        except JobError as e:
            job.set_status(JobStatus.FAILED, message=f"launch failed: {e}")
            self._events.error(
                f"Launching job {job.id()[:8]} on {host.name()} failed: {e}"
            )
        except HostUnreachable as e:
            job.set_status(JobStatus.UNKNOWN)
            self._host_lost(host, e)
        else:
            job.set_status(JobStatus.RUNNING)
            self._events.info(
                f"Launched job {job.id()[:8]} ({job.shorthand_command()}) "
                f"on {host.name()} with {format_size(memory)} reserved "
                f"({self._estimate(job).source})."
            )
            if host.name() in self._partitions:
                # NOTE: After the launch, which made the owner's slice.
                self._cap_user(host, job.owner() or self._user)

    def _ceiling(self, host_name: str, user: str) -> int | None:
        """All the memory `user`'s jobs may hold on a host: their
        partition plus BORROW_MARGIN, or None for no limit."""
        share = self._share(host_name, user)
        if share is None or share["memory"] is None:
            return None
        return int(share["memory"] * (1 + BORROW_MARGIN))

    def _cap_user(self, host: Host, user: str) -> None:
        """Have `host`'s kernel hold `user`'s jobs to their ceiling, a
        backstop for a spike faster than the memory checks."""
        try:
            host.set_user_memory(user, self._ceiling(host.name(), user))
        except (HostUnreachable, JobError) as e:
            self._logger.info(f"Capping {user} on {host.name()}: {e}")

    def _publish(self) -> None:
        usage = self._usage()
        hosts = tuple(
            {
                "name": name,
                "isa": host.isa(),
                "domain": host.domain(),
                "state": str(host.state()),
                "mode": str(self._host_mode[name]),
                "cores": host.cores(),
                "cores_used": usage[name]["cores"],
                "memory": host.memory(),
                "memory_used": usage[name]["memory"],
                "memory_available": host.available_memory(),
                "limits": host.limits(),
                "partitions": self._partitions.get(name, {}),
                "users": usage[name]["users"],
                "jobs": usage[name]["jobs"],
                "error": host.last_error(),
            }
            for name, host in self._hosts.items()
        )
        experiments = []
        for name, experiment in self._experiments.items():
            counts: dict[str, int] = {}
            for job in experiment.jobs():
                counts[job.status().value] = (
                    counts.get(job.status().value, 0) + 1
                )
            experiments.append(
                {
                    "name": name,
                    "state": "killing" if name in self._killing else "active",
                    "outdir": str(experiment.outdir()),
                    "jobs": len(experiment.jobs()),
                    "counts": counts,
                }
            )
        snapshot = Snapshot(
            taken_at=time.time(),
            info=dict(
                self._info,
                scripts=sorted(self._scripts),
                script_jobs={
                    path: sum(
                        1 for origin in self._job_origin.values()
                        if origin == path
                    )
                    for path in self._scripts
                },
                deleted_jobs=sorted(self._deleted_jobs),
                deleted_experiments=sorted(self._deleted_experiments),
            ),
            hosts=hosts,
            experiments=tuple(experiments),
            jobs=tuple(self._job_view(job) for job in self._jobs.values()),
            job_locations={
                job_id: (job.outdir(), job.host(), job.owner())
                for job_id, job in self._jobs.items()
            },
            host_objects=dict(self._hosts),
        )
        with self._snapshot_lock:
            self._snapshot = snapshot

    # ---- Persistence -----------------------------------------------------

    def _state_data(self) -> dict:
        return {
            "name": self._name,
            "scripts": [
                {
                    "path": path,
                    "sha256": sha,
                    "compiled": self._compiled[path],
                }
                for path, sha in self._scripts.items()
            ],
            "hosts": {
                name: {
                    "spec": host.spec(),
                    "mode": self._host_mode[name].value,
                }
                for name, host in self._hosts.items()
            },
            "killing": sorted(self._killing),
            "partitions": self._partitions,
            "memory_floors": self._memory_floors,
            "deleted_jobs": sorted(self._deleted_jobs),
            "deleted_experiments": sorted(self._deleted_experiments),
            "jobs": {
                job_id: job.runtime_state()
                for job_id, job in self._jobs.items()
                if job.status() != JobStatus.QUEUED
            },
        }

    def _persist(self, force: bool = False) -> None:
        if self._store is None or not (self._dirty or force):
            return
        if self._store.save(self._state_data(), force=force):
            self._dirty = False

    def resume(self, data: dict) -> None:
        """Rebuild from a saved state. Call before `run`.

        Jobs keep their saved status: only queued jobs are ever launched, so
        jobs that were running are checked with their host, never restarted.
        Scripts come back as they were compiled; none is run again.
        """
        # NOTE: Before the scripts load, so they skip what was deleted.
        self._deleted_jobs = set(data.get("deleted_jobs", []))
        self._deleted_experiments = set(data.get("deleted_experiments", []))
        for script in data.get("scripts", []):
            path = Path(script["path"])
            try:
                changed = path.exists() and _sha256(path) != script["sha256"]
            except OSError:
                changed = False
            if changed:
                self._events.warn(
                    f"{path} changed since it was loaded; `reload` it to "
                    "pick that up."
                )
            try:
                for line in self._load(script["compiled"], merge=True):
                    self._logger.info(line)
            except Exception as e:
                self._events.error(f"Could not restore {path}: {e}")

        for name, saved in data.get("hosts", {}).items():
            spec = saved["spec"]
            if name not in self._hosts:
                try:
                    host = Host.from_spec(spec)
                except (KeyError, ValueError) as e:
                    # NOTE: e.g. a host saved before hosts had an isa.
                    self._events.warn(
                        f"Not restoring host {name} from saved state ({e}); "
                        "load a script that defines it."
                    )
                    continue
                self._add_host(host)
            # NOTE: Limits changed with `capacity` outlive a restart.
            self._hosts[name].set_limits(
                "auto" if spec["cores"] is None else spec["cores"],
                "auto" if spec["memory"] is None else spec["memory"],
            )
            self._host_mode[name] = HostMode(saved["mode"])

        for name in data.get("killing", []):
            if name in self._experiments:
                self._killing.add(name)
        self._partitions = {
            host: shares
            for host, shares in data["partitions"].items()
            if host in self._hosts
        }
        self._memory_floors = {
            job_id: floor
            for job_id, floor in data["memory_floors"].items()
            if job_id in self._jobs
        }

        orphans = []
        for job_id, saved in data.get("jobs", {}).items():
            job = self._jobs.get(job_id)
            if job is None:
                if JobStatus(saved["status"]).active():
                    orphans.append((job_id, saved))
                continue
            job.restore_runtime_state(saved)
        for job_id, saved in orphans:
            self._events.warn(
                f"Job {job_id[:8]} ({saved['status']} on {saved['host']}, "
                f"outdir {saved['outdir']}) is no longer defined by any "
                "script; it is not tracked anymore."
            )
        self._dirty = True
        self._publish()
        self._events.info(
            f"Resumed {len(self._experiments)} experiment(s) and "
            f"{len(self._hosts)} host(s) from saved state."
        )

    # ---- Users and names (run on the scheduler thread) ---------------------

    def _named(self, name: str) -> str:
        """A name the caller gave: in a lab, a name without a "/" is in
        the caller's namespace; one with a "/" names its own."""
        if not self._lab or "/" in name:
            return name
        return f"{self._caller.user}/{name}"

    def _script_key(self, path: str) -> str:
        """A script the caller named: in a lab, a path is the caller's own
        script, and "<user>:<path>" anyone's."""
        if self._lab and ":" in path and not path.startswith("/"):
            return path
        path = str(Path(path).expanduser().resolve())
        return f"{self._caller.user}:{path}" if self._lab else path

    def _owner_of(self, name: str) -> str:
        """The user an experiment, job or script key belongs to."""
        if not self._lab:
            return self._user
        return name.split(":" if ":" in name.split("/")[0] else "/", 1)[0]

    def _mine(self, name: str) -> bool:
        return self._caller.admin or self._owner_of(name) == self._caller.user

    def _require_mine(self, name: str, what: str) -> None:
        if not self._mine(name):
            raise CommandError(
                f"{what} is {self._owner_of(name)}'s; only they or an admin "
                "may change it"
            )

    def _require_admin(self, what: str) -> None:
        if not self._caller.admin:
            raise CommandError(f"only an admin may {what}")

    def _namespaced(self, compiled: dict, user: str) -> dict:
        """`compiled`, loaded by `user`: they own it and, in a lab, its
        names go in their namespace, so they can't reach anyone else's."""
        if not self._lab:
            return dict(compiled, owner=user)

        def name(value: str) -> str:
            return f"{user}/{value}"

        def job(spec: dict) -> dict:
            return dict(
                spec,
                id=name(spec["id"]),
                experiment=name(spec["experiment"]),
                after=[name(i) for i in spec["after"]],
                after_per_host=[name(i) for i in spec["after_per_host"]],
            )

        return dict(
            compiled,
            owner=user,
            script=f"{user}:{compiled['script']}",
            experiments=[
                dict(e, name=name(e["name"]), jobs=[job(j) for j in e["jobs"]])
                for e in compiled["experiments"]
            ],
            templates=[
                dict(
                    t,
                    id=name(t["id"]),
                    experiment=name(t["experiment"]),
                    instances=[job(j) for j in t["instances"]],
                )
                for t in compiled["templates"]
            ],
        )

    # ---- Commands (run on the scheduler thread) ---------------------------

    def _add_host(self, host: Host) -> None:
        self._hosts[host.name()] = host
        self._host_mode[host.name()] = HostMode.ACTIVE
        self._dirty = True

    def _record_dependencies(self, job: Job) -> None:
        self._deps[job.id()] = job.dependency_ids()

    def _put_job(
        self, experiment: Experiment, job: LoadedJob, origin: str, owner: str
    ) -> str:
        """Add `job`, run by `owner`, to `experiment`, or give the known job
        of its id the new definition, keeping its status, unless it is
        running. Returns "added", "updated", "same", "running", "deleted"
        or "duplicate"."""
        if job.id() in self._deleted_jobs:
            return "deleted"
        job.set_owner(owner)
        old = self._jobs.get(job.id())
        if old is not None:
            if old.experiment() != experiment.name():
                return "duplicate"
            if isinstance(old, LoadedJob) and old.spec() == job.spec():
                return "same"
            if old.status().active():
                return "running"
            job.restore_runtime_state(old.runtime_state())
            experiment.remove_job(job.id())
            self._memory.forget(job.id())
        else:
            self._job_origin[job.id()] = origin
        experiment.register_job(job)
        self._jobs[job.id()] = job
        self._record_dependencies(job)
        self._dirty = True
        if old is None:
            self._unchecked.add(job.id())
            return "added"
        return "updated"

    def _check_dependencies(self, jobs: list[Job]) -> None:
        """Refuse a script whose jobs depend on unknown jobs, or on each
        other in a cycle. Called before anything is loaded."""
        defined = {job.id(): job for job in jobs}
        for job in defined.values():
            for parent_id in job.dependency_ids()[0]:
                if parent_id not in defined and parent_id not in self._jobs:
                    raise CommandError(
                        f"job {job.id()[:12]} depends on {parent_id[:12]}, "
                        "which is neither loaded nor registered in an "
                        "experiment of this script"
                    )
        graph = {job_id: plain for job_id, (plain, _) in self._deps.items()}
        for job_id, job in defined.items():
            graph[job_id] = job.dependency_ids()[0]
        done: set[str] = set()

        def visit(job_id: str, path: list[str]) -> None:
            if job_id in path:
                cycle = path[path.index(job_id) :] + [job_id]
                raise CommandError(
                    "dependency cycle: "
                    + " -> ".join(step[:12] for step in cycle)
                )
            if job_id in done:
                return
            for parent_id in graph.get(job_id, ()):
                visit(parent_id, path + [job_id])
            done.add(job_id)

        for job_id in graph:
            visit(job_id, [])

    def _load(self, compiled: dict, merge: bool) -> list[str]:
        """Add what a compiled script defines (see `script.compile_script`).
        With `merge` (reload), jobs already known take their new
        definition unless they are running. `compiled` is namespaced."""
        for bundle_id, packed in compiled["blobs"].items():
            self._keep_blob(bundle_id, base64.b64decode(packed))
        # NOTE: The blobs are kept as files; the state keeps only their ids.
        compiled = dict(compiled, blobs={})
        origin, owner = compiled["script"], compiled["owner"]
        new_hosts = [
            spec["name"]
            for spec in compiled["hosts"]
            if spec["name"] not in self._hosts
        ]
        if new_hosts and not self._caller.admin:
            raise CommandError(
                f"only an admin may add hosts ({', '.join(new_hosts)}); load "
                "a script without them, or ask an admin to add them"
            )
        experiments = [
            (spec, [LoadedJob(job) for job in spec["jobs"]])
            for spec in compiled["experiments"]
        ]
        templates = [
            (spec, [LoadedJob(job) for job in spec["instances"]])
            for spec in compiled["templates"]
        ]
        self._check_dependencies(
            [job for _, jobs in experiments + templates for job in jobs]
        )
        lines = []

        added_hosts = 0
        for spec in compiled["hosts"]:
            if spec["name"] in self._hosts:
                if not merge:
                    lines.append(f"host {spec['name']} already added; skipped")
                continue
            self._add_host(Host.from_spec(spec))
            lines.append(f"added host {spec['name']}")
            added_hosts += 1
        if added_hosts and any(path != origin for path in self._scripts):
            lines.append(
                "reload the other loaded scripts so their jobs can run on "
                "the new host(s)"
            )

        skipped_deleted = 0
        for spec, jobs in experiments:
            name = spec["name"]
            if name in self._killing:
                lines.append(f"experiment {name} is being killed; skipped")
                continue
            if name in self._deleted_experiments:
                lines.append(
                    f"experiment {name} was deleted; skipped (`undelete "
                    f"--experiment {name}` to bring it back)"
                )
                continue
            existing = self._experiments.get(name)
            if existing is not None and not merge:
                lines.append(
                    f"experiment {name} already loaded; use `reload` to add "
                    "new jobs to it"
                )
                continue
            if existing is None:
                existing = Experiment(name, Path(spec["outdir"]))
                self._experiments[name] = existing
                lines.append(f"added experiment {name}")
            counts: dict[str, int] = {}
            for job in jobs:
                outcome = self._put_job(existing, job, origin, owner)
                counts[outcome] = counts.get(outcome, 0) + 1
                if outcome == "duplicate":
                    lines.append(
                        f"job {job.id()[:8]} in {name} duplicates one in "
                        f"{self._jobs[job.id()].experiment()}; skipped"
                    )
            skipped_deleted += counts.get("deleted", 0)
            line = f"{name}: {counts.get('added', 0)} new job(s)"
            if counts.get("updated"):
                line += f", {counts['updated']} updated"
            if counts.get("running"):
                line += (
                    f", {counts['running']} running with their old "
                    "definition"
                )
            lines.append(line)
            defined = {job.id() for job in jobs}
            stale = [
                job
                for job in existing.jobs()
                if job.id() not in defined
                and self._job_origin.get(job.id()) == origin
            ]
            if merge and stale:
                lines.append(
                    f"{name}: {len(stale)} job(s) are no longer in the script "
                    "(kept)"
                )
        if skipped_deleted:
            lines.append(f"{skipped_deleted} deleted job(s) skipped")

        for spec, jobs in templates:
            if spec["experiment"] in self._deleted_experiments:
                continue
            new = spec["id"] not in self._templates
            self._templates[spec["id"]] = {
                "experiment": spec["experiment"],
                "outdir": spec["outdir"],
            }
            # NOTE: Templates aren't jobs, but `delete --script` needs to
            # know which script defined them.
            self._job_origin.setdefault(spec["id"], origin)
            experiment = self._experiments.get(spec["experiment"])
            if experiment is None:
                experiment = Experiment(spec["experiment"], Path(spec["outdir"]))
                self._experiments[spec["experiment"]] = experiment
            added = sum(
                self._put_job(experiment, job, origin, owner) == "added"
                for job in jobs
            )
            if new:
                lines.append(
                    f"added per-host job {spec['id']} ({len(jobs)} host(s))"
                )
            elif added:
                lines.append(f"per-host job {spec['id']}: {added} new host(s)")

        self._scripts[origin] = compiled["sha256"]
        self._compiled[origin] = compiled
        self._dirty = True
        return lines

    def _keep_blob(self, bundle_id: str, data: bytes) -> None:
        """Keep a packed bundle, once it unpacks to what its id says: ids
        are shared by every user, so a forged one must never get in."""
        if self._blob(bundle_id) is not None:
            return
        with tempfile.TemporaryDirectory() as scratch:
            try:
                unpack(data, bundle_id, Path(scratch))
            except (BundleError, OSError, EOFError, tarfile.TarError) as e:
                raise CommandError(f"bundle {bundle_id[:12]}: {e}")
        if self._blob_dir is None:
            self._blobs[bundle_id] = data
            return
        self._blob_dir.mkdir(exist_ok=True)
        tmp = self._blob_dir / f".{bundle_id}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, self._blob_dir / f"{bundle_id}.tar.gz")

    def _blob(self, bundle_id: str) -> bytes | None:
        if self._blob_dir is None:
            return self._blobs.get(bundle_id)
        try:
            return (self._blob_dir / f"{bundle_id}.tar.gz").read_bytes()
        except FileNotFoundError:
            return None

    def _ship(self, host: Host, job: Job) -> None:
        """Give `host` the job's bundle, unless it has it."""
        spec = job.bundle_spec()
        if spec is None or host.has_bundle(job, spec["id"]):
            return
        data = self._blob(spec["id"])
        if data is None:
            raise JobError(
                f"bundle {spec['id'][:12]} is gone from the scheduler; "
                "reload the job's script"
            )
        host.put_bundle(job, spec["id"], data)

    def _cmd_load(self, compiled: dict) -> list[str]:
        """Load what the console compiled, as the caller's."""
        compiled = self._namespaced(compiled, self._caller.user)
        if compiled["script"] in self._scripts:
            raise CommandError(
                f"{compiled['script']} is already loaded; use `reload` to "
                "pick up changes"
            )
        return self._load(compiled, merge=False)

    def _cmd_reload(self, compiled: dict) -> list[str]:
        return self._load(
            self._namespaced(compiled, self._caller.user), merge=True
        )

    def _experiment(self, name: str) -> Experiment:
        name = self._named(name)
        if name not in self._experiments:
            raise CommandError(f"no experiment named {name!r}")
        return self._experiments[name]

    def _host(self, name: str) -> Host:
        if name not in self._hosts:
            raise CommandError(f"no host named {name!r}")
        return self._hosts[name]

    def _resolve_job(self, prefix: str) -> Job:
        prefix = self._named(prefix)
        matches = [
            job for job_id, job in self._jobs.items()
            if job_id.startswith(prefix)
        ]
        if not matches:
            raise CommandError(f"no job id starts with {prefix!r}")
        if len(matches) > 1:
            raise CommandError(
                f"{prefix!r} matches {len(matches)} jobs; use a longer prefix"
            )
        return matches[0]

    def _cmd_kill(self, experiment: str) -> list[str]:
        name = self._experiment(experiment).name()
        self._require_mine(name, f"experiment {name}")
        self._killing.add(name)
        return [f"killing experiment {name}"]

    def _cmd_drain(self, host: str) -> list[str]:
        self._require_admin("drain hosts")
        self._host(host)
        self._host_mode[host] = HostMode.DRAINING
        return [f"host {host} won't get new jobs"]

    def _cmd_undrain(self, host: str) -> list[str]:
        self._require_admin("undrain hosts")
        self._host(host)
        self._host_mode[host] = HostMode.ACTIVE
        return [f"host {host} is accepting jobs again"]

    def _cmd_remove(self, host: str, force: bool = False) -> list[str]:
        self._require_admin("remove hosts")
        self._host(host)
        active = self._active_jobs(host)
        if force:
            self._drop_host(host)
            return [
                f"removed host {host}"
                + (
                    f"; {len(active)} job(s) on it are untracked, "
                    "`reset --force` them to run them elsewhere"
                    if active
                    else ""
                )
            ]
        self._host_mode[host] = HostMode.REMOVING
        return [
            f"host {host} will be removed once its {len(active)} running "
            "job(s) finish"
        ]

    def _cmd_capacity(
        self, host: str, cores: str | None = None, memory: str | None = None
    ) -> list[str]:
        """Limit what jobs may use on `host`: cores as N, +N or -N, memory
        as a size like 200G; "auto" for what the worker reports."""
        self._require_admin("change host limits")
        target = self._host(host)
        if cores is None and memory is None:
            raise CommandError("give --cores, --memory or both")
        try:
            if cores is not None and cores.startswith(("+", "-")):
                cores = str(max(0, target.cores() + int(cores)))
            target.set_limits(cores, memory)
        except (TypeError, ValueError) as e:
            raise CommandError(f"bad limit: {e}")
        memory_limit = target.memory()
        return [
            f"host {host}: {target.cores()} core(s), "
            + (
                f"{format_size(memory_limit)} for jobs"
                if memory_limit is not None
                else "memory unknown until its worker reports it"
            )
        ]

    def _cmd_partition(
        self,
        host: str,
        user: str,
        cores: str | None = None,
        memory: str | None = None,
        clear: bool = False,
    ) -> list[str]:
        """Give `user` (or EVERYONE, for users without their own) a share
        of `host`: cores as N, memory as a size, "none" for no limit.
        Once a host has partitions, users without one (and with no
        EVERYONE partition) can't run there."""
        self._require_admin("change partitions")
        target = self._host(host)
        lines = self._set_partition(host, user, cores, memory, clear)
        if target.up():
            owners = {job.owner() or self._user for job in self._active_jobs(host)}
            for owner in owners | ({user} - {EVERYONE}):
                self._cap_user(target, owner)
        return lines

    def _set_partition(
        self,
        host: str,
        user: str,
        cores: str | None,
        memory: str | None,
        clear: bool,
    ) -> list[str]:
        shares = self._partitions.setdefault(host, {})
        if clear:
            shares.pop(user, None)
            if not shares:
                del self._partitions[host]
            return [f"{host}: {user} has no partition"]
        if cores is None and memory is None:
            raise CommandError("give --cores, --memory, both, or --clear")
        share = dict(shares.get(user, {"cores": None, "memory": None}))
        try:
            if cores is not None:
                share["cores"] = None if cores == "none" else int(cores)
                if share["cores"] is not None and share["cores"] < 0:
                    raise ValueError(f"cores can't be negative: {cores}")
            if memory is not None:
                share["memory"] = None if memory == "none" else parse_size(memory)
        except (TypeError, ValueError) as e:
            raise CommandError(f"bad partition: {e}")
        shares[user] = share
        return [
            f"{host}: {user} gets "
            + ("any number of" if share["cores"] is None else str(share["cores"]))
            + " core(s) and "
            + (
                "any memory"
                if share["memory"] is None
                else format_size(share["memory"])
            )
            + f", and may borrow {BORROW_MARGIN:.0%} more from idle partitions"
        ]

    def _cmd_memory(self, job: str) -> list[str]:
        """A job's memory: the guess, the estimate and why, what it holds,
        and its recorded runs."""
        target = self._resolve_job(job)
        guess = target.memory_guess()
        lines = [
            f"{target.id()[:12]} ({target.shorthand_command()}): "
            f"{target.cores()} core(s), guess "
            + (format_size(guess) if guess else "none")
        ]
        if target.status().pending():
            estimate = self._estimate(target)
            lines.append(
                f"estimate {format_size(estimate.memory)} "
                f"({estimate.source}: {estimate.detail})"
            )
        else:
            lines.append(
                f"{target.status()}: reserved "
                f"{format_size(target.reserved())}, now "
                f"{format_size(target.memory_now())}, peak "
                f"{format_size(target.memory_peak())}"
            )
        runs = self._memory.runs(target.id())
        if runs:
            lines.append(f"{len(runs)} recorded run(s), newest first:")
        for run in runs[:10]:
            started = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(run["start_time"]))
                if run["start_time"]
                else "?"
            )
            lines.append(
                f"  {started} on {run['host']}: {run['outcome']}, peak "
                f"{format_size(run['peak'])}, reserved "
                f"{format_size(run['reserved'])}, "
                f"{run['co_jobs'] if run['co_jobs'] is not None else '?'} "
                "other job(s) on the host"
            )
        return lines

    def _cmd_files(self, job: str, match: str | None = None) -> list[str]:
        """The files a finished job left in its outdir, from the index its
        host keeps; only its owner and admins see them."""
        target = self._resolve_job(job)
        if not self.may_read(self._caller, target.id()):
            raise CommandError(
                f"only the owner of {target.id()[:12]} and admins see its files"
            )
        host = self._hosts.get(target.host() or "")
        if host is None or not host.up():
            raise CommandError(f"{target.id()[:12]}: its host is not connected")
        try:
            index = host.job_index(target.owner(), target.outdir())
        except (HostUnreachable, JobError) as e:
            raise CommandError(f"{target.id()[:12]}: {e}")
        if index is None:
            return [f"{target.id()[:12]}: no index yet (it hasn't ended)"]
        files = index["files"]
        shown = sorted(
            name for name in files if match is None or fnmatch(name, match)
        )
        lines = [
            f"{target.id()[:12]}: {len(files)} file(s), "
            f"{format_size(sum(entry[0] for entry in files.values()))}"
        ]
        for name in shown[:FILES_SHOWN]:
            lines.append(f"  {format_size(files[name][0]):>8}  {name}")
        if len(shown) > FILES_SHOWN:
            lines.append(f"  ... {len(shown) - FILES_SHOWN} more (use --match)")
        return lines

    def _cmd_resolve(self, targets: list[str]) -> list[dict]:
        """The jobs that experiment names and job id prefixes name, each
        once, as {"id", "experiment", "outdir", "host", "status"}, for
        `helper fetch`; only their owners and admins may ask. A target
        that names an experiment means all its jobs; otherwise it is a job
        id prefix, resolved as every other command resolves one."""
        if not targets:
            raise CommandError("give experiments or job ids")
        found: dict[str, Job] = {}
        for target in targets:
            name = self._named(target)
            if name in self._experiments:
                jobs = self._experiments[name].jobs()
            elif not any(job_id.startswith(name) for job_id in self._jobs):
                raise CommandError(
                    f"no experiment named {name!r}, and no job id starts "
                    f"with {name!r}"
                )
            else:
                jobs = [self._resolve_job(target)]
            for job in jobs:
                if not (self._caller.admin or job.owner() == self._caller.user):
                    raise CommandError(
                        f"only the owner of {job.id()[:12]} and admins see "
                        "its files"
                    )
                found.setdefault(job.id(), job)
        return [
            {
                "id": job.id(),
                "experiment": job.experiment(),
                "outdir": job.outdir().as_posix(),
                "host": job.host() or "",
                "status": job.status().value,
            }
            for job in found.values()
        ]

    def _cmd_signal(self, jobs: list[str], signal: str) -> list[str]:
        if signal not in self.SIGNALS:
            raise CommandError(
                f"signal must be one of {', '.join(self.SIGNALS)}"
            )
        lines = []
        for prefix in jobs:
            job = self._resolve_job(prefix)
            if not self._mine(job.id()):
                lines.append(f"{job.id()[:8]}: not yours")
                continue
            host = self._hosts.get(job.host() or "")
            if not job.status().active() or host is None:
                lines.append(f"{job.id()[:8]}: not running")
                continue
            if not host.up():
                lines.append(f"{job.id()[:8]}: host {host.name()} is down")
                continue
            try:
                sent = host.signal(job, self.SIGNALS[signal])
            except HostUnreachable as e:
                self._host_lost(host, e)
                lines.append(f"{job.id()[:8]}: {e}")
                continue
            except JobError as e:
                lines.append(f"{job.id()[:8]}: {e}")
                continue
            if sent and job.status() == JobStatus.PAUSED:
                # NOTE: A frozen job acts on a signal once it thaws.
                self._thaw(job, host)
            lines.append(
                f"{job.id()[:8]}: sent {signal}"
                if sent
                else f"{job.id()[:8]}: was not running"
            )
        return lines

    def _cmd_resume(self, jobs: list[str]) -> list[str]:
        """Let paused jobs go on; one paused for memory keeps what it
        uses as its reservation."""
        lines = []
        for prefix in jobs:
            job = self._resolve_job(prefix)
            self._require_mine(job.id(), f"job {job.id()[:12]}")
            host = self._hosts.get(job.host() or "")
            if job.status() != JobStatus.PAUSED or host is None:
                lines.append(f"{job.id()[:12]}: not paused")
                continue
            if not host.up():
                lines.append(f"{job.id()[:12]}: host {host.name()} is down")
                continue
            if self._thaw(job, host):
                job.set_reserved(max(job.reserved() or 0, job.memory_now() or 0))
                lines.append(f"{job.id()[:12]}: resumed")
        return lines

    def _cmd_retry(
        self, jobs: list[str], memory: str | None = None
    ) -> list[str]:
        """Run jobs again with more memory: `memory`, or their peak plus
        GRACE_FACTOR. Running or paused jobs are killed first."""
        try:
            floor = parse_size(memory) if memory is not None else None
        except ValueError as e:
            raise CommandError(f"bad memory: {e}")
        lines = []
        for prefix in jobs:
            job = self._resolve_job(prefix)
            self._require_mine(job.id(), f"job {job.id()[:12]}")
            if job.status().active():
                host = self._hosts.get(job.host() or "")
                if host is None or not host.up():
                    lines.append(f"{job.id()[:12]}: its host is down")
                    continue
                try:
                    host.signal(job, SIGNAL.KILL.value)
                except (HostUnreachable, JobError) as e:
                    lines.append(f"{job.id()[:12]}: {e}")
                    continue
                self._record_memory(job, host, JobStatus.KILLED.value)
                job.set_status(JobStatus.KILLED)
            wanted = floor or int(
                max(job.memory_peak() or 0, job.memory_now() or 0)
                * GRACE_FACTOR
            )
            if wanted:
                self._memory_floors[job.id()] = wanted
            job.reset()
            lines.append(
                f"{job.id()[:12]}: queued again"
                + (f" with at least {format_size(wanted)}" if wanted else "")
            )
        return lines

    def _cmd_reset(
        self,
        jobs: list[str] = (),
        experiment: str | None = None,
        status: str | None = None,
        force: bool = False,
    ) -> list[str]:
        targets: list[Job] = [self._resolve_job(prefix) for prefix in jobs]
        if experiment is not None:
            wanted = JobStatus(status) if status else None
            targets += [
                job
                for job in self._experiment(experiment).jobs()
                if (wanted is None and job.status().terminal())
                or job.status() == wanted
            ]
        if not targets:
            raise CommandError("no jobs to reset")
        for job in targets:
            self._require_mine(job.id(), f"job {job.id()[:12]}")
        lines, count = [], 0
        for job in targets:
            if job.status() == JobStatus.UNKNOWN and force:
                job.set_status(JobStatus.KILLED)
            if job.reset():
                count += 1
            else:
                lines.append(
                    f"{job.id()[:8]} is {job.status()}; "
                    + (
                        "use --force to requeue it anyway"
                        if job.status() == JobStatus.UNKNOWN
                        else "kill it first"
                    )
                )
        lines.append(f"requeued {count} job(s)")
        return lines

    def _cmd_stop(self, kill_jobs: bool = False) -> list[str]:
        self._require_admin("stop the scheduler")
        lines = []
        if kill_jobs:
            killed = 0
            for job in list(self._jobs.values()):
                host = self._hosts.get(job.host() or "")
                if not job.status().active() or host is None or not host.up():
                    continue
                try:
                    killed += host.signal(job, SIGNAL.KILL.value)
                except (HostUnreachable, JobError) as e:
                    lines.append(f"{job.id()[:8]}: {e}")
            lines.append(f"sent SIGKILL to {killed} job(s)")
        else:
            running = sum(
                1 for job in self._jobs.values() if job.status().active()
            )
            lines.append(
                f"{running} job(s) keep running; start the scheduler again "
                "to pick them back up"
            )
        self._stop.set()
        return lines

    def _refuse_if_active(self, jobs: Iterable[Job], what: str) -> None:
        active = [job for job in jobs if job.status().active()]
        if active:
            ids = ", ".join(job.id()[:8] for job in active[:10])
            more = f" and {len(active) - 10} more" if len(active) > 10 else ""
            raise CommandError(
                f"can't delete {what}: {len(active)} job(s) are still "
                f"running ({ids}{more}); kill them first"
            )

    def _cmd_delete(
        self,
        jobs: list[str] = (),
        experiment: str | None = None,
        script: str | None = None,
    ) -> list[str]:
        """Forget jobs, an experiment, or a script's jobs. Output files on
        disk are never touched."""
        if script is not None:
            path = self._script_key(script)
            if path not in self._scripts:
                raise CommandError(f"{script} is not a loaded script")
            self._require_mine(path, f"script {path}")
            owned = [
                job
                for job_id, job in self._jobs.items()
                if self._job_origin.get(job_id) == path
            ]
            self._refuse_if_active(owned, f"script {script}")
            self._refuse_if_needed(
                {job.id() for job in owned}, f"script {script}"
            )
            touched = {job.experiment() for job in owned}
            for job in owned:
                self._forget_job(job)
            for template_id in list(self._templates):
                if self._job_origin.get(template_id) == path:
                    self._forget_template(template_id)
            emptied = [
                name
                for name in sorted(touched)
                if name in self._experiments
                and not self._experiments[name].jobs()
            ]
            for name in emptied:
                self._remove_experiment(name)
            del self._scripts[path]
            del self._compiled[path]
            self._dirty = True
            return [
                f"deleted script {script}: {len(owned)} job(s)"
                + (f", experiment(s) {', '.join(emptied)}" if emptied else "")
                + "; it won't be loaded again unless you `load` it"
            ]

        if experiment is not None:
            target = self._experiment(experiment)
            name = target.name()
            self._require_mine(name, f"experiment {name}")
            self._refuse_if_active(target.jobs(), f"experiment {name}")
            self._refuse_if_needed(
                {job.id() for job in target.jobs()}, f"experiment {name}"
            )
            count = len(target.jobs())
            self._remove_experiment(name)
            self._deleted_experiments.add(name)
            return [f"deleted experiment {name} and its {count} job(s)"]

        if not jobs:
            raise CommandError("nothing to delete")
        lines, deleted = [], 0
        targets = [self._resolve_job(prefix) for prefix in jobs]
        # NOTE: Jobs deleted together may depend on each other.
        selected = {job.id() for job in targets}
        for job in targets:
            if not self._mine(job.id()):
                lines.append(f"{job.id()[:8]}: skipped, not yours")
                continue
            if job.status().active():
                lines.append(f"{job.id()[:8]}: skipped, it is {job.status()}")
                continue
            dependents = self._pending_dependents(selected | {job.id()})
            needing = [
                d for d in dependents
                if job.id() in self._deps[d.id()][0]
                or any(
                    PerHostJob.instance_id(t, job.pinned_host() or "")
                    == job.id()
                    for t in self._deps[d.id()][1]
                )
            ]
            if needing:
                lines.append(
                    f"{job.id()[:8]}: skipped, {len(needing)} job(s) that "
                    "haven't run yet depend on it"
                )
                continue
            self._forget_job(job)
            self._deleted_jobs.add(job.id())
            deleted += 1
        lines.append(f"deleted {deleted} job(s)")
        return lines

    def _cmd_undelete(
        self, jobs: list[str] = (), experiment: str | None = None
    ) -> list[str]:
        """Stop skipping deleted jobs or an experiment, and load every
        loaded script again, as compiled, so they come back."""
        restored = 0
        if experiment is not None:
            name = self._named(experiment)
            if name not in self._deleted_experiments:
                raise CommandError(f"experiment {name} isn't deleted")
            self._require_mine(name, f"experiment {name}")
            self._deleted_experiments.discard(name)
            restored += 1
        for prefix in jobs:
            prefix = self._named(prefix)
            matches = [i for i in self._deleted_jobs if i.startswith(prefix)]
            if len(matches) != 1:
                raise CommandError(
                    f"{prefix!r} matches {len(matches)} deleted jobs"
                )
            self._require_mine(matches[0], f"job {matches[0][:12]}")
            self._deleted_jobs.discard(matches[0])
            restored += 1
        if not restored:
            raise CommandError("nothing to undelete")
        lines = []
        for path in list(self._scripts):
            try:
                lines += self._load(self._compiled[path], merge=True)
            except Exception as e:
                lines.append(f"reloading {path} failed: {e}")
        return lines
