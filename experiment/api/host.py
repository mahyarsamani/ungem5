"""The scheduler's handle on one worker machine."""

import json
import ssl
import threading
import time

from enum import Enum
from pathlib import Path

import rpyc

from ..common import pki
from .resources import format_size, parse_size
from .work import Job, JobStatus
from .worker import PROTOCOL_CONFIG

MAX_BACKOFF = 60.0
# NOTE: Without a memory limit, jobs may reserve this share of the
# machine's memory; the rest is left to the OS and everyone else.
DEFAULT_MEMORY_SHARE = 0.9
RESOURCES_REFRESH_SECS = 10.0

# NOTE: `uname -m` names, so a job can pick e.g. the matching venv or binary.
ISAS = ("x86_64", "aarch64", "riscv64")


class SIGNAL(Enum):
    TERM = 15
    INT = 2
    QUIT = 3
    KILL = 9


class JobError(Exception):
    """An operation on a single job failed; the host itself is fine."""


class HostUnreachable(Exception):
    """The connection to the host failed; it will be retried."""


class HostState(Enum):
    DOWN = "down"
    UP = "up"

    def __str__(self) -> str:
        return self.value


def _is_remote(exception: BaseException) -> bool:
    # NOTE: rpyc re-raises exceptions from the worker locally and tags them
    # with the remote traceback.
    return hasattr(exception, "_remote_tb")


def remote_message(exception: BaseException) -> str:
    """'Type: message' of a remote exception, without its traceback."""
    lines = str(getattr(exception, "_remote_tb", "")).strip().splitlines()
    return lines[-1] if lines else f"{type(exception).__name__}: {exception}"


