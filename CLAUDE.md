# Waveshed QGIS Plugin - Claude Code Guide

Waveshed is a QGIS plugin providing GUI access to the AETHER RF propagation engine. Pure Python wrapper — all computation is done by external Rust binaries (aether_core, aether_converter, aether_export). The plugin (product) is "Waveshed"; the engine keeps its "Aether" name.

## Architecture

```
waveshed/             Plugin package (installed into QGIS plugins dir)
  __init__.py          classFactory() entry point
  metadata.txt         QGIS plugin registry metadata
  plugin.py            Menu/toolbar integration, init/unload lifecycle
  provider.py          QgsProcessingProvider registration
  algorithms/          QgsProcessingAlgorithm implementations
  gui/                 Qt dialogs, hand-built in Python (no .ui files)
  core/                Business logic (no GUI imports)
  resources/           Icons, color ramps
tests/                 pytest unit + integration tests
```

## Key Constraint: Zero Changes to AETHER Binaries

All adaptation happens in this plugin. Communication with binaries is exclusively through:
- JSON config files (written by plugin, read by binaries)
- Subprocess invocation (stdin/stdout/stderr)
- File outputs (.bit/.tiles, .json sidecar, .tif)

## Binary Interfaces

| Binary | CLI | Input | Output |
|--------|-----|-------|--------|
| `aether_core` | `--config <job.json>` | Job config JSON, .abt terrain tiles | .bit/.tiles + .json sidecar |
| `aether_converter` | `ingest --job-file <job.json>` | Ingest job JSON, GeoTIFF | .abt terrain tiles |
| `aether_export` | `-i <.bit/.tiles> -j <.json> -o <.tif>` | .bit/.tiles + .json sidecar | Cloud-Optimized BigTIFF |

Every binary answers `--version` with `<name> <semver>` (toolkit binaries since
converter 0.2.5 / export 1.0.1). `binary_manager.check_engine_for_job` runs
`aether_core --version` at the start of every run, logs the engine path and
version, and refuses 90 m / 250 m on engines older than
`COARSE_RESOLUTION_MIN_ENGINE` (0.4.3) before any terrain is fetched; an engine
that does not know the flag counts as the oldest known build.

## Job Config JSON Structure (aether_core)

Defined in `rust/aether_core/src/config.rs`. Sections: tx, rx, analysis, output, processing, propagation.

- `analysis.resolution_m` is an f32; the plugin offers [2, 5, 10, 30, 90, 250] (see `job_builder.VALID_RESOLUTIONS`). The engine itself accepts any value ≥ 0.1 m — this list is a plugin-side convenience, kept in sync with `terrain_adapter.ABT_EXTENT_DEG`, which maps each resolution to its `.abt` tile extent in degrees and is the single source of truth for both terrain paths (Site Analysis and Map Converter). Tile extent is an engine constraint, not just a file-size one: every tile a wedge touches goes into one contiguous terrain-atlas allocation capped at ~3.86 GB, so oversized tiles get the job rejected.
- `analysis.task_type`: "SINGLE" (coverage), "P2P", "BATCH_P2P"
- `analysis.propagation_model`: "LOS", "SIMPLE_LOSS", "ITM"
- `analysis.compute_backend`: "AUTO", "GPU", "CPU"
- `tx.mode` / `rx.mode`: "AGL" or "AMSL"
- `propagation.radio_climate`: integer 1-6 (ITM spec)
- `propagation.earth_radius_mode`: "FOUR_THIRDS" or "ADVANCED"

## Converter Ingest Job JSON Structure

Defined in `aether_converter/src/ingest.rs` (contract v2.0):
```json
{
  "output_path": "path/to/output.abt",
  "format": "r16sint",
  "ul_lat": 47.5, "ul_lon": 8.0,
  "resolution_m": 10,
  "size_px": 4096,
  "sources": [
    {"path": "overlay.tif", "crs": "EPSG:32632"},
    {"path": "base.tif", "crs": "EPSG:4326"}
  ],
  "void_fill_m": 0.0
}
```

