"""Runs and sets of runs: where stats come from.

A `Run` is one gem5 output directory: its labels, its SimObject tree from
`config.json`, and the blocks of one dump, by default its largest
`stats_dump_<N>.txt` (or the last dump of `stats.txt`).
A `RunSet` holds many runs and builds a stat across all of them on request:
`runs["numCycles"]` is one `Scalar` with a value per run and owner.
`RunSet.from_samples` makes one run per dump of a periodically sampled run,
so a stat becomes a time series over its `sample` label.
"""

import glob
import re
import warnings
from collections import Counter, OrderedDict
from concurrent.futures import ProcessPoolExecutor
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union

from . import cache as _cache
from .config import ConfigTree
from .owners import OwnerScheme
from .parse import StatLine, dump_locations, dump_scalars, find_dump_file, read_dump
from .records import Block, DumpBlocks, build_blocks, suggest_specs
from .specs import Registry
from .stats import KINDS, Histogram, Index, Stat

Labels = Union[None, Dict[str, Any], Callable[[Path], Dict[str, Any]]]


class Run:
    """One gem5 output directory."""

    def __init__(
        self,
        path: Union[str, Path],
        labels: Optional[Dict[str, Any]] = None,
        registry: Optional[Registry] = None,
        owners: Optional[OwnerScheme] = None,
        quiet: bool = True,
        dump: Union[int, Tuple[Path, int]] = -1,
        tree: Optional[ConfigTree] = None,
        cache: bool = True,
        lines: Optional[List[StatLine]] = None,
    ) -> None:
        """`dump` picks the dump: an index into `dumps()` (the default, -1,
        is the last, as `find_dump_file` finds it) or a location (file, dump
        in file) from it. `tree` reuses a parsed `config.json` of the same
        directory; `cache` uses graphix's parsed-dump cache; `lines` are the
        dump's lines when the caller has read them already."""
        self.path = Path(path)
        self.labels = OrderedDict(labels or {})
        self.labels.setdefault("run", self.path.name)
        self.tree = tree or ConfigTree.from_file(self.path / "config.json")
        self.dump_file, self.dump_index = _location(self.path, dump)
        if lines is None:
            with warnings.catch_warnings():
                if quiet:
                    warnings.simplefilter("ignore")
                lines = read_dump(self.dump_file, self.dump_index, cache=cache)
        # The dump's simulated time, if it has one: a sample's position.
        self.sim_seconds = next(
            (line.values[0] for line in lines if line.name == "simSeconds"),
            None,
        )
        self.blocks: DumpBlocks = build_blocks(lines, self.tree, registry)
        self.owners = owners
        self._by_name = self.blocks.by_name()
        self._controllers: Dict[str, Dict[int, str]] = {}
        if owners is not None:
            owners.check(obj.path for obj in self.tree)

    def __repr__(self) -> str:
        return f"Run({self.path.name!r}, {dict(self.labels)})"

    def names(self) -> List[str]:
        return list(self._by_name)

    def dumps(self) -> List[Tuple[Path, int]]:
        """Every dump of this run's directory, in order, as (file, dump in
        file); `Run(path, dump=k)` reads the k-th."""
        return dump_locations(self.path)

    def resets(self) -> List[int]:
        """Positions in `dumps()` of dumps that follow a stats reset.

        gem5's `simTicks` counts from the last reset and `finalTick` from
        the start of the simulation, so a dump whose `simTicks` is smaller
        than the previous dump's while `finalTick` grew follows a reset.
        A run with an ROI that resets once and dumps cumulatively after it
        has none past its first dump, so its last dump covers the whole ROI.
        """
        ticks = [_dump_ticks(file, index) for file, index in self.dumps()]
        return [
            position
            for position in range(1, len(ticks))
            if None not in (ticks[position] + ticks[position - 1])
            and ticks[position][0] < ticks[position - 1][0]
            and ticks[position][1] > ticks[position - 1][1]
        ]

    def blocks_named(self, name: str) -> List[Block]:
        return self._by_name.get(name, [])

    def canonical(self, path: str) -> Tuple[str, Tuple]:
        """The owner name and extra dims for a raw path under the scheme."""
        if self.owners is None:
            return path, ()
        return self.owners.map(path)

    def controller_path(self, machine: str, version: int) -> str:
        """The SimObject path of Ruby controller `<machine>_Controller` #version.

        `machine` is the SLICC machine name (`Cache`, `Memory`, `MiscNode`),
        or a MachineID string such as `Cache_12`.
        """
        table = self._controllers.get(machine)
        if table is None:
            suffix = f"::{machine}_Controller"
            table = {
                obj.params["version"]: _stats_path(self.tree, obj.path)
                for obj in self.tree
                if obj.cxx_class.endswith(suffix) and "version" in obj.params
            }
            self._controllers[machine] = table
        try:
            return table[int(version)]
        except KeyError:
            raise KeyError(
                f"{self.path.name}: no {machine}_Controller with version {version}."
            ) from None

    def ticks_per_cycle(self, domain: str = "board.clk_domain") -> float:
        """The clock period of a clock domain in ticks, from `config.json`
        (gem5 stores a `SrcClockDomain`'s `clock` as a list of periods; the
        first is the one in use without DVFS)."""
        clock = self.tree[domain].params.get("clock")
        if isinstance(clock, list):
            clock = clock[0] if clock else None
        if not clock:
            raise KeyError(f"{self.path.name}: '{domain}' has no clock period.")
        return float(clock)

    def controller(self, machine_id: str) -> str:
        """The path of a controller named like gem5's MachineID (`Cache_12`)."""
        machine, _, version = machine_id.rpartition("_")
        return self.controller_path(machine, int(version))


