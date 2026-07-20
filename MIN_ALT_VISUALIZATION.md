# Min-Altitude LOS — feature + visualization brainstorm

Two things are covered here:

1. **What was implemented** for the new `MIN_ALT` propagation model.
2. **State-of-the-art QGIS visualization options** for letting a user display
   their preferred altitude across one or many result layers — with a
   recommendation and what shipped.

---

## 1. What `MIN_ALT` is

The AETHER engine already supports a `MIN_ALT` output mode
(`rust/aether_core/src/solver.rs::OutputMode::MinAltLos`). Instead of a 1-bit
visibility mask (`LOS`) or an 8-bit loss map (`SIMPLE_LOSS`/`ITM`), each pixel
holds the **lowest altitude above ground at which that location first gains
line-of-sight to the transmitter**:

- 16-bit GeoTIFF, `value × 0.5 m` AGL (GDAL `SCALE=0.5`, `OFFSET=0`),
- `65535` = never visible / no-data (the GeoTIFF `NODATA` tag),
- computed **independently of the receiver height** — one run answers "what
  altitude do I need here?" for *every* location at once
  (`cpu_coverage.rs:503`: `agl = tx_h + max_slope·dist − ground`).

The single most valuable property: **coverage at any altitude *A* is just
`{pixels where required_altitude ≤ A}`** — a pure re-classification of one
raster, with **no recomputation**. That is what makes an interactive altitude
slider possible.

### Wiring added to the plugin (zero changes to AETHER binaries)

| Area | Change |
|------|--------|
| `core/min_alt.py` *(new)* | Quantization contract (0.5 m step, 65535 sentinel), metre↔raw helpers, colour-ramp math. Pure/unit-tested. |
| `core/result_loader.py` | `MIN_ALT` branch → continuous altitude ramp; `build_min_alt_renderer()` shared with the explorer; `estimate_max_altitude_m()`. Stamps `aether/model` on the layer. |
| `core/layer_utils.py` | `mark_aether_model` / `aether_model` / `is_min_alt_layer`. |
| `algorithms/coverage.py` | `MIN_ALT` added to the Processing `_MODELS`. |
| `gui/main_dialog.py` | Third analysis mode radio **“Min Altitude”**; P2P tab disabled in this mode (min-altitude is coverage-only). |
| `gui/site_analysis_tab.py` | Geometric-mode UI (no assets/AZ); altitude table hidden with an explainer; one job per site; offers to open the explorer when done. |
| `gui/altitude_explorer.py` *(new)* | The interactive dock (see §2). |
| `plugin.py` | Toolbar/menu action + dock lifecycle. |
| `tests/test_min_alt.py` *(new)* | Quantization, ramp, job-config, model-tagging tests. |

> **⚠️ One assumption to verify in a live QGIS.** All ramp/threshold values are
> handed to the renderer in **raw u16 units** (`metres ÷ 0.5`), because QGIS'
> pseudocolour renderer classifies on the *unscaled* band value — GDAL
> `SCALE`/`OFFSET` are metadata only. If your QGIS build turns out to apply the
> scale (i.e. the existing `SIMPLE_LOSS`/`ITM` dBm ramp renders correctly on the
> 8-bit `OFFSET=-150` output), flip the two conversions in
> `core/min_alt.py::altitude_to_raw` / `raw_to_altitude` to identity — that is
> the single point of change. This is called out because the existing loss ramp
> uses the *physical* (dBm) domain, i.e. the opposite assumption; only one of the
> two can be right on a given QGIS build, so it is worth a 30-second visual check.

---

## 2. Visualization: displaying a *preferred altitude*

Goal: the user picks an altitude and instantly sees where they can reach the
transmitter — on one layer or several (e.g. multiple sites) at once.

### Option A — Interactive altitude slider (dock panel) ✅ **shipped**

A `QgsDockWidget` (“AETHER Altitude Explorer”) with a live slider. Dragging it
re-classifies every selected `MIN_ALT` layer to show the area reachable at ≤ the
chosen altitude. Two colour modes: *shade by required altitude* (blue = low/easy,
red = must climb) or *flat coverage mask*.

- **Why it wins:** the single killer capability of `MIN_ALT` is "any altitude,
  instantly, no recompute". A slider is the most direct expression of that. It
  is the native QGIS idiom for persistent interactive controls (cf. the Layer
  Styling panel), it drives **multiple layers at once**, and it needs no extra
  data — just renderer swaps, which are effectively free.
