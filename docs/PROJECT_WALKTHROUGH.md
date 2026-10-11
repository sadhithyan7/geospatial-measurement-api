# Project Walkthrough

Everything in this project, explained so you can talk about any part of it
without opening the code. Read this once end to end before an interview.

---

## 1. The one-sentence version

> A FastAPI service that takes a Shapefile or KML upload, extracts every
> feature, reprojects each one into a metre-based coordinate system chosen
> from its own location, and returns area for polygons and length for lines —
> with the projection it used reported alongside every number.

**If you only remember one thing:** you cannot measure area or distance in
latitude/longitude degrees, and this project is built around doing that
correctly.

---

## 2. Tech stack, and why each piece is there

| Layer | Choice | Why this and not the obvious alternative |
|---|---|---|
| Web framework | **FastAPI** | Async-capable ASGI, Pydantic validation built in, and auto-generated OpenAPI docs at `/docs` — which doubles as the UI a reviewer tries first. Django + DRF was the other allowed option; it brings an admin, auth and migrations this service has no use for. |
| Geometry | **Shapely 2.2** | The Python binding to GEOS, the same engine behind PostGIS. Array-based `transform` in v2 reprojects a whole geometry in one vectorised call. |
| Projection | **pyproj 3.8** | Binding to PROJ, the library that defines the EPSG transformations. Supplies the `Transformer` and the CRS parsing. |
| Shapefile reading | **pyshp** | Pure Python, implements the shapefile spec directly. No GDAL. |
| KML reading | **stdlib `xml.etree`** | KML is XML with a CRS fixed by spec. A GIS driver adds nothing. `defusedxml` is used when installed, for entity-expansion hardening. |
| Persistence | **SQLAlchemy 2.1 + SQLite** | ORM with typed `Mapped[]` columns. SQLite means zero setup; `DATABASE_URL` swaps in Postgres with no code change. |
| Tests | **pytest + httpx** | 84 tests. `TestClient` exercises the real ASGI app. |
| Frontend | **One HTML file** | No build step, no framework. The real interface is `/docs`. |

### The decision you will most likely be challenged on

> *"Why not GeoPandas? It reads both formats in one line."*

Three reasons, in order of weight:

1. **Deployment size.** GeoPandas requires GDAL — ~100 MB of binaries plus
   system packages. Measured: ~150 MB installed without it, 250 MB+ with.
   Vercel's limit is 250 MB. It did not fit.
2. **No functional loss at this scope.** GeoPandas exists to normalise dozens
   of formats through one driver layer. This service reads two, one of which
   (KML) has a CRS fixed by specification.
3. **Explicitness.** GeoPandas has `estimate_utm_crs()`, which would have done
   the zone selection for me. Implementing it is about fifteen lines of
   arithmetic I can derive on a whiteboard, rather than a call I would have
   to defend without understanding.

Say plainly what the trade-off cost: **if the requirement grew to GeoPackage,
DXF, GML and raster, GeoPandas/GDAL becomes the right answer** and the
`Parser` protocol is where it would slot in.

---

## 3. Architecture

```
            HTTP request
                 │
        ┌────────▼─────────┐
        │   app/api/       │   routing, validation, HTTP status codes
        │   routes.py      │   knows nothing about shapefiles
        └────────┬─────────┘
                 │
        ┌────────▼──────────────┐
        │ services/processor.py │   the only module that knows the
        │                       │   full sequence of steps
        └───┬───────────────┬───┘
            │               │
   ┌────────▼──────┐   ┌────▼──────────────────┐
   │   parsers/    │   │ crs.py + measurement  │
   │ shapefile,kml │   │ projection + maths    │
   │ no DB, no HTTP│   │ no files, no HTTP     │
   └───────┬───────┘   └────┬──────────────────┘
           │                │
        ┌──▼────────────────▼──┐
        │  models.py / db      │   SQLAlchemy ORM
        └──────────────────────┘
```

**The principle:** each layer only knows the layer below it. The payoff is
testability — `test_crs.py` calls `projection_for(77.59, 12.97)` directly and
asserts it returns EPSG:32643, with no HTTP request and no database. If the
CRS maths lived inside the route handler, testing it would mean constructing
an upload.

### Every file, and what it does

