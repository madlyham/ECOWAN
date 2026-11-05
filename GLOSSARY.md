# ECOWAN Domain Glossary

Canonical domain vocabulary for the ECOWAN platform. All code, specifications, and agent sessions must use these terms consistently.

---

## Core Platform Terms (Settled)

- **Sensor Node (`Node`)**: A physical ESP32 + Semtech SX1262 microcontroller unit (`ENODE1`, `ENODE2`) running `NodeCore` firmware that samples attached sensors and transmits LoRaWAN uplinks on the `AS923` regional plan.
- **Stationary NodeCore (`NodeCore`)**: The immutable C++ firmware foundation providing Core 0 Wi-Fi SoftAP maintenance (`/update` and `/rollback`), dual-layer OTA trial verification, SX1262 SPI initialization with a 5-second settle delay, NVS session/nonce persistence, and UTC slot scheduling.
- **UTC Slot Schedule**: The deterministic time-division transmission window (`SLOT_PERIOD_MS = 60000`, `SLOT_GUARD_MS = 3000`) synchronized via LoRaWAN `DeviceTimeReq`/`DeviceTimeAns` MAC commands so multiple environmental sensor nodes share a single frequency lane without colliding.
- **EMF1 (`Edge Measurement Format v1`)**: The canonical binary wire protocol on LoRaWAN `FPort 10` (magic header `0xE1`) encoding typed scalar sensor readings as `[field_id][type_code][big-endian value]`. Missing/faulty readings (`NaN`) are omitted, never zero-filled.
- **Gateway Hub (`dragino`)**: The Dragino LPS8v2 LoRaWAN gateway (`100.83.203.103`) hosting the 8-channel SX1302 packet forwarder, ChirpStack v4 Network Server (`:8090`), Mosquitto MQTT broker (`:1883`), and the Edge Monitor service (`edge_dash.py` on `:8026`).
- **Compiler & Vision Server (`lacave`)**: The local/Tailscale server (`100.93.135.116`) running `compile_bridge.py` (`:8027`) for Arduino builds and `vision_hub.py` (`:8028`) for server-side `Frame Reconstruction`, radio budget benchmarking, and ML inference.

---

## Vision & AIoT Streaming Terms (`EGDC v2` — Finalized Experimental Specification)

- **Vision Host**: The edge compute unit at a camera station (Jetson Orin, Raspberry Pi, or Laptop VM) connected to an Intel RealSense D435. It runs the **4-Stage Visual-Difference Engine**, evaluates the **Cost-Based Adaptive Cutoff**, and emits transport-independent `EGDC v2` packets.
- **Masked Environmental Reference (`E_ref`)**: A slowly adapting exponential moving average of the scene background that absorbs normal illumination shifts and wind oscillations, but **freezes (masks out) suspicious/candidate anomaly cells** so a slowly developing smoke plume is never absorbed into the background as the "new normal."
- **Cost-Based Adaptive Cutoff**: The decision engine that chooses between sending `EGDC v2` delta packets vs. a fresh LAN/Internet Keyframe by evaluating both configurable packet ceilings (`Conservative=4`, `Balanced=8`, `Extended=10`, `Experimental=16`) and total estimated transmission cost:
  $$C_{\text{delta}} = T_{\text{airtime}} + T_{\text{queue}} + T_{\text{recovery}}$$
- **Payload Layer Separation (`App` vs. `MAC` vs. `PHY`)**:
  - **Application Payload (`App Bytes`)**: Usable `EGDC v2` bytes (e.g., `51 B` at `DR2`, `160 B` operational target cap vs. `222 B` LoRaWAN regional maximum at `DR4/DR5` with `FOpts=0`).
  - **MAC Overhead (`MAC Bytes`)**: Protocol framing added by the radio stack (`13 B` minimum for LoRaWAN `MHDR+FHDR+FPort+MIC`; `0–4 B` for Custom Raw LoRa).
  - **Physical Payload (`PHY Bytes`)**: `App Bytes + MAC Bytes`, which determines actual symbol count and RF airtime on air.
- **Frame Generation ID (`keyframe_gen_id`)**: An 8-bit generation identifier in the `EGDC v2` header incremented on every new Keyframe or `frame_seq` wraparound (`256` frames), preventing delayed or out-of-order packets from corrupting a newer reconstruction cycle.
- **Incomplete-Frame Protection**: The receiver rule requiring incoming delta packets to assemble in a **Shadow Staging Buffer** indexed by `(keyframe_gen_id, frame_seq)` so incomplete updates never silently overwrite the last verified complete image (`committed_frame`).
- **Codec-Transport Independence**: The formal architectural seam guaranteeing that `EGDC v2` only produces and consumes byte buffers, remaining completely agnostic to whether packets travel over `LoRaWANSerialPoolTransport`, `CustomLoRaRawTransport`, or `LanSimulatedLoRaTransport`.
