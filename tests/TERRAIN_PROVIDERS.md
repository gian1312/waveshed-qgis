# Testing terrain providers

How to exercise the terrain pipeline across XYZ, WMS/WMTS/WCS, local files, folders, VRT/COG,
remote `/vsi`, and offline. Written for manual/integration testing — the pytest suite stubs GDAL
to two attributes, so **no GDAL code path executes under `pytest`**. Everything below is the part
tests cannot reach.

---

## 0. Two things to know before you start

**There are two independent terrain implementations.** Testing one does not test the other.

| | Site Analysis / P2P | Map Converter tab |
|---|---|---|
| Code | `core/terrain_adapter.py` | `gui/map_converter_tab.py` |
| Entry | `prepare_terrain()` — sector AOI (`tx_lat`, `max_range_km`, `az_*`) | bbox, multi-layer stack, multi-resolution |
| XYZ | **same path** — shared Rust `download` into the pool (`terrain_adapter.ensure_pool_tiles`); pool `.abt` becomes an ingest source for the tab | same |
| Rendered (WMS/WMTS/ArcGIS) | `_export_via_qgis`, per tile | same renderer, per tile, on the main thread during resolve |
| Folder scan | `terrain_adapter.list_terrain_files` — `os.walk`, recursive, sorted | same (subfolders now included; was a flat `os.listdir`) |
| Tile name | `tile_N47.50E8.50_30m.abt` | same — one naming scheme (`terrain_adapter._tile_params`) |
| Source model | `sources[]` per tile + `void_fill_m: 0.0` | `sources[]` per tile, UI stack order, **no** `void_fill_m` (VOID kept) |
| Pre-run check | — | `aether_converter plan` cross-check; mismatch or missing subcommand aborts |
| Force rebuild | *no control* — Settings → Clear cache only | "Rebuild existing tiles" checkbox |

**The source model is generic now.** Both paths hand `aether_converter ingest`
a per-tile `sources[]` list — each file in its OWN CRS (`crs` declared when the
plugin knows an EPSG authid, else the converter reads GeoKeys) — filtered per
tile by WGS84 bounds + a per-file halo (`max(4 source px, 0.6 output cells)`,
longitude cos-corrected). There is **no plugin-side GDAL warp** any more: a
CRS the converter rejects fails the run with the converter's own message.
Folder entries are enumerated **sorted ascending by path** on both paths —
for overlapping files the earlier-sorted filename wins per pixel, so folder
priority is deterministic across machines. Formats the converter's tiff reader cannot open
(.hgt/.dem, VRT, `/vsi*`, GeoTIFFs > 512 MB) are window-copied to plain
GeoTIFF at native resolution (`terrain_adapter.materialize_for_converter`,
never resampled). Non-file QGIS layers (WMS/WMTS/ArcGIS) are still rendered
through QGIS to a WGS84 GeoTIFF — unavoidable.

Run every provider through **both**. Differences between them are the bug, not the test.

**The cache will lie to you.** A second run with a changed source can silently serve the first
run's tiles. See §4 before trusting any result.

---

## 1. Setup

### Binaries
`discover_binary_dir()` takes the **first directory containing all three** of `aether_core`,
`aether_converter`, `aether_export` — so a partial build directory is skipped and `PATH` silently
wins instead. Search order:

1. QgsSettings `waveshed/binary_dir` (Settings dialog)
2. `AETHER_BIN_DIR` env var
3. `PATH`
4. `~/.aether/bin`
5. `<plugin>/bin/`

```bash
export AETHER_BIN_DIR=/workspace/aether-tools/target/release   # all three must be here
```

### Cache location
Root is QgsSettings `waveshed/cache_dir`, default `~/.aether/cache`. Point it at a scratch dir so
you can delete it freely:

```
~/.aether/cache/pool/<md5>     # md5("v4|" + source_identity(source) [+ "|" + buildings fingerprint])
~/.aether/cache/views/<md5>    # md5("v4|" + RAW uri + "|" + tile names + "|" + resolution [+ buildings])
```

The view key uses the **raw QGIS URI**; the pool key uses the normalised `source_identity()`.
Re-spelling a URI (reordering params, changing case) moves the view but *not* the pool — a useful
way to force a fresh assembly without re-downloading.

### Local DEM fixtures
`data/swissaltiregio_tiles/` holds **2365 real swisstopo tiles** (12 GB, gridded ASCII XYZ, 10 m,
EPSG:2056/LV95, **no CRS declared in the file**). Not directly ingestible — convert first:

```bash
python3 tools/xyz_to_geotiff.py --tiles data/swissaltiregio_tiles --out /scratch/ch_tif --vrt
```

For everything else, synthesise small DEMs. GDAL CLI ships with QGIS; `gdal_create` needs
**GDAL ≥ 3.2** — on older builds substitute `gdal_translate -outsize` from any existing raster.

```bash
cd /scratch/dem
# baseline WGS84 Int16
gdal_create -outsize 512 512 -bands 1 -burn 1200 -ot Int16 -a_srs EPSG:4326 \
            -a_ullr 8.0 47.6 8.2 47.4 base_wgs84.tif
# Float32 + NaN nodata  -> the F4 regression (NaN used to decode as 0 m "valid ground")
gdal_create -outsize 512 512 -bands 1 -burn 1200 -ot Float32 -a_srs EPSG:4326 \
            -a_ullr 8.0 47.6 8.2 47.4 -a_nodata nan f32_nan.tif
# Int32 + classic nodata -> low 16 bits of -2147483648 are zero
gdal_create -outsize 512 512 -bands 1 -burn 1200 -ot Int32 -a_srs EPSG:4326 \
            -a_ullr 8.0 47.6 8.2 47.4 -a_nodata -2147483648 i32_nodata.tif
# projected CRS variants -> the A8 overlap test
gdalwarp -t_srs EPSG:3857 base_wgs84.tif merc.tif
gdalwarp -t_srs EPSG:32632 base_wgs84.tif utm32.tif
gdalwarp -t_srs EPSG:2056 base_wgs84.tif lv95.tif
# COG and VRT
gdal_translate -of COG base_wgs84.tif cog.tif
gdalbuildvrt mosaic.vrt base_wgs84.tif
```

> Only `.tif .tiff .dem .hgt` are scanned in a terrain **folder**. `.vrt` is **not** — a VRT works
> only when loaded as a QGIS raster *layer*, never when dropped in a directory.

---

## 2. Provider matrix

Add each as a QGIS layer, then run both paths over the same small AOI at the same resolution.

| # | Provider | How to add | What it exercises |
|---|---|---|---|
| 1 | **XYZ / Terrarium** | XYZ connection, `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`, **Max Zoom 15**, Interpretation = Terrarium | The shared Rust download path — BOTH tabs use it (pool `.abt` → ingest source in the Map Converter) |
| 2 | **XYZ, no `interpretation=`** | same, leave Interpretation unset | `xyz_encoding()` URL sniffing; must resolve Terrarium from `elevation-tiles-prod` |
| 3 | **XYZ / Mapbox Terrain-RGB** | `https://api.mapbox.com/v4/mapbox.terrain-rgb/{z}/{x}/{y}.pngraw?access_token=…` | B2 — must **not** decode as Terrarium (that gives ≈ −32000 m) |
| 4 | **XYZ, zoom too deep** | provider 1 with **Max Zoom 18** | B1 — must fail loudly, not produce flat 0 m |
| 5 | **WMS / WMTS** elevation | any WCS/WMS DEM service | The QGIS export path; nodata handling differs from the file path (A9) |
| 6 | **WMS basemap (RGB)** | any imagery WMS | B3 — must be rejected as imagery, not classified as a DEM |
| 7 | **Single GeoTIFF** | `base_wgs84.tif` | Baseline |
| 8 | **Folder of GeoTIFFs** | Add Folder → `/scratch/dem` | one shared scanner (`list_terrain_files`, recursive + sorted) on both paths (was A11) |
| 9 | **Nested folder** | put tifs in `/scratch/dem/sub/` | BOTH paths find them now (the Map Converter used to scan flat) |
| 10 | **VRT** | `mosaic.vrt` as a layer | Must not be scanned in a folder; must work as a layer |
| 11 | **COG** | `cog.tif` | Local COG |
| 12 | **Remote COG** | `/vsicurl/https://host/dem.tif` | A10 — `abspath()` collapses `https://` → `https:/` |
| 13 | **Zipped** | `/vsizip//path/x.zip/dem.tif` | Same `/vsi` prefix handling |
| 14 | **LV95 / swisstopo** | `lv95.tif`, or the converted `ch_tif` dir | generic-CRS ingest (was A2); LV95 now goes through the same `sources[]`+proj path as every projected CRS |
| 15 | **EPSG:3857 / 32632 folder** | `merc.tif`, `utm32.tif` | generic per-file bounds transform (was A8) — projected folders must filter correctly now |
| 16 | **Float32 NaN nodata** | `f32_nan.tif` | F4 — voids must be `-9999`, not 0 m |
| 17 | **Int32 nodata** | `i32_nodata.tif` | F4 — truncation to 0 |
| 18 | **Pre-built `.abt` dir** | point at a pool dir | Flat listing only, by design |

