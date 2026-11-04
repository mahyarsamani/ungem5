import math
import shutil
import tempfile
import unittest
import warnings
from pathlib import Path

import gxtest  # noqa: E402,F401  (path and a temporary cache)

import graphix as gx  # noqa: E402
from graphix.gem5 import derived  # noqa: E402
from graphix.stats import Buckets, Index  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHAPES = ROOT / "tests" / "fixtures" / "shapes"
EXAMPLE_DATA = ROOT / "example_data"


def idx(run, owner):
    return Index((("run", run),), owner, (), 0, owner)


class TestTails(unittest.TestCase):
    def setUp(self):
        # 1 underflow, 2 + 4 + 2 in buckets, 1 overflow: 10 samples.
        self.b = Buckets(((0, 1, 2), (1, 2, 4), (2, 3, 2)), underflow=1, overflow=1)

    def test_quantile_counts_tails(self):
        self.assertEqual(self.b.quantile(0.05), -math.inf)
        self.assertEqual(self.b.quantile(0.95), math.inf)
        self.assertAlmostEqual(self.b.quantile(0.5), 1 + (5 - 3) / 4)
        self.assertAlmostEqual(self.b.quantile(0.5, tails=False), 1 + (4 - 2) / 4)

    def test_cdf_pdf_over_every_sample(self):
        self.assertEqual(self.b.cdf(), [0.3, 0.7, 0.9])
        self.assertEqual(self.b.pdf(), [0.2, 0.4, 0.2])
        self.assertEqual(self.b.cdf(tails=False), [0.25, 0.75, 1.0])

    def test_frame_tail_rows(self):
        hist = gx.Histogram("h", {idx("r", "o"): self.b})
        frame = hist.to_frame(tails=True)
        self.assertEqual(list(frame["lo"])[0], -math.inf)
        self.assertEqual(list(frame["hi"])[-1], math.inf)
        self.assertEqual(list(frame["count"]), [1, 2, 4, 2, 1])
        self.assertEqual(list(frame["cdf"])[-1], 1.0)
        self.assertEqual(len(hist.to_frame()), 3)


class TestUnits(unittest.TestCase):
    def test_scale_histogram_and_scalar(self):
        b = Buckets(((0, 500, 3), (500, 1000, 1)), value_sum=1000.0)
        hist = gx.Histogram("lat", {idx("r", "o"): b}, "Tick").scale(1 / 250, "Cycle")
        scaled = hist[idx("r", "o")]
        self.assertEqual(scaled.buckets, ((0, 2, 3), (2, 4, 1)))
        self.assertEqual(scaled.mean(), 1.0)
        self.assertEqual(hist.unit, "Cycle")
        ticks = gx.Scalar("t", {idx("r", "o"): 500.0}, "Tick").scale(1 / 250, "Cycle")
        self.assertEqual([v for _, v in ticks], [2.0])

    def test_unspecified_sum_does_not_warn(self):
        s = gx.Scalar("hits", {idx("r", "a"): 1.0, idx("r", "b"): 2.0}, "Unspecified")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            s.reduce("sum")
            s.with_unit("Ratio").reduce("sum")
        self.assertEqual(len(caught), 1)

    def test_ticks_per_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "run"
            shutil.copytree(SHAPES, run)
            text = (run / "config.json").read_text().replace(
                '"cache_line_size": 64,',
                '"cache_line_size": 64, "clk_domain": {"type": "SrcClockDomain", '
                '"cxx_class": "gem5::SrcClockDomain", "name": "clk_domain", '
                '"path": "board.clk_domain", "clock": [250]},',
            )
            (run / "config.json").write_text(text)
            runs = gx.RunSet.from_dirs([run])
            self.assertEqual(runs.runs[0].ticks_per_cycle(), 250.0)


