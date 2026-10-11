# Geospatial File Measurement API

A backend service that accepts a **zipped Shapefile** or a **KML** file, extracts
every feature in it, and returns **area** for polygons and **length** for
linestrings — each one measured in a projected coordinate system chosen for
where that feature actually sits on the globe.

**Live demo:** _(add your Vercel URL here)_ · **Interactive API docs:** `/docs`

Built with FastAPI, Shapely, pyproj and SQLAlchemy.

---

## The problem this service exists to get right

Most geospatial files store coordinates as **longitude and latitude in
degrees** (EPSG:4326). A degree is an *angle*, not a length. Running a planar
area formula over degrees returns a number in "square degrees", which is not
a unit of area and cannot be converted into one, because the ground distance
of a degree changes with latitude:

| Latitude | Ground distance of 1° longitude |
|---|---|
| 0° (equator) | ~111.3 km |
| 12.97° (Bengaluru) | ~108.5 km |
| 51.5° (London) | ~69.3 km |
| 80° | ~19.4 km |

So the same polygon drawn in Chennai and in Oslo would report the same
"area" in degrees while covering wildly different ground.

**What this service does instead:** every geometry is reprojected into a
metre-based CRS *before* it is measured, and the projection is picked **per
feature** from that feature's own centroid.

- Between 80°S and 84°N → the **UTM zone** containing the centroid.
  Zone number is `floor((longitude + 180) / 6) + 1`; the EPSG code is
  `32600 + zone` in the north and `32700 + zone` in the south.
- Outside that band → **Universal Polar Stereographic** (EPSG:5041 / 5042),
  because UTM is undefined at the poles.
- If the source file is *already* projected in metres, it is measured as-is,
  avoiding a pointless round trip and its rounding error.

Every measurement in the response carries the EPSG code that produced it and
a plain-English reason, so the number can be audited instead of trusted:

```json
"projected_crs": "EPSG:32643",
"projection_reason": "Centroid at lon 77.5046, lat 12.9745 falls in UTM zone 43 (Northern hemisphere)."
```

---

## Quick start

```bash
git clone https://github.com/sadhithyan7/geospatial-measurement-api.git
cd geospatial-measurement-api

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

| URL | What it is |
|---|---|
| http://localhost:8000/ | Demo page — drop a file or run a sample |
| http://localhost:8000/docs | Swagger UI, every endpoint runnable in the browser |
| http://localhost:8000/redoc | ReDoc reference |

Three sample files ship in `samples/` so the service can be exercised without
hunting for geospatial data:

| File | What it exercises |
|---|---|
| `sample_survey.kml` | Polygon, polygon-with-hole, linestring, point, mixed MultiGeometry, and one deliberately broken placemark |
| `sample_parcels_shapefile.zip` | A complete shapefile with `.shp` / `.shx` / `.dbf` / `.prj` |
| `sample_no_prj.zip` | The same layer with the `.prj` removed, to show the CRS-fallback warning |

### With Docker

```bash
docker build -t geoapi .
docker run -p 8000:8000 -v geoapi-data:/data geoapi
```

---

## API

### `POST /api/files/`
Upload and process a file. Multipart form field: `file`.

```bash
curl -F "file=@samples/sample_parcels_shapefile.zip" \
     http://localhost:8000/api/files/