class Host:
    def __init__(
        self,
        name: str,
        isa: str,
        domain: str,
        cores: int | None = None,
        memory: int | str | None = None,
        port: int = 9100,
        *,
        cert: Path | str | None = None,
        key: Path | str | None = None,
        ca: Path | str | None = None,
        insecure: bool = False,
    ) -> None:
        """
        :param isa: the machine's architecture as `uname -m` prints it, one
            of ISAS. Jobs can use it in `Job.compatible_with_host`.
        :param cores: how many cores jobs may keep busy here; None for all
            the cores the worker reports.
        :param memory: how much memory jobs may reserve here, in bytes or
            as "200G"; None for DEFAULT_MEMORY_SHARE of what the worker
            reports.
        :param cert, key, ca: the scheduler's client certificate, its key,
            and the CA that signed the worker's certificate. Default to
            `scheduler.crt`, `scheduler.key` and `ca.crt` in the pki dir of
            whoever runs the scheduler, looked up when it connects.
        :param insecure: connect without TLS. Only for a worker started with
            `--insecure-localhost` on this machine.
        """
        if isa not in ISAS:
            raise ValueError(
                f"Host {name}: isa must be one of {', '.join(ISAS)}, "
                f"not {isa!r}"
            )
        self._name = name
        self._isa = isa
        self._domain = domain
        self._cores: int | None = None
        self._memory: int | None = None
        self.set_limits(cores, memory)
        self._port = port
        # NOTE: None for the defaults: a host compiled in a console must not
        # carry the console user's paths to the scheduler.
        self._cert = Path(cert) if cert else None
        self._key = Path(key) if key else None
        self._ca = Path(ca) if ca else None
        self._insecure = insecure

        self._connection = None
        self._lock = threading.RLock()
        self._state = HostState.DOWN
        self._last_error: str | None = None
        self._failures = 0
        self._next_attempt = 0.0
        # NOTE: (cores, total memory, available memory) from the worker.
        self._reported: tuple[int, int, int] | None = None
        self._reported_at = 0.0

    def name(self) -> str:
        return self._name

    def isa(self) -> str:
        return self._isa

    def domain(self) -> str:
        return self._domain

    def set_limits(
        self,
        cores: "int | str | None" = None,
        memory: "int | str | None" = None,
    ) -> None:
        """Limits for jobs on this host; "auto" goes back to what the
        worker reports, None leaves a limit as it is."""
        if cores == "auto":
            self._cores = None
        elif cores is not None:
            if int(cores) < 0:
                raise ValueError(f"cores can't be negative: {cores}")
            self._cores = int(cores)
        if memory == "auto":
            self._memory = None
        elif memory is not None:
            self._memory = parse_size(memory)

    def cores(self) -> int:
        """Cores jobs may use: the limit, or what the worker reported."""
        if self._cores is not None:
            return self._cores
        return self._reported[0] if self._reported else 0

    def memory(self) -> int | None:
        """Bytes jobs may reserve, or None if there's no limit and the
        worker hasn't reported yet: then memory isn't limited."""
        if self._memory is not None:
            return self._memory
        if self._reported:
            return int(self._reported[1] * DEFAULT_MEMORY_SHARE)
        return None

    def available_memory(self) -> int | None:
        """Bytes free on the machine right now, as the worker last said."""
        return self._reported[2] if self._reported else None

    def limits(self) -> dict:
        return {"cores": self._cores, "memory": self._memory}

    def reported(self) -> tuple[int, int, int] | None:
        return self._reported

    def reported_at(self) -> float:
        """When the worker last reported its resources."""
        return self._reported_at

    def refresh_resources(self, force: bool = False) -> None:
        """Ask the worker for its cores and memory, at most every
        RESOURCES_REFRESH_SECS."""
        now = time.time()
        if not force and now - self._reported_at < RESOURCES_REFRESH_SECS:
            return
        self._reported_at = now
        cores, total, available = self._call("host_resources")
        self._reported = (int(cores), int(total), int(available))

    def state(self) -> HostState:
        return self._state

    def up(self) -> bool:
        return self._state == HostState.UP

    def last_error(self) -> str | None:
        return self._last_error

    def failures(self) -> int:
        """Consecutive failed connection attempts."""
        return self._failures

    def due_for_reconnect(self, now: float) -> bool:
        return self._state == HostState.DOWN and now >= self._next_attempt

    def _open(self):
        if self._insecure:
            if self._domain not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError(
                    "insecure connections are only allowed to localhost"
                )
            return rpyc.connect(
                self._domain,
                self._port,
                config=PROTOCOL_CONFIG,
                keepalive=True,
            )
        default_cert, default_key = pki.paths(pki.SCHEDULER)
        cert = self._cert or default_cert
        key = self._key or default_key
        ca = self._ca or pki.ca_path()
        for path in (cert, key, ca):
            if not path.exists():
                raise FileNotFoundError(
                    f"{path} is missing. See `helper certs --help`."
                )
        pki.check_key_permissions(key)
        # NOTE: Passing cert_reqs explicitly keeps hostname checking on: the
        # worker's certificate must name `domain`.
        return rpyc.ssl_connect(
            self._domain,
            self._port,
            keyfile=str(key),
            certfile=str(cert),
            ca_certs=str(ca),
            cert_reqs=ssl.CERT_REQUIRED,
            config=PROTOCOL_CONFIG,
            keepalive=True,
        )

    def connect(self) -> bool:
        """Try to connect. On failure, schedule the next attempt."""
        with self._lock:
            if self._state == HostState.UP:
                return True
            try:
                self._connection = self._open()
                self._connection.root.ping()
            except Exception as e:
                self._mark_down(e)
                return False
            self._state = HostState.UP
            self._failures = 0
            self._last_error = None
        try:
            self.refresh_resources(force=True)
        except HostUnreachable:
            return False
        return True

    def disconnect(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception:
                    pass
            self._connection = None
            self._state = HostState.DOWN

    def _mark_down(self, exception: BaseException) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass
        self._connection = None
        self._state = HostState.DOWN
        self._failures += 1
        self._last_error = f"{type(exception).__name__}: {exception}"
        self._next_attempt = time.time() + min(
            MAX_BACKOFF, 2.0 ** min(self._failures, 6)
        )

    def _call(self, method: str, *args):
        if self._state != HostState.UP:
            # NOTE: Don't wait on the lock while a reconnect holds it.
            raise HostUnreachable(f"{self._name} is not connected")
        with self._lock:
            if self._state != HostState.UP or self._connection is None:
                raise HostUnreachable(f"{self._name} is not connected")
            try:
                return getattr(self._connection.root, method)(*args)
            except Exception as e:
                if _is_remote(e):
                    raise JobError(remote_message(e)) from None
                self._mark_down(e)
                raise HostUnreachable(
                    f"{self._name}: {self._last_error}"
                ) from e

    # NOTE: Every call names the job's owner, whom the worker acts as.

    def has_bundle(self, job: Job, bundle_id: str) -> bool:
        return bool(self._call("has_bundle", job.owner(), bundle_id))

    def put_bundle(self, job: Job, bundle_id: str, data: bytes) -> None:
        self._call("put_bundle", job.owner(), bundle_id, data)

    def launch(self, job: Job) -> None:
        spec = job.bundle_spec()
        self._call(
            "launch_job",
            job.id(),
            job.owner(),
            job.cwd().as_posix(),
            job.command(),
            job.outdir().as_posix(),
            tuple((label, path.as_posix()) for label, path in job.files()),
            tuple((content, path.as_posix()) for _, content, path in job.dumps()),
            (
                None
                if spec is None
                else (spec["id"], spec["root"], tuple(spec["data"]))
            ),
        )

    def job_index(self, job: Job) -> dict | None:
        """The index of the job's outdir (see `runner.write_index`), or
        None before the job ends."""
        return json.loads(
            self._call("job_index", job.owner(), job.outdir().as_posix())
        )

    def statuses(self, jobs: list[Job]) -> list[tuple]:
        """(status, returncode, message, start_time, end_time, memory) per
        job; see `userio.resolve_status`."""
        if not jobs:
            return []
        results = self._call(
            "job_statuses",
            tuple((job.owner(), job.outdir().as_posix()) for job in jobs),
        )
        return [tuple(result) for result in results]

    def finished_runs(self, jobs: list[Job]) -> list[tuple | None]:
        """(job id, status) of the run that ended in each job's outdir,
        or None; see `userio.finished_runs`."""
        if not jobs:
            return []
        results = self._call(
            "finished_runs",
            tuple((job.owner(), job.outdir().as_posix()) for job in jobs),
        )
        return [
            None if run is None else (run[0], tuple(run[1])) for run in results
        ]

    def memory_trace(self, job: Job) -> list[dict]:
        """The job's memory samples; empty if the worker has none."""
        data = self._call("memory_trace", job.owner(), job.outdir().as_posix())
        samples = []
        for line in bytes(data).decode(errors="replace").splitlines():
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return samples

    def freeze(self, job: Job, frozen: bool) -> bool:
        return bool(
            self._call(
                "freeze_job", job.owner(), job.outdir().as_posix(), frozen
            )
        )

    def set_user_memory(self, user: str, limit: int | None) -> bool:
        return bool(self._call("set_user_memory", user, limit))

    def signal(self, job: Job, signum: int) -> bool:
        return bool(
            self._call(
                "signal_job", job.owner(), job.outdir().as_posix(), signum
            )
        )

    def read_file(
        self, user: str, outdir: Path, label: str, offset: int, length: int
    ) -> bytes:
        return self._call(
            "read_file", user, Path(outdir).as_posix(), label, offset, length
        )

    def spec(self) -> dict:
        return {
            "name": self._name,
            "isa": self._isa,
            "domain": self._domain,
            "cores": self._cores,
            "memory": self._memory,
            "port": self._port,
            "cert": str(self._cert) if self._cert else None,
            "key": str(self._key) if self._key else None,
            "ca": str(self._ca) if self._ca else None,
            "insecure": self._insecure,
        }

    @classmethod
    def from_spec(cls, spec: dict) -> "Host":
        return cls(
            name=spec["name"],
            isa=spec["isa"],
            domain=spec["domain"],
            cores=spec["cores"],
            memory=spec["memory"],
            port=spec["port"],
            cert=spec.get("cert"),
            key=spec.get("key"),
            ca=spec.get("ca"),
            insecure=spec.get("insecure", False),
        )

    def __str__(self) -> str:
        return (
            f"{self.__class__.__name__}(name={self._name}, isa={self._isa}, "
            f"domain={self._domain}:{self._port}, "
            f"cores={self._cores or 'auto'}, "
            f"memory={format_size(self._memory) if self._memory else 'auto'})"
        )

    def __repr__(self) -> str:
        return self.__str__()


def job_status_from_worker(status: str) -> JobStatus | None:
    return {
        "running": JobStatus.RUNNING,
        "exited": JobStatus.EXITED,
        "failed": JobStatus.FAILED,
        "killed": JobStatus.KILLED,
    }.get(status)
