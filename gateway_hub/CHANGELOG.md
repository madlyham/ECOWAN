# Changelog

## v2.9 — Hardened Boot Order, Pruned Endpoints & Security Sanitization
- **Wi-Fi SoftAP Boot Order (Fix #1):** Started `otaBegin()` first before radio initialization in `setup()` so the emergency maintenance AP is always accessible even if radio setup fails. Added 60s timeout and automatic `esp_restart()` fallback on radio failure.
- **Dead Legacy Endpoint Pruning (Fix #2):** Removed 133 lines of obsolete multi-file packaging code (`/api/field-package`, `/api/firmware-source`, and dead contract references) in `edge_dash.py`.
- **ChirpStack Token Invalidation (Fix #3):** Auto-invalidates stale cached tokens on HTTP 401 Unauthorized in `edge_dash.py` and retries once with a fresh token.
- **Compiler Bridge Stale Binary Unlink (Fix #4):** Ensures existing `.bin` files are deleted before `arduino-cli compile` runs in `compile_bridge.py`.
- **DOM XSS Sanitization (Fix #5):** Added `cfgEscapeHtml()` in `index.html` to sanitize SSID and password in review cards, with test coverage in `test_codegen.js`.
- **EMF1 Payload Codec Consolidation (Fix #6):** Consolidated `payload_codec.py` into `gateway_hub/` with `EMF1_REGISTRY` export, enabling standalone `edge_dash.py` imports and passing all 18 codec unit tests.
- **OTA Upload Mutex Timeout (Fix #7):** Added 1500ms timeout (`pdMS_TO_TICKS(1500)`) for `coreMutex` in `otaUploadStream()` across `ECOWAN_NodeCore_Template.ino`, `ENODE1.ino`, `ENODE2.ino`, and `index.html` codegen to eliminate instant 400 rejection during radio activity.

## v2.8 — EGM Architecture Contracts & Reference Sketch Synchronization
- Bootstrapped hierarchical Elephant-Goldfish Model (EGM) documentation contracts across Root `README.md` (Blueprint/Hay), `gateway_hub/README.md`, `ENODE1/README.md`, and `ENODE2/README.md`.
- Regenerated `ENODE1.ino`, `ENODE2.ino`, and `ECOWAN_NodeCore_Template.ino` from the v2.7 codegen generator, ensuring all reference sketches have identical rollback fail-safe support.
- Pruned obsolete legacy folders (`ENode1_Slotted/`, `ENode2_Slotted/`).
- Resolved all code-to-document drift reported by the EGM drift audit.

## v2.7 — Dual-Layer ESP32 OTA Firmware Rollback Fail-Safe
- Added automatic self-testing trial partition check (`ESP_OTA_IMG_PENDING_VERIFY`) to ESP32 NodeCore startup.
- Automatically confirms firmware health via `esp_ota_mark_app_valid_cancel_rollback()` upon first successful LoRaWAN uplink.
- Added 1-click web rollback endpoint (`POST /rollback`) in SoftAP maintenance portal (`http://192.168.4.1/`) calling `esp_ota_mark_app_invalid_rollback_and_reboot()` with fallback `esp_ota_set_boot_partition()`.
- Added unit tests for rollback codegen markers and verified compilation against `LaCave` build server (binary size: 1,014,288 bytes).
- Live deployed to Dragino gateway (`100.83.203.103`).

## v2.6 — Single-Dashboard Node Provisioning ("Drop ChirpStack Bit by Bit")
- Automatic generation of random `DevEUI`, `JoinEUI`, and mirrored 128-bit `NwkKey`/`AppKey`.
- Automated collision-free UTC slot allocation (`cfgNextFreeSlotSecond()`).
- Added headless ChirpStack REST API bridge (`chirpstack_sync_device()`) syncing device profiles and keys on `POST /api/configs`.
- Added in-dashboard LoRaWAN lifecycle actions: `POST /api/nodes/reset-join` (clears nonces, active sessions, and queues) and `POST /api/nodes/delete`.
- Added gateway radio diagnostics (Dragino SX1302 forwarder status, gateway ID, AS923 region, and ChirpStack gateway heartbeat) to `/api/state` and UI.

## v2.5 — Modular Dual-Dropdown Sensor Assignment
- Added support for sensors requiring multiple pins (e.g. 4-pin light sensor module with `AO` analog pin on ADC1 and optional `DO` digital comparator pin).
- Auto-filters occupied GPIO pins to prevent pin collision during configuration.
- Auto-assigns EMF1 wire encoding slots based on catalog metadata.

## v2.4 — Core-first integration candidate
- Uses the shared architecture in both supplied NodeCore demos as the mandatory-core reference.
- Keeps SX1262, OTAA, persistent nonce/session buffers, WLAN `.bin` OTA, DeviceTime request/response handling, UTC slot scheduling, EMF1 encoder and FPort 10.
- Replaces hard-coded node identity and OTAA secrets with explicit user-editable placeholders and a fail-closed check.
- Removes hardcoded sensor implementations in favor of modular generation.
- Adds an EMF1 core-health uptime field (0x10) so the core can exercise uplinks before sensor modules are added.
- Makes report interval and UTC slot second user-configurable.