def _location(path: Path, dump: Union[int, Tuple[Path, int]]) -> Tuple[Path, int]:
    """(file, dump in file) for a dump given as a location or a position."""
    if isinstance(dump, tuple):
        return dump
    if dump == -1:
        return find_dump_file(path), -1
    return dump_locations(path)[dump]


def _dump_ticks(path: Path, dump: int) -> Tuple[Optional[float], Optional[float]]:
    """(simTicks, finalTick) of one dump, without parsing the rest."""
    values = dump_scalars(path, dump, ("simTicks", "finalTick"))
    return values.get("simTicks"), values.get("finalTick")


def _read_lines(task: Tuple[Path, int, bool]) -> List[StatLine]:
    """A process-pool worker: one dump's lines (through the cache)."""
    path, dump, cache = task
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return read_dump(path, dump, cache=cache)


def _read_all(
    locations: Sequence[Tuple[Path, int]], cache: bool, workers: int
) -> List[List[StatLine]]:
    """The lines of many dumps, cache hits read here and misses parsed by
    `workers` processes (which also fill the cache)."""
    results: List[Optional[List[StatLine]]] = [None] * len(locations)
    misses = []
    for position, (path, dump) in enumerate(locations):
        cached = _cache.load(path, dump) if cache else None
        if cached is not None:
            results[position] = cached
        else:
            misses.append(position)
    tasks = [(locations[p][0], locations[p][1], cache) for p in misses]
    if workers > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(min(workers, len(tasks))) as pool:
            parsed = list(pool.map(_read_lines, tasks))
    else:
        parsed = [_read_lines(task) for task in tasks]
    for position, lines in zip(misses, parsed):
        results[position] = lines
    return results


def _run_directories(
    paths: Union[str, Path, Iterable[Union[str, Path]]]
) -> List[Path]:
    """The run directories (those with a `config.json`) among `paths`, a
    directory list or a glob such as `"data/*/*"`."""
    if isinstance(paths, (str, Path)):
        text = str(paths)
        if any(ch in text for ch in "*?["):
            candidates = [Path(p) for p in sorted(glob.glob(text))]
        else:
            candidates = [Path(text)]
    else:
        candidates = [Path(p) for p in paths]
    directories = [p for p in candidates if (p / "config.json").exists()]
    if not directories:
        raise FileNotFoundError(f"No run directories (with config.json) in {paths}.")
    return directories


def _labels_for(labels: Labels, directory: Path) -> Dict[str, Any]:
    """The labels of one run directory: from a function, a dict of path ->
    labels, or none."""
    if callable(labels):
        return labels(directory)
    if isinstance(labels, dict):
        return labels.get(directory, labels.get(str(directory), {}))
    return {}


