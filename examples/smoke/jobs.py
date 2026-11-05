"""Smoke test for a scheduler and one worker on the same machine.

Load this in the console of a scheduler whose worker runs on this machine:

    helper work --name <host>            # in another terminal
    helper schedule --name <host>
    <host>> load examples/smoke/jobs.py

Five plain shell jobs cover a clean exit, a failure, a memory guess that
the scheduler can check against what the job used, a dependency, and a
long job to kill. Outputs go under $UNGEM5_SMOKE_OUT, by default
~/.cache/ungem5/smoke/<host>. The worker listens on $UNGEM5_SMOKE_PORT
(9100) and is reached at $UNGEM5_SMOKE_DOMAIN (this machine's name, which
its certificate always carries).
"""

import os
import platform

from pathlib import Path

from ungem5.jobs.resources import Resources
from ungem5.jobs.work import Experiment, Job
from ungem5.schedule.host import Host

NAME = platform.node()
OUT = Path(
    os.environ.get("UNGEM5_SMOKE_OUT")
    or Path.home() / ".cache" / "ungem5" / "smoke" / NAME
)
PORT = int(os.environ.get("UNGEM5_SMOKE_PORT", "9100"))
DOMAIN = os.environ.get("UNGEM5_SMOKE_DOMAIN", NAME)

hosts = [
    Host(NAME, platform.machine(), DOMAIN, cores=2, memory="4G", port=PORT)
]
smoke = Experiment("smoke", OUT)


def job(name, command, resources=None, after=()):
    # NOTE: A job runs in `cwd`, not in its outdir, so name files by
    # absolute path, as gem5 jobs do.
    outdir = OUT / name
    command = f"OUTDIR={outdir}; {command}"
    item = Job(
        "smoke",
        OUT,
        command,
        name,
        outdir,
        resources,
        name,
        aux_files=(("result", outdir / "result.txt"),),
        after=after,
    )
    smoke.register_job(item)
    return item


ok = job("ok", "sleep 2; echo hello > $OUTDIR/result.txt")
fails = job("fails", "sleep 1; exit 3")
mem = job(
    "mem",
    "sleep 2; python3 -c 'x = bytearray(200 << 20); print(len(x))' "
    "> $OUTDIR/result.txt",
    Resources(cores=1, memory="1G"),
)
after_ok = job(
    "after-ok", "cat $OUTDIR/../ok/result.txt > $OUTDIR/result.txt", after=[ok]
)
long = job("long", "sleep 600")
