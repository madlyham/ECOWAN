# Changelog

## v2.4 — Core-first integration candidate
- Uses the shared architecture in both supplied NodeCore demos as the mandatory-core reference.
- Keeps SX1262, OTAA, persistent nonce/session buffers, WLAN `.bin` OTA, DeviceTime request/response handling, UTC slot scheduling, EMF1 encoder and FPort 10.
- Replaces hard-coded node identity and OTAA secrets with explicit user-editable placeholders and a fail-closed check.
- Removes the DHT11 and soil moisture implementations for this phase.
- Adds an EMF1 core-health uptime field so the core can exercise uplinks before sensor modules are added.
- Makes report interval and UTC slot second user-configurable.
- No claim of successful compile or hardware validation.
