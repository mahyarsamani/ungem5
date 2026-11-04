import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

import gxtest  # noqa: E402,F401  (path and a temporary cache)

import graphix as gx  # noqa: E402
from graphix import cache  # noqa: E402
from graphix.parse import read_dump  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHAPES = ROOT / "tests" / "fixtures" / "shapes"

try:
    import experiment.api.runner  # noqa: F401

    HAVE_EXPERIMENT = True
except ImportError:
    HAVE_EXPERIMENT = False

DUMP = "---------- Begin Simulation Statistics ----------\n{}---------- End Simulation Statistics   ----------\n"


def write_dumps(path: Path, ticks):
    """A stats.txt with one dump per (simTicks, finalTick) pair."""
    path.write_text(
        "".join(
            DUMP.format(f"simTicks {s} # ticks (Tick)\nfinalTick {f} # ticks (Tick)\n")
            for s, f in ticks
        )
    )


class TestCache(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.stats = self.directory / "stats.txt"
        shutil.copy(SHAPES / "stats.txt", self.stats)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_hit_equals_parse(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            first = read_dump(self.stats)
            self.assertIsNotNone(cache.load(self.stats, -1))
            second = read_dump(self.stats)
            uncached = read_dump(self.stats, cache=False)
        # repr, because a `nan` value never equals itself.
        self.assertEqual(repr(first), repr(second))
        self.assertEqual(repr(first), repr(uncached))

    def test_changed_file_misses(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            read_dump(self.stats)
            with open(self.stats, "a") as stats:
                stats.write(DUMP.format("simTicks 7 # ticks (Tick)\n"))
            later = os.stat(self.stats).st_mtime_ns + 1_000_000
            os.utime(self.stats, ns=(later, later))
            self.assertIsNone(cache.load(self.stats, -1))
            self.assertEqual(read_dump(self.stats)[0].values, (7.0,))

    def test_off(self):
        previous = os.environ["GRAPHIX_CACHE"]
        os.environ["GRAPHIX_CACHE"] = "off"
        try:
            self.assertIsNone(cache.cache_dir())
            cache.store(self.stats, -1, [])
            self.assertIsNone(cache.load(self.stats, -1))
        finally:
            os.environ["GRAPHIX_CACHE"] = previous


class TestDumps(unittest.TestCase):
    def test_resets_and_dump_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            run.mkdir()
            shutil.copy(SHAPES / "config.json", run / "config.json")
            write_dumps(run / "stats.txt", [(100, 100), (200, 200), (50, 250)])
            runs = gx.RunSet.from_dirs([run])
            self.assertEqual(len(runs.runs[0].dumps()), 3)
            self.assertEqual(runs.runs[0].resets(), [2])
            first = gx.RunSet.from_dirs([run], dump=0)
            self.assertEqual([v for _, v in first["simTicks"]], [100.0])

    def test_workers_match_one_process(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = []
            for name in ("a", "b", "c"):
                shutil.copytree(SHAPES, Path(directory) / name)
                runs.append(Path(directory) / name)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                one = gx.RunSet.from_dirs(runs, cache=False)
                many = gx.RunSet.from_dirs(runs, workers=3)
                again = gx.RunSet.from_dirs(runs, workers=3)  # from the cache
        expected = dict(one["numCycles"].items())
        self.assertEqual(dict(many["numCycles"].items()), expected)
        self.assertEqual(dict(again["numCycles"].items()), expected)


@unittest.skipUnless(HAVE_EXPERIMENT, "tools/experiment is not installed")
class TestHelperSource(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.experiment = self.directory / "se-test"
        self.experiment.mkdir()

    def tearDown(self):
        shutil.rmtree(self.directory)

    def job(self, name, exit_record, finished=True, params=None, launched=True):
        job = self.experiment / name
        (job / ".job").mkdir(parents=True)
        shutil.copy(SHAPES / "config.json", job / "config.json")
        shutil.copy(SHAPES / "stats.txt", job / "stats.txt")
        if launched:
            (job / ".job" / "launch.json").write_text("{}")
        if exit_record is not None:
            (job / ".job" / "exit.json").write_text(json.dumps(exit_record))
        if finished:
            (job / "simulation_finished").touch()
        (job / "params.json").write_text(
            json.dumps(params or {"workload_name": name, "noc_mods": ["a", "b"], "nested": {"x": 1}})
        )
        return job

    def test_statuses(self):
        from graphix.sources import helper

        ok = {"returncode": 0, "signals_received": [], "error": None}
        self.job("done", ok)
        self.job("unfinished", ok, finished=False)
        self.job("failed", dict(ok, returncode=1))
        self.job("killed", dict(ok, signals_received=[9]))
        self.job("running", None, finished=False)
        self.job("missing", None, finished=False, launched=False)
        statuses = {p.name: s for p, s in helper.find_jobs(self.experiment)}
        self.assertEqual(
            statuses,
            {"done": "done", "unfinished": "unfinished", "failed": "failed",
             "killed": "failed", "running": "running", "missing": "missing"},
        )

    def test_from_experiment_labels_and_done(self):
        ok = {"returncode": 0, "signals_received": [], "error": None}
        self.job("a", ok)
        self.job("b", ok)
        self.job("c", dict(ok, returncode=2))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            runs = gx.RunSet.from_experiment(
                self.experiment,
                labels=lambda job: {"tag": job.name.upper()},
                done=lambda job: job.name != "b",
            )
        self.assertEqual(len(runs), 1)
        labels = dict(runs.runs[0].labels)
        self.assertEqual(labels["experiment"], "se-test")
        self.assertEqual(labels["workload_name"], "a")
        self.assertEqual(labels["noc_mods"], ("a", "b"))
        self.assertEqual(labels["nested.x"], 1)
        self.assertEqual(labels["tag"], "A")
        with self.assertRaises(FileNotFoundError):
            gx.RunSet.from_experiment(self.experiment, done=lambda job: False)


if __name__ == "__main__":
    unittest.main()
