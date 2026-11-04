import math
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

import gxtest  # noqa: E402,F401  (path and a temporary cache)

ROOT = Path(__file__).resolve().parents[1]

from graphix import (  # noqa: E402
    ConfigTree,
    count_dumps,
    dump_locations,
    find_dump_file,
    read_dump,
    split_name,
)
from graphix.parse import StatsParseError  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SHAPES = FIXTURES / "shapes"
EXAMPLE_DATA = ROOT / "example_data"


def read_shapes(dump=-1):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lines = read_dump(SHAPES / "stats.txt", dump)
    return {(l.name, l.occurrence): l for l in lines}, caught


class TestReadDump(unittest.TestCase):
    def test_last_dump_is_default(self):
        lines, _ = read_shapes()
        self.assertIn(("simSeconds", 0), lines)
        self.assertNotIn(("finalTick", 0), lines)

    def test_select_dump_by_index(self):
        lines, _ = read_shapes(0)
        self.assertEqual(lines[("simTicks", 0)].values, (1000.0,))
        with self.assertRaises(IndexError):
            read_dump(SHAPES / "stats.txt", 2)

    def test_plain_line_with_compound_unit(self):
        lines, _ = read_shapes()
        line = lines[("hostTickRate", 0)]
        self.assertEqual(line.values, (15955463.0,))
        self.assertEqual(line.unit, "Tick/Second")
        self.assertEqual(
            line.description,
            "The number of ticks simulated per host second (ticks/s)",
        )
        self.assertFalse(line.oneline)

    def test_nan_negative_and_pdf_columns(self):
        lines, _ = read_shapes()
        mean = "board.processor.cores0.core.commit.numCommittedDist::mean"
        self.assertTrue(math.isnan(lines[(mean, 0)].values[0]))
        delta = "board.processor.cores0.core.delta"
        self.assertEqual(lines[(delta, 0)].values, (-2.5,))
        bucket = "board.processor.cores0.core.commit.numCommittedDist::0"
        self.assertEqual(lines[(bucket, 0)].values, (60.0,))
        self.assertEqual(lines[(bucket, 0)].unit, "Count")

    def test_no_description_unit_only(self):
        lines, _ = read_shapes()
        retries = (
            "board.cache_hierarchy.core_tiles0.core_clusters0.dcache."
            "outTransLatHist.SendEvict.retries"
        )
        self.assertIsNone(lines[(retries, 0)].description)
        self.assertEqual(lines[(retries, 0)].unit, "Unspecified")

    def test_oneline(self):
        lines, _ = read_shapes()
        hist = (
            "board.cache_hierarchy.core_tiles0.core_clusters0.dcache."
            "outTransLatHist.SendEvict"
        )
        self.assertTrue(lines[(hist, 0)].oneline)
        self.assertEqual(lines[(hist, 0)].values, (216152, 12, 2, 0, 0))
        ctrl = "board.cache_hierarchy.ruby_system.Cache_Controller.Load"
        self.assertEqual(lines[(ctrl, 0)].values, (10.0, 10.0))
        wide = "board.cache_hierarchy.ruby_system.m_outstandReqHistSeqr"
        self.assertEqual(
            lines[(wide, 0)].values, (712623289462, 437977020129, 0)
        )

    def test_duplicates_are_numbered_and_warned_once(self):
        lines, caught = read_shapes()
        name = "board.cache_hierarchy.core_tiles0.core_clusters0.dcache.avg_size"
        self.assertEqual(lines[(name, 0)].values, (4.749266,))
        self.assertEqual(lines[(name, 1)].values, (0.873239,))
        self.assertEqual(len(caught), 1)
        self.assertIn("1 stat name(s)", str(caught[0].message))

    def test_bad_value_reports_file_and_line(self):
        bad = FIXTURES / "bad" / "stats.txt"
        with self.assertRaises(StatsParseError) as error:
            read_dump(bad)
        self.assertEqual(error.exception.lineno, 3)
        self.assertIn("stats.txt:3", str(error.exception))

    def test_find_dump_file(self):
        self.assertEqual(find_dump_file(SHAPES), SHAPES / "stats.txt")
        largest = FIXTURES / "dumps"
        self.assertEqual(
            find_dump_file(largest), largest / "stats_dump_10.txt"
        )

    def test_dump_locations(self):
        self.assertEqual(count_dumps(SHAPES / "stats.txt"), 2)
        self.assertEqual(
            dump_locations(SHAPES),
            [(SHAPES / "stats.txt", 0), (SHAPES / "stats.txt", 1)],
        )
        numbered = FIXTURES / "dumps"
        self.assertEqual(
            dump_locations(numbered),
            [
                (numbered / f"stats_dump_{n}.txt", -1)
                for n in (2, 9, 10)
            ],
        )

    def test_dump_being_written_does_not_stop_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.txt"
            path.write_text(
                (SHAPES / "stats.txt").read_text()
                + "---------- Begin Simulation Statistics ----------\n"
                + "simTicks 5\n"
            )
            self.assertEqual(count_dumps(path), 2)
            last = {line.name: line for line in read_dump(path)}
            self.assertEqual(last["simSeconds"].values, (0.000854,))
            first = {line.name: line for line in read_dump(path, 0)}
            self.assertEqual(first["simTicks"].values, (1000.0,))


