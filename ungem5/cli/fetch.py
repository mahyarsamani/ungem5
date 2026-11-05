import argparse

from pathlib import Path

from ..common.log import error
from ..jobs import settings
from ..results.fetch import DEFAULT_GLOBS, FetchError, SchedulerSource, fetch
from ..schedule.console import ConsoleError, ControlClient


def parse_fetch_args(args):
    parser = argparse.ArgumentParser(
        prog="helper fetch",
        description="Pull finished jobs' result files from the hosts that "
        "ran them, through the running scheduler, into "
        "DIR/<experiment>/<job>/. Only files whose sha256 differs from the "
        "local copy move, and each keeps its mtime. By default the files "
        f"ungem5's results and stats read: {', '.join(DEFAULT_GLOBS)}.",
    )
    parser.add_argument(
        "targets",
        nargs="+",
        metavar="experiment|job",
        help="An experiment's name (all its jobs) or a job id prefix.",
    )
    parser.add_argument(
        "--into",
        required=True,
        type=Path,
        metavar="DIR",
        help="Where the jobs land, as DIR/<experiment>/<job>/.",
    )
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        metavar="GLOB",
        help="Also fetch the paths matching this glob, like 'window.json' "
        "or '*.log'; repeat for several.",
    )
    parser.add_argument(
        "--lab",
        action="store_true",
        help="Fetch through the lab scheduler ([lab] socket in "
        f"{settings.SYSTEM_FILE} or {settings.USER_FILE}) instead of your own.",
    )
    return parser.parse_known_args(args)


def _process_fetch_args(known_args, unknown_args) -> int:
    if unknown_args:
        error(f"unrecognized arguments: {' '.join(unknown_args)}")
        return 2
    if known_args.lab:
        try:
            socket = Path(settings.load()["lab"]["socket"])
        except settings.SettingsError as e:
            error(str(e))
            return 2
        client = ControlClient(socket)
        if not client.connect():
            error(f"Can't reach the lab scheduler at {socket}.")
            return 1
    else:
        client = ControlClient()
        if not client.connect():
            error("No scheduler is running. Start one with `helper schedule`.")
            return 1
    try:
        results = fetch(
            SchedulerSource(client),
            known_args.targets,
            known_args.into,
            DEFAULT_GLOBS + tuple(known_args.match),
        )
    except (ConsoleError, FetchError) as e:
        error(str(e))
        return 1
    finally:
        client.close()
    return 1 if any(result.error is not None for result in results) else 0
