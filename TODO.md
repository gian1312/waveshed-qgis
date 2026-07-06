# AETHER QGIS Plugin — TODO

## 0. HIGH PRIORITY — OPEN (NOT fixed)

Two issues remain broken after several attempts this session. The plugin changes
made so far did NOT fix them and are unverified against the real binaries. Both
need a fresh diagnosis.

### 0.1 [A] Terrain download leaves permanent "stripe" gaps — NOT FIXED
**Symptom.** With an XYZ elevation source (Mapzen Terrarium), a few tiles fail
and the coverage shows bands/stripes — always at the **same spot**, persisting
**even after deleting the cache**. Deterministic: the failing tiles are the
largest (high-detail mountain PNGs, e.g. clustered ~47.5N 10.2E). Fresh runs of
other areas succeed 100%.

**Verified facts** (`aether_converter/src/download.rs`, read-only agent):
- A failed XYZ tile is written as **elevation 0** (grid init ~`:1211`), so the
  stripe is a flat-0 band.
- Failing run logs a few `FAILED` with `connection closed before message
  completed` under `connections=625` (the plugin's `aether/download_connections`,
  honored verbatim ~`:958` — not self-scaled).
- The internal 3×/tile retry does NOT fire for "connection closed"
  (`Retries: 0`).
- The converter CANNOT retry individual XYZ tiles, cannot patch an existing
  `.abt`, never lists failed tile coords (only first-of-category + counts), no
  resume/skip-existing — every run truncates & rewrites each `.abt`. Smallest
  re-fetch unit = one whole `.abt` sub-tile.

**Tried and did NOT fix it (plugin):** re-download only sub-tiles whose `.abt`
shows a zero-block, at halved `max_connections` per pass; mark incomplete caches
so they re-download. Still broken as before.

**Leads:** (a) confirm/deny concurrency as the cause — run the failing area at a
low `aether/download_connections` (e.g. 64) and check it reaches Y/Y; (b) if a
converter change is later allowed, add a failed-tile list + tile-subset patch so
only the actually-failed tiles are refetched.

### 0.2 [B] No coverage when the range crosses a local DEM's border — NOT FIXED
**Symptom.** With a local GeoTIFF DEM (e.g. swissALTIRegio), when the analysis
range extends past the DEM extent there is **no coverage** in the beyond-DEM
area. Small radii (fully inside the DEM) work.

**Cause NOT yet correctly identified.** A prior analysis blamed the beyond-DEM
`.abt` pixels being `-9999` instead of `0` — **that is WRONG**: aether_core would
produce coverage there even with `-9999`, so the missing coverage is NOT about
the fill value. `scan_abt.py` on the failing cache showed 0 zero-bands / 0
stride-overflow (11 tiles) — the written tiles look clean. aether_core is
confirmed to handle proper tiles.

**Tried and did NOT fix it (plugin):** GDAL warp `INIT_DEST=0` + delete-nodata
(0-fill beyond the DEM), size cap, warn-once. "Behaves as before."

**Leads:** find what aether_core actually needs beyond the DEM edge that it isn't
getting. Diff a working in-DEM run against a border-crossing run: are the correct
`.abt` sub-tiles for the beyond-DEM area generated and handed to aether_core, and
how does the job's analysis area map to the terrain tiles it loads?

## 1. Bug Fixes

### 1.1 Log size estimate is misleading
The log prints two different "tile" counts without distinguishing them:
- "225 sub-tiles, ~53104 MB output" = output .abt files
- "tiles=683x1021=697343 (~136199MB)" = XYZ web tiles to download

The XYZ download estimate uses a hardcoded 200 KB/tile assumption
(`download.rs:475`) which is wildly inaccurate for terrain PNGs.
Fix: label them clearly ("output tiles" vs "source tiles"), and compute
the download estimate from actual mean tile size after the first strip.

### 1.2 ABT tile size mismatch between QGIS plugin and aether_core
The QGIS plugin always produces **1-degree sub-tiles** (`terrain_adapter.py`
`_compute_subtiles`, `sub = 1.0`). AETHER's `prepare_data.py` uses
**variable sub-tile sizes**: 0.1 deg (res <= 3 m), 0.5 deg (res > 3 m with
Swiss data), 1.0 deg (base DEM only).

At 10 m resolution with 1-degree tiles the pixel grid is 11112x11112
(~236 MB per tile). With 0.1-degree tiles it would be 1112x1112 (~2.4 MB).
aether_core accepts both, but:
- 1-degree tiles at high resolution are wastefully large and slow to
  download/assemble.
- When mixing QGIS-generated and prepare_data-generated caches the core
  might pick different tiles, causing inconsistent results.

