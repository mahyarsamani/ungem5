from __future__ import annotations

import json

from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence, Tuple

from .resources import Resources

if TYPE_CHECKING:
    from ..schedule.host import Host
    from .bundle import Bundle

MEMORY_EXCEED_ACTIONS = ("pause", "kill", "retry")


class JobStatus(Enum):
    QUEUED = "queued"
    LAUNCHING = "launching"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    KILLED = "killed"
    # NOTE: The job was launched but its host can't be reached, so we don't
    # know. It is never relaunched automatically.
    UNKNOWN = "unknown"
    # NOTE: Not launched yet because of dependencies: still to finish
    # (waiting), or failed and need a reset (blocked).
    WAITING = "waiting"
    BLOCKED = "blocked"
    # NOTE: Frozen on its host for memory: it keeps what it holds, and
    # waits for `resume`, `retry` or a kill.
    PAUSED = "paused"

    def color(self) -> str:
        return {
            JobStatus.WAITING: "#93C5FD",
            JobStatus.BLOCKED: "#F97316",
            JobStatus.QUEUED: "#FAFAFA",
            JobStatus.LAUNCHING: "#F59E0B",
            JobStatus.RUNNING: "#10B981",
            JobStatus.PAUSED: "#FACC15",
            JobStatus.EXITED: "#6B7280",
            JobStatus.FAILED: "#EF4444",
            JobStatus.KILLED: "#090A0D",
            JobStatus.UNKNOWN: "#A855F7",
        }[self]

    def active(self) -> bool:
        """The job may be running on its host and holds host capacity."""
        return self in (
            JobStatus.LAUNCHING,
            JobStatus.RUNNING,
            JobStatus.PAUSED,
            JobStatus.UNKNOWN,
        )

    def terminal(self) -> bool:
        return self in (JobStatus.EXITED, JobStatus.FAILED, JobStatus.KILLED)

    def pending(self) -> bool:
        """Not launched yet: queued, waiting or blocked."""
        return self in (JobStatus.QUEUED, JobStatus.WAITING, JobStatus.BLOCKED)

    def __str__(self) -> str:
        return self.value


