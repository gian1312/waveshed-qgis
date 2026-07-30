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
