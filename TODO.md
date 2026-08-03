# Waveshed — TODO

Running list of follow-up work. Keep items short; link to the code site.

## Terrain / live download

- [ ] **Test the live XYZ download with services other than Mapzen Global
  Terrain.** The Rust live-download path (`waveshed/core/terrain_adapter.py`,
  `_try_rust_download`) has so far only been exercised against Mapzen Global
  Terrain (Terrarium encoding). Verify it end-to-end with other elevation-tile
  providers as well — e.g. AWS Terrain Tiles, Mapbox Terrain-RGB, and Nextzen.
  Check: encoding detection (terrarium vs. mapbox), `{z}/{x}/{y}` URL
  templating, per-service `zmax`/zoom limits, and any API-key / attribution
  requirements.

- [ ] **Verify the local-DEM border fix.** Coverage used to stop at the edge of
  a local GeoTIFF DEM when the analysis range extended past it. Believed fixed
  (`terrain_adapter.py` warps with `INIT_DEST=0` and strips the nodata flag so
  beyond-DEM pixels read as valid 0 m ground) but **not yet confirmed on a real
  run**. Test: a site near the edge of a swissALTI-style DEM with a range that
  reaches well past the border, and check coverage continues beyond it.

## Buildings

- [x] **Buildings in Site Analysis, from OpenFreeMap.** Done. `openfreemap.py`
  fetches z14 MVT tiles into `<cache>/_buildings/` as `{z}_{x}_{y}.pbf`;
  `prepare_terrain(osm_buildings=True)` sets the converter's new
  `buildings_pbf_dir` job field. The `.abt` cache key now carries a buildings
  dimension, which is **load-bearing**: an above-ground height is measured
  against the surface it is written onto, so re-applying buildings to a tile
  that already has them makes them grow. Downloads are capped at 4096 tiles
  (buildings only exist at z14, so a large range is an enormous fetch).
  The direct XYZ downloader is bypassed when buildings are requested — it
  writes `.abt` straight from the tile stream and cannot fuse an overlay.

- [ ] **Buildings in the P2P tab.** Site Analysis has the option; P2P does not.
  The plumbing (`prepare_terrain(osm_buildings=...)`) is already there.

- [ ] **Verify buildings end-to-end against a real run.** The Rust and Python
  sides are unit-tested and the tile-name contract is pinned on both sides, but
  no real OpenFreeMap fetch → converter ingest → coverage run has been done.

- [ ] **Check AETHER_Web still renders buildings correctly.** The shared
  rasterizer changed the vector-tile write rule from "add per pixel" to "max of
  an absolute roof". Roofs are now flat instead of draped over slopes and
  overlapping footprints no longer stack — both fixes, but the web app's output
  will visibly change and should be eyeballed.

## Publishing (plugins.qgis.org)

- [x] **Runtime data attribution.** Done. `core/attribution.py` is the single
  source of credit for every dataset the plugin fetches (Mapzen/Terrarium,
  OpenFreeMap, Copernicus, swisstopo), shown in Settings under "Data sources"
  and in the buildings tooltip. Tests pin that ODbL sources credit
  OpenStreetMap and that we never credit a dataset we do not request.
- [ ] **Add `changelog=` to `waveshed/metadata.txt`** — reviewers routinely ask
  for it and it is shown on the plugin page.
- [ ] **Make the GPL "or later" grant explicit.** `LICENSE` is the bare GPLv2
  text; the "or at your option any later version" wording currently lives only
  in `README.md` and `metadata.txt`.
- [ ] **Make `package.py` exclusions explicit.** `.venv`, `tmp/`, `tests/`,
  `deploy.local.ini` and `*_rc.py` are excluded only as a side effect of
  walking `waveshed/` rather than the repo root; a generated `resources_rc.py`
  inside the package *would* ship.

## UI / UX

- [ ] **Help system.** No Help action or bundled help exists, and only ~19
  widgets have tooltips. Deferred until the rest of the release work is done.

## Housekeeping

- [ ] **The FlatGeobuf conversion cache is never cleaned.**
  `<tmp>/aether_fgb/` accumulates converted `.fgb` files and nothing removes
  them. Entries are now keyed by source fingerprint + declared CRS (needed for
  correctness — see `_fgb_cache_name`), so editing a source or re-declaring its
  CRS leaves the previous conversion behind rather than overwriting it. Bounded
  by how often sources change, but worth an age-based sweep.

- [ ] **`CLAUDE.md` is stale on dependencies** — it still lists `pynacl` as a
  bundled exception, but `waveshed/core/api_key.py` is pure standard library
  and nothing imports `nacl`.
- [ ] **`tests/fixtures/` is empty** — no on-disk fixture data backs any
  integration test.
