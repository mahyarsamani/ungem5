"""What a worker does with a job's files, done as the job's owner.

A worker run by a user does these in its own process (`LocalIO`). A worker
run by root (the lab setup) never touches users' files itself: for each
user it keeps a helper process, `python -m ungem5.schedule.userio`, running
as that user (`HelperIO`), so ownership, permissions and NFS root squash
work as they would for the user. The helper reads one JSON request per
line on stdin and answers with one JSON line on stdout.
"""

import base64
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from pathlib import Path

import psutil

from ..jobs import bundle
from .runner import (
    EXIT_FILE,
    FILES_FILE,
    INDEX_FILE,
    JOB_DIR,
    LAUNCH_FILE,
    MEMORY_FILE,
    MEMORY_TRACE_FILE,
    read_json,
    write_json_atomic,
)

MAX_READ = 1 << 20
MAX_TRACE = 8 << 20


def _job_dir(outdir: Path) -> Path:
    return outdir / JOB_DIR


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def leader_alive(launch: dict) -> bool:
    """Is the process recorded in launch.json still the same live process?"""
    try:
        proc = psutil.Process(launch["pid"])
        # NOTE: Guards against the pid having been reused by another process.
        if abs(proc.create_time() - launch["create_time"]) > 1.0:
            return False
        return proc.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD)
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


Memory = tuple[int | None, int | None] | None
Status = tuple[
    str, int | None, str | None, float | None, float | None, Memory
]


def _sampled_memory(job_dir: Path) -> Memory:
    latest = read_json(job_dir / MEMORY_FILE)
    if latest is None:
        return None
    return latest.get("rss"), latest.get("peak")


def resolve_status(outdir: Path) -> Status:
    """Returns (status, returncode, message, start_time, end_time, memory)
    for the job in `outdir`. Times are epoch seconds on this machine's
    clock; memory is (bytes resident now, peak bytes so far), with None
    for "now" once the job ended, or None if nothing was measured.

    status is one of "missing", "running", "exited", "failed", "killed",
    and "retry" (the job's watcher asked for it to be queued again).
    """
    job_dir = _job_dir(outdir)
    launch = read_json(job_dir / LAUNCH_FILE)
    if launch is None:
        return "missing", None, None, None, None, None
    start = launch.get("start_time")

    def from_exit(record: dict) -> Status:
        rc = record.get("returncode")
        end = record.get("end_time")
        peak = (record.get("memory") or {}).get("peak")
        memory = (None, peak) if peak is not None else None
        verdict = record.get("watcher")
        if verdict:
            status = "retry" if verdict["action"] == "retry" else "failed"
            message = f"watcher: {verdict['message']}"
            return status, rc, message, start, end, memory
        if rc == 0:
            return "exited", rc, None, start, end, memory
        if rc is not None and rc < 0 and -rc in record.get(
            "signals_received", []
        ):
            message = f"stopped by signal {-rc}"
            return "killed", rc, message, start, end, memory
        if rc is not None and rc < 0:
            message = f"terminated by signal {-rc}"
            return "failed", rc, message, start, end, memory
        return "failed", rc, record.get("error"), start, end, memory

    record = read_json(job_dir / EXIT_FILE)
    if record is not None:
        return from_exit(record)
    if leader_alive(launch):
        return "running", None, None, start, None, _sampled_memory(job_dir)
    # NOTE: The runner may have written exit.json right after our first look.
    record = read_json(job_dir / EXIT_FILE)
    if record is not None:
        return from_exit(record)
    sampled = _sampled_memory(job_dir)
    return (
        "killed",
        None,
        "ended without an exit record (e.g. SIGKILL)",
        start,
        None,
        (None, sampled[1]) if sampled else None,
    )


# ---- The operations --------------------------------------------------------


