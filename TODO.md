# AETHER QGIS Plugin — TODO

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

### 4.3 Map Converter tab tests (once implemented)

- **Layer stack ordering**: verify priority (overlay > high-res > base).
- **CRS auto-detection**: provide layers with known CRS, verify correct
  detection.
- **Extent from polygon**: mock map tool, verify bbox snapping.
- **Batch job JSON generation**: verify output matches `IngestJob` schema.
- **Multi-resolution generation**: verify correct sub-tile sizes and
  calc_size per resolution.