class Job:
    def __init__(
        self,
        experiment: str,
        cwd: Path,
        command: str,
        shorthand_command: str,
        outdir: Path,
        resources: Resources | None,
        id: str,
        aux_files: Sequence[Tuple[str, Path]] = (),
        dumps: Sequence[Tuple[str, str, Path]] = (),
        after: Iterable["Job | PerHostJob"] = (),
    ) -> None:
        """
        :param resources: cores and a memory guess; None is one core and
            no guess.
        :param aux_files: (label, path) of extra output files to show on the
            dashboard, e.g. ("stats", outdir / "stats.txt"). Paths must be
            inside `outdir`.
        :param dumps: (label, content, path) of files the worker writes
            before launching the job. Paths must be inside `outdir`.
        :param after: jobs this one depends on; see `depends_on`.
        """
        self._experiment = experiment
        self._cwd = Path(cwd)
        self._command = command
        self._shorthand_command = shorthand_command
        self._outdir = Path(outdir)
        self._resources = Resources.of(resources)
        self._id = id
        if not (isinstance(self.nice, int) and 0 <= self.nice <= 19):
            raise ValueError(f"{id}: nice must be 0 to 19, not {self.nice!r}")
        if self.on_memory_exceed not in MEMORY_EXCEED_ACTIONS:
            raise ValueError(
                f"{id}: on_memory_exceed must be one of "
                f"{', '.join(MEMORY_EXCEED_ACTIONS)}, not "
                f"{self.on_memory_exceed!r}"
            )
        self._aux_files = tuple(aux_files)
        self._dumps = tuple(dumps)

        # NOTE: Runtime state, owned by the scheduler.
        self._status = JobStatus.QUEUED
        self._host_name: str | None = None
        self._returncode: int | None = None
        self._message: str | None = None
        # NOTE: Epoch seconds on the worker's clock, from the job's records.
        self._start_time: float | None = None
        self._end_time: float | None = None
        # NOTE: Requeues asked for by the watcher since the last `reset`.
        self._retries = 0
        # NOTE: Bytes the scheduler set aside for this run, and what the
        # runner last measured (now and peak so far).
        self._reserved: int | None = None
        self._memory_now: int | None = None
        self._memory_peak: int | None = None
        # NOTE: Why a PAUSED job was frozen: "memory" (it outgrew its
        # reservation) or "host" (its host ran low on memory).
        self._paused_for: str | None = None

        self._parents: list[Job | PerHostJob] = []
        # NOTE: Set on the instances of a PerHostJob: the only host they
        # may run on.
        self._pinned_host: str | None = None
        # NOTE: The user it runs as; the scheduler sets it on load.
        self._owner: str | None = None
        # NOTE: The Python it runs, shipped to its host (see bundle.py);
        # None runs it in `cwd` as it is.
        self._bundle: "Bundle | None" = None
        self.depends_on(*after)

    def depends_on(self, *parents: "Job | PerHostJob") -> "Job":
        """Run this job only after `parents` have exited successfully.

        A Job parent has to exit once, on any host. A PerHostJob parent
        has to exit on the host this job runs on, so this job only runs on
        hosts where it did. Returns self, so calls can be chained.
        """
        for parent in parents:
            if not isinstance(parent, (Job, PerHostJob)):
                raise TypeError(
                    f"{self._id}: a dependency must be a Job or a "
                    f"PerHostJob, not {type(parent).__name__}"
                )
            if parent is self:
                raise ValueError(f"{self._id} can't depend on itself")
            if all(parent is not known for known in self._parents):
                self._parents.append(parent)
        return self

    def parents(self) -> list["Job | PerHostJob"]:
        return list(self._parents)

    def dependency_ids(self) -> tuple[list[str], list[str]]:
        """Ids of the jobs and of the per-host jobs this one depends on."""
        return (
            [p.id() for p in self._parents if isinstance(p, Job)],
            [p.id() for p in self._parents if isinstance(p, PerHostJob)],
        )

    def pinned_host(self) -> str | None:
        return self._pinned_host

    def bundle(self) -> "Bundle | None":
        return self._bundle

    def bundle_spec(self) -> dict | None:
        """The bundle as its host needs it: id, root and data paths."""
        return self._bundle.spec() if self._bundle is not None else None

    def owner(self) -> str | None:
        return self._owner

    def set_owner(self, user: str) -> None:
        self._owner = user

    # NOTE: Watcher settings; override in a subclass along with `watcher`.
    watch_interval = 60
    watch_timeout = 60
    max_retries = 3
    # NOTE: Process settings; override in a subclass. The job runs with
    # this nice value (0 to 19), and its memory is sampled this often.
    nice = 0
    memory_interval = 120
    # NOTE: What the scheduler does when the job outgrows its reservation
    # and its host has no room to grant it more: "pause" it until you
    # decide, "kill" it (it fails), or "retry" it with more memory.
    on_memory_exceed = "pause"

    def compatible_with_host(self, host: "Host") -> bool:
        """Whether this job may run on `host`. Override to restrict it,
        e.g. `return host.isa() == "aarch64"`."""
        return True

    def watcher(self) -> str | None:
        """A script that checks on this job, or None for no checks.

        The worker's runner runs it every `watch_interval` seconds while the
        job runs (killing it after `watch_timeout` seconds), and once after
        the job ends. It runs with bash, or with its own interpreter if it
        starts with `#!`, in the job's outdir, with JOB_OUTDIR, JOB_ID,
        JOB_PID, JOB_ELAPSED, JOB_RUNNING (1 or 0) and, after the job ends,
        JOB_EXIT_CODE set. The first line it prints decides:

            nothing or "ok"     keep going
            "fail <message>"    kill the job; it ends as failed
            "retry <message>"   kill the job and queue it again, at most
                                `max_retries` times, then it fails

        Its stderr and every verdict go to .job/watcher.log.
        """
        pass

    def experiment(self) -> str:
        return self._experiment

    def cwd(self) -> Path:
        return self._cwd

    def command(self) -> str:
        return self._command

    def shorthand_command(self) -> str:
        return self._shorthand_command

    def display_command(self) -> str:
        """The command to show people; subclasses whose `command` is a
        wrapper (e.g. gem5Job) show what it runs instead."""
        return self._command

    def outdir(self) -> Path:
        return self._outdir

    def id(self) -> str:
        return self._id

    def resources(self) -> Resources:
        return self._resources

    def cores(self) -> int:
        return self._resources.cores

    def memory_guess(self) -> int | None:
        return self._resources.memory

    def features(self) -> dict:
        """What decides this job's memory use, for the scheduler's estimate.

        Jobs with equal features are expected to peak alike, so a rerun
        reserves what the last runs used. Values must be JSON. Override
        to describe your job better (gem5Job does); never include the
        host or anything that changes between identical runs.
        """
        return {"experiment": self._experiment, "command": self._command}

    def memory_group(self) -> str:
        """Jobs that share this tend to be alike in size, so the scheduler
        uses their records to correct your `memory` guess."""
        return self._experiment

    def files(self) -> Tuple[Tuple[str, Path], ...]:
        job_dir = self._outdir / ".job"
        watcher = (
            (
                ("watcher", job_dir / "watcher"),
                ("watcher.log", job_dir / "watcher.log"),
            )
            if self.watcher() is not None
            else ()
        )
        return (
            ("stdout", self._outdir / "stdout"),
            ("stderr", self._outdir / "stderr"),
            *self._aux_files,
            *((label, path) for label, _, path in self._dumps),
            *watcher,
        )

    def dumps(self) -> Tuple[Tuple[str, str, Path], ...]:
        job_dir = self._outdir / ".job"
        process = json.dumps(
            {"nice": self.nice, "memory_interval": self.memory_interval}
        )
        dumps = (*self._dumps, ("process", process, job_dir / "process.json"))
        spec = self.bundle_spec()
        if spec is not None:
            dumps += (
                (
                    "resources",
                    json.dumps(spec, indent=1),
                    job_dir / "resources.json",
                ),
            )
        script = self.watcher()
        if script is None:
            return dumps
        config = json.dumps(
            {"interval": self.watch_interval, "timeout": self.watch_timeout}
        )
        return (
            *dumps,
            ("watcher", script, job_dir / "watcher"),
            ("watcher config", config, job_dir / "watcher.json"),
        )

    def status(self) -> JobStatus:
        return self._status

    def host(self) -> str | None:
        return self._host_name

    def returncode(self) -> int | None:
        return self._returncode

    def message(self) -> str | None:
        return self._message

    def start_time(self) -> float | None:
        return self._start_time

    def end_time(self) -> float | None:
        return self._end_time

    def reserved(self) -> int | None:
        return self._reserved

    def memory_now(self) -> int | None:
        return self._memory_now

    def memory_peak(self) -> int | None:
        return self._memory_peak

    def set_memory(self, now: int | None, peak: int | None) -> None:
        """What the runner measured; `now` is None once the job ended."""
        self._memory_now = now
        if peak is not None:
            self._memory_peak = max(peak, self._memory_peak or 0)

    def set_reserved(self, reserved: int) -> None:
        self._reserved = reserved

    def paused_for(self) -> str | None:
        return self._paused_for

    def pause(self, reason: str, message: str) -> None:
        self._status = JobStatus.PAUSED
        self._paused_for = reason
        self._message = message

    def unpause(self) -> None:
        self._status = JobStatus.RUNNING
        self._paused_for = None
        self._message = None

    def set_status(
        self,
        status: JobStatus,
        returncode: int | None = None,
        message: str | None = None,
    ) -> None:
        self._status = status
        if returncode is not None:
            self._returncode = returncode
        if message is not None:
            self._message = message

    def set_times(self, start: float | None, end: float | None) -> None:
        if start is not None:
            self._start_time = start
        if end is not None:
            self._end_time = end

    def _clear_run(self) -> None:
        self._returncode = None
        self._message = None
        self._start_time = None
        self._end_time = None
        self._reserved = None
        self._memory_now = None
        self._memory_peak = None
        self._paused_for = None

    def assign(self, host_name: str, reserved: int | None = None) -> None:
        self._host_name = host_name
        self._status = JobStatus.LAUNCHING
        self._clear_run()
        self._reserved = reserved

    def set_pending(self, status: JobStatus, reason: str | None) -> None:
        """Queued, waiting or blocked, as its dependencies allow."""
        self._status = status
        self._message = reason

    def reset(self) -> bool:
        if self._status.active():
            return False
        self._status = JobStatus.QUEUED
        self._host_name = None
        self._clear_run()
        self._retries = 0
        return True

    def retries(self) -> int:
        return self._retries

    def retry(self, message: str) -> bool:
        """Queue the job again for its watcher, unless it has used up
        `max_retries`. Returns whether it was queued."""
        if self._retries >= self.max_retries:
            return False
        self._retries += 1
        self._status = JobStatus.QUEUED
        self._host_name = None
        self._clear_run()
        self._message = f"retry {self._retries}/{self.max_retries}: {message}"
        return True

    def forget(self) -> None:
        """Requeue a job whose host has no record of it."""
        self._status = JobStatus.QUEUED
        self._host_name = None

    def runtime_state(self) -> dict:
        return {
            "experiment": self._experiment,
            "outdir": str(self._outdir),
            "status": self._status.value,
            "host": self._host_name,
            "returncode": self._returncode,
            "message": self._message,
            "start_time": self._start_time,
            "end_time": self._end_time,
            "retries": self._retries,
            "reserved": self._reserved,
            "memory_now": self._memory_now,
            "memory_peak": self._memory_peak,
            "paused_for": self._paused_for,
        }

    def restore_runtime_state(self, state: dict) -> None:
        status = JobStatus(state["status"])
        # NOTE: A launch that was in flight when the scheduler died may or may
        # not have happened; treat it like any job we have to ask about.
        if status == JobStatus.LAUNCHING:
            status = JobStatus.UNKNOWN
        self._status = status
        self._host_name = state.get("host")
        self._returncode = state.get("returncode")
        self._message = state.get("message")
        self._start_time = state.get("start_time")
        self._end_time = state.get("end_time")
        self._retries = state.get("retries", 0)
        self._reserved = state.get("reserved")
        self._memory_now = state.get("memory_now")
        self._memory_peak = state.get("memory_peak")
        self._paused_for = state.get("paused_for")

    def view(self) -> dict:
        return {
            "id": self._id,
            "experiment": self._experiment,
            "command": self._shorthand_command,
            "full_command": self.display_command(),
            "cores": self.cores(),
            "memory_guess": self.memory_guess(),
            "reserved": self._reserved,
            "memory_now": self._memory_now,
            "memory_peak": self._memory_peak,
            "paused_for": self._paused_for,
            "on_memory_exceed": self.on_memory_exceed,
            "nice": self.nice,
            "host": self._host_name or "",
            "status": self._status.value,
            "status_color": self._status.color(),
            "returncode": self._returncode,
            "message": self._message or "",
            "start_time": self._start_time,
            "end_time": self._end_time,
            "retries": self._retries,
            "pinned_host": self._pinned_host,
            "files": [label for label, _ in self.files()],
            "outdir": str(self._outdir),
        }

    def compile(self, hosts: Iterable[str]) -> dict:
        """This job as plain data for the scheduler: what its methods say
        now. `hosts` are the names of the hosts it is compatible with."""
        after, after_per_host = self.dependency_ids()
        return {
            "id": self._id,
            "experiment": self._experiment,
            "cwd": str(self._cwd),
            "command": self._command,
            "shorthand_command": self._shorthand_command,
            "display_command": self.display_command(),
            "outdir": str(self._outdir),
            "cores": self.cores(),
            "memory": self.memory_guess(),
            "nice": self.nice,
            "memory_interval": self.memory_interval,
            "max_retries": self.max_retries,
            "on_memory_exceed": self.on_memory_exceed,
            "files": [[label, str(path)] for label, path in self.files()],
            "dumps": [
                [label, content, str(path)]
                for label, content, path in self.dumps()
            ],
            "features": self.features(),
            "memory_group": self.memory_group(),
            "bundle": self.bundle_spec(),
            "after": after,
            "after_per_host": after_per_host,
            "pinned_host": self._pinned_host,
            "hosts": sorted(hosts),
        }

    def __str__(self):
        return (
            f"{self.__class__.__name__}(id={self._id[:8]}, "
            f"command={self._shorthand_command}, status={self._status})"
        )

    def __repr__(self):
        return self.__str__()