**Resolutions to sweep:** `2, 5, 10, 30, 90, 250` m. Tile extent and size are fixed per resolution:

| res | extent° | size px | exact res m | file bytes |
|---|---|---|---|---|
| 2 | 0.1 | 5556 | 1.99984 | 62,582,828 |
| 5 | 0.25 | 5556 | 4.99960 | 62,582,828 |
| 10 | 0.25 | 2780 | 9.99200 | 15,657,004 |
| 30 | 0.5 | 1852 | 29.99757 | 7,111,724 |
| 90 | 1.0 | 1236 | 89.89563 | 3,164,204 |
| 250 | 2.0 | 892 | 249.12780 | 1,598,508 |

Use 30 m for routine sweeps; 2 m only for cap testing (62 MB/tile — the engine's terrain atlas is
capped near 3.86 GB and **nothing pre-flights it**, so ~62 tiles is the practical ceiling).

**Geographic edge cases** worth one pass each: negative longitude (no test in the suite uses one),
across the antimeridian (lon 179.9), above 85°N, and below sea level (Dead Sea, Schiphol).

---

## 3. Bypassing the GUI (most reproducible)

Drive the converter directly — no QGIS, no cache, no layer URI parsing.

```bash
aether_converter download --job-file dl.json
aether_converter ingest   --job-file ing.json      # accepts an object OR an array
aether_converter plan --south 47.0 --north 47.5 --west 8.0 --east 8.5 --resolutions 30,90
```

```jsonc
// dl.json — required: url_template, encoding, output_dir, zoom, tiles[]
{"url_template":"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
 "encoding":"terrarium","output_dir":"./out","zoom":12,
 "tiles":[{"filename":"t.abt","ul_lat":47.5,"ul_lon":8.0,"size_px":2048,"resolution_m":10.0}]}

// ing.json — sources[] in priority order (first valid sample wins);
// crs/nodata optional per source; void_fill_m optional (omit to keep VOID)
{"output_path":"./out/t.abt","format":"r16sint","ul_lat":47.5,"ul_lon":8.0,
 "resolution_m":10.0,"size_px":2048,
 "sources":[{"path":"./base_wgs84.tif","crs":"EPSG:4326"}],"void_fill_m":0.0}
// (base_tif/swiss_tifs are deprecated aliases; do not mix them with sources)
```

`plan` prints an `aether-plan/1` JSON document (tile filenames, count, bytes)
that must match the plugin's own enumeration exactly — the Map Converter
worker runs it before every conversion and aborts on any difference.

`encoding` accepts exactly `terrarium` or `mapbox`. Working examples:
`aether-tools/fixtures/jobs/{download,ingest}_job.json`. Schemas:
`aether-tools/schemas/{download,ingest}_job.schema.json`. Validate with
`python3 aether-tools/tools/validate_fixtures.py`.

**Running the same AOI through both `download` and `ingest` and diffing the `.abt` files is the
single most valuable test here** — the two writers currently disagree on pixel anchoring (F2:
download samples cell centres, ingest samples NW corners, so ingest sits half a cell north-west —
5 m at 10 m resolution, 125 m at 250 m).

---

## 4. Cache control — do this between every run

There is no cache-only or offline mode. Options, cheapest first:

```bash
# 1. Invalidate specific tiles (empty sidecar; makes the plugin refetch)
touch ~/.aether/cache/pool/<md5>/tile_N47.50E8.50_30m.abt.rebuild

# 2. Drop one source's pool
rm -rf ~/.aether/cache/pool/<md5>

# 3. Nuke everything
rm -rf ~/.aether/cache/{pool,views}     # or Settings → Terrain cache → Clear
```

Map Converter has a **"Rebuild existing tiles"** checkbox; Site Analysis has no equivalent, so use
`.rebuild` files or delete the pool. `_CACHE_SCHEMA` is `"v4"` — bumping it self-invalidates
everything, which is what any pixel-affecting change should ship with.

Note: pool identity deliberately ignores mtime/size for local files (A15), so **editing a DEM in
place does not invalidate its cache**. Always delete the pool after editing a fixture.

---

## 5. Checking the output

