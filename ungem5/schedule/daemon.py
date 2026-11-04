"""Runs the scheduler as a background process that consoles attach to.

One scheduler runs per user per machine (see `state.InstanceLock`). Consoles
talk to it over a Unix socket in the user's private runtime directory; the
socket also checks that the peer runs as the same user.

A lab scheduler (`--lab`) is a system service that serves every member of
one group: consoles connect to a socket anyone can open, the kernel tells
the scheduler which user is on the other end, and that user is who every
command comes from.
"""

import argparse
import json
import logging
import os
import platform
import pwd
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

import rpyc

from rpyc.utils.authenticators import AuthenticationError
from rpyc.utils.server import ThreadedServer

from rpyc.utils.helpers import classpartial

from ..common.log import error, info, install_warning_format
from ..jobs import settings
from .dashboard import Dashboard
from .scheduler import Caller, CommandError, Scheduler
from .state import InstanceLock, StateStore, socket_path, state_dir
from .worker import PROTOCOL_CONFIG

DEFAULT_DASHBOARD_PORT = 9200
DEFAULT_POLLING_SECS = 1.0


class PeerUidAuthenticator:
    """Accept Unix-socket peers running as this user or, given
    `allow_gids`, as any user but root in one of those groups."""

    def __init__(self, allow_gids: Iterable[int] = ()) -> None:
        self._allow_gids = frozenset(allow_gids)

    def __call__(self, sock):
        creds = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _, uid, _ = struct.unpack("3i", creds)
        try:
            account = pwd.getpwuid(uid)
        except KeyError:
            raise AuthenticationError(f"uid {uid} has no account") from None
        if not self._allow_gids:
            allowed = uid == os.getuid()
        else:
            allowed = uid != 0 and not self._allow_gids.isdisjoint(
                os.getgrouplist(account.pw_name, account.pw_gid)
            )
        if not allowed:
            raise AuthenticationError(
                f"{account.pw_name} may not use this scheduler"
            )
        return sock, {"uid": uid, "user": account.pw_name}


class ControlService(rpyc.Service):
    """What a console can do, as the user on the other end of its
    connection. Everything crosses as JSON strings."""

    def __init__(self, scheduler: Scheduler, dashboard: Dashboard) -> None:
        super().__init__()
        self._scheduler = scheduler
        self._dashboard = dashboard
        self._caller: Caller | None = None

    def on_connect(self, conn) -> None:
        self._caller = self._scheduler.caller(
            conn._config["credentials"]["user"]
        )

    def exposed_snapshot(self) -> str:
        data = self._scheduler.snapshot().to_dict()
        data["you"] = {"user": self._caller.user, "admin": self._caller.admin}
        data["info"] = dict(
            data["info"], dashboard_url=self._dashboard.url(self._caller.user)
        )
        return json.dumps(data)

    def exposed_events(self, since: int) -> str:
        return json.dumps(self._scheduler.events().since(int(since)))

    def exposed_last_event(self) -> int:
        return self._scheduler.events().last_seq()

    def exposed_execute(self, name: str, kwargs_json: str) -> str:
        try:
            lines = self._scheduler.call(
                str(name),
                timeout=300,
                caller=self._caller,
                **json.loads(kwargs_json),
            )
        except CommandError as e:
            # NOTE: A builtin exception type crosses rpyc cleanly.
            raise ValueError(str(e)) from None
        return json.dumps(lines)


def _setup_logging(directory: Path, name: str, to_stderr: bool) -> logging.Logger:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
    )

    def configure(logger_name: str, file_name: str) -> logging.Logger:
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers.clear()
        handler = RotatingFileHandler(
            directory / file_name,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        if to_stderr:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(formatter)
            logger.addHandler(stream)
        return logger

    configure("werkzeug", "dashboard.log")
    configure(f"{name}.control", "control.log")
    return configure(f"{name}.scheduler", "scheduler.log")


def run_daemon(
    name: str,
    dashboard_port: int = DEFAULT_DASHBOARD_PORT,
    polling_secs: float = DEFAULT_POLLING_SECS,
    fresh: bool = False,
    log_to_stderr: bool = False,
    lab_gids: Iterable[int] = (),
    admins: Iterable[str] = (),
    socket_file: Path | None = None,
) -> int:
    """Run the scheduler until it is stopped. With `lab_gids`, it is a lab
    scheduler for the members of those groups, `admins` among them; its
    socket (`socket_file`) is open to everyone, who are checked when they
    connect."""
    lab_gids = frozenset(lab_gids)
    lab = bool(lab_gids)
    if lab and socket_file is None:
        error("A lab scheduler needs a socket.")
        return 2
    lock = InstanceLock()
    if not lock.acquire(
        {
            "pid": os.getpid(),
            "name": name,
            "dashboard_port": dashboard_port,
            "started_at": time.time(),
        }
    ):
        holder = lock.holder() or {}
        error(
            "A scheduler is already running for this user on this machine "
            f"(pid {holder.get('pid')}, name {holder.get('name')}, dashboard "
            f"port {holder.get('dashboard_port')}). Attach with "
            "`helper console`."
        )
        return 1

    sock_path = socket_file or socket_path()
    control = None
    dashboard = None
    serving = False
    try:
        store = StateStore(name)
        logger = _setup_logging(store.directory(), name, log_to_stderr)
        scheduler = Scheduler(
            name, polling_secs, store, logger, lab=lab, admins=admins
        )

        dashboard = Dashboard(
            scheduler, dashboard_port, f"Scheduler {name}@{platform.node()}"
        )
        scheduler.set_info(
            pid=os.getpid(),
            host=platform.node(),
            dashboard_port=dashboard_port,
            polling_secs=polling_secs,
            state_file=str(store.path()),
            log_dir=str(store.directory()),
            cwd=os.getcwd(),
        )

        if fresh:
            archived = store.archive()
            if archived is not None:
                scheduler.events().info(f"Archived old state to {archived}.")
        else:
            data = store.load()
            if data is not None:
                scheduler.resume(data)

        # NOTE: We hold the lock, so any socket file left here is stale.
        sock_path.unlink(missing_ok=True)
        control = ThreadedServer(
            classpartial(ControlService, scheduler, dashboard),
            socket_path=str(sock_path),
            authenticator=PeerUidAuthenticator(lab_gids),
            protocol_config=PROTOCOL_CONFIG,
            logger=logging.getLogger(f"{name}.control"),
        )
        # NOTE: In a lab anyone may connect; the authenticator decides.
        os.chmod(sock_path, 0o666 if lab else 0o600)

        threads = [
            threading.Thread(
                target=scheduler.run, name=f"{name}.scheduler", daemon=True
            ),
            threading.Thread(
                target=dashboard.serve_forever,
                name=f"{name}.dashboard",
                daemon=True,
            ),
            threading.Thread(
                target=control.start, name=f"{name}.control", daemon=True
            ),
        ]
        for thread in threads:
            thread.start()
        serving = True

        def request_stop(signum, _frame):
            scheduler.events().info(f"Received signal {signum}; stopping.")
            scheduler.stop()

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, request_stop)

        scheduler.events().info(
            f"Scheduler {name} running (pid {os.getpid()})"
            + (
                f" for group(s) {', '.join(map(str, sorted(lab_gids)))}."
                if lab
                else "."
            )
        )
        while not scheduler.wait_stopped(0.5):
            pass
        return 0
    finally:
        # NOTE: shutdown() waits for serve_forever(), so only call it if the
        # dashboard thread was started.
        if dashboard is not None and serving:
            dashboard.shutdown()
        if control is not None:
            control.close()
        sock_path.unlink(missing_ok=True)
        lock.release()


