"""Runs written by `helper` jobs (the scheduler's runner and gem5 jobs).

A gem5 job writes to `<experiment dir>/<job id>/`; by convention the
experiment directory is `<gem5_out_base_dir>/<experiment name>`, but each
script chooses it, so this module takes experiment directories as paths.
A job directory holds:
- `.job/launch.json` and `.job/exit.json`, written by the runner
  (`ungem5.schedule.runner`, whose file names this module imports);
- `params.json` (the run function's arguments by name) and the markers
  `run_function_called` and `simulation_finished`, written by the project's
  run script through the `record_args` decorator (names in
  `ungem5.jobs.markers`);
- whatever the project's own code writes (a measurement window file, for
  example), which a caller judges with its own `done` predicate.

A job is `done` when its `exit.json` reports return code 0, no signal and no
error, and the run script left `simulation_finished`.
"""

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from ..jobs.markers import FINISHED_MARKER, PARAMS_FILE
from ..schedule.runner import EXIT_FILE, JOB_DIR, LAUNCH_FILE, read_json

# The statuses `job_status` returns.
DONE = "done"            # exit 0, no signal or error, simulation_finished
UNFINISHED = "unfinished"  # exit 0 but no simulation_finished
FAILED = "failed"        # a non-zero exit, a signal, or a runner error
RUNNING = "running"      # launched, no exit record yet (or still running)
MISSING = "missing"      # never launched by the runner (no launch record)


def job_status(job_dir: Union[str, Path]) -> str:
    """One of `done`, `unfinished`, `failed`, `running`, `missing`."""
    job_dir = Path(job_dir)
    record = job_dir / JOB_DIR
    exit_record = read_json(record / EXIT_FILE)
    if exit_record is None:
        launched = (record / LAUNCH_FILE).exists()
        return RUNNING if launched else MISSING
    if (
        exit_record.get("returncode") != 0
        or exit_record.get("signals_received")
        or exit_record.get("error")
    ):
        return FAILED
    return DONE if (job_dir / FINISHED_MARKER).exists() else UNFINISHED


def find_jobs(experiment_dir: Union[str, Path]) -> List[Tuple[Path, str]]:
    """Every job directory of an experiment, sorted, with its status."""
    experiment_dir = Path(experiment_dir)
    if not experiment_dir.is_dir():
        raise FileNotFoundError(f"No experiment directory {experiment_dir}.")
    jobs = []
    for job_dir in sorted(p for p in experiment_dir.iterdir() if p.is_dir()):
        if (job_dir / JOB_DIR).is_dir() or (job_dir / PARAMS_FILE).exists():
            jobs.append((job_dir, job_status(job_dir)))
    return jobs


def params(job_dir: Union[str, Path]) -> Dict[str, Any]:
    """The run function's arguments (`params.json`), or {} without one."""
    data = read_json(Path(job_dir) / PARAMS_FILE)
    return data if isinstance(data, dict) else {}


def job_labels(job_dir: Union[str, Path]) -> Dict[str, Any]:
    """Labels every helper job has: `experiment` (the directory's name) and
    its `params.json`, flattened (`a.b` for nested keys) and made hashable
    (lists become tuples)."""
    job_dir = Path(job_dir)
    labels: Dict[str, Any] = {"experiment": job_dir.parent.name}
    labels.update(_flatten(params(job_dir)))
    return labels


def select_jobs(
    experiment_dirs: Union[str, Path, Iterable[Union[str, Path]]],
    statuses: Iterable[str] = (DONE,),
    done: Optional[Callable[[Path], bool]] = None,
) -> List[Path]:
    """The job directories, over all experiments, whose status is in
    `statuses` and for which `done` (a project's own completion rule, such
    as a window file) holds."""
    if isinstance(experiment_dirs, (str, Path)):
        experiment_dirs = [experiment_dirs]
    statuses = set(statuses)
    kept = []
    for experiment_dir in experiment_dirs:
        for job_dir, status in find_jobs(experiment_dir):
            if status in statuses and (done is None or done(job_dir)):
                kept.append(job_dir)
    return kept


def _flatten(data: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    flat = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = _hashable(value)
    return flat


def _hashable(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value
