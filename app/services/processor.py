"""Processing pipeline: upload -> parse -> project -> measure -> persist.

This module is the only place that knows the full sequence. Parsers know
nothing about databases, the CRS module knows nothing about HTTP, and the
API layer knows nothing about shapefile internals. That separation is what
makes each piece testable on its own.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from pyproj import CRS
from shapely.geometry import mapping
from sqlalchemy.orm import Session

from app.models import Feature, FileStatus, UploadedFile
from app.services.crs import describe_crs, project_for_measurement
from app.services.measurement import MeasurementStatus, measure
from app.services.parsers.base import ParsedDataset, ParseError
from app.services.parsers.kml_parser import KMLParser
from app.services.parsers.shapefile_parser import ShapefileParser

# Extension -> parser. Adding a format means adding one entry here.
PARSERS = {
    ".zip": ShapefileParser,
    ".kml": KMLParser,
}

SUPPORTED_EXTENSIONS = tuple(PARSERS)

# When a shapefile ships without a .prj, this is what we fall back to. It is
# the right guess for the overwhelming majority of GPS and drone exports, and
# the assumption is always surfaced to the caller as a warning.
DEFAULT_CRS = CRS.from_epsg(4326)


def select_parser(filename: str):
    """Choose a parser by file extension, or raise a clear ParseError."""
    suffix = Path(filename).suffix.lower()
    parser_class = PARSERS.get(suffix)
    if parser_class is None:
        raise ParseError(
            f"Unsupported file type '{suffix or filename}'. Upload a .kml file "
            "or a .zip containing a shapefile."
        )
    return parser_class()


def process_file(
    session: Session, record: UploadedFile, stored_path: str
) -> UploadedFile:
    """Run the full pipeline for one uploaded file and persist the results.

    Failures are handled at two levels. A failure to read the file at all is
    terminal and marks the record FAILED. A failure on a single feature is
    recorded against that feature and processing continues, which is why a
    file with one corrupt geometry still returns measurements for the rest.
    """
    started = time.perf_counter()
    record.status = FileStatus.PROCESSING.value
    session.commit()

    try:
        parser = select_parser(record.filename)
        dataset: ParsedDataset = parser.parse(stored_path)
    except ParseError as exc:
        record.status = FileStatus.FAILED.value
        record.error = str(exc)
        record.processing_ms = int((time.perf_counter() - started) * 1000)
        session.commit()
        return record
    except Exception as exc:  # unexpected: still must not 500 the request
        record.status = FileStatus.FAILED.value
        record.error = f"Unexpected error while reading the file: {exc}"
        record.processing_ms = int((time.perf_counter() - started) * 1000)
        session.commit()
        return record

    source_crs = dataset.crs or DEFAULT_CRS
    warnings = list(dataset.warnings)

    record.source_format = dataset.source_format
    record.layer_name = dataset.layer_name
    record.crs = describe_crs(source_crs)

    measured = 0
    failed = 0
    total_area = 0.0
    total_length = 0.0

    for parsed in dataset.features:
        feature = Feature(
            file_id=record.id,
            feature_index=parsed.index,
            properties=parsed.properties or {},
        )

        if parsed.geometry is None:
            feature.geometry_type = "Unknown"
            feature.measurement_status = MeasurementStatus.FAILED.value
            feature.error = parsed.error or "No geometry present."
            failed += 1
            session.add(feature)
            continue

        feature.geometry_type = parsed.geometry.geom_type
        try:
            feature.geometry_geojson = json.dumps(mapping(parsed.geometry))
        except Exception:
            feature.geometry_geojson = None

        try:
            projected, choice = project_for_measurement(parsed.geometry, source_crs)
            result = measure(projected, choice.as_dict())

            feature.measurement_status = result.status.value
            feature.projected_crs = f"EPSG:{choice.epsg}"
            feature.projection_reason = choice.reason
            feature.note = result.note
            feature.warnings = result.warnings

            if result.area:
                feature.area_sq_m = result.area["square_metres"]
                total_area += result.area["square_metres"]
            if result.length:
                feature.length_m = result.length["metres"]
                # Polygon `length` is perimeter, which is not comparable to
                # line length, so it is excluded from the file-level total.
                if result.geometry_type not in {"Polygon", "MultiPolygon"}:
                    total_length += result.length["metres"]

            if result.status is MeasurementStatus.MEASURED:
                measured += 1
        except Exception as exc:
            feature.measurement_status = MeasurementStatus.FAILED.value
            feature.error = f"Measurement failed: {exc}"
            failed += 1

        session.add(feature)

    record.feature_count = len(dataset.features)
    record.measured_count = measured
    record.failed_count = failed
    record.total_area_sq_m = round(total_area, 4)
    record.total_length_m = round(total_length, 4)
    record.warnings = warnings
    record.status = (
        FileStatus.COMPLETED_WITH_ERRORS.value if failed else FileStatus.COMPLETED.value
    )
    record.processing_ms = int((time.perf_counter() - started) * 1000)

    session.commit()
    session.refresh(record)
    return record