- `sources[]`: priority = array order (first valid sample wins per pixel).
  `crs` is optional (`"EPSG:nnnn"` or a proj string); when absent the
  converter reads the GeoTIFF's GeoKeys and hard-errors if they are
  user-defined/absent — so the plugin states it for every file it knows a CRS
  for, as a proj string when there is no EPSG code (see Terrain folders). `nodata` is optional per source (else `GDAL_NODATA`
  tag). The converter reprojects while sampling — the plugin never warps.
- A source `path` may also be a pool **`.abt` tile** (detected by its `AETH`
  magic): self-describing — geometry from its own header, values already
  half-metres. Setting `crs` or `nodata` on a `.abt` source is a converter
  hard error. The Map Converter uses this for XYZ terrain: the shared
  downloader (`terrain_adapter.ensure_pool_tiles`) fills the Site-Analysis
  pool, and the pool tile becomes an ingest source.
- Both tabs acquire terrain through ONE router: XYZ → shared Rust
  `download`; local files/folders → direct `sources[]`; only true rendered
  servers (WMS/WMTS/ArcGIS) are exported through QGIS, per tile. The single
  engine-subprocess entry point is `terrain_adapter.run_converter_streaming`.
- `void_fill_m` is optional: pixels no source covers are written as this
  elevation instead of the VOID sentinel. Site Analysis passes `0.0`
  (0 m ground outside the DEM); the Map Converter tab omits it (VOID kept).
- **No-data contract (2026-08-31, CONTRACT items 17/18): ground a source has
  no data for degrades to sea, loudly — never an error.** Download: HTTP 404
  is "no tile here" (own error class + `[Stats] NO-DATA (HTTP 404):` line),
  excluded from the majority-fatal rule — a run over or past a bounded
  source's edge exits 0 with voids there; only real failures
  (timeout/connect/403/429/5xx/decode) can majority-abort. Ingest: a batch
  with zero covered pixels exits 0 with `[Warn] NO-DATA batch:` instead of
  bailing. Plugin side: an all-404 miss set is a COMPLETE download (no
  rebuild flags), `_warn_no_data_tiles`/`_warn_if_bbox_exceeds_bounds` (and
  the rendered-layer extent check) tell the user "assumed 0 m (sea level)",
  and `_sync_view` fills pool-tile voids to 0 m in the Site Analysis VIEW
  only — the pool keeps VOID for the Map Converter, whose output keeps VOID
  by contract. The torture suite's `overrun:` checks pin all of this per
  acquisition route. Rendered servers (the per-tile QGIS export) state the
  same contract through `rendered_export_gap`: `writeRaster` reports success
  for an answer that arrived with holes, so the export is measured against
  the LAYER's own published extent — empty ground outside it is the coverage
  edge (already warned by bounds), empty ground inside it is a partial
  answer and gets its own warning. Without it a dropped block reached the
  user as silent sea level, visible only as a cross-tab terrain
  disagreement (row 4.3, 2026-09-20: up to 402 m).
- **Degraded download tiles** (toolkit protocol, 2026-10): a source tile whose
  fetch failed and was filled from a coarser zoom prints `[Degraded] z= x= y=
  reason=… filled_from=z<k>` (+ `[Stats] DEGRADED: n of total …`) and is NOT
  counted in `[Stats] Tiles: ok/total OK`. Such an output tile has no hole, so
  `_download_zoom_group` maps each line (`_xyz_tile_bbox`) onto the output
  specs it intersects (`_specs_hit_by_degraded`), retries those at reduced
  connections, and after the last pass keeps them for this run with a
  `.rebuild` flag plus the warning "N terrain tile(s) were filled from coarser
  data (z…) … re-downloaded next run". A degraded pass never takes the
  complete, all-404 or total-failure branches.