class RunSet:
    """Many runs; index it by stat name to get a stat across all of them."""

    def __init__(self, runs: Sequence[Run]) -> None:
        self.runs = list(runs)
        self._cache: Dict[Tuple[str, bool], Stat] = {}

    @classmethod
    def from_dirs(
        cls,
        paths: Union[str, Path, Iterable[Union[str, Path]]],
        labels: Labels = None,
        specs: Sequence = (),
        owners: Optional[OwnerScheme] = None,
        registry: Optional[Registry] = None,
        dump: int = -1,
        cache: bool = True,
        workers: int = 1,
    ) -> "RunSet":
        """Load runs.

        `paths` is a directory list or a glob such as `"data/*/*"`; only
        directories with a `config.json` are runs.
        `labels` is a function from a run directory to its label dict (or a
        dict of path -> labels); every run also gets a `run` label, its
        directory name.
        `specs` are user `StatSpec`s, placed before the built-in gem5 specs.
        `dump` picks each run's dump by position in `Run.dumps()` (default
        the last); `from_samples` loads them all.
        `cache` uses the parsed-dump cache (`ungem5.stats.cache`), and
        `workers` > 1 parses the dumps the cache misses in that many
        processes.
        """
        registry = registry if registry is not None else Registry(specs)
        directories = _run_directories(paths)
        locations = [_location(directory, dump) for directory in directories]
        all_lines = _read_all(locations, cache, workers)
        runs = [
            Run(
                directory,
                _labels_for(labels, directory),
                registry,
                owners,
                dump=location,
                lines=lines,
            )
            for directory, location, lines in zip(directories, locations, all_lines)
        ]
        return cls(runs)

    @classmethod
    def from_experiment(
        cls,
        experiment_dirs: Union[str, Path, Iterable[Union[str, Path]]],
        labels: Labels = None,
        done: Optional[Callable[[Path], bool]] = None,
        statuses: Iterable[str] = ("done",),
        **from_dirs_args: Any,
    ) -> "RunSet":
        """Load the finished jobs of `helper` experiments.

        `experiment_dirs` are experiment output directories
        (`<gem5_out_base_dir>/<experiment>`), one or many.
        A job is kept when its status (`ungem5.results.jobs.job_status`)
        is in `statuses` (default: exited cleanly with `simulation_finished`)
        and the project's own `done(job_dir)` holds, when given.
        Each run is labelled with `experiment` and its `params.json`
        (`job_labels`), then with `labels` (a function or a dict, as in
        `from_dirs`), which win on a clash.
        The other arguments go to `from_dirs`.
        """
        # NOTE: Imported here, so `import ungem5.stats` does not load the
        # scheduler's modules.
        from ..results import jobs as helper

        jobs = helper.select_jobs(experiment_dirs, statuses, done)
        if not jobs:
            raise FileNotFoundError(
                f"No job of {experiment_dirs} has a status in {sorted(statuses)}"
                + (" and passes `done`." if done else ".")
            )
        all_labels = {
            str(job): {**helper.job_labels(job), **_labels_for(labels, job)}
            for job in jobs
        }
        return cls.from_dirs(jobs, labels=all_labels, **from_dirs_args)

    @classmethod
    def from_samples(
        cls,
        paths: Union[str, Path, Iterable[Union[str, Path]]],
        labels: Labels = None,
        specs: Sequence = (),
        owners: Optional[OwnerScheme] = None,
        registry: Optional[Registry] = None,
        cache: bool = True,
        workers: int = 1,
    ) -> "RunSet":
        """Load every dump of each run, as one run per dump.

        A run that dumps its stats periodically (for example every
        millisecond of a region of interest, without a reset) leaves one
        dump per sample (`Run.dumps()`). Each becomes a run with the
        directory's labels plus `sample` (0, 1, ... in order) and
        `sim_seconds` (the dump's `simSeconds`, None if it has none), so a
        stat indexed by them is a time series:
        `runs["numCycles"].to_frame()` has a row per sample.
        `paths`, `labels`, `specs`, `owners`, `cache` and `workers` are as
        in `from_dirs`.
        """
        registry = registry if registry is not None else Registry(specs)
        runs = []
        for directory in _run_directories(paths):
            base_labels = _labels_for(labels, directory)
            tree = ConfigTree.from_file(directory / "config.json")
            locations = dump_locations(directory)
            all_lines = _read_all(locations, cache, workers)
            for sample, (location, lines) in enumerate(zip(locations, all_lines)):
                run = Run(
                    directory,
                    dict(base_labels, sample=sample),
                    registry,
                    owners,
                    dump=location,
                    tree=tree,
                    lines=lines,
                )
                run.labels["sim_seconds"] = run.sim_seconds
                runs.append(run)
        return cls(runs)

    def __len__(self) -> int:
        return len(self.runs)

    def __iter__(self) -> Iterator[Run]:
        return iter(self.runs)

    def __repr__(self) -> str:
        return f"RunSet({len(self.runs)} runs)"

    def select(self, **match: Any) -> "RunSet":
        """Keep the runs whose labels match (value, list of values, or predicate)."""
        from .stats.base import _match_value

        kept = [
            run
            for run in self.runs
            if all(_match_value(run.labels.get(k, object()), v) for k, v in match.items())
        ]
        return RunSet(kept)

    def labels(self):
        """A DataFrame with one row of labels per run."""
        import pandas as pd

        return pd.DataFrame([dict(run.labels) for run in self.runs])

    def skipped(self) -> Counter:
        """Lines skipped by specs (Formulas and friends), summed over runs."""
        total = Counter()
        for run in self.runs:
            total.update(run.blocks.skipped)
        return total

    def catalog(self, pattern: Optional[str] = None):
        """A DataFrame of stat names: kind, owner class, unit, block count.

        `pattern` is a glob on the name (`"*Latency*"`).
        A name with several rows is printed by different kinds of objects;
        see `get` for how to pick one.
        """
        import pandas as pd

        rows = OrderedDict()
        for run in self.runs:
            for block in run.blocks.blocks:
                if pattern and not fnmatchcase(block.name, pattern):
                    continue
                key = (block.name, block.owner_class)
                row = rows.get(key)
                if row is None:
                    row = rows[key] = {
                        "name": block.name,
                        "kind": block.kind,
                        "owner_class": block.owner_class,
                        "unit": block.unit,
                        "dims": ", ".join(k for k, _ in block.dims),
                        "blocks": 0,
                        "description": block.description,
                    }
                row["blocks"] += 1
        columns = ["name", "kind", "owner_class", "unit", "dims", "blocks", "description"]
        frame = pd.DataFrame(list(rows.values()), columns=columns)
        return frame.sort_values(["name", "owner_class"]).reset_index(drop=True)

    def suggest_specs(self, min_variants: int = 3) -> List[str]:
        """Key families that look like dimensions, merged over all runs."""
        merged = DumpBlocks(
            [block for run in self.runs for block in run.blocks.blocks], Counter()
        )
        return suggest_specs(merged, min_variants)

    def __getitem__(self, name: str) -> Stat:
        return self.get(name)

    def get(
        self,
        name: str,
        owner_class: Union[None, str, Sequence[str]] = None,
        fill: bool = True,
    ) -> Stat:
        """Build stat `name` across all runs.

        One name can mean different stats on different kinds of objects
        (`lookups` on a branch predictor, its BTB, and its indirect
        predictor).
        When the blocks of a name differ in kind or spec, `get` raises and
        lists each family's owner classes; pass `owner_class` (a glob or a
        list of globs on the C++ class) to pick one.

        With `fill=True`, a value gem5 did not print (a `nozero` stat that
        was zero) is filled with zero for every owner of the same C++ class
        in the same run, for each dim combination that run printed.
        """
        classes = (owner_class,) if isinstance(owner_class, str) else owner_class
        key = (name, tuple(classes) if classes else None, fill)
        if key not in self._cache:
            self._cache[key] = self._build(name, classes, fill)
        return self._cache[key]

    def _build(self, name: str, classes: Optional[Sequence[str]], fill: bool) -> Stat:
        found = [
            (run, block)
            for run in self.runs
            for block in run.blocks_named(name)
            if not classes
            or any(fnmatchcase(block.owner_class or "", c) for c in classes)
        ]
        if not found:
            close = sorted({n for run in self.runs for n in run.names() if name.lower() in n.lower()})
            hint = f" Similar: {close[:8]}." if close else ""
            where = f" with owner_class {list(classes)}" if classes else ""
            raise KeyError(f"No stat named '{name}'{where}.{hint}")
        _check_one_family(name, [block for _, block in found])
        cls = _stat_class(name, [block for _, block in found])
        spec = found[0][1].spec
        unit = (spec.unit if spec is not None else None) or next(
            (b.unit for _, b in found if b.unit), None
        )
        description = next((b.description for _, b in found if b.description), None)

        raw: Dict[Run, Dict[Tuple[str, tuple, int], Any]] = OrderedDict()
        for run, block in found:
            per_run = raw.setdefault(run, OrderedDict())
            if hasattr(cls, "values_from_block"):
                values = cls.values_from_block(block, run)
            else:
                values = [(block.owner, cls.value_from_block(block))]
            for path, value in values:
                per_run[(path, block.dims, block.occurrence)] = value

        if fill:
            classes = {
                run.tree[path].cxx_class
                for run, entries in raw.items()
                for (path, _, _) in entries
                if path in run.tree
            }
            # A run where gem5 printed none of the stat (all zero) still gets
            # zeros when the stat has no dims; with dims, only the dim values
            # that run printed are known. Histograms are never filled into
            # such a run: a variant that never samples one (a baseline run of
            # a stat only the new design adds) has no histogram, not an
            # empty one.
            no_dims = all(dims == () for entries in raw.values() for (_, dims, _) in entries)
            if no_dims and not issubclass(cls, Histogram):
                for run in self.runs:
                    raw.setdefault(run, OrderedDict())
            for run, entries in raw.items():
                owners = [obj.path for obj in run.tree if obj.cxx_class in classes]
                stats_paths = [_stats_path(run.tree, p) for p in owners]
                combos = list(OrderedDict.fromkeys(dims for (_, dims, occ) in entries if occ == 0))
                if no_dims:
                    combos = [()]
                for path in stats_paths:
                    for dims in combos:
                        entries.setdefault((path, dims, 0), _zero(cls))

        data = OrderedDict()
        for run, entries in raw.items():
            labels = tuple(run.labels.items())
            for (path, dims, occurrence), value in entries.items():
                owner, owner_dims = run.canonical(path)
                converted = spec.convert_dims(dims, run) if spec is not None else dims
                index = Index(labels, owner, tuple(converted) + tuple(owner_dims), occurrence, path)
                if index in data:
                    raise ValueError(
                        f"'{name}': two values map to {index}; check the owner "
                        f"scheme and the spec's dims."
                    )
                data[index] = value
        return cls(name, data, unit, description, spec)


