# Waveshed

**RF propagation analysis for QGIS — coverage and point-to-point — powered by the Aether GPU engine.**

Waveshed is a QGIS plugin for radio-frequency propagation modelling. It adds:

- **Site Analysis** — area coverage / signal-strength maps from a transmitter.
- **Point-to-Point (P2P)** — link-budget analysis between two sites, single or batch.
- Propagation models: **LOS**, **Free-Space Path Loss**, and **ITM (Longley-Rice)**.

All computation runs on the **Aether engine**, a GPU-accelerated propagation core.
The plugin itself is a pure-Python wrapper: it prepares terrain, builds job
configurations, invokes the engine, and loads results back into QGIS.

## Installation

### From the Waveshed plugin repository (recommended)

1. In QGIS, open **Plugins → Manage and Install Plugins… → Settings**.
2. Under **Plugin Repositories** click **Add…**, name it `Waveshed` and enter
   the URL `https://waveshed.io/qgis/plugins.xml`.
3. Go to **All**, search for **Waveshed** and click **Install Plugin**.
   QGIS offers updates from the same repository from then on.

(Publication on plugins.qgis.org follows; until then the plugin is not in the
default QGIS repository.)

### From a ZIP

1. Download the latest `waveshed.<version>.zip` (https://waveshed.io/downloads).
2. In QGIS, open **Plugins → Manage and Install Plugins… → Install from ZIP**.
3. Select the downloaded ZIP and click **Install Plugin**.

## The Aether engine (downloaded separately)

The plugin does **not** bundle the Aether engine binaries. The engine is
**separate, proprietary, closed-source software** and is **not** covered by this
repository's license. On first use, open the plugin's **Settings → Download**
dialog: it fetches the engine on demand from [waveshed.io](https://waveshed.io),
shows the engine **EULA** for your acceptance, and installs the binaries locally.
This download is always an explicit, user-initiated action.

On **macOS**, the plugin automatically clears the download-quarantine (Gatekeeper)
attribute from the installed engine after download, so the binaries launch
without a manual `xattr -cr` step.

Running analyses also requires an **API key** from
[waveshed.io](https://waveshed.io), entered in the plugin's Settings dialog.

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