- `base_tif` / `swiss_tifs` are DEPRECATED aliases still accepted by the
  converter for older callers; the plugin no longer emits them, and emitting
  them alongside `sources` is an error.
- The converter also has a `plan` subcommand
  (`plan --south S --north N --west W --east E --resolutions 30,90`) that
  prints the tile grid as JSON (`aether-plan/1`); the Map Converter worker
  cross-checks it against the plugin's own enumeration before every run and
  aborts on mismatch (or on an engine too old to have `plan`).

## Terrain folders (`core/terrain_index.py`, `gui/terrain_folder_scan.py`)

A local terrain folder (Settings → Local terrain dir, Map Converter → Add
Folder) is read through ONE persistent footprint index per folder
(`<cache>/index/<md5(folder)>.json`): per file size+mtime (change detector),
readable flag + reason, driver, CRS (WKT / EPSG authid / proj string),
native + WGS84 bounds and a densified WGS84 footprint ring, pixel size
(native + metres), dtype, bands, compression, rotation, scale/offset, nodata.
Pure GDAL/OSR, thread-safe. Rules:

- **Never on the GUI thread.** `FolderIndex.refresh` stats every file
  (threaded) and probes only new/changed ones in a 16-thread pool. The GUI runs
  it through `terrain_folder_scan.scan_folder` (QThread + progress dialog +
  local event loop), also for the coverage check. Measured on 2,500 small LV95
  GeoTIFFs on the Windows mount: first scan 11.5 s (was 50 s per run through
  `source_file_info`), repeat 0.6 s, coverage 0.2–1.3 s off-thread (was 74 s
  of BuildVRT on the GUI thread). A run re-checks each index once
  (`reset_terrain_warnings` / the Map Converter worker call `expire_all`);
  jobs of one run reuse it for `FRESH_S`.
- **Scan** = every GDAL raster extension in `RASTER_EXTS` plus `.zip` members
  (`/vsizip/`), recursive. Excluded by construction: `.vrt` (a mosaic of the
  files beside it — scanning it duplicates them; reason in `EXCLUDED_EXTS`;
  torture `folder:vrt-ignored`), sidecars (`.aux.xml`, `.ovr`, `.msk`, world
  files, `.hdr`), `._*`. File-name order sorts a zip member where its archive
  sorts (`name_key`).
