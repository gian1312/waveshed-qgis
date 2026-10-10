"""The Waveshed help text — one HTML fragment per topic.

Plain data: no Qt, no QGIS, and no imports from the rest of the plugin except
the stdlib-only ``core.site_links`` (page URLs), so the whole document can be
checked by the test suite. Every control is quoted with
the exact label the code gives it, so a reader can find it in the dialog.

Keep this in step with the code. The tests in ``tests/test_help_content.py``
pin the parts that are derived from constants (``VALID_RESOLUTIONS``,
``ABT_EXTENT_DEG``, the sites-table columns, the ITM parameter labels), so a
rename that is not reflected here fails the suite rather than quietly leaving
the help wrong.
"""

from __future__ import annotations

from typing import List, NamedTuple, Tuple

# The one pure-stdlib module this file uses: page links carry the site's
# preview flag, and that must stay in a single place (core/site_links.py).
from ..core.site_links import GET_KEY_URL


class Topic(NamedTuple):
    """One help section."""

    #: Stable anchor name, also what ``show_help(anchor)`` takes.
    anchor: str
    #: Title, shown in the topic list and as the section heading.
    title: str
    #: HTML body fragment (no heading — :func:`help.section_html` adds it).
    body: str
    #: Extra words the search box should match on top of title + body.
    keywords: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

_OVERVIEW = """
<p><b>Waveshed</b> is a QGIS plugin for radio-frequency propagation analysis.
The plugin itself is pure Python: it prepares terrain, writes a job
configuration, runs the <b>Aether engine</b> (three external binaries) and
loads the result back into QGIS as styled layers.</p>

<h3>What it computes</h3>
<ul>
<li><b>360&deg; site analysis</b> — an area map around a transmitter: pure
geometric line of sight, received signal strength (ITM), or the
<b>LOS Floor</b> surface (the lowest altitude at which every location first
sees the transmitter).</li>
<li><b>Point-to-point links</b> — one link, or a whole batch of them from a
CSV, with signal strength, path loss and terrain profiles.</li>
<li><b>Terrain conversion</b> — the Map Converter turns a stack of rasters
(plus buildings) into <code>.abt</code> tiles the engine reads.</li>
</ul>

<h3>Where the plugin lives</h3>
<ul>
<li>Menu <b>Plugins &rarr; Waveshed</b>, with <b>Waveshed Analysis</b>
(the main dialog), <b>Altitude Explorer</b> (a dock for LOS Floor results) and
<b>Waveshed Help</b> (this text). The first two also sit on the
<b>Waveshed</b> toolbar.</li>
<li>The main dialog has an <b>Analysis Mode:</b> row at the top
(<b>Line of Sight (LOS)</b>, <b>Propagation Loss</b>, <b>LOS Floor</b>) and the
tabs <b>360&deg;</b>, <b>P2P Link</b>, <b>Assets</b>, <b>Map Converter</b>,
<b>Settings</b> and <b>Help</b>. The mode row applies to both analysis
tabs.</li>
<li>The Processing toolbox carries a <b>Waveshed</b> provider with two
algorithms — see <a href="#processing">Processing algorithms</a>.</li>
</ul>

<h3>A first run, start to finish</h3>
<ol>
<li><b>Install the engine and a key</b> — <b>Settings</b> tab, <b>Download
Binaries</b>, then paste your API key and press <b>Save</b>
(<a href="#install">details</a>).</li>
<li><b>Load an elevation source</b> — an XYZ elevation tile service, a DEM
file or folder, or a rendered elevation service
(<a href="#terrain">details</a>).</li>
<li><b>Open Waveshed Analysis</b>, pick a mode, go to the <b>360&deg;</b> tab
and press <b>Pick</b> on the site row to place the transmitter on the map.</li>
<li>Set <b>Height (m)</b> / <b>Height Mode</b>, <b>Range (km)</b>,
<b>Resolution (m)</b> and (for Propagation Loss) an <b>Asset</b>.</li>
<li>Press <b>Run Analysis</b>. Terrain is downloaded or built first, then
<code>aether_core</code> runs, then <code>aether_export</code> writes the
GeoTIFF and the layer appears under <b>Waveshed &rarr; Coverage</b>.</li>
</ol>

<h3>Watching a run</h3>
<p>Progress is shown in the tab, and every line the engine prints is copied to
the QGIS <b>Log Messages</b> panel: tag <b>Waveshed</b> for the engine and the
plugin, <b>Waveshed-Terrain</b> for terrain acquisition (cache hits, tile
counts, download statistics, warnings). When a run behaves oddly, that panel is
the first place to look.</p>

<h3>What the plugin never does</h3>
<p>All computation happens in the engine binaries. The plugin never warps a
raster itself, never bundles the engine, and never sends your data anywhere —
the only network traffic it starts is the engine download, the release
manifest, elevation tiles from the service you selected, OpenFreeMap building
tiles when you tick <b>Include buildings (OpenFreeMap)</b>, and the OSM
Overpass API when you use the Map Converter's <b>Download Buildings...</b>.</p>
"""

# ---------------------------------------------------------------------------
# Engine install / licensing
# ---------------------------------------------------------------------------

_INSTALL = """
<p>The Aether engine is <b>separate, proprietary software</b> under its own
EULA. It is not bundled with this GPL-licensed plugin; you download it on
demand, and it is the only part that needs a licence key.</p>

<h3>The three binaries</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Binary</th><th>What it does</th><th>Licensed</th></tr>
<tr><td><code>aether_core</code></td><td>Runs the propagation job (coverage,
P2P, batch P2P) from a job JSON.</td><td>Yes — needs an API key</td></tr>
<tr><td><code>aether_converter</code></td><td><code>download</code> XYZ
elevation tiles, <code>ingest</code> rasters into <code>.abt</code> terrain
tiles, <code>plan</code> the tile grid.</td><td>No</td></tr>
<tr><td><code>aether_export</code></td><td>Turns the engine's
<code>.bit</code>/<code>.tiles</code> output into a Cloud-Optimized
GeoTIFF.</td><td>No</td></tr>
</table>
<p>All three must be present in one directory — the plugin refuses a run with
<b>Aether engine not found</b> when any is missing.</p>

<h3>Installing</h3>
<ol>
<li>Open the <b>Settings</b> tab and press <b>Download Binaries</b>. On a QGIS
start without an engine, a notice in the message bar has an <b>Open
Settings</b> button that takes you there; nothing is ever downloaded without
this step.</li>
<li>The plugin fetches the release manifest from
<a href="https://waveshed.io/releases/latest.json">waveshed.io/releases/latest.json</a>
and checks its <b>signature</b> against the Waveshed release key built into
the plugin. A manifest with a bad or unknown signature is refused; only
releases up to engine 0.4.7, published before signing, may come unsigned.
It then picks the asset for your platform (archives are accepted only from
<code>releases.waveshed.io</code>) and shows the <b>Download Aether engine</b>
consent dialog naming the version. It shows a highlighted <b>Key points</b>
summary (non-commercial use only; commercial, governmental and organisational
use needs written permission; model output, no warranty, no liability) and
below it the full EULA text, which governs — the summary is not exhaustive.
Nothing is downloaded before you press <b>Accept &amp; Download</b>, and the
consent is asked again every time; the plugin only records which EULA version
you accepted.</li>
<li>The archive is verified against the manifest's <b>SHA-256</b> before it is
extracted. A missing, placeholder or mismatched digest aborts the install —
this is fail-closed by design.</li>
<li>The archive is unpacked into a staging folder inside the <b>Binary
directory:</b> you set (or <code>~/.aether/bin</code>). There all three
binaries must be present, are made executable (Linux/macOS), have the macOS
Gatekeeper quarantine attribute or the Windows Mark-of-the-Web cleared, and
<code>aether_core --version</code> must report the version the manifest
promised. Only then are the files swapped into place — all or nothing, so a
failed install leaves the previous engine untouched. Other files in that
folder (for example a <code>license.key</code>) are never touched.</li>
</ol>
<p><b>macOS:</b> the engine is ad-hoc signed and not notarized. The plugin
clears the download quarantine on every start and whenever you pick a folder
with <b>Browse...</b> or <b>Auto-detect</b>, so a copy you unzipped from a
browser download works too. If macOS still refuses to run it, the error shows
the fix: <code>xattr -cr "&lt;engine folder&gt;"</code> in Terminal.</p>
<p>Supported platforms: <b>Windows x64</b>, <b>Linux x64</b>,
<b>macOS arm64</b> (Apple Silicon). Intel macOS is not supported.</p>

<h3>How the plugin finds the engine</h3>
<p>First directory that contains <i>all three</i> binaries wins:</p>
<ol>
<li>the <b>Binary directory:</b> saved in Settings
(<code>waveshed/binary_dir</code>),</li>
<li>the <code>AETHER_BIN_DIR</code> environment variable,</li>
<li>the system <code>PATH</code> (looked up via <code>aether_core</code>),</li>
<li><code>~/.aether/bin</code>,</li>
<li><code>&lt;plugin directory&gt;/bin</code>.</li>
</ol>
<p><b>Auto-detect</b> in Settings runs exactly that search and fills the field
in. A partially populated directory is skipped, not half-used.</p>

<h3>API key</h3>
<p><code>aether_core</code> needs a key; the converter and the exporter do not.
Paste it into <b>API key:</b> in Settings and press <b>Save</b> — the status
line turns into <b>&check; Key saved</b>. <b>Get API Key</b> opens
<a href="{GET_KEY_URL}">waveshed.io/get-key</a>, the page that issues free
keys for non-commercial use. The key is
stored in QGIS settings under <code>waveshed/api_key</code> and passed to the
engine as the <code>AETHER_LICENSE</code> environment variable at launch.</p>
<p>The plugin checks the key <i>structurally</i> only: valid Base58 (Bitcoin
alphabet) text whose payload decodes to a 188-byte licence of format v3
(about 257 characters), with a known licence type and a sane expiry. Keys of
the retired v1/v2 formats are recognised and rejected with a clear message.
Whitespace picked up from a wrapped terminal copy is stripped automatically.
The Ed25519 signature, the machine lock and revocation are verified inside the
engine, so a structurally valid but unsigned key fails at run time, not in the
dialog.</p>

<h3>Machine fingerprint</h3>
<p>A machine-locked (node-locked) key is issued against this computer's
hardware fingerprint. Press <b>Show machine fingerprint</b> in Settings: the
plugin runs <code>aether_core --fingerprint</code> in the background (it can
take a few seconds) and shows the 64-character hex value in a copyable field.
Send that value when requesting a locked key. Engine builds older than
<b>v0.4.2</b> do not know the flag and the plugin says so explicitly — update
the engine.</p>

<h3>Engine updates</h3>
<p>Once per QGIS session the plugin compares the installed engine with the
signed manifest in the background; when a newer release exists it shows one
notice per release with an <b>Update</b> button. Switch this off with
<b>Check for engine updates when QGIS starts</b> in Settings; offline it stays
silent. <b>Check for updates</b> in Settings does the same on demand and shows
<b>Installed engine</b> / <b>Latest release</b>. <b>Update</b> (or <b>Download
Binaries</b>) runs the normal install — EULA consent included — and swaps the
new engine in only after it has been verified (see above). On Windows, close
running analyses first: an engine that is still running is detected and the
update is refused, leaving the old engine in place. An analysis that fails
because the engine is too old offers <b>Update engine</b> in its error
message. The installed version is remembered under
<code>waveshed/installed_engine_version</code>. If the manifest declares a
<code>min_plugin_version</code> newer than the installed plugin you get an
advisory <b>Plugin update recommended</b> warning and can still continue.
Some engine defects are fixed only by updating — see
<a href="#troubleshooting">Troubleshooting</a>.</p>
""".replace("{GET_KEY_URL}", GET_KEY_URL)