| File | Responsibility |
|---|---|
| `app/main.py` | Builds the FastAPI app: CORS, global exception handler, static mount, `/health`, DB init. |
| `app/config.py` | Env-driven settings. Detects serverless and relocates writable paths to `/tmp`. |
| `app/database.py` | Engine, session factory, `get_session()` dependency, `init_db()`. |
| `app/models.py` | `UploadedFile` and `Feature` ORM models, plus the `FileStatus` enum. |
| `app/schemas.py` | Pydantic response models — the HTTP contract, declared in one place. |
| `app/api/routes.py` | The six endpoints, streaming upload handler, pagination and filtering. |
| `app/services/processor.py` | Orchestration: parse → project → measure → persist. Owns failure policy. |
| `app/services/crs.py` | **The important one.** UTM/UPS selection, transformer caching, reprojection. |
| `app/services/measurement.py` | Area/length by geometry type, unit conversion, validity warnings. |
| `app/services/parsers/base.py` | `ParsedDataset`, `ParsedFeature`, `ParseError`, and the `Parser` protocol. |
| `app/services/parsers/shapefile_parser.py` | Safe zip extraction, component discovery, `.prj` handling. |
| `app/services/parsers/kml_parser.py` | Namespace-agnostic XML walk, coordinate parsing, geometry building. |
| `app/static/index.html` | Demo page: drop zone, sample buttons, Leaflet map, measurement table. |

---

## 4. The CRS problem, in full

This is the heart of the project. Be able to explain it from scratch.

### Why degrees cannot be measured

A coordinate like `(77.59, 12.97)` is **two angles** — 77.59° of rotation
around the Earth's axis, 12.97° up from the equator. It is a position on a
sphere, not a point on a plane.

Apply the shoelace formula to a box spanning 0.01° × 0.01° and you get
`0.0001`. The unit is "square degrees", which does not convert into m²,
because the ground size of a degree is not constant:

- 1° of **latitude** is ~110.6 km anywhere (meridians are near-circles).
- 1° of **longitude** is ~111.3 km at the equator, ~108.5 km at Bengaluru,
  ~69.3 km at London, and **0 km at the poles**, where all meridians meet.

So the same `0.0001` square degrees is ~1.23 km² at the equator and ~0.77 km²
in London. The number is not merely in the wrong unit — it is not a
measurement at all.

### The fix, and the choice inside it

Reproject into a CRS whose units are metres before measuring. *Which* CRS is
a real decision:

| Option | Why not |
|---|---|
| **Web Mercator (3857)** | One projection worldwide, trivially simple. Area error grows with latitude — by a factor of ~2 at 45°, and far worse beyond. Unusable for measurement; it exists for map tiles. |
| **A single national grid** (e.g. an Indian zone) | Accurate inside its own country, wrong outside. This service cannot assume where the data is from. |
| **Equal-area projection** (e.g. Mollweide) | Area is correct globally, but shape and distance are badly distorted, so polygon perimeters and line lengths come out wrong. |
| **Per-feature UTM** ✅ | Scale error stays within ~1 part in 2500 inside a zone. Both area *and* length stay accurate. |

**What UTM is:** the globe sliced into 60 north–south zones, each 6° of
longitude wide, each with its own projection tuned for that strip.

**The arithmetic**, which is all of `utm_zone_from_longitude`:

```python
zone = floor((longitude + 180) / 6) + 1      # 1 … 60
epsg = (32600 if latitude >= 0 else 32700) + zone
```

`+180` shifts the −180…180 range to 0…360; `/6` because zones are 6° wide;
`+1` because zones are 1-indexed. EPSG groups northern UTM zones at 326xx and
southern at 327xx.

**The edge cases handled:**

| Case | Handling |
|---|---|
| Beyond 84°N / 80°S | UTM is undefined there; switches to Universal Polar Stereographic (EPSG:5041/5042). |
| Longitude of exactly 180° | Would compute to zone 61; clamped to a valid zone. |
| Longitude outside −180…180 (real data wraps) | Normalised with modulo rather than raising. |
| Source already projected in metres | Measured in place — no round trip, no added rounding. |
| Feature spanning two zones | Zone chosen from the centroid, and `projection_reason` reports which. |

### The bug this design caught

pyproj honours each authority's declared axis order. **EPSG:4326 declares
latitude first.** Build a `Transformer` without `always_xy=True` and every
coordinate comes out swapped — and because 77.59 and 12.97 are both valid as
either, nothing raises. It silently produces a point in the wrong hemisphere.

The ground-truth test (`a 1 km square must measure ~1,000,000 m²`) caught it.
A test asserting "the function returns what it returns" would not have.

---

## 5. The processing pipeline, step by step

Walk through what happens on `POST /api/files/`:

1. **Extension check** → `415` if not `.kml` / `.zip`. Rejecting before
   reading any bytes.
