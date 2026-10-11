# Interview Prep — Aereo SDE Intern

Questions you are likely to face, grouped by how probable they are, with the
shape of a good answer. The assignment brief says explicitly:

> *"During the interview, you may be asked to explain your implementation,
> justify design decisions, debug or modify part of the application, or handle
> a changed requirement."*

So this is not a trivia test. They will open your repo and ask you to change
something. Prepare accordingly.

---

## Part 1 — Almost certain

### "Walk me through what you built."
Use the two-minute version at the end of `PROJECT_WALKTHROUGH.md`. Lead with
the CRS problem, not the file formats — the CRS is the engineering, the
parsing is plumbing.

### "Why did you pick this assignment over the certificate generator?"
Be honest and specific: it is closer to what Aereo does. Drone surveys produce
shapefiles and KML, and measurement on that data is the product. The
certificate generator would have tested job queues; this tested whether you
understand coordinate systems, which is the domain.

### "Why can't you just calculate area from latitude and longitude?"
**The single most likely technical question.** Full answer in §4 of the
walkthrough. Compressed:

> A degree is an angle, not a distance. One degree of longitude is about
> 111 km at the equator and zero at the poles, because the meridians
> converge. Applying a planar area formula to degrees gives you "square
> degrees", which doesn't convert to square metres — the conversion factor
> depends on latitude. So I reproject into a metre-based CRS first.

Then volunteer the next layer without being asked: *which* metre-based CRS,
and why UTM over Web Mercator.

### "How did you choose the UTM zone?"
Derive it live — it is three lines and they will be watching whether you can:

```python
zone = floor((longitude + 180) / 6) + 1     # 60 zones, 6 degrees each
epsg = (32600 if latitude >= 0 else 32700) + zone
```

Then the edge cases, unprompted: beyond 84°N/80°S UTM is undefined so it
falls back to polar stereographic; longitude exactly 180 would compute to
zone 61 and is clamped; out-of-range longitudes are normalised rather than
raising.

### "Why not GeoPandas?"
Size first, then the honest caveat. ~150 MB versus 250 MB+, which is the
serverless limit. Then: *"If the scope grew to GeoPackage, DXF and raster,
GeoPandas is the right answer and I'd switch."* Showing you know when your
own decision expires is worth more than the decision.

### "Walk me through your project structure."
`routes → processor → (parsers | crs + measurement) → models`. The line to
land: *"Each layer only knows the one below it, which is why I can test the
CRS maths by calling a function instead of making an HTTP request."*

### "How do you handle a file where one feature is broken?"
Two levels of failure. File-level is terminal (422). Feature-level is
recorded on that feature and the loop continues. Point them at
`sample_survey.kml`: 6 features, 1 deliberately broken, returns 201 with 4
measurements. There is a test named exactly that.

---

## Part 2 — Likely

### "What happens with a shapefile that has no .prj?"
It is a warning, not an error — because it happens constantly in real data.
Assumes EPSG:4326, and returns an explicit warning saying so. `sample_no_prj.zip`
demonstrates it. The reasoning: rejecting would be *correct* and *useless*.

### "What if a polygon spans two UTM zones?"
Honest answer: the centroid's zone is used, and `projection_reason` reports
which. Error grows with span. The proper fix is a geodesic calculation on the
ellipsoid (`pyproj.Geod`) as a cross-check — it is in the future scope.

Do not pretend this is solved. Naming a real limitation accurately is the
answer they want.

### "Why synchronous processing instead of a queue?"
8–75 ms on the samples; sub-second for ten thousand features. A broker and a
worker would add operational weight for nothing. Then the migration path:
`process_file()` already takes `(session, record, path)` and returns nothing
to the caller, and the status enum already runs
`PENDING → PROCESSING → COMPLETED`. Moving it behind a worker means returning
202 and letting the client poll — no redesign.

### "How would you handle a 2 GB file?"
Stream-parse instead of loading, move processing to a worker, chunk the
feature writes, return 202 with a polling URL, store the upload in object
storage rather than local disk. Say what breaks first: memory during parse.

### "What would you change for production?"
PostGIS instead of GeoJSON-in-TEXT. Object storage for uploads. A real task
queue. API keys and rate limiting. Structured logging with a request id.
CI running the tests. Everything here is already in the README's future scope
— know it without reading it.

### "Why does an upload with failed features return 201 and not 207 or 422?"
Because the upload succeeded and a resource was created. The failures are
*data* about that resource, surfaced in `status`, `failed_count`, and per-
feature `measurement_status`. 207 Multi-Status is for WebDAV-style batch
responses; it would be unusual here. This is a judgement call — hold it, but
acknowledge it is one.

### "How did you test geospatial code?"
Ground truth, not snapshots. A 1 km square must measure ~1,000,000 m² within
1%, where the 1% is UTM's scale factor. One degree at the equator must be
~111.3 km. Bengaluru must resolve to 32643. Then the story:

> That approach caught a real bug. pyproj follows each authority's axis
> order, and EPSG:4326 declares latitude first — so without `always_xy=True`
> every coordinate came out swapped. Nothing raised, because 77.59 and 12.97
> are both plausible either way. Only the ground-truth assertion caught it.

**Have this story ready.** It is the most convincing thing you can say.

---

## Part 3 — Live modification (be ready to actually type)

They may share a screen and ask for a change. The likely ones:

### "Add support for GeoJSON."
Say the shape before touching the keyboard:
1. New `GeoJSONParser` in `app/services/parsers/` satisfying the `Parser`
   protocol — one `parse(path) -> ParsedDataset` method.