- [ ] **Untested paths**: `get_source_resolution_info`, the `_try_rust_download`
  job JSON, and the buildings resolve/convert path have no coverage.
  Antimeridian (lon ≈ ±180) is untested *and* unhandled in `_compute_subtiles`.

---

# Issue register — 2026-07-30 code audit

Everything found in the audit of the cache / buildings / solver / Explorer
report. All findings are from **reading the code** — QGIS and GDAL are not
installed in the dev container, so none of it is runtime-verified. Line numbers
are against the tree as of that date.

## Terrain cache

- [x] **Every run re-downloaded terrain it already had.** *(Not in the original
  audit — found while implementing the items below.)* `_try_rust_download`
  treated an empty `affected` list as a failure: when source tiles were
  unavailable but no output tile had a hole, it wrote `.incomplete` and
  returned `_cache_hit()`, i.e. **False**. Three consequences, all now fixed:
  the caller read that as "download unavailable" and redid the whole
  preparation through the slow extract+convert path in the *same* run; nothing
  outside that function ever cleared the marker, so every later run
  re-downloaded the entire set and landed in the same branch again; and the
  converter reports exactly this case as "100.0% success" (see the example in
  `_TILES_OK_RE`, 697340/697343), so it was the common path, not an edge case.
  An output tile never written at all is now also counted as affected —
  `_abt_has_gaps` returns False for a file it cannot open, so a wholly missing
  tile used to escape both checks.

- [x] **A failed OpenFreeMap download poisoned the cache silently.** *(Also
  not in the audit.)* Buildings are part of the cache key, but on a failed
  download the code logged a warning, built plain terrain and stored it under
  the buildings-keyed hash. The next run hit that cache, skipped the download
  and produced building-free coverage with no warning at all. The cache is now
  left marked for rebuild — on both the ingest and the fast-download path,
  which a failed download reaches because `pbf_dir` goes back to None.

- [ ] **The `.abt` cache never reuses a tile across sites.** *Partly done:*
  the key is now the tile set, so two requests resolving to the same tiles
  share one cache directory. The canonical pool + hardlinks — which is what
  makes a *subset* reuse the tiles of a superset — is still open, and is now
  much more valuable because tiles are 16x smaller. Original finding:
  `_cache_key`
  (`core/terrain_adapter.py:242`) hashes the raw float **bbox dict**, making the
  key a per-*request* identity, while `:330` already emits globally canonical
  per-*tile* names (`tile_N47.00E6.00_5m.abt`) snapped to the global 1° grid by
  `_compute_subtiles` (`:346-347`). Neither write path (`:599-620` Rust
  download, `:1069-1109` extract+convert) checks whether a tile already exists.
  Two sites 5 km apart re-download every shared tile.
  AETHER's own `python/drivers/prepare_data.py:363-371` already has the missing
  layer (flat geography-addressed dir + `if os.path.exists(abt_path): continue`).
  Fix shape: a canonical pool keyed `(source, tile, resolution, buildings_id)`
  plus hardlinks into the per-request view dir — `abt.list_tiles` is
  deliberately non-recursive (`core/abt.py:95-99`) and `job_builder.py:198/:255`
  pass exactly one `terrain_dir`, so a shared parent dir is not an option.
  The buildings term must move into the *tile* identity, not vanish: buildings
  are burned into the pixels and re-applying them makes them grow.

- [x] **The bbox floats are unrounded, so near-identical requests always miss.**
  Done. `_cache_key` now hashes the sorted canonical tile filenames plus a
  `_CACHE_SCHEMA` version, so a sub-metre site nudge or 30 km vs 31 km lands on
  the same cache. Azimuth dropped out of the key entirely: a narrower sector
  selects a strict subset of the tiles and so hashes differently on its own.
  Original finding:
  `_compute_sector_bbox` (`:179-184`) returns unsnapped floats stringified at
  full `repr` precision, and the margin is `range_deg * 0.01` — so a sub-metre
  site nudge, or 30 km vs 31 km at the same site, is a total cache miss. Snap
  the bbox to the tile grid before hashing.

- [x] **Partial caches are silently reused — correctness bug.** Done.
  `_cache_hit(cache_dir, expected)` now requires every expected tile to be
  present, `prepare_terrain` marks the directory incomplete the moment it
  creates it, and each path clears the marker only after verifying its own
  output (the ingest path checks the tiles actually landed — the converter can
  exit 0 having skipped one). Original finding: `_cache_hit`
  (`:267-273`) returns True if **any** `.abt` is present, and only
  `_try_rust_download` writes the `.incomplete` marker (`:657`, `:664`) — the
  converter/ingest path (`:1067-1122`) never writes it, including in its
  `finally`. A run killed after tile 1 of 4 is reported as a HIT on the next
  run, and the engine (which resolves terrain by header via `peek_abt`) simply
  finds no tile for the missing cells. Result: a silently wrong coverage layer,
  no error. Likely source of the "empty stripes" the comment at `:1005-1007`
  alludes to. Check *all* expected tiles, and write the marker on every path.