def prepare(outdir: str, cwd: str, files: list, dumps: list) -> None:
    """Get `outdir` ready for a launch: refuse if a job runs there, clear
    the last run's records, write the dumps, and note which files the
    dashboard may read."""
    out = Path(outdir)
    if not out.is_absolute() or not Path(cwd).is_absolute():
        raise ValueError("cwd and outdir must be absolute paths")
    job_dir = _job_dir(out)
    job_dir.mkdir(parents=True, exist_ok=True)
    if resolve_status(out)[0] == "running":
        raise RuntimeError(f"a job is already running in {out}")
    for name in (
        LAUNCH_FILE, EXIT_FILE, MEMORY_FILE, MEMORY_TRACE_FILE, INDEX_FILE
    ):
        (job_dir / name).unlink(missing_ok=True)

    allowed = {}
    for label, path in files:
        p = Path(path)
        if not _inside(p, out):
            raise ValueError(f"file {label}={p} is outside {out}")
        allowed[str(label)] = str(p)
    for content, path in dumps:
        p = Path(path)
        if not _inside(p, out):
            raise ValueError(f"dump {p} is outside {out}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content))
    write_json_atomic(job_dir / FILES_FILE, allowed)


def launch_record(outdir: str) -> dict | None:
    """The job's launch.json, which the runner writes when it starts."""
    return read_json(_job_dir(Path(outdir)) / LAUNCH_FILE)


def statuses(outdirs: list) -> list:
    return [resolve_status(Path(outdir)) for outdir in outdirs]


def finished_runs(outdirs: list) -> list:
    """For each outdir, (job id, status) of the run that ended there, or
    None if none has. An exit record says the same on any host, so this
    may ask a host other than the one the job ran on."""
    results = []
    for outdir in outdirs:
        job_dir = _job_dir(Path(outdir))
        launch = read_json(job_dir / LAUNCH_FILE)
        if launch is None or read_json(job_dir / EXIT_FILE) is None:
            results.append(None)
            continue
        results.append((launch.get("job_id"), resolve_status(Path(outdir))))
    return results


def read_file(outdir: str, label: str, offset: int, length: int) -> bytes:
    """A labelled file of the job (files.json), or any file of its
    outdir's index, by its path relative to the outdir."""
    out = Path(outdir)
    allowed = read_json(_job_dir(out) / FILES_FILE) or {}
    if label in allowed:
        path = Path(allowed[label])
    elif label in ((read_json(_job_dir(out) / INDEX_FILE) or {}).get("files") or {}):
        path = out / label
    else:
        raise PermissionError(f"{label} is not a file of this job")
    if not _inside(path, out):
        raise PermissionError(f"{path} is outside {out}")
    try:
        with open(path, "rb") as f:
            f.seek(max(0, int(offset)))
            return f.read(max(0, min(int(length), MAX_READ)))
    except FileNotFoundError:
        raise FileNotFoundError(f"{label} does not exist (yet)")


def memory_trace(outdir: str) -> bytes:
    """The job's memory samples, one JSON object per line (the last
    MAX_TRACE bytes of them)."""
    path = _job_dir(Path(outdir)) / MEMORY_TRACE_FILE
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - MAX_TRACE))
            data = f.read()
    except FileNotFoundError:
        return b""
    if size > MAX_TRACE:
        # NOTE: Drop the partial first line.
        data = data.partition(b"\n")[2]
    return data


def job_index(outdir: str) -> dict | None:
    """The index the runner wrote when the job ended, or None."""
    return read_json(_job_dir(Path(outdir)) / INDEX_FILE)


# ---- The store: bundles and working directories ------------------------------
#
# A store is one user's, on one host: <base>/<hostname>/<user>, so workers
# sharing a filesystem never write each other's. blobs/<id>.tar.gz are the
# bundles as they arrived; work/<key> are working directories, one per
# bundle and set of data paths, which jobs run in and never change.


def has_bundle(store: str, bundle_id: str) -> bool:
    return (Path(store) / "blobs" / f"{_checked(bundle_id)}.tar.gz").is_file()


def put_bundle(store: str, bundle_id: str, data: bytes) -> None:
    """Keep a packed bundle, once it unpacks to what its id says."""
    blobs = Path(store) / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=blobs) as scratch:
        bundle.unpack(data, _checked(bundle_id), Path(scratch) / "check")
        tmp = Path(scratch) / "blob"
        tmp.write_bytes(data)
        os.replace(tmp, blobs / f"{bundle_id}.tar.gz")


def _checked(bundle_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_id):
        raise ValueError(f"{bundle_id!r} is not a bundle id")
    return bundle_id


def _link(work: Path, root: Path, relative: str, bundled: set[str]) -> None:
    """Make `relative` in `work` show `root`'s: a symlink, or, where
    bundled files sit inside it, a directory of links to its other
    entries."""
    target, source = work / relative, root / relative
    inside = any(Path(name).is_relative_to(relative) for name in bundled)
    if not inside:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_symlink() and not target.exists():
            target.symlink_to(source)
        return
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        name = str(Path(relative) / child.name)
        if name not in bundled:
            _link(work, root, name, bundled)


