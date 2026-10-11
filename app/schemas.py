"""Pydantic response models.

Declared explicitly rather than generated from the ORM so that the HTTP
contract is visible in one file and cannot drift silently when a column is
renamed. FastAPI turns these into the OpenAPI schema at /docs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AreaPayload(BaseModel):
    square_metres: float
    hectares: float
    square_kilometres: float
    acres: float


class LengthPayload(BaseModel):
    metres: float
    kilometres: float


class ProjectedCRS(BaseModel):
    epsg: str = Field(examples=["EPSG:32643"])
    name: str = Field(examples=["WGS 84 / UTM zone 43N"])
    reason: str


class FeatureMeasurement(BaseModel):
    feature_id: int
    feature_index: int
    geometry_type: str
    measurement_status: str = Field(
        description="MEASURED | NOT_APPLICABLE | UNSUPPORTED | FAILED"
    )
    area: AreaPayload | None = None
    length: LengthPayload | None = None
    projected_crs: str | None = None
    projection_reason: str | None = None
    properties: dict[str, Any] = {}
    note: str | None = None
    warnings: list[str] = []
    error: str | None = None


class FileSummary(BaseModel):
    id: str
    filename: str
    source_format: str
    size_bytes: int
    status: str
    crs: str
    layer_name: str | None = None
    feature_count: int
    measured_count: int
    failed_count: int
    totals: dict[str, float]
    processing_ms: int
    created_at: datetime
    warnings: list[str] = []
    error: str | None = None


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    status: str
    source_crs: str
    feature_count: int
    returned: int
    offset: int
    limit: int
    totals: dict[str, float]
    measurements: list[FeatureMeasurement]


class FileListResponse(BaseModel):
    count: int
    files: list[FileSummary]


class ErrorResponse(BaseModel):
    detail: str