- **Problem files are asked, never dropped or guessed.** `problem_of`:
  unusable (cannot open, no geotransform, truncated — bottom-right block read,
  ≥2 bands / colour table = picture, CRS not transformable), no CRS, 8-bit
  single band ("not elevation?"). One prompt (`confirm_problem_files`: "Skip
  N unusable file(s) and continue?", default Cancel; skipped files logged).
  Site Analysis lists CRS-less files there; the Map Converter asks a CRS for
  them first (`ask_crs_for_files`: the folder's dominant CRS offered, for all
  or per file; a CRS the coordinates cannot be in is refused). A folder
  entry's `crs_authid` is display only — files NEVER inherit it. Answers are
  `terrain_index.approve` (Site Analysis, bound to size+mtime) or the entry's
  `decisions`/`crs_overrides` (Map Converter). Unanswered problems are a hard
  error naming every file (`source_infos`) — Processing and the torture
  runner see that.
- **Routing by metadata, not extension** (`materialize_reason`): non-GTiff,
  `/vsi*`, compression outside NONE/LZW/DEFLATE/PACKBITS (ZSTD/LERC abort the
  converter batch), rotated/flipped geotransform (`gdal.Warp` to north-up in
  the SAME CRS — the one resampling, unavoidable), scale/offset (copied as
  Float32 metres via `unscale`, so it can neither be lost nor applied twice
  whatever the toolkit does), odd NBITS, > 512 MB. Non-EPSG CRSs go to the
  converter as a proj string (`crs_arg`).
- **Overlap priority** per folder (`waveshed/overlap_priority/<md5>`): finest
  resolution wins (default; ties = file-name order) or file name order (warns
  when a coarser file then beats a finer one). The effective order is logged.
  UI: Settings "Where files overlap:" and the Map Converter row combo.
- **Coverage** (`coverage`) from per-file WGS84 footprints on a 512² grid:
  covered %, and holes INSIDE the data (sandwiched between covered cells,
  ≥3×3 cells) reported even above the 98 % overhang tolerance.
- **Per-tile invalidation.** Pools stay keyed on the folder path
  (`_schema_for`/`_FILE_SOURCE_SCHEMA` unchanged); each file-built pool tile
  has a `<name>.src` sidecar = `tile_source_fingerprint` (path + size+mtime +
  CRS + route of exactly the files `_tile_contributors` admits, in priority
  order, `_SOURCE_ROUTE_VERSION`). Missing/different = stale, rebuilt alone.
  Views are unchanged (`_sync_view` refreshes by mtime). The Map Converter
  keeps `.waveshed_tiles.json` in its output dir and rebuilds tiles whose
  stack fingerprint changed even without "Rebuild existing tiles"; tiles with
  no record keep the old skip rule (logged).

**Index speed (2026-10-14).** `terrain_index.probe_geotiff_fast` reads an
ordinary GeoTIFF's first IFD from one 64 KB read (no GDAL; CRS + WGS84 transform
looked up once per EPSG code) — one band, standard sample type, known
compression, one tiepoint or a matrix, an EPSG code, no sidecar (`.aux.xml`,
`.prj`, world file…); anything else returns None and goes through the GDAL
probe, so unusual files are judged exactly as before
(`tests/test_terrain_index_fast.py` holds both probes to identical entries,
incl. the torture fixtures). The scan takes sizes/mtimes from the directory
listing (free on Windows, parallel `stat` elsewhere), sidecars are looked up in
the listing, the probe reuses the scan's stat, and the index is written in one
`write`. 2,500 files on the container's Windows mount: first scan 24.9 s →
3.3 s (raw open+read floor there 1.35 s), repeat 1.2 s → 0.7 s.

## Map Converter progress and extents

**One bar, weighted by work (`core/run_progress.py`).** No fixed phase spans.
`RunProgress` holds slots of estimated work in one unit (a downloaded 256²
source tile; convert/render per 256² output block, `*_UNITS_PER_BLOCK`):
`("render", i)` only for entries that render, `("download", k, res)` per
`ensure_pool_tiles` call, `("convert",)`. `_on_run` estimates (geometry only,
no pool stat on the GUI thread), the worker refines (pool-checked, exact jobs).
Remaining-work rule — finishing Δ advances `f += Δ(1−f)/remaining` — so the bar
never goes back, moves with every finished unit (retry passes ADD their work
when they start), and is 100 % only when all slots are done. Between converter
lines (download: every 10 % of a pass; ingest: every 10 tiles) it may creep at
the smoothed rate up to the next certain report (`next_download_report`). Only
existing toolkit lines are parsed: `[Download] z= tiles=AxB=N` (pass total up
front), `[Download] X% (a/b) — MB/s, errors, in-flight`, `Progress: a/b`.
`DownloadProgressAggregator` counts source tiles per zoom group + a 1 % tail held
until the group's outcome is known; `progress_cb(frac, label)` stays the
contract (Site Analysis, Processing), a callback with a `download_event(info)`
attribute also gets the counts. The worker's `status` texts are unchanged (the
torture runner filters them); the tab's label comes from the model.

