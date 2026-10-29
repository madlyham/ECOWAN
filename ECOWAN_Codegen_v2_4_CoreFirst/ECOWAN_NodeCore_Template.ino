/*
  ECOWAN Codegen v2.4 — mandatory shared NodeCore template
  Target: ESP32 + SX1262/RA-01SH, Arduino framework, RadioLib.
  Core features:
    - WLAN maintenance AP + browser .bin upload
    - LoRaWAN OTAA + persistent RadioLib nonce/session buffers
    - UTC DeviceTime slots so ENode1/ENode2 do not uplink together
    - EMF1 v1 uplink on explicit FPort 10
    - EMF1 core-health uplink + explicit sensor application hook

  IMPORTANT:
    - This is a hardware-integration candidate, not yet a certified/verified release.
    - Replace identity/key placeholders before compiling or deploying.
    - Verify the regional band/channel plan against local rules and ChirpStack.
    - WLAN OTA is password-protected but does NOT itself provide cryptographic firmware
      authenticity. Production deployment should enable ESP32 Secure Boot and use
      signed firmware, with a controlled provisioning process.
*/

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_system.h>
#include <RadioLib.h>
#include <Preferences.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>

// -------------------- Node identity / OTAA keys --------------------
// Private credentials are kept in credentials.h (git-ignored).
// See credentials.h.example for setup and provisioning template.
#if __has_include("credentials.h")
  #include "credentials.h"
#elif __has_include("../credentials.h")
  #include "../credentials.h"
#elif __has_include("credentials.h.example")
  #include "credentials.h.example"
#else
  #include "../credentials.h.example"
#endif

// -------------------- Board configuration --------------------
#ifndef RADIO_NSS_PIN
  #define RADIO_NSS_PIN 5
#endif
#ifndef RADIO_DIO1_PIN
  #define RADIO_DIO1_PIN 33
#endif
#ifndef RADIO_RST_PIN
  #define RADIO_RST_PIN 27
#endif
#ifndef RADIO_BUSY_PIN
  #define RADIO_BUSY_PIN 26
#endif
#ifndef RADIO_SCK_PIN
  #define RADIO_SCK_PIN 18
#endif
#ifndef RADIO_MISO_PIN
  #define RADIO_MISO_PIN 19
#endif
#ifndef RADIO_MOSI_PIN
  #define RADIO_MOSI_PIN 23
#endif

static constexpr uint8_t EMF1_FPORT = 10;
static constexpr uint8_t EMF1_MAGIC = 0xE1;
static constexpr size_t EMF1_MAX_APP_BYTES = 51;  // Conservative profile; verify for selected DR/region.
static constexpr uint32_t RADIO_SETUP_DELAY_MS = 5000UL;
static constexpr uint32_t JOIN_RETRY_MS = 30000UL;
// USER CONFIGURATION: reserved UTC slot within each reporting interval.
#define ECOWAN_REPORT_INTERVAL_SECONDS 60UL
#define ECOWAN_UTC_SLOT_SECOND 5UL  // choose 0..interval-1; assign unique slots per collision domain
static constexpr uint32_t FIRST_UPLINK_DELAY_MS = ((ECOWAN_UTC_SLOT_SECOND >= 10UL) ? ECOWAN_UTC_SLOT_SECOND : 10UL) * 1000UL;
static constexpr uint32_t SLOT_PERIOD_MS = ECOWAN_REPORT_INTERVAL_SECONDS * 1000UL;
static constexpr uint32_t SLOT_OFFSET_MS = ECOWAN_UTC_SLOT_SECOND * 1000UL;
static constexpr uint32_t SLOT_GUARD_MS = 3000UL;
static constexpr uint8_t TIME_SYNC_EVERY_UPLINKS = 10;

// -------------------- Hardware/library objects --------------------
SX1262 radio = new Module(RADIO_NSS_PIN, RADIO_DIO1_PIN, RADIO_RST_PIN, RADIO_BUSY_PIN);
LoRaWANNode loraNode(&radio, &AS923);
Preferences preferences;
WebServer otaServer(80);

