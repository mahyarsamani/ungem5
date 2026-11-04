"""The SimObject tree of one run, read from gem5's `config.json`.

Every SimObject in `config.json` is a dict with `type`, `cxx_class`, `name`,
and `path`; a vector of SimObjects is a list of such dicts.
Everything else on a SimObject is a parameter.
The root's path is `root` in `config.json`, but stats of the root carry no
prefix, so graphix gives the root the empty path.

`config.json` zero-pads the names of SimObject vectors with ten or more
elements (`system_links01`), while stats use the unpadded name
(`system_links1`).
The tree accepts both spellings; `SimObject.path` keeps the config one.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union


class SimObject:
    """One SimObject: its path, type, parameters, and children."""

    def __init__(
        self,
        name: str,
        path: str,
        type: str,
        cxx_class: str,
        params: Dict[str, Any],
        parent: Optional["SimObject"] = None,
    ) -> None:
        self._name = name
        self._path = path
        self._type = type
        self._cxx_class = cxx_class
        self._params = params
        self._parent = parent
        self._children: Dict[str, "SimObject"] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def path(self) -> str:
        return self._path

    @property
    def type(self) -> str:
        return self._type

    @property
    def cxx_class(self) -> str:
        return self._cxx_class

    @property
    def params(self) -> Dict[str, Any]:
        return self._params

    @property
    def parent(self) -> Optional["SimObject"]:
        return self._parent

    @property
    def children(self) -> Dict[str, "SimObject"]:
        """Children by name; padded vector names also appear unpadded."""
        return self._children

    def walk(self) -> Iterator["SimObject"]:
        """Yield this SimObject and all its descendants, depth first."""
        yield self
        for child_name, child in self._children.items():
            if child.name == child_name:  # skip unpadded aliases
                yield from child.walk()

    def __getattr__(self, name: str) -> Any:
        # Only called when normal lookup fails; guard against recursion
        # before __init__ has set the fields (copy, pickle).
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._params:
            return self._params[name]
        if name in self._children:
            return self._children[name]
        raise AttributeError(
            f"SimObject '{self._path or '<root>'}' has no parameter or "
            f"child named '{name}'."
        )

    def __repr__(self) -> str:
        return f"SimObject({self._path or '<root>'!r}, type={self._type!r})"


class ConfigTree:
    """All SimObjects of one run, indexed by path."""

    def __init__(self, root: SimObject) -> None:
        self._root = root
        self._by_path = {}
        self._index(root, "")

    def _index(self, obj: SimObject, stats_path: str) -> None:
        self._by_path[obj.path] = obj
        self._by_path.setdefault(stats_path, obj)
        for child_name, child in obj.children.items():
            if child.name != child_name:
                continue  # an unpadded alias; reached through its real name
            alias = _unpadded(child_name)
            if obj.children.get(alias) is not child:
                alias = child_name
            child_stats = f"{stats_path}.{alias}" if stats_path else alias
            self._index(child, child_stats)

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "ConfigTree":
        with open(path, "r") as config_file:
            return cls.from_dict(json.load(config_file))

    @classmethod
    def from_dict(cls, config: Dict[str, Any]) -> "ConfigTree":
        if not _is_sim_object(config):
            raise ValueError(
                "config.json root is not a SimObject "
                "(expected 'type', 'cxx_class', and 'path')."
            )
        return cls(_build(config, parent=None, path=""))

    @property
    def root(self) -> SimObject:
        return self._root

    def __contains__(self, path: str) -> bool:
        return path in self._by_path

    def __getitem__(self, path: str) -> SimObject:
        try:
            return self._by_path[path]
        except KeyError:
            raise KeyError(f"No SimObject with path '{path}'.") from None

    def __iter__(self) -> Iterator[SimObject]:
        return self._root.walk()

    def __len__(self) -> int:
        return sum(1 for _ in self._root.walk())

    def find(
        self,
        type: Optional[str] = None,
        cxx_class: Optional[str] = None,
        **params: Any,
    ) -> List[SimObject]:
        """Return the SimObjects whose type, class, and params all match."""
        found = []
        for obj in self._root.walk():
            if type is not None and obj.type != type:
                continue
            if cxx_class is not None and obj.cxx_class != cxx_class:
                continue
            if any(obj.params.get(k, _MISSING) != v for k, v in params.items()):
                continue
            found.append(obj)
        return found

    def owner_length(self, tokens: Sequence[str]) -> int:
        """Return how many leading tokens form the deepest SimObject path.

        The match walks the tree token by token, so a token is never split.
        The last token is never taken, because a stat needs a name.
        Returns 0 when the first token is not a child of the root.
        """
        node = self._root
        length = 0
        for token in tokens[:-1]:
            child = node.children.get(token)
            if child is None:
                break
            node = child
            length += 1
        return length


_MISSING = object()


def _is_sim_object(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and "cxx_class" in value
        and "path" in value
        and "type" in value
    )


def _is_sim_object_vector(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(_is_sim_object(item) for item in value)
    )


_TRAILING_NUMBER = re.compile(r"(\d+)$")


def _unpadded(name: str) -> str:
    """`system_links01` -> `system_links1`; other names are unchanged."""
    return _TRAILING_NUMBER.sub(lambda m: str(int(m.group(1))), name)


def _build(
    config: Dict[str, Any], parent: Optional[SimObject], path: str
) -> SimObject:
    params = {}
    children = []
    for key, value in config.items():
        if _is_sim_object(value):
            children.append((value, False))
        elif _is_sim_object_vector(value):
            children.extend((item, True) for item in value)
        else:
            params[key] = value
    name = config.get("name") or ""
    obj = SimObject(
        name, path, config["type"], config["cxx_class"], params, parent
    )
    for child, in_vector in children:
        child_name = child["name"]
        child_path = f"{path}.{child_name}" if path else child_name
        if child.get("path") != child_path:
            raise ValueError(
                f"config.json path mismatch: expected '{child_path}', "
                f"found '{child.get('path')}'."
            )
        child_obj = _build(child, obj, child_path)
        obj.children[child_name] = child_obj
        alias = _unpadded(child_name)
        if in_vector and alias != child_name:
            obj.children.setdefault(alias, child_obj)
    return obj