Fix: match prepare_data.py's variable sub-tile logic. Use smaller tiles
for higher resolutions (e.g. 0.25 deg for 10 m, 0.1 deg for 2 m).
Verify the resulting .abt headers (size_px, scale_x/y, stride) match
what aether_core's `io::peek_abt()` expects.

### 1.3 Verify 5 m resolution download performance
At 5 m resolution the XYZ tile count and .abt output size are roughly 4x
that of 10 m. Profile a real 5 m download (e.g. 100 km range) and check:
- Total XYZ tiles vs available server bandwidth (are we saturating S3?).
- Strip assembly time — 22224-px-wide rows may blow L3 cache.
- Peak RAM — a single strip at 5 m is ~4x the memory of 10 m.
- Disk I/O — each .abt tile is ~940 MB; verify sequential write keeps up.
- Whether sub-tile size should drop to 0.25 deg at 5 m (like
  prepare_data.py uses 0.5 deg) to keep per-file size manageable.

---

## 2. UI / UX Improvements

### 2.1 Add one site entry by default
When the Site Analysis tab opens, pre-populate one empty site row so the
user doesn't have to click "Add Site" before they can start.

### 2.2 Add 90 m resolution option
Currently available: [2, 5, 10, 30]. Add 90 m as a fast-preview option.
Update `_RESOLUTIONS` in `coverage.py`, `p2p.py`, and the GUI spinboxes.
Also update `job_builder.py` validation if it enforces a whitelist.

### 2.3 Warn on too-low resolution for the selected range
If the user picks a resolution that will produce a very large download
(e.g. 2 m at 500 km), show a warning with the estimated download size
and time. Thresholds to consider:
- `> 50 GB output` -> warning
- `> 200 GB output` -> strong warning / require confirmation
Compute from `_estimate_abt_disk_mb()` in the GUI before launching.

### 2.4 Warn on missing terrain tiles in DEM/TIFF folder
When the user selects a local terrain directory, scan it and compare
coverage against the requested bbox. If tiles are missing (gaps in the
1-degree or sub-degree grid), show a list of missing tiles and ask for
confirmation before proceeding. Use GDAL to read extents of each file.

### 2.5 Alert when user selects a non-terrain layer
Detect when the user picks a map/imagery layer (e.g. satellite, OSM,
vector tiles) instead of a DEM/elevation layer and show a clear error.
Heuristics:
- Band count: DEM is typically single-band; RGB/RGBA = not terrain.
- Data type: DEM is Float32/Int16; Byte = likely imagery.
- Layer name / source keywords: "ortho", "satellite", "osm", "street".
- Raster statistics: if band 1 min/max is 0-255 with 3+ bands, it's
  almost certainly an image, not elevation data.
Show: "This looks like a map/image layer, not a DEM. Elevation data is
required for propagation analysis."

### 2.6 Create Help system
- **In-plugin help**: add a Help button / menu entry that opens a panel
  or dialog with:
  - Quick-start guide (add DEM, place TX, run coverage).
  - Explanation of each parameter (resolution, range, propagation model,
    azimuth, height modes AGL/AMSL).
  - Recommended DEM sources (Mapzen Terrarium XYZ, Copernicus 30 m,
    national high-res DEMs).
  - Troubleshooting: common errors, what "Rust download failed" means,
    how to check terrain coverage.
- **Tooltips**: add `setToolTip()` on all input widgets with a one-line
  description.
- **Context help**: link specific error dialogs to the relevant help
  section.
- Consider shipping a bundled HTML/Markdown help file that also works
  offline.

### 2.7 Implement buildings connection
Expose the converter's building overlay capability in the plugin:
- Let the user select a building footprint layer (FlatGeobuf, Shapefile,
  GeoPackage, or a directory of .fgb parts).
- The layer must have 3D geometry (Z = roof elevation) or a height
  attribute.
- Pass the buildings path as `buildings_file` in the converter ingest
  job JSON.
- In the Site Analysis / P2P tabs, add an optional "Buildings" layer
  picker (QgsMapLayerComboBox filtered to vector polygon layers).
- For XYZ download path (Path 1): buildings can't be fused during
  download — need a post-processing step that re-runs the converter
  with the downloaded .abt as base + buildings overlay.
- Warn if the building layer CRS differs from WGS84 (converter expects
  WGS84 or LV95 geometries).

---

## 3. Map Converter Tab (New Feature)

### 3.1 Overview
New tab in the main dialog that lets users fuse multiple raster layers
into .abt tiles offline, independent of a simulation run. This matches
what `prepare_data.py` does server-side, but with a GUI and support for
arbitrary layer stacks.

### 3.2 Layer stack
The user defines an ordered list of layers (highest priority on top):
1. **Sparse overlay** (e.g. buildings from FlatGeobuf / shapefile)
2. **High-res local** (e.g. SwissALTI3D 2 m from a folder of GeoTIFFs)
3. **Medium-res WMS/XYZ** (e.g. EU-DEM 30 m from a WMS)
4. **Base DEM** (e.g. Copernicus 30 m from XYZ tiles)