# ---------------------------------------------------------------------------
# Modes / models
# ---------------------------------------------------------------------------

_MODELS = """
<p>The <b>Analysis Mode:</b> row at the top of the dialog picks what is
computed. It applies to whichever analysis tab you are on, and it changes which
controls the tabs show.</p>

<h3>Line of Sight (LOS)</h3>
<p>Pure geometry: can a receiver at the given altitude see the transmitter's
antenna over the terrain surface (buildings included when you asked for them),
bending rays with the effective-earth model? No frequency, no power, no antenna
pattern — so the <b>Asset</b> and <b>AZ Rotation (deg)</b> columns are hidden
in the 360&deg; tab, and the P2P tab labels its endpoints <b>Site A:</b> /
<b>Site B:</b>. The result is a two-value raster: visible / not visible.</p>

<h3>Propagation Loss</h3>
<p>Signal strength from the transmitter's ERP through the <b>ITM</b>
(Longley-Rice / Irregular Terrain Model) point-to-point mode. Selecting it
reveals the <b>Model:</b> dropdown — which offers <b>ITM</b> only — the
<b>ITM Parameters</b> group in the 360&deg; tab, and the frequency/ERP controls
in the P2P tab, where the endpoints become <b>Transmitter:</b> /
<b>Receiver:</b>. The result is a continuous dBm raster.</p>
<p>The older free-space-loss model (<code>SIMPLE_LOSS</code>) was withdrawn as
a user choice in 0.2.0: it ignores terrain entirely, so on a terrain analysis
tool it reads as a modelling option when it is really a lower bound. Results
computed with it still load and style correctly.</p>

<h3>LOS Floor</h3>
<p>One run that answers coverage at <i>every</i> altitude. For each location the
engine returns the <b>lowest altitude above ground at which that location first
gains line of sight</b> to the transmitter; locations that never see it are
no-data. The raster is 16-bit, one count = 0.5 m, 65535 = never visible.</p>
<ul>
<li>It is geometric like LOS, so assets and antenna rotation are hidden.</li>
<li>The <b>Altitudes</b> table is replaced by a note: a single pass already
spans all altitudes, so per-altitude rows would only recompute the same
raster. One job per site is built (internally at 1.5 m AGL, which the result
does not depend on).</li>
<li>It is a coverage-only output — a single link has no minimum-altitude
surface — so the <b>P2P Link</b> tab is disabled while it is selected, and the
radio itself is disabled while the P2P tab is in front.</li>
<li>After the run the plugin offers to open the
<a href="#altitude-explorer">Altitude Explorer</a>, which re-styles the result
live at any altitude without recomputing.</li>
</ul>
<p>On the wire the model is called <code>MIN_ALT</code>; that is the name in
the job JSON, in the Processing dropdown and on the layer's stamp. In the user
interface it is <b>LOS Floor</b>.</p>

<h3>Earth Radius Mode</h3>
<p>Both analysis tabs carry <b>Earth Radius Mode:</b>, and it applies to every
model including plain LOS, because it is the horizon geometry itself:</p>
<ul>
<li><b>FOUR_THIRDS</b> (default, recommended) — standard refraction: the engine
derives the effective curvature from the surface refractivity, which at
N = 301 gives the familiar k &asymp; 4/3 earth.</li>
<li><b>ADVANCED</b> — the Sandia/Doerry adaptive method: the k-factor is
computed per path from the refractivity and the two endpoint altitudes, and
clamped to 0.1&ndash;10.</li>
</ul>
<p>See <a href="#physics">Physics notes</a> for what that curvature does to a
long path, and <a href="#itm">ITM parameters</a> for everything in the
<b>ITM Parameters</b> group.</p>
"""

# ---------------------------------------------------------------------------
# Heights: AGL vs AMSL
# ---------------------------------------------------------------------------

_HEIGHTS = """
<p>Every height and altitude in the plugin is paired with a mode. They are not
interchangeable, and picking the wrong one is the most common way to get a
confidently wrong answer.</p>
<ul>
<li><b>AGL</b> — metres <i>above the ground directly below</i>. A mast, a
rooftop antenna, a terrain-following drone. The engine samples the terrain at
that location and adds the height.</li>
<li><b>AMSL</b> — metres <i>above mean sea level</i>: an absolute elevation.
An aircraft's flight level, a summit station whose elevation you know, a site
digitised from a map.</li>
</ul>

<h3>Limits</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Mode</th><th>Minimum</th><th>Why</th></tr>
<tr><td>AGL</td><td><b>1.0 m</b></td><td>ITM is only validated down to 1 m
(below it the effective antenna height collapses towards zero and the
ground-reflection term saturates), and <code>.abt</code> terrain is stored in
<b>0.5 m</b> steps — so below 1 m the visibility decision is made by the DEM's
rounding rather than by geometry, which renders as checkerboard speckle.</td></tr>
<tr><td>AMSL</td><td><b>-500 m</b></td><td>An absolute elevation is
legitimately zero or negative: the Dead Sea shore is about -430 m and Schiphol
is -4 m. -500 m clears the lowest exposed land on Earth while still rejecting a
value typed by accident.</td></tr>
</table>
<p>Both upper limits in the dialogs are 10&nbsp;000 m. ITM's own validated
ceiling is 1000 m AGL — see <a href="#itm">ITM parameters</a>.</p>

<h3>Where the floor is enforced</h3>
<p>The spinbox minimum follows the mode beside it, so you meet the limit while
typing. Because not every path has a spinbox, the same rule is applied
again:</p>
<ul>
<li>in the job builder, which <i>rejects</i> the job rather than quietly
computing at a height you did not ask for;</li>
<li>in both batch-CSV parsers, per row, naming the line number;</li>
<li>in the Processing algorithms, whose height parameters are AGL-only;</li>
<li>in asset loading — a hand-edited asset whose
<code>default_height_m</code> is below the floor is raised to 1 m and the
substitution is logged, so it cannot seed bad site rows.</li>
</ul>
<p>The engine has its own backstop: it clamps a sub-minimum AGL height and
prints a <code>[Warn]</code>, so old job files still run. The plugin does not
rely on that.</p>

<h3>LOS Floor is the exception</h3>
<p>In <b>LOS Floor</b> mode the receiver altitude is the answer, not an input,
so the receiver is exempt from the floor (a 0 m receiver is exactly the
question being asked). The transmitter is a real antenna and is checked
normally.</p>

<h3>Reading a result</h3>
<p>A coverage layer answers "at the receiver altitude you asked for". A LOS
Floor layer is measured <b>above ground</b> by default; the Altitude Explorer
can build a sea-level twin of it, which answers "what altitude must I hold
here" — see <a href="#altitude-explorer">LOS Floor and the Altitude
Explorer</a>.</p>
"""

# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

_TERRAIN = """
<p>The engine reads terrain only as <code>.abt</code> tiles: 16-bit elevations
in 0.5 m steps, one tile per fixed geographic square. Everything in this
section is about producing them.</p>

<h3>Where terrain can come from</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Source</th><th>Route</th><th>Notes</th></tr>
<tr><td><b>XYZ elevation tiles</b> (Terrarium, Mapbox Terrain-RGB)</td>
<td>The engine's own parallel downloader writes <code>.abt</code> directly —
no intermediate file.</td>
<td>Fastest path by far, and the only path for an XYZ layer: rendering XYZ
tiles through QGIS instead is <i>not</i> offered as a fallback, because it
would silently produce terrain built the wrong way.</td></tr>
<tr><td><b>Local DEM files and folders</b> (.tif, .tiff, .dem, .hgt)</td>
<td>Handed to <code>aether_converter ingest</code> as <code>sources[]</code>
entries in their own CRS; the converter reprojects while sampling.</td>
<td>Folders are scanned recursively and sorted; for overlapping files the
earlier-sorted path wins per pixel. Formats the converter's reader cannot open
(.hgt/.dem, VRT, <code>/vsi*</code>, GeoTIFFs over 512 MB) are first
window-copied to a plain GeoTIFF at native resolution — never resampled.</td></tr>
<tr><td><b>Rendered services</b> (WMS, WMTS, ArcGIS, WCS)</td>
<td>QGIS renders the layer per tile into a temporary WGS84 GeoTIFF, which is
then ingested.</td>
<td>Unavoidable for a server that only answers with pictures, and the slowest
route. A service that returns <i>imagery</i> rather than elevation is
refused outright.</td></tr>
<tr><td><b>A folder of pre-built <code>.abt</code> tiles</b></td>
<td>Handed to the engine untouched.</td>
<td>What the <a href="#map-converter">Map Converter</a> produces. Select it as
the <b>Local terrain dir:</b> in Settings; it then appears in the DEM
dropdown as <b>Local: &lt;path&gt;</b>.</td></tr>
</table>

<h3>XYZ encoding and zoom</h3>
<p>Terrarium and Mapbox Terrain-RGB tiles are both plain RGB PNGs, so nothing
in the pixels distinguishes them — decoding one as the other puts the terrain
near -32&nbsp;000 m. The plugin resolves the encoding from the layer's
<code>interpretation=</code> parameter when QGIS has one, else from a table of
known services (<code>resources/known_services.json</code>), else it assumes
Terrarium and says so in the log. A URL that names both families is refused
with an explanation rather than guessed.</p>
<p>QGIS defaults a hand-added XYZ layer to <b>Max. Zoom Level</b> 18, but no
public terrain service goes that deep — every request past the real maximum
returns 404. The plugin clamps the zoom to what the service is known to publish
(for example z15 for the Mapzen/AWS Terrarium tiles) and logs the reason. The
<b>DEM Layer:</b> info line shows the effective zoom and the resulting ground
resolution, for example
<code>terrarium z15 (~5m at equator, less at higher latitudes)</code>.</p>

<h3>Resolution and tile extent</h3>
<p>The plugin offers <b>2, 5, 10, 30, 90 and 250 m</b>. The engine itself
accepts any value at or above 0.1 m; this is a convenience list, kept in step
with the per-resolution tile extent below — the single source of truth for both
terrain paths.</p>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Resolution</th><th>Tile extent</th><th>Tile size</th>
<th>File size</th><th>Typical use</th></tr>
<tr><td>2 m</td><td>0.1&deg;</td><td>5556 &times; 5556 px</td><td>~63 MB</td>
<td>Urban / very short range</td></tr>
<tr><td>5 m</td><td>0.25&deg;</td><td>5556 &times; 5556 px</td><td>~63 MB</td>
<td>Detailed local studies</td></tr>
<tr><td>10 m</td><td>0.25&deg;</td><td>2780 &times; 2780 px</td><td>~16 MB</td>
<td>Default; national DEMs</td></tr>
<tr><td>30 m</td><td>0.5&deg;</td><td>1852 &times; 1852 px</td><td>~7 MB</td>
<td>Routine regional work (SRTM 1-arcsec)</td></tr>
<tr><td>90 m</td><td>1.0&deg;</td><td>1236 &times; 1236 px</td><td>~3 MB</td>
<td>Large areas (SRTM 3-arcsec)</td></tr>
<tr><td>250 m</td><td>2.0&deg;</td><td>892 &times; 892 px</td><td>~1.6 MB</td>
<td>Continental scale (GMTED/MODIS)</td></tr>
</table>
<p>The extent ladder is not a disk-size convenience: the engine loads every
tile a wedge touches into <i>one contiguous</i> terrain-atlas allocation and
rejects the job when that exceeds about <b>3.86 GB</b>. Finer resolutions
therefore get smaller tiles. At 2 m a tile is ~63 MB, which puts the practical
ceiling near 60 tiles for a single run.</p>

<h3>The tile pool and the cache</h3>
<p>Terrain is cached under the <b>Terrain cache:</b> directory in Settings
(default <code>~/.aether/cache</code>):</p>
<ul>
<li><code>pool/&lt;id&gt;/</code> — every tile ever built for one source (plus
the building set, when buildings were requested). Membership is per tile, so a
shorter range, a nudged site or a narrower sector downloads nothing new.</li>
<li><code>views/&lt;id&gt;/</code> — the exact tile set one run hands the
engine, hard-linked from the pool (copied where the filesystem refuses a
link).</li>
</ul>
<p>Tiles are named by position and resolution, for example
<code>tile_N47.50E8.50_30m.abt</code>. A tile that was written short (a killed
run) or that is missing the buildings its pool identity claims is flagged for
rebuild and re-made on the next run. Before a run the plugin prices only what is
genuinely missing, logs a <code>terrain plan:</code> line, and asks for
confirmation with <b>Large terrain download</b> past ~50 GB (and more insistently
past ~200 GB).</p>
<p>The cache never evicts anything. <b>Clear...</b> beside <b>Terrain cache:</b>
in Settings reports the size and deletes it — that is also how you pick up newer
OpenFreeMap building data, whose cache identity is deliberately date-free.</p>

<h3>Missing data degrades to sea level, loudly</h3>
<p>Ground that a source has no data for is never an error. It becomes
<b>0 m (sea level)</b> for Site Analysis, and you are told:</p>
<ul>
<li><b>Downloads:</b> HTTP 404 means "no tile here", not a failure. The
converter counts them (<code>[Stats] NO-DATA (HTTP 404):</code>) and exits
successfully; the plugin warns that the requested area reaches past the
service's coverage. Real failures — timeouts, connection errors, 403, 429, 5xx,
decode errors — can still abort the run.</li>
<li><b>DEM files and folders:</b> when the analysis range reaches past the data,
you get a warning naming that, once per run, and pixels outside are ingested as
0 m.</li>
<li><b>Rendered services:</b> <code>writeRaster</code> reports success even for
an answer that arrived with holes, so the export is measured against the
layer's own published extent. Empty ground outside it is the coverage edge
(already warned about); empty ground <i>inside</i> it is a partial answer and
gets its own warning. Without that check a dropped block reaches you as silent
sea level.</li>
<li><b>Inside the cache:</b> the pool keeps voids (the Map Converter's output
must keep them by contract); the per-run view the engine reads gets a copy with
voids filled to 0 m. A raw void would otherwise be read as terrain five
kilometres below the sea.</li>
</ul>
<p>Coverage computed over assumed sea level is not reliable. If the warning
mentions your area of interest, use a source that covers the full range.</p>

<h3>Buildings</h3>
<p>Tick <b>Include buildings (OpenFreeMap)</b> in the 360&deg; tab to fetch
OpenStreetMap building footprints and burn them into the terrain surface, so
they obstruct exactly as terrain does. Buildings are published at zoom 14 only,
which makes this a sizeable download — it suits small areas; a request over
about 4096 tiles is refused rather than started. Building tiles pool alongside
terrain and are reused across runs.</p>
<p>If buildings were requested but could not be fetched or applied, the run
continues <i>without</i> them, says so in the log, and flags the tiles for
rebuild — so the next run retries instead of serving building-free terrain from
a cache entry that claims to have buildings.</p>
<p>The Map Converter can also take buildings from a file or folder
(FlatGeobuf, Shapefile, GeoPackage, GeoJSON, ESRI .gdb); the converter reads
FlatGeobuf in WGS84 and anything else is converted first.</p>

<h3>Not a DEM</h3>
<p>Choosing a basemap or an RGB imagery service as the terrain source would
read colour values as metres. The dialogs ask <b>Not a DEM?</b> before such a
run, and terrain preparation refuses a layer that classifies as imagery
outright — including on the Processing paths, which never see a dialog.</p>
"""

