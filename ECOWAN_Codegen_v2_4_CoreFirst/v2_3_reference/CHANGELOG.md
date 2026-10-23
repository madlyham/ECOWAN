# Changelog

## 2.3.0 — dual-output light sensor capability test

- Added a selectable four-pin light sensor module with AO analog and optional DO digital output.
- Added generator branches for both raw ADC and digital comparator state, with D0 pin validation and radio-pin conflict warnings.
- Made the missing LoRaWAN join/session/uplink integration a prominent pre-compiler gate. This remains a source scaffold, not flash-ready firmware.
- No hardware or compiler validation is claimed.

# Changelog

## 2.2.0 — Single assembled OTA-prefix sketch

- Split the browser generator into `cfgOtaCorePrefix(payload)` and `cfgGeneratedEmf1Application(payload)`, then assemble them into one `.ino`. The OTA source is no longer conceptually a separate downloadable code fragment.
- Renamed the wizard action to “Download combined OTA + EMF1 .ino”. The downloaded file contains OTA maintenance first, then EMF1 type/field encoder and generated sensor logic.
- Changed field bundles to use Arduino CLI-compatible `firmware/<SketchName>/<SketchName>.ino` layout, with one `.ino` file only. Configuration and codecs remain sidecar compiler/profile inputs.
- Added `BUILD_MANIFEST.json` with full-source, source-prefix and OTA-block SHA-256 values; node identity, codec FPort and canonical registry fingerprint; and an explicit not-yet-build-ready status.
- Added server-side checks that the fixed OTA block appears exactly once, comes before generated EMF1/application code, and matches the approved immutable hash in `ota_core_contract.json`. The direct `.ino` download also goes through this validation path.
- Added tests for single-source assembly, marker ordering, sketch-folder naming and OTA-block extraction.
- Validation: 32 Python tests plus JavaScript syntax, dashboard codegen, direct-source/bundle endpoint, OTA hash tamper rejection and ChirpStack codec tests passed during development. Source not compiled or flashed.

## 2.1.0 — EMF1-only code generation baseline

- Made `emf1_registry.json` the authoritative machine-readable registry for EMF1 field IDs and wire types.
- Regenerate the ChirpStack JavaScript and browser UI registry fragments with `build_emf1_artifacts.py`; `--check` fails on drift.
- Removed Legacy Fixed6 from generator choices and source-scaffold output. Retained only receive-side decoding for already-installed nodes on FPort 1.
- Made the generated C++ encoder's common-field and wire-type switches derive from the registry used by the UI.
- Calculate generated packet-buffer capacity from configured fields, with a minimum of 16 bytes and a hard source-scaffold ceiling of 255 bytes. This is not a regional/data-rate LoRaWAN payload-limit check.
- Added tests for generated C++ tables, calculated buffer sizing, common field enforcement, explicit custom wire types, stable IDs/keys and exact browser/backend field-map agreement.
- Validation: 28 Python tests, registry synchronization checks, inline dashboard JavaScript syntax checks, browser generator tests and ChirpStack JavaScript codec conformance tests passed in the development environment.

## Known limits

- Arduino source scaffold has not been compiled or flashed.
- A compiler service, `.bin` publishing, automatic WLAN installation and production OTA authentication/signature validation are not implemented.
- Existing Legacy Fixed6 nodes remain on their current firmware until a deliberate firmware migration is tested.