Each layer entry needs:
- Source: QGIS raster layer, local directory, or WMS/XYZ URL
- Priority: drag-to-reorder
- Native resolution (auto-detected from provider or metadata)
- CRS (auto-detected; prompt user if detection fails)

### 3.3 CRS handling
- Auto-detect from QGIS layer CRS or GeoTIFF metadata.
- If a layer has no CRS (or CRS is unknown), show a dropdown to let the
  user pick. Default suggestions: EPSG:4326, EPSG:2056 (LV95),
  EPSG:32632 (UTM 32N).
- Reproject all layers to WGS84 before feeding to the converter.

### 3.4 Extent selection
- Let the user draw a polygon on the QGIS map canvas to define the
  output extent (use `QgsMapToolEmitPoint` or `QgsRubberBand`).
- Also support: bbox from current map view, bbox from a layer extent,
  or manual N/S/E/W entry.
- Snap extent to 1-degree (or sub-degree) tile grid boundaries.

### 3.5 Resolution and sub-tile size selection
- Let the user pick which resolutions to generate (checkboxes):
  [2 m, 5 m, 10 m, 30 m, 90 m].
- Auto-compute sub-tile size per resolution (match prepare_data.py):
  - res <= 3 m  -> 0.1 deg sub-tiles
  - res <= 10 m -> 0.25 deg sub-tiles
  - res <= 30 m -> 0.5 deg sub-tiles
  - res > 30 m  -> 1.0 deg sub-tiles
- Show estimated output size and tile count before starting.
- calc_size formula: `round(sub_deg / (res_m / 111111))`, rounded up
  to next multiple of 4 (BC6H block alignment).

### 3.6 Converter integration
- Build an ingest batch job JSON matching the `IngestJob` struct
  (`ingest.rs:18-29`):
  ```json
  {
    "output_path": "...",
    "format": "r16sint",
    "ul_lat": 47.5, "ul_lon": 8.0,
    "resolution_m": 10.0,
    "size_px": 2780,
    "base_tif": "path/to/base.tif",
    "swiss_tifs": ["path/to/high_res_1.tif", ...],
    "buildings_file": "path/to/buildings_dir"
  }
  ```
- For XYZ/WMS sources that aren't local files: export via GDAL or QGIS
  `writeRaster` to a temp GeoTIFF first (like terrain_adapter.py Path 3).
- Run `aether_converter ingest --job-file <batch.json>`.
- The converter handles layer fusion internally: swiss_tifs override
  base_tif per-pixel, buildings overlay on top.

### 3.7 Format selection
- Let the user choose output format:
  - **r16sint** (2 bytes/px) — compatible everywhere, fast ingest
  - **bc6h** (1 byte/px) — GPU-optimized, smaller on disk
- Default: r16sint (bc6h requires the converter built with the `bc6h`
  feature flag).

---

## 4. Tests

### 4.1 Unit tests (no AETHER binaries required)

- **`_compute_sector_bbox`**: verify tight bbox for full circle, 90 deg
  sector, wrapping sector (e.g. 270-90), small range, equator vs poles.
- **`_compute_subtiles` + circular filter**: verify tile count for known
  bbox/range/azimuth combos. Full circle at 47 N / 500 km should produce
  ~177 tiles (not 225). 90 deg sector should produce ~50.
- **`_estimate_abt_disk_mb`**: verify against hand-computed values for
  known tile sizes.
- **`_cache_key`**: verify azimuth-dependent keys differ from full-circle
  keys.
- **`_cache_hit`**: mock filesystem, check .abt detection.
- **`get_source_resolution_info`**: mock XYZ and local layer providers.
- **`_try_rust_download` job JSON**: verify zoom calculation, encoding
  detection, URL template extraction for known XYZ source strings.
- **Sub-tile size logic** (once variable sub-tile sizes are implemented):
  verify that resolution-to-subtile-degree mapping matches prepare_data.py.
- **Non-terrain layer detection**: verify heuristics correctly reject
  RGB imagery, vector tiles, and OSM layers.

### 4.2 Integration tests (require QGIS environment)

- **Round-trip with a small XYZ area**: download a 1x1 degree area at
  30 m, verify .abt files are created, headers are valid, pixel values
  are non-zero.
- **Local TIFF folder path**: provide a folder of small test GeoTIFFs,
  verify extraction + conversion produces valid .abt.
- **Missing tile detection**: provide a folder with gaps, verify warning.
- **GUI defaults**: verify Site Analysis tab opens with one pre-populated
  row.
- **Resolution warning**: set 2 m / 500 km, verify warning fires.

