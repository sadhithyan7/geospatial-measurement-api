"""Test fixtures.

Each test module runs against its own throwaway SQLite file. The environment
variables are set before `app` is imported, because `app.config` resolves
settings at import time.
"""
import os
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="geoapi-tests-")
os.environ["DATA_DIR"] = TMP
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def kml_file():
    return SAMPLES / "sample_survey.kml"


@pytest.fixture
def shapefile_zip():
    return SAMPLES / "sample_parcels_shapefile.zip"


@pytest.fixture
def shapefile_no_prj():
    return SAMPLES / "sample_no_prj.zip"


def upload(client, path: Path):
    with open(path, "rb") as fh:
        return client.post(
            "/api/files/", files={"file": (path.name, fh, "application/octet-stream")}
        )