def workdir(store: str, bundle_id: str, root: str, data: list) -> str:
    """The working directory for a bundle with these data paths (under
    `root`, on a filesystem this host sees), made once."""
    blob = Path(store) / "blobs" / f"{_checked(bundle_id)}.tar.gz"
    if not blob.is_file():
        raise FileNotFoundError(f"bundle {bundle_id[:12]} is not on this host")
    key = hashlib.sha256(
        json.dumps([bundle_id, root, sorted(data)]).encode()
    ).hexdigest()
    work = Path(store) / "work" / key
    if work.is_dir():
        return str(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{key[:12]}-", dir=work.parent))
    try:
        names = bundle.unpack(blob.read_bytes(), bundle_id, tmp)
        for relative in sorted(data):
            _link(tmp, Path(root), relative, set(names))
        try:
            os.rename(tmp, work)
        except OSError:
            # NOTE: Another launch made it first; theirs is the same.
            if not work.is_dir():
                raise
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return str(work)


OPERATIONS = {
    function.__name__: function
    for function in (
        prepare,
        launch_record,
        statuses,
        finished_runs,
        read_file,
        memory_trace,
        job_index,
        has_bundle,
        put_bundle,
        workdir,
    )
}

# NOTE: Errors that cross from the helper as themselves; others arrive as
# RuntimeError.
ERRORS = {
    error.__name__: error
    for error in (
        ValueError,
        RuntimeError,
        PermissionError,
        FileNotFoundError,
    )
}


def _encode(value):
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode()}
    return value


def _decode(value):
    if isinstance(value, dict) and set(value) == {"bytes"}:
        return base64.b64decode(value["bytes"])
    return value


# ---- Doing them ------------------------------------------------------------


class LocalIO:
    """The operations in this process, as whoever runs it."""

    def call(self, name: str, *args):
        return OPERATIONS[name](*args)

    def close(self) -> None:
        pass


class HelperIO:
    """The operations in a helper process running as `user`. Only root
    can start one for another user."""

    def __init__(self, user: str) -> None:
        self._account = pwd.getpwnam(user)
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def _start(self) -> subprocess.Popen:
        account = self._account
        return subprocess.Popen(
            [sys.executable, "-B", "-m", "ungem5.schedule.userio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            user=account.pw_uid,
            group=account.pw_gid,
            extra_groups=os.getgrouplist(account.pw_name, account.pw_gid),
            env={
                "HOME": account.pw_dir,
                "USER": account.pw_name,
                "LOGNAME": account.pw_name,
                "PATH": "/usr/local/bin:/usr/bin:/bin",
            },
            cwd="/",
            close_fds=True,
        )

    def call(self, name: str, *args):
        request = json.dumps({"op": name, "args": [_encode(a) for a in args]})
        request = (request + "\n").encode()
        with self._lock:
            # NOTE: One retry, with a new helper if the old one died.
            for attempt in range(2):
                if self._process is None or self._process.poll() is not None:
                    self._process = self._start()
                try:
                    self._process.stdin.write(request)
                    self._process.stdin.flush()
                    line = self._process.stdout.readline()
                except (BrokenPipeError, OSError):
                    line = b""
                if line:
                    break
                self._process.kill()
                self._process = None
            else:
                raise RuntimeError(
                    f"the helper for {self._account.pw_name} keeps dying"
                )
        reply = json.loads(line)
        if "error" in reply:
            raise ERRORS.get(reply["type"], RuntimeError)(reply["error"])
        return _decode(reply["result"])

    def close(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.stdin.close()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None


def serve(stdin=sys.stdin.buffer, stdout=sys.stdout.buffer) -> None:
    """The helper's loop: one request per line until stdin closes."""
    for line in stdin:
        try:
            request = json.loads(line)
            args = [_decode(arg) for arg in request["args"]]
            result = _encode(OPERATIONS[request["op"]](*args))
            reply = {"result": result}
        except Exception as e:
            reply = {"error": str(e), "type": type(e).__name__}
        stdout.write((json.dumps(reply) + "\n").encode())
        stdout.flush()


if __name__ == "__main__":
    serve()
