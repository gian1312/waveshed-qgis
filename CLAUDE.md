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
- **Stop / Cancel contract (2026-10-20).** Stop kills the engine subprocess
  in every path — download pass, ingest, aether_core — through the runner's
  `on_start(proc)` hook: `prepare_terrain` / `prepare_terrain_profile` take
  `should_cancel` + `on_start` and hand both to every engine run, because the
  converter prints ingest progress only every 10 tiles (polling lines alone
  cannot stop it); `should_cancel` is also polled per tile, band, pool-check
  tile and folder-index file. Cancel is always `ConverterCancelled` (also for
  a process killed before a line reached the check), never an error dialog;
  scratch/job files are still removed. Every tab's worker (Site Analysis,
  P2P, Map Converter) kills a process that starts after Stop on
  registration; the run UI (Run, Convert) resets only on `QThread.finished`
  of the current worker — Stop never blocks the GUI thread (Site
  Analysis / P2P `_kill_proc` = `terrain_adapter.kill_engine_process`: POSIX
  `terminate()`; Windows `proc.kill()` on the handle, then a fire-and-forget
  `taskkill /F /T` for children — never a blocking taskkill).
  Processing passes `feedback.isCanceled`. Pinned by
  `tests/test_cancel_contract.py`.
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
- **Recorded void counts (2026-10-25, toolkit CONTRACT changelog 46,
  aether_converter >= 0.2.11 — `binary_manager.CONVERTER_TILE_VOIDS_MIN_VERSION`
  / `converter_supports_tile_voids`).** The view fill used to read every pool
  tile to find voids (`_tile_has_voids`; owner, 2026-10-24: 119 tiles of 2 m,
  7.3 GB memory-mapped, 18 s, for two with voids). The converter prints
  `[Tile] <name> voids=<n>` (samples equal to the -9999 sentinel in the file
  as written): a download for every tile a NON-repair pass writes (after the
  building burn), an ingest right after the tile's `covered=` line (after the
  void fill: 0 with `void_fill_m`); `terrain_adapter.parse_tile_voids_line`
  (`_TILE_LINE_RE` never takes it). `_try_rust_download._run_pass` marks a
  pass's tiles unknown at launch and binds each count to the file as the pass
  left it (`_voids_record`: count + size + mtime_ns); a repair pass leaves its
  tiles unknown; when the download ends (an exception too) `_settle_voids`
  re-checks each binding, writes the records into the POOL's
  `.waveshed_tiles.json` (`_record_pool_voids` → `write_tiles_manifest(voids=)`:
  optional key `"voids"`, name → `{voids, abt_size, abt_mtime}`, no schema
  change, written only while a record exists — a converter that prints no
  count leaves no manifest in the pool) and returns them
  (`DownloadOutcome.voids`). The Site Analysis ingest records its placed tiles
  (`<name>` or `.part`) the same way. Both tabs' pool downloads go through
  `_try_rust_download` (the Map Converter writes no pool records of its own);
  `remove_pool_tiles` drops the records of what it removes. Views:
  `_view_voids(pool, names, tiles, run=)` = this run's record, else the
  manifest's (a cache HIT), each only while bound to the file the view links
  (`resolve_pool_tile(for_link)` for a `.part`) → `_sync_view(voids=)`: known
  0 → linked unread, known > 0 → `_fill_view_voids` at once, unknown →
  scanned as before; every caller passes it (download, HIT, ingest, profile
  view). One `  view: N tile(s) linked from recorded void counts, M scanned,
  K filled (T s)` line per view built; the `view fill:` line per filled tile
  stays. Nothing waits for the line (no call site needs the gate). The count
  is the sentinel only, the fill takes every sample <= -5000 counts: a tile
  with water deeper than 2500 m (the downloader keeps bathymetry to -11000 m)
  prints 0 and is linked raw. The Map Converter's ingest panel drops the
  line; Site Analysis' download / ingest line handlers log it. Pinned by
  `tests/test_tile_voids.py` (real converter: `AETHER_CONVERTER_0211`, else
  AETHER_BIN_DIR / PATH when >= 0.2.11; skipped without).
