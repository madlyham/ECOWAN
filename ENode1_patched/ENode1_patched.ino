#include <RadioLib.h>
#include <DHT.h>
#include <Preferences.h>
#include <math.h>

#define NSS_PIN   5
#define DIO1_PIN  33
#define RST_PIN   27
#define BUSY_PIN  26
#define DHT_PIN   4
#define HW038_PIN 34

#define DHTTYPE DHT11 

// --- Initialize Components ---
SX1262 radio = new Module(NSS_PIN, DIO1_PIN, RST_PIN, BUSY_PIN);
LoRaWANNode node(&radio, &AS923); 
DHT dht(DHT_PIN, DHTTYPE);
Preferences prefs;  

// --- LoRaWAN Credentials (OTAA) ---
uint64_t joinEUI = 0x827b193dd87983cfULL;
uint64_t devEUI  = 0xd251f42dc4918f96ULL; 

uint8_t nwkKey[] = { 0x97, 0x0d, 0xd3, 0x8b, 0xd8, 0x9c, 0x7a, 0xcc, 0xb2, 0xdb, 0xa0, 0x4d, 0x1a, 0x42, 0x94, 0x21 };
uint8_t appKey[] = { 0x97, 0x0d, 0xd3, 0x8b, 0xd8, 0x9c, 0x7a, 0xcc, 0xb2, 0xdb, 0xa0, 0x4d, 0x1a, 0x42, 0x94, 0x21 };

// Persist both OTAA counters and session after joins/transmissions. In particular,
// the frame counter must survive resets to avoid replay/rejoin problems.
void persistLoRaWANState() {
  uint8_t *noncesBuffer = node.getBufferNonces();
  uint8_t *sessionBuffer = node.getBufferSession();
  size_t savedNonces = prefs.putBytes("nonces", noncesBuffer, RADIOLIB_LORAWAN_NONCES_BUF_SIZE);
  size_t savedSession = prefs.putBytes("session", sessionBuffer, RADIOLIB_LORAWAN_SESSION_BUF_SIZE);
  if (savedNonces != RADIOLIB_LORAWAN_NONCES_BUF_SIZE || savedSession != RADIOLIB_LORAWAN_SESSION_BUF_SIZE) {
    Serial.println("[LoRaWAN] WARNING: session state was not fully saved to flash.");
  }
}

