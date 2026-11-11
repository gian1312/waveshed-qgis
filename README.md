# Waveshed

**RF propagation analysis for QGIS — coverage and point-to-point — powered by the Aether GPU engine.**

Waveshed is a QGIS plugin for radio-frequency propagation modelling. It adds:

- **Site Analysis** — area coverage / signal-strength maps from a transmitter.
- **Point-to-Point (P2P)** — link-budget analysis between two sites, single or batch.
- Propagation models: **LOS** and **ITM (Longley-Rice)**; Site Analysis also
  computes the **LOS Floor** (the lowest altitude at which each location sees
  the transmitter).

All computation runs on the **Aether engine**, a GPU-accelerated propagation core.
The plugin itself is a pure-Python wrapper: it prepares terrain, builds job
configurations, invokes the engine, and loads results back into QGIS.

## Installation

### From the official QGIS plugin repository (recommended)

Waveshed is listed in the official QGIS plugin repository:
<https://plugins.qgis.org/plugins/waveshed/>. It is not marked experimental,
so no setting has to change.

1. In QGIS, open **Plugins → Manage and Install Plugins…**.
2. On the **All** tab, search for **Waveshed** and click **Install Plugin**.

QGIS offers every new version as an update from there. Requires QGIS 3.28 or
newer (QGIS 4 included).

### Other ways to install

- **From a ZIP** (for computers without internet access, no automatic
  updates): download the latest `waveshed.<version>.zip` from
  <https://waveshed.io/downloads>, then in QGIS open
  **Plugins → Manage and Install Plugins… → Install from ZIP**, select the
  file and click **Install Plugin**.
- **From the Waveshed plugin repository** (alternative): under
  **Plugins → Manage and Install Plugins… → Settings → Plugin Repositories**
  click **Add…** and enter `https://waveshed.io/qgis/plugins.xml`. If you
  added this repository before the plugin was listed on plugins.qgis.org, you
  can remove it again on the same page, so QGIS lists Waveshed from one source.

## The Aether engine (downloaded separately)

The plugin does **not** bundle the Aether engine binaries. The engine is
**separate, proprietary, closed-source software** and is **not** covered by this
repository's license. On first use, open **Plugins → Waveshed → Waveshed
Analysis**, switch to the **Settings** tab and click **Download Binaries**: the
plugin fetches the engine on demand from [waveshed.io](https://waveshed.io),
shows the engine **EULA** for your acceptance (**Accept & Download**), and
installs the binaries locally.
This download is always an explicit, user-initiated action.

On **macOS**, the plugin automatically clears the download-quarantine (Gatekeeper)
attribute from the installed engine after download, so the binaries launch
without a manual `xattr -cr` step.

Running analyses also requires an **API key**. **Get API Key** on the same
tab opens <https://waveshed.io/get-key>, which issues free keys for
non-commercial use; paste the key into **API key:** and click **Save**.

Supported engine platforms: **Windows x64**, **Linux x64**, and **macOS arm64**.

## License

Copyright (C) 2026 Gian-Andrea Heinrich.

Waveshed (this plugin) is free software: you can redistribute it and/or modify it
under the terms of the **GNU General Public License as published by the Free
Software Foundation, either version 2 of the License, or (at your option) any
later version** (GPL-2.0-or-later).

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the [`LICENSE`](LICENSE) file for the full GPL-2.0 text.

**The Aether engine binaries are not part of this program.** They are separate,
proprietary software distributed by Waveshed under their own End User License
Agreement, downloaded from [waveshed.io](https://waveshed.io). The GPL does not
apply to the Aether engine. The engine EULA permits **non-commercial use only**;
commercial, governmental and organisational use requires prior written
permission (info@waveshed.io).

**Model output, no liability.** Results are outputs of a radio propagation
model — approximations, not measurements or guarantees. Do not rely on them for
safety-of-life, regulatory, planning or financial decisions without independent
verification. To the maximum extent permitted by law, the author accepts no
liability for anything arising from their use. (This notice adds no restriction
to the GPL.)

## Links

- Website: <https://waveshed.io>
- Source & issues: <https://github.com/gian1312/waveshed-qgis>