class LoadedJob(Job):
    """A job as the scheduler holds it: the data `Job.compile` made of it
    in the console, so the scheduler never runs a script's code."""

    def __init__(self, spec: dict) -> None:
        # NOTE: Before Job.__init__, which checks nice.
        self.nice = spec["nice"]
        self.memory_interval = spec["memory_interval"]
        self.max_retries = spec["max_retries"]
        self.on_memory_exceed = spec["on_memory_exceed"]
        super().__init__(
            spec["experiment"],
            Path(spec["cwd"]),
            spec["command"],
            spec["shorthand_command"],
            Path(spec["outdir"]),
            Resources(spec["cores"], spec["memory"]),
            spec["id"],
        )
        self._spec = spec
        self._pinned_host = spec["pinned_host"]
        self._hosts = frozenset(spec["hosts"])

    def spec(self) -> dict:
        return self._spec

    def display_command(self) -> str:
        return self._spec["display_command"]

    def files(self) -> Tuple[Tuple[str, Path], ...]:
        return tuple((label, Path(path)) for label, path in self._spec["files"])

    def dumps(self) -> Tuple[Tuple[str, str, Path], ...]:
        return tuple(
            (label, content, Path(path))
            for label, content, path in self._spec["dumps"]
        )

    def features(self) -> dict:
        return self._spec["features"]

    def memory_group(self) -> str:
        return self._spec["memory_group"]

    def bundle_spec(self) -> dict | None:
        return self._spec["bundle"]

    def dependency_ids(self) -> tuple[list[str], list[str]]:
        return list(self._spec["after"]), list(self._spec["after_per_host"])

    def compatible_with_host(self, host: "Host") -> bool:
        return host.name() in self._hosts