class TestConfigAndNames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ConfigTree.from_file(SHAPES / "config.json")

    def test_tree_paths_and_params(self):
        self.assertIn("", self.tree)
        dcache = self.tree["board.cache_hierarchy.core_tiles0.core_clusters0.dcache"]
        self.assertEqual(dcache.version, 0)
        self.assertEqual(dcache.cache.size, "64KiB")
        self.assertEqual(self.tree.root.board.cache_line_size, 64)
        self.assertEqual(self.tree.root.board.mem_ranges, [])
        with self.assertRaises(AttributeError):
            dcache.no_such_param

    def test_find(self):
        found = self.tree.find(type="CHI_Cache_Controller", version=1)
        self.assertEqual(
            [obj.path for obj in found],
            ["board.cache_hierarchy.core_tiles0.core_clusters0.l2cache"],
        )

    def test_split_global_and_owned(self):
        self.assertEqual(split_name("simSeconds", self.tree).owner, "")
        name = split_name(
            "board.processor.cores0.core.commit.numCommittedDist::0", self.tree
        )
        self.assertEqual(name.owner, "board.processor.cores0.core")
        self.assertEqual(name.key, "commit.numCommittedDist")
        self.assertEqual(name.field, "0")

    def test_split_keeps_dots_in_key(self):
        name = split_name(
            "board.cache_hierarchy.core_tiles0.core_clusters0.dcache."
            "outTransLatHist.SendEvict.retries",
            self.tree,
        )
        self.assertEqual(
            name.owner, "board.cache_hierarchy.core_tiles0.core_clusters0.dcache"
        )
        self.assertEqual(name.key, "outTransLatHist.SendEvict.retries")
        self.assertIsNone(name.field)

    def test_split_vector2d_row_with_dotted_subname(self):
        name = split_name(
            "board.memory.mem_ctrl0.avgPriority_cache_hierarchy."
            "memory_controllers1::samples",
            self.tree,
        )
        self.assertEqual(name.owner, "board.memory.mem_ctrl0")
        self.assertEqual(
            name.key, "avgPriority_cache_hierarchy.memory_controllers1"
        )

    def test_split_stat_named_like_child_is_not_owner(self):
        # The last token is always the stat, never a SimObject.
        name = split_name(
            "board.cache_hierarchy.core_tiles0.core_clusters0.dcache.cache",
            self.tree,
        )
        self.assertEqual(name.key, "cache")

    def test_padded_vector_names_resolve_unpadded(self):
        def obj(name, path, **extra):
            return dict(type="T", cxx_class="gem5::T", name=name, path=path, **extra)

        links = [obj(f"links{i:02d}", f"net.links{i:02d}") for i in range(11)]
        tree = ConfigTree.from_dict(
            obj(None, "root", net=obj("net", "net", links=links, rank01=obj("rank01", "net.rank01")))
        )
        self.assertEqual(tree["net.links1"].path, "net.links01")
        self.assertIs(tree["net.links1"], tree["net.links01"])
        self.assertEqual(len(tree), 1 + 1 + 11 + 1)
        name = split_name("net.links1.m_msg_count", tree)
        self.assertEqual((name.owner, name.key), ("net.links1", "m_msg_count"))
        # A single SimObject keeps its own name; only vectors are padded.
        self.assertNotIn("net.rank1", tree)

    def test_split_without_tree(self):
        name = split_name("a.b.c::d")
        self.assertEqual((name.owner, name.key, name.field), ("", "a.b.c", "d"))


@unittest.skipUnless(EXAMPLE_DATA.exists(), "example_data/ not present")
class TestExampleData(unittest.TestCase):
    def test_every_line_parses_and_resolves(self):
        runs = sorted(p.parent for p in EXAMPLE_DATA.glob("*/*/config.json"))
        self.assertTrue(runs)
        for run in runs:
            with self.subTest(run=run.relative_to(EXAMPLE_DATA)):
                tree = ConfigTree.from_file(run / "config.json")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    lines = read_dump(find_dump_file(run))
                self.assertGreater(len(lines), 1000)
                owned = sum(
                    1 for l in lines if split_name(l.name, tree).owner
                )
                # Only the root's own stats (simSeconds, ...) have no owner.
                self.assertLess(len(lines) - owned, 20)
                # Padded vector children (system_links01) must be owners.
                stuck = [
                    l.name
                    for l in lines
                    if split_name(l.name, tree).key.startswith(
                        ("system_links", "port_buffers", "buffers")
                    )
                ]
                self.assertEqual(stuck, [])


if __name__ == "__main__":
    unittest.main()
