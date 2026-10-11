"""Geometry measurement.

Measurements are always computed on a geometry that has already been
reprojected into a metre-based CRS by `app.services.crs`. This module never
sees degrees, which is what keeps the unit conversions below honest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from shapely.geometry.base import BaseGeometry

SQ_M_PER_HECTARE = 10_000.0
SQ_M_PER_SQ_KM = 1_000_000.0
SQ_M_PER_ACRE = 4046.8564224  # international acre, exact by definition


class MeasurementStatus(str, Enum):
    """Per-feature outcome, so one bad geometry cannot fail a whole file."""

    MEASURED = "MEASURED"          # area or length computed
    NOT_APPLICABLE = "NOT_APPLICABLE"  # valid geometry with nothing to measure (Point)
    UNSUPPORTED = "UNSUPPORTED"    # geometry type we do not measure
    FAILED = "FAILED"              # geometry was malformed or projection failed


AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString", "LinearRing"}
POINT_TYPES = {"Point", "MultiPoint"}


@dataclass
class Measurement:
    """Result of measuring a single feature."""

    status: MeasurementStatus
    geometry_type: str
    area: dict[str, float] | None = None
    length: dict[str, float] | None = None
    projected_crs: dict[str, str] | None = None
    note: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        payload: dict = {
            "status": self.status.value,
            "geometry_type": self.geometry_type,
            "area": self.area,
            "length": self.length,
            "projected_crs": self.projected_crs,
        }
        if self.note:
            payload["note"] = self.note
        if self.warnings:
            payload["warnings"] = self.warnings
        return payload


def _area_payload(square_metres: float) -> dict[str, float]:
    return {
        "square_metres": round(square_metres, 4),
        "hectares": round(square_metres / SQ_M_PER_HECTARE, 6),
        "square_kilometres": round(square_metres / SQ_M_PER_SQ_KM, 8),
        "acres": round(square_metres / SQ_M_PER_ACRE, 6),
    }


def _length_payload(metres: float) -> dict[str, float]:
    return {
        "metres": round(metres, 4),
        "kilometres": round(metres / 1000.0, 7),
    }


def measure(projected: BaseGeometry, projection: dict[str, str]) -> Measurement:
    """Measure an already-projected geometry.

    Polygons yield area plus perimeter; lines yield length; points yield
    nothing. Anything else is reported as unsupported rather than guessed at.
    """
    geom_type = projected.geom_type
    warnings: list[str] = []

    if projected.is_empty:
        return Measurement(
            status=MeasurementStatus.NOT_APPLICABLE,
            geometry_type=geom_type,
            projected_crs=projection,
            note="Geometry is empty; there is nothing to measure.",
        )

    if geom_type in AREA_TYPES:
        # Self-intersecting ("bowtie") polygons are common in hand-digitised
        # survey data. Shapely still returns a number for them, but it is not
        # the area a surveyor means, so the result is flagged rather than
        # silently repaired -- repairing would change the user's data.
        if not projected.is_valid:
            warnings.append(
                "Geometry is not OGC-valid (likely self-intersecting); the "
                "reported area follows the shoelace formula over the rings as "
                "given and may not match the intended footprint."
            )
        return Measurement(
            status=MeasurementStatus.MEASURED,
            geometry_type=geom_type,
            area=_area_payload(projected.area),
            length=_length_payload(projected.length),  # perimeter
            projected_crs=projection,
            note="`length` is the perimeter of the polygon boundary.",
            warnings=warnings,
        )

    if geom_type in LENGTH_TYPES:
        return Measurement(
            status=MeasurementStatus.MEASURED,
            geometry_type=geom_type,
            length=_length_payload(projected.length),
            projected_crs=projection,
            warnings=warnings,
        )

    if geom_type in POINT_TYPES:
        return Measurement(
            status=MeasurementStatus.NOT_APPLICABLE,
            geometry_type=geom_type,
            projected_crs=projection,
            note="Points have neither area nor length.",
        )

    if geom_type == "GeometryCollection":
        # A collection can mix polygons and lines. Both totals are reported so
        # the caller keeps the full picture instead of an arbitrary pick.
        area = sum(g.area for g in projected.geoms if g.geom_type in AREA_TYPES)
        length = sum(
            g.length for g in projected.geoms if g.geom_type in LENGTH_TYPES
        )
        return Measurement(
            status=MeasurementStatus.MEASURED,
            geometry_type=geom_type,
            area=_area_payload(area) if area > 0 else None,
            length=_length_payload(length) if length > 0 else None,
            projected_crs=projection,
            note=(
                "Mixed collection: area summed over polygon members, length "
                "summed over line members."
            ),
        )

    return Measurement(
        status=MeasurementStatus.UNSUPPORTED,
        geometry_type=geom_type,
        projected_crs=projection,
        note=f"Geometry type '{geom_type}' has no defined measurement here.",
    )
