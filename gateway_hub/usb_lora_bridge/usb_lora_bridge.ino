/*
  ECOWAN 3.0 — USB LoRa Extension Bridge Firmware
  Target: ESP32 + SX1262 / RA-01SH (RadioLib, AS923 region plan)
  Role: Connects via USB Serial (115200 baud) to Edge Laptop/Orin.
        Accepts AT commands from Python (realsense_gui.py / realsense_node.py)
        and transmits EGDC v2 Delta / Firewatch ROI packets wirelessly to ChirpStack.
*/

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include <Preferences.h>
#include <stdint.h>
#include <string.h>

#if __has_include("credentials.h")
  #include "credentials.h"
#else
  #include "credentials.h.example"
#endif

// Hardware SPI & Radio Pins (Standard ECOWAN Pinout)
#define RADIO_NSS_PIN   5
#define RADIO_DIO1_PIN 33
#define RADIO_RST_PIN  27
#define RADIO_BUSY_PIN 26
#define RADIO_SCK_PIN  18
#define RADIO_MISO_PIN 19
#define RADIO_MOSI_PIN 23

SX1262 radio = new Module(RADIO_NSS_PIN, RADIO_DIO1_PIN, RADIO_RST_PIN, RADIO_BUSY_PIN);
LoRaWANNode loraNode(&radio, &AS923);
Preferences preferences;

static bool radioReady = false;
static bool lorawanJoined = false;
static String serialBuffer = "";

// Helper: Hex character to nibble
static int8_t hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

// Helper: Decode hex string to byte array
static size_t decodeHex(const String& hexStr, uint8_t* outBuf, size_t maxLen) {
  size_t hexLen = hexStr.length();
  size_t byteCount = 0;
  for (size_t i = 0; i + 1 < hexLen && byteCount < maxLen; i += 2) {
    int8_t high = hexNibble(hexStr.charAt(i));
    int8_t low = hexNibble(hexStr.charAt(i + 1));
    if (high < 0 || low < 0) break;
    outBuf[byteCount++] = (uint8_t)((high << 4) | low);
  }
  return byteCount;
}

static void restoreNoncesAndSession() {
  preferences.begin("lorawan", false);
  uint8_t noncesBuffer[128];
  size_t noncesLen = preferences.getBytes("nonces", noncesBuffer, sizeof(noncesBuffer));
  if (noncesLen > 0) {
    int16_t setRes = loraNode.setBufferNonces(noncesBuffer);
    Serial.printf("[LORA] Restored nonces (%u bytes, code=%d)\n", (unsigned)noncesLen, setRes);
  }
  uint8_t sessionBuffer[256];
  size_t sessionLen = preferences.getBytes("session", sessionBuffer, sizeof(sessionBuffer));
  if (sessionLen > 0) {
    int16_t setRes = loraNode.setBufferSession(sessionBuffer);
    Serial.printf("[LORA] Restored session (%u bytes, code=%d)\n", (unsigned)sessionLen, setRes);
    if (setRes == RADIOLIB_ERR_NONE) {
      lorawanJoined = true;
    }
  }
  preferences.end();
}

static void saveSession() {
  preferences.begin("lorawan", false);
  uint8_t sessionBuffer[256];
  size_t sessionLen = loraNode.getBufferSession(sessionBuffer);
  if (sessionLen > 0 && sessionLen <= sizeof(sessionBuffer)) {
    preferences.putBytes("session", sessionBuffer, sessionLen);
  }
  uint8_t noncesBuffer[128];
  size_t noncesLen = loraNode.getBufferNonces(noncesBuffer);
  if (noncesLen > 0 && noncesLen <= sizeof(noncesBuffer)) {
    preferences.putBytes("nonces", noncesBuffer, noncesLen);
  }
  preferences.end();
}

static void doJoin() {
  Serial.println("[LORA] Activating OTAA Join...");
  int16_t state = loraNode.activateOTAA();
  if (state == RADIOLIB_LORAWAN_MODE_OTAA || state == RADIOLIB_ERR_NONE) {
    lorawanJoined = true;
    saveSession();
    Serial.println("OK joined=1");
  } else {
    lorawanJoined = false;
    Serial.printf("ERR join_failed code=%d\n", state);
  }
}