**Extents.** Rows: ExtendedSelection — Draw Extent / Extent from Layer / Paste /
Remove act on all selected rows; Up/Down/overlap rule on the current row.
Extents are copied per row (`_apply_extent`), never shared. A folder row's
extent defaults to the union of its usable files' `wgs84_bounds` from the scan's
index (`_folder_extent`; skipped/unusable files excluded, CRS-less files only
with the user's CRS); "Whole folder (all files)" in the picker re-scans
off-thread. Add All Layers prefills each file layer's own extent; services
(XYZ/WMS) get none — their extent is the whole service.

## Adaptive / custom resolution (`core/range_profile.py`)

Site Analysis offers Fixed / Adaptive / Custom next to the resolution. A profile is
`analysis.range_profile` (engine >= `RANGE_PROFILE_MIN_ENGINE`, 0.4.9 — older engines
ignore the field and would run uniformly at the coarse step, so
`check_engine_for_job(..., range_profile=True)` refuses them before any download).
An adaptive site is ONE job: the profile with `resolution_m` = the chosen (base)
resolution, which stays the output pixel size everywhere — one `.bit`, one `.tif`,
one layer (`SiteAnalysisTab._build_jobs`). Band steps come from `VALID_RESOLUTIONS` and must
have nesting tile extents (2 m only steps to 30/90/250). Terrain:
`terrain_adapter.prepare_terrain_profile` builds each band's tiles only where its ring
needs them and never a footprint a finer band holds (`min_range_km` /
`skip_footprints`, also in `terrain_plan` for the pre-run estimate), then links all
bands into one view.

**Output pixel is explicit.** `resolution_m` is the map grid — one pixel size for
the whole result, never several. Fixed: = the resolution. Adaptive / Custom: the
user picks it ("Output pixel (m)", any size ≥ the first step); the profile steps
only set ray sampling (`_output_res`, `_build_jobs`). Verified 2026-10-13: a 30 m
output of a 2 m-start profile equals the 2 m output at the same points (MIN_ALT,
LOS, ITM), exports to an ordinary 30 m GeoTIFF; each pixel is a point sample.

**No silent tile picking.** aether_core keeps, per footprint, the tile closest to
`resolution_m` and lets finer footprints win — silently. The plugin therefore
never hands the engine a choice: its own views hold one tile per footprint, and a
pre-built `.abt` folder goes through `select_abt_tiles` (per footprint the tile
matching the finest step that footprint needs) into an `abt_selection_view`.
Footprints lacking that resolution are listed in a pre-run Yes/No dialog
("Terrain resolution not available") and logged as `terrain substitution`.

**Buildings per band.** `rp.band_buildings(bands, overrides, master)`: Adaptive
gives buildings to bands with step < `BUILDINGS_SKIP_STEP_M` (90) only; Custom
keeps per-band choices keyed by step (`_custom_buildings`, session-only like
`_custom_bands`; missing steps follow the same rule); "Include buildings" is the
master switch. Flags ride in `CoverageParams.range_profile_buildings` (never in the
job JSON) to `prepare_terrain_profile(band_buildings=...)`, which builds each band
into its own pool (with / without buildings) and links both into one view whose
key encodes which steps carry buildings; `terrain_plan_profile` prices the same.
A Custom site whose range ends inside the first band runs as a fixed job but keeps
that band's flag (one-element `range_profile_buildings`, read by the worker's fixed
path and the estimate). Multi-site (2026-10-19, Rigi/Pilatus, real terrarium +
OpenFreeMap): sites share pool tiles, each gets its own view; pinned by
`MultiSiteBuildingsViews`.

## Sensitive Files (DO NOT commit)

- `api_private_key.bin` / `api_private_key.pem` - Ed25519 signing key (server-side only)
- `vendor_keys.json` - Master seed + keypair (AETHER proprietary builds)
- `api_key.txt` - User API keys
- Any `*.key` files

These are covered by `.gitignore`. The plugin embeds no licence key at all —
licence signatures, node-lock and revocation are verified by the engine. The
only key it carries is the **public** release-manifest key in
`waveshed/core/release_keys.py` (see Engine distribution); the matching private
key lives in the AETHER vendor keys / CI secrets, never here.