2. Register `".geojson": GeoJSONParser` in the `PARSERS` dict in
   `processor.py`.
3. Nothing else changes. CRS is EPSG:4326 per RFC 7946.
4. Add tests mirroring `test_parsers.py`.

That the answer is "one new class and one dict entry" *is* the point of the
protocol.

### "Add perimeter as a separate field."
It is already computed — polygons return `length` as perimeter. The change is
renaming it in the schema for clarity. Point out the existing `note` field
that documents it, and the file-total logic that deliberately excludes
polygon perimeter from the length total.

### "Make it return results in acres only."
`measurement.py`, `_area_payload()`. Mention the conversion is the exact
international acre (4046.8564224 m²), not an approximation.

### "Add a filter for features above a minimum area."
A query param on `get_measurements`, added to the SQLAlchemy `where` clause —
`Feature.area_sq_m >= min_area`. Note it filters in SQL, not in Python, so
pagination stays correct.

### "This endpoint is slow with 50,000 features. Fix it."
Diagnose out loud before changing anything: is it parsing, reprojection, or
the DB write? Likely the per-feature `session.add()`. Fix with
`bulk_save_objects` or a single `insert().values([...])`. Mention the
transformer cache already removes repeated pipeline construction, and that
reprojection is already vectorised.

---

## Part 4 — Fundamentals they will check

The brief asks for *"Python fundamentals, REST APIs, SQL and OOP"*.

**Python**
- Generators vs lists, and why `iterShapeRecords()` matters for memory.
- Decorators — `@lru_cache` on `_transformer`, and why caching helps here.
- Context managers — `with zipfile.ZipFile(...)`, and the `finally` that
  removes the temp directory.
- `dataclass` vs Pydantic model: dataclasses internally, Pydantic at the
  boundary where validation and serialisation are needed.
- Type hints and `Protocol` for structural typing.

**REST**
- Idempotency: GET/DELETE yes, POST no.
- Why 422 rather than 400 here.
- Status code meanings — be exact.
- Pagination: why an unbounded list endpoint is a bug.

**SQL**
- Why `(file_id, feature_index)` is indexed as a composite and in that order.
- What `cascade="all, delete-orphan"` does.
- N+1 queries — where one would appear if measurements were fetched per
  feature, and that the current code avoids it with a single filtered query.
- Transactions: the commit boundaries in `process_file`.

**OOP**
- The `Parser` protocol as an interface, and why structural typing beats
  inheritance here.
- Separation of concerns across the layers.
- Why `ParseError` is a custom exception rather than a bare `Exception`.

---

## Part 5 — Questions about Aereo (know these before you walk in)

| | |
|---|---|
| Founded | 2013, as **Aarav Unmanned Systems**; rebranded to **Aereo** |
| Funding | ~$16.8M Series B — $15M led by 360 ONE Asset, plus a $1.8M extension led by JSW Ventures |
| Product | **Aereo Cloud** — drone data into 2D/3D visualisations, digital twins, analytics |
| Sectors | Mining, infrastructure, energy, utilities |
| Customers | Tata Steel (27+ mines and stockyards, since 2019), Coal India's Digicoal |
| Government | SVAMITVA — 45,000+ villages mapped; DILRMP — 50,000+ sq km |
| Founders | Vipul Singh, Suhas Banshiwala |

**The connection to make explicit:** their core loop is drone survey → vector
data → measurement → insight. Your project is the measurement step of that
loop, built the way their product has to build it.

---

## Part 6 — Questions to ask them

Asking nothing reads as disinterest. Pick three or four:

**Technical**
- How does Aereo Cloud handle CRS at scale — a single national grid per
  project, or per-feature selection like this?
- Are you on PostGIS, or something purpose-built for point clouds?
- What does the pipeline from raw drone imagery to measurable vector data
  look like — where does photogrammetry hand off to the backend?
- How do you validate measurement accuracy against ground survey control?

**About the role**
- What would an intern own in the first month?
- Is the work closer to platform/API or to the geospatial processing pipeline?
- How does code review work here?

**About the company**
- What changed technically when you moved from services into the Aereo Cloud
  product?
- What does the PPO decision actually get assessed on?

---

## Part 7 — Things to be careful about

**Be upfront about AI assistance if asked.** The certificate-generator brief
says explicitly that AI tools are allowed and that you must be able to
explain and modify what you submit. The honest framing: you used AI as a
pair, and you understand every decision — then prove it by deriving the UTM
formula or walking the failure policy. Pretending otherwise is the only way
this becomes a problem.

**Do not oversell.** If you have not used PostGIS, say so and say what you
know about why it would help. "I haven't, but here's what I'd need to learn"
beats a bluff that unravels in one follow-up.

**Know your own limitations list.** §10 of the walkthrough. Volunteering
these makes everything else you say more credible.

**Logistics they will raise:** the internship is 6 months, in-office in
Bengaluru, and you are a final-year student in Chennai graduating in 2027.
Have a clear answer on whether your college permits this and how you would
relocate. This is the most likely non-technical blocker — do not be caught
unprepared by it.

---

## The night before

- [ ] Clone your own repo fresh and run `pip install -r requirements-dev.txt`
      then `pytest`. Make sure it passes on a clean machine.
- [ ] Open the live Vercel URL and upload all three samples.
- [ ] Re-read `app/services/crs.py` — it is the file they will open.
- [ ] Practise the two-minute walkthrough out loud, twice.
- [ ] Be able to write the UTM zone formula from memory.
- [ ] Have the `always_xy=True` bug story ready.
- [ ] Know your three honest limitations.