class PerHostJob:
    """A job to run once on each host, like a build to a host-local disk.

    The scheduler makes one instance (a Job, from `make_job`) for every
    host this accepts in `compatible_with_host`, as soon as it knows the
    host, and runs it there. A job that depends on a PerHostJob runs only
    on hosts where its instance exited successfully.
    """

    def __init__(self, experiment: str, id: str, outdir: Path) -> None:
        self._experiment = experiment
        self._id = id
        self._outdir = Path(outdir)

    def experiment(self) -> str:
        return self._experiment

    def id(self) -> str:
        return self._id

    def outdir(self) -> Path:
        return self._outdir

    def compatible_with_host(self, host: "Host") -> bool:
        """Which hosts get an instance. Override to restrict it."""
        return True

    def make_job(self, host_name: str, id: str, outdir: Path) -> Job:
        """The Job to run on `host_name`, with this id and outdir."""
        raise NotImplementedError

    @staticmethod
    def instance_id(template_id: str, host_name: str) -> str:
        return f"{template_id}@{host_name}"

    def instance(self, host_name: str) -> Job:
        job = self.make_job(
            host_name,
            self.instance_id(self._id, host_name),
            self._outdir / host_name,
        )
        job._pinned_host = host_name
        return job

    def __str__(self) -> str:
        return f"{self.__class__.__name__}(id={self._id})"

    def __repr__(self) -> str:
        return self.__str__()


