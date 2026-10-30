# ECOWAN Gateway & Server Hub (`gateway_hub/`)

This folder is the single hub for all gateway-side and build-server-side files in the ECOWAN platform:

| File | Role | Live Deployment Target |
| :--- | :--- | :--- |
| `index.html` | Edge Monitor Web Dashboard + Stationary Core-First Firmware Generator | Dragino `/root/index.html` |
| `edge_dash.py` | Gateway HTTP & MQTT Backend Service (`edge-monitor.service`) | Dragino `/root/edge_dash.py` |
| `parts_catalog.json` | Modular sensor catalog, port profiles, and EMF1 wire-type definitions | Dragino `/root/parts_catalog.json` |
| `compile_bridge.py` | Remote Arduino-CLI Compiler Bridge with warm build cache (`compile-bridge.service`) | Build server `lacave` `/root/compile_bridge.py` |
| `ECOWAN_NodeCore_Template.ino` | Stationary NodeCore v2.4 Arduino template (WLAN OTA + LoRaWAN + UTC Slots + EMF1) | Embedded in `index.html` & verified by `test_codegen.js` |
| `CODEGEN_CORE_CONTRACT.json` | Stationary core contract specification & markers | Reference contract |
| `test_codegen.js` | Automated unit test suite for code generation, validation, and determinism | Run locally via `node gateway_hub/test_codegen.js` |

---

## KISS Engineering Philosophy ("Keep It Simple, Stupid")

All gateway and server-side components adhere strictly to the KISS principles:
1. **Simplicity Over Cleverness**: Write clean, direct code. Avoid speculative layers of abstraction and premature complexity.
2. **Automate the Cognitive Burden**: Resolve routine decisions automatically (e.g., auto-resolving EMF1 wire encoding slots, auto-filtering occupied GPIO pins) so the user does not need to configure internal technical details.
3. **Trim the Fat**: Continuously remove dead code, unused functions, obsolete legacy folders, and duplicated files.
4. **Single Source of Truth**: Keep all gateway, server, and catalog files consolidated here in `gateway_hub/` without redundant root copies.
5. **Direct, Actionable UI**: Keep primary actions immediately accessible (e.g., always-visible "Save Configuration", direct "Download Code", and clean inline telemetry readouts).
