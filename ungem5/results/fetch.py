"""Pull finished jobs' result files from the hosts that ran them.

When a job ends, its runner indexes every file of its outdir in
`.job/index.json` (path, size, mtime and sha256, see
`ungem5.schedule.runner.write_index`). `fetch` asks a source (the running
scheduler, through `SchedulerSource`) which jobs some experiments and job
ids name, then for each job its index, and pulls the files that match the
globs and whose sha256 differs from the local copy's, so fetching an
unchanged job again moves no bytes.

A job lands in `<into>/<experiment>/<job>/`, where the two names are the
last two parts of its outdir: by convention the experiment's directory
and the job's id, and the names `results.jobs` labels a run with. So
`RunSet.from_experiment(<into>/<experiment>)` reads a fetched experiment
exactly as it reads the outdir on NFS.
Each file is written atomically and given the index's mtime. The stats
cache is keyed on path, size and mtime, so a file left as it was keeps its
cache entry and a file fetched anew gets a new one.
"""

import hashlib
import os
import time

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Protocol

from ..jobs.markers import FINISHED_MARKER, PARAMS_FILE
from ..jobs.resources import format_size
from ..schedule.runner import EXIT_FILE, JOB_DIR, LAUNCH_FILE
from ..schedule.userio import MAX_READ

# NOTE: What `results.jobs` and `stats` read from a job directory, and
# nothing else; `--match` adds a project's own files.
DEFAULT_GLOBS = (
    # NOTE: stats: a run's SimObject tree; `_run_directories` counts only
    # directories with one as runs.
    "config.json",
    # NOTE: stats: `parse.find_dump_file` and `dump_locations` read the
    # stats_dump_<N>.txt files, or stats.txt when there are none.
    "stats.txt",
    "stats_dump_*.txt",
    # NOTE: results.jobs: `params` and `job_labels`; `find_jobs` counts a
    # directory with it (or with .job/) as a job.
    PARAMS_FILE,
    # NOTE: results.jobs: `job_status` tells done from unfinished by it.
    FINISHED_MARKER,
    # NOTE: results.jobs: `job_status` reads the exit record, and the
    # launch record when there is no exit record.
    f"{JOB_DIR}/{EXIT_FILE}",
    f"{JOB_DIR}/{LAUNCH_FILE}",
    # NOTE: A run script's record of how its window ended; SIFT's `done`
    # (analysis/sift_sets.py) reads it, and the merge plan listed it.
    "window.json",
)

# NOTE: The most a worker reads of a file per call.
CHUNK = MAX_READ


class FetchError(RuntimeError):
    """Fetching one job, or planning a fetch, failed; the message is meant
    for the user."""


class Source(Protocol):
    """Where jobs and their files come from."""

    def resolve(self, targets: list[str]) -> list[dict]:
        """The jobs that experiment names and job ids name, each as
        {"id", "experiment", "outdir", "host", "status"}."""

    def job_index(self, job_id: str) -> dict | None:
        """The job's index, or None before it ends."""

    def read_file(
        self, job_id: str, path: str, offset: int, length: int
    ) -> bytes:
        """Up to `length` bytes at `offset` of a file in the job's index."""


class SchedulerSource:
    """The running scheduler, through a console's control connection
    (`schedule.console.ControlClient`).

    The scheduler resolves names as the console's commands do and lets
    only a job's owner and admins read its files, so this works for a lab
    user who holds no certificate a worker would accept. A refusal for one
    job becomes a `FetchError`, so the other jobs go on; a lost connection
    ends the fetch.
    """

    def __init__(self, client) -> None:
        self._client = client

    def resolve(self, targets: list[str]) -> list[dict]:
        return self._client.execute("resolve", targets=list(targets))

    def job_index(self, job_id: str) -> dict | None:
        return self._for_one_job(self._client.job_index, job_id)

    def read_file(
        self, job_id: str, path: str, offset: int, length: int
    ) -> bytes:
        return self._for_one_job(
            self._client.read_file, job_id, path, offset, length
        )

    @staticmethod
    def _for_one_job(method, *args):
        # NOTE: Imported here, so importing this module doesn't load the
        # console's terminal libraries.
        from ..schedule.console import ConsoleError, Disconnected

        try:
            return method(*args)
        except Disconnected:
            raise
        except ConsoleError as e:
            raise FetchError(str(e)) from None


@dataclass
class JobFetch:
    """What fetching one job did."""

    job_id: str
    directory: Path
    # NOTE: Files in the index that match the globs.
    wanted: int = 0
    fetched: int = 0
    bytes: int = 0
    # NOTE: Why nothing was fetched, for a job that is fine (not ended).
    skipped: str | None = None
    error: str | None = None

    def line(self) -> str:
        name = self.job_id[:12]
        if self.error is not None:
            return f"{name}: failed, {self.error}"
        if self.skipped is not None:
            return f"{name}: skipped, {self.skipped}"
        return (
            f"{name}: fetched {self.fetched} of {self.wanted} file(s), "
            f"{format_size(self.bytes)}"
        )


