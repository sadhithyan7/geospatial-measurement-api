"""ORM models.

Two tables in a parent/child relationship: one row per uploaded file, and one
row per feature found inside it. Measurements are stored rather than
recomputed on read, because reprojection is the expensive step and a file's
geometry never changes after upload.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FileStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    # A file whose features were read but where some individual geometries
    # failed. Distinguishing this from FAILED lets a caller use the good rows.
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    source_format: Mapped[str] = mapped_column(String(32), default="unknown")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(32), default=FileStatus.PENDING.value)
    crs: Mapped[str] = mapped_column(String(128), default="UNKNOWN")
    layer_name: Mapped[str | None] = mapped_column(String(256), nullable=True)

    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    measured_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    total_area_sq_m: Mapped[float] = mapped_column(Float, default=0.0)
    total_length_m: Mapped[float] = mapped_column(Float, default=0.0)

    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    processing_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    features: Mapped[list["Feature"]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        order_by="Feature.feature_index",
    )


class Feature(Base):
    __tablename__ = "features"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(
        ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False
    )
    feature_index: Mapped[int] = mapped_column(Integer, nullable=False)

    geometry_type: Mapped[str] = mapped_column(String(64), default="Unknown")
    # Geometry is persisted as GeoJSON text. A production deployment would use
    # PostGIS and a real geometry column; GeoJSON keeps this portable to
    # SQLite, which is what makes the service runnable with zero setup.
    geometry_geojson: Mapped[str | None] = mapped_column(Text, nullable=True)
    properties: Mapped[dict] = mapped_column(JSON, default=dict)

    measurement_status: Mapped[str] = mapped_column(String(32), default="UNSUPPORTED")
    area_sq_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    length_m: Mapped[float | None] = mapped_column(Float, nullable=True)

    projected_crs: Mapped[str | None] = mapped_column(String(64), nullable=True)
    projection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    file: Mapped["UploadedFile"] = relationship(back_populates="features")


# Measurements are always fetched by file, so the composite index matches the
# access pattern and keeps GET /measurements a single indexed range scan.
Index("ix_features_file_index", Feature.file_id, Feature.feature_index)
