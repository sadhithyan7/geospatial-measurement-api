"""Shapefile parser.

A "shapefile" is really a set of sibling files that must travel together,
which is why the API accepts them zipped:

  .shp  geometry records       (required)
  .shx  index into the .shp    (required by the format; pyshp can rebuild)
  .dbf  dBASE attribute table  (required for properties)
  .prj  WKT of the CRS         (optional, and its absence is the single most
                                common real-world data problem)

Parsing is done with `pyshp`, a pure-Python reader. The alternative,
GeoPandas, pulls in GDAL -- roughly 100 MB of binaries that would push the
deployment bundle past the serverless size limit for no functional gain at
this scope.
"""
from __future__ import annotations

import os
import tempfile
import zipfile
from contextlib import suppress
from pathlib import Path
from shutil import rmtree

import shapefile  # pyshp
from shapely.geometry import shape as shapely_shape

from app.config import settings
from app.services.crs import parse_crs

from .base import ParsedDataset, ParsedFeature, ParseError

REQUIRED_EXTENSIONS = {".shp"}


def safe_extract(zip_path: str, destination: str) -> list[str]:
    """Extract a zip while refusing archives that try to escape `destination`.

    A crafted zip can carry member names like `../../etc/passwd` ("zip slip")
    or a few kilobytes that inflate to gigabytes ("zip bomb"). Both are
    checked here, before any bytes are written, because this archive arrived
    over an unauthenticated HTTP endpoint.
    """
    extracted: list[str] = []
    dest_root = Path(destination).resolve()

    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()

        if len(members) > settings.MAX_ZIP_MEMBERS:
            raise ParseError(
                f"Archive contains {len(members)} entries, above the limit of "
                f"{settings.MAX_ZIP_MEMBERS}."
            )

        total = sum(m.file_size for m in members)
        if total > settings.MAX_UNCOMPRESSED_BYTES:
            raise ParseError(
                f"Archive expands to {total} bytes, above the limit of "
                f"{settings.MAX_UNCOMPRESSED_BYTES}."
            )

        for member in members:
            if member.is_dir():
                continue
            target = (dest_root / member.filename).resolve()
            if not str(target).startswith(str(dest_root) + os.sep):
                raise ParseError(
                    f"Archive entry '{member.filename}' resolves outside the "
                    "extraction directory and was rejected."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(str(target))

    return extracted


def _find_component(files: list[str], extension: str) -> str | None:
    """Locate one shapefile sibling, ignoring macOS resource-fork junk.

    Zips made on macOS carry a `__MACOSX/` directory holding `._name.shp`
    stubs that are not valid shapefiles. Reading one of those instead of the
    real file is a confusing failure, so they are filtered out here.
    """
    candidates = [
        f for f in files
        if f.lower().endswith(extension)
        and "__MACOSX" not in f
        and not os.path.basename(f).startswith("._")
    ]
    if not candidates:
        return None
    # Shallowest path first: the real layer usually sits at the archive root.
    return sorted(candidates, key=lambda p: (p.count(os.sep), len(p)))[0]


class ShapefileParser:
    """Reads a zipped shapefile into a `ParsedDataset`."""

    source_format = "shapefile"

    def parse(self, path: str) -> ParsedDataset:
        # Extract into a temporary directory of our own rather than beside the
        # input. The archive may live in a directory we do not own -- a shared
        # samples folder, or a caller's working directory -- and leaving
        # unpacked components there would both pollute it and risk a second
        # parse picking up the wrong layer. Everything is read into memory
        # before the directory is removed, so nothing outlives this method.
        work_dir = tempfile.mkdtemp(prefix="shp-extract-")
        try:
            return self._parse_extracted(path, work_dir)
        finally:
            with suppress(OSError):
                rmtree(work_dir, ignore_errors=True)

    def _parse_extracted(self, path: str, work_dir: str) -> ParsedDataset:
        try:
            files = safe_extract(path, work_dir)
        except zipfile.BadZipFile as exc:
            raise ParseError("File is not a readable .zip archive.") from exc

        shp = _find_component(files, ".shp")
        if shp is None:
            raise ParseError(
                "No .shp file found in the archive. A zipped shapefile must "
                "contain at least a .shp, and normally .shx, .dbf and .prj too."
            )

        dbf = _find_component(files, ".dbf")
        shx = _find_component(files, ".shx")
        prj = _find_component(files, ".prj")

        warnings: list[str] = []
        crs = None
        if prj:
            crs = parse_crs(Path(prj).read_text(encoding="utf-8", errors="replace"))
            if crs is None:
                warnings.append(
                    "A .prj file was present but its contents could not be "
                    "parsed as a CRS; assuming EPSG:4326."
                )
        else:
            warnings.append(
                "No .prj file in the archive, so the source CRS is undeclared. "
                "Assuming EPSG:4326 (WGS 84), the most common case for survey "
                "exports. Measurements will be wrong if the data is actually "
                "in a projected CRS."
            )

        if dbf is None:
            warnings.append("No .dbf file found; features will have no attributes.")

        try:
            reader = shapefile.Reader(shp=shp, dbf=dbf, shx=shx)
        except Exception as exc:  # pyshp raises a broad set of errors
            raise ParseError(f"Could not read shapefile: {exc}") from exc

        features: list[ParsedFeature] = []
        try:
            field_names = [f[0] for f in reader.fields[1:]]  # skip DeletionFlag
        except Exception:
            field_names = []

        for index, record in enumerate(reader.iterShapeRecords()):
            if index >= settings.MAX_FEATURES:
                warnings.append(
                    f"File contains more than {settings.MAX_FEATURES} features; "
                    "processing was truncated at that limit."
                )
                break

            properties: dict = {}
            try:
                if field_names and record.record is not None:
                    properties = {
                        name: _jsonable(value)
                        for name, value in zip(field_names, list(record.record))
                    }
            except Exception as exc:
                properties = {"_attribute_error": str(exc)}

            # A single corrupt record must not abort the remaining features,
            # so each geometry is converted inside its own try block.
            try:
                geo = record.shape.__geo_interface__
                geometry = shapely_shape(geo)
                features.append(
                    ParsedFeature(index=index, geometry=geometry, properties=properties)
                )
            except Exception as exc:
                features.append(
                    ParsedFeature(
                        index=index,
                        geometry=None,
                        properties=properties,
                        error=f"Unreadable geometry: {exc}",
                    )
                )

        reader.close()

        return ParsedDataset(
            crs=crs,
            features=features,
            layer_name=Path(shp).stem,
            source_format=self.source_format,
            warnings=warnings,
        )


def _jsonable(value):
    """Coerce dBASE values (dates, bytes, Decimal) into JSON-safe types."""
    import datetime
    import decimal

    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, str):
        return value.strip()
    return value
