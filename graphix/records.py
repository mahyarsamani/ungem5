"""Group the lines of one dump into stat blocks.

A block is every line of one stat instance: one family name, one owner, one
set of dimension values, and one occurrence.
A scalar is a block with a single plain line; a vector or histogram is a
block of `::<field>` lines, a one-line (`|`) line, or both.
The block's kind comes from its spec, or else from its lines:
- a `samples` field means a histogram (gem5's Distribution and Histogram
  always print it);
- other fields, or a one-line line, mean a vector;
- a single plain line means a scalar.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import ConfigTree
from .names import FIELD_SEPARATOR, split_name
from .parse import StatLine
from .specs import Registry, StatSpec

KINDS = ("scalar", "vector", "histogram", "ruby_controller")


@dataclass
class Block:
    name: str
    owner: str
    owner_class: Optional[str]
    dims: Tuple[Tuple[str, object], ...]
    occurrence: int
    spec: Optional[StatSpec]
    fields: Dict[Optional[str], Tuple[float, ...]] = field(default_factory=dict)
    oneline: bool = False
    unit: Optional[str] = None
    description: Optional[str] = None

    @property
    def kind(self) -> str:
        if self.spec is not None and self.spec.kind is not None:
            kind = self.spec.kind
            return kind if isinstance(kind, str) else getattr(kind, "kind", "custom")
        named = [f for f in self.fields if f is not None]
        if "samples" in named or "bucket_size" in named:
            return "histogram"
        if named or self.oneline:
            return "vector"
        return "scalar"


@dataclass
class DumpBlocks:
    """All blocks of one dump, plus what was skipped and why."""

    blocks: List[Block]
    skipped: Counter  # spec doc (or key) -> number of lines

    def by_name(self) -> Dict[str, List[Block]]:
        grouped: Dict[str, List[Block]] = {}
        for block in self.blocks:
            grouped.setdefault(block.name, []).append(block)
        return grouped


def build_blocks(
    lines: List[StatLine],
    tree: Optional[ConfigTree] = None,
    registry: Optional[Registry] = None,
) -> DumpBlocks:
    registry = registry if registry is not None else Registry()
    blocks: Dict[tuple, Block] = {}
    skipped: Counter = Counter()
    # Most lines repeat a base name with another `::` field (a vector's
    # elements, a histogram's buckets), so the owner split and the spec
    # resolution are done once per base name.
    by_base: Dict[str, tuple] = {}
    for line in lines:
        base, separator, line_field = line.name.partition(FIELD_SEPARATOR)
        line_field = line_field if separator else None
        resolved = by_base.get(base)
        if resolved is None:
            stat_name = split_name(base, tree)
            owner_class = (
                tree[stat_name.owner].cxx_class if tree is not None else None
            )
            resolution = registry.resolve(
                stat_name.owner, owner_class, stat_name.key
            )
            resolved = (stat_name.owner, stat_name.key, owner_class, resolution)
            by_base[base] = resolved
        owner, key, owner_class, resolution = resolved
        if resolution.skip:
            skipped[resolution.spec.doc or key] += 1
            continue
        block_key = (
            resolution.name,
            owner,
            resolution.dims,
            line.occurrence,
        )
        block = blocks.get(block_key)
        if block is None:
            block = Block(
                resolution.name,
                owner,
                owner_class,
                resolution.dims,
                line.occurrence,
                resolution.spec,
            )
            blocks[block_key] = block
        if line_field in block.fields:
            raise ValueError(
                f"line {line.lineno}: '{line.name}' maps onto a block that "
                f"already has field {line_field!r}; two keys resolve to "
                f"the same family '{resolution.name}' and dims {resolution.dims}."
            )
        block.fields[line_field] = line.values
        block.oneline = block.oneline or line.oneline
        block.unit = block.unit or line.unit
        block.description = block.description or line.description
    return DumpBlocks(list(blocks.values()), skipped)


def suggest_specs(dump: DumpBlocks, min_variants: int = 3) -> List[str]:
    """List key families that look like they carry a dimension.

    A family is a group of unspecced stat names under owners of the same C++
    class that differ only in their last dot token, with at least
    `min_variants` different last tokens, and whose blocks all share one kind
    and one description.
    The shared description separates a dimension (`usefulBytes.<accessor>`,
    one meaning per variant) from a named stats group (`commit.<stat>`,
    a different meaning per member).
    The output is a list of template suggestions such as
    `gem5::ruby::CacheMemory: usefulBytes.{?}  (5 variants: cell, face, ...)`.
    """
    variants: Dict[Tuple[Optional[str], str], set] = {}
    signatures: Dict[Tuple[Optional[str], str], set] = {}
    for block in dump.blocks:
        if block.spec is not None or "." not in block.name:
            continue
        prefix, _, last = block.name.rpartition(".")
        group = (block.owner_class, prefix)
        variants.setdefault(group, set()).add(last)
        signatures.setdefault(group, set()).add((block.kind, block.description))
    suggestions = []
    for (owner_class, prefix), lasts in sorted(
        variants.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        if len(lasts) < min_variants:
            continue
        if len(signatures[(owner_class, prefix)]) > 1:
            continue
        sample = ", ".join(sorted(lasts)[:4])
        suggestions.append(
            f"{owner_class}: {prefix}.{{?}}  ({len(lasts)} variants: {sample}, ...)"
        )
    return suggestions