- **Degraded download tiles** (toolkit protocol, 2026-10): a source tile whose
  fetch failed and was filled from a coarser zoom prints `[Degraded] z= x= y=
  reason=… filled_from=z<k>` (+ `[Stats] DEGRADED: n of total …`) and is NOT
  counted in `[Stats] Tiles: ok/total OK`. Such an output tile has no hole, so
  `_download_zoom_group` maps each line (`_xyz_tile_bbox`) onto the output
  specs it intersects (`_specs_hit_by_degraded`). Degraded-only (no gap/hole,
  no hard failure) → ONE repair pass: the job adds `repair_source_tiles:
  [[z,x,y],…]` (exactly the `[Degraded]` tiles) with the hit output specs, and
  the converter patches only those source tiles into the existing .abt
  (requires `aether_converter --version` >= `CONVERTER_REPAIR_MIN_VERSION`
  0.2.6, `binary_manager.converter_supports_repair`, probed lazily + cached;
  older/unknown → the whole-output-tile re-run, logged as "fallback"). Gaps
  keep the whole-tile retry. Every pass uses the configured connections — no
  plugin-side halving (the converter's adaptive window owns backoff; halving
  stacked a second one: 256→128→64→32 after one bad tile). Still degraded
  after the repair pass (incl. a repair pass that fetched less than asked
  with no `[Degraded]` line) → kept for this run with a `.rebuild` flag plus
  the warning "N terrain tile(s) were filled from coarser data (z…) …
  re-downloaded next run"; a failed repair pass rewrites those tiles whole if
  a pass is left. The repair pass is a progress pass like a retry (work = its
  source tiles, label "repair pass k/n"). A degraded pass never takes the
  complete, all-404 or total-failure branches. **Gap scan only where a hole
  can be (round 6):** a hole is a source tile missed WITHOUT a coarser fill,
  so only `missing - n_degraded` (missing = total − ok; n_degraded = the
  pass's `[Degraded]` count, `[Stats] DEGRADED` if higher) can leave one.
  0 → no output tile is read (a stat for "never written" only; `affected` =
  the degraded hits) — the owner's z15 band read all 706 tiles of 62 MB
  twice (44 GB, 159–252 s per pass, "degraded/repair plan") behind ONE
  timed-out, z14-filled tile. > 0 → the converter names no failed tile
  (download.rs: `[Download] first <class>: z/x/y` for the first of a class
  only, nothing for HTTP errors / 404, `[Strip …] … FAILED` without
  coordinates), so the tiles THIS pass wrote are read (tiles outside it
  were left clean by an earlier pass), logged `gap scan: reading N output
  tile(s) at z… — K source tile(s) missing without a coarser fill (J of them
  no-data, HTTP 404); …`; `_abt_has_holes` then `_abt_has_zero_fill` of a
  tile share ONE read (`_abt_block_flags`: one min/max pass; the other
  answer kept per thread for the same path+size+mtime, `_abt_block_answer`).
  Repair pass and rebuild flags unchanged. `tests/test_timing_lane.py`.
- **Raw source-tile cache (2026-10-21, toolkit CONTRACT changelog 38,
  aether_converter 0.2.8).** Every download job (every pass of
  `_try_rust_download._run_pass` — Site Analysis through `prepare_terrain`,
  the Map Converter through `ensure_pool_tiles`; first, retry and repair
  passes alike) carries `cache_dir` = `terrain_adapter.xyz_cache_dir()` =
  `<cache>/xyz`, made by `ensure_engine_dirs` right before each launch; no
  setting, always on. The converter keeps the raw PNG/WebP bytes of real
  server answers there (200 bodies and 404 "no tile"; never degraded or
  failed tiles) in its own layout (`<service_key>/z<zoom>/x<x>_y<yblock>.pack`
  + `<service_key>/service.json`; the plugin never reads it) and later jobs
  fetch only what it lacks. Older converters ignore the field (0.2.7:
  identical pool bytes, `tests/test_raw_tile_cache.py`). Its line
  `[Download] cache: <hits> of <n> source tiles from cache` is logged like
  every download line and parsed by nothing; progress/`[Stats]` count a
  cached tile as fetched, `.abt` bytes are unchanged, the zoom rule per
  resolution stays. Rebuild flags keep working: degraded/failed tiles are
  never cached, so a flagged tile is fetched again. Settings → Clear Terrain
  Cache deletes `<cache>/xyz` with the terrain sets (`cache_entries` = the
  md5 dirs + exactly that dir when it exists; the dialog says "…, of which
  N MB downloaded source tiles"); the folder index only when ticked.
- **Buildings on ingest jobs.** `buildings_file` (FlatGeobuf, an Add
  Buildings… row, converted by `core.buildings_source`; one per run — more
  than one building FILE row is refused) and `buildings_pbf_dir` (a
  directory of z14 MVT tiles, contract §9a — an INGEST field as well as a
  download one) may be combined. **OpenFreeMap z14 has ONE code path,
  `core/osm_buildings.py` (2026-10-23), for the Map Converter's Download
  Buildings… row and Site Analysis's "Include buildings" (360° and every
  profile band; P2P and Processing pass none):** `BuildingCover` (cover of
  a list of extents; per output tile `members` = cover tiles reaching the
  tile grown by `REACH_DEG` = 1/16 z14 tile → `tag`, `needed`,
  `view_names`); `ensure_tiles` = the strict fetch into THE store
  `terrain_adapter.osm_buildings_store_dir()` (`<cache>/pool/<md5(
  "buildings|openfreemap:z14")>`, cleared with the cache):
  `resolve_tile_template_strict` (TileJSON read every time, no fallback URL)
  + `openfreemap.fetch_building_tiles` — 32 keep-alive connections
  (`FETCH_WORKERS`, `_TileClient`; urllib when a proxy applies; 149 tiles/s
  measured vs 11-24 with a connection per tile), gzip stored as served,
  every answer checked (`tile_problem`) before an atomic write
  (`_write_tile`), 4 attempts (1/3/8 s, `Retry-After`, 20 s socket, 90 s per
  answer); 404 / empty 200 = no data; the first tile still failing or
  undecodable → `FetchFailed` (cause + missing count), no data anywhere too
  unless `require_data=False`; `batch_view` = `<cache>/views/<md5 of the
  names>` holding exactly those names (`terrain_adapter.link_pbf_view`, the
  ONE linker; `view=` links into a directory named earlier; its links run on
  `link_threads()` threads — 16 where the file system takes them, ONE on
  Windows: the owner's bench of 2026-11-07 on an NTFS HDD did 20,000 links
  at 2,120/s on one thread and 1,540/s on sixteen, the directory inserts
  being serialised, so the 16-thread version of round 8 had linked 21,000
  tiles in 24 s where 12 s had done; `WAVESHED_LINK_THREADS=<n>` overrides
  for an A/B run; the per-link cost is the file system's, not Python's:
  ~470 µs on that disk against ~10 µs of interpreter overhead);
  `BuildingsFetch` = that fetch in its OWN thread (`waveshed-buildings`),
  the one both tabs run: groups in order (a tile with its first group), one
  TileJSON read + store listing, `ensure_tiles` per group; `wait_for(names,
  should_cancel)` / `ready_for` (each group's names announced when in, the
  last after `on_fetched`; with `require_data` also only once some tile has
  data); `link_when_ready(names, view, link_names)` (`when_ready`, the same
  rule as a callback: for a converter launched before its tiles — `view`
  from `ready_view` gets exactly `link_names()` and THEN its marker,
  `complete_view`; at once in the caller's thread when the names are in,
  else on the fetch thread right after they are; dropped after a failure or
  Stop; a link/marker failure there = the fetch's error in its own words);
  its list counterpart `list_when_ready(names, path, list_names)`
  (aether_converter >= 0.2.14, toolkit CONTRACT changelog 51,
  `buildings_pbf_list` — no view, nothing linked: the converter reads the
  listed tiles straight from THE store and the list IS the ready signal;
  same rule, same threads, dropped after a failure or Stop): `path` from
  `ready_list(needed)` = `terrain_adapter.osm_buildings_list_path` =
  `<cache>/views/<md5("buildings|openfreemap:z14|lists")>/<md5(sorted
  names + "|list")>.txt` (its own key; ONE md5 directory, so Clear Terrain
  Cache takes the lists with the views — measured 2026-11-07: a file
  straight in `<cache>/views` survives `clear_cache`, which deletes md5
  DIRECTORIES only), a stale list removed first (`remove_building_list`;
  cannot → None, the caller takes the view) and THE store made (the
  job's `buildings_pbf_dir`: on a fresh cache the fetch has written
  nothing when the converter starts); `complete_list` →
  `write_building_list` writes exactly `list_names()` (sorted, once, `\n`,
  UTF-8; none = an empty file) to a temp file next to it +
  `replace_with_retry`; a write failure = the fetch's error, `Buildings:
  Building tile list <path> could not be written (…)`;
  `log` gets template / cache counts / a line per 10 %; hooks
  `on_begin`, `on_start(cached, todo)`, `on_progress(done, todo, bytes,
  MB/s)`, `on_fetched(result)` (raising = failure in its own words),
  `on_failed(cause)` (after `error` is set), `on_end`; a failure →
  `error` = `failure_text(cause)` in whole-fetch numbers, `should_cancel()`
  → quiet `cancelled`; `BurnMonitor` = the converter's lines over every converter
  run of one plugin run — ingest `[Info] buildings_pbf_dir: N tile(s), no
  buildings in extent` / `… B building(s) (A after dedup), D drawn, P
  pixel(s) raised` per tile, download `[Buildings] N pbf tile(s), no
  buildings in extent` / `[Buildings] N pbf tile(s) → B building(s) (D dup
  dropped) → H/T abt tile(s), P px in S s` per pass (a repair pass owes
  none; the `[Buildings] waiting for the building tiles (…)` /
  `… building tiles ready after S s` of a job with `buildings_pbf_ready`
  — or `buildings_pbf_list`, the list's path inside — are no report —
  kind "wait", `waits` / `waited_s`, per converter run
  `batch_waited_s`; the 0.2.14 decode header `[Rust] Decoding N listed
  building tiles from "<dir>" (once for the run)...` is a "decoding" line
  like the old `… building tiles in "<dir>"` one, `LISTED_DECODING_RE`,
  and moves no bar); `feed` is True at the line after `[Warn] buildings <tile>.pbf:`
  (kill now), `end` flags a run that owed a report and printed none (an
  engine ignoring the field), `verdict` = undecodable tiles / a silent run /
  buildings decoded and nothing drawn over the WHOLE run (one batch at the
  edge legitimately draws none), `empty` ("no buildings in extent"
  everywhere) is a warning, never an error; `drop_undecodable` removes such
  tiles from store and views (a list needs nothing: every run removes its
  list and writes it anew from the store). `download_building_tiles` and
  its 4096-tile cap are gone.
  **Map Converter** (2026-10-22, replaced Overpass, whose one query for
  16.6 deg² answered empty after 107 s and let the run go on WITHOUT
  buildings): the row (source `[OSM Download — set extent, download at
  convert]`, unchanged, so older stacks load) fetches only the z14 tiles the
  PENDING output tiles need. `_BuildingsBranch` (a thin
  `osm_buildings.BuildingsFetch` subclass: the worker's log, progress slot
  `("buildings",)`, status line, `_halt` on failure; `require_data`)
  starts after the partition
  (before the terrain download), one `ensure_tiles` call per group in batch
  order (Phase 2's tiles, then the bands north to south; progress/log/errors
  over the whole fetch; no data at all = run error); `_pbf_view` (each
  batch's job build) waits only for THAT batch's names (`wait_for`),
  `_await_buildings` joins the fetch at the end. **On aether_converter >=
  0.2.12 (2026-10-25, CONTRACT item 48;** `_converter_waits_for_buildings`
  → `binary_manager.converter_supports_ingest_buildings_ready`, decided in
  `_start_buildings`, log `  buildings: every batch is launched at once —
  aether_converter >= 0.2.12 waits for its building tiles (_ready in its
  view)`**) no batch waits:** `_marker_view` names the batch's view from
  its NEEDED names (`osm_buildings.ready_view`, stale `_ready` removed;
  cannot → that batch waits as before), registers it
  (`_BuildingsBranch.link_when_ready` with `osm.view_names(tiles)`: linked
  and marked as soon as its names are in — at once, in the worker thread,
  when they are; log `  buildings: N OpenFreeMap tile(s) linked into
  <view>, _ready written — the converter burns them now`), and every job
  carrying the view gets `buildings_pbf_ready: "_ready"` +
  `buildings_pbf_wait_s: 14400` (`_ready_fields` → `_build_tile_jobs(
  pbf_ready=)`) — Phase 2 and every Phase-3 batch launch at once, the
  converter waits at its burn (the BurnMonitor's "wait" lines → the slot's
  step `waiting for building tiles`, status `Waiting for the building
  tiles...`); the timing line's "building view" is the registration only;
  the end-of-batch line adds ` — the converter waited W s for them`
  (`batch_waited_s`). A fetch failure `_halt`s the run as before (the
  waiting converter is killed with the others; one error); a link failure
  on the fetch thread is the fetch's error. **On aether_converter >=
  0.2.14 (2026-11-07, toolkit CONTRACT changelog 51;**
  `_converter_lists_buildings` →
  `binary_manager.converter_supports_buildings_list`,
  `CONVERTER_BUILDINGS_LIST_MIN_VERSION`, decided in `_start_buildings`
  (`_pbf_list`), log `  buildings: every batch is launched at once —
  aether_converter >= 0.2.14 waits for the list of its building tiles and
  reads them from <store>`**) no view
  is built and nothing linked** (the owner's run of 2026-11-05 spent 23.8 s
  and 24.7 s linking 21,012 and 20,703 tiles before two batches — NTFS,
  ~470 µs a link): each batch's job build asks `_pbf_batch(tiles)` →
  `(buildings_pbf_dir, job fields)`: `_listed_batch` names the batch's
  list from its NEEDED names (`osm_buildings.ready_list`, a stale list
  removed; cannot → `  WARNING: buildings: <list> (left by an earlier run)
  could not be removed — this batch's building tiles are linked into a
  view instead` and the 0.2.12 view below), registers it
  (`_BuildingsBranch.list_when_ready` with `osm.view_names(tiles)`; log
  `  buildings: N OpenFreeMap tile(s) for M output tile(s) — converted
  now, the converter waits for their list <list>`, then — at once, in the
  worker thread, when the names are in, else from the fetch thread —
  `  buildings: N OpenFreeMap tile(s) listed for the converter in <list>
  — the converter burns them now`), and every job the tiles reach gets
  `buildings_pbf_dir` = THE store + `buildings_pbf_list` = the list +
  `buildings_pbf_wait_s: 14400` (`_build_tile_jobs(pbf_ready=)`; never
  `buildings_pbf_ready`); otherwise `_pbf_batch` = `_pbf_view` +
  `_ready_fields`. The wait / ready lines (the list's path inside), the
  label, the status and the end-of-batch "waited" suffix are the marker
  mode's; the timing line's "building view" lane is the list's
  registration; the converter's header `[Rust] Decoding N listed building
  tiles from "<store>" (once for the run)...` is logged like the old one.
  Fetch failure / Stop: no list, the waiting converter killed as before.
  Older/unknown converter: unchanged. Jobs the tiles reach get
  the batch's view (0.2.14: the store and the list); the pool download
  stays building-free. Fingerprint:
  `BuildingCover.tag` per tile (`openfreemap:z14@<z14 range>`, + md5 of the
  members when partial; "" where none reaches; a file row keeps its old
  tag), known before the fetch. Per batch (`_run_ingest`, the run's
  `_burns`): a decode warning kills it and ends the run; no report →
  `_BuildingsRunFailed`, its written tiles recorded unverified. Run level
  (`_judge_burns`): decoded and none drawn → run error, this run's
  OpenFreeMap tiles unverified — judged only when the run built EVERY output
  tile the buildings reach (one edge tile rebuilt beside tiles an earlier
  run drew on: logged "not judged"); all empty → warning. **Manifest schema
  2** (`terrain_adapter.read_tiles_manifest` / `write_tiles_manifest`):
  `tiles` = name → fingerprint as in schema 1 (`_tiles_to_build` reads only
  these) + `buildings` = name → `openfreemap:z14` / `file` /
  `file+openfreemap:z14` / `none` / `unverified` (`_buildings_record`,
  written after the batch's report checked clean; `unverified` beside the
  never-a-fingerprint `buildings-unverified`); schema 1 = buildings unknown.
  Logs: `Downloading buildings (OpenFreeMap z14): N tiles for S=… N=… W=…
  E=…`, `  buildings: k% (…)` per 10 %, `Buildings ready: N tiles (M MB) in
  T s`, `  buildings: burned onto a of b tile(s) — …`. Convert refuses an OSM
  row without extent or off the terrain (`_osm_rows_problem`). Not in the
  disk need yet. Pinned by `tests/test_mc_osm_buildings.py` (stand-in server
  `tests/_ofm_server.py`, real 0.2.7 converter;
  `RealConverterPhase2BesideItsBuildings`: the real >= 0.2.12 —
  `AETHER_CONVERTER_0212` or AETHER_BIN_DIR — Phase 2 launched before its
  tiles, byte-identical to the sequential run; skipped without one; a
  >= 0.2.14 binary pinned to the view there;
  `RealConverterPhase2ListsItsBuildings`: the real >= 0.2.14 —
  `AETHER_CONVERTER_0214` when set (a failure when it names a missing file
  or an older build), else the `AETHER_BIN_DIR` / PATH converter when it
  answers >= 0.2.14, else skipped naming what was seen (the release check
  passes only `--bin-dir`, so the pipeline must reach it that way) — Phase
  2 through its list,
  byte-identical to the view mode of the same binary),
  `tests/test_mc_pipeline.py` (`BatchesLaunchBeforeTheirBuildings`, its
  `test_list_mode_…` with a fake 0.2.14 that reads only the listed names:
  no `os.link`, no view directory, the list written atomically after its
  last name is in, a stale one removed, none after a failure / Stop, the
  0.2.12 / 0.2.13 view and the sequential jobs pinned),
  `tests/test_osm_buildings.py`, `tests/test_engine_version.py` (gate).
  **Site Analysis** (`prepare_terrain`, every band of
  `prepare_terrain_profile`): the cover is the WHOLE pool tiles to build
  (`todo`; a partial file-source build: its regions), never the sector bbox
  — a tile pooled "with buildings" holds every building on it
  (`_osm_buildings_cover`: logs the count and its bound at
  `TILE_BYTES_ESTIMATE` 50 KB a tile; no cap; `terrain_plan`'s
  `buildings_mb` — that bound for the missing tiles, store not listed — is
  "building tiles" in `_disk_refusal`). **Next to the download (2026-10-23,
  toolkit CONTRACT changelog 45 / §9b):** the XYZ download route (no
  buildings FILE) on `aether_converter` >= 0.2.10
  (`binary_manager.converter_supports_buildings_ready`, the cached
  `--version` probe; `CONVERTER_BUILDINGS_READY_MIN_VERSION`) runs
  `_BuildingsBesideConverter` (`what="download"`, made by
  `_buildings_beside`): the
  view is named BEFORE the fetch from the tiles NEEDED
  (`osm_buildings.ready_view` → `osm_buildings_view_dir(needed,
  marker="_ready")`, its own key — never
  a batch view another run relinks), created, a stale `_ready` removed
  (`osm_buildings.remove_ready_marker`; cannot → the sequential path),
  then `BuildingsFetch` (`require_data=False`) starts and the download job
  is launched AT ONCE with `buildings_pbf_dir` + `buildings_pbf_ready:
  "_ready"` + `buildings_pbf_wait_s: 14400` on every pass
  (`_try_rust_download(pbf_ready=, pbf_wait_s=)`; repair passes ignore
  them): the converter assembles the terrain, then waits at its burn for
  the marker. `on_fetched` links the view (`batch_view(names, view=)`:
  exactly the tiles with data, none when there is none — the converter
  then prints `[Buildings] 0 pbf tile(s), no buildings in extent`, the
  fetch's warning is the only one) and THEN writes the marker atomically
  (`complete_view` → `write_ready_marker`: temp + `replace_with_retry`).
  Every converter process goes through `converter_on_start` (the caller's
  `on_start` too);
  a failed fetch — or one that ends without its marker, Stop included (a
  caller without `on_start` could not kill it) — kills it
  (`kill_engine_process`),
  a pass launched after that is killed at once, `converter_cancel` turns
  the exit into `ConverterCancelled`; a download that raises (fetch
  failure, Stop, any error; not the burn's own undecodable verdict, which
  flags as before) has its pass's pool tiles removed (`_discard` →
  `remove_pool_tiles`, survivors flagged): a killed converter leaves
  complete terrain WITHOUT buildings. Fetch failure → RuntimeError with
  the fetch's cause (strict, as before); Stop → quiet `ConverterCancelled`;
  no fetch thread outlives the download (`close`). Logs: `  buildings:
  fetched next to the terrain download (aether_converter >= 0.2.10 waits
  for <view>/_ready)`, the converter's wait / ready lines like every line,
  `  buildings: N OpenFreeMap tile(s) linked into <view>, _ready written —
  the download burns them now`, then `  buildings fetched next to the
  terrain download: fetch N s, download M s, converter waited W s` (W =
  `BurnMonitor.waited_s`, the converter's own figure). **Next to the
  conversion (2026-10-25, toolkit CONTRACT item 48):** every INGEST route
  (file / folder / rendered sources, a buildings FILE beside OpenFreeMap)
  on `aether_converter` >= 0.2.12
  (`binary_manager.converter_supports_ingest_buildings_ready`,
  `CONVERTER_INGEST_BUILDINGS_READY_MIN_VERSION`) runs the same class with
  `what="ingest"`: the fetch starts before the jobs are built (`start`),
  every job carrying the view gets `buildings_pbf_ready: "_ready"` +
  `buildings_pbf_wait_s: 14400` (`job_fields`) and `_run_converter` is
  launched at once through `run` (its `converter_cancel` /
  `converter_on_start`); the converter starts its terrain work and decodes
  the view when the marker appears (no tile written before). A fetch that
  already ended without its marker launches nothing; failure / Stop as for
  the download — the route's `finally` closes the fetch and removes
  `incoming`, nothing is pooled; no tile with data → the fetch's warning
  only. Logs: `  buildings: fetched next to the conversion
  (aether_converter >= 0.2.12 waits for <view>/_ready)` (at the fetch's
  start), `… _ready written — the conversion burns them now`, `  buildings
  fetched next to the conversion: fetch X s, converter Y s, converter
  waited Z s` (Z = the converter's `ready after` figure, else 0.0).
  **List mode (2026-11-07, aether_converter >= 0.2.14, toolkit CONTRACT
  changelog 51; both routes):** `_buildings_beside` (after the route's own
  gate, `binary_manager.converter_supports_buildings_list`) names the
  LIST instead of the view (`osm_buildings.ready_list(needed)`, a stale
  list removed; cannot → `  WARNING: buildings: <list> (left by an earlier
  run) could not be removed — the building tiles are linked into a view
  for the download|conversion instead` and the view + marker above) and
  runs `_BuildingsBesideConverter(pbf_list=)` with `pbf_dir` = THE store:
  no view is built, nothing linked; the jobs carry `buildings_pbf_dir` =
  the store + `buildings_pbf_list` + `buildings_pbf_wait_s: 14400`, never
  `buildings_pbf_ready` (ingest: `job_fields`; download: every pass,
  `_try_rust_download(pbf_list=, pbf_wait_s=)` through `launch_fields`;
  repair passes ignore it); `_fetched` writes the list (`complete_list`:
  exactly the tiles with data, none when there is none) where the marker
  mode links the view and writes `_ready`; `marker` is the list. Logs:
  `  buildings: fetched next to the terrain download|conversion
  (aether_converter >= 0.2.14 waits for <list>)`, `  buildings fused
  during download (buildings_pbf_dir; the converter waits for <list>)`,
  `  buildings: N OpenFreeMap tile(s) listed for the converter in <list> —
  the download|conversion burns them now`, the timing lines unchanged.
  Failure / Stop / cleanup as above (the list never written).
  **Sequential**
  (`_fetch_osm_buildings`, unchanged): downloads on a converter < 0.2.10
  or unknown (it would burn the directory half-filled, or fail on a
  missing one) and ingests on one < 0.2.12 or unknown (it decodes the
  directory when it STARTS): fetch first (`should_cancel`), a
  failure ENDS the run with the cause, nothing downloaded or pooled; no
  tile with data → warning, no field. The download job (every pass) /
  ingest jobs carry the view; `_try_rust_download(burns=)` reads each
  pass's lines when it ends (a decode warning stops the download),
  `_run_converter(burns=)` kills an ingest at the line after it. A verdict
  error is a RuntimeError: tiles a download wrote are flagged for rebuild,
  an ingest places nothing (`incoming` removed). The buildings FILE checks
  (`_buildings_complaint`, `_buildings_drew_nothing` → `buildings_missing`
  flags) are unchanged. Pinned by `tests/test_sa_osm_buildings.py` (fakes
  + real 0.2.7; `BesideTheDownload`: a waiting fake converter over a real
  process; `RealConverterBesideTheDownload`: the real >= 0.2.10 —
  `AETHER_CONVERTER_0210` or AETHER_BIN_DIR — byte-identical pool tile to
  the sequential path, killed while waiting by a failed fetch; skipped
  without one; `BesideTheConversion` / `RealConverterBesideTheConversion`:
  the same for the folder ingest, the real >= 0.2.12 from
  `AETHER_CONVERTER_0212`; both marker classes pin a >= 0.2.14 binary to
  the view; the `test_list_mode_…` of `BesideTheDownload` /
  `BesideTheConversion`: fake 0.2.14 converters reading only the listed
  names — store + list in the job, no `.pbf` link, no building view, the
  list written once, atomically, after the fetch, a stale one removed
  (cannot → the view), none after a failure / Stop, 0.2.13 keeps the
  view; `RealConverterListsBesideTheConversion`: the real >= 0.2.14 from
  `AETHER_CONVERTER_0214`, else `AETHER_BIN_DIR` / PATH — the same
  `_list_converter` helper as the mc test — pools the bytes of the view
  mode) and `tests/test_osm_buildings.py` (`FetchThread`). The "converter
  waits" tests of both files hold the delayed building tiles until the
  converter's own `[Buildings] waiting for the building tiles (` line has
  been logged (`_HeldTiles` in `tests/test_mc_osm_buildings.py`, shared by
  the sa file; 120 s cap, specific failure messages; the "waits" tests
  release 1 s after the line so the converter's one-decimal "waited" figure
  is above 0.0, the failed-fetch tests release at the line) instead of a
  fixed 3–6 s server delay: the fixed delay failed under load (the
  2026-11-08 audit's parallel suite run) while the product behaviour was
  correct.
  **Prepared `.abt` folders:** `terrain_adapter.prepared_buildings(folder,
  selected tiles)` reads the manifest → `all` / `none` / `mixed` /
  `unknown` (no manifest, schema 1, no record, `unverified`). Site Analysis
  `_on_run` (state from `_terrain_dir_checks`, the coverage check's thread,
  over the memoised selection): ticked + all → information "already
  contains buildings; nothing is added"; none / mixed → warning with the
  counts (Yes = run anyway); unknown → one log line; unticked with
  buildings → information once per folder per session; toggling the box on
  a prepared folder → status tip + message-bar hint. The coverage warning no
  longer carries buildings text there. `prepare_terrain` /
  `prepare_terrain_profile` log `prepared_buildings_line` (Processing); P2P
  `_check_terrain_folder` logs one line.
- `base_tif` / `swiss_tifs` are DEPRECATED aliases still accepted by the
  converter for older callers; the plugin no longer emits them, and emitting
  them alongside `sources` is an error.
- The converter also has a `plan` subcommand
  (`plan --south S --north N --west W --east E --resolutions 30,90`) that
  prints the tile grid as JSON (`aether-plan/1`); the Map Converter worker
  cross-checks it against the plugin's own enumeration before every run and
  aborts on mismatch (or on an engine too old to have `plan`). Converters
  <= 0.2.8 (and unknown) snap with a bare `ceil(east / finest) * finest`
  (`_snap_bbox`, `plan.rs`), so E 11.6466 at 0.1° becomes
  11.700000000000001 and both plan a 2 m column wholly east of the area
  (owner's bbox: 2,463 tiles — `test_owner_bbox_float_edge_matches_the_engine`,
  real 0.2.7). Converter >= 0.2.9 (`_converter_snaps_with_epsilon`,
  `probe_converter_version`; toolkit changelog 43) gets the identical formula
  on both sides: `idx = floor(x/finest + 1e-9)` (south/west) /
  `ceil(x/finest - 1e-9)` (north/east), edge `round(idx * finest, 9)`
  (`_snap_bbox(epsilon=True)`), and each resolution's first row/column at
  `floor(edge/ext + 1e-9) * ext` (`_enumerate_tiles(epsilon=True)` — the
  rounded edge IS the line, 47.3, and the bare `floor(47.3/0.1)` would put
  the stray row back). That is the worker's grid and the cross-check's
  expectation (also tier B's `_plugin_enumeration`); the GUI estimate keeps
  the bare snap (an upper bound). It keeps an ON-grid edge where it is
  (S 47.3: no stray row) and drops the owner's 69th column: 68 columns of
  2 m, 2,438 tiles (1,700 + 308 + 308 + 90 + 32; `tests/test_mc_pipeline.py
  EpsilonSnap`). Never change one side alone: the cross-check would abort
  every such run.
- **Tiles outside every source (2026-11-07, owner's decision).** The coarse
  grids snap outward on their own lines (5 / 10 m 0.25°, 30 m 0.5°, 90 m
  1°), so whole tiles can lie past every layer's extent: no XYZ entry
  overlaps them, no file covers them, they rode with the first band batch
  and were converted from nothing. The owner's run of 2026-11-05 (XYZ
  Mapzen + swisstopo folder, both S 45.7648 N 48.1226 W 4.8094 E 11.5693,
  2/5/10/30/90 m): of 2,438 tiles, 56 (28 × 5 m + 28 × 10 m, the 45.5–45.75
  N row) empty, about 2 GB, no buildings (`burned onto 215 of 271
  tile(s)`). Now the worker, right after the cross-check (which keeps the
  FULL grid), drops every tile whose bbox intersects no TERRAIN row's
  extent (`_tiles_within_sources`, on `_jobs_overlapping_extent`: xyz /
  rendered / file / folder rows; a row without an extent = the whole
  service, overlaps every tile; building rows never count) before the
  fingerprints, `_tiles_to_build` and the partition, with ONE line (`  56
  tile(s) outside every source skipped (the 5 m, 10 m grids reach past the
  layers' extents): 28 × 5 m, 28 × 10 m`). Partially overlapping tiles stay;
  a dropped tile is never downloaded, rendered, written or recorded (not in
  `Conversion complete: N tiles.`); empty tiles earlier runs wrote there are
  left alone, never deleted. The label (`_estimate_tile_count_and_mb(
  entries=)`), `_estimate_disk_need` and `_pending_render_jobs` (render
  list, `_estimate_run_work`) drop the same tiles, so their count and disk
  need are what the run builds. The bare snap's stray row/column (the GUI
  estimate; the worker on a converter <= 0.2.8 or unknown) lies wholly
  outside the area, so it is dropped too: both snaps build the same tiles.
  `tests/test_mc_pipeline.py` (`TilesWithinSources`,
  `TilesOutsideEverySource`).

## Terrain folders (`core/terrain_index.py`, `gui/terrain_folder_scan.py`)

A local terrain folder (Settings → Local terrain dir, Map Converter → Add
Folder) is read through ONE persistent footprint index per folder
(`<cache>/index/<md5(folder)>.json`): per file size+mtime (change detector),
readable flag + reason, driver, CRS (WKT / EPSG authid / proj string),
native + WGS84 bounds and a densified WGS84 footprint ring, pixel size
(native + metres), dtype, bands, compression, rotation, scale/offset, nodata.
Pure GDAL/OSR, thread-safe. Rules:

- **Picking one (Site Analysis / P2P DEM combo, 2026-11-05).** Both combos
  list the Settings folder (when set and existing), then the folders picked
  with **Folder…** beside the combo, then the project's raster layers; every
  folder is a str entry `Local: <path>`, the same kind downstream
  (`_terrain_dir_checks`, `scan_folder`). ONE module, `gui/terrain_dirs.py`:
  the recent list `waveshed/terrain_dirs_recent` (both tabs, most recent
  first, deduplicated by `normcase(abspath)` like the index, at most
  `MAX_RECENT` 8; folders that no longer exist are dropped from it whenever
  a combo is filled), `combo_folders` (a recent entry for the Settings
  folder is not listed twice), `ask_terrain_dir` (the browser opens in the
  selected folder, else the last pick, else home; Cancel stores nothing). A
  pick is selected at once (`_populate_raster_layers(select_dir=)`, which
  otherwise keeps the selection across refills) and
  `main_dialog.terrain_dir_picked_in` refills the other tab's combo. Pinned
  by `tests/test_terrain_dirs.py`.
- **Never on the GUI thread.** `FolderIndex.refresh` stats every file
  (threaded) and probes only new/changed ones (plain GeoTIFFs through one
  `aether_converter probe` run, the rest in a 16-thread pool — see Index
  speed). The GUI runs
  it through `terrain_folder_scan.scan_folder` (QThread + progress dialog +
  local event loop), also for the coverage check; other folder work uses
  `run_task` (same dialog, `TaskThread`). Measured on 2,500 small LV95
  GeoTIFFs on the Windows mount: first scan 11.5 s (was 50 s per run through
  `source_file_info`), repeat 0.6 s, coverage 0.2–1.3 s off-thread (was 74 s
  of BuildVRT on the GUI thread). A run re-checks each index once
  (`reset_terrain_warnings` / the Map Converter worker call `expire_all`);
  jobs of one run reuse it for `FRESH_S`. A Site Analysis Run checks it at
  the Run click only (see One preparation per Site Analysis run).
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
  keeps `.waveshed_tiles.json` (schema 2: + per-tile buildings records, see
  Buildings on ingest jobs) in its output dir and rebuilds tiles whose
  stack fingerprint changed even without "Rebuild existing tiles" (a tile
  completed from files in Phase 2 is recorded with its files-only
  fingerprint, see Two-phase conversion); tiles with no record keep the old
  skip rule (logged) — only if whole: a short file is rebuilt.
- **Partial pool tiles (2026-10-21).** A FILE source (folder / file layer;
  never XYZ or rendered services, never the Map Converter) builds a pool tile
  only for the job's region R = tile ∩ (sector bbox grown by
  max(2 output px, 500 m)), snapped outward to the tile's pixel grid
  (`tile_regions`; profile bands use the job's output pixel too). The ingest
  job keeps the WHOLE tile geometry and lists only `_tile_contributors` of R
  (normal halo, FootprintIndex, priority order) — a cell samples only files
  containing its centre, so bytes inside R equal a whole build; outside R is
  void fill. A rotated file keeps the whole tile's materialize window (its
  one Warp is anchored at the window). Such a tile is `<name>.part` +
  `<name>.part.json` (`extent`, `regions[{bbox, fp}]` with fp =
  `tile_source_fingerprint` of that region, `abt_size`/`abt_mtime` binding it
  to the `.part`; written abt first, json second). ONE hit rule,
  `resolve_pool_tile`, used by prepare_terrain, prepare_terrain_profile, the
  views and terrain_plan: complete `<name>` + current `.src`, no flag → hit;
  else bound `.part` whose regions cover R, every region fp current, no
  `.rebuild` flag → hit; else rebuild R ∪ the `.part`'s regions ∪ the run's
  `region_union` (`run_region_union` over ALL jobs of a Site Analysis run,
  computed before the first job — a shared tile is built once); a union
  covering the tile writes the canonical `<name>` + `.src` and drops the
  `.part` pair (a `.part` write drops a superseded complete tile). Every
  ingest output goes to `<pool>/.incoming-*/` and is renamed into place —
  pool tiles are replaced, never rewritten (views hardlink them; the XYZ
  download path is unchanged). The rename is `replace_with_retry` (a Windows
  refusal — file held open: PermissionError / error 5, 32 — retried 5 × 0.1 s);
  still refused → the previous copy stays (`.part.json` restored), only the
  new file goes, the tile is flagged for rebuild. `_tile_ready`
  / `_listed_tile_ready` / `_tile_is_whole(<name>)` / `*.abt` listings never
  see a `.part` (pinned by `tests/test_partial_pool_tiles.py`). Views link
  the `.part` under the canonical name (`_sync_view(tiles=...)`, link mode:
  no flag/fp check, newer of complete/`.part` wins; a missing or renamed
  source is never "fresh"); the view key hashes the regions the run needs.
  Logs: `cache MISS — n of m … (k partial: region x deg² of their y deg²)`,
  `terrain plan: … k of them partial`; `terrain_plan(source_infos=…)` also
  prices the files the regions read (Site Analysis: off-thread, raster
  folders). The torture runner globs `*.abt.part` too and compares the two
  tabs over the analysis box only (`_cross_tab_contract(bbox=…)`).

**Index speed (2026-10-14).** `terrain_index.probe_geotiff_fast` reads an
ordinary GeoTIFF's first IFD from one 64 KB read (no GDAL; CRS + WGS84 transform
looked up once per EPSG code) — one band, standard sample type, known
compression, one tiepoint or a matrix, an EPSG code, no sidecar (`.aux.xml`,
`.prj`, world file…); anything else returns None and goes through the GDAL
probe, so unusual files are judged exactly as before
(`tests/test_terrain_index_fast.py` holds both probes to identical entries;
the identity-geotransform case builds its own file with the torture
generator's recipe, so nothing in the suite depends on the gitignored
`data/torture` — a skip there would fail the release check). The scan takes
sizes/mtimes from the directory
listing (free on Windows, parallel `stat` elsewhere), sidecars are looked up in
the listing, the probe reuses the scan's stat, and the index is written in one
`write`. 2,500 files on the container's Windows mount: first scan 24.9 s →
3.3 s (raw open+read floor there 1.35 s), repeat 1.2 s → 0.7 s.

**Converter probe (2026-10-21).** The Python probe is GIL-bound (43,650
swissALTI3D files: 84 s cold). With ≥ `PROBE_MIN_FILES` (64) new/changed
plain `.tif`/`.tiff` files (no sidecar — the probe reads none — not `/vsi*`)
and `aether_converter --version` ≥ `CONVERTER_PROBE_MIN_VERSION` (0.2.7,
`terrain_index.probe_converter`; absent/older/unknown → the old path
unchanged), `refresh` writes the path list to a temp file and runs
`aether_converter probe --list` ONCE (`run_probe`: `subprocess` with the
plugin's creationflags/UTF-8 conventions, lines parsed as they stream in —
`aether-probe/1`, one per file in list order, matched by `path`; progress
every 256 lines; Cancel = a watcher polling `should_cancel` every 0.1 s
kills the process, the lines already read become entries and are saved, the
next refresh probes only the rest). `entries_from_probe` builds each entry
exactly as `probe_geotiff_fast` would — `_epsg_crs` once per EPSG code, the
footprint rings of ALL files of a CRS in ONE batched `TransformPoints`
(`_geo_fields_many`: distinct points only, grid tiles share corners/edges;
the 80-point edge sampling only for files whose ring's curvature bound
|Δ²|/8 exceeds `_RING_BBOX_EPS_DEG` = 2e-8° — 1 km LV95 tiles never do,
deviation ≤ 3.6e-9° measured; footprint rounding identical to `round(x, 7)`
via `_round7`), dtype/compression names, nodata (`"nan"`/`"inf"`),
scale/offset (null → 1/0), rotation/flip, readable as `problem_of` derives
it. Left to `probe_file` (GDAL) unchanged: `ok:false` lines, i8/u64/i64,
codecs outside `_TIFF_COMPRESSION`, CRS-less or non-EPSG files, the identity
geotransform, an uncompressed file shorter than its pixels (cut off →
GDAL says truncated), a line whose size+mtime differs from the listing, a
failed run, and a COMPRESSED file on a line without `last_chunk_end` (a
converter built before toolkit changelog 35). **Truncation** (was a known
gap): `last_chunk_end` = end of the bottom-right block, the one GDAL's probe
reads; > `size` or null → the entry GDAL gives a truncated file (readable
False, `TRUNCATED_REASON` + what the probe saw, no CRS/geometry —
`problem_of` unusable), pinned by `test_compressed_pixels_cut_off_behind_the_header`
(real converter and recorded fixture; the fixture's `last_chunk_end` is
re-based on the size of the file written at test time). Parity: that file runs the real converter
when AETHER_BIN_DIR/PATH has one ≥ 0.2.7, else the recorded
`tests/fixtures/probe_parity.jsonl` (re-record with `python3
tests/test_terrain_index_fast.py --record <exe>`); `tests/test_probe_scan.py`
drives `refresh` through the real subprocess path. Log: `folder index …
probe: N files in X s (open … ms, read … ms avg), entries built in Y s; K
left to the in-process probe`. Python side for 43,650 synthetic lines: run +
read + parse 0.6 s (overlaps the converter's own I/O), entries 1.5 s (GC
paused while building, `_no_gc`); the one index `write` (48 MB) is extra.
1,000 real files on the container mount: entries identical field for field.

**Windows Defender: a note, nothing more (owner decision 2026-10-23; the
exclusion offer of 2026-10-21 — status queries, dialog, UAC prompt — is
gone).** The plugin never queries or changes Defender, and
`gui/terrain_folder_scan` starts no process (no `subprocess` import; pinned
by `TestColdScanNote` in `tests/test_probe_scan.py`). Windows only
(`sys.platform == "win32"`, `_is_windows`; nothing at all elsewhere): when
`scan_folder`'s progress reaches phase `reading` with total ≥
`COLD_SCAN_NOTE_MIN_FILES` (1000 — at the typical 2-5 ms Defender adds per
first open that is ≥ 2-5 s of scan, ~86 s at the owner's 86 ms; smaller
reads are over before the note could be read), the dialog label gets one
more paragraph, `COLD_SCAN_NOTE` ("Windows Defender scans every file on
first open. For large folders, exclude this folder and the Waveshed terrain
cache from real-time scanning (Windows Security → Virus & threat protection
→ Exclusions)."), wrapped to 72 columns, kept until the dialog closes, and
logged once per folder per session (Info, with the folder and the cache
path). Every bulk read opens with a `(0, n, "reading")` tick — before the
converter probe starts (its own ticks come every 256 lines) and before the
in-process probes read their first file — so the note shows at its start.
`RefreshStats.open_ms` stays (the probe's `open … ms avg`, also in the
probe log line); `_time_opens` is gone.

**The index outlives the session.** A converter-probed index is read back by
the next session — the same process with every in-memory index dropped, and
a real second process (`TestTheIndexOutlivesTheSession`: 70 plain GeoTIFFs,
real converter): every file only stat'ed, no probe, no data file opened, the
index read from `<cache>/index/<md5>.json`, for the folder given plain, with
a trailing separator, relative, or in another letter case (Windows;
simulated with its normcase) — `load` keeps the spelling the index was
saved with, so the entries (full
paths) keep matching (another spelling listed every file under new keys:
all "new", the whole folder probed again). `save` renames through
`terrain_adapter.replace_with_retry` (Windows refuses the rename while
Defender or a second QGIS holds the file) and logs a save that still fails
(`folder index …: could not be saved to …`) — it used to be dropped
silently, costing the next session the whole folder.

**Converter diagnostics file (2026-10-21).** `run_converter_streaming` gives
every engine child `AETHER_DIAG_FILE` = `<cache>/logs/aether_converter-<YYYYmmdd>.log`
(`diag_file_path`) unless its environment (the caller's `env`, else the
process's) already sets one — an explicit value wins. Converter ≥ 0.2.8
(toolkit CONTRACT changelog 37) appends its `[Rust] tile …` / `[Strip …]`
diagnostics there instead of the console; parsed lines are unchanged. The
first launch of a session (and of a new day) creates the directory, deletes
`aether_converter-<date>.log` files in it older than 14 days (mtime; nothing
else) and logs `converter diagnostics: <path>` once; a directory that cannot
be made or listed is logged once, the launch goes on (env unchanged).
`_run_converter` drops `[Rust] tile ` lines (0.2.7 still prints them, 920 per
run) from the panel and the error tail. Settings → Clear Terrain Cache keeps
`<cache>/index` unless its checkbox is ticked (`clear_index`: `<md5>.json`
and `abt-<md5>.json` only, in-memory indexes forgotten).

**QGIS process memory line (2026-11-07).** The converters print their own
peaks (toolkit changelog 50: `peak rss N MB` on `[Rust] run:`, `[Stats] Peak
RSS:` per download pass), and the owner's run still had ~20 GB of 31 that no
line attributed. Once per run — the Map Converter worker right after the
lanes switch, the Site Analysis worker right after the engine gate — the tab
logs `  QGIS process: 4,812 MB resident (peak 6,201 MB)`
(`core/process_memory.py`: Windows `K32GetProcessMemoryInfo` from kernel32,
then psapi's `GetProcessMemoryInfo`, through ctypes with declared
`argtypes`/`restype` on fresh function objects (`dll[name]`, never the shared
`windll` attributes); Linux `/proc/self/status` `VmRSS`/`VmHWM`; macOS
`ru_maxrss` peak only; standard library, never raises, no line when the OS
does not say). The owner's Windows runs of 2026-11-08 logged NO line with
the first version (undeclared `GetCurrentProcess` return type); the
hardened call is not yet confirmed on Windows — check with
`from waveshed.core import process_memory; print(process_memory.resident_bytes())`
in the QGIS Python console. `tests/test_process_memory.py` (fake `windll`
objects cover both DLL paths), pins in `test_download_lanes.py` and
`test_site_analysis_worker.py`.

**Tile → file matching (2026-10-20).** ONE source of truth:
`_tile_contributors` (the exact halo rule), always through a
`FootprintIndex` for real runs — 0.1° buckets of `wgs84_bounds`; a lookup
takes the buckets the tile grown by the halo rule's maximum meets, then runs
the exact test on those in priority order (odd/huge files: every tile), so
the list is the full scan's element for element (property test,
`tests/test_footprint_index.py`). Built ONCE per run — Map Converter worker
before the fingerprints (status "Matching N files to T tiles..."),
Site Analysis in its `TerrainPrep` (below; without one: `prepare_terrain`
where `_infos_near` runs, `prepare_terrain_profile` once for all bands,
`source_infos`) — and each tile's match is kept on the index, so
fingerprint, Phase-2 match, download estimate and the job's `sources[]`
share it. Never add a
second tile→file filter. 43,650 files × 2,610 tiles: fingerprints 100 s →
0.6 s, job build 87 s → 3.3 s.

**One preparation per Site Analysis run (2026-10-23).** The owner's Run on
43,650 swissALTI3D files checked the index twice (scan thread, then the
worker after `expire_all`), built the source infos three times (coverage
check, plan, worker) and a FootprintIndex twice. Now the index is checked
at the Run click only (`scan_folder`), and `_terrain_dir_checks` (the
coverage check's thread) builds ONE `terrain_adapter.TerrainPrep`
(`build_terrain_prep`: `folder_source_infos` — its "local dir" / "overlap
priority" lines — + one FootprintIndex, and the signature they were read
under, recorded by `folder_source_infos(used=)` under the index lock: the
`FolderIndex` object, its `generation` (bumped by every refresh that
changes entries), `refreshed_at`, `dir_mtime_ns` (the folder's mtime read
before the listing), overlap priority, approved answers). The coverage
warning (`terrain_coverage_warning(infos=)`: no second file list) and the
plan come from it; `_on_run` → `_launch_worker(terrain_prep=)` →
`_SiteAnalysisWorker(terrain_prep=)` → every job's `prepare_terrain` /
`prepare_terrain_profile(terrain_prep=)`. While `stale_reason()` is None
(same index object and generation, younger than `FRESH_S`, folder mtime,
priority and answers unchanged) they skip the index re-check, source-info
build + sort and FootprintIndex build (log `source files: N usable, from
this run's terrain preparation (folder checked X s ago)`): `prepare_terrain`
still narrows with `_infos_near` and matches through `SubsetFootprintIndex`
(the full index's answer kept to the sub-list, at sub-list positions — the
materialized window copies keep their names), so the jobs are the ones a
read gives; a profile uses the bundle's list as its `source_infos`. Stale →
`terrain preparation of this run not reused (…)` and the folder is read as
before (the worker's `expire_all` stays, so that read re-checks). Callers
without a bundle (Processing, P2P, the torture runner) are unchanged. The
finished scan thread (`scan_folder`) and worker drop the bundle (~27 MB for
43,650 files). Pinned by `tests/test_terrain_prep.py` (one Run of four jobs
= one `_walk_stat`, one `source_infos`, one FootprintIndex, each log line
once; fixed/profile jobs, coverage text and plan numbers equal with and
without; the stale cases). Container, 43,650 synthetic LV95 files on the
Windows mount, 10 m / 50 km (26 tiles): start of `_terrain_dir_checks` →
converter launch 34.7–39.1 s → 22.0–24.1 s (the worker's 10.7–14.9 s index
re-check and 0.4–0.5 s source infos gone; its fingerprints reuse the
plan's matches); what is left is `preflight_ingest_jobs` (18.4–20.6 s
there — one `os.path.exists` per distinct source, 10,487, over 9p), the
coverage grid (2.4–2.8 s), the bundle (0.5–0.6 s) and the plan
(0.1–0.4 s).

**Two-phase conversion (Map Converter, 2026-10-21; replaced the per-file
void scan — `has_voids`/`scan_voids`/`ensure_void_flags` are no longer
called, the owner rejected its 77 s one-time scan).** After the stale check
(`_tiles_to_build`), `_partition_pending` splits the pending tiles an XYZ
entry overlaps: PROVEN = the RECTANGLES of the Phase-2 stack's files
(`_phase2_entries`: the entries ABOVE the first XYZ entry — below it the pool
outranks them, so only those give a byte-identical result) cover tile + halo
(`terrain_adapter.rect_cover_contributors`, geometry only; two CRSs, rotated
files, an untransformable CRS = not covered); every other one (no file, a
partial one) needs the pool. After the disk check, from t = 0 all run at
once: the pool download of the unproven tiles — one `_DownloadBranch` thread
per POOL (entries sharing one — the same XYZ layer twice — share its jobs,
`_pool_calls`, each keeping its own tiles), several pools concurrently — and,
in the worker thread, the INGEST QUEUE, Phase 2 first: the proven tiles from
those files alone into `<output>/.phase1-<pid>/` (the converter's atomic
temp + rename stays inside it). **Bands (2026-10-23).** Each pool downloads
per latitude band (`_band_height`: the coarsest output tile extent of the
run, >= 0.25°, on that grid; a tile belongs to the band of its centre,
`_band_of`), north to south, one converter `download` job per band and zoom,
finest zoom first (`_band_calls`; the toolkit derives coarse source tiles
from the fine ones it cached); logged once (`Downloading terrain for N
tile(s) … : 3 band(s) × z15, z12, z11 — …`). **Two lanes per pool
(2026-10-25; owner's run 2026-10-24: z15 at line rate, the coarser passes
disk-bound from the raw tile cache with the network idle, ~150 s of a
750 s lane).** `_DownloadBranch` runs two threads, one converter process
each: the FETCH lane (`waveshed-download-<k>`) runs each unit's finest
call in unit order (the hole unit `push_front`-ed in front, as before);
when it ends, the unit's coarser calls go to the DERIVE lane
(`waveshed-derive-<k>`), in the order the fetch lane finished them. **One
band behind (2026-11-05, owner's decision):** a band's coarser calls are
held (`_held_for`, polled by `_next_derive`, which takes the first unit of
its queue that may run) until the NEXT band's finest call has landed —
the toolkit derives a coarse source tile only when the cache holds all
four finer children, and a source tile straddling a band edge has two of
them in the next band, so without the hold those edge tiles were derived
or fetched (the service's own, differently resampled z14) depending on how
far the concurrent z15 pass had got, differing between runs and from the
derived interior; held, they derive from z15 like the interior in every
run — band b's z14/z12/z11 beside band b+2's z15, the last two bands'
coarser passes after the fetch lane is done (the owner's run of
2026-11-05: about a minute of 18 min; the hole unit is never held, and a
hole pass can still make a tile on the pool/local boundary derivable in
one run and not another — accepted residual). The timing line of a held
unit ends in `; held N.N s until band 3/4's z15 landed`. **Lane tags
(2026-11-07, owner):** two lanes log into one panel, so with two lanes every
download line the panel gets is prefixed with its pass's zoom — `    [z14]
[Download] 10% (575/5742) …` beside `    [z15] [Download] 10% (8113/81127)
…` — through `terrain_adapter.set_panel_tag` / `panel_tag` (a thread-local
read by `_run_converter_download_once`'s panel copy only; the raw lines and
`on_line` never carry it), set by `_run_pool_calls(lane_tag=True)` for the
lane's thread and for the call only; one lane (`WAVESHED_SERIAL_ZOOMS=1`)
and Site Analysis set none. `_unit_done`
fires once, from the
lane that finished the unit last (a one-call unit: its call); the branch
`ended` once closed and both lanes drained; either lane's failure is the
branch's `error` (first only) and `_halt`s the run; Stop kills both
(`on_start` = `_register_proc`). A/B switch `WAVESHED_SERIAL_ZOOMS=1`
(`SERIAL_ZOOMS_ENV`, `_serial_zooms_requested`, read once per `run()`):
one lane, the order before, call for call; one log line `  download
lanes: one per pool …` / `two per pool …` with the first branch. The
gain is unmeasured on the owner's single HDD. Real 0.2.11 converter once
(container scratch run, not in the suite; 2 m + 30 m, two bands, local
terrarium server): two download processes at once, every output tile
byte-identical to the one-lane run, the same raw-cache entries, no lock
warning. Two processes per service now share the raw tile cache's packs:
the pack lock serializes writes, but a derived z14 or coarser entry can
replace a real one the other process wrote meanwhile (toolkit
`write_pack` writes entries unconditionally), and which edge tiles are
derivable depends on how far the concurrent z15 pass has got.
`tests/test_download_lanes.py`. As soon as a pool's band has
landed (`_unit_done`), the pending Phase-3 tiles whose pool tiles are ALL in
place (a tile under several pools waits for each; tiles no XYZ entry
overlaps ride with the first band — or form a batch at once when nothing
downloads) are queued as one Phase-3 batch (`_Batch`); the worker runs the
queue one ingest at a time (`_drain_queue`/`_run_batch`) while the downloads
go on, and ends when it is drained and every branch has ended. Phase 2
prints `[Tile] <name>
covered=<a>/<b>` per written tile (aether_converter >= 0.2.7,
`_TILE_LINE_MIN_VERSION`, probed through
`binary_manager.probe_converter_version`; `terrain_adapter.parse_tile_line`;
parsed, not logged — the per-tile `[Rust] tile …` timing line is dropped too,
`[Rust] run:` stays). covered == total → moved into the output dir
(`replace_with_retry`; a move Windows still refuses is logged and queued like
a hole), final, manifest = the tile's files-only fingerprint
(`_stack_tile_fingerprints(files_only=…)`: Phase-2 stack + buildings + the
`_FILES_COMPLETE_TAG`, no pool part — a later run accepts it while those
files are unchanged; the tag keeps a files-only build whose holes were never
checked, e.g. from a stack without XYZ, from matching it). A hole (no-data
inside the files) or no line → deleted, queued: when Phase 2 ends, the
queued tiles get ONE extra download unit per pool (`_schedule_holes`, pushed
in front of the pool's next band — after the band it downloads now; slots
`("download", k, zoom, 2)`), then their own batch. Every Phase-3 batch
converts with the whole stack into the output dir (manifest = full-stack
fingerprint), job for job the one-shot build (real 0.2.7 binary,
`RealConverterEndToEnd`, and `RealConverterBands` for two band downloads +
two batches: every tile byte-identical to one ingest of the whole stack
over one download job; a second run rebuilds nothing). Per batch: job file
`%TEMP%/aether_conv_<pid>_<id>.json` (Phase 2: `_files`), removed after it;
its own progress slot; its building tiles only (`_pbf_view`); its window
copies removed after it; manifest records through `_write_output_manifest`
(read-modify-write under `_MANIFEST_LOCK`, temp + `replace_with_retry`).
Disk is checked before anything starts (Phase-2 tiles + copies, every band
download, every Phase-3 tile), before the hole download (what is still to
be written: unconverted tiles, the band downloads still in flight, the hole
pool tiles) and before each batch (its own tiles + copies). Older/unknown
converter → no Phase 2 (log `Phase 2 skipped: …`), the pool for every
overlapping tile. Logs `Phase 3 (band 2/3): running the converter (N
tiles)...` per batch, `Phase 3: N tile(s) converted with the whole stack in
K batch(es).`, `Conversion complete: N tiles.` (Phase 2 + Phase 3).
**One download per zoom:** `_xyz_download_plan` groups an entry's tiles by
the zoom the downloader fetches them at (`terrain_adapter._download_specs_by_zoom`
— tile centre latitude + resolution, capped at zmax), so 2 m and 5 m are ONE
z15 job per band (each spec its own `resolution_m`/`size_px`; the owner's run
fetched z15 twice, 12 + 15 GB) and 2 m + 30 m two; the progress slots,
`_disk_items` and the worker use that plan (mixed job = per-resolution jobs
byte for byte: `TestOneJobPerZoomRealConverter`). **Stop / failure:** every
engine process — each band's and the hole pass's download, Phase 2, each
batch — registers through `_register_proc` (the list `_procs`); `cancel()`
kills them all (`_kill_procs`: terminate all, 3 s for all, then kill;
through `Popen` only), a process registered after Stop dies on
registration. A failed branch or batch (download error, converter error,
buildings) stops the others and the queue (`_halt`, the same kill); the run
ends with that error once all have ended — tiles of batches that finished
before keep their records; Stop (and a download that cancelled itself) ends
quietly. Cancel or failure at any point removes `.phase1-*` (stale ones of a
killed run too — `_cleanup_scratch`, `_run_phase2`) and every job file;
nothing from Phase 2 ever sits in the output dir unpromoted, so "no record →
kept" cannot keep a VOID-holed tile. Status `Converting N tiles from local
files; downloading terrain for M tile(s) (z15)` (`...` when nothing
downloads), `Converting band 2/3: N .abt tiles...`, log `Phase 2: N tiles
from local files — K complete, M need the pool`. Pinned by
`tests/test_two_phase_convert.py` and `tests/test_mc_pipeline.py`.

**Disk safety (2026-10-21).** Need = the output tiles to build (new, short,
rebuilt by fingerprint — `_tiles_to_build`, the worker's rule) + the pool
tiles not yet in their pool (estimate: `_pool_estimate_tiles`; worker:
exactly what Phase 2 left) × the real `.abt` bytes
(`terrain_adapter._abt_whole_bytes`: 44 + 256-aligned stride × size; 2 m /
0.1° = 62,582,828 B) + the window copies of `materialize`-routed files
(`terrain_adapter.materialized_window_bytes`: clamped tile+halo window in
native px × the copy's sample size, Float32 when unscaled/warped) + the raw
source tiles a download adds to `<cache>/xyz` ("raw tiles", only when
something is fetched: `terrain_adapter.raw_tile_bytes` = per zoom group the
converter's fetch set, `_fetch_tile_count` — download.rs `tiles_to_fetch`
mirrored, equal to its `[Download] z=` total — × `XYZ_SOURCE_TILE_BYTES`
200 KiB, the converter's own high per-tile guess; measured 83 KB on AWS
z15), grouped per drive of output dir / pool / source-tile cache / %TEMP%
(`output_estimate.drive_needs`, `shutil.disk_usage`). Refused when need > free − 2 GB
(`DISK_RESERVE_BYTES`). The label adds per drive `X: need … GB / free … GB
(…)`, red when short (`_estimate_disk_need`, in the label's estimate thread
— below: folder infos from the IN-MEMORY index, never refreshed, read under
`index.lock`; a file layer probed once; per-tile verdicts memoised per stack
in `_est_cache`; with a buildings FILE in the stack recorded tiles count as
kept — OpenFreeMap rows are fingerprinted). `_on_run` refuses with the same numbers
("Not enough disk space"); the worker re-checks exactly before Phase 2,
before the hole download (what is still to be written after Phase 2) and
before each Phase-3 batch (its own tiles), failing with `Not enough disk
space for …` instead of ENOSPC. The "exists" skip is
"whole": a file shorter than its spec (and not whole by its own header) is
rebuilt (`_output_tile_whole`, also in `_build_tile_jobs` and the render
list). Site Analysis: `_disk_refusal` puts `terrain_plan`'s `download_mb`
(0 for a pre-built `.abt` folder) and its `raw_mb` (XYZ sources only, the
same `raw_tile_bytes` of the missing tiles; "raw tiles") on the cache drive
and the coverage rasters on the output drive, refusing the same way (with a
resolution that would fit); `output_space_problem` keeps only the "Very large result"
question.
**Keep pool tiles (2026-10-23, default OFF;** box next to "Rebuild existing
tiles", `waveshed/map_converter/keep_pool_tiles`, `_keep_pool_tiles_setting`).
The pool copy only pays off for Site Analysis from the same XYZ source, else it
doubles the disk use. `ensure_pool_tiles` returns `PoolTiles` (a dict, plus
`pool_dir`, `fetched` = missing/short/flagged at the call, `present`);
`_run_pool_calls(on_pool_tiles=)` → `_note_pool_tiles`; off → after a
SUCCESSFUL run only (never Stop/failure: the next run reuses them)
`_drop_run_pool_tiles` → `terrain_adapter.remove_pool_tiles` deletes exactly
those `.abt` + `.src` / `.rebuild` and logs `Removed N pool tile(s) this run
downloaded (X MB freed …)` (a file still hard-linked from a view frees nothing,
counted apart); pooled-before tiles and `<cache>/xyz` are never touched. The
need still counts the pool tiles (they exist during the run); with the box off
the label adds `(pool tiles removed at the end)` to the pool's drive line.
Pinned by `tests/test_keep_pool_tiles.py`.
**The label off the GUI thread (2026-11-07, owner: "the checkboxes lag
until the size estimate is done" — every toggle ran both estimates on the
GUI thread; a synthetic copy of the owner's stack, 43,659 LV95 files + XYZ,
2/5/10/30/90 m, took 0.3–3.7 s per toggle in the container).**
`_update_estimate` (every box, "Rebuild existing tiles" — now wired too —,
"Keep pool tiles", row added / removed, extent set, settings) bumps
`_est_generation` (a running job's `superseded` event set — `should_cancel`
of `_estimate_disk_need`, polled per tile — a pending one dropped), answers
the file-free cases at once (`_estimate_precheck`: the three old texts),
else sets `_ESTIMATING_TEXT` ("Estimating tiles and disk space…") and
restarts the single-shot `_est_timer` (`_ESTIMATE_DEBOUNCE_MS` 150). On
timeout `_estimate_job` reads PLAIN inputs on the GUI thread
(`_EstimateJob`: rows as `_plain_entry` copies — the QGIS layer as
`_PlainLayer`, its `source()` string, a read error re-raised where the
estimate reads it; extent / decisions / CRS answers copied; overlap rule
resolved —, out_res, output dir, both boxes, `_est_cache` + `_est_lock`)
and runs it in the tab's ONE `_EstimateThread` (QThread, restarted per
job, held in `_LIVE_ESTIMATES` while it runs); a job taken while one runs
is THE pending one (a newer replaces it), started from `finished` if still
current. `_run_estimate` = count, then the need under `job.lock` (`_on_run`
prices under `_est_lock` too, with the same cache); `_estimate_answer`
turns any failure into the `disk estimate failed: …` warning (a failed
count: label `Estimate failed: …`), never raised; `_on_estimated` shows a
result only while its generation is the latest (`_show_estimate`: the old
text and colours). Deep inside, `get_cache_dir` still reads QgsSettings in
the thread, as the run's worker always has. `cancel_worker` →
`_stop_estimate` (no wait). Tests: `flush_estimate()` runs the request
inline (the stub QThread never runs); `tests/test_mc_estimate_thread.py`.

## Map Converter progress and extents

**One bar, weighted by work (`core/run_progress.py`).** No fixed phase spans.
`RunProgress` holds slots of estimated work in one unit (a downloaded 256²
source tile; convert/render per 256² output block, `*_UNITS_PER_BLOCK`):
`("render", i)` only for entries that render, `("download", k, zoom)` per
(entry, zoom) download call — its band jobs report into it on top of the
ended bands' units and below the later bands' estimates (`_SlotLedger`,
sized per band by `_estimate_download_work(height=, ledger=)`; each
RUNNING call kept apart, `_LedgerCall`, the slot = their sum — two lanes
can run two calls of one slot at once), so it completes after its last
band; each pool's two lanes are two activities (`_download_activity(k,
derive=)`, an idle lane's ended) — (`("download", k, zoom,
2)` for the second pass over Phase 2's holes), `("convert", "files")` for
Phase 2 (two-phase conversion), `("convert", <batch id>)` per Phase-3 batch
and `("convert",)` for the Phase-3 work no batch holds yet (sized with every
Phase-3 tile; a forming batch MOVES its tiles' `_convert_work` into its own
slot, so the total never jumps); the worker sizes the concurrent download
from its partition (`set_work`, all download slots zeroed first via
`keys()`), the second pass adds its slots when Phase 2 has spoken;
`("buildings",)` is the OpenFreeMap fetch (one unit per z14 tile to fetch,
activity `Downloading buildings (OpenFreeMap z14)`). `_on_run` estimates (geometry only,
no pool stat on the GUI thread), the worker refines (pool-checked from ONE
directory listing, `_listed_tile_ready` = `_tile_ready`'s size rule — also
`ensure_pool_tiles` and the Site Analysis stale check; only a file shorter
than its spec's tile has its own header read; exact jobs).
Remaining-work rule — finishing Δ advances `f += Δ(1−f)/remaining` — so the bar
never goes back, moves with every finished unit (retry passes ADD their work
when they start), and is 100 % only when all slots are done. Between converter
lines (download: every 10 % of a pass; ingest: every 10 tiles) each slot may
creep at its own smoothed rate up to its next certain report
(`next_download_report`) — per slot, so Phase 2 and a download reporting in
turn neither reset nor double-count each other's creep. While they run
together each is an activity of the model (`set_activity` /
`end_activity`; every Phase-3 batch is one, keyed by its slot); the label
names all, in start order: `Converting band 2/3: 120/531 tiles;
Downloading terrain z15 (2, 5 m) band 3/3: 12,345/193,978 source tiles,
25.1 MB/s · ETA …` (also `Converting tiles from local files`, `Converting
Phase-2 holes`, `… (Phase-2 holes)` downloads). Only
existing toolkit lines are parsed: `[Download] z= tiles=AxB=N` (pass total up
front), `[Download] X% (a/b) — MB/s, errors, in-flight`, `Progress: a/b`, and
(toolkit 0.2.9) a batch's building decode, `[Rust] Decoding building tiles:
n/m` every 1,000 tiles + `[Rust] Building tiles decoded: M tile(s), B
building(s) in S s` before its first `Progress:` line: a batch with
`buildings_pbf_dir` jobs books `BUILDING_DECODE_SHARE` (10 %) of its slot to
the decode (creeping to `next_decode_report`), label `Converting band 2/4:
decoding building tiles 12,000/20,808` (detail `step`); without those lines
the first `Progress:` line jumps the share (the header — `… tiles in
"<dir>"`, on 0.2.14 `[Rust] Decoding N listed building tiles from "<dir>"`
— moves nothing). A step without a counter is
named alone: `Converting band 2/4: waiting for building tiles` while a
>= 0.2.12 batch's converter waits for its view's marker (>= 0.2.14: for
its list; the jobs carry `buildings_pbf_ready` or `buildings_pbf_list`) (its `Progress:`
lines move the bar meanwhile, the label waits for the ready line). **ETA:** stretched after
`QUIET_S` of silence, and `ETA —` (`ETA_UNKNOWN`, `eta_stalled`) once no slot
has reported progress for `ETA_BLANK_S` (30 s) — never extrapolated (the
owner saw "days"); `test_run_progress.py`.
`DownloadProgressAggregator` counts source tiles per zoom group + a 1 % tail held
until the group's outcome is known; `progress_cb(frac, label)` stays the
contract (Site Analysis, Processing), a callback with a `download_event(info)`
attribute also gets the counts. The worker's progress `status` texts keep
their shapes (`Downloading terrain (z…): a/b source tiles`, `Terrain
download: a/b tiles ready`, `Converting tile a/b` — the torture runner
filters them); the tab's label comes from the model.

**Resolutions.** The run's output resolutions are the "Generate tiles at"
checkboxes; the per-row "Target Res" column (never read by a run) is gone —
`_LayerEntry` still accepts `target_resolutions=` and ignores it, so callers
and stacks that carry it load silently. At run start `_upsampling_warnings`
logs one line per source a checked resolution is finer than (by > 10 %,
`_FINER_THAN_SOURCE` 0.9); the "Upsampling Warning" confirmation (an output
finer than EVERY source) lists them.
**Auto (2026-11-07, owner: "a reasonable range, especially with more than
one coverage layer").** `auto_output_resolutions(entries)` (pure): the
finest `native_res_m` of the TERRAIN rows (`_finest_terrain_res`; building
rows never count; filled at add: a folder from `_index_finest_res`, XYZ
from `xyz_native_resolution_m`, a layer from `_detect_resolution`) snapped
UP to a key of `_ABT_EXTENT_DEG` with the upsampling tolerance
(`_tile_resolution_for`: the smallest key ≥ 0.9 × native — 2.0 → 2, 4.8 → 5,
9 → 10, 25 → 30, 30.9 (1″ at 0°) → 30, 85 / 92.6 → 90, past 100 → 250,
finer than 2 → 2; strict snapping sent 1″/3″ data at low latitudes a whole
step coarser), plus `_AUTO_RINGS_M` (30, 90 — the adaptive profile's rings)
coarser than it; no terrain row with a known resolution → [30]. ONE fine
resolution for the stack: the owner's (2 m folder + Mapzen 4.8 m) gives
[2, 30, 90] — the hand-ticked 2/5/10/30/90 built 616 tiles of 5 m / 10 m
(24.1 GB; 560 / 21.9 GB without the tiles outside every source) no profile
uses. A fine small-area row beside a coarse large-area
one puts the fine grid over the whole combined extent (the estimate shows
it). Tab: 30 m ticked until the first row; the "Auto" button right after
the boxes (`_btn_auto_res`, tooltip `_auto_resolutions_tip`: the rule + this
stack's result) → `_on_auto_resolutions` (apply, log, re-arm);
`_on_stack_changed` (every add — layer, all layers, folder, both buildings
rows —, Remove, `_set_extent_rows`) applies it while
`_res_user_touched` is False and always refreshes the estimate; a box
changed by hand (`_on_res_toggled`, `stateChanged`; the rule's own ticks
under `_res_applying` are not the user's) sets it. One log line when the
ticks change or Auto is pressed: `  output resolutions: auto 2 m, 30 m,
90 m (finest source 2.0 m; the analysis profile's 30 m and 90 m rings)`.
`tests/test_mc_auto_resolutions.py`.

**Extents.** Rows: ExtendedSelection — Draw Extent / Extent from Layer / Paste /
Remove act on all selected rows; Up/Down/overlap rule on the current row.
Extents are copied per row (`_apply_extent`), never shared. A folder row's
extent defaults to the union of its usable files' `wgs84_bounds` from the scan's
index (`_folder_extent`; skipped/unusable files excluded, CRS-less files only
with the user's CRS); "Whole folder (all files)" in the picker re-scans
off-thread. Add All Layers prefills each file layer's own extent; services
(XYZ/WMS) get none — their extent is the whole service.

**Timing lanes (2026-10-23; Info lines, `perf_counter`, parsed by nothing).**
The owner's run had 3 min 48 s of unexplained silence between a degraded z15
pass and its repair pass. `run_converter_streaming` keeps per thread a
`StreamTiming` (launch, first/last line, reaped exit; `last_stream_timing`,
`forget_stream_timing`); `_try_rust_download`'s `_PassTiming` logs per pass,
when the next pass is about to launch (last: `next launch —`): `timing: z15
pass 1: converter exit +X.Xs after its last line; post-pass parse A.As, flags
B.Bs, degraded/repair plan C.Cs, next launch D.Ds` (`mark` points in
`_run_pass` / `_download_zoom_group`; the gap scan, when a pass needs one —
see Degraded download tiles — is "plan"; ~0 for a degraded-only pass).
Per Phase-3 batch, right before its converter starts (`_run_ingest(
before_launch=)`): `timing: band k/n ready at +X s, started at +Y s (queue
wait Z s), building view W s, disk check D s, jobs J s, launch prep L s`
(zero = the run's start, `_t0`; `_Batch.ready_t`). `tests/test_timing_lane.py`.
Per download unit with coarser calls (both lanes done, before its batch
forms; `_DownloadBranch._timing_line`): `timing: band 2/4: z15 landed at
+X s, coarser passes landed at +Y s, ran beside band 3/4's z15: yes` —
"yes" when another unit's finest call overlapped its coarser passes (the
first such named), else `… : no` naming the next unit, or `ran beside
another band's z15: no (none was downloading)`; one lane always says no;
a unit held for the next band's finest (one band behind) appends `; held
N.N s until band 3/4's z15 landed`. One-call units log none.
`tests/test_download_lanes.py`.

**Inspect .abt (2026-10-23).** Was: every tile at the finest resolution into a
16,384² canvas on the GUI thread (2,438 tiles ≈ 60 GB of reads, resolutions
overwriting in file order). Now `_on_inspect_abt` runs `abt_inspect.plan_folder`
(groups per resolution from the header index — counts, MB, `coverage` of the
union of all tiles' extents, no body read) and the mosaic in
`terrain_folder_scan.run_task` (`TaskThread` + the scan's dialog + Cancel);
`_ask_inspect_choice` lists the resolutions, default `default_choice` = the
coarsest covering ≥ 95 % (`COVERS_FRACTION`; none → coarsest first until
together), `choice_note` gives the read volume and warns above 2 GB
(`BIG_READ_BYTES`). `abt.mosaic_to_geotiff(headers=, overviews=True,
max_dim=INSPECT_MAX_DIM 8192)` reads only those tiles and only the rows the
canvas samples (`read_tile_rows`, `mosaic_read_bytes`), paints coarse first so
the finest wins (`mosaic_order`), writes tiled DEFLATE + internal AVERAGE
overviews 2–32 (nodata kept; Cancel removes the file) and logs tiles, MB and
seconds. The Altitude Explorer's call is unchanged (16,384 px, no overviews).
`tests/test_abt_inspect.py`.

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
pre-built `.abt` folder goes through `select_abt_tiles` (geometry in
`select_abt_headers`) into an `abt_selection_view`. Two passes (2026-10-23):
1 — every footprint holding a tile of exactly the finest step it needs takes
it; 2 — only the NEEDED ground counts (round 6): footprint ∩ sector bbox,
within the outermost ring, cut into cells by the edges of the footprints
overlapping it and sampled at each cell's point nearest the TX (exact: the
footprints over a cell do not change inside it, steps never get finer with
distance). A point's *limit* is its ring's step where the folder holds that
step or finer there, else the finest it has there (a true gap); a point no
chosen tile ≤ its limit covers is open and belongs to every footprint there
whose finest tile is ≤ its limit. A footprint with no open point is left out
silently; else — best match first (smallest resolution ratio, smaller
footprint), re-ranked as choices serve shared points — it takes its coarsest
tile ≤ the finest limit of its open points. So where the folder has the step
over the whole needed ground: exactly that step's tiles, no coarser, no
finer. Coarser only for a true gap → `substitutions` (pre-run Yes/No dialog
"Terrain resolution not available", logged as `terrain substitution`);
finer only where nothing else serves → `finer` (logged only). Round 6, the
owner's log (2/5/10/30/90 m folder, 2,463 tiles; fixed 10 m / 50 km at
46.5090 N 7.0360 E): judged over the WHOLE footprint at 5×5 samples it took
2 m × 1, 30 m × 4, 90 m × 2 beside 10 m × 23, six with a false "needs 10 m,
only 30 m / 90 m" (samples outside the sector or past 50 km), and the 2 m
tile set the engine's grid (VRAM 2.8 instead of 1.4 GB); now 10 m × 23. At
150 km (46.7284 N 8.8965 E) 10 m × 157 + the two 1° 90 m tiles south of the
folder's 10 m grid (45.5 N; the sector reaches 45.38 N) — a true gap, warned
(was 2/30/90 m × 6/14/11, 25 substitutions). Select on in-memory headers
(container, min of runs; 50 km / 150 km / the 31.5/126/500 km profile):
12 / 53 / 115 ms → 8 / 34 / 64–72 ms; random gapped folders: no hole, nothing
coarser than its limit (the 5×5 rule left a few). `uncovered` = 0.25° cells the
folder has data for but no chosen tile covers (expected empty) → the
coverage warning, NODATA wording: a pre-built folder is never filled, missing
ground is NODATA to the engine, not 0 m. The old single pass dropped a
footprint whenever others merely HELD its step (owner's 10/30/90 m run: 44
cells NODATA, silent); "smaller footprint first" alone would take 330 2 m
tiles (19.8 GiB) for that run's 30/90 m rings. Headers come from ONE header
index per folder (`core/abt_index.py`: name → size, mtime_ns, header fields;
`<cache>/index/abt-<md5(folder)>.json` for ≥ 64 tiles; one `scandir` + one stat per tile,
only new/changed tiles opened, reused for `FRESH_S` while the folder's mtime
holds; `list_abt_tiles` / `is_abt_tile_dir` use its listing). A selection is
memoised 60 s per (folder, listing signature, TX, bands, az): Site Analysis
`_on_run` makes it in the coverage check's worker thread
(`_terrain_dir_checks`; `is_abt_tile_dir` once on the GUI thread, the run's
index look expired first), the coverage warning and the worker's view reuse
it. `terrain_plan` / `terrain_plan_profile` / `terrain_size_warning(source=)`
price a pre-built folder as nothing to build (log `terrain plan: pre-built
folder — nothing to build`). Synthetic 2,463-tile folder on the container
mount (two runs): select 25.9–40.7 s → 1.7–2.4 s cold, 0.5–0.7 s next run,
0.01 s within a run; `is_abt_tile_dir` 5.9–10.2 s → ≤ 0.05 s. Pinned by
`tests/test_abt_selection.py`.

**Buildings per band.** `rp.band_buildings(bands, overrides, master)`: Adaptive
gives buildings to bands with step < `BUILDINGS_SKIP_STEP_M` (90) only; Custom
keeps per-band choices keyed by step (`_custom_buildings`, session-only like
`_custom_bands`; missing steps follow the same rule); "Include buildings" is the
master switch. Flags ride in `CoverageParams.range_profile_buildings` (never in the
job JSON) to `prepare_terrain_profile(band_buildings=...)`, which builds each band
into its own pool (with / without buildings — a band with buildings through the
strict path of `prepare_terrain`, see Buildings on ingest jobs) and links both
into one view whose key encodes which steps carry buildings;
`terrain_plan_profile` prices the same.
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
- Windows-only behaviour is gated on `sys.platform == "win32"`
  (`terrain_adapter._is_windows`; tests flip it); engine launches pass
  `_SUBPROCESS_FLAGS` (CREATE_NO_WINDOW). binary_manager keeps `platform.system()`.
- Python 3.8 still runs the plugin (QGIS 3.28 on Ubuntu 20.04): every module
  with builtin-generic or `X | Y` annotations carries
  `from __future__ import annotations`; MD5 fingerprints go through
  `core/hashes.md5()` (3.9's `usedforsecurity=False` where Python knows it,
  the plain call on 3.8 — never `hashlib.md5(…, usedforsecurity=False)`
  directly); `Executor.shutdown(cancel_futures=True)` is wrapped in
  `try/except TypeError` with a `wait=True` fallback. No `removeprefix`,
  `math.lcm`, `zoneinfo`, `functools.cache`.
- Registry-scan hygiene (plugins.qgis.org blocks a version on ANY Bandit or
  detect-secrets finding, see Build / Package / Release): best-effort code
  uses `with contextlib.suppress(Exception):  # reason`, never
  `except Exception: pass` (B110/B112); every `subprocess` launch carries
  `# nosec B603` (+ `B607` for a bare program name) with a short reason on
  the line Bandit reports (the statement's first line); `urlopen` sites
  `# nosec B310`; a constant `ExecuteSQL` `# nosec B608`; user-facing
  messages avoid "Select … from" wording (B608 false positive); no literal
  that looks like a key — the Base58 alphabet is built from `string`
  constants, and the public manifest key line ends in
  `# pragma: allowlist secret`, which the AETHER release GUI's key rewrite
  emits (`python/pipeline/core.py` `_KEY_LINE_PRAGMA`). Never ship `.bandit`
  or `.secrets.baseline`. Check with `python3 tools/qgis_scan.py`.
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
  and before every `--version` probe of a binary in the engine dir
  (`discover_binary_dir`; a binary found elsewhere on PATH is probed
  untouched); a Gatekeeper-looking failure gets the
  `xattr -cr "<dir>"` hint (`macos_launch_hint`). Every binary_manager launch
  passes CREATE_NO_WINDOW on Windows and closes stdin (no console flash).
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

Plugin is distributed as a ZIP (build with `python3 package.py [OUT_DIR]` →
`dist/waveshed.<version>.zip`; `--release` additionally requires public
metadata URLs and a trusted manifest key; unknown flags exit 2 with usage —
`package.py --help` once built a ZIP into a directory named `--help`, which
was tracked in git until 2026-11-08).

**waveshed.io links (preview gate lifted 2026-11-08).** `/downloads` and
`/get-key` are public; the site ignores the old `?prv` flag, so older plugin
releases keep working. `core/site_links.py` is the only place that builds
such URLs. `PREVIEW_QUERY = ""`: `site_page_url()` (static text) and
`page_url()` (buttons) return plain URLs, and `page_url()` makes no network
call. The once-per-session `/get-key` probe (2 s timeout) stays dormant for
a future gate and only runs while `PREVIEW_QUERY` is set.
`tests/test_site_links.py` pins the plain URLs, the no-probe path, the
dormant probe (with a preview query monkeypatched in, `_open` seam) and that
no gated URL is hardcoded elsewhere.

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
builds the ZIP and predicts the registry's scan (`security_scanner.py`,
`tasks/run_security_scan.py`, `models.py` of qgis/QGIS-Plugins-Website): five
CHECKS, each passed only with ZERO findings — Bandit (critical; the 72
enabled rules via `-t`, no `-ll`, so LOW counts; `# nosec` honoured),
Secrets Detection (critical; metadata.txt excluded), Flake8 (info; 26 codes,
no `--max-complexity`), File Permissions (info), Suspicious Files (warning).
Any Bandit or detect-secrets finding BLOCKS the version (exit 1) — a blocked
version cannot be unblocked, the next upload needs a higher version; pass
rate = passed checks / 5. The per-rule severity table does NOT grade (the
old tool's "0 Critical / PASS" for 0.4.0 was blocked: Bandit 60, Secrets 2,
Flake8 2 × F811, 40.0 %; `tests/test_qgis_scan.py`). The analysers live in a venv in the gitignored `tmp/`
(setup in the script's docstring), never as plugin dependencies. Names that
merely contain `token`/`password`/`secret` trip Bandit B105 even for
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
