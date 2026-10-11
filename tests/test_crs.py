"""Tests for CRS selection and reprojection.

These are the highest-value tests in the suite: a wrong UTM zone produces a
plausible-looking but wrong number, which no amount of API testing catches.
"""
import math

import pytest
from pyproj import CRS
from shapely.geometry import LineString, Polygon

from app.services.crs import (
    describe_crs,
    parse_crs,
    project_for_measurement,
    projection_for,
    utm_zone_from_longitude,
)

WGS84 = CRS.from_epsg(4326)


@pytest.mark.parametrize(
    "longitude,expected_zone",
    [
        (-180.0, 1), (-177.0, 1), (-174.1, 1), (-173.9, 2),
        (-0.12, 30), (0.0, 31), (77.59, 43),
        (179.9, 60), (180.0, 1),   # 180 wraps back to zone 1
        (185.0, 1),                # == -175, normalised instead of crashing
        (-190.0, 59),              # == +170, which sits in zone 59
    ],
)
def test_utm_zone_from_longitude(longitude, expected_zone):
    assert utm_zone_from_longitude(longitude) == expected_zone


@pytest.mark.parametrize(
    "lon,lat,expected_epsg",
    [
        (77.59, 12.97, 32643),   # Bengaluru -> zone 43 North
        (-74.00, 40.71, 32618),  # New York  -> zone 18 North
        (151.21, -33.87, 32756), # Sydney    -> zone 56 South
        (0.0, 0.0, 32631),       # null island, northern by convention
        (10.0, 85.0, 5041),      # above 84N -> UPS North
        (10.0, -85.0, 5042),     # below 80S -> UPS South
    ],
)
def test_projection_selection(lon, lat, expected_epsg):
    assert projection_for(lon, lat).epsg == expected_epsg


def test_southern_hemisphere_uses_327xx_band():
    assert projection_for(20.0, -10.0).epsg == 32734
    assert projection_for(20.0, 10.0).epsg == 32634


def test_projection_choice_explains_itself():
    choice = projection_for(77.59, 12.97)
    assert "zone 43" in choice.reason
    assert "43N" in choice.name


def test_already_projected_crs_is_not_reprojected():
    """A source already in metres should be measured where it stands."""
    utm = CRS.from_epsg(32643)
    square = Polygon([(500000, 1000000), (500100, 1000000),
                      (500100, 1000100), (500000, 1000100)])
    projected, choice = project_for_measurement(square, utm)
    assert projected.equals(square)
    assert choice.epsg == 32643
    assert "already projected" in choice.reason


def test_degrees_are_never_measured_directly():
    """The core guarantee: a 4326 polygon must not be measured in degrees.

    In degrees this square has an 'area' of 0.0001. If that number ever comes
    back, reprojection has silently stopped happening.
    """
    square = Polygon([(77.5, 12.9), (77.51, 12.9), (77.51, 12.91), (77.5, 12.91)])
    projected, _ = project_for_measurement(square, WGS84)
    assert projected.area > 1_000_000 * 0.5
    assert not math.isclose(projected.area, 0.0001, rel_tol=0.5)


def test_parse_crs_accepts_several_forms():
    assert parse_crs(4326).to_epsg() == 4326
    assert parse_crs("EPSG:32643").to_epsg() == 32643
    assert parse_crs(WGS84.to_wkt()).to_epsg() == 4326


def test_parse_crs_returns_none_for_junk():
    assert parse_crs("not a crs at all") is None
    assert parse_crs(None) is None
    assert parse_crs("") is None


def test_describe_crs():
    assert describe_crs(WGS84) == "EPSG:4326"
    assert describe_crs(None) == "UNKNOWN"


def test_line_crossing_zones_still_measures():
    """A line spanning two UTM zones is measured, using its centroid's zone."""
    line = LineString([(76.0, 12.0), (80.0, 12.0)])
    projected, choice = project_for_measurement(line, WGS84)
    assert projected.length > 400_000
    assert choice.epsg in (32643, 32644)
