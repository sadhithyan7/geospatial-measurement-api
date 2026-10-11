"""KML parser.

KML is XML. It is parsed here with the standard library's ElementTree rather
than a GIS driver, for three reasons:

1. The OGC KML specification fixes the CRS at WGS 84 (EPSG:4326) -- there is
   no projection to negotiate, which removes most of the complexity a general
   driver exists to handle.
2. It keeps the dependency set small enough to deploy on a serverless
   platform.
3. Coordinate parsing is explicit and testable, instead of hidden behind GDAL.

`defusedxml` is used when available to neutralise XML entity-expansion
attacks ("billion laughs"); Python's own parser already disables external
entity resolution, so the stdlib path remains safe for untrusted input.
"""
from __future__ import annotations

from pathlib import Path

from pyproj import CRS
from shapely.geometry import (
    LinearRing,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.collection import GeometryCollection

from app.config import settings

from .base import ParsedDataset, ParsedFeature, ParseError

try:  # pragma: no cover - import-time branch
    from defusedxml import ElementTree as ET  # type: ignore
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET  # type: ignore

# KML files appear in the wild under both the 2.2 OGC namespace and the older
# Google namespace, and some exports carry none at all. Rather than matching
# on a fixed namespace, tags are compared by local name.
GX_NAMESPACE = "http://www.google.com/kml/ext/2.2"


def _local(tag: str) -> str:
    """Strip any XML namespace, returning the bare tag name."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_all(element, name: str):
    """Depth-first search for descendants whose local name matches `name`."""
    for child in element.iter():
        if _local(child.tag) == name:
            yield child


def _first_child(element, name: str):
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def parse_coordinates(text: str | None) -> list[tuple[float, float]]:
    """Parse a KML <coordinates> block into (lon, lat) tuples.

    The KML grammar is whitespace-separated tuples of `lon,lat[,altitude]`.
    Real files are messier than the grammar: they carry newlines and tabs
    between tuples, trailing commas, and occasional empty tokens. Altitude is
    read and discarded because every measurement here is planar -- a slope
    distance would need a terrain model the service does not have.
    """
    if not text:
        return []

    points: list[tuple[float, float]] = []
    for token in text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        points.append((lon, lat))
    return points


def _build_polygon(element):
    """Assemble a Polygon from its outer boundary and any inner holes."""
    outer_el = next(_find_all(element, "outerBoundaryIs"), None)
    if outer_el is None:
        return None

    outer_coords_el = next(_find_all(outer_el, "coordinates"), None)
    shell = parse_coordinates(outer_coords_el.text if outer_coords_el is not None else None)
    if len(shell) < 3:
        return None

    holes: list[list[tuple[float, float]]] = []
    for inner_el in _find_all(element, "innerBoundaryIs"):
        coords_el = next(_find_all(inner_el, "coordinates"), None)
        ring = parse_coordinates(coords_el.text if coords_el is not None else None)
        if len(ring) >= 3:
            holes.append(ring)

    return Polygon(shell, holes)


def _build_geometry(element):
    """Convert one KML geometry element into a Shapely geometry."""
    name = _local(element.tag)

    if name == "Point":
        coords_el = next(_find_all(element, "coordinates"), None)
        pts = parse_coordinates(coords_el.text if coords_el is not None else None)
        return Point(pts[0]) if pts else None

    if name in {"LineString", "LinearRing"}:
        coords_el = next(_find_all(element, "coordinates"), None)
        pts = parse_coordinates(coords_el.text if coords_el is not None else None)
        if len(pts) < 2:
            return None
        return LinearRing(pts) if name == "LinearRing" and len(pts) >= 3 else LineString(pts)

    if name == "Polygon":
        return _build_polygon(element)

    if name == "MultiGeometry":
        # A MultiGeometry may hold one type or several. Uniform polygon sets
        # become a MultiPolygon; mixed sets become a GeometryCollection so no
        # member is silently dropped.
        parts = []
        for child in element:
            geom = _build_geometry(child)
            if geom is not None and not geom.is_empty:
                parts.append(geom)
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        if all(p.geom_type == "Polygon" for p in parts):
            return MultiPolygon(parts)
        return GeometryCollection(parts)

    if name == "Track" or element.tag == f"{{{GX_NAMESPACE}}}Track":
        # gx:Track stores coordinates in repeated <gx:coord> elements with
        # space separators rather than a single comma-separated block.
        pts: list[tuple[float, float]] = []
        for coord in _find_all(element, "coord"):
            if not coord.text:
                continue
            bits = coord.text.split()
            if len(bits) >= 2:
                try:
                    pts.append((float(bits[0]), float(bits[1])))
                except ValueError:
                    continue
        return LineString(pts) if len(pts) >= 2 else None

    return None


GEOMETRY_TAGS = {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry", "Track"}


def _extract_properties(placemark) -> dict:
    """Pull name, description and ExtendedData fields off a Placemark."""
    properties: dict = {}

    for key in ("name", "description", "address", "phoneNumber"):
        node = _first_child(placemark, key)
        if node is not None and node.text:
            properties[key] = node.text.strip()

    for data in _find_all(placemark, "Data"):
        key = data.get("name")
        value_el = next(_find_all(data, "value"), None)
        if key:
            properties[key] = (value_el.text or "").strip() if value_el is not None else None

    for simple in _find_all(placemark, "SimpleData"):
        key = simple.get("name")
        if key:
            properties[key] = (simple.text or "").strip()

    return properties


class KMLParser:
    """Reads a .kml file into a `ParsedDataset`."""

    source_format = "kml"

    def parse(self, path: str) -> ParsedDataset:
        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except Exception as exc:
            raise ParseError(f"File is not well-formed XML/KML: {exc}") from exc

        placemarks = list(_find_all(root, "Placemark"))
        warnings: list[str] = []

        if not placemarks:
            raise ParseError(
                "No <Placemark> elements found. If this is a KMZ archive, "
                "unzip it and upload the .kml inside."
            )

        features: list[ParsedFeature] = []
        for index, placemark in enumerate(placemarks):
            if index >= settings.MAX_FEATURES:
                warnings.append(
                    f"File contains more than {settings.MAX_FEATURES} placemarks; "
                    "processing was truncated at that limit."
                )
                break

            properties = _extract_properties(placemark)

            geometry_el = None
            for child in placemark:
                if _local(child.tag) in GEOMETRY_TAGS:
                    geometry_el = child
                    break

            if geometry_el is None:
                features.append(
                    ParsedFeature(
                        index=index,
                        geometry=None,
                        properties=properties,
                        error="Placemark carries no supported geometry element.",
                    )
                )
                continue

            try:
                geometry = _build_geometry(geometry_el)
                if geometry is None or geometry.is_empty:
                    features.append(
                        ParsedFeature(
                            index=index,
                            geometry=None,
                            properties=properties,
                            error=(
                                f"<{_local(geometry_el.tag)}> held too few valid "
                                "coordinates to form a geometry."
                            ),
                        )
                    )
                else:
                    features.append(
                        ParsedFeature(index=index, geometry=geometry, properties=properties)
                    )
            except Exception as exc:
                features.append(
                    ParsedFeature(
                        index=index,
                        geometry=None,
                        properties=properties,
                        error=f"Could not build geometry: {exc}",
                    )
                )

        doc_name_el = next(_find_all(root, "name"), None)

        return ParsedDataset(
            # Fixed by the OGC KML specification; KML has no other CRS.
            crs=CRS.from_epsg(4326),
            features=features,
            layer_name=(doc_name_el.text.strip() if doc_name_el is not None and doc_name_el.text else Path(path).stem),
            source_format=self.source_format,
            warnings=warnings,
        )