# ---------------------------------------------------------------------------
# 360 Site Analysis
# ---------------------------------------------------------------------------

_SITE_ANALYSIS = """
<p>The <b>360&deg;</b> tab computes an area map around one or more transmitter
sites. Every site is crossed with every receiver altitude, so three sites and
two altitudes are six jobs, run one after another.</p>

<h3>Sites</h3>
<p>The <b>Sites</b> table, with <b>Add</b> / <b>Remove</b> above it. One empty
row is present at start-up. Columns:</p>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Column</th><th>Meaning</th></tr>
<tr><td><b>Asset</b></td><td>An emitter definition from the <b>Assets</b> tab.
It supplies the frequency and ERP, and pre-fills the height and height mode
from the asset's defaults. Hidden in LOS and LOS Floor modes, which have no
frequency or power.</td></tr>
<tr><td><b>Location</b></td><td><b>Lat</b> and <b>Lon</b> in decimal degrees
(six decimals, WGS84) plus <b>Pick</b>, which minimises the dialog and lets you
click the site on the map canvas. Placed sites are drawn on the canvas as cyan
dots. A row left at 0/0 is skipped. Placing a site here clears the P2P tab's
endpoints, so a run can never use points you last placed in the tab you are not
looking at.</td></tr>
<tr><td><b>Height (m)</b></td><td>Transmitter antenna height, default 30.0 m.
Its minimum follows the column beside it — see
<a href="#heights">Heights: AGL and AMSL</a>.</td></tr>
<tr><td><b>Height Mode</b></td><td><b>AGL</b> or <b>AMSL</b>.</td></tr>
<tr><td><b>AZ Rotation (deg)</b></td><td>Bearing the antenna pattern is rotated
to, clockwise from north. Only meaningful for a directional asset; hidden in
the geometric modes.</td></tr>
<tr><td><b>AZ Start (deg)</b> / <b>AZ End (deg)</b></td><td>The azimuth sector
to compute, clockwise from north, 0&ndash;360. The default 0&ndash;360 is the
full circle; a narrow sector saves both terrain and computation, because only
the tiles the arc touches are prepared. Start &gt; End wraps past north
(for example 350&ndash;20).</td></tr>
<tr><td><b>Range (km)</b></td><td>Radius of the analysis disc,
1&ndash;500 km, default 50.</td></tr>
</table>

<h3>Altitudes</h3>
<p>The <b>Altitudes</b> table (also with <b>Add</b> / <b>Remove</b>) lists the
receiver altitudes to compute: one <b>Altitude (m)</b> per row, each with a
<b>Reference</b> of <b>AGL</b> or <b>AMSL</b>. The default row is <b>1.5 m AGL</b> — a person with a handheld.
Add 100 m AGL for a drone, or 3000 m AMSL for an aircraft. Every altitude costs
a full run.</p>
<p>In <b>LOS Floor</b> mode the table is replaced by a note: that single run
already covers every altitude.</p>

<h3>Analysis Parameters</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Control</th><th>Meaning</th></tr>
<tr><td><b>DEM Layer:</b></td><td>The elevation source: any raster layer in the
project, plus the <b>Local: &lt;path&gt;</b> entry when a <b>Local terrain
dir:</b> is configured in Settings. The plugin's own coverage and P2P results
are filtered out of the list; terrain the plugin generated is kept, because it
is elevation data. The grey line below shows <b>Source resolution:</b>, or
<b>Local directory:</b>, or a warning that the layer looks like
imagery.</td></tr>
<tr><td><b>Resolution (m):</b></td><td>2, 5, 10, 30, 90 or 250 — the computation
grid, and the resolution of the terrain tiles that get built. Default 10. See
the table in <a href="#terrain">Terrain</a>.</td></tr>
<tr><td><b>Buildings:</b></td><td><b>Include buildings (OpenFreeMap)</b> — burn
OSM building footprints into the terrain surface.</td></tr>
<tr><td><b>Backend:</b></td><td><b>AUTO</b> (GPU when one is usable, else CPU),
<b>GPU</b> or <b>CPU</b>. Forcing CPU is much slower but sidesteps a driver
problem.</td></tr>
<tr><td><b>Earth Radius Mode:</b></td><td><b>FOUR_THIRDS</b> or
<b>ADVANCED</b> — see <a href="#models">Modes and models</a>.</td></tr>
<tr><td><b>Output Dir:</b></td><td>Where results are written, with
<b>Browse...</b>. Defaults to <code>aether_output</code> in the system temp
directory. Each job gets its own sub-directory.</td></tr>
</table>
<p>The RAM and VRAM budgets sent with every job come from the <b>Settings</b>
tab, not from this tab.</p>

<h3>ITM Parameters</h3>
<p>Shown only in <b>Propagation Loss</b> mode with model <b>ITM</b>:
<b>Ground Permittivity:</b>, <b>Ground Conductivity:</b>,
<b>Surface Refractivity:</b>, <b>Radio Climate:</b>, <b>Polarization:</b>,
<b>Confidence:</b> and <b>Reliability:</b>. Each is explained under
<a href="#itm">ITM parameters</a>.</p>

<h3>What happens when you press Run Analysis</h3>
<ol>
<li><b>Checks first.</b> At least one site, at least one site with a location,
at least one altitude, a DEM or terrain directory, an output directory, and the
engine installed — a missing engine stops the run here rather than after the
terrain extract.</li>
<li><b>Antenna floor.</b> Any AGL height below 1 m is rejected outright (the
job builder would raise anyway).</li>
<li><b>Model validity.</b> Parameters outside the propagation model's range
raise <b>Outside the propagation model's range</b>, listing each problem; you
can run anyway, but the numbers are probably invalid. See
<a href="#itm">ITM parameters</a>.</li>
<li><b>Terrain size.</b> The tiles that still have to be built are priced
against the pool; a large figure asks for confirmation. A local terrain
directory that does not reach across the analysis area (or was built at a
different resolution, or without the buildings you asked for) raises
<b>Incomplete terrain coverage</b>.</li>
<li><b>Per job:</b> prepare terrain &rarr; write the job JSON &rarr; run
<code>aether_core</code> (its wedge progress drives the bar and every line
lands in the log) &rarr; run <code>aether_export</code> to a GeoTIFF &rarr;
load the layer.</li>
</ol>
<p><b>Stop</b> cancels: the engine process and its children are killed, and the
run ends quietly. If a job fails, the <i>whole run</i> stops there with
<b>Analysis Failed</b> and the engine's last lines — later sites are not
attempted, and results already produced by earlier jobs stay on disk but are not
loaded.</p>

<h3>Results</h3>
<p>Every successful run adds its layers under <b>Waveshed &rarr; Coverage
&rarr; Run &lt;timestamp&gt;</b>, named
<code>Site 1 (47.0563, 8.4846) @ 1.5m AGL</code> — or
<code>&mdash; LOS Floor</code> in LOS Floor mode, where the plugin then offers
to open the Altitude Explorer. See <a href="#results">Result layers</a>.</p>
"""

# ---------------------------------------------------------------------------
# Results / symbology
# ---------------------------------------------------------------------------

