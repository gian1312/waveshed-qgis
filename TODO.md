# Waveshed — TODO

Running list of follow-up work. Keep items short; link to the code site.

## High priority

- [ ] **Deduplicate the XYZ tile-mosaic assembly — the WASM copy still carries
  the 2026-08-26 download defects.** The tiles→`.abt` assembly exists three
  times in aether-tools: `run_download_async` and `run_download_mem` in
  `crates/aether_converter/src/download.rs` (both fixed: tile size read from
  the PNG, missing tiles → `-9999` VOID), and
  `crates/aether_converter_wasm/src/lib.rs:45 assemble_terrain`, a separate
  browser entry for Web-Worker-decoded tiles that was NOT fixed: hardcoded
  256 px stride (folds 512 px @2x tiles into terrain hundreds of metres
  wrong), zero-fill for missing tiles (fake sea level marked real), and
  pre-contract-v2.0 point sampling instead of area-averaging. The waveshed.io
  browser pipeline produces exactly the terrain the torture suite just caught
  in the plugin. Fix by collapsing all three onto one shared assembly (the
  fixed `run_download_mem` machinery is the obvious core), then delete
  `assemble_terrain`'s inline copy. Needs a wasm32 toolchain to build/test
  (`wasm-pack build --release --target web crates/aether_converter_wasm`);
  not installable in the dev container — do it where the WASM crates build.
  Related smaller holes found in the same review, fix while there:
  `run_download_mem` never applies the >50%-lost fatal rule
  (`fetch_failure_is_fatal`), and `decode_png` decodes palette/indexed PNGs
  as garbage instead of refusing them.

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
- [x] **Add `changelog=` to `waveshed/metadata.txt`** — done 2026-08-03, with a
  0.2.0 entry covering the tile pool, the buildings pool, the SIMPLE_LOSS
  removal (including its Processing-index break), attribution, cache clearing
  and the two silent-cache correctness fixes. Version bumped 0.1.0 → 0.2.0.
- [x] **Make the GPL "or later" grant explicit.** Done 2026-08-03: `LICENSE`
  now opens with the standard copyright + "either version 2 …, or (at your
  option) any later version" notice, the warranty disclaimer, and a statement
  that the Aether binaries are not part of the program, ahead of the verbatim
  GPLv2 text. The grant no longer lives only in `README.md`/`metadata.txt`.
- [x] **Make `package.py` exclusions explicit.** Done 2026-08-03. `SKIP_DIRS`
  now names `.venv`/`venv`/`tmp`/`tests`/`dist` outright instead of relying on
  the walk starting at `waveshed/`, and a new `SKIP_GLOBS` excludes generated
  `*_rc.py` and `deploy.local.ini` — the one case that really could appear
  *inside* the package. Three tests pin it; `python3 package.py` builds a clean
  182 KiB zip. (Nothing imports a `*_rc.py` today, and none exists.)

## UI / UX

- [ ] **Fix the layer legend.** Reported 2026-08-05. The legend QGIS shows for
  a loaded result does not read correctly — capture what it currently shows
  versus what it should (band values vs dB, ramp stops, units, the
  MIN_ALT/LOS/loss variants) before changing `core/result_loader.py`, which
  builds all three renderers.

- [ ] **Allow setting a minimum dB value.** Reported 2026-08-05. The web app
  already has this ("Min dB threshold (RF mode)"); the plugin has no
  equivalent, so a loss result is rendered across its whole range and weak
  signal dominates the ramp. Needs a control plus a floor applied when the
  colour ramp is built (`core/result_loader.py`) — and a decision on whether
  the floor is display-only or also passed to the engine.

- [ ] **Help system.** No Help action or bundled help exists, and only ~19
  widgets have tooltips. Deferred until the rest of the release work is done.