- **Cost:** ~one self-contained module. Shipped.
- **Trade-off vs a tab:** a dock stays open beside the map while you pan/zoom; a
  modal-ish tab inside the analysis dialog would cover the map or force
  round-trips. Hence dock over "view tab".

### Option B — QGIS **Temporal Controller** as an altitude scrubber

Repurpose the Temporal Controller's animation slider as an *altitude* axis:
build a paletted/VRT stack and step through altitude bands, exporting frames to
an animated GIF/MP4 "coverage grows as you climb" flythrough.

- **Great for:** briefings, reports, sharing a single artefact.
- **Downside:** hijacking a time axis for altitude is a hack (labels say
  "time"), and it needs a frame per band. Best as a *complement* to A for export.

### Option C — Graduated / continuous pseudocolour (static default) ✅ **shipped as the default style**

The layer loads with a colour-blind-safe RdYlBu-reversed ramp over required
altitude. This is the at-a-glance "how hard is each spot" map before you touch a
slider. Pair with a **hillshaded DEM underneath at ~50 % layer opacity** (or QGIS
blending mode *Multiply*) for terrain context — the standard cartographic move.

### Option D — Iso-altitude **contours** ("reach lines") ✅ **shipped**

Draws labelled lines of equal required altitude (e.g. 25 m, 50 m, 100 m) via
`gdal.ContourGenerateEx` on the `MIN_ALT` raster (honouring the 0.5 m scale and
the 65535 sentinel). Output is a GeoPackage line layer with an `alt_m`
attribute, styled graduated + labelled in metres — "fly above N m to clear this
ridge" at a glance. Exposed as the **Iso-altitude contours…** button in the
explorer (pick the interval); runs per selected layer.

### Option E — Multi-site "**best altitude / best site**" merge ✅ **shipped**

*"Across all my transmitters, what is the lowest altitude I need here, and which
site serves it?"* The **Merge selected → best site** button warps the ticked
MIN_ALT layers onto a shared grid (nearest-neighbour, to preserve the sentinel
and 0.5 m quantisation) and reduces them pixel-by-pixel:

- **Best required altitude** — per-pixel *minimum* required altitude; a MIN_ALT
  layer itself, so the **same slider drives it**.
- **Best site** — per-pixel *argmin* (which transmitter), a categorical layer
  with one colour per site.

The reduction streams one warped raster at a time (peak ≈ 3 arrays, not N) and
the core math (`raster_tools.fold_best_site` / `reduce_best_site`) is
GDAL-free and unit-tested.

### Option F — 2.5D / 3D

Load the `MIN_ALT` raster as an elevation-styled layer in the **QGIS 3D Map
View**, or drape the reachable-area mask over the DEM. Compelling for stakeholder
demos; heavier and less precise for day-to-day planning. Nice-to-have.

### Recommendation

**A (interactive dock) + C (continuous default)** exploit what makes `MIN_ALT`
special and cover the one-or-many-layers requirement with zero recompute; **D
(contours)** and **E (best-site merge)** turn a visual union into a quantitative
one and a print-ready deliverable. **All of A, C, D, E are implemented.** **B
(animated altitude flythrough)** and **F (3D drape)** remain presentation extras.

---

## 3. How to use it

1. Open **AETHER Analysis**, pick the **Min Altitude** mode.
2. Add one or more sites, pick a DEM, **Run Analysis**.
3. Accept the prompt to open the **Altitude Explorer** (or use the toolbar
   button any time).
4. Tick the layers to drive, drag the slider to your preferred altitude, and the
   reachable area updates live. *Reset to full range* restores the continuous
   ramp.

Both new tools live in the explorer's **Tools** group and act on the ticked
layers: **Merge selected → best site** and **Iso-altitude contours…**. Outputs
land under the `AETHER › Best Site` and `AETHER › Contours` layer groups.

## 4. Follow-ups (not yet built)

- [ ] **Animated altitude flythrough** export (Option B, Temporal Controller).
- [ ] **3D drape** of the reachable mask over the DEM (Option F).
- [ ] Thread the best-site merge for very large multi-site jobs (currently a
      synchronous busy-cursor op).
- [ ] Verify the raw-vs-scaled rendering assumption on a target QGIS build
      (see the warning in §1) and reconcile the `SIMPLE_LOSS`/`ITM` ramp domain
      with it.