_RESULTS = """
<p>Results are loaded into the layer tree under a <b>Waveshed</b> group and
styled by the model that produced them. The model is also stamped on the layer
as a custom property, which is how the Altitude Explorer recognises the layers
it can drive — and it survives a project save.</p>

<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Group</th><th>Holds</th></tr>
<tr><td><b>Waveshed &rarr; Coverage &rarr; Run &lt;timestamp&gt;</b></td>
<td>360&deg; results, one sub-group per run.</td></tr>
<tr><td><b>Waveshed &rarr; P2P</b></td><td>Point-to-point result tables.</td></tr>
<tr><td><b>Waveshed &rarr; Terrain</b></td><td>Terrain the plugin loaded for
inspection (Map Converter's <b>Inspect .abt...</b>).</td></tr>
<tr><td><b>Waveshed &rarr; Above Sea Level</b></td><td>Sea-level twins of LOS
Floor layers.</td></tr>
<tr><td><b>Waveshed &rarr; Best Site</b></td><td>Merged best-altitude and
best-site rasters.</td></tr>
<tr><td><b>Waveshed &rarr; Contours</b></td><td>Iso-altitude contour
layers.</td></tr>
</table>

<h3>Symbology</h3>
<ul>
<li><b>LOS</b> — exact two-value ramp: 0 is transparent (<i>No Data</i>),
1 is green at 70&nbsp;% opacity (<i>Visible</i>).</li>
<li><b>Propagation Loss (ITM)</b> — interpolated signal-strength ramp from
-140 dBm (red) through -70 dBm (yellow) to 0 dBm (green). The band holds
received power in dBm.</li>
<li><b>LOS Floor</b> — a continuous blue-to-red ramp over the altitude actually
required: blue where a receiver at ground level already sees the transmitter,
red where it must climb. The ramp spans the layer's own range (for a sea-level
twin it starts at the valley floor, not at 0 m), and the never-visible sentinel
is transparent through the GeoTIFF's no-data tag.</li>
<li><b>Best site</b> — one categorical colour per site, labelled with the site
name; 255 means no site reaches that pixel.</li>
<li><b>Iso-altitude contours</b> — a graduated line style with the altitude in
the <code>alt_m</code> attribute; labels appear once you zoom in on a dense
layer.</li>
</ul>
<p>QGIS classifies on the <i>raw</i> band value, so a LOS Floor raster's
pixel values are counts of 0.5 m, not metres (GDAL scale 0.5). Anything you
build on top of one should convert at the boundary rather than assume
metres.</p>

<h3>Files a run leaves behind</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>File</th><th>What it is</th></tr>
<tr><td><code>&lt;name&gt;_job.json</code></td><td>The exact job configuration
the run used — the one durable record of how the raster was made. It is also
how the Altitude Explorer finds the terrain a run was computed over.</td></tr>
<tr><td><code>&lt;name&gt;.bit</code> / <code>.tiles</code> +
<code>.json</code></td><td>The engine's native output and its
sidecar.</td></tr>
<tr><td><code>&lt;name&gt;.tif</code></td><td>The Cloud-Optimized GeoTIFF
<code>aether_export</code> produced — the layer you see.</td></tr>
<tr><td><code>&lt;name&gt;.csv</code>, <code>p2p_report.txt</code>,
<code>*.gp</code></td><td>Point-to-point outputs — see
<a href="#p2p">P2P links</a>.</td></tr>
</table>
"""

# ---------------------------------------------------------------------------
# P2P
# ---------------------------------------------------------------------------

_P2P = """
<p>The <b>P2P Link</b> tab analyses a single link between two points. In
<b>Line of Sight (LOS)</b> mode the endpoints are labelled <b>Site A:</b> and
<b>Site B:</b>; in <b>Propagation Loss</b> mode they become
<b>Transmitter:</b> and <b>Receiver:</b> and the <b>Parameters</b> group
appears. The tab is disabled in <b>LOS Floor</b> mode — a single link has no
minimum-altitude surface.</p>

<h3>Endpoints</h3>
<ul>
<li><b>Location:</b> — <b>Lat</b> / <b>Lon</b> in decimal degrees, or
<b>Pick from Map</b>. Site A is drawn red, Site B blue, and the link between
them yellow. Placing an endpoint here clears the 360&deg; tab's sites.</li>
<li><b>Height:</b> — the antenna height with its <b>Mode:</b>
(<b>AGL</b> / <b>AMSL</b>). Defaults: 30.0 m for A, 1.5 m for B. Only one
decimal is kept, because that is all the batch CSV the run is built from
carries.</li>
</ul>

<h3>Parameters (Propagation Loss only)</h3>
<ul>
<li><b>Asset:</b> — an emitter from the <b>Assets</b> tab; selecting one fills
in frequency and ERP. Leave it blank to type them by hand.</li>
<li><b>Frequency (MHz):</b> — 1&ndash;3000 MHz, default 433.</li>
<li><b>ERP (Watts):</b> — effective radiated power, default 10 W.</li>
<li><b>AZ Rotation:</b> — bearing the antenna pattern is rotated to, clockwise
from north; only meaningful for a directional asset.</li>
</ul>

<h3>Analysis</h3>
<p><b>DEM Layer:</b> (a raster layer in the project — this tab has no
<b>Local:</b> terrain-directory entry), <b>Resolution:</b>, <b>Backend:</b>,
<b>Output Dir:</b> with <b>Browse</b>, and <b>Earth Radius Mode:</b>. The same
meanings as in the <a href="#site-analysis">360&deg; tab</a>.</p>

<h3>Running</h3>
<p><b>Run Analysis</b> validates the inputs (both endpoints placed, a DEM
selected, an output directory, both antenna heights above their floor),
warns about out-of-range model parameters and large terrain downloads, then
writes a two-line batch CSV from the endpoints, prepares terrain over the pair,
and runs <code>aether_core</code>. There is no export step — the engine writes
its results directly.</p>

<h3>Outputs</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>File</th><th>Contents</th></tr>
<tr><td><code>&lt;output_name&gt;.csv</code></td><td>One row per link:
<b>Source_ID</b>, <b>Target_ID</b>, <b>Signal_dBm</b>,
<b>Path_Loss_dB</b>.</td></tr>
<tr><td><code>p2p_report.txt</code></td><td>Human-readable summary from the
engine.</td></tr>
<tr><td><code>terrain_profile.gp</code></td><td>Terrain elevation against
distance along the path.</td></tr>
<tr><td><code>height_profile.gp</code></td><td>The same profile with the earth
bulge added.</td></tr>
<tr><td><code>path_profile.gp</code></td><td>Cumulative path loss at each
distance.</td></tr>
</table>
<p>The CSV and the report are shown in the <b>Results</b> box. When profile
files exist, <b>View Plots</b> appears and opens <b>P2P Link Analysis
Plots</b> with two tabs:</p>
<ul>
<li><b>Terrain Profile</b> — terrain elevation on the left axis and signal
strength (ERP in dBm minus path loss) on the right.</li>
<li><b>Curved Earth Profile</b> — the terrain lifted onto a 4/3-earth bulge,
the straight line of sight between the two antenna tips, the full first Fresnel
zone (F1) as a dotted outline and the <b>0.6 F1</b> clearance band shaded
inside it. 0.6&nbsp;F1 is the criterion that matters: once terrain stays outside
60&nbsp;% of the first Fresnel radius, diffraction loss is back to roughly free
space. F1 = 547.7 &times; &radic;(d1&middot;d2 / (f&middot;D)) with distances in
km, frequency in MHz and the radius in metres.</li>
</ul>
<p>The plots need <b>matplotlib</b>. QGIS normally ships it; if it is missing
you get <b>Missing Dependency</b> and everything else still works.</p>

<h3>Batch CSV</h3>
<p>The batch mode is exposed through the Processing algorithm
<b>Point-to-Point Link Analysis</b> (parameter <b>Batch CSV File</b>); the tab
itself always builds a two-line CSV from its own endpoints. The format is the
same:</p>
<pre>S,TX1,47.056300,8.484600,40.0,AGL
R,RX1,47.500000,8.900000,10.0,AGL
R,RX2,47.600000,8.700000,2.0,AMSL</pre>
<ul>
<li>Six fields per row: <b>type</b> (<code>S</code> = source/transmitter,
<code>R</code> = receiver), <b>ID</b>, <b>latitude</b>, <b>longitude</b>,
<b>altitude in metres</b>, <b>mode</b> (<code>AGL</code> or
<code>AMSL</code>).</li>
<li>Blank lines and lines starting with <code>#</code> are ignored; a UTF-8
byte-order mark is tolerated.</li>
<li>Every violation names its line number: too few fields, a type that is not
S or R, a mode that is not AGL/AMSL, a non-numeric altitude, or an AGL altitude
below the 1 m floor.</li>
<li>A batch is computed as the <b>full cross product</b> of the S rows with the
R rows — 3 sources and 10 receivers are 30 links.</li>
</ul>
<p><b>Terrain</b> for a batch is prepared over the centroid of all points with
a radius of half the bounding-box diagonal plus 5 km (at least 10 km).</p>
<p><b>The range cap matters.</b> The engine reads
<code>analysis.max_range_km</code> as a per-link <i>distance cap</i> and writes
a 0 dB / 0 m result for any link longer than it — silently. The plugin
therefore sizes the cap as the larger of the terrain radius and the longest
link in the batch rounded up plus 1 km, rather than reusing the terrain radius
(which once answered 0/0 for a 25.2 km link under an 18 km terrain radius).
Raising the cap costs nothing in terrain: the engine selects tiles by walking
each link's own path.</p>
"""

# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

_ASSETS = """
<p>The <b>Assets</b> tab manages emitter definitions — frequency, power,
antenna gain, antenna patterns and default mounting height. Each asset is a
JSON file in the assets directory (<code>waveshed/assets_dir</code>, default
<code>~/.aether/assets</code>), so they are shared between projects and easy to
copy between machines.</p>

<h3>Managing assets</h3>
<p>The list along the top, with <b>Reload</b>, <b>New</b>, <b>Save</b>,
<b>Save As</b> and <b>Delete</b>, plus the <b>Name:</b> field. The editor below
has two sub-tabs: <b>Parameters</b> and <b>Antenna Patterns</b>. Unsaved
changes are tracked and you are asked before they are lost. A file that is not
valid JSON is skipped silently rather than breaking the list.</p>

<h3>Parameters</h3>
<ul>
<li><b>Output</b> group: <b>Frequency:</b> (MHz), <b>Peak Power:</b> (W),
<b>Antenna Gain:</b> (dBi), <b>Polarization:</b>
(<b>Horizontal (0)</b> / <b>Vertical (1)</b>), and a read-only
<b>Computed ERP: &mdash; W</b> line.</li>
<li><b>Defaults</b> group: <b>Default Height:</b> and <b>Height Mode:</b>
(<b>AGL</b> / <b>AMSL</b>) — what a new site row is pre-filled with when this
asset is chosen.</li>
<li><b>ITM Statistical Parameters</b> group: <b>Fraction of Situations:</b> and
<b>Fraction of Time:</b>, the asset's own record of the confidence/reliability
quantiles.</li>
</ul>

<h3>ERP and EIRP</h3>
<p>ERP is recomputed from peak power and gain whenever an asset is loaded or
edited:</p>
<pre>EIRP(dBm) = 10 &middot; log10(peak power in W &times; 1000) + gain(dBi)
ERP(dBm)  = EIRP(dBm) - 2.15
ERP(W)    = 10 ^ ((ERP(dBm) - 30) / 10)</pre>
<p>The 2.15 dB is the gain of a half-wave dipole over an isotropic radiator:
<b>EIRP is referenced to an isotropic antenna, ERP to a dipole</b>, so ERP is
always 2.15 dB lower for the same transmitter. The engine's
<code>erp_watts</code> field — and the <b>ERP (Watts):</b> box in the P2P tab —
are ERP. If your datasheet quotes EIRP, subtract 2.15 dB (divide by 1.64)
first, or enter the transmitter power and antenna gain and let the plugin do
it.</p>

<h3>Antenna patterns</h3>
<p>The <b>Antenna Patterns</b> sub-tab holds an <b>Azimuth (Horizontal)
Pattern</b> and an <b>Elevation (Vertical) Pattern</b> table, both with columns
<b>Angle (deg)</b> and <b>Gain (0-1)</b> — linear gain, 1.0 at the main lobe.
Each table has <b>Add Row</b>, <b>Remove Row</b>, <b>Load .az</b> /
<b>Load .el</b>, <b>Save .az</b> / <b>Save .el</b>, <b>Generate</b>,
<b>Visualize</b> and <b>Clear</b>.</p>
<ul>
<li><b>File format</b> — SPLAT! <code>.az</code> / <code>.el</code>: one
whitespace-separated <i>angle gain</i> pair per line, header and comment lines
containing <code>;</code> or <code>#</code> skipped, gains validated to
0&ndash;1.</li>
<li><b>Generate</b> opens <b>Generate Azimuth Pattern</b> /
<b>Generate Elevation Pattern</b> with a <b>Pattern Type:</b> of
<b>Omnidirectional</b>, <b>Sectoral</b>, <b>Directional</b>, <b>Multibeam</b>,
<b>Isotropic (Omni 3D)</b> or <b>Generic (Multi-Beam)</b> for azimuth, and
<b>Isotropic</b>, <b>Directional</b>, <b>Multibeam</b> or
<b>Generic (Multi-Beam)</b> for elevation. Parameters are the usual ones —
<b>Beamwidth:</b>, <b>Sidelobe Level:</b>, <b>Front-to-Back Ratio:</b>,
<b>Number of Beams:</b>, <b>Separation:</b>, <b>Global Heading:</b>,
<b>Tilt:</b> — plus a beam table with <b>Add Beam</b> / <b>Remove Beam</b> for
the generic types.</li>
<li><b>Visualize</b> draws the table as a polar plot (azimuth clockwise from
north; elevation from -90&deg; to +90&deg;). It needs matplotlib and
numpy.</li>
</ul>

<h3>How an asset is applied today</h3>
<p>Be aware of the current limits — this is what the code does, not what the
engine is capable of:</p>
<ul>
<li>The 360&deg; tab takes <b>frequency</b> and <b>ERP</b> from the selected
asset (falling back to 433 MHz / 10 W when no asset is chosen, and computing
ERP from peak power and gain when the stored ERP is not positive), and
pre-fills the site's <b>Height (m)</b> and <b>Height Mode</b>.</li>
<li>The P2P tab copies frequency and ERP into its own boxes, which you can then
edit.</li>
<li><b>Antenna patterns stored in an asset are not yet sent to the engine.</b>
The engine takes patterns as <code>.az</code>/<code>.el</code> <i>files</i>,
and the plugin currently passes none, so a run is omnidirectional even when the
asset has a pattern. <b>AZ Rotation</b> is passed through and only has an
effect once a pattern is.</li>
<li>Polarization and the statistical fractions stored on an asset are
documentation: the values a run uses come from the <b>ITM Parameters</b> group
in the 360&deg; tab.</li>
</ul>
"""

