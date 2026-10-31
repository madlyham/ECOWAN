# ECOWAN Platform Blueprint

## System Overview
ECOWAN is a modular environmental-monitoring AIoT platform engineered for high reliability, low power consumption, and zero-touch configuration. The architecture integrates ESP32 + Semtech SX1262 (RA-01SH) LoRaWAN sensor nodes, a Dragino LPS8v2 gateway running ChirpStack v4 and MQTT, a remote high-speed Arduino build server (`lacave`), and an integrated Edge Monitor web dashboard featuring automated firmware code generation.

The system enforces strict determinism across wireless transmissions via a UTC second-slot scheduler (`DeviceTimeReq`/`DeviceTimeAns`), dual-layer ESP32 OTA firmware rollback fail-safes, and standard Edge Measurement Format v1 (`EMF1`) frames on LoRaWAN FPort 10.

## Local Interface & Invariants
- `credentials.h.example`: Baseline configuration template defining standard OTAA credential keys (`DEV_EUI`, `JOIN_EUI`, `NWK_KEY`, `APP_KEY`), default Wi-Fi SoftAP passwords, and server endpoints. Real credentials must reside in `credentials.h` (git-ignored) and never be committed to source control.

## Subdirectories & System Boundaries
- **[gateway_hub](./gateway_hub/README.md)**: Central operational hub containing the Dragino Edge Monitor backend service (`edge_dash.py`), frontend dashboard with single-page firmware code generator (`index.html`), remote compiler bridge (`compile_bridge.py`), sensor parts catalog (`parts_catalog.json`), stationary core contracts, and codegen unit test suite.
- **[ENODE1](./ENODE1/README.md)**: Production reference firmware for sensor node `ENODE1` equipped with DHT11 temperature/humidity and HW-038 soil moisture sensors, configured with UTC minute slot offset at second 5.
- **[ENODE2](./ENODE2/README.md)**: Production reference firmware for sensor node `ENODE2` equipped with DHT11 temperature/humidity and HW-038 soil moisture sensors, configured with UTC minute slot offset at second 35.

## Global Invariants
- **KISS Engineering**: Simplicity over cleverness; automate cognitive burden (auto-assigning next free wire encoding slots, auto-filtering occupied GPIO pins); single source of truth in `gateway_hub/`.
- **Radio & Timing Discipline**:
  - Transceivers operate under the `AS923` regional plan using `RadioLib`.
  - ESP32 nodes must observe a 5,000 ms settle delay (`RADIO_SETUP_DELAY_MS`) after radio SPI setup before calling `activateOTAA()`.
  - LoRaWAN sessions and nonces must persist across reboots in ESP32 Preferences (`"lorawan"` namespace) to prevent ChirpStack DevNonce replay rejection.
  - Uplinks must follow the 60-second UTC slot schedule (`SLOT_PERIOD_MS = 60000UL`). Nodes must never send burst catch-up transmissions after missed slots.
- **Payload Standards (EMF1 v1 on FPort 10)**:
  - Magic byte `0xE1` followed by `[field_id][type_code][big-endian value]` records.
  - Missing or faulty sensor readings (`NaN`) must be omitted from the payload rather than transmitted as zero.
- **Dual-Layer OTA Rollback Fail-Safe**:
  - Newly flashed firmware boots in `ESP_OTA_IMG_PENDING_VERIFY`. It is marked valid via `esp_ota_mark_app_valid_cancel_rollback()` only after a successful uplink cycle.
  - The maintenance portal (`http://192.168.4.1/`) provides a 1-click fallback endpoint (`POST /rollback`) that restarts the node into the previous working partition.
- **Zero-Touch Provisioning Synchronization**:
  - Sensor nodes added or updated via the Edge Monitor dashboard are automatically provisioned in ChirpStack v4 via its local REST API without manual gateway intervention.

## Global Side Effects
- Mutates persistent JSON states on the Dragino gateway (`/root/node_configs.json`, `/root/edge_monitor_state.json`).
- Directly modifies the ChirpStack v4 PostgreSQL database via authenticated local REST API calls on port 8090.
- Interacts with the gateway MQTT broker on `127.0.0.1:1883` under `application/+/device/+/event/+`.
- Writes session states and nonces to ESP32 onboard flash NVS across power cycles.

## System Verification
- Run: `node gateway_hub/test_codegen.js`