class Experiment:
    def __init__(self, name: str, outdir: Path) -> None:
        self._name = name
        self._outdir = Path(outdir)
        self._jobs: dict[str, Job] = dict()
        self._per_host: dict[str, PerHostJob] = dict()

    def name(self) -> str:
        return self._name

    def outdir(self) -> Path:
        return self._outdir

    def register_job(self, job: "Job | PerHostJob") -> bool:
        """Returns False (and ignores the job) if its id is already here."""
        if isinstance(job, PerHostJob):
            if job.id() in self._per_host:
                return False
            self._per_host[job.id()] = job
            return True
        if job.id() in self._jobs:
            return False
        self._jobs[job.id()] = job
        return True

    def per_host_jobs(self) -> list[PerHostJob]:
        return list(self._per_host.values())

    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def remove_job(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def queued(self) -> list[Job]:
        return [
            job
            for job in self._jobs.values()
            if job.status() == JobStatus.QUEUED
        ]

    def __str__(self) -> str:
        return (
            f"{self.__class__.__name__}(name={self._name}, "
            f"outdir={self._outdir}, jobs={len(self._jobs)})"
        )

    def __repr__(self):
        return self.__str__()


def find_jobs(experiments: Iterable[Experiment]) -> Iterable[Job]:
    for experiment in experiments:
        yield from experiment.jobs()


class ProjectConfiguration:

    def name(self):
        raise NotImplementedError

    def base_dir(self):
        raise NotImplementedError

    def get_experiment_dir(self, experiment: Experiment) -> Path:
        raise NotImplementedError