// Mutex prevents flash update writes overlapping RadioLib transactions/session writes.
// The OTA web task remains alive while the radio task is busy; an upload is rejected
// immediately if the radio currently owns the shared resource.
static SemaphoreHandle_t coreMutex = nullptr;
static volatile bool otaUploadInProgress = false;
static volatile bool otaUploadRejected = false;
static String otaUploadProblem;
static bool radioReady = false;
static bool lorawanJoined = false;
static bool networkTimeValid = false;
static bool hasSentFirstUplink = false;
static uint64_t networkTimeBaseMs = 0;
static uint32_t localMillisAtSync = 0;
static uint32_t nextSlotLocalMs = 0;
static bool nextSlotScheduled = false;
static uint8_t uplinksSinceTimeRequest = TIME_SYNC_EVERY_UPLINKS;

// -------------------- OTA core --------------------
static const char OTA_HTML[] PROGMEM = R"OTA(
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ECOWAN Node Maintenance</title></head><body>
<h1>ECOWAN Node Maintenance</h1>
<p>Node: )OTA" NODE_NAME R"OTA(</p>
<p>Choose a firmware .bin built for this exact board and partition layout.</p>
<form method="POST" action="/update" enctype="multipart/form-data">
<input type="file" name="firmware" accept=".bin,application/octet-stream" required>
<button type="submit">Upload firmware</button></form>
<p>Keep the node powered until the upload finishes and the device restarts.</p>
</body></html>
)OTA";

static void otaRoot() {
  otaServer.send_P(200, "text/html; charset=utf-8", OTA_HTML);
}

static void releaseCoreMutexIfHeldByUpload() {
  if (otaUploadInProgress) {
    otaUploadInProgress = false;
    xSemaphoreGive(coreMutex);
  }
}

static void otaUploadStream() {
  HTTPUpload& upload = otaServer.upload();

  switch (upload.status) {
    case UPLOAD_FILE_START: {
      otaUploadRejected = false;
      otaUploadProblem = "";

      if (!upload.filename.endsWith(".bin") && !upload.filename.endsWith(".BIN")) {
        otaUploadRejected = true;
        otaUploadProblem = "Filename must end in .bin";
        break;
      }

      if (coreMutex == nullptr || xSemaphoreTake(coreMutex, 0) != pdTRUE) {
        otaUploadRejected = true;
        otaUploadProblem = "Radio/session operation is active. Wait a few seconds and retry.";
        break;
      }
      otaUploadInProgress = true;

      if (esp_ota_get_next_update_partition(nullptr) == nullptr) {
        otaUploadRejected = true;
        otaUploadProblem = "No OTA application partition. Check the board partition scheme.";
        releaseCoreMutexIfHeldByUpload();
        break;
      }

      if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
        Update.printError(Serial);
        otaUploadRejected = true;
        otaUploadProblem = "Could not begin update. Check OTA partition space.";
        releaseCoreMutexIfHeldByUpload();
      } else {
        Serial.printf("[OTA] Upload started: %s\n", upload.filename.c_str());
      }
      break;
    }

    case UPLOAD_FILE_WRITE: {
      if (otaUploadRejected) break;
      if (!otaUploadInProgress) {
        otaUploadRejected = true;
        otaUploadProblem = "Upload session was not initialized.";
        break;
      }
      const size_t written = Update.write(upload.buf, upload.currentSize);
      if (written != upload.currentSize) {
        Update.printError(Serial);
        otaUploadRejected = true;
        otaUploadProblem = "Flash write incomplete.";
        Update.abort();
        releaseCoreMutexIfHeldByUpload();
      }
      break;
    }

    case UPLOAD_FILE_END:
      if (!otaUploadRejected && otaUploadInProgress) {
        if (!Update.end(true)) {
          Update.printError(Serial);
          otaUploadRejected = true;
          otaUploadProblem = "Update finalization or image validation failed.";
        } else {
          Serial.printf("[OTA] Image accepted; bytes written: %u\n",
                        static_cast<unsigned>(upload.totalSize));
        }
      } else if (otaUploadInProgress) {
        Update.abort();
      }
      releaseCoreMutexIfHeldByUpload();
      break;

    case UPLOAD_FILE_ABORTED:
      Update.abort();
      otaUploadRejected = true;
      otaUploadProblem = "Upload aborted.";
      releaseCoreMutexIfHeldByUpload();
      Serial.println("[OTA] Upload aborted.");
      break;

    default:
      break;
  }
}

