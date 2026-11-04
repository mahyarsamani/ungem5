"""Load scripts, compiled to plain data.

A load script is Python that defines Hosts and Experiments. The console
runs it, as the user, in a fresh interpreter (`python -m
ungem5.jobs.script`), and sends the scheduler what it defines as data:
every job's command, files, dumps, features and dependencies, the hosts it
may run on, and the packed bundles of the Python jobs run (`bundle.py`),
which freezes that code at load. The scheduler never runs a script's code, and a
fresh interpreter picks up edits to modules the script imports.
"""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import traceback

from pathlib import Path
from typing import Any, Iterable

from ..schedule.host import Host
from . import bundle
from .work import Experiment, Job, PerHostJob


class ScriptError(Exception):
    """The script could not be compiled; the message is for the user."""


def load_objects(script: Path) -> tuple[list[Host], list[Experiment]]:
    """Run `script` and collect the Hosts and Experiments it defines.

    Looks at the script's top-level variables, and inside dicts, lists,
    tuples and sets they hold.
    """
    mod_name = f"_plugin_{hashlib.sha1(str(script).encode()).hexdigest()}"
    spec = importlib.util.spec_from_file_location(mod_name, str(script))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {script}")
    module = importlib.util.module_from_spec(spec)
    # NOTE: Let the script import modules that sit next to it.
    sys.path.insert(0, str(script.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(script.parent))

    def walk(obj: Any, seen: set[int]) -> Iterable[Any]:
        if id(obj) in seen:
            return
        seen.add(id(obj))
        yield obj
        if isinstance(obj, dict):
            for value in obj.values():
                yield from walk(value, seen)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for value in obj:
                yield from walk(value, seen)

    hosts, experiments, seen = [], [], set()
    for value in vars(module).values():
        for obj in walk(value, seen):
            if isinstance(obj, Host):
                hosts.append(obj)
            elif isinstance(obj, Experiment):
                experiments.append(obj)
    return hosts, experiments


def host_from_view(view: dict) -> Host:
    """A host as the scheduler's snapshot shows it, for jobs to check
    compatibility against; it is never connected."""
    return Host(
        view["name"], view["isa"], view["domain"], view["cores"],
        view["memory"], insecure=True,
    )


def _compatible(
    item: "Job | PerHostJob", hosts: Iterable[Host], warnings: list[str]
) -> list[str]:
    """Names of the hosts `item` accepts; a raising check means no."""
    names = []
    for host in hosts:
        try:
            if item.compatible_with_host(host):
                names.append(host.name())
        except Exception as e:
            warnings.append(
                f"compatible_with_host of {item.id()[:12]} raised "
                f"{type(e).__name__}: {e} for host {host.name()}; not "
                "running it there"
            )
    return names


def compile_script(script: Path, known_hosts: Iterable[Host]) -> dict:
    """Run `script` and compile what it defines.

    `known_hosts` are the hosts the scheduler has; with the script's own
    hosts, they decide where each job may run and which per-host jobs
    exist. A host added later gets them when the script is reloaded.
    """
    script = Path(script).expanduser().resolve()
    if not script.is_file() or script.suffix != ".py":
        raise ScriptError(f"{script} is not a Python file")
    hosts, experiments = load_objects(script)
    return compile_objects(
        str(script),
        hashlib.sha256(script.read_bytes()).hexdigest(),
        hosts,
        experiments,
        known_hosts,
    )


def compile_objects(
    origin: str,
    sha256: str,
    hosts: list[Host],
    experiments: list[Experiment],
    known_hosts: Iterable[Host],
) -> dict:
    """What `compile_script` makes of the objects a script defined."""
    registry = {host.name(): host for host in known_hosts}
    for host in hosts:
        registry.setdefault(host.name(), host)
    warnings: list[str] = []

    templates = {
        template.id(): template
        for experiment in experiments
        for template in experiment.per_host_jobs()
    }
    for experiment in experiments:
        for job in experiment.jobs():
            for parent in job.parents():
                if isinstance(parent, PerHostJob):
                    templates.setdefault(parent.id(), parent)

    # NOTE: Each bundle's packed files, once however many jobs share it.
    blobs: dict[str, str] = {}

    def with_bundle(job: Job) -> Job:
        found = job.bundle()
        if found is not None and found.id not in blobs:
            blobs[found.id] = base64.b64encode(bundle.pack(found)).decode()
            warnings.extend(found.warnings)
        return job

    compiled_templates = []
    for template in templates.values():
        instances = []
        for name in _compatible(template, registry.values(), warnings):
            job = with_bundle(template.instance(name))
            instances.append(job.compile([name]))
        compiled_templates.append(
            {
                "id": template.id(),
                "experiment": template.experiment(),
                "outdir": str(template.outdir()),
                "instances": instances,
            }
        )

    return {
        "script": origin,
        "sha256": sha256,
        "hosts": [host.spec() for host in hosts],
        "experiments": [
            {
                "name": experiment.name(),
                "outdir": str(experiment.outdir()),
                "jobs": [
                    with_bundle(job).compile(
                        _compatible(job, registry.values(), warnings)
                    )
                    for job in experiment.jobs()
                ],
            }
            for experiment in experiments
        ],
        "templates": compiled_templates,
        "blobs": blobs,
        "warnings": warnings,
    }


def compile_in_subprocess(
    script: Path, known_hosts: list[dict], timeout: float = 600
) -> dict:
    """`compile_script` in a fresh interpreter, so the script runs with
    this process's environment and working directory but none of its
    imported modules.
    `known_hosts` are host views from the scheduler's snapshot. The
    script's own output goes to this process's stdout and stderr."""
    with tempfile.TemporaryDirectory(prefix="experiment-load-") as tmp:
        hosts_path = Path(tmp) / "hosts.json"
        out_path = Path(tmp) / "compiled.json"
        hosts_path.write_text(json.dumps(known_hosts))
        try:
            # NOTE: -B: no .pyc of the user's modules, which could go
            # stale on an edit within the same second.
            result = subprocess.run(
                [
                    sys.executable, "-B", "-m", "ungem5.jobs.script",
                    str(script), str(hosts_path), str(out_path),
                ],
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ScriptError(f"{script} did not finish within {timeout}s")
        if result.returncode != 0 or not out_path.exists():
            raise ScriptError(
                f"{script} failed (exit code {result.returncode}); its "
                "traceback is above"
            )
        return json.loads(out_path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ungem5.jobs.script")
    parser.add_argument("script", type=Path)
    parser.add_argument("hosts", type=Path, help="JSON list of host views.")
    parser.add_argument("out", type=Path)
    args = parser.parse_args(argv)
    try:
        hosts = [
            host_from_view(view)
            for view in json.loads(args.hosts.read_text())
        ]
        compiled = compile_script(args.script, hosts)
        tmp = args.out.with_name(args.out.name + ".tmp")
        tmp.write_text(json.dumps(compiled))
        os.replace(tmp, args.out)
    except ScriptError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