- [ ] **No cache eviction of any kind.** *Partly done:* Settings now has a
  "Clear..." button next to the cache path (`terrain_adapter.clear_cache` /
  `cache_entries` / `cache_size_bytes`), which reports the size, confirms, and
  only ever removes md5-named entry directories — the cache root is
  user-configurable and may hold other things. Automatic eviction (LRU/TTL/size
  cap) is still open. Original finding: nothing in `waveshed/` ever deletes a
  cache directory — no LRU, no TTL, no size cap, and no "Clear cache" button
  (`gui/settings_dialog.py:275-284` is only a path field + Browse). At ~1 GB per
  run with no cross-site reuse, `~/.aether/cache` grows without bound. The only
  size logic is an advisory pre-run warning (`_SIZE_WARN_MB`,
  `_SIZE_STRONG_WARN_MB`, `:416-420`), and `estimate_terrain_disk_mb` has no
  notion of cross-site overlap so it over-estimates.

- [ ] **OpenFreeMap buildings never invalidate.** `_OSM_BUILDINGS_TOKEN =
  "openfreemap:z14"` (`:191-195`) is opaque and date-free, so OFM republishes
  are never picked up — the cache must be cleared by hand.

- [ ] **Map Converter ignores a relocated cache.** `gui/map_converter_tab.py:1196,
  1800, 1808` hardcode the `~/.aether/cache` default instead of calling
  `get_cache_dir()` (`core/terrain_adapter.py:52-54`).

## Buildings / terrain preparation performance

- [ ] **Enabling buildings abandons the fast downloader — 7-17x slowdown.** One
  branch at `core/terrain_adapter.py:1049-1052` diverts to
  `_export_via_qgis` -> `QgsRasterFileWriter::writeRaster`. Measured from a real
  run: same 5 m / 30 km area is 22.3 s on the fast path vs 151.2 s with
  buildings (122.1 s of it `writeRaster`); the 50 km case is 1687.5 s, of which
  ~1560 s is four `writeRaster` calls while the actual Rust converter work is
  only 108 s.
  The comment claims the downloader "cannot fuse a building overlay". It can:
  `download.rs:1469-1512` already builds each `.abt` as an in-memory `Vec<u8>`
  handed out at `:1730-1733`, and `mvt.rs:458-463` already exposes
  `apply_buildings_to_abt_tiles` taking exactly those buffers — the WASM web app
  calls it today (`aether_converter_wasm/src/lib.rs:249`), and `run_download_mem`
  is not wasm-gated. Needs an engine change (see "Engine-side" below).

- [ ] **`_export_via_qgis` is slow for four separate reasons**
  (`core/terrain_adapter.py:861-894`), all fixable plugin-side:
  - Network parallelism: the pipe is a clone of the XYZ/WMS provider pulled
    block-by-block, capped at Qt's default ~6 concurrent requests per host,
    versus 256 in the Rust path (`:59`). That ratio alone explains ~5.5x.
  - `setMaxTileSize` is never called -> QGIS's default 500x500 blocks -> ~1089
    provider round-trips per 16384px tile, and 500 px never aligns to the 256 px
    tile grid so boundary tiles are fetched up to 4x.
  - `setCreateOptions` is never called — no COMPRESS, TILED, NUM_THREADS or
    BIGTIFF, so every temp is an uncompressed Float32 GeoTIFF
    (268,435,456 px x 4 B = the "1074MB" in the logs). The plugin sets
    `COMPRESS=DEFLATE, TILED=YES` everywhere else (`core/abt.py:332`,
    `core/raster_tools.py:284`, `tools/xyz_to_geotiff.py:52`) — just not here.
  - No GDAL cache configuration anywhere in the plugin (zero `GDAL_CACHEMAX` /
    `SetConfigOption` hits), so GDAL runs at its 5%-of-RAM default while
    striping 1 GB of scanlines.

- [ ] **The extract is never clipped to the analysis bbox.** `sub_bbox`
  (`:1070-1079`) is built entirely from the tile's own grid cell (`t["ul_lat"]`,
  `t["size_px"]`) — `bbox` is never referenced — so a 0.55° x 0.79° request
  always extracts a full 1° x 1° tile. Safe to clip: `ingest.rs:196-206` and
  `:246-262` already bounds-check every sample against the base image extent and
  fall through to `-9999` outside it. Worth ~2.3x.

