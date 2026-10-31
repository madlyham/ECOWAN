# ENODE2 Component Package

## Overview
Production reference firmware for the **ENODE2** environmental monitoring sensor node. Assembles the stationary ESP32 NodeCore v2.7 with a Semtech SX1262 LoRaWAN radio, a DHT11 digital temperature/humidity sensor, and an HW-038 analog soil moisture sensor. Features dual-layer WLAN OTA rollback fail-safe, LoRaWAN OTAA with NVS state persistence, and UTC minute slot-aligned transmissions at second 35.

## Interface
- `void setup()`: Configures serial (115200 baud), SX1262 SPI bus (`GPIO18`, `GPIO19`, `GPIO23`, `GPIO5`), FreeRTOS `coreMutex`, LoRaWAN OTAA activation, sensor inputs, and launches the Core 0 WLAN maintenance task.
- `void loop()`: Main execution loop managing UTC slot alignment, reading sensors, constructing EMF1 frames, and waiting for assigned transmission windows.
- `static bool otaBegin()`: Initializes ESP32 SoftAP (`ECOWAN-ENode2`) and starts the port 80 HTTP web server.
- `static void otaRollbackHandler()`: Endpoint handler for `POST /rollback`; triggers `esp_ota_mark_app_invalid_rollback_and_reboot()` or fallback `esp_ota_set_boot_partition()` to revert to the alternate firmware slot.
- `static void readAndSendEMF1()`: Samples DHT11 (GPIO4) and HW-038 (GPIO34 / ADC1_CH6), packs readings into Edge Measurement Format v1 (`0xE1`), and transmits uplink on LoRaWAN `FPort 10`.
- `static void scheduleAssignedUplinkSlot()`: Schedules the next immediate transmission window aligned to `SLOT_OFFSET_MS = 35000UL` (second 35 of the UTC minute).
- `static void persistLoRaWANStateLocked()`: Atomically saves RadioLib nonces and session buffers to ESP32 NVS (`"lorawan"` namespace).
- `static void requestAndUpdateNetworkTime(bool requestWasSent)`: Emits LoRaWAN MAC command `0x0D` (`DeviceTimeReq`) and processes gateway `DeviceTimeAns` to synchronize node clock with network UTC.

## Invariants
- **Radio Settle Delay**: Firmware enforces a 5,000 ms settle delay (`RADIO_SETUP_DELAY_MS`) after radio SPI initialization before invoking `activateOTAA()`.
- **Pin Allocations**:
  - SX1262 SPI: `SCK=18`, `MISO=19`, `MOSI=23`, `NSS=5`, `DIO1=33`, `RESET=27`, `BUSY=26`.
  - DHT11: `GPIO4` with NaN retry backoff.
  - HW-038 Soil Moisture: `GPIO34` (`ADC1_CH6`, 12-bit, `ADC_11db` attenuation, 8-sample rolling average). `pinMode()` must never be called on input-only GPIO34.
- **Concurrency & Flash Safety**: `coreMutex` must be acquired before any RadioLib SPI transfer or NVS write; WLAN OTA updates reject incoming binary streams if `coreMutex` is held.
- **Fail-Safe OTA Rollback**: New firmware boots in `ESP_OTA_IMG_PENDING_VERIFY`. It is marked valid via `esp_ota_mark_app_valid_cancel_rollback()` only after a successful first uplink cycle. If the node crashes or panics before completing an uplink, the bootloader reverts to the alternate partition.
- **Slot Alignment**: Fixed offset of 35 seconds into each 60-second UTC minute (`SLOT_OFFSET_MS = 35000UL`, `SLOT_PERIOD_MS = 60000UL`). Catch-up burst transmissions after missed slots are prohibited.

## Side Effects
- Writes nonces and active LoRaWAN sessions to ESP32 Preferences/NVS flash.
- Transmits RF packets on `AS923` frequencies via SX1262.
- Broadcasts a 2.4 GHz 802.11b/g/n Wi-Fi SoftAP (`ECOWAN-ENode2`).
- Reboots the MCU via `esp_restart()` on completed OTA uploads or rollback triggers.

## Verification
- Run: Remote build check via `lacave` compiler bridge (`POST http://100.93.135.116:8027/api/compile`) or local compilation via `arduino-cli compile --fqbn esp32:esp32:esp32 ENODE2/ENODE2.ino`.