- [x] **P2P had no asset or azimuth controls at all.** Fixed 2026-08-05. The
  original report ("we cannot select the assets and azimuths in propagation
  loss mode") was first read as the 360° table's collapsed columns; that was a
  real and separate bug, but the P2P tab genuinely had **no such controls** —
  zero hits for `asset` or `azimuth` in the whole file. `P2PParams` already
  carried `az_pattern`, `el_pattern` and `az_rotation`, so only the UI was
  missing: a link could not use a stored emitter and every frequency/ERP had
  to be retyped. Added an Asset picker (fills frequency and ERP from the
  chosen emitter, blank leaves hand-entered values alone) and an AZ Rotation
  control, both inside the existing Propagation-Loss-only params group.

- [x] **Pre-built `.abt` tiles could not be used**, even for tiles the Map
  Converter had just produced ("it doesn't seem to check .abt tiles, only for
  gtiff etc.", 2026-08-04). Fixed 2026-08-05 by **detection** rather than a
  second control: a terrain directory holding `.abt` files is recognised for
  what it is and handed to the engine untouched.
  * `list_abt_tiles` / `is_abt_tile_dir` — deliberately **non-recursive**: the
    engine resolves terrain with a plain `read_dir` over the one directory it
    is given, so tiles in sub-folders are invisible to it, and reporting them
    would promise coverage the run would not have.
  * `prepare_terrain` returns the directory immediately. Everything past that
    point exists to *produce* `.abt` tiles, and the converter would reject a
    `.abt` as its `base_tif` anyway.
  * `terrain_coverage_warning` checks `.abt` sets by their **own headers**.
    GDAL cannot open a `.abt`, so the mosaic path reported a folder of
    perfectly good tiles as unreadable terrain — which is very likely what
    "it doesn't seem to check .abt tiles" actually was. Now warns on no
    overlap, partial coverage (same 98% tolerance as rasters), and tiles whose
    headers cannot be read.
  * The "no terrain files found" message now names `.abt` and the
    no-sub-folders rule, since pointing at Map Converter output one level up
    is the obvious mistake.

  **Suitability is now checked, not assumed** (2026-08-05, after a real run
  used 55 pre-built tiles for a 5 m / 50 km analysis without checking
  anything). Pre-built tiles are finished output: the run cannot change their
  resolution and cannot add buildings to them, so both mismatches have to be
  raised or the result is confidently wrong.
  * **Resolution.** Warns when the finest available tile is more than 1.25x
    coarser than the requested resolution — 30 m tiles must not silently serve
    a 5 m analysis, because the run upsamples them and presents the result at
    5 m, which reads as detail that was never measured. The tolerance absorbs
    the exact-resolution rounding a tile carries (29.99757 m for a nominal
    30 m tile). Finer-than-requested is fine: downsampling is honest.
  * **Buildings.** Requesting buildings against pre-built tiles is now a
    confirmation dialog, not a log line — heights are baked in when the tile
    is built, so the checkbox cannot be honoured and silently ignoring it was
    the failure this exists to prevent.
  * Both run in `prepare_terrain` too, logged as `WARNING`, because a
    Processing run never sees the dialog and this path skips every other
    safeguard in that function.

- [x] **"Include buildings" was unusably slow.** Fixed 2026-08-04 — this was
  the plugin half of the engine change, and it is what actually delivers the
  speed-up. The engine gained `buildings_pbf_dir` on `DownloadJob`; the plugin
  was still diverting every buildings run onto `_export_via_qgis` →
  `writeRaster` regardless, so nothing changed for the user even after the new
  binary shipped.

  `prepare_terrain` now passes `pbf_dir` straight to `_try_rust_download`, and
  the divert survives **only** for a FlatGeobuf `buildings_file` — an IngestJob
  field with no downloader equivalent. OpenFreeMap runs take the fast path.

  **Guarded against a silent wrong answer.** `buildings_pbf_dir` is additive,
  so an engine predating it parses the job, ignores the field and writes
  perfectly good building-*free* terrain — which would then be pooled under an
  identity claiming buildings and reused forever, exactly the failure mode
  already fixed once for failed OFM downloads.

  The gate is `_binary_has_buildings_support`: the `[Buildings]` marker is a
  literal in the executable, so its presence is decided **by inspecting the
  binary before anything runs**. An unreadable binary is assumed to support it,
  because a false negative costs the 7-17x slow path.

  **This replaced a first attempt that scraped the converter's output for the
  `[Buildings]` line, and that attempt was wrong** — it fired against an engine
  that provably has the feature (2026-08-05: the distributed
  `bin/windows/aether_converter.exe` contains the marker, and running
  `bin/linux/aether_converter` with a `buildings_pbf_dir` prints
  `[Buildings] 0 pbf tile(s), no buildings in extent`), sending a real run down
  the slow path and reporting "engine too old" about a current engine. Two
  lessons kept in the tests: gate on what is decidable, not on log text; and
  note that `[Buildings]` prints *before* `[Stats]`, so it is easy to miss in a
  tail-pasted log. Covered by `TestBuildingsOnTheFastPath`, including a
  regression test for the false negative.

---

## Verification backlog — run these against a real QGIS

Nothing below is known broken; these are the checks that have not been done
because there is no QGIS/GDAL in the dev container. Grouped so a session can
take one group at a time. Items marked **(regression)** guard a bug that was
real once.

### V1. Buildings, end to end — the one that matters
- [ ] Buildings run takes the **fast path**: log shows
  `buildings fused during download`, then a `[Buildings] N pbf tile(s) → M
  building(s) → X/Y abt tile(s)` line, and **no** `writeRaster` at all.
- [ ] Timing is the ~22 s class at 5 m / 30 km, not the ~151 s class.
  **(regression** — the 7-17x divert.**)**
- [ ] Buildings are visibly present in the coverage raster.
- [ ] Re-running the same area reuses the pool and re-downloads nothing.
- [ ] A 50 km buildings run completes at all (was ~1687 s, ~1560 s of it in
  `writeRaster`).
- [ ] Buildings still work through the **ingest** path — force it with a
  FlatGeobuf `buildings_file`, which has no downloader equivalent.
- [ ] **(regression)** A run whose OpenFreeMap download fails leaves the tiles
  flagged, so the *next* run re-fetches instead of silently serving
  building-free terrain from a buildings-keyed pool.

### V2. Pre-built `.abt` tiles
- [ ] Point terrain at a Map Converter output folder → log reads
  `using N pre-built .abt tile(s) — no download or conversion`, and the run
  completes without invoking the converter.
- [ ] The same folder in **P2P**, not just 360°.
- [ ] Tiles one level down → warned that sub-folders are not searched.
- [ ] A folder whose tiles are somewhere else entirely → "do not overlap".
- [ ] A folder covering only part of the area → partial-coverage warning
  (98% tolerance) rather than silent 0 m sea level.
- [ ] A truncated/corrupt `.abt` → reported as unreadable, not treated as
  terrain. **(regression** — GDAL cannot open `.abt`, so these used to be
  reported as "no terrain files found".**)**
- [ ] Buildings checkbox with pre-built tiles → logs the "used as-is" note.

### V3. P2P
- [ ] Asset dropdown lists the emitters from the Assets tab and fills
  frequency + ERP on selection; blank leaves hand-typed values alone.
- [ ] AZ Rotation reaches the job (non-zero only).
- [ ] Fresnel band at 40 km / 100 MHz is ~173 m, shaded at 0.6 F1 with the
  full F1 dotted. **(regression** — was 31.6x too small.**)**
- [ ] Assets tab changes show up in the P2P dropdown without reopening the
  plugin (`_refresh_assets` is written but not yet wired to a signal —
  **known gap**, see below).

### V4. Modes, tabs, window
- [ ] MIN_ALT radio greys out on the P2P tab and a MIN_ALT selection falls
  back to LOS when switching there.
- [ ] Placing a site in 360° clears P2P endpoints and its map markers, and
  vice versa; 360° heights/ranges/assets survive.
- [ ] Propagation Loss shows usable Asset and AZ Rotation columns.
  **(regression** — cell-widget columns collapsed under
  `ResizeToContents`.**)**
- [ ] Geometry persists across close **and** Esc. **(regression** — the save
  lived in a shadowed duplicate `closeEvent`.**)**

### V5. Log hygiene and packaging
- [ ] No `ResourceWarning: unclosed file` after a P2P or converter run.
- [ ] No `setFilters` DeprecationWarning on dialog open. **Unverified by
  design** — the fix depends on how sip binds the flag type on your build.
- [ ] `python3 package.py` output installs cleanly from the QGIS Plugin
  Manager ZIP, and the ZIP contains no `*_rc.py`, `tests/` or `.venv`.
- [ ] Processing algorithms still load, and any saved Processing model has its
  Propagation Model re-picked (the SIMPLE_LOSS removal shifted enum indices).

### V6a. Engine-copy confusion — the plugin reads a directory nothing distributes

Cost most of a debugging session on 2026-08-05, so it is written down rather
than rediscovered. The engine exists in several places, updated by **different**
pipeline actions:

| Path | Updated by | Read by |
|---|---|---|
| `AETHER/bin/<platform>/` | Build | the build itself |
| `AETHER_Web/bin/` | **Distribute** (`targets=("web", …)`) | Waveshed web |
| `MPT_SIGMA/aether/<dir>/` | **Distribute** (`targets=(…, "mpt")`) | MPT_SIGMA |
| `~/.aether/bin/` | **"Deploy engine to ~/.aether/bin (local test)"** | **the QGIS plugin** |

`plan_distribute` copies to the web and MPT targets only and never touches
`~/.aether/bin`, so a build can be correctly built **and** correctly
distributed while the QGIS plugin still runs a months-old engine — with no
signal from either side. Confirmed by inspection: both distribute targets
carried the buildings marker, the plugin's copy did not.

- [ ] **Make the mismatch visible.** Options, cheapest first: have the plugin
  log the engine path + build identity on every run (path is logged now, build
  identity is not); add a version/date line to `Settings → Download` showing
  what is staged versus what the manifest offers; or have the pipeline's
  Distribute action also refresh `~/.aether/bin` when a local deploy is
  already present.
- [ ] **Give the engine a queryable version.** `aether_converter --version`
  would turn every "is this the new binary?" question into one command, and
  would let the plugin's capability check read a version instead of scanning
  the executable for a marker string.

### V6. Known gaps, not yet done
- [ ] **P2P asset list does not refresh** while the dialog is open.
  `_refresh_assets` exists but nothing calls it; wire it to the Assets tab's
  change signal, the same way the 360° tab refreshes its cache.
- [ ] **Map Converter and Site Analysis still cannot run concurrently** — the
  layer-preparation dialog is modal. Accepted for now; the real fix is
  replacing `writeRaster` with a GDAL warp.
- [ ] **Terrain download tested only against Mapzen Terrarium** — AWS Terrain
  Tiles, Mapbox Terrain-RGB and Nextzen are untried (encoding detection,
  `{z}/{x}/{y}` templating, per-service zoom caps, API keys).
- [ ] **Local-DEM border fix unconfirmed**, and `AETHER/Design Documents/
  qgis_plugin/TODO.md` §0.2 records the opposite conclusion — that the cause
  was never identified and `INIT_DEST=0` did **not** fix it. Reconcile the two
  documents on real evidence before trusting either.
- [ ] **Terrain "stripe" gaps** (`…/qgis_plugin/TODO.md` §0.1) — deterministic,
  survives a cache wipe, unrelated to the cache fixes. Next step is the cheap
  one already written down: rerun the failing area at
  `aether/download_connections=64` to confirm or kill the concurrency
  hypothesis.
- [ ] **Antimeridian (lon ≈ ±180) is untested and unhandled** in
  `_compute_subtiles`.
- [ ] Untested code paths: `get_source_resolution_info`, the
  `_try_rust_download` job JSON, and the buildings resolve/convert path.

---

- [x] **Map Converter froze the whole GUI while preparing layers.** Fixed
  2026-08-04. `_MapConverterWorker.__init__` called
  `_resolve_source_on_main_thread` for every layer — and a QThread's
  constructor runs on the **calling** thread, so the entire WMS/XYZ
  `writeRaster` export happened on the GUI thread before `start()` was ever
  reached. The class docstring claimed the worker did this export; it did not.
  Resolution now happens in `_on_run` via `resolve_sources_with_progress`,
  a window-modal `QProgressDialog` with a per-layer label and a working Cancel,
  and the resolved paths are passed into the worker. **Accepted as fixed by the
  user 2026-08-05** ("the freeze is masked now"), with the limitation
  understood: the export is still on the main thread — `QgsRasterLayer` is
  main-thread-only — so the dialog is modal and the plugin still cannot be used
  for anything else while it runs. Running Map Converter and Site Analysis
  *concurrently* therefore remains impossible, and the yesterday's-item-5
  concurrency check is moot. The real fix is dropping `writeRaster` for a GDAL
  warp, as `terrain_adapter._try_gdal_warp` already does; until then this is
  visible and interruptible rather than fast.

- [x] **The drawn extent rectangle never disappeared.** Fixed 2026-08-04: the
  rubber band is reset once the second click commits the rectangle. It was
  deliberately kept (`"Keep the rubber band visible (don't reset it)"`), but
  the extent is shown in the layer entry afterwards, so the band only
  accumulated stale outlines across successive draws.

- [x] **Minimum LOS Altitude was selectable while the P2P tab was open.** Fixed
  2026-08-04: the radio is disabled (with an explanatory tooltip) whenever the
  P2P tab is in front, and a MIN_ALT selection falls back to LOS on switching
  to it. Previously the reverse was implemented — selecting MIN_ALT *disabled
  the P2P tab*, which reads as the tab being broken rather than the mode being
  inapplicable.

- [x] **Sites in 360° and P2P were kept independently.** Fixed 2026-08-04:
  placing a site in either tab clears the other tab's sites, through a new
  `AetherMainDialog.sites_picked_in` and a `clear_sites()` on both tabs. The
  360° rows keep their heights, ranges and assets — only the coordinates are
  dropped — so re-placing a point does not discard the rest of the setup.

- [x] **Asset and AZ Rotation columns were unusable in Propagation Loss mode.**
  Fixed 2026-08-04, and it was not a visibility bug — `set_mode` unhid them
  correctly. Every data column holds a **cell widget**, and
  `QHeaderView.ResizeToContents` sizes from *item data*, which those columns do
  not have, so it collapsed them to near-zero the moment they were shown.
  Columns now have declared widths (`_COL_WIDTHS`) with `Interactive` resize
  and a `_COL_MIN_WIDTH` floor; Location still stretches. The fixed columns
  total ~785 px, which the old 640 px dialog minimum could never show — hence
  "squished" — so the dialog minimum is now 900x600. A test pins that the
  column total stays under the dialog's opening width.

- [x] **The main dialog still did not remember its size** (reported 2026-08-04
  after the first attempt). The first fix added a *second* `closeEvent` to a
  class that already had one at line 224 — Python keeps the last definition, so
  the geometry save was dead code that never ran. Now folded into the existing
  `closeEvent` **and** `reject()`, because Esc closes through `reject()` and
  never reaches `closeEvent` at all. Geometry is also read back with an
  explicit `type=QByteArray`, since only a QByteArray restores and QgsSettings
  returns whatever it stored.

## Correctness — analysis output

- [x] **The Fresnel zone on the P2P profile was 31.6x too small.** Fixed
  2026-08-04, reported from a real run ("7 m at 40 km for 100 MHz seems off" —
  it is: the answer is 173 m). `gui/p2p_tab.py` used
  `17.32 * sqrt(d1*d2/(f_MHz*D))`, but **17.32 is the constant for frequency in
  GHz**; the MHz form is **547.7** (= 17.32 x sqrt(1000)). Every Fresnel band
  ever drawn was understated by sqrt(1000) = 31.6x, which makes obstructed
  paths look clear. The maths moved into a pure `fresnel_radius_m()` with
  `tests/test_p2p_fresnel.py` validating it against `F1 = sqrt(lambda d1 d2/D)`
  derived from *c*, plus an explicit guard on the old wrong value.
- [x] **The profile shaded the full F1 rather than the 0.6 F1 clearance.** Fixed
  in the same change: 0.6 F1 is the criterion that matters (terrain outside it
  costs roughly nothing), so that band is shaded and the full F1 is drawn as a
  dotted outline. Matches the waveshed.io profile chart, which already used
  0.6 F1 — the two products now agree on both the constant and the criterion.

  **waveshed.io checked — not affected.** `web/src/lib/utils/profile-chart.ts:383`
  already uses `547.7 * sqrt(d1*d2/(f*D_km))` with `f` in MHz and shades
  `0.6 * f1`. The defect was plugin-only; the two now agree.

## Housekeeping

- [ ] **The FlatGeobuf conversion cache is never cleaned.**
  `<tmp>/aether_fgb/` accumulates converted `.fgb` files and nothing removes
  them. Entries are now keyed by source fingerprint + declared CRS (needed for
  correctness — see `_fgb_cache_name`), so editing a source or re-declaring its
  CRS leaves the previous conversion behind rather than overwriting it. Bounded
  by how often sources change, but worth an age-based sweep.

- [x] **`CLAUDE.md` is stale on dependencies** — fixed 2026-08-03. The `pynacl`
  exception is gone; the rule now reads "no exceptions" and says why (a QGIS
  plugin cannot install a pip dependency). Confirmed nothing imports `nacl`.
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

- [x] **The `.abt` cache never reuses a tile across sites.** Done, as the
  canonical pool the finding describes.
  **An intermediate attempt keyed the whole directory on its exact tile set;
  that was still wrong and still re-downloaded constantly** — a smaller range
  is a *subset* of an existing tile set, and set equality calls a subset a
  miss. Membership is a per-tile question and is now asked per tile.
  Layout: `<cache>/pool/<md5(schema|source|buildings)>/` holds each tile once,
  named by its own geography, and `<cache>/views/<md5(tile set)>/` holds
  hardlinks (copy fallback) to exactly one run's tiles. The view is not an
  optimisation: `engines/coverage.rs:574-607` builds `relevant_tiles` from the
  whole directory listing with **no bbox filter**, so handing the engine the
  pool would drag every tile ever built into the atlas and the output grid.
  A tile counts as present only if its declared geometry matches its file
  length (`_tile_is_whole`) and it carries no `.rebuild` sidecar — so a run
  killed mid-write, a gapped download, or tiles built without their claimed
  buildings are re-made per tile instead of poisoning a whole directory.

  Two further identity fixes (schema v3), because a tile's pixels depend on
  more than its position:
  * **Source is normalised** (`source_identity`). The pool used to key on the
    raw `dem_layer.source()` URI, whose spelling depends on how the user added
    the layer — parameter order, `zmin`, `interpretation`, percent-encoding.
    Re-adding the same Mapzen endpoint therefore meant a new pool and a full
    re-download of byte-identical data. An XYZ URI now reduces to
    `url template | encoding | zmax`, which is what actually decides the
    pixels. Verified: two spellings of one endpoint share a pool.
  * **Zoom is computed per tile** (`tile_zoom`), from the tile's own latitude,
    and downloads are grouped by zoom. It used to come from the request's
    bbox *centre*, so a tile shared by two runs centred at different latitudes
    was built from different source zooms and its pixels depended on which run
    fetched it first. Now zoom is a pure function of (source, tile,
    resolution), so it needs no term in the key. Note the practical effect is
    cross-run, not within-run: inside one run `zmax` usually pins every tile
    to the same zoom anyway.

  Original finding: `_cache_key`
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
  Done — by the tile pool above, not by rounding the bbox. Nothing hashes a
  bbox any more: the request is resolved to canonical tile names and each is
  looked up individually, so a sub-metre nudge, 30 km vs 31 km, and a narrowed
  sector all reuse what is already pooled. (The view directory is still keyed
  on the tile set, so each run gets its own directory; that key never decides
  what gets *downloaded*.) Original finding:
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
  cap) is still open.
  The **pre-run estimate is now pool-aware** (`terrain_plan`): it prices only
  the tiles genuinely missing and reports what is being reused, instead of
  announcing the whole tile set as a download on every run and training the
  user to click through the warning. The local-terrain coverage check now
  fires on **partial** coverage too, not only on none — a terrain directory is
  the only source once selected (no fall back to the base DEM), so whatever it
  misses is built as 0 m sea level and displays as real coverage. Tolerance is
  98%, since the analysis bbox carries a 1% margin of its own.
  Original finding: nothing in `waveshed/` ever deletes a
  cache directory — no LRU, no TTL, no size cap, and no "Clear cache" button
  (`gui/settings_dialog.py:275-284` is only a path field + Browse). At ~1 GB per
  run with no cross-site reuse, `~/.aether/cache` grows without bound. The only
  size logic is an advisory pre-run warning (`_SIZE_WARN_MB`,
  `_SIZE_STRONG_WARN_MB`, `:416-420`), and `estimate_terrain_disk_mb` has no
  notion of cross-site overlap so it over-estimates.

- [ ] **OpenFreeMap buildings never invalidate.** `_OSM_BUILDINGS_TOKEN =
  "openfreemap:z14"` (`:191-195`) is opaque and date-free, so OFM republishes
  are never picked up — the cache must be cleared by hand.

- [ ] **The cache path only governs `.abt` tiles — every heavy intermediate
  ignores it.** Confirmed on a real run: the pool and views correctly landed
  under a relocated `G:/…/cache_test`, while the per-tile base GeoTIFFs went to
  the system temp directory. On Windows that is `C:\Users\…\AppData\Local\Temp`,
  so a user who deliberately puts the cache on a big drive still fills the
  system drive — 31 MB × 24 tiles for one 50 km / 10 m run, and that scales
  with area. Sites:
  * `core/terrain_adapter.py:1578` — the base GeoTIFF per tile (the big one),
    and `:981` the download job JSON.
  * `gui/map_converter_tab.py:1189, 1793, 1801` — hardcode `~/.aether/cache`
    instead of `get_cache_dir()`, so the Map Converter writes to the old
    location outright (this was the original finding below).
  * `gui/map_converter_tab.py:818-885` — `<temp>/aether_fgb`, the FlatGeobuf
    cache that is also never cleaned.
  * `gui/site_analysis_tab.py:609`, `gui/p2p_tab.py:942` —
    `<temp>/aether_output` as the default output directory;
    `gui/altitude_explorer.py:1556` — `<temp>/aether_tools`.
  Decide what "Terrain cache" means: today it is "where .abt tiles go", but a
  user setting it reasonably expects "where this plugin puts its data".
  Original finding: `gui/map_converter_tab.py:1196,
  1800, 1808` hardcode the `~/.aether/cache` default instead of calling
  `get_cache_dir()` (`core/terrain_adapter.py:52-54`).

- [x] **Restore the disambiguator on the temp GeoTIFF name.** Done 2026-08-03.
  The name is now `aether_{pool_hash}_{pid}_{tile}.tif` — pool identity covers
  a different source or building set writing the same canonical tile name, and
  the pid covers two runs of the *same* source (Site Analysis and Map Converter
  together, or two windows), which the pool hash alone does not. Noted in the
  code that the pid is correct only while this stays scratch: caching the
  extract between runs (the open item below) means dropping the pid *and* the
  `finally`, not just the `finally`. Original finding:
  `core/terrain_adapter.py:1578` built `aether_{tile}.tif` in the shared temp
  directory, having lost the `cache_hash` term when the pool landed. Tile names
  are now globally canonical (position + resolution only), so the same name
  means different pixels for different sources — two runs, or Site Analysis and
  Map Converter together, read and write one another's temp file, and the
  `finally` deletes it out from under whoever is still using it.

## MAJOR — buildings are unusably slow, and the target is 500 km

Reported from real runs: **hours at 30 km**, against a target of 500 km, and
far slower than the same job on waveshed.io. Effectively unusable today. The
individual findings are itemised in the section below; this is the summary and
the decision.

**Where the time goes.** Enabling buildings diverts terrain preparation off the
Rust downloader onto `_export_via_qgis` → `QgsRasterFileWriter::writeRaster`
(`core/terrain_adapter.py:1050-1052` picks the branch, `:930` lands on it).
Measured previously: the same 5 m / 30 km area is 22.3 s on the fast path vs
151.2 s with buildings, 122.1 s of it inside `writeRaster`; at 50 km it is
1687.5 s, ~1560 s of which is four `writeRaster` calls, while the actual Rust
converter work is only 108 s. It scales with area, so 500 km is not a longer
wait — it is out of reach by orders of magnitude.

**Why waveshed.io is fast.** The web app never takes this path. It calls
`mvt::apply_buildings_to_abt_tiles` on the downloader's in-memory `.abt`
buffers (`aether_converter_wasm/src/lib.rs:249`). The plugin cannot, only
because `DownloadJob` has no buildings field — not because the capability is
missing. `run_download_mem` is not wasm-gated and hands out exactly those
buffers at `download.rs:1730-1733`.

**Decision: do not optimise `_export_via_qgis`.** Six of the items below
(`setMaxTileSize`, `setCreateOptions`, GDAL cache config, the `cap = 16384`
resolution loss, the missing progress/cancel, the main-thread affinity bug)
only exist on that path, and the engine-side change deletes the path entirely.
Tuning it means tuning code that is meant to be removed, and even a perfect
version of it is a QGIS provider pull capped at ~6 concurrent requests per host
against the Rust path's 256.

**Do this instead** (engine change, then a small plugin change):
1. Add `buildings_pbf_dir` to `DownloadJob` (`download.rs:43-50`) and call
   `mvt::apply_buildings_to_abt_tiles` on the `MemAbt` buffers after
   `download.rs:1733` — ~20 lines against a library function that already
   exists and is already used by the web build. *Or* expose an
   `apply-buildings` subcommand (`main.rs:64/68/159` has only Convert / Ingest
   / Download), ~10 lines, letting the plugin chain fast-download →
   apply-buildings.
2. Then delete the buildings branch at `core/terrain_adapter.py:1050-1052` so
   buildings runs use the same path as everything else.

**Also required for 500 km, engine-side:** `ingest.rs:504` re-scans and
re-decodes the entire `buildings_pbf_dir` for *every* output tile with no bbox
filter, and each rayon worker holds a full `Vec<Building>` for the whole area —
1369 × 4 = 5476 gunzip + protobuf decodes on the 50 km run alone. This grows
quadratically with range. The plugin now hands the converter a per-run view of
the building-tile pool (`_link_pbf_view`) so the scan is at least bounded by
the run rather than by everything ever downloaded, but the real fix is the
bbox filter in the converter.

**Still capped at 4096 building tiles** (`core/openfreemap.py:47`) — a 100 km
radius is already ~7000 tiles, so 500 km cannot even start. The limit has to go
away by chunking per `.abt` tile (see below); there is no z-level fallback
because the `building` layer only exists at z14.

## Large ranges at high resolution: pick a workable resolution, or refuse

Today nothing reconciles range against resolution, so the user can ask for a
combination that cannot be built or cannot be solved, and only finds out after
a long download — or gets a misleading error from the engine. Needed:

- **A pre-flight feasibility check** run when range/resolution change, not
  after the download: tiles to fetch, disk, and the terrain-atlas allocation
  the solver will need (tile bytes × tiles per wedge against
  `0.9 * min(gpu max_buffer_size, 4 GiB)`). The tile-extent ladder now makes
  the atlas term predictable, so this is computable up front.
- **Varying resolution across the run.** A 500 km study does not need 5 m
  everywhere. Allow a coarser resolution beyond some radius (the engine already
  picks the best-fit tile per grid cell — `engines/coverage.rs:582-607` sorts
  each tile group by |res - requested| — so mixed-resolution `.abt` tiles in one
  directory are already handled). The plugin side is choosing the ladder,
  generating the tiles, and showing the user what they will get.
- **Refuse clearly when it will not work.** If the combination cannot be built
  or solved, say which limit is hit and what to change (reduce range, coarsen
  resolution, narrow the sector) *before* anything is downloaded. Right now the
  only feedback is the solver's hardcoded VRAM message, which names the wrong
  cause — see the solver section.
- **Cap the building-tile count per `.abt` tile** rather than per run, so the
  4096 limit stops being a global range ceiling.

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

- [x] **No shared building-tile cache.** Done. Building tiles now pool next to
  the terrain tiles (`<pool>/_buildings/`) — they are globally addressed
  `z_x_y.pbf`, so 30 km → 31 km reuses all 1369 instead of re-fetching them.
  The converter is handed a per-run *view* of that pool rather than the pool
  itself, because `ingest.rs:504` re-decodes every file in the directory for
  every output tile it writes; a shared directory that grows across runs would
  make each run slower than the last. Original finding: `pbf_dir` lives inside the terrain cache
  (`core/terrain_adapter.py:1034`), which is keyed on bbox — so 30 km -> 31 km
  re-downloads all 1369 z14 tiles.

- [x] **`download_building_tiles` uses eager ordered `pool.map`** — fixed
  2026-08-03: now `submit` + `as_completed`, so results are counted as they
  land instead of in submission order and one slow tile no longer stalls the
  ones behind it for up to `timeout=30.0`. Only the running total is used, so
  ordering was never needed. Worker count unchanged.

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

- [x] **`_extract_reproject` takes a `binary_manager` it never uses** — dead
  parameter removed 2026-08-03, along with the argument at its one call site.
  The Rust downloader it was a placeholder for is reached a different way now.

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

> **Still open in full as of 2026-08-05** — user confirmed the Explorer "still
> has many issues". All 18 findings below stand; none has been worked on. They
> come from one code audit of a single 1902-line file and are best done as
> **one redesign, not 18 tickets** — they overlap heavily (sizing, the missing
> size policies and the `addStretch` are the same layout problem; the O(n²)
> twin sync and the undebounced slider are the same responsiveness problem).
>
> **Write the test net first.** `No test imports altitude_explorer` (last item
> in this section), so a redesign currently breaks nothing detectably. The
> contract to preserve is `band_stops` + `build_band_renderer`, already covered
> by `tests/test_min_alt.py`.
>
> Rough order: (1) the two functional defects — AMSL+AGL together, and
> half-metre altitudes being unreachable through a `QSpinBox` against a 0.5 m
> raster quantum; (2) responsiveness — slider debounce, the O(n²)
> `_sync_twin_visibility`, the `_next_altitude` materialisation; (3) layout and
> sizing; (4) polish — theming, number formatting, mnemonics, message bar.

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

- [x] **The main dialog opens too small and never remembers a size.** Done
  2026-08-03: opens at 980x780 on a first run and restores the user's saved
  geometry after that, via `QgsSettings` key `waveshed/main_dialog_geometry`,
  saved in `closeEvent` (which `WA_DeleteOnClose` makes the last chance to read
  it). The 640x580 minimum stays as a small-screen floor. Original finding:
  `gui/main_dialog.py:39` sets `setMinimumSize(640, 580)` for a QDialog holding
  five dense tabs, and there was no `resize()` call anywhere in the plugin GUI,
  so it always opened at its minimum.

- [x] **Per-tab minimums contradict the dialog's.** ~~Finding withdrawn~~ —
  **not a defect; the premise is wrong.** Checked 2026-08-03: neither cited
  minimum belongs to a tab. `gui/p2p_tab.py:257` `setMinimumSize(800, 550)` is
  on `_P2PResultViewer(QDialog)` and `gui/asset_manager_tab.py:663`
  `(550, 550)` is on `_PatternVisualizerDialog(QDialog)` — both standalone
  pop-up windows that never sit inside the main dialog, so they are free to be
  larger than it. The only cited item that *is* a tab is
  `gui/settings_dialog.py:120` `setMinimumWidth(560)`, and 560 < 640, so it
  does not contradict the dialog either. Nothing to do.

## Propagation models

- [x] **Remove SIMPLE_LOSS as a user-selectable model.** Done 2026-08-03.
  Removed from the GUI combo (`gui/main_dialog.py`), both Processing enums
  (`algorithms/coverage.py` → `["LOS", "ITM", "MIN_ALT"]`, `algorithms/p2p.py`
  → `["LOS", "ITM"]`), every docstring, and the `about=` line in
  `metadata.txt`, which still advertised "Free-Space Path Loss". The
  `gui/p2p_tab.py` fallback default was changed to `"ITM"` — left alone it
  would have made P2P silently run the removed model. `core/result_loader.py`
  is untouched, so results already computed with SIMPLE_LOSS still load.
  On the positional-index hazard: the enum shift is real and unavoidable, but
  the plugin is `version=0.1.0`, `experimental=True` and not yet on
  plugins.qgis.org, so no third-party saved Processing models exist — this was
  the last cheap moment to do it. Recorded in the new `changelog=` and version
  bumped to 0.2.0 so the break is stated rather than silent.

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

- [x] **Fuse buildings in the Rust downloader.** Done engine-side 2026-08-03 in
  the **aether-tools** working checkout (`crates/aether_converter/src/download.rs`).
  `DownloadJob` gained an optional `buildings_pbf_dir` (`#[serde(default)]`, so
  older jobs are unaffected), and a new `apply_buildings_post_pass` fuses the
  buildings once `run_download_async` has finished writing the tiles.

  Two deviations from the plan above, both deliberate:
  * **Post-pass over the written files, not the `MemAbt` buffers.** The plan
    named `download.rs:1733`, which is inside `run_download_mem` — the WASM
    in-memory path the web app uses. The plugin goes through
    `run_download_async`, which *streams* each tile to disk row by row, so
    there is no whole-tile buffer to hand the rasterizer mid-download. The
    post-pass reads each finished tile back, rasterizes, writes it out.
  * **Decode once, rasterize per tile.** Rather than calling
    `mvt::apply_buildings_to_abt_tiles` (which takes every `.abt` at once, so
    peak memory is the whole run), the pass decodes the PBF set once and then
    handles one tile at a time. Cost is `O(pbf + abt)` rather than their
    product, and peak memory is one tile — this is the same trap as
    `ingest.rs:504`, avoided rather than inherited.

  Mixed-zoom PBF directories are **refused**, not merged: `rasterize_buildings`
  resolves above-ground heights against the terrain it reads before it writes,
  so a second pass over a tile would measure new roofs against the first pass's
  roofs and the buildings would grow. OpenFreeMap is z14-only, so one zoom is
  the real-world case anyway.

  Contract updated in the same change, per the repo's additive-only policy:
  `docs/CONTRACT.md` §9b, `schemas/download_job.schema.json`, and
  `schemas/ingest_job.schema.json` — the last of which was **missing
  `buildings_pbf_dir` entirely** even though the code and CONTRACT.md §9 both
  had it. `cargo test --workspace` 81 pass, `validate_fixtures.py` clean.

  **Plugin side landed 2026-08-04**, once the engine was built and distributed
  — see "Include buildings was unusably slow" under UI/UX. The divert now
  applies only to FlatGeobuf, and an engine lacking the feature is detected
  from its own output rather than assumed present.

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

## Found in live QGIS testing, 2026-08-12 (fix later — analysis confirmed)

- [x] **Map Converter XYZ bypasses the toolkit resampler — checkerboard NOT
  fixed on this path.** (fixed 2026-08-12: acquisition router + shared
  `ensure_pool_tiles` downloader; QGIS renders only true rendered servers) The tab renders elevation XYZ through QGIS
  (`map_converter_tab._resolve_source_on_main_thread`): one whole-extent
  GeoTIFF at `min(out_res)`, nearest-neighbour decimation inside QGIS's raster
  pipe, on the MAIN THREAD (UI frozen, progress stuck at 0, no real tile
  download at native zoom). The converter then ingests at ratio ~1 and has
  nothing to average. Fix: route elevation-encoded XYZ through
  `aether_converter download` per zoom group, exactly like Site Analysis's
  `_try_rust_download`. QGIS render stays only for true rendered servers.
- [x] **`_resolve_source_on_main_thread` swallows export failures**
  (`except Exception: pass` → returns the raw source path). Fail loudly.
  (fixed 2026-08-12: hard RuntimeError naming the layer and the cause)
- [x] **`_detect_xyz_resolution` still trusts raw `zmax`** — duplicate of the
  bug `resolve_zmax` fixed; the UI resolution label lies for hand-added layers.
  (fixed 2026-08-12: replaced by `terrain_adapter.xyz_native_resolution_m`,
  resolve_zmax-based)
- [x] **Settings dialog changes don't reach open tabs** (binary dir, cache dir,
  dropdowns) until the plugin dialog is reopened. Tabs read QgsSettings at
  construction only; no refresh signal exists.
  (fixed 2026-08-12: SettingsDialog emits `settings_changed` on save when a
  value actually changed; the main dialog fans out to per-tab
  `refresh_settings()` — DEM local-dir entry, asset dropdowns, asset list,
  Map Converter output default all follow saved settings now, without
  touching user-entered form state.)

## Acquisition unification follow-up (Addendum A4, 2026-08-12 — note only)

- [ ] **Buildings tile download is the plugin's last Python HTTP downloader.**
  Terrain download is Rust (`aether_converter download`), but OpenFreeMap
  building tiles are still fetched by `core/openfreemap.py` (urllib, Python
  thread pool). Candidate: move a generic vector-tile fetcher into the
  toolkit (same job-file + `[Download]`/`[Stats]` progress contract) so the
  plugin sheds its last HTTP downloader and buildings get the same retry /
  progress machinery as terrain. Do not implement ad hoc — needs a toolkit
  contract entry first.
- [ ] **WebP tile decode in the toolkit downloader.** `download.rs` decodes
  PNG only; MapTiler Terrain-RGB **v2** (and some self-hosted stacks) serve
  WebP tiles, which currently must fail loudly. Add WebP decode via a pure-
  Rust decoder (e.g. `image-webp`) behind the same `decode → f32 grid` seam;
  encoding formula handling (terrarium/terrain-rgb) is codec-independent and
  unchanged.