2. **DB row created** with status `PENDING`, so a failure is still inspectable.
3. **Stream to disk** in 1 MB chunks into a per-upload temp directory, with a
   running size check → `413` mid-stream if oversized. Never loads the whole
   file into memory.
4. **Parser selected** by extension from the `PARSERS` dict.
5. **Parse** → a `ParsedDataset` holding the source CRS and a list of
   features, each of which may carry its own `error`.
6. **Per feature:** reproject to WGS 84 to find the centroid → pick UTM/UPS →
   reproject into it → measure → record the projection used.
7. **Persist** the file row (counts, totals, warnings, timing) and one row per
   feature.
8. **Temp directory deleted** in a `finally` block, so it goes even on error.

### Failure policy — two distinct levels

| Level | Example | Behaviour |
|---|---|---|
| **File** | Corrupt zip, malformed XML, no `.shp` inside | Terminal. Status `FAILED`, HTTP `422`. |
| **Feature** | One placemark with a single coordinate | Recorded on that feature, loop continues. File ends `COMPLETED_WITH_ERRORS`, HTTP `201`. |

This is the behaviour the assignment asked for in one line — *"a failure while
generating one should not prevent others"* — and `sample_survey.kml` proves
it: 6 features in, 4 measured, 1 not applicable, 1 failed, HTTP 201.

---

## 6. The API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/files/` | Upload and process |
| `GET` | `/api/files/` | List uploads (paginated) |
| `GET` | `/api/files/{id}/` | One file's summary |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements (paginated, filterable) |
| `GET` | `/api/files/{id}/geojson/` | FeatureCollection for QGIS / geojson.io |
| `DELETE` | `/api/files/{id}/` | Delete a file and its features |
| `GET` | `/health` | Liveness probe |

### Status codes, and why each

| Code | Meaning here |
|---|---|
| `201 Created` | A resource was created. Returned even for `COMPLETED_WITH_ERRORS` — the upload succeeded, some features did not. |
| `413 Content Too Large` | Past `MAX_UPLOAD_BYTES`. |
| `415 Unsupported Media Type` | Wrong extension — rejected before reading. |
| `422 Unprocessable Entity` | Request was well-formed, payload was not readable. The distinction from `400` is the point: nothing is wrong with the *request*. |
| `404 Not Found` | Unknown file id. |
| `204 No Content` | Successful delete, nothing to return. |

### Two endpoints beyond the brief, and why

- **`/geojson/`** — lets a reviewer open the result in QGIS or geojson.io and
  check the numbers against a real basemap. Verification, not decoration.
- **`DELETE`** — without it the demo deployment accumulates rows forever.

---

## 7. Data model

```
uploaded_files                          features
─────────────                           ────────
id            TEXT PK  (12-char uuid)   id                INTEGER PK
filename      TEXT                      file_id           FK ──┐
source_format TEXT                      feature_index     INT  │
status        TEXT                      geometry_type     TEXT │
crs           TEXT                      geometry_geojson  TEXT │
feature_count INT                       properties        JSON │
measured_count INT                      measurement_status TEXT│
failed_count  INT                       area_sq_m         FLOAT│
total_area_sq_m FLOAT                   length_m          FLOAT│
total_length_m  FLOAT                   projected_crs     TEXT │
warnings      JSON                      projection_reason TEXT │
processing_ms INT                       error             TEXT │
created_at    DATETIME  ◄───────────────────────────────────── ┘
```

Design points worth stating:

- **Measurements are stored, not recomputed on read.** Reprojection is the
  expensive step and a file's geometry never changes after upload.
- **`projection_reason` is persisted per feature.** The explanation has to
  survive as long as the number it explains.
- **Composite index on `(file_id, feature_index)`** matches the only access
  pattern — fetch one file's features in order — so the measurements endpoint
  is a single indexed range scan.
- **`cascade="all, delete-orphan"`** so deleting a file takes its features.
- **Geometry as GeoJSON text** is the compromise that keeps SQLite viable;
  PostGIS with a real geometry column is the production answer.

---

## 8. Security

The upload endpoint is unauthenticated and accepts archives. Each guard maps
to a specific attack:

| Threat | Guard |
|---|---|
| **Zip slip** — `../../etc/passwd` as a member name | Every path resolved and checked to be inside the target before writing. Test: `test_safe_extract_blocks_path_traversal`. |
| **Zip bomb** — kilobytes expanding to gigabytes | Declared uncompressed total and member count checked *before* extraction. |
| **Memory exhaustion** | 1 MB streaming chunks with a running size check. |
| **Feature flood** | Parsing stops at `MAX_FEATURES`, and says so in `warnings`. |
| **XML entity expansion** | `defusedxml` when available; stdlib parser already disables external entity resolution. |
| **Error leakage** | Global handler logs the traceback, returns a generic message — no filesystem paths to the client. |
| **Cross-upload interference** | Each upload extracts into its own temp directory. |

---

## 9. Testing

**84 tests.** The structure matters as much as the count:

| File | Covers |
|---|---|
| `test_crs.py` | Zone boundaries, hemispheres, polar fallback, antimeridian wrap, already-projected sources |
| `test_measurement.py` | Ground-truth area and length, holes, MultiPolygon, invalid geometry, unit consistency |
| `test_parsers.py` | Both formats, malformed input, zip-slip, zip-bomb, parser selection |
| `test_api.py` | Every endpoint, every status code, pagination, filtering, failure isolation |

**The ones to quote in an interview:**

```python
def test_one_square_kilometre_measures_as_one_square_kilometre():
    # A box sized to 1 km a side must return ~1,000,000 m2, within 1%.
    # The 1% is UTM's scale factor, which is by design slightly under 1.

def test_degrees_are_never_measured_directly():
    # In degrees this polygon's "area" is 0.0001. If that number ever
    # comes back, reprojection has silently stopped happening.

def test_one_bad_feature_does_not_sink_the_file():
    # 6 features in, 1 broken: still HTTP 201, still 4 measurements.
```

The point to make: these assert against **external ground truth**, not
against current behaviour. A test that says "the function returns what it
returns" passes forever and catches nothing.

---

## 10. Honest limitations

Volunteer these. Knowing what your code does not do reads as senior.

1. **Planar measurement only.** Area is of the 2D footprint. A sloped
   hillside has more surface area than its footprint; that needs a DEM.
2. **Centroid-based zone selection.** A feature spanning several UTM zones is
   measured in its centroid's zone. Error grows with span; a geodesic
   calculation would be the cross-check.
3. **Serverless storage is ephemeral.** Uploads survive a warm instance, not a
   cold start. Production needs Postgres plus object storage.
4. **Synchronous processing.** Fine to tens of thousands of features; past
   that it needs a queue.
5. **No authentication or rate limiting.** It is a demo endpoint.
6. **Invalid geometries are flagged, not repaired.** Deliberate — silently
   changing someone's survey data is worse than reporting the problem.
7. **KMZ not supported.** It is a zipped KML; the archive currently routes to
   the shapefile parser. A content sniff would fix it.

---

## 11. Numbers worth having memorised

| | |
|---|---|
| Test count | 84, all passing |
| Processing time | 8–75 ms for the sample files |
| Installed dependency size | ~150 MB (vs 250 MB+ with GeoPandas) |
| Runtime dependencies | 8 |
| UTM zones | 60, each 6° wide |
| UTM validity band | 80°S to 84°N |
| UTM scale accuracy | ~1 part in 2500 within a zone |
| EPSG bands | 326xx north, 327xx south |
| Bengaluru's zone | 43N → EPSG:32643 |

---

## 12. A two-minute spoken walkthrough

> The service takes a Shapefile or KML upload and returns area for polygons
> and length for lines.
>
> The thing it has to get right is coordinate systems. Most geospatial files
> store longitude and latitude in degrees, and a degree is an angle, not a
> length — one degree of longitude is 111 km at the equator and zero at the
> poles. So measuring directly in degrees gives you a number in square
> degrees, which doesn't convert to anything.
>
> Instead I reproject every geometry into a metre-based system before
> measuring, and I pick that system per feature from its own centroid — the
> UTM zone it falls in, or polar stereographic beyond 84 degrees. The
> response reports which projection produced each number, so the result can
> be audited rather than trusted.
>
> It's FastAPI, Shapely and pyproj, with SQLAlchemy over SQLite. I avoided
> GeoPandas deliberately — it needs GDAL, which is about 100 MB, and that
> wouldn't fit in a serverless bundle. Implementing the zone selection myself
> is fifteen lines of arithmetic, and it meant I actually understood it.
>
> Failures are handled at two levels: a corrupt file is a 422, but one bad
> geometry inside a good file is recorded against that feature and the rest
> still come back. There are 84 tests, and the important ones assert against
> known ground truth — a one-kilometre square has to measure a million square
> metres, Bengaluru coordinates have to resolve to zone 43 North.
