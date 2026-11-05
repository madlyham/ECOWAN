# ECOWAN 3.0 — USB LoRa Extension Bridge

## Overview
Connects an ESP32 microcontroller paired with a Semtech SX1262 / RA-01SH LoRa transceiver to an edge computer (laptop, NVIDIA Jetson Orin) via USB. Acts as a transparent LoRaWAN modem for transmitting EGDC v2 vision deltas and Firewatch foveated ROI block alerts over the AS923 regional frequency plan to ChirpStack v4.

## Hardware Pinout (Standard ECOWAN ESP32 + RA-01SH)
| Signal | ESP32 GPIO | Description |
|---|---|---|
| SCK | GPIO 18 | SPI Clock |
| MISO | GPIO 19 | SPI Master-In Slave-Out |
| MOSI | GPIO 23 | SPI Master-Out Slave-In |
| NSS / CS | GPIO 5 | Chip Select |
| DIO1 | GPIO 33 | Radio Interrupt |
| RST | GPIO 27 | Radio Reset |
| BUSY | GPIO 26 | Radio Busy Flag |

## Flash Instructions
1. Copy `credentials.h.example` to `credentials.h`:
   ```bash
   cp credentials.h.example credentials.h
   ```
2. Edit `credentials.h` with your ChirpStack DevEUI, JoinEUI, NwkKey, and AppKey.
3. In Arduino IDE or via `arduino-cli`:
   - Board: `esp32:esp32:esp32`
   - Library: `RadioLib` (v6.x or v7.x)
   - Compile & Flash to your ESP32 COM port at `115200` baud.

## Serial Protocol (115200 Baud)
| Command | Response | Description |
|---|---|---|
| `AT` or `AT+PING` | `OK PONG` | Health check |
| `AT+VERSION` | `OK ECOWAN 3.0 USB LoRa Bridge` | Firmware version query |
| `AT+STATUS` | `OK joined=<0\|1> fcnt=<N> dev_eui=<hex>` | RadioLib join state & uplink counter |
| `AT+JOIN` | `OK joined=1` or `ERR join_failed code=<N>` | Forces OTAA network join |
| `AT+LORA_TX=<fport>:<hex>` | `OK tx_done fport=<fport> bytes=<len> airtime_ms=<ms>` | Transmits raw hex payload over AS923 |
| `AT+RESET` | `OK restarting` | Restarts ESP32 MCU |
