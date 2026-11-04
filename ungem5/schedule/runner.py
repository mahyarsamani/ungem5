"""Runs one job and records how it ended.

The worker launches this module, as the job's owner:

    python -m ungem5.schedule.runner --outdir D --cwd C --job-id I -- <command>

The runner leads its own process group, writes D/.job/launch.json (its pid
and start time) and logs to D/.job/runner.log. It runs `<command>` with
bash, sends its output to D/stdout and D/stderr and, when the command ends,
atomically writes D/.job/exit.json. These two records let anyone (a
restarted worker, a restarted scheduler) tell whether the job is running,
finished, failed, or was killed, without having been its parent. Then it
indexes every file of D in D/.job/index.json (size, mtime, sha256), so
readers fetch only the files they need.

Signals are delivered to the whole process group by the worker, so the
runner only notes which signal it received (to tell "killed on request" from
"crashed") and keeps waiting for the command to end.

If the job has a watcher (D/.job/watcher, see `Job.watcher`), the runner runs
it every `interval` seconds while the command runs, and once more after it
ends. The first line the watcher prints decides what happens:

    (nothing) or "ok"    keep going
    "fail <message>"     kill the command; the job ends as failed
    "retry <message>"    kill the command; the scheduler queues it again

The verdict is recorded in exit.json, so the scheduler learns about it the
same way it learns about any other ending.

The command runs with the nice value in D/.job/process.json. Every
`memory_interval` seconds (same file) the runner measures the resident memory
of the command and everything it started: the latest sample goes to
D/.job/memory.json and every sample to D/.job/memory.jsonl. The peak, which
the kernel tracks exactly even between samples, goes into exit.json.
"""

import argparse
import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
import time

from pathlib import Path

JOB_DIR = ".job"
LAUNCH_FILE = "launch.json"
EXIT_FILE = "exit.json"
FILES_FILE = "files.json"
PROCESS_FILE = "process.json"
MEMORY_FILE = "memory.json"
MEMORY_TRACE_FILE = "memory.jsonl"
WATCHER_FILE = "watcher"
WATCHER_CONFIG_FILE = "watcher.json"
WATCHER_LOG_FILE = "watcher.log"
RUNNER_LOG_FILE = "runner.log"
INDEX_FILE = "index.json"

DEFAULT_MEMORY_INTERVAL = 120

WATCHER_ACTIONS = ("ok", "fail", "retry")
KILL_GRACE_SECS = 10

FORWARDED_SIGNALS = (
    signal.SIGTERM,
    signal.SIGINT,
    signal.SIGQUIT,
    signal.SIGHUP,
)


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class Watcher:
    """Runs a job's watcher script and interprets what it prints."""

    def __init__(
        self, job_dir: Path, outdir: Path, job_id: str, config: dict
    ) -> None:
        self._script = job_dir / WATCHER_FILE
        self._log = job_dir / WATCHER_LOG_FILE
        self._outdir = outdir
        self._job_id = job_id
        self.interval = float(config.get("interval", 60))
        self._timeout = float(config.get("timeout", 60))
        self._started = time.time()

    @classmethod
    def load(cls, job_dir: Path, outdir: Path, job_id: str) -> "Watcher | None":
        if not (job_dir / WATCHER_FILE).is_file():
            return None
        return cls(
            job_dir,
            outdir,
            job_id,
            read_json(job_dir / WATCHER_CONFIG_FILE) or {},
        )

    def log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self._log, "a") as f:
            f.write(f"{stamp} {message}\n")

    def _command(self) -> list[str]:
        with open(self._script, "rb") as f:
            shebang = f.read(2) == b"#!"
        if shebang:
            os.chmod(self._script, 0o755)
            return [str(self._script)]
        return ["bash", str(self._script)]

    def check(self, pid: int, exit_code: int | None) -> tuple[str, str]:
        """Run the watcher once; returns (action, message)."""
        env = dict(
            os.environ,
            JOB_OUTDIR=str(self._outdir),
            JOB_ID=self._job_id,
            JOB_PID=str(pid),
            JOB_ELAPSED=str(int(time.time() - self._started)),
            JOB_RUNNING="0" if exit_code is not None else "1",
        )
        if exit_code is not None:
            env["JOB_EXIT_CODE"] = str(exit_code)
        with open(self._log, "a") as log:
            try:
                # NOTE: Its own session, so a timeout kills everything it ran.
                watcher = subprocess.Popen(
                    self._command(),
                    cwd=self._outdir,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    text=True,
                    start_new_session=True,
                )
            except OSError as e:
                self.log(f"could not run the watcher: {e}")
                return "ok", ""
            try:
                output, _ = watcher.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                os.killpg(watcher.pid, signal.SIGKILL)
                watcher.wait()
                self.log(f"watcher timed out after {self._timeout:g}s; ignored")
                return "ok", ""
        line = next((l.strip() for l in output.splitlines() if l.strip()), "")
        if not line:
            return "ok", ""
        action, _, message = line.partition(" ")
        if action not in WATCHER_ACTIONS:
            self.log(f"watcher printed {line!r}, not ok/fail/retry; ignored")
            return "ok", ""
        if action != "ok":
            self.log(f"{action}: {message}")
        return action, message.strip()