# ---------------------------------------------------------------------------
# Map Converter
# ---------------------------------------------------------------------------

_MAP_CONVERTER = """
<p>The <b>Map Converter</b> tab builds <code>.abt</code> terrain tiles from a
prioritised stack of layers, independently of any analysis. Use it to pre-build
terrain for an area you will analyse repeatedly, to fuse a high-resolution
local DEM over a global one, or to bake buildings in once.</p>

<h3>Layer stack</h3>
<p><b>Layer Stack (highest priority on top)</b>: the first source that has a
valid sample for a pixel wins, so put your best data at the top and move rows
with <b>&#9650; Up</b> / <b>&#9660; Down</b>. Sources are added with:</p>
<ul>
<li><b>Add Layer</b> — a raster (or vector, for buildings) layer from the
project, chosen in the dropdown beside the button.</li>
<li><b>Add Folder...</b> — a folder of GeoTIFF/DEM files, scanned recursively;
the CRS is detected, or you are asked for it.</li>
<li><b>Add Buildings...</b> — building footprints from a file (FGB, SHP, GPKG,
GeoJSON) or a folder of <code>.fgb</code> parts or an ESRI <code>.gdb</code>.
Anything that is not FlatGeobuf in WGS84 is converted first.</li>
<li><b>Download Buildings...</b> — fetch OpenStreetMap building footprints
for the row's extent through the Overpass API
(<code>overpass-api.de</code>), as a deferred entry that is downloaded when
the conversion runs. Set an extent first — without one the download is
skipped.</li>
<li><b>Inspect .abt...</b> — load a folder of existing <code>.abt</code> tiles
back into QGIS as a terrain layer, under <b>Waveshed &rarr; Terrain</b>. Useful
for checking a build before running an analysis over it.</li>
</ul>
<p>The table shows <b>Type</b>, <b>Source</b>, <b>CRS</b>, <b>Native Res</b>,
<b>Target Res</b> and <b>Extent</b>. Every raster row needs an extent: select a
row and use <b>Draw Extent for Layer</b> (drag a rectangle on the map) or
<b>Extent from Layer</b> (the layer's own bounds). <b>Remove</b> drops a
row.</p>

<h3>Output</h3>
<ul>
<li><b>Output .abt Resolutions</b> — <b>Generate tiles at:</b> with a checkbox
per resolution (<b>2m</b>, <b>5m</b>, <b>10m</b>, <b>30m</b>, <b>90m</b>,
<b>250m</b>; 30 m ticked by default). Each checkbox's tooltip states the tile
extent in degrees and the tile size in pixels. Several may be ticked at
once.</li>
<li><b>Rebuild existing tiles</b> — tiles are normally skipped when a file of
the same name already exists, and tile names encode only position and
resolution, not the source or the plugin version. After changing a source or an
export setting, tick this or you will keep looking at the old pixels.</li>
<li><b>Output:</b> — the target directory; the placeholder is the terrain cache
root. <b>Browse...</b> to pick another.</li>
<li>The estimate line reports <code>.abt tiles: N | Est. disk: X MB (Y GB)</code>
and turns orange past 50 GB and red past 200 GB. It prices only what will
actually be built.</li>
</ul>
<p><b>Convert</b> starts the run, <b>Cancel</b> stops it, the progress bar and
the status line track it, and the converter's own output is streamed into the
log box at the bottom.</p>

<h3>The plan cross-check</h3>
<p>Before every conversion the worker runs <code>aether_converter plan</code>
over the same area and resolutions and compares the engine's tile grid with the
plugin's own enumeration. Any difference — or an engine too old to have the
<code>plan</code> subcommand — aborts the run rather than producing tiles the
two sides disagree about. The tile naming and geometry are the same as Site
Analysis uses, so a folder built here can be handed straight to an analysis as
a terrain directory.</p>

<h3>Difference from Site Analysis terrain</h3>
<p>Both paths share the downloader, the folder scanner, the tile naming and the
per-tile <code>sources[]</code> model. The one deliberate difference is
no-data: Site Analysis fills unknown ground with 0 m (sea level) for the
engine, while the Map Converter's output <b>keeps voids</b>. That is the
contract — its tiles are data, not one run's view of the data. A void tile
inspected in QGIS therefore shows holes rather than sea.</p>
"""

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS = """
<p>The <b>Settings</b> tab is the plugin's configuration; it is saved when you
press <b>Save Settings</b> and also when the dialog closes. Saving fans the new
values out to every open tab immediately, so dropdowns and paths never go
stale.</p>

<h3>Binaries</h3>
<ul>
<li><b>Binary directory:</b> with <b>Browse...</b> — where the three engine
binaries live.</li>
<li><b>Auto-detect</b> — run the discovery order described in
<a href="#install">Installing the engine</a> and fill the field in.</li>
<li><b>Download Binaries</b> — fetch and install the latest engine after EULA
consent.</li>
<li><b>Check for updates</b> — compare the installed engine with the latest
release; the line below shows <b>Installed engine</b> / <b>Latest
release</b>.</li>
<li><b>Check for engine updates when QGIS starts</b> — the once-per-session
background check (on by default).</li>
<li>The status line reads <b>Binaries: Found (3/3)</b> in green or
<b>Binaries: Missing</b> in red, and an orange line below reports platform
prerequisites (for example missing Vulkan drivers on Linux).</li>
</ul>

<h3>API Key</h3>
<p><b>API key:</b> (masked, with <b>Show</b> / <b>Hide</b>), <b>Save</b> —
which validates and stores in one step — <b>Get API Key</b> and
<b>Show machine fingerprint</b>. See <a href="#install">Installing the
engine</a>.</p>

<h3>Defaults</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Setting</th><th>Default</th><th>Meaning</th></tr>
<tr><td><b>Local terrain dir:</b></td><td>(empty)</td><td>A directory of DEM
files, or of pre-built <code>.abt</code> tiles. When set it appears in both
analysis tabs' DEM dropdown as <b>Local: &lt;path&gt;</b>. Selected, it is the
<i>only</i> source — whatever it does not cover is computed over 0 m.</td></tr>
<tr><td><b>Terrain cache:</b></td><td><code>~/.aether/cache</code></td>
<td>Where the tile pool and the per-run views live. <b>Clear...</b> reports how
many entries and how many megabytes would go, then deletes them.</td></tr>
<tr><td><b>Max VRAM budget (GB):</b></td><td>Auto (range 1&ndash;256)</td>
<td>Auto sends nothing and the engine sizes the budget itself (on Apple Silicon
that is the GPU memory macOS allows). A number is sent with coverage jobs as
<code>processing.max_vram_usage_gb</code>. It does <i>not</i> raise the engine's
~3.86 GB single-allocation ceiling for the terrain atlas.</td></tr>
<tr><td><b>Max RAM budget (GB):</b></td><td>Auto (range 1&ndash;256)</td>
<td>Auto sends nothing and the engine chooses. A number is sent with coverage
jobs as <code>processing.max_ram_usage_gb</code>. Values saved by an earlier
version stay as they are; set the box back to Auto to hand the choice to the
engine.</td></tr>
<tr><td><b>Download connections:</b></td><td>256 (range 16&ndash;1024)</td>
<td>Parallel HTTP connections the terrain downloader uses. Lower it on a
connection that objects to the rate; retries automatically use half.</td></tr>
</table>

<h3>Data sources</h3>
<p>The credits for the third-party datasets the plugin can fetch on your
behalf — see <a href="#attribution">Data attribution</a>.</p>

<h3>Stored settings</h3>
<p>Everything is kept in QGIS settings under the <code>waveshed/</code> prefix:
<code>binary_dir</code>, <code>installed_engine_version</code>,
<code>api_key</code>, <code>terrain_dir</code>, <code>cache_dir</code>,
<code>max_ram_gb</code>, <code>max_vram_gb</code>,
<code>download_connections</code> and the main dialog's geometry. Two more keys
exist without a control in this dialog: <code>assets_dir</code> (where emitter
JSON files live, default <code>~/.aether/assets</code>) and
<code>download_max_passes</code> (terrain download attempts including retries,
default 4; set it to 1 to disable retries). Binary discovery also honours the
<code>AETHER_BIN_DIR</code> environment variable.</p>
"""

# ---------------------------------------------------------------------------
# Altitude explorer
# ---------------------------------------------------------------------------

