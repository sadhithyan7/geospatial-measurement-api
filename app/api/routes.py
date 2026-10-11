"""HTTP API routes."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_session
from app.models import Feature, FileStatus, UploadedFile
from app.schemas import (
    FeatureMeasurement,
    FileListResponse,
    FileSummary,
    MeasurementsResponse,
)
from app.services.processor import SUPPORTED_EXTENSIONS, process_file

router = APIRouter(prefix="/api", tags=["files"])


def _summary(record: UploadedFile) -> FileSummary:
    return FileSummary(
        id=record.id,
        filename=record.filename,
        source_format=record.source_format,
        size_bytes=record.size_bytes,
        status=record.status,
        crs=record.crs,
        layer_name=record.layer_name,
        feature_count=record.feature_count,
        measured_count=record.measured_count,
        failed_count=record.failed_count,
        totals={
            "area_square_metres": record.total_area_sq_m,
            "area_hectares": round(record.total_area_sq_m / 10_000, 6),
            "area_square_kilometres": round(record.total_area_sq_m / 1_000_000, 8),
            "length_metres": record.total_length_m,
            "length_kilometres": round(record.total_length_m / 1000, 7),
        },
        processing_ms=record.processing_ms,
        created_at=record.created_at,
        warnings=record.warnings or [],
        error=record.error,
    )


def _save_upload(upload: UploadFile, destination: Path) -> int:
    """Stream an upload to disk, aborting if it exceeds the size limit.

    The file is copied in chunks rather than read with `.read()` so that a
    large upload never has to fit in memory at once, and the running total is
    checked each chunk so an oversized body is rejected mid-stream instead of
    after it has all been accepted.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    chunk_size = 1024 * 1024

    with open(destination, "wb") as out:
        while True:
            chunk = upload.file.read(chunk_size)
            if not chunk:
                break
            written += len(chunk)
            if written > settings.MAX_UPLOAD_BYTES:
                out.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=(
                        f"File exceeds the maximum upload size of "
                        f"{settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
                    ),
                )
            out.write(chunk)

    return written


