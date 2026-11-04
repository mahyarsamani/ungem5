"""Run labels for the SIFT profiling sweeps.

Every run directory written by `helper` holds a `constructor.py`: the Python
call that created the job, for example

    job = gem5FSSimulation(
        getExperiment(), 4, Path(...), Path(...), "hpcg",
        num_processes=8, dim_x=64, dim_y=64, dim_z=64, seconds=2,
        dcache_size="512KiB", noc_mods="stride_prefetcher", kvm=...,
    )

`sift_labels` reads that call with `ast` (it never runs it) and returns:
- `sweep`: the parent directory without `profiling_` (`baseline` for the
  plain `profiling` sweep), which names the hardware configuration;
- `workload`: the first plain string argument (`hpcg`, `ume`, ...);
- `input`: the workload's own keyword arguments joined into one string;
- `np`: the number of processes (1 when absent).
"""

import ast
from pathlib import Path
from typing import Any, Dict

# Keyword arguments that configure the system or the run, not the input.
NOT_INPUT = {
    "kvm", "sample_stats", "sample_period", "seconds", "num_processes",
    "dcache_size", "core_mods", "noc_mods",
}


def constructor_call(run_dir: Path) -> ast.Call:
    tree = ast.parse((Path(run_dir) / "constructor.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node
    raise ValueError(f"No call in {run_dir / 'constructor.py'}.")


def constructor_arguments(run_dir: Path):
    """Positional string literals and literal keyword arguments."""
    call = constructor_call(run_dir)
    positional = [
        arg.value
        for arg in call.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    ]
    keywords = {}
    for keyword in call.keywords:
        try:
            keywords[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            keywords[keyword.arg] = True  # a flag such as OtherValues.Store_Const
    return positional, keywords


def sift_labels(run_dir: Path) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    positional, keywords = constructor_arguments(run_dir)
    sweep = run_dir.parent.name.removeprefix("profiling").lstrip("_") or "baseline"
    inputs = []
    dims = [keywords.get(f"dim_{axis}") for axis in "xyz"]
    if all(dims) and len(set(dims)) == 1:
        inputs.append(f"dim={dims[0]}")
        keywords = {k: v for k, v in keywords.items() if not k.startswith("dim_")}
    inputs += [
        f"{k.removesuffix('_name')}={v}"
        for k, v in keywords.items()
        if k not in NOT_INPUT
    ]
    return {
        "sweep": sweep,
        "workload": positional[0] if positional else "unknown",
        "input": ",".join(inputs),
        "np": keywords.get("num_processes", 1),
    }
