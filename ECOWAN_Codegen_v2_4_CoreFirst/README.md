# ECOWAN Codegen v2.4 — Core-first integration candidate

## Goal
Make the proven overlap between the two supplied node sketches the mandatory shared firmware core:
- SX1262 initialization (pins 5/33/27/26 by default)
- OTAA using user-provisioned JoinEUI, DevEUI, NwkKey and AppKey
- RadioLib nonce/session persistence in ESP32 Preferences
- password-protected WLAN AP with browser `.bin` OTA
- LoRaWAN DeviceTime synchronization and UTC-aligned slot scheduling
- EMF1 encoder and explicit FPort 10
- sensor-independent application hook

## Important current behavior
The template sends a small **core-health EMF1 field** until sensor modules are added:
- Field ID `0x10`
- Type `0x31` (unsigned 32-bit integer)
- Value: uptime seconds
- FPort `10`

The sensor layer is intentionally not implemented in this phase. Later sensor modules should append their own registered fields through `buildApplicationFields()` without modifying radio/session/OTA/scheduler code.

## Provisioning
Edit the top of `ECOWAN_NodeCore_Template.ino`:
- `NODE_NAME`
- `OTA_AP_SSID`
- `OTA_AP_PASSWORD` (use a unique strong password)
- `JOIN_EUI`, `DEV_EUI`
- `NWK_KEY`, `APP_KEY` (16 bytes each)
- `ECOWAN_REPORT_INTERVAL_SECONDS`
- `ECOWAN_UTC_SLOT_SECOND`

The sketch deliberately halts before radio startup when EUI/key placeholders remain zero. Never reuse the same DevEUI/keys across different devices. Do not publish credentials.

## UTC slot contract
`ECOWAN_UTC_SLOT_SECOND` is a second offset within each `ECOWAN_REPORT_INTERVAL_SECONDS` cycle. Assign different slots to nodes that share a collision domain. The scheduler uses LoRaWAN DeviceTime when available and randomized timing before synchronization.

A UTC slot is not a zero-collision guarantee. Production scheduling must account for airtime, regional data rate, clock uncertainty, guard time, retransmissions and other LoRaWAN devices.

## Dependencies / board
- ESP32 Arduino core with OTA partition support
- RadioLib version matching the supplied node sketches
- SX1262/RA-01SH wiring compatible with the pin definitions
- AS923 region/channel plan must match the gateway and ChirpStack configuration

## Validation status
- Derived from the two user-provided working-demo sketches, retaining their shared OTA/LoRaWAN/session/UTC/EMF1 architecture.
- Static checks performed on template structure only.
- **Not compiled** against the exact installed ESP32 core and RadioLib version.
- **Not hardware-verified** as this generic template.
- Not a production-certified or zero-failure release.

The existing v2.3 UI/backend is included for reference. Replacing its protected OTA core requires deploying the matching server-side template/hash contract as well as the UI; uploading only `index.html` is not sufficient to change server-side firmware assembly.