@router.post(
    "/files/",
    response_model=FileSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and process a geospatial file",
)
async def upload_file(
    file: UploadFile = File(..., description="A .kml file or a .zip containing a shapefile"),
    session: Session = Depends(get_session),
) -> FileSummary:
    """Accept a geospatial file, process it, and return a summary.

    Processing runs synchronously. For the file sizes this service targets
    (thousands of features, sub-second parsing) a synchronous request is
    simpler to operate and to reason about than a job queue, and it lets the
    caller get measurements in one round trip. The trade-off and the point at
    which it stops holding are documented in the README.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename supplied.")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{suffix or file.filename}'. "
                f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}."
            ),
        )

    record = UploadedFile(filename=file.filename, status=FileStatus.PENDING.value)
    session.add(record)
    session.commit()
    session.refresh(record)

    # Each upload gets an isolated temp directory. Shapefile extraction writes
    # siblings next to the archive, so isolation prevents two concurrent
    # uploads of different layers from reading each other's components.
    work_dir = Path(tempfile.mkdtemp(prefix=f"geoapi-{record.id}-"))
    stored = work_dir / Path(file.filename).name

    try:
        record.size_bytes = _save_upload(file, stored)
        session.commit()
        process_file(session, record, str(stored))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if record.status == FileStatus.FAILED.value:
        # The record is kept so the failure is inspectable via GET, but the
        # upload call itself reports 422: the request was well-formed, the
        # payload was not processable.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=record.error or "The file could not be processed.",
        )

    return _summary(record)


@router.get("/files/", response_model=FileListResponse, summary="List uploaded files")
def list_files(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> FileListResponse:
    total = session.scalar(select(func.count()).select_from(UploadedFile)) or 0
    records = session.scalars(
        select(UploadedFile)
        .order_by(UploadedFile.created_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return FileListResponse(count=total, files=[_summary(r) for r in records])


@router.get(
    "/files/{file_id}/",
    response_model=FileSummary,
    summary="Get information about one uploaded file",
)
def get_file(file_id: str, session: Session = Depends(get_session)) -> FileSummary:
    record = session.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No file with id '{file_id}'.")
    return _summary(record)


@router.get(
    "/files/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get per-feature measurements for an uploaded file",
)
def get_measurements(
    file_id: str,
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    geometry_type: str | None = Query(
        None, description="Filter by geometry type, e.g. Polygon"
    ),
    measurement_status: str | None = Query(
        None, description="Filter by MEASURED | NOT_APPLICABLE | UNSUPPORTED | FAILED"
    ),
    session: Session = Depends(get_session),
) -> MeasurementsResponse:
    """Return measurements for a file's features.

    Paginated because a survey layer can hold tens of thousands of features
    and an unbounded response would be both slow and unusable in a browser.
    """
    record = session.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No file with id '{file_id}'.")

    query = select(Feature).where(Feature.file_id == file_id)
    if geometry_type:
        query = query.where(Feature.geometry_type == geometry_type)
    if measurement_status:
        query = query.where(Feature.measurement_status == measurement_status.upper())

    features = session.scalars(
        query.order_by(Feature.feature_index).limit(limit).offset(offset)
    ).all()

    measurements = [
        FeatureMeasurement(
            feature_id=f.id,
            feature_index=f.feature_index,
            geometry_type=f.geometry_type,
            measurement_status=f.measurement_status,
            area=(
                {
                    "square_metres": f.area_sq_m,
                    "hectares": round(f.area_sq_m / 10_000, 6),
                    "square_kilometres": round(f.area_sq_m / 1_000_000, 8),
                    "acres": round(f.area_sq_m / 4046.8564224, 6),
                }
                if f.area_sq_m is not None
                else None
            ),
            length=(
                {"metres": f.length_m, "kilometres": round(f.length_m / 1000, 7)}
                if f.length_m is not None
                else None
            ),
            projected_crs=f.projected_crs,
            projection_reason=f.projection_reason,
            properties=f.properties or {},
            note=f.note,
            warnings=f.warnings or [],
            error=f.error,
        )
        for f in features
    ]

    return MeasurementsResponse(
        file_id=record.id,
        filename=record.filename,
        status=record.status,
        source_crs=record.crs,
        feature_count=record.feature_count,
        returned=len(measurements),
        offset=offset,
        limit=limit,
        totals={
            "area_square_metres": record.total_area_sq_m,
            "area_hectares": round(record.total_area_sq_m / 10_000, 6),
            "length_metres": record.total_length_m,
        },
        measurements=measurements,
    )


@router.get(
    "/files/{file_id}/geojson/",
    summary="Download the file's features as GeoJSON",
)
def get_geojson(file_id: str, session: Session = Depends(get_session)) -> dict:
    """Return a GeoJSON FeatureCollection with measurements as properties.

    This exists so the result can be opened directly in QGIS, geojson.io or a
    web map, which is how a surveyor would actually verify the numbers.
    """
    import json as _json

    record = session.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No file with id '{file_id}'.")

    features = session.scalars(
        select(Feature)
        .where(Feature.file_id == file_id, Feature.geometry_geojson.is_not(None))
        .order_by(Feature.feature_index)
    ).all()

    return {
        "type": "FeatureCollection",
        # Geometry is stored in the source CRS; GeoJSON (RFC 7946) mandates
        # WGS 84, so this is only strictly conformant for 4326 sources. Named
        # here rather than left implicit.
        "crs": {"type": "name", "properties": {"name": record.crs}},
        "features": [
            {
                "type": "Feature",
                "id": f.feature_index,
                "geometry": _json.loads(f.geometry_geojson),
                "properties": {
                    **(f.properties or {}),
                    "_measurement_status": f.measurement_status,
                    "_area_sq_m": f.area_sq_m,
                    "_length_m": f.length_m,
                    "_projected_crs": f.projected_crs,
                },
            }
            for f in features
        ],
    }


@router.delete("/files/{file_id}/", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: str, session: Session = Depends(get_session)) -> None:
    record = session.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No file with id '{file_id}'.")
    session.delete(record)
    session.commit()
