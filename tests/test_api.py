"""End-to-end API tests."""
import io
import zipfile

import pytest

from tests.conftest import upload


# ---------- happy paths ----------

def test_upload_kml_returns_a_processed_summary(client, kml_file):
    response = upload(client, kml_file)
    assert response.status_code == 201
    body = response.json()
    assert body["crs"] == "EPSG:4326"
    assert body["source_format"] == "kml"
    assert body["feature_count"] == 6
    assert body["measured_count"] == 4
    assert body["failed_count"] == 1          # the deliberately broken placemark
    assert body["totals"]["area_square_metres"] > 0
    assert body["status"] == "COMPLETED_WITH_ERRORS"


def test_upload_shapefile_returns_a_clean_summary(client, shapefile_zip):
    body = upload(client, shapefile_zip).json()
    assert body["status"] == "COMPLETED"
    assert body["source_format"] == "shapefile"
    assert body["feature_count"] == 3
    assert body["measured_count"] == 3
    assert body["failed_count"] == 0
    assert body["warnings"] == []


def test_missing_prj_is_reported_as_a_warning_not_a_failure(client, shapefile_no_prj):
    body = upload(client, shapefile_no_prj).json()
    assert body["status"] == "COMPLETED"
    assert body["crs"] == "EPSG:4326"
    assert any("No .prj" in w for w in body["warnings"])


# ---------- measurements ----------

def test_measurements_carry_the_projection_used(client, kml_file):
    file_id = upload(client, kml_file).json()["id"]
    body = client.get(f"/api/files/{file_id}/measurements/").json()

    measured = [m for m in body["measurements"] if m["measurement_status"] == "MEASURED"]
    assert measured
    for m in measured:
        # Bengaluru data must land in UTM zone 43N, and say so.
        assert m["projected_crs"] == "EPSG:32643"
        assert "zone 43" in m["projection_reason"]


def test_polygons_get_area_lines_get_length_points_get_neither(client, kml_file):
    file_id = upload(client, kml_file).json()["id"]
    by_type = {
        m["geometry_type"]: m
        for m in client.get(f"/api/files/{file_id}/measurements/").json()["measurements"]
    }
    assert by_type["Polygon"]["area"]["square_metres"] > 0
    assert by_type["LineString"]["length"]["metres"] > 0
    assert by_type["LineString"]["area"] is None
    assert by_type["Point"]["measurement_status"] == "NOT_APPLICABLE"
    assert by_type["Point"]["area"] is None and by_type["Point"]["length"] is None


def test_source_attributes_survive_to_the_response(client, shapefile_zip):
    file_id = upload(client, shapefile_zip).json()["id"]
    first = client.get(f"/api/files/{file_id}/measurements/").json()["measurements"][0]
    assert first["properties"]["parcel_id"] == "PCL-001"
    assert first["properties"]["land_use"] == "Excavation"


def test_measurements_paginate(client, kml_file):
    file_id = upload(client, kml_file).json()["id"]
    page = client.get(f"/api/files/{file_id}/measurements/?limit=2&offset=1").json()
    assert page["returned"] == 2
    assert page["offset"] == 1
    assert page["measurements"][0]["feature_index"] == 1


def test_measurements_filter_by_geometry_type(client, kml_file):
    file_id = upload(client, kml_file).json()["id"]
    body = client.get(f"/api/files/{file_id}/measurements/?geometry_type=Polygon").json()
    assert body["returned"] == 2
    assert {m["geometry_type"] for m in body["measurements"]} == {"Polygon"}


def test_measurements_filter_by_status(client, kml_file):
    file_id = upload(client, kml_file).json()["id"]
    body = client.get(
        f"/api/files/{file_id}/measurements/?measurement_status=FAILED"
    ).json()
    assert body["returned"] == 1
    assert body["measurements"][0]["error"]


# ---------- failure handling ----------

def test_one_bad_feature_does_not_sink_the_file(client, kml_file):
    """The central resilience guarantee, asserted end to end."""
    file_id = upload(client, kml_file).json()["id"]
    body = client.get(f"/api/files/{file_id}/measurements/").json()
    failed = [m for m in body["measurements"] if m["measurement_status"] == "FAILED"]
    measured = [m for m in body["measurements"] if m["measurement_status"] == "MEASURED"]
    assert len(failed) == 1
    assert failed[0]["error"]
    assert len(measured) == 4   # the rest still came back with numbers


@pytest.mark.parametrize("name,content", [
    ("notes.txt", b"plain text"),
    ("data.geojson", b'{"type":"FeatureCollection","features":[]}'),
])
def test_unsupported_extensions_are_rejected_with_415(client, name, content):
    response = client.post("/api/files/", files={"file": (name, content, "text/plain")})
    assert response.status_code == 415
    assert "Unsupported file type" in response.json()["detail"]


def test_corrupt_zip_returns_422(client):
    response = client.post(
        "/api/files/", files={"file": ("x.zip", b"not a zip", "application/zip")}
    )
    assert response.status_code == 422
    assert "not a readable .zip" in response.json()["detail"]


def test_malformed_kml_returns_422(client):
    response = client.post(
        "/api/files/", files={"file": ("x.kml", b"<kml><unclosed>", "application/xml")}
    )
    assert response.status_code == 422


def test_zip_with_no_shapefile_returns_422(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("readme.txt", "nothing useful")
    buffer.seek(0)
    response = client.post(
        "/api/files/", files={"file": ("x.zip", buffer.read(), "application/zip")}
    )
    assert response.status_code == 422
    assert "No .shp" in response.json()["detail"]


def test_oversized_upload_is_rejected_with_413(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 512)
    response = client.post(
        "/api/files/", files={"file": ("big.kml", b"x" * 4096, "application/xml")}
    )
    assert response.status_code == 413


# ---------- retrieval, listing, deletion ----------

def test_get_file_by_id(client, kml_file):
    file_id = upload(client, kml_file).json()["id"]
    body = client.get(f"/api/files/{file_id}/").json()
    assert body["id"] == file_id
    assert body["filename"] == "sample_survey.kml"


def test_unknown_ids_return_404(client):
    assert client.get("/api/files/nope/").status_code == 404
    assert client.get("/api/files/nope/measurements/").status_code == 404
    assert client.get("/api/files/nope/geojson/").status_code == 404


def test_list_files(client, kml_file):
    upload(client, kml_file)
    body = client.get("/api/files/").json()
    assert body["count"] >= 1
    assert body["files"]


def test_geojson_export_is_valid_and_carries_measurements(client, shapefile_zip):
    file_id = upload(client, shapefile_zip).json()["id"]
    body = client.get(f"/api/files/{file_id}/geojson/").json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 3
    first = body["features"][0]
    assert first["geometry"]["type"] == "Polygon"
    assert first["properties"]["_area_sq_m"] > 0
    assert first["properties"]["_projected_crs"] == "EPSG:32643"


def test_delete_removes_the_file_and_its_features(client, kml_file):
    file_id = upload(client, kml_file).json()["id"]
    assert client.delete(f"/api/files/{file_id}/").status_code == 204
    assert client.get(f"/api/files/{file_id}/").status_code == 404


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_openapi_schema_is_generated(client):
    schema = client.get("/openapi.json").json()
    assert "/api/files/" in schema["paths"]
    assert "/api/files/{file_id}/measurements/" in schema["paths"]
