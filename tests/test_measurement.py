"""Tests for measurement correctness and geometry-type handling."""
import pytest
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection, LineString, MultiPolygon, Point, Polygon,
)

from app.services.crs import project_for_measurement
from app.services.measurement import MeasurementStatus, measure

WGS84 = CRS.from_epsg(4326)


def measure_wgs84(geometry):
    projected, choice = project_for_measurement(geometry, WGS84)
    return measure(projected, choice.as_dict())


def test_one_square_kilometre_measures_as_one_square_kilometre():
    """Ground truth: a box sized to 1 km a side must come back ~1,000,000 m2.

    A 1% tolerance absorbs UTM's scale factor, which is by design slightly
    under 1 at the centre of a zone.
    """
    lat = 12.97
    d_lat = 1000 / 110_574          # metres per degree of latitude
    d_lon = 1000 / 108_477          # metres per degree of longitude at 12.97N
    box = Polygon([
        (77.5, lat), (77.5 + d_lon, lat),
        (77.5 + d_lon, lat + d_lat), (77.5, lat + d_lat),
    ])
    result = measure_wgs84(box)
    assert result.status is MeasurementStatus.MEASURED
    assert result.area["square_metres"] == pytest.approx(1_000_000, rel=0.01)
    assert result.area["hectares"] == pytest.approx(100, rel=0.01)
    assert result.length["metres"] == pytest.approx(4_000, rel=0.01)  # perimeter


def test_one_degree_at_the_equator_is_about_111_km():
    line = LineString([(77.5, 0.0), (78.5, 0.0)])
    result = measure_wgs84(line)
    assert result.status is MeasurementStatus.MEASURED
    assert result.length["kilometres"] == pytest.approx(111.3, rel=0.01)


def test_polygon_hole_is_subtracted():
    outer = [(77.5, 12.9), (77.51, 12.9), (77.51, 12.91), (77.5, 12.91)]
    hole = [(77.502, 12.902), (77.508, 12.902), (77.508, 12.908), (77.502, 12.908)]
    solid = measure_wgs84(Polygon(outer))
    holed = measure_wgs84(Polygon(outer, [hole]))
    assert holed.area["square_metres"] < solid.area["square_metres"]
    assert holed.area["square_metres"] == pytest.approx(
        solid.area["square_metres"] * 0.64, rel=0.05
    )


def test_point_has_no_measurement():
    result = measure_wgs84(Point(77.5, 12.9))
    assert result.status is MeasurementStatus.NOT_APPLICABLE
    assert result.area is None and result.length is None


def test_multipolygon_areas_are_summed():
    a = Polygon([(77.5, 12.9), (77.51, 12.9), (77.51, 12.91), (77.5, 12.91)])
    b = Polygon([(77.6, 12.9), (77.61, 12.9), (77.61, 12.91), (77.6, 12.91)])
    combined = measure_wgs84(MultiPolygon([a, b]))
    single = measure_wgs84(a)
    assert combined.area["square_metres"] == pytest.approx(
        single.area["square_metres"] * 2, rel=0.02
    )


def test_invalid_polygon_is_measured_but_flagged():
    """A self-intersecting polygon returns a number plus an explicit warning."""
    bowtie = Polygon([(77.5, 12.9), (77.6, 13.0), (77.5, 13.0), (77.6, 12.9)])
    result = measure_wgs84(bowtie)
    assert result.status is MeasurementStatus.MEASURED
    assert result.warnings and "not OGC-valid" in result.warnings[0]


def test_geometry_collection_reports_both_totals():
    poly = Polygon([(77.5, 12.9), (77.51, 12.9), (77.51, 12.91), (77.5, 12.91)])
    line = LineString([(77.6, 12.9), (77.62, 12.9)])
    result = measure_wgs84(GeometryCollection([poly, line]))
    assert result.status is MeasurementStatus.MEASURED
    assert result.area["square_metres"] > 0
    assert result.length["metres"] > 0


def test_empty_geometry_is_not_applicable():
    projected, choice = project_for_measurement(
        Polygon([(77.5, 12.9), (77.51, 12.9), (77.51, 12.91)]), WGS84
    )
    result = measure(projected.intersection(Point(0, 0).buffer(0)), choice.as_dict())
    assert result.status is MeasurementStatus.NOT_APPLICABLE


def test_unit_conversions_are_consistent():
    box = Polygon([(77.5, 12.9), (77.51, 12.9), (77.51, 12.91), (77.5, 12.91)])
    area = measure_wgs84(box).area
    assert area["hectares"] == pytest.approx(area["square_metres"] / 10_000, rel=1e-6)
    assert area["acres"] == pytest.approx(
        area["square_metres"] / 4046.8564224, rel=1e-6
    )