def job_directory(into: Path, outdir: str) -> Path:
    """Where a job whose outdir is `outdir` lands under `into`."""
    outdir = PurePosixPath(outdir)
    return Path(into) / outdir.parent.name / outdir.name


def wanted_files(files: Iterable[str], globs: Iterable[str]) -> list[str]:
    """The paths among `files` that match any of `globs`, as `files
    --match` matches them (`fnmatch` on the whole relative path)."""
    globs = list(globs)
    return sorted(
        path for path in files if any(fnmatch(path, glob) for glob in globs)
    )


def fetch(
    source: Source,
    targets: Iterable[str],
    into: Path | str,
    globs: Iterable[str] = DEFAULT_GLOBS,
    say: Callable[[str], None] = print,
) -> list[JobFetch]:
    """Fetch the files matching `globs` of the jobs that `targets`
    (experiment names and job ids, prefixes allowed) name into
    `into/<experiment>/<job>/`; says one line per job and a total."""
    into = Path(into)
    globs = tuple(globs)
    jobs = source.resolve(list(targets))
    landing: dict[Path, str] = {}
    placed = []
    for job in jobs:
        directory = job_directory(into, job["outdir"])
        if directory in landing:
            raise FetchError(
                f"{landing[directory]} and {job['id']} would both land in "
                f"{directory}; fetch them into different directories"
            )
        landing[directory] = job["id"]
        placed.append((directory, job))
    results = []
    for directory, job in placed:
        result = JobFetch(job["id"], directory)
        try:
            _fetch_job(source, job, globs, result)
        except (FetchError, OSError) as e:
            result.error = str(e)
        say(result.line())
        results.append(result)
    say(total_line(results))
    return results


def total_line(results: list[JobFetch]) -> str:
    fetched = sum(result.fetched for result in results)
    wanted = sum(result.wanted for result in results)
    size = sum(result.bytes for result in results)
    line = (
        f"total: fetched {fetched} of {wanted} file(s), {format_size(size)}, "
        f"from {len(results)} job(s)"
    )
    skipped = sum(1 for result in results if result.skipped is not None)
    failed = sum(1 for result in results if result.error is not None)
    if skipped:
        line += f"; {skipped} skipped"
    if failed:
        line += f"; {failed} failed"
    return line


def _fetch_job(
    source: Source, job: dict, globs: tuple, result: JobFetch
) -> None:
    if not job["host"]:
        result.skipped = f"it has not run ({job['status']})"
        return
    index = source.job_index(job["id"])
    if index is None:
        result.skipped = f"no index yet (it hasn't ended; {job['status']})"
        return
    files = index["files"]
    names = wanted_files(files, globs)
    result.wanted = len(names)
    for name in names:
        moved = fetch_file(
            source, job["id"], name, files[name],
            _destination(result.directory, name),
        )
        if moved is not None:
            result.fetched += 1
            result.bytes += moved


def _destination(directory: Path, relative: str) -> Path:
    """`relative`, a path from a job's index, under `directory`; the index
    is the job owner's file, so a path that leaves the directory is
    refused."""
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
        raise FetchError(f"{relative!r} is not a path inside a job's outdir")
    return directory.joinpath(*parts)


def _sha256(path: Path) -> str | None:
    """The sha256 of a local file, or None if there is none."""
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                hasher.update(chunk)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None
    return hasher.hexdigest()


def fetch_file(
    source: Source,
    job_id: str,
    relative: str,
    entry: list,
    destination: Path,
) -> int | None:
    """Make `destination` the file `entry` ([size, mtime, sha256] from the
    index) describes: None if it already held those bytes, else how many
    bytes were pulled. Either way it ends up with the index's mtime."""
    size, mtime, sha256 = int(entry[0]), float(entry[1]), str(entry[2])
    # NOTE: Set and compared in nanoseconds, so a float's rounding never
    # makes an unchanged file look touched.
    mtime_ns = round(mtime * 1e9)
    if _sha256(destination) == sha256:
        if destination.stat().st_mtime_ns != mtime_ns:
            os.utime(destination, ns=(time.time_ns(), mtime_ns))
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.fetch.tmp")
    hasher = hashlib.sha256()
    received = 0
    try:
        with open(temporary, "wb") as f:
            while received < size:
                chunk = source.read_file(
                    job_id, relative, received, min(CHUNK, size - received)
                )
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                received += len(chunk)
            f.flush()
            os.fsync(f.fileno())
        if received != size or hasher.hexdigest() != sha256:
            raise FetchError(
                f"{relative} on its host no longer matches the job's index "
                "(did the job run again?); fetch it once the job has ended"
            )
        os.utime(temporary, ns=(time.time_ns(), mtime_ns))
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return received
