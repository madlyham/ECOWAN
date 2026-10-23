# Edge Monitor — EMF1 Codegen 2.3.0 (Single-Sketch OTA Prefix)

This package makes the firmware source assembly coherent and repeatable while keeping the code generator and codec independent of physical node firmware deployment.

## One source file, sidecar metadata

The wizard assembles **one Arduino `.ino`** for each node. The fixed WLAN OTA maintenance prefix comes first, followed by the generated EMF1 encoder and configured sensor/application section. There is no standalone OTA source chunk for a compiler to concatenate later.

The field bundle stores the sketch in Arduino CLI-compatible structure:

```text
firmware/
  <SketchName>/
    <SketchName>.ino       # complete assembled source: OTA prefix + EMF1/sensor application
BUILD_MANIFEST.json       # source and OTA-block SHA-256; build requirements/status
field-job.json            # normalized job and assembly metadata
node-config.json          # normalized node/sensor configuration
chirpstack_codec.js       # configuration-bound decoder
chirpstack_codec_universal.js
PAYLOAD_CODEC_STANDARD.md
emf1_registry.json
ota_core_contract.json     # approved protected OTA-core fingerprint
```

`field-job.json`, `node-config.json`, `BUILD_MANIFEST.json`, and ChirpStack codec files are **sidecar inputs**, not firmware source fragments. The future compiler should compile the single `.ino`, use the manifest to pin/validate its inputs, and only then publish a `.bin`.

## Assembly contract

- `cfgOtaCorePrefix(payload)` emits the fixed WLAN maintenance prefix and node preamble.
- `cfgGeneratedEmf1Application(payload)` emits the selected EMF1 field encoder, sensor integrations, `setup()` and `loop()`.
- `cfgAssembledSketchSource(payload)` joins those into exactly one source file. The server rejects bundles unless the OTA core occurs exactly once and precedes the generated application marker.
- `ota_core_contract.json` pins the approved version/hash for the immutable OTA core. Both the direct `.ino` download and ZIP endpoint are server-validated against it. `BUILD_MANIFEST.json` records the full `.ino` SHA-256, OTA core block SHA-256, source prefix SHA-256, node identity, EMF1 FPort and registry fingerprint.
- `build_emf1_artifacts.py --check` keeps the common field/wire-type definitions synchronized across the registry, Python decoder, ChirpStack decoder and browser generator.

## Canonical payload standard

- `emf1_registry.json` is the canonical machine-readable definition of EMF1 v1: magic byte, FPort, wire types, common field IDs, aliases, units/scales, and receive-only legacy policy.
- `payload_codec.py`, `chirpstack_codec.js`, browser field metadata, and generated C++ type checks derive their definitions from that standard.
- New generated application payloads use EMF1 v1 only: header `0xE1`, FPort 10, typed big-endian records.
- Existing installed nodes may still be decoded as Legacy Fixed6 on FPort 1, but the wizard cannot generate that format.

## Validation

```sh
./run_codec_tests.sh
```

The suite checks Python syntax/tests, registry drift, shared valid/invalid test vectors, ChirpStack JavaScript behavior, browser codegen, OTA-prefix ordering and hash, one `setup()`/`loop()`, stable field IDs/keys, generated encoder tables, and the actual source/bundle HTTP endpoints including rejection of a tampered OTA block.

## 2.3.0 — dual-output light sensor capability test

- Added a selectable 4-pin AO/DO light sensor module. AO uses an ADC-capable GPIO selected as the row's interface pin; optional D0 is entered as `GPIOxx` in the bus/D0 field.
- Generates raw ADC and digital-threshold readings as distinct EMF1 fields. The digital value is a comparator state, not lux. No calibration or lux conversion is inferred.
- Added basic pin-conflict warnings for the optional D0 pin and updated the review panel to make the missing LoRaWAN integration an explicit pre-compiler gate.
- Validation in this package is static source/JS syntax checking only; this version has not been compiled or flashed.

## Important status

This is still a **source scaffold, not flash-ready firmware**. The code does not yet contain the selected LoRaWAN stack's actual uplink-send integration. Before the future build server compiles it, the project needs an exact ESP32 board/FQBN and partition layout, LoRaWAN library/version, external driver dependencies, region/data-rate payload validation, and production OTA authentication/integrity protection. The `.bin` compiler service and automatic WLAN installation are not implemented. Existing nodes are untouched.