class MemoryMeter:
    """Samples the resident memory of a process and its descendants."""

    def __init__(self, job_dir: Path, interval: float) -> None:
        self._latest = job_dir / MEMORY_FILE
        self._trace = job_dir / MEMORY_TRACE_FILE
        self.interval = max(1.0, float(interval))
        self.peak = 0
        self.samples = 0
        self._trace.unlink(missing_ok=True)
        self._latest.unlink(missing_ok=True)

    @staticmethod
    def tree_rss(pid: int) -> int:
        """Bytes resident in `pid` and everything it started."""
        import psutil

        try:
            procs = [psutil.Process(pid)]
            procs += procs[0].children(recursive=True)
        except psutil.NoSuchProcess:
            return 0
        total = 0
        for proc in procs:
            try:
                total += proc.memory_info().rss
            except psutil.Error:
                pass
        return total

    def sample(self, pid: int) -> None:
        rss = self.tree_rss(pid)
        self.peak = max(self.peak, rss)
        self.samples += 1
        now = time.time()
        with open(self._trace, "a") as f:
            f.write(json.dumps({"time": now, "rss": rss}) + "\n")
        write_json_atomic(
            self._latest, {"time": now, "rss": rss, "peak": self.peak}
        )

    def result(self) -> dict:
        """The peak, after the command was waited for. ru_maxrss is the
        largest peak of any waited-for descendant, in KiB on Linux."""
        children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return {
            "peak": max(self.peak, children * 1024),
            "sampled_peak": self.peak,
            "samples": self.samples,
        }


def write_index(outdir: Path) -> None:
    """Index every file of the outdir by its relative path: size, mtime
    and sha256, so a reader can fetch only what it needs, and only what
    changed."""
    files = {}
    index = outdir / JOB_DIR / INDEX_FILE
    for path in sorted(outdir.rglob("*")):
        if path == index or path.is_symlink() or not path.is_file():
            continue
        hasher = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    hasher.update(chunk)
            info = path.stat()
        except OSError:
            continue
        files[str(path.relative_to(outdir))] = [
            info.st_size, info.st_mtime, hasher.hexdigest()
        ]
    write_json_atomic(index, {"made_at": time.time(), "files": files})


def kill_tree(child: subprocess.Popen) -> None:
    """TERM the command and everything it started, then KILL what's left."""
    import psutil

    try:
        procs = [psutil.Process(child.pid)]
        procs += procs[0].children(recursive=True)
    except psutil.NoSuchProcess:
        return
    for proc in procs:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=KILL_GRACE_SECS)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ungem5.schedule.runner")
    parser.add_argument("--outdir", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--job-id", default="")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    command = " ".join(command)

    job_dir = args.outdir / JOB_DIR
    job_dir.mkdir(parents=True, exist_ok=True)
    log = open(job_dir / RUNNER_LOG_FILE, "w")
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    # NOTE: Lead a process group, so a signal reaches the whole job. A
    # worker's child or a systemd unit's main process may already lead one.
    if os.getpgid(0) != os.getpid():
        os.setsid()
    import psutil

    write_json_atomic(
        job_dir / LAUNCH_FILE,
        {
            "job_id": args.job_id,
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "create_time": psutil.Process().create_time(),
            "start_time": time.time(),
            "cwd": str(args.cwd),
            "command": command,
        },
    )
    watcher = Watcher.load(job_dir, args.outdir, args.job_id)
    process = read_json(job_dir / PROCESS_FILE) or {}
    nice = int(process.get("nice", 0))
    meter = MemoryMeter(
        job_dir, process.get("memory_interval", DEFAULT_MEMORY_INTERVAL)
    )

    received = []

    def note_signal(signum, _frame):
        # NOTE: A Python handler (unlike SIG_IGN) is reset to the default
        # on exec, so the command still reacts to the signals normally.
        received.append(signum)

    for sig in FORWARDED_SIGNALS:
        signal.signal(sig, note_signal)

    returncode = None
    error = None
    verdict = ("ok", "")
    try:
        with open(args.outdir / "stdout", "w") as out, open(
            args.outdir / "stderr", "w"
        ) as err:
            child = subprocess.Popen(
                ["bash", "-c", command],
                cwd=args.cwd,
                # NOTE: So job commands can run this package's modules with
                # the worker's own Python, e.g. ungem5.run.simulate.
                env=dict(os.environ, EXPERIMENT_PYTHON=sys.executable),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                # NOTE: The runner has no threads, so preexec_fn is safe.
                preexec_fn=(lambda: os.nice(nice)) if nice else None,
            )
            next_sample = time.monotonic() + meter.interval
            next_check = (
                time.monotonic() + watcher.interval if watcher else None
            )
            while True:
                due = min(t for t in (next_sample, next_check) if t)
                try:
                    returncode = child.wait(
                        timeout=max(0.0, due - time.monotonic())
                    )
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if now >= next_sample:
                    meter.sample(child.pid)
                    next_sample = now + meter.interval
                if next_check is not None and now >= next_check:
                    verdict = watcher.check(child.pid, None)
                    next_check = time.monotonic() + watcher.interval
                    if verdict[0] != "ok":
                        kill_tree(child)
                        returncode = child.wait()
                        break
    except OSError as e:
        error = f"could not start command: {e}"
        returncode = 127

    if watcher and verdict[0] == "ok":
        # NOTE: Lets a watcher judge a run that ended on its own, e.g. turn
        # an exit code of 0 with a fatal in stderr into a failure.
        verdict = watcher.check(-1, returncode)

    write_json_atomic(
        job_dir / EXIT_FILE,
        {
            "returncode": returncode,
            "signals_received": received,
            "end_time": time.time(),
            "error": error,
            "memory": meter.result(),
            "watcher": (
                None
                if verdict[0] == "ok"
                else {"action": verdict[0], "message": verdict[1]}
            ),
        },
    )
    # NOTE: After exit.json, so the job's end never waits on hashing.
    write_index(args.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