static void handleLoraTx(const String& arg) {
  // Expected format: <fport>:<hex_string>
  int colonIdx = arg.indexOf(':');
  if (colonIdx < 0) {
    Serial.println("ERR bad_format_expected_fport:hex");
    return;
  }

  uint8_t fport = (uint8_t)arg.substring(0, colonIdx).toInt();
  if (fport == 0) fport = 11;
  String hexPayload = arg.substring(colonIdx + 1);
  hexPayload.trim();

  uint8_t payload[256];
  size_t len = decodeHex(hexPayload, payload, sizeof(payload));
  if (len == 0) {
    Serial.println("ERR empty_or_invalid_hex");
    return;
  }

  if (!lorawanJoined) {
    // Attempt lazy join if not yet joined
    int16_t joinState = loraNode.activateOTAA();
    if (joinState == RADIOLIB_LORAWAN_MODE_OTAA || joinState == RADIOLIB_ERR_NONE) {
      lorawanJoined = true;
      saveSession();
    } else {
      Serial.printf("ERR not_joined code=%d\n", joinState);
      return;
    }
  }

  uint32_t tStart = millis();
  int16_t state = loraNode.sendReceive(payload, len, fport);
  uint32_t airtimeMs = millis() - tStart;

  if (state == RADIOLIB_ERR_NONE || state == RADIOLIB_LORAWAN_NO_DOWNLINK) {
    saveSession();
    Serial.printf("OK tx_done fport=%u bytes=%u airtime_ms=%.1f\n", fport, (unsigned)len, (float)airtimeMs);
  } else {
    Serial.printf("ERR tx_failed code=%d fport=%u\n", state, fport);
  }
}

static void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd.equalsIgnoreCase("AT") || cmd.equalsIgnoreCase("AT+PING")) {
    Serial.println("OK PONG");
  } else if (cmd.equalsIgnoreCase("AT+VERSION")) {
    Serial.println("OK ECOWAN 3.0 USB LoRa Bridge");
  } else if (cmd.equalsIgnoreCase("AT+STATUS")) {
    Serial.printf("OK joined=%d fcnt=%lu dev_eui=%016llX\n",
      lorawanJoined ? 1 : 0,
      loraNode.getFCntUp(),
      DEV_EUI
    );
  } else if (cmd.equalsIgnoreCase("AT+JOIN")) {
    doJoin();
  } else if (cmd.startsWith("AT+LORA_TX=")) {
    handleLoraTx(cmd.substring(11));
  } else if (cmd.equalsIgnoreCase("AT+RESET")) {
    Serial.println("OK restarting");
    delay(100);
    ESP.restart();
  } else {
    Serial.printf("ERR unknown_command: %s\n", cmd.c_str());
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n--- ECOWAN 3.0 USB LoRa Bridge ---");

  SPI.begin(RADIO_SCK_PIN, RADIO_MISO_PIN, RADIO_MOSI_PIN, RADIO_NSS_PIN);

  int16_t state = radio.begin();
  if (state != RADIOLIB_ERR_NONE) {
    Serial.printf("[RADIO] Init failed code=%d\n", state);
  } else {
    Serial.println("[RADIO] SX1262 initialized.");
  }

  // Settle delay before OTAA init per ECOWAN hardware rules
  delay(1000);

  int16_t loraState = loraNode.beginOTAA(JOIN_EUI, DEV_EUI, NWK_KEY, APP_KEY);
  if (loraState == RADIOLIB_ERR_NONE) {
    radioReady = true;
    Serial.println("[LORA] OTAA node initialized (AS923).");
    restoreNoncesAndSession();
  } else {
    Serial.printf("[LORA] beginOTAA failed code=%d\n", loraState);
  }

  Serial.println("READY ECOWAN USB LORA BRIDGE v3.0");
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialBuffer.length() > 0) {
        handleCommand(serialBuffer);
        serialBuffer = "";
      }
    } else {
      if (serialBuffer.length() < 1024) {
        serialBuffer += c;
      }
    }
  }
}
