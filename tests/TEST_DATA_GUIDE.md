# Test-data guide — building the torture set

How to obtain or fabricate every dataset needed to challenge the terrain and
buildings pipeline, and how to wire each one up in QGIS. Companion to
`TERRAIN_PROVIDERS.md` (which describes *what to exercise*; this file is
*where the data comes from*).

> **Most of this is automated now.** `python3 tools/make_torture_project.py`
> downloads the key-free sources, fabricates the derived fixtures with GDAL,
> and writes a QGIS project — `data/torture/waveshed_torture.qgs` — whose layer
> tree *is* this document: one group per section, one layer per row, each row's
> expectation in the layer abstract. API keys and paths go in
> `torture.local.ini` (gitignored; copy of `torture.local.template.ini`); only
> rows 1.3a, 1.4, 1.8 and 1.9 need one. What got built and what was skipped is
> written to `data/torture/README.md`.
>
> Opening it is two steps: run `data/torture/open_in_qgis.bat` (or open the
> `.qgs`), then click **Enable macros**. The project carries a macro that
> registers all the Browser connections of §1 and §4 — XYZ *interpretation*
> included — serves the local Terrain-RGB fixture of row 1.3b on 127.0.0.1 so
> no second terminal is needed, and reports any layer that failed to load.
> `qgis_console_bootstrap.py` does the same by hand if macros are off in your
> profile. The manual steps below remain the reference — and are what you need
> for a row the tool skipped.

Conventions:
- Put everything under `data/torture/` (gitignored, next to the existing
  swissaltiregio data).
- Run every terrain source through **both** paths — Site Analysis *and* the
  Map Converter. Divergence between them is a bug by definition.
- Clear the cache (Settings → Clear cache) before any A/B terrain comparison.
- Commands assume the OSGeo4W shell (Windows) or any GDAL ≥ 3.6; snippets
  marked *console* run in the QGIS Python console.

> **Known gap while testing (2026-08-12):** the Map Converter's XYZ path still
> renders through QGIS and bypasses the toolkit resampler — XYZ checkerboard
> comparisons are only meaningful in Site Analysis until TODO
> "Map Converter XYZ bypasses the toolkit resampler" is fixed.

---

## 1. XYZ elevation services

**About the two "encodings":** *Terrarium* and *Terrain-RGB* are the two
industry pixel-encoding **formats** for elevation-in-PNG tiles — formats like
PNG vs JPEG, not vendor lock-in. Every elevation tile service on earth serves
one of the two formulas; "mapbox" is merely the historical name of the second
formula. Supporting both IS the generic solution. (A third axis is the image
*codec*: some services serve WebP instead of PNG — see 1.4.)

**Adding an XYZ connection — step by step:**

1. If the Browser panel is hidden: *View ▸ Panels ▸ Browser*.
2. In the Browser panel, right-click **XYZ Tiles** ▸ **New Connection…**
   (Alternative on QGIS ≥ 3.30: *Layer ▸ Data Source Manager ▸ XYZ*.)
3. **Name**: e.g. `Terrarium AWS`. **URL**: paste exactly, curly braces literal:
   `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`
4. **Max. Zoom Level**: tick the checkbox next to the spinbox first — the
   spinbox is greyed out until its checkbox is ticked, which is why it looks
   like "there is no option". Then set 15 (row 1.1) or leave unticked
   (row 1.2 — QGIS then uses its default 18, which is the test).
5. **Interpretation** dropdown at the bottom of the same dialog — exists only
   on QGIS ≥ 3.28. *Terrarium Terrain* for row 1.1; leave *Default* for
   row 1.2. Older QGIS: no dropdown exists; the plugin infers from the URL.
6. OK, then **double-click the new entry** to add it as a layer.
7. **Verify before anything else**: right-click layer ▸ *Zoom to Layer* —
   the canvas must show tiles (greyscale-ish when an interpretation is set,
   map-colored RGB when not). Canvas empty = the connection itself is broken;
   check internet/proxy (*Settings ▸ Options ▸ Network*) before blaming the
   plugin.

Settings live on the **connection**, not the layer — to change zoom or
interpretation, edit the connection (right-click it ▸ Edit) and re-add the
layer.