class AmbiguousStatError(TypeError):
    """One name covers several different stats; pick one by owner class."""


def _check_one_family(name: str, blocks: List[Block]) -> None:
    """Raise when the blocks of one name belong to different stat families.

    Blocks form one family when they share a spec and a kind; a size-1
    vector prints like a scalar, so scalar and vector blocks of the same
    owner class count as one kind.
    """
    kinds_by_class: Dict[Optional[str], set] = OrderedDict()
    spec_by_class: Dict[Optional[str], Any] = {}
    for block in blocks:
        kinds_by_class.setdefault(block.owner_class, set()).add(block.kind)
        spec_by_class[block.owner_class] = block.spec
    families: Dict[tuple, List[str]] = OrderedDict()
    for owner_class, kinds in kinds_by_class.items():
        kind = "vector" if kinds == {"scalar", "vector"} else "/".join(sorted(kinds))
        spec = spec_by_class[owner_class]
        families.setdefault((id(spec), kind), []).append(owner_class or "?")
    if len(families) > 1:
        lines = []
        for (spec_id, kind), owner_classes in families.items():
            spec = next(spec_by_class[c] for c in kinds_by_class if (c or "?") in owner_classes)
            source = f"spec {spec!r}" if spec is not None else "no spec"
            lines.append(f"  {kind} ({source}): {', '.join(owner_classes)}")
        raise AmbiguousStatError(
            f"'{name}' names {len(families)} different stats:\n"
            + "\n".join(lines)
            + f"\nPick one with get('{name}', owner_class=...)."
        )


def _stat_class(name: str, blocks: List[Block]):
    spec = blocks[0].spec
    if spec is not None and isinstance(spec.kind, type):
        return spec.kind
    kinds = {block.kind for block in blocks}
    if kinds == {"scalar", "vector"}:
        kinds = {"vector"}  # a size-1 vector prints like a scalar
    if len(kinds) != 1:
        raise TypeError(f"'{name}' has blocks of different kinds: {sorted(kinds)}.")
    return KINDS[kinds.pop()]


def _zero(cls):
    zero = cls.zero
    return dict(zero) if isinstance(zero, dict) else zero


def _stats_path(tree: ConfigTree, path: str) -> str:
    """The stats spelling (unpadded vector names) of a config path."""
    obj = tree[path]
    parts = []
    while obj is not None and obj.parent is not None:
        parent = obj.parent
        alias = re.sub(r"(\d+)$", lambda m: str(int(m.group(1))), obj.name)
        parts.append(alias if parent.children.get(alias) is obj else obj.name)
        obj = parent
    return ".".join(reversed(parts))
