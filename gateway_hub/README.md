# ECOWAN Gateway & Server Hub (`gateway_hub`)

## Overview
The `gateway_hub` directory is the unified operational hub for all gateway-side services, build-server integrations, modular sensor definitions, and browser-based firmware generation within the ECOWAN platform. It hosts the Edge Monitor web dashboard (`edge_dash.py` + `index.html`), the remote Arduino build server compiler bridge (`compile_bridge.py`), the hardware sensor definitions (`parts_catalog.json`), the stationary core contracts (`CODEGEN_CORE_CONTRACT.json`, `ECOWAN_NodeCore_Template.ino`), and the automated validation test suite (`test_codegen.js`).

## Interface
- **HTTP Backend Service (`edge_dash.py` on Dragino port 8026 / 8443):**
  - `GET /`: Serves the single-page Edge Monitor dashboard application (`index.html`).
  - `GET /healthz`: Service health probe returning `{"ok": true, "version": "...", "mqtt": "...", "nodes": N}`.
  - `GET /api/state`: Returns live telemetry, node statuses, and Dragino SX1302 gateway radio diagnostics (`/usr/bin/fwd`, `/var/iot/status`, Gateway ID).
  - `POST /api/configs`: Validates and saves node hardware profiles into `/root/node_configs.json`; automatically executes headless device creation and key synchronization against ChirpStack REST API (`http://127.0.0.1:8090/api/devices`).
  - `POST /api/nodes/reset-join`: Flushes `dev-nonces`, clears device activation sessions, and purges downlink queues in ChirpStack.
  - `POST /api/nodes/delete`: Atomically unregisters a node from ChirpStack and local state.
  - `GET /api/catalog`: Returns `parts_catalog.json`.
- **Remote Compiler Bridge (`compile_bridge.py` on build server port 8027):**
  - `GET /health`: Reports compiler readiness, cached sketches count, and `arduino-cli` status.
  - `POST /api/compile`: Compiles submitted Arduino `.ino` sketches using an MD5-hashed warm build cache.
- **Frontend Firmware Generator (`index.html`):**
  - `cfgAssembleSketch(payload)`: Assembles complete standalone `.ino` source code combining stationary NodeCore with selected sensor drivers.
  - `cfgNextFreeSlotSecond(period, excludeId)`: Computes the next collision-free UTC second offset.
  - `cfgRandomHex(bytes)`: Cryptographically generates random `DevEUI`, `JoinEUI`, and 128-bit LoRaWAN keys.
  - Dual-Layer Rollback Portal: Generates embedded web maintenance endpoints (`/update` for firmware binary upload and `/rollback` for 1-click fallback).
- **Core Contracts & Catalogs:**
  - `parts_catalog.json`: Hardware pin profiles, EMF1 wire-types, and constraints for supported sensors.
  - `CODEGEN_CORE_CONTRACT.json`: Immutable structural anchors for the NodeCore template.
  - `ECOWAN_NodeCore_Template.ino`: Reference implementation of the stationary NodeCore.

## Invariants
- **KISS Philosophy**: Simplicity over cleverness; automate cognitive burden (auto-assigning next free wire slots, auto-filtering occupied GPIO pins); single source of truth in `gateway_hub/`.
- **Gateway Deployment Flag**: The Dropbear SSH server on the Dragino LPS8v2 requires the legacy SCP flag (`scp -O`).
- **Backup Before Overwrite**: All deployments to the gateway (`100.83.203.103`) must preserve `.bak` backups (`index.html.bak`, `edge_dash.py.bak`, `parts_catalog.json.bak`) before overwriting live files in `/root/`.
- **EMF1 Wire Format Standard**: Frames sent to FPort 10 must start with magic header byte `0xE1`. Failed sensor reads (`NaN`) must be omitted from the frame rather than transmitted as zero.
- **Headless Identity Synchronization**: LoRaWAN 1.0.3 devices require identical 128-bit `nwkKey` and `appKey`. The backend mirrors single-key inputs automatically.
- **OTA Rollback Invariant**: Assembled firmware must boot in `ESP_OTA_IMG_PENDING_VERIFY` and cancel rollback via `esp_ota_mark_app_valid_cancel_rollback()` only after a successful first uplink.

## Side Effects
- Mutates `/root/node_configs.json` and `/root/edge_monitor_state.json` on the Dragino gateway.
- Subscribes to local MQTT broker `127.0.0.1:1883` on topic `application/+/device/+/event/+`.
- Issues authenticated REST mutations to ChirpStack v4 API on port 8090.
- Allocates temporary compilation directories in `/tmp/ecowan-builds/` on build server `lacave`.

## Verification
- Run: `node gateway_hub/test_codegen.js`
