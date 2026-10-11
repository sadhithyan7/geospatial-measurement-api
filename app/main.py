"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import settings
from app.database import init_db

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s | %(message)s"
)
logger = logging.getLogger("geoapi")

STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
Upload a **Shapefile** (zipped) or a **KML** file and get back per-feature
measurements: **area** for polygons, **length** for linestrings.

### The thing this service gets right

Area and distance cannot be computed from latitude/longitude degrees -- a
degree is an angle, not a length, and its ground distance changes with
latitude. Every geometry is therefore reprojected into a metre-based CRS
before it is measured. The projection is chosen **per feature** from its own
centroid (UTM zone, or Universal Polar Stereographic beyond 84 N / 80 S),
and the choice is reported back with every measurement so the number can be
audited rather than trusted.

### Endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/files/` | Upload and process a file |
| GET | `/api/files/` | List uploads |
| GET | `/api/files/{id}/` | File summary |
| GET | `/api/files/{id}/measurements/` | Per-feature measurements |
| GET | `/api/files/{id}/geojson/` | FeatureCollection for QGIS / geojson.io |
| DELETE | `/api/files/{id}/` | Delete an upload |
"""


# Tables are created at import time, not only in the lifespan handler.
# `create_all` is idempotent, and some serverless adapters invoke the ASGI app
# without running lifespan events at all -- initialising here means a cold
# start can never serve a request against a missing table.
init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database ready at %s", settings.resolved_database_url())
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description=DESCRIPTION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return JSON for unexpected errors instead of an HTML traceback.

    The detail is deliberately generic: internal exception text can disclose
    filesystem paths and library versions. The full traceback goes to the log.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred while processing the request."},
    )


app.include_router(router)


@app.get("/health", tags=["meta"], summary="Liveness probe")
def health() -> dict:
    return {"status": "ok", "version": settings.VERSION}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))
