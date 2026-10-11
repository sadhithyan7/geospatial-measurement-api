"""Shared types for geospatial file parsers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol

from pyproj import CRS
from shapely.geometry.base import BaseGeometry


class ParseError(Exception):
    """Raised when a file cannot be read as the format it claims to be.

    Carries a message intended for the API caller, so parser failures surface
    as actionable 422 responses instead of leaking tracebacks.
    """


@dataclass
class ParsedFeature:
    """One feature lifted out of a source file, before measurement."""

    index: int
    geometry: BaseGeometry | None
    properties: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class ParsedDataset:
    """Everything a parser extracts from one uploaded file."""

    crs: CRS | None
    features: list[ParsedFeature]
    layer_name: str | None = None
    source_format: str = "unknown"
    warnings: list[str] = field(default_factory=list)


class Parser(Protocol):
    """Structural interface every format parser satisfies.

    Adding GeoJSON or GeoPackage support later means writing one more class
    with this shape and registering it -- no change to the processing or API
    layers.
    """

    def parse(self, path: str) -> ParsedDataset: ...
