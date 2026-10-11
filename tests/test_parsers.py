"""Tests for the file parsers, including malformed input."""
import zipfile

import pytest

from app.services.parsers.base import ParseError
from app.services.parsers.kml_parser import KMLParser, parse_coordinates
from app.services.parsers.shapefile_parser import ShapefileParser, safe_extract
from app.services.processor import select_parser


# ---------- KML coordinate parsing ----------

def test_parse_coordinates_handles_altitude_and_whitespace():
    text = "77.5,12.9,0 77.6,12.9,0\n   77.6,13.0,120.5\t"
    assert parse_coordinates(text) == [(77.5, 12.9), (77.6, 12.9), (77.6, 13.0)]


def test_parse_coordinates_without_altitude():
    assert parse_coordinates("1.0,2.0 3.0,4.0") == [(1.0, 2.0), (3.0, 4.0)]


@pytest.mark.parametrize("junk", [None, "", "   ", "garbage", "1.0", "a,b"])
def test_parse_coordinates_rejects_junk(junk):
    assert parse_coordinates(junk) == []


# ---------- KML documents ----------

def test_kml_reads_every_geometry_type(kml_file):
    dataset = KMLParser().parse(str(kml_file))
    assert dataset.crs.to_epsg() == 4326  # fixed by the KML specification
    types = [f.geometry.geom_type for f in dataset.features if f.geometry]
    assert "Polygon" in types
    assert "LineString" in types
    assert "Point" in types
    assert "GeometryCollection" in types


def test_kml_keeps_extended_data_as_properties(kml_file):
    dataset = KMLParser().parse(str(kml_file))
    first = dataset.features[0]
    assert first.properties["name"] == "Pit Boundary A"
    assert first.properties["site_code"] == "PIT-A"


def test_kml_records_a_bad_placemark_without_losing_the_others(kml_file):
    """One unusable placemark must not cost us the five good ones."""
    dataset = KMLParser().parse(str(kml_file))
    broken = [f for f in dataset.features if f.geometry is None]
    good = [f for f in dataset.features if f.geometry is not None]
    assert len(broken) == 1
    assert len(good) >= 4
    assert "too few valid coordinates" in broken[0].error


def test_kml_rejects_non_xml(tmp_path):
    bad = tmp_path / "bad.kml"
    bad.write_text("this is definitely not xml")
    with pytest.raises(ParseError, match="not well-formed"):
        KMLParser().parse(str(bad))


def test_kml_without_placemarks_is_rejected(tmp_path):
    empty = tmp_path / "empty.kml"
    empty.write_text('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>')
    with pytest.raises(ParseError, match="No <Placemark>"):
        KMLParser().parse(str(empty))


# ---------- Shapefiles ----------

def test_shapefile_reads_geometry_crs_and_attributes(shapefile_zip):
    dataset = ShapefileParser().parse(str(shapefile_zip))
    assert dataset.crs.to_epsg() == 4326
    assert len(dataset.features) == 3
    assert dataset.features[0].properties["parcel_id"] == "PCL-001"
    assert dataset.features[0].properties["owner"] == "State Mining Corp"
    assert dataset.warnings == []


def test_shapefile_without_prj_warns_and_falls_back(shapefile_no_prj):
    dataset = ShapefileParser().parse(str(shapefile_no_prj))
    assert dataset.crs is None  # undeclared; processor applies the fallback
    assert any("No .prj" in w for w in dataset.warnings)


def test_zip_without_a_shp_is_rejected(tmp_path):
    archive = tmp_path / "nothing.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("readme.txt", "no shapefile in here")
    with pytest.raises(ParseError, match="No .shp file"):
        ShapefileParser().parse(str(archive))


def test_corrupt_zip_is_rejected(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04 but not really")
    with pytest.raises(ParseError, match="not a readable .zip"):
        ShapefileParser().parse(str(bad))


# ---------- Archive safety ----------

def test_safe_extract_blocks_path_traversal(tmp_path):
    """A 'zip slip' entry must be refused, not written outside the target."""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../../escaped.txt", "payload")
    with pytest.raises(ParseError, match="outside the extraction directory"):
        safe_extract(str(evil), str(tmp_path / "out"))
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_safe_extract_rejects_too_many_members(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MAX_ZIP_MEMBERS", 3)
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for i in range(5):
            z.writestr(f"f{i}.txt", "x")
    with pytest.raises(ParseError, match="above the limit"):
        safe_extract(str(archive), str(tmp_path / "out"))


def test_safe_extract_rejects_oversized_expansion(tmp_path, monkeypatch):
    """Guards the zip-bomb case: small archive, enormous expanded size."""
    from app.config import settings
    monkeypatch.setattr(settings, "MAX_UNCOMPRESSED_BYTES", 100)
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", "0" * 10_000)
    with pytest.raises(ParseError, match="expands to"):
        safe_extract(str(archive), str(tmp_path / "out"))


# ---------- Parser selection ----------

@pytest.mark.parametrize("name,expected", [
    ("survey.kml", "kml"), ("SURVEY.KML", "kml"),
    ("parcels.zip", "shapefile"), ("Parcels.ZIP", "shapefile"),
])
def test_select_parser(name, expected):
    assert select_parser(name).source_format == expected


@pytest.mark.parametrize("name", ["data.geojson", "notes.txt", "noextension"])
def test_select_parser_rejects_unsupported(name):
    with pytest.raises(ParseError, match="Unsupported file type"):
        select_parser(name)
