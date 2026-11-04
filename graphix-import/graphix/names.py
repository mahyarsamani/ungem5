"""Split a printed stat name into owner, key, and field.

A printed name is `<owner>.<key>[::<field>]`:
- the owner is the deepest SimObject path from `config.json`;
- the key is everything between the owner and `::`, kept verbatim, because
  its dots can be a named stats group (`commit`, `RequestType`) or part of the
  stat's own name (`outTransLatHist.SendEvict`) and the text cannot tell
  these apart;
- the field is what follows `::`: a vector element, a bucket, or a
  distribution field.
Stat specs (`graphix.specs`) later give meaning to the dots in a key.
"""

from dataclasses import dataclass
from typing import Optional

from .config import ConfigTree

FIELD_SEPARATOR = "::"


@dataclass(frozen=True)
class StatName:
    owner: str
    key: str
    field: Optional[str]

    @property
    def base(self) -> str:
        """The name without its field: `<owner>.<key>`, or the key alone."""
        return f"{self.owner}.{self.key}" if self.owner else self.key


def split_name(name: str, tree: Optional[ConfigTree] = None) -> StatName:
    """Split a printed stat name; without a tree, the owner is empty."""
    base, separator, field = name.partition(FIELD_SEPARATOR)
    field = field if separator else None
    if tree is None:
        return StatName("", base, field)
    tokens = base.split(".")
    length = tree.owner_length(tokens)
    return StatName(
        ".".join(tokens[:length]), ".".join(tokens[length:]), field
    )