def daemon_command(
    name: str,
    dashboard_port: int,
    polling_secs: float,
    fresh: bool = False,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "ungem5.schedule.daemon",
        "--name",
        name,
        "--dashboard-port",
        str(dashboard_port),
        "--polling-secs",
        str(polling_secs),
    ] + (["--fresh"] if fresh else [])


def spawn_daemon(
    name: str,
    dashboard_port: int = DEFAULT_DASHBOARD_PORT,
    polling_secs: float = DEFAULT_POLLING_SECS,
    fresh: bool = False,
    timeout: float = 30,
) -> bool:
    """Start the daemon in the background; wait until it accepts consoles."""
    out_path = state_dir(name) / "daemon.out"
    with open(out_path, "ab") as out:
        process = subprocess.Popen(
            daemon_command(name, dashboard_port, polling_secs, fresh),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.time() + timeout
    path = socket_path()
    while time.time() < deadline:
        if process.poll() is not None:
            error(f"The scheduler exited during startup; see {out_path}:")
            lines = out_path.read_text(errors="replace").splitlines()
            for line in lines[-15:]:
                print(f"  {line}", file=sys.stderr)
            return False
        if path.exists():
            try:
                rpyc.utils.factory.unix_connect(
                    str(path), config=PROTOCOL_CONFIG
                ).close()
                info(f"Started scheduler {name} (pid {process.pid}).")
                return True
            except (OSError, EOFError):
                pass
        time.sleep(0.2)
    error(f"The scheduler didn't come up within {timeout}s; see {out_path}.")
    return False


def main(argv: list[str] | None = None) -> int:
    install_warning_format()
    parser = argparse.ArgumentParser(
        prog="ungem5.schedule.daemon",
        description="Run a scheduler. With --lab, a lab scheduler whose "
        f"defaults come from [scheduler] in {settings.SYSTEM_FILE}.",
    )
    parser.add_argument("--name")
    parser.add_argument("--dashboard-port", type=int)
    parser.add_argument(
        "--polling-secs", type=float, default=DEFAULT_POLLING_SECS
    )
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument(
        "--lab",
        action="store_true",
        help="Serve every member of the allowed groups (a lab scheduler).",
    )
    parser.add_argument(
        "--allow-gid",
        type=int,
        action="append",
        help="Lab: a group whose members may use the scheduler; repeat for "
        "several.",
    )
    parser.add_argument(
        "--admin",
        action="append",
        help="Lab: a user who may change hosts, partitions and anyone's "
        "jobs; repeat for several.",
    )
    parser.add_argument(
        "--socket", type=Path, help="Lab: the control socket."
    )
    args = parser.parse_args(argv)
    if not args.lab:
        return run_daemon(
            args.name or platform.node(),
            args.dashboard_port or DEFAULT_DASHBOARD_PORT,
            args.polling_secs,
            args.fresh,
            log_to_stderr=args.foreground,
        )
    try:
        config = settings.load()["scheduler"]
        admins = settings.user_names(args.admin or config["admins"])
    except settings.SettingsError as e:
        error(str(e))
        return 2
    gids = args.allow_gid or config["allow_gids"]
    if not gids:
        error("A lab scheduler needs [scheduler] allow_gids or --allow-gid.")
        return 2
    return run_daemon(
        args.name or config["name"],
        args.dashboard_port or config["dashboard_port"],
        args.polling_secs,
        args.fresh,
        log_to_stderr=args.foreground,
        lab_gids=gids,
        admins=admins,
        socket_file=args.socket or Path(config["socket"]),
    )


if __name__ == "__main__":
    sys.exit(main())
