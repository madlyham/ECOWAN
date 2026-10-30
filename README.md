# ECOWAN — Modular Environmental Monitoring AIoT & LoRaWAN Platform

**ECOWAN** is a modular environmental-monitoring platform built around **ESP32 + SX1262 (RA-01SH) LoRaWAN nodes**, a **Dragino LPS8v2 Gateway** running **ChirpStack v4** + **MQTT**, and the **Edge Monitor** web dashboard + server-side firmware codegen hub (`gateway_hub/`).

---

## 1. Repository Layout

```text
AITA_Sripts/
├── gateway_hub/                         # Main Hub for all Gateway & Server-Side Services
│   ├── index.html                       # Edge Monitor Web App + Stationary Core-First Firmware Generator
│   ├── edge_dash.py                     # Gateway HTTP & MQTT Backend Service (/root/edge_dash.py on Dragino)
│   ├── compile_bridge.py                # Remote Build Server Arduino-CLI Compiler Bridge (/root/compile_bridge.py on lacave)
│   ├── parts_catalog.json               # Modular sensor catalog & EMF1 wire-type definitions
│   ├── ECOWAN_NodeCore_Template.ino     # Stationary NodeCore v2.4 Arduino template (OTA + LoRaWAN + UTC Slots + EMF1)
│   ├── CODEGEN_CORE_CONTRACT.json       # Stationary core contract specification
│   ├── test_codegen.js                  # Automated unit test suite for codegen & UI
│   ├── README.md                        # Gateway & server hub documentation
│   └── CHANGELOG.md                     # Hub & Codegen v2.4 changelog
├── ENODE1/
│   ├── ENODE1.ino                       # Production NodeCore v2.4 firmware for ENODE1 (UTC slot 5s)
│   └── credentials.h.example            # Example node credentials template
├── ENODE2/
│   ├── ENODE2.ino                       # Production NodeCore v2.4 firmware for ENODE2 (UTC slot 35s)
│   └── credentials.h.example            # Example node credentials template
├── credentials.h.example                # Shared credentials template
├── AGENTS.md                            # Engineering & diagnostic rules
└── README.md                            # Project overview
```

---

## 2. Hardware Architecture & Pin Map

### Sensor Nodes (`ENODE1`, `ENODE2`, and Generated Nodes)
- **MCU**: ESP32 (`115200` baud serial monitor)
- **LoRa Transceiver**: Semtech SX1262 / Ai-Thinker RA-01SH (`RadioLib`, `AS923` region plan)

| Function | ESP32 Pin | Notes |
| :--- | :--- | :--- |
| **LoRa `SCK`** | `GPIO18` | Explicit `SPI.begin(18, 19, 23, 5)` |
| **LoRa `MISO`** | `GPIO19` | SPI MISO |
| **LoRa `MOSI`** | `GPIO23` | SPI MOSI |
| **LoRa `NSS / CS`** | `GPIO5` | SPI Chip Select |
| **LoRa `DIO1`** | `GPIO33` | SX1262 IRQ |
| **LoRa `RESET`** | `GPIO27` | SX1262 Hardware Reset |
| **LoRa `BUSY`** | `GPIO26` | SX1262 Busy Status |
| **DHT11 Data** | `GPIO4` | 1-Wire Digital Temperature & Humidity (with 2.1s retry on `NAN`) |
| **HW-038 Soil Moisture `AO`** | `GPIO34` / `GPIO32` | `ADC1` input (`12-bit`, `ADC_11db`, 8-sample average; max `3.3V`) |
| **4-Pin Light Sensor (Optional)** | Configurable | Dual-dropdown `AO` (ADC1 pin) + optional `DO` threshold pin (Active-LOW) |

### Gateway (`Dragino LPS8v2`) & Build Server (`lacave`)
- **LoRaWAN Network Server**: ChirpStack v4
- **MQTT Broker**: `127.0.0.1:1883` (`application/+/device/+/event/+`)
- **Edge Monitor Service**: `edge-monitor.service` (`/usr/bin/python3 -u /root/edge_dash.py` on port `8026`, Tailscale HTTPS on port `8443`)
- **Build Server**: `compile-bridge.service` (`/usr/bin/python3 -u /root/compile_bridge.py` on `lacave:8027` with persistent build cache)

---

## 3. Stationary NodeCore v2.4 & Uplink Reliability

Every ECOWAN node sketch (`ENODE1.ino`, `ENODE2.ino`, and sketches generated via `gateway_hub/index.html`) shares an immutable **NodeCore v2.4** foundation:

1. **5-Second Radio Settle Delay (`RADIO_SETUP_DELAY_MS = 5000UL`)**:
   - After `radio.begin()` and `loraNode.beginOTAA(...)`, the firmware waits **5,000 ms** before calling `activateOTAA()`, allowing the 3.3V rail and SX1262 TCXO/PLL to stabilize.
2. **NVS Session & Nonce Persistence**:
   - Saves RadioLib `nonces` and `session` buffers in ESP32 `Preferences` (`"lorawan"` namespace) so reboots restore the active LoRaWAN session without burning `DevNonce` values or resetting frame counters (`FCnt`).
3. **UTC Slot Collision Avoidance (`DeviceTimeReq` / `DeviceTimeAns`)**:
   - Requests network UTC time via LoRaWAN MAC command `0x0D` (`DeviceTimeReq`) upon join and every 10 uplinks.
   - Aligns transmissions to each node's assigned UTC second within the reporting cycle (`ENODE1` = second `5`, `ENODE2` = second `35` in a `60s` period).
4. **Password-Protected WLAN `.bin` OTA (`WebServer` on Port 80)**:
   - Runs a dedicated FreeRTOS task on Core 0 serving `http://192.168.4.1/` (`/update` accepts `.bin` firmware images) serialized via `coreMutex`.

---

## 4. Payload Codec Standard (`EMF1 v1` on `FPort 10`)

- **Magic Header**: `0xE1` on **LoRaWAN `FPort 10`**
- **Record Layout**: `[field_id][type_code][big-endian value]`
  - `0x01` **Temperature**: type `0x24` (`int16`, scale `0.01 °C`)
  - `0x02` **Humidity**: type `0x23` (`uint16`, scale `0.01 %RH`)
  - `0x03` **Soil Moisture**: type `0x11` (`uint16`, scale `1`, raw 12-bit ADC counts `0..4095`)
  - `0x10` **Core Uptime**: type `0x31` (`uint32`, scale `1`, seconds — emitted automatically in core-only mode)
- **Missing Sensor Rule**: Failed sensor reads (`NAN`) are omitted from the EMF1 frame rather than encoded as `0`.

---

## 5. Verification

Run the automated codegen & UI unit test suite from the repository root:

```bash
node gateway_hub/test_codegen.js
```