## Code Conventions

- snake_case everywhere (Python, JSON keys, file names)
- Qt for all GUI, imported through `qgis.PyQt` only — never `PyQt5`/`PyQt6`
  directly. The plugin runs on QGIS 3.28+ (Qt5) **and** QGIS 4 (Qt6), so every
  Qt/QGIS enum is spelled with its scope (`Qt.CheckState.Checked`,
  `QMessageBox.StandardButton.Yes`, `QgsWkbTypes.GeometryType.PointGeometry`),
  `.exec()` not `.exec_()`, and `QAction`/`QActionGroup`/`QShortcut` are
  imported with a `try: from qgis.PyQt.QtGui` / `except ImportError:
  from qgis.PyQt.QtWidgets` pair (Qt6 moved them). Scoped names work on the
  PyQt5 QGIS 3.28 ships, so this costs nothing on Qt5.
  `tests/test_qt6_compat.py` enforces all of it; `tests/conftest.py` mirrors it
  by stubbing scoped enums only.
- GDAL access via `osgeo` (QGIS bundles it) — never add GDAL as a pip dependency
- Use `QgsSettings("waveshed/...")` for persistent plugin settings (the engine env var `AETHER_BIN_DIR` keeps its `AETHER` name)
- Use `QgsMessageLog` for debug logging, `QgsMessageBar` for user-facing messages
- Type hints on all public functions
- No external pip dependencies beyond what QGIS provides (PyQt5, osgeo/GDAL, numpy).
  There are no exceptions — `core/api_key.py` validates keys structurally
  (Base58 charset + payload length) with the standard library alone; the
  Ed25519 signature is verified by the engine binary, so nothing imports
  `nacl`; the one signature the plugin checks itself (the release manifest)
  goes through the pure-Python verifier `core/ed25519.py`. Keep it that way: a
  pip dependency is a support burden in a QGIS plugin, which cannot install one.

## Testing

```bash
pytest tests/ -v --tb=short
```

Unit tests mock QGIS APIs. Integration tests require QGIS environment.

## Engine distribution (binary_manager)

The engine is never bundled; Settings → Download Binaries fetches it on demand
after EULA consent (asked every time). Rules, all in `core/binary_manager.py`:

- **Signed manifest.** `fetch_manifest` is the only way any code gets a
  manifest (Download, the startup update check, Check for updates). It checks
  `https://releases.waveshed.io/manifests/v<version>/latest.json.sig`
  (JSON `{alg: ed25519, context: waveshed-manifest-v1, public_key, signature}`)
  over `b"waveshed-manifest-v1\0" + the exact manifest bytes` — the
  waveshed.io relay serves them verbatim. Valid + trusted key → ok; bad /
  untrusted / malformed → refuse; HTTP 404 → allowed only for versions
  `<= LAST_UNSIGNED_ENGINE` (0.4.7, published before signing; never raise it);
  any other fetch error → refuse. The version must be X.Y.Z before it is put
  in the URL.
- **Trusted keys** live in `core/release_keys.py` (`MANIFEST_PUBLIC_KEYS`,
  64-hex strings). That file is rewritten by the AETHER release GUI — keep the
  documented tuple format. Empty tuple = every signed manifest refused;
  `package.py --release` refuses to build then. Rotation = ship the new key
  next to the old one first.
- **Host pin.** Asset URLs must start with `https://releases.waveshed.io/`.
- **Staged, all-or-nothing install.** The archive is SHA-256-checked
  (fail-closed), extracted into `<target>/.waveshed-staging-*`, must contain
  all three binaries, gets `chmod +x` + quarantine clearing + `revocation.bin`,
  and `aether_core --version` must report the manifest version. Only then
  `_install_staged` swaps files in (backup-rename, move, roll back on any
  error; Windows refuses up front when a binary is locked by a running job).
  Files the archive does not contain (`license.key`) are never touched;
  leftovers are removed by the next install.