> The AWS endpoint is alive: all URL variants below answered HTTP 200 on
> 2026-08-12. If a run "doesn't download", suspect the plugin path first —
> until the XYZ-routing fix, the Map Converter never downloaded tiles at
> native zoom at all (it rendered via QGIS) — and re-test after updating
> plugin + binaries.

| # | Service | URL | Setup | Purpose / expected |
|---|---|---|---|---|
| 1.1 | AWS Terrain Tiles (Terrarium), correct | `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png` (alt: `https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png`) | Max Zoom **15**, Interpretation *Terrarium Terrain* | Happy path. Free, no key. |
| 1.2 | Same, worst-case defaults | same URL | deliberately leave Max Zoom at QGIS's default **18** and Interpretation *Default* | Plugin must infer terrarium from the URL and clamp z18→z15 with a log warning. Must NOT produce flat-sea tiles. |
| 1.3a | Terrain-RGB, hosted | `https://api.maptiler.com/tiles/terrain-rgb/{z}/{x}/{y}.png?key=KEY` (MapTiler **v1** — PNG, free key at maptiler.com) | Interpretation *Mapbox Terrain RGB* | The terrain-rgb decode formula — flagged in code as never tested end-to-end. Elevations plausible, not ≈ −32000 m. |
| 1.3b | Terrain-RGB, **local — no key needed** | `http://localhost:8000/terrain-rgb/{z}/{x}/{y}.png` via the re-encoder below | — | Same purpose, fully offline and deterministic. Preferred. |
| 1.4 | MapTiler Terrain-RGB **v2** | `https://api.maptiler.com/tiles/terrain-rgb-v2/{z}/{x}/{y}.webp?key=KEY` | free MapTiler key | **WebP codec — toolkit decodes PNG only** (see TODO: WebP decode support). Until then: must fail loudly naming the decode error, never silent 0 m. |
| 1.5 | Ambiguous encoding | copy connection 1.1, edit URL to e.g. `.../terrarium-terrain-rgb-mirror/{z}/{x}/{y}.png` (any URL containing both `terrarium` and `terrain-rgb`) | — | Must raise the encoding-ambiguity error dialog, refuse to run. |
| 1.6 | Dead endpoint | `https://tiles.example.invalid/terrarium/{z}/{x}/{y}.png` | — | Total fetch failure → hard error, nothing cached, no flat-0 terrain. |
| 1.7 | Imagery XYZ | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | — | Classifier: must appear as imagery, never in the DEM picker. |

**Local Terrain-RGB fixture (1.3b)** — grab a few Terrarium tiles, re-encode
them with the terrain-rgb formula, serve them (`pip install pillow requests`
in the OSGeo4W shell):

```python
# make_terrain_rgb.py — run once, then: python -m http.server 8000
import io, os, requests
from PIL import Image
Z, XR, YR = 12, range(2129, 2133), range(1448, 1452)   # around Bern; adjust
for x in XR:
    for y in YR:
        t = requests.get(f"https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{Z}/{x}/{y}.png", timeout=30)
        img = Image.open(io.BytesIO(t.content)).convert("RGB")
        out = Image.new("RGB", img.size)
        for px in range(img.width):
            for py in range(img.height):
                r, g, b = img.getpixel((px, py))
                elev = (r * 256 + g + b / 256) - 32768          # terrarium -> metres
                v = max(0, int(round((elev + 10000) / 0.1)))     # metres -> terrain-rgb
                out.putpixel((px, py), ((v >> 16) & 255, (v >> 8) & 255, v & 255))
        os.makedirs(f"terrain-rgb/{Z}/{x}", exist_ok=True)
        out.save(f"terrain-rgb/{Z}/{x}/{y}.png")
```

Why this is a valid test and not circular: Terrain-RGB is a **published
interchange formula** — `elevation = -10000 + 0.1 × (R·65536 + G·256 + B)` —
implemented identically by Mapbox, MapTiler and the reference encoder
(`rio-rgbify`). The 5-line encoder above implements the published formula in
Python; the decoder under test implements it independently in Rust. Spec-pinned
checkpoints to rule out a shared wrong constant: **0 m ⇒ RGB (1,134,160)**,
**−10000 m ⇒ (0,0,0)**; verify one output pixel by hand. Anything the decoder
gets wrong (channel order, offset, scale) shows up as hundreds of metres of
error immediately. If you prefer a genuinely independent producer, that is
row 1.3a (MapTiler v1, free key) — same formula, their encoder.

