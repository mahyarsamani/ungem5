"""The learned memory estimate."""

import random
import time

import pytest

from experiment.api.memory import MemoryHistory
from experiment.api.model import COVERAGE, MIN_RUNS, PeakModel, Run, flatten
from experiment.api.work import Job

M = 1 << 20


def test_flatten_keeps_paths_numbers_and_text():
    assert flatten({"a": {"b": 2}, "c": ["x", 3], "d": None, "e": True}) == {
        "a.b": 2.0,
        "c.#len": 2.0,
        "c.0": "x",
        "c.1": 3.0,
        "d": "None",
        "e": "True",
    }


def peak_of(size, kind, rng):
    """Peaks grow with the input size, by kind, with some noise."""
    return int(size * {"cg": 3, "mg": 5, "spmv": 2}[kind] * M * rng.uniform(1, 1.3))


def runs(count, rng):
    result = []
    for _ in range(count):
        size, kind = rng.choice([16, 32, 64, 128, 256]), rng.choice(["cg", "mg", "spmv"])
        features = {"script": "run.py", "args": [kind, str(size)], "size": size}
        result.append(Run(features, "run.py on gem5", None, peak_of(size, kind, rng)))
    return result


def test_the_model_reserves_enough_for_most_new_jobs():
    pytest.importorskip("sklearn")
    rng = random.Random(1)
    assert PeakModel.fit(runs(MIN_RUNS - 1, rng)) is None
    model = PeakModel.fit(runs(300, rng))
    fresh = runs(300, rng)
    covered = sum(
        model.predict(run.features, run.group, None) >= run.peak for run in fresh
    )
    assert covered / len(fresh) >= COVERAGE - 0.07
    # NOTE: Not by reserving everything: a small job gets far less than a
    # big one.
    small = model.predict({"script": "run.py", "args": ["spmv", "16"], "size": 16}, "run.py on gem5", None)
    big = model.predict({"script": "run.py", "args": ["mg", "256"], "size": 256}, "run.py on gem5", None)
    assert small * 10 < big


def test_the_history_switches_to_the_model_once_it_can(tmp_path):
    pytest.importorskip("sklearn")
    rng = random.Random(2)
    history = MemoryHistory(tmp_path / "memory.sqlite", default_memory=1 << 30)

    class Sized(Job):
        def __init__(self, i, size, kind):
            super().__init__(
                "e", tmp_path, f"run {kind} {size} {i}", "r",
                tmp_path / str(i), None, f"j{i}",
            )
            self._size, self._kind = size, kind

        def features(self):
            return {"size": self._size, "kind": self._kind}

    for i in range(60):
        size, kind = rng.choice([16, 64, 256]), rng.choice(["cg", "mg"])
        j = Sized(i, size, kind)
        j.assign("h")
        j.set_times(time.time(), time.time())
        j.set_memory(None, peak_of(size, kind, rng))
        history.record(j, "h", "exited", [], 0, 0)
    assert history.model() is not None
    # NOTE: A size no run had: the model answers.
    estimate = history.estimate(Sized(999, 128, "mg"))
    assert estimate.source == "model" and "learned from 60 run(s)" in estimate.detail
    assert 64 * 5 * M <= estimate.memory <= 256 * 5 * M * 2
    # NOTE: A job like one that ran gets that run's history.
    assert history.estimate(Sized(998, 64, "mg")).source == "history"
    history.close()
    reopened = MemoryHistory(tmp_path / "memory.sqlite", default_memory=1 << 30)
    assert reopened.model() is not None