```

```json
{
  "id": "6c1601df89aa",
  "filename": "sample_parcels_shapefile.zip",
  "source_format": "shapefile",
  "status": "COMPLETED",
  "crs": "EPSG:4326",
  "layer_name": "parcels",
  "feature_count": 3,
  "measured_count": 3,
  "failed_count": 0,
  "totals": {
    "area_square_metres": 4518749.0,
    "area_hectares": 451.8749,
    "length_metres": 0.0
  },
  "processing_ms": 74,
  "warnings": []
}
```

**Status codes**

| Code | When |
|---|---|
| `201` | Processed (possibly with per-feature errors — see `status`) |
| `413` | Upload exceeds `MAX_UPLOAD_BYTES` |
| `415` | Extension is not `.kml` or `.zip` |
| `422` | Well-formed request, unreadable payload (corrupt zip, malformed XML, no `.shp`) |

**File-level `status` values**

| Value | Meaning |
|---|---|
| `COMPLETED` | Every feature was read |
| `COMPLETED_WITH_ERRORS` | Some features failed; the rest have measurements |
| `FAILED` | The file itself could not be read |

### `GET /api/files/{id}/`
Summary for one upload. `404` if the id is unknown.

### `GET /api/files/{id}/measurements/`
Per-feature measurements. Paginated, and filterable.

| Query param | Default | Purpose |
|---|---|---|
| `limit` | 500 | Page size (1–5000) |
| `offset` | 0 | Page offset |
| `geometry_type` | — | e.g. `Polygon` |
| `measurement_status` | — | `MEASURED` / `NOT_APPLICABLE` / `UNSUPPORTED` / `FAILED` |

```bash
curl "http://localhost:8000/api/files/6c1601df89aa/measurements/?geometry_type=Polygon"
```

```json
{
  "file_id": "6c1601df89aa",
  "source_crs": "EPSG:4326",
  "feature_count": 3,
  "returned": 3,
  "measurements": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "measurement_status": "MEASURED",
      "area": {
        "square_metres": 1201796.6,
        "hectares": 120.17966,
        "square_kilometres": 1.2017966,
        "acres": 296.96
      },
      "length": { "metres": 4385.3, "kilometres": 4.3853 },
      "projected_crs": "EPSG:32643",
      "projection_reason": "Centroid at lon 77.6050, lat 12.9550 falls in UTM zone 43 (Northern hemisphere).",
      "properties": { "parcel_id": "PCL-001", "owner": "State Mining Corp", "land_use": "Excavation" },
      "note": "`length` is the perimeter of the polygon boundary.",
      "warnings": []
    }
  ]
}
```

**Per-feature `measurement_status`**

| Value | Meaning |
|---|---|
| `MEASURED` | Area and/or length computed |
| `NOT_APPLICABLE` | Valid geometry with nothing to measure (a Point) |
| `UNSUPPORTED` | Geometry type outside the measurement rules |
| `FAILED` | Geometry was unreadable or projection failed; `error` says why |

### `GET /api/files/{id}/geojson/`
The features as a GeoJSON `FeatureCollection`, measurements folded into each
feature's properties. Open it straight in QGIS or geojson.io to check the
numbers against a basemap.

### `GET /api/files/` · `DELETE /api/files/{id}/` · `GET /health`
List uploads, delete one, liveness probe.

---

## Architecture

```
            HTTP
              │
       ┌──────▼───────┐
       │  app/api     │  routing, validation, status codes
       └──────┬───────┘
              │
       ┌──────▼─────────────┐
       │ services/processor │  orchestrates the pipeline
       └──┬────────┬────────┘
          │        │
   ┌──────▼──┐  ┌──▼────────────┐
   │ parsers │  │ crs + measure │
   └─────────┘  └───────────────┘
          │        │
       ┌──▼────────▼──┐
       │ models / db  │  SQLAlchemy ORM
       └──────────────┘