## 2. Local rasters — `data/torture/dem/`

### 2.1 Real DEMs to download

| Dataset | CRS | Where | Notes |
|---|---|---|---|
| swissALTI3D 0.5 m/2 m | EPSG:2056 | You already hold 12 GB of swissaltiregio `.xyz.zip` in `data/swissaltiregio_tiles/`. Convert: `python tools/xyz_to_geotiff.py <in.xyz> <out.tif>` (see the tool's help). Fresh tiles: https://www.swisstopo.admin.ch/en/height-model-swissalti3d → CSV of per-tile links → `curl -O` loop. | The LV95/proj4rs parity case. Output may shift ≤ 1 source px vs pre-refactor — expected, documented in CONTRACT v2.0. |
| Copernicus GLO-30 | EPSG:4326 | `https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N46_00_E007_00_DEM/Copernicus_DSM_COG_10_N46_00_E007_00_DEM.tif` (tile naming: `N<lat>_00_E<lon>_00`). No key. | Geographic passthrough — must be byte-identical to pre-refactor output. Also the `/vsicurl/` remote-COG test: add as raster with `/vsicurl/https://…` prefix. |
| SRTM `.hgt` | implicit 4326 | https://viewfinderpanoramas.org/dem3.html (no login) — pick the Alps zip, extract e.g. `N46E007.hgt`. | Materialize-helper path (non-TIFF). |
| Dead Sea DEM | EPSG:4326 | Copernicus tile `N31_00_E035_00`. | Negative elevations end-to-end + AMSL −430 m site entry. |

### 2.2 Fabricated variants (from any tile above)

```sh
# UTM 32N — generic projected CRS (impossible before the refactor)
gdalwarp -t_srs EPSG:32632 -r near swiss.tif utm32.tif

# Nodata variants
gdal_translate -ot Float32 -a_nodata nan     src.tif f32_nan.tif
gdal_translate -ot Int16   -a_nodata -32768  src.tif i16_nd.tif
gdal_translate -ot Int32   -a_nodata -2147483648 src.tif i32_nd.tif

# Compression / layout torture — the converter reads these DIRECTLY (no GDAL):
gdal_translate -co COMPRESS=DEFLATE src.tif deflate.tif      # should work
gdal_translate -co TILED=YES        src.tif tiled.tif        # tiff-crate tiling — verify!
gdal_translate -of COG -co COMPRESS=ZSTD src.tif zstd_cog.tif  # likely UNREADABLE by
#   the tiff crate → MUST fail loudly naming the file, never silently skip. Open question.

# >512 MB monolith (per-tile window-copy RAM bound):
gdalwarp swiss_tile_*.tif monolith.tif      # mosaic enough tiles to pass 512 MB

# VRT (materialize path):
gdalbuildvrt mosaic.vrt data/torture/dem/swiss/*.tif

# USGS ASCII DEM (exotic format → materialize):
gdal_translate -of USGSDEM src.tif ascii.dem
```

*console* — broken-georeferencing fixtures (fail-loudly tests):

```python
from osgeo import gdal
# no CRS: converter must error naming the file and say `add "crs"`.
ds = gdal.Open(r'data/torture/dem/broken_nocrs.tif', gdal.GA_Update)
ds.SetProjection(''); ds = None
# no geotransform: hard error (filename georeferencing was deliberately removed).
ds = gdal.Open(r'data/torture/dem/broken_nogt.tif', gdal.GA_Update)
ds.SetGeoTransform((0.0, 1.0, 0.0, 0.0, 0.0, 1.0)); ds = None  # GDAL drops the tags
```

### 2.3 Folder cases

- `mixed_crs/` — LV95 tiles + one WGS84 Copernicus tile in ONE folder. Each
  file must be read in its **own** CRS (per-file GeoKeys win; the folder-level
  guess is only a fallback). Terrain must be continuous, nothing offset by km.
- `overlap/` — two tiles covering the same ground with visibly different
  values (e.g. one `gdal_calc -A a.tif --calc="A+50"`). Priority must be
  deterministic: alphabetically-first wins, identical on every run/machine.

## 3. Buildings — `data/torture/buildings/`

| # | Dataset | Where / how | Purpose |
|---|---|---|---|
| 3.1 | swissBUILDINGS3D 3.0 | https://www.swisstopo.admin.ch/en/landscape-model-swissbuildings3d-3-0 — free download, GDB and GPKG flavors | Real GDB → FGB conversion; LV95 vector CRS. |
| 3.2 | Multi-layer GPKG | `ogr2ogr -f GPKG multi.gpkg buildings_a.shp -nln buildings` then `ogr2ogr -f GPKG -update multi.gpkg other.shp -nln other_layer` — then add the **second** layer in QGIS | The `\|layername=` sublayer fix — was silently broken; the converter must receive the *selected* layer. |
| 3.3 | OSM Overpass | plugin's own "Download OSM buildings" button over a city | Built-in path incl. height tags. |
| 3.4 | OpenFreeMap PBF (Site Analysis) | tick "OSM buildings" on a coverage run | z14 MVT fetch; then repeat with network cut → run must FAIL, not cache building-free terrain under a buildings key. |
| 3.5 | Wrong-CRS shapefile | `ogr2ogr -t_srs EPSG:2056 lv95.shp osm.geojson` | Reprojection-on-conversion path. |
| 3.6 | FGB passthrough | `ogr2ogr -f FlatGeobuf direct.fgb buildings.gpkg` | Must not be re-converted, and must not be handed over with wrong CRS. |

## 4. Servers (beyond XYZ)

**Adding a WMS/WMTS connection — step by step** (this is where the earlier
guide broke: it gave a capabilities URL with `SERVICE=`/`REQUEST=` already in
it — **QGIS appends those parameters itself, and the duplicated parameters
make the request fail**; connection URLs must be bare):

1. Browser panel ▸ right-click **WMS/WMTS** ▸ **New Connection…**
2. **Name**: `swisstopo WMS`. **URL**: exactly `https://wms.geo.admin.ch/?`
   — nothing after the `?`. (Optionally `https://wms.geo.admin.ch/?lang=en`.)
3. OK, then expand the new entry in the Browser. The first expansion
   downloads a very large capabilities document (~700 layers) and can take
   30–60 s — if it times out, raise *Settings ▸ Options ▸ Network ▸ Timeout*
   to 60000 ms and try again.
4. Scroll/search to the layer (names below), double-click to add. If asked,
   pick CRS EPSG:2056 or 3857 and image format PNG.
5. WMTS: same dialog family — right-click **WMS/WMTS** works for both; the
   swisstopo WMTS capabilities URL is
   `https://wmts.geo.admin.ch/EPSG/3857/1.0.0/WMTSCapabilities.xml` (this one
   IS a full document URL and is used as-is — WMTS connections take the
   capabilities document, WMS connections take the bare endpoint).
6. WCS: Browser ▸ right-click **WCS** ▸ New Connection… URL bare:
   `https://www.wcs.nrw.de/geobasis/wcs_nw_dgm?` — same no-parameters rule.

| # | Service | Connection URL (bare) | Layer | Expected |
|---|---|---|---|---|
| 4.1 | WMS imagery | `https://ows.terrestris.de/osm/service?` | `OSM-WMS` | Classifier: **imagery** — never offered as DEM (ARGB32 band-heuristic trap). |
| 4.2 | WMTS imagery | `https://wmts.geo.admin.ch/EPSG/3857/1.0.0/WMTSCapabilities.xml` | `ch.swisstopo.pixelkarte-farbe` | Same. |
| 4.3 | WCS elevation | `https://www.wcs.nrw.de/geobasis/wcs_nw_dgm?` | DGM coverage | Coverage provider → real Float32 values; band heuristics may classify as DEM. Third real CRS (EPSG:25832) end-to-end. |
| 4.4 | ArcGIS ImageServer | `https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer` (Browser ▸ ArcGIS REST Servers ▸ New Connection) | 3DEPElevation | Rendered-provider classification path. |
| 4.5 | swisstopo WMS | `https://wms.geo.admin.ch/?` | `ch.swisstopo.pixelkarte-farbe` | WMS imagery classification (free, no key). |
| 4.6 | swisstopo WMS, hillshade trap | `https://wms.geo.admin.ch/?` | `ch.swisstopo.swissalti3d-reliefschattierung` | **Looks like terrain, IS a rendered picture.** Classifier must call it imagery — picking it as DEM is the exact silent failure the URL/provider guard exists for. |

## 4b. CRS support — what the toolkit transforms, and how failures surface

Probed against the shipped proj4rs/crs-definitions stack (2026-08-12), one
representative per projection family, all in-domain points:

- **Working** (transform verified, plausible coordinates): geographic (4326 —
  bypasses proj4rs entirely), Web Mercator 3857, LV95 2056 (somerc),
  UTM/ETRS 32632/25832/26915, France Lambert-93 2154 (lcc), EU LAEA 3035,
  OSGB 27700 (tmerc), NZTM 2193, Finland 3067, Sweden 3006, Dutch RD 28992
  (sterea), Hungary EOV 23700 (somerc), Gauss-Krüger 31467, California
  State Plane in **US survey feet** 2229 (`+units` honored — feet/metre ratio
  measured 3.28083; the GeoTIFF geotransform of a feet CRS is also in feet,
  so sampling stays consistent), polar stereographic 3413/3031.
- **Not supported, fails CLEANLY**: Czech Krovak 5514 → "Projection not
  found" at `Proj::from_proj_string` → ingest hard-errors naming the CRS
  before any tile is written. Ready-made test: `gdalwarp -t_srs EPSG:5514
  src.tif krovak.tif`, feed it, expect that error.
- **Codes missing from crs-definitions** → "unknown EPSG code" hard error,
  message suggests supplying a proj string in the source's `crs`.
- **Datum caveat**: proj4rs applies parameter-based (`+towgs84`) datum
  shifts only — **no NTv2/NADGRIDS grid files**. For grid-based CRS (OSGB,
  DHDN, NAD27…) accuracy is the Helmert approximation, typically metre-level:
  irrelevant at 30 m terrain, visible below ~5 m. Documented, not silent.

Every failure mode observed is a **hard error at job start**; none produced
NaN or silently wrong coordinates in the probe set.

## 5. Geometry run matrix

Run each as a Map Converter conversion AND (where sensible) a coverage run:

| Case | BBox / site | Stresses |
|---|---|---|
| Andes | S −34.9 N −34.5 W −71.2 E −70.6, 30 m | Negative coords through `plan` (regression: clap fix). |
| London | W −0.3 E 0.4 around 51.5 N | Prime-meridian sign crossing. |
| 2 m @ 47.4°N | S 47.35 N 47.62 E 8.21–8.77, res 2 (+30) | The 0.1° snap grid (regression: double-snap fix). |
| Norway | S 59.9 N 60.3 W 10.4 E 11.1, 5+90 m | cos(lat) halo; check tile seams at edges. |
| Dead Sea | site at ~31.5 N 35.5 E, height −430 **AMSL** | Negative-AMSL entry allowed; AGL floor untouched. |
| One tile | any 0.05° box | Minimal case. |
| Huge | half a continent at 2 m | Must hit the size warning / 2 M-tile cap error — never hang. |
| Fiji | bbox across ±180° | Antimeridian is unsupported: document behavior — must fail loudly, not wrap silently. |

## 6. Operational drills

- Swap an **old** `aether_converter` into the binary dir → Map Converter must
  abort: "engine binaries predate this plugin version".
- Kill the network mid-download (Site Analysis) → second run heals via
  `.rebuild` flags; no permanently-poisoned pool.
- Same area twice → pool hit (fast); re-spelled XYZ URI (reorder params) →
  view rebuilds, pool reused; Settings → Clear cache works.
- Batch CSV rows: 0.5 m AGL (line-numbered rejection), −430 m AMSL (accepted),
  non-numeric altitude (rejection), bad mode string (rejection).
- 0.5 m in every height input: Site table, P2P A/B, both Processing algorithms,
  Asset default — all reject; switching to AMSL opens the minimum to −500 m.