class TestOuter(unittest.TestCase):
    def test_scalar_outer_fills(self):
        hits = gx.Scalar("hits", {idx("r", "a"): 3.0, idx("r", "b"): 1.0}, "Count")
        misses = gx.Scalar("misses", {idx("r", "b"): 1.0, idx("r", "c"): 2.0}, "Count")
        total = hits.add(misses, how="outer")
        self.assertEqual({i.owner: v for i, v in total}, {"a": 3.0, "b": 2.0, "c": 2.0})
        ratio = hits.div(total, how="outer")
        self.assertEqual({i.owner: v for i, v in ratio}, {"a": 1.0, "b": 0.5, "c": 0.0})

    def test_vector_outer(self):
        a = gx.Vector("a", {idx("r", "x"): {"e": 1.0}})
        b = gx.Vector("b", {idx("r", "y"): {"e": 2.0}})
        total = a.add(b, how="outer")
        self.assertEqual({i.owner: dict(v) for i, v in total}, {"x": {"e": 1.0}, "y": {"e": 2.0}})


class TestEmpty(unittest.TestCase):
    def test_empty_frames_keep_columns(self):
        s = gx.Scalar("s", {idx("r", "a"): 1.0})
        self.assertEqual(list(s.select(owner="nothing").to_frame().columns), ["owner", "stat", "value", "unit"])
        h = gx.Histogram("h", {idx("r", "a"): Buckets(((0, 1, 1),))})
        self.assertIn("cdf", h.select(owner="nothing").to_frame().columns)
        self.assertIn("p50", h.select(owner="nothing").to_frame("summary").columns)
        v = gx.Vector("v", {idx("r", "a"): {"e": 1.0}})
        self.assertIn("element", v.select(owner="nothing").to_frame().columns)
        self.assertEqual(len(gx.table(s.select(owner="nothing"))), 0)

    def test_catalog_without_match(self):
        runs = gx.RunSet.from_dirs([SHAPES])
        catalog = runs.catalog("no such stat*")
        self.assertTrue(catalog.empty)
        self.assertIn("owner_class", catalog.columns)

    def test_histogram_not_filled_into_runs_without_it(self):
        with tempfile.TemporaryDirectory() as directory:
            sparse = Path(directory) / "sparse"
            sparse.mkdir()
            shutil.copy(SHAPES / "config.json", sparse / "config.json")
            (sparse / "stats.txt").write_text(
                "---------- Begin Simulation Statistics ----------\n"
                "simTicks 1 # ticks (Tick)\n"
                "---------- End Simulation Statistics   ----------\n"
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                runs = gx.RunSet.from_dirs([SHAPES, sparse])
            dist = runs["commit.numCommittedDist"]
            self.assertEqual(dist.labels("run"), ["shapes"])
            # Scalars still are: the sparse run's core ran zero cycles.
            self.assertIn("sparse", runs["numCycles"].labels("run"))
            empty = gx.Histogram("h", {idx("r", "a"): Buckets(), idx("r", "b"): Buckets(((0, 1, 1),))})
            self.assertEqual(empty.drop_empty().owners(), ["b"])


@unittest.skipUnless(EXAMPLE_DATA.exists(), "example_data/ not present")
class TestDerived(unittest.TestCase):
    def test_demand_hit_ratio(self):
        runs = gx.RunSet.from_dirs(sorted(EXAMPLE_DATA.glob("profiling/*"))[:1])
        owner = "*core_tiles0.core_clusters0.dcache.cache"
        accesses = derived.demand_accesses(runs, owner)
        hits = runs["m_demand_hits"].select(owner=owner)
        misses = runs["m_demand_misses"].select(owner=owner)
        self.assertEqual(
            [v for _, v in accesses], [hits.values()[0] + misses.values()[0]]
        )
        ratio = derived.demand_hit_ratio(runs, owner).values()[0]
        self.assertAlmostEqual(ratio, hits.values()[0] / accesses.values()[0])


if __name__ == "__main__":
    unittest.main()
