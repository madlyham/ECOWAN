'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const htmlPath = path.join(__dirname, 'index.html');
const catalogPath = path.join(__dirname, 'parts_catalog.json');
const html = fs.readFileSync(htmlPath, 'utf8');
const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'));

// Extract the main application <script> from index.html (the last <script> block)
const scriptStart = html.lastIndexOf('<script>');
const scriptEnd = html.lastIndexOf('</script>');
assert.ok(scriptStart > 0 && scriptEnd > scriptStart, 'Could not locate main <script> in index.html');
const mainScript = html.slice(scriptStart + '<script>'.length, scriptEnd);

function makeElementStub() {
  return {
    value: '', textContent: '', hidden: false, style: {}, children: [], options: [],
    addEventListener: function () {}, setAttribute: function () {}, getAttribute: function () { return ''; },
    appendChild: function (c) { this.children.push(c); return c; },
    querySelectorAll: function () { return []; }, querySelector: function () { return makeElementStub(); }
  };
}

const sandbox = {
  window: {},
  document: {
    documentElement: { getAttribute: function () { return 'dark'; }, setAttribute: function () {} },
    querySelector: function () { return makeElementStub(); },
    querySelectorAll: function () { return []; },
    createElement: function () { return makeElementStub(); },
    createElementNS: function () { return makeElementStub(); },
    body: makeElementStub()
  },
  localStorage: { getItem: function () { return null; }, setItem: function () {} },
  fetch: function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ required: false, authenticated: true }); } }); },
  setInterval: function () {},
  setTimeout: function () {},
  clearTimeout: function () {},
  URL: { createObjectURL: function () { return ''; }, revokeObjectURL: function () {} },
  Blob: function (parts) { this.parts = parts; },
  console: console,
  Math: Math, Date: Date, JSON: JSON, Object: Object, Array: Array, String: String, Number: Number, Boolean: Boolean, RegExp: RegExp, Error: Error, Promise: Promise
};
sandbox.window = sandbox;

// Expose internal codegen functions from the IIFE for unit testing
const instrumentedScript = mainScript.replace(
  /bootstrapAuth\(\);\s*\}\)\(\);/,
  'window.__codegenTestApi = { CFG, CFG_BOARD_PROFILES, CFG_REFERENCE_NODES, cfgNormalizePayload, cfgConfigFingerprint, cfgValidate, cfgInspectSensorIssues, cfgBuildCodecFields, cfgBuildFieldPackage, cfgOtaCorePrefix, cfgGeneratedEmf1Application, cfgAssembledSketchSource, cfgSketchName };\n})();'
);

vm.runInNewContext(instrumentedScript, sandbox, { filename: 'index.html', timeout: 5000 });
const api = sandbox.__codegenTestApi;
assert.ok(api, 'Codegen test API was not exposed');
api.CFG.catalog = catalog;

// 1. Test ENode1 Reference Profile Generation
const enode1Profile = {
  name: 'ENode1',
  devEui: 'd251f42dc4918f96',
  joinEui: '827b193dd87983cf',
  nwkKey: '00112233445566778899aabbccddeeff',
  appKey: '00112233445566778899aabbccddeeff',
  otaApSsid: 'ECOWAN-ENode1',
  otaApPassword: 'ECOWAN-ENode1',
  reportingIntervalSeconds: 60,
  utcSlotSecond: 5,
  firmwareProfile: 'esp32_lorawan',
  placement: 'Test Site Alpha',
  sensors: [
    { id: 'enode1-dht11', partId: 'dht11', label: 'DHT11', protocol: 'gpio', port: 'GPIO4', fields: [{ key: 'temperature', label: 'Temperature', unit: '°C' }, { key: 'humidity', label: 'Humidity', unit: '%RH' }] },
    { id: 'enode1-hw038', partId: 'hw038_moisture', label: 'HW-038 soil moisture', protocol: 'analog', port: 'GPIO34', fields: [{ key: 'moisture', label: 'Moisture ADC', unit: 'ADC' }] }
  ]
};