_ALTITUDE_EXPLORER = """
<p>A <b>LOS Floor</b> run answers coverage at every altitude at once. The
<b>Altitude Explorer</b> — the dock opened from <b>Plugins &rarr; Waveshed
&rarr; Altitude Explorer</b>, from the toolbar, or from the prompt after a LOS
Floor run — is how you read that answer. It only re-styles layers, so every
change is instant: nothing is recomputed.</p>

<h3>What the raster holds</h3>
<p>One 16-bit value per pixel: the lowest altitude at which that location first
gains line of sight to the transmitter, in counts of <b>0.5 m</b>. The value
<b>65535</b> means "never visible, at any altitude" and is the no-data
value.</p>

<h3>Layers</h3>
<p>The <b>Layers</b> group lists every LOS Floor raster in the project with a
tick box, and <b>Select all</b> / <b>Select none</b> / <b>Refresh</b>. Whatever
is ticked is what the controls below drive; several sites can be driven
together.</p>

<h3>Altitude bands, each with its own reference</h3>
<p>The <b>Altitudes</b> group holds a list of bands. <b>Add altitude</b> adds
one, <b>Remove</b> drops the selected one, the <b>Colour:</b> button sets its
colour, and the slider and spin box set the altitude of the band currently
selected. Untick a band to hide it without losing it — the bands above keep
their own areas rather than inheriting its.</p>
<p><b>Measured from:</b> (<b>Ground (AGL)</b> / <b>Sea level (AMSL)</b>) is a
property of a band, not of the whole dock: it applies to the band selected in
the list and to the next band you add. So a single view can carry
<b>100 m AGL</b> and <b>2500 m AMSL</b> at the same time — a terrain-following
drone and an aircraft holding a flight level, side by side. The suffix in the
spin box and the band's label always state which reference that band uses.</p>
<p>The two numbers are deliberately not interchangeable: 100 m AGL is a
sensible drone height, while 100 m AMSL is underground through most of the
Alps.</p>

<h3>Sea-level twins</h3>
<p>The engine emits the surface above <i>ground</i>. An AMSL band needs the
same surface above <i>sea level</i>, which is the AGL value plus the terrain
under each pixel. The first time a band asks for AMSL, the plugin builds a
cached <b>sea-level twin</b> of each selected layer:</p>
<ul>
<li>The terrain used is <b>the run's own terrain</b>, found through the
<code>_job.json</code> written beside the result — so it matches the analysis
to the metre and you are not asked to re-identify a DEM. If that terrain is
gone, the <b>Elevation data needed</b> dialog asks for a DEM layer or a file
once, and the answer is reused for the session.</li>
<li>The twin keeps the identical encoding (0.5 m counts, same sentinel), so
every tool — bands, shading, contours, the best-site merge — drives it exactly
like the original. It is loaded under <b>Waveshed &rarr; Above Sea Level</b>
and stamped with the terrain it was built from, so a stale twin is rebuilt
rather than reused.</li>
<li>Progress is shown as <b>Sea-level altitudes</b> and can be cancelled.
Pixels that are reachable but have no terrain underneath are left blank — an
honest hole beats an invented sea-level altitude — and you are told how many
there were.</li>
<li>Ground below sea level is clamped to 0 m, because the encoding cannot
express a negative altitude.</li>
</ul>

<h3>Colouring</h3>
<ul>
<li><b>Limit to these altitudes</b> — on, the map shows only what your bands
reach; off, the whole layer is drawn with its continuous ramp.</li>
<li><b>Colour by: Altitude band</b> — one colour per band. Coverage at altitude
A always contains coverage at every lower altitude, so the bands draw as nested
rings: the lowest band owns everything up to its altitude, each higher one owns
only what the one below did not reach, and above the highest band nothing is
drawn.</li>
<li><b>Colour by: Required altitude</b> — one continuous ramp up to the highest
band, colouring each pixel by the altitude it actually needs (blue = low,
red = high). This is the "required altitude" ramp: a gradient of how far you
would have to climb, location by location.</li>
<li><b>Reset to full range</b> restores the default continuous styling on the
selected layers.</li>
</ul>

<h3>Tools</h3>
<ul>
<li><b>Merge selected &rarr; best site</b> — across two or more selected
layers, compute per pixel the <i>lowest</i> required altitude and which site
provides it. You get a combined LOS Floor raster (drivable by the same bands)
plus a categorical best-site map, both under <b>Waveshed &rarr; Best Site</b>.
Ties go to the earlier site; pixels no site reaches are no-data. Inputs are
resampled onto a shared grid with nearest-neighbour, so the 0.5 m quantisation
and the sentinel survive. A merge grid larger than 400 million pixels is
refused with an explanation — usually it means the sites are far apart or the
resolutions do not match.</li>
<li><b>Iso-altitude contours...</b> — draw labelled lines of equal required
altitude (the 50 m / 100 m "reach" lines) for each selected layer, into a
GeoPackage with an <code>alt_m</code> attribute, loaded under
<b>Waveshed &rarr; Contours</b>. You choose the interval; very large rasters
are averaged down to a pixel budget before tracing, which is reported.</li>
</ul>
<p>Both tools run in the background and can be cancelled; a cancelled run
leaves no partial output.</p>
"""

# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

_PROCESSING = """
<p>The plugin registers a <b>Waveshed</b> provider ("Waveshed RF Propagation")
in the Processing toolbox, group <b>Analysis</b>, with two algorithms. They run
the same pipeline as the dialogs and are scriptable, batchable and usable
inside models — but they show no dialogs, so every check they perform is a hard
error instead of a question.</p>

<h3>Site Analysis (Coverage) — <code>waveshed:coverage</code></h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Parameter</th><th>Type / values</th><th>Default</th></tr>
<tr><td><b>DEM Layer</b></td><td>Raster layer</td><td>&mdash;</td></tr>
<tr><td><b>TX Latitude</b></td><td>-90 &ndash; 90</td><td>0</td></tr>
<tr><td><b>TX Longitude</b></td><td>-180 &ndash; 180</td><td>0</td></tr>
<tr><td><b>TX Height (m AGL)</b></td><td>1 &ndash; 10000; AGL only</td>
<td>30</td></tr>
<tr><td><b>Frequency (MHz)</b></td><td>1 &ndash; 3000</td><td>433</td></tr>
<tr><td><b>ERP (Watts)</b></td><td>0.001 &ndash; 1000000</td><td>10</td></tr>
<tr><td><b>Propagation Model</b></td><td><b>LOS</b>, <b>ITM</b>,
<b>MIN_ALT</b></td><td>LOS</td></tr>
<tr><td><b>Resolution (m)</b></td><td>2, 5, 10, 30, 90, 250</td><td>10</td></tr>
<tr><td><b>Max Range (km)</b></td><td>1 &ndash; 500</td><td>50</td></tr>
<tr><td><b>Compute Backend</b></td><td>AUTO, GPU, CPU</td><td>AUTO</td></tr>
<tr><td><b>Output Directory</b></td><td>Folder</td><td>&mdash;</td></tr>
</table>
<p>It prepares terrain, runs <code>aether_core</code>, exports the GeoTIFF and
adds it to <b>Waveshed &rarr; Coverage</b>. <b>MIN_ALT</b> is the LOS Floor
model. ITM parameters are not exposed here and take the engine defaults
(&epsilon; 15, &sigma; 0.005 S/m, N 301, climate 5, horizontal polarization,
confidence and reliability 0.50, FOUR_THIRDS).</p>

<h3>Point-to-Point Link Analysis — <code>waveshed:p2p</code></h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Parameter</th><th>Type / values</th><th>Default</th></tr>
<tr><td><b>DEM Layer</b></td><td>Raster layer</td><td>&mdash;</td></tr>
<tr><td><b>Batch CSV File</b></td><td>Optional CSV — see
<a href="#p2p">Batch CSV</a></td><td>(none)</td></tr>
<tr><td><b>TX Latitude</b> / <b>TX Longitude</b></td><td>Coordinates</td>
<td>0 / 0</td></tr>
<tr><td><b>TX Height (m AGL)</b></td><td>1 &ndash; 10000; AGL only</td>
<td>30</td></tr>
<tr><td><b>RX Latitude</b> / <b>RX Longitude</b></td><td>Coordinates</td>
<td>0 / 0</td></tr>
<tr><td><b>RX Height (m AGL)</b></td><td>1 &ndash; 10000; AGL only</td>
<td>1.5</td></tr>
<tr><td><b>Propagation Model</b></td><td><b>LOS</b>, <b>ITM</b></td>
<td>LOS</td></tr>
<tr><td><b>Resolution (m)</b></td><td>2, 5, 10, 30, 90, 250</td><td>10</td></tr>
<tr><td><b>Compute Backend</b></td><td>AUTO, GPU, CPU</td><td>AUTO</td></tr>
<tr><td><b>Output Directory</b></td><td>Folder</td><td>&mdash;</td></tr>
</table>
<p>When a <b>Batch CSV File</b> is given the TX/RX coordinate parameters are
ignored entirely and the CSV drives every link; the CSV rows are then the only
thing validated (line by line, including the AGL floor). Without one, a
temporary two-line CSV is written from the parameters. The algorithm returns the
path of the result CSV. Frequency and ERP are fixed at 433 MHz / 10 W on this
path.</p>

<h3>Things to know</h3>
<ul>
<li><b>The dropdowns are index-based.</b> Removing <code>SIMPLE_LOSS</code> in
0.2.0 shifted every index after it, so a saved model or script that selected
<b>ITM</b> or <b>MIN_ALT</b> by index must re-pick it.</li>
<li>Both algorithms need a valid <b>API key</b> and the engine binaries; a
missing key is a Processing error, not a dialog.</li>
<li>Both push every engine line into the Processing log, and terrain
acquisition drives the first 10&ndash;20 % of the progress bar.</li>
<li>Heights here are always AGL — there is no mode parameter, which is why the
1 m floor can be a parameter bound.</li>
</ul>
"""

# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------

_PHYSICS = """
<h3>What "line of sight" means here</h3>
<p>A location is visible when a straight ray from the transmitting antenna to
the receiving point clears every terrain sample along the way — over the
<i>effective</i> earth, not the geometric one. The surface tested is the
<code>.abt</code> terrain, so it includes buildings when you asked for them and
excludes vegetation always. There is no Fresnel-clearance requirement in the
LOS answer: it is a geometric yes/no, which is why a path can be "visible" and
still be a poor radio link. The P2P plots show the 0.6 F1 clearance band for
exactly that reason.</p>
<p>Terrain is quantised to 0.5 m and sampled at the analysis resolution, so a
grazing path is decided at that grain — one reason antennas below 1 m AGL are
rejected (<a href="#heights">details</a>).</p>

<h3>Earth curvature and the 4/3 earth</h3>
<p>The atmosphere's refractive index decreases with height, so radio rays bend
slightly downwards and reach further than optical straight lines. The standard
way to model that is to keep the rays straight and inflate the earth's radius by
<b>k = 4/3</b>: R<sub>eff</sub> = 4/3 &times; 6371 km &asymp;
<b>8495 km</b>.</p>
<p>How far the surface falls away below the straight line from your antenna is
the <i>bulge</i>, h &asymp; D&sup2; / (2 k R):</p>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Distance</th><th>Bulge below the tangent line (k = 4/3)</th></tr>
<tr><td>50 km</td><td>~0.15 km</td></tr>
<tr><td>100 km</td><td>~0.6 km</td></tr>
<tr><td>200 km</td><td>~2.4 km</td></tr>
<tr><td>300 km</td><td>~5.3 km</td></tr>
</table>
<p>The smooth-earth horizon of a platform at height h is
d &asymp; &radic;(2 k R h): a <b>5000 m AMSL</b> aircraft sees about
<b>291 km</b> over a smooth earth. So at 300 km an airborne receiver is already
past its own smooth-earth horizon, and whether it sees the transmitter depends
entirely on the terrain in between.</p>
<p><b>This is why distant massifs shadow far-field coverage.</b> At 300 km the
line of sight is more than 5 km above the local ground at mid-path; a 4000 m
range 150 km away still sits above it and cuts a shadow across everything
behind. A LOS Floor map showing large unreachable areas beyond a mountain
range, or requiring thousands of metres of altitude behind one, is correct
physics — not an AGL/AMSL mix-up and not a terrain bug. A quick sanity check:
switch the Altitude Explorer band to AMSL and compare the required altitude
against the blocking ridge's elevation; they should be consistent.</p>
<p><b>ADVANCED</b> earth-radius mode replaces the fixed 4/3 with a per-path
k computed from the refractivity and the endpoint altitudes (Sandia/Doerry),
clamped between 0.1 and 10 — useful for high platforms or unusual atmospheres,
but not the default because 4/3 is what published link budgets assume.</p>

<h3>Free space, and why loss is never better than it</h3>
<p>Free-space path loss is the floor of any propagation result: no terrain
model can return less loss than an unobstructed path through vacuum. ITM adds
diffraction, tropospheric scatter and ground-reflection effects on top of that.
Inside 100 m the engine substitutes free-space loss, because ITM is undefined
there.</p>

<h3>Units</h3>
<p>Power is in dBm (dB relative to 1 mW): 0 dBm = 1 mW, 30 dBm = 1 W, and
10 W ERP = 40 dBm. Path loss is in dB. Received signal is transmitter power
(ERP expressed in dBm) minus path loss, which is exactly how the P2P plot's
signal curve is built.</p>
"""

