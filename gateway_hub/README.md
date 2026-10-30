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