const enode1Source1 = api.cfgAssembledSketchSource(enode1Profile);
const enode1Source2 = api.cfgAssembledSketchSource(enode1Profile);
assert.strictEqual(enode1Source1, enode1Source2, 'Code generation must be 100% deterministic for identical input');
assert.ok(enode1Source1.includes('#define NODE_NAME "ENode1"'));
assert.ok(enode1Source1.includes('#define OTA_AP_SSID "ECOWAN-ENode1"'), 'Custom otaApSsid must be emitted into OTA_AP_SSID');
assert.ok(enode1Source1.includes('#define OTA_AP_PASSWORD "ECOWAN-ENode1"'), 'Custom otaApPassword must be emitted into OTA_AP_PASSWORD');
assert.ok(enode1Source1.includes('#define ECOWAN_UTC_SLOT_SECOND 5UL'));
assert.ok(enode1Source1.includes('static constexpr uint32_t FIRST_UPLINK_DELAY_MS = 10000UL;'));
assert.ok(!enode1Source1.includes('loraNode.scheduleTransmission'), 'Generated sketch must NOT call loraNode.scheduleTransmission (causes SX1262 warm-sleep FIFO wipe)');
assert.ok(enode1Source1.includes('nextSlotScheduled'), 'Generated sketch must use immediate-TX slot alignment');
assert.strictEqual((enode1Source1.match(/void setup\(\)/g) || []).length, 1, 'Exactly one setup() must exist');
assert.strictEqual((enode1Source1.match(/void loop\(\)/g) || []).length, 1, 'Exactly one loop() must exist');

// 2. Test ENode2 Reference Profile Generation (distinct identity, slot 35s, startup delay 35000UL)
const enode2Profile = {
  name: 'ENode2',
  devEui: 'b1e299ff3eed5d1d',
  joinEui: '6bbf280ebd665410',
  nwkKey: 'ffeeddccbbaa99887766554433221100',
  appKey: 'ffeeddccbbaa99887766554433221100',
  otaApSsid: 'ECOWAN-ENode2',
  otaApPassword: 'ECOWAN-ENode2',
  reportingIntervalSeconds: 60,
  utcSlotSecond: 35,
  firmwareProfile: 'esp32_lorawan',
  placement: 'Test Site Beta',
  sensors: [
    { id: 'enode2-dht11', partId: 'dht11', label: 'DHT11', protocol: 'gpio', port: 'GPIO4', fields: [{ key: 'temperature', label: 'Temperature', unit: '°C' }, { key: 'humidity', label: 'Humidity', unit: '%RH' }] },
    { id: 'enode2-hw038', partId: 'hw038_moisture', label: 'HW-038 soil moisture', protocol: 'analog', port: 'GPIO34', fields: [{ key: 'moisture', label: 'Moisture ADC', unit: 'ADC' }] }
  ]
};

const enode2Source = api.cfgAssembledSketchSource(enode2Profile);
assert.ok(enode2Source.includes('#define NODE_NAME "ENode2"'));
assert.ok(enode2Source.includes('#define OTA_AP_SSID "ECOWAN-ENode2"'));
assert.ok(enode2Source.includes('0x6BBF280EBD665410ULL'));
assert.ok(enode2Source.includes('0xB1E299FF3EED5D1DULL'));
assert.ok(enode2Source.includes('#define ECOWAN_UTC_SLOT_SECOND 35UL'));
assert.ok(enode2Source.includes('static constexpr uint32_t FIRST_UPLINK_DELAY_MS = 35000UL;'), 'ENode2 first uplink delay must follow its 35s slot offset');
assert.notStrictEqual(api.cfgConfigFingerprint(enode1Profile), api.cfgConfigFingerprint(enode2Profile), 'ENode1 and ENode2 must have distinct config fingerprints');
assert.ok(!enode2Source.includes('pinMode(SENSOR_2_PIN'), 'Input-only GPIO34 must never have pinMode() called on it');

// 3. Test Multi-Sensor Profile with 4-pin Light Sensor (AO on GPIO32 + D0 on GPIO25) andCore-Only Profile
const lightProfile = Object.assign({}, enode2Profile, {
  name: 'ENode2_Light',
  sensors: enode2Profile.sensors.concat([
    { id: 'enode2-light', partId: 'light_sensor_4pin', label: 'Light AO+DO', protocol: 'analog', port: 'GPIO32', busAddress: 'GPIO25', fields: [{ key: 'light_adc', label: 'Light ADC', unit: 'ADC' }, { key: 'light_digital_state', label: 'Light D0', unit: 'state' }] }
  ])
});
const lightSource = api.cfgAssembledSketchSource(lightProfile);
assert.ok(lightSource.includes('#define SENSOR_3_AO_PIN 32'));
assert.ok(lightSource.includes('#define SENSOR_3_D0_PIN 25'));
assert.ok(lightSource.includes('emfAddBool('));