# ---------------------------------------------------------------------------
# ITM parameters
# ---------------------------------------------------------------------------

_ITM = """
<p>ITM (the Irregular Terrain Model, "Longley-Rice") predicts median
transmission loss over irregular terrain from the terrain profile plus a handful
of electrical and statistical parameters. These are the controls in the
<b>ITM Parameters</b> group of the 360&deg; tab; the engine defaults are used
wherever the parameter is not exposed.</p>

<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Control</th><th>Job field</th><th>Default</th><th>Meaning</th></tr>
<tr><td><b>Ground Permittivity:</b></td><td><code>eps_dielect</code></td>
<td>15.0</td><td>Relative permittivity of the ground. 15 is average ground;
roughly 4&ndash;5 for poor/dry ground or granite, 25&ndash;30 for wet ground,
81 for fresh water and sea water. Together with the conductivity it sets the
complex surface impedance, which drives the ground-reflection term — it matters
most at low antenna heights and low frequencies.</td></tr>
<tr><td><b>Ground Conductivity:</b></td><td><code>sgm_conductivity</code></td>
<td>0.005</td><td>Ground conductivity in siemens per metre. 0.005 is average
ground, ~0.001 poor/dry, ~0.02 wet ground, 0.01 fresh water and <b>5.0</b> sea
water.</td></tr>
<tr><td><b>Surface Refractivity:</b></td><td><code>eno_ns_surfref</code></td>
<td>301.0</td><td>Sea-level surface refractivity N<sub>0</sub> in N-units,
valid 250&ndash;400. 301 is the global average; ~360 for humid coastal air,
~280&ndash;300 for dry continental air. It sets the effective earth curvature
in FOUR_THIRDS mode (N = 301 gives k &asymp; 1.333) and feeds the ADVANCED
k-factor.</td></tr>
<tr><td><b>Radio Climate:</b></td><td><code>radio_climate</code></td>
<td>Continental Temperate (5)</td><td>The climate zone whose statistics the
variability model uses: <b>Equatorial (1)</b>,
<b>Continental Subtropical (2)</b>, <b>Maritime Subtropical (3)</b>,
<b>Desert (4)</b>, <b>Continental Temperate (5)</b>,
<b>Maritime Temperate (6)</b>. (ITM also defines 7, maritime temperate over
sea; the engine accepts 1&ndash;7 and substitutes 5 for anything else.)</td></tr>
<tr><td><b>Polarization:</b></td><td><code>pol</code></td>
<td>Horizontal (0)</td><td><b>Horizontal (0)</b> or <b>Vertical (1)</b>.
Affects the ground-reflection coefficient; vertical polarization typically
propagates a little better over conductive ground at low frequencies.</td></tr>
<tr><td><b>Confidence:</b></td><td><code>conf</code></td><td>0.50</td>
<td>The confidence quantile (fraction of <i>situations</i>): the share of
comparable locations for which the predicted loss is not exceeded. 0.50 is the
median; 0.90 is a conservative planning value that predicts a weaker
signal.</td></tr>
<tr><td><b>Reliability:</b></td><td><code>rel</code></td><td>0.50</td>
<td>The reliability quantile (fraction of <i>time</i>): the share of time the
prediction holds. Same idea, applied to temporal variability. Both are
validated for 0.01&ndash;0.99.</td></tr>
<tr><td><b>Earth Radius Mode:</b></td><td><code>earth_radius_mode</code></td>
<td>FOUR_THIRDS</td><td>See <a href="#models">Modes and models</a> and
<a href="#physics">Physics notes</a>.</td></tr>
</table>

<h3>Validity — what the model is defined for</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Quantity</th><th>Validated range</th><th>Hard range</th></tr>
<tr><td>Antenna height (AGL)</td><td>1 &ndash; 1000 m</td>
<td>0.5 m and below is "probably invalid"; stock ITM documents 3000 m as its
upper limit, the engine follows Splat NG's relaxed 12&nbsp;000 m</td></tr>
<tr><td>Frequency</td><td>40 MHz &ndash; 10 GHz</td>
<td>20 MHz &ndash; 20 GHz</td></tr>
<tr><td>Path length</td><td>1 &ndash; 1000 km</td><td>1 &ndash; 2000 km</td></tr>
<tr><td>Surface refractivity (height-reduced)</td><td colspan="2">250 &ndash;
400 N-units</td></tr>
<tr><td>Confidence / reliability</td><td colspan="2">0.01 &ndash; 0.99</td></tr>
</table>

<h3>Two consequences worth knowing</h3>
<ul>
<li><b>Every coverage map contains out-of-range ground.</b> ITM is undefined
below 1 km path length, and a map always includes the disc around the
transmitter — so the innermost kilometre is out of range on every azimuth. The
engine substitutes free-space loss below 100 m. The plugin states this as a
CAUTION rather than hiding it.</li>
<li><b>Altitude walks refractivity out of range.</b> ITM reduces the surface
value along each path as
ens = N<sub>0</sub> &middot; exp(-z<sub>sys</sub>/9460), where
z<sub>sys</sub> is the mean terrain height of the profile. With the default
N<sub>0</sub> = 301 the reduced value drops through 250 at about
<b>1760 m AMSL</b> mean terrain height — so a textbook sea-level default puts
an alpine scene out of range. Raise N<sub>0</sub> for high-altitude
scenes.</li>
</ul>

<h3>How violations are reported</h3>
<p>ITM raises an internal indicator (<code>kwx</code>) for these conditions, but
neither the engine nor the original SPLAT! surfaces it in map mode — an
out-of-range run otherwise looks exactly like a valid one. So:</p>
<ul>
<li>The plugin checks the parameters before the run and writes every finding to
the log as <code>model validity: [CAUTION|INVALID] …</code>.</li>
<li>Anything marked <b>INVALID</b> raises <b>Outside the propagation model's
range</b> in the dialog, listing each problem; the run completes if you
insist, but the numbers are probably invalid.</li>
<li>The engine repeats the same check and prints <code>[Warn]</code> lines. It
only warns — the sole value it actually changes is a sub-minimum AGL antenna
height, which it clamps to 1 m.</li>
</ul>
"""

# ---------------------------------------------------------------------------
# Troubleshooting
# ---------------------------------------------------------------------------

_TROUBLESHOOTING = """
<p>The QGIS <b>Log Messages</b> panel (tags <b>Waveshed</b> and
<b>Waveshed-Terrain</b>) carries the engine's own words and the terrain
decisions. Start there.</p>

<h3>"Aether engine not found"</h3>
<p>One or more of <code>aether_core</code>, <code>aether_converter</code>,
<code>aether_export</code> could not be located. The message lists every place
that was searched. Fix it in <b>Settings</b>: <b>Download Binaries</b>, or
point <b>Binary directory:</b> at an existing install, or press
<b>Auto-detect</b>. Remember all three must be in the <i>same</i> directory —
a partial build directory is skipped and something else on
<code>PATH</code> may win instead. The check runs before terrain preparation
on purpose, so you do not pay for a long extract first.</p>

<h3>"This Aether engine build does not support fingerprint reporting"</h3>
<p><code>aether_core --fingerprint</code> needs engine <b>v0.4.2 or newer</b>.
Update via <b>Settings &rarr; Download Binaries</b> and try again. If instead
you get the engine's own <code>[E:…]</code> text, the flag was understood and
the fingerprint computation itself failed — that message is the actionable
one.</p>

<h3>"Unsupported resolution 90m. Allowed: [2.0, 5.0, 10.0, 30.0]"</h3>
<p>The plugin runs <code>aether_core --version</code> at the start of every analysis, logs the engine path and version under the Waveshed tag, and refuses a 90 m or 250 m job on an engine older than <b>0.4.3</b> (or one too old to know the flag) <i>before</i> any terrain is downloaded. If you still see the engine line below, the run reached an older engine another way.</p>
<p>Older engine builds carried a hardcoded resolution list on
the native GPU path, contradicting both the engine contract (any positive
resolution) and the plugin's offer of 90 m and 250 m. The list is gone in newer
builds: <b>update the engine</b> via <b>Settings &rarr; Download Binaries</b>,
or pick 2, 5, 10 or 30 m for this run. It is not a plugin setting, and the
plugin appends exactly this hint to the engine's error.</p>

<h3>API key problems</h3>
<ul>
<li><b>"API key required to run the Aether engine"</b> — nothing is stored.
Paste the key in <b>Settings &rarr; API key:</b> and press <b>Save</b>.</li>
<li><b>"not valid Base58 text"</b> — characters were mangled in copying. Copy
the key again; internal whitespace from a wrapped terminal line is handled
automatically, but a substituted character is not.</li>
<li><b>"decoded to N bytes, expected 84 or 116"</b> — the key is truncated or
is not a Waveshed key.</li>
<li>The plugin only checks the key's structure. A structurally valid key that
the engine rejects (wrong signature, expired, locked to another machine) fails
inside <code>aether_core</code> — for a machine-locked key, check the
fingerprint you registered matches <b>Show machine fingerprint</b> on this
computer.</li>
</ul>

<h3>Terrain downloads and no-data</h3>
<ul>
<li><b>Everything is flat / the result covers the sea.</b> The requested area
reached past the source's coverage, and missing ground is assumed 0 m. Look for
the <code>NO-DATA (HTTP 404)</code> statistics line or the "assumed 0 m (sea
level)" warning, and use a source that covers the full range.</li>
<li><b>Terrain near -32000 m.</b> The XYZ encoding was guessed wrong: a Mapbox
Terrain-RGB service decoded as Terrarium. Set the layer's interpretation
explicitly (XYZ connection dialog, or <code>interpretation=mapboxterrain</code>
in the layer URI) and clear the cached tiles for it.</li>
<li><b>"Terrain download failed … rendering its tiles through QGIS is not a
fallback".</b> The engine's own diagnosis follows the message — often a codec
problem (a service serving WebP to a PNG decoder) or a dead network, not
something a plugin setting can fix.</li>
<li><b>The analysis ran over a flat sea at fine resolution.</b> The layer's
<b>Max. Zoom Level</b> claimed more than the service publishes, so every tile
404'd. The plugin clamps this and logs the reason; fix the layer to silence
it.</li>
<li><b>A rendered service dropped part of its answer.</b> You get a warning
that empty ground appeared <i>inside</i> the layer's own published extent —
a partial answer, not a coverage edge. Re-run (often transient), lower
<b>Download connections:</b>, or use a different source; without re-running,
that block is sea level.</li>
<li><b>Stale or half-built tiles.</b> Tiles written short are flagged and
rebuilt automatically, but tile names encode only position and resolution — so
after changing a source, use <b>Clear...</b> beside <b>Terrain cache:</b> (or
<b>Rebuild existing tiles</b> in the Map Converter).</li>
</ul>

<h3>Runs that stop early</h3>
<ul>
<li><b>A failing job aborts the whole run.</b> The 360&deg; tab runs its jobs
in sequence in one worker; if the first site fails, the remaining sites and
altitudes are not attempted and you get one <b>Analysis Failed</b> dialog.
Fix the cause and re-run — cached terrain means the retry is much faster.</li>
<li><b>"Incomplete terrain coverage"</b> — the selected local terrain directory
does not span the analysis area, was built at a different resolution, or lacks
the buildings you asked for. Anything it misses is computed over 0 m.</li>
<li><b>The job is rejected with a VRAM message.</b> The engine loads every tile
a wedge touches into one contiguous allocation capped near 3.86 GB. Raising
<b>Max VRAM budget (GB):</b> does not help — reduce the range, narrow the
azimuth sector, or use a coarser resolution.</li>
<li><b>Nothing happens on the GPU.</b> Set <b>Backend:</b> to <b>CPU</b> to
confirm it is a driver issue. On Linux, install Vulkan
(<code>libvulkan1</code>, <code>mesa-vulkan-drivers</code>) — Settings reports
when <code>vulkaninfo</code> is missing.</li>
</ul>

<h3>Other</h3>
<ul>
<li><b>"Missing Dependency: matplotlib"</b> — the P2P plots and the polar
pattern viewer need matplotlib (and numpy for the viewer). Everything else
works without them.</li>
<li><b>A layer will not appear in the DEM dropdown.</b> The plugin's own
coverage and P2P results are filtered out deliberately; terrain it generated is
kept. Reload the layer, or use <b>Local terrain dir:</b>.</li>
<li><b>"Not a DEM?"</b> — the layer classifies as imagery. Say no and pick an
elevation source; a rendered basemap really is refused later.</li>
<li><b>Results look shifted by a pixel after an update.</b> The cache schema is
bumped when tile geometry or sampling changes, so old tiles are rebuilt rather
than re-served; this is expected once per schema change.</li>
<li><b>A saved Processing model picks the wrong model.</b> The enums are
index-based and <code>SIMPLE_LOSS</code> was removed — re-pick the value.</li>
</ul>
"""

# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

_GLOSSARY = """
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Term</th><th>Meaning</th></tr>
<tr><td><b>.abt</b></td><td>The Aether terrain tile format: a small header plus
16-bit elevations in 0.5 m steps, one file per fixed geographic square. The only
terrain the engine reads.</td></tr>
<tr><td><b>AGL</b></td><td>Above Ground Level — height above the ground
directly below.</td></tr>
<tr><td><b>AMSL</b></td><td>Above Mean Sea Level — absolute elevation.</td></tr>
<tr><td><b>Azimuth</b></td><td>Compass bearing in degrees clockwise from north.
<b>AZ Start</b>/<b>AZ End</b> bound the computed sector; <b>AZ Rotation</b>
turns the antenna pattern.</td></tr>
<tr><td><b>Backend</b></td><td>Where the engine computes: AUTO, GPU or
CPU.</td></tr>
<tr><td><b>Batch P2P</b></td><td>A task that computes the full cross product of
the S (source) and R (receiver) rows of a CSV.</td></tr>
<tr><td><b>dBm</b></td><td>Power in dB relative to 1 mW. 30 dBm = 1 W.</td></tr>
<tr><td><b>DEM</b></td><td>Digital Elevation Model — the raster elevation
source a run is built from.</td></tr>
<tr><td><b>EIRP</b></td><td>Effective Isotropic Radiated Power — power referred
to an isotropic radiator.</td></tr>
<tr><td><b>ERP</b></td><td>Effective Radiated Power — power referred to a
half-wave dipole, i.e. EIRP minus 2.15 dB. The engine's
<code>erp_watts</code>.</td></tr>
<tr><td><b>Fresnel zone</b></td><td>The ellipsoid around a radio path whose
clearance governs diffraction loss. Keeping terrain outside <b>0.6 &times;</b>
the first zone (F1) makes a path behave as if unobstructed.</td></tr>
<tr><td><b>ITM</b></td><td>Irregular Terrain Model, also called Longley-Rice: a
terrain-dependent median transmission loss model.</td></tr>
<tr><td><b>k-factor</b></td><td>Effective-earth-radius multiplier modelling
atmospheric refraction; k = 4/3 is the standard value.</td></tr>
<tr><td><b>kwx</b></td><td>ITM's internal indicator that a parameter left the
model's validated range. Not surfaced by the engine in map mode, which is why
the plugin checks the same conditions itself.</td></tr>
<tr><td><b>LOS</b></td><td>Line of Sight — geometric visibility over the
terrain surface, on an effective-earth curvature.</td></tr>
<tr><td><b>LOS Floor</b></td><td>The lowest altitude at which a location first
gains line of sight to the transmitter. The engine calls the model
<code>MIN_ALT</code>.</td></tr>
<tr><td><b>N-units</b></td><td>Units of atmospheric refractivity; the surface
value N<sub>0</sub> is typically 250&ndash;400, default 301.</td></tr>
<tr><td><b>Pool / view</b></td><td>The terrain cache: the <i>pool</i> holds
every tile built for a source, a <i>view</i> is the exact tile set one run
hands the engine, linked from the pool.</td></tr>
<tr><td><b>Sentinel</b></td><td>The value 65535 in a LOS Floor raster: never
visible, at any altitude.</td></tr>
<tr><td><b>Terrarium / Terrain-RGB</b></td><td>The two RGB encodings of
elevation in web tiles. They look identical as pixels; decoding one as the other
puts terrain near -32000 m.</td></tr>
<tr><td><b>Twin (sea-level twin)</b></td><td>A copy of a LOS Floor raster with
the terrain added, so its values are AMSL instead of AGL.</td></tr>
<tr><td><b>Void</b></td><td>A terrain sample no source covered. Kept as void by
the Map Converter; filled to 0 m in the view Site Analysis hands the
engine.</td></tr>
<tr><td><b>Wedge</b></td><td>One angular slice of a 360&deg; computation. The
engine's progress reads <code>Wedge 120/360</code>, which drives the plugin's
progress bar.</td></tr>
<tr><td><b>XYZ tiles</b></td><td>Slippy-map tiles addressed by zoom/column/row.
Elevation services publish them RGB-encoded.</td></tr>
</table>
"""

# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

_ATTRIBUTION = """
<h3>The plugin</h3>
<p>Waveshed is free software under the <b>GNU General Public License, version 2
or later</b>. Copyright &copy; 2026 Gian-Andrea Heinrich. It comes with no
warranty; see the <code>LICENSE</code> file for the full text. Source and issue
tracker:
<a href="https://github.com/gian1312/waveshed-qgis">github.com/gian1312/waveshed-qgis</a>.</p>

<h3>The Aether engine</h3>
<p>The engine binaries are <b>not part of this program</b>. They are separate,
proprietary, closed-source software distributed by Waveshed under their own End
User License Agreement, shown for acceptance before every download. The GPL
does not apply to them, and running them needs an API key from
<a href="https://waveshed.io">waveshed.io</a>.</p>
<p>The engine EULA grants <b>non-commercial use only</b> (private/personal,
hobby, and education or research by individuals). <b>Commercial, governmental
and organisational use</b> — companies, public bodies, NGOs, associations and
other organisations — requires the author's prior written permission
(info@waveshed.io).</p>

<h3>Model output, no liability</h3>
<p>Results are outputs of a radio propagation <b>model</b>: approximations, not
measurements, and not a guarantee of real-world coverage or link performance.
Do not rely on them for safety-of-life, regulatory, planning or financial
decisions without independent verification. To the maximum extent permitted by
law, the author accepts no liability for anything arising from their use. (For
the plugin itself this is an informational notice; it adds no restriction to
the GPL.)</p>

<h3>Data the plugin downloads on your behalf</h3>
<p>These datasets keep their own licences; several require attribution wherever
the data — or a map derived from it — is shown. The same list is in
<b>Settings &rarr; Data sources</b>.</p>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Source</th><th>Credit</th><th>Licence</th></tr>
<tr><td>Mapzen / Terrarium terrain tiles</td>
<td>&copy; Mapzen, &copy; OpenStreetMap contributors, and the source agencies
listed by Mapzen (incl. SRTM, GMTED, NED, and national datasets)</td>
<td>ODbL / mixed public-domain sources</td></tr>
<tr><td>OpenFreeMap building footprints</td>
<td>&copy; OpenStreetMap contributors, &copy; OpenMapTiles,
&copy; OpenFreeMap</td><td>ODbL</td></tr>
<tr><td>OpenStreetMap via the Overpass API (Map Converter's
<b>Download Buildings...</b>)</td>
<td>&copy; OpenStreetMap contributors</td><td>ODbL</td></tr>
<tr><td>Copernicus DEM</td><td>Contains modified Copernicus data</td>
<td>Copernicus licence</td></tr>
<tr><td>swissALTI3D / swissALTIRegio</td><td>&copy; swisstopo</td>
<td>Swiss Open Government Data</td></tr>
</table>
<p>Further detail:
<a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">Mapzen
attribution</a>, <a href="https://openfreemap.org">openfreemap.org</a>,
<a href="https://spacedata.copernicus.eu">spacedata.copernicus.eu</a>,
<a href="https://www.swisstopo.admin.ch">swisstopo.admin.ch</a>.</p>
<p>Any elevation or building data you add yourself stays under whatever licence
you obtained it under; the plugin neither adds nor removes obligations
there.</p>
"""


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

TOPICS: List[Topic] = [
    Topic("overview", "Overview and workflow", _OVERVIEW,
          ("start", "getting started", "menu", "toolbar", "first run")),
    Topic("install", "Installing the engine, licence and API key", _INSTALL,
          ("download", "binaries", "eula", "fingerprint", "update",
           "licence", "license", "aether_core")),
    Topic("models", "Analysis modes and propagation models", _MODELS,
          ("LOS", "ITM", "MIN_ALT", "loss", "mode", "earth radius")),
    Topic("heights", "Heights: AGL and AMSL", _HEIGHTS,
          ("altitude", "floor", "minimum", "antenna height")),
    Topic("terrain", "Terrain: sources, tiles and the cache", _TERRAIN,
          ("DEM", "XYZ", "WMS", "WMTS", "ArcGIS", "abt", "cache", "pool",
           "buildings", "no-data", "sea level", "resolution")),
    Topic("site-analysis", "360° Site Analysis", _SITE_ANALYSIS,
          ("coverage", "sites", "altitudes", "range", "azimuth", "run")),
    Topic("results", "Result layers and symbology", _RESULTS,
          ("legend", "colour", "color", "layers", "output files")),
    Topic("p2p", "P2P links and batch CSV", _P2P,
          ("point to point", "link", "fresnel", "profile", "csv", "batch")),
    Topic("assets", "Assets: emitters and antenna patterns", _ASSETS,
          ("ERP", "EIRP", "gain", "pattern", "az", "el", "emitter")),
    Topic("map-converter", "Map Converter", _MAP_CONVERTER,
          ("abt", "ingest", "tiles", "stack", "plan")),
    Topic("settings", "Settings", _SETTINGS,
          ("cache", "RAM", "VRAM", "connections", "binary directory")),
    Topic("altitude-explorer", "LOS Floor and the Altitude Explorer",
          _ALTITUDE_EXPLORER,
          ("bands", "AMSL", "twin", "contours", "best site", "merge",
           "required altitude")),
    Topic("processing", "Processing algorithms", _PROCESSING,
          ("toolbox", "model builder", "batch", "script")),
    Topic("physics", "Physics notes", _PHYSICS,
          ("curvature", "k-factor", "horizon", "bulge", "free space",
           "shadow")),
    Topic("itm", "ITM parameters and validity", _ITM,
          ("permittivity", "conductivity", "refractivity", "climate",
           "polarization", "confidence", "reliability", "kwx")),
    Topic("troubleshooting", "Troubleshooting", _TROUBLESHOOTING,
          ("error", "failed", "problem", "not found", "unsupported")),
    Topic("glossary", "Glossary", _GLOSSARY,
          ("terms", "abbreviations", "definitions")),
    Topic("attribution", "Licences and data attribution", _ATTRIBUTION,
          ("GPL", "EULA", "ODbL", "credits", "copyright")),
]
