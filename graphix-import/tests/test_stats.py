import math
import re
import sys
import unittest
import warnings
from pathlib import Path

import gxtest  # noqa: E402,F401  (path and a temporary cache)

ROOT = Path(__file__).resolve().parents[1]

import graphix as gx  # noqa: E402
from graphix.records import Block  # noqa: E402
from graphix.stats import Buckets, Index, merge_buckets  # noqa: E402
from graphix.stats.align import AlignmentError  # noqa: E402
from graphix.stats.histogram import buckets_from_block  # noqa: E402

SHAPES = ROOT / "tests" / "fixtures" / "shapes"
EXAMPLE_DATA = ROOT / "example_data"


def idx(run, owner, **dims):
    return Index((("run", run),), owner, tuple(dims.items()), 0, owner)


def scalar(name, values, unit="Count"):
    return gx.Scalar(name, {idx(*k): v for k, v in values.items()}, unit)


class TestScalar(unittest.TestCase):
    def setUp(self):
        self.a = scalar("a", {("r0", "c0"): 6.0, ("r0", "c1"): 3.0, ("r1", "c0"): 0.0})
        self.b = scalar("b", {("r0", "c0"): 2.0, ("r0", "c1"): 0.0, ("r1", "c0"): 0.0})

    def test_every_operator_including_reflected(self):
        values = lambda s: [v for _, v in s]
        self.assertEqual(values(self.a + self.b), [8.0, 3.0, 0.0])
        self.assertEqual(values(self.a - self.b), [4.0, 3.0, 0.0])
        self.assertEqual(values(self.a * self.b), [12.0, 0.0, 0.0])
        self.assertEqual(values(self.a // 4), [1, 0, 0])
        self.assertEqual(values(self.a % 4), [2.0, 3.0, 0.0])
        self.assertEqual(values(self.a ** 2), [36.0, 9.0, 0.0])
        self.assertEqual(values(10 - self.a), [4.0, 7.0, 10.0])
        self.assertEqual(values(2 * self.a), [12.0, 6.0, 0.0])
        self.assertEqual(values(12 / self.b)[0], 6.0)
        self.assertEqual(values(1 + self.a), [7.0, 4.0, 1.0])
        self.assertEqual(values(-self.a), [-6.0, -3.0, -0.0])

    def test_division_by_zero_is_ieee(self):
        result = [v for _, v in self.a / self.b]
        self.assertEqual(result[0], 3.0)
        self.assertEqual(result[1], math.inf)
        self.assertTrue(math.isnan(result[2]))
        self.assertTrue(math.isnan([v for _, v in self.a % 0][0]))

    def test_units(self):
        self.assertEqual((self.a / self.b).unit, "Ratio")
        cycles = scalar("cycles", {("r0", "c0"): 1.0, ("r0", "c1"): 1.0, ("r1", "c0"): 1.0}, "Cycle")
        self.assertEqual((self.a / cycles).unit, "Count/Cycle")
        with self.assertRaises(ValueError):
            self.a + cycles

    def test_strict_inner_and_by_run_alignment(self):
        partial = scalar("p", {("r0", "c0"): 1.0})
        with self.assertRaises(AlignmentError):
            self.a + partial
        self.assertEqual(len(self.a.add(partial, how="inner")), 1)
        per_run = scalar("ticks", {("r0", ""): 2.0, ("r1", ""): 4.0}, "Tick")
        rate = self.a / per_run
        self.assertEqual([v for _, v in rate], [3.0, 1.5, 0.0])
        two_per_run = scalar("x", {("r0", "a"): 1.0, ("r0", "b"): 1.0})
        with self.assertRaises(AlignmentError):
            self.a / two_per_run

    def test_select(self):
        self.assertEqual(len(self.a.select(owner="c*")), 3)
        self.assertEqual(len(self.a.select(owner="c1")), 1)
        self.assertEqual(len(self.a.select(run="r1")), 1)
        self.assertEqual(len(self.a.select(run=["r0", "r1"], owner=re.compile(r"c[01]"))), 3)
        self.assertEqual(len(self.a.select(run=lambda r: r.endswith("0"))), 2)
        self.assertEqual(len(self.a.drop(owner="c1")), 2)

    def test_reduce_over_owner_and_label(self):
        total = self.a.select(owner="c*").reduce("sum")
        self.assertEqual(
            [(i.owner, v) for i, v in total], [("sum(c*)", 9.0), ("sum(c*)", 0.0)]
        )
        mean = self.a.reduce("mean", over=["owner", "run"], label="all")
        self.assertEqual([v for _, v in mean], [3.0])
        weighted = self.a.reduce("weighted_mean", weights=self.b + 1)
        self.assertAlmostEqual([v for _, v in weighted][0], (6 * 3 + 3 * 1) / 4)
        with self.assertRaises(TypeError):
            self.a.reduce("merge")
        with self.assertRaises(KeyError):
            self.a.reduce("sum", over="thread")

    def test_sum_of_ratio_warns(self):
        ratio = self.a.with_unit("Ratio")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ratio.reduce("sum")
        self.assertEqual(len(caught), 1)

    def test_relative_to(self):
        ipc = gx.Scalar(
            "ipc",
            {
                Index((("w", "bfs"), ("cfg", "base")), "core"): 2.0,
                Index((("w", "bfs"), ("cfg", "new")), "core"): 3.0,
            },
        )
        speedup = ipc.relative_to(cfg="base")
        self.assertEqual([v for _, v in speedup], [1.0, 1.5])
        with self.assertRaises(KeyError):
            ipc.relative_to(cfg="missing")

    def test_frames(self):
        long = self.a.to_frame()
        self.assertEqual(list(long.columns), ["run", "owner", "stat", "value", "unit"])
        wide = self.a.to_frame(wide=True)
        self.assertEqual(wide.loc["r0", "c1"], 3.0)
        both = gx.table(self.a, self.b)
        self.assertEqual(list(both.columns), ["a", "b"])


class TestVector(unittest.TestCase):
    def setUp(self):
        self.v = gx.Vector(
            "v",
            {idx("r0", "c0"): {"x": 1.0, "y": 3.0}, idx("r0", "c1"): {"x": 2.0}},
            "Count",
        )

    def test_missing_elements_are_zero(self):
        self.assertEqual([v for _, v in self.v.element("y")], [3.0, 0.0])
        self.assertEqual([v for _, v in self.v.total()], [4.0, 2.0])
        summed = self.v + self.v
        self.assertEqual(summed[idx("r0", "c1")], {"x": 4.0})

    def test_share_reduce_matrix(self):
        self.assertEqual(self.v.share()[idx("r0", "c0")], {"x": 0.25, "y": 0.75})
        total = self.v.reduce("sum")
        self.assertEqual(list(total.values()), [{"x": 3.0, "y": 3.0}])
        matrix = self.v.matrix(rows=["owner"])
        self.assertEqual(matrix.loc["c1", "y"], 0.0)

    def test_broadcast_scalar(self):
        per_run = scalar("t", {("r0", ""): 2.0})
        halved = self.v / per_run
        self.assertEqual(halved[idx("r0", "c0")], {"x": 0.5, "y": 1.5})

    def test_element_dim_name(self):
        spec = gx.StatSpec("v", element_dim="op_class")
        v = gx.Vector("v", dict(self.v.items()), spec=spec)
        self.assertIn("op_class", v.to_frame().columns)


class TestHistogram(unittest.TestCase):
    def test_labelled_and_oneline_buckets(self):
        block = Block("h", "o", None, (), 0, None)
        block.fields = {
            "samples": (11,), "mean": (1.2,), "underflows": (1,), "0": (3,),
            "1": (4,), "2-3": (2,), "overflows": (1,), "total": (11,),
        }
        buckets = buckets_from_block(block)
        self.assertEqual(buckets.buckets, ((0, 1, 3), (1, 2, 4), (2, 4, 2)))
        self.assertEqual((buckets.underflow, buckets.overflow), (1, 1))
        # gem5's own mean, kept through the sum of values; the midpoint
        # estimate leaves the under- and overflows out.
        self.assertTrue(buckets.mean_exact)
        self.assertAlmostEqual(buckets.value_sum, 13.2)
        self.assertAlmostEqual(buckets.mean(), 1.2)
        self.assertAlmostEqual(buckets.mean(exact=False), 1.0)
        oneline = Block("h", "o", None, (), 0, None, oneline=True)
        oneline.fields = {None: (5, 0, 1), "bucket_size": (512,), "max_bucket": (1100,)}
        self.assertEqual(
            buckets_from_block(oneline).buckets,
            ((0, 512, 5), (512, 1024, 0), (1024, 1101, 1)),
        )

    def test_exact_merge_of_nested_grids(self):
        fine = Buckets(((0, 2, 1), (2, 4, 2), (4, 6, 3), (6, 8, 4)))
        coarse = Buckets(((0, 4, 10), (4, 8, 20)))
        merged = merge_buckets([fine, coarse])
        self.assertTrue(merged.exact)
        self.assertEqual(merged.buckets, ((0, 4, 13), (4, 8, 27)))
        self.assertEqual(merged.samples, fine.samples + coarse.samples)

    def test_inexact_merge_is_flagged(self):
        a = Buckets(((0, 3, 3),))
        b = Buckets(((0, 2, 2), (2, 4, 2)))
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            merged = merge_buckets([a, b])
        self.assertFalse(merged.exact)
        self.assertAlmostEqual(merged.samples, 7)

    def test_exact_mean_survives_merge_and_rebin(self):
        # Two wide-bucket histograms whose sums gem5 printed: the merged
        # mean is their pooled mean, not a midpoint estimate.
        a = Buckets(((0, 1000, 10),), value_sum=100.0)      # mean 10
        b = Buckets(((0, 1000, 30),), value_sum=900.0)      # mean 30
        merged = merge_buckets([a, b])
        self.assertTrue(merged.mean_exact)
        self.assertAlmostEqual(merged.mean(), 25.0)
        self.assertAlmostEqual(merged.mean(exact=False), 499.5)
        self.assertAlmostEqual(a.rebin([0, 500, 1000]).mean(), 10.0)
        # One part without a sum makes the pooled mean an estimate again.
        unknown = merge_buckets([a, Buckets(((0, 1000, 30),))])
        self.assertFalse(unknown.mean_exact)
        self.assertAlmostEqual(unknown.mean(), 499.5)

    def test_derived_summaries(self):
        b = Buckets(((0, 1, 1), (1, 2, 1), (2, 3, 2)))
        self.assertFalse(b.mean_exact)
        self.assertAlmostEqual(b.mean(), 1.25)
        self.assertEqual(b.quantile(0.5), 2.0)
        self.assertEqual(b.cdf(), [0.25, 0.5, 1.0])
        hist = gx.Histogram("h", {idx("r0", "c0"): b, idx("r0", "c1"): b})
        merged = hist.merge()
        self.assertEqual(list(merged.values())[0].samples, 8)
        frame = merged.to_frame()
        self.assertEqual(list(frame["count"]), [2, 2, 4])
        with self.assertRaises(TypeError):
            hist + hist
        with self.assertRaises(TypeError):
            hist.reduce("mean")


class TestOwnerScheme(unittest.TestCase):
    def setUp(self):
        self.scheme = gx.OwnerScheme(
            {
                "core{c}.l1d": [
                    "b.tiles{t}.clusters{c}.dcache",
                    "b.tiles{t}.clusters{c}.l1dcache",
                ],
                "core{c}": ["b.tiles{t}.clusters{c}"],
            }
        )

    def test_equivalent_paths_and_dims(self):
        self.assertEqual(self.scheme.map("b.tiles0.clusters1.dcache"), ("core1.l1d", (("t", "0"),)))
        self.assertEqual(self.scheme.map("b.tiles0.clusters1.l1dcache.cache"), ("core1.l1d.cache", (("t", "0"),)))
        self.assertEqual(self.scheme.map("b.tiles2.clusters1.router"), ("core1.router", (("t", "2"),)))
        self.assertEqual(self.scheme.map("b.other"), ("b.other", ()))

    def test_collision_and_unmapped(self):
        with self.assertRaises(ValueError):
            gx.OwnerScheme({"x": ["a.one", "a.two"]}).check(["a.one", "a.two"])
        strict = gx.OwnerScheme({"x": ["a"]}, unmapped="error")
        with self.assertRaises(KeyError):
            strict.map("b")
        no_suffix = gx.OwnerScheme({"x": ["a"]}, keep_suffix=False)
        self.assertEqual(no_suffix.map("a.child"), ("a.child", ()))

    def test_json_round_trip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owners.json"
            self.scheme.to_json(path)
            again = gx.OwnerScheme.from_json(path)
        self.assertEqual(again.map("b.tiles0.clusters1.dcache"), self.scheme.map("b.tiles0.clusters1.dcache"))


class TestRunSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = gx.RunSet.from_dirs([SHAPES], labels=lambda p: {"config": "shapes"})

    def test_samples_are_runs_in_order(self):
        samples = gx.RunSet.from_samples([SHAPES], labels=lambda p: {"config": "shapes"})
        self.assertEqual(
            [dict(run.labels) for run in samples],
            [
                {"config": "shapes", "sample": 0, "run": "shapes", "sim_seconds": None},
                {"config": "shapes", "sample": 1, "run": "shapes", "sim_seconds": 0.000854},
            ],
        )
        # Root stats such as simTicks are not stats here (the dump's
        # simSeconds is the sim_seconds label); numCycles is only in the
        # second dump (the first is zero-filled unless fill=False), and the
        # last sample is the run from_dirs reads.
        frame = samples.get("numCycles", fill=False).to_frame()
        self.assertEqual(set(frame["sample"]), {1})
        self.assertEqual(
            sorted(frame["value"]),
            sorted(self.runs["numCycles"].to_frame()["value"]),
        )

    def test_labels_and_catalog(self):
        self.assertEqual(dict(self.runs.runs[0].labels), {"config": "shapes", "run": "shapes"})
        catalog = self.runs.catalog("*Controller*")
        self.assertEqual(list(catalog["kind"]), ["ruby_controller"])

    def test_ruby_counts_move_to_controllers(self):
        events = self.runs["Cache_Controller.events"]
        owners = {i.owner: v for i, v in events}
        self.assertEqual(
            owners,
            {
                "board.cache_hierarchy.core_tiles0.core_clusters0.dcache": 10.0,
                "board.cache_hierarchy.core_tiles0.core_clusters0.l2cache": 10.0,
            },
        )

    def test_fill_zero_for_same_class_owners(self):
        retries = self.runs["outTransLatHist.retries"]
        owners = {i.owner: v for i, v in retries}
        self.assertEqual(owners["board.cache_hierarchy.core_tiles0.core_clusters0.l2cache"], 0.0)
        unfilled = self.runs.get("outTransLatHist.retries", fill=False)
        self.assertEqual(len(unfilled), 1)

    def test_fill_reaches_runs_that_printed_nothing(self):
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            sparse = Path(directory) / "sparse"
            sparse.mkdir()
            shutil.copy(SHAPES / "config.json", sparse / "config.json")
            (sparse / "stats.txt").write_text(
                "---------- Begin Simulation Statistics ----------\n"
                "simTicks 1 # ticks (Tick)\n"
                "---------- End Simulation Statistics   ----------\n"
            )
            runs = gx.RunSet.from_dirs([SHAPES, sparse])
            cycles = {(i.label("run"), i.owner): v for i, v in runs["numCycles"]}
            self.assertEqual(cycles[("sparse", "board.processor.cores0.core")], 0.0)
            # With dims, a run that printed nothing has no known dim values.
            retries = runs["outTransLatHist.retries"]
            self.assertEqual(retries.labels("run"), ["shapes"])

    def test_unknown_name_hints(self):
        with self.assertRaises(KeyError) as error:
            self.runs["numcycles"]
        self.assertIn("numCycles", str(error.exception))


@unittest.skipUnless(EXAMPLE_DATA.exists(), "example_data/ not present")
class TestExampleRunSet(unittest.TestCase):
    def test_canonical_owners_and_controller_lookup(self):
        scheme = gx.OwnerScheme(
            {"tile{t}.cluster{c}.l1d": ["board.cache_hierarchy.core_tiles{t}.core_clusters{c}.dcache"]}
        )
        runs = gx.RunSet.from_dirs(sorted(EXAMPLE_DATA.glob("profiling/*"))[:1], owners=scheme)
        misses = runs["m_demand_misses"].select(owner="tile*.l1d.cache")
        self.assertEqual(len(misses), 8)
        run = runs.runs[0]
        self.assertEqual(run.controller("Cache_0"), "board.cache_hierarchy.core_tiles0.core_clusters0.dcache")
        events = runs["Cache_Controller.events"].select(owner="tile0.cluster0.l1d", event="Load")
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
