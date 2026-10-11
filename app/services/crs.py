"""Coordinate Reference System handling.

The central correctness problem in this service
--------------------------------------------------
Geographic CRSs such as EPSG:4326 (WGS 84) store coordinates as angles --
degrees of longitude and latitude -- not as lengths. Running a planar area
or distance formula over degrees produces a number in "square degrees",
which is not a unit of area: one degree of longitude is ~111 km at the
equator and 0 km at the poles, so the same shape yields different answers
depending on where on Earth it sits.

The fix is to reproject each geometry into a *projected* CRS whose units are
metres before measuring. This module picks that CRS and performs the
transform.

Strategy: per-feature UTM zone selection
----------------------------------------
UTM divides the world into 60 north-south zones, each 6 degrees of longitude
wide, and each zone has its own projection tuned to minimise distortion
inside it (scale error stays within ~1 part in 2500). We derive the zone
from each geometry's own centroid, so a file spanning several zones still
measures every feature in the projection that best fits that feature.

Alternatives considered are documented in docs/PROJECT_WALKTHROUGH.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely import transform as shapely_transform
from shapely.geometry.base import BaseGeometry

# UTM is only defined between 80 degrees S and 84 degrees N. Beyond that the
# zones converge and distortion explodes, so the Universal Polar Stereographic
# system is the correct choice instead.
UTM_MIN_LATITUDE = -80.0
UTM_MAX_LATITUDE = 84.0

EPSG_UPS_NORTH = 5041
EPSG_UPS_SOUTH = 5042

WGS84 = CRS.from_epsg(4326)


@dataclass(frozen=True)
class ProjectionChoice:
    """Which projected CRS was used for a measurement, and why."""

    epsg: int
    name: str
    reason: str

    def as_dict(self) -> dict:
        return {"epsg": f"EPSG:{self.epsg}", "name": self.name, "reason": self.reason}


def utm_zone_from_longitude(longitude: float) -> int:
    """Return the 1-60 UTM zone number containing `longitude`.

    Zone 1 starts at 180 degrees W and each zone spans 6 degrees. Longitudes
    are normalised into [-180, 180) first so that values such as 185 or -190,
    which appear in real-world data that has wrapped the antimeridian, still
    resolve to a valid zone instead of raising.
    """
    normalised = ((longitude + 180.0) % 360.0) - 180.0
    zone = int(math.floor((normalised + 180.0) / 6.0)) + 1
    # Guard the boundary case where longitude == 180 exactly.
    return min(max(zone, 1), 60)


def projection_for(longitude: float, latitude: float) -> ProjectionChoice:
    """Pick the metre-based CRS best suited to a point on the globe."""
    if latitude > UTM_MAX_LATITUDE:
        return ProjectionChoice(
            epsg=EPSG_UPS_NORTH,
            name="WGS 84 / UPS North (E,N)",
            reason=(
                f"Latitude {latitude:.4f} is above the UTM limit of "
                f"{UTM_MAX_LATITUDE}; Universal Polar Stereographic used instead."
            ),
        )
    if latitude < UTM_MIN_LATITUDE:
        return ProjectionChoice(
            epsg=EPSG_UPS_SOUTH,
            name="WGS 84 / UPS South (E,N)",
            reason=(
                f"Latitude {latitude:.4f} is below the UTM limit of "
                f"{UTM_MIN_LATITUDE}; Universal Polar Stereographic used instead."
            ),
        )

    zone = utm_zone_from_longitude(longitude)
    hemisphere = "North" if latitude >= 0 else "South"
    # EPSG assigns 326xx to northern UTM zones and 327xx to southern ones.
    epsg = (32600 if latitude >= 0 else 32700) + zone
    return ProjectionChoice(
        epsg=epsg,
        name=f"WGS 84 / UTM zone {zone}{'N' if latitude >= 0 else 'S'}",
        reason=(
            f"Centroid at lon {longitude:.4f}, lat {latitude:.4f} falls in UTM "
            f"zone {zone} ({hemisphere}ern hemisphere)."
        ),
    )


@lru_cache(maxsize=256)
def _transformer(source_srs: str, target_epsg: int) -> Transformer:
    """Build and cache a Transformer.

    Transformer construction is the expensive part of a reprojection because
    pyproj has to look up and compile the transformation pipeline. Files
    routinely contain thousands of features sharing one source CRS and one
    target zone, so caching turns N pipeline builds into one.

    `always_xy=True` forces longitude/latitude (x, y) ordering. Without it
    pyproj honours the authority's axis order, which for EPSG:4326 is
    latitude-first, and every coordinate silently comes out swapped.
    """
    return Transformer.from_crs(
        CRS.from_user_input(source_srs), CRS.from_epsg(target_epsg), always_xy=True
    )


def _reproject(geometry: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    """Apply a pyproj Transformer to every coordinate in a geometry.

    Shapely 2's `transform` hands the callback the whole coordinate array at
    once, so pyproj reprojects the geometry in a single vectorised call
    rather than once per vertex. On a survey polygon with thousands of
    vertices that is the difference between one PROJ call and thousands.
    """

    def _apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely_transform(geometry, _apply)


def parse_crs(value: str | int | None) -> CRS | None:
    """Best-effort CRS parse from WKT, PROJ string, EPSG code, or auth name."""
    if value is None or value == "":
        return None
    try:
        return CRS.from_user_input(value)
    except (CRSError, ValueError, TypeError):
        return None


def describe_crs(crs: CRS | None) -> str:
    """Human and machine friendly CRS label, e.g. 'EPSG:4326'."""
    if crs is None:
        return "UNKNOWN"
    epsg = crs.to_epsg()
    if epsg:
        return f"EPSG:{epsg}"
    return crs.name or "UNKNOWN"


def to_wgs84(geometry: BaseGeometry, source: CRS) -> BaseGeometry:
    """Reproject a geometry into EPSG:4326.

    Used to find a geometry's position on the globe before choosing a UTM
    zone, so that projected source files (which have no degrees of their own)
    can still be zoned correctly.
    """
    if source.equals(WGS84):
        return geometry
    return _reproject(geometry, _transformer(source.to_wkt(), 4326))


def project_for_measurement(
    geometry: BaseGeometry, source: CRS
) -> tuple[BaseGeometry, ProjectionChoice]:
    """Reproject `geometry` into a metre-based CRS chosen for its location.

    Returns the projected geometry together with the choice that was made, so
    that the API can report exactly which projection produced each number
    rather than asking the caller to trust an opaque result.
    """
    if source.is_projected and _axis_is_metre(source):
        epsg = source.to_epsg()
        return geometry, ProjectionChoice(
            epsg=epsg or 0,
            name=source.name,
            reason=(
                "Source CRS is already projected with metre units; measured "
                "in place to avoid a needless reprojection and its rounding."
            ),
        )

    # Locate the geometry on the globe, then pick the zone that fits it.
    geographic = to_wgs84(geometry, source)
    centroid = geographic.centroid
    if centroid.is_empty:
        raise ValueError("Cannot determine a projection for an empty geometry.")

    choice = projection_for(centroid.x, centroid.y)
    transformer = _transformer(source.to_wkt(), choice.epsg)
    return _reproject(geometry, transformer), choice


def _axis_is_metre(crs: CRS) -> bool:
    """True when every axis of a projected CRS is measured in metres."""
    try:
        units = {axis.unit_name.lower() for axis in crs.axis_info}
    except Exception:  # pragma: no cover - defensive, pyproj shape varies
        return False
    return bool(units) and units.issubset({"metre", "meter", "m"})