- [ ] **`cap = 16384` silently degrades resolution.** `:882-885` clamps the base
  raster to 16384 px, but `_tile_params` (`:322-331`) asks for `size_px = 22224`
  at 5 m. Effective sampling becomes 6.78 m/px and the `.abt` is then written at
  22224 px, upsampling 6.8 m data. So the buildings path is both slower *and*
  36% coarser than the fast path. Only raise this after the clip fix, or it adds
  1.84x more pixels.

- [ ] **The temp GeoTIFF is deleted every run** (`finally`, `:1119-1122`), so a
  repeat of the same area re-pays all ~12,200 HTTP fetches. The observed
  122.1 s -> 33.8 s variance for the same tile is uncontrolled Qt network-disk
  cache (~50 MiB default against a ~1 GB tile set) plus CDN edge warming — i.e.
  a 122 s step whose repeat cost is unpredictable. Cache it next to the `.abt`s.

- [ ] **`feedback` is accepted and never used.** `core/terrain_adapter.py:965`
  takes it (and `algorithms/p2p.py:299-307` passes a real one), but nothing
  inside `prepare_terrain` calls `setProgress`, `setProgressText` or
  `isCanceled`. So `writeRaster` is a single blocking 122-400 s C++ call with no
  progress and no cancellation — logs simply stop mid-line. This is the "we got
  stuck" report. `gui/site_analysis_tab.py:216` only checks `self._canceled`
  *between* steps.

- [ ] **`_export_via_qgis` runs on a worker thread — deadlock risk.** It calls
  `dem_layer.dataProvider().clone()` and `QgsProject.instance().transformContext()`
  from `_SiteAnalysisWorker(QThread)` (`gui/site_analysis_tab.py:137, :209`;
  same in `gui/p2p_tab.py:447, :518`). The plugin's own code says this is wrong
  — `gui/map_converter_tab.py:409-411`: *"QGIS writeRaster needs a
  QgsRasterLayer which is main-thread-only. So export NOW on main thread."*
  The WMS provider's tile fetch runs a nested `QEventLoop` over the thread-affine
  `QgsNetworkAccessManager`.

