import re
import sys
import unittest
import warnings
from pathlib import Path

import gxtest  # noqa: E402,F401  (path and a temporary cache)

ROOT = Path(__file__).resolve().parents[1]

from graphix import ConfigTree, find_dump_file, read_dump  # noqa: E402
from graphix.parse import StatLine  # noqa: E402
from graphix.records import build_blocks, suggest_specs  # noqa: E402
from graphix.specs import Registry, StatSpec, compile_template  # noqa: E402

SHAPES = ROOT / "tests" / "fixtures" / "shapes"
EXAMPLE_DATA = ROOT / "example_data"

DCACHE = "board.cache_hierarchy.core_tiles0.core_clusters0.dcache"


def line(name, *values, oneline=False, occurrence=0, description=None):
    return StatLine(name, tuple(values), oneline, description, None, 1, occurrence)


class TestTemplates(unittest.TestCase):
    def test_default_name_drops_placeholders(self):
        regex, name = compile_template("outTransLatHist.{event}.retries")
        self.assertEqual(name, "outTransLatHist.retries")
        self.assertEqual(
            regex.fullmatch("outTransLatHist.SendEvict.retries").group("event"),
            "SendEvict",
        )
        self.assertIsNone(regex.fullmatch("outTransLatHist.a.b.retries"))

    def test_placeholder_inside_a_token_may_be_empty(self):
        regex, name = compile_template("indRel{kind}AccLat.{relation}")
        self.assertEqual(name, "indRelAccLat")
        self.assertEqual(regex.fullmatch("indRelAccLat.x").group("kind"), "")
        self.assertEqual(regex.fullmatch("indRelProdAccLat.x").group("kind"), "Prod")

    def test_rest_placeholder_takes_dots(self):
        regex, _ = compile_template("pktCount_{src...}")
        self.assertEqual(
            regex.fullmatch("pktCount_board.cache.mem_side").group("src"),
            "board.cache.mem_side",
        )

    def test_literals_are_escaped(self):
        regex, _ = compile_template("bytesTransferred[{type}][{dst}]")
        match = regex.fullmatch("bytesTransferred[prefetch][Cache_2]")
        self.assertEqual(match.group("type"), "prefetch")
        self.assertEqual(regex.fullmatch("bytesTransferred[][Cache_2]").group("type"), "")


class TestStatSpec(unittest.TestCase):
    def test_name_format_and_dim_conversion(self):
        spec = StatSpec(
            re.compile(r"(?P<stat>lookups|squashes)_(?P<thread>\d+)"),
            name="{stat}",
            dims={"thread": int},
        )
        resolution = spec.resolve("lookups_3", spec.match_key("lookups_3"))
        self.assertEqual(resolution.name, "lookups")
        self.assertEqual(resolution.dims, (("thread", "3"),))
        self.assertEqual(spec.convert_dims(resolution.dims), (("thread", 3),))

    def test_converter_may_take_the_run(self):
        spec = StatSpec("x.{dst}", dims={"dst": lambda dst, run: f"{run}:{dst}"})
        dims = spec.resolve("x.Cache_2", spec.match_key("x.Cache_2")).dims
        self.assertEqual(spec.convert_dims(dims, "r0"), (("dst", "r0:Cache_2"),))

    def test_owner_filters(self):
        spec = StatSpec("x", owner="*.dcache", owner_class="gem5::ruby::*")
        self.assertTrue(spec.match_owner("a.dcache", "gem5::ruby::CHI::Cache_Controller"))
        self.assertFalse(spec.match_owner("a.icache", "gem5::ruby::CHI::Cache_Controller"))
        self.assertFalse(spec.match_owner("a.dcache", "gem5::o3::CPU"))
        self.assertFalse(spec.match_owner("a.dcache", None))

    def test_user_specs_come_first(self):
        override = StatSpec("outTransLatHist.{event}", name="mine", kind="vector")
        registry = Registry([override])
        resolution = registry.resolve(DCACHE, "gem5::ruby::CHI::Cache_Controller", "outTransLatHist.SendEvict")
        self.assertEqual(resolution.name, "mine")
        builtin = Registry().resolve(DCACHE, "gem5::ruby::CHI::Cache_Controller", "outTransLatHist.SendEvict")
        self.assertEqual(builtin.name, "outTransLatHist")
        self.assertEqual(builtin.dims, (("event", "SendEvict"),))

    def test_cache_respects_owner(self):
        registry = Registry()
        # 'hits' is a Formula on a TLB but a plain stat elsewhere.
        self.assertTrue(registry.resolve("a.dtb", "gem5::ArmISA::TLB", "hits").skip)
        plain = registry.resolve("a.indirect", "gem5::branch_prediction::SimpleIndirectPredictor", "hits")
        self.assertFalse(plain.skip)
        self.assertIsNone(plain.spec)

    def test_add_invalidates_cache(self):
        registry = Registry(builtins=False)
        self.assertIsNone(registry.resolve("", None, "foo").spec)
        registry.add(StatSpec("foo", kind="scalar"))
        self.assertIsNotNone(registry.resolve("", None, "foo").spec)


class TestBuildBlocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ConfigTree.from_file(SHAPES / "config.json")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            lines = read_dump(SHAPES / "stats.txt")
        cls.dump = build_blocks(lines, tree, Registry())
        cls.blocks = {(b.name, b.owner, b.dims, b.occurrence): b for b in cls.dump.blocks}

    def block(self, name, owner, dims=(), occurrence=0):
        return self.blocks[(name, owner, dims, occurrence)]

    def test_formulas_and_summary_only_stats_are_skipped(self):
        names = {b.name for b in self.dump.blocks}
        self.assertNotIn("simSeconds", names)
        self.assertNotIn("hostTickRate", names)
        self.assertEqual(self.dump.skipped["Formula"], 2)
        self.assertEqual(self.dump.skipped["VectorStandardDeviation: summaries only"], 1)

    def test_vector2d_row_becomes_dim(self):
        block = self.block("lookups", "board.processor.cores0.core.branchPred", (("thread", "0"),))
        self.assertEqual(block.kind, "vector")
        self.assertEqual(block.spec.element_dim, "branch_type")
        self.assertEqual(set(block.fields), {"NoBranch", "Return", "total"})

    def test_kinds_from_lines(self):
        core = "board.processor.cores0.core"
        self.assertEqual(self.block("numCycles", core).kind, "scalar")
        self.assertEqual(self.block("statFuBusy", core).kind, "vector")
        self.assertEqual(self.block("commit.numCommittedDist", core).kind, "histogram")

    def test_ruby_stats(self):
        hist = self.block("outTransLatHist", DCACHE, (("event", "SendEvict"),))
        self.assertEqual(hist.kind, "histogram")
        self.assertTrue(hist.oneline)
        self.assertEqual(set(hist.fields), {None, "bucket_size", "max_bucket"})
        retries = self.block("outTransLatHist.retries", DCACHE, (("event", "SendEvict"),))
        self.assertEqual(retries.kind, "scalar")
        ctrl = self.block(
            "Cache_Controller.events",
            "board.cache_hierarchy.ruby_system",
            (("event", "Load"),),
        )
        self.assertEqual(ctrl.kind, "ruby_controller")
        self.assertEqual(ctrl.fields[None], (10.0, 10.0))

    def test_duplicates_become_separate_blocks(self):
        first = self.block("avg_size", DCACHE)
        second = self.block("avg_size", DCACHE, occurrence=1)
        self.assertNotEqual(first.fields[None], second.fields[None])

    def test_collision_is_an_error(self):
        registry = Registry([StatSpec("{a}_{b}", name="same")], builtins=False)
        lines = [line("x_1", 1.0), line("x_2", 2.0)]
        dump = build_blocks(lines, None, registry)
        self.assertEqual(len(dump.blocks), 2)  # dims differ: no collision
        registry = Registry([StatSpec(re.compile(r"\w+"), name="same")], builtins=False)
        with self.assertRaises(ValueError):
            build_blocks(lines, None, registry)


class TestSuggestSpecs(unittest.TestCase):
    def test_dimension_family_is_suggested_but_group_is_not(self):
        lines = [
            line(f"usefulBytes.{a}::samples", 1.0, description="Useful bytes")
            for a in ("cell", "face", "zone")
        ] + [
            line("commit.amos", 1.0, description="AMOs"),
            line("commit.membars", 1.0, description="Membars"),
            line("commit.functionCalls", 1.0, description="Calls"),
        ]
        dump = build_blocks(lines, None, Registry(builtins=False))
        suggestions = suggest_specs(dump)
        self.assertEqual(len(suggestions), 1)
        self.assertIn("usefulBytes.{?}", suggestions[0])


@unittest.skipUnless(EXAMPLE_DATA.exists(), "example_data/ not present")
class TestExampleBlocks(unittest.TestCase):
    def test_every_run_builds(self):
        for run in sorted(p.parent for p in EXAMPLE_DATA.glob("*/*/config.json")):
            with self.subTest(run=run.relative_to(EXAMPLE_DATA)):
                tree = ConfigTree.from_file(run / "config.json")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    dump = build_blocks(read_dump(find_dump_file(run)), tree)
                kinds = {b.kind for b in dump.blocks}
                self.assertEqual(kinds, {"scalar", "vector", "histogram", "ruby_controller"})
                self.assertGreater(dump.skipped["Formula"], 100)
                # Every Ruby controller block is a one-line vector.
                for block in dump.blocks:
                    if block.kind == "ruby_controller":
                        self.assertIn(None, block.fields)


if __name__ == "__main__":
    unittest.main()