const coreOnlyProfile = Object.assign({}, enode1Profile, { name: 'CoreHealthNode', sensors: [] });
const coreOnlySource = api.cfgAssembledSketchSource(coreOnlyProfile);
assert.ok(coreOnlySource.includes('emfAddValue(0x10, 0x31, static_cast<double>(uptimeSeconds));'));

// 4. Test Invalid Configuration Rejections
const invalidCases = [
  { name: 'missing name', patch: { name: '' }, match: /node name/i },
  { name: 'all-zero DevEUI', patch: { devEui: '0000000000000000' }, match: /all zeros/i },
  { name: 'short DevEUI', patch: { devEui: '1234' }, match: /16 hexadecimal/i },
  { name: 'bad NwkKey length', patch: { nwkKey: 'abcd' }, match: /32 hexadecimal/i },
  { name: 'short OTA password', patch: { otaApPassword: 'short' }, match: /8 and 63/i },
  { name: 'slot >= interval', patch: { reportingIntervalSeconds: 60, utcSlotSecond: 60 }, match: /UTC Slot Second/i },
  { name: 'gateway controller target', patch: { firmwareProfile: 'gateway_controller' }, match: /not an ESP32/i },
  {
    name: 'radio reserved pin GPIO5',
    patch: { sensors: [{ partId: 'dht11', protocol: 'gpio', port: 'GPIO5', fields: [] }] },
    match: /reserved by the SX1262/i
  },
  {
    name: 'SPI reserved pin GPIO18',
    patch: { sensors: [{ partId: 'dht11', protocol: 'gpio', port: 'GPIO18', fields: [] }] },
    match: /reserved by the SX1262/i
  },
  {
    name: 'DHT11 on input-only GPIO34',
    patch: { sensors: [{ partId: 'dht11', protocol: 'gpio', port: 'GPIO34', fields: [] }] },
    match: /input-only pin/i
  },
  {
    name: 'analog sensor on ADC2 pin GPIO4',
    patch: { sensors: [{ partId: 'hw038_moisture', protocol: 'analog', port: 'GPIO4', fields: [] }] },
    match: /ADC1/i
  },
  {
    name: 'duplicate GPIO4 across two sensors',
    patch: {
      sensors: [
        { partId: 'dht11', protocol: 'gpio', port: 'GPIO4', fields: [] },
        { partId: 'dht11', protocol: 'gpio', port: 'GPIO4', fields: [] }
      ]
    },
    match: /multiple sensors/i
  },
  {
    name: 'light sensor D0 conflicting with DHT11 GPIO4',
    patch: {
      sensors: [
        { partId: 'dht11', protocol: 'gpio', port: 'GPIO4', fields: [] },
        { partId: 'light_sensor_4pin', protocol: 'analog', port: 'GPIO32', busAddress: 'GPIO4', fields: [] }
      ]
    },
    match: /assigned more than once/i
  }
];

for (const tc of invalidCases) {
  const candidate = Object.assign({}, enode1Profile, tc.patch);
  assert.throws(
    () => api.cfgAssembledSketchSource(candidate),
    tc.match,
    `Expected invalid case "${tc.name}" to throw matching ${tc.match}`
  );
}

// Optional: write generated ENode1 and ENode2 sketches to a target dir if requested via CLI arg
if (process.argv[2]) {
  const outRoot = process.argv[2];
  const e1Dir = path.join(outRoot, 'Gen_ENode1');
  const e2Dir = path.join(outRoot, 'Gen_ENode2');
  fs.mkdirSync(e1Dir, { recursive: true });
  fs.mkdirSync(e2Dir, { recursive: true });
  fs.writeFileSync(path.join(e1Dir, 'Gen_ENode1.ino'), enode1Source1, 'utf8');
  fs.writeFileSync(path.join(e2Dir, 'Gen_ENode2.ino'), enode2Source, 'utf8');
  console.log('Exported generated sketches to', outRoot);
}

console.log('All ECOWAN codegen unit tests (ENode1, ENode2, LightAO+DO, CoreOnly, Determinism, 13 Invalid Config checks) passed.');
