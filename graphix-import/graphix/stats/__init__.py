"""Stat classes; `KINDS` maps a block kind to the class that holds it."""

from .base import Index, Stat
from .histogram import Buckets, Histogram, merge_buckets
from .ruby import ControllerCount
from .scalar import Scalar
from .vector import Vector

KINDS = {
    "scalar": Scalar,
    "vector": Vector,
    "histogram": Histogram,
    "ruby_controller": ControllerCount,
}

__all__ = [
    "Buckets",
    "ControllerCount",
    "Histogram",
    "Index",
    "KINDS",
    "Scalar",
    "Stat",
    "Vector",
    "merge_buckets",
]