static void otaUploadResult() {
  if (otaUploadRejected) {
    otaServer.send(400, "text/plain; charset=utf-8", "Rejected: " + otaUploadProblem);
    return;
  }
  if (Update.hasError()) {
    otaServer.send(500, "text/plain; charset=utf-8", "Firmware update failed; check serial log.");
    return;
  }
  otaServer.send(200, "text/plain; charset=utf-8", "Update accepted. Restarting.");
  Serial.println("[OTA] Update accepted; restarting.");
  delay(500);
  ESP.restart();
}

static void otaNotFound() {
  otaServer.sendHeader("Location", "/", true);
  otaServer.send(302, "text/plain", "Open the maintenance page.");
}

static void otaTask(void*) {
  otaServer.begin();
  Serial.printf("[OTA] WLAN maintenance ready at http://%s/\n",
                WiFi.softAPIP().toString().c_str());
  for (;;) {
    otaServer.handleClient();
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

static bool otaBegin() {
  WiFi.mode(WIFI_AP);
  WiFi.setSleep(false);
  if (!WiFi.softAP(OTA_AP_SSID, OTA_AP_PASSWORD)) {
    Serial.println("[OTA] ERROR: failed to start maintenance access point.");
    return false;
  }

  otaServer.on("/", HTTP_GET, otaRoot);
  otaServer.on("/update", HTTP_POST, otaUploadResult, otaUploadStream);
  otaServer.onNotFound(otaNotFound);

  BaseType_t taskOk = xTaskCreatePinnedToCore(
    otaTask, "ota-web", 8192, nullptr, 1, nullptr, 0
  );
  if (taskOk != pdPASS) {
    Serial.println("[OTA] ERROR: could not create OTA service task.");
    return false;
  }
  return true;
}

// -------------------- EMF1 v1 core --------------------
// Record format: [field_id][type_code][big-endian value bytes], after leading 0xE1.
// Canonical field map for this reference node:
//   0x01 temperature: 0x24 signed int16, scale 0.01 °C
//   0x02 humidity:    0x23 unsigned int16, scale 0.01 %RH
//   0x03 moisture:    0x11 unsigned int16, raw ADC counts
static uint8_t emfBuffer[EMF1_MAX_APP_BYTES];
static bool emfSeen[256];
static size_t emfLength = 0;

static bool emfTypeSpec(uint8_t type, uint8_t& width, bool& isSigned, double& scale, bool& isBool) {
  isSigned = false;
  isBool = false;
  scale = 1.0;
  switch (type) {
    case 0x01: width = 1; isBool = true; return true;
    case 0x02: width = 1; return true;
    case 0x03: width = 1; isSigned = true; return true;
    case 0x11: width = 2; return true;
    case 0x12: width = 2; isSigned = true; return true;
    case 0x21: width = 2; scale = 0.1; return true;
    case 0x22: width = 2; isSigned = true; scale = 0.1; return true;
    case 0x23: width = 2; scale = 0.01; return true;
    case 0x24: width = 2; isSigned = true; scale = 0.01; return true;
    case 0x31: width = 4; return true;
    case 0x32: width = 4; isSigned = true; return true;
    default: return false;
  }
}

static uint8_t emfCanonicalType(uint8_t id) {
  switch (id) {
    case 0x01: return 0x24;
    case 0x02: return 0x23;
    case 0x03: return 0x11;
    case 0x10: return 0x31;
    default: return 0; // Additional IDs require an explicit registry entry.
  }
}

static void emfBegin() {
  emfLength = 1;
  emfBuffer[0] = EMF1_MAGIC;
  memset(emfSeen, 0, sizeof(emfSeen));
}

static bool emfAppendRaw(uint8_t id, uint8_t type, int64_t raw) {
  uint8_t width = 0;
  bool isSigned = false, isBool = false;
  double scale = 1.0;

  if (id == 0 || id == 0xFF || emfSeen[id] || !emfTypeSpec(type, width, isSigned, scale, isBool)) return false;
  const uint8_t canonical = emfCanonicalType(id);
  if (canonical != 0 && canonical != type) return false;
  if (isBool && raw != 0 && raw != 1) return false;

  const int bits = static_cast<int>(width) * 8;
  const int64_t signedMin = -(INT64_C(1) << (bits - 1));
  const int64_t signedMax = (INT64_C(1) << (bits - 1)) - 1;
  const uint64_t unsignedMax = (UINT64_C(1) << bits) - 1;
  if (isSigned && (raw < signedMin || raw > signedMax)) return false;
  if (!isSigned && (raw < 0 || static_cast<uint64_t>(raw) > unsignedMax)) return false;
  if (emfLength + 2 + width > sizeof(emfBuffer)) return false;

  emfBuffer[emfLength++] = id;
  emfBuffer[emfLength++] = type;
  const uint32_t encoded = static_cast<uint32_t>(raw);
  for (int shift = (static_cast<int>(width) - 1) * 8; shift >= 0; shift -= 8) {
    emfBuffer[emfLength++] = static_cast<uint8_t>(encoded >> shift);
  }
  emfSeen[id] = true;
  return true;
}

static bool emfAddValue(uint8_t id, uint8_t type, double engineeringValue) {
  uint8_t width = 0;
  bool isSigned = false, isBool = false;
  double scale = 1.0;
  if (!isfinite(engineeringValue) || !emfTypeSpec(type, width, isSigned, scale, isBool)) return false;
  if (isBool) {
    if (engineeringValue != 0.0 && engineeringValue != 1.0) return false;
    return emfAppendRaw(id, type, static_cast<int64_t>(engineeringValue));
  }
  const double scaled = engineeringValue / scale;
  if (!isfinite(scaled)) return false;
  const double rounded = round(scaled);
  const int bits = static_cast<int>(width) * 8;
  const double low = isSigned ? -pow(2.0, bits - 1) : 0.0;
  const double high = isSigned ? pow(2.0, bits - 1) - 1.0 : pow(2.0, bits) - 1.0;
  if (rounded < low || rounded > high) return false;
  return emfAppendRaw(id, type, static_cast<int64_t>(rounded));
}

static void emfPrintPayload() {
  Serial.printf("[EMF1] FPort=%u length=%u hex=", EMF1_FPORT, static_cast<unsigned>(emfLength));
  for (size_t i = 0; i < emfLength; ++i) Serial.printf("%02X", emfBuffer[i]);
  Serial.println();
}

// -------------------- Sensor/application extension point --------------------
// Sensor drivers are intentionally omitted from this core-first template.
// Add modules later without changing OTAA, persistence, OTA, scheduler or EMF1 encoding.

// -------------------- LoRaWAN core --------------------
static void persistLoRaWANStateLocked() {
  const size_t nonces = preferences.putBytes(
    "nonces", loraNode.getBufferNonces(), RADIOLIB_LORAWAN_NONCES_BUF_SIZE);
  const size_t session = preferences.putBytes(
    "session", loraNode.getBufferSession(), RADIOLIB_LORAWAN_SESSION_BUF_SIZE);
  if (nonces != RADIOLIB_LORAWAN_NONCES_BUF_SIZE ||
      session != RADIOLIB_LORAWAN_SESSION_BUF_SIZE) {
    Serial.println("[LoRaWAN] WARNING: could not persist complete session state.");
  }
}

static bool activationSucceeded(int state) {
  return state == RADIOLIB_ERR_NONE ||
         state == RADIOLIB_LORAWAN_NEW_SESSION ||
         state == RADIOLIB_LORAWAN_SESSION_RESTORED;
}

static uint64_t currentNetworkTimeMs() {
  return networkTimeBaseMs + static_cast<uint32_t>(millis() - localMillisAtSync);
}

static void scheduleAssignedUplinkSlot() {
  if (!networkTimeValid || !hasSentFirstUplink || !nextSlotScheduled) return;
  nextSlotScheduled = false;

  int32_t remainingMs = static_cast<int32_t>(nextSlotLocalMs - millis());
  if (remainingMs > 0 && remainingMs <= static_cast<int32_t>(SLOT_PERIOD_MS)) {
    Serial.printf("[Scheduler] Aligning to assigned UTC slot (+%lu s, waiting %ld ms)...\n",
                  static_cast<unsigned long>(SLOT_OFFSET_MS / 1000UL),
                  static_cast<long>(remainingMs));
    delay(static_cast<uint32_t>(remainingMs));
  } else {
    Serial.printf("[Scheduler] Assigned UTC slot second: %lu s into each %lu s cycle.\n",
                  static_cast<unsigned long>(SLOT_OFFSET_MS / 1000UL),
                  static_cast<unsigned long>(SLOT_PERIOD_MS / 1000UL));
  }
}

static void requestAndUpdateNetworkTime(bool requestWasSent) {
  if (!requestWasSent) return;

  uint32_t unixSeconds = 0;
  uint16_t milliseconds = 0;
  const int timeState = loraNode.getMacDeviceTimeAns(&unixSeconds, &milliseconds, true);
  if (timeState == RADIOLIB_ERR_NONE) {
    const bool wasSynced = networkTimeValid;
    networkTimeBaseMs = static_cast<uint64_t>(unixSeconds) * 1000ULL + milliseconds;
    localMillisAtSync = millis();
    networkTimeValid = true;
    uplinksSinceTimeRequest = 1;
    Serial.printf("[Scheduler] Network time synchronized: %lu s + %u ms.\n",
                  static_cast<unsigned long>(unixSeconds), milliseconds);
    if (!wasSynced) Serial.println("[Scheduler] Scheduled slots enabled.");
  } else {
    uplinksSinceTimeRequest = TIME_SYNC_EVERY_UPLINKS;
    Serial.printf("[Scheduler] DeviceTimeAns unavailable, code %d\n", timeState);
    if (!networkTimeValid) {
      Serial.println("[Scheduler] Staying in randomized fallback until time sync succeeds.");
    }
  }
}

static bool beginRadioAndLoRaWAN() {
  Serial.println("[LoRa] Initializing SX1262...");
  SPI.begin(RADIO_SCK_PIN, RADIO_MISO_PIN, RADIO_MOSI_PIN, RADIO_NSS_PIN);
  int state = radio.begin();
  if (state != RADIOLIB_ERR_NONE) {
    Serial.printf("[LoRa] ERROR: radio.begin() failed, code %d\n", state);
    return false;
  }
  radioReady = true;
  Serial.println("[LoRa] SX1262 initialized.");

  if (!preferences.begin("lorawan", false)) {
    Serial.println("[LoRaWAN] ERROR: Preferences storage unavailable.");
    return false;
  }

  size_t nonceLength = preferences.getBytesLength("nonces");
  size_t sessionLength = preferences.getBytesLength("session");
  uint8_t nonceBuffer[RADIOLIB_LORAWAN_NONCES_BUF_SIZE] = {};
  uint8_t sessionBuffer[RADIOLIB_LORAWAN_SESSION_BUF_SIZE] = {};
  bool haveNonces = nonceLength == sizeof(nonceBuffer) &&
    preferences.getBytes("nonces", nonceBuffer, sizeof(nonceBuffer)) == sizeof(nonceBuffer);
  bool haveSession = sessionLength == sizeof(sessionBuffer) &&
    preferences.getBytes("session", sessionBuffer, sizeof(sessionBuffer)) == sizeof(sessionBuffer);

  state = loraNode.beginOTAA(JOIN_EUI, DEV_EUI, NWK_KEY, APP_KEY);
  if (state != RADIOLIB_ERR_NONE) {
    Serial.printf("[LoRaWAN] ERROR: beginOTAA failed, code %d\n", state);
    return false;
  }

  Serial.printf("[LoRa] Waiting %lu ms for radio & rail settle before OTAA activation...\n",
                static_cast<unsigned long>(RADIO_SETUP_DELAY_MS));
  xSemaphoreGive(coreMutex);
  vTaskDelay(pdMS_TO_TICKS(RADIO_SETUP_DELAY_MS));
  if (xSemaphoreTake(coreMutex, portMAX_DELAY) != pdTRUE) {
    Serial.println("[Core] FATAL: could not reacquire mutex after radio settle delay.");
    return false;
  }

  if (haveNonces && haveSession) {
    state = loraNode.setBufferNonces(nonceBuffer);
    if (state == RADIOLIB_ERR_NONE) state = loraNode.setBufferSession(sessionBuffer);
    if (state == RADIOLIB_ERR_NONE) state = loraNode.activateOTAA();
    if (state == RADIOLIB_ERR_NONE || state == RADIOLIB_LORAWAN_SESSION_RESTORED) {
      Serial.println("[LoRaWAN] Saved session restored.");
      lorawanJoined = true;
      persistLoRaWANStateLocked();
      return true;
    }
    Serial.printf("[LoRaWAN] Saved session restore failed, code %d; fresh join follows.\n", state);
  } else {
    Serial.println("[LoRaWAN] No complete saved session; fresh OTAA join required.");
  }

  preferences.remove("session");
  state = loraNode.beginOTAA(JOIN_EUI, DEV_EUI, NWK_KEY, APP_KEY);
  if (state != RADIOLIB_ERR_NONE) {
    Serial.printf("[LoRaWAN] ERROR: beginOTAA reinitialization failed, code %d\n", state);
    return false;
  }
  if (haveNonces) {
    state = loraNode.setBufferNonces(nonceBuffer);
    if (state != RADIOLIB_ERR_NONE) {
      Serial.printf("[LoRaWAN] Stored nonce state rejected, code %d; inspect device provisioning.\n", state);
      preferences.remove("nonces");
    }
  }

  for (;;) {
    Serial.println("[LoRaWAN] Attempting OTAA join...");
    state = loraNode.activateOTAA();
    if (activationSucceeded(state)) {
      Serial.println("[LoRaWAN] OTAA join successful.");
      lorawanJoined = true;
      persistLoRaWANStateLocked();
      return true;
    }
    Serial.printf("[LoRaWAN] Join failed, code %d. Retrying in %lu ms.\n", state,
                  static_cast<unsigned long>(JOIN_RETRY_MS));
    preferences.putBytes("nonces", loraNode.getBufferNonces(), RADIOLIB_LORAWAN_NONCES_BUF_SIZE);

    // Release the shared flash/radio lock during backoff. This allows an OTA
    // upload to acquire the lock and finish even if OTAA is failing repeatedly.
    xSemaphoreGive(coreMutex);
    vTaskDelay(pdMS_TO_TICKS(JOIN_RETRY_MS));
    if (xSemaphoreTake(coreMutex, portMAX_DELAY) != pdTRUE) {
      Serial.println("[Core] FATAL: could not reacquire mutex after join backoff.");
      return false;
    }
  }
}

static void waitForAssignedUplinkSlotInLoop() {
  if (!networkTimeValid || !hasSentFirstUplink) {
    nextSlotScheduled = false;
    return;
  }

  uint64_t nowUtcMs = currentNetworkTimeMs();
  uint64_t targetUtcMs = (nowUtcMs / SLOT_PERIOD_MS) * SLOT_PERIOD_MS + SLOT_OFFSET_MS;
  if (targetUtcMs <= nowUtcMs + SLOT_GUARD_MS) {
    targetUtcMs += SLOT_PERIOD_MS;
  }

  nextSlotLocalMs = localMillisAtSync + static_cast<uint32_t>(targetUtcMs - networkTimeBaseMs);
  nextSlotScheduled = true;

  int32_t waitMs = static_cast<int32_t>(nextSlotLocalMs - millis()) - 2500;
  if (waitMs > 0) {
    Serial.printf("[Scheduler] Waiting %.1f s until pre-slot sensor sample window...\n", waitMs / 1000.0f);
    delay(static_cast<uint32_t>(waitMs));
  }
}

static void buildApplicationFields() {
  // Core-only diagnostic field: uptime in seconds, uint32, EMF1 type 0x31.
  // Sensor modules will append their registered fields here in a later phase.
  emfBegin();
  const uint32_t uptimeSeconds = millis() / 1000UL;
  if (!emfAddValue(0x10, 0x31, static_cast<double>(uptimeSeconds))) {
    Serial.println("[EMF1] Core uptime field rejected; uplink skipped.");
  }
}

static void readAndSendEMF1() {
  buildApplicationFields();
  if (emfLength <= 1 || emfLength > EMF1_MAX_APP_BYTES) {
    Serial.println("[EMF1] No valid fields or payload exceeds configured limit; uplink skipped.");
    delay(10000);
    return;
  }
  emfPrintPayload();

  // Wait outside coreMutex until the exact UTC slot mark so OTA stays responsive
  // and sendReceive() transmits immediately without staging FIFO across SX1262 sleep.
  scheduleAssignedUplinkSlot();

  if (xSemaphoreTake(coreMutex, portMAX_DELAY) != pdTRUE) {
    Serial.println("[Core] ERROR: could not acquire radio/flash mutex.");
    delay(5000);
    return;
  }

  const bool requestDeviceTime = (!networkTimeValid ||
                                  uplinksSinceTimeRequest >= TIME_SYNC_EVERY_UPLINKS);
  if (requestDeviceTime) {
    const int requestState = loraNode.sendMacCommandReq(RADIOLIB_LORAWAN_MAC_DEVICE_TIME);
    if (requestState < RADIOLIB_ERR_NONE) {
      Serial.printf("[Scheduler] Could not queue DeviceTimeReq, code %d\n", requestState);
    } else {
      Serial.println("[Scheduler] DeviceTimeReq queued.");
    }
  }

  const int txState = loraNode.sendReceive(emfBuffer, emfLength, EMF1_FPORT);
  if (requestDeviceTime) requestAndUpdateNetworkTime(true);
  else uplinksSinceTimeRequest++;
  persistLoRaWANStateLocked();
  xSemaphoreGive(coreMutex);
  hasSentFirstUplink = true;

  if (txState == RADIOLIB_ERR_NONE) {
    Serial.println("[LoRaWAN] Uplink sent; no downlink payload.");
  } else if (txState > 0) {
    Serial.printf("[LoRaWAN] Uplink completed; downlink payload bytes received: %d\n", txState);
  } else {
    Serial.printf("[LoRaWAN] sendReceive failed, code %d\n", txState);
    if (txState == RADIOLIB_ERR_NETWORK_NOT_JOINED) lorawanJoined = false;
    delay(10000);
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.printf("\n[NodeCore] Booting %s\n", NODE_NAME);

  bool allZeroKeys = true;
  for (size_t i = 0; i < 16; ++i) {
    if (NWK_KEY[i] != 0 || APP_KEY[i] != 0) { allZeroKeys = false; break; }
  }
  if (JOIN_EUI == 0 || DEV_EUI == 0 || allZeroKeys) {
    Serial.println("[Provisioning] ERROR: replace JOIN_EUI, DEV_EUI, NWK_KEY and APP_KEY placeholders before use.");
    while (true) delay(1000);
  }

  coreMutex = xSemaphoreCreateMutex();
  if (coreMutex == nullptr) {
    Serial.println("[Core] FATAL: mutex allocation failed.");
    while (true) delay(1000);
  }

  // Initialize Radio FIRST on clean power rail (matching working baseline)
  if (xSemaphoreTake(coreMutex, portMAX_DELAY) != pdTRUE) {
    Serial.println("[Core] FATAL: could not acquire core mutex.");
    while (true) delay(1000);
  }
  const bool started = beginRadioAndLoRaWAN();
  xSemaphoreGive(coreMutex);

  if (!started) {
    Serial.println("[LoRaWAN] Radio/session setup failed. Check wiring, power, and SPI pins.");
    while (true) {
      Serial.println("[Core] Radio setup is not ready; reboot to retry after checking wiring/config.");
      delay(10000);
    }
  }

  // Start WLAN maintenance AP after radio is initialized
  if (!otaBegin()) {
    Serial.println("[OTA] WARNING: maintenance AP/task did not start; normal LoRaWAN boot continues.");
  }
  Serial.println("[NodeCore] Core initialized: WLAN OTA + LoRaWAN + UTC slots + EMF1.");

  randomSeed(static_cast<uint32_t>(esp_random()));
  const uint32_t startupDelay = FIRST_UPLINK_DELAY_MS + static_cast<uint32_t>(random(0, 5001));
  Serial.printf("[Scheduler] UTC slot is second %lu in each %lu s interval.\n",
                static_cast<unsigned long>(SLOT_OFFSET_MS / 1000UL),
                static_cast<unsigned long>(SLOT_PERIOD_MS / 1000UL));
  Serial.printf("[Scheduler] First uplink delayed by %.1f seconds to reduce synchronized transmissions.\n",
                startupDelay / 1000.0f);
  delay(startupDelay);
}

void loop() {
  if (!radioReady || !lorawanJoined) {
    delay(1000);
    return;
  }

  // After the first uplink, send on this node's UTC-aligned slot.
  // Until DeviceTime sync succeeds, use randomized fallback timing.
  if (hasSentFirstUplink) {
    if (!networkTimeValid) {
      const uint32_t fallbackDelay = SLOT_PERIOD_MS + static_cast<uint32_t>(random(0, 8001));
      Serial.printf("[Scheduler] Unsynchronized fallback wait: %.1f seconds.\n",
                    fallbackDelay / 1000.0f);
      delay(fallbackDelay);
    } else {
      waitForAssignedUplinkSlotInLoop();
    }
  }

  readAndSendEMF1();
}