```bash
python3 tools/abt_diag.py --cache <dir> --out report.json   # holes, nodata vs zero, refetch+decode
python3 tools/abt_diag.py --compare <dirA> <dirB>           # determinism between two runs
python3 tools/abt_view.py --cache <dir> --render /scratch/png
python3 tools/scan_abt.py --cache <dir> --zero-frac 0.01    # full zero rows/cols, stride overflow
```

`abt_diag` distinguishes **`nodata`** blocks (all px ≤ −10000 — a correctly recorded void) from
**`zero`** blocks (0 m — usually a silent failure). That distinction is the main thing to look at:
a zero block over land means the provider failed and the pipeline did not notice.

Checklist per run:

- Tile **count** matches the AOI, and file sizes match the table in §2 exactly.
- No zero blocks over land (`abt_diag`).
- Voids read as `-9999`, not 0.
- Re-running produces byte-identical tiles (`--compare`).
- The same AOI via a different provider agrees within the DEM's own accuracy — **cross-provider
  disagreement of 45–55 m in Europe is the vertical-datum gap (F1), not your test.**

Checkerboard measurement is ad-hoc and lives outside this repo:
`AETHER/data/_tmp_los_check/compare.py` (run with cwd set to that directory). Baseline is
**~8.7 % checkerboard energy on real alpine terrain even in an ideal pipeline** — intrinsic to a
running-max horizon test, so do not chase it to zero.

---

## 6. Failure injection

No built-in switches. The real levers:

| Goal | How |
|---|---|
| Total fetch failure | Set layer Max Zoom above the service's real max (Terrarium/Mapzen/Mapbox all stop at **15**) |
| Partial failure | AOI straddling the service's coverage edge, or open ocean |
| Force the slow path | Point `waveshed/binary_dir` at a dir missing any of the three binaries |
| Disable retries | QgsSettings `waveshed/download_max_passes` = **1** (default 4) |
| Throttle | `waveshed/download_connections`, default **256**, floor 32 |
| Network timeout | **Not configurable** — hardcoded 30 s in `download.rs` |
| Disk-space failure | Fill the target volume; the pre-flight raises before any fetch |

Expected messages — match on these, they are contract text:

- Near-total fetch (**strict majority** of tiles failed) → non-zero exit,
  `Tile download failed: {failed} of {attempted} terrain tiles ({pct}%) could not be fetched; …`
- Disk → `Insufficient disk space: need ~{} MB for {} .abt output files, …`
- Plugin, 0 tiles → `ERROR: Rust download z={zoom}: 0 of {total} source tile(s) fetched — nothing
  was downloaded, so these tiles would be flat 0 m terrain.` then falls back to the slow path.
- Below the majority threshold you get only `[Stats] ERRORS (n): timeout=…, HTTP_4xx=…` — a
  partially-failed run still succeeds by design.

`RUST_LOG` does **not** affect `aether_converter` (its log filters are hardcoded); it is set only
for `aether_core`.

---

## 7. Known-broken — don't file these

Confirmed open findings; see `tmp/terrain-audit-plan.html` for the full set.

- ~~A12/E3 (Map Converter renders XYZ through QGIS)~~ — killed 2026-08-12: one acquisition router; XYZ goes through the shared toolkit downloader on both tabs, QGIS renders only true rendered servers (per tile, at the finer of native/finest-output resolution).
- ~~A2 (folder converts via one file), A4 (whole-extent base per tile), A8 (projected folders
  drop everything)~~ — killed by construction by the generic `sources[]` model: every overlapping
  file rides along per tile, filtered by a generic per-file bounds transform.
- **`/vsicurl/` paths get mangled** by `abspath()` (A10).
- **`download` and `ingest` tiles are offset half a pixel** from each other (F2).
- **Bilinear downsampling deletes ridge crests** → optimistic LOS (F3).
- **No vertical-datum handling at all** → 45–55 m cross-source offsets in Europe (F1).
- **No pre-flight against the ~3.86 GB atlas cap**; the warning threshold is 50 GB (F5).
- **The pool has no lock and no atomic write** — concurrent runs corrupt tiles that then cache
  forever, and Cancel does not stop the terrain phase (F6).
- **Polar is unhandled** — past ±85° you get real terrain from 85°N (F7). The
  antimeridian half of F7 is closed: `aether_converter plan` refuses a bbox reaching
  outside `[-180,180] x [-90,90]` and names the crossing, and the Map Converter aborts
  on any plan failure, so it fails loudly instead of wrapping.
- **Grid is square in degrees**, so E-W resolution is over-sampled by 1/cos(lat) (F8).