```

```
app/
├── main.py                     FastAPI app, CORS, error handler, static mount
├── config.py                   env-driven settings
├── database.py                 engine + session lifecycle
├── models.py                   UploadedFile, Feature
├── schemas.py                  Pydantic response contracts
├── api/routes.py               the six endpoints
├── services/
│   ├── processor.py            upload → parse → project → measure → persist
│   ├── crs.py                  UTM/UPS selection and reprojection
│   ├── measurement.py          area, length, unit conversion
│   └── parsers/
│       ├── base.py             ParsedDataset / ParsedFeature / Parser protocol
│       ├── shapefile_parser.py pyshp + safe zip extraction
│       └── kml_parser.py       namespace-agnostic ElementTree parsing
└── static/index.html           demo page
```

Each layer only knows the one below it. Parsers know nothing about HTTP or
databases; `crs.py` knows nothing about files; `routes.py` knows nothing about
shapefile internals. That is what lets the CRS maths be tested directly,
without constructing a request or touching a database.

### Request flow

1. **Validate** the extension and stream the upload to an isolated temp
   directory, aborting mid-stream past the size cap.
2. **Select a parser** from the extension (`.zip` → shapefile, `.kml` → KML).
3. **Parse** into a `ParsedDataset`: source CRS, plus a list of features each
   carrying geometry, properties, and possibly its own error.
4. **For every feature** — find its centroid in WGS 84, pick the UTM/UPS zone,
   reproject, measure, and record which projection was used.
5. **Persist** the file row and its feature rows, then delete the temp
   directory.

### Measurement rules

| Geometry | Result |
|---|---|
| Polygon, MultiPolygon | Area (m², ha, km², acres) + perimeter. Interior rings are subtracted. |
| LineString, MultiLineString, LinearRing | Length (m, km) |
| Point, MultiPoint | `NOT_APPLICABLE` |
| GeometryCollection | Polygon members summed for area, line members for length |
| Anything else | `UNSUPPORTED`, with a reason |

Polygon perimeter is deliberately **excluded** from the file-level length
total — adding a perimeter to a road length produces a meaningless number.

---

## Design decisions

### Pure-Python geospatial stack instead of GeoPandas/GDAL
The obvious choice is `geopandas.read_file()`, which reads both formats in one
line. It pulls in GDAL: roughly 100 MB of binaries, plus system packages in
Docker. Measured on this project, the pure-Python set
(`pyshp` + `shapely` + `pyproj`) comes to **~150 MB installed, versus 250 MB+
with GeoPandas** — the difference between fitting inside a serverless bundle
and not.

The functional loss is nil at this scope: `pyshp` reads the shapefile spec
directly, and KML is XML with a CRS fixed by specification, so there is no
projection negotiation for a driver to handle. The gain is that CRS selection
is explicit code I can test and explain, rather than a call to
`estimate_utm_crs()` whose behaviour I would be taking on trust.

### Synchronous processing, not a task queue
Processing is CPU-bound and fast — the samples complete in 8–75 ms, and a
10,000-feature layer parses in well under a second. A Celery/Redis queue would
add a broker, a worker process, and polling for the client, in exchange for
nothing at this scale.

**Where this breaks:** at roughly 50,000+ features, or if raster/DEM support
were added, processing would outlast a sensible HTTP timeout. The migration
path is already open — `process_file()` takes `(session, record, path)` and
returns nothing to the caller, so it can move behind a worker with the
endpoint returning `202 Accepted` and the existing `status` field
(`PENDING → PROCESSING → COMPLETED`) driving a poll. The status model was
designed for that from the start.

### Failure isolated at the feature level
A file-level failure (corrupt zip, malformed XML) is terminal and returns
`422`. A feature-level failure is recorded against that feature and processing
continues. A survey file with one bad geometry out of 500 should return 499
measurements, not an error — which is exactly what `sample_survey.kml`
demonstrates: 4 measured, 1 point not applicable, 1 failed, HTTP 201.

### Missing `.prj` is a warning, not an error
Shapefiles arrive without a `.prj` constantly. Rejecting them would be
correct and useless. The service assumes EPSG:4326 — right for the large
majority of GPS and drone exports — and returns an explicit warning saying
the assumption was made and what it would mean if wrong.

### Geometry stored as GeoJSON text
PostGIS with a real geometry column is the production answer. GeoJSON in a
`TEXT` column keeps the whole service runnable on SQLite with zero setup,
which matters more for a reviewable project. `DATABASE_URL` already accepts a
Postgres URL, so the swap is configuration, not code.

### Uploads treated as hostile
The upload endpoint is unauthenticated and takes archives, so:

- **Zip-slip** — every extracted path is resolved and checked to be inside the
  target directory before a byte is written.
- **Zip bombs** — the declared uncompressed total and member count are checked
  against limits before extraction.
- **Memory exhaustion** — uploads stream to disk in 1 MB chunks with a running
  size check, so an oversized body is cut off mid-stream.
- **Feature floods** — parsing stops at `MAX_FEATURES` and says so.
- **Error leakage** — the global handler logs the traceback and returns a
  generic message, so no filesystem path reaches the client.

### Transformer caching
Building a pyproj `Transformer` compiles a transformation pipeline, which is
the expensive part of a reprojection. Since thousands of features typically
share one source CRS and one target zone, transformers are cached with
`lru_cache`, turning N pipeline builds into one. Reprojection itself is
vectorised through Shapely 2's array-based `transform`, so a polygon's
coordinates cross into PROJ in a single call rather than one per vertex.

---

## Testing

```bash
pytest                 # 84 tests
pytest -v              # per-test names
pytest tests/test_crs.py
```

```
tests/test_crs.py           UTM/UPS selection, zone boundaries, antimeridian
tests/test_measurement.py   area/length correctness, holes, invalid geometry
tests/test_parsers.py       both formats, malformed input, archive safety
tests/test_api.py           endpoints, status codes, pagination, filtering
```

The tests that matter most assert against **known ground truth**, not against
whatever the code happens to return:

- A box sized to 1 km × 1 km must measure 1,000,000 m² within 1% (the
  tolerance is UTM's scale factor, which is by design slightly under 1).
- One degree of longitude at the equator must measure ~111.3 km.
- Bengaluru coordinates must resolve to EPSG:32643, New York to 32618,
  Sydney to 32756.
- A 4326 polygon must never come back with an area near its value in square
  degrees — the regression test for reprojection silently breaking.
- A polygon with a hole must measure less than the same polygon without one.

Plus the resilience guarantee, asserted end to end: a KML containing one
broken placemark returns `201` with measurements for the rest.

---

## Deployment

### Vercel
`vercel.json` routes every request to `api/index.py`, which exposes the same
ASGI app.

```bash
npm i -g vercel
vercel          # preview
vercel --prod   # production
```

**One caveat worth knowing.** Serverless filesystems are ephemeral and
read-only outside `/tmp`, so `config.py` detects the platform and relocates
SQLite there. Uploads therefore survive within a warm instance but not across
cold starts — fine for a demo, and the reason a real deployment sets
`DATABASE_URL` to a managed Postgres and writes files to object storage.

### Any container host
The `Dockerfile` runs as a non-root user with a healthcheck and a `/data`
volume, and needs no system GDAL packages.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite in `DATA_DIR` | Any SQLAlchemy URL |
| `DATA_DIR` | `./data` (`/tmp/geoapi` serverless) | Writable path |
| `MAX_UPLOAD_BYTES` | 52428800 (50 MB) | Upload cap |
| `MAX_UNCOMPRESSED_BYTES` | 262144000 (250 MB) | Zip-bomb guard |
| `MAX_ZIP_MEMBERS` | 200 | Archive entry cap |
| `MAX_FEATURES` | 50000 | Features parsed per file |
| `ALLOWED_ORIGINS` | `*` | CORS origins |

---

## What I learned building this

**Coordinate reference systems were the whole problem.** I started out
thinking the task was "parse a file, call `.area`". The first version
returned numbers like `0.0001` and I assumed the geometry was broken. It
wasn't — the area was correct, in square degrees, which is a unit that means
nothing. Understanding *why* that number is meaningless, rather than just
learning the incantation to avoid it, is the thing I actually took away:
latitude/longitude is an angular system on an ellipsoid, and every planar
formula silently assumes a plane.

**Picking the projection is a judgement call, not a lookup.** A single global
projection like Web Mercator is simple but its area error reaches a factor of
several away from the equator. Per-feature UTM keeps distortion inside ~1 part
in 2500 but means two features in one file can be measured in different
systems — which is correct, and also surprising enough that it needs
reporting. That is why `projection_reason` exists in the response: I would
rather the API explain itself than be believed.

**The shapefile "format" is four files in a trench coat.** Learning that the
`.prj` is optional, that its absence is routine rather than exceptional, and
that macOS zips carry `__MACOSX/._name.shp` decoys that parse as garbage, all
came from feeding the parser deliberately awkward archives.

**Constraints improved the design.** Fitting inside a serverless bundle ruled
out GeoPandas, which forced me to implement UTM zone selection myself. The
result is roughly fifteen lines I can derive on a whiteboard, instead of a
library call I would have had to defend without understanding.

**Testing geospatial code needs real ground truth.** Asserting that a function
returns what it currently returns proves nothing. Asserting that a 1 km square
measures a million square metres catches a whole class of silent errors — and
it caught two of mine, including a transform that was quietly swapping
latitude and longitude because I had omitted `always_xy=True`.

---

## Future scope

**Correctness**
- **Geodesic measurement** on the ellipsoid (`pyproj.Geod`) as a cross-check
  against the projected result, flagging features where the two disagree by
  more than a threshold — the honest way to detect a badly-chosen projection.
- **Geometry repair** offered explicitly (`buffer(0)` / `make_valid`) as an
  opt-in query parameter, reporting both the raw and repaired area rather
  than silently altering the user's data.
- **3D / slope area** for terrain-draped polygons, which needs a DEM and is
  what a mining volume calculation actually requires.

**Capability**
- More formats behind the existing `Parser` protocol: GeoJSON, GeoPackage,
  KMZ (a zipped KML), DXF.
- **Volume from a surface** — the measurement a drone survey of a stockpile or
  a pit is really after.
- **Coordinate-aware diffing**: upload the same site twice and get the change
  in area between surveys.

**Production readiness**
- PostGIS, so geometry is a first-class column and spatial queries become
  possible.
- Background processing via Celery or `arq` once file sizes justify it; the
  status model is already shaped for it.
- Object storage for uploads, with a signed-URL download of the original.
- API keys plus per-key rate limiting, since the current endpoint is open.
- Structured JSON logging with a request id threaded through, and a
  `/metrics` endpoint for processing time and failure rate.
- CI on GitHub Actions running `pytest` and `ruff` on every push.

---

## Licence

MIT