- [ ] **Building downloads are capped at 4096 tiles with no way around it.**
  `core/openfreemap.py:36,47` (`BUILDING_ZOOM = 14`, `MAX_TILES = 4096`); the
  error at `:157-164` is what large-range runs hit ("This area needs 32761
  building tiles"). There is no z-level fallback and there cannot be one — the
  `building` layer does not exist below z14 — so the limit has to go away by
  **chunking per `.abt` tile**, not by zooming out.

- [ ] **Building tile counts use the rectangular bbox, not the sector.**
  `estimate_tile_count` (`core/openfreemap.py:88-95`) ignores the circle: for a
  360° run ~21% of downloaded tiles are outside range entirely, and far more for
  an azimuth sector. `_compute_subtiles` (`core/terrain_adapter.py:355-385`)
  already has exact circle + arc intersection tests to reuse.

- [ ] **No shared building-tile cache.** `pbf_dir` lives inside the terrain cache
  (`core/terrain_adapter.py:1034`), which is keyed on bbox — so 30 km -> 31 km
  re-downloads all 1369 z14 tiles.

- [ ] **`download_building_tiles` uses eager ordered `pool.map`**
  (`core/openfreemap.py:141, :202-205`) with 8 workers, so one slow tile stalls
  the result iteration for up to `timeout=30.0`.

## Terrain source selection

- [ ] **No way to point an analysis at pre-built `.abt` tiles.** The engine side
  is fully done: `processing.terrain_dir` exists in `config.rs:21`,
  `toolkit/schemas/job.schema.json` and `toolkit/docs/CONTRACT.md:420`;
  `core/job_builder.py:197-201` / `:254-258` already write it; and discovery is
  **by header, not filename** (`engines/coverage.rs:523-538` via `peek_abt`,
  same in `engines/p2p.rs:91`, `engines/cpu_coverage.rs:225`). Only the UI and a
  `prepare_terrain` bypass are missing. Tiles must be flat (`read_dir` is not
  recursive) and cover the bbox at a compatible resolution.

- [ ] **The existing "Local: …" entry is a source-raster dir, not `.abt`.**
  `gui/site_analysis_tab.py:715-720` reads `waveshed/terrain_dir` and
  `core/terrain_adapter.py:741` globs `(".tif",".tiff",".dem",".hgt")`, then
  `:1084-1085` re-runs the converter over it. `gui/settings_dialog.py:268` says
  so. Do not conflate the two — a `.abt` picker must be a separate control.

- [ ] **P2P has no local-terrain path at all.** `gui/p2p_tab.py:925` is populated
  only from `QgsProject` rasters (`:1004-1008`) and `_P2PWorker.__init__`
  (`:463-470`) has no `terrain_dir` argument.

- [ ] **`_extract_reproject` takes a `binary_manager` it never uses**
  (`core/terrain_adapter.py:918`) — a dead hook where the Rust downloader was
  meant to go.

## Buildings source selection

- [ ] **`prepare_terrain(buildings_file=…)` is complete, wired, and never
  called.** `core/terrain_adapter.py:969`, threaded into the cache key
  (`:992-996`) and the ingest job (`:1105-1106`). No caller anywhere passes it.

- [ ] **Buildings are a single hard-wired OpenFreeMap checkbox**
  (`gui/site_analysis_tab.py:561-571`, read at `:1471`), with no layer/file
  option — even though Map Converter has the whole flow already
  (`gui/map_converter_tab.py:1093-1107` layer combo, `:1334-1388` file/folder,
  `:322-354` `_convert_to_fgb`, `:798-856` resolution).

- [ ] **The FlatGeobuf building path silently drops 2D layers.** The two
  converter inputs differ fundamentally (`toolkit/crates/aether_converter/src/ingest.rs:23-41`):
  `buildings_pbf_dir` (MVT) reads heights from **attributes** via a ladder —
  `render_height`, else `building:levels * 3.0`, else a 6 m default
  (`mvt.rs:229-259`) — while `buildings_file` (FGB) reads **no attributes at
  all**: `collect_fgb_buildings` (`ingest.rs:556-596`) does
  `let z_vals = match geo.z() { Some(v) => v, None => return };` and takes max Z
  as an absolute roof. A polygon layer with a `height` column is dropped on the
  floor. `gui/map_converter_tab.py:1863-1876` guards this with a
  `QgsWkbTypes.hasZ` check; a new selector must do the same, and must warn
  rather than silently skip.

- [ ] **`_convert_to_fgb` / `_fgb_translate_options` live in a GUI file**
  (`gui/map_converter_tab.py:280-354`) but are needed by any analysis-tab
  selector. `core/layer_utils.py` has nothing about buildings and is the natural
  home. (Flag before moving — it is an adjacent-file refactor.)

## Solver / engine (needs an engine release; see "Engine-side")

- [ ] **The solver's failure message is a hardcoded red herring.**
  `solver.rs:439-462` tests four predicates (`vram_ok && ram_ok && texture_ok &&
  single_alloc_ok`) and the `else` branch records nothing about which one failed.
  `:465-477` then bails on `best_azimuth_steps == 0` with a fixed string blaming
  VRAM and the macro-block pool — printed for *any* of the four failures, and
  advising `Increase processing.max_vram_usage_gb`, which for the common failure
  cannot help. Record the failing predicate and report it.

- [ ] **The real constraint is the terrain atlas single allocation.**
  `atlas_bytes <= safe_alloc_bytes` (`solver.rs:449`). `tile_bytes`
  (`:198-201`) is 2 B/px with 256-byte row padding: a 22224 px tile is
  `44544 * 22224 = 989,945,856 B = 0.99 GB`. Four tiles = 3.96 GB against a
  3.86 GB ceiling (90% of `min(gpu max_buffer_size, 4 GiB)`, `:353` /
  `engines/coverage.rs:722`) — **it fails by ~95 MB**. Sub-tile the atlas layers
  (only the wedge's terrain footprint is ever sampled) or stream layers per bank.

- [ ] **A site near a 1° tile corner costs 4x, and no wedge angle can reduce it.**
  Grid snapping at `engines/coverage.rs:680-684` uses 1° cells. A TX whose bbox
  crosses both a parallel and a meridian needs 4 tiles.
  `count_tiles_in_wedge_real` (`solver.rs:291-305`) counts the TX tile
  **unconditionally**, the other three share the corner point at a single
  azimuth, and `:391-396` takes the **max over all wedges** — so `max_tiles`
  is pinned at 4 for arbitrarily small theta.

- [ ] **`max_macro_blocks` is a hardcoded 1,000,000-block pool.**
  `solver.rs:376-378`: `1_000_000 * words_per_macro_block * 4`. For MinAltLos
  (512 words, `:104-110`) that is 2.048 GB charged against VRAM regardless of
  the job. A 12156x12118 output can only ever hold
  `ceil(12156/32) * ceil(12118/32) = 144,020` blocks = 295 MB — a **7x
  over-charge**. `output_width_px` / `output_height_px` are already passed in
  (`engines/coverage.rs:736-737`) but the solver never sizes any buffer from
  them. The runtime clamp (`engines/coverage.rs:847-851`) is a no-op on typical
  hardware.

- [x] **The two tile-generation paths disagree by 4x, and the coarse one causes
  the failure above.** Done — this was an unfinished migration, not a new
  design decision: AETHER's `Design Documents/qgis_plugin/TODO.md` §1.2 names
  the defect and §3.5 fixes the ladder, and `map_converter_tab._ABT_EXTENT_DEG`
  already implemented it. Only `_subtile_degrees` was still on the old
  `prepare_data.py` rule. The table now lives in `core/terrain_adapter` as
  `ABT_EXTENT_DEG` and drives both paths; the map-converter name is an alias.
  The `high_res` flag is gone — one resolution-keyed ladder for local terrain
  and base DEM alike, as §3.5 specifies. Measured after: a 5 m atlas layer is
  62.6 MB (was 989.9 MB) and a 30 km / 5 m site is 12 tiles whose worst-case
  4-tile atlas is 0.25 GB against the 3.86 GB ceiling. **`_CACHE_SCHEMA` was
  bumped to v2, so existing caches are abandoned, not silently reused with the
  old 1° tiles inside.** Original finding:
  `core/terrain_adapter._subtile_degrees` (`:294-310`)
  returns `sub = 1.0` for the base-DEM/XYZ path at every resolution — 22224 px
  tiles at 5 m — while `gui/map_converter_tab._ABT_EXTENT_DEG` (`:77-84`) uses
  0.25° for 5 m and 10 m — 5556 px tiles. Same resolution, 16x difference in
  area. (CLAUDE.md claims the two are "kept in sync". They are not.)
  Atlas layer size follows directly: 44544 * 22224 = **0.99 GB** per layer today
  versus 11264 * 5556 = **62.6 MB** at 0.25°. Four 1° tiles = 3.96 GB and the
  job is rejected; ~12-20 quarter-degree tiles = 0.8-1.3 GB and it runs.
  **This is a plugin-side fix for the solver rejection, available without any
  engine change**, and it also makes the shared per-tile cache far more
  reusable. Invalidates existing cache entries, so land it together with the
  cache rework.

- [ ] **`max_vram_usage_gb` truncates to whole GB.** `engines/coverage.rs:456`:
  `(limit_vram_gb_raw as f64 * 0.90) as u64` — 8 -> 7.2 -> **7**. Fractional GB
  are silently discarded. `engines/p2p.rs:134` uses a different margin (0.85).

- [x] **The Settings VRAM control is dead.** Done — and `max_ram_gb` had the
  identical defect, so both are fixed. The last leg (`job_builder` → job JSON)
  was always wired, which is what made it look connected; the missing leg was
  QgsSettings → params. Both are now dataclass `default_factory` reads
  (`job_builder._processing_setting`) rather than kwargs at the four
  construction sites, so a new construction site cannot forget them. Original
  finding: `gui/settings_dialog.py:289-292,
  335, 345` reads/writes QgsSettings `waveshed/max_vram_gb` and **nothing ever
  reads it back into `CoverageParams`**. `core/job_builder.py:52` / `:100`
  hardcode `max_vram_gb: int = 8`, and neither `algorithms/coverage.py:259` nor
  `gui/site_analysis_tab.py:1307` passes it. So the solver's own remediation
  advice is unreachable from the GUI. Plugin-side fix, do it regardless.

- [ ] **The converter pre-loads every base DEM before processing any tile.**
  `aether_converter/src/main.rs:106-116` — four 1074 MB Float32 GeoTIFFs decode
  with `Limits::unlimited()` (`ingest.rs:89`) to a transient 1074 MB f32 buffer
  each, retaining 537 MB `Vec<i16>`. Then every rayon worker allocates
  `vec![0i16; total_pixels]` = 988 MB (`ingest.rs:185`), with
  `safe_threads = (total_ram_gb / 1.5).clamp(1, cores)` (`main.rs:86`) — a 16 GB
  box gets 10 threads x 988 MB on top of the cached bases. The trim heuristic
  (`main.rs:139-145`) fires every 5th tile and retains only names starting
  `Chunk_`, while the plugin names its temps `aether_{hash}_{filename}.tif`
  (`core/terrain_adapter.py:1080-1082`) — so it would drop exactly the expensive
  base DEMs and keep nothing.

- [ ] **Every output tile re-decodes every building tile.** `ingest.rs:504`
  iterates the whole `pbf_dir` per output tile with no bbox filter, and each
  parallel worker holds a full `Vec<Building>` for the entire area — 1369 x 4 =
  5476 gunzip + protobuf decodes on the 50 km run.

## Altitude Explorer / Waveshed Explorer

- [ ] **AMSL and AGL cannot be shown together.** `_sync_twin_visibility`
  (`gui/altitude_explorer.py:865-885`) explicitly hides whichever surface is not
  selected, and `_driven_layers()` (`:843-854`) returns originals **or** twins.
  Everything needed for "both" already exists: `_band_sets` is already keyed by
  reference (`:532`), both surfaces are already independent `QgsRasterLayer`s in
  separate QGIS groups (`core/result_loader.py:49`), twins are disk-cached and
  mtime-validated (`:358-376`), and `core/layer_utils.altitude_reference(layer)`
  can already report a layer's reference — but `_apply()` (`:1491`, `:1501`)
  ignores it and passes the single global `self._reference`.

- [ ] **The altitude list is capped at 110 px against `MAX_BANDS = 8`.**
  `:648` vs `:99`. Checkable rows with a 12 px icon are ~18-22 px, so 8 bands
  need ~150-180 px: a permanent scrollbar showing ~5 rows. The layer list is
  capped at 120 (`:599`) — commit `5b09a11` shrank it from 140 to make room.

- [ ] **`layout.addStretch()` (`:738`) eats all surplus dock height**, so
  enlarging the dock never enlarges either list.

- [ ] **Zero `setSizePolicy` and zero `setMinimumHeight` calls in 1902 lines**,
  and no `QScrollArea` around root (`:582`, `:739`) — the lists collapse toward
  one row in a short dock and the panel clips rather than scrolls.

- [ ] **The dock sets no minimum width**, so it inherits QGIS's ~300 px default
  and elides three unwrappable HBox rows (`:613-631`, `:655-666`, `:682-699`).

- [ ] **The slider has no debounce and no `setTracking(False)`** — there is not
  one `QTimer` in the file. Every pixel of travel runs `_apply()` ->
  `estimate_altitude_range_m` -> `provider.bandStatistics()` (a potential
  full-raster scan) **per layer**, plus a renderer rebuild and a repaint of
  every COG (`:742`, `:1473`, `:1482-1504`).

- [ ] **`_sync_twin_visibility` is O(n²) over the project** — it iterates every
  layer and calls `_twin_of` (`:834`), itself a full `mapLayers()` scan, on every
  tick and reference change.

- [ ] **Half-metre altitudes are unreachable.** The control is a `QSpinBox`
  (`:673`, `setValue(int(round(...)))` at `:1239-1240`) but the raster quantum is
  `MIN_ALT_STEP_M = 0.5`. `_snap` (`:1181-1188`) also returns `int`, and
  `:1261-1263` truncate the float range instead of floor/ceil.

- [ ] **The band list is rebuilt with `clear()` + re-add on every change**
  (`_rebuild_band_list`, `:1204-1214`), causing flicker and making the selected
  row jump under the cursor when an edit finishes (`:1455-1458`).
  `refresh_layers` (`:767-803`) does the same and is wired to `layersAdded` /
  `layersRemoved` (`:574-575`), so each AMSL twin added by
  `_on_amsl_layer_done` (`:1062`) re-triggers a full rebuild plus `_apply()`
  mid-build.

- [ ] **`_next_altitude` materialises `range(int(low), int(high) + 1)`** on the
  UI thread (`:1392`) — thousands of ints for an alpine AMSL range, then an
  O(n·m) `max(...)` over it.

- [ ] **Hard-coded colours and pixel font sizes ignore theme and HiDPI**
  (`:592` `color: gray; font-size: 11px`, `:705` bold). Grey-on-dark is
  near-invisible.

- [ ] **Band numbers are ragged.** `%g` formatting (`:1207`, `:1451`) with no
  fixed decimals, no thousands separator and no column alignment, so
  `2450 m AMSL` sits next to `1250 m AMSL` in a plain list.

- [ ] **`_SWATCH_PX = 12` (`:102`) with no `setIconSize`** — the chip renders in
  a mismatched ~16 px slot. Also no `setUniformItemSizes`, no `setSpacing`, no
  `setAlternatingRowColors`.

- [ ] **`btn_reset` "Reset to full range" only unticks a checkbox**
  (`_on_reset`, `:1465-1467`) — the label misdescribes the mechanism. And
  `_on_threshold_toggled` (`:1460-1463`) only greys the list, which keeps taking
  the same space.

- [ ] **No mnemonics or shortcuts anywhere** (zero `setShortcut` hits), no Delete
  key on the band list, and bands are not editable in place (`:1209` sets only
  `ItemIsUserCheckable`).

- [ ] **Errors use modal `QMessageBox` batches** (`:1126`, `:1153`, `:1657`)
  where the rest of the plugin uses `QgsMessageBar` (`:1352`, `:1825`).

- [ ] **`ref_row` (`:613`) is bare in the root VBox** while everything else sits
  in a `QGroupBox`, and there are three different button alignments in one panel
  (`:717` right-aligned lone Reset, `:734-735` full-width stacked).

- [ ] **No test imports `altitude_explorer`.** A redesign breaks no tests and
  gets no safety net. The contract to preserve is `band_stops` +
  `build_band_renderer` (covered by `tests/test_min_alt.py`).

## Window sizing

- [ ] **The main dialog opens too small and never remembers a size.**
  `gui/main_dialog.py:39` sets `setMinimumSize(640, 580)` for a QDialog holding
  five dense tabs (`:120-124`), and **there is no `resize()` call anywhere in
  the plugin GUI** — so it always opens at its minimum/sizeHint. Add a sensible
  default and persist geometry to QgsSettings.

- [ ] **Per-tab minimums contradict the dialog's.** `gui/p2p_tab.py:254` demands
  `setMinimumSize(800, 550)` inside a dialog whose own minimum width is 640;
  `gui/asset_manager_tab.py:663` sets `(550, 550)`,
  `gui/settings_dialog.py:120` `setMinimumWidth(560)`. The dialog minimum must
  be at least the largest tab minimum plus chrome.

## Propagation models

- [ ] **Remove SIMPLE_LOSS as a user-selectable model.**
  `gui/main_dialog.py:102` (`addItems(["SIMPLE_LOSS", "ITM"])`),
  `algorithms/coverage.py:67`, `algorithms/p2p.py:47`, plus docstrings at
  `gui/main_dialog.py:59`, `gui/p2p_tab.py:632`, `gui/site_analysis_tab.py:10`.
  **Do not miss `gui/p2p_tab.py:1150`** — the fallback default is
  `"SIMPLE_LOSS"`, so removing the combo without changing it makes P2P silently
  run the removed model. **Keep** `core/result_loader.py:93, 665, 697` — the
  loss colour ramp branch is needed to load previously-computed results whose
  sidecar says `SIMPLE_LOSS`. Note that Processing enum indices are positional,
  so removing an entry shifts every index after it and silently repoints saved
  Processing models.

## Repository hygiene

- [ ] **No `.gitattributes` in any repo, and CRLF churn hides real diffs.**
  *Partly done:* `.gitattributes` (`* text=auto`, `eol=lf`/`eol=crlf` for
  scripts, `binary` for images and the `.abt`/`.bit`/`.pbf`/`.fgb`/`.tif`
  formats) added to all five repos, and `core.filemode=false` set here — this
  was the only repo with it `true`. This repo has been renormalised
  (`git add --renormalize .`): 21,284 phantom lines are gone and the staged
  diff is just the real change. **Still to run, one command per repo, when
  their working trees are convenient:** `git add --renormalize .` in AETHER,
  AETHER_Web, aether-tools and MPT_SIGMA. Left undone deliberately — it stages
  every file, and MPT_SIGMA has real uncommitted work in the noise.
  Original finding:
  `git status` shows 52 modified files in this repo (20,848 insertions /
  20,848 deletions) and 22 in AETHER (5,299 / 5,299), but
  `git diff --ignore-all-space` reports **0 insertions, 0 deletions** — it is
  entirely line-ending plus filemode `644 -> 755` churn from the Windows mount.
  This repo has `core.filemode=true`, AETHER has it `false`.
  Fix: `.gitattributes` with `* text=auto` (plus `-text` for `*.png`, `*.abt`,
  `*.pbf`, `*.fgb`), `core.filemode=false`, then `git add --renormalize .`.

## Engine-side (requires an aether-toolkit change + binary release)

These cannot be done from the plugin — `CLAUDE.md`'s "Zero Changes to AETHER
Binaries" rule means each needs a new engine build shipped through the
waveshed.io release manifest.

- [ ] **Fuse buildings in the Rust downloader.** `DownloadJob`
  (`download.rs:43-50`) has no buildings field (zero `buildings|pbf|mvt` hits in
  the file) and the downloader emits `.abt` only (zero `.tif` hits), while
  `IngestJob` requires a `base_tif`. That gap is the entire reason the slow
  QGIS `writeRaster` bridge exists. Add `buildings_pbf_dir` to `DownloadJob` and
  call `mvt::apply_buildings_to_abt_tiles` on the `MemAbt` buffers after
  `download.rs:1733`. ~20 lines; the library function already exists.

- [ ] **Or: expose an `apply-buildings` subcommand.** `aether_converter` has
  exactly three (`Convert`, `Ingest`, `Download` — `main.rs:64/68/159`) and none
  applies buildings to existing `.abt` files, even though
  `mvt::apply_buildings_to_abt_tiles` is right there. A ~10-line subcommand
  would let the plugin chain fast-download -> apply-buildings with no
  `writeRaster` at all.

- [ ] Solver fixes: report the failing predicate; size `max_macro_blocks` from
  the real output grid; sub-tile or stream the terrain atlas; compute the VRAM
  budget in bytes rather than truncated whole GB. See the solver section above.

- [ ] Converter fixes: bbox-filter the `.pbf` scan per output tile
  (`ingest.rs:504`); fix the base-DEM cache retain predicate
  (`main.rs:139-145`); bound the pre-load in `main.rs:106-116`.
