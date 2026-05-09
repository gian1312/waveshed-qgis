# Open Questions — Map Converter Tab

All questions resolved. Decisions documented here for reference.

## Architecture

**Q1. Layer priority**: Two-tier converter model (base + overlays).
Lowest-priority layer = `base_tif`, all higher = overlay array
(first-match-wins in converter). Works globally — not Swiss-specific.
**Implemented.**

**Q2. Folder expansion**: Folders expanded to individual file paths.
Converter's overlay field only accepts files, not directories.
**Implemented.**

**Q3. CRS reprojection**: Plugin reprojects non-WGS84 layers to WGS84
via `gdal.Warp` before passing to converter. Converter has no
reprojection capability. **Implemented.**

**Q4. XYZ/WMS layers**: Not a question — the converter produces .abt
directly, which is all we need. For XYZ layers in the Map Converter,
we export to temp GeoTIFF via QGIS, then feed to converter. The Rust
XYZ downloader (terrain_adapter Path 1) could be used as an optimisation
but is not needed for correctness. **Current approach works.**

## Data & Formats

**Q5. bc6h**: Removed from UI. Only r16sint for now. bc6h can be added
back later if needed. **Implemented.**

**Q6. Mixed format**: Moot — only r16sint available. **N/A.**

**Q7. Tile naming**: Matches `prepare_data.py` convention:
`Tile_N{lat}E{lon}_{res}m_r16sint.abt`. aether_core finds tiles by
`.abt` extension + binary header, not by filename. **No issue.**

**Q8. Sub-tile sizes**: Sub-tile degree controls how large each .abt
file is on disk — smaller sub-tiles = smaller files = less RAM needed
during conversion. Not related to output resolution. Current mapping:
2m→0.1°, 5m→0.25°, 10m→0.25°, 30m→0.5°, 90m→1.0°. Keeps all files
under ~60 MB. **No change needed.**

## UX

**Q9. Extent snapping**: Bbox snapped to finest sub-tile grid boundary.
**Implemented.**

**Q10. Existing tiles**: Count shown in estimate label. Tiles skipped
during conversion. **Implemented.**

**Q11. Progress bar**: Converter prints `[Rust] Progress: N/M` every
10 tiles. Worker parses this and drives the progress bar.
**Implemented.**

**Q12. Cancel + resume**: Already works — completed tiles remain,
skipped on next run. **Already working.**

**Q13. Building 3D validation**: WKB Z check with user warning.
**Implemented.**

## Testing

**Q14-Q15**: Tests use `conftest.py` following MPT_SIGMA pattern —
installs QGIS/PyQt stubs into `sys.modules` before any plugin import.
51 unit tests pass without QGIS runtime. **Implemented.**