bool activationSucceeded(int state) {
  return state == RADIOLIB_ERR_NONE ||
         state == RADIOLIB_LORAWAN_NEW_SESSION ||
         state == RADIOLIB_LORAWAN_SESSION_RESTORED;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  dht.begin();
  pinMode(HW038_PIN, INPUT);

  Serial.println("\n[LoRa] Initializing radio...");
  int state = radio.begin();
  if (state != RADIOLIB_ERR_NONE) {
    Serial.print("[LoRa] Radio initialization failed, code ");
    Serial.println(state);
    while (true) delay(1000);
  }

  if (!prefs.begin("lorawan", false)) {
    Serial.println("[LoRaWAN] ERROR: could not open Preferences storage.");
    while (true) delay(1000);
  }

  size_t nonceLength = prefs.getBytesLength("nonces");
  size_t sessionLength = prefs.getBytesLength("session");
  uint8_t noncesBuffer[RADIOLIB_LORAWAN_NONCES_BUF_SIZE] = {};
  uint8_t sessionBuffer[RADIOLIB_LORAWAN_SESSION_BUF_SIZE] = {};
  bool haveValidNonces = nonceLength == RADIOLIB_LORAWAN_NONCES_BUF_SIZE &&
      prefs.getBytes("nonces", noncesBuffer, sizeof(noncesBuffer)) == sizeof(noncesBuffer);
  bool haveValidSession = sessionLength == RADIOLIB_LORAWAN_SESSION_BUF_SIZE &&
      prefs.getBytes("session", sessionBuffer, sizeof(sessionBuffer)) == sizeof(sessionBuffer);
  bool restored = false;

  Serial.println("[LoRaWAN] Checking saved OTAA state...");
  state = node.beginOTAA(joinEUI, devEUI, nwkKey, appKey);
  if (state != RADIOLIB_ERR_NONE) {
    Serial.print("[LoRaWAN] Credential/configuration initialization failed, code ");
    Serial.println(state);
    while (true) delay(1000);
  }

  if (haveValidNonces && haveValidSession) {
    int nonceState = node.setBufferNonces(noncesBuffer);
    int sessionState = nonceState == RADIOLIB_ERR_NONE ? node.setBufferSession(sessionBuffer) : nonceState;
    if (nonceState == RADIOLIB_ERR_NONE && sessionState == RADIOLIB_ERR_NONE) {
      state = node.activateOTAA();
      if (state == RADIOLIB_ERR_NONE || state == RADIOLIB_LORAWAN_SESSION_RESTORED) {
        Serial.println("[LoRaWAN] Saved session restored successfully.");
        restored = true;
        persistLoRaWANState();
      } else {
        Serial.print("[LoRaWAN] Saved session restore failed, code ");
        Serial.println(state);
      }
    } else {
      Serial.print("[LoRaWAN] Saved state failed validation (nonces=");
      Serial.print(nonceState);
      Serial.print(", session=");
      Serial.print(sessionState);
      Serial.println("). Will attempt a fresh OTAA session while preserving valid nonces.");
    }
  } else {
    Serial.print("[LoRaWAN] Saved buffers incomplete/invalid (nonces bytes=");
    Serial.print(nonceLength);
    Serial.print(", session bytes=");
    Serial.print(sessionLength);
    Serial.println("). Will attempt OTAA join.");
  }

  if (!restored) {
    // Keep a valid nonce buffer so a fresh join does not restart the DevNonce sequence.
    // A bad/incomplete session is removed; it is not safe to pass it back to RadioLib.
    prefs.remove("session");
    if (!haveValidNonces) prefs.remove("nonces");
    state = node.beginOTAA(joinEUI, devEUI, nwkKey, appKey);
    if (state != RADIOLIB_ERR_NONE) {
      Serial.print("[LoRaWAN] Could not reinitialize OTAA, code ");
      Serial.println(state);
      while (true) delay(1000);
    }
    if (haveValidNonces) {
      state = node.setBufferNonces(noncesBuffer);
      if (state != RADIOLIB_ERR_NONE) {
        Serial.print("[LoRaWAN] Stored DevNonce data was rejected, code ");
        Serial.println(state);
        prefs.remove("nonces");
      }
    }

    while (true) {
      Serial.println("[LoRaWAN] Attempting OTAA join...");
      state = node.activateOTAA();
      if (activationSucceeded(state)) {
        Serial.println("[LoRaWAN] OTAA join successful.");
        persistLoRaWANState();
        break;
      }
      Serial.print("[LoRaWAN] Join failed, code ");
      Serial.print(state);
      Serial.println(". Retrying in 30 seconds; check ChirpStack join logs and device keys.");
      // Store the nonce buffer after each attempt if RadioLib advanced it.
      prefs.putBytes("nonces", node.getBufferNonces(), RADIOLIB_LORAWAN_NONCES_BUF_SIZE);
      delay(30000);
    }
  }
}

void loop() {
  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  int moisture = analogRead(HW038_PIN);

  bool dhtOk = !isnan(humidity) && !isnan(temperature);
  if (!dhtOk) {
    // DHT11 reads can fail transiently. Retry once after its minimum read interval.
    Serial.println("[Sensor] DHT read failed; retrying in 2 seconds.");
    delay(2100);
    humidity = dht.readHumidity();
    temperature = dht.readTemperature();
    dhtOk = !isnan(humidity) && !isnan(temperature);
  }
  if (!dhtOk) {
    // Do NOT return here. The original early return skipped the radio send and the
    // 60-second delay, so a failed DHT could make the node look completely silent.
    Serial.println("[Sensor] DHT still unavailable. Sending moisture with invalid-DHT markers.");
  }

  // Six-byte protocol remains compatible: signed temp x100, humidity x100, raw ADC.
  // -32768 and 65535 explicitly mean a missing DHT reading; dashboard recognizes them.
  int16_t tempInt = dhtOk ? (int16_t)lroundf(temperature * 100.0f) : (int16_t)-32768;
  uint16_t humInt = dhtOk ? (uint16_t)lroundf(humidity * 100.0f) : (uint16_t)65535;
  uint16_t moistInt = (uint16_t)constrain(moisture, 0, 65535);
  uint8_t payload[6] = {
    (uint8_t)((uint16_t)tempInt >> 8), (uint8_t)tempInt,
    (uint8_t)(humInt >> 8), (uint8_t)humInt,
    (uint8_t)(moistInt >> 8), (uint8_t)moistInt
  };

  Serial.println("[LoRaWAN] Sending sensor frame...");
  int txState = node.sendReceive(payload, sizeof(payload));
  if (txState == RADIOLIB_ERR_NONE) {
    Serial.println("[LoRaWAN] Uplink procedure completed.");
  } else if (txState > 0) {
    Serial.print("[LoRaWAN] Uplink completed; downlink payload bytes received: ");
    Serial.println(txState);
  } else {
    Serial.print("[LoRaWAN] sendReceive reported code ");
    Serial.println(txState);
  }

  // Save after EVERY attempt. A frame counter can advance even when no downlink arrives.
  persistLoRaWANState();
  Serial.println("[Sensor] Next sample in about 60 seconds.");
  delay(60000);
}