### 4.3 Geographic diversity tests

Test at different locations on earth to catch projection, tile boundary,
and data availability edge cases:

| Location | Lat | Lon | Why |
|----------|-----|-----|-----|
| Central Europe (Zurich) | 47.4 | 8.5 | Baseline, high-res Swiss data |
| Equator (Quito) | -0.2 | -78.5 | cos(lat)~1, lon_range_deg = lat_range_deg |
| High latitude (Tromsoe) | 69.6 | 19.0 | cos(lat)~0.35, extreme lon stretch |
| Southern hemisphere (Cape Town) | -34.0 | 18.5 | Negative lat, different tile naming |
| Antimeridian (Fiji) | -17.8 | 178.0 | Bbox wraps past 180 deg |
| Prime meridian (London) | 51.5 | 0.0 | Lon near zero, bbox crosses 0 |
| High altitude (La Paz) | -16.5 | -68.1 | Extreme elevation range |
| Flat terrain (Netherlands) | 52.1 | 5.1 | Near-zero elevation, sea level |
| Small island (Reunion) | -21.1 | 55.5 | Mostly ocean tiles, sparse data |

For each: run prepare_terrain at 30 m / 50 km, verify .abt files are
created, elevation values are sane, no crashes or coordinate overflows.

### 4.4 Map Converter tab tests (once implemented)

- **Layer stack ordering**: verify priority (overlay > high-res > base).
- **CRS auto-detection**: provide layers with known CRS, verify correct
  detection.
- **Extent from polygon**: mock map tool, verify bbox snapping.
- **Batch job JSON generation**: verify output matches `IngestJob` schema.
- **Multi-resolution generation**: verify correct sub-tile sizes and
  calc_size per resolution.

### 4.5 Buildings integration tests (once implemented)

- **FlatGeobuf overlay**: provide a small .fgb with 3D buildings, verify
  roof elevations appear in .abt output above terrain baseline.
- **CRS mismatch**: provide buildings in LV95, verify converter handles
  the reprojection (or plugin warns).
- **Empty area**: provide buildings file that doesn't overlap the tile,
  verify terrain is unchanged.

---

## 5. Licence Tracking

### 5.1 Licence inventory

Track all dependencies and data sources to avoid licence issues.

| Component | Licence | Notes |
|-----------|---------|-------|
| **QGIS Plugin code** | ? | Define plugin licence (GPLv2+ to match QGIS?) |
| **PyQt5** | GPLv3 / commercial | Bundled with QGIS, no separate distribution |
| **osgeo / GDAL** | MIT/X | Bundled with QGIS |
| **pynacl** | Apache 2.0 | Bundled with plugin for Ed25519 key validation |
| **aether_core** | Proprietary | Binary only, not distributed with plugin |
| **aether_converter** | Proprietary | Binary only, not distributed with plugin |
| **aether_export** | Proprietary | Binary only, not distributed with plugin |
| **reqwest** (Rust) | MIT/Apache 2.0 | Statically linked into converter |
| **tokio** (Rust) | MIT | Statically linked into converter |
| **rayon** (Rust) | MIT/Apache 2.0 | Statically linked into converter |
| **png** (Rust) | MIT/Apache 2.0 | Statically linked into converter |
| **serde** (Rust) | MIT/Apache 2.0 | Statically linked into converter |
| **sysinfo** (Rust) | MIT | Statically linked into converter |
| **image_dds** (Rust) | MIT/Apache 2.0 | Optional, BC6H feature |
| **wgpu** (Rust) | MIT/Apache 2.0 | In aether_core |

### 5.2 Data source licences

| Source | Licence | Attribution required? |
|--------|---------|----------------------|
| Mapzen Terrarium tiles (S3) | ODbL + various | Yes — Mapzen, OSM contributors, various national agencies |
| Mapbox Terrain tiles | Mapbox ToS | Yes — requires Mapbox attribution |
| Copernicus DEM (COP30) | Copernicus licence | Yes — "contains Copernicus data" |
| SwissALTI3D | Open Government Data (OGD) | Yes — swisstopo |
| OpenTopography API | Free tier ToS | API key required, rate limits |

### 5.3 Action items

- [ ] Decide on plugin licence (GPLv2+ recommended for QGIS plugin repo).
- [ ] Add LICENCE file to plugin root.
- [ ] Add attribution notices for bundled dependencies.
- [ ] Verify all Rust crate licences are compatible with proprietary
      binary distribution (MIT/Apache 2.0 = OK).
- [ ] Add data source attribution to plugin About dialog.
- [ ] Check if Mapzen S3 tiles have usage limits or require attribution
      display in the output.
- [ ] Check Mapbox ToS if Mapbox encoding is used — may require visible
      Mapbox logo on map output.