- **macOS: no notarization, by decision.** The ad-hoc-signed engine runs once
  the download quarantine is cleared. Like MPT_SIGMA, `prepare_engine_dir`
  (`chmod +x` + `xattr -cr`) runs at plugin load, after Browse/Auto-detect,
  and before every `--version` probe; a Gatekeeper-looking failure gets the
  `xattr -cr "<dir>"` hint (`macos_launch_hint`).
- **No engine launches on the GUI thread when the dialog opens or closes.**
  `probe_engine_version` caches successful answers per binary (path, size,
  mtime) and `_vulkan_issue` caches the Linux `vulkaninfo` check per session;
  the Settings tab probes in an `_EngineProbeThread` on open and the close path
  saves with `save_settings(revalidate=False)`. Launching `aether_core
  --version` synchronously (an AV-scanned exe on Windows) froze open and close
  for 2–3 s.
- **Notices** (`gui/engine_notices.py`): first start without an engine →
  message bar with "Open Settings"; once per session (switch
  `waveshed/engine_update_check`, default on; silent offline) a newer release
  → one notice per version (`waveshed/engine_update_notified_version`) with
  "Update" → Settings download flow. Run errors that say "Settings →
  Download binaries" get an "Update engine" / "Open Settings" button
  (`show_run_error`). Nothing ever downloads without the consent dialog.

## Build / Package / Release

Plugin is distributed as a ZIP (build with `python3 package.py` →
`dist/waveshed.<version>.zip`; `--release` additionally requires public
metadata URLs and a trusted manifest key).

Before uploading to plugins.qgis.org, run `python3 tools/release_check.py`
(the pipeline GUI's *Run checks*): the full test suite — failures, errors AND
skips fail it — then `tools/qgis_scan.py`. Needs GDAL + numpy + pytest and
`aether_converter` (`--bin-dir`, `AETHER_BIN_DIR` or `~/.aether/bin`). The GUI
runs it in Docker (`tools/check_container/Dockerfile`: bookworm PyQGIS/GDAL/
numpy from apt, pinned pytest + analysers in a `--system-site-packages` venv,
`WAVESHED_SCAN_TOOLS` points the scan at them) with AETHER's `bin/linux`
mounted — the owner does not want check tooling installed on the Windows host.
`--install-tools` (pytest + a `tmp/` analyser venv) is for the dev container only.
`tools/qgis_scan.py` alone: it
builds the ZIP and runs the registry's four scans (Bandit, detect-secrets,
flake8, file analysis) with the registry's own Critical/Warning/Info grading;
exit 1 on any Critical. The analysers live in a venv in the gitignored `tmp/`
(setup in the script's docstring), never as plugin dependencies. Names that
merely contain `token`/`password`/`secret` trip Bandit B105 (Critical) even for
harmless values; `package.py` writes every ZIP entry 0644 because the registry
flags executable bits.

Release = push tag `v<metadata version>`. `.github/workflows/release.yml` runs
the full test suite (distro Python + python3-gdal, so nothing is skipped for
want of GDAL), `package.py --release`, `tools/make_plugin_repo.py` (writes
`plugins.xml` + `latest.json` from the metadata *inside* the ZIP), uploads to
R2 `waveshed-releases` — `qgis/waveshed.<V>.zip` (immutable, never
overwritten), then `qgis/latest.json`, then `qgis/plugins.xml` — and waits
until `https://waveshed.io/qgis/latest.json` / `plugins.xml` (AETHER_Web
relays) serve the new version, then re-downloads the ZIP and checks its
SHA-256. Secrets: `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_S3_ENDPOINT`.
`workflow_dispatch` is a dry run (no upload). QGIS users add
`https://waveshed.io/qgis/plugins.xml` as a plugin repository; plugins.qgis.org
is the later, separate channel.
